//! The last window in which a queued mutation can still be stopped: the trade
//! WebSocket's own queue, its reconnect backoff, and its dial.
//!
//! The adapter re-reads the authority after the per-second budget, but the
//! frame then waits inside the transport for as long as the socket takes to
//! come back. These tests hold the socket down across that wait and ask what
//! reached the venue.
//!
//! Real time, not the paused clock: a live `accept_async` fixture gives the
//! runtime no idle point to auto-advance from, so the TTLs here are short and
//! the reconnect backoff (250 ms) is the thing they expire inside.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::clock::mono_ns;
use engine_types::{
    AmendRequest, AmendSpec, AuthorityEpoch, CommandAuthority, OrderKind, OrderRequest, Side,
    StrategyId, SymbolId, VenueError, VenueGateway,
};
use engine_venue::{BybitGateway, RealmCredentials, VenueRealm};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;

const KEY: &str = "demoKey000000000001";
const SECRET: &str = "demoSecret00000000000000000001";
/// Longer than any frame this fixture writes takes, shorter than the trade
/// worker's 250 ms reconnect backoff.
const SHORT_TTL: Duration = Duration::from_millis(100);

/// Accepts one connection, authenticates it, reads a single frame and drops
/// the socket; then accepts a second, authenticates it, and answers every
/// request frame it receives. The frames the second connection received are
/// what the assertions read.
struct RedialledVenue {
    url: String,
    seen: Arc<Mutex<Vec<Value>>>,
    task: tokio::task::JoinHandle<()>,
}

impl RedialledVenue {
    async fn start() -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorder = seen.clone();
        let task = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut first = accept_async(stream).await.unwrap();
            authenticate(&mut first).await;
            first.next().await;
            drop(first);

            let (stream, _) = listener.accept().await.unwrap();
            let mut second = accept_async(stream).await.unwrap();
            authenticate(&mut second).await;
            while let Some(Ok(Message::Text(text))) = second.next().await {
                let request: Value = serde_json::from_str(text.as_str()).unwrap();
                if request["op"] == "ping" {
                    let pong = Message::text(r#"{"op":"pong","retCode":0}"#);
                    second.send(pong).await.unwrap();
                    continue;
                }
                recorder.lock().unwrap().push(request.clone());
                second.send(answer(&request)).await.unwrap();
            }
        });
        Self {
            url: format!("ws://{address}"),
            seen,
            task,
        }
    }

    fn seen(&self) -> Vec<Value> {
        self.seen.lock().unwrap().clone()
    }

    fn client_order_ids(&self, operation: &str) -> Vec<String> {
        self.seen()
            .iter()
            .filter(|frame| frame["op"] == operation)
            .map(|frame| {
                frame["args"][0]["orderLinkId"]
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect()
    }
}

impl Drop for RedialledVenue {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn authenticate<S>(socket: &mut tokio_tungstenite::WebSocketStream<S>)
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let auth = socket.next().await.unwrap().unwrap().into_text().unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(auth.as_str()).unwrap()["op"],
        "auth"
    );
    socket
        .send(Message::text(r#"{"op":"auth","retCode":0}"#))
        .await
        .unwrap();
}

fn answer(request: &Value) -> Message {
    Message::text(
        json!({
            "reqId": request["reqId"],
            "op": request["op"],
            "retCode": 0,
            "retMsg": "OK",
            "data": {
                "orderId": "venue-1",
                "orderLinkId": request["args"][0]["orderLinkId"],
            },
            "retExtInfo": {},
        })
        .to_string(),
    )
}

/// The REST base is unreachable on purpose: every path under test is the
/// trade socket's, and a REST fallback would be a silently different result.
fn gateway(trade_url: &str) -> BybitGateway {
    BybitGateway::for_test_with_trade_transport(
        "http://127.0.0.1:1",
        trade_url,
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test(KEY, SECRET),
        vec!["BTCUSDT".to_string()],
    )
}

fn opening(client_order_id: &str) -> OrderRequest {
    OrderRequest {
        client_order_id: client_order_id.to_string(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.001,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    }
}

fn reducing(client_order_id: &str) -> OrderRequest {
    OrderRequest {
        reduce_only: true,
        side: Side::Sell,
        ..opening(client_order_id)
    }
}

fn amend(client_order_id: &str, authority: Option<CommandAuthority>) -> AmendRequest {
    AmendRequest {
        symbol: SymbolId(0),
        client_order_id: client_order_id.to_string(),
        spec: AmendSpec {
            px: Some(30_000.0),
            qty: None,
            exact_terms: None,
        },
        authority,
    }
}

fn live(epoch: &AuthorityEpoch) -> CommandAuthority {
    CommandAuthority {
        epoch: epoch.current(),
        queued_ns: mono_ns(),
        expires_at_ns: u64::MAX,
    }
}

/// Live when the adapter checks it, lapsed by the time the socket is back.
fn expiring(epoch: &AuthorityEpoch, ttl: Duration) -> CommandAuthority {
    let queued_ns = mono_ns();
    CommandAuthority {
        epoch: epoch.current(),
        queued_ns,
        expires_at_ns: queued_ns + ttl.as_nanos() as u64,
    }
}

fn refusal(reply: &Result<impl std::fmt::Debug, VenueError>) -> &str {
    match reply {
        Err(VenueError::BadRequest(reason)) => reason,
        other => panic!("expected a never-transmitted refusal, got {other:?}"),
    }
}

/// Spend the first connection and leave the worker in its reconnect backoff.
/// The reply is the one this fixture's dropped socket produces: the venue took
/// the frame and never answered it.
async fn drop_the_socket(gateway: &mut BybitGateway, epoch: &AuthorityEpoch) {
    let held = live(epoch);
    let replies = gateway
        .send_orders_under(&[opening("warm-up")], Some((epoch, held)))
        .await;
    assert!(
        matches!(&replies[0], Err(VenueError::Transport(_))),
        "{replies:?}"
    );
}

#[tokio::test]
async fn an_opening_whose_authority_lapses_in_the_reconnect_backoff_is_never_written() {
    let venue = RedialledVenue::start().await;
    let mut gateway = gateway(&venue.url);
    let epoch = AuthorityEpoch::new();
    drop_the_socket(&mut gateway, &epoch).await;

    let doomed = expiring(&epoch, SHORT_TTL);
    let replies = gateway
        .send_orders_under(&[opening("doomed")], Some((&epoch, doomed)))
        .await;
    assert!(
        refusal(&replies[0]).starts_with("authority:"),
        "{replies:?}"
    );

    let held = live(&epoch);
    gateway
        .send_orders_under(&[opening("after-reconnect")], Some((&epoch, held)))
        .await
        .pop()
        .unwrap()
        .expect("the reconnected socket refused a live authority");

    assert_eq!(
        venue.client_order_ids("order.create"),
        ["after-reconnect"],
        "an opening whose authority lapsed in the transport queue reached the venue"
    );
}

#[tokio::test]
async fn an_amend_whose_authority_lapses_in_the_reconnect_backoff_is_never_written() {
    let venue = RedialledVenue::start().await;
    let mut gateway = gateway(&venue.url);
    let epoch = AuthorityEpoch::new();
    drop_the_socket(&mut gateway, &epoch).await;

    // One group, two authorities: the gates are per body, so a misalignment
    // here would refuse the wrong amendment rather than none.
    let group = [
        amend("doomed", Some(expiring(&epoch, SHORT_TTL))),
        amend("sibling", Some(live(&epoch))),
    ];
    let replies = gateway.amend_orders_under(&group, &epoch).await;
    assert!(
        refusal(&replies[0]).starts_with("authority:"),
        "{replies:?}"
    );
    replies[1]
        .as_ref()
        .expect("the reconnected socket refused a live authority");

    assert_eq!(
        venue.client_order_ids("order.amend"),
        ["sibling"],
        "an amendment whose authority lapsed in the transport queue reached the venue"
    );
}

#[tokio::test]
async fn an_epoch_advanced_while_the_opening_is_queued_refuses_it_before_the_write() {
    let venue = RedialledVenue::start().await;
    let mut gateway = gateway(&venue.url);
    let epoch = AuthorityEpoch::new();
    drop_the_socket(&mut gateway, &epoch).await;

    let held = live(&epoch);
    // After the adapter's own check and before the socket is back: the halt
    // this stands for happens while the frame sits in the transport queue.
    let halt = async {
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(epoch.advance(), 2);
    };
    let group = [opening("superseded")];
    let (replies, ()) = tokio::join!(
        gateway.send_orders_under(&group, Some((&epoch, held))),
        halt
    );
    assert_eq!(refusal(&replies[0]), "authority: epoch 1 superseded by 2");

    let held = live(&epoch);
    gateway
        .send_orders_under(&[opening("after-reconnect")], Some((&epoch, held)))
        .await
        .pop()
        .unwrap()
        .expect("the reconnected socket refused a live authority");

    assert_eq!(
        venue.client_order_ids("order.create"),
        ["after-reconnect"],
        "an opening superseded in the transport queue reached the venue"
    );
}

#[tokio::test]
async fn a_placement_under_no_authority_is_written_after_the_reconnect() {
    let venue = RedialledVenue::start().await;
    let mut gateway = gateway(&venue.url);
    let epoch = AuthorityEpoch::new();
    drop_the_socket(&mut gateway, &epoch).await;

    gateway
        .send_orders(&[reducing("reducing")])
        .await
        .pop()
        .unwrap()
        .expect("a placement nothing can refuse locally was refused");

    assert_eq!(
        venue.client_order_ids("order.create"),
        ["reducing"],
        "a reduction queued across the reconnect was not written"
    );
}

#[tokio::test]
async fn an_opening_the_socket_takes_without_answering_stays_a_transport_failure() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = accept_async(stream).await.unwrap();
        authenticate(&mut socket).await;
        let sent = socket.next().await.unwrap().unwrap().into_text().unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(sent.as_str()).unwrap()["op"],
            "order.create"
        );
        // The authority expires here, after the frame is on the wire.
        tokio::time::sleep(SHORT_TTL * 2).await;
        drop(socket);
    });

    let mut gateway = gateway(&format!("ws://{address}"));
    let epoch = AuthorityEpoch::new();
    let held = expiring(&epoch, SHORT_TTL);
    let replies = gateway
        .send_orders_under(&[opening("taken")], Some((&epoch, held)))
        .await;

    assert!(
        matches!(&replies[0], Err(VenueError::Transport(_))),
        "an opening the venue may already hold was reported as never sent: {replies:?}"
    );
    server.await.unwrap();
}
