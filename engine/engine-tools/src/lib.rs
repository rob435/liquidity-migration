//! Offline workloads and operator reports built against the execution core.

use engine_core::{
    assembly, clock, config, controls, engine, execution, identities, ledger, reconcile, signals,
    trades,
};

pub mod backtest;
pub mod bench;
pub mod sim;
pub mod timing;

#[cfg(test)]
mod testpath;

pub mod canary;
pub mod flatness;
pub mod takeover;
