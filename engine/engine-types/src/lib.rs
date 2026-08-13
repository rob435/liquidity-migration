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

pub mod ids;
pub mod market;
pub mod orders;
pub mod quantize;
pub mod risk;
pub mod strategy;
pub mod wal;

pub use ids::{StrategyId, Symbol, SymbolId, SymbolTable, TimerId};
pub use market::{
    Feed, FeedError, MarketEvent, MarketFeed, MarketState, OrderFeed, Quote, Subscription, Ticker,
};
pub use orders::{
    Intent, InstrumentRule, OrderAck, OrderKind, OrderRequest, OrderUpdate, Side, StopSpec,
    TimeInForce, VenueError,
};
pub use risk::{AccountView, DenyReason, PositionView, RiskKernel, RiskVerdict};
pub use strategy::{EngineEvent, Strategy, StrategyCtx};
pub use wal::{Wal, WalError, WalRecord};

/// A venue gateway: signs and sends orders, attaches stops, reads account
/// state. Implementations talk to the demo venue only; there is deliberately
/// no way to express a mainnet endpoint through this trait.
#[allow(async_fn_in_trait)]
pub trait VenueGateway {
    /// Place an order. The caller has already made the intent durable.
    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError>;
    /// Cancel by the engine's own client order id.
    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError>;
    /// Attach or move a position stop (stop-loss trigger price).
    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError>;
    /// Current positions and equity, one venue round trip.
    async fn account_view(&mut self) -> Result<AccountView, VenueError>;
    /// Tick size, quantity step, and minimums for every tradable symbol.
    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError>;
}
