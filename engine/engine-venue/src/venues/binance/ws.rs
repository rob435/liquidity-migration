//! Binance's private stream: order and fill updates for one account.
//!
//! The stream is entered by a `listenKey` — a token the REST side hands out
//! under the API key alone, no signature — and stays alive only while that
//! key does: the key expires an hour after it was last touched, so the
//! socket task keeps touching it, and a key that expires anyway surfaces as
//! a reconnect with a fresh one. The testnet carries this stream in full; the
//! realm remains blocked until its signed order lifecycle is observed.
//!
//! **The socket lives in its own task**, for the same reason Bybit's does:
//! the engine waits on this feed inside a `select!`, which drops the losing
//! branch's future every flush tick, so anything half-finished inside
//! `next_update` would be thrown away several times a second. The key
//! request, the dial, the keepalive schedule and the reconnect loop all
//! belong to a task nobody cancels, and `next_update` is nothing but a
//! channel receive.
//!
//! Ordinary orders use `ORDER_TRADE_UPDATE`. Conditional stops use
//! `ALGO_UPDATE`; when one triggers, its `ai` field names the ordinary order
//! that later carries the fill. The decoder remembers that link so the fill
//! arrives with the empty client id the engine reserves for a position stop.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_types::ids::{Symbol, SymbolId};
use engine_types::market::{FeedError, OrderFeed};
use engine_types::orders::{OrderAck, OrderUpdate};
use engine_types::VenueError;
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use super::parse::{id_text, is_exchange_or_stop_id, scoped_execution_id};
use super::realm::BinanceRealm;
use super::rest::RestClient;
use crate::creds::Credentials;
use crate::json::{num_field, opt_num_field, str_field};
use crate::mono_ns;

const PATH_LISTEN_KEY: &str = "/fapi/v1/listenKey";

/// A listen key lives 60 minutes past its last touch; touching it at half
/// that tolerates one whole missed round before anything expires.
const KEEPALIVE_EVERY: Duration = Duration::from_secs(30 * 60);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_CAP: Duration = Duration::from_secs(30);
/// A socket that lasted this long was a real connection, so the wait between
/// dials starts over.
const HEALTHY_AFTER: Duration = Duration::from_secs(30);
/// How many client order ids to remember so one order acks once.
const ACK_MEMORY: usize = 8192;
const BOOKKEEPING_MEMORY: usize = 8192;
const CHANNEL_DEPTH: usize = 1024;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;
type Handover = Result<OrderUpdate, FeedError>;

pub struct BinanceOrderFeed {
    rest_base: String,
    ws_base: String,
    creds: Credentials,
    ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>,
    updates: Option<mpsc::Receiver<Handover>>,
}

impl BinanceOrderFeed {
    /// The live feed: the realm's hosts, and the realm's credentials from
    /// the environment. Like the gateway, mainnet fails here unless the
    /// owner has armed `REAL_MONEY`.
    pub fn new(realm: BinanceRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        Ok(Self::build(
            realm.rest_base(),
            realm.websocket(),
            creds,
            symbols,
        ))
    }

    /// Point the feed at local servers. Tests only.
    pub fn for_test(
        rest_url: &str,
        ws_url: &str,
        creds: Credentials,
        symbols: Vec<Symbol>,
    ) -> Self {
        Self::build(rest_url, ws_url, creds, symbols)
    }

    fn build(rest_base: &str, ws_base: &str, creds: Credentials, symbols: Vec<Symbol>) -> Self {
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Self {
            rest_base: rest_base.to_string(),
            ws_base: ws_base.to_string(),
            creds,
            ids: Arc::new(RwLock::new(ids)),
            updates: None,
        }
    }

    fn start(&mut self) {
        let (tx, rx) = mpsc::channel(CHANNEL_DEPTH);
        let worker = Worker {
            rest: RestClient::new(self.rest_base.clone(), self.creds.clone()),
            ws_base: self.ws_base.clone(),
            decoder: Decoder::new(self.ids.clone()),
            backoff: Duration::ZERO,
        };
        tokio::spawn(worker.run(tx));
        self.updates = Some(rx);
    }
}

impl OrderFeed for BinanceOrderFeed {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        let mut ids = self.ids.write().expect("the symbol map lock is poisoned");
        ids.entry(symbol.to_string()).or_insert(id);
    }

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        if self.updates.is_none() {
            self.start();
        }
        let updates = self.updates.as_mut().expect("started just above");
        match updates.recv().await {
            Some(handover) => handover,
            None => Err(FeedError::Closed),
        }
    }
}

struct Worker {
    rest: RestClient,
    ws_base: String,
    decoder: Decoder,
    backoff: Duration,
}

/// The engine dropped the feed: there is nobody left to hand updates to.
struct Gone;

impl Worker {
    async fn run(mut self, tx: mpsc::Sender<Handover>) {
        let dropped = tx.closed();
        tokio::pin!(dropped);
        tokio::select! {
            _ = &mut dropped => (),
            _ = self.reconnect_forever(&tx) => (),
        }
        tracing::info!("private stream task finished; the engine let the feed go");
    }

    async fn reconnect_forever(&mut self, tx: &mpsc::Sender<Handover>) -> Gone {
        loop {
            match self.connect().await {
                Ok(socket) => {
                    let opened = Instant::now();
                    // First-connection readiness and reconnect watermark,
                    // before any account update from this socket.
                    let reset = OrderUpdate::StreamReset { recv_ns: mono_ns() };
                    if hand_over(tx, Ok(reset)).await.is_err() {
                        return Gone;
                    }
                    if self.pump(socket, tx).await.is_err() {
                        return Gone;
                    }
                    if opened.elapsed() >= HEALTHY_AFTER {
                        self.backoff = Duration::ZERO;
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, "private stream did not come up; trying again");
                    if hand_over(tx, Err(e)).await.is_err() {
                        return Gone;
                    }
                }
            }
            self.wait_before_redialling().await;
        }
    }

    async fn wait_before_redialling(&mut self) {
        if !self.backoff.is_zero() {
            tokio::time::sleep(self.backoff).await;
        }
        self.backoff = if self.backoff.is_zero() {
            BACKOFF_START
        } else {
            (self.backoff * 2).min(BACKOFF_CAP)
        };
    }

    async fn connect(&mut self) -> Result<Socket, FeedError> {
        crate::tls::install_crypto_provider();

        // A fresh key on every dial. POST returns the account's currently
        // active key and extends it, so a reconnect after an expiry and a
        // reconnect after a network fault are one code path.
        let reply = self
            .rest
            .post_keyed(PATH_LISTEN_KEY)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        let listen_key = reply
            .get("listenKey")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                FeedError::Transport("the listen-key reply carried no listenKey".to_string())
            })?
            .to_string();

        // Current private streams take the listen key and selected events in
        // the query. The older path form stopped carrying private data in
        // April 2026. Nagle off: an order update held back for coalescing
        // arrives late.
        let url = private_stream_url(&self.ws_base, &listen_key);
        let (socket, _) = connect_async_with_config(url.as_str(), None, true)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;
        tracing::info!("private stream connected");
        Ok(socket)
    }

    async fn pump(&mut self, mut socket: Socket, tx: &mpsc::Sender<Handover>) -> Result<(), Gone> {
        let mut last_touch = Instant::now();
        loop {
            let deadline = tokio::time::Instant::from_std(last_touch + KEEPALIVE_EVERY);
            let wake = tokio::select! {
                frame = socket.next() => Wake::Frame(frame),
                _ = tokio::time::sleep_until(deadline) => Wake::Keepalive,
            };
            let step = match wake {
                Wake::Keepalive => match self.rest.put_keyed(PATH_LISTEN_KEY).await {
                    Ok(_) => Step::Touched,
                    // A key that cannot be extended will expire under the
                    // socket; redialling now trades a clean reconnect for a
                    // silent death later.
                    Err(e) => Step::Dropped(format!("listen-key keepalive failed: {e}")),
                },
                Wake::Frame(Some(Ok(Message::Text(text)))) => Step::Text(text.as_str().to_string()),
                Wake::Frame(Some(Ok(Message::Ping(payload)))) => {
                    let _ = socket.send(Message::Pong(payload)).await;
                    Step::Idle
                }
                Wake::Frame(Some(Ok(Message::Close(_)))) => {
                    Step::Dropped("venue closed the socket".to_string())
                }
                Wake::Frame(Some(Ok(_))) => Step::Idle,
                Wake::Frame(Some(Err(e))) => Step::Dropped(e.to_string()),
                Wake::Frame(None) => Step::Dropped("stream ended".to_string()),
            };

            match step {
                Step::Idle => (),
                Step::Touched => last_touch = Instant::now(),
                Step::Text(text) => {
                    let read = self.decoder.ingest(&text);
                    while let Some(update) = self.decoder.pending.pop_front() {
                        hand_over(tx, Ok(update)).await?;
                    }
                    if let Err(e) = read {
                        tracing::warn!(error = %e, "private stream frame");
                        hand_over(tx, Err(e)).await?;
                        return Ok(());
                    }
                }
                Step::Dropped(why) => {
                    tracing::warn!(why, "private stream dropped, reconnecting");
                    hand_over(
                        tx,
                        Err(FeedError::Transport(format!(
                            "private stream unavailable: {why}"
                        ))),
                    )
                    .await?;
                    return Ok(());
                }
            }
        }
    }
}

fn private_stream_url(ws_base: &str, listen_key: &str) -> String {
    format!("{ws_base}/private/ws?listenKey={listen_key}&events=ORDER_TRADE_UPDATE/ALGO_UPDATE")
}

enum Wake {
    Frame(Option<Result<Message, tokio_tungstenite::tungstenite::Error>>),
    Keepalive,
}

enum Step {
    Idle,
    Touched,
    Text(String),
    Dropped(String),
}

async fn hand_over(tx: &mpsc::Sender<Handover>, item: Handover) -> Result<(), Gone> {
    tx.send(item).await.map_err(|_| Gone)
}

/// Frames in, updates out. No socket and no clock but the receive stamp.
pub(crate) struct Decoder {
    ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>,
    pub(crate) pending: VecDeque<OrderUpdate>,
    acked: HashSet<String>,
    acked_order: VecDeque<String>,
    bookkeeping_orders: HashSet<String>,
    bookkeeping_order: VecDeque<String>,
}

impl Decoder {
    pub(crate) fn new(ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>) -> Self {
        Self {
            ids,
            pending: VecDeque::new(),
            acked: HashSet::new(),
            acked_order: VecDeque::new(),
            bookkeeping_orders: HashSet::new(),
            bookkeeping_order: VecDeque::new(),
        }
    }

    pub(crate) fn ingest(&mut self, text: &str) -> Result<(), FeedError> {
        let frame: Value = serde_json::from_str(text)
            .map_err(|e| FeedError::BadMessage(format!("{e}: {}", first_chars(text))))?;
        match frame.get("e").and_then(Value::as_str) {
            Some("ORDER_TRADE_UPDATE") => self.order_trade_update(&frame),
            Some("ALGO_UPDATE") => self.algo_update(&frame),
            // The key died under the socket; only a fresh dial gets a new
            // one, so this surfaces as a transport fault the worker redials
            // on.
            Some("listenKeyExpired") => Err(FeedError::Transport(
                "the venue expired this stream's listen key".to_string(),
            )),
            // ACCOUNT_UPDATE and anything else: positions and balances are
            // the account view's to read, not this feed's to relay.
            _ => Ok(()),
        }
    }

    fn order_trade_update(&mut self, frame: &Value) -> Result<(), FeedError> {
        let order = frame
            .get("o")
            .ok_or_else(|| FeedError::BadMessage("ORDER_TRADE_UPDATE carries no order".into()))?;
        let client_order_id =
            str_field(order, "c").map_err(|e| FeedError::BadMessage(e.to_string()))?;
        let execution_type = order.get("x").and_then(Value::as_str).unwrap_or_default();
        let venue_order_id = id_text(order, "i");
        let venue_symbol = order.get("s").and_then(Value::as_str);
        let bookkeeping = order.get("cp").and_then(Value::as_bool) == Some(true)
            || is_exchange_or_stop_id(&client_order_id)
            || venue_order_id.as_ref().is_some_and(|id| {
                venue_symbol.is_some_and(|symbol| {
                    self.bookkeeping_orders
                        .contains(&scoped_execution_id(symbol, id))
                })
            });
        let recv_ns = mono_ns();
        match execution_type {
            "NEW" => {
                if bookkeeping {
                    // This adapter's own stop order, or the exchange's forced
                    // close: not an order any strategy is waiting on. Its
                    // acceptance is the position's stop appearing.
                    if let (Some(symbol), Ok(Some(trigger_px))) = (
                        self.symbol_id(order),
                        opt_num_field(order, "sp").map_err(bad),
                    ) {
                        if trigger_px > 0.0 {
                            self.pending.push_back(OrderUpdate::StopAttached {
                                symbol,
                                trigger_px,
                                recv_ns,
                            });
                        }
                    }
                    return Ok(());
                }
                // One ack per order: nothing observed says the venue repeats
                // an acceptance, and a repeat read as a second order is the
                // failure this guards.
                if !self.remember_ack(&client_order_id) {
                    return Ok(());
                }
                let venue_order_id = order
                    .get("i")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| FeedError::BadMessage("an acceptance carried no i".into()))?;
                self.pending.push_back(OrderUpdate::Ack(OrderAck {
                    client_order_id,
                    venue_order_id: venue_order_id.to_string(),
                    sent_ns: 0,
                    ack_ns: recv_ns,
                }));
            }
            "TRADE" => {
                let Some(symbol) = self.symbol_id(order) else {
                    // A fill on a symbol the engine has no id for cannot be
                    // routed. It is still a real fill, so it is said out loud.
                    tracing::warn!(
                        symbol = order
                            .get("s")
                            .and_then(|value| value.as_str())
                            .unwrap_or("?"),
                        "a fill arrived on a symbol this engine has no id for"
                    );
                    return Ok(());
                };
                let side = match order.get("S").and_then(Value::as_str) {
                    Some("BUY") => engine_types::Side::Buy,
                    Some("SELL") => engine_types::Side::Sell,
                    other => {
                        return Err(FeedError::BadMessage(format!(
                            "a fill carries side {other:?}"
                        )))
                    }
                };
                let native_exec_id = order
                    .get("t")
                    .and_then(Value::as_i64)
                    .filter(|t| *t > 0)
                    .ok_or_else(|| FeedError::BadMessage("a fill carried no trade id".into()))?;
                let symbol_name = order
                    .get("s")
                    .and_then(Value::as_str)
                    .ok_or_else(|| FeedError::BadMessage("a fill carried no symbol".into()))?;
                let qty = num_field(order, "l").map_err(bad)?;
                let px = num_field(order, "L").map_err(bad)?;
                let venue_ts_ms = frame.get("T").and_then(Value::as_i64).unwrap_or(0);
                if qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
                    return Err(FeedError::BadMessage(
                        "a fill has non-positive quantity, price, or transaction time".into(),
                    ));
                }
                let is_maker = order.get("m").and_then(Value::as_bool).ok_or_else(|| {
                    FeedError::BadMessage("a fill has no boolean maker flag".into())
                })?;
                let mut fee = opt_num_field(order, "n").map_err(bad)?;
                if fee.is_some() {
                    let fee_asset = str_field(order, "N").map_err(bad)?;
                    if fee_asset != "USDT" {
                        tracing::warn!(
                            symbol = symbol_name,
                            trade_id = native_exec_id,
                            asset = %fee_asset,
                            "a Binance fee is not USDT; preserving the fill with an unknown fee"
                        );
                        fee = None;
                    }
                }
                self.pending.push_back(OrderUpdate::Fill {
                    exec_id: scoped_execution_id(symbol_name, &native_exec_id.to_string()),
                    // The stop's fill belongs to the position, not to any
                    // strategy order; the empty id is how the engine spells
                    // that.
                    client_order_id: if bookkeeping {
                        String::new()
                    } else {
                        client_order_id
                    },
                    symbol,
                    side,
                    qty,
                    px,
                    fee,
                    is_maker,
                    // Binance states no reason for a close on this row.
                    forced_close: None,
                    venue_ts_ms,
                    recv_ns,
                });
            }
            // EXPIRED ends an order without a trade — above all a post-only
            // that would have crossed, which the venue expires rather than
            // fills. EXPIRED under self-trade prevention spells itself the
            // same way. Both leave nothing working, which is a cancellation
            // to the engine.
            "CANCELED" | "EXPIRED" => {
                if !bookkeeping {
                    self.pending.push_back(OrderUpdate::Cancelled {
                        client_order_id,
                        recv_ns,
                    });
                }
            }
            "AMENDMENT" => {
                if bookkeeping {
                    return Ok(());
                }
                let total = num_field(order, "q").map_err(bad)?;
                let filled = num_field(order, "z").map_err(bad)?;
                self.pending.push_back(OrderUpdate::Amended {
                    client_order_id,
                    px: num_field(order, "p").map_err(bad)?,
                    // What is still working: a fill that landed while the
                    // amend was in flight shows here as a smaller number.
                    qty: (total - filled).max(0.0),
                    recv_ns,
                });
            }
            // CALCULATED is liquidation settlement bookkeeping; the fills
            // themselves arrive as TRADE rows.
            _ => (),
        }
        Ok(())
    }

    fn algo_update(&mut self, frame: &Value) -> Result<(), FeedError> {
        let order = frame
            .get("o")
            .ok_or_else(|| FeedError::BadMessage("ALGO_UPDATE carries no order".into()))?;
        let native_stop = order.get("at").and_then(Value::as_str) == Some("CONDITIONAL")
            && order.get("o").and_then(Value::as_str) == Some("STOP_MARKET")
            && order.get("cp").and_then(Value::as_bool) == Some(true);
        if !native_stop {
            return Ok(());
        }

        if let (Some(actual_order_id), Some(symbol)) = (
            id_text(order, "ai").filter(|id| id != "0"),
            order.get("s").and_then(Value::as_str),
        ) {
            self.remember_bookkeeping_order(scoped_execution_id(symbol, &actual_order_id));
        }

        let adapter_stop = order
            .get("caid")
            .and_then(Value::as_str)
            .is_some_and(|id| id.starts_with(super::parse::STOP_ID_PREFIX));
        if order.get("X").and_then(Value::as_str) == Some("NEW") && adapter_stop {
            let Some(symbol) = self.symbol_id(order) else {
                tracing::warn!(
                    symbol = order
                        .get("s")
                        .and_then(|value| value.as_str())
                        .unwrap_or("?"),
                    "an algo stop arrived on a symbol this engine has no id for"
                );
                return Ok(());
            };
            let trigger_px = num_field(order, "tp").map_err(bad)?;
            if trigger_px <= 0.0 {
                return Err(FeedError::BadMessage(
                    "an algo stop acceptance carried no positive trigger price".into(),
                ));
            }
            self.pending.push_back(OrderUpdate::StopAttached {
                symbol,
                trigger_px,
                recv_ns: mono_ns(),
            });
        }
        Ok(())
    }

    fn symbol_id(&self, order: &Value) -> Option<SymbolId> {
        let symbol = order.get("s").and_then(Value::as_str)?;
        let ids = self.ids.read().expect("the symbol map lock is poisoned");
        ids.get(symbol).copied()
    }

    /// True the first time this id acks. Bounded, so a long run's memory
    /// stays flat.
    fn remember_ack(&mut self, client_order_id: &str) -> bool {
        if !self.acked.insert(client_order_id.to_string()) {
            return false;
        }
        self.acked_order.push_back(client_order_id.to_string());
        if self.acked_order.len() > ACK_MEMORY {
            if let Some(oldest) = self.acked_order.pop_front() {
                self.acked.remove(&oldest);
            }
        }
        true
    }

    fn remember_bookkeeping_order(&mut self, venue_order_id: String) {
        if !self.bookkeeping_orders.insert(venue_order_id.clone()) {
            return;
        }
        self.bookkeeping_order.push_back(venue_order_id);
        if self.bookkeeping_order.len() > BOOKKEEPING_MEMORY {
            if let Some(oldest) = self.bookkeeping_order.pop_front() {
                self.bookkeeping_orders.remove(&oldest);
            }
        }
    }
}

fn bad(error: VenueError) -> FeedError {
    FeedError::BadMessage(error.to_string())
}

fn first_chars(text: &str) -> String {
    text.chars().take(160).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::Side;

    fn decoder() -> Decoder {
        let ids = HashMap::from([
            ("BTCUSDT".to_string(), SymbolId(0)),
            ("ETHUSDT".to_string(), SymbolId(1)),
        ]);
        Decoder::new(Arc::new(RwLock::new(ids)))
    }

    /// The documented ORDER_TRADE_UPDATE shape: lettered fields, numbers as
    /// strings, the order nested under `o`.
    fn frame(fields: &str) -> String {
        format!(
            r#"{{"e":"ORDER_TRADE_UPDATE","E":1568879465651,"T":1568879465650,"o":{{{fields}}}}}"#
        )
    }

    fn algo_frame(fields: &str) -> String {
        format!(r#"{{"e":"ALGO_UPDATE","E":1764774615227,"T":1764774615226,"o":{{{fields}}}}}"#)
    }

    #[test]
    fn the_private_route_carries_the_key_and_only_consumed_events_in_the_query() {
        assert_eq!(
            private_stream_url("wss://socket.invalid", "listen-key-1"),
            "wss://socket.invalid/private/ws?listenKey=listen-key-1&events=ORDER_TRADE_UPDATE/ALGO_UPDATE"
        );
    }

    #[test]
    fn an_acceptance_acks_once_and_carries_the_venues_number() {
        let mut d = decoder();
        let accepted = frame(
            r#""s":"BTCUSDT","c":"eng-1","S":"BUY","o":"LIMIT","f":"GTC","q":"0.004",
               "p":"78000.1","ap":"0","sp":"0","x":"NEW","X":"NEW","i":8886774,"l":"0",
               "z":"0","L":"0","T":1568879465650,"t":0,"m":false,"R":false"#,
        );
        d.ingest(&accepted).unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::Ack(ack)) => {
                assert_eq!(ack.client_order_id, "eng-1");
                assert_eq!(ack.venue_order_id, "8886774");
            }
            other => panic!("expected an ack, got {other:?}"),
        }
        d.ingest(&accepted).unwrap();
        assert!(d.pending.is_empty(), "the same order acked twice");
    }

    #[test]
    fn a_fill_carries_price_size_fee_and_which_side_of_the_spread() {
        let mut d = decoder();
        d.ingest(&frame(
            r#""s":"ETHUSDT","c":"eng-2","S":"SELL","o":"LIMIT","f":"GTC","q":"1.5",
               "p":"2800","ap":"2800","sp":"0","x":"TRADE","X":"PARTIALLY_FILLED",
               "i":8886775,"l":"0.5","z":"0.5","L":"2800.5","n":"0.056",
               "N":"USDT","T":1568879465650,"t":109891,"m":true,"R":false"#,
        ))
        .unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::Fill {
                exec_id,
                client_order_id,
                symbol,
                side,
                qty,
                px,
                fee,
                is_maker,
                venue_ts_ms,
                ..
            }) => {
                assert_eq!(exec_id, "ETHUSDT:109891");
                assert_eq!(client_order_id, "eng-2");
                assert_eq!(symbol, SymbolId(1));
                assert_eq!(side, Side::Sell);
                assert_eq!(qty, 0.5, "the last filled quantity, not the order's");
                assert_eq!(px, 2800.5, "the last filled price, not the order's");
                assert_eq!(fee, Some(0.056));
                assert!(is_maker);
                assert_eq!(venue_ts_ms, 1568879465650);
            }
            other => panic!("expected a fill, got {other:?}"),
        }
    }

    #[test]
    fn an_algo_stop_acceptance_is_a_stop_attached_not_an_ack() {
        let mut d = decoder();
        d.ingest(&algo_frame(
            r#""caid":"engstop-1788092000000-1","aid":2148713,"at":"CONDITIONAL",
               "o":"STOP_MARKET","s":"BTCUSDT","S":"SELL","ps":"BOTH","q":"0",
               "X":"NEW","ai":"","tp":"75000.5","p":"0","wt":"MARK_PRICE",
               "cp":true,"R":false"#,
        ))
        .unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::StopAttached {
                symbol, trigger_px, ..
            }) => {
                assert_eq!(symbol, SymbolId(0));
                assert_eq!(trigger_px, 75000.5);
            }
            other => panic!("expected StopAttached, got {other:?}"),
        }
        assert!(d.pending.is_empty());
    }

    #[test]
    fn a_foreign_algo_stop_waits_for_the_side_aware_account_snapshot() {
        let mut d = decoder();
        d.ingest(&algo_frame(
            r#""caid":"manual-stop","aid":2148714,"at":"CONDITIONAL",
               "o":"STOP_MARKET","s":"BTCUSDT","S":"BUY","q":"0",
               "X":"NEW","ai":"","tp":"80000","cp":true"#,
        ))
        .unwrap();
        assert!(d.pending.is_empty());
    }

    #[test]
    fn a_fill_needs_a_boolean_maker_flag() {
        let mut d = decoder();
        let missing_maker = frame(
            r#""s":"BTCUSDT","c":"eng-1","S":"BUY","o":"LIMIT","q":"1",
               "p":"1","sp":"0","x":"TRADE","X":"FILLED","i":1,"l":"1",
               "z":"1","L":"1","n":"0.1","N":"USDT","T":1,"t":22"#,
        );
        assert!(matches!(
            d.ingest(&missing_maker),
            Err(FeedError::BadMessage(_))
        ));
        assert!(d.pending.is_empty());
    }

    #[test]
    fn an_algo_stop_links_its_actual_order_to_the_later_fill() {
        let mut d = decoder();
        d.ingest(&algo_frame(
            r#""caid":"engstop-1788092000000-1","aid":2148713,"at":"CONDITIONAL",
               "o":"STOP_MARKET","s":"BTCUSDT","S":"SELL","ps":"BOTH","q":"0",
               "X":"TRIGGERED","ai":"991","tp":"75000","p":"0","wt":"MARK_PRICE",
               "cp":true,"R":false"#,
        ))
        .unwrap();
        assert!(d.pending.is_empty());

        d.ingest(&frame(
            r#""s":"BTCUSDT","c":"venue-generated","S":"SELL","o":"MARKET","q":"0.004",
               "p":"0","sp":"0","x":"TRADE","X":"FILLED","i":991,"l":"0.004",
               "z":"0.004","L":"74990.1","n":"0.015","N":"USDT",
               "T":1568879465650,"t":22,"m":false"#,
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Fill { client_order_id, .. }) if client_order_id.is_empty()
        ));
    }

    #[test]
    fn a_stop_order_number_on_one_symbol_does_not_capture_another_symbols_fill() {
        let mut d = decoder();
        d.ingest(&algo_frame(
            r#""caid":"engstop-1","aid":2,"at":"CONDITIONAL","o":"STOP_MARKET",
               "s":"BTCUSDT","S":"SELL","X":"TRIGGERED","ai":"991",
               "tp":"75000","cp":true"#,
        ))
        .unwrap();
        d.ingest(&frame(
            r#""s":"ETHUSDT","c":"eng-eth","S":"BUY","o":"LIMIT","q":"1",
               "p":"2800","sp":"0","x":"TRADE","X":"FILLED","i":991,"l":"1",
               "z":"1","L":"2800","n":"0.1","N":"USDT","T":1,"t":22,"m":false"#,
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Fill { client_order_id, .. }) if client_order_id == "eng-eth"
        ));
    }

    #[test]
    fn a_non_usdt_fill_fee_stays_unknown_instead_of_corrupting_accounting() {
        let mut d = decoder();
        let result = d.ingest(&frame(
            r#""s":"BTCUSDT","c":"eng-1","S":"BUY","o":"LIMIT","q":"1",
               "p":"1","sp":"0","x":"TRADE","X":"FILLED","i":1,"l":"1",
               "z":"1","L":"1","n":"0.1","N":"BNB","T":1,"t":22,"m":false"#,
        ));
        result.unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Fill { fee: None, .. })
        ));
    }

    #[test]
    fn rest_and_stream_agree_on_scoped_ids_and_non_usdt_fee_handling() {
        let rest = super::super::parse::parse_trades(&serde_json::json!([{
            "id": 22, "orderId": 991, "symbol": "BTCUSDT", "side": "SELL",
            "price": "74990.1", "qty": "0.004", "commission": "0.015",
            "commissionAsset": "BNB", "maker": false, "time": 1568879465650i64
        }]))
        .unwrap()
        .pop()
        .unwrap()
        .1;

        let mut d = decoder();
        d.ingest(&frame(
            r#""s":"BTCUSDT","c":"eng-1","S":"SELL","o":"MARKET","q":"0.004",
               "p":"0","sp":"0","x":"TRADE","X":"FILLED","i":991,"l":"0.004",
               "z":"0.004","L":"74990.1","n":"0.015","N":"BNB",
               "T":1568879465650,"t":22,"m":false"#,
        ))
        .unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::Fill { exec_id, fee, .. }) => {
                assert_eq!(exec_id, rest.exec_id);
                assert_eq!(fee, rest.fee);
                assert_eq!(fee, None);
            }
            other => panic!("expected a fill, got {other:?}"),
        }
    }

    #[test]
    fn a_close_all_fill_is_bookkeeping_even_if_the_algo_event_was_missed() {
        // A reconnect forgets the ALGO_UPDATE-to-order-id join. `cp` is on
        // the actual ORDER_TRADE_UPDATE, so this remains attributable when
        // the trade wins the cross-event-type race or lands after a redial.
        let mut reconnected = decoder();
        reconnected
            .ingest(&frame(
                r#""s":"BTCUSDT","c":"venue-generated","S":"SELL","o":"MARKET",
                   "q":"0.004","p":"0","sp":"0","x":"TRADE","X":"FILLED",
                   "i":991,"l":"0.004","z":"0.004","L":"74990.1","n":"0.015",
                   "N":"USDT","T":1568879465650,"t":22,"m":false,"cp":true"#,
            ))
            .unwrap();
        assert!(matches!(
            reconnected.pending.pop_front(),
            Some(OrderUpdate::Fill { client_order_id, .. }) if client_order_id.is_empty()
        ));
    }

    #[test]
    fn a_fill_needs_positive_size_price_and_transaction_time() {
        for (qty, px) in [("0", "1"), ("1", "0")] {
            let mut d = decoder();
            let bad_fill = frame(&format!(
                r#""s":"BTCUSDT","c":"eng-1","S":"BUY","o":"LIMIT","q":"1",
                   "p":"1","sp":"0","x":"TRADE","X":"FILLED","i":1,"l":"{qty}",
                   "z":"1","L":"{px}","n":"0.1","N":"USDT","T":1,"t":22,"m":false"#
            ));
            assert!(matches!(d.ingest(&bad_fill), Err(FeedError::BadMessage(_))));
            assert!(d.pending.is_empty());
        }

        let mut d = decoder();
        let missing_time = frame(
            r#""s":"BTCUSDT","c":"eng-1","S":"BUY","o":"LIMIT","q":"1",
               "p":"1","sp":"0","x":"TRADE","X":"FILLED","i":1,"l":"1",
               "z":"1","L":"1","n":"0.1","N":"USDT","T":1,"t":22,"m":false"#,
        )
        .replacen(r#""T":1568879465650"#, r#""T":0"#, 1);
        assert!(matches!(
            d.ingest(&missing_time),
            Err(FeedError::BadMessage(_))
        ));
        assert!(d.pending.is_empty());
    }

    #[test]
    fn an_exchange_forced_close_arrives_with_the_empty_client_id() {
        let mut d = decoder();
        d.ingest(&frame(
            r#""s":"BTCUSDT","c":"autoclose-159610762004000001","S":"SELL","o":"MARKET",
               "q":"0.004","p":"0","sp":"0","x":"TRADE","X":"FILLED","i":1,
               "l":"0.004","z":"0.004","L":"74990.1","n":"0.015","N":"USDT",
               "T":1568879465650,"t":22,"m":false"#,
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Fill { client_order_id, .. }) if client_order_id.is_empty()
        ));
    }

    #[test]
    fn a_cancel_and_a_post_only_expiry_both_end_the_order() {
        let mut d = decoder();
        d.ingest(&frame(
            r#""s":"BTCUSDT","c":"eng-3","S":"BUY","o":"LIMIT","q":"1","p":"1","sp":"0",
               "x":"CANCELED","X":"CANCELED","i":2,"l":"0","z":"0","L":"0",
               "T":1,"t":0,"m":false"#,
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Cancelled { ref client_order_id, .. }) if client_order_id == "eng-3"
        ));

        // A GTX that would cross is expired by the venue rather than filled;
        // to the engine that is the order ending without a trade.
        d.ingest(&frame(
            r#""s":"BTCUSDT","c":"eng-4","S":"BUY","o":"LIMIT","q":"1","p":"1","sp":"0",
               "x":"EXPIRED","X":"EXPIRED","i":3,"l":"0","z":"0","L":"0",
               "T":1,"t":0,"m":false"#,
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Cancelled { ref client_order_id, .. }) if client_order_id == "eng-4"
        ));
    }

    #[test]
    fn an_amendment_reports_the_price_and_what_is_still_working() {
        let mut d = decoder();
        d.ingest(&frame(
            r#""s":"BTCUSDT","c":"eng-5","S":"BUY","o":"LIMIT","q":"0.010","p":"77000.5",
               "sp":"0","x":"AMENDMENT","X":"PARTIALLY_FILLED","i":4,"l":"0","z":"0.003",
               "L":"0","T":1,"t":0,"m":false"#,
        ))
        .unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::Amended {
                client_order_id,
                px,
                qty,
                ..
            }) => {
                assert_eq!(client_order_id, "eng-5");
                assert_eq!(px, 77000.5);
                assert_eq!(qty, 0.007, "a fill in flight shows as a smaller remainder");
            }
            other => panic!("expected Amended, got {other:?}"),
        }
    }

    #[test]
    fn a_fill_on_a_symbol_with_no_id_is_skipped_and_said_out_loud() {
        let mut d = decoder();
        d.ingest(&frame(
            r#""s":"DOGEUSDT","c":"eng-6","S":"BUY","o":"LIMIT","q":"1","p":"1","sp":"0",
               "x":"TRADE","X":"FILLED","i":5,"l":"1","z":"1","L":"0.1","n":"0",
               "T":1,"t":33,"m":false"#,
        ))
        .unwrap();
        assert!(d.pending.is_empty());
    }

    #[test]
    fn an_expired_listen_key_is_a_transport_fault_so_the_socket_redials() {
        let mut d = decoder();
        let expired = d.ingest(r#"{"e":"listenKeyExpired","E":1576653824250,"listenKey":"old"}"#);
        assert!(
            matches!(expired, Err(FeedError::Transport(_))),
            "{expired:?}"
        );
    }

    #[test]
    fn account_updates_and_strangers_are_not_this_feeds_to_relay() {
        let mut d = decoder();
        assert!(d
            .ingest(r#"{"e":"ACCOUNT_UPDATE","E":1,"T":1,"a":{"B":[],"P":[]}}"#)
            .is_ok());
        assert!(d.ingest(r#"{"e":"MARGIN_CALL","E":1}"#).is_ok());
        assert!(d.pending.is_empty());
    }

    #[test]
    fn an_unreadable_frame_surfaces_for_fail_closed_recovery() {
        let mut d = decoder();
        assert!(matches!(
            d.ingest("{not json"),
            Err(FeedError::BadMessage(_))
        ));
    }

    #[test]
    fn the_ack_memory_stays_flat_over_a_long_run() {
        let mut d = decoder();
        for n in 0..(ACK_MEMORY + 100) {
            assert!(d.remember_ack(&format!("eng-1-{n}")));
        }
        assert_eq!(d.acked.len(), ACK_MEMORY);
        assert_eq!(d.acked_order.len(), ACK_MEMORY);

        for n in 0..(BOOKKEEPING_MEMORY + 100) {
            d.remember_bookkeeping_order(n.to_string());
        }
        assert_eq!(d.bookkeeping_orders.len(), BOOKKEEPING_MEMORY);
        assert_eq!(d.bookkeeping_order.len(), BOOKKEEPING_MEMORY);
    }
}
