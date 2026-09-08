//! Offline workloads and operator reports built against the execution core.

use engine_core::{
    assembly, clock, config, controls, engine, execution, identities, ledger, reconcile, signals,
    trades,
};

pub mod backtest;
pub mod bench;
pub mod equity_recorder;
pub mod execution_study;
pub mod sim;
pub mod timing;
pub mod wal_conversion;

#[cfg(test)]
mod testpath;

#[cfg(any(feature = "bybit", feature = "mexc"))]
pub mod canary;
pub mod flatness;
pub mod takeover;
