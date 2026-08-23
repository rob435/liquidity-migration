//! MEXC public market data.
//!
//! One socket, one subscription per symbol: `sub.ticker` carries the touch
//! (`bid1`/`ask1`) and the mark, index and funding rate in the same message, so
//! a quote and a ticker come out of one push and the two can never disagree
//! about when they were seen.
//!
//! **Why not the depth channel.** MEXC turned zipped push on by default for
//! `sub.depth` and `sub.deal`, so those arrive gzip-compressed; the ticker
//! channel does not. Reading a compressed frame as text is the fastest way to
//! publish a price nobody quoted, and the ticker channel gives the touch
//! without that risk. What it costs is size: the ticker states no quantity, so
//! [`Quote::bid_qty`] and `ask_qty` are zero — "not stated", rather than a
//! depth that was made up. A strategy sizing against book depth cannot use
//! this venue's feed until the depth channel is decompressed.
//!
//! **The funding rate here is the eight-hourly one**, like Bybit's and unlike
//! Hyperliquid's hourly figure — the venue settles on a `collectCycle` of 8.
//! It is reported as the venue states it.
//!
//! The socket lives in its own task, for the same reason every other feed here
//! does: the engine drops `next_event`'s future several times a second inside a
//! `select!`, and a dial or a backoff sleep held inside it would start over
//! from nothing every tick.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Quote, Subscription, SymbolId, Ticker};
use engine_venue::venues::mexc::public;
use engine_venue::MexcRealm;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tracing::{info, warn};

use crate::symbols::intern;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// The venue closes a connection it has not heard from in a minute, and asks
/// for a ping every 10-20 seconds.
const PING_INTERVAL: Duration = Duration::from_secs(15);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
const QUEUE_DEPTH: usize = 4096;

/// Eight hours, the venue's settlement cycle for every contract it lists.
const FUNDING_INTERVAL_MS: i64 = 8 * 60 * 60 * 1000;

pub struct MexcPublicFeed {
    realm: MexcRealm,
    subs: Vec<Subscription>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    inbox: Option<mpsc::Receiver<Result<MarketEvent, FeedError>>>,
    admissions: Option<mpsc::UnboundedSender<Vec<Subscription>>>,
    worker: Option<JoinHandle<()>>,
}

impl MexcPublicFeed {
    pub fn new(realm: MexcRealm, subs: &[Subscription]) -> Self {
        let mut feed = MexcPublicFeed {
            realm,
            subs: Vec::new(),
            ids: Arc::new(RwLock::new(HashMap::new())),
            inbox: None,
            admissions: None,
            worker: None,
        };
        for sub in subs {
            feed.remember(sub.clone());
        }
        feed
    }

    /// The id this feed hands out for a symbol, if it follows it.
    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(&symbol.to_uppercase()).copied()
    }

    pub fn admit(&mut self, symbol: &str, feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let sub = Subscription { symbol: symbol.clone(), feed };
        if self.remember(sub.clone()) {
            if let Some(tx) = &self.admissions {
                let _ = tx.send(vec![sub]);
            }
        }
        intern(&self.ids, &symbol)
    }

    /// True when this subscription is new. One channel serves both the quote
    /// and the ticker, so a symbol is asked for once however many feed kinds
    /// the engine wants from it.
    fn remember(&mut self, sub: Subscription) -> bool {
        intern(&self.ids, &sub.symbol);
        if self.subs.iter().any(|held| held.symbol == sub.symbol) {
            self.subs.push(sub);
            return false;
        }
        self.subs.push(sub);
        true
    }

    fn start(&mut self) {
        let (events, inbox) = mpsc::channel(QUEUE_DEPTH);
        let (admit_tx, admit_rx) = mpsc::unbounded_channel();
        let worker = Worker {
            realm: self.realm,
            wanted: unique_symbols(&self.subs),
            ids: Arc::clone(&self.ids),
            admissions: admit_rx,
            venue_symbols: HashMap::new(),
            backoff: Duration::ZERO,
            connected_before: false,
        };
        self.inbox = Some(inbox);
        self.admissions = Some(admit_tx);
        self.worker = Some(tokio::spawn(worker.run(events)));
    }
}

impl Drop for MexcPublicFeed {
    fn drop(&mut self) {
        if let Some(worker) = self.worker.take() {
            worker.abort();
        }
    }
}

impl MarketFeed for MexcPublicFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        // The worker is spawned here rather than in the constructor: building
        // a feed is not necessarily done inside a tokio runtime, and reading
        // from one always is.
        if self.inbox.is_none() {
            self.start();
        }
        match self.inbox.as_mut() {
            Some(inbox) => inbox.recv().await.unwrap_or(Err(FeedError::Closed)),
            None => Err(FeedError::Closed),
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        Some(MexcPublicFeed::admit(self, symbol, feed))
    }
}

fn unique_symbols(subs: &[Subscription]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for sub in subs {
        if !out.contains(&sub.symbol) {
            out.push(sub.symbol.clone());
        }
    }
    out
}

struct Worker {
    realm: MexcRealm,
    /// The engine's spellings, e.g. `BTCUSDT`.
    wanted: Vec<String>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    admissions: mpsc::UnboundedReceiver<Vec<Subscription>>,
    /// The engine's spelling to the venue's, read from the venue itself.
    venue_symbols: HashMap<String, String>,
    backoff: Duration,
    connected_before: bool,
}

impl Worker {
    async fn run(mut self, events: mpsc::Sender<Result<MarketEvent, FeedError>>) {
        loop {
            match self.connect().await {
                Ok(mut socket) => {
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
                    self.backoff = Duration::ZERO;
                    if self.pump(&mut socket, &events).await.is_err() {
                        return;
                    }
                }
                Err(e) => {
                    warn!(error = %e, "mexc market feed did not come up; trying again");
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
        // The venue's own symbol list, public and keyless. Read on every
        // reconnect so a contract listed while the socket was down is
        // subscribable without restarting the engine.
        let pairs = public::symbol_map(self.realm)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        self.venue_symbols = pairs.into_iter().collect();
        let (mut socket, _) = connect_async(self.realm.websocket())
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        let wanted = self.wanted.clone();
        self.subscribe(&mut socket, &wanted).await?;
        info!(symbols = self.wanted.len(), "mexc market feed subscribed");
        Ok(socket)
    }

    async fn subscribe(&self, socket: &mut Socket, symbols: &[String]) -> Result<(), FeedError> {
        for symbol in symbols {
            let Some(venue_symbol) = self.venue_symbols.get(symbol) else {
                // Named but not listed by the venue. Said once, and the rest
                // of the subscription still goes out — one unknown symbol is
                // not a reason to have no prices at all.
                warn!(symbol, "mexc lists no contract for this symbol; not subscribed");
                continue;
            };
            let frame = json!({"method": "sub.ticker", "param": {"symbol": venue_symbol}});
            socket
                .send(Message::Text(frame.to_string().into()))
                .await
                .map_err(|e| FeedError::Transport(e.to_string()))?;
        }
        Ok(())
    }

    async fn pump(
        &mut self,
        socket: &mut Socket,
        events: &mpsc::Sender<Result<MarketEvent, FeedError>>,
    ) -> Result<(), ()> {
        let mut ping = tokio::time::interval(PING_INTERVAL);
        ping.tick().await;
        loop {
            tokio::select! {
                _ = ping.tick() => {
                    // The venue wants an application-level ping, not a
                    // protocol frame, and closes a connection it has not heard
                    // from in a minute.
                    let frame = json!({"method": "ping"}).to_string();
                    if socket.send(Message::Text(frame.into())).await.is_err() {
                        return Ok(());
                    }
                }
                Some(fresh) = self.admissions.recv() => {
                    let names = unique_symbols(&fresh);
                    for name in &names {
                        if !self.wanted.contains(name) {
                            self.wanted.push(name.clone());
                        }
                    }
                    if self.subscribe(socket, &names).await.is_err() {
                        return Ok(());
                    }
                }
                message = socket.next() => {
                    let Some(message) = message else { return Ok(()) };
                    let Ok(message) = message else { return Ok(()) };
                    let text = match message {
                        Message::Text(text) => text.to_string(),
                        // Every channel this feed subscribes to is text. A
                        // binary frame here is a compressed channel nobody
                        // asked for; reading it as text would publish noise.
                        Message::Binary(_) => continue,
                        Message::Close(_) => return Ok(()),
                        _ => continue,
                    };
                    let recv_ns = engine_types::clock::mono_ns();
                    for event in self.decode(&text, recv_ns) {
                        if events.send(Ok(event)).await.is_err() {
                            return Err(());
                        }
                    }
                }
            }
        }
    }

    /// One `push.ticker` becomes a quote and a ticker, both stamped with the
    /// same arrival time.
    fn decode(&self, text: &str, recv_ns: u64) -> Vec<MarketEvent> {
        let Ok(frame) = serde_json::from_str::<Value>(text) else { return Vec::new() };
        if frame.get("channel").and_then(Value::as_str) != Some("push.ticker") {
            return Vec::new();
        }
        let Some(data) = frame.get("data") else { return Vec::new() };
        let Some(venue_symbol) = data.get("symbol").and_then(Value::as_str) else {
            return Vec::new();
        };
        let Some(symbol) = self.engine_symbol(venue_symbol) else { return Vec::new() };
        let Some(id) = self.ids.read().ok().and_then(|ids| ids.get(&symbol).copied()) else {
            return Vec::new();
        };
        let venue_ts_ms = data.get("timestamp").and_then(Value::as_i64).unwrap_or(0);
        let num = |name: &str| data.get(name).and_then(Value::as_f64);

        let mut out = Vec::with_capacity(2);
        // Either side absent means nothing is resting there; a zeroed price
        // would read as a real one, so a half-empty book is not a quote.
        if let (Some(bid_px), Some(ask_px)) = (num("bid1"), num("ask1")) {
            if bid_px > 0.0 && ask_px > 0.0 {
                out.push(MarketEvent::Quote {
                    symbol: id,
                    quote: Quote {
                        bid_px,
                        ask_px,
                        // The ticker channel states no size. Zero is "not
                        // stated" rather than a depth invented here.
                        bid_qty: 0.0,
                        ask_qty: 0.0,
                        venue_ts_ms,
                        recv_ns,
                        // The ticker carries no sequence. Zero is "not
                        // stated"; nothing here claims continuity it cannot
                        // see. The depth channel does number its pushes.
                        seq: 0,
                    },
                });
            }
        }
        if let Some(funding_rate) = num("fundingRate") {
            out.push(MarketEvent::Ticker {
                symbol: id,
                ticker: Ticker {
                    last_px: num("lastPrice").unwrap_or(0.0),
                    // MEXC's "fair price" is the mark the venue liquidates
                    // against; "index price" is the outside reference.
                    mark_px: num("fairPrice").unwrap_or(0.0),
                    index_px: num("indexPrice").unwrap_or(0.0),
                    funding_rate,
                    next_funding_ms: next_funding_ms(venue_ts_ms),
                    venue_ts_ms,
                    recv_ns,
                },
            });
        }
        out
    }

    fn engine_symbol(&self, venue_symbol: &str) -> Option<String> {
        self.venue_symbols
            .iter()
            .find(|(_, venue)| venue.as_str() == venue_symbol)
            .map(|(engine, _)| engine.clone())
    }
}

/// The next eight-hourly settlement at or after this stamp.
///
/// The ticker channel does not carry a settlement time — only the REST funding
/// endpoint does — so it is derived from the cycle the venue publishes for
/// every contract it lists. Eight hours from the epoch lands on 00:00, 08:00
/// and 16:00 UTC, which is where MEXC settles.
fn next_funding_ms(venue_ts_ms: i64) -> i64 {
    if venue_ts_ms <= 0 {
        return 0;
    }
    (venue_ts_ms / FUNDING_INTERVAL_MS + 1) * FUNDING_INTERVAL_MS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn worker() -> Worker {
        let (_tx, rx) = mpsc::unbounded_channel();
        let ids = Arc::new(RwLock::new(HashMap::new()));
        intern(&ids, "BTCUSDT");
        Worker {
            realm: MexcRealm::Mainnet,
            wanted: vec!["BTCUSDT".to_string()],
            ids,
            admissions: rx,
            venue_symbols: HashMap::from([("BTCUSDT".to_string(), "BTC_USDT".to_string())]),
            backoff: Duration::ZERO,
            connected_before: false,
        }
    }

    /// Recorded from the venue's own `push.ticker` example, with a real
    /// timestamp put on it.
    const PUSH: &str = r#"{"channel":"push.ticker","data":{"symbol":"BTC_USDT",
        "ask1":6866.5,"bid1":6865,"contractId":1,"fairPrice":6867.4,"fundingRate":0.0008,
        "indexPrice":6861.6,"lastPrice":6865.5,"timestamp":1787492334852},"ts":1787492334853}"#;

    #[test]
    fn one_push_becomes_both_a_quote_and_a_ticker() {
        let out = worker().decode(PUSH, 42);
        assert_eq!(out.len(), 2);
        match out[0] {
            MarketEvent::Quote { symbol, quote } => {
                assert_eq!(symbol, SymbolId(0));
                assert_eq!(quote.bid_px, 6865.0);
                assert_eq!(quote.ask_px, 6866.5);
                assert_eq!(quote.recv_ns, 42);
                // The channel states no size, and none is invented.
                assert_eq!(quote.bid_qty, 0.0);
                assert_eq!(quote.ask_qty, 0.0);
                assert_eq!(quote.seq, 0);
            }
            ref other => panic!("{other:?}"),
        }
        match out[1] {
            MarketEvent::Ticker { ticker, .. } => {
                assert_eq!(ticker.mark_px, 6867.4, "fairPrice is the mark");
                assert_eq!(ticker.index_px, 6861.6);
                assert_eq!(ticker.funding_rate, 0.0008);
                assert_eq!(ticker.last_px, 6865.5);
            }
            ref other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_frame_for_another_channel_or_another_symbol_is_ignored() {
        let w = worker();
        assert!(w.decode(r#"{"channel":"pong","data":1787492334852}"#, 1).is_empty());
        assert!(w
            .decode(r#"{"channel":"push.ticker","data":{"symbol":"ETH_USDT","bid1":1,"ask1":2}}"#, 1)
            .is_empty());
        assert!(w.decode("not json", 1).is_empty());
    }

    #[test]
    fn a_half_empty_book_is_not_a_quote() {
        // A zeroed side would read as a real price at zero, which is a price
        // nobody is quoting.
        let w = worker();
        let one_sided = r#"{"channel":"push.ticker","data":{"symbol":"BTC_USDT","bid1":0,
            "ask1":6866.5,"fundingRate":0.0008,"timestamp":1}}"#;
        let out = w.decode(one_sided, 1);
        assert!(
            out.iter().all(|e| !matches!(e, MarketEvent::Quote { .. })),
            "a one-sided book was published as a quote"
        );
        // The ticker still comes through: funding is not a book.
        assert_eq!(out.len(), 1);
    }

    #[test]
    fn the_funding_stamp_lands_on_the_venues_eight_hourly_settlement() {
        // MEXC settles every 8 hours, at 00:00, 08:00 and 16:00 UTC.
        // 1787492334852 ms is inside one of those cycles.
        let next = next_funding_ms(1787492334852);
        assert_eq!(next % FUNDING_INTERVAL_MS, 0, "not on an eight-hour boundary");
        assert!(next > 1787492334852);
        assert!(next - 1787492334852 <= FUNDING_INTERVAL_MS);
        // A venue stamp we did not get is not a settlement time we can invent.
        assert_eq!(next_funding_ms(0), 0);
    }

    #[test]
    fn a_symbol_is_subscribed_once_however_many_feed_kinds_want_it() {
        let subs = vec![
            Subscription { symbol: "BTCUSDT".into(), feed: Feed::Quote },
            Subscription { symbol: "BTCUSDT".into(), feed: Feed::Ticker },
            Subscription { symbol: "ETHUSDT".into(), feed: Feed::Quote },
        ];
        assert_eq!(unique_symbols(&subs), vec!["BTCUSDT", "ETHUSDT"]);
    }
}
