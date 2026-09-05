use super::*;

pub(super) struct RecoveredStrategyInputs {
    pub(super) strategy_checkpoints:
        std::collections::BTreeMap<(StrategyId, SymbolId), StrategyCheckpoint>,
    pub(super) strategy_global_checkpoints:
        std::collections::BTreeMap<StrategyId, StrategyGlobalCheckpointState>,
    pub(super) strategy_events: std::collections::BTreeMap<(StrategyId, String), StrategyEvent>,
    pub(super) signals: crate::signal_state::SignalState,
    pub(super) runtime_control_requests: Vec<RuntimeControlRequest>,
    pub(super) runtime_control_consumed: std::collections::BTreeSet<(StrategyId, String)>,
    pub(super) runtime_entries_enabled: std::collections::BTreeMap<StrategyId, bool>,
    pub(super) routes: Vec<(SymbolId, StrategyId, Subscription)>,
}

pub(super) fn restore_strategy_inputs(
    effective: &[WalRecord],
    strategies: &[Box<dyn Strategy>],
    names: &[String],
    table: &SymbolTable,
    initial_global_checkpoints: std::collections::BTreeMap<
        StrategyId,
        StrategyGlobalCheckpointState,
    >,
) -> Result<RecoveredStrategyInputs, EngineError> {
    let mut strategy_checkpoints = replay_strategy_checkpoints(effective);
    strategy_checkpoints.retain(|(strategy, symbol), _| {
        strategy.idx() < strategies.len() && symbol.idx() < table.len()
    });
    let mut strategy_global_checkpoints = replay_strategy_global_checkpoints(effective);
    strategy_global_checkpoints.extend(initial_global_checkpoints);
    strategy_global_checkpoints.retain(|strategy, _| strategy.idx() < strategies.len());
    let mut strategy_events = replay_strategy_events(effective);
    strategy_events.retain(|_, event| {
        (event.source.0 as usize) < strategies.len()
            && (event.destination.0 as usize) < strategies.len()
    });
    let mut signals = crate::signal_state::SignalState::replay(effective, strategies.len())
        .map_err(EngineError::Boot)?;
    signals.require_readiness(
        strategies
            .iter()
            .enumerate()
            .filter(|(_, strategy)| strategy.requires_signal_readiness())
            .map(|(index, _)| StrategyId(index as u16)),
    );
    let ReplayedRuntimeControlState {
        requests: runtime_control_requests,
        consumed: runtime_control_consumed,
        entries_enabled: runtime_entries_enabled,
    } = replay_runtime_control_state(effective)?;
    for request in &runtime_control_requests {
        crate::controls::validate(request).map_err(EngineError::Boot)?;
        let expected_name = names.get(request.strategy.0 as usize).ok_or_else(|| {
            EngineError::Boot(format!(
                "runtime control request {:?} names strategy {} outside the configured table",
                request.request_id, request.strategy.0
            ))
        })?;
        if expected_name != &request.strategy_name {
            return Err(EngineError::Boot(format!(
                "runtime control request {:?} binds strategy {} to {:?}, expected {:?}",
                request.request_id, request.strategy.0, request.strategy_name, expected_name
            )));
        }
    }
    let mut routes = Vec::new();
    for row in signals.subscriptions() {
        if row.subscriptions.len() > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS {
            return Err(EngineError::Boot(format!(
                "durable signal source {} has {} subscriptions; maximum is {}",
                row.source,
                row.subscriptions.len(),
                engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
            )));
        }
        for subscription in &row.subscriptions {
            let Some(symbol) = table.get(&subscription.symbol) else {
                return Err(EngineError::Boot(format!(
                    "durable signal source {} names {} outside the restored symbol table",
                    row.source, subscription.symbol
                )));
            };
            routes.push((symbol, row.destination, subscription.clone()));
        }
    }
    Ok(RecoveredStrategyInputs {
        strategy_checkpoints,
        strategy_global_checkpoints,
        strategy_events,
        signals,
        runtime_control_requests,
        runtime_control_consumed,
        runtime_entries_enabled,
        routes,
    })
}
