//! The Bybit gateway: the [`VenueGateway`] contract over Bybit v5.
//!
//! One adapter, two realms. Which account it reaches is decided once, at
//! construction, by the [`VenueRealm`] handed to [`BybitGateway::new`] — and
//! the host is derived from that realm rather than passed alongside it, so the
//! two cannot disagree.

use std::collections::{HashMap, HashSet, VecDeque};
use std::time::{Duration, Instant};

use engine_types::ids::{Symbol, SymbolId};
use engine_types::orders::{
    AccountInventory, AccountOrder, AccountPosition, AmendSpec, InstrumentRule, OrderAck,
    OrderKind, OrderRequest, Side, TimeInForce, VenueExecution, VenueOrder,
};
use engine_types::risk::AccountView;
use engine_types::{AccountIdentity, VenueCaps, VenueError, VenueGateway, VenueMutationTiming};
use serde_json::{Map, Value};

use super::parse::{
    parse_active_strategies, parse_asset_overview, parse_cancel_batch, parse_executions,
    parse_instruments, parse_inventory_orders, parse_inventory_positions, parse_inventory_wallet,
    parse_linear_settle_coins, parse_order_ack, parse_positions, parse_rfq_quotes,
    parse_rfq_requests, parse_spread_orders, parse_wallet, parse_working_orders, venue_result,
    verify_attestation_key, verify_funded_key, verify_one_way_position,
};
use super::realm::VenueRealm;
use super::rest::RestClient;
use super::sign::RECV_WINDOW_MS;
use super::trade_ws::TradeClient;
use super::CATEGORY;
use crate::creds::Credentials;
use crate::fmt::venue_num;
use crate::http::percent_encode;
use crate::mono_ns;
use crate::wall_ms;

const PATH_TIME: &str = "/v5/market/time";
const PATH_ORDER_CREATE: &str = "/v5/order/create";
const PATH_ORDER_CANCEL: &str = "/v5/order/cancel";
const PATH_ORDER_CANCEL_BATCH: &str = "/v5/order/cancel-batch";
const PATH_ORDER_AMEND: &str = "/v5/order/amend";
const PATH_TRADING_STOP: &str = "/v5/position/trading-stop";
const PATH_SET_LEVERAGE: &str = "/v5/position/set-leverage";
const PATH_WALLET: &str = "/v5/account/wallet-balance";
const PATH_ASSET_OVERVIEW: &str = "/v5/asset/asset-overview";
const PATH_POSITIONS: &str = "/v5/position/list";
const PATH_INSTRUMENTS: &str = "/v5/market/instruments-info";
const PATH_QUERY_API: &str = "/v5/user/query-api";
const PATH_ORDERS_OPEN: &str = "/v5/order/realtime";
const PATH_SPREAD_ORDERS_OPEN: &str = "/v5/spread/order/realtime";
const PATH_RFQ_QUOTES_OPEN: &str = "/v5/rfq/quote-realtime";
const PATH_RFQS_OPEN: &str = "/v5/rfq/rfq-realtime";
const PATH_STRATEGIES: &str = "/v5/strategy/list";
const PATH_EXECUTIONS: &str = "/v5/execution/list";

// Bybit's documented create-order quota is ten requests per UID per second.
// Up to ten siblings leave together. A larger call is refused before the wire:
// the engine must revalidate it as another bounded group, not reserve it behind
// one or more HTTP timeout waves.
const ORDER_CREATES_PER_SECOND: usize = 10;
const ORDER_CANCELS_PER_SECOND: usize = 10;
const ORDER_CANCEL_BATCHES_PER_SECOND: usize = 10;
const ORDER_AMENDS_PER_SECOND: usize = 10;
const TRADING_STOPS_MAINNET_PER_SECOND: usize = 10;
const TRADING_STOPS_DEMO_PER_SECOND: usize = 5;
const LEVERAGE_CHANGES_PER_SECOND: usize = 10;
const POSITION_READS_PER_SECOND: usize = 50;
const POSITION_MODE_PAGE_LIMIT: usize = 200;
const WARM_CONNECTIONS: usize = 10;
// The endpoint accepts twenty request objects, but Bybit consumes rate-limit
// quota by object and the default linear quota is ten per second. Refusing a
// larger wire batch keeps admission exact instead of relying on a 10006 retry
// after a halt has already begun.
const MAX_CANCEL_BATCH_ITEMS: usize = 10;
const RATE_LIMIT_WINDOW: Duration = Duration::from_secs(1);
const RATE_LIMIT_GUARD: Duration = Duration::from_millis(1);

/// Enough to cover every linear contract several times over; a cursor that
/// never empties is a venue fault, not a reason to loop forever.
const MAX_PAGES: usize = 20;

pub struct BybitGateway {
    realm: VenueRealm,
    rest: RestClient,
    trade: Option<TradeClient>,
    names: Vec<Symbol>,
    ids: HashMap<Symbol, SymbolId>,
    one_way_verified: Vec<bool>,
    clock_checked: bool,
    create_limiter: RollingRateLimiter,
    cancel_limiter: RollingRateLimiter,
    cancel_batch_limiter: RollingRateLimiter,
    amend_limiter: RollingRateLimiter,
    stop_limiter: RollingRateLimiter,
    leverage_limiter: RollingRateLimiter,
    position_read_limiter: RollingRateLimiter,
    attestation_key: bool,
    last_mutation_timing: Option<VenueMutationTiming>,
    last_rate_wait_ns: Option<u64>,
}

#[derive(Default)]
struct RollingRateLimiter {
    admissions: VecDeque<Instant>,
    /// This account's own per-second quota for the endpoint, as the venue
    /// stated it on an acknowledgement. `None` until one has been read.
    declared: Option<usize>,
}

impl RollingRateLimiter {
    /// How many requests a second to pace to: the venue's own figure when it
    /// is the larger, otherwise the documented default.
    ///
    /// Raise-only, on purpose. Every batch this adapter sends is already
    /// capped at the documented default, so `count <= limit` holds by
    /// construction and a reservation can always eventually be met. Obeying a
    /// smaller declared limit would break that invariant for a batch already
    /// admitted, so a smaller figure is logged and not adopted.
    fn limit(&self, default: usize) -> usize {
        self.declared.map_or(default, |seen| seen.max(default))
    }

    /// Record what the venue said its limit is. True when this is news.
    fn declare(&mut self, seen: Option<usize>) -> bool {
        match seen {
            Some(seen) if self.declared != Some(seen) => {
                self.declared = Some(seen);
                true
            }
            _ => false,
        }
    }

    /// Reserve a batch against Bybit's rolling one-second per-UID window.
    /// `Err(wait)` means no capacity was consumed and the caller must retry
    /// after the named delay. One millisecond of guard keeps local/venue clock
    /// boundaries from turning an exactly-one-second admission into 10006.
    fn try_reserve(&mut self, now: Instant, count: usize, limit: usize) -> Result<(), Duration> {
        while self
            .admissions
            .front()
            .is_some_and(|at| now.duration_since(*at) >= RATE_LIMIT_WINDOW + RATE_LIMIT_GUARD)
        {
            self.admissions.pop_front();
        }
        if self.admissions.len() + count <= limit {
            self.admissions.extend(std::iter::repeat_n(now, count));
            return Ok(());
        }

        let must_expire = self.admissions.len() + count - limit;
        let release = self.admissions[must_expire - 1] + RATE_LIMIT_WINDOW + RATE_LIMIT_GUARD;
        Err(release.saturating_duration_since(now))
    }

    /// Venue arrival happens after local admission. Re-anchor the requests
    /// that just completed to the conservative side of that uncertainty, so
    /// a faster following request cannot cross the server's rolling window
    /// even when the preceding network leg was slow.
    fn anchor_completion(&mut self, completed: Instant, count: usize) {
        debug_assert!(count <= self.admissions.len());
        for admitted in self.admissions.iter_mut().rev().take(count) {
            *admitted = completed;
        }
    }
}

/// Returns how long admission was held back. Nanoseconds of our own pacing,
/// which the order path measures apart from the venue's own leg.
async fn reserve_rate_capacity(
    limiter: &mut RollingRateLimiter,
    count: usize,
    default: usize,
) -> u64 {
    debug_assert!(count <= default);
    let limit = limiter.limit(default);
    let began = Instant::now();
    loop {
        match limiter.try_reserve(Instant::now(), count, limit) {
            Ok(()) => return began.elapsed().as_nanos() as u64,
            Err(wait) => tokio::time::sleep(wait).await,
        }
    }
}

fn copy_venue_error(error: &VenueError) -> VenueError {
    match error {
        VenueError::BadRequest(detail) => VenueError::BadRequest(detail.clone()),
        VenueError::Transport(detail) => VenueError::Transport(detail.clone()),
        VenueError::Rejected { code, message } => VenueError::Rejected {
            code: *code,
            message: message.clone(),
        },
        VenueError::BadReply(detail) => VenueError::BadReply(detail.clone()),
        VenueError::Credentials(detail) => VenueError::Credentials(detail.clone()),
    }
}

fn cancel_batch_error(count: usize, error: VenueError) -> Vec<Result<(), VenueError>> {
    (0..count).map(|_| Err(copy_venue_error(&error))).collect()
}

/// A deliberately narrow live capability for deployment attestation.
///
/// Its gateway is private and this type exposes no order, cancel, amend,
/// leverage, stop, or websocket API. That is why it may authenticate a
/// disarmed funded account: proving old exposure absent is not authority to
/// create new exposure.
pub struct BybitInventoryProbe {
    gateway: BybitGateway,
}

impl BybitInventoryProbe {
    pub fn new(realm: VenueRealm) -> Result<Self, VenueError> {
        let credentials = realm.inventory_credentials()?;
        let mut gateway =
            BybitGateway::build(realm, realm.rest_base(), credentials, Vec::new(), None);
        gateway.attestation_key = realm == VenueRealm::Mainnet;
        Ok(Self { gateway })
    }

    pub async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        self.gateway.account_identity().await
    }

    pub async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        self.gateway.account_inventory().await
    }
}

impl BybitGateway {
    /// The live gateway: the realm's host, and the realm's credentials from
    /// the environment. There is no argument for the host on purpose — it is
    /// derived from the realm, so the account being addressed and the account
    /// being signed for are one decision, not two that must be kept in step.
    ///
    /// For `VenueRealm::Mainnet` this fails unless the owner has armed
    /// `REAL_MONEY` on the host, and it fails at the credential read, before
    /// any socket is opened.
    ///
    /// `symbols` is the engine's symbol table in `SymbolId` order — position
    /// `i` is the name of `SymbolId(i)`.
    pub fn new(realm: VenueRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        let built = Self::build(realm, realm.rest_base(), creds, symbols, realm.trade_ws());
        // The Python fleet reads the resolved endpoint back and compares it to
        // the realm it asked for (`bybit._require_realm_endpoint`), because
        // there the transport picks its own host from a separate argument.
        // Here one function derives it, so this can only fail if someone later
        // reintroduces a second way to set the host — which is exactly when
        // you want to hear about it.
        if built.rest.base() != realm.rest_base() {
            return Err(VenueError::BadRequest(format!(
                "realm {realm} resolved to {}, but only {} is permitted for that realm",
                built.rest.base(),
                realm.rest_base()
            )));
        }
        Ok(built)
    }

    /// Point the gateway at a local server. Tests and the mock-venue
    /// benchmark only; the live path is [`BybitGateway::new`].
    ///
    /// This is the one constructor that takes a host, so it is the one that
    /// could name a real venue. `tests/venue_fence.rs` is what stops that:
    /// no venue host may be written anywhere outside `realm.rs`, test files
    /// included.
    pub fn for_test(
        base_url: &str,
        realm: VenueRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        let mut gateway = Self::build(realm, base_url, creds, symbols, None);
        gateway.one_way_verified.fill(true);
        gateway
    }

    /// Point both transports at local servers. Tests only.
    #[doc(hidden)]
    pub fn for_test_with_trade_transport(
        base_url: &str,
        trade_url: &str,
        realm: VenueRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        let mut gateway = Self::build(realm, base_url, creds, symbols, Some(trade_url));
        gateway.one_way_verified.fill(true);
        gateway
    }

    /// Point the gateway at a local server and retain the live startup mode
    /// check. Request-shape tests use this; production uses [`Self::new`].
    #[doc(hidden)]
    pub fn for_test_with_position_mode_check(
        base_url: &str,
        realm: VenueRealm,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        Self::build(realm, base_url, creds, symbols, None)
    }

    /// Which account this gateway addresses: the realm it was built for, and
    /// the one whose credentials it signs with.
    pub fn realm(&self) -> VenueRealm {
        self.realm
    }

    fn build(
        realm: VenueRealm,
        base_url: &str,
        creds: Credentials,
        symbols: Vec<Symbol>,
        trade_url: Option<&str>,
    ) -> Self {
        let one_way_verified = vec![false; symbols.len()];
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Self {
            realm,
            rest: RestClient::new(base_url, creds.clone()),
            trade: trade_url.map(|url| TradeClient::new(url, creds)),
            names: symbols,
            ids,
            one_way_verified,
            clock_checked: false,
            create_limiter: RollingRateLimiter::default(),
            cancel_limiter: RollingRateLimiter::default(),
            cancel_batch_limiter: RollingRateLimiter::default(),
            amend_limiter: RollingRateLimiter::default(),
            stop_limiter: RollingRateLimiter::default(),
            leverage_limiter: RollingRateLimiter::default(),
            position_read_limiter: RollingRateLimiter::default(),
            attestation_key: false,
            last_mutation_timing: None,
            last_rate_wait_ns: None,
        }
    }

    /// Open the TLS session before an order needs it.
    pub async fn warm(&mut self) -> Result<(), VenueError> {
        if self.clock_checked {
            return Ok(());
        }
        // HTTP/1.1 cannot multiplex a sibling burst. Open the whole ten-order
        // pool concurrently now, off the hot path, and validate the server
        // clock on every response rather than treating nine warm-up replies
        // as throwaways.
        let checks = (0..WARM_CONNECTIONS).map(|_| async {
            let sent_ms = wall_ms();
            let envelope = self.rest.get_public(PATH_TIME, "").await?;
            let received_ms = wall_ms();
            validate_server_clock(&envelope, sent_ms, received_ms)?;
            venue_result(envelope)?;
            Ok::<(), VenueError>(())
        });
        futures_util::future::try_join_all(checks).await?;
        let trade_warm = match &mut self.trade {
            Some(trade) => trade.warm().await,
            None => Ok(()),
        };
        if let Err(error) = trade_warm {
            tracing::warn!(
                error = %error,
                "Bybit trade WebSocket unavailable; using signed REST for order mutations this run"
            );
            self.trade = None;
        }
        self.clock_checked = true;
        Ok(())
    }

    /// Add a symbol the engine has since interned. Ids stay in step with the
    /// engine's own table only if it appends in the same order.
    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        if let Some(id) = self.ids.get(name) {
            return *id;
        }
        let id = SymbolId(u16::try_from(self.names.len()).expect("more than 65535 symbols"));
        self.names.push(name.to_string());
        self.one_way_verified.push(false);
        self.ids.insert(name.to_string(), id);
        id
    }

    pub fn symbols(&self) -> &[Symbol] {
        &self.names
    }

    fn name_of(&self, id: SymbolId) -> Result<&str, VenueError> {
        self.names
            .get(id.0 as usize)
            .map(String::as_str)
            .ok_or_else(|| {
                VenueError::BadRequest(format!("symbol id {} is not in the gateway's table", id.0))
            })
    }

    async fn require_one_way(&mut self, id: SymbolId) -> Result<(), VenueError> {
        let at = id.0 as usize;
        let verified = self.one_way_verified.get(at).copied().ok_or_else(|| {
            VenueError::BadRequest(format!("symbol id {} is not in the gateway's table", id.0))
        })?;
        if verified {
            return Ok(());
        }

        let symbol = self.name_of(id)?.to_string();
        let query = format!(
            "category={CATEGORY}&symbol={}&limit={POSITION_MODE_PAGE_LIMIT}",
            percent_encode(&symbol)
        );
        reserve_rate_capacity(
            &mut self.position_read_limiter,
            1,
            POSITION_READS_PER_SECOND,
        )
        .await;
        let envelope = self.rest.get_signed(PATH_POSITIONS, &query).await;
        self.position_read_limiter
            .anchor_completion(Instant::now(), 1);
        verify_one_way_position(&venue_result(envelope?)?, &symbol)?;
        self.one_way_verified[at] = true;
        Ok(())
    }

    async fn require_configured_symbols_one_way(&mut self) -> Result<(), VenueError> {
        let pending: Vec<(usize, String)> = self
            .names
            .iter()
            .enumerate()
            .filter(|(at, _)| !self.one_way_verified[*at])
            .map(|(at, symbol)| (at, symbol.clone()))
            .collect();
        for wave in pending.chunks(POSITION_READS_PER_SECOND) {
            reserve_rate_capacity(
                &mut self.position_read_limiter,
                wave.len(),
                POSITION_READS_PER_SECOND,
            )
            .await;
            let rest = &self.rest;
            let checks = wave.iter().map(|(_, symbol)| async move {
                let query = format!(
                    "category={CATEGORY}&symbol={}&limit={POSITION_MODE_PAGE_LIMIT}",
                    percent_encode(symbol)
                );
                let envelope = rest.get_signed(PATH_POSITIONS, &query).await?;
                verify_one_way_position(&venue_result(envelope)?, symbol)
            });
            let result = futures_util::future::try_join_all(checks).await;
            self.position_read_limiter
                .anchor_completion(Instant::now(), wave.len());
            result?;
            for (at, _) in wave {
                self.one_way_verified[*at] = true;
            }
        }
        Ok(())
    }

    fn order_body(&self, req: &OrderRequest) -> Result<Value, VenueError> {
        if req.close_position && (!req.reduce_only || !matches!(req.kind, OrderKind::Market)) {
            return Err(VenueError::BadRequest(
                "a full-position close must be a reduce-only market order".into(),
            ));
        }
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(req.symbol)?.into());
        body.insert("side".into(), side_str(req.side).into());
        body.insert(
            "qty".into(),
            if req.close_position {
                "0".into()
            } else {
                venue_num(req.qty)?.into()
            },
        );
        body.insert("reduceOnly".into(), req.reduce_only.into());
        if req.close_position {
            body.insert("closeOnTrigger".into(), true.into());
        }
        body.insert("orderLinkId".into(), req.client_order_id.as_str().into());
        match req.kind {
            OrderKind::Market => {
                body.insert("orderType".into(), "Market".into());
            }
            OrderKind::Limit { px, tif } => {
                body.insert("orderType".into(), "Limit".into());
                body.insert("price".into(), venue_num(px)?.into());
                body.insert("timeInForce".into(), tif_str(tif).into());
            }
        }
        if let Some(stop) = req.stop {
            if !req.reduce_only {
                body.insert("tpslMode".into(), "Full".into());
                body.insert("stopLoss".into(), venue_num(stop.trigger_px)?.into());
                body.insert("slTriggerBy".into(), "MarkPrice".into());
                body.insert("slOrderType".into(), "Market".into());
                body.insert("positionIdx".into(), 0.into());
            }
        }
        Ok(Value::Object(body))
    }

    async fn send_one(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let body = self.order_body(req)?;
        if let Some(trade) = &mut self.trade {
            let reply = trade.request("order.create", vec![body]).await?;
            Self::note_quota(
                &mut self.create_limiter,
                "order.create",
                ORDER_CREATES_PER_SECOND,
                reply.quota_per_second,
            );
            let mut ack = parse_order_ack(&reply.data, &req.client_order_id, reply.ack_ns)?;
            ack.sent_ns = reply.sent_ns;
            return Ok(ack);
        }
        self.send_one_rest(req, body).await
    }

    async fn send_one_rest(&self, req: &OrderRequest, body: Value) -> Result<OrderAck, VenueError> {
        let envelope = self.rest.post_signed(PATH_ORDER_CREATE, &body).await?;
        let ack_ns = mono_ns();
        let result = venue_result(envelope)?;
        parse_order_ack(&result, &req.client_order_id, ack_ns)
    }

    async fn send_trade_batch(
        &mut self,
        reqs: &[OrderRequest],
    ) -> Vec<Result<OrderAck, VenueError>> {
        let mut items = Vec::with_capacity(reqs.len());
        for req in reqs {
            let mut body = match self.order_body(req) {
                Ok(Value::Object(body)) => body,
                Ok(_) => unreachable!("order body is an object"),
                Err(error) => return reqs.iter().map(|_| Err(copy_venue_error(&error))).collect(),
            };
            body.remove("category");
            items.push(Value::Object(body));
        }
        let args = vec![serde_json::json!({
            "category": CATEGORY,
            "request": items,
        })];
        let reply = match self
            .trade
            .as_mut()
            .expect("called only with WebSocket transport")
            .request("order.create-batch", args)
            .await
        {
            Ok(reply) => reply,
            Err(error) => return reqs.iter().map(|_| Err(copy_venue_error(&error))).collect(),
        };
        // The batch endpoint spends create quota by object, so its stated
        // limit belongs to the same window the single creates draw on.
        Self::note_quota(
            &mut self.create_limiter,
            "order.create-batch",
            ORDER_CREATES_PER_SECOND,
            reply.quota_per_second,
        );
        let rows = reply
            .data
            .get("list")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let statuses = reply
            .ret_ext_info
            .get("list")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        if rows.len() != reqs.len() || statuses.len() != reqs.len() {
            let error = VenueError::BadReply(format!(
                "WebSocket batch returned {} rows and {} statuses for {} orders",
                rows.len(),
                statuses.len(),
                reqs.len()
            ));
            return reqs.iter().map(|_| Err(copy_venue_error(&error))).collect();
        }
        reqs.iter()
            .zip(rows)
            .zip(statuses)
            .map(|((req, row), status)| {
                let code = status.get("code").and_then(Value::as_i64).unwrap_or(-1);
                let message = status
                    .get("msg")
                    .and_then(Value::as_str)
                    .unwrap_or("batch item carries no status message");
                if code != 0 {
                    return Err(VenueError::Rejected {
                        code,
                        message: message.to_string(),
                    });
                }
                let mut ack = parse_order_ack(row, &req.client_order_id, reply.ack_ns)?;
                ack.sent_ns = reply.sent_ns;
                Ok(ack)
            })
            .collect()
    }

    /// Take the account's real quota from an acknowledgement's header.
    fn note_quota(
        limiter: &mut RollingRateLimiter,
        endpoint: &'static str,
        default: usize,
        seen: Option<usize>,
    ) {
        if limiter.declare(seen) {
            let paced = limiter.limit(default);
            tracing::info!(
                endpoint,
                venue_says = seen,
                pacing_to = paced,
                documented_default = default,
                "venue stated this account's request quota"
            );
        }
    }

    async fn reserve_create_capacity(&mut self, count: usize) {
        let waited =
            reserve_rate_capacity(&mut self.create_limiter, count, ORDER_CREATES_PER_SECOND).await;
        self.last_rate_wait_ns = Some(waited);
    }

    async fn inventory_positions(
        &self,
        category: &str,
        base_query: &str,
    ) -> Result<Vec<AccountPosition>, VenueError> {
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                base_query.to_string()
            } else {
                format!("{base_query}&cursor={}", percent_encode(&cursor))
            };
            let envelope = self.rest.get_signed(PATH_POSITIONS, &query).await?;
            let (rows, next) = parse_inventory_positions(&venue_result(envelope)?, category)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "{category} position inventory still had pages after {MAX_PAGES}"
        )))
    }

    async fn inventory_orders(
        &self,
        category: &str,
        base_query: &str,
    ) -> Result<Vec<AccountOrder>, VenueError> {
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                base_query.to_string()
            } else {
                format!("{base_query}&cursor={}", percent_encode(&cursor))
            };
            let envelope = self.rest.get_signed(PATH_ORDERS_OPEN, &query).await?;
            let (rows, next) = parse_inventory_orders(&venue_result(envelope)?, category)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "{category} order inventory still had pages after {MAX_PAGES}"
        )))
    }

    async fn linear_settle_coins(&self) -> Result<Vec<String>, VenueError> {
        let mut coins = vec!["USDT".to_string(), "USDC".to_string()];
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                "category=linear&limit=1000".to_string()
            } else {
                format!(
                    "category=linear&limit=1000&cursor={}",
                    percent_encode(&cursor)
                )
            };
            let envelope = self.rest.get_public(PATH_INSTRUMENTS, &query).await?;
            let (page, next) = parse_linear_settle_coins(&venue_result(envelope)?)?;
            for coin in page {
                if !coins.contains(&coin) {
                    coins.push(coin);
                }
            }
            if next.is_empty() {
                return Ok(coins);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "linear settlement-coin inventory still had pages after {MAX_PAGES}"
        )))
    }

    async fn inventory_spread_orders(&self) -> Result<Vec<AccountOrder>, VenueError> {
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                "limit=20".to_string()
            } else {
                format!("limit=20&cursor={}", percent_encode(&cursor))
            };
            let envelope = self
                .rest
                .get_signed(PATH_SPREAD_ORDERS_OPEN, &query)
                .await?;
            let (rows, next) = parse_spread_orders(&venue_result(envelope)?)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "spread order inventory still had pages after {MAX_PAGES}"
        )))
    }

    async fn inventory_rfq_state(&self) -> Result<Vec<AccountOrder>, VenueError> {
        // `quote`: our sent maker quotes, externally executable by their
        // inquirer. `request`: quotes on our inquiries, including PendingFill
        // after Execute Quote's asynchronous acceptance but before a position
        // appears. The inquiry list corroborates that lifecycle.
        let sent = self
            .rest
            .get_signed(PATH_RFQ_QUOTES_OPEN, "traderType=quote");
        let received = self
            .rest
            .get_signed(PATH_RFQ_QUOTES_OPEN, "traderType=request");
        let inquiries = self.rest.get_signed(PATH_RFQS_OPEN, "traderType=request");
        let (sent, (received, inquiries)) = futures_util::future::try_join(
            sent,
            futures_util::future::try_join(received, inquiries),
        )
        .await?;
        let sent = parse_rfq_quotes(&venue_result(sent)?, "quote")?;
        let received = parse_rfq_quotes(&venue_result(received)?, "request")?;
        let inquiries = parse_rfq_requests(&venue_result(inquiries)?)?;
        Ok(sent.into_iter().chain(received).chain(inquiries).collect())
    }

    async fn inventory_strategies_for_status(
        &self,
        status: i64,
    ) -> Result<Vec<AccountOrder>, VenueError> {
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                format!("status={status}&pageSize=50")
            } else {
                format!(
                    "status={status}&pageSize=50&cursor={}",
                    percent_encode(&cursor)
                )
            };
            let envelope = self.rest.get_signed(PATH_STRATEGIES, &query).await?;
            let (rows, next) = parse_active_strategies(&venue_result(envelope)?, status)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "venue strategy status {status} inventory still had pages after {MAX_PAGES}"
        )))
    }

    async fn inventory_strategies(&self) -> Result<Vec<AccountOrder>, VenueError> {
        // Query only exposure-capable states. Walking status-3 lifetime
        // history would make a mature flat account fail after the page cap.
        let pages = futures_util::future::try_join_all(
            [2_i64, 4, 5, 6]
                .into_iter()
                .map(|status| self.inventory_strategies_for_status(status)),
        )
        .await?;
        Ok(pages.into_iter().flatten().collect())
    }
}

#[engine_types::async_trait]
impl VenueGateway for BybitGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            // Bybit v5 linear perps: the position carries its own stop-loss
            // (POST /v5/position/trading-stop, and stopLoss on the entry).
            native_position_stop: true,
            // POST /v5/order/amend, by orderLinkId — see amend_order below.
            amend_in_place: true,
            // POST /v5/position/set-leverage; see set_leverage below.
            set_leverage: true,
            // qty=0 + reduceOnly + closeOnTrigger closes the whole linear
            // position even when its dust is below ordinary order minimums.
            close_position_below_minimum: true,
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        self.last_rate_wait_ns = None;
        self.require_one_way(req.symbol).await?;
        self.reserve_create_capacity(1).await;
        let reply = self.send_one(req).await;
        self.create_limiter.anchor_completion(Instant::now(), 1);
        reply
    }

    async fn send_orders(&mut self, reqs: &[OrderRequest]) -> Vec<Result<OrderAck, VenueError>> {
        // Cleared before any path that can return without reserving, so a
        // command refused here is never charged the previous one's wait.
        self.last_rate_wait_ns = None;
        if reqs.len() > ORDER_CREATES_PER_SECOND {
            return reqs
                .iter()
                .map(|_| {
                    Err(VenueError::BadRequest(format!(
                        "Bybit order batch has {} requests; maximum concurrent admission is {ORDER_CREATES_PER_SECOND}",
                        reqs.len()
                    )))
                })
                .collect();
        }
        for req in reqs {
            if let Err(error) = self.require_one_way(req.symbol).await {
                return reqs
                    .iter()
                    .map(|_| Err(VenueError::BadRequest(error.to_string())))
                    .collect();
            }
        }
        self.reserve_create_capacity(reqs.len()).await;
        if self.trade.is_some() {
            let replies = if reqs.len() == 1 {
                vec![self.send_one(&reqs[0]).await]
            } else if !trade_batch_preserves_stops(reqs) {
                let mut replies = Vec::with_capacity(reqs.len());
                for request in reqs {
                    replies.push(self.send_one(request).await);
                }
                replies
            } else {
                self.send_trade_batch(reqs).await
            };
            self.create_limiter
                .anchor_completion(Instant::now(), reqs.len());
            return replies;
        }
        // One-way positions have one position-level Full stop. Preserve each
        // symbol's submission order so later siblings cannot overtake the stop
        // state intended by earlier ones, while unrelated symbols still use
        // independent connections concurrently.
        let mut chains: HashMap<SymbolId, Vec<(usize, &OrderRequest)>> = HashMap::new();
        for (index, request) in reqs.iter().enumerate() {
            chains
                .entry(request.symbol)
                .or_default()
                .push((index, request));
        }
        let chains = chains.into_values().map(|chain| async {
            let mut replies = Vec::with_capacity(chain.len());
            for (index, request) in chain {
                let body = self.order_body(request);
                let reply = match body {
                    Ok(body) => self.send_one_rest(request, body).await,
                    Err(error) => Err(error),
                };
                replies.push((index, reply));
            }
            replies
        });
        let mut replies: Vec<_> = futures_util::future::join_all(chains)
            .await
            .into_iter()
            .flatten()
            .collect();
        self.create_limiter
            .anchor_completion(Instant::now(), reqs.len());
        replies.sort_unstable_by_key(|(index, _)| *index);
        replies.into_iter().map(|(_, reply)| reply).collect()
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        self.last_mutation_timing = None;
        self.last_rate_wait_ns = None;
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        body.insert("orderLinkId".into(), client_order_id.into());

        self.last_rate_wait_ns = Some(
            reserve_rate_capacity(&mut self.cancel_limiter, 1, ORDER_CANCELS_PER_SECOND).await,
        );
        if let Some(trade) = &mut self.trade {
            let reply = trade
                .request("order.cancel", vec![Value::Object(body)])
                .await;
            self.cancel_limiter.anchor_completion(Instant::now(), 1);
            let reply = reply?;
            Self::note_quota(
                &mut self.cancel_limiter,
                "order.cancel",
                ORDER_CANCELS_PER_SECOND,
                reply.quota_per_second,
            );
            self.last_mutation_timing = Some(VenueMutationTiming {
                sent_ns: reply.sent_ns,
                ack_ns: reply.ack_ns,
            });
            return Ok(());
        }
        let envelope = self
            .rest
            .post_signed(PATH_ORDER_CANCEL, &Value::Object(body))
            .await;
        self.cancel_limiter.anchor_completion(Instant::now(), 1);
        let envelope = envelope?;
        venue_result(envelope)?;
        Ok(())
    }

    async fn cancel_orders(
        &mut self,
        requests: &[(SymbolId, String)],
    ) -> Vec<Result<(), VenueError>> {
        self.last_mutation_timing = None;
        self.last_rate_wait_ns = None;
        if requests.is_empty() {
            return Vec::new();
        }
        if requests.len() > MAX_CANCEL_BATCH_ITEMS {
            return cancel_batch_error(
                requests.len(),
                VenueError::BadRequest(format!(
                    "Bybit cancel batch has {} requests; maximum is {MAX_CANCEL_BATCH_ITEMS}",
                    requests.len()
                )),
            );
        }

        let mut items = Vec::with_capacity(requests.len());
        let mut client_order_ids = Vec::with_capacity(requests.len());
        for (symbol, client_order_id) in requests {
            let symbol = match self.name_of(*symbol) {
                Ok(symbol) => symbol,
                Err(error) => return cancel_batch_error(requests.len(), error),
            };
            let mut item = Map::new();
            item.insert("symbol".into(), symbol.into());
            item.insert("orderLinkId".into(), client_order_id.as_str().into());
            items.push(Value::Object(item));
            client_order_ids.push(client_order_id.clone());
        }
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("request".into(), Value::Array(items));

        self.last_rate_wait_ns = Some(
            reserve_rate_capacity(
                &mut self.cancel_batch_limiter,
                requests.len(),
                ORDER_CANCEL_BATCHES_PER_SECOND,
            )
            .await,
        );
        if let Some(trade) = &mut self.trade {
            let reply = trade
                .request("order.cancel-batch", vec![Value::Object(body)])
                .await;
            self.cancel_batch_limiter
                .anchor_completion(Instant::now(), requests.len());
            let reply = match reply {
                Ok(reply) => reply,
                Err(error) => return cancel_batch_error(requests.len(), error),
            };
            Self::note_quota(
                &mut self.cancel_batch_limiter,
                "order.cancel-batch",
                ORDER_CANCEL_BATCHES_PER_SECOND,
                reply.quota_per_second,
            );
            self.last_mutation_timing = Some(VenueMutationTiming {
                sent_ns: reply.sent_ns,
                ack_ns: reply.ack_ns,
            });
            let statuses = reply
                .ret_ext_info
                .get("list")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            if statuses.len() != requests.len() {
                return cancel_batch_error(
                    requests.len(),
                    VenueError::BadReply(format!(
                        "WebSocket cancel batch returned {} statuses for {} orders",
                        statuses.len(),
                        requests.len()
                    )),
                );
            }
            return statuses
                .iter()
                .map(|status| {
                    let code = status.get("code").and_then(Value::as_i64).unwrap_or(-1);
                    if code == 0 {
                        Ok(())
                    } else {
                        Err(VenueError::Rejected {
                            code,
                            message: status
                                .get("msg")
                                .and_then(Value::as_str)
                                .unwrap_or("cancel batch item carries no message")
                                .to_string(),
                        })
                    }
                })
                .collect();
        }
        let envelope = self
            .rest
            .post_signed(PATH_ORDER_CANCEL_BATCH, &Value::Object(body))
            .await;
        self.cancel_batch_limiter
            .anchor_completion(Instant::now(), requests.len());
        let envelope = match envelope {
            Ok(envelope) => envelope,
            Err(error) => return cancel_batch_error(requests.len(), error),
        };
        let ret_ext_info = envelope.get("retExtInfo").cloned();
        let parsed = venue_result(envelope).and_then(|result| {
            let ret_ext_info = ret_ext_info.ok_or_else(|| {
                VenueError::BadReply("cancel-batch reply carries no retExtInfo".to_string())
            })?;
            parse_cancel_batch(&result, &ret_ext_info, &client_order_ids)
        });
        match parsed {
            Ok(replies) => replies,
            Err(error) => cancel_batch_error(requests.len(), error),
        }
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.last_mutation_timing = None;
        self.last_rate_wait_ns = None;
        if spec.px.is_none() && spec.qty.is_none() {
            return Err(VenueError::BadRequest(
                "an amend that changes neither price nor size".to_string(),
            ));
        }
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        body.insert("orderLinkId".into(), client_order_id.into());
        // Only what is changing. Bybit reads an absent field as "leave it",
        // and a price echoed back unchanged still costs the order its place
        // in the queue.
        if let Some(px) = spec.px {
            body.insert("price".into(), venue_num(px)?.into());
        }
        if let Some(qty) = spec.qty {
            body.insert("qty".into(), venue_num(qty)?.into());
        }

        self.last_rate_wait_ns =
            Some(reserve_rate_capacity(&mut self.amend_limiter, 1, ORDER_AMENDS_PER_SECOND).await);
        if let Some(trade) = &mut self.trade {
            let reply = trade
                .request("order.amend", vec![Value::Object(body)])
                .await;
            self.amend_limiter.anchor_completion(Instant::now(), 1);
            let reply = reply?;
            Self::note_quota(
                &mut self.amend_limiter,
                "order.amend",
                ORDER_AMENDS_PER_SECOND,
                reply.quota_per_second,
            );
            self.last_mutation_timing = Some(VenueMutationTiming {
                sent_ns: reply.sent_ns,
                ack_ns: reply.ack_ns,
            });
            return Ok(());
        }
        let envelope = self
            .rest
            .post_signed(PATH_ORDER_AMEND, &Value::Object(body))
            .await;
        self.amend_limiter.anchor_completion(Instant::now(), 1);
        let envelope = envelope?;
        venue_result(envelope)?;
        Ok(())
    }

    fn take_mutation_timing(&mut self) -> Option<VenueMutationTiming> {
        self.last_mutation_timing.take()
    }

    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        self.last_rate_wait_ns.take()
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        self.require_one_way(symbol).await?;
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        body.insert("stopLoss".into(), venue_num(trigger_px)?.into());
        // Full: the stop closes the whole position. positionIdx 0 is one-way
        // mode, which is how the demo account is configured.
        body.insert("tpslMode".into(), "Full".into());
        body.insert("positionIdx".into(), 0.into());
        body.insert("slTriggerBy".into(), "MarkPrice".into());
        body.insert("slOrderType".into(), "Market".into());

        let stop_limit = match self.realm {
            VenueRealm::Demo => TRADING_STOPS_DEMO_PER_SECOND,
            VenueRealm::Mainnet => TRADING_STOPS_MAINNET_PER_SECOND,
        };
        reserve_rate_capacity(&mut self.stop_limiter, 1, stop_limit).await;
        let envelope = self
            .rest
            .post_signed(PATH_TRADING_STOP, &Value::Object(body))
            .await;
        self.stop_limiter.anchor_completion(Instant::now(), 1);
        let envelope = envelope?;
        venue_result(envelope)?;
        Ok(())
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        Some(BybitGateway::add_symbol(self, symbol))
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        self.require_one_way(symbol).await?;
        let text = venue_num(leverage)?;
        let mut body = Map::new();
        body.insert("category".into(), CATEGORY.into());
        body.insert("symbol".into(), self.name_of(symbol)?.into());
        // Both sides, always the same number. One-way position mode still
        // carries two, and a venue that has them different would post
        // different margin depending on which way a position went.
        body.insert("buyLeverage".into(), text.clone().into());
        body.insert("sellLeverage".into(), text.into());

        reserve_rate_capacity(&mut self.leverage_limiter, 1, LEVERAGE_CHANGES_PER_SECOND).await;
        let envelope = self
            .rest
            .post_signed(PATH_SET_LEVERAGE, &Value::Object(body))
            .await;
        self.leverage_limiter.anchor_completion(Instant::now(), 1);
        let envelope = envelope?;
        match venue_result(envelope) {
            Ok(_) => Ok(()),
            // 110043 is "leverage not modified": the symbol already sits at
            // this number. Bybit reports it as an error and it is not one —
            // the request asked for a state, and the state is what was asked
            // for. Treating it as a failure would block every repeat entry on
            // a symbol whose leverage is already right, which is most of them.
            Err(VenueError::Rejected { code: 110043, .. }) => Ok(()),
            Err(other) => Err(other),
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        self.warm().await?;
        let envelope = self.rest.get_signed(PATH_QUERY_API, "").await?;
        let result = venue_result(envelope)?;

        // The venue is asked which key it just authenticated, and the answer
        // has to be the key we signed with. Without this the reply could be
        // about some other account and we would take out a lock in its name
        // while trading this one. The key is not a secret — it rides in a
        // header on every signed request — so a plain comparison is enough;
        // what matters is that the comparison happens at all.
        let reported = result
            .get("apiKey")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or_default();
        if reported.is_empty() || reported != self.rest.api_key() {
            return Err(VenueError::Credentials(
                "the venue says this reply is about a different API key than the one it was \
                 signed with"
                    .to_string(),
            ));
        }
        if self.realm == VenueRealm::Mainnet {
            if self.attestation_key {
                let expected_ip = std::env::var("BYBIT_ATTEST_API_KEY_IP").map_err(|_| {
                    VenueError::Credentials(
                        "BYBIT_ATTEST_API_KEY_IP is required for the read-only attestation key allowlist"
                            .to_string(),
                    )
                })?;
                verify_attestation_key(&result, &expected_ip)?;
            } else {
                let expected_ip = std::env::var("BYBIT_REAL_API_KEY_IP").map_err(|_| {
                    VenueError::Credentials(
                        "BYBIT_REAL_API_KEY_IP is required for the funded key allowlist"
                            .to_string(),
                    )
                })?;
                let backup_ip = std::env::var("BYBIT_REAL_API_KEY_BACKUP_IP")
                    .ok()
                    .filter(|value| !value.trim().is_empty());
                verify_funded_key(&result, &expected_ip, backup_ip.as_deref())?;
            }
        }

        // Bybit sends userID as a number here and as a string elsewhere; both
        // are the same account and both are read.
        let raw = match result.get("userID") {
            Some(Value::String(text)) => text.clone(),
            Some(Value::Number(number)) => number.to_string(),
            _ => String::new(),
        };
        let user_id = crate::lease::account_id_text(&raw).ok_or_else(|| {
            VenueError::BadReply(format!(
                "the venue gave no usable account number ({raw:?}), so there is nothing to name \
                 the single-writer lock after"
            ))
        })?;
        if self.realm == VenueRealm::Mainnet {
            let exclusive = std::env::var("BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID")
                .map_err(|_| {
                    VenueError::Credentials(
                        "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID is required: the funded UID must be dedicated to this engine with no manual trading, venue bots, or other trading keys"
                            .to_string(),
                    )
                })?;
            if exclusive.trim() != user_id {
                return Err(VenueError::Credentials(format!(
                    "funded credentials answer as UID {user_id}, but BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID acknowledges {:?}",
                    exclusive.trim()
                )));
            }
        }

        let identity = AccountIdentity {
            venue: crate::lease::VENUE_BYBIT.to_string(),
            user_id,
            // Which realm this gateway was built for, not a reading and not
            // a guess. Both halves that read a realm depend on it: the
            // mainnet producers check it against their own environment, and
            // the single-writer lease is named by it.
            realm: realm_name(self.realm).to_string(),
        };
        // A settle-coin read hides flat symbols, while those are exactly the
        // symbols whose next order still depends on the account's mode.
        self.require_configured_symbols_one_way().await?;
        Ok(identity)
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        // Stamp the beginning of the scan. A private fill received while the
        // REST requests are in flight is not proven present in their snapshot
        // and must remain in the risk kernel's recent-fill overlay.
        let observed_ns = mono_ns();
        // Wallet and positions are separate endpoints, so this is two round
        // trips however it is written — they at least go out together.
        let wallet = self.rest.get_signed(PATH_WALLET, "accountType=UNIFIED");
        let positions = self
            .rest
            .get_signed(PATH_POSITIONS, "category=linear&settleCoin=USDT&limit=200");
        let (wallet, positions) = futures_util::future::try_join(wallet, positions).await?;
        let (equity_usdt, available_usdt) = parse_wallet(&venue_result(wallet)?)?;

        let ids = &self.ids;
        let resolve = |name: &str| ids.get(name).copied();
        let (mut open, mut cursor) = parse_positions(&venue_result(positions)?, &resolve)?;
        let mut pages = 1;
        while !cursor.is_empty() && pages < MAX_PAGES {
            let query = format!(
                "category=linear&settleCoin=USDT&limit=200&cursor={}",
                percent_encode(&cursor)
            );
            let more = self.rest.get_signed(PATH_POSITIONS, &query).await?;
            let (rows, next) = parse_positions(&venue_result(more)?, &resolve)?;
            open.extend(rows);
            cursor = next;
            pages += 1;
        }
        // A truncated position list under-counts exposure and can hide an
        // unprotected position — this read fails closed, like instruments.
        if !cursor.is_empty() {
            return Err(VenueError::BadReply(format!(
                "position listing still had pages after {MAX_PAGES}"
            )));
        }

        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions: open,
            observed_ns,
        })
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        // `settleCoin` rather than a symbol list: the point of this read is to
        // find orders nobody here placed, and asking only about the symbols
        // the engine knows would hide exactly those.
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                format!("category={CATEGORY}&settleCoin=USDT&limit=50")
            } else {
                format!(
                    "category={CATEGORY}&settleCoin=USDT&limit=50&cursor={}",
                    percent_encode(&cursor)
                )
            };
            let envelope = self.rest.get_signed(PATH_ORDERS_OPEN, &query).await?;
            let (rows, next) = parse_working_orders(&venue_result(envelope)?)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        // A truncated list would say "nobody else is here" when somebody is,
        // which is the one answer this read must never give by accident.
        Err(VenueError::BadReply(format!(
            "working-order listing still had pages after {MAX_PAGES}"
        )))
    }

    async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        // Stamp the beginning, not the end. The caller's freshness bound then
        // rejects a scan whose first page has gone stale while later pages
        // were still arriving.
        let observed_ms = wall_ms();
        // Discover every settlement surface the venue currently advertises,
        // while retaining USDT/USDC even if an entire class is temporarily
        // empty or delisted. A new settlement coin therefore becomes part of
        // the proof without an engine release.
        let settle_coins = self.linear_settle_coins().await?;
        let position_queries: Vec<String> = settle_coins
            .iter()
            .map(|coin| {
                format!(
                    "category=linear&settleCoin={}&limit=200",
                    percent_encode(coin)
                )
            })
            .collect();
        let order_queries: Vec<String> = settle_coins
            .iter()
            .map(|coin| {
                format!(
                    "category=linear&settleCoin={}&openOnly=0&limit=50",
                    percent_encode(coin)
                )
            })
            .collect();

        let wallet = async {
            let envelope = self
                .rest
                .get_signed(PATH_WALLET, "accountType=UNIFIED")
                .await?;
            parse_inventory_wallet(&venue_result(envelope)?)
        };
        let asset_overview = async {
            if self.realm != VenueRealm::Mainnet {
                return Ok::<_, VenueError>((Vec::new(), Vec::new()));
            }
            let envelope = self.rest.get_signed(PATH_ASSET_OVERVIEW, "").await?;
            parse_asset_overview(&venue_result(envelope)?, wall_ms())
        };
        let positions = async {
            let linear = futures_util::future::try_join_all(
                position_queries
                    .iter()
                    .map(|query| self.inventory_positions("linear", query)),
            );
            let other = futures_util::future::try_join_all([
                self.inventory_positions("inverse", "category=inverse&limit=200"),
                self.inventory_positions("option", "category=option&limit=200"),
            ]);
            let (linear, other) = futures_util::future::try_join(linear, other).await?;
            Ok::<_, VenueError>(
                linear
                    .into_iter()
                    .chain(other)
                    .flatten()
                    .collect::<Vec<_>>(),
            )
        };
        let orders = async {
            let linear = futures_util::future::try_join_all(
                order_queries
                    .iter()
                    .map(|query| self.inventory_orders("linear", query)),
            );
            let other = futures_util::future::try_join_all([
                self.inventory_orders("inverse", "category=inverse&openOnly=0&limit=50"),
                self.inventory_orders("option", "category=option&openOnly=0&limit=50"),
                self.inventory_orders("spot", "category=spot&openOnly=0&limit=50"),
            ]);
            let extended = async {
                if self.realm == VenueRealm::Mainnet {
                    let (spread, (rfq, strategies)) = futures_util::future::try_join(
                        self.inventory_spread_orders(),
                        futures_util::future::try_join(
                            self.inventory_rfq_state(),
                            self.inventory_strategies(),
                        ),
                    )
                    .await?;
                    Ok::<_, VenueError>(
                        spread
                            .into_iter()
                            .chain(rfq)
                            .chain(strategies)
                            .collect::<Vec<_>>(),
                    )
                } else {
                    // Bybit Demo publishes neither spread nor RFQ APIs and
                    // cannot create those products. Calling the endpoints
                    // would make every demo attestation fail for an
                    // unsupported surface rather than prove anything.
                    Ok(Vec::new())
                }
            };
            let (linear, (other, extended)) = futures_util::future::try_join(
                linear,
                futures_util::future::try_join(other, extended),
            )
            .await?;
            Ok::<_, VenueError>(
                linear
                    .into_iter()
                    .chain(other)
                    .flatten()
                    .chain(extended)
                    .collect::<Vec<_>>(),
            )
        };
        let account_assets = async {
            let (wallet_positions, (overview_positions, overview_orders)) =
                futures_util::future::try_join(wallet, asset_overview).await?;
            Ok::<_, VenueError>((wallet_positions, overview_positions, overview_orders))
        };
        let ((wallet_positions, overview_positions, overview_orders), (positions, mut open_orders)) =
            futures_util::future::try_join(
                account_assets,
                futures_util::future::try_join(positions, orders),
            )
            .await?;
        open_orders.extend(overview_orders);
        let extended_scope = if self.realm == VenueRealm::Mainnet {
            "; spread open orders, both RFQ quote roles/inquiries, venue-native TWAP/chase/iceberg/POV strategies, and cross-account TradingBot/CopyTrading assets"
        } else {
            "; spread and RFQ unsupported by Bybit Demo"
        };
        Ok(AccountInventory {
            scope: format!(
                "credential account: linear settlements [{}], inverse and option positions; non-cash wallet assets and liabilities; linear, inverse, option and spot open orders{}",
                settle_coins.join(","), extended_scope
            ),
            positions: positions
                .into_iter()
                .chain(wallet_positions)
                .chain(overview_positions)
                .collect(),
            open_orders,
            observed_ms,
        })
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        // The venue caps one query's window at 7 days, so a longer ask walks
        // in slices. `settleCoin` rather than a symbol, for the same reason
        // as working_orders: this read exists to find what the log missed,
        // and asking only about known symbols would hide exactly that.
        const SLICE_MS: i64 = 6 * 86_400_000;
        let mut out = Vec::new();
        let mut from = start_ms;
        while from < end_ms {
            let to = (from + SLICE_MS).min(end_ms);
            let mut cursor = String::new();
            let mut pages = 0;
            loop {
                if pages >= MAX_PAGES {
                    // A truncated history would quietly leave fills missing,
                    // which is the one answer this read must never give.
                    return Err(VenueError::BadReply(format!(
                        "execution listing still had pages after {MAX_PAGES}"
                    )));
                }
                let query = if cursor.is_empty() {
                    format!(
                        "category={CATEGORY}&settleCoin=USDT&startTime={from}&endTime={to}&limit=100"
                    )
                } else {
                    format!(
                        "category={CATEGORY}&settleCoin=USDT&startTime={from}&endTime={to}\
                         &limit=100&cursor={}",
                        percent_encode(&cursor)
                    )
                };
                let envelope = self.rest.get_signed(PATH_EXECUTIONS, &query).await?;
                let (rows, next) = parse_executions(&venue_result(envelope)?)?;
                out.extend(rows);
                if next.is_empty() {
                    break;
                }
                cursor = next;
                pages += 1;
            }
            from = to;
        }
        Ok(out)
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        let mut out = Vec::new();
        let mut cursor = String::new();
        for _ in 0..MAX_PAGES {
            let query = if cursor.is_empty() {
                format!("category={CATEGORY}&limit=1000")
            } else {
                format!(
                    "category={CATEGORY}&limit=1000&cursor={}",
                    percent_encode(&cursor)
                )
            };
            // Public listing: unsigned.
            let envelope = self.rest.get_public(PATH_INSTRUMENTS, &query).await?;
            let (rows, next) = parse_instruments(&venue_result(envelope)?)?;
            out.extend(rows);
            if next.is_empty() {
                return Ok(out);
            }
            cursor = next;
        }
        Err(VenueError::BadReply(format!(
            "instrument listing still had pages after {MAX_PAGES}"
        )))
    }
}

fn trade_batch_preserves_stops(reqs: &[OrderRequest]) -> bool {
    let mut stopped_openings = HashSet::new();
    for request in reqs {
        if request.stop.is_some()
            && !request.reduce_only
            && !stopped_openings.insert(request.symbol)
        {
            return false;
        }
    }
    true
}

fn validate_server_clock(
    envelope: &Value,
    sent_ms: i64,
    received_ms: i64,
) -> Result<(), VenueError> {
    let server_ms = match envelope.get("time") {
        Some(Value::Number(value)) => value.as_i64(),
        Some(Value::String(value)) => value.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| {
        VenueError::BadReply("time reply has no millisecond server clock".to_string())
    })?;
    let recv_window_ms = RECV_WINDOW_MS
        .parse::<i64>()
        .expect("Bybit receive window is a decimal integer");
    if received_ms < sent_ms {
        return Err(VenueError::Credentials(
            "host wall clock moved backwards during Bybit's clock check".to_string(),
        ));
    }
    // The server stamps its reply between our send and receive. Comparing it
    // with one edge mistakes network delay for clock skew; the midpoint is
    // the standard bounded estimate and catches an ahead clock that a slow
    // outbound request can otherwise hide.
    let local_ms = sent_ms.saturating_add(received_ms.saturating_sub(sent_ms) / 2);

    if local_ms >= server_ms.saturating_add(1_000)
        || server_ms.saturating_sub(local_ms) > recv_window_ms
    {
        return Err(VenueError::Credentials(format!(
            "host clock is outside Bybit's signing window: local_midpoint={local_ms}, server={server_ms}, sent={sent_ms}, received={received_ms}"
        )));
    }
    Ok(())
}

fn side_str(side: Side) -> &'static str {
    match side {
        Side::Buy => "Buy",
        Side::Sell => "Sell",
    }
}

fn tif_str(tif: TimeInForce) -> &'static str {
    match tif {
        TimeInForce::Gtc => "GTC",
        TimeInForce::Ioc => "IOC",
        TimeInForce::PostOnly => "PostOnly",
    }
}

/// The realm's name as everything outside this crate spells it: the lease
/// file, the heartbeat, and the environment the target producers check their
/// own against.
///
/// It is the realm the gateway was built for and never a fixed spelling: the
/// mainnet producers compare it against their own environment and block every
/// entry when it disagrees, and the single-writer lease is named by it, so a
/// funded engine reporting `demo` would take its lease in the wrong realm's
/// name.
fn realm_name(realm: VenueRealm) -> &'static str {
    match realm {
        VenueRealm::Demo => crate::lease::REALM_DEMO,
        VenueRealm::Mainnet => crate::lease::REALM_MAINNET,
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn each_realm_reports_its_own_name_and_never_the_other() {
        // Found on the funded account's first shadow run: the engine
        // authenticated as 552445993 and published `realm: "demo"` beside it.
        // The mainnet producers compare that against their own environment and
        // would have blocked every entry; the single-writer lease is named by
        // it, so a live funded engine would have taken a demo-realm lease.
        assert_eq!(realm_name(VenueRealm::Demo), "demo");
        assert_eq!(realm_name(VenueRealm::Mainnet), "mainnet");
        assert_ne!(
            realm_name(VenueRealm::Mainnet),
            realm_name(VenueRealm::Demo),
            "one name for both realms is the bug this exists to stop"
        );
    }

    use super::*;

    #[test]
    fn sides_and_time_in_force_use_the_venue_spelling() {
        assert_eq!(side_str(Side::Buy), "Buy");
        assert_eq!(side_str(Side::Sell), "Sell");
        assert_eq!(tif_str(TimeInForce::Gtc), "GTC");
        assert_eq!(tif_str(TimeInForce::Ioc), "IOC");
        assert_eq!(tif_str(TimeInForce::PostOnly), "PostOnly");
    }

    #[tokio::test]
    async fn the_wait_reported_is_the_wait_that_was_actually_paid() {
        // The mark separates pacing this engine chose from latency the venue
        // imposed, so it has to be the real figure: zero when nothing was
        // held back, and the window when something was.
        let mut limiter = RollingRateLimiter::default();
        let straight_through = reserve_rate_capacity(&mut limiter, 2, 2).await;
        assert!(
            straight_through < 1_000_000,
            "an unblocked reservation reported {straight_through}ns of waiting"
        );
        let held = reserve_rate_capacity(&mut limiter, 2, 2).await;
        assert!(
            held >= RATE_LIMIT_WINDOW.as_nanos() as u64,
            "a reservation that waited out the window reported {held}ns"
        );
    }

    #[tokio::test]
    async fn a_command_that_never_reserved_carries_no_earlier_commands_wait() {
        // Taking the mark clears it, and a send that is refused before it
        // reserves clears it too. Otherwise a batch rejected on its size would
        // be charged whatever the last real command was held back by.
        let creds = VenueRealm::Demo.credentials_for_test("key", "secret");
        let mut gateway = BybitGateway::for_test(
            "http://127.0.0.1:1",
            VenueRealm::Demo,
            creds,
            vec!["BTCUSDT".to_string()],
        );
        gateway.last_rate_wait_ns = Some(900_000_000);

        let oversized: Vec<OrderRequest> = (0..ORDER_CREATES_PER_SECOND + 1)
            .map(|i| stopped_order(&format!("eng-{i}"), 0))
            .collect();
        let replies = VenueGateway::send_orders(&mut gateway, &oversized).await;
        assert_eq!(replies.len(), oversized.len());
        assert!(
            replies.iter().all(Result::is_err),
            "an oversized batch was sent"
        );
        assert_eq!(
            VenueGateway::take_rate_wait_ns(&mut gateway),
            None,
            "a refused batch was charged the previous command's wait"
        );
    }

    #[test]
    fn a_stated_quota_larger_than_the_default_is_what_the_pacing_uses() {
        // The point of reading the header at all. An account granted a
        // market-maker tier is paced at the documented default forever unless
        // the venue's own figure is taken and used.
        let mut limiter = RollingRateLimiter::default();
        assert_eq!(limiter.limit(ORDER_CREATES_PER_SECOND), 10);
        assert!(limiter.declare(Some(20)));
        assert_eq!(limiter.limit(ORDER_CREATES_PER_SECOND), 20);

        // Twenty admissions now fit in the window that used to hold ten.
        let now = Instant::now();
        for _ in 0..20 {
            limiter
                .try_reserve(now, 1, limiter.limit(ORDER_CREATES_PER_SECOND))
                .expect("inside the stated quota");
        }
        assert!(limiter
            .try_reserve(now, 1, limiter.limit(ORDER_CREATES_PER_SECOND))
            .is_err());
    }

    #[test]
    fn a_stated_quota_below_the_default_is_recorded_and_not_obeyed() {
        // Raise-only. Every batch this adapter sends is capped at the
        // documented default, so pacing below it would leave an already
        // admitted batch unable to reserve at all — a wait with no end
        // rather than a slower one.
        let mut limiter = RollingRateLimiter::default();
        assert!(limiter.declare(Some(3)));
        assert_eq!(limiter.declared, Some(3));
        assert_eq!(limiter.limit(ORDER_CREATES_PER_SECOND), 10);
        assert_eq!(
            limiter.limit(MAX_CANCEL_BATCH_ITEMS),
            MAX_CANCEL_BATCH_ITEMS
        );
    }

    #[test]
    fn an_absent_or_repeated_quota_is_not_news() {
        // The log line hangs off this, and a line per acknowledgement would
        // be one per order.
        let mut limiter = RollingRateLimiter::default();
        assert!(!limiter.declare(None));
        assert_eq!(limiter.declared, None);
        assert!(limiter.declare(Some(20)));
        assert!(!limiter.declare(Some(20)));
        assert!(!limiter.declare(None), "silence does not retract a figure");
        assert_eq!(limiter.declared, Some(20));
        assert!(limiter.declare(Some(40)), "a raised tier is news");
    }

    fn stopped_order(id: &str, symbol: u16) -> OrderRequest {
        OrderRequest {
            client_order_id: id.to_string(),
            strategy: engine_types::StrategyId(0),
            symbol: SymbolId(symbol),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Limit {
                px: 10.0,
                tif: TimeInForce::PostOnly,
            },
            stop: Some(engine_types::StopSpec { trigger_px: 8.0 }),
            reduce_only: false,
            close_position: false,
        }
    }

    #[test]
    fn two_opening_stops_on_one_symbol_are_not_sent_as_one_batch() {
        let first = stopped_order("bid", 0);
        let mut second = stopped_order("ask", 0);
        second.side = Side::Sell;
        assert!(!trade_batch_preserves_stops(&[first.clone(), second]));
        assert!(trade_batch_preserves_stops(&[
            first,
            stopped_order("other", 1),
        ]));
    }

    #[test]
    fn clock_check_enforces_the_venue_signing_window() {
        let reply = |time| serde_json::json!({"time": time});
        assert!(validate_server_clock(&reply(10_000), 9_999, 10_001).is_ok());
        assert!(validate_server_clock(&reply(10_000), 10_999, 11_001).is_err());
        assert!(validate_server_clock(&reply(10_000), 4_998, 5_000).is_err());
        assert!(
            validate_server_clock(&reply(13_000), 12_000, 18_000).is_err(),
            "outbound delay must not hide a host clock that is two seconds ahead"
        );
        assert!(validate_server_clock(&reply(13_000), 10_000, 16_000).is_ok());
        assert!(validate_server_clock(&reply(10_000), 10_001, 10_000).is_err());
    }

    #[test]
    fn create_limit_is_a_rolling_window_not_a_local_bucket() {
        let mut limiter = RollingRateLimiter::default();
        let epoch = Instant::now();
        let first = epoch + Duration::from_millis(900);
        limiter
            .try_reserve(first, 10, ORDER_CREATES_PER_SECOND)
            .expect("first ten");

        let boundary_burst = epoch + Duration::from_millis(1_100);
        assert_eq!(
            limiter.try_reserve(boundary_burst, 10, ORDER_CREATES_PER_SECOND),
            Err(Duration::from_millis(801)),
            "a fixed local-second bucket would incorrectly release this batch"
        );
        assert!(limiter
            .try_reserve(
                epoch + Duration::from_millis(1_900),
                10,
                ORDER_CREATES_PER_SECOND
            )
            .is_err());
        limiter
            .try_reserve(
                epoch + Duration::from_millis(1_901),
                10,
                ORDER_CREATES_PER_SECOND,
            )
            .expect("the prior rolling window plus guard has elapsed");
    }

    #[test]
    fn rolling_window_starts_no_earlier_than_response_completion() {
        let mut limiter = RollingRateLimiter::default();
        let epoch = Instant::now();
        limiter
            .try_reserve(
                epoch + Duration::from_millis(900),
                10,
                ORDER_CREATES_PER_SECOND,
            )
            .expect("first ten");
        limiter.anchor_completion(epoch + Duration::from_millis(1_200), 10);

        assert!(limiter
            .try_reserve(
                epoch + Duration::from_millis(2_200),
                10,
                ORDER_CREATES_PER_SECOND,
            )
            .is_err());
        limiter
            .try_reserve(
                epoch + Duration::from_millis(2_201),
                10,
                ORDER_CREATES_PER_SECOND,
            )
            .expect("completion anchor plus guard elapsed");
    }

    #[test]
    fn cancel_and_cancel_batch_have_independent_rolling_windows() {
        let epoch = Instant::now();
        let first = epoch + Duration::from_millis(900);
        let boundary_burst = epoch + Duration::from_millis(1_100);
        let mut individual = RollingRateLimiter::default();
        let mut batch = RollingRateLimiter::default();

        individual
            .try_reserve(first, 10, ORDER_CANCELS_PER_SECOND)
            .expect("ten individual cancels");
        assert!(individual
            .try_reserve(boundary_burst, 1, ORDER_CANCELS_PER_SECOND)
            .is_err());
        batch
            .try_reserve(boundary_burst, 10, ORDER_CANCEL_BATCHES_PER_SECOND)
            .expect("batch cancels have a separate endpoint window");
        assert!(batch
            .try_reserve(boundary_burst, 1, ORDER_CANCEL_BATCHES_PER_SECOND)
            .is_err());
    }

    #[test]
    fn every_position_mutation_has_its_own_exact_rolling_window() {
        let at = Instant::now();
        for limit in [
            ORDER_AMENDS_PER_SECOND,
            TRADING_STOPS_MAINNET_PER_SECOND,
            TRADING_STOPS_DEMO_PER_SECOND,
            LEVERAGE_CHANGES_PER_SECOND,
        ] {
            let mut limiter = RollingRateLimiter::default();
            limiter
                .try_reserve(at, limit, limit)
                .expect("the documented window is admitted");
            assert_eq!(
                limiter.try_reserve(at, 1, limit),
                Err(RATE_LIMIT_WINDOW + RATE_LIMIT_GUARD),
                "request N+1 must wait for the exact endpoint window"
            );
        }
    }

    #[test]
    fn position_mode_reads_are_bounded_in_fifty_request_waves() {
        let at = Instant::now();
        let mut limiter = RollingRateLimiter::default();
        limiter
            .try_reserve(at, POSITION_READS_PER_SECOND, POSITION_READS_PER_SECOND)
            .expect("one full startup wave");
        assert!(limiter
            .try_reserve(at, 1, POSITION_READS_PER_SECOND)
            .is_err());
    }

    #[test]
    fn an_unknown_symbol_id_cannot_become_a_request() {
        let gw = BybitGateway::for_test(
            "http://127.0.0.1:1",
            VenueRealm::Demo,
            VenueRealm::Demo.credentials_for_test("k", "s"),
            vec!["BTCUSDT".to_string()],
        );
        assert!(gw.name_of(SymbolId(0)).is_ok());
        assert!(gw.name_of(SymbolId(7)).is_err());
    }

    #[test]
    fn added_symbols_keep_their_position_as_the_id() {
        let mut gw = BybitGateway::for_test(
            "http://127.0.0.1:1",
            VenueRealm::Demo,
            VenueRealm::Demo.credentials_for_test("k", "s"),
            vec!["BTCUSDT".to_string()],
        );
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.add_symbol("ETHUSDT"), SymbolId(1));
        assert_eq!(gw.name_of(SymbolId(1)).unwrap(), "ETHUSDT");
    }
}
