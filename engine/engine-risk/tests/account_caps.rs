//! The account-wide capital caps, against the same rules as the account-level
//! checks in account_kernel.py: `component_gross_limit`,
//! `initial_margin_limit`, and the pair `negative_available_margin` /
//! `available_margin_limit`.
//!
//! The shape here is chosen so every cap binds in turn and none hides behind
//! another: a 100 USDT reference, 175 of account gross, 100 of margin, at
//! leverage 2, partitioned carry 100 / long 75 by gross. Each cap is checked
//! just under, just over, and after the capital reference has moved. Fidelity
//! to the shipped profiles is a different test's job — operational_profile.rs
//! reads the real files.
//!
//! Nothing here bounds one symbol on its own. The sleeve's own gross share is
//! what stops a single name, and section 1 holds that to be true.

mod common;

use common::*;
use engine_risk::{EnvelopeConfig, Kernel, KernelConfig};
use engine_types::orders::Side;
use engine_types::risk::{AccountView, DenyReason, PositionView, RiskKernel, RiskVerdict};

const NOW: u64 = SEC;

/// The equity-anchored shape, at the numbers this file's boundaries need.
fn mainnet_config() -> KernelConfig {
    KernelConfig {
        max_account_view_age_ns: MAX_VIEW_AGE_NS,
        envelope: EnvelopeConfig {
            tracks_equity: true,
            reference_usdt: 100.0,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            // 175 of account gross over a 100 reference.
            gross_notional_multiple: 1.75,
            disaster_stop_fraction: DISASTER_STOP_FRACTION,
            max_component_gross_notional_usdt: 175.0,
            max_initial_margin_usdt: 100.0,
        },
        leverage: 2.0,
        qty_tolerance: QTY_TOLERANCE,
    }
}

fn kernel(cfg: KernelConfig) -> Kernel {
    Kernel::new(cfg).expect("config")
}

/// A buy at 10.0 with its stop at the disaster distance, so the envelope
/// charges it the same as everything already held and never fires first.
fn buy(qty: f64) -> engine_types::orders::Intent {
    entry(CARRY, BUSDT, Side::Buy, qty, 10.0, 6.5, NOW)
}

fn deny(verdict: RiskVerdict) -> DenyReason {
    match verdict {
        RiskVerdict::Deny { reason } => reason,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

/// An account view with a chosen spare margin rather than the equity default.
fn view_with_available(
    equity_usdt: f64,
    available_usdt: f64,
    positions: Vec<PositionView>,
) -> AccountView {
    AccountView {
        equity_usdt,
        available_usdt,
        positions,
        observed_ns: NOW,
    }
}

// --------------------------------------------------------------------------
// 1. Nothing caps one symbol
// --------------------------------------------------------------------------

#[test]
// One name may carry the whole book. On this shape that is 175 USDT against a
// 100 USDT reference — the concentration the retired per-symbol cap used to
// refuse at 50.
fn one_symbol_may_carry_the_whole_book() {
    let mut k = kernel(mainnet_config());
    assert_eq!(
        k.assess(&buy(17.5), &flat(100.0, NOW)),
        RiskVerdict::Allow { qty: 17.5 }
    );
}

#[test]
// And what bounds it is a whole-book control, reached by the whole book: on
// this shape the envelope's allowance and the account gross ceiling both sit
// at 175, and the envelope is evaluated first.
fn what_bounds_one_symbol_is_a_whole_book_control() {
    let mut k = kernel(mainnet_config());
    assert!(matches!(
        k.assess(&buy(17.6), &flat(100.0, NOW)),
        RiskVerdict::Deny {
            reason: DenyReason::EnvelopeBreached { .. }
        }
    ));
}

#[test]
// What the symbol already holds is still counted — into the book's gross, not
// into any ceiling of its own.
fn what_a_symbol_already_holds_counts_only_toward_the_book() {
    let mut k = kernel(mainnet_config());
    let held = view(
        100.0,
        vec![position(BUSDT, Side::Buy, 9.0, 10.0, true)],
        NOW,
    );
    assert!(matches!(
        deny(k.assess(&buy(9.0), &held)),
        DenyReason::EnvelopeBreached { .. }
    ));
}

// --------------------------------------------------------------------------
// 2. The second account-wide gross ceiling
// --------------------------------------------------------------------------

/// The same shape with the second gross ceiling set below the account gross
/// cap. Shipped equal in both Python profiles, where it therefore never binds.
fn split_gross_config() -> KernelConfig {
    let mut cfg = mainnet_config();
    cfg.envelope.max_component_gross_notional_usdt = 80.0;
    cfg
}

#[test]
// account_kernel.py:3121. Spread across two symbols so neither symbol cap
// fires first.
fn a_book_exactly_at_the_second_gross_ceiling_is_allowed() {
    let mut k = kernel(split_gross_config());
    let held = view(
        100.0,
        vec![position(CUSDT, Side::Buy, 4.5, 10.0, true)],
        NOW,
    );
    assert_eq!(
        k.assess(&buy(3.5), &held),
        RiskVerdict::Allow { qty: 3.5 },
        "45 held plus 35 asked is exactly the 80 ceiling"
    );
}

#[test]
fn a_book_one_step_over_the_second_gross_ceiling_is_refused() {
    let mut k = kernel(split_gross_config());
    let held = view(
        100.0,
        vec![position(CUSDT, Side::Buy, 4.5, 10.0, true)],
        NOW,
    );
    match deny(k.assess(&buy(4.0), &held)) {
        DenyReason::ComponentGrossBreached {
            gross_usdt,
            cap_usdt,
        } => {
            assert!((gross_usdt - 85.0).abs() < 1e-9, "got {gross_usdt}");
            assert!((cap_usdt - 80.0).abs() < 1e-9, "got {cap_usdt}");
        }
        other => panic!("expected a gross ceiling breach, got {other:?}"),
    }
}

#[test]
fn the_second_gross_ceiling_follows_the_capital_reference() {
    let mut small = kernel(split_gross_config());
    let held_at_100 = view(
        100.0,
        vec![position(CUSDT, Side::Buy, 4.5, 10.0, true)],
        NOW,
    );
    assert!(matches!(
        deny(small.assess(&buy(4.0), &held_at_100)),
        DenyReason::ComponentGrossBreached { .. }
    ));

    let mut doubled = kernel(split_gross_config());
    let held_at_200 = view(
        200.0,
        vec![position(CUSDT, Side::Buy, 4.5, 10.0, true)],
        NOW,
    );
    assert_eq!(
        doubled.assess(&buy(4.0), &held_at_200),
        RiskVerdict::Allow { qty: 4.0 },
        "at a 200 reference the same 85 USDT sits inside a 160 ceiling"
    );
}

// --------------------------------------------------------------------------
// 3. max_initial_margin_usdt at account level
// --------------------------------------------------------------------------

/// The account margin cap set below what the gross cap funds — otherwise the
/// envelope allowance reaches the same book first.
fn margin_capped_config() -> KernelConfig {
    let mut cfg = mainnet_config();
    cfg.envelope.max_initial_margin_usdt = 40.0;
    cfg
}

#[test]
// account_kernel.py:3125. 80 USDT of gross at leverage 2 is 40 of margin.
fn a_book_exactly_at_the_account_margin_cap_is_allowed() {
    let mut k = kernel(margin_capped_config());
    assert_eq!(
        k.assess(&buy(8.0), &flat(100.0, NOW)),
        RiskVerdict::Allow { qty: 8.0 }
    );
}

#[test]
fn a_book_one_step_over_the_account_margin_cap_is_refused() {
    let mut k = kernel(margin_capped_config());
    match deny(k.assess(&buy(8.2), &flat(100.0, NOW))) {
        DenyReason::InitialMarginBreached {
            margin_usdt,
            cap_usdt,
        } => {
            assert!((margin_usdt - 41.0).abs() < 1e-9, "got {margin_usdt}");
            assert!((cap_usdt - 40.0).abs() < 1e-9, "got {cap_usdt}");
        }
        other => panic!("expected a margin cap breach, got {other:?}"),
    }
}

#[test]
fn the_account_margin_cap_follows_the_capital_reference() {
    let mut small = kernel(margin_capped_config());
    assert!(matches!(
        deny(small.assess(&buy(8.2), &flat(100.0, NOW))),
        DenyReason::InitialMarginBreached { .. }
    ));

    let mut doubled = kernel(margin_capped_config());
    assert_eq!(
        doubled.assess(&buy(8.2), &flat(200.0, NOW)),
        RiskVerdict::Allow { qty: 8.2 },
        "at a 200 reference the same 41 of margin sits inside an 80 cap"
    );
}

// --------------------------------------------------------------------------
// 4. The available-margin increase test
// --------------------------------------------------------------------------

#[test]
// account_kernel.py:3145. 40 USDT of gross at leverage 2 needs 20 of margin.
fn an_increase_the_spare_margin_exactly_covers_is_allowed() {
    let mut k = kernel(mainnet_config());
    assert_eq!(
        k.assess(&buy(4.0), &view_with_available(100.0, 20.0, Vec::new())),
        RiskVerdict::Allow { qty: 4.0 }
    );
}

#[test]
fn an_increase_larger_than_the_spare_margin_is_refused() {
    let mut k = kernel(mainnet_config());
    match deny(k.assess(&buy(4.002), &view_with_available(100.0, 20.0, Vec::new()))) {
        DenyReason::AvailableMarginExhausted {
            additional_margin_usdt,
            available_usdt,
        } => {
            assert!(
                (additional_margin_usdt - 20.01).abs() < 1e-9,
                "got {additional_margin_usdt}"
            );
            assert!((available_usdt - 20.0).abs() < 1e-9, "got {available_usdt}");
        }
        other => panic!("expected a spare margin refusal, got {other:?}"),
    }
}

#[test]
// account_kernel.py:3022 refuses a non-risk-reducing batch outright on a
// negative reading. Here one order is the whole increase and its margin is
// always positive, so the increase test above already refuses it.
fn a_negative_spare_margin_refuses_every_entry() {
    let mut k = kernel(mainnet_config());
    match deny(k.assess(&buy(1.0), &view_with_available(100.0, -5.0, Vec::new()))) {
        DenyReason::AvailableMarginExhausted {
            additional_margin_usdt,
            available_usdt,
        } => {
            assert!((additional_margin_usdt - 5.0).abs() < 1e-9);
            assert!((available_usdt + 5.0).abs() < 1e-9);
        }
        other => panic!("expected a spare margin refusal, got {other:?}"),
    }
}

#[test]
// The owner hand-trading the account drives spare margin negative in ordinary
// operation. That must never block getting out.
fn a_negative_spare_margin_still_lets_an_exit_through() {
    let mut k = kernel(mainnet_config());
    let held = view_with_available(
        100.0,
        -5.0,
        vec![position(BUSDT, Side::Buy, 2.0, 10.0, true)],
    );
    assert_eq!(
        k.assess(&exit(CARRY, BUSDT, Side::Sell, 2.0, 10.0, NOW), &held),
        RiskVerdict::Allow { qty: 2.0 }
    );
}

#[test]
// Spare margin is what is left AFTER the standing book's margin is deducted,
// so only the increase is new money. Charging the whole projected book against
// it counts the standing book twice and halves the account.
fn only_the_increase_is_charged_against_spare_margin_not_the_whole_book() {
    let mut k = kernel(mainnet_config());
    // 45 USDT already held is 22.5 of margin, more than the 20 spare on its
    // own. The 40 USDT asked needs 20, which the spare exactly covers.
    let held = view_with_available(
        100.0,
        20.0,
        vec![position(CUSDT, Side::Buy, 4.5, 10.0, true)],
    );
    assert_eq!(
        k.assess(&buy(4.0), &held),
        RiskVerdict::Allow { qty: 4.0 },
        "the standing book is already paid for"
    );

    // And one step more is refused for the order's own margin, not the book's.
    match deny(k.assess(&buy(4.002), &held)) {
        DenyReason::AvailableMarginExhausted {
            additional_margin_usdt,
            ..
        } => assert!(
            (additional_margin_usdt - 20.01).abs() < 1e-9,
            "the whole book would have been 42.51; got {additional_margin_usdt}"
        ),
        other => panic!("expected a spare margin refusal, got {other:?}"),
    }
}

// --------------------------------------------------------------------------
// Order of evaluation, and what exits skip
// --------------------------------------------------------------------------

#[test]
// The envelope charges a wide stop more than its notional, so it must keep
// speaking first: the caps below it are plain notional and margin ceilings.
fn an_envelope_breach_is_reported_before_the_account_caps() {
    let mut k = kernel(mainnet_config());
    // 100 USDT of notional with a stop the whole way to zero: 100 of worst
    // case against a 61.25 allowance, while the symbol cap is only 50.
    let wide = entry(CARRY, BUSDT, Side::Buy, 10.0, 10.0, 0.01, NOW);
    assert!(matches!(
        deny(k.assess(&wide, &flat(100.0, NOW))),
        DenyReason::EnvelopeBreached { .. }
    ));
}

#[test]
// The account caps refuse, as the Python kernel refuses the whole batch. The
// partition clamps, so it stays the last word on size and comes after them.
fn an_account_cap_is_reported_before_the_partition_clamps() {
    let mut cfg = mainnet_config();
    // Below LONG's own 75 share, so the book's ceiling is reached first.
    cfg.envelope.max_component_gross_notional_usdt = 60.0;
    let mut k = kernel(cfg);
    // LONG's share funds 75; this asks 70, which the share would allow whole
    // and the book's 60 ceiling will not.
    let over = entry(LONG, BUSDT, Side::Buy, 7.0, 10.0, 6.5, NOW);
    assert!(matches!(
        deny(k.assess(&over, &flat(100.0, NOW))),
        DenyReason::ComponentGrossBreached { .. }
    ));
}

#[test]
// Risk-reducing orders flow past every cap here, exactly as a risk-reducing
// batch skips them all in account_kernel.py.
fn an_exit_is_never_blocked_by_the_account_caps() {
    let mut cfg = mainnet_config();
    cfg.envelope.max_component_gross_notional_usdt = 1.0;
    cfg.envelope.max_initial_margin_usdt = 1.0;
    let mut k = kernel(cfg);
    let held = view_with_available(
        100.0,
        0.0,
        vec![position(BUSDT, Side::Buy, 6.0, 10.0, true)],
    );
    assert_eq!(
        k.assess(&exit(CARRY, BUSDT, Side::Sell, 6.0, 10.0, NOW), &held),
        RiskVerdict::Allow { qty: 6.0 }
    );
}

// --------------------------------------------------------------------------
// The load-time proofs, from operational_profile.py
// --------------------------------------------------------------------------

#[test]
// operational_profile.py:296-299 and :416. Caps that do not nest describe a
// book nobody can reach.
fn a_config_whose_caps_do_not_nest_is_refused() {
    let mut gross_over_account = mainnet_config();
    gross_over_account.envelope.max_component_gross_notional_usdt = 176.0;
    assert!(Kernel::new(gross_over_account)
        .err()
        .expect("must refuse")
        .detail
        .contains("max_component_gross_notional_usdt cannot exceed"));

    let mut margin_over_reference = mainnet_config();
    margin_over_reference.envelope.max_initial_margin_usdt = 101.0;
    assert!(Kernel::new(margin_over_reference)
        .err()
        .expect("must refuse")
        .detail
        .contains("max_initial_margin_usdt cannot exceed"));
}

#[test]
fn a_cap_that_is_not_a_positive_number_is_refused() {
    for (name, mut cfg) in [
        ("max_component_gross_notional_usdt", mainnet_config()),
        ("max_initial_margin_usdt", mainnet_config()),
    ] {
        match name {
            "max_component_gross_notional_usdt" => {
                cfg.envelope.max_component_gross_notional_usdt = f64::NAN
            }
            _ => cfg.envelope.max_initial_margin_usdt = -1.0,
        }
        let detail = Kernel::new(cfg).err().expect("must refuse").detail;
        assert!(detail.contains(name), "{name}: got {detail}");
    }
}
