//! Lighter public market data.
//!
//! One socket, one channel per market: `order_book/{index}` carries the resting
//! bids and asks, and the top of each side is the touch this feed reports. The
//! subscription names a market by its index, so the venue's market list is read
//! once — without credentials, since prices are public — to turn `BTCUSDT` into
//! that number.
//!
//! **The channel is a book, not a quote.** It sends the whole ladder once and
//! only the levels that move after it, so this feed keeps the ladder: a change
//! message's first entry is the lowest-numbered price that moved rather than
//! the best one, and a level the venue removes arrives with a size of zero.
//! Reading either as the touch publishes a price nobody is quoting. Changes
//! that arrive before the venue's own snapshot are dropped rather than treated
//! as a book of their own.
//!
//! **There is no ticker channel here.** The venue publishes no mark price,
//! index price or funding rate on this socket, so no [`Ticker`] is ever
//! emitted: a strategy that needs funding cannot get it from this venue's
//! stream, and an invented zero would read as a real rate of zero — which for
//! a carry sleeve is the difference between "no edge" and "no data".
//!
//! The socket lives in its own task, for the same reason every other feed here
//! does: the engine drops `next_event`'s future several times a second, and a
//! dial held inside it would start over from nothing every tick.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Quote, Subscription, SymbolId};
use engine_venue::venues::lighter::markets::{engine_symbol, Market};
use engine_venue::venues::lighter::public;
use engine_venue::LighterRealm;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tracing::{info, warn};

/// The venue drops a socket that has said nothing for two minutes.
/// How long the venue has to send anything at all after a keep-alive before
/// the socket counts as dead. The Bybit feed's number.
const PONG_TIMEOUT: Duration = Duration::from_secs(10);

const PING_INTERVAL: Duration = Duration::from_secs(45);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
const QUEUE_DEPTH: usize = 4096;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

pub struct LighterPublicFeed {
    realm: LighterRealm,
    url: Option<String>,
    wanted: Vec<String>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    admissions: Option<mpsc::UnboundedSender<String>>,
    inbox: Option<Inbox>,
}

struct Inbox {
    events: mpsc::Receiver<Result<MarketEvent, FeedError>>,
    worker: JoinHandle<()>,
}

impl LighterPublicFeed {
    pub fn new(realm: LighterRealm, subs: &[Subscription]) -> Self {
        Self::build(realm, None, subs)
    }

    /// Point the feed at a local server. Tests only.
    pub fn for_test(url: &str, subs: &[Subscription]) -> Self {
        Self::build(LighterRealm::Testnet, Some(url.to_string()), subs)
    }

    fn build(realm: LighterRealm, url: Option<String>, subs: &[Subscription]) -> Self {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        let mut wanted = Vec::new();
        for sub in subs {
            let symbol = sub.symbol.to_uppercase();
            intern(&ids, &symbol);
            if !wanted.contains(&symbol) {
                wanted.push(symbol);
            }
        }
        LighterPublicFeed {
            realm,
            url,
            wanted,
            ids,
            admissions: None,
            inbox: None,
        }
    }

    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(&symbol.to_uppercase()).copied()
    }

    pub fn admit(&mut self, symbol: &str, _feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        if !self.wanted.contains(&symbol) {
            self.wanted.push(symbol.clone());
            if let Some(tx) = &self.admissions {
                let _ = tx.send(symbol.clone());
            }
        }
        intern(&self.ids, &symbol)
    }

    fn start(&mut self) {
        let (events, inbox) = mpsc::channel(QUEUE_DEPTH);
        let (admit_tx, admit_rx) = mpsc::unbounded_channel();
        self.admissions = Some(admit_tx);
        let worker = Worker {
            realm: self.realm,
            url: self
                .url
                .clone()
                .unwrap_or_else(|| self.realm.websocket().to_string()),
            wanted: self.wanted.clone(),
            ids: self.ids.clone(),
            admissions: admit_rx,
            markets: HashMap::new(),
            backoff: Duration::ZERO,
            connected_before: false,
            books: HashMap::new(),
        };
        let handle = tokio::spawn(worker.run(events));
        self.inbox = Some(Inbox {
            events: inbox,
            worker: handle,
        });
    }
}

impl Drop for LighterPublicFeed {
    fn drop(&mut self) {
        if let Some(inbox) = &self.inbox {
            inbox.worker.abort();
        }
    }
}

impl MarketFeed for LighterPublicFeed {
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
        Some(LighterPublicFeed::admit(self, symbol, feed))
    }
}

struct Worker {
    realm: LighterRealm,
    url: String,
    wanted: Vec<String>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    admissions: mpsc::UnboundedReceiver<String>,
    /// Market index to the engine's symbol, built from the venue's own list.
    markets: HashMap<i16, String>,
    backoff: Duration,
    connected_before: bool,
    /// One book per market. The venue sends a snapshot once and changed levels
    /// after it, so the touch cannot be read off a single message: a delta's
    /// first entry is the lowest-numbered price that changed, not the best
    /// one, and a level it deletes arrives with a size of zero.
    books: HashMap<i16, Book>,
}

/// Both sides of one market, keyed by price. Positive `f64` bit patterns sort
/// the same way the numbers do, which is what makes the touch the first or
/// last entry rather than a scan.
#[derive(Default)]
struct Book {
    /// Set by the venue's snapshot. Until it arrives the book is unknown, and
    /// a quote built from deltas alone would be a fiction.
    started: bool,
    bids: BTreeMap<u64, f64>,
    asks: BTreeMap<u64, f64>,
}

impl Book {
    fn apply(&mut self, side: &Value, name: &str, replace: bool) {
        let book = if name == "bids" { &mut self.bids } else { &mut self.asks };
        if replace {
            book.clear();
        }
        let Some(levels) = side.get(name).and_then(Value::as_array) else { return };
        for level in levels {
            let Some(px) = level.get("price").and_then(Value::as_str).and_then(|p| p.parse::<f64>().ok())
            else {
                continue;
            };
            let size = level
                .get("size")
                .and_then(Value::as_str)
                .and_then(|q| q.parse::<f64>().ok())
                .unwrap_or(0.0);
            if !px.is_finite() || px <= 0.0 {
                continue;
            }
            // A size of zero is the venue deleting the level, not a level with
            // nothing on it.
            if size > 0.0 {
                book.insert(px.to_bits(), size);
            } else {
                book.remove(&px.to_bits());
            }
        }
    }

    fn touch(&self) -> Option<((f64, f64), (f64, f64))> {
        if !self.started {
            return None;
        }
        let (bid_bits, bid_qty) = self.bids.iter().next_back()?;
        let (ask_bits, ask_qty) = self.asks.iter().next()?;
        Some((
            (f64::from_bits(*bid_bits), *bid_qty),
            (f64::from_bits(*ask_bits), *ask_qty),
        ))
    }
}

impl Worker {
    async fn run(mut self, events: mpsc::Sender<Result<MarketEvent, FeedError>>) {
        loop {
            match self.connect().await {
                Ok(mut socket) => {
                    let opened = Instant::now();
                    if self.connected_before {
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
                    warn!(error = %e, "lighter market feed did not come up; trying again");
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

    /// The venue's market list, re-read on every dial: a market listed since
    /// the last connection is one a book may already be naming.
    async fn load_markets(&mut self) -> Result<Vec<Market>, FeedError> {
        public::markets(self.realm)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))
    }

    async fn connect(&mut self) -> Result<Socket, FeedError> {
        install_crypto_provider();
        let listed = self.load_markets().await?;
        self.markets = listed
            .iter()
            .map(|market| (market.index, engine_symbol(&market.symbol)))
            .collect();

        let (mut socket, _) = connect_async(self.url.as_str())
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        let wanted = self.wanted.clone();
        for symbol in &wanted {
            self.subscribe(&mut socket, symbol).await?;
        }
        info!(markets = self.wanted.len(), "lighter market feed subscribed");
        Ok(socket)
    }

    async fn subscribe(&self, socket: &mut Socket, symbol: &str) -> Result<(), FeedError> {
        let Some(index) = self.index_of(symbol) else {
            // A symbol the venue does not list is not an error to die on: the
            // engine may be following it on another venue's book, or it may be
            // listed later.
            warn!(symbol, "this venue lists no such market; not subscribing");
            return Ok(());
        };
        let payload = json!({"type": "subscribe", "channel": format!("order_book/{index}")});
        socket
            .send(Message::text(payload.to_string()))
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))
    }

    fn index_of(&self, symbol: &str) -> Option<i16> {
        self.markets
            .iter()
            .find(|(_, name)| name.as_str() == symbol)
            .map(|(index, _)| *index)
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
                    let Some(symbol) = fresh else { return Err(Gone) };
                    if !self.wanted.contains(&symbol) {
                        self.wanted.push(symbol.clone());
                    }
                    if self.subscribe(socket, &symbol).await.is_err() {
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
                            warn!("lighter market feed closed; reconnecting");
                            return Ok(());
                        }
                        Some(Ok(_)) => (),
                        Some(Err(e)) => {
                            warn!(error = %e, "lighter market feed dropped; reconnecting");
                            return Ok(());
                        }
                    }
                }
                _ = tokio::time::sleep_until(deadline) => {
                    if pong_due.is_some_and(|due| Instant::now() >= due) {
                        warn!("lighter market feed stopped answering; reconnecting");
                        return Ok(());
                    }
                    if socket.send(Message::text(r#"{"type":"ping"}"#)).await.is_err() {
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
        let channel = frame.get("channel").and_then(Value::as_str).unwrap_or_default();
        let Some(index) = channel.strip_prefix("order_book:").and_then(|n| n.parse::<i16>().ok())
        else {
            if frame.get("type").and_then(Value::as_str) == Some("error") {
                let why = frame.get("message").and_then(Value::as_str).unwrap_or("no reason");
                return Err(FeedError::Transport(format!(
                    "venue refused a market subscription: {why}"
                )));
            }
            return Ok(None);
        };
        let Some(symbol) = self.markets.get(&index) else { return Ok(None) };
        let Some(id) = self.id_of(symbol) else { return Ok(None) };
        let Some(rows) = frame.get("order_book") else { return Ok(None) };

        // A snapshot replaces the book; anything else changes it. Reading the
        // first level of either as the touch was the bug: on a change message
        // that entry is only the lowest-numbered price that moved.
        let snapshot = frame
            .get("type")
            .and_then(Value::as_str)
            .is_some_and(|kind| kind.starts_with("subscribed"));
        let held = self.books.entry(index).or_default();
        if snapshot {
            held.started = true;
        }
        if !held.started {
            // Changes before the venue's snapshot: nothing to change.
            return Ok(None);
        }
        held.apply(rows, "bids", snapshot);
        held.apply(rows, "asks", snapshot);

        // Both sides, or nothing. A one-sided book read as a quote would put a
        // bid of zero into the engine's picture, and anything pricing against
        // it would cross the whole book.
        let Some((bid, ask)) = held.touch() else { return Ok(None) };
        Ok(Some(MarketEvent::Quote {
            symbol: id,
            quote: Quote {
                bid_px: bid.0,
                bid_qty: bid.1,
                ask_px: ask.0,
                ask_qty: ask.1,
                venue_ts_ms: frame.get("timestamp").and_then(Value::as_i64).unwrap_or(0),
                recv_ns: engine_types::clock::mono_ns(),
                // The venue numbers its updates, and the number is worth
                // carrying: a gap in it is a gap in the book.
                seq: frame
                    .get("offset")
                    .and_then(Value::as_u64)
                    .unwrap_or(0),
            },
        }))
    }

    fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(symbol).copied()
    }
}

struct Gone;



fn intern(ids: &Arc<RwLock<HashMap<String, SymbolId>>>, symbol: &str) -> SymbolId {
    let mut ids = ids.write().expect("the symbol map lock is poisoned");
    if let Some(id) = ids.get(symbol) {
        return *id;
    }
    let id = SymbolId(u16::try_from(ids.len()).expect("more than 65535 symbols"));
    ids.insert(symbol.to_string(), id);
    id
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
            realm: LighterRealm::Testnet,
            url: "ws://127.0.0.1:1".to_string(),
            wanted: vec!["BTCUSDT".to_string()],
            ids,
            admissions: rx,
            markets: HashMap::from([(0i16, "BTCUSDT".to_string()), (1i16, "ETHUSDT".to_string())]),
            backoff: Duration::ZERO,
            connected_before: false,
            books: HashMap::new(),
        }
    }

    #[test]
    fn a_book_update_becomes_a_quote() {
        let mut w = worker();
        let event = w
            .decode(
                r#"{"channel":"order_book:0","type":"subscribed/order_book",
                   "timestamp":1774884082326,"offset":1558300,
                   "order_book":{"code":0,
                     "asks":[{"price":"2064.54","size":"0.3285"},{"price":"2064.60","size":"1"}],
                     "bids":[{"price":"2064.10","size":"0.5"}]}}"#,
            )
            .unwrap()
            .expect("a quote");
        match event {
            MarketEvent::Quote { symbol, quote } => {
                assert_eq!(symbol, SymbolId(0));
                assert_eq!(quote.bid_px, 2064.10);
                assert_eq!(quote.bid_qty, 0.5);
                assert_eq!(quote.ask_px, 2064.54, "the first ask is the touch");
                assert_eq!(quote.ask_qty, 0.3285);
                assert_eq!(quote.venue_ts_ms, 1774884082326);
                assert_eq!(quote.seq, 1558300);
            }
            other => panic!("expected a quote, got {other:?}"),
        }
    }

    #[test]
    fn a_change_is_merged_into_the_book_rather_than_read_as_one() {
        // The venue sends the book once and only the levels that moved after
        // it. A change message's first entry is the lowest-numbered price that
        // changed, not the best one — reading it as the touch published a
        // price nobody was quoting.
        let mut w = worker();
        w.decode(
            r#"{"channel":"order_book:0","type":"subscribed/order_book","timestamp":1,
               "order_book":{
                 "bids":[{"price":"100.0","size":"1"},{"price":"99.0","size":"2"}],
                 "asks":[{"price":"101.0","size":"1"},{"price":"102.0","size":"2"}]}}"#,
        )
        .unwrap()
        .expect("the snapshot is a quote");

        // A change deep in the book leaves the touch where it was.
        let event = w
            .decode(
                r#"{"channel":"order_book:0","type":"update/order_book","timestamp":2,
                   "order_book":{"bids":[{"price":"98.0","size":"5"}],"asks":[]}}"#,
            )
            .unwrap()
            .expect("a quote");
        let MarketEvent::Quote { quote, .. } = event else { panic!("expected a quote") };
        assert_eq!(quote.bid_px, 100.0, "a deeper bid was read as the touch");
        assert_eq!(quote.ask_px, 101.0);

        // A size of zero deletes the level; the next one down becomes the
        // touch. Published as a level of no size it would have been a bid of
        // one hundred that nobody was making.
        let event = w
            .decode(
                r#"{"channel":"order_book:0","type":"update/order_book","timestamp":3,
                   "order_book":{"bids":[{"price":"100.0","size":"0"}],"asks":[]}}"#,
            )
            .unwrap()
            .expect("a quote");
        let MarketEvent::Quote { quote, .. } = event else { panic!("expected a quote") };
        assert_eq!(quote.bid_px, 99.0, "a deleted level stayed on the book");
        assert_eq!(quote.bid_qty, 2.0);
    }

    #[test]
    fn a_change_before_the_venues_book_is_not_a_quote() {
        // Nothing to change yet, and a quote built out of changes alone is a
        // book this feed invented.
        let mut w = worker();
        assert!(w
            .decode(
                r#"{"channel":"order_book:0","type":"update/order_book","timestamp":1,
                   "order_book":{"bids":[{"price":"1","size":"1"}],
                   "asks":[{"price":"2","size":"1"}]}}"#
            )
            .unwrap()
            .is_none());
    }

    #[test]
    fn a_one_sided_book_is_not_a_quote() {
        let mut w = worker();
        assert!(w
            .decode(
                r#"{"channel":"order_book:0","type":"subscribed/order_book","timestamp":1,
                   "order_book":{"asks":[],"bids":[{"price":"1","size":"1"}]}}"#
            )
            .unwrap()
            .is_none());
    }

    #[test]
    fn a_market_this_feed_does_not_follow_is_not_an_event() {
        // ETH is a market the venue lists, but no strategy asked for it.
        let mut w = worker();
        assert!(w
            .decode(
                r#"{"channel":"order_book:1","type":"subscribed/order_book","timestamp":1,
                   "order_book":{"asks":[{"price":"2","size":"1"}],
                   "bids":[{"price":"1","size":"1"}]}}"#
            )
            .unwrap()
            .is_some(), "ETH has an id, so it is deliverable");
        // A market number the venue never listed is not.
        assert!(w
            .decode(
                r#"{"channel":"order_book:99","type":"subscribed/order_book","timestamp":1,
                   "order_book":{"asks":[{"price":"2","size":"1"}],
                   "bids":[{"price":"1","size":"1"}]}}"#
            )
            .unwrap()
            .is_none());
    }

    #[test]
    fn no_ticker_is_ever_emitted_because_the_venue_publishes_none() {
        // Stated as a test so the absence is a decision rather than an
        // oversight: an invented funding rate of zero reads as a real one.
        let mut w = worker();
        let event = w
            .decode(
                r#"{"channel":"order_book:0","type":"subscribed/order_book","timestamp":1,
                   "order_book":{"asks":[{"price":"2","size":"1"}],
                   "bids":[{"price":"1","size":"1"}]}}"#,
            )
            .unwrap()
            .unwrap();
        assert!(matches!(event, MarketEvent::Quote { .. }));
    }

    #[test]
    fn a_refused_subscription_is_a_transport_failure_so_the_socket_is_redialled() {
        let mut w = worker();
        assert!(matches!(
            w.decode(r#"{"type":"error","message":"unknown channel"}"#),
            Err(FeedError::Transport(_))
        ));
        assert!(w.decode(r#"{"type":"connected"}"#).unwrap().is_none());
    }

    #[test]
    fn a_symbol_admitted_later_gets_an_id() {
        let mut feed = LighterPublicFeed::for_test(
            "ws://127.0.0.1:1",
            &[Subscription { symbol: "BTCUSDT".into(), feed: Feed::Quote }],
        );
        assert_eq!(feed.id_of("BTCUSDT"), Some(SymbolId(0)));
        assert_eq!(feed.admit("ETHUSDT", Feed::Quote), SymbolId(1));
        assert_eq!(feed.admit("ETHUSDT", Feed::Quote), SymbolId(1));
    }
}
