use super::*;
use engine_types::execution_allocation::ExecutionAllocation;
use engine_types::numeric::{AssetAmount, ExecutionAmounts};

#[derive(Debug)]
pub(crate) struct PreparedPortfolioFill {
    pub allocation: ExecutionAllocation,
    inventory: crate::inventory::InventoryBatchChange,
    accounting: crate::execution_accounting::AccountingBatch,
    /// The binary64 signed quantity this execution puts into each sleeve's
    /// sum. Empty when the venue stated exact amounts.
    legacy_inputs: Vec<(StrategyId, SymbolId, engine_types::numeric::Exact)>,
}

#[derive(Clone, Copy)]
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
    engine_net: bool,
}

impl Attribution {
    pub(crate) fn prepare_portfolio_update_for_order(
        &self,
        request: Option<&engine_types::OrderRequest>,
        names: &[String],
        update: &OrderUpdate,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        self.prepare_portfolio_update_on_grid(request, names, update, None)
    }

    pub(crate) fn prepare_portfolio_update_on_grid(
        &self,
        request: Option<&engine_types::OrderRequest>,
        names: &[String],
        update: &OrderUpdate,
        legacy_step: Option<&engine_types::numeric::Exact>,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        self.prepare_portfolio_update_authorized(
            request.and_then(|r| r.sleeve_owner()),
            names,
            update,
            request.is_some_and(|r| r.is_portfolio_reduction()),
            legacy_step,
        )
    }

    fn prepare_portfolio_update_authorized(
        &self,
        owner: Option<StrategyId>,
        names: &[String],
        update: &OrderUpdate,
        engine_net: bool,
        legacy_step: Option<&engine_types::numeric::Exact>,
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
                engine_net,
            },
            legacy_step,
        )
    }

    pub(crate) fn prepare_portfolio_recovered_for_order(
        &self,
        request: Option<&engine_types::OrderRequest>,
        names: &[String],
        record: &WalRecord,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        self.prepare_portfolio_recovered_on_grid(request, names, record, None)
    }

    pub(crate) fn prepare_portfolio_recovered_on_grid(
        &self,
        request: Option<&engine_types::OrderRequest>,
        names: &[String],
        record: &WalRecord,
        legacy_step: Option<&engine_types::numeric::Exact>,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        self.prepare_portfolio_recovered_authorized(
            request.and_then(|r| r.sleeve_owner()),
            names,
            record,
            request.is_some_and(|r| r.is_portfolio_reduction()),
            legacy_step,
        )
    }

    fn prepare_portfolio_recovered_authorized(
        &self,
        owner: Option<StrategyId>,
        names: &[String],
        record: &WalRecord,
        engine_net: bool,
        legacy_step: Option<&engine_types::numeric::Exact>,
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
                engine_net,
            },
            legacy_step,
        )
    }

    /// Every sleeve's holding in this symbol on the venue's quantity grid.
    ///
    /// A row whose binary64 readings admit no single grid value keeps its
    /// sum, so an execution that needs one still refuses rather than being
    /// charged against a guess.
    fn owned_on_grid(
        &self,
        symbol: SymbolId,
        step: &engine_types::numeric::Exact,
    ) -> std::collections::BTreeMap<StrategyId, engine_types::numeric::Exact> {
        self.inventory
            .rows()
            .filter(|row| row.symbol == symbol)
            .map(|row| {
                let quantity = self
                    .legacy_quantities
                    .get(&(row.strategy, symbol))
                    .and_then(|origin| origin.resolve(&row.signed_qty, step).ok())
                    .unwrap_or_else(|| row.signed_qty.clone());
                (row.strategy, quantity)
            })
            .collect()
    }

    fn prepare_execution_allocation(
        &self,
        owner: Option<StrategyId>,
        names: &[String],
        execution: ExecutionRef<'_>,
        legacy_step: Option<&engine_types::numeric::Exact>,
    ) -> Result<Option<PreparedPortfolioFill>, String> {
        let forced = execution.engine_net
            || (execution.client_order_id.is_empty() && execution.forced_close.is_some());
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
        let (mut quantity, price, settlement_asset, fee) = if let Some(amounts) = execution.amounts
        {
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
        let legacy_step = execution.allocation.map_or_else(
            || {
                (execution.amounts.is_none()
                    && owner.is_none()
                    && forced
                    && quantity != self.inventory.net(execution.symbol).abs())
                .then_some(legacy_step)
                .flatten()
            },
            |allocation| allocation.legacy_quantity_step.as_ref(),
        );
        // The holding this execution is charged against was summed from the
        // same binary64 readings, so it lands a rounding off the grid the
        // quantity above was just resolved onto. Both sides are compared and
        // sliced on that grid or neither is.
        let owned = legacy_step.map(|step| self.owned_on_grid(execution.symbol, step));
        if let Some(step) = legacy_step {
            if execution.amounts.is_some() || owner.is_some() || !forced {
                return Err("legacy grid receipt requires a binary64 FIFO execution".into());
            }
            quantity = crate::portfolio_allocation::legacy_quantity(execution.qty, step)?;
        }
        let mut allocation = crate::portfolio_allocation::allocate(
            &self.inventory,
            owner,
            names,
            crate::portfolio_allocation::AllocationInput {
                symbol: execution.symbol,
                side: execution.side,
                quantity: &quantity,
                owned: owned.as_ref(),
                fee: fee.as_ref(),
                forced_close: forced,
            },
        )?;
        allocation.legacy_quantity_step = legacy_step.cloned();
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
        let legacy_inputs = if execution.amounts.is_some() {
            Vec::new()
        } else {
            allocation
                .slices
                .iter()
                .map(|slice| {
                    let signed = if execution.side == Side::Buy {
                        slice.quantity.clone()
                    } else {
                        -&slice.quantity
                    };
                    (slice.strategy, execution.symbol, signed)
                })
                .collect()
        };
        Ok(Some(PreparedPortfolioFill {
            allocation,
            inventory,
            accounting,
            legacy_inputs,
        }))
    }

    /// `legacy_inputs` is empty exactly when the venue stated exact amounts.
    /// A recorded allocation does not make a binary64 quantity exact — it
    /// only makes it durable — so the fill runs the same legacy tail that
    /// replay of the record will run, `commit_prepared`'s.
    pub(crate) fn commit_portfolio_fill(
        &mut self,
        prepared: PreparedPortfolioFill,
    ) -> Result<(), String> {
        let PreparedPortfolioFill {
            inventory,
            accounting,
            legacy_inputs,
            ..
        } = prepared;
        let origins = legacy_inputs
            .iter()
            .map(|(strategy, symbol, signed)| {
                self.note_legacy_reading((*strategy, *symbol), signed)
            })
            .collect::<Result<Vec<_>, String>>()?;
        self.inventory.validate_batch(&inventory)?;
        self.accounting.validate_batch(&accounting)?;
        self.inventory.apply_batch(inventory)?;
        self.accounting.apply_batch(accounting)?;
        for ((strategy, symbol, _), origin) in legacy_inputs.into_iter().zip(origins) {
            self.settle_legacy_sum((strategy, symbol), origin);
        }
        self.legacy_quantities
            .retain(|(strategy, symbol), _| self.inventory.position(*strategy, *symbol).is_some());
        Ok(())
    }
}

impl Attribution {
    pub(crate) fn fold_record_fill(
        &mut self,
        record: &WalRecord,
        owner: Option<StrategyId>,
        request: Option<&engine_types::OrderRequest>,
        names: &[String],
    ) -> Result<bool, String> {
        let allocated = matches!(
            record,
            WalRecord::OrderUpdate {
                callbacks: _,
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
                WalRecord::OrderUpdate { update, .. } => self.prepare_portfolio_update_authorized(
                    owner,
                    names,
                    update,
                    request.is_some_and(|r| r.is_portfolio_reduction()),
                    None,
                )?,
                WalRecord::RecoveredFill { .. } => self.prepare_portfolio_recovered_authorized(
                    owner,
                    names,
                    record,
                    request.is_some_and(|r| r.is_portfolio_reduction()),
                    None,
                )?,
                _ => unreachable!(),
            }
            .ok_or("recorded allocation has no owner")?;
            self.commit_portfolio_fill(prepared)?;
            return Ok(true);
        }
        if request.is_some_and(|r| r.is_portfolio_reduction()) {
            return Err("engine net execution is missing its durable allocation".into());
        }
        let Some(owner) = owner else {
            return Ok(false);
        };
        match record {
            WalRecord::OrderUpdate { update, .. } => self.try_on_update(owner, update)?,
            WalRecord::RecoveredFill { .. } => self.try_on_recovered(owner, record)?,
            _ => return Err("expected a fill record".into()),
        }
        Ok(true)
    }
}
