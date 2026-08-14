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

/// A new price and/or size for an order already resting at the venue.
/// `None` leaves that field as it is.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AmendSpec {
    pub px: Option<f64>,
    pub qty: Option<f64>,
}

/// What a strategy asks the engine to do. Placing is one of three verbs: a
/// market maker that can only place is a maker that cannot leave.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Action {
    Place(Intent),
    /// Pull a resting order by the id the engine minted for it.
    Cancel {
        symbol: SymbolId,
        client_order_id: String,
    },
    /// Reprice or resize in place, keeping the order's venue identity and,
    /// where the venue allows it, its queue position. Refused on a venue
    /// whose [`crate::VenueCaps`] say it cannot amend — the engine will not
    /// quietly substitute cancel-and-replace, which is a different trade.
    Amend {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
    },
}

impl Action {
    /// Whether this action can only reduce the engine's exposure. Under a
    /// flood the engine drops what adds risk and lets the rest through, so
    /// cancels and exits keep flowing when entries stop.
    pub fn is_risk_reducing(&self) -> bool {
        match self {
            Action::Place(intent) => intent.reduce_only,
            Action::Cancel { .. } => true,
            Action::Amend { .. } => false,
        }
    }

    pub fn symbol(&self) -> SymbolId {
        match self {
            Action::Place(intent) => intent.symbol,
            Action::Cancel { symbol, .. } | Action::Amend { symbol, .. } => *symbol,
        }
    }
}

/// One of a strategy's own orders that the log says is still working. Handed
/// out by [`crate::strategy::StrategyCtx::resting`] so a quoting strategy can
/// find the order it wants to pull or move. Borrowed, not owned: reading your
/// own book allocates nothing.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct RestingOrder<'a> {
    pub client_order_id: &'a str,
    pub symbol: SymbolId,
    pub side: Side,
    pub kind: OrderKind,
    pub qty: f64,
    pub filled_qty: f64,
    pub reduce_only: bool,
    /// The venue has acknowledged it. An unacked order is still out there —
    /// it may rest, or the reply may simply not have arrived yet.
    pub acked: bool,
}

impl RestingOrder<'_> {
    /// Resting limit price, or `None` for a market order in flight.
    pub fn px(&self) -> Option<f64> {
        match self.kind {
            OrderKind::Limit { px, .. } => Some(px),
            OrderKind::Market => None,
        }
    }

    pub fn remaining_qty(&self) -> f64 {
        (self.qty - self.filled_qty).max(0.0)
    }
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
    /// The private stream reconnected; updates during the gap may be lost.
    /// The engine must refresh its account view before trusting exposure.
    StreamReset {
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
    /// The request could not be built at all (unknown symbol, non-finite
    /// number). Nothing was sent; retrying the same input cannot succeed.
    #[error("cannot build request: {0}")]
    BadRequest(String),
    #[error("venue transport: {0}")]
    Transport(String),
    #[error("venue rejected ({code}): {message}")]
    Rejected { code: i64, message: String },
    #[error("venue reply unreadable: {0}")]
    BadReply(String),
    #[error("venue credentials missing or malformed: {0}")]
    Credentials(String),
}
