//! Public market data for every venue the engine can trade, parsed once into
//! the engine's contract types.
//!
//! One module per venue — [`binance`], [`bybit`], [`hyperliquid`], [`lighter`],
//! [`mexc`], [`variational`] — the same six the order crate is laid out by, so an
//! exchange is one folder in each.
//!
//! The Bybit stream is public — no credentials — and it is the same price feed
//! the demo account trades against. [`binance`], [`hyperliquid`], [`lighter`],
//! [`mexc`] and [`variational`] are the other five venues' feeds, and [`feeds`] is the one
//! type the engine holds:
//! built from the same venue name that built the gateway, so orders and prices
//! cannot come from two different venues.
//!
//! No host is written down in this crate. Every venue's hostname lives in that
//! venue's own realm table in `engine-venue`, which has a fence that reads its
//! source back — a host spelled out here would be one that fence never sees.

#[cfg(feature = "binance")]
pub mod binance;
#[cfg(feature = "bybit")]
pub mod bybit;
pub mod feeds;
#[cfg(feature = "hyperliquid")]
pub mod hyperliquid;
#[cfg(feature = "lighter")]
pub mod lighter;
#[cfg(feature = "mexc")]
pub mod mexc;
#[cfg(feature = "variational")]
pub mod variational;

#[cfg(any(
    feature = "binance",
    feature = "hyperliquid",
    feature = "lighter",
    feature = "mexc",
    feature = "variational"
))]
mod symbols;

#[cfg(feature = "binance")]
pub use binance::BinancePublicFeed;
#[cfg(feature = "bybit")]
pub use bybit::BybitPublicFeed;
pub use feeds::MarketFeeds;
#[cfg(feature = "hyperliquid")]
pub use hyperliquid::HyperliquidPublicFeed;
#[cfg(feature = "lighter")]
pub use lighter::LighterPublicFeed;
#[cfg(feature = "mexc")]
pub use mexc::MexcPublicFeed;
#[cfg(feature = "variational")]
pub use variational::VariationalPublicFeed;

#[cfg(all(test, feature = "bybit"))]
#[path = "../../test-support/io.rs"]
mod test_io;
