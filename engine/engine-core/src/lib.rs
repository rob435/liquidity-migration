//! The execution engine's loop.
//!
//! Read `docs/engine.md` first: this crate is the part that wires the others
//! together and runs them. It is written against the traits in `engine-types`
//! and nothing else, which is why it can be built and tested while the crates
//! that supply market data, the venue, the log and the risk kernel are still
//! being written. `assembly.rs` is where those get plugged in.

pub mod assembly;
pub mod bench;
pub mod clock;
pub mod config;
pub mod ctx;
pub mod engine;

pub mod inflight;
pub mod ledger;
pub mod replay;
pub mod routing;
pub mod runner;

#[cfg(test)]
mod testpath;
#[cfg(test)]
mod tests;

pub use engine::{Engine, EngineError, RunOutcome, StopReason};
