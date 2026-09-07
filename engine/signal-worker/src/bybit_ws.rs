use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::time::Instant;

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
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

fn public_linear_url() -> &'static str {
    engine_public::VenueRealm::Demo.public_ws()
}

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
            public_linear_url(),
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
                _ = tokio::time::sleep_until(deadline) => {
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

#[cfg(test)]
mod tests;
