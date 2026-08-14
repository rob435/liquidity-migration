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
//! - the research system's target book, when one is being watched
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
    quantize, AccountView, Action, AmendSpec, EngineEvent, InstrumentRule, Intent, MarketEvent,
    MarketFeed, MarketState, OrderFeed, OrderKind, OrderRequest, OrderUpdate, RiskKernel,
    RiskVerdict, StopSpec, Strategy, StrategyId, Subscription, SymbolId, TargetBook, VenueError,
    VenueGateway, Wal, WalError, WalRecord,
};

use crate::clock;
use crate::config::EngineSection;
use crate::ctx::{Ctx, Timers};
use crate::inflight::{self, LedgerOfOrders, OrderRegistry};
use crate::ledger::{LatencyLedger, Segment};
use crate::routing::Routing;
use crate::targets::TargetBookWatcher;

/// A strategy that emits from every order update it hears could keep the loop
/// busy forever. One wake handles this many actions; past that only the ones
/// that reduce risk keep flowing, and the loop goes back to reading the
/// market.
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
    pending: VecDeque<Action>,
    account: AccountView,
    registry: OrderRegistry,
    orders: LedgerOfOrders,
    ledger: LatencyLedger,
    /// The target book watcher, when one was configured. Parked here between
    /// boot and run; `run` takes it out for the length of the run, because
    /// the loop's `select!` needs it as a local — a future borrowing it out
    /// of `self` would lock out every branch that has work to do.
    targets: Option<TargetBookWatcher>,
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
        mut risk: R,
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

        // The newest control anchor in the log is the loss guard's memory:
        // restored before anything is judged, so a restart can neither
        // refresh the day's loss budget nor clear a latched trip.
        if let Some(state) = replayed.iter().rev().find_map(|record| match record {
            WalRecord::ControlAnchor { state, .. } => Some(state.as_str()),
            _ => None,
        }) {
            risk.restore_control_anchor(state);
        }

        let account = venue.account_view().await?;
        // The reading's wall time, so the loss guard anchors on the right
        // UTC day from the first evaluation.
        risk.observe_wall_clock_ns((clock::wall_ms() as u64).saturating_mul(1_000_000));

        let mut registry = OrderRegistry::new(format!("eng-{boot_ms}-"));
        for order in orders.in_flight() {
            registry.own(&order.request.client_order_id, order.request.strategy);
            // The kernel's partition must keep charging last boot's working
            // orders, or a restart hands every share out twice.
            let request = &order.request;
            risk.register_order(
                &request.client_order_id,
                &Intent {
                    strategy: request.strategy,
                    symbol: request.symbol,
                    side: request.side,
                    qty: request.qty,
                    kind: request.kind,
                    stop: request.stop,
                    reduce_only: request.reduce_only,
                    tag: "recovered".to_string(),
                    decided_ns: 0,
                },
                request.qty,
            );
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
            targets: None,
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

    /// Follow a target book. Optional: with no watcher the engine simply
    /// never hears about one, and a follower plug holds whatever it holds.
    pub fn watch_targets(&mut self, watcher: TargetBookWatcher) {
        self.targets = Some(watcher);
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
        // Out of `self` for the length of the run: a select! branch waiting
        // on it must not borrow the engine the other branches need.
        let mut targets = self.targets.take();
        let mut targets_open = targets.is_some();

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
                book = next_book(&mut targets), if targets_open => match book {
                    Some(book) => self.on_targets(book).await?,
                    // The worker only stops when it is dropped, so this is a
                    // surprise worth a loud line. Nothing else changes: no
                    // book means no decision, and followers hold.
                    None => {
                        tracing::error!("the target book watcher stopped; no further books will arrive");
                        targets_open = false;
                    }
                },
                _ = tokio::time::sleep(timer_wait.unwrap_or_default()), if timer_wait.is_some() => {
                    self.on_timers().await?;
                }
                _ = flush_tick.tick() => self.on_tick().await?,
            }
        }

        self.targets = targets;
        self.finish().await?;
        Ok(RunOutcome {
            stopped_by,
            market_events: self.events_seen,
            orders_sent: self.orders_sent,
        })
    }

    /// Last ledger line on the way out, and the whole tail forced to disk:
    /// a graceful stop that leaves its closing updates in the page cache
    /// tells the next boot's audit a lie.
    pub async fn finish(&mut self) -> Result<(), EngineError> {
        let now = clock::now_ns();
        let record = self.ledger.record_for_wal(now);
        self.wal.append(&record)?;
        self.wal.barrier()?;
        tracing::info!("latency, {}", self.ledger.plain_line(now));
        Ok(())
    }

    async fn on_market(&mut self, event: MarketEvent) -> Result<(), EngineError> {
        let now = clock::now_ns();
        self.market.apply(&event);
        match event {
            MarketEvent::Quote { symbol, quote } if quote.bid_px > 0.0 && quote.ask_px > 0.0 => {
                self.risk
                    .observe_price(symbol, (quote.bid_px + quote.ask_px) / 2.0);
            }
            MarketEvent::Ticker { symbol, ticker } if ticker.last_px > 0.0 => {
                self.risk.observe_price(symbol, ticker.last_px);
            }
            _ => {}
        }
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
                orders,
                registry,
                account,
                rules,
                ..
            } = self;
            let count = strategies.len();
            let mut feed = |sid| {
                feed_strategy(
                    strategies,
                    market,
                    account,
                    rules,
                    timers,
                    pending,
                    orders,
                    registry,
                    sid,
                    &engine_event,
                    now,
                )
            };
            match event {
                MarketEvent::Quote { symbol, .. } => {
                    for sid in routing.quote_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::Ticker { symbol, .. } => {
                    for sid in routing.ticker_listeners(symbol) {
                        feed(*sid);
                    }
                }
                MarketEvent::FeedReset { .. } => {
                    for index in 0..count {
                        feed(StrategyId(index as u16));
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
                orders,
                registry,
                account,
                rules,
                ..
            } = self;
            feed_strategy(
                strategies, market, account, rules, timers, pending, orders, registry, sid, &event,
                now,
            );
        }
        self.drain(now).await
    }

    /// A fresh target book. Every strategy hears it, the way a feed reset is
    /// heard: which of them follows a book is the plug's business, not the
    /// loop's. Nothing arrives here unless a whole book was read, so this is
    /// only ever called with a decision in hand.
    async fn on_targets(&mut self, book: TargetBook) -> Result<(), EngineError> {
        let now = clock::now_ns();
        tracing::info!(
            source = %book.source,
            targets = book.targets.len(),
            valid_until_ms = book.valid_until_ms,
            "a target book reached the strategies"
        );
        let event = EngineEvent::Targets(book);
        {
            let Engine {
                strategies,
                market,
                timers,
                pending,
                orders,
                registry,
                account,
                rules,
                ..
            } = self;
            for index in 0..strategies.len() {
                feed_strategy(
                    strategies,
                    market,
                    account,
                    rules,
                    timers,
                    pending,
                    orders,
                    registry,
                    StrategyId(index as u16),
                    &event,
                    now,
                );
            }
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
                Ok(view) => {
                    self.account = view;
                    // The loss guard's daily anchor rolls on the READING's
                    // UTC day, so hand it the reading's wall time.
                    self.risk
                        .observe_wall_clock_ns((clock::wall_ms() as u64).saturating_mul(1_000_000));
                }
                // Keeping the old reading is not the same as trusting it: it
                // ages, and the risk kernel refuses on an old reading.
                Err(e) => tracing::warn!(error = %e, "could not refresh the account reading"),
            }
        }
        self.persist_control_anchor()
    }

    async fn drain(&mut self, origin_ns: u64) -> Result<(), EngineError> {
        let mut handled = 0usize;
        let mut adding_dropped = 0usize;
        while let Some(action) = self.pending.pop_front() {
            handled += 1;
            // Past the cap, whatever adds risk is dropped but whatever sheds
            // it still flows: an exit or a cancel queued behind a flood must
            // get out, or its strategy is stranded holding a position — or an
            // order — it believes it is rid of. An amend counts as adding: it
            // can raise the size of a resting order. The hard cap bounds even
            // the de-risking ones against a runaway loop.
            if handled > MAX_INTENTS_PER_WAKE && !action.is_risk_reducing() {
                adding_dropped += 1;
                continue;
            }
            if handled > MAX_INTENTS_PER_WAKE * 4 {
                let dropped = self.pending.len() + 1;
                self.pending.clear();
                tracing::error!(dropped, "far too many actions in one wake; the rest were dropped");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("dropped {dropped} actions, exits included: more than {} in one wake", MAX_INTENTS_PER_WAKE * 4),
                })?;
                break;
            }
            match action {
                Action::Place(intent) => self.process_intent(intent, origin_ns).await?,
                Action::Cancel {
                    symbol,
                    client_order_id,
                } => self.process_cancel(symbol, &client_order_id, origin_ns).await?,
                Action::Amend {
                    symbol,
                    client_order_id,
                    spec,
                } => {
                    self.process_amend(symbol, &client_order_id, spec, origin_ns)
                        .await?
                }
            }
        }
        if adding_dropped > 0 {
            tracing::error!(adding_dropped, "too many actions in one wake; entries and amends were dropped");
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "dropped {adding_dropped} entries and amends: more than {MAX_INTENTS_PER_WAKE} actions in one wake (exits and cancels still flowed)"
                ),
            })?;
        }
        self.persist_control_anchor()
    }

    /// Write a changed control anchor down and force it to disk. Rare (a
    /// day roll or a trip), and a trip that is not durable is a trip a
    /// crash-loop can forget.
    fn persist_control_anchor(&mut self) -> Result<(), EngineError> {
        if let Some(state) = self.risk.take_control_anchor() {
            self.wal.append(&WalRecord::ControlAnchor {
                source: "risk".into(),
                state,
            })?;
            self.wal.barrier()?;
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

        // A non-finite number would be written to the log as null and stop
        // the next boot's replay dead, so it is refused before any append.
        if let Some(what) = unreal_number(&intent) {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("intent {} refused: {what} is not a finite number", intent.tag),
            })?;
            tracing::error!(tag = %intent.tag, what, "intent carries an unreal number");
            return Ok(());
        }

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

        // The risk kernel requires a position-opening intent to carry a stop.
        // A venue that keeps no stop of its own would leave that rule
        // unenforced without ever saying so: the order goes out, the log
        // records a stop, and nothing at the venue is watching the position.
        // An exit sheds its stop below in any case, so it is not held back.
        if intent.stop.is_some() && !intent.reduce_only && !self.venue.caps().native_position_stop {
            return self.refuse(
                &client_order_id,
                &intent,
                "the intent carries a stop and this venue keeps none",
            );
        }

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
            // The venue rejects a reduce-only order that carries stop
            // fields, so an exit sheds its stop here — the log records what
            // is actually sent. An entry's stop is quantized against the
            // instrument tick, rounded toward triggering sooner.
            stop: if intent.reduce_only {
                None
            } else {
                intent.stop.map(|s| StopSpec {
                    trigger_px: quantize::quantize_px(s.trigger_px, intent.side.flipped(), &rule),
                })
            },
            reduce_only: intent.reduce_only,
        };

        // Durable before the wire. Everything above this line can be lost by
        // a crash without consequence; everything below it cannot. In shadow
        // nothing reaches the wire, so no fsync — and no barrier BETWEEN the
        // order record and its "no send" note, which once let a crash leave
        // a durable order with no note: a phantom in-flight on replay.
        let sent_record = WalRecord::OrderSent {
            request: request.clone(),
            wire_ns: clock::now_ns(),
        };
        self.wal.append(&sent_record)?;
        if !self.shadow {
            self.wal.barrier()?;
        }
        self.ledger
            .record(Segment::Durable, clock::now_ns().saturating_sub(decided_ns));
        self.orders.apply(&sent_record);
        self.registry.own(&client_order_id, intent.strategy);
        self.risk.register_order(&client_order_id, &intent, qty);
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
                // A request that could not be built never left the box:
                // certainty, not doubt. Ending it frees its reservation and
                // tells the strategy, instead of a phantom in-flight order.
                Err(VenueError::BadRequest(detail)) => Some(OrderUpdate::Reject {
                    client_order_id: client_order_id.clone(),
                    code: 0,
                    reason: format!("never sent: {detail}"),
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

    /// Pull a resting order.
    ///
    /// Not the order path in miniature: there is no barrier before the wire.
    /// A cancel adds no exposure, and an order the log still shows working is
    /// recovered at the next boot whether or not the cancel survived a crash
    /// — so the fsync would buy nothing. `origin_ns` is taken but not
    /// recorded: the latency ledger measures the order path, and mixing a
    /// barrier-free cancel into "out the door" would flatter that number.
    async fn process_cancel(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        _origin_ns: u64,
    ) -> Result<(), EngineError> {
        self.wal.append(&WalRecord::CancelSent {
            symbol,
            client_order_id: client_order_id.to_string(),
            wire_ns: clock::now_ns(),
        })?;

        if self.shadow {
            // The never-sent marker is read back as an ending for the order
            // id that follows it, so these words name the cancel and not the
            // order it would have pulled: that order's fate is whatever the
            // log already says it is.
            self.wal.append(&WalRecord::Note {
                source: "shadow".into(),
                text: format!(
                    "{}cancel of {client_order_id} on {}",
                    inflight::NEVER_SENT_PREFIX,
                    self.market.table.name(symbol)
                ),
            })?;
            return Ok(());
        }

        match self.venue.cancel_order(symbol, client_order_id).await {
            Ok(()) => Ok(()),
            // A request that could not be built never left the box: the
            // resting order is untouched, which is certainty rather than
            // doubt. No synthetic reject — that would end an order at the
            // venue's expense in our own book only.
            Err(VenueError::BadRequest(detail)) => {
                tracing::error!(id = client_order_id, detail, "cancel never sent");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("cancel of {client_order_id} never sent: {detail}"),
                })?;
                Ok(())
            }
            Err(other) => {
                // We do not know whether the venue got it. The order stays in
                // flight either way, and the private stream will say if it
                // went; a retry from here is how one gets cancelled twice.
                tracing::error!(id = client_order_id, error = %other, "cancel failed with no answer");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "cancel of {client_order_id} sent with no answer ({other}); the order is still counted as working"
                    ),
                })?;
                Ok(())
            }
        }
    }

    /// Reprice or resize a resting order in place. See [`process_cancel`] for
    /// why neither of these two paths barriers before the wire.
    ///
    /// [`process_cancel`]: Engine::process_cancel
    /// Whether an amend would leave more size working than the log currently
    /// says. An order the log does not know is treated as growing: not
    /// knowing is not the same as knowing it is smaller.
    fn grows_the_order(&self, client_order_id: &str, spec: &AmendSpec) -> bool {
        let Some(new_qty) = spec.qty else {
            return false;
        };
        match self.orders.orders.get(client_order_id) {
            Some(order) => new_qty > order.request.qty,
            None => true,
        }
    }

    async fn process_amend(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
        _origin_ns: u64,
    ) -> Result<(), EngineError> {
        if !self.venue.caps().amend_in_place {
            // No quiet fallback to cancel-and-replace. A replaced order is a
            // new order at the back of the queue at a fresh price — a
            // different trade from the one asked for, and the strategy would
            // never learn it had been substituted.
            tracing::warn!(id = client_order_id, "this venue cannot amend; the order is left alone");
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: this venue cannot change a resting order in place, and cancel-and-replace is a different trade"
                ),
            })?;
            return Ok(());
        }

        // An amend that raises the size adds exposure, so it is durable
        // before the wire for the same reason a send is: a crash in between
        // must never leave the log describing a smaller order than the one
        // the venue is now working. Repricing, or shrinking, cannot create
        // exposure and rides the ordinary group flush.
        let grows = self.grows_the_order(client_order_id, &spec);
        self.wal.append(&WalRecord::AmendSent {
            symbol,
            client_order_id: client_order_id.to_string(),
            spec,
            wire_ns: clock::now_ns(),
        })?;
        if grows && !self.shadow {
            self.wal.barrier()?;
        }

        if self.shadow {
            self.wal.append(&WalRecord::Note {
                source: "shadow".into(),
                text: format!(
                    "{}amend of {client_order_id} on {}",
                    inflight::NEVER_SENT_PREFIX,
                    self.market.table.name(symbol)
                ),
            })?;
            return Ok(());
        }

        match self.venue.amend_order(symbol, client_order_id, spec).await {
            Ok(()) => Ok(()),
            Err(VenueError::BadRequest(detail)) => {
                tracing::error!(id = client_order_id, detail, "amend never sent");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("amend of {client_order_id} never sent: {detail}"),
                })?;
                Ok(())
            }
            Err(other) => {
                // The order is still working; we just do not know at which
                // price or size. The next account or order update settles it.
                tracing::error!(id = client_order_id, error = %other, "amend failed with no answer");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "amend of {client_order_id} sent with no answer ({other}); its price and size are unconfirmed"
                    ),
                })?;
                Ok(())
            }
        }
    }

    /// Every order update, wherever it came from, goes through here.
    async fn take_update(&mut self, update: OrderUpdate) -> Result<(), EngineError> {
        self.wal.append(&WalRecord::OrderUpdate {
            update: update.clone(),
        })?;
        self.risk.on_update(&update);
        self.orders.apply_update(&update);

        // A private-stream gap may have swallowed fills. Refresh the account
        // reading now rather than trusting exposure across the gap.
        if let OrderUpdate::StreamReset { .. } = update {
            match self.venue.account_view().await {
                Ok(view) => {
                    self.account = view;
                    self.risk
                        .observe_wall_clock_ns((clock::wall_ms() as u64).saturating_mul(1_000_000));
                }
                Err(e) => {
                    tracing::warn!(error = %e, "no fresh account reading after a stream gap");
                }
            }
        }

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
                        orders,
                        registry,
                        account,
                        rules,
                        ..
                    } = self;
                    feed_strategy(
                        strategies, market, account, rules, timers, pending, orders, registry, sid,
                        &event, now,
                    );
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
                        orders,
                        registry,
                        account,
                        rules,
                        ..
                    } = self;
                    for sid in routing.all_listeners(symbol) {
                        feed_strategy(
                            strategies, market, account, rules, timers, pending, orders, registry,
                            sid, &event, now,
                        );
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
        let orders = &self.orders;
        mint_unused(self.registry.prefix(), &mut self.next_order_n, |candidate| {
            orders.contains(candidate)
        })
    }

    pub fn strategy_names(&self) -> &[String] {
        &self.names
    }
}

#[allow(clippy::too_many_arguments)]
fn feed_strategy(
    strategies: &mut [Box<dyn Strategy>],
    market: &MarketState,
    account: &AccountView,
    rules: &[Option<InstrumentRule>],
    timers: &mut Timers,
    pending: &mut VecDeque<Action>,
    orders: &LedgerOfOrders,
    registry: &OrderRegistry,
    sid: StrategyId,
    event: &EngineEvent,
    now_ns: u64,
) {
    let Some(strategy) = strategies.get_mut(sid.0 as usize) else {
        return;
    };
    let mut ctx = Ctx {
        market,
        account,
        rules,
        now_ns,
        strategy: sid,
        out: pending,
        timers,
        orders,
        registry,
    };
    strategy.on_event(event, &mut ctx);
}

/// One receive on the book watcher, or a future that never resolves when
/// there is nothing to watch. The loop guards this branch anyway; the
/// pending arm means the function is honest on its own.
async fn next_book(watcher: &mut Option<TargetBookWatcher>) -> Option<TargetBook> {
    match watcher {
        Some(watcher) => watcher.next_book().await,
        None => std::future::pending().await,
    }
}

/// When this message reached us. The whole chain is measured from here.
/// Mint the next client order id, skipping any the log already knows: the
/// boot prefix comes from a wall clock, and a clock stepped back must not
/// let a new order overwrite a recovered one's ledger entry.
pub(crate) fn mint_unused(
    prefix: &str,
    next_n: &mut u64,
    taken: impl Fn(&str) -> bool,
) -> String {
    loop {
        *next_n += 1;
        let id = format!("{prefix}{next_n}");
        assert!(id.len() <= 36, "client order id too long: {id}");
        if !taken(&id) {
            return id;
        }
    }
}

/// The first non-finite number an intent carries, named, or None.
fn unreal_number(intent: &Intent) -> Option<&'static str> {
    if !intent.qty.is_finite() {
        return Some("quantity");
    }
    if let OrderKind::Limit { px, .. } = intent.kind {
        if !px.is_finite() {
            return Some("limit price");
        }
    }
    if let Some(stop) = intent.stop {
        if !stop.trigger_px.is_finite() {
            return Some("stop price");
        }
    }
    None
}

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
