//! Fills the private stream never delivered, recovered from the venue's own
//! execution history after a reconnect.

use super::*;

/// Wait for the recovery pass to write its record, so the assertions below are
/// about what the engine did rather than about how fast it did it.
async fn until_recovered(records: Rc<RefCell<Vec<WalRecord>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        if records
            .borrow()
            .iter()
            .any(|r| matches!(r, WalRecord::RecoveredFill { .. }))
        {
            return;
        }
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

/// What one recovery pass left behind.
struct Recovered {
    risk_saw: Rc<RefCell<Vec<OrderUpdate>>>,
    records: Rc<RefCell<Vec<WalRecord>>>,
    costs: crate::execution::Costs,
    counted: u64,
}

/// One buy, then a stream reset with the venue's history carrying that order's
/// fill.
async fn recover_one_fill() -> Recovered {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let sent = h.sends.borrow()[0].client_order_id.clone();
    h.risk_saw.borrow_mut().clear();

    // The venue traded it while the stream was away.
    *h.executions.borrow_mut() = Some(vec![VenueExecution {
        exec_id: "e-1".into(),
        client_order_id: sent,
        symbol: "BTCUSDT".into(),
        side: Side::Buy,
        qty: 0.01,
        px: 30_000.0,
        fee: 0.18,
        is_maker: false,
        venue_ts_ms: clock::wall_ms(),
    }]);
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            until_recovered(h.records.clone()),
        )
        .await
        .unwrap();
    Recovered {
        risk_saw: h.risk_saw.clone(),
        records: h.records.clone(),
        costs: engine.fills().total(),
        counted: engine.fills().recovered,
    }
}

#[tokio::test]
async fn a_recovered_fill_reaches_the_risk_kernel() {
    // The kernel reserved this order's size when it approved it, and only a
    // fill releases the reservation. A recovered fill that stopped at the log
    // would leave the position counted twice for the life of the process —
    // once as a reservation nothing ever ends, once in the account view — and
    // every later entry would be judged against the sum.
    let Recovered { risk_saw, records, .. } = recover_one_fill().await;

    assert!(
        records
            .borrow()
            .iter()
            .any(|r| matches!(r, WalRecord::RecoveredFill { .. })),
        "the recovery itself has to have happened for this test to say anything"
    );
    let fills: Vec<f64> = risk_saw
        .borrow()
        .iter()
        .filter_map(|u| match u {
            OrderUpdate::Fill { qty, .. } => Some(*qty),
            _ => None,
        })
        .collect();
    assert_eq!(fills, vec![0.01], "the kernel was not told what filled");
}

#[tokio::test]
async fn a_recovered_blank_fill_is_not_laundered_into_the_only_sleeve() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let sent = h.sends.borrow()[0].client_order_id.clone();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::Fill {
                exec_id: "entry-exec".into(),
                client_order_id: sent,
                symbol,
                side: Side::Buy,
                qty: 0.01,
                px: 30_000.0,
                fee: 0.18,
                is_maker: false,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
            }]),
            tokio::time::sleep(Duration::from_millis(20)),
        )
        .await
        .unwrap();
    let attributed_qty = |engine: &Engine<MockWal, MockRisk, MockVenue>| {
        let WalRecord::SegmentBase { attribution, .. } = engine.rotation_base(1) else {
            unreachable!()
        };
        attribution
            .into_iter()
            .find(|row| row.strategy == StrategyId(0) && row.symbol == symbol)
            .map(|row| row.signed_qty)
            .unwrap_or(0.0)
    };
    assert_eq!(attributed_qty(&engine), 0.01);

    *h.executions.borrow_mut() = Some(vec![VenueExecution {
        exec_id: "native-stop-exec".into(),
        client_order_id: String::new(),
        symbol: "BTCUSDT".into(),
        side: Side::Sell,
        qty: 0.004,
        px: 29_000.0,
        fee: 0.07,
        is_maker: false,
        venue_ts_ms: clock::wall_ms(),
    }]);
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            until_recovered(h.records.clone()),
        )
        .await
        .unwrap();

    assert_eq!(attributed_qty(&engine), 0.01);
    assert!(h.records.borrow().iter().any(|record| matches!(
        record,
        WalRecord::Reconciled { may_open: false, findings, .. }
            if findings.iter().any(|line| line.contains("blank client id"))
    )));
    let WalRecord::SegmentBase { may_open, logged_exposure, .. } = engine.rotation_base(2) else {
        unreachable!()
    };
    assert!(!may_open);
    assert_eq!(logged_exposure.len(), 1);
    assert_eq!(logged_exposure[0].signed_qty, 0.01);
}

#[tokio::test]
async fn a_repeated_live_exec_id_mutates_the_engine_once() {
    let (mut engine, h) = build(allow_all(), Vec::new(), &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let fill = OrderUpdate::Fill {
        exec_id: "exec-once".to_string(), client_order_id: "external".to_string(),
        symbol, side: Side::Buy, qty: 1.0, px: 100.0, fee: 0.0,
        is_maker: false, venue_ts_ms: clock::wall_ms(), recv_ns: 1,
    };
    engine.run(
        &mut ScriptFeed::quotes(symbol, 0, false),
        &mut ScriptOrderFeed::playing(vec![fill.clone(), fill]),
        tokio::time::sleep(Duration::from_millis(20)),
    ).await.unwrap();
    let journaled = h.records.borrow().iter().filter(|record| matches!(
        record, WalRecord::OrderUpdate { update: OrderUpdate::Fill { exec_id, .. } }
            if exec_id == "exec-once"
    )).count();
    assert_eq!(journaled, 1);
    assert_eq!(h.risk_saw.borrow().iter().filter(|u| matches!(u, OrderUpdate::Fill { .. })).count(), 1);
    let WalRecord::SegmentBase { may_open, logged_exposure, .. } = engine.rotation_base(1) else {
        unreachable!()
    };
    assert!(!may_open, "an unowned live fill latches entries off");
    assert!(logged_exposure.is_empty(), "an unowned fill is not trusted exposure");
}

#[tokio::test]
async fn execution_history_failure_after_a_gap_stops_the_run_and_latches_entries() {
    let (mut engine, h) = build(allow_all(), Vec::new(), &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    *h.executions.borrow_mut() = None;

    let result = engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            std::future::pending::<()>(),
        )
        .await;

    assert!(matches!(result, Err(EngineError::Venue(_))));
    assert!(h.records.borrow().iter().any(|record| matches!(
        record,
        WalRecord::Reconciled { may_open: false, findings, .. }
            if findings.iter().any(|line| line.contains("execution history is unavailable"))
    )));
    let WalRecord::SegmentBase { may_open, .. } = engine.rotation_base(1) else {
        unreachable!()
    };
    assert!(!may_open);
}

#[tokio::test]
async fn execution_history_failure_aborts_boot() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    *venue.executions.borrow_mut() = None;
    let (risk, _) = MockRisk::with(allow_all());
    let (buyer, _) = Buyer::new("BTCUSDT", 0, 0.01);
    let replayed = vec![WalRecord::Boot {
        version: ENGINE_VERSION.into(),
        config_sha256: "old".into(),
        wall_ts_ms: clock::wall_ms() - 1_000,
    }];

    let result = Engine::boot(
        &settings(),
        "new",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await;

    assert!(matches!(result, Err(EngineError::Boot(message)) if message.contains("execution history")));
}

#[tokio::test]
async fn a_recovered_fill_is_in_what_the_trading_cost() {
    // It traded, so it cost something. Left out, the traded notional is short
    // by however much the stream missed and every mean taken over it is a
    // mean over the fills that happened to be delivered.
    let Recovered { costs, counted, .. } = recover_one_fill().await;

    assert_eq!(costs.fills, 1, "the cost ledger never heard about it");
    assert_eq!(counted, 1, "and could not say how much of itself arrived late");
    assert!((costs.notional_usdt - 300.0).abs() < 1e-9, "{}", costs.notional_usdt);
}

#[tokio::test]
async fn a_fill_the_last_run_was_told_about_is_not_recovered_again() {
    // The pass reaches back two minutes past this boot, and the venue hands
    // back everything in that window. A fill the previous run heard on its own
    // stream is in the log with no venue execution id on it, so nothing but
    // the log itself can say it is already counted -- and counted twice it
    // moves exposure, the claims table, the kernel's reservation and the cost
    // ledger, all by a position that was never opened.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 0, 0.01);
    let already = OrderUpdate::Fill {
        exec_id: String::new(),
        client_order_id: "eng-last-run-1".into(),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        px: 30_000.0,
        fee: 0.18,
        is_maker: false,
        venue_ts_ms: clock::wall_ms() - 30_000,
        recv_ns: 1,
    };
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[WalRecord::OrderUpdate { update: already.clone() }],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let OrderUpdate::Fill { venue_ts_ms, .. } = already else { unreachable!() };

    // Both what the last run already heard and one it never did, so the pass
    // has something to finish on whichever way the dedup goes.
    *h.executions.borrow_mut() = Some(vec![
        VenueExecution {
            exec_id: "e-old".into(),
            client_order_id: "eng-last-run-1".into(),
            symbol: "BTCUSDT".into(),
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: 0.18,
            is_maker: false,
            venue_ts_ms,
        },
        // Same legacy tuple, distinct venue execution. The one delivered log
        // row may consume only one occurrence, not hide both.
        VenueExecution {
            exec_id: "e-old-2".into(),
            client_order_id: "eng-last-run-1".into(),
            symbol: "BTCUSDT".into(),
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: 0.18,
            is_maker: false,
            venue_ts_ms,
        },
        VenueExecution {
            exec_id: "e-new".into(),
            client_order_id: "eng-never-seen".into(),
            symbol: "BTCUSDT".into(),
            side: Side::Buy,
            qty: 0.02,
            px: 30_000.0,
            fee: 0.36,
            is_maker: false,
            venue_ts_ms: clock::wall_ms(),
        },
    ]);
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            until_recovered(h.records.clone()),
        )
        .await
        .unwrap();

    let recovered: Vec<String> = h
        .records
        .borrow()
        .iter()
        .filter_map(|r| match r {
            WalRecord::RecoveredFill { exec_id, .. } => Some(exec_id.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(
        recovered,
        vec!["e-old-2".to_string(), "e-new".to_string()],
        "one legacy row consumes exactly one matching execution"
    );
}
