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
