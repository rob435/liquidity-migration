//! Fixtures shared by the parity tables. Every number here comes from the
//! Python control or its tests; the source is named where it is not obvious.

#![allow(dead_code)]

use engine_risk::{
    EnvelopeConfig, KernelConfig, LossGuardConfig, PartitionConfig, StrategyAllocation,
};
use engine_types::ids::{StrategyId, SymbolId};
use engine_types::orders::{Intent, OrderKind, Side, StopSpec, TimeInForce};
use engine_types::risk::{AccountView, PositionView};

pub const SEC: u64 = 1_000_000_000;
pub const DAY: u64 = 86_400 * SEC;

/// account_loss_guard.py: DEFAULT_MAX_EQUITY_STALENESS_NS.
pub const MAX_VIEW_AGE_NS: u64 = 120 * SEC;
/// deploy/account-execution-mainnet.env.template: DISASTER_STOP_FRACTION.
pub const DISASTER_STOP_FRACTION: f64 = 0.35;
/// account_contracts.py: AccountRiskPolicy.quantity_tolerance.
pub const QTY_TOLERANCE: f64 = 1e-12;
/// tests/account/test_sleeve_capital_partition.py: InstrumentRules min_notional.
pub const MIN_ORDER_NOTIONAL_USDT: f64 = 1.0;

pub const CARRY: StrategyId = StrategyId(0);
pub const LONG: StrategyId = StrategyId(1);
/// A third strategy no fixture partition names. The sleeve it was named for
/// has since lost its claim on capital in the operational profiles, so read
/// this as "a strategy with no share", not as a live sleeve.
pub const CONTINUOUS: StrategyId = StrategyId(2);
pub const BUSDT: SymbolId = SymbolId(0);
pub const CUSDT: SymbolId = SymbolId(1);

/// Wall-clock nanoseconds at noon on a UTC day index, for the loss guard's day
/// roll. Python takes the day from the equity reading's own timestamp.
pub fn utc_noon(day_index: u64) -> u64 {
    day_index * DAY + 12 * 3600 * SEC
}

/// The unpartitioned demo shape: configs/operational.demo.json, whose capital
/// reference is fixed at 250_000 and whose gross cap is twice it.
pub fn demo_config() -> KernelConfig {
    KernelConfig {
        max_account_view_age_ns: MAX_VIEW_AGE_NS,
        loss_guard: LossGuardConfig {
            max_daily_loss_usdt: Some(1_000.0),
        },
        envelope: EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: 250_000.0,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: 2.0,
            disaster_stop_fraction: DISASTER_STOP_FRACTION,
            // The loosest legal setting: symbol and second gross ceiling both
            // at the account gross cap, margin at what that gross funds. The
            // demo profile is tighter (a 125_000 symbol cap), but these tables
            // are about the envelope and the partition, and a tighter cap here
            // would refuse their orders before the control under test ran.
            // tests/account_caps.rs sets each cap to the binding one instead.
            max_symbol_notional_usdt: 500_000.0,
            max_component_gross_notional_usdt: 500_000.0,
            max_initial_margin_usdt: 250_000.0,
        },
        partition: PartitionConfig {
            allocations: Vec::new(),
            leverage: 2.0,
            min_order_notional_usdt: MIN_ORDER_NOTIONAL_USDT,
        },
        qty_tolerance: QTY_TOLERANCE,
    }
}

/// The equity-anchored shape: configs/operational.mainnet.json's
/// capital_reference block, at the demo reference the envelope tests use.
pub fn equity_tracking_config() -> KernelConfig {
    let mut cfg = demo_config();
    cfg.envelope.tracks_equity = true;
    cfg
}

/// tests/account/test_sleeve_capital_partition.py `_policy()`: room for 1000
/// USDT of book, split 600 CARRY / 300 LONG, margin shares at leverage 2.
pub fn partition_config() -> KernelConfig {
    let mut cfg = demo_config();
    cfg.envelope.reference_usdt = 500.0;
    cfg.envelope.gross_notional_multiple = 2.0;
    // The account caps move with the reference, or they would sit 500x above
    // the 1000 USDT of book this shape describes and never be reached.
    cfg.envelope.max_symbol_notional_usdt = 1_000.0;
    cfg.envelope.max_component_gross_notional_usdt = 1_000.0;
    cfg.envelope.max_initial_margin_usdt = 500.0;
    cfg.partition = PartitionConfig {
        allocations: vec![
            StrategyAllocation {
                strategy: CARRY,
                max_gross_notional_usdt: 600.0,
                max_initial_margin_usdt: 300.0,
            },
            StrategyAllocation {
                strategy: LONG,
                max_gross_notional_usdt: 300.0,
                max_initial_margin_usdt: 150.0,
            },
        ],
        leverage: 2.0,
        min_order_notional_usdt: MIN_ORDER_NOTIONAL_USDT,
    };
    cfg
}

pub fn view(equity_usdt: f64, positions: Vec<PositionView>, observed_ns: u64) -> AccountView {
    AccountView {
        equity_usdt,
        available_usdt: equity_usdt,
        positions,
        observed_ns,
    }
}

pub fn flat(equity_usdt: f64, observed_ns: u64) -> AccountView {
    view(equity_usdt, Vec::new(), observed_ns)
}

pub fn position(
    symbol: SymbolId,
    side: Side,
    qty: f64,
    entry_px: f64,
    stop_attached: bool,
) -> PositionView {
    PositionView {
        symbol,
        side,
        qty,
        entry_px,
        stop_attached,
        leverage: None
    }
}

/// A position-opening limit order carrying a stop.
pub fn entry(
    strategy: StrategyId,
    symbol: SymbolId,
    side: Side,
    qty: f64,
    px: f64,
    stop_px: f64,
    decided_ns: u64,
) -> Intent {
    Intent {
        strategy,
        symbol,
        side,
        qty,
        kind: OrderKind::Limit {
            px,
            tif: TimeInForce::Gtc,
        },
        stop: Some(StopSpec {
            trigger_px: stop_px,
        }),
        reduce_only: false,
        tag: "test".to_string(),
        decided_ns,
        work: None,
        leverage: None,
    }
}

/// The same order with no stop on it.
pub fn naked_entry(
    strategy: StrategyId,
    symbol: SymbolId,
    side: Side,
    qty: f64,
    px: f64,
    decided_ns: u64,
) -> Intent {
    let mut intent = entry(strategy, symbol, side, qty, px, px * 0.9, decided_ns);
    intent.stop = None;
    intent
}

pub fn market_entry(
    strategy: StrategyId,
    symbol: SymbolId,
    side: Side,
    qty: f64,
    stop_px: f64,
    decided_ns: u64,
) -> Intent {
    let mut intent = entry(strategy, symbol, side, qty, 1.0, stop_px, decided_ns);
    intent.kind = OrderKind::Market;
    intent
}

pub fn exit(
    strategy: StrategyId,
    symbol: SymbolId,
    side: Side,
    qty: f64,
    px: f64,
    decided_ns: u64,
) -> Intent {
    let mut intent = entry(strategy, symbol, side, qty, px, px * 0.9, decided_ns);
    intent.stop = None;
    intent.reduce_only = true;
    intent
}
