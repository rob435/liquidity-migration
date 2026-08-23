//! MEXC futures: USDT perpetuals over HTTPS, and the private order stream.
//!
//! One realm, and it is funded. MEXC publishes no testnet host for the futures
//! API, so there is no practice account to point this engine at — [`realm`] is
//! the only place its host and credential variable names are written down, and
//! it holds two of them, because REST and the websocket are on different hosts.
//!
//! Two things about this venue differ in kind from the others and both live in
//! [`contracts`]: orders are quoted in *contracts* rather than coins, and the
//! venue spells a symbol `BTC_USDT` where the engine says `BTCUSDT`.

pub mod contracts;
pub mod gateway;
pub mod public;
pub mod realm;

mod parse;
mod rest;
mod sign;
mod ws;

pub use gateway::MexcGateway;
pub use ws::MexcOrderFeed;
pub use realm::MexcRealm;

/// What this venue is called in an account identity, a lease file name, and
/// the engine's heartbeat.
pub const VENUE_NAME: &str = "mexc";
