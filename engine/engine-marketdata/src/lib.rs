//! Bybit public market data, parsed once into the engine's contract types.
//!
//! Three layers, split so the parse can be tested without a socket:
//!
//! - [`parse`] turns raw bytes into typed frames. Pure, no state.
//! - [`state`] keeps the per-symbol book and ticker, and decides whether a
//!   frame is an event, nothing, or a lost-continuity resync.
//! - [`feed`] owns the socket: subscribe, keep-alive, reconnect, backoff.
//!
//! The stream is public — no credentials — and it is the same price feed the
//! demo account trades against.

pub mod feed;
pub mod parse;
pub mod state;

pub use feed::{BybitPublicFeed, MonoClock, BYBIT_PUBLIC_LINEAR_URL};
pub use parse::{
    parse_frame, parse_frame_bytes, BookFrame, Level, Levels, ParsedFrame, TickerFrame,
};
pub use state::{Applied, FeedState, ResyncReason};
