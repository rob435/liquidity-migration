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

use std::collections::HashMap;

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest, Side, TimeInForce, VenueExecution,
    VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use k256::ecdsa::SigningKey;
use serde_json::{json, Value};

use super::assets::{venue_px, venue_sz, Asset, Assets};
use super::cloid;
use super::parse::{
    all_accepted, first_status, parse_executions, parse_margin, parse_meta, parse_order_ack,
    parse_positions, parse_working_orders, stops_by_coin, venue_result,
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
use crate::{mono_ns, wall_ms};

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
    names: Vec<Symbol>,
    ids: HashMap<Symbol, SymbolId>,
    assets: Assets,
    /// Nonces must climb. Wall milliseconds do, except when two orders leave
    /// inside one millisecond, so the last one used is remembered.
    last_nonce: u64,
}

impl HyperliquidGateway {
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
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Ok(Self {
            realm,
            http: HttpClient::new(base_url),
            account,
            signer,
            names: symbols,
            ids,
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
        let text = serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.http.post(PATH_INFO, text, "application/json", &[]).await
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
        let text = serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        let envelope = self
            .http
            .post(PATH_EXCHANGE, text, "application/json", &[])
            .await?;
        venue_result(envelope)
    }

    async fn load_assets(&mut self) -> Result<(), VenueError> {
        let meta = self.info(json!({"type": "meta"})).await?;
        self.assets = Assets::from_rows(parse_meta(&meta)?);
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
    fn entry_wire(
        &self,
        req: &OrderRequest,
        asset: &Asset,
        reference_px: f64,
    ) -> Result<OrderWire, VenueError> {
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
                    OrderKindWire::Limit { tif: TimeInForce::Ioc },
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
    async fn mid_price(&self, coin: &str) -> Result<f64, VenueError> {
        let mids = self.info(json!({"type": "allMids"})).await?;
        let text = mids
            .get(coin)
            .and_then(Value::as_str)
            .ok_or_else(|| VenueError::BadReply(format!("the venue quotes no mid for {coin}")))?;
        let px: f64 = text
            .parse()
            .map_err(|_| VenueError::BadReply(format!("the mid for {coin} is not a number: {text:?}")))?;
        if !px.is_finite() || px <= 0.0 {
            return Err(VenueError::BadReply(format!("the mid for {coin} is {px}")));
        }
        Ok(px)
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
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let symbol = self.name_of(req.symbol)?.to_string();
        let asset = self.asset_for(&symbol).await?;

        // Only a market order needs a reference price, and only then is the
        // round trip for one paid.
        let reference_px = match req.kind {
            // The asset row's own spelling, not one folded from the engine's
            // symbol: `allMids` is keyed the way the venue writes the coin,
            // and `kPEPE` is not `KPEPE`.
            OrderKind::Market => self.mid_price(&asset.coin.clone()).await?,
            OrderKind::Limit { px, .. } => px,
        };
        let entry = self.entry_wire(req, &asset, reference_px)?;

        // The stop rides with the entry, so one signed action leaves the
        // position protected rather than two. Never on an exit: a reduce-only
        // order that closes a position has nothing left to protect, and the
        // venue would refuse the pair.
        let (orders, grouping) = match req.stop {
            Some(stop) if !req.reduce_only => (
                vec![
                    entry,
                    self.stop_wire(&asset, req.side, req.qty, stop.trigger_px)?,
                ],
                GROUPING_ORDER_TPSL,
            ),
            _ => (vec![entry], GROUPING_NONE),
        };

        let data = self.exchange(order_action(orders, grouping)).await?;
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
        let open = self.open_orders().await?;
        let rows = open
            .as_array()
            .ok_or_else(|| VenueError::BadReply("the open-order reply is not a list".to_string()))?;
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
        let px = match spec.px {
            Some(px) => venue_px(px, side, asset.sz_decimals)?,
            None => crate::json::num_field(current, "limitPx")?.to_string(),
        };
        let sz = match spec.qty {
            Some(qty) => venue_sz(qty, asset.sz_decimals)?,
            None => crate::json::num_field(current, "sz")?.to_string(),
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

        let order = OrderWire {
            asset: asset.index,
            is_buy,
            px,
            sz,
            reduce_only: current.get("reduceOnly").and_then(Value::as_bool).unwrap_or(false),
            kind: OrderKindWire::Limit { tif },
            cloid: Some(wanted.clone()),
        };
        let data = self.exchange(modify_action(&wanted, order)).await?;
        first_status(&data)?;
        Ok(())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.to_string();
        let asset = self.asset_for(&name).await?;
        // The venue's own spelling. Both reads below compare it against what
        // the venue wrote, and a coin folded up from the engine's symbol never
        // matches for the assets this venue names with a lower-case prefix —
        // leaving a position that cannot be protected and cannot be exited.
        let coin = asset.coin.clone();

        // What the stop has to cover, from the venue rather than from memory.
        let state = self
            .info(json!({"type": "clearinghouseState", "user": self.address_text()}))
            .await?;
        let (position_side, qty) = position_of(&state, &coin)?.ok_or_else(|| {
            VenueError::BadRequest(format!(
                "there is no open position in {name} for a stop to protect"
            ))
        })?;

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
        let stop = self.stop_wire(&asset, position_side, qty, trigger_px)?;
        let data = self
            .exchange(order_action(vec![stop], GROUPING_POSITION_TPSL))
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
        self.exchange(update_leverage_action(asset.index, true, capped)).await?;
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
        // Two reads, issued together: the account, and the open orders that
        // say which positions carry a stop. This venue keeps no stop on the
        // position row, so one read cannot answer both.
        let state = self.info(json!({
            "type": "clearinghouseState",
            "user": self.address_text(),
        }));
        let orders = self.open_orders();
        let (state, orders) = futures_util::future::try_join(state, orders).await?;
        let observed_ns = mono_ns();

        let (equity_usdt, available_usdt) = parse_margin(&state)?;
        let stops = stops_by_coin(&orders)?;
        let ids = &self.ids;
        let resolve = |name: &str| ids.get(name).copied();
        let positions = parse_positions(&state, &stops, &resolve)?;

        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions,
            observed_ns,
        })
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
    ) -> Result<Vec<VenueExecution>, VenueError> {
        // The venue answers at most 2000 fills per query and returns the
        // oldest first, so a long window walks forward from the last fill seen
        // rather than assuming one reply covered it.
        const PAGE_LIMIT: usize = 2000;
        const MAX_PAGES: usize = 20;
        let mut out: Vec<VenueExecution> = Vec::new();
        let mut from = start_ms;
        for _ in 0..MAX_PAGES {
            if from > end_ms {
                return Ok(out);
            }
            let page = self
                .info(json!({
                    "type": "userFillsByTime",
                    "user": self.address_text(),
                    "startTime": from,
                    "endTime": end_ms,
                }))
                .await?;
            let rows = parse_executions(&page)?;
            let count = rows.len();
            let newest = rows.iter().map(|r| r.venue_ts_ms).max();
            // Fills already held are dropped by their own id, so a page that
            // overlaps the last one does not double-count.
            for row in rows {
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

/// The side and size of an open position in one coin, or `None` when there is
/// none.
fn position_of(state: &Value, coin: &str) -> Result<Option<(Side, f64)>, VenueError> {
    let rows = state
        .get("assetPositions")
        .and_then(Value::as_array)
        .ok_or_else(|| VenueError::BadReply("no assetPositions in the account reply".to_string()))?;
    for row in rows {
        let Some(position) = row.get("position") else { continue };
        if position.get("coin").and_then(Value::as_str) != Some(coin) {
            continue;
        }
        let signed = crate::json::num_field(position, "szi")?;
        if signed == 0.0 {
            return Ok(None);
        }
        return Ok(Some((
            if signed > 0.0 { Side::Buy } else { Side::Sell },
            signed.abs(),
        )));
    }
    Ok(None)
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
        if !row.get("isTrigger").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        if !row.get("reduceOnly").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        let kind = row.get("orderType").and_then(Value::as_str).unwrap_or_default();
        if !kind.to_ascii_lowercase().starts_with("stop") {
            continue;
        }
        out.push(int_field(row, "oid")?);
    }
    Ok(out)
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
        assert!(matches!(buy.kind, OrderKindWire::Limit { tif: TimeInForce::Ioc }));
        let buy_px: f64 = buy.px.parse().unwrap();
        assert!(buy_px > 100_000.0, "a buy must cross: {buy_px}");
        assert!(buy_px <= 105_000.0, "and by no more than the slippage bound: {buy_px}");

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
                    OrderKind::Limit { px: 99_999.4, tif: TimeInForce::PostOnly },
                    Side::Buy,
                ),
                &asset(),
                0.0,
            )
            .unwrap();
        assert!(matches!(wire.kind, OrderKindWire::Limit { tif: TimeInForce::PostOnly }));
        // Rounded toward the passive side, so a post-only order is never
        // rounded into crossing.
        assert_eq!(wire.px, "99999");
        assert_eq!(wire.sz, "0.01");
        assert_eq!(wire.asset, 3);
        assert!(wire.is_buy);
        assert_eq!(wire.cloid.as_deref(), Some(cloid::to_cloid("eng-1700000000000-1").as_str()));
    }

    #[test]
    fn a_stop_sits_on_the_other_side_of_the_position_and_may_only_reduce() {
        let gw = gateway();
        let stop = gw.stop_wire(&asset(), Side::Buy, 0.01, 93_000.0).unwrap();
        assert!(!stop.is_buy, "the stop on a long must sell");
        assert!(stop.reduce_only);
        match stop.kind {
            OrderKindWire::Trigger { is_market, tpsl, .. } => {
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
        assert_eq!(position_of(&state, "BTC").unwrap(), Some((Side::Sell, 0.5)));
        assert_eq!(position_of(&state, "ETH").unwrap(), None);
        assert_eq!(position_of(&state, "SOL").unwrap(), None);
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
