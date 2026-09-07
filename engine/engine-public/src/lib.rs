//! Non-secret venue metadata, public catalogs and protocol-independent transport.

pub mod http;
pub mod json;
pub mod numeric_wire;
pub mod symbols;
pub mod tls;
pub mod venues;
pub use venues::{
    binance::realm::BinanceRealm, bybit::realm::VenueRealm, hyperliquid::realm::HyperliquidRealm,
    lighter::realm::LighterRealm, mexc::realm::MexcRealm, variational::realm::VariationalRealm,
};
pub mod registry;
pub use registry::{VenueName, VenueReadiness};

#[cfg(test)]
#[path = "../../test-support/io.rs"]
mod test_io;
