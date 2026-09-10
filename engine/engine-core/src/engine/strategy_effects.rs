use super::*;
use crate::effects::EffectKey;

type PlacementEffect = (
    Intent,
    Option<EffectKey>,
    Option<crate::ctx::CallbackTiming>,
    Option<std::sync::Arc<engine_types::DecisionCause>>,
);
type CancellationEffect = (SymbolId, String, Option<EffectKey>);

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn flush_strategy_prefix(&mut self) -> Result<(), EngineError> {
        if self.strategy_barrier_pending {
            self.wal.barrier()?;
            self.strategy_barrier_pending = false;
            for owner in std::mem::take(&mut self.strategy_runtime_retirements) {
                self.host.callbacks.state.forget_process(owner);
            }
        }
        Ok(())
    }

    pub(super) fn begin_dispatch_barrier(
        &mut self,
    ) -> Result<engine_types::wal::PendingBarrier, EngineError> {
        let barrier = self.wal.barrier_begin()?;
        self.strategy_barrier_pending = false;
        self.dispatches
            .strategy_runtime_retirements
            .append(&mut self.strategy_runtime_retirements);
        Ok(barrier)
    }

    pub(super) fn journal_transition(&mut self, id: u64) -> Result<(), EngineError> {
        if self.host.effects.journaled.contains(&id) {
            return Ok(());
        }
        let earlier: Vec<_> = self
            .host
            .effects
            .transitions
            .range(..id)
            .filter(|(earlier, _)| !self.host.effects.journaled.contains(earlier))
            .map(|(earlier, _)| *earlier)
            .collect();
        for earlier in earlier {
            self.journal_transition(earlier)?;
        }
        let transition = self.prepare_transition(id)?;
        self.wal.append(&WalRecord::StrategyTransitionQueued {
            transition: transition.clone(),
        })?;
        self.strategy_barrier_pending = true;
        if transition
            .effects
            .iter()
            .any(|action| matches!(action, Action::SetStrategyGlobalCheckpoint { .. }))
        {
            self.strategy_runtime_retirements
                .insert(transition.strategy);
        }
        for id in transition.order_ids.iter().flatten() {
            self.books.registry.own(id, transition.strategy);
        }
        self.host.effects.transitions.insert(id, transition);
        self.host.effects.journaled.insert(id);
        Ok(())
    }

    pub(super) fn prepare_transition(
        &mut self,
        id: u64,
    ) -> Result<engine_types::StrategyTransitionState, EngineError> {
        let transition = self
            .host
            .effects
            .transitions
            .get(&id)
            .ok_or_else(|| EngineError::State(format!("missing strategy transition {id}")))?
            .clone();
        self.prepare_effect_transition(transition)
    }

    pub(super) fn prepare_effect_transition(
        &mut self,
        mut transition: engine_types::StrategyTransitionState,
    ) -> Result<engine_types::StrategyTransitionState, EngineError> {
        let id = transition.id;
        for (action, order_id) in transition.effects.iter().zip(&mut transition.order_ids) {
            if matches!(action, Action::Place(_)) {
                let id = self.mint_id()?;
                *order_id = Some(id);
            }
        }
        let encoded = serde_json::to_vec(&transition)
            .map_err(|error| EngineError::State(error.to_string()))?;
        serde_json::from_slice::<engine_types::StrategyTransitionState>(&encoded).map_err(
            |error| {
                EngineError::State(format!(
                    "strategy transition {id} cannot be replayed: {error}"
                ))
            },
        )?;
        Ok(transition)
    }

    pub(super) fn complete_effect(&mut self, effect: Option<EffectKey>) -> Result<(), EngineError> {
        let Some(key) = effect else {
            return Ok(());
        };
        let order_id = self
            .host
            .effects
            .transitions
            .get(&key.transition_id)
            .and_then(|transition| transition.order_ids.get(key.index))
            .cloned()
            .flatten();
        self.wal.append(&WalRecord::StrategyEffectCompleted {
            transition_id: key.transition_id,
            effect_index: key.index,
        })?;
        self.host
            .effects
            .complete(key)
            .map_err(EngineError::State)?;
        if let Some(id) = order_id {
            self.host.callbacks.refused_orders.remove(&id);
        }
        Ok(())
    }

    pub(super) async fn flush_placements(
        &mut self,
        pending: Vec<PlacementEffect>,
        origin_ns: u64,
    ) -> Result<bool, EngineError> {
        if pending.is_empty() {
            return Ok(false);
        }
        let mut intents = Vec::with_capacity(pending.len());
        let mut completed = Vec::with_capacity(pending.len());
        for (intent, effect, timing, cause) in pending {
            let order_id = effect.and_then(|key| {
                self.host
                    .effects
                    .transitions
                    .get(&key.transition_id)
                    .and_then(|transition| transition.order_ids.get(key.index))
                    .cloned()
                    .flatten()
            });
            if order_id.as_ref().is_some_and(|id| {
                self.books.orders.contains(id) || self.host.callbacks.refused_orders.contains(id)
            }) {
                self.complete_effect(effect)?;
                continue;
            }
            intents.push((intent, order_id, timing, cause));
            completed.push(effect);
        }
        let sent = self.process_intents(intents, origin_ns).await?;
        for effect in completed {
            self.complete_effect(effect)?;
        }
        Ok(sent)
    }

    pub(super) async fn flush_cancellations(
        &mut self,
        pending: Vec<CancellationEffect>,
    ) -> Result<bool, EngineError> {
        if pending.is_empty() {
            return Ok(false);
        }
        let requests = pending
            .iter()
            .map(|(symbol, id, _)| (*symbol, id.clone()))
            .collect();
        let sent = self.process_cancels(requests).await?;
        for (_, _, effect) in pending {
            self.complete_effect(effect)?;
        }
        Ok(sent)
    }

    pub(super) async fn restore_strategy_effects(&mut self) -> Result<(), EngineError> {
        for transition in self.host.effects.transitions.values() {
            for id in transition.order_ids.iter().flatten() {
                self.books.registry.own(id, transition.strategy);
            }
            for (index, action) in transition.effects.iter().enumerate() {
                if !transition.completed.contains(&index) {
                    self.host.pending.push_back(PendingAction {
                        caller: Some(transition.strategy),
                        action: action.clone(),
                        effect: Some(EffectKey {
                            transition_id: transition.id,
                            index,
                        }),
                        callback_id: Some(transition.id),
                        timing: None,
                        cause: Some(std::sync::Arc::new(engine_types::DecisionCause {
                            callback_wall_ms: clock::wall_ms(),
                            callback_id: Some(transition.id),
                            causes: vec![engine_types::Cause::Restored],
                        })),
                    });
                }
            }
        }
        while !self.host.pending.is_empty()
            || !self.ready_actions.is_empty()
            || !self.pending_mutations.is_empty()
            || self.dispatches.write.is_some()
        {
            tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.drain(clock::now_ns()))
                .await
                .map_err(|_| {
                    EngineError::TimedOut("strategy effects during boot restore".into())
                })??;
            if self.dispatches.write.is_some() {
                let result =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.dispatches.durable.recv())
                        .await
                        .map_err(|_| {
                            EngineError::TimedOut(
                                "order dispatch durability during boot restore".into(),
                            )
                        })?;
                self.on_order_dispatch_durable(result).await?;
                continue;
            }
            if !self.pending_mutations.is_empty() {
                let completion =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.venue_completions.recv())
                        .await
                        .map_err(|_| {
                            EngineError::TimedOut(
                                "strategy effect mutation during boot restore".into(),
                            )
                        })?
                        .ok_or(EngineError::TaskStopped {
                            task: EngineTask::Venue,
                            detail: "while restoring strategy effects",
                        })?;
                self.take_venue_completion(completion).await?;
            }
        }
        Ok(())
    }
}
