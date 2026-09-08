//! MEXC's private login, filter, keep-alive and pushes, against a local
//! WebSocket server. No network, no credentials.

use crate::support::IoProgress;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_public::venues::mexc::contracts::Contracts;
use engine_types::numeric::ExactInstrumentSpec;
use engine_types::{FeedError, OrderFeed, OrderUpdate, Side, SymbolId};
use engine_venue::{MexcOrderFeed, MexcRealm, RealmCredentials};
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, KeyInit, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

const KEY: &str = "demoKey000000000001";
const SECRET: &str = "demoSecret00000000000000000001";
const CLIENT: &str = "eng-1700000000000-1";

/// Recorded from `GET /api/v1/contract/detail`: one contract at the size that
/// makes a contract count and a coin count different numbers.
const DETAIL: &str = r#"{"success":true,"code":0,"data":[
  {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
   "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,
   "limitMaxVol":2500000,"maxLeverage":500,"apiAllowed":true}]}"#;

/// Derived here, not asked of the code under test. The primitive itself is
/// pinned against Python's `hmac` in `venues/mexc/sign.rs`.
fn expected_login_signature(req_time: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(SECRET.as_bytes()).unwrap();
    mac.update(format!("{KEY}{req_time}").as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

fn spec() -> ExactInstrumentSpec {
    let (symbol, spec) = Contracts::parse_raw(DETAIL)
        .unwrap()
        .instrument_specs()
        .pop()
        .unwrap();
    assert_eq!(symbol, "BTCUSDT");
    assert_eq!(spec.native_symbol, "BTC_USDT");
    spec
}

fn feed(url: &str) -> MexcOrderFeed {
    let mut feed =
        MexcOrderFeed::for_test(url, MexcRealm::Mainnet.credentials_for_test(KEY, SECRET));
    // The venue names `BTC_USDT`; the id and the contract size both reach the
    // decoder this way and no other.
    feed.learn_instrument(SymbolId(0), &spec());
    feed
}

/// One three-contract maker fill, in the venue's own frame shape.
fn deal_frame() -> String {
    json!({"channel":"push.personal.order.deal","data":{
        "id":987654321_i64, "symbol":"BTC_USDT", "side":1, "vol":3,
        "price":45000.5, "feeCurrency":"USDT", "fee":0.225,
        "timestamp":1700000000000_i64, "profit":0, "isTaker":false,
        "category":1, "orderId":123456789_i64, "isSelf":false,
        "externalOid":CLIENT, "positionMode":1, "reduceOnly":false
    },"ts":1700000000000_i64})
    .to_string()
}

fn order_frame() -> String {
    json!({"channel":"push.personal.order","data":{
        "orderId":123456789_i64, "symbol":"BTC_USDT", "price":45000.5, "vol":3,
        "leverage":20, "side":1, "category":1, "orderType":1, "dealVol":0,
        "state":2, "errorCode":0, "externalOid":CLIENT, "openType":2,
        "createTime":1700000000000_i64, "updateTime":1700000000000_i64
    },"ts":1700000000000_i64})
    .to_string()
}

struct Server {
    url: String,
    seen: Arc<Mutex<Vec<Value>>>,
    logins: Arc<AtomicUsize>,
}

impl Server {
    fn seen(&self) -> Vec<Value> {
        self.seen.lock().unwrap().clone()
    }
}

/// Accept connections for ever. `accept_login` decides whether the login is
/// answered with `rs.login` success or with `rs.error`; a served connection
/// pushes `frames` once the filter has arrived and then records whatever the
/// client sends after that.
async fn serve(accept_login: bool, frames: Vec<String>) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
    let logins = Arc::new(AtomicUsize::new(0));
    let recorded = seen.clone();
    let counted = logins.clone();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let recorded = recorded.clone();
            let counted = counted.clone();
            let frames = frames.clone();
            tokio::spawn(async move {
                let Ok(mut socket) = tokio_tungstenite::accept_async(stream).await else {
                    return;
                };
                let Some(login) = next_json(&mut socket).await else {
                    return;
                };
                recorded.lock().unwrap().push(login);
                counted.fetch_add(1, Ordering::SeqCst);
                let reply = if accept_login {
                    json!({"channel":"rs.login","data":"success","ts":"1700000000000"})
                } else {
                    json!({"channel":"rs.error","data":"login failed","ts":"1700000000000"})
                };
                if socket.send(Message::text(reply.to_string())).await.is_err() {
                    return;
                }
                if !accept_login {
                    while socket.next().await.is_some() {}
                    return;
                }
                let Some(filter) = next_json(&mut socket).await else {
                    return;
                };
                recorded.lock().unwrap().push(filter);
                for frame in frames {
                    if socket.send(Message::text(frame)).await.is_err() {
                        return;
                    }
                }
                while let Some(Ok(message)) = socket.next().await {
                    if let Message::Text(text) = message {
                        if let Ok(value) = serde_json::from_str::<Value>(text.as_str()) {
                            recorded.lock().unwrap().push(value);
                        }
                    }
                }
            });
        }
    });
    Server {
        url: format!("ws://{addr}/edge"),
        seen,
        logins,
    }
}

type ServerSocket = tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>;

async fn next_json(socket: &mut ServerSocket) -> Option<Value> {
    while let Some(Ok(message)) = socket.next().await {
        if let Message::Text(text) = message {
            return serde_json::from_str(text.as_str()).ok();
        }
    }
    None
}

/// Everything the feed has queued right now, without waiting for more.
async fn drain(feed: &mut MexcOrderFeed) -> Vec<Result<OrderUpdate, FeedError>> {
    let mut out = Vec::new();
    loop {
        match futures_util::poll!(Box::pin(feed.next_update())) {
            std::task::Poll::Ready(item) => out.push(item),
            std::task::Poll::Pending => return out,
        }
    }
}

async fn settle() {
    for _ in 0..200 {
        tokio::task::yield_now().await;
    }
}

#[tokio::test(start_paused = true)]
async fn the_feed_logs_in_filters_and_maps_what_arrives() {
    let _io = IoProgress::new();
    let server = serve(true, vec![order_frame(), deal_frame()]).await;
    let mut feed = feed(&server.url);

    assert!(
        matches!(
            feed.next_update().await.unwrap(),
            OrderUpdate::StreamReset { .. }
        ),
        "the readiness watermark must come before anything the socket carries"
    );
    match feed.next_update().await.unwrap() {
        OrderUpdate::Ack(ack) => {
            assert_eq!(ack.client_order_id, CLIENT);
            assert_eq!(ack.venue_order_id, "123456789");
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
            amounts,
            is_maker,
            ..
        } => {
            assert_eq!(exec_id, "987654321");
            assert_eq!(client_order_id, CLIENT);
            assert_eq!(symbol, SymbolId(0));
            assert_eq!(side, Side::Buy);
            // Three contracts at 0.0001 each, not three coins.
            assert_eq!(qty, 0.0003);
            assert_eq!(px, 45000.5);
            assert_eq!(fee, Some(0.225));
            assert!(is_maker);
            let amounts = amounts.unwrap();
            amounts.validate_projection(qty, px, fee).unwrap();
            assert_eq!(
                amounts.quantity.value,
                engine_types::numeric::Exact::parse_decimal("0.0003").unwrap()
            );
        }
        other => panic!("expected Fill, got {other:?}"),
    }

    let seen = server.seen();
    let login = &seen[0];
    assert_eq!(login["method"], "login");
    assert_eq!(
        login["subscribe"], false,
        "the default push was not cancelled"
    );
    assert_eq!(login["param"]["apiKey"], KEY);
    let req_time = login["param"]["reqTime"]
        .as_str()
        .expect("reqTime is a string");
    assert!(
        req_time.parse::<i64>().unwrap() > 1_600_000_000_000,
        "{req_time} is not a millisecond stamp"
    );
    assert_eq!(
        login["param"]["signature"].as_str().unwrap(),
        expected_login_signature(req_time),
        "the login is signed over something other than apiKey + reqTime"
    );

    let filter = &seen[1];
    assert_eq!(filter["method"], "personal.filter");
    let filters = filter["param"]["filters"].as_array().unwrap();
    let names: Vec<&str> = filters
        .iter()
        .map(|row| row["filter"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["order", "order.deal"]);
    for row in filters {
        assert_eq!(
            row["rules"].as_array().unwrap().len(),
            0,
            "an empty rule list is every contract"
        );
    }

    // The venue drops a socket it has heard nothing on for a minute.
    assert!(
        !server.seen().iter().any(|frame| frame["method"] == "ping"),
        "a ping went out before its interval"
    );
    tokio::time::advance(Duration::from_secs(16)).await;
    settle().await;
    assert!(
        server.seen().iter().any(|frame| frame["method"] == "ping"),
        "the keep-alive never went out: {:?}",
        server.seen()
    );
}

#[tokio::test(start_paused = true)]
async fn a_refused_login_leaves_the_paced_resync_running() {
    let _io = IoProgress::new();
    let server = serve(false, Vec::new()).await;
    let mut feed = feed(&server.url);

    let mut seen = Vec::new();
    for _ in 0..6 {
        settle().await;
        tokio::time::advance(Duration::from_secs(5)).await;
        settle().await;
        seen.extend(drain(&mut feed).await);
    }
    assert!(
        matches!(seen.first(), Some(Ok(OrderUpdate::StreamReset { .. }))),
        "the readiness watermark did not come first, so boot would refuse to \
         start on a venue this feed can still reconcile against: {seen:?}"
    );
    let mut resyncs = 0;
    for update in seen {
        match update {
            Ok(OrderUpdate::StreamReset { .. }) => resyncs += 1,
            // Later refusals are reported; they are not the end of the feed.
            Err(FeedError::Transport(_)) => (),
            other => panic!("a refused login produced {other:?}"),
        }
    }
    assert!(
        resyncs >= 3,
        "the paced resync stopped when the login was refused: {resyncs} in 30s"
    );
    assert!(
        server.logins.load(Ordering::SeqCst) > 1,
        "the feed gave up on the login instead of retrying"
    );
}
