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
    quantize, AccountView, Action, AmendSpec, DenyReason, EngineEvent, InstrumentRule, Intent,
    MarketEvent, MarketFeed, MarketState, OrderFeed, OrderKind, OrderRequest, OrderUpdate,
    RiskKernel, RiskVerdict, StopSpec, Strategy, StrategyId, Subscription, SymbolId, SymbolTable,
    TargetBook, TimeInForce, VenueError, VenueGateway, Wal, WalError, WalRecord, WorkPolicy,
};

use crate::attribution::Attribution;
use crate::clock;
use crate::config::EngineSection;
use crate::ctx::{Ctx, Timers};
use crate::execution::{self, Fills};
use crate::heartbeat::{self, Heartbeat};
use crate::inflight::{self, LedgerOfOrders, OrderRegistry};
use crate::reconcile;
use crate::ledger::{LatencyLedger, Segment};
use crate::routing::Routing;
use crate::targets::TargetBooks;
use crate::working::{self, WorkingOrders};

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
    /// Whose each position is. The account reading is per symbol and
    /// carries no strategy on it, so this is summed from the fills of the
    /// orders each strategy placed, and rebuilt from the log at boot.
    attribution: Attribution,
    /// The resting entries this engine is advancing. Empty unless a strategy
    /// asked for one to be worked.
    working: WorkingOrders,
    /// Symbols a book has named that the engine does not follow yet.
    ///
    /// Filled while a book is being handled and drained by the run loop, which
    /// is the only place that holds the feeds. Admitting from inside
    /// `on_targets` would mean borrowing them out of the `select!` they are
    /// waiting in.
    wanted_symbols: Vec<String>,
    /// What leverage each symbol was last set to by this engine.
    ///
    /// A symbol keeps its leverage at the venue until somebody changes it, so
    /// re-sending the same number before every entry would buy a round trip
    /// per order for nothing. What makes the cache safe is forgetting a symbol
    /// the moment the account reading shows it flat: the owner trades this
    /// account by hand, and a symbol that has been closed and reopened may
    /// have been set to anything in between. The same rule the Python fleet
    /// runs under `--shared-leverage-authority`.
    leverage_at: std::collections::HashMap<SymbolId, f64>,
    ledger: LatencyLedger,
    /// What the fills cost. The latency ledger beside it measures our own side
    /// of the wire; this one measures the price, which is the half that
    /// actually shows up in the account.
    fills: Fills,
    /// The target book watcher, when one was configured. Parked here between
    /// boot and run; `run` takes it out for the length of the run, because
    /// the loop's `select!` needs it as a local — a future borrowing it out
    /// of `self` would lock out every branch that has work to do.
    targets: TargetBooks,
    /// The heartbeat file, when one was configured. Telemetry only: nothing
    /// in the loop reads it and nothing in the loop waits on it.
    heartbeat: Option<Heartbeat>,
    /// Whether boot's comparison against the venue left this engine free to
    /// add exposure. False latches: it is written into the log and read back
    /// on the next boot, so a restart cannot quietly clear it.
    may_open: bool,
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
    ///
    /// The strategies are named by their plug, which is right for a fleet
    /// where each plug runs once and wrong for this one: both sleeves here are
    /// `target_book`, so a log and a heartbeat that named the plug said
    /// "target_book" twice and left nobody able to tell carry from long. Use
    /// [`Engine::boot_as`] to give them the names their config chose.
    pub async fn boot(
        settings: &EngineSection,
        config_sha256: &str,
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        Engine::boot_as(settings, config_sha256, wal, risk, venue, strategies, &[], replayed).await
    }

    /// The same, with each sleeve's own name from its config block.
    ///
    /// `sleeves[i]` names the strategy in position `i`; a short list, or an
    /// entry that is empty, falls back to that strategy's plug name. This is
    /// what goes in the log's id table and in the heartbeat, so `engine fills`
    /// can say which sleeve's trading cost what.
    #[allow(clippy::too_many_arguments)]
    pub async fn boot_as(
        settings: &EngineSection,
        config_sha256: &str,
        mut wal: W,
        mut risk: R,
        mut venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        let orders = LedgerOfOrders::from_records(replayed);
        // Same records, same join: a restart must not forget whose
        // position is whose, or the other sleeve trades straight into it.
        let attribution = Attribution::from_records(replayed);
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
            names.push(match sleeves.get(index) {
                Some(sleeve) if !sleeve.is_empty() => sleeve.clone(),
                _ => strategy.name().to_string(),
            });
            for sub in strategy.subscriptions() {
                let symbol = market.add_symbol(&sub.symbol);
                routing.add(symbol, sub.feed, sid);
                if !subscriptions.contains(&sub) {
                    subscriptions.push(sub.clone());
                }
            }
        }
        routing.size_to(market.table.len());
        // Say what the ids mean before any record uses one. Without this every
        // later line names a number, and a log read a week later cannot say
        // which coin an order was for.
        wal.append(&names_record(&names, &market))?;

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

        // What the log believes against what the venue says. Boot is the one
        // moment the two can be compared: from here on the engine only ever
        // learns about its own orders.
        let may_open = Self::reconcile_with_venue(
            &mut wal,
            &mut venue,
            &orders,
            replayed,
            &account,
            &market.table,
            &rules,
            settings.shadow,
        )
        .await?;

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
                    // The order is already at the venue; there is nothing
                    // left to decide about how it was placed, and its
                    // leverage was set before it went.
                    work: None,
                    leverage: None,
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
            attribution,
            // Deliberately not restored from the log. The window is measured
            // from a monotonic clock that does not survive a restart, and the
            // venue's own creation time is not something this engine can ask
            // for — so a recovered order is left alone rather than worked
            // from a made-up deadline.
            working: WorkingOrders::default(),
            wanted_symbols: Vec::new(),
            leverage_at: std::collections::HashMap::new(),
            may_open,
            ledger: LatencyLedger::new(now),
            // Deliberately not rebuilt from the log at boot, unlike
            // attribution: this is a running score for the run in front of
            // you, and the whole history is one `engine fills` away.
            fills: Fills::default(),
            targets: TargetBooks::new(Vec::new()),
            heartbeat: None,
            shadow: settings.shadow,
            group_flush: Duration::from_millis(settings.group_flush_ms.max(1)),
            refresh_after_ns: settings.account_view_max_age_ms.saturating_mul(1_000_000) / 2,
            next_order_n: 0,
            orders_sent: 0,
            events_seen: 0,
            subscriptions,
        })
    }

    /// Compare the log against the venue, write down what was found, and say
    /// whether the engine may open new exposure.
    ///
    /// The latch is durable. If an earlier boot found something it could not
    /// explain and stopped opening, this one starts stopped too — a restart
    /// that cleared it would turn "stop and tell somebody" into "stop until
    /// the next crash", which is no protection at all on a process that gets
    /// restarted by a supervisor.
    ///
    /// Nothing here cancels anything. An order the engine did not place is
    /// not its to take down, and a position it cannot account for is not its
    /// to close. It says so, repairs the stops it has evidence for, and
    /// stops adding.
    #[allow(clippy::too_many_arguments)]
    async fn reconcile_with_venue(
        wal: &mut W,
        venue: &mut V,
        orders: &LedgerOfOrders,
        replayed: &[WalRecord],
        account: &AccountView,
        table: &SymbolTable,
        rules: &[Option<InstrumentRule>],
        shadow: bool,
    ) -> Result<bool, EngineError> {
        let latched = replayed.iter().rev().find_map(|record| match record {
            WalRecord::Reconciled { may_open, .. } => Some(*may_open),
            _ => None,
        });

        let working = match venue.working_orders().await {
            Ok(rows) => rows,
            Err(e) => {
                // Not knowing what the venue is working is exactly the state
                // this check exists to catch, so it is not something to
                // shrug at and carry on from.
                return Err(EngineError::Boot(format!(
                    "cannot read what the venue is working, so there is no way to tell \
                     whose orders are out there: {e}"
                )));
            }
        };

        let found = reconcile::reconcile(
            orders,
            replayed,
            &working,
            account,
            |name| table.get(name),
            |id| rules.get(id.0 as usize).and_then(|r| r.as_ref()).map(|r| r.qty_step),
        );

        for line in found.lines() {
            tracing::warn!(finding = %line, "reconciliation");
        }

        // A stop the log says belongs somewhere, that the venue does not have.
        // Putting it back is the one repair the engine can make from evidence
        // rather than from a guess.
        for (symbol, trigger_px) in found.stop_repairs() {
            if shadow {
                tracing::warn!(
                    symbol = table.name(symbol),
                    trigger_px,
                    "shadow: this position has no stop and would be given one"
                );
                continue;
            }
            match venue.set_stop(symbol, trigger_px).await {
                Ok(()) => tracing::info!(
                    symbol = table.name(symbol),
                    trigger_px,
                    "put back the stop the log says this position was opened behind"
                ),
                Err(e) => tracing::error!(
                    symbol = table.name(symbol),
                    trigger_px,
                    error = %e,
                    "could not put the stop back; the risk kernel will refuse entries while \
                     any position is unprotected"
                ),
            }
        }

        let may_open = latched.unwrap_or(true) && !found.must_not_open();
        if latched == Some(false) && !found.must_not_open() {
            tracing::error!(
                "an earlier boot stopped this engine opening new positions and nothing here \
                 clears that; it will reduce only until somebody looks at the log"
            );
        }
        if !may_open {
            tracing::error!(
                "this engine will not open new positions: the account holds orders or exposure \
                 its own log cannot account for"
            );
        }

        wal.append(&WalRecord::Reconciled {
            wall_ts_ms: clock::wall_ms(),
            findings: found.lines(),
            may_open,
        })?;
        // Durable before trading starts: a crash between here and the first
        // order must not lose a latch that was just set.
        wal.barrier()?;
        Ok(may_open)
    }

    pub fn subscriptions(&self) -> &[Subscription] {
        &self.subscriptions
    }

    /// Follow a target book. Optional: with no watcher the engine simply
    /// never hears about one, and a follower plug holds whatever it holds.
    pub fn watch_targets(&mut self, books: TargetBooks) {
        self.targets = books;
    }

    /// Say how this engine is, in a file. Optional: with no heartbeat the
    /// engine writes nothing about itself and nothing outside the process can
    /// tell whether it is well.
    pub fn write_heartbeat(&mut self, heartbeat: Heartbeat) {
        self.heartbeat = Some(heartbeat);
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
        let mut targets = std::mem::replace(&mut self.targets, TargetBooks::new(Vec::new()));
        let mut targets_open = !targets.is_empty();

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
                book = targets.next_book(), if targets_open => match book {
                    Some((strategy, book)) => self.on_targets(strategy, book).await?,
                    // Every watcher has gone. Each one's own departure was
                    // logged as it happened; this is the last of them.
                    // Nothing else changes: no book means no decision, and
                    // followers hold what they hold.
                    None => {
                        tracing::error!("every target book watcher has stopped; no further books will arrive");
                        targets_open = false;
                    }
                },
                _ = tokio::time::sleep(timer_wait.unwrap_or_default()), if timer_wait.is_some() => {
                    self.on_timers().await?;
                }
                _ = flush_tick.tick() => self.on_tick().await?,
            }

            // Outside the select!, where the feeds are borrowable again.
            if !self.wanted_symbols.is_empty() {
                self.admit_wanted(market_feed, order_feed).await?;
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
                attribution,
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
                    attribution,
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
                attribution,
                account,
                rules,
                ..
            } = self;
            feed_strategy(
                strategies, market, account, rules, timers, pending, orders, registry, attribution, sid, &event,
                now,
            );
        }
        self.drain(now).await
    }

    /// A fresh target book. Every strategy hears it, the way a feed reset is
    /// heard: which of them follows a book is the plug's business, not the
    /// loop's. Nothing arrives here unless a whole book was read, so this is
    /// only ever called with a decision in hand.
    /// One book, to the one strategy that asked for its path.
    ///
    /// Not broadcast. Two sleeves on one account each have their own book, and
    /// the venue holds one position per symbol — so a book delivered to the
    /// wrong follower is that follower trying to hold another sleeve's
    /// positions.
    /// Start following symbols a book named that the engine did not know.
    ///
    /// Every table that maps a name to a `SymbolId` has to gain the symbol in
    /// the same order, because the id is an index assigned by position. Four
    /// of them exist — the engine's own, the public feed's, the venue
    /// gateway's, and the private stream's — and if any two disagreed, an
    /// order meant for one symbol would be sent for another. So this is the
    /// only place that admits, it admits one name at a time, and it checks
    /// that all four agree before the symbol is usable. A disagreement drops
    /// the symbol rather than trading it: the engine carries on with the names
    /// it already had, and says loudly which one it refused.
    async fn admit_wanted<M, O>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
    ) -> Result<(), EngineError>
    where
        M: engine_types::MarketFeed,
        O: engine_types::OrderFeed,
    {
        let wanted = std::mem::take(&mut self.wanted_symbols);
        let mut admitted = 0usize;
        for name in wanted {
            let core_id = self.market.add_symbol(&name);
            let feed_id = market_feed.admit(&name, engine_types::Feed::Quote);
            let venue_id = self.venue.add_symbol(&name);
            if feed_id != Some(core_id) || venue_id != Some(core_id) {
                tracing::error!(
                    symbol = %name,
                    ?core_id,
                    ?feed_id,
                    ?venue_id,
                    "the parts of the engine disagree about this symbol's id; it will not be \
                     traded. Nothing else is affected — the ids already handed out do not move."
                );
                continue;
            }
            order_feed.learn(&name, core_id);
            self.routing.size_to(self.market.table.len());
            admitted += 1;
            tracing::info!(symbol = %name, id = core_id.0, "following a symbol a book named");
        }
        if admitted == 0 {
            return Ok(());
        }
        // The table grew, so say what it is now. Ids are only appended, so
        // this is the earlier one plus the new names.
        self.wal.append(&names_record(&self.names, &self.market))?;
        // One venue read covers everything admitted this pass. Without a rule
        // there is no way to quantize, so the symbol is followed but nothing
        // can be sent for it — which is the same state as a symbol whose rule
        // was missing at boot.
        self.rules.resize(self.market.table.len(), None);
        match self.venue.instrument_rules().await {
            Ok(fetched) => {
                for (name, rule) in fetched {
                    if let Some(id) = self.market.table.get(&name) {
                        self.rules[id.0 as usize] = Some(rule);
                    }
                }
            }
            Err(e) => tracing::warn!(
                error = %e,
                "no instrument rules for the symbols just taken on; they cannot trade until \
                 the next attempt"
            ),
        }
        Ok(())
    }

    /// Take a fresh account reading.
    ///
    /// The one place a reading is adopted, so what has to happen with it
    /// cannot be done in one path and forgotten in the other.
    fn adopt_view(&mut self, view: AccountView) {
        forget_leverage_where_flat(&mut self.leverage_at, &view.positions);
        self.account = view;
        // The loss guard's daily anchor rolls on the READING's UTC day, so
        // hand it the reading's wall time.
        self.risk
            .observe_wall_clock_ns((clock::wall_ms() as u64).saturating_mul(1_000_000));
    }

    /// Make the venue agree that this symbol sits at this leverage, before an
    /// order that would post margin against it.
    ///
    /// Margin is notional divided by leverage, so an order sized at one
    /// leverage and filled at another does not commit the capital the risk
    /// kernel priced. Unknown is not "probably fine": every failure here
    /// refuses the order rather than sending it and hoping.
    async fn ensure_leverage(&mut self, symbol: SymbolId, want: f64) -> Result<(), String> {
        if !want.is_finite() || want <= 0.0 {
            return Err(format!(
                "the decision asks for leverage {want}, which is not a leverage"
            ));
        }
        if !self.venue.caps().set_leverage {
            return Err(format!(
                "this decision was sized at leverage {want}, and this venue cannot be told                  what leverage to use — the margin it would post is not the margin it was                  sized at"
            ));
        }
        if self.leverage_at.get(&symbol).is_some_and(|at| *at == want) {
            return Ok(());
        }
        match self.venue.set_leverage(symbol, want).await {
            Ok(()) => {
                self.leverage_at.insert(symbol, want);
                Ok(())
            }
            Err(e) => Err(format!("could not set leverage to {want}: {e}")),
        }
    }

    async fn on_targets(
        &mut self,
        strategy: StrategyId,
        book: TargetBook,
    ) -> Result<(), EngineError> {
        let now = clock::now_ns();
        tracing::info!(
            strategy = strategy.0,
            source = %book.source,
            targets = book.targets.len(),
            valid_until_ms = book.valid_until_ms,
            "a target book reached its strategy"
        );
        // A book may name something this engine has never followed. Note it
        // for the run loop, which can reach the feeds; the strategy is still
        // woken now, and will act on the name once it has a price.
        for target in &book.targets {
            if self.market.table.get(&target.symbol).is_none()
                && !self.wanted_symbols.contains(&target.symbol)
            {
                self.wanted_symbols.push(target.symbol.clone());
            }
        }

        let event = EngineEvent::Targets(book);
        {
            let Engine {
                strategies,
                market,
                timers,
                pending,
                orders,
                registry,
                attribution,
                account,
                rules,
                ..
            } = self;
            feed_strategy(
                strategies,
                market,
                account,
                rules,
                timers,
                pending,
                orders,
                registry,
                attribution,
                strategy,
                &event,
                now,
            );
        }
        self.drain(now).await
    }

    async fn on_tick(&mut self) -> Result<(), EngineError> {
        self.wal.flush()?;
        let now = clock::now_ns();
        // First, and on this tick rather than on a market message: it is the
        // cheapest point in the tick, and it is in front of the account
        // refresh below, which is a venue round trip.
        self.beat(now);
        if self.ledger.due(now) {
            let record = self.ledger.record_for_wal(now);
            self.wal.append(&record)?;
            tracing::info!("latency, {}", self.ledger.plain_line(now));
            self.ledger.reset(now);
        }
        // Any markout whose horizon has come round. Written down because a log
        // holds no prices: this is the one execution number that cannot be
        // worked out later from the records already in it.
        for mark in self.fills.due(now, &self.market) {
            self.wal.append(&mark.to_record())?;
        }
        if now.saturating_sub(self.account.observed_ns) >= self.refresh_after_ns {
            match self.venue.account_view().await {
                Ok(view) => {
                    self.adopt_view(view);
                }
                // Keeping the old reading is not the same as trusting it: it
                // ages, and the risk kernel refuses on an old reading.
                Err(e) => tracing::warn!(error = %e, "could not refresh the account reading"),
            }
        }

        // Every resting entry gets one look. Read the clock again: the
        // account refresh above is a venue round trip, and the stamp from
        // before it is old by the time we get here.
        let now = clock::now_ns();
        {
            let Engine {
                working,
                market,
                rules,
                orders,
                pending,
                ..
            } = self;
            working.pass(now, market, rules, orders, pending);
        }
        // Through the ordinary queue, so the flood cap counts these too.
        self.drain(now).await
    }

    /// Write the heartbeat file, when one was asked for and its own cadence
    /// has come round.
    ///
    /// Nothing here returns an error, because there is nothing an error here
    /// should change: the file is how something outside tells whether this
    /// engine is well, and an engine that stopped trading because it could
    /// not describe itself would be a worse answer than one nobody can see.
    fn beat(&mut self, now_ns: u64) {
        let Engine {
            heartbeat,
            ledger,
            fills,
            names,
            may_open,
            shadow,
            events_seen,
            orders_sent,
            account,
            market,
            ..
        } = self;
        let Some(heartbeat) = heartbeat.as_mut() else {
            return;
        };
        if !heartbeat.due(now_ns) {
            return;
        }
        // Named, because the producers that read this file know symbols by
        // name and nothing else. Flat rows are dropped the way every other
        // reader of this view drops them: flat is not a holding.
        let holdings: Vec<(String, engine_types::Side, f64, f64)> = account
            .positions
            .iter()
            .filter(|p| p.qty > 0.0)
            .map(|p| {
                (
                    market.table.name(p.symbol).to_string(),
                    p.side,
                    p.qty,
                    p.entry_px,
                )
            })
            .collect();
        // Rolled up once, here, rather than kept as a running total: the
        // per-sleeve rows are what the ledger is for, and adding them up is
        // cheaper than keeping a second copy correct.
        let costs = fills.total();
        heartbeat.write(
            now_ns,
            &heartbeat::Facts {
                shadow: *shadow,
                may_open: *may_open,
                market_events: *events_seen,
                orders_sent: *orders_sent,
                strategies: names,
                decide: ledger.quantiles(Segment::Decide),
                wire: ledger.quantiles(Segment::Wire),
                equity_usdt: account.equity_usdt,
                available_usdt: account.available_usdt,
                // The age, not the stamp: this engine's clock is monotonic
                // and means nothing outside this process.
                account_age_ns: (account.observed_ns != 0)
                    .then(|| now_ns.saturating_sub(account.observed_ns)),
                holdings: &holdings,
                costs: &costs,
            },
        );
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
                } => {
                    let taken = self
                        .process_cancel(symbol, &client_order_id, origin_ns)
                        .await?;
                    // The supervisor may be waiting on this answer, and it
                    // only latches "pulled" on one it got.
                    self.working.cancelled(&client_order_id, taken);
                }
                Action::Amend {
                    symbol,
                    client_order_id,
                    spec,
                } => {
                    let taken = self
                        .process_amend(symbol, &client_order_id, spec, origin_ns)
                        .await?;
                    self.working
                        .amended(&client_order_id, spec.px, taken, clock::now_ns());
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

        // The strategy's own words, work policy included, before the engine
        // touches anything.
        self.wal.append(&WalRecord::Intent {
            intent: intent.clone(),
        })?;

        // Boot found orders or exposure this log cannot account for, which
        // means somebody else is on this account and every number the kernel
        // works from is measuring their trading too. Reducing is still
        // allowed — taking exposure off is safe whoever put it on — but
        // nothing new is added until an operator has looked.
        if !self.may_open && !intent.reduce_only {
            let verdict = RiskVerdict::Deny {
                reason: DenyReason::UnknownState {
                    detail: "boot could not account for what this account holds".to_string(),
                },
            };
            self.wal.append(&WalRecord::Verdict {
                client_order_id: None,
                verdict,
            })?;
            tracing::warn!(
                tag = %intent.tag,
                "refused: this engine is not opening new positions after what boot found"
            );
            return Ok(());
        }

        // An entry the strategy asked to have worked starts as a resting
        // limit instead of crossing the spread. Rewritten here, before the
        // kernel judges it, so the kernel judges the order that is actually
        // sent — and before the id is minted, so a refusal still costs
        // nothing.
        let mut intent = intent;
        let work = self.plan_resting_entry(&mut intent);

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

        // Before the durable record, because a leverage that could not be set
        // means this order must not go at all — and an OrderSent record is
        // the engine saying it is about to put one on the wire.
        //
        // Entries only. An exit at the wrong leverage is still an exit, and
        // making it wait on a round trip would be the wrong trade. Shadow runs
        // send nothing, so there is nothing to set.
        if !self.shadow && !intent.reduce_only {
            if let Some(want) = intent.leverage {
                if let Err(reason) = self.ensure_leverage(request.symbol, want).await {
                    return self.refuse(&client_order_id, &intent, &reason);
                }
            }
        }

        // Durable before the wire. Everything above this line can be lost by
        // a crash without consequence; everything below it cannot. In shadow
        // nothing reaches the wire, so no fsync — and no barrier BETWEEN the
        // order record and its "no send" note, which once let a crash leave
        // a durable order with no note: a phantom in-flight on replay.
        let sent_record = WalRecord::OrderSent {
            request: request.clone(),
            wire_ns: clock::now_ns(),
            // `M0`. Read here rather than at the fill because this is the only
            // moment it exists: a worked entry can rest for a minute, and by
            // the time it fills the price it was decided against is gone.
            // Zero when the book was unreadable, which makes every arrival
            // number for this order missing rather than flattering.
            arrival_mid: self.decision_mid(request.symbol),
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

        // Start working it from the price that is actually resting — the
        // quantized one, not the one the planner asked for.
        if let (Some(policy), OrderKind::Limit { px, .. }) = (work, request.kind) {
            let mid = self.decision_mid(request.symbol);
            let state = working::plan::WorkState::new(request.side, px, mid, clock::now_ns());
            self.working
                .take_on(&client_order_id, request.symbol, policy, state);
        }

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
    ///
    /// True means the change went through: the venue took it, or shadow mode
    /// deliberately skipped the wire. False means the resting order is
    /// untouched, and whoever asked has to ask again.
    async fn process_cancel(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        _origin_ns: u64,
    ) -> Result<bool, EngineError> {
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
            return Ok(true);
        }

        match self.venue.cancel_order(symbol, client_order_id).await {
            Ok(()) => Ok(true),
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
                Ok(false)
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
                Ok(false)
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

    /// True when the change went through; see [`process_cancel`] for what the
    /// answer means.
    ///
    /// [`process_cancel`]: Engine::process_cancel
    async fn process_amend(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
        _origin_ns: u64,
    ) -> Result<bool, EngineError> {
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
            return Ok(false);
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
            return Ok(true);
        }

        match self.venue.amend_order(symbol, client_order_id, spec).await {
            Ok(()) => Ok(true),
            Err(VenueError::BadRequest(detail)) => {
                tracing::error!(id = client_order_id, detail, "amend never sent");
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("amend of {client_order_id} never sent: {detail}"),
                })?;
                Ok(false)
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
                Ok(false)
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
        // Whose fill it was, before any strategy is woken, so the one that
        // placed the order sees its own position already changed. The ledger
        // is asked rather than the registry: the registry knows only this
        // boot's ids and the ones in flight when it started, and a fill can
        // still arrive for an order older than either.
        if let Some(id) = inflight::client_order_id(&update) {
            match self.orders.owner_of(id) {
                Some(sid) => {
                    self.attribution.on_update(sid, &update);
                    self.price_fill(sid, &update);
                }
                // Charged to nobody on purpose. `reconcile` is what notices
                // the account holds more than the log accounts for, and it
                // already stops the engine opening on top of it.
                None if matches!(update, OrderUpdate::Fill { .. }) => tracing::warn!(
                    id,
                    "a fill for an order this log never recorded sending; it is charged to \
                     no strategy"
                ),
                None => {}
            }
        }

        // A private-stream gap may have swallowed fills. Refresh the account
        // reading now rather than trusting exposure across the gap.
        if let OrderUpdate::StreamReset { .. } = update {
            match self.venue.account_view().await {
                Ok(view) => self.adopt_view(view),
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
                        attribution,
                        account,
                        rules,
                        ..
                    } = self;
                    feed_strategy(
                        strategies, market, account, rules, timers, pending, orders, registry, attribution, sid,
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
                        attribution,
                        account,
                        rules,
                        ..
                    } = self;
                    for sid in routing.all_listeners(symbol) {
                        feed_strategy(
                            strategies, market, account, rules, timers, pending, orders, registry, attribution,
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

    /// Turn an entry the strategy asked to have worked into the resting limit
    /// it should start as, and say whether it will be worked at all.
    ///
    /// `None` leaves the intent exactly as the strategy wrote it: no policy,
    /// an exit, a symbol with no instrument rule, or a spread too thin for
    /// resting to pay for itself.
    fn plan_resting_entry(&self, intent: &mut Intent) -> Option<WorkPolicy> {
        let rule = self.rules.get(intent.symbol.0 as usize).copied().flatten()?;
        let touch = self
            .market
            .quotes
            .get(intent.symbol.0 as usize)
            .map(working::touch_of)
            .unwrap_or_default();
        match working::plan::opening(intent, touch, &rule) {
            working::plan::Opening::AsWritten => None,
            working::plan::Opening::WorkAsPriced { policy } => Some(policy),
            working::plan::Opening::Rest { px, policy } => {
                // Good-till-cancelled, not post-only. The overnight lab that
                // first measured resting ran post-only into the demo realm's
                // pretend internal liquidity, which flattered it; the numbers
                // this recipe is built on are GTC numbers.
                intent.kind = OrderKind::Limit {
                    px,
                    tif: TimeInForce::Gtc,
                };
                Some(policy)
            }
        }
    }

    /// The mid this order was decided against, or zero when the book was not
    /// two-sided. Only the early cross reads it, and it stays off at zero.
    fn decision_mid(&self, symbol: SymbolId) -> f64 {
        let quote = self.market.quote(symbol);
        if quote.bid_px > 0.0 && quote.ask_px > quote.bid_px {
            (quote.bid_px + quote.ask_px) / 2.0
        } else {
            0.0
        }
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

    /// Price one fill against the book that was on the screen when its order
    /// left, and start its markout clock.
    ///
    /// The anchor comes off the order ledger rather than out of memory,
    /// because the ledger is rebuilt from the log at boot: a fill for an order
    /// sent before a restart is still priced against the right midpoint.
    fn price_fill(&mut self, strategy: StrategyId, update: &OrderUpdate) {
        let OrderUpdate::Fill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            is_maker,
            venue_ts_ms,
            ..
        } = update
        else {
            return;
        };
        let arrival_mid = self
            .orders
            .orders
            .get(client_order_id)
            .map(|order| order.arrival_mid)
            .unwrap_or(0.0);
        self.fills.on_fill(
            &execution::Fill {
                client_order_id: client_order_id.clone(),
                strategy,
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                is_maker: *is_maker,
                arrival_mid,
                venue_ts_ms: *venue_ts_ms,
            },
            clock::now_ns(),
        );
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

    /// What the fills have cost so far this run.
    pub fn fills(&self) -> &Fills {
        &self.fills
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
    attribution: &Attribution,
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
        attribution,
    };
    strategy.on_event(event, &mut ctx);
}

/// Drop the remembered leverage of every symbol the reading shows flat.
///
/// A symbol with no position may be reopened at any leverage by anyone holding
/// a key, and the owner trades the funded account by hand. Remembering what we
/// last set it to would then be remembering something that is no longer true,
/// and the next entry would skip the call that would have corrected it.
///
/// A symbol still open keeps its entry: its leverage cannot be changed at the
/// venue while a position is on it.
pub(crate) fn forget_leverage_where_flat(
    leverage_at: &mut std::collections::HashMap<SymbolId, f64>,
    positions: &[engine_types::risk::PositionView],
) {
    leverage_at.retain(|symbol, _| positions.iter().any(|p| p.symbol == *symbol));
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

/// Both id tables as a log record, so every number in the log can be turned
/// back into a sleeve and a coin.
fn names_record(strategies: &[String], market: &MarketState) -> WalRecord {
    WalRecord::Names {
        strategies: strategies.to_vec(),
        symbols: (0..market.table.len())
            .map(|i| market.table.name(SymbolId(i as u16)).to_string())
            .collect(),
    }
}
