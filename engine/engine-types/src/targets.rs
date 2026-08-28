//! The target book: what the research system decided to hold.
//!
//! This is the shape that crosses the wall between the two halves of the
//! repository. Python writes it to a file, the engine reads that file, and
//! neither side calls the other. Only the shape lives here — reading it,
//! and deciding what to do about it, belong elsewhere.
//!
//! Times are venue wall-clock milliseconds, not the engine's monotonic
//! nanoseconds: they were written by another process on another clock, and
//! the only clock they can be compared against is one of the same kind.

/// One symbol's absolute target, as research wrote it down.
#[derive(Clone, Debug, PartialEq)]
pub struct BookTarget {
    pub symbol: String,
    /// Signed: positive long, negative short, and exactly zero means hold
    /// none of it — an exit, not an absence.
    pub notional_usdt: f64,
    /// How far below (long) or above (short) the entry the stop sits.
    pub stop_loss_fraction: f64,
    /// The leverage research sized against. Carried so the log says what the
    /// book assumed; the account's own leverage is what the venue applies.
    pub leverage: f64,
    /// Direct deadline for opening or adding exposure in this symbol. When
    /// absent, the follower uses the book-wide validity and cutoff.
    pub entry_valid_until_ms: Option<i64>,
    /// Exact signed base-asset quantity. When present, execution follows this
    /// quantity instead of re-deriving it from notional and a later price.
    pub target_qty: Option<f64>,
}

/// A whole book, already known to be a version this engine reads.
///
/// A book is absolute: silence about a symbol says hold none of it. So an
/// empty `targets` list is a real decision — hold nothing — and is quite
/// different from having no book at all, which is no decision.
#[derive(Clone, Debug, PartialEq)]
pub struct TargetBook {
    /// Which producer wrote it. For the log, not for routing.
    pub source: String,
    pub decision_ts_ms: i64,
    /// Past this, entries stop; exits keep working.
    pub valid_until_ms: i64,
    pub targets: Vec<BookTarget>,
}
