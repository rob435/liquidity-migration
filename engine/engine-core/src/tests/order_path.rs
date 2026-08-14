//! The order path: what is written, in what order, and what never leaves.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

#[tokio::test]
async fn the_log_is_written_in_order_and_the_barrier_comes_before_the_send() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut feed = ScriptFeed::quotes(symbol, 1, true);
    let mut orders = ScriptOrderFeed::empty();
    engine
        .run(&mut feed, &mut orders, std::future::pending::<()>())
        .await
        .unwrap();

    let kinds = appends(&h.tape);
    let want = [
        "boot",
        "note", // boot says which mode it is in
        "names", // and what its sleeve and symbol ids mean
        "reconciled", // and what the venue said, against the log
        "intent",
        "verdict",
        "order_sent",
        "order_update",
        "latency_ledger",
    ];
    assert_eq!(kinds, want, "log records in order");

    let sent_at = at(&h.tape, &Step::Append("order_sent".into())).unwrap();
    // The first barrier on the tape is boot's; the one this test is about is
    // the order's, which comes after the order was written down.
    let barrier_at = after(&h.tape, &Step::Barrier, sent_at).unwrap();
    let send_at = at(
        &h.tape,
        &Step::Send(h.sends.borrow()[0].client_order_id.clone()),
    )
    .unwrap();
    assert!(sent_at < barrier_at, "the record is written before the fsync");
    assert!(
        barrier_at < send_at,
        "the order is on disk before it is on the wire ({barrier_at} vs {send_at})"
    );

    let id = &h.sends.borrow()[0].client_order_id;
    assert!(id.starts_with("eng-"), "id shape: {id}");
    assert!(id.len() <= 36, "id is short enough for the venue: {id}");
    assert_eq!(h.risk_saw.borrow().len(), 1, "risk hears the reply");
}

#[tokio::test]
async fn the_verdict_record_names_the_order_it_approved() {
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

    let records = h.records.borrow();
    let verdict = records
        .iter()
        .find_map(|r| match r {
            WalRecord::Verdict {
                client_order_id, ..
            } => Some(client_order_id.clone()),
            _ => None,
        })
        .unwrap();
    assert_eq!(verdict, Some(h.sends.borrow()[0].client_order_id.clone()));
}

#[tokio::test]
async fn shadow_mode_sends_nothing_and_says_so() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    assert!(engine.shadow());
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "no order left the box");
    assert!(
        !h.tape
            .borrow()
            .iter()
            .any(|s| matches!(s, Step::Send(_))),
        "the gateway was never called"
    );
    let notes: Vec<String> = h
        .records
        .borrow()
        .iter()
        .filter_map(|r| match r {
            WalRecord::Note { source, text } if source == "shadow" => Some(text.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(notes.len(), 1, "one note per skipped send");
    assert!(notes[0].contains("no send"), "{}", notes[0]);
    // No order fsync in shadow: nothing leaves the box, so durability buys
    // nothing — and a barrier BETWEEN the order record and its "no send"
    // note opened a crash window where replay reported a phantom in-flight
    // order on a run that never sent anything. The two records travel
    // together, and the only fsync is the final shutdown one.
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "shadow mode paid for an order fsync"
    );
    assert_eq!(appends(&h.tape).iter().filter(|k| *k == "order_sent").count(), 1);
    assert!(engine.in_flight_ids().is_empty(), "nothing ever left the box");
    let replayed = crate::replay::describe(&h.records.borrow(), false);
    assert!(replayed.in_flight.is_empty(), "and the log reads back that way");
}

#[tokio::test]
async fn a_refusal_stops_before_the_order_is_written() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let deny = RiskVerdict::Deny {
        reason: DenyReason::MissingStop,
    };
    let (mut engine, h) = build(false, deny, vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 3, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let kinds = appends(&h.tape);
    assert!(!kinds.contains(&"order_sent".to_string()), "{kinds:?}");
    assert!(h.sends.borrow().is_empty(), "nothing reached the venue");
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "no fsync for an order that does not exist (the shutdown one aside)"
    );
    assert_eq!(kinds.iter().filter(|k| *k == "intent").count(), 3);
    assert_eq!(kinds.iter().filter(|k| *k == "verdict").count(), 3);
    assert!(engine.in_flight_ids().is_empty());
}

#[tokio::test]
async fn a_size_below_the_venue_minimum_is_refused_with_a_note() {
    // 0.0004 rounds down to nothing at a step of 0.001.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.0004);
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

    let kinds = appends(&h.tape);
    assert_eq!(
        kinds,
        ["boot", "note", "names", "reconciled", "intent", "verdict", "note", "latency_ledger"]
    );
    assert!(h.sends.borrow().is_empty());
    let note = note_saying(&h.records, "not sent");
    assert!(note.contains("smallest tradable size"), "{note}");
}

#[tokio::test]
async fn the_newest_control_anchor_in_the_log_is_restored_at_boot() {
    // A restart must never hand the day a fresh loss budget or clear a
    // latched trip — the newest anchor in the log is the guard's memory.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let replayed = vec![
        WalRecord::ControlAnchor { source: "risk".into(), state: "{\"old\":true}".into() },
        WalRecord::Note { source: "engine".into(), text: "unrelated".into() },
        WalRecord::ControlAnchor { source: "risk".into(), state: "{\"newest\":true}".into() },
    ];
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let restored = risk.restored.clone();
    let engine = Engine::boot(
        &settings(true),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await
    .expect("boot");
    drop(engine);
    assert_eq!(*restored.borrow(), vec!["{\"newest\":true}".to_string()]);
}

#[tokio::test]
async fn a_recovered_in_flight_order_is_registered_with_the_kernel() {
    // After a restart the partition would otherwise believe every share is
    // free while last boot's orders are still working at the venue.
    let replayed = vec![WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: "eng-1700000000000-4".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.25,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
        },
        wire_ns: 3,
        arrival_mid: 0.0,
    }];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let registered = risk.registered.clone();
    let engine = Engine::boot(
        &settings(true),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await
    .expect("boot");
    drop(engine);
    assert_eq!(
        *registered.borrow(),
        vec![("eng-1700000000000-4".to_string(), 0.25)]
    );
}

#[test]
fn minting_skips_an_id_the_log_already_knows() {
    // boot_ms is a wall clock; a clock stepped back can repeat a previous
    // boot's prefix, and overwriting a recovered order's ledger entry makes
    // a real working order invisible.
    let taken = ["eng-99-1".to_string(), "eng-99-2".to_string()];
    let mut n = 0;
    let id = crate::engine::mint_unused("eng-99-", &mut n, |candidate| {
        taken.contains(&candidate.to_string())
    });
    assert_eq!(id, "eng-99-3");
    assert_eq!(n, 3);
}

#[tokio::test]
async fn a_changed_control_anchor_is_written_and_made_durable() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    engine.risk.anchor_script.push_back("{\"day\":1}".to_string());
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let records = h.records.borrow();
    let anchor_at = records.iter().position(
        |r| matches!(r, WalRecord::ControlAnchor { state, .. } if state == "{\"day\":1}"),
    );
    assert!(anchor_at.is_some(), "the changed anchor never reached the log");
    let tape = h.tape.borrow();
    let write = tape
        .iter()
        .position(|s| matches!(s, Step::Append(k) if k == "control_anchor"))
        .expect("append step");
    assert!(
        tape[write..].contains(&Step::Barrier),
        "the anchor was written but never made durable"
    );
}

#[tokio::test]
async fn a_clean_shutdown_forces_its_tail_to_disk() {
    // Power loss right after a graceful stop must not lose the closing
    // updates and ledger line: completed orders reading back as in flight
    // is the conservative direction, but it is still a lie in the audit.
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

    let tape = h.tape.borrow();
    let last_append = tape
        .iter()
        .rposition(|s| matches!(s, Step::Append(_)))
        .expect("something was appended");
    let last_barrier = tape.iter().rposition(|s| matches!(s, Step::Barrier));
    assert!(
        last_barrier.is_some_and(|b| b > last_append),
        "the log's tail was never forced to disk on the way out"
    );
}

/// Emits a burst of entries with exits at the back, all in one wake.
struct BurstEmitter {
    symbol: String,
    entries: usize,
    exits: usize,
    fired: bool,
}

impl Strategy for BurstEmitter {
    fn name(&self) -> &str {
        "burst"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if self.fired {
                return;
            }
            self.fired = true;
            for i in 0..(self.entries + self.exits) {
                let exit = i >= self.entries;
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol: *symbol,
                    side: if exit { Side::Sell } else { Side::Buy },
                    qty: 0.01,
                    kind: OrderKind::Market,
                    stop: if exit {
                        None
                    } else {
                        Some(StopSpec { trigger_px: quote.bid_px * 0.99 })
                    },
                    reduce_only: exit,
                    tag: if exit { "burst-exit".into() } else { "burst-entry".into() },
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
        }
    }
}

#[tokio::test]
async fn a_flooded_wake_drops_entries_but_never_exits() {
    // The cap exists so a runaway strategy cannot wedge the loop — but a
    // de-risking order queued behind the flood must still get out, or the
    // strategy is stranded holding a position it believes it exited.
    let burst = BurstEmitter { symbol: "BTCUSDT".into(), entries: 68, exits: 2, fired: false };
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    let exits = sends.iter().filter(|s| s.reduce_only).count();
    let entries = sends.iter().filter(|s| !s.reduce_only).count();
    assert_eq!(exits, 2, "both exits reach the venue");
    assert!(entries <= 64, "the flood of entries is capped: {entries}");
    let note = note_saying(&h.records, "dropped");
    assert!(note.contains("entries"), "the drop is named: {note}");
}

/// Emits one reduce-only exit that (wrongly) still carries a stop.
struct SloppyExiter {
    symbol: String,
    sent: bool,
}

impl Strategy for SloppyExiter {
    fn name(&self) -> &str {
        "sloppy-exiter"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if !self.sent {
                self.sent = true;
                ctx.place(Intent {
                    strategy: StrategyId(0),
                    symbol: *symbol,
                    side: Side::Sell,
                    qty: 0.01,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec {
                        trigger_px: quote.bid_px * 0.99,
                    }),
                    reduce_only: true,
                    tag: "sloppy-exit".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
        }
    }
}

#[tokio::test]
async fn an_exit_sheds_its_stop_before_the_log_and_the_wire() {
    // The venue rejects a reduce-only order carrying stop fields, and the
    // log must record what was actually sent — so the stop comes off at
    // request build, not just at the venue boundary.
    let exiter = SloppyExiter { symbol: "BTCUSDT".into(), sent: false };
    let (mut engine, h) = build(false, allow_all(), vec![Box::new(exiter)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    assert_eq!(sends.len(), 1);
    assert!(sends[0].reduce_only);
    assert!(sends[0].stop.is_none(), "the wire saw a stop on an exit");
    for record in h.records.borrow().iter() {
        if let WalRecord::OrderSent { request, .. } = record {
            assert!(request.stop.is_none(), "the log saw a stop on an exit");
        }
    }
}

#[tokio::test]
async fn an_intent_with_an_unreal_number_never_reaches_the_log() {
    // serde_json writes a non-finite float as null, which cannot be read
    // back into f64 — so a NaN quantity appended to the log would make that
    // frame unreadable and stop the next boot's replay. The refusal must
    // come before any log write.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, f64::NAN);
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

    let kinds = appends(&h.tape);
    assert!(
        !kinds.contains(&"intent".to_string()),
        "a NaN intent was written to the log: {kinds:?}"
    );
    assert!(h.sends.borrow().is_empty(), "nothing reached the venue");
    let note = note_saying(&h.records, "not a finite");
    assert!(note.contains("buy"), "the note names the intent's tag: {note}");
    for record in h.records.borrow().iter() {
        let bytes = serde_json::to_vec(record).expect("serializable");
        let _: WalRecord = serde_json::from_slice(&bytes)
            .expect("every log record must survive a read-back");
    }
}

#[tokio::test]
async fn an_order_under_the_minimum_value_is_refused() {
    // 0.001 of a 30k coin is 30 dollars; ask for a minimum of 5 and it passes,
    // so raise the size floor by asking for a tiny quantity of a cheap symbol.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.001);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.rules[0].1.min_notional = 1_000_000.0;
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(sends.borrow().is_empty());
    let note = note_saying(&records, "not sent");
    assert!(note.contains("smallest order value"), "{note}");
}

#[tokio::test]
async fn a_send_with_no_answer_leaves_the_order_in_flight() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Transport("connection reset".into()));
    let (risk, risk_saw) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(sends.borrow().len(), 1, "it did go out");
    assert_eq!(engine.in_flight_ids().len(), 1, "and we do not know its fate");
    assert!(risk_saw.borrow().is_empty(), "no reply, so nothing to tell risk");
    assert!(note_saying(&records, "no answer").contains(&sends.borrow()[0].client_order_id));
}

#[tokio::test]
async fn a_rejected_order_is_over() {
    let (buyer, heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (mut venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Rejected {
        code: 110007,
        message: "not enough balance".into(),
    });
    let (risk, risk_saw) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(false),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(engine.in_flight_ids().is_empty());
    assert_eq!(risk_saw.borrow().len(), 1);
    assert_eq!(heard.borrow().len(), 1, "the strategy hears its rejection");
    assert!(heard.borrow()[0].contains("110007"), "{:?}", heard.borrow());
}

#[tokio::test]
async fn an_order_left_in_flight_by_the_last_run_comes_back_and_is_not_resent() {
    let stale = OrderRequest {
        client_order_id: "eng-1700000000000-4".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
    };
    let finished = OrderRequest {
        client_order_id: "eng-1700000000000-3".into(),
        ..stale.clone()
    };
    let replayed = vec![
        WalRecord::Boot {
            version: "old".into(),
            config_sha256: "abc".into(),
            wall_ts_ms: 1,
        },
        WalRecord::OrderSent {
            request: finished.clone(),
            wire_ns: 1,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: finished.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.01,
                px: 30_000.0,
                fee: 0.1,
                is_maker: false,
                venue_ts_ms: 2,
                recv_ns: 2,
            },
        },
        WalRecord::OrderSent {
            request: stale.clone(),
            wire_ns: 3,
            arrival_mid: 0.0,
        },
    ];

    let (buyer, heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &replayed,
    )
    .await;
    assert_eq!(
        engine.in_flight_ids(),
        vec!["eng-1700000000000-4"],
        "the unanswered one, and only it"
    );

    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![OrderUpdate::Fill {
            client_order_id: stale.client_order_id.clone(),
            symbol,
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: 0.1,
            is_maker: false,
            venue_ts_ms: 4,
            recv_ns: 4,
        }]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    assert!(h.sends.borrow().is_empty(), "boot never re-sends anything");
    assert!(
        engine.in_flight_ids().is_empty(),
        "the late fill closes the recovered order"
    );
    assert_eq!(
        heard.borrow().len(),
        1,
        "and the strategy that placed it hears the news"
    );
}

#[tokio::test]
async fn timers_fire_for_the_strategy_that_armed_them() {
    let (one, fired_one) = Ticker::new("BTCUSDT", 11, 3_000_000);
    let (two, fired_two) = Ticker::new("ETHUSDT", 22, 5_000_000);
    let (mut engine, _h) = build(
        false,
        allow_all(),
        vec![Box::new(one), Box::new(two)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    let btc = engine.market().table.get("BTCUSDT").unwrap();
    let eth = engine.market().table.get("ETHUSDT").unwrap();
    let mut feed = ScriptFeed {
        events: VecDeque::from(vec![
            MarketEvent::Quote {
                symbol: btc,
                quote: Quote {
                    bid_px: 30_000.0,
                    ask_px: 30_000.5,
                    recv_ns: clock::now_ns(),
                    ..Quote::default()
                },
            },
            MarketEvent::Quote {
                symbol: eth,
                quote: Quote {
                    bid_px: 2_000.0,
                    ask_px: 2_000.5,
                    recv_ns: clock::now_ns(),
                    ..Quote::default()
                },
            },
        ]),
        close_at_end: false,
        admitted: Rc::new(RefCell::new(Vec::new())),
        known: 1,
        admits_wrongly: false,
    };
    engine
        .run(
            &mut feed,
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(80)),
        )
        .await
        .unwrap();

    assert_eq!(*fired_one.borrow(), vec![TimerId(11)], "each hears its own");
    assert_eq!(*fired_two.borrow(), vec![TimerId(22)]);
}

#[tokio::test]
async fn a_market_message_only_reaches_the_strategies_that_asked_for_it() {
    let (btc_buyer, btc_heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (eth_buyer, eth_heard) = Buyer::new("ETHUSDT", 1, 0.01);
    let (mut engine, h) = build(
        false,
        allow_all(),
        vec![Box::new(btc_buyer), Box::new(eth_buyer)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    let btc = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(btc, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow().len(), 1, "one quote, one order");
    assert_eq!(h.sends.borrow()[0].strategy, StrategyId(0));
    assert_eq!(btc_heard.borrow().len(), 1, "its owner hears the reply");
    assert!(eth_heard.borrow().is_empty(), "the other strategy hears nothing");
}

#[tokio::test]
async fn the_group_flush_tick_pushes_the_log_out() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut small_flush = settings(false);
    small_flush.group_flush_ms = 5;
    let mut engine = Engine::boot(&small_flush, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let flushes = tape.borrow().iter().filter(|s| **s == Step::Flush).count();
    assert!(flushes >= 3, "the tick keeps flushing, saw {flushes}");
}

#[tokio::test]
async fn the_account_reading_is_refreshed_before_it_goes_stale() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut quick = settings(false);
    quick.group_flush_ms = 5;
    quick.account_view_max_age_ms = 20; // refreshed at half of this
    let mut engine = Engine::boot(&quick, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    let reads = tape
        .borrow()
        .iter()
        .filter(|s| **s == Step::ReadAccount)
        .count();
    assert!(reads >= 3, "one at boot and more as it ages, saw {reads}");
}

#[tokio::test]
async fn boot_reads_the_rules_and_the_account_before_anything_else() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (_engine, h) = build(true, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let steps = h.tape.borrow().clone();
    assert_eq!(steps[0], Step::Append("boot".into()));
    assert_eq!(steps[1], Step::Append("note".into()), "which mode it is in");
    assert_eq!(
        steps[2],
        Step::Append("names".into()),
        "what the ids mean, before any record uses one"
    );
    assert_eq!(steps[3], Step::ReadRules);
    assert_eq!(steps[4], Step::ReadAccount);
}

#[tokio::test]
async fn the_bench_runs_the_real_loop_and_fills_the_histograms() {
    let path = temp_path("bench-smoke");
    let options = BenchOptions {
        events: 300,
        rate: 0,
        every_nth: 10,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
    };
    let result = bench::run(&options).await.expect("bench");
    assert_eq!(result.events, 300);
    assert_eq!(result.orders, 30, "one order every tenth quote");
    for (segment, q) in &result.segments {
        assert!(q.count > 0, "{segment:?} recorded nothing");
        assert!(q.p50_ns > 0, "{segment:?} p50 is zero");
        assert!(q.max_ns >= q.p50_ns);
    }
    // The log is real and reads back.
    let report = crate::replay::read(path.path()).unwrap();
    assert!(report.records > 30, "records: {}", report.records);
    assert!(!report.torn_tail);
    assert!(result.table().contains("write it down"));
    assert!(result.as_json().contains("\"orders\":30"));
}
