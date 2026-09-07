//! Binance request shapes against a local HTTP server. No venue credentials
//! and no external network.

use engine_venue::RealmCredentials;

use crate::support::{Recorded, TestServer};
use engine_types::{
    OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce, VenueError,
    VenueGateway,
};
use engine_venue::{BinanceGateway, BinanceRealm};

const BTC_EXCHANGE_INFO: &str = r#"{"symbols":[{
    "symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL",
    "quoteAsset":"USDT","marginAsset":"USDT","filters":[
        {"filterType":"PRICE_FILTER","tickSize":"0.1"},
        {"filterType":"LOT_SIZE","minQty":"0.001","maxQty":"100","stepSize":"0.001"},
        {"filterType":"MARKET_LOT_SIZE","minQty":"0.001","maxQty":"100",
         "stepSize":"0.001"},
        {"filterType":"MIN_NOTIONAL","notional":"5"}
    ]
}]}"#;

fn gateway(server: &TestServer) -> BinanceGateway {
    BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("test-key", "test-secret"),
        vec!["BTCUSDT".to_string()],
    )
}

fn entry(stop: Option<StopSpec>) -> OrderRequest {
    OrderRequest {
        client_order_id: "eng-1700000000000-1".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.004,
        kind: OrderKind::Limit {
            px: 78_000.1,
            tif: TimeInForce::PostOnly,
        },
        stop,
        reduce_only: false,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    }
}

fn query_value<'a>(request: &'a Recorded, name: &str) -> Option<&'a str> {
    request.query.split('&').find_map(|pair| {
        let (key, value) = pair.split_once('=')?;
        (key == name).then_some(value)
    })
}

#[tokio::test(start_paused = true)]
async fn an_entry_and_stop_use_the_ordinary_and_algo_services_in_that_order() {
    let server =
        TestServer::start(
            |request, _| match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/fapi/v1/exchangeInfo") => (200, BTC_EXCHANGE_INFO.into()),
                ("POST", "/fapi/v1/order") => (
                    200,
                    r#"{"clientOrderId":"eng-1700000000000-1","orderId":41}"#.into(),
                ),
                ("POST", "/fapi/v1/algoOrder") => (
                    200,
                    format!(
                        r#"{{"clientAlgoId":"{}","algoId":42,"algoStatus":"NEW"}}"#,
                        query_value(request, "clientAlgoId").unwrap()
                    ),
                ),
                _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
            },
        )
        .await;
    let mut gateway = gateway(&server);

    let ack = gateway
        .send_order(&entry(Some(StopSpec {
            trigger_px: 75_000.5,
        })))
        .await
        .unwrap();
    assert_eq!(ack.venue_order_id, "41");

    let requests = server.requests();
    assert_eq!(requests.len(), 3);
    assert_eq!(requests[0].path, "/fapi/v1/exchangeInfo");
    let ordinary = &requests[1];
    assert_eq!(ordinary.path, "/fapi/v1/order");
    assert_eq!(query_value(ordinary, "type"), Some("LIMIT"));
    assert_eq!(query_value(ordinary, "timeInForce"), Some("GTX"));
    assert_eq!(
        query_value(ordinary, "newClientOrderId"),
        Some("eng-1700000000000-1")
    );
    assert_eq!(ordinary.header("x-mbx-apikey"), Some("test-key"));
    assert!(query_value(ordinary, "signature").is_some());

    let stop = &requests[2];
    assert_eq!(stop.path, "/fapi/v1/algoOrder");
    assert_eq!(query_value(stop, "algoType"), Some("CONDITIONAL"));
    assert_eq!(query_value(stop, "type"), Some("STOP_MARKET"));
    assert!(
        query_value(stop, "timeInForce").is_none(),
        "the current Algo schema does not allow a TIF on STOP_MARKET"
    );
    assert_eq!(query_value(stop, "closePosition"), Some("true"));
    assert_eq!(query_value(stop, "triggerPrice"), Some("75000.5"));
    assert_eq!(query_value(stop, "workingType"), Some("MARK_PRICE"));
    assert!(query_value(stop, "clientAlgoId")
        .unwrap()
        .starts_with("engstop-"));
    assert!(query_value(stop, "newClientOrderId").is_none());
    assert!(query_value(stop, "quantity").is_none());
}

#[tokio::test(start_paused = true)]
async fn cancelling_an_entry_after_restart_cancels_its_exact_attached_algo_stop() {
    let server =
        TestServer::start(
            |request, _| match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/fapi/v1/exchangeInfo") => (200, BTC_EXCHANGE_INFO.into()),
                ("POST", "/fapi/v1/order") => (
                    200,
                    r#"{"clientOrderId":"eng-1700000000000-1","orderId":41}"#.into(),
                ),
                ("POST", "/fapi/v1/algoOrder") => (
                    200,
                    format!(
                        r#"{{"clientAlgoId":"{}","algoId":42,"algoStatus":"NEW"}}"#,
                        query_value(request, "clientAlgoId").unwrap()
                    ),
                ),
                ("DELETE", "/fapi/v1/order") => (
                    200,
                    r#"{"clientOrderId":"eng-1700000000000-1","orderId":41,
                     "status":"CANCELED","executedQty":"0"}"#
                        .into(),
                ),
                ("DELETE", "/fapi/v1/algoOrder") => (
                    200,
                    format!(
                        r#"{{"clientAlgoId":"{}","algoId":42,"code":"200","msg":"success"}}"#,
                        query_value(request, "clientAlgoId").unwrap()
                    ),
                ),
                _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
            },
        )
        .await;

    let mut placing = gateway(&server);
    placing
        .send_order(&entry(Some(StopSpec {
            trigger_px: 75_000.5,
        })))
        .await
        .unwrap();
    drop(placing);

    let mut restarted = gateway(&server);
    restarted
        .cancel_order(SymbolId(0), "eng-1700000000000-1")
        .await
        .unwrap();

    let requests = server.requests();
    assert_eq!(requests.len(), 5);
    let placed_stop_id = query_value(&requests[2], "clientAlgoId").unwrap();
    let cancelled_stop_id = query_value(&requests[4], "clientAlgoId").unwrap();
    assert_eq!(cancelled_stop_id, placed_stop_id);
    assert!(cancelled_stop_id.starts_with("engstop-"));
    assert!(cancelled_stop_id.len() <= 36);
    assert_eq!(requests[3].path, "/fapi/v1/order");
    assert_eq!(
        query_value(&requests[3], "origClientOrderId"),
        Some("eng-1700000000000-1")
    );
    assert_eq!(requests[4].path, "/fapi/v1/algoOrder");
    assert!(query_value(&requests[4], "symbol").is_none());
    assert!(query_value(&requests[4], "algoId").is_none());
}

#[tokio::test(start_paused = true)]
async fn cancelling_a_partially_filled_entry_keeps_its_position_stop() {
    let server =
        TestServer::start(
            |request, _| match (request.method.as_str(), request.path.as_str()) {
                ("DELETE", "/fapi/v1/order") => (
                    200,
                    r#"{"clientOrderId":"eng-1700000000000-1","orderId":41,
                     "status":"CANCELED","executedQty":"0.001"}"#
                        .into(),
                ),
                ("DELETE", "/fapi/v1/algoOrder") => (
                    500,
                    r#"{"code":-1,"msg":"a partial fill's stop must stay"}"#.into(),
                ),
                _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
            },
        )
        .await;
    let mut gateway = gateway(&server);

    gateway
        .cancel_order(SymbolId(0), "eng-1700000000000-1")
        .await
        .unwrap();

    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].path, "/fapi/v1/order");
}

#[tokio::test(start_paused = true)]
async fn a_refused_stop_cancels_the_accepted_entry_and_keeps_the_outcome_unknown() {
    let server =
        TestServer::start(
            |request, _| match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/fapi/v1/exchangeInfo") => (200, BTC_EXCHANGE_INFO.into()),
                ("POST", "/fapi/v1/order") => (
                    200,
                    r#"{"clientOrderId":"eng-1700000000000-1","orderId":41}"#.into(),
                ),
                ("POST", "/fapi/v1/algoOrder") => (
                    400,
                    r#"{"code":-2021,"msg":"Order would immediately trigger."}"#.into(),
                ),
                ("DELETE", "/fapi/v1/order") => (
                    200,
                    r#"{"clientOrderId":"eng-1700000000000-1","orderId":41,"status":"CANCELED"}"#
                        .into(),
                ),
                _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
            },
        )
        .await;
    let mut gateway = gateway(&server);

    let error = gateway
        .send_order(&entry(Some(StopSpec {
            trigger_px: 75_000.5,
        })))
        .await
        .unwrap_err();
    assert!(matches!(error, VenueError::BadReply(_)), "{error:?}");
    assert!(error.to_string().contains("reconcile"), "{error}");

    let requests = server.requests();
    assert_eq!(requests.len(), 4);
    assert_eq!(requests[3].method, "DELETE");
    assert_eq!(query_value(&requests[3], "symbol"), Some("BTCUSDT"));
    assert_eq!(
        query_value(&requests[3], "origClientOrderId"),
        Some("eng-1700000000000-1")
    );
}

#[tokio::test(start_paused = true)]
async fn account_identity_comes_from_signed_balance_and_checks_position_mode() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/fapi/v3/balance" => (
            200,
            r#"[{"accountAlias":"SgsR","asset":"USDT"},{"accountAlias":"SgsR","asset":"USDC"}]"#
                .into(),
        ),
        "/fapi/v1/multiAssetsMargin" => (200, r#"{"multiAssetsMargin":false}"#.into()),
        "/fapi/v1/positionSide/dual" => (200, r#"{"dualSidePosition":false}"#.into()),
        _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
    })
    .await;
    let mut gateway = gateway(&server);

    let identity = gateway.account_identity().await.unwrap();
    assert_eq!(identity.venue, "binance");
    assert_eq!(identity.user_id, "SgsR");
    assert_eq!(identity.realm, "binance_testnet");

    let requests = server.requests();
    assert_eq!(requests.len(), 3);
    assert_eq!(requests[0].path, "/fapi/v3/balance");
    assert_eq!(requests[1].path, "/fapi/v1/multiAssetsMargin");
    assert_eq!(requests[2].path, "/fapi/v1/positionSide/dual");
    for request in requests {
        assert_eq!(request.method, "GET");
        assert_eq!(request.header("x-mbx-apikey"), Some("test-key"));
        assert!(query_value(&request, "timestamp").is_some());
        assert!(query_value(&request, "signature").is_some());
    }
}

#[tokio::test(start_paused = true)]
async fn account_identity_rejects_multi_assets_mode_before_position_mode() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/fapi/v3/balance" => (200, r#"[{"accountAlias":"SgsR","asset":"USDT"}]"#.into()),
        "/fapi/v1/multiAssetsMargin" => (200, r#"{"multiAssetsMargin":true}"#.into()),
        "/fapi/v1/positionSide/dual" => (
            500,
            r#"{"code":-1,"msg":"multi-assets must stop startup first"}"#.into(),
        ),
        _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
    })
    .await;
    let mut gateway = gateway(&server);

    let error = gateway.account_identity().await.unwrap_err();
    assert!(matches!(error, VenueError::BadRequest(_)), "{error:?}");
    assert!(error.to_string().contains("multi-assets mode"), "{error}");
    assert!(error.to_string().contains("literal USDT"), "{error}");
    let requests = server.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].path, "/fapi/v3/balance");
    assert_eq!(requests[1].path, "/fapi/v1/multiAssetsMargin");
    for request in requests {
        assert_eq!(request.method, "GET");
        assert!(query_value(&request, "timestamp").is_some());
        assert!(query_value(&request, "signature").is_some());
    }
}

#[tokio::test(start_paused = true)]
async fn replacing_a_stop_cancels_only_same_side_algos_by_algo_id() {
    let server =
        TestServer::start(
            |request, _| match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/fapi/v2/account") => (
                    200,
                    r#"{"positions":[{"symbol":"BTCUSDT","positionAmt":"0.004"}]}"#.into(),
                ),
                ("GET", "/fapi/v1/openAlgoOrders") => (
                    200,
                    r#"[
                {"algoId":11,"clientAlgoId":"old-long","algoType":"CONDITIONAL",
                 "symbol":"BTCUSDT","side":"SELL","orderType":"STOP_MARKET",
                 "closePosition":true,"triggerPrice":"75000","workingType":"MARK_PRICE"},
                {"algoId":12,"clientAlgoId":"wrong-side","algoType":"CONDITIONAL",
                 "symbol":"BTCUSDT","side":"BUY","orderType":"STOP_MARKET",
                 "closePosition":true,"triggerPrice":"81000","workingType":"MARK_PRICE"}
            ]"#
                    .into(),
                ),
                ("POST", "/fapi/v1/algoOrder") => (
                    200,
                    format!(
                        r#"{{"clientAlgoId":"{}","algoId":13,"algoStatus":"NEW"}}"#,
                        query_value(request, "clientAlgoId").unwrap()
                    ),
                ),
                ("DELETE", "/fapi/v1/algoOrder") => {
                    (400, r#"{"code":-2011,"msg":"Unknown order sent."}"#.into())
                }
                _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
            },
        )
        .await;
    let mut gateway = gateway(&server);

    gateway.set_stop(SymbolId(0), 76_000.5).await.unwrap();

    let deletes: Vec<_> = server
        .requests()
        .into_iter()
        .filter(|request| request.method == "DELETE")
        .collect();
    assert_eq!(deletes.len(), 1);
    assert_eq!(query_value(&deletes[0], "algoId"), Some("11"));
    assert!(query_value(&deletes[0], "symbol").is_none());
    assert!(query_value(&deletes[0], "clientAlgoId").is_none());
    assert!(query_value(&deletes[0], "signature").is_some());
}

#[tokio::test(start_paused = true)]
async fn replacing_a_stop_surfaces_any_cancel_refusal_other_than_known_already_gone() {
    let server =
        TestServer::start(
            |request, _| match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/fapi/v2/account") => (
                    200,
                    r#"{"positions":[{"symbol":"BTCUSDT","positionAmt":"0.004"}]}"#.into(),
                ),
                ("GET", "/fapi/v1/openAlgoOrders") => (
                    200,
                    r#"[{"algoId":11,"clientAlgoId":"old-long","algoType":"CONDITIONAL",
                         "symbol":"BTCUSDT","side":"SELL","orderType":"STOP_MARKET",
                         "closePosition":true,"triggerPrice":"75000",
                         "workingType":"MARK_PRICE"}]"#
                        .into(),
                ),
                ("POST", "/fapi/v1/algoOrder") => (
                    200,
                    format!(
                        r#"{{"clientAlgoId":"{}","algoId":13,"algoStatus":"NEW"}}"#,
                        query_value(request, "clientAlgoId").unwrap()
                    ),
                ),
                ("DELETE", "/fapi/v1/algoOrder") => (
                    400,
                    r#"{"code":-4120,"msg":"Order type not supported for this endpoint."}"#.into(),
                ),
                _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
            },
        )
        .await;
    let mut gateway = gateway(&server);

    let error = gateway.set_stop(SymbolId(0), 76_000.5).await.unwrap_err();
    assert!(matches!(error, VenueError::Rejected { code: -4120, .. }));
}

#[tokio::test(start_paused = true)]
async fn market_orders_use_the_distinct_market_lot_minimum_step_and_maximum() {
    let exchange_info = r#"{"symbols":[{
        "symbol":"ARKUSDT","status":"TRADING","contractType":"PERPETUAL",
        "quoteAsset":"USDT","marginAsset":"USDT","filters":[
            {"filterType":"PRICE_FILTER","tickSize":"0.001"},
            {"filterType":"LOT_SIZE","minQty":"3","maxQty":"900000","stepSize":"3"},
            {"filterType":"MARKET_LOT_SIZE","minQty":"1","maxQty":"30","stepSize":"1"},
            {"filterType":"MIN_NOTIONAL","notional":"5"}
        ]
    }]}"#;
    let server = TestServer::start(move |request, _| match request.path.as_str() {
        "/fapi/v1/exchangeInfo" => (200, exchange_info.into()),
        "/fapi/v1/order" => (
            200,
            format!(
                r#"{{"clientOrderId":"{}","orderId":41}}"#,
                query_value(request, "newClientOrderId").unwrap()
            ),
        ),
        _ => (404, r#"{"code":-1,"msg":"unexpected path"}"#.into()),
    })
    .await;
    let mut gateway = BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("test-key", "test-secret"),
        vec!["ARKUSDT".to_string()],
    );

    let rules = gateway.instrument_rules().await.unwrap();
    assert_eq!(rules[0].1.min_qty, 3.0);
    assert_eq!(rules[0].1.qty_step, 3.0);

    let too_large_entry = OrderRequest {
        client_order_id: "eng-limit-33".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 33.0,
        kind: OrderKind::Limit {
            px: 1.0,
            tif: TimeInForce::PostOnly,
        },
        stop: None,
        reduce_only: false,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    };
    let error = gateway.send_order(&too_large_entry).await.unwrap_err();
    assert!(matches!(error, VenueError::BadRequest(_)), "{error:?}");
    assert!(
        error.to_string().contains("LOT_SIZE and MARKET_LOT_SIZE"),
        "{error}"
    );

    let market = |qty| OrderRequest {
        client_order_id: "eng-market-1".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: true,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    };
    for bad in [0.5, 1.5, 31.0] {
        let error = gateway.send_order(&market(bad)).await.unwrap_err();
        assert!(
            matches!(error, VenueError::BadRequest(_)),
            "{bad}: {error:?}"
        );
    }
    gateway.send_order(&market(30.0)).await.unwrap();

    let requests = server.requests();
    assert_eq!(requests.len(), 2, "invalid market sizes reached the wire");
    assert_eq!(requests[0].path, "/fapi/v1/exchangeInfo");
    assert_eq!(requests[1].path, "/fapi/v1/order");
    assert_eq!(query_value(&requests[1], "type"), Some("MARKET"));
    assert_eq!(query_value(&requests[1], "quantity"), Some("30"));
}

#[tokio::test(start_paused = true)]
async fn execution_recovery_is_unavailable_and_never_reaches_the_wire() {
    let server =
        TestServer::start(|_, _| (500, r#"{"code":-1,"msg":"must not be called"}"#.into())).await;
    let mut gateway = gateway(&server);
    let end_ms = engine_types::clock::wall_ms();

    let error = gateway
        .executions(end_ms - 1_000, end_ms)
        .await
        .unwrap_err();
    assert!(matches!(error, VenueError::BadRequest(_)), "{error:?}");
    assert!(error.to_string().contains("unavailable"), "{error}");
    assert!(error.to_string().contains("account-wide"), "{error}");
    assert!(error.to_string().contains("90 days"), "{error}");
    assert!(server.requests().is_empty());
}

#[tokio::test(start_paused = true)]
async fn private_stream_retries_key_failure_resets_before_news_and_deduplicates_after_expiry() {
    use engine_types::{FeedError, OrderFeed, OrderUpdate};
    use engine_venue::BinanceOrderFeed;
    use futures_util::{SinkExt, StreamExt};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;
    use tokio_tungstenite::tungstenite::Message;

    let rest = TestServer::start(|request, count| {
        assert_eq!(request.method, "POST");
        assert_eq!(request.path, "/fapi/v1/listenKey");
        assert_eq!(request.header("x-mbx-apikey"), Some("test-key"));
        if count == 0 {
            (503, r#"{"msg":"unavailable"}"#.into())
        } else {
            (200, format!(r#"{{"listenKey":"key-{count}"}}"#))
        }
    })
    .await;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let ws_url = format!("ws://{}", listener.local_addr().unwrap());
    let paths = Arc::new(Mutex::new(Vec::new()));
    let captured = paths.clone();
    let (closed, observed_close) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let ack = |client: &str| {
            serde_json::json!({"e":"ORDER_TRADE_UPDATE", "o":{
                "c":client, "i":42, "s":"BTCUSDT", "x":"NEW", "X":"NEW"
            }})
            .to_string()
        };
        for connection in 1..=2 {
            let (stream, _) = listener.accept().await.unwrap();
            let paths = captured.clone();
            let mut socket = tokio_tungstenite::accept_hdr_async(
                stream,
                move |request: &tokio_tungstenite::tungstenite::handshake::server::Request,
                      response| {
                    paths.lock().unwrap().push(request.uri().to_string());
                    Ok(response)
                },
            )
            .await
            .unwrap();
            socket
                .send(Message::Text(ack("client-1").into()))
                .await
                .unwrap();
            if connection == 1 {
                socket
                    .send(Message::Text(r#"{"e":"listenKeyExpired"}"#.into()))
                    .await
                    .unwrap();
            } else {
                socket
                    .send(Message::Text(ack("client-2").into()))
                    .await
                    .unwrap();
                while let Some(Ok(message)) = socket.next().await {
                    if matches!(message, Message::Close(_)) {
                        break;
                    }
                }
            }
        }
        let _ = closed.send(());
    });
    let mut feed = BinanceOrderFeed::for_test(
        &rest.base_url(),
        &ws_url,
        BinanceRealm::Testnet.credentials_for_test("test-key", "test-secret"),
        vec!["BTCUSDT".into()],
    );
    let sequence = tokio::time::timeout(Duration::from_secs(3), async {
        assert!(matches!(feed.next_update().await,Err(FeedError::Transport(_))));
        assert!(matches!(feed.next_update().await,Ok(OrderUpdate::StreamReset{..})));
        assert!(matches!(feed.next_update().await,Ok(OrderUpdate::Ack(ack)) if ack.client_order_id == "client-1"));
        assert!(matches!(feed.next_update().await,Err(FeedError::Transport(message)) if message.contains("expired")));
        tokio::time::advance(Duration::from_millis(250)).await;
        assert!(matches!(feed.next_update().await,Ok(OrderUpdate::StreamReset{..})));
        assert!(matches!(feed.next_update().await,Ok(OrderUpdate::Ack(ack)) if ack.client_order_id == "client-2"));
    }).await;
    sequence.expect("private stream must recover through both sessions");
    assert_eq!(
        *paths.lock().unwrap(),
        vec![
            "/private/ws?listenKey=key-1&events=ORDER_TRADE_UPDATE/ALGO_UPDATE",
            "/private/ws?listenKey=key-2&events=ORDER_TRADE_UPDATE/ALGO_UPDATE",
        ]
    );
    drop(feed);
    tokio::time::timeout(Duration::from_secs(1), observed_close)
        .await
        .unwrap()
        .unwrap();
    server.await.unwrap();
    assert_eq!(rest.to_path("/fapi/v1/listenKey").len(), 3);
}

#[tokio::test(start_paused = true)]
async fn independent_catalog_installs_market_rules_before_a_mutation_without_another_read() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/fapi/v1/exchangeInfo" => (200, BTC_EXCHANGE_INFO.into()),
        "/fapi/v1/order" => (
            200,
            r#"{"clientOrderId":"eng-1700000000000-1","orderId":41}"#.into(),
        ),
        other => panic!("unexpected {other}"),
    })
    .await;
    let mut gw = gateway(&server);
    let catalog = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    assert_eq!(catalog.rules.len(), 1);
    assert_eq!(catalog.specs.len(), 1);
    gw.install_instrument_catalog(&catalog).unwrap();
    gw.send_order(&entry(None)).await.unwrap();
    assert_eq!(server.to_path("/fapi/v1/exchangeInfo").len(), 1);
    let other = TestServer::start(|_, _| (503, "unavailable".into())).await;
    assert!(gateway(&other)
        .install_instrument_catalog(&catalog)
        .is_err());
    assert!(gateway(&other)
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .is_err());
}

#[tokio::test(start_paused = true)]
async fn exact_amend_preserves_unmodified_native_quantity_and_checks_identity() {
    use engine_types::numeric::Exact;
    use engine_types::order_terms::{ExactAmendTerms, OrderInputPolicy};
    for foreign in [false, true] {
        let server=TestServer::start(move|request,_| match (request.method.as_str(),request.path.as_str()) {
            ("GET","/fapi/v1/exchangeInfo")=>(200,BTC_EXCHANGE_INFO.replace("\"0.1\"","\"0.000000000000000000001\"").replace("\"0.001\"","\"0.000000000000000000001\"")),
            ("GET","/fapi/v1/order")=>(200,format!(r#"{{"symbol":"{}","clientOrderId":"eng-1","type":"LIMIT","side":"BUY","price":"99.1","origQty":0.123456789012345678901}}"#,if foreign{"ETHUSDT"}else{"BTCUSDT"})),
            ("PUT","/fapi/v1/order")=>(200,"{}".into()),
            _=>panic!("unexpected request"),
        }).await;
        let mut gw = gateway(&server);
        let catalog = gw
            .instrument_catalog_client()
            .unwrap()
            .fetch()
            .await
            .unwrap();
        gw.install_instrument_catalog(&catalog).unwrap();
        let mut spec = engine_types::AmendSpec {
            px: None,
            qty: None,
            exact_terms: None,
        };
        ExactAmendTerms {
            quantity: None,
            limit_price: Some(Exact::parse_decimal("100.123456789012345678901").unwrap()),
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(&mut spec)
        .unwrap();
        let result = gw.amend_order(SymbolId(0), "eng-1", spec).await;
        let requests = server.to_path("/fapi/v1/order");
        let sent = requests.iter().find(|request| request.method == "PUT");
        if foreign {
            assert!(result.is_err());
            assert!(sent.is_none());
        } else {
            result.unwrap();
            let sent = sent.unwrap();
            assert_eq!(
                query_value(sent, "price"),
                Some("100.123456789012345678901")
            );
            assert_eq!(
                query_value(sent, "quantity"),
                Some("0.123456789012345678901")
            );
            assert!(query_value(sent, "signature").is_some());
        }
    }
}

#[tokio::test(start_paused = true)]
async fn independent_account_recovery_uses_requested_ids_and_preserves_history_unavailability() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/fapi/v2/account" => (200, r#"{"totalMarginBalance":"1500.25","availableBalance":"1200.5","positions":[{"symbol":"BTCUSDT","positionAmt":"0.004","entryPrice":"78000.5","leverage":"6","positionSide":"BOTH"}]}"#.into()),
        "/fapi/v1/openAlgoOrders" => (200, r#"[{"symbol":"BTCUSDT","algoType":"CONDITIONAL","orderType":"STOP_MARKET","closePosition":true,"workingType":"MARK_PRICE","side":"SELL","triggerPrice":"75000"}]"#.into()),
        other => panic!("unexpected read path {other}"),
    }).await;
    let gw = gateway(&server);
    let client = gw
        .account_recovery_client()
        .expect("independent account recovery client");
    let names = vec!["ETHUSDT".into(), "BTCUSDT".into()];
    let scan_started_after = engine_types::clock::mono_ns();
    let view = client.account_view(&names).await.unwrap();
    let scan_completed_before = engine_types::clock::mono_ns();
    assert_eq!((view.equity_usdt, view.available_usdt), (1500.25, 1200.5));
    assert_eq!(view.positions[0].symbol, SymbolId(1));
    assert_eq!(view.positions[0].qty, 0.004);
    assert_eq!(view.positions[0].stop_px, 75000.0);
    assert!(view.positions[0].stop_attached);
    assert!((scan_started_after..=scan_completed_before).contains(&view.observed_ns));
    assert!(
        matches!(client.executions(&names, 1, 2).await, Err(VenueError::BadRequest(reason)) if reason.starts_with("Binance execution recovery is unavailable:"))
    );
    assert_eq!(server.only("/fapi/v1/openAlgoOrders").method, "GET");
}
