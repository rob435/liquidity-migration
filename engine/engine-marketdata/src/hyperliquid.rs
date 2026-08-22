//! Hyperliquid public market data.
//!
//! One socket, two subscriptions per symbol: `bbo` for the touch and
//! `activeAssetCtx` for mark, oracle and funding. Both are public — no
//! credentials — and both are per coin, so a symbol taken on after boot is
//! subscribed while the socket stays up.
//!
//! **The funding rate here is not the same number Bybit publishes.** Bybit
//! quotes the rate for its next eight-hourly settlement; Hyperliquid pays
//! every hour, and the `funding` field is that hourly rate — one eighth of the
//! eight-hour figure the same market would quote elsewhere. It is reported as
//! the venue states it, because converting it here would hide which venue a
//! number came from. Anything comparing carry across venues has to scale by
//! the interval, and [`Ticker::next_funding_ms`] is what says when the next
//! one lands.
//!
//! The socket lives in its own task for the same reason Bybit's does: the
//! engine waits on `next_event` inside a `select!` and drops the losing
//! branch's future several times a second, so a dial or a backoff sleep held
//! inside it would start over from nothing every tick.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Quote, Subscription, SymbolId, Ticker};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tracing::{info, warn};

/// The venue drops a socket that has said nothing for 60 seconds.
/// How long the venue has to send anything at all after a keep-alive before
/// the socket counts as dead. The Bybit feed's number.
const PONG_TIMEOUT: Duration = Duration::from_secs(10);

const PING_INTERVAL: Duration = Duration::from_secs(30);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
const QUEUE_DEPTH: usize = 4096;

/// Funding is paid every hour, on the hour. The venue's own rule, and the only
/// way to say when the next settlement is: the stream carries a rate and no
/// settlement time.
const FUNDING_INTERVAL_MS: i64 = 3_600_000;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// Hyperliquid's public websocket, as a [`MarketFeed`].
pub struct HyperliquidPublicFeed {
    url: String,
    subs: Vec<Subscription>,
    admissions: Option<mpsc::UnboundedSender<Vec<Subscription>>>,
    /// Name to id, shared with the worker's decoder. A lock rather than a
    /// second table built in the same order: two tables that must be interned
    /// in lockstep is a rule somebody eventually breaks, and the symptom is a
    /// price delivered under another symbol's id.
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    inbox: Option<Inbox>,
}

struct Inbox {
    events: mpsc::Receiver<Result<MarketEvent, FeedError>>,
    worker: JoinHandle<()>,
}

impl HyperliquidPublicFeed {
    /// Build the feed against the realm's socket. Nothing is dialled until the
    /// first `next_event`.
    pub fn new(realm: engine_venue::HyperliquidRealm, subs: &[Subscription]) -> Self {
        Self::with_url(realm.websocket(), subs)
    }

    pub fn with_url(url: impl Into<String>, subs: &[Subscription]) -> Self {
        let subs: Vec<Subscription> = subs
            .iter()
            .map(|s| Subscription {
                symbol: s.symbol.to_uppercase(),
                feed: s.feed,
            })
            .collect();
        let mut feed = HyperliquidPublicFeed {
            url: url.into(),
            subs: Vec::new(),
            admissions: None,
            ids: Arc::new(RwLock::new(HashMap::new())),
            inbox: None,
        };
        for sub in subs {
            feed.remember(sub);
        }
        feed
    }

    /// The id this feed hands out for a symbol, if it follows it.
    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(&symbol.to_uppercase()).copied()
    }

    /// Start following a symbol this feed was not built with.
    pub fn admit(&mut self, symbol: &str, feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let sub = Subscription {
            symbol: symbol.clone(),
            feed,
        };
        if self.remember(sub.clone()) {
            if let Some(tx) = &self.admissions {
                // Unbounded, and the worker drains it before servicing a
                // timer, so this cannot block the loop that called it.
                let _ = tx.send(vec![sub]);
            }
        }
        intern(&self.ids, &symbol)
    }

    /// True when this subscription is new. Both feeds of one symbol share a
    /// coin, so the socket is asked once per coin per feed kind.
    fn remember(&mut self, sub: Subscription) -> bool {
        intern(&self.ids, &sub.symbol);
        if self.subs.contains(&sub) {
            return false;
        }
        self.subs.push(sub);
        true
    }

    fn start(&mut self) {
        let (events, inbox) = mpsc::channel(QUEUE_DEPTH);
        let (admit_tx, admit_rx) = mpsc::unbounded_channel();
        self.admissions = Some(admit_tx);
        let worker = Worker {
            url: self.url.clone(),
            subs: self.subs.clone(),
            ids: self.ids.clone(),
            admissions: admit_rx,
            backoff: Duration::ZERO,
            connected_before: false,
        };
        let handle = tokio::spawn(worker.run(events));
        self.inbox = Some(Inbox {
            events: inbox,
            worker: handle,
        });
    }
}

impl Drop for HyperliquidPublicFeed {
    fn drop(&mut self) {
        if let Some(inbox) = &self.inbox {
            inbox.worker.abort();
        }
    }
}

impl MarketFeed for HyperliquidPublicFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self.inbox.is_none() {
            self.start();
        }
        let inbox = self.inbox.as_mut().expect("started just above");
        match inbox.events.recv().await {
            Some(event) => event,
            None => Err(FeedError::Closed),
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        Some(HyperliquidPublicFeed::admit(self, symbol, feed))
    }
}

struct Worker {
    url: String,
    subs: Vec<Subscription>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    admissions: mpsc::UnboundedReceiver<Vec<Subscription>>,
    backoff: Duration,
    connected_before: bool,
}

impl Worker {
    async fn run(mut self, events: mpsc::Sender<Result<MarketEvent, FeedError>>) {
        loop {
            match self.connect().await {
                Ok(mut socket) => {
                    let opened = Instant::now();
                    if self.connected_before {
                        // Prices during the gap were missed; the engine clears
                        // its picture rather than reading a stale one as
                        // current.
                        let reset = MarketEvent::FeedReset {
                            recv_ns: engine_types::clock::mono_ns(),
                        };
                        if events.send(Ok(reset)).await.is_err() {
                            return;
                        }
                    }
                    self.connected_before = true;
                    if self.pump(&mut socket, &events).await.is_err() {
                        return;
                    }
                    if opened.elapsed() >= Duration::from_secs(30) {
                        self.backoff = Duration::ZERO;
                    }
                }
                Err(e) => {
                    warn!(error = %e, "hyperliquid market feed did not come up; trying again");
                    if events.send(Err(e)).await.is_err() {
                        return;
                    }
                }
            }
            if !self.backoff.is_zero() {
                tokio::time::sleep(self.backoff).await;
            }
            self.backoff = if self.backoff.is_zero() {
                BACKOFF_START
            } else {
                (self.backoff * 2).min(BACKOFF_MAX)
            };
        }
    }

    async fn connect(&mut self) -> Result<Socket, FeedError> {
        install_crypto_provider();
        let (mut socket, _) = connect_async(self.url.as_str())
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        let subs = self.subs.clone();
        self.subscribe(&mut socket, &subs).await?;
        info!(symbols = self.subs.len(), "hyperliquid market feed subscribed");
        Ok(socket)
    }

    async fn subscribe(
        &mut self,
        socket: &mut Socket,
        subs: &[Subscription],
    ) -> Result<(), FeedError> {
        for sub in subs {
            let payload = json!({
                "method": "subscribe",
                "subscription": {"type": channel_for(sub.feed), "coin": coin_of(&sub.symbol)},
            });
            socket
                .send(Message::text(payload.to_string()))
                .await
                .map_err(|e| FeedError::Transport(e.to_string()))?;
        }
        Ok(())
    }

    async fn pump(
        &mut self,
        socket: &mut Socket,
        events: &mpsc::Sender<Result<MarketEvent, FeedError>>,
    ) -> Result<(), Gone> {
        let mut last_ping = Instant::now();
        // Armed when a keep-alive goes out, cleared by anything the venue
        // sends back. Without it a half-open socket — the TCP session alive,
        // the venue silent — is never noticed: pings go out forever, no frame
        // ever arrives, and the engine sees neither prices nor a fault. This
        // is the Bybit feed's rule, which the two newer feeds did not carry.
        let mut pong_due: Option<Instant> = None;
        loop {
            let deadline = tokio::time::Instant::from_std(match pong_due {
                Some(due) => due.min(last_ping + PING_INTERVAL),
                None => last_ping + PING_INTERVAL,
            });
            tokio::select! {
                fresh = self.admissions.recv() => {
                    let Some(fresh) = fresh else { return Err(Gone) };
                    for sub in &fresh {
                        intern(&self.ids, &sub.symbol);
                        if !self.subs.contains(sub) {
                            self.subs.push(sub.clone());
                        }
                    }
                    if self.subscribe(socket, &fresh).await.is_err() {
                        return Ok(());
                    }
                }
                frame = socket.next() => {
                    // Any frame at all proves the far side is answering.
                    pong_due = None;
                    match frame {
                        Some(Ok(Message::Text(text))) => {
                            match self.decode(text.as_str()) {
                                Ok(Some(event)) => {
                                    if events.send(Ok(event)).await.is_err() {
                                        return Err(Gone);
                                    }
                                }
                                Ok(None) => (),
                                Err(e) => {
                                    if events.send(Err(e)).await.is_err() {
                                        return Err(Gone);
                                    }
                                }
                            }
                        }
                        Some(Ok(Message::Ping(payload))) => {
                            let _ = socket.send(Message::Pong(payload)).await;
                        }
                        Some(Ok(Message::Close(_))) | None => {
                            warn!("hyperliquid market feed closed; reconnecting");
                            return Ok(());
                        }
                        Some(Ok(_)) => (),
                        Some(Err(e)) => {
                            warn!(error = %e, "hyperliquid market feed dropped; reconnecting");
                            return Ok(());
                        }
                    }
                }
                _ = tokio::time::sleep_until(deadline) => {
                    if pong_due.is_some_and(|due| Instant::now() >= due) {
                        warn!("hyperliquid market feed stopped answering; reconnecting");
                        return Ok(());
                    }
                    if socket.send(Message::text(r#"{"method":"ping"}"#)).await.is_err() {
                        return Ok(());
                    }
                    last_ping = Instant::now();
                    if pong_due.is_none() {
                        pong_due = Some(last_ping + PONG_TIMEOUT);
                    }
                }
            }
        }
    }

    fn decode(&mut self, text: &str) -> Result<Option<MarketEvent>, FeedError> {
        let frame: Value = serde_json::from_str(text)
            .map_err(|e| FeedError::BadMessage(format!("{e}: {}", first_chars(text))))?;
        let Some(channel) = frame.get("channel").and_then(Value::as_str) else {
            return Ok(None);
        };
        let recv_ns = engine_types::clock::mono_ns();
        match channel {
            "bbo" => Ok(self.decode_bbo(&frame, recv_ns)),
            "activeAssetCtx" => Ok(self.decode_ctx(&frame, recv_ns)),
            "error" => {
                let why = frame.get("data").and_then(Value::as_str).unwrap_or("no reason");
                Err(FeedError::Transport(format!(
                    "venue refused a market subscription: {why}"
                )))
            }
            _ => Ok(None),
        }
    }

    fn decode_bbo(&self, frame: &Value, recv_ns: u64) -> Option<MarketEvent> {
        let data = frame.get("data")?;
        let symbol = symbol_of(data.get("coin")?.as_str()?);
        let id = self.id_of(&symbol)?;
        let levels = data.get("bbo")?.as_array()?;
        // Either side may be absent when nothing is resting there. A zeroed
        // price would read as a real one, so a half-empty book is not an
        // event at all.
        let bid = levels.first().and_then(level)?;
        let ask = levels.get(1).and_then(level)?;
        Some(MarketEvent::Quote {
            symbol: id,
            quote: Quote {
                bid_px: bid.0,
                bid_qty: bid.1,
                ask_px: ask.0,
                ask_qty: ask.1,
                venue_ts_ms: data.get("time").and_then(Value::as_i64).unwrap_or(0),
                recv_ns,
                // The venue numbers no sequence on this channel. Zero is
                // "not stated"; nothing here claims continuity it cannot see.
                seq: 0,
            },
        })
    }

    fn decode_ctx(&self, frame: &Value, recv_ns: u64) -> Option<MarketEvent> {
        let data = frame.get("data")?;
        let symbol = symbol_of(data.get("coin")?.as_str()?);
        let id = self.id_of(&symbol)?;
        let ctx = data.get("ctx")?;
        let venue_ts_ms = engine_types::clock::wall_ms();
        Some(MarketEvent::Ticker {
            symbol: id,
            ticker: Ticker {
                last_px: number(ctx, "midPx").unwrap_or(0.0),
                mark_px: number(ctx, "markPx").unwrap_or(0.0),
                index_px: number(ctx, "oraclePx").unwrap_or(0.0),
                // The venue's own hourly rate, not scaled to anyone else's
                // settlement period. See the module note.
                funding_rate: number(ctx, "funding").unwrap_or(0.0),
                next_funding_ms: next_funding_ms(venue_ts_ms),
                venue_ts_ms,
                recv_ns,
            },
        })
    }
}

impl Worker {
    fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(symbol).copied()
    }
}

/// Give a symbol an id, or return the one it already has. Ids are positions,
/// assigned in the order symbols are first seen — the same rule every other
/// table in the engine follows.
fn intern(ids: &Arc<RwLock<HashMap<String, SymbolId>>>, symbol: &str) -> SymbolId {
    let mut ids = ids.write().expect("the symbol map lock is poisoned");
    if let Some(id) = ids.get(symbol) {
        return *id;
    }
    let id = SymbolId(u16::try_from(ids.len()).expect("more than 65535 symbols"));
    ids.insert(symbol.to_string(), id);
    id
}

/// The engine dropped the feed.
struct Gone;

/// The next hour boundary, in venue wall milliseconds. Funding is paid every
/// hour on this venue, so the next settlement is the next whole hour.
fn next_funding_ms(now_ms: i64) -> i64 {
    if now_ms <= 0 {
        return 0;
    }
    now_ms - now_ms.rem_euclid(FUNDING_INTERVAL_MS) + FUNDING_INTERVAL_MS
}

fn channel_for(feed: Feed) -> &'static str {
    match feed {
        Feed::Quote => "bbo",
        Feed::Ticker => "activeAssetCtx",
    }
}

/// `BTCUSDT` -> `BTC`, the same mapping the gateway uses.
///
/// Upper-cased, because that is how a symbol reaches this crate — the Python
/// fleet upper-cases every symbol in a target book, and the engine's tables
/// keep that spelling. The venue's own name for a few assets is not upper-case
/// (`kPEPE`, `kBONK`, `kSHIB` and their kin), and the subscription names the
/// coin, so those cannot be subscribed here: the venue refuses the channel and
/// the feed redials rather than serving a wrong price. Trading them would need
/// this feed to read the venue's asset list for its spelling, which it does
/// not do.
fn coin_of(symbol: &str) -> String {
    let upper = symbol.trim().to_ascii_uppercase();
    for suffix in ["USDT", "USDC", "USD"] {
        if let Some(head) = upper.strip_suffix(suffix) {
            if !head.is_empty() {
                return head.to_string();
            }
        }
    }
    upper
}

/// `BTC` -> `BTCUSDT`, the engine's spelling.
fn symbol_of(coin: &str) -> String {
    format!("{}USDT", coin.trim().to_ascii_uppercase())
}

fn level(value: &Value) -> Option<(f64, f64)> {
    let px = value.get("px")?.as_str()?.parse().ok()?;
    let sz = value.get("sz")?.as_str()?.parse().ok()?;
    Some((px, sz))
}

fn number(obj: &Value, name: &str) -> Option<f64> {
    match obj.get(name) {
        Some(Value::String(text)) => text.parse().ok(),
        Some(Value::Number(n)) => n.as_f64(),
        _ => None,
    }
}

fn first_chars(text: &str) -> String {
    text.chars().take(160).collect()
}

fn install_crypto_provider() {
    use std::sync::Once;
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn worker() -> Worker {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        intern(&ids, "BTCUSDT");
        intern(&ids, "ETHUSDT");
        let (_tx, rx) = mpsc::unbounded_channel();
        Worker {
            url: "ws://127.0.0.1:1".to_string(),
            subs: Vec::new(),
            ids,
            admissions: rx,
            backoff: Duration::ZERO,
            connected_before: false,
        }
    }

    #[test]
    fn a_book_symbol_and_the_venues_coin_map_both_ways() {
        assert_eq!(coin_of("BTCUSDT"), "BTC");
        assert_eq!(coin_of("kPEPEUSDT"), "KPEPE");
        assert_eq!(symbol_of("BTC"), "BTCUSDT");
        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"] {
            assert_eq!(symbol_of(&coin_of(symbol)), symbol);
        }
    }

    #[test]
    fn the_two_feeds_subscribe_to_the_venues_two_channels() {
        assert_eq!(channel_for(Feed::Quote), "bbo");
        assert_eq!(channel_for(Feed::Ticker), "activeAssetCtx");
    }

    #[test]
    fn a_touch_becomes_a_quote() {
        let mut w = worker();
        let event = w
            .decode(
                r#"{"channel":"bbo","data":{"coin":"BTC","time":1700,
                   "bbo":[{"px":"94990.5","sz":"1.5","n":3},{"px":"95010.5","sz":"2","n":4}]}}"#,
            )
            .unwrap()
            .expect("a quote");
        match event {
            MarketEvent::Quote { symbol, quote } => {
                assert_eq!(symbol, SymbolId(0));
                assert_eq!(quote.bid_px, 94990.5);
                assert_eq!(quote.bid_qty, 1.5);
                assert_eq!(quote.ask_px, 95010.5);
                assert_eq!(quote.ask_qty, 2.0);
                assert_eq!(quote.venue_ts_ms, 1700);
            }
            other => panic!("expected a quote, got {other:?}"),
        }
    }

    #[test]
    fn a_half_empty_book_is_not_a_quote() {
        // A missing side means nothing is resting there. Reading it as a zero
        // would put a bid of nothing into the engine's picture, and anything
        // pricing against it would cross the whole book.
        let mut w = worker();
        let empty = w
            .decode(r#"{"channel":"bbo","data":{"coin":"BTC","time":1,"bbo":[null,null]}}"#)
            .unwrap();
        assert!(empty.is_none());
        let one_sided = w
            .decode(
                r#"{"channel":"bbo","data":{"coin":"BTC","time":1,
                   "bbo":[{"px":"1","sz":"1","n":1},null]}}"#,
            )
            .unwrap();
        assert!(one_sided.is_none());
    }

    #[test]
    fn an_asset_context_becomes_a_ticker() {
        let mut w = worker();
        let event = w
            .decode(
                r#"{"channel":"activeAssetCtx","data":{"coin":"ETH","ctx":{
                   "funding":"0.0000125","openInterest":"1000","prevDayPx":"3000",
                   "dayNtlVlm":"5000000","premium":"0.0001","oraclePx":"3010.5",
                   "markPx":"3011.0","midPx":"3010.75"}}}"#,
            )
            .unwrap()
            .expect("a ticker");
        match event {
            MarketEvent::Ticker { symbol, ticker } => {
                assert_eq!(symbol, SymbolId(1));
                assert_eq!(ticker.mark_px, 3011.0);
                assert_eq!(ticker.index_px, 3010.5);
                assert_eq!(ticker.last_px, 3010.75);
                // Reported exactly as the venue states it: an HOURLY rate.
                assert_eq!(ticker.funding_rate, 0.0000125);
                assert!(ticker.next_funding_ms > 0);
            }
            other => panic!("expected a ticker, got {other:?}"),
        }
    }

    #[test]
    fn the_next_funding_is_the_next_whole_hour() {
        // Funding pays hourly here, so the settlement a strategy waits for is
        // the top of the next hour and not eight hours out.
        assert_eq!(next_funding_ms(3_600_000), 7_200_000);
        assert_eq!(next_funding_ms(3_600_001), 7_200_000);
        assert_eq!(next_funding_ms(7_199_999), 7_200_000);
        assert_eq!(next_funding_ms(0), 0);
        // Always ahead of now, never level with it.
        for now in [1_700_000_000_000i64, 1_700_000_000_001, 1_699_999_999_999] {
            assert!(next_funding_ms(now) > now, "{now}");
            assert!(next_funding_ms(now) - now <= FUNDING_INTERVAL_MS);
        }
    }

    #[test]
    fn a_symbol_this_feed_does_not_follow_is_not_an_event() {
        let mut w = worker();
        assert!(w
            .decode(r#"{"channel":"bbo","data":{"coin":"DOGE","time":1,
                       "bbo":[{"px":"1","sz":"1","n":1},{"px":"2","sz":"1","n":1}]}}"#)
            .unwrap()
            .is_none());
    }

    #[test]
    fn a_refused_subscription_is_a_transport_failure_so_the_socket_is_redialled() {
        let mut w = worker();
        assert!(matches!(
            w.decode(r#"{"channel":"error","data":"Invalid coin"}"#),
            Err(FeedError::Transport(_))
        ));
        // A pong and a subscription acknowledgement are neither events nor
        // errors.
        assert!(w.decode(r#"{"channel":"pong"}"#).unwrap().is_none());
        assert!(w
            .decode(r#"{"channel":"subscriptionResponse","data":{}}"#)
            .unwrap()
            .is_none());
    }

    #[test]
    fn one_symbol_asked_for_twice_is_subscribed_once() {
        let mut feed = HyperliquidPublicFeed::with_url(
            "ws://127.0.0.1:1",
            &[Subscription { symbol: "BTCUSDT".into(), feed: Feed::Quote }],
        );
        assert_eq!(feed.subs.len(), 1);
        feed.admit("BTCUSDT", Feed::Quote);
        assert_eq!(feed.subs.len(), 1, "the same subscription was sent twice");
        // A different feed on the same coin is a second subscription.
        feed.admit("BTCUSDT", Feed::Ticker);
        assert_eq!(feed.subs.len(), 2);
    }
}
