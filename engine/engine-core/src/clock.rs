//! One monotonic clock for the whole engine.
//!
//! Every `*_ns` stamp in the log and in the latency ledger comes from here, so
//! they are all comparable to each other. They are not wall-clock times; use
//! [`wall_ms`] when a record needs a human date.

/// Nanoseconds since the engine's clock origin (the shared one in
/// engine-types, so stamps from every crate are comparable).
pub fn now_ns() -> u64 {
    engine_types::clock::mono_ns()
}

/// Wall-clock milliseconds since the unix epoch.
pub fn wall_ms() -> i64 {
    engine_types::clock::wall_ms()
}
