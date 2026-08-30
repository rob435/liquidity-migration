//! Two clocks: monotonic nanoseconds for engine stamps, wall milliseconds
//! for the venue's own timestamp field.

use std::future::Future;

/// Engine monotonic nanoseconds from the shared origin in engine-types, so
/// this crate's stamps are comparable to every other crate's.
pub(crate) fn mono_ns() -> u64 {
    engine_types::clock::mono_ns()
}

/// Venue wall-clock milliseconds, what Bybit signs against.
pub(crate) fn wall_ms() -> i64 {
    engine_types::clock::wall_ms()
}

/// Stamp an account scan before its first request is polled. The returned
/// view may describe any point during the round trip, so completion time would
/// incorrectly erase fills that arrived while the request was in flight.
pub(crate) async fn account_scan<F: Future>(scan: F) -> (u64, F::Output) {
    let observed_ns = mono_ns();
    (observed_ns, scan.await)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_delayed_account_reply_keeps_the_scan_start_stamp() {
        let (observed_ns, completed_ns) = account_scan(async {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            mono_ns()
        })
        .await;

        assert!(completed_ns > observed_ns);
        assert!(completed_ns.saturating_sub(observed_ns) >= 5_000_000);
    }
}
