use super::*;

pub(super) struct ConfiguredStrategies {
    pub(super) market: MarketState,
    pub(super) routing: Routing,
    pub(super) names: Vec<String>,
    pub(super) subscriptions: Vec<Subscription>,
    pub(super) signal_dependencies: Vec<Vec<StrategyId>>,
    pub(super) initial_global_checkpoints:
        std::collections::BTreeMap<StrategyId, StrategyGlobalCheckpointState>,
}

pub(super) fn restore_configuration(
    strategies: &[Box<dyn Strategy>],
    sleeves: &[String],
    replayed: &[WalRecord],
) -> Result<ConfiguredStrategies, EngineError> {
    let names: Vec<_> = strategies
        .iter()
        .enumerate()
        .map(|(index, strategy)| {
            sleeves
                .get(index)
                .filter(|name| !name.is_empty())
                .cloned()
                .unwrap_or_else(|| strategy.name().to_string())
        })
        .collect();
    let requested: Vec<_> = strategies
        .iter()
        .flat_map(|strategy| strategy.subscriptions())
        .map(|subscription| subscription.symbol)
        .collect();
    let plan = crate::identities::plan_identities(replayed, &names, None, &Default::default(), &[])
        .map_err(|error| EngineError::Boot(error.to_string()))?;
    if plan
        .slot_configs
        .iter()
        .enumerate()
        .any(|(index, config)| *config != Some(index))
    {
        return Err(EngineError::Boot("strategy instances must be constructed in durable identity order; use assembly::strategies_for_registry before boot".into()));
    }
    let symbol_count = plan
        .state
        .instruments
        .iter()
        .map(|binding| binding.symbol.as_str())
        .chain(requested.iter().map(String::as_str))
        .collect::<std::collections::BTreeSet<_>>()
        .len();
    if symbol_count > engine_types::identity::DENSE_ID_CAPACITY {
        return Err(EngineError::Boot(
            engine_types::identity::IdentityError::SymbolIdsExhausted.to_string(),
        ));
    }
    let mut market = MarketState::default();
    for binding in &plan.state.instruments {
        market.add_symbol(&binding.symbol);
    }
    let mut routing = Routing::default();
    let mut subscriptions = Vec::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let sid = StrategyId(
            u16::try_from(index)
                .map_err(|_| EngineError::Boot("strategy id capacity exhausted".into()))?,
        );
        for sub in strategy.subscriptions() {
            let symbol = market.add_symbol(&sub.symbol);
            routing.add(symbol, sub.feed, sid);
            if !subscriptions.contains(&sub) {
                subscriptions.push(sub);
            }
        }
    }
    let signal_dependencies = crate::signal_state::dependency_closure(
        &names,
        &strategies
            .iter()
            .map(|strategy| strategy.input_dependencies())
            .collect::<Vec<_>>(),
    )
    .map_err(EngineError::Boot)?;
    let restored_symbol_checkpoints = replay_strategy_checkpoints(replayed);
    for ((owner, _), checkpoint) in &restored_symbol_checkpoints {
        let strategy = strategies.get(owner.idx()).ok_or_else(|| {
            EngineError::Boot(format!(
                "checkpoint names strategy {} outside the configured table",
                owner.0
            ))
        })?;
        validate_strategy_checkpoint(strategy.as_ref(), checkpoint).map_err(|error| {
            EngineError::Boot(format!(
                "strategy {} refused its restored checkpoint: {error}",
                names[owner.idx()]
            ))
        })?;
    }
    let restored_global_before_boot = replay_strategy_global_checkpoints(replayed);
    for (owner, state) in &restored_global_before_boot {
        let strategy = strategies.get(owner.idx()).ok_or_else(|| {
            EngineError::Boot(format!(
                "global checkpoint names strategy {} outside the configured table",
                owner.0
            ))
        })?;
        if strategy.callback_enabled()
            && state
                .provenance
                .as_ref()
                .is_some_and(|provenance| !provenance.import_complete)
        {
            return Err(EngineError::Boot(format!(
                "strategy {} has an incomplete stopped-runtime import",
                names[owner.idx()]
            )));
        }
        validate_strategy_checkpoint(strategy.as_ref(), &state.checkpoint).map_err(|error| {
            EngineError::Boot(format!(
                "strategy {} refused its restored global checkpoint: {error}",
                names[owner.idx()]
            ))
        })?;
    }
    let prior_slots = crate::identities::replay_identities(replayed)
        .map_err(|error| EngineError::Boot(error.to_string()))?
        .map_or(0, |state| state.sleeves.len());
    let mut initial_global_checkpoints = std::collections::BTreeMap::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let id = StrategyId(
            u16::try_from(index)
                .map_err(|_| EngineError::Boot("strategy id capacity exhausted".into()))?,
        );
        if !strategy.callback_enabled() || restored_global_before_boot.contains_key(&id) {
            continue;
        }
        if index < prior_slots {
            if strategy.checkpoint_identity().is_some() {
                return Err(EngineError::Boot(format!("strategy {} has no whole-sleeve checkpoint in this nonempty WAL; import retired state while the engine is stopped", names[index])));
            }
            continue;
        }
        match strategy.initial_checkpoint() {
            Some(checkpoint) => {
                validate_strategy_checkpoint(strategy.as_ref(), &checkpoint).map_err(|error| {
                    EngineError::Boot(format!(
                        "strategy {} refused its initial checkpoint: {error}",
                        names[index]
                    ))
                })?;
                initial_global_checkpoints.insert(
                    id,
                    StrategyGlobalCheckpointState {
                        strategy: id,
                        checkpoint,
                        provenance: None,
                    },
                );
            }
            None if strategy.checkpoint_identity().is_some() => {
                return Err(EngineError::Boot(format!(
                    "strategy {} declares whole-sleeve state but no canonical initial checkpoint",
                    names[index]
                )))
            }
            None => {}
        }
    }
    routing.size_to(market.table.len());
    Ok(ConfiguredStrategies {
        market,
        routing,
        names,
        subscriptions,
        signal_dependencies,
        initial_global_checkpoints,
    })
}
