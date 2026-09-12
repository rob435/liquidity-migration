use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, Symbol, SymbolId};
use crate::strategy::{StrategyCheckpoint, StrategyEvent};

#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
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

/// A close the venue itself started on the position, named from the venue's
/// own row. It carries no `orderLinkId`, because no order of ours asked for it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ForcedClose {
    StopLoss,
    TakeProfit,
    Liquidation,
    AutoDeleverage,
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
    /// Fee savings can justify joining a one-tick spread. Old WAL policies
    /// retain their spread filter when this field is absent.
    #[serde(default)]
    pub rest_on_tight_spread: bool,
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
            rest_on_tight_spread: false,
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

impl WorkPolicy {
    pub fn passive_entry_30s() -> Self {
        Self {
            rest_on_tight_spread: true,
            window_ms: 30_000,
            reprice_ms: 15_000,
            max_amends: 1,
            improve_lean: 0.0,
            back_lean: 0.0,
            urgency_join_frac: 1.0,
            urgency_improve_frac: 1.0,
            ..Self::default()
        }
    }
}

/// What a strategy asks for. Strategies never build venue payloads; they
/// emit intents and the engine does the rest.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct IntentPrices {
    pub limit_price: Option<crate::numeric::Exact>,
    pub stop_trigger_price: Option<crate::numeric::Exact>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Intent {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    /// Canonical units; `qty` is only the compatibility projection when present.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_quantity: Option<Box<crate::numeric::Exact>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_prices: Option<Box<IntentPrices>>,
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

impl Intent {
    pub fn limit_price(&self) -> Result<Option<crate::numeric::Exact>, crate::numeric::ExactError> {
        let canonical = self
            .exact_prices
            .as_ref()
            .and_then(|prices| prices.limit_price.as_ref());
        let projection = match self.kind {
            OrderKind::Limit { px, .. } => Some(px),
            OrderKind::Market => None,
        };
        Self::price(canonical, projection)
    }

    pub fn stop_price(&self) -> Result<Option<crate::numeric::Exact>, crate::numeric::ExactError> {
        let canonical = self
            .exact_prices
            .as_ref()
            .and_then(|prices| prices.stop_trigger_price.as_ref());
        Self::price(canonical, self.stop.map(|stop| stop.trigger_px))
    }

    pub fn validate_price_projection(&self) -> Result<(), crate::numeric::ExactError> {
        self.limit_price()?;
        self.stop_price()?;
        Ok(())
    }

    fn price(
        canonical: Option<&crate::numeric::Exact>,
        projection: Option<f64>,
    ) -> Result<Option<crate::numeric::Exact>, crate::numeric::ExactError> {
        use crate::numeric::{Exact, ExactError};
        let Some(projection) = projection else {
            return if canonical.is_some() {
                Err(ExactError::InvalidProjection)
            } else {
                Ok(None)
            };
        };
        let value = if let Some(canonical) = canonical {
            canonical.validate_storage()?;
            if canonical.to_f64()? != projection {
                return Err(ExactError::InvalidProjection);
            }
            canonical.clone()
        } else {
            Exact::parse_decimal(&projection.to_string())?
        };
        if !value.is_positive() {
            return Err(ExactError::InvalidProjection);
        }
        Ok(Some(value))
    }

    pub fn quantity(&self) -> Result<crate::numeric::Exact, crate::numeric::ExactError> {
        use crate::numeric::{Exact, ExactError};
        if !self.qty.is_finite() {
            return Err(ExactError::NonFinite);
        }
        if self.qty <= 0.0 {
            return Err(ExactError::InvalidProjection);
        }
        if let Some(quantity) = &self.exact_quantity {
            quantity.validate_storage()?;
            if !quantity.is_positive() || quantity.to_f64()? != self.qty {
                return Err(ExactError::InvalidProjection);
            }
            Ok((**quantity).clone())
        } else {
            Exact::parse_decimal(&self.qty.to_string())
        }
    }
}

/// A new price and/or size for an order already resting at the venue.
/// `None` leaves that field as it is.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AmendSpec {
    pub px: Option<f64>,
    pub qty: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_terms: Option<Box<crate::order_terms::ExactAmendTerms>>,
}

/// One amendment as the venue task hands it to an adapter: the order, the
/// new terms, and the permission it was queued under. `authority: None` is an
/// amendment nothing may refuse at the send boundary.
#[derive(Clone, Debug, PartialEq)]
pub struct AmendRequest {
    pub symbol: SymbolId,
    pub client_order_id: String,
    pub spec: AmendSpec,
    pub authority: Option<crate::authority::CommandAuthority>,
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
    /// Replace one strategy symbol's durable checkpoint. The engine context
    /// overwrites `strategy`, exactly as it does for an intent owner.
    SetStrategyCheckpoint {
        strategy: StrategyId,
        symbol: SymbolId,
        checkpoint: StrategyCheckpoint,
    },
    /// Replace the durable checkpoint for this whole sleeve. The engine
    /// context overwrites `strategy`; there is no sentinel symbol.
    SetStrategyGlobalCheckpoint {
        strategy: StrategyId,
        checkpoint: StrategyCheckpoint,
    },
    /// Publish one immutable event to another configured strategy. The engine
    /// context overwrites `event.source` before it reaches the WAL.
    PublishStrategyEvent {
        event: StrategyEvent,
    },
    /// Mark a cross-sleeve event consumed. The engine context overwrites
    /// `destination`, so only the addressed strategy can remove it.
    ConsumeStrategyEvent {
        source: StrategyId,
        destination: StrategyId,
        event_id: String,
    },
    /// Mark one durable external observation consumed. The engine context
    /// overwrites `strategy`, so another sleeve cannot acknowledge it.
    ConsumeSignalObservation {
        strategy: StrategyId,
        source: String,
        sequence: u64,
        observation_id: String,
    },
    /// Terminal rejection is durable and distinct from successful consumption.
    RejectSignalObservation {
        strategy: StrategyId,
        source: String,
        sequence: u64,
        observation_id: String,
        reason: String,
    },
    /// Acknowledge one replayable runtime control after the reducer's own
    /// checkpoint/effects have entered the FIFO. The context overwrites the
    /// strategy id.
    ConsumeRuntimeControl {
        strategy: StrategyId,
        request_id: String,
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
            Action::SetStrategyCheckpoint { .. } => true,
            Action::SetStrategyGlobalCheckpoint { .. }
            | Action::PublishStrategyEvent { .. }
            | Action::ConsumeStrategyEvent { .. }
            | Action::ConsumeSignalObservation { .. }
            | Action::RejectSignalObservation { .. }
            | Action::ConsumeRuntimeControl { .. } => true,
        }
    }

    pub fn symbol(&self) -> Option<SymbolId> {
        match self {
            Action::Place(intent) => Some(intent.symbol),
            Action::Cancel { symbol, .. }
            | Action::Amend { symbol, .. }
            | Action::SetStop { symbol, .. }
            | Action::SetStrategyCheckpoint { symbol, .. } => Some(*symbol),
            Action::RecordQuoteFill { features } => Some(features.symbol),
            Action::SetStrategyGlobalCheckpoint { .. }
            | Action::PublishStrategyEvent { .. }
            | Action::ConsumeStrategyEvent { .. }
            | Action::ConsumeSignalObservation { .. }
            | Action::RejectSignalObservation { .. }
            | Action::ConsumeRuntimeControl { .. } => None,
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
/// interning. `product` names the venue category that was scanned; a
/// `wallet_dust` or `asset_account_dust:*` product is a holding the venue
/// values under one dollar, which no order can close.
#[derive(Clone, Debug, PartialEq)]
pub struct AccountPosition {
    pub product: String,
    pub symbol: String,
    pub side: Side,
    pub qty: f64,
}

impl AccountPosition {
    pub fn is_dust(&self) -> bool {
        self.product == "wallet_dust" || self.product.starts_with("asset_account_dust:")
    }
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
    /// Flat means no position, no working order, and nothing held beyond dust.
    pub fn is_flat(&self) -> bool {
        self.positions.iter().all(AccountPosition::is_dust) && self.open_orders.is_empty()
    }
}

/// One execution from the venue's own history, as the venue says it.
///
/// The private stream forgets: a fill that lands while the engine is down or
/// while the stream is reconnecting is never delivered. This is the venue's
/// answer to "what traded on this account between these times", which is the
/// only way the log can learn those fills after the fact. The symbol is the
/// venue's own spelling for the same reason as [`VenueOrder`]'s.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct VenueExecution {
    /// The venue's own id for this execution — unique per fill, and the
    /// dedup key against reading the same history twice.
    pub exec_id: String,
    /// The venue calls this `orderLinkId`. Empty for executions the engine
    /// never ordered: a venue-attached stop firing, a hand trade. A blank id
    /// with a `forced_close` below is charged to the sleeve the close reduces.
    pub client_order_id: String,
    pub symbol: String,
    pub side: Side,
    pub qty: f64,
    pub px: f64,
    /// What the venue charged in account currency. `None` means the venue did
    /// not state it; zero is reserved for a fee the venue explicitly stated
    /// was zero.
    pub fee: Option<f64>,
    pub amounts: Option<crate::numeric::ExecutionAmounts>,
    pub is_maker: bool,
    /// The venue's own reason for closing the position, when it says one.
    pub forced_close: Option<ForcedClose>,
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
    /// Canonical ledger remainder projected once; None is legacy compatibility input.
    pub remaining_qty: Option<f64>,
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
        self.remaining_qty
            .unwrap_or_else(|| (self.qty - self.filled_qty).max(0.0))
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SleeveOrderEffect {
    Increase { stop: StopSpec },
    Reduce,
    EmergencyNetReduction { emergency_id: u64 },
}

/// A risk-approved order on its way to the venue. Quantities and prices are
/// already quantized to the instrument's step and tick.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OrderRequest {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_terms: Option<Box<crate::order_terms::ExactOrderTerms>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sleeve_effect: Option<SleeveOrderEffect>,
    /// Engine-minted, unique per boot, recorded in the log before send.
    pub client_order_id: String,
    /// Durable compatibility slot; `sleeve_owner()` is None for engine-owned net closes.
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    pub kind: OrderKind,
    pub stop: Option<StopSpec>,
    pub reduce_only: bool,
    /// Close the complete venue position. The requested quantity remains the
    /// accounting quantity even when an adapter uses a venue-specific wire
    /// sentinel such as Bybit's zero-quantity close.
    #[serde(default)]
    pub close_position: bool,
}

impl OrderRequest {
    pub fn canonical_intent_prices(&self) -> Option<Box<IntentPrices>> {
        self.exact_terms.as_deref().map(|terms| {
            Box::new(IntentPrices {
                limit_price: terms.limit_price.clone(),
                stop_trigger_price: terms.stop_trigger_price.clone(),
            })
        })
    }
    pub fn is_portfolio_reduction(&self) -> bool {
        matches!(
            self.sleeve_effect,
            Some(SleeveOrderEffect::EmergencyNetReduction { .. })
        )
    }

    pub fn sleeve_owner(&self) -> Option<StrategyId> {
        (!self.is_portfolio_reduction()).then_some(self.strategy)
    }

    pub fn is_sleeve_reduction(&self) -> bool {
        match self.sleeve_effect {
            Some(SleeveOrderEffect::Reduce | SleeveOrderEffect::EmergencyNetReduction { .. }) => {
                true
            }
            Some(SleeveOrderEffect::Increase { .. }) => false,
            None => self.reduce_only,
        }
    }

    pub fn sleeve_stop(&self) -> Option<StopSpec> {
        match self.sleeve_effect {
            Some(SleeveOrderEffect::Reduce | SleeveOrderEffect::EmergencyNetReduction { .. }) => {
                None
            }
            Some(SleeveOrderEffect::Increase { stop }) => Some(stop),
            None => self.stop,
        }
    }
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
    /// Canonical ledger remainder projected once; None is legacy compatibility input.
    pub remaining_qty: Option<f64>,
    pub reduce_only: bool,
}

impl OrderFacts {
    pub fn remaining_qty(&self) -> f64 {
        self.remaining_qty
            .unwrap_or_else(|| (self.qty - self.filled_qty).max(0.0))
    }
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
        #[serde(default, skip_serializing_if = "Option::is_none")]
        allocation: Option<Box<crate::execution_allocation::ExecutionAllocation>>,
        /// The venue's unique identity for this execution. Empty only when a
        /// legacy log predates execution identity.
        #[serde(default)]
        exec_id: String,
        /// Empty for a venue-native position stop, which belongs to the
        /// symbol's current position rather than to the entry order. A blank
        /// id with a `forced_close` below is charged to the sleeve the close
        /// reduces.
        client_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        /// What the venue charged in account currency. Older log records carry
        /// a number here and deserialize as `Some`; an absent field is unknown.
        #[serde(default)]
        fee: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        amounts: Option<Box<crate::numeric::ExecutionAmounts>>,
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
        /// The venue's own reason for closing the position, when it says one.
        ///
        /// Defaulted on the way in, so a log written before this field existed
        /// still replays; `None` there reads as a fill no venue reason was
        /// recorded for, which is what those records hold.
        #[serde(default)]
        forced_close: Option<ForcedClose>,
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
        #[serde(default, skip_serializing_if = "Option::is_none")]
        exact_terms: Option<Box<crate::order_terms::ExactAmendedTerms>>,
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
///
/// What a strategy sizes against. Sizing is statistical and works in floats;
/// order terms are quantized against [`crate::numeric::ExactInstrumentSpec`]
/// and never against this. Build it with [`InstrumentRule::from_exact`] so
/// the venue's own decimals are parsed once and the two cannot drift.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct InstrumentRule {
    pub tick_size: f64,
    pub qty_step: f64,
    pub min_qty: f64,
    pub min_notional: f64,
}

impl InstrumentRule {
    /// The sizing rule this instrument's exact spec implies.
    ///
    /// An absent minimum is no minimum, which is zero. An absent tick or step
    /// has no float spelling to offer and yields `None`: a strategy sizing
    /// against a guessed grid would round to a quantity the venue refuses.
    /// The finer of the limit and market quantity steps is the one reported,
    /// so sizing never proposes a quantity that is illegal on either.
    pub fn from_exact(spec: &crate::numeric::ExactInstrumentSpec) -> Option<Self> {
        fn positive(value: Option<&crate::numeric::Exact>) -> Option<f64> {
            let value = value?.to_f64().ok()?;
            (value.is_finite() && value > 0.0).then_some(value)
        }
        fn at_least_zero(value: Option<&crate::numeric::Exact>) -> Option<f64> {
            match value {
                None => Some(0.0),
                Some(value) => {
                    let value = value.to_f64().ok()?;
                    (value.is_finite() && value >= 0.0).then_some(value)
                }
            }
        }
        let step = [
            positive(spec.qty_step.as_ref()),
            positive(spec.market_qty_step.as_ref()),
        ];
        Some(Self {
            tick_size: positive(spec.tick_size.as_ref())?,
            qty_step: step.into_iter().flatten().min_by(f64::total_cmp)?,
            min_qty: at_least_zero(spec.min_qty.as_ref())?,
            min_notional: at_least_zero(spec.min_notional.as_ref())?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stop_fill() -> OrderUpdate {
        OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "exec-1".to_string(),
            client_order_id: String::new(),
            symbol: SymbolId(3),
            side: Side::Sell,
            qty: 10.0,
            px: 1.5,
            fee: Some(0.01),
            is_maker: false,
            forced_close: Some(ForcedClose::StopLoss),
            venue_ts_ms: 7,
            recv_ns: 8,
        }
    }

    #[test]
    fn a_fill_written_before_the_venue_reason_still_replays() {
        let mut encoded = serde_json::to_value(stop_fill()).expect("serialize a fill");
        assert_eq!(
            encoded["Fill"]["forced_close"], "StopLoss",
            "it round trips"
        );
        encoded["Fill"]
            .as_object_mut()
            .expect("a fill is an object")
            .remove("forced_close");
        assert!(matches!(
            serde_json::from_value::<OrderUpdate>(encoded).expect("an older fill reads"),
            OrderUpdate::Fill {
                forced_close: None,
                ..
            }
        ));
    }
}

#[derive(Debug, thiserror::Error)]
pub enum VenueError {
    #[error("venue capability unavailable: {0}")]
    Unsupported(String),
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

/// A status lookup is disposition evidence; executions remain fill authority.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct OrderLookupRow {
    pub symbol: Symbol,
    pub client_order_id: String,
    pub venue_order_id: String,
    pub filled_qty: crate::numeric::ExactNumber,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum TerminalOrderStatus {
    Filled,
    Cancelled,
    Rejected,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderLookup {
    Working(OrderLookupRow),
    Terminal {
        status: TerminalOrderStatus,
        row: OrderLookupRow,
    },
    /// Requires endpoint-specific proof that the request was never accepted.
    NeverAccepted,
    Unknown {
        reason: String,
    },
    Unavailable,
}

/// Account and execution reads have no ownership of the venue mutation path.
#[crate::async_trait]
pub trait AccountRecoveryClient: Send + Sync + 'static {
    fn execution_history_progress(&self) -> Option<std::sync::Arc<std::sync::atomic::AtomicU64>> {
        None
    }
    fn install_instrument_catalog(
        &self,
        _catalog: &InstrumentCatalog,
    ) -> Result<(), crate::VenueError> {
        Ok(())
    }
    async fn account_view(
        &self,
        symbols: &[crate::Symbol],
    ) -> Result<crate::AccountView, crate::VenueError>;
    async fn executions(
        &self,
        symbols: &[crate::Symbol],
        start_ms: i64,
        end_ms: i64,
    ) -> Result<crate::ExecutionHistory, crate::VenueError>;
}

/// Read-only client with no ownership of the serialized venue mutation path.
#[crate::async_trait]
pub trait OrderLookupClient: Send + Sync + 'static {
    async fn lookup(
        &self,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<OrderLookup, crate::VenueError>;
}

#[derive(Clone, Debug, Default)]
pub struct InstrumentCatalog {
    pub cache: Option<std::sync::Arc<dyn InstrumentCatalogCache>>,
    pub rules: Vec<(crate::Symbol, crate::InstrumentRule)>,
    pub specs: Vec<(crate::Symbol, crate::numeric::ExactInstrumentSpec)>,
}
#[crate::async_trait]
pub trait InstrumentCatalogClient: Send + Sync + 'static {
    async fn fetch(&self) -> Result<InstrumentCatalog, VenueError>;
}

pub trait InstrumentCatalogCache: std::any::Any + Send + Sync + std::fmt::Debug {
    fn as_any(&self) -> &dyn std::any::Any;
    fn checkpoint(&self) -> Result<InstrumentCatalogCacheSnapshot, VenueError>;
    fn retain_previous(
        &self,
        checkpoint: &InstrumentCatalogCheckpoint,
    ) -> Result<InstrumentCatalog, VenueError>;
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct InstrumentCatalogCheckpoint {
    pub schema_version: u32,
    pub rules: Vec<(crate::Symbol, crate::InstrumentRule)>,
    pub specs: Vec<(crate::Symbol, crate::numeric::ExactInstrumentSpec)>,
    pub cache: InstrumentCatalogCacheSnapshot,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct InstrumentCatalogCacheSnapshot {
    pub kind: String,
    pub payload: Vec<u8>,
}
impl InstrumentCatalogCheckpoint {
    pub fn validate_bounds(&self) -> Result<(), VenueError> {
        if self.schema_version != 1
            || self.cache.payload.len() > 64 * 1024 * 1024
            || self.rules.len() > 100_000
            || self.specs.len() > 100_000
        {
            return Err(VenueError::BadReply(
                "unsupported or oversized instrument catalog checkpoint".into(),
            ));
        }
        if self.cache.kind.len() > 64
            || self.rules.iter().any(|(name, _)| name.len() > 256)
            || self.specs.iter().any(|(name, _)| name.len() > 256)
        {
            return Err(VenueError::BadReply(
                "oversized instrument catalog identifiers".into(),
            ));
        }
        struct LimitedBytes(usize);
        impl std::io::Write for LimitedBytes {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.0 = self
                    .0
                    .checked_add(bytes.len())
                    .ok_or_else(|| std::io::Error::other("catalog size overflow"))?;
                if self.0 > 64 * 1024 * 1024 {
                    return Err(std::io::Error::other(
                        "catalog checkpoint exceeds serialized size limit",
                    ));
                }
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        serde_json::to_writer(LimitedBytes(0), self)
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        Ok(())
    }
}
impl InstrumentCatalog {
    pub fn retain_previous(
        &self,
        checkpoint: &InstrumentCatalogCheckpoint,
    ) -> Result<Self, VenueError> {
        self.cache
            .as_ref()
            .ok_or_else(|| VenueError::Unsupported("catalog has no native cache".into()))?
            .retain_previous(checkpoint)
    }
    pub fn checkpoint(&self) -> Result<InstrumentCatalogCheckpoint, VenueError> {
        let checkpoint = InstrumentCatalogCheckpoint {
            schema_version: 1,
            rules: self.rules.clone(),
            specs: self.specs.clone(),
            cache: self
                .cache
                .as_ref()
                .ok_or_else(|| VenueError::Unsupported("catalog has no native cache".into()))?
                .checkpoint()?,
        };
        checkpoint.validate_bounds()?;
        Ok(checkpoint)
    }
}

#[cfg(test)]
mod inventory_flatness_tests {
    use super::*;

    fn row(product: &str, symbol: &str) -> AccountPosition {
        AccountPosition {
            product: product.into(),
            symbol: symbol.into(),
            side: Side::Buy,
            qty: 0.0001,
        }
    }

    #[test]
    fn dust_alone_reads_flat_and_anything_else_does_not() {
        let mut inventory = AccountInventory {
            scope: "test".into(),
            positions: vec![
                row("wallet_dust", "MNT"),
                row("asset_account_dust:UnifiedTradingAccount:CRYPTO", "MNT"),
            ],
            open_orders: Vec::new(),
            observed_ms: 1,
        };
        assert!(inventory.is_flat());
        inventory.positions.push(row("wallet_asset", "SOL"));
        assert!(!inventory.is_flat());
        inventory.positions.pop();
        inventory.positions.push(row("linear", "INJUSDT"));
        assert!(!inventory.is_flat());
        inventory.positions.pop();
        inventory.open_orders.push(AccountOrder {
            product: "linear".into(),
            symbol: "INJUSDT".into(),
            client_order_id: String::new(),
        });
        assert!(!inventory.is_flat());
    }
}
