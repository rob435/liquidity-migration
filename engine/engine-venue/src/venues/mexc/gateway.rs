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

use std::collections::HashMap;

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest, Side, TimeInForce,
    VenueExecution, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::contracts::{Ceiling, Contracts};
use super::parse::{
    id_text, parse_assets, parse_deals, parse_open_orders, parse_order_ack, parse_position_stops,
    parse_positions, venue_result,
};
use super::realm::MexcRealm;
use super::rest::RestClient;
use super::VENUE_NAME;
use crate::creds::Credentials;
use crate::fmt::venue_num;
use crate::mono_ns;

const PATH_CONTRACT_DETAIL: &str = "/api/v1/contract/detail";
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

/// Trigger on the last traded price.
const TREND_LAST_PRICE: i64 = 1;

/// The engine's own pages of a venue list. Enough to cover any account this
/// engine runs; a cursor that never empties is a venue fault, not a reason to
/// loop forever.
const MAX_PAGES: u32 = 20;
const PAGE_SIZE: u32 = 100;

pub struct MexcGateway {
    realm: MexcRealm,
    rest: RestClient,
    names: Vec<Symbol>,
    ids: HashMap<Symbol, SymbolId>,
    /// The venue's contract table, read once and kept. Every size that crosses
    /// this boundary needs it, so an adapter without it can convert nothing.
    contracts: Contracts,
}

impl MexcGateway {
    /// The live gateway: the realm's host and the realm's credentials from the
    /// environment.
    ///
    /// MEXC has only a funded realm, so this always fails unless the owner has
    /// armed `REAL_MONEY` on the host — and it fails at the credential read,
    /// before any socket is opened.
    pub fn new(realm: MexcRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        let built = Self::build(realm, realm.rest_base(), creds, symbols);
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

    fn build(
        realm: MexcRealm,
        base_url: &str,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Self {
            realm,
            rest: RestClient::new(base_url, creds),
            names: symbols,
            ids,
            contracts: Contracts::default(),
        }
    }

    fn name_of(&self, symbol: SymbolId) -> Result<&Symbol, VenueError> {
        self.names
            .get(symbol.0 as usize)
            .ok_or_else(|| VenueError::BadRequest(format!("no symbol at id {}", symbol.0)))
    }

    /// The contract table, read once. Public market data, so no credential —
    /// which means a gateway can size an order before it has ever signed
    /// anything.
    async fn contracts(&mut self) -> Result<&Contracts, VenueError> {
        if self.contracts.is_empty() {
            let body = self.rest.get_public(PATH_CONTRACT_DETAIL, "").await?;
            self.contracts = Contracts::parse(&body)?;
        }
        Ok(&self.contracts)
    }

    /// Every position the venue is holding, and its id, so a stop can be
    /// addressed. Keyed by the engine's symbol.
    async fn position_ids(&mut self) -> Result<HashMap<Symbol, String>, VenueError> {
        let body = self.rest.get_signed(PATH_POSITIONS, &[]).await?;
        let data = venue_result(&body)?;
        let rows = data
            .as_array()
            .ok_or_else(|| VenueError::BadReply("open positions was not a list".into()))?
            .clone();
        let contracts = self.contracts().await?;
        let mut out = HashMap::new();
        for row in &rows {
            let Some(venue_symbol) = row.get("symbol").and_then(Value::as_str) else { continue };
            let Some(symbol) = contracts.symbol_of(venue_symbol) else { continue };
            let Some(id) = id_text(row, "positionId") else { continue };
            out.insert(symbol.clone(), id);
        }
        Ok(out)
    }

    /// The live take-profit/stop-loss records, by position id. MEXC's position
    /// object carries no stop field, so this is the only honest answer to
    /// "does this position have a stop".
    async fn stop_records(&self) -> Result<Value, VenueError> {
        let body = self.rest.get_signed(PATH_STOP_OPEN, &[]).await?;
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
            OrderKind::Limit { tif: TimeInForce::PostOnly, .. } => 2,
            OrderKind::Limit { tif: TimeInForce::Ioc, .. } => 3,
            OrderKind::Limit { tif: TimeInForce::Gtc, .. } => 1,
        }
    }
}

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
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        // MEXC publishes no account number: no endpoint returns one, and
        // neither positions nor orders carry a uid. What names the account is
        // the key that opens it — but the key itself is not written to a lock
        // file on disk, so this is a digest of it. Two engines holding the
        // same key reach the same name, which is the whole job.
        let body = self.rest.get_signed(PATH_ASSETS, &[]).await?;
        venue_result(&body)?;
        let digest = Sha256::digest(self.rest.api_key().as_bytes());
        Ok(AccountIdentity {
            venue: VENUE_NAME.to_string(),
            user_id: format!("key-{}", hex::encode(&digest[..8])),
            realm: self.realm.as_str().to_string(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let name = self.name_of(req.symbol)?.clone();
        let ceiling = match req.kind {
            OrderKind::Market => Ceiling::Market,
            OrderKind::Limit { .. } => Ceiling::Limit,
        };
        let (venue_symbol, vol) = {
            let contract = self.contracts().await?.tradable(&name)?;
            (contract.venue_symbol.clone(), contract.vol_for(req.qty, ceiling)?)
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
            body["price"] = json!(venue_num(px)?);
        }
        if req.reduce_only {
            body["reduceOnly"] = json!(true);
        }
        // The entry carries its stop in the same signed call, so a fill is
        // never unprotected while a second round trip is in flight. This is an
        // order-bound stop — `set_stop` is what puts the position-level record
        // on, and its size is what tracks a position that later changes.
        if let Some(stop) = req.stop {
            body["stopLossPrice"] = json!(venue_num(stop.trigger_px)?);
            body["lossTrend"] = json!(TREND_LAST_PRICE);
        }

        let reply = self.rest.post_signed(PATH_ORDER_CREATE, &body).await?;
        let data = venue_result(&reply)?;
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id: parse_order_ack(data)?,
            ack_ns: mono_ns(),
        })
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        let venue_symbol = self.contracts().await?.tradable(&name)?.venue_symbol.clone();
        // The endpoint takes a list even for one order.
        let body = json!([{ "symbol": venue_symbol, "externalOid": client_order_id }]);
        let reply = self.rest.post_signed(PATH_ORDER_CANCEL_EXTERNAL, &body).await?;
        venue_result(&reply)?;
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
        let name = self.name_of(symbol)?.clone();
        // Proves the symbol is one this venue will take API orders on before
        // anything is sent about it.
        self.contracts().await?.tradable(&name)?;
        let position_id = self
            .position_ids()
            .await?
            .get(&name)
            .cloned()
            .ok_or_else(|| {
                VenueError::BadRequest(format!(
                    "MEXC holds no position on {name}, and a stop here is attached to one"
                ))
            })?;

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

        let price = venue_num(trigger_px)?;
        let reply = match existing {
            Some(record_id) => {
                let body = json!({
                    "stopPlanOrderId": record_id,
                    "stopLossPrice": price,
                    "lossTrend": TREND_LAST_PRICE,
                });
                self.rest.post_signed(PATH_STOP_CHANGE, &body).await?
            }
            None => {
                let body = json!({
                    "positionId": position_id,
                    "stopLossPrice": price,
                    "lossTrend": TREND_LAST_PRICE,
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

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        let (venue_symbol, max_leverage) = {
            let contract = self.contracts().await?.tradable(&name)?;
            (contract.venue_symbol.clone(), contract.max_leverage)
        };
        if !leverage.is_finite() || leverage < 1.0 {
            return Err(VenueError::BadRequest(format!(
                "{leverage} is not a leverage MEXC will take"
            )));
        }
        if leverage > max_leverage {
            return Err(VenueError::BadRequest(format!(
                "{venue_symbol} caps leverage at {max_leverage}, and this asks for {leverage}"
            )));
        }
        let want = leverage.round() as i64;
        let held = self.position_ids().await?.get(&name).cloned();
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
        if let Some(id) = self.ids.get(symbol) {
            return Some(*id);
        }
        let id = SymbolId(u16::try_from(self.names.len()).ok()?);
        self.names.push(symbol.to_string());
        self.ids.insert(symbol.to_string(), id);
        Some(id)
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        let assets = self.rest.get_signed(PATH_ASSETS, &[]).await?;
        let (equity_usdt, available_usdt) = parse_assets(venue_result(&assets)?)?;
        let positions_body = self.rest.get_signed(PATH_POSITIONS, &[]).await?;
        let positions_data = venue_result(&positions_body)?.clone();
        // The position rows say nothing about stops, so the stop book is read
        // alongside and joined in. Without it every position would report
        // itself unprotected, and the engine would act on that.
        let stops = parse_position_stops(&self.stop_records().await?);
        let ids = self.ids.clone();
        let contracts = self.contracts().await?;
        let positions = parse_positions(&positions_data, contracts, &ids, &stops)?;
        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions,
            observed_ns: mono_ns(),
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        // Re-read rather than serve the cache: this is the call the engine
        // makes to learn the venue's current rules, and a contract's size or
        // tick can change under it.
        let body = self.rest.get_public(PATH_CONTRACT_DETAIL, "").await?;
        self.contracts = Contracts::parse(&body)?;
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
            let rows = parse_open_orders(&data, contracts)?;
            let short_page = rows.len() < PAGE_SIZE as usize;
            out.extend(rows);
            if short_page {
                break;
            }
        }
        Ok(out)
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        // `symbol` is required here, so the sweep is per symbol rather than
        // account-wide. The engine asks about the symbols it follows.
        let names = self.names.clone();
        let mut out = Vec::new();
        for name in names {
            let venue_symbol = match self.contracts().await?.any(&name) {
                Some(contract) => contract.venue_symbol.clone(),
                None => continue,
            };
            for page in 1..=MAX_PAGES {
                let body = self
                    .rest
                    .get_signed(
                        PATH_DEALS,
                        &[
                            ("symbol", venue_symbol.clone()),
                            ("start_time", start_ms.to_string()),
                            ("end_time", end_ms.to_string()),
                            ("page_num", page.to_string()),
                            ("page_size", PAGE_SIZE.to_string()),
                        ],
                    )
                    .await?;
                let data = venue_result(&body)?.clone();
                let contracts = self.contracts().await?;
                let rows = parse_deals(&data, contracts)?;
                let short_page = rows.len() < PAGE_SIZE as usize;
                out.extend(rows);
                if short_page {
                    break;
                }
            }
        }
        Ok(out)
    }
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
    fn post_only_is_an_order_type_here_not_a_time_in_force() {
        assert_eq!(MexcGateway::venue_type(&OrderKind::Market), 5);
        assert_eq!(
            MexcGateway::venue_type(&OrderKind::Limit { px: 1.0, tif: TimeInForce::Gtc }),
            1
        );
        assert_eq!(
            MexcGateway::venue_type(&OrderKind::Limit { px: 1.0, tif: TimeInForce::PostOnly }),
            2
        );
        assert_eq!(
            MexcGateway::venue_type(&OrderKind::Limit { px: 1.0, tif: TimeInForce::Ioc }),
            3
        );
    }

    #[test]
    fn the_caps_say_what_this_adapter_can_actually_do() {
        let creds = MexcRealm::Mainnet.credentials_for_test("k", "s");
        let gw = MexcGateway::for_test("http://127.0.0.1:1", MexcRealm::Mainnet, creds, vec![]);
        let caps = gw.caps();
        assert!(caps.native_position_stop);
        assert!(!caps.amend_in_place, "MEXC has no amend for an ordinary order");
        assert!(caps.set_leverage);
    }

    #[test]
    fn the_undocumented_stop_flags_are_stated_rather_than_defaulted() {
        // The values, not the call. MEXC documents no default for any of the
        // three, and the wrong `stopLossReverse` turns a stop-out into an
        // opposite position that carries no stop of its own.
        assert_eq!(REVERSE_NO, 2, "2 is 'no' — 1 would reverse");
        assert_eq!(VOL_TYPE_POSITION, 2, "2 tracks the position; 1 is a fixed size");
        assert_eq!(POSITION_MODE_ONE_WAY, 2, "hedge mode holds two positions per symbol");
    }

    #[tokio::test]
    async fn an_amend_is_refused_rather_than_turned_into_a_replacement() {
        let creds = MexcRealm::Mainnet.credentials_for_test("k", "s");
        let mut gw =
            MexcGateway::for_test("http://127.0.0.1:1", MexcRealm::Mainnet, creds, vec!["BTCUSDT".into()]);
        let err = gw
            .amend_order(SymbolId(0), "eng-1", AmendSpec { px: Some(1.0), qty: None })
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
        assert_eq!(VenueGateway::add_symbol(&mut gw, "BTCUSDT"), Some(SymbolId(0)));
        assert_eq!(VenueGateway::add_symbol(&mut gw, "ETHUSDT"), Some(SymbolId(1)));
        assert_eq!(VenueGateway::add_symbol(&mut gw, "ETHUSDT"), Some(SymbolId(1)));
    }
}
