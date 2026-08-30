//! The private stream handshake, against a local WebSocket server. No
//! network, no credentials.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::{FeedError, OrderFeed, OrderUpdate, Side, SymbolId};
use engine_venue::{BybitOrderFeed, VenueRealm};
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, KeyInit, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

const KEY: &str = "demoKey000000000001";
const SECRET: &str = "demoSecret00000000000000000001";

/// Derived here, not asked of the code under test.
fn expected_ws_signature(expires: i64) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(SECRET.as_bytes()).unwrap();
    mac.update(format!("GET/realtime{expires}").as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// What the server does with one connection.
enum Outcome {
    /// Read the auth op and then close the socket instead of answering: a
    /// venue hiccup in the middle of the handshake.
    CloseMidAuth,
    /// Answer the auth op with a refusal.
    Refuse(&'static str),
    /// Finish the handshake, push these frames, then either close or hold the
    /// socket open.
    Serve {
        frames: Vec<String>,
        then_close: bool,
    },
}

struct Conn {
    /// How long the venue takes to answer the auth op. A real venue is a
    /// round trip away, not a function call.
    auth_delay: Duration,
    outcome: Outcome,
}

impl Conn {
    fn serving(frames: Vec<String>) -> Conn {
        Conn {
            auth_delay: Duration::ZERO,
            outcome: Outcome::Serve {
                frames,
                then_close: false,
            },
        }
    }
}

struct Server {
    url: String,
    seen: Arc<Mutex<Vec<Value>>>,
    connections: Arc<AtomicUsize>,
    hung_up: Arc<AtomicUsize>,
}

impl Server {
    fn connections(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }

    /// How many client connections have ended, from the server's side.
    fn hung_up(&self) -> usize {
        self.hung_up.load(Ordering::SeqCst)
    }

    fn seen(&self) -> Vec<Value> {
        self.seen.lock().unwrap().clone()
    }
}

/// A server that accepts connection after connection, handing the nth one to
/// `plan(n)`. Every op the client sends is recorded.
async fn serve<F>(plan: F) -> Server
where
    F: Fn(usize) -> Conn + Send + Sync + 'static,
{
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
    let connections = Arc::new(AtomicUsize::new(0));

    let hung_up = Arc::new(AtomicUsize::new(0));
    let recorded = seen.clone();
    let counted = connections.clone();
    let ended = hung_up.clone();
    tokio::spawn(async move {
        let mut n = 0usize;
        while let Ok((stream, _)) = listener.accept().await {
            let conn = plan(n);
            n += 1;
            counted.fetch_add(1, Ordering::SeqCst);
            let recorded = recorded.clone();
            let ended = ended.clone();
            tokio::spawn(async move {
                one_connection(stream, conn, recorded).await;
                ended.fetch_add(1, Ordering::SeqCst);
            });
        }
    });

    Server {
        url: format!("ws://{addr}/v5/private"),
        seen,
        connections,
        hung_up,
    }
}

type ServerSocket = tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>;

async fn one_connection(stream: tokio::net::TcpStream, conn: Conn, seen: Arc<Mutex<Vec<Value>>>) {
    let Ok(mut socket) = tokio_tungstenite::accept_async(stream).await else {
        return;
    };
    let Some(auth) = next_json(&mut socket).await else {
        return;
    };
    seen.lock().unwrap().push(auth);
    tokio::time::sleep(conn.auth_delay).await;

    match conn.outcome {
        Outcome::CloseMidAuth => {
            let _ = socket.close(None).await;
        }
        Outcome::Refuse(why) => {
            let _ = socket
                .send(Message::text(
                    json!({"op": "auth", "success": false, "ret_msg": why}).to_string(),
                ))
                .await;
            hold(&mut socket).await;
        }
        Outcome::Serve { frames, then_close } => {
            if socket
                .send(Message::text(
                    json!({"op": "auth", "success": true, "ret_msg": "", "conn_id": "test"})
                        .to_string(),
                ))
                .await
                .is_err()
            {
                return;
            }
            let Some(subscribe) = next_json(&mut socket).await else {
                return;
            };
            seen.lock().unwrap().push(subscribe);
            let _ = socket
                .send(Message::text(
                    json!({"op": "subscribe", "success": true, "conn_id": "test"}).to_string(),
                ))
                .await;
            for frame in frames {
                if socket.send(Message::text(frame)).await.is_err() {
                    return;
                }
            }
            if then_close {
                let _ = socket.close(None).await;
            } else {
                hold(&mut socket).await;
            }
        }
    }
}

/// Keep the socket open so the client does not see a close.
async fn hold(socket: &mut ServerSocket) {
    while let Some(Ok(msg)) = socket.next().await {
        if msg.is_close() {
            break;
        }
    }
}

/// The next text op from the client, or `None` if it went away.
async fn next_json(socket: &mut ServerSocket) -> Option<Value> {
    loop {
        match socket.next().await?.ok()? {
            Message::Text(text) => return serde_json::from_str(text.as_str()).ok(),
            Message::Close(_) => return None,
            _ => continue,
        }
    }
}

/// A server that completes the handshake on every connection and pushes
/// `frames` at the client.
async fn start(frames: Vec<String>) -> (String, Arc<Mutex<Vec<Value>>>) {
    let server = serve(move |_| Conn::serving(frames.clone())).await;
    (server.url.clone(), server.seen.clone())
}

fn feed(url: &str) -> BybitOrderFeed {
    BybitOrderFeed::for_test(
        url,
        VenueRealm::Demo.credentials_for_test(KEY, SECRET),
        vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
    )
}

/// One `order` frame carrying a New order under `link_id`.
fn ack_frame(link_id: &str) -> String {
    json!({
        "topic": "order",
        "id": "test",
        "creationTime": 1_672_364_262_474i64,
        "data": [{
            "symbol": "BTCUSDT",
            "orderId": format!("ord-for-{link_id}"),
            "orderLinkId": link_id,
            "side": "Buy",
            "orderType": "Limit",
            "orderStatus": "New",
            "price": "95000",
            "qty": "0.01",
            "category": "linear"
        }]
    })
    .to_string()
}

/// Read updates the way the engine does: a fresh `next_update` future inside a
/// `select!` that also carries the 250 ms group-flush tick, so the losing
/// branch's future is dropped every time the tick wins. Panics if nothing
/// arrives inside `limit`.
async fn drive(feed: &mut BybitOrderFeed, limit: Duration, what: &str) -> OrderUpdate {
    let mut flush_tick = tokio::time::interval(Duration::from_millis(250));
    flush_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let deadline = tokio::time::Instant::now() + limit;
    loop {
        tokio::select! {
            update = feed.next_update() => match update {
                Ok(update) => return update,
                Err(FeedError::Closed) => panic!("{what}: the feed reported itself closed"),
                // A feed that errors without closing is reconnecting inside;
                // the engine logs it and keeps waiting, so this does too.
                Err(_) => (),
            },
            _ = flush_tick.tick() => (),
            _ = tokio::time::sleep_until(deadline) => {
                panic!("{what}: nothing arrived in {limit:?}")
            }
        }
    }
}

/// Read updates one after another, nothing cancelling anything. A feed that
/// reports itself closed has retired itself, which is the fault this catches.
async fn read(feed: &mut BybitOrderFeed, limit: Duration, what: &str) -> OrderUpdate {
    let waited = tokio::time::timeout(limit, async {
        loop {
            match feed.next_update().await {
                Ok(update) => return update,
                Err(FeedError::Closed) => panic!("{what}: the feed reported itself closed"),
                Err(_) => (),
            }
        }
    })
    .await;
    waited.unwrap_or_else(|_| panic!("{what}: nothing arrived in {limit:?}"))
}

#[tokio::test]
async fn the_feed_authenticates_subscribes_and_maps_what_arrives() {
    let order_frame = json!({
        "topic": "order",
        "id": "test",
        "creationTime": 1_672_364_262_474i64,
        "data": [{
            "symbol": "BTCUSDT",
            "orderId": "ord-1",
            "orderLinkId": "eng-1",
            "side": "Buy",
            "orderType": "Limit",
            "orderStatus": "New",
            "price": "95000",
            "qty": "0.01",
            "category": "linear"
        }]
    })
    .to_string();
    let execution_frame = json!({
        "topic": "execution",
        "id": "test",
        "creationTime": 1_746_270_400_355i64,
        "data": [{
            "execId": "exec-1",
            "execPrice": "95900.1",
            "execQty": "0.01",
            "execFee": "0.527",
            "execTime": "1746270400353",
            "orderLinkId": "eng-1",
            "orderId": "ord-1",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execType": "Trade"
        }]
    })
    .to_string();

    let (url, seen) = start(vec![order_frame, execution_frame]).await;
    let mut feed = feed(&url);

    match feed.next_update().await.unwrap() {
        OrderUpdate::StreamReset { .. } => (),
        other => panic!("expected initial StreamReset, got {other:?}"),
    }
    match feed.next_update().await.unwrap() {
        OrderUpdate::Ack(ack) => {
            assert_eq!(ack.client_order_id, "eng-1");
            assert_eq!(ack.venue_order_id, "ord-1");
        }
        other => panic!("expected Ack, got {other:?}"),
    }
    match feed.next_update().await.unwrap() {
        OrderUpdate::Fill {
            exec_id,
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            venue_ts_ms,
            ..
        } => {
            assert_eq!(exec_id, "exec-1");
            assert_eq!(client_order_id, "eng-1");
            assert_eq!(symbol, SymbolId(0));
            assert_eq!(side, Side::Buy);
            assert_eq!(qty, 0.01);
            assert_eq!(px, 95900.1);
            assert_eq!(fee, Some(0.527));
            assert_eq!(venue_ts_ms, 1_746_270_400_353);
        }
        other => panic!("expected Fill, got {other:?}"),
    }

    let seen = seen.lock().unwrap();
    let auth = &seen[0];
    assert_eq!(auth["op"], "auth");
    assert_eq!(auth["args"][0], KEY);
    let expires = auth["args"][1].as_i64().expect("expires is a number");
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    assert!(expires > now_ms, "expiry must be in the future");
    assert!(expires < now_ms + 60_000, "expiry must be near");
    assert_eq!(auth["args"][2], expected_ws_signature(expires));

    let subscribe = &seen[1];
    assert_eq!(subscribe["op"], "subscribe");
    assert_eq!(
        subscribe["args"],
        json!(["order", "execution.fast", "execution"])
    );
}

#[tokio::test]
async fn demo_does_not_request_the_mainnet_only_fast_execution_topic() {
    let (url, seen) = start(Vec::new()).await;
    let mut feed = BybitOrderFeed::for_test_realm(
        &url,
        VenueRealm::Demo.credentials_for_test(KEY, SECRET),
        vec!["BTCUSDT".to_string()],
        VenueRealm::Demo,
    );

    assert!(matches!(
        feed.next_update().await.unwrap(),
        OrderUpdate::StreamReset { .. }
    ));
    let seen = seen.lock().unwrap();
    assert_eq!(seen[1]["op"], "subscribe");
    assert_eq!(seen[1]["args"], json!(["order", "execution"]));
}

#[tokio::test]
async fn a_refused_auth_is_reported_not_swallowed() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let _ = socket.next().await;
        socket
            .send(Message::text(
                json!({"op": "auth", "success": false, "ret_msg": "error sign!"}).to_string(),
            ))
            .await
            .unwrap();
        // Keep the connection up so the failure is the auth, not a close.
        let _ = socket.next().await;
    });

    let mut feed = feed(&format!("ws://{addr}/v5/private"));
    match feed.next_update().await {
        Err(FeedError::Transport(why)) => assert!(why.contains("error sign!"), "{why}"),
        other => panic!("expected a transport error, got {other:?}"),
    }
}

/// Defect 1: the engine drops the losing `select!` branch every flush tick, so
/// a handshake that lives inside `next_update`'s future never finishes.
#[tokio::test]
async fn the_handshake_survives_the_engine_dropping_the_future_every_tick() {
    let server = serve(|_| Conn {
        // Longer than the engine's 250 ms flush tick, which is what a venue a
        // round trip away actually costs.
        auth_delay: Duration::from_millis(400),
        outcome: Outcome::Serve {
            frames: vec![ack_frame("eng-1")],
            then_close: false,
        },
    })
    .await;
    let mut feed = feed(&server.url);

    match drive(&mut feed, Duration::from_secs(8), "initial reset").await {
        OrderUpdate::StreamReset { .. } => (),
        other => panic!("expected initial StreamReset, got {other:?}"),
    }
    match drive(&mut feed, Duration::from_secs(8), "first account update").await {
        OrderUpdate::Ack(ack) => assert_eq!(ack.client_order_id, "eng-1"),
        other => panic!("expected Ack, got {other:?}"),
    }
    // One socket, not a new dial per tick.
    assert_eq!(server.connections(), 1, "the feed redialled");
}

/// Defect 2: a venue hiccup part-way through the handshake used to read as
/// `Closed`, which the engine takes as "no more order news, ever". Read
/// straight through, with no `select!` anywhere, so only this defect can fail
/// the test.
#[tokio::test]
async fn a_socket_closed_mid_auth_is_retried_not_terminal() {
    let server = serve(|n| Conn {
        auth_delay: Duration::ZERO,
        outcome: if n == 0 {
            Outcome::CloseMidAuth
        } else {
            Outcome::Serve {
                frames: vec![ack_frame("eng-1")],
                then_close: false,
            }
        },
    })
    .await;
    let mut feed = feed(&server.url);

    match read(
        &mut feed,
        Duration::from_secs(8),
        "initial reset after the hiccup",
    )
    .await
    {
        OrderUpdate::StreamReset { .. } => (),
        other => panic!("expected initial StreamReset, got {other:?}"),
    }
    match read(&mut feed, Duration::from_secs(8), "update after the hiccup").await {
        OrderUpdate::Ack(ack) => assert_eq!(ack.client_order_id, "eng-1"),
        other => panic!("expected Ack, got {other:?}"),
    }
    assert!(server.connections() >= 2, "the feed never redialled");
}

/// A refused auth is reported and then tried again. Nothing the venue says
/// during the handshake retires the feed.
#[tokio::test]
async fn a_refused_auth_is_retried_until_it_takes() {
    let server = serve(|n| Conn {
        auth_delay: Duration::ZERO,
        outcome: if n < 2 {
            Outcome::Refuse("error sign!")
        } else {
            Outcome::Serve {
                frames: vec![ack_frame("eng-1")],
                then_close: false,
            }
        },
    })
    .await;
    let mut feed = feed(&server.url);

    match drive(
        &mut feed,
        Duration::from_secs(8),
        "initial reset after two refusals",
    )
    .await
    {
        OrderUpdate::StreamReset { .. } => (),
        other => panic!("expected initial StreamReset, got {other:?}"),
    }
    match drive(
        &mut feed,
        Duration::from_secs(8),
        "update after two refusals",
    )
    .await
    {
        OrderUpdate::Ack(ack) => assert_eq!(ack.client_order_id, "eng-1"),
        other => panic!("expected Ack, got {other:?}"),
    }
    assert_eq!(
        server.connections(),
        3,
        "expected two refusals then a good one"
    );
    let auths = server.seen().iter().filter(|op| op["op"] == "auth").count();
    assert_eq!(auths, 3, "every attempt signed a fresh auth op");
}

/// The socket belongs to the feed: let the feed go and the socket goes with
/// it, rather than a task reconnecting forever to nobody.
#[tokio::test]
async fn dropping_the_feed_takes_the_socket_with_it() {
    let server = serve(|_| Conn::serving(vec![ack_frame("eng-1")])).await;
    let mut feed = feed(&server.url);

    read(&mut feed, Duration::from_secs(8), "first update").await;
    assert_eq!(server.connections(), 1);
    assert_eq!(server.hung_up(), 0, "the socket ended too early");

    drop(feed);
    let gave_up = tokio::time::timeout(Duration::from_secs(5), async {
        while server.hung_up() == 0 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    assert!(gave_up.is_ok(), "the socket outlived the feed");
    assert_eq!(server.connections(), 1, "it redialled after being dropped");
}

/// Every successful subscription owes the engine a `StreamReset` before any
/// account row. The first establishes readiness; a reconnect also marks the
/// interval whose updates may have been lost.
#[tokio::test]
async fn every_subscription_announces_itself_before_the_socket_says_anything() {
    let server = serve(|n| Conn {
        auth_delay: Duration::ZERO,
        outcome: Outcome::Serve {
            frames: vec![ack_frame(if n == 0 { "eng-1" } else { "eng-2" })],
            then_close: n == 0,
        },
    })
    .await;
    let mut feed = feed(&server.url);

    match drive(&mut feed, Duration::from_secs(8), "initial reset").await {
        OrderUpdate::StreamReset { .. } => (),
        other => panic!("expected initial StreamReset, got {other:?}"),
    }
    match drive(&mut feed, Duration::from_secs(8), "first update").await {
        OrderUpdate::Ack(ack) => assert_eq!(ack.client_order_id, "eng-1"),
        other => panic!("expected Ack, got {other:?}"),
    }
    match drive(&mut feed, Duration::from_secs(8), "the reset").await {
        OrderUpdate::StreamReset { .. } => (),
        other => panic!("expected StreamReset, got {other:?}"),
    }
    match drive(&mut feed, Duration::from_secs(8), "update after the reset").await {
        OrderUpdate::Ack(ack) => assert_eq!(ack.client_order_id, "eng-2"),
        other => panic!("expected Ack, got {other:?}"),
    }
}
