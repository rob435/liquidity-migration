//! One module per venue. Four peers, not one venue and three exceptions.
//!
//! Each holds everything that is true of that venue and nothing that is true
//! of another: its realm table (the only place its hosts are written down),
//! its credentials' meaning, its signing, its REST shapes, its private stream,
//! and its own `VenueCaps` — what it can actually do, stated by the adapter
//! rather than assumed by the engine.
//!
//! What they share lives above: [`crate::arming`] holds the `REAL_MONEY` law,
//! [`crate::http`] the socket, [`crate::json`] the field reads, and
//! [`crate::registry`] the name that picks one.
//!
//! Adding a fifth is a directory here, a `mod` line below, a realm table with
//! its hosts, and a name in [`crate::registry::KNOWN_VENUES`]. Nothing in the
//! engine's wiring moves: the loop is generic over the gateway type, and
//! `tests/venue_fence.rs` reads this tree back to check the new hosts are
//! written in exactly one file.

pub mod bybit;
pub mod hyperliquid;
pub mod lighter;
pub mod variational;
