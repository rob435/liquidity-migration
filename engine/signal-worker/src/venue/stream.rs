//! The venue-neutral public stream vocabulary. Every `PublicVenue`'s stream
//! reports its transport in these types; `StreamHealth`'s field names reach
//! the heartbeat, and `scripts/runtime/check_fleet_liveness.py` reads
//! `bybit_ws_ticker_coverage_complete` and `bybit_ws_gap_open` off it.

use std::collections::BTreeSet;

use serde_json::Value;

use crate::model::BybitTickerWire;

/// One closed hourly bar as the wire carries it: `[start_ms, open, high, low,
/// close, volume_base, turnover_quote]`.
#[derive(Clone, Debug, PartialEq)]
pub struct ConfirmedKline {
    pub symbol: String,
    pub available_at_ms: i64,
    pub row: Vec<Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum StreamEvent {
    EpochStarted {
        epoch: u64,
        observed_ts_ms: i64,
        reconnected: bool,
    },
    GapOpened {
        epoch: u64,
        observed_ts_ms: i64,
    },
    KlineClosed(ConfirmedKline),
    Fault(String),
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct StreamHealth {
    pub connected: bool,
    pub epoch: u64,
    pub gap_open: bool,
    pub gap_open_since_ms: Option<i64>,
    pub reconnect_count: u64,
    pub fault_count: u64,
    pub last_frame_ts_ms: Option<i64>,
    pub ticker_rows: usize,
    pub ticker_capacity: usize,
    pub ticker_coverage_complete: bool,
    pub ticker_topics_accepted: usize,
    pub ticker_topics_quarantined: usize,
    pub kline_topics_accepted: usize,
    pub kline_topics_quarantined: usize,
    pub queued_frames: usize,
    pub queue_capacity: usize,
}

/// The transport history a replacement stream must continue. Epoch numbering
/// is the token `mark_gap_repaired` matches, so it must never restart while
/// repair lanes from the outgoing stream are still in flight; the gap stamp and
/// the two counters are what the heartbeat and the on-call page read.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StreamContinuity {
    pub epoch: u64,
    pub gap_open: bool,
    pub gap_open_since_ms: Option<i64>,
    pub reconnect_count: u64,
    pub fault_count: u64,
}

impl From<&StreamHealth> for StreamContinuity {
    fn from(health: &StreamHealth) -> Self {
        Self {
            epoch: health.epoch,
            gap_open: health.gap_open,
            gap_open_since_ms: health.gap_open_since_ms,
            reconnect_count: health.reconnect_count,
            fault_count: health.fault_count,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TickerSample {
    pub observed_ts_ms: i64,
    pub available_at_ms: i64,
    pub rows: Vec<BybitTickerWire>,
}

/// The live transport for one venue's followed symbols.
pub trait PublicStream: Send {
    /// The next transport or market event, or `None` once the stream's task is
    /// gone. Cancel-safe: a dropped future loses no frame.
    fn next_event(&mut self) -> super::BoxFuture<'_, Option<StreamEvent>>;
    fn sample_tickers(&self, observed_ts_ms: i64, max_age_ms: i64) -> Option<TickerSample>;
    fn mark_gap_repaired(&self, epoch: u64) -> bool;
    fn mark_source_fault(&self, observed_ts_ms: i64);
    fn reconcile_tickers(
        &self,
        epoch: u64,
        rows: &[BybitTickerWire],
        request_started_at_ms: i64,
        received_at_ms: i64,
    ) -> bool;
    fn health(&self) -> StreamHealth;
    fn symbols(&self) -> &BTreeSet<String>;
}
