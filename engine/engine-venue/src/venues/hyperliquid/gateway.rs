//! The Hyperliquid gateway: the [`VenueGateway`] contract over Hyperliquid's
//! `/info` and `/exchange` endpoints.
//!
//! One adapter, two networks. Which one it reaches is decided once, at
//! construction, by the [`HyperliquidRealm`] handed to
//! [`HyperliquidGateway::new`] — and the host is derived from that realm
//! rather than passed alongside it, so the two cannot disagree. The realm also
//! decides the byte that goes into every signature, so a testnet-signed order
//! cannot be replayed against the funded account.
//!
//! Three things differ from Bybit in a way a reader has to know about:
//!
//! - **There is no market order.** The venue takes limit orders only, so a
//!   market intent goes out as an immediate-or-cancel limit priced through the
//!   book by [`MARKET_SLIPPAGE`]. That is a bound on how far it may fill, not
//!   an expectation of where.
//! - **A stop is a separate order**, not a field on the position. An entry
//!   carries its stop as a second order in the same signed action, and moving
//!   a stop later pulls the old order before placing the new one.
//! - **Assets are numbered by position** in the venue's own list, so the list
//!   is read before the first order and re-read when a symbol is not in it.

#[path = "recovery.rs"]
mod recovery;

use crate::RealmCredentials;

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest, Side, TimeInForce, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use k256::ecdsa::SigningKey;
use serde_json::{json, Value};

use super::assets::{venue_px, venue_sz, Asset, Assets};
use super::cloid;
use super::parse::{
    all_accepted, first_status, parse_margin, parse_meta_raw, parse_order_ack, parse_positions,
    parse_working_orders, stops_by_coin, venue_result,
};
use super::realm::HyperliquidRealm;
use super::sign::{address_of, address_text, parse_address, parse_key, sign_l1_action};
use super::wire::{
    cancel_action, cancel_by_cloid_action, modify_action, order_action, tif_of,
    update_leverage_action, OrderKindWire, OrderWire, GROUPING_NONE, GROUPING_ORDER_TPSL,
    GROUPING_POSITION_TPSL, TPSL_STOP,
};
use crate::creds::Credentials;
use crate::http::HttpClient;
use crate::json::int_field;
use crate::{account_scan, mono_ns, wall_ms};

const PATH_INFO: &str = "/info";
const PATH_EXCHANGE: &str = "/exchange";

/// How far through the book an immediate-or-cancel limit may reach when the
/// engine asked for a market order. The venue's own SDK uses this number for
/// the same purpose. It bounds the fill; it does not predict it.
const MARKET_SLIPPAGE: f64 = 0.05;

/// Hyperliquid's account leverage is a whole number.
const MIN_LEVERAGE: i64 = 1;

pub struct HyperliquidGateway {
    realm: HyperliquidRealm,
    http: HttpClient,
    /// The account orders are placed for. Every `/info` read is addressed to
    /// it, and it is not the address that signs.
    account: [u8; 20],
    /// The API wallet the account approved. It signs and cannot withdraw.
    signer: SigningKey,
    symbols: engine_public::symbols::SymbolCatalog,
    assets: Assets,
    /// Nonces must climb. Wall milliseconds do, except when two orders leave
    /// inside one millisecond, so the last one used is remembered.
    last_nonce: u64,
}

impl HyperliquidGateway {
    async fn set_stop_terms(
        &mut self,
        symbol: SymbolId,
        trigger_px: f64,
        exact: Option<&engine_types::order_terms::ExactStopTerms>,
    ) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.to_string();
        let asset = self.asset_for(&name).await?;
        // The venue's own spelling. Both reads below compare it against what
        // the venue wrote, and a coin folded up from the engine's symbol never
        // matches for the assets this venue names with a lower-case prefix —
        // leaving a position that cannot be protected and cannot be exited.
        let coin = asset.coin.clone();

        // What the stop has to cover, from the venue rather than from memory.
        let raw: Box<serde_json::value::RawValue> = self
            .info_as(json!({"type": "clearinghouseState", "user": self.address_text()}))
            .await?;
        let (position_side, exact_qty) = crate::stop_state::hyperliquid(raw.get(), &coin)?;
        if exact.is_some_and(|terms| terms.position_side != position_side) {
            return Err(VenueError::BadRequest(
                "native position changed side before stop".into(),
            ));
        }

        // Which stops are standing now, read before anything is sent, so the
        // list is exactly the old ones and the replacement cannot be in it.
        let open = self.open_orders().await?;
        let old = stop_oids(&open, &coin)?;

        // The replacement first, the old ones after. The other order leaves
        // the position bare for the width of a round trip, and bare for good
        // if the placement then fails — which is the one state this call
        // exists to prevent. Two stops for a moment is harmless: whichever
        // fires first flattens the position, and the other can only reduce a
        // position that is already gone.
        let stop = match exact {
            Some(terms) => {
                let step = engine_types::numeric::Exact::parse_decimal(&format!(
                    "1e-{}",
                    asset.sz_decimals
                ))
                .map_err(crate::order_wire::error)?;
                if !exact_qty
                    .is_multiple_of(&step)
                    .map_err(crate::order_wire::error)?
                {
                    return Err(VenueError::BadReply(
                        "native stop quantity violates asset precision".into(),
                    ));
                }
                let text = engine_types::order_terms::decimal_wire(&terms.trigger_price)
                    .map_err(crate::order_wire::error)?;
                OrderWire {
                    asset: asset.index,
                    is_buy: position_side.flipped() == Side::Buy,
                    px: text.clone(),
                    sz: engine_types::order_terms::decimal_wire(&exact_qty)
                        .map_err(crate::order_wire::error)?,
                    reduce_only: true,
                    kind: OrderKindWire::Trigger {
                        is_market: true,
                        trigger_px: text,
                        tpsl: TPSL_STOP,
                    },
                    cloid: None,
                }
            }
            None => self.stop_wire(
                &asset,
                position_side,
                exact_qty.to_f64().map_err(crate::order_wire::error)?,
                trigger_px,
            )?,
        };
        let data = self
            .exchange(order_action(&[stop], GROUPING_POSITION_TPSL))
            .await?;
        first_status(&data)?;

        for oid in old {
            let data = self.exchange(cancel_action(asset.index, oid)).await?;
            // A stop that fired or was pulled between the read and here is
            // gone, which is the state this was asking for.
            if let Err(VenueError::Rejected { .. }) = first_status(&data) {
                tracing::debug!(oid, coin = %coin, "a standing stop was already gone");
            }
        }
        Ok(())
    }

    /// The live gateway: the realm's host, and the realm's credentials from
    /// the environment. There is no argument for the host on purpose — it is
    /// derived from the realm, so the account being addressed and the network
    /// being signed for are one decision.
    ///
    /// For `HyperliquidRealm::Mainnet` this fails unless the owner has armed
    /// `REAL_MONEY` on the host, and it fails at the credential read, before
    /// any socket is opened.
    pub fn new(realm: HyperliquidRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
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
        realm: HyperliquidRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Result<Self, VenueError> {
        Self::build(realm, base_url, creds, symbols)
    }

    fn build(
        realm: HyperliquidRealm,
        base_url: &str,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Result<Self, VenueError> {
        let account = parse_address(creds.key())?;
        let signer = parse_key(creds.secret())?;
        Ok(Self {
            realm,
            http: HttpClient::new(base_url),
            account,
            signer,
            symbols: engine_public::symbols::SymbolCatalog::from_names(symbols),
            assets: Assets::default(),
            last_nonce: 0,
        })
    }

    pub fn realm(&self) -> HyperliquidRealm {
        self.realm
    }

    /// Open the TLS session, and load the asset list, before an order needs
    /// either.
    pub async fn warm(&mut self) -> Result<(), VenueError> {
        self.load_assets().await
    }

    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        self.symbols.intern(name).expect("more than 65535 symbols")
    }

    pub fn symbols(&self) -> &[Symbol] {
        self.symbols.names()
    }

    fn name_of(&self, id: SymbolId) -> Result<&str, VenueError> {
        self.symbols
            .names()
            .get(id.0 as usize)
            .map(String::as_str)
            .ok_or_else(|| {
                VenueError::BadRequest(format!("symbol id {} is not in the gateway's table", id.0))
            })
    }

    fn address_text(&self) -> String {
        address_text(self.account)
    }

    /// The address the key on this host signs as — the API wallet, not the
    /// account. Worth having for an operator: "which wallet is this box
    /// trading with" is otherwise only answerable by deriving it by hand.
    pub fn signer_address(&self) -> String {
        address_text(address_of(&self.signer))
    }

    /// Strictly increasing, because the venue refuses a nonce it has seen.
    fn next_nonce(&mut self) -> u64 {
        let now = wall_ms().max(0) as u64;
        self.last_nonce = now.max(self.last_nonce + 1);
        self.last_nonce
    }

    async fn info(&self, body: Value) -> Result<Value, VenueError> {
        let text =
            serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.http
            .post(PATH_INFO, text, "application/json", &[])
            .await
    }

    async fn info_as<T: serde::de::DeserializeOwned>(&self, body: Value) -> Result<T, VenueError> {
        let text =
            serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.http
            .post_as(PATH_INFO, text, "application/json", &[])
            .await
    }

    /// Sign one action and send it. The value that was hashed is the value
    /// that is rendered, so what was signed and what goes out cannot differ.
    async fn exchange(&mut self, action: super::msgpack::Mp) -> Result<Value, VenueError> {
        let nonce = self.next_nonce();
        let signature = sign_l1_action(
            &self.signer,
            &action,
            None,
            nonce,
            None,
            self.realm.signs_as_mainnet(),
        )?;
        let body = json!({
            "action": super::wire::to_json(&action),
            "nonce": nonce,
            "signature": {"r": signature.r, "s": signature.s, "v": signature.v},
        });
        let text =
            serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        let envelope = self
            .http
            .post(PATH_EXCHANGE, text, "application/json", &[])
            .await?;
        venue_result(envelope)
    }

    async fn load_assets(&mut self) -> Result<(), VenueError> {
        let meta: Box<serde_json::value::RawValue> = self.info_as(json!({"type": "meta"})).await?;
        self.assets = Assets::from_rows(parse_meta_raw(meta.get())?);
        Ok(())
    }

    /// The asset a symbol names, reloading the venue's list once if it is not
    /// there yet — a symbol listed after this engine booted is the ordinary
    /// case, not a fault.
    async fn asset_for(&mut self, symbol: &str) -> Result<Asset, VenueError> {
        if self.assets.is_empty() {
            self.load_assets().await?;
        }
        if let Ok(asset) = self.assets.for_symbol(symbol) {
            return Ok(asset.clone());
        }
        self.load_assets().await?;
        self.assets.for_symbol(symbol).cloned()
    }

    async fn asset_of_id(&mut self, id: SymbolId) -> Result<Asset, VenueError> {
        let symbol = self.name_of(id)?.to_string();
        self.asset_for(&symbol).await
    }

    /// Build the entry as the venue takes it. A market intent becomes an
    /// immediate-or-cancel limit priced through the book, because this venue
    /// has no market order.
    #[cfg(test)]
    fn entry_wire(
        &self,
        req: &OrderRequest,
        asset: &Asset,
        reference_px: f64,
    ) -> Result<OrderWire, VenueError> {
        self.entry_wire_with_reference(req, asset, reference_px, None)
    }
    fn entry_wire_with_reference(
        &self,
        req: &OrderRequest,
        asset: &Asset,
        reference_px: f64,
        exact_reference: Option<&engine_types::numeric::Exact>,
    ) -> Result<OrderWire, VenueError> {
        if let Some(terms) = crate::order_wire::terms(req)? {
            use engine_types::numeric::Exact;
            use engine_types::order_terms::{decimal_wire, quantize_price, strategy_decimal};
            let spec = asset.exact_spec()?;
            terms
                .validate_wire_grid(
                    &spec,
                    req.kind,
                    engine_types::order_terms::QuantityPolicy::Normal,
                )
                .map_err(crate::order_wire::error)?;
            let (price, kind) = match req.kind {
                OrderKind::Market => {
                    let reference = match exact_reference {
                        Some(reference) => reference.clone(),
                        None => strategy_decimal(reference_px).map_err(crate::order_wire::error)?,
                    };
                    let slippage = Exact::parse_decimal(&MARKET_SLIPPAGE.to_string())
                        .map_err(crate::order_wire::error)?;
                    let multiplier = if req.side == Side::Buy {
                        Exact::one() + slippage
                    } else {
                        Exact::one() - slippage
                    };
                    let price =
                        quantize_price(&(reference * multiplier), req.side.flipped(), &spec)
                            .map_err(crate::order_wire::error)?;
                    (
                        price,
                        OrderKindWire::Limit {
                            tif: TimeInForce::Ioc,
                        },
                    )
                }
                OrderKind::Limit { tif, .. } => {
                    let price = terms.limit_price.as_ref().expect("validated limit");
                    if quantize_price(price, req.side, &spec).map_err(crate::order_wire::error)?
                        != *price
                    {
                        return Err(crate::order_wire::error(
                            "exact limit is not legal at venue precision",
                        ));
                    }
                    (price.clone(), OrderKindWire::Limit { tif })
                }
            };
            return Ok(OrderWire {
                asset: asset.index,
                is_buy: req.side == Side::Buy,
                px: decimal_wire(&price).map_err(crate::order_wire::error)?,
                sz: decimal_wire(&terms.quantity).map_err(crate::order_wire::error)?,
                reduce_only: req.reduce_only,
                kind,
                cloid: Some(cloid::to_cloid(&req.client_order_id)),
            });
        }
        let (px, kind) = match req.kind {
            OrderKind::Market => {
                let through = match req.side {
                    Side::Buy => reference_px * (1.0 + MARKET_SLIPPAGE),
                    Side::Sell => reference_px * (1.0 - MARKET_SLIPPAGE),
                };
                // Priced toward the side that crosses, which is the opposite
                // of the passive rounding a resting order wants.
                (
                    venue_px(through, req.side.flipped(), asset.sz_decimals)?,
                    OrderKindWire::Limit {
                        tif: TimeInForce::Ioc,
                    },
                )
            }
            OrderKind::Limit { px, tif } => (
                venue_px(px, req.side, asset.sz_decimals)?,
                OrderKindWire::Limit { tif },
            ),
        };
        Ok(OrderWire {
            asset: asset.index,
            is_buy: matches!(req.side, Side::Buy),
            px,
            sz: venue_sz(req.qty, asset.sz_decimals)?,
            reduce_only: req.reduce_only,
            kind,
            cloid: Some(cloid::to_cloid(&req.client_order_id)),
        })
    }

    fn attached_stop_wire(
        &self,
        asset: &Asset,
        req: &OrderRequest,
        trigger_px: f64,
    ) -> Result<OrderWire, VenueError> {
        match crate::order_wire::terms(req)? {
            None => self.stop_wire(asset, req.side, req.qty, trigger_px),
            Some(terms) => {
                use engine_types::order_terms::{decimal_wire, quantize_price};
                let trigger = terms
                    .physical_stop_trigger_price
                    .as_ref()
                    .expect("validated physical stop");
                if quantize_price(trigger, req.side.flipped(), &asset.exact_spec()?)
                    .map_err(crate::order_wire::error)?
                    != *trigger
                {
                    return Err(crate::order_wire::error(
                        "exact stop is not legal at venue precision",
                    ));
                }
                let text = decimal_wire(trigger).map_err(crate::order_wire::error)?;
                Ok(OrderWire {
                    asset: asset.index,
                    is_buy: req.side.flipped() == Side::Buy,
                    px: text.clone(),
                    sz: decimal_wire(&terms.quantity).map_err(crate::order_wire::error)?,
                    reduce_only: true,
                    kind: OrderKindWire::Trigger {
                        is_market: true,
                        trigger_px: text,
                        tpsl: TPSL_STOP,
                    },
                    cloid: None,
                })
            }
        }
    }

    /// A stop, as the reduce-only trigger order this venue keeps stops as.
    ///
    /// The limit price is the trigger itself: `is_market` makes it cross when
    /// it fires, and the venue reads the limit price only as a bound. A stop
    /// that cannot fill is not a stop, so it crosses.
    fn stop_wire(
        &self,
        asset: &Asset,
        position_side: Side,
        qty: f64,
        trigger_px: f64,
    ) -> Result<OrderWire, VenueError> {
        // The stop closes the position, so it is on the other side of it.
        let exit_side = position_side.flipped();
        let text = venue_px(trigger_px, exit_side, asset.sz_decimals)?;
        Ok(OrderWire {
            asset: asset.index,
            is_buy: matches!(exit_side, Side::Buy),
            px: text.clone(),
            sz: venue_sz(qty, asset.sz_decimals)?,
            reduce_only: true,
            kind: OrderKindWire::Trigger {
                is_market: true,
                trigger_px: text,
                tpsl: TPSL_STOP,
            },
            cloid: None,
        })
    }

    /// The venue's mid prices, for pricing a market order through the book.
    async fn mid_price(
        &self,
        coin: &str,
    ) -> Result<engine_types::numeric::ExactNumber, VenueError> {
        let mids: std::collections::BTreeMap<String, engine_public::numeric_wire::DecimalField> =
            self.http
                .post_as(
                    PATH_INFO,
                    r#"{"type":"allMids"}"#.to_owned(),
                    "application/json",
                    &[],
                )
                .await?;
        let number = mids
            .get(coin)
            .ok_or_else(|| VenueError::BadReply(format!("the venue quotes no mid for {coin}")))?
            .required("mid")?;
        if !number.value.is_positive() {
            return Err(VenueError::BadReply("mid price is not positive".into()));
        }
        number.value.to_f64().map_err(crate::order_wire::error)?;
        Ok(number)
    }

    async fn open_orders(&self) -> Result<Value, VenueError> {
        self.info(json!({
            "type": "frontendOpenOrders",
            "user": self.address_text(),
        }))
        .await
    }
}

#[engine_types::async_trait]
impl VenueGateway for HyperliquidGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            // Kept by the venue as a reduce-only stop trigger that outlives
            // this process, which is what the engine needs of it: the entry
            // carries its stop in the same signed action, and `set_stop`
            // replaces it. Not a field on the position, which is why
            // `account_view` reads the open orders to answer whether a
            // position is protected.
            native_position_stop: true,
            // The `batchModify` action, addressed by the engine's own client
            // order id.
            amend_in_place: true,
            // The `updateLeverage` action.
            set_leverage: true,
            close_position_below_minimum: false,
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let symbol = self.name_of(req.symbol)?.to_string();
        let asset = self.asset_for(&symbol).await?;

        // Only a market order needs a reference price, and only then is the
        // round trip for one paid.
        let reference = match req.kind {
            OrderKind::Market => Some(self.mid_price(&asset.coin).await?),
            OrderKind::Limit { .. } => None,
        };
        let reference_px = match req.kind {
            OrderKind::Limit { px, .. } => px,
            OrderKind::Market => reference
                .as_ref()
                .expect("market reference")
                .value
                .to_f64()
                .map_err(crate::order_wire::error)?,
        };
        let entry = self.entry_wire_with_reference(
            req,
            &asset,
            reference_px,
            reference.as_ref().map(|number| &number.value),
        )?;

        // The stop rides with the entry, so one signed action leaves the
        // position protected rather than two. Never on an exit: a reduce-only
        // order that closes a position has nothing left to protect, and the
        // venue would refuse the pair.
        let (orders, grouping) = match req.stop {
            Some(stop) if !req.reduce_only => (
                vec![
                    entry,
                    self.attached_stop_wire(&asset, req, stop.trigger_px)?,
                ],
                GROUPING_ORDER_TPSL,
            ),
            _ => (vec![entry], GROUPING_NONE),
        };

        let data = self.exchange(order_action(&orders, grouping)).await?;
        let ack_ns = mono_ns();
        // Every status, not just the first: an entry accepted with its stop
        // refused would otherwise be recorded as a protected position.
        let statuses = all_accepted(&data)?;
        parse_order_ack(&statuses[0], &req.client_order_id, ack_ns)
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        let asset = self.asset_of_id(symbol).await?;
        let data = self
            .exchange(cancel_by_cloid_action(
                asset.index,
                &cloid::to_cloid(client_order_id),
            ))
            .await?;
        first_status(&data)?;
        Ok(())
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        crate::order_wire::amend_terms(&spec)?;
        if spec.px.is_none() && spec.qty.is_none() {
            return Err(VenueError::BadRequest(
                "an amend that changes neither price nor size".to_string(),
            ));
        }
        // The venue's modify replaces the whole order, so an amend that only
        // moves the price still has to say what the size is. The current order
        // is read back for the half that is not changing rather than assumed.
        let asset = self.asset_of_id(symbol).await?;
        let wanted = cloid::to_cloid(client_order_id);
        let raw_rows: Vec<Box<serde_json::value::RawValue>> = self
            .info_as(json!({"type":"frontendOpenOrders","user":self.address_text()}))
            .await?;
        let open: Value = serde_json::from_str(
            &serde_json::to_string(&raw_rows).map_err(crate::order_wire::error)?,
        )
        .map_err(|e| VenueError::BadReply(e.to_string()))?;
        let rows = open.as_array().ok_or_else(|| {
            VenueError::BadReply("the open-order reply is not a list".to_string())
        })?;
        let current = rows
            .iter()
            .find(|row| row.get("cloid").and_then(Value::as_str) == Some(wanted.as_str()))
            .ok_or_else(|| {
                VenueError::BadRequest(format!(
                    "the venue is not working an order named {client_order_id}"
                ))
            })?;

        let is_buy = match current.get("side").and_then(Value::as_str) {
            Some("B") => true,
            Some("A") => false,
            other => {
                return Err(VenueError::BadReply(format!(
                    "side is {other:?}, and this venue writes A for ask or B for bid"
                )))
            }
        };
        let side = if is_buy { Side::Buy } else { Side::Sell };
        let (px, sz) = if let Some(terms) = crate::order_wire::amend_terms(&spec)? {
            let raw = raw_rows
                .iter()
                .find(|raw| {
                    #[derive(serde::Deserialize)]
                    struct Identity {
                        cloid: String,
                    }
                    serde_json::from_str::<Identity>(raw.get()).is_ok_and(|row| row.cloid == wanted)
                })
                .ok_or_else(|| VenueError::BadReply("amend lookup lost its raw row".into()))?;
            let (old_px, old_qty) =
                crate::amend_state::resting_hyperliquid(raw.get(), &asset.coin, &wanted)?;
            let px = terms.limit_price.as_ref().unwrap_or(&old_px);
            let qty = terms.quantity.as_ref().unwrap_or(&old_qty);
            let effective = engine_types::order_terms::ExactOrderTerms {
                quantity: qty.clone(),
                limit_price: Some(px.clone()),
                stop_trigger_price: None,
                physical_stop_trigger_price: None,
                input_policy: engine_types::order_terms::OrderInputPolicy::StrategyShortestDecimal,
            };
            effective
                .validate_wire_grid(
                    &asset.exact_spec()?,
                    OrderKind::Limit {
                        px: px.to_f64().map_err(crate::order_wire::error)?,
                        tif: TimeInForce::Gtc,
                    },
                    engine_types::order_terms::QuantityPolicy::Normal,
                )
                .map_err(crate::order_wire::error)?;
            (
                engine_types::order_terms::decimal_wire(px).map_err(crate::order_wire::error)?,
                engine_types::order_terms::decimal_wire(qty).map_err(crate::order_wire::error)?,
            )
        } else {
            (
                match spec.px {
                    Some(px) => venue_px(px, side, asset.sz_decimals)?,
                    None => crate::json::num_field(current, "limitPx")?.to_string(),
                },
                match spec.qty {
                    Some(qty) => venue_sz(qty, asset.sz_decimals)?,
                    None => crate::json::num_field(current, "sz")?.to_string(),
                },
            )
        };

        // The venue's modify replaces the order outright, so the
        // time-in-force has to be carried across with everything else. Reading
        // it back rather than defaulting is the whole point: defaulting to
        // `Gtc` would turn a resting post-only quote into one that can cross,
        // and the first the engine would hear of it is a taker fee.
        let Some(tif) = current.get("tif").and_then(Value::as_str).and_then(tif_of) else {
            return Err(VenueError::BadReply(format!(
                "the venue's row for {client_order_id} does not say its time-in-force, and \
                 amending it would have to guess whether it may cross"
            )));
        };

        let reduce_only = match current.get("reduceOnly").and_then(Value::as_bool) {
            Some(value) => value,
            None if spec.exact_terms.is_some() => {
                return Err(VenueError::BadReply(
                    "amend lookup has no readable reduce-only flag".into(),
                ))
            }
            None => false,
        };
        let order = OrderWire {
            asset: asset.index,
            is_buy,
            px,
            sz,
            reduce_only,
            kind: OrderKindWire::Limit { tif },
            cloid: Some(wanted.clone()),
        };
        let data = self.exchange(modify_action(&wanted, &order)).await?;
        first_status(&data)?;
        Ok(())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        self.set_stop_terms(symbol, trigger_px, None).await
    }

    async fn set_stop_exact(
        &mut self,
        symbol: SymbolId,
        terms: &engine_types::order_terms::ExactStopTerms,
    ) -> Result<(), VenueError> {
        terms
            .validate_wire_grid(
                &self
                    .assets
                    .for_symbol(self.name_of(symbol)?)?
                    .exact_spec()?,
            )
            .map_err(crate::order_wire::error)?;
        self.set_stop_terms(
            symbol,
            terms
                .trigger_price
                .to_f64()
                .map_err(crate::order_wire::error)?,
            Some(terms),
        )
        .await
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        Some(HyperliquidGateway::add_symbol(self, symbol))
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        let asset = self.asset_of_id(symbol).await?;
        if !leverage.is_finite() || leverage < 1.0 {
            return Err(VenueError::BadRequest(format!(
                "{leverage} is not a leverage this venue takes"
            )));
        }
        // Whole numbers only, and rounded DOWN: asking for 2.9 and getting 3
        // would post less margin than the risk kernel priced.
        let whole = (leverage.floor() as i64).max(MIN_LEVERAGE);
        let capped = whole.min(asset.max_leverage.floor().max(1.0) as i64);
        // Cross margin, which is how the venue's accounts are held here and
        // what `clearinghouseState`'s equity means.
        self.exchange(update_leverage_action(asset.index, true, capped))
            .await?;
        Ok(())
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        // There is no "who am I" endpoint: the account is its address, and the
        // address is what every read is already addressed to. What is worth
        // checking is that the key on this host actually signs for it — either
        // as the account itself, or as an API wallet the account approved.
        let signer = address_of(&self.signer);
        if signer != self.account {
            let approved = self
                .info(json!({
                    "type": "extraAgents",
                    "user": self.address_text(),
                }))
                .await?;
            let wanted = address_text(signer);
            let listed = approved
                .as_array()
                .map(|rows| {
                    rows.iter().any(|row| {
                        row.get("address")
                            .and_then(Value::as_str)
                            .is_some_and(|a| a.eq_ignore_ascii_case(&wanted))
                    })
                })
                .unwrap_or(false);
            if !listed {
                return Err(VenueError::Credentials(format!(
                    "the key on this host signs as {wanted}, which the account {} has not \
                     approved as an API wallet — orders signed by it would be refused",
                    self.address_text()
                )));
            }
        }

        Ok(AccountIdentity {
            venue: super::VENUE_NAME.to_string(),
            user_id: self.address_text(),
            realm: self.realm.as_str().to_string(),
        })
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        engine_types::orders::AccountRecoveryClient::account_view(
            &recovery::RecoveryClient::new(self),
            self.symbols.names(),
        )
        .await
    }

    fn restore_instrument_catalog(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        let pages = crate::catalog_checkpoint::decode(checkpoint, "hyperliquid", self.http.base())?;
        let catalog = catalog_from_pages(self.http.base(), pages)?;
        crate::catalog_checkpoint::check(checkpoint, catalog)
    }
    fn install_instrument_catalog(
        &mut self,
        catalog: &engine_types::orders::InstrumentCatalog,
    ) -> Result<(), VenueError> {
        let snapshot = catalog
            .cache
            .as_ref()
            .and_then(|cache| cache.as_ref().as_any().downcast_ref::<CatalogSnapshot>())
            .ok_or_else(|| VenueError::BadRequest("catalog belongs to another adapter".into()))?;
        if snapshot.base != self.http.base() {
            return Err(VenueError::BadRequest(
                "catalog belongs to another venue endpoint".into(),
            ));
        }
        self.assets = snapshot.data.clone();
        Ok(())
    }

    fn account_recovery_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::AccountRecoveryClient>> {
        Some(Box::new(recovery::RecoveryClient::new(self)))
    }

    fn instrument_catalog_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::InstrumentCatalogClient>> {
        Some(Box::new(LookupClient {
            http: self.http.clone(),
            account: self.address_text(),
        }))
    }

    fn order_lookup_client(&self) -> Option<Box<dyn engine_types::orders::OrderLookupClient>> {
        Some(Box::new(LookupClient {
            http: self.http.clone(),
            account: self.address_text(),
        }))
    }

    async fn order_status(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        let name = self.name_of(symbol)?.to_owned();
        let raw:Box<serde_json::value::RawValue>=self.info_as(json!({"type":"orderStatus","user":self.address_text(),"oid":cloid::to_cloid(client_order_id)})).await?;
        super::lookup::parse(raw.get(), &name, client_order_id)
    }

    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)>, VenueError> {
        self.load_assets().await?;
        self.assets.instrument_specs()
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.load_assets().await?;
        Ok(self.assets.instrument_rules())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        // Addressed to the account, not to a symbol list: the point of this
        // read is to find orders nobody here placed, and asking only about the
        // symbols the engine knows would hide exactly those.
        let open = self.open_orders().await?;
        parse_working_orders(&open)
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        engine_types::orders::AccountRecoveryClient::executions(
            &recovery::RecoveryClient::new(self),
            self.symbols.names(),
            start_ms,
            end_ms,
        )
        .await
    }
}

/// The venue's order numbers of every reduce-only stop standing on one coin.
fn stop_oids(orders: &Value, coin: &str) -> Result<Vec<i64>, VenueError> {
    let rows = orders
        .as_array()
        .ok_or_else(|| VenueError::BadReply("the open-order reply is not a list".to_string()))?;
    let mut out = Vec::new();
    for row in rows {
        if row.get("coin").and_then(Value::as_str) != Some(coin) {
            continue;
        }
        if !row
            .get("isTrigger")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        if !row
            .get("reduceOnly")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let kind = row
            .get("orderType")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !kind.to_ascii_lowercase().starts_with("stop") {
            continue;
        }
        out.push(int_field(row, "oid")?);
    }
    Ok(out)
}

#[derive(Debug)]
struct CatalogSnapshot {
    base: String,
    pages: Vec<String>,
    data: Assets,
}

impl engine_types::orders::InstrumentCatalogCache for CatalogSnapshot {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
    fn checkpoint(
        &self,
    ) -> Result<engine_types::orders::InstrumentCatalogCacheSnapshot, VenueError> {
        crate::catalog_checkpoint::encode("hyperliquid", &self.base, &self.pages)
    }
    fn retain_previous(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        let previous = crate::catalog_checkpoint::decode(checkpoint, "hyperliquid", &self.base)?;
        crate::catalog_checkpoint::check(
            checkpoint,
            catalog_from_pages(&self.base, previous.clone())?,
        )?;
        let pages =
            crate::catalog_checkpoint::merge_pages("hyperliquid", previous, self.pages.clone())?;
        let catalog = catalog_from_pages(&self.base, pages)?;
        catalog.checkpoint()?.validate_bounds()?;
        Ok(catalog)
    }
}

#[engine_types::async_trait]
impl engine_types::orders::InstrumentCatalogClient for LookupClient {
    async fn fetch(&self) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        let raw: Box<serde_json::value::RawValue> = self
            .http
            .post_as(
                PATH_INFO,
                r#"{"type":"meta"}"#.to_owned(),
                "application/json",
                &[],
            )
            .await?;
        catalog_from_pages(self.http.base(), vec![raw.get().to_owned()])
    }
}

struct LookupClient {
    http: HttpClient,
    account: String,
}
#[engine_types::async_trait]
impl engine_types::orders::OrderLookupClient for LookupClient {
    async fn lookup(
        &self,
        name: &str,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        let body = json!({"type":"orderStatus", "user":self.account, "oid":cloid::to_cloid(client_order_id)}).to_string();
        let raw: Box<serde_json::value::RawValue> = self
            .http
            .post_as(PATH_INFO, body, "application/json", &[])
            .await?;
        super::lookup::parse(raw.get(), name, client_order_id)
    }
}

fn catalog_from_pages(
    base: &str,
    pages: Vec<String>,
) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
    if pages.len() != 1 {
        return Err(VenueError::BadReply(
            "catalog requires one metadata page".into(),
        ));
    }
    let raw = pages[0].as_str();
    let catalog = Assets::from_rows(parse_meta_raw(raw)?);
    Ok(engine_types::orders::InstrumentCatalog {
        rules: catalog.instrument_rules(),
        specs: catalog.instrument_specs()?,
        cache: Some(std::sync::Arc::new(CatalogSnapshot {
            pages,
            base: base.to_owned(),
            data: catalog,
        })),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gateway() -> HyperliquidGateway {
        HyperliquidGateway::for_test(
            "http://127.0.0.1:1",
            HyperliquidRealm::Testnet,
            HyperliquidRealm::Testnet.credentials_for_test(
                "0x0000000000000000000000000000000000000001",
                "0x0123456789012345678901234567890123456789012345678901234567890123",
            ),
            vec!["BTCUSDT".to_string()],
        )
        .expect("test credentials")
    }

    fn asset() -> Asset {
        Asset {
            coin: "BTC".to_string(),
            index: 3,
            sz_decimals: 5,
            max_leverage: 40.0,
        }
    }

    fn request(kind: OrderKind, side: Side) -> OrderRequest {
        OrderRequest {
            client_order_id: "eng-1700000000000-1".to_string(),
            strategy: engine_types::StrategyId(0),
            symbol: SymbolId(0),
            side,
            qty: 0.01,
            kind,
            stop: None,
            reduce_only: false,
            exact_terms: None,
            sleeve_effect: None,
            close_position: false,
        }
    }

    #[test]
    fn a_market_intent_becomes_an_ioc_limit_priced_through_the_book() {
        // The venue has no market order, so this is what one turns into. A
        // buy is priced above the mid and a sell below it; both cross.
        let gw = gateway();
        let buy = gw
            .entry_wire(&request(OrderKind::Market, Side::Buy), &asset(), 100_000.0)
            .unwrap();
        assert!(matches!(
            buy.kind,
            OrderKindWire::Limit {
                tif: TimeInForce::Ioc
            }
        ));
        let buy_px: f64 = buy.px.parse().unwrap();
        assert!(buy_px > 100_000.0, "a buy must cross: {buy_px}");
        assert!(
            buy_px <= 105_000.0,
            "and by no more than the slippage bound: {buy_px}"
        );

        let sell = gw
            .entry_wire(&request(OrderKind::Market, Side::Sell), &asset(), 100_000.0)
            .unwrap();
        let sell_px: f64 = sell.px.parse().unwrap();
        assert!(sell_px < 100_000.0, "a sell must cross: {sell_px}");
        assert!(sell_px >= 95_000.0, "{sell_px}");
    }

    #[test]
    fn a_limit_intent_keeps_its_price_and_its_time_in_force() {
        let gw = gateway();
        let wire = gw
            .entry_wire(
                &request(
                    OrderKind::Limit {
                        px: 99_999.4,
                        tif: TimeInForce::PostOnly,
                    },
                    Side::Buy,
                ),
                &asset(),
                0.0,
            )
            .unwrap();
        assert!(matches!(
            wire.kind,
            OrderKindWire::Limit {
                tif: TimeInForce::PostOnly
            }
        ));
        // Rounded toward the passive side, so a post-only order is never
        // rounded into crossing.
        assert_eq!(wire.px, "99999");
        assert_eq!(wire.sz, "0.01");
        assert_eq!(wire.asset, 3);
        assert!(wire.is_buy);
        assert_eq!(
            wire.cloid.as_deref(),
            Some(cloid::to_cloid("eng-1700000000000-1").as_str())
        );
    }

    #[test]
    fn a_stop_sits_on_the_other_side_of_the_position_and_may_only_reduce() {
        let gw = gateway();
        let stop = gw.stop_wire(&asset(), Side::Buy, 0.01, 93_000.0).unwrap();
        assert!(!stop.is_buy, "the stop on a long must sell");
        assert!(stop.reduce_only);
        match stop.kind {
            OrderKindWire::Trigger {
                is_market, tpsl, ..
            } => {
                assert!(is_market, "a stop that cannot fill is not a stop");
                assert_eq!(tpsl, "sl");
            }
            other => panic!("a stop must be a trigger order, got {other:?}"),
        }

        let on_short = gw.stop_wire(&asset(), Side::Sell, 0.01, 97_000.0).unwrap();
        assert!(on_short.is_buy, "the stop on a short must buy");
    }

    #[test]
    fn nonces_climb_even_inside_one_millisecond() {
        // The venue refuses a nonce it has already seen, so two orders sent in
        // the same millisecond must not carry the same number.
        let mut gw = gateway();
        let first = gw.next_nonce();
        let second = gw.next_nonce();
        let third = gw.next_nonce();
        assert!(second > first, "{first} then {second}");
        assert!(third > second);
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
    fn the_caps_say_what_this_adapter_can_actually_do() {
        let caps = gateway().caps();
        assert!(caps.native_position_stop);
        assert!(caps.amend_in_place);
        assert!(caps.set_leverage);
    }

    #[test]
    fn a_position_is_found_by_its_coin_and_a_flat_one_is_not_a_position() {
        let state = serde_json::json!({"assetPositions": [
            {"position": {"coin": "BTC", "szi": "-0.5", "entryPx": "95000"}},
            {"position": {"coin": "ETH", "szi": "0", "entryPx": "0"}}
        ]});
        assert_eq!(
            crate::stop_state::hyperliquid(&state.to_string(), "BTC").unwrap(),
            (
                Side::Sell,
                engine_types::numeric::Exact::parse_decimal("0.5").unwrap()
            )
        );
        assert!(matches!(
            crate::stop_state::hyperliquid(&state.to_string(), "ETH"),
            Err(VenueError::BadRequest(_))
        ));
        assert!(matches!(
            crate::stop_state::hyperliquid(&state.to_string(), "SOL"),
            Err(VenueError::BadRequest(_))
        ));
    }

    #[test]
    fn only_this_coins_reduce_only_stops_are_pulled_when_a_stop_moves() {
        // Pulling another coin's stop would leave that position unprotected,
        // and pulling an entry would cancel a trade nobody asked to cancel.
        let orders = serde_json::json!([
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "oid": 1},
            {"coin": "ETH", "isTrigger": true, "reduceOnly": true,
             "orderType": "Stop Market", "oid": 2},
            {"coin": "BTC", "isTrigger": true, "reduceOnly": true,
             "orderType": "Take Profit Market", "oid": 3},
            {"coin": "BTC", "isTrigger": false, "reduceOnly": false,
             "orderType": "Limit", "oid": 4}
        ]);
        assert_eq!(stop_oids(&orders, "BTC").unwrap(), vec![1]);
        assert_eq!(stop_oids(&orders, "ETH").unwrap(), vec![2]);
    }
}
