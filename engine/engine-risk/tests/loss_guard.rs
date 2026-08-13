//! The account loss guard, against the same table as
//! tests/policy/test_account_loss_guard.py.

mod common;

use common::*;
use engine_risk::{Kernel, KernelConfig, LossGuardConfig};
use engine_types::orders::Side;
use engine_types::risk::{DenyReason, RiskKernel, RiskVerdict};

const DAY1: u64 = 20_664;
const DAY2: u64 = 20_665;

fn kernel_at(day: u64) -> Kernel {
    let mut kernel = Kernel::new(demo_config()).expect("config");
    kernel.observe_wall_clock_ns(utc_noon(day));
    kernel
}

fn buy(now_ns: u64) -> engine_types::orders::Intent {
    entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, now_ns)
}

fn assess(kernel: &mut Kernel, equity: f64, now_ns: u64) -> RiskVerdict {
    kernel.assess(&buy(now_ns), &flat(equity, now_ns))
}

#[test]
// test_first_reading_of_a_day_sets_the_anchor_and_never_trips
fn the_first_reading_of_a_day_sets_the_anchor_and_never_trips() {
    let mut kernel = kernel_at(DAY1);
    assert_eq!(
        assess(&mut kernel, 250_000.0, SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
    assert_eq!(
        kernel.loss_guard_anchor().opening_equity_usdt,
        Some(250_000.0)
    );
}

#[test]
// The fleet's answer to a trip is "flatten and stop": risk-reducing orders
// flow while entries are refused (the owner's flatten IS exits). An engine
// that blocks the flatten is stricter in the one direction that hurts.
fn a_tripped_guard_still_lets_a_genuine_exit_through() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assert!(matches!(
        assess(&mut kernel, 249_000.0, 2 * SEC),
        RiskVerdict::Deny { .. }
    ));

    let held = view(
        249_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        3 * SEC,
    );
    let out = kernel.assess(&exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, 3 * SEC), &held);
    assert_eq!(out, RiskVerdict::Allow { qty: 5.0 });

    // The trip still stands against new risk.
    assert!(matches!(
        assess(&mut kernel, 260_000.0, 4 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
// test_a_loss_below_the_ceiling_is_not_a_trip
fn a_loss_below_the_ceiling_is_not_a_trip() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assert_eq!(
        assess(&mut kernel, 249_001.0, 2 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
// test_reaching_the_ceiling_trips_and_the_trip_is_permanent
fn reaching_the_ceiling_trips_and_the_trip_is_permanent() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assert_eq!(
        assess(&mut kernel, 249_000.0, 2 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped {
                equity_usdt: 249_000.0,
                floor_usdt: 249_000.0,
            }
        }
    );

    // Equity recovering fully does not un-trip it.
    assert!(matches!(
        assess(&mut kernel, 260_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));

    // Nor does a new day.
    kernel.observe_wall_clock_ns(utc_noon(DAY2));
    assert!(matches!(
        assess(&mut kernel, 260_000.0, 4 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
// test_only_an_explicit_reset_clears_a_trip
fn only_an_explicit_reset_clears_a_trip() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assess(&mut kernel, 248_000.0, 2 * SEC);
    assert!(kernel.loss_guard_anchor().trip.is_some());

    kernel.reset_loss_guard();
    assert!(kernel.loss_guard_anchor().trip.is_none());
    assert_eq!(
        assess(&mut kernel, 248_000.0, 3 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
// test_a_new_utc_day_re_anchors_the_budget
fn a_new_utc_day_re_anchors_the_budget() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assess(&mut kernel, 249_500.0, 2 * SEC);

    kernel.observe_wall_clock_ns(utc_noon(DAY2));
    assess(&mut kernel, 249_500.0, 3 * SEC);
    assert_eq!(
        kernel.loss_guard_anchor().opening_equity_usdt,
        Some(249_500.0)
    );

    // The prior day's 500 loss no longer counts against the new day.
    assert_eq!(
        assess(&mut kernel, 248_600.0, 4 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
// test_stale_equity_blocks_new_risk_but_does_not_trip
fn a_stale_view_blocks_new_risk_but_does_not_trip() {
    let mut kernel = kernel_at(DAY1);
    let observed = 200 * SEC;
    assess(&mut kernel, 250_000.0, observed);

    let now = observed + 121 * SEC;
    let verdict = kernel.assess(&buy(now), &flat(250_000.0, observed));
    assert_eq!(
        verdict,
        RiskVerdict::Deny {
            reason: DenyReason::StaleAccountView {
                age_ns: 121 * SEC,
                max_age_ns: MAX_VIEW_AGE_NS,
            }
        }
    );
    assert!(
        kernel.loss_guard_anchor().trip.is_none(),
        "staleness must never flatten the book"
    );
}

#[test]
// test_a_feed_that_recovers_returns_to_ok
fn a_view_that_recovers_returns_to_allowing() {
    let mut kernel = kernel_at(DAY1);
    let observed = 200 * SEC;
    assess(&mut kernel, 250_000.0, observed);
    assert!(matches!(
        kernel.assess(&buy(observed + 200 * SEC), &flat(250_000.0, observed)),
        RiskVerdict::Deny {
            reason: DenyReason::StaleAccountView { .. }
        }
    ));
    let fresh = observed + 210 * SEC;
    assert_eq!(
        assess(&mut kernel, 249_900.0, fresh),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
// test_a_backwards_clock_blocks_rather_than_trusting_the_reading
fn a_view_from_after_the_decision_refuses_rather_than_trusting_the_reading() {
    let mut kernel = kernel_at(DAY1);
    let now = 100 * SEC;
    let verdict = kernel.assess(&buy(now), &flat(250_000.0, now + 5 * SEC));
    assert!(matches!(
        verdict,
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
    assert!(kernel.loss_guard_anchor().trip.is_none());
}

#[test]
// test_a_restart_that_restores_its_anchor_keeps_the_days_budget
fn a_restart_that_restores_its_anchor_keeps_the_days_budget() {
    let mut first = kernel_at(DAY1);
    assess(&mut first, 250_000.0, SEC);
    assess(&mut first, 249_200.0, 2 * SEC);
    let saved = first.loss_guard_anchor();

    let mut revived = kernel_at(DAY1);
    revived.restore_loss_guard(saved);
    assert_eq!(
        revived.loss_guard_anchor().opening_equity_usdt,
        Some(250_000.0)
    );
    assert!(matches!(
        assess(&mut revived, 249_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
// test_a_restart_without_the_anchor_would_have_refreshed_the_budget
fn a_restart_without_the_anchor_would_have_refreshed_the_budget() {
    let mut naive = kernel_at(DAY1);
    assess(&mut naive, 249_200.0, 2 * SEC);
    assert_eq!(
        naive.loss_guard_anchor().opening_equity_usdt,
        Some(249_200.0)
    );
    assert_eq!(
        assess(&mut naive, 249_000.0, 3 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
// test_a_restored_trip_stays_tripped
fn a_restored_trip_stays_tripped() {
    let mut first = kernel_at(DAY1);
    assess(&mut first, 250_000.0, SEC);
    assess(&mut first, 248_000.0, 2 * SEC);

    let mut revived = kernel_at(DAY1);
    revived.restore_loss_guard(first.loss_guard_anchor());
    assert!(revived.loss_guard_anchor().trip.is_some());
    assert!(matches!(
        assess(&mut revived, 260_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
// test_an_unset_ceiling_never_trips_but_still_blocks_on_blindness
fn an_unset_ceiling_never_trips_but_still_refuses_on_blindness() {
    let cfg = KernelConfig {
        loss_guard: LossGuardConfig {
            max_daily_loss_usdt: None,
        },
        ..demo_config()
    };
    let mut kernel = Kernel::new(cfg).expect("config");
    kernel.observe_wall_clock_ns(utc_noon(DAY1));
    assess(&mut kernel, 250_000.0, 200 * SEC);
    assert_eq!(
        assess(&mut kernel, 1_000.0, 201 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );

    let observed = 201 * SEC;
    assert!(matches!(
        kernel.assess(&buy(observed + 500 * SEC), &flat(1_000.0, observed)),
        RiskVerdict::Deny {
            reason: DenyReason::StaleAccountView { .. }
        }
    ));
}

#[test]
// test_a_non_positive_ceiling_is_rejected_at_construction
fn a_non_positive_ceiling_is_rejected_at_construction() {
    for bad in [0.0, -1.0] {
        let cfg = KernelConfig {
            loss_guard: LossGuardConfig {
                max_daily_loss_usdt: Some(bad),
            },
            ..demo_config()
        };
        let err = Kernel::new(cfg).err().expect("must refuse");
        assert!(err.detail.contains("must be positive"), "{}", err.detail);
    }
}

#[test]
// The exit exemption is only for GENUINE reductions: a reduce_only flag on
// an order that reduces nothing is unknown state, tripped or not.
fn a_tripped_guard_still_refuses_an_exit_that_reduces_nothing() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assess(&mut kernel, 249_000.0, 2 * SEC);

    let flat_book = flat(249_000.0, 3 * SEC);
    let out = exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, 3 * SEC);
    assert!(matches!(
        kernel.assess(&out, &flat_book),
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
}
