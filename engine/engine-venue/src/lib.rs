//! Authenticated execution adapters, private order streams, signing and account leases.
//! Public catalogs, non-secret realm metadata and shared HTTP transport belong to
//! `engine-public`; credentials and the arming check remain in this crate.
//! `tests/venue_fence.rs` scans both crates and the public market feeds.

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

#[cfg(any(
    feature = "binance",
    feature = "hyperliquid",
    feature = "lighter",
    feature = "mexc"
))]
pub(crate) use clock::account_scan;
pub(crate) use clock::{mono_ns, wall_ms};

pub use arming::{check_arming, check_arming_with, env_flag, real_money_armed, REAL_MONEY_ENV};
pub use creds::Credentials;
pub use registry::{
    known_venues, Capability, Evidence, InventoryProbe, OrderFeeds, Venue, VenueName,
    VenueReadiness, BINANCE_MAINNET, BINANCE_TESTNET, BYBIT_DEMO, BYBIT_MAINNET,
    HYPERLIQUID_MAINNET, HYPERLIQUID_TESTNET, LIGHTER_MAINNET, LIGHTER_TESTNET, MEXC_MAINNET,
    VARIATIONAL_MAINNET,
};
#[cfg(feature = "binance")]
pub use venues::binance::{BinanceGateway, BinanceOrderFeed};
#[cfg(feature = "bybit")]
pub use venues::bybit::{
    BybitGateway, BybitInventoryProbe, BybitOrderFeed, BybitOrderReceipt, API_KEY_ENV,
    API_SECRET_ENV,
};
#[cfg(feature = "hyperliquid")]
pub use venues::hyperliquid::{
    HyperliquidGateway, HyperliquidInventoryProbe, HyperliquidOrderFeed,
};
#[cfg(feature = "lighter")]
pub use venues::lighter::{LighterGateway, LighterOrderFeed};
#[cfg(feature = "mexc")]
pub use venues::mexc::{MexcGateway, MexcInventoryProbe, MexcOrderFeed};
#[cfg(feature = "variational")]
pub use venues::variational::VariationalGateway;

mod realm_credentials;
pub use realm_credentials::RealmCredentials;
mod signing;
mod stream;
mod wire;

mod order_lookup;

#[cfg(any(test, feature = "binance"))]
mod shared_budget;

mod amend_state;
mod order_wire;
mod stop_state;

mod catalog_checkpoint;

mod account_numbers;
mod account_recovery;
mod account_stops;

pub use engine_public::{
    BinanceRealm, HyperliquidRealm, LighterRealm, MexcRealm, VariationalRealm, VenueRealm,
};

#[cfg(all(test, feature = "bybit"))]
#[path = "../../test-support/io.rs"]
mod test_io;
