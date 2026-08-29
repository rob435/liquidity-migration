//! Hyperliquid's private stream: order and fill updates for one account.
//!
//! The socket carries everything that happens to the account, including orders
//! this engine did not place. Rows without a client id this engine minted are
//! not ours and are dropped — the account may be hand-traded too.
//!
//! **The socket lives in its own task**, for the same reason Bybit's does: the
//! engine waits on this feed inside a `select!`, which drops the losing
//! branch's future every flush tick, so anything half-finished inside
//! `next_update` would be thrown away several times a second. The dial, the
//! subscriptions, the ping schedule and the reconnect loop all belong to a task
//! nobody cancels, and `next_update` is nothing but a channel receive.
//!
//! **The first fill message is a snapshot and is dropped.** Subscribing to
//! `userFills` replays the account's recent fills, which the engine's log
//! already holds; delivering them would double-count every one. Fills missed
//! while the socket was down are recovered the way every other gap is — the
//! reconnect raises [`OrderUpdate::StreamReset`], and the engine refreshes its
//! account view and reads the venue's own history.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use engine_types::ids::{Symbol, SymbolId};
use engine_types::market::{FeedError, OrderFeed};
use engine_types::orders::{OrderAck, OrderUpdate};
use engine_types::VenueError;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};

use super::cloid;
use super::parse::parse_execution;
use super::realm::HyperliquidRealm;
use super::sign::{address_text, parse_address};
use crate::creds::Credentials;
use crate::json::int_field;
use crate::mono_ns;

/// The venue drops a socket that has said nothing for 60 seconds.
const PING_EVERY: Duration = Duration::from_secs(30);
const BACKOFF_START: Duration = Duration::from_millis(250);
const BACKOFF_CAP: Duration = Duration::from_secs(30);
/// A socket that lasted this long was a real connection, so the wait between
/// dials starts over.
const HEALTHY_AFTER: Duration = Duration::from_secs(30);
/// How many client order ids to remember so one order acks once.
const ACK_MEMORY: usize = 8192;
const CHANNEL_DEPTH: usize = 1024;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;
type Handover = Result<OrderUpdate, FeedError>;

pub struct HyperliquidOrderFeed {
    url: String,
    account: String,
    ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>,
    updates: Option<mpsc::Receiver<Handover>>,
}

impl HyperliquidOrderFeed {
    /// The live feed: the realm's socket, and the realm's account address from
    /// the environment. Like the gateway, mainnet fails here unless the owner
    /// has armed `REAL_MONEY`.
    pub fn new(realm: HyperliquidRealm, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        let creds = realm.credentials()?;
        Self::build(realm.websocket(), &creds, symbols)
    }

    /// Point the feed at a local server. Tests and the mock venue only.
    pub fn for_test(
        url: &str,
        creds: &Credentials,
        symbols: Vec<Symbol>,
    ) -> Result<Self, VenueError> {
        Self::build(url, creds, symbols)
    }

    fn build(url: &str, creds: &Credentials, symbols: Vec<Symbol>) -> Result<Self, VenueError> {
        // Read rather than trusted: the subscription is addressed by this
        // string, and a malformed one would subscribe to nothing and look
        // exactly like a quiet account.
        let account = address_text(parse_address(creds.key())?);
        let ids = symbols
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
            .collect();
        Ok(Self {
            url: url.to_string(),
            account,
            ids: Arc::new(RwLock::new(ids)),
            updates: None,
        })
    }

    /// Teach the decoder what id a symbol has. Idempotent, and a name already
    /// known keeps the id it had.
    pub fn learn(&mut self, symbol: &str, id: SymbolId) {
        let mut ids = self.ids.write().expect("the symbol map lock is poisoned");
        ids.entry(symbol.to_string()).or_insert(id);
    }

    fn start(&mut self) {
        let (tx, rx) = mpsc::channel(CHANNEL_DEPTH);
        let worker = Worker {
            url: self.url.clone(),
            account: self.account.clone(),
            decoder: Decoder::new(self.ids.clone()),
            backoff: Duration::ZERO,
        };
        tokio::spawn(worker.run(tx));
        self.updates = Some(rx);
    }
}

impl OrderFeed for HyperliquidOrderFeed {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        HyperliquidOrderFeed::learn(self, symbol, id);
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
    url: String,
    account: String,
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
                    // A fresh socket replays the fill snapshot, and the engine
                    // already holds those fills.
                    self.decoder.awaiting_snapshot = true;
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

    async fn connect(&self) -> Result<Socket, FeedError> {
        crate::tls::install_crypto_provider();

        // Nagle off: an order update held back for coalescing is an order
        // update arriving late.
        let (mut socket, _) = connect_async_with_config(self.url.as_str(), None, true)
            .await
            .map_err(|e| FeedError::Transport(e.to_string()))?;

        // Both subscriptions are per account, not per symbol, so a symbol the
        // engine starts following later already arrives.
        for topic in ["orderUpdates", "userFills"] {
            let subscribe = json!({
                "method": "subscribe",
                "subscription": {"type": topic, "user": self.account},
            });
            send(&mut socket, subscribe.to_string()).await?;
        }

        tracing::info!(account = %self.account, "private stream subscribed");
        Ok(socket)
    }

    async fn pump(&mut self, mut socket: Socket, tx: &mpsc::Sender<Handover>) -> Result<(), Gone> {
        let mut last_ping = Instant::now();
        loop {
            let deadline = tokio::time::Instant::from_std(last_ping + PING_EVERY);
            let wake = tokio::select! {
                frame = socket.next() => Wake::Frame(frame),
                _ = tokio::time::sleep_until(deadline) => Wake::Ping,
            };
            let step = match wake {
                Wake::Ping => match send(&mut socket, r#"{"method":"ping"}"#.to_string()).await {
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
        .send(Message::Text(text.into()))
        .await
        .map_err(|e| FeedError::Transport(e.to_string()))
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
    /// The next `userFills` message is the subscription snapshot.
    pub(crate) awaiting_snapshot: bool,
}

impl Decoder {
    pub(crate) fn new(ids: Arc<RwLock<HashMap<Symbol, SymbolId>>>) -> Self {
        Self {
            ids,
            pending: VecDeque::new(),
            acked: HashSet::new(),
            acked_order: VecDeque::new(),
            awaiting_snapshot: true,
        }
    }

    pub(crate) fn ingest(&mut self, text: &str) -> Result<(), FeedError> {
        let frame: Value = serde_json::from_str(text)
            .map_err(|e| FeedError::BadMessage(format!("{e}: {}", first_chars(text))))?;
        let Some(channel) = frame.get("channel").and_then(Value::as_str) else {
            return Ok(());
        };
        match channel {
            "orderUpdates" => self.order_updates(&frame),
            "userFills" => self.user_fills(&frame),
            "error" => {
                let why = frame
                    .get("data")
                    .and_then(Value::as_str)
                    .unwrap_or("no reason");
                Err(FeedError::Transport(format!(
                    "venue refused a subscription: {why}"
                )))
            }
            // pong, subscriptionResponse, and anything else this feed did not
            // ask for.
            _ => Ok(()),
        }
    }

    fn order_updates(&mut self, frame: &Value) -> Result<(), FeedError> {
        let rows = frame
            .get("data")
            .and_then(Value::as_array)
            .ok_or_else(|| FeedError::BadMessage("orderUpdates carries no list".to_string()))?;
        for row in rows {
            let Some(order) = row.get("order") else {
                continue;
            };
            // Orders this engine did not name are not ours to route.
            let Some(client_order_id) = order
                .get("cloid")
                .and_then(Value::as_str)
                .and_then(cloid::from_cloid)
            else {
                continue;
            };
            let status = row
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let recv_ns = mono_ns();
            match status {
                "open" => {
                    // One ack per order: the venue repeats the open status on
                    // a resubscribe, and a second ack would look like a second
                    // order.
                    if !self.remember_ack(&client_order_id) {
                        continue;
                    }
                    let oid = int_field(order, "oid")
                        .map_err(|e| FeedError::BadMessage(e.to_string()))?;
                    self.pending.push_back(OrderUpdate::Ack(OrderAck {
                        client_order_id,
                        venue_order_id: oid.to_string(),
                        sent_ns: 0,
                        ack_ns: recv_ns,
                    }));
                }
                // `filled` is not routed from here: the fill itself arrives
                // on `userFills` with the price, size and fee, and taking it
                // from both would count it twice. `triggered` ends the trigger
                // order and starts the one it fires, whose fill arrives there
                // too.
                "filled" | "triggered" => (),
                // Matched by shape, not by a list. This venue spells a dozen
                // endings — `reduceOnlyCanceled`, `siblingFilledCanceled`,
                // `openInterestCapCanceled`, `tickRejected` and more — and it
                // adds to them. An ending the engine does not recognise leaves
                // the order in flight for the rest of the run, waiting for a
                // message the venue has already sent.
                other if is_rejection(other) => {
                    self.pending.push_back(OrderUpdate::Reject {
                        client_order_id,
                        // The venue numbers nothing here; the reason is the
                        // status word itself.
                        code: 0,
                        reason: status.to_string(),
                    });
                }
                other if is_cancellation(other) => {
                    self.pending.push_back(OrderUpdate::Cancelled {
                        client_order_id,
                        recv_ns,
                    });
                }
                _ => (),
            }
        }
        Ok(())
    }

    fn user_fills(&mut self, frame: &Value) -> Result<(), FeedError> {
        let data = frame
            .get("data")
            .ok_or_else(|| FeedError::BadMessage("userFills carries no data".to_string()))?;
        let is_snapshot = data
            .get("isSnapshot")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if is_snapshot && self.awaiting_snapshot {
            // Already in the engine's log; delivering them would double-count
            // every fill the account has recently had.
            self.awaiting_snapshot = false;
            return Ok(());
        }
        self.awaiting_snapshot = false;
        let fills = data
            .get("fills")
            .and_then(Value::as_array)
            .ok_or_else(|| FeedError::BadMessage("userFills carries no fills".to_string()))?;
        for row in fills {
            let execution =
                parse_execution(row).map_err(|e| FeedError::BadMessage(e.to_string()))?;
            let Some(symbol) = self.symbol_id(&execution.symbol) else {
                // A fill on a symbol the engine has no id for cannot be
                // routed. It is still a real fill, so it is said out loud.
                tracing::warn!(
                    symbol = %execution.symbol,
                    "a fill arrived on a symbol this engine has no id for"
                );
                continue;
            };
            self.pending.push_back(OrderUpdate::Fill {
                exec_id: execution.exec_id,
                client_order_id: execution.client_order_id,
                symbol,
                side: execution.side,
                qty: execution.qty,
                px: execution.px,
                fee: execution.fee,
                is_maker: execution.is_maker,
                venue_ts_ms: execution.venue_ts_ms,
                recv_ns: mono_ns(),
            });
        }
        Ok(())
    }

    fn symbol_id(&self, symbol: &str) -> Option<SymbolId> {
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
}

fn first_chars(text: &str) -> String {
    text.chars().take(160).collect()
}

/// The venue ends every refusal in `Rejected` and every kill in `Canceled` —
/// except `scheduledCancel`, the dead-man's switch, which ends in `Cancel`.
/// Both endings are matched for that reason.
fn is_rejection(status: &str) -> bool {
    status == "rejected" || status.ends_with("Rejected")
}

fn is_cancellation(status: &str) -> bool {
    status == "canceled" || status.ends_with("Canceled") || status.ends_with("Cancel")
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::Side;

    #[test]
    fn every_ending_the_venue_spells_reaches_the_engine() {
        // The venue's own list, and it grows. An ending nobody routed would
        // leave the order in flight for the rest of the run — and
        // `siblingFilledCanceled` is not an edge case: it is what every
        // stop-and-target pair sends when one side fills.
        for killed in [
            "canceled",
            "marginCanceled",
            "reduceOnlyCanceled",
            "siblingFilledCanceled",
            "openInterestCapCanceled",
            "vaultWithdrawalCanceled",
            "selfTradeCanceled",
            "delistedCanceled",
            "liquidatedCanceled",
            "scheduledCancel",
        ] {
            assert!(is_cancellation(killed), "{killed} reaches nothing");
        }
        for refused in [
            "rejected",
            "tickRejected",
            "minTradeNtlRejected",
            "perpMarginRejected",
        ] {
            assert!(is_rejection(refused), "{refused} reaches nothing");
        }
        // And the two that are deliberately not routed here stay that way.
        assert!(!is_cancellation("filled") && !is_rejection("filled"));
        assert!(!is_cancellation("triggered") && !is_rejection("triggered"));
        assert!(!is_cancellation("open") && !is_rejection("open"));
    }

    fn decoder() -> Decoder {
        let ids = HashMap::from([
            ("BTCUSDT".to_string(), SymbolId(0)),
            ("ETHUSDT".to_string(), SymbolId(1)),
        ]);
        Decoder::new(Arc::new(RwLock::new(ids)))
    }

    fn ours() -> String {
        cloid::to_cloid("eng-1700000000000-9")
    }

    #[test]
    fn an_open_order_acks_once_and_carries_the_venues_number() {
        let mut d = decoder();
        let frame = format!(
            r#"{{"channel":"orderUpdates","data":[{{"order":{{"coin":"BTC","side":"B",
               "limitPx":"95000","sz":"0.01","oid":4242,"cloid":"{}"}},"status":"open",
               "statusTimestamp":1700}}]}}"#,
            ours()
        );
        d.ingest(&frame).unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::Ack(ack)) => {
                assert_eq!(ack.client_order_id, "eng-1700000000000-9");
                assert_eq!(ack.venue_order_id, "4242");
            }
            other => panic!("expected an ack, got {other:?}"),
        }
        // The venue repeats `open` on a resubscribe; a second ack would read
        // as a second order.
        d.ingest(&frame).unwrap();
        assert!(d.pending.is_empty(), "the same order acked twice");
    }

    #[test]
    fn an_order_this_engine_did_not_name_is_not_routed() {
        // The account is hand-traded too, and a stranger's order routed to a
        // strategy is a position that strategy never took.
        let mut d = decoder();
        d.ingest(
            r#"{"channel":"orderUpdates","data":[{"order":{"coin":"BTC","side":"B",
               "limitPx":"1","sz":"1","oid":1},"status":"open"}]}"#,
        )
        .unwrap();
        d.ingest(
            r#"{"channel":"orderUpdates","data":[{"order":{"coin":"BTC","side":"B",
               "limitPx":"1","sz":"1","oid":2,"cloid":"0x02aabbccddeeff00112233445566778899"},
               "status":"open"}]}"#,
        )
        .unwrap();
        assert!(d.pending.is_empty());
    }

    #[test]
    fn the_first_fill_message_is_a_snapshot_and_is_dropped() {
        // Subscribing replays fills the engine's log already holds. Delivering
        // them would double-count every one, and on a restart that is the
        // whole recent history of the account.
        let mut d = decoder();
        let fill = format!(
            r#"{{"coin":"BTC","px":"95000","sz":"0.01","side":"B","time":1700,"fee":"0.02",
               "tid":11,"crossed":true,"oid":1,"cloid":"{}"}}"#,
            ours()
        );
        d.ingest(&format!(
            r#"{{"channel":"userFills","data":{{"isSnapshot":true,"user":"0x1","fills":[{fill}]}}}}"#
        ))
        .unwrap();
        assert!(d.pending.is_empty(), "the snapshot was delivered");

        // And the live ones after it are.
        d.ingest(&format!(
            r#"{{"channel":"userFills","data":{{"user":"0x1","fills":[{fill}]}}}}"#
        ))
        .unwrap();
        assert_eq!(d.pending.len(), 1);
    }

    #[test]
    fn a_live_fill_carries_price_size_fee_and_which_side_of_the_spread() {
        let mut d = decoder();
        d.awaiting_snapshot = false;
        d.ingest(&format!(
            r#"{{"channel":"userFills","data":{{"user":"0x1","fills":[
               {{"coin":"ETH","px":"3000.5","sz":"1.5","side":"A","time":1701,"fee":"-0.01",
                 "tid":12,"crossed":false,"oid":2,"cloid":"{}"}}]}}}}"#,
            ours()
        ))
        .unwrap();
        match d.pending.pop_front() {
            Some(OrderUpdate::Fill {
                exec_id,
                symbol,
                side,
                qty,
                px,
                fee,
                is_maker,
                venue_ts_ms,
                ..
            }) => {
                assert_eq!(exec_id, "12");
                assert_eq!(symbol, SymbolId(1));
                assert_eq!(side, Side::Sell);
                assert_eq!(qty, 1.5);
                assert_eq!(px, 3000.5);
                assert_eq!(fee, -0.01);
                assert!(is_maker, "crossed:false means we were the resting side");
                assert_eq!(venue_ts_ms, 1701);
            }
            other => panic!("expected a fill, got {other:?}"),
        }
    }

    #[test]
    fn an_unnamed_live_fill_on_a_known_symbol_is_retained() {
        let mut d = decoder();
        d.awaiting_snapshot = false;
        d.ingest(
            r#"{"channel":"userFills","data":{"user":"0x1","fills":[
               {"coin":"BTC","px":"100","sz":"1","side":"A","time":1701,"fee":"0.01",
                "tid":13,"crossed":true,"oid":2}]}}"#,
        )
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Fill { exec_id, client_order_id, symbol: SymbolId(0), .. })
                if exec_id == "13" && client_order_id.is_empty()
        ));
    }

    #[test]
    fn a_fill_is_taken_from_one_channel_only() {
        // `orderUpdates` also reports `filled`. Routing it from both would
        // count every fill twice, once without a price.
        let mut d = decoder();
        d.ingest(&format!(
            r#"{{"channel":"orderUpdates","data":[{{"order":{{"coin":"BTC","side":"B",
               "limitPx":"95000","sz":"0","oid":5,"cloid":"{}"}},"status":"filled"}}]}}"#,
            ours()
        ))
        .unwrap();
        assert!(
            d.pending.is_empty(),
            "a fill was routed from the order channel"
        );
    }

    #[test]
    fn a_cancel_and_a_rejection_reach_the_engine() {
        let mut d = decoder();
        d.ingest(&format!(
            r#"{{"channel":"orderUpdates","data":[{{"order":{{"coin":"BTC","side":"B",
               "limitPx":"1","sz":"1","oid":6,"cloid":"{}"}},"status":"canceled"}}]}}"#,
            ours()
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Cancelled { .. })
        ));

        d.ingest(&format!(
            r#"{{"channel":"orderUpdates","data":[{{"order":{{"coin":"BTC","side":"B",
               "limitPx":"1","sz":"1","oid":7,"cloid":"{}"}},"status":"rejected"}}]}}"#,
            ours()
        ))
        .unwrap();
        assert!(matches!(
            d.pending.pop_front(),
            Some(OrderUpdate::Reject { .. })
        ));
    }

    #[test]
    fn a_refused_subscription_is_a_transport_failure_so_the_socket_is_redialled() {
        let mut d = decoder();
        let refused = d.ingest(r#"{"channel":"error","data":"Invalid subscription"}"#);
        assert!(
            matches!(refused, Err(FeedError::Transport(_))),
            "{refused:?}"
        );
        // A pong is not.
        assert!(d.ingest(r#"{"channel":"pong"}"#).is_ok());
        assert!(d
            .ingest(r#"{"channel":"subscriptionResponse","data":{}}"#)
            .is_ok());
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
    }
}
