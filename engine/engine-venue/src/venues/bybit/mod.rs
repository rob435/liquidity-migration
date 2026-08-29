//! Bybit v5: USDT perpetuals over HTTPS, and the private order stream.
//!
//! Two realms, demo and mainnet, and the difference between them is real
//! money. [`realm`] is the only module that turns a Bybit realm into a
//! hostname or a credential variable name.

pub mod gateway;
pub mod realm;

mod parse;
mod rest;
mod sign;
mod trade_ws;
mod ws;

pub use gateway::{BybitGateway, BybitInventoryProbe};
pub use realm::VenueRealm;
pub use ws::BybitOrderFeed;

/// The demo credential variables, re-exported for the operator-facing checks
/// that name them. The authority is [`realm::VenueRealm::credential_vars`].
pub const API_KEY_ENV: &str = "BYBIT_DEMO_API_KEY";
pub const API_SECRET_ENV: &str = "BYBIT_DEMO_API_SECRET";

/// Every request is `linear` — USDT perpetuals, the only thing we trade.
pub(crate) const CATEGORY: &str = "linear";
