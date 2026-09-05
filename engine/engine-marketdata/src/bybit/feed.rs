//! The socket: connect, subscribe, keep alive, refresh silent topics, and
//! reconnect. This file owns wire-level request correlation and liveness;
//! [`super::parse`] and [`super::state`] own market payloads and book state.
//!
//! The socket lives in its own task, not inside `next_event`. The engine core
//! waits on `next_event` in a `select!` and throws that future away every time
//! another branch wins — its flush tick alone fires every 250ms — so anything
//! half-finished in there would be started over from nothing: a dial, a
//! backoff sleep, a whole reconnect. Once backoff passes the tick spacing the
//! feed can never finish reconnecting, and the engine trades on a frozen
//! picture with no error to show for it.
//!
//! So the task owns the socket and nobody cancels it, which makes its awaits
//! safe. It posts finished events down a channel, and `next_event` is only a
//! channel receive — a receive that is dropped part-way loses nothing.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::{Arc, Mutex, Once};
use std::time::{Duration, Instant};

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Subscription, SymbolId, SymbolTable};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use tokio::net::TcpStream;
use tokio::sync::{mpsc, Notify};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::{protocol::WebSocketConfig, Message};
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};
use tracing::{debug, info, warn};

use crate::bybit::parse::{parse_frame, parse_frame_bytes, ParsedFrame};
use crate::bybit::state::{Applied, FeedState, ResyncReason};

/// Public market data for USDT/USDC perpetuals. No credentials: the demo
/// account trades against these same prices.
///
/// Read from the venue crate's realm table rather than written here. Every
/// venue host this engine knows lives in exactly one file per venue, and
/// `engine-venue`'s own fence reads those files back to prove it — a host
/// spelled out in this crate would be one the fence never sees.
pub fn bybit_public_linear_url() -> &'static str {
    engine_public::VenueRealm::Demo.public_ws()
}

const PING_INTERVAL: Duration = Duration::from_secs(20);
const PONG_TIMEOUT: Duration = Duration::from_secs(10);
const MARKET_IDLE_TIMEOUT: Duration = Duration::from_secs(45);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const SUBSCRIBE_REPLY_TIMEOUT: Duration = Duration::from_secs(10);
const SOCKET_WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
/// Bybit caps the size of one request frame, not the number of topics.
const TOPICS_PER_MESSAGE: usize = 100;
const MAX_PENDING_EPOCHS: usize = 64;
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
const MAX_SUBSCRIPTION_PENDING_MESSAGES: usize = 4_096;
const MAX_SUBSCRIPTION_PENDING_BYTES: usize = 8 * 1024 * 1024;
const MAX_QUARANTINE_REPROBES_PER_WINDOW: usize = 8;
const MAX_STALE_QUOTE_REFRESHES_PER_SWEEP: usize = 8;
const QUARANTINE_REPROBE_EPOCHS: u64 = 8;
const QUARANTINE_REPROBE_INTERVAL: Duration = Duration::from_secs(60);
const TOPIC_MAINTENANCE_INTERVAL: Duration = Duration::from_secs(1);

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Copy, Clone)]
struct FeedTiming {
    ping_interval: Duration,
    pong_timeout: Duration,
    market_idle_timeout: Duration,
    quarantine_reprobe_interval: Duration,
    topic_maintenance_interval: Duration,
}

impl Default for FeedTiming {
    fn default() -> Self {
        Self {
            ping_interval: PING_INTERVAL,
            pong_timeout: PONG_TIMEOUT,
            market_idle_timeout: MARKET_IDLE_TIMEOUT,
            quarantine_reprobe_interval: QUARANTINE_REPROBE_INTERVAL,
            topic_maintenance_interval: TOPIC_MAINTENANCE_INTERVAL,
        }
    }
}

/// Monotonic nanoseconds from the engine's shared clock origin (in
/// engine-types), so feed stamps are comparable to every other crate's.
#[derive(Copy, Clone, Debug)]
pub struct MonoClock;

impl MonoClock {
    pub fn new() -> Self {
        MonoClock
    }

    pub fn now_ns(&self) -> u64 {
        engine_types::clock::mono_ns()
    }
}

impl Default for MonoClock {
    fn default() -> Self {
        MonoClock::new()
    }
}

/// Bybit's public v5 linear websocket, as a [`MarketFeed`]. A thin front end:
/// it holds the settings and the queue the socket worker fills.
pub struct BybitPublicFeed {
    url: String,
    topics: Vec<String>,
    subs: Vec<Subscription>,
    /// Where late subscriptions go once the worker is running. `None` before
    /// the first `next_event`, when appending to `subs` is enough on its own.
    admissions: Option<mpsc::UnboundedSender<Vec<Subscription>>>,
    /// Shared only so the synchronous front end can coalesce repeated
    /// admissions without guessing whether the worker quarantined a topic.
    topic_status: Arc<Mutex<TopicStatus>>,
    /// The same interning the worker builds, so a `SymbolId` handed out here
    /// means what the worker's events mean.
    table: SymbolTable,
    clock: MonoClock,
    timing: FeedTiming,
    inbox: Option<Inbox>,
    pending_reset: bool,
}

/// The running worker: where its events land, and the handle that stops it.
struct Inbox {
    events: Arc<Handoff>,
    worker: JoinHandle<()>,
}

struct Handoff {
    state: Mutex<HandoffState>,
    ready: Notify,
}

#[derive(Default)]
struct HandoffState {
    items: VecDeque<Result<MarketEvent, FeedError>>,
    closed: bool,
}

#[derive(Default)]
struct TopicStatus {
    /// Quarantined topic and earliest time a fresh admission may re-probe it.
    quarantined: BTreeMap<String, Instant>,
}

impl Handoff {
    fn new() -> Self {
        Self {
            state: Mutex::new(HandoffState::default()),
            ready: Notify::new(),
        }
    }

    fn push(&self, item: Result<MarketEvent, FeedError>) -> bool {
        let mut state = self.state.lock().expect("market handoff lock is poisoned");
        if state.closed {
            return false;
        }
        if let Ok(MarketEvent::Trades {
            symbol,
            trades: incoming,
        }) = &item
        {
            for queued in state.items.iter_mut().rev() {
                let Some(queued_key) = market_key(queued) else {
                    break;
                };
                if queued_key == (symbol.0, 2) {
                    if let Ok(MarketEvent::Trades { trades, .. }) = queued {
                        trades.buy_qty += incoming.buy_qty;
                        trades.sell_qty += incoming.sell_qty;
                        trades.trade_count =
                            trades.trade_count.saturating_add(incoming.trade_count);
                        if incoming.seq >= trades.seq {
                            trades.last_px = incoming.last_px;
                            trades.seq = incoming.seq;
                            trades.venue_ts_ms = incoming.venue_ts_ms;
                            trades.recv_ns = incoming.recv_ns;
                        }
                    }
                    return true;
                }
            }
        }
        if let Some(key) = market_key(&item) {
            for queued in state.items.iter_mut().rev() {
                let Some(queued_key) = market_key(queued) else {
                    break;
                };
                if queued_key == key {
                    *queued = item;
                    return true;
                }
            }
        } else {
            let controls = state
                .items
                .iter()
                .filter(|item| market_key(item).is_none())
                .count();
            if controls >= MAX_PENDING_EPOCHS {
                state.items.clear();
            }
        }
        state.items.push_back(item);
        drop(state);
        self.ready.notify_one();
        true
    }

    fn close(&self) {
        let mut state = self.state.lock().expect("market handoff lock is poisoned");
        state.closed = true;
        drop(state);
        self.ready.notify_waiters();
    }

    async fn recv(&self) -> Option<Result<MarketEvent, FeedError>> {
        loop {
            let notified = self.ready.notified();
            {
                let mut state = self.state.lock().expect("market handoff lock is poisoned");
                if let Some(item) = state.items.pop_front() {
                    return Some(item);
                }
                if state.closed {
                    return None;
                }
            }
            notified.await;
        }
    }
}

fn market_key(item: &Result<MarketEvent, FeedError>) -> Option<(u16, u8)> {
    match item {
        Ok(MarketEvent::Quote { symbol, .. }) => Some((symbol.0, 0)),
        Ok(MarketEvent::Depth { symbol, .. }) => Some((symbol.0, 1)),
        Ok(MarketEvent::Trades { symbol, .. }) => Some((symbol.0, 2)),
        Ok(MarketEvent::Ticker { symbol, .. }) => Some((symbol.0, 3)),
        Ok(MarketEvent::FeedReset { .. }) | Err(_) => None,
    }
}

// Carries the fixed L50 value without allocating on every socket frame.
#[allow(clippy::large_enum_variant)]
enum Step {
    Event(MarketEvent),
    Idle,
    Reconnect,
}

enum SubscriptionDisposition {
    Accepted(Vec<(u64, Message)>),
    Refused(SubscriptionRefusal),
}

struct SubscriptionAck {
    request_id: String,
    success: bool,
    code: Option<i64>,
    ret_msg: String,
}

struct SubscriptionRefusal {
    code: Option<i64>,
    message: String,
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
enum SubscriptionRefusalKind {
    Topic,
    Transient,
    Global,
}

impl BybitPublicFeed {
    /// Build the feed against the public linear stream. Nothing is dialled
    /// until the first `next_event`.
    pub fn new(subs: &[Subscription]) -> Self {
        Self::with_url(bybit_public_linear_url(), subs)
    }

    pub fn with_url(url: impl Into<String>, subs: &[Subscription]) -> Self {
        // One spelling of a symbol, used for both the topic and the interning,
        // so a frame's topic always resolves to the id we handed out.
        let subs: Vec<Subscription> = subs
            .iter()
            .map(|s| Subscription {
                symbol: s.symbol.to_uppercase(),
                feed: s.feed,
            })
            .collect();
        let mut topics: Vec<String> = subs.iter().map(topic_for).collect();
        topics.sort();
        topics.dedup();
        BybitPublicFeed {
            url: url.into(),
            topics,
            table: FeedState::new(&subs).into_table(),
            subs,
            admissions: None,
            topic_status: Arc::new(Mutex::new(TopicStatus::default())),
            clock: MonoClock::new(),
            timing: FeedTiming::default(),
            inbox: None,
            pending_reset: false,
        }
    }

    #[cfg(test)]
    fn with_timing(
        mut self,
        ping_interval: Duration,
        pong_timeout: Duration,
        market_idle_timeout: Duration,
    ) -> Self {
        self.timing = FeedTiming {
            ping_interval,
            pong_timeout,
            market_idle_timeout,
            ..self.timing
        };
        self
    }

    #[cfg(test)]
    fn with_topic_timing(
        mut self,
        quarantine_reprobe_interval: Duration,
        topic_maintenance_interval: Duration,
    ) -> Self {
        self.timing.quarantine_reprobe_interval = quarantine_reprobe_interval;
        self.timing.topic_maintenance_interval = topic_maintenance_interval;
        self
    }

    /// The feed's symbol interning. The engine core seeds its `MarketState`
    /// from this so a `SymbolId` means the same thing on both sides.
    pub fn symbols(&self) -> &SymbolTable {
        &self.table
    }

    /// The topics this feed subscribes on every connect.
    pub fn topics(&self) -> &[String] {
        &self.topics
    }

    /// Start following a symbol the feed was not built with.
    ///
    /// Returns the `SymbolId` this feed will use for it. That id is the same
    /// one the worker's state will assign, because both intern in the same
    /// order — the boot subscriptions, then admissions as they arrive. Nothing
    /// else in the engine may intern out of band, or a `SymbolId` would mean
    /// two different symbols in two places, and orders would go to the wrong
    /// one.
    ///
    /// A symbol already known keeps its id. Re-admitting an active topic is a
    /// worker no-op; re-admitting a quarantined topic explicitly re-probes it.
    /// Before the worker is running this only records the subscription; the
    /// first dial carries it like any other.
    pub fn admit(&mut self, symbol: &str, feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let sub = Subscription {
            symbol: symbol.clone(),
            feed,
        };
        let topic = topic_for(&sub);
        let known = self.topics.contains(&topic);
        if !known {
            self.topics.push(topic.clone());
            self.subs.push(sub.clone());
        }
        let should_send = !known || {
            let mut status = self
                .topic_status
                .lock()
                .expect("market topic status lock is poisoned");
            let now = Instant::now();
            match status.quarantined.get_mut(&topic) {
                Some(next_reprobe_at) if now >= *next_reprobe_at => {
                    *next_reprobe_at = now + self.timing.quarantine_reprobe_interval;
                    true
                }
                Some(_) | None => false,
            }
        };
        if should_send {
            if let Some(tx) = &self.admissions {
                let _ = tx.send(vec![sub]);
            }
        }
        self.table.intern(&symbol)
    }

    /// Start the socket worker. Called on the first `next_event`, so nothing
    /// is dialled until somebody asks for a price.
    fn start(&mut self) {
        let events = Arc::new(Handoff::new());
        let (admit_tx, admit_rx) = mpsc::unbounded_channel();
        self.admissions = Some(admit_tx);
        let mut state = FeedState::default();
        for index in 0..self.table.len() {
            state.intern(self.table.name(SymbolId(index as u16)));
        }
        let worker = FeedWorker {
            url: self.url.clone(),
            topics: self.topics.clone(),
            quarantined_topics: BTreeMap::new(),
            quarantine_reprobe_at: BTreeMap::new(),
            manual_reprobe_window_started: Instant::now(),
            manual_reprobes_in_window: 0,
            active_topics: BTreeSet::new(),
            topic_status: self.topic_status.clone(),
            state,
            clock: self.clock,
            events: events.clone(),
            backoff: BACKOFF_START,
            epochs: 0,
            next_ping_at: Instant::now() + self.timing.ping_interval,
            pong_deadline: None,
            market_idle_deadline: None,
            active_quote_last_event_at: BTreeMap::new(),
            next_topic_maintenance_at: Instant::now() + self.timing.topic_maintenance_interval,
            maintaining_topics: false,
            timing: self.timing,
            subscription_request_sequence: 0,
            admissions: admit_rx,
        };
        // The engine runs one thread, so this stays on it.
        let worker = tokio::spawn(worker.run());
        self.inbox = Some(Inbox { events, worker });
    }
}

impl Drop for BybitPublicFeed {
    /// Nobody else holds the socket. Stop the worker with the feed, or the
    /// task would sit on a connection nothing is listening to.
    fn drop(&mut self) {
        if let Some(inbox) = &self.inbox {
            inbox.worker.abort();
        }
    }
}

/// Owns the socket for as long as the feed lives. Nothing cancels it, so it
/// can dial, sleep out a backoff and reconnect without losing its place.
struct FeedWorker {
    url: String,
    topics: Vec<String>,
    /// Topic and first socket epoch on which a bounded re-probe is allowed.
    quarantined_topics: BTreeMap<String, u64>,
    quarantine_reprobe_at: BTreeMap<String, Instant>,
    manual_reprobe_window_started: Instant,
    manual_reprobes_in_window: usize,
    /// Exact topics acknowledged on the current socket. Frames from everything
    /// else are ignored, even if the symbol was interned in an older epoch.
    active_topics: BTreeSet<String>,
    topic_status: Arc<Mutex<TopicStatus>>,
    state: FeedState,
    clock: MonoClock,
    events: Arc<Handoff>,
    backoff: Duration,
    /// How many sockets this worker has opened. Past the first, a new one is a
    /// reconnect and owes the strategies a `FeedReset`.
    epochs: u64,
    next_ping_at: Instant,
    pong_deadline: Option<Instant>,
    market_idle_deadline: Option<Instant>,
    active_quote_last_event_at: BTreeMap<String, Instant>,
    next_topic_maintenance_at: Instant,
    maintaining_topics: bool,
    timing: FeedTiming,
    subscription_request_sequence: u64,
    /// Symbols admitted after the socket was already up.
    admissions: mpsc::UnboundedReceiver<Vec<Subscription>>,
}

impl FeedWorker {
    async fn run(mut self) {
        loop {
            let mut socket = match self.connect().await {
                Ok(connected) => connected,
                Err(error) => {
                    let _ = self.emit(Err(error));
                    return;
                }
            };
            loop {
                match self.step(&mut socket).await {
                    Ok(Step::Event(event)) => {
                        if !self.emit(Ok(event)) {
                            return;
                        }
                    }
                    Ok(Step::Idle) => {}
                    Ok(Step::Reconnect) => {
                        self.bump_backoff();
                        break;
                    }
                    // Nothing a fresh socket can fix. Say so and stop.
                    Err(e) => {
                        let _ = self.emit(Err(e));
                        return;
                    }
                }
            }
        }
    }

    /// False once nobody is listening, which is the worker's cue to stop.
    fn emit(&self, item: Result<MarketEvent, FeedError>) -> bool {
        self.events.push(item)
    }

    /// Dial, subscribe, and start a fresh epoch. Retries with capped backoff
    /// until it succeeds; a market feed that gives up is worse than a slow one.
    async fn connect(&mut self) -> Result<Socket, FeedError> {
        loop {
            // The first dial is immediate. A failed first dial still has to
            // earn the same backoff as every reconnect; `epochs` remains zero
            // until a socket succeeds, so checking it alone turns an outage
            // into a tight connection storm.
            if self.epochs > 0 || self.backoff > BACKOFF_START {
                tokio::time::sleep(self.backoff).await;
            }
            let mut socket = match self.dial_socket().await {
                Ok(socket) => socket,
                Err(error) => {
                    warn!(url = %self.url, backoff = ?self.backoff, "market feed dial failed: {error}");
                    self.bump_backoff();
                    continue;
                }
            };

            let reconnected = self.epochs > 0;
            self.epochs = self.epochs.saturating_add(1);
            self.subscription_request_sequence = 0;
            self.active_topics.clear();
            self.active_quote_last_event_at.clear();
            // The new socket knows nothing of the old book. Merging across the
            // seam would invent prices. Announce that break before subscription
            // setup can emit an accepted topic's first frame.
            self.state.reset();
            let now = Instant::now();
            self.next_ping_at = now + self.timing.ping_interval;
            self.pong_deadline = None;
            self.market_idle_deadline =
                (!self.topics.is_empty()).then_some(now + self.timing.market_idle_timeout);
            self.next_topic_maintenance_at = now + self.timing.topic_maintenance_interval;
            if reconnected {
                let recv_ns = self.clock.now_ns();
                if !self.emit(Ok(MarketEvent::FeedReset { recv_ns })) {
                    return Err(FeedError::Closed);
                }
            }

            let candidates = self.subscription_candidates();
            let retry_backoff = self.backoff;
            match self.subscribe_topics(&mut socket, &candidates).await {
                Ok(()) => {
                    info!(
                        url = %self.url,
                        topics = self.active_topics.len(),
                        quarantined = self.quarantined_topics.len(),
                        epoch = self.epochs,
                        "market feed connected"
                    );
                    return Ok(socket);
                }
                Err(FeedError::Closed) => return Err(FeedError::Closed),
                Err(error @ FeedError::BadMessage(_)) => return Err(error),
                Err(error) => {
                    // A partial setup can carry ACKs and market frames before
                    // a later group fails. Those do not make the connection
                    // attempt healthy enough to reset its retry delay.
                    self.backoff = retry_backoff;
                    warn!(url = %self.url, backoff = ?self.backoff, "market feed subscription failed: {error}");
                    self.bump_backoff();
                }
            }
        }
    }

    async fn dial_socket(&self) -> Result<Socket, FeedError> {
        install_crypto_provider();
        let connected = tokio::time::timeout(
            CONNECT_TIMEOUT,
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
        .map_err(|_| FeedError::Transport("market feed dial timed out".to_string()))?;
        connected
            .map(|(socket, _)| socket)
            .map_err(|e| FeedError::Transport(e.to_string()))
    }

    fn subscription_candidates(&mut self) -> Vec<String> {
        let mut candidates = self
            .topics
            .iter()
            .filter(|topic| !self.quarantined_topics.contains_key(*topic))
            .cloned()
            .collect::<Vec<_>>();
        let reprobes = self
            .quarantined_topics
            .iter()
            .filter(|(_, retry_epoch)| **retry_epoch <= self.epochs)
            .take(MAX_QUARANTINE_REPROBES_PER_WINDOW)
            .map(|(topic, _)| topic.clone())
            .collect::<Vec<_>>();
        let next_retry_epoch = self.epochs.saturating_add(QUARANTINE_REPROBE_EPOCHS);
        let next_manual_reprobe_at = Instant::now() + self.timing.quarantine_reprobe_interval;
        for topic in &reprobes {
            if let Some(retry_epoch) = self.quarantined_topics.get_mut(topic) {
                *retry_epoch = next_retry_epoch;
            }
            self.quarantine_reprobe_at
                .insert(topic.clone(), next_manual_reprobe_at);
        }
        let mut status = self
            .topic_status
            .lock()
            .expect("market topic status lock is poisoned");
        for topic in &reprobes {
            status
                .quarantined
                .insert(topic.clone(), next_manual_reprobe_at);
        }
        candidates.extend(reprobes);
        candidates
    }

    fn quarantine(&mut self, topic: &str, reason: &str) {
        let retry_epoch = self.epochs.saturating_add(QUARANTINE_REPROBE_EPOCHS);
        let retry_at = Instant::now() + self.timing.quarantine_reprobe_interval;
        let first = self
            .quarantined_topics
            .insert(topic.to_string(), retry_epoch)
            .is_none();
        self.quarantine_reprobe_at
            .insert(topic.to_string(), retry_at);
        self.active_topics.remove(topic);
        self.active_quote_last_event_at.remove(topic);
        self.topic_status
            .lock()
            .expect("market topic status lock is poisoned")
            .quarantined
            .entry(topic.to_string())
            .or_insert_with(Instant::now);
        if first {
            warn!(%topic, %reason, retry_epoch, "market topic quarantined; other topics remain live");
        }
    }

    fn activate(&mut self, topics: &[String]) {
        let mut status = self
            .topic_status
            .lock()
            .expect("market topic status lock is poisoned");
        let now = Instant::now();
        for topic in topics {
            self.quarantined_topics.remove(topic);
            self.quarantine_reprobe_at.remove(topic);
            self.active_topics.insert(topic.clone());
            if is_quote_topic(topic) {
                self.active_quote_last_event_at.insert(topic.clone(), now);
            }
            status.quarantined.remove(topic);
        }
    }

    async fn subscribe_topics(
        &mut self,
        socket: &mut Socket,
        topics: &[String],
    ) -> Result<(), FeedError> {
        let mut work: VecDeque<Vec<String>> = topics
            .chunks(TOPICS_PER_MESSAGE)
            .map(|chunk| chunk.to_vec())
            .collect();
        while let Some(group) = work.pop_front() {
            let request_id = self.next_subscription_request_id()?;
            match self.subscribe_group(socket, &group, &request_id).await? {
                SubscriptionDisposition::Accepted(pending) => {
                    self.activate(&group);
                    for (recv_ns, message) in pending {
                        self.process_active_message(message, recv_ns)?;
                    }
                }
                SubscriptionDisposition::Refused(refusal) => {
                    match subscription_refusal_kind(refusal.code, &refusal.message) {
                        SubscriptionRefusalKind::Topic => {
                            if group.len() == 1 {
                                self.quarantine(&group[0], &refusal.message);
                            } else {
                                let right = group[group.len() / 2..].to_vec();
                                let left = group[..group.len() / 2].to_vec();
                                work.push_front(right);
                                work.push_front(left);
                            }
                        }
                        SubscriptionRefusalKind::Transient => {
                            return Err(FeedError::Transport(format!(
                                "subscribe refused{}: {}",
                                refusal
                                    .code
                                    .map(|code| format!(" with code {code}"))
                                    .unwrap_or_default(),
                                refusal.message
                            )));
                        }
                        SubscriptionRefusalKind::Global => {
                            return Err(FeedError::BadMessage(format!(
                                "Bybit rejected the whole subscription request with code 10404: {}",
                                refusal.message
                            )));
                        }
                    }
                }
            }
        }
        Ok(())
    }

    fn next_subscription_request_id(&mut self) -> Result<String, FeedError> {
        self.subscription_request_sequence = self
            .subscription_request_sequence
            .checked_add(1)
            .ok_or_else(|| FeedError::Transport("subscription request id exhausted".to_string()))?;
        Ok(self.subscription_request_sequence.to_string())
    }

    async fn subscribe_group(
        &mut self,
        socket: &mut Socket,
        topics: &[String],
        request_id: &str,
    ) -> Result<SubscriptionDisposition, FeedError> {
        if self.market_idle_deadline.is_none() {
            self.market_idle_deadline = Some(Instant::now() + self.timing.market_idle_timeout);
        }
        let payload = subscribe_payload(topics, request_id);
        tokio::time::timeout(SOCKET_WRITE_TIMEOUT, socket.send(Message::text(payload)))
            .await
            .map_err(|_| FeedError::Transport("market subscription send timed out".to_string()))?
            .map_err(|error| FeedError::Transport(error.to_string()))?;

        let requested = topics.iter().map(String::as_str).collect::<BTreeSet<_>>();
        let mut pending = Vec::new();
        let mut pending_bytes = 0_usize;
        let reply_deadline = Instant::now() + SUBSCRIBE_REPLY_TIMEOUT;
        loop {
            let (recv_ns, message) = self
                .next_subscription_message(socket, reply_deadline)
                .await?;
            if let Message::Ping(payload) = &message {
                tokio::time::timeout(
                    SOCKET_WRITE_TIMEOUT,
                    socket.send(Message::Pong(payload.clone())),
                )
                .await
                .map_err(|_| FeedError::Transport("market pong send timed out".to_string()))?
                .map_err(|error| FeedError::Transport(error.to_string()))?;
                continue;
            }
            if let Some(ack) = subscription_ack(&message) {
                if ack.request_id != request_id {
                    debug!(
                        expected_request_id = request_id,
                        received_request_id = %ack.request_id,
                        "ignoring unmatched market subscription reply"
                    );
                    continue;
                }
                return if ack.success {
                    Ok(SubscriptionDisposition::Accepted(pending))
                } else {
                    Ok(SubscriptionDisposition::Refused(SubscriptionRefusal {
                        code: ack.code,
                        message: ack.ret_msg,
                    }))
                };
            }

            let topic = message_topic(&message).map(str::to_owned);
            if topic
                .as_deref()
                .is_some_and(|topic| self.active_topics.contains(topic))
            {
                self.process_active_message(message, recv_ns)?;
                continue;
            }
            if topic
                .as_deref()
                .is_some_and(|topic| requested.contains(topic))
            {
                let message_bytes = message_payload_len(&message);
                if pending.len() >= MAX_SUBSCRIPTION_PENDING_MESSAGES
                    || message_bytes > MAX_SUBSCRIPTION_PENDING_BYTES.saturating_sub(pending_bytes)
                {
                    return Err(FeedError::Transport(
                        "market subscription staging capacity exceeded".to_string(),
                    ));
                }
                pending_bytes += message_bytes;
                pending.push((recv_ns, message));
                continue;
            }
            if topic.is_none() {
                self.process_active_message(message, recv_ns)?;
            } else {
                debug!(
                    ?topic,
                    "discarding market frame outside the active subscription request"
                );
            }
        }
    }

    async fn unsubscribe_topic(
        &mut self,
        socket: &mut Socket,
        topic: &str,
    ) -> Result<Option<SubscriptionRefusal>, FeedError> {
        let request_id = self.next_subscription_request_id()?;
        let payload = unsubscribe_payload(&[topic.to_owned()], &request_id);
        tokio::time::timeout(SOCKET_WRITE_TIMEOUT, socket.send(Message::text(payload)))
            .await
            .map_err(|_| FeedError::Transport("market unsubscribe send timed out".to_string()))?
            .map_err(|error| FeedError::Transport(error.to_string()))?;

        let reply_deadline = Instant::now() + SUBSCRIBE_REPLY_TIMEOUT;
        loop {
            let (recv_ns, message) = self
                .next_subscription_message(socket, reply_deadline)
                .await?;
            if let Message::Ping(payload) = &message {
                tokio::time::timeout(
                    SOCKET_WRITE_TIMEOUT,
                    socket.send(Message::Pong(payload.clone())),
                )
                .await
                .map_err(|_| FeedError::Transport("market pong send timed out".to_string()))?
                .map_err(|error| FeedError::Transport(error.to_string()))?;
                continue;
            }
            if let Some(ack) = operation_ack(&message, "unsubscribe") {
                if ack.request_id != request_id {
                    debug!(
                        expected_request_id = request_id,
                        received_request_id = %ack.request_id,
                        "ignoring unmatched market unsubscribe reply"
                    );
                    continue;
                }
                return if ack.success {
                    Ok(None)
                } else {
                    Ok(Some(SubscriptionRefusal {
                        code: ack.code,
                        message: ack.ret_msg,
                    }))
                };
            }

            let message_topic = message_topic(&message);
            if message_topic.is_none()
                || message_topic.is_some_and(|name| self.active_topics.contains(name))
            {
                self.process_active_message(message, recv_ns)?;
            } else {
                debug!(
                    ?message_topic,
                    "discarding market frame while refreshing a topic"
                );
            }
        }
    }

    async fn refresh_stale_quote(
        &mut self,
        socket: &mut Socket,
        topic: String,
    ) -> Result<(), FeedError> {
        let unsubscribe = match self.unsubscribe_topic(socket, &topic).await {
            Ok(outcome) => outcome,
            Err(FeedError::BadMessage(error)) => return Err(FeedError::BadMessage(error)),
            Err(FeedError::Closed) => return Err(FeedError::Closed),
            Err(error) => {
                warn!(%topic, %error, "stale quote refresh could not unsubscribe; retrying later");
                self.active_quote_last_event_at
                    .insert(topic, Instant::now());
                return Ok(());
            }
        };
        if let Some(refusal) = unsubscribe {
            match subscription_refusal_kind(refusal.code, &refusal.message) {
                SubscriptionRefusalKind::Global => {
                    return Err(FeedError::BadMessage(format!(
                        "Bybit rejected the whole unsubscribe request with code 10404: {}",
                        refusal.message
                    )));
                }
                SubscriptionRefusalKind::Transient => {
                    warn!(%topic, message = %refusal.message, "stale quote unsubscribe was refused; retrying later");
                    self.active_quote_last_event_at
                        .insert(topic, Instant::now());
                    return Ok(());
                }
                // The venue already considers it absent. Subscribing again is
                // still the right recovery action.
                SubscriptionRefusalKind::Topic => {}
            }
        }

        self.active_topics.remove(&topic);
        self.active_quote_last_event_at.remove(&topic);
        if let Some(symbol) = topic_symbol(&topic) {
            self.state.reset_quote(symbol);
        }
        match self
            .subscribe_topics(socket, std::slice::from_ref(&topic))
            .await
        {
            Ok(()) => Ok(()),
            Err(FeedError::BadMessage(error)) => Err(FeedError::BadMessage(error)),
            Err(FeedError::Closed) => Err(FeedError::Closed),
            Err(error) => {
                self.quarantine(&topic, &error.to_string());
                warn!(%topic, %error, "stale quote resubscribe failed; scheduled another retry");
                Ok(())
            }
        }
    }

    async fn maintain_topics(&mut self, socket: &mut Socket) -> Result<(), FeedError> {
        let now = Instant::now();
        if now.duration_since(self.manual_reprobe_window_started)
            >= self.timing.quarantine_reprobe_interval
        {
            self.manual_reprobe_window_started = now;
            self.manual_reprobes_in_window = 0;
        }

        let stale_quotes = self
            .active_quote_last_event_at
            .iter()
            .filter(|(_, last_event_at)| {
                now.duration_since(**last_event_at) >= self.timing.market_idle_timeout
            })
            .take(MAX_STALE_QUOTE_REFRESHES_PER_SWEEP)
            .map(|(topic, _)| topic.clone())
            .collect::<Vec<_>>();
        for topic in stale_quotes {
            self.refresh_stale_quote(socket, topic).await?;
        }

        let budget =
            MAX_QUARANTINE_REPROBES_PER_WINDOW.saturating_sub(self.manual_reprobes_in_window);
        let due = self
            .quarantine_reprobe_at
            .iter()
            .filter(|(_, retry_at)| **retry_at <= now)
            .take(budget)
            .map(|(topic, _)| topic.clone())
            .collect::<Vec<_>>();
        for topic in due {
            self.manual_reprobes_in_window += 1;
            self.quarantine_reprobe_at.insert(
                topic.clone(),
                Instant::now() + self.timing.quarantine_reprobe_interval,
            );
            if let Some(retry_epoch) = self.quarantined_topics.get_mut(&topic) {
                *retry_epoch = self.epochs.saturating_add(QUARANTINE_REPROBE_EPOCHS);
            }
            match self
                .subscribe_topics(socket, std::slice::from_ref(&topic))
                .await
            {
                Ok(()) => {}
                Err(FeedError::BadMessage(error)) => {
                    return Err(FeedError::BadMessage(error));
                }
                Err(FeedError::Closed) => return Err(FeedError::Closed),
                Err(error) => {
                    self.quarantine(&topic, &error.to_string());
                    warn!(%topic, %error, "market topic re-probe failed; preserving healthy topics");
                }
            }
        }
        Ok(())
    }

    async fn next_subscription_message(
        &mut self,
        socket: &mut Socket,
        reply_deadline: Instant,
    ) -> Result<(u64, Message), FeedError> {
        loop {
            let now = Instant::now();
            if now >= reply_deadline {
                return Err(FeedError::Transport(
                    "market subscription phase timed out".to_string(),
                ));
            }
            let housekeeping_at = self.next_transport_housekeeping_at();
            if now >= housekeeping_at {
                match self.transport_housekeeping(socket).await? {
                    Step::Reconnect => {
                        return Err(FeedError::Transport(
                            "market keep-alive failed during subscription".to_string(),
                        ))
                    }
                    Step::Event(_) | Step::Idle => continue,
                }
            }
            let wake_at = reply_deadline.min(housekeeping_at);
            match tokio::time::timeout_at(wake_at.into(), socket.next()).await {
                Ok(Some(Ok(message))) => return Ok((self.clock.now_ns(), message)),
                Ok(Some(Err(error))) => return Err(FeedError::Transport(error.to_string())),
                Ok(None) => {
                    return Err(FeedError::Transport(
                        "market socket closed before subscription ack".to_string(),
                    ))
                }
                Err(_) if Instant::now() >= reply_deadline => {
                    return Err(FeedError::Transport(
                        "market subscription phase timed out".to_string(),
                    ))
                }
                Err(_) => match self.transport_housekeeping(socket).await? {
                    Step::Reconnect => {
                        return Err(FeedError::Transport(
                            "market keep-alive failed during subscription".to_string(),
                        ))
                    }
                    Step::Event(_) | Step::Idle => {}
                },
            }
        }
    }

    fn process_active_message(&mut self, message: Message, recv_ns: u64) -> Result<(), FeedError> {
        match self.on_message(message, recv_ns)? {
            Step::Event(event) if !self.emit(Ok(event)) => Err(FeedError::Closed),
            Step::Reconnect => Err(FeedError::Transport(
                "market socket lost continuity during subscription".to_string(),
            )),
            Step::Event(_) | Step::Idle => Ok(()),
        }
    }

    /// Take on symbols the engine has just started following.
    ///
    /// Their topics join `self.topics`, so a reconnect resubscribes them along
    /// with everything else, and the state grows to hold them. Interning here
    /// gives the same ids the feed handed out, because both intern in the same
    /// order: the boot subscriptions, then each admission as it arrives.
    async fn admit(
        &mut self,
        subs: Vec<Subscription>,
        socket: &mut Socket,
    ) -> Result<(), FeedError> {
        let now = Instant::now();
        if now.duration_since(self.manual_reprobe_window_started)
            >= self.timing.quarantine_reprobe_interval
        {
            self.manual_reprobe_window_started = now;
            self.manual_reprobes_in_window = 0;
        }
        let mut fresh: Vec<String> = Vec::new();
        for sub in &subs {
            self.state.intern(&sub.symbol);
            let topic = topic_for(sub);
            if !self.topics.contains(&topic) {
                self.topics.push(topic.clone());
                fresh.push(topic);
            } else if self.quarantined_topics.contains_key(&topic)
                && self.manual_reprobes_in_window < MAX_QUARANTINE_REPROBES_PER_WINDOW
            {
                self.manual_reprobes_in_window += 1;
                fresh.push(topic);
            }
        }
        if fresh.is_empty() {
            return Ok(());
        }
        info!(
            topics = fresh.len(),
            "subscribing to symbols taken on since boot"
        );
        self.subscribe_topics(socket, &fresh).await
    }

    fn bump_backoff(&mut self) {
        self.backoff = (self.backoff * 2).min(BACKOFF_MAX);
    }

    fn next_transport_housekeeping_at(&self) -> Instant {
        let keepalive = self
            .pong_deadline
            .map_or(self.next_ping_at, |pong| pong.min(self.next_ping_at));
        self.market_idle_deadline
            .map_or(keepalive, |market| market.min(keepalive))
    }

    fn next_housekeeping_at(&self) -> Instant {
        let next = self.next_transport_housekeeping_at();
        if self.maintaining_topics {
            next
        } else {
            next.min(self.next_topic_maintenance_at)
        }
    }

    async fn step(&mut self, socket: &mut Socket) -> Result<Step, FeedError> {
        let clock = self.clock;
        let deadline = self.next_housekeeping_at();
        if Instant::now() >= deadline {
            return self.housekeeping(socket).await;
        }
        let incoming = tokio::select! {
            msg = socket.next() => Some((clock.now_ns(), msg)),
            // A symbol the engine has just taken on. Handled before the timer
            // so a book naming a new name is not waiting out a ping interval.
            admitted = self.admissions.recv() => {
                if let Some(subs) = admitted {
                    if let Err(error) = self.admit(subs, socket).await {
                        warn!(%error, "market admission lost continuity; resyncing");
                        return Ok(Step::Reconnect);
                    }
                }
                return Ok(Step::Idle);
            }
            _ = tokio::time::sleep_until(deadline.into()) => None,
        };
        match incoming {
            Some((recv_ns, Some(Ok(msg)))) => self.on_message(msg, recv_ns),
            Some((_, Some(Err(e)))) => {
                warn!("market feed socket error: {e}");
                Ok(Step::Reconnect)
            }
            Some((_, None)) => {
                warn!("market feed socket closed");
                Ok(Step::Reconnect)
            }
            None => self.housekeeping(socket).await,
        }
    }

    fn on_message(&mut self, msg: Message, recv_ns: u64) -> Result<Step, FeedError> {
        let message_topic = message_topic(&msg);
        if let Some(topic) = message_topic {
            if !self.active_topics.contains(topic) {
                debug!(topic, "discarding market frame for inactive topic");
                return Ok(Step::Idle);
            }
        }
        let parsed = match &msg {
            Message::Text(text) => parse_frame(text.as_str()),
            Message::Binary(bytes) => parse_frame_bytes(bytes),
            Message::Close(_) => {
                warn!("market feed received close");
                return Ok(Step::Reconnect);
            }
            // Protocol-level ping/pong is answered by the websocket layer.
            _ => return Ok(Step::Idle),
        };
        let frame = match parsed {
            Ok(frame) => frame,
            Err(e) => {
                // One unreadable frame is not worth dropping the socket for.
                warn!("skipping unreadable market frame: {e}");
                return Ok(Step::Idle);
            }
        };
        match &frame {
            ParsedFrame::Pong => {
                self.pong_deadline = None;
                debug!("market feed keep-alive answered");
            }
            ParsedFrame::Ack { op, success, .. } => {
                debug!(
                    op,
                    success, "ignoring market reply with no matching request"
                );
                return Ok(Step::Idle);
            }
            _ => {}
        }
        match self.state.apply(&frame, recv_ns) {
            Applied::Event(event) => {
                self.backoff = BACKOFF_START;
                self.market_idle_deadline = Some(Instant::now() + self.timing.market_idle_timeout);
                if let Some(last_event_at) =
                    message_topic.and_then(|topic| self.active_quote_last_event_at.get_mut(topic))
                {
                    *last_event_at = Instant::now();
                }
                Ok(Step::Event(event))
            }
            Applied::Nothing => Ok(Step::Idle),
            Applied::Resync(ResyncReason::SubscriptionLost) => {
                let detail = match frame {
                    ParsedFrame::Ack { op, ret_msg, .. } => format!("{op} refused: {ret_msg}"),
                    _ => "subscription refused".to_string(),
                };
                warn!(%detail, "market subscription state changed; resyncing");
                Ok(Step::Reconnect)
            }
            Applied::Resync(reason) => {
                warn!(?reason, "market feed lost continuity; resyncing");
                Ok(Step::Reconnect)
            }
        }
    }

    async fn housekeeping(&mut self, socket: &mut Socket) -> Result<Step, FeedError> {
        let now = Instant::now();
        let transport = self.transport_housekeeping(socket).await?;
        if matches!(transport, Step::Reconnect) {
            return Ok(transport);
        }
        if !self.maintaining_topics && now >= self.next_topic_maintenance_at {
            self.next_topic_maintenance_at = now + self.timing.topic_maintenance_interval;
            self.maintaining_topics = true;
            let result = self.maintain_topics(socket).await;
            self.maintaining_topics = false;
            result?;
        }
        Ok(transport)
    }

    async fn transport_housekeeping(&mut self, socket: &mut Socket) -> Result<Step, FeedError> {
        let now = Instant::now();
        if self
            .market_idle_deadline
            .is_some_and(|deadline| now >= deadline)
        {
            warn!(
                idle_ms = self.timing.market_idle_timeout.as_millis(),
                "market feed produced no accepted market traffic; resyncing"
            );
            return Ok(Step::Reconnect);
        }
        if let Some(deadline) = self.pong_deadline {
            if now >= deadline {
                warn!("market feed keep-alive unanswered; resyncing");
                return Ok(Step::Reconnect);
            }
        }
        if now < self.next_ping_at {
            return Ok(Step::Idle);
        }
        match tokio::time::timeout(
            SOCKET_WRITE_TIMEOUT,
            socket.send(Message::text(PING_PAYLOAD)),
        )
        .await
        {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                warn!(%error, "market feed ping failed");
                return Ok(Step::Reconnect);
            }
            Err(_) => {
                warn!("market feed ping timed out");
                return Ok(Step::Reconnect);
            }
        }
        self.next_ping_at = now + self.timing.ping_interval;
        if self.pong_deadline.is_none() {
            self.pong_deadline = Some(now + self.timing.pong_timeout);
        }
        Ok(Step::Idle)
    }
}

impl Drop for FeedWorker {
    fn drop(&mut self) {
        self.events.close();
    }
}

impl MarketFeed for BybitPublicFeed {
    /// One receive, nothing else. Dropped part-way it loses nothing, which is
    /// what the engine core's `select!` needs.
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self.pending_reset {
            self.pending_reset = false;
            return Ok(MarketEvent::FeedReset {
                recv_ns: engine_types::clock::mono_ns(),
            });
        }
        if self.subs.is_empty() {
            return std::future::pending().await;
        }
        if self.inbox.is_none() {
            self.start();
        }
        let inbox = self.inbox.as_mut().expect("the worker was just started");
        // No sender left means the worker is gone for good.
        inbox.events.recv().await.unwrap_or(Err(FeedError::Closed))
    }
    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        let symbol = symbol.to_uppercase();
        let before = self.subs.len();
        self.subs
            .retain(|row| row.symbol != symbol || row.feed != feed);
        if self.subs.len() == before {
            return false;
        }
        self.topics = self.subs.iter().map(topic_for).collect();
        self.topic_status
            .lock()
            .expect("market topic status lock is poisoned")
            .quarantined
            .retain(|topic, _| self.topics.contains(topic));
        if let Some(inbox) = self.inbox.take() {
            inbox.worker.abort();
        }
        self.admissions = None;
        self.pending_reset = true;
        true
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        Some(BybitPublicFeed::admit(self, symbol, feed))
    }
}

const PING_PAYLOAD: &str = r#"{"op":"ping"}"#;

/// rustls refuses to guess a cipher provider, and tokio-tungstenite leaves the
/// choice to us. Name it once, or the first TLS handshake panics.
fn install_crypto_provider() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        // An error means the process already has one, which is equally fine.
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

fn topic_for(sub: &Subscription) -> String {
    match sub.feed {
        // Depth 1 is the venue's fastest book stream, pushed every 10ms.
        Feed::Quote => format!("orderbook.1.{}", sub.symbol),
        Feed::Depth => format!("orderbook.50.{}", sub.symbol),
        Feed::Trades => format!("publicTrade.{}", sub.symbol),
        Feed::Ticker => format!("tickers.{}", sub.symbol),
    }
}

fn subscribe_payload(topics: &[String], request_id: &str) -> String {
    serde_json::json!({ "op": "subscribe", "req_id": request_id, "args": topics }).to_string()
}

fn unsubscribe_payload(topics: &[String], request_id: &str) -> String {
    serde_json::json!({ "op": "unsubscribe", "req_id": request_id, "args": topics }).to_string()
}

fn is_quote_topic(topic: &str) -> bool {
    topic.starts_with("orderbook.1.")
}

fn topic_symbol(topic: &str) -> Option<&str> {
    topic
        .rsplit_once('.')
        .map(|(_, symbol)| symbol)
        .filter(|symbol| !symbol.is_empty())
}

fn subscription_refusal_kind(code: Option<i64>, message: &str) -> SubscriptionRefusalKind {
    if code == Some(10404) {
        return SubscriptionRefusalKind::Global;
    }
    if matches!(code, Some(10429 | 10016 | 10019)) {
        return SubscriptionRefusalKind::Transient;
    }

    let lower = message.to_ascii_lowercase();
    if [
        "invalid symbol",
        "symbol is invalid",
        "symbol not found",
        "invalid topic",
        "topic is invalid",
        "topic not found",
        "invalid args",
        "args params error",
        "args parameter error",
    ]
    .iter()
    .any(|needle| lower.contains(needle))
    {
        SubscriptionRefusalKind::Topic
    } else {
        SubscriptionRefusalKind::Transient
    }
}

fn message_payload_len(message: &Message) -> usize {
    match message {
        Message::Text(text) => text.len(),
        Message::Binary(bytes) | Message::Ping(bytes) | Message::Pong(bytes) => bytes.len(),
        Message::Close(frame) => frame.as_ref().map_or(0, |frame| frame.reason.len() + 2),
        Message::Frame(frame) => frame.payload().len(),
    }
}

#[derive(Deserialize)]
struct ControlEnvelope<'a> {
    #[serde(borrow, default)]
    topic: Option<&'a str>,
    #[serde(borrow, default)]
    op: Option<&'a str>,
    #[serde(borrow, default, alias = "reqId")]
    req_id: Option<&'a str>,
    #[serde(default)]
    success: Option<bool>,
    #[serde(default, rename = "ret_code", alias = "retCode", alias = "code")]
    ret_code: Option<i64>,
    #[serde(borrow, default, alias = "retMsg")]
    ret_msg: Option<&'a str>,
}

fn control_envelope(message: &Message) -> Option<ControlEnvelope<'_>> {
    match message {
        Message::Text(text) => serde_json::from_str(text.as_str()).ok(),
        Message::Binary(bytes) => serde_json::from_slice(bytes).ok(),
        _ => None,
    }
}

fn subscription_ack(message: &Message) -> Option<SubscriptionAck> {
    operation_ack(message, "subscribe")
}

fn operation_ack(message: &Message, operation: &str) -> Option<SubscriptionAck> {
    let envelope = control_envelope(message)?;
    let success = envelope
        .success
        .unwrap_or_else(|| envelope.ret_code == Some(0))
        && envelope.ret_code.is_none_or(|code| code == 0);
    (envelope.op == Some(operation)).then(|| SubscriptionAck {
        request_id: envelope.req_id.unwrap_or("").to_owned(),
        success,
        code: envelope.ret_code,
        ret_msg: envelope.ret_msg.unwrap_or("").to_owned(),
    })
}

fn message_topic(message: &Message) -> Option<&str> {
    control_envelope(message)?.topic
}

#[cfg(test)]
mod tests;

#[cfg(test)]
#[tokio::test]
async fn empty_demand_after_retirement_stays_idle_until_readmission() {
    use std::future::Future;
    let subs = [Subscription {
        symbol: "BTCUSDT".into(),
        feed: Feed::Quote,
    }];
    let mut feed = BybitPublicFeed::new(&subs);
    assert!(MarketFeed::retire(&mut feed, "BTCUSDT", Feed::Quote));
    assert!(matches!(
        feed.next_event().await.unwrap(),
        MarketEvent::FeedReset { .. }
    ));
    let mut context = std::task::Context::from_waker(std::task::Waker::noop());
    let mut event = Box::pin(feed.next_event());
    assert!(event.as_mut().poll(&mut context).is_pending());
    drop(event);
    assert!(
        feed.inbox.is_none(),
        "empty demand must not start a socket or poll worker"
    );
    assert_eq!(
        MarketFeed::admit(&mut feed, "BTCUSDT", Feed::Quote),
        Some(SymbolId(0))
    );
    let mut event = Box::pin(feed.next_event());
    assert!(event.as_mut().poll(&mut context).is_pending());
    drop(event);
    assert!(
        feed.inbox.is_some(),
        "readmission restarts the worker with its original symbol ID"
    );
}
