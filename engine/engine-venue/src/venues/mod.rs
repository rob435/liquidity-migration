//! One module per venue. Five peers, not one venue and four exceptions.
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
//! Adding another is a directory here, a `mod` line below, a realm table with
//! its hosts, and a variant in [`crate::registry::VenueName::ALL`] — plus its
//! arms in the two switches, one here and one in `engine-marketdata`. Nothing
//! in `engine-core` moves: the loop is generic over the gateway type. A venue
//! left out of `ALL` is one no config can select, refused at boot by name.
//! `tests/venue_fence.rs` reads this tree back to check the new hosts are
//! written in exactly one file, and fails outright on a venue directory it
//! does not know.

pub mod bybit;
pub mod hyperliquid;
pub mod lighter;
pub mod mexc;
pub mod variational;
