//! The venue adapters: Bybit v5 over HTTPS, and the private order stream.
//!
//! Two realms, demo and mainnet, and the difference between them is real
//! money. [`realm`] is the only module that turns a realm into a hostname or a
//! credential variable name; everything else carries a [`VenueRealm`] and asks
//! it. That is what makes the guarantees below checkable in one place instead
//! of argued across a crate.
//!
//! **What stops an accident reaching the funded account.** Four things, and
//! none of them is "we were careful":
//!
//! 1. `REAL_MONEY=true` must be set in the host's credential file, by the
//!    account owner. The engine never writes it. Without it, building a
//!    mainnet gateway fails at the credential read, before a socket is opened.
//! 2. The two realms read disjoint environment variables, so a demo key cannot
//!    authenticate mainnet even if the realm were wrong.
//! 3. A realm is always named explicitly. There is no default, so a config
//!    that forgets to say which account it trades does not get one chosen for
//!    it.
//! 4. The realm and the host cannot disagree: the live constructors take a
//!    realm and derive the host from it, and then read it back and check.
//!
//! And it cuts both ways — an armed host refuses to run the *demo* realm, so a
//! box cannot be half-live and half-practice at once.
//!
//! `tests/venue_fence.rs` reads the whole crate source back and enforces the
//! structural half: the only venue hosts named anywhere are the four in
//! [`realm`], they appear only in that module, and testnet and every alternate
//! Bybit domain are absent entirely.
//!
//! Credentials come from the environment and are never taken as arguments in
//! the live path. A base URL can be supplied only through the `for_test`
//! constructors, which is how the tests and the engine's mock-venue benchmark
//! reach localhost.
//!
//! [`lease`] names accounts rather than reaching them; the module says why its
//! spelling of `mainnet` is a lock file's name and not an endpoint.

mod clock;
mod creds;
mod fmt;
mod gateway;
pub mod lease;
mod parse;
pub mod realm;
mod registry;
mod rest;
mod sign;
mod tls;
mod ws;

pub use creds::{Credentials, API_KEY_ENV, API_SECRET_ENV};
pub use gateway::BybitGateway;
pub use realm::{
    check_arming, check_arming_with, env_flag, real_money_armed, VenueRealm, REAL_MONEY_ENV,
};
pub use registry::{Venue, BYBIT_DEMO, BYBIT_MAINNET, KNOWN_VENUES};
pub use ws::BybitOrderFeed;

/// Every request is `linear` — USDT perpetuals, the only thing we trade.
pub(crate) const CATEGORY: &str = "linear";
