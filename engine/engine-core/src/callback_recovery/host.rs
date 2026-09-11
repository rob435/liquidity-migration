use std::collections::{BTreeMap, BTreeSet};

use engine_types::{Strategy, StrategyId, WalRecord};

use super::{order_news::OrderNews, paging::CallbackPages, state::CallbackState};

pub enum CallbackExecution {
    Embedded,
}

pub struct CallbackHost {
    pub state: CallbackState,
    pub pages: CallbackPages,
    pub order_news: OrderNews,
    pub faults: BTreeMap<StrategyId, String>,
    pub refused_orders: BTreeSet<String>,
    active: BTreeSet<StrategyId>,
    pub recovering: bool,
}

impl CallbackHost {
    /// Apply boot's process checks without fetching historical callback pages.
    pub fn validate_recovery(
        strategies: &[Box<dyn Strategy>],
        records: &[WalRecord],
    ) -> Result<(), String> {
        // Takeover reads a whole chain and keeps no cursor; segment 1 and the
        // dense sequences are this projection's own.
        let (state, pages) = CallbackPages::replay(
            &crate::assembly::BootReplay::dense(records),
            strategies.len(),
            1,
        )?;
        Self::build(
            CallbackExecution::Embedded,
            strategies,
            records,
            state,
            pages,
        )?;
        Ok(())
    }

    pub fn new(
        execution: CallbackExecution,
        strategies: &[Box<dyn Strategy>],
        records: &[WalRecord],
    ) -> Result<Self, String> {
        let state = CallbackState::replay(records, strategies.len())?;
        Self::build(execution, strategies, records, state, Default::default())
    }

    pub fn new_paged(
        execution: CallbackExecution,
        strategies: &[Box<dyn Strategy>],
        replayed: &crate::assembly::BootReplay<'_>,
        reader: Box<dyn engine_types::strategy_process::CallbackWalReader>,
    ) -> Result<Self, String> {
        let (state, mut pages) =
            CallbackPages::replay(replayed, strategies.len(), reader.start().segment)?;
        pages.attach(reader);
        Self::build(execution, strategies, replayed, state, pages)
    }

    fn build(
        CallbackExecution::Embedded: CallbackExecution,
        strategies: &[Box<dyn Strategy>],
        records: &[WalRecord],
        state: CallbackState,
        pages: CallbackPages,
    ) -> Result<Self, String> {
        let mut active = BTreeSet::new();
        for (index, strategy) in strategies.iter().enumerate() {
            let id = StrategyId(u16::try_from(index).map_err(|_| "too many strategies")?);
            if !strategy.callback_enabled() {
                continue;
            }
            active.insert(id);
            if let Some(previous) = state.committed.get(&id) {
                let runtime = strategy
                    .runtime_state()?
                    .ok_or("retained runtime has no registered strategy")?;
                if previous.runtime.kind != runtime.kind
                    || previous.runtime.configuration_sha256 != runtime.configuration_sha256
                {
                    return Err(format!(
                        "strategy {} retained runtime belongs to another configuration",
                        strategy.name()
                    ));
                }
            }
        }
        let effects = crate::effects::Effects::replay(records, strategies.len())?;
        let mut refused_orders = BTreeSet::new();
        for record in records {
            let WalRecord::StrategyCallbackSource {
                placement: Some(id),
                strategy,
                event,
            } = record
            else {
                continue;
            };
            let pending = effects.transitions.values().find_map(|transition| {
                transition
                    .order_ids
                    .iter()
                    .position(|known| known.as_ref() == Some(id))
                    .map(|index| (&transition.effects[index], transition.strategy))
            });
            let Some((engine_types::Action::Place(intent), owner)) = pending else {
                continue;
            };
            if !matches!(event, engine_types::strategy_process::CallbackEvent::IntentRefused { symbol, reduce_only, .. } if *symbol == intent.symbol && *reduce_only == intent.reduce_only)
                || *strategy != owner
                || !refused_orders.insert(id.clone())
            {
                return Err("durable refusal changes or repeats its placement authority".into());
            }
        }
        let recovering = !state.committed.is_empty() || !state.inputs.is_empty() || !pages.slots.is_empty()
            || records.iter().any(|record| matches!(record,
                WalRecord::SegmentBase { strategy_callback_sources, .. } if !strategy_callback_sources.is_empty())
                || matches!(record, WalRecord::StrategyCallbackSource { .. } | WalRecord::OrderUpdate { callbacks: Some(_), .. } | WalRecord::RecoveredFill { callbacks: Some(_), .. }));
        Ok(Self {
            state,
            pages,
            active,
            recovering,
            refused_orders,
            order_news: OrderNews::default(),
            faults: BTreeMap::new(),
        })
    }

    pub fn is_active(&self, strategy: StrategyId) -> bool {
        self.active.contains(&strategy)
    }
    pub fn active(&self) -> BTreeSet<StrategyId> {
        self.active
            .iter()
            .copied()
            .filter(|owner| !self.faults.contains_key(owner))
            .collect()
    }
    pub fn pending_for(&self, strategy: StrategyId) -> bool {
        self.pages.owner_pending(strategy)
            || self
                .state
                .inputs
                .values()
                .any(|input| input.strategy == strategy)
    }
}
