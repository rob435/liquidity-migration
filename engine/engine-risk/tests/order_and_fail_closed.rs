//! Two properties the individual tables cannot show: that the controls are
//! evaluated in the documented order, and that anything the kernel cannot
//! positively classify refuses.

mod common;

use common::*;
use engine_risk::Kernel;
use engine_types::orders::{OrderKind, Side, TimeInForce};
use engine_types::risk::{AccountView, DenyReason, RiskKernel, RiskVerdict};

const NOW: u64 = 300 * SEC;

#[test]
// One resting exit already covers the position; approving a second at full
// size means two venue orders racing to close one position. The venue's
// reduce-only handling bounds each order alone, not the stack.
fn a_position_already_covered_by_a_resting_exit_takes_no_second_exit() {
    let mut k = kernel();
    let held = view(
        1_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        SEC,
    );

    let first = exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, 2 * SEC);
    assert_eq!(k.assess(&first, &held), RiskVerdict::Allow { qty: 5.0 });
    k.register_order("x1", &first, 5.0);

    let second = exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, 3 * SEC);
    assert!(matches!(
        k.assess(&second, &held),
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));

    // The venue rejecting the first frees the position for a retry.
    k.on_update(&engine_types::orders::OrderUpdate::Reject {
        client_order_id: "x1".to_string(),
        code: 1,
        reason: "test".to_string(),
    });
    let retry = exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, 4 * SEC);
    assert_eq!(k.assess(&retry, &held), RiskVerdict::Allow { qty: 5.0 });
}

#[test]
fn an_exit_with_an_unreadable_limit_price_is_refused() {
    let mut k = kernel();
    let held = view(
        1_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        SEC,
    );
    let mut bad = exit(CARRY, BUSDT, Side::Sell, 5.0, -5.0, 2 * SEC);
    bad.kind = engine_types::orders::OrderKind::Limit {
        px: -5.0,
        tif: engine_types::orders::TimeInForce::Gtc,
    };
    assert!(matches!(
        k.assess(&bad, &held),
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
}

#[test]
// Blindness stops new risk, never de-risking: the fleet exits against its
// reconstructed positions while blocked, and the venue's own reduce-only
// enforcement bounds a mis-sized exit from a stale reading.
fn a_stale_reading_still_lets_a_genuine_exit_through() {
    let mut k = kernel();
    let held = view(
        1_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        SEC,
    );
    let decided = SEC + MAX_VIEW_AGE_NS + SEC;

    let out = k.assess(&exit(CARRY, BUSDT, Side::Sell, 7.0, 10.0, decided), &held);
    assert_eq!(out, RiskVerdict::Allow { qty: 5.0 }, "clamped to the position");

    assert!(matches!(
        k.assess(&entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, decided), &held),
        RiskVerdict::Deny {
            reason: DenyReason::StaleAccountView { .. }
        }
    ));
}

#[test]
fn a_fresh_fill_is_immediately_available_to_a_reduce_only_exit() {
    use engine_types::orders::OrderUpdate;
    let mut k = kernel();
    let filled = entry(CARRY, BUSDT, Side::Buy, 3.0, 10.0, 9.0, SEC);
    k.register_order("fresh-entry", &filled, 3.0);
    k.on_update(&OrderUpdate::Fill {
        exec_id: String::new(),
        client_order_id: "fresh-entry".to_string(), symbol: BUSDT, side: Side::Buy,
        qty: 3.0, px: 10.0, fee: 0.0, is_maker: false, venue_ts_ms: 0, recv_ns: 2 * SEC,
    });
    let out = exit(CARRY, BUSDT, Side::Sell, 9.0, 10.0, 3 * SEC);
    assert_eq!(k.assess(&out, &flat(1_000.0, SEC)), RiskVerdict::Allow { qty: 3.0 });
}

fn kernel() -> Kernel {
    Kernel::new(demo_config()).expect("config")
}

fn deny_reason(verdict: RiskVerdict) -> DenyReason {
    match verdict {
        RiskVerdict::Deny { reason } => reason,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

// --------------------------------------------------------------------------
// Order of evaluation
// --------------------------------------------------------------------------

#[test]
// Readability is judged before age, so a genuine exit can still be sized from
// a stale-but-readable view.
fn an_unreadable_equity_is_reported_even_when_the_view_is_also_stale() {
    let mut kernel = kernel();
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
    let stale = flat(f64::NAN, NOW - 121 * SEC);
    assert!(matches!(
        deny_reason(kernel.assess(&intent, &stale)),
        DenyReason::UnknownState { .. }
    ));
}

#[test]
fn a_missing_stop_is_reported_before_a_breached_cap() {
    let mut cfg = demo_config();
    // Small enough that the order below breaches every cap under the stop
    // check as well as failing it.
    cfg.envelope.max_component_gross_notional_usdt = 1.0;
    let mut kernel = Kernel::new(cfg).expect("config");
    let naked = naked_entry(CARRY, BUSDT, Side::Buy, 100.0, 10.0, NOW);
    assert_eq!(
        deny_reason(kernel.assess(&naked, &flat(10_000.0, NOW))),
        DenyReason::MissingStop
    );
}

#[test]
fn an_envelope_breach_is_reported_before_the_account_caps() {
    let mut cfg = demo_config();
    cfg.envelope.reference_usdt = 500.0;
    cfg.envelope.max_component_gross_notional_usdt = 1_000.0;
    cfg.envelope.max_initial_margin_usdt = 500.0;
    let mut kernel = Kernel::new(cfg).expect("config");
    // 2000 USDT of book: past the envelope's allowance and past the gross
    // ceiling, so which one is named says which ran first.
    let huge = entry(CARRY, BUSDT, Side::Buy, 200.0, 10.0, 9.0, NOW);
    assert!(matches!(
        deny_reason(kernel.assess(&huge, &flat(10_000.0, NOW))),
        DenyReason::EnvelopeBreached { .. }
    ));
}

// --------------------------------------------------------------------------
// Fail closed
// --------------------------------------------------------------------------

#[test]
fn a_stale_view_refuses() {
    let mut kernel = kernel();
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
    let age_ns = MAX_VIEW_AGE_NS + 1;
    assert_eq!(
        deny_reason(kernel.assess(&intent, &flat(250_000.0, NOW - age_ns))),
        DenyReason::StaleAccountView {
            age_ns,
            max_age_ns: MAX_VIEW_AGE_NS,
        }
    );

    // Exactly at the bound is still evidence about the account now.
    assert!(matches!(
        kernel.assess(&intent, &flat(250_000.0, NOW - MAX_VIEW_AGE_NS)),
        RiskVerdict::Allow { .. }
    ));
}

#[test]
fn an_unreadable_equity_refuses() {
    for equity in [f64::NAN, f64::INFINITY, -1.0, 0.0] {
        let mut kernel = kernel();
        let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
        assert!(
            matches!(
                deny_reason(kernel.assess(&intent, &flat(equity, NOW))),
                DenyReason::UnknownState { .. }
            ),
            "equity {equity} must refuse"
        );
    }
}

#[test]
fn a_view_holding_both_sides_of_one_symbol_refuses() {
    let mut kernel = kernel();
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
    let conflicted = view(
        250_000.0,
        vec![
            position(BUSDT, Side::Buy, 1.0, 10.0, true),
            position(BUSDT, Side::Sell, 1.0, 10.0, true),
        ],
        NOW,
    );
    assert!(matches!(
        deny_reason(kernel.assess(&intent, &conflicted)),
        DenyReason::UnknownState { .. }
    ));
}

#[test]
fn an_unreadable_position_refuses() {
    let cases = [
        position(BUSDT, Side::Buy, f64::NAN, 10.0, true),
        position(BUSDT, Side::Buy, -1.0, 10.0, true),
        position(BUSDT, Side::Buy, 1.0, 0.0, true),
        position(BUSDT, Side::Buy, 1.0, f64::NAN, true),
    ];
    for bad in cases {
        let mut kernel = kernel();
        let intent = entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
        let held = view(250_000.0, vec![bad.clone()], NOW);
        assert!(
            matches!(
                deny_reason(kernel.assess(&intent, &held)),
                DenyReason::UnknownState { .. }
            ),
            "position {bad:?} must refuse"
        );
    }
}

#[test]
fn an_unreadable_intent_refuses() {
    let mut kernel = kernel();
    for qty in [f64::NAN, 0.0, -1.0] {
        let mut intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
        intent.qty = qty;
        assert!(
            matches!(
                deny_reason(kernel.assess(&intent, &flat(250_000.0, NOW))),
                DenyReason::UnknownState { .. }
            ),
            "quantity {qty} must refuse"
        );
    }

    let mut bad_price = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
    bad_price.kind = OrderKind::Limit {
        px: f64::NAN,
        tif: TimeInForce::Gtc,
    };
    assert!(matches!(
        deny_reason(kernel.assess(&bad_price, &flat(250_000.0, NOW))),
        DenyReason::UnknownState { .. }
    ));
}

#[test]
fn a_symbol_the_kernel_cannot_price_refuses() {
    let mut kernel = kernel();
    let blind = market_entry(CARRY, BUSDT, Side::Buy, 1.0, 9.3, NOW);
    assert!(matches!(
        deny_reason(kernel.assess(&blind, &flat(250_000.0, NOW))),
        DenyReason::UnknownState { .. }
    ));
}

#[test]
fn a_reduce_only_order_that_does_not_reduce_refuses() {
    let mut kernel = kernel();

    // Nothing to reduce.
    let out = exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, NOW);
    assert!(matches!(
        deny_reason(kernel.assess(&out, &flat(250_000.0, NOW))),
        DenyReason::UnknownState { .. }
    ));

    // The same side as the position it names.
    let held = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 1.0, 10.0, true)],
        NOW,
    );
    let wrong_way = exit(CARRY, BUSDT, Side::Buy, 1.0, 10.0, NOW);
    assert!(matches!(
        deny_reason(kernel.assess(&wrong_way, &held)),
        DenyReason::UnknownState { .. }
    ));
}

#[test]
// A reduction is sized against the reconstructed position
// alone; asking to close more than is provably open is clamped to it.
fn an_oversized_exit_is_clamped_to_the_position() {
    let mut kernel = kernel();
    let held = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 2.0, 10.0, true)],
        NOW,
    );
    let out = exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, NOW);
    assert_eq!(kernel.assess(&out, &held), RiskVerdict::Allow { qty: 2.0 });
}

#[test]
fn an_entry_that_crosses_through_flat_refuses() {
    let mut kernel = kernel();
    let held = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 2.0, 10.0, true)],
        NOW,
    );
    let flip = entry(CARRY, BUSDT, Side::Sell, 5.0, 10.0, 11.0, NOW);
    assert!(matches!(
        deny_reason(kernel.assess(&flip, &held)),
        DenyReason::UnknownState { .. }
    ));
}

#[test]
fn a_view_from_after_the_decision_refuses() {
    let mut kernel = kernel();
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW);
    let ahead = AccountView {
        observed_ns: NOW + 1,
        ..flat(250_000.0, NOW)
    };
    assert!(matches!(
        deny_reason(kernel.assess(&intent, &ahead)),
        DenyReason::UnknownState { .. }
    ));
}
