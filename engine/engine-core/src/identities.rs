use std::collections::{BTreeMap, BTreeSet};

use engine_types::identity::{
    IdentityError, IdentityState, InstrumentBinding, InstrumentIdentity, InstrumentKey,
    InstrumentScope, SleeveKey, DENSE_ID_CAPACITY,
};
use engine_types::{StrategyId, SymbolId, WalRecord};

pub struct IdentityPlan {
    pub state: IdentityState,
    /// Durable slot -> current config index; absent slots need passive owners.
    pub slot_configs: Vec<Option<usize>>,
    /// Current config index -> durable id, used before strategy construction.
    pub configured_ids: Vec<StrategyId>,
    pub changed: bool,
}

fn names_match(state: &IdentityState, strategies: &[String], symbols: &[String]) -> bool {
    strategies.len() <= state.sleeves.len()
        && strategies
            .iter()
            .zip(&state.sleeves)
            .all(|(name, key)| name == key.as_str())
        && symbols.len() <= state.instruments.len()
        && symbols
            .iter()
            .zip(&state.instruments)
            .all(|(name, binding)| name == &binding.symbol)
}

fn legacy_names(strategies: &[String], symbols: &[String]) -> Result<IdentityState, IdentityError> {
    let state = IdentityState {
        sleeves: strategies
            .iter()
            .cloned()
            .map(SleeveKey::new)
            .collect::<Result<_, _>>()?,
        instruments: symbols
            .iter()
            .map(|symbol| InstrumentBinding {
                symbol: symbol.clone(),
                identity: InstrumentIdentity::Unresolved,
            })
            .collect(),
        ..IdentityState::default()
    };
    state.validate()?;
    Ok(state)
}

pub fn replay_identities(replayed: &[WalRecord]) -> Result<Option<IdentityState>, IdentityError> {
    let mut registry: Option<IdentityState> = None;
    let mut names: Option<(Vec<String>, Vec<String>)> = None;
    for record in replayed {
        let candidate = match record {
            WalRecord::IdentityState { state, .. } => Some(state),
            WalRecord::SegmentBase {
                identities: Some(state),
                ..
            } => Some(state),
            WalRecord::SegmentBase {
                identities: None, ..
            } if registry.is_some() => {
                return Err(IdentityError::Invalid(
                    "rotation discarded the registered identity table".into(),
                ));
            }
            _ => None,
        };
        if let Some(candidate) = candidate {
            candidate.validate()?;
            if let Some(prior) = &registry {
                candidate.validate_extension(prior)?;
            }
            if let Some((strategies, symbols)) = &names {
                if !names_match(candidate, strategies, symbols) {
                    return Err(IdentityError::Invalid(
                        "registry reinterprets an earlier Names table".into(),
                    ));
                }
            }
            registry = Some(candidate.clone());
        }
        if let WalRecord::Names {
            strategies,
            symbols,
        }
        | WalRecord::SegmentBase {
            strategies,
            symbols,
            ..
        } = record
        {
            legacy_names(strategies, symbols)?;
            if let Some((old_strategies, old_symbols)) = &names {
                if !strategies.starts_with(old_strategies) || !symbols.starts_with(old_symbols) {
                    return Err(IdentityError::Invalid("legacy Names changed or removed an existing dense id; ownership is ambiguous".into()));
                }
            }
            if registry
                .as_ref()
                .is_some_and(|state| !names_match(state, strategies, symbols))
            {
                return Err(IdentityError::Invalid(
                    "Names uses an unregistered or reassigned dense id".into(),
                ));
            }
            names = Some((strategies.clone(), symbols.clone()));
        }
    }
    if registry.is_none() {
        if let Some((strategies, symbols)) = names {
            return legacy_names(&strategies, &symbols).map(Some);
        }
        if replayed
            .iter()
            .any(|record| !matches!(record, WalRecord::Boot { .. } | WalRecord::Note { .. }))
        {
            return Err(IdentityError::Invalid(
                "legacy WAL has state but no Names table; dense ownership cannot be inferred"
                    .into(),
            ));
        }
    }
    Ok(registry)
}

pub fn plan_identities(
    replayed: &[WalRecord],
    configured_sleeves: &[String],
    scope: Option<&InstrumentScope>,
    native_symbols: &BTreeMap<String, String>,
    requested_symbols: &[String],
) -> Result<IdentityPlan, IdentityError> {
    let prior = replay_identities(replayed)?;
    let mut state = prior.clone().unwrap_or_default();
    if let Some(scope) = scope {
        scope.validate()?;
        if state.scope.as_ref().is_some_and(|old| old != scope) {
            return Err(IdentityError::Invalid(
                "authenticated venue/environment differs from the WAL registry".into(),
            ));
        }
        state.scope = Some(scope.clone());
    }
    let mut configured = BTreeMap::new();
    for (index, name) in configured_sleeves.iter().enumerate() {
        let key = SleeveKey::new(name.clone())?;
        if configured.insert(key.clone(), index).is_some() {
            return Err(IdentityError::Invalid(format!(
                "configured sleeve key {:?} is ambiguous",
                key.as_str()
            )));
        }
        if !state.sleeves.contains(&key) {
            if state.sleeves.len() == DENSE_ID_CAPACITY {
                return Err(IdentityError::SleeveIdsExhausted);
            }
            state.sleeves.push(key);
        }
    }
    let slot_configs: Vec<_> = state
        .sleeves
        .iter()
        .map(|key| configured.get(key).copied())
        .collect();
    let mut configured_ids = vec![StrategyId(0); configured_sleeves.len()];
    for (slot, config) in slot_configs.iter().enumerate() {
        if let Some(config) = config {
            configured_ids[*config] =
                StrategyId(u16::try_from(slot).map_err(|_| IdentityError::SleeveIdsExhausted)?);
        }
    }
    if let Some(scope) = &state.scope {
        for binding in &mut state.instruments {
            if let Some(native) = native_symbols.get(&binding.symbol) {
                let key = InstrumentKey::in_scope(scope, native.clone());
                if matches!(&binding.identity, InstrumentIdentity::Resolved(old) if old != &key) {
                    return Err(IdentityError::Invalid(format!(
                        "native symbol changed for registered alias {:?}",
                        binding.symbol
                    )));
                }
                binding.identity = InstrumentIdentity::Resolved(key);
            }
        }
    }
    let mut known: BTreeSet<_> = state
        .instruments
        .iter()
        .map(|binding| binding.symbol.clone())
        .collect();
    for symbol in requested_symbols {
        if known.contains(symbol) {
            continue;
        }
        if state.instruments.len() == DENSE_ID_CAPACITY {
            return Err(IdentityError::SymbolIdsExhausted);
        }
        let identity = match &state.scope {
            Some(scope) => InstrumentIdentity::Resolved(InstrumentKey::in_scope(
                scope,
                native_symbols
                    .get(symbol)
                    .ok_or_else(|| IdentityError::UnresolvedInstrument(symbol.clone()))?
                    .clone(),
            )),
            None => InstrumentIdentity::Unresolved,
        };
        state.instruments.push(InstrumentBinding {
            symbol: symbol.clone(),
            identity,
        });
        known.insert(symbol.clone());
    }
    state.validate()?;
    if let Some(prior) = &prior {
        state.validate_extension(prior)?;
    }
    let persisted = replayed.iter().any(|record| {
        matches!(
            record,
            WalRecord::IdentityState { .. }
                | WalRecord::SegmentBase {
                    identities: Some(_),
                    ..
                }
        )
    });
    let changed = !persisted || prior.as_ref() != Some(&state);
    Ok(IdentityPlan {
        state,
        slot_configs,
        configured_ids,
        changed,
    })
}

pub(crate) fn reserve_symbol_names(
    plan: &mut IdentityPlan,
    symbols: &[String],
) -> Result<(), IdentityError> {
    for symbol in symbols {
        if plan
            .state
            .instruments
            .iter()
            .any(|binding| binding.symbol == *symbol)
        {
            continue;
        }
        if plan.state.instruments.len() == DENSE_ID_CAPACITY {
            return Err(IdentityError::SymbolIdsExhausted);
        }
        plan.state.instruments.push(InstrumentBinding {
            symbol: symbol.clone(),
            identity: InstrumentIdentity::Unresolved,
        });
        plan.changed = true;
    }
    plan.state.validate()
}

pub fn plan_instrument(
    state: &IdentityState,
    symbol: &str,
    key: InstrumentKey,
) -> Result<(IdentityState, SymbolId), IdentityError> {
    let mut next = state.clone();
    let scope = InstrumentScope {
        venue: key.venue.clone(),
        environment: key.environment.clone(),
    };
    if next.scope.as_ref().is_some_and(|old| old != &scope) {
        return Err(IdentityError::Invalid(
            "instrument admission changes venue/environment".into(),
        ));
    }
    next.scope = Some(scope);
    let index = match next
        .instruments
        .iter()
        .position(|binding| binding.symbol == symbol)
    {
        Some(index) => {
            if matches!(&next.instruments[index].identity, InstrumentIdentity::Resolved(old) if old != &key)
            {
                return Err(IdentityError::Invalid(format!(
                    "instrument admission reassigns {symbol:?}"
                )));
            }
            next.instruments[index].identity = InstrumentIdentity::Resolved(key);
            index
        }
        None => {
            if next.instruments.len() == DENSE_ID_CAPACITY {
                return Err(IdentityError::SymbolIdsExhausted);
            }
            next.instruments.push(InstrumentBinding {
                symbol: symbol.into(),
                identity: InstrumentIdentity::Resolved(key),
            });
            next.instruments.len() - 1
        }
    };
    next.validate_extension(state)?;
    Ok((
        next,
        SymbolId(u16::try_from(index).map_err(|_| IdentityError::SymbolIdsExhausted)?),
    ))
}

pub(crate) struct InactiveStrategy {
    key: SleeveKey,
    runtime: Option<engine_types::strategy_process::StrategyRuntimeState>,
    subscriptions: Option<Vec<engine_types::Subscription>>,
}

impl InactiveStrategy {
    pub(crate) fn new(
        key: SleeveKey,
        committed: Option<&engine_types::strategy_process::StrategyProcessState>,
    ) -> Self {
        Self {
            key,
            runtime: committed.map(|state| state.runtime.clone()),
            subscriptions: committed.and_then(|state| state.retained_signal_subscriptions.clone()),
        }
    }
}

impl engine_types::Strategy for InactiveStrategy {
    fn name(&self) -> &str {
        self.key.as_str()
    }
    fn callback_enabled(&self) -> bool {
        false
    }
    fn subscriptions(&self) -> Vec<engine_types::Subscription> {
        Vec::new()
    }
    fn runtime_state(
        &self,
    ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String> {
        Ok(self.runtime.clone())
    }
    fn retained_signal_subscriptions(&self) -> Option<Vec<engine_types::Subscription>> {
        self.subscriptions.clone()
    }
}

#[cfg(test)]
#[path = "identities_tests.rs"]
mod tests;
