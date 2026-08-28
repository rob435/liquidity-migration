//! Pulling and moving what rests, and reading your own book.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

/// Pulls one named order on its first quote, and nothing after.
struct Canceller {
    symbol: String,
    order: String,
    fired: bool,
}

impl Canceller {
    fn new(symbol: &str, order: &str) -> Self {
        Canceller {
            symbol: symbol.into(),
            order: order.into(),
            fired: false,
        }
    }
}

impl Strategy for Canceller {
    fn name(&self) -> &str {
        "canceller"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            if !self.fired {
                self.fired = true;
                ctx.cancel(*symbol, &self.order);
            }
        }
    }
}

/// Moves one named order's price on its first quote.
struct Amender {
    symbol: String,
    order: String,
    fired: bool,
}

impl Strategy for Amender {
    fn name(&self) -> &str {
        "amender"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event {
            if !self.fired {
                self.fired = true;
                ctx.amend(
                    *symbol,
                    &self.order,
                    AmendSpec {
                        px: Some(quote.bid_px),
                        qty: None,
                    },
                );
            }
        }
    }
}

#[tokio::test]
async fn a_cancel_is_written_down_and_reaches_the_venue_without_an_fsync() {
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(Canceller::new("BTCUSDT", "eng-old-1"))],
        &["BTCUSDT"],
        &[],
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
        *h.cancels.borrow(),
        vec![(symbol, "eng-old-1".to_string())],
        "the cancel reached the gateway"
    );
    let record = h
        .records
        .borrow()
        .iter()
        .find_map(|r| match r {
            WalRecord::CancelSent {
                symbol,
                client_order_id,
                wire_ns,
            } => Some((*symbol, client_order_id.clone(), *wire_ns)),
            _ => None,
        })
        .expect("a cancel_sent record");
    assert_eq!(record.0, symbol);
    assert_eq!(record.1, "eng-old-1");
    assert!(record.2 > 0, "the record stamps when it went out");

    let written = at(&h.tape, &Step::Append("cancel_sent".into())).unwrap();
    let sent = at(&h.tape, &Step::Cancel("eng-old-1".into())).unwrap();
    assert!(written < sent, "written down before it went out");
    // A cancel adds no exposure, so it does not pay for an fsync: the only
    // barrier on the tape is the shutdown one.
    assert!(
        only_the_shutdown_barrier(&h.tape),
        "a cancel paid for a barrier"
    );
}

/// Fires a burst of entries with cancels behind them, all in one wake.
struct MixedBurst {
    symbol: String,
    entries: usize,
    cancels: usize,
    fired: bool,
}

impl Strategy for MixedBurst {
    fn name(&self) -> &str {
        "mixed-burst"
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
            for _ in 0..self.entries {
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
                    tag: "flood-entry".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
            for i in 0..self.cancels {
                ctx.cancel(*symbol, &format!("eng-old-{i}"));
            }
        }
    }
}

#[tokio::test]
async fn a_flooded_wake_drops_entries_but_never_cancels() {
    // Same reason exits are spared: a strategy that cannot pull its resting
    // orders is stranded with a book it believes it has already cleared.
    let burst = MixedBurst {
        symbol: "BTCUSDT".into(),
        entries: 70,
        cancels: 3,
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

    assert_eq!(
        h.cancels.borrow().len(),
        3,
        "every cancel reached the venue, queued behind the flood"
    );
    assert_eq!(
        h.sends.borrow().len(),
        64,
        "the flood of entries stopped at the cap"
    );
    let note = note_saying(&h.records, "dropped");
    assert!(note.contains("entries"), "the drop is named: {note}");
}

#[tokio::test]
async fn an_amend_is_refused_where_the_venue_cannot_move_a_resting_order() {
    // Cancel-and-replace is a different trade — a new order at the back of
    // the queue — so the engine says no rather than substituting one.
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.caps.amend_in_place = false;
    let amends = venue.amends.clone();
    let cancels = venue.cancels.clone();
    let (risk, _seen) = MockRisk::with(allow_all());
    let amender = Amender {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        fired: false,
    };
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(amender)],
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

    assert!(amends.borrow().is_empty(), "nothing reached the venue");
    assert!(
        cancels.borrow().is_empty(),
        "and it was not quietly turned into a cancel"
    );
    assert!(
        !appends(&tape).contains(&"amend_sent".to_string()),
        "nothing was written down as sent"
    );
    let note = note_saying(&records, "not amended");
    assert!(note.contains("eng-old-1"), "{note}");
    assert!(note.contains("cancel-and-replace"), "{note}");
}

#[tokio::test]
async fn an_async_amend_ack_keeps_its_range_and_queues_cancellation() {
    let amender = Amender {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        fired: false,
    };
    let old = OrderRequest {
        client_order_id: "eng-old-1".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Limit {
            px: 29_000.0,
            tif: engine_types::TimeInForce::Gtc,
        },
        stop: Some(StopSpec {
            trigger_px: 28_000.0,
        }),
        reduce_only: false,
    };
    let replayed = [WalRecord::OrderSent {
        request: old,
        wire_ns: 1,
        arrival_mid: 29_500.0,
    }];
    let (mut engine, h) = build_with_venue_orders(
        allow_all(),
        vec![Box::new(amender)],
        &["BTCUSDT"],
        &replayed,
        vec![still_working("eng-old-1", "BTCUSDT", 0.01)],
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

    let amends = h.amends.borrow();
    assert_eq!(amends.len(), 1);
    assert_eq!(amends[0].0, symbol);
    assert_eq!(amends[0].1, "eng-old-1");
    assert_eq!(amends[0].2.px, Some(30_000.0), "the new price, unchanged");
    assert_eq!(amends[0].2.qty, None, "and no size change was asked for");
    assert_eq!(h.cancels.borrow().len(), 1, "ambiguous order is pulled");
    assert!(!h
        .records
        .borrow()
        .iter()
        .any(|record| matches!(record, WalRecord::AmendResolved { .. })));
    let written = at(&h.tape, &Step::Append("amend_sent".into())).unwrap();
    let sent = at(&h.tape, &Step::Amend("eng-old-1".into())).unwrap();
    assert!(written < sent, "written down before it went out");
    // Repricing can multiply notional and stop distance, so its conservative
    // replacement reservation is durable before the venue call.
    assert!(
        h.tape
            .borrow()
            .iter()
            .any(|step| matches!(step, Step::Barrier)),
        "an opening reprice did not pay for its risk barrier"
    );
    assert!(
        h.risk_clocks.borrow().len() >= 2,
        "opening-amend risk time was not advanced after boot"
    );
}

#[tokio::test]
async fn a_nonfinite_amend_approval_never_reaches_the_venue() {
    let amender = Amender {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        fired: false,
    };
    let old = OrderRequest {
        client_order_id: "eng-old-1".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.01,
        kind: OrderKind::Limit {
            px: 29_000.0,
            tif: engine_types::TimeInForce::Gtc,
        },
        stop: Some(StopSpec {
            trigger_px: 28_000.0,
        }),
        reduce_only: false,
    };
    let replayed = [WalRecord::OrderSent {
        request: old,
        wire_ns: 1,
        arrival_mid: 29_500.0,
    }];
    let (mut engine, h) = build_with_amend_verdict(
        RiskVerdict::Allow { qty: f64::NAN },
        vec![Box::new(amender)],
        &["BTCUSDT"],
        &replayed,
        vec![still_working("eng-old-1", "BTCUSDT", 0.01)],
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

    assert!(h.amends.borrow().is_empty());
    assert!(!appends(&h.tape).contains(&"amend_sent".to_string()));
    assert!(note_saying(&h.records, "not amended").contains("invalid quantity verdict"));
    for record in h.records.borrow().iter() {
        let bytes = serde_json::to_vec(record).expect("serializable");
        let _: WalRecord =
            serde_json::from_slice(&bytes).expect("sanitized amend records must replay");
    }
}

/// Raises one named order's size on its first quote.
struct Grower {
    symbol: String,
    order: String,
    qty: f64,
    fired: bool,
}

impl Strategy for Grower {
    fn name(&self) -> &str {
        "grower"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            if !self.fired {
                self.fired = true;
                ctx.amend(
                    *symbol,
                    &self.order,
                    AmendSpec {
                        px: None,
                        qty: Some(self.qty),
                    },
                );
            }
        }
    }
}

#[tokio::test]
async fn a_quantity_amend_is_refused_until_risk_and_ledger_can_resize_together() {
    // Growing a resting order adds exposure, so it earns the same fsync a
    // send does: a crash in between must never leave the log describing a
    // smaller order than the one the venue is now working.
    let grower = Grower {
        symbol: "BTCUSDT".into(),
        order: "eng-old-1".into(),
        qty: 99.0,
        fired: false,
    };
    let (mut engine, h) = build(allow_all(), vec![Box::new(grower)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(h.amends.borrow().is_empty());
    assert!(h.records.borrow().iter().any(|record| matches!(record,
        WalRecord::Note { text, .. } if text.contains("quantity changes are unsupported"))));
}

#[tokio::test]
async fn an_entry_carrying_a_stop_is_refused_where_the_venue_keeps_none() {
    // The kernel demands a stop on an entry. A venue that cannot hold one
    // would leave that rule unenforced while the log still showed a stop, so
    // the order does not go at all.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.caps.native_position_stop = false;
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

    assert!(sends.borrow().is_empty(), "nothing reached the venue");
    assert!(
        !appends(&tape).contains(&"order_sent".to_string()),
        "and nothing was written down as sent"
    );
    let note = note_saying(&records, "not sent");
    assert!(note.contains("keeps none"), "{note}");
    assert!(engine.in_flight_ids().is_empty());
}

/// Writes down its own working orders every time the engine wakes it, and
/// optionally places one entry first so the book has something it owns.
struct Watcher {
    symbol: String,
    place: Option<f64>,
    placed: bool,
    seen: Rc<RefCell<Vec<Vec<String>>>>,
}

impl Watcher {
    fn new(symbol: &str) -> (Self, Rc<RefCell<Vec<Vec<String>>>>) {
        let seen = Rc::new(RefCell::new(Vec::new()));
        (
            Watcher {
                symbol: symbol.to_string(),
                place: None,
                placed: false,
                seen: seen.clone(),
            },
            seen,
        )
    }

    fn buying(symbol: &str, qty: f64) -> (Self, Rc<RefCell<Vec<Vec<String>>>>) {
        let (mut me, seen) = Watcher::new(symbol);
        me.place = Some(qty);
        (me, seen)
    }
}

impl Strategy for Watcher {
    fn name(&self) -> &str {
        "watcher"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let mut mine = Vec::new();
        ctx.resting(&mut mine);
        self.seen
            .borrow_mut()
            .push(mine.iter().map(|o| o.client_order_id.to_string()).collect());

        if let (EngineEvent::Market(MarketEvent::Quote { symbol, quote }), Some(qty), false) =
            (event, self.place, self.placed)
        {
            self.placed = true;
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: Side::Buy,
                qty,
                kind: OrderKind::Market,
                stop: Some(StopSpec {
                    trigger_px: quote.bid_px * 0.99,
                }),
                reduce_only: false,
                tag: "watch".into(),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
    }
}

#[tokio::test]
async fn each_strategy_reads_only_its_own_working_orders() {
    // Two strategies, one book. A strategy that could see another's orders
    // could cancel them.
    let mine = OrderRequest {
        client_order_id: "eng-1700000000000-1".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.25,
        kind: OrderKind::Limit {
            px: 29_000.0,
            tif: engine_types::TimeInForce::Gtc,
        },
        stop: None,
        reduce_only: false,
    };
    let theirs = OrderRequest {
        client_order_id: "eng-1700000000000-2".into(),
        strategy: StrategyId(1),
        ..mine.clone()
    };
    let finished = OrderRequest {
        client_order_id: "eng-1700000000000-3".into(),
        ..mine.clone()
    };
    let replayed = vec![
        WalRecord::OrderSent {
            request: mine.clone(),
            wire_ns: 1,
            arrival_mid: 0.0,
        },
        WalRecord::OrderSent {
            request: theirs.clone(),
            wire_ns: 2,
            arrival_mid: 0.0,
        },
        WalRecord::OrderSent {
            request: finished.clone(),
            wire_ns: 3,
            arrival_mid: 0.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: finished.client_order_id.clone(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.25,
                px: 29_000.0,
                fee: 0.0,
                is_maker: false,
                venue_ts_ms: 4,
                recv_ns: 4,
            },
        },
    ];

    let (one, seen_one) = Watcher::new("BTCUSDT");
    let (two, seen_two) = Watcher::new("BTCUSDT");
    let (mut engine, _h) = build_with_venue_orders(
        allow_all(),
        vec![Box::new(one), Box::new(two)],
        &["BTCUSDT"],
        &replayed,
        // The venue confirms both are still working; unconfirmed recovered
        // orders are reaped at boot rather than shown to anybody.
        vec![
            still_working("eng-1700000000000-1", "BTCUSDT", 0.25),
            still_working("eng-1700000000000-2", "BTCUSDT", 0.25),
        ],
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
        *seen_one.borrow(),
        vec![vec!["eng-1700000000000-1".to_string()]],
        "its own working order, and nothing else the log knows about"
    );
    assert_eq!(
        *seen_two.borrow(),
        vec![vec!["eng-1700000000000-2".to_string()]],
        "the other strategy's book is its own"
    );
}

#[tokio::test]
async fn an_order_this_strategy_placed_is_in_its_book_until_it_ends() {
    let (watcher, seen) = Watcher::buying("BTCUSDT", 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(watcher)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let id = h.sends.borrow()[0].client_order_id.clone();
    let seen = seen.borrow();
    assert_eq!(seen[0], Vec::<String>::new(), "nothing placed yet");
    // An ack is not an ending, so the order is still the strategy's to pull.
    assert_eq!(seen.last().unwrap(), &vec![id], "acked and still working");
}

#[tokio::test]
async fn an_order_the_log_has_ended_leaves_the_strategys_book() {
    // Ownership outlives the order — the engine still routes its news here —
    // so only the ending keeps a dead order out of the strategy's book. A
    // strategy that saw it would try to cancel or move something gone.
    let (watcher, seen) = Watcher::buying("BTCUSDT", 0.01);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue.reply = Some(VenueError::Rejected {
        code: 110007,
        message: "not enough balance".into(),
    });
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(watcher)],
        &[],
    )
    .await
    .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(sends.borrow().len(), 1, "one order really was placed");
    assert!(
        engine.in_flight_ids().is_empty(),
        "and the venue refused it"
    );
    for (n, snapshot) in seen.borrow().iter().enumerate() {
        assert!(
            snapshot.is_empty(),
            "a rejected order was still in the book at wake {n}: {snapshot:?}"
        );
    }
    assert!(seen.borrow().len() >= 3, "it was woken after the rejection");
}
