//! Variational: a peer-to-peer derivatives protocol, read-only from here.
//!
//! The venue publishes market statistics and no trading API. [`gateway`] says
//! exactly what that means for an engine pointed at it, and refuses every
//! write; [`realm`] holds the one host.

pub mod gateway;
pub mod realm;

pub mod parse;

pub use gateway::VariationalGateway;
pub use realm::VariationalRealm;

/// What this venue is called in an account identity, a lease file name, and
/// the engine's heartbeat.
pub const VENUE_NAME: &str = "variational";
