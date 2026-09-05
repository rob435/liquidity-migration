use crate::support::TestServer;
use engine_types::numeric::Exact;
use engine_types::{AccountView, VenueGateway};
use engine_venue::{
    BinanceGateway, BinanceRealm, BybitGateway, HyperliquidGateway, HyperliquidRealm,
    LighterGateway, LighterRealm, MexcGateway, MexcRealm, RealmCredentials, VenueRealm,
};

const PRICE: &str = "89.99999999999999999999";
fn assert_exact(view: AccountView) {
    assert_eq!(view.positions.len(), 1);
    let position = &view.positions[0];
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
async fn bybit_account_stop_preserves_escaped_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/v5/account/wallet-balance" => r#"{"retCode":0,"result":{"list":[{"accountType":"UNIFIED","totalEquity":"1500","totalAvailableBalance":"1200","coin":[]}]}}"#.into(),
        "/v5/position/list" => r#"{"retCode":0,"result":{"list":[{"symbol":"BTCUSDT","side":"Buy","size":"1","avgPrice":"100","stopLoss":"89.9999999999999999999\u0039","positionIdx":0}],"nextPageCursor":""}}"#.into(),
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
    );
}
#[tokio::test]
async fn binance_account_stop_preserves_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/fapi/v2/account" => r#"{"totalMarginBalance":"1500","availableBalance":"1200","positions":[{"symbol":"BTCUSDT","positionAmt":"1","entryPrice":"100","leverage":"6","positionSide":"BOTH"}]}"#.into(),
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
    );
}
#[tokio::test]
async fn hyperliquid_account_stop_preserves_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.json()["type"].as_str().unwrap() {
        "clearinghouseState" => r#"{"marginSummary":{"accountValue":"1500","totalMarginUsed":"300"},"withdrawable":"1200","assetPositions":[{"position":{"coin":"BTC","szi":"1","entryPx":"100","leverage":{"type":"cross","value":20}}}]}"#.into(),
        "frontendOpenOrders" => format!(r#"[{{"coin":"BTC","side":"A","sz":"1","origSz":"1","oid":77,"reduceOnly":true,"isTrigger":true,"orderType":"Stop Market","triggerPx":{PRICE}}}]"#),
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
    );
}
#[tokio::test]
async fn lighter_account_stop_preserves_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/api/v1/account" => r#"{"code":200,"accounts":[{"collateral":"1500","available_balance":"1200","positions":[{"market_id":0,"symbol":"BTC","sign":1,"position":"1","avg_entry_price":"100","initial_margin_fraction":"0.05"}]}]}"#.into(),
        "/api/v1/accountActiveOrders" => format!(r#"{{"code":200,"orders":[{{"market_index":0,"reduce_only":true,"type":"stop-loss","is_ask":true,"trigger_price":{PRICE}}}]}}"#),
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
    );
}
#[tokio::test]
async fn mexc_account_stop_preserves_bare_native_decimal() {
    let server = TestServer::start(|request,_| (200, match request.path.as_str() {
        "/api/v1/contract/detail" => r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into(),
        "/api/v1/private/account/assets" => r#"{"success":true,"code":0,"data":[{"currency":"USDT","equity":1500,"availableBalance":1200}]}"#.into(),
        "/api/v1/private/position/open_positions" => r#"{"success":true,"code":0,"data":[{"positionId":"7","symbol":"BTC_USDT","holdVol":5,"positionType":1,"holdAvgPrice":100,"leverage":2,"state":1}]}"#.into(),
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
    );
}
