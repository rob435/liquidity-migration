//! The venue adapters, and the one switch that picks between them.
//!
//! Five venues live under [`venues`], one module each: Bybit, Hyperliquid,
//! Lighter, MEXC, Variational. The engine names one in `engine.toml` and gets its
//! gateway, its private order stream, and its public market feed — all three
//! from the same name, so a config cannot half-switch.
//!
//! Every venue has realms, and the difference between two realms is real
//! money. A venue's own `realm` module is the only place that turns one of its
//! realms into a hostname or a credential variable name; everything else
//! carries the realm and asks it. That is what makes the guarantees below
//! checkable in one place instead of argued across a crate.
//!
//! **What stops an accident reaching a funded account.** Four things, and none
//! of them is "we were careful":
//!
//! 1. `REAL_MONEY=true` must be set in the host's credential file, by the
//!    account owner. The engine never writes it. Without it, building a
//!    real-money gateway fails at the credential read, before a socket is
//!    opened. The rule is stated once, in [`arming`], and every venue's realm
//!    table asks it rather than restating it.
//! 2. Each realm reads its own environment variables, disjoint from every
//!    other realm's — across venues as well as within one. A Bybit demo key
//!    cannot authenticate Bybit mainnet, and no key of any venue can
//!    authenticate another.
//! 3. A realm is always named explicitly. There is no default, so a config
//!    that forgets to say which account it trades does not get one chosen for
//!    it.
//! 4. The realm and the host cannot disagree: the live constructors take a
//!    realm and derive the host from it, and then read it back and check.
//!
//! And it cuts both ways — an armed host refuses to run a *practice* realm, so
//! a box cannot be half-live and half-practice at once.
//!
//! `tests/venue_fence.rs` reads the whole crate source back and enforces the
//! structural half: every venue host named anywhere in the crate is one this
//! crate declares, each appears only in its own venue's `realm.rs`, and
//! testnet hosts of a venue that has no testnet realm are absent entirely.
//!
//! Credentials come from the environment and are never taken as arguments in
//! the live path. A base URL can be supplied only through the `for_test`
//! constructors, which is how the tests and the engine's mock-venue benchmark
//! reach localhost.
//!
//! [`lease`] names accounts rather than reaching them; the module says why its
//! spelling of a realm is a lock file's name and not an endpoint.

pub mod arming;
pub mod lease;
pub mod venues;

mod clock;
mod creds;
mod fmt;
mod http;
mod json;
mod registry;
mod tls;

pub(crate) use clock::{account_scan, mono_ns, wall_ms};

pub use arming::{check_arming, check_arming_with, env_flag, real_money_armed, REAL_MONEY_ENV};
pub use creds::Credentials;
pub use registry::{
    known_venues, InventoryProbe, OrderFeeds, Venue, VenueName, VenueReadiness, BYBIT_DEMO,
    BYBIT_MAINNET, HYPERLIQUID_MAINNET, HYPERLIQUID_TESTNET, LIGHTER_MAINNET, LIGHTER_TESTNET,
    VARIATIONAL_MAINNET,
};
pub use venues::bybit::{
    BybitGateway, BybitInventoryProbe, BybitOrderFeed, BybitOrderReceipt, VenueRealm, API_KEY_ENV,
    API_SECRET_ENV,
};
pub use venues::hyperliquid::{HyperliquidGateway, HyperliquidOrderFeed, HyperliquidRealm};
pub use venues::lighter::{LighterGateway, LighterOrderFeed, LighterRealm};
pub use venues::mexc::{MexcGateway, MexcOrderFeed, MexcRealm};
pub use venues::variational::{VariationalGateway, VariationalRealm};
