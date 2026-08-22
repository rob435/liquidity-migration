//! Choosing a venue by name, and proving the choice changes nothing.
//!
//! `Venue` wraps an adapter so the engine can pick one from config. A wrapper
//! that quietly altered a request would be worse than no wrapper at all, so
//! these tests drive every method through it against the same local server
//! the adapter's own tests use. No network, no credentials.

mod support;

use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId, VenueGateway,
};
use engine_venue::{BybitGateway, Venue, VenueRealm, BYBIT_DEMO};
use support::TestServer;

fn adapter(server: &TestServer) -> BybitGateway {
    BybitGateway::for_test(
        &server.base_url(),
        VenueRealm::Demo,
        VenueRealm::Demo.credentials_for_test("demoKey000000000001", "demoSecret00000000000000000001"),
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

/// Answers every endpoint the seven methods reach.
fn answer(path: &str) -> (u16, String) {
    match path {
        "/v5/order/create" => ok(r#"{"orderId":"ord-1","orderLinkId":"eng-1"}"#),
        "/v5/account/wallet-balance" => ok(r#"{"list":[{"accountType":"UNIFIED",
            "totalEquity":"1500.25","totalAvailableBalance":"1200.5","coin":[]}]}"#),
        "/v5/position/list" => ok(r#"{"list":[
            {"symbol":"BTCUSDT","side":"Buy","size":"0.01","avgPrice":"95000",
             "stopLoss":"93000","positionIdx":0}
           ],"nextPageCursor":""}"#),
        "/v5/market/instruments-info" => ok(r#"{"category":"linear","list":[
            {"symbol":"BTCUSDT","priceFilter":{"tickSize":"0.10"},
             "lotSizeFilter":{"qtyStep":"0.001","minOrderQty":"0.001","minNotionalValue":"5"}}
           ],"nextPageCursor":""}"#),
        _ => ok("{}"),
    }
}

#[tokio::test]
async fn every_method_reaches_the_adapter_behind_the_name() {
    // One stub arm on the enum would be an order that silently never left.
    let server = TestServer::start(|request, _| answer(&request.path)).await;
    let mut venue = Venue::Bybit(adapter(&server));

    assert!(venue.caps().native_position_stop, "caps came from the adapter");
    venue.send_order(&market_order()).await.unwrap();
    venue.cancel_order(SymbolId(0), "eng-1").await.unwrap();
    venue
        .amend_order(
            SymbolId(0),
            "eng-1",
            AmendSpec { px: Some(94_000.5), qty: None },
        )
        .await
        .unwrap();
    venue.set_stop(SymbolId(1), 2950.0).await.unwrap();
    let view = venue.account_view().await.unwrap();
    let rules = venue.instrument_rules().await.unwrap();

    assert_eq!(view.equity_usdt, 1500.25);
    assert_eq!(view.positions.len(), 1);
    assert_eq!(rules.len(), 1);
    for path in [
        "/v5/order/create",
        "/v5/order/cancel",
        "/v5/order/amend",
        "/v5/position/trading-stop",
        "/v5/account/wallet-balance",
        "/v5/position/list",
        "/v5/market/instruments-info",
    ] {
        assert_eq!(server.to_path(path).len(), 1, "nothing was sent to {path}");
    }
}

#[tokio::test]
async fn the_chosen_venue_puts_the_same_bytes_on_the_wire_as_the_adapter() {
    let server = TestServer::start(|request, _| answer(&request.path)).await;
    let mut direct = adapter(&server);
    let mut chosen = Venue::Bybit(adapter(&server));

    assert_eq!(chosen.caps(), direct.caps());
    direct.send_order(&market_order()).await.unwrap();
    chosen.send_order(&market_order()).await.unwrap();

    let sent = server.to_path("/v5/order/create");
    assert_eq!(sent.len(), 2);
    assert_eq!(sent[0].method, sent[1].method);
    assert_eq!(
        sent[0].body, sent[1].body,
        "choosing the venue by name changed the order"
    );
}

#[tokio::test]
async fn a_built_venue_knows_the_name_it_was_chosen_by() {
    let server = TestServer::start(|request, _| answer(&request.path)).await;
    assert_eq!(Venue::Bybit(adapter(&server)).name().as_str(), BYBIT_DEMO);
}
