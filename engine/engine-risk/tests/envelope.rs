//! The equity-anchored envelope, against the same table as
//! tests/policy/test_equity_anchored_envelope.py, plus the worst-case-loss
//! allowance the Rust deny reason is written in.

mod common;

use common::*;
use engine_risk::{Kernel, KernelConfig};
use engine_types::orders::Side;
use engine_types::risk::{DenyReason, RiskKernel, RiskVerdict};

fn observe(kernel: &mut Kernel, equity: f64) -> RiskVerdict {
    let now = SEC;
    kernel.assess(
        &entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, now),
        &flat(equity, now),
    )
}

#[test]
// A fill lands, the account view has not caught up, and the reservation
// already drained — for that window the filled position must still count
// against the envelope, or a second order is judged against a book the
// kernel knows is missing a position.
fn a_fill_newer_than_the_view_still_counts_against_the_envelope() {
    use engine_types::orders::OrderUpdate;

    let cfg = KernelConfig { ..demo_config() };
    let mut kernel = Kernel::new(cfg).expect("config");
    kernel.observe_price(BUSDT, 10.0);
    kernel.observe_price(CUSDT, 10.0);

    // 400k notional fills at 2*SEC; the view below is from SEC.
    let filled = entry(CARRY, BUSDT, Side::Buy, 40_000.0, 10.0, 9.0, SEC);
    kernel.register_order("f1", &filled, 40_000.0);
    kernel.on_update(&OrderUpdate::Fill {
        exec_id: String::new(),
        client_order_id: "f1".to_string(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 40_000.0,
        px: 10.0,
        fee: Some(0.0),
        is_maker: false,
        venue_ts_ms: 0,
        recv_ns: 2 * SEC,
    });

    // 150k more would fit an empty book (52.5k of 175k allowance) but not
    // one already carrying the 400k fill (140k + 52.5k > 175k).
    let next = entry(CARRY, CUSDT, Side::Buy, 15_000.0, 10.0, 9.0, 3 * SEC);
    let stale_view = flat(250_000.0, SEC);
    assert!(matches!(
        kernel.assess(&next, &stale_view),
        RiskVerdict::Deny {
            reason: DenyReason::EnvelopeBreached { .. }
        }
    ));

    // Once the view includes the fill (observed after it), the recent-fill
    // term must NOT double-count on top of the view's position.
    let caught_up = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 40_000.0, 10.0, true)],
        3 * SEC,
    );
    let small = entry(CARRY, CUSDT, Side::Buy, 5_000.0, 10.0, 9.0, 4 * SEC);
    assert_eq!(
        kernel.assess(&small, &caught_up),
        RiskVerdict::Allow { qty: 5_000.0 },
        "the caught-up view must not be double-counted"
    );
}

#[test]
// test_a_profile_without_the_block_keeps_the_historical_fixed_reference
fn a_config_that_does_not_track_equity_keeps_its_fixed_reference() {
    let cfg = KernelConfig { ..demo_config() };
    let mut kernel = Kernel::new(cfg).expect("config");
    observe(&mut kernel, 1.0);
    assert_eq!(kernel.capital_reference_usdt(), 250_000.0);
}

#[test]
// test_the_reference_follows_equity_down_immediately
fn the_reference_follows_equity_down_immediately() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let start = kernel.capital_reference_usdt();
    observe(&mut kernel, start * 0.99);
    assert_eq!(kernel.capital_reference_usdt(), start * 0.99);
}

#[test]
// test_expansion_waits_for_a_move_larger_than_the_dead_band
fn expansion_waits_for_a_move_larger_than_the_dead_band() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let start = kernel.capital_reference_usdt();

    observe(&mut kernel, start * 1.02);
    assert_eq!(kernel.capital_reference_usdt(), start);

    observe(&mut kernel, start * 1.20);
    assert_eq!(kernel.capital_reference_usdt(), start * 1.20);
}

#[test]
// test_unknown_equity_moves_nothing, and the same readings fail closed here
fn unknown_equity_moves_nothing_and_refuses() {
    for equity in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
        let start = kernel.capital_reference_usdt();
        assert!(
            matches!(
                observe(&mut kernel, equity),
                RiskVerdict::Deny {
                    reason: DenyReason::UnknownState { .. }
                }
            ),
            "equity {equity} must refuse"
        );
        assert_eq!(kernel.capital_reference_usdt(), start);
    }
}

#[test]
// test_the_floor_bounds_the_envelope_rather_than_collapsing_it
fn the_floor_bounds_the_envelope_rather_than_collapsing_it() {
    let mut cfg = equity_tracking_config();
    cfg.envelope.floor_usdt = 500.0;
    let mut kernel = Kernel::new(cfg).expect("config");
    observe(&mut kernel, 1.0);
    assert_eq!(kernel.capital_reference_usdt(), 500.0);
}

#[test]
// test_an_equity_fraction_can_hold_the_book_below_the_wallet
fn an_equity_fraction_can_hold_the_book_below_the_wallet() {
    let mut cfg = equity_tracking_config();
    cfg.envelope.equity_fraction = 0.25;
    let mut kernel = Kernel::new(cfg).expect("config");
    observe(&mut kernel, 10_000.0);
    assert_eq!(kernel.capital_reference_usdt(), 2_500.0);
}

#[test]
// test_the_profile_refuses_an_unbounded_or_oversized_anchor
fn the_config_refuses_an_unbounded_or_oversized_anchor() {
    let mut zero_floor = equity_tracking_config();
    zero_floor.envelope.floor_usdt = 0.0;
    assert!(Kernel::new(zero_floor)
        .err()
        .expect("must refuse")
        .detail
        .contains("floor_usdt must be positive"));

    let mut oversized = equity_tracking_config();
    oversized.envelope.equity_fraction = 1.5;
    assert!(Kernel::new(oversized)
        .err()
        .expect("must refuse")
        .detail
        .contains("equity_fraction cannot exceed 1"));

    let mut no_stop_distance = equity_tracking_config();
    no_stop_distance.envelope.disaster_stop_fraction = 1.0;
    assert!(Kernel::new(no_stop_distance)
        .err()
        .expect("must refuse")
        .detail
        .contains("fraction in (0, 1)"));
}

// --------------------------------------------------------------------------
// The allowance itself. Demo shape: reference 250_000, gross cap twice it, and
// a 0.35 disaster stop, so the book may risk 175_000.
// --------------------------------------------------------------------------

#[test]
fn a_book_exactly_at_the_allowance_is_allowed() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let now = SEC;
    // 500_000 notional * 0.35 = 175_000 = 250_000 * 2.0 * 0.35.
    let intent = entry(CARRY, BUSDT, Side::Buy, 50_000.0, 10.0, 9.0, now);
    assert_eq!(
        kernel.assess(&intent, &flat(250_000.0, now)),
        RiskVerdict::Allow { qty: 50_000.0 }
    );
}

#[test]
fn a_book_one_step_over_the_allowance_is_refused() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let now = SEC;
    let intent = entry(CARRY, BUSDT, Side::Buy, 50_001.0, 10.0, 9.0, now);
    match kernel.assess(&intent, &flat(250_000.0, now)) {
        RiskVerdict::Deny {
            reason:
                DenyReason::EnvelopeBreached {
                    worst_case_loss_usdt,
                    allowance_usdt,
                },
        } => {
            assert!((worst_case_loss_usdt - 175_003.5).abs() < 1e-6);
            assert!((allowance_usdt - 175_000.0).abs() < 1e-6);
        }
        other => panic!("expected an envelope breach, got {other:?}"),
    }
}

#[test]
fn the_positions_already_held_count_against_the_allowance() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let now = SEC;
    let held = view(
        250_000.0,
        vec![position(CUSDT, Side::Buy, 40_000.0, 10.0, true)],
        now,
    );
    // 400_000 held + 110_000 asked is over the 500_000 the allowance funds.
    let intent = entry(CARRY, BUSDT, Side::Buy, 11_000.0, 10.0, 9.0, now);
    assert!(matches!(
        kernel.assess(&intent, &held),
        RiskVerdict::Deny {
            reason: DenyReason::EnvelopeBreached { .. }
        }
    ));

    let smaller = entry(CARRY, BUSDT, Side::Buy, 10_000.0, 10.0, 9.0, now);
    assert_eq!(
        kernel.assess(&smaller, &held),
        RiskVerdict::Allow { qty: 10_000.0 }
    );
}

#[test]
fn a_stop_wider_than_the_disaster_stop_is_charged_at_its_own_distance() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let now = SEC;
    // 400_000 notional: 140_000 at the 0.35 disaster stop, 200_000 at this
    // order's own stop half the entry price away.
    let tight = entry(CARRY, BUSDT, Side::Buy, 40_000.0, 10.0, 9.0, now);
    assert_eq!(
        kernel.assess(&tight, &flat(250_000.0, now)),
        RiskVerdict::Allow { qty: 40_000.0 }
    );

    let wide = entry(CARRY, BUSDT, Side::Buy, 40_000.0, 10.0, 5.0, now);
    match kernel.assess(&wide, &flat(250_000.0, now)) {
        RiskVerdict::Deny {
            reason:
                DenyReason::EnvelopeBreached {
                    worst_case_loss_usdt,
                    ..
                },
        } => assert!((worst_case_loss_usdt - 200_000.0).abs() < 1e-6),
        other => panic!("expected an envelope breach, got {other:?}"),
    }
}

#[test]
fn the_allowance_follows_equity_down() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let now = SEC;
    let intent = entry(CARRY, BUSDT, Side::Buy, 50_000.0, 10.0, 9.0, now);
    assert_eq!(
        kernel.assess(&intent, &flat(250_000.0, now)),
        RiskVerdict::Allow { qty: 50_000.0 }
    );

    // The same order against a wallet that contracted 1%: the allowance is now
    // 173_250 and 175_000 of worst case no longer fits.
    assert!(matches!(
        kernel.assess(&intent, &flat(247_500.0, now)),
        RiskVerdict::Deny {
            reason: DenyReason::EnvelopeBreached { .. }
        }
    ));
}

#[test]
// test_an_exit_is_never_blocked_by_the_partition, same rule for the envelope:
// a risk-reducing order bypasses the caps by design.
fn an_exit_is_never_blocked_by_the_envelope() {
    let mut kernel = Kernel::new(equity_tracking_config()).expect("config");
    let now = SEC;
    let held = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 60_000.0, 10.0, true)],
        now,
    );
    let out = exit(CARRY, BUSDT, Side::Sell, 60_000.0, 10.0, now);
    assert_eq!(
        kernel.assess(&out, &held),
        RiskVerdict::Allow { qty: 60_000.0 }
    );
}
