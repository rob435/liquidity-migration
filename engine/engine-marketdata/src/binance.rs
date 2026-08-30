//! Binance USD-M futures public market data.
//!
//! Binance now routes fast book data and regular market data through separate
//! websocket paths. One worker owns both sockets: `/public` carries the touch
//! and complete top-20 partial-depth snapshots; `/market` carries trades, last
//! price, mark, index and funding. This feed does not build the engine's full
//! 50-level capacity through the separate diff-depth bootstrap. If either side
//! drops the pair reconnects and emits one reset, so a strategy never joins
//! fresh values from one route to stale values from the other.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_types::{
    BookLevel, Depth, Feed, FeedError, MarketEvent, MarketFeed, Quote, Subscription, SymbolId,
    Ticker, TradeFlow, BOOK_DEPTH,
};
use engine_venue::BinanceRealm;
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

const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(8);
const HEALTHY_AFTER: Duration = Duration::from_secs(30);
const QUEUE_DEPTH: usize = 4096;
/// Public sockets accept ten client messages a second. Five subscription
/// batches leave room for the pongs that share that same allowance.
const DYNAMIC_SUBSCRIPTION_GAP: Duration = Duration::from_millis(200);

pub struct BinancePublicFeed {
    realm: BinanceRealm,
    symbols: Vec<String>,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    inbox: Option<mpsc::Receiver<Result<MarketEvent, FeedError>>>,
    admissions: Option<mpsc::UnboundedSender<String>>,
    worker: Option<JoinHandle<()>>,
}

impl BinancePublicFeed {
    pub fn new(realm: BinanceRealm, subs: &[Subscription]) -> Self {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        let mut symbols = Vec::new();
        for sub in subs {
            let symbol = sub.symbol.to_uppercase();
            intern(&ids, &symbol);
            if !symbols.contains(&symbol) {
                symbols.push(symbol);
            }
        }
        Self {
            realm,
            symbols,
            ids,
            inbox: None,
            admissions: None,
            worker: None,
        }
    }

    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(&symbol.to_uppercase()).copied()
    }

    pub fn admit(&mut self, symbol: &str, _feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let id = intern(&self.ids, &symbol);
        if !self.symbols.contains(&symbol) {
            self.symbols.push(symbol.clone());
            if let Some(tx) = &self.admissions {
                let _ = tx.send(symbol);
            }
        }
        id
    }

    fn start(&mut self) {
        let (events, inbox) = mpsc::channel(QUEUE_DEPTH);
        let (admit_tx, admit_rx) = mpsc::unbounded_channel();
        let worker = Worker {
            realm: self.realm,
            symbols: self.symbols.clone(),
            admissions: admit_rx,
            decoder: Decoder::new(Arc::clone(&self.ids)),
            backoff: Duration::ZERO,
            connected_before: false,
            request_id: 1,
        };
        self.inbox = Some(inbox);
        self.admissions = Some(admit_tx);
        self.worker = Some(tokio::spawn(worker.run(events)));
    }
}

impl Drop for BinancePublicFeed {
    fn drop(&mut self) {
        if let Some(worker) = self.worker.take() {
            worker.abort();
        }
    }
}

impl MarketFeed for BinancePublicFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self.inbox.is_none() {
            self.start();
        }
        match self.inbox.as_mut() {
            Some(inbox) => inbox.recv().await.unwrap_or(Err(FeedError::Closed)),
            None => Err(FeedError::Closed),
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        Some(BinancePublicFeed::admit(self, symbol, feed))
    }
}

struct Worker {
    realm: BinanceRealm,
    symbols: Vec<String>,
    admissions: mpsc::UnboundedReceiver<String>,
    decoder: Decoder,
    backoff: Duration,
    connected_before: bool,
    request_id: u64,
}

impl Worker {
    async fn run(mut self, events: mpsc::Sender<Result<MarketEvent, FeedError>>) {
        loop {
            match self.connect().await {
                Ok((mut public, mut market)) => {
                    let opened = Instant::now();
                    if self.connected_before
                        && events
                            .send(Ok(MarketEvent::FeedReset {
                                recv_ns: engine_types::clock::mono_ns(),
                            }))
                            .await
                            .is_err()
                    {
                        return;
                    }
                    self.connected_before = true;
                    if self.pump(&mut public, &mut market, &events).await.is_err() {
                        return;
                    }
                    if opened.elapsed() >= HEALTHY_AFTER {
                        self.backoff = Duration::ZERO;
                    }
                }
                Err(error) => {
                    warn!(%error, "binance market feed did not come up; trying again");
                    if events.send(Err(error)).await.is_err() {
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

    async fn connect(&mut self) -> Result<(Socket, Socket), FeedError> {
        install_crypto_provider();
        self.decoder.reset();
        let base = self.realm.websocket();
        let public_url = format!("{base}/public/ws");
        let market_url = format!("{base}/market/ws");
        let (public, market) =
            tokio::try_join!(connect_async(public_url), connect_async(market_url))
                .map_err(|error| FeedError::Transport(error.to_string()))?;
        let (mut public, _) = public;
        let (mut market, _) = market;
        let symbols = self.symbols.clone();
        self.subscribe(&mut public, &mut market, &symbols).await?;
        info!(symbols = symbols.len(), "binance market feed subscribed");
        Ok((public, market))
    }

    async fn subscribe(
        &mut self,
        public: &mut Socket,
        market: &mut Socket,
        symbols: &[String],
    ) -> Result<(), FeedError> {
        if symbols.is_empty() {
            return Ok(());
        }
        let (public_streams, market_streams) = subscription_streams(symbols);
        self.send_subscription(public, public_streams).await?;
        self.send_subscription(market, market_streams).await
    }

    async fn send_subscription(
        &mut self,
        socket: &mut Socket,
        streams: Vec<String>,
    ) -> Result<(), FeedError> {
        let id = self.request_id;
        self.request_id = self.request_id.wrapping_add(1).max(1);
        let frame = json!({"method": "SUBSCRIBE", "params": streams, "id": id});
        socket
            .send(Message::Text(frame.to_string().into()))
            .await
            .map_err(|error| FeedError::Transport(error.to_string()))
    }

    async fn pump(
        &mut self,
        public: &mut Socket,
        market: &mut Socket,
        events: &mpsc::Sender<Result<MarketEvent, FeedError>>,
    ) -> Result<(), ()> {
        let mut pending_symbols = Vec::new();
        let mut next_subscription = tokio::time::Instant::now();
        loop {
            tokio::select! {
                Some(symbol) = self.admissions.recv() => {
                    if !self.symbols.contains(&symbol) {
                        self.symbols.push(symbol.clone());
                        pending_symbols.push(symbol);
                    }
                }
                _ = tokio::time::sleep_until(next_subscription), if !pending_symbols.is_empty() => {
                    let symbols = std::mem::take(&mut pending_symbols);
                    if self.subscribe(public, market, &symbols).await.is_err() {
                        return Ok(());
                    }
                    next_subscription = tokio::time::Instant::now() + DYNAMIC_SUBSCRIPTION_GAP;
                }
                message = public.next() => {
                    if self.handle_message(message, public, events).await? {
                        return Ok(());
                    }
                }
                message = market.next() => {
                    if self.handle_message(message, market, events).await? {
                        return Ok(());
                    }
                }
            }
        }
    }

    /// True means this socket ended and both routed sockets must redial.
    async fn handle_message(
        &mut self,
        message: Option<Result<Message, tokio_tungstenite::tungstenite::Error>>,
        socket: &mut Socket,
        events: &mpsc::Sender<Result<MarketEvent, FeedError>>,
    ) -> Result<bool, ()> {
        let Some(message) = message else {
            return Ok(true);
        };
        let message = match message {
            Ok(message) => message,
            Err(_) => return Ok(true),
        };
        let text = match message {
            Message::Text(text) => text.to_string(),
            Message::Ping(payload) => {
                let _ = socket.send(Message::Pong(payload)).await;
                return Ok(false);
            }
            Message::Close(_) => return Ok(true),
            _ => return Ok(false),
        };
        let recv_ns = engine_types::clock::mono_ns();
        match self.decoder.decode(&text, recv_ns) {
            Ok(decoded) => {
                for event in decoded {
                    if events.send(Ok(event)).await.is_err() {
                        return Err(());
                    }
                }
                Ok(false)
            }
            Err(error) => {
                if events.send(Err(error)).await.is_err() {
                    return Err(());
                }
                Ok(true)
            }
        }
    }
}

fn subscription_streams(symbols: &[String]) -> (Vec<String>, Vec<String>) {
    let mut public = Vec::with_capacity(symbols.len() * 2);
    let mut market = Vec::with_capacity(symbols.len() * 3);
    for symbol in symbols {
        let symbol = symbol.to_ascii_lowercase();
        public.push(format!("{symbol}@bookTicker"));
        public.push(format!("{symbol}@depth20@100ms"));
        market.push(format!("{symbol}@aggTrade"));
        market.push(format!("{symbol}@ticker"));
        market.push(format!("{symbol}@markPrice@1s"));
    }
    (public, market)
}

#[derive(Copy, Clone, Default)]
struct TickerParts {
    last_px: Option<f64>,
    mark_px: Option<f64>,
    index_px: Option<f64>,
    funding_rate: Option<f64>,
    next_funding_ms: Option<i64>,
    venue_ts_ms: i64,
}

struct Decoder {
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    quote_seq: HashMap<SymbolId, u64>,
    depth_seq: HashMap<SymbolId, u64>,
    ticker: HashMap<SymbolId, TickerParts>,
}

impl Decoder {
    fn new(ids: Arc<RwLock<HashMap<String, SymbolId>>>) -> Self {
        Self {
            ids,
            quote_seq: HashMap::new(),
            depth_seq: HashMap::new(),
            ticker: HashMap::new(),
        }
    }

    fn reset(&mut self) {
        self.quote_seq.clear();
        self.depth_seq.clear();
        self.ticker.clear();
    }

    fn decode(&mut self, text: &str, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let outer: Value = serde_json::from_str(text)
            .map_err(|error| FeedError::BadMessage(format!("{error}: {}", first_chars(text))))?;
        if outer.get("result").is_some() {
            return Ok(Vec::new());
        }
        if let Some(code) = outer.get("code") {
            return Err(FeedError::Transport(format!(
                "venue refused a market subscription ({code}): {}",
                outer
                    .get("msg")
                    .and_then(Value::as_str)
                    .unwrap_or("no reason")
            )));
        }
        let frame = outer.get("data").unwrap_or(&outer);
        let kind = frame.get("e").and_then(Value::as_str);
        match kind {
            Some("bookTicker") => self.book_ticker(frame, recv_ns),
            Some("depthUpdate") => self.depth(frame, recv_ns),
            Some("aggTrade") => self.trade(frame, recv_ns),
            Some("24hrTicker") => self.last_price(frame, recv_ns),
            Some("markPriceUpdate") => self.mark_price(frame, recv_ns),
            None if frame.get("u").is_some()
                && frame.get("s").is_some()
                && frame.get("b").and_then(Value::as_str).is_some() =>
            {
                self.book_ticker(frame, recv_ns)
            }
            _ => Ok(Vec::new()),
        }
    }

    fn symbol(&self, frame: &Value) -> Option<SymbolId> {
        let symbol = frame.get("s").and_then(Value::as_str)?.to_uppercase();
        self.ids.read().ok()?.get(&symbol).copied()
    }

    fn book_ticker(&mut self, frame: &Value, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let Some(symbol) = self.symbol(frame) else {
            return Ok(Vec::new());
        };
        let seq = positive_u64(frame, "u")?;
        if self
            .quote_seq
            .get(&symbol)
            .is_some_and(|prior| seq <= *prior)
        {
            return Ok(Vec::new());
        }
        self.quote_seq.insert(symbol, seq);
        let quote = Quote {
            bid_px: positive_num(frame, "b")?,
            bid_qty: nonnegative_num(frame, "B")?,
            ask_px: positive_num(frame, "a")?,
            ask_qty: nonnegative_num(frame, "A")?,
            venue_ts_ms: frame
                .get("T")
                .or_else(|| frame.get("E"))
                .and_then(Value::as_i64)
                .unwrap_or(0),
            recv_ns,
            seq,
        };
        Ok(vec![MarketEvent::Quote { symbol, quote }])
    }

    fn depth(&mut self, frame: &Value, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let Some(symbol) = self.symbol(frame) else {
            return Ok(Vec::new());
        };
        let seq = positive_u64(frame, "u")?;
        if let Some(prior) = self.depth_seq.get(&symbol) {
            if seq == *prior {
                return Ok(Vec::new());
            }
            if seq < *prior {
                return Err(FeedError::BadMessage(format!(
                    "a partial-depth snapshot regressed from update {prior} to {seq}"
                )));
            }
        }
        let mut depth = Depth {
            update_id: seq,
            seq,
            venue_ts_ms: frame
                .get("T")
                .or_else(|| frame.get("E"))
                .and_then(Value::as_i64)
                .unwrap_or(0),
            recv_ns,
            ..Depth::default()
        };
        depth.bid_len = parse_levels(frame, "b", &mut depth.bids)?;
        depth.ask_len = parse_levels(frame, "a", &mut depth.asks)?;
        self.depth_seq.insert(symbol, seq);
        Ok(vec![MarketEvent::Depth { symbol, depth }])
    }

    fn trade(&self, frame: &Value, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let Some(symbol) = self.symbol(frame) else {
            return Ok(Vec::new());
        };
        let qty = positive_num(frame, "q")?;
        let buyer_maker = frame
            .get("m")
            .and_then(Value::as_bool)
            .ok_or_else(|| FeedError::BadMessage("an aggregate trade has no maker side".into()))?;
        let first = positive_u64(frame, "f")?;
        let last = positive_u64(frame, "l")?;
        if last < first {
            return Err(FeedError::BadMessage(
                "an aggregate trade ends before it begins".into(),
            ));
        }
        let trade_count = u16::try_from(last - first + 1).unwrap_or(u16::MAX);
        Ok(vec![MarketEvent::Trades {
            symbol,
            trades: TradeFlow {
                buy_qty: if buyer_maker { 0.0 } else { qty },
                sell_qty: if buyer_maker { qty } else { 0.0 },
                last_px: positive_num(frame, "p")?,
                trade_count,
                seq: positive_u64(frame, "a")?,
                venue_ts_ms: frame.get("T").and_then(Value::as_i64).unwrap_or(0),
                recv_ns,
            },
        }])
    }

    fn last_price(&mut self, frame: &Value, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let Some(symbol) = self.symbol(frame) else {
            return Ok(Vec::new());
        };
        let held = self.ticker.entry(symbol).or_default();
        held.last_px = Some(positive_num(frame, "c")?);
        held.venue_ts_ms = frame.get("E").and_then(Value::as_i64).unwrap_or(0);
        Ok(ready_ticker(symbol, *held, recv_ns).into_iter().collect())
    }

    fn mark_price(&mut self, frame: &Value, recv_ns: u64) -> Result<Vec<MarketEvent>, FeedError> {
        let Some(symbol) = self.symbol(frame) else {
            return Ok(Vec::new());
        };
        let held = self.ticker.entry(symbol).or_default();
        held.mark_px = Some(positive_num(frame, "p")?);
        held.index_px = Some(positive_num(frame, "i")?);
        held.funding_rate = Some(number(frame, "r")?);
        held.next_funding_ms = frame.get("T").and_then(Value::as_i64);
        held.venue_ts_ms = frame.get("E").and_then(Value::as_i64).unwrap_or(0);
        Ok(ready_ticker(symbol, *held, recv_ns).into_iter().collect())
    }
}

fn ready_ticker(symbol: SymbolId, held: TickerParts, recv_ns: u64) -> Option<MarketEvent> {
    Some(MarketEvent::Ticker {
        symbol,
        ticker: Ticker {
            last_px: held.last_px?,
            mark_px: held.mark_px?,
            index_px: held.index_px?,
            funding_rate: held.funding_rate?,
            next_funding_ms: held.next_funding_ms?,
            venue_ts_ms: held.venue_ts_ms,
            recv_ns,
        },
    })
}

fn parse_levels(
    frame: &Value,
    name: &str,
    out: &mut [BookLevel; BOOK_DEPTH],
) -> Result<u8, FeedError> {
    let rows = frame
        .get(name)
        .and_then(Value::as_array)
        .ok_or_else(|| FeedError::BadMessage(format!("a depth update has no {name} levels")))?;
    let mut len = 0usize;
    for row in rows.iter().take(BOOK_DEPTH) {
        let pair = row
            .as_array()
            .filter(|pair| pair.len() >= 2)
            .ok_or_else(|| {
                FeedError::BadMessage("a depth level is not a price-size pair".into())
            })?;
        let px = value_num(&pair[0])?;
        let qty = value_num(&pair[1])?;
        if px <= 0.0 || qty < 0.0 {
            return Err(FeedError::BadMessage(
                "a depth level has an invalid price or size".into(),
            ));
        }
        out[len] = BookLevel { px, qty };
        len += 1;
    }
    Ok(len as u8)
}

fn positive_u64(frame: &Value, name: &str) -> Result<u64, FeedError> {
    frame
        .get(name)
        .and_then(|value| match value {
            Value::Number(number) => number.as_u64(),
            Value::String(text) => text.parse().ok(),
            _ => None,
        })
        .filter(|value| *value > 0)
        .ok_or_else(|| FeedError::BadMessage(format!("market message has no positive {name}")))
}

fn number(frame: &Value, name: &str) -> Result<f64, FeedError> {
    frame
        .get(name)
        .ok_or_else(|| FeedError::BadMessage(format!("market message has no {name}")))
        .and_then(value_num)
}

fn positive_num(frame: &Value, name: &str) -> Result<f64, FeedError> {
    number(frame, name).and_then(|value| {
        if value > 0.0 {
            Ok(value)
        } else {
            Err(FeedError::BadMessage(format!(
                "market message has non-positive {name}"
            )))
        }
    })
}

fn nonnegative_num(frame: &Value, name: &str) -> Result<f64, FeedError> {
    number(frame, name).and_then(|value| {
        if value >= 0.0 {
            Ok(value)
        } else {
            Err(FeedError::BadMessage(format!(
                "market message has negative {name}"
            )))
        }
    })
}

fn value_num(value: &Value) -> Result<f64, FeedError> {
    let parsed = match value {
        Value::String(text) => text.parse().ok(),
        Value::Number(number) => number.as_f64(),
        _ => None,
    };
    parsed
        .filter(|value| value.is_finite())
        .ok_or_else(|| FeedError::BadMessage("market number is unreadable".into()))
}

fn first_chars(text: &str) -> String {
    text.chars().take(160).collect()
}

fn install_crypto_provider() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decoder() -> Decoder {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        intern(&ids, "BTCUSDT");
        Decoder::new(ids)
    }

    #[test]
    fn streams_are_split_between_the_two_current_routes() {
        let (public, market) = subscription_streams(&["BTCUSDT".into()]);
        assert_eq!(public, ["btcusdt@bookTicker", "btcusdt@depth20@100ms"]);
        assert_eq!(
            market,
            ["btcusdt@aggTrade", "btcusdt@ticker", "btcusdt@markPrice@1s"]
        );
    }

    #[test]
    fn book_ticker_is_a_complete_touch_and_old_updates_do_not_go_backwards() {
        let mut d = decoder();
        let text = r#"{"e":"bookTicker","u":400900217,"E":1602839590076,
            "T":1602839590068,"s":"BTCUSDT","b":"25000.1","B":"3.2",
            "a":"25000.2","A":"1.4"}"#;
        let events = d.decode(text, 9).unwrap();
        assert!(matches!(
            events.as_slice(),
            [MarketEvent::Quote { symbol: SymbolId(0), quote }]
                if quote.bid_px == 25000.1 && quote.ask_qty == 1.4 && quote.seq == 400900217
        ));
        assert!(d.decode(text, 10).unwrap().is_empty());
    }

    #[test]
    fn partial_depth_is_published_as_the_complete_top_twenty_snapshot() {
        let mut d = decoder();
        let events = d
            .decode(
                r#"{"e":"depthUpdate","E":1591269992453,"T":1591269992451,
               "s":"BTCUSDT","U":157,"u":160,"pu":149,
               "b":[["25000.1","1.2"],["25000.0","2.3"]],
               "a":[["25000.2","0.4"]]}"#,
                11,
            )
            .unwrap();
        match events.as_slice() {
            [MarketEvent::Depth { depth, .. }] => {
                assert_eq!(depth.update_id, 160);
                assert_eq!(depth.bid_len, 2);
                assert_eq!(depth.ask_len, 1);
                assert_eq!(
                    depth.bids[0],
                    BookLevel {
                        px: 25000.1,
                        qty: 1.2
                    }
                );
            }
            other => panic!("expected depth, got {other:?}"),
        }
    }

    #[test]
    fn partial_depth_ignores_duplicates_and_refuses_regression_until_reset() {
        let mut d = decoder();
        let at_160 = r#"{"e":"depthUpdate","E":1,"s":"BTCUSDT","u":160,
            "b":[["25000.1","1.2"]],"a":[["25000.2","0.4"]]}"#;
        let at_159 = r#"{"e":"depthUpdate","E":2,"s":"BTCUSDT","u":159,
            "b":[["25000.0","1.1"]],"a":[["25000.3","0.5"]]}"#;
        assert_eq!(d.decode(at_160, 1).unwrap().len(), 1);
        assert!(d.decode(at_160, 2).unwrap().is_empty());
        assert!(matches!(d.decode(at_159, 3), Err(FeedError::BadMessage(_))));

        d.reset();
        assert_eq!(d.decode(at_159, 4).unwrap().len(), 1);
    }

    #[test]
    fn aggregate_trade_preserves_aggressor_side_and_count() {
        let mut d = decoder();
        let events = d
            .decode(
                r#"{"e":"aggTrade","E":123456789,"s":"BTCUSDT","a":5933014,
               "p":"25000.5","q":"0.2","f":100,"l":102,"T":123456785,"m":true}"#,
                12,
            )
            .unwrap();
        assert!(matches!(
            events.as_slice(),
            [MarketEvent::Trades { trades, .. }]
                if trades.buy_qty == 0.0 && trades.sell_qty == 0.2 && trades.trade_count == 3
        ));
    }

    #[test]
    fn ticker_waits_until_last_and_mark_halves_have_both_arrived() {
        let mut d = decoder();
        assert!(d
            .decode(
                r#"{"e":"24hrTicker","E":123456789,"s":"BTCUSDT","c":"25001.0"}"#,
                13,
            )
            .unwrap()
            .is_empty());
        let events = d
            .decode(
                r#"{"e":"markPriceUpdate","E":1562305380000,"s":"BTCUSDT",
               "p":"25000.8","i":"24999.9","r":"0.00010000","T":1562306400000}"#,
                14,
            )
            .unwrap();
        assert!(matches!(
            events.as_slice(),
            [MarketEvent::Ticker { ticker, .. }]
                if ticker.last_px == 25001.0 && ticker.mark_px == 25000.8
                    && ticker.index_px == 24999.9 && ticker.funding_rate == 0.0001
                    && ticker.next_funding_ms == 1562306400000
        ));
    }

    #[test]
    fn a_subscription_refusal_is_not_silently_ignored() {
        let mut d = decoder();
        assert!(matches!(
            d.decode(r#"{"code":2,"msg":"Invalid request"}"#, 1),
            Err(FeedError::Transport(_))
        ));
    }
}
