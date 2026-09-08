use crate::support::TestServer;
use engine_types::numeric::Exact;
use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
use engine_types::{
    OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce, VenueGateway,
};
use engine_venue::{MexcGateway, MexcInventoryProbe, MexcRealm, RealmCredentials};

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
async fn a_cancel_names_one_order_as_an_object_and_reads_the_orders_own_result() {
    // Observed live on 2026-09-08: `cancel_with_external` takes one object and
    // answers `{"data":{"externalOid":..,"errorCode":0,"errorMsg":"success"}}`;
    // a list body is refused with `600 Parameter error` and the order rests on.
    let server = TestServer::start(|request, _| {
        if request.path == "/api/v1/contract/detail" {
            return (200, r#"{"success":true,"code":0,"data":[{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#.into());
        }
        if request.path == "/api/v1/private/order/cancel_with_external" {
            let body: serde_json::Value = serde_json::from_str(&request.body).unwrap_or_default();
            let Some(object) = body.as_object() else {
                return (200, r#"{"success":false,"code":600,"message":"Parameter error"}"#.into());
            };
            if object.get("externalOid").and_then(|v| v.as_str()) == Some("gone") {
                return (200, r#"{"success":true,"code":0,"data":{"externalOid":"gone","errorCode":2005,"errorMsg":"order not exist"}}"#.into());
            }
            return (200, r#"{"success":true,"code":0,"data":{"externalOid":"lmcan-1","errorCode":0,"errorMsg":"success"}}"#.into());
        }
        (200, r#"{"success":true,"code":0,"data":"7"}"#.into())
    })
    .await;
    let mut gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );
    gw.cancel_order(SymbolId(0), "lmcan-1").await.unwrap();
    let sent = server.to_path("/api/v1/private/order/cancel_with_external");
    assert_eq!(sent.len(), 1);
    let body = sent[0].json();
    assert!(body.is_object(), "cancel body was {body}");
    assert_eq!(body["symbol"], "BTC_USDT");
    assert_eq!(body["externalOid"], "lmcan-1");

    // A success envelope around a per-order refusal is still a refusal.
    let err = gw.cancel_order(SymbolId(0), "gone").await.unwrap_err();
    assert!(
        matches!(err, engine_types::VenueError::Rejected { code: 2005, .. }),
        "got {err:?}"
    );
}

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

#[tokio::test(start_paused = true)]
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

const CONTRACT_DETAIL: &str = r#"{"success":true,"code":0,"data":[
    {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
     "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":100,"apiAllowed":true},
    {"symbol":"ETH_USDT","baseCoin":"ETH","quoteCoin":"USDT","settleCoin":"USDT",
     "contractSize":0.01,"priceUnit":0.01,"minVol":1,"maxVol":100,"apiAllowed":true}]}"#;

#[tokio::test(start_paused = true)]
async fn the_public_ping_is_the_clock_a_history_window_is_bounded_by() {
    let server = TestServer::start(|request, seen| match (request.path.as_str(), seen) {
        ("/api/v1/contract/ping", 0) => (
            200,
            r#"{"success":true,"code":0,"data":1787492334852}"#.into(),
        ),
        ("/api/v1/contract/ping", _) => (200, r#"{"success":true,"code":0,"data":0}"#.into()),
        _ => panic!(
            "the clock read is public and touches nothing else: {}",
            request.path
        ),
    })
    .await;
    let gw = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
        vec!["BTCUSDT".into()],
    );

    assert_eq!(gw.venue_time_ms().await.unwrap(), 1787492334852);
    // A clock the venue did not state is never a zero the caller can subtract.
    assert!(gw.venue_time_ms().await.is_err());
    let sent = server.to_path("/api/v1/contract/ping");
    assert_eq!(sent.len(), 2);
    assert_eq!(sent[0].method, "GET");
    assert_eq!(sent[0].header("request-key"), None, "the ping is unsigned");
}

#[tokio::test(start_paused = true)]
async fn the_inventory_probe_names_the_account_by_its_key_and_never_signs_a_mutation() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/private/account/assets" => (
            200,
            r#"{"success":true,"code":0,"data":[{"currency":"USDT","equity":32.8,"availableBalance":32.8}]}"#.into(),
        ),
        other => panic!("identity must read one endpoint, not {other}"),
    })
    .await;
    let creds = MexcRealm::Mainnet.credentials_for_test("theKey", "secret");
    let mut probe = MexcInventoryProbe::for_test(&server.base_url(), MexcRealm::Mainnet, creds);
    let who = probe.account_identity().await.unwrap();

    assert_eq!(who.venue, "mexc");
    assert_eq!(who.realm, "mexc_mainnet");
    // MEXC publishes no account number, so the key names the account. The
    // probe and the gateway must derive the same one from the same key.
    assert!(who.user_id.starts_with("key-"), "{}", who.user_id);
    assert_eq!(who.user_id.len(), "key-".len() + 16);
    let mut gateway = MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("theKey", "othersecret"),
        vec![],
    );
    assert_eq!(
        VenueGateway::account_identity(&mut gateway).await.unwrap(),
        who
    );

    let mut other = MexcInventoryProbe::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("anotherKey", "secret"),
    );
    assert_ne!(other.account_identity().await.unwrap().user_id, who.user_id);
}

#[tokio::test(start_paused = true)]
async fn the_inventory_probe_scans_balances_positions_orders_and_position_stops() {
    let server = TestServer::start(|request, _| {
        let body = match request.path.as_str() {
            "/api/v1/contract/detail" => CONTRACT_DETAIL.to_string(),
            "/api/v1/private/account/assets" => r#"{"success":true,"code":0,"data":[
                {"currency":"USDT","equity":32.8,"availableBalance":32.8},
                {"currency":"MX","equity":0.996,"availableBalance":0.996}]}"#
                .to_string(),
            "/api/v1/private/position/open_positions" => r#"{"success":true,"code":0,"data":[
                {"positionId":"42","symbol":"ETH_USDT","holdVol":5,"positionType":2,
                 "holdAvgPrice":3000.0,"leverage":2}]}"#
                .to_string(),
            "/api/v1/private/order/list/open_orders" => r#"{"success":true,"code":0,"data":[
                {"orderId":"7","symbol":"BTC_USDT","side":1,"vol":3,"dealVol":0,
                 "externalOid":"lmcan-1"}]}"#
                .to_string(),
            "/api/v1/private/stoporder/open_orders" => r#"{"success":true,"code":0,"data":[
                {"id":91,"orderId":"0","symbol":"ETH_USDT","positionId":"42","stopLossPrice":2900.0},
                {"id":92,"orderId":"7","symbol":"BTC_USDT","stopLossPrice":70000.0}]}"#
                .to_string(),
            other => panic!("the probe reached {other}"),
        };
        (200, body)
    })
    .await;
    let mut probe = MexcInventoryProbe::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("key", "secret"),
    );

    let inventory = probe.account_inventory().await.unwrap();

    assert!(inventory.observed_ms > 0);
    assert!(
        inventory.scope.contains("MEXC futures"),
        "{}",
        inventory.scope
    );
    let positions: Vec<_> = inventory
        .positions
        .iter()
        .map(|p| (p.product.as_str(), p.symbol.as_str(), p.side, p.qty))
        .collect();
    assert_eq!(
        positions,
        vec![
            ("linear", "ETHUSDT", Side::Sell, 0.05),
            ("wallet_asset", "MX", Side::Buy, 0.996),
        ],
        "settle cash is not exposure; every other balance is"
    );
    let orders: Vec<_> = inventory
        .open_orders
        .iter()
        .map(|o| {
            (
                o.product.as_str(),
                o.symbol.as_str(),
                o.client_order_id.as_str(),
            )
        })
        .collect();
    assert_eq!(
        orders,
        vec![
            ("linear", "BTCUSDT", "lmcan-1"),
            ("position_stop", "ETHUSDT", "stoporder-91"),
        ],
        "the stop bound to order 7 is that order, counted once"
    );
    // Reads only: nothing the probe can call is a POST.
    assert!(
        server.requests().iter().all(|sent| sent.method == "GET"),
        "the inventory probe issued a mutation"
    );
}
