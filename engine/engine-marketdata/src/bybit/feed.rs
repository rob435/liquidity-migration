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
    engine_venue::VenueRealm::Demo.public_ws()
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
        let worker = FeedWorker {
            url: self.url.clone(),
            topics: self.topics.clone(),
            quarantined_topics: BTreeMap::new(),
            quarantine_reprobe_at: BTreeMap::new(),
            manual_reprobe_window_started: Instant::now(),
            manual_reprobes_in_window: 0,
            active_topics: BTreeSet::new(),
            topic_status: self.topic_status.clone(),
            state: FeedState::new(&self.subs),
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

    fn quarantine(&mut self, topic: String, reason: &str) {
        let retry_epoch = self.epochs.saturating_add(QUARANTINE_REPROBE_EPOCHS);
        let retry_at = Instant::now() + self.timing.quarantine_reprobe_interval;
        let first = self
            .quarantined_topics
            .insert(topic.clone(), retry_epoch)
            .is_none();
        self.quarantine_reprobe_at.insert(topic.clone(), retry_at);
        self.active_topics.remove(&topic);
        self.active_quote_last_event_at.remove(&topic);
        self.topic_status
            .lock()
            .expect("market topic status lock is poisoned")
            .quarantined
            .entry(topic.clone())
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
                                self.quarantine(group[0].clone(), &refusal.message);
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
                self.quarantine(topic.clone(), &error.to_string());
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
                    self.quarantine(topic.clone(), &error.to_string());
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
        if self.inbox.is_none() {
            self.start();
        }
        let inbox = self.inbox.as_mut().expect("the worker was just started");
        // No sender left means the worker is gone for good.
        inbox.events.recv().await.unwrap_or(Err(FeedError::Closed))
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
    let success = envelope.success.unwrap_or(envelope.ret_code == Some(0))
        && !envelope.ret_code.is_some_and(|code| code != 0);
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
mod tests {
    use super::*;

    fn subs() -> Vec<Subscription> {
        vec![
            Subscription {
                symbol: "btcusdt".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Ticker,
            },
            Subscription {
                symbol: "ETHUSDT".into(),
                feed: Feed::Quote,
            },
        ]
    }

    #[test]
    fn subscriptions_become_topics_once_each() {
        let feed = BybitPublicFeed::new(&subs());
        assert_eq!(
            feed.topics(),
            [
                "orderbook.1.BTCUSDT",
                "orderbook.1.ETHUSDT",
                "tickers.BTCUSDT"
            ]
        );
        assert_eq!(feed.symbols().len(), 2);
    }

    #[test]
    fn l50_and_public_trades_use_the_exact_bybit_topics() {
        assert_eq!(
            topic_for(&Subscription {
                symbol: "ONTUSDT".into(),
                feed: Feed::Depth,
            }),
            "orderbook.50.ONTUSDT",
        );
        assert_eq!(
            topic_for(&Subscription {
                symbol: "ONTUSDT".into(),
                feed: Feed::Trades,
            }),
            "publicTrade.ONTUSDT",
        );
    }

    #[test]
    fn only_l1_quotes_have_a_per_topic_idle_contract() {
        assert!(is_quote_topic("orderbook.1.BTCUSDT"));
        assert!(!is_quote_topic("orderbook.50.BTCUSDT"));
        assert!(!is_quote_topic("tickers.BTCUSDT"));
        assert!(!is_quote_topic("publicTrade.BTCUSDT"));
    }

    #[test]
    fn the_subscribe_frame_is_the_venue_shape() {
        let topics = vec!["orderbook.1.BTCUSDT".to_string(), "tickers.BTCUSDT".into()];
        assert_eq!(
            subscribe_payload(&topics, "7"),
            r#"{"args":["orderbook.1.BTCUSDT","tickers.BTCUSDT"],"op":"subscribe","req_id":"7"}"#
        );
    }

    #[test]
    fn subscription_response_code_is_parsed_as_an_integer() {
        let message = Message::text(
            r#"{"retCode":10404,"retMsg":"op type is not found","reqId":"7","op":"subscribe"}"#,
        );
        let ack = subscription_ack(&message).expect("subscription response");
        assert_eq!(ack.request_id, "7");
        assert_eq!(ack.code, Some(10404));
        assert!(!ack.success);
        assert_eq!(ack.ret_msg, "op type is not found");
    }

    #[test]
    fn subscription_refusals_are_scoped_by_code_and_specific_topic_text() {
        assert_eq!(
            subscription_refusal_kind(None, "Invalid symbol"),
            SubscriptionRefusalKind::Topic
        );
        assert_eq!(
            subscription_refusal_kind(None, "args params error"),
            SubscriptionRefusalKind::Topic
        );
        assert_eq!(
            subscription_refusal_kind(Some(10404), "op type is not found"),
            SubscriptionRefusalKind::Global
        );
        assert_eq!(
            subscription_refusal_kind(None, "request handler not found"),
            SubscriptionRefusalKind::Transient
        );
        assert_eq!(
            subscription_refusal_kind(None, "not found"),
            SubscriptionRefusalKind::Transient
        );
        assert_eq!(
            subscription_refusal_kind(Some(10429), "system level frequency protection"),
            SubscriptionRefusalKind::Transient
        );
        assert_eq!(
            subscription_refusal_kind(Some(10016), "service is restarting"),
            SubscriptionRefusalKind::Transient
        );
    }

    #[test]
    fn the_feed_dials_the_realm_tables_public_stream_and_nothing_else() {
        // The literal is pinned in `engine-venue`'s own fence, which is the
        // one place a venue host may be written down. What this crate has to
        // promise is only that it reads it from there — a host spelled out
        // here would be one the fence never sees.
        assert_eq!(
            bybit_public_linear_url(),
            engine_venue::VenueRealm::Demo.public_ws()
        );
        assert!(bybit_public_linear_url().starts_with("wss://"));
        assert!(bybit_public_linear_url().ends_with("/v5/public/linear"));
    }

    #[test]
    fn the_clock_moves_forward() {
        let clock = MonoClock::new();
        let first = clock.now_ns();
        let second = clock.now_ns();
        assert!(second >= first);
    }

    #[tokio::test]
    async fn the_handoff_coalesces_l1_without_crossing_an_epoch_reset() {
        let handoff = Handoff::new();
        let quote = |symbol, bid_px| MarketEvent::Quote {
            symbol: SymbolId(symbol),
            quote: engine_types::Quote {
                bid_px,
                ..engine_types::Quote::default()
            },
        };
        assert!(handoff.push(Ok(quote(0, 10.0))));
        assert!(handoff.push(Ok(quote(0, 11.0))));
        assert!(handoff.push(Ok(quote(1, 20.0))));
        assert!(handoff.push(Ok(MarketEvent::FeedReset { recv_ns: 7 })));
        assert!(handoff.push(Ok(quote(0, 12.0))));

        assert!(
            matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(0), quote })) if quote.bid_px == 11.0)
        );
        assert!(
            matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(1), quote })) if quote.bid_px == 20.0)
        );
        assert!(matches!(
            handoff.recv().await,
            Some(Ok(MarketEvent::FeedReset { recv_ns: 7 }))
        ));
        assert!(
            matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(0), quote })) if quote.bid_px == 12.0)
        );
    }

    #[tokio::test]
    async fn the_handoff_adds_trade_flow_instead_of_dropping_bursts() {
        let handoff = Handoff::new();
        let flow = |buy_qty, sell_qty, seq| MarketEvent::Trades {
            symbol: SymbolId(0),
            trades: engine_types::TradeFlow {
                buy_qty,
                sell_qty,
                last_px: seq as f64,
                trade_count: 1,
                seq,
                recv_ns: seq,
                ..engine_types::TradeFlow::default()
            },
        };
        assert!(handoff.push(Ok(flow(1.0, 0.0, 1))));
        assert!(handoff.push(Ok(flow(0.0, 2.0, 2))));
        let Some(Ok(MarketEvent::Trades { trades, .. })) = handoff.recv().await else {
            panic!("expected merged trade flow");
        };
        assert_eq!(trades.buy_qty, 1.0);
        assert_eq!(trades.sell_qty, 2.0);
        assert_eq!(trades.trade_count, 2);
        assert_eq!(trades.last_px, 2.0);
        assert_eq!(trades.seq, 2);
    }

    fn operation_request(message: &Message) -> (String, String, Vec<String>) {
        let value: serde_json::Value =
            serde_json::from_str(message.to_text().expect("text request")).expect("request JSON");
        let operation = value["op"].as_str().expect("request operation").to_owned();
        let request_id = value["req_id"].as_str().expect("request id").to_owned();
        let topics = value["args"]
            .as_array()
            .expect("request topics")
            .iter()
            .map(|topic| topic.as_str().expect("topic").to_owned())
            .collect();
        (operation, request_id, topics)
    }

    fn subscribe_request(message: &Message) -> (String, Vec<String>) {
        let (operation, request_id, topics) = operation_request(message);
        assert_eq!(operation, "subscribe");
        (request_id, topics)
    }

    fn subscribe_reply(request_id: &str, success: bool, ret_msg: &str) -> String {
        serde_json::json!({
            "success": success,
            "ret_msg": ret_msg,
            "conn_id": "x",
            "req_id": request_id,
            "op": "subscribe"
        })
        .to_string()
    }

    fn subscribe_reply_with_code(request_id: &str, code: i64, ret_msg: &str) -> String {
        serde_json::json!({
            "success": code == 0,
            "retCode": code,
            "retMsg": ret_msg,
            "conn_id": "x",
            "reqId": request_id,
            "op": "subscribe"
        })
        .to_string()
    }

    fn unsubscribe_reply(request_id: &str, success: bool, ret_msg: &str) -> String {
        serde_json::json!({
            "success": success,
            "ret_msg": ret_msg,
            "conn_id": "x",
            "req_id": request_id,
            "op": "unsubscribe"
        })
        .to_string()
    }

    fn snapshot(update_id: u64, bid: f64, ask: f64) -> String {
        snapshot_for("BTCUSDT", update_id, bid, ask)
    }

    fn snapshot_for(symbol: &str, update_id: u64, bid: f64, ask: f64) -> String {
        format!(
            r#"{{"topic":"orderbook.1.{symbol}","ts":10,"type":"snapshot","data":{{"s":"{symbol}","b":[["{bid}","1"]],"a":[["{ask}","1"]],"u":{update_id}}},"cts":9}}"#
        )
    }

    async fn next(feed: &mut BybitPublicFeed) -> MarketEvent {
        tokio::time::timeout(Duration::from_secs(10), feed.next_event())
            .await
            .expect("an event before the deadline")
            .expect("feed stays healthy")
    }

    /// Accept one connection, echo the subscribe request back to the test,
    /// send the scripted frames, then hang up.
    async fn serve_once(
        listener: &tokio::net::TcpListener,
        frames: &[String],
        subscribes: &tokio::sync::mpsc::UnboundedSender<String>,
    ) {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        if let Some(Ok(message)) = ws.next().await {
            subscribes
                .send(message.to_text().expect("text request").to_owned())
                .expect("test is listening");
            let (request_id, _) = subscribe_request(&message);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("sends ack");
        }
        for frame in frames {
            ws.send(Message::text(frame.clone())).await.expect("sends");
        }
        ws.close(None).await.expect("closes");
    }

    /// A dropped socket must resubscribe, announce the break, and only then
    /// deliver the new epoch's prices.
    #[tokio::test]
    async fn a_dropped_socket_resubscribes_and_announces_the_reset() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (tx, mut subscribes) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            serve_once(&listener, &[snapshot(100, 10.0, 10.1)], &tx).await;
            serve_once(&listener, &[snapshot(500, 11.0, 11.1)], &tx).await;
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );

        match next(&mut feed).await {
            MarketEvent::Quote { quote, .. } => assert_eq!(quote.bid_px, 10.0),
            other => panic!("expected the first epoch's quote, got {other:?}"),
        }
        // The break is announced before any price from the new socket.
        assert!(
            matches!(next(&mut feed).await, MarketEvent::FeedReset { .. }),
            "the reconnect must announce itself first"
        );
        match next(&mut feed).await {
            MarketEvent::Quote { quote, .. } => assert_eq!(quote.bid_px, 11.0),
            other => panic!("expected the second epoch's quote, got {other:?}"),
        }

        // Both sockets were told what to send.
        for _ in 0..2 {
            let sent = subscribes.recv().await.expect("a subscribe per connection");
            assert!(sent.contains("orderbook.1.BTCUSDT"), "subscribe was {sent}");
        }
    }

    #[tokio::test]
    async fn ping_responses_cannot_hide_a_market_silent_socket() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (ping_tx, mut pings) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("first socket");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("first handshake");
            let request = ws.next().await.expect("first request").expect("read");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("accepts first subscription");
            while let Some(Ok(message)) = ws.next().await {
                let Ok(text) = message.to_text() else {
                    continue;
                };
                let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
                    continue;
                };
                if value["op"] == "ping" {
                    ping_tx.send(()).expect("test listens");
                    ws.send(Message::text(
                        r#"{"success":true,"ret_msg":"pong","op":"ping"}"#,
                    ))
                    .await
                    .expect("answers application ping");
                    ws.send(Message::text(r#"{"op":"notice"}"#))
                        .await
                        .expect("sends an ignored control frame");
                    ws.send(Message::text("not-json"))
                        .await
                        .expect("sends an unreadable frame");
                }
            }

            let (stream, _) = listener.accept().await.expect("second socket");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("second handshake");
            let request = ws.next().await.expect("second request").expect("read");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("accepts second subscription");
            ws.send(Message::text(snapshot(200, 20.0, 20.1)))
                .await
                .expect("fresh epoch publishes market traffic");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        )
        .with_timing(
            Duration::from_millis(30),
            Duration::from_millis(25),
            Duration::from_millis(200),
        );

        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::FeedReset { .. }
        ));
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0
        ));
        let mut answered = 0;
        while pings.try_recv().is_ok() {
            answered += 1;
        }
        assert!(
            answered >= 2,
            "the first socket did not stay ping-responsive"
        );
    }

    #[tokio::test]
    async fn ack_and_pong_only_epochs_escalate_reconnect_backoff() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (attempts_tx, mut attempts) = tokio::sync::mpsc::unbounded_channel();
        let (pings_tx, mut pings) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            for _ in 0..3 {
                let (stream, _) = listener.accept().await.expect("accepts epoch");
                attempts_tx
                    .send(Instant::now())
                    .expect("test tracks attempts");
                let mut ws = tokio_tungstenite::accept_async(stream)
                    .await
                    .expect("handshakes");
                let request = ws.next().await.expect("subscription").expect("read");
                let (request_id, _) = subscribe_request(&request);
                ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                    .await
                    .expect("acknowledges subscription");
                while let Some(Ok(message)) = ws.next().await {
                    let Ok(text) = message.to_text() else {
                        continue;
                    };
                    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
                        continue;
                    };
                    if value["op"] == "ping" {
                        pings_tx.send(()).expect("test tracks pings");
                        ws.send(Message::text(
                            r#"{"success":true,"ret_msg":"pong","op":"ping"}"#,
                        ))
                        .await
                        .expect("answers application ping");
                    }
                }
            }
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        )
        .with_timing(
            Duration::from_millis(30),
            Duration::from_millis(25),
            Duration::from_millis(150),
        );
        let waiting = tokio::spawn(async move {
            for _ in 0..2 {
                assert!(matches!(
                    feed.next_event().await,
                    Ok(MarketEvent::FeedReset { .. })
                ));
            }
        });

        let stamps = tokio::time::timeout(Duration::from_secs(4), async {
            let mut stamps = Vec::new();
            for _ in 0..3 {
                stamps.push(attempts.recv().await.expect("worker keeps reconnecting"));
            }
            stamps
        })
        .await
        .expect("three paced epochs before the deadline");
        waiting.await.expect("feed consumer");

        assert!(
            stamps[1].duration_since(stamps[0]) >= BACKOFF_START * 2,
            "the first market-silent epoch did not earn a 500ms retry: {stamps:?}"
        );
        assert!(
            stamps[2].duration_since(stamps[1]) >= BACKOFF_START * 4,
            "ACK/pong traffic reset the escalating retry delay: {stamps:?}"
        );
        let mut answered = 0;
        while pings.try_recv().is_ok() {
            answered += 1;
        }
        assert!(answered >= 4, "the silent epochs were not pong-responsive");
    }

    #[tokio::test]
    async fn accepted_market_traffic_refreshes_the_idle_deadline() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let request = ws.next().await.expect("request").expect("read");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("accepts subscription");
            tokio::time::sleep(Duration::from_millis(180)).await;
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("first market event");
            tokio::time::sleep(Duration::from_millis(180)).await;
            ws.send(Message::text(snapshot(101, 11.0, 11.1)))
                .await
                .expect("second market event");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        )
        .with_timing(
            Duration::from_secs(1),
            Duration::from_secs(1),
            Duration::from_millis(300),
        );

        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
        ));
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0
        ));
    }

    #[tokio::test]
    async fn one_frozen_quote_is_refreshed_without_interrupting_healthy_topics() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("one stable socket");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let request = ws.next().await.expect("boot request").expect("read");
            let (request_id, topics) = subscribe_request(&request);
            assert_eq!(topics, ["orderbook.1.BTCUSDT", "orderbook.1.ETHUSDT"]);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("accepts both quotes");
            ws.send(Message::text(snapshot_for("BTCUSDT", 1, 10.0, 10.1)))
                .await
                .expect("initial BTC quote");
            ws.send(Message::text(snapshot_for("ETHUSDT", 1, 20.0, 20.1)))
                .await
                .expect("initial ETH quote");
            let eth_last_event_at = Instant::now();

            let mut updates = 2_u64;
            let mut ticks = tokio::time::interval(Duration::from_millis(20));
            ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut eth_unsubscribed = false;
            loop {
                tokio::select! {
                    _ = ticks.tick() => {
                        ws.send(Message::text(snapshot_for(
                            "BTCUSDT",
                            updates,
                            1_000.0 + updates as f64,
                            1_000.1 + updates as f64,
                        )))
                        .await
                        .expect("healthy BTC quote");
                        updates += 1;
                    }
                    incoming = ws.next() => {
                        let message = incoming.expect("refresh request").expect("reads request");
                        let (operation, request_id, topics) = operation_request(&message);
                        assert_eq!(topics, ["orderbook.1.ETHUSDT"]);
                        match operation.as_str() {
                            "unsubscribe" => {
                                assert!(!eth_unsubscribed, "ETH was unsubscribed twice");
                                assert!(
                                    eth_last_event_at.elapsed() >= Duration::from_millis(140),
                                    "ETH refreshed before its idle deadline"
                                );
                                eth_unsubscribed = true;
                                ws.send(Message::text(unsubscribe_reply(&request_id, true, "")))
                                    .await
                                    .expect("accepts ETH unsubscribe");
                            }
                            "subscribe" => {
                                assert!(eth_unsubscribed, "ETH resubscribed before unsubscribe");
                                ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                                    .await
                                    .expect("accepts ETH resubscribe");
                                ws.send(Message::text(snapshot_for(
                                    "ETHUSDT", 2, 222.0, 222.1,
                                )))
                                .await
                                .expect("fresh ETH snapshot");
                                return;
                            }
                            other => panic!("unexpected operation {other}"),
                        }
                    }
                }
            }
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[
                Subscription {
                    symbol: "BTCUSDT".into(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "ETHUSDT".into(),
                    feed: Feed::Quote,
                },
            ],
        )
        .with_timing(
            Duration::from_secs(1),
            Duration::from_secs(1),
            Duration::from_millis(160),
        )
        .with_topic_timing(Duration::from_secs(1), Duration::from_millis(5));

        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
        ));
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0
        ));

        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let mut healthy_btc_updates = 0;
        loop {
            let event = tokio::time::timeout_at(deadline, feed.next_event())
                .await
                .expect("frozen ETH recovers before the deadline")
                .expect("feed remains healthy");
            match event {
                MarketEvent::Quote { quote, .. } if quote.bid_px == 222.0 => break,
                MarketEvent::Quote { quote, .. } if quote.bid_px >= 1_000.0 => {
                    healthy_btc_updates += 1;
                }
                MarketEvent::FeedReset { .. } => {
                    panic!("a single frozen quote reconnected the whole feed")
                }
                _ => {}
            }
        }
        assert!(
            healthy_btc_updates > 0,
            "healthy BTC traffic stopped while ETH recovered"
        );
        server.await.expect("stable-socket server");
    }

    #[tokio::test]
    async fn market_data_before_the_subscribe_ack_is_delivered() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let request = ws.next().await.expect("request").expect("read request");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("sends the early quote");
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("sends ack");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );

        match next(&mut feed).await {
            MarketEvent::Quote { quote, .. } => assert_eq!(quote.bid_px, 10.0),
            other => panic!("expected the quote that preceded the ack, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn request_wide_10404_is_surfaced_without_topic_bisection() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (requests_tx, mut requests) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            while let Some(Ok(request @ Message::Text(_))) = ws.next().await {
                let (request_id, topics) = subscribe_request(&request);
                requests_tx.send(topics).expect("test listens");
                ws.send(Message::text(subscribe_reply_with_code(
                    &request_id,
                    10404,
                    "op type is not found",
                )))
                .await
                .expect("returns the request-wide refusal");
            }
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[
                Subscription {
                    symbol: "BTCUSDT".into(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "ETHUSDT".into(),
                    feed: Feed::Quote,
                },
            ],
        );

        let error = tokio::time::timeout(Duration::from_secs(2), feed.next_event())
            .await
            .expect("the global refusal is surfaced")
            .expect_err("the feed must not run after a global protocol refusal");
        assert!(
            matches!(error, FeedError::BadMessage(ref message) if message.contains("10404")),
            "unexpected refusal: {error}"
        );
        assert_eq!(
            requests.recv().await.expect("the original request"),
            ["orderbook.1.BTCUSDT", "orderbook.1.ETHUSDT"]
        );
        let second = tokio::time::timeout(Duration::from_millis(100), requests.recv()).await;
        assert!(
            !matches!(second, Ok(Some(_))),
            "a request-wide refusal was incorrectly bisected"
        );
    }

    #[tokio::test]
    async fn a_stray_refusal_cannot_break_an_active_feed() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (first_seen, continue_after_first) = tokio::sync::oneshot::channel();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let request = ws.next().await.expect("request").expect("read");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("accepts subscription");
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("first quote");
            continue_after_first.await.expect("client saw first quote");

            ws.send(Message::text(subscribe_reply(
                &request_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("sends a delayed refusal for the completed request");
            ws.send(Message::text(snapshot(101, 11.0, 11.1)))
                .await
                .expect("active topic keeps flowing");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
        ));
        first_seen.send(()).expect("server still waits");
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0
        ));
    }

    #[tokio::test]
    async fn a_delayed_ack_cannot_activate_a_refused_topic_or_release_its_early_frame() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");

            let combined = ws.next().await.expect("combined request").expect("read");
            let (combined_id, combined_topics) = subscribe_request(&combined);
            assert_eq!(combined_topics.len(), 2);
            ws.send(Message::text(subscribe_reply(
                &combined_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("splits the group");

            let bad = ws.next().await.expect("bad singleton").expect("read");
            let (bad_id, bad_topics) = subscribe_request(&bad);
            assert_eq!(bad_topics, ["orderbook.1.BADUSDT"]);
            ws.send(Message::text(subscribe_reply(&combined_id, true, "")))
                .await
                .expect("sends delayed old ack");
            ws.send(Message::text(snapshot_for("BADUSDT", 10, 666.0, 667.0)))
                .await
                .expect("sends refused topic data before its reply");
            ws.send(Message::text(subscribe_reply(
                &bad_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("refuses bad singleton");

            let good = ws.next().await.expect("good singleton").expect("read");
            let (good_id, good_topics) = subscribe_request(&good);
            assert_eq!(good_topics, ["orderbook.1.BTCUSDT"]);
            ws.send(Message::text(subscribe_reply(&good_id, true, "")))
                .await
                .expect("accepts good singleton");
            ws.send(Message::text(snapshot(20, 10.0, 10.1)))
                .await
                .expect("sends good quote");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[
                Subscription {
                    symbol: "BADUSDT".into(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "BTCUSDT".into(),
                    feed: Feed::Quote,
                },
            ],
        );
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0),
            "a delayed ACK or refused topic frame escaped activation"
        );
    }

    #[tokio::test]
    async fn a_quarantine_survives_a_later_setup_disconnect() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (requests_tx, mut requests) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("first socket");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("first handshake");
            let combined = ws.next().await.expect("combined request").expect("read");
            let (combined_id, combined_topics) = subscribe_request(&combined);
            requests_tx.send(combined_topics).expect("test listens");
            ws.send(Message::text(subscribe_reply(
                &combined_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("splits group");
            let bad = ws.next().await.expect("bad singleton").expect("read");
            let (bad_id, bad_topics) = subscribe_request(&bad);
            requests_tx.send(bad_topics).expect("test listens");
            ws.send(Message::text(subscribe_reply(
                &bad_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("quarantines bad");
            let good = ws.next().await.expect("good singleton").expect("read");
            let (_, good_topics) = subscribe_request(&good);
            requests_tx.send(good_topics).expect("test listens");
            drop(ws);

            let (stream, _) = listener.accept().await.expect("second socket");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("second handshake");
            let retried = ws.next().await.expect("retry request").expect("read");
            let (retry_id, retry_topics) = subscribe_request(&retried);
            requests_tx.send(retry_topics).expect("test listens");
            ws.send(Message::text(subscribe_reply(&retry_id, true, "")))
                .await
                .expect("accepts healthy retry");
            ws.send(Message::text(snapshot(30, 12.0, 12.1)))
                .await
                .expect("sends healthy quote");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[
                Subscription {
                    symbol: "BADUSDT".into(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "BTCUSDT".into(),
                    feed: Feed::Quote,
                },
            ],
        );
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::FeedReset { .. }
        ));
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 12.0)
        );
        assert_eq!(requests.recv().await.expect("combined").len(), 2);
        assert_eq!(requests.recv().await.expect("bad"), ["orderbook.1.BADUSDT"]);
        assert_eq!(
            requests.recv().await.expect("good"),
            ["orderbook.1.BTCUSDT"]
        );
        assert_eq!(
            requests.recv().await.expect("retry"),
            ["orderbook.1.BTCUSDT"],
            "an incrementally quarantined topic returned on the immediate reconnect"
        );
    }

    #[tokio::test]
    async fn active_topic_traffic_bypasses_a_slow_late_subscription() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (flooded, flood_done) = tokio::sync::oneshot::channel();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let boot = ws.next().await.expect("boot request").expect("read");
            let (boot_id, _) = subscribe_request(&boot);
            ws.send(Message::text(subscribe_reply(&boot_id, true, "")))
                .await
                .expect("accepts boot topic");
            ws.send(Message::text(snapshot(1, 10.0, 10.1)))
                .await
                .expect("sends boot quote");

            let admission = ws.next().await.expect("late admission").expect("read");
            let (admission_id, admission_topics) = subscribe_request(&admission);
            assert_eq!(admission_topics, ["orderbook.1.ETHUSDT"]);
            for update in 0..=MAX_SUBSCRIPTION_PENDING_MESSAGES {
                ws.send(Message::text(snapshot(
                    100 + update as u64,
                    20.0 + update as f64,
                    20.1 + update as f64,
                )))
                .await
                .expect("active quote flood stays writable");
            }
            ws.send(Message::text(subscribe_reply(&admission_id, true, "")))
                .await
                .expect("eventually accepts late topic");
            ws.send(Message::text(snapshot(10_000, 99_999.0, 99_999.1)))
                .await
                .expect("sends post-ack sentinel");
            let _ = flooded.send(());
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
        );
        feed.admit("ETHUSDT", Feed::Quote);
        tokio::time::timeout(Duration::from_secs(10), flood_done)
            .await
            .expect("the accepted topic did not stall the socket")
            .expect("server completed the flood");
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            let event = tokio::time::timeout_at(deadline, feed.next_event())
                .await
                .expect("sentinel before deadline")
                .expect("feed stays live through the slow ACK");
            match event {
                MarketEvent::Quote { quote, .. } if quote.bid_px == 99_999.0 => break,
                MarketEvent::FeedReset { .. } => {
                    panic!("active traffic overflowed subscription staging and reconnected")
                }
                _ => {}
            }
        }
    }

    /// Serve one epoch: accept, wait for the subscribe, ack, send the frames,
    /// hang up. A connection that dies before it says anything is a dial the
    /// caller abandoned — drop it and wait for the next one.
    async fn serve_epoch(listener: &tokio::net::TcpListener, frames: &[String]) {
        loop {
            let Ok((stream, _)) = listener.accept().await else {
                continue;
            };
            let Ok(mut ws) = tokio_tungstenite::accept_async(stream).await else {
                continue;
            };
            let Some(Ok(request @ Message::Text(_))) = ws.next().await else {
                continue;
            };
            let (request_id, _) = subscribe_request(&request);
            if ws
                .send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .is_err()
            {
                continue;
            }
            for frame in frames {
                if ws.send(Message::text(frame.clone())).await.is_err() {
                    break;
                }
            }
            let _ = ws.close(None).await;
            return;
        }
    }

    /// The engine core waits on the feed inside a `select!` and throws away
    /// the future of every branch that did not win; its flush tick fires
    /// every 250ms. Drive the feed exactly that way — a fresh `next_event`
    /// future each time round — and the reconnect must still land.
    #[tokio::test]
    async fn a_reconnect_lands_while_the_caller_cancels_every_250ms() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            serve_epoch(&listener, &[snapshot(100, 10.0, 10.1)]).await;
            serve_epoch(&listener, &[snapshot(500, 11.0, 11.1)]).await;
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );

        // The core's flush cadence, first tick immediate, same as the loop.
        let mut flush_tick = tokio::time::interval(Duration::from_millis(250));
        let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
        let mut second_epoch = false;
        let mut seen: Vec<String> = Vec::new();

        while !second_epoch && tokio::time::Instant::now() < deadline {
            tokio::select! {
                event = feed.next_event() => match event {
                    Ok(event) => {
                        seen.push(format!("{event:?}"));
                        if let MarketEvent::Quote { quote, .. } = event {
                            second_epoch = quote.bid_px == 11.0;
                        }
                    }
                    Err(e) => {
                        seen.push(format!("error: {e}"));
                        break;
                    }
                },
                _ = flush_tick.tick() => {}
                _ = tokio::time::sleep_until(deadline) => {}
            }
        }

        assert!(
            second_epoch,
            "the second epoch's quote never arrived; the feed produced {seen:?}"
        );
    }

    #[tokio::test]
    async fn failed_first_dials_are_backed_off() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (tx, mut attempts) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            for _ in 0..3 {
                let (stream, _) = listener.accept().await.expect("accepts");
                tx.send(Instant::now()).expect("test is listening");
                drop(stream);
            }
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        let waiting = tokio::spawn(async move { feed.next_event().await });
        let stamps = tokio::time::timeout(Duration::from_secs(4), async {
            let mut stamps = Vec::new();
            for _ in 0..3 {
                stamps.push(attempts.recv().await.expect("worker keeps retrying"));
            }
            stamps
        })
        .await
        .expect("three paced attempts before the deadline");
        waiting.abort();

        assert!(
            stamps[1].duration_since(stamps[0]) >= BACKOFF_START * 2,
            "the second initial dial was not backed off: {stamps:?}"
        );
        assert!(
            stamps[2].duration_since(stamps[1]) >= BACKOFF_START * 4,
            "the third initial dial did not increase its backoff: {stamps:?}"
        );
    }

    /// One retired topic is isolated and omitted on reconnect. Healthy books
    /// keep flowing across both epochs.
    #[tokio::test]
    async fn a_refused_topic_does_not_end_the_feed() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (requests_tx, mut requests) = tokio::sync::mpsc::unbounded_channel();

        tokio::spawn(async move {
            for epoch in 0..2 {
                let (stream, _) = listener.accept().await.expect("accepts");
                let mut ws = tokio_tungstenite::accept_async(stream)
                    .await
                    .expect("handshakes");
                while let Some(Ok(request @ Message::Text(_))) = ws.next().await {
                    let (request_id, names) = subscribe_request(&request);
                    requests_tx.send(names.clone()).expect("test listens");
                    if names.iter().any(|topic| topic.contains("BADUSDT")) {
                        ws.send(Message::text(subscribe_reply(
                            &request_id,
                            false,
                            "Invalid symbol",
                        )))
                        .await
                        .expect("refuses bad");
                        continue;
                    }
                    ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                        .await
                        .expect("accepts healthy");
                    ws.send(Message::text(snapshot(
                        100 + epoch * 100,
                        10.0 + epoch as f64,
                        10.1 + epoch as f64,
                    )))
                    .await
                    .expect("sends healthy quote");
                    break;
                }
                ws.close(None).await.expect("closes epoch");
            }
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[
                Subscription {
                    symbol: "BADUSDT".into(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "BTCUSDT".into(),
                    feed: Feed::Quote,
                },
            ],
        );

        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
        );
        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::FeedReset { .. }
        ));
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0)
        );

        let first = requests.recv().await.expect("initial group");
        let isolated_bad = requests.recv().await.expect("bad half");
        let isolated_good = requests.recv().await.expect("good half");
        let after_reconnect = requests.recv().await.expect("next epoch");
        assert_eq!(first.len(), 2);
        assert_eq!(isolated_bad, ["orderbook.1.BADUSDT"]);
        assert_eq!(isolated_good, ["orderbook.1.BTCUSDT"]);
        assert_eq!(after_reconnect, ["orderbook.1.BTCUSDT"]);
    }

    #[tokio::test]
    async fn a_refused_late_admission_keeps_books_live_and_can_be_reprobed() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let boot = ws.next().await.expect("boot request").expect("read");
            let (boot_request_id, _) = subscribe_request(&boot);
            ws.send(Message::text(subscribe_reply(&boot_request_id, true, "")))
                .await
                .expect("accepts boot topic");
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("sends boot quote");

            let admission = ws.next().await.expect("admission arrives").expect("read");
            assert!(
                admission.to_text().expect("text").contains("BADUSDT"),
                "unexpected admission {admission:?}"
            );
            let (admission_request_id, _) = subscribe_request(&admission);
            ws.send(Message::text(subscribe_reply(
                &admission_request_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("refuses only the new topic");
            ws.send(Message::text(snapshot(101, 11.0, 11.1)))
                .await
                .expect("existing topic continues");

            let retry = ws.next().await.expect("retry arrives").expect("read");
            let (retry_request_id, retry_topics) = subscribe_request(&retry);
            assert_eq!(retry_topics, ["orderbook.1.BADUSDT"]);
            ws.send(Message::text(subscribe_reply(&retry_request_id, true, "")))
                .await
                .expect("accepts explicit retry");
            ws.send(Message::text(snapshot_for("BADUSDT", 102, 20.0, 20.1)))
                .await
                .expect("retried topic becomes live");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
        );
        feed.admit("BADUSDT", Feed::Quote);
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0)
        );
        feed.admit("BADUSDT", Feed::Quote);
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0)
        );
    }

    #[tokio::test]
    async fn a_quarantined_topic_reprobes_itself_on_the_same_socket() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("one stable socket");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let boot = ws.next().await.expect("boot request").expect("read");
            let (boot_id, boot_topics) = subscribe_request(&boot);
            assert_eq!(boot_topics, ["orderbook.1.BTCUSDT"]);
            ws.send(Message::text(subscribe_reply(&boot_id, true, "")))
                .await
                .expect("accepts boot quote");
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("initial BTC quote");

            let admission = ws.next().await.expect("one admission").expect("read");
            let (admission_id, admission_topics) = subscribe_request(&admission);
            assert_eq!(admission_topics, ["orderbook.1.BADUSDT"]);
            ws.send(Message::text(subscribe_reply(
                &admission_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("quarantines the late topic");
            let quarantined_at = Instant::now();
            ws.send(Message::text(snapshot(101, 11.0, 11.1)))
                .await
                .expect("healthy quote remains live");

            let retry = tokio::time::timeout(Duration::from_secs(1), ws.next())
                .await
                .expect("the feed schedules its own retry")
                .expect("retry request")
                .expect("reads retry");
            assert!(
                quarantined_at.elapsed() >= Duration::from_millis(70),
                "quarantine retried before its cooldown"
            );
            let (retry_id, retry_topics) = subscribe_request(&retry);
            assert_eq!(retry_topics, ["orderbook.1.BADUSDT"]);
            ws.send(Message::text(subscribe_reply(&retry_id, true, "")))
                .await
                .expect("accepts the timed retry");
            ws.send(Message::text(snapshot_for("BADUSDT", 102, 20.0, 20.1)))
                .await
                .expect("retried topic becomes live");
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        )
        .with_timing(
            Duration::from_secs(1),
            Duration::from_secs(1),
            Duration::from_millis(500),
        )
        .with_topic_timing(Duration::from_millis(80), Duration::from_millis(5));

        assert!(matches!(
            next(&mut feed).await,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
        ));
        feed.admit("BADUSDT", Feed::Quote);

        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let mut healthy_quote_seen = false;
        loop {
            let event = tokio::time::timeout_at(deadline, feed.next_event())
                .await
                .expect("timed re-probe completes")
                .expect("feed remains healthy");
            match event {
                MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0 => break,
                MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0 => {
                    healthy_quote_seen = true;
                }
                MarketEvent::FeedReset { .. } => {
                    panic!("the timed topic re-probe opened a new socket")
                }
                _ => {}
            }
        }
        assert!(
            healthy_quote_seen,
            "the accepted topic stopped during quarantine"
        );
        server.await.expect("stable-socket server");
    }

    #[tokio::test]
    async fn repeated_admission_cannot_turn_one_quarantine_into_a_request_storm() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let boot = ws.next().await.expect("boot request").expect("read");
            let (boot_id, _) = subscribe_request(&boot);
            ws.send(Message::text(subscribe_reply(&boot_id, true, "")))
                .await
                .expect("accepts boot topic");
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("sends boot quote");

            let admission = ws.next().await.expect("admission").expect("read");
            let (admission_id, admission_topics) = subscribe_request(&admission);
            assert_eq!(admission_topics, ["orderbook.1.BADUSDT"]);
            ws.send(Message::text(subscribe_reply(
                &admission_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("refuses new topic");
            ws.send(Message::text(snapshot(101, 11.0, 11.1)))
                .await
                .expect("signals quarantine completion");

            let retry = ws.next().await.expect("one explicit retry").expect("read");
            let (retry_id, retry_topics) = subscribe_request(&retry);
            assert_eq!(retry_topics, ["orderbook.1.BADUSDT"]);
            ws.send(Message::text(subscribe_reply(
                &retry_id,
                false,
                "Invalid symbol",
            )))
            .await
            .expect("refuses retry");
            assert!(
                tokio::time::timeout(Duration::from_millis(200), ws.next())
                    .await
                    .is_err(),
                "repeated admission sent another probe inside the cooldown"
            );
            ws.send(Message::text(snapshot(102, 12.0, 12.1)))
                .await
                .expect("healthy topic remains live");
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
        );
        feed.admit("BADUSDT", Feed::Quote);
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0)
        );
        for _ in 0..32 {
            feed.admit("BADUSDT", Feed::Quote);
        }
        assert!(
            matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 12.0)
        );
    }

    /// Dropping the feed must take the socket with it, or a dead engine would
    /// leave a task reading prices nobody wants.
    #[tokio::test]
    async fn dropping_the_feed_lets_go_of_the_socket() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();
        let (hung_up, closed) = tokio::sync::oneshot::channel();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let request = ws.next().await.expect("request").expect("read");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("sends ack");
            ws.send(Message::text(snapshot(100, 10.0, 10.1)))
                .await
                .expect("sends");
            // Runs out when the other end goes away.
            while ws.next().await.is_some() {}
            let _ = hung_up.send(());
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        assert!(matches!(next(&mut feed).await, MarketEvent::Quote { .. }));

        drop(feed);
        tokio::time::timeout(Duration::from_secs(5), closed)
            .await
            .expect("the socket closed when the feed did")
            .expect("the server was still listening");
    }

    /// Connects to the real public stream. Off by default; run with
    /// `cargo test -p engine-marketdata -- --ignored live_public_feed`.
    #[tokio::test]
    #[ignore = "needs network"]
    async fn live_public_feed_delivers_quotes_and_tickers() {
        let mut feed = BybitPublicFeed::new(&[
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Ticker,
            },
        ]);
        let mut quotes = 0;
        let mut tickers = 0;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
        while (quotes < 5 || tickers < 1) && tokio::time::Instant::now() < deadline {
            let event = tokio::time::timeout_at(deadline, feed.next_event())
                .await
                .expect("a frame within the deadline")
                .expect("feed stays healthy");
            match event {
                MarketEvent::Quote { quote, .. } => {
                    assert!(quote.ask_px > quote.bid_px, "crossed book: {quote:?}");
                    assert!(quote.recv_ns > 0);
                    assert!(quote.venue_ts_ms > 1_700_000_000_000);
                    quotes += 1;
                }
                MarketEvent::Ticker { ticker, .. } => {
                    assert!(ticker.mark_px > 0.0, "no mark price: {ticker:?}");
                    tickers += 1;
                }
                MarketEvent::FeedReset { .. }
                | MarketEvent::Depth { .. }
                | MarketEvent::Trades { .. } => {}
            }
        }
        assert!(quotes >= 5, "only {quotes} quotes arrived");
        assert!(tickers >= 1, "only {tickers} tickers arrived");
    }

    #[tokio::test]
    #[ignore = "needs network"]
    async fn live_public_feed_delivers_l50_and_aggressor_trades() {
        let mut feed = BybitPublicFeed::new(&[
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Depth,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Trades,
            },
        ]);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        let mut depth_seen = false;
        let mut trades_seen = false;
        while (!depth_seen || !trades_seen) && tokio::time::Instant::now() < deadline {
            let event = tokio::time::timeout_at(deadline, feed.next_event())
                .await
                .expect("a frame within the deadline")
                .expect("feed stays healthy");
            match event {
                MarketEvent::Depth { depth, .. } => {
                    assert!(depth.bid_len > 1, "not a deep book: {depth:?}");
                    assert!(depth.ask_len > 1, "not a deep book: {depth:?}");
                    assert!(depth.best_ask().unwrap().px > depth.best_bid().unwrap().px);
                    depth_seen = true;
                }
                MarketEvent::Trades { trades, .. } => {
                    assert!(trades.trade_count > 0);
                    assert!(trades.last_px > 0.0);
                    trades_seen = true;
                }
                MarketEvent::FeedReset { .. }
                | MarketEvent::Quote { .. }
                | MarketEvent::Ticker { .. } => {}
            }
        }
        assert!(depth_seen, "no L50 event arrived");
        assert!(trades_seen, "no public trade event arrived");
    }
}
