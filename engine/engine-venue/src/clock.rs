//! Two clocks: monotonic nanoseconds for engine stamps, wall milliseconds
//! for the venue's own timestamp field.

use std::sync::OnceLock;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

/// Engine monotonic nanoseconds, measured from the first call in this
/// process. Comparable to other stamps taken through this function.
///
/// NOTE for the integrating session: each crate that stamps `*_ns` currently
/// picks its own baseline, so stamps from different crates differ by a
/// constant offset. A shared epoch belongs in `engine-types`; this crate
/// does not add one on its own.
pub(crate) fn mono_ns() -> u64 {
    static BASE: OnceLock<Instant> = OnceLock::new();
    let base = *BASE.get_or_init(Instant::now);
    Instant::now().saturating_duration_since(base).as_nanos() as u64
}

/// Venue wall-clock milliseconds, what Bybit signs against.
pub(crate) fn wall_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
