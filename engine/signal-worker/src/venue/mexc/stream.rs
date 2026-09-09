//! MEXC's public `edge` WebSocket, in the neutral stream vocabulary.
//!
//! Two subscriptions per symbol, one frame each: `sub.ticker` answers
//! `rs.sub.ticker` and pushes a COMPLETE ticker snapshot on `push.ticker`
//! (there are no deltas here, unlike Bybit's channel), and `sub.kline` answers
//! `rs.sub.kline` and pushes the bar that is open at the venue on
//! `push.kline`. A refused subscription answers `rs.error` naming the
//! contract; the successes are anonymous, so the accepted counts come from the
//! two reply channels and the quarantine from the named refusals.
//!
//! # Confirming a bar
//!
//! `push.kline` carries no `confirm` flag: the venue re-sends the open bar on
//! every trade. A bar is published once, when either
//!
//! - a frame for a LATER bar of the same symbol arrives, so the held bar is
//!   final; or
//! - the frame's own bar has closed by this process's clock, which is the last
//!   update of an hour that has already elapsed.
//!
//! Both paths keep the guard the Bybit stream puts on a `confirm: true` frame:
//! a bar whose hour has not elapsed here is never published, and each
//! `(symbol, bar)` is published at most once per epoch. A symbol that trades
//! rarely may have its bar confirmed hours late; the REST kline lane is what
//! fills that in, and the stream is an accelerator.
//!
//! # Keep-alive
//!
//! The venue wants an application-level `{"method":"ping"}`, not a protocol
//! ping frame, and closes a connection it has not heard from in a minute.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::net::TcpStream;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::{protocol::WebSocketConfig, Message};
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use super::{base_quantity, ContractTable};
use crate::http::wall_ms;
use crate::model::BybitTickerWire;
use crate::normalize::{normalize_kline_rows, normalize_ticker_strict, value_f64, value_i64};
use crate::venue::{
    BoxFuture, ConfirmedKline, PublicStream, StreamContinuity, StreamEvent, StreamHealth,
    TickerSample,
};
use crate::worker::WorkerError;
use crate::HOUR_MS;

const MAX_STREAM_EVENTS: usize = 1_024;
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;

/// An application frame, not a protocol ping: the venue asks for one every
/// 10-20 seconds and closes a socket it has not heard from in a minute.
const PING_PAYLOAD: &str = r#"{"method":"ping"}"#;

/// The venue's name for the hourly bar, the same one the REST reads use.
const KLINE_INTERVAL: &str = super::KLINE_INTERVAL;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// REST and the WebSocket are deliberately on different hosts here; both come
/// from the realm table in `engine-public`.
fn public_stream_url() -> &'static str {
    engine_public::MexcRealm::Mainnet.websocket()
}

pub struct MexcPublicStream {
    symbols: BTreeSet<String>,
    events: mpsc::Receiver<StreamEvent>,
    control: watch::Receiver<ControlState>,
    shared: Arc<Mutex<SharedState>>,
    worker: JoinHandle<()>,
    queue_capacity: usize,
}

impl MexcPublicStream {
    pub(crate) fn spawn(
        symbols: Vec<String>,
        contracts: Arc<ContractTable>,
        request_timeout_ms: u64,
        retry_base_ms: u64,
    ) -> Result<Self, WorkerError> {
        Self::spawn_continuing(
            symbols,
            contracts,
            request_timeout_ms,
            retry_base_ms,
            StreamContinuity::default(),
        )
    }

    /// The successor of a stream this process is replacing. It keeps the
    /// outgoing stream's epoch numbering, gap stamp and fault clocks: a symbol
    /// set changing is not the transport recovering.
    pub(crate) fn spawn_continuing(
        symbols: Vec<String>,
        contracts: Arc<ContractTable>,
        request_timeout_ms: u64,
        retry_base_ms: u64,
        continuity: StreamContinuity,
    ) -> Result<Self, WorkerError> {
        Self::with_url_continuing(
            public_stream_url(),
            symbols,
            contracts,
            StreamOptions::production(request_timeout_ms, retry_base_ms),
            continuity,
        )
    }

    #[cfg(test)]
    fn with_url(
        url: impl Into<String>,
        symbols: Vec<String>,
        contracts: Arc<ContractTable>,
        options: StreamOptions,
    ) -> Result<Self, WorkerError> {
        Self::with_url_continuing(
            url,
            symbols,
            contracts,
            options,
            StreamContinuity::default(),
        )
    }

    fn with_url_continuing(
        url: impl Into<String>,
        symbols: Vec<String>,
        contracts: Arc<ContractTable>,
        options: StreamOptions,
        continuity: StreamContinuity,
    ) -> Result<Self, WorkerError> {
        let symbols = normalize_symbols(symbols)?;
        let queue_capacity = stream_event_capacity(symbols.len());
        let (events_tx, events) = mpsc::channel(queue_capacity);
        let (control_tx, control) = watch::channel(ControlState::default());
        let shared = Arc::new(Mutex::new(SharedState::continuing(&symbols, continuity)));
        let worker = StreamWorker {
            url: url.into(),
            symbols: symbols.clone(),
            contracts,
            quarantined: BTreeSet::new(),
            open_bars: BTreeMap::new(),
            published_through: BTreeMap::new(),
            shared: Arc::clone(&shared),
            events: events_tx,
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
        let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
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
        let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
        if state.health.connected && state.health.epoch == epoch {
            state.health.gap_open = false;
            state.health.gap_open_since_ms = None;
            true
        } else {
            false
        }
    }

    pub fn mark_source_fault(&self, observed_ts_ms: i64) {
        let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
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
        let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
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
        let state = self.shared.lock().expect("MEXC stream state lock poisoned");
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

impl PublicStream for MexcPublicStream {
    fn next_event(&mut self) -> BoxFuture<'_, Option<StreamEvent>> {
        Box::pin(MexcPublicStream::next_event(self))
    }

    fn sample_tickers(&self, observed_ts_ms: i64, max_age_ms: i64) -> Option<TickerSample> {
        MexcPublicStream::sample_tickers(self, observed_ts_ms, max_age_ms)
    }

    fn mark_gap_repaired(&self, epoch: u64) -> bool {
        MexcPublicStream::mark_gap_repaired(self, epoch)
    }

    fn mark_source_fault(&self, observed_ts_ms: i64) {
        MexcPublicStream::mark_source_fault(self, observed_ts_ms);
    }

    fn reconcile_tickers(
        &self,
        epoch: u64,
        rows: &[BybitTickerWire],
        request_started_at_ms: i64,
        received_at_ms: i64,
    ) -> bool {
        MexcPublicStream::reconcile_tickers(
            self,
            epoch,
            rows,
            request_started_at_ms,
            received_at_ms,
        )
    }

    fn health(&self) -> StreamHealth {
        MexcPublicStream::health(self)
    }

    fn symbols(&self) -> &BTreeSet<String> {
        MexcPublicStream::symbols(self)
    }
}

impl Drop for MexcPublicStream {
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

    fn activate_epoch(&mut self, outcome: &SubscriptionOutcome) {
        self.health.connected = true;
        if self.health.epoch > 1 {
            self.health.reconnect_count = self.health.reconnect_count.saturating_add(1);
        }
        self.update_topic_counts(outcome);
    }

    /// A refused contract is refused on both channels: the venue's `rs.error`
    /// names the contract and not the channel.
    fn update_topic_counts(&mut self, outcome: &SubscriptionOutcome) {
        self.health.ticker_topics_accepted = outcome.accepted_ticker;
        self.health.kline_topics_accepted = outcome.accepted_kline;
        self.health.ticker_topics_quarantined = outcome.quarantined.len();
        self.health.kline_topics_quarantined = outcome.quarantined.len();
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

    /// Every `push.ticker` frame is a complete snapshot, so a frame replaces
    /// the held row rather than merging into it.
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
            // The venue asks for a ping every 10-20 seconds and drops a socket
            // it has not heard from in 60.
            ping_interval: Duration::from_secs(15),
            pong_timeout: Duration::from_secs(10),
            data_idle_timeout: Duration::from_secs(45),
            quarantine_reprobe_interval: Duration::from_secs(60),
            backoff_start,
            backoff_max: Duration::from_secs(8).max(backoff_start),
        }
    }
}

/// The bar a symbol has open at the venue, kept until a later bar or an
/// elapsed hour makes it final.
struct OpenBar {
    open_ts_ms: i64,
    row: Vec<Value>,
}

struct StreamWorker {
    url: String,
    symbols: BTreeSet<String>,
    contracts: Arc<ContractTable>,
    /// Engine symbols the venue refused. Re-probed on the same interval the
    /// Bybit stream re-probes a quarantined topic.
    quarantined: BTreeSet<String>,
    open_bars: BTreeMap<String, OpenBar>,
    /// The newest bar published per symbol, so no bar is published twice.
    published_through: BTreeMap<String, i64>,
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

#[derive(Default)]
struct SubscriptionOutcome {
    accepted_ticker: usize,
    accepted_kline: usize,
    /// Engine symbols the venue named in an `rs.error`, plus any this table
    /// does not list at all.
    quarantined: BTreeSet<String>,
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
                let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
                state.prepare_epoch(self.epoch, observed_ts_ms);
            }
            self.next_ping_at = Instant::now() + self.options.ping_interval;
            self.pong_deadline = None;
            self.last_data_at = Instant::now();
            self.quarantined.clear();
            self.open_bars.clear();
            self.published_through.clear();
            let requested = self.symbols.iter().cloned().collect::<Vec<_>>();
            let outcome = match self.subscribe(&mut socket, &requested).await {
                Ok(outcome) => {
                    self.quarantined = outcome.quarantined.clone();
                    self.next_quarantine_reprobe_at =
                        Instant::now() + self.options.quarantine_reprobe_interval;
                    {
                        let mut state =
                            self.shared.lock().expect("MEXC stream state lock poisoned");
                        state.activate_epoch(&outcome);
                    }
                    if !outcome.quarantined.is_empty() {
                        let samples = outcome
                            .quarantined
                            .iter()
                            .take(3)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ");
                        self.fault(format!(
                            "MEXC public stream quarantined {} refused contracts; {samples}",
                            outcome.quarantined.len()
                        ));
                    }
                    self.control.send_replace(ControlState {
                        epoch: self.epoch,
                        observed_ts_ms,
                        connected: true,
                        reconnected: self.epoch > 1,
                    });
                    if outcome.saw_market_data {
                        self.backoff = self.options.backoff_start;
                    }
                    self.read_socket(&mut socket).await
                }
                Err(error) => Err(error),
            };
            if let Err(error) = outcome {
                self.fault(error);
            }
            let gap_ts_ms = wall_ms().unwrap_or(observed_ts_ms);
            {
                let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
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
        .map_err(|_| "MEXC public WebSocket dial timed out".to_owned())?;
        connected
            .map(|(socket, _)| socket)
            .map_err(|error| format!("MEXC public WebSocket dial: {error}"))
    }

    /// One `sub.ticker` and one `sub.kline` per contract, then every reply
    /// counted. The venue answers each request exactly once, so a reply short
    /// of the requests is a failed epoch rather than a silent half
    /// subscription.
    async fn subscribe(
        &mut self,
        socket: &mut Socket,
        symbols: &[String],
    ) -> Result<SubscriptionOutcome, String> {
        let mut outcome = SubscriptionOutcome::default();
        let mut requested = Vec::new();
        for symbol in symbols {
            match self.contracts.venue_symbol(symbol) {
                Some(venue_symbol) => requested.push((symbol.clone(), venue_symbol.to_owned())),
                None => {
                    outcome.quarantined.insert(symbol.clone());
                }
            }
        }
        for (_, venue_symbol) in &requested {
            for frame in subscription_frames(venue_symbol) {
                tokio::time::timeout(
                    self.options.write_timeout,
                    socket.send(Message::text(frame)),
                )
                .await
                .map_err(|_| "MEXC public subscription write timed out".to_owned())?
                .map_err(|error| format!("MEXC public subscription write: {error}"))?;
            }
        }
        let expected = requested.len().saturating_mul(2);
        let mut replies = 0_usize;
        let mut faults = Vec::new();
        let mut confirmed = Vec::new();
        tokio::time::timeout(self.options.subscribe_timeout, async {
            while replies < expected {
                let message = socket
                    .next()
                    .await
                    .ok_or_else(|| "MEXC public socket closed before subscribe replies".to_owned())?
                    .map_err(|error| format!("MEXC public subscription read: {error}"))?;
                if let Message::Ping(payload) = &message {
                    tokio::time::timeout(
                        self.options.write_timeout,
                        socket.send(Message::Pong(payload.clone())),
                    )
                    .await
                    .map_err(|_| "MEXC public subscription pong timed out".to_owned())?
                    .map_err(|error| format!("MEXC public subscription pong: {error}"))?;
                    continue;
                }
                let received_at_ms = wall_ms().map_err(|error| error.to_string())?;
                match parse_socket_message(&message, &self.contracts, received_at_ms)? {
                    ParsedMessage::TickerAck { accepted } => {
                        replies += 1;
                        if accepted {
                            outcome.accepted_ticker += 1;
                        } else {
                            faults.push("MEXC refused a ticker subscription".to_owned());
                        }
                    }
                    ParsedMessage::KlineAck { accepted } => {
                        replies += 1;
                        if accepted {
                            outcome.accepted_kline += 1;
                        } else {
                            faults.push("MEXC refused a kline subscription".to_owned());
                        }
                    }
                    ParsedMessage::Refusal { venue_symbol, why } => {
                        replies += 1;
                        match venue_symbol
                            .as_deref()
                            .and_then(|name| self.contracts.engine_symbol(name))
                        {
                            Some(symbol) => {
                                outcome.quarantined.insert(symbol.to_owned());
                            }
                            None => faults.push(format!(
                                "MEXC refused a subscription it did not name: {why}"
                            )),
                        }
                    }
                    ParsedMessage::Ticker(row) => {
                        outcome.saw_market_data = true;
                        self.hold_ticker(*row, received_at_ms);
                    }
                    ParsedMessage::Kline(frame) => {
                        outcome.saw_market_data = true;
                        match self.hold_kline(*frame, received_at_ms) {
                            Ok(rows) => confirmed.extend(rows),
                            Err(error) => faults.push(error),
                        }
                    }
                    ParsedMessage::Pong | ParsedMessage::Ignore => {}
                }
            }
            Ok::<_, String>(())
        })
        .await
        .map_err(|_| {
            format!("MEXC public subscription replies timed out after {replies} of {expected}")
        })??;
        for row in confirmed {
            self.publish(row)?;
        }
        for fault in faults {
            self.fault(fault);
        }
        Ok(outcome)
    }

    async fn read_socket(&mut self, socket: &mut Socket) -> Result<(), String> {
        loop {
            let mut deadline = self
                .pong_deadline
                .unwrap_or(self.next_ping_at)
                .min(self.next_ping_at)
                .min(self.last_data_at + self.options.data_idle_timeout);
            if !self.quarantined.is_empty() {
                deadline = deadline.min(self.next_quarantine_reprobe_at);
            }
            tokio::select! {
                incoming = socket.next() => {
                    let message = incoming
                        .ok_or_else(|| "MEXC public socket closed".to_owned())?
                        .map_err(|error| format!("MEXC public socket read: {error}"))?;
                    match message {
                        Message::Close(_) => return Err("MEXC public socket received close".to_owned()),
                        Message::Ping(payload) => {
                            tokio::time::timeout(
                                self.options.write_timeout,
                                socket.send(Message::Pong(payload)),
                            )
                            .await
                                .map_err(|_| "MEXC public protocol pong timed out".to_owned())?
                                .map_err(|error| format!("MEXC public protocol pong: {error}"))?;
                        }
                        _ => {
                            let received_at_ms = wall_ms().map_err(|error| error.to_string())?;
                            match parse_socket_message(&message, &self.contracts, received_at_ms) {
                                Ok(parsed) => {
                                    if self.apply(parsed, received_at_ms)? {
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
                        return Err("MEXC public keep-alive was unanswered".to_owned());
                    }
                    if now >= self.last_data_at + self.options.data_idle_timeout {
                        return Err("MEXC public data stream became idle".to_owned());
                    }
                    if !self.quarantined.is_empty() && now >= self.next_quarantine_reprobe_at {
                        self.reprobe_quarantined(socket).await?;
                        continue;
                    }
                    if now >= self.next_ping_at {
                        tokio::time::timeout(
                            self.options.write_timeout,
                            socket.send(Message::text(PING_PAYLOAD)),
                        )
                        .await
                        .map_err(|_| "MEXC public ping write timed out".to_owned())?
                        .map_err(|error| format!("MEXC public ping write: {error}"))?;
                        self.next_ping_at = now + self.options.ping_interval;
                        if self.pong_deadline.is_none() {
                            self.pong_deadline = Some(now + self.options.pong_timeout);
                        }
                    }
                }
            }
        }
    }

    /// A contract listed while this socket was up is subscribable without a
    /// redial, and a name the venue never lists stays quarantined.
    async fn reprobe_quarantined(&mut self, socket: &mut Socket) -> Result<(), String> {
        let candidates = self.quarantined.iter().cloned().collect::<Vec<_>>();
        if candidates.is_empty() {
            return Ok(());
        }
        let accepted = {
            let state = self.shared.lock().expect("MEXC stream state lock poisoned");
            (
                state.health.ticker_topics_accepted,
                state.health.kline_topics_accepted,
            )
        };
        let mut outcome = self.subscribe(socket, &candidates).await?;
        outcome.accepted_ticker += accepted.0;
        outcome.accepted_kline += accepted.1;
        self.quarantined = outcome.quarantined.clone();
        {
            let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
            state.update_topic_counts(&outcome);
        }
        if outcome.saw_market_data {
            self.backoff = self.options.backoff_start;
        }
        self.next_quarantine_reprobe_at = Instant::now() + self.options.quarantine_reprobe_interval;
        Ok(())
    }

    fn apply(&mut self, parsed: ParsedMessage, received_at_ms: i64) -> Result<bool, String> {
        match parsed {
            ParsedMessage::Ticker(row) => {
                if !self.symbols.contains(&row.symbol) {
                    return Ok(false);
                }
                self.last_data_at = Instant::now();
                self.hold_ticker(*row, received_at_ms);
                Ok(true)
            }
            ParsedMessage::Kline(frame) => {
                if !self.symbols.contains(&frame.symbol) {
                    return Ok(false);
                }
                self.last_data_at = Instant::now();
                {
                    let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
                    state.saw_frame(received_at_ms);
                }
                // A malformed bar is a fault on that bar, not the end of the
                // epoch; a full confirmed-kline queue is the end of it,
                // because the gap after it needs REST repair.
                match self.hold_kline(*frame, received_at_ms) {
                    Ok(rows) => {
                        for row in rows {
                            self.publish(row)?;
                        }
                    }
                    Err(error) => self.fault(error),
                }
                Ok(true)
            }
            ParsedMessage::Pong => {
                self.pong_deadline = None;
                Ok(false)
            }
            ParsedMessage::Refusal { why, .. } => {
                self.fault(format!("MEXC public stream refusal: {why}"));
                Ok(false)
            }
            ParsedMessage::TickerAck { .. }
            | ParsedMessage::KlineAck { .. }
            | ParsedMessage::Ignore => Ok(false),
        }
    }

    fn hold_ticker(&mut self, row: BybitTickerWire, received_at_ms: i64) {
        if !self.symbols.contains(&row.symbol) {
            return;
        }
        let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
        state.tickers.apply(row, received_at_ms);
        state.saw_frame(received_at_ms);
    }

    /// The confirmation rule this module documents: a held bar is final once a
    /// later bar arrives, and a frame whose own hour has elapsed here is
    /// itself final. Each bar leaves once.
    fn hold_kline(
        &mut self,
        frame: KlineFrame,
        received_at_ms: i64,
    ) -> Result<Vec<ConfirmedKline>, String> {
        let published_through = self
            .published_through
            .get(&frame.symbol)
            .copied()
            .unwrap_or(0);
        if frame.open_ts_ms <= published_through {
            return Ok(Vec::new());
        }
        let mut out = Vec::new();
        if let Some(held) = self.open_bars.get(&frame.symbol) {
            if held.open_ts_ms < frame.open_ts_ms
                && held.open_ts_ms > published_through
                && received_at_ms >= held.open_ts_ms.saturating_add(HOUR_MS)
            {
                out.push(confirmed(
                    frame.symbol.clone(),
                    held.row.clone(),
                    received_at_ms,
                )?);
            }
        }
        if received_at_ms >= frame.open_ts_ms.saturating_add(HOUR_MS) {
            out.push(confirmed(frame.symbol.clone(), frame.row, received_at_ms)?);
            self.open_bars.remove(&frame.symbol);
        } else {
            self.open_bars.insert(
                frame.symbol.clone(),
                OpenBar {
                    open_ts_ms: frame.open_ts_ms,
                    row: frame.row,
                },
            );
        }
        for row in &out {
            let open_ts_ms = value_i64(&row.row[0], "MEXC kline open clock")
                .map_err(|error| error.to_string())?;
            self.published_through
                .insert(row.symbol.clone(), open_ts_ms);
        }
        Ok(out)
    }

    fn publish(&self, row: ConfirmedKline) -> Result<(), String> {
        self.events
            .try_send(StreamEvent::KlineClosed(row))
            .map_err(|error| {
                format!("MEXC confirmed-kline queue requires reconnect repair: {error}")
            })
    }

    fn bump_backoff(&mut self) {
        self.backoff = self.backoff.saturating_mul(2).min(self.options.backoff_max);
    }

    fn fault(&self, error: String) {
        let mut state = self.shared.lock().expect("MEXC stream state lock poisoned");
        state.health.fault_count = state.health.fault_count.saturating_add(1);
        drop(state);
        let _ = self.events.try_send(StreamEvent::Fault(error));
    }
}

fn confirmed(
    symbol: String,
    row: Vec<Value>,
    available_at_ms: i64,
) -> Result<ConfirmedKline, String> {
    normalize_kline_rows(&symbol, available_at_ms, std::slice::from_ref(&row))
        .map_err(|error| error.to_string())?;
    Ok(ConfirmedKline {
        symbol,
        available_at_ms,
        row,
    })
}

#[derive(Clone, Debug, PartialEq)]
struct KlineFrame {
    symbol: String,
    open_ts_ms: i64,
    row: Vec<Value>,
}

#[derive(Clone, Debug, PartialEq)]
enum ParsedMessage {
    TickerAck {
        accepted: bool,
    },
    KlineAck {
        accepted: bool,
    },
    Refusal {
        venue_symbol: Option<String>,
        why: String,
    },
    Pong,
    Ticker(Box<BybitTickerWire>),
    Kline(Box<KlineFrame>),
    Ignore,
}

fn parse_socket_message(
    message: &Message,
    contracts: &ContractTable,
    received_at_ms: i64,
) -> Result<ParsedMessage, String> {
    match message {
        Message::Text(text) => parse_frame(text.as_str(), contracts, received_at_ms),
        Message::Binary(bytes) => {
            let text = std::str::from_utf8(bytes)
                .map_err(|error| format!("MEXC public binary frame is not UTF-8: {error}"))?;
            parse_frame(text, contracts, received_at_ms)
        }
        _ => Ok(ParsedMessage::Ignore),
    }
}

fn parse_frame(
    text: &str,
    contracts: &ContractTable,
    received_at_ms: i64,
) -> Result<ParsedMessage, String> {
    let value: Value = serde_json::from_str(text)
        .map_err(|error| format!("MEXC public frame is invalid JSON: {error}"))?;
    let Some(channel) = value.get("channel").and_then(Value::as_str) else {
        return Ok(ParsedMessage::Ignore);
    };
    let accepted = || value.get("data").and_then(Value::as_str) == Some("success");
    match channel {
        "pong" => return Ok(ParsedMessage::Pong),
        "rs.sub.ticker" => {
            return Ok(ParsedMessage::TickerAck {
                accepted: accepted(),
            })
        }
        "rs.sub.kline" => {
            return Ok(ParsedMessage::KlineAck {
                accepted: accepted(),
            })
        }
        "rs.error" => {
            let why = value
                .get("data")
                .and_then(Value::as_str)
                .unwrap_or("no reason")
                .to_owned();
            return Ok(ParsedMessage::Refusal {
                venue_symbol: bracketed(&why),
                why,
            });
        }
        "push.ticker" | "push.kline" => {}
        _ => return Ok(ParsedMessage::Ignore),
    }
    let data = value
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("MEXC {channel} frame lacks object data"))?;
    let data = Value::Object(data.clone());
    let venue_symbol = data
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| format!("MEXC {channel} frame lacks a symbol"))?;
    if value
        .get("symbol")
        .and_then(Value::as_str)
        .is_some_and(|outer| outer != venue_symbol)
    {
        return Err(format!("MEXC {channel} frame symbols disagree"));
    }
    let Some(symbol) = contracts.engine_symbol(venue_symbol).map(str::to_owned) else {
        return Ok(ParsedMessage::Ignore);
    };
    let contract_size = contracts
        .row(&symbol)
        .ok_or_else(|| format!("MEXC {channel} frame names a contract with no size"))?
        .contract_size;
    if channel == "push.ticker" {
        let row = super::ticker_wire(contracts, &data, received_at_ms)
            .ok_or_else(|| "MEXC push.ticker frame is not a readable row".to_owned())?;
        normalize_ticker_strict(received_at_ms, received_at_ms, &row)
            .map_err(|error| error.to_string())?;
        return Ok(ParsedMessage::Ticker(Box::new(row)));
    }
    if data.get("interval").and_then(Value::as_str) != Some(KLINE_INTERVAL) {
        return Ok(ParsedMessage::Ignore);
    }
    let seconds = value_i64(
        data.get("t")
            .ok_or_else(|| "MEXC kline t is absent".to_owned())?,
        "MEXC kline t",
    )
    .map_err(|error| error.to_string())?;
    let open_ts_ms = seconds
        .checked_mul(1_000)
        .ok_or_else(|| "MEXC kline t overflowed".to_owned())?;
    if open_ts_ms <= 0 || open_ts_ms % HOUR_MS != 0 {
        return Err("MEXC kline t is not an hour boundary".to_owned());
    }
    let contracts_traded = data
        .get("q")
        .ok_or_else(|| "MEXC kline q is absent".to_owned())?;
    let volume_base = base_quantity(Some(contracts_traded), contract_size)
        .ok_or_else(|| "MEXC kline q is not a quantity".to_owned())?;
    Ok(ParsedMessage::Kline(Box::new(KlineFrame {
        symbol,
        open_ts_ms,
        row: vec![
            Value::from(open_ts_ms),
            required(&data, "ro", "MEXC kline ro")?,
            required(&data, "rh", "MEXC kline rh")?,
            required(&data, "rl", "MEXC kline rl")?,
            required(&data, "rc", "MEXC kline rc")?,
            volume_base,
            required(&data, "a", "MEXC kline a")?,
        ],
    })))
}

/// The contract named in an `rs.error`: `Contract [NOPE_USDT] not exists`.
fn bracketed(text: &str) -> Option<String> {
    let start = text.find('[')?;
    let end = text[start + 1..].find(']')?;
    let name = text[start + 1..start + 1 + end].trim();
    (!name.is_empty()).then(|| name.to_owned())
}

fn required(data: &Value, key: &str, label: &str) -> Result<Value, String> {
    let value = data.get(key).ok_or_else(|| format!("{label} is absent"))?;
    value_f64(value, label).map_err(|error| error.to_string())?;
    Ok(value.clone())
}

fn subscription_frames(venue_symbol: &str) -> [String; 2] {
    [
        serde_json::json!({"method": "sub.ticker", "param": {"symbol": venue_symbol}}).to_string(),
        serde_json::json!({
            "method": "sub.kline",
            "param": {"symbol": venue_symbol, "interval": KLINE_INTERVAL}
        })
        .to_string(),
    ]
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
            return Err(WorkerError::config("MEXC stream symbol is invalid"));
        }
        out.insert(symbol);
    }
    if out.is_empty() {
        return Err(WorkerError::config("MEXC stream has no symbols"));
    }
    Ok(out)
}

fn stream_event_capacity(symbols: usize) -> usize {
    symbols.saturating_mul(2).clamp(64, MAX_STREAM_EVENTS)
}

#[cfg(test)]
mod tests;
