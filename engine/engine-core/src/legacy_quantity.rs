mod replay;
pub(crate) use replay::{canonical_symbol, Event, Replay};

use std::collections::BTreeMap;

use engine_types::numeric::{Exact, ExactInstrumentSpec};
use engine_types::{LegacyPhysicalQuantityCorrection, SymbolId};

#[derive(Clone, Debug, Default)]
pub(crate) struct Origin {
    quantity: Exact,
    uncertainty: Exact,
}

impl Origin {
    pub(crate) fn note(&mut self, signed: &Exact) -> Result<(), String> {
        let value = signed.abs().to_f64().map_err(|e| e.to_string())?;
        let next = f64::from_bits(
            value
                .to_bits()
                .checked_add(1)
                .ok_or("legacy quantity overflow")?,
        );
        let ulp = if next.is_finite() {
            Exact::from_legacy_f64(next).map_err(|e| e.to_string())?
                - Exact::from_legacy_f64(value).map_err(|e| e.to_string())?
        } else {
            Exact::from_legacy_f64(value).map_err(|e| e.to_string())?
                - Exact::from_legacy_f64(f64::from_bits(value.to_bits() - 1))
                    .map_err(|e| e.to_string())?
        };
        self.quantity += signed;
        self.uncertainty += ulp * Exact::parse_decimal("64").unwrap();
        self.quantity
            .validate_storage()
            .map_err(|e| e.to_string())?;
        self.uncertainty
            .validate_storage()
            .map_err(|e| e.to_string())
    }

    pub(crate) fn resolve(&self, current: &Exact, step: &Exact) -> Result<Exact, String> {
        let low = (&self.quantity - &self.uncertainty)
            .ceil_to(step)
            .map_err(|e| e.to_string())?;
        let high = (&self.quantity + &self.uncertainty)
            .floor_to(step)
            .map_err(|e| e.to_string())?;
        if low != high {
            return Err(
                "legacy quantity has no unique venue-grid value within 64 ULPs per input".into(),
            );
        }
        let corrected = current + &(low - &self.quantity);
        corrected.validate_storage().map_err(|e| e.to_string())?;
        corrected.to_f64().map_err(|e| e.to_string())?;
        Ok(corrected)
    }
}

pub(crate) fn step(
    specs: &BTreeMap<SymbolId, ExactInstrumentSpec>,
    symbol: SymbolId,
) -> Result<Exact, String> {
    specs
        .get(&symbol)
        .and_then(|spec| spec.qty_step.clone())
        .filter(Exact::is_positive)
        .ok_or_else(|| "legacy quantity has no native quantity grid".into())
}

pub(crate) fn apply_physical(
    exposure: &mut crate::reconcile::PhysicalExposure,
    origins: &mut BTreeMap<SymbolId, Origin>,
    corrections: &[LegacyPhysicalQuantityCorrection],
) -> Result<(), String> {
    if corrections.len() != origins.len() {
        return Err("legacy physical adoption changes its eligible symbol set".into());
    }
    let mut next = BTreeMap::new();
    for row in corrections {
        let before = exposure.get(&row.symbol).cloned().unwrap_or_default();
        let origin = origins
            .get(&row.symbol)
            .ok_or("physical adoption has no legacy quantity")?;
        if row.before != before
            || row.after != origin.resolve(&before, &row.step)?
            || next.insert(row.symbol, row.after.clone()).is_some()
        {
            return Err(
                "physical adoption changes its original quantity or grid resolution".into(),
            );
        }
    }
    for (symbol, after) in next {
        if after.is_zero() {
            exposure.remove(&symbol);
        } else {
            exposure.insert(symbol, after);
        }
    }
    origins.clear();
    Ok(())
}

pub(crate) fn physical_cut(
    exposure: &mut crate::reconcile::PhysicalExposure,
    origins: &mut BTreeMap<SymbolId, Origin>,
    delta: &mut BTreeMap<SymbolId, Exact>,
    used: &mut std::collections::BTreeSet<SymbolId>,
    context: &[LegacyPhysicalQuantityCorrection],
    symbol: Option<SymbolId>,
    terminal: Option<bool>,
) -> Result<(), String> {
    let mut selected: BTreeMap<_, _> = origins
        .iter()
        .filter(|(known, _)| symbol.is_none_or(|symbol| symbol == **known))
        .map(|(symbol, origin)| (*symbol, origin.clone()))
        .collect();
    let mut corrections = Vec::new();
    for (symbol, origin) in &selected {
        used.insert(*symbol);
        let row = context
            .iter()
            .find(|row| row.symbol == *symbol)
            .ok_or("missing legacy physical grid context")?;
        let before = exposure.get(symbol).cloned().unwrap_or_default();
        corrections.push(LegacyPhysicalQuantityCorrection {
            symbol: *symbol,
            after: origin.resolve(&before, &row.step)?,
            before,
            step: row.step.clone(),
        });
    }
    apply_physical(exposure, &mut selected, &corrections)?;
    for row in corrections {
        origins.remove(&row.symbol);
        *delta.entry(row.symbol).or_default() += row.after - row.before;
    }
    if let Some(validate_terminal) = terminal {
        if *used != context.iter().map(|row| row.symbol).collect() {
            return Err("legacy physical context changes its eligible symbols".into());
        }
        if validate_terminal {
            for row in context {
                let after = exposure.get(&row.symbol).cloned().unwrap_or_default();
                let before = &after - delta.get(&row.symbol).cloned().unwrap_or_default();
                if row.after != after || row.before != before {
                    return Err("legacy physical context changes its terminal quantities".into());
                }
            }
        }
    }
    Ok(())
}

pub(crate) fn plan(
    records: &[engine_types::WalRecord],
    specs: &BTreeMap<SymbolId, ExactInstrumentSpec>,
    wall_ts_ms: i64,
) -> Result<Option<engine_types::WalRecord>, String> {
    use engine_types::{LegacySleeveQuantityCorrection, WalRecord};
    let mut replay = Replay::new(records, None)?;
    while replay.next()?.is_some() {}
    let original = replay.state().snapshot();
    let (physical, _, _, physical_keys) =
        crate::reconcile::position_state_with_adoption(records, None, false)?;
    let sleeves = replay
        .discovered
        .iter()
        .map(|&(strategy, symbol)| {
            let before = original
                .positions
                .iter()
                .find(|row| (row.strategy, row.symbol) == (strategy, symbol))
                .map(|row| row.signed_qty.clone())
                .unwrap_or_default();
            Ok(LegacySleeveQuantityCorrection {
                strategy,
                symbol,
                after: before.clone(),
                before,
                step: step(specs, symbol)?,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    let physical = physical_keys
        .into_iter()
        .map(|symbol| {
            let before = physical.get(&symbol).cloned().unwrap_or_default();
            Ok(LegacyPhysicalQuantityCorrection {
                symbol,
                after: before.clone(),
                before,
                step: step(specs, symbol)?,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    if sleeves.is_empty() && physical.is_empty() {
        return Ok(None);
    }
    let mut record = WalRecord::LegacyQuantityGridAdopted {
        version: 2,
        wall_ts_ms,
        sleeves,
        physical,
    };
    let normalized = Replay::with_planning(records, Some(&record), true)?
        .finish()?
        .snapshot();
    let normalized_physical =
        crate::reconcile::position_state_with_adoption(records, Some(&record), true)?.0;
    if let WalRecord::LegacyQuantityGridAdopted {
        sleeves, physical, ..
    } = &mut record
    {
        for row in sleeves {
            row.after = normalized
                .positions
                .iter()
                .find(|position| (position.strategy, position.symbol) == (row.strategy, row.symbol))
                .map(|position| position.signed_qty.clone())
                .unwrap_or_default();
        }
        for row in physical {
            row.after = normalized_physical
                .get(&row.symbol)
                .cloned()
                .unwrap_or_default();
        }
    }
    Ok(Some(record))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{attribution::Attribution, execution::Fills, reconcile};
    use engine_types::{OrderKind, OrderRequest, OrderUpdate, Side, StrategyId, WalRecord};

    fn exact(value: &str) -> Exact {
        Exact::parse_decimal(value).unwrap()
    }

    pub(super) fn base(rows: &[(u16, f64)], physical: f64) -> WalRecord {
        serde_json::from_value(serde_json::json!({
            "kind":"segment_base", "wall_ts_ms":1, "strategies":["left","right","third"], "symbols":["BTCUSDT"],
            "may_open":true, "control_anchors":[], "open_orders":[],
            "attribution":rows.iter().map(|(strategy,qty)|serde_json::json!({"strategy":strategy,"symbol":0,"signed_qty":qty})).collect::<Vec<_>>(),
            "logged_exposure":[{"symbol":0,"signed_qty":physical}],
            "intended_stops":[{"symbol":0,"side":"Buy","trigger_px":90.0}]
        })).unwrap()
    }

    pub(super) fn specs() -> BTreeMap<SymbolId, ExactInstrumentSpec> {
        let mut spec = crate::tests::shared_sleeves::spec();
        spec.qty_step = Some(exact("0.01"));
        BTreeMap::from([(SymbolId(0), spec)])
    }

    pub(super) fn fill(id: &str, side: Side, qty: &str, canonical: bool) -> Vec<WalRecord> {
        use engine_types::numeric::{AssetAmount, AssetId, ExactNumber, ExecutionAmounts};
        let quantity = exact(qty).to_f64().unwrap();
        vec![
            WalRecord::OrderSent {
                dispatch: None,
                request: OrderRequest {
                    client_order_id: id.into(),
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side,
                    qty: quantity,
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: false,
                    close_position: false,
                    sleeve_effect: None,
                    exact_terms: None,
                },
                wire_ns: 1,
                arrival_mid: 100.0,
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Fill {
                    allocation: None,
                    amounts: canonical.then(|| {
                        Box::new(ExecutionAmounts {
                            quantity: ExactNumber::venue_decimal(qty).unwrap(),
                            price: ExactNumber::venue_decimal("100").unwrap(),
                            fee: Some(AssetAmount {
                                asset: AssetId::Named("USDT".into()),
                                amount: ExactNumber::venue_decimal("0.0066016").unwrap(),
                            }),
                            settlement_asset: AssetId::Named("USDT".into()),
                        })
                    }),
                    exec_id: id.into(),
                    client_order_id: id.into(),
                    symbol: SymbolId(0),
                    side,
                    qty: quantity,
                    px: 100.0,
                    fee: Some(0.0066016),
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: 2,
                    recv_ns: 2,
                },
            },
        ]
    }

    pub(super) fn plan(records: &[WalRecord]) -> WalRecord {
        super::plan(records, &specs(), 3).unwrap().unwrap()
    }

    #[test]
    fn legacy_intervals_require_one_quantum_and_preserve_canonical_suffixes() {
        for (legacy, expected) in [
            (0.2899999999999999, "0.29"),
            (1.7000000000000002, "1.7"),
            (221.6, "221.6"),
            (-0.2899999999999999, "-0.29"),
        ] {
            let raw = Exact::from_legacy_f64(legacy).unwrap();
            let mut origin = Origin::default();
            origin.note(&raw).unwrap();
            let suffix = exact("0.000000000000000001");
            assert_eq!(
                origin.resolve(&(&raw + &suffix), &exact("0.01")).unwrap(),
                exact(expected) + suffix
            );
        }
        for value in [f64::MAX, -f64::MAX, 9_007_199_254_740_992.0] {
            let raw = Exact::from_legacy_f64(value).unwrap();
            let mut origin = Origin::default();
            origin.note(&raw).unwrap();
            assert!(origin.resolve(&raw, &Exact::one()).is_err());
        }
        for value in [0.0, -0.0, f64::from_bits(1), -f64::from_bits(1)] {
            let raw = Exact::from_legacy_f64(value).unwrap();
            let mut origin = Origin::default();
            origin.note(&raw).unwrap();
            assert_eq!(origin.resolve(&raw, &exact("0.01")).unwrap(), Exact::zero());
        }
        let raw = Exact::from_legacy_f64(0.1234).unwrap();
        let mut origin = Origin::default();
        origin.note(&raw).unwrap();
        assert!(origin.resolve(&raw, &exact("0.01")).is_err());
    }

    #[test]
    fn priced_legacy_rounding_residual_closes_at_the_real_fill_without_losing_cash() {
        let mut records = vec![base(&[], 0.0)];
        records.extend(fill("legacy-buy-one", Side::Buy, "0.1", false));
        records.extend(fill("legacy-buy-two", Side::Buy, "0.2", false));
        let mut closing = fill("native-close", Side::Sell, "0.3", true);
        if let WalRecord::OrderUpdate {
            update: OrderUpdate::Fill { venue_ts_ms, .. },
            ..
        } = closing.last_mut().unwrap()
        {
            *venue_ts_ms = 17_000;
        }
        records.extend(closing);
        let before = Fills::try_from_records(&records).unwrap();
        let lot = before.open_trade_lots().remove(0);
        assert!(lot.priced && lot.signed_qty.is_positive());
        assert!(before.closed().is_empty());
        let expected_net = &lot.cash - lot.fees.as_ref().unwrap();
        let adoption = plan(&records);
        assert!(
            matches!(&adoption, WalRecord::LegacyQuantityGridAdopted { sleeves, .. } if sleeves[0].after.is_zero())
        );
        records.push(adoption);
        let after = Fills::try_from_records(&records).unwrap();
        assert!(after.open_trade_lots().is_empty());
        assert_eq!(
            after.closed().len(),
            1,
            "adoption discarded a priced closing lot"
        );
        let trade = &after.closed()[0];
        assert_eq!(trade.closed_ms, 17_000);
        assert_eq!(
            trade.round_trip.as_ref().unwrap().net_usdt_exact,
            expected_net
        );
        assert_eq!(trade.fills, 3);
        assert_eq!(trade.loss_row().unwrap().closed_ms, 17_000);
        assert_eq!(
            Fills::try_from_records(&records).unwrap().closed(),
            after.closed()
        );
    }

    #[test]
    fn legacy_and_canonical_partial_fills_keep_cash_cost_unknowns_and_stops_across_replay() {
        let mut records = vec![base(&[(0, 0.2899999999999999)], 0.2899999999999999)];
        records.extend(fill("legacy-sale", Side::Sell, "0.01", false));
        records.extend(fill(
            "canonical-part",
            Side::Buy,
            "0.000000000000000001",
            true,
        ));
        let before = Attribution::try_from_records(&records).unwrap().snapshot();
        let lots_before = Fills::try_from_records(&records).unwrap().open_trade_lots();
        let stops_before = reconcile::intended_stops(&records).unwrap();
        let adoption = plan(&records);
        records.push(adoption.clone());
        let after = Attribution::try_from_records(&records).unwrap().snapshot();
        assert_eq!(after.positions[0].signed_qty, exact("0.280000000000000001"));
        assert_eq!(after.accounting, before.accounting);
        assert_eq!(after.unvalued, before.unvalued);
        assert_eq!(after.internal_settlements, before.internal_settlements);
        assert_eq!(
            after.positions[0].entry_value,
            before.positions[0].entry_value
        );
        assert_eq!(after.positions[0].stop_px, before.positions[0].stop_px);
        let mut expected = lots_before;
        expected[0].signed_qty = exact("0.280000000000000001");
        expected[0].exact_quantity = true;
        let lots = Fills::try_from_records(&records).unwrap().open_trade_lots();
        assert_eq!(lots, expected);
        assert_eq!(reconcile::intended_stops(&records).unwrap(), stops_before);
        assert_eq!(
            reconcile::physical_exposure(&records).unwrap()[&SymbolId(0)],
            exact("0.280000000000000001")
        );
        let ledger = crate::inflight::LedgerOfOrders::try_from_records(&records).unwrap();
        let account = engine_types::AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: vec![],
            observed_ns: 1,
        };
        let findings = reconcile::reconcile(
            &ledger,
            &records,
            &[],
            &account,
            |_| Some(SymbolId(0)),
            |_| Some(0.01),
            |_| Some(0.1),
        )
        .unwrap();
        assert!(!findings
            .findings
            .iter()
            .any(|row| matches!(row, reconcile::Finding::ForeignFill { .. })));
        records.push(adoption);
        assert!(Attribution::try_from_records(&records).is_err());
    }

    #[test]
    fn shared_opposing_claims_resolve_individually_without_net_allocation_guessing() {
        let mut records = vec![base(&[(0, 0.2899999999999999), (1, 0.1), (2, -0.19)], 0.2)];
        records.push(plan(&records));
        let claims = Attribution::try_from_records(&records).unwrap();
        assert_eq!(
            claims.signed_exact(StrategyId(0), SymbolId(0)),
            exact("0.29")
        );
        assert_eq!(
            claims.signed_exact(StrategyId(1), SymbolId(0)),
            exact("0.1")
        );
        assert_eq!(
            claims.signed_exact(StrategyId(2), SymbolId(0)),
            exact("-0.19")
        );
        assert_eq!(
            reconcile::physical_exposure(&records).unwrap()[&SymbolId(0)],
            exact("0.2")
        );
        assert!(claims.legacy_quantities.is_empty());
    }

    #[test]
    fn a_closed_legacy_lot_cannot_round_a_later_canonical_opening() {
        let mut records = vec![base(&[(0, 0.2899999999999999)], 0.2899999999999999)];
        records.extend(fill("legacy-close", Side::Sell, "0.29", false));
        records.extend(fill(
            "canonical-open",
            Side::Buy,
            "0.100000000000000001",
            true,
        ));
        let before = Attribution::try_from_records(&records).unwrap().snapshot();
        let before_lots = Fills::try_from_records(&records).unwrap().open_trade_lots();
        let adoption = plan(&records);
        assert!(
            matches!(&adoption,WalRecord::LegacyQuantityGridAdopted{sleeves,..} if sleeves.is_empty())
        );
        records.push(adoption);
        assert_eq!(
            Attribution::try_from_records(&records).unwrap().snapshot(),
            before
        );
        assert_eq!(
            Fills::try_from_records(&records).unwrap().open_trade_lots(),
            before_lots
        );
        assert_eq!(
            before.positions[0].signed_qty,
            exact("0.100000000000000001")
        );
        assert!(before.positions[0].entry_value.is_some());
        assert_eq!(
            reconcile::physical_exposure(&records).unwrap()[&SymbolId(0)],
            exact("0.100000000000000001")
        );
    }

    #[test]
    fn grid_adoption_crash_cuts_and_rotation_preserve_quantities_cash_and_eligibility() {
        use engine_types::Wal;
        let mut records = vec![base(&[(0, 0.2899999999999999)], 0.2899999999999999)];
        records.extend(fill("partial-before-adoption", Side::Sell, "0.01", false));
        records.push(WalRecord::ExecutionPrecisionV1);
        let adoption = plan(&records);
        let directory = std::env::temp_dir().join(format!(
            "legacy-grid-{}-{}",
            std::process::id(),
            engine_types::clock::wall_ns()
        ));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("engine.wal");
        let mut legacy = serde_json::to_value(&records[0]).unwrap();
        legacy["kind"] = "segment_base".into();
        let payload = serde_json::to_vec(&legacy).unwrap();
        let mut frame = b"EWAL0001".to_vec();
        frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        frame.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        frame.extend_from_slice(&payload);
        std::fs::write(&path, frame).unwrap();
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for row in &records[1..] {
            wal.append(row).unwrap();
        }
        wal.barrier().unwrap();
        let before = std::fs::metadata(&path).unwrap().len() as usize;
        wal.append(&adoption).unwrap();
        wal.barrier().unwrap();
        drop(wal);
        let full = std::fs::read(&path).unwrap();
        for cut in [before, before + 1, before + 9, full.len() - 1, full.len()] {
            let copy = directory.join(format!("cut-{cut}.wal"));
            std::fs::write(&copy, &full[..cut]).unwrap();
            let (_, read) = engine_wal::WalWriter::open(&copy).unwrap();
            let read = read.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
            let claims = Attribution::try_from_records(&read).unwrap();
            assert_eq!(claims.legacy_quantities.is_empty(), cut == full.len());
            let expected = if cut == full.len() {
                exact("0.28")
            } else {
                Exact::from_legacy_f64(0.2899999999999999).unwrap()
                    - Exact::from_legacy_f64(0.01).unwrap()
            };
            assert_eq!(claims.signed_exact(StrategyId(0), SymbolId(0)), expected);
            if cut < full.len() {
                assert_eq!(plan(&read), adoption);
            }
        }
        records.push(adoption);
        let portfolio = Attribution::try_from_records(&records).unwrap().snapshot();
        let lots = Fills::try_from_records(&records).unwrap().open_trade_lots();
        let physical = reconcile::physical_exposure(&records).unwrap();
        let stops = reconcile::intended_stops(&records).unwrap();
        let mut snapshot = serde_json::to_value(base(&[(0, 0.28)], 0.28)).unwrap();
        snapshot["kind"] = "segment_base_v7".into();
        snapshot["portfolio"] = serde_json::to_value(&portfolio).unwrap();
        snapshot["open_trade_lots"] = serde_json::to_value(&lots).unwrap();
        snapshot["logged_exposure"] =
            serde_json::to_value(reconcile::snapshot_exposure(&physical)).unwrap();
        let snapshot: WalRecord = serde_json::from_value(snapshot).unwrap();
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        wal.rotate(&snapshot).unwrap();
        drop(wal);
        let (_, read) = engine_wal::open_current(&path).unwrap();
        let read = read.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        let claims = Attribution::try_from_records(&read).unwrap();
        assert_eq!(claims.snapshot(), portfolio);
        assert!(claims.legacy_quantities.is_empty());
        assert_eq!(
            Fills::try_from_records(&read).unwrap().open_trade_lots(),
            lots
        );
        assert_eq!(reconcile::physical_exposure(&read).unwrap(), physical);
        assert_eq!(reconcile::intended_stops(&read).unwrap(), stops);
        assert!(reconcile::legacy_physical_origins(&read)
            .unwrap()
            .is_empty());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn durable_exact_allocation_without_native_amounts_preserves_a_canonical_residual() {
        use engine_types::execution_allocation::{
            AllocationPolicy, ExecutionAllocation, ExecutionSlice,
        };
        let mut claims = Attribution::restore(&engine_types::portfolio::PortfolioState {
            positions: vec![engine_types::portfolio::PortfolioPosition {
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                signed_qty: exact("0.250000000000000001"),
                entry_value: None,
                stop_px: None,
                settlement_asset: engine_types::numeric::AssetId::Unknown,
            }],
            ..Default::default()
        })
        .unwrap();
        let request = OrderRequest {
            client_order_id: "canonical-allocation".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.25,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        };
        let update = OrderUpdate::Fill {
            allocation: Some(Box::new(ExecutionAllocation {
                policy: AllocationPolicy::DirectOrder,
                slices: vec![ExecutionSlice {
                    strategy: StrategyId(0),
                    strategy_key: "left".into(),
                    quantity: exact("0.25"),
                    fee: None,
                }],
            })),
            amounts: None,
            exec_id: "canonical-allocation-fill".into(),
            client_order_id: request.client_order_id.clone(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.25,
            px: 100.0,
            fee: None,
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 2,
            recv_ns: 2,
        };
        let mut snapshot = serde_json::to_value(base(&[(0, 0.25)], 0.25)).unwrap();
        snapshot["portfolio"] = serde_json::to_value(claims.snapshot()).unwrap();
        snapshot["intended_stops"] = serde_json::json!([]);
        let records = vec![
            serde_json::from_value(snapshot).unwrap(),
            WalRecord::OrderSent {
                dispatch: None,
                request: request.clone(),
                wire_ns: 1,
                arrival_mid: 100.0,
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: update.clone(),
            },
        ];
        let prepared = claims
            .prepare_portfolio_update_for_order(Some(&request), &["left".into()], &update)
            .unwrap()
            .unwrap();
        claims.commit_portfolio_fill(prepared).unwrap();
        assert_eq!(
            claims.signed_exact(StrategyId(0), SymbolId(0)),
            exact("0.000000000000000001")
        );
        assert!(claims.legacy_quantities.is_empty());
        assert_eq!(
            Attribution::try_from_records(&records).unwrap().snapshot(),
            claims.snapshot()
        );
    }
}

#[cfg(test)]
#[path = "legacy_quantity/context_tests.rs"]
mod context_tests;
