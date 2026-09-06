use super::*;
use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};

const ADOPTION: &str = "legacy_quantity_grid_adopted_v2";

#[derive(Clone, Copy, Debug)]
enum WriteFailure {
    Append,
    Barrier,
}

struct BootAttempt {
    result: Result<Engine<MockWal, MockRisk, MockVenue>, EngineError>,
    records: Arc<Mutex<Vec<WalRecord>>>,
    rolling: MockRolling,
    venue_writes: [usize; 4],
}

fn exact(value: &str) -> Exact {
    Exact::parse_decimal(value).unwrap()
}

fn execution(
    id: &str,
    side: Side,
    quantity: &str,
    price: &str,
    fee: &str,
    time: i64,
    native: bool,
) -> Vec<WalRecord> {
    let qty = quantity.parse().unwrap();
    let px = price.parse().unwrap();
    vec![
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: id.into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side,
                qty,
                kind: OrderKind::Market,
                stop: (side == Side::Buy).then_some(StopSpec { trigger_px: 80.0 }),
                reduce_only: side == Side::Sell,
                close_position: false,
                sleeve_effect: None,
                exact_terms: None,
            },
            wire_ns: 1,
            arrival_mid: px,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: native.then(|| {
                    Box::new(ExecutionAmounts {
                        quantity: ExactNumber::venue_decimal(quantity).unwrap(),
                        price: ExactNumber::venue_decimal(price).unwrap(),
                        fee: Some(AssetAmount {
                            asset: AssetId::Named("USDT".into()),
                            amount: ExactNumber::venue_decimal(fee).unwrap(),
                        }),
                        settlement_asset: AssetId::Named("USDT".into()),
                    })
                }),
                exec_id: id.into(),
                client_order_id: id.into(),
                symbol: SymbolId(0),
                side,
                qty,
                px,
                fee: Some(fee.parse().unwrap()),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: time,
                recv_ns: 1,
            },
        },
    ]
}

fn prefix(reopened: bool) -> (Vec<WalRecord>, ClosedTradeRow) {
    let time = recent_replay_ms();
    let mut records = vec![serde_json::from_value(serde_json::json!({
        "kind":"segment_base", "wall_ts_ms":time-100,
        "strategies":["left"], "symbols":["BTCUSDT"],
        "may_open":true, "control_anchors":[], "open_orders":[],
        "attribution":[], "logged_exposure":[], "intended_stops":[],
        "execution_history_through_ms":time
    }))
    .unwrap()];
    records.extend(execution(
        "eng-legacy-entry-a",
        Side::Buy,
        "0.1",
        "100",
        "0.01",
        time - 30,
        false,
    ));
    records.extend(execution(
        "eng-legacy-entry-b",
        Side::Buy,
        "0.2",
        "100",
        "0.02",
        time - 20,
        false,
    ));
    records.extend(execution(
        "eng-native-close",
        Side::Sell,
        "0.3",
        "90",
        "0.003",
        time - 10,
        true,
    ));
    if reopened {
        records.extend(execution(
            "eng-native-reopen",
            Side::Buy,
            "0.01",
            "110",
            "0.004",
            time - 5,
            true,
        ));
    }
    let legacy_cost = (Exact::from_legacy_f64(0.1).unwrap() + Exact::from_legacy_f64(0.2).unwrap())
        * exact("100");
    let fees = Exact::from_legacy_f64(0.01).unwrap()
        + Exact::from_legacy_f64(0.02).unwrap()
        + exact("0.003");
    let net = exact("0.3") * exact("90") - legacy_cost - fees;
    let expected = ClosedTradeRow {
        closed_ms: time - 10,
        net_usdt: net.to_f64().unwrap(),
        net_usdt_exact: Some(net),
        unpriced: None,
    };
    (records, expected)
}

async fn boot(prior: &[WalRecord], reopened: bool, failure: Option<WriteFailure>) -> BootAttempt {
    let tape = tape();
    let (mut wal, records) = MockWal::new(tape.clone());
    *records.lock().unwrap() = prior.to_vec();
    wal.seq = prior.len() as u64;
    match failure {
        Some(WriteFailure::Append) => wal.fail_append(ADOPTION),
        Some(WriteFailure::Barrier) => wal.fail_barrier_after = Some(ADOPTION),
        None => (),
    }
    let (mut venue, sends) = MockVenue::new(tape, &["BTCUSDT"]);
    let mut specification = shared_sleeves::spec();
    specification.qty_step = Some(exact("0.01"));
    specification.market_qty_step = specification.qty_step.clone();
    specification.min_qty = specification.qty_step.clone();
    specification.market_min_qty = specification.qty_step.clone();
    venue.exact_specs = Some(vec![("BTCUSDT".into(), specification)]);
    let positions = if reopened {
        vec![engine_types::PositionView {
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            entry_px: 110.0,
            exact_amounts: Some(Box::new(engine_types::risk::PositionAmounts {
                quantity: ExactNumber::venue_decimal("0.01").unwrap(),
                entry_price: ExactNumber::venue_decimal("110").unwrap(),
            })),
            stop_px: 80.0,
            exact_stop_px: Some(Box::new(exact("80"))),
            stop_attached: true,
            leverage: Some(2.0),
        }]
    } else {
        Vec::new()
    };
    venue.account_readings.lock().unwrap().push_back(positions);
    let stops = venue.stops.clone();
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let (risk, _) = MockRisk::with(allow_all());
    let rolling = risk.rolling.clone();
    let result = Engine::boot(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![shared_sleeves::idle("left")],
        prior,
    )
    .await;
    let venue_writes = [
        sends.lock().unwrap().len(),
        stops.lock().unwrap().len(),
        cancels.lock().unwrap().len(),
        amends.lock().unwrap().len(),
    ];
    BootAttempt {
        result,
        records,
        rolling,
        venue_writes,
    }
}

fn assert_snapshot(snapshot: &WalRecord, expected: &ClosedTradeRow, reopened: bool) {
    let WalRecord::SegmentBase {
        rolling_loss_rows,
        portfolio: Some(portfolio),
        open_trade_lots: Some(lots),
        ..
    } = snapshot
    else {
        panic!("complete boot state")
    };
    assert_eq!(
        rolling_loss_rows,
        std::slice::from_ref(expected),
        "the real close seeds the rolling-loss window exactly once"
    );
    assert_eq!(portfolio.positions.len(), usize::from(reopened));
    assert_eq!(lots.len(), usize::from(reopened));
    if reopened {
        let position = &portfolio.positions[0];
        assert_eq!(position.signed_qty, exact("0.01"));
        assert_eq!(position.entry_value, Some(exact("1.1")));
        assert_eq!(position.settlement_asset, AssetId::Named("USDT".into()));
        assert_eq!(lots[0].signed_qty, exact("0.01"));
        assert_eq!(lots[0].opened_ms, expected.closed_ms + 5);
        assert_eq!(lots[0].cash, -exact("1.1"));
        assert_eq!(lots[0].fees, Some(exact("0.004")));
        assert_eq!(lots[0].fills, 1);
    }
}

#[tokio::test]
async fn contextual_legacy_close_seeds_loss_once_through_boot_replay_and_rotation() {
    for reopened in [false, true] {
        let (original, expected) = prefix(reopened);
        let first = boot(&original, reopened, None).await;
        assert_eq!(first.venue_writes, [0; 4]);
        let engine = first.result.unwrap();
        assert_eq!(first.rolling.closes(), vec![expected.clone()]);
        let snapshot = engine.rotation_base(clock::wall_ms());
        assert_snapshot(&snapshot, &expected, reopened);
        let records = first.records.lock().unwrap().clone();
        assert_eq!(&records[..original.len()], original);
        assert_eq!(
            records
                .iter()
                .filter(|r| matches!(r, WalRecord::LegacyQuantityGridAdopted { .. }))
                .count(),
            1
        );
        let rebuilt = crate::execution::Fills::try_from_records(&records).unwrap();
        assert_eq!(rebuilt.closed().len(), 1);
        assert_eq!(rebuilt.closed()[0].loss_row(), Some(expected.clone()));
        assert_eq!(rebuilt.closed()[0].fills, 3);
        drop(engine);
        let second = boot(&records, reopened, None).await;
        assert_eq!(second.venue_writes, [0; 4]);
        assert_eq!(second.rolling.closes(), vec![expected.clone()]);
        let engine = second.result.unwrap();
        let snapshot = engine.rotation_base(clock::wall_ms());
        assert_snapshot(&snapshot, &expected, reopened);
        assert_eq!(
            second
                .records
                .lock()
                .unwrap()
                .iter()
                .filter(|r| matches!(r, WalRecord::LegacyQuantityGridAdopted { .. }))
                .count(),
            1
        );
        drop(engine);

        let path = temp_path("legacy-accounting-boot-rotation");
        let (mut writer, _) = engine_wal::WalWriter::open(&path).unwrap();
        writer.append(&snapshot).unwrap();
        writer.barrier().unwrap();
        writer.rotate(&snapshot).unwrap();
        drop(writer);
        let (_, rotated) = crate::assembly::wal(&path).unwrap();
        let third = boot(&rotated, reopened, None).await;
        assert_eq!(third.venue_writes, [0; 4]);
        assert!(
            third.rolling.closes().is_empty(),
            "rotation restores the existing loss row instead of announcing the close again"
        );
        let engine = third.result.unwrap();
        assert_snapshot(&engine.rotation_base(clock::wall_ms()), &expected, reopened);
        assert!(third
            .records
            .lock()
            .unwrap()
            .iter()
            .all(|r| !matches!(r, WalRecord::LegacyQuantityGridAdopted { .. })));
    }
}

#[tokio::test]
async fn contextual_legacy_close_does_not_publish_before_adoption_is_durable() {
    for failure in [WriteFailure::Append, WriteFailure::Barrier] {
        let (original, expected) = prefix(true);
        let failed = boot(&original, true, Some(failure)).await;
        assert!(failed.result.is_err(), "{failure:?} must stop boot");
        let error = failed.result.err().unwrap().to_string();
        assert!(
            error.contains("test failure") || error.contains("test barrier failure"),
            "unexpected boot failure: {error}"
        );
        assert_eq!(failed.venue_writes, [0; 4]);
        assert!(
            failed.rolling.closes().is_empty(),
            "{failure:?} published a corrected close before durability"
        );
        assert!(failed.rolling.rows.lock().unwrap().is_empty());
        let records = failed.records.lock().unwrap().clone();
        assert_eq!(&records[..original.len()], original);
        assert!(records[original.len()..].iter().all(|record| !matches!(
            record,
            WalRecord::Reconciled { .. }
                | WalRecord::SegmentBase { .. }
                | WalRecord::StrategyCallbackQueued { .. }
        )));

        let restarted = boot(&original, true, None).await;
        assert_eq!(restarted.rolling.closes(), vec![expected.clone()]);
        assert_snapshot(
            &restarted.result.unwrap().rotation_base(clock::wall_ms()),
            &expected,
            true,
        );
        if matches!(failure, WriteFailure::Barrier) {
            let retained = boot(&records, true, None).await;
            assert_eq!(retained.rolling.closes(), vec![expected.clone()]);
            assert_snapshot(
                &retained.result.unwrap().rotation_base(clock::wall_ms()),
                &expected,
                true,
            );
            assert_eq!(
                retained
                    .records
                    .lock()
                    .unwrap()
                    .iter()
                    .filter(|record| matches!(record, WalRecord::LegacyQuantityGridAdopted { .. }))
                    .count(),
                1
            );
        }
    }
}
