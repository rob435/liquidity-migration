//! The loop.
//!
//! One process, one thread, one loop. The runtime is tokio's
//! `new_current_thread`, and nothing on the hot path is spawned, sent to a
//! pool, or shared with another thread: a market message is parsed by the
//! feed, applied to the shared picture, handed to the strategies that asked
//! for it, and the intent that comes back is logged, judged, made durable and
//! sent — all on the same thread, so there is no lock and no hand-off to
//! account for in the latency numbers. The only threads in the whole binary
//! belong to the bench's pretend venue, which stands in for a machine that is
//! genuinely somewhere else.
//!
//! What the loop waits on, all in one `select!`:
//!
//! - the market feed
//! - the private order feed
//! - the next due strategy timer
//! - the group-flush tick (also the moment the account reading is refreshed)
//! - shutdown
//!
//! **Feeds must be cancel-safe.** `select!` drops the futures of the branches
//! that did not win, so `MarketFeed::next_event` and `OrderFeed::next_update`
//! must lose nothing when dropped part-way — the same contract
//! `tokio::sync::mpsc::Receiver::recv` keeps. A feed that reads a socket must
//! park partial reads in its own buffer, not in the future.
//!
//! The order of the intent pipeline is the part that a crash reads back:
//! intent recorded, verdict recorded, order recorded **and forced to disk**,
//! and only then the bytes leave. A crash between the barrier and the reply
//! leaves an order the log knows about and no reply for it, which is exactly
//! what `engine replay` shows as in flight.

use std::collections::VecDeque;
use std::future::Future;
use std::time::Duration;

use engine_types::{
    quantize, AccountView, EngineEvent, InstrumentRule, Intent, MarketEvent, MarketFeed,
    MarketState, OrderFeed, OrderKind, OrderRequest, OrderUpdate, RiskKernel, RiskVerdict,
    Strategy, StrategyId, Subscription, SymbolId, VenueError, VenueGateway, Wal, WalError,
    WalRecord,
};

use crate::clock;
use crate::config::EngineSection;
use crate::ctx::{Ctx, Timers};
use crate::inflight::{self, LedgerOfOrders, OrderRegistry};
use crate::ledger::{LatencyLedger, Segment};
use crate::routing::Routing;

/// A strategy that emits from every order update it hears could keep the loop
/// busy forever. One wake handles this many intents; the rest are dropped
/// with a note, and the loop goes back to reading the market.
pub const MAX_INTENTS_PER_WAKE: usize = 64;

pub const ENGINE_VERSION: &str = concat!("engine-core ", env!("CARGO_PKG_VERSION"));

#[derive(Debug)]
pub enum EngineError {
    Wal(WalError),
    Venue(VenueError),
    Boot(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::Wal(e) => write!(f, "log: {e}"),
            EngineError::Venue(e) => write!(f, "venue: {e}"),
            EngineError::Boot(m) => write!(f, "boot: {m}"),
        }
    }
}

impl std::error::Error for EngineError {}

impl From<WalError> for EngineError {
    fn from(e: WalError) -> Self {
        EngineError::Wal(e)
    }
}

impl From<VenueError> for EngineError {
    fn from(e: VenueError) -> Self {
        EngineError::Venue(e)
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum StopReason {
    Shutdown,
    FeedClosed,
}

#[derive(Debug)]
pub struct RunOutcome {
    pub stopped_by: StopReason,
    pub market_events: u64,
    pub orders_sent: u64,
}

pub struct Engine<W: Wal, R: RiskKernel, V: VenueGateway> {
    pub wal: W,
    pub risk: R,
    pub venue: V,
    strategies: Vec<Box<dyn Strategy>>,
    names: Vec<String>,
    market: MarketState,
    routing: Routing,
    rules: Vec<Option<InstrumentRule>>,
    timers: Timers,
    pending: VecDeque<Intent>,
    account: AccountView,
    registry: OrderRegistry,
    orders: LedgerOfOrders,
    ledger: LatencyLedger,
    shadow: bool,
    group_flush: Duration,
    refresh_after_ns: u64,
    next_order_n: u64,
    orders_sent: u64,
    /// Counted for the whole run. The ledger's own count clears every minute.
    events_seen: u64,
    subscriptions: Vec<Subscription>,
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Come up: read the log back, say who we are in it, learn what the
    /// strategies want, then ask the venue for the instrument rules and the
    /// account before the first message is allowed in.
    pub async fn boot(
        settings: &EngineSection,
        config_sha256: &str,
        mut wal: W,
        risk: R,
        mut venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        let orders = LedgerOfOrders::from_records(replayed);
        let recovered = orders.in_flight().len();

        let boot_ms = clock::wall_ms();
        wal.append(&WalRecord::Boot {
            version: ENGINE_VERSION.to_string(),
            config_sha256: config_sha256.to_string(),
            wall_ts_ms: boot_ms,
        })?;
        // The Boot record has no room for the mode, and a reader of the log
        // needs it: the records look the same either way.
        wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: if settings.shadow {
                "shadow: orders are worked out and written down, never sent".to_string()
            } else {
                "live: orders are sent, each one gated by the risk kernel".to_string()
            },
        })?;

        let mut market = MarketState::default();
        let mut routing = Routing::default();
        let mut names = Vec::with_capacity(strategies.len());
        let mut subscriptions = Vec::new();
        for (index, strategy) in strategies.iter().enumerate() {
            let sid = StrategyId(u16::try_from(index).map_err(|_| {
                EngineError::Boot("more than 65535 strategies".to_string())
            })?);
            names.push(strategy.name().to_string());
            for sub in strategy.subscriptions() {
                let symbol = market.add_symbol(&sub.symbol);
                routing.add(symbol, sub.feed, sid);
                if !subscriptions.contains(&sub) {
                    subscriptions.push(sub.clone());
                }
            }
        }
        routing.size_to(market.table.len());

        let mut rules = vec![None; market.table.len()];
        for (name, rule) in venue.instrument_rules().await? {
            if let Some(id) = market.table.get(&name) {
                rules[id.0 as usize] = Some(rule);
            }
        }
        let missing: Vec<&str> = (0..market.table.len())
            .filter(|i| rules[*i].is_none())
            .map(|i| market.table.name(SymbolId(i as u16)))
            .collect();
        if !missing.is_empty() {
            // No rule means no way to quantize, which means nothing can be
            // sent for that symbol. Say so now rather than at the first
            // intent.
            tracing::warn!(symbols = ?missing, "no instrument rules; these symbols cannot trade");
        }

        let account = venue.account_view().await?;

        let mut registry = OrderRegistry::new(format!("eng-{boot_ms}-"));
        for order in orders.in_flight() {
            registry.own(&order.request.client_order_id, order.request.strategy);
        }
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                ids = ?orders.in_flight_ids(),
                "orders were in flight when the engine last stopped; they are not re-sent"
            );
        }

        let now = clock::now_ns();
        Ok(Engine {
            wal,
            risk,
            venue,
            strategies,
            names,
            market,
            routing,
            rules,
            timers: Timers::default(),
            pending: VecDeque::new(),
            account,
            registry,
            orders,
            ledger: LatencyLedger::new(now),
            shadow: settings.shadow,
            group_flush: Duration::from_millis(settings.group_flush_ms.max(1)),
            refresh_after_ns: settings.account_view_max_age_ms.saturating_mul(1_000_000) / 2,
            next_order_n: 0,
            orders_sent: 0,
            events_seen: 0,
            subscriptions,
        })
    }

    pub fn subscriptions(&self) -> &[Subscription] {
        &self.subscriptions
    }

    pub fn in_flight_ids(&self) -> Vec<&str> {
        self.orders.in_flight_ids()
    }

    pub fn ledger(&self) -> &LatencyLedger {
        &self.ledger
    }

    pub fn market(&self) -> &MarketState {
        &self.market
    }

    pub fn account(&self) -> &AccountView {
        &self.account
    }

    pub fn shadow(&self) -> bool {
        self.shadow
    }

    /// Run until shutdown resolves or the market feed closes.
    pub async fn run<M, O, S>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
        shutdown: S,
    ) -> Result<RunOutcome, EngineError>
    where
        M: MarketFeed,
        O: OrderFeed,
        S: Future<Output = ()>,
    {
        tokio::pin!(shutdown);
        let mut flush_tick = tokio::time::interval(self.group_flush);
        flush_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut stopped_by = StopReason::Shutdown;
        let mut order_feed_open = true;

        loop {
            let timer_wait = self.timers.next_deadline().map(|deadline| {
                Duration::from_nanos(deadline.saturating_sub(clock::now_ns()))
            });

            tokio::select! {
                _ = &mut shutdown => break,
                event = market_feed.next_event() => match event {
                    Ok(event) => self.on_market(event).await?,
                    Err(engine_types::FeedError::Closed) => {
                        stopped_by = StopReason::FeedClosed;
                        break;
                    }
                    // A feed that errors without closing is expected to be
                    // reconnecting inside. Wait a moment so a broken one
                    // cannot spin the loop.
                    Err(e) => {
                        tracing::warn!(error = %e, "market feed hiccup");
                        tokio::time::sleep(Duration::from_millis(1)).await;
                    }
                },
                update = order_feed.next_update(), if order_feed_open => match update {
                    Ok(update) => {
                        let now = clock::now_ns();
                        self.take_update(update).await?;
                        self.drain(now).await?;
                    }
                    Err(engine_types::FeedError::Closed) => {
                        tracing::warn!("order feed closed; order news now only comes from replies");
                        order_feed_open = false;
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "order feed hiccup");
                        tokio::time::sleep(Duration::from_millis(1)).await;
                    }
                },
                _ = tokio::time::sleep(timer_wait.unwrap_or_default()), if timer_wait.is_some() => {
                    self.on_timers().await?;
                }
                _ = flush_tick.tick() => self.on_tick().await?,
            }
        }

        self.finish().await?;
        Ok(RunOutcome {
            stopped_by,
            market_events: self.events_seen,
            orders_sent: self.orders_sent,
        })
    }

    /// Last flush and last ledger line on the way out.
    pub async fn finish(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        let record = self.ledger.record_for_wal(now);
        self.wal.append(&record)?;
        self.wal.flush()?;
        tracing::info!("latency, {}", self.ledger.plain_line(now));
        Ok(())
    }

    async fn on_market(&mut self, event: MarketEvent) -> Result<(), EngineError> {
        let now = clock::now_ns();
        self.market.apply(&event);
        self.ledger.saw_event();
        self.events_seen += 1;
        let origin_ns = arrival_ns(&event, now);
        let engine_event = EngineEvent::Market(event);
        {
            let Engine {
                strategies,
                market,
                timers,
                pending,
                routing,
                ..
            } = self;
            match event {
                MarketEvent::Quote { symbol, .. } => {
                    for sid in routing.quote_listeners(symbol) {
                        feed_strategy(strategies, market, timers, pending, *sid, &engine_event, now);
                    }
                }
                MarketEvent::Ticker { symbol, .. } => {
                    for sid in routing.ticker_listeners(symbol) {
                        feed_strategy(strategies, market, timers, pending, *sid, &engine_event, now);
                    }
                }
                MarketEvent::FeedReset { .. } => {
                    for index in 0..strategies.len() {
                        let sid = StrategyId(index as u16);
                        feed_strategy(strategies, market, timers, pending, sid, &engine_event, now);
                    }
                }
            }
        }
        self.drain(origin_ns).await
    }

    async fn on_timers(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        while let Some((sid, timer)) = self.timers.pop_due(now) {
            let event = EngineEvent::Timer { id: timer, now_ns: now };
            let Engine {
                strategies,
                market,
                timers,
                pending,
                ..
            } = self;
            feed_strategy(strategies, market, timers, pending, sid, &event, now);
        }
        self.drain(now).await
    }

    async fn on_tick(&mut self) -> Result<(), EngineError> {
        self.wal.flush()?;
        let now = clock::now_ns();
        if self.ledger.due(now) {
            let record = self.ledger.record_for_wal(now);
            self.wal.append(&record)?;
            tracing::info!("latency, {}", self.ledger.plain_line(now));
            self.ledger.reset(now);
        }
        if now.saturating_sub(self.account.observed_ns) >= self.refresh_after_ns {
            match self.venue.account_view().await {
                Ok(view) => self.account = view,
                // Keeping the old reading is not the same as trusting it: it
                // ages, and the risk kernel refuses on an old reading.
                Err(e) => tracing::warn!(error = %e, "could not refresh the account reading"),
            }
        }
        Ok(())
    }

    async fn drain(&mut self, origin_ns: u64) -> Result<(), EngineError> {
        let mut handled = 0usize;
        while let Some(intent) = self.pending.pop_front() {
            handled += 1;
            if handled > MAX_INTENTS_PER_WAKE {
                let dropped = self.pending.len() + 1;
                self.pending.clear();
                tracing::error!(dropped, "too many intents in one wake; the rest were dropped");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("dropped {dropped} intents: more than {MAX_INTENTS_PER_WAKE} in one wake"),
                })?;
                break;
            }
            self.process_intent(intent, origin_ns).await?;
        }
        Ok(())
    }

    /// The hot path, in the order a crash reads it back.
    async fn process_intent(&mut self, intent: Intent, origin_ns: u64) -> Result<(), EngineError> {
        let decided_ns = if intent.decided_ns > 0 {
            intent.decided_ns
        } else {
            clock::now_ns()
        };
        self.ledger
            .record(Segment::Decide, decided_ns.saturating_sub(origin_ns));

        self.wal.append(&WalRecord::Intent {
            intent: intent.clone(),
        })?;

        let verdict = {
            let Engine { risk, account, .. } = self;
            risk.assess(&intent, account)
        };
        let allowed_qty = match &verdict {
            RiskVerdict::Allow { qty } => *qty,
            RiskVerdict::Deny { reason } => {
                let reason = format!("{reason:?}");
                self.wal.append(&WalRecord::Verdict {
                    client_order_id: None,
                    verdict,
                })?;
                tracing::info!(tag = %intent.tag, reason, "risk refused the order");
                return Ok(());
            }
        };

        // Minting the id here (not a log write) lets the verdict record name
        // the order it approved; a refused intent never burns an id.
        let client_order_id = self.mint_id();
        self.wal.append(&WalRecord::Verdict {
            client_order_id: Some(client_order_id.clone()),
            verdict,
        })?;

        let Some(rule) = self.rules.get(intent.symbol.0 as usize).copied().flatten() else {
            return self.refuse(
                &client_order_id,
                &intent,
                "no instrument rule for this symbol",
            );
        };
        let Some(qty) = quantize::quantize_qty(allowed_qty, &rule) else {
            return self.refuse(
                &client_order_id,
                &intent,
                &format!(
                    "{allowed_qty} does not reach the smallest tradable size ({} step, {} minimum)",
                    rule.qty_step, rule.min_qty
                ),
            );
        };
        let kind = match intent.kind {
            OrderKind::Market => OrderKind::Market,
            OrderKind::Limit { px, tif } => OrderKind::Limit {
                px: quantize::quantize_px(px, intent.side, &rule),
                tif,
            },
        };
        if let Some(reference_px) = self.reference_px(intent.symbol, &kind) {
            let notional = qty * reference_px;
            if notional + 1e-9 < rule.min_notional {
                return self.refuse(
                    &client_order_id,
                    &intent,
                    &format!(
                        "{notional:.4} is under the venue's smallest order value ({})",
                        rule.min_notional
                    ),
                );
            }
        }

        let request = OrderRequest {
            client_order_id: client_order_id.clone(),
            strategy: intent.strategy,
            symbol: intent.symbol,
            side: intent.side,
            qty,
            kind,
            stop: intent.stop,
            reduce_only: intent.reduce_only,
        };

        // Durable before the wire. Everything above this line can be lost by
        // a crash without consequence; everything below it cannot.
        let sent_record = WalRecord::OrderSent {
            request: request.clone(),
            wire_ns: clock::now_ns(),
        };
        self.wal.append(&sent_record)?;
        self.wal.barrier()?;
        self.ledger
            .record(Segment::Durable, clock::now_ns().saturating_sub(decided_ns));
        self.orders.apply(&sent_record);
        self.registry.own(&client_order_id, intent.strategy);
        self.orders_sent += 1;

        let update = if self.shadow {
            let note = WalRecord::Note {
                source: "shadow".into(),
                text: format!(
                    "{}{client_order_id} would have been {:?} {} {} ({})",
                    inflight::NEVER_SENT_PREFIX,
                    request.side,
                    request.qty,
                    self.market.table.name(request.symbol),
                    intent.tag
                ),
            };
            self.wal.append(&note)?;
            self.orders.apply(&note);
            self.ledger
                .record(Segment::Wire, clock::now_ns().saturating_sub(decided_ns));
            None
        } else {
            let send_started_ns = clock::now_ns();
            let reply = self.venue.send_order(&request).await;
            let returned_ns = clock::now_ns();
            self.ledger
                .record(Segment::Wire, returned_ns.saturating_sub(decided_ns));
            match reply {
                Ok(ack) => {
                    let ack_ns = if ack.ack_ns > send_started_ns {
                        ack.ack_ns
                    } else {
                        returned_ns
                    };
                    self.ledger
                        .record(Segment::Ack, ack_ns.saturating_sub(send_started_ns));
                    Some(OrderUpdate::Ack(ack))
                }
                Err(VenueError::Rejected { code, message }) => Some(OrderUpdate::Reject {
                    client_order_id: client_order_id.clone(),
                    code,
                    reason: message,
                }),
                Err(other) => {
                    // We do not know whether the venue got it. Leave the
                    // order in flight and say why; guessing is how an order
                    // gets sent twice.
                    tracing::error!(id = %client_order_id, error = %other, "send failed with no answer");
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "{client_order_id} sent with no answer ({other}); still counted as in flight"
                        ),
                    })?;
                    None
                }
            }
        };

        self.ledger
            .record(Segment::EndToEnd, clock::now_ns().saturating_sub(origin_ns));

        if let Some(update) = update {
            self.take_update(update).await?;
        }
        Ok(())
    }

    /// Every order update, wherever it came from, goes through here.
    async fn take_update(&mut self, update: OrderUpdate) -> Result<(), EngineError> {
        self.wal.append(&WalRecord::OrderUpdate {
            update: update.clone(),
        })?;
        self.risk.on_update(&update);
        self.orders.apply_update(&update);

        let now = clock::now_ns();
        let event = EngineEvent::Order(update.clone());
        match inflight::client_order_id(&update) {
            Some(id) => match self.registry.owner_of(id) {
                Some(sid) => {
                    let Engine {
                        strategies,
                        market,
                        timers,
                        pending,
                        ..
                    } = self;
                    feed_strategy(strategies, market, timers, pending, sid, &event, now);
                }
                None => {
                    let ours = self.registry.is_ours(id);
                    tracing::warn!(id, ours, "order update for an order no strategy owns");
                }
            },
            None => {
                // A stop belongs to a symbol, not to an order: tell whoever
                // watches that symbol.
                if let OrderUpdate::StopAttached { symbol, .. } = update {
                    let Engine {
                        strategies,
                        market,
                        timers,
                        pending,
                        routing,
                        ..
                    } = self;
                    for sid in routing.all_listeners(symbol) {
                        feed_strategy(strategies, market, timers, pending, sid, &event, now);
                    }
                }
            }
        }
        Ok(())
    }

    fn refuse(
        &mut self,
        client_order_id: &str,
        intent: &Intent,
        why: &str,
    ) -> Result<(), EngineError> {
        tracing::warn!(id = client_order_id, tag = %intent.tag, why, "order not sent");
        self.wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: format!("{client_order_id} not sent ({}): {why}", intent.tag),
        })?;
        Ok(())
    }

    fn reference_px(&self, symbol: SymbolId, kind: &OrderKind) -> Option<f64> {
        if let OrderKind::Limit { px, .. } = kind {
            return Some(*px);
        }
        let quote = self.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
            return Some((quote.bid_px + quote.ask_px) / 2.0);
        }
        let ticker = self.market.ticker(symbol);
        [ticker.last_px, ticker.mark_px]
            .into_iter()
            .find(|px| *px > 0.0)
    }

    fn mint_id(&mut self) -> String {
        self.next_order_n += 1;
        let id = format!("{}{}", self.registry.prefix(), self.next_order_n);
        debug_assert!(id.len() <= 36, "client order id too long: {id}");
        id
    }

    pub fn strategy_names(&self) -> &[String] {
        &self.names
    }
}

fn feed_strategy(
    strategies: &mut [Box<dyn Strategy>],
    market: &MarketState,
    timers: &mut Timers,
    pending: &mut VecDeque<Intent>,
    sid: StrategyId,
    event: &EngineEvent,
    now_ns: u64,
) {
    let Some(strategy) = strategies.get_mut(sid.0 as usize) else {
        return;
    };
    let mut ctx = Ctx {
        market,
        now_ns,
        strategy: sid,
        out: pending,
        timers,
    };
    strategy.on_event(event, &mut ctx);
}

/// When this message reached us. The whole chain is measured from here.
fn arrival_ns(event: &MarketEvent, fallback: u64) -> u64 {
    let stamp = match event {
        MarketEvent::Quote { quote, .. } => quote.recv_ns,
        MarketEvent::Ticker { ticker, .. } => ticker.recv_ns,
        MarketEvent::FeedReset { recv_ns } => *recv_ns,
    };
    if stamp == 0 {
        fallback
    } else {
        stamp
    }
}
