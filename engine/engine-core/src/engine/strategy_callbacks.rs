use super::*;
use crate::callback_recovery::order_news::OrderNews;
use engine_types::strategy_process::{
    CallbackEvent, CallbackReply, SnapshotCtx, StrategyProcessState,
};

#[cfg(test)]
#[path = "strategy_callbacks/embedded_tests.rs"]
mod embedded_tests;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn ensure_callback_reader(
        &mut self,
        replayed: &[WalRecord],
    ) -> Result<(), EngineError> {
        if self.host.callbacks.recovering && !self.host.callbacks.order_news.has_reader() {
            let reader = self.wal.callback_reader()?.ok_or_else(|| {
                EngineError::State("retained callbacks require their WAL reader".into())
            })?;
            self.host
                .callbacks
                .order_news
                .attach(reader, replayed, self.host.strategies.len())
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
        if placement
            .as_ref()
            .is_some_and(|id| self.host.callbacks.refused_orders.contains(id))
        {
            return Ok(());
        }
        if self.host.callbacks.recovering {
            self.ensure_callback_reader(&[])?;
            let offset = self.wal.segment_size();
            let sequence = self.wal.append(&WalRecord::StrategyCallbackSource {
                placement: placement.clone(),
                strategy,
                event: CallbackEvent::from(&event),
            })?;
            if let Some(id) = placement {
                self.host.callbacks.refused_orders.insert(id);
            }
            self.host
                .callbacks
                .order_news
                .record_at(sequence, offset, &[strategy])
                .map_err(EngineError::State)?;
        } else {
            self.host
                .feed(&self.books, strategy, &event, clock::now_ns());
        }
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
        for action in actions {
            match action {
                Action::SetStrategyCheckpoint { checkpoint, .. }
                | Action::SetStrategyGlobalCheckpoint { checkpoint, .. } => {
                    validate_strategy_checkpoint(owner.as_ref(), checkpoint)?
                }
                Action::PublishStrategyEvent { event } => self
                    .validate_strategy_event(event)
                    .map_err(|error| error.to_string())?,
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
                _ => (),
            }
        }
        Ok(())
    }

    fn replay_retained_callback(
        &mut self,
        strategy: StrategyId,
        event: &CallbackEvent,
        snapshot: Option<&engine_types::strategy_process::CallbackSnapshot>,
    ) -> Result<bool, EngineError> {
        let event = EngineEvent::try_from(event).map_err(EngineError::Boot)?;
        if let Some(snapshot) = snapshot {
            let mut actions = VecDeque::new();
            let mut timers = Vec::new();
            let mut ctx = SnapshotCtx::new(snapshot, |reply| match reply {
                CallbackReply::Action { action } => {
                    actions.push_back(crate::ctx::bind_action(strategy, snapshot.now_ns, action))
                }
                CallbackReply::Timer { timer } => timers.push(timer),
                _ => (),
            })
            .map_err(EngineError::Boot)?;
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.host.strategies[strategy.idx()].on_event(&event, &mut ctx);
            }));
            drop(ctx);
            if result.is_err() {
                self.host
                    .fault(&self.books, strategy, "retained callback panicked".into());
                return Ok(false);
            }
            self.validate_callback_actions(strategy, actions.make_contiguous())
                .map_err(EngineError::Boot)?;
            self.host.capture_actions(
                strategy,
                &mut actions,
                snapshot.now_ns,
                engine_types::Cause::from(&event),
            );
            for timer in timers {
                let remaining_ms = timer
                    .deadline_wall_ms
                    .saturating_sub(clock::wall_ms())
                    .max(0) as u64;
                self.host.timers.arm(
                    strategy,
                    timer.id,
                    clock::now_ns().saturating_add(remaining_ms.saturating_mul(1_000_000)),
                );
            }
            Ok(true)
        } else {
            Ok(self
                .host
                .feed(&self.books, strategy, &event, clock::now_ns()))
        }
    }

    fn retain_recovered_runtime(
        &mut self,
        strategy: StrategyId,
        callback_id: u64,
    ) -> Result<(), EngineError> {
        let plug = &self.host.strategies[strategy.idx()];
        if let Some(runtime) = plug.runtime_state().map_err(EngineError::Boot)? {
            self.host.callbacks.state.committed.insert(
                strategy,
                StrategyProcessState {
                    strategy,
                    last_callback_id: callback_id,
                    runtime,
                    timers: self
                        .host
                        .timers
                        .snapshot(strategy, clock::now_ns(), clock::wall_ms()),
                    retained_signal_subscriptions: plug.retained_signal_subscriptions(),
                },
            );
        }
        Ok(())
    }

    async fn persist_callback_recovery(&mut self) -> Result<(), EngineError> {
        // The base retires legacy input and preserves its effect suffix in one frame.
        for pending in &mut self.host.pending {
            pending.timing = None;
            if pending.effect.is_none() {
                if let Some(strategy) = pending.caller {
                    let id = self
                        .host
                        .effects
                        .capture(strategy, vec![pending.action.clone()]);
                    pending.effect = Some(crate::effects::EffectKey {
                        transition_id: id,
                        index: 0,
                    });
                    pending.callback_id = Some(id);
                }
            }
        }
        let unjournaled: Vec<_> = self
            .host
            .effects
            .transitions
            .keys()
            .filter(|id| !self.host.effects.journaled.contains(id))
            .copied()
            .collect();
        for id in unjournaled {
            let transition = self.prepare_transition(id)?;
            for order in transition.order_ids.iter().flatten() {
                self.books.registry.own(order, transition.strategy);
            }
            self.host.effects.transitions.insert(id, transition);
            self.host.effects.journaled.insert(id);
        }
        self.wal.append(&self.rotation_base(clock::wall_ms()))?;
        self.wal.barrier()?;
        self.host.pending.retain(|pending| pending.effect.is_none());
        self.restore_strategy_effects().await
    }

    pub(super) async fn restore_strategy_callbacks(&mut self) -> Result<(), EngineError> {
        if !self.host.callbacks.recovering {
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
            let active = self.host.callbacks.active();
            self.host
                .callbacks
                .pages
                .start_load(&self.host.callbacks.state, &active);
            if self.host.callbacks.pages.loading() {
                let completion = self
                    .host
                    .callbacks
                    .pages
                    .completed
                    .recv()
                    .await
                    .ok_or_else(|| EngineError::Boot("retained callback reader stopped".into()))?;
                if let Some((strategy, error)) = self
                    .host
                    .callbacks
                    .pages
                    .returned(completion, &mut self.host.callbacks.state, 0)
                    .map_err(EngineError::Boot)?
                {
                    self.host.fault(&self.books, strategy, error);
                }
            }
            let input = self
                .host
                .callbacks
                .state
                .inputs
                .values()
                .find(|input| active.contains(&input.strategy))
                .cloned();
            if let Some(input) = input {
                if !self.replay_retained_callback(input.strategy, &input.event, input.snapshot())? {
                    continue;
                }
                self.host
                    .callbacks
                    .state
                    .discard(input.callback_id)
                    .map_err(EngineError::Boot)?;
                self.host.callbacks.pages.remove(input.callback_id);
                self.retain_recovered_runtime(input.strategy, input.callback_id)?;
                self.persist_callback_recovery().await?;
                continue;
            }
            self.wal.flush()?;
            self.host
                .callbacks
                .order_news
                .start_read_for(|strategy| active.contains(&strategy));
            if !self.host.callbacks.order_news.pending() {
                break;
            }
            let completion = self
                .host
                .callbacks
                .order_news
                .completed
                .recv()
                .await
                .ok_or_else(|| {
                    EngineError::Boot("retained callback source reader stopped".into())
                })?;
            let (strategy, cursor, record) = self
                .host
                .callbacks
                .order_news
                .returned(completion)
                .map_err(EngineError::Boot)?;
            let origin = engine_types::strategy_process::CallbackOrderOrigin {
                segment: cursor.segment,
                sequence: cursor.sequence,
            };
            if let Some((owners, event)) = record.source {
                if owners.contains(&strategy)
                    && self.host.callbacks.order_news.source_due(strategy, origin)
                {
                    let event = match event {
                        CallbackEvent::Order { update } => CallbackEvent::Order {
                            update: OrderNews::slice(&update, strategy)
                                .map_err(EngineError::Boot)?,
                        },
                        event => event,
                    };
                    if !self.replay_retained_callback(strategy, &event, None)? {
                        continue;
                    }
                    self.host.callbacks.order_news.accepted(strategy, origin);
                    self.host
                        .callbacks
                        .order_news
                        .advance(strategy, record.next);
                    let id = self.host.callbacks.state.next_id;
                    self.host.callbacks.state.next_id = id
                        .checked_add(1)
                        .ok_or_else(|| EngineError::Boot("callback identity exhausted".into()))?;
                    self.retain_recovered_runtime(strategy, id)?;
                    self.persist_callback_recovery().await?;
                    continue;
                }
            }
            self.host
                .callbacks
                .order_news
                .advance(strategy, record.next);
        }
        self.host.callbacks.recovering = false;
        Ok(())
    }
}
