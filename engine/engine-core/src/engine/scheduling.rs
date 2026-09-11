use super::*;
use engine_types::orders::{OrderLookup, TerminalOrderStatus};

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn wake_restored_strategies(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        for index in 0..self.host.strategies.len() {
            let id = u16::try_from(index).map_err(|_| {
                EngineError::Boot("configured strategy count exceeds the strategy-id range".into())
            })?;
            self.feed_one_strategy(StrategyId(id), &EngineEvent::Boot, now);
        }
        Ok(())
    }

    pub(super) fn feed_one_strategy(
        &mut self,
        sid: StrategyId,
        event: &EngineEvent,
        now_ns: u64,
    ) -> bool {
        self.host.feed(&self.books, sid, event, now_ns)
    }

    pub(super) fn validate_strategy_event(&self, event: &StrategyEvent) -> Result<(), EngineError> {
        if event.source.0 as usize >= self.host.strategies.len()
            || event.destination.0 as usize >= self.host.strategies.len()
        {
            return Err(EngineError::State(format!(
                "strategy event {} routes from {} to {}, outside {} configured strategies",
                event.event_id,
                event.source.0,
                event.destination.0,
                self.host.strategies.len()
            )));
        }
        if event.kind.is_empty() || event.kind.len() > 256 {
            return Err(EngineError::State(
                "strategy event kind must contain 1..=256 bytes".to_string(),
            ));
        }
        if event.event_id.is_empty() || event.event_id.len() > 256 {
            return Err(EngineError::State(
                "strategy event id must contain 1..=256 bytes".to_string(),
            ));
        }
        if event.payload.len() > engine_types::MAX_STRATEGY_EVENT_BYTES {
            return Err(EngineError::State(format!(
                "strategy event payload is {} bytes; maximum is {}",
                event.payload.len(),
                engine_types::MAX_STRATEGY_EVENT_BYTES
            )));
        }
        Ok(())
    }

    /// Semantic admission for one spooled runtime control request. `Err` is a
    /// refusal of the request itself — a stale, misaddressed, or conflicting
    /// command — never an engine fault; the caller retires the refused
    /// request and keeps running. `Ok(false)` means this exact request was
    /// already accepted and needs no new WAL record.
    pub(super) fn admit_runtime_control(
        &self,
        request: &engine_types::RuntimeControlRequest,
    ) -> Result<bool, String> {
        crate::controls::validate(request)?;
        let expected_name = self
            .host
            .names
            .get(request.strategy.0 as usize)
            .ok_or_else(|| {
                format!(
                    "runtime control request {:?} names strategy {} outside {} configured sleeves",
                    request.request_id,
                    request.strategy.0,
                    self.host.names.len()
                )
            })?;
        if expected_name != &request.strategy_name {
            return Err(format!(
                "runtime control request {:?} binds strategy {} to {:?}, expected {:?}",
                request.request_id, request.strategy.0, request.strategy_name, expected_name
            ));
        }
        if let Some(known) = self.runtime_control_requests.iter().find(|known| {
            known.strategy == request.strategy && known.request_id == request.request_id
        }) {
            if known == request {
                return Ok(false);
            }
            return Err(format!(
                "strategy {} reused runtime request id {:?} with different bytes",
                request.strategy.0, request.request_id
            ));
        }
        if matches!(
            request.command,
            engine_types::RuntimeControlCommand::FlattenDirectional
        ) && self.host.entries_enabled.get(&request.strategy).copied() != Some(false)
        {
            return Err(format!(
                "strategy {} must have a durable entries-disabled override before flatten",
                request.strategy_name
            ));
        }
        Ok(true)
    }

    /// Journal and apply one admitted request. Errors here are engine faults.
    pub(super) fn apply_runtime_control(
        &mut self,
        request: engine_types::RuntimeControlRequest,
    ) -> Result<(), EngineError> {
        self.wal.append(&WalRecord::RuntimeControlAccepted {
            wall_ts_ms: clock::wall_ms(),
            request: request.clone(),
        })?;
        self.wal.barrier()?;
        self.runtime_control_requests.push(request.clone());
        match request.command {
            engine_types::RuntimeControlCommand::SetEntriesEnabled { entries_enabled } => {
                let was = self
                    .host
                    .entries_enabled
                    .insert(request.strategy, entries_enabled);
                // An entry this sleeve decided a moment ago must not go out
                // under the permission the operator has just withdrawn.
                if !entries_enabled && was != Some(false) {
                    self.supersede_openings();
                }
                self.feed_one_strategy(
                    request.strategy,
                    &EngineEvent::EntryPermission {
                        request_id: request.request_id,
                        entries_enabled,
                    },
                    clock::now_ns(),
                );
            }
            engine_types::RuntimeControlCommand::FlattenDirectional => {
                self.feed_one_strategy(
                    request.strategy,
                    &EngineEvent::FlattenDirectional {
                        request_id: request.request_id,
                    },
                    clock::now_ns(),
                );
            }
        }
        self.queue_halted_entry_cancels()?;
        Ok(())
    }

    /// Journal state/control actions before anything later in the same reducer
    /// wake can touch the venue. `Ok(Some(action))` is an ordinary venue action.
    fn handle_durable_action(&mut self, action: Action) -> Result<Option<Action>, EngineError> {
        match action {
            Action::RecordQuoteFill { features } => {
                self.wal.append(&WalRecord::QuoteFill { features })?;
                Ok(None)
            }
            Action::SetStrategyCheckpoint {
                strategy,
                symbol,
                checkpoint,
            } => {
                let owner = self
                    .host
                    .strategies
                    .get(usize::from(strategy.0))
                    .ok_or_else(|| {
                        EngineError::State(format!(
                            "checkpoint names strategy {} outside the configured table",
                            strategy.0
                        ))
                    })?;
                validate_strategy_checkpoint(owner.as_ref(), &checkpoint).map_err(|error| {
                    EngineError::State(format!(
                        "strategy {} refused checkpoint: {error}",
                        self.host
                            .names
                            .get(usize::from(strategy.0))
                            .map(String::as_str)
                            .unwrap_or("unknown")
                    ))
                })?;
                let key = (strategy, symbol);
                if self.host.checkpoints.get(&key) != Some(&checkpoint) {
                    self.wal.append(&WalRecord::StrategyCheckpoint {
                        wall_ts_ms: clock::wall_ms(),
                        strategy,
                        symbol,
                        checkpoint: checkpoint.clone(),
                    })?;
                    self.strategy_barrier_pending = true;
                    self.host.checkpoints.insert(key, checkpoint);
                }
                Ok(None)
            }
            Action::SetStrategyGlobalCheckpoint {
                strategy,
                checkpoint,
            } => {
                let owner = self
                    .host
                    .strategies
                    .get(usize::from(strategy.0))
                    .ok_or_else(|| {
                        EngineError::State(format!(
                            "global checkpoint names strategy {} outside the configured table",
                            strategy.0
                        ))
                    })?;
                validate_strategy_checkpoint(owner.as_ref(), &checkpoint).map_err(|error| {
                    EngineError::State(format!(
                        "strategy {} refused global checkpoint: {error}",
                        self.host
                            .names
                            .get(usize::from(strategy.0))
                            .map(String::as_str)
                            .unwrap_or("unknown")
                    ))
                })?;
                let same = self
                    .host
                    .global_checkpoints
                    .get(&strategy)
                    .is_some_and(|state| state.checkpoint == checkpoint);
                if !same {
                    let state = StrategyGlobalCheckpointState {
                        strategy,
                        checkpoint: checkpoint.clone(),
                        provenance: None,
                    };
                    self.wal.append(&WalRecord::StrategyGlobalCheckpoint {
                        wall_ts_ms: clock::wall_ms(),
                        strategy,
                        checkpoint,
                        provenance: None,
                    })?;
                    self.strategy_barrier_pending = true;
                    self.host.global_checkpoints.insert(strategy, state);
                }
                Ok(None)
            }
            Action::PublishStrategyEvent { event } => {
                self.validate_strategy_event(&event)?;
                let key = (event.source, event.event_id.clone());
                if let Some(known) = self.host.events.get(&key) {
                    if known != &event {
                        return Err(EngineError::State(format!(
                            "strategy {} reused event id {} with different bytes",
                            event.source.0, event.event_id
                        )));
                    }
                    return Ok(None);
                }
                self.wal.append(&WalRecord::StrategyEventPublished {
                    wall_ts_ms: clock::wall_ms(),
                    event: event.clone(),
                })?;
                self.strategy_barrier_pending = true;
                self.host.events.insert(key, event.clone());
                let destination = event.destination;
                self.feed_one_strategy(
                    destination,
                    &EngineEvent::StrategyEvent(event),
                    clock::now_ns(),
                );
                Ok(None)
            }
            Action::ConsumeStrategyEvent {
                source,
                destination,
                event_id,
            } => {
                let key = (source, event_id.clone());
                let Some(event) = self.host.events.get(&key) else {
                    return Ok(None);
                };
                if event.destination != destination {
                    return Err(EngineError::State(format!(
                        "strategy {} cannot consume event {} addressed to {}",
                        destination.0, event_id, event.destination.0
                    )));
                }
                self.wal.append(&WalRecord::StrategyEventConsumed {
                    wall_ts_ms: clock::wall_ms(),
                    source,
                    destination,
                    event_id,
                })?;
                self.strategy_barrier_pending = true;
                self.host.events.remove(&key);
                Ok(None)
            }
            Action::ConsumeSignalObservation {
                strategy,
                source,
                sequence,
                observation_id,
            } => {
                if !self
                    .signals
                    .consumable(strategy, &source, sequence, &observation_id)
                    .map_err(EngineError::State)?
                {
                    return Ok(None);
                }
                self.wal.append(&WalRecord::SignalObservationConsumed {
                    wall_ts_ms: clock::wall_ms(),
                    strategy,
                    source: source.clone(),
                    sequence,
                    observation_id,
                })?;
                self.strategy_barrier_pending = true;
                self.signals.consume(&source, sequence);
                Ok(None)
            }
            Action::RejectSignalObservation {
                strategy,
                source,
                sequence,
                observation_id,
                reason,
            } => {
                if !self
                    .signals
                    .consumable(strategy, &source, sequence, &observation_id)
                    .map_err(EngineError::State)?
                {
                    return Ok(None);
                }
                self.wal.append(&WalRecord::SignalObservationRejected {
                    wall_ts_ms: clock::wall_ms(),
                    strategy,
                    source: source.clone(),
                    sequence,
                    observation_id,
                    reason,
                })?;
                self.strategy_barrier_pending = true;
                self.signals.consume(&source, sequence);
                Ok(None)
            }
            Action::ConsumeRuntimeControl {
                strategy,
                request_id,
            } => {
                let key = (strategy, request_id.clone());
                if self.runtime_control_consumed.contains(&key) {
                    return Ok(None);
                }
                let Some(request) = self.runtime_control_requests.iter().find(|request| {
                    request.strategy == strategy && request.request_id == request_id
                }) else {
                    return Err(EngineError::State(format!(
                        "strategy {} cannot consume unknown runtime request {:?}",
                        strategy.0, request_id
                    )));
                };
                if !matches!(
                    request.command,
                    engine_types::RuntimeControlCommand::FlattenDirectional
                ) {
                    return Err(EngineError::State(format!(
                        "strategy {} cannot consume non-replayable runtime request {:?}",
                        strategy.0, request_id
                    )));
                }
                self.wal.append(&WalRecord::RuntimeControlConsumed {
                    wall_ts_ms: clock::wall_ms(),
                    strategy,
                    request_id: request_id.clone(),
                })?;
                self.strategy_barrier_pending = true;
                self.runtime_control_consumed.insert(key);
                Ok(None)
            }
            other => Ok(Some(other)),
        }
    }

    /// Redeliver WAL-restored messages after every strategy can see its restored
    /// global checkpoint and attributed account state. Their acknowledge
    /// actions enter the ordinary FIFO and are drained when the run starts.
    pub(super) fn redeliver_durable_strategy_inputs(&mut self) {
        let events: Vec<_> = self.host.events.values().cloned().collect();
        let now = clock::now_ns();
        for event in events {
            self.feed_one_strategy(event.destination, &EngineEvent::StrategyEvent(event), now);
        }
        self.deliver_pending_signal_callbacks();
        let flatten: Vec<_> = self
            .runtime_control_requests
            .iter()
            .filter(|request| {
                matches!(
                    request.command,
                    engine_types::RuntimeControlCommand::FlattenDirectional
                ) && !self
                    .runtime_control_consumed
                    .contains(&(request.strategy, request.request_id.clone()))
            })
            .cloned()
            .collect();
        for request in flatten {
            self.feed_one_strategy(
                request.strategy,
                &EngineEvent::FlattenDirectional {
                    request_id: request.request_id,
                },
                now,
            );
        }
    }

    pub(super) async fn take_completion_turn<O: OrderFeed>(
        &mut self,
        completion: MutationCompletion,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        self.take_venue_completion(completion).await?;
        let private_update = tokio::select! {
            biased;
            update = order_feed.next_update(), if !self.order_lineage.waiting() => Some(update),
            _ = std::future::ready(()) => None,
        };
        match private_update {
            Some(Ok(update)) => self.take_update(update).await?,
            Some(Err(engine_types::FeedError::Closed)) => {
                return Err(EngineError::State(
                    "private order feed closed while a venue mutation completed".to_string(),
                ));
            }
            Some(Err(error)) => {
                self.invalidate_private_stream()?;
                tracing::warn!(error = %error, "order feed hiccup after venue mutation");
            }
            None => {}
        }
        self.refresh_account_if_due(clock::now_ns()).await?;
        self.queue_halted_entry_cancels()?;
        self.drain(clock::now_ns()).await
    }

    pub(super) async fn settle_after_market_close<O: OrderFeed>(
        &mut self,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        while self.dispatches.write.is_some() || !self.pending_mutations.is_empty() {
            if self.dispatches.write.is_some() {
                let result =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.dispatches.durable.recv())
                        .await
                        .map_err(|_| {
                            EngineError::TimedOut(
                                "order dispatch durability after market close".into(),
                            )
                        })?;
                self.on_order_dispatch_durable(result).await?;
                self.drain(clock::now_ns()).await?;
                continue;
            }
            let completion =
                tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.venue_completions.recv())
                    .await
                    .map_err(|_| {
                        EngineError::TimedOut(format!(
                            "{} venue mutations after market close",
                            self.pending_mutations.len()
                        ))
                    })?
                    .ok_or(EngineError::TaskStopped {
                        task: EngineTask::Venue,
                        detail: "while the market-close tail was draining",
                    })?;
            self.take_completion_turn(completion, order_feed).await?;
        }
        Ok(())
    }

    // A market event is 1.6 KB (the inline book). It is copied once, into
    // the strategies' event, and otherwise travels by reference.
    pub(super) async fn on_market(&mut self, event: &MarketEvent) -> Result<(), EngineError> {
        let now = clock::now_ns();
        self.books.market.apply(event);
        self.observe_virtual_stops(event)?;
        self.enforce_position_stop_intent().await?;
        match event {
            MarketEvent::Quote { symbol, quote } if quote.bid_px > 0.0 && quote.ask_px > 0.0 => {
                self.risk
                    .observe_price(*symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Depth { symbol, depth }
                if depth.best_bid().is_some() && depth.best_ask().is_some() =>
            {
                let quote = depth.quote();
                self.risk
                    .observe_price(*symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Trades { symbol, trades } if trades.last_px > 0.0 => {
                self.risk.observe_price(*symbol, trades.last_px);
            }
            MarketEvent::Ticker { symbol, ticker } if ticker.last_px > 0.0 => {
                self.risk.observe_price(*symbol, ticker.last_px);
            }
            _ => {}
        }
        self.ledger.saw_event();
        self.events_seen += 1;
        let origin_ns = arrival_ns(event, now);
        let engine_event = EngineEvent::Market(*event);
        {
            let count = self.host.strategies.len();
            let mut feed = |sid| self.host.feed(&self.books, sid, &engine_event, now);
            match event {
                MarketEvent::Quote { symbol, .. } => {
                    for sid in self.routing.quote_listeners(*symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Depth { symbol, .. } => {
                    for sid in self.routing.depth_listeners(*symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Trades { symbol, .. } => {
                    for sid in self.routing.trade_listeners(*symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Ticker { symbol, .. } => {
                    for sid in self.routing.ticker_listeners(*symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::FeedReset { .. } => {
                    for index in 0..count {
                        feed(StrategyId(index as u16));
                    }
                }
            }
        }
        self.drain(origin_ns).await
    }

    pub(super) async fn on_timers(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        let mut due = [None; MAX_TIMER_CALLBACKS_PER_TURN];
        for slot in &mut due {
            let callbacks = &self.host.callbacks;
            let Some(timer) = self.host.timers.pop_due_for(now, |strategy| {
                callbacks.is_active(strategy) && !callbacks.faults.contains_key(&strategy)
            }) else {
                break;
            };
            *slot = Some(timer);
        }
        for (sid, timer, deadline) in due.into_iter().flatten() {
            // A callback may replace a timer already in this turn's snapshot.
            // Newly armed timers fire in a later turn, including zero-delay ones.
            if self.host.timers.is_armed(sid, timer) {
                continue;
            }
            if !self.host.timer_ready(sid) {
                self.host.timers.arm(sid, timer, deadline);
                continue;
            }
            let event = EngineEvent::Timer {
                id: timer,
                now_ns: now,
            };
            if !self.host.feed(&self.books, sid, &event, now) {
                self.host
                    .timers
                    .arm(sid, timer, now.saturating_add(1_000_000_000));
            }
        }
        self.drain(now).await?;
        // Immediately ready timers must also let feed tasks reach the executor.
        tokio::task::yield_now().await;
        Ok(())
    }

    /// Pull every still-live opening order when reconciliation has latched new
    /// exposure off or private-stream continuity is unavailable. The durable
    /// reconciliation state is written before this queue reaches the venue.
    /// Foreign and reduce-only orders are left alone: cancelling another
    /// writer's order or a protective exit is not a safe guess.
    pub(super) fn queue_halted_entry_cancels(&mut self) -> Result<(), EngineError> {
        let entries: Vec<(SymbolId, String)> = self
            .books
            .orders
            .in_flight()
            .into_iter()
            .filter(|order| {
                !order.request.is_sleeve_reduction()
                    && self.opening_refusal(order.request.strategy).is_some()
            })
            .map(|order| (order.request.symbol, order.request.client_order_id.clone()))
            .collect();
        for (symbol, client_order_id) in entries {
            self.enqueue_halt_cancel(symbol, client_order_id);
        }

        // An order that ended by any route — its private update, a recovered
        // fill, a status read — is out of the halt with it.
        let ended: Vec<String> = self
            .halt_cancels
            .keys()
            .filter(|id| !self.is_live_halt_order(id))
            .cloned()
            .collect();
        for id in &ended {
            self.halt_cancels.remove(id);
        }

        let now_ns = clock::now_ns();
        if let Some((client_order_id, state)) =
            self.halt_cancels.iter().find(|(_, state)| match state {
                HaltCancelState::Submitting { .. } => false,
                HaltCancelState::AwaitingPrivate { deadline_ns }
                | HaltCancelState::Resolving { deadline_ns, .. } => now_ns >= *deadline_ns,
            })
        {
            let what = match state {
                HaltCancelState::Resolving { .. } => {
                    "was refused or unanswered and the venue did not settle the order"
                }
                _ => "was accepted but not confirmed by the private stream",
            };
            return Err(EngineError::Reconcile(format!(
                "opening-halt cancellation for {client_order_id} {what} within {} ms",
                HALT_CANCEL_CONFIRM_NS / 1_000_000
            )));
        }
        Ok(())
    }

    pub(super) fn enqueue_halt_cancel(&mut self, symbol: SymbolId, client_order_id: String) {
        if self.halt_cancels.contains_key(&client_order_id) {
            return;
        }
        self.halt_cancels.insert(
            client_order_id.clone(),
            HaltCancelState::Submitting { deadline_ns: None },
        );
        self.halt_cancel_queue.push_back((symbol, client_order_id));
    }

    pub(super) async fn dispatch_halt_cancel_group(&mut self) -> Result<(), EngineError> {
        let mut requests = Vec::with_capacity(MAX_CANCELS_PER_BATCH);
        while requests.len() < MAX_CANCELS_PER_BATCH {
            let Some((symbol, client_order_id)) = self.halt_cancel_queue.pop_front() else {
                break;
            };
            let live = self.is_live_halt_order(&client_order_id);
            if live
                && matches!(
                    self.halt_cancels.get(&client_order_id),
                    Some(HaltCancelState::Submitting { .. })
                )
            {
                requests.push((symbol, client_order_id));
            } else if !live {
                self.halt_cancels.remove(&client_order_id);
            }
        }
        self.process_cancels(requests).await.map(|_| ())
    }

    /// The next halt cancel whose status read is due, while the lookup lane
    /// — one read at a time, shared with ambiguous sends — is free. Due: a
    /// refused or unanswered cancel past its retry instant, or an accepted
    /// one the private stream has left unconfirmed for half the window; the
    /// venue publishes an ending in milliseconds, so half the window of
    /// silence is a dropped update. Other commands in flight on the symbol
    /// do not hold the read: it asks about one order whose own cancel has
    /// already been answered, and a sleeve working the symbol would
    /// otherwise starve it for the whole window.
    fn due_halt_lookup(&self, now_ns: u64) -> Option<(String, SymbolId)> {
        if !self.dispatches.lookup_pending.is_empty() {
            return None;
        }
        self.halt_cancels.iter().find_map(|(id, state)| {
            let due = match state {
                HaltCancelState::Submitting { .. } => false,
                HaltCancelState::Resolving { retry_after_ns, .. } => *retry_after_ns <= now_ns,
                HaltCancelState::AwaitingPrivate { deadline_ns } => {
                    now_ns >= deadline_ns.saturating_sub(HALT_CANCEL_CONFIRM_NS / 2)
                }
            };
            if !due {
                return None;
            }
            let order = self.books.orders.orders.get(id)?;
            Some((id.clone(), order.request.symbol))
        })
    }

    pub(super) fn halt_lookup_due(&self, now_ns: u64) -> bool {
        self.due_halt_lookup(now_ns).is_some()
    }

    /// The next instant the halt needs the loop awake for: a status read
    /// coming due, or a window closing. A read already due but not started
    /// (the lane or the symbol is busy) is not a reason to wake again; the
    /// window's close is.
    pub(super) fn next_halt_wake_ns(&self, now_ns: u64) -> Option<u64> {
        self.halt_cancels
            .values()
            .filter_map(|state| match *state {
                HaltCancelState::Submitting { .. } => None,
                HaltCancelState::AwaitingPrivate { deadline_ns } => {
                    let read_at = deadline_ns.saturating_sub(HALT_CANCEL_CONFIRM_NS / 2);
                    Some(if read_at > now_ns {
                        read_at
                    } else {
                        deadline_ns
                    })
                }
                HaltCancelState::Resolving {
                    deadline_ns,
                    retry_after_ns,
                } => Some(if retry_after_ns > now_ns {
                    retry_after_ns.min(deadline_ns)
                } else {
                    deadline_ns
                }),
            })
            .min()
    }

    pub(super) fn start_halt_lookup(&mut self, now_ns: u64) -> Result<(), EngineError> {
        let Some((id, symbol)) = self.due_halt_lookup(now_ns) else {
            return Ok(());
        };
        if let Some(HaltCancelState::AwaitingPrivate { deadline_ns }) =
            self.halt_cancels.get(&id).copied()
        {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "the private stream has not confirmed the accepted halt cancel of {id}; reading the order's status at the venue"
                ),
            })?;
            self.halt_cancels.insert(
                id.clone(),
                HaltCancelState::Resolving {
                    deadline_ns,
                    retry_after_ns: now_ns,
                },
            );
        }
        if let Err(error) = self.start_order_lookup(id.clone(), symbol) {
            tracing::warn!(id, error = %error, "halt cancel status read not started");
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("status read for {id} not started ({error}); asking again"),
            })?;
            self.retry_halt_lookup(&id, now_ns);
        }
        Ok(())
    }

    fn retry_halt_lookup(&mut self, id: &str, now_ns: u64) {
        if let Some(HaltCancelState::Resolving { retry_after_ns, .. }) =
            self.halt_cancels.get_mut(id)
        {
            *retry_after_ns = now_ns.saturating_add(HALT_LOOKUP_RETRY_NS);
        }
    }

    /// The venue's answer about an order whose halt cancel came back refused
    /// or unanswered. Working: cancel it again. Ended with every fill already
    /// in the log: record the ending, which takes the order out of the halt.
    /// Ended with fills the log has not seen: recover execution history first
    /// and read again. Anything else: read again after `HALT_LOOKUP_RETRY_NS`,
    /// until the deadline set by the first cancel reply ends the run.
    pub(super) async fn apply_halt_lookup(
        &mut self,
        id: &str,
        result: Result<OrderLookup, String>,
    ) -> Result<(), EngineError> {
        let Some(HaltCancelState::Resolving { deadline_ns, .. }) =
            self.halt_cancels.get(id).copied()
        else {
            return Ok(());
        };
        let Some(order) = self
            .books
            .orders
            .orders
            .get(id)
            .filter(|order| order.in_flight())
        else {
            self.halt_cancels.remove(id);
            return Ok(());
        };
        let symbol = order.request.symbol;
        let known_filled = order.filled_exact().map_err(EngineError::State)?;
        let exact_order = order.request.exact_terms.is_some();
        let name = self.books.market.table.name(symbol).to_string();
        let now_ns = clock::now_ns();
        let identity = |row: &engine_types::orders::OrderLookupRow| {
            row.client_order_id == id && row.symbol == name
        };
        let again = HaltCancelState::Resolving {
            deadline_ns,
            retry_after_ns: now_ns.saturating_add(HALT_LOOKUP_RETRY_NS),
        };
        let (next, text) = match result {
            Ok(OrderLookup::Working(row)) if identity(&row) => {
                self.halt_cancel_queue.push_back((symbol, id.to_string()));
                (
                    HaltCancelState::Submitting {
                        deadline_ns: Some(deadline_ns),
                    },
                    format!("{id} is still working at the venue; cancelling it again"),
                )
            }
            Ok(OrderLookup::Terminal { status, row }) if identity(&row) => {
                if row.filled_qty.value > known_filled
                    || (exact_order && row.filled_qty.value != known_filled)
                {
                    self.recovery.history_requested = true;
                    (
                        again,
                        format!(
                            "{id} ended at the venue ({status:?}) but its fill total disagrees with this log; recovering execution history"
                        ),
                    )
                } else {
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "{id} ended at the venue ({status:?}); recording the ending the status read proved"
                        ),
                    })?;
                    let update = match status {
                        TerminalOrderStatus::Rejected => OrderUpdate::Reject {
                            client_order_id: id.into(),
                            code: 0,
                            reason: "venue terminal order lookup: rejected".into(),
                        },
                        TerminalOrderStatus::Filled | TerminalOrderStatus::Cancelled => {
                            OrderUpdate::Cancelled {
                                client_order_id: id.into(),
                                recv_ns: now_ns,
                            }
                        }
                    };
                    // Ending the order removes its halt entry.
                    self.take_update(update).await?;
                    return Ok(());
                }
            }
            Ok(OrderLookup::Working(_)) | Ok(OrderLookup::Terminal { .. }) => (
                again,
                format!(
                    "the venue answered a status read for {id} with another order; asking again"
                ),
            ),
            Ok(OrderLookup::NeverAccepted) => (
                again,
                format!("the venue has no record of {id}; asking again"),
            ),
            Ok(OrderLookup::Unknown { reason }) => (
                again,
                format!("the venue could not settle {id} ({reason}); asking again"),
            ),
            Ok(OrderLookup::Unavailable) => (
                again,
                format!("the venue cannot look up {id} by client order id; asking again"),
            ),
            Err(error) => (
                again,
                format!("status read for {id} failed ({error}); asking again"),
            ),
        };
        self.wal.append(&WalRecord::Note {
            source: "engine".into(),
            text,
        })?;
        self.halt_cancels.insert(id.to_string(), next);
        Ok(())
    }

    pub(super) async fn on_tick(&mut self) -> Result<(), EngineError> {
        self.wal.flush()?;
        self.service_order_lineage().await?;
        self.trim_order_lineage_cache()?;
        // A segment must not expose a queued callback or order disposition
        // before the barrier that owns its publication has completed.
        if self.rotate_after_bytes > 0
            && self.wal.segment_size() >= self.rotate_after_bytes
            && !self.host.callbacks.order_news.pending()
            && !self.host.callbacks.pages.loading()
            && self.dispatches.write.is_none()
            && !self.portfolio_dirty
            && !self.symbol_admission.persisting()
            && !self.recovery.uncommitted()
        {
            let pending: Vec<_> = self.host.effects.transitions.keys().copied().collect();
            for id in pending {
                self.journal_transition(id)?;
            }
            self.flush_strategy_prefix()?;
            let base = self.rotation_base(clock::wall_ms());
            // Timed here rather than inside the log: this is the wait the
            // engine loop actually takes, two fdatasyncs and a directory
            // fsync, with nothing else running.
            let rotating = std::time::Instant::now();
            if self.wal.rotate(&base)? {
                let took_ns = rotating.elapsed().as_nanos() as u64;
                // Read the moment rotation returned, so the segment is the
                // restatement and nothing since.
                let base_bytes = self.wal.segment_size();
                if let Some(heartbeat) = self.heartbeat.as_mut() {
                    heartbeat.record_rotation(took_ns, base_bytes);
                }
                // The log replaces its durability thread's descriptor during
                // a rotation; a run that came back without one pays every
                // later barrier on this loop.
                crate::heartbeat::warn_once_on_caller_thread_barriers(self.wal.durability_mode());
                self.books
                    .orders
                    .try_apply(&base)
                    .map_err(EngineError::State)?;
                if self.host.callbacks.order_news.has_reader() {
                    let reader = self.wal.callback_reader()?.ok_or_else(|| {
                        EngineError::State("rotated WAL lost its callback reader".into())
                    })?;
                    self.host
                        .callbacks
                        .order_news
                        .rotated(reader)
                        .map_err(EngineError::State)?;
                }
                tracing::info!(
                    "log rotated: a fresh segment restates the engine's state; the old \
                     segment stays in place as an archive"
                );
            }
        }
        let now = clock::now_ns();
        // First, and on this tick rather than on a market message: it is the
        // cheapest point in the tick, and it is in front of the account
        // refresh below, which is a venue round trip.
        self.beat(now);
        self.record_trades();
        if self.ledger.due(now) {
            let record = self.ledger.record_for_wal(now);
            self.wal.append(&record)?;
            tracing::info!("latency, {}", self.ledger.plain_line(now));
            self.ledger.reset(now);
        }
        // Any markout whose horizon has come round. Written down because a log
        // holds no prices: this is the one execution number that cannot be
        // worked out later from the records already in it.
        for mark in self.fills.due(now, &self.books.market) {
            self.wal.append(&mark.to_record())?;
        }
        self.checkpoint_history_if_due().await?;
        self.refresh_account_if_due(now).await?;
        self.queue_halted_entry_cancels()?;

        // The maintenance pass uses the latest committed account snapshot.
        let now = clock::now_ns();
        if self.may_open && self.private_stream_ready {
            let mut maintenance = VecDeque::new();
            self.working.pass(
                now,
                &self.books.market,
                &self.books.rules,
                &self.books.orders,
                &mut maintenance,
            );
            let callback_wall_ms = clock::wall_ms();
            self.host
                .pending
                .extend(maintenance.into_iter().map(|action| {
                    let client_order_id = crate::working::worked_order_of(&action);
                    crate::ctx::PendingAction {
                        cause: Some(std::sync::Arc::new(engine_types::DecisionCause {
                            callback_wall_ms,
                            callback_id: None,
                            causes: vec![engine_types::Cause::Working { client_order_id }],
                        })),
                        ..action.into()
                    }
                }));
        }
        // Through the ordinary queue, so the flood cap counts these too.
        self.drain(now).await
    }

    pub(super) fn request_account_refresh_after(&mut self, frontier_ns: u64) {
        if self.account_refresh_started_ns <= frontier_ns {
            self.account_refresh_requested_after = Some(
                self.account_refresh_requested_after
                    .map_or(frontier_ns, |previous| previous.max(frontier_ns)),
            );
        }
    }

    pub(super) fn account_refresh_due(&self, now_ns: u64) -> bool {
        self.account_refresh_requested_after.is_some()
            || now_ns.saturating_sub(self.books.account.observed_ns) >= self.refresh_after_ns
    }

    pub(super) async fn refresh_account_if_due(&mut self, now_ns: u64) -> Result<(), EngineError> {
        self.launch_account_recovery(self.account_refresh_due(now_ns));
        self.service_account_recovery().await
    }

    pub(super) async fn drain(&mut self, origin_ns: u64) -> Result<(), EngineError> {
        self.service_order_lineage().await?;
        self.service_order_dispatches().await?;
        self.service_portfolio_controls().await?;
        self.pull_unconfirmed_amends()?;
        let mut progress = self.drain_progress.take().unwrap_or(DrainProgress {
            origin_ns,
            handled: 0,
            adding_dropped: 0,
        });
        let mut placements = Vec::new();
        let mut cancellations = Vec::new();
        let mut handled_this_turn = 0;
        loop {
            let mut blocked_in_a_row = 0;
            if self.host.pending.is_empty() {
                self.load_ready_wake(&mut progress);
            }
            while let Some(pending) = self.host.pending.pop_front() {
                if self.dispatches.write.is_some()
                    && matches!(
                        pending.action,
                        Action::Place(_) | Action::Amend { .. } | Action::SetStop { .. }
                    )
                {
                    let owner = pending.caller.zip(pending.callback_id);
                    self.dispatches
                        .waiting
                        .push_back((pending, progress.origin_ns));
                    if let Some(owner) = owner {
                        self.host.pending.retain(|queued| {
                            if queued.caller.zip(queued.callback_id) == Some(owner) {
                                self.dispatches
                                    .waiting
                                    .push_back((queued.clone(), progress.origin_ns));
                                false
                            } else {
                                true
                            }
                        });
                    }
                    continue;
                }
                if let Some((caller, callback_id)) = pending.caller.zip(pending.callback_id) {
                    if self
                        .host
                        .effects
                        .earliest(caller)
                        .is_some_and(|earliest| earliest < callback_id)
                    {
                        self.host.pending.push_back(pending);
                        handled_this_turn += 1;
                        blocked_in_a_row += 1;
                        // The transition being waited on can itself be parked
                        // in `ready_actions`, which only loads when this queue
                        // empties. A queue that has rotated whole without
                        // progressing must resume a parked wake, or the two
                        // wait on each other forever.
                        if blocked_in_a_row >= self.host.pending.len()
                            && !self.ready_actions.is_empty()
                        {
                            blocked_in_a_row = 0;
                            self.load_ready_wake(&mut progress);
                        }
                        if handled_this_turn >= MAX_INTENTS_PER_WAKE * 4 {
                            self.flush_placements(
                                std::mem::take(&mut placements),
                                progress.origin_ns,
                            )
                            .await?;
                            self.flush_cancellations(std::mem::take(&mut cancellations))
                                .await?;
                            return self.pause_drain(progress).await;
                        }
                        continue;
                    }
                }
                blocked_in_a_row = 0;
                if let Some(key) = pending.effect {
                    self.journal_transition(key.transition_id)?;
                }
                let action = &pending.action;
                // Ordered effects cannot cross an unsent batch, including
                // durable checkpoint/consume records that have no symbol.
                if !matches!(action, Action::Place(_)) && !placements.is_empty() {
                    let sent = self
                        .flush_placements(std::mem::take(&mut placements), progress.origin_ns)
                        .await?;
                    if sent {
                        self.host.pending.push_front(pending);
                        return self.pause_drain(progress).await;
                    }
                }
                if !matches!(action, Action::Cancel { .. }) && !cancellations.is_empty() {
                    let sent = self
                        .flush_cancellations(std::mem::take(&mut cancellations))
                        .await?;
                    if sent {
                        self.host.pending.push_front(pending);
                        return self.pause_drain(progress).await;
                    }
                }
                if handled_this_turn >= MAX_INTENTS_PER_WAKE * 4 {
                    self.host.pending.push_front(pending);
                    self.flush_placements(std::mem::take(&mut placements), progress.origin_ns)
                        .await?;
                    self.flush_cancellations(std::mem::take(&mut cancellations))
                        .await?;
                    return self.pause_drain(progress).await;
                }
                handled_this_turn += 1;
                if !self.admit_effect_caller(&pending)? {
                    self.complete_effect(pending.effect)?;
                    continue;
                }
                let PendingAction {
                    caller,
                    action,
                    effect,
                    callback_id,
                    timing,
                    cause,
                } = pending;
                let Some(action) = self.handle_durable_action(action)? else {
                    self.complete_effect(effect)?;
                    continue;
                };
                progress.handled += 1;
                if progress.handled > MAX_INTENTS_PER_WAKE && !action.is_risk_reducing() {
                    progress.adding_dropped += 1;
                    if progress.adding_dropped == 1 {
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!("dropped entries and amends: more than {MAX_INTENTS_PER_WAKE} actions in one wake; exits and cancels remain queued"),
                        })?;
                    }
                    if let Action::Place(intent) = &action {
                        self.wal.append(&WalRecord::Intent {
                            intent: intent.clone(),
                            cause: cause.as_deref().cloned().map(Box::new),
                        })?;
                        let order_id = effect.and_then(|key| {
                            self.host
                                .effects
                                .transitions
                                .get(&key.transition_id)
                                .and_then(|transition| transition.order_ids.get(key.index))
                                .cloned()
                                .flatten()
                        });
                        self.tell_refused(
                            intent,
                            super::intent_admission::Refused::plain(
                                super::intent_admission::code::WAKE_ACTION_LIMIT,
                            ),
                            order_id.as_deref(),
                        )?;
                    }
                    self.complete_effect(effect)?;
                    continue;
                }

                let symbol = action
                    .symbol()
                    .expect("durable control actions are handled before symbol dispatch");
                if self.busy_symbols.contains_key(&symbol) {
                    self.defer_action(
                        PendingAction {
                            caller,
                            action,
                            effect,
                            callback_id,
                            timing,
                            cause,
                        },
                        progress.origin_ns,
                    );
                    continue;
                }

                match action {
                    Action::Place(intent) => {
                        placements.push((intent, effect, timing, cause));
                        if placements.len() == MAX_ORDERS_PER_BATCH {
                            let sent = self
                                .flush_placements(
                                    std::mem::take(&mut placements),
                                    progress.origin_ns,
                                )
                                .await?;
                            if sent && !self.host.pending.is_empty() {
                                return self.pause_drain(progress).await;
                            }
                        }
                    }
                    Action::Cancel {
                        symbol,
                        client_order_id,
                    } => {
                        cancellations.push((symbol, client_order_id, effect));
                        if cancellations.len() == MAX_CANCELS_PER_BATCH {
                            let sent = self
                                .flush_cancellations(std::mem::take(&mut cancellations))
                                .await?;
                            if sent && !self.host.pending.is_empty() {
                                return self.pause_drain(progress).await;
                            }
                        }
                    }
                    Action::Amend {
                        symbol,
                        client_order_id,
                        spec,
                    } => {
                        let taken = self
                            .process_amend(
                                symbol,
                                &client_order_id,
                                spec.clone(),
                                progress.origin_ns,
                            )
                            .await?;
                        self.working
                            .amended(&client_order_id, spec.px, taken, clock::now_ns());
                        self.complete_effect(effect)?;
                        if !self.host.pending.is_empty() {
                            return self.pause_drain(progress).await;
                        }
                    }
                    Action::SetStop { symbol, trigger_px } => {
                        self.process_set_stop(caller, symbol, trigger_px).await?;
                        self.complete_effect(effect)?;
                        if !self.host.pending.is_empty() {
                            return self.pause_drain(progress).await;
                        }
                    }
                    Action::RecordQuoteFill { .. } => {
                        unreachable!("quote-fill receipts are journaled before venue actions")
                    }
                    Action::SetStrategyCheckpoint { .. } => {
                        unreachable!("strategy checkpoints are journaled before venue actions")
                    }
                    Action::SetStrategyGlobalCheckpoint { .. }
                    | Action::PublishStrategyEvent { .. }
                    | Action::ConsumeStrategyEvent { .. }
                    | Action::ConsumeSignalObservation { .. }
                    | Action::RejectSignalObservation { .. }
                    | Action::ConsumeRuntimeControl { .. } => {
                        unreachable!("strategy control state is journaled before venue actions")
                    }
                }
            }
            let sent = self
                .flush_placements(std::mem::take(&mut placements), progress.origin_ns)
                .await?;
            if sent && !self.host.pending.is_empty() {
                return self.pause_drain(progress).await;
            }
            let cancelled = self
                .flush_cancellations(std::mem::take(&mut cancellations))
                .await?;
            if cancelled && !self.host.pending.is_empty() {
                return self.pause_drain(progress).await;
            }
            if self.host.pending.is_empty() && !self.ready_actions.is_empty() {
                self.load_ready_wake(&mut progress);
                continue;
            }
            if self.host.pending.is_empty() {
                break;
            }
        }
        self.flush_strategy_prefix()?;
        self.park_dispatch_wake(&progress);
        if progress.adding_dropped > 0 {
            tracing::error!(
                adding_dropped = progress.adding_dropped,
                "too many actions in one wake; entries and amends were dropped"
            );
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "dropped {} entries and amends: more than {MAX_INTENTS_PER_WAKE} actions in one wake (exits and cancels still flowed)",
                    progress.adding_dropped
                ),
            })?;
        }
        Ok(())
    }

    fn admit_effect_caller(&mut self, pending: &PendingAction) -> Result<bool, EngineError> {
        let Some(caller) = pending.caller else {
            return Ok(true);
        };
        let allowed = match &pending.action {
            Action::Cancel {
                symbol,
                client_order_id,
            }
            | Action::Amend {
                symbol,
                client_order_id,
                ..
            } => self
                .books
                .orders
                .orders
                .get(client_order_id)
                .is_some_and(|order| {
                    order.request.strategy == caller && order.request.symbol == *symbol
                }),
            Action::SetStop { symbol, .. } => {
                if self.instrument_specs.contains_key(symbol) {
                    self.books.attribution.signed(caller, *symbol) != 0.0
                } else {
                    self.books.attribution.sole_owner(*symbol) == Some(caller)
                }
            }
            _ => true,
        };
        if !allowed {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "strategy {} effect refused: foreign_or_unknown_effect_owner: {:?}",
                    caller.0, pending.action
                ),
            })?;
        }
        Ok(allowed)
    }

    /// End one venue-mutation turn without ending its strategy wake. The
    /// batch has completed its record/send/reply sequence (and, for entries,
    /// its durability barrier); this only keeps the flood counters and
    /// latency origin while the run loop polls account-safety inputs.
    async fn pause_drain(&mut self, progress: DrainProgress) -> Result<(), EngineError> {
        self.flush_strategy_prefix()?;
        self.drain_progress = Some(progress);
        tokio::task::yield_now().await;
        Ok(())
    }

    fn defer_action(&mut self, pending: PendingAction, origin_ns: u64) {
        let action = &pending.action;
        let symbol = action
            .symbol()
            .expect("only symbol-scoped venue actions can be deferred");
        if let Some(key) = pending.effect {
            let mut suffix = VecDeque::new();
            self.host.pending.retain(|queued| {
                if queued
                    .effect
                    .is_some_and(|other| other.transition_id == key.transition_id)
                {
                    suffix.push_back((queued.clone(), origin_ns));
                    false
                } else {
                    true
                }
            });
            let queue = self.deferred_actions.entry(symbol).or_default();
            queue.push_back((pending, origin_ns));
            queue.append(&mut suffix);
            return;
        }
        let queue = self.deferred_actions.entry(symbol).or_default();
        match &action {
            Action::Amend {
                client_order_id, ..
            } => {
                if queue.iter().any(|(queued, _)| {
                    matches!(&queued.action, Action::Cancel { client_order_id: queued_id, .. } if queued_id == client_order_id)
                }) {
                    return;
                }
                queue.retain(|(queued, _)| {
                    !matches!(&queued.action, Action::Amend { client_order_id: queued_id, .. } if queued_id == client_order_id)
                });
            }
            Action::Cancel {
                client_order_id, ..
            } => {
                if queue.iter().any(|(queued, _)| {
                    matches!(&queued.action, Action::Cancel { client_order_id: queued_id, .. } if queued_id == client_order_id)
                }) {
                    return;
                }
                queue.retain(|(queued, _)| {
                    !matches!(&queued.action, Action::Amend { client_order_id: queued_id, .. } if queued_id == client_order_id)
                });
            }
            Action::SetStop { trigger_px, .. } => {
                let owned = pending
                    .caller
                    .map(|caller| self.books.attribution.signed(caller, symbol));
                if trigger_px.is_finite() && *trigger_px > 0.0 {
                    let same_owner_stop = |queued: &PendingAction| {
                        if queued.caller != pending.caller {
                            return None;
                        }
                        match queued.action {
                            Action::SetStop {
                                trigger_px: old, ..
                            } if old.is_finite() && old > 0.0 => Some(old),
                            _ => None,
                        }
                    };
                    if queue.iter().any(|(queued, _)| {
                        same_owner_stop(queued).is_some_and(|old| {
                            old == *trigger_px
                                || owned.is_some_and(|qty| {
                                    if qty > 0.0 {
                                        old >= *trigger_px
                                    } else if qty < 0.0 {
                                        old <= *trigger_px
                                    } else {
                                        false
                                    }
                                })
                        })
                    }) {
                        return;
                    }
                    if owned.is_some_and(|qty| qty != 0.0) {
                        queue.retain(|(queued, _)| same_owner_stop(queued).is_none());
                    }
                }
            }
            Action::Place(intent)
                if !intent.reduce_only
                    && intent.tag == "quote"
                    && matches!(
                        intent.kind,
                        OrderKind::Limit {
                            tif: TimeInForce::PostOnly,
                            ..
                        }
                    ) =>
            {
                queue.retain(|(queued, _)| {
                    !matches!(
                        &queued.action,
                        Action::Place(older)
                            if !older.reduce_only
                                && older.strategy == intent.strategy
                                && older.side == intent.side
                                && older.tag == intent.tag
                                && matches!(
                                    older.kind,
                                    OrderKind::Limit {
                                        tif: TimeInForce::PostOnly,
                                        ..
                                    }
                                )
                    )
                });
            }
            Action::Place(_) => {}
            Action::RecordQuoteFill { .. } => {}
            Action::SetStrategyCheckpoint { .. } => {}
            Action::SetStrategyGlobalCheckpoint { .. }
            | Action::PublishStrategyEvent { .. }
            | Action::ConsumeStrategyEvent { .. }
            | Action::ConsumeSignalObservation { .. }
            | Action::RejectSignalObservation { .. }
            | Action::ConsumeRuntimeControl { .. } => {}
        }
        queue.push_back((pending, origin_ns));
    }

    fn park_dispatch_wake(&mut self, progress: &DrainProgress) {
        if self
            .dispatches
            .waiting
            .iter()
            .any(|(_, origin)| *origin == progress.origin_ns)
        {
            self.suspended_wakes
                .insert(progress.origin_ns, progress.clone());
        }
    }

    fn load_ready_wake(&mut self, progress: &mut DrainProgress) {
        let Some((action, origin_ns)) = self.ready_actions.pop_front() else {
            return;
        };
        self.park_dispatch_wake(progress);
        *progress = self
            .suspended_wakes
            .remove(&origin_ns)
            .unwrap_or(DrainProgress {
                origin_ns,
                handled: 0,
                adding_dropped: 0,
            });
        self.host.pending.push_back(action);
        while self
            .ready_actions
            .front()
            .is_some_and(|(_, queued_origin)| *queued_origin == origin_ns)
        {
            let (action, _) = self.ready_actions.pop_front().expect("front checked above");
            self.host.pending.push_back(action);
        }
    }

    pub(super) fn mark_symbols_busy(&mut self, symbols: impl IntoIterator<Item = SymbolId>) {
        for symbol in symbols {
            *self.busy_symbols.entry(symbol).or_default() += 1;
        }
    }

    pub(super) fn release_symbols(&mut self, symbols: impl IntoIterator<Item = SymbolId>) {
        let mut ready = Vec::new();
        for symbol in symbols {
            let Some(count) = self.busy_symbols.get_mut(&symbol) else {
                continue;
            };
            *count -= 1;
            if *count == 0 {
                self.busy_symbols.remove(&symbol);
                ready.push(symbol);
            }
        }
        for symbol in ready {
            if let Some(mut queued) = self.deferred_actions.remove(&symbol) {
                while let Some((action, origin_ns)) = queued.pop_front() {
                    self.ready_actions.push_back((action, origin_ns));
                }
            }
        }
    }
}

#[cfg(test)]
mod dispatch_budget_tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn deferred_stops_keep_each_owner_and_never_replace_a_tighter_target() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let symbol = SymbolId(0);
        engine
            .books
            .attribution
            .note(StrategyId(0), symbol, Side::Buy, 1.0);
        engine
            .books
            .attribution
            .note(StrategyId(1), symbol, Side::Sell, 1.0);
        let mut enqueue = |owner, trigger_px| {
            engine.defer_action(
                PendingAction {
                    caller: Some(StrategyId(owner)),
                    action: Action::SetStop { symbol, trigger_px },
                    effect: None,
                    callback_id: None,
                    timing: None,
                    cause: None,
                },
                1,
            );
        };
        enqueue(0, 95.0);
        enqueue(1, 105.0);
        enqueue(0, 94.0);
        for _ in 0..1000 {
            enqueue(0, 95.0);
        }
        let queued = &engine.deferred_actions[&symbol];
        assert_eq!(
            queued.len(),
            2,
            "different sleeves must retain separate stop obligations during overload"
        );
        assert!(queued.iter().any(|(p, _)| p.caller == Some(StrategyId(0))
            && matches!(
                p.action,
                Action::SetStop {
                    trigger_px: 95.0,
                    ..
                }
            )));
        assert!(queued.iter().any(|(p, _)| p.caller == Some(StrategyId(1))
            && matches!(
                p.action,
                Action::SetStop {
                    trigger_px: 105.0,
                    ..
                }
            )));
    }

    #[tokio::test(start_paused = true)]
    async fn a_durable_dispatch_wait_preserves_the_original_wake_budget() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let original = DrainProgress {
            origin_ns: 123,
            handled: MAX_INTENTS_PER_WAKE,
            adding_dropped: 7,
        };
        let action: PendingAction = Action::Cancel {
            symbol: SymbolId(0),
            client_order_id: "exit-owner".into(),
        }
        .into();
        engine
            .dispatches
            .waiting
            .push_back((action, original.origin_ns));
        engine.park_dispatch_wake(&original);
        assert!(
            engine.drain_progress.is_none(),
            "waiting for fsync must leave market turns selectable"
        );
        engine.ready_actions.append(&mut engine.dispatches.waiting);
        let mut unrelated = DrainProgress {
            origin_ns: 456,
            handled: 0,
            adding_dropped: 0,
        };
        engine.load_ready_wake(&mut unrelated);
        assert_eq!(unrelated.origin_ns, 123);
        assert_eq!(
            unrelated.handled, MAX_INTENTS_PER_WAKE,
            "an fsync must not admit another opening flood"
        );
        assert_eq!(unrelated.adding_dropped, 7);
        assert!(engine.suspended_wakes.is_empty());
        assert_eq!(
            engine.host.pending.len(),
            1,
            "the suffix survives the same wait"
        );
    }

    /// A journaled transition parked in `ready_actions` while later actions
    /// from the same strategy sit in `host.pending`: the pending actions wait
    /// on the transition and the transition waits on the queue emptying.
    #[tokio::test(start_paused = true)]
    async fn a_parked_transition_runs_before_the_later_actions_that_wait_on_it() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let symbol = SymbolId(0);
        let consume = Action::ConsumeStrategyEvent {
            source: StrategyId(0),
            destination: StrategyId(0),
            event_id: "already-consumed".into(),
        };
        let parked = engine
            .host
            .effects
            .capture(StrategyId(0), vec![consume.clone()]);
        engine.ready_actions.push_back((
            PendingAction {
                caller: Some(StrategyId(0)),
                action: consume,
                effect: Some(crate::effects::EffectKey {
                    transition_id: parked,
                    index: 0,
                }),
                callback_id: Some(parked),
                timing: None,
                cause: None,
            },
            2,
        ));
        engine.host.pending.push_back(PendingAction {
            caller: Some(StrategyId(0)),
            action: Action::Cancel {
                symbol,
                client_order_id: "later".into(),
            },
            effect: None,
            callback_id: Some(parked + 1),
            timing: None,
            cause: None,
        });

        engine.drain(1).await.unwrap();

        assert!(
            engine.host.effects.transitions.is_empty(),
            "transition {parked} never ran: {:?}",
            engine.host.effects.transitions.keys().collect::<Vec<_>>()
        );
        assert!(engine.ready_actions.is_empty(), "the parked wake never ran");
        assert!(
            engine.host.pending.is_empty(),
            "{:?}",
            engine
                .host
                .pending
                .iter()
                .map(|queued| queued.callback_id)
                .collect::<Vec<_>>()
        );
        assert!(
            engine.drain_progress.is_none(),
            "the drain never reached the end of its queue"
        );
    }
}
