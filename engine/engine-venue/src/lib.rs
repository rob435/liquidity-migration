//! The venue adapters: Bybit v5 over HTTPS, and the private order stream.
//!
//! Demo only, by construction. The two hostnames below are the only venue
//! hosts anywhere in this crate — there is no mainnet path to misconfigure.
//! The runtime config picks an adapter by *name* ([`Venue::by_name`]) and
//! never carries a URL, so config chooses which of the compiled-in adapters
//! runs and cannot introduce an endpoint. A base URL can be supplied only
//! through the `for_test` constructors, which is how the tests and the
//! engine's mock-venue benchmark reach localhost. `tests/demo_fence.rs`
//! reads the whole crate source back — every module, adapters included — and
//! fails if any other venue host appears.
//!
//! Credentials come from the environment and are never taken as arguments in
//! the live path. A missing key or secret is a `VenueError::Credentials` at
//! construction, so a misconfigured host cannot start an unsigned engine.
//!
//! [`lease`] is the odd one out: it names accounts rather than reaching them,
//! and one of the names it can spell is `mainnet`. That is a lock file's name,
//! not an endpoint — the fence above still holds, and the module says why.

mod clock;
mod creds;
mod fmt;
mod gateway;
pub mod lease;
mod parse;
mod registry;
mod rest;
mod sign;
mod tls;
mod ws;

pub use creds::{Credentials, API_KEY_ENV, API_SECRET_ENV};
pub use gateway::BybitGateway;
pub use registry::{Venue, BYBIT_DEMO, KNOWN_VENUES};
pub use ws::BybitOrderFeed;

/// Bybit demo REST base. The only REST host this crate knows.
pub const DEMO_REST_BASE: &str = "https://api-demo.bybit.com";

/// Bybit demo private stream. The only WebSocket host this crate knows.
/// Bybit serves demo public market data from the mainnet stream instead;
/// that is the market data crate's business, not this one's.
pub const DEMO_PRIVATE_WS: &str = "wss://stream-demo.bybit.com/v5/private";

/// Every request is `linear` — USDT perpetuals, the only thing we trade.
pub(crate) const CATEGORY: &str = "linear";
