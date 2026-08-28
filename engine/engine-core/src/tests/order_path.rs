//! The order path: what is written, in what order, and what never leaves.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

struct ClosedOrderFeed;
impl OrderFeed for ClosedOrderFeed {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        Err(FeedError::Closed)
    }
}

#[tokio::test]
async fn a_dead_private_feed_stops_for_supervised_recovery() {
    // The engine's symbol table is built from strategy subscriptions, not
    // from the mock venue's rule inventory. No market event is delivered in
    // this test, so this buyer acts only as a passive BTC subscription.
    let (subscriber, _) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, _) = build(allow_all(), vec![Box::new(subscriber)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let outcome = engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ClosedOrderFeed,
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(outcome.stopped_by, StopReason::FeedClosed);
}

#[tokio::test]
async fn the_log_is_written_in_order_and_the_barrier_comes_before_the_send() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
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
        "note",       // boot says which mode it is in
        "names",      // and what its sleeve and symbol ids mean
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
    assert!(
        sent_at < barrier_at,
        "the record is written before the fsync"
    );
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
async fn a_refusal_stops_before_the_order_is_written() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let deny = RiskVerdict::Deny {
        reason: DenyReason::MissingStop,
    };
    let (mut engine, h) = build(deny, vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
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

    let kinds = appends(&h.tape);
    assert_eq!(
        kinds,
        [
            "boot",
            "note",
            "names",
            "reconciled",
            "intent",
            "verdict",
            "note",
            "latency_ledger"
        ]
    );
    assert!(h.sends.borrow().is_empty());
    let note = note_saying(&h.records, "not sent");
    assert!(note.contains("smallest tradable size"), "{note}");
}

#[tokio::test]
async fn retired_control_anchors_are_ignored_and_cannot_halt_entries() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let replayed = vec![
        WalRecord::ControlAnchor {
            source: "risk".into(),
            state: "{\"old\":true}".into(),
        },
        WalRecord::Note {
            source: "engine".into(),
            text: "unrelated".into(),
        },
        WalRecord::ControlAnchor {
            source: "risk".into(),
            state: "{malformed-retired-state".into(),
        },
    ];
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await
    .expect("boot");
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(sends.borrow().len(), 1);
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
    let (mut venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    // The venue confirms it is still working the order; a recovered order
    // the venue does not confirm is reaped at boot instead of registered.
    venue.working = vec![still_working("eng-1700000000000-4", "BTCUSDT", 0.25)];
    let (risk, _seen) = MockRisk::with(allow_all());
    let registered = risk.registered.clone();
    let engine = Engine::boot(
        &settings(),
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

#[tokio::test]
async fn a_part_filled_recovered_order_reserves_only_its_remainder() {
    let id = "eng-1700000000000-5";
    let replayed = vec![
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: id.into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 10.0,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px: 90.0 }),
                reduce_only: false,
            },
            wire_ns: 3,
            arrival_mid: 100.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: "partial".into(),
                client_order_id: id.into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 9.0,
                px: 100.0,
                fee: 0.0,
                is_maker: true,
                venue_ts_ms: recent_replay_ms(),
                recv_ns: 1,
            },
        },
    ];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (mut venue, _sends) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.working = vec![still_working(id, "BTCUSDT", 10.0)];
    venue
        .account_readings
        .borrow_mut()
        .push_back(vec![engine_types::PositionView {
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 9.0,
            entry_px: 100.0,
            stop_px: 90.0,
            stop_attached: true,
            leverage: None,
        }]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let registered = risk.registered.clone();

    let engine = Engine::boot(
        &settings(),
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

    assert_eq!(*registered.borrow(), vec![(id.to_string(), 1.0)]);
}

#[tokio::test]
async fn symbol_ids_survive_a_restart_in_the_log_order() {
    // Ids are interning positions, and every join boot makes against the
    // replayed records — whose fills are whose, what exposure the log
    // accounts for, which symbol an in-flight order is in — names the OLD
    // run's numbers. The log's own Names record, not this config's
    // subscription order, must decide the table.
    let replayed = vec![WalRecord::Names {
        strategies: vec!["carry".into()],
        symbols: vec!["ETHUSDT".into(), "HOMEUSDT".into(), "BTCUSDT".into()],
    }];
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (engine, _h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &replayed).await;
    let table = &engine.market().table;
    assert_eq!(table.get("ETHUSDT"), Some(SymbolId(0)));
    assert_eq!(
        table.get("HOMEUSDT"),
        Some(SymbolId(1)),
        "a symbol a book admitted last run keeps its position"
    );
    assert_eq!(
        table.get("BTCUSDT"),
        Some(SymbolId(2)),
        "the config's own symbol keeps the log's id, not position zero"
    );
}

#[tokio::test]
async fn an_order_the_venue_is_not_working_is_reaped_at_boot() {
    // It ended while the engine was down, and no update for it will ever
    // arrive — the private stream does not replay history. Left "in flight"
    // it would charge the kernel's partition on every future boot and hold
    // the one-order-per-symbol gate closed against the symbol, exits
    // included.
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
    let (wal, records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let registered = risk.registered.clone();
    let engine = Engine::boot(
        &settings(),
        "0000000000000000",
        wal,
        risk,
        venue,
        vec![Box::new(buyer)],
        &replayed,
    )
    .await
    .expect("boot");
    assert!(
        engine.in_flight_ids().is_empty(),
        "the venue is not working it, so it is not in flight"
    );
    assert!(
        registered.borrow().is_empty(),
        "a dead order must not charge the partition"
    );
    // The ending is durable, so the next boot does not rediscover the ghost.
    let reaped = records.borrow().iter().any(|record| {
        matches!(
            record,
            WalRecord::OrderUpdate {
                update: OrderUpdate::Cancelled { client_order_id, .. }
            } if client_order_id == "eng-1700000000000-4"
        )
    });
    assert!(reaped, "the log records the ending the venue proved");
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
async fn a_clean_shutdown_forces_its_tail_to_disk() {
    // Power loss right after a graceful stop must not lose the closing
    // updates and ledger line: completed orders reading back as in flight
    // is the conservative direction, but it is still a lie in the audit.
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

/// Becomes ready only after the first native-sized placement group reached
/// the mock venue. This models private lifecycle news arriving while a
/// larger sibling wake is still being drained.
struct PrivateUpdateAfterFirstPlacementBatch {
    sends: Rc<RefCell<Vec<OrderRequest>>>,
    tape: Tape,
    delivered: bool,
}

impl OrderFeed for PrivateUpdateAfterFirstPlacementBatch {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        while self.sends.borrow().len() < crate::engine::MAX_ORDERS_PER_BATCH {
            tokio::task::yield_now().await;
        }
        if self.delivered {
            return std::future::pending().await;
        }
        self.delivered = true;
        let client_order_id = self.sends.borrow()[0].client_order_id.clone();
        self.tape.borrow_mut().push(Step::PrivateUpdate);
        Ok(OrderUpdate::Ack(OrderAck {
            client_order_id,
            venue_order_id: "private-stream-ack".to_string(),
            ack_ns: clock::now_ns(),
        }))
    }
}

struct PrivateUpdateAfterFirstCancelBatch {
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    tape: Tape,
    delivered: bool,
}

impl OrderFeed for PrivateUpdateAfterFirstCancelBatch {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        while self.cancels.borrow().len() < crate::engine::MAX_CANCELS_PER_BATCH {
            tokio::task::yield_now().await;
        }
        if self.delivered {
            return std::future::pending().await;
        }
        self.delivered = true;
        let client_order_id = self.cancels.borrow()[0].1.clone();
        self.tape.borrow_mut().push(Step::PrivateUpdate);
        Ok(OrderUpdate::Cancelled {
            client_order_id,
            recv_ns: clock::now_ns(),
        })
    }
}

struct CancelBurst {
    symbol: String,
    cancels: usize,
    fired: bool,
}

impl Strategy for CancelBurst {
    fn name(&self) -> &str {
        "cancel-burst"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event else {
            return;
        };
        if self.fired {
            return;
        }
        self.fired = true;
        for i in 0..self.cancels {
            ctx.cancel(*symbol, &format!("eng-cancel-burst-{i}"));
        }
    }
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
                        Some(StopSpec {
                            trigger_px: quote.bid_px * 0.99,
                        })
                    },
                    reduce_only: exit,
                    tag: if exit {
                        "burst-exit".into()
                    } else {
                        "burst-entry".into()
                    },
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
        }
    }
}

#[tokio::test]
async fn sibling_orders_share_one_barrier_before_the_first_send() {
    let burst = BurstEmitter {
        symbol: "BTCUSDT".into(),
        entries: 3,
        exits: 0,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow().len(), 3);
    let tape = h.tape.borrow();
    let sent: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| {
            matches!(step, Step::Append(kind) if kind == "order_sent").then_some(at)
        })
        .collect();
    let sends: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| matches!(step, Step::Send(_)).then_some(at))
        .collect();
    assert_eq!(sent.len(), 3);
    assert_eq!(sends.len(), 3);
    assert!(
        sent[2] < sends[0],
        "every sibling is journaled before any send"
    );
    assert_eq!(
        tape[sent[0]..sends[0]]
            .iter()
            .filter(|step| matches!(step, Step::Barrier))
            .count(),
        1,
        "one durability barrier covers the batch"
    );
}

struct StopSequence {
    symbol: String,
    stops: Vec<f64>,
    fired: bool,
}

struct StopPerWake {
    symbol: String,
    stops: VecDeque<f64>,
}

impl Strategy for StopPerWake {
    fn name(&self) -> &str {
        "stop-per-wake"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event else {
            return;
        };
        let Some(trigger_px) = self.stops.pop_front() else {
            return;
        };
        ctx.place(Intent {
            strategy: StrategyId(0),
            symbol: *symbol,
            side: Side::Buy,
            qty: 0.1,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px }),
            reduce_only: false,
            tag: "stop-per-wake".into(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }
}

impl Strategy for StopSequence {
    fn name(&self) -> &str {
        "stop-sequence"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event else {
            return;
        };
        if self.fired {
            return;
        }
        self.fired = true;
        for (index, trigger_px) in self.stops.iter().copied().enumerate() {
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: Side::Buy,
                qty: 0.1,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px }),
                reduce_only: false,
                tag: format!("stop-{index}"),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
    }
}

#[tokio::test]
async fn a_same_side_sibling_cannot_loosen_the_whole_position_stop() {
    let strategy = StopSequence {
        symbol: "BTCUSDT".into(),
        stops: vec![95.0, 90.0],
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(strategy)], &["BTCUSDT"], &[]).await;
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
    assert_eq!(sends[0].stop, Some(StopSpec { trigger_px: 95.0 }));
    assert!(note_saying(&h.records, "would loosen").contains("whole Buy position"));
}

#[tokio::test]
async fn same_side_siblings_may_tighten_the_whole_position_stop() {
    let strategy = StopSequence {
        symbol: "BTCUSDT".into(),
        stops: vec![90.0, 95.0],
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(strategy)], &["BTCUSDT"], &[]).await;
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
    assert_eq!(sends.len(), 2);
    assert_eq!(sends[0].stop, Some(StopSpec { trigger_px: 90.0 }));
    assert_eq!(sends[1].stop, Some(StopSpec { trigger_px: 95.0 }));
}

#[tokio::test]
async fn a_prior_wakes_unfilled_order_also_prevents_stop_loosening() {
    let strategy = StopPerWake {
        symbol: "BTCUSDT".into(),
        stops: VecDeque::from([95.0, 90.0]),
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(strategy)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    assert_eq!(sends.len(), 1, "the first order is still in flight");
    assert_eq!(sends[0].stop, Some(StopSpec { trigger_px: 95.0 }));
    assert!(note_saying(&h.records, "would loosen").contains("whole Buy position"));
}

#[tokio::test]
async fn a_fresh_account_view_repairs_a_loosened_whole_position_stop() {
    let id = "eng-protected-1";
    let fill_ms = clock::wall_ms();
    let replayed = vec![
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: id.into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px: 95.0 }),
                reduce_only: false,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: "protected-fill".into(),
                client_order_id: id.into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                px: 100.0,
                fee: 0.0,
                is_maker: true,
                venue_ts_ms: fill_ms,
                recv_ns: 1,
            },
        },
    ];
    let protected = engine_types::PositionView {
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        entry_px: 100.0,
        stop_px: 95.0,
        stop_attached: true,
        leverage: None,
    };
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let (mut engine, h) = build_with_venue_state(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &replayed,
        Vec::new(),
        vec![protected.clone()],
    )
    .await;
    let mut loosened = protected;
    loosened.stop_px = 90.0;
    h.account_readings.borrow_mut().push_back(vec![loosened]);
    let stops = h.stops.clone();
    let shutdown = async move {
        loop {
            if !stops.borrow().is_empty() {
                break;
            }
            tokio::task::yield_now().await;
        }
    };

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 2 }]),
            shutdown,
        )
        .await
        .unwrap();

    assert_eq!(*h.stops.borrow(), vec![(SymbolId(0), 95.0)]);
    assert!(h.records.borrow().iter().any(|record| matches!(
        record,
        WalRecord::Note { source, text }
            if source == "stop-supervisor" && text.contains("95")
    )));
}

#[tokio::test]
async fn oversized_sibling_bursts_are_revalidated_after_each_bounded_send() {
    let burst = BurstEmitter {
        symbol: "BTCUSDT".into(),
        entries: 11,
        exits: 0,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
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
    let sent: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| {
            matches!(step, Step::Append(kind) if kind == "order_sent").then_some(at)
        })
        .collect();
    let sends: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| matches!(step, Step::Send(_)).then_some(at))
        .collect();
    assert_eq!(sent.len(), 11);
    assert_eq!(sends.len(), 11);
    assert!(sent[9] < sends[0], "the first ten share one reservation");
    assert!(
        sends[9] < sent[10],
        "the eleventh order was not revalidated after the preceding bounded send"
    );
    let batch_barriers: Vec<usize> = tape
        .iter()
        .enumerate()
        .skip(sent[0])
        .take(sends[10] - sent[0] + 1)
        .filter_map(|(at, step)| matches!(step, Step::Barrier).then_some(at))
        .collect();
    assert_eq!(batch_barriers.len(), 2);
    assert!(
        sent[9] < batch_barriers[0] && batch_barriers[0] < sends[0],
        "the first bounded group is durable before its first send"
    );
    assert!(
        sent[10] < batch_barriers[1] && batch_barriers[1] < sends[10],
        "the second bounded group is durable before its send"
    );
}

#[tokio::test]
async fn private_updates_are_polled_between_bounded_sibling_sends() {
    let burst = BurstEmitter {
        symbol: "BTCUSDT".into(),
        entries: 11,
        exits: 0,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut order_feed = PrivateUpdateAfterFirstPlacementBatch {
        sends: h.sends.clone(),
        tape: h.tape.clone(),
        delivered: false,
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut order_feed,
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let tape = h.tape.borrow();
    let sends: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| matches!(step, Step::Send(_)).then_some(at))
        .collect();
    let private = tape
        .iter()
        .position(|step| matches!(step, Step::PrivateUpdate))
        .expect("the ready private update was never polled");
    assert_eq!(sends.len(), 11);
    assert!(
        sends[9] < private && private < sends[10],
        "private lifecycle news must be applied between native-sized sibling groups"
    );
}

#[tokio::test]
async fn private_updates_are_polled_between_bounded_cancel_groups() {
    let burst = CancelBurst {
        symbol: "BTCUSDT".into(),
        cancels: 11,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut order_feed = PrivateUpdateAfterFirstCancelBatch {
        cancels: h.cancels.clone(),
        tape: h.tape.clone(),
        delivered: false,
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut order_feed,
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let tape = h.tape.borrow();
    let cancels: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| matches!(step, Step::Cancel(_)).then_some(at))
        .collect();
    let private = tape
        .iter()
        .position(|step| matches!(step, Step::PrivateUpdate))
        .expect("the ready private update was never polled");
    assert_eq!(cancels.len(), 11);
    assert!(
        cancels[9] < private && private < cancels[10],
        "private lifecycle news must be applied between native-sized cancel groups"
    );
}

#[tokio::test]
async fn due_account_refresh_is_polled_between_bounded_sibling_sends() {
    let burst = BurstEmitter {
        symbol: "BTCUSDT".into(),
        entries: 11,
        exits: 0,
        fired: false,
    };
    let mut refresh_every_tick = settings();
    refresh_every_tick.account_view_max_age_ms = 0;
    let (mut engine, h) = build_with(
        &refresh_every_tick,
        allow_all(),
        vec![Box::new(burst)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
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

    let tape = h.tape.borrow();
    let sends: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| matches!(step, Step::Send(_)).then_some(at))
        .collect();
    let account_reads: Vec<usize> = tape
        .iter()
        .enumerate()
        .filter_map(|(at, step)| matches!(step, Step::ReadAccount).then_some(at))
        .collect();
    assert_eq!(sends.len(), 11);
    assert!(
        account_reads.len() >= 2,
        "the due refresh never ran; tape={tape:?}"
    );
    assert!(
        sends[9] < account_reads[1] && account_reads[1] < sends[10],
        "the due account view must be adopted between native-sized sibling groups"
    );
}

#[tokio::test]
async fn shutdown_after_a_batch_does_not_abandon_a_trailing_exit() {
    let burst = BurstEmitter {
        symbol: "BTCUSDT".into(),
        entries: 10,
        exits: 1,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let sends = h.sends.clone();
    let shutdown = async move {
        while sends.borrow().len() < crate::engine::MAX_ORDERS_PER_BATCH {
            tokio::task::yield_now().await;
        }
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            shutdown,
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    assert_eq!(
        sends.len(),
        11,
        "shutdown truncated the bounded wake after its first native group"
    );
    assert!(
        sends[10].reduce_only,
        "the trailing risk-reducing sibling was not completed before shutdown"
    );
}

struct ConflictingLeverageSiblings {
    symbol: String,
    fired: bool,
}

impl Strategy for ConflictingLeverageSiblings {
    fn name(&self) -> &str {
        "leverage-conflict"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event else {
            return;
        };
        if self.fired {
            return;
        }
        self.fired = true;
        for (index, leverage) in [3.0, 5.0].into_iter().enumerate() {
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: Side::Buy,
                qty: 0.01,
                kind: OrderKind::Market,
                stop: Some(StopSpec {
                    trigger_px: quote.bid_px * 0.99,
                }),
                reduce_only: false,
                tag: format!("leverage-{index}"),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: Some(leverage),
            });
        }
        // Risk-reducing work does not depend on the entry leverage and must
        // not be stranded behind the conflicting siblings.
        ctx.place(Intent {
            strategy: StrategyId(0),
            symbol: *symbol,
            side: Side::Sell,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "exit".into(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }
}

#[tokio::test]
async fn same_symbol_siblings_with_conflicting_leverage_are_refused_before_mutation() {
    let strategy = ConflictingLeverageSiblings {
        symbol: "BTCUSDT".into(),
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(strategy)], &["BTCUSDT"], &[]).await;
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
        h.leverages.borrow().is_empty(),
        "a rejected leverage epoch still mutated the venue"
    );
    let sends = h.sends.borrow();
    assert_eq!(sends.len(), 1, "only the risk-reducing sibling may flow");
    assert!(sends[0].reduce_only);
    assert_eq!(
        h.records
            .borrow()
            .iter()
            .filter(|record| matches!(record, WalRecord::Intent { .. }))
            .count(),
        3,
        "every sibling remains auditable"
    );
    assert_eq!(
        h.records
            .borrow()
            .iter()
            .filter(
                |record| matches!(record, WalRecord::Note { source, .. } if source == "leverage")
            )
            .count(),
        2
    );
}

#[tokio::test]
async fn a_flooded_wake_drops_entries_but_never_exits() {
    // The cap exists so a runaway strategy cannot wedge the loop — but a
    // de-risking order queued behind the flood must still get out, or the
    // strategy is stranded holding a position it believes it exited.
    let burst = BurstEmitter {
        symbol: "BTCUSDT".into(),
        entries: 68,
        exits: 2,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(burst)], &["BTCUSDT"], &[]).await;
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
    let exiter = SloppyExiter {
        symbol: "BTCUSDT".into(),
        sent: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(exiter)], &["BTCUSDT"], &[]).await;
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

    let kinds = appends(&h.tape);
    assert!(
        !kinds.contains(&"intent".to_string()),
        "a NaN intent was written to the log: {kinds:?}"
    );
    assert!(h.sends.borrow().is_empty(), "nothing reached the venue");
    let note = note_saying(&h.records, "not a finite");
    assert!(
        note.contains("buy"),
        "the note names the intent's tag: {note}"
    );
    for record in h.records.borrow().iter() {
        let bytes = serde_json::to_vec(record).expect("serializable");
        let _: WalRecord =
            serde_json::from_slice(&bytes).expect("every log record must survive a read-back");
    }
}

#[test]
fn invalid_kernel_verdicts_are_finite_fail_closed_records() {
    let verdicts = [
        RiskVerdict::Allow { qty: f64::NAN },
        RiskVerdict::Allow { qty: 2.0 },
        RiskVerdict::Deny {
            reason: DenyReason::EnvelopeBreached {
                worst_case_loss_usdt: f64::INFINITY,
                allowance_usdt: 1.0,
            },
        },
    ];
    for raw in verdicts {
        let verdict = durable_risk_verdict(raw, 1.0, false);
        assert!(matches!(
            verdict,
            RiskVerdict::Deny {
                reason: DenyReason::UnknownState { .. }
            }
        ));
        let record = WalRecord::Verdict {
            client_order_id: None,
            verdict,
        };
        let bytes = serde_json::to_vec(&record).expect("serializable");
        let _: WalRecord = serde_json::from_slice(&bytes).expect("sanitized verdict must replay");
    }
}

#[test]
fn kernel_verdict_cannot_enlarge_a_request_by_even_one_ulp() {
    let requested = 1.0_f64;
    let one_ulp_larger = f64::from_bits(requested.to_bits() + 1);
    let verdict = durable_risk_verdict(
        RiskVerdict::Allow {
            qty: one_ulp_larger,
        },
        requested,
        false,
    );
    assert!(matches!(
        verdict,
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));

    // The former relative tolerance admitted this one-unit enlargement.
    let large_requested = 1_000_000_000_000.0_f64;
    let verdict = durable_risk_verdict(
        RiskVerdict::Allow {
            qty: large_requested + 1.0,
        },
        large_requested,
        false,
    );
    assert!(matches!(
        verdict,
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
}

#[test]
fn amend_verdict_quantity_must_match_exactly() {
    let requested = 1.0_f64;
    for qty in [
        f64::from_bits(requested.to_bits() - 1),
        f64::from_bits(requested.to_bits() + 1),
    ] {
        let verdict = durable_risk_verdict(RiskVerdict::Allow { qty }, requested, true);
        assert!(matches!(
            verdict,
            RiskVerdict::Deny {
                reason: DenyReason::UnknownState { .. }
            }
        ));
    }
    assert_eq!(
        durable_risk_verdict(RiskVerdict::Allow { qty: requested }, requested, true,),
        RiskVerdict::Allow { qty: requested }
    );
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
        &settings(),
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
        &settings(),
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
    assert_eq!(
        engine.in_flight_ids().len(),
        1,
        "and we do not know its fate"
    );
    assert!(
        risk_saw.borrow().is_empty(),
        "no reply, so nothing to tell risk"
    );
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
        &settings(),
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
            wall_ts_ms: recent_replay_ms(),
        },
        WalRecord::OrderSent {
            request: finished.clone(),
            wire_ns: 1,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: finished.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.01,
                px: 30_000.0,
                fee: 0.1,
                is_maker: false,
                venue_ts_ms: recent_replay_ms() + 1,
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
    let (mut engine, h) = build_with_venue_orders(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &replayed,
        vec![still_working("eng-1700000000000-4", "BTCUSDT", 0.01)],
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
            exec_id: String::new(),
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
    assert!(
        eth_heard.borrow().is_empty(),
        "the other strategy hears nothing"
    );
}

#[tokio::test]
async fn the_group_flush_tick_pushes_the_log_out() {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 100, 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut small_flush = settings();
    small_flush.group_flush_ms = 5;
    let mut engine = Engine::boot(
        &small_flush,
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
    let mut quick = settings();
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
    let (_engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
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
        fills: false,
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
