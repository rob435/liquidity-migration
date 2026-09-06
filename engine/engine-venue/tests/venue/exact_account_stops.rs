use crate::support::TestServer;
use engine_types::numeric::Exact;
use engine_types::{AccountView, VenueGateway};
use engine_venue::{
    BinanceGateway, BinanceRealm, BybitGateway, HyperliquidGateway, HyperliquidRealm,
    LighterGateway, LighterRealm, MexcGateway, MexcRealm, RealmCredentials, VenueRealm,
};

const PRICE: &str = "89.99999999999999999999";
fn assert_exact(view: AccountView, expected_quantity: &str) {
    assert_eq!(
        view.equity().unwrap(),
        Exact::parse_decimal("1500.0000000000000000001").unwrap()
    );
    assert_eq!(
        view.available().unwrap(),
        Exact::parse_decimal("1199.9999999999999999999").unwrap()
    );
    assert_eq!(view.positions.len(), 1);
    let position = &view.positions[0];
    assert_eq!(
        position.quantity().unwrap(),
        Exact::parse_decimal(expected_quantity).unwrap()
    );
    assert_eq!(
        position.entry_price().unwrap(),
        Exact::parse_decimal("100.0000000000000000001").unwrap()
    );
    assert_eq!(position.stop_px, 90.0);
    assert!(position.stop_attached);
    assert_eq!(
        position.exact_stop_px.as_deref(),
        Some(&Exact::parse_decimal(PRICE).unwrap()),
        "account stop decimal vanished before confirmation"
    );
    let restored: AccountView =
        serde_json::from_slice(&serde_json::to_vec(&view).unwrap()).unwrap();
    assert_eq!(restored, view);
}
#[tokio::test]
async fn bybit_account_amounts_and_stop_preserve_escaped_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/v5/account/wallet-balance" => r#"{"retCode":0,"result":{"list":[{"accountType":"UNIFIED","totalEquity":"1500.0000000000000000001","totalAvailableBalance":"1199.9999999999999999999","coin":[]}]}}"#.into(),
        "/v5/position/list" => r#"{"retCode":0,"result":{"list":[{"symbol":"BTCUSDT","side":"Buy","size":"1.0000000000000000001","avgPrice":"100.0000000000000000001","stopLoss":"89.9999999999999999999\u0039","positionIdx":0}],"nextPageCursor":""}}"#.into(),
        other => panic!("unexpected path {other}"),
    })).await;
    let gateway = BybitGateway::for_test(
        &server.base_url(),
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    assert_exact(
        gateway
            .account_recovery_client()
            .unwrap()
            .account_view(&["BTCUSDT".into()])
            .await
            .unwrap(),
        "1.0000000000000000001",
    );
}
#[tokio::test]
async fn binance_account_amounts_and_stop_preserve_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/fapi/v2/account" => r#"{"totalMarginBalance":"1500.0000000000000000001","availableBalance":"1199.9999999999999999999","positions":[{"symbol":"BTCUSDT","positionAmt":"1.0000000000000000001","entryPrice":"100.0000000000000000001","leverage":"6","positionSide":"BOTH"}]}"#.into(),
        "/fapi/v1/openAlgoOrders" => format!(r#"[{{"symbol":"BTCUSDT","algoType":"CONDITIONAL","orderType":"STOP_MARKET","closePosition":true,"workingType":"MARK_PRICE","side":"SELL","triggerPrice":{PRICE}}}]"#),
        other => panic!("unexpected path {other}"),
    })).await;
    let gateway = BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    assert_exact(
        gateway
            .account_recovery_client()
            .unwrap()
            .account_view(&["BTCUSDT".into()])
            .await
            .unwrap(),
        "1.0000000000000000001",
    );
}
#[tokio::test]
async fn hyperliquid_account_amounts_and_stop_preserve_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.json()["type"].as_str().unwrap() {
        "clearinghouseState" => r#"{"marginSummary":{"accountValue":"1500.0000000000000000001","totalMarginUsed":"300"},"withdrawable":"1199.9999999999999999999","assetPositions":[{"position":{"coin":"BTC","szi":"1.0000000000000000001","entryPx":"100.0000000000000000001","leverage":{"type":"cross","value":20}}}]}"#.into(),
        "frontendOpenOrders" => format!(r#"[{{"coin":"BTC","side":"A","sz":"1.0000000000000000001","origSz":"1.0000000000000000001","oid":77,"reduceOnly":true,"isTrigger":true,"orderType":"Stop Market","triggerPx":{PRICE}}}]"#),
        other => panic!("unexpected kind {other}"),
    })).await;
    let gateway = HyperliquidGateway::for_test(
        &server.base_url(),
        HyperliquidRealm::Testnet,
        HyperliquidRealm::Testnet.credentials_for_test(
            "0x0000000000000000000000000000000000000001",
            "0x0123456789012345678901234567890123456789012345678901234567890123",
        ),
        vec!["BTCUSDT".into()],
    )
    .unwrap();
    assert_exact(
        gateway
            .account_recovery_client()
            .unwrap()
            .account_view(&["BTCUSDT".into()])
            .await
            .unwrap(),
        "1.0000000000000000001",
    );
}
#[tokio::test]
async fn lighter_account_amounts_and_stop_preserve_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/api/v1/account" => r#"{"code":200,"accounts":[{"collateral":"1500.0000000000000000001","available_balance":"1199.9999999999999999999","positions":[{"market_id":0,"symbol":"BTC","sign":1,"position":"1.0000000000000000001","avg_entry_price":"100.0000000000000000001","initial_margin_fraction":"0.05"}]}]}"#.into(),
        "/api/v1/accountActiveOrders" => format!(r#"{{"code":200,"orders":[{{"market_index":0,"order_index":77,"remaining_base_amount":"1.0000000000000000001","reduce_only":true,"type":"stop-loss","is_ask":true,"trigger_price":{PRICE}}}]}}"#),
        other => panic!("unexpected path {other}"),
    })).await;
    let gateway = LighterGateway::for_test(
        &server.base_url(),
        LighterRealm::Testnet,
        LighterRealm::Testnet.credentials_for_test(
            "42:3",
            "0101010101010101010101010101010101010101010101010101010101010101010101010101010f",
        ),
        vec!["BTCUSDT".into()],
    )
    .unwrap();
    assert_exact(
        gateway
            .account_recovery_client()
            .unwrap()
            .account_view(&["BTCUSDT".into()])
            .await
            .unwrap(),
        "1.0000000000000000001",
    );
}
#[tokio::test]
async fn mexc_account_amounts_and_stop_preserve_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/api/v1/contract/detail" => r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into(),
        "/api/v1/private/account/assets" => r#"{"success":true,"code":0,"data":[{"currency":"USDT","equity":1500.0000000000000000001,"availableBalance":1199.9999999999999999999}]}"#.into(),
        "/api/v1/private/position/open_positions" => r#"{"success":true,"code":0,"data":[{"positionId":"7","symbol":"BTC_USDT","holdVol":9007199254740993,"positionType":1,"holdAvgPrice":100.0000000000000000001,"leverage":2,"state":1}]}"#.into(),
        "/api/v1/private/stoporder/open_orders" => format!(r#"{{"success":true,"code":0,"data":[{{"positionId":"7","orderId":"0","stopLossPrice":{PRICE}}}]}}"#),
        other => panic!("unexpected path {other}"),
    })).await;
    let mut gateway = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    let catalog = gateway
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    gateway.install_instrument_catalog(&catalog).unwrap();
    assert_exact(
        gateway
            .account_recovery_client()
            .unwrap()
            .account_view(&["BTCUSDT".into()])
            .await
            .unwrap(),
        "900719925474.0993",
    );
}

async fn hyperliquid_coverage(
    orders: String,
    short: bool,
) -> Result<AccountView, engine_types::VenueError> {
    let server = TestServer::start(move |request,_| (200, match request.json()["type"].as_str().unwrap() {
        "clearinghouseState" => format!(r#"{{"marginSummary":{{"accountValue":"1500","totalMarginUsed":"300"}},"withdrawable":"1200","assetPositions":[{{"position":{{"coin":"BTC","szi":"{}1.0000000000000000001","entryPx":"100","leverage":{{"type":"cross","value":20}}}}}}]}}"#, if short { "-" } else { "" }),
        "frontendOpenOrders" => orders.clone(),
        other => panic!("unexpected kind {other}"),
    })).await;
    let gateway = HyperliquidGateway::for_test(
        &server.base_url(),
        HyperliquidRealm::Testnet,
        HyperliquidRealm::Testnet.credentials_for_test(
            "0x0000000000000000000000000000000000000001",
            "0x0123456789012345678901234567890123456789012345678901234567890123",
        ),
        vec!["BTCUSDT".into()],
    )
    .unwrap();
    gateway
        .account_recovery_client()
        .unwrap()
        .account_view(&["BTCUSDT".into()])
        .await
}
async fn lighter_coverage(
    orders: String,
    short: bool,
) -> Result<AccountView, engine_types::VenueError> {
    let server = TestServer::start(move |request,_| (200, match request.path.as_str() {
        "/api/v1/account" => format!(r#"{{"code":200,"accounts":[{{"collateral":"1500","available_balance":"1200","positions":[{{"market_id":0,"symbol":"BTC","sign":{},"position":"1.0000000000000000001","avg_entry_price":"100","initial_margin_fraction":"0.05"}}]}}]}}"#, if short { -1 } else { 1 }),
        "/api/v1/accountActiveOrders" => format!(r#"{{"code":200,"orders":{orders}}}"#),
        other => panic!("unexpected path {other}"),
    })).await;
    let gateway = LighterGateway::for_test(
        &server.base_url(),
        LighterRealm::Testnet,
        LighterRealm::Testnet.credentials_for_test(
            "42:3",
            "0101010101010101010101010101010101010101010101010101010101010101010101010101010f",
        ),
        vec!["BTCUSDT".into()],
    )
    .unwrap();
    gateway
        .account_recovery_client()
        .unwrap()
        .account_view(&["BTCUSDT".into()])
        .await
}
fn hyper_stop(id: u64, short: bool, quantity: &str, price: &str) -> serde_json::Value {
    serde_json::json!({"coin":"BTC","oid":id,"side":if short {"B"} else {"A"},"sz":quantity,"isTrigger":true,"reduceOnly":true,"orderType":"Stop Market","triggerPx":price})
}
fn lighter_stop(id: u64, short: bool, quantity: &str, price: &str) -> serde_json::Value {
    serde_json::json!({"market_index":0,"order_index":id,"is_ask":!short,"remaining_base_amount":quantity,"reduce_only":true,"type":"stop-loss","trigger_price":price})
}

#[tokio::test]
async fn native_stop_coverage_requires_the_correct_side_and_every_canonical_unit() {
    for lighter in [false, true] {
        for short in [false, true] {
            let stop = if lighter { lighter_stop } else { hyper_stop };
            let price = if short { "110" } else { "90" };
            for (name, orders, protected) in [
                (
                    "opposite direction",
                    vec![stop(1, !short, "2", price)],
                    false,
                ),
                ("sub-ulp shortfall", vec![stop(1, short, "1", price)], false),
                (
                    "complete",
                    vec![stop(1, short, "1.0000000000000000001", price)],
                    true,
                ),
            ] {
                let encoded = serde_json::to_string(&orders).unwrap();
                let view = if lighter {
                    lighter_coverage(encoded, short).await
                } else {
                    hyperliquid_coverage(encoded, short).await
                }
                .unwrap();
                let position = &view.positions[0];
                assert_eq!(
                    position.stop_attached, protected,
                    "lighter={lighter}, short={short}: {name}"
                );
                assert_eq!(position.exact_stop_px.is_some(), protected);
                assert_eq!(
                    position.quantity().unwrap(),
                    Exact::parse_decimal("1.0000000000000000001").unwrap()
                );
                let restored: AccountView =
                    serde_json::from_slice(&serde_json::to_vec(&view).unwrap()).unwrap();
                assert_eq!(restored, view);
            }
        }
    }
}

#[tokio::test]
async fn native_stop_coverage_aggregates_unique_orders_at_the_full_size_trigger() {
    for lighter in [false, true] {
        for short in [false, true] {
            let stop = if lighter { lighter_stop } else { hyper_stop };
            let tight = if short { "105" } else { "95" };
            let loose = if short { "110" } else { "90" };
            let orders = vec![
                stop(1, short, "0.5", tight),
                stop(2, short, "0.5000000000000000001", loose),
            ];
            let encoded = serde_json::to_string(&orders).unwrap();
            let view = if lighter {
                lighter_coverage(encoded, short).await
            } else {
                hyperliquid_coverage(encoded, short).await
            }
            .unwrap();
            assert_eq!(
                view.positions[0].stop_price().unwrap(),
                Exact::parse_decimal(loose).unwrap()
            );
            let duplicate = serde_json::to_string(&vec![stop(1, short, "0.6", tight); 2]).unwrap();
            let view = if lighter {
                lighter_coverage(duplicate, short).await
            } else {
                hyperliquid_coverage(duplicate, short).await
            };
            assert!(
                matches!(view, Err(engine_types::VenueError::BadReply(message)) if message.contains("appears twice"))
            );
        }
    }
}

#[tokio::test]
async fn lighter_account_does_not_invent_a_direction_for_malformed_native_positions() {
    for (quantity, sign) in [("1", 0), ("1", 2), ("-1", 1), ("-1", -1)] {
        let server = TestServer::start(move |request, _| {
            (200, match request.path.as_str() {
                "/api/v1/account" => format!(r#"{{"code":200,"accounts":[{{"collateral":"1500","available_balance":"1200","positions":[{{"market_id":0,"symbol":"BTC","sign":{sign},"position":"{quantity}","avg_entry_price":"100","initial_margin_fraction":"0.05"}}]}}]}}"#),
                "/api/v1/accountActiveOrders" => r#"{"code":200,"orders":[]}"#.into(),
                other => panic!("unexpected path {other}"),
            })
        }).await;
        let gateway = LighterGateway::for_test(
            &server.base_url(),
            LighterRealm::Testnet,
            LighterRealm::Testnet.credentials_for_test(
                "42:3",
                "0101010101010101010101010101010101010101010101010101010101010101010101010101010f",
            ),
            vec!["BTCUSDT".into()],
        )
        .unwrap();
        assert!(
            matches!(
                gateway
                    .account_recovery_client()
                    .unwrap()
                    .account_view(&["BTCUSDT".into()])
                    .await,
                Err(engine_types::VenueError::BadReply(_))
            ),
            "quantity={quantity}, sign={sign}"
        );
    }
}
