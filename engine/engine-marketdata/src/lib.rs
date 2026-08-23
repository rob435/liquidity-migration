//! Public market data for every venue the engine can trade, parsed once into
//! the engine's contract types.
//!
//! One module per venue — [`bybit`], [`hyperliquid`], [`lighter`],
//! [`variational`] — the same four the order crate is laid out by, so an
//! exchange is one folder in each.
//!
//! The Bybit stream is public — no credentials — and it is the same price feed
//! the demo account trades against. [`hyperliquid`], [`lighter`] and
//! [`variational`] are the other three venues' feeds, and [`feeds`] is the one
//! type the engine holds:
//! built from the same venue name that built the gateway, so orders and prices
//! cannot come from two different venues.
//!
//! No host is written down in this crate. Every venue's hostname lives in that
//! venue's own realm table in `engine-venue`, which has a fence that reads its
//! source back — a host spelled out here would be one that fence never sees.

pub mod bybit;
pub mod feeds;
pub mod hyperliquid;
pub mod lighter;
pub mod mexc;
pub mod variational;

mod symbols;

pub use bybit::BybitPublicFeed;
pub use feeds::MarketFeeds;
pub use hyperliquid::HyperliquidPublicFeed;
pub use lighter::LighterPublicFeed;
pub use mexc::MexcPublicFeed;
pub use variational::VariationalPublicFeed;
