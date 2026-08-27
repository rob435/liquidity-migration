//! The socket: connect, subscribe, keep alive, reconnect. Every parsing and
//! bookkeeping decision belongs to [`super::parse`] and [`super::state`]; this
//! file owns only the wire and the clock.
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

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, Once};
use std::time::{Duration, Instant};

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Subscription, SymbolId, SymbolTable};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, Notify};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
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
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const SUBSCRIBE_REPLY_TIMEOUT: Duration = Duration::from_secs(10);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
/// Bybit caps the size of one request frame, not the number of topics.
const TOPICS_PER_MESSAGE: usize = 100;
const MAX_PENDING_EPOCHS: usize = 64;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

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
    /// The same interning the worker builds, so a `SymbolId` handed out here
    /// means what the worker's events mean.
    table: SymbolTable,
    clock: MonoClock,
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
        if let Some(key) = market_key(&item) {
            for queued in state.items.iter_mut().rev() {
                let Some(queued_key) = market_key(queued) else { break };
                if queued_key == key {
                    *queued = item;
                    return true;
                }
            }
        } else {
            let controls = state.items.iter().filter(|item| market_key(item).is_none()).count();
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

fn market_key(item: &Result<MarketEvent, FeedError>) -> Option<(u16, bool)> {
    match item {
        Ok(MarketEvent::Quote { symbol, .. }) => Some((symbol.0, false)),
        Ok(MarketEvent::Ticker { symbol, .. }) => Some((symbol.0, true)),
        Ok(MarketEvent::FeedReset { .. }) | Err(_) => None,
    }
}

enum Step {
    Event(MarketEvent),
    Idle,
    Reconnect,
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
            clock: MonoClock::new(),
            inbox: None,
        }
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
    /// Idempotent: a symbol already known keeps its id and no frame is sent.
    /// Before the worker is running this only records the subscription; the
    /// first dial carries it like any other.
    pub fn admit(&mut self, symbol: &str, feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let sub = Subscription { symbol: symbol.clone(), feed };
        let topic = topic_for(&sub);
        let known = self.topics.contains(&topic);
        if !known {
            self.topics.push(topic);
            self.subs.push(sub.clone());
            if let Some(tx) = &self.admissions {
                // Unbounded, and the worker drains it before servicing a
                // timer, so this cannot block the loop that called it.
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
            state: FeedState::new(&self.subs),
            clock: self.clock,
            events: events.clone(),
            backoff: BACKOFF_START,
            epochs: 0,
            next_ping_at: Instant::now() + PING_INTERVAL,
            pong_deadline: None,
            admissions: admit_rx,
        };
        // The engine runs one thread, so this stays on it.
        let worker = tokio::spawn(worker.run());
        self.inbox = Some(Inbox {
            events,
            worker,
        });
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
    state: FeedState,
    clock: MonoClock,
    events: Arc<Handoff>,
    backoff: Duration,
    /// How many sockets this worker has opened. Past the first, a new one is a
    /// reconnect and owes the strategies a `FeedReset`.
    epochs: u64,
    next_ping_at: Instant,
    pong_deadline: Option<Instant>,
    /// Symbols admitted after the socket was already up.
    admissions: mpsc::UnboundedReceiver<Vec<Subscription>>,
}

impl FeedWorker {
    async fn run(mut self) {
        loop {
            let reconnected = self.epochs > 0;
            let mut socket = match self.connect().await {
                Ok(socket) => socket,
                Err(error) => {
                    let _ = self.emit(Err(error));
                    return;
                }
            };
            // The break is announced before any price off the new socket.
            if reconnected {
                let recv_ns = self.clock.now_ns();
                if !self.emit(Ok(MarketEvent::FeedReset { recv_ns })) {
                    return;
                }
            }
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
            if self.epochs > 0 {
                tokio::time::sleep(self.backoff).await;
            }
            match self.dial().await {
                Ok(socket) => {
                    // The new socket knows nothing of the old book. Merging
                    // across the seam would invent prices.
                    self.state.reset();
                    let now = Instant::now();
                    self.next_ping_at = now + PING_INTERVAL;
                    self.pong_deadline = None;
                    self.epochs += 1;
                    info!(
                        url = %self.url,
                        topics = self.topics.len(),
                        epoch = self.epochs,
                        "market feed connected"
                    );
                    return Ok(socket);
                }
                Err(e) => {
                    if matches!(&e, FeedError::Transport(text) if text.starts_with("subscribe refused:")) {
                        return Err(e);
                    }
                    warn!(url = %self.url, backoff = ?self.backoff, "market feed dial failed: {e}");
                    self.bump_backoff();
                }
            }
        }
    }

    async fn dial(&self) -> Result<Socket, FeedError> {
        install_crypto_provider();
        let connected = tokio::time::timeout(
            CONNECT_TIMEOUT,
            connect_async_with_config(self.url.as_str(), None, true),
        )
        .await
        .map_err(|_| FeedError::Transport("market feed dial timed out".to_string()))?;
        let (mut socket, _) = connected.map_err(|e| FeedError::Transport(e.to_string()))?;
        for chunk in self.topics.chunks(TOPICS_PER_MESSAGE) {
            let payload = subscribe_payload(chunk);
            send_subscription(&mut socket, payload).await?;
        }
        Ok(socket)
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
        let mut fresh: Vec<String> = Vec::new();
        for sub in &subs {
            self.state.intern(&sub.symbol);
            let topic = topic_for(sub);
            if !self.topics.contains(&topic) {
                self.topics.push(topic.clone());
                fresh.push(topic);
            }
        }
        if fresh.is_empty() {
            return Ok(());
        }
        info!(topics = fresh.len(), "subscribing to symbols taken on since boot");
        for chunk in fresh.chunks(TOPICS_PER_MESSAGE) {
            let payload = subscribe_payload(chunk);
            tokio::time::timeout(SUBSCRIBE_REPLY_TIMEOUT, socket.send(Message::text(payload)))
                .await
                .map_err(|_| FeedError::Transport("market subscription send timed out".to_string()))?
                .map_err(|e| FeedError::Transport(e.to_string()))?;
            self.await_admission_ack(socket).await?;
        }
        Ok(())
    }

    async fn await_admission_ack(&mut self, socket: &mut Socket) -> Result<(), FeedError> {
        tokio::time::timeout(SUBSCRIBE_REPLY_TIMEOUT, async {
            loop {
                let message = socket
                    .next()
                    .await
                    .ok_or_else(|| FeedError::Transport("market socket closed before subscription ack".to_string()))?
                    .map_err(|e| FeedError::Transport(e.to_string()))?;
                if let Some((success, ret_msg)) = subscription_ack(&message)? {
                    return if success {
                        Ok(())
                    } else {
                        Err(FeedError::Transport(format!("subscribe refused: {ret_msg}")))
                    };
                }
                let recv_ns = self.clock.now_ns();
                match self.on_message(message, recv_ns)? {
                    Step::Event(event) if !self.emit(Ok(event)) => return Err(FeedError::Closed),
                    Step::Reconnect => {
                        return Err(FeedError::Transport(
                            "market socket lost continuity before subscription ack".to_string(),
                        ))
                    }
                    _ => {}
                }
            }
        })
        .await
        .map_err(|_| FeedError::Transport("no market subscription ack from the venue".to_string()))?
    }

    fn bump_backoff(&mut self) {
        self.backoff = (self.backoff * 2).min(BACKOFF_MAX);
    }

    async fn step(&mut self, socket: &mut Socket) -> Result<Step, FeedError> {
        let clock = self.clock;
        let deadline = match self.pong_deadline {
            Some(pong) => pong.min(self.next_ping_at),
            None => self.next_ping_at,
        };
        let incoming = tokio::select! {
            // Draining the socket beats servicing a timer.
            biased;
            msg = socket.next() => Some((clock.now_ns(), msg)),
            // A symbol the engine has just taken on. Handled before the timer
            // so a book naming a new name is not waiting out a ping interval.
            admitted = self.admissions.recv() => {
                if let Some(subs) = admitted {
                    self.admit(subs, socket).await?;
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
        match frame {
            ParsedFrame::Pong => {
                self.pong_deadline = None;
                debug!("market feed keep-alive answered");
            }
            ParsedFrame::Ack { op, success, .. } if success => {
                debug!(op, "market feed subscription acknowledged");
            }
            _ => {}
        }
        // The socket is carrying real traffic; earn back the fast retry.
        self.backoff = BACKOFF_START;

        match self.state.apply(&frame, recv_ns) {
            Applied::Event(event) => Ok(Step::Event(event)),
            Applied::Nothing => Ok(Step::Idle),
            Applied::Resync(ResyncReason::SubscriptionLost) => {
                // Reconnecting cannot fix a topic the venue refuses.
                let detail = match frame {
                    ParsedFrame::Ack { op, ret_msg, .. } => format!("{op} refused: {ret_msg}"),
                    _ => "subscription refused".to_string(),
                };
                Err(FeedError::Transport(detail))
            }
            Applied::Resync(reason) => {
                warn!(?reason, "market feed lost continuity; resyncing");
                Ok(Step::Reconnect)
            }
        }
    }

    async fn housekeeping(&mut self, socket: &mut Socket) -> Result<Step, FeedError> {
        let now = Instant::now();
        if let Some(deadline) = self.pong_deadline {
            if now >= deadline {
                warn!("market feed keep-alive unanswered; resyncing");
                return Ok(Step::Reconnect);
            }
        }
        if now < self.next_ping_at {
            return Ok(Step::Idle);
        }
        if let Err(e) = socket.send(Message::text(PING_PAYLOAD)).await {
            warn!("market feed ping failed: {e}");
            return Ok(Step::Reconnect);
        }
        self.next_ping_at = now + PING_INTERVAL;
        if self.pong_deadline.is_none() {
            self.pong_deadline = Some(now + PONG_TIMEOUT);
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
        Feed::Ticker => format!("tickers.{}", sub.symbol),
    }
}

fn subscribe_payload(topics: &[String]) -> String {
    serde_json::json!({ "op": "subscribe", "args": topics }).to_string()
}

async fn send_subscription(socket: &mut Socket, payload: String) -> Result<(), FeedError> {
    tokio::time::timeout(SUBSCRIBE_REPLY_TIMEOUT, async {
        socket
            .send(Message::text(payload))
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        loop {
            let message = socket
                .next()
                .await
                .ok_or_else(|| FeedError::Transport("market socket closed before subscription ack".to_string()))?
                .map_err(|e| FeedError::Transport(e.to_string()))?;
            if let Message::Ping(payload) = &message {
                socket
                    .send(Message::Pong(payload.clone()))
                    .await
                    .map_err(|e| FeedError::Transport(e.to_string()))?;
                continue;
            }
            match subscription_ack(&message)? {
                Some((true, _)) => return Ok(()),
                Some((false, ret_msg)) => {
                    return Err(FeedError::Transport(format!("subscribe refused: {ret_msg}")))
                }
                None => {
                    let is_market_data = matches!(
                        &message,
                        Message::Text(text)
                            if matches!(parse_frame(text.as_str()), Ok(ParsedFrame::Book(_) | ParsedFrame::Ticker(_)))
                    ) || matches!(
                        &message,
                        Message::Binary(bytes)
                            if matches!(parse_frame_bytes(bytes), Ok(ParsedFrame::Book(_) | ParsedFrame::Ticker(_)))
                    );
                    if is_market_data {
                        return Err(FeedError::Transport(
                            "market data arrived before the subscription was acknowledged".to_string(),
                        ));
                    }
                }
            }
        }
    })
    .await
    .map_err(|_| FeedError::Transport("market subscription phase timed out".to_string()))?
}

fn subscription_ack(message: &Message) -> Result<Option<(bool, String)>, FeedError> {
    let parsed = match message {
        Message::Text(text) => parse_frame(text.as_str()),
        Message::Binary(bytes) => parse_frame_bytes(bytes),
        _ => return Ok(None),
    }
    .map_err(|e| FeedError::BadMessage(e.to_string()))?;
    match parsed {
        ParsedFrame::Ack { op, success, ret_msg } if op == "subscribe" => {
            Ok(Some((success, ret_msg.to_string())))
        }
        _ => Ok(None),
    }
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
    fn the_subscribe_frame_is_the_venue_shape() {
        let topics = vec!["orderbook.1.BTCUSDT".to_string(), "tickers.BTCUSDT".into()];
        assert_eq!(
            subscribe_payload(&topics),
            r#"{"args":["orderbook.1.BTCUSDT","tickers.BTCUSDT"],"op":"subscribe"}"#
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
            quote: engine_types::Quote { bid_px, ..engine_types::Quote::default() },
        };
        assert!(handoff.push(Ok(quote(0, 10.0))));
        assert!(handoff.push(Ok(quote(0, 11.0))));
        assert!(handoff.push(Ok(quote(1, 20.0))));
        assert!(handoff.push(Ok(MarketEvent::FeedReset { recv_ns: 7 })));
        assert!(handoff.push(Ok(quote(0, 12.0))));

        assert!(matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(0), quote })) if quote.bid_px == 11.0));
        assert!(matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(1), quote })) if quote.bid_px == 20.0));
        assert!(matches!(handoff.recv().await, Some(Ok(MarketEvent::FeedReset { recv_ns: 7 }))));
        assert!(matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(0), quote })) if quote.bid_px == 12.0));
    }

    const ACK: &str =
        r#"{"success":true,"ret_msg":"","conn_id":"x","req_id":"1","op":"subscribe"}"#;

    fn snapshot(update_id: u64, bid: f64, ask: f64) -> String {
        format!(
            r#"{{"topic":"orderbook.1.BTCUSDT","ts":10,"type":"snapshot","data":{{"s":"BTCUSDT","b":[["{bid}","1"]],"a":[["{ask}","1"]],"u":{update_id}}},"cts":9}}"#
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
        if let Some(Ok(Message::Text(text))) = ws.next().await {
            subscribes
                .send(text.to_string())
                .expect("test is listening");
        }
        ws.send(Message::text(ACK)).await.expect("sends ack");
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
            if !matches!(ws.next().await, Some(Ok(Message::Text(_)))) {
                continue;
            }
            if ws.send(Message::text(ACK)).await.is_err() {
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

    const REFUSED: &str = r#"{"success":false,"ret_msg":"Invalid symbol","conn_id":"x","req_id":"1","op":"subscribe"}"#;

    /// A topic the venue refuses is not a broken socket — dialling again would
    /// only ask the same bad question. The worker says so once and stops, and
    /// the feed reads closed from then on.
    #[tokio::test]
    async fn a_refused_subscription_ends_the_feed() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds");
        let port = listener.local_addr().expect("has an address").port();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let _ = ws.next().await;
            ws.send(Message::text(REFUSED))
                .await
                .expect("sends the refusal");
            // Hold the socket open. The worker must stop on its own.
            while ws.next().await.is_some() {}
        });

        let mut feed = BybitPublicFeed::with_url(
            format!("ws://127.0.0.1:{port}"),
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );

        let refusal = tokio::time::timeout(Duration::from_secs(10), feed.next_event())
            .await
            .expect("an answer before the deadline");
        assert!(
            matches!(refusal, Err(FeedError::Transport(_))),
            "expected the venue's refusal, got {refusal:?}"
        );
        let after = tokio::time::timeout(Duration::from_secs(10), feed.next_event())
            .await
            .expect("an answer before the deadline");
        assert!(
            matches!(after, Err(FeedError::Closed)),
            "a stopped worker leaves the feed closed, got {after:?}"
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
            let _ = ws.next().await;
            ws.send(Message::text(ACK)).await.expect("sends ack");
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
                MarketEvent::FeedReset { .. } => {}
            }
        }
        assert!(quotes >= 5, "only {quotes} quotes arrived");
        assert!(tickers >= 1, "only {tickers} tickers arrived");
    }
}
