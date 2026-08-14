//! The follower, driven the way the engine drives it: a book arrives, prices
//! arrive, and what it sends is read off the bench.
//!
//! [`plan`] already owns the arithmetic and is tested cell by cell in
//! `plan.rs`. What is tested here is everything around it — that a book is
//! stored and acted on, that no book is *no decision*, that an entry carries
//! its stop, and that one decision does not become several orders.
//!
//! [`plan`]: super::super::plan::plan

use engine_types::{Action, BookTarget, Side};

use super::*;
use crate::mock_ctx::{Harness, RestingSeed};

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.001,
    qty_step: 0.1,
    min_qty: 0.1,
    min_notional: 5.0,
};

const NOW_MS: i64 = 1_700_000_000_000;
/// Comfortably past the entry cutoff, so entries are open.
const VALID_MS: i64 = NOW_MS + 24 * 3_600_000;

fn follower(symbols: &[&str]) -> Box<dyn Strategy> {
    let list = symbols
        .iter()
        .map(|s| format!("\"{s}\""))
        .collect::<Vec<_>>()
        .join(", ");
    let params: toml::Value =
        toml::from_str(&format!("symbols = [{list}]\n")).expect("the test config parses");
    crate::build_strategy(NAME, StrategyId(2), &params).expect("the block builds")
}

fn target(symbol: &str, notional_usdt: f64) -> BookTarget {
    BookTarget {
        symbol: symbol.to_string(),
        notional_usdt,
        stop_loss_fraction: 0.35,
        leverage: 2.0,
    }
}

fn book(targets: Vec<BookTarget>) -> TargetBook {
    TargetBook {
        source: "carry".to_string(),
        decision_ts_ms: NOW_MS - 1_000,
        valid_until_ms: VALID_MS,
        targets,
    }
}

/// A bench with the wall clock set, the instrument rules seeded, and one
/// price standing for each symbol. The book is half a unit wide either side,
/// so the mid is exactly `px` and the sizes in these tests are round numbers
/// rather than float dust.
fn bench(symbols: &[&str], px: f64) -> Harness {
    let mut h = Harness::new(follower(symbols));
    h.ctx.set_wall_ms(NOW_MS);
    for symbol in symbols {
        h.ctx.set_rule(symbol, RULE);
        h.quote(symbol, px - 0.5, px + 0.5);
    }
    h.drain();
    h
}

#[test]
fn a_book_that_arrives_is_entered_with_the_right_side_size_and_stop() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.symbol, h.ctx.id_of("KAITOUSDT"));
    assert_eq!(intent.side, Side::Buy);
    // 100 USDT at a mid of 10, quantized to the 0.1 step.
    assert_eq!(intent.qty, 10.0);
    assert_eq!(intent.kind, OrderKind::Market);
    assert!(!intent.reduce_only, "an entry is not reduce-only");
    let stop = intent.stop.expect("an entry always carries a stop");
    assert!(
        (stop.trigger_px - 6.5).abs() < 1e-9,
        "35% below the mid, got {}",
        stop.trigger_px
    );
    assert_eq!(intent.strategy, StrategyId(2));
}

#[test]
fn a_short_target_sells_with_its_stop_above() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", -100.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.side, Side::Sell);
    assert_eq!(intent.qty, 10.0);
    assert!(intent.stop.expect("a stop").trigger_px > 13.0);
}

#[test]
fn an_empty_book_exits_everything_held() {
    // A book that names nothing is a decision to hold nothing, and it is
    // acted on. This is the case that must never be confused with silence.
    let mut h = bench(&["KAITOUSDT", "COTIUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.ctx.set_position("COTIUSDT", Side::Sell, 4.0, 10.0);

    h.targets(book(vec![]));

    let mut sent = h.drain();
    assert_eq!(sent.len(), 2, "both holdings are closed, got {sent:?}");
    sent.sort_by_key(|i| i.symbol.0);
    for intent in &sent {
        assert!(intent.reduce_only, "an exit is reduce-only");
        assert!(intent.stop.is_none(), "an exit carries no stop");
        assert_eq!(intent.kind, OrderKind::Market);
    }
    let kaito = h.ctx.id_of("KAITOUSDT");
    let coti = h.ctx.id_of("COTIUSDT");
    let by_symbol = |id| sent.iter().find(|i| i.symbol == id).expect("a step for it");
    assert_eq!(by_symbol(kaito).side, Side::Sell, "closing a long sells");
    assert_eq!(by_symbol(kaito).qty, 10.0);
    assert_eq!(by_symbol(coti).side, Side::Buy, "closing a short buys");
    assert_eq!(by_symbol(coti).qty, 4.0);
}

#[test]
fn with_no_book_nothing_is_sent_and_a_position_is_left_alone() {
    // No book is *no decision*. A follower that flattened here would empty a
    // live account the moment research stopped writing.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);

    for _ in 0..5 {
        h.quote("KAITOUSDT", 9.5, 10.5);
    }
    assert!(h.drain_actions().is_empty(), "no book, nothing to do");
    assert!(
        h.ctx.position(h.ctx.id_of("KAITOUSDT")).is_some(),
        "the holding is untouched"
    );
}

#[test]
fn a_symbol_the_book_stops_naming_is_exited_while_the_rest_is_kept() {
    let mut h = bench(&["KAITOUSDT", "COTIUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.ctx.set_position("COTIUSDT", Side::Buy, 10.0, 10.0);

    // KAITO is still wanted at what it already is; COTI has dropped out.
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let sent = h.drain();
    assert_eq!(sent.len(), 1, "only the dropped name moves, got {sent:?}");
    assert_eq!(sent[0].symbol, h.ctx.id_of("COTIUSDT"));
    assert!(sent[0].reduce_only);
}

#[test]
fn a_second_identical_book_does_not_resend_an_order_that_is_already_resting() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert_eq!(h.drain().len(), 1, "the first book opens the position");

    // The engine minted an id for that order and it is still working: no
    // fill has come back, so the account reading still says flat.
    let symbol = h.ctx.id_of("KAITOUSDT");
    h.rest(RestingSeed {
        client_order_id: "eng-1".to_string(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Market,
        qty: 10.0,
        filled_qty: 0.0,
        reduce_only: false,
        acked: false,
    });

    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    h.quote("KAITOUSDT", 9.5, 10.5);
    assert!(
        h.drain_actions().is_empty(),
        "one decision, one order: the entry is already out there"
    );
}

#[test]
fn what_was_sent_is_remembered_until_the_reading_shows_it() {
    // The dangerous shape: the order has left the resting set (a filled one
    // is ended the moment the fill lands) and the account reading has not
    // caught up, so both places a plug can look say flat. Without a memory
    // of what it sent, it buys the same target a second time.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert_eq!(h.drain().len(), 1, "the entry goes out once");
    h.quote("KAITOUSDT", 9.5, 10.5);
    assert!(
        h.drain().is_empty(),
        "nothing resting and a stale reading must not become a second entry"
    );
    h.quote("KAITOUSDT", 9.4, 10.6);
    assert!(h.drain().is_empty(), "and it does not drift back on later quotes");
}

#[test]
fn a_holding_that_matches_the_book_is_left_alone() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert!(
        h.drain_actions().is_empty(),
        "100 wanted, 100 standing: nothing to do"
    );
}

#[test]
fn a_bigger_target_adds_and_carries_a_stop_anchored_on_the_entry() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    // Opened at 12, marked at 10: the stop must come off the 12.
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 12.0);
    h.targets(book(vec![target("KAITOUSDT", 150.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.side, Side::Buy);
    assert_eq!(intent.qty, 5.0, "the difference, not the whole target");
    assert!(!intent.reduce_only);
    // Adding is opening, and the risk kernel refuses an opening order with
    // no stop, so the whole position's stop is re-declared here.
    let stop = intent.stop.expect("an addition carries a stop or it is refused");
    assert!(
        (stop.trigger_px - 7.8).abs() < 1e-9,
        "35% below the 12.0 entry, got {}",
        stop.trigger_px
    );
}

#[test]
fn a_smaller_target_shrinks_reduce_only() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 20.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.side, Side::Sell);
    assert_eq!(intent.qty, 10.0);
    assert!(intent.reduce_only, "shrinking may only reduce");
    assert!(intent.stop.is_none());
}

#[test]
fn exits_go_out_before_entries() {
    // Rotating one name for another has to free the capital before it spends
    // it, or the entry is refused for room the exit was about to make.
    let mut h = bench(&["OLDUSDT", "NEWUSDT"], 10.0);
    h.ctx.set_position("OLDUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("NEWUSDT", 100.0)]));

    let sent = h.drain();
    assert_eq!(sent.len(), 2, "one out, one in, got {sent:?}");
    assert_eq!(sent[0].symbol, h.ctx.id_of("OLDUSDT"));
    assert!(sent[0].reduce_only, "the exit is first");
    assert_eq!(sent[1].symbol, h.ctx.id_of("NEWUSDT"));
    assert!(!sent[1].reduce_only);
}

#[test]
fn a_book_that_arrives_before_the_first_price_is_acted_on_at_the_first_quote() {
    let mut h = Harness::new(follower(&["KAITOUSDT"]));
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_rule("KAITOUSDT", RULE);

    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert!(
        h.drain_actions().is_empty(),
        "nothing can be sized without a price"
    );

    h.quote("KAITOUSDT", 9.5, 10.5);
    assert_eq!(h.one_intent().qty, 10.0);
}

#[test]
fn past_the_entry_window_entries_stop_and_exits_do_not() {
    let mut h = bench(&["OLDUSDT", "NEWUSDT"], 10.0);
    h.ctx.set_position("OLDUSDT", Side::Buy, 10.0, 10.0);
    // Inside the cutoff: the book is about to run out.
    h.ctx
        .set_wall_ms(VALID_MS - PlanRules::FLEET.entry_cutoff_ms + 1);

    h.targets(book(vec![target("NEWUSDT", 100.0)]));

    let sent = h.drain();
    assert_eq!(sent.len(), 1, "the entry is held back, got {sent:?}");
    assert_eq!(sent[0].symbol, h.ctx.id_of("OLDUSDT"));
    assert!(sent[0].reduce_only, "the exit still goes");
}

#[test]
fn a_symbol_outside_the_configured_universe_is_skipped_not_traded() {
    // The engine interns subscriptions once at boot, so a name that first
    // appears in a later book has no price, no rule and no id. Named as a
    // limit in the module doc, and pinned here.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("LATEUSDT", 100.0)]));
    assert!(h.drain_actions().is_empty());
}

#[test]
fn a_symbol_with_no_instrument_rule_is_not_guessed_at() {
    let mut h = Harness::new(follower(&["KAITOUSDT"]));
    h.ctx.set_wall_ms(NOW_MS);
    h.quote("KAITOUSDT", 9.5, 10.5);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert!(
        h.drain_actions().is_empty(),
        "nothing can be quantized without a rule"
    );
}

#[test]
fn the_newest_book_replaces_the_one_before_it() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert_eq!(h.drain().len(), 1);

    // The order filled and the account reading caught up.
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 0.0)]));

    let intent = h.one_intent();
    assert!(intent.reduce_only, "zero is an exit, not an absence");
    assert_eq!(intent.qty, 10.0);
}

#[test]
fn only_places_are_emitted_nothing_is_cancelled_or_amended() {
    // The follower works in market orders. If it ever grows a resting entry
    // this test is the thing that should be rewritten deliberately.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    for action in h.drain_actions() {
        assert!(
            matches!(action, Action::Place(_)),
            "expected only placements, got {action:?}"
        );
    }
}

#[test]
fn entries_cross_the_spread_unless_the_config_asks_them_to_rest() {
    // The default is what the follower always did. Turning resting on is a
    // decision somebody makes, not one they inherit with an upgrade.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", 60.0)]));
    let intent = h.one_intent();
    assert!(intent.work.is_none(), "an entry crosses unless asked otherwise");
}

/// The same bench, but with the follower told to rest its entries.
fn resting_bench(symbols: &[&str], px: f64) -> Harness {
    let list = symbols
        .iter()
        .map(|s| format!("\"{s}\""))
        .collect::<Vec<_>>()
        .join(", ");
    let config: toml::Value =
        toml::from_str(&format!("symbols = [{list}]\nrest_entries = true\n"))
            .expect("test config parses");
    let plug = TargetBookFollower::from_params(StrategyId(0), &config).expect("it builds");
    let mut h = Harness::new(Box::new(plug));
    h.ctx.set_wall_ms(NOW_MS);
    for symbol in symbols {
        h.ctx.set_rule(symbol, RULE);
        h.quote(symbol, px - 0.5, px + 0.5);
    }
    h.drain();
    h
}

#[test]
fn a_follower_told_to_rest_its_entries_asks_for_them_to_be_worked() {
    let mut h = resting_bench(&["KAITOUSDT"], 10.0);
    h.targets(book(vec![target("KAITOUSDT", 60.0)]));
    let intent = h.one_intent();
    assert!(intent.work.is_some(), "the entry should be worked in");
}

#[test]
fn a_full_exit_is_never_worked_however_the_follower_is_configured() {
    let mut h = resting_bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 6.0, 10.0);
    h.targets(book(vec![]));
    let intent = h.one_intent();
    assert!(intent.reduce_only);
    assert!(
        intent.work.is_none(),
        "a resting exit is exposure nobody wanted, still on the book"
    );
}

#[test]
fn trimming_a_position_is_not_worked_either() {
    // A shrink is an exit in all but name, and it comes down the resize path
    // rather than the exit one — so it needs its own test, or the rule is
    // only enforced on the half that happened to be looked at.
    let mut h = resting_bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 6.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 30.0)]));
    let intent = h.one_intent();
    assert!(intent.reduce_only, "60 USDT down to 30 is a trim");
    assert!(intent.work.is_none(), "a trim takes exposure off; it does not wait for a price");
}

#[test]
fn adding_to_a_position_is_worked_like_any_other_entry() {
    let mut h = resting_bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 3.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 60.0)]));
    let intent = h.one_intent();
    assert!(!intent.reduce_only, "30 USDT up to 60 adds exposure");
    assert!(intent.work.is_some(), "the half that adds is an entry");
}

#[test]
fn a_rest_entries_value_that_is_not_true_or_false_is_refused() {
    let bad: toml::Value = toml::from_str(
        "symbols = [\"KAITOUSDT\"]\nrest_entries = \"yes\"\n",
    )
    .expect("test config parses");
    assert!(TargetBookFollower::from_params(StrategyId(0), &bad).is_err());
}

// ---- A name that goes flat under us ----
//
// Before these, a stop that fired was undone by the very next quote: the book
// still said hold it, the position was gone, so the plug bought it straight
// back at full size with a fresh stop. Seconds, not minutes.

/// Hold what the book asks, then take the position away the way a venue stop
/// leaves it: flat, with nothing sent by us.
fn stopped_out(h: &mut Harness) {
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert!(h.drain().is_empty(), "already at target, so nothing is sent");
    h.ctx.set_position("KAITOUSDT", Side::Buy, 0.0, 10.0);
}

#[test]
fn a_stop_that_fired_is_not_undone_by_the_next_quote() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    stopped_out(&mut h);

    h.quote("KAITOUSDT", 9.5, 10.5);

    assert!(
        h.drain().is_empty(),
        "the position went flat without us; buying it back undoes the stop"
    );
}

#[test]
fn the_same_book_written_again_does_not_clear_the_latch() {
    // The case that matters for a producer on a one-minute clock: if a new
    // book lifted the latch, the loop would just run a minute slower.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    stopped_out(&mut h);

    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    assert!(h.drain().is_empty(), "a fresh copy of the same decision is not new news");
}

#[test]
fn the_producer_dropping_the_name_lifts_the_latch() {
    // The producer has taken the news into account -- it stopped asking. A
    // later book that asks again is a new decision, and is acted on.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    stopped_out(&mut h);

    h.targets(book(vec![]));
    assert!(h.drain().is_empty(), "nothing held, nothing to exit");

    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.side, Side::Buy);
    assert_eq!(intent.tag, "book-enter");
}

#[test]
fn a_latched_name_is_not_exited_either() {
    // Left alone means left alone. If the position comes back -- a late fill,
    // somebody re-opening by hand -- it is not ours to close.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    stopped_out(&mut h);
    h.quote("KAITOUSDT", 9.5, 10.5);
    h.drain();

    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.quote("KAITOUSDT", 9.5, 10.5);

    assert!(h.drain().is_empty(), "no exit for a name we were told to leave alone");
}

#[test]
fn an_entry_still_on_its_way_does_not_latch_its_own_symbol() {
    // Nothing was ever held, so nothing went flat. Without the was-held check
    // an entry would latch its own symbol the moment `sent_ahead` lapsed, and
    // the plug would open that name once and never again.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert_eq!(h.drain().len(), 1, "the entry goes out");

    // Sent but not yet in the reading: the plug counts it as held and does
    // not send it twice.
    h.quote("KAITOUSDT", 9.5, 10.5);
    assert!(h.drain().is_empty(), "one decision is one order");

    // The reading catches up, and the name is still perfectly tradable: a
    // bigger book resizes it up rather than finding it latched.
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 200.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.tag, "book-resize");
    assert!(!intent.reduce_only, "it grew");
}

#[test]
fn a_name_we_exited_ourselves_can_be_entered_again() {
    // The book said hold none, we closed it, it went flat. That is us, not
    // somebody else, so nothing latches and a later book re-enters.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 0.0)]));
    let exit = h.one_intent();
    assert!(exit.reduce_only, "that is an exit");

    h.ctx.set_position("KAITOUSDT", Side::Buy, 0.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.tag, "book-enter");
}

// ---- Two sleeves, one account ----
//
// The venue holds one position per symbol however many plugs run, and the
// account reading says nothing about whose it is. Before these, a carry
// follower and a long follower on one account would each read the other's
// exposure as their own.

#[test]
fn a_follower_does_not_exit_a_position_another_sleeve_opened() {
    // The bug, exactly: our book does not name BTCUSDT, the account holds
    // some, so the plug closed it -- a full-size reduce-only on a position it
    // never opened.
    let mut h = bench(&["KAITOUSDT", "BTCUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_foreign_position("BTCUSDT", Side::Buy, 1.0, 60_000.0);

    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let sent = h.drain();
    let btc = h.ctx.id_of("BTCUSDT");
    assert!(
        sent.iter().all(|intent| intent.symbol != btc),
        "the other sleeve's position is not ours to close, got {sent:?}"
    );
    assert_eq!(sent.len(), 1, "our own name is still entered");
}

#[test]
fn a_follower_does_not_enter_a_name_another_sleeve_is_holding() {
    // Both books want it. The first one there keeps it: one venue stop per
    // position means the second entry would silently replace the first's.
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_foreign_position("KAITOUSDT", Side::Buy, 5.0, 10.0);

    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    assert!(
        h.drain().is_empty(),
        "the other sleeve got there first; sizing on top would share one stop"
    );
}

#[test]
fn a_name_the_other_sleeve_lets_go_of_becomes_ours_again() {
    let mut h = bench(&["KAITOUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_foreign_position("KAITOUSDT", Side::Buy, 5.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    assert!(h.drain().is_empty(), "not ours yet");

    // They closed it. Nobody holds it now.
    h.ctx.set_position("KAITOUSDT", Side::Buy, 0.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));

    let intent = h.one_intent();
    assert_eq!(intent.tag, "book-enter");
}

#[test]
fn probe_a_zero_target_for_a_name_outside_the_config_list_still_exits() {
    // What the live fleet actually asked for: carry's config lists BTCUSDT,
    // its book named HOMEUSDT at zero, and the engine had taken HOMEUSDT on
    // from the book itself.
    let mut h = bench(&["BTCUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_position("HOMEUSDT", Side::Buy, 14110.0, 0.01);

    h.targets(book(vec![target("HOMEUSDT", 0.0)]));

    let sent = h.drain();
    assert_eq!(sent.len(), 1, "the zero target is an exit, got {sent:?}");
    assert!(sent[0].reduce_only);
}

#[test]
fn an_empty_book_closes_a_name_the_book_itself_introduced() {
    // The seed list is tiny and the book grows it, so the normal steady state
    // is positions in names the config never mentioned. Before this, an empty
    // book -- the one instruction whose whole meaning is "hold nothing" --
    // closed only the seed list and left every one of those standing.
    let mut h = bench(&["BTCUSDT"], 10.0);
    h.ctx.set_wall_ms(NOW_MS);
    h.ctx.set_position("KAITOUSDT", Side::Buy, 10.0, 10.0);
    h.targets(book(vec![target("KAITOUSDT", 100.0)]));
    h.drain();

    h.targets(book(vec![]));

    let intent = h.one_intent();
    assert_eq!(intent.symbol, h.ctx.id_of("KAITOUSDT"));
    assert!(intent.reduce_only, "hold nothing means close it");
}
