mod support;
use engine_types::numeric::Exact;
use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
use engine_types::{
    OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, TimeInForce, VenueGateway,
};
use engine_venue::{MexcGateway, MexcRealm, RealmCredentials};
use support::TestServer;

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
