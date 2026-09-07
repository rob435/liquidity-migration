use std::collections::{BTreeMap, BTreeSet};

use engine_types::{Action, StrategyEffectsState, StrategyId, StrategyTransitionState, WalRecord};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EffectKey {
    pub transition_id: u64,
    pub index: usize,
}

#[derive(Default)]
pub(crate) struct Effects {
    pub next_id: u64,
    pub transitions: BTreeMap<u64, StrategyTransitionState>,
    pub journaled: BTreeSet<u64>,
}

impl Effects {
    pub fn capture(&mut self, strategy: StrategyId, effects: Vec<Action>) -> u64 {
        let id = self.next_id;
        self.next_id = id.checked_add(1).expect("strategy transition id exhausted");
        self.transitions.insert(
            id,
            StrategyTransitionState {
                origin: engine_types::wal::StrategyTransitionOrigin::Embedded,
                id,
                strategy,
                order_ids: vec![None; effects.len()],
                effects,
                completed: Vec::new(),
            },
        );
        id
    }

    pub fn earliest(&self, strategy: StrategyId) -> Option<u64> {
        self.transitions
            .values()
            .find(|transition| transition.strategy == strategy)
            .map(|transition| transition.id)
    }

    pub fn snapshot(&self) -> StrategyEffectsState {
        StrategyEffectsState {
            next_transition_id: self.next_id,
            transitions: self
                .transitions
                .values()
                .filter(|transition| self.journaled.contains(&transition.id))
                .cloned()
                .collect(),
        }
    }

    pub fn complete(&mut self, key: EffectKey) -> Result<(), String> {
        let transition = self
            .transitions
            .get_mut(&key.transition_id)
            .ok_or_else(|| {
                format!(
                    "completion names unknown strategy transition {}",
                    key.transition_id
                )
            })?;
        if key.index >= transition.effects.len() || transition.completed.contains(&key.index) {
            return Err(format!(
                "invalid or repeated effect {} for transition {}",
                key.index, key.transition_id
            ));
        }
        transition.completed.push(key.index);
        if transition.completed.len() == transition.effects.len() {
            self.transitions.remove(&key.transition_id);
            self.journaled.remove(&key.transition_id);
        }
        Ok(())
    }

    pub fn replay(records: &[WalRecord], strategy_count: usize) -> Result<Self, String> {
        let mut result = Self::default();
        for record in records {
            match record {
                WalRecord::SegmentBase {
                    strategy_effects, ..
                } => {
                    result = Self::default();
                    result.next_id = strategy_effects.next_transition_id;
                    for transition in &strategy_effects.transitions {
                        result.restore(transition.clone(), strategy_count)?;
                    }
                }
                WalRecord::StrategyTransitionQueued { transition }
                | WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued {
                        transition: Some(transition),
                        ..
                    },
                ) => {
                    if transition.id < result.next_id {
                        return Err(format!(
                            "strategy transition id {} is reused",
                            transition.id
                        ));
                    }
                    result.next_id = transition
                        .id
                        .checked_add(1)
                        .ok_or_else(|| "strategy transition id exhausted".to_string())?;
                    result.restore(transition.clone(), strategy_count)?;
                }
                WalRecord::StrategyEffectCompleted {
                    transition_id,
                    effect_index,
                } => {
                    result.complete(EffectKey {
                        transition_id: *transition_id,
                        index: *effect_index,
                    })?;
                }
                _ => {}
            }
        }
        Ok(result)
    }

    fn restore(
        &mut self,
        transition: StrategyTransitionState,
        strategy_count: usize,
    ) -> Result<(), String> {
        let valid =
            transition.id < self.next_id
                && transition.strategy.idx() < strategy_count
                && !transition.effects.is_empty()
                && transition.effects.len() == transition.order_ids.len()
                && transition.completed.len() < transition.effects.len()
                && transition
                    .completed
                    .iter()
                    .all(|index| *index < transition.effects.len())
                && transition
                    .completed
                    .iter()
                    .copied()
                    .collect::<BTreeSet<_>>()
                    .len()
                    == transition.completed.len()
                && transition.effects.iter().zip(&transition.order_ids).all(
                    |(action, order_id)| match action {
                        Action::Place(intent) => {
                            intent.strategy == transition.strategy
                                && order_id.as_ref().is_some_and(|id| !id.is_empty())
                        }
                        _ => order_id.is_none(),
                    },
                );
        if !valid || self.transitions.contains_key(&transition.id) {
            return Err(format!("invalid strategy transition {}", transition.id));
        }
        self.journaled.insert(transition.id);
        self.transitions.insert(transition.id, transition);
        Ok(())
    }
}
