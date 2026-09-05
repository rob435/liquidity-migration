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
    let mut market = MarketState::default();
    // Ids are interning positions, so the previous run's table is
    // re-interned first, in its own order: every id the replayed records
    // name then means the same symbol in this run. Attribution, the
    // reconcile's exposure accounting, and in-flight recovery all join
    // the OLD run's numbers against this table — a symbol a signal
    // admitted at runtime last run would otherwise come back at a
    // different position, or not at all. `assembly::symbol_order` seeds
    // the gateway and the private stream with this same order.
    for name in crate::replay::LogNames::of_log(replayed).symbols {
        market.add_symbol(&name);
    }
    let mut routing = Routing::default();
    let mut names = Vec::with_capacity(strategies.len());
    let mut subscriptions = Vec::new();
    for (index, strategy) in strategies.iter().enumerate() {
        let sid = StrategyId(
            u16::try_from(index)
                .map_err(|_| EngineError::Boot("more than 65535 strategies".to_string()))?,
        );
        names.push(match sleeves.get(index) {
            Some(sleeve) if !sleeve.is_empty() => sleeve.clone(),
            _ => strategy.name().to_string(),
        });
        for sub in strategy.subscriptions() {
            let symbol = market.add_symbol(&sub.symbol);
            routing.add(symbol, sub.feed, sid);
            if !subscriptions.contains(&sub) {
                subscriptions.push(sub.clone());
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
    let prior_names = crate::replay::LogNames::of_log(replayed).strategies;
    if !sleeves.is_empty()
        && !prior_names.is_empty()
        && !names.as_slice().starts_with(prior_names.as_slice())
    {
        return Err(EngineError::Boot(format!(
            "configured strategy identity/order {:?} does not preserve the WAL prefix {:?}",
            names, prior_names
        )));
    }
    let mut distinct = std::collections::HashSet::new();
    if !sleeves.is_empty()
        && names
            .iter()
            .any(|name| name.is_empty() || !distinct.insert(name))
    {
        return Err(EngineError::Boot(
            "strategy sleeve names must be non-empty and unique".to_string(),
        ));
    }
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
        if state
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
    let mut initial_global_checkpoints = std::collections::BTreeMap::new();
    if replayed.is_empty() {
        for (index, strategy) in strategies.iter().enumerate() {
            let id = StrategyId(
                u16::try_from(index)
                    .map_err(|_| EngineError::Boot("more than 65535 strategies".to_string()))?,
            );
            match strategy.initial_checkpoint() {
                Some(checkpoint) => {
                    validate_strategy_checkpoint(strategy.as_ref(), &checkpoint).map_err(
                        |error| {
                            EngineError::Boot(format!(
                                "strategy {} refused its initial checkpoint: {error}",
                                names[index]
                            ))
                        },
                    )?;
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
                    )));
                }
                None => {}
            }
        }
    } else {
        for (index, strategy) in strategies.iter().enumerate() {
            if strategy.checkpoint_identity().is_some()
                && !restored_global_before_boot
                    .contains_key(&StrategyId(u16::try_from(index).map_err(|_| {
                        EngineError::Boot("more than 65535 strategies".to_string())
                    })?))
            {
                return Err(EngineError::Boot(format!(
                    "strategy {} has no whole-sleeve checkpoint in this nonempty WAL; import retired state while the engine is stopped",
                    names[index]
                )));
            }
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
