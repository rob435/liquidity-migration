//! The private stream handshake, against a local WebSocket server. No
//! network, no credentials.

use std::sync::{Arc, Mutex};

use engine_types::{FeedError, OrderFeed, OrderUpdate, Side, SymbolId};
use engine_venue::{BybitOrderFeed, Credentials};
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

/// A server that completes the handshake, records what it was sent, then
/// pushes `frames` at the client.
async fn start(frames: Vec<String>) -> (String, Arc<Mutex<Vec<Value>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
    let recorded = seen.clone();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();

        // Auth first.
        let auth = next_json(&mut socket).await;
        recorded.lock().unwrap().push(auth);
        socket
            .send(Message::text(
                json!({"op": "auth", "success": true, "ret_msg": "", "conn_id": "test"})
                    .to_string(),
            ))
            .await
            .unwrap();

        // Then the subscription.
        let subscribe = next_json(&mut socket).await;
        recorded.lock().unwrap().push(subscribe);
        socket
            .send(Message::text(
                json!({"op": "subscribe", "success": true, "conn_id": "test"}).to_string(),
            ))
            .await
            .unwrap();

        for frame in frames {
            socket.send(Message::text(frame)).await.unwrap();
        }
        // Hold the socket open so the client does not see a close.
        while let Some(Ok(msg)) = socket.next().await {
            if msg.is_close() {
                break;
            }
        }
    });

    (format!("ws://{addr}/v5/private"), seen)
}

async fn next_json(
    socket: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
) -> Value {
    loop {
        match socket.next().await.expect("client sent nothing").unwrap() {
            Message::Text(text) => return serde_json::from_str(text.as_str()).unwrap(),
            _ => continue,
        }
    }
}

fn feed(url: &str) -> BybitOrderFeed {
    BybitOrderFeed::for_test(
        url,
        Credentials::new(KEY, SECRET),
        vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
    )
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
        OrderUpdate::Ack(ack) => {
            assert_eq!(ack.client_order_id, "eng-1");
            assert_eq!(ack.venue_order_id, "ord-1");
        }
        other => panic!("expected Ack, got {other:?}"),
    }
    match feed.next_update().await.unwrap() {
        OrderUpdate::Fill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            venue_ts_ms,
            ..
        } => {
            assert_eq!(client_order_id, "eng-1");
            assert_eq!(symbol, SymbolId(0));
            assert_eq!(side, Side::Buy);
            assert_eq!(qty, 0.01);
            assert_eq!(px, 95900.1);
            assert_eq!(fee, 0.527);
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
    assert_eq!(subscribe["args"], json!(["order", "execution"]));
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
