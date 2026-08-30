//! Restart-safe one-shot price-touch strategy.
//!
//! [`plan::decide`] owns the complete state transition. [`plug::TouchSniper`]
//! only reads the engine snapshot, applies the returned state, and emits the
//! ordered effects. The consumed-arm checkpoint is queued before the entry;
//! the engine makes it durable before venue dispatch and carries it through
//! WAL rotation.

pub mod plan;
mod plug;

pub use plug::{TouchSniper, NAME};

#[cfg(test)]
mod tests;
