mod support;
use engine_types::numeric::Exact;
use engine_types::order_terms::ExactStopTerms;
use engine_types::{Side, SymbolId, VenueGateway};
use engine_venue::{
    BinanceGateway, BinanceRealm, BybitGateway, HyperliquidGateway, HyperliquidRealm,
    LighterGateway, LighterRealm, MexcGateway, MexcRealm, RealmCredentials, VenueRealm,
};
use support::TestServer;
const HL_KEY: &str = "0x0123456789012345678901234567890123456789012345678901234567890123";
const HL_ACCOUNT: &str = "0x0000000000000000000000000000000000000001";
const LIGHTER_KEY: &str =
    "0101010101010101010101010101010101010101010101010101010101010101010101010101010f";
fn terms(trigger: &str, reference: &str) -> ExactStopTerms {
    ExactStopTerms {
        trigger_price: Exact::parse_decimal(trigger).unwrap(),
        position_side: Side::Buy,
        reference_price: Exact::parse_decimal(reference).unwrap(),
    }
}
async fn install(gw: &mut impl VenueGateway) {
    let catalog = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    let checkpoint = catalog.checkpoint().unwrap();
    let restored = serde_json::from_slice(&serde_json::to_vec(&checkpoint).unwrap()).unwrap();
    let cold = gw.restore_instrument_catalog(&restored).unwrap();
    assert_eq!(cold.rules, catalog.rules);
    assert_eq!(cold.specs, catalog.specs);
    let mut bad = checkpoint.clone();
    bad.schema_version = 2;
    assert!(gw.restore_instrument_catalog(&bad).is_err());
    let mut bad = checkpoint.clone();
    bad.cache.kind = "another-adapter".into();
    assert!(gw.restore_instrument_catalog(&bad).is_err());
    let mut bad = checkpoint.clone();
    let mut payload: serde_json::Value = serde_json::from_slice(&bad.cache.payload).unwrap();
    payload["base"] = "https://another-realm.invalid".into();
    bad.cache.payload = serde_json::to_vec(&payload).unwrap();
    assert!(gw.restore_instrument_catalog(&bad).is_err());
    let mut bad = checkpoint;
    bad.specs.clear();
    assert!(gw.restore_instrument_catalog(&bad).is_err());
    gw.install_instrument_catalog(&cold).unwrap();
}
fn ok(result: &str) -> (u16, String) {
    (
        200,
        format!(r#"{{"retCode":0,"retMsg":"OK","result":{result}}}"#),
    )
}

#[tokio::test]
async fn bybit_exact_full_stop_preserves_price_and_refuses_a_changed_native_side() {
    for changed in [false, true] {
        let server=TestServer::start(move|request,_|match request.path.as_str(){
            "/v5/market/instruments-info"=>ok(r#"{"category":"linear","list":[{"symbol":"BTCUSDT","status":"Trading","priceFilter":{"tickSize":"0.0000000000001"},"lotSizeFilter":{"qtyStep":"0.001","minOrderQty":"0.001","maxOrderQty":"100","maxMktOrderQty":"100"}}],"nextPageCursor":""}"#),
            "/v5/position/list"=>ok(&format!(r#"{{"list":[{{"symbol":"BTCUSDT","positionIdx":0,"side":"{}","size":"1"}}]}}"#,if changed{"Sell"}else{"Buy"})),
            "/v5/position/trading-stop"=>ok("{}"),
            path=>panic!("unexpected {path}"),
        }).await;
        let mut gw = BybitGateway::for_test(
            &server.base_url(),
            VenueRealm::Demo,
            VenueRealm::Demo.credentials_for_test("key", "secret"),
            vec!["BTCUSDT".into()],
        );
        install(&mut gw).await;
        let result = gw
            .set_stop_exact(SymbolId(0), &terms("1.1234567890123", "2"))
            .await;
        if changed {
            assert!(result.is_err());
            assert!(server.to_path("/v5/position/trading-stop").is_empty());
        } else {
            result.unwrap();
            let sent = server.only("/v5/position/trading-stop").json();
            assert_eq!(sent["stopLoss"], "1.1234567890123");
            assert_eq!(sent["tpslMode"], "Full");
        }
    }
}

#[tokio::test]
async fn binance_exact_native_stop_preserves_price_without_cancel_before_replacement() {
    let server=TestServer::start(|request,_|match request.path.as_str(){
        "/fapi/v1/exchangeInfo"=>(200,r#"{"symbols":[{"symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDT","marginAsset":"USDT","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.0000000000001"},{"filterType":"LOT_SIZE","minQty":"0.001","maxQty":"100","stepSize":"0.001"},{"filterType":"MARKET_LOT_SIZE","minQty":"0.001","maxQty":"100","stepSize":"0.001"},{"filterType":"MIN_NOTIONAL","notional":"5"}]}]}"#.into()),
        "/fapi/v2/account"=>(200,r#"{"positions":[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"1"}]}"#.into()),
        "/fapi/v1/openAlgoOrders"=>(200,"[]".into()),
        "/fapi/v1/algoOrder"=>{assert_eq!(request.method,"POST");let id=request.query.split('&').find_map(|p|p.strip_prefix("clientAlgoId=")).unwrap();(200,format!(r#"{{"clientAlgoId":"{id}","algoId":7,"algoStatus":"NEW"}}"#))},
        path=>panic!("unexpected {path}"),
    }).await;
    let mut gw = BinanceGateway::for_test(
        &server.base_url(),
        BinanceRealm::Testnet,
        BinanceRealm::Testnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    install(&mut gw).await;
    gw.set_stop_exact(SymbolId(0), &terms("1.1234567890123", "2"))
        .await
        .unwrap();
    let request = server.only("/fapi/v1/algoOrder");
    assert!(request.query.contains("triggerPrice=1.1234567890123"));
    assert!(request.query.contains("closePosition=true"));
}

#[tokio::test]
async fn mexc_exact_native_stop_preserves_position_id_and_decimal_price() {
    let server=TestServer::start(|request,_|{
        let data=match request.path.as_str(){
            "/api/v1/contract/detail"=>r#"[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.0000000000001,"minVol":1,"maxVol":100,"apiAllowed":true}]"#,
            "/api/v1/private/position/open_positions"=>r#"[{"symbol":"BTC_USDT","positionId":"7","positionType":1,"holdVol":1}]"#,
            "/api/v1/private/stoporder/open_orders"=>"[]",
            "/api/v1/private/stoporder/place"=>"true",
            path=>panic!("unexpected {path}"),
        };(200,format!(r#"{{"success":true,"code":0,"data":{data}}}"#))
    }).await;
    let mut gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    install(&mut gw).await;
    gw.set_stop_exact(SymbolId(0), &terms("1.1234567890123", "2"))
        .await
        .unwrap();
    let sent = server.only("/api/v1/private/stoporder/place").json();
    assert_eq!(sent["positionId"], "7");
    assert_eq!(sent["stopLossPrice"], "1.1234567890123");
    assert_eq!(sent["stopLossReverse"], 2);
}

#[tokio::test]
async fn hyperliquid_exact_stop_uses_actual_lexical_native_quantity() {
    let server=TestServer::start(|request,_|{
        if request.path=="/exchange" { return (200,r#"{"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":7}}]}}}"#.into()); }
        let reply=match request.json()["type"].as_str().unwrap(){
            "meta"=>r#"{"universe":[{"name":"BTC","szDecimals":5,"maxLeverage":40}]}"#,
            "clearinghouseState"=>r#"{"assetPositions":[{"position":{"coin":"BTC","szi":0.12345}}]}"#,
            "frontendOpenOrders"=>"[]",
            kind=>panic!("unexpected {kind}"),
        };(200,reply.into())
    }).await;
    let mut gw = HyperliquidGateway::for_test(
        &server.base_url(),
        HyperliquidRealm::Testnet,
        HyperliquidRealm::Testnet.credentials_for_test(HL_ACCOUNT, HL_KEY),
        vec!["BTCUSDT".into()],
    )
    .unwrap();
    install(&mut gw).await;
    gw.set_stop_exact(SymbolId(0), &terms("9.9", "10"))
        .await
        .unwrap();
    let sent = server.only("/exchange").json();
    let order = &sent["action"]["orders"][0];
    assert_eq!(order["s"], "0.12345");
    assert_eq!(order["t"]["trigger"]["triggerPx"], "9.9");
    assert_eq!(order["r"], true);
}

#[tokio::test]
async fn lighter_exact_stop_never_loses_a_native_lot_to_binary64_scaling() {
    let server=TestServer::start(|request,_|{
        let reply=match request.path.as_str(){
            "/api/v1/orderBookDetails"=>r#"{"code":200,"order_book_details":[{"symbol":"BTC","market_id":0,"status":"active","supported_size_decimals":8,"supported_price_decimals":1,"min_base_amount":"0.00000001","min_quote_amount":"10"}]}"#,
            "/api/v1/account"=>r#"{"code":200,"accounts":[{"account_index":42,"positions":[{"market_id":0,"symbol":"BTC","sign":1,"position":1.23456789}]}]}"#,
            "/api/v1/accountActiveOrders"=>r#"{"code":200,"orders":[]}"#,
            "/api/v1/nextNonce"=>r#"{"code":200,"nonce":7}"#,
            "/api/v1/sendTx"=>r#"{"code":200,"tx_hash":"0xabc"}"#,
            path=>panic!("unexpected {path}"),
        };(200,reply.into())
    }).await;
    let mut gw = LighterGateway::for_test(
        &server.base_url(),
        LighterRealm::Testnet,
        LighterRealm::Testnet.credentials_for_test("42:3", LIGHTER_KEY),
        vec!["BTCUSDT".into()],
    )
    .unwrap();
    install(&mut gw).await;
    gw.set_stop_exact(SymbolId(0), &terms("90.1", "100"))
        .await
        .unwrap();
    let sent = server.only("/api/v1/sendTx");
    let raw = sent
        .body
        .split('&')
        .find_map(|pair| pair.strip_prefix("tx_info="))
        .unwrap();
    let bytes = raw.as_bytes();
    let mut decoded = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            decoded.push(u8::from_str_radix(&raw[index + 1..index + 3], 16).unwrap());
            index += 3;
        } else {
            decoded.push(if bytes[index] == b'+' {
                b' '
            } else {
                bytes[index]
            });
            index += 1;
        }
    }
    let tx: serde_json::Value = serde_json::from_slice(&decoded).unwrap();
    assert_eq!(tx["BaseAmount"], 123456789);
    assert_eq!(tx["TriggerPrice"], 901);
    assert_eq!(tx["ReduceOnly"], 1);
}

#[tokio::test]
async fn retained_native_asset_survives_refresh_and_restart_at_its_original_index() {
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    let calls = Arc::new(AtomicUsize::new(0));
    let read = calls.clone();
    let server=TestServer::start(move |request,_|{
        if request.path=="/exchange" {return(200,r#"{"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":7}}]}}}"#.into());}
        let reply=match request.json()["type"].as_str().unwrap(){
            "meta"=>if read.fetch_add(1,Ordering::SeqCst)==0 {r#"{"universe":[{"name":"BTC","szDecimals":5,"maxLeverage":40},{"name":"ETH","szDecimals":4,"maxLeverage":20}]}"#}else{r#"{"universe":[{"name":"BTC","szDecimals":5,"maxLeverage":40}]}"#},
            "clearinghouseState"=>r#"{"assetPositions":[{"position":{"coin":"ETH","szi":"1.2345"}}]}"#,
            "frontendOpenOrders"=>"[]",
            kind=>panic!("unexpected {kind}"),
        };(200,reply.into())
    }).await;
    let make = || {
        HyperliquidGateway::for_test(
            &server.base_url(),
            HyperliquidRealm::Testnet,
            HyperliquidRealm::Testnet.credentials_for_test(HL_ACCOUNT, HL_KEY),
            vec!["ETHUSDT".into()],
        )
        .unwrap()
    };
    let gw = make();
    let old = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap()
        .checkpoint()
        .unwrap();
    let fresh = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    assert_eq!(fresh.specs.len(), 1);
    let merged = fresh.retain_previous(&old).unwrap();
    assert_eq!(
        merged.specs.len(),
        2,
        "metadata refresh discarded a retained position's native asset"
    );
    let bytes = serde_json::to_vec(&merged.checkpoint().unwrap()).unwrap();
    let mut restart = make();
    let restored = restart
        .restore_instrument_catalog(&serde_json::from_slice(&bytes).unwrap())
        .unwrap();
    restart.install_instrument_catalog(&restored).unwrap();
    restart
        .set_stop_exact(SymbolId(0), &terms("9.9", "10"))
        .await
        .unwrap();
    let sent = server.only("/exchange").json();
    assert_eq!(sent["action"]["orders"][0]["a"], 1);
    assert_eq!(sent["action"]["orders"][0]["s"], "1.2345");
    assert_eq!(
        calls.load(Ordering::SeqCst),
        2,
        "restart protection performed another metadata query"
    );
}
