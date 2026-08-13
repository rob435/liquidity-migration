use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, SymbolId};

#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn flipped(self) -> Side {
        match self {
            Side::Buy => Side::Sell,
            Side::Sell => Side::Buy,
        }
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum TimeInForce {
    Gtc,
    Ioc,
    PostOnly,
}

#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum OrderKind {
    Market,
    Limit { px: f64, tif: TimeInForce },
}

/// Stop-loss to attach with (or immediately after) the entry. The risk
/// kernel refuses position-opening intents that carry no stop.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StopSpec {
    pub trigger_px: f64,
}

/// What a strategy asks for. Strategies never build venue payloads; they
/// emit intents and the engine does the rest.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Intent {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    pub kind: OrderKind,
    pub stop: Option<StopSpec>,
    /// True for exits: the order may only reduce an existing position.
    pub reduce_only: bool,
    /// Short strategy-chosen label, recorded in the log.
    pub tag: String,
    /// Engine monotonic nanoseconds when the strategy decided.
    pub decided_ns: u64,
}

/// A risk-approved order on its way to the venue. Quantities and prices are
/// already quantized to the instrument's step and tick.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OrderRequest {
    /// Engine-minted, unique per boot, recorded in the log before send.
    pub client_order_id: String,
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    pub kind: OrderKind,
    pub stop: Option<StopSpec>,
    pub reduce_only: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OrderAck {
    pub client_order_id: String,
    pub venue_order_id: String,
    /// Engine monotonic nanoseconds when the venue reply was parsed.
    pub ack_ns: u64,
}

/// Order lifecycle news, from the venue reply or the private stream.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum OrderUpdate {
    Ack(OrderAck),
    Reject {
        client_order_id: String,
        code: i64,
        reason: String,
    },
    Fill {
        client_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        fee: f64,
        venue_ts_ms: i64,
        recv_ns: u64,
    },
    Cancelled {
        client_order_id: String,
        recv_ns: u64,
    },
    StopAttached {
        symbol: SymbolId,
        trigger_px: f64,
        recv_ns: u64,
    },
}

/// Tick size, step size, and minimums for one instrument.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct InstrumentRule {
    pub tick_size: f64,
    pub qty_step: f64,
    pub min_qty: f64,
    pub min_notional: f64,
}

#[derive(Debug, thiserror::Error)]
pub enum VenueError {
    #[error("venue transport: {0}")]
    Transport(String),
    #[error("venue rejected ({code}): {message}")]
    Rejected { code: i64, message: String },
    #[error("venue reply unreadable: {0}")]
    BadReply(String),
    #[error("venue credentials missing or malformed: {0}")]
    Credentials(String),
}
