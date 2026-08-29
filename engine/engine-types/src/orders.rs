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
    ///
    /// Fifteen seconds, which is the cadence every measured arm ran at and
    /// what the amend budget below was sized for. Moving the order more often
    /// buys almost nothing — across a 24-arm tape sweep, chasing at all was
    /// worth 0.20 bp against not chasing — and each move is a signed venue
    /// call.
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
    /// Zero turns the early cross off, and that is what ships.
    ///
    /// Every arm of the tape sweep waited out its window instead, and every
    /// one beat crossing — because a rest that misses costs only 0.94 bp more
    /// than crossing at the start, while one that fills saves 4.18. Giving up
    /// early forfeits the second to avoid the first.
    pub drift_cross_fee_bp: f64,
    /// Rest at the mid the order was decided against, and never move to a
    /// worse price than that. Nothing is bought above, or sold below, the
    /// price the strategy decided on; the cost is every fill the market walks
    /// away from.
    ///
    /// Defaulted on read: the log holds records written before this field
    /// existed, and replay must not fail on them. A required field here is an
    /// engine that cannot boot on its own history.
    #[serde(default)]
    pub hold_decision_px: bool,
    /// When patience runs out, take the order down instead of crossing for
    /// what is left. Without this the price cap above only delays the cross.
    ///
    /// Defaulted on read, for the same reason as the field above.
    #[serde(default)]
    pub give_up_instead_of_crossing: bool,
}

impl Default for WorkPolicy {
    fn default() -> Self {
        WorkPolicy {
            window_ms: 120_000,
            reprice_ms: 15_000,
            cross_grace_ms: 20_000,
            max_amends: 8,
            improve_lean: 0.15,
            back_lean: 0.15,
            urgency_join_frac: 0.5,
            urgency_improve_frac: 0.85,
            drift_cross_fee_bp: 0.0,
            hold_decision_px: false,
            give_up_instead_of_crossing: false,
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
    /// exactly as written, which is what an exit always gets, whatever it
    /// asks for. Defaulted on the way in so a log written before the field
    /// existed still replays.
    #[serde(default)]
    pub work: Option<WorkPolicy>,
    /// The venue margin leverage this size was worked out at.
    ///
    /// `None` means the strategy has no opinion and the symbol keeps whatever
    /// leverage it carries. `Some` means the engine sets it at the venue
    /// before the order goes, because the margin a position posts is notional
    /// divided by this, and a position that posts different margin from the
    /// one the risk kernel priced is not the position anybody agreed to.
    ///
    /// Only honoured on orders that increase exposure: an exit at the wrong
    /// leverage is still an exit, and making it wait on a round trip would be
    /// the wrong trade-off.
    #[serde(default)]
    pub leverage: Option<f64>,
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
    /// Move the venue-native stop on a position already held, without sending
    /// an order. The stop covers the whole position and outlives the process
    /// that asked for it, so a strategy whose stop distance narrows over the
    /// life of a trade has no other way to make that real at the venue.
    ///
    /// The engine refuses one that would move a stop further from the
    /// position than where it stands. A stop that loosens is not a stop.
    SetStop {
        symbol: SymbolId,
        trigger_px: f64,
    },
    /// Persist the market state around one quoter fill. This changes no venue
    /// state; it joins to the ordinary fee and markout records by execution
    /// and client-order id.
    RecordQuoteFill {
        features: QuoteFillFeatures,
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
            // Only ever accepted when it tightens, so it can only cut risk.
            Action::SetStop { .. } => true,
            Action::RecordQuoteFill { .. } => true,
        }
    }

    pub fn symbol(&self) -> SymbolId {
        match self {
            Action::Place(intent) => intent.symbol,
            Action::Cancel { symbol, .. }
            | Action::Amend { symbol, .. }
            | Action::SetStop { symbol, .. } => *symbol,
            Action::RecordQuoteFill { features } => features.symbol,
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

/// One nonzero position from an account-wide inventory, before symbol
/// interning. `product` names the venue category that was scanned.
#[derive(Clone, Debug, PartialEq)]
pub struct AccountPosition {
    pub product: String,
    pub symbol: String,
    pub side: Side,
    pub qty: f64,
}

/// One working order from an account-wide inventory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccountOrder {
    pub product: String,
    pub symbol: String,
    pub client_order_id: String,
}

/// A complete scan of every product surface the venue account can carry.
/// An adapter returns an error when it cannot prove the scan is complete.
#[derive(Clone, Debug, PartialEq)]
pub struct AccountInventory {
    pub scope: String,
    pub positions: Vec<AccountPosition>,
    pub open_orders: Vec<AccountOrder>,
    pub observed_ms: i64,
}

impl AccountInventory {
    pub fn is_flat(&self) -> bool {
        self.positions.is_empty() && self.open_orders.is_empty()
    }
}

/// One execution from the venue's own history, as the venue says it.
///
/// The private stream forgets: a fill that lands while the engine is down or
/// while the stream is reconnecting is never delivered. This is the venue's
/// answer to "what traded on this account between these times", which is the
/// only way the log can learn those fills after the fact. The symbol is the
/// venue's own spelling for the same reason as [`VenueOrder`]'s.
#[derive(Clone, Debug, PartialEq)]
pub struct VenueExecution {
    /// The venue's own id for this execution — unique per fill, and the
    /// dedup key against reading the same history twice.
    pub exec_id: String,
    /// The venue calls this `orderLinkId`. Empty for executions the engine
    /// never ordered: a venue-attached stop firing, a hand trade.
    pub client_order_id: String,
    pub symbol: String,
    pub side: Side,
    pub qty: f64,
    pub px: f64,
    pub fee: f64,
    pub is_maker: bool,
    pub venue_ts_ms: i64,
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
    /// Engine monotonic nanoseconds when the request bytes were handed to
    /// the transport. Zero when an adapter cannot expose that boundary.
    #[serde(default)]
    pub sent_ns: u64,
    /// Engine monotonic nanoseconds when the venue reply was parsed.
    pub ack_ns: u64,
}

/// The log's answer about one order, for a strategy that holds only its id.
/// See `StrategyCtx::order_facts`.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct OrderFacts {
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    pub filled_qty: f64,
    pub reduce_only: bool,
}

/// The market state surrounding one quoter fill.
///
/// Fee and later markouts already live on the fill ledger. This is the
/// decision state they need to be judged against: how one-sided the public
/// flow was, how much nearby book stood behind it, and how valuable the queue
/// looked when the fill became visible.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct QuoteFillFeatures {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub exec_id: String,
    pub client_order_id: String,
    pub side: Side,
    pub is_maker: bool,
    pub recv_ns: u64,
    pub flow_fast: Option<f64>,
    pub flow_slow: Option<f64>,
    pub flow_score: Option<f64>,
    pub last_depth_ratio: Option<f64>,
    pub same_side_depth_usdt: Option<f64>,
    pub spread_bps: Option<f64>,
    pub volatility_bps: Option<f64>,
    pub queue_ahead_usdt: Option<f64>,
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
        /// The venue's unique identity for this execution. Empty only when a
        /// legacy log predates execution identity.
        #[serde(default)]
        exec_id: String,
        /// Empty for a venue-native position stop, which belongs to the
        /// symbol's current position rather than to the entry order.
        client_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        fee: f64,
        /// We were the resting side: somebody else crossed the spread to
        /// trade with us. The venue says so on every execution, and it is the
        /// difference between earning the spread and paying it — so a maker
        /// share is the first number to look at when asking whether the
        /// working supervisor is doing its job.
        ///
        /// Defaulted on the way in, so a log written before this field
        /// existed still replays. False is the honest default: an old log
        /// cannot tell us, and counting an unknown as a maker would flatter
        /// every number computed from it.
        #[serde(default)]
        is_maker: bool,
        venue_ts_ms: i64,
        recv_ns: u64,
    },
    /// Earlier, fee-less fill notice from the venue's fast execution topic.
    /// Strategies may react to it; the ordinary `Fill` remains the only
    /// accounting authority and follows with the fee and full fields.
    FastFill {
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
    /// What price a resting order is working at, in the venue's own words.
    ///
    /// The venue republishes an order whenever it changes without trading,
    /// which is what an applied amend looks like from outside. An amend
    /// acknowledgement says only that the request was taken — never what
    /// price it left the order at — so this is the one place the venue
    /// states it, and the only thing that can end an amend's ambiguity
    /// without pulling the order.
    Amended {
        client_order_id: String,
        px: f64,
        /// What is still working. A fill that landed while the amend was in
        /// flight shows here as a smaller number.
        qty: f64,
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
