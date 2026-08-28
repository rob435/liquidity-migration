//! Rotation restates the log: replaying the old records and replaying the
//! restatement alone must recover the same engine.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;
use engine_types::PositionView;

const STOP_BTC: f64 = 90.0;
const STOP_ETH: f64 = 80.0;

struct StopMover { symbol: String, stops: VecDeque<f64> }
impl Strategy for StopMover {
    fn name(&self) -> &str { "stop-mover" }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription { symbol: self.symbol.clone(), feed: Feed::Quote }]
    }
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            if let Some(trigger_px) = self.stops.pop_front() {
                ctx.emit(engine_types::Action::SetStop { symbol: *symbol, trigger_px });
            }
        }
    }
}

#[tokio::test]
async fn a_live_stop_move_is_validated_and_survives_rotation() {
    let mover = StopMover { symbol: "BTCUSDT".into(), stops: VecDeque::from(vec![70.0, f64::NAN, 90.0]) };
    let held = vec![PositionView { symbol: SymbolId(0), side: Side::Buy, qty: 1.0,
        entry_px: 100.0, stop_attached: true, stop_px: 80.0, leverage: None }];
    let (mut engine, h) = build_with_venue_state(allow_all(), vec![Box::new(mover)],
        &["BTCUSDT"], &[], Vec::new(), held).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine.run(&mut ScriptFeed::quotes(symbol, 3, true), &mut ScriptOrderFeed::empty(),
        std::future::pending::<()>()).await.unwrap();
    assert_eq!(*h.stops.borrow(), vec![(symbol, 90.0)]);
    let WalRecord::SegmentBase { intended_stops, .. } = engine.rotation_base(7) else { panic!() };
    assert_eq!(intended_stops[0].trigger_px, 90.0);
}

fn sent(id: &str, symbol: u16, qty: f64, stop: f64) -> WalRecord {
    WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: id.to_string(),
            strategy: StrategyId(0),
            symbol: SymbolId(symbol),
            side: Side::Buy,
            qty,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: stop }),
            reduce_only: false,
        },
        wire_ns: 5,
        arrival_mid: 100.25,
    }
}

fn fill(id: &str, symbol: u16, qty: f64) -> WalRecord {
    WalRecord::OrderUpdate {
        update: OrderUpdate::Fill {
            exec_id: String::new(),
            client_order_id: id.to_string(),
            symbol: SymbolId(symbol),
            side: Side::Buy,
            qty,
            px: 100.0,
            fee: 0.01,
            is_maker: true,
            venue_ts_ms: recent_replay_ms(),
            recv_ns: 6,
        },
    }
}

/// A previous run's log with everything a restatement has to carry: an id
/// table, a control anchor, a closed order whose fills ARE the position, an
/// order still in flight and part-filled, and a stranger's durable fill that
/// belongs to neither trusted exposure nor a strategy.
fn previous_log() -> Vec<WalRecord> {
    vec![
        WalRecord::Names {
            strategies: vec!["buyer".to_string()],
            symbols: vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
        },
        WalRecord::ControlAnchor { source: "risk".to_string(), state: "anchor-1".to_string() },
        sent("eng-a", 0, 2.0, STOP_BTC),
        fill("eng-a", 0, 2.0),
        sent("eng-b", 1, 1.0, STOP_ETH),
        fill("eng-b", 1, 0.4),
        // Somebody else's fill: observed by the log, owned by nobody.
        fill("stranger-1", 0, 5.0),
    ]
}

/// What the venue holds after [`previous_log`]'s fills: the closed order's 2
/// BTC (plus the stranger's 5) and eng-b's partial 0.4 ETH. Boot reads this;
/// a venue reported flat would rightly clear the sleeves' claims instead of
/// carrying them.
fn venue_holdings() -> Vec<PositionView> {
    vec![
        PositionView {
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 7.0,
            entry_px: 100.0,
            stop_attached: true, stop_px: 0.0,
            leverage: None,
        },
        PositionView {
            symbol: SymbolId(1),
            side: Side::Buy,
            qty: 0.4,
            entry_px: 100.0,
            stop_attached: true, stop_px: 0.0,
            leverage: None,
        },
    ]
}

#[tokio::test]
async fn replaying_the_restatement_recovers_the_same_engine_as_the_old_log() {
    let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
    let working = vec![still_working("eng-b", "ETHUSDT", 1.0)];
    let (engine_a, _) = build_with_venue_state(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT", "ETHUSDT"],
        &previous_log(),
        working.clone(),
        venue_holdings(),
    )
    .await;
    let base = engine_a.rotation_base(recent_replay_ms());

    // The restatement says what the log said, in full: names, latch, anchor,
    // whose fills built what, the trusted per-symbol fill total, the intended
    // stops, and the one open order with
    // its partial fill.
    let WalRecord::SegmentBase {
        strategies,
        symbols,
        may_open,
        control_anchors,
        attribution,
        logged_exposure,
        intended_stops,
        open_orders,
        ..
    } = &base
    else {
        panic!("rotation_base must build a SegmentBase record");
    };
    assert_eq!(strategies, &["buyer".to_string()]);
    assert_eq!(symbols, &["BTCUSDT".to_string(), "ETHUSDT".to_string()]);
    assert!(!*may_open, "the foreign fill latches opening off");
    assert_eq!(control_anchors.len(), 1);
    assert_eq!(control_anchors[0].state, "anchor-1");
    let attributed: Vec<(u16, u16, f64)> = attribution
        .iter()
        .map(|row| (row.strategy.0, row.symbol.0, row.signed_qty))
        .collect();
    assert_eq!(attributed, vec![(0, 0, 2.0), (0, 1, 0.4)], "strangers are owned by nobody");
    let exposure: Vec<(u16, f64)> = logged_exposure
        .iter()
        .map(|row| (row.symbol.0, row.signed_qty))
        .collect();
    assert_eq!(exposure, vec![(0, 2.0), (1, 0.4)], "the stranger's 5 BTC stay untrusted");
    let stops: Vec<(u16, f64)> = intended_stops
        .iter()
        .map(|row| (row.symbol.0, row.trigger_px))
        .collect();
    assert_eq!(stops, vec![(0, STOP_BTC), (1, STOP_ETH)]);
    assert_eq!(open_orders.len(), 1, "only eng-b is still out there");
    assert_eq!(open_orders[0].request.client_order_id, "eng-b");
    assert_eq!(open_orders[0].filled_qty, 0.4, "the partial fill survives the restatement");

    // The equivalence itself: an engine booted from the restatement alone is
    // the engine booted from the whole old log.
    let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
    let (engine_b, _) = build_with_venue_state(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT", "ETHUSDT"],
        std::slice::from_ref(&base),
        working,
        venue_holdings(),
    )
    .await;
    assert_eq!(engine_b.in_flight_ids(), engine_a.in_flight_ids());
    assert_eq!(
        engine_b.rotation_base(recent_replay_ms()),
        base,
        "orders, latches, names, attribution, exposure and stops all round-trip"
    );
}

/// The reason the restatement carries the per-symbol fill totals: a restart
/// after a rotation must still be able to account for the position it is
/// holding, or boot latches the engine against opening on its own position.
#[tokio::test]
async fn a_restart_on_a_rotated_log_still_accounts_for_its_position() {
    let held = vec![PositionView {
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 2.0,
        entry_px: 100.0,
        stop_attached: true, stop_px: 0.0,
        leverage: None
    }];
    let log = vec![
        WalRecord::Names {
            strategies: vec!["buyer".to_string()],
            symbols: vec!["BTCUSDT".to_string()],
        },
        sent("eng-a", 0, 2.0, STOP_BTC),
        fill("eng-a", 0, 2.0),
    ];

    let reconciled_may_open = |records: &Rc<RefCell<Vec<WalRecord>>>| {
        records
            .borrow()
            .iter()
            .find_map(|record| match record {
                WalRecord::Reconciled { may_open, .. } => Some(*may_open),
                _ => None,
            })
            .expect("boot writes a Reconciled record")
    };

    // Booted on the full log, holding the position: accounted for.
    let base = {
        let tape = tape();
        let (wal, records) = MockWal::new(tape.clone());
        let (venue, _) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
        venue.account_readings.borrow_mut().push_back(held.clone());
        let (risk, _) = MockRisk::with(allow_all());
        let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
        let engine = Engine::boot(
            &settings(),
            "0000000000000000",
            wal,
            risk,
            venue,
            vec![Box::new(buyer)],
            &log,
        )
        .await
        .expect("boot");
        assert!(reconciled_may_open(&records), "the full log accounts for the position");
        engine.rotation_base(recent_replay_ms())
    };

    // Booted on the restatement alone, holding the same position: still
    // accounted for. This is what the logged-exposure rows buy.
    {
        let tape = tape();
        let (wal, records) = MockWal::new(tape.clone());
        let (venue, _) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
        venue.account_readings.borrow_mut().push_back(held.clone());
        let (risk, _) = MockRisk::with(allow_all());
        let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
        let _engine = Engine::boot(
            &settings(),
            "0000000000000000",
            wal,
            risk,
            venue,
            vec![Box::new(buyer)],
            &[base],
        )
        .await
        .expect("boot");
        assert!(
            reconciled_may_open(&records),
            "a rotated log must not read its own position as somebody else's"
        );
    }

    // The control that proves the mechanism is doing something: the same
    // position over an EMPTY log is unaccounted, and boot latches.
    {
        let tape = tape();
        let (wal, records) = MockWal::new(tape.clone());
        let (venue, _) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
        venue.account_readings.borrow_mut().push_back(held);
        let (risk, _) = MockRisk::with(allow_all());
        let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
        let _engine = Engine::boot(
            &settings(),
            "0000000000000000",
            wal,
            risk,
            venue,
            vec![Box::new(buyer)],
            &[],
        )
        .await
        .expect("boot");
        assert!(!reconciled_may_open(&records), "an empty log cannot account for it");
    }
}

/// The boot entry the runner actually uses, over real files: a rotation cut
/// off mid-restatement leaves `assembly::wal` replaying the old segment.
#[tokio::test]
async fn a_torn_rotation_on_disk_boots_from_the_old_segment() {
    let dir = std::env::temp_dir().join(format!(
        "engine-core-rotation-{}-{}",
        std::process::id(),
        clock::now_ns()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let family = dir.join("engine.wal");

    {
        let (mut wal, _) = engine_wal::WalWriter::open(&family).unwrap();
        for record in previous_log() {
            wal.append(&record).unwrap();
        }
        Wal::barrier(&mut wal).unwrap();

        // A real restatement, then the crash: the new segment loses its tail.
        let (buyer, _) = Buyer::new("BTCUSDT", 1, 0.01);
        let (engine, _) = build_with_venue_orders(allow_all(),
            vec![Box::new(buyer)],
            &["BTCUSDT", "ETHUSDT"],
            &previous_log(),
            vec![still_working("eng-b", "ETHUSDT", 1.0)],
        )
        .await;
        Wal::rotate(&mut wal, &engine.rotation_base(7)).unwrap();
    }

    let second = dir.join("engine.wal.000002");
    let whole = std::fs::read(&second).unwrap();
    for cut in [0u64, 9, (whole.len() as u64) / 2, whole.len() as u64 - 1] {
        std::fs::write(&second, &whole[..cut as usize]).unwrap();
        let (_, replayed) = crate::assembly::wal(&family).expect("boot must not error");
        assert_eq!(
            replayed,
            previous_log(),
            "cut at byte {cut}: boot falls back to the old segment, nothing lost"
        );
    }

    // Restatement whole again: boot picks the new segment, and it says the
    // same thing the old log did.
    std::fs::write(&second, &whole).unwrap();
    let (_, replayed) = crate::assembly::wal(&family).expect("boot");
    assert_eq!(replayed.len(), 1);
    assert!(matches!(replayed[0], WalRecord::SegmentBase { .. }));

    std::fs::remove_dir_all(&dir).unwrap();
}
