use super::*;
use crate::effects::EffectKey;
use crate::strategy_process::host::{CallbackCompletion, CallbackWrite};
use engine_types::strategy_process::{CallbackPreparation, StrategyProcessState};

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn service_strategy_callbacks(&mut self) -> Result<(), EngineError> {
        if !self.host.callbacks.isolated() {
            return Ok(());
        }
        self.ensure_callback_reader(&[])?;
        self.service_order_callback_sources()?;
        let pending_boot: Vec<_> = self.host.callbacks.pending_boot.iter().copied().collect();
        for strategy in pending_boot {
            let _ = self.host.callbacks.enqueue(strategy, &EngineEvent::Boot);
        }
        self.retry_callback_inputs();
        if self.host.callbacks.write.is_some() {
            match self.host.callbacks.durable.try_recv() {
                Ok(result) => self.on_callback_durable(Some(result))?,
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => return Ok(()),
                Err(_) => {
                    return Err(EngineError::TaskStopped {
                        task: EngineTask::CallbackDurability,
                        detail: "",
                    })
                }
            }
        }
        match self.host.callbacks.pages.completed.try_recv() {
            Ok(completion) => self.on_callback_page(completion)?,
            Err(tokio::sync::mpsc::error::TryRecvError::Empty) => (),
            Err(_) => return Err(EngineError::State("callback page channel closed".into())),
        }
        self.host.callbacks.start_page_load();
        if !self.host.callbacks.unwritten.is_empty() {
            let mut cursors = Vec::new();
            for input in &self.host.callbacks.unwritten {
                let sequence = self.wal.append(&WalRecord::StrategyCallbackQueued {
                    input: input.clone(),
                })?;
                let origin = self
                    .host
                    .callbacks
                    .order_news
                    .origin(sequence)
                    .map_err(EngineError::State)?;
                cursors.push(engine_types::strategy_process::CallbackWalCursor {
                    segment: origin.segment,
                    sequence,
                    offset: 0,
                });
            }
            let barrier = self.wal.barrier_begin()?;
            let inputs = self
                .host
                .callbacks
                .unwritten
                .drain(..)
                .zip(cursors)
                .collect();
            self.host
                .callbacks
                .begin_write(CallbackWrite::Accept(inputs), barrier);
            return Ok(());
        }
        let mut strategies: Vec<_> = self
            .host
            .callbacks
            .state
            .inputs
            .values()
            .map(|input| input.strategy)
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        if let Some(previous) = self.host.callbacks.last_launched {
            let next = strategies.partition_point(|strategy| *strategy <= previous);
            strategies.rotate_left(next);
        }
        for strategy in strategies {
            if !self.host.callbacks.is_active(strategy)
                || self.host.effects.earliest(strategy).is_some()
                || !self.host.callbacks.retry_ready(strategy)
            {
                continue;
            }
            let input = self
                .host
                .callbacks
                .state
                .inputs
                .values()
                .find(|input| input.strategy == strategy)
                .cloned()
                .expect("queued strategy");
            if input.snapshot().is_none() {
                let snapshot = match self.host.snapshot(&self.books, strategy, clock::now_ns()) {
                    Ok(snapshot) => snapshot,
                    Err(error) => {
                        self.fail_strategy_callback(strategy, error)?;
                        continue;
                    }
                };
                let mut prepared = input;
                prepared.preparation = CallbackPreparation::Prepared { snapshot };
                if let Err(error) = self.host.callbacks.state.can_prepare(&prepared) {
                    self.fail_strategy_callback(strategy, error)?;
                    continue;
                }
                let sequence = self.wal.append(&WalRecord::StrategyCallbackPrepared {
                    input: prepared.clone(),
                })?;
                let barrier = self.wal.barrier_begin()?;
                self.host.callbacks.begin_write(
                    CallbackWrite::Prepare((
                        prepared,
                        engine_types::strategy_process::CallbackWalCursor {
                            segment: self
                                .host
                                .callbacks
                                .order_news
                                .origin(sequence)
                                .map_err(EngineError::State)?
                                .segment,
                            sequence,
                            offset: 0,
                        },
                    )),
                    barrier,
                );
                return Ok(());
            }
            if let Err(error) = self.host.callbacks.launch(strategy) {
                self.fail_strategy_callback(strategy, error)?;
            }
        }
        Ok(())
    }

    pub(super) fn on_callback_page(
        &mut self,
        completion: crate::strategy_process::paging::PageCompletion,
    ) -> Result<(), EngineError> {
        let reserved = self.host.callbacks.unwritten_size();
        if let Some((strategy, error)) = self
            .host
            .callbacks
            .pages
            .returned(completion, &mut self.host.callbacks.state, reserved)
            .map_err(EngineError::State)?
        {
            self.host.callbacks.faults.insert(strategy, error);
        }
        Ok(())
    }

    fn retry_callback_inputs(&mut self) {
        use crate::strategy_process::retry::DurableRetry;
        let retries: Vec<_> = self
            .host
            .callbacks
            .retry_inputs
            .durable
            .iter()
            .cloned()
            .collect();
        let mut blocked = std::collections::BTreeSet::new();
        for key in retries {
            let strategy = key.owner();
            if blocked.contains(&strategy) {
                continue;
            }
            let event = match &key {
                DurableRetry::StrategyEvent {
                    source, event_id, ..
                } => self
                    .host
                    .events
                    .get(&(*source, event_id.clone()))
                    .cloned()
                    .map(EngineEvent::StrategyEvent),
                DurableRetry::Control { request_id, .. } => self
                    .runtime_control_requests
                    .iter()
                    .find(|request| {
                        request.strategy == strategy && &request.request_id == request_id
                    })
                    .map(|request| match request.command {
                        engine_types::RuntimeControlCommand::SetEntriesEnabled {
                            entries_enabled,
                        } => EngineEvent::EntryPermission {
                            request_id: request_id.clone(),
                            entries_enabled,
                        },
                        engine_types::RuntimeControlCommand::FlattenDirectional => {
                            EngineEvent::FlattenDirectional {
                                request_id: request_id.clone(),
                            }
                        }
                    }),
            };
            if let Some(event) = event {
                if self.host.callbacks.enqueue(strategy, &event).is_err() {
                    blocked.insert(strategy);
                }
            } else {
                self.host
                    .callbacks
                    .retry_inputs
                    .durable
                    .retain(|pending| pending != &key);
            }
        }
        let market: Vec<_> = self
            .host
            .callbacks
            .retry_inputs
            .market
            .iter()
            .copied()
            .collect();
        for (strategy, slot) in market {
            let event = slot.latest(
                &self.books.market,
                self.host.callbacks.retry_inputs.reset_ns,
            );
            let _ = self
                .host
                .callbacks
                .enqueue(strategy, &EngineEvent::Market(event));
        }
    }

    pub(super) fn ensure_callback_reader(
        &mut self,
        records: &[WalRecord],
    ) -> Result<(), EngineError> {
        if self.host.callbacks.isolated() && !self.host.callbacks.order_news.has_reader() {
            let reader = self.wal.callback_reader()?.ok_or_else(|| {
                EngineError::State(
                    "isolated callbacks require a durable order-source reader".into(),
                )
            })?;
            self.host
                .callbacks
                .order_news
                .attach(reader, records, self.host.strategies.len())
                .map_err(EngineError::State)?;
        }
        Ok(())
    }

    pub(super) fn deliver_callback_source(
        &mut self,
        strategy: StrategyId,
        event: EngineEvent,
        placement: Option<String>,
    ) -> Result<(), EngineError> {
        if !self.host.callbacks.isolated() {
            self.host
                .feed(&self.books, strategy, &event, clock::now_ns());
            return Ok(());
        }
        if placement
            .as_ref()
            .is_some_and(|id| self.host.callbacks.refused_orders.contains(id))
        {
            return Ok(());
        }
        self.ensure_callback_reader(&[])?;
        let event = engine_types::strategy_process::CallbackEvent::from(&event);
        let ready = !self.host.callbacks.order_news.unread_for(strategy);
        let sequence = self.wal.append(&WalRecord::StrategyCallbackSource {
            placement: placement.clone(),
            strategy,
            event: event.clone(),
        })?;
        if let Some(id) = placement {
            if self.host.effects.transitions.values().any(|transition| {
                transition
                    .order_ids
                    .iter()
                    .flatten()
                    .any(|known| known == &id)
            }) {
                self.host.callbacks.refused_orders.insert(id);
            }
        }
        self.host
            .callbacks
            .order_news
            .record(sequence, &[strategy])
            .map_err(EngineError::State)?;
        if ready {
            let origin = self
                .host
                .callbacks
                .order_news
                .origin(sequence)
                .map_err(EngineError::State)?;
            if let Err(error) = self.host.callbacks.enqueue_source(strategy, event, origin) {
                self.host.callbacks.faults.insert(strategy, error);
            }
        }
        Ok(())
    }

    fn service_order_callback_sources(&mut self) -> Result<(), EngineError> {
        match self.host.callbacks.order_news.completed.try_recv() {
            Ok(completion) => self.on_order_callback_source(completion)?,
            Err(tokio::sync::mpsc::error::TryRecvError::Empty) => (),
            Err(_) => {
                return Err(EngineError::State(
                    "order callback read channel closed".into(),
                ))
            }
        }
        if self.host.callbacks.order_news.unread() && !self.host.callbacks.order_news.pending() {
            self.wal.flush()?;
            let allowed: std::collections::BTreeSet<_> = (0..self.host.strategies.len())
                .map(|index| StrategyId(index as u16))
                .filter(|strategy| {
                    self.host.callbacks.is_active(*strategy)
                        && (!self.host.callbacks.pages.enabled()
                            || !self.host.callbacks.pending_for(*strategy))
                })
                .collect();
            self.host
                .callbacks
                .order_news
                .start_read_for(|strategy| allowed.contains(&strategy));
        }
        Ok(())
    }

    pub(super) fn on_order_callback_source(
        &mut self,
        completion: crate::strategy_process::order_news::ReadCompletion,
    ) -> Result<(), EngineError> {
        let (strategy, cursor, record) = self
            .host
            .callbacks
            .order_news
            .returned(completion)
            .map_err(EngineError::State)?;
        if let Some((owners, event)) = record.source {
            if owners.contains(&strategy)
                && self.host.callbacks.order_news.source_due(
                    strategy,
                    engine_types::strategy_process::CallbackOrderOrigin {
                        segment: cursor.segment,
                        sequence: cursor.sequence,
                    },
                )
            {
                let event = match event {
                    engine_types::strategy_process::CallbackEvent::Order { update } => {
                        engine_types::strategy_process::CallbackEvent::Order {
                            update: crate::strategy_process::order_news::OrderNews::slice(
                                &update, strategy,
                            )
                            .map_err(EngineError::State)?,
                        }
                    }
                    event => event,
                };
                let origin = engine_types::strategy_process::CallbackOrderOrigin {
                    segment: cursor.segment,
                    sequence: cursor.sequence,
                };
                if let Err(error) = self.host.callbacks.enqueue_source(strategy, event, origin) {
                    self.host.callbacks.faults.insert(strategy, error);
                    self.host.callbacks.order_news.refused(strategy);
                    return Ok(());
                }
            }
        }
        self.host
            .callbacks
            .order_news
            .advance(strategy, record.next);
        Ok(())
    }

    fn fail_strategy_callback(
        &mut self,
        strategy: StrategyId,
        error: String,
    ) -> Result<(), EngineError> {
        let changed = self.host.callbacks.faults.get(&strategy) != Some(&error);
        if changed {
            tracing::error!(
                strategy = strategy.0,
                error,
                "strategy callback aborted; input retained for process restart"
            );
            self.wal.append(&WalRecord::Note {
                source: "strategy_process".into(),
                text: format!(
                    "strategy {} callback aborted; input retained: {error}",
                    strategy.0
                ),
            })?;
        }
        self.host.callbacks.failed(strategy, error);
        Ok(())
    }

    pub(super) fn on_strategy_callback(
        &mut self,
        completion: Option<CallbackCompletion>,
    ) -> Result<(), EngineError> {
        let completion = completion.ok_or(EngineError::TaskStopped {
            task: EngineTask::StrategyHost,
            detail: "",
        })?;
        self.host
            .callbacks
            .completed(completion.strategy, completion.input_id)
            .map_err(EngineError::State)?;
        let (worker, proposal) = match completion.result {
            Ok(result) => result,
            Err(error) => return self.fail_strategy_callback(completion.strategy, error),
        };
        if let Err(error) = engine_strategies::runtime::restore(&proposal.state) {
            return self.fail_strategy_callback(completion.strategy, error);
        }
        let input = self
            .host
            .callbacks
            .state
            .inputs
            .get(&completion.input_id)
            .ok_or_else(|| EngineError::State("strategy completion lost its input".into()))?;
        let now_ns = input
            .snapshot()
            .ok_or_else(|| {
                EngineError::State("callback completed without a prepared invocation".into())
            })?
            .now_ns;
        let actions: Vec<_> = proposal
            .actions
            .into_iter()
            .map(|action| crate::ctx::bind_action(completion.strategy, now_ns, action))
            .collect();
        if let Err(error) = self.validate_callback_actions(completion.strategy, &actions) {
            return self.fail_strategy_callback(completion.strategy, error);
        }
        let mut timers = self
            .host
            .callbacks
            .state
            .committed
            .get(&completion.strategy)
            .map(|state| state.timers.clone())
            .unwrap_or_default();
        for timer in proposal.timers {
            timers.retain(|previous| previous.id != timer.id);
            timers.push(timer);
        }
        timers.sort_by_key(|timer| timer.id.0);
        let process = StrategyProcessState {
            strategy: completion.strategy,
            last_callback_id: completion.input_id,
            runtime: proposal.state,
            timers,
            retained_signal_subscriptions: proposal.retained_signal_subscriptions,
        };
        if let Err(error) = self
            .host
            .callbacks
            .can_commit(completion.input_id, &process)
        {
            return self.fail_strategy_callback(completion.strategy, error);
        }
        let transition = if actions.is_empty() {
            None
        } else {
            let id = self.host.effects.next_id;
            self.host.effects.next_id = id
                .checked_add(1)
                .ok_or_else(|| EngineError::State("strategy transition id exhausted".into()))?;
            let transition = engine_types::StrategyTransitionState {
                origin: engine_types::wal::StrategyTransitionOrigin::Process {
                    callback_id: completion.input_id,
                },
                id,
                strategy: completion.strategy,
                order_ids: vec![None; actions.len()],
                effects: actions,
                completed: Vec::new(),
            };
            match self.prepare_effect_transition(transition) {
                Ok(transition) => Some(transition),
                Err(error) => {
                    return self.fail_strategy_callback(completion.strategy, error.to_string())
                }
            }
        };
        self.wal
            .append(&WalRecord::StrategyProcessTransitionQueued {
                input_id: completion.input_id,
                transition: transition.clone(),
                process: process.clone(),
            })?;
        let barrier = self.wal.barrier_begin()?;
        self.host.callbacks.begin_write(
            CallbackWrite::Commit {
                input_id: completion.input_id,
                transition,
                process,
                worker,
            },
            barrier,
        );
        Ok(())
    }

    fn validate_callback_actions(
        &self,
        strategy: StrategyId,
        actions: &[Action],
    ) -> Result<(), String> {
        let owner = self
            .host
            .strategies
            .get(strategy.idx())
            .ok_or("callback owner is absent")?;
        let mut published = std::collections::BTreeMap::new();
        for action in actions {
            match action {
                Action::SetStrategyCheckpoint { checkpoint, .. }
                | Action::SetStrategyGlobalCheckpoint { checkpoint, .. } => {
                    validate_strategy_checkpoint(owner.as_ref(), checkpoint)?
                }
                Action::PublishStrategyEvent { event } => {
                    self.validate_strategy_event(event)
                        .map_err(|error| error.to_string())?;
                    let key = (event.source, event.event_id.clone());
                    if published
                        .get(&key)
                        .or_else(|| self.host.events.get(&key))
                        .is_some_and(|known| known != event)
                    {
                        return Err(
                            "callback reused a strategy event id with different bytes".into()
                        );
                    }
                    published.insert(key, event.clone());
                }
                Action::ConsumeStrategyEvent {
                    source,
                    destination,
                    event_id,
                } => {
                    if published
                        .get(&(*source, event_id.clone()))
                        .or_else(|| self.host.events.get(&(*source, event_id.clone())))
                        .is_some_and(|event| event.destination != *destination)
                    {
                        return Err("callback consumed another strategy's event".into());
                    }
                }
                Action::ConsumeSignalObservation {
                    strategy,
                    source,
                    sequence,
                    observation_id,
                }
                | Action::RejectSignalObservation {
                    strategy,
                    source,
                    sequence,
                    observation_id,
                    ..
                } => {
                    self.signals
                        .consumable(*strategy, source, *sequence, observation_id)?;
                }
                Action::ConsumeRuntimeControl {
                    strategy,
                    request_id,
                } => {
                    if !self
                        .runtime_control_consumed
                        .contains(&(*strategy, request_id.clone()))
                        && !self.runtime_control_requests.iter().any(|request| {
                            request.strategy == *strategy
                                && request.request_id == *request_id
                                && matches!(
                                    request.command,
                                    engine_types::RuntimeControlCommand::FlattenDirectional
                                )
                        })
                    {
                        return Err(
                            "callback consumed an unknown or non-replayable runtime control".into(),
                        );
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }

    pub(super) fn on_callback_durable(
        &mut self,
        result: Option<Result<(), WalError>>,
    ) -> Result<(), EngineError> {
        result.ok_or(EngineError::TaskStopped {
            task: EngineTask::CallbackDurability,
            detail: "",
        })??;
        let write =
            self.host.callbacks.write.take().ok_or_else(|| {
                EngineError::State("callback durability result has no owner".into())
            })?;
        match write {
            CallbackWrite::Accept(inputs) => {
                for (input, cursor) in inputs {
                    self.host
                        .callbacks
                        .accepted(input, cursor)
                        .map_err(EngineError::State)?;
                }
            }
            CallbackWrite::Prepare((input, cursor)) => {
                if self.host.callbacks.pages.enabled() {
                    self.host
                        .callbacks
                        .pages
                        .prepared(&input, cursor)
                        .map_err(EngineError::State)?;
                }
                self.host
                    .callbacks
                    .state
                    .prepared(input)
                    .map_err(EngineError::State)?;
            }
            CallbackWrite::Commit {
                input_id,
                transition,
                process,
                worker,
            } => {
                self.host
                    .callbacks
                    .state
                    .commit(input_id, process.clone())
                    .map_err(EngineError::State)?;
                self.host.callbacks.pages.remove(input_id);
                self.host.strategies[process.strategy.idx()] =
                    engine_strategies::runtime::restore(&process.runtime)
                        .map_err(EngineError::State)?;
                self.host.timers.restore(
                    process.strategy,
                    &process.timers,
                    clock::now_ns(),
                    clock::wall_ms(),
                );
                if let Some(transition) = transition {
                    for id in transition.order_ids.iter().flatten() {
                        self.books.registry.own(id, transition.strategy);
                    }
                    self.host.effects.journaled.insert(transition.id);
                    for (index, action) in transition.effects.iter().enumerate() {
                        self.host.pending.push_back(PendingAction {
                            caller: Some(transition.strategy),
                            action: action.clone(),
                            effect: Some(EffectKey {
                                transition_id: transition.id,
                                index,
                            }),
                            callback_id: Some(transition.id),
                        });
                    }
                    self.host
                        .effects
                        .transitions
                        .insert(transition.id, transition);
                }
                self.host.callbacks.succeeded(process.strategy, worker);
            }
        }
        Ok(())
    }

    pub(super) async fn restore_strategy_callbacks(&mut self) -> Result<(), EngineError> {
        if !self.host.callbacks.isolated() {
            return Ok(());
        }
        for (strategy, process) in &self.host.callbacks.state.committed {
            if !self.host.callbacks.is_active(*strategy) {
                continue;
            }
            self.host.strategies[strategy.idx()] =
                engine_strategies::runtime::restore(&process.runtime).map_err(EngineError::Boot)?;
            self.host.timers.restore(
                *strategy,
                &process.timers,
                clock::now_ns(),
                clock::wall_ms(),
            );
        }
        loop {
            self.service_strategy_callbacks()?;
            self.drain(clock::now_ns()).await?;
            if self.host.callbacks.pages.loading() && self.host.callbacks.write.is_none() {
                let completion = tokio::time::timeout(
                    MUTATION_DRAIN_TIMEOUT,
                    self.host.callbacks.pages.completed.recv(),
                )
                .await
                .map_err(|_| EngineError::Boot("timed out loading durable callback input".into()))?
                .ok_or_else(|| EngineError::Boot("callback page reader stopped".into()))?;
                self.on_callback_page(completion)?;
                continue;
            }
            if self.host.callbacks.order_news.pending() {
                let completion = tokio::time::timeout(
                    MUTATION_DRAIN_TIMEOUT,
                    self.host.callbacks.order_news.completed.recv(),
                )
                .await
                .map_err(|_| {
                    EngineError::Boot("timed out reading durable order callback source".into())
                })?
                .ok_or_else(|| EngineError::Boot("order callback reader stopped".into()))?;
                self.on_order_callback_source(completion)?;
                continue;
            }
            if self.host.callbacks.write.is_some() {
                let result = self.host.callbacks.durable.recv().await;
                self.on_callback_durable(result)?;
                continue;
            }
            if self.dispatches.write.is_some() {
                let result = self.dispatches.durable.recv().await;
                self.on_order_dispatch_durable(result).await?;
                continue;
            }
            if !self.pending_mutations.is_empty() {
                let completion =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.venue_completions.recv())
                        .await
                        .map_err(|_| {
                            EngineError::TimedOut(
                                "callback effect mutation during boot restore".into(),
                            )
                        })?
                        .ok_or(EngineError::TaskStopped {
                            task: EngineTask::Venue,
                            detail: "restoring callback effects",
                        })?;
                self.take_venue_completion(completion).await?;
                continue;
            }
            if self.host.callbacks.running() {
                let completion = self.host.callbacks.completions.recv().await;
                self.on_strategy_callback(completion)?;
                continue;
            }
            if self.host.pending.is_empty() && self.ready_actions.is_empty() {
                break;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy_process::host::{CallbackExecution, CallbackHost};
    use crate::strategy_process::{CallbackProposal, StrategyProcess};
    use engine_types::strategy_process::StrategyTimerState;
    use engine_types::TimerId;

    async fn prepared() -> (
        Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>,
        std::sync::Arc<std::sync::Mutex<Vec<WalRecord>>>,
        u64,
    ) {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let plug = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, records) = crate::tests::callback_test_fixture(vec![plug]).await;
        engine.host.timers = Timers::default();
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            &[],
        )
        .unwrap();
        assert!(engine.feed_one_strategy(StrategyId(0), &EngineEvent::Boot, clock::now_ns()));
        engine.service_strategy_callbacks().unwrap();
        engine.service_strategy_callbacks().unwrap();
        let settled = engine.host.callbacks.durable.try_recv().unwrap();
        engine.on_callback_durable(Some(settled)).unwrap();
        let input_id = *engine.host.callbacks.state.inputs.keys().next().unwrap();
        (engine, records, input_id)
    }

    fn proposal_completion(
        engine: &mut Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>,
        input_id: u64,
    ) -> CallbackCompletion {
        engine
            .host
            .callbacks
            .expect_test_completion(StrategyId(0), input_id);
        let mut command = std::process::Command::new("/bin/sleep");
        command.arg("60");
        CallbackCompletion {
            strategy: StrategyId(0),
            input_id,
            result: Ok((
                StrategyProcess::spawn_command(command).unwrap(),
                CallbackProposal {
                    callback_id: input_id,
                    actions: vec![Action::Cancel {
                        symbol: SymbolId(0),
                        client_order_id: "owned-cancel".into(),
                    }],
                    timers: vec![StrategyTimerState {
                        id: TimerId(7),
                        deadline_ns: clock::now_ns() + 10_000_000_000,
                        deadline_wall_ms: clock::wall_ms() + 10_000,
                    }],
                    state: engine.host.strategies[0].runtime_state().unwrap().unwrap(),
                    retained_signal_subscriptions: None,
                },
            )),
        }
    }

    async fn full_inbox() -> (
        Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>,
        std::sync::Arc<std::sync::Mutex<Vec<WalRecord>>>,
    ) {
        use crate::strategy_process::state::CallbackState;
        use engine_types::strategy_process::{
            CallbackEvent, StrategyCallbackInput, MAX_PROCESS_PROPOSAL_BYTES,
        };
        let (mut engine, records, input_id) = prepared().await;
        let snapshot = engine.host.callbacks.state.inputs[&input_id]
            .snapshot()
            .unwrap()
            .clone();
        engine.host.callbacks.state = CallbackState::default();
        let mut filler = StrategyCallbackInput {
            order_origin: None,
            callback_id: 100,
            strategy: StrategyId(0),
            preparation: CallbackPreparation::Prepared { snapshot },
            event: CallbackEvent::IntentRefused {
                symbol: SymbolId(0),
                reduce_only: false,
                reason: String::new(),
            },
        };
        let overhead = CallbackState::size(&filler).unwrap();
        let CallbackEvent::IntentRefused { reason, .. } = &mut filler.event else {
            unreachable!()
        };
        *reason = "x".repeat(MAX_PROCESS_PROPOSAL_BYTES - overhead - 8);
        engine.host.callbacks.state.accept(filler).unwrap();
        (engine, records)
    }

    #[tokio::test(start_paused = true)]
    async fn a_full_callback_inbox_retains_private_fill_and_boot_until_delivery() {
        use engine_types::strategy_process::CallbackEvent;
        let (mut engine, records) = full_inbox().await;
        let sent = WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "callback-owned".into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.01,
                kind: OrderKind::Market,
                stop: Some(StopSpec {
                    trigger_px: 29_000.0,
                }),
                reduce_only: false,
                close_position: false,
                sleeve_effect: None,
                exact_terms: None,
            },
            wire_ns: 1,
            arrival_mid: 30_000.0,
        };
        engine.books.orders.apply(&sent);
        engine.books.registry.own("callback-owned", StrategyId(0));
        engine.wal.append(&sent).unwrap();
        let fill = OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "callback-private-fill".into(),
            client_order_id: "callback-owned".into(),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: Some(0.1),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: clock::now_ns(),
        };
        engine.take_update(fill).await.unwrap();
        assert!(engine.host.callbacks.unwritten.is_empty());
        assert!(
            engine.host.callbacks.order_news.unread_for(StrategyId(0)),
            "private news was discarded when the callback inbox filled"
        );
        assert!(!engine.feed_one_strategy(StrategyId(0), &EngineEvent::Boot, clock::now_ns()));
        assert!(engine.host.callbacks.pending_boot.contains(&StrategyId(0)));
        assert_eq!(engine.host.callbacks.state.inputs.len(), 1);
        let parent = records
            .lock()
            .unwrap()
            .iter()
            .find_map(|record| match record {
                WalRecord::OrderUpdate {
                    callbacks: Some(owners),
                    update: update @ OrderUpdate::Fill { .. },
                } => Some((owners.clone(), update.clone())),
                _ => None,
            })
            .expect("one durable parent must carry callback ownership");
        assert_eq!(parent.0, [StrategyId(0)]);
        let OrderUpdate::Fill { allocation, .. } = &parent.1 else {
            unreachable!()
        };
        assert!(
            allocation.is_some(),
            "terminal sender eviction must not erase callback allocation"
        );
        engine
            .host
            .callbacks
            .state
            .commit(
                100,
                StrategyProcessState {
                    strategy: StrategyId(0),
                    last_callback_id: 100,
                    runtime: engine.host.strategies[0].runtime_state().unwrap().unwrap(),
                    timers: Vec::new(),
                    retained_signal_subscriptions: None,
                },
            )
            .unwrap();
        for _ in 0..100 {
            engine.service_order_callback_sources().unwrap();
            if !engine.host.callbacks.order_news.unread() {
                break;
            }
            if engine.host.callbacks.order_news.pending() {
                let completion = engine
                    .host
                    .callbacks
                    .order_news
                    .completed
                    .recv()
                    .await
                    .unwrap();
                engine.on_order_callback_source(completion).unwrap();
            }
        }
        assert!(
            !engine.host.callbacks.order_news.unread(),
            "private news never retried after capacity returned"
        );
        engine.service_strategy_callbacks().unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        let inputs: Vec<_> = engine
            .host
            .callbacks
            .state
            .inputs
            .values()
            .cloned()
            .collect();
        assert_eq!(inputs.len(), 2);
        assert!(matches!(inputs[0].event, CallbackEvent::Order { .. }));
        assert!(inputs[0].order_origin.is_some());
        assert!(matches!(inputs[1].event, CallbackEvent::Boot));
        assert!(!engine.host.callbacks.pending_boot.contains(&StrategyId(0)));
        engine.service_strategy_callbacks().unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        let facts = engine.host.callbacks.state.inputs[&inputs[0].callback_id]
            .snapshot()
            .unwrap()
            .symbols[0]
            .facts
            .as_ref()
            .unwrap()
            .clone();
        assert_eq!(
            facts.attributed_signed_qty, 0.01,
            "order callback snapshot preceded committed fill accounting"
        );
        assert_eq!(facts.open_order_count, 0);
        assert_eq!(
            records
                .lock()
                .unwrap()
                .iter()
                .filter(|record| matches!(
                    record,
                    WalRecord::OrderUpdate {
                        callbacks: Some(_),
                        update: OrderUpdate::Fill { .. }
                    }
                ))
                .count(),
            1
        );
        let replay = records.lock().unwrap().clone();
        let mut restored = crate::strategy_process::order_news::OrderNews::default();
        restored
            .attach(engine.wal.callback_reader().unwrap().unwrap(), &replay, 1)
            .unwrap();
        assert!(
            !restored.unread(),
            "accepted callback source redelivered on restart"
        );
        engine.host.callbacks.stop().await;
    }

    #[tokio::test(start_paused = true)]
    async fn a_refusal_retries_from_its_durable_source_before_the_latest_market_wake() {
        use engine_types::strategy_process::CallbackEvent;
        let (mut engine, records) = full_inbox().await;
        let intent = Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: 90.0 }),
            reduce_only: false,
            tag: "refusal".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        engine.tell_refused(&intent, "test refusal", None).unwrap();
        assert!(
            engine.host.callbacks.order_news.unread(),
            "full inbox lost an internal refusal"
        );
        for bid in [99.0, 199.0] {
            let event = MarketEvent::Quote {
                symbol: SymbolId(0),
                quote: engine_types::Quote {
                    bid_px: bid,
                    ask_px: bid + 2.0,
                    recv_ns: clock::now_ns(),
                    ..Default::default()
                },
            };
            engine.books.market.apply(&event);
            assert!(!engine.feed_one_strategy(
                StrategyId(0),
                &EngineEvent::Market(event),
                clock::now_ns()
            ));
        }
        assert_eq!(
            engine.host.callbacks.retry_inputs.market.len(),
            1,
            "overload retained each historical quote instead of one latest wake"
        );
        engine
            .host
            .callbacks
            .state
            .commit(
                100,
                StrategyProcessState {
                    strategy: StrategyId(0),
                    last_callback_id: 100,
                    runtime: engine.host.strategies[0].runtime_state().unwrap().unwrap(),
                    timers: Vec::new(),
                    retained_signal_subscriptions: None,
                },
            )
            .unwrap();
        for _ in 0..100 {
            engine.service_order_callback_sources().unwrap();
            if !engine.host.callbacks.order_news.unread() {
                break;
            }
            if engine.host.callbacks.order_news.pending() {
                let completion = engine
                    .host
                    .callbacks
                    .order_news
                    .completed
                    .recv()
                    .await
                    .unwrap();
                engine.on_order_callback_source(completion).unwrap();
            }
        }
        engine.retry_callback_inputs();
        assert!(engine.host.callbacks.retry_inputs.market.is_empty());
        let inputs: Vec<_> = engine.host.callbacks.unwritten.iter().collect();
        assert_eq!(inputs.len(), 2);
        assert!(
            matches!(&inputs[0].event, CallbackEvent::IntentRefused { reason, .. } if reason == "test refusal")
        );
        assert!(
            matches!(inputs[1].event, CallbackEvent::Quote { quote, .. } if quote.bid_px == 199.0)
        );
        assert!(inputs[0].order_origin.is_some());
        let rows = records.lock().unwrap().clone();
        let mut restored = crate::strategy_process::order_news::OrderNews::default();
        restored
            .attach(engine.wal.callback_reader().unwrap().unwrap(), &rows, 1)
            .unwrap();
        assert!(
            restored.unread(),
            "restart before callback admission lost the durable refusal"
        );
        engine.host.callbacks.stop().await;
    }

    #[tokio::test(start_paused = true)]
    async fn a_refusal_source_is_a_replayable_disposition_before_effect_completion() {
        use engine_types::strategy_process::CallbackEvent;
        let (mut engine, records, input_id) = prepared().await;
        let mut completion = proposal_completion(&mut engine, input_id);
        completion.result.as_mut().unwrap().1.actions = vec![Action::Place(Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: 90.0 }),
            reduce_only: false,
            tag: "denied-effect".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        })];
        engine.on_strategy_callback(Some(completion)).unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        let transition = engine
            .host
            .effects
            .transitions
            .values()
            .next()
            .unwrap()
            .clone();
        let Action::Place(intent) = &transition.effects[0] else {
            unreachable!()
        };
        let id = transition.order_ids[0].as_deref().unwrap();
        engine
            .tell_refused(intent, "test denied effect", Some(id))
            .unwrap();
        let replay = records.lock().unwrap().clone();
        engine.host.callbacks.stop().await;
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            &replay,
        )
        .unwrap();
        assert!(engine.host.callbacks.refused_orders.contains(id));
        engine.host.effects = crate::effects::Effects::replay(&replay, 1).unwrap();
        engine.host.pending.clear();
        let before = records.lock().unwrap().len();
        engine
            .flush_placements(
                vec![(
                    intent.clone(),
                    Some(EffectKey {
                        transition_id: transition.id,
                        index: 0,
                    }),
                )],
                clock::now_ns(),
            )
            .await
            .unwrap();
        assert!(engine.host.effects.transitions.is_empty());
        assert!(engine.host.callbacks.refused_orders.is_empty());
        assert!(engine.books.orders.orders.is_empty());
        assert!(
            !records.lock().unwrap()[before..]
                .iter()
                .any(|record| matches!(
                    record,
                    WalRecord::StrategyCallbackSource { .. }
                        | WalRecord::OrderSent { .. }
                        | WalRecord::Intent { .. }
                )),
            "restart re-admitted an effect whose refusal was already durable"
        );
        assert_eq!(
            replay
                .iter()
                .filter(|record| matches!(
                    record,
                    WalRecord::StrategyCallbackSource {
                        event: CallbackEvent::IntentRefused { .. },
                        ..
                    }
                ))
                .count(),
            1
        );
    }

    #[tokio::test(start_paused = true)]
    async fn an_inactive_sleeve_preserves_uncommitted_callbacks_and_reactivates_the_same_tail() {
        struct Paused;
        impl Strategy for Paused {
            fn name(&self) -> &str {
                "paused"
            }
            fn subscriptions(&self) -> Vec<Subscription> {
                Vec::new()
            }
            fn callback_enabled(&self) -> bool {
                false
            }
            fn on_event(&mut self, _: &EngineEvent, _: &mut dyn engine_types::StrategyCtx) {
                panic!("paused callback executed");
            }
            fn runtime_state(
                &self,
            ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String>
            {
                panic!("inactive constructor requested an executable runtime");
            }
        }
        let (mut engine, records, input_id) = prepared().await;
        let mut completion = proposal_completion(&mut engine, input_id);
        completion.result.as_mut().unwrap().1.actions.clear();
        engine.on_strategy_callback(Some(completion)).unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        assert!(engine.feed_one_strategy(StrategyId(0), &EngineEvent::Boot, clock::now_ns()));
        engine.service_strategy_callbacks().unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        let base = engine.rotation_base(clock::wall_ms());
        let active = std::mem::replace(&mut engine.host.strategies, vec![Box::new(Paused)]);
        engine.host.callbacks.stop().await;
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            std::slice::from_ref(&base),
        )
        .unwrap();
        let pending = engine.host.callbacks.state.inputs.clone();
        let committed = engine.host.callbacks.state.committed.clone();
        records.lock().unwrap().clear();
        engine.restore_strategy_callbacks().await.unwrap();
        assert_eq!(engine.host.strategies[0].name(), "paused");
        assert_eq!(engine.host.callbacks.state.inputs, pending);
        assert_eq!(engine.host.callbacks.state.committed, committed);
        assert!(!engine.host.callbacks.running());
        assert!(!records.lock().unwrap().iter().any(|record| matches!(
            record,
            WalRecord::StrategyCallbackPrepared { .. }
                | WalRecord::StrategyProcessTransitionQueued { .. }
        )));
        let rotated = engine.rotation_base(clock::wall_ms());
        engine.host.strategies = active;
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            &[rotated],
        )
        .unwrap();
        assert_eq!(engine.host.callbacks.state.inputs, pending);
        engine.service_strategy_callbacks().unwrap();
        assert!(matches!(
            engine.host.callbacks.write,
            Some(CallbackWrite::Prepare(_))
        ));
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        assert!(engine
            .host
            .callbacks
            .state
            .inputs
            .values()
            .next()
            .unwrap()
            .snapshot()
            .is_some());
        assert_eq!(engine.host.callbacks.state.committed, committed);
    }

    #[tokio::test(start_paused = true)]
    async fn successive_callbacks_cannot_grow_committed_timers_without_a_bound() {
        let (mut engine, records, mut input_id) = prepared().await;
        for batch in 0..32_u32 {
            let previous = engine
                .host
                .callbacks
                .state
                .committed
                .get(&StrategyId(0))
                .cloned();
            let mut completion = proposal_completion(&mut engine, input_id);
            let proposal = &mut completion.result.as_mut().unwrap().1;
            proposal.actions.clear();
            proposal.timers = (0..16)
                .map(|offset| StrategyTimerState {
                    id: TimerId(batch * 16 + offset),
                    deadline_ns: u64::MAX,
                    deadline_wall_ms: i64::MAX,
                })
                .collect();
            let mut candidate = previous.clone().unwrap_or_else(|| StrategyProcessState {
                strategy: StrategyId(0),
                last_callback_id: input_id,
                runtime: proposal.state.clone(),
                timers: Vec::new(),
                retained_signal_subscriptions: None,
            });
            candidate.timers.extend(proposal.timers.iter().cloned());
            let exceeds =
                candidate.timers.len() > engine_types::strategy_process::MAX_PROCESS_TIMERS;
            if exceeds {
                proposal.actions.push(Action::SetStrategyGlobalCheckpoint {
                    strategy: StrategyId(0),
                    checkpoint: engine_types::StrategyCheckpoint {
                        schema_version: 1,
                        decision_fingerprint: "retained-budget".into(),
                        payload: vec![1],
                    },
                });
                proposal.actions.push(Action::Cancel {
                    symbol: SymbolId(0),
                    client_order_id: "required-effect-suffix".into(),
                });
            }
            records.lock().unwrap().clear();
            engine.on_strategy_callback(Some(completion)).unwrap();
            if exceeds {
                assert!(
                    batch > 1,
                    "fixture must exercise growth across committed callbacks"
                );
                assert!(
                    engine.host.callbacks.write.is_none(),
                    "lifetime timer growth became a durable state/effect transition"
                );
                assert_eq!(
                    engine.host.callbacks.state.committed.get(&StrategyId(0)),
                    previous.as_ref()
                );
                assert!(engine.host.callbacks.state.inputs.contains_key(&input_id));
                assert!(engine.host.pending.is_empty());
                assert!(engine.host.callbacks.faults.contains_key(&StrategyId(0)));
                assert!(!records.lock().unwrap().iter().any(|record| matches!(
                    record,
                    WalRecord::StrategyProcessTransitionQueued { .. }
                )));
                return;
            }
            let result = engine.host.callbacks.durable.recv().await;
            engine.on_callback_durable(result).unwrap();
            assert!(engine.feed_one_strategy(StrategyId(0), &EngineEvent::Boot, clock::now_ns()));
            engine.service_strategy_callbacks().unwrap();
            let result = engine.host.callbacks.durable.recv().await;
            engine.on_callback_durable(result).unwrap();
            engine.service_strategy_callbacks().unwrap();
            let result = engine.host.callbacks.durable.recv().await;
            engine.on_callback_durable(result).unwrap();
            input_id = *engine.host.callbacks.state.inputs.keys().next().unwrap();
        }
        panic!("fixture did not cross the complete-state byte bound");
    }

    #[tokio::test(start_paused = true)]
    async fn an_invalid_durable_effect_aborts_before_private_state_commit() {
        let (mut engine, records, input_id) = prepared().await;
        let mut completion = proposal_completion(&mut engine, input_id);
        completion
            .result
            .as_mut()
            .unwrap()
            .1
            .actions
            .push(Action::ConsumeRuntimeControl {
                strategy: StrategyId(0),
                request_id: "never-accepted".into(),
            });
        engine.on_strategy_callback(Some(completion)).unwrap();
        assert!(
            engine.host.callbacks.write.is_none(),
            "invalid effect became a durable private-state transition"
        );
        assert!(engine.host.callbacks.state.committed.is_empty());
        assert!(engine.host.callbacks.state.inputs.contains_key(&input_id));
        assert!(engine.host.pending.is_empty());
        assert!(engine.host.callbacks.faults.contains_key(&StrategyId(0)));
        assert!(!records
            .lock()
            .unwrap()
            .iter()
            .any(|record| matches!(record, WalRecord::StrategyProcessTransitionQueued { .. })));
    }

    #[tokio::test(start_paused = true)]
    async fn a_second_queued_callback_uses_facts_after_the_first_opening_is_materialized() {
        let (mut engine, _, input_id) = prepared().await;
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: 99.0,
                ask_px: 101.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        assert!(engine.feed_one_strategy(
            StrategyId(0),
            &EngineEvent::Timer {
                id: TimerId(9),
                now_ns: clock::now_ns()
            },
            clock::now_ns()
        ));
        let mut completion = proposal_completion(&mut engine, input_id);
        completion.result.as_mut().unwrap().1.actions = vec![Action::Place(Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.1,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: 90.0 }),
            reduce_only: false,
            tag: "fresh-snapshot".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        })];
        engine.on_strategy_callback(Some(completion)).unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        engine.service_strategy_callbacks().unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        let second_id = *engine.host.callbacks.state.inputs.keys().next().unwrap();
        engine.service_strategy_callbacks().unwrap();
        assert!(
            engine.host.callbacks.write.is_none(),
            "second callback was prepared before the first callback's opening effect"
        );
        assert!(engine.host.callbacks.state.inputs[&second_id]
            .snapshot()
            .is_none());
        engine.drain(clock::now_ns()).await.unwrap();
        engine.service_strategy_callbacks().unwrap();
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        let snapshot = engine.host.callbacks.state.inputs[&second_id]
            .snapshot()
            .unwrap();
        assert_eq!(snapshot.orders.len(), 1);
        let facts = snapshot.symbols[0].facts.as_ref().unwrap();
        assert_eq!(facts.open_order_count, 1);
        assert!(facts.in_flight_signed_qty > 0.0);
        engine.host.callbacks.stop().await;
    }

    #[tokio::test(start_paused = true)]
    async fn callback_commit_barrier_failure_publishes_neither_state_timers_nor_effects() {
        let (mut engine, _, input_id) = prepared().await;
        engine.wal.fail_barrier_after = Some("strategy_process_transition_queued");
        let completion = proposal_completion(&mut engine, input_id);
        assert!(engine.on_strategy_callback(Some(completion)).is_err());
        assert!(engine.host.callbacks.state.committed.is_empty());
        assert!(engine.host.callbacks.state.inputs.contains_key(&input_id));
        assert_eq!(engine.host.timers.armed_count(), 0);
        assert!(engine.host.pending.is_empty());
        assert!(engine.host.effects.transitions.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn callback_state_timer_and_effect_suffix_replay_at_the_same_commit_cut() {
        let (mut engine, records, input_id) = prepared().await;
        let completion = proposal_completion(&mut engine, input_id);
        engine.on_strategy_callback(Some(completion)).unwrap();
        assert!(
            engine.host.callbacks.state.committed.is_empty(),
            "a started barrier does not publish state"
        );
        let settled = engine.host.callbacks.durable.try_recv().unwrap();
        engine.on_callback_durable(Some(settled)).unwrap();
        assert!(engine.host.callbacks.state.inputs.is_empty());
        assert_eq!(engine.host.timers.armed_count(), 1);
        assert_eq!(engine.host.pending.len(), 1);
        let records = records.lock().unwrap().clone();
        let cut = records
            .iter()
            .position(|record| matches!(record, WalRecord::StrategyProcessTransitionQueued { .. }))
            .unwrap();
        let before =
            crate::strategy_process::state::CallbackState::replay(&records[..cut], 1).unwrap();
        assert!(before.committed.is_empty());
        assert!(before.inputs.contains_key(&input_id));
        let after =
            crate::strategy_process::state::CallbackState::replay(&records[..=cut], 1).unwrap();
        assert!(after.inputs.is_empty());
        assert_eq!(after.committed[&StrategyId(0)].timers.len(), 1);
        let effects = crate::effects::Effects::replay(&records[..=cut], 1).unwrap();
        assert_eq!(
            effects.transitions.values().next().unwrap().effects.len(),
            1
        );
        let base = engine.rotation_base(clock::wall_ms());
        let rotated = crate::strategy_process::state::CallbackState::replay(&[base], 1).unwrap();
        assert_eq!(rotated.committed, after.committed);
        engine.host.callbacks.stop().await;
    }

    #[tokio::test(start_paused = true)]
    async fn fired_timer_is_owned_by_queued_input_until_no_rearm_commit() {
        let (mut engine, _, input_id) = prepared().await;
        let completion = proposal_completion(&mut engine, input_id);
        engine.on_strategy_callback(Some(completion)).unwrap();
        let settled = engine.host.callbacks.durable.try_recv().unwrap();
        engine.on_callback_durable(Some(settled)).unwrap();
        engine.host.pending.clear();
        engine.host.effects.transitions.clear();
        engine.host.effects.journaled.clear();
        assert!(engine.feed_one_strategy(
            StrategyId(0),
            &EngineEvent::Timer {
                id: TimerId(7),
                now_ns: clock::now_ns()
            },
            clock::now_ns()
        ));
        engine.service_strategy_callbacks().unwrap();
        engine.service_strategy_callbacks().unwrap();
        assert!(engine.host.callbacks.state.committed[&StrategyId(0)]
            .timers
            .is_empty());
        let base = engine.rotation_base(clock::wall_ms());
        let replayed = crate::strategy_process::state::CallbackState::replay(&[base], 1).unwrap();
        assert!(replayed.committed[&StrategyId(0)].timers.is_empty());
        assert_eq!(
            replayed.inputs.len(),
            1,
            "an aborted timer callback remains replayable"
        );
        let settled = engine.host.callbacks.durable.try_recv().unwrap();
        engine.on_callback_durable(Some(settled)).unwrap();
        let next_id = *engine.host.callbacks.state.inputs.keys().next().unwrap();
        let mut completion = proposal_completion(&mut engine, next_id);
        completion.result.as_mut().unwrap().1.timers.clear();
        completion.result.as_mut().unwrap().1.actions.clear();
        engine.on_strategy_callback(Some(completion)).unwrap();
        let settled = engine.host.callbacks.durable.try_recv().unwrap();
        engine.on_callback_durable(Some(settled)).unwrap();
        assert_eq!(engine.host.timers.armed_count(), 0);
        assert!(engine.host.callbacks.state.inputs.is_empty());
        engine.host.callbacks.stop().await;
    }
    #[tokio::test(start_paused = true)]
    async fn delayed_callback_fsync_keeps_market_turns_live_and_state_unpublished() {
        let (mut engine, _, input_id) = prepared().await;
        engine
            .wal
            .delay_callback_barriers(Duration::from_millis(250));
        let completion = proposal_completion(&mut engine, input_id);
        let began = std::time::Instant::now();
        engine.on_strategy_callback(Some(completion)).unwrap();
        assert!(
            began.elapsed() < Duration::from_millis(100),
            "callback fsync blocked the core for {:?}",
            began.elapsed()
        );
        assert!(engine.host.callbacks.state.committed.is_empty());
        engine
            .on_market(&MarketEvent::Quote {
                symbol: SymbolId(0),
                quote: engine_types::Quote {
                    bid_px: 123.0,
                    ask_px: 124.0,
                    recv_ns: clock::now_ns(),
                    ..Default::default()
                },
            })
            .await
            .unwrap();
        assert_eq!(engine.books.market.quotes[0].bid_px, 123.0);
        assert!(engine.host.pending.is_empty());
        let result = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(result).unwrap();
        assert_eq!(engine.host.pending.len(), 1);
        engine.host.callbacks.stop().await;
    }
}
