//! Shared contracts for the execution engine.
//!
//! Every other crate implements or consumes what is defined here. Changing a
//! public signature in this crate is an integration decision, not a local
//! edit — the orchestrating session owns this file set.
//!
//! Numbers: prices and quantities are `f64` end to end (matching the Python
//! research system); they are quantized to the instrument's tick and step
//! only at the venue boundary, via [`quantize`].
//! Time: `recv_ns`-style fields are monotonic nanoseconds from the engine's
//! own clock (comparable to each other, not to wall time); `*_ms` fields are
//! venue wall-clock milliseconds.

pub mod clock;
pub mod ids;
pub mod market;
pub mod orders;
pub mod quantize;
pub mod risk;
pub mod strategy;
pub mod targets;
pub mod wal;

pub use ids::{StrategyId, Symbol, SymbolId, SymbolTable, TimerId};
pub use market::{
    Feed, FeedError, MarketEvent, MarketFeed, MarketState, OrderFeed, Quote, Subscription, Ticker,
};
pub use orders::{
    Action, AmendSpec, Intent, InstrumentRule, OrderAck, OrderKind, OrderRequest, OrderUpdate,
    RestingOrder, Side, StopSpec, TimeInForce, VenueError, VenueOrder, WorkPolicy,
};
pub use risk::{AccountView, DenyReason, PositionView, RiskKernel, RiskVerdict};
pub use strategy::{EngineEvent, Strategy, StrategyCtx};
pub use targets::{BookTarget, TargetBook};
pub use wal::{Wal, WalError, WalRecord};

/// Which venue account a gateway's credentials actually reach, as the venue
/// itself reports it.
///
/// Read from the venue rather than from config on purpose: config says which
/// key to sign with, and only the venue can say whose account that key opens.
/// Two engines pointed at one account by two different config files have to
/// arrive at the same answer here, because this is what names the lock that
/// keeps them from both sending.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccountIdentity {
    /// The venue's own account number, as a decimal string.
    pub user_id: String,
    /// Which venue the account lives on: `demo` or `mainnet`.
    pub realm: String,
}

/// What a venue can actually do. Venues differ in kind, not just in address:
/// one holds a position-level stop for you, the next expects you to work the
/// exit yourself. The engine reads these and refuses an action the venue
/// cannot honour, rather than quietly substituting a different trade.
///
/// Every field is stated by the adapter. There is no default: a wrong guess
/// here is a strategy believing it has a stop it does not have.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct VenueCaps {
    /// The venue keeps a position-level stop the engine can set and move
    /// (Bybit's full-position trading stop). False means a stop has to be
    /// worked as an ordinary reduce-only order by whoever wants one.
    pub native_position_stop: bool,
    /// A resting order can be repriced or resized in place, keeping its
    /// venue identity.
    pub amend_in_place: bool,
    /// Post-only (maker-or-cancel) is honoured on limit orders.
    pub post_only: bool,
    /// Orders may be sent in batches within one request.
    pub batch_orders: bool,
    /// The venue's margin leverage for a symbol can be set by the engine.
    /// False means whatever leverage the symbol already carries is what an
    /// order will post margin at, and the engine has no say in it.
    pub set_leverage: bool,
}

/// A venue gateway: signs and sends orders, attaches stops, reads account
/// state. One implementation per venue; the engine picks one by name at
/// assembly and never learns which it got.
///
/// Implementations reach practice venues only. Whether a given adapter is
/// allowed to touch real money is that adapter's own decision, made in its
/// own crate — this trait deliberately cannot express an endpoint.
#[allow(async_fn_in_trait)]
pub trait VenueGateway {
    /// What this venue can do. Read before asking it for anything exotic.
    fn caps(&self) -> VenueCaps;
    /// Whose account these credentials open. Asked once at boot, before
    /// anything is sent: it is what the single-writer lock is named after, so
    /// an engine that cannot answer it does not know what it would be
    /// stepping on.
    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError>;
    /// Place an order. The caller has already made the intent durable.
    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError>;
    /// Cancel by the engine's own client order id.
    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError>;
    /// Reprice or resize a resting order in place. Only called when
    /// [`VenueCaps::amend_in_place`] is true.
    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError>;
    /// Attach or move a position stop (stop-loss trigger price). Only called
    /// when [`VenueCaps::native_position_stop`] is true.
    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError>;
    /// Set the venue's margin leverage for a symbol, both sides. Only called
    /// when [`VenueCaps::set_leverage`] is true, and only before an order that
    /// would increase exposure.
    ///
    /// Leverage decides how much margin a position posts, so an engine that
    /// cannot set it is trading at whatever the last person to touch the
    /// symbol chose. Setting it to the value it already holds must succeed:
    /// venues tend to report that as an error, and it is not one.
    ///
    /// The default refuses, so a venue that has not implemented it cannot
    /// quietly claim the leverage was applied.
    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        let _ = (symbol, leverage);
        Err(VenueError::BadRequest(
            "this venue cannot set leverage, and said so in its caps".to_string(),
        ))
    }
    /// Current positions and equity. On Bybit this is two venue reads
    /// (wallet and positions), issued together.
    async fn account_view(&mut self) -> Result<AccountView, VenueError>;
    /// Tick size, quantity step, and minimums for every tradable symbol.
    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError>;
    /// Every order the venue is currently working on this account, whoever
    /// placed it. Read at boot: the log says what this engine sent, and only
    /// the venue can say what is actually out there.
    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError>;
}
