//! The engine's one monotonic clock.
//!
//! Every `*_ns` stamp in events, orders, the log, and the latency ledger
//! comes from here, so stamps taken in different crates are comparable.
//! The origin is the first call in the process; these are not wall times.

use std::sync::OnceLock;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

fn origin() -> Instant {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    *ORIGIN.get_or_init(Instant::now)
}

/// Engine monotonic nanoseconds since the process's clock origin.
pub fn mono_ns() -> u64 {
    origin().elapsed().as_nanos() as u64
}

/// Wall-clock milliseconds since the unix epoch, for venue timestamps and
/// human-readable records.
pub fn wall_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Wall-clock nanoseconds since the unix epoch. This is the bridge between
/// monotonic engine timings and externally captured venue data; it is not
/// used to measure durations.
pub fn wall_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stamps_share_one_origin_and_never_go_backwards() {
        let a = mono_ns();
        let b = mono_ns();
        assert!(b >= a);
    }

    #[test]
    fn wall_stamps_share_the_unix_epoch() {
        let ms = wall_ms().max(0) as u64;
        let ns = wall_ns();
        assert!(ns / 1_000_000 >= ms.saturating_sub(1));
        assert!(ns / 1_000_000 <= ms.saturating_add(1));
    }
}
