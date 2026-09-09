//! What the fills cost.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;
use crate::execution::HORIZONS_MS;
use engine_types::PositionView;

#[tokio::test(start_paused = true)]
async fn the_order_record_carries_the_midpoint_it_will_be_judged_against() {
    // Without this the log can say what we sent and what we got, and never
    // what the difference was worth: by the time a rested entry fills, the
    // price it was decided against is gone.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut feed = ScriptFeed::quotes(symbol, 1, true);
    engine
        .run(
            &mut feed,
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let records = h.records.lock().unwrap();
    let anchor = records
        .iter()
        .find_map(|r| match r {
            WalRecord::OrderSent { arrival_mid, .. } => Some(*arrival_mid),
            _ => None,
        })
        .expect("an order was sent");
    // The book the strategy saw: 30,000.0 bid against 30,000.5 ask.
    assert_eq!(anchor, 30_000.25, "the midpoint when the order left");
}

#[tokio::test(start_paused = true)]
async fn a_fill_is_priced_against_its_own_orders_midpoint_across_a_restart() {
    // The anchor lives in the log, not in memory, so an order sent before a
    // restart is still priced correctly when its fill turns up after one.
    let before = OrderRequest {
        client_order_id: "eng-1700000000000-4".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    };
    let replayed = vec![
        WalRecord::Boot {
            version: "old".into(),
            config_sha256: "abc".into(),
            wall_ts_ms: recent_replay_ms(),
            commit: String::new(),
        },
        WalRecord::OrderSent {
            dispatch: None,
            request: before.clone(),
            wire_ns: 1,
            arrival_mid: 30_000.0,
        },
    ];

    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, _h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &replayed).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: String::new(),
            client_order_id: before.client_order_id.clone(),
            symbol,
            side: Side::Buy,
            // Three basis points above the midpoint that order was sent at.
            px: 30_009.0,
            qty: 0.01,
            fee: Some(0.0),
            is_maker: true,
            forced_close: None,
            venue_ts_ms: 4,
            recv_ns: 4,
        }]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let costs = engine.fills().total();
    assert_eq!(costs.fills, 1);
    assert_eq!(costs.maker_fills, 1, "the venue said we rested");
    let shortfall = costs.arrival_shortfall.mean().expect("priced");
    assert!(
        (shortfall - 3.0).abs() < 0.01,
        "3 bp against the old anchor, got {shortfall}"
    );
}

#[tokio::test(start_paused = true)]
async fn a_fill_the_log_cannot_anchor_is_counted_but_not_priced() {
    // A fill for an order this log never sent — somebody hand-trading the
    // same account. It is not ours to score, and guessing a price for it
    // would put another person's execution in our own numbers.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, _h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: String::new(),
            client_order_id: "placed-by-hand".into(),
            symbol,
            side: Side::Buy,
            px: 30_009.0,
            qty: 0.01,
            fee: Some(0.0),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 4,
            recv_ns: 4,
        }]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    assert_eq!(engine.fills().total().fills, 0, "not our trade to score");
}

#[tokio::test(start_paused = true)]
async fn two_sleeves_running_one_plug_are_told_apart_by_their_config_names() {
    // Two sleeves can share one implementation. Their configured names keep
    // the log and heartbeat records distinct.
    let (one, _a) = Buyer::new("BTCUSDT", 100, 0.01);
    let (two, _b) = Buyer::new("ETHUSDT", 100, 0.01);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT", "ETHUSDT"]);
    let (risk, _saw) = MockRisk::with(allow_all());
    let engine = Engine::boot_as(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(one), Box::new(two)],
        &["carry".to_string(), "long".to_string()],
        &[],
    )
    .await
    .expect("boot");

    assert_eq!(
        engine.strategy_names(),
        &["carry".to_string(), "long".to_string()]
    );
    let said = records
        .lock()
        .unwrap()
        .iter()
        .find_map(crate::replay::LogNames::strategy_table)
        .expect("the log says what its ids mean");
    assert_eq!(said, vec!["carry".to_string(), "long".to_string()]);
}

#[tokio::test(start_paused = true)]
async fn a_sleeve_with_no_name_of_its_own_keeps_the_plugs() {
    // The fallback, and the reason `boot` can stay a one-liner over `boot_as`.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (_engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let said = h
        .records
        .lock()
        .unwrap()
        .iter()
        .find_map(crate::replay::LogNames::strategy_table)
        .expect("the log says what its ids mean");
    assert_eq!(said, vec!["buyer".to_string()], "the plug's own name");
}

// ------------------------------------------ horizons owed across a restart

/// The order the fixtures below are all one fill of.
const OWED_ID: &str = "eng-1700000000000-4";

/// A fill this old is past the one-second, fifteen-second and one-minute
/// horizons and their lateness bounds, and inside the five-minute one. Four
/// seconds of slack, so a slow boot cannot push the last one over.
fn stale_fill_ms() -> i64 {
    clock::wall_ms() - 301_000
}

fn owed_order() -> OrderRequest {
    OrderRequest {
        client_order_id: OWED_ID.into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    }
}

fn owed_fill(venue_ts_ms: i64) -> OrderUpdate {
    OrderUpdate::Fill {
        allocation: None,
        amounts: None,
        exec_id: String::new(),
        client_order_id: OWED_ID.into(),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        px: 30_000.0,
        fee: Some(0.0),
        is_maker: true,
        forced_close: None,
        venue_ts_ms,
        recv_ns: 4,
    }
}

/// A previous run: one order, its fill, and then nothing. Every horizon that
/// fill was owed died with that process before this change.
fn owed_replay(venue_ts_ms: i64) -> Vec<WalRecord> {
    vec![
        WalRecord::Boot {
            version: "old".into(),
            config_sha256: "abc".into(),
            wall_ts_ms: recent_replay_ms(),
            commit: String::new(),
        },
        WalRecord::OrderSent {
            dispatch: None,
            request: owed_order(),
            wire_ns: 1,
            arrival_mid: 30_000.0,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: owed_fill(venue_ts_ms),
        },
    ]
}

/// What the venue holds because of that fill.
fn owed_position() -> Vec<PositionView> {
    vec![PositionView {
        exact_amounts: None,
        exact_stop_px: None,
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        entry_px: 30_000.0,
        stop_attached: false,
        stop_px: 0.0,
        leverage: None,
    }]
}

/// Every markout in a log, keyed the way the record itself keys a horizon.
fn marks_in(records: &[WalRecord]) -> Vec<(String, i64, u64)> {
    records
        .iter()
        .filter_map(|record| match record {
            WalRecord::Markout {
                client_order_id,
                fill_ts_ms,
                horizon_ms,
                ..
            } => Some((client_order_id.clone(), *fill_ts_ms, *horizon_ms)),
            _ => None,
        })
        .collect()
}

#[tokio::test(start_paused = true)]
async fn a_restart_writes_the_marks_the_last_process_still_owed() {
    // The obligation lives in the log, not in memory. A restart used to end
    // every horizon a fill was still owed, so the 1m and 5m columns lost
    // whatever traded near a deploy.
    let filled_ms = stale_fill_ms();
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, h) = build_with_venue_state(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &owed_replay(filled_ms),
        Vec::new(),
        owed_position(),
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(400)),
        )
        .await
        .unwrap();

    let written = marks_in(&h.records.lock().unwrap());
    assert_eq!(
        written,
        HORIZONS_MS
            .iter()
            .map(|horizon| (OWED_ID.to_string(), filled_ms, *horizon))
            .collect::<Vec<_>>(),
        "one mark per horizon, and no horizon twice"
    );
    let total = engine.fills().total();
    assert_eq!(
        total.marks_late, 3,
        "the first three horizons were past their lateness bound at boot"
    );
    assert_eq!(
        total.marks_late_across_restart, 3,
        "and they were late because the process stopped, not because it stalled"
    );
    assert!(
        total.markout[HORIZONS_MS.len() - 1].mean().is_some(),
        "the five-minute horizon came round while this engine was up"
    );
}

#[tokio::test(start_paused = true)]
async fn a_rotation_between_the_fill_and_the_restart_keeps_the_obligation() {
    // Boot replays one segment. A rotation that did not restate the owed
    // horizons would lose them at the segment boundary even without a crash.
    let filled_ms = stale_fill_ms();
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, _h) = build(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[
            WalRecord::Boot {
                version: "old".into(),
                config_sha256: "abc".into(),
                wall_ts_ms: recent_replay_ms(),
                commit: String::new(),
            },
            WalRecord::OrderSent {
                dispatch: None,
                request: owed_order(),
                wire_ns: 1,
                arrival_mid: 30_000.0,
            },
        ],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    // Delivered now, stamped by the venue 301 seconds ago: this engine dates
    // it from its own clock and owes it every horizon, and the restatement
    // carries the venue's stamp, which is the only clock the next process
    // shares.
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed {
                learned: Rc::new(RefCell::new(Vec::new())),
                updates: VecDeque::from(vec![owed_fill(filled_ms)]),
            },
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let base = engine.rotation_base(clock::wall_ms());
    let WalRecord::SegmentBase {
        ref owed_markouts, ..
    } = base
    else {
        panic!("a rotation writes a segment base");
    };
    assert_eq!(owed_markouts.len(), 1, "the rotation restates it");
    assert_eq!(owed_markouts[0].client_order_id, OWED_ID);
    assert_eq!(owed_markouts[0].fill_ts_ms, filled_ms);
    assert_eq!(owed_markouts[0].owed, 0b1111, "every horizon still owed");

    let (mut restarted, h) = build_with_venue_state(
        allow_all(),
        vec![Box::new(Buyer::new("BTCUSDT", 100, 0.01).0)],
        &["BTCUSDT"],
        std::slice::from_ref(&base),
        Vec::new(),
        owed_position(),
    )
    .await;
    let symbol = restarted.market().table.get("BTCUSDT").unwrap();
    restarted
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(400)),
        )
        .await
        .unwrap();

    assert_eq!(
        marks_in(&h.records.lock().unwrap()),
        HORIZONS_MS
            .iter()
            .map(|horizon| (OWED_ID.to_string(), filled_ms, *horizon))
            .collect::<Vec<_>>(),
        "the obligation survived the segment boundary and was answered"
    );
}

#[tokio::test(start_paused = true)]
async fn replaying_one_log_twice_writes_no_mark_twice() {
    // The second boot reads the marks the first one wrote and asks for
    // nothing more. Without that, every restart would add another copy of
    // every horizon and the columns would count one fill many times.
    let filled_ms = stale_fill_ms();
    let replayed = owed_replay(filled_ms);
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, first) = build_with_venue_state(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &replayed,
        Vec::new(),
        owed_position(),
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(400)),
        )
        .await
        .unwrap();

    let mut once = replayed.clone();
    once.extend(first.records.lock().unwrap().iter().cloned());
    assert_eq!(marks_in(&once).len(), HORIZONS_MS.len());

    let (mut again, second) = build_with_venue_state(
        allow_all(),
        vec![Box::new(Buyer::new("BTCUSDT", 100, 0.01).0)],
        &["BTCUSDT"],
        &once,
        Vec::new(),
        owed_position(),
    )
    .await;
    let symbol = again.market().table.get("BTCUSDT").unwrap();
    again
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(400)),
        )
        .await
        .unwrap();

    assert!(
        marks_in(&second.records.lock().unwrap()).is_empty(),
        "every horizon was already answered in the log this booted from"
    );
    // And the report read off that one log counts each horizon once, with
    // the restart named as the reason the first three were late.
    let read_back = crate::execution::Fills::from_records(&once).total();
    assert_eq!(read_back.marks_late, 3);
    assert_eq!(read_back.marks_late_across_restart, 3);
    assert!(read_back.markout[HORIZONS_MS.len() - 1].mean().is_some());
}
