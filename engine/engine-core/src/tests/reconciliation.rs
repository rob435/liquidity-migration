//! Boot compares the log against the venue.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

#[tokio::test]
async fn a_quiet_account_leaves_the_engine_free_to_trade() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(h.sends.borrow().len(), 1, "nothing was in the way");
    let latched = h.records.borrow().iter().any(|r| {
        matches!(r, WalRecord::Reconciled { may_open, .. } if !may_open)
    });
    assert!(!latched, "there was nothing to latch on");
}

#[tokio::test]
async fn an_order_this_engine_never_placed_stops_it_opening() {
    // Another writer on the account makes every number the kernel works from
    // measure somebody else's trading as well as its own.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        vec![someone_elses_order("BTCUSDT")],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "no order should have left the box");
    let records = h.records.borrow();
    assert!(
        records.iter().any(|r| matches!(r, WalRecord::Reconciled { may_open: false, .. })),
        "boot must write down that it stopped opening"
    );
    // The intent is still recorded, and refused. A strategy that is never
    // told no is a strategy nobody can debug.
    assert!(records.iter().any(|r| matches!(r, WalRecord::Intent { .. })));
    assert!(records.iter().any(|r| matches!(
        r,
        WalRecord::Verdict { client_order_id: None, verdict: RiskVerdict::Deny { .. } }
    )));
}

#[tokio::test]
async fn a_hand_trade_in_a_symbol_nobody_here_trades_does_not_stop_it() {
    // The owner trades this account by hand. Stopping for an order in a
    // symbol no strategy can even address would mean stopping most days.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        vec![someone_elses_order("DOGEUSDT")],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(h.sends.borrow().len(), 1, "it should still be trading");
}

#[tokio::test]
async fn an_operators_clear_resets_the_latch() {
    // "It will reduce only until somebody looks at the log" — this is
    // somebody having looked. The clear record resets the memory, and on a
    // clean account the engine trades again.
    let earlier = vec![
        WalRecord::Reconciled {
            wall_ts_ms: 1,
            findings: vec!["old debt".to_string()],
            may_open: false,
        },
        WalRecord::LatchCleared {
            wall_ts_ms: 2,
            note: "looked at the log".to_string(),
            restated_exposure: Vec::new(),
            findings: vec!["old debt".to_string()],
        },
    ];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &earlier).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(h.sends.borrow().len(), 1, "the clear must lift the latch");
}

#[tokio::test]
async fn a_clear_resets_the_memory_not_the_check() {
    // The same clear, but the venue still has a second writer's order in a
    // symbol a strategy here trades: boot's own comparison latches again.
    let earlier = vec![
        WalRecord::Reconciled {
            wall_ts_ms: 1,
            findings: vec!["old debt".to_string()],
            may_open: false,
        },
        WalRecord::LatchCleared {
            wall_ts_ms: 2,
            note: "looked at the log".to_string(),
            restated_exposure: Vec::new(),
            findings: vec!["old debt".to_string()],
        },
    ];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &earlier,
        vec![someone_elses_order("BTCUSDT")],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(h.sends.borrow().is_empty(), "a fresh finding must latch again");
    let records = h.records.borrow();
    assert!(
        records.iter().any(|r| matches!(r, WalRecord::Reconciled { may_open: false, .. })),
        "the new latch must be written down"
    );
}

#[tokio::test]
async fn a_latch_from_an_earlier_boot_survives_the_restart() {
    // The whole point of writing it down. A restart that cleared the latch
    // would turn "stop and tell somebody" into "stop until the next crash",
    // and something restarts this process automatically.
    let earlier = vec![WalRecord::Reconciled {
        wall_ts_ms: 1,
        findings: vec!["someone else was working an order".to_string()],
        may_open: false,
    }];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    // The venue is quiet now: whatever it was has gone. The latch still holds.
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &earlier).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(h.sends.borrow().is_empty(), "the latch did not survive the restart");
}
