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

pub(crate) use clock::{account_scan, mono_ns, wall_ms};

pub use arming::{check_arming, check_arming_with, env_flag, real_money_armed, REAL_MONEY_ENV};
pub use creds::Credentials;
pub use registry::{
    known_venues, InventoryProbe, OrderFeeds, Venue, VenueName, VenueReadiness, BINANCE_MAINNET,
    BINANCE_TESTNET, BYBIT_DEMO, BYBIT_MAINNET, HYPERLIQUID_MAINNET, HYPERLIQUID_TESTNET,
    LIGHTER_MAINNET, LIGHTER_TESTNET, MEXC_MAINNET, VARIATIONAL_MAINNET,
};
pub use venues::binance::{BinanceGateway, BinanceOrderFeed, BinanceRealm};
pub use venues::bybit::{
    BybitGateway, BybitInventoryProbe, BybitOrderFeed, BybitOrderReceipt, VenueRealm, API_KEY_ENV,
    API_SECRET_ENV,
};
pub use venues::hyperliquid::{HyperliquidGateway, HyperliquidOrderFeed, HyperliquidRealm};
pub use venues::lighter::{LighterGateway, LighterOrderFeed, LighterRealm};
pub use venues::mexc::{MexcGateway, MexcOrderFeed, MexcRealm};
pub use venues::variational::{VariationalGateway, VariationalRealm};

mod realm_credentials;
pub use realm_credentials::RealmCredentials;
mod signing;
mod stream;
mod wire;

mod order_lookup;

mod shared_budget;

mod order_wire;
