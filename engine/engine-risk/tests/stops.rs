//! The stop-attach discipline: the decision half of venue_protection.py and of
//! the entry-protection reducer in account_kernel.py.
//!
//! Baselines from tests/venue/test_venue_protection.py: reference price 10.0,
//! fallback stop fraction 0.07, so a long stop sits at 9.3 and a short at 10.7.

mod common;

use common::*;
use engine_risk::Kernel;
use engine_types::orders::{Side, StopSpec};
use engine_types::risk::{DenyReason, RiskKernel, RiskVerdict};

const NOW: u64 = SEC;

fn kernel() -> Kernel {
    Kernel::new(demo_config()).expect("config")
}

fn denied_for_missing_stop(verdict: RiskVerdict) -> bool {
    verdict
        == RiskVerdict::Deny {
            reason: DenyReason::MissingStop,
        }
}

#[test]
// test_bybit_demo_adapter_refuses_naked_entry_before_any_mutation:
// Buy 0.1 at reference 10.0 with no stop fields.
fn a_naked_entry_is_refused() {
    let mut kernel = kernel();
    let intent = naked_entry(CARRY, BUSDT, Side::Buy, 0.1, 10.0, NOW);
    assert!(denied_for_missing_stop(
        kernel.assess(&intent, &flat(250_000.0, NOW))
    ));
}

#[test]
// The fallback baselines: 10 * 0.93 for a long, 10 * 1.07 for a short.
fn an_entry_carrying_its_stop_is_allowed_on_both_sides() {
    let mut kernel = kernel();
    let long = entry(CARRY, BUSDT, Side::Buy, 2.0, 10.0, 9.3, NOW);
    assert_eq!(
        kernel.assess(&long, &flat(250_000.0, NOW)),
        RiskVerdict::Allow { qty: 2.0 }
    );

    let short = entry(CARRY, CUSDT, Side::Sell, 2.0, 10.0, 10.7, NOW);
    assert_eq!(
        kernel.assess(&short, &flat(250_000.0, NOW)),
        RiskVerdict::Allow { qty: 2.0 }
    );
}

#[test]
// test_order_event_replay_rejects_partial_or_crossed_entry_protection:
// price 11.0 against reference 10.0 is "stop must be below". The comparison is
// inclusive at both ends, so a stop exactly at the entry is also no stop.
fn a_stop_on_the_wrong_side_of_the_entry_is_no_stop() {
    let mut kernel = kernel();
    for trigger in [11.0, 10.0] {
        let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, trigger, NOW);
        assert!(
            denied_for_missing_stop(kernel.assess(&intent, &flat(250_000.0, NOW))),
            "long stop at {trigger} must be refused"
        );
    }
    for trigger in [9.0, 10.0] {
        let intent = entry(CARRY, BUSDT, Side::Sell, 1.0, 10.0, trigger, NOW);
        assert!(
            denied_for_missing_stop(kernel.assess(&intent, &flat(250_000.0, NOW))),
            "short stop at {trigger} must be refused"
        );
    }
}

#[test]
// venue_protection.py `_optional_float`: a stop of "0" reads as absent, not as
// a stop at zero. Same for anything unreadable.
fn a_stop_that_is_not_a_positive_price_reads_as_absent() {
    let mut kernel = kernel();
    for trigger in [0.0, -1.0, f64::NAN] {
        let mut intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.3, NOW);
        intent.stop = Some(StopSpec {
            trigger_px: trigger,
        });
        assert!(
            denied_for_missing_stop(kernel.assess(&intent, &flat(250_000.0, NOW))),
            "stop {trigger} must read as absent"
        );
    }
}

#[test]
// test_reduce_only_command_never_carries_entry_attached_protection
fn an_exit_carries_no_stop_and_is_allowed_without_one() {
    let mut kernel = kernel();
    let held = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 2.0, 10.0, true)],
        NOW,
    );
    let out = exit(CARRY, BUSDT, Side::Sell, 2.0, 10.0, NOW);
    assert_eq!(kernel.assess(&out, &held), RiskVerdict::Allow { qty: 2.0 });
}

#[test]
// test_native_scale_in_requires_existing_open_position_protection: an open
// position with no stop is a fault state, and no new risk goes on top of it.
fn no_new_risk_while_a_held_position_has_no_stop() {
    let mut kernel = kernel();
    let unprotected = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 1.0, 10.0, false)],
        NOW,
    );
    let scale_in = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.3, NOW);
    assert!(denied_for_missing_stop(
        kernel.assess(&scale_in, &unprotected)
    ));

    // Another symbol entirely is refused too: the account-level health chain
    // blocks the book, not just the symbol.
    let elsewhere = entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.3, NOW);
    assert!(denied_for_missing_stop(
        kernel.assess(&elsewhere, &unprotected)
    ));
}

#[test]
// The fault state must never be the reason a book cannot come down.
fn an_exit_is_allowed_while_the_book_is_unprotected() {
    let mut kernel = kernel();
    let unprotected = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 2.0, 10.0, false)],
        NOW,
    );
    let out = exit(CARRY, BUSDT, Side::Sell, 2.0, 10.0, NOW);
    assert_eq!(
        kernel.assess(&out, &unprotected),
        RiskVerdict::Allow { qty: 2.0 }
    );
}

#[test]
// The durable reference price Python judges geometry against is not always the
// order's own limit, so the stop has to clear both.
fn a_stop_must_clear_every_price_the_order_could_fill_at() {
    let mut kernel = kernel();
    kernel.observe_price(BUSDT, 12.0);

    // A stop at 11.0 is below the last price but not below the limit this
    // order may fill at.
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 11.0, NOW);
    assert!(denied_for_missing_stop(
        kernel.assess(&intent, &flat(250_000.0, NOW))
    ));

    let clear = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.3, NOW);
    assert_eq!(
        kernel.assess(&clear, &flat(250_000.0, NOW)),
        RiskVerdict::Allow { qty: 1.0 }
    );
}

#[test]
// A market entry has no price of its own; the stop geometry is judged against
// the last price the kernel was given.
fn a_market_entry_is_judged_against_the_last_known_price() {
    let mut kernel = kernel();
    kernel.observe_price(BUSDT, 10.0);
    let good = market_entry(CARRY, BUSDT, Side::Buy, 1.0, 9.3, NOW);
    assert_eq!(
        kernel.assess(&good, &flat(250_000.0, NOW)),
        RiskVerdict::Allow { qty: 1.0 }
    );

    let crossed = market_entry(CARRY, BUSDT, Side::Buy, 1.0, 10.7, NOW);
    assert!(denied_for_missing_stop(
        kernel.assess(&crossed, &flat(250_000.0, NOW))
    ));
}
