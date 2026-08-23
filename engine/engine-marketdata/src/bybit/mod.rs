//! Bybit's public market data, in three layers.
//!
//! [`parse`] turns raw bytes into typed frames — pure, no state. [`state`]
//! keeps the per-symbol book and ticker and decides whether a frame is an
//! event, nothing, or a lost-continuity resync. [`feed`] owns the socket:
//! subscribe, keep-alive, reconnect, backoff.
//!
//! The split is what lets the parse be tested without a network. The other
//! three venues each answer for all three layers in one file, because their
//! streams are small enough that splitting them would separate nothing.

pub mod feed;
pub mod parse;
pub mod state;

pub use feed::{bybit_public_linear_url, BybitPublicFeed, MonoClock};
