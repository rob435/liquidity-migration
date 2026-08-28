//! The durable UTC daily-loss halt.

mod common;

use common::*;
use engine_risk::{
    cleared_loss_guard_state, Kernel, KernelConfig, LossGuardAnchor, LossGuardConfig,
};
use engine_types::orders::Side;
use engine_types::risk::{DenyReason, RiskKernel, RiskVerdict};

const DAY1: u64 = 20_664;
const DAY2: u64 = DAY1 + 1;

fn utc_noon(day: u64) -> u64 {
    (day * 86_400 + 12 * 60 * 60) * SEC
}

fn utc_boundary(day: u64, offset_seconds: i64) -> u64 {
    ((day as i64 * 86_400) + offset_seconds) as u64 * SEC
}

fn guarded_config() -> KernelConfig {
    KernelConfig {
        loss_guard: LossGuardConfig {
            max_daily_loss_usdt: Some(1_000.0),
        },
        ..demo_config()
    }
}

fn kernel_at(day: u64) -> Kernel {
    let mut kernel = Kernel::new(guarded_config()).expect("config");
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
fn reaching_the_floor_latches_the_trip() {
    let mut kernel = kernel_at(DAY1);
    assert_eq!(
        assess(&mut kernel, 250_000.0, SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
    assert_eq!(
        assess(&mut kernel, 249_001.0, 2 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
    assert_eq!(
        assess(&mut kernel, 249_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped {
                equity_usdt: 249_000.0,
                floor_usdt: 249_000.0,
            }
        }
    );

    // Recovery and a new UTC day do not clear a halt.
    kernel.observe_wall_clock_ns(utc_noon(DAY2));
    assert!(matches!(
        assess(&mut kernel, 260_000.0, 4 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
fn fresh_account_views_trip_without_waiting_for_another_intent() {
    let mut kernel = kernel_at(DAY1);
    RiskKernel::observe_account_view(&mut kernel, &flat(250_000.0, SEC));
    assert!(!RiskKernel::entries_halted(&kernel));
    let opening = RiskKernel::take_control_anchor(&mut kernel).expect("opening anchor");
    assert!(opening.contains("250000"));

    RiskKernel::observe_account_view(&mut kernel, &flat(249_000.0, 2 * SEC));
    assert!(RiskKernel::entries_halted(&kernel));
    let tripped = RiskKernel::take_control_anchor(&mut kernel).expect("trip anchor");
    assert!(tripped.contains("249000"));

    // The halt is still enforced through the ordinary intent verdict, while
    // the engine-level hook can cancel orders that predate this assessment.
    assert!(matches!(
        assess(&mut kernel, 260_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
fn a_non_tripped_anchor_rolls_on_the_next_utc_day() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assess(&mut kernel, 249_500.0, 2 * SEC);

    kernel.observe_wall_clock_ns(utc_noon(DAY2));
    assert_eq!(
        assess(&mut kernel, 249_500.0, 3 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
    assert_eq!(
        kernel.loss_guard_anchor().opening_equity_usdt,
        Some(249_500.0)
    );
    assert_eq!(
        assess(&mut kernel, 248_600.0, 4 * SEC),
        RiskVerdict::Allow { qty: 1.0 },
        "the prior day's loss is outside the new budget"
    );
}

#[test]
fn a_loss_across_midnight_cannot_become_the_new_baseline() {
    let mut kernel = Kernel::new(guarded_config()).expect("config");
    kernel.observe_wall_clock_ns(utc_boundary(DAY2, -1));
    RiskKernel::observe_account_view(&mut kernel, &flat(250_000.0, SEC));

    kernel.observe_wall_clock_ns(utc_boundary(DAY2, 1));
    RiskKernel::observe_account_view(&mut kernel, &flat(249_000.0, 2 * SEC));

    assert!(RiskKernel::entries_halted(&kernel));
    let anchor = kernel.loss_guard_anchor();
    assert_eq!(anchor.day, Some(DAY2));
    assert_eq!(anchor.opening_equity_usdt, Some(250_000.0));
    assert_eq!(
        anchor.trip.expect("cross-boundary trip").equity_usdt,
        249_000.0
    );
}

#[test]
fn durable_pre_midnight_evidence_bridges_a_restart() {
    let mut first = Kernel::new(guarded_config()).expect("config");
    first.observe_wall_clock_ns(utc_boundary(DAY2, -30));
    RiskKernel::observe_account_view(&mut first, &flat(250_000.0, SEC));
    let checkpoint = RiskKernel::take_control_anchor(&mut first).expect("durable checkpoint");

    let mut restarted = Kernel::new(guarded_config()).expect("config");
    RiskKernel::restore_control_anchor(&mut restarted, &checkpoint).expect("valid checkpoint");
    restarted.observe_wall_clock_ns(utc_boundary(DAY2, 5));
    RiskKernel::observe_account_view(&mut restarted, &flat(249_000.0, 2 * SEC));

    assert!(RiskKernel::entries_halted(&restarted));
    assert_eq!(
        restarted.loss_guard_anchor().opening_equity_usdt,
        Some(250_000.0)
    );
}

#[test]
fn a_higher_read_between_periodic_checkpoints_is_durable_before_rollover() {
    let mut first = Kernel::new(guarded_config()).expect("config");
    first.observe_wall_clock_ns(utc_boundary(DAY2, -60));
    RiskKernel::observe_account_view(&mut first, &flat(100_000.0, SEC));
    let _ = RiskKernel::take_control_anchor(&mut first).expect("initial checkpoint");

    first.observe_wall_clock_ns(utc_boundary(DAY2, -30));
    RiskKernel::observe_account_view(&mut first, &flat(102_000.0, 2 * SEC));
    let raised = RiskKernel::take_control_anchor(&mut first)
        .expect("a higher boundary observation must be durable immediately");

    let mut restarted = Kernel::new(guarded_config()).expect("config");
    RiskKernel::restore_control_anchor(&mut restarted, &raised).expect("valid raised checkpoint");
    restarted.observe_wall_clock_ns(utc_boundary(DAY2, 5));
    RiskKernel::observe_account_view(&mut restarted, &flat(101_000.0, 3 * SEC));

    assert!(RiskKernel::entries_halted(&restarted));
    assert_eq!(
        restarted.loss_guard_anchor().opening_equity_usdt,
        Some(102_000.0)
    );
}

#[test]
fn latest_equity_is_checkpointed_at_a_bounded_cadence() {
    let mut kernel = Kernel::new(guarded_config()).expect("config");
    kernel.observe_wall_clock_ns(utc_noon(DAY1));
    RiskKernel::observe_account_view(&mut kernel, &flat(250_000.0, SEC));
    let _ = RiskKernel::take_control_anchor(&mut kernel).expect("opening checkpoint");

    kernel.observe_wall_clock_ns(utc_noon(DAY1) + 30 * SEC);
    RiskKernel::observe_account_view(&mut kernel, &flat(249_900.0, 2 * SEC));
    assert_eq!(RiskKernel::take_control_anchor(&mut kernel), None);

    kernel.observe_wall_clock_ns(utc_noon(DAY1) + 60 * SEC);
    RiskKernel::observe_account_view(&mut kernel, &flat(249_800.0, 3 * SEC));
    let checkpoint = RiskKernel::take_control_anchor(&mut kernel).expect("minute checkpoint");
    assert!(checkpoint.contains("249800"));
}

#[test]
fn a_backwards_utc_day_does_not_refresh_the_budget() {
    let mut kernel = kernel_at(DAY2);
    assess(&mut kernel, 250_000.0, SEC);
    kernel.observe_wall_clock_ns(utc_noon(DAY1));
    assert!(matches!(
        assess(&mut kernel, 249_000.0, 2 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
fn a_tripped_guard_still_allows_a_genuine_exit() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assess(&mut kernel, 249_000.0, 2 * SEC);

    let held = view(
        249_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        3 * SEC,
    );
    assert_eq!(
        kernel.assess(&exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, 3 * SEC), &held),
        RiskVerdict::Allow { qty: 5.0 }
    );
    assert!(kernel.loss_guard_anchor().trip.is_some());
}

#[test]
fn the_control_anchor_preserves_budget_and_trip_across_restart() {
    let mut first = kernel_at(DAY1);
    assert_eq!(RiskKernel::take_control_anchor(&mut first), None);
    assess(&mut first, 250_000.0, SEC);
    let opening = RiskKernel::take_control_anchor(&mut first).expect("opening changed");
    assert_eq!(RiskKernel::take_control_anchor(&mut first), None);

    let mut continued = kernel_at(DAY1);
    RiskKernel::restore_control_anchor(&mut continued, &opening).expect("valid opening anchor");
    assert!(matches!(
        assess(&mut continued, 249_000.0, 2 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
    let tripped = RiskKernel::take_control_anchor(&mut continued).expect("trip changed");

    let mut restarted = kernel_at(DAY1);
    RiskKernel::restore_control_anchor(&mut restarted, &tripped).expect("valid trip anchor");
    assert!(matches!(
        assess(&mut restarted, 260_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped { .. }
        }
    ));
}

#[test]
fn malformed_or_inconsistent_control_anchors_are_rejected() {
    let invalid = [
        "{not-json",
        r#"{"day":1,"opening_equity_usdt":250000.0,"trip":null,"unknown":true}"#,
        r#"{"day":1,"opening_equity_usdt":-1.0,"trip":null}"#,
        r#"{"day":1,"opening_equity_usdt":250000.0,"trip":{"equity_usdt":249000.0,"floor_usdt":123.0}}"#,
        r#"{"day":1,"opening_equity_usdt":null,"trip":{"equity_usdt":249000.0,"floor_usdt":249000.0}}"#,
        r#"{"day":1,"opening_equity_usdt":250000.0,"last_equity_usdt":249500.0,"trip":null}"#,
        r#"{"day":1,"opening_equity_usdt":250000.0,"last_equity_usdt":"nan","last_observed_wall_ns":1,"trip":null}"#,
    ];

    for state in invalid {
        let mut kernel = kernel_at(DAY1);
        assert!(
            RiskKernel::restore_control_anchor(&mut kernel, state).is_err(),
            "accepted invalid state: {state}"
        );
        assert_eq!(kernel.loss_guard_anchor(), LossGuardAnchor::default());
    }
}

#[test]
fn a_zero_equity_trip_survives_restart() {
    let mut first = kernel_at(DAY1);
    RiskKernel::observe_account_view(&mut first, &flat(1_000.0, SEC));
    let _opening = RiskKernel::take_control_anchor(&mut first).expect("opening anchor");

    RiskKernel::observe_account_view(&mut first, &flat(0.0, 2 * SEC));
    assert!(RiskKernel::entries_halted(&first));
    let tripped = RiskKernel::take_control_anchor(&mut first).expect("zero-equity trip");

    let mut restarted = kernel_at(DAY1);
    RiskKernel::restore_control_anchor(&mut restarted, &tripped).expect("valid zero-equity trip");
    assert!(RiskKernel::entries_halted(&restarted));
    assert!(matches!(
        assess(&mut restarted, 5_000.0, 3 * SEC),
        RiskVerdict::Deny {
            reason: DenyReason::LossGuardTripped {
                equity_usdt: 0.0,
                floor_usdt: 0.0,
            }
        }
    ));
}

#[test]
fn a_zero_first_reading_does_not_poison_the_opening_anchor() {
    let mut kernel = kernel_at(DAY1);
    RiskKernel::observe_account_view(&mut kernel, &flat(0.0, SEC));
    assert_eq!(kernel.loss_guard_anchor().opening_equity_usdt, None);
    assert!(!RiskKernel::entries_halted(&kernel));

    RiskKernel::observe_account_view(&mut kernel, &flat(250_000.0, 2 * SEC));
    assert_eq!(
        kernel.loss_guard_anchor().opening_equity_usdt,
        Some(250_000.0)
    );
    assert!(!RiskKernel::entries_halted(&kernel));
}

#[test]
fn explicit_reset_is_the_only_way_to_clear_a_trip() {
    let mut kernel = kernel_at(DAY1);
    assess(&mut kernel, 250_000.0, SEC);
    assess(&mut kernel, 249_000.0, 2 * SEC);
    kernel.reset_loss_guard();

    assert_eq!(
        assess(&mut kernel, 249_000.0, 3 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
    assert_eq!(
        kernel.loss_guard_anchor().opening_equity_usdt,
        Some(249_000.0)
    );

    let mut restarted = kernel_at(DAY1);
    RiskKernel::restore_control_anchor(&mut restarted, &cleared_loss_guard_state())
        .expect("valid cleared anchor");
    assert_eq!(
        assess(&mut restarted, 249_000.0, 4 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
fn a_disabled_or_invalid_ceiling_has_explicit_behavior() {
    let mut disabled = Kernel::new(demo_config()).expect("disabled config");
    disabled.observe_wall_clock_ns(utc_noon(DAY1));
    assess(&mut disabled, 250_000.0, SEC);
    assert_eq!(RiskKernel::take_control_anchor(&mut disabled), None);
    assert_eq!(
        assess(&mut disabled, 1_000.0, 2 * SEC),
        RiskVerdict::Allow { qty: 1.0 }
    );

    for value in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let cfg = KernelConfig {
            loss_guard: LossGuardConfig {
                max_daily_loss_usdt: Some(value),
            },
            ..demo_config()
        };
        let err = Kernel::new(cfg).err().expect("must refuse invalid ceiling");
        assert!(err.detail.contains("max_daily_loss_usdt"), "{}", err.detail);
    }
}
