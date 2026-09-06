use super::tests::{base, fill, plan, specs};
use super::*;
use crate::{attribution::Attribution, execution::Fills, reconcile};
use engine_types::{OrderUpdate, Side, StrategyId, WalRecord};

fn n(value: &str) -> Exact {
    Exact::parse_decimal(value).unwrap()
}

fn add(
    records: &mut Vec<WalRecord>,
    owner: u16,
    side: Side,
    quantity: &str,
    canonical: bool,
    timestamp: i64,
) {
    let mut rows = fill(
        &format!("fill-{}", records.len()),
        side,
        quantity,
        canonical,
    );
    if let WalRecord::OrderSent { request, .. } = &mut rows[0] {
        request.strategy = StrategyId(owner);
    }
    if let WalRecord::OrderUpdate {
        update: OrderUpdate::Fill { venue_ts_ms, .. },
        ..
    } = &mut rows[1]
    {
        *venue_ts_ms = timestamp;
    }
    records.extend(rows);
}

fn priced_prefix() -> Vec<WalRecord> {
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "0.1", false, 1000);
    add(&mut records, 0, Side::Buy, "0.2", false, 2000);
    records
}

fn first_close_net(quantity: &str) -> Exact {
    let cost =
        (Exact::from_legacy_f64(0.1).unwrap() + Exact::from_legacy_f64(0.2).unwrap()) * n("100");
    let fees = Exact::from_legacy_f64(0.0066016).unwrap() * n("2")
        + n("0.0066016") * n("0.3").checked_div(&n(quantity)).unwrap();
    n("30") - cost - fees
}

#[test]
fn distinct_native_cycles_and_reopening_keep_their_own_costs_and_timestamps() {
    let mut records = priced_prefix();
    add(&mut records, 0, Side::Sell, "0.3", true, 3000);
    add(&mut records, 0, Side::Buy, "1", true, 4000);
    add(&mut records, 0, Side::Sell, "1", true, 5000);
    add(&mut records, 0, Side::Buy, "0.01", true, 6000);
    let original = serde_json::to_vec(&records).unwrap();
    records.push(plan(&records));
    let fills = Fills::try_from_records(&records).unwrap();
    assert_eq!(
        fills
            .closed()
            .iter()
            .map(|trade| trade.closed_ms)
            .collect::<Vec<_>>(),
        vec![3000, 5000]
    );
    assert_eq!(
        fills.closed()[0]
            .round_trip
            .as_ref()
            .unwrap()
            .net_usdt_exact,
        first_close_net("0.3")
    );
    assert_eq!(
        fills.closed()[1]
            .round_trip
            .as_ref()
            .unwrap()
            .net_usdt_exact,
        -n("0.0132032")
    );
    let lot = fills.open_trade_lots().remove(0);
    assert_eq!(
        (
            lot.signed_qty,
            lot.in_qty,
            lot.cash,
            lot.fees,
            lot.opened_ms
        ),
        (n("0.01"), n("0.01"), -n("1"), Some(n("0.0066016")), 6000)
    );
    let positions = Attribution::try_from_records(&records)
        .unwrap()
        .snapshot()
        .positions;
    assert_eq!(positions[0].signed_qty, n("0.01"));
    assert_eq!(positions[0].entry_value, Some(n("1")));
    assert_eq!(
        serde_json::to_vec(&records[..records.len() - 1]).unwrap(),
        original
    );
}

#[test]
fn native_reversal_splits_the_actual_whole_fee_and_preserves_canonical_opening_basis() {
    for quantity in ["0.31", "0.300000000000000001"] {
        let mut records = priced_prefix();
        add(&mut records, 0, Side::Sell, quantity, true, 3000);
        records.push(plan(&records));
        let fills = Fills::try_from_records(&records).unwrap();
        assert_eq!(fills.closed().len(), 1);
        let closed = &fills.closed()[0];
        assert_eq!(closed.closed_ms, 3000);
        assert_eq!(
            closed.round_trip.as_ref().unwrap().net_usdt_exact,
            first_close_net(quantity)
        );
        let remaining = n(quantity) - n("0.3");
        let lot = fills.open_trade_lots().remove(0);
        assert_eq!(lot.signed_qty, -&remaining);
        assert_eq!(lot.in_qty, remaining);
        assert_eq!(lot.in_value, &remaining * n("100"));
        assert_eq!(lot.cash, &remaining * n("100"));
        assert_eq!(
            lot.fees,
            Some(n("0.0066016") * remaining.checked_div(&n(quantity)).unwrap())
        );
        let mut restored = crate::execution::roundtrip::Lots::default();
        restored.restore(&fills.open_trade_lots()).unwrap();
        assert_eq!(restored.checkpoint(), fills.open_trade_lots());
    }
}

#[test]
fn a_later_legacy_close_or_claim_drop_cannot_erase_a_witnessed_native_close() {
    for drop_claim in [false, true] {
        let mut records = priced_prefix();
        add(&mut records, 0, Side::Sell, "0.3", true, 3000);
        if drop_claim {
            let rows = Attribution::try_from_records(&records)
                .unwrap()
                .rows()
                .into_iter()
                .map(|(strategy, symbol, signed_qty)| engine_types::FilledTotal {
                    strategy,
                    symbol,
                    signed_qty,
                })
                .collect();
            records.push(WalRecord::ClaimsDropped {
                wall_ts_ms: 9000,
                rows,
            });
        } else {
            add(&mut records, 0, Side::Buy, "1", false, 4000);
            add(&mut records, 0, Side::Sell, "1", false, 5000);
        }
        assert!(Attribution::try_from_records(&records)
            .unwrap()
            .legacy_quantities
            .is_empty());
        records.push(plan(&records));
        let fills = Fills::try_from_records(&records).unwrap();
        assert_eq!(fills.closed()[0].closed_ms, 3000);
        assert_eq!(
            fills.closed()[0]
                .round_trip
                .as_ref()
                .unwrap()
                .net_usdt_exact,
            first_close_net("0.3")
        );
        assert_eq!(fills.closed().len(), if drop_claim { 1 } else { 2 });
    }
}

fn forced(records: &mut Vec<WalRecord>, quantity: &str) {
    let rows = fill("forced", Side::Sell, quantity, true);
    let WalRecord::OrderUpdate {
        mut update,
        callbacks,
    } = rows[1].clone()
    else {
        unreachable!()
    };
    if let OrderUpdate::Fill {
        client_order_id,
        forced_close,
        ..
    } = &mut update
    {
        client_order_id.clear();
        *forced_close = Some(engine_types::ForcedClose::StopLoss);
    }
    let state = Attribution::try_from_records(records).unwrap();
    let names = vec!["left".to_string(), "right".to_string(), "third".to_string()];
    let prepared = state
        .prepare_portfolio_update_for_order(None, &names, &update)
        .unwrap()
        .unwrap();
    if let OrderUpdate::Fill { allocation, .. } = &mut update {
        *allocation = Some(Box::new(prepared.allocation));
    }
    records.push(WalRecord::OrderUpdate { update, callbacks });
}

#[test]
fn legacy_fifo_slices_rederive_from_valid_original_receipts_with_exact_whole_fee_conservation() {
    for opposing in [false, true] {
        let mut records = vec![base(&[], 0.0)];
        add(&mut records, 0, Side::Buy, "0.1", false, 1000);
        add(&mut records, 1, Side::Buy, "0.2", false, 2000);
        if opposing {
            add(&mut records, 2, Side::Sell, "0.1", false, 2500);
        }
        forced(&mut records, if opposing { "0.2" } else { "0.3" });
        let original = serde_json::to_vec(&records).unwrap();
        let adoption = plan(&records);
        records.push(adoption.clone());
        let state = Attribution::try_from_records(&records).unwrap().snapshot();
        let quantities: Vec<_> = state
            .positions
            .iter()
            .map(|row| (row.strategy, row.signed_qty.clone()))
            .collect();
        assert_eq!(
            quantities,
            if opposing {
                vec![(StrategyId(1), n("0.1")), (StrategyId(2), -n("0.1"))]
            } else {
                vec![]
            }
        );
        let known_fee: Exact = state
            .accounting
            .iter()
            .map(|row| row.fees.clone())
            .fold(Exact::zero(), |sum, fee| sum + fee);
        assert_eq!(known_fee, n("0.0066016"));
        assert!(reconcile::physical_exposure(&records).unwrap().is_empty());
        assert_eq!(
            serde_json::to_vec(&records[..records.len() - 1]).unwrap(),
            original
        );
        let mut bad = records.clone();
        if let WalRecord::OrderUpdate {
            update:
                OrderUpdate::Fill {
                    allocation: Some(allocation),
                    ..
                },
            ..
        } = &mut bad[records.len() - 2]
        {
            allocation.slices[0].quantity += n("0.01");
        }
        assert!(
            Attribution::try_from_records(&bad).is_err(),
            "adoption accepted an invalid original FIFO receipt"
        );
    }
}

fn offset(records: &mut Vec<WalRecord>, timestamp: i64) {
    use engine_types::numeric::AssetId;
    use engine_types::portfolio_control::{PortfolioOffsetSettlement, PortfolioOffsetSlice};
    let state = Attribution::try_from_records(records).unwrap().snapshot();
    records.push(WalRecord::PortfolioOffsetSettled {
        settlement: PortfolioOffsetSettlement {
            emergency_id: 1,
            symbol: SymbolId(0),
            price: n("99.1"),
            settled_ms: timestamp,
            slices: state
                .positions
                .into_iter()
                .map(|row| PortfolioOffsetSlice {
                    strategy: row.strategy,
                    signed_quantity: -row.signed_qty,
                    settlement_asset: AssetId::Named("USDT".into()),
                })
                .collect(),
        },
    });
}

#[test]
fn internal_offsets_preserve_real_prior_closes_and_still_close_corrected_balanced_owners() {
    let mut records = priced_prefix();
    add(&mut records, 1, Side::Sell, "0.1", false, 2100);
    add(&mut records, 1, Side::Sell, "0.2", false, 2200);
    add(&mut records, 0, Side::Sell, "0.3", true, 3000);
    add(&mut records, 1, Side::Buy, "0.3", true, 4000);
    add(&mut records, 0, Side::Buy, "1", true, 5000);
    add(&mut records, 1, Side::Sell, "1", true, 6000);
    offset(&mut records, 9000);
    records.push(plan(&records));
    let fills = Fills::try_from_records(&records).unwrap();
    assert_eq!(
        fills
            .closed()
            .iter()
            .map(|trade| trade.closed_ms)
            .collect::<Vec<_>>(),
        vec![3000, 4000, 9000, 9000]
    );
    assert!(fills.closed()[..2]
        .iter()
        .all(|trade| trade.internal_settlement.is_none()));
    assert!(fills.closed()[2..]
        .iter()
        .all(|trade| trade.internal_settlement == Some(1)));
    assert!(fills.open_trade_lots().is_empty());
    let state = Attribution::try_from_records(&records).unwrap().snapshot();
    assert!(state.positions.is_empty());
    assert!(state
        .internal_settlements
        .iter()
        .map(|row| row.cash_flow.clone())
        .fold(Exact::zero(), |sum, cash| sum + cash)
        .is_zero());
    assert_eq!(state.internal_settlements[0].cash_flow, n("99.1"));
}

#[test]
fn a_pure_legacy_full_binary_offset_has_no_adopted_sleeve_context() {
    let mut records = vec![base(
        &[(0, 0.30000000000000004), (1, -0.30000000000000004)],
        0.0,
    )];
    offset(&mut records, 9000);
    let original = Fills::try_from_records(&records).unwrap().closed().to_vec();
    if let Some(adoption) = super::plan(&records, &specs(), 10_000).unwrap() {
        assert!(
            matches!(&adoption,WalRecord::LegacyQuantityGridAdopted { sleeves,.. } if sleeves.is_empty())
        );
        records.push(adoption);
    }
    assert_eq!(
        Fills::try_from_records(&records).unwrap().closed(),
        original
    );
}

#[test]
fn duplicate_missing_and_wrong_terminal_grid_contexts_are_rejected() {
    let mut records = priced_prefix();
    add(&mut records, 0, Side::Sell, "0.3", true, 3000);
    let valid = plan(&records);
    for mutation in 0..4 {
        let mut record = valid.clone();
        if let WalRecord::LegacyQuantityGridAdopted {
            sleeves, version, ..
        } = &mut record
        {
            match mutation {
                0 => sleeves.push(sleeves[0].clone()),
                1 => sleeves.clear(),
                2 => sleeves[0].after = n("1"),
                _ => *version = 1,
            }
        }
        let mut bad = records.clone();
        bad.push(record);
        assert!(Attribution::try_from_records(&bad).is_err());
    }
}

#[test]
fn internal_offset_cannot_erase_an_authoritative_canonical_microscopic_position() {
    let mut records = priced_prefix();
    add(&mut records, 0, Side::Sell, "0.3", true, 3000);
    let delta = Attribution::try_from_records(&records)
        .unwrap()
        .signed_exact(StrategyId(0), SymbolId(0));
    assert!(delta.is_positive());
    add(
        &mut records,
        1,
        Side::Sell,
        &delta.to_decimal_string().unwrap(),
        true,
        4000,
    );
    let native = records.last().unwrap().clone();
    offset(&mut records, 9000);
    assert!(Attribution::try_from_records(&records)
        .unwrap()
        .snapshot()
        .positions
        .is_empty());
    let original = serde_json::to_vec(&records).unwrap();
    let error = crate::legacy_quantity::plan(&records, &specs(), 10000).unwrap_err();
    assert_eq!(error, "invalid internal settlement");
    assert_eq!(serde_json::to_vec(&records).unwrap(), original);
    assert_eq!(records[records.len() - 2], native);
    assert!(matches!(native, WalRecord::OrderUpdate {
        update: OrderUpdate::Fill { amounts: Some(amounts), .. }, ..
    } if amounts.quantity.value == delta));
}
