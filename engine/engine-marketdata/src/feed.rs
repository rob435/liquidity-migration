//! The socket: connect, subscribe, keep alive, reconnect. Every parsing and
//! bookkeeping decision belongs to [`crate::parse`] and [`crate::state`]; this
//! file owns only the wire and the clock.

use std::sync::Once;
use std::time::{Duration, Instant};

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Subscription, SymbolTable};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tracing::{debug, info, warn};

use crate::parse::{parse_frame, parse_frame_bytes, ParsedFrame};
use crate::state::{Applied, FeedState, ResyncReason};

/// Public market data for USDT/USDC perpetuals. No credentials: the demo
/// account trades against these same prices.
pub const BYBIT_PUBLIC_LINEAR_URL: &str = "wss://stream.bybit.com/v5/public/linear";

const PING_INTERVAL: Duration = Duration::from_secs(20);
const PONG_TIMEOUT: Duration = Duration::from_secs(10);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
/// Bybit caps the size of one request frame, not the number of topics.
const TOPICS_PER_MESSAGE: usize = 100;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// Monotonic nanoseconds from a base taken when the feed was built. Comparable
/// to other engine stamps, not to wall time.
#[derive(Copy, Clone, Debug)]
pub struct MonoClock {
    base: Instant,
}

impl MonoClock {
    pub fn new() -> Self {
        MonoClock {
            base: Instant::now(),
        }
    }

    pub fn now_ns(&self) -> u64 {
        self.base.elapsed().as_nanos() as u64
    }
}

impl Default for MonoClock {
    fn default() -> Self {
        MonoClock::new()
    }
}

/// Bybit's public v5 linear websocket, as a [`MarketFeed`].
pub struct BybitPublicFeed {
    url: String,
    topics: Vec<String>,
    state: FeedState,
    socket: Option<Socket>,
    clock: MonoClock,
    pending: Option<MarketEvent>,
    backoff: Duration,
    /// How many sockets this feed has opened. Past the first, a new one is a
    /// reconnect and owes the strategies a `FeedReset`.
    epochs: u64,
    next_ping_at: Instant,
    pong_deadline: Option<Instant>,
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
        Self::with_url(BYBIT_PUBLIC_LINEAR_URL, subs)
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
        let now = Instant::now();
        BybitPublicFeed {
            url: url.into(),
            topics,
            state: FeedState::new(&subs),
            socket: None,
            clock: MonoClock::new(),
            pending: None,
            backoff: BACKOFF_START,
            epochs: 0,
            next_ping_at: now + PING_INTERVAL,
            pong_deadline: None,
        }
    }

    /// The feed's symbol interning. The engine core seeds its `MarketState`
    /// from this so a `SymbolId` means the same thing on both sides.
    pub fn symbols(&self) -> &SymbolTable {
        self.state.table()
    }

    /// The clock the feed stamps `recv_ns` with.
    pub fn clock(&self) -> MonoClock {
        self.clock
    }

    /// The topics this feed subscribes on every connect.
    pub fn topics(&self) -> &[String] {
        &self.topics
    }

    /// Dial, subscribe, and start a fresh epoch. Retries with capped backoff
    /// until it succeeds; a market feed that gives up is worse than a slow one.
    async fn connect(&mut self) {
        loop {
            if self.epochs > 0 {
                tokio::time::sleep(self.backoff).await;
            }
            match self.dial().await {
                Ok(socket) => {
                    self.socket = Some(socket);
                    // The new socket knows nothing of the old book. Merging
                    // across the seam would invent prices.
                    self.state.reset();
                    let now = Instant::now();
                    self.next_ping_at = now + PING_INTERVAL;
                    self.pong_deadline = None;
                    if self.epochs > 0 {
                        self.pending = Some(MarketEvent::FeedReset {
                            recv_ns: self.clock.now_ns(),
                        });
                    }
                    self.epochs += 1;
                    info!(
                        url = %self.url,
                        topics = self.topics.len(),
                        epoch = self.epochs,
                        "market feed connected"
                    );
                    return;
                }
                Err(e) => {
                    warn!(url = %self.url, backoff = ?self.backoff, "market feed dial failed: {e}");
                    self.bump_backoff();
                }
            }
        }
    }

    async fn dial(&mut self) -> Result<Socket, FeedError> {
        install_crypto_provider();
        let (mut socket, _) = connect_async(self.url.as_str())
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        for chunk in self.topics.chunks(TOPICS_PER_MESSAGE) {
            let payload = subscribe_payload(chunk);
            socket
                .send(Message::text(payload))
                .await
                .map_err(|e| FeedError::Transport(e.to_string()))?;
        }
        Ok(socket)
    }

    fn bump_backoff(&mut self) {
        self.backoff = (self.backoff * 2).min(BACKOFF_MAX);
    }

    async fn step(&mut self) -> Result<Step, FeedError> {
        let clock = self.clock;
        let deadline = match self.pong_deadline {
            Some(pong) => pong.min(self.next_ping_at),
            None => self.next_ping_at,
        };
        let incoming = {
            let socket = self.socket.as_mut().expect("stepped without a socket");
            tokio::select! {
                // Draining the socket beats servicing a timer.
                biased;
                msg = socket.next() => Some((clock.now_ns(), msg)),
                _ = tokio::time::sleep_until(deadline.into()) => None,
            }
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
            None => self.housekeeping().await,
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

    async fn housekeeping(&mut self) -> Result<Step, FeedError> {
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
        let socket = self.socket.as_mut().expect("pinged without a socket");
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

impl MarketFeed for BybitPublicFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        loop {
            if let Some(event) = self.pending.take() {
                return Ok(event);
            }
            if self.socket.is_none() {
                self.connect().await;
                continue;
            }
            match self.step().await? {
                Step::Event(event) => return Ok(event),
                Step::Idle => {}
                Step::Reconnect => {
                    self.socket = None;
                    self.bump_backoff();
                }
            }
        }
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
    fn the_feed_dials_the_public_stream_and_nothing_else() {
        assert_eq!(
            BYBIT_PUBLIC_LINEAR_URL,
            "wss://stream.bybit.com/v5/public/linear"
        );
    }

    #[test]
    fn the_clock_moves_forward() {
        let clock = MonoClock::new();
        let first = clock.now_ns();
        let second = clock.now_ns();
        assert!(second >= first);
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
