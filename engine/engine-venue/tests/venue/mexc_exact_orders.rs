use crate::support::TestServer;
use engine_types::numeric::Exact;
use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
use engine_types::{
    OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce, VenueGateway,
};
use engine_venue::{MexcGateway, MexcRealm, RealmCredentials};

#[tokio::test]
async fn exact_contracts_and_prices_reach_wire_and_fractional_contracts_are_refused() {
    let server = TestServer::start(|request, _| {
        if request.path == "/api/v1/contract/detail" {
            (200, r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.0000000000001,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into())
        } else { (200, r#"{"success":true,"code":0,"data":"7"}"#.into()) }
    }).await;
    let mut gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    let mut request = OrderRequest {
        exact_terms: None,
        sleeve_effect: None,
        client_order_id: "id".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        kind: OrderKind::Limit {
            px: 1.0,
            tif: TimeInForce::Gtc,
        },
        stop: Some(StopSpec { trigger_px: 0.1 }),
        reduce_only: false,
        close_position: false,
    };
    let terms = ExactOrderTerms {
        quantity: Exact::parse_decimal("0.0003").unwrap(),
        limit_price: Some(Exact::parse_decimal("0.1234567890123").unwrap()),
        stop_trigger_price: Some(Exact::parse_decimal("0.1134567890123").unwrap()),
        physical_stop_trigger_price: Some(Exact::parse_decimal("0.1134567890123").unwrap()),
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    };
    terms.apply_projection(&mut request).unwrap();
    gw.send_order(&request).await.unwrap();
    let sent = server.only("/api/v1/private/order/create").json();
    assert_eq!(sent["vol"], 3);
    assert_eq!(sent["price"], "0.1234567890123");
    assert_eq!(sent["stopLossPrice"], "0.1134567890123");
    let mut fractional = terms.clone();
    fractional.quantity = Exact::parse_decimal("0.00035").unwrap();
    fractional.apply_projection(&mut request).unwrap();
    assert!(gw.send_order(&request).await.is_err());
    let mut oversized = terms;
    oversized.quantity = Exact::parse_decimal("0.0101").unwrap();
    oversized.apply_projection(&mut request).unwrap();
    assert!(gw.send_order(&request).await.is_err());
    assert_eq!(server.to_path("/api/v1/private/order/create").len(), 1);
}

#[tokio::test]
async fn independent_catalog_installs_contract_multipliers_before_a_mutation_without_another_read()
{
    let server = TestServer::start(|request, _| if request.path == "/api/v1/contract/detail" {
        (200, r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into())
    } else { (200, r#"{"success":true,"code":0,"data":"7"}"#.into()) }).await;
    let mut gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    let catalog = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    assert_eq!(catalog.rules.len(), 1);
    assert_eq!(catalog.specs.len(), 1);
    gw.install_instrument_catalog(&catalog).unwrap();
    let request = OrderRequest {
        client_order_id: "id".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.0003,
        kind: OrderKind::Limit {
            px: 100.0,
            tif: TimeInForce::Gtc,
        },
        stop: None,
        reduce_only: false,
        close_position: false,
        sleeve_effect: None,
        exact_terms: None,
    };
    gw.send_order(&request).await.unwrap();
    assert_eq!(server.to_path("/api/v1/contract/detail").len(), 1);
    assert_eq!(server.only("/api/v1/private/order/create").json()["vol"], 3);
}

#[tokio::test]
async fn independent_account_recovery_retains_contract_units_and_requested_ids() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into()),
        "/api/v1/private/account/assets" => (200, r#"{"success":true,"code":0,"data":[{"currency":"USDT","equity":1500.25,"availableBalance":1200.5}]}"#.into()),
        "/api/v1/private/position/open_positions" => (200, r#"{"success":true,"code":0,"data":[{"positionId":"7","symbol":"BTC_USDT","holdVol":5,"positionType":1,"holdAvgPrice":109777.5,"leverage":2,"state":1}]}"#.into()),
        "/api/v1/private/stoporder/open_orders" => (200, r#"{"success":true,"code":0,"data":[{"positionId":"7","orderId":"0","stopLossPrice":101000}] }"#.into()),
        other => panic!("unexpected read path {other}"),
    }).await;
    let mut gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    let catalog = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    gw.install_instrument_catalog(&catalog).unwrap();
    let client = gw
        .account_recovery_client()
        .expect("independent account recovery client");
    let view = client
        .account_view(&["ETHUSDT".into(), "BTCUSDT".into()])
        .await
        .unwrap();
    assert_eq!((view.equity_usdt, view.available_usdt), (1500.25, 1200.5));
    assert_eq!(view.positions[0].symbol, SymbolId(1));
    assert_eq!(view.positions[0].qty, 0.0005);
    assert_eq!(view.positions[0].stop_px, 101000.0);
    assert!(view.positions[0].stop_attached);
    assert_eq!(
        server.to_path("/api/v1/contract/detail").len(),
        1,
        "retained catalog must not depend on another metadata request"
    );
}

#[tokio::test]
async fn recovery_catalog_install_refreshes_native_units_without_metadata_reads() {
    let server = TestServer::start(|request, prior| match request.path.as_str() {
        "/api/v1/contract/detail" if prior == 0 => (200, r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into()),
        "/api/v1/contract/detail" => (503, "metadata unavailable".into()),
        "/api/v1/private/account/assets" => (200, r#"{"success":true,"code":0,"data":[{"currency":"USDT","equity":1500.25,"availableBalance":1200.5}]}"#.into()),
        "/api/v1/private/position/open_positions" => (200, r#"{"success":true,"code":0,"data":[{"positionId":"7","symbol":"BTC_USDT","holdVol":5,"positionType":1,"holdAvgPrice":109777.5,"leverage":2,"state":1}]}"#.into()),
        "/api/v1/private/stoporder/open_orders" => (200, r#"{"success":true,"code":0,"data":[{"positionId":"7","orderId":"0","stopLossPrice":101000}]}"#.into()),
        "/api/v1/private/order/list/order_deals/v3" => (200, r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","side":1,"id":"9","externalOid":"engine-entry","vol":3,"price":109777.5,"fee":0.01,"taker":true,"timestamp":150}]}"#.into()),
        other => panic!("unexpected read path {other}"),
    }).await;
    let gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    let client = gw.account_recovery_client().unwrap();
    let catalog = gw
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    client.install_instrument_catalog(&catalog).unwrap();
    let names = vec!["ETHUSDT".into(), "BTCUSDT".into()];
    let view = client.account_view(&names).await.unwrap();
    assert_eq!(
        (view.positions[0].symbol, view.positions[0].qty),
        (SymbolId(1), 0.0005)
    );
    assert!(view.positions[0].stop_attached);
    let fills = client
        .executions(&["BTCUSDT".into()], 100, 200)
        .await
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(fills.len(), 1);
    assert_eq!(
        (fills[0].symbol.as_str(), fills[0].qty),
        ("BTCUSDT", 0.0003)
    );
    assert_eq!(
        fills[0].amounts.as_ref().unwrap().quantity.value,
        Exact::parse_decimal("0.0003").unwrap()
    );
    assert_eq!(server.to_path("/api/v1/contract/detail").len(), 1);
    let other = TestServer::start(|_, _| panic!("catalog installation must not issue HTTP")).await;
    let other_gateway = MexcGateway::for_test(
        &other.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    assert!(
        matches!(other_gateway.account_recovery_client().unwrap().install_instrument_catalog(&catalog), Err(engine_types::VenueError::BadRequest(reason)) if reason == "recovery catalog belongs to another endpoint")
    );
    assert!(other.requests().is_empty());
}
