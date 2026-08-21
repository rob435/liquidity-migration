//! Fixtures shared by the risk tables. Each number's source is named where it
//! is not obvious.
//!
//! Every test binary in this directory takes its own subset of these, so an
//! unused-here helper is normal and the lint is off for the file.
#![allow(dead_code)]

use engine_risk::{EnvelopeConfig, KernelConfig, PartitionConfig, StrategyAllocation};
use engine_types::ids::{StrategyId, SymbolId};
use engine_types::orders::{Intent, OrderKind, Side, StopSpec, TimeInForce};
use engine_types::risk::{AccountView, PositionView};

pub const SEC: u64 = 1_000_000_000;

/// The kernel's `max_account_view_age_ns`, as both operational profiles set it.
pub const MAX_VIEW_AGE_NS: u64 = 120 * SEC;
/// deploy/account-execution-mainnet.env.template: DISASTER_STOP_FRACTION.
pub const DISASTER_STOP_FRACTION: f64 = 0.35;
/// account_contracts.py: AccountRiskPolicy.quantity_tolerance.
pub const QTY_TOLERANCE: f64 = 1e-12;
/// tests/account/test_sleeve_capital_partition.py: InstrumentRules min_notional.
pub const MIN_ORDER_NOTIONAL_USDT: f64 = 1.0;

pub const CARRY: StrategyId = StrategyId(0);
pub const LONG: StrategyId = StrategyId(1);
/// A strategy with no share: no fixture partition names it, which is what
/// these tables use it to exercise. Not a live sleeve.
pub const CONTINUOUS: StrategyId = StrategyId(2);
pub const BUSDT: SymbolId = SymbolId(0);
pub const CUSDT: SymbolId = SymbolId(1);

/// The unpartitioned shape: a fixed 250_000 capital reference and a gross cap
/// twice it. The reference is the demo profile's; the multiple is this file's
/// own, kept low so the partition tables reach their caps.
pub fn demo_config() -> KernelConfig {
    KernelConfig {
        max_account_view_age_ns: MAX_VIEW_AGE_NS,
        envelope: EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: 250_000.0,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: 2.0,
            disaster_stop_fraction: DISASTER_STOP_FRACTION,
            // The loosest legal setting: the second gross ceiling at the
            // account gross cap, margin at what that gross funds. These tables
            // are about the envelope and the partition, and a tighter cap here
            // would refuse their orders before the control under test ran.
            // tests/account_caps.rs sets each cap to the binding one instead.
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

/// The same shape with the envelope tracking equity, which is what the funded
/// profile's `capital_reference` block turns on.
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
        stop_px: 0.0,
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
