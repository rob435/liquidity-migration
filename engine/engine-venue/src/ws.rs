//! The private order stream: order and execution updates over the demo
//! WebSocket.
//!
//! The socket carries everything that happens to the account, including
//! orders this engine did not place. Rows without our own `orderLinkId` are
//! not ours and are dropped — the demo account is hand-traded too.
//!
//! **The socket lives in its own task.** The engine waits on this feed inside
//! a `select!`, which drops the losing branch's future every flush tick, so
//! anything half-finished inside `next_update` is thrown away several times a
//! second. A handshake is six awaits long and cannot survive that. So the
//! dial, the auth, the subscription, the ping schedule and the reconnect loop
//! all belong to a task nobody cancels, and `next_update` is nothing but a
//! channel receive — which loses nothing when it is dropped. The task is
//! spawned on the engine's own current-thread runtime, so it still runs on
//! the one engine thread; it just is not part of the `select!`.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_types::ids::{Symbol, SymbolId};
use engine_types::market::{FeedError, OrderFeed};
use engine_types::orders::{OrderAck, OrderUpdate, Side};
use engine_types::VenueError;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use crate::clock::{mono_ns, wall_ms};
use crate::creds::Credentials;
use crate::parse::{num_field, opt_num_field, str_field};
use crate::realm::VenueRealm;
use crate::sign::ws_signature;

/// Bybit drops a private socket that goes quiet for 30 seconds.
const PING_EVERY: Duration = Duration::from_secs(20);
const AUTH_WINDOW_MS: i64 = 5_000;
const AUTH_REPLY_TIMEOUT: Duration = Duration::from_secs(10);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_CAP: Duration = Duration::from_secs(30);
/// A socket that lasted this long was a real connection, so the wait between
/// dials starts over. Shorter than that and the venue is still unhappy.
const HEALTHY_AFTER: Duration = Duration::from_secs(30);
/// How many client order ids to remember so one order acks once. Well past
/// anything in flight, and it keeps a long run's memory flat.
const ACK_MEMORY: usize = 8192;
/// Room for a burst of updates while the engine is busy with something else.
const CHANNEL_DEPTH: usize = 1024;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;
/// What the socket task hands over: an update, or a failure worth telling the
/// engine about. Never [`FeedError::Closed`] — that arrives as the channel
/// itself closing, and means the task is gone.
type Handover = Result<OrderUpdate, FeedError>;

pub struct BybitOrderFeed {
    url: String,
    creds: Credentials,
    /// Name to id, shared with the socket task's decoder.
    ///
    /// The private stream is subscribed per account, not per symbol, so
    /// updates for a symbol the engine started following after boot already
    /// arrive — what is missing is a way to read them. The engine writes here;
    /// the decoder reads. A lock rather than a channel because a fill must be
    /// decodable the instant the engine says it is following the name, not one
    /// message later.
    ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>,
    updates: Option<mpsc::Receiver<Handover>>,
}

impl BybitOrderFeed {
    /// The live feed: the realm's private stream, and the realm's credentials
    /// from the environment. Like the gateway, mainnet fails here unless the
    /// owner has armed `REAL_MONEY`.
    pub fn new(realm: VenueRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = Credentials::from_env(realm)?;
        Ok(Self::build(realm.private_ws(), creds, symbols))
    }

    /// Point the feed at a local server. Tests and the mock venue only.
    pub fn for_test(url: &str, creds: Credentials, symbols: Vec<Symbol>) -> Self {
        Self::build(url, creds, symbols)
    }

    fn build(url: &str, creds: Credentials, symbols: Vec<Symbol>) -> Self {
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Self {
            url: url.to_string(),
            creds,
            ids: Arc::new(RwLock::new(ids)),
            updates: None,
        }
    }

    /// Teach the decoder what id a symbol has. Idempotent, and a name already
    /// known keeps the id it had — an id that moved would make every record
    /// already in the log mean something else.
    pub fn learn(&mut self, symbol: &str, id: SymbolId) {
        let mut ids = self.ids.write().expect("the symbol map lock is poisoned");
        ids.entry(symbol.to_string()).or_insert(id);
    }

    /// Start the socket task. The first `next_update` does this, because that
    /// is the first moment there is certain to be a runtime to spawn on.
    fn start(&mut self) {
        let (tx, rx) = mpsc::channel(CHANNEL_DEPTH);
        let worker = Worker {
            url: self.url.clone(),
            creds: self.creds.clone(),
            decoder: Decoder::new(self.ids.clone()),
            backoff: Duration::ZERO,
            connected_before: false,
        };
        tokio::spawn(worker.run(tx));
        self.updates = Some(rx);
    }
}

impl OrderFeed for BybitOrderFeed {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        BybitOrderFeed::learn(self, symbol, id);
    }

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        if self.updates.is_none() {
            self.start();
        }
        let updates = self.updates.as_mut().expect("started just above");
        match updates.recv().await {
            Some(handover) => handover,
            // The socket task only ends when this feed is dropped, so a shut
            // channel really is the end of the feed.
            None => Err(FeedError::Closed),
        }
    }
}

/// The socket task: dial, authenticate, subscribe, read, reconnect. Nothing
/// cancels it, so a handshake several round trips long gets to finish.
struct Worker {
    url: String,
    creds: Credentials,
    decoder: Decoder,
    backoff: Duration,
    connected_before: bool,
}

/// The engine dropped the feed: there is nobody left to hand updates to.
struct Gone;

impl Worker {
    async fn run(mut self, tx: mpsc::Sender<Handover>) {
        let dropped = tx.closed();
        tokio::pin!(dropped);
        tokio::select! {
            // Stop promptly when the feed goes away, even if the loop below is
            // parked on a socket or a backoff sleep. No task is left behind.
            _ = &mut dropped => (),
            _ = self.reconnect_forever(&tx) => (),
        }
        tracing::info!("private stream task finished; the engine let the feed go");
    }

    /// Keep a socket up for as long as the engine is listening.
    ///
    /// Nothing the venue does during a handshake is final. A refused auth is
    /// most likely a bad key, which will go on failing and go on saying so in
    /// the log — the honest behaviour for a process nobody is watching.
    /// Retiring the feed instead would cost every fill for the rest of the
    /// run, silently.
    async fn reconnect_forever(&mut self, tx: &mpsc::Sender<Handover>) -> Gone {
        loop {
            match self.connect().await {
                Ok(socket) => {
                    let opened = Instant::now();
                    if self.connected_before {
                        // Updates that happened while we were away are lost;
                        // the engine must refresh its account view rather
                        // than trust the gap. This goes out before anything
                        // read off the new socket.
                        let reset = OrderUpdate::StreamReset { recv_ns: mono_ns() };
                        if hand_over(tx, Ok(reset)).await.is_err() {
                            return Gone;
                        }
                    }
                    self.connected_before = true;
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

    /// Wait, then make the next wait longer. Only a dial that failed or a
    /// socket that died reaches here, so the wait grows while the venue is
    /// unhappy and never because the engine looked away.
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

    async fn connect(&self) -> Result<Socket, FeedError> {
        crate::tls::install_crypto_provider();

        // Nagle off: an order update held back for coalescing is an order
        // update arriving late.
        let (mut socket, _) = connect_async_with_config(self.url.as_str(), None, true)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;

        let expires = wall_ms() + AUTH_WINDOW_MS;
        let auth = json!({
            "op": "auth",
            "args": [self.creds.key(), expires, ws_signature(self.creds.secret(), expires)],
        });
        send(&mut socket, auth.to_string()).await?;
        await_auth(&mut socket).await?;

        let subscribe = json!({"op": "subscribe", "args": ["order", "execution"]});
        send(&mut socket, subscribe.to_string()).await?;

        tracing::info!("private stream authenticated and subscribed");
        Ok(socket)
    }

    /// Read one socket until it dies. `Err(Gone)` means the feed was dropped;
    /// `Ok` means dial again.
    async fn pump(&mut self, mut socket: Socket, tx: &mpsc::Sender<Handover>) -> Result<(), Gone> {
        let mut last_ping = Instant::now();
        loop {
            let deadline = tokio::time::Instant::from_std(last_ping + PING_EVERY);
            let wake = tokio::select! {
                frame = socket.next() => Wake::Frame(frame),
                _ = tokio::time::sleep_until(deadline) => Wake::Ping,
            };
            let step = match wake {
                Wake::Ping => match send(&mut socket, r#"{"op":"ping"}"#.to_string()).await {
                    Ok(()) => Step::Pinged,
                    Err(e) => Step::Dropped(e.to_string()),
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
                Step::Pinged => last_ping = Instant::now(),
                Step::Text(text) => {
                    let read = self.decoder.ingest(&text);
                    while let Some(update) = self.decoder.pending.pop_front() {
                        hand_over(tx, Ok(update)).await?;
                    }
                    if let Err(e) = read {
                        // A refused op leaves a socket that will never carry
                        // what we asked for; one unreadable frame does not.
                        let socket_is_useless = matches!(e, FeedError::Transport(_));
                        tracing::warn!(error = %e, "private stream frame");
                        hand_over(tx, Err(e)).await?;
                        if socket_is_useless {
                            return Ok(());
                        }
                    }
                }
                Step::Dropped(why) => {
                    tracing::warn!(why, "private stream dropped, reconnecting");
                    return Ok(());
                }
            }
        }
    }
}

/// Hand one item to the engine, waiting if it is behind.
async fn hand_over(tx: &mpsc::Sender<Handover>, item: Handover) -> Result<(), Gone> {
    tx.send(item).await.map_err(|_| Gone)
}

/// Frames in, updates out, plus the memory of which orders have already
/// acked. No socket and no clock but the receive stamp.
struct Decoder {
    ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>,
    pending: VecDeque<OrderUpdate>,
    acked: HashSet<String>,
    acked_order: VecDeque<String>,
}

impl Decoder {
    fn new(ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>) -> Self {
        Self {
            ids,
            pending: VecDeque::new(),
            acked: HashSet::new(),
            acked_order: VecDeque::new(),
        }
    }

    /// Turn one frame into updates and queue them.
    fn ingest(&mut self, text: &str) -> Result<(), FeedError> {
        let frame: Value = serde_json::from_str(text)
            .map_err(|e| FeedError::BadMessage(format!("{e}: {}", first_chars(text))))?;

        // Control replies (pong, subscribe acks) carry no topic.
        let Some(topic) = frame.get("topic").and_then(Value::as_str) else {
            if frame.get("success").and_then(Value::as_bool) == Some(false) {
                let why = frame.get("ret_msg").and_then(Value::as_str).unwrap_or("no reason");
                return Err(FeedError::Transport(format!("venue refused an op: {why}")));
            }
            return Ok(());
        };
        let rows = frame.get("data").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]);
        let recv_ns = mono_ns();

        for row in rows {
            let update = if topic.starts_with("order") {
                map_order_row(row, recv_ns)?
            } else if topic.starts_with("execution") {
                let ids = self.ids.read().expect("the symbol map lock is poisoned");
                map_execution_row(row, &|name: &str| ids.get(name).copied(), recv_ns)?
            } else {
                None
            };
            if let Some(update) = update {
                if let OrderUpdate::Ack(ref ack) = update {
                    // The venue repeats New on later changes; the engine
                    // hears one ack per order.
                    if !self.remember_ack(&ack.client_order_id) {
                        continue;
                    }
                }
                self.pending.push_back(update);
            }
        }
        Ok(())
    }

    /// False if this order has already been acked.
    fn remember_ack(&mut self, client_order_id: &str) -> bool {
        if !self.acked.insert(client_order_id.to_string()) {
            return false;
        }
        self.acked_order.push_back(client_order_id.to_string());
        while self.acked_order.len() > ACK_MEMORY {
            if let Some(old) = self.acked_order.pop_front() {
                self.acked.remove(&old);
            }
        }
        true
    }
}

enum Wake {
    Frame(Option<Result<Message, tokio_tungstenite::tungstenite::Error>>),
    Ping,
}

enum Step {
    Idle,
    Pinged,
    Text(String),
    Dropped(String),
}

async fn send(socket: &mut Socket, text: String) -> Result<(), FeedError> {
    socket
        .send(Message::text(text))
        .await
        .map_err(|e| FeedError::Transport(e.to_string()))
}

/// Read frames until the venue answers the auth op.
async fn await_auth(socket: &mut Socket) -> Result<(), FeedError> {
    let reply = tokio::time::timeout(AUTH_REPLY_TIMEOUT, async {
        while let Some(frame) = socket.next().await {
            let frame = frame.map_err(|e| FeedError::Transport(e.to_string()))?;
            let Message::Text(text) = frame else { continue };
            let value: Value = serde_json::from_str(text.as_str())
                .map_err(|e| FeedError::BadMessage(e.to_string()))?;
            if value.get("op").and_then(Value::as_str) == Some("auth") {
                return Ok(value);
            }
        }
        // The venue hung up mid-handshake. That is a hiccup to dial through,
        // not the end of the feed.
        Err(FeedError::Transport(
            "the venue closed the socket during auth".to_string(),
        ))
    })
    .await
    .map_err(|_| FeedError::Transport("no auth reply from the venue".to_string()))??;

    if reply.get("success").and_then(Value::as_bool) == Some(true) {
        return Ok(());
    }
    let why = reply.get("ret_msg").and_then(Value::as_str).unwrap_or("no reason");
    Err(FeedError::Transport(format!("private stream auth refused: {why}")))
}

/// One `order` row. `None` for rows that say nothing the engine acts on.
pub(crate) fn map_order_row(row: &Value, recv_ns: u64) -> Result<Option<OrderUpdate>, FeedError> {
    let client_order_id = field(row, "orderLinkId")?;
    // No link id: someone else's order on this account.
    if client_order_id.is_empty() {
        return Ok(None);
    }
    let status = field(row, "orderStatus")?;
    let update = match status.as_str() {
        "New" | "Untriggered" => OrderUpdate::Ack(OrderAck {
            client_order_id,
            venue_order_id: field(row, "orderId")?,
            ack_ns: recv_ns,
        }),
        "Cancelled" | "Deactivated" => OrderUpdate::Cancelled {
            client_order_id,
            recv_ns,
        },
        "Rejected" => OrderUpdate::Reject {
            client_order_id,
            // The stream names the reason but carries no numeric code.
            code: 0,
            reason: row
                .get("rejectReason")
                .and_then(Value::as_str)
                .unwrap_or("rejected")
                .to_string(),
        },
        // Fills arrive on the execution topic, with the price and the fee.
        _ => return Ok(None),
    };
    Ok(Some(update))
}

/// One `execution` row.
pub(crate) fn map_execution_row(
    row: &Value,
    resolve: &dyn Fn(&str) -> Option<SymbolId>,
    recv_ns: u64,
) -> Result<Option<OrderUpdate>, FeedError> {
    // Funding and settlement rows also land here; they move money without
    // being fills, and the engine would double-count them as trades.
    let exec_type = field(row, "execType")?;
    if !matches!(exec_type.as_str(), "Trade" | "AdlTrade" | "BustTrade") {
        return Ok(None);
    }
    let client_order_id = field(row, "orderLinkId")?;
    if client_order_id.is_empty() {
        return Ok(None);
    }
    let symbol_name = field(row, "symbol")?;
    let symbol = resolve(&symbol_name).ok_or_else(|| {
        FeedError::BadMessage(format!(
            "fill on {symbol_name}, which the engine does not know"
        ))
    })?;
    let side = match field(row, "side")?.as_str() {
        "Buy" => Side::Buy,
        "Sell" => Side::Sell,
        other => {
            return Err(FeedError::BadMessage(format!(
                "fill on {symbol_name} has an unknown side {other:?}"
            )))
        }
    };
    Ok(Some(OrderUpdate::Fill {
        client_order_id,
        symbol,
        side,
        qty: number(row, "execQty")?,
        px: number(row, "execPrice")?,
        // A maker rebate comes back negative; it is a fee either way.
        fee: opt_num_field(row, "execFee").map_err(bad_field)?.unwrap_or(0.0),
        // Absent means taker. The venue sends this on every execution, so an
        // absent one is a message shape we do not know — and the expensive
        // side is the safe thing to assume about a fill we cannot classify.
        is_maker: row.get("isMaker").and_then(Value::as_bool).unwrap_or(false),
        venue_ts_ms: number(row, "execTime")? as i64,
        recv_ns,
    }))
}

fn field(row: &Value, name: &str) -> Result<String, FeedError> {
    str_field(row, name).map_err(bad_field)
}

fn number(row: &Value, name: &str) -> Result<f64, FeedError> {
    num_field(row, name).map_err(bad_field)
}

fn bad_field(e: VenueError) -> FeedError {
    FeedError::BadMessage(e.to_string())
}

fn first_chars(text: &str) -> String {
    text.chars().take(200).collect()
}

#[cfg(test)]
mod tests {
    /// A decoder that knows these symbols, ids by position — the same mapping
    /// the live feed starts with.
    fn decoder_for(symbols: &[&str]) -> super::Decoder {
        use super::*;
        let ids: HashMap<Symbol, SymbolId> = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.to_string(), SymbolId(i as u16)))
            .collect();
        Decoder::new(Arc::new(RwLock::new(ids)))
    }

    use super::*;
    use serde_json::json;

    fn resolve(name: &str) -> Option<SymbolId> {
        match name {
            "BTCUSDT" => Some(SymbolId(0)),
            "ETHUSDT" => Some(SymbolId(1)),
            _ => None,
        }
    }

    #[test]
    fn a_new_order_becomes_an_ack() {
        let row = json!({
            "symbol": "BTCUSDT",
            "orderId": "8ea1c8e1-0000-4000-9000-000000000001",
            "orderLinkId": "eng-42",
            "side": "Buy",
            "orderType": "Limit",
            "orderStatus": "New",
            "price": "95000",
            "qty": "0.01",
            "cumExecQty": "0",
            "category": "linear"
        });
        match map_order_row(&row, 777).unwrap().unwrap() {
            OrderUpdate::Ack(ack) => {
                assert_eq!(ack.client_order_id, "eng-42");
                assert_eq!(ack.venue_order_id, "8ea1c8e1-0000-4000-9000-000000000001");
                assert_eq!(ack.ack_ns, 777);
            }
            other => panic!("expected Ack, got {other:?}"),
        }
    }

    #[test]
    fn an_untriggered_conditional_order_also_acks() {
        let row = json!({
            "orderId": "abc", "orderLinkId": "eng-1", "orderStatus": "Untriggered"
        });
        assert!(matches!(
            map_order_row(&row, 1).unwrap(),
            Some(OrderUpdate::Ack(_))
        ));
    }

    #[test]
    fn cancelled_and_deactivated_both_mean_cancelled() {
        for status in ["Cancelled", "Deactivated"] {
            let row = json!({"orderId": "abc", "orderLinkId": "eng-9", "orderStatus": status});
            match map_order_row(&row, 5).unwrap().unwrap() {
                OrderUpdate::Cancelled { client_order_id, recv_ns } => {
                    assert_eq!(client_order_id, "eng-9");
                    assert_eq!(recv_ns, 5);
                }
                other => panic!("expected Cancelled for {status}, got {other:?}"),
            }
        }
    }

    #[test]
    fn a_rejected_order_carries_the_venue_reason() {
        let row = json!({
            "orderId": "abc", "orderLinkId": "eng-3", "orderStatus": "Rejected",
            "rejectReason": "EC_PostOnlyWillTakeLiquidity"
        });
        match map_order_row(&row, 1).unwrap().unwrap() {
            OrderUpdate::Reject { client_order_id, reason, .. } => {
                assert_eq!(client_order_id, "eng-3");
                assert_eq!(reason, "EC_PostOnlyWillTakeLiquidity");
            }
            other => panic!("expected Reject, got {other:?}"),
        }
    }

    #[test]
    fn filled_and_partially_filled_say_nothing_here() {
        for status in ["Filled", "PartiallyFilled", "Triggered"] {
            let row = json!({"orderId": "abc", "orderLinkId": "eng-4", "orderStatus": status});
            assert!(map_order_row(&row, 1).unwrap().is_none(), "{status}");
        }
    }

    #[test]
    fn an_order_without_our_link_id_is_not_ours() {
        let row = json!({"orderId": "abc", "orderLinkId": "", "orderStatus": "New"});
        assert!(map_order_row(&row, 1).unwrap().is_none());
    }

    #[test]
    fn an_execution_becomes_a_fill() {
        // Shape taken from the venue's own example push.
        let row = json!({
            "execId": "0ab1bdf7-4219-438b-b30a-32ec863018f7",
            "execPrice": "95900.1",
            "execQty": "0.5",
            "execFee": "26.3725275",
            "execTime": "1746270400353",
            "orderLinkId": "eng-11",
            "orderId": "9aac161b-8ed6-450d-9cab-c5cc67c21784",
            "symbol": "BTCUSDT",
            "side": "Sell",
            "execType": "Trade",
            "feeRate": "0.00055",
            "closedSize": "0.5"
        });
        match map_execution_row(&row, &resolve, 99).unwrap().unwrap() {
            OrderUpdate::Fill {
                client_order_id,
                symbol,
                side,
                qty,
                px,
                fee,
                is_maker,
                venue_ts_ms,
                recv_ns,
            } => {
                assert_eq!(client_order_id, "eng-11");
                assert_eq!(symbol, SymbolId(0));
                assert_eq!(side, Side::Sell);
                assert_eq!(qty, 0.5);
                assert_eq!(px, 95900.1);
                assert_eq!(fee, 26.3725275);
                assert!(!is_maker, "this row does not say it rested");
                assert_eq!(venue_ts_ms, 1_746_270_400_353);
                assert_eq!(recv_ns, 99);
            }
            other => panic!("expected Fill, got {other:?}"),
        }
    }

    #[test]
    fn the_venue_says_which_side_of_the_spread_we_were_on() {
        // Earning the spread and paying it are the same fill until this flag
        // is read. Bybit sends it on every execution.
        let resolve = |name: &str| (name == "BTCUSDT").then_some(SymbolId(0));
        let row = |is_maker: bool| {
            json!({
                "execPrice": "95900.1", "execQty": "0.5", "execFee": "26.37",
                "execTime": "1746270400353", "orderLinkId": "eng-11",
                "symbol": "BTCUSDT", "side": "Sell", "execType": "Trade",
                "isMaker": is_maker
            })
        };
        let maker_flag = |is_maker: bool| match map_execution_row(&row(is_maker), &resolve, 1)
            .unwrap()
            .unwrap()
        {
            OrderUpdate::Fill { is_maker, .. } => is_maker,
            other => panic!("expected Fill, got {other:?}"),
        };
        assert!(maker_flag(true), "we rested and somebody came to us");
        assert!(!maker_flag(false), "we crossed the spread");
    }

    #[test]
    fn funding_and_settlement_rows_are_not_fills() {
        for exec_type in ["Funding", "Settle", "SessionSettlePnl", "BustTrade"] {
            let row = json!({
                "execPrice": "1", "execQty": "1", "execFee": "0", "execTime": "1",
                "orderLinkId": "eng-1", "symbol": "BTCUSDT", "side": "Buy",
                "execType": exec_type
            });
            let mapped = map_execution_row(&row, &resolve, 1).unwrap();
            if exec_type == "BustTrade" {
                assert!(mapped.is_some(), "{exec_type} moves the position");
            } else {
                assert!(mapped.is_none(), "{exec_type} is not a fill");
            }
        }
    }

    #[test]
    fn a_fill_on_an_unknown_symbol_is_a_bad_message() {
        let row = json!({
            "execPrice": "1", "execQty": "1", "execFee": "0", "execTime": "1",
            "orderLinkId": "eng-1", "symbol": "VANRYUSDT", "side": "Buy", "execType": "Trade"
        });
        match map_execution_row(&row, &resolve, 1) {
            Err(FeedError::BadMessage(msg)) => assert!(msg.contains("VANRYUSDT"), "{msg}"),
            other => panic!("expected BadMessage, got {other:?}"),
        }
    }

    #[test]
    fn a_missing_fee_reads_as_zero_but_a_missing_price_does_not() {
        let base = json!({
            "execQty": "1", "execTime": "1", "orderLinkId": "eng-1",
            "symbol": "BTCUSDT", "side": "Buy", "execType": "Trade"
        });
        let mut no_fee = base.clone();
        no_fee["execPrice"] = json!("100");
        match map_execution_row(&no_fee, &resolve, 1).unwrap().unwrap() {
            OrderUpdate::Fill { fee, .. } => assert_eq!(fee, 0.0),
            other => panic!("expected Fill, got {other:?}"),
        }
        assert!(map_execution_row(&base, &resolve, 1).is_err());
    }

    #[test]
    fn a_whole_frame_queues_every_row_once() {
        let mut feed = decoder_for(&["BTCUSDT"]);
        let frame = json!({
            "topic": "order",
            "id": "test",
            "creationTime": 1672364262474i64,
            "data": [
                {"orderId": "a", "orderLinkId": "eng-1", "orderStatus": "New"},
                {"orderId": "b", "orderLinkId": "eng-2", "orderStatus": "New"},
                {"orderId": "a", "orderLinkId": "eng-1", "orderStatus": "New"}
            ]
        })
        .to_string();
        feed.ingest(&frame).unwrap();
        // Three rows in, two orders out: eng-1 acks once.
        assert_eq!(feed.pending.len(), 2);

        // A later frame repeating New for eng-1 adds nothing.
        feed.ingest(&frame).unwrap();
        assert_eq!(feed.pending.len(), 2);
    }

    #[test]
    fn control_frames_are_ignored_and_failures_surface() {
        let mut feed = decoder_for(&["BTCUSDT"]);
        feed.ingest(r#"{"op":"pong","success":true,"ret_msg":"pong"}"#).unwrap();
        feed.ingest(r#"{"op":"subscribe","success":true}"#).unwrap();
        assert!(feed.pending.is_empty());

        let err = feed.ingest(r#"{"op":"subscribe","success":false,"ret_msg":"bad topic"}"#);
        assert!(matches!(err, Err(FeedError::Transport(_))));
        assert!(feed.ingest("not json at all").is_err());
    }

    #[test]
    fn ack_memory_stays_bounded() {
        let mut feed = decoder_for(&["BTCUSDT"]);
        for i in 0..(ACK_MEMORY + 100) {
            assert!(feed.remember_ack(&format!("eng-{i}")));
        }
        assert_eq!(feed.acked.len(), ACK_MEMORY);
        assert_eq!(feed.acked_order.len(), ACK_MEMORY);
    }
}
