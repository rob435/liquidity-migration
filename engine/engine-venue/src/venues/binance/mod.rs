//! Binance USDT-margined perpetual futures over HTTPS, and the listen-key
//! private stream.
//!
//! Two realms. The testnet carries the full private stream, but engine runs
//! remain blocked until the separate entry-plus-Algo-stop lifecycle has been
//! observed there with credentials. [`realm`] is the only place the hosts
//! and environment variable names are written down.
//!
//! Three things about this venue differ in kind from the others:
//!
//! - **A refusal is an HTTP status code**, not a field inside a 200 envelope;
//!   [`parse`] recovers the venue's `{code, msg}` from the transport error.
//! - **The position stop is a close-position algo order** the venue keeps,
//!   not a field on the position — so "is this position protected" is
//!   answered by reading the open algo orders.
//! - **The account name comes from the signed balance read.** Every asset row
//!   carries the same unique account alias; identity refuses an empty or
//!   inconsistent reply rather than naming the single-writer lease locally.

pub mod gateway;
pub mod realm;

mod parse;
mod rest;
mod sign;
mod ws;

pub use gateway::BinanceGateway;
pub use realm::BinanceRealm;
pub use ws::BinanceOrderFeed;

/// What this venue is called in an account identity, a lease file name, and
/// the engine's heartbeat.
pub const VENUE_NAME: &str = "binance";
