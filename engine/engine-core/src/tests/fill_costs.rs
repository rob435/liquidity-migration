//! What the fills cost.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

#[tokio::test]
async fn the_order_record_carries_the_midpoint_it_will_be_judged_against() {
    // Without this the log can say what we sent and what we got, and never
    // what the difference was worth: by the time a rested entry fills, the
    // price it was decided against is gone.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut feed = ScriptFeed::quotes(symbol, 1, true);
    engine
        .run(&mut feed, &mut ScriptOrderFeed::empty(), std::future::pending::<()>())
        .await
        .unwrap();

    let records = h.records.borrow();
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

#[tokio::test]
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
    };
    let replayed = vec![
        WalRecord::Boot {
            version: "old".into(),
            config_sha256: "abc".into(),
            wall_ts_ms: 1,
        },
        WalRecord::OrderSent {
            request: before.clone(),
            wire_ns: 1,
            arrival_mid: 30_000.0,
        },
    ];

    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, _h) = build(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &replayed,
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![OrderUpdate::Fill {
            client_order_id: before.client_order_id.clone(),
            symbol,
            side: Side::Buy,
            // Three basis points above the midpoint that order was sent at.
            px: 30_009.0,
            qty: 0.01,
            fee: 0.0,
            is_maker: true,
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
    assert!((shortfall - 3.0).abs() < 0.01, "3 bp against the old anchor, got {shortfall}");
}

#[tokio::test]
async fn a_fill_the_log_cannot_anchor_is_counted_but_not_priced() {
    // A fill for an order this log never sent — somebody hand-trading the
    // same account. It is not ours to score, and guessing a price for it
    // would put another person's execution in our own numbers.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, _h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![OrderUpdate::Fill {
            client_order_id: "placed-by-hand".into(),
            symbol,
            side: Side::Buy,
            px: 30_009.0,
            qty: 0.01,
            fee: 0.0,
            is_maker: false,
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

#[tokio::test]
async fn two_sleeves_running_one_plug_are_told_apart_by_their_config_names() {
    // This fleet runs `target_book` twice. Named by the plug, the log and the
    // heartbeat said "target_book" twice and nobody could tell carry's
    // trading from long's.
    let (one, _a) = Buyer::new("BTCUSDT", 100, 0.01);
    let (two, _b) = Buyer::new("ETHUSDT", 100, 0.01);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT", "ETHUSDT"]);
    let (risk, _saw) = MockRisk::with(allow_all());
    let engine = Engine::boot_as(
        &settings(true),
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

    assert_eq!(engine.strategy_names(), &["carry".to_string(), "long".to_string()]);
    let said = records
        .borrow()
        .iter()
        .find_map(|r| match r {
            WalRecord::Names { strategies, .. } => Some(strategies.clone()),
            _ => None,
        })
        .expect("the log says what its ids mean");
    assert_eq!(said, vec!["carry".to_string(), "long".to_string()]);
}

#[tokio::test]
async fn a_sleeve_with_no_name_of_its_own_keeps_the_plugs() {
    // The fallback, and the reason `boot` can stay a one-liner over `boot_as`.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (_engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let said = h
        .records
        .borrow()
        .iter()
        .find_map(|r| match r {
            WalRecord::Names { strategies, .. } => Some(strategies.clone()),
            _ => None,
        })
        .expect("the log says what its ids mean");
    assert_eq!(said, vec!["buyer".to_string()], "the plug's own name");
}

#[tokio::test]
async fn the_bench_can_fill_what_it_accepts_and_the_whole_cost_path_runs() {
    // Every other test here drives one piece. This drives the loop: a real
    // engine, a real log with its fsync, orders that come back filled, and a
    // markout queue drained by the group-flush tick.
    //
    // It is paced rather than run flat out, because the shortest horizon is a
    // second and a bench that finishes in eighty milliseconds proves nothing
    // about a mark ever coming due.
    let path = temp_path("bench-fills");
    let options = BenchOptions {
        events: 600,
        rate: 400,
        every_nth: 50,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
        fills: true,
    };
    let result = bench::run(&options).await.expect("the bench runs");
    assert!(result.orders > 0, "no orders, nothing to price");

    let (replayed, _torn) = engine_wal::replay_scan(path.path()).expect("the log reads back");
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    let costs = crate::execution::Fills::from_records(&records).total();

    // All but at most the last. The run ends when the market feed closes, and
    // a fill still in the channel at that moment is never read -- which is the
    // truthful shape of a process that stops, not something to pad over.
    assert!(
        costs.fills + 1 >= result.orders && costs.fills <= result.orders,
        "{} fills for {} orders",
        costs.fills,
        result.orders
    );
    assert!(costs.notional_usdt > 0.0);
    // The venue charges a flat two basis points, so this is arithmetic and not
    // a range: if it drifts, the fee is not reaching the ledger.
    let fee = costs.fee.mean().expect("the fee was priced");
    assert!((fee - 2.0).abs() < 0.01, "expected 2 bp of fee, got {fee}");
    // Priced against the book each order left at, which the log carries.
    let arrival = costs.arrival_shortfall.mean().expect("the arrival was priced");
    assert!(arrival.abs() < 5.0, "half a tick on a 30,000 book, got {arrival}");
    // Equal to the sum of the two means only because every fill here had both
    // halves; it is accumulated per fill, so the float arithmetic is not the
    // same order and the last bit differs.
    let all_in = costs.all_in_arrival_bps().expect("both halves, every fill");
    assert!((all_in - (arrival + fee)).abs() < 1e-9, "{all_in} vs {}", arrival + fee);

    // And the part nothing else exercises: a horizon came round, the tick read
    // the book, and the mark went into the log.
    let marks = records
        .iter()
        .filter(|r| matches!(r, WalRecord::Markout { .. }))
        .count();
    assert!(marks > 0, "no markout came due in {} records", records.len());
    assert!(
        costs.markout[0].mean().is_some(),
        "the one-second bucket is empty despite {marks} mark(s)"
    );
}

#[tokio::test]
async fn the_bench_fills_nothing_unless_it_is_asked_to() {
    // The default has to stay the run the latency table was measured on.
    let path = temp_path("bench-no-fills");
    let options = BenchOptions {
        events: 200,
        rate: 0,
        every_nth: 50,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
        ..BenchOptions::default()
    };
    assert!(!options.fills, "off unless asked");
    bench::run(&options).await.expect("the bench runs");
    let (replayed, _torn) = engine_wal::replay_scan(path.path()).expect("the log reads back");
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    assert_eq!(crate::execution::Fills::from_records(&records).total().fills, 0);
}
