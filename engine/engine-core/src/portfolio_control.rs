use engine_types::portfolio_control::*;
use engine_types::{StrategyId, SymbolId, WalRecord};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Default)]
pub(crate) struct PortfolioControls {
    next_id: u64,
    retry_after: BTreeMap<u64, std::time::Instant>,
    pub native_pending: BTreeMap<SymbolId, engine_types::numeric::Exact>,
    pub exits: BTreeMap<(StrategyId, SymbolId), PortfolioExit>,
    pub emergencies: BTreeMap<SymbolId, PortfolioEmergency>,
}

impl PortfolioControls {
    pub fn replay(records: &[WalRecord]) -> Result<Self, String> {
        let mut book = Self {
            next_id: 1,
            ..Default::default()
        };
        let mut orders = crate::inflight::LedgerOfOrders::default();
        for record in records {
            book.apply(record)?;
            orders.try_apply(record)?;
            book.retire_completed_orders(&orders);
        }
        book.retain_native_offsets(
            &crate::attribution::Attribution::try_from_records(records)?.snapshot(),
        );
        Ok(book)
    }
    pub(crate) fn retire_completed_orders(&mut self, orders: &crate::inflight::LedgerOfOrders) {
        let completed = |id: &Option<String>| {
            id.as_ref()
                .and_then(|id| orders.orders.get(id))
                .is_some_and(|order| !order.in_flight())
        };
        for exit in self.exits.values_mut() {
            if completed(&exit.order_id) {
                exit.order_id = None;
            }
        }
        for emergency in self.emergencies.values_mut() {
            if completed(&emergency.order_id) {
                emergency.order_id = None;
            }
        }
    }
    pub fn retain_native_offsets(&mut self, portfolio: &engine_types::portfolio::PortfolioState) {
        self.native_pending.retain(|symbol, _| {
            let rows = || {
                portfolio
                    .positions
                    .iter()
                    .filter(|row| row.symbol == *symbol)
            };
            rows().any(|row| row.signed_qty.is_positive())
                && rows().any(|row| row.signed_qty.is_negative())
        });
    }
    pub fn retry_ready(&self, id: u64) -> bool {
        self.retry_after
            .get(&id)
            .is_none_or(|deadline| std::time::Instant::now() >= *deadline)
    }
    pub fn attempted(&mut self, id: u64, attempt: u32) {
        let delay_ms = 250_u64
            .saturating_mul(1_u64 << attempt.saturating_sub(1).min(7))
            .min(30_000);
        self.retry_after.insert(
            id,
            std::time::Instant::now() + std::time::Duration::from_millis(delay_ms),
        );
    }
    pub fn next_id(&self) -> Result<u64, String> {
        if self.next_id == 0 || self.next_id == u64::MAX {
            return Err("portfolio control ID capacity exhausted".into());
        }
        Ok(self.next_id)
    }
    pub fn snapshot(&self) -> PortfolioControlState {
        PortfolioControlState {
            schema_version: 1,
            next_id: self.next_id,
            native_pending: self
                .native_pending
                .iter()
                .map(|(symbol, price)| (*symbol, price.clone()))
                .collect(),
            exits: self.exits.values().cloned().collect(),
            emergencies: self.emergencies.values().cloned().collect(),
        }
    }
    pub fn blocked(&self, strategy: StrategyId, symbol: SymbolId) -> bool {
        self.exits.contains_key(&(strategy, symbol)) || self.emergencies.contains_key(&symbol)
    }
    pub fn validate_engine_order(
        &self,
        request: &engine_types::OrderRequest,
    ) -> Result<(), String> {
        let Some(engine_types::orders::SleeveOrderEffect::EmergencyNetReduction { emergency_id }) =
            request.sleeve_effect
        else {
            return Ok(());
        };
        let owned = self.emergencies.get(&request.symbol).is_some_and(|state| {
            state.id == emergency_id
                && state.phase == PortfolioEmergencyPhase::CloseNet
                && state.order_id.as_deref() == Some(request.client_order_id.as_str())
        });
        if !owned
            || !request.reduce_only
            || request.stop.is_some()
            || !matches!(request.kind, engine_types::OrderKind::Market)
        {
            return Err("engine net order has no matching durable emergency owner".into());
        }
        let terms = request
            .exact_terms
            .as_ref()
            .ok_or("engine net order has no exact quantity")?;
        terms
            .validate_projection(request)
            .map_err(|e| e.to_string())?;
        if terms.stop_trigger_price.is_some()
            || terms.physical_stop_trigger_price.is_some()
            || !terms.quantity.is_positive()
        {
            return Err("engine net order is not an unprotected physical reduction".into());
        }
        Ok(())
    }
    pub fn apply(&mut self, record: &WalRecord) -> Result<(), String> {
        match record {
            WalRecord::SegmentBase {
                portfolio_control: state,
                open_orders,
                ..
            } => {
                if state.schema_version != 1 || state.next_id == 0 {
                    return Err("invalid portfolio control snapshot".into());
                }
                let mut next = Self {
                    next_id: 1,
                    ..Default::default()
                };
                let mut ids = BTreeSet::new();
                for exit in &state.exits {
                    validate_exit(exit)?;
                    if exit.id >= state.next_id
                        || !ids.insert(exit.id)
                        || next
                            .exits
                            .insert((exit.strategy, exit.symbol), exit.clone())
                            .is_some()
                    {
                        return Err("duplicate or unallocated portfolio exit in rotation".into());
                    }
                }
                for emergency in &state.emergencies {
                    validate_emergency(emergency)?;
                    if emergency.id >= state.next_id
                        || !ids.insert(emergency.id)
                        || next
                            .emergencies
                            .insert(emergency.symbol, emergency.clone())
                            .is_some()
                    {
                        return Err(
                            "duplicate or unallocated portfolio emergency in rotation".into()
                        );
                    }
                }
                for (symbol, price) in &state.native_pending {
                    price.validate_storage().map_err(|e| e.to_string())?;
                    if !price.is_positive()
                        || next.native_pending.insert(*symbol, price.clone()).is_some()
                    {
                        return Err("invalid native-close obligation in rotation".into());
                    }
                }
                for order in open_orders {
                    if order.terminal.is_none() {
                        next.validate_engine_order(&order.request)?;
                    } else if let Some(
                        engine_types::orders::SleeveOrderEffect::EmergencyNetReduction {
                            emergency_id,
                        },
                    ) = order.request.sleeve_effect
                    {
                        let prefix = format!("eng-pe-{emergency_id}-");
                        if emergency_id == 0
                            || emergency_id >= state.next_id
                            || !(order
                                .request
                                .client_order_id
                                .strip_prefix(&prefix)
                                .is_some_and(|attempt| {
                                    attempt.parse::<u32>().is_ok_and(|attempt| attempt > 0)
                                })
                                || numeric_engine_order_id(&order.request.client_order_id))
                            || !order.request.reduce_only
                            || order.request.stop.is_some()
                            || !matches!(order.request.kind, engine_types::OrderKind::Market)
                        {
                            return Err(
                                "terminal engine net order has invalid durable lineage".into()
                            );
                        }
                        order
                            .request
                            .exact_terms
                            .as_ref()
                            .ok_or("terminal engine net order has no exact terms")?
                            .validate_projection(&order.request)
                            .map_err(|error| error.to_string())?;
                    }
                }
                next.next_id = state.next_id;
                *self = next;
            }
            WalRecord::OrderSent { request, .. } => self.validate_engine_order(request)?,
            WalRecord::PortfolioExitChanged { state } => {
                validate_exit(state)?;
                let key = (state.strategy, state.symbol);
                if let Some(old) = self.exits.get(&key) {
                    if state.id != old.id
                        || state.trigger_price != old.trigger_price
                        || state.position_side != old.position_side
                        || state.target_remaining > old.target_remaining
                        || state.started_ms != old.started_ms
                        || state.attempt < old.attempt
                    {
                        return Err("portfolio exit changed its identity or moved backward".into());
                    }
                } else {
                    self.allocate(state.id)?;
                }
                self.exits.insert(key, state.clone());
            }
            WalRecord::PortfolioExitCompleted {
                id,
                strategy,
                symbol,
            } => {
                if self.exits.get(&(*strategy, *symbol)).map(|state| state.id) != Some(*id) {
                    return Err("portfolio exit completion has no active owner".into());
                }
                self.exits.remove(&(*strategy, *symbol));
                self.retry_after.remove(id);
            }
            WalRecord::PortfolioEmergencyChanged { state } => {
                validate_emergency(state)?;
                if let Some(old) = self.emergencies.get(&state.symbol) {
                    if state.id != old.id
                        || state.reference_price != old.reference_price
                        || state.reason != old.reason
                        || state.started_ms != old.started_ms
                        || state.attempt < old.attempt
                    {
                        return Err(
                            "portfolio emergency changed its identity or moved backward".into()
                        );
                    }
                    let legal = old.phase == state.phase
                        || matches!(
                            (old.phase, state.phase),
                            (
                                PortfolioEmergencyPhase::ResolveOrders,
                                PortfolioEmergencyPhase::CloseNet
                            ) | (
                                PortfolioEmergencyPhase::CloseNet,
                                PortfolioEmergencyPhase::SettleOffsets
                            ) | (
                                PortfolioEmergencyPhase::SettleOffsets,
                                PortfolioEmergencyPhase::ResolveOrders
                            )
                        );
                    if !legal {
                        return Err("portfolio emergency skipped order resolution".into());
                    }
                } else {
                    if state.phase != PortfolioEmergencyPhase::ResolveOrders {
                        return Err("portfolio emergency must start by resolving orders".into());
                    }
                    self.allocate(state.id)?;
                }
                self.native_pending.remove(&state.symbol);
                self.emergencies.insert(state.symbol, state.clone());
            }
            WalRecord::PortfolioEmergencyCompleted { id, symbol } => {
                if self.emergencies.get(symbol).map(|state| state.id) != Some(*id) {
                    return Err("portfolio emergency completion has no active owner".into());
                }
                self.emergencies.remove(symbol);
                self.retry_after.remove(id);
            }
            WalRecord::PortfolioOffsetSettled { settlement } => {
                let Some(emergency) = self.emergencies.get(&settlement.symbol) else {
                    return Err("internal settlement has no active emergency".into());
                };
                if emergency.id != settlement.emergency_id
                    || emergency.phase != PortfolioEmergencyPhase::SettleOffsets
                    || settlement.price != emergency.reference_price
                {
                    return Err("internal settlement overtook physical close".into());
                }
                validate_settlement(settlement)?;
            }
            WalRecord::OrderUpdate {
                update:
                    engine_types::OrderUpdate::Fill {
                        symbol,
                        px,
                        amounts,
                        allocation: Some(allocation),
                        ..
                    },
                ..
            } if allocation.policy
                == engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo =>
            {
                let price = amounts
                    .as_ref()
                    .map(|a| Ok(a.price.value.clone()))
                    .unwrap_or_else(|| engine_types::numeric::Exact::from_legacy_f64(*px))
                    .map_err(|e| e.to_string())?;
                if !self.emergencies.contains_key(symbol) {
                    self.native_pending.entry(*symbol).or_insert(price);
                }
            }
            WalRecord::RecoveredFill {
                symbol,
                px,
                amounts,
                allocation: Some(allocation),
                ..
            } if allocation.policy
                == engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo =>
            {
                let price = amounts
                    .as_ref()
                    .map(|a| Ok(a.price.value.clone()))
                    .unwrap_or_else(|| engine_types::numeric::Exact::from_legacy_f64(*px))
                    .map_err(|e| e.to_string())?;
                if !self.emergencies.contains_key(symbol) {
                    self.native_pending.entry(*symbol).or_insert(price);
                }
            }
            _ => (),
        }
        Ok(())
    }
    fn allocate(&mut self, id: u64) -> Result<(), String> {
        if id != self.next_id()? {
            return Err("portfolio control ID is not the next durable identity".into());
        }
        self.next_id += 1;
        Ok(())
    }
}

fn validate_order_id(id: &Option<String>, attempt: u32) -> Result<(), String> {
    if id
        .as_ref()
        .is_some_and(|id| id.is_empty() || id.len() > 128 || attempt == 0)
    {
        return Err("invalid portfolio order attempt".into());
    }
    Ok(())
}
fn validate_exit(state: &PortfolioExit) -> Result<(), String> {
    if state.id == 0
        || state.target_remaining.is_negative()
        || state
            .trigger_price
            .as_ref()
            .is_some_and(|price| !price.is_positive())
    {
        return Err("invalid portfolio exit identity or trigger".into());
    }
    state
        .target_remaining
        .validate_storage()
        .map_err(|e| e.to_string())?;
    if let Some(price) = &state.trigger_price {
        price.validate_storage().map_err(|e| e.to_string())?;
    }
    validate_order_id(&state.order_id, state.attempt)
}
fn validate_emergency(state: &PortfolioEmergency) -> Result<(), String> {
    if state.id == 0 || !state.reference_price.is_positive() {
        return Err("invalid portfolio emergency identity or reference".into());
    }
    state
        .reference_price
        .validate_storage()
        .map_err(|e| e.to_string())?;
    validate_order_id(&state.order_id, state.attempt)
}
pub(crate) fn validate_settlement(settlement: &PortfolioOffsetSettlement) -> Result<(), String> {
    if settlement.emergency_id == 0
        || !settlement.price.is_positive()
        || settlement.slices.len() < 2
    {
        return Err("invalid internal settlement".into());
    }
    settlement
        .price
        .validate_storage()
        .map_err(|e| e.to_string())?;
    let mut by_asset = BTreeMap::<_, engine_types::numeric::Exact>::new();
    let mut owners = BTreeSet::new();
    for slice in &settlement.slices {
        slice
            .signed_quantity
            .validate_storage()
            .map_err(|e| e.to_string())?;
        if slice.signed_quantity.is_zero() || !owners.insert(slice.strategy) {
            return Err("invalid internal settlement ownership".into());
        }
        *by_asset.entry(slice.settlement_asset.clone()).or_default() += &slice.signed_quantity;
    }
    if by_asset.values().any(|net| !net.is_zero()) {
        return Err("internal settlement is not balanced in each asset".into());
    }
    Ok(())
}

fn numeric_engine_order_id(id: &str) -> bool {
    let mut parts = id.split('-');
    parts.next() == Some("eng")
        && parts
            .next()
            .is_some_and(|epoch| epoch.parse::<u64>().is_ok())
        && parts.next().is_some_and(|counter| {
            counter
                .parse::<u32>()
                .is_ok_and(|counter| counter > 0 && counter < (1 << 18))
        })
        && parts.next().is_none()
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::numeric::{AssetId, Exact};
    fn emergency() -> PortfolioEmergency {
        PortfolioEmergency {
            id: 1,
            symbol: SymbolId(0),
            reference_price: Exact::parse_decimal("99.1").unwrap(),
            reason: PortfolioEmergencyReason::NativeClose,
            phase: PortfolioEmergencyPhase::ResolveOrders,
            started_ms: 1,
            attempt: 0,
            order_id: None,
        }
    }
    #[test]
    fn an_engine_net_order_requires_its_durable_emergency_owner() {
        let mut state = emergency();
        let mut book = PortfolioControls::replay(&[]).unwrap();
        let mut request = engine_types::OrderRequest {
            client_order_id: "eng-pe-1-1".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: engine_types::Side::Sell,
            qty: 1.0,
            kind: engine_types::OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: false,
            exact_terms: None,
            sleeve_effect: Some(
                engine_types::orders::SleeveOrderEffect::EmergencyNetReduction { emergency_id: 1 },
            ),
        };
        engine_types::order_terms::ExactOrderTerms {
            quantity: Exact::parse_decimal("1").unwrap(),
            limit_price: None,
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: engine_types::order_terms::OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(&mut request)
        .unwrap();
        let sent = |request| WalRecord::OrderSent {
            request,
            dispatch: None,
            wire_ns: 1,
            arrival_mid: 100.0,
        };
        assert!(
            book.apply(&sent(request.clone())).is_err(),
            "a parent cannot bypass sleeve ownership without an active emergency"
        );
        book.apply(&WalRecord::PortfolioEmergencyChanged {
            state: state.clone(),
        })
        .unwrap();
        state.phase = PortfolioEmergencyPhase::CloseNet;
        state.attempt = 1;
        state.order_id = Some(request.client_order_id.clone());
        book.apply(&WalRecord::PortfolioEmergencyChanged { state })
            .unwrap();
        book.apply(&sent(request.clone())).unwrap();
        request.reduce_only = false;
        assert!(
            book.apply(&sent(request.clone())).is_err(),
            "an engine-owned close must never become physical growth"
        );
        request.reduce_only = true;
        request.client_order_id = "another-parent".into();
        assert!(
            book.apply(&sent(request)).is_err(),
            "the emergency has one durable parent attempt"
        );
    }

    #[test]
    fn settlement_reference_cannot_change_after_the_observed_emergency() {
        let mut book = PortfolioControls::replay(&[]).unwrap();
        let state = emergency();
        book.apply(&WalRecord::PortfolioEmergencyChanged {
            state: state.clone(),
        })
        .unwrap();
        let before = book.snapshot();
        let mut changed = state;
        changed.reference_price = Exact::parse_decimal("101").unwrap();
        assert!(
            book.apply(&WalRecord::PortfolioEmergencyChanged { state: changed })
                .is_err(),
            "replay accepted a different internal settlement mark"
        );
        assert_eq!(book.snapshot(), before);
    }
    #[test]
    fn settlement_uses_the_exact_frozen_emergency_mark() {
        let mut book = PortfolioControls::replay(&[]).unwrap();
        let mut state = emergency();
        for phase in [
            PortfolioEmergencyPhase::ResolveOrders,
            PortfolioEmergencyPhase::CloseNet,
            PortfolioEmergencyPhase::SettleOffsets,
        ] {
            state.phase = phase;
            book.apply(&WalRecord::PortfolioEmergencyChanged {
                state: state.clone(),
            })
            .unwrap();
        }
        let mut settlement = PortfolioOffsetSettlement {
            emergency_id: 1,
            symbol: SymbolId(0),
            price: Exact::parse_decimal("99.100000000000001").unwrap(),
            settled_ms: 5,
            slices: vec![
                PortfolioOffsetSlice {
                    strategy: StrategyId(0),
                    signed_quantity: Exact::parse_decimal("-1").unwrap(),
                    settlement_asset: AssetId::Unknown,
                },
                PortfolioOffsetSlice {
                    strategy: StrategyId(1),
                    signed_quantity: Exact::parse_decimal("1").unwrap(),
                    settlement_asset: AssetId::Unknown,
                },
            ],
        };
        assert_eq!(
            settlement.price.to_f64().unwrap(),
            state.reference_price.to_f64().unwrap()
        );
        assert!(
            book.apply(&WalRecord::PortfolioOffsetSettled {
                settlement: settlement.clone()
            })
            .is_err(),
            "binary64 equality must not substitute for the frozen exact mark"
        );
        settlement.price = state.reference_price;
        book.apply(&WalRecord::PortfolioOffsetSettled { settlement })
            .unwrap();
    }
}
