//! The rolling loss window: what this engine's own closed round trips, net of
//! venue fees, cost inside a day, and what that stops.
//!
//! demo_config() puts the capital reference at 250_000 and
//! max_rolling_loss_fraction at 0.1, so every table here is against a 25_000
//! limit.

mod common;

use common::*;
use engine_risk::{Kernel, KernelConfig, ROLLING_LOSS_WINDOW_MS};
use engine_types::orders::Side;
use engine_types::risk::{ClosedTradeRow, DenyReason, RiskKernel, RiskVerdict, RollingLossView};

const NOW: u64 = SEC;
/// A venue wall clock in milliseconds. Only the distances from it matter.
const CLOSED_MS: i64 = 1_700_000_000_000;
const LIMIT_USDT: f64 = 25_000.0;

fn kernel() -> Kernel {
    Kernel::new(demo_config()).expect("config")
}

fn closed(closed_ms: i64, net_usdt: f64) -> ClosedTradeRow {
    ClosedTradeRow {
        closed_ms,
        net_usdt,
    }
}

/// One small entry: 1.0 at 10.0, far under every other cap, so only the
/// rolling loss window can refuse it.
fn assess_entry(kernel: &mut Kernel, equity_usdt: f64) -> RiskVerdict {
    kernel.assess(
        &entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, NOW),
        &flat(equity_usdt, NOW),
    )
}

fn allowed(verdict: RiskVerdict) -> bool {
    verdict == RiskVerdict::Allow { qty: 1.0 }
}

fn tripped(verdict: RiskVerdict) -> bool {
    matches!(
        verdict,
        RiskVerdict::Deny {
            reason: DenyReason::RollingLossTripped { .. }
        }
    )
}

#[test]
fn a_fresh_kernel_reports_an_empty_window() {
    let kernel = kernel();
    assert_eq!(
        RiskKernel::rolling_loss(&kernel).expect("this kernel keeps a window"),
        RollingLossView {
            window_ms: ROLLING_LOSS_WINDOW_MS,
            trades: 0,
            net_usdt: 0.0,
            limit_usdt: LIMIT_USDT,
            tripped: false,
        }
    );
    assert!(RiskKernel::rolling_loss_rows(&kernel).is_empty());
}

#[test]
fn one_cent_inside_the_limit_enters_and_one_cent_past_it_does_not() {
    let mut kernel = kernel();
    kernel.observe_closed_trade(closed(CLOSED_MS, -24_999.99));
    assert!(allowed(assess_entry(&mut kernel, 250_000.0)));

    kernel.observe_closed_trade(closed(CLOSED_MS + 1, -0.02));
    let RiskVerdict::Deny {
        reason:
            DenyReason::RollingLossTripped {
                window_net_usdt,
                limit_usdt,
                window_ms,
            },
    } = assess_entry(&mut kernel, 250_000.0)
    else {
        panic!("a window past its limit must refuse the entry");
    };
    assert!(
        (window_net_usdt - -25_000.01).abs() < 1e-6,
        "{window_net_usdt}"
    );
    assert_eq!(limit_usdt, LIMIT_USDT);
    assert_eq!(window_ms, ROLLING_LOSS_WINDOW_MS);
}

#[test]
fn a_genuine_exit_still_passes_while_the_window_is_tripped() {
    let mut kernel = kernel();
    kernel.observe_closed_trade(closed(CLOSED_MS, -30_000.0));
    assert!(tripped(assess_entry(&mut kernel, 250_000.0)));

    let held = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        NOW,
    );
    assert_eq!(
        kernel.assess(&exit(CARRY, BUSDT, Side::Sell, 5.0, 10.0, NOW), &held),
        RiskVerdict::Allow { qty: 5.0 },
        "taking risk off must not wait on the loss window"
    );
}

#[test]
fn the_window_is_net_of_wins_not_a_sum_of_losses() {
    let mut kernel = kernel();
    kernel.observe_closed_trade(closed(CLOSED_MS, -30_000.0));
    assert!(tripped(assess_entry(&mut kernel, 250_000.0)));

    kernel.observe_closed_trade(closed(CLOSED_MS + 1, 10_000.0));
    assert_eq!(kernel.rolling_loss().net_usdt, -20_000.0);
    assert!(allowed(assess_entry(&mut kernel, 250_000.0)));
}

#[test]
fn a_trade_one_millisecond_inside_the_window_still_counts() {
    let mut kernel = kernel();
    kernel.observe_closed_trade(closed(CLOSED_MS, -30_000.0));
    kernel.observe_wall_clock_ms(CLOSED_MS + ROLLING_LOSS_WINDOW_MS - 1);
    assert_eq!(kernel.rolling_loss().trades, 1);
    assert!(tripped(assess_entry(&mut kernel, 250_000.0)));
}

#[test]
fn a_trade_exactly_one_window_old_is_out_and_entries_resume() {
    for age_ms in [ROLLING_LOSS_WINDOW_MS, ROLLING_LOSS_WINDOW_MS + 1] {
        let mut kernel = kernel();
        kernel.observe_closed_trade(closed(CLOSED_MS, -30_000.0));
        kernel.observe_wall_clock_ms(CLOSED_MS + age_ms);
        let window = kernel.rolling_loss();
        assert_eq!(window.trades, 0, "{age_ms}");
        assert_eq!(window.net_usdt, 0.0, "{age_ms}");
        assert!(!window.tripped, "{age_ms}");
        assert!(allowed(assess_entry(&mut kernel, 250_000.0)), "{age_ms}");
    }
}

#[test]
fn an_older_clock_reading_does_not_re_open_a_drained_window() {
    let mut kernel = kernel();
    kernel.observe_closed_trade(closed(CLOSED_MS, -30_000.0));
    kernel.observe_wall_clock_ms(CLOSED_MS + ROLLING_LOSS_WINDOW_MS);
    assert_eq!(kernel.rolling_loss().trades, 0);

    kernel.observe_wall_clock_ms(CLOSED_MS + 1);
    assert_eq!(kernel.rolling_loss().trades, 0);
    assert!(allowed(assess_entry(&mut kernel, 250_000.0)));
}

#[test]
fn a_restart_that_restores_its_closed_trades_stays_tripped() {
    let mut kernel = kernel();
    let stored = vec![
        closed(CLOSED_MS - 2 * ROLLING_LOSS_WINDOW_MS, -99_000.0),
        closed(CLOSED_MS, -30_000.0),
    ];
    let booting: &mut dyn RiskKernel = &mut kernel;
    booting.restore_rolling_loss_rows(&stored);
    booting.observe_wall_clock_ms(CLOSED_MS + 1_000);
    assert_eq!(
        booting.rolling_loss_rows(),
        vec![closed(CLOSED_MS, -30_000.0)],
        "a trade already outside the window must not be restored into it"
    );
    assert!(
        booting
            .rolling_loss()
            .expect("this kernel keeps a window")
            .tripped
    );
    assert!(tripped(assess_entry(&mut kernel, 250_000.0)));
}

#[test]
fn restoring_sets_the_window_rather_than_adding_to_it() {
    let mut kernel = kernel();
    kernel.observe_closed_trade(closed(CLOSED_MS, -30_000.0));
    kernel.restore_rolling_loss_rows(&[closed(CLOSED_MS + 1, -1_000.0)]);
    assert_eq!(
        kernel.rolling_loss_rows(),
        vec![closed(CLOSED_MS + 1, -1_000.0)]
    );
    assert!(allowed(assess_entry(&mut kernel, 250_000.0)));
}

#[test]
fn a_fall_in_equity_contracts_the_limit_and_trips_the_window() {
    let mut tracking = Kernel::new(equity_tracking_config()).expect("config");
    tracking.observe_closed_trade(closed(CLOSED_MS, -20_000.0));
    assert!(allowed(assess_entry(&mut tracking, 250_000.0)));

    let RiskVerdict::Deny {
        reason: DenyReason::RollingLossTripped { limit_usdt, .. },
    } = assess_entry(&mut tracking, 100_000.0)
    else {
        panic!("a 20_000 loss must trip a limit that has fallen to 10_000");
    };
    assert_eq!(limit_usdt, 10_000.0);

    // The same fall against a pinned reference leaves the limit where it was.
    let mut pinned = kernel();
    pinned.observe_closed_trade(closed(CLOSED_MS, -20_000.0));
    assert!(allowed(assess_entry(&mut pinned, 100_000.0)));
}

#[test]
fn an_unreadable_closed_trade_is_ignored() {
    let mut kernel = kernel();
    for row in [
        closed(CLOSED_MS, f64::NAN),
        closed(CLOSED_MS, f64::NEG_INFINITY),
        closed(0, -99_000.0),
        closed(-1, -99_000.0),
    ] {
        kernel.observe_closed_trade(row);
    }
    assert_eq!(kernel.rolling_loss().trades, 0);
    assert!(kernel.rolling_loss_rows().is_empty());
    assert!(allowed(assess_entry(&mut kernel, 250_000.0)));

    kernel.restore_rolling_loss_rows(&[closed(0, -99_000.0), closed(CLOSED_MS, -1_000.0)]);
    assert_eq!(
        kernel.rolling_loss_rows(),
        vec![closed(CLOSED_MS, -1_000.0)]
    );
}

#[test]
fn the_config_refuses_a_rolling_loss_fraction_outside_its_range() {
    for fraction in [0.0, -0.1, 1.0 + f64::EPSILON, 1.5, f64::NAN] {
        let cfg = KernelConfig {
            max_rolling_loss_fraction: fraction,
            ..demo_config()
        };
        assert!(cfg.validate().is_err(), "{fraction} was accepted");
    }
    let cfg = KernelConfig {
        max_rolling_loss_fraction: 1.0,
        ..demo_config()
    };
    assert!(cfg.validate().is_ok());
}
