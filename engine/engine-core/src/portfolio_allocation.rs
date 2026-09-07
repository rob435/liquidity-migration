use crate::inventory::Inventory;
use engine_types::execution_allocation::{AllocationPolicy, ExecutionAllocation, ExecutionSlice};
use engine_types::numeric::{AssetAmount, Exact, ExactNumber};
use engine_types::{Side, StrategyId, SymbolId};

#[derive(Clone, Copy)]
pub(crate) struct AllocationInput<'a> {
    pub symbol: SymbolId,
    pub side: Side,
    pub quantity: &'a Exact,
    pub fee: Option<&'a AssetAmount>,
    pub forced_close: bool,
}

pub(crate) fn allocate(
    inventory: &Inventory,
    owner: Option<StrategyId>,
    strategy_names: &[String],
    input: AllocationInput<'_>,
) -> Result<ExecutionAllocation, String> {
    let AllocationInput {
        symbol,
        side,
        quantity,
        fee,
        forced_close,
    } = input;
    if !quantity.is_positive() {
        return Err("execution allocation quantity must be positive".into());
    }
    quantity.validate_storage().map_err(|e| e.to_string())?;
    if let Some(fee) = fee {
        fee.amount
            .validate_provenance()
            .map_err(|e| e.to_string())?;
    }
    let name = |strategy: StrategyId| -> Result<String, String> {
        let key = strategy_names
            .get(strategy.idx())
            .ok_or("allocation owner has no durable strategy key")?;
        if key.is_empty() || strategy_names.iter().filter(|other| *other == key).count() != 1 {
            return Err("allocation owner key is empty or ambiguous".into());
        }
        Ok(key.clone())
    };
    if let Some(strategy) = owner {
        return Ok(ExecutionAllocation {
            policy: AllocationPolicy::DirectOrder,
            legacy_quantity_step: None,
            slices: vec![ExecutionSlice {
                strategy,
                strategy_key: name(strategy)?,
                quantity: quantity.clone(),
                fee: fee.cloned(),
            }],
        });
    }
    if !forced_close {
        return Err("execution has neither an order owner nor a venue forced-close reason".into());
    }
    let net = inventory.net(symbol);
    if net.is_zero() || net.is_positive() == (side == Side::Buy) || quantity > &net.abs() {
        return Err("forced execution does not reduce the owned physical net".into());
    }
    let mut contributors = inventory
        .rows()
        .filter(|row| row.symbol == symbol && row.signed_qty.is_positive() == net.is_positive())
        .map(|row| name(row.strategy).map(|name| (name, row)))
        .collect::<Result<Vec<_>, _>>()?;
    contributors.sort_by(|a, b| a.0.cmp(&b.0));
    let mut remaining = quantity.clone();
    let mut slices = Vec::new();
    for (strategy_key, row) in contributors {
        if remaining.is_zero() {
            break;
        }
        let qty = remaining.clone().min(row.signed_qty.abs());
        let slice_fee = fee
            .map(|fee| -> Result<AssetAmount, String> {
                let value = (&fee.amount.value * &qty)
                    .checked_div(quantity)
                    .map_err(|e| e.to_string())?;
                value.validate_storage().map_err(|e| e.to_string())?;
                Ok(AssetAmount {
                    asset: fee.asset.clone(),
                    amount: ExactNumber::derived(value),
                })
            })
            .transpose()?;
        remaining -= &qty;
        slices.push(ExecutionSlice {
            strategy: row.strategy,
            strategy_key,
            quantity: qty,
            fee: slice_fee,
        });
    }
    if !remaining.is_zero() {
        return Err("forced execution has insufficient owned contributors".into());
    }
    Ok(ExecutionAllocation {
        policy: AllocationPolicy::EmergencyNetFifo,
        legacy_quantity_step: None,
        slices,
    })
}

pub(crate) fn fill_quantity(
    qty: f64,
    amounts: Option<&engine_types::numeric::ExecutionAmounts>,
    allocation: Option<&ExecutionAllocation>,
) -> Result<Exact, String> {
    let raw = crate::reconcile::fill_quantity(qty, amounts)?;
    let Some(step) = allocation.and_then(|row| row.legacy_quantity_step.as_ref()) else {
        return Ok(raw);
    };
    if amounts.is_some()
        || allocation.is_none_or(|row| row.policy != AllocationPolicy::EmergencyNetFifo)
    {
        return Err("legacy grid receipt requires a binary64 FIFO execution".into());
    }
    legacy_quantity(qty, step)
}

pub(crate) fn legacy_quantity(qty: f64, step: &Exact) -> Result<Exact, String> {
    if !step.is_positive() {
        return Err("legacy execution quantity grid must be positive".into());
    }
    let raw = Exact::from_legacy_f64(qty).map_err(|e| e.to_string())?;
    let mut origin = crate::legacy_quantity::Origin::default();
    origin.note(&raw)?;
    let quantity = origin.resolve(&raw, step)?;
    if !quantity.is_positive() {
        return Err("legacy execution quantity does not resolve to a positive grid value".into());
    }
    Ok(quantity)
}

pub(crate) fn slice_updates(
    update: &engine_types::OrderUpdate,
) -> Result<Option<Vec<(StrategyId, engine_types::OrderUpdate)>>, String> {
    use engine_types::OrderUpdate;
    let OrderUpdate::Fill {
        allocation: Some(allocation),
        qty,
        fee,
        amounts,
        ..
    } = update
    else {
        return Ok(None);
    };
    let exact_qty = fill_quantity(*qty, amounts.as_deref(), Some(allocation))?;
    let mut sum = Exact::zero();
    let expected_fee = if let Some(amounts) = amounts {
        amounts.fee.clone()
    } else {
        fee.map(|value| {
            ExactNumber::legacy_binary64(value).map(|amount| AssetAmount {
                asset: engine_types::numeric::AssetId::Unknown,
                amount,
            })
        })
        .transpose()
        .map_err(|e| e.to_string())?
    };
    let mut fee_sum = Exact::zero();
    let mut owners = std::collections::BTreeSet::new();
    let mut keys = std::collections::BTreeSet::new();
    let mut result = Vec::with_capacity(allocation.slices.len());
    for slice in &allocation.slices {
        if !slice.quantity.is_positive() || !owners.insert(slice.strategy) {
            return Err("execution allocation has invalid or repeated owner quantity".into());
        }
        if slice.strategy_key.is_empty() || !keys.insert(&slice.strategy_key) {
            return Err("execution allocation has invalid or repeated owner key".into());
        }
        match (&expected_fee, &slice.fee) {
            (None, None) => {}
            (Some(expected), Some(fee)) if expected.asset == fee.asset => {
                fee.amount
                    .validate_provenance()
                    .map_err(|e| e.to_string())?;
                fee_sum += &fee.amount.value;
            }
            _ => return Err("execution allocation changes fee availability or currency".into()),
        }
        sum += &slice.quantity;
        let mut part = update.clone();
        let OrderUpdate::Fill {
            allocation,
            qty,
            fee: part_fee,
            amounts: part_amounts,
            ..
        } = &mut part
        else {
            unreachable!()
        };
        *allocation = None;
        *qty = slice.quantity.to_f64().map_err(|e| e.to_string())?;
        *part_fee = if fee.is_some() {
            slice
                .fee
                .as_ref()
                .map(|fee| fee.amount.value.to_f64().map_err(|e| e.to_string()))
                .transpose()?
        } else {
            None
        };
        if let Some(amounts) = part_amounts {
            amounts.quantity = ExactNumber::derived(slice.quantity.clone());
            amounts.fee = slice.fee.clone();
        }
        result.push((slice.strategy, part));
    }
    if sum != exact_qty {
        return Err("execution allocation does not conserve venue quantity".into());
    }
    if expected_fee.is_some_and(|fee| fee.amount.value != fee_sum) {
        return Err("execution allocation does not conserve venue fee".into());
    }
    Ok(Some(result))
}

pub(crate) fn recovered_update(
    record: &engine_types::WalRecord,
    recv_ns: u64,
) -> Option<engine_types::OrderUpdate> {
    let engine_types::WalRecord::RecoveredFill {
        allocation,
        amounts,
        exec_id,
        client_order_id,
        symbol,
        side,
        qty,
        px,
        fee,
        is_maker,
        forced_close,
        venue_ts_ms,
        ..
    } = record
    else {
        return None;
    };
    Some(engine_types::OrderUpdate::Fill {
        allocation: allocation.clone(),
        amounts: amounts.clone().map(Box::new),
        exec_id: exec_id.clone(),
        client_order_id: client_order_id.clone(),
        symbol: *symbol,
        side: *side,
        qty: *qty,
        px: *px,
        fee: *fee,
        is_maker: *is_maker,
        forced_close: *forced_close,
        venue_ts_ms: *venue_ts_ms,
        recv_ns,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::inventory::InventoryFill;
    use engine_types::numeric::AssetId;
    const SYMBOL: SymbolId = SymbolId(0);
    fn exact(value: &str) -> Exact {
        Exact::parse_decimal(value).unwrap()
    }
    fn inventory() -> Inventory {
        let mut inventory = Inventory::default();
        for (id, side, qty) in [
            (0, Side::Buy, "3"),
            (1, Side::Buy, "2"),
            (2, Side::Sell, "1"),
        ] {
            inventory
                .fill(InventoryFill {
                    strategy: StrategyId(id),
                    symbol: SYMBOL,
                    side,
                    qty: exact(qty),
                    px: Some(exact("10")),
                    stop: None,
                    settlement_asset: AssetId::Named("USDT".into()),
                })
                .unwrap();
        }
        inventory
    }
    fn names() -> Vec<String> {
        vec!["long".into(), "carry".into(), "hedge".into()]
    }

    #[test]
    fn emergency_fill_allocates_fifo_by_stable_key_and_preserves_fee_units() {
        let inventory = inventory();
        let fee = AssetAmount {
            asset: AssetId::Named("BNB".into()),
            amount: ExactNumber::venue_decimal("0.03").unwrap(),
        };
        let plan = allocate(
            &inventory,
            None,
            &names(),
            AllocationInput {
                symbol: SYMBOL,
                side: Side::Sell,
                quantity: &exact("3"),
                fee: Some(&fee),
                forced_close: true,
            },
        )
        .unwrap();
        assert_eq!(
            plan.slices
                .iter()
                .map(|slice| slice.strategy_key.as_str())
                .collect::<Vec<_>>(),
            ["carry", "long"]
        );
        assert_eq!(plan.slices[0].quantity, exact("2"));
        assert_eq!(plan.slices[1].quantity, exact("1"));
        assert_eq!(
            plan.slices[0].fee.as_ref().unwrap().amount.value,
            exact("0.02")
        );
        assert_eq!(
            plan.slices[1].fee.as_ref().unwrap().amount.value,
            exact("0.01")
        );
        assert!(plan
            .slices
            .iter()
            .all(|slice| slice.fee.as_ref().unwrap().asset == fee.asset));
        assert_eq!(
            serde_json::from_slice::<ExecutionAllocation>(&serde_json::to_vec(&plan).unwrap())
                .unwrap(),
            plan
        );
    }

    #[test]
    fn emergency_allocation_does_not_synthetically_close_offsetting_sleeves() {
        let mut inventory = inventory();
        let plan = allocate(
            &inventory,
            None,
            &names(),
            AllocationInput {
                symbol: SYMBOL,
                side: Side::Sell,
                quantity: &exact("4"),
                fee: None,
                forced_close: true,
            },
        )
        .unwrap();
        let batch = inventory
            .prepare_batch(
                plan.slices
                    .iter()
                    .map(|slice| InventoryFill {
                        strategy: slice.strategy,
                        symbol: SYMBOL,
                        side: Side::Sell,
                        qty: slice.quantity.clone(),
                        px: Some(exact("9")),
                        stop: None,
                        settlement_asset: AssetId::Named("USDT".into()),
                    })
                    .collect(),
            )
            .unwrap();
        inventory.apply_batch(batch).unwrap();
        assert!(inventory.net(SYMBOL).is_zero());
        assert_eq!(inventory.gross(SYMBOL), exact("2"));
        assert_eq!(
            inventory
                .position(StrategyId(0), SYMBOL)
                .unwrap()
                .signed_qty,
            exact("1")
        );
        assert_eq!(
            inventory
                .position(StrategyId(2), SYMBOL)
                .unwrap()
                .signed_qty,
            exact("-1")
        );
    }

    #[test]
    fn unknown_or_oversized_forced_execution_cannot_flip_owned_inventory() {
        let inventory = inventory();
        let before = inventory.snapshot();
        for (side, qty, forced) in [
            (Side::Sell, "5", true),
            (Side::Buy, "1", true),
            (Side::Sell, "1", false),
        ] {
            assert!(allocate(
                &inventory,
                None,
                &names(),
                AllocationInput {
                    symbol: SYMBOL,
                    side,
                    quantity: &exact(qty),
                    fee: None,
                    forced_close: forced
                }
            )
            .is_err());
        }
        assert_eq!(inventory.snapshot(), before);
    }

    #[test]
    fn batch_validation_failure_and_stale_replay_leave_all_owners_unchanged() {
        let mut inventory = inventory();
        let make = |id, qty| InventoryFill {
            strategy: StrategyId(id),
            symbol: SYMBOL,
            side: Side::Sell,
            qty: exact(qty),
            px: Some(exact("9")),
            stop: None,
            settlement_asset: AssetId::Named("USDT".into()),
        };
        let before = inventory.snapshot();
        assert!(inventory
            .prepare_batch(vec![make(0, "1"), make(1, "-1")])
            .is_err());
        assert_eq!(inventory.snapshot(), before);
        let prepared = inventory
            .prepare_batch(vec![make(0, "1"), make(1, "1")])
            .unwrap();
        inventory.fill(make(2, "1")).unwrap();
        let changed = inventory.snapshot();
        assert!(inventory.apply_batch(prepared).is_err());
        assert_eq!(inventory.snapshot(), changed);
    }
}
