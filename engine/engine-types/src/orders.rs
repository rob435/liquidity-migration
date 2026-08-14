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

/// Rest an entry at the touch and work it, instead of crossing the spread and
/// paying the taker fee. Attached to an [`Intent`] as `work`; the engine's own
/// supervisor does the working, so no strategy writes a repricing loop.
///
/// Every number here was measured in the Python fleet's quote-forge night
/// replay (34 symbols, 199,785 paired attempts): the recipe came out
/// 0.36 bp per entry cheaper than plainly joining the touch and repricing.
/// The defaults are that recipe.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct WorkPolicy {
    /// How long the order may rest before it crosses the spread and takes
    /// whatever is left.
    pub window_ms: u64,
    /// The floor on how often the order is moved. Also paces the retries of a
    /// cross and of the cancel that follows one.
    pub reprice_ms: u64,
    /// After the cross, how long to wait for the rest to fill before pulling
    /// the order and letting the strategy decide again.
    pub cross_grace_ms: u64,
    /// How many moves are budgeted. Soft: past `urgency_join_frac` of the
    /// window the escalation outranks it, because the budget is a schedule
    /// and not a protection.
    pub max_amends: u32,
    /// Book lean at or above this rests one tick inside the spread. Zero
    /// turns that off.
    pub improve_lean: f64,
    /// Book lean at or below minus this rests one tick behind the touch.
    /// Zero turns that off.
    pub back_lean: f64,
    /// Past this fraction of the window, never rest behind the touch.
    pub urgency_join_frac: f64,
    /// Past this fraction of the window, rest inside the spread when there is
    /// room for it.
    pub urgency_improve_frac: f64,
    /// The fee term in the early cross: leave patience once the market has
    /// run against the decision by more than twice the half-spread plus this.
    /// Zero turns the early cross off. Measured on the same night replay:
    /// making this trigger MORE sensitive was the only dial that was clearly
    /// harmful.
    pub drift_cross_fee_bp: f64,
}

impl Default for WorkPolicy {
    fn default() -> Self {
        WorkPolicy {
            window_ms: 120_000,
            reprice_ms: 3_000,
            cross_grace_ms: 20_000,
            max_amends: 8,
            improve_lean: 0.15,
            back_lean: 0.15,
            urgency_join_frac: 0.5,
            urgency_improve_frac: 0.85,
            drift_cross_fee_bp: 5.5,
        }
    }
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
    /// Rest this entry and let the engine work it. `None` sends the order
    /// exactly as written, which is what every strategy did before this
    /// field existed — and what an exit always gets, whatever it asks for.
    /// Defaulted on the way in so a log written before the field existed
    /// still replays.
    #[serde(default)]
    pub work: Option<WorkPolicy>,
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

/// One order the venue says is working, as the venue says it.
///
/// Not [`RestingOrder`]: that is the engine's own picture, built from its log
/// and borrowed from it. This is the venue's answer to "what have you got",
/// which is the only way to learn about an order the log never saw — one
/// placed by hand, by another process, or by the exchange itself. The symbol
/// is the venue's own spelling, because an order in a symbol no strategy
/// subscribed to has no id here and is still worth knowing about.
#[derive(Clone, Debug, PartialEq)]
pub struct VenueOrder {
    /// The venue calls this `orderLinkId`. Empty for orders the exchange
    /// created itself, above all the stop attached to a position.
    pub client_order_id: String,
    pub symbol: String,
    pub side: Side,
    pub qty: f64,
    pub filled_qty: f64,
    pub reduce_only: bool,
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
