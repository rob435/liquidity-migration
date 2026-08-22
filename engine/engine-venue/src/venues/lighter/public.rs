//! The venue's market list, read without credentials.
//!
//! The market-data feed needs to turn `BTCUSDT` into the market index its
//! subscription names, and that mapping is public. It lives here rather than
//! on the gateway so the price side of this venue needs no key — a shadow run
//! or a data-only unit should not have to hold one.

use engine_types::VenueError;

use super::markets::{Market, Markets};
use super::parse::{parse_markets, venue_result};
use super::realm::LighterRealm;
use crate::http::HttpClient;

const PATH_MARKETS: &str = "/api/v1/orderBookDetails";

/// Every market the venue is currently running.
pub async fn markets(realm: LighterRealm) -> Result<Vec<Market>, VenueError> {
    markets_from(realm.rest_base()).await
}

/// The same read against a named host. Tests only.
pub async fn markets_from(base_url: &str) -> Result<Vec<Market>, VenueError> {
    let http = HttpClient::new(base_url);
    let reply = http.get(PATH_MARKETS, "", &[]).await?;
    parse_markets(&venue_result(reply)?)
}

/// The market table, for a caller that wants the lookups rather than the rows.
pub async fn market_table(realm: LighterRealm) -> Result<Markets, VenueError> {
    Ok(Markets::from_rows(markets(realm).await?))
}
