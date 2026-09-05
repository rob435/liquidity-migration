use super::*;
use engine_types::execution_allocation::ExecutionAllocation;
use engine_types::numeric::{AssetAmount, ExecutionAmounts};

#[derive(Debug)]
pub(crate) struct PreparedPortfolioFill {
    pub allocation: ExecutionAllocation,
    inventory: crate::inventory::InventoryBatchChange,
    accounting: crate::execution_accounting::AccountingBatch,
    symbol: SymbolId,
    legacy: bool,
}

struct ExecutionRef<'a> {
    client_order_id: &'a str,
    symbol: SymbolId,
    side: Side,
    qty: f64,
    px: f64,
    fee: Option<f64>,
    amounts: Option<&'a ExecutionAmounts>,
    allocation: Option<&'a ExecutionAllocation>,
    forced_close: Option<ForcedClose>,
}

impl Attribution {
    pub(crate) fn prepare_portfolio_update(
        &self,
        owner: Option<StrategyId>,
        names: &[String],
        update: &OrderUpdate,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        let OrderUpdate::Fill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            amounts,
            allocation,
            forced_close,
            ..
        } = update
        else {
            return Ok(None);
        };
        self.prepare_execution_allocation(
            owner,
            names,
            ExecutionRef {
                client_order_id,
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                amounts: amounts.as_deref(),
                allocation: allocation.as_deref(),
                forced_close: *forced_close,
            },
        )
    }

    pub(crate) fn prepare_portfolio_recovered(
        &self,
        owner: Option<StrategyId>,
        names: &[String],
        record: &WalRecord,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        let WalRecord::RecoveredFill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            amounts,
            allocation,
            forced_close,
            ..
        } = record
        else {
            return Err("expected recovered execution".into());
        };
        self.prepare_execution_allocation(
            owner,
            names,
            ExecutionRef {
                client_order_id,
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                amounts: amounts.as_ref(),
                allocation: allocation.as_deref(),
                forced_close: *forced_close,
            },
        )
    }

    fn prepare_execution_allocation(
        &self,
        owner: Option<StrategyId>,
        names: &[String],
        execution: ExecutionRef<'_>,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        let forced = execution.client_order_id.is_empty() && execution.forced_close.is_some();
        if owner.is_none() && !forced {
            if execution.allocation.is_some() {
                return Err("unowned execution carries sleeve allocation".into());
            }
            return Ok(None);
        }
        if owner.is_none() {
            let net = self.inventory.net(execution.symbol);
            if net.is_zero()
                || net.is_positive() == (execution.side == Side::Buy)
                || execution.qty > net.abs().to_f64().map_err(|e| e.to_string())?
            {
                if execution.allocation.is_some() {
                    return Err("recorded forced allocation exceeds owned physical net".into());
                }
                return Ok(None);
            }
        }
        use engine_types::numeric::{AssetId, Exact, ExactNumber};
        let (quantity, price, settlement_asset, fee) = if let Some(amounts) = execution.amounts {
            amounts
                .validate_projection(execution.qty, execution.px, execution.fee)
                .map_err(|e| e.to_string())?;
            (
                amounts.quantity.value.clone(),
                amounts.price.value.clone(),
                amounts.settlement_asset.clone(),
                amounts.fee.clone(),
            )
        } else {
            let fee = execution
                .fee
                .map(|value| {
                    ExactNumber::legacy_binary64(value).map(|amount| AssetAmount {
                        asset: AssetId::Unknown,
                        amount,
                    })
                })
                .transpose()
                .map_err(|e| e.to_string())?;
            (
                Exact::from_legacy_f64(execution.qty).map_err(|e| e.to_string())?,
                Exact::from_legacy_f64(execution.px).map_err(|e| e.to_string())?,
                AssetId::Unknown,
                fee,
            )
        };
        let allocation = crate::portfolio_allocation::allocate(
            &self.inventory,
            owner,
            names,
            crate::portfolio_allocation::AllocationInput {
                symbol: execution.symbol,
                side: execution.side,
                quantity: &quantity,
                fee: fee.as_ref(),
                forced_close: forced,
            },
        )?;
        if execution
            .allocation
            .is_some_and(|recorded| recorded != &allocation)
        {
            return Err("recorded execution allocation disagrees with durable ownership".into());
        }
        let inventory = self.inventory.prepare_batch(
            allocation
                .slices
                .iter()
                .map(|slice| crate::inventory::InventoryFill {
                    strategy: slice.strategy,
                    symbol: execution.symbol,
                    side: execution.side,
                    qty: slice.quantity.clone(),
                    px: Some(price.clone()),
                    stop: None,
                    settlement_asset: settlement_asset.clone(),
                })
                .collect(),
        )?;
        let accounting = self.accounting.prepare_batch(
            allocation
                .slices
                .iter()
                .zip(&inventory.realized)
                .map(|(slice, (strategy, symbol, realized))| {
                    crate::execution_accounting::ExecutionAccountingInput {
                        strategy: *strategy,
                        symbol: *symbol,
                        side: execution.side,
                        qty: slice.quantity.clone(),
                        px: Some(price.clone()),
                        settlement_asset: settlement_asset.clone(),
                        fee: slice.fee.clone(),
                        realized: realized.clone(),
                    }
                })
                .collect(),
        )?;
        Ok(Some(PreparedPortfolioFill {
            allocation,
            inventory,
            accounting,
            symbol: execution.symbol,
            legacy: execution.amounts.is_none(),
        }))
    }

    pub(crate) fn commit_portfolio_fill(
        &mut self,
        prepared: PreparedPortfolioFill,
    ) -> Result<(), String> {
        self.inventory.validate_batch(&prepared.inventory)?;
        self.accounting.validate_batch(&prepared.accounting)?;
        self.inventory.apply_batch(prepared.inventory)?;
        self.accounting.apply_batch(prepared.accounting)?;
        if prepared.legacy {
            for slice in prepared.allocation.slices {
                if self.signed(slice.strategy, prepared.symbol).abs() < FLAT {
                    self.inventory.remove(slice.strategy, prepared.symbol);
                }
            }
        }
        Ok(())
    }
}

impl Attribution {
    pub(crate) fn fold_record_fill(
        &mut self,
        record: &WalRecord,
        owner: Option<StrategyId>,
        names: &[String],
    ) -> Result<bool, String> {
        let allocated = matches!(
            record,
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill {
                    allocation: Some(_),
                    ..
                }
            } | WalRecord::RecoveredFill {
                allocation: Some(_),
                ..
            }
        );
        if allocated {
            let prepared = match record {
                WalRecord::OrderUpdate { update } => {
                    self.prepare_portfolio_update(owner, names, update)?
                }
                WalRecord::RecoveredFill { .. } => {
                    self.prepare_portfolio_recovered(owner, names, record)?
                }
                _ => unreachable!(),
            }
            .ok_or("recorded allocation has no owner")?;
            self.commit_portfolio_fill(prepared)?;
            return Ok(true);
        }
        let Some(owner) = owner else {
            return Ok(false);
        };
        match record {
            WalRecord::OrderUpdate { update } => self.try_on_update(owner, update)?,
            WalRecord::RecoveredFill { .. } => self.try_on_recovered(owner, record)?,
            _ => return Err("expected a fill record".into()),
        }
        Ok(true)
    }
}
