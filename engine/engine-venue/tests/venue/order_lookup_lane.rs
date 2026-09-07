//! Independent lookup clients retain the same identity and failure contracts as direct reads.
use crate::support::TestServer;
use engine_types::numeric::Exact;
#[cfg(feature = "binance")]
use engine_types::numeric::NumericProvenance;
use engine_types::orders::{OrderLookup, TerminalOrderStatus};
use engine_types::VenueGateway;
#[cfg(feature = "binance")]
use engine_venue::BinanceGateway;
#[cfg(feature = "binance")]
use engine_venue::BinanceRealm;
#[cfg(feature = "bybit")]
use engine_venue::BybitGateway;
#[cfg(feature = "hyperliquid")]
use engine_venue::HyperliquidGateway;
#[cfg(feature = "hyperliquid")]
use engine_venue::HyperliquidRealm;
#[cfg(feature = "lighter")]
use engine_venue::LighterGateway;
#[cfg(feature = "lighter")]
use engine_venue::LighterRealm;
#[cfg(feature = "mexc")]
use engine_venue::MexcGateway;
#[cfg(feature = "mexc")]
use engine_venue::MexcRealm;
use engine_venue::RealmCredentials;
#[cfg(feature = "bybit")]
use engine_venue::VenueRealm;

const ID: &str = "eng-1700000000000-1";
#[cfg(feature = "hyperliquid")]
const HL_KEY: &str = "0x0123456789012345678901234567890123456789012345678901234567890123";
#[cfg(feature = "hyperliquid")]
const HL_ACCOUNT: &str = "0x0000000000000000000000000000000000000001";
#[cfg(feature = "lighter")]
const LIGHTER_KEY: &str =
    "0101010101010101010101010101010101010101010101010101010101010101010101010101010f";
#[cfg(feature = "mexc")]
const MEXC_DETAIL: &str = r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,"apiAllowed":true}]}"#;
#[cfg(feature = "lighter")]
const LIGHTER_MARKETS: &str = r#"{"code":200,"order_book_details":[{"symbol":"BTC","market_id":0,"status":"active","supported_size_decimals":5,"supported_price_decimals":1,"min_base_amount":"0.0001","min_quote_amount":"10"}]}"#;
fn symbols() -> Vec<String> {
    vec!["BTCUSDT".into()]
}
fn cancelled(lookup: OrderLookup, qty: &str) {
    let OrderLookup::Terminal {
        status: TerminalOrderStatus::Cancelled,
        row,
    } = lookup
    else {
        panic!("cancelled lookup was lost: {lookup:?}")
    };
    assert_eq!(row.symbol, "BTCUSDT");
    assert_eq!(row.client_order_id, ID);
    assert_eq!(row.filled_qty.value, Exact::parse_decimal(qty).unwrap());
}

#[cfg(feature = "bybit")]
#[tokio::test(start_paused = true)]
async fn bybit_lookup_falls_back_to_history_and_empty_history_remains_unknown() {
    let server=TestServer::start(|request,count|{
        assert_eq!(request.method,"GET");
        assert!(request.query.contains(&format!("orderLinkId={ID}")));
        assert!(request.header("X-BAPI-SIGN").is_some());
        let list=if request.path=="/v5/order/history" && count==0 {format!(r#"[{{"symbol":"BTCUSDT","orderLinkId":"{ID}","orderId":"7","orderStatus":"Cancelled","cumExecQty":0.12345678901234567890123456789}}]"#)}else{"[]".into()};
        (200,format!(r#"{{"retCode":0,"retMsg":"OK","result":{{"category":"linear","list":{list},"nextPageCursor":""}}}}"#))
    }).await;
    let gw = BybitGateway::for_test(
        &server.base_url(),
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test("key", "secret"),
        symbols(),
    );
    cancelled(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        "0.12345678901234567890123456789",
    );
    assert!(matches!(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        OrderLookup::Unknown { .. }
    ));
    assert_eq!(
        server
            .requests()
            .iter()
            .map(|r| r.path.as_str())
            .collect::<Vec<_>>(),
        vec![
            "/v5/order/realtime",
            "/v5/order/history",
            "/v5/order/realtime",
            "/v5/order/history"
        ]
    );
}

#[cfg(feature = "binance")]
#[tokio::test(start_paused = true)]
async fn binance_absence_is_not_never_accepted_and_503_does_not_become_absence() {
    let server=TestServer::start(|request,count|{
        assert_eq!(request.method,"GET");assert_eq!(request.path,"/fapi/v1/order");
        assert!(request.query.contains(&format!("origClientOrderId={ID}")));
        assert!(request.query.contains("signature="));
        match count {
            0=>(200,format!(r#"{{"symbol":"BTCUSDT","clientOrderId":"{ID}","orderId":7,"status":"CANCELED","executedQty":"0.000000000000000000123"}}"#)),
            1=>(400,r#"{"code":-2013,"msg":"Order does not exist."}"#.into()),
            _=>(503,r#"{"code":-1000,"msg":"overloaded"}"#.into()),
        }
    }).await;
    let gw = BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("key", "secret"),
        symbols(),
    );
    cancelled(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        "0.000000000000000000123",
    );
    assert!(matches!(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        OrderLookup::Unknown { .. }
    ));
    assert!(gw
        .order_lookup_client()
        .expect("independent read client")
        .lookup("BTCUSDT", ID)
        .await
        .is_err());
    assert_eq!(server.requests().len(), 3);
}

#[cfg(feature = "mexc")]
#[tokio::test(start_paused = true)]
async fn mexc_lookup_converts_contracts_exactly_and_does_not_guess_missing_orders() {
    let server=TestServer::start(|request,count|{
        if request.path=="/api/v1/contract/detail" {return (200,MEXC_DETAIL.into());}
        assert_eq!(request.path,format!("/api/v1/private/order/external/BTC_USDT/{ID}"));
        assert!(request.header("Signature").is_some());
        let data=if count==0 {format!(r#"{{"symbol":"BTC_USDT","externalOid":"{ID}","orderId":"7","state":4,"dealVol":3}}"#)}else{"null".into()};
        (200,format!(r#"{{"success":true,"code":0,"data":{data}}}"#))
    }).await;
    let gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        symbols(),
    );
    cancelled(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        "0.0003",
    );
    assert!(matches!(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        OrderLookup::Unknown { .. }
    ));
    assert!(server.requests().iter().all(|r| r.method == "GET"));
}

#[cfg(feature = "hyperliquid")]
#[tokio::test(start_paused = true)]
async fn hyperliquid_lookup_uses_packed_cloid_and_keeps_unknown_oid_unresolved() {
    let server=TestServer::start(|request,count|{
        assert_eq!(request.path,"/info");let body=request.json();
        assert_eq!(body["type"],"orderStatus");assert_eq!(body["user"],HL_ACCOUNT);
        let cloid=body["oid"].as_str().unwrap();assert_eq!(cloid.len(),34);
        if count>0 {return (200,r#"{"status":"unknownOid"}"#.into());}
        (200,format!(r#"{{"status":"order","order":{{"status":"canceled","order":{{"coin":"BTC","cloid":"{cloid}","oid":7,"origSz":"0.0007","sz":"0.0004"}}}}}}"#))
    }).await;
    let gw = HyperliquidGateway::for_test(
        &server.base_url(),
        HyperliquidRealm::Testnet,
        HyperliquidRealm::Testnet.credentials_for_test(HL_ACCOUNT, HL_KEY),
        symbols(),
    )
    .unwrap();
    cancelled(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        "0.0003",
    );
    assert!(matches!(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        OrderLookup::Unknown { .. }
    ));
    assert_eq!(server.requests().len(), 2);
}

#[cfg(feature = "lighter")]
#[tokio::test(start_paused = true)]
async fn lighter_lookup_checks_account_market_and_client_index_and_keeps_absence_unknown() {
    let server=TestServer::start(|request,count|{
        if request.path=="/api/v1/orderBookDetails" {return (200,LIGHTER_MARKETS.into());}
        assert_eq!(request.path,"/api/v1/accountOrders");assert!(request.header("Authorization").is_some());
        assert!(request.query.starts_with("account_index=42&client_order_indexes="));
        let index=request.query.split("client_order_indexes=").nth(1).unwrap();
        let orders=if count==0 {format!(r#"[{{"order_id":"7","client_order_index":{index},"market_index":0,"owner_account_index":42,"filled_base_amount":0.0001234567890123456789,"status":"canceled"}}]"#)}else{"[]".into()};
        (200,format!(r#"{{"code":200,"orders":{orders}}}"#))
    }).await;
    let gw = LighterGateway::for_test(
        &server.base_url(),
        LighterRealm::Testnet,
        LighterRealm::Testnet.credentials_for_test("42:3", LIGHTER_KEY),
        symbols(),
    )
    .unwrap();
    cancelled(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        "0.0001234567890123456789",
    );
    assert!(matches!(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        OrderLookup::Unknown { .. }
    ));
    assert!(server.requests().iter().all(|r| r.method == "GET"));
}

#[cfg(feature = "binance")]
#[tokio::test(start_paused = true)]
async fn unknown_status_wrong_identity_and_malformed_qty_cannot_claim_known_order() {
    let server=TestServer::start(|_,count|{
        let (symbol,status,qty)=match count {0=>("BTCUSDT","NEW_VENUE_STATE","0"),1=>("ETHUSDT","NEW","0"),_=>("BTCUSDT","FILLED","null")};
        (200,format!(r#"{{"symbol":"{symbol}","clientOrderId":"{ID}","orderId":7,"status":"{status}","executedQty":{qty}}}"#))
    }).await;
    let gw = BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("key", "secret"),
        symbols(),
    );
    assert!(matches!(
        gw.order_lookup_client()
            .expect("independent read client")
            .lookup("BTCUSDT", ID)
            .await
            .unwrap(),
        OrderLookup::Unknown { .. }
    ));
    assert!(gw
        .order_lookup_client()
        .expect("independent read client")
        .lookup("BTCUSDT", ID)
        .await
        .is_err());
    assert!(gw
        .order_lookup_client()
        .expect("independent read client")
        .lookup("BTCUSDT", ID)
        .await
        .is_err());
}

#[cfg(feature = "binance")]
#[tokio::test(start_paused = true)]
async fn working_order_with_zero_fills_is_distinct_from_absence_and_survives_escaped_identity() {
    let server=TestServer::start(|_,_|(200,r#"{"symbol":"BTCUSDT","clientOrderId":"eng-1700000000000-\u0031","orderId":"7","status":"PARTIALLY_FILLED","executedQty":"0"}"#.to_string())).await;
    let gw = BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("key", "secret"),
        symbols(),
    );
    let OrderLookup::Working(row) = gw
        .order_lookup_client()
        .expect("independent read client")
        .lookup("BTCUSDT", ID)
        .await
        .unwrap()
    else {
        panic!("working zero-fill order disappeared")
    };
    assert!(row.filled_qty.value.is_zero());
    assert_eq!(row.client_order_id, ID);
    assert_eq!(row.filled_qty.provenance, NumericProvenance::VenueDecimal);
}
