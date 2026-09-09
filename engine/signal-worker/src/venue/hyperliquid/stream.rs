//! Hyperliquid's public WebSocket, with the observable behaviour of every
//! other venue's public stream: one epoch per dial, a gap open until the
//! subscriptions are live, a bounded ticker cache whose fields age
//! independently, and confirmed hourly bars on the event queue.
//!
//! Two things differ from Bybit's socket and shape the code below.
//!
//! - **Every subscription is its own message and its own reply.** The venue
//!   answers a `subscribe` with `subscriptionResponse` echoing it, or with an
//!   `error` frame carrying the refused subscription's text; there is no
//!   request id and no success flag. A refusal that names a subscription this
//!   epoch asked for quarantines that topic, exactly as a Bybit topic refusal
//!   does. A refusal that names nothing this epoch asked for is a transport
//!   failure, because it cannot be attributed to one topic.
//! - **A candle frame carries no `confirm` flag.** The `candle` channel keeps
//!   restating the running bar, so a bar is handed over when a frame for a
//!   later bar of the same coin arrives, or once its own hour has been closed
//!   for [`CANDLE_SETTLE_LAG_MS`] — a coin that trades on will confirm on the
//!   first frame of the next hour, and a coin that stops trading confirms on
//!   the timer. Either way each bar is handed over once.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::time::Instant;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::{protocol::WebSocketConfig, Message};
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use super::{engine_symbol, kline_row, next_settlement_ms, ticker_wire};
use crate::http::wall_ms;
use crate::model::BybitTickerWire;
use crate::normalize::normalize_ticker_strict;
use crate::venue::{
    BoxFuture, ConfirmedKline, PublicStream, StreamContinuity, StreamEvent, StreamHealth,
    TickerSample,
};
use crate::worker::WorkerError;
use crate::HOUR_MS;

/// Subscriptions written before the replies to them are read. One message per
/// subscription, so this bounds both the frames staged while a batch is
/// unacknowledged and how long the read side goes undrained.
const SUBSCRIPTIONS_PER_BATCH: usize = 25;
const MAX_STREAM_EVENTS: usize = 1_024;
const PING_PAYLOAD: &str = r#"{"method":"ping"}"#;
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
/// The bar interval this stream follows, in the venue's spelling.
const CANDLE_INTERVAL: &str = "1h";
/// How long after its hour closes a bar is handed over when no frame for the
/// next bar has arrived. A late print inside the closing hour still lands in
/// the bar it belongs to.
const CANDLE_SETTLE_LAG_MS: i64 = 60_000;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

pub struct HyperliquidPublicStream {
    symbols: BTreeSet<String>,
    events: mpsc::Receiver<StreamEvent>,
    control: watch::Receiver<ControlState>,
    shared: Arc<Mutex<SharedState>>,
    worker: JoinHandle<()>,
    queue_capacity: usize,
}

impl HyperliquidPublicStream {
    /// The successor of a stream this process is replacing. It keeps the
    /// outgoing stream's epoch numbering, gap stamp, and fault clocks: a symbol
    /// set changing is not the transport recovering.
    pub fn spawn_continuing(
        url: &str,
        coins: BTreeMap<String, String>,
        request_timeout_ms: u64,
        retry_base_ms: u64,
        continuity: StreamContinuity,
    ) -> Result<Self, WorkerError> {
        Self::with_options(
            url,
            coins,
            StreamOptions::production(request_timeout_ms, retry_base_ms),
            continuity,
        )
    }

    fn with_options(
        url: &str,
        coins: BTreeMap<String, String>,
        options: StreamOptions,
        continuity: StreamContinuity,
    ) -> Result<Self, WorkerError> {
        let topics = topics(&coins)?;
        let symbols = coins.keys().cloned().collect::<BTreeSet<_>>();
        let queue_capacity = stream_event_capacity(symbols.len());
        let (tx, events) = mpsc::channel(queue_capacity);
        let (control_tx, control) = watch::channel(ControlState::default());
        let shared = Arc::new(Mutex::new(SharedState::continuing(&symbols, continuity)));
        let worker = StreamWorker {
            url: url.to_owned(),
            symbols_by_coin: coins
                .iter()
                .map(|(symbol, coin)| (coin.clone(), symbol.clone()))
                .collect(),
            topics,
            symbols: symbols.clone(),
            active_topics: BTreeSet::new(),
            quarantined_topics: BTreeSet::new(),
            candles: CandleWatch::default(),
            shared: Arc::clone(&shared),
            events: tx,
            control: control_tx,
            options,
            epoch: continuity.epoch,
            backoff: options.backoff_start,
            next_ping_at: Instant::now() + options.ping_interval,
            pong_deadline: None,
            last_data_at: Instant::now(),
            next_quarantine_reprobe_at: Instant::now() + options.quarantine_reprobe_interval,
        };
        Ok(Self {
            symbols,
            events,
            control,
            shared,
            worker: tokio::spawn(worker.run()),
            queue_capacity,
        })
    }

    pub async fn next_event(&mut self) -> Option<StreamEvent> {
        tokio::select! {
            biased;
            changed = self.control.changed() => {
                if changed.is_err() {
                    return None;
                }
                Some(self.control.borrow_and_update().event())
            }
            event = self.events.recv() => event,
        }
    }

    pub fn sample_tickers(&self, observed_ts_ms: i64, max_age_ms: i64) -> Option<TickerSample> {
        let mut state = self.state();
        if !state.health.connected {
            return None;
        }
        let rows = state.tickers.sample(observed_ts_ms, max_age_ms);
        let fresh_mark_coverage =
            rows.iter().filter(|row| row.mark_price.is_some()).count() == state.tickers.capacity();
        state.health.ticker_coverage_complete =
            fresh_mark_coverage && state.ws_ticker_coverage_complete();
        if rows.is_empty() {
            return None;
        }
        Some(TickerSample {
            observed_ts_ms,
            available_at_ms: observed_ts_ms,
            rows,
        })
    }

    pub fn mark_gap_repaired(&self, epoch: u64) -> bool {
        let mut state = self.state();
        if state.health.connected && state.health.epoch == epoch {
            state.health.gap_open = false;
            state.health.gap_open_since_ms = None;
            true
        } else {
            false
        }
    }

    pub fn mark_source_fault(&self, observed_ts_ms: i64) {
        let mut state = self.state();
        state.tickers.clear();
        state.health.gap_open = true;
        state.health.gap_open_since_ms.get_or_insert(observed_ts_ms);
        state.health.ticker_coverage_complete = false;
        state.health.fault_count = state.health.fault_count.saturating_add(1);
    }

    pub fn reconcile_tickers(
        &self,
        epoch: u64,
        rows: &[BybitTickerWire],
        request_started_at_ms: i64,
        received_at_ms: i64,
    ) -> bool {
        let mut state = self.state();
        if !state.health.connected || state.health.epoch != epoch {
            return false;
        }
        for row in rows {
            state
                .tickers
                .reconcile_rest(row.clone(), request_started_at_ms, received_at_ms);
        }
        state.ws_ticker_coverage_complete()
    }

    pub fn health(&self) -> StreamHealth {
        let state = self.state();
        let mut health = state.health.clone();
        health.ticker_rows = state.tickers.len();
        health.ticker_capacity = state.tickers.capacity();
        health.queued_frames = self.events.len();
        health.queue_capacity = self.queue_capacity;
        health
    }

    pub fn symbols(&self) -> &BTreeSet<String> {
        &self.symbols
    }

    fn state(&self) -> std::sync::MutexGuard<'_, SharedState> {
        self.shared
            .lock()
            .expect("Hyperliquid stream state lock poisoned")
    }
}

impl PublicStream for HyperliquidPublicStream {
    fn next_event(&mut self) -> BoxFuture<'_, Option<StreamEvent>> {
        Box::pin(HyperliquidPublicStream::next_event(self))
    }

    fn sample_tickers(&self, observed_ts_ms: i64, max_age_ms: i64) -> Option<TickerSample> {
        HyperliquidPublicStream::sample_tickers(self, observed_ts_ms, max_age_ms)
    }

    fn mark_gap_repaired(&self, epoch: u64) -> bool {
        HyperliquidPublicStream::mark_gap_repaired(self, epoch)
    }

    fn mark_source_fault(&self, observed_ts_ms: i64) {
        HyperliquidPublicStream::mark_source_fault(self, observed_ts_ms);
    }

    fn reconcile_tickers(
        &self,
        epoch: u64,
        rows: &[BybitTickerWire],
        request_started_at_ms: i64,
        received_at_ms: i64,
    ) -> bool {
        HyperliquidPublicStream::reconcile_tickers(
            self,
            epoch,
            rows,
            request_started_at_ms,
            received_at_ms,
        )
    }

    fn health(&self) -> StreamHealth {
        HyperliquidPublicStream::health(self)
    }

    fn symbols(&self) -> &BTreeSet<String> {
        HyperliquidPublicStream::symbols(self)
    }
}

impl Drop for HyperliquidPublicStream {
    fn drop(&mut self) {
        self.worker.abort();
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct ControlState {
    epoch: u64,
    observed_ts_ms: i64,
    connected: bool,
    reconnected: bool,
}

impl ControlState {
    fn event(&self) -> StreamEvent {
        if self.connected {
            StreamEvent::EpochStarted {
                epoch: self.epoch,
                observed_ts_ms: self.observed_ts_ms,
                reconnected: self.reconnected,
            }
        } else {
            StreamEvent::GapOpened {
                epoch: self.epoch,
                observed_ts_ms: self.observed_ts_ms,
            }
        }
    }
}

struct SharedState {
    health: StreamHealth,
    tickers: TickerCache,
}

impl SharedState {
    fn continuing(symbols: &BTreeSet<String>, continuity: StreamContinuity) -> Self {
        let tickers = TickerCache::new(symbols.clone());
        Self {
            health: StreamHealth {
                ticker_capacity: tickers.capacity(),
                epoch: continuity.epoch,
                gap_open: continuity.gap_open,
                gap_open_since_ms: continuity.gap_open_since_ms,
                reconnect_count: continuity.reconnect_count,
                fault_count: continuity.fault_count,
                ..StreamHealth::default()
            },
            tickers,
        }
    }

    fn prepare_epoch(&mut self, epoch: u64, observed_ts_ms: i64) {
        self.tickers.clear();
        self.health.connected = false;
        self.health.epoch = epoch;
        self.health.gap_open = true;
        self.health.gap_open_since_ms.get_or_insert(observed_ts_ms);
        self.health.last_frame_ts_ms = None;
        self.health.ticker_coverage_complete = false;
        self.health.ticker_topics_accepted = 0;
        self.health.ticker_topics_quarantined = 0;
        self.health.kline_topics_accepted = 0;
        self.health.kline_topics_quarantined = 0;
    }

    fn activate_epoch(
        &mut self,
        accepted_topics: &BTreeSet<String>,
        quarantined_topics: &BTreeSet<String>,
    ) {
        self.health.connected = true;
        if self.health.epoch > 1 {
            self.health.reconnect_count = self.health.reconnect_count.saturating_add(1);
        }
        self.update_topic_counts(accepted_topics, quarantined_topics);
    }

    fn update_topic_counts(
        &mut self,
        accepted_topics: &BTreeSet<String>,
        quarantined_topics: &BTreeSet<String>,
    ) {
        let count = |topics: &BTreeSet<String>, channel: Channel| {
            topics
                .iter()
                .filter(|topic| topic.starts_with(channel.topic_prefix()))
                .count()
        };
        self.health.ticker_topics_accepted = count(accepted_topics, Channel::Ticker);
        self.health.kline_topics_accepted = count(accepted_topics, Channel::Candle);
        self.health.ticker_topics_quarantined = count(quarantined_topics, Channel::Ticker);
        self.health.kline_topics_quarantined = count(quarantined_topics, Channel::Candle);
    }

    fn open_gap(&mut self, observed_ts_ms: i64) {
        self.tickers.clear();
        self.health.connected = false;
        self.health.gap_open = true;
        self.health.gap_open_since_ms.get_or_insert(observed_ts_ms);
    }

    fn saw_frame(&mut self, received_at_ms: i64) {
        self.health.last_frame_ts_ms = Some(received_at_ms);
    }

    fn ws_ticker_coverage_complete(&self) -> bool {
        self.health.ticker_topics_quarantined == 0
            && self.health.ticker_topics_accepted == self.tickers.capacity()
            && self.tickers.ws_coverage_complete()
    }
}

#[derive(Clone)]
struct CachedTicker {
    row: BybitTickerWire,
    freshness: TickerFreshness,
    ws_snapshot_seen: bool,
}

#[derive(Clone, Default)]
struct TickerFreshness {
    last_price: Option<i64>,
    mark_price: Option<i64>,
    index_price: Option<i64>,
    bid1_price: Option<i64>,
    ask1_price: Option<i64>,
    bid1_size: Option<i64>,
    ask1_size: Option<i64>,
    open_interest: Option<i64>,
    open_interest_value: Option<i64>,
    turnover24h: Option<i64>,
    volume24h: Option<i64>,
    funding_rate: Option<i64>,
    next_funding_time: Option<i64>,
}

impl TickerFreshness {
    fn from_row(row: &BybitTickerWire, received_at_ms: i64) -> Self {
        macro_rules! stamped {
            ($field:ident) => {
                row.$field.as_ref().map(|_| received_at_ms)
            };
        }
        Self {
            last_price: stamped!(last_price),
            mark_price: stamped!(mark_price),
            index_price: stamped!(index_price),
            bid1_price: stamped!(bid1_price),
            ask1_price: stamped!(ask1_price),
            bid1_size: stamped!(bid1_size),
            ask1_size: stamped!(ask1_size),
            open_interest: stamped!(open_interest),
            open_interest_value: stamped!(open_interest_value),
            turnover24h: stamped!(turnover24h),
            volume24h: stamped!(volume24h),
            funding_rate: stamped!(funding_rate),
            next_funding_time: stamped!(next_funding_time),
        }
    }
}

impl CachedTicker {
    fn sample(&self, now_ms: i64, max_age_ms: i64) -> BybitTickerWire {
        let mut row = self.row.clone();
        macro_rules! clear_stale {
            ($field:ident) => {
                if !self.freshness.$field.is_some_and(|received_at_ms| {
                    received_at_ms <= now_ms && now_ms.saturating_sub(received_at_ms) <= max_age_ms
                }) {
                    row.$field = None;
                }
            };
        }
        clear_stale!(last_price);
        clear_stale!(mark_price);
        clear_stale!(index_price);
        clear_stale!(bid1_price);
        clear_stale!(ask1_price);
        clear_stale!(bid1_size);
        clear_stale!(ask1_size);
        clear_stale!(open_interest);
        clear_stale!(open_interest_value);
        clear_stale!(turnover24h);
        clear_stale!(volume24h);
        clear_stale!(funding_rate);
        if row
            .next_funding_time
            .as_ref()
            .and_then(|value| value_i64(value, "next funding time").ok())
            .is_none_or(|settlement_ts_ms| settlement_ts_ms < now_ms)
        {
            row.next_funding_time = None;
        }
        row.mark_observed_ts_ms = row.mark_price.as_ref().and(self.freshness.mark_price);
        row.funding_observed_ts_ms = row.funding_rate.as_ref().and(self.freshness.funding_rate);
        row.schedule_observed_ts_ms = row
            .next_funding_time
            .as_ref()
            .and(self.freshness.next_funding_time);
        row
    }
}

struct TickerCache {
    allowed: BTreeSet<String>,
    rows: BTreeMap<String, CachedTicker>,
}

impl TickerCache {
    fn new(allowed: BTreeSet<String>) -> Self {
        Self {
            allowed,
            rows: BTreeMap::new(),
        }
    }

    /// Every `activeAssetCtx` frame restates the whole context, so a frame
    /// replaces the cached row rather than merging into it.
    fn apply(&mut self, row: BybitTickerWire, received_at_ms: i64) {
        let symbol = row.symbol.clone();
        if !self.allowed.contains(&symbol) {
            return;
        }
        self.rows.insert(
            symbol,
            CachedTicker {
                freshness: TickerFreshness::from_row(&row, received_at_ms),
                row,
                ws_snapshot_seen: true,
            },
        );
    }

    fn reconcile_rest(
        &mut self,
        incoming: BybitTickerWire,
        request_started_at_ms: i64,
        received_at_ms: i64,
    ) {
        let symbol = incoming.symbol.clone();
        if !self.allowed.contains(&symbol) {
            return;
        }
        let Some(existing) = self.rows.get_mut(&symbol) else {
            self.rows.insert(
                symbol,
                CachedTicker {
                    freshness: TickerFreshness::from_row(&incoming, received_at_ms),
                    row: incoming,
                    ws_snapshot_seen: false,
                },
            );
            return;
        };
        macro_rules! reconcile_field {
            ($field:ident) => {
                if incoming.$field.is_some()
                    && existing
                        .freshness
                        .$field
                        .is_none_or(|updated_at_ms| updated_at_ms < request_started_at_ms)
                {
                    existing.row.$field = incoming.$field;
                    existing.freshness.$field = Some(received_at_ms);
                }
            };
        }
        reconcile_field!(last_price);
        reconcile_field!(mark_price);
        reconcile_field!(index_price);
        reconcile_field!(bid1_price);
        reconcile_field!(ask1_price);
        reconcile_field!(bid1_size);
        reconcile_field!(ask1_size);
        reconcile_field!(open_interest);
        reconcile_field!(open_interest_value);
        reconcile_field!(turnover24h);
        reconcile_field!(volume24h);
        reconcile_field!(funding_rate);
        reconcile_field!(next_funding_time);
    }

    fn sample(&self, now_ms: i64, max_age_ms: i64) -> Vec<BybitTickerWire> {
        self.rows
            .values()
            .map(|row| row.sample(now_ms, max_age_ms))
            .filter(|row| {
                row.mark_price.is_some()
                    || (row.funding_rate.is_some() && row.next_funding_time.is_some())
            })
            .collect()
    }

    fn clear(&mut self) {
        self.rows.clear();
    }

    fn len(&self) -> usize {
        self.rows.len()
    }

    fn capacity(&self) -> usize {
        self.allowed.len()
    }

    fn ws_coverage_complete(&self) -> bool {
        self.rows.len() == self.allowed.len()
            && self
                .rows
                .values()
                .all(|row| row.ws_snapshot_seen && row.freshness.mark_price.is_some())
    }
}

/// The running bar per symbol, and the last bar handed over, so a bar reaches
/// the queue once whichever rule confirms it.
#[derive(Default)]
struct CandleWatch {
    running: BTreeMap<String, ConfirmedKline>,
    handed_over_open_ms: BTreeMap<String, i64>,
}

impl CandleWatch {
    fn clear(&mut self) {
        self.running.clear();
        self.handed_over_open_ms.clear();
    }

    /// Take in one candle frame. Returns the bar it closed, if any.
    fn observe(&mut self, row: ConfirmedKline, open_ts_ms: i64) -> Option<ConfirmedKline> {
        if self
            .handed_over_open_ms
            .get(&row.symbol)
            .is_some_and(|handed| *handed >= open_ts_ms)
        {
            return None;
        }
        let closed = match self.running.get(&row.symbol) {
            Some(running) if running_open_ms(running) < open_ts_ms => {
                self.running.remove(&row.symbol)
            }
            _ => None,
        };
        let symbol = row.symbol.clone();
        self.running.insert(symbol.clone(), row);
        if let Some(closed) = closed {
            self.handed_over_open_ms
                .insert(symbol, running_open_ms(&closed));
            return Some(closed);
        }
        None
    }

    /// Every running bar whose hour closed at least [`CANDLE_SETTLE_LAG_MS`]
    /// ago. A coin that stops trading has no next frame to close its bar.
    fn settled(&mut self, now_ms: i64) -> Vec<ConfirmedKline> {
        let due = self
            .running
            .iter()
            .filter(|(_, running)| {
                running_open_ms(running) + HOUR_MS + CANDLE_SETTLE_LAG_MS <= now_ms
            })
            .map(|(symbol, _)| symbol.clone())
            .collect::<Vec<_>>();
        due.into_iter()
            .filter_map(|symbol| {
                let running = self.running.remove(&symbol)?;
                self.handed_over_open_ms
                    .insert(symbol, running_open_ms(&running));
                Some(running)
            })
            .collect()
    }
}

fn running_open_ms(row: &ConfirmedKline) -> i64 {
    row.row
        .first()
        .and_then(|value| value_i64(value, "candle open").ok())
        .unwrap_or_default()
}

#[derive(Clone, Copy)]
struct StreamOptions {
    connect_timeout: Duration,
    subscribe_timeout: Duration,
    write_timeout: Duration,
    ping_interval: Duration,
    pong_timeout: Duration,
    data_idle_timeout: Duration,
    quarantine_reprobe_interval: Duration,
    candle_sweep_interval: Duration,
    backoff_start: Duration,
    backoff_max: Duration,
}

impl StreamOptions {
    fn production(request_timeout_ms: u64, retry_base_ms: u64) -> Self {
        let request_timeout = Duration::from_millis(request_timeout_ms);
        let backoff_start = Duration::from_millis(retry_base_ms);
        Self {
            connect_timeout: request_timeout,
            subscribe_timeout: request_timeout,
            write_timeout: request_timeout,
            // The venue drops a socket that has said nothing for 60 seconds.
            ping_interval: Duration::from_secs(30),
            pong_timeout: Duration::from_secs(10),
            data_idle_timeout: Duration::from_secs(45),
            quarantine_reprobe_interval: Duration::from_secs(60),
            candle_sweep_interval: Duration::from_secs(5),
            backoff_start,
            backoff_max: Duration::from_secs(8).max(backoff_start),
        }
    }
}

struct StreamWorker {
    url: String,
    /// The venue's coin spelling to the engine's symbol. Every frame names the
    /// coin.
    symbols_by_coin: BTreeMap<String, String>,
    topics: Vec<Topic>,
    symbols: BTreeSet<String>,
    active_topics: BTreeSet<String>,
    quarantined_topics: BTreeSet<String>,
    candles: CandleWatch,
    shared: Arc<Mutex<SharedState>>,
    events: mpsc::Sender<StreamEvent>,
    control: watch::Sender<ControlState>,
    options: StreamOptions,
    epoch: u64,
    backoff: Duration,
    next_ping_at: Instant,
    pong_deadline: Option<Instant>,
    last_data_at: Instant,
    next_quarantine_reprobe_at: Instant,
}

struct SubscriptionOutcome {
    accepted_topics: BTreeSet<String>,
    quarantined_topics: BTreeSet<String>,
    /// Candle frames that arrived while their subscription was unanswered,
    /// newest per bar. They carry events, so they wait for the epoch to be
    /// live; a restatement of the same bar replaces the one held.
    staged_candles: BTreeMap<(String, i64), ConfirmedKline>,
    saw_market_data: bool,
}

impl StreamWorker {
    async fn run(mut self) {
        loop {
            if self.epoch > 0 || self.backoff > self.options.backoff_start {
                tokio::time::sleep(self.backoff).await;
            }
            let mut socket = match self.dial().await {
                Ok(socket) => socket,
                Err(error) => {
                    self.fault(error);
                    self.bump_backoff();
                    continue;
                }
            };
            self.epoch = self.epoch.saturating_add(1);
            let observed_ts_ms = match wall_ms() {
                Ok(value) => value,
                Err(error) => {
                    self.fault(error.to_string());
                    return;
                }
            };
            self.state().prepare_epoch(self.epoch, observed_ts_ms);
            self.next_ping_at = Instant::now() + self.options.ping_interval;
            self.pong_deadline = None;
            self.last_data_at = Instant::now();
            self.active_topics.clear();
            self.quarantined_topics.clear();
            self.candles.clear();
            let initial_topics = self.topics.clone();
            let outcome = match self.subscribe(&mut socket, &initial_topics).await {
                Ok(subscription) => {
                    self.active_topics = subscription.accepted_topics.clone();
                    self.quarantined_topics = subscription.quarantined_topics.clone();
                    self.next_quarantine_reprobe_at =
                        Instant::now() + self.options.quarantine_reprobe_interval;
                    self.state().activate_epoch(
                        &subscription.accepted_topics,
                        &subscription.quarantined_topics,
                    );
                    if !subscription.quarantined_topics.is_empty() {
                        let samples = subscription
                            .quarantined_topics
                            .iter()
                            .take(3)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ");
                        self.fault(format!(
                            "Hyperliquid public stream quarantined {} refused topics; {samples}",
                            subscription.quarantined_topics.len()
                        ));
                    }
                    self.control.send_replace(ControlState {
                        epoch: self.epoch,
                        observed_ts_ms,
                        connected: true,
                        reconnected: self.epoch > 1,
                    });
                    match self
                        .replay_staged(subscription.staged_candles, subscription.saw_market_data)
                        .await
                    {
                        Ok(()) => self.read_socket(&mut socket).await,
                        Err(error) => Err(error),
                    }
                }
                Err(error) => Err(error),
            };
            if let Err(error) = outcome {
                self.fault(error);
            }
            let gap_ts_ms = wall_ms().unwrap_or(observed_ts_ms);
            self.state().open_gap(gap_ts_ms);
            self.control.send_replace(ControlState {
                epoch: self.epoch,
                observed_ts_ms: gap_ts_ms,
                connected: false,
                reconnected: self.epoch > 1,
            });
            self.bump_backoff();
        }
    }

    async fn dial(&self) -> Result<Socket, String> {
        engine_public::tls::install_crypto_provider();
        let connected = tokio::time::timeout(
            self.options.connect_timeout,
            connect_async_with_config(
                self.url.as_str(),
                Some(
                    WebSocketConfig::default()
                        .read_buffer_size(64 * 1024)
                        .write_buffer_size(16 * 1024)
                        .max_write_buffer_size(256 * 1024)
                        .max_message_size(Some(MAX_MESSAGE_BYTES))
                        .max_frame_size(Some(MAX_MESSAGE_BYTES)),
                ),
                true,
            ),
        )
        .await
        .map_err(|_| "Hyperliquid public WebSocket dial timed out".to_owned())?;
        connected
            .map(|(socket, _)| socket)
            .map_err(|error| format!("Hyperliquid public WebSocket dial: {error}"))
    }

    async fn subscribe(
        &mut self,
        socket: &mut Socket,
        topics: &[Topic],
    ) -> Result<SubscriptionOutcome, String> {
        let mut quarantined_topics = BTreeSet::new();
        let mut staged_candles = BTreeMap::new();
        let mut saw_market_data = false;
        let mut batches = topics
            .chunks(SUBSCRIPTIONS_PER_BATCH)
            .map(<[Topic]>::to_vec)
            .collect::<VecDeque<_>>();
        while let Some(batch) = batches.pop_front() {
            let mut pending = BTreeMap::new();
            for topic in &batch {
                let payload = json!({
                    "method": "subscribe",
                    "subscription": topic.subscription(),
                })
                .to_string();
                tokio::time::timeout(
                    self.options.write_timeout,
                    socket.send(Message::text(payload)),
                )
                .await
                .map_err(|_| "Hyperliquid public subscription write timed out".to_owned())?
                .map_err(|error| format!("Hyperliquid public subscription write: {error}"))?;
                pending.insert(topic.subscription_key(), topic.name());
            }
            tokio::time::timeout(self.options.subscribe_timeout, async {
                while !pending.is_empty() {
                    let message = socket
                        .next()
                        .await
                        .ok_or_else(|| {
                            "Hyperliquid public socket closed before subscribe reply".to_owned()
                        })?
                        .map_err(|error| {
                            format!("Hyperliquid public subscription read: {error}")
                        })?;
                    if let Message::Ping(payload) = &message {
                        tokio::time::timeout(
                            self.options.write_timeout,
                            socket.send(Message::Pong(payload.clone())),
                        )
                        .await
                        .map_err(|_| {
                            "Hyperliquid public subscription pong timed out".to_owned()
                        })?
                        .map_err(|error| {
                            format!("Hyperliquid public subscription pong: {error}")
                        })?;
                        continue;
                    }
                    let received_at_ms = wall_ms().map_err(|error| error.to_string())?;
                    match self.parse(&message, received_at_ms)? {
                        ParsedMessage::Accepted(key) => {
                            if let Some(topic) = pending.remove(&key) {
                                self.active_topics.insert(topic);
                            }
                        }
                        ParsedMessage::Refused { key, reason } => match pending.remove(&key) {
                            Some(topic) => {
                                quarantined_topics.insert(topic);
                            }
                            None => {
                                return Err(format!(
                                    "Hyperliquid public subscription refusal names no requested topic: {reason}"
                                ))
                            }
                        },
                        ParsedMessage::Pong | ParsedMessage::Ignore => {}
                        // A candle frame carries an event, so it waits for the
                        // epoch; a context frame only fills the cache and is
                        // taken now.
                        ParsedMessage::Candle { row, open_ts_ms } => {
                            if !pending.values().any(|held| held == &Channel::Candle.topic(&row.symbol))
                                && !self
                                    .active_topics
                                    .contains(&Channel::Candle.topic(&row.symbol))
                            {
                                continue;
                            }
                            if staged_candles.len() >= MAX_STREAM_EVENTS {
                                return Err(
                                    "Hyperliquid pre-activation candle buffer exceeded its bound"
                                        .to_owned(),
                                );
                            }
                            saw_market_data = true;
                            staged_candles.insert((row.symbol.clone(), open_ts_ms), *row);
                        }
                        parsed => saw_market_data |= self.apply(parsed, received_at_ms).await?,
                    }
                }
                Ok::<_, String>(())
            })
            .await
            .map_err(|_| "Hyperliquid public subscription reply timed out".to_owned())??;
        }
        Ok(SubscriptionOutcome {
            accepted_topics: self.active_topics.clone(),
            quarantined_topics,
            staged_candles,
            saw_market_data,
        })
    }

    /// Candle frames that arrived while their subscription was unanswered.
    /// Their topics are live now, so they go through the ordinary path, in bar
    /// order per symbol.
    async fn replay_staged(
        &mut self,
        staged_candles: BTreeMap<(String, i64), ConfirmedKline>,
        mut saw_market_data: bool,
    ) -> Result<(), String> {
        for ((_, open_ts_ms), row) in staged_candles {
            let received_at_ms = row.available_at_ms;
            saw_market_data |= self
                .apply(
                    ParsedMessage::Candle {
                        row: Box::new(row),
                        open_ts_ms,
                    },
                    received_at_ms,
                )
                .await?;
        }
        if saw_market_data {
            self.backoff = self.options.backoff_start;
        }
        Ok(())
    }

    async fn read_socket(&mut self, socket: &mut Socket) -> Result<(), String> {
        loop {
            let mut deadline = self
                .pong_deadline
                .unwrap_or(self.next_ping_at)
                .min(self.next_ping_at)
                .min(self.last_data_at + self.options.data_idle_timeout)
                .min(Instant::now() + self.options.candle_sweep_interval);
            if !self.quarantined_topics.is_empty() {
                deadline = deadline.min(self.next_quarantine_reprobe_at);
            }
            tokio::select! {
                incoming = socket.next() => {
                    let message = incoming
                        .ok_or_else(|| "Hyperliquid public socket closed".to_owned())?
                        .map_err(|error| format!("Hyperliquid public socket read: {error}"))?;
                    match message {
                        Message::Close(_) => {
                            return Err("Hyperliquid public socket received close".to_owned())
                        }
                        Message::Ping(payload) => {
                            tokio::time::timeout(
                                self.options.write_timeout,
                                socket.send(Message::Pong(payload)),
                            )
                            .await
                                .map_err(|_| "Hyperliquid public protocol pong timed out".to_owned())?
                                .map_err(|error| format!("Hyperliquid public protocol pong: {error}"))?;
                        }
                        _ => {
                            let received_at_ms = wall_ms().map_err(|error| error.to_string())?;
                            match self.parse(&message, received_at_ms) {
                                Ok(parsed) => {
                                    if self.apply(parsed, received_at_ms).await? {
                                        self.backoff = self.options.backoff_start;
                                    }
                                }
                                Err(error) => self.fault(error),
                            }
                        }
                    }
                }
                _ = tokio::time::sleep_until(deadline) => {
                    let now = Instant::now();
                    if self.pong_deadline.is_some_and(|deadline| now >= deadline) {
                        return Err("Hyperliquid public keep-alive was unanswered".to_owned());
                    }
                    if now >= self.last_data_at + self.options.data_idle_timeout {
                        return Err("Hyperliquid public data stream became idle".to_owned());
                    }
                    self.sweep_candles()?;
                    if !self.quarantined_topics.is_empty()
                        && now >= self.next_quarantine_reprobe_at
                    {
                        self.reprobe_quarantined(socket).await?;
                        continue;
                    }
                    if now >= self.next_ping_at {
                        tokio::time::timeout(
                            self.options.write_timeout,
                            socket.send(Message::text(PING_PAYLOAD)),
                        )
                        .await
                        .map_err(|_| "Hyperliquid public ping write timed out".to_owned())?
                        .map_err(|error| format!("Hyperliquid public ping write: {error}"))?;
                        self.next_ping_at = now + self.options.ping_interval;
                        if self.pong_deadline.is_none() {
                            self.pong_deadline = Some(now + self.options.pong_timeout);
                        }
                    }
                }
            }
        }
    }

    async fn reprobe_quarantined(&mut self, socket: &mut Socket) -> Result<(), String> {
        let candidates = self
            .topics
            .iter()
            .filter(|topic| self.quarantined_topics.contains(&topic.name()))
            .cloned()
            .collect::<Vec<_>>();
        if candidates.is_empty() {
            self.quarantined_topics.clear();
            return Ok(());
        }
        match self.subscribe(socket, &candidates).await {
            Ok(outcome) => {
                self.active_topics = outcome.accepted_topics;
                self.quarantined_topics = outcome.quarantined_topics;
                self.state()
                    .update_topic_counts(&self.active_topics, &self.quarantined_topics);
                self.replay_staged(outcome.staged_candles, outcome.saw_market_data)
                    .await?;
            }
            Err(error) => {
                self.quarantined_topics
                    .retain(|topic| !self.active_topics.contains(topic));
                self.state()
                    .update_topic_counts(&self.active_topics, &self.quarantined_topics);
                return Err(format!("Hyperliquid quarantined-topic re-probe: {error}"));
            }
        }
        self.next_quarantine_reprobe_at = Instant::now() + self.options.quarantine_reprobe_interval;
        Ok(())
    }

    fn parse(&self, message: &Message, received_at_ms: i64) -> Result<ParsedMessage, String> {
        let text = match message {
            Message::Text(text) => text.as_str().to_owned(),
            Message::Binary(bytes) => std::str::from_utf8(bytes)
                .map_err(|error| format!("Hyperliquid public binary frame is not UTF-8: {error}"))?
                .to_owned(),
            _ => return Ok(ParsedMessage::Ignore),
        };
        parse_frame(&self.symbols_by_coin, &text, received_at_ms)
    }

    async fn apply(&mut self, parsed: ParsedMessage, received_at_ms: i64) -> Result<bool, String> {
        match parsed {
            ParsedMessage::Ticker(row) => {
                if !self
                    .active_topics
                    .contains(&Channel::Ticker.topic(&row.symbol))
                {
                    return Ok(false);
                }
                self.last_data_at = Instant::now();
                let mut state = self.state();
                state.tickers.apply(*row, received_at_ms);
                state.saw_frame(received_at_ms);
                Ok(true)
            }
            ParsedMessage::Candle { row, open_ts_ms } => {
                if !self
                    .active_topics
                    .contains(&Channel::Candle.topic(&row.symbol))
                    || !self.symbols.contains(&row.symbol)
                {
                    return Ok(false);
                }
                self.last_data_at = Instant::now();
                self.state().saw_frame(received_at_ms);
                if let Some(closed) = self.candles.observe(*row, open_ts_ms) {
                    self.hand_over(closed)?;
                }
                Ok(true)
            }
            ParsedMessage::Pong => {
                self.pong_deadline = None;
                Ok(false)
            }
            ParsedMessage::Accepted(_) | ParsedMessage::Refused { .. } | ParsedMessage::Ignore => {
                Ok(false)
            }
        }
    }

    fn sweep_candles(&mut self) -> Result<(), String> {
        let now_ms = wall_ms().map_err(|error| error.to_string())?;
        for closed in self.candles.settled(now_ms) {
            self.hand_over(closed)?;
        }
        Ok(())
    }

    /// One closed bar, checked as a closed bar before it leaves this task.
    fn hand_over(&self, mut row: ConfirmedKline) -> Result<(), String> {
        row.available_at_ms = wall_ms().map_err(|error| error.to_string())?;
        crate::normalize::normalize_kline_rows(
            &row.symbol,
            row.available_at_ms,
            std::slice::from_ref(&row.row),
        )
        .map_err(|error| error.to_string())?;
        self.events
            .try_send(StreamEvent::KlineClosed(row))
            .map_err(|error| {
                format!("Hyperliquid confirmed-kline queue requires reconnect repair: {error}")
            })
    }

    fn bump_backoff(&mut self) {
        self.backoff = self.backoff.saturating_mul(2).min(self.options.backoff_max);
    }

    fn fault(&self, error: String) {
        {
            let mut state = self.state();
            state.health.fault_count = state.health.fault_count.saturating_add(1);
        }
        let _ = self.events.try_send(StreamEvent::Fault(error));
    }

    fn state(&self) -> std::sync::MutexGuard<'_, SharedState> {
        self.shared
            .lock()
            .expect("Hyperliquid stream state lock poisoned")
    }
}

/// One subscription this stream keeps, named the way the heartbeat's topic
/// counts read it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Channel {
    Ticker,
    Candle,
}

impl Channel {
    fn wire(self) -> &'static str {
        match self {
            Self::Ticker => "activeAssetCtx",
            Self::Candle => "candle",
        }
    }

    fn topic_prefix(self) -> &'static str {
        match self {
            Self::Ticker => "activeAssetCtx.",
            Self::Candle => "candle.1h.",
        }
    }

    fn topic(self, symbol: &str) -> String {
        format!("{}{symbol}", self.topic_prefix())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Topic {
    channel: Channel,
    symbol: String,
    coin: String,
}

impl Topic {
    fn name(&self) -> String {
        self.channel.topic(&self.symbol)
    }

    fn subscription(&self) -> Value {
        match self.channel {
            Channel::Ticker => json!({"type": self.channel.wire(), "coin": self.coin}),
            Channel::Candle => json!({
                "type": self.channel.wire(),
                "coin": self.coin,
                "interval": CANDLE_INTERVAL,
            }),
        }
    }

    fn subscription_key(&self) -> SubscriptionKey {
        SubscriptionKey {
            channel: self.channel,
            coin: self.coin.clone(),
        }
    }
}

/// What a reply names. The venue echoes the subscription it accepted and
/// quotes the one it refused; neither carries a request id.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct SubscriptionKey {
    channel: Channel,
    coin: String,
}

#[derive(Clone, Debug, PartialEq)]
enum ParsedMessage {
    Accepted(SubscriptionKey),
    Refused {
        key: SubscriptionKey,
        reason: String,
    },
    Pong,
    Ticker(Box<BybitTickerWire>),
    Candle {
        row: Box<ConfirmedKline>,
        open_ts_ms: i64,
    },
    Ignore,
}

fn parse_frame(
    symbols_by_coin: &BTreeMap<String, String>,
    text: &str,
    received_at_ms: i64,
) -> Result<ParsedMessage, String> {
    let value: Value = serde_json::from_str(text)
        .map_err(|error| format!("Hyperliquid public frame is invalid JSON: {error}"))?;
    let Some(channel) = value.get("channel").and_then(Value::as_str) else {
        return Ok(ParsedMessage::Ignore);
    };
    match channel {
        "pong" => Ok(ParsedMessage::Pong),
        "subscriptionResponse" => {
            let subscription = value
                .get("data")
                .and_then(|data| data.get("subscription"))
                .ok_or_else(|| {
                    "Hyperliquid subscription reply carries no subscription".to_owned()
                })?;
            match subscription_key(subscription) {
                Some(key) => Ok(ParsedMessage::Accepted(key)),
                None => Ok(ParsedMessage::Ignore),
            }
        }
        "error" => {
            let reason = value
                .get("data")
                .and_then(Value::as_str)
                .unwrap_or("no reason")
                .to_owned();
            match quoted_subscription(&reason)
                .as_ref()
                .and_then(subscription_key)
            {
                Some(key) => Ok(ParsedMessage::Refused { key, reason }),
                None => Err(format!("Hyperliquid public stream error: {reason}")),
            }
        }
        "activeAssetCtx" => {
            let data = value
                .get("data")
                .ok_or_else(|| "Hyperliquid context frame lacks data".to_owned())?;
            let coin = data
                .get("coin")
                .and_then(Value::as_str)
                .ok_or_else(|| "Hyperliquid context frame lacks coin".to_owned())?;
            let Some(symbol) = followed_symbol(symbols_by_coin, coin) else {
                return Ok(ParsedMessage::Ignore);
            };
            let context = data
                .get("ctx")
                .ok_or_else(|| "Hyperliquid context frame lacks ctx".to_owned())?;
            let row = ticker_wire(symbol, context, next_settlement_ms(received_at_ms))
                .map_err(|error| error.to_string())?;
            normalize_ticker_strict(received_at_ms, received_at_ms, &row)
                .map_err(|error| error.to_string())?;
            Ok(ParsedMessage::Ticker(Box::new(row)))
        }
        "candle" => {
            let data = value
                .get("data")
                .ok_or_else(|| "Hyperliquid candle frame lacks data".to_owned())?;
            if data.get("i").and_then(Value::as_str) != Some(CANDLE_INTERVAL) {
                return Ok(ParsedMessage::Ignore);
            }
            let coin = data
                .get("s")
                .and_then(Value::as_str)
                .ok_or_else(|| "Hyperliquid candle frame lacks its coin".to_owned())?;
            let Some(symbol) = followed_symbol(symbols_by_coin, coin) else {
                return Ok(ParsedMessage::Ignore);
            };
            let row = kline_row(data).map_err(|error| error.to_string())?;
            let open_ts_ms = value_i64(&row[0], "Hyperliquid candle open")?;
            if open_ts_ms <= 0 || open_ts_ms % HOUR_MS != 0 {
                return Err("Hyperliquid candle has an invalid open clock".to_owned());
            }
            Ok(ParsedMessage::Candle {
                row: Box::new(ConfirmedKline {
                    symbol,
                    available_at_ms: received_at_ms,
                    row,
                }),
                open_ts_ms,
            })
        }
        _ => Ok(ParsedMessage::Ignore),
    }
}

/// The engine symbol a coin belongs to. The coin table is the authority, and a
/// coin outside it is a frame for a symbol this stream does not follow.
fn followed_symbol(symbols_by_coin: &BTreeMap<String, String>, coin: &str) -> Option<String> {
    symbols_by_coin
        .get(coin)
        .cloned()
        .filter(|symbol| symbol == &engine_symbol(coin))
}

fn subscription_key(subscription: &Value) -> Option<SubscriptionKey> {
    let coin = subscription.get("coin").and_then(Value::as_str)?.to_owned();
    let interval = subscription.get("interval").and_then(Value::as_str);
    match subscription.get("type").and_then(Value::as_str)? {
        "activeAssetCtx" => Some(SubscriptionKey {
            channel: Channel::Ticker,
            coin,
        }),
        "candle" if interval == Some(CANDLE_INTERVAL) => Some(SubscriptionKey {
            channel: Channel::Candle,
            coin,
        }),
        _ => None,
    }
}

/// The subscription a refusal quotes. The venue writes the reason as
/// `Invalid subscription {"type":"activeAssetCtx","coin":"NOTACOIN"}`.
fn quoted_subscription(reason: &str) -> Option<Value> {
    let opening = reason.find('{')?;
    serde_json::from_str(&reason[opening..]).ok()
}

fn value_i64(value: &Value, label: &str) -> Result<i64, String> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| format!("{label} is not an integer"))
}

/// A ticker and a candle subscription per symbol, in symbol order.
fn topics(coins: &BTreeMap<String, String>) -> Result<Vec<Topic>, WorkerError> {
    if coins.is_empty() {
        return Err(WorkerError::config("Hyperliquid stream has no symbols"));
    }
    let mut topics = Vec::with_capacity(coins.len().saturating_mul(2));
    for (symbol, coin) in coins {
        if symbol.is_empty()
            || !symbol
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
            || coin.trim().is_empty()
        {
            return Err(WorkerError::config("Hyperliquid stream symbol is invalid"));
        }
        for channel in [Channel::Ticker, Channel::Candle] {
            topics.push(Topic {
                channel,
                symbol: symbol.clone(),
                coin: coin.clone(),
            });
        }
    }
    Ok(topics)
}

fn stream_event_capacity(symbols: usize) -> usize {
    symbols.saturating_mul(2).clamp(64, MAX_STREAM_EVENTS)
}

#[cfg(test)]
mod tests;
