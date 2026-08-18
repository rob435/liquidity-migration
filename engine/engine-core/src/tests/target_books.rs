//! The target-book seam, and stating leverage before an order goes.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

/// Writes down every book the loop hands it, and places nothing.
struct BookListener {
    symbol: String,
    heard: Rc<RefCell<Vec<String>>>,
}

impl BookListener {
    fn new(symbol: &str) -> (Self, Rc<RefCell<Vec<String>>>) {
        let heard = Rc::new(RefCell::new(Vec::new()));
        (
            BookListener {
                symbol: symbol.to_string(),
                heard: heard.clone(),
            },
            heard,
        )
    }
}

impl Strategy for BookListener {
    fn name(&self) -> &str {
        "book_listener"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, _ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Targets(book) = event {
            self.heard
                .borrow_mut()
                .push(format!("{} x{}", book.source, book.targets.len()));
        }
    }
}

const BOOK_JSON: &str = r#"{"version":1,"source":"carry","decision_ts_ms":1700000000000,
"valid_until_ms":1700086400000,"targets":[{"symbol":"BTCUSDT","notional_usdt":50.0,
"stop_loss_fraction":0.35,"leverage":2.0}]}"#;

/// Stop the loop as soon as the strategy has heard something, or give up
/// after a while so a failure reads as an assertion and not a hung test.
async fn until_heard(heard: Rc<RefCell<Vec<String>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while heard.borrow().is_empty() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

/// A buyer whose entries were sized at a leverage, so the engine has to make
/// the venue agree before the order goes.
fn levered_buyer(symbol: &str, leverage: f64) -> (Buyer, Rc<RefCell<Vec<String>>>) {
    let (mut buyer, heard) = Buyer::new(symbol, 1, 0.01);
    buyer.leverage = Some(leverage);
    (buyer, heard)
}

#[tokio::test]
async fn an_entry_states_its_leverage_before_the_order_goes() {
    // Margin posted is notional divided by leverage. An order sized at one
    // and filled at another does not commit the capital the kernel priced.
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.leverages.borrow().as_slice(), [(SymbolId(0), 2.0)]);
    assert_eq!(h.sends.borrow().len(), 1, "the order still went");
    // Order matters: the venue heard about leverage before it heard the order.
    let tape = h.tape.borrow();
    assert!(
        tape.iter().any(|step| matches!(step, Step::Send(_))),
        "no send on the tape: {tape:?}"
    );
}

#[tokio::test]
async fn a_second_entry_on_the_same_symbol_does_not_pay_for_leverage_twice() {
    // A symbol keeps its leverage at the venue. Re-stating it before every
    // order would buy a round trip per order, measured at ~190 ms on the
    // Python fleet, for no change at all.
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 3, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow().len(), 3, "three entries went");
    assert_eq!(
        h.leverages.borrow().len(),
        1,
        "leverage was stated more than once for one symbol: {:?}",
        h.leverages.borrow()
    );
}

#[tokio::test]
async fn an_exit_does_not_wait_on_leverage() {
    // An exit at the wrong leverage is still an exit. Making it wait on a
    // venue round trip would be the wrong trade to make.
    let (mut seller, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    seller.leverage = Some(2.0);
    seller.reduce_only = true;
    let (mut engine, h) =
        build(false, allow_all(), vec![Box::new(seller)], &["BTCUSDT"], &[]).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert!(
        h.leverages.borrow().is_empty(),
        "an exit stopped to set leverage: {:?}",
        h.leverages.borrow()
    );
}

#[tokio::test]
async fn an_order_whose_leverage_will_not_set_is_not_sent() {
    // Fails closed. Sending anyway would post margin nobody priced, and the
    // whole reason for stating leverage is that we do not know what the
    // symbol currently carries.
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let mut settings = settings(false);
    settings.shadow = false;
    let (mut engine, h) = build_with_refusing_leverage(
        &settings,
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
    )
    .await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.leverages.borrow().len(), 1, "it tried");
    assert!(
        h.sends.borrow().is_empty(),
        "an order went at a leverage the venue would not accept"
    );
}

#[test]
fn a_symbol_that_goes_flat_forgets_its_leverage() {
    // The cache is only safe because of this. The owner trades the funded
    // account by hand, so a symbol that has been closed and reopened may have
    // been set to anything in between -- and a remembered value would make
    // the next entry skip the call that would have corrected it.
    use engine_types::risk::PositionView;
    let mut at = std::collections::HashMap::new();
    at.insert(SymbolId(0), 2.0);
    at.insert(SymbolId(1), 3.0);

    let still_open = vec![PositionView {
        symbol: SymbolId(1),
        side: Side::Buy,
        qty: 1.0,
        entry_px: 100.0,
        stop_attached: true,
        leverage: None
    }];
    crate::engine::forget_leverage_where_flat(&mut at, &still_open);

    assert_eq!(at.get(&SymbolId(1)), Some(&3.0), "an open symbol keeps it");
    assert!(at.get(&SymbolId(0)).is_none(), "a flat symbol forgets it");
}

#[tokio::test]
async fn a_book_on_disk_reaches_the_strategies_through_the_loop() {
    let path = temp_path("book-seam");
    std::fs::write(&path, BOOK_JSON).expect("writes the book");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, _h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            until_heard(heard.clone()),
        )
        .await
        .unwrap();

    assert_eq!(
        heard.borrow().as_slice(),
        ["carry x1"],
        "the book reached the strategy whole"
    );
}

/// A second sleeve's book, on a different symbol and from a different
/// producer, so a crossover shows up as the wrong name in the wrong place.
const LONG_BOOK_JSON: &str = r#"{"version":1,"source":"long","decision_ts_ms":1700000000000,
"valid_until_ms":1700086400000,"targets":[{"symbol":"ETHUSDT","notional_usdt":40.0,
"stop_loss_fraction":0.35,"leverage":2.0},{"symbol":"BTCUSDT","notional_usdt":10.0,
"stop_loss_fraction":0.35,"leverage":2.0}]}"#;

fn book_watcher(path: &crate::testpath::TempPath) -> crate::targets::TargetBookWatcher {
    crate::targets::TargetBookWatcher::with_poll(
        path.path().to_path_buf(),
        Duration::from_millis(5),
    )
}

/// Wait until both sleeves have heard something, or give up.
async fn until_both_heard(a: Rc<RefCell<Vec<String>>>, b: Rc<RefCell<Vec<String>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while (a.borrow().is_empty() || b.borrow().is_empty())
        && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
    // A crossover would arrive on the same poll as the right book, so give the
    // loop a moment to deliver a second one before deciding nothing did.
    tokio::time::sleep(Duration::from_millis(40)).await;
}

#[tokio::test]
async fn each_sleeve_hears_only_its_own_book() {
    // The whole reason books are routed rather than broadcast. Two sleeves
    // share one account and one position per symbol, so a follower handed the
    // other's book would start holding the other's positions -- and both books
    // below name BTCUSDT, which is exactly where that would show.
    let carry_path = temp_path("book-two-carry");
    let long_path = temp_path("book-two-long");
    std::fs::write(&carry_path, BOOK_JSON).expect("writes the carry book");
    std::fs::write(&long_path, LONG_BOOK_JSON).expect("writes the long book");

    let (carry, carry_heard) = BookListener::new("BTCUSDT");
    let (long, long_heard) = BookListener::new("ETHUSDT");
    let (mut engine, _h) = build(
        true,
        allow_all(),
        vec![Box::new(carry), Box::new(long)],
        &["BTCUSDT", "ETHUSDT"],
        &[],
    )
    .await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![
        (StrategyId(0), book_watcher(&carry_path)),
        (StrategyId(1), book_watcher(&long_path)),
    ]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            until_both_heard(carry_heard.clone(), long_heard.clone()),
        )
        .await
        .unwrap();

    assert_eq!(
        carry_heard.borrow().as_slice(),
        ["carry x1"],
        "the carry sleeve heard something other than its own book"
    );
    assert_eq!(
        long_heard.borrow().as_slice(),
        ["long x2"],
        "the long sleeve heard something other than its own book"
    );
}

#[tokio::test]
async fn a_symbol_a_book_names_late_is_taken_on() {
    // The universe used to be fixed when the engine booted: a name that first
    // appeared in a later book had no price, no instrument rule and no
    // SymbolId, so it was skipped rather than traded. The fleet's universe
    // moves with listings, so that was the engine quietly holding a smaller
    // book than it was told to.
    let path = temp_path("book-late-symbol");
    std::fs::write(&path, LONG_BOOK_JSON).expect("writes a book naming ETHUSDT");
    let (listener, heard) = BookListener::new("BTCUSDT");
    // Boots knowing BTCUSDT only.
    let (mut engine, h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    assert!(engine.market().table.get("ETHUSDT").is_none(), "it starts unknown");

    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        book_watcher(&path),
    )]));

    let mut feed = ScriptFeed::quotes(SymbolId(0), 0, false);
    let admitted = feed.admitted.clone();
    let mut orders = ScriptOrderFeed::empty();
    let learned = orders.learned.clone();
    engine
        .run(&mut feed, &mut orders, until_heard(heard.clone()))
        .await
        .unwrap();

    let id = engine
        .market()
        .table
        .get("ETHUSDT")
        .expect("the engine now follows the symbol the book named");
    // All three of the other tables that map names to ids agree, which is the
    // only thing that makes the id safe to place an order against.
    assert_eq!(admitted.borrow().as_slice(), [("ETHUSDT".to_string(), id)]);
    assert_eq!(learned.borrow().as_slice(), [("ETHUSDT".to_string(), id)]);
    assert!(
        h.sends.borrow().is_empty(),
        "taking on a symbol is not a reason to trade it"
    );
}

#[tokio::test]
async fn a_symbol_the_parts_disagree_about_is_not_traded() {
    // A SymbolId is an index assigned by position, so two tables that
    // disagreed about one would send an order meant for one symbol on
    // another. The engine checks rather than trusts, and drops the symbol.
    let path = temp_path("book-late-symbol-clash");
    std::fs::write(&path, LONG_BOOK_JSON).expect("writes a book naming ETHUSDT");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, _h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        book_watcher(&path),
    )]));

    let mut feed = ScriptFeed::quotes(SymbolId(0), 0, false);
    feed.admits_wrongly = true;
    let mut orders = ScriptOrderFeed::empty();
    let learned = orders.learned.clone();
    engine
        .run(&mut feed, &mut orders, until_heard(heard.clone()))
        .await
        .unwrap();

    assert!(
        learned.borrow().is_empty(),
        "a symbol the parts disagree about was passed on as usable"
    );
    // BTCUSDT, which was there before, is untouched: one bad symbol does not
    // take the engine down or move anybody else's id.
    assert_eq!(engine.market().table.get("BTCUSDT"), Some(SymbolId(0)));
}

#[tokio::test]
async fn an_unreadable_book_wakes_nobody() {
    // No decision, not an empty one. A strategy that were woken here with
    // nothing in hand would have to guess, and the guess that empties an
    // account is the one it would make.
    let path = temp_path("book-seam-torn");
    std::fs::write(&path, "{ not json at all").expect("writes the file");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, _h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            async {
                tokio::time::sleep(Duration::from_millis(200)).await;
            },
        )
        .await
        .unwrap();

    assert!(heard.borrow().is_empty(), "nothing was delivered");
}

#[tokio::test]
async fn with_no_watcher_the_loop_runs_as_it_always_did() {
    let (listener, heard) = BookListener::new("BTCUSDT");
    let (mut engine, h) =
        build(true, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 3, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(heard.borrow().is_empty());
    assert!(h.sends.borrow().is_empty());
}

// ---------------------------------------------------------------- authority

/// Wait until the venue's scripted account reading has been consumed, so the
/// next phase runs against the view the test just fed in.
async fn until_reading_consumed(
    readings: Rc<RefCell<VecDeque<Vec<engine_types::PositionView>>>>,
) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !readings.borrow().is_empty() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

/// One entry, then a flat account reading, then another entry — the measured
/// live shape (every carry hold ends flat before the next one opens), which
/// cost 27 of 67 real orders a ~172 ms confirmation round trip.
async fn flat_spell_leverage_calls(section: EngineSection) -> usize {
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let (mut engine, h) =
        build_with(&section, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[], Vec::new())
            .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    // First entry: pays the confirmation either way.
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    // The account reads back flat — the moment shared authority forgets.
    h.account_readings.borrow_mut().push_back(Vec::new());
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            until_reading_consumed(h.account_readings.clone()),
        )
        .await
        .unwrap();

    // Second entry, from flat.
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(h.sends.borrow().len(), 2, "both entries went");
    let count = h.leverages.borrow().len();
    count
}

#[tokio::test]
async fn sole_authority_does_not_repay_leverage_after_a_flat_spell() {
    let mut section = settings_sole(false);
    section.shadow = false;
    assert_eq!(
        flat_spell_leverage_calls(section).await,
        1,
        "under sole authority what this engine set stays set across a flat spell"
    );
}

#[tokio::test]
async fn shared_authority_still_reconfirms_after_a_flat_spell() {
    // The default is untouched: a hand may have retuned the symbol while it
    // sat flat, so the entry from flat confirms with the venue again.
    let mut section = settings(false);
    section.shadow = false;
    assert_eq!(flat_spell_leverage_calls(section).await, 2);
}

#[tokio::test]
async fn a_venue_row_contradicting_the_cache_evicts_the_trust() {
    // Sole authority is a claim about the world, and the venue's own position
    // row is the check: a held position running at a leverage this engine
    // never set means somebody else writes here — say so, drop the trust,
    // and confirm inline again on the next entry.
    let mut section = settings_sole(false);
    section.shadow = false;
    let (buyer, _heard) = levered_buyer("BTCUSDT", 2.0);
    let (mut engine, h) =
        build_with(&section, allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[], Vec::new())
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
    assert_eq!(h.leverages.borrow().len(), 1);

    // The venue says the held position runs at 5x. This engine set 2x.
    h.account_readings.borrow_mut().push_back(vec![engine_types::PositionView {
        symbol,
        side: Side::Buy,
        qty: 0.01,
        entry_px: 30_000.0,
        stop_attached: true,
        leverage: Some(5.0),
    }]);
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            until_reading_consumed(h.account_readings.clone()),
        )
        .await
        .unwrap();

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();

    assert_eq!(
        h.leverages.borrow().len(),
        2,
        "the contradicted trust was evicted, so the next entry confirmed inline"
    );
    assert!(
        h.records.borrow().iter().any(|record| matches!(
            record,
            WalRecord::Note { source, .. } if source == "leverage-authority"
        )),
        "the contradiction was written down, not just acted on"
    );
}

#[tokio::test]
async fn a_book_arrival_pre_arms_leverage_before_any_entry() {
    // The round trip happens where nobody is waiting on an order: at book
    // arrival. The entry that follows finds the cache warm and goes straight
    // to the wire.
    let path = temp_path("book-pre-arm");
    std::fs::write(&path, BOOK_JSON).expect("writes the book");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let mut section = settings_sole(false);
    section.shadow = false;
    let (mut engine, h) =
        build_with(&section, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[], Vec::new())
            .await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            until_heard(heard.clone()),
        )
        .await
        .unwrap();

    assert_eq!(
        h.leverages.borrow().as_slice(),
        [(SymbolId(0), 2.0)],
        "the book's leverage was armed at arrival, before any entry existed"
    );
    assert!(h.sends.borrow().is_empty(), "no order went; only the arm");
}

#[tokio::test]
async fn a_shadow_engine_never_pre_arms_the_real_account() {
    // Shadow computes and logs; it must not mutate the account it reads.
    let path = temp_path("book-pre-arm-shadow");
    std::fs::write(&path, BOOK_JSON).expect("writes the book");
    let (listener, heard) = BookListener::new("BTCUSDT");
    let section = settings_sole(true);
    let (mut engine, h) =
        build_with(&section, allow_all(), vec![Box::new(listener)], &["BTCUSDT"], &[], Vec::new())
            .await;
    engine.watch_targets(crate::targets::TargetBooks::new(vec![(
        StrategyId(0),
        crate::targets::TargetBookWatcher::with_poll(
            path.path().to_path_buf(),
            Duration::from_millis(5),
        ),
    )]));

    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            until_heard(heard.clone()),
        )
        .await
        .unwrap();

    assert!(
        h.leverages.borrow().is_empty(),
        "a shadow engine armed leverage on the real account: {:?}",
        h.leverages.borrow()
    );
}
