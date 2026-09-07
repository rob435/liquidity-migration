//! Authenticated demo wire frames; provenance and identifier sanitization are in the corpus header.
//! Native order, position and execution frames retain their numeric wire lexemes.
use crate::support::IoProgress;
use engine_types::numeric::AssetId;
use engine_types::{FeedError, OrderFeed, OrderUpdate, Side, SymbolId};
use engine_venue::{BybitOrderFeed, RealmCredentials, VenueRealm};
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio_tungstenite::tungstenite::Message;

const CORPUS: &str = include_str!("bybit_demo_private.jsonl");

#[tokio::test(start_paused = true)]
async fn authenticated_demo_frames_replay_through_the_live_private_decoder() {
    let _io = IoProgress::new();
    let mut rows = CORPUS
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap());
    let header = rows.next().unwrap();
    assert_eq!(header["capture"]["realm"], "demo");
    assert_eq!(header["capture"]["authentication"], "accepted");
    let frames: Vec<_> = rows
        .map(|row| row["frame"].as_str().unwrap().to_owned())
        .collect();
    let topics: Vec<_> = frames
        .iter()
        .filter_map(|raw| {
            serde_json::from_str::<Value>(raw).unwrap()["topic"]
                .as_str()
                .map(str::to_owned)
        })
        .collect();
    assert_eq!(
        topics,
        [
            "order",
            "position",
            "order",
            "position",
            "order",
            "position",
            "order",
            "position",
            "position",
            "execution",
            "order",
            "position",
        ]
    );

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        for epoch in 0..2 {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let auth: Value =
                serde_json::from_str(&socket.next().await.unwrap().unwrap().into_text().unwrap())
                    .unwrap();
            assert_eq!(auth["op"], "auth");
            socket
                .send(Message::text(r#"{"op":"auth","success":true}"#))
                .await
                .unwrap();
            let subscribe: Value =
                serde_json::from_str(&socket.next().await.unwrap().unwrap().into_text().unwrap())
                    .unwrap();
            assert_eq!(subscribe["op"], "subscribe");
            socket
                .send(Message::text(r#"{"op":"subscribe","success":true}"#))
                .await
                .unwrap();
            if epoch == 0 {
                for frame in &frames {
                    socket.send(Message::text(frame.as_str())).await.unwrap();
                }
                socket.close(None).await.unwrap();
            } else {
                while socket.next().await.is_some() {}
            }
        }
    });
    let mut feed = BybitOrderFeed::for_test(
        &format!("ws://{address}/v5/private"),
        VenueRealm::Demo.credentials_for_test("fixture-key", "fixture-secret"),
        vec!["BTCUSDT".into(), "LINKUSDT".into()],
    );
    assert!(matches!(
        feed.next_update().await.unwrap(),
        OrderUpdate::StreamReset { .. }
    ));
    for id in 1..=2 {
        let OrderUpdate::Ack(ack) = feed.next_update().await.unwrap() else {
            panic!("recorded New must acknowledge")
        };
        assert_eq!(ack.client_order_id, format!("eng-1700000000000-{id}"));
        assert_eq!(ack.venue_order_id, format!("captured-orderId-{id}"));
        let OrderUpdate::Cancelled {
            client_order_id, ..
        } = feed.next_update().await.unwrap()
        else {
            panic!("recorded Cancelled must cancel; flat position must emit no fill")
        };
        assert_eq!(client_order_id, ack.client_order_id);
    }
    let OrderUpdate::Fill {
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
    } = feed.next_update().await.unwrap()
    else {
        panic!("recorded execution must carry the native fill")
    };
    assert_eq!(exec_id, "captured-execId-1");
    assert_eq!(client_order_id, "eng-1700000000000-3");
    assert_eq!((symbol, side), (SymbolId(1), Side::Buy));
    assert_eq!((qty, px, fee), (53.9, 13.329, Some(0.39513821)));
    assert_eq!(venue_ts_ms, 1_788_728_733_671);
    assert!(!is_maker);
    assert!(forced_close.is_none() && allocation.is_none());
    let amounts = amounts.expect("live decoder retains exact execution amounts");
    amounts.validate_projection(qty, px, fee).unwrap();
    assert_eq!(amounts.quantity.value.to_decimal_string().unwrap(), "53.9");
    assert_eq!(amounts.price.value.to_decimal_string().unwrap(), "13.329");
    let fee = amounts.fee.unwrap();
    assert_eq!(fee.asset, AssetId::Named("USDT".into()));
    assert_eq!(fee.amount.value.to_decimal_string().unwrap(), "0.39513821");
    assert!(
        matches!(feed.next_update().await, Err(FeedError::Transport(reason)) if reason.contains("closed the socket"))
    );
    // Filled order, unlinked native stop and position rows emit no duplicate fill.
    assert!(matches!(
        feed.next_update().await.unwrap(),
        OrderUpdate::StreamReset { .. }
    ));
    drop(feed);
    server.await.unwrap();
}
