//! One bounded order lifecycle on the Bybit practice account.
//!
//! This is an operator proof, not a strategy. It can only select the compiled
//! demo realm, takes the fleet's account lease, proves the exact account id,
//! rests one minimum-value post-only order away from the touch, cancels it,
//! and reads the account twice before letting go. Any fill is closed in full
//! and makes the command fail after cleanup.

use std::error::Error;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use engine_types::quantize::{quantize_px, round_clean, steps};
use engine_types::{
    AccountInventory, Feed, FeedError, InstrumentRule, MarketEvent, MarketFeed, OrderAck,
    OrderFeed, OrderKind, OrderRequest, OrderUpdate, Quote, Side, StopSpec, StrategyId,
    Subscription, SymbolId, TimeInForce, VenueError, VenueExecution, VenueGateway, VenueOrder,
};
use engine_venue::lease;
use engine_venue::{BybitOrderReceipt, Venue, VenueName};

use crate::{assembly, config};

const LEASE_ROLE: &str = "canary-order";
const QUOTE_TIMEOUT: Duration = Duration::from_secs(20);
const PRIVATE_UPDATE_TIMEOUT: Duration = Duration::from_secs(10);
const CLEAN_SCAN_PAUSE: Duration = Duration::from_secs(1);
const CLEAN_SCAN_ATTEMPTS: usize = 30;
const MIN_CLEAN_OBSERVATION: Duration = Duration::from_secs(5);
const MAX_QUOTE_AGE_MS: i64 = 30_000;
const MAX_INVENTORY_AGE_MS: i64 = 30_000;

#[derive(Copy, Clone)]
struct Timings {
    private_update_timeout: Duration,
    clean_scan_pause: Duration,
    clean_scan_attempts: usize,
    min_clean_observation: Duration,
}

impl Default for Timings {
    fn default() -> Self {
        Self {
            private_update_timeout: PRIVATE_UPDATE_TIMEOUT,
            clean_scan_pause: CLEAN_SCAN_PAUSE,
            clean_scan_attempts: CLEAN_SCAN_ATTEMPTS,
            min_clean_observation: MIN_CLEAN_OBSERVATION,
        }
    }
}

#[derive(Clone, Debug)]
struct CanaryPlan {
    request: OrderRequest,
    close_id: String,
    bid_px: f64,
    ask_px: f64,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum PrivateSignal {
    New,
    Cancelled,
    Filled,
}

#[derive(Debug)]
struct CleanupOutcome {
    original_filled: bool,
    close_attempted: bool,
    original_terminal: bool,
    original_cancelled: bool,
}

#[allow(async_fn_in_trait)]
trait CanaryGateway {
    async fn send(&mut self, request: &OrderRequest) -> Result<OrderAck, VenueError>;
    async fn cancel(&mut self, symbol: SymbolId, client_id: &str) -> Result<(), VenueError>;
    async fn working(&mut self) -> Result<Vec<VenueOrder>, VenueError>;
    async fn inventory(&mut self) -> Result<AccountInventory, VenueError>;
    async fn executions_between(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError>;
    async fn venue_time(&mut self) -> Result<i64, VenueError>;
    async fn receipt(
        &mut self,
        symbol: SymbolId,
        client_id: &str,
    ) -> Result<Option<BybitOrderReceipt>, VenueError>;
}

impl CanaryGateway for Venue {
    async fn send(&mut self, request: &OrderRequest) -> Result<OrderAck, VenueError> {
        VenueGateway::send_order(self, request).await
    }

    async fn cancel(&mut self, symbol: SymbolId, client_id: &str) -> Result<(), VenueError> {
        VenueGateway::cancel_order(self, symbol, client_id).await
    }

    async fn working(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        VenueGateway::working_orders(self).await
    }

    async fn inventory(&mut self) -> Result<AccountInventory, VenueError> {
        VenueGateway::account_inventory(self).await
    }

    async fn executions_between(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        VenueGateway::executions(self, start_ms, end_ms).await
    }

    async fn venue_time(&mut self) -> Result<i64, VenueError> {
        match self {
            Venue::Bybit(gateway) => gateway.venue_time_ms().await,
            _ => Err(VenueError::BadRequest(
                "canary venue-time reads exist only for Bybit".to_string(),
            )),
        }
    }

    async fn receipt(
        &mut self,
        symbol: SymbolId,
        client_id: &str,
    ) -> Result<Option<BybitOrderReceipt>, VenueError> {
        match self {
            Venue::Bybit(gateway) => gateway.order_receipt(symbol, client_id).await,
            _ => Err(VenueError::BadRequest(
                "canary order receipts exist only for Bybit".to_string(),
            )),
        }
    }
}

/// Submit and cancel one demo order. `--execute` and the expected account id
/// are required by the CLI before this reaches credentials or a socket.
pub async fn run(
    config_path: &Path,
    symbol: &str,
    expected_user_id: &str,
    execute: bool,
) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let chosen = assembly::venue_name(&loaded.config.engine.venue)?;
    require_demo(chosen)?;
    if !execute {
        return Err(
            "canary-order changes venue state; inspect the command, then add --execute"
                .to_string()
                .into(),
        );
    }

    let symbol = normalized_symbol(symbol)?;
    let expected_user_id = expected_user_id.trim();
    if expected_user_id.is_empty() {
        return Err(
            "canary-order needs --expected-user-id from an authenticated venue reply"
                .to_string()
                .into(),
        );
    }

    let symbols = vec![symbol.clone()];
    let mut venue = assembly::venue(chosen, symbols.clone())?;
    let mut order_feed = assembly::order_feed(chosen, symbols)?;
    let subscription = Subscription {
        symbol: symbol.clone(),
        feed: Feed::Quote,
    };
    let mut market_feed = assembly::market_feed(chosen, &[subscription]);

    let before = VenueGateway::account_identity(&mut venue).await?;
    if before.user_id != expected_user_id {
        return Err(format!(
            "demo credentials reach user {}, not --expected-user-id {}",
            before.user_id, expected_user_id
        )
        .into());
    }
    if before.venue != chosen.venue() || before.realm != chosen.realm() {
        return Err(format!(
            "demo identity says venue={} realm={}, expected venue={} realm={}",
            before.venue,
            before.realm,
            chosen.venue(),
            chosen.realm()
        )
        .into());
    }

    let held = lease::acquire(&before.venue, &before.realm, &before.user_id, LEASE_ROLE)?;
    let after = VenueGateway::account_identity(&mut venue).await?;
    if after != before {
        return Err(format!(
            "account identity changed while taking the lease: before={before:?} after={after:?}"
        )
        .into());
    }
    println!(
        "canary account venue={} realm={} user_id={} lease={}",
        after.venue,
        after.realm,
        after.user_id,
        held.path().display()
    );

    tokio::time::timeout(QUOTE_TIMEOUT, order_feed.await_ready())
        .await
        .map_err(|_| "private order feed was not ready inside 20 seconds")??;
    println!("canary private_feed=ready");

    let pre = CanaryGateway::inventory(&mut venue).await?;
    require_derivative_flat(&pre, "before the canary")?;
    println!("canary precheck=derivative-flat scope={}", pre.scope);

    let rule = VenueGateway::instrument_rules(&mut venue)
        .await?
        .into_iter()
        .find_map(|(name, rule)| (name == symbol).then_some(rule))
        .ok_or_else(|| format!("venue returned no instrument rule for {symbol}"))?;
    let quote = next_fresh_quote(&mut market_feed, SymbolId(0)).await?;
    let now_ms = engine_types::clock::wall_ms();
    let plan = make_plan(&symbol, rule, quote, now_ms)?;
    println!(
        "canary quote symbol={} bid={} ask={} order_px={} qty={} stop={}",
        symbol,
        plan.bid_px,
        plan.ask_px,
        limit_px(&plan.request),
        plan.request.qty,
        plan.request.stop.map(|stop| stop.trigger_px).unwrap_or(0.0)
    );

    let result = execute_claimed(
        &mut venue,
        &mut order_feed,
        &symbol,
        &plan,
        Timings::default(),
    )
    .await;
    drop(held);
    result
}

fn require_demo(chosen: VenueName) -> Result<(), Box<dyn Error>> {
    if chosen != VenueName::BybitDemo {
        return Err(format!(
            "canary-order is compiled for bybit_demo only; config selects {chosen}"
        )
        .into());
    }
    Ok(())
}

fn normalized_symbol(raw: &str) -> Result<String, Box<dyn Error>> {
    let symbol = raw.trim().to_ascii_uppercase();
    if symbol.len() < 4
        || symbol.len() > 24
        || !symbol.ends_with("USDT")
        || !symbol
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
    {
        return Err(format!(
            "canary symbol must be one uppercase/digit Bybit linear USDT name, not {raw:?}"
        )
        .into());
    }
    Ok(symbol)
}

async fn next_fresh_quote<M: MarketFeed>(
    feed: &mut M,
    wanted: SymbolId,
) -> Result<Quote, Box<dyn Error>> {
    let quote = tokio::time::timeout(QUOTE_TIMEOUT, async {
        loop {
            match feed.next_event().await? {
                MarketEvent::Quote { symbol, quote } if symbol == wanted => {
                    return Ok::<Quote, FeedError>(quote)
                }
                _ => {}
            }
        }
    })
    .await
    .map_err(|_| "no public quote arrived inside 20 seconds")??;
    Ok(quote)
}

fn make_plan(
    symbol: &str,
    rule: InstrumentRule,
    quote: Quote,
    now_ms: i64,
) -> Result<CanaryPlan, Box<dyn Error>> {
    validate_rule(rule)?;
    if !quote.bid_px.is_finite()
        || !quote.ask_px.is_finite()
        || !quote.bid_qty.is_finite()
        || !quote.ask_qty.is_finite()
        || quote.bid_px <= 0.0
        || quote.ask_px <= quote.bid_px
        || quote.bid_qty <= 0.0
        || quote.ask_qty <= 0.0
    {
        return Err(format!("unusable public quote for {symbol}: {quote:?}").into());
    }
    let age_ms = now_ms - quote.venue_ts_ms;
    if quote.venue_ts_ms <= 0 || !(-5_000..=MAX_QUOTE_AGE_MS).contains(&age_ms) {
        return Err(format!(
            "public quote for {symbol} is not fresh: venue_ts_ms={} age_ms={age_ms}",
            quote.venue_ts_ms
        )
        .into());
    }

    // Fifty basis points is hundreds of ordinary spreads away while staying
    // well inside Bybit's dynamic price band. PostOnly is the wire-level veto
    // against a stale quote turning this proof into a taker order.
    let px = quantize_px(quote.bid_px * 0.995, Side::Buy, &rule);
    if !px.is_finite() || px <= 0.0 || px >= quote.bid_px {
        return Err(format!(
            "cannot place a passive canary below bid {} on tick {}",
            quote.bid_px, rule.tick_size
        )
        .into());
    }

    let wanted_qty = rule.min_qty.max(rule.min_notional * 1.05 / px);
    let qty_steps = steps(wanted_qty, rule.qty_step).ceil();
    let mut qty = round_clean(qty_steps * rule.qty_step, rule.qty_step);
    if qty * px + 1e-12 < rule.min_notional {
        qty = round_clean(qty + rule.qty_step, rule.qty_step);
    }
    if !qty.is_finite() || qty < rule.min_qty || qty * px + 1e-12 < rule.min_notional {
        return Err(
            format!("cannot form a minimum-value canary from rule {rule:?} at price {px}").into(),
        );
    }

    let stop_px = quantize_px(px * 0.85, Side::Sell, &rule);
    if !stop_px.is_finite() || stop_px <= 0.0 || stop_px >= px {
        return Err(format!("cannot form a protective stop below canary price {px}").into());
    }

    let (client_id, close_id) = order_ids();
    if client_id.len() > 36 || close_id.len() > 36 {
        return Err(
            "generated canary order id exceeds Bybit's 36-character limit"
                .to_string()
                .into(),
        );
    }
    Ok(CanaryPlan {
        request: OrderRequest {
            client_order_id: client_id,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            kind: OrderKind::Limit {
                px,
                tif: TimeInForce::PostOnly,
            },
            stop: Some(StopSpec {
                trigger_px: stop_px,
            }),
            reduce_only: false,
            close_position: false,
        },
        close_id,
        bid_px: quote.bid_px,
        ask_px: quote.ask_px,
    })
}

fn order_ids() -> (String, String) {
    static NONCE: AtomicU64 = AtomicU64::new(0);
    let stamp = engine_types::clock::wall_ns();
    let process = std::process::id() & 0xffff;
    let nonce = NONCE.fetch_add(1, Ordering::Relaxed) & 0xffff;
    (
        format!("lmcan-{stamp:x}-{process:04x}-{nonce:04x}"),
        format!("lmcls-{stamp:x}-{process:04x}-{nonce:04x}"),
    )
}

fn validate_rule(rule: InstrumentRule) -> Result<(), Box<dyn Error>> {
    if !rule.tick_size.is_finite()
        || !rule.qty_step.is_finite()
        || !rule.min_qty.is_finite()
        || !rule.min_notional.is_finite()
        || rule.tick_size <= 0.0
        || rule.qty_step <= 0.0
        || rule.min_qty <= 0.0
        || rule.min_notional <= 0.0
    {
        return Err(format!("venue returned an unusable instrument rule: {rule:?}").into());
    }
    Ok(())
}

fn require_derivative_flat(inventory: &AccountInventory, when: &str) -> Result<(), Box<dyn Error>> {
    require_fresh_inventory(inventory)?;
    let non_wallet: Vec<_> = inventory
        .positions
        .iter()
        .filter(|position| position.product != "wallet_asset")
        .collect();
    if !non_wallet.is_empty() || !inventory.open_orders.is_empty() {
        return Err(format!(
            "account is not derivative/order flat {when}: positions={non_wallet:?} open_orders={:?}",
            inventory.open_orders
        )
        .into());
    }
    Ok(())
}

fn require_fresh_inventory(inventory: &AccountInventory) -> Result<(), Box<dyn Error>> {
    let age_ms = engine_types::clock::wall_ms() - inventory.observed_ms;
    if inventory.observed_ms <= 0 || !(-5_000..=MAX_INVENTORY_AGE_MS).contains(&age_ms) {
        return Err(format!(
            "account inventory is not fresh: observed_ms={} age_ms={age_ms}",
            inventory.observed_ms
        )
        .into());
    }
    Ok(())
}

async fn execute_claimed<G: CanaryGateway, F: OrderFeed>(
    gateway: &mut G,
    order_feed: &mut F,
    symbol: &str,
    plan: &CanaryPlan,
    timings: Timings,
) -> Result<(), Box<dyn Error>> {
    let start_ms = gateway.venue_time().await?.saturating_sub(5_000);
    let sent = gateway.send(&plan.request).await;
    let ack = match sent {
        Ok(ack) if ack.client_order_id == plan.request.client_order_id => {
            println!(
                "canary create=accepted client_id={} venue_order_id={}",
                ack.client_order_id, ack.venue_order_id
            );
            Some(ack)
        }
        Ok(ack) => {
            let _ = gateway
                .cancel(plan.request.symbol, &plan.request.client_order_id)
                .await;
            let cleanup = cleanup(gateway, symbol, plan, start_ms, false, timings).await?;
            return Err(format!(
                "venue acknowledged a different client id {:?}; cleanup={cleanup:?}",
                ack.client_order_id
            )
            .into());
        }
        Err(error) => {
            let _ = gateway
                .cancel(plan.request.symbol, &plan.request.client_order_id)
                .await;
            let cleanup = cleanup(gateway, symbol, plan, start_ms, false, timings).await?;
            return Err(format!(
                "order create was ambiguous or failed: {error}; cleanup={cleanup:?}"
            )
            .into());
        }
    };

    let mut original_filled = false;
    let first_signal = wait_for_signal(
        order_feed,
        &plan.request.client_order_id,
        false,
        timings.private_update_timeout,
    )
    .await;
    let mut new_confirmed = matches!(first_signal, Some(PrivateSignal::New));
    original_filled |= matches!(first_signal, Some(PrivateSignal::Filled));
    if matches!(first_signal, Some(PrivateSignal::New)) {
        println!("canary private_order=New");
    }

    if !new_confirmed {
        let working = gateway.working().await?;
        new_confirmed = working
            .iter()
            .any(|order| order.client_order_id == plan.request.client_order_id);
        if new_confirmed {
            println!("canary rest_order=visible");
        }
    }

    let cancel = gateway
        .cancel(plan.request.symbol, &plan.request.client_order_id)
        .await;
    if cancel.is_ok() {
        println!("canary cancel=accepted");
    }
    let cancel_signal = wait_for_signal(
        order_feed,
        &plan.request.client_order_id,
        true,
        timings.private_update_timeout,
    )
    .await;
    let cancelled_confirmed = matches!(cancel_signal, Some(PrivateSignal::Cancelled));
    original_filled |= matches!(cancel_signal, Some(PrivateSignal::Filled));
    if cancelled_confirmed {
        println!("canary private_order=Cancelled");
    }

    let cleanup = cleanup(
        gateway,
        symbol,
        plan,
        start_ms,
        cancelled_confirmed,
        timings,
    )
    .await?;
    original_filled |= cleanup.original_filled;
    if original_filled || cleanup.close_attempted {
        return Err(
            "canary order filled; the command closed it and proved the derivative account clean"
                .to_string()
                .into(),
        );
    }
    if !new_confirmed {
        return Err(
            "create returned an id, but neither the private feed nor open-order inventory proved New"
                .to_string()
                .into(),
        );
    }
    if !cleanup.original_terminal || !cleanup.original_cancelled {
        return Err(format!(
            "the order never reached an exact Cancelled terminal state; cancel_reply={cancel:?}"
        )
        .into());
    }
    println!(
        "canary result=PASS client_id={} create_ack={} new_confirmed={} cancel_confirmed={} cleanup_scans=2",
        plan.request.client_order_id,
        ack.is_some(),
        new_confirmed,
        if cancelled_confirmed { "private" } else { "order-status" }
    );
    Ok(())
}

async fn wait_for_signal<F: OrderFeed>(
    feed: &mut F,
    client_id: &str,
    waiting_for_cancel: bool,
    timeout: Duration,
) -> Option<PrivateSignal> {
    let result = tokio::time::timeout(timeout, async {
        loop {
            let update = feed.next_update().await?;
            match signal_for(&update, client_id) {
                Some(PrivateSignal::New) if waiting_for_cancel => continue,
                Some(signal) => return Ok::<Option<PrivateSignal>, FeedError>(Some(signal)),
                None => {
                    if matches!(update, OrderUpdate::StreamReset { .. }) {
                        return Ok::<Option<PrivateSignal>, FeedError>(None);
                    }
                }
            }
        }
    })
    .await;
    match result {
        Ok(Ok(signal)) => signal,
        Ok(Err(error)) => {
            tracing::warn!(%error, "private order feed became uncertain during canary");
            None
        }
        Err(_) => None,
    }
}

fn signal_for(update: &OrderUpdate, client_id: &str) -> Option<PrivateSignal> {
    match update {
        OrderUpdate::Ack(ack) if ack.client_order_id == client_id => Some(PrivateSignal::New),
        OrderUpdate::Cancelled {
            client_order_id, ..
        } if client_order_id == client_id => Some(PrivateSignal::Cancelled),
        OrderUpdate::Fill {
            client_order_id, ..
        }
        | OrderUpdate::FastFill {
            client_order_id, ..
        } if client_order_id == client_id => Some(PrivateSignal::Filled),
        _ => None,
    }
}

async fn cleanup<G: CanaryGateway>(
    gateway: &mut G,
    symbol: &str,
    plan: &CanaryPlan,
    start_ms: i64,
    terminal_hint: bool,
    timings: Timings,
) -> Result<CleanupOutcome, Box<dyn Error>> {
    let mut consecutive_clean = 0;
    let mut close_attempted = false;
    let mut close_terminal = true;
    let mut original_filled = false;
    let mut original_terminal = terminal_hint;
    let mut original_cancelled = terminal_hint;
    let observation_started = tokio::time::Instant::now();
    let mut last_problem = String::new();

    for attempt in 0..timings.clean_scan_attempts {
        let inventory = match gateway.inventory().await {
            Ok(inventory) => inventory,
            Err(error) => {
                last_problem = format!("account inventory failed: {error}");
                tracing::warn!(%error, "account inventory failed during canary cleanup");
                if let Ok(working) = gateway.working().await {
                    for order in working {
                        let client_id = order.client_order_id.as_str();
                        if client_id == plan.request.client_order_id || client_id == plan.close_id {
                            let _ = gateway.cancel(plan.request.symbol, client_id).await;
                        }
                    }
                }
                consecutive_clean = 0;
                if attempt + 1 < timings.clean_scan_attempts {
                    tokio::time::sleep(timings.clean_scan_pause).await;
                }
                continue;
            }
        };
        if let Err(error) = require_fresh_inventory(&inventory) {
            last_problem = error.to_string();
            tracing::warn!(%error, "stale account inventory during canary cleanup");
            consecutive_clean = 0;
            if attempt + 1 < timings.clean_scan_attempts {
                tokio::time::sleep(timings.clean_scan_pause).await;
            }
            continue;
        }
        let venue_end_ms = match gateway.venue_time().await {
            Ok(value) => Some(value),
            Err(error) => {
                last_problem = format!("venue-time checkpoint failed: {error}");
                tracing::warn!(%error, "venue-time checkpoint failed during canary cleanup");
                None
            }
        };
        let history_current = if let Some(end_ms) = venue_end_ms {
            match gateway.executions_between(start_ms, end_ms).await {
                Ok(executions) => {
                    original_filled |= executions
                        .iter()
                        .any(|fill| fill.client_order_id == plan.request.client_order_id);
                    true
                }
                Err(error) => {
                    last_problem = format!("execution history failed: {error}");
                    tracing::warn!(%error, "execution history failed during canary cleanup");
                    false
                }
            }
        } else {
            false
        };

        let mut receipt_says_active = false;
        let receipt_current = match gateway
            .receipt(plan.request.symbol, &plan.request.client_order_id)
            .await
        {
            Ok(Some(receipt)) => {
                original_filled |= receipt.has_fill();
                if receipt.is_terminal() {
                    original_terminal = true;
                    original_cancelled = receipt.status == "Cancelled";
                    println!(
                        "canary order_status={} cumulative_filled_qty={}",
                        receipt.status, receipt.cumulative_filled_qty
                    );
                } else {
                    receipt_says_active = true;
                }
                true
            }
            Ok(None) => true,
            Err(error) => {
                last_problem = format!("exact order status failed: {error}");
                tracing::warn!(%error, "exact order status failed during canary cleanup");
                false
            }
        };

        let mut close_receipt_active = false;
        let close_receipt_current = if close_attempted {
            match gateway.receipt(plan.request.symbol, &plan.close_id).await {
                Ok(Some(receipt)) => {
                    close_terminal = receipt.is_terminal();
                    close_receipt_active = !close_terminal;
                    true
                }
                Ok(None) => false,
                Err(error) => {
                    last_problem = format!("recovery-close status failed: {error}");
                    tracing::warn!(%error, "recovery-close status failed during canary cleanup");
                    false
                }
            }
        } else {
            true
        };

        let mut our_open = receipt_says_active;
        let mut close_open = close_receipt_active;
        let mut unexpected_orders = Vec::new();
        for order in &inventory.open_orders {
            if order.client_order_id == plan.request.client_order_id {
                our_open = true;
            } else if order.client_order_id == plan.close_id {
                close_open = true;
            } else {
                unexpected_orders.push(order);
            }
        }
        if our_open {
            if let Err(error) = gateway
                .cancel(plan.request.symbol, &plan.request.client_order_id)
                .await
            {
                tracing::warn!(%error, "retrying canary cancellation through account scans");
            }
            consecutive_clean = 0;
        }

        let is_our_position = |position: &engine_types::AccountPosition| {
            position.product == "linear"
                && position.symbol == symbol
                && position.side == Side::Buy
                && position.qty.is_finite()
                && position.qty > 0.0
        };
        let non_wallet: Vec<_> = inventory
            .positions
            .iter()
            .filter(|position| position.product != "wallet_asset")
            .collect();
        let our_positions: Vec<_> = non_wallet
            .iter()
            .copied()
            .filter(|position| is_our_position(position))
            .collect();
        let unexpected_positions: Vec<_> = non_wallet
            .iter()
            .copied()
            .filter(|position| !is_our_position(position))
            .collect();
        if !our_positions.is_empty() {
            let qty: f64 = our_positions.iter().map(|position| position.qty).sum();
            if !close_attempted {
                let close = close_request(plan, qty);
                close_attempted = true;
                close_terminal = false;
                match gateway.send(&close).await {
                    Ok(ack) if ack.client_order_id == plan.close_id => {
                        println!("canary recovery=full-position-close qty={qty}");
                    }
                    Ok(ack) => {
                        last_problem = format!(
                            "recovery close acknowledged a different client id {:?}",
                            ack.client_order_id
                        );
                        tracing::error!(detail = %last_problem, "canary recovery close is ambiguous");
                    }
                    Err(error) => {
                        last_problem = format!("recovery close was ambiguous or failed: {error}");
                        tracing::error!(%error, "canary full-position recovery close was refused");
                    }
                }
            }
            consecutive_clean = 0;
        } else if close_open {
            if let Err(error) = gateway.cancel(plan.request.symbol, &plan.close_id).await {
                tracing::warn!(%error, "cancelling a leftover canary recovery order");
            }
            consecutive_clean = 0;
        } else if !our_open && (!unexpected_positions.is_empty() || !unexpected_orders.is_empty()) {
            if original_terminal && (!close_attempted || close_terminal) {
                return Err(format!(
                    "unrelated account state appeared after canary teardown: positions={unexpected_positions:?} open_orders={unexpected_orders:?}"
                )
                .into());
            }
            consecutive_clean = 0;
        } else if !our_open
            && inventory.open_orders.is_empty()
            && history_current
            && receipt_current
            && original_terminal
            && (!close_attempted || (close_terminal && close_receipt_current))
        {
            consecutive_clean += 1;
            println!(
                "canary cleanup_scan={} derivative_positions=0 open_orders=0",
                consecutive_clean
            );
            if consecutive_clean >= 2
                && observation_started.elapsed() >= timings.min_clean_observation
            {
                return Ok(CleanupOutcome {
                    original_filled,
                    close_attempted,
                    original_terminal,
                    original_cancelled,
                });
            }
        } else {
            consecutive_clean = 0;
        }

        if attempt + 1 < timings.clean_scan_attempts {
            tokio::time::sleep(timings.clean_scan_pause).await;
        }
    }
    let _ = gateway
        .cancel(plan.request.symbol, &plan.request.client_order_id)
        .await;
    if close_attempted {
        let _ = gateway.cancel(plan.request.symbol, &plan.close_id).await;
    }
    let detail = if last_problem.is_empty() {
        "venue state stayed non-flat or uncertain".to_string()
    } else {
        last_problem
    };
    Err(
        format!("canary cleanup could not prove two consecutive flat account scans: {detail}")
            .into(),
    )
}

fn close_request(plan: &CanaryPlan, qty: f64) -> OrderRequest {
    OrderRequest {
        client_order_id: plan.close_id.clone(),
        strategy: plan.request.strategy,
        symbol: plan.request.symbol,
        side: plan.request.side.flipped(),
        qty,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: true,
        close_position: true,
    }
}

fn limit_px(request: &OrderRequest) -> f64 {
    match request.kind {
        OrderKind::Limit { px, .. } => px,
        OrderKind::Market => 0.0,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;

    use super::*;
    use engine_types::{AccountOrder, AccountPosition};

    fn quote() -> Quote {
        Quote {
            bid_px: 1.4,
            bid_qty: 100.0,
            ask_px: 1.4001,
            ask_qty: 100.0,
            venue_ts_ms: 1_000_000,
            recv_ns: 1,
            seq: 1,
        }
    }

    fn rule() -> InstrumentRule {
        InstrumentRule {
            tick_size: 0.0001,
            qty_step: 0.1,
            min_qty: 0.1,
            min_notional: 5.0,
        }
    }

    fn plan() -> CanaryPlan {
        make_plan("XRPUSDT", rule(), quote(), 1_000_100).unwrap()
    }

    fn flat() -> AccountInventory {
        AccountInventory {
            scope: "test".into(),
            positions: vec![AccountPosition {
                product: "wallet_asset".into(),
                symbol: "BTC".into(),
                side: Side::Buy,
                qty: 2.0,
            }],
            open_orders: Vec::new(),
            observed_ms: engine_types::clock::wall_ms(),
        }
    }

    fn ack(client_id: &str) -> OrderAck {
        OrderAck {
            client_order_id: client_id.into(),
            venue_order_id: format!("venue-{client_id}"),
            sent_ns: 1,
            ack_ns: 2,
        }
    }

    struct FakeFeed {
        updates: VecDeque<Result<OrderUpdate, FeedError>>,
    }

    impl OrderFeed for FakeFeed {
        async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
            self.updates.pop_front().unwrap_or(Err(FeedError::Closed))
        }
    }

    struct FakeGateway {
        sends: Vec<OrderRequest>,
        inventories: VecDeque<AccountInventory>,
        working: Vec<VenueOrder>,
        executions: Vec<VenueExecution>,
        cancel_ok: bool,
        create_error: bool,
        close_error: bool,
        receipt_script: VecDeque<Option<&'static str>>,
        cancel_ids: Vec<String>,
        execution_ends: Vec<i64>,
    }

    impl CanaryGateway for FakeGateway {
        async fn send(&mut self, request: &OrderRequest) -> Result<OrderAck, VenueError> {
            self.sends.push(request.clone());
            if (!request.close_position && self.create_error)
                || (request.close_position && self.close_error)
            {
                return Err(VenueError::Transport("lost create reply".into()));
            }
            Ok(ack(&request.client_order_id))
        }

        async fn cancel(&mut self, _symbol: SymbolId, client_id: &str) -> Result<(), VenueError> {
            self.cancel_ids.push(client_id.to_string());
            if self.cancel_ok {
                Ok(())
            } else {
                Err(VenueError::Transport("lost cancel reply".into()))
            }
        }

        async fn working(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
            Ok(self.working.clone())
        }

        async fn inventory(&mut self) -> Result<AccountInventory, VenueError> {
            Ok(self.inventories.pop_front().unwrap_or_else(flat))
        }

        async fn executions_between(
            &mut self,
            _start_ms: i64,
            end_ms: i64,
        ) -> Result<Vec<VenueExecution>, VenueError> {
            self.execution_ends.push(end_ms);
            Ok(self.executions.clone())
        }

        async fn venue_time(&mut self) -> Result<i64, VenueError> {
            Ok(engine_types::clock::wall_ms() + 5_000)
        }

        async fn receipt(
            &mut self,
            _symbol: SymbolId,
            client_id: &str,
        ) -> Result<Option<BybitOrderReceipt>, VenueError> {
            if let Some(scripted) = self.receipt_script.pop_front() {
                return Ok(scripted.map(|status| BybitOrderReceipt {
                    symbol: "XRPUSDT".into(),
                    client_order_id: client_id.into(),
                    status: status.into(),
                    cumulative_filled_qty: 0.0,
                    updated_ms: engine_types::clock::wall_ms(),
                }));
            }
            let Some(request) = self
                .sends
                .iter()
                .find(|request| request.client_order_id == client_id)
            else {
                return Ok(None);
            };
            let filled = self
                .executions
                .iter()
                .any(|fill| fill.client_order_id == client_id)
                || request.close_position;
            Ok(Some(BybitOrderReceipt {
                symbol: "XRPUSDT".into(),
                client_order_id: client_id.into(),
                status: if filled { "Filled" } else { "Cancelled" }.into(),
                cumulative_filled_qty: if filled { request.qty } else { 0.0 },
                updated_ms: engine_types::clock::wall_ms(),
            }))
        }
    }

    fn timings() -> Timings {
        Timings {
            private_update_timeout: Duration::from_millis(1),
            clean_scan_pause: Duration::ZERO,
            clean_scan_attempts: 4,
            min_clean_observation: Duration::ZERO,
        }
    }

    #[test]
    fn only_the_practice_realm_can_reach_the_command() {
        assert!(require_demo(VenueName::BybitDemo).is_ok());
        assert!(require_demo(VenueName::BybitMainnet).is_err());
        assert!(require_demo(VenueName::HyperliquidTestnet).is_err());
    }

    #[test]
    fn the_order_is_passive_minimum_value_and_has_a_full_stop() {
        let plan = plan();
        let px = limit_px(&plan.request);
        assert!(px < plan.bid_px);
        assert!(matches!(
            plan.request.kind,
            OrderKind::Limit {
                tif: TimeInForce::PostOnly,
                ..
            }
        ));
        assert!(plan.request.qty * px >= rule().min_notional);
        assert!(plan.request.qty >= rule().min_qty);
        assert!(plan.request.stop.unwrap().trigger_px < px);
        assert!(!plan.request.reduce_only);
    }

    #[test]
    fn order_ids_are_unique_and_fit_the_venue_limit() {
        let mut ids = std::collections::HashSet::new();
        for _ in 0..10_000 {
            let (entry, close) = order_ids();
            assert!(entry.len() <= 36, "entry id was {entry:?}");
            assert!(close.len() <= 36, "close id was {close:?}");
            assert!(ids.insert(entry));
            assert!(ids.insert(close));
        }
    }

    #[test]
    fn wallet_holdings_do_not_masquerade_as_derivative_exposure() {
        assert!(require_derivative_flat(&flat(), "test").is_ok());
        let mut inventory = flat();
        inventory.positions.push(AccountPosition {
            product: "linear".into(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: 1.0,
        });
        assert!(require_derivative_flat(&inventory, "test").is_err());
        let mut inventory = flat();
        inventory.open_orders.push(AccountOrder {
            product: "spot".into(),
            symbol: "BTCUSDT".into(),
            client_order_id: "somebody-else".into(),
        });
        assert!(require_derivative_flat(&inventory, "test").is_err());
    }

    #[tokio::test]
    async fn new_then_cancelled_and_two_flat_scans_pass() {
        let plan = plan();
        let before_ms = engine_types::clock::wall_ms();
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([flat(), flat()]),
            working: Vec::new(),
            executions: Vec::new(),
            cancel_ok: true,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::from([
                Ok(OrderUpdate::Ack(ack(&plan.request.client_order_id))),
                Ok(OrderUpdate::Cancelled {
                    client_order_id: plan.request.client_order_id.clone(),
                    recv_ns: 3,
                }),
            ]),
        };

        execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, timings())
            .await
            .unwrap();
        assert_eq!(gateway.sends, vec![plan.request]);
        assert!(
            gateway
                .execution_ends
                .iter()
                .all(|end_ms| *end_ms >= before_ms + 4_000),
            "execution history did not use the venue clock: {:?}",
            gateway.execution_ends
        );
    }

    #[tokio::test]
    async fn lost_cancel_reply_is_resolved_by_private_cancelled() {
        let plan = plan();
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([flat(), flat()]),
            working: Vec::new(),
            executions: Vec::new(),
            cancel_ok: false,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::from([
                Ok(OrderUpdate::Ack(ack(&plan.request.client_order_id))),
                Ok(OrderUpdate::Cancelled {
                    client_order_id: plan.request.client_order_id.clone(),
                    recv_ns: 3,
                }),
            ]),
        };

        execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, timings())
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn an_accidental_fill_is_closed_and_reported_as_failure() {
        let plan = plan();
        let mut exposed = flat();
        exposed.positions.push(AccountPosition {
            product: "linear".into(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: plan.request.qty,
        });
        let fill = VenueExecution {
            exec_id: "fill-1".into(),
            client_order_id: plan.request.client_order_id.clone(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: plan.request.qty,
            px: limit_px(&plan.request),
            fee: Some(0.0),
            is_maker: true,
            forced_close: None,
            venue_ts_ms: 1,
        };
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([exposed, flat(), flat()]),
            working: Vec::new(),
            executions: vec![fill],
            cancel_ok: true,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::from([Ok(OrderUpdate::FastFill {
                exec_id: "fill-1".into(),
                client_order_id: plan.request.client_order_id.clone(),
                venue_order_id: "venue-entry".into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: plan.request.qty,
                px: limit_px(&plan.request),
                is_maker: true,
                venue_ts_ms: 1,
                recv_ns: 2,
            })]),
        };

        let error = execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, timings())
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("filled"), "{error}");
        assert_eq!(gateway.sends.len(), 2);
        let close = gateway.sends.last().unwrap();
        assert!(close.reduce_only && close.close_position);
        assert_eq!(close.side, Side::Sell);
    }

    #[tokio::test]
    async fn an_ambiguous_create_is_cancelled_until_its_late_status_is_terminal() {
        let plan = plan();
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([flat(), flat(), flat(), flat(), flat()]),
            working: Vec::new(),
            executions: Vec::new(),
            cancel_ok: true,
            create_error: true,
            close_error: false,
            receipt_script: VecDeque::from([
                None,
                None,
                Some("New"),
                Some("Cancelled"),
                Some("Cancelled"),
            ]),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::new(),
        };
        let mut slow_status = timings();
        slow_status.clean_scan_attempts = 5;

        let error = execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, slow_status)
            .await
            .unwrap_err()
            .to_string();

        assert!(error.contains("ambiguous or failed"), "{error}");
        assert!(
            gateway
                .cancel_ids
                .iter()
                .filter(|client_id| *client_id == &plan.request.client_order_id)
                .count()
                >= 2,
            "late active order was not cancelled again: {:?}",
            gateway.cancel_ids
        );
    }

    #[tokio::test]
    async fn a_cancel_reply_without_any_terminal_evidence_fails_closed() {
        let plan = plan();
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([flat(), flat(), flat(), flat()]),
            working: Vec::new(),
            executions: Vec::new(),
            cancel_ok: true,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::from([None, None, None, None]),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::from([
                Ok(OrderUpdate::Ack(ack(&plan.request.client_order_id))),
                Err(FeedError::Closed),
            ]),
        };

        let error = execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, timings())
            .await
            .unwrap_err()
            .to_string();

        assert!(error.contains("could not prove"), "{error}");
        assert!(gateway.cancel_ids.len() >= 2);
    }

    #[tokio::test]
    async fn a_fill_seen_after_cancelled_is_still_closed() {
        let plan = plan();
        let mut exposed = flat();
        exposed.positions.push(AccountPosition {
            product: "linear".into(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: plan.request.qty,
        });
        let fill = VenueExecution {
            exec_id: "late-fill".into(),
            client_order_id: plan.request.client_order_id.clone(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: plan.request.qty,
            px: limit_px(&plan.request),
            fee: Some(0.0),
            is_maker: true,
            forced_close: None,
            venue_ts_ms: 1,
        };
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([exposed, flat(), flat()]),
            working: Vec::new(),
            executions: vec![fill],
            cancel_ok: true,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::from([
                Ok(OrderUpdate::Ack(ack(&plan.request.client_order_id))),
                Ok(OrderUpdate::Cancelled {
                    client_order_id: plan.request.client_order_id.clone(),
                    recv_ns: 3,
                }),
            ]),
        };

        let error = execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, timings())
            .await
            .unwrap_err()
            .to_string();

        assert!(error.contains("filled"), "{error}");
        assert_eq!(gateway.sends.len(), 2);
        assert!(gateway.sends[1].reduce_only && gateway.sends[1].close_position);
    }

    #[tokio::test]
    async fn an_ambiguous_recovery_close_is_never_submitted_twice() {
        let plan = plan();
        let mut exposed = flat();
        exposed.positions.push(AccountPosition {
            product: "linear".into(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: plan.request.qty,
        });
        let fill = VenueExecution {
            exec_id: "fill-before-lost-close-reply".into(),
            client_order_id: plan.request.client_order_id.clone(),
            symbol: "XRPUSDT".into(),
            side: Side::Buy,
            qty: plan.request.qty,
            px: limit_px(&plan.request),
            fee: Some(0.0),
            is_maker: true,
            forced_close: None,
            venue_ts_ms: 1,
        };
        let mut gateway = FakeGateway {
            sends: Vec::new(),
            inventories: VecDeque::from([exposed, flat(), flat()]),
            working: Vec::new(),
            executions: vec![fill],
            cancel_ok: true,
            create_error: false,
            close_error: true,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };
        let mut feed = FakeFeed {
            updates: VecDeque::from([
                Ok(OrderUpdate::Ack(ack(&plan.request.client_order_id))),
                Ok(OrderUpdate::Cancelled {
                    client_order_id: plan.request.client_order_id.clone(),
                    recv_ns: 3,
                }),
            ]),
        };

        let error = execute_claimed(&mut gateway, &mut feed, "XRPUSDT", &plan, timings())
            .await
            .unwrap_err()
            .to_string();

        assert!(error.contains("filled"), "{error}");
        assert_eq!(gateway.sends.len(), 2, "recovery close was duplicated");
        assert_eq!(gateway.sends[1].client_order_id, plan.close_id);
    }

    #[tokio::test]
    async fn unrelated_order_state_does_not_skip_our_teardown() {
        let plan = plan();
        let own = AccountOrder {
            product: "linear".into(),
            symbol: "XRPUSDT".into(),
            client_order_id: plan.request.client_order_id.clone(),
        };
        let foreign = AccountOrder {
            product: "linear".into(),
            symbol: "ETHUSDT".into(),
            client_order_id: "not-this-canary".into(),
        };
        let mut both = flat();
        both.open_orders = vec![own, foreign.clone()];
        let mut foreign_only = flat();
        foreign_only.open_orders = vec![foreign];
        let mut gateway = FakeGateway {
            sends: vec![plan.request.clone()],
            inventories: VecDeque::from([both, foreign_only]),
            working: Vec::new(),
            executions: Vec::new(),
            cancel_ok: true,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };

        let error = cleanup(&mut gateway, "XRPUSDT", &plan, 1, false, timings())
            .await
            .unwrap_err()
            .to_string();

        assert!(error.contains("unrelated account state"), "{error}");
        assert!(gateway.cancel_ids.contains(&plan.request.client_order_id));
    }

    #[tokio::test]
    async fn an_unrelated_short_does_not_prevent_closing_our_long() {
        let plan = plan();
        let mut mixed = flat();
        mixed.positions.extend([
            AccountPosition {
                product: "linear".into(),
                symbol: "XRPUSDT".into(),
                side: Side::Buy,
                qty: plan.request.qty,
            },
            AccountPosition {
                product: "linear".into(),
                symbol: "ETHUSDT".into(),
                side: Side::Sell,
                qty: 1.0,
            },
        ]);
        let mut foreign_only = flat();
        foreign_only.positions.push(AccountPosition {
            product: "linear".into(),
            symbol: "ETHUSDT".into(),
            side: Side::Sell,
            qty: 1.0,
        });
        let mut gateway = FakeGateway {
            sends: vec![plan.request.clone()],
            inventories: VecDeque::from([mixed, foreign_only]),
            working: Vec::new(),
            executions: Vec::new(),
            cancel_ok: true,
            create_error: false,
            close_error: false,
            receipt_script: VecDeque::new(),
            cancel_ids: Vec::new(),
            execution_ends: Vec::new(),
        };

        let error = cleanup(&mut gateway, "XRPUSDT", &plan, 1, false, timings())
            .await
            .unwrap_err()
            .to_string();

        assert!(error.contains("unrelated account state"), "{error}");
        assert_eq!(gateway.sends.len(), 2);
        assert!(gateway.sends[1].reduce_only && gateway.sends[1].close_position);
    }
}
