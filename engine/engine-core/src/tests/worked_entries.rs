//! Working a resting entry: rest, walk, and when to send as written.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

#[tokio::test]
async fn an_entry_asked_to_be_worked_rests_at_the_touch_instead_of_crossing() {
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, WorkPolicy::default());
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    let sends = h.sends.borrow();
    assert_eq!(sends.len(), 1);
    assert_eq!(
        sends[0].kind,
        OrderKind::Limit {
            px: 30_000.0,
            tif: engine_types::TimeInForce::Gtc
        },
        "a limit at the bid, not a market order across the spread"
    );
}

#[tokio::test]
async fn a_spread_too_thin_to_pay_for_still_sends_the_market_order() {
    // The one-tick book. Below two ticks the taker cost is already near the
    // maker floor, so the order goes out exactly as it did before any of this
    // existed.
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, WorkPolicy::default());
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

    assert_eq!(h.sends.borrow()[0].kind, OrderKind::Market);
}

#[tokio::test]
async fn an_exit_is_sent_as_written_even_when_it_asks_to_be_worked() {
    // A resting exit that does not fill is exposure nobody wanted, still on
    // the book.
    struct Exiter {
        sent: bool,
    }
    impl Strategy for Exiter {
        fn name(&self) -> &str {
            "exiter"
        }
        fn subscriptions(&self) -> Vec<Subscription> {
            vec![Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }]
        }
        fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
            let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event else {
                return;
            };
            if self.sent {
                return;
            }
            self.sent = true;
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
                work: Some(WorkPolicy::default()),
                leverage: None,
            });
        }
    }

    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(Exiter { sent: false })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(
        h.sends.borrow()[0].kind,
        OrderKind::Market,
        "the spread was wide enough to rest in; it is an exit that stops it"
    );
}

#[tokio::test]
async fn the_group_flush_tick_walks_a_resting_entry_after_the_market() {
    // The supervisor has no clock of its own: it rides the tick the loop
    // already had.
    let quick = WorkPolicy {
        reprice_ms: 1,
        ..WorkPolicy::default()
    };
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, quick);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let amends = venue.amends.clone();
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut fast = settings();
    fast.group_flush_ms = 5;
    let mut engine = Engine::boot(&fast, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    // Two quotes: the first places the entry at 30_000, the second moves the
    // bid up to 30_001 and overtakes it.
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 2, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    let id = sends.borrow()[0].client_order_id.clone();
    let amends = amends.borrow();
    assert!(!amends.is_empty(), "the overtaken order was not moved");
    assert_eq!(amends[0].1, id);
    assert_eq!(
        amends[0].2,
        AmendSpec {
            px: Some(30_001.0),
            qty: None
        },
        "back to the touch that overtook it, price only"
    );
}

#[tokio::test]
async fn repricing_a_resting_entry_reserves_its_price_range_before_the_wire() {
    // A price-only amend can still increase notional or stop distance. The
    // engine therefore reserves the conservative old/new price range and
    // makes that state durable before asking the venue to mutate the order.
    let quick = WorkPolicy {
        reprice_ms: 1,
        ..WorkPolicy::default()
    };
    let (buyer, _heard) = Buyer::working("BTCUSDT", 1, 0.01, quick);
    let tape = tape();
    let (wal, _records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let amends = venue.amends.clone();
    let (risk, _seen) = MockRisk::with(allow_all());
    let mut fast = settings();
    fast.group_flush_ms = 5;
    let mut engine = Engine::boot(&fast, "0", wal, risk, venue, vec![Box::new(buyer)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::wide_quotes(symbol, 2, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();
    assert!(
        !amends.borrow().is_empty(),
        "there was a reprice to measure"
    );

    // The changed reservation and AmendSent record must reach the durability
    // barrier before the venue sees the amend.
    let steps = tape.borrow();
    let mut checked = 0;
    for (i, step) in steps.iter().enumerate() {
        if *step != Step::Append("amend_sent".into()) {
            continue;
        }
        let wire = i + steps[i..]
            .iter()
            .position(|s| matches!(s, Step::Amend(_)))
            .expect("the reprice reaches the venue");
        let barrier = i + steps[i..wire]
            .iter()
            .position(|s| matches!(s, Step::Barrier))
            .expect("the amend's conservative reservation is durable");
        assert!(
            i < barrier && barrier < wire,
            "the barrier precedes the wire"
        );
        checked += 1;
    }
    assert!(checked > 0, "no reprice was written down");
}

#[tokio::test]
async fn a_fresh_account_trip_is_durable_before_resting_entries_are_cancelled() {
    /// Trips on the first fresh account reading after an order has been
    /// reserved. This makes the test independent of whether Tokio services
    /// its immediately-ready interval tick before the scripted quote.
    struct TripAfterEntry {
        opening_seen: bool,
        entry_reserved: bool,
        halted: bool,
        changed_anchor: Option<String>,
    }

    impl RiskKernel for TripAfterEntry {
        fn assess(&mut self, intent: &Intent, _account: &AccountView) -> RiskVerdict {
            if self.halted {
                RiskVerdict::Deny {
                    reason: DenyReason::LossGuardTripped {
                        equity_usdt: 9_000.0,
                        floor_usdt: 9_000.0,
                    },
                }
            } else {
                RiskVerdict::Allow { qty: intent.qty }
            }
        }

        fn on_update(&mut self, _update: &OrderUpdate) {}

        fn observe_account_view(&mut self, _account: &AccountView) {
            if !self.opening_seen {
                self.opening_seen = true;
                self.changed_anchor = Some("opening".into());
            } else if self.entry_reserved && !self.halted {
                self.halted = true;
                self.changed_anchor = Some("tripped".into());
            }
        }

        fn register_order(&mut self, _id: &str, _intent: &Intent, _approved_qty: f64) {
            self.entry_reserved = true;
        }

        fn entries_halted(&self) -> bool {
            self.halted
        }

        fn take_control_anchor(&mut self) -> Option<String> {
            self.changed_anchor.take()
        }
    }

    struct RestingBurst {
        fired: bool,
        count: usize,
        work: WorkPolicy,
    }

    impl Strategy for RestingBurst {
        fn name(&self) -> &str {
            "resting-burst"
        }

        fn subscriptions(&self) -> Vec<Subscription> {
            vec![Subscription {
                symbol: "BTCUSDT".into(),
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
            for index in 0..self.count {
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
                    tag: format!("loss-guard-resting-{index}"),
                    decided_ns: ctx.now_ns(),
                    work: Some(self.work),
                    leverage: None,
                });
            }
        }
    }

    let quick = WorkPolicy {
        reprice_ms: 1,
        ..WorkPolicy::default()
    };
    let burst = RestingBurst {
        fired: false,
        // Spans several native groups. Exactly how many placement groups win
        // the race with the fresh account reading is deliberately unspecified;
        // once that reading trips the guard, the durable barrier must divide
        // the sent prefix from cancellation and suppress the unsent suffix.
        count: 61,
        work: quick,
    };
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let risk = TripAfterEntry {
        opening_seen: false,
        entry_reserved: false,
        halted: false,
        changed_anchor: None,
    };
    let mut fast = settings();
    fast.group_flush_ms = 5;
    fast.account_view_max_age_ms = 1;
    let mut engine = Engine::boot(&fast, "0", wal, risk, venue, vec![Box::new(burst)], &[])
        .await
        .unwrap();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let error = tokio::time::timeout(
        Duration::from_secs(2),
        engine.run(
            &mut ScriptFeed::wide_quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        ),
    )
    .await
    .expect("the private-confirmation deadline remained bounded")
    .expect_err("an accepted cancel without private confirmation must restart");
    assert!(
        error
            .to_string()
            .contains("not confirmed by the private stream"),
        "{error}"
    );

    let ids: Vec<_> = sends
        .borrow()
        .iter()
        .map(|request| request.client_order_id.clone())
        .collect();
    assert!(
        !ids.is_empty() && ids.len() <= 61,
        "the venue must see a nonempty bounded prefix, saw {}",
        ids.len()
    );
    let mut sent_ids = ids.clone();
    sent_ids.sort();
    let sent_count = sent_ids.len();
    sent_ids.dedup();
    assert_eq!(sent_ids.len(), sent_count, "placement ids must be unique");

    let mut cancelled: Vec<_> = cancels
        .borrow()
        .iter()
        .map(|(_, client_order_id)| client_order_id.clone())
        .collect();
    cancelled.sort();
    assert_eq!(
        cancelled, sent_ids,
        "every sent resting id must be cancelled exactly once, and no unsent id may be cancelled"
    );

    let mut observable: Vec<_> = records
        .borrow()
        .iter()
        .filter_map(|record| match record {
            WalRecord::CancelSent {
                client_order_id, ..
            } => Some(client_order_id.clone()),
            _ => None,
        })
        .collect();
    observable.sort();
    assert_eq!(
        observable, sent_ids,
        "every wire cancellation must have exactly one WAL-observable record"
    );
    assert!(amends.borrow().is_empty(), "a halted entry was amended");

    let steps = tape.borrow();
    let id = ids.first().expect("at least one sent entry");
    let sent = steps
        .iter()
        .position(|step| step == &Step::Send(id.clone()))
        .expect("entry send on tape");
    let anchor = steps
        .iter()
        .enumerate()
        .skip(sent + 1)
        .find_map(|(index, step)| (step == &Step::Append("control_anchor".into())).then_some(index))
        .expect("trip anchor after the entry");
    let barrier = steps
        .iter()
        .enumerate()
        .skip(anchor + 1)
        .find_map(|(index, step)| (step == &Step::Barrier).then_some(index))
        .expect("trip durability barrier");
    let first_cancel = steps
        .iter()
        .position(|step| matches!(step, Step::Cancel(_)))
        .expect("entry cancellation on tape");
    assert!(
        anchor < barrier && barrier < first_cancel,
        "the trip anchor and its barrier must precede the first cancel"
    );
    assert!(
        !steps[barrier + 1..]
            .iter()
            .any(|step| matches!(step, Step::Send(_))),
        "an opening placement reached the venue after the trip became durable"
    );
}
