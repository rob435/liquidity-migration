//! What the gateway actually puts on the wire, checked against a local
//! server. No network, no credentials.

mod support;

use std::time::Duration;

use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce,
    VenueError, VenueGateway,
};
use engine_venue::{BybitGateway, Venue, VenueRealm};
use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;
use support::{Recorded, TestServer};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const KEY: &str = "demoKey000000000001";
const SECRET: &str = "demoSecret00000000000000000001";

fn gateway(server: &TestServer) -> BybitGateway {
    BybitGateway::for_test(
        &server.base_url(),
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test(KEY, SECRET),
        vec![
            "BTCUSDT".to_string(),
            "ETHUSDT".to_string(),
            "SOLUSDT".to_string(),
        ],
    )
}

fn startup_gateway(server: &TestServer) -> BybitGateway {
    BybitGateway::for_test_with_position_mode_check(
        &server.base_url(),
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test(KEY, SECRET),
        vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
    )
}

fn market_order() -> OrderRequest {
    OrderRequest {
        client_order_id: "eng-1".to_string(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.001,
        kind: OrderKind::Market,
        stop: Some(StopSpec {
            trigger_px: 93000.5,
        }),
        reduce_only: false,
    }
}

fn ok(result: &str) -> (u16, String) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis();
    (
        200,
        format!(r#"{{"retCode":0,"retMsg":"OK","result":{result},"time":{now}}}"#),
    )
}

fn batch_ok(result: &str, ret_ext_info: &str) -> (u16, String) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis();
    (
        200,
        format!(
            r#"{{"retCode":0,"retMsg":"OK","result":{result},"retExtInfo":{ret_ext_info},"time":{now}}}"#
        ),
    )
}

fn one_way_position(request: &Recorded) -> (u16, String) {
    let symbol = request
        .query
        .strip_prefix("category=linear&symbol=")
        .and_then(|query| query.strip_suffix("&limit=200"))
        .expect("an explicit linear symbol query");
    ok(&format!(
        r#"{{"category":"linear","list":[{{"symbol":"{symbol}","positionIdx":0,"side":"","size":"0"}}],"nextPageCursor":"opaque-live-cursor"}}"#
    ))
}

/// Recompute the signature here, from the timestamp and payload the gateway
/// actually sent. Independent of the crate's own signing code, so it pins
/// that the bytes signed are the bytes sent.
fn expected_signature(payload: &str, timestamp: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(SECRET.as_bytes()).unwrap();
    mac.update(format!("{timestamp}{KEY}5000{payload}").as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

fn assert_signed(request: &Recorded, payload: &str) {
    assert_eq!(request.header("x-bapi-api-key"), Some(KEY));
    assert_eq!(request.header("x-bapi-recv-window"), Some("5000"));
    let timestamp = request
        .header("x-bapi-timestamp")
        .expect("timestamp header");
    assert!(timestamp.parse::<i64>().unwrap() > 1_600_000_000_000);
    assert_eq!(
        request.header("x-bapi-sign"),
        Some(expected_signature(payload, timestamp).as_str()),
        "signature does not cover the payload that was sent"
    );
}

#[tokio::test]
async fn send_order_posts_the_documented_shape() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1","orderLinkId":"eng-1"}"#)).await;
    let mut gw = gateway(&server);

    let ack = gw.send_order(&market_order()).await.unwrap();
    assert_eq!(ack.client_order_id, "eng-1");
    assert_eq!(ack.venue_order_id, "ord-1");
    assert!(ack.ack_ns > 0);

    let request = server.only("/v5/order/create");
    assert_eq!(request.method, "POST");
    assert_eq!(request.query, "");
    assert_eq!(request.header("content-type"), Some("application/json"));
    assert_signed(&request, &request.body);

    let body = request.json();
    assert_eq!(body["category"], "linear");
    assert_eq!(body["symbol"], "BTCUSDT");
    assert_eq!(body["side"], "Buy");
    assert_eq!(body["orderType"], "Market");
    assert_eq!(body["qty"], "0.001");
    assert_eq!(body["reduceOnly"], false);
    assert_eq!(body["orderLinkId"], "eng-1");
    // The stop rides with the entry: one round trip, position protected.
    assert_eq!(body["tpslMode"], "Full");
    assert_eq!(body["stopLoss"], "93000.5");
    assert_eq!(body["slTriggerBy"], "MarkPrice");
    assert_eq!(body["slOrderType"], "Market");
    assert_eq!(body["positionIdx"], 0);
    // A market order carries no price and lets the venue apply its own IOC.
    assert!(body.get("price").is_none());
    assert!(body.get("timeInForce").is_none());
}

#[tokio::test]
async fn sibling_orders_are_on_the_wire_together() {
    // Each complete request is held briefly before acknowledgement. A serial
    // gateway can therefore reach only one in flight; the batch path reaches
    // all three during that deterministic overlap window.
    let server = TestServer::start_delayed(
        |request, prior| {
            assert_eq!(request.path, "/v5/order/create");
            ok(&format!(r#"{{"orderId":"ord-{prior}"}}"#))
        },
        Duration::from_millis(100),
    )
    .await;
    let mut gw = gateway(&server);
    let requests: Vec<_> = (0..3)
        .map(|index| {
            let mut request = market_order();
            request.client_order_id = format!("eng-sibling-{index}");
            request.symbol = SymbolId(index);
            request
        })
        .collect();

    let replies = gw.send_orders(&requests).await;
    assert!(replies.iter().all(Result::is_ok), "{replies:?}");
    assert_eq!(server.peak_in_flight(), 3);
    assert_eq!(server.to_path("/v5/order/create").len(), 3);
}

#[tokio::test]
async fn same_symbol_siblings_keep_request_order_on_the_wire() {
    let server = TestServer::start_delayed(
        |request, prior| {
            assert_eq!(request.path, "/v5/order/create");
            ok(&format!(r#"{{"orderId":"ord-{prior}"}}"#))
        },
        Duration::from_millis(40),
    )
    .await;
    let mut gw = gateway(&server);
    let requests: Vec<_> = (0..3)
        .map(|index| {
            let mut request = market_order();
            request.client_order_id = format!("eng-same-symbol-{index}");
            request
        })
        .collect();

    let replies = gw.send_orders(&requests).await;

    assert!(replies.iter().all(Result::is_ok), "{replies:?}");
    assert_eq!(server.peak_in_flight(), 1);
    let wire_ids: Vec<_> = server
        .to_path("/v5/order/create")
        .iter()
        .map(|request| request.json()["orderLinkId"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(
        wire_ids,
        [
            "eng-same-symbol-0",
            "eng-same-symbol-1",
            "eng-same-symbol-2"
        ]
    );
}

#[tokio::test]
async fn an_oversized_sibling_batch_is_refused_before_the_wire() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"unexpected"}"#)).await;
    let mut gw = gateway(&server);
    let requests: Vec<_> = (0..11)
        .map(|index| {
            let mut request = market_order();
            request.client_order_id = format!("eng-oversized-{index}");
            request
        })
        .collect();

    let replies = gw.send_orders(&requests).await;
    assert_eq!(replies.len(), requests.len());
    assert!(replies
        .iter()
        .all(|reply| matches!(reply, Err(VenueError::BadRequest(_)))));
    assert!(server.requests().is_empty());
}

#[tokio::test]
async fn a_limit_order_carries_price_and_time_in_force() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-2"}"#)).await;
    let mut gw = gateway(&server);

    let request = OrderRequest {
        client_order_id: "eng-2".to_string(),
        strategy: StrategyId(1),
        symbol: SymbolId(1),
        side: Side::Sell,
        qty: 1.5,
        kind: OrderKind::Limit {
            px: 3000.25,
            tif: TimeInForce::PostOnly,
        },
        stop: None,
        reduce_only: true,
    };
    gw.send_order(&request).await.unwrap();

    let body = server.only("/v5/order/create").json();
    assert_eq!(body["symbol"], "ETHUSDT");
    assert_eq!(body["side"], "Sell");
    assert_eq!(body["orderType"], "Limit");
    assert_eq!(body["price"], "3000.25");
    assert_eq!(body["timeInForce"], "PostOnly");
    assert_eq!(body["qty"], "1.5");
    assert_eq!(body["reduceOnly"], true);
    assert!(body.get("stopLoss").is_none());
    assert!(body.get("tpslMode").is_none());
}

#[tokio::test]
async fn a_reduce_only_order_never_renders_a_stop_even_when_handed_one() {
    // Bybit: "When reduceOnly is true, take profit/stop loss cannot be set" —
    // rendering the stop would reject the whole exit, exactly when exiting
    // matters most. The earlier version of this test set stop: None, which
    // proved nothing.
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-9"}"#)).await;
    let mut gw = gateway(&server);

    let request = OrderRequest {
        client_order_id: "eng-9".to_string(),
        strategy: StrategyId(1),
        symbol: SymbolId(1),
        side: Side::Sell,
        qty: 1.5,
        kind: OrderKind::Market,
        stop: Some(engine_types::StopSpec { trigger_px: 2900.0 }),
        reduce_only: true,
    };
    gw.send_order(&request).await.unwrap();

    let body = server.only("/v5/order/create").json();
    assert_eq!(body["reduceOnly"], true);
    assert!(
        body.get("stopLoss").is_none(),
        "stopLoss on a reduce-only order"
    );
    assert!(
        body.get("tpslMode").is_none(),
        "tpslMode on a reduce-only order"
    );
}

#[tokio::test]
async fn a_non_zero_retcode_is_a_rejection() {
    let server = TestServer::start(|_, _| {
        (
            200,
            r#"{"retCode":110007,"retMsg":"ab not enough for new order","result":{}}"#.to_string(),
        )
    })
    .await;
    let mut gw = gateway(&server);

    match gw.send_order(&market_order()).await {
        Err(VenueError::Rejected { code, message }) => {
            assert_eq!(code, 110007);
            assert_eq!(message, "ab not enough for new order");
        }
        other => panic!("expected Rejected, got {other:?}"),
    }
}

#[tokio::test]
async fn a_malformed_reply_is_a_bad_reply() {
    let server = TestServer::start(|_, _| (200, "<html>rate limited</html>".to_string())).await;
    let mut gw = gateway(&server);
    assert!(matches!(
        gw.send_order(&market_order()).await,
        Err(VenueError::BadReply(_))
    ));
}

#[tokio::test]
async fn an_accepted_order_without_an_id_is_a_bad_reply() {
    let server = TestServer::start(|_, _| ok(r#"{"orderLinkId":"eng-1"}"#)).await;
    let mut gw = gateway(&server);
    assert!(matches!(
        gw.send_order(&market_order()).await,
        Err(VenueError::BadReply(_))
    ));
}

#[tokio::test]
async fn a_server_error_is_transport_not_a_rejection() {
    let server = TestServer::start(|_, _| (503, "upstream down".to_string())).await;
    let mut gw = gateway(&server);
    assert!(matches!(
        gw.send_order(&market_order()).await,
        Err(VenueError::Transport(_))
    ));
}

#[tokio::test]
async fn cancel_goes_by_our_own_order_id() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1","orderLinkId":"eng-1"}"#)).await;
    let mut gw = gateway(&server);

    gw.cancel_order(SymbolId(0), "eng-1").await.unwrap();

    let request = server.only("/v5/order/cancel");
    assert_eq!(request.method, "POST");
    assert_signed(&request, &request.body);
    let body = request.json();
    assert_eq!(body["category"], "linear");
    assert_eq!(body["symbol"], "BTCUSDT");
    assert_eq!(body["orderLinkId"], "eng-1");
}

#[tokio::test]
async fn cancel_batch_posts_one_ordered_request_and_preserves_partial_rejection() {
    let server = TestServer::start(|_, _| {
        batch_ok(
            r#"{"list":[{"category":"linear","symbol":"BTCUSDT","orderId":"ord-1","orderLinkId":"eng-1"},{"category":"linear","symbol":"ETHUSDT","orderId":"","orderLinkId":"eng-2"}]}"#,
            r#"{"list":[{"code":"0","msg":"OK"},{"code":"110001","msg":"Order does not exist"}]}"#,
        )
    })
    .await;
    let mut gw = gateway(&server);
    let requests = vec![
        (SymbolId(0), "eng-1".to_string()),
        (SymbolId(1), "eng-2".to_string()),
    ];

    let replies = gw.cancel_orders(&requests).await;

    assert_eq!(replies.len(), 2);
    assert!(replies[0].is_ok());
    assert!(matches!(
        &replies[1],
        Err(VenueError::Rejected { code: 110001, message })
            if message == "Order does not exist"
    ));
    assert!(server.to_path("/v5/order/cancel").is_empty());
    let request = server.only("/v5/order/cancel-batch");
    assert_eq!(request.method, "POST");
    assert_signed(&request, &request.body);
    let body = request.json();
    assert_eq!(body["category"], "linear");
    assert_eq!(body["request"].as_array().unwrap().len(), 2);
    assert_eq!(body["request"][0]["symbol"], "BTCUSDT");
    assert_eq!(body["request"][0]["orderLinkId"], "eng-1");
    assert_eq!(body["request"][1]["symbol"], "ETHUSDT");
    assert_eq!(body["request"][1]["orderLinkId"], "eng-2");
}

#[tokio::test]
async fn ten_cancels_use_one_quota_exact_native_batch_call() {
    let server = TestServer::start(|request, _| {
        assert_eq!(request.path, "/v5/order/cancel-batch");
        let body = request.json();
        let submitted = body["request"].as_array().unwrap();
        let identities: Vec<_> = submitted
            .iter()
            .enumerate()
            .map(|(index, item)| {
                serde_json::json!({
                    "category": "linear",
                    "symbol": item["symbol"].clone(),
                    "orderId": format!("venue-{index}"),
                    "orderLinkId": item["orderLinkId"].clone()
                })
            })
            .collect();
        let outcomes: Vec<_> = submitted
            .iter()
            .map(|_| serde_json::json!({"code": 0, "msg": "OK"}))
            .collect();
        batch_ok(
            &serde_json::json!({"list": identities}).to_string(),
            &serde_json::json!({"list": outcomes}).to_string(),
        )
    })
    .await;
    // Exercise the production enum as well as the concrete adapter. Missing
    // this dispatch once silently selected the trait's serial fallback.
    let mut gw = Venue::Bybit(gateway(&server));
    let requests: Vec<_> = (0..10)
        .map(|index| (SymbolId((index % 2) as u16), format!("eng-cancel-{index}")))
        .collect();

    let replies = gw.cancel_orders(&requests).await;

    assert_eq!(replies.len(), requests.len());
    assert!(replies.iter().all(Result::is_ok), "{replies:?}");
    let request = server.only("/v5/order/cancel-batch");
    assert_eq!(request.json()["request"].as_array().unwrap().len(), 10);
}

#[tokio::test]
async fn cancel_batch_identity_mismatch_fails_every_item_closed() {
    let server = TestServer::start(|_, _| {
        batch_ok(
            r#"{"list":[{"orderLinkId":"eng-2"},{"orderLinkId":"eng-1"}]}"#,
            r#"{"list":[{"code":0,"msg":"OK"},{"code":0,"msg":"OK"}]}"#,
        )
    })
    .await;
    let mut gw = gateway(&server);
    let requests = vec![
        (SymbolId(0), "eng-1".to_string()),
        (SymbolId(1), "eng-2".to_string()),
    ];

    let replies = gw.cancel_orders(&requests).await;

    assert_eq!(replies.len(), requests.len());
    assert!(replies
        .iter()
        .all(|reply| matches!(reply, Err(VenueError::BadReply(_)))));
    assert_eq!(server.to_path("/v5/order/cancel-batch").len(), 1);
}

#[tokio::test]
async fn an_oversized_cancel_batch_is_refused_before_the_wire() {
    let server = TestServer::start(|_, _| batch_ok(r#"{"list":[]}"#, r#"{"list":[]}"#)).await;
    let mut gw = gateway(&server);
    let requests: Vec<_> = (0..11)
        .map(|index| (SymbolId(0), format!("eng-oversized-cancel-{index}")))
        .collect();

    let replies = gw.cancel_orders(&requests).await;

    assert_eq!(replies.len(), requests.len());
    assert!(replies
        .iter()
        .all(|reply| matches!(reply, Err(VenueError::BadRequest(_)))));
    assert!(server.requests().is_empty());
}

#[tokio::test]
async fn an_amend_carries_only_the_field_it_changes() {
    // An echoed-back price is not a no-op at the venue: it costs the order
    // its place in the queue, which is the one thing amending is for.
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1","orderLinkId":"eng-1"}"#)).await;
    let mut gw = gateway(&server);
    assert!(gw.caps().amend_in_place, "and the engine is told it can");

    gw.amend_order(
        SymbolId(0),
        "eng-1",
        AmendSpec {
            px: Some(94_000.5),
            qty: None,
        },
    )
    .await
    .unwrap();

    let request = server.only("/v5/order/amend");
    assert_eq!(request.method, "POST");
    assert_signed(&request, &request.body);
    let body = request.json();
    assert_eq!(body["category"], "linear");
    assert_eq!(body["symbol"], "BTCUSDT");
    assert_eq!(body["orderLinkId"], "eng-1");
    assert_eq!(body["price"], "94000.5");
    assert!(body.get("qty").is_none(), "the size was not being changed");
}

#[tokio::test]
async fn an_amend_renders_a_new_size_as_a_venue_string() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1"}"#)).await;
    let mut gw = gateway(&server);

    gw.amend_order(
        SymbolId(1),
        "eng-2",
        AmendSpec {
            px: None,
            qty: Some(1.5),
        },
    )
    .await
    .unwrap();

    let body = server.only("/v5/order/amend").json();
    assert_eq!(body["symbol"], "ETHUSDT");
    assert_eq!(body["qty"], "1.5");
    assert!(body.get("price").is_none());
}

#[tokio::test]
async fn an_amend_that_changes_nothing_never_reaches_the_venue() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1"}"#)).await;
    let mut gw = gateway(&server);

    assert!(matches!(
        gw.amend_order(
            SymbolId(0),
            "eng-1",
            AmendSpec {
                px: None,
                qty: None
            }
        )
        .await,
        Err(VenueError::BadRequest(_))
    ));
    let mut bad_size = AmendSpec {
        px: None,
        qty: Some(f64::NAN),
    };
    assert!(gw
        .amend_order(SymbolId(0), "eng-1", bad_size)
        .await
        .is_err());
    bad_size.qty = Some(0.0);
    assert!(gw
        .amend_order(SymbolId(0), "eng-1", bad_size)
        .await
        .is_err());
    assert!(
        server.requests().is_empty(),
        "nothing should have been sent"
    );
}

#[tokio::test]
async fn the_gateway_says_what_bybit_can_actually_do() {
    // The engine refuses actions on this word, so a wrong one here is a
    // strategy believing it has something it does not.
    let server = TestServer::start(|_, _| ok("{}")).await;
    let caps = gateway(&server).caps();
    assert!(
        caps.native_position_stop,
        "trading-stop holds the position stop"
    );
    assert!(caps.amend_in_place, "/v5/order/amend");
}

#[tokio::test]
async fn set_stop_uses_full_mode_on_the_one_way_position() {
    let server = TestServer::start(|_, _| ok("{}")).await;
    let mut gw = gateway(&server);

    gw.set_stop(SymbolId(1), 2950.0).await.unwrap();

    let request = server.only("/v5/position/trading-stop");
    assert_eq!(request.method, "POST");
    assert_signed(&request, &request.body);
    let body = request.json();
    assert_eq!(body["category"], "linear");
    assert_eq!(body["symbol"], "ETHUSDT");
    assert_eq!(body["stopLoss"], "2950");
    assert_eq!(body["tpslMode"], "Full");
    assert_eq!(body["positionIdx"], 0);
    assert_eq!(body["slTriggerBy"], "MarkPrice");
    assert_eq!(body["slOrderType"], "Market");
}

#[tokio::test]
async fn account_view_reads_wallet_and_positions() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/account/wallet-balance" => ok(r#"{"list":[{"accountType":"UNIFIED",
             "totalEquity":"1500.25","totalAvailableBalance":"1200.5","coin":[]}]}"#),
        "/v5/position/list" => ok(r#"{"list":[
             {"symbol":"BTCUSDT","side":"Buy","size":"0.01","avgPrice":"95000",
              "stopLoss":"93000","positionIdx":0},
             {"symbol":"ETHUSDT","side":"","size":"0","avgPrice":"0","stopLoss":""}
            ],"nextPageCursor":""}"#),
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gw = gateway(&server);

    let view = gw.account_view().await.unwrap();
    assert_eq!(view.equity_usdt, 1500.25);
    assert_eq!(view.available_usdt, 1200.5);
    assert_eq!(view.positions.len(), 1);
    assert_eq!(view.positions[0].symbol, SymbolId(0));
    assert!(view.positions[0].stop_attached);
    assert!(view.observed_ns > 0);

    // A signed GET signs the raw query string, not the body.
    let wallet = server.only("/v5/account/wallet-balance");
    assert_eq!(wallet.method, "GET");
    assert_eq!(wallet.query, "accountType=UNIFIED");
    assert_eq!(wallet.body, "");
    assert_signed(&wallet, &wallet.query);

    let positions = server.only("/v5/position/list");
    assert_eq!(positions.query, "category=linear&settleCoin=USDT&limit=200");
    assert_signed(&positions, &positions.query);

    // The two reads go out together rather than one after the other.
    assert_eq!(server.connections(), 2);
}

#[tokio::test]
async fn mainnet_inventory_reads_unfiltered_cross_account_assets() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/market/instruments-info" => {
            ok(r#"{"category":"linear","list":[],"nextPageCursor":""}"#)
        }
        "/v5/account/wallet-balance" => ok(
            r#"{"list":[{"accountType":"UNIFIED","coin":[]}]}"#,
        ),
        "/v5/asset/asset-overview" => {
            let snapshot_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis();
            ok(&format!(
                r#"{{"totalEquity":"0","list":[{{"accountType":"TradingBot","totalEquity":"0","valuationCurrency":"USD","snapshotTime":"{snapshot_ms}","categories":[{{"category":"Futures Grid Bot","equity":"0","coinDetail":[]}}]}}]}}"#
            ))
        }
        "/v5/position/list" => {
            let category = request
                .query
                .strip_prefix("category=")
                .and_then(|query| query.split('&').next())
                .unwrap();
            ok(&format!(
                r#"{{"category":"{category}","list":[],"nextPageCursor":""}}"#
            ))
        }
        "/v5/order/realtime" => {
            let category = request
                .query
                .strip_prefix("category=")
                .and_then(|query| query.split('&').next())
                .unwrap();
            ok(&format!(
                r#"{{"category":"{category}","list":[],"nextPageCursor":""}}"#
            ))
        }
        "/v5/spread/order/realtime" => ok(r#"{"list":[],"nextPageCursor":""}"#),
        "/v5/rfq/quote-realtime" | "/v5/rfq/rfq-realtime" => ok(r#"{"list":[]}"#),
        "/v5/strategy/list" => ok(r#"{"list":[],"nextCursor":""}"#),
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gw = BybitGateway::for_test(
        &server.base_url(),
        VenueRealm::Mainnet,
        VenueRealm::Mainnet.credentials_for_test(KEY, SECRET),
        Vec::new(),
    );

    let inventory = gw.account_inventory().await.unwrap();
    assert!(inventory.positions.is_empty());
    assert_eq!(inventory.open_orders.len(), 1);
    assert_eq!(inventory.open_orders[0].product, "asset_account:TradingBot");
    assert_eq!(inventory.open_orders[0].symbol, "Futures Grid Bot");

    let request = server.only("/v5/asset/asset-overview");
    assert_eq!(request.method, "GET");
    assert_eq!(
        request.query, "",
        "the scan must not select one account type"
    );
    assert_eq!(request.body, "");
    assert_signed(&request, "");
}

#[tokio::test]
async fn blank_wallet_totals_fail_rather_than_read_as_zero() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/account/wallet-balance" => ok(r#"{"list":[{"accountType":"UNIFIED",
             "totalEquity":"","totalAvailableBalance":""}]}"#),
        _ => ok(r#"{"list":[],"nextPageCursor":""}"#),
    })
    .await;
    let mut gw = gateway(&server);
    assert!(matches!(
        gw.account_view().await,
        Err(VenueError::BadReply(_))
    ));
}

#[tokio::test]
async fn instrument_rules_follow_the_page_cursor() {
    let server = TestServer::start(|request, prior| {
        assert_eq!(request.method, "GET");
        // Public listing: no signature headers on this one.
        assert!(request.header("x-bapi-sign").is_none());
        if prior == 0 {
            assert_eq!(request.query, "category=linear&limit=1000");
            ok(r#"{"category":"linear","list":[
                {"symbol":"BTCUSDT","priceFilter":{"tickSize":"0.10"},
                 "lotSizeFilter":{"qtyStep":"0.001","minOrderQty":"0.001","minNotionalValue":"5"}}
             ],"nextPageCursor":"page%3D2"}"#)
        } else {
            assert_eq!(
                request.query,
                "category=linear&limit=1000&cursor=page%253D2"
            );
            ok(r#"{"category":"linear","list":[
                {"symbol":"ETHUSDT","priceFilter":{"tickSize":"0.01"},
                 "lotSizeFilter":{"qtyStep":"0.01","minOrderQty":"0.01","minNotionalValue":"5"}}
             ],"nextPageCursor":""}"#)
        }
    })
    .await;
    let mut gw = gateway(&server);

    let rules = gw.instrument_rules().await.unwrap();
    assert_eq!(rules.len(), 2);
    assert_eq!(rules[0].0, "BTCUSDT");
    assert_eq!(rules[0].1.tick_size, 0.10);
    assert_eq!(rules[1].0, "ETHUSDT");
    assert_eq!(rules[1].1.qty_step, 0.01);
    assert_eq!(server.to_path("/v5/market/instruments-info").len(), 2);
}

#[tokio::test]
async fn warm_opens_the_connection_on_the_public_time_endpoint() {
    let server =
        TestServer::start(|_, _| ok(r#"{"timeSecond":"1700000000","timeNano":"1700000000000"}"#))
            .await;
    let mut gw = gateway(&server);

    gw.warm().await.unwrap();

    let requests = server.to_path("/v5/market/time");
    assert_eq!(requests.len(), 10, "the full HTTP/1.1 order pool is warm");
    assert_eq!(server.connections(), 10);
    for request in requests {
        assert_eq!(request.method, "GET");
        assert_eq!(request.query, "");
        assert!(request.header("x-bapi-sign").is_none());
    }
}

#[tokio::test]
async fn a_refused_trade_socket_keeps_the_warm_rest_order_path() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let trade_url = format!("ws://{}", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0_u8; 2048];
        let _ = stream.read(&mut request).await;
        stream
            .write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            .await
            .unwrap();
    });
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/market/time" => ok(r#"{"timeSecond":"1700000000"}"#),
        "/v5/order/create" => ok(r#"{"orderId":"rest-fallback"}"#),
        path => panic!("unexpected REST path after trade refusal: {path}"),
    })
    .await;
    let mut gw = BybitGateway::for_test_with_trade_transport(
        &server.base_url(),
        &trade_url,
        VenueRealm::Mainnet,
        VenueRealm::Mainnet.credentials_for_test(KEY, SECRET),
        vec!["BTCUSDT".to_string()],
    );

    gw.warm().await.expect("REST warm remains usable");
    let ack = gw.send_order(&market_order()).await.expect("REST fallback");
    assert_eq!(ack.venue_order_id, "rest-fallback");
    assert_eq!(server.to_path("/v5/order/create").len(), 1);
}

#[tokio::test]
async fn the_warm_connection_is_reused_for_the_order() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/market/time" => ok(r#"{"timeSecond":"1700000000"}"#),
        _ => ok(r#"{"orderId":"ord-1"}"#),
    })
    .await;
    let mut gw = gateway(&server);

    gw.warm().await.unwrap();
    gw.send_order(&market_order()).await.unwrap();

    // Ten warmups and one order, still on the original ten sockets: the
    // order did not pay for a new connection.
    assert_eq!(server.requests().len(), 11);
    assert_eq!(server.connections(), 10);
}

#[tokio::test]
async fn a_symbol_the_gateway_does_not_know_never_reaches_the_venue() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1"}"#)).await;
    let mut gw = gateway(&server);

    let mut request = market_order();
    request.symbol = SymbolId(9);
    assert!(gw.send_order(&request).await.is_err());
    assert!(
        server.requests().is_empty(),
        "nothing should have been sent"
    );
}

#[tokio::test]
async fn a_runtime_symbol_is_checked_once_before_its_first_order() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/position/list" => one_way_position(request),
        "/v5/order/create" => ok(r#"{"orderId":"ord-runtime"}"#),
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gw = gateway(&server);
    let symbol = gw.add_symbol("XRPUSDT");
    let mut order = market_order();
    order.symbol = symbol;
    order.stop = None;

    gw.send_order(&order).await.unwrap();
    order.client_order_id = "eng-runtime-2".to_string();
    gw.send_order(&order).await.unwrap();

    let checks = server.to_path("/v5/position/list");
    assert_eq!(checks.len(), 1, "the successful proof is cached");
    assert_eq!(checks[0].query, "category=linear&symbol=XRPUSDT&limit=200");
    assert_eq!(server.to_path("/v5/order/create").len(), 2);
}

#[tokio::test]
async fn an_unquantized_quantity_never_reaches_the_venue() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1"}"#)).await;
    let mut gw = gateway(&server);

    let mut request = market_order();
    request.qty = f64::NAN;
    assert!(gw.send_order(&request).await.is_err());
    request.qty = 0.0;
    assert!(gw.send_order(&request).await.is_err());
    assert!(
        server.requests().is_empty(),
        "nothing should have been sent"
    );
}

#[tokio::test]
async fn account_identity_asks_the_venue_whose_account_this_is() {
    let server = TestServer::start(|request, _| {
        if request.path == "/v5/position/list" {
            one_way_position(request)
        } else {
            ok(&format!(
                r#"{{"id":"1","apiKey":"{KEY}","userID":6039967,"readOnly":0}}"#
            ))
        }
    })
    .await;
    let mut gw = startup_gateway(&server);

    let who = gw.account_identity().await.unwrap();
    assert_eq!(who.user_id, "6039967");
    assert_eq!(who.realm, "demo");

    let request = server.only("/v5/user/query-api");
    assert_eq!(request.method, "GET");
    assert_eq!(request.query, "", "the identity read takes no parameters");
    assert_signed(&request, "");

    let checks = server.to_path("/v5/position/list");
    assert_eq!(checks.len(), 2, "every configured symbol is checked");
    assert_eq!(checks[0].method, "GET");
    assert_eq!(checks[0].query, "category=linear&symbol=BTCUSDT&limit=200");
    assert_eq!(checks[1].query, "category=linear&symbol=ETHUSDT&limit=200");
    for check in checks {
        assert_signed(&check, &check.query);
        assert!(check.body.is_empty());
    }
}

#[tokio::test]
async fn an_account_number_sent_as_text_reads_the_same() {
    // Bybit sends userID as a number here and as a string elsewhere. Both
    // have to land on the same lock file name or the two engines miss.
    let server = TestServer::start(|request, _| {
        if request.path == "/v5/position/list" {
            one_way_position(request)
        } else {
            ok(&format!(r#"{{"apiKey":"{KEY}","userID":"0006039967"}}"#))
        }
    })
    .await;
    assert_eq!(
        startup_gateway(&server)
            .account_identity()
            .await
            .unwrap()
            .user_id,
        "6039967"
    );
}

#[tokio::test]
async fn hedge_mode_refuses_startup_before_any_order_can_be_sent() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/v5/position/list" if request.query.contains("symbol=BTCUSDT&") => {
            one_way_position(request)
        }
        "/v5/position/list" => ok(
            r#"{"category":"linear","list":[{"symbol":"ETHUSDT","positionIdx":1,"side":"","size":"0"},{"symbol":"ETHUSDT","positionIdx":2,"side":"","size":"0"}],"nextPageCursor":""}"#,
        ),
        _ => ok(&format!(r#"{{"apiKey":"{KEY}","userID":6039967}}"#)),
    })
    .await;

    let refused = startup_gateway(&server)
        .account_identity()
        .await
        .unwrap_err();
    assert!(matches!(refused, VenueError::BadReply(_)), "{refused:?}");
    assert!(refused.to_string().contains("ETHUSDT"), "{refused}");
    assert!(
        server
            .requests()
            .iter()
            .all(|request| request.method == "GET"),
        "startup verification must not change account settings"
    );
    assert!(server.to_path("/v5/order/create").is_empty());
}

#[tokio::test]
async fn an_identity_about_a_different_api_key_is_refused() {
    // Whatever produced this reply, it was not the key we signed with — so
    // the account number in it is somebody else's, and taking a lock in that
    // name would leave this account unguarded.
    let server =
        TestServer::start(|_, _| ok(r#"{"apiKey":"someoneElsesKey0001","userID":9999999}"#)).await;
    let refused = gateway(&server).account_identity().await;
    assert!(
        matches!(refused, Err(VenueError::Credentials(_))),
        "a reply about another key was trusted: {refused:?}"
    );
}

#[tokio::test]
async fn an_identity_with_no_usable_account_number_is_refused() {
    for result in [
        r#"{"apiKey":"KEYHERE"}"#,
        r#"{"apiKey":"KEYHERE","userID":0}"#,
        r#"{"apiKey":"KEYHERE","userID":"nope"}"#,
    ] {
        let body = result.replace("KEYHERE", KEY);
        let server = TestServer::start(move |_, _| ok(&body)).await;
        let refused = gateway(&server).account_identity().await;
        assert!(
            matches!(refused, Err(VenueError::BadReply(_))),
            "{result} was accepted as an account number: {refused:?}"
        );
    }
}

// ---------------------------------------------------------------------------
// Leverage
// ---------------------------------------------------------------------------

#[tokio::test]
async fn set_leverage_states_both_sides_and_the_symbol() {
    let server = TestServer::start(|_, _| ok("{}")).await;
    let mut gw = gateway(&server);

    gw.set_leverage(SymbolId(1), 2.0).await.unwrap();

    let request = server.only("/v5/position/set-leverage");
    let body = request.json();
    assert_eq!(body["category"], "linear");
    assert_eq!(body["symbol"], "ETHUSDT");
    // Both sides, the same number. One-way position mode still carries two,
    // and a venue holding them apart would post different margin depending on
    // which way a position went.
    assert_eq!(body["buyLeverage"], "2");
    assert_eq!(body["sellLeverage"], "2");
    assert_signed(&request, &request.body);
}

#[tokio::test]
async fn leverage_not_modified_is_success_not_a_failure() {
    // Bybit answers 110043 when the symbol already sits at the number asked
    // for. The request asked for a state and the state is what was asked for,
    // so this is the request succeeding. Read as an error it would block
    // every repeat entry on a symbol whose leverage is already right -- which
    // is most entries, most of the time.
    let server = TestServer::start(|_, _| {
        (
            200,
            r#"{"retCode":110043,"retMsg":"leverage not modified","result":{},"time":1700000000000}"#
                .to_string(),
        )
    })
    .await;
    let mut gw = gateway(&server);

    gw.set_leverage(SymbolId(0), 2.0)
        .await
        .expect("\"already at this leverage\" is not a failure");
}

#[tokio::test]
async fn a_real_leverage_refusal_is_still_a_refusal() {
    // The other half of the pair: only 110043 is forgiven, and the proof that
    // the arm above is not swallowing everything.
    let server = TestServer::start(|_, _| {
        (
            200,
            r#"{"retCode":110044,"retMsg":"leverage limit exceeded","result":{},"time":1700000000000}"#
                .to_string(),
        )
    })
    .await;
    let mut gw = gateway(&server);

    let err = gw.set_leverage(SymbolId(0), 500.0).await.unwrap_err();
    match err {
        VenueError::Rejected { code, .. } => assert_eq!(code, 110044),
        other => panic!("expected a venue refusal, got {other:?}"),
    }
}

#[tokio::test]
async fn the_gateway_says_it_can_set_leverage() {
    // The engine only calls set_leverage when this is true, and refuses an
    // order sized at a leverage it cannot state. A gateway that could set it
    // but said otherwise would block every levered entry.
    let server = TestServer::start(|_, _| ok("{}")).await;
    assert!(gateway(&server).caps().set_leverage);
}
