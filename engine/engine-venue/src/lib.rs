//! The demo venue gateway: Bybit v5 over HTTPS, and the private order stream.
//!
//! Demo only, by construction. The two hostnames below are the only venue
//! hosts anywhere in this crate — there is no mainnet path to misconfigure.
//! The runtime config carries no URL at all; a base URL can be supplied only
//! through the `for_test` constructors, which is how the tests and the
//! engine's mock-venue benchmark reach localhost. `tests/demo_fence.rs`
//! reads the crate source back and fails if any other venue host appears.
//!
//! Credentials come from the environment and are never taken as arguments in
//! the live path. A missing key or secret is a `VenueError::Credentials` at
//! construction, so a misconfigured host cannot start an unsigned engine.

mod clock;
mod creds;
mod fmt;
mod gateway;
mod parse;
mod rest;
mod sign;
mod ws;

pub use creds::{Credentials, API_KEY_ENV, API_SECRET_ENV};
pub use gateway::BybitGateway;
pub use ws::BybitOrderFeed;

/// Bybit demo REST base. The only REST host this crate knows.
pub const DEMO_REST_BASE: &str = "https://api-demo.bybit.com";

/// Bybit demo private stream. The only WebSocket host this crate knows.
/// Bybit serves demo public market data from the mainnet stream instead;
/// that is the market data crate's business, not this one's.
pub const DEMO_PRIVATE_WS: &str = "wss://stream-demo.bybit.com/v5/private";

/// Every request is `linear` — USDT perpetuals, the only thing we trade.
pub(crate) const CATEGORY: &str = "linear";
