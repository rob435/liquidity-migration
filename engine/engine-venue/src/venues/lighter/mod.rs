//! Lighter: a zk-rollup order book for perpetuals.
//!
//! Unlike the other three, this venue does not sign a request -- it signs a
//! *transaction*: a fixed list of integers hashed with Poseidon2 and signed
//! with Schnorr over the ECgFp5 curve. [`crypto`] is that stack, ported and
//! pinned against the venue's own reference; [`tx`] is the field lists it
//! hashes; [`realm`] is the only place a host, a chain id, or a credential
//! variable is written down.

pub mod gateway;
pub mod public;
pub mod realm;

pub(crate) mod crypto;
pub mod markets;
pub(crate) mod order_index;
pub(crate) mod parse;
pub(crate) mod tx;
pub(crate) mod ws;

pub use gateway::LighterGateway;
pub use realm::LighterRealm;
pub use ws::LighterOrderFeed;

/// What this venue is called in an account identity, a lease file name, and
/// the engine's heartbeat.
pub const VENUE_NAME: &str = "lighter";
