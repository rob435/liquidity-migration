//! The execution engine's loop.
//!
//! Read `docs/engine.md` first: this crate is the part that wires the others
//! together and runs them. It is written against the traits in `engine-types`
//! and nothing else, which is why it can be built and tested while the crates
//! that supply market data, the venue, the log and the risk kernel are still
//! being written. `assembly.rs` is where those get plugged in.

pub mod account_state_bench;
pub mod assembly;
pub mod attribution;
pub mod bench;
pub mod clear;
pub mod clock;
pub mod config;
pub mod covers;
pub mod ctx;
pub mod engine;
pub mod execution;
mod execution_ids;
pub mod flatness;
pub mod heartbeat;
pub mod inflight;
pub mod ledger;
pub mod loss_reset;
pub mod reconcile;
pub mod replay;
pub mod routing;
pub mod runner;
pub mod targets;
pub mod trades;
pub mod working;

#[cfg(test)]
mod testpath;
#[cfg(test)]
mod tests;

pub use engine::{Engine, EngineError, RunOutcome, StopReason};
