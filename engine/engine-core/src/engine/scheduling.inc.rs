impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    fn wake_restored_strategies(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        for index in 0..self.strategies.len() {
            let id = u16::try_from(index).map_err(|_| {
                EngineError::Boot("configured strategy count exceeds the strategy-id range".into())
            })?;
            self.feed_one_strategy(StrategyId(id), &EngineEvent::Boot, now);
        }
        Ok(())
    }

    fn feed_one_strategy(&mut self, sid: StrategyId, event: &EngineEvent, now_ns: u64) {
        let Engine {
            strategies,
            market,
            timers,
            pending,
            orders,
            registry,
            attribution,
            covers,
            strategy_checkpoints,
            strategy_global_checkpoints,
            strategy_events,
            runtime_entries_enabled,
            names,
            account,
            rules,
            ..
        } = self;
        feed_strategy(
            strategies,
            market,
            account,
            rules,
            timers,
            pending,
            orders,
            registry,
            attribution,
            covers,
            strategy_checkpoints,
            strategy_global_checkpoints,
            strategy_events,
            names,
            runtime_entries_enabled,
            sid,
            event,
            now_ns,
        );
    }

    fn validate_strategy_event(&self, event: &StrategyEvent) -> Result<(), EngineError> {
        if event.source.0 as usize >= self.strategies.len()
            || event.destination.0 as usize >= self.strategies.len()
        {
            return Err(EngineError::State(format!(
                "strategy event {} routes from {} to {}, outside {} configured strategies",
                event.event_id,
                event.source.0,
                event.destination.0,
                self.strategies.len()
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
    fn admit_runtime_control(
        &self,
        request: &engine_types::RuntimeControlRequest,
    ) -> Result<bool, String> {
        crate::controls::validate(request)?;
        let expected_name = self.names.get(request.strategy.0 as usize).ok_or_else(|| {
            format!(
                "runtime control request {:?} names strategy {} outside {} configured sleeves",
                request.request_id,
                request.strategy.0,
                self.names.len()
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
        ) && self
            .runtime_entries_enabled
            .get(&request.strategy.0)
            .copied()
            != Some(false)
        {
            return Err(format!(
                "strategy {} must have a durable entries-disabled override before flatten",
                request.strategy_name
            ));
        }
        Ok(true)
    }

    /// Journal and apply one admitted request. Errors here are engine faults.
    fn apply_runtime_control(
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
                self.runtime_entries_enabled
                    .insert(request.strategy.0, entries_enabled);
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
                let owner = self.strategies.get(usize::from(strategy.0)).ok_or_else(|| {
                    EngineError::State(format!(
                        "checkpoint names strategy {} outside the configured table",
                        strategy.0
                    ))
                })?;
                validate_strategy_checkpoint(owner.as_ref(), &checkpoint).map_err(|error| {
                    EngineError::State(format!(
                        "strategy {} refused checkpoint: {error}",
                        self.names
                            .get(usize::from(strategy.0))
                            .map(String::as_str)
                            .unwrap_or("unknown")
                    ))
                })?;
                let key = (strategy.0, symbol.0);
                if self.strategy_checkpoints.get(&key) != Some(&checkpoint) {
                    self.strategy_checkpoints.insert(key, checkpoint.clone());
                    self.wal.append(&WalRecord::StrategyCheckpoint {
                        wall_ts_ms: clock::wall_ms(),
                        strategy,
                        symbol,
                        checkpoint,
                    })?;
                    self.wal.barrier()?;
                }
                Ok(None)
            }
            Action::SetStrategyGlobalCheckpoint {
                strategy,
                checkpoint,
            } => {
                let owner = self.strategies.get(usize::from(strategy.0)).ok_or_else(|| {
                    EngineError::State(format!(
                        "global checkpoint names strategy {} outside the configured table",
                        strategy.0
                    ))
                })?;
                validate_strategy_checkpoint(owner.as_ref(), &checkpoint).map_err(|error| {
                    EngineError::State(format!(
                        "strategy {} refused global checkpoint: {error}",
                        self.names
                            .get(usize::from(strategy.0))
                            .map(String::as_str)
                            .unwrap_or("unknown")
                    ))
                })?;
                let same = self
                    .strategy_global_checkpoints
                    .get(&strategy.0)
                    .is_some_and(|state| state.checkpoint == checkpoint);
                if !same {
                    let state = StrategyGlobalCheckpointState {
                        strategy,
                        checkpoint: checkpoint.clone(),
                        provenance: None,
                    };
                    self.strategy_global_checkpoints.insert(strategy.0, state);
                    self.wal.append(&WalRecord::StrategyGlobalCheckpoint {
                        wall_ts_ms: clock::wall_ms(),
                        strategy,
                        checkpoint,
                        provenance: None,
                    })?;
                    self.wal.barrier()?;
                }
                Ok(None)
            }
            Action::PublishStrategyEvent { event } => {
                self.validate_strategy_event(&event)?;
                let key = (event.source.0, event.event_id.clone());
                if let Some(known) = self.strategy_events.get(&key) {
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
                self.wal.barrier()?;
                self.strategy_events.insert(key, event.clone());
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
                let key = (source.0, event_id.clone());
                let Some(event) = self.strategy_events.get(&key) else {
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
                self.wal.barrier()?;
                self.strategy_events.remove(&key);
                Ok(None)
            }
            Action::ConsumeSignalObservation {
                strategy,
                source,
                sequence,
                observation_id,
            } => {
                let key = (source.clone(), sequence);
                let Some(observation) = self.signal_observations.get(&key) else {
                    return Ok(None);
                };
                if observation.destination != strategy
                    || observation.observation_id != observation_id
                {
                    return Err(EngineError::State(format!(
                        "strategy {} cannot consume signal {} #{} {}",
                        strategy.0, source, sequence, observation_id
                    )));
                }
                self.wal.append(&WalRecord::SignalObservationConsumed {
                    wall_ts_ms: clock::wall_ms(),
                    strategy,
                    source,
                    sequence,
                    observation_id,
                })?;
                self.wal.barrier()?;
                self.signal_observations.remove(&key);
                Ok(None)
            }
            Action::ConsumeRuntimeControl {
                strategy,
                request_id,
            } => {
                let key = (strategy.0, request_id.clone());
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
                self.wal.barrier()?;
                self.runtime_control_consumed.insert(key);
                Ok(None)
            }
            other => Ok(Some(other)),
        }
    }

    /// Redeliver WAL-restored messages after every strategy can see its restored
    /// global checkpoint and attributed account state. Their acknowledge
    /// actions enter the ordinary FIFO and are drained when the run starts.
    fn redeliver_durable_strategy_inputs(&mut self) {
        let events: Vec<_> = self.strategy_events.values().cloned().collect();
        let observations: Vec<_> = self.signal_observations.values().cloned().collect();
        let now = clock::now_ns();
        for event in events {
            self.feed_one_strategy(
                event.destination,
                &EngineEvent::StrategyEvent(event),
                now,
            );
        }
        for observation in observations {
            self.feed_one_strategy(
                observation.destination,
                &EngineEvent::Signal(observation),
                now,
            );
        }
        let flatten: Vec<_> = self
            .runtime_control_requests
            .iter()
            .filter(|request| {
                matches!(
                    request.command,
                    engine_types::RuntimeControlCommand::FlattenDirectional
                ) && !self
                    .runtime_control_consumed
                    .contains(&(request.strategy.0, request.request_id.clone()))
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

    async fn take_completion_turn<O: OrderFeed>(
        &mut self,
        completion: MutationCompletion,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        self.take_venue_completion(completion).await?;
        let private_update = tokio::select! {
            biased;
            update = order_feed.next_update() => Some(update),
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
        self.drain(clock::now_ns()).await
    }

    async fn settle_after_market_close<O: OrderFeed>(
        &mut self,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        while !self.pending_mutations.is_empty() {
            let completion =
                tokio::time::timeout(Duration::from_secs(10), self.venue_completions.recv())
                    .await
                    .map_err(|_| {
                        EngineError::State(format!(
                            "market feed closed with {} venue mutations still outstanding",
                            self.pending_mutations.len()
                        ))
                    })?
                    .ok_or_else(|| {
                        EngineError::State(
                            "venue task stopped while the market-close tail was draining"
                                .to_string(),
                        )
                    })?;
            self.take_completion_turn(completion, order_feed).await?;
        }
        Ok(())
    }

    async fn on_market(&mut self, event: MarketEvent) -> Result<(), EngineError> {
        let now = clock::now_ns();
        self.market.apply(&event);
        match event {
            MarketEvent::Quote { symbol, quote } if quote.bid_px > 0.0 && quote.ask_px > 0.0 => {
                self.risk
                    .observe_price(symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Depth { symbol, depth }
                if depth.best_bid().is_some() && depth.best_ask().is_some() =>
            {
                let quote = depth.quote();
                self.risk
                    .observe_price(symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Trades { symbol, trades } if trades.last_px > 0.0 => {
                self.risk.observe_price(symbol, trades.last_px);
            }
            MarketEvent::Ticker { symbol, ticker } if ticker.last_px > 0.0 => {
                self.risk.observe_price(symbol, ticker.last_px);
            }
            _ => {}
        }
        self.ledger.saw_event();
        self.events_seen += 1;
        let origin_ns = arrival_ns(&event, now);
        let engine_event = EngineEvent::Market(event);
        {
            let Engine {
                strategies,
                market,
                timers,
                pending,
                routing,
                orders,
                registry,
                attribution,
                covers,
                strategy_checkpoints,
                strategy_global_checkpoints,
                strategy_events,
                runtime_entries_enabled,
                names,
                account,
                rules,
                ..
            } = self;
            let count = strategies.len();
            let mut feed = |sid| {
                feed_strategy(
                    strategies,
                    market,
                    account,
                    rules,
                    timers,
                    pending,
                    orders,
                    registry,
                    attribution,
                    covers,
                    strategy_checkpoints,
                    strategy_global_checkpoints,
                    strategy_events,
                    names,
                    runtime_entries_enabled,
                    sid,
                    &engine_event,
                    now,
                )
            };
            match event {
                MarketEvent::Quote { symbol, .. } => {
                    for sid in routing.quote_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Depth { symbol, .. } => {
                    for sid in routing.depth_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Trades { symbol, .. } => {
                    for sid in routing.trade_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Ticker { symbol, .. } => {
                    for sid in routing.ticker_listeners(symbol) {
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

    async fn on_timers(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        while let Some((sid, timer)) = self.timers.pop_due(now) {
            let event = EngineEvent::Timer {
                id: timer,
                now_ns: now,
            };
            let Engine {
                strategies,
                market,
                timers,
                pending,
                orders,
                registry,
                attribution,
                covers,
                strategy_checkpoints,
                strategy_global_checkpoints,
                strategy_events,
                runtime_entries_enabled,
                names,
                account,
                rules,
                ..
            } = self;
            feed_strategy(
                strategies,
                market,
                account,
                rules,
                timers,
                pending,
                orders,
                registry,
                attribution,
                covers,
                strategy_checkpoints,
                strategy_global_checkpoints,
                strategy_events,
                names,
                runtime_entries_enabled,
                sid,
                &event,
                now,
            );
        }
        self.drain(now).await
    }

    /// Validate one external envelope and hold it until its requested symbol
    /// set is aligned across every engine table. Old spool rows are ignored by
    /// the durable per-source cursor; a gap stops delivery.
    fn queue_signal_observation(
        &mut self,
        observation: SignalObservation,
    ) -> Result<(), EngineError> {
        crate::signals::validate(&observation).map_err(EngineError::State)?;
        if observation.destination.0 as usize >= self.strategies.len() {
            return Err(EngineError::State(format!(
                "signal {} #{} addresses strategy {}, but only {} are configured",
                observation.source,
                observation.sequence,
                observation.destination.0,
                self.strategies.len()
            )));
        }

        let cursor = self.signal_cursors.get(&observation.source);
        if let Some(cursor) = cursor {
            if observation.sequence < cursor.sequence {
                return Ok(());
            }
            if observation.sequence == cursor.sequence {
                if observation.content_sha256 != cursor.content_sha256 {
                    return Err(EngineError::State(format!(
                        "signal source {} rewrote durable sequence {}",
                        observation.source, observation.sequence
                    )));
                }
                return Ok(());
            }
        }
        if let Some(queued) = self.pending_signal_deliveries.iter().find(|queued| {
            queued.source == observation.source && queued.sequence == observation.sequence
        }) {
            if queued != &observation {
                return Err(EngineError::State(format!(
                    "signal source {} reused queued sequence {} with different bytes",
                    observation.source, observation.sequence
                )));
            }
            return Ok(());
        }
        let expected = self
            .pending_signal_deliveries
            .iter()
            .rev()
            .find(|queued| queued.source == observation.source)
            .map(|queued| queued.sequence.saturating_add(1))
            .or_else(|| cursor.map(|known| known.sequence.saturating_add(1)))
            .unwrap_or(1);
        if observation.sequence < expected {
            tracing::warn!(
                source = %observation.source,
                sequence = observation.sequence,
                expected,
                "signal row is behind the rows already queued; dropped"
            );
            return Ok(());
        }
        if observation.sequence > expected {
            // The spool no longer holds the rows in between and a restart
            // would meet the same gap. The engine goes on from the row it
            // has; the cursor records the jump.
            tracing::error!(
                source = %observation.source,
                expected,
                got = observation.sequence,
                skipped = observation.sequence - expected,
                "signal source has a sequence gap; continuing from the row on hand"
            );
        }

        let route = (observation.source.clone(), observation.destination.0);
        let mut durable = self
            .signal_subscriptions
            .get(&route)
            .map(|row| row.subscriptions.clone())
            .unwrap_or_default();
        for queued in self.pending_signal_deliveries.iter().filter(|queued| {
            queued.source == observation.source
                && queued.destination == observation.destination
        }) {
            for subscription in &queued.subscriptions {
                if !durable.contains(subscription) {
                    durable.push(subscription.clone());
                }
            }
        }
        for subscription in &observation.subscriptions {
            if !durable.contains(subscription) {
                durable.push(subscription.clone());
            }
        }
        if durable.len() > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS {
            return Err(EngineError::State(format!(
                "signal source {} would retain {} subscriptions for strategy {}; maximum is {}",
                observation.source,
                durable.len(),
                observation.destination.0,
                engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
            )));
        }

        for subscription in &observation.subscriptions {
            let listener = (observation.destination, subscription.feed);
            let subscribed = self.subscriptions.contains(subscription);
            if let Some(symbol) = self
                .market
                .table
                .get(&subscription.symbol)
                .filter(|_| subscribed)
            {
                self.routing.add(symbol, subscription.feed, observation.destination);
                continue;
            }
            if let Some(wanted) = self
                .wanted_symbols
                .iter_mut()
                .find(|wanted| wanted.name == subscription.symbol)
            {
                if !wanted.listeners.contains(&listener) {
                    wanted.listeners.push(listener);
                }
            } else {
                self.wanted_symbols.push(WantedSymbol {
                    name: subscription.symbol.clone(),
                    listeners: vec![listener],
                });
            }
        }
        self.pending_signal_deliveries.push_back(observation);
        Ok(())
    }

    /// Append and barrier every fully admitted signal before reducer delivery.
    fn accept_pending_signals(&mut self) -> Result<(), EngineError> {
        let observations = std::mem::take(&mut self.pending_signal_deliveries);
        let now = clock::now_ns();
        for observation in observations {
            for subscription in &observation.subscriptions {
                let Some(symbol) = self.market.table.get(&subscription.symbol) else {
                    return Err(EngineError::State(format!(
                        "signal {} #{} symbol {} was not admitted",
                        observation.source, observation.sequence, subscription.symbol
                    )));
                };
                if !self.subscriptions.contains(subscription)
                    || self
                        .rules
                        .get(symbol.0 as usize)
                        .copied()
                        .flatten()
                        .is_none()
                {
                    return Err(EngineError::State(format!(
                        "signal {} #{} symbol/feed/rule {} {:?} is incomplete",
                        observation.source,
                        observation.sequence,
                        subscription.symbol,
                        subscription.feed
                    )));
                }
            }
            self.wal.append(&WalRecord::SignalObservation {
                wall_ts_ms: clock::wall_ms(),
                observation: observation.clone(),
            })?;
            self.wal.barrier()?;
            self.signal_cursors.insert(
                observation.source.clone(),
                SignalCursor {
                    source: observation.source.clone(),
                    sequence: observation.sequence,
                    content_sha256: observation.content_sha256.clone(),
                },
            );
            let subscriptions = self
                .signal_subscriptions
                .entry((observation.source.clone(), observation.destination.0))
                .or_insert_with(|| SignalSubscriptionState {
                    source: observation.source.clone(),
                    destination: observation.destination,
                    subscriptions: Vec::new(),
                });
            for subscription in &observation.subscriptions {
                if !subscriptions.subscriptions.contains(subscription) {
                    subscriptions.subscriptions.push(subscription.clone());
                }
            }
            self.signal_observations.insert(
                (observation.source.clone(), observation.sequence),
                observation.clone(),
            );
            self.feed_one_strategy(
                observation.destination,
                &EngineEvent::Signal(observation),
                now,
            );
        }
        Ok(())
    }

    /// Start following symbols a durable observation names that the engine
    /// did not know.
    ///
    /// Every table that maps a name to a `SymbolId` has to gain the symbol in
    /// the same order, because the id is an index assigned by position. Four
    /// of them exist — the engine's own, the public feed's, the venue
    /// gateway's, and the private stream's — and if any two disagreed, an
    /// order meant for one symbol would be sent for another. So this is the
    /// only place that admits, it admits one name at a time, and it checks
    /// that all four agree before the symbol is usable. A disagreement drops
    /// the symbol rather than trading it: the engine carries on with the names
    /// it already had, and says loudly which one it refused.
    async fn admit_wanted<M, O>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
    ) -> Result<(), EngineError>
    where
        M: engine_types::MarketFeed,
        O: engine_types::OrderFeed,
    {
        let wanted = std::mem::take(&mut self.wanted_symbols);
        let mut admitted = 0usize;
        for wanted in wanted {
            let name = wanted.name;
            let core_id = self.market.add_symbol(&name);
            let venue_id = self.venue.add_symbol_async(&name).await?;
            let mut feeds = Vec::new();
            for (_, feed) in &wanted.listeners {
                if !feeds.contains(feed) {
                    feeds.push(*feed);
                }
            }
            let feed_ids: Vec<_> = feeds
                .iter()
                .map(|feed| (*feed, market_feed.admit(&name, *feed)))
                .collect();
            if feed_ids.iter().any(|(_, id)| *id != Some(core_id)) || venue_id != Some(core_id) {
                tracing::error!(
                    symbol = %name,
                    ?core_id,
                    ?feed_ids,
                    ?venue_id,
                    "the parts of the engine disagree about this symbol's id; it will not be \
                     traded. Nothing else is affected — the ids already handed out do not move."
                );
                return Err(EngineError::State(format!(
                    "signal-required symbol {name} has inconsistent ids: core {:?}, feed {:?}, venue {:?}",
                    core_id, feed_ids, venue_id
                )));
            }
            order_feed.learn(&name, core_id);
            self.routing.size_to(self.market.table.len());
            for (strategy, feed) in wanted.listeners {
                self.routing.add(core_id, feed, strategy);
            }
            for feed in feeds {
                let subscription = Subscription {
                    symbol: name.clone(),
                    feed,
                };
                if !self.subscriptions.contains(&subscription) {
                    self.subscriptions.push(subscription);
                }
            }
            admitted += 1;
            tracing::info!(symbol = %name, id = core_id.0, "following a symbol a signal named");
        }
        if admitted == 0 {
            return Ok(());
        }
        // The table grew, so say what it is now. Ids are only appended, so
        // this is the earlier one plus the new names.
        let names = names_record(&self.names, &self.market);
        self.wal.append(&names)?;
        self.fills.learn(&names);
        // One venue read covers everything admitted this pass. Without a rule
        // there is no way to quantize, so the symbol is followed but nothing
        // can be sent for it — which is the same state as a symbol whose rule
        // was missing at boot.
        self.rules.resize(self.market.table.len(), None);
        match self.venue.instrument_rules().await {
            Ok(fetched) => {
                for (name, rule) in fetched {
                    if let Some(id) = self.market.table.get(&name) {
                        self.rules[id.0 as usize] = Some(rule);
                    }
                }
            }
            Err(e) => tracing::warn!(
                error = %e,
                "no instrument rules for the symbols just taken on; they cannot trade until \
                 the next attempt"
            ),
        }
        for observation in &self.pending_signal_deliveries {
            for subscription in &observation.subscriptions {
                let Some(symbol) = self.market.table.get(&subscription.symbol) else {
                    continue;
                };
                if self
                    .rules
                    .get(symbol.0 as usize)
                    .copied()
                    .flatten()
                    .is_none()
                {
                    return Err(EngineError::State(format!(
                        "signal-required symbol {} has no venue instrument rule",
                        subscription.symbol
                    )));
                }
            }
        }
        Ok(())
    }

    /// Pull every still-live opening order when reconciliation has latched new
    /// exposure off or private-stream continuity is unavailable. The durable
    /// reconciliation state is written before this queue reaches the venue.
    /// Foreign and reduce-only orders are left alone: cancelling another
    /// writer's order or a protective exit is not a safe guess.
    fn queue_halted_entry_cancels(&mut self) -> Result<(), EngineError> {
        if self.may_open && self.private_stream_ready {
            return Ok(());
        }
        let entries: Vec<(SymbolId, String)> = self
            .orders
            .in_flight()
            .into_iter()
            .filter(|order| !order.request.reduce_only)
            .map(|order| (order.request.symbol, order.request.client_order_id.clone()))
            .collect();
        for (symbol, client_order_id) in entries {
            self.enqueue_halt_cancel(symbol, client_order_id);
        }

        let now_ns = clock::now_ns();
        if let Some((client_order_id, _)) = self.halt_cancels.iter().find(|(id, state)| {
            matches!(
                state,
                HaltCancelState::AwaitingPrivate { deadline_ns }
                    if now_ns >= *deadline_ns && self.is_live_opening(id)
            )
        }) {
            return Err(EngineError::State(format!(
                "opening-halt cancellation for {client_order_id} was accepted but not confirmed by the private stream within {} ms; restarting for venue reconciliation",
                HALT_CANCEL_CONFIRM_NS / 1_000_000
            )));
        }
        Ok(())
    }

    fn enqueue_halt_cancel(&mut self, symbol: SymbolId, client_order_id: String) {
        if self.halt_cancels.contains_key(&client_order_id) {
            return;
        }
        self.halt_cancels
            .insert(client_order_id.clone(), HaltCancelState::Submitting);
        self.halt_cancel_queue.push_back((symbol, client_order_id));
    }

    async fn dispatch_halt_cancel_group(&mut self) -> Result<(), EngineError> {
        let mut requests = Vec::with_capacity(MAX_CANCELS_PER_BATCH);
        while requests.len() < MAX_CANCELS_PER_BATCH {
            let Some((symbol, client_order_id)) = self.halt_cancel_queue.pop_front() else {
                break;
            };
            let live = self.is_live_opening(&client_order_id);
            if live
                && matches!(
                    self.halt_cancels.get(&client_order_id),
                    Some(HaltCancelState::Submitting)
                )
            {
                requests.push((symbol, client_order_id));
            } else if !live {
                self.halt_cancels.remove(&client_order_id);
            }
        }
        self.process_cancels(requests).await.map(|_| ())
    }

    async fn on_tick(&mut self) -> Result<(), EngineError> {
        self.wal.flush()?;
        // Rotation is decided here, on the group-flush tick, and nowhere
        // else. The loop is one thread and one task, so this can never fall
        // between an intent's durability barrier and its send — that whole
        // stretch is inside `process_intent`, which has returned before the
        // tick can fire. The restatement is built from live state that the
        // same code as boot's replay maintains, no append can interleave
        // between building it and writing it, and `WalWriter::rotate`
        // carries the byte-level crash-ordering argument: the restatement
        // is durable in the new segment before that segment can be the one
        // boot picks, and a crash anywhere leaves boot on the old segment
        // with nothing invented and nothing lost.
        if self.rotate_after_bytes > 0 && self.wal.segment_size() >= self.rotate_after_bytes {
            let base = self.rotation_base(clock::wall_ms());
            if self.wal.rotate(&base)? {
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
        for mark in self.fills.due(now, &self.market) {
            self.wal.append(&mark.to_record())?;
        }
        self.checkpoint_history_if_due().await?;
        self.refresh_account_if_due(now).await?;
        self.queue_halted_entry_cancels()?;

        // Every resting entry gets one look. Read the clock again: the
        // account refresh above is a venue round trip, and the stamp from
        // before it is old by the time we get here.
        let now = clock::now_ns();
        if self.may_open && self.private_stream_ready {
            let Engine {
                working,
                market,
                rules,
                orders,
                pending,
                ..
            } = self;
            working.pass(now, market, rules, orders, pending);
        }
        // Through the ordinary queue, so the flood cap counts these too.
        self.drain(now).await
    }

    fn account_refresh_due(&self, now_ns: u64) -> bool {
        now_ns.saturating_sub(self.account.observed_ns) >= self.refresh_after_ns
    }

    async fn refresh_account_if_due(&mut self, now_ns: u64) -> Result<(), EngineError> {
        if !self.account_refresh_due(now_ns) {
            return Ok(());
        }
        match self.venue.account_view().await {
            Ok(view) => {
                self.adopt_view(view);
                self.enforce_position_stop_intent().await?;
            }
            // Keeping the old reading is not the same as trusting it: it
            // ages, and the risk kernel refuses on an old reading.
            Err(e) => tracing::warn!(error = %e, "could not refresh the account reading"),
        }
        Ok(())
    }

    async fn drain(&mut self, origin_ns: u64) -> Result<(), EngineError> {
        self.pull_unconfirmed_amends()?;
        let mut progress = self.drain_progress.take().unwrap_or(DrainProgress {
            origin_ns,
            handled: 0,
            adding_dropped: 0,
        });
        let mut placements = Vec::new();
        let mut cancellations = Vec::new();
        let mut hard_cap_hit = false;
        loop {
            if self.pending.is_empty() {
                self.load_ready_wake(&mut progress);
            }
            while let Some(action) = self.pending.pop_front() {
                let Some(action) = self.handle_durable_action(action)? else {
                    continue;
                };
                progress.handled += 1;
                // Past the cap, whatever adds risk is dropped but whatever sheds
                // it still flows: an exit or a cancel queued behind a flood must
                // get out, or its strategy is stranded holding a position — or an
                // order — it believes it is rid of. An amend counts as adding: it
                // can raise the size of a resting order. The hard cap bounds even
                // the de-risking ones against a runaway loop.
                if progress.handled > MAX_INTENTS_PER_WAKE && !action.is_risk_reducing() {
                    progress.adding_dropped += 1;
                    continue;
                }
                if progress.handled > MAX_INTENTS_PER_WAKE * 4 {
                    let dropped = self.pending.len() + 1;
                    self.pending.clear();
                    hard_cap_hit = true;
                    tracing::error!(
                        dropped,
                        "far too many actions in one wake; the rest were dropped"
                    );
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "dropped {dropped} actions, exits included: more than {} in one wake",
                            MAX_INTENTS_PER_WAKE * 4
                        ),
                    })?;
                    break;
                }

                let symbol = action
                    .symbol()
                    .expect("durable control actions are handled before symbol dispatch");
                if self.busy_symbols.contains_key(&symbol) {
                    self.defer_action(action, progress.origin_ns);
                    continue;
                }

                // Do not cross a placement boundary with the next verb
                // already consumed. If a real send happened, put this action
                // back at the front and let the run loop poll account-safety
                // inputs before resuming the same FIFO wake.
                if !matches!(&action, Action::Place(_)) && !placements.is_empty() {
                    let sent = self
                        .process_intents(std::mem::take(&mut placements), progress.origin_ns)
                        .await?;
                    if sent {
                        progress.handled -= 1;
                        self.pending.push_front(action);
                        return self.pause_drain(progress);
                    }
                }

                // Ordinary cancels share the same cooperative boundary. A
                // run of cancels accumulates into one native-sized request;
                // flush it before a different verb, then resume that verb on
                // the next turn.
                let accumulates_cancel = matches!(&action, Action::Cancel { .. });
                if !accumulates_cancel && !cancellations.is_empty() {
                    let sent = self
                        .process_cancels(std::mem::take(&mut cancellations))
                        .await?;
                    if sent {
                        progress.handled -= 1;
                        self.pending.push_front(action);
                        return self.pause_drain(progress);
                    }
                }
                match action {
                    Action::Place(intent) => {
                        placements.push(intent);
                        if placements.len() == MAX_ORDERS_PER_BATCH {
                            let sent = self
                                .process_intents(
                                    std::mem::take(&mut placements),
                                    progress.origin_ns,
                                )
                                .await?;
                            if sent && !self.pending.is_empty() {
                                return self.pause_drain(progress);
                            }
                        }
                    }
                    Action::Cancel {
                        symbol,
                        client_order_id,
                    } => {
                        cancellations.push((symbol, client_order_id));
                        if cancellations.len() == MAX_CANCELS_PER_BATCH {
                            let sent = self
                                .process_cancels(std::mem::take(&mut cancellations))
                                .await?;
                            if sent && !self.pending.is_empty() {
                                return self.pause_drain(progress);
                            }
                        }
                    }
                    Action::Amend {
                        symbol,
                        client_order_id,
                        spec,
                    } => {
                        let taken = self
                            .process_amend(symbol, &client_order_id, spec, progress.origin_ns)
                            .await?;
                        self.working
                            .amended(&client_order_id, spec.px, taken, clock::now_ns());
                        if !self.pending.is_empty() {
                            return self.pause_drain(progress);
                        }
                    }
                    Action::SetStop { symbol, trigger_px } => {
                        self.process_set_stop(symbol, trigger_px).await?;
                        if !self.pending.is_empty() {
                            return self.pause_drain(progress);
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
                    | Action::ConsumeRuntimeControl { .. } => {
                        unreachable!("strategy control state is journaled before venue actions")
                    }
                }
            }
            let sent = self
                .process_intents(std::mem::take(&mut placements), progress.origin_ns)
                .await?;
            if sent && !hard_cap_hit && !self.pending.is_empty() {
                return self.pause_drain(progress);
            }
            let cancelled = self
                .process_cancels(std::mem::take(&mut cancellations))
                .await?;
            if cancelled && !hard_cap_hit && !self.pending.is_empty() {
                return self.pause_drain(progress);
            }
            if !hard_cap_hit && self.pending.is_empty() && !self.ready_actions.is_empty() {
                self.load_ready_wake(&mut progress);
                continue;
            }
            if hard_cap_hit || self.pending.is_empty() {
                break;
            }
        }
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

    /// End one venue-mutation turn without ending its strategy wake. The
    /// batch has completed its record/send/reply sequence (and, for entries,
    /// its durability barrier); this only keeps the flood counters and
    /// latency origin while the run loop polls account-safety inputs.
    fn pause_drain(&mut self, progress: DrainProgress) -> Result<(), EngineError> {
        self.drain_progress = Some(progress);
        Ok(())
    }

    fn defer_action(&mut self, action: Action, origin_ns: u64) {
        let symbol = action
            .symbol()
            .expect("only symbol-scoped venue actions can be deferred");
        let queue = self.deferred_actions.entry(symbol).or_default();
        match &action {
            Action::Amend {
                client_order_id, ..
            } => {
                if queue.iter().any(|(queued, _)| {
                    matches!(queued, Action::Cancel { client_order_id: queued_id, .. } if queued_id == client_order_id)
                }) {
                    return;
                }
                queue.retain(|(queued, _)| {
                    !matches!(queued, Action::Amend { client_order_id: queued_id, .. } if queued_id == client_order_id)
                });
            }
            Action::Cancel {
                client_order_id, ..
            } => {
                if queue.iter().any(|(queued, _)| {
                    matches!(queued, Action::Cancel { client_order_id: queued_id, .. } if queued_id == client_order_id)
                }) {
                    return;
                }
                queue.retain(|(queued, _)| {
                    !matches!(queued, Action::Amend { client_order_id: queued_id, .. } if queued_id == client_order_id)
                });
            }
            Action::SetStop { .. } => {
                queue.retain(|(queued, _)| !matches!(queued, Action::SetStop { .. }));
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
                        queued,
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
            | Action::ConsumeRuntimeControl { .. } => {}
        }
        queue.push_back((action, origin_ns));
    }

    fn load_ready_wake(&mut self, progress: &mut DrainProgress) {
        let Some((action, origin_ns)) = self.ready_actions.pop_front() else {
            return;
        };
        *progress = DrainProgress {
            origin_ns,
            handled: 0,
            adding_dropped: 0,
        };
        self.pending.push_back(action);
        while self
            .ready_actions
            .front()
            .is_some_and(|(_, queued_origin)| *queued_origin == origin_ns)
        {
            let (action, _) = self.ready_actions.pop_front().expect("front checked above");
            self.pending.push_back(action);
        }
    }

    fn mark_symbols_busy(&mut self, symbols: impl IntoIterator<Item = SymbolId>) {
        for symbol in symbols {
            *self.busy_symbols.entry(symbol).or_default() += 1;
        }
    }

    fn release_symbols(&mut self, symbols: impl IntoIterator<Item = SymbolId>) {
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
