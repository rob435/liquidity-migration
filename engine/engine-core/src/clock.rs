//! One monotonic clock for the whole engine.
//!
//! Every `*_ns` stamp in the log and in the latency ledger comes from here, so
//! they are all comparable to each other. They are not wall-clock times; use
//! [`wall_ms`] when a record needs a human date.

use std::sync::OnceLock;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

fn origin() -> Instant {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    *ORIGIN.get_or_init(Instant::now)
}

/// Nanoseconds since the engine's clock origin.
pub fn now_ns() -> u64 {
    origin().elapsed().as_nanos() as u64
}

/// Wall-clock milliseconds since the unix epoch.
pub fn wall_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
