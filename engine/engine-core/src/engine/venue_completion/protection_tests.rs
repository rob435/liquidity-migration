use super::*;
use crate::tests::{callback_test_fixture, shared_sleeves, MockRisk, MockVenue, MockWal};
use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};
use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
use engine_types::orders::SleeveOrderEffect;
use engine_types::portfolio::PortfolioPosition;
use std::sync::{Arc, Mutex};

type TestEngine = Engine<MockWal, MockRisk, MockVenue>;

// Exact economic values from the two captured TAO executions; local identities are synthetic.
const CELLS: [(&str, &str, &str, &str); 2] = [
    ("3.191", "257.57", "0.45204823", "202.13"),
    ("0.248", "257.77", "0.06392696", "202.32"),
];

fn d(value: &str) -> Exact {
    Exact::parse_decimal(value).unwrap()
}

async fn filled(
    (quantity, price, fee, stop): (&str, &str, &str, &str),
) -> (TestEngine, Arc<Mutex<Vec<WalRecord>>>) {
    let (mut engine, records) = callback_test_fixture(vec![
        shared_sleeves::idle("left"),
        shared_sleeves::idle("right"),
    ])
    .await;
    let stop_px = d(stop).to_f64().unwrap();
    let mut request = OrderRequest {
        client_order_id: "allocated-stop-source".into(),
        strategy: StrategyId(1),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: d(quantity).to_f64().unwrap(),
        kind: OrderKind::Market,
        stop: Some(StopSpec {
            trigger_px: stop_px,
        }),
        reduce_only: false,
        close_position: false,
        sleeve_effect: Some(SleeveOrderEffect::Increase {
            stop: StopSpec {
                trigger_px: stop_px,
            },
        }),
        exact_terms: None,
    };
    ExactOrderTerms {
        quantity: d(quantity),
        limit_price: None,
        stop_trigger_price: Some(d(stop)),
        physical_stop_trigger_price: Some(d(stop)),
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    }
    .apply_projection(&mut request)
    .unwrap();
    let sent = WalRecord::OrderSent {
        dispatch: None,
        request,
        arrival_mid: d(price).to_f64().unwrap(),
        wire_ns: clock::now_ns(),
    };
    engine.books.orders.apply(&sent);
    engine
        .books
        .registry
        .own("allocated-stop-source", StrategyId(1));
    engine.wal.append(&sent).unwrap();
    let update = OrderUpdate::Fill {
        allocation: None,
        amounts: Some(Box::new(ExecutionAmounts {
            quantity: ExactNumber::venue_decimal(quantity).unwrap(),
            price: ExactNumber::venue_decimal(price).unwrap(),
            fee: Some(AssetAmount {
                asset: AssetId::Named("USDT".into()),
                amount: ExactNumber::venue_decimal(fee).unwrap(),
            }),
            settlement_asset: AssetId::Named("USDT".into()),
        })),
        exec_id: "allocated-stop-execution".into(),
        client_order_id: "allocated-stop-source".into(),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: d(quantity).to_f64().unwrap(),
        px: d(price).to_f64().unwrap(),
        fee: Some(d(fee).to_f64().unwrap()),
        is_maker: false,
        forced_close: None,
        venue_ts_ms: clock::wall_ms(),
        recv_ns: clock::now_ns(),
    };
    let journaled = TestEngine::journal_update(
        None,
        update,
        &mut engine.wal,
        &engine.books.orders,
        &engine.books.attribution,
        CallbackOwners {
            names: &engine.host.names,
            destinations: Some(vec![StrategyId(1)]),
        },
        &mut engine.recovered_exec_ids,
        &mut engine.may_open,
    )
    .unwrap()
    .unwrap();
    engine.apply_journaled_update(journaled).unwrap();
    (engine, records)
}

#[tokio::test(start_paused = true)]
async fn allocated_fill_keeps_its_exact_owned_stop_before_callbacks_and_replay() {
    for cell in CELLS {
        let (engine, records) = filled(cell).await;
        let live = engine.books.attribution.snapshot();
        assert_eq!(live.positions[0].strategy, StrategyId(1));
        assert_eq!(live.positions[0].signed_qty, d(cell.0));
        assert_eq!(
            live.positions[0].stop_px,
            Some(d(cell.3)),
            "allocated live fill lost its order-owned stop before the strategy callback"
        );
        let rows = records.lock().unwrap().clone();
        assert!(rows.iter().any(|row| matches!(
            row,
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill {
                    allocation: Some(_),
                    ..
                },
                ..
            }
        )));
        let replayed = Attribution::try_from_records(&rows).unwrap().snapshot();
        assert_eq!(
            live, replayed,
            "live stop and accounting differ from WAL replay"
        );
        let rotated = engine.rotation_base(clock::wall_ms());
        assert_eq!(
            Attribution::try_from_records(&[rotated])
                .unwrap()
                .snapshot(),
            live
        );
    }
}

async fn missing_stop_base(cell: (&str, &str, &str, &str)) -> WalRecord {
    let (mut engine, _) = filled(cell).await;
    let mut state = engine.books.attribution.snapshot();
    state.positions[0].stop_px = None;
    engine.books.attribution = Attribution::restore(&state).unwrap();
    engine.may_open = false;
    engine.rotation_base(clock::wall_ms())
}

fn held(cell: (&str, &str, &str, &str)) -> Vec<engine_types::PositionView> {
    vec![engine_types::PositionView {
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: d(cell.0).to_f64().unwrap(),
        entry_px: d(cell.1).to_f64().unwrap(),
        exact_amounts: Some(Box::new(engine_types::risk::PositionAmounts {
            quantity: ExactNumber::venue_decimal(cell.0).unwrap(),
            entry_price: ExactNumber::venue_decimal(cell.1).unwrap(),
        })),
        stop_attached: true,
        stop_px: d(cell.3).to_f64().unwrap(),
        exact_stop_px: Some(Box::new(d(cell.3))),
        leverage: None,
    }]
}

#[tokio::test(start_paused = true)]
async fn rotated_single_owner_restores_durable_stop_before_boot_callbacks_without_clearing_latch() {
    for cell in CELLS {
        let base = missing_stop_base(cell).await;
        let decoded: WalRecord =
            serde_json::from_slice(&serde_json::to_vec(&base).unwrap()).unwrap();
        let restored = Attribution::try_from_records(std::slice::from_ref(&decoded)).unwrap();
        let expected = Exact::from_legacy_f64(d(cell.3).to_f64().unwrap()).unwrap();
        assert_eq!(
            restored.snapshot().positions[0].stop_px,
            Some(expected),
            "canonical sole-owner rotation ignored its same-base durable position stop"
        );
        let path = crate::testpath::temp_path("restored-sole-owner-stop");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        wal.append(&decoded).unwrap();
        wal.barrier().unwrap();
        let mut engine =
            shared_sleeves::restart_portfolio_with_wal(wal, &[decoded], held(cell)).await;
        assert!(
            !engine.may_open,
            "stop repair must not clear the durable reconciliation latch"
        );
        assert!(
            engine.stop_repairs_pending.is_empty(),
            "native full stop was not recognized after restatement"
        );
        assert_eq!(engine.books.attribution.snapshot(), restored.snapshot());
        assert!(engine.pending_mutations.is_empty());
        assert_eq!(engine.orders_sent, 0);
        engine.wal.flush().unwrap();
        assert!(
            engine_wal::replay(&path)
                .unwrap()
                .iter()
                .all(|(_, row)| !matches!(
                    row,
                    WalRecord::StopSet { .. } | WalRecord::OrderSent { .. }
                )),
            "already protected native position triggered a new stop or order"
        );
        let next = engine.rotation_base(clock::wall_ms());
        engine.wal.rotate(&next).unwrap();
        drop(engine);
        let (wal, rows) = engine_wal::open_current(&path).unwrap();
        let rows: Vec<_> = rows.into_iter().map(|(_, row)| row).collect();
        let again = shared_sleeves::restart_portfolio_with_wal(wal, &rows, held(cell)).await;
        assert!(!again.may_open);
        assert!(again.stop_repairs_pending.is_empty());
        assert!(again.pending_mutations.is_empty());
        assert_eq!(again.orders_sent, 0);
        assert_eq!(again.books.attribution.snapshot(), restored.snapshot());
    }
}

#[tokio::test(start_paused = true)]
async fn canonical_stop_recovery_never_borrows_shared_or_opposing_protection() {
    for other_qty in ["1", "-1"] {
        let mut base = missing_stop_base(CELLS[0]).await;
        let WalRecord::SegmentBase {
            portfolio: Some(state),
            attribution,
            ..
        } = &mut base
        else {
            unreachable!()
        };
        state.positions.push(PortfolioPosition {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            signed_qty: d(other_qty),
            entry_value: Some(d("100")),
            stop_px: None,
            settlement_asset: AssetId::Named("USDT".into()),
        });
        attribution.push(engine_types::FilledTotal {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            signed_qty: d(other_qty).to_f64().unwrap(),
        });
        let restored = Attribution::try_from_records(&[base]).unwrap().snapshot();
        assert!(restored.positions.iter().all(|row| row.stop_px.is_none()));
    }
}

#[tokio::test(start_paused = true)]
async fn canonical_stop_recovery_requires_explicit_matching_side_and_keeps_known_stop() {
    for side in [None, Some(Side::Sell)] {
        let mut base = missing_stop_base(CELLS[0]).await;
        let WalRecord::SegmentBase { intended_stops, .. } = &mut base else {
            unreachable!()
        };
        intended_stops[0].side = side;
        assert!(Attribution::try_from_records(&[base])
            .unwrap()
            .snapshot()
            .positions[0]
            .stop_px
            .is_none());
    }
    let mut base = missing_stop_base(CELLS[0]).await;
    let WalRecord::SegmentBase {
        portfolio: Some(state),
        ..
    } = &mut base
    else {
        unreachable!()
    };
    state.positions[0].stop_px = Some(d("201.01"));
    assert_eq!(
        Attribution::try_from_records(&[base])
            .unwrap()
            .snapshot()
            .positions[0]
            .stop_px,
        Some(d("201.01"))
    );
}
