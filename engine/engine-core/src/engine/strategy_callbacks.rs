use super::*;
use crate::effects::EffectKey;
use crate::strategy_process::host::{CallbackCompletion, CallbackWrite};
use engine_types::strategy_process::{CallbackPreparation, StrategyProcessState};

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn service_strategy_callbacks(&mut self) -> Result<(), EngineError> {
        if !self.host.callbacks.isolated() {
            return Ok(());
        }
        if self.host.callbacks.write.is_some() {
            match self.host.callbacks.durable.try_recv() {
                Ok(result) => self.on_callback_durable(Some(result))?,
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => return Ok(()),
                Err(_) => {
                    return Err(EngineError::State(
                        "callback durability channel closed".into(),
                    ))
                }
            }
        }
        if !self.host.callbacks.unwritten.is_empty() {
            for input in &self.host.callbacks.unwritten {
                self.wal.append(&WalRecord::StrategyCallbackQueued {
                    input: input.clone(),
                })?;
            }
            let barrier = self.wal.barrier_begin()?;
            let inputs = self.host.callbacks.unwritten.drain(..).collect();
            self.host
                .callbacks
                .begin_write(CallbackWrite::Accept(inputs), barrier);
            return Ok(());
        }
        let strategies: std::collections::BTreeSet<_> = self
            .host
            .callbacks
            .state
            .inputs
            .values()
            .map(|input| input.strategy)
            .collect();
        for strategy in strategies {
            if self.host.effects.earliest(strategy).is_some()
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
                self.wal.append(&WalRecord::StrategyCallbackPrepared {
                    input: prepared.clone(),
                })?;
                let barrier = self.wal.barrier_begin()?;
                self.host
                    .callbacks
                    .begin_write(CallbackWrite::Prepare(prepared), barrier);
                return Ok(());
            }
            if let Err(error) = self.host.callbacks.launch(strategy) {
                self.fail_strategy_callback(strategy, error)?;
            }
        }
        Ok(())
    }

    fn fail_strategy_callback(
        &mut self,
        strategy: StrategyId,
        error: String,
    ) -> Result<(), EngineError> {
        let changed = self.host.callbacks.faults.get(&strategy) != Some(&error);
        self.host.callbacks.failed(strategy, error.clone());
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
        Ok(())
    }

    pub(super) fn on_strategy_callback(
        &mut self,
        completion: Option<CallbackCompletion>,
    ) -> Result<(), EngineError> {
        let completion = completion
            .ok_or_else(|| EngineError::State("strategy completion channel closed".into()))?;
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
        result.ok_or_else(|| EngineError::State("callback durability task stopped".into()))??;
        let write =
            self.host.callbacks.write.take().ok_or_else(|| {
                EngineError::State("callback durability result has no owner".into())
            })?;
        match write {
            CallbackWrite::Accept(inputs) => {
                for input in inputs {
                    self.host
                        .callbacks
                        .accepted(input)
                        .map_err(EngineError::State)?;
                }
            }
            CallbackWrite::Prepare(input) => self
                .host
                .callbacks
                .state
                .prepared(input)
                .map_err(EngineError::State)?,
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
                            EngineError::Boot("timed out restoring callback effect mutation".into())
                        })?
                        .ok_or_else(|| {
                            EngineError::Boot(
                                "venue task stopped restoring callback effects".into(),
                            )
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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

    #[tokio::test]
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
    #[tokio::test]
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
            .on_market(MarketEvent::Quote {
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
