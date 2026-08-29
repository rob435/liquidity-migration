//! The Lighter gateway: the [`VenueGateway`] contract over Lighter's REST API.
//!
//! One adapter, two networks, and the realm decides the host AND the chain id
//! that goes into every signature — so a testnet-signed order cannot be
//! replayed against the funded account.
//!
//! What is different in kind from the other three:
//!
//! - **Orders are transactions, not requests.** A fixed list of integers is
//!   hashed and signed ([`super::tx`], [`super::crypto`]); the HTTP call is
//!   only how the signed thing travels. Prices and sizes are integers in the
//!   market's own units, converted once, in [`super::markets`], because the
//!   signature covers the integer.
//! - **Nonces are strictly ordered per API key.** The venue takes them in
//!   order and refuses a gap, so the gateway keeps its own counter after
//!   reading the venue's once, rather than asking before every order.
//! - **A stop is a separate reduce-only trigger order**, as on Hyperliquid, so
//!   whether a position is protected is answered from the open orders.
//!
//! **What is proven and what is not.** The signing is pinned, layer by layer,
//! against the venue's own Go reference — see `crypto/vectors.rs`. The request
//! shapes below are taken from that reference's structs and from the venue's
//! published API, and are checked by `tests/lighter_requests.rs` against a
//! local server. Neither of those is the venue accepting an order; only
//! sending one proves that, and the funded realm is the owner's to arm.

use std::collections::HashMap;

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest, Side, TimeInForce, VenueExecution,
    VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use serde_json::Value;

use super::crypto::gfp5;
use super::crypto::poseidon2::hash_to_quintic_extension;
use super::crypto::scalar::Scalar;
use super::crypto::schnorr;
use super::markets::{venue_price, venue_size, Market, Markets};
use super::order_index;
use super::parse::{
    parse_executions, parse_margin, parse_markets, parse_nonce, parse_positions,
    parse_working_orders, stops_by_market, venue_result,
};
use super::realm::{AccountKey, LighterRealm};
use super::tx::{
    CancelOrder, CreateOrder, NIL_ORDER_EXPIRY, NIL_TRIGGER_PRICE, ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET, ORDER_TYPE_STOP_LOSS, TIF_GOOD_TILL_TIME, TIF_IMMEDIATE_OR_CANCEL,
    TIF_POST_ONLY, TX_TYPE_CANCEL_ORDER, TX_TYPE_CREATE_ORDER,
};
use crate::creds::Credentials;
use crate::http::{percent_encode, HttpClient};
use crate::json::int_field;
use crate::{mono_ns, wall_ms};

const PATH_SEND_TX: &str = "/api/v1/sendTx";
const PATH_NEXT_NONCE: &str = "/api/v1/nextNonce";
const PATH_MARKETS: &str = "/api/v1/orderBookDetails";
const PATH_ACCOUNT: &str = "/api/v1/account";
const PATH_ACTIVE_ORDERS: &str = "/api/v1/accountActiveOrders";
const PATH_TRADES: &str = "/api/v1/trades";

/// How long a transaction stays valid. Long enough to survive a slow round
/// trip, short enough that one the venue never saw cannot be replayed later.
const TX_LIFETIME_MS: i64 = 60_000;

/// How long a resting order lives. The venue requires an expiry on anything
/// that is not immediate-or-cancel, and refuses one under five minutes or
/// over thirty days.
const ORDER_LIFETIME_MS: i64 = 28 * 24 * 60 * 60 * 1000;

/// How long an auth token is good for. The venue allows up to eight hours;
/// shorter means a leaked token is worth less and costs only a re-sign.
const AUTH_LIFETIME_S: i64 = 600;

pub struct LighterGateway {
    realm: LighterRealm,
    http: HttpClient,
    account: AccountKey,
    secret: Scalar,
    names: Vec<Symbol>,
    ids: HashMap<Symbol, SymbolId>,
    markets: Markets,
    /// The venue takes nonces strictly in order per API key. Read once, then
    /// counted here — asking before every order would be a round trip in
    /// front of every order.
    next_nonce: Option<i64>,
}

impl LighterGateway {
    /// The live gateway: the realm's host, chain id and credentials.
    pub fn new(realm: LighterRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        let built = Self::build(realm, realm.rest_base(), creds, symbols)?;
        if built.http.base() != realm.rest_base() {
            return Err(VenueError::BadRequest(format!(
                "realm {realm} resolved to {}, but only {} is permitted for that realm",
                built.http.base(),
                realm.rest_base()
            )));
        }
        Ok(built)
    }

    /// Point the gateway at a local server. Tests and the mock venue only.
    pub fn for_test(
        base_url: &str,
        realm: LighterRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Result<Self, VenueError> {
        Self::build(realm, base_url, creds, symbols)
    }

    fn build(
        realm: LighterRealm,
        base_url: &str,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Result<Self, VenueError> {
        let account = AccountKey::parse(creds.key())?;
        let secret = read_secret(creds.secret())?;
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Ok(Self {
            realm,
            http: HttpClient::new(base_url),
            account,
            secret,
            names: symbols,
            ids,
            markets: Markets::default(),
            next_nonce: None,
        })
    }

    pub fn realm(&self) -> LighterRealm {
        self.realm
    }

    /// Load the market list before an order needs it.
    pub async fn warm(&mut self) -> Result<(), VenueError> {
        self.load_markets().await
    }

    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        if let Some(id) = self.ids.get(name) {
            return *id;
        }
        let id = SymbolId(u16::try_from(self.names.len()).expect("more than 65535 symbols"));
        self.names.push(name.to_string());
        self.ids.insert(name.to_string(), id);
        id
    }

    pub fn symbols(&self) -> &[Symbol] {
        &self.names
    }

    fn name_of(&self, id: SymbolId) -> Result<&str, VenueError> {
        self.names.get(id.0 as usize).map(String::as_str).ok_or_else(|| {
            VenueError::BadRequest(format!("symbol id {} is not in the gateway's table", id.0))
        })
    }

    /// The public key this account's API key slot must be registered with.
    /// Worth having for an operator: "which key is this box signing with" is
    /// otherwise only answerable by deriving it by hand.
    pub fn public_key(&self) -> String {
        hex::encode(gfp5::to_le_bytes(&schnorr::public_key(&self.secret)))
    }

    async fn get(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        let reply = self.http.get(path, query, &[]).await?;
        venue_result(reply)
    }

    /// A signed read: the venue wants a token proving the key holder asked.
    async fn get_signed(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        let token = self.auth_token()?;
        let reply = self
            .http
            .get(path, query, &[("Authorization", token)])
            .await?;
        venue_result(reply)
    }

    /// `<deadline>:<account>:<key slot>`, signed, with the signature appended.
    ///
    /// The message is hashed as bytes-in-field-elements, which is the venue's
    /// own way of hashing a string — eight little-endian bytes per element.
    fn auth_token(&self) -> Result<String, VenueError> {
        let deadline = wall_ms() / 1000 + AUTH_LIFETIME_S;
        let message = format!(
            "{deadline}:{}:{}",
            self.account.account_index, self.account.api_key_index
        );
        let hashed = hash_to_quintic_extension(&order_index::bytes_as_fields(message.as_bytes()));
        let signature = schnorr::sign(&hashed, &self.secret);
        Ok(format!("{message}:{}", hex::encode(signature.to_bytes())))
    }

    async fn load_markets(&mut self) -> Result<(), VenueError> {
        let reply = self.get(PATH_MARKETS, "").await?;
        self.markets = Markets::from_rows(parse_markets(&reply)?);
        Ok(())
    }

    async fn market_for(&mut self, symbol: &str) -> Result<Market, VenueError> {
        if self.markets.is_empty() {
            self.load_markets().await?;
        }
        if let Ok(market) = self.markets.for_symbol(symbol) {
            return Ok(market.clone());
        }
        self.load_markets().await?;
        self.markets.for_symbol(symbol).cloned()
    }

    async fn market_of_id(&mut self, id: SymbolId) -> Result<Market, VenueError> {
        let symbol = self.name_of(id)?.to_string();
        self.market_for(&symbol).await
    }

    /// The next nonce, read from the venue once and then counted.
    async fn take_nonce(&mut self) -> Result<i64, VenueError> {
        if self.next_nonce.is_none() {
            let query = format!(
                "account_index={}&api_key_index={}",
                self.account.account_index, self.account.api_key_index
            );
            let reply = self.get(PATH_NEXT_NONCE, &query).await?;
            self.next_nonce = Some(parse_nonce(&reply)?);
        }
        let nonce = self.next_nonce.expect("read just above");
        self.next_nonce = Some(nonce + 1);
        Ok(nonce)
    }

    /// A send that did not clearly succeed may or may not have consumed its
    /// nonce, and a counter one ahead of the venue's refuses every order after
    /// it — for the life of the process. Forget it and read the venue's again
    /// on the next send.
    ///
    /// A timed-out send is the case that matters most, not a refusal: a
    /// refusal at least says the venue saw the transaction, while a socket
    /// that died mid-flight says nothing at all.
    fn forget_nonce(&mut self) {
        self.next_nonce = None;
    }

    /// Sign a transaction and post it.
    async fn send_tx(&mut self, tx_type: u8, hashed: &gfp5::Element, body: Value) -> Result<Value, VenueError> {
        let signature = schnorr::sign(hashed, &self.secret);
        let mut signed = body;
        signed["Sig"] = Value::String(base64_signature(&signature.to_bytes()));
        let info = serde_json::to_string(&signed)
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        let form = format!(
            "tx_type={tx_type}&tx_info={}",
            percent_encode(&info)
        );
        let sent = self
            .http
            .post(PATH_SEND_TX, form, "application/x-www-form-urlencoded", &[])
            .await
            .and_then(venue_result);
        if sent.is_err() {
            self.forget_nonce();
        }
        sent
    }

    /// The reduce-only trigger that protects a position. Its own transaction,
    /// because the venue takes one order per transaction.
    ///
    /// Both paths that place a stop — an entry carrying one, and a stop being
    /// moved — build it here. They drifted apart when they did not.
    async fn send_stop(
        &mut self,
        market: &Market,
        covering: Side,
        qty: f64,
        trigger_px: f64,
        name: &str,
    ) -> Result<(), VenueError> {
        let trigger = venue_price(trigger_px, covering, market)?;
        let protection = CreateOrder {
            account_index: self.account.account_index,
            api_key_index: self.account.api_key_index,
            market_index: market.index,
            client_order_index: order_index::to_index(name),
            base_amount: venue_size(qty, market)?,
            // Not the trigger. On this venue the price field bounds how far
            // the immediate-or-cancel that a stop becomes may fill, so a bound
            // set at the trigger is a stop that fills nothing the moment the
            // price gaps through it — which is the moment it exists for. The
            // widest the field allows, the same bound a market order takes
            // here.
            price: match covering {
                Side::Buy => u32::MAX,
                Side::Sell => 1,
            },
            is_ask: u8::from(matches!(covering, Side::Sell)),
            order_type: ORDER_TYPE_STOP_LOSS,
            // A stop fires and takes; the venue requires this.
            time_in_force: TIF_IMMEDIATE_OR_CANCEL,
            reduce_only: 1,
            trigger_price: trigger,
            // Immediate-or-cancel carries no expiry, the same rule the entry
            // follows.
            order_expiry: NIL_ORDER_EXPIRY,
            expired_at: wall_ms() + TX_LIFETIME_MS,
            nonce: self.take_nonce().await?,
        };
        let hashed = protection.hash(self.realm.chain_id());
        self.send_tx(TX_TYPE_CREATE_ORDER, &hashed, protection.to_json(&[0u8; 80]))
            .await?;
        Ok(())
    }

    async fn active_orders(&self) -> Result<Value, VenueError> {
        // No market named, so the venue answers for every market — which is
        // the point of this read: to find orders nobody here placed.
        let query = format!("account_index={}", self.account.account_index);
        self.get_signed(PATH_ACTIVE_ORDERS, &query).await
    }

    fn order_type_and_tif(&self, kind: OrderKind) -> (u8, u8) {
        match kind {
            OrderKind::Market => (ORDER_TYPE_MARKET, TIF_IMMEDIATE_OR_CANCEL),
            OrderKind::Limit { tif, .. } => (
                ORDER_TYPE_LIMIT,
                match tif {
                    TimeInForce::Gtc => TIF_GOOD_TILL_TIME,
                    TimeInForce::Ioc => TIF_IMMEDIATE_OR_CANCEL,
                    TimeInForce::PostOnly => TIF_POST_ONLY,
                },
            ),
        }
    }
}

#[engine_types::async_trait]
impl VenueGateway for LighterGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            // A reduce-only stop-loss trigger order the venue keeps and that
            // outlives this process. Like Hyperliquid's, it is an order and
            // not a field on the position, which is why `account_view` reads
            // the open orders to answer whether a position is protected.
            native_position_stop: true,
            // The venue has a modify transaction; this adapter does not send
            // one. The engine does NOT quietly fall back to cancel-and-replace
            // when told a venue cannot amend — that is a new order at the back
            // of the queue at a fresh price, a different trade — so it leaves
            // the order where it is and says so in the log. A resting quote on
            // this venue therefore does not move until it is cancelled.
            amend_in_place: false,
            // Leverage is set by a margin-fraction transaction this adapter
            // does not send, so the symbol keeps whatever it carries and the
            // engine is told it has no say.
            //
            // **This is what stops the venue trading today.** The engine
            // refuses an entry that names a leverage when it cannot set one —
            // the margin posted would not be the margin the position was sized
            // at — and the target-book follower names one on every entry. So
            // an engine on Lighter reads the market, protects and exits what
            // it already holds, and opens nothing. Trading it means sending
            // the margin-fraction transaction, whose signed field list has to
            // be pinned against the venue's reference the way the other two
            // are.
            set_leverage: false,
            close_position_below_minimum: false,
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let symbol = self.name_of(req.symbol)?.to_string();
        let market = self.market_for(&symbol).await?;
        let (order_type, time_in_force) = self.order_type_and_tif(req.kind);
        let is_ask = u8::from(matches!(req.side, Side::Sell));

        // A market order still carries a price on this venue: the field is not
        // optional, and it bounds how far an immediate-or-cancel may fill.
        let price = match req.kind {
            OrderKind::Limit { px, .. } => venue_price(px, req.side, &market)?,
            // Nothing to bound it against without a book read, so the widest
            // the field allows: the order is immediate-or-cancel and takes
            // what is there.
            OrderKind::Market => match req.side {
                Side::Buy => u32::MAX,
                Side::Sell => 1,
            },
        };

        let now = wall_ms();
        let order = CreateOrder {
            account_index: self.account.account_index,
            api_key_index: self.account.api_key_index,
            market_index: market.index,
            client_order_index: order_index::to_index(&req.client_order_id),
            base_amount: venue_size(req.qty, &market)?,
            price,
            is_ask,
            order_type,
            time_in_force,
            reduce_only: u8::from(req.reduce_only),
            trigger_price: NIL_TRIGGER_PRICE,
            // Immediate-or-cancel orders must carry no expiry; everything else
            // must carry one.
            order_expiry: if time_in_force == TIF_IMMEDIATE_OR_CANCEL {
                NIL_ORDER_EXPIRY
            } else {
                now + ORDER_LIFETIME_MS
            },
            expired_at: now + TX_LIFETIME_MS,
            nonce: self.take_nonce().await?,
        };
        let hashed = order.hash(self.realm.chain_id());
        let reply = self
            .send_tx(TX_TYPE_CREATE_ORDER, &hashed, order.to_json(&[0u8; 80]))
            .await?;
        let ack_ns = mono_ns();

        // The stop is a second transaction: this venue takes one order per
        // transaction, so an entry and its stop cannot travel together the way
        // they do on Bybit and Hyperliquid.
        //
        // The entry above is already accepted by the time this is sent, so a
        // failure here is NOT the entry's failure. Returning one would tell
        // the engine no order exists, free its reservation, and leave a live
        // position nothing in the log accounts for — which the next boot then
        // finds as unexplained exposure and latches on. The ack is the truth;
        // the missing stop is caught where a missing stop is always caught, by
        // `stop_attached` in the next account view.
        let stop_failed = match req.stop {
            Some(stop) if !req.reduce_only => self
                .send_stop(
                    &market,
                    req.side.flipped(),
                    req.qty,
                    stop.trigger_px,
                    &format!("{}-stop", req.client_order_id),
                )
                .await
                .err(),
            _ => None,
        };
        if let Some(refused) = &stop_failed {
            tracing::error!(
                id = %req.client_order_id,
                error = %refused,
                "the entry is live and its stop was refused; the position is unprotected"
            );
        }

        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id: reply
                .get("tx_hash")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            sent_ns: 0,
            ack_ns,
        })
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        let market = self.market_of_id(symbol).await?;
        let cancel = CancelOrder {
            account_index: self.account.account_index,
            api_key_index: self.account.api_key_index,
            market_index: market.index,
            index: order_index::to_index(client_order_id),
            expired_at: wall_ms() + TX_LIFETIME_MS,
            nonce: self.take_nonce().await?,
        };
        let hashed = cancel.hash(self.realm.chain_id());
        self.send_tx(TX_TYPE_CANCEL_ORDER, &hashed, cancel.to_json(&[0u8; 80]))
            .await?;
        Ok(())
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
        _spec: AmendSpec,
    ) -> Result<(), VenueError> {
        // Declared false in `caps`, so the engine does not call this — and
        // refusing rather than quietly cancel-and-replacing is the point: that
        // is a different trade, and the caller decides whether to make it.
        Err(VenueError::BadRequest(
            "this adapter does not amend on Lighter, and said so in its caps; the engine's own \
             supervisor cancels and replaces instead"
                .to_string(),
        ))
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.to_string();
        let market = self.market_for(&name).await?;

        // What the stop has to cover, and which way it points, from the venue
        // rather than from memory.
        let account = self
            .get(
                PATH_ACCOUNT,
                &format!("by=index&value={}", self.account.account_index),
            )
            .await?;
        let ids = &self.ids;
        let resolve = |name: &str| ids.get(name).copied();
        let held = parse_positions(&account, &self.markets, &HashMap::new(), &resolve)?;
        let position = held
            .iter()
            .find(|p| p.symbol == symbol)
            .ok_or_else(|| {
                VenueError::BadRequest(format!(
                    "there is no open position in {name} for a stop to protect"
                ))
            })?;

        // Which stops are standing now, read before anything is sent, so the
        // list is exactly the old ones and the replacement cannot be in it.
        let open = self.active_orders().await?;
        let old: Vec<i64> = open
            .get("orders")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter(|row| {
                        // Read the same way every other field in this adapter
                        // is: a stringed number read with `as_i64` alone comes
                        // back as no match, and the old stop is left standing
                        // beside the new one.
                        int_field(row, "market_index").ok() == Some(i64::from(market.index))
                            && row.get("reduce_only").and_then(Value::as_bool).unwrap_or(false)
                            && row
                                .get("type")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_ascii_lowercase()
                                .contains("stop")
                    })
                    .filter_map(|row| int_field(row, "client_order_index").ok())
                    .collect()
            })
            .unwrap_or_default();

        // The replacement first, the old ones after. The other order leaves
        // the position bare for the width of a round trip, and bare for good
        // if the placement then fails — which is the one state this call
        // exists to prevent. Two reduce-only stops for a moment is harmless:
        // whichever fires first flattens the position, and the other can only
        // reduce a position that is already gone.
        let exit_side = position.side.flipped();
        let qty = position.qty;
        self.send_stop(
            &market,
            exit_side,
            qty,
            trigger_px,
            &format!("stop-{}-{}", market.index, wall_ms()),
        )
        .await?;

        for index in old {
            let cancel = CancelOrder {
                account_index: self.account.account_index,
                api_key_index: self.account.api_key_index,
                market_index: market.index,
                index,
                expired_at: wall_ms() + TX_LIFETIME_MS,
                nonce: self.take_nonce().await?,
            };
            let hashed = cancel.hash(self.realm.chain_id());
            if let Err(gone) = self
                .send_tx(TX_TYPE_CANCEL_ORDER, &hashed, cancel.to_json(&[0u8; 80]))
                .await
            {
                tracing::debug!(index, error = %gone, "a standing stop was already gone");
            }
        }
        Ok(())
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        Some(LighterGateway::add_symbol(self, symbol))
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        // The account is a number the operator configured, so what is worth
        // asking the venue is whether it exists and whether this key is
        // registered against it — an unregistered key signs orders the venue
        // refuses, and saying so at boot beats finding out per order.
        let reply = self
            .get(
                PATH_ACCOUNT,
                &format!("by=index&value={}", self.account.account_index),
            )
            .await?;
        let known = reply
            .get("accounts")
            .and_then(Value::as_array)
            .map(|rows| !rows.is_empty())
            .unwrap_or(false);
        if !known {
            return Err(VenueError::Credentials(format!(
                "the venue knows no account {}",
                self.account.account_index
            )));
        }
        Ok(AccountIdentity {
            venue: super::VENUE_NAME.to_string(),
            user_id: self.account.account_index.to_string(),
            realm: self.realm.as_str().to_string(),
        })
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        if self.markets.is_empty() {
            self.load_markets().await?;
        }
        // Two reads, issued together: the account, and the open orders that
        // say which positions carry a stop.
        let account_query = format!("by=index&value={}", self.account.account_index);
        let account = self.get(PATH_ACCOUNT, &account_query);
        let orders = self.active_orders();
        let (account, orders) = futures_util::future::try_join(account, orders).await?;
        let observed_ns = mono_ns();

        let (equity_usdt, available_usdt) = parse_margin(&account)?;
        let stops = stops_by_market(&orders)?;
        let ids = &self.ids;
        let resolve = |name: &str| ids.get(name).copied();
        let positions = parse_positions(&account, &self.markets, &stops, &resolve)?;

        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions,
            observed_ns,
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.load_markets().await?;
        Ok(self.markets.instrument_rules())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        if self.markets.is_empty() {
            self.load_markets().await?;
        }
        let orders = self.active_orders().await?;
        parse_working_orders(&orders, &self.markets)
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        if self.markets.is_empty() {
            self.load_markets().await?;
        }
        // The venue answers at most this many per query, oldest first, so a
        // busy window walks forward from the last fill seen. On this venue
        // this read is the ONLY way a fill is ever learned — the private feed
        // paces resyncs and carries no fills of its own — so one truncated
        // page is a fill the log never gets.
        const PAGE_LIMIT: usize = 100;
        const MAX_PAGES: usize = 20;
        let mut out: Vec<VenueExecution> = Vec::new();
        let mut from = start_ms;
        for _ in 0..MAX_PAGES {
            if from > end_ms {
                return Ok(out);
            }
            let query = format!(
                "account_index={}&sort_by=timestamp&sort_dir=asc&from={from}&to={end_ms}\
                 &limit={PAGE_LIMIT}",
                self.account.account_index
            );
            let reply = self.get_signed(PATH_TRADES, &query).await?;
            let rows = parse_executions(&reply, self.account.account_index, &self.markets)?;
            let count = rows.len();
            let newest = rows.iter().map(|r| r.venue_ts_ms).max();
            // Fills already held are dropped by their own id, so a page that
            // overlaps the last one does not double-count.
            for row in rows {
                if row.venue_ts_ms < start_ms || row.venue_ts_ms > end_ms {
                    continue;
                }
                if !out.iter().any(|held| held.exec_id == row.exec_id) {
                    out.push(row);
                }
            }
            if count < PAGE_LIMIT {
                return Ok(out);
            }
            match newest {
                // Every fill in a full page shares one millisecond: stepping
                // past it would drop fills, and not stepping loops forever.
                Some(newest) if newest > from => from = newest,
                _ => {
                    return Err(VenueError::BadReply(
                        "a full page of fills shares one timestamp, so the history cannot be \
                         walked without losing some"
                            .to_string(),
                    ))
                }
            }
        }
        // A truncated history would quietly leave fills missing, which is the
        // one answer this read must never give.
        Err(VenueError::BadReply(format!(
            "fill history still had pages after {MAX_PAGES}"
        )))
    }
}

/// Read the API key: forty bytes of hex, with or without the `0x`.
fn read_secret(raw: &str) -> Result<Scalar, VenueError> {
    let trimmed = raw.trim();
    let body = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
        .unwrap_or(trimmed);
    let bytes = hex::decode(body).map_err(|_| {
        VenueError::Credentials(
            "a Lighter API private key is 40 bytes of hex, written with or without a leading 0x"
                .to_string(),
        )
    })?;
    schnorr::secret_from_le_bytes(&bytes)
}

/// Standard base64. Shared with [`super::tx`]'s own encoder by calling it.
fn base64_signature(bytes: &[u8; 80]) -> String {
    let placeholder = CreateOrder {
        account_index: 0,
        api_key_index: 2,
        market_index: 0,
        client_order_index: 1,
        base_amount: 1,
        price: 1,
        is_ask: 0,
        order_type: ORDER_TYPE_LIMIT,
        time_in_force: TIF_IMMEDIATE_OR_CANCEL,
        reduce_only: 0,
        trigger_price: NIL_TRIGGER_PRICE,
        order_expiry: NIL_ORDER_EXPIRY,
        expired_at: 1,
        nonce: 0,
    };
    let body = placeholder.to_json(bytes);
    body["Sig"]
        .as_str()
        .expect("the encoder writes a string")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Forty bytes of hex; obviously not a real key.
    const KEY: &str = "0101010101010101010101010101010101010101010101010101010101010101010101010101010f";

    /// A well-formed order whose only interesting field is the nonce.
    fn placeholder_order() -> CreateOrder {
        CreateOrder {
            account_index: 42,
            api_key_index: 3,
            market_index: 0,
            client_order_index: 1,
            base_amount: 1,
            price: 1,
            is_ask: 0,
            order_type: ORDER_TYPE_LIMIT,
            time_in_force: TIF_IMMEDIATE_OR_CANCEL,
            reduce_only: 0,
            trigger_price: NIL_TRIGGER_PRICE,
            order_expiry: NIL_ORDER_EXPIRY,
            expired_at: 1,
            nonce: 0,
        }
    }

    fn gateway() -> LighterGateway {
        LighterGateway::for_test(
            "http://127.0.0.1:1",
            LighterRealm::Testnet,
            LighterRealm::Testnet.credentials_for_test("42:3", KEY),
            vec!["BTCUSDT".to_string()],
        )
        .expect("test credentials")
    }

    #[test]
    fn the_venue_cannot_take_an_entry_that_names_a_leverage() {
        // Stated here because it is the difference between "a venue the switch
        // offers" and "a venue a strategy can open on". The engine refuses an
        // entry naming a leverage it cannot set, and the target-book follower
        // names one on every entry.
        assert!(!gateway().caps().set_leverage);
    }

    #[test]
    fn the_caps_say_only_what_this_adapter_actually_does() {
        let caps = gateway().caps();
        assert!(caps.native_position_stop);
        assert!(!caps.amend_in_place, "nothing here sends a modify");
        assert!(!caps.set_leverage, "nothing here sends a margin change");
    }

    #[test]
    fn the_order_kinds_map_to_the_venues_numbering() {
        let gw = gateway();
        assert_eq!(
            gw.order_type_and_tif(OrderKind::Market),
            (ORDER_TYPE_MARKET, TIF_IMMEDIATE_OR_CANCEL)
        );
        assert_eq!(
            gw.order_type_and_tif(OrderKind::Limit { px: 1.0, tif: TimeInForce::PostOnly }),
            (ORDER_TYPE_LIMIT, TIF_POST_ONLY)
        );
        assert_eq!(
            gw.order_type_and_tif(OrderKind::Limit { px: 1.0, tif: TimeInForce::Gtc }),
            (ORDER_TYPE_LIMIT, TIF_GOOD_TILL_TIME)
        );
        assert_eq!(
            gw.order_type_and_tif(OrderKind::Limit { px: 1.0, tif: TimeInForce::Ioc }),
            (ORDER_TYPE_LIMIT, TIF_IMMEDIATE_OR_CANCEL)
        );
    }

    #[test]
    fn a_key_that_is_not_forty_bytes_is_refused() {
        assert!(read_secret(KEY).is_ok());
        assert!(read_secret(&format!("0x{KEY}")).is_ok());
        for bad in ["", "0x", "zz", &"01".repeat(32)] {
            assert!(read_secret(bad).is_err(), "{bad:?} was accepted as a key");
        }
    }

    #[test]
    fn an_unknown_symbol_id_cannot_become_a_request() {
        let gw = gateway();
        assert!(gw.name_of(SymbolId(0)).is_ok());
        assert!(gw.name_of(SymbolId(7)).is_err());
    }

    #[test]
    fn added_symbols_keep_their_position_as_the_id() {
        let mut gw = gateway();
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.name_of(SymbolId(1)).unwrap(), "ETHUSDT");
    }

    #[test]
    fn the_auth_token_is_the_message_and_its_signature() {
        let gw = gateway();
        let token = gw.auth_token().expect("a token");
        let parts: Vec<&str> = token.split(':').collect();
        assert_eq!(parts.len(), 4, "deadline:account:key:signature — {token}");
        assert_eq!(parts[1], "42", "the account index");
        assert_eq!(parts[2], "3", "the API key slot");
        assert_eq!(parts[3].len(), 160, "eighty signature bytes as hex");
        let deadline: i64 = parts[0].parse().expect("a deadline");
        assert!(deadline > wall_ms() / 1000, "the token is already expired");
    }

    #[tokio::test]
    async fn a_send_that_does_not_arrive_forgets_the_nonce() {
        // The counter is advanced before the send. If the send then fails at
        // the socket, the venue may never have seen that nonce, and a counter
        // one ahead of the venue's refuses every order after it for the life
        // of the process. This gateway points at a closed port, so the post
        // fails as transport rather than as a refusal — which is the case that
        // says nothing about whether the venue consumed the nonce.
        let mut gw = gateway();
        gw.next_nonce = Some(7);
        let order = CreateOrder { nonce: 7, ..placeholder_order() };
        let hashed = order.hash(gw.realm.chain_id());
        let sent = gw
            .send_tx(TX_TYPE_CREATE_ORDER, &hashed, order.to_json(&[0u8; 80]))
            .await;
        assert!(matches!(sent, Err(VenueError::Transport(_))), "{sent:?}");
        assert_eq!(gw.next_nonce, None, "a dead socket left the counter advanced");
    }

    #[tokio::test]
    async fn nonces_are_handed_out_in_order_once_the_venue_has_been_asked() {
        // Seeded, so `take_nonce` hands out the counter rather than asking the
        // venue for it — the same path a second order in one boot takes.
        let mut gw = gateway();
        gw.next_nonce = Some(10);
        assert_eq!(gw.take_nonce().await.unwrap(), 10);
        assert_eq!(gw.take_nonce().await.unwrap(), 11);
        assert_eq!(gw.next_nonce, Some(12));
    }

    #[test]
    fn the_public_key_is_forty_bytes_of_hex() {
        let shown = gateway().public_key();
        assert_eq!(shown.len(), 80);
        assert!(shown.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn the_signature_travels_as_base64() {
        let encoded = base64_signature(&[7u8; 80]);
        assert_eq!(encoded.len(), 108);
        assert!(!encoded.contains(' '));
    }
}
