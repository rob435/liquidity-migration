use super::*;
use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio_control::{InternalSettlementTotals, PortfolioOffsetSettlement};
use std::collections::BTreeMap;

type Key = (StrategyId, SymbolId, AssetId);
#[derive(Debug, Default)]
pub(super) struct InternalAccounting(BTreeMap<Key, InternalSettlementTotals>);

pub(crate) struct PreparedInternalSettlement {
    inventory: crate::inventory::InventoryBatchChange,
    totals: Vec<(
        Key,
        Option<InternalSettlementTotals>,
        InternalSettlementTotals,
    )>,
}
impl InternalAccounting {
    pub(super) fn restore(rows: &[InternalSettlementTotals]) -> Result<Self, String> {
        let mut out = Self::default();
        for row in rows {
            row.cash_flow
                .validate_storage()
                .map_err(|e| e.to_string())?;
            row.realized_gross
                .validate_storage()
                .map_err(|e| e.to_string())?;
            if matches!(&row.asset, AssetId::Named(asset) if asset.is_empty()) {
                return Err("invalid internal settlement asset".into());
            }
            let key = (row.strategy, row.symbol, row.asset.clone());
            if out.0.insert(key, row.clone()).is_some() {
                return Err("duplicate internal settlement total".into());
            }
        }
        Ok(out)
    }
    pub(super) fn snapshot(&self) -> Vec<InternalSettlementTotals> {
        self.0.values().cloned().collect()
    }
}

impl Attribution {
    pub(crate) fn prepare_internal_settlement(
        &self,
        settlement: &PortfolioOffsetSettlement,
    ) -> Result<PreparedInternalSettlement, String> {
        crate::portfolio_control::validate_settlement(settlement)?;
        let holdings: Vec<_> = self
            .inventory
            .rows()
            .filter(|row| row.symbol == settlement.symbol)
            .collect();
        if holdings.len() != settlement.slices.len() {
            return Err(
                "internal settlement must close every remaining sleeve on the symbol".into(),
            );
        }
        let mut fills = Vec::new();
        for slice in &settlement.slices {
            let held = self
                .inventory
                .position(slice.strategy, settlement.symbol)
                .ok_or("internal settlement names an unowned position")?;
            if &held.signed_qty + &slice.signed_quantity != Exact::zero()
                || (held.settlement_asset != AssetId::Unknown
                    && held.settlement_asset != slice.settlement_asset)
            {
                return Err("internal settlement disagrees with durable inventory or asset".into());
            }
            fills.push(crate::inventory::InventoryFill {
                strategy: slice.strategy,
                symbol: settlement.symbol,
                side: if slice.signed_quantity.is_positive() {
                    Side::Buy
                } else {
                    Side::Sell
                },
                qty: slice.signed_quantity.abs(),
                px: Some(settlement.price.clone()),
                stop: None,
                settlement_asset: slice.settlement_asset.clone(),
            });
        }
        let inventory = self.inventory.prepare_batch(fills)?;
        let mut totals = Vec::new();
        for (slice, (_, _, realized)) in settlement.slices.iter().zip(&inventory.realized) {
            let key = (
                slice.strategy,
                settlement.symbol,
                slice.settlement_asset.clone(),
            );
            let prior = self.internal.0.get(&key).cloned();
            let mut next = prior.clone().unwrap_or_else(|| InternalSettlementTotals {
                strategy: slice.strategy,
                symbol: settlement.symbol,
                asset: slice.settlement_asset.clone(),
                cash_flow: Exact::zero(),
                realized_gross: Exact::zero(),
                unvalued_realized_events: 0,
            });
            next.cash_flow -= &slice.signed_quantity * &settlement.price;
            match &realized.amount {
                Some(value) if realized.asset == slice.settlement_asset => {
                    next.realized_gross += value
                }
                Some(value) if value.is_zero() => (),
                _ => {
                    next.unvalued_realized_events = next
                        .unvalued_realized_events
                        .checked_add(1)
                        .ok_or("internal settlement uncertainty count exhausted")?
                }
            }
            next.cash_flow
                .validate_storage()
                .map_err(|e| e.to_string())?;
            next.realized_gross
                .validate_storage()
                .map_err(|e| e.to_string())?;
            totals.push((key, prior, next));
        }
        Ok(PreparedInternalSettlement { inventory, totals })
    }
    pub(crate) fn commit_internal_settlement(
        &mut self,
        prepared: PreparedInternalSettlement,
    ) -> Result<(), String> {
        self.inventory.validate_batch(&prepared.inventory)?;
        if prepared
            .totals
            .iter()
            .any(|(key, prior, _)| self.internal.0.get(key) != prior.as_ref())
        {
            return Err("internal settlement was prepared against different totals".into());
        }
        self.inventory.apply_batch(prepared.inventory)?;
        self.legacy_quantities
            .retain(|(strategy, symbol), _| self.inventory.position(*strategy, *symbol).is_some());
        for (key, _, next) in prepared.totals {
            self.internal.0.insert(key, next);
        }
        Ok(())
    }
    pub(crate) fn validate_sleeve_stop_exact(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        trigger: &Exact,
    ) -> Result<(), String> {
        trigger.validate_storage().map_err(|e| e.to_string())?;
        if !trigger.is_positive() {
            return Err("sleeve stop is not positive".into());
        }
        let held = self
            .inventory
            .position(strategy, symbol)
            .ok_or("sleeve stop has no owned position")?;
        if held.signed_qty.is_positive() != (side == Side::Buy) {
            return Err("sleeve stop has the wrong position direction".into());
        }
        Ok(())
    }
    pub(crate) fn set_sleeve_stop_exact(
        &mut self,
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        trigger: Exact,
    ) -> Result<(), String> {
        self.validate_sleeve_stop_exact(strategy, symbol, side, &trigger)?;
        self.inventory.tighten_stop(strategy, symbol, side, trigger);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::portfolio_control::PortfolioOffsetSlice;
    fn decimal(s: &str) -> Exact {
        Exact::parse_decimal(s).unwrap()
    }
    fn settlement() -> PortfolioOffsetSettlement {
        PortfolioOffsetSettlement {
            emergency_id: 1,
            symbol: SymbolId(0),
            price: decimal("99.1"),
            settled_ms: 9,
            slices: vec![
                PortfolioOffsetSlice {
                    strategy: StrategyId(0),
                    signed_quantity: decimal("-0.3"),
                    settlement_asset: AssetId::Named("USDT".into()),
                },
                PortfolioOffsetSlice {
                    strategy: StrategyId(1),
                    signed_quantity: decimal("0.3"),
                    settlement_asset: AssetId::Named("USDT".into()),
                },
            ],
        }
    }
    #[test]
    fn internal_settlement_is_exact_atomic_and_separate_from_venue_executions() {
        let prior = crate::tests::shared_sleeves::owned_records("0.3", "0.3");
        let mut book = Attribution::try_from_records(&prior).unwrap();
        let before = book.snapshot();
        for bad in 0..3 {
            let mut invalid = settlement();
            match bad {
                0 => {
                    invalid.slices[0].settlement_asset = AssetId::Named("BTC".into());
                }
                1 => {
                    invalid.slices[0].signed_quantity = decimal("-0.2");
                    invalid.slices[1].signed_quantity = decimal("0.2");
                }
                _ => {
                    invalid.slices[1].strategy = StrategyId(0);
                }
            }
            assert!(book.prepare_internal_settlement(&invalid).is_err());
            assert_eq!(
                book.snapshot(),
                before,
                "failed preparation mutated a sleeve or its fees"
            );
        }
        let prepared = book.prepare_internal_settlement(&settlement()).unwrap();
        book.commit_internal_settlement(prepared).unwrap();
        let after = book.snapshot();
        assert!(after.positions.is_empty());
        assert_eq!(
            after.accounting, before.accounting,
            "internal closure invented venue accounting"
        );
        assert_eq!(after.internal_settlements[0].cash_flow, decimal("29.73"));
        assert_eq!(after.internal_settlements[1].cash_flow, decimal("-29.73"));
        assert_eq!(
            after.internal_settlements[0].realized_gross,
            decimal("-0.27")
        );
        assert_eq!(
            after.internal_settlements[1].realized_gross,
            decimal("0.27")
        );
        assert!(
            book.prepare_internal_settlement(&settlement()).is_err(),
            "a completed internal closure must not apply twice"
        );
        assert_eq!(book.snapshot(), after);
    }
}
