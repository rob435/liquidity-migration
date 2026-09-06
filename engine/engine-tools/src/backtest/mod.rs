//! `engine backtest`: the live loop, driven by a recorded tape in the tape's
//! own time.
//!
//! What runs is the engine — `Engine::boot_as`, the risk kernel, the
//! strategy reducers, the working-order supervisor, the log — against parts
//! that stand in for the outside world:
//!
//! - a **virtual clock**, thread-local, advanced only by the [`feed::TapeFeed`]
//!   as it releases rows; the loop's group-flush tick and strategy timers
//!   run on it through [`scheduler::VirtualTimer`], so they fire in tape time;
//! - the **tape** in `market_tape`'s frozen row contract, book rows rebuilt
//!   with the same chaining rule the recorder's own reader uses;
//! - a **simulated venue** that keeps the venue-side book, fills by walking
//!   it, rests limit orders behind the displayed queue, triggers stops on the
//!   mark and fills them through the gap, settles funding at the published
//!   boundary, posts margin, and refuses what Bybit would refuse;
//! - a **modelled round trip**: an order reaches the venue half a round trip
//!   after it left and is matched against the book *then*, never earlier.
//!
//! Ordering is the driver's one job. Nothing in the engine may observe a
//! row, a fill, a tick, or a signal before its own instant, and two runs of
//! one tape write byte-identical logs. Both are tested.
//!
//! What it does not model, on purpose: our own orders' impact on the book,
//! other participants reacting to us, venue outages, rate-limit waits. Every
//! number it prints is bounded by those omissions, and the report says so.

pub mod feed;
pub mod runner;
pub mod scheduler;
pub mod signals;
pub mod tape;
pub mod venue;

#[cfg(test)]
mod tests;

pub use runner::{run, BacktestOptions, BacktestReport};
