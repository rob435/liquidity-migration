use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, SymbolId};
use crate::orders::{AmendSpec, Intent, OrderRequest, OrderUpdate};
use crate::risk::RiskVerdict;

/// One record in the append-only log. Serialized as tagged JSON inside a
/// checksummed binary frame (framing is the WAL crate's concern). Kept
/// human-readable on purpose: the log is the engine's audit trail.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum WalRecord {
    /// Engine start: code identity and config identity, so every later
    /// record is attributable.
    Boot {
        version: String,
        config_sha256: String,
        wall_ts_ms: i64,
    },
    Intent {
        intent: Intent,
    },
    Verdict {
        client_order_id: Option<String>,
        verdict: RiskVerdict,
    },
    /// Written and made durable BEFORE the order bytes leave the socket. A
    /// crash between this record and the ack can never forget an in-flight
    /// order.
    OrderSent {
        request: OrderRequest,
        wire_ns: u64,
        /// `M0`: the midpoint of the book at the moment this order left, which
        /// is what every arrival number is measured against
        /// (`docs/architecture.md` §Trade diagnostics). Zero means the book
        /// could not be read, and a zero anchor yields no measurement rather
        /// than a flattering one.
        ///
        /// It is written here, on the send, because this is the only moment
        /// it exists: a worked entry can rest for a minute before it fills,
        /// and by then the price it was decided against is gone. Defaulted on
        /// the way in so a log written before the field existed still replays
        /// — as unmeasurable, which is the truth about it.
        #[serde(default)]
        arrival_mid: f64,
    },
    OrderUpdate {
        update: OrderUpdate,
    },
    /// A cancel on its way out. Not barriered before the wire: a cancel adds
    /// no exposure, and an order the log still shows working is recovered at
    /// boot whether or not the cancel survived the crash.
    CancelSent {
        symbol: SymbolId,
        client_order_id: String,
        wire_ns: u64,
    },
    /// An in-place reprice or resize on its way out.
    AmendSent {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        wire_ns: u64,
    },
    /// Where the market went after one of our fills.
    ///
    /// The one execution-quality number that is an observation rather than
    /// arithmetic: what a fill cost against the book when the order left can
    /// always be recomputed from `OrderSent` and the fill, but a price five
    /// minutes later exists only if somebody wrote it down at the time. So
    /// this is written when the horizon comes due.
    ///
    /// Names and signs are `docs/architecture.md` §Trade diagnostics.
    Markout {
        client_order_id: String,
        strategy: StrategyId,
        symbol: SymbolId,
        /// Which fill, by the venue's own stamp — an order can fill more than
        /// once.
        fill_ts_ms: i64,
        horizon_ms: u64,
        /// `Mh`. Absent when no readable book turned up inside the lateness
        /// bound, which is a horizon terminally missing rather than a zero.
        mid: Option<f64>,
        /// **Positive means the price moved our way.** The opposite sign
        /// convention from everything else, and it is the doc's.
        signed_markout_bps: Option<f64>,
        /// What the horizon actually came to. The engine looks on its
        /// group-flush tick, so a mark is always a little late.
        actual_horizon_ms: u64,
        /// What this mark speaks for, so a rollup can weight it.
        notional_usdt: f64,
    },
    /// Periodic latency ledger line: histogram quantiles in nanoseconds.
    LatencyLedger {
        window_s: u32,
        events: u64,
        decide_p50_ns: u64,
        decide_p99_ns: u64,
        wire_p50_ns: u64,
        wire_p99_ns: u64,
    },
    /// Free-form strategy note, tagged and rare.
    Note {
        source: String,
        text: String,
    },
    /// Control state that must outlive the process — above all the loss
    /// guard's daily anchor and trip latch. Written (and made durable) the
    /// moment it changes; the newest one is restored at boot, so a restart
    /// can never hand the day a fresh loss budget or clear a trip.
    ControlAnchor {
        source: String,
        state: String,
    },
    /// What boot found when it compared this log against the venue, and
    /// whether the engine may open new exposure afterwards.
    ///
    /// `may_open` false is a latch, and it is written here rather than held in
    /// memory for the same reason the loss guard's trip is: a restart that
    /// cleared it would turn "stop and tell somebody" into "stop until the
    /// next crash". Boot reads the newest one back before it reads anything
    /// from the venue.
    Reconciled {
        wall_ts_ms: i64,
        findings: Vec<String>,
        may_open: bool,
    },
}

#[derive(Debug, thiserror::Error)]
pub enum WalError {
    #[error("wal io: {0}")]
    Io(#[from] std::io::Error),
    #[error("wal frame corrupt at offset {offset}: {detail}")]
    Corrupt { offset: u64, detail: String },
}

/// The append-only log. One writer (the engine loop). Appends are buffered;
/// `barrier` is the durability point used before order sends; `flush` is the
/// cheap group commit for everything else.
pub trait Wal {
    /// Buffered append. Returns the record's sequence number.
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError>;
    /// Make everything appended so far durable now (fdatasync).
    fn barrier(&mut self) -> Result<(), WalError>;
    /// Push buffered bytes to the OS without forcing disk durability.
    fn flush(&mut self) -> Result<(), WalError>;
}
