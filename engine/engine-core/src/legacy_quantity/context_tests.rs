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
            records.push(WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::ClaimsDropped {
                    wall_ts_ms: 9000,
                    rows,
                },
            ));
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

fn legacy_emergency(records: &mut Vec<WalRecord>, quantity: &str, engine_order: bool) {
    let state = Attribution::try_from_records(records).unwrap();
    let side = if state.signed_exact(StrategyId(0), SymbolId(0)).is_positive() {
        Side::Sell
    } else {
        Side::Buy
    };
    let mut rows = fill("legacy-emergency", side, quantity, false);
    let request = if engine_order {
        let WalRecord::OrderSent { request, .. } = &mut rows[0] else {
            unreachable!()
        };
        request.reduce_only = true;
        request.sleeve_effect = Some(
            engine_types::orders::SleeveOrderEffect::EmergencyNetReduction { emergency_id: 1 },
        );
        Some(request.clone())
    } else {
        None
    };
    let WalRecord::OrderUpdate { update, .. } = &mut rows[1] else {
        unreachable!()
    };
    if !engine_order {
        let OrderUpdate::Fill {
            client_order_id,
            forced_close,
            ..
        } = update
        else {
            unreachable!()
        };
        client_order_id.clear();
        *forced_close = Some(engine_types::ForcedClose::StopLoss);
    }
    let step = records
        .iter()
        .any(|row| matches!(row, WalRecord::LegacyQuantityGridAdopted { .. }))
        .then(|| n("0.01"));
    let prepared = state
        .prepare_portfolio_update_on_grid(
            request.as_ref(),
            &["left".into(), "right".into(), "third".into()],
            update,
            step.as_ref(),
        )
        .expect("legacy full close must respect the adopted quantity")
        .unwrap();
    let OrderUpdate::Fill {
        allocation,
        amounts,
        ..
    } = update
    else {
        unreachable!()
    };
    assert!(amounts.is_none());
    *allocation = Some(Box::new(prepared.allocation));
    if engine_order {
        records.push(rows.remove(0));
    }
    records.push(rows.pop().unwrap());
}

fn assert_legacy_emergency_closed(records: &[WalRecord]) {
    let claims = Attribution::try_from_records(records).unwrap().snapshot();
    assert!(claims.positions.is_empty());
    assert!(
        claims.accounting.is_empty(),
        "legacy asset units must remain unknown"
    );
    assert_eq!(claims.unvalued[0].execution_cash_flow_events, 2);
    assert_eq!(claims.unvalued[0].fee_events, 2);
    assert!(reconcile::physical_exposure(records).unwrap().is_empty());
    let fills = Fills::try_from_records(records).unwrap();
    assert!(fills.open_trade_lots().is_empty());
    assert_eq!(fills.closed().len(), 1);
    assert_eq!(
        fills.closed()[0]
            .round_trip
            .as_ref()
            .unwrap()
            .net_usdt_exact,
        -Exact::from_legacy_f64(0.0066016).unwrap() * n("2")
    );
}

#[test]
fn legacy_binary64_emergency_fifo_rederives_an_actual_point_one_full_close() {
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "0.1", false, 1000);
    legacy_emergency(&mut records, "0.1", true);
    assert_legacy_emergency_closed(&records);
    let original = serde_json::to_vec(&records).unwrap();
    records.push(plan(&records));
    assert_legacy_emergency_closed(&records);
    assert_eq!(
        serde_json::to_vec(&records[..records.len() - 1]).unwrap(),
        original
    );
}

#[test]
fn legacy_binary64_forced_full_close_after_grid_adoption_keeps_cash_and_replays() {
    for side in [Side::Buy, Side::Sell] {
        for quantity in ["0.1", "0.3"] {
            let mut records = vec![base(&[], 0.0)];
            add(&mut records, 0, side, quantity, false, 1000);
            records.push(plan(&records));
            legacy_emergency(&mut records, quantity, false);
            assert_legacy_emergency_closed(&records);
            let persisted = serde_json::to_vec(&records).unwrap();
            let restored: Vec<WalRecord> = serde_json::from_slice(&persisted).unwrap();
            assert_legacy_emergency_closed(&restored);
        }
    }
}

#[test]
fn native_sub_ulp_partial_is_not_erased_by_a_legacy_forced_overfill() {
    for opening_side in [Side::Buy, Side::Sell] {
        let closing_side = if opening_side == Side::Buy {
            Side::Sell
        } else {
            Side::Buy
        };
        let mut records = vec![base(&[], 0.0)];
        add(&mut records, 0, opening_side, "0.1", true, 1000);
        add(
            &mut records,
            0,
            closing_side,
            "0.00000000000000000001",
            true,
            2000,
        );
        let claims = Attribution::try_from_records(&records).unwrap();
        let before = claims.snapshot();
        assert_eq!(
            before.positions[0].signed_qty.abs(),
            n("0.09999999999999999999")
        );
        assert_eq!(before.positions[0].signed_qty.abs().to_f64().unwrap(), 0.1);
        let mut rows = fill("unproven-close", closing_side, "0.1", false);
        let WalRecord::OrderUpdate { update, .. } = &mut rows[1] else {
            unreachable!()
        };
        let OrderUpdate::Fill {
            client_order_id,
            forced_close,
            ..
        } = update
        else {
            unreachable!()
        };
        client_order_id.clear();
        *forced_close = Some(engine_types::ForcedClose::StopLoss);
        assert!(claims
            .prepare_portfolio_update_on_grid(None, &["left".into()], update, Some(&n("0.01")))
            .is_err());
        assert_eq!(claims.snapshot(), before);

        let mut rows = fill(
            "known-partial",
            closing_side,
            "0.09999999999999999998",
            true,
        );
        let WalRecord::OrderUpdate { update, .. } = &mut rows[1] else {
            unreachable!()
        };
        let OrderUpdate::Fill {
            client_order_id,
            forced_close,
            ..
        } = update
        else {
            unreachable!()
        };
        client_order_id.clear();
        *forced_close = Some(engine_types::ForcedClose::StopLoss);
        let prepared = claims
            .prepare_portfolio_update_for_order(None, &["left".into()], update)
            .unwrap()
            .unwrap();
        let mut claims = claims;
        claims.commit_portfolio_fill(prepared, true).unwrap();
        assert_eq!(
            claims.signed_exact(StrategyId(0), SymbolId(0)).abs(),
            n("0.00000000000000000001")
        );
    }
}

#[test]
fn allocated_native_btc_fee_remains_unpriced_in_usdt_round_trips() {
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "1", true, 1000);
    forced(&mut records, "1");
    let WalRecord::OrderUpdate {
        update:
            OrderUpdate::Fill {
                amounts: Some(amounts),
                allocation: Some(allocation),
                ..
            },
        ..
    } = records.last_mut().unwrap()
    else {
        unreachable!()
    };
    amounts.fee.as_mut().unwrap().asset = engine_types::numeric::AssetId::Named("BTC".into());
    allocation.slices[0].fee.as_mut().unwrap().asset =
        engine_types::numeric::AssetId::Named("BTC".into());
    let claims = Attribution::try_from_records(&records).unwrap().snapshot();
    assert!(claims.positions.is_empty());
    assert_eq!(
        claims
            .accounting
            .iter()
            .find(|row| row.asset == engine_types::numeric::AssetId::Named("BTC".into()))
            .unwrap()
            .fees,
        n("0.0066016")
    );
    let fills = Fills::try_from_records(&records).unwrap();
    assert_eq!(fills.closed().len(), 1);
    assert!(fills.closed()[0].round_trip.is_none());
    assert_eq!(
        fills.closed()[0].unpriced,
        Some(engine_types::risk::UnpricedTradeReason::FeeValue)
    );
}

#[test]
fn current_binary64_full_close_keeps_its_quantity_before_any_grid_adoption() {
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "0.1", false, 1000);
    let mut claims = Attribution::try_from_records(&records).unwrap();
    let mut rows = fill("before-adoption", Side::Sell, "0.1", false);
    let WalRecord::OrderUpdate { update, .. } = &mut rows[1] else {
        unreachable!()
    };
    let OrderUpdate::Fill {
        client_order_id,
        forced_close,
        ..
    } = update
    else {
        unreachable!()
    };
    client_order_id.clear();
    *forced_close = Some(engine_types::ForcedClose::StopLoss);
    let prepared = claims
        .prepare_portfolio_update_on_grid(None, &["left".into()], update, Some(&n("0.01")))
        .unwrap()
        .unwrap();
    assert_eq!(
        prepared.allocation.slices[0].quantity,
        Exact::from_legacy_f64(0.1).unwrap()
    );
    assert!(prepared.allocation.legacy_quantity_step.is_none());
    claims.commit_portfolio_fill(prepared, true).unwrap();
    assert!(claims.snapshot().positions.is_empty());
}

#[test]
fn legacy_grid_receipts_reject_invalid_steps_native_amounts_and_changed_slice_totals() {
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "0.1", false, 1000);
    records.push(plan(&records));
    legacy_emergency(&mut records, "0.1", false);
    for mutation in 0..6 {
        let mut invalid = records.clone();
        let WalRecord::OrderUpdate {
            update:
                OrderUpdate::Fill {
                    amounts,
                    qty,
                    allocation: Some(allocation),
                    ..
                },
            ..
        } = invalid.last_mut().unwrap()
        else {
            unreachable!()
        };
        match mutation {
            0 => allocation.legacy_quantity_step = Some(Exact::zero()),
            1 => allocation.legacy_quantity_step = Some(n("0.03")),
            2 => allocation.slices[0].quantity = n("0.09"),
            3 => allocation.legacy_quantity_step = Some(n("0.00000000000000001")),
            4 => *qty = 0.101,
            _ => {
                let native = fill("native", Side::Sell, "0.1", true);
                let WalRecord::OrderUpdate {
                    update:
                        OrderUpdate::Fill {
                            amounts: source, ..
                        },
                    ..
                } = &native[1]
                else {
                    unreachable!()
                };
                *amounts = source.clone();
            }
        }
        assert!(
            Attribution::try_from_records(&invalid).is_err(),
            "mutation {mutation}"
        );
        assert!(
            Fills::try_from_records(&invalid).is_err(),
            "mutation {mutation}"
        );
    }
    assert_legacy_emergency_closed(&records);
}

#[test]
fn recovered_legacy_grid_receipt_keeps_the_delivered_fill_quantity_and_economics() {
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "0.1", false, 1000);
    records.push(plan(&records));
    let claims = Attribution::try_from_records(&records).unwrap();
    legacy_emergency(&mut records, "0.1", false);
    let delivered = Fills::try_from_records(&records).unwrap().closed().to_vec();
    let WalRecord::OrderUpdate {
        update:
            OrderUpdate::Fill {
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
            },
        ..
    } = records.pop().unwrap()
    else {
        unreachable!()
    };
    let mut recovered = WalRecord::RecoveredFill {
        callbacks: None,
        allocation: None,
        amounts: amounts.map(|amounts| *amounts),
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
        recovered_wall_ts_ms: 4000,
    };
    let prepared = claims
        .prepare_portfolio_recovered_on_grid(None, &["left".into()], &recovered, Some(&n("0.01")))
        .unwrap()
        .unwrap();
    assert_eq!(Some(&prepared.allocation), allocation.as_deref());
    let WalRecord::RecoveredFill { allocation, .. } = &mut recovered else {
        unreachable!()
    };
    *allocation = Some(Box::new(prepared.allocation));
    records.push(recovered);
    assert_legacy_emergency_closed(&records);
    assert_eq!(
        Fills::try_from_records(&records).unwrap().closed(),
        delivered
    );
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

#[test]
fn a_legacy_close_on_an_adopted_sleeve_leaves_the_live_and_replayed_row_alike() {
    let names: Vec<String> = ["left", "right", "third"]
        .iter()
        .map(|key| (*key).to_string())
        .collect();
    let mut records = vec![base(&[], 0.0)];
    add(&mut records, 0, Side::Buy, "0.1", false, 1000);
    records.push(plan(&records));
    assert_eq!(
        Attribution::try_from_records(&records)
            .unwrap()
            .signed_exact(StrategyId(0), SymbolId(0)),
        n("0.1"),
        "adoption did not put the sleeve on the venue grid"
    );

    let closing = fill("legacy-close", Side::Sell, "0.1", false);
    let (WalRecord::OrderSent { request, .. }, WalRecord::OrderUpdate { update, .. }) =
        (&closing[0], &closing[1])
    else {
        unreachable!()
    };
    let mut live = Attribution::try_from_records(&records).unwrap();
    let prepared = live
        .prepare_portfolio_update_for_order(Some(request), &names, update)
        .unwrap()
        .unwrap();
    live.commit_portfolio_fill(prepared, false).unwrap();

    records.extend(closing);
    let replayed = Attribution::try_from_records(&records).unwrap();
    assert_eq!(live.snapshot(), replayed.snapshot());
    assert!(live.snapshot().positions.is_empty());
    assert_eq!(
        live.validate_sleeve_stop_exact(StrategyId(0), SymbolId(0), Side::Sell, &n("200")),
        Err("sleeve stop has no owned position".into()),
        "the live engine can still price a sleeve stop off binary64 rounding"
    );
}
