//! What the gateway actually puts on the wire, checked against a local
//! server. No network, no credentials.

mod support;

use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce,
    VenueError, VenueGateway,
};
use engine_venue::{BybitGateway, VenueRealm};
use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;
use support::{Recorded, TestServer};

const KEY: &str = "demoKey000000000001";
const SECRET: &str = "demoSecret00000000000000000001";

fn gateway(server: &TestServer) -> BybitGateway {
    BybitGateway::for_test(
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
        stop: Some(StopSpec { trigger_px: 93000.5 }),
        reduce_only: false,
    }
}

fn ok(result: &str) -> (u16, String) {
    (200, format!(r#"{{"retCode":0,"retMsg":"OK","result":{result},"time":1700000000000}}"#))
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
    let timestamp = request.header("x-bapi-timestamp").expect("timestamp header");
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
    // A market order carries no price and lets the venue apply its own IOC.
    assert!(body.get("price").is_none());
    assert!(body.get("timeInForce").is_none());
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
    assert!(body.get("stopLoss").is_none(), "stopLoss on a reduce-only order");
    assert!(body.get("tpslMode").is_none(), "tpslMode on a reduce-only order");
}

#[tokio::test]
async fn a_non_zero_retcode_is_a_rejection() {
    let server = TestServer::start(|_, _| {
        (200, r#"{"retCode":110007,"retMsg":"ab not enough for new order","result":{}}"#.to_string())
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
        gw.amend_order(SymbolId(0), "eng-1", AmendSpec { px: None, qty: None }).await,
        Err(VenueError::BadRequest(_))
    ));
    let mut bad_size = AmendSpec { px: None, qty: Some(f64::NAN) };
    assert!(gw.amend_order(SymbolId(0), "eng-1", bad_size).await.is_err());
    bad_size.qty = Some(0.0);
    assert!(gw.amend_order(SymbolId(0), "eng-1", bad_size).await.is_err());
    assert!(server.requests().is_empty(), "nothing should have been sent");
}

#[tokio::test]
async fn the_gateway_says_what_bybit_can_actually_do() {
    // The engine refuses actions on this word, so a wrong one here is a
    // strategy believing it has something it does not.
    let server = TestServer::start(|_, _| ok("{}")).await;
    let caps = gateway(&server).caps();
    assert!(caps.native_position_stop, "trading-stop holds the position stop");
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
            assert_eq!(request.query, "category=linear&limit=1000&cursor=page%253D2");
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

    let request = server.only("/v5/market/time");
    assert_eq!(request.method, "GET");
    assert_eq!(request.query, "");
    assert!(request.header("x-bapi-sign").is_none());
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

    // Two requests down one socket: the order did not pay for a new
    // connection, which is the whole point of warming.
    assert_eq!(server.requests().len(), 2);
    assert_eq!(server.connections(), 1);
}

#[tokio::test]
async fn a_symbol_the_gateway_does_not_know_never_reaches_the_venue() {
    let server = TestServer::start(|_, _| ok(r#"{"orderId":"ord-1"}"#)).await;
    let mut gw = gateway(&server);

    let mut request = market_order();
    request.symbol = SymbolId(9);
    assert!(gw.send_order(&request).await.is_err());
    assert!(server.requests().is_empty(), "nothing should have been sent");
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
    assert!(server.requests().is_empty(), "nothing should have been sent");
}

#[tokio::test]
async fn account_identity_asks_the_venue_whose_account_this_is() {
    let server = TestServer::start(|_, _| {
        ok(&format!(r#"{{"id":"1","apiKey":"{KEY}","userID":6039967,"readOnly":0}}"#))
    })
    .await;
    let mut gw = gateway(&server);

    let who = gw.account_identity().await.unwrap();
    assert_eq!(who.user_id, "6039967");
    assert_eq!(who.realm, "demo");

    let request = server.only("/v5/user/query-api");
    assert_eq!(request.method, "GET");
    assert_eq!(request.query, "", "the identity read takes no parameters");
    assert_signed(&request, "");
}

#[tokio::test]
async fn an_account_number_sent_as_text_reads_the_same() {
    // Bybit sends userID as a number here and as a string elsewhere. Both
    // have to land on the same lock file name or the two engines miss.
    let server = TestServer::start(|_, _| {
        ok(&format!(r#"{{"apiKey":"{KEY}","userID":"0006039967"}}"#))
    })
    .await;
    assert_eq!(gateway(&server).account_identity().await.unwrap().user_id, "6039967");
}

#[tokio::test]
async fn an_identity_about_a_different_api_key_is_refused() {
    // Whatever produced this reply, it was not the key we signed with — so
    // the account number in it is somebody else's, and taking a lock in that
    // name would leave this account unguarded.
    let server = TestServer::start(|_, _| {
        ok(r#"{"apiKey":"someoneElsesKey0001","userID":9999999}"#)
    })
    .await;
    let refused = gateway(&server).account_identity().await;
    assert!(
        matches!(refused, Err(VenueError::Credentials(_))),
        "a reply about another key was trusted: {refused:?}"
    );
}

#[tokio::test]
async fn an_identity_with_no_usable_account_number_is_refused() {
    for result in [r#"{"apiKey":"KEYHERE"}"#, r#"{"apiKey":"KEYHERE","userID":0}"#,
                   r#"{"apiKey":"KEYHERE","userID":"nope"}"#] {
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
