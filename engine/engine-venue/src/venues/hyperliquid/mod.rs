//! Hyperliquid: perpetuals on its own L1, signed with the account's own wallet
//! key rather than an API secret.
//!
//! [`realm`] is the only module that names a Hyperliquid host or a credential
//! variable. What is different in kind from Bybit is written where it bites:
//! [`gateway`] for the missing market order and the separate stop, [`sign`]
//! for the four-step action signature, [`cloid`] for the sixteen bytes the
//! engine's own order id has to fit into.

pub mod gateway;
pub mod realm;

mod assets;
mod cloid;
mod msgpack;
mod parse;
mod sign;
mod wire;
mod ws;

pub use gateway::HyperliquidGateway;
pub use realm::HyperliquidRealm;
pub use ws::HyperliquidOrderFeed;

/// What this venue is called in an account identity, a lease file name, and
/// the engine's heartbeat.
pub const VENUE_NAME: &str = "hyperliquid";
