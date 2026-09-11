//! The execution engine's loop.
//!
//! Read `docs/engine.md` first. The loop is written against the traits in
//! `engine-types` — `Wal`, `RiskKernel`, `VenueGateway`, `MarketFeed`,
//! `OrderFeed`, `Strategy` — and names no venue, log format or kernel of its
//! own. `assembly.rs` is the only file here that names the concrete crates
//! behind those traits; `runner.rs` is the `engine run` command that puts them
//! together.

pub mod account_state_bench;
pub mod assembly;
pub mod attribution;
pub mod callback_recovery;
pub mod clear;
pub mod clock;
pub mod config;
pub mod controls;
pub mod covers;
pub mod ctx;
mod effects;
pub mod engine;
pub mod execution;
mod execution_accounting;
mod execution_ids;
pub mod heartbeat;
pub mod identities;
pub mod inflight;
mod inventory;
pub mod ledger;
mod legacy_quantity;
pub mod legacy_signals;
mod order_dispatch;
mod portfolio_allocation;
mod portfolio_control;
mod portfolio_protection;
mod portfolio_routes;
pub mod reconcile;
pub mod replay;
pub mod routing;
pub mod runner;
mod signal_state;
pub mod signals;
#[cfg(test)]
#[path = "../../test-support/clock.rs"]
mod test_clock;
#[cfg(test)]
#[path = "../../test-support/io.rs"]
mod test_io;
pub mod trades;
pub mod venue_runtime;
pub mod working;

#[cfg(test)]
mod testpath;
#[cfg(test)]
mod tests;

pub use engine::{Engine, EngineError, RunOutcome, StopReason};
