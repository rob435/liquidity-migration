//! The MEXC gateway: the [`VenueGateway`] contract over MEXC futures.
//!
//! One adapter, one realm, and that realm is funded — the host is derived from
//! it rather than passed alongside it, so the account being addressed and the
//! account being signed for are one decision.
//!
//! **Sizes cross this boundary in two units.** The engine speaks base coin;
//! MEXC counts contracts. [`super::contracts`] converts both ways and is the
//! only place that does.
//!
//! **The stop is sent in two forms, on purpose.** An entry carries its stop in
//! the same signed call, so there is no window where a filled position is
//! unprotected. [`MexcGateway::set_stop`] then works the venue's
//! position-level record, which is the one whose size tracks the position.
//! Every stop this adapter writes states `volType`, `stopLossReverse` and
//! `takeProfitReverse` explicitly: MEXC documents none of their defaults, and
//! a reverse stop would open an opposite position instead of flattening one.

#[path = "recovery.rs"]
mod recovery;

use crate::RealmCredentials;
use std::collections::HashMap;

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AccountInventory, AccountOrder, AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest,
    Side, TimeInForce, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use serde_json::{json, Value};

use super::account_binding::AccountBinding;
use super::contracts::{Ceiling, Contracts};
use super::parse::{
    id_text, parse_assets, parse_inventory_assets, parse_inventory_positions,
    parse_inventory_stop_orders, parse_open_orders, parse_order_ack, parse_position_stops,
    parse_positions, venue_result, SETTLE_CURRENCY,
};
use super::realm::MexcRealm;
use super::rest::{OperationClass, RestClient};
use super::VENUE_NAME;
use crate::creds::Credentials;
use crate::fmt::venue_num;
use crate::{account_scan, mono_ns, wall_ms};

const PATH_CONTRACT_DETAIL: &str = "/api/v1/contract/detail";
const PATH_PING: &str = "/api/v1/contract/ping";
const PATH_ASSETS: &str = "/api/v1/private/account/assets";
const PATH_POSITIONS: &str = "/api/v1/private/position/open_positions";
const PATH_OPEN_ORDERS: &str = "/api/v1/private/order/list/open_orders";
const PATH_DEALS: &str = "/api/v1/private/order/list/order_deals/v3";
const PATH_ORDER_CREATE: &str = "/api/v1/private/order/create";
const PATH_ORDER_CANCEL_EXTERNAL: &str = "/api/v1/private/order/cancel_with_external";
const PATH_STOP_PLACE: &str = "/api/v1/private/stoporder/place";
const PATH_STOP_CHANGE: &str = "/api/v1/private/stoporder/change_plan_price";
const PATH_STOP_OPEN: &str = "/api/v1/private/stoporder/open_orders";
const PATH_LEVERAGE: &str = "/api/v1/private/position/change_leverage";

/// Cross margin.
///
/// Not a preference — isolated margin makes MEXC require a `leverage` on every
/// order, and this engine decides leverage separately (`set_leverage`, called
/// before an entry that names one) so an `OrderRequest` carries none. Cross is
/// also what the account-wide caps in the risk kernel already reason about.
const OPEN_TYPE_CROSS: i64 = 2;

/// One-way mode. The engine holds one position per symbol; MEXC defaults to
/// hedge mode, where a symbol can carry a long and a short at once with
/// separate ids. Reduce-only is also documented as one-way only, so an exit
/// depends on this.
const POSITION_MODE_ONE_WAY: i64 = 2;

/// "Position TP/SL": the record's size tracks the position. The other value is
/// a fixed quantity, which would leave a position that grew partly unguarded.
const VOL_TYPE_POSITION: i64 = 2;

/// "No". A reversing stop opens an opposite position instead of closing the
/// one it guarded — a new position, carrying no stop of its own.
const REVERSE_NO: i64 = 2;

const CATALOG_KIND_V2: &str = "mexc-execution-v2";

/// The engine's own pages of a venue list. Enough to cover any account this
/// engine runs; a cursor that never empties is a venue fault, not a reason to
/// loop forever.
const MAX_PAGES: u32 = 20;
const PAGE_SIZE: u32 = 100;

fn execution_page_complete(symbol: &str, page: u32, raw_count: usize) -> Result<bool, VenueError> {
    if raw_count > PAGE_SIZE as usize {
        return Err(VenueError::BadReply(format!(
            "execution page {page} for {symbol} returned {raw_count} rows after requesting {PAGE_SIZE}"
        )));
    }
    if raw_count < PAGE_SIZE as usize {
        return Ok(true);
    }
    Ok(false)
}

pub struct MexcGateway {
    realm: MexcRealm,
    account_binding: AccountBinding,
    rest: RestClient,
    symbols: engine_public::symbols::SymbolCatalog,
    /// The venue's contract table, read once and kept. Every size that crosses
    /// this boundary needs it, so an adapter without it can convert nothing.
    contracts: Contracts,
    lookup_catalog: std::sync::Arc<std::sync::RwLock<Contracts>>,
}

impl MexcGateway {
    async fn set_stop_terms(
        &mut self,
        symbol: SymbolId,
        trigger_px: f64,
        exact: Option<&engine_types::order_terms::ExactStopTerms>,
    ) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        let loss_trend = self.contracts().await?.existing(&name)?.stop_trend()?;
        let position_id = if let Some(terms) = exact {
            let venue_symbol = self.contracts.existing(&name)?.venue_symbol.clone();
            let raw: Box<serde_json::value::RawValue> = self
                .rest
                .get_signed_as_for(PATH_POSITIONS, &[], OperationClass::Protection)
                .await?;
            let (id, side) = crate::stop_state::mexc(raw.get(), &venue_symbol)?;
            if side != terms.position_side {
                return Err(VenueError::BadRequest(
                    "native position changed side before stop".into(),
                ));
            }
            id
        } else {
            self.position_ids(OperationClass::Protection)
                .await?
                .get(&name)
                .cloned()
                .ok_or_else(|| {
                    VenueError::BadRequest(format!(
                        "MEXC holds no position on {name}, and a stop here is attached to one"
                    ))
                })?
        };

        // A stop already on this position is moved rather than added to. MEXC
        // lets several records coexist on one position, so placing a second
        // would leave two live stops with no way to tell which fires.
        let records = self.stop_records().await?;
        let existing = records
            .as_array()
            .into_iter()
            .flatten()
            .find(|row| {
                id_text(row, "positionId").as_deref() == Some(position_id.as_str())
                    && id_text(row, "orderId").as_deref().unwrap_or("0") == "0"
            })
            .and_then(|row| id_text(row, "id"));

        let price = match exact {
            Some(terms) => engine_types::order_terms::decimal_wire(&terms.trigger_price)
                .map_err(crate::order_wire::error)?,
            None => venue_num(trigger_px)?,
        };
        let reply = match existing {
            Some(record_id) => {
                let body = json!({
                    "stopPlanOrderId": record_id,
                    "stopLossPrice": price,
                    "lossTrend": loss_trend,
                });
                self.rest.post_signed(PATH_STOP_CHANGE, &body).await?
            }
            None => {
                let body = json!({
                    "positionId": position_id,
                    "stopLossPrice": price,
                    "lossTrend": loss_trend,
                    // Stated, never defaulted. MEXC documents no default for
                    // either, and the wrong one on `stopLossReverse` turns a
                    // stop-out into an opposite position that carries no stop.
                    "volType": VOL_TYPE_POSITION,
                    "stopLossReverse": REVERSE_NO,
                    "takeProfitReverse": REVERSE_NO,
                });
                self.rest.post_signed(PATH_STOP_PLACE, &body).await?
            }
        };
        venue_result(&reply)?;
        Ok(())
    }

    /// The live gateway: the realm's host and the realm's credentials from the
    /// environment.
    ///
    /// MEXC has only a funded realm, so this always fails unless the owner has
    /// armed `REAL_MONEY` on the host — and it fails at the credential read,
    /// before any socket is opened.
    pub fn new(realm: MexcRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        let binding = AccountBinding::load(creds.key())?;
        let built = Self::build_bound(realm, realm.rest_base(), creds, symbols, binding);
        if built.rest.base() != realm.rest_base() {
            return Err(VenueError::BadRequest(format!(
                "realm {realm} resolved to {}, but only {} is permitted for that realm",
                built.rest.base(),
                realm.rest_base()
            )));
        }
        Ok(built)
    }

    /// Point the gateway at a local server. Tests only; the live path is
    /// [`MexcGateway::new`]. `tests/venue_fence.rs` is what stops this
    /// reaching a real venue: no host may be written outside `realm.rs`.
    pub fn for_test(
        base_url: &str,
        realm: MexcRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        Self::build(realm, base_url, creds, symbols)
    }

    pub fn realm(&self) -> MexcRealm {
        self.realm
    }

    /// The venue's own millisecond clock. Public, so this answers before the
    /// gateway has signed anything, and it is what bounds an execution-history
    /// window in the venue's time rather than this host's.
    pub async fn venue_time_ms(&self) -> Result<i64, VenueError> {
        let body: Value = self.rest.get_public_as(PATH_PING, "").await?;
        let data = venue_result(&body)?;
        match data {
            Value::Number(value) => value.as_i64(),
            Value::String(value) => value.parse().ok(),
            _ => None,
        }
        .filter(|value| *value > 0)
        .ok_or_else(|| {
            VenueError::BadReply("ping reply has no millisecond server clock".to_string())
        })
    }

    /// Every surface this credential's futures account can carry, as one
    /// scan. Reads only; nothing here can create exposure.
    async fn account_scan(&mut self) -> Result<AccountInventory, VenueError> {
        // Stamp the beginning, not the end. The caller's freshness bound then
        // rejects a scan whose first read has gone stale while later pages
        // were still arriving.
        let observed_ms = wall_ms();
        self.contracts().await?;

        let assets = {
            let body = self.rest.get_signed(PATH_ASSETS, &[]).await?;
            parse_inventory_assets(venue_result(&body)?)?
        };
        let positions = {
            let body = self.rest.get_signed(PATH_POSITIONS, &[]).await?;
            parse_inventory_positions(venue_result(&body)?, &self.contracts)?
        };
        let mut open_orders: Vec<AccountOrder> = VenueGateway::working_orders(self)
            .await?
            .into_iter()
            .map(|order| {
                let symbol = self.contracts.symbol_of(&order.symbol).ok_or_else(|| {
                    VenueError::BadReply(format!(
                        "working order names unknown contract {}",
                        order.symbol
                    ))
                })?;
                Ok(AccountOrder {
                    product: "linear".to_string(),
                    symbol: symbol.clone(),
                    client_order_id: order.client_order_id,
                })
            })
            .collect::<Result<_, VenueError>>()?;

        let mut stops_complete = false;
        for page in 1..=MAX_PAGES {
            let body = self
                .rest
                .get_signed(
                    PATH_STOP_OPEN,
                    &[
                        ("page_num", page.to_string()),
                        ("page_size", PAGE_SIZE.to_string()),
                    ],
                )
                .await?;
            let (rows, raw_count) =
                parse_inventory_stop_orders(venue_result(&body)?, &self.contracts)?;
            open_orders.extend(rows);
            if raw_count < PAGE_SIZE as usize {
                stops_complete = true;
                break;
            }
        }
        if !stops_complete {
            return Err(VenueError::BadReply(format!(
                "stop-order listing still had pages after {MAX_PAGES}"
            )));
        }

        Ok(AccountInventory {
            scope: format!(
                "credential account: MEXC futures — {SETTLE_CURRENCY} and every other wallet balance, open positions and working orders on every listed contract, and position-bound stop records. MEXC's spot and other product accounts are on a separate API this adapter does not sign for and are not covered."
            ),
            positions: positions.into_iter().chain(assets).collect(),
            open_orders,
            observed_ms,
        })
    }

    fn build(realm: MexcRealm, base_url: &str, creds: Credentials, symbols: Vec<Symbol>) -> Self {
        let binding = AccountBinding::fixture(creds.key());
        Self::build_bound(realm, base_url, creds, symbols, binding)
    }

    fn build_bound(
        realm: MexcRealm,
        base_url: &str,
        creds: Credentials,
        symbols: Vec<Symbol>,
        account_binding: AccountBinding,
    ) -> Self {
        Self {
            realm,
            account_binding,
            rest: RestClient::new(base_url, creds),
            symbols: engine_public::symbols::SymbolCatalog::from_names(symbols),
            contracts: Contracts::default(),
            lookup_catalog: Default::default(),
        }
    }

    fn name_of(&self, symbol: SymbolId) -> Result<&Symbol, VenueError> {
        self.symbols
            .names()
            .get(symbol.0 as usize)
            .ok_or_else(|| VenueError::BadRequest(format!("no symbol at id {}", symbol.0)))
    }

    /// The contract table, read once. Public market data, so no credential —
    /// which means a gateway can size an order before it has ever signed
    /// anything.
    async fn contracts(&mut self) -> Result<&Contracts, VenueError> {
        if self.contracts.is_empty() {
            let body: Box<serde_json::value::RawValue> =
                self.rest.get_public_as(PATH_CONTRACT_DETAIL, "").await?;
            self.remember_contracts(Contracts::parse_raw(body.get())?)?;
        }
        Ok(&self.contracts)
    }

    fn remember_contracts(&mut self, contracts: Contracts) -> Result<(), VenueError> {
        *self
            .lookup_catalog
            .write()
            .map_err(|_| VenueError::BadReply("MEXC lookup catalog lock poisoned".into()))? =
            contracts.clone();
        self.contracts = contracts;
        Ok(())
    }

    /// Every position the venue is holding, and its id, so a stop can be
    /// addressed. Keyed by the engine's symbol.
    async fn position_ids(
        &mut self,
        class: OperationClass,
    ) -> Result<HashMap<Symbol, String>, VenueError> {
        let body = self.rest.get_signed_for(PATH_POSITIONS, &[], class).await?;
        let data = venue_result(&body)?;
        let rows = data
            .as_array()
            .ok_or_else(|| VenueError::BadReply("open positions was not a list".into()))?
            .clone();
        let contracts = self.contracts().await?;
        let mut out = HashMap::new();
        for row in &rows {
            let Some(venue_symbol) = row.get("symbol").and_then(Value::as_str) else {
                continue;
            };
            let Some(symbol) = contracts.symbol_of(venue_symbol) else {
                continue;
            };
            let Some(id) = id_text(row, "positionId") else {
                continue;
            };
            out.insert(symbol.clone(), id);
        }
        Ok(out)
    }

    /// The live take-profit/stop-loss records, by position id. MEXC's position
    /// object carries no stop field, so this is the only honest answer to
    /// "does this position have a stop".
    async fn stop_records(&self) -> Result<Value, VenueError> {
        let body = self
            .rest
            .get_signed_for(PATH_STOP_OPEN, &[], OperationClass::Protection)
            .await?;
        Ok(venue_result(&body)?.clone())
    }

    /// Which of MEXC's four `side` values an engine side and intent mean.
    fn venue_side(side: Side, reduce_only: bool) -> i64 {
        match (side, reduce_only) {
            (Side::Buy, false) => 1,  // open long
            (Side::Buy, true) => 2,   // close short
            (Side::Sell, false) => 3, // open short
            (Side::Sell, true) => 4,  // close long
        }
    }

    /// MEXC's order `type`, which carries the time-in-force rather than
    /// leaving it to a separate field — post-only IS a type here.
    fn venue_type(kind: &OrderKind) -> i64 {
        match kind {
            OrderKind::Market => 5,
            OrderKind::Limit {
                tif: TimeInForce::PostOnly,
                ..
            } => 2,
            OrderKind::Limit {
                tif: TimeInForce::Ioc,
                ..
            } => 3,
            OrderKind::Limit {
                tif: TimeInForce::Gtc,
                ..
            } => 1,
        }
    }
}

#[engine_types::async_trait]
impl VenueGateway for MexcGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            // The venue keeps a take-profit/stop-loss record attached to the
            // position, addressable and movable after the fact. Note what is
            // NOT proven: MEXC publishes no testnet, so this adapter's stop
            // path has never run against the venue. `set_stop` states every
            // undocumented flag explicitly rather than relying on a default.
            native_position_stop: true,
            // MEXC has no amend for an ordinary order. The engine does NOT
            // fall back to cancel-and-replace when told a venue cannot amend —
            // that is a new order at the back of the queue at a fresh price —
            // so a resting quote here does not move until it is cancelled.
            amend_in_place: false,
            // POST /api/v1/private/position/change_leverage.
            set_leverage: true,
            close_position_below_minimum: false,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        let user_id = self
            .account_binding
            .identity_for(self.rest.api_key())?
            .to_owned();
        let body = self.rest.get_signed(PATH_ASSETS, &[]).await?;
        venue_result(&body)?;
        Ok(AccountIdentity {
            venue: VENUE_NAME.to_string(),
            user_id,
            realm: self.realm.as_str().to_string(),
        })
    }

    async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        self.account_scan().await
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let terms = crate::order_wire::terms(req)?;
        let name = self.name_of(req.symbol)?.clone();
        let ceiling = match req.kind {
            OrderKind::Market => Ceiling::Market,
            OrderKind::Limit { .. } => Ceiling::Limit,
        };
        let (venue_symbol, vol, loss_trend) = {
            let contracts = self.contracts().await?;
            let contract = if req.reduce_only {
                contracts.existing(&name)?
            } else {
                contracts.tradable(&name)?
            };
            let volume_unit = contract.require_order_capability()?;
            (
                contract.venue_symbol.clone(),
                match terms {
                    Some(terms) => {
                        terms
                            .validate_wire_grid(
                                &contract.exact_spec,
                                req.kind,
                                engine_types::order_terms::QuantityPolicy::Normal,
                            )
                            .map_err(crate::order_wire::error)?;
                        let max = match ceiling {
                            Ceiling::Market => contract.exact_spec.max_market_qty.as_ref(),
                            Ceiling::Limit => contract
                                .exact_spec
                                .max_qty
                                .as_ref()
                                .or(contract.exact_spec.max_market_qty.as_ref()),
                        };
                        if max.is_some_and(|max| terms.quantity > *max) {
                            return Err(crate::order_wire::error(
                                "exact quantity exceeds contract order ceiling",
                            ));
                        }
                        let vol = terms
                            .quantity
                            .checked_div(&contract.exact_contract_size.value)
                            .and_then(|vol| vol.to_u64_exact())
                            .map_err(crate::order_wire::error)?;
                        if vol % volume_unit != 0 {
                            return Err(crate::order_wire::error(
                                "exact native volume is off the volUnit grid",
                            ));
                        }
                        vol
                    }
                    None => contract.vol_for(req.qty, ceiling)?,
                },
                req.stop.map(|_| contract.stop_trend()).transpose()?,
            )
        };

        let mut body = json!({
            "symbol": venue_symbol,
            "vol": vol,
            "side": Self::venue_side(req.side, req.reduce_only),
            "type": Self::venue_type(&req.kind),
            "openType": OPEN_TYPE_CROSS,
            "positionMode": POSITION_MODE_ONE_WAY,
            "externalOid": req.client_order_id,
        });
        // The venue's field table marks `price` required, and that is wrong for
        // a market order: a client exercised against the live venue omits it
        // for the market types, and there is no price to quantize anyway.
        if let OrderKind::Limit { px, .. } = req.kind {
            body["price"] = json!(crate::order_wire::price(req, px)?);
        }
        if req.reduce_only {
            body["reduceOnly"] = json!(true);
        }
        // The entry carries its stop in the same signed call, so a fill is
        // never unprotected while a second round trip is in flight. This is an
        // order-bound stop — `set_stop` is what puts the position-level record
        // on, and its size is what tracks a position that later changes.
        if let Some(stop) = req.stop {
            body["stopLossPrice"] = json!(crate::order_wire::stop(req, stop.trigger_px)?);
            body["lossTrend"] = json!(loss_trend.ok_or_else(|| {
                VenueError::BadRequest("stop trigger capability is unavailable".into())
            })?);
        }

        let reply = self.rest.post_signed(PATH_ORDER_CREATE, &body).await?;
        let data = venue_result(&reply)?;
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id: parse_order_ack(data)?,
            sent_ns: 0,
            ack_ns: mono_ns(),
        })
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        let venue_symbol = self
            .contracts()
            .await?
            .existing(&name)?
            .venue_symbol
            .clone();
        // One order, as an object. The list form belongs to the batch
        // endpoint `/order/cancel`, which takes venue ids; this one refuses a
        // list with `600 Parameter error`.
        let body = json!({ "symbol": venue_symbol, "externalOid": client_order_id });
        let reply = self
            .rest
            .post_signed(PATH_ORDER_CANCEL_EXTERNAL, &body)
            .await?;
        let data = venue_result(&reply)?;
        // The envelope says the request parsed; the order's own result is
        // inside it, and a non-zero code there is the refusal.
        let error_code = data
            .get("errorCode")
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                VenueError::BadReply("MEXC cancellation has no integer errorCode".into())
            })?;
        if data
            .get("externalOid")
            .is_some_and(|id| id.as_str() != Some(client_order_id))
        {
            return Err(VenueError::BadReply(
                "MEXC cancellation reply names another order or has an invalid externalOid".into(),
            ));
        }
        if error_code != 0 {
            return Err(VenueError::Rejected {
                code: error_code,
                message: data
                    .get("errorMsg")
                    .and_then(Value::as_str)
                    .unwrap_or("(no errorMsg)")
                    .to_string(),
            });
        }
        Ok(())
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        _client_order_id: &str,
        _spec: AmendSpec,
    ) -> Result<(), VenueError> {
        // Declared false in `caps`, so the engine does not call this. Refusing
        // rather than silently cancelling and replacing: that is a new order at
        // the back of the queue at a fresh price, which is a different trade
        // from the one asked for.
        Err(VenueError::BadRequest(
            "MEXC has no amend for an ordinary order, and this adapter says so in its caps"
                .to_string(),
        ))
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        self.set_stop_terms(symbol, trigger_px, None).await
    }

    async fn set_stop_exact(
        &mut self,
        symbol: SymbolId,
        terms: &engine_types::order_terms::ExactStopTerms,
    ) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        terms
            .validate_wire_grid(&self.contracts().await?.existing(&name)?.exact_spec)
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

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        // Reject before any network I/O; the core must never cache a value
        // different from the integer sent to the venue.
        if !leverage.is_finite()
            || leverage < 1.0
            || leverage.fract() != 0.0
            || leverage >= i64::MAX as f64
        {
            return Err(VenueError::BadRequest(format!(
                "MEXC leverage must be a positive, representable integer; received {leverage}"
            )));
        }
        let name = self.name_of(symbol)?.clone();
        let (venue_symbol, min_leverage, max_leverage) = {
            let contract = self.contracts().await?.tradable(&name)?;
            let bounds = contract
                .execution
                .min_leverage
                .zip(contract.execution.max_leverage)
                .filter(|(min, max)| {
                    min.is_finite()
                        && max.is_finite()
                        && *min >= 1.0
                        && *max >= *min
                        && min.fract() == 0.0
                        && max.fract() == 0.0
                })
                .ok_or_else(|| {
                    VenueError::BadRequest(format!(
                        "{} has no valid leverage bounds",
                        contract.venue_symbol
                    ))
                })?;
            (contract.venue_symbol.clone(), bounds.0, bounds.1)
        };
        if leverage < min_leverage || leverage > max_leverage {
            return Err(VenueError::BadRequest(format!(
                "{venue_symbol} permits leverage {min_leverage}..={max_leverage}, and this asks for {leverage}"
            )));
        }
        let want = leverage as i64;
        let held = self
            .position_ids(OperationClass::Administration)
            .await?
            .get(&name)
            .cloned();
        match held {
            // With a position open the call names it, and one call is the
            // whole job.
            Some(position_id) => {
                let body = json!({"positionId": position_id, "leverage": want});
                let reply = self.rest.post_signed(PATH_LEVERAGE, &body).await?;
                venue_result(&reply)?;
            }
            // With none, MEXC wants the side named — and the trait's contract
            // is "both sides", because which way the next order goes is not
            // known here. Two calls, and both must land.
            None => {
                for position_type in [1, 2] {
                    let body = json!({
                        "symbol": venue_symbol,
                        "leverage": want,
                        "openType": OPEN_TYPE_CROSS,
                        "positionType": position_type,
                    });
                    let reply = self.rest.post_signed(PATH_LEVERAGE, &body).await?;
                    venue_result(&reply)?;
                }
            }
        }
        Ok(())
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        self.symbols.intern(symbol)
    }

    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        Some(self.rest.take_mutation_wait_ns())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        self.contracts().await?;
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
        let version = checkpoint_version(checkpoint)?;
        let pages =
            crate::catalog_checkpoint::decode(checkpoint, catalog_kind(version), self.rest.base())?;
        let catalog = catalog_from_pages_version(self.rest.base(), pages, version)?;
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
        if snapshot.base != self.rest.base() {
            return Err(VenueError::BadRequest(
                "catalog belongs to another venue endpoint".into(),
            ));
        }
        self.remember_contracts(snapshot.data.clone())
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
            rest: self.rest.clone(),
            catalog: self.lookup_catalog.clone(),
        }))
    }

    fn order_lookup_client(&self) -> Option<Box<dyn engine_types::orders::OrderLookupClient>> {
        Some(Box::new(LookupClient {
            rest: self.rest.clone(),
            catalog: self.lookup_catalog.clone(),
        }))
    }

    async fn order_status(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        let name = self.name_of(symbol)?.clone();
        let contract = self
            .contracts()
            .await?
            .any(&name)
            .ok_or_else(|| {
                VenueError::BadRequest("order lookup symbol has no contract metadata".into())
            })?
            .clone();
        let path = format!(
            "/api/v1/private/order/external/{}/{}",
            crate::http::percent_encode(&contract.venue_symbol),
            crate::http::percent_encode(client_order_id)
        );
        let raw: Box<serde_json::value::RawValue> = self.rest.get_signed_as(&path, &[]).await?;
        super::lookup::parse(raw.get(), &name, client_order_id, &contract)
    }

    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(Symbol, engine_types::numeric::ExactInstrumentSpec)>, VenueError> {
        let body: Box<serde_json::value::RawValue> =
            self.rest.get_public_as(PATH_CONTRACT_DETAIL, "").await?;
        self.remember_contracts(Contracts::parse_raw(body.get())?)?;
        Ok(self.contracts.instrument_specs())
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        // Re-read rather than serve the cache: this is the call the engine
        // makes to learn the venue's current rules, and a contract's size or
        // tick can change under it.
        let body: Box<serde_json::value::RawValue> =
            self.rest.get_public_as(PATH_CONTRACT_DETAIL, "").await?;
        self.remember_contracts(Contracts::parse_raw(body.get())?)?;
        Ok(self.contracts.rules())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        // The endpoint has no symbol filter: the whole account is paged.
        let mut out = Vec::new();
        for page in 1..=MAX_PAGES {
            let body = self
                .rest
                .get_signed(
                    PATH_OPEN_ORDERS,
                    &[
                        ("page_num", page.to_string()),
                        ("page_size", PAGE_SIZE.to_string()),
                    ],
                )
                .await?;
            let data = venue_result(&body)?.clone();
            let contracts = self.contracts().await?;
            let (rows, raw_count) = parse_open_orders(&data, contracts)?;
            out.extend(rows);
            if raw_count < PAGE_SIZE as usize {
                return Ok(out);
            }
        }
        Err(VenueError::BadReply(format!(
            "working-order listing still had pages after {MAX_PAGES}"
        )))
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        self.contracts().await?;
        engine_types::orders::AccountRecoveryClient::executions(
            &recovery::RecoveryClient::new(self),
            self.symbols.names(),
            start_ms,
            end_ms,
        )
        .await
    }
}

/// A deliberately narrow live capability for deployment attestation.
///
/// Its gateway is private and this type exposes no order, cancel, amend,
/// leverage, stop, or websocket API. That is why it may authenticate a
/// disarmed funded account: proving old exposure absent is not authority to
/// create new exposure. MEXC has one credential pair, so the same key that
/// trades is the key that reads — the narrowing is in this type's surface, not
/// in a second key.
pub struct MexcInventoryProbe {
    gateway: MexcGateway,
}

impl MexcInventoryProbe {
    pub fn new(realm: MexcRealm) -> Result<Self, VenueError> {
        let (key_var, secret_var) = realm.credential_vars();
        let credentials = Credentials::from_env_read_only(
            realm.as_str(),
            realm.is_real_money(),
            key_var,
            secret_var,
        )?;
        let binding = AccountBinding::load(credentials.key())?;
        Ok(Self {
            gateway: MexcGateway::build_bound(
                realm,
                realm.rest_base(),
                credentials,
                Vec::new(),
                binding,
            ),
        })
    }

    /// Point the probe at a local server. Tests only; the live path is
    /// [`MexcInventoryProbe::new`].
    pub fn for_test(base_url: &str, realm: MexcRealm, creds: Credentials) -> Self {
        Self {
            gateway: MexcGateway::build(realm, base_url, creds, Vec::new()),
        }
    }

    pub async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        VenueGateway::account_identity(&mut self.gateway).await
    }

    pub async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        self.gateway.account_scan().await
    }
}

#[derive(Debug)]
struct CatalogSnapshot {
    base: String,
    pages: Vec<String>,
    data: Contracts,
    metadata_version: u8,
}

impl engine_types::orders::InstrumentCatalogCache for CatalogSnapshot {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
    fn checkpoint(
        &self,
    ) -> Result<engine_types::orders::InstrumentCatalogCacheSnapshot, VenueError> {
        crate::catalog_checkpoint::encode(
            catalog_kind(self.metadata_version),
            &self.base,
            &self.pages,
        )
    }
    fn retain_previous(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        let version = checkpoint_version(checkpoint)?;
        let previous =
            crate::catalog_checkpoint::decode(checkpoint, catalog_kind(version), &self.base)?;
        crate::catalog_checkpoint::check(
            checkpoint,
            catalog_from_pages_version(&self.base, previous.clone(), version)?,
        )?;
        let pages = crate::catalog_checkpoint::merge_pages("mexc", previous, self.pages.clone())?;
        let catalog = catalog_from_pages_version(&self.base, pages, self.metadata_version)?;
        catalog.checkpoint()?.validate_bounds()?;
        Ok(catalog)
    }
}

#[engine_types::async_trait]
impl engine_types::orders::InstrumentCatalogClient for LookupClient {
    async fn fetch(&self) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        let raw: Box<serde_json::value::RawValue> =
            self.rest.get_public_as(PATH_CONTRACT_DETAIL, "").await?;
        catalog_from_pages(self.rest.base(), vec![raw.get().to_owned()])
    }
}

struct LookupClient {
    rest: RestClient,
    catalog: std::sync::Arc<std::sync::RwLock<Contracts>>,
}
#[engine_types::async_trait]
impl engine_types::orders::OrderLookupClient for LookupClient {
    async fn lookup(
        &self,
        name: &str,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        let retained = self
            .catalog
            .read()
            .map_err(|_| VenueError::BadReply("MEXC lookup catalog lock poisoned".into()))?
            .any(name)
            .cloned();
        let contract = match retained {
            Some(contract) => contract,
            None => {
                let raw: Box<serde_json::value::RawValue> =
                    self.rest.get_public_as(PATH_CONTRACT_DETAIL, "").await?;
                Contracts::parse_raw(raw.get())?
                    .any(name)
                    .cloned()
                    .ok_or_else(|| {
                        VenueError::BadRequest(
                            "order lookup symbol has no retained contract metadata".into(),
                        )
                    })?
            }
        };
        let path = format!(
            "/api/v1/private/order/external/{}/{}",
            crate::http::percent_encode(&contract.venue_symbol),
            crate::http::percent_encode(client_order_id)
        );
        let raw: Box<serde_json::value::RawValue> = self.rest.get_signed_as(&path, &[]).await?;
        super::lookup::parse(raw.get(), name, client_order_id, &contract)
    }
}

fn catalog_from_pages(
    base: &str,
    pages: Vec<String>,
) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
    catalog_from_pages_version(base, pages, 2)
}

fn catalog_kind(version: u8) -> &'static str {
    if version == 1 {
        "mexc"
    } else {
        CATALOG_KIND_V2
    }
}

fn checkpoint_version(
    checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
) -> Result<u8, VenueError> {
    match checkpoint.cache.kind.as_str() {
        "mexc" => Ok(1),
        CATALOG_KIND_V2 => Ok(2),
        _ => Err(VenueError::BadReply(
            "unsupported MEXC catalog checkpoint kind".into(),
        )),
    }
}

fn catalog_from_pages_version(
    base: &str,
    pages: Vec<String>,
    version: u8,
) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
    if pages.len() != 1 {
        return Err(VenueError::BadReply(
            "catalog requires one metadata page".into(),
        ));
    }
    let raw = pages[0].as_str();
    let catalog = if version == 1 {
        Contracts::parse_checkpoint_v1(raw)?
    } else {
        Contracts::parse_raw(raw)?
    };
    Ok(engine_types::orders::InstrumentCatalog {
        rules: catalog.rules(),
        specs: catalog.instrument_specs(),
        cache: Some(std::sync::Arc::new(CatalogSnapshot {
            pages,
            base: base.to_owned(),
            data: catalog,
            metadata_version: version,
        })),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_four_way_side_carries_both_direction_and_intent() {
        // MEXC has no buy/sell field: 1 and 3 open, 2 and 4 close, and getting
        // this backwards would open a position where an exit was asked for.
        assert_eq!(MexcGateway::venue_side(Side::Buy, false), 1);
        assert_eq!(MexcGateway::venue_side(Side::Buy, true), 2);
        assert_eq!(MexcGateway::venue_side(Side::Sell, false), 3);
        assert_eq!(MexcGateway::venue_side(Side::Sell, true), 4);
    }

    #[test]
    fn execution_pagination_needs_a_raw_short_page() {
        assert!(execution_page_complete("BTCUSDT", 1, 99).unwrap());
        assert!(!execution_page_complete("BTCUSDT", 1, 100).unwrap());
        assert!(!execution_page_complete("BTCUSDT", MAX_PAGES * 100, 100).unwrap());
        assert!(execution_page_complete("BTCUSDT", 1, 101).is_err());
    }

    #[test]
    fn post_only_is_an_order_type_here_not_a_time_in_force() {
        assert_eq!(MexcGateway::venue_type(&OrderKind::Market), 5);
        assert_eq!(
            MexcGateway::venue_type(&OrderKind::Limit {
                px: 1.0,
                tif: TimeInForce::Gtc
            }),
            1
        );
        assert_eq!(
            MexcGateway::venue_type(&OrderKind::Limit {
                px: 1.0,
                tif: TimeInForce::PostOnly
            }),
            2
        );
        assert_eq!(
            MexcGateway::venue_type(&OrderKind::Limit {
                px: 1.0,
                tif: TimeInForce::Ioc
            }),
            3
        );
    }

    #[test]
    fn the_caps_say_what_this_adapter_can_actually_do() {
        let creds = MexcRealm::Mainnet.credentials_for_test("k", "s");
        let gw = MexcGateway::for_test("http://127.0.0.1:1", MexcRealm::Mainnet, creds, vec![]);
        let caps = gw.caps();
        assert!(caps.native_position_stop);
        assert!(
            !caps.amend_in_place,
            "MEXC has no amend for an ordinary order"
        );
        assert!(caps.set_leverage);
    }

    #[test]
    fn the_undocumented_stop_flags_are_stated_rather_than_defaulted() {
        // The values, not the call. MEXC documents no default for any of the
        // three, and the wrong `stopLossReverse` turns a stop-out into an
        // opposite position that carries no stop of its own.
        assert_eq!(REVERSE_NO, 2, "2 is 'no' — 1 would reverse");
        assert_eq!(
            VOL_TYPE_POSITION, 2,
            "2 tracks the position; 1 is a fixed size"
        );
        assert_eq!(
            POSITION_MODE_ONE_WAY, 2,
            "hedge mode holds two positions per symbol"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn an_amend_is_refused_rather_than_turned_into_a_replacement() {
        let creds = MexcRealm::Mainnet.credentials_for_test("k", "s");
        let mut gw = MexcGateway::for_test(
            "http://127.0.0.1:1",
            MexcRealm::Mainnet,
            creds,
            vec!["BTCUSDT".into()],
        );
        let err = gw
            .amend_order(
                SymbolId(0),
                "eng-1",
                AmendSpec {
                    exact_terms: None,
                    px: Some(1.0),
                    qty: None,
                },
            )
            .await
            .unwrap_err();
        assert!(err.to_string().contains("no amend"), "{err}");
    }

    #[test]
    fn a_symbol_taken_on_later_gets_the_next_id_in_order() {
        // Every table that maps names to ids has to grow in the same order:
        // a SymbolId is an index assigned by position.
        let creds = MexcRealm::Mainnet.credentials_for_test("k", "s");
        let mut gw = MexcGateway::for_test(
            "http://127.0.0.1:1",
            MexcRealm::Mainnet,
            creds,
            vec!["BTCUSDT".to_string()],
        );
        assert_eq!(
            VenueGateway::add_symbol(&mut gw, "BTCUSDT"),
            Some(SymbolId(0))
        );
        assert_eq!(
            VenueGateway::add_symbol(&mut gw, "ETHUSDT"),
            Some(SymbolId(1))
        );
        assert_eq!(
            VenueGateway::add_symbol(&mut gw, "ETHUSDT"),
            Some(SymbolId(1))
        );
    }
}
