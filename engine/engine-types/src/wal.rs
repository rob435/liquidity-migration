use serde::{Deserialize, Serialize};

use crate::orders::{Intent, OrderRequest, OrderUpdate};
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
    },
    OrderUpdate {
        update: OrderUpdate,
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
