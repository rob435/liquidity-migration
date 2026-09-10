//! The `intent_refused.code` vocabulary, pinned.
//!
//! A code is a wire value a report groups a population by, so the set is
//! frozen the way a record shape is: renaming one silently splits a lane in
//! two, and every log already written keeps the old word.

use std::collections::BTreeSet;

use engine_types::DenyReason;

use super::intent_admission::{code, OpeningRefusal};

/// Every code a funded binary can write. `no_instrument_rule`,
/// `below_minimum_size` and `below_minimum_notional` are absent on purpose:
/// only the `#[cfg(test)]` legacy quantize fixture reaches them.
const PINNED: [&str; 40] = [
    // The engine's own words, before or after the kernel.
    "batch_leverage_conflict",
    "close_does_not_reduce",
    "entry_stop_distance_cap",
    "exact_instrument_illegal",
    "exact_instrument_metadata_unavailable",
    "execution_control_unreadable_number",
    "invalid_exact_prices",
    "invalid_exact_quantity",
    "leverage_unsupported",
    "physical_protection",
    "price_collar",
    "stale_quote",
    "stop_would_loosen_position",
    "unreal_number",
    "venue_keeps_no_stop",
    "wake_action_limit",
    // `OpeningRefusal::as_str`.
    "engine_latched",
    "foreign_strategy_owner",
    "instrument_catalog_unready",
    "instrument_unlisted",
    "order_dispatch_unresolved",
    "portfolio_exit_pending",
    "private_stream_unready",
    "runtime_entries_disabled",
    "signal_producer_unready",
    "signal_sequence_gap",
    "stop_repair_pending",
    "strategy_callback_unavailable",
    "strategy_inactive",
    // `DenyReason::code`. `loss_guard_tripped` and `partition_exhausted` are
    // retired shapes the kernel no longer produces; the words stay because
    // the logs holding them stay readable.
    "available_margin_exhausted",
    "component_gross_breached",
    "envelope_breached",
    "initial_margin_breached",
    "loss_guard_tripped",
    "missing_stop",
    "partition_exhausted",
    "rolling_loss_tripped",
    "stale_account_view",
    "symbol_notional_breached",
    "unknown_state",
];

fn opening_refusals() -> [OpeningRefusal; 13] {
    [
        OpeningRefusal::ForeignStrategyOwner,
        OpeningRefusal::PortfolioExitPending,
        OpeningRefusal::StopRepairPending,
        OpeningRefusal::SignalSequenceGap,
        OpeningRefusal::SignalProducerUnready,
        OpeningRefusal::StrategyCallbackUnavailable,
        OpeningRefusal::StrategyInactive,
        OpeningRefusal::InstrumentCatalogUnready,
        OpeningRefusal::InstrumentUnlisted,
        OpeningRefusal::OrderDispatchUnresolved,
        OpeningRefusal::RuntimeEntriesDisabled,
        OpeningRefusal::PrivateStreamUnready,
        OpeningRefusal::EngineLatched,
    ]
}

fn deny_reasons() -> [DenyReason; 12] {
    [
        DenyReason::LossGuardTripped {
            equity_usdt: 0.0,
            floor_usdt: 0.0,
        },
        DenyReason::EnvelopeBreached {
            modelled_stop_charge_usdt: 0.0,
            allowance_usdt: 0.0,
        },
        DenyReason::ComponentGrossBreached {
            gross_usdt: 0.0,
            cap_usdt: 0.0,
        },
        DenyReason::InitialMarginBreached {
            margin_usdt: 0.0,
            cap_usdt: 0.0,
        },
        DenyReason::AvailableMarginExhausted {
            additional_margin_usdt: 0.0,
            available_usdt: 0.0,
        },
        DenyReason::RollingLossTripped {
            window_net_usdt: 0.0,
            limit_usdt: 0.0,
            window_ms: 0,
        },
        DenyReason::PartitionExhausted {
            strategy: engine_types::StrategyId(0),
            requested_usdt: 0.0,
            remaining_usdt: 0.0,
        },
        DenyReason::MissingStop,
        DenyReason::StaleAccountView {
            age_ns: 0,
            max_age_ns: 0,
        },
        DenyReason::StaleQuote {
            age_ns: 0,
            max_age_ns: 0,
        },
        DenyReason::UnknownState {
            detail: String::new(),
        },
        DenyReason::SymbolNotionalBreached {
            symbol: engine_types::SymbolId(0),
            notional_usdt: 0.0,
            cap_usdt: 0.0,
        },
    ]
}

#[test]
fn the_emitted_refusal_code_set_is_exactly_the_pinned_one() {
    let mut emitted = BTreeSet::new();
    for word in [
        code::UNREAL_NUMBER,
        code::INVALID_EXACT_PRICES,
        code::INVALID_EXACT_QUANTITY,
        code::STALE_QUOTE,
        code::WAKE_ACTION_LIMIT,
        code::BATCH_LEVERAGE_CONFLICT,
        code::PRICE_COLLAR,
        code::ENTRY_STOP_DISTANCE_CAP,
        code::EXECUTION_CONTROL_UNREADABLE_NUMBER,
        code::VENUE_KEEPS_NO_STOP,
        code::EXACT_INSTRUMENT_METADATA_UNAVAILABLE,
        code::EXACT_INSTRUMENT_ILLEGAL,
        code::PHYSICAL_PROTECTION,
        code::CLOSE_DOES_NOT_REDUCE,
        code::LEVERAGE_UNSUPPORTED,
        code::STOP_WOULD_LOOSEN_POSITION,
    ] {
        emitted.insert(word);
    }
    for refusal in opening_refusals() {
        emitted.insert(refusal.as_str());
    }
    for reason in deny_reasons() {
        emitted.insert(reason.code());
    }
    assert_eq!(
        emitted,
        BTreeSet::from(PINNED),
        "the refusal vocabulary changed; a code is a wire value"
    );
}

#[test]
fn every_code_is_snake_case_and_carries_no_number() {
    for word in PINNED {
        assert!(
            !word.is_empty()
                && word.chars().all(|c| c.is_ascii_lowercase() || c == '_')
                && !word.starts_with('_')
                && !word.ends_with('_'),
            "{word} is not a bounded snake_case grouping key"
        );
    }
}

/// One condition, one word: the engine's pre-kernel staleness check and the
/// kernel's own must not split the lane.
#[test]
fn the_engine_and_the_kernel_name_a_stale_quote_the_same_way() {
    assert_eq!(
        code::STALE_QUOTE,
        DenyReason::StaleQuote {
            age_ns: 1,
            max_age_ns: 2
        }
        .code()
    );
}

#[test]
fn each_execution_control_refusal_maps_to_its_own_code() {
    use super::execution_controls::control_refusal_code;
    assert_eq!(
        control_refusal_code("price_collar: limit is outside the mark band"),
        code::PRICE_COLLAR
    );
    assert_eq!(
        control_refusal_code("price_collar: fresh mark price is unavailable"),
        code::PRICE_COLLAR
    );
    assert_eq!(
        control_refusal_code("stop cap: requested stop is on the wrong side of entry"),
        code::ENTRY_STOP_DISTANCE_CAP
    );
    assert_eq!(
        control_refusal_code("stop cap: executable reference is unavailable"),
        code::ENTRY_STOP_DISTANCE_CAP
    );
    assert_eq!(
        control_refusal_code("value 1e999 is out of storage range"),
        code::EXECUTION_CONTROL_UNREADABLE_NUMBER
    );
}
