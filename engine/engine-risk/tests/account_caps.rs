//! The account-wide capital caps, against the same rules as the account-level
//! checks in account_kernel.py: `symbol_notional_limit`,
//! `component_gross_limit`, `initial_margin_limit`, and the pair
//! `negative_available_margin` / `available_margin_limit`.
//!
//! The shape here is configs/operational.mainnet.json: a 100 USDT reference,
//! 175 of account gross, 50 on any one symbol, 100 of margin, at leverage 2,
//! partitioned carry 100 / long 75 by gross. Every cap is checked just under,
//! just over, and after the capital reference has moved.

mod common;

use common::*;
use engine_risk::{
    EnvelopeConfig, Kernel, KernelConfig, LossGuardConfig, PartitionConfig, StrategyAllocation,
};
use engine_types::orders::Side;
use engine_types::risk::{AccountView, DenyReason, PositionView, RiskKernel, RiskVerdict};

const NOW: u64 = SEC;

/// configs/operational.mainnet.json, key for key.
fn mainnet_config() -> KernelConfig {
    KernelConfig {
        max_account_view_age_ns: MAX_VIEW_AGE_NS,
        loss_guard: LossGuardConfig {
            max_daily_loss_usdt: Some(10.0),
        },
        envelope: EnvelopeConfig {
            tracks_equity: true,
            reference_usdt: 100.0,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            // 175 of account gross over a 100 reference.
            gross_notional_multiple: 1.75,
            disaster_stop_fraction: DISASTER_STOP_FRACTION,
            max_symbol_notional_usdt: 50.0,
            max_component_gross_notional_usdt: 175.0,
            max_initial_margin_usdt: 100.0,
        },
        partition: PartitionConfig {
            allocations: vec![
                StrategyAllocation {
                    strategy: CARRY,
                    max_gross_notional_usdt: 100.0,
                    max_initial_margin_usdt: 57.14285714285714,
                },
                StrategyAllocation {
                    strategy: LONG,
                    max_gross_notional_usdt: 75.0,
                    max_initial_margin_usdt: 42.857142857142854,
                },
            ],
            leverage: 2.0,
            min_order_notional_usdt: MIN_ORDER_NOTIONAL_USDT,
        },
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
// 1. max_symbol_notional_usdt
// --------------------------------------------------------------------------

#[test]
// account_kernel.py:3212. One strategy owns a symbol, so its own share is all
// that stops it putting everything on one name; this is the cap that does.
fn a_symbol_exactly_at_its_cap_is_allowed() {
    let mut k = kernel(mainnet_config());
    assert_eq!(
        k.assess(&buy(5.0), &flat(100.0, NOW)),
        RiskVerdict::Allow { qty: 5.0 },
        "50 USDT on one symbol is exactly the cap"
    );
}

#[test]
fn a_symbol_one_step_over_its_cap_is_refused() {
    let mut k = kernel(mainnet_config());
    match deny(k.assess(&buy(5.001), &flat(100.0, NOW))) {
        DenyReason::SymbolNotionalBreached {
            symbol,
            notional_usdt,
            cap_usdt,
        } => {
            assert_eq!(symbol, BUSDT, "the refusal names the symbol");
            assert!((notional_usdt - 50.01).abs() < 1e-9, "got {notional_usdt}");
            assert!((cap_usdt - 50.0).abs() < 1e-9, "got {cap_usdt}");
        }
        other => panic!("expected a symbol cap breach, got {other:?}"),
    }
}

#[test]
// The position already held counts: the cap is on the symbol, not the order.
fn what_the_symbol_already_holds_counts_against_its_cap() {
    let mut k = kernel(mainnet_config());
    let held = view(
        100.0,
        vec![position(BUSDT, Side::Buy, 4.0, 10.0, true)],
        NOW,
    );
    assert_eq!(
        k.assess(&buy(1.0), &held),
        RiskVerdict::Allow { qty: 1.0 },
        "40 held plus 10 asked is exactly the 50 cap"
    );
    assert!(matches!(
        deny(k.assess(&buy(1.1), &held)),
        DenyReason::SymbolNotionalBreached { .. }
    ));
}

#[test]
// account_kernel.py checks every symbol in the projected book, not only the
// ones the batch names, so a symbol already over its cap stops new risk
// anywhere until it is brought back.
fn a_different_symbol_over_its_cap_refuses_the_entry_and_names_that_symbol() {
    let mut k = kernel(mainnet_config());
    let held = view(
        100.0,
        vec![position(CUSDT, Side::Buy, 6.0, 10.0, true)],
        NOW,
    );
    match deny(k.assess(&buy(1.0), &held)) {
        DenyReason::SymbolNotionalBreached {
            symbol,
            notional_usdt,
            ..
        } => {
            assert_eq!(symbol, CUSDT, "the operator needs the symbol that is over");
            assert!((notional_usdt - 60.0).abs() < 1e-9, "got {notional_usdt}");
        }
        other => panic!("expected a symbol cap breach, got {other:?}"),
    }
}

#[test]
// profile_at_capital_reference rescales max_symbol_notional_usdt with the
// reference, so a wallet that doubles doubles what one symbol may carry.
fn the_symbol_cap_follows_the_capital_reference() {
    let mut small = kernel(mainnet_config());
    assert!(matches!(
        deny(small.assess(&buy(6.0), &flat(100.0, NOW))),
        DenyReason::SymbolNotionalBreached { .. }
    ));

    let mut doubled = kernel(mainnet_config());
    assert_eq!(
        doubled.assess(&buy(6.0), &flat(200.0, NOW)),
        RiskVerdict::Allow { qty: 6.0 },
        "at a 200 reference the same 60 USDT sits inside a 100 cap"
    );
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

/// Unpartitioned, with the account margin cap set below what the gross cap
/// funds — otherwise the envelope allowance reaches the same book first.
fn margin_capped_config() -> KernelConfig {
    let mut cfg = mainnet_config();
    cfg.envelope.max_symbol_notional_usdt = 175.0;
    cfg.envelope.max_initial_margin_usdt = 40.0;
    cfg.partition.allocations = Vec::new();
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
    let mut k = kernel(mainnet_config());
    // LONG's share funds 75; this asks 60, which the share would allow whole
    // and the 50 symbol cap will not.
    let over = entry(LONG, BUSDT, Side::Buy, 6.0, 10.0, 6.5, NOW);
    assert!(matches!(
        deny(k.assess(&over, &flat(100.0, NOW))),
        DenyReason::SymbolNotionalBreached { .. }
    ));
}

#[test]
// Risk-reducing orders flow past every cap here, exactly as a risk-reducing
// batch skips them all in account_kernel.py.
fn an_exit_is_never_blocked_by_the_account_caps() {
    let mut cfg = mainnet_config();
    cfg.envelope.max_symbol_notional_usdt = 1.0;
    cfg.envelope.max_component_gross_notional_usdt = 1.0;
    cfg.envelope.max_initial_margin_usdt = 1.0;
    // The partition's margin shares would not fit a 1 USDT account cap, and
    // the shares are not what this case is about.
    cfg.partition.allocations = Vec::new();
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
    let mut symbol_over_gross = mainnet_config();
    symbol_over_gross.envelope.max_symbol_notional_usdt = 176.0;
    assert!(Kernel::new(symbol_over_gross)
        .err()
        .expect("must refuse")
        .detail
        .contains("max_symbol_notional_usdt cannot exceed"));

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
// _parse_sleeve_limits proves the margin shares sum inside the declared
// account margin cap. The mainnet shares sum to exactly it.
fn a_partition_whose_margin_shares_exceed_the_account_cap_is_refused() {
    let mut cfg = mainnet_config();
    cfg.envelope.max_initial_margin_usdt = 99.0;
    assert!(Kernel::new(cfg)
        .err()
        .expect("must refuse")
        .detail
        .contains("partition margin shares sum above max_initial_margin_usdt"));
}

#[test]
fn a_cap_that_is_not_a_positive_number_is_refused() {
    for (name, mut cfg) in [
        ("max_symbol_notional_usdt", mainnet_config()),
        ("max_component_gross_notional_usdt", mainnet_config()),
        ("max_initial_margin_usdt", mainnet_config()),
    ] {
        match name {
            "max_symbol_notional_usdt" => cfg.envelope.max_symbol_notional_usdt = 0.0,
            "max_component_gross_notional_usdt" => {
                cfg.envelope.max_component_gross_notional_usdt = f64::NAN
            }
            _ => cfg.envelope.max_initial_margin_usdt = -1.0,
        }
        let detail = Kernel::new(cfg).err().expect("must refuse").detail;
        assert!(detail.contains(name), "{name}: got {detail}");
    }
}
