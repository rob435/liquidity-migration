use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, SymbolId};
use crate::orders::{AmendSpec, Intent, OrderRequest, OrderUpdate, Side};
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
    /// A venue-native stop moved on a position, with no order involved.
    /// Written before the call so a crash cannot forget where the stop was
    /// meant to be: `intended_stops` folds this the same way it folds an
    /// opening order's own stop, and boot's repair uses the result.
    StopSet {
        symbol: SymbolId,
        trigger_px: f64,
        wall_ts_ms: i64,
    },
    /// An in-place reprice or resize on its way out.
    AmendSent {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        wire_ns: u64,
    },
    /// A definitive venue answer to an amend. Until this arrives, replay
    /// keeps the full old/requested price range reserved: its high end prices
    /// notional and both ends price stop loss. An accepted/rejected answer
    /// narrows that conservative ambiguity to the price actually working.
    AmendResolved {
        client_order_id: String,
        effective_px: f64,
    },
    /// What the ids in this log mean.
    ///
    /// Both [`StrategyId`] and [`SymbolId`] are indexes handed out by
    /// position — the strategy's place in the config, the order a symbol was
    /// interned in — so every other record's `strategy` and `symbol` fields
    /// are numbers that mean nothing on their own. A log read a week later
    /// could say an order was strategy 1's, for symbol 4, and no more. This
    /// is the record that makes it readable.
    ///
    /// Written after boot interns what the strategies asked for, and again
    /// whenever a book names a symbol the engine had never followed. The whole
    /// table each time: ids are only ever appended, so the newest record is a
    /// superset of every earlier one and a reader can simply keep the last.
    Names {
        /// `strategies[i]` is the name of `StrategyId(i)`.
        strategies: Vec<String>,
        /// `symbols[i]` is the name of `SymbolId(i)`.
        symbols: Vec<String>,
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
        #[serde(default)]
        durable_p50_ns: u64,
        #[serde(default)]
        durable_p99_ns: u64,
        wire_p50_ns: u64,
        wire_p99_ns: u64,
        #[serde(default)]
        ack_p50_ns: u64,
        #[serde(default)]
        ack_p99_ns: u64,
        #[serde(default)]
        dispatch_queue_p50_ns: u64,
        #[serde(default)]
        dispatch_queue_p99_ns: u64,
        #[serde(default)]
        venue_task_p50_ns: u64,
        #[serde(default)]
        venue_task_p99_ns: u64,
        #[serde(default)]
        core_resume_p50_ns: u64,
        #[serde(default)]
        core_resume_p99_ns: u64,
        #[serde(default)]
        end_to_end_p50_ns: u64,
        #[serde(default)]
        end_to_end_p99_ns: u64,
    },
    /// Exact monotonic timing marks for one venue mutation item. These are
    /// the reconstructable measurements; histogram rows above are only the
    /// quick roll-up.
    VenueTiming {
        command_id: u64,
        operation: String,
        client_order_id: String,
        queued_ns: u64,
        task_started_ns: u64,
        /// Exact transport hand-off when the adapter exposes it.
        socket_write_ns: Option<u64>,
        /// Parsed venue acknowledgement for placements.
        ack_ns: Option<u64>,
        task_completed_ns: u64,
        core_handled_ns: u64,
    },
    /// Early fill signal from Bybit's fee-less fast stream. The ordinary
    /// authoritative fill follows separately and owns position accounting.
    FastExecution {
        exec_id: String,
        client_order_id: String,
        venue_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        is_maker: bool,
        venue_ts_ms: i64,
        recv_ns: u64,
    },
    /// Free-form strategy note, tagged and rare.
    Note {
        source: String,
        text: String,
    },
    /// Historical durable control state. The daily-loss feature that wrote
    /// these records is retired, but the shape remains so existing logs stay
    /// readable. New engine runs do not emit or restore it.
    ControlAnchor {
        source: String,
        state: String,
    },
    /// What boot found when it compared this log against the venue, and
    /// whether the engine may open new exposure afterwards.
    ///
    /// `may_open` false is a latch, and it is written here rather than held in
    /// memory: a restart that cleared it would turn "stop and tell somebody"
    /// into "stop until the next crash". Boot reads the newest one back before
    /// it reads anything from the venue.
    Reconciled {
        wall_ts_ms: i64,
        findings: Vec<String>,
        may_open: bool,
    },
    /// A fill this engine's own stream never delivered, recovered from the
    /// venue's execution history: it happened while the engine was down, or
    /// inside a private-stream gap. Counted into the per-symbol exposure sum
    /// exactly like a delivered fill, so the log stays an account of what the
    /// position actually is rather than only of what this process witnessed.
    RecoveredFill {
        /// The venue's own execution id — the dedup key against fetching the
        /// same history twice.
        exec_id: String,
        /// Empty when the venue reports none: a venue-attached stop firing,
        /// or a hand trade.
        client_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        fee: f64,
        is_maker: bool,
        /// When it happened, by the venue's clock.
        venue_ts_ms: i64,
        /// When this engine learned of it.
        recovered_wall_ts_ms: i64,
    },
    /// An operator looked at the log (`engine reconcile-clear --execute`):
    /// the per-symbol exposure sum is restated to the venue's own positions,
    /// with the standing findings kept here as the receipt, and the may-open
    /// latch resets. This is the deliberate act [`WalRecord::Reconciled`]'s
    /// latch waits for — the next boot still runs its own comparison, so a
    /// difference that appears again latches again.
    LatchCleared {
        wall_ts_ms: i64,
        /// Why, in the operator's words.
        note: String,
        /// The exposure ledger as restated — the venue's signed positions at
        /// the moment of clearing. A reader treats this as "set", like
        /// [`WalRecord::SegmentBase`]'s copy.
        restated_exposure: Vec<SymbolTotal>,
        /// What stood unexplained before the clear, so the log keeps saying
        /// what was absorbed.
        findings: Vec<String>,
    },
    /// Sleeve claims boot dropped because the venue held nothing in the
    /// symbol: closes this log never got to charge (a venue stop firing with
    /// no order id of ours, an inherited position wound down) leave a sleeve's
    /// row standing forever, and the row locks every other sleeve out of the
    /// name. Written durable so a later boot replays the drop instead of
    /// rebuilding the residue from the old fills — by then another sleeve may
    /// hold the symbol, and the venue no longer being flat would make the
    /// residue undroppable.
    ClaimsDropped {
        wall_ts_ms: i64,
        /// Exactly the rows as they stood when dropped, as the receipt.
        rows: Vec<FilledTotal>,
    },
    /// The first record of every log segment after the first: everything boot
    /// replay needs from the segments before this one, restated, so replaying
    /// this one segment recovers the same engine as replaying them all.
    ///
    /// One record on purpose. The frame checksum makes it all-or-nothing, so
    /// "this segment is complete enough to trust" is a single mechanical
    /// check: its first record reads back as one of these. A restatement
    /// spread over several records would need its own end-marker protocol,
    /// and a crash between two of them would leave a segment that replays
    /// half a state without saying so.
    ///
    /// Why not restate with the existing kinds: three of the things boot
    /// needs are sums over the whole history — whose fills built each
    /// position (`attribution`), what every fill in the log adds up to per
    /// symbol (`logged_exposure`), and the newest intended stop per symbol —
    /// and no copy of "the still-open orders' records" carries them. Copying
    /// every contributing fill forward would grow without bound, and writing
    /// invented fills that sum right would make the log lie.
    ///
    /// Every field is a **restatement of state at the moment of rotation**,
    /// so a reader of the whole segment chain treats it as "set", not "add":
    /// at that point in the stream it is exactly what the records before it
    /// already produced, which is what makes chain reads and single-segment
    /// reads agree.
    SegmentBase {
        wall_ts_ms: i64,
        /// The id tables, same meaning as [`WalRecord::Names`].
        strategies: Vec<String>,
        symbols: Vec<String>,
        /// The reconciliation latch, same meaning as
        /// [`WalRecord::Reconciled`]'s `may_open`.
        may_open: bool,
        /// Historical control anchors carried by older rotations. Retained in
        /// the schema for replay compatibility; new rotations leave it empty.
        control_anchors: Vec<AnchorState>,
        /// Signed filled quantity per (strategy, symbol): whose fills built
        /// each position. Flat rows are absent.
        attribution: Vec<FilledTotal>,
        /// Signed quantity per symbol summed over every fill the log ever
        /// held, strangers' included — what reconcile compares the venue's
        /// positions against.
        logged_exposure: Vec<SymbolTotal>,
        /// The stop each symbol's newest opening order asked for, so a stop
        /// the venue drops can still be put back after a rotation.
        intended_stops: Vec<IntendedStop>,
        /// Venue execution ids still inside the recovery horizon. Bounded in
        /// memory and restated so rotating cannot make an old fill new again.
        #[serde(default)]
        recent_execution_ids: Vec<RecentExecutionId>,
        /// Every order still in flight, with the fields its own records
        /// carried.
        open_orders: Vec<OpenOrderState>,
    },
}

/// One control-state line inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AnchorState {
    pub source: String,
    pub state: String,
}

/// One attribution row inside [`WalRecord::SegmentBase`]. Positive is long.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct FilledTotal {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub signed_qty: f64,
}

/// One per-symbol fill total inside [`WalRecord::SegmentBase`].
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct SymbolTotal {
    pub symbol: SymbolId,
    pub signed_qty: f64,
}

/// One intended-stop row inside [`WalRecord::SegmentBase`].
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct IntendedStop {
    pub symbol: SymbolId,
    /// Direction of the position this stop protects. Older segment bases did
    /// not carry it; those rows deserialize as unknown and are deliberately
    /// not trusted for automatic repair.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub side: Option<Side>,
    pub trigger_px: f64,
}

/// One execution-id dedup entry inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RecentExecutionId {
    pub exec_id: String,
    /// Wall-clock milliseconds when the engine first learned this id.
    pub seen_ms: i64,
}

/// One still-open order inside [`WalRecord::SegmentBase`]: what its own
/// `OrderSent` record and the updates so far said about it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OpenOrderState {
    pub request: OrderRequest,
    pub wire_ns: u64,
    #[serde(default)]
    pub arrival_mid: f64,
    pub acked: bool,
    pub filled_qty: f64,
    /// Plausible working-price bounds after an amend whose answer was lost.
    /// Zero in older segments means "derive the exact price from request".
    #[serde(default)]
    pub reservation_low_px: f64,
    #[serde(default)]
    pub reservation_high_px: f64,
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
    /// Bytes in the current segment, buffered ones included. Zero for a log
    /// that does not live in a file, which also means it is never rotated.
    fn segment_size(&self) -> u64 {
        0
    }
    /// Start a fresh segment whose first record is `base` (a
    /// [`WalRecord::SegmentBase`]), archiving the current one in place.
    /// Returns false for a log that does not rotate — the in-memory test
    /// doubles — which is not an error, just a log with nothing to archive.
    fn rotate(&mut self, base: &WalRecord) -> Result<bool, WalError> {
        let _ = base;
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The exact bytes a live log already holds. A refusal is written whole,
    /// so every reason the kernel has ever produced is frozen into the format
    /// and the reader has to keep understanding it long after the rule that
    /// produced it is gone.
    #[test]
    fn a_refusal_the_kernel_no_longer_produces_still_reads_back() {
        let frame = r#"{"kind":"verdict","client_order_id":null,"verdict":{"Deny":{"reason":{"SymbolNotionalBreached":{"symbol":11,"notional_usdt":156255.2326,"cap_usdt":125000.0}}}}}"#;

        let record: WalRecord =
            serde_json::from_str(frame).expect("an old refusal must still parse");

        let WalRecord::Verdict { verdict, .. } = record else {
            panic!("expected a verdict record");
        };
        let RiskVerdict::Deny { reason } = verdict else {
            panic!("expected a denial");
        };
        let rendered = format!("{reason:?}");
        assert!(rendered.contains("SymbolNotionalBreached"), "{rendered}");
        assert!(rendered.contains("156255.2326"), "{rendered}");
        assert!(rendered.contains("125000.0"), "{rendered}");
    }

    #[test]
    fn a_retired_loss_guard_verdict_and_anchor_still_read_back() {
        let verdict = r#"{"kind":"verdict","client_order_id":null,"verdict":{"Deny":{"reason":{"LossGuardTripped":{"equity_usdt":89.5,"floor_usdt":90.0}}}}}"#;
        let anchor = r#"{"kind":"control_anchor","source":"risk","state":"{\"day\":20693,\"tripped\":true}"}"#;

        let verdict: WalRecord =
            serde_json::from_str(verdict).expect("a retired loss verdict must still parse");
        let anchor: WalRecord =
            serde_json::from_str(anchor).expect("a retired control anchor must still parse");

        assert!(matches!(
            verdict,
            WalRecord::Verdict {
                verdict: RiskVerdict::Deny {
                    reason: crate::risk::DenyReason::LossGuardTripped {
                        equity_usdt: 89.5,
                        floor_usdt: 90.0,
                    },
                },
                ..
            }
        ));
        assert!(matches!(
            anchor,
            WalRecord::ControlAnchor { source, state }
                if source == "risk" && state.contains("tripped")
        ));
    }

    #[test]
    fn a_legacy_intended_stop_reads_as_direction_unknown() {
        let row: IntendedStop = serde_json::from_str(r#"{"symbol":3,"trigger_px":90.0}"#)
            .expect("legacy segment rows must still parse");

        assert_eq!(row.symbol, SymbolId(3));
        assert_eq!(row.side, None);
        assert_eq!(row.trigger_px, 90.0);
    }
}
