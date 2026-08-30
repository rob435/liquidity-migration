//! The Binance gateway: the [`VenueGateway`] contract over USDT-margined
//! perpetual futures.
//!
//! One adapter, two realms, and the host is derived from the realm rather
//! than passed alongside it, so the account being addressed and the account
//! being signed for are one decision.
//!
//! **The stop is a separate algo order the venue keeps.** There is no stop
//! field on the position: protection is a `STOP_MARKET` algo order with
//! `closePosition=true`, triggered on the mark price and sized by the
//! position however it grows. Binance does not expose an atomic ordinary
//! entry plus algo-stop request. This adapter places the entry, then the stop;
//! if the stop is refused it immediately asks to cancel the entry and reports
//! an unknown outcome so reconciliation, rather than a guessed rejection,
//! decides what exists. [`BinanceGateway::set_stop`] places the replacement
//! before it pulls the old.
//!
//! **The account must use one-way, single-asset mode.** Reduce-only — which
//! every exit here is — is refused in hedge mode. Multi-assets account totals
//! are USD-valued across eligible collateral, while the engine's account
//! contract is literal USDT. Identity proves both settings before startup.
//!
//! **Execution recovery is unavailable.** Account trades require a symbol,
//! while the account-wide order index and individual order lookup can forget
//! an ordinary GTC order based on when it was created, even if it fills in the
//! requested trade interval. The gateway therefore refuses execution-history
//! reads instead of returning a list that may omit an account fill.
//!
//! **A partial limit fill can be smaller than the market-order minimum.** The
//! public filters and submitted-size checks cannot prevent that dust. Until a
//! signed account proves a supported dust-close path, both realms remain
//! production-blocked.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AmendSpec, InstrumentRule, OrderAck, OrderKind, OrderRequest, Side, TimeInForce,
    VenueExecution, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::parse::{
    native_stop_algo_ids, parse_account, parse_account_alias, parse_algo_ack, parse_exchange_info,
    parse_multi_assets_mode, parse_order_ack, parse_position_sides, parse_position_stops,
    parse_working_algo_orders, parse_working_orders, MarketQtyRule, STOP_ID_PREFIX,
};
use super::realm::BinanceRealm;
use super::rest::RestClient;
use super::VENUE_NAME;
use crate::creds::Credentials;
use crate::fmt::venue_num;
use crate::{account_scan, mono_ns, wall_ms};

const PATH_ORDER: &str = "/fapi/v1/order";
const PATH_OPEN_ORDERS: &str = "/fapi/v1/openOrders";
const PATH_ALGO_ORDER: &str = "/fapi/v1/algoOrder";
const PATH_OPEN_ALGO_ORDERS: &str = "/fapi/v1/openAlgoOrders";
const PATH_ACCOUNT: &str = "/fapi/v2/account";
const PATH_BALANCE: &str = "/fapi/v3/balance";
const PATH_EXCHANGE_INFO: &str = "/fapi/v1/exchangeInfo";
const PATH_LEVERAGE: &str = "/fapi/v1/leverage";
const PATH_MULTI_ASSETS_MODE: &str = "/fapi/v1/multiAssetsMargin";
const PATH_POSITION_MODE: &str = "/fapi/v1/positionSide/dual";

/// The venue budgets requests two ways and both are paced here. Orders spend
/// an order count — 300 per rolling ten seconds, and nothing on request
/// weight — while reads spend weight against the per-minute IP budget. The
/// mainnet figure is 2400 and the testnet's is larger; pacing both realms to
/// the smaller is one number that is honest on either.
const REQUEST_WEIGHT_PER_MINUTE: u32 = 2400;
const ORDERS_PER_TEN_SECONDS: u32 = 300;
const ORDERS_PER_MINUTE: u32 = 1200;
const WEIGHT_WINDOW: Duration = Duration::from_secs(60);
const ORDER_WINDOW: Duration = Duration::from_secs(10);
const ORDER_MINUTE_WINDOW: Duration = Duration::from_secs(60);
/// Keeps a local/venue clock boundary from turning an exactly-on-time
/// admission into the venue's 429.
const RATE_LIMIT_GUARD: Duration = Duration::from_millis(1);

// Documented request weights, spent against the per-minute budget. The
// account-wide open-order read is the conservative figure for the symbol-less
// form; the current docs page for that endpoint would not load to re-pin it.
const WEIGHT_ACCOUNT: u32 = 5;
const WEIGHT_BALANCE: u32 = 5;
const WEIGHT_OPEN_ORDERS_ALL: u32 = 40;
const WEIGHT_OPEN_ORDERS_SYMBOL: u32 = 1;
const WEIGHT_QUERY_ORDER: u32 = 1;
const WEIGHT_CANCEL: u32 = 1;
const WEIGHT_LEVERAGE: u32 = 1;
const WEIGHT_MULTI_ASSETS_MODE: u32 = 30;
const WEIGHT_POSITION_MODE: u32 = 30;
const WEIGHT_EXCHANGE_INFO: u32 = 1;

fn algo_order_is_already_gone(error: &VenueError) -> bool {
    matches!(
        error,
        VenueError::Rejected { code: -2011, message }
            if message.trim().eq_ignore_ascii_case("Unknown order sent.")
    )
}

/// A rolling spend window: the venue's own accounting, kept locally so a
/// request is held back here — measured, and reported through
/// [`VenueGateway::take_rate_wait_ns`] — instead of refused there.
struct RollingBudget {
    window: Duration,
    admissions: VecDeque<(Instant, u32)>,
    spent: u32,
}

impl RollingBudget {
    fn new(window: Duration) -> Self {
        Self {
            window,
            admissions: VecDeque::new(),
            spent: 0,
        }
    }

    /// Reserve `cost` against the window. `Err(wait)` means nothing was
    /// consumed and the caller must retry after the named delay.
    fn try_reserve(&mut self, now: Instant, cost: u32, capacity: u32) -> Result<(), Duration> {
        while self
            .admissions
            .front()
            .is_some_and(|(at, _)| now.duration_since(*at) >= self.window + RATE_LIMIT_GUARD)
        {
            let (_, expired) = self.admissions.pop_front().expect("front just checked");
            self.spent -= expired;
        }
        if self.spent + cost <= capacity {
            self.admissions.push_back((now, cost));
            self.spent += cost;
            return Ok(());
        }
        let mut must_free = self.spent + cost - capacity;
        for (at, held) in &self.admissions {
            if *held >= must_free {
                return Err((*at + self.window + RATE_LIMIT_GUARD).saturating_duration_since(now));
            }
            must_free -= held;
        }
        // The window is empty and the cost alone exceeds capacity: a caller
        // asking for more than the venue ever grants would spin forever.
        Err(Duration::ZERO)
    }

    /// Venue arrival happens after local admission. Re-anchor what just
    /// completed to the conservative side of that uncertainty, so a fast
    /// follower cannot cross the venue's rolling window when the preceding
    /// network leg was slow.
    fn anchor_completion(&mut self, completed: Instant, cost: u32) {
        let mut remaining = cost;
        for (at, held) in self.admissions.iter_mut().rev() {
            if remaining == 0 {
                break;
            }
            *at = completed;
            remaining = remaining.saturating_sub(*held);
        }
    }
}

/// Returns how long admission was held back: nanoseconds of our own pacing,
/// which the order path measures apart from the venue's own leg.
async fn reserve(budget: &mut RollingBudget, cost: u32, capacity: u32) -> u64 {
    debug_assert!(cost <= capacity);
    let began = Instant::now();
    loop {
        match budget.try_reserve(Instant::now(), cost, capacity) {
            Ok(()) => return began.elapsed().as_nanos() as u64,
            Err(wait) if wait.is_zero() => return began.elapsed().as_nanos() as u64,
            Err(wait) => tokio::time::sleep(wait).await,
        }
    }
}

pub struct BinanceGateway {
    realm: BinanceRealm,
    rest: RestClient,
    names: Vec<Symbol>,
    ids: HashMap<Symbol, SymbolId>,
    market_qty_rules: HashMap<Symbol, MarketQtyRule>,
    weight_budget: RollingBudget,
    order_ten_second_budget: RollingBudget,
    order_minute_budget: RollingBudget,
    /// Makes a stop's client id unique when two are minted in one
    /// millisecond. The id must fit the venue's 36-character bound, so it
    /// cannot afford to carry the symbol.
    stop_seq: u32,
    last_rate_wait_ns: Option<u64>,
}

impl BinanceGateway {
    /// The live gateway: the realm's host and the realm's credentials from
    /// the environment. For `BinanceRealm::Mainnet` this fails unless the
    /// owner has armed `REAL_MONEY` on the host, and it fails at the
    /// credential read, before any socket is opened.
    pub fn new(realm: BinanceRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
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
    /// [`BinanceGateway::new`]. `tests/venue_fence.rs` is what stops this
    /// reaching a real venue: no host may be written outside `realm.rs`.
    pub fn for_test(
        base_url: &str,
        realm: BinanceRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        Self::build(realm, base_url, creds, symbols)
    }

    pub fn realm(&self) -> BinanceRealm {
        self.realm
    }

    fn build(
        realm: BinanceRealm,
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
            market_qty_rules: HashMap::new(),
            weight_budget: RollingBudget::new(WEIGHT_WINDOW),
            order_ten_second_budget: RollingBudget::new(ORDER_WINDOW),
            order_minute_budget: RollingBudget::new(ORDER_MINUTE_WINDOW),
            stop_seq: 0,
            last_rate_wait_ns: None,
        }
    }

    fn name_of(&self, symbol: SymbolId) -> Result<&Symbol, VenueError> {
        self.names
            .get(symbol.0 as usize)
            .ok_or_else(|| VenueError::BadRequest(format!("no symbol at id {}", symbol.0)))
    }

    async fn spend_weight(&mut self, cost: u32) {
        reserve(&mut self.weight_budget, cost, REQUEST_WEIGHT_PER_MINUTE).await;
    }

    fn settle_weight(&mut self, cost: u32) {
        self.weight_budget.anchor_completion(Instant::now(), cost);
    }

    async fn spend_orders(&mut self, cost: u32) -> u64 {
        let began = Instant::now();
        reserve(&mut self.order_minute_budget, cost, ORDERS_PER_MINUTE).await;
        reserve(
            &mut self.order_ten_second_budget,
            cost,
            ORDERS_PER_TEN_SECONDS,
        )
        .await;
        began.elapsed().as_nanos() as u64
    }

    fn settle_orders(&mut self, cost: u32) {
        let now = Instant::now();
        self.order_minute_budget.anchor_completion(now, cost);
        self.order_ten_second_budget.anchor_completion(now, cost);
    }

    fn venue_side(side: Side) -> &'static str {
        match side {
            Side::Buy => "BUY",
            Side::Sell => "SELL",
        }
    }

    /// The venue's spelling of a time-in-force. Post-only is `GTX` — "good
    /// till crossing" — which the venue expires instead of letting it take.
    fn venue_tif(tif: TimeInForce) -> &'static str {
        match tif {
            TimeInForce::Gtc => "GTC",
            TimeInForce::Ioc => "IOC",
            TimeInForce::PostOnly => "GTX",
        }
    }

    /// The wire parameters of one entry or exit order.
    fn order_params(
        name: &str,
        req: &OrderRequest,
    ) -> Result<Vec<(&'static str, String)>, VenueError> {
        let mut params: Vec<(&'static str, String)> = vec![
            ("symbol", name.to_string()),
            ("side", Self::venue_side(req.side).to_string()),
        ];
        match req.kind {
            OrderKind::Market => params.push(("type", "MARKET".to_string())),
            OrderKind::Limit { px, tif } => {
                params.push(("type", "LIMIT".to_string()));
                params.push(("timeInForce", Self::venue_tif(tif).to_string()));
                params.push(("price", venue_num(px)?));
            }
        }
        params.push(("quantity", venue_num(req.qty)?));
        if req.reduce_only {
            params.push(("reduceOnly", "true".to_string()));
        }
        params.push(("newClientOrderId", req.client_order_id.clone()));
        Ok(params)
    }

    /// The wire parameters of one position stop: a close-position stop-market
    /// on the mark price. `closePosition` sizes it by the position, so it
    /// carries no quantity — the venue refuses one beside it.
    fn stop_params(
        name: &str,
        position_side: Side,
        trigger_px: f64,
        client_order_id: String,
    ) -> Result<Vec<(&'static str, String)>, VenueError> {
        Ok(vec![
            ("algoType", "CONDITIONAL".to_string()),
            ("symbol", name.to_string()),
            // The stop closes the position, so it is on the other side of it.
            (
                "side",
                Self::venue_side(position_side.flipped()).to_string(),
            ),
            ("type", "STOP_MARKET".to_string()),
            ("closePosition", "true".to_string()),
            ("triggerPrice", venue_num(trigger_px)?),
            // The mark price: the price the venue liquidates against, and the
            // one a stop that exists to prevent liquidation should watch.
            ("workingType", "MARK_PRICE".to_string()),
            ("clientAlgoId", client_order_id),
        ])
    }

    fn mint_stop_id(&mut self) -> String {
        self.stop_seq += 1;
        format!("{STOP_ID_PREFIX}{}-{}", wall_ms(), self.stop_seq)
    }

    /// Stable across restarts so canceling an unfilled entry can address the
    /// separate close-position Algo stop created beside it. Fourteen digest
    /// bytes leave 112 bits of identity inside Binance's 36-character bound.
    fn attached_stop_id(entry_client_order_id: &str) -> String {
        let digest = Sha256::digest(entry_client_order_id.as_bytes());
        format!("{STOP_ID_PREFIX}{}", hex::encode(&digest[..14]))
    }

    async fn open_algo_orders_raw(&mut self, name: &str) -> Result<Value, VenueError> {
        self.spend_weight(WEIGHT_OPEN_ORDERS_SYMBOL).await;
        let reply = self
            .rest
            .get_signed(PATH_OPEN_ALGO_ORDERS, &[("symbol", name.to_string())])
            .await;
        self.settle_weight(WEIGHT_OPEN_ORDERS_SYMBOL);
        reply
    }

    /// Which side the venue holds in one symbol, or an error when it is flat.
    async fn held_position_side(&mut self, name: &str) -> Result<Side, VenueError> {
        self.spend_weight(WEIGHT_ACCOUNT).await;
        let reply = self.rest.get_signed(PATH_ACCOUNT, &[]).await;
        self.settle_weight(WEIGHT_ACCOUNT);
        let data = reply?;
        let rows = data
            .get("positions")
            .and_then(Value::as_array)
            .ok_or_else(|| VenueError::BadReply("the account reply carried no positions".into()))?;
        for row in rows {
            if row.get("symbol").and_then(Value::as_str) != Some(name) {
                continue;
            }
            let amount = crate::json::num_field(row, "positionAmt")?;
            if amount > 0.0 {
                return Ok(Side::Buy);
            }
            if amount < 0.0 {
                return Ok(Side::Sell);
            }
        }
        Err(VenueError::BadRequest(format!(
            "there is no open position in {name} for a stop to protect"
        )))
    }

    async fn market_qty_rule(&mut self, name: &str) -> Result<MarketQtyRule, VenueError> {
        if let Some(rule) = self.market_qty_rules.get(name) {
            return Ok(*rule);
        }
        self.spend_weight(WEIGHT_EXCHANGE_INFO).await;
        let reply = self.rest.get_public(PATH_EXCHANGE_INFO, "").await;
        self.settle_weight(WEIGHT_EXCHANGE_INFO);
        let parsed = parse_exchange_info(&reply?)?;
        self.market_qty_rules.extend(parsed.market_qty);
        self.market_qty_rules.get(name).copied().ok_or_else(|| {
            VenueError::BadReply(format!(
                "exchangeInfo carried no market-order size rule for {name}"
            ))
        })
    }

    fn validate_market_qty(name: &str, qty: f64, rule: MarketQtyRule) -> Result<(), VenueError> {
        let step_count = engine_types::quantize::steps(qty, rule.qty_step);
        let tolerance = rule.qty_step.max(1e-12) * 1e-9;
        if !qty.is_finite() || qty <= 0.0 {
            return Err(VenueError::BadRequest(format!(
                "{qty} is not a positive finite market-order quantity for {name}"
            )));
        }
        if qty + tolerance < rule.min_qty || qty - tolerance > rule.max_qty {
            return Err(VenueError::BadRequest(format!(
                "market-order quantity {qty} for {name} is outside Binance's {}..={} range",
                rule.min_qty, rule.max_qty
            )));
        }
        let step_error = (step_count - step_count.round()).abs();
        if !step_count.is_finite() || step_error > 1e-9 {
            return Err(VenueError::BadRequest(format!(
                "market-order quantity {qty} for {name} is not a multiple of Binance's {} step",
                rule.qty_step
            )));
        }
        Ok(())
    }

    fn validate_opening_qty(name: &str, qty: f64, rule: MarketQtyRule) -> Result<(), VenueError> {
        let step_count = engine_types::quantize::steps(qty, rule.opening_qty_step);
        let tolerance = rule.opening_qty_step.max(1e-12) * 1e-9;
        if !qty.is_finite() || qty <= 0.0 {
            return Err(VenueError::BadRequest(format!(
                "{qty} is not a positive finite opening quantity for {name}"
            )));
        }
        if qty + tolerance < rule.opening_min_qty || qty - tolerance > rule.max_qty {
            return Err(VenueError::BadRequest(format!(
                "opening quantity {qty} for {name} is outside Binance's shared LOT_SIZE and \
                 MARKET_LOT_SIZE {}..={} range",
                rule.opening_min_qty, rule.max_qty
            )));
        }
        let step_error = (step_count - step_count.round()).abs();
        if !step_count.is_finite() || step_error > 1e-9 {
            return Err(VenueError::BadRequest(format!(
                "opening quantity {qty} for {name} is not a multiple of the shared LOT_SIZE and \
                 MARKET_LOT_SIZE {} step",
                rule.opening_qty_step
            )));
        }
        Ok(())
    }
}

#[engine_types::async_trait]
impl VenueGateway for BinanceGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            // Kept by the venue as a close-position stop-market order that
            // outlives this process and is sized by the position however it
            // grows — which is what the engine needs of a position stop. Not
            // a field on the position, which is why `account_view` reads the
            // open algo orders to answer whether a position is protected.
            native_position_stop: true,
            // PUT /fapi/v1/order, addressed by the engine's own client id.
            // The venue modifies LIMIT orders only — which is the only kind
            // the engine amends — and a modified order goes to the back of
            // the queue.
            amend_in_place: true,
            // POST /fapi/v1/leverage.
            set_leverage: true,
            // The venue's close-position sentinel exists only on its trigger
            // order types, never on the plain market exit the engine would
            // send below the minimum.
            close_position_below_minimum: false,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        // Every asset row repeats the account's unique alias. Reading it from
        // the signed account removes the possibility that two writers name
        // one account differently in their local environment.
        self.spend_weight(WEIGHT_BALANCE).await;
        let balance = self.rest.get_signed(PATH_BALANCE, &[]).await;
        self.settle_weight(WEIGHT_BALANCE);
        let account_alias = parse_account_alias(&balance?)?;

        // Multi-assets mode reports account totals in USD-equivalent value
        // across eligible collateral. The shared account contract names and
        // consumes literal USDT, so accepting that reply would change units.
        self.spend_weight(WEIGHT_MULTI_ASSETS_MODE).await;
        let multi_assets = self.rest.get_signed(PATH_MULTI_ASSETS_MODE, &[]).await;
        self.settle_weight(WEIGHT_MULTI_ASSETS_MODE);
        if parse_multi_assets_mode(&multi_assets?)? {
            return Err(VenueError::BadRequest(
                "this Binance account uses multi-assets mode, whose account totals are \
                 USD-equivalent across eligible collateral; the engine requires literal USDT, \
                 so switch the account to single-asset mode before starting"
                    .to_string(),
            ));
        }

        // Reduce-only is refused in hedge mode, so every exit this engine
        // sends depends on one-way position mode.
        self.spend_weight(WEIGHT_POSITION_MODE).await;
        let reply = self.rest.get_signed(PATH_POSITION_MODE, &[]).await;
        self.settle_weight(WEIGHT_POSITION_MODE);
        let dual = reply?
            .get("dualSidePosition")
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                VenueError::BadReply("the position-mode reply carried no dualSidePosition".into())
            })?;
        if dual {
            return Err(VenueError::BadRequest(
                "this Binance account is in hedge mode, and every exit this engine sends is \
                 reduce-only, which hedge mode refuses; switch the account to one-way position \
                 mode before starting"
                    .to_string(),
            ));
        }
        Ok(AccountIdentity {
            venue: VENUE_NAME.to_string(),
            user_id: account_alias,
            realm: self.realm.as_str().to_string(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let name = self.name_of(req.symbol)?.clone();
        let entry = Self::order_params(&name, req)?;
        let qty_rule = self.market_qty_rule(&name).await?;
        if !req.reduce_only {
            // This constrains the submitted total under both filters. It does
            // not make a partial limit fill market-closeable: the venue can
            // fill less than MARKET_LOT_SIZE.minQty.
            Self::validate_opening_qty(&name, req.qty, qty_rule)?;
        }
        if matches!(req.kind, OrderKind::Market) {
            Self::validate_market_qty(&name, req.qty, qty_rule)?;
        }
        let stop_plan = match req.stop.filter(|_| !req.reduce_only) {
            Some(stop) => {
                let stop_id = Self::attached_stop_id(&req.client_order_id);
                let params = Self::stop_params(&name, req.side, stop.trigger_px, stop_id.clone())?;
                Some((stop_id, params))
            }
            None => None,
        };
        self.last_rate_wait_ns = Some(self.spend_orders(1 + u32::from(stop_plan.is_some())).await);
        let entry_reply = self.rest.post_signed(PATH_ORDER, &entry).await;
        self.settle_orders(1);
        let (entry_client_id, venue_order_id) = parse_order_ack(&entry_reply?)?;
        if entry_client_id != req.client_order_id {
            return Err(VenueError::BadReply(format!(
                "the entry reply acknowledged {entry_client_id}, not {}",
                req.client_order_id
            )));
        }

        // Conditional orders moved to the Algo Service. There is no endpoint
        // that atomically combines this ordinary entry with its stop, so the
        // stop follows immediately. A refused stop makes the accepted entry's
        // outcome unknown even when the best-effort cancel succeeds: a market
        // entry may already have filled.
        if let Some((stop_id, stop_params)) = stop_plan {
            let stop_reply = self.rest.post_signed(PATH_ALGO_ORDER, &stop_params).await;
            self.settle_orders(1);
            match stop_reply.and_then(|reply| parse_algo_ack(&reply)) {
                Ok((client_id, _)) if client_id == stop_id => {}
                result => {
                    self.spend_weight(WEIGHT_CANCEL).await;
                    let cancel = self
                        .rest
                        .delete_signed(
                            PATH_ORDER,
                            &[
                                ("symbol", name.clone()),
                                ("origClientOrderId", req.client_order_id.clone()),
                            ],
                        )
                        .await;
                    self.settle_weight(WEIGHT_CANCEL);
                    let stop_error = match result {
                        Ok((client_id, _)) => format!(
                            "the stop reply acknowledged {client_id}, not the sent id {stop_id}"
                        ),
                        Err(error) => error.to_string(),
                    };
                    let cancel_result = match cancel {
                        Ok(_) => "entry cancellation was accepted".to_string(),
                        Err(error) => format!("entry cancellation also failed: {error}"),
                    };
                    return Err(VenueError::BadReply(format!(
                        "entry {} was accepted but its algo stop was not: {stop_error}; \
                         {cancel_result}; reconcile the entry and position before retrying",
                        req.client_order_id
                    )));
                }
            }
        }

        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id,
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
        let waited = reserve(
            &mut self.weight_budget,
            WEIGHT_CANCEL,
            REQUEST_WEIGHT_PER_MINUTE,
        )
        .await;
        self.last_rate_wait_ns = Some(waited);
        let reply = self
            .rest
            .delete_signed(
                PATH_ORDER,
                &[
                    ("symbol", name.clone()),
                    ("origClientOrderId", client_order_id.to_string()),
                ],
            )
            .await;
        self.settle_weight(WEIGHT_CANCEL);
        let cancelled_order = reply?;
        let executed_qty = crate::json::num_field(&cancelled_order, "executedQty")?;
        if executed_qty < 0.0 {
            return Err(VenueError::BadReply(format!(
                "the cancellation reply for {client_order_id} carried negative executedQty {executed_qty}"
            )));
        }
        if executed_qty > 0.0 {
            // A partial fill leaves a position. Its close-position stop is
            // still protection, not an orphan, so only the unfilled tail was
            // canceled here.
            return Ok(());
        }

        // An attached stop is a separate Algo order. Its deterministic id is
        // recoverable after restart, so canceling a wholly unfilled entry
        // also removes the exact latent stop paired with it. Entries without
        // an attached stop legitimately answer "unknown order" here.
        let stop_id = Self::attached_stop_id(client_order_id);
        self.spend_weight(WEIGHT_CANCEL).await;
        let cancelled = self
            .rest
            .delete_signed(PATH_ALGO_ORDER, &[("clientAlgoId", stop_id.clone())])
            .await;
        self.settle_weight(WEIGHT_CANCEL);
        match cancelled {
            Err(error) if algo_order_is_already_gone(&error) => {
                tracing::debug!(%stop_id, symbol = %name, "the entry's attached stop was already gone");
            }
            result => {
                result?;
            }
        }
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
        let name = self.name_of(symbol)?.clone();
        // The venue's modify demands both the price and the quantity, so the
        // half that is not changing is read back rather than assumed.
        self.spend_weight(WEIGHT_QUERY_ORDER).await;
        let reply = self
            .rest
            .get_signed(
                PATH_ORDER,
                &[
                    ("symbol", name.clone()),
                    ("origClientOrderId", client_order_id.to_string()),
                ],
            )
            .await;
        self.settle_weight(WEIGHT_QUERY_ORDER);
        let current = reply?;
        if current.get("type").and_then(Value::as_str) != Some("LIMIT") {
            return Err(VenueError::BadRequest(format!(
                "{client_order_id} is not a LIMIT order, and this venue modifies no other kind"
            )));
        }
        let side = crate::json::str_field(&current, "side")?;
        let px = match spec.px {
            Some(px) => venue_num(px)?,
            None => crate::json::num_field(&current, "price").and_then(venue_num)?,
        };
        let qty = match spec.qty {
            Some(qty) => venue_num(qty)?,
            None => crate::json::num_field(&current, "origQty").and_then(venue_num)?,
        };

        let waited = self.spend_orders(1).await;
        self.last_rate_wait_ns = Some(waited);
        let reply = self
            .rest
            .put_signed(
                PATH_ORDER,
                &[
                    ("symbol", name),
                    ("side", side),
                    ("quantity", qty),
                    ("price", px),
                    ("origClientOrderId", client_order_id.to_string()),
                ],
            )
            .await;
        self.settle_orders(1);
        reply?;
        Ok(())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        // What the stop has to cover, from the venue rather than from memory.
        let position_side = self.held_position_side(&name).await?;

        // Which stops are standing now, read before anything is sent, so the
        // list is exactly the old ones and the replacement cannot be in it.
        let standing = self.open_algo_orders_raw(&name).await?;
        let old = native_stop_algo_ids(&standing, position_side)?;

        // The replacement first, the old ones after. The other order leaves
        // the position bare for the width of a round trip, and bare for good
        // if the placement then fails. Two stops for a moment is harmless:
        // both close the position, and the second can only close what is
        // already gone.
        let stop_id = self.mint_stop_id();
        let params = Self::stop_params(&name, position_side, trigger_px, stop_id.clone())?;
        let waited = self.spend_orders(1).await;
        self.last_rate_wait_ns = Some(waited);
        let placed = self.rest.post_signed(PATH_ALGO_ORDER, &params).await;
        self.settle_orders(1);
        let (accepted_id, _) = parse_algo_ack(&placed?)?;
        if accepted_id != stop_id {
            return Err(VenueError::BadReply(format!(
                "the replacement stop reply acknowledged {accepted_id}, not {stop_id}"
            )));
        }

        for order_id in old {
            self.spend_weight(WEIGHT_CANCEL).await;
            let cancelled = self
                .rest
                .delete_signed(PATH_ALGO_ORDER, &[("algoId", order_id.clone())])
                .await;
            self.settle_weight(WEIGHT_CANCEL);
            // A stop that fired or was pulled between the read and here is
            // gone, which is the state this was asking for.
            match cancelled {
                Err(error) if algo_order_is_already_gone(&error) => {
                    tracing::debug!(%order_id, symbol = %name, "a standing stop was already gone");
                }
                result => {
                    result?;
                }
            }
        }
        Ok(())
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        let name = self.name_of(symbol)?.clone();
        if !leverage.is_finite() || !(1.0..=125.0).contains(&leverage) {
            return Err(VenueError::BadRequest(format!(
                "{leverage} is not a leverage Binance will take"
            )));
        }
        // Whole numbers only, and rounded DOWN: asking for 2.9 and getting 3
        // would post less margin than the risk kernel priced. The per-symbol
        // ceiling lives in the venue's leverage brackets; a figure above it
        // is the venue's own rejection to make, and it makes it by name.
        let whole = (leverage.floor() as i64).max(1);
        self.spend_weight(WEIGHT_LEVERAGE).await;
        let reply = self
            .rest
            .post_signed(
                PATH_LEVERAGE,
                &[("symbol", name), ("leverage", whole.to_string())],
            )
            .await;
        self.settle_weight(WEIGHT_LEVERAGE);
        reply?;
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
        let (observed_ns, reply) = account_scan(async {
            self.spend_weight(WEIGHT_ACCOUNT).await;
            let account = self.rest.get_signed(PATH_ACCOUNT, &[]).await;
            self.settle_weight(WEIGHT_ACCOUNT);
            let account = account?;
            // The position rows say nothing about stops, so the stop book is
            // read beside them and joined in — one open-algo read per held
            // symbol, because "which symbols" is only known from the account.
            // Without the join every position would report itself
            // unprotected, and the engine would act on that.
            let (equity, available, mut positions) =
                parse_account(&account, &self.ids, &HashMap::new())?;
            for position in &mut positions {
                let name = self.name_of(position.symbol)?.clone();
                let stops = parse_position_stops(&self.open_algo_orders_raw(&name).await?);
                if let Some(held) = stops.get(name.as_str()) {
                    position.stop_px = held.nearest(position.side).unwrap_or(0.0);
                    position.stop_attached = position.stop_px > 0.0;
                }
            }
            Ok::<_, VenueError>((equity, available, positions))
        })
        .await;
        let (equity_usdt, available_usdt, positions) = reply?;
        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions,
            observed_ns,
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.spend_weight(WEIGHT_EXCHANGE_INFO).await;
        let reply = self.rest.get_public(PATH_EXCHANGE_INFO, "").await;
        self.settle_weight(WEIGHT_EXCHANGE_INFO);
        let parsed = parse_exchange_info(&reply?)?;
        self.market_qty_rules = parsed.market_qty;
        Ok(parsed.instruments)
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        // Addressed to the account, not to a symbol list: the point of this
        // read is to find orders nobody here placed, and asking only about
        // the symbols the engine knows would hide exactly those.
        self.spend_weight(WEIGHT_OPEN_ORDERS_ALL).await;
        let reply = self.rest.get_signed(PATH_OPEN_ORDERS, &[]).await;
        self.settle_weight(WEIGHT_OPEN_ORDERS_ALL);
        let mut orders = parse_working_orders(&reply?)?;
        self.spend_weight(WEIGHT_OPEN_ORDERS_ALL).await;
        let algos = self.rest.get_signed(PATH_OPEN_ALGO_ORDERS, &[]).await;
        self.settle_weight(WEIGHT_OPEN_ORDERS_ALL);
        self.spend_weight(WEIGHT_ACCOUNT).await;
        let account = self.rest.get_signed(PATH_ACCOUNT, &[]).await;
        self.settle_weight(WEIGHT_ACCOUNT);
        let held_sides = parse_position_sides(&account?)?;
        orders.extend(parse_working_algo_orders(&algos?, &held_sides)?);
        Ok(orders)
    }

    async fn executions(
        &mut self,
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        Err(VenueError::BadRequest(
            "Binance execution recovery is unavailable: account trades require a symbol, while \
             account-wide order discovery and order lookup can omit a fill from an ordinary GTC \
             order created more than 90 days ago; a complete account-wide interval cannot be \
             proved"
                .to_string(),
        ))
    }

    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        self.last_rate_wait_ns.take()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::orders::StopSpec;
    use engine_types::StrategyId;

    fn gateway() -> BinanceGateway {
        BinanceGateway::for_test(
            "http://127.0.0.1:1",
            BinanceRealm::Testnet,
            BinanceRealm::Testnet.credentials_for_test("k", "s"),
            vec!["BTCUSDT".to_string()],
        )
    }

    fn request(kind: OrderKind, reduce_only: bool) -> OrderRequest {
        OrderRequest {
            client_order_id: "eng-1700000000000-1".to_string(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.004,
            kind,
            stop: Some(StopSpec {
                trigger_px: 75000.5,
            }),
            reduce_only,
            close_position: false,
        }
    }

    fn value_of<'a>(params: &'a [(&'static str, String)], key: &str) -> Option<&'a str> {
        params
            .iter()
            .find(|(name, _)| *name == key)
            .map(|(_, value)| value.as_str())
    }

    #[test]
    fn the_caps_say_what_this_adapter_can_actually_do() {
        let caps = gateway().caps();
        assert!(
            caps.native_position_stop,
            "the venue keeps the close-position stop order, and set_stop moves it"
        );
        assert!(caps.amend_in_place);
        assert!(caps.set_leverage);
        assert!(!caps.close_position_below_minimum);
    }

    #[test]
    fn post_only_goes_out_as_gtx_and_the_sides_are_spelled_the_venues_way() {
        assert_eq!(BinanceGateway::venue_tif(TimeInForce::Gtc), "GTC");
        assert_eq!(BinanceGateway::venue_tif(TimeInForce::Ioc), "IOC");
        assert_eq!(BinanceGateway::venue_tif(TimeInForce::PostOnly), "GTX");
        assert_eq!(BinanceGateway::venue_side(Side::Buy), "BUY");
        assert_eq!(BinanceGateway::venue_side(Side::Sell), "SELL");
    }

    #[test]
    fn a_market_order_carries_no_price_and_no_time_in_force() {
        let params =
            BinanceGateway::order_params("BTCUSDT", &request(OrderKind::Market, false)).unwrap();
        assert_eq!(value_of(&params, "type"), Some("MARKET"));
        assert_eq!(value_of(&params, "quantity"), Some("0.004"));
        assert_eq!(
            value_of(&params, "newClientOrderId"),
            Some("eng-1700000000000-1")
        );
        assert!(value_of(&params, "price").is_none());
        assert!(value_of(&params, "timeInForce").is_none());
        assert!(value_of(&params, "reduceOnly").is_none());
    }

    #[test]
    fn decimal_dust_does_not_reject_a_valid_market_quantity_step() {
        for (qty, step) in [(8.197, 0.001), (819.3, 0.1)] {
            let rule = MarketQtyRule {
                min_qty: step,
                max_qty: 10_000.0,
                qty_step: step,
                opening_min_qty: step,
                opening_qty_step: step,
            };
            BinanceGateway::validate_market_qty("DUSTUSDT", qty, rule).unwrap();
            assert!(
                BinanceGateway::validate_market_qty("DUSTUSDT", qty + step / 2.0, rule).is_err()
            );
        }
    }

    #[test]
    fn a_limit_order_carries_both_and_an_exit_says_reduce_only() {
        let params = BinanceGateway::order_params(
            "BTCUSDT",
            &request(
                OrderKind::Limit {
                    px: 78000.1,
                    tif: TimeInForce::PostOnly,
                },
                true,
            ),
        )
        .unwrap();
        assert_eq!(value_of(&params, "type"), Some("LIMIT"));
        assert_eq!(value_of(&params, "timeInForce"), Some("GTX"));
        assert_eq!(value_of(&params, "price"), Some("78000.1"));
        assert_eq!(value_of(&params, "reduceOnly"), Some("true"));
    }

    #[test]
    fn the_stop_closes_the_whole_position_from_the_other_side_on_the_mark() {
        let stop =
            BinanceGateway::stop_params("BTCUSDT", Side::Buy, 75000.5, "engstop-1-1".to_string())
                .unwrap();
        assert_eq!(
            value_of(&stop, "side"),
            Some("SELL"),
            "a long stops by selling"
        );
        assert_eq!(value_of(&stop, "algoType"), Some("CONDITIONAL"));
        assert_eq!(value_of(&stop, "type"), Some("STOP_MARKET"));
        assert_eq!(value_of(&stop, "closePosition"), Some("true"));
        assert_eq!(value_of(&stop, "triggerPrice"), Some("75000.5"));
        assert_eq!(value_of(&stop, "workingType"), Some("MARK_PRICE"));
        assert_eq!(value_of(&stop, "clientAlgoId"), Some("engstop-1-1"));
        assert!(value_of(&stop, "newClientOrderId").is_none());
        assert!(
            value_of(&stop, "quantity").is_none(),
            "closePosition and quantity are refused together"
        );
        assert!(
            value_of(&stop, "reduceOnly").is_none(),
            "closePosition and reduceOnly are refused together"
        );

        let on_short =
            BinanceGateway::stop_params("BTCUSDT", Side::Sell, 80000.0, "engstop-1-2".to_string())
                .unwrap();
        assert_eq!(value_of(&on_short, "side"), Some("BUY"));
    }

    #[test]
    fn a_minted_stop_id_is_ours_unique_and_inside_the_venues_bound() {
        let mut gw = gateway();
        let a = gw.mint_stop_id();
        let b = gw.mint_stop_id();
        assert_ne!(a, b, "two stops in one millisecond must not share an id");
        for id in [&a, &b] {
            assert!(id.starts_with(STOP_ID_PREFIX));
            // The venue caps a client order id at 36 characters.
            assert!(id.len() <= 36, "{id}");
            assert!(super::super::parse::is_exchange_or_stop_id(id));
        }
    }

    #[tokio::test]
    async fn an_empty_amend_is_refused_before_anything_is_read() {
        let mut gw = gateway();
        let err = gw
            .amend_order(
                SymbolId(0),
                "eng-1",
                AmendSpec {
                    px: None,
                    qty: None,
                },
            )
            .await
            .unwrap_err();
        assert!(err.to_string().contains("neither"), "{err}");
    }

    #[tokio::test]
    async fn a_leverage_the_venue_cannot_take_is_refused_before_the_wire() {
        let mut gw = gateway();
        for bad in [0.5, 0.0, -3.0, 126.0, f64::NAN, f64::INFINITY] {
            assert!(gw.set_leverage(SymbolId(0), bad).await.is_err(), "{bad}");
        }
    }

    #[test]
    fn only_the_known_unknown_order_cancel_is_already_gone() {
        assert!(algo_order_is_already_gone(&VenueError::Rejected {
            code: -2011,
            message: "Unknown order sent.".into(),
        }));
        assert!(!algo_order_is_already_gone(&VenueError::Rejected {
            code: -2011,
            message: "CANCEL_REJECTED".into(),
        }));
        assert!(!algo_order_is_already_gone(&VenueError::Rejected {
            code: -4120,
            message: "Order type not supported for this endpoint.".into(),
        }));
    }

    #[test]
    fn the_budget_holds_a_request_back_when_the_window_is_spent() {
        let mut budget = RollingBudget::new(Duration::from_secs(10));
        let now = Instant::now();
        assert!(budget.try_reserve(now, 299, 300).is_ok());
        assert!(budget.try_reserve(now, 1, 300).is_ok());
        let wait = budget.try_reserve(now, 1, 300).unwrap_err();
        assert!(wait > Duration::from_secs(9), "{wait:?}");
        // A window later the spend has expired.
        let later = now + Duration::from_secs(11);
        assert!(budget.try_reserve(later, 300, 300).is_ok());
    }

    #[test]
    fn completion_anchoring_moves_the_spend_to_the_conservative_side() {
        let mut budget = RollingBudget::new(Duration::from_secs(10));
        let began = Instant::now();
        assert!(budget.try_reserve(began, 300, 300).is_ok());
        // The venue saw the request later than local admission did.
        let completed = began + Duration::from_secs(5);
        budget.anchor_completion(completed, 300);
        let wait = budget
            .try_reserve(began + Duration::from_secs(11), 1, 300)
            .unwrap_err();
        assert!(
            wait > Duration::from_secs(3),
            "the anchored spend should still be in the window: {wait:?}"
        );
    }

    #[test]
    fn a_cost_larger_than_the_whole_budget_cannot_wait_forever() {
        let mut budget = RollingBudget::new(Duration::from_secs(10));
        assert_eq!(
            budget.try_reserve(Instant::now(), 500, 300),
            Err(Duration::ZERO)
        );
    }

    #[test]
    fn a_symbol_taken_on_later_gets_the_next_id_in_order() {
        let mut gw = gateway();
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
