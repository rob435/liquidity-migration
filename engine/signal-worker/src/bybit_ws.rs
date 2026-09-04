use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::{Arc, Mutex, Once};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::net::TcpStream;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::{protocol::WebSocketConfig, Message};
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use crate::http::wall_ms;
use crate::model::BybitTickerWire;
use crate::normalize::{normalize_kline_rows, normalize_ticker_strict};
use crate::worker::WorkerError;
use crate::HOUR_MS;

const TOPICS_PER_MESSAGE: usize = 100;
const MAX_STREAM_EVENTS: usize = 1_024;
const MAX_SUBSCRIPTION_STAGED_BYTES: usize = 8 * 1024 * 1024;
const PING_PAYLOAD: &str = r#"{"op":"ping"}"#;
const PUBLIC_LINEAR_URL: &str = "wss://stream.bybit.com/v5/public/linear";
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Clone, Debug, PartialEq)]
pub struct ConfirmedKline {
    pub symbol: String,
    pub available_at_ms: i64,
    pub row: Vec<Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum StreamEvent {
    EpochStarted {
        epoch: u64,
        observed_ts_ms: i64,
        reconnected: bool,
    },
    GapOpened {
        epoch: u64,
        observed_ts_ms: i64,
    },
    KlineClosed(ConfirmedKline),
    Fault(String),
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct StreamHealth {
    pub connected: bool,
    pub epoch: u64,
    pub gap_open: bool,
    pub gap_open_since_ms: Option<i64>,
    pub reconnect_count: u64,
    pub fault_count: u64,
    pub last_frame_ts_ms: Option<i64>,
    pub ticker_rows: usize,
    pub ticker_capacity: usize,
    pub ticker_coverage_complete: bool,
    pub ticker_topics_accepted: usize,
    pub ticker_topics_quarantined: usize,
    pub kline_topics_accepted: usize,
    pub kline_topics_quarantined: usize,
    pub queued_frames: usize,
    pub queue_capacity: usize,
}

/// The transport history a replacement stream must continue. Epoch numbering
/// is the token `mark_gap_repaired` matches, so it must never restart while
/// repair lanes from the outgoing stream are still in flight; the gap stamp and
/// the two counters are what the heartbeat and the on-call page read.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StreamContinuity {
    pub epoch: u64,
    pub gap_open: bool,
    pub gap_open_since_ms: Option<i64>,
    pub reconnect_count: u64,
    pub fault_count: u64,
}

impl From<&StreamHealth> for StreamContinuity {
    fn from(health: &StreamHealth) -> Self {
        Self {
            epoch: health.epoch,
            gap_open: health.gap_open,
            gap_open_since_ms: health.gap_open_since_ms,
            reconnect_count: health.reconnect_count,
            fault_count: health.fault_count,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TickerSample {
    pub observed_ts_ms: i64,
    pub available_at_ms: i64,
    pub rows: Vec<BybitTickerWire>,
}

pub struct BybitPublicStream {
    symbols: BTreeSet<String>,
    events: mpsc::Receiver<StreamEvent>,
    control: watch::Receiver<ControlState>,
    shared: Arc<Mutex<SharedState>>,
    worker: JoinHandle<()>,
    queue_capacity: usize,
}

impl BybitPublicStream {
    pub fn spawn(
        symbols: Vec<String>,
        request_timeout_ms: u64,
        retry_base_ms: u64,
    ) -> Result<Self, WorkerError> {
        Self::spawn_continuing(
            symbols,
            request_timeout_ms,
            retry_base_ms,
            StreamContinuity::default(),
        )
    }

    /// The successor of a stream this process is replacing. It keeps the
    /// outgoing stream's epoch numbering, gap stamp, and fault clocks: a symbol
    /// set changing is not the transport recovering.
    pub fn spawn_continuing(
        symbols: Vec<String>,
        request_timeout_ms: u64,
        retry_base_ms: u64,
        continuity: StreamContinuity,
    ) -> Result<Self, WorkerError> {
        Self::with_url_continuing(
            PUBLIC_LINEAR_URL,
            symbols,
            StreamOptions::production(request_timeout_ms, retry_base_ms),
            continuity,
        )
    }

    #[cfg(test)]
    pub(crate) fn inert_for_test(symbols: Vec<String>) -> Result<Self, WorkerError> {
        let symbols = normalize_symbols(symbols)?;
        let queue_capacity = stream_event_capacity(symbols.len());
        let (events_tx, events) = mpsc::channel(queue_capacity);
        let (control_tx, control) = watch::channel(ControlState::default());
        drop(events_tx);
        drop(control_tx);
        Ok(Self {
            symbols: symbols.clone(),
            events,
            control,
            shared: Arc::new(Mutex::new(SharedState::continuing(
                &symbols,
                StreamContinuity::default(),
            ))),
            worker: tokio::spawn(async {}),
            queue_capacity,
        })
    }

    #[cfg(test)]
    fn with_url(
        url: impl Into<String>,
        symbols: Vec<String>,
        options: StreamOptions,
    ) -> Result<Self, WorkerError> {
        Self::with_url_continuing(url, symbols, options, StreamContinuity::default())
    }

    fn with_url_continuing(
        url: impl Into<String>,
        symbols: Vec<String>,
        options: StreamOptions,
        continuity: StreamContinuity,
    ) -> Result<Self, WorkerError> {
        let symbols = normalize_symbols(symbols)?;
        let queue_capacity = stream_event_capacity(symbols.len());
        let (tx, events) = mpsc::channel(queue_capacity);
        let (control_tx, control) = watch::channel(ControlState::default());
        let shared = Arc::new(Mutex::new(SharedState::continuing(&symbols, continuity)));
        let worker = StreamWorker {
            url: url.into(),
            topics: topics(&symbols),
            symbols: symbols.clone(),
            active_topics: BTreeSet::new(),
            quarantined_topics: BTreeSet::new(),
            shared: Arc::clone(&shared),
            events: tx,
            control: control_tx,
            options,
            epoch: continuity.epoch,
            next_request_nonce: 0,
            backoff: options.backoff_start,
            next_ping_at: Instant::now() + options.ping_interval,
            pong_deadline: None,
            last_data_at: Instant::now(),
            next_quarantine_reprobe_at: Instant::now() + options.quarantine_reprobe_interval,
        };
        Ok(Self {
            symbols: symbols.clone(),
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
        let mut state = self
            .shared
            .lock()
            .expect("Bybit stream state lock poisoned");
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
        let mut state = self
            .shared
            .lock()
            .expect("Bybit stream state lock poisoned");
        if state.health.connected && state.health.epoch == epoch {
            state.health.gap_open = false;
            state.health.gap_open_since_ms = None;
            true
        } else {
            false
        }
    }

    pub fn mark_source_fault(&self, observed_ts_ms: i64) {
        let mut state = self
            .shared
            .lock()
            .expect("Bybit stream state lock poisoned");
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
        let mut state = self
            .shared
            .lock()
            .expect("Bybit stream state lock poisoned");
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
        let state = self
            .shared
            .lock()
            .expect("Bybit stream state lock poisoned");
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
}

impl Drop for BybitPublicStream {
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
        self.health.ticker_topics_accepted = accepted_topics
            .iter()
            .filter(|topic| topic.starts_with("tickers."))
            .count();
        self.health.kline_topics_accepted = accepted_topics
            .iter()
            .filter(|topic| topic.starts_with("kline.60."))
            .count();
        self.health.ticker_topics_quarantined = quarantined_topics
            .iter()
            .filter(|topic| topic.starts_with("tickers."))
            .count();
        self.health.kline_topics_quarantined = quarantined_topics
            .iter()
            .filter(|topic| topic.starts_with("kline.60."))
            .count();
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

    fn apply(&mut self, frame: TickerFrame, received_at_ms: i64) {
        let symbol = frame.row.symbol.clone();
        if !self.allowed.contains(&symbol) {
            return;
        }
        match frame.kind {
            TickerKind::Snapshot => {
                self.rows.insert(
                    symbol,
                    CachedTicker {
                        freshness: TickerFreshness::from_row(&frame.row, received_at_ms),
                        row: frame.row,
                        ws_snapshot_seen: true,
                    },
                );
            }
            TickerKind::Delta => {
                let Some(existing) = self.rows.get_mut(&symbol) else {
                    return;
                };
                merge_ticker(existing, frame.row, received_at_ms);
            }
        }
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

#[derive(Clone, Copy)]
struct StreamOptions {
    connect_timeout: Duration,
    subscribe_timeout: Duration,
    write_timeout: Duration,
    ping_interval: Duration,
    pong_timeout: Duration,
    data_idle_timeout: Duration,
    quarantine_reprobe_interval: Duration,
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
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(10),
            data_idle_timeout: Duration::from_secs(45),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start,
            backoff_max: Duration::from_secs(8).max(backoff_start),
        }
    }
}

struct StreamWorker {
    url: String,
    topics: Vec<String>,
    symbols: BTreeSet<String>,
    active_topics: BTreeSet<String>,
    quarantined_topics: BTreeSet<String>,
    shared: Arc<Mutex<SharedState>>,
    events: mpsc::Sender<StreamEvent>,
    control: watch::Sender<ControlState>,
    options: StreamOptions,
    epoch: u64,
    next_request_nonce: u64,
    backoff: Duration,
    next_ping_at: Instant,
    pong_deadline: Option<Instant>,
    last_data_at: Instant,
    next_quarantine_reprobe_at: Instant,
}

struct SubscriptionOutcome {
    accepted_topics: BTreeSet<String>,
    quarantined_topics: BTreeSet<String>,
    preactivation_klines: BTreeMap<(String, i64), ConfirmedKline>,
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
            {
                let mut state = self
                    .shared
                    .lock()
                    .expect("Bybit stream state lock poisoned");
                state.prepare_epoch(self.epoch, observed_ts_ms);
            }
            self.next_ping_at = Instant::now() + self.options.ping_interval;
            self.pong_deadline = None;
            self.last_data_at = Instant::now();
            self.active_topics.clear();
            self.quarantined_topics.clear();
            let initial_topics = self.topics.clone();
            let outcome = match self.subscribe(&mut socket, &initial_topics).await {
                Ok(subscription) => {
                    self.active_topics = subscription.accepted_topics.clone();
                    self.quarantined_topics = subscription.quarantined_topics.clone();
                    self.next_quarantine_reprobe_at =
                        Instant::now() + self.options.quarantine_reprobe_interval;
                    {
                        let mut state = self
                            .shared
                            .lock()
                            .expect("Bybit stream state lock poisoned");
                        state.activate_epoch(
                            &subscription.accepted_topics,
                            &subscription.quarantined_topics,
                        );
                    }
                    if !subscription.quarantined_topics.is_empty() {
                        let samples = subscription
                            .quarantined_topics
                            .iter()
                            .take(3)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ");
                        self.fault(format!(
                            "Bybit public stream quarantined {} refused topics; {samples}",
                            subscription.quarantined_topics.len()
                        ));
                    }
                    self.control.send_replace(ControlState {
                        epoch: self.epoch,
                        observed_ts_ms,
                        connected: true,
                        reconnected: self.epoch > 1,
                    });
                    let mut saw_market_data = subscription.saw_market_data;
                    let mut preactivation_error = None;
                    for row in subscription.preactivation_klines.into_values() {
                        let received_at_ms = row.available_at_ms;
                        match self
                            .apply(ParsedMessage::Klines(vec![row]), received_at_ms)
                            .await
                        {
                            Ok(saw_data) => saw_market_data |= saw_data,
                            Err(error) => {
                                preactivation_error = Some(error);
                                break;
                            }
                        }
                    }
                    if let Some(error) = preactivation_error {
                        Err(error)
                    } else {
                        if saw_market_data {
                            self.backoff = self.options.backoff_start;
                        }
                        self.read_socket(&mut socket).await
                    }
                }
                Err(error) => Err(error),
            };
            if let Err(error) = outcome {
                self.fault(error);
            }
            let gap_ts_ms = wall_ms().unwrap_or(observed_ts_ms);
            {
                let mut state = self
                    .shared
                    .lock()
                    .expect("Bybit stream state lock poisoned");
                state.open_gap(gap_ts_ms);
            }
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
        install_crypto_provider();
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
        .map_err(|_| "Bybit public WebSocket dial timed out".to_owned())?;
        connected
            .map(|(socket, _)| socket)
            .map_err(|error| format!("Bybit public WebSocket dial: {error}"))
    }

    async fn subscribe(
        &mut self,
        socket: &mut Socket,
        topics: &[String],
    ) -> Result<SubscriptionOutcome, String> {
        let mut accepted_topics = self.active_topics.clone();
        let mut quarantined_topics = BTreeSet::new();
        let mut preactivation_klines = BTreeMap::new();
        let mut saw_market_data = false;
        let mut chunks = topics
            .chunks(TOPICS_PER_MESSAGE)
            .map(<[String]>::to_vec)
            .collect::<VecDeque<_>>();
        while let Some(chunk) = chunks.pop_front() {
            let req_id = format!("s{}-{}", self.epoch, self.next_request_nonce);
            self.next_request_nonce = self.next_request_nonce.saturating_add(1);
            let payload = serde_json::json!({
                "req_id": req_id,
                "op": "subscribe",
                "args": &chunk,
            })
            .to_string();
            tokio::time::timeout(
                self.options.write_timeout,
                socket.send(Message::text(payload)),
            )
            .await
            .map_err(|_| "Bybit public subscription write timed out".to_owned())?
            .map_err(|error| format!("Bybit public subscription write: {error}"))?;
            let requested_topics = chunk.iter().cloned().collect::<BTreeSet<_>>();
            let mut staged = Vec::<(ParsedMessage, i64)>::new();
            let mut staged_bytes = 0_usize;
            let (success, ret_code, ret_msg) =
                tokio::time::timeout(self.options.subscribe_timeout, async {
                    loop {
                        let message = socket
                            .next()
                            .await
                            .ok_or_else(|| {
                                "Bybit public socket closed before subscribe ack".to_owned()
                            })?
                            .map_err(|error| format!("Bybit public subscription read: {error}"))?;
                        if let Message::Ping(payload) = &message {
                            tokio::time::timeout(
                                self.options.write_timeout,
                                socket.send(Message::Pong(payload.clone())),
                            )
                            .await
                            .map_err(|_| "Bybit public subscription pong timed out".to_owned())?
                            .map_err(|error| format!("Bybit public subscription pong: {error}"))?;
                            continue;
                        }
                        let received_at_ms = wall_ms().map_err(|error| error.to_string())?;
                        match parse_socket_message(&message, received_at_ms)? {
                            ParsedMessage::Ack {
                                op,
                                req_id: ack_req_id,
                                success,
                                ret_code,
                                ret_msg,
                            } if op == "subscribe"
                                && ack_req_id.as_deref() == Some(req_id.as_str()) =>
                            {
                                return Ok::<_, String>((success, ret_code, ret_msg));
                            }
                            ParsedMessage::Pong | ParsedMessage::Ignore => {}
                            parsed => {
                                let topic = parsed_topic(&parsed);
                                if topic
                                    .as_ref()
                                    .is_some_and(|topic| accepted_topics.contains(topic))
                                {
                                    saw_market_data |= self
                                        .apply_subscription_message(
                                            parsed,
                                            received_at_ms,
                                            &mut preactivation_klines,
                                        )
                                        .await?;
                                    continue;
                                }
                                if !topic
                                    .as_ref()
                                    .is_some_and(|topic| requested_topics.contains(topic))
                                {
                                    continue;
                                }
                                let message_bytes = message_payload_len(&message);
                                if staged.len() >= MAX_STREAM_EVENTS
                                    || message_bytes
                                        > MAX_SUBSCRIPTION_STAGED_BYTES.saturating_sub(staged_bytes)
                                {
                                    return Err(
                                        "Bybit pre-activation frame buffer exceeded its bound"
                                            .to_owned(),
                                    );
                                }
                                staged_bytes = staged_bytes.saturating_add(message_bytes);
                                staged.push((parsed, received_at_ms));
                            }
                        }
                    }
                })
                .await
                .map_err(|_| "Bybit public subscription reply timed out".to_owned())??;
            if success {
                accepted_topics.extend(chunk);
                self.active_topics = accepted_topics.clone();
                for (parsed, received_at_ms) in staged {
                    saw_market_data |= self
                        .apply_subscription_message(
                            parsed,
                            received_at_ms,
                            &mut preactivation_klines,
                        )
                        .await?;
                }
            } else {
                if ret_code == Some(10404) {
                    return Err(format!(
                        "Bybit public subscription global refusal retCode=10404 retMsg={ret_msg}"
                    ));
                }
                if !topic_local_subscription_refusal(ret_code, &ret_msg) {
                    return Err(format!(
                        "Bybit public subscription transient refusal retCode={} retMsg={ret_msg}",
                        ret_code
                            .map(|code| code.to_string())
                            .unwrap_or_else(|| "absent".to_owned())
                    ));
                }
                if chunk.len() == 1 {
                    quarantined_topics.insert(chunk[0].clone());
                    continue;
                }
                let midpoint = chunk.len() / 2;
                let left = chunk[..midpoint].to_vec();
                let right = chunk[midpoint..].to_vec();
                chunks.push_front(right);
                chunks.push_front(left);
            }
        }
        Ok(SubscriptionOutcome {
            accepted_topics,
            quarantined_topics,
            preactivation_klines,
            saw_market_data,
        })
    }

    async fn apply_subscription_message(
        &mut self,
        parsed: ParsedMessage,
        received_at_ms: i64,
        preactivation_klines: &mut BTreeMap<(String, i64), ConfirmedKline>,
    ) -> Result<bool, String> {
        if let ParsedMessage::Klines(rows) = parsed {
            let mut saw_market_data = false;
            for row in rows {
                if !self
                    .active_topics
                    .contains(&format!("kline.60.{}", row.symbol))
                {
                    continue;
                }
                let open_ts_ms = row
                    .row
                    .first()
                    .ok_or_else(|| "Bybit confirmed kline lacks its open clock".to_owned())
                    .and_then(|value| value_i64(value, "Bybit kline start"))?;
                saw_market_data = true;
                preactivation_klines.insert((row.symbol.clone(), open_ts_ms), row);
            }
            return Ok(saw_market_data);
        }
        self.apply(parsed, received_at_ms).await
    }

    async fn read_socket(&mut self, socket: &mut Socket) -> Result<(), String> {
        loop {
            let mut deadline = self
                .pong_deadline
                .unwrap_or(self.next_ping_at)
                .min(self.next_ping_at)
                .min(self.last_data_at + self.options.data_idle_timeout);
            if !self.quarantined_topics.is_empty() {
                deadline = deadline.min(self.next_quarantine_reprobe_at);
            }
            tokio::select! {
                incoming = socket.next() => {
                    let message = incoming
                        .ok_or_else(|| "Bybit public socket closed".to_owned())?
                        .map_err(|error| format!("Bybit public socket read: {error}"))?;
                    match message {
                        Message::Close(_) => return Err("Bybit public socket received close".to_owned()),
                        Message::Ping(payload) => {
                            tokio::time::timeout(
                                self.options.write_timeout,
                                socket.send(Message::Pong(payload)),
                            )
                            .await
                                .map_err(|_| "Bybit public protocol pong timed out".to_owned())?
                                .map_err(|error| format!("Bybit public protocol pong: {error}"))?;
                        }
                        _ => {
                            let received_at_ms = wall_ms().map_err(|error| error.to_string())?;
                            match parse_socket_message(&message, received_at_ms) {
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
                _ = tokio::time::sleep_until(deadline.into()) => {
                    let now = Instant::now();
                    if self.pong_deadline.is_some_and(|deadline| now >= deadline) {
                        return Err("Bybit public keep-alive was unanswered".to_owned());
                    }
                    if now >= self.last_data_at + self.options.data_idle_timeout {
                        return Err("Bybit public data stream became idle".to_owned());
                    }
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
                        .map_err(|_| "Bybit public ping write timed out".to_owned())?
                        .map_err(|error| format!("Bybit public ping write: {error}"))?;
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
        let candidates = self.quarantined_topics.iter().cloned().collect::<Vec<_>>();
        if candidates.is_empty() {
            return Ok(());
        }
        match self.subscribe(socket, &candidates).await {
            Ok(outcome) => {
                self.active_topics = outcome.accepted_topics;
                self.quarantined_topics = outcome.quarantined_topics;
                {
                    let mut state = self
                        .shared
                        .lock()
                        .expect("Bybit stream state lock poisoned");
                    state.update_topic_counts(&self.active_topics, &self.quarantined_topics);
                }
                let mut saw_market_data = outcome.saw_market_data;
                for row in outcome.preactivation_klines.into_values() {
                    let received_at_ms = row.available_at_ms;
                    saw_market_data |= self
                        .apply(ParsedMessage::Klines(vec![row]), received_at_ms)
                        .await?;
                }
                if saw_market_data {
                    self.backoff = self.options.backoff_start;
                }
            }
            Err(error) => {
                self.quarantined_topics
                    .retain(|topic| !self.active_topics.contains(topic));
                {
                    let mut state = self
                        .shared
                        .lock()
                        .expect("Bybit stream state lock poisoned");
                    state.update_topic_counts(&self.active_topics, &self.quarantined_topics);
                }
                if error.starts_with("Bybit public subscription transient refusal") {
                    self.fault(format!("Bybit quarantined-topic re-probe: {error}"));
                } else {
                    return Err(format!("Bybit quarantined-topic re-probe: {error}"));
                }
            }
        }
        self.next_quarantine_reprobe_at = Instant::now() + self.options.quarantine_reprobe_interval;
        Ok(())
    }

    async fn apply(&mut self, parsed: ParsedMessage, received_at_ms: i64) -> Result<bool, String> {
        match parsed {
            ParsedMessage::Ticker(frame) => {
                if !self
                    .active_topics
                    .contains(&format!("tickers.{}", frame.row.symbol))
                {
                    return Ok(false);
                }
                self.last_data_at = Instant::now();
                let mut state = self
                    .shared
                    .lock()
                    .expect("Bybit stream state lock poisoned");
                state.tickers.apply(*frame, received_at_ms);
                state.saw_frame(received_at_ms);
                Ok(true)
            }
            ParsedMessage::Klines(mut rows) => {
                rows.retain(|row| {
                    self.active_topics
                        .contains(&format!("kline.60.{}", row.symbol))
                });
                if rows.is_empty() {
                    return Ok(false);
                }
                self.last_data_at = Instant::now();
                {
                    let mut state = self
                        .shared
                        .lock()
                        .expect("Bybit stream state lock poisoned");
                    state.saw_frame(received_at_ms);
                }
                for row in rows {
                    if !self.symbols.contains(&row.symbol) {
                        continue;
                    }
                    self.events
                        .try_send(StreamEvent::KlineClosed(row))
                        .map_err(|error| {
                            format!(
                                "Bybit confirmed-kline queue requires reconnect repair: {error}"
                            )
                        })?;
                }
                Ok(true)
            }
            ParsedMessage::Pong => {
                self.pong_deadline = None;
                Ok(false)
            }
            ParsedMessage::Ack { .. } | ParsedMessage::Ignore => Ok(false),
        }
    }

    fn bump_backoff(&mut self) {
        self.backoff = self.backoff.saturating_mul(2).min(self.options.backoff_max);
    }

    fn fault(&self, error: String) {
        let mut state = self
            .shared
            .lock()
            .expect("Bybit stream state lock poisoned");
        state.health.fault_count = state.health.fault_count.saturating_add(1);
        drop(state);
        let _ = self.events.try_send(StreamEvent::Fault(error));
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TickerKind {
    Snapshot,
    Delta,
}

#[derive(Clone, Debug, PartialEq)]
struct TickerFrame {
    kind: TickerKind,
    row: BybitTickerWire,
}

#[derive(Clone, Debug, PartialEq)]
enum ParsedMessage {
    Ack {
        op: String,
        req_id: Option<String>,
        success: bool,
        ret_code: Option<i64>,
        ret_msg: String,
    },
    Pong,
    Ticker(Box<TickerFrame>),
    Klines(Vec<ConfirmedKline>),
    Ignore,
}

fn parse_socket_message(message: &Message, received_at_ms: i64) -> Result<ParsedMessage, String> {
    match message {
        Message::Text(text) => parse_frame(text.as_str(), received_at_ms),
        Message::Binary(bytes) => {
            let text = std::str::from_utf8(bytes)
                .map_err(|error| format!("Bybit public binary frame is not UTF-8: {error}"))?;
            parse_frame(text, received_at_ms)
        }
        _ => Ok(ParsedMessage::Ignore),
    }
}

fn parse_frame(text: &str, received_at_ms: i64) -> Result<ParsedMessage, String> {
    let value: Value = serde_json::from_str(text)
        .map_err(|error| format!("Bybit public frame is invalid JSON: {error}"))?;
    if let Some(op) = value.get("op").and_then(Value::as_str) {
        if op == "pong" || value.get("ret_msg").and_then(Value::as_str) == Some("pong") {
            return Ok(ParsedMessage::Pong);
        }
        if let Some(success) = value.get("success").and_then(Value::as_bool) {
            return Ok(ParsedMessage::Ack {
                op: op.to_owned(),
                req_id: value
                    .get("req_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                success,
                ret_code: value
                    .get("retCode")
                    .or_else(|| value.get("ret_code"))
                    .and_then(|value| match value {
                        Value::Number(number) => number.as_i64(),
                        Value::String(text) => text.parse().ok(),
                        _ => None,
                    }),
                ret_msg: value
                    .get("ret_msg")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
            });
        }
    }
    let Some(topic) = value.get("topic").and_then(Value::as_str) else {
        return Ok(ParsedMessage::Ignore);
    };
    if let Some(topic_symbol) = topic.strip_prefix("tickers.") {
        let kind = match value.get("type").and_then(Value::as_str) {
            Some("snapshot") => TickerKind::Snapshot,
            Some("delta") => TickerKind::Delta,
            _ => return Err("Bybit ticker frame has an unknown type".to_owned()),
        };
        let data = value
            .get("data")
            .and_then(Value::as_object)
            .ok_or_else(|| "Bybit ticker frame lacks object data".to_owned())?;
        let row = ticker_wire(&Value::Object(data.clone())).map_err(|error| error.to_string())?;
        if row.symbol != topic_symbol.to_ascii_uppercase() {
            return Err("Bybit ticker topic and payload symbols disagree".to_owned());
        }
        normalize_ticker_strict(received_at_ms, received_at_ms, &row)
            .map_err(|error| error.to_string())?;
        return Ok(ParsedMessage::Ticker(Box::new(TickerFrame { kind, row })));
    }
    let Some(suffix) = topic.strip_prefix("kline.60.") else {
        return Ok(ParsedMessage::Ignore);
    };
    let topic_symbol = suffix.to_ascii_uppercase();
    let data = value
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| "Bybit kline frame lacks list data".to_owned())?;
    let mut rows = Vec::new();
    for item in data {
        if item.get("confirm").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let symbol = item
            .get("symbol")
            .and_then(Value::as_str)
            .map(str::to_ascii_uppercase)
            .unwrap_or_else(|| topic_symbol.clone());
        if symbol != topic_symbol {
            return Err("Bybit kline topic and payload symbols disagree".to_owned());
        }
        let start = required(item, "start", "Bybit kline start")?;
        let start_ms = value_i64(start, "Bybit kline start")?;
        if start_ms <= 0 || start_ms % HOUR_MS != 0 || received_at_ms < start_ms + HOUR_MS {
            return Err("Bybit confirmed kline has an invalid close clock".to_owned());
        }
        let row = ConfirmedKline {
            symbol,
            available_at_ms: received_at_ms,
            row: vec![
                start.clone(),
                required(item, "open", "Bybit kline open")?.clone(),
                required(item, "high", "Bybit kline high")?.clone(),
                required(item, "low", "Bybit kline low")?.clone(),
                required(item, "close", "Bybit kline close")?.clone(),
                required(item, "volume", "Bybit kline volume")?.clone(),
                required(item, "turnover", "Bybit kline turnover")?.clone(),
            ],
        };
        normalize_kline_rows(
            &row.symbol,
            row.available_at_ms,
            std::slice::from_ref(&row.row),
        )
        .map_err(|error| error.to_string())?;
        rows.push(row);
    }
    Ok(ParsedMessage::Klines(rows))
}

fn parsed_topic(parsed: &ParsedMessage) -> Option<String> {
    match parsed {
        ParsedMessage::Ticker(frame) => Some(format!("tickers.{}", frame.row.symbol)),
        ParsedMessage::Klines(rows) => rows.first().map(|row| format!("kline.60.{}", row.symbol)),
        ParsedMessage::Ack { .. } | ParsedMessage::Pong | ParsedMessage::Ignore => None,
    }
}

fn message_payload_len(message: &Message) -> usize {
    match message {
        Message::Text(text) => text.len(),
        Message::Binary(bytes) | Message::Ping(bytes) | Message::Pong(bytes) => bytes.len(),
        Message::Close(frame) => frame.as_ref().map_or(0, |frame| frame.reason.len()),
        Message::Frame(frame) => frame.payload().len(),
    }
}

fn topic_local_subscription_refusal(ret_code: Option<i64>, ret_msg: &str) -> bool {
    if ret_code == Some(10001) {
        return true;
    }
    if ret_code.is_some() {
        return false;
    }
    let message = ret_msg.to_ascii_lowercase();
    [
        "bad topic",
        "bad symbol",
        "invalid topic",
        "invalid symbol",
        "topic does not exist",
        "symbol does not exist",
    ]
    .iter()
    .any(|needle| message.contains(needle))
}

pub(crate) fn ticker_wire(value: &Value) -> Result<BybitTickerWire, WorkerError> {
    let symbol = value
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| WorkerError::network("Bybit ticker lacks symbol"))?
        .to_ascii_uppercase();
    Ok(BybitTickerWire {
        symbol,
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: value.get("lastPrice").cloned(),
        mark_price: value.get("markPrice").cloned(),
        index_price: value.get("indexPrice").cloned(),
        bid1_price: value.get("bid1Price").cloned(),
        ask1_price: value.get("ask1Price").cloned(),
        bid1_size: value.get("bid1Size").cloned(),
        ask1_size: value.get("ask1Size").cloned(),
        open_interest: value.get("openInterest").cloned(),
        open_interest_value: value.get("openInterestValue").cloned(),
        turnover24h: value.get("turnover24h").cloned(),
        volume24h: value.get("volume24h").cloned(),
        funding_rate: value.get("fundingRate").cloned(),
        next_funding_time: value.get("nextFundingTime").cloned(),
    })
}

fn merge_ticker(existing: &mut CachedTicker, incoming: BybitTickerWire, received_at_ms: i64) {
    macro_rules! replace_some {
        ($field:ident) => {
            if incoming.$field.is_some() {
                existing.row.$field = incoming.$field;
                existing.freshness.$field = Some(received_at_ms);
            }
        };
    }
    replace_some!(last_price);
    replace_some!(mark_price);
    replace_some!(index_price);
    replace_some!(bid1_price);
    replace_some!(ask1_price);
    replace_some!(bid1_size);
    replace_some!(ask1_size);
    replace_some!(open_interest);
    replace_some!(open_interest_value);
    replace_some!(turnover24h);
    replace_some!(volume24h);
    replace_some!(funding_rate);
    replace_some!(next_funding_time);
}

fn required<'a>(value: &'a Value, key: &str, label: &str) -> Result<&'a Value, String> {
    value.get(key).ok_or_else(|| format!("{label} is absent"))
}

fn value_i64(value: &Value, label: &str) -> Result<i64, String> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| format!("{label} is not an integer"))
}

fn normalize_symbols(symbols: Vec<String>) -> Result<BTreeSet<String>, WorkerError> {
    let mut out = BTreeSet::new();
    for symbol in symbols {
        let symbol = symbol.trim().to_ascii_uppercase();
        if symbol.is_empty()
            || !symbol
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
        {
            return Err(WorkerError::config("Bybit stream symbol is invalid"));
        }
        out.insert(symbol);
    }
    if out.is_empty() {
        return Err(WorkerError::config("Bybit stream has no symbols"));
    }
    Ok(out)
}

fn topics(symbols: &BTreeSet<String>) -> Vec<String> {
    let mut topics = Vec::with_capacity(symbols.len().saturating_mul(2));
    for symbol in symbols {
        topics.push(format!("tickers.{symbol}"));
        topics.push(format!("kline.60.{symbol}"));
    }
    topics
}

fn stream_event_capacity(symbols: usize) -> usize {
    symbols.saturating_mul(2).clamp(64, MAX_STREAM_EVENTS)
}

fn install_crypto_provider() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ticker(kind: &str, symbol: &str, fields: &str) -> String {
        format!(
            r#"{{"topic":"tickers.{symbol}","type":"{kind}","ts":1,"data":{{"symbol":"{symbol}",{fields}}}}}"#
        )
    }

    fn kline(symbol: &str, start: i64, confirm: bool) -> String {
        format!(
            r#"{{"topic":"kline.60.{symbol}","type":"snapshot","ts":{},"data":[{{"start":{start},"end":{},"interval":"60","open":"100","high":"110","low":"90","close":"105","volume":"2","turnover":"205","confirm":{confirm},"timestamp":{}}}]}}"#,
            start + HOUR_MS,
            start + HOUR_MS - 1,
            start + HOUR_MS,
        )
    }

    #[test]
    fn a_replacement_stream_continues_the_epoch_and_the_gap_clock() {
        // A universe refresh replaces the stream object, and the health record
        // lives in it. A successor that starts fresh reuses epoch numbers a
        // repair lane spawned for the outgoing stream still carries, and dates
        // the gap from the refresh instead of from the outage.
        let symbols = BTreeSet::from(["BTCUSDT".to_owned(), "ETHUSDT".to_owned()]);
        let outgoing = StreamHealth {
            connected: true,
            epoch: 3,
            gap_open: true,
            gap_open_since_ms: Some(1_788_538_993_000),
            reconnect_count: 2,
            fault_count: 5,
            ..StreamHealth::default()
        };

        let restarted = SharedState::continuing(&symbols, StreamContinuity::default());
        assert_eq!(restarted.health.epoch, 0);
        assert_eq!(restarted.health.gap_open_since_ms, None);

        let mut successor = SharedState::continuing(&symbols, StreamContinuity::from(&outgoing));
        assert_eq!(successor.health.reconnect_count, 2);
        assert_eq!(successor.health.fault_count, 5);
        assert!(successor.health.gap_open);
        successor.prepare_epoch(successor.health.epoch + 1, 1_788_546_193_000);

        assert_eq!(
            successor.health.epoch, 4,
            "the successor's first epoch is above every epoch an in-flight repair still holds"
        );
        assert_eq!(
            successor.health.gap_open_since_ms,
            Some(1_788_538_993_000),
            "the gap is as old as the transport's, not as old as the rebuild"
        );
    }

    #[test]
    fn parser_keeps_only_confirmed_hourly_klines() {
        let start = 10 * HOUR_MS;
        assert_eq!(
            parse_frame(&kline("BTCUSDT", start, false), start + HOUR_MS).unwrap(),
            ParsedMessage::Klines(Vec::new())
        );
        let ParsedMessage::Klines(rows) =
            parse_frame(&kline("BTCUSDT", start, true), start + HOUR_MS).unwrap()
        else {
            panic!("expected kline rows");
        };
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].symbol, "BTCUSDT");
        assert_eq!(rows[0].row[0], Value::from(start));
        assert_eq!(rows[0].row[4], Value::from("105"));
    }

    #[test]
    fn parser_rejects_semantically_invalid_market_rows_before_the_shared_loop() {
        let bad_ticker = ticker("snapshot", "BTCUSDT", r#""markPrice":"not-a-number""#);
        assert!(parse_frame(&bad_ticker, 10 * HOUR_MS).is_err());

        let start = 10 * HOUR_MS;
        let bad_kline =
            kline("BTCUSDT", start, true).replace(r#""open":"100""#, r#""open":"not-a-number""#);
        assert!(parse_frame(&bad_kline, start + HOUR_MS).is_err());
    }

    #[test]
    fn subscription_ack_parser_preserves_numeric_and_string_return_codes() {
        for frame in [
            r#"{"op":"subscribe","success":false,"retCode":10429,"ret_msg":"busy","req_id":"s1-0"}"#,
            r#"{"op":"subscribe","success":false,"ret_code":"10016","ret_msg":"restart","req_id":"s1-1"}"#,
        ] {
            let ParsedMessage::Ack {
                success, ret_code, ..
            } = parse_frame(frame, 1_000).unwrap()
            else {
                panic!("expected subscribe ack");
            };
            assert!(!success);
            assert!(matches!(ret_code, Some(10429 | 10016)));
        }
        assert!(!topic_local_subscription_refusal(Some(10429), "busy"));
        assert!(!topic_local_subscription_refusal(Some(10016), "restart"));
        assert!(!topic_local_subscription_refusal(Some(10019), "restart"));
        assert!(!topic_local_subscription_refusal(Some(20003), "slow down"));
        assert!(!topic_local_subscription_refusal(
            Some(10404),
            "op type is not found"
        ));
        assert!(!topic_local_subscription_refusal(None, "route not found"));
        assert!(!topic_local_subscription_refusal(None, "invalid operation"));
        assert!(topic_local_subscription_refusal(Some(10001), "bad param"));
        assert!(topic_local_subscription_refusal(None, "invalid topic"));
    }

    #[test]
    fn ticker_deltas_merge_into_a_full_bounded_cache() {
        let allowed = ["BTCUSDT".to_owned(), "ETHUSDT".to_owned()]
            .into_iter()
            .collect();
        let mut cache = TickerCache::new(allowed);
        let frames = [
            ticker(
                "snapshot",
                "BTCUSDT",
                r#""lastPrice":"100","markPrice":"101","fundingRate":"-0.001""#,
            ),
            ticker("delta", "BTCUSDT", r#""markPrice":"102""#),
            ticker("snapshot", "ETHUSDT", r#""markPrice":"50""#),
            ticker("snapshot", "UNKNOWN", r#""markPrice":"999""#),
        ];
        for (index, frame) in frames.iter().enumerate() {
            let ParsedMessage::Ticker(frame) = parse_frame(frame, 1_000 + index as i64).unwrap()
            else {
                panic!("expected ticker");
            };
            cache.apply(*frame, 1_000 + index as i64);
        }
        for index in 0..10_000 {
            let frame = ticker(
                "delta",
                "BTCUSDT",
                &format!(r#""markPrice":"{}""#, 103 + index),
            );
            let ParsedMessage::Ticker(frame) = parse_frame(&frame, 2_000 + index).unwrap() else {
                panic!("expected ticker");
            };
            cache.apply(*frame, 2_000 + index);
        }
        assert_eq!(cache.len(), 2);
        assert_eq!(cache.capacity(), 2);
        let rows = cache.sample(12_000, 30_000);
        assert_eq!(rows[0].last_price, Some(Value::from("100")));
        assert_eq!(rows[0].mark_price, Some(Value::from("10102")));
        cache.clear();
        assert_eq!(cache.len(), 0);
    }

    #[test]
    fn ticker_fields_age_independently_and_one_stale_symbol_does_not_block_others() {
        let allowed = ["BTCUSDT".to_owned(), "ETHUSDT".to_owned()]
            .into_iter()
            .collect();
        let mut cache = TickerCache::new(allowed);
        let ParsedMessage::Ticker(btc) = parse_frame(
            &ticker(
                "snapshot",
                "BTCUSDT",
                r#""markPrice":"100","fundingRate":"0.001","nextFundingTime":"100000""#,
            ),
            1_000,
        )
        .unwrap() else {
            panic!("expected ticker");
        };
        cache.apply(*btc, 1_000);
        let ParsedMessage::Ticker(eth) =
            parse_frame(&ticker("snapshot", "ETHUSDT", r#""markPrice":"50""#), 1_000).unwrap()
        else {
            panic!("expected ticker");
        };
        cache.apply(*eth, 1_000);
        let ParsedMessage::Ticker(delta) =
            parse_frame(&ticker("delta", "BTCUSDT", r#""markPrice":"101""#), 2_500).unwrap()
        else {
            panic!("expected ticker");
        };
        cache.apply(*delta, 2_500);

        let rows = cache.sample(2_600, 1_000);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].symbol, "BTCUSDT");
        assert_eq!(rows[0].mark_price, Some(Value::from("101")));
        assert_eq!(rows[0].mark_observed_ts_ms, Some(2_500));
        assert_eq!(rows[0].funding_rate, None);
        assert_eq!(rows[0].funding_observed_ts_ms, None);
        assert_eq!(rows[0].next_funding_time, Some(Value::from("100000")));
        assert_eq!(rows[0].schedule_observed_ts_ms, Some(1_000));
        assert_eq!(cache.sample(100_001, 1_000).len(), 0);
    }

    #[test]
    fn fresh_funding_schedule_survives_a_stale_mark() {
        let allowed = ["BTCUSDT".to_owned()].into_iter().collect();
        let mut cache = TickerCache::new(allowed);
        let ParsedMessage::Ticker(snapshot) = parse_frame(
            &ticker("snapshot", "BTCUSDT", r#""markPrice":"100""#),
            1_000,
        )
        .unwrap() else {
            panic!("expected ticker");
        };
        cache.apply(*snapshot, 1_000);
        let ParsedMessage::Ticker(delta) = parse_frame(
            &ticker(
                "delta",
                "BTCUSDT",
                r#""fundingRate":"0.001","nextFundingTime":"100000""#,
            ),
            2_500,
        )
        .unwrap() else {
            panic!("expected ticker");
        };
        cache.apply(*delta, 2_500);

        let rows = cache.sample(2_600, 1_000);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].mark_price, None);
        assert_eq!(rows[0].mark_observed_ts_ms, None);
        assert_eq!(rows[0].funding_rate, Some(Value::from("0.001")));
        assert_eq!(rows[0].funding_observed_ts_ms, Some(2_500));
        assert_eq!(rows[0].next_funding_time, Some(Value::from("100000")));
    }

    #[test]
    fn rest_reconcile_heals_stale_fields_without_rolling_back_same_millisecond_ws_data() {
        let allowed = ["BTCUSDT".to_owned()].into_iter().collect();
        let mut cache = TickerCache::new(allowed);
        let ParsedMessage::Ticker(snapshot) = parse_frame(
            &ticker("snapshot", "BTCUSDT", r#""markPrice":"100""#),
            1_000,
        )
        .unwrap() else {
            panic!("expected ticker");
        };
        cache.apply(*snapshot, 1_000);
        let ParsedMessage::Ticker(rest_old) =
            parse_frame(&ticker("snapshot", "BTCUSDT", r#""markPrice":"90""#), 2_000).unwrap()
        else {
            panic!("expected ticker");
        };
        cache.reconcile_rest(rest_old.row, 1_500, 2_000);
        assert_eq!(
            cache.sample(2_001, 1_000)[0].mark_price,
            Some(Value::from("90"))
        );

        let ParsedMessage::Ticker(ws_new) =
            parse_frame(&ticker("delta", "BTCUSDT", r#""markPrice":"110""#), 2_500).unwrap()
        else {
            panic!("expected ticker");
        };
        cache.apply(*ws_new, 2_500);
        let ParsedMessage::Ticker(stale_response) =
            parse_frame(&ticker("snapshot", "BTCUSDT", r#""markPrice":"95""#), 3_000).unwrap()
        else {
            panic!("expected ticker");
        };
        cache.reconcile_rest(stale_response.row, 2_500, 3_000);
        let row = &cache.sample(3_001, 1_000)[0];
        assert_eq!(row.mark_price, Some(Value::from("110")));
        assert_eq!(row.mark_observed_ts_ms, Some(2_500));
    }

    #[test]
    fn event_queue_has_a_hard_cap() {
        assert_eq!(stream_event_capacity(1), 64);
        assert_eq!(stream_event_capacity(150), 300);
        assert_eq!(stream_event_capacity(10_000), MAX_STREAM_EVENTS);
    }

    #[test]
    fn public_stream_url_is_the_credential_free_linear_endpoint() {
        assert_eq!(PUBLIC_LINEAR_URL, "wss://stream.bybit.com/v5/public/linear");
    }

    async fn serve_epoch(
        listener: &tokio::net::TcpListener,
        start: i64,
        mark: &str,
        hold_open: bool,
    ) {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
            .as_str()
            .unwrap()
            .to_owned();
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": true,
                    "ret_msg": "",
                    "conn_id": "x",
                    "req_id": req_id,
                    "op": "subscribe",
                })
                .to_string(),
            ))
            .await
            .unwrap();
        socket
            .send(Message::text(ticker(
                "snapshot",
                "BTCUSDT",
                &format!(r#""lastPrice":"{mark}","markPrice":"{mark}""#),
            )))
            .await
            .unwrap();
        socket
            .send(Message::text(kline("BTCUSDT", start, true)))
            .await
            .unwrap();
        if hold_open {
            while socket.next().await.is_some() {}
        } else {
            tokio::time::sleep(Duration::from_millis(100)).await;
            socket.close(None).await.unwrap();
        }
    }

    async fn next(stream: &mut BybitPublicStream) -> StreamEvent {
        tokio::time::timeout(Duration::from_secs(3), stream.next_event())
            .await
            .unwrap()
            .unwrap()
    }

    #[tokio::test]
    async fn frames_before_the_final_subscription_ack_are_discarded_on_refusal() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let first = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let first_req = serde_json::from_str::<Value>(&first).unwrap()["req_id"]
                .as_str()
                .unwrap()
                .to_owned();
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": true,
                        "ret_msg": "",
                        "req_id": first_req,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            socket
                .send(Message::text(kline("S000USDT", 10 * HOUR_MS, true)))
                .await
                .unwrap();
            let second = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let second_req = serde_json::from_str::<Value>(&second).unwrap()["req_id"]
                .as_str()
                .unwrap()
                .to_owned();
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": false,
                        "ret_msg": "bad topic",
                        "req_id": second_req,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
        });
        let symbols = (0..60).map(|index| format!("S{index:03}USDT")).collect();
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_secs(10),
            backoff_max: Duration::from_secs(10),
        };
        let mut stream =
            BybitPublicStream::with_url(format!("ws://127.0.0.1:{port}"), symbols, options)
                .unwrap();
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::GapOpened { epoch: 1, .. }
            ) {
                break;
            }
        }
        while let Ok(event) = stream.events.try_recv() {
            assert!(!matches!(event, StreamEvent::KlineClosed(_)));
        }
        assert_eq!(stream.health().ticker_rows, 0);
        assert!(!stream.health().connected);
    }

    #[tokio::test]
    async fn one_refused_ticker_is_quarantined_without_disabling_other_topics() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let mut accepted = BTreeSet::new();
            let mut refused = false;
            while accepted.len() < 3 || !refused {
                let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
                let request: Value = serde_json::from_str(&request).unwrap();
                let req_id = request["req_id"].as_str().unwrap();
                let args = request["args"].as_array().unwrap();
                let contains_bad_ticker = args
                    .iter()
                    .any(|topic| topic.as_str() == Some("tickers.BADUSDT"));
                let success = !contains_bad_ticker;
                if success {
                    accepted.extend(args.iter().filter_map(Value::as_str).map(str::to_owned));
                } else if args.len() == 1 {
                    refused = true;
                }
                socket
                    .send(Message::text(
                        serde_json::json!({
                            "success": success,
                            "ret_msg": if success { "" } else { "bad topic" },
                            "req_id": req_id,
                            "op": "subscribe"
                        })
                        .to_string(),
                    ))
                    .await
                    .unwrap();
            }
            socket
                .send(Message::text(ticker(
                    "snapshot",
                    "BTCUSDT",
                    r#""markPrice":"100""#,
                )))
                .await
                .unwrap();
            socket
                .send(Message::text(kline("BTCUSDT", 10 * HOUR_MS, true)))
                .await
                .unwrap();
            while socket.next().await.is_some() {}
        });
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_secs(10),
            backoff_max: Duration::from_secs(10),
        };
        let mut stream = BybitPublicStream::with_url(
            format!("ws://127.0.0.1:{port}"),
            vec!["BADUSDT".into(), "BTCUSDT".into()],
            options,
        )
        .unwrap();
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::EpochStarted { epoch: 1, .. }
            ) {
                break;
            }
        }
        let health = stream.health();
        assert!(health.connected);
        assert!(health.gap_open);
        assert_eq!(health.ticker_topics_accepted, 1);
        assert_eq!(health.ticker_topics_quarantined, 1);
        assert_eq!(health.kline_topics_accepted, 2);
        assert_eq!(health.kline_topics_quarantined, 0);
        assert!(stream.mark_gap_repaired(1));
        assert!(!stream.health().gap_open);
        loop {
            if matches!(next(&mut stream).await, StreamEvent::KlineClosed(_)) {
                break;
            }
        }
        let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
        assert_eq!(sample.rows.len(), 1);
        assert_eq!(sample.rows[0].symbol, "BTCUSDT");
    }

    #[tokio::test]
    async fn quarantined_topic_reprobes_use_unique_ids_and_survive_transient_refusal() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (reprobe_tx, reprobe_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let mut accepted = BTreeSet::new();
            let mut seen_req_ids = BTreeSet::new();
            let mut refused = false;
            while accepted.len() < 3 || !refused {
                let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
                let request: Value = serde_json::from_str(&request).unwrap();
                let req_id = request["req_id"].as_str().unwrap().to_owned();
                assert!(seen_req_ids.insert(req_id.clone()), "duplicate request id");
                let args = request["args"].as_array().unwrap();
                let contains_bad_ticker = args
                    .iter()
                    .any(|topic| topic.as_str() == Some("tickers.BADUSDT"));
                let success = !contains_bad_ticker;
                if success {
                    accepted.extend(args.iter().filter_map(Value::as_str).map(str::to_owned));
                } else if args.len() == 1 {
                    refused = true;
                }
                socket
                    .send(Message::text(
                        serde_json::json!({
                            "success": success,
                            "retCode": if success { 0 } else { 10001 },
                            "ret_msg": if success { "" } else { "invalid symbol" },
                            "req_id": &req_id,
                            "op": "subscribe"
                        })
                        .to_string(),
                    ))
                    .await
                    .unwrap();
            }
            socket
                .send(Message::text(ticker(
                    "snapshot",
                    "BTCUSDT",
                    r#""markPrice":"100""#,
                )))
                .await
                .unwrap();
            let first_reprobe = tokio::time::timeout(Duration::from_secs(1), socket.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap()
                .into_text()
                .unwrap();
            let first_reprobe: Value = serde_json::from_str(&first_reprobe).unwrap();
            assert_eq!(
                first_reprobe["args"].as_array().unwrap(),
                &[Value::from("tickers.BADUSDT")]
            );
            let first_reprobe_id = first_reprobe["req_id"].as_str().unwrap().to_owned();
            assert!(
                seen_req_ids.insert(first_reprobe_id.clone()),
                "re-probe reused an earlier request id"
            );
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": false,
                        "retCode": 10429,
                        "ret_msg": "system frequency protection",
                        "req_id": first_reprobe_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            socket
                .send(Message::text(ticker(
                    "delta",
                    "BTCUSDT",
                    r#""markPrice":"101""#,
                )))
                .await
                .unwrap();
            let second_reprobe = tokio::time::timeout(Duration::from_secs(1), socket.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap()
                .into_text()
                .unwrap();
            let second_reprobe: Value = serde_json::from_str(&second_reprobe).unwrap();
            assert_eq!(
                second_reprobe["args"].as_array().unwrap(),
                &[Value::from("tickers.BADUSDT")]
            );
            let second_reprobe_id = second_reprobe["req_id"].as_str().unwrap().to_owned();
            assert!(
                seen_req_ids.insert(second_reprobe_id.clone()),
                "second re-probe reused an earlier request id"
            );
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": true,
                        "retCode": 0,
                        "ret_msg": "",
                        "req_id": second_reprobe_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            socket
                .send(Message::text(ticker(
                    "snapshot",
                    "BADUSDT",
                    r#""markPrice":"50""#,
                )))
                .await
                .unwrap();
            reprobe_tx.send(()).unwrap();
            while socket.next().await.is_some() {}
        });
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_millis(20),
            backoff_start: Duration::from_secs(10),
            backoff_max: Duration::from_secs(10),
        };
        let mut stream = BybitPublicStream::with_url(
            format!("ws://127.0.0.1:{port}"),
            vec!["BADUSDT".into(), "BTCUSDT".into()],
            options,
        )
        .unwrap();
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::EpochStarted { epoch: 1, .. }
            ) {
                break;
            }
        }
        tokio::time::timeout(Duration::from_secs(1), reprobe_rx)
            .await
            .unwrap()
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                let health = stream.health();
                if health.ticker_topics_quarantined == 0 && health.ticker_rows == 2 {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let health = stream.health();
        assert_eq!(health.epoch, 1);
        assert_eq!(health.reconnect_count, 0);
        assert_eq!(health.ticker_topics_accepted, 2);
        assert_eq!(health.ticker_topics_quarantined, 0);
        let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
        assert_eq!(sample.rows.len(), 2);
    }

    #[tokio::test]
    async fn transient_subscription_refusal_reconnects_without_quarantining_topics() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (first, _) = listener.accept().await.unwrap();
            let mut first = tokio_tungstenite::accept_async(first).await.unwrap();
            let request = first.next().await.unwrap().unwrap().into_text().unwrap();
            let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
                .as_str()
                .unwrap()
                .to_owned();
            first
                .send(Message::text(
                    serde_json::json!({
                        "success": false,
                        "retCode": 10429,
                        "ret_msg": "system frequency protection",
                        "req_id": req_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            drop(first);

            let (second, _) = listener.accept().await.unwrap();
            let mut second = tokio_tungstenite::accept_async(second).await.unwrap();
            let request = second.next().await.unwrap().unwrap().into_text().unwrap();
            let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
                .as_str()
                .unwrap()
                .to_owned();
            second
                .send(Message::text(
                    serde_json::json!({
                        "success": true,
                        "retCode": 0,
                        "ret_msg": "",
                        "req_id": req_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            second
                .send(Message::text(ticker(
                    "snapshot",
                    "BTCUSDT",
                    r#""markPrice":"100""#,
                )))
                .await
                .unwrap();
            while second.next().await.is_some() {}
        });
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_millis(10),
            backoff_max: Duration::from_millis(20),
        };
        let mut stream = BybitPublicStream::with_url(
            format!("ws://127.0.0.1:{port}"),
            vec!["BTCUSDT".into()],
            options,
        )
        .unwrap();
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::EpochStarted {
                    epoch: 2,
                    reconnected: true,
                    ..
                }
            ) {
                break;
            }
        }
        let health = stream.health();
        assert!(health.connected);
        assert_eq!(health.ticker_topics_quarantined, 0);
        assert_eq!(health.kline_topics_quarantined, 0);
        assert_eq!(health.reconnect_count, 1);
    }

    #[tokio::test]
    async fn global_10404_refusal_never_bisects_or_quarantines_topics() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (request_count_tx, request_count_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
                .as_str()
                .unwrap()
                .to_owned();
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": false,
                        "retCode": 10404,
                        "ret_msg": "op type is not found",
                        "req_id": req_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            let extra = matches!(
                tokio::time::timeout(Duration::from_millis(100), socket.next()).await,
                Ok(Some(Ok(Message::Text(text)))) if text.contains("subscribe")
            );
            let _ = request_count_tx.send(1 + usize::from(extra));
        });
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_secs(10),
            backoff_max: Duration::from_secs(10),
        };
        let mut stream = BybitPublicStream::with_url(
            format!("ws://127.0.0.1:{port}"),
            vec!["BTCUSDT".into(), "ETHUSDT".into()],
            options,
        )
        .unwrap();
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::GapOpened { epoch: 1, .. }
            ) {
                break;
            }
        }
        assert_eq!(request_count_rx.await.unwrap(), 1);
        assert_eq!(stream.health().ticker_topics_quarantined, 0);
        assert_eq!(stream.health().kline_topics_quarantined, 0);
    }

    #[tokio::test]
    async fn accepted_topic_flood_bypasses_the_unacknowledged_chunk_buffer() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let first = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let first: Value = serde_json::from_str(&first).unwrap();
            assert!(first["args"]
                .as_array()
                .unwrap()
                .iter()
                .any(|topic| topic == "tickers.S000USDT"));
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": true,
                        "ret_msg": "",
                        "req_id": first["req_id"],
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            let second = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let second: Value = serde_json::from_str(&second).unwrap();
            socket
                .send(Message::text(ticker(
                    "snapshot",
                    "S000USDT",
                    r#""markPrice":"1""#,
                )))
                .await
                .unwrap();
            for index in 1..MAX_STREAM_EVENTS + 32 {
                socket
                    .send(Message::text(ticker(
                        "delta",
                        "S000USDT",
                        &format!(r#""markPrice":"{index}""#),
                    )))
                    .await
                    .unwrap();
            }
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": true,
                        "ret_msg": "",
                        "req_id": second["req_id"],
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            while socket.next().await.is_some() {}
        });
        let symbols = (0..60).map(|index| format!("S{index:03}USDT")).collect();
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(10),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_secs(10),
            backoff_max: Duration::from_secs(10),
        };
        let mut stream =
            BybitPublicStream::with_url(format!("ws://127.0.0.1:{port}"), symbols, options)
                .unwrap();
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::EpochStarted { epoch: 1, .. }
            ) {
                break;
            }
        }
        let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
        let row = sample
            .rows
            .iter()
            .find(|row| row.symbol == "S000USDT")
            .unwrap();
        assert_eq!(
            row.mark_price,
            Some(Value::from((MAX_STREAM_EVENTS + 31).to_string()))
        );
        assert_eq!(stream.health().queued_frames, 0);
    }

    #[tokio::test]
    async fn ack_and_pong_only_epochs_do_not_reset_reconnect_backoff() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (accepted_tx, mut accepted_rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(async move {
            for _ in 0..3 {
                let (stream, _) = listener.accept().await.unwrap();
                accepted_tx.send(Instant::now()).unwrap();
                let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
                let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
                let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
                    .as_str()
                    .unwrap()
                    .to_owned();
                socket
                    .send(Message::text(
                        serde_json::json!({
                            "success": true,
                            "ret_msg": "",
                            "req_id": req_id,
                            "op": "subscribe"
                        })
                        .to_string(),
                    ))
                    .await
                    .unwrap();
                socket
                    .send(Message::text(r#"{"op":"pong"}"#))
                    .await
                    .unwrap();
                while socket.next().await.is_some() {}
            }
        });
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(1),
            pong_timeout: Duration::from_millis(100),
            data_idle_timeout: Duration::from_millis(15),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_millis(30),
            backoff_max: Duration::from_millis(120),
        };
        let stream = BybitPublicStream::with_url(
            format!("ws://127.0.0.1:{port}"),
            vec!["BTCUSDT".into()],
            options,
        )
        .unwrap();
        let first = tokio::time::timeout(Duration::from_secs(2), accepted_rx.recv())
            .await
            .unwrap()
            .unwrap();
        let second = tokio::time::timeout(Duration::from_secs(2), accepted_rx.recv())
            .await
            .unwrap()
            .unwrap();
        let third = tokio::time::timeout(Duration::from_secs(2), accepted_rx.recv())
            .await
            .unwrap()
            .unwrap();
        let first_delay = second.duration_since(first);
        let second_delay = third.duration_since(second);
        assert!(first_delay >= Duration::from_millis(60));
        assert!(second_delay >= Duration::from_millis(120));
        assert!(second_delay > first_delay + Duration::from_millis(30));
        drop(stream);
    }

    #[tokio::test]
    async fn reconnect_opens_a_gap_and_clears_the_old_epoch_cache() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let start = 10 * HOUR_MS;
        tokio::spawn(async move {
            serve_epoch(&listener, start, "100", false).await;
            serve_epoch(&listener, start + HOUR_MS, "200", true).await;
        });
        let options = StreamOptions {
            connect_timeout: Duration::from_secs(2),
            subscribe_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            ping_interval: Duration::from_secs(20),
            pong_timeout: Duration::from_secs(2),
            data_idle_timeout: Duration::from_secs(2),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start: Duration::from_millis(10),
            backoff_max: Duration::from_millis(20),
        };
        let mut stream = BybitPublicStream::with_url(
            format!("ws://127.0.0.1:{port}"),
            vec!["BTCUSDT".into()],
            options,
        )
        .unwrap();

        loop {
            match next(&mut stream).await {
                StreamEvent::EpochStarted { epoch: 1, .. } => break,
                StreamEvent::Fault(_) => {}
                other => panic!("data/control event preceded the first epoch: {other:?}"),
            }
        }
        assert!(matches!(
            next(&mut stream).await,
            StreamEvent::KlineClosed(_)
        ));
        assert!(stream.mark_gap_repaired(1));
        assert!(stream.sample_tickers(wall_ms().unwrap(), 30_000).is_some());
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::GapOpened { epoch: 1, .. }
            ) {
                break;
            }
        }
        assert!(stream.sample_tickers(wall_ms().unwrap(), 30_000).is_none());
        loop {
            if matches!(
                next(&mut stream).await,
                StreamEvent::EpochStarted {
                    epoch: 2,
                    reconnected: true,
                    ..
                }
            ) {
                break;
            }
        }
        assert!(matches!(
            next(&mut stream).await,
            StreamEvent::KlineClosed(_)
        ));
        assert!(stream.mark_gap_repaired(2));
        let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
        assert_eq!(sample.rows[0].mark_price, Some(Value::from("200")));
        assert!(stream.health().queued_frames <= stream.health().queue_capacity);
    }

    #[tokio::test]
    #[ignore = "needs network"]
    async fn live_public_stream_accepts_btc_topics_and_delivers_a_ticker() {
        let stream = BybitPublicStream::spawn(vec!["BTCUSDT".into()], 10_000, 250).unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            let health = stream.health();
            if health.connected
                && health.ticker_topics_accepted == 1
                && health.kline_topics_accepted == 1
            {
                if let Some(sample) = stream.sample_tickers(wall_ms().unwrap(), 30_000) {
                    if sample.rows.len() == 1 && sample.rows[0].mark_price.is_some() {
                        let complete = stream.health();
                        assert!(complete.ticker_coverage_complete);
                        assert_eq!(complete.ticker_topics_quarantined, 0);
                        assert_eq!(complete.kline_topics_quarantined, 0);
                        return;
                    }
                }
            }
            assert!(
                Instant::now() < deadline,
                "Bybit public worker stream did not become complete: {health:?}"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}
