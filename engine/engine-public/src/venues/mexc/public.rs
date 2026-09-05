//! The venue's contract list, read without credentials.
//!
//! The market-data feed needs to turn `BTCUSDT` into the `BTC_USDT` its
//! subscription names, and that mapping is public. It lives here rather than on
//! the gateway so the price side of this venue needs no key — which matters
//! more here than on the other venues, because MEXC's only realm is funded and
//! holding a key for it is not something a data-only unit should have to do.
//!
//! The mapping is read from the venue rather than derived by cutting the
//! engine's symbol at a known quote suffix: `USD`, `USDT`, `USDC` and `USD1`
//! are all live quote currencies here, and `PEPE_USDT`, `PEPE_USDC` and
//! `PEPE_USD1` all exist, so there is no cut that is right.

use engine_types::ids::Symbol;
use engine_types::VenueError;

use super::contracts::Contracts;
use super::realm::MexcRealm;
use crate::http::HttpClient;

const PATH_CONTRACT_DETAIL: &str = "/api/v1/contract/detail";

/// Every contract the venue lists, as (the engine's spelling, the venue's).
pub async fn symbol_map(realm: MexcRealm) -> Result<Vec<(Symbol, String)>, VenueError> {
    symbol_map_from(realm.rest_base()).await
}

/// The same read against a named host. Tests only.
pub async fn symbol_map_from(base_url: &str) -> Result<Vec<(Symbol, String)>, VenueError> {
    let http = HttpClient::new(base_url);
    let reply = http.get(PATH_CONTRACT_DETAIL, "", &[]).await?;
    Ok(Contracts::parse(&reply)?.symbol_pairs())
}
