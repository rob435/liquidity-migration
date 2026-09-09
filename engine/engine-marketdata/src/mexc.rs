//! MEXC public market data.
//!
//! One socket, two subscriptions per symbol. `sub.ticker` carries a complete
//! touch (`bid1`/`ask1`) plus mark, index and funding. Unmerged `sub.depth`
//! carries a version on every book change, so missed packets are visible even
//! though quote prices still come from the complete ticker snapshot.
//!
//! The depth subscription explicitly sets `compress:false`. Here that means
//! no event merging: every next version must equal the prior version plus one.
//! A gap clears prices and redials. Depth levels are incremental and require a
//! REST snapshot to form a complete ladder, so they are not passed off as one;
//! [`Quote::bid_qty`] and `ask_qty` remain zero — "not stated".
//!
//! **Funding here is per contract, not venue-wide.** The venue publishes a
//! `collectCycle` per contract and 8 h, 4 h, 1 h and 24 h are all live, so the
//! rate on one symbol is not comparable with the rate on another by period.
//! It is reported as the venue states it and never rescaled.
//! [`Ticker::next_funding_ms`] is the venue's own next settlement for that
//! contract: `push.ticker` carries the rate but neither a settlement time nor
//! a cycle, so the schedule is read from `contract/funding_rate` on connect and
//! re-read hourly, and the venue's stamp is rolled forward by that contract's
//! cycle in between. A symbol the funding page did not state carries
//! `next_funding_ms = 0` — "not stated", the same convention [`Quote::bid_qty`]
//! uses above. A reader must not treat 0 as a settlement time, and there is no
//! venue-wide cycle to fall back on.
//!
//! The socket lives in its own task, for the same reason every other feed here
//! does: the engine drops `next_event`'s future several times a second inside a
//! `select!`, and a dial or a backoff sleep held inside it would start over
//! from nothing every tick.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_public::venues::mexc::public::{self, FundingSchedule};
use engine_public::MexcRealm;
use engine_types::{
    Feed, FeedError, MarketEvent, MarketFeed, Quote, Subscription, SymbolId, Ticker,
};
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

/// How often the settlement schedule is re-read. One hour is the shortest
/// `collectCycle` the venue lists, so every contract's stamp stays within one
/// of its own cycles of the venue's; between reads the cached stamp rolls
/// forward by that cycle, which needs no read at all. Newly listed contracts
/// and a cycle the venue changed arrive on this cadence.
const FUNDING_REFRESH: Duration = Duration::from_secs(60 * 60);

pub struct MexcPublicFeed {
    realm: MexcRealm,
    subs: Vec<Subscription>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    inbox: Option<mpsc::Receiver<Result<MarketEvent, FeedError>>>,
    admissions: Option<mpsc::UnboundedSender<Vec<Subscription>>>,
    worker: Option<JoinHandle<()>>,
    pending_reset: bool,
}

impl MexcPublicFeed {
    pub fn new(realm: MexcRealm, subs: &[Subscription]) -> Self {
        let mut feed = MexcPublicFeed {
            realm,
            subs: Vec::new(),
            ids: Arc::new(RwLock::new(HashMap::new())),
            inbox: None,
            pending_reset: false,
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
        crate::symbols::resolve(&self.ids, &symbol.to_uppercase())
    }

    pub fn admit(&mut self, symbol: &str, feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let sub = Subscription {
            symbol: symbol.clone(),
            feed,
        };
        if self.remember(sub.clone()) {
            if let Some(tx) = &self.admissions {
                let _ = tx.send(vec![sub]);
            }
        }
        intern(&self.ids, &symbol)
    }

    /// True when this symbol is new. One ticker/depth pair serves both feed
    /// kinds, so a symbol is asked for once however many plugs want it.
    fn remember(&mut self, sub: Subscription) -> bool {
        intern(&self.ids, &sub.symbol);
        if self.subs.contains(&sub) {
            return false;
        }
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
        let (schedules_tx, schedules) = mpsc::channel(1);
        let worker = Worker {
            realm: self.realm,
            wanted: unique_symbols(&self.subs),
            ids: Arc::clone(&self.ids),
            admissions: admit_rx,
            venue_symbols: HashMap::new(),
            funding: HashMap::new(),
            schedules,
            schedules_tx,
            backoff: Duration::ZERO,
            connected_before: false,
            depth_versions: HashMap::new(),
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
        if self.pending_reset {
            self.pending_reset = false;
            return Ok(MarketEvent::FeedReset {
                recv_ns: engine_types::clock::mono_ns(),
            });
        }
        // The worker is spawned here rather than in the constructor: building
        // a feed is not necessarily done inside a tokio runtime, and reading
        // from one always is.
        if self.subs.is_empty() {
            return std::future::pending().await;
        }
        if self.inbox.is_none() {
            self.start();
        }
        match self.inbox.as_mut() {
            Some(inbox) => inbox.recv().await.unwrap_or(Err(FeedError::Closed)),
            None => Err(FeedError::Closed),
        }
    }

    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        let symbol = symbol.to_uppercase();
        let before = self.subs.len();
        self.subs
            .retain(|row| row.symbol != symbol || row.feed != feed);
        if self.subs.len() == before {
            return false;
        }
        if self.subs.iter().any(|row| row.symbol == symbol) {
            return true;
        }
        if let Some(worker) = self.worker.take() {
            worker.abort();
        }
        self.inbox = None;
        self.admissions = None;
        self.pending_reset = true;
        true
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
    /// The venue's stated settlement schedule, by the venue's symbol.
    funding: HashMap<String, FundingSchedule>,
    /// Refreshed schedules, delivered rather than awaited in place: a REST
    /// round trip held inside [`Worker::pump`]'s `select!` stalls every price
    /// on the socket for as long as it takes. Holding the sender here is what
    /// keeps the receiving branch from resolving on a closed channel.
    schedules: mpsc::Receiver<Vec<(String, FundingSchedule)>>,
    schedules_tx: mpsc::Sender<Vec<(String, FundingSchedule)>>,
    backoff: Duration,
    connected_before: bool,
    /// Latest unmerged depth version per symbol. It guards ticker-derived
    /// quotes; incremental depth levels alone are not a complete book.
    depth_versions: HashMap<SymbolId, u64>,
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
                    // Only a socket that stayed up earns the reset. Zeroing on
                    // connect instead lets a venue that accepts and drops be
                    // redialled with no wait, for ever.
                    if opened.elapsed() >= Duration::from_secs(30) {
                        self.backoff = Duration::ZERO;
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
        // The only endpoint that states a settlement time. Read here, where
        // there is no cached schedule to fall back on: coming up without one
        // would report "not stated" for every symbol on the socket.
        let schedule = public::funding_schedule(self.realm)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        self.funding = schedule.into_iter().collect();
        self.depth_versions.clear();
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
                warn!(
                    symbol,
                    "mexc lists no contract for this symbol; not subscribed"
                );
                continue;
            };
            for frame in subscription_frames(venue_symbol) {
                socket
                    .send(Message::Text(frame.to_string().into()))
                    .await
                    .map_err(|e| FeedError::Transport(e.to_string()))?;
            }
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
        let mut schedule_due = tokio::time::interval(FUNDING_REFRESH);
        // `connect` has just read it.
        schedule_due.tick().await;
        loop {
            tokio::select! {
                _ = schedule_due.tick() => {
                    let realm = self.realm;
                    let schedules = self.schedules_tx.clone();
                    tokio::spawn(async move {
                        match public::funding_schedule(realm).await {
                            Ok(rows) => {
                                let _ = schedules.send(rows).await;
                            }
                            Err(e) => warn!(
                                error = %e,
                                "mexc settlement schedule did not refresh; keeping the last one"
                            ),
                        }
                    });
                }
                Some(rows) = self.schedules.recv() => {
                    self.funding = rows.into_iter().collect();
                }
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
                        // Every documented channel this feed subscribes to is
                        // JSON text. `compress` controls event merging rather
                        // than websocket payload encoding, so a binary frame
                        // is neither one of our subscriptions nor safe to
                        // reinterpret as text.
                        Message::Binary(_) => continue,
                        Message::Close(_) => return Ok(()),
                        _ => continue,
                    };
                    let recv_ns = engine_types::clock::mono_ns();
                    match self.decode(&text, recv_ns) {
                        Ok(decoded) => {
                            for event in decoded {
                                if events.send(Ok(event)).await.is_err() {
                                    return Err(());
                                }
                            }
                        }
                        Err(e) => {
                            let reset = MarketEvent::FeedReset { recv_ns };
                            if events.send(Ok(reset)).await.is_err() {
                                return Err(());
                            }
                            if events.send(Err(e)).await.is_err() {
                                return Err(());
                            }
                            self.connected_before = false;
                            return Ok(());
                        }
                    }
                }
            }
        }
    }

    fn decode(&mut self, text: &str, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let frame = serde_json::from_str::<Value>(text)
            .map_err(|e| FeedError::BadMessage(format!("{e}: {}", first_chars(text))))?;
        match frame.get("channel").and_then(Value::as_str) {
            Some("push.depth") => {
                self.accept_depth_version(&frame)?;
                return Ok(Vec::new());
            }
            Some("push.ticker") => (),
            Some("rs.error") => {
                let why = frame
                    .get("data")
                    .and_then(Value::as_str)
                    .unwrap_or("no reason");
                return Err(FeedError::Transport(format!(
                    "venue refused a market subscription: {why}"
                )));
            }
            _ => return Ok(Vec::new()),
        }
        let Some(data) = frame.get("data") else {
            return Ok(Vec::new());
        };
        let Some(venue_symbol) = data.get("symbol").and_then(Value::as_str) else {
            return Ok(Vec::new());
        };
        let Some(symbol) = self.engine_symbol(venue_symbol) else {
            return Ok(Vec::new());
        };
        let Some(id) = self
            .ids
            .read()
            .ok()
            .and_then(|ids| ids.get(&symbol).copied())
        else {
            return Ok(Vec::new());
        };
        let venue_ts_ms = data.get("timestamp").and_then(Value::as_i64).unwrap_or(0);
        let num = |name: &str| data.get(name).and_then(Value::as_f64);

        let mut out = Vec::with_capacity(2);
        // Either side absent means nothing is resting there; a zeroed price
        // would read as a real one, so a half-empty book is not a quote.
        if let (Some(seq), Some(bid_px), Some(ask_px)) = (
            self.depth_versions.get(&id).copied(),
            num("bid1"),
            num("ask1"),
        ) {
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
                        // Latest depth version observed before this complete
                        // ticker snapshot. Its continuity is checked even
                        // though incremental depth levels are not published.
                        seq,
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
                    next_funding_ms: next_funding_ms(self.funding.get(venue_symbol), venue_ts_ms),
                    venue_ts_ms,
                    recv_ns,
                },
            });
        }
        Ok(out)
    }

    fn accept_depth_version(&mut self, frame: &Value) -> Result<(), FeedError> {
        let data = frame
            .get("data")
            .ok_or_else(|| FeedError::BadMessage("mexc depth push has no data".to_string()))?;
        let venue_symbol = frame
            .get("symbol")
            .and_then(Value::as_str)
            .or_else(|| data.get("symbol").and_then(Value::as_str))
            .ok_or_else(|| FeedError::BadMessage("mexc depth push has no symbol".to_string()))?;
        let Some(symbol) = self.engine_symbol(venue_symbol) else {
            return Ok(());
        };
        let Some(id) = self
            .ids
            .read()
            .ok()
            .and_then(|ids| ids.get(&symbol).copied())
        else {
            return Ok(());
        };
        let version = data
            .get("version")
            .and_then(Value::as_u64)
            .filter(|version| *version > 0)
            .ok_or_else(|| {
                FeedError::BadMessage(format!("mexc depth push for {symbol} has no version"))
            })?;

        if let Some(prior) = self.depth_versions.get(&id).copied() {
            if prior.checked_add(1) != Some(version) {
                self.depth_versions.clear();
                return Err(FeedError::BadMessage(format!(
                    "mexc depth continuity lost for {symbol}: version={version}, expected={}",
                    prior.saturating_add(1)
                )));
            }
        }
        self.depth_versions.insert(id, version);
        Ok(())
    }

    fn engine_symbol(&self, venue_symbol: &str) -> Option<String> {
        self.venue_symbols
            .iter()
            .find(|(_, venue)| venue.as_str() == venue_symbol)
            .map(|(engine, _)| engine.clone())
    }
}

fn subscription_frames(venue_symbol: &str) -> [Value; 2] {
    [
        json!({"method": "sub.ticker", "param": {"symbol": venue_symbol}}),
        json!({"method": "sub.depth", "param": {
            "symbol": venue_symbol, "compress": false
        }}),
    ]
}

fn first_chars(text: &str) -> String {
    text.chars().take(160).collect()
}

/// This contract's next settlement, after `venue_ts_ms`.
///
/// The venue's own stamp fixes the phase and its `collectCycle` the period.
/// Neither is derivable from the clock: settlement is not an epoch multiple of
/// the cycle — `US30_USDT` settles every 24 h at 16:00 UTC — and the cycle
/// differs between contracts. A contract with no schedule reports 0, "not
/// stated"; a guessed cycle would be wrong on about half the venue.
fn next_funding_ms(schedule: Option<&FundingSchedule>, venue_ts_ms: i64) -> i64 {
    let Some(schedule) = schedule else {
        return 0;
    };
    let cycle = schedule.collect_cycle_ms;
    let stated = schedule.next_settle_ms;
    if venue_ts_ms <= 0 || stated <= 0 || cycle <= 0 {
        return 0;
    }
    if stated > venue_ts_ms {
        return stated;
    }
    // The stamp has settled since it was read. The phase holds, so the next one
    // is a whole number of cycles on from it.
    let cycles = (venue_ts_ms - stated) / cycle + 1;
    stated.saturating_add(cycles.saturating_mul(cycle))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Recorded from `GET /api/v1/contract/funding_rate` on the realm's own
    /// host, no symbol: one real row at each cycle the venue lists. Real
    /// bytes, so a renamed field fails here rather than on a live settlement.
    const FUNDING: &str = r#"{"success":true,"code":0,"data":[
        {"symbol":"BTC_USDT","fundingRate":6.2e-05,"maxFundingRate":0.0018,
         "minFundingRate":-0.0018,"collectCycle":8,"nextSettleTime":1788969600000,
         "timestamp":1788950082772,"idxPrice":79129.8,"fairPrice":79090},
        {"symbol":"XAU_USDT","fundingRate":3.4e-05,"maxFundingRate":0.03,
         "minFundingRate":-0.03,"collectCycle":4,"nextSettleTime":1788955200000,
         "timestamp":1788950082772,"idxPrice":4397.34,"fairPrice":4399.08},
        {"symbol":"SOPH_USDT","fundingRate":-0.00013,"maxFundingRate":0.03,
         "minFundingRate":-0.03,"collectCycle":1,"nextSettleTime":1788951600000,
         "timestamp":1788950082772,"idxPrice":0.00543,"fairPrice":0.00542},
        {"symbol":"US30_USDT","fundingRate":0,"maxFundingRate":0,"minFundingRate":0,
         "collectCycle":24,"nextSettleTime":1788969600000,"timestamp":1788950082772,
         "idxPrice":52548.46,"fairPrice":52551.36}]}"#;

    /// The stamp the page above was read at.
    const READ_AT: i64 = 1_788_950_082_772;
    const HOUR: i64 = 60 * 60 * 1000;

    fn funding() -> HashMap<String, FundingSchedule> {
        public::parse_funding(FUNDING)
            .unwrap()
            .into_iter()
            .collect()
    }

    fn worker() -> Worker {
        let (_tx, rx) = mpsc::unbounded_channel();
        let (schedules_tx, schedules) = mpsc::channel(1);
        let ids = Arc::new(RwLock::new(HashMap::new()));
        intern(&ids, "BTCUSDT");
        Worker {
            realm: MexcRealm::Mainnet,
            wanted: vec!["BTCUSDT".to_string()],
            ids,
            admissions: rx,
            venue_symbols: HashMap::from([("BTCUSDT".to_string(), "BTC_USDT".to_string())]),
            funding: funding(),
            schedules,
            schedules_tx,
            backoff: Duration::ZERO,
            connected_before: false,
            depth_versions: HashMap::new(),
        }
    }

    /// Recorded from the venue's own `push.ticker` example, with a real
    /// timestamp put on it.
    const PUSH: &str = r#"{"channel":"push.ticker","data":{"symbol":"BTC_USDT",
        "ask1":6866.5,"bid1":6865,"contractId":1,"fairPrice":6867.4,"fundingRate":0.0008,
        "indexPrice":6861.6,"lastPrice":6865.5,"timestamp":1787492334852},"ts":1787492334853}"#;

    const DEPTH_10: &str = r#"{"channel":"push.depth","data":{"asks":[],"bids":[],
        "version":10},"symbol":"BTC_USDT","ts":1787492334851}"#;

    #[test]
    fn one_push_becomes_both_a_quote_and_a_ticker() {
        let mut worker = worker();
        worker.decode(DEPTH_10, 41).unwrap();
        let out = worker.decode(PUSH, 42).unwrap();
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
                assert_eq!(quote.seq, 10);
            }
            ref other => panic!("{other:?}"),
        }
        match out[1] {
            MarketEvent::Ticker { ticker, .. } => {
                assert_eq!(ticker.mark_px, 6867.4, "fairPrice is the mark");
                assert_eq!(ticker.index_px, 6861.6);
                assert_eq!(ticker.funding_rate, 0.0008);
                assert_eq!(ticker.last_px, 6865.5);
                // The venue's own stamp for BTC_USDT, looked up by the venue's
                // spelling of the symbol rather than the engine's.
                assert_eq!(ticker.next_funding_ms, 1_788_969_600_000);
            }
            ref other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_frame_for_another_channel_or_another_symbol_is_ignored() {
        let mut w = worker();
        assert!(w
            .decode(r#"{"channel":"pong","data":1787492334852}"#, 1)
            .unwrap()
            .is_empty());
        assert!(w
            .decode(
                r#"{"channel":"push.ticker","data":{"symbol":"ETH_USDT","bid1":1,"ask1":2}}"#,
                1
            )
            .unwrap()
            .is_empty());
        assert!(matches!(
            w.decode("not json", 1),
            Err(FeedError::BadMessage(_))
        ));
    }

    #[test]
    fn a_half_empty_book_is_not_a_quote() {
        // A zeroed side would read as a real price at zero, which is a price
        // nobody is quoting.
        let mut w = worker();
        w.decode(DEPTH_10, 0).unwrap();
        let one_sided = r#"{"channel":"push.ticker","data":{"symbol":"BTC_USDT","bid1":0,
            "ask1":6866.5,"fundingRate":0.0008,"timestamp":1}}"#;
        let out = w.decode(one_sided, 1).unwrap();
        assert!(
            out.iter().all(|e| !matches!(e, MarketEvent::Quote { .. })),
            "a one-sided book was published as a quote"
        );
        // The ticker still comes through: funding is not a book.
        assert_eq!(out.len(), 1);
    }

    #[test]
    fn a_depth_gap_resets_quote_admission_but_not_ticker_decoding() {
        let mut w = worker();
        w.decode(DEPTH_10, 1).unwrap();
        assert!(w
            .decode(PUSH, 2)
            .unwrap()
            .iter()
            .any(|event| matches!(event, MarketEvent::Quote { .. })));
        w.decode(
            r#"{"channel":"push.depth","data":{"asks":[],"bids":[],"version":11},
               "symbol":"BTC_USDT","ts":1787492334854}"#,
            3,
        )
        .unwrap();

        let gap = w.decode(
            r#"{"channel":"push.depth","data":{"asks":[],"bids":[],"version":13},
               "symbol":"BTC_USDT","ts":1787492334855}"#,
            4,
        );
        assert!(matches!(gap, Err(FeedError::BadMessage(_))));
        assert!(w.depth_versions.is_empty());

        let after = w.decode(PUSH, 5).unwrap();
        assert!(
            after
                .iter()
                .all(|event| !matches!(event, MarketEvent::Quote { .. })),
            "a quote was published without a new continuous depth epoch"
        );
        assert!(
            after
                .iter()
                .any(|event| matches!(event, MarketEvent::Ticker { .. })),
            "funding disappeared with the depth guard"
        );
    }

    #[test]
    fn the_depth_subscription_disables_event_merging() {
        let frames = subscription_frames("BTC_USDT");
        assert_eq!(frames[0]["method"], "sub.ticker");
        assert_eq!(frames[1]["method"], "sub.depth");
        assert_eq!(frames[1]["param"]["symbol"], "BTC_USDT");
        assert_eq!(frames[1]["param"]["compress"], false);
    }

    #[test]
    fn the_funding_stamp_is_the_venues_own_settlement_for_that_contract() {
        let schedules = funding();
        // A 4 h contract settles four hours before the 8 h ones do; the next
        // eight-hour epoch multiple, 1788969600000, is that much too late.
        assert_eq!(
            next_funding_ms(schedules.get("XAU_USDT"), READ_AT),
            1_788_955_200_000
        );
        assert_eq!(
            next_funding_ms(schedules.get("BTC_USDT"), READ_AT),
            1_788_969_600_000
        );
        // 24 h from the epoch lands on 00:00 UTC; this contract settles 16:00.
        let us30 = schedules.get("US30_USDT").copied().unwrap();
        assert_eq!(
            next_funding_ms(Some(&us30), READ_AT),
            1_788_969_600_000,
            "the venue's stamp, not an epoch multiple"
        );
        assert_ne!(us30.next_settle_ms % us30.collect_cycle_ms, 0);
    }

    #[test]
    fn a_contract_with_no_stated_schedule_reads_zero_rather_than_a_guessed_cycle() {
        let schedules = funding();
        // Not on the page: "not stated", the convention the depth guard uses.
        assert_eq!(next_funding_ms(schedules.get("ETH_USDT"), READ_AT), 0);
        assert_eq!(next_funding_ms(None, READ_AT), 0);
        // No venue stamp on the frame is not a settlement time either.
        assert_eq!(next_funding_ms(schedules.get("BTC_USDT"), 0), 0);
    }

    #[test]
    fn a_settled_stamp_rolls_forward_by_that_contracts_own_cycle() {
        let schedules = funding();
        let four_hourly = schedules.get("XAU_USDT");
        let stated = 1_788_955_200_000;
        // At the stamp itself funding has settled, so the next one is a cycle
        // on. Same one until it is reached.
        assert_eq!(next_funding_ms(four_hourly, stated), stated + 4 * HOUR);
        assert_eq!(next_funding_ms(four_hourly, stated + 1), stated + 4 * HOUR);
        assert_eq!(
            next_funding_ms(four_hourly, stated + 4 * HOUR - 1),
            stated + 4 * HOUR
        );
        assert_eq!(
            next_funding_ms(four_hourly, stated + 4 * HOUR),
            stated + 8 * HOUR
        );
        // A week of missed refreshes still lands on the venue's own phase.
        let late = stated + 7 * 24 * HOUR;
        let next = next_funding_ms(four_hourly, late);
        assert_eq!((next - stated) % (4 * HOUR), 0);
        assert!(next > late && next - late <= 4 * HOUR);
        // The hourly contract rolls by an hour, not by eight.
        assert_eq!(
            next_funding_ms(schedules.get("SOPH_USDT"), 1_788_951_600_000),
            1_788_951_600_000 + HOUR
        );
    }

    #[test]
    fn the_schedule_is_re_read_at_least_as_often_as_the_venue_settles() {
        // Between reads the stamp only rolls forward on the cached cycle, so a
        // cadence longer than the shortest cycle the venue lists would carry a
        // changed cycle past a settlement.
        let refresh = i64::try_from(FUNDING_REFRESH.as_millis()).unwrap();
        let shortest = funding()
            .values()
            .map(|schedule| schedule.collect_cycle_ms)
            .min()
            .unwrap();
        assert!(
            refresh <= shortest,
            "a {refresh} ms refresh is slower than the venue's {shortest} ms cycle"
        );
    }

    #[test]
    fn a_symbol_is_subscribed_once_however_many_feed_kinds_want_it() {
        let subs = vec![
            Subscription {
                symbol: "BTCUSDT".into(),
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
        ];
        assert_eq!(unique_symbols(&subs), vec!["BTCUSDT", "ETHUSDT"]);
    }
    #[test]
    fn repeated_admission_retains_one_demand_row_per_symbol_and_feed() {
        let mut feed = MexcPublicFeed::new(MexcRealm::Mainnet, &[]);
        for _ in 0..4096 {
            feed.admit("BTCUSDT", Feed::Quote);
        }
        assert_eq!(feed.subs.len(), 1);
        feed.admit("BTCUSDT", Feed::Ticker);
        assert_eq!(feed.subs.len(), 2);
        assert!(MarketFeed::retire(&mut feed, "BTCUSDT", Feed::Quote));
        assert_eq!(
            feed.subs,
            vec![Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Ticker
            }]
        );
    }
}

#[cfg(test)]
#[tokio::test(start_paused = true)]
async fn empty_demand_after_retirement_stays_idle_until_readmission() {
    use std::future::Future;
    let subs = [Subscription {
        symbol: "BTCUSDT".into(),
        feed: Feed::Quote,
    }];
    let mut feed = MexcPublicFeed::new(MexcRealm::Mainnet, &subs);
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
