//! Two clocks: monotonic nanoseconds for engine stamps, wall milliseconds
//! for the venue's own timestamp field.

/// Engine monotonic nanoseconds from the shared origin in engine-types, so
/// this crate's stamps are comparable to every other crate's.
pub(crate) fn mono_ns() -> u64 {
    engine_types::clock::mono_ns()
}

/// Venue wall-clock milliseconds, what Bybit signs against.
pub(crate) fn wall_ms() -> i64 {
    engine_types::clock::wall_ms()
}
