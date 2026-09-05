use std::collections::BTreeMap;

use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio::{PortfolioPosition, PortfolioState};
use engine_types::{Side, StrategyId, SymbolId};

#[derive(Clone, Debug, Default)]
pub(crate) struct Inventory {
    positions: BTreeMap<(StrategyId, SymbolId), PortfolioPosition>,
}

pub(crate) struct InventoryFill {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: Exact,
    pub px: Option<Exact>,
    pub stop: Option<Exact>,
    pub settlement_asset: AssetId,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct RealizedValue {
    pub amount: Option<Exact>,
    pub asset: AssetId,
}

#[derive(Debug)]
pub(crate) struct InventoryChange {
    key: (StrategyId, SymbolId),
    prior: Option<PortfolioPosition>,
    next: Option<PortfolioPosition>,
    prior_net: Exact,
    prior_gross: Exact,
    pub realized: RealizedValue,
}

#[derive(Debug)]
pub(crate) struct InventoryBatchChange {
    prior: BTreeMap<(StrategyId, SymbolId), PortfolioPosition>,
    next: Inventory,
    pub realized: Vec<(StrategyId, SymbolId, RealizedValue)>,
}

impl Inventory {
    pub(crate) fn restore(state: &PortfolioState) -> Result<Self, String> {
        if !matches!(state.schema_version, 1 | 2) {
            return Err("unsupported portfolio inventory schema".into());
        }
        let mut positions = BTreeMap::new();
        let mut totals = BTreeMap::<SymbolId, (Exact, Exact)>::new();
        for row in &state.positions {
            validate_position(row)?;
            if positions
                .insert((row.strategy, row.symbol), row.clone())
                .is_some()
            {
                return Err("duplicate portfolio inventory owner".into());
            }
            let (net, gross) = totals.entry(row.symbol).or_default();
            *net += &row.signed_qty;
            *gross += row.signed_qty.abs();
        }
        for (net, gross) in totals.values() {
            validate_quantity(net)?;
            validate_quantity(gross)?;
        }
        Ok(Self { positions })
    }

    pub(crate) fn snapshot(&self) -> PortfolioState {
        PortfolioState {
            schema_version: 2,
            positions: self.positions.values().cloned().collect(),
            accounting_complete_from_start: false,
            ..Default::default()
        }
    }
    pub(crate) fn rows(&self) -> impl Iterator<Item = &PortfolioPosition> {
        self.positions.values()
    }
    pub(crate) fn restate_legacy(
        &mut self,
        rows: &[engine_types::FilledTotal],
    ) -> Result<(), String> {
        let mut positions = Vec::with_capacity(rows.len());
        for row in rows {
            let signed_qty = Exact::from_legacy_f64(row.signed_qty).map_err(|e| e.to_string())?;
            if signed_qty.is_zero() {
                continue;
            }
            positions.push(PortfolioPosition {
                strategy: row.strategy,
                symbol: row.symbol,
                signed_qty,
                entry_value: None,
                stop_px: None,
                settlement_asset: AssetId::Unknown,
            });
        }
        let next = Self::restore(&PortfolioState {
            schema_version: 1,
            positions,
            ..Default::default()
        })?;
        *self = next;
        Ok(())
    }
    pub(crate) fn position(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
    ) -> Option<&PortfolioPosition> {
        self.positions.get(&(strategy, symbol))
    }
    pub(crate) fn remove(&mut self, strategy: StrategyId, symbol: SymbolId) {
        self.positions.remove(&(strategy, symbol));
    }
    pub(crate) fn tighten_stop(
        &mut self,
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        stop: Exact,
    ) {
        if !stop.is_positive() {
            return;
        }
        let Some(position) = self.positions.get_mut(&(strategy, symbol)) else {
            return;
        };
        if position.signed_qty.is_positive() != (side == Side::Buy) {
            return;
        }
        position.stop_px = Some(match position.stop_px.take() {
            Some(prior) if side == Side::Buy => prior.max(stop),
            Some(prior) => prior.min(stop),
            None => stop,
        });
    }
    pub(crate) fn net(&self, symbol: SymbolId) -> Exact {
        self.positions
            .values()
            .filter(|row| row.symbol == symbol)
            .fold(Exact::zero(), |qty, row| qty + &row.signed_qty)
    }
    pub(crate) fn gross(&self, symbol: SymbolId) -> Exact {
        self.positions
            .values()
            .filter(|row| row.symbol == symbol)
            .fold(Exact::zero(), |qty, row| qty + row.signed_qty.abs())
    }

    pub(crate) fn prepare_batch(
        &self,
        fills: Vec<InventoryFill>,
    ) -> Result<InventoryBatchChange, String> {
        let mut next = self.clone();
        let mut realized = Vec::with_capacity(fills.len());
        for fill in fills {
            let (strategy, symbol) = (fill.strategy, fill.symbol);
            realized.push((strategy, symbol, next.fill(fill)?));
        }
        Ok(InventoryBatchChange {
            prior: self.positions.clone(),
            next,
            realized,
        })
    }

    pub(crate) fn validate_batch(&self, change: &InventoryBatchChange) -> Result<(), String> {
        if self.positions != change.prior {
            return Err("stale prepared inventory batch".into());
        }
        Ok(())
    }

    pub(crate) fn apply_batch(
        &mut self,
        change: InventoryBatchChange,
    ) -> Result<Vec<(StrategyId, SymbolId, RealizedValue)>, String> {
        self.validate_batch(&change)?;
        *self = change.next;
        Ok(change.realized)
    }

    pub(crate) fn prepare_fill(&self, fill: InventoryFill) -> Result<InventoryChange, String> {
        let InventoryFill {
            strategy,
            symbol,
            side,
            qty,
            px,
            stop,
            settlement_asset,
        } = fill;
        if !qty.is_positive()
            || px.as_ref().is_some_and(|p| !p.is_positive())
            || stop.as_ref().is_some_and(|p| !p.is_positive())
        {
            return Err("invalid portfolio execution quantity or price".into());
        }
        validate_quantity(&qty)?;
        for value in px.iter().chain(stop.iter()) {
            value
                .to_f64()
                .map_err(|e| format!("portfolio execution price: {e}"))?;
            value.validate_storage().map_err(|e| e.to_string())?;
        }
        let key = (strategy, symbol);
        let prior = self.positions.get(&key).cloned();
        let prior_net = self.net(symbol);
        let prior_gross = self.gross(symbol);
        let delta = if side == Side::Buy {
            qty.clone()
        } else {
            -&qty
        };
        let (next, realized) = if let Some(held) = &prior {
            if matches!((&held.settlement_asset, &settlement_asset), (AssetId::Named(old), AssetId::Named(new)) if old != new)
            {
                return Err("position settlement asset changed while held".into());
            }
            let joined_asset = if held.settlement_asset == settlement_asset {
                settlement_asset.clone()
            } else {
                AssetId::Unknown
            };
            let prior_qty = held.signed_qty.abs();
            let signed_qty = &held.signed_qty + &delta;
            let grows = held.signed_qty.signum() == delta.signum();
            let (entry_value, stop_px, asset, amount) = if grows {
                let value = held
                    .entry_value
                    .as_ref()
                    .zip(px.as_ref())
                    .map(|(value, price)| value + &qty * price);
                let stop = match (held.stop_px.as_ref(), stop) {
                    (Some(old), Some(new)) if side == Side::Buy => Some(old.clone().max(new)),
                    (Some(old), Some(new)) => Some(old.clone().min(new)),
                    (Some(old), None) => Some(old.clone()),
                    (None, value) => value,
                };
                (value, stop, joined_asset.clone(), Some(Exact::zero()))
            } else {
                let closed = qty.clone().min(prior_qty.clone());
                let closed_cost = held.entry_value.as_ref().map(|value| {
                    (value * &closed)
                        .checked_div(&prior_qty)
                        .expect("nonzero held quantity")
                });
                let amount = closed_cost.as_ref().zip(px.as_ref()).map(|(cost, price)| {
                    let value = &closed * price - cost;
                    if held.signed_qty.is_positive() {
                        value
                    } else {
                        -value
                    }
                });
                if qty < prior_qty {
                    let value = held
                        .entry_value
                        .as_ref()
                        .zip(closed_cost.as_ref())
                        .map(|(value, closed)| value - closed);
                    (
                        value,
                        held.stop_px.clone(),
                        held.settlement_asset.clone(),
                        amount,
                    )
                } else if qty == prior_qty {
                    (
                        Some(Exact::zero()),
                        None,
                        held.settlement_asset.clone(),
                        amount,
                    )
                } else {
                    let value = px.as_ref().map(|price| (&qty - &prior_qty) * price);
                    (value, stop, settlement_asset.clone(), amount)
                }
            };
            (
                if signed_qty.is_zero() {
                    None
                } else {
                    Some(PortfolioPosition {
                        strategy,
                        symbol,
                        signed_qty,
                        entry_value,
                        stop_px,
                        settlement_asset: asset,
                    })
                },
                RealizedValue {
                    amount,
                    asset: joined_asset,
                },
            )
        } else {
            (
                Some(PortfolioPosition {
                    strategy,
                    symbol,
                    signed_qty: delta,
                    entry_value: px.as_ref().map(|price| &qty * price),
                    stop_px: stop,
                    settlement_asset: settlement_asset.clone(),
                }),
                RealizedValue {
                    amount: Some(Exact::zero()),
                    asset: settlement_asset,
                },
            )
        };
        if let Some(position) = &next {
            validate_position(position)?;
        }
        let old_qty = prior
            .as_ref()
            .map(|row| row.signed_qty.clone())
            .unwrap_or_default();
        let new_qty = next
            .as_ref()
            .map(|row| row.signed_qty.clone())
            .unwrap_or_default();
        validate_quantity(&(&prior_net - &old_qty + &new_qty))?;
        validate_quantity(&(&prior_gross - old_qty.abs() + new_qty.abs()))?;
        if let Some(value) = &realized.amount {
            value.validate_storage().map_err(|e| e.to_string())?;
        }
        Ok(InventoryChange {
            key,
            prior,
            next,
            prior_net,
            prior_gross,
            realized,
        })
    }

    pub(crate) fn validate_change(&self, change: &InventoryChange) -> Result<(), String> {
        if self.positions.get(&change.key) != change.prior.as_ref()
            || self.net(change.key.1) != change.prior_net
            || self.gross(change.key.1) != change.prior_gross
        {
            return Err("stale prepared inventory change".into());
        }
        Ok(())
    }

    pub(crate) fn apply(&mut self, change: InventoryChange) -> Result<RealizedValue, String> {
        self.validate_change(&change)?;
        match change.next {
            Some(position) => {
                self.positions.insert(change.key, position);
            }
            None => {
                self.positions.remove(&change.key);
            }
        }
        Ok(change.realized)
    }
    pub(crate) fn fill(&mut self, fill: InventoryFill) -> Result<RealizedValue, String> {
        let change = self.prepare_fill(fill)?;
        self.apply(change)
    }
}

fn validate_quantity(qty: &Exact) -> Result<(), String> {
    qty.to_f64()
        .map_err(|e| format!("portfolio quantity projection: {e}"))?;
    qty.validate_storage().map_err(|e| e.to_string())
}
fn validate_position(row: &PortfolioPosition) -> Result<(), String> {
    if row.signed_qty.is_zero()
        || row
            .entry_value
            .as_ref()
            .is_some_and(|value| !value.is_positive())
        || row.stop_px.as_ref().is_some_and(|stop| !stop.is_positive())
    {
        return Err("invalid portfolio inventory row".into());
    }
    validate_quantity(&row.signed_qty)?;
    for value in row.entry_value.iter().chain(row.stop_px.iter()) {
        value.validate_storage().map_err(|e| e.to_string())?;
    }
    if let Some(cost) = &row.entry_value {
        cost.checked_div(&row.signed_qty.abs())
            .and_then(|price| price.to_f64())
            .map_err(|e| format!("portfolio basis projection: {e}"))?;
    }
    if let Some(stop) = &row.stop_px {
        stop.to_f64()
            .map_err(|e| format!("portfolio stop projection: {e}"))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    const A: StrategyId = StrategyId(0);
    const B: StrategyId = StrategyId(1);
    const BTC: SymbolId = SymbolId(0);
    fn n(value: &str) -> Exact {
        Exact::parse_decimal(value).unwrap()
    }
    fn fill(
        book: &mut Inventory,
        owner: StrategyId,
        side: Side,
        qty: &str,
        px: &str,
    ) -> Option<Exact> {
        book.fill(InventoryFill {
            strategy: owner,
            symbol: BTC,
            side,
            qty: n(qty),
            px: Some(n(px)),
            stop: None,
            settlement_asset: AssetId::Named("USDT".into()),
        })
        .unwrap()
        .amount
    }

    #[test]
    fn opposite_sleeves_keep_exact_gross_and_basis_through_restart() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "0.1", "100");
        fill(&mut book, A, Side::Buy, "0.2", "110");
        fill(&mut book, B, Side::Sell, "0.3", "120");
        assert_eq!(book.net(BTC), n("0"));
        assert_eq!(book.gross(BTC), n("0.6"));
        let bytes = serde_json::to_vec(&book.snapshot()).unwrap();
        let mut recovered = Inventory::restore(&serde_json::from_slice(&bytes).unwrap()).unwrap();
        assert_eq!(recovered.snapshot(), book.snapshot());
        assert_eq!(
            fill(&mut recovered, A, Side::Sell, "0.3", "130"),
            Some(n("7"))
        );
        assert_eq!(
            recovered.position(B, BTC).unwrap().entry_value,
            Some(n("36"))
        );
        assert_eq!(recovered.net(BTC), n("-0.3"));
    }

    #[test]
    fn partial_close_keeps_rational_cost_and_flip_uses_new_basis() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1", "100");
        fill(&mut book, A, Side::Buy, "2", "110");
        let first = fill(&mut book, A, Side::Sell, "1", "120").unwrap();
        let second = fill(&mut book, A, Side::Sell, "3", "130").unwrap();
        assert_eq!(first + second, n("60"));
        let position = book.position(A, BTC).unwrap();
        assert_eq!(position.signed_qty, n("-1"));
        assert_eq!(position.entry_value, Some(n("130")));
        assert_eq!(position.stop_px, None);
    }

    #[test]
    fn legacy_unknown_basis_stays_unknown_until_that_position_closes() {
        let mut book = Inventory::default();
        book.fill(InventoryFill {
            strategy: A,
            symbol: BTC,
            side: Side::Buy,
            qty: n("2"),
            px: None,
            stop: None,
            settlement_asset: AssetId::Unknown,
        })
        .unwrap();
        fill(&mut book, A, Side::Buy, "1", "100");
        assert_eq!(fill(&mut book, A, Side::Sell, "3", "110"), None);
        fill(&mut book, A, Side::Buy, "1", "120");
        assert_eq!(book.position(A, BTC).unwrap().entry_value, Some(n("120")));
    }

    #[test]
    fn invalid_fill_and_duplicate_restore_cannot_mutate_inventory() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1", "100");
        let original = book.snapshot();
        assert!(book
            .fill(InventoryFill {
                strategy: A,
                symbol: BTC,
                side: Side::Sell,
                qty: n("-1"),
                px: Some(n("110")),
                stop: None,
                settlement_asset: AssetId::Unknown
            })
            .is_err());
        assert_eq!(book.snapshot(), original);
        let mut duplicate = original;
        duplicate.positions.push(duplicate.positions[0].clone());
        assert!(Inventory::restore(&duplicate).is_err());
    }
    #[test]
    fn unprojectable_growth_and_restore_are_refused_without_erasing_a_real_holding() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1e308", "1");
        let before = book.snapshot();
        let result = book.fill(InventoryFill {
            strategy: A,
            symbol: BTC,
            side: Side::Buy,
            qty: n("1e308"),
            px: Some(n("1")),
            stop: None,
            settlement_asset: AssetId::Named("USDT".into()),
        });
        assert!(
            result.is_err(),
            "accepted exact inventory whose compatibility quantity overflows"
        );
        assert_eq!(book.snapshot(), before);
        let mut malformed = before;
        malformed.positions[0].signed_qty = n("1e309");
        assert!(
            Inventory::restore(&malformed).is_err(),
            "restore accepted an unreadable quantity projection"
        );
    }

    #[test]
    fn unknown_execution_units_cannot_inherit_a_known_cost_basis_label() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1", "100");
        book.fill(InventoryFill {
            strategy: A,
            symbol: BTC,
            side: Side::Buy,
            qty: n("1"),
            px: Some(n("110")),
            stop: None,
            settlement_asset: AssetId::Unknown,
        })
        .unwrap();
        assert_eq!(
            book.position(A, BTC).unwrap().settlement_asset,
            AssetId::Unknown,
            "unknown-unit cost was relabeled as USDT"
        );
    }

    #[test]
    fn restore_checks_each_quantity_and_total_virtual_gross_projection() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1e308", "1");
        let mut state = book.snapshot();
        state.positions[0].signed_qty = n("1e309");
        assert!(
            Inventory::restore(&state).is_err(),
            "restore accepted a quantity whose legacy projection is infinite"
        );
        state.positions[0].signed_qty = n("1e-400");
        assert!(
            Inventory::restore(&state).is_err(),
            "restore accepted a nonzero quantity whose legacy projection is zero"
        );
        state.positions[0].signed_qty = n("1e308");
        let mut second = state.positions[0].clone();
        second.strategy = B;
        second.signed_qty = n("-1e308");
        state.positions.push(second);
        assert!(
            Inventory::restore(&state).is_err(),
            "net zero hid overflowing virtual gross"
        );
    }

    #[test]
    fn prepared_change_is_read_only_and_stale_sibling_totals_cannot_commit() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1", "100");
        let before = book.snapshot();
        let change = book
            .prepare_fill(InventoryFill {
                strategy: A,
                symbol: BTC,
                side: Side::Sell,
                qty: n("0.5"),
                px: Some(n("120")),
                stop: None,
                settlement_asset: AssetId::Named("USDT".into()),
            })
            .unwrap();
        assert_eq!(book.snapshot(), before);
        assert_eq!(
            change.realized,
            RealizedValue {
                amount: Some(n("10")),
                asset: AssetId::Named("USDT".into())
            }
        );
        fill(&mut book, B, Side::Sell, "0.2", "110");
        let after_sibling = book.snapshot();
        assert!(book.apply(change).is_err());
        assert_eq!(book.snapshot(), after_sibling);
    }

    #[test]
    fn unknown_close_units_stay_on_realized_value_without_relabeling_remaining_cost() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "2", "100");
        let realized = book
            .fill(InventoryFill {
                strategy: A,
                symbol: BTC,
                side: Side::Sell,
                qty: n("1"),
                px: Some(n("110")),
                stop: None,
                settlement_asset: AssetId::Unknown,
            })
            .unwrap();
        assert_eq!(
            realized,
            RealizedValue {
                amount: Some(n("10")),
                asset: AssetId::Unknown
            }
        );
        assert_eq!(
            book.position(A, BTC).unwrap().settlement_asset,
            AssetId::Named("USDT".into())
        );
        assert_eq!(book.position(A, BTC).unwrap().entry_value, Some(n("100")));
    }

    #[test]
    fn net_zero_does_not_hide_gross_overflow_during_preparation() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1e308", "1");
        let before = book.snapshot();
        assert!(book
            .prepare_fill(InventoryFill {
                strategy: B,
                symbol: BTC,
                side: Side::Sell,
                qty: n("1e308"),
                px: Some(n("1")),
                stop: None,
                settlement_asset: AssetId::Named("USDT".into())
            })
            .is_err());
        assert_eq!(book.snapshot(), before);
    }

    #[test]
    fn invalid_legacy_restatement_preserves_prior_inventory() {
        let mut book = Inventory::default();
        fill(&mut book, A, Side::Buy, "1", "100");
        let before = book.snapshot();
        let row = |strategy, qty| engine_types::FilledTotal {
            strategy,
            symbol: BTC,
            signed_qty: qty,
        };
        for rows in [
            vec![row(A, f64::INFINITY)],
            vec![row(A, 1.0), row(A, 2.0)],
            vec![row(A, 1e308), row(B, -1e308)],
        ] {
            assert!(book.restate_legacy(&rows).is_err());
            assert_eq!(book.snapshot(), before);
        }
    }
}
