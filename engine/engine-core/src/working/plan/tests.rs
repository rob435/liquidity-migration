//! The recipe, rule by rule. Every test here drives the pure decision
//! directly, so a change in behaviour shows up as a failing assertion and not
//! as a slightly different trade three layers away.

use super::*;
use engine_types::{OrderKind, StopSpec, StrategyId, SymbolId, TimeInForce};

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.5,
    qty_step: 0.001,
    min_qty: 0.001,
    min_notional: 5.0,
};

const SECOND: u64 = 1_000_000_000;

fn policy() -> WorkPolicy {
    WorkPolicy::default()
}

/// A two-sided book with four ticks of spread — plenty to rest in.
fn wide(bid_qty: f64, ask_qty: f64) -> Touch {
    Touch {
        bid_px: 99.0,
        bid_qty,
        ask_px: 101.0,
        ask_qty,
    }
}

/// Sizes the feed did not carry.
fn no_sizes() -> Touch {
    wide(0.0, 0.0)
}

fn buying(px: f64, now_ns: u64) -> WorkState {
    WorkState::new(Side::Buy, px, 0.0, now_ns)
}

fn intent(kind: OrderKind, reduce_only: bool, work: Option<WorkPolicy>) -> Intent {
    Intent {
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        kind,
        stop: Some(StopSpec { trigger_px: 90.0 }),
        reduce_only,
        tag: "t".into(),
        decided_ns: 1,
        work,
        leverage: None,
    }
}

// ------------------------------------------------------------- placement

#[test]
fn a_spread_too_thin_to_pay_for_is_not_rested() {
    // One tick of spread: the taker cost is already near the maker floor, and
    // the night replay showed these books losing to adverse selection.
    let thin = Touch {
        bid_px: 100.0,
        bid_qty: 0.0,
        ask_px: 100.5,
        ask_qty: 0.0,
    };
    assert!(!worth_resting(thin, RULE.tick_size));
    assert_eq!(resting_px(Side::Buy, thin, &RULE, &policy()), None);
    assert_eq!(
        opening(&intent(OrderKind::Market, false, Some(policy())), thin, &RULE),
        Opening::AsWritten,
        "the order goes out exactly as the strategy wrote it"
    );
}

#[test]
fn a_spread_too_thin_as_a_share_of_price_is_not_rested_either() {
    // Two whole ticks, but on a price where two ticks is a hundredth of a
    // basis point. The tick gate alone would let this one through.
    let fine = Touch {
        bid_px: 100_000.0,
        bid_qty: 0.0,
        ask_px: 100_001.0,
        ask_qty: 0.0,
    };
    assert!(fine.spread() >= MIN_SPREAD_TICKS * RULE.tick_size, "the tick gate passes");
    assert!(!worth_resting(fine, RULE.tick_size), "the share-of-price gate does not");
}

#[test]
fn an_exit_is_never_worked() {
    // A resting exit that does not fill is exposure nobody wanted, still on
    // the book.
    let exit = intent(OrderKind::Market, true, Some(policy()));
    assert_eq!(opening(&exit, wide(1000.0, 100.0), &RULE), Opening::AsWritten);
}

#[test]
fn an_order_with_no_policy_goes_out_as_written() {
    let plain = intent(OrderKind::Market, false, None);
    assert_eq!(opening(&plain, wide(0.0, 0.0), &RULE), Opening::AsWritten);
}

#[test]
fn a_book_leaning_our_way_rests_one_tick_inside_the_spread() {
    // Size stacked on our own side means we fill late and the price is about
    // to walk away, so jump the queue.
    let touch = wide(1000.0, 100.0);
    assert!(lean(Side::Buy, touch).unwrap() >= policy().improve_lean);
    assert_eq!(resting_px(Side::Buy, touch, &RULE, &policy()), Some(99.5));
    assert_eq!(
        opening(&intent(OrderKind::Market, false, Some(policy())), touch, &RULE),
        Opening::Rest { px: 99.5, policy: policy() }
    );
}

#[test]
fn a_book_leaning_against_us_rests_one_tick_behind_the_touch() {
    // The bid we would join is about to trade; joining it buys the adverse
    // fill.
    let touch = wide(100.0, 1000.0);
    assert!(lean(Side::Buy, touch).unwrap() <= -policy().back_lean);
    assert_eq!(resting_px(Side::Buy, touch, &RULE, &policy()), Some(98.5));
}

#[test]
fn without_displayed_sizes_the_order_plainly_joins_the_touch() {
    assert_eq!(lean(Side::Buy, no_sizes()), None);
    assert_eq!(resting_px(Side::Buy, no_sizes(), &RULE, &policy()), Some(99.0));
}

#[test]
fn a_balanced_book_joins_the_touch_too() {
    let touch = wide(500.0, 500.0);
    assert_eq!(lean(Side::Buy, touch), Some(0.0));
    assert_eq!(resting_px(Side::Buy, touch, &RULE, &policy()), Some(99.0));
}

#[test]
fn a_sell_reads_the_lean_from_its_own_chair() {
    // Size stacked on the ask is the seller's "leaning our way".
    let touch = wide(100.0, 1000.0);
    assert!(lean(Side::Sell, touch).unwrap() >= policy().improve_lean);
    assert_eq!(resting_px(Side::Sell, touch, &RULE, &policy()), Some(100.5));
    assert!(lean(Side::Sell, wide(1000.0, 100.0)).unwrap() <= -policy().back_lean);
    assert_eq!(resting_px(Side::Sell, wide(1000.0, 100.0), &RULE, &policy()), Some(101.5));
}

#[test]
fn a_strategy_that_priced_the_order_itself_keeps_its_price() {
    let priced = intent(
        OrderKind::Limit { px: 42.0, tif: TimeInForce::Gtc },
        false,
        Some(policy()),
    );
    assert_eq!(
        opening(&priced, wide(1000.0, 100.0), &RULE),
        Opening::WorkAsPriced { policy: policy() }
    );
}

// ------------------------------------------------------- while it rests

#[test]
fn a_touch_that_fell_away_does_not_drag_the_order_down() {
    // The bid retreated and left us alone at the front of the book, which is
    // the best place to be. Chasing it down would give that away.
    let state = buying(99.0, 0);
    let retreated = Touch { bid_px: 98.0, bid_qty: 0.0, ask_px: 100.0, ask_qty: 0.0 };
    let out = plan_work(&state, retreated, &RULE, 4 * SECOND, &policy());
    assert_eq!(out.step, WorkStep::Hold);
    assert!(out.looked, "the cadence still came round");
}

#[test]
fn a_retreating_touch_is_not_chased_even_near_the_end_of_the_window() {
    // The urgency ladder escalates; it does not chase.
    let state = buying(99.0, 0);
    let retreated = Touch { bid_px: 98.0, bid_qty: 0.0, ask_px: 100.0, ask_qty: 0.0 };
    let late = (0.9 * policy().window_ms as f64) as u64 * 1_000_000;
    assert_eq!(plan_work(&state, retreated, &RULE, late, &policy()).step, WorkStep::Hold);
}

#[test]
fn an_order_the_touch_overtook_comes_back_to_it() {
    let state = buying(99.0, 0);
    let moved_up = Touch { bid_px: 100.0, bid_qty: 0.0, ask_px: 102.0, ask_qty: 0.0 };
    assert_eq!(
        plan_work(&state, moved_up, &RULE, 4 * SECOND, &policy()).step,
        WorkStep::Move { px: 100.0 }
    );
}

#[test]
fn a_book_that_leans_against_us_keeps_the_order_back_while_the_window_is_young() {
    // Overtaken, but the touch ahead is about to trade: staying back is the
    // cheaper fill.
    let state = buying(98.5, 0);
    let against = Touch { bid_px: 99.0, bid_qty: 100.0, ask_px: 101.0, ask_qty: 1000.0 };
    assert_eq!(plan_work(&state, against, &RULE, 4 * SECOND, &policy()).step, WorkStep::Hold);

    // Past half the window it joins anyway: the clock outranks the lean.
    let half = policy().window_ms / 2 * 1_000_000;
    assert_eq!(
        plan_work(&state, against, &RULE, half, &policy()).step,
        WorkStep::Move { px: 99.0 }
    );
}

#[test]
fn past_the_improve_urgency_the_order_moves_inside_the_spread() {
    let state = buying(99.0, 0);
    let late = (policy().urgency_improve_frac * policy().window_ms as f64) as u64 * 1_000_000;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, late, &policy()).step,
        WorkStep::Move { px: 99.5 },
        "no lean needed: the clock alone improves at the end"
    );
}

#[test]
fn a_price_that_is_already_right_is_left_alone() {
    // Under half a tick is the same price, and moving costs queue position.
    let state = buying(99.0, 0);
    assert_eq!(plan_work(&state, no_sizes(), &RULE, 4 * SECOND, &policy()).step, WorkStep::Hold);
}

#[test]
fn the_amend_budget_stops_mattering_past_the_join_urgency() {
    let mut state = buying(99.0, 0);
    state.amends = policy().max_amends;
    let moved_up = Touch { bid_px: 100.0, bid_qty: 0.0, ask_px: 102.0, ask_qty: 0.0 };

    // Early in the window the budget holds it.
    assert_eq!(plan_work(&state, moved_up, &RULE, 4 * SECOND, &policy()).step, WorkStep::Hold);

    // Past the join threshold the escalation outranks the budget: the ladder
    // the budget would block is the whole reason the recipe measured cheaper.
    let half = policy().window_ms / 2 * 1_000_000;
    assert_eq!(
        plan_work(&state, moved_up, &RULE, half, &policy()).step,
        WorkStep::Move { px: 100.0 }
    );
}

#[test]
fn the_reprice_cadence_paces_the_looks() {
    let state = buying(99.0, 0);
    let moved_up = Touch { bid_px: 100.0, bid_qty: 0.0, ask_px: 102.0, ask_qty: 0.0 };
    let early = plan_work(&state, moved_up, &RULE, SECOND, &policy());
    assert_eq!(early.step, WorkStep::Hold);
    assert!(!early.looked, "the cadence has not come round");
    let due = plan_work(&state, moved_up, &RULE, 3 * SECOND, &policy());
    assert!(due.looked);
    assert_eq!(due.step, WorkStep::Move { px: 100.0 });
}

#[test]
fn a_book_that_cannot_be_read_still_costs_a_look() {
    // Otherwise a dark symbol is retried on every tick instead of on the
    // cadence.
    let state = buying(99.0, 0);
    let dark = Touch::default();
    let out = plan_work(&state, dark, &RULE, 4 * SECOND, &policy());
    assert_eq!(out.step, WorkStep::Hold);
    assert!(out.looked);
}

// -------------------------------------------------------------- crossing

#[test]
fn the_window_running_out_crosses() {
    let state = buying(99.0, 0);
    let over = policy().window_ms * 1_000_000;
    // Through the far touch with a pad, snapped the aggressive way round.
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, over, &policy()).step,
        WorkStep::Cross { px: 102.0 }
    );
}

#[test]
fn the_window_end_does_not_wait_for_the_reprice_cadence() {
    // The look was a moment ago, so the reprice gate would hold; the window
    // must not be held with it.
    let mut state = buying(99.0, 0);
    let over = policy().window_ms * 1_000_000;
    state.last_look_ns = over;
    assert!(matches!(
        plan_work(&state, no_sizes(), &RULE, over, &policy()).step,
        WorkStep::Cross { .. }
    ));
}

#[test]
fn a_sell_crosses_down_through_the_bid() {
    let state = WorkState::new(Side::Sell, 101.0, 0.0, 0);
    let over = policy().window_ms * 1_000_000;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, over, &policy()).step,
        WorkStep::Cross { px: 98.0 }
    );
}

#[test]
fn a_market_that_ran_against_the_decision_crosses_early() {
    // Waiting has been proven more expensive than the spread.
    let state = WorkState::new(Side::Buy, 99.0, 100.0, 0);
    let ran_up = Touch { bid_px: 101.5, bid_qty: 0.0, ask_px: 102.5, ask_qty: 0.0 };
    assert!(matches!(
        plan_work(&state, ran_up, &RULE, 4 * SECOND, &policy()).step,
        WorkStep::Cross { .. }
    ));
}

#[test]
fn a_market_that_only_drifted_a_little_keeps_waiting() {
    let state = WorkState::new(Side::Buy, 99.0, 100.0, 0);
    let nudged = Touch { bid_px: 100.0, bid_qty: 0.0, ask_px: 101.0, ask_qty: 0.0 };
    assert!(!matches!(
        plan_work(&state, nudged, &RULE, 4 * SECOND, &policy()).step,
        WorkStep::Cross { .. }
    ));
}

#[test]
fn the_early_cross_stays_off_without_a_decision_mid() {
    // A mid of zero means we never knew where the market was when the order
    // was decided, so there is nothing to measure drift against.
    let state = WorkState::new(Side::Buy, 99.0, 0.0, 0);
    let ran_up = Touch { bid_px: 101.5, bid_qty: 0.0, ask_px: 102.5, ask_qty: 0.0 };
    assert!(!matches!(
        plan_work(&state, ran_up, &RULE, 4 * SECOND, &policy()).step,
        WorkStep::Cross { .. }
    ));
}

#[test]
fn the_early_cross_can_be_turned_off() {
    let state = WorkState::new(Side::Buy, 99.0, 100.0, 0);
    let ran_up = Touch { bid_px: 101.5, bid_qty: 0.0, ask_px: 102.5, ask_qty: 0.0 };
    let off = WorkPolicy { drift_cross_fee_bp: 0.0, ..policy() };
    assert!(!matches!(
        plan_work(&state, ran_up, &RULE, 4 * SECOND, &off).step,
        WorkStep::Cross { .. }
    ));
}

#[test]
fn a_cross_with_no_readable_book_still_has_to_start_the_grace_clock() {
    // Otherwise the cancel that bounds the order has no deadline to fire on.
    let state = buying(99.0, 0);
    let over = policy().window_ms * 1_000_000;
    assert_eq!(
        plan_work(&state, Touch::default(), &RULE, over, &policy()).step,
        WorkStep::CrossUnpriced
    );
}

#[test]
fn cross_retries_are_paced_on_the_reprice_cadence() {
    // Each attempt is a signed venue call. Retrying on every pass spent the
    // whole grace window blocking the loop and tripped the rate limit.
    let mut state = buying(99.0, 0);
    state.cross_started_ns = 10 * SECOND;
    state.last_cross_try_ns = 10 * SECOND;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, 11 * SECOND, &policy()).step,
        WorkStep::Hold
    );
    assert!(matches!(
        plan_work(&state, no_sizes(), &RULE, 13 * SECOND, &policy()).step,
        WorkStep::Cross { .. }
    ));
}

#[test]
fn a_cross_the_venue_took_stops_the_retries() {
    let mut state = buying(102.0, 0);
    state.cross_started_ns = 10 * SECOND;
    state.last_cross_try_ns = 10 * SECOND;
    state.crossed = true;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, 20 * SECOND, &policy()).step,
        WorkStep::Hold,
        "it is crossed and inside the grace; there is nothing to do but wait"
    );
}

#[test]
fn the_grace_running_out_cancels() {
    let mut state = buying(102.0, 0);
    state.cross_started_ns = 10 * SECOND;
    state.crossed = true;
    let grace_over = 10 * SECOND + policy().cross_grace_ms * 1_000_000;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, grace_over, &policy()).step,
        WorkStep::Cancel
    );
}

#[test]
fn cancel_retries_are_paced_too() {
    // A venue that keeps refusing must not become a hot loop.
    let mut state = buying(102.0, 0);
    state.cross_started_ns = 10 * SECOND;
    state.crossed = true;
    let grace_over = 10 * SECOND + policy().cross_grace_ms * 1_000_000;
    state.last_cancel_try_ns = grace_over;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, grace_over + SECOND, &policy()).step,
        WorkStep::Hold
    );
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, grace_over + 3 * SECOND, &policy()).step,
        WorkStep::Cancel
    );
}

#[test]
fn an_order_already_asked_to_be_pulled_is_left_alone() {
    let mut state = buying(102.0, 0);
    state.cross_started_ns = 10 * SECOND;
    state.crossed = true;
    state.cancel_requested = true;
    let grace_over = 10 * SECOND + policy().cross_grace_ms * 1_000_000;
    assert_eq!(
        plan_work(&state, no_sizes(), &RULE, grace_over + 10 * SECOND, &policy()).step,
        WorkStep::Hold
    );
}

#[test]
fn a_crossing_order_is_never_repriced_back_to_a_passive_price() {
    // The branch order is what guarantees this: the crossing arm returns
    // before the desired-price arm is ever reached.
    let mut state = buying(102.0, 0);
    state.cross_started_ns = 10 * SECOND;
    state.crossed = true;
    let moved_up = Touch { bid_px: 100.0, bid_qty: 0.0, ask_px: 102.0, ask_qty: 0.0 };
    assert_eq!(
        plan_work(&state, moved_up, &RULE, 15 * SECOND, &policy()).step,
        WorkStep::Hold
    );
}

#[test]
fn a_symbol_with_no_tick_is_not_worked_at_all() {
    let no_rule = InstrumentRule { tick_size: 0.0, ..RULE };
    let state = buying(99.0, 0);
    assert_eq!(
        plan_work(&state, no_sizes(), &no_rule, 999 * SECOND, &policy()).step,
        WorkStep::Hold
    );
}
