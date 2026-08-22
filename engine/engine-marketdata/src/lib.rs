//! Public market data for every venue the engine can trade, parsed once into
//! the engine's contract types.
//!
//! Three layers, split so the parse can be tested without a socket:
//!
//! - [`parse`] turns raw bytes into typed frames. Pure, no state.
//! - [`state`] keeps the per-symbol book and ticker, and decides whether a
//!   frame is an event, nothing, or a lost-continuity resync.
//! - [`feed`] owns the socket: subscribe, keep-alive, reconnect, backoff.
//!
//! The Bybit stream is public — no credentials — and it is the same price feed
//! the demo account trades against. [`hyperliquid`] and [`variational`] are the
//! other two venues' feeds, and [`feeds`] is the one type the engine holds:
//! built from the same venue name that built the gateway, so orders and prices
//! cannot come from two different venues.
//!
//! No host is written down in this crate. Every venue's hostname lives in that
//! venue's own realm table in `engine-venue`, which has a fence that reads its
//! source back — a host spelled out here would be one that fence never sees.

pub mod feed;
pub mod feeds;
pub mod hyperliquid;
pub mod lighter;
pub mod parse;
pub mod state;
pub mod variational;

pub use feed::{bybit_public_linear_url, BybitPublicFeed, MonoClock};
pub use feeds::MarketFeeds;
pub use hyperliquid::HyperliquidPublicFeed;
pub use lighter::LighterPublicFeed;
pub use variational::VariationalPublicFeed;
pub use parse::{
    parse_frame, parse_frame_bytes, BookFrame, Level, Levels, ParsedFrame, TickerFrame,
};
pub use state::{Applied, FeedState, ResyncReason};
