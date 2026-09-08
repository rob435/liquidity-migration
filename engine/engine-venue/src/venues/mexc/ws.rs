//! MEXC's private side: the account's own orders and fills, live on the
//! venue's one websocket, with a paced resync behind them.
//!
//! **The socket lives in its own task.** The engine waits on this feed inside
//! a `select!`, which drops the losing branch's future every flush tick, so a
//! login handshake held inside `next_update` would be thrown away several
//! times a second. The dial, the login, the filter, the ping schedule and the
//! reconnect loop belong to a task nobody cancels; `next_update` is a channel
//! receive, which loses nothing when it is dropped.
//!
//! **The resync never stops.** MEXC's own execution history
//! ([`super::gateway::MexcGateway::executions`]) is what the engine
//! reconciles against, so [`OrderUpdate::StreamReset`] goes out every
//! [`CONNECTED_RESYNC`] while the socket is up and every [`DEGRADED_RESYNC`]
//! while it is not. A refused login therefore costs latency, not fills: the
//! feed falls back to the paced resync and keeps redialling underneath it.
//!
//! **Quantities on this socket are contract counts.** `vol` is multiplied by
//! the contract size before it is a fill, and the venue names a contract
//! `BTC_USDT` where the engine says `BTCUSDT`. Both the multiplier and the id
//! arrive through [`OrderFeed::learn_instrument`], never through `learn`.

use crate::stream::{hand_over, until_closed, AckMemory, Gone, Handover, ReconnectBackoff};
use crate::RealmCredentials;
use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use engine_public::numeric_wire::DecimalField;
use engine_types::ids::SymbolId;
use engine_types::market::{FeedError, OrderFeed};
use engine_types::numeric::{
    AssetAmount, AssetId, Exact, ExactInstrumentSpec, ExactNumber, ExecutionAmounts,
};
use engine_types::orders::{ForcedClose, OrderAck, OrderUpdate};
use engine_types::VenueError;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::{Message, Utf8Bytes};
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use super::realm::MexcRealm;
use super::sign::rest_signature;
use crate::creds::Credentials;
use crate::wire::{self, Field, Id, IntegerField, RawField};
use crate::{mono_ns, wall_ms};

/// The venue closes a socket it has heard nothing on for 60 seconds, and asks
/// for a ping every 10-20.
const PING_EVERY: Duration = Duration::from_secs(15);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const LOGIN_REPLY_TIMEOUT: Duration = Duration::from_secs(10);
const SOCKET_WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const PONG_TIMEOUT: Duration = Duration::from_secs(10);

/// How often the engine is told to re-read while the socket is up. The socket
/// is the fast path; the venue's execution history stays the authority, and
/// this is how often it is consulted anyway.
pub const CONNECTED_RESYNC: Duration = Duration::from_secs(60);

/// The cadence while there is no socket — the whole feed when a login is
/// refused. It sits inside MEXC's own budget for the history call it
/// triggers: 20 requests per 2 seconds, per symbol.
pub const DEGRADED_RESYNC: Duration = Duration::from_secs(5);

/// How many client order ids to remember so one order acks once. Well past
/// anything in flight, and it keeps a long run's memory flat.
const ACK_MEMORY: usize = 8192;
/// Room for a burst of updates while the engine is busy with something else.
const CHANNEL_DEPTH: usize = 1024;

const PING_FRAME: &str = r#"{"method":"ping"}"#;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// One contract, as [`OrderFeed::learn_instrument`] stated it.
#[derive(Clone)]
struct Learned {
    id: SymbolId,
    /// Base coin per contract: the multiplier every `vol` on this socket goes
    /// through.
    contract_size: Exact,
    settlement_asset: AssetId,
}

pub struct MexcOrderFeed {
    url: String,
    creds: Credentials,
    /// The venue's contract names to what the decoder needs to read a fill on
    /// them. The engine writes here as it admits symbols; the socket task
    /// reads. A lock rather than a channel because a fill must be decodable
    /// the instant the engine says it is following the contract.
    contracts: Arc<RwLock<HashMap<String, Learned>>>,
    updates: Option<mpsc::Receiver<Handover>>,
}

impl MexcOrderFeed {
    /// The live feed: the realm's websocket and the realm's credentials from
    /// the environment. MEXC's only realm is funded, so this fails here, at
    /// boot, unless the owner has armed `REAL_MONEY`.
    pub fn new(realm: MexcRealm) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        Ok(Self::build(realm.websocket(), creds))
    }

    /// Point the feed at a local server. Tests only.
    pub fn for_test(url: &str, creds: Credentials) -> Self {
        Self::build(url, creds)
    }

    fn build(url: &str, creds: Credentials) -> Self {
        MexcOrderFeed {
            url: url.to_string(),
            creds,
            contracts: Arc::new(RwLock::new(HashMap::new())),
            updates: None,
        }
    }

    /// Start the socket task. The first `next_update` does this, because that
    /// is the first moment there is certain to be a runtime to spawn on.
    fn start(&mut self) {
        let (tx, rx) = mpsc::channel(CHANNEL_DEPTH);
        let worker = Worker {
            url: self.url.clone(),
            creds: self.creds.clone(),
            decoder: Decoder::new(self.contracts.clone()),
            backoff: ReconnectBackoff::default(),
            resync: Resync {
                next: Instant::now() + DEGRADED_RESYNC,
                announced: false,
            },
        };
        tokio::spawn(worker.run(tx));
        self.updates = Some(rx);
    }
}

impl OrderFeed for MexcOrderFeed {
    fn learn_instrument(&mut self, id: SymbolId, spec: &ExactInstrumentSpec) {
        let Some(contract_size) = spec
            .contract_multiplier
            .clone()
            .or_else(|| spec.qty_step.clone())
        else {
            return;
        };
        self.contracts
            .write()
            .expect("the contract map lock is poisoned")
            .insert(
                spec.native_symbol.clone(),
                Learned {
                    id,
                    contract_size,
                    settlement_asset: spec.settlement_asset.clone(),
                },
            );
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

/// The socket task: dial, log in, filter, read, reconnect — and raise the
/// paced resync throughout, including while there is no socket at all.
struct Worker {
    url: String,
    creds: Credentials,
    decoder: Decoder,
    backoff: ReconnectBackoff,
    resync: Resync,
}

struct Resync {
    /// When the next paced [`OrderUpdate::StreamReset`] is owed. One deadline
    /// carries both cadences: a live socket pushes it out to
    /// [`CONNECTED_RESYNC`], losing one brings it back to
    /// [`DEGRADED_RESYNC`].
    next: Instant,
    /// Whether the engine has had a reset yet. Until it has, a failed dial is
    /// logged and not handed over: `OrderFeeds::await_ready` reads anything
    /// before the first reset as a boot failure, and a venue that is briefly
    /// unreachable is not one here — the paced resync is a working feed on
    /// its own.
    announced: bool,
}

impl Worker {
    async fn run(mut self, tx: mpsc::Sender<Handover>) {
        until_closed(&tx, self.reconnect_forever(&tx)).await;
        tracing::info!("mexc private stream task finished; the engine let the feed go");
    }

    /// Keep a socket up for as long as the engine is listening.
    ///
    /// Nothing the venue does during a handshake is final. A refused login is
    /// most likely a key without futures permission, which will go on failing
    /// and go on saying so in the log; retiring the feed instead would cost
    /// the resync as well, which is the part that still works.
    async fn reconnect_forever(&mut self, tx: &mpsc::Sender<Handover>) -> Gone {
        loop {
            let dial = Self::connect(&self.url, &self.creds);
            let dialled = match paced(&mut self.resync, tx, dial).await {
                Ok(dialled) => dialled,
                Err(Gone) => return Gone,
            };
            match dialled {
                Ok(socket) => {
                    let opened = Instant::now();
                    self.resync.next = opened + CONNECTED_RESYNC;
                    self.resync.announced = true;
                    // Both the first-connection readiness watermark and the
                    // reconnect gap marker. It goes out before any frame from
                    // the new socket, so boot can establish the stream before
                    // taking REST snapshots and a running engine can recover
                    // history before accepting more risk.
                    let reset = OrderUpdate::StreamReset { recv_ns: mono_ns() };
                    if hand_over(tx, Ok(reset)).await.is_err() {
                        return Gone;
                    }
                    if self.pump(socket, tx).await.is_err() {
                        return Gone;
                    }
                    self.resync.next = Instant::now() + DEGRADED_RESYNC;
                    self.backoff.completed_session(opened.elapsed());
                }
                Err(e) => {
                    tracing::warn!(error = %e, "mexc private stream did not come up; trying again");
                    if self.resync.announced && hand_over(tx, Err(e)).await.is_err() {
                        return Gone;
                    }
                }
            }
            let wait = self.backoff.wait();
            if paced(&mut self.resync, tx, wait).await.is_err() {
                return Gone;
            }
        }
    }

    async fn connect(url: &str, creds: &Credentials) -> Result<Socket, FeedError> {
        crate::tls::install_crypto_provider();

        // Nagle off: an order update held back for coalescing is an order
        // update arriving late.
        let connected =
            tokio::time::timeout(CONNECT_TIMEOUT, connect_async_with_config(url, None, true))
                .await
                .map_err(|_| {
                    FeedError::Transport("mexc private stream dial timed out".to_string())
                })?;
        let (mut socket, _) = connected.map_err(|e| FeedError::Transport(e.to_string()))?;

        let req_time = wall_ms();
        // Signed string is `apiKey + reqTime`, which is the REST payload rule
        // with an empty body. `subscribe` sits beside `method` and not inside
        // `param`, where the venue's own example puts it; it cancels the
        // default push of every private channel, and the filter below then
        // names the two this engine reads.
        let login = json!({
            "subscribe": false,
            "method": "login",
            "param": {
                "apiKey": creds.key(),
                "reqTime": req_time.to_string(),
                "signature": rest_signature(creds.secret(), creds.key(), req_time, ""),
            },
        });
        send(&mut socket, login.to_string()).await?;
        await_login(&mut socket).await?;

        // Empty rules is every contract. The venue documents no reply to
        // this, so nothing waits for one.
        let filter = json!({
            "method": "personal.filter",
            "param": {"filters": [
                {"filter": "order", "rules": []},
                {"filter": "order.deal", "rules": []},
            ]},
        });
        send(&mut socket, filter.to_string()).await?;
        tracing::info!("mexc private stream logged in and filtered");
        Ok(socket)
    }

    /// Read one socket until it dies. `Err(Gone)` means the feed was dropped;
    /// `Ok` means dial again.
    async fn pump(&mut self, mut socket: Socket, tx: &mpsc::Sender<Handover>) -> Result<(), Gone> {
        let mut next_ping_at = Instant::now() + PING_EVERY;
        let mut pong_deadline: Option<Instant> = None;
        loop {
            let mut deadline = next_ping_at.min(self.resync.next);
            if let Some(pong) = pong_deadline {
                deadline = deadline.min(pong);
            }
            let wake = tokio::select! {
                frame = socket.next() => Wake::Frame(frame),
                _ = tokio::time::sleep_until(deadline) => Wake::Timer,
            };
            let step = match wake {
                Wake::Timer if pong_deadline.is_some_and(|deadline| Instant::now() >= deadline) => {
                    Step::Dropped("mexc private stream keep-alive unanswered".to_string())
                }
                Wake::Timer if Instant::now() >= self.resync.next => Step::Resync,
                Wake::Timer => match send(&mut socket, PING_FRAME.to_string()).await {
                    Ok(()) => Step::Pinged,
                    Err(e) => Step::Dropped(e.to_string()),
                },
                Wake::Frame(Some(Ok(Message::Text(text)))) => Step::Text(text),
                Wake::Frame(Some(Ok(Message::Ping(payload)))) => {
                    match send_message(&mut socket, Message::Pong(payload)).await {
                        Ok(()) => Step::Idle,
                        Err(e) => Step::Dropped(e.to_string()),
                    }
                }
                Wake::Frame(Some(Ok(Message::Pong(_)))) => Step::Ponged,
                Wake::Frame(Some(Ok(Message::Close(_)))) => {
                    Step::Dropped("the venue closed the socket".to_string())
                }
                Wake::Frame(Some(Ok(_))) => Step::Idle,
                Wake::Frame(Some(Err(e))) => Step::Dropped(e.to_string()),
                Wake::Frame(None) => Step::Dropped("stream ended".to_string()),
            };

            match step {
                Step::Idle => (),
                Step::Resync => {
                    self.resync.next = Instant::now() + CONNECTED_RESYNC;
                    let reset = OrderUpdate::StreamReset { recv_ns: mono_ns() };
                    hand_over(tx, Ok(reset)).await?;
                }
                Step::Pinged => {
                    let now = Instant::now();
                    next_ping_at = now + PING_EVERY;
                    pong_deadline = Some(now + PONG_TIMEOUT);
                }
                Step::Ponged => pong_deadline = None,
                Step::Text(text) => {
                    let read = self.decoder.ingest(text.as_str());
                    if matches!(&read, Ok(true)) {
                        pong_deadline = None;
                    }
                    while let Some(update) = self.decoder.pending.pop_front() {
                        hand_over(tx, Ok(update)).await?;
                    }
                    if let Err(e) = read {
                        tracing::warn!(error = %e, "mexc private stream frame");
                        hand_over(tx, Err(e)).await?;
                        // Once any account frame is unreadable, continuing on
                        // this socket provides no recovery watermark. Redial
                        // so the next successful login emits a reset and
                        // history closes the unknown interval.
                        return Ok(());
                    }
                }
                Step::Dropped(why) => {
                    tracing::warn!(why, "mexc private stream dropped, reconnecting");
                    hand_over(
                        tx,
                        Err(FeedError::Transport(format!(
                            "mexc private stream unavailable: {why}"
                        ))),
                    )
                    .await?;
                    return Ok(());
                }
            }
        }
    }
}

/// Run `work` with the paced resync still going. Only the engine dropping the
/// feed cuts it short.
async fn paced<T>(
    resync: &mut Resync,
    tx: &mpsc::Sender<Handover>,
    work: impl Future<Output = T>,
) -> Result<T, Gone> {
    tokio::pin!(work);
    loop {
        tokio::select! {
            done = &mut work => return Ok(done),
            _ = tokio::time::sleep_until(resync.next) => {
                resync.next = Instant::now() + DEGRADED_RESYNC;
                resync.announced = true;
                hand_over(tx, Ok(OrderUpdate::StreamReset { recv_ns: mono_ns() })).await?;
            }
        }
    }
}

enum Wake {
    Frame(Option<Result<Message, tokio_tungstenite::tungstenite::Error>>),
    Timer,
}

enum Step {
    Idle,
    Pinged,
    Ponged,
    Resync,
    Text(Utf8Bytes),
    Dropped(String),
}

async fn send(socket: &mut Socket, text: String) -> Result<(), FeedError> {
    send_message(socket, Message::text(text)).await
}

async fn send_message(socket: &mut Socket, message: Message) -> Result<(), FeedError> {
    tokio::time::timeout(SOCKET_WRITE_TIMEOUT, socket.send(message))
        .await
        .map_err(|_| FeedError::Transport("mexc private stream write timed out".to_string()))?
        .map_err(|e| FeedError::Transport(e.to_string()))
}

/// Read frames until the venue answers the login.
async fn await_login(socket: &mut Socket) -> Result<(), FeedError> {
    tokio::time::timeout(LOGIN_REPLY_TIMEOUT, async {
        while let Some(frame) = socket.next().await {
            let frame = frame.map_err(|e| FeedError::Transport(e.to_string()))?;
            let text = match frame {
                Message::Ping(payload) => {
                    send_message(socket, Message::Pong(payload)).await?;
                    continue;
                }
                Message::Text(text) => text,
                _ => continue,
            };
            let value: Value = serde_json::from_str(text.as_str())
                .map_err(|e| FeedError::BadMessage(e.to_string()))?;
            let said = value.get("data").map(render).unwrap_or_default();
            match value.get("channel").and_then(Value::as_str) {
                Some("rs.login") if said == "success" => return Ok(()),
                Some("rs.login") | Some("rs.error") => {
                    return Err(FeedError::Transport(format!(
                        "mexc refused the private login: {said}"
                    )))
                }
                _ => continue,
            }
        }
        // The venue hung up mid-handshake. That is a hiccup to dial through,
        // not the end of the feed.
        Err(FeedError::Transport(
            "the venue closed the socket during login".to_string(),
        ))
    })
    .await
    .map_err(|_| FeedError::Transport("no login reply from mexc".to_string()))?
}

#[derive(Default, Deserialize)]
struct Envelope {
    #[serde(default)]
    channel: Field<String>,
    #[serde(default)]
    data: RawField<Box<serde_json::value::RawValue>>,
}

/// One `push.personal.order` row.
#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct OrderRow {
    order_id: Field<Id>,
    external_oid: Field<String>,
    state: IntegerField,
    deal_vol: DecimalField,
    error_code: IntegerField,
}

/// One `push.personal.order.deal` row.
#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct DealRow {
    id: Field<Id>,
    symbol: Field<String>,
    side: IntegerField,
    vol: DecimalField,
    price: DecimalField,
    fee: DecimalField,
    /// The socket spells the maker/taker flag `isTaker`; the REST history
    /// endpoint spells the same flag `taker`. Both are read, and a fill
    /// stating neither is not decoded.
    is_taker: Field<bool>,
    taker: Field<bool>,
    category: IntegerField,
    external_oid: Field<Value>,
    timestamp: IntegerField,
}

/// Frames in, updates out, plus the memory of which orders have already
/// acked. No socket and no clock but the receive stamp.
struct Decoder {
    contracts: Arc<RwLock<HashMap<String, Learned>>>,
    pending: VecDeque<OrderUpdate>,
    acknowledgements: AckMemory,
}

impl Decoder {
    fn new(contracts: Arc<RwLock<HashMap<String, Learned>>>) -> Self {
        Decoder {
            contracts,
            pending: VecDeque::new(),
            acknowledgements: AckMemory::new(ACK_MEMORY),
        }
    }

    /// Turn one frame into updates and queue them. `Ok(true)` for the venue's
    /// keep-alive answer.
    fn ingest(&mut self, text: &str) -> Result<bool, FeedError> {
        let frame: Envelope = wire::raw_object(text)
            .map_err(|e| FeedError::BadMessage(format!("{e}: {}", first_chars(text))))?;
        let Some(channel) = frame.channel.0.as_deref() else {
            return Ok(false);
        };
        let recv_ns = mono_ns();
        match channel {
            "pong" => return Ok(true),
            "rs.error" => {
                let why = frame
                    .data
                    .0
                    .as_deref()
                    .and_then(|raw| serde_json::from_str::<Value>(raw.get()).ok())
                    .map(|data| render(&data))
                    .unwrap_or_else(|| "no reason".to_string());
                return Err(FeedError::Transport(format!(
                    "mexc refused a private command: {why}"
                )));
            }
            "push.personal.order" => {
                if let Some(update) = map_order_row(payload(channel, &frame)?, recv_ns)? {
                    if let OrderUpdate::Ack(ref ack) = update {
                        // The venue republishes a working order on every
                        // change; the engine hears one ack per order.
                        if !self.acknowledgements.remember(&ack.client_order_id) {
                            return Ok(false);
                        }
                    }
                    self.pending.push_back(update);
                }
            }
            "push.personal.order.deal" => {
                let fill = self.map_deal_row(payload(channel, &frame)?, recv_ns)?;
                self.pending.push_back(fill);
            }
            // `rs.login`, the filter's acknowledgement, and every channel the
            // filter did not ask for.
            _ => (),
        }
        Ok(false)
    }

    fn map_deal_row(&self, raw: &str, recv_ns: u64) -> Result<OrderUpdate, FeedError> {
        let row: DealRow =
            wire::raw_object(raw).map_err(|e| FeedError::BadMessage(e.to_string()))?;
        let venue_symbol = row.symbol.required("symbol").map_err(bad_field)?;
        let contract = self
            .contracts
            .read()
            .expect("the contract map lock is poisoned")
            .get(venue_symbol)
            .cloned()
            .ok_or_else(|| {
                FeedError::BadMessage(format!(
                    "fill on {venue_symbol}, which the engine does not know"
                ))
            })?;
        let exec_id = row
            .id
            .0
            .map(Id::into_text)
            .filter(|id| !id.trim().is_empty())
            .ok_or_else(|| {
                FeedError::BadMessage(format!("a fill on {venue_symbol} has no readable id"))
            })?;
        let side_raw = row.side.required("side").map_err(bad_field)?;
        let (side, _) = super::parse::side_of(side_raw).ok_or_else(|| {
            FeedError::BadMessage(format!("fill {exec_id} has unknown side {side_raw}"))
        })?;
        let quantity = ExactNumber::derived(
            &row.vol.required("vol").map_err(bad_field)?.value * &contract.contract_size,
        );
        let qty = quantity
            .value
            .to_f64()
            .map_err(|e| FeedError::BadMessage(e.to_string()))?;
        let price = row.price.required("price").map_err(bad_field)?;
        let px = price
            .value
            .to_f64()
            .map_err(|e| FeedError::BadMessage(e.to_string()))?;
        let fee = row.fee.optional("fee").map_err(bad_field)?;
        let legacy_fee = fee
            .as_ref()
            .map(|fee| fee.value.to_f64())
            .transpose()
            .map_err(|e| FeedError::BadMessage(e.to_string()))?;
        let venue_ts_ms = row.timestamp.required("timestamp").map_err(bad_field)?;
        if qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
            return Err(FeedError::BadMessage(format!(
                "fill {exec_id} has non-positive quantity, price, or timestamp"
            )));
        }
        let taker = row.is_taker.0.or(row.taker.0).ok_or_else(|| {
            FeedError::BadMessage(format!("fill {exec_id} states no maker or taker flag"))
        })?;
        let client_order_id = match row.external_oid.0 {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(text)) => text,
            Some(other) => {
                return Err(FeedError::BadMessage(format!(
                    "fill {exec_id} has a non-string externalOid {other}"
                )))
            }
        };
        Ok(OrderUpdate::Fill {
            allocation: None,
            exec_id,
            client_order_id,
            symbol: contract.id,
            side,
            qty,
            px,
            fee: legacy_fee,
            amounts: Some(Box::new(ExecutionAmounts {
                settlement_asset: contract.settlement_asset.clone(),
                quantity,
                price,
                // The same asset the REST history path assigns, so one fill
                // read down both paths reconciles.
                fee: fee.map(|amount| AssetAmount {
                    asset: contract.settlement_asset.clone(),
                    amount,
                }),
            })),
            is_maker: !taker,
            forced_close: forced_close_of(&row.category),
            venue_ts_ms,
            recv_ns,
        })
    }
}

/// One `push.personal.order` row. `None` for rows that say nothing the engine
/// acts on, including every order that is not ours.
fn map_order_row(raw: &str, recv_ns: u64) -> Result<Option<OrderUpdate>, FeedError> {
    let row: OrderRow = wire::raw_object(raw).map_err(|e| FeedError::BadMessage(e.to_string()))?;
    let client_order_id = row.external_oid.text().to_string();
    // No id of ours: a hand trade, or a stop the venue placed on a position.
    if client_order_id.is_empty() {
        return Ok(None);
    }
    let state = row.state.required("state").map_err(bad_field)?;
    let update = match state {
        // Working. Anything already matched is the deal frame's news, with
        // the price and the fee on it.
        2 => {
            let filled = row.deal_vol.required("dealVol").map_err(bad_field)?;
            if !filled.value.is_zero() {
                return Ok(None);
            }
            let venue_order_id = row
                .order_id
                .0
                .map(Id::into_text)
                .filter(|id| !id.trim().is_empty())
                .ok_or_else(|| {
                    FeedError::BadMessage(format!(
                        "accepted order {client_order_id} carries no venue id"
                    ))
                })?;
            OrderUpdate::Ack(OrderAck {
                client_order_id,
                venue_order_id,
                sent_ns: 0,
                ack_ns: recv_ns,
            })
        }
        4 => OrderUpdate::Cancelled {
            client_order_id,
            recv_ns,
        },
        5 => {
            let code = row.error_code.required("errorCode").map_err(bad_field)?;
            OrderUpdate::Reject {
                client_order_id,
                code,
                // The venue names no reason on this channel, only the number.
                reason: format!("mexc marked the order invalid, errorCode {code}"),
            }
        }
        _ => return Ok(None),
    };
    Ok(Some(update))
}

/// The venue's own reason for a close, from `category`. 2 is the liquidation
/// engine and 4 is auto-deleveraging; 3, "custody close", names no variant
/// here rather than being asserted as one.
fn forced_close_of(category: &IntegerField) -> Option<ForcedClose> {
    match category {
        IntegerField::Number(2) => Some(ForcedClose::Liquidation),
        IntegerField::Number(4) => Some(ForcedClose::AutoDeleverage),
        _ => None,
    }
}

/// The `data` object of a push frame, still as raw bytes: the decimal lexemes
/// in it have to survive as far as [`DecimalField`].
fn payload<'a>(channel: &str, frame: &'a Envelope) -> Result<&'a str, FeedError> {
    let raw = frame
        .data
        .0
        .as_deref()
        .map(|raw| raw.get())
        .ok_or_else(|| FeedError::BadMessage(format!("{channel} frame has no data")))?;
    if !raw.trim_start().starts_with('{') {
        return Err(FeedError::BadMessage(format!(
            "{channel} frame data is not an object"
        )));
    }
    Ok(raw)
}

fn render(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn bad_field(e: VenueError) -> FeedError {
    FeedError::BadMessage(e.to_string())
}

fn first_chars(text: &str) -> String {
    text.chars().take(200).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::orders::Side;
    use serde_json::json;

    /// A decoder that knows `BTC_USDT` at the venue's real contract size.
    fn decoder() -> Decoder {
        Decoder::new(Arc::new(RwLock::new(HashMap::from([(
            "BTC_USDT".to_string(),
            Learned {
                id: SymbolId(0),
                contract_size: Exact::parse_decimal("0.0001").unwrap(),
                settlement_asset: AssetId::Named("USDT".into()),
            },
        )]))))
    }

    /// The venue's own `push.personal.order` example, with this engine's id
    /// on it.
    fn order_frame(overrides: Value) -> String {
        let mut frame = json!({"channel":"push.personal.order","data":{
            "orderId":123456789_i64, "symbol":"BTC_USDT", "positionId":987654321_i64,
            "price":45000.5, "vol":10, "leverage":20, "side":1, "category":1,
            "orderType":1, "dealAvgPrice":0, "dealVol":0, "orderMargin":2250.0,
            "usedMargin":0, "takerFee":0, "makerFee":0, "profit":0,
            "feeCurrency":"USDT", "openType":2, "state":2, "errorCode":0,
            "externalOid":"eng-1", "createTime":1760942212000_i64,
            "updateTime":1760942212000_i64, "remainVol":10, "positionMode":1,
            "reduceOnly":false
        },"ts":1760942212000_i64});
        for (key, value) in overrides.as_object().unwrap() {
            frame["data"][key] = value.clone();
        }
        frame.to_string()
    }

    /// The venue's own `push.personal.order.deal` example, at a contract size
    /// this engine trades.
    fn deal_frame(overrides: Value) -> String {
        let mut frame = json!({"channel":"push.personal.order.deal","data":{
            "id":987654321_i64, "symbol":"BTC_USDT", "side":1, "vol":3,
            "price":45000.5, "feeCurrency":"USDT", "fee":0.225,
            "timestamp":1760942212000_i64, "profit":0, "isTaker":false,
            "category":1, "orderId":123456789_i64, "isSelf":false,
            "externalOid":"eng-1", "positionMode":1, "reduceOnly":false
        },"ts":1760942212000_i64});
        for (key, value) in overrides.as_object().unwrap() {
            if value.is_null() {
                frame["data"].as_object_mut().unwrap().remove(key);
            } else {
                frame["data"][key] = value.clone();
            }
        }
        frame.to_string()
    }

    #[test]
    fn a_working_order_acknowledges_once() {
        let mut decoder = decoder();
        decoder.ingest(&order_frame(json!({}))).unwrap();
        match decoder.pending.pop_front() {
            Some(OrderUpdate::Ack(ack)) => {
                assert_eq!(ack.client_order_id, "eng-1");
                assert_eq!(ack.venue_order_id, "123456789");
                assert_eq!(ack.sent_ns, 0);
            }
            other => panic!("expected Ack, got {other:?}"),
        }
        // The venue republishes the order on every change.
        decoder.ingest(&order_frame(json!({}))).unwrap();
        assert!(decoder.pending.is_empty(), "the order acked twice");
    }

    #[test]
    fn a_working_order_that_has_already_traded_is_the_deal_frames_news() {
        let mut decoder = decoder();
        decoder.ingest(&order_frame(json!({"dealVol": 3}))).unwrap();
        assert!(decoder.pending.is_empty());
        // Filled and pending say nothing here either.
        for state in [1, 3] {
            decoder
                .ingest(&order_frame(json!({"state": state})))
                .unwrap();
            assert!(decoder.pending.is_empty(), "state {state}");
        }
    }

    #[test]
    fn a_cancelled_order_says_so() {
        let mut decoder = decoder();
        decoder.ingest(&order_frame(json!({"state": 4}))).unwrap();
        match decoder.pending.pop_front() {
            Some(OrderUpdate::Cancelled {
                client_order_id, ..
            }) => assert_eq!(client_order_id, "eng-1"),
            other => panic!("expected Cancelled, got {other:?}"),
        }
    }

    #[test]
    fn an_invalid_order_carries_the_venues_own_code() {
        // 20 is the post-only cancel: the order would have crossed.
        let mut decoder = decoder();
        decoder
            .ingest(&order_frame(json!({"state": 5, "errorCode": 20})))
            .unwrap();
        match decoder.pending.pop_front() {
            Some(OrderUpdate::Reject {
                client_order_id,
                code,
                reason,
            }) => {
                assert_eq!(client_order_id, "eng-1");
                assert_eq!(code, 20);
                assert!(reason.contains("20"), "{reason}");
            }
            other => panic!("expected Reject, got {other:?}"),
        }
    }

    #[test]
    fn an_order_without_our_id_is_not_ours() {
        let mut decoder = decoder();
        for oid in [json!(""), Value::Null] {
            decoder
                .ingest(&order_frame(json!({"externalOid": oid})))
                .unwrap();
            assert!(decoder.pending.is_empty());
        }
    }

    #[test]
    fn a_maker_fill_is_converted_out_of_contracts() {
        // Three contracts of BTC_USDT at 0.0001 each is 0.0003 BTC. Passing
        // the contract count through would be wrong by four orders of
        // magnitude.
        let mut decoder = decoder();
        decoder.ingest(&deal_frame(json!({}))).unwrap();
        match decoder.pending.pop_front() {
            Some(OrderUpdate::Fill {
                exec_id,
                client_order_id,
                symbol,
                side,
                qty,
                px,
                fee,
                amounts,
                is_maker,
                forced_close,
                venue_ts_ms,
                allocation,
                ..
            }) => {
                assert_eq!(exec_id, "987654321");
                assert_eq!(client_order_id, "eng-1");
                assert_eq!(symbol, SymbolId(0));
                assert_eq!(side, Side::Buy);
                assert_eq!(qty, 0.0003);
                assert_eq!(px, 45000.5);
                assert_eq!(fee, Some(0.225));
                assert!(is_maker, "isTaker false is a resting fill");
                assert_eq!(forced_close, None);
                assert_eq!(venue_ts_ms, 1_760_942_212_000);
                assert!(allocation.is_none());
                let amounts = amounts.unwrap();
                amounts.validate_projection(qty, px, fee).unwrap();
                assert_eq!(
                    amounts.quantity.value.to_decimal_string().unwrap(),
                    "0.0003"
                );
                assert_eq!(
                    amounts.fee.unwrap().asset,
                    AssetId::Named("USDT".to_string())
                );
            }
            other => panic!("expected Fill, got {other:?}"),
        }
    }

    #[test]
    fn a_fill_the_engine_never_ordered_keeps_the_venues_identity() {
        // A hand trade, or the venue's own stop firing under a position.
        let mut decoder = decoder();
        for oid in [Value::Null, json!(null)] {
            decoder
                .ingest(&deal_frame(json!({"externalOid": oid})))
                .unwrap();
            match decoder.pending.pop_front() {
                Some(OrderUpdate::Fill {
                    exec_id,
                    client_order_id,
                    ..
                }) => {
                    assert_eq!(exec_id, "987654321");
                    assert!(client_order_id.is_empty());
                }
                other => panic!("expected Fill, got {other:?}"),
            }
        }
    }

    #[test]
    fn both_spellings_of_the_taker_flag_read_the_same() {
        // The socket documents `isTaker`; the REST history endpoint the same
        // flag as `taker`. A fill stating neither is not decoded.
        let mut decoder = decoder();
        for (field, taker) in [("isTaker", true), ("taker", true), ("isTaker", false)] {
            let mut overrides = json!({"isTaker": null, "taker": null});
            overrides[field] = json!(taker);
            decoder.ingest(&deal_frame(overrides)).unwrap();
            match decoder.pending.pop_front() {
                Some(OrderUpdate::Fill { is_maker, .. }) => assert_eq!(is_maker, !taker),
                other => panic!("expected Fill, got {other:?}"),
            }
        }
        assert!(matches!(
            decoder.ingest(&deal_frame(json!({"isTaker": null, "taker": null}))),
            Err(FeedError::BadMessage(_))
        ));
    }

    #[test]
    fn the_venues_own_close_is_named_when_the_category_says_one() {
        let mut decoder = decoder();
        for (category, expected) in [
            (1, None),
            (2, Some(ForcedClose::Liquidation)),
            (3, None),
            (4, Some(ForcedClose::AutoDeleverage)),
        ] {
            decoder
                .ingest(&deal_frame(json!({"category": category})))
                .unwrap();
            match decoder.pending.pop_front() {
                Some(OrderUpdate::Fill { forced_close, .. }) => {
                    assert_eq!(forced_close, expected, "category {category}")
                }
                other => panic!("expected Fill, got {other:?}"),
            }
        }
    }

    #[test]
    fn a_malformed_deal_is_a_bad_message_and_never_a_guessed_fill() {
        for overrides in [
            json!({"id": null}),
            json!({"symbol": "ETH_USDT"}),
            json!({"symbol": null}),
            json!({"side": 9}),
            json!({"side": null}),
            json!({"vol": null}),
            json!({"vol": 0}),
            json!({"price": null}),
            json!({"price": 0}),
            json!({"price": "NaN"}),
            json!({"fee": "broken"}),
            json!({"timestamp": null}),
            json!({"timestamp": 0}),
            json!({"externalOid": 7}),
        ] {
            let mut decoder = decoder();
            let frame = deal_frame(overrides.clone());
            assert!(
                matches!(decoder.ingest(&frame), Err(FeedError::BadMessage(_))),
                "{overrides} was accepted"
            );
            assert!(decoder.pending.is_empty(), "{overrides} produced a fill");
        }
    }

    #[test]
    fn a_truncated_deal_frame_never_emits_a_partial_fill() {
        let frame = deal_frame(json!({}));
        for cut in 0..frame.len() {
            let mut decoder = decoder();
            let _ = decoder.ingest(&frame[..cut]);
            assert!(decoder.pending.is_empty(), "partial fill at {cut}");
        }
    }

    #[test]
    fn a_push_with_no_object_to_read_asks_for_recovery() {
        for channel in ["push.personal.order", "push.personal.order.deal"] {
            for data in [Value::Null, json!([]), json!("text"), json!(7)] {
                let mut decoder = decoder();
                let frame = json!({"channel": channel, "data": data}).to_string();
                assert!(
                    matches!(decoder.ingest(&frame), Err(FeedError::BadMessage(_))),
                    "{channel} silently ignored {data}"
                );
            }
            let mut decoder = decoder();
            assert!(decoder
                .ingest(&json!({"channel": channel}).to_string())
                .is_err());
        }
    }

    #[test]
    fn control_frames_are_recognized_and_refusals_surface() {
        let mut decoder = decoder();
        assert!(decoder
            .ingest(r#"{"channel":"pong","data":1760942212000}"#)
            .unwrap());
        for quiet in [
            r#"{"channel":"rs.login","data":"success","ts":"1587442022003"}"#,
            r#"{"channel":"push.personal.position","data":{"symbol":"BTC_USDT"}}"#,
            "null",
            "[]",
            r#"{"channel":7}"#,
        ] {
            assert!(!decoder.ingest(quiet).unwrap(), "{quiet}");
            assert!(decoder.pending.is_empty(), "{quiet}");
        }
        assert!(matches!(
            decoder.ingest(r#"{"channel":"rs.error","data":"Blocked","ts":1}"#),
            Err(FeedError::Transport(why)) if why.ends_with("Blocked")
        ));
        assert!(decoder.ingest("{not json").is_err());
    }

    #[test]
    fn ack_memory_stays_bounded() {
        let mut decoder = decoder();
        for i in 0..(ACK_MEMORY + 100) {
            assert!(decoder.acknowledgements.remember(&format!("eng-{i}")));
        }
        assert_eq!(decoder.acknowledgements.lengths(), (ACK_MEMORY, ACK_MEMORY));
    }

    #[tokio::test(start_paused = true)]
    async fn the_paced_resync_keeps_going_while_the_dial_does_not_finish() {
        // The whole feed while a login is refused: no socket, and the engine
        // still gets told to re-read on the old cadence.
        let (tx, mut rx) = mpsc::channel(8);
        let mut resync = Resync {
            next: Instant::now() + DEGRADED_RESYNC,
            announced: false,
        };
        let never = std::future::pending::<()>();
        let paced = paced(&mut resync, &tx, never);
        tokio::pin!(paced);
        for tick in 1..=3 {
            tokio::select! {
                _ = &mut paced => panic!("a pending dial finished"),
                _ = tokio::time::sleep(DEGRADED_RESYNC + Duration::from_millis(1)) => (),
            }
            assert!(
                matches!(rx.try_recv(), Ok(Ok(OrderUpdate::StreamReset { .. }))),
                "resync {tick} never came"
            );
        }
    }
}
