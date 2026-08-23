//! The Variational gateway: everything the venue's public API can answer, and
//! a clear refusal for everything it cannot.
//!
//! **This adapter cannot trade, and says so.** Variational publishes one
//! read-only endpoint — `GET /metadata/stats`, platform and per-listing market
//! statistics — and their documentation states plainly that the trading API
//! "is still in development, and is not yet available to any users". There is
//! no place-order endpoint to call, no account endpoint to read, and no
//! authentication scheme published to sign with.
//!
//! So every write here returns [`VenueError::BadRequest`] naming the reason,
//! rather than a silence, a panic, or a pretend acknowledgement. An engine
//! pointed at this venue starts, reads the market, and refuses to trade it —
//! which is the honest behaviour and a useful one: it is how the venue's
//! listings, spreads and funding can be watched from the same process that
//! trades elsewhere.
//!
//! What lands when the trading API ships is the request-building and the
//! signing. The wiring — the name in the registry, the realm, the credential
//! variables, the lease, the feed — is already here and already tested.

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{AmendSpec, InstrumentRule, OrderAck, OrderRequest, VenueOrder};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use serde_json::Value;

use super::realm::VariationalRealm;
use crate::http::HttpClient;

const PATH_STATS: &str = "/metadata/stats";

/// What the venue's account is called in a lease file. There is no account
/// number to use: nothing here authenticates, so the lease names the venue's
/// public face and still keeps two engines from both running this unit.
const PUBLIC_ACCOUNT: &str = "public";

/// The one sentence every refusal here shares. One string, so an operator who
/// meets it twice recognises it, and so a later change cannot leave half the
/// methods claiming something different.
const NO_TRADING_API: &str =
    "Variational publishes no trading API — the venue's own documentation says it is still in \
     development and not available to any users — so this engine can read its market and \
     cannot place, cancel, amend, or protect an order on it";

pub struct VariationalGateway {
    realm: VariationalRealm,
    http: HttpClient,
    names: Vec<Symbol>,
}

impl VariationalGateway {
    /// The live gateway. No credentials are read: there is nothing to sign.
    pub fn new(realm: VariationalRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let built = Self::build(realm, realm.rest_base(), symbols);
        if built.http.base() != realm.rest_base() {
            return Err(VenueError::BadRequest(format!(
                "realm {realm} resolved to {}, but only {} is permitted for that realm",
                built.http.base(),
                realm.rest_base()
            )));
        }
        Ok(built)
    }

    /// Point the gateway at a local server. Tests only.
    pub fn for_test(base_url: &str, realm: VariationalRealm, symbols: Vec<Symbol>) -> Self {
        Self::build(realm, base_url, symbols)
    }

    fn build(realm: VariationalRealm, base_url: &str, symbols: Vec<Symbol>) -> Self {
        Self {
            realm,
            http: HttpClient::new(base_url),
            names: symbols,
        }
    }

    pub fn realm(&self) -> VariationalRealm {
        self.realm
    }

    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        if let Some(at) = self.names.iter().position(|held| held == name) {
            return SymbolId(at as u16);
        }
        let id = SymbolId(u16::try_from(self.names.len()).expect("more than 65535 symbols"));
        self.names.push(name.to_string());
        id
    }

    /// The venue's market statistics. The whole of its public API, and what
    /// the market feed reads too.
    pub async fn stats(&self) -> Result<Value, VenueError> {
        self.http.get(PATH_STATS, "", &[]).await
    }

    fn refuse<T>(&self) -> Result<T, VenueError> {
        Err(VenueError::BadRequest(NO_TRADING_API.to_string()))
    }
}

impl VenueGateway for VariationalGateway {
    fn caps(&self) -> VenueCaps {
        // Every one false, and none of them a guess: there is no endpoint
        // behind any of these. The engine reads caps before it asks for
        // anything, so a strategy that needs a stop is refused at the door
        // rather than by a request that could not be built.
        VenueCaps {
            native_position_stop: false,
            amend_in_place: false,
            set_leverage: false,
        }
    }

    async fn send_order(&mut self, _req: &OrderRequest) -> Result<OrderAck, VenueError> {
        self.refuse()
    }

    async fn cancel_order(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
    ) -> Result<(), VenueError> {
        self.refuse()
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
        _spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.refuse()
    }

    async fn set_stop(&mut self, _symbol: SymbolId, _trigger_px: f64) -> Result<(), VenueError> {
        self.refuse()
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        Some(VariationalGateway::add_symbol(self, symbol))
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        // Nothing authenticates, so there is no account number to ask for. The
        // lease is still taken — under this name — because two engines running
        // this venue's unit on one box would still be two processes fighting
        // over one heartbeat file and one log.
        Ok(AccountIdentity {
            venue: super::VENUE_NAME.to_string(),
            user_id: PUBLIC_ACCOUNT.to_string(),
            realm: self.realm.as_str().to_string(),
        })
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        // Not a zero: a fabricated equity of nothing would read as a real
        // account that is empty, and the envelope and the partition judge real
        // positions against that number. An error is the truth.
        //
        // The engine reads this at boot, so this is also why an engine cannot
        // run on this venue at all — it stops here rather than start on an
        // invented account. The market feed does not go through the engine and
        // works on its own.
        Err(VenueError::BadRequest(format!(
            "{NO_TRADING_API}; there is no account endpoint to read equity or positions from"
        )))
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        // Empty, and not an error. The venue publishes market statistics and
        // no contract specification: there is no tick size, no size step and
        // no minimum to be had, and inventing them would be a number the
        // engine quantized real orders against.
        //
        // Empty is also what makes an engine on this venue useful rather than
        // dead. Boot warns "these symbols cannot trade" and carries on, the
        // market feed runs, and the engine's own rule — an intent for a symbol
        // with no instrument rule is refused — is what stops an order, before
        // the gateway's refusal is even reached.
        Ok(Vec::new())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        // Empty rather than an error: the engine asks this to find orders it
        // did not place, and on a venue where nobody can place one the honest
        // answer is that there are none.
        Ok(Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderKind, Side, StrategyId};

    fn gateway() -> VariationalGateway {
        VariationalGateway::for_test(
            "http://127.0.0.1:1",
            VariationalRealm::Mainnet,
            vec!["BTCUSDT".to_string()],
        )
    }

    fn request() -> OrderRequest {
        OrderRequest {
            client_order_id: "eng-1-1".to_string(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
        }
    }

    #[tokio::test]
    async fn every_write_refuses_and_says_why() {
        // The claim this whole module rests on. If a later change makes one of
        // these return `Ok` without an endpoint behind it, the engine would
        // record an order that never existed.
        let mut gw = gateway();
        for said in [
            gw.send_order(&request()).await.map(|_| ()).unwrap_err().to_string(),
            gw.cancel_order(SymbolId(0), "eng-1-1").await.unwrap_err().to_string(),
            gw.amend_order(SymbolId(0), "eng-1-1", AmendSpec { px: Some(1.0), qty: None })
                .await
                .unwrap_err()
                .to_string(),
            gw.set_stop(SymbolId(0), 1.0).await.unwrap_err().to_string(),
            gw.set_leverage(SymbolId(0), 2.0).await.unwrap_err().to_string(),
            gw.account_view().await.map(|_| ()).unwrap_err().to_string(),
        ] {
            assert!(
                said.contains("trading API") || said.contains("cannot set leverage"),
                "a refusal that does not say why: {said}"
            );
        }
    }

    #[tokio::test]
    async fn the_caps_promise_nothing_the_venue_cannot_do() {
        let caps = gateway().caps();
        assert!(!caps.native_position_stop);
        assert!(!caps.amend_in_place);
        assert!(!caps.set_leverage);
    }

    #[tokio::test]
    async fn there_are_no_working_orders_because_nobody_can_place_one() {
        assert!(gateway().working_orders().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn the_account_is_named_even_though_nothing_authenticates() {
        // The lease still has to have something to be named after.
        let who = gateway().account_identity().await.unwrap();
        assert_eq!(who.venue, "variational");
        assert_eq!(who.realm, "variational_mainnet");
        assert_eq!(who.user_id, PUBLIC_ACCOUNT);
        assert!(
            crate::lease::account_key_text(&who.user_id).is_some(),
            "the lease cannot name a file after this account"
        );
    }

    #[test]
    fn added_symbols_keep_their_position_as_the_id() {
        let mut gw = gateway();
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.add_symbol("BTCUSDT"), SymbolId(0));
    }
}
