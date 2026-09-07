//! Boot compares the log against the venue.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

#[tokio::test(start_paused = true)]
async fn a_quiet_account_leaves_the_engine_free_to_trade() {
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
    assert_eq!(h.sends.lock().unwrap().len(), 1, "nothing was in the way");
    let latched = h
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|r| matches!(r, WalRecord::Reconciled { may_open, .. } if !may_open));
    assert!(!latched, "there was nothing to latch on");
}

#[tokio::test(start_paused = true)]
async fn an_order_this_engine_never_placed_stops_it_opening() {
    // Another writer on the account makes every number the kernel works from
    // measure somebody else's trading as well as its own.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
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

    assert!(
        h.sends.lock().unwrap().is_empty(),
        "no order should have left the box"
    );
    let records = h.records.lock().unwrap();
    assert!(
        records.iter().any(|r| matches!(
            r,
            WalRecord::Reconciled {
                may_open: false,
                ..
            }
        )),
        "boot must write down that it stopped opening"
    );
    // The intent is still recorded, and refused. A strategy that is never
    // told no is a strategy nobody can debug.
    assert!(records
        .iter()
        .any(|r| matches!(r, WalRecord::Intent { .. })));
    assert!(records.iter().any(|r| matches!(
        r,
        WalRecord::Verdict {
            client_order_id: None,
            verdict: RiskVerdict::Deny { .. }
        }
    )));
}

#[tokio::test(start_paused = true)]
async fn a_hand_trade_in_a_symbol_nobody_here_trades_does_not_stop_it() {
    // The owner trades this account by hand. Stopping for an order in a
    // symbol no strategy can even address would mean stopping most days.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
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
    assert_eq!(
        h.sends.lock().unwrap().len(),
        1,
        "it should still be trading"
    );
}

#[tokio::test(start_paused = true)]
async fn an_operators_clear_resets_the_latch() {
    // "It will reduce only until somebody looks at the log" — this is
    // somebody having looked. The clear record resets the memory, and on a
    // clean account the engine trades again.
    let earlier = vec![
        WalRecord::Reconciled {
            wall_ts_ms: recent_replay_ms(),
            findings: vec!["old debt".to_string()],
            may_open: false,
        },
        WalRecord::LatchCleared {
            wall_ts_ms: recent_replay_ms() + 1,
            note: "looked at the log".to_string(),
            restated_exposure: Vec::new(),
            findings: vec!["old debt".to_string()],
        },
    ];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &earlier).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(
        h.sends.lock().unwrap().len(),
        1,
        "the clear must lift the latch"
    );
}

#[tokio::test(start_paused = true)]
async fn a_clear_resets_the_memory_not_the_check() {
    // The same clear, but the venue still has a second writer's order in a
    // symbol a strategy here trades: boot's own comparison latches again.
    let earlier = vec![
        WalRecord::Reconciled {
            wall_ts_ms: recent_replay_ms(),
            findings: vec!["old debt".to_string()],
            may_open: false,
        },
        WalRecord::LatchCleared {
            wall_ts_ms: recent_replay_ms() + 1,
            note: "looked at the log".to_string(),
            restated_exposure: Vec::new(),
            findings: vec!["old debt".to_string()],
        },
    ];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_venue_orders(
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
    assert!(
        h.sends.lock().unwrap().is_empty(),
        "a fresh finding must latch again"
    );
    let records = h.records.lock().unwrap();
    assert!(
        records.iter().any(|r| matches!(
            r,
            WalRecord::Reconciled {
                may_open: false,
                ..
            }
        )),
        "the new latch must be written down"
    );
}

/// Asks whether somebody else is holding its symbol, and writes down every
/// answer.
struct ForeignProbe {
    symbol: String,
    saw: Rc<RefCell<Vec<bool>>>,
}

impl Strategy for ForeignProbe {
    fn name(&self) -> &str {
        "probe"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            self.saw.lock().unwrap().push(ctx.foreign_position(*symbol));
        }
    }
}

#[tokio::test(start_paused = true)]
async fn a_missing_close_retains_owned_inventory_and_latches_across_restart() {
    let previous = vec![
        WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
            strategies: vec!["carry".to_string(), "probe".to_string()],
            symbols: vec!["ZECUSDT".to_string()],
        }),
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "eng-old-1".to_string(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 2.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: String::new(),
                client_order_id: "eng-old-1".to_string(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 2.0,
                px: 100.0,
                fee: Some(0.01),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: recent_replay_ms(),
                recv_ns: 2,
            },
        },
    ];
    // Strategy 0 answers to the old log's "carry"; the probe is strategy 1.
    let (idle, _) = Buyer::new("ZECUSDT", u64::MAX, 0.01);
    let saw = Rc::new(RefCell::new(Vec::new()));
    let probe = ForeignProbe {
        symbol: "ZECUSDT".to_string(),
        saw: saw.clone(),
    };
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(idle), Box::new(probe)],
        &["ZECUSDT"],
        &previous,
    )
    .await;
    let symbol = engine.market().table.get("ZECUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(
        !saw.lock().unwrap().is_empty(),
        "the probe must have been asked something"
    );
    assert!(
        saw.lock().unwrap().iter().all(|foreign| *foreign),
        "a missing close cannot erase the recorded owner"
    );
    let mut records = previous.clone();
    records.extend(h.records.lock().unwrap().iter().cloned());
    assert!(!records.iter().any(|r| matches!(
        r,
        WalRecord::Retained(engine_types::wal::RetainedWalRecord::ClaimsDropped { .. })
    )));
    assert!(records.iter().any(|r| matches!(
        r,
        WalRecord::Reconciled {
            may_open: false,
            ..
        }
    )));
    let before = crate::attribution::Attribution::try_from_records(&records)
        .unwrap()
        .snapshot();
    assert_eq!(
        before.positions[0].signed_qty,
        engine_types::numeric::Exact::parse_decimal("2").unwrap()
    );
    let (idle, _) = Buyer::new("ZECUSDT", u64::MAX, 0.01);
    let (_restarted, h2) = build(
        allow_all(),
        vec![
            Box::new(idle),
            Box::new(ForeignProbe {
                symbol: "ZECUSDT".into(),
                saw,
            }),
        ],
        &["ZECUSDT"],
        &records,
    )
    .await;
    records.extend(h2.records.lock().unwrap().iter().cloned());
    assert_eq!(
        crate::attribution::Attribution::try_from_records(&records)
            .unwrap()
            .snapshot(),
        before
    );
    assert!(!records.iter().any(|r| matches!(
        r,
        WalRecord::Retained(engine_types::wal::RetainedWalRecord::ClaimsDropped { .. })
    )));
}

#[tokio::test(start_paused = true)]
async fn a_dropped_claim_stays_dropped_after_the_other_sleeve_enters() {
    let previous = vec![
        WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
            strategies: vec!["carry".to_string(), "probe".to_string()],
            symbols: vec!["ZECUSDT".to_string()],
        }),
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "eng-old-1".to_string(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 2.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: String::new(),
                client_order_id: "eng-old-1".to_string(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 2.0,
                px: 100.0,
                fee: Some(0.01),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: recent_replay_ms(),
                recv_ns: 2,
            },
        },
    ];

    let mut log = previous;
    log.push(WalRecord::Retained(
        engine_types::wal::RetainedWalRecord::ClaimsDropped {
            wall_ts_ms: recent_replay_ms(),
            rows: vec![engine_types::FilledTotal {
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                signed_qty: 2.0,
            }],
        },
    ));
    log.push(WalRecord::OrderSent {
        dispatch: None,
        request: OrderRequest {
            client_order_id: "eng-new-1".to_string(),
            strategy: StrategyId(1),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.5,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            exact_terms: None,
            sleeve_effect: None,
            close_position: false,
        },
        wire_ns: 3,
        arrival_mid: 0.0,
    });
    log.push(WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: String::new(),
            client_order_id: "eng-new-1".to_string(),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.5,
            px: 100.0,
            fee: Some(0.01),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: recent_replay_ms() + 1,
            recv_ns: 5,
        },
    });

    // Second boot: the venue now holds the second sleeve's position, so a
    // flat sweep cannot fire. The replayed drop is what keeps the old claim
    // from coming back.
    let (idle, _) = Buyer::new("ZECUSDT", u64::MAX, 0.01);
    let saw2 = Rc::new(RefCell::new(Vec::new()));
    let probe = ForeignProbe {
        symbol: "ZECUSDT".to_string(),
        saw: saw2.clone(),
    };
    let (mut engine, _h) = build_with_venue_state(
        allow_all(),
        vec![Box::new(idle), Box::new(probe)],
        &["ZECUSDT"],
        &log,
        Vec::new(),
        vec![engine_types::PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.5,
            entry_px: 100.0,
            stop_attached: true,
            stop_px: 0.0,
            leverage: None,
        }],
    )
    .await;
    let symbol = engine.market().table.get("ZECUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(
        !saw2.lock().unwrap().is_empty(),
        "the probe must have been asked something"
    );
    assert!(
        saw2.lock().unwrap().iter().all(|foreign| !foreign),
        "the position is the second sleeve's own; the old claim must not come back"
    );
}

#[tokio::test(start_paused = true)]
async fn a_latch_from_an_earlier_boot_survives_the_restart() {
    // The whole point of writing it down. A restart that cleared the latch
    // would turn "stop and tell somebody" into "stop until the next crash",
    // and something restarts this process automatically.
    let earlier = vec![WalRecord::Reconciled {
        wall_ts_ms: recent_replay_ms(),
        findings: vec!["someone else was working an order".to_string()],
        may_open: false,
    }];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    // The venue is quiet now: whatever it was has gone. The latch still holds.
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &earlier).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(
        h.sends.lock().unwrap().is_empty(),
        "the latch did not survive the restart"
    );
}

#[test]
fn a_logged_physical_position_missing_from_native_account_is_unreconciled() {
    use crate::reconcile::{self, Finding};
    use engine_types::numeric::Exact;
    let physical =
        std::collections::BTreeMap::from([(SymbolId(0), Exact::parse_decimal("1564").unwrap())]);
    let result = reconcile::reconcile_positions(
        &crate::inflight::LedgerOfOrders::default(),
        &[],
        (&physical, &Default::default()),
        &[],
        &AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: vec![],
            observed_ns: 1,
        },
        |_| Some(SymbolId(0)),
        |_| Some(1.0),
        |_| Some(0.001),
    )
    .unwrap();
    assert!(result.findings.iter().any(|f|matches!(f,Finding::UnaccountedExposure{symbol:SymbolId(0),venue_qty,logged_qty} if *venue_qty==0.0 && *logged_qty==1564.0)));
    assert!(result.must_not_open());
}
