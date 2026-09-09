//! Audit regressions use synthetic venue replies and local sockets only.
use crate::support::TestServer;
use engine_types::{OrderKind, OrderRequest, Side, StrategyId, SymbolId, VenueError, VenueGateway};
use engine_venue::{MexcGateway, MexcRealm, RealmCredentials};
use serde_json::json;

fn gateway(server: &TestServer) -> MexcGateway {
    MexcGateway::for_test(
        &server.base_url(),
        MexcRealm::Mainnet,
        MexcRealm::Mainnet.credentials_for_test("audit-key", "audit-secret"),
        vec!["BTCUSDT".into()],
    )
}

fn metadata(api_allowed: bool) -> String {
    json!({"success":true,"code":0,"data":[{
        "symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
        "contractSize":0.001,"priceUnit":0.1,"volUnit":1,"minVol":1,"maxVol":100,
        "minLeverage":1,"maxLeverage":100,"apiAllowed":api_allowed,"state":0,
        "positionOpenType":3,"stopOnlyFair":false,"futureType":1
    }]})
    .to_string()
}

fn order(reduce_only: bool) -> OrderRequest {
    OrderRequest {
        client_order_id: "audit-order".into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 0.005,
        kind: OrderKind::Market,
        stop: None,
        reduce_only,
        close_position: false,
        exact_terms: None,
        sleeve_effect: None,
    }
}

#[tokio::test(start_paused = true)]
async fn removed_symbol_keeps_cleanup_and_lookup_identity_without_regaining_opening_permission() {
    let server = TestServer::start(|request, prior| match request.path.as_str() {
        "/api/v1/contract/detail" => {
            let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
            if prior > 0 {
                page["data"][0]["symbol"] = json!("ETH_USDT");
                page["data"][0]["baseCoin"] = json!("ETH");
            }
            (200, page.to_string())
        }
        "/api/v1/private/order/cancel_with_external" => (200, json!({"success":true,"code":0,
            "data":{"externalOid":"audit-order","errorCode":0}}).to_string()),
        "/api/v1/private/order/create" => (200, json!({"success":true,"code":0,"data":"7"}).to_string()),
        "/api/v1/private/order/external/BTC_USDT/audit-order" => (200, json!({"success":true,"code":0,
            "data":{"symbol":"BTC_USDT","externalOid":"audit-order","orderId":"7","state":3,"dealVol":5}}).to_string()),
        other => panic!("unexpected path {other}"),
    }).await;
    let mut gateway = gateway(&server);
    let lookup = gateway.order_lookup_client().unwrap();
    let client = gateway.instrument_catalog_client().unwrap();
    let old = client.fetch().await.unwrap();
    gateway.install_instrument_catalog(&old).unwrap();
    let fresh = client.fetch().await.unwrap();
    let retained = fresh.retain_previous(&old.checkpoint().unwrap()).unwrap();
    gateway.install_instrument_catalog(&retained).unwrap();
    gateway
        .cancel_order(SymbolId(0), "audit-order")
        .await
        .unwrap();
    gateway.send_order(&order(true)).await.unwrap();
    assert!(matches!(
        gateway.send_order(&order(false)).await,
        Err(VenueError::BadRequest(_))
    ));
    let result = lookup.lookup("BTCUSDT", "audit-order").await.unwrap();
    let engine_types::orders::OrderLookup::Terminal { row, .. } = result else {
        panic!("removed-symbol lookup lost the terminal order");
    };
    assert_eq!(
        row.filled_qty.value,
        engine_types::numeric::Exact::parse_decimal("0.005").unwrap()
    );
    assert_eq!(
        server.to_path("/api/v1/contract/detail").len(),
        2,
        "lookup discarded retained immutable identity and refetched the removed symbol"
    );
    assert_eq!(server.to_path("/api/v1/private/order/create").len(), 1);
    let checkpoint = retained.checkpoint().unwrap();
    let restored = gateway.restore_instrument_catalog(&checkpoint).unwrap();
    gateway.install_instrument_catalog(&restored).unwrap();
    assert!(matches!(
        gateway.send_order(&order(false)).await,
        Err(VenueError::BadRequest(_))
    ));
}

#[tokio::test(start_paused = true)]
async fn legacy_checkpoint_keeps_its_original_grid_but_cannot_authorize_new_exposure() {
    use engine_public::venues::mexc::contracts::Contracts;
    use engine_types::orders::{InstrumentCatalogCacheSnapshot, InstrumentCatalogCheckpoint};
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => {
            let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
            page["data"][0]["volUnit"] = json!(5);
            (200, page.to_string())
        }
        other => panic!("unexpected mutation {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
    page["data"][0]["volUnit"] = json!(5);
    let raw = page.to_string();
    let legacy = Contracts::parse_checkpoint_v1(&raw).unwrap();
    let checkpoint = InstrumentCatalogCheckpoint {
        schema_version: 1,
        rules: legacy.rules(),
        specs: legacy.instrument_specs(),
        cache: InstrumentCatalogCacheSnapshot {
            kind: "mexc".into(),
            payload: serde_json::to_vec(&json!({"base":server.base_url(),"pages":[raw]})).unwrap(),
        },
    };
    let restored = gateway.restore_instrument_catalog(&checkpoint).unwrap();
    assert_eq!(
        restored.specs[0].1.qty_step,
        Some(engine_types::numeric::Exact::parse_decimal("0.001").unwrap())
    );
    assert_eq!(restored.checkpoint().unwrap(), checkpoint);
    gateway.install_instrument_catalog(&restored).unwrap();
    assert!(matches!(
        gateway.send_order(&order(false)).await,
        Err(VenueError::BadRequest(_))
    ));
    let fresh = gateway
        .instrument_catalog_client()
        .unwrap()
        .fetch()
        .await
        .unwrap();
    let upgraded = fresh.retain_previous(&checkpoint).unwrap();
    assert_eq!(
        upgraded.specs[0].1.qty_step,
        Some(engine_types::numeric::Exact::parse_decimal("0.005").unwrap())
    );
    assert_eq!(
        upgraded.checkpoint().unwrap().cache.kind,
        "mexc-execution-v2"
    );
}

#[tokio::test(start_paused = true)]
async fn malformed_cancel_then_terminal_fill_keeps_the_exact_authoritative_quantity() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, metadata(true)),
        "/api/v1/private/order/cancel_with_external" =>
            (200, json!({"success":true,"code":0,"data":{}}).to_string()),
        "/api/v1/private/order/external/BTC_USDT/audit-order" => (200, json!({"success":true,"code":0,
            "data":{"symbol":"BTC_USDT","externalOid":"audit-order","orderId":"7","state":3,"dealVol":5}}).to_string()),
        other => panic!("unexpected path {other}"),
    }).await;
    let mut gateway = gateway(&server);
    assert!(matches!(
        gateway.cancel_order(SymbolId(0), "audit-order").await,
        Err(VenueError::BadReply(_))
    ));
    for _ in 0..2 {
        let lookup = gateway
            .order_status(SymbolId(0), "audit-order")
            .await
            .unwrap();
        let engine_types::orders::OrderLookup::Terminal { status, row } = lookup else {
            panic!("not terminal");
        };
        assert_eq!(status, engine_types::orders::TerminalOrderStatus::Filled);
        assert_eq!(
            row.filled_qty.value,
            engine_types::numeric::Exact::parse_decimal("0.005").unwrap()
        );
    }
    assert_eq!(
        server
            .to_path("/api/v1/private/order/cancel_with_external")
            .len(),
        1
    );
    assert!(server.to_path("/api/v1/private/order/create").is_empty());
}

#[tokio::test(start_paused = true)]
async fn published_contract_step_is_enforced_for_legacy_and_exact_orders() {
    let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
    page["data"][0]["volUnit"] = json!(5);
    page["data"][0]["minVol"] = json!(5);
    let server = TestServer::start(move |request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, page.to_string()),
        "/api/v1/private/order/create" => {
            (200, json!({"success":true,"code":0,"data":"7"}).to_string())
        }
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    let mut request = order(false);
    request.qty = 0.006;
    assert!(matches!(
        gateway.send_order(&request).await,
        Err(VenueError::BadRequest(_))
    ));
    let terms = engine_types::order_terms::ExactOrderTerms {
        quantity: engine_types::numeric::Exact::parse_decimal("0.006").unwrap(),
        limit_price: None,
        stop_trigger_price: None,
        physical_stop_trigger_price: None,
        input_policy: engine_types::order_terms::OrderInputPolicy::StrategyShortestDecimal,
    };
    terms.apply_projection(&mut request).unwrap();
    assert!(matches!(
        gateway.send_order(&request).await,
        Err(VenueError::BadRequest(_))
    ));
    request.exact_terms = None;
    request.qty = 0.010;
    gateway.send_order(&request).await.unwrap();
    assert_eq!(
        server.only("/api/v1/private/order/create").json()["vol"],
        10
    );
}

#[tokio::test(start_paused = true)]
async fn lifecycle_margin_mode_and_unknown_execution_metadata_refuse_new_exposure() {
    for (field, value) in [
        ("state", json!(4)),
        ("positionOpenType", json!(1)),
        ("futureType", json!(2)),
        ("state", json!(null)),
        ("positionOpenType", json!(null)),
        ("volUnit", json!(null)),
        ("stopOnlyFair", json!(null)),
        ("futureType", json!(null)),
    ] {
        let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
        page["data"][0][field] = value;
        let server = TestServer::start(move |request, _| match request.path.as_str() {
            "/api/v1/contract/detail" => (200, page.to_string()),
            "/api/v1/private/order/create" => {
                (200, json!({"success":true,"code":0,"data":"7"}).to_string())
            }
            other => panic!("unexpected path {other}"),
        })
        .await;
        let mut gateway = gateway(&server);
        assert!(
            matches!(
                gateway.send_order(&order(false)).await,
                Err(VenueError::BadRequest(_))
            ),
            "unsupported {field} allowed new exposure"
        );
        assert!(server.to_path("/api/v1/private/order/create").is_empty());
    }
}

#[tokio::test(start_paused = true)]
async fn fair_only_contracts_never_send_last_price_protection() {
    let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
    page["data"][0]["stopOnlyFair"] = json!(true);
    let server = TestServer::start(move |request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, page.to_string()),
        "/api/v1/private/position/open_positions" => (
            200,
            json!({"success":true,"code":0,
            "data":[{"positionId":"17","symbol":"BTC_USDT","holdVol":5,"positionType":1}]})
            .to_string(),
        ),
        "/api/v1/private/stoporder/open_orders" => {
            (200, json!({"success":true,"code":0,"data":[]}).to_string())
        }
        "/api/v1/private/order/create" | "/api/v1/private/stoporder/place" => {
            (200, json!({"success":true,"code":0,"data":"7"}).to_string())
        }
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    let mut request = order(false);
    request.side = Side::Buy;
    request.stop = Some(engine_types::StopSpec { trigger_px: 90.0 });
    gateway.send_order(&request).await.unwrap();
    gateway.set_stop(SymbolId(0), 90.0).await.unwrap();
    assert_eq!(
        server.only("/api/v1/private/order/create").json()["lossTrend"],
        2
    );
    assert_eq!(
        server.only("/api/v1/private/stoporder/place").json()["lossTrend"],
        2
    );
}

#[tokio::test(start_paused = true)]
async fn disabled_opening_permission_does_not_disable_cancellation_reduction_or_stop_repair() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, metadata(false)),
        "/api/v1/private/order/cancel_with_external" => (
            200,
            json!({"success":true,"code":0,
            "data":{"externalOid":"audit-order","errorCode":0}})
            .to_string(),
        ),
        "/api/v1/private/position/open_positions" => (
            200,
            json!({"success":true,"code":0,
            "data":[{"positionId":"17","symbol":"BTC_USDT","holdVol":5,"positionType":1}]})
            .to_string(),
        ),
        "/api/v1/private/stoporder/open_orders" => {
            (200, json!({"success":true,"code":0,"data":[]}).to_string())
        }
        "/api/v1/private/order/create" | "/api/v1/private/stoporder/place" => {
            (200, json!({"success":true,"code":0,"data":"7"}).to_string())
        }
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    gateway
        .cancel_order(SymbolId(0), "audit-order")
        .await
        .unwrap();
    gateway.send_order(&order(true)).await.unwrap();
    gateway.set_stop(SymbolId(0), 90.0).await.unwrap();
    assert!(matches!(
        gateway.send_order(&order(false)).await,
        Err(VenueError::BadRequest(_))
    ));
    assert_eq!(server.to_path("/api/v1/private/order/create").len(), 1);
    assert_eq!(
        server.only("/api/v1/private/order/create").json()["reduceOnly"],
        true
    );
    assert_eq!(server.to_path("/api/v1/private/stoporder/place").len(), 1);
}

#[tokio::test(start_paused = true)]
async fn fractional_or_unrepresentable_leverage_is_refused_before_any_http() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, metadata(true)),
        _ => (200, json!({"success":true,"code":0,"data":[]}).to_string()),
    })
    .await;
    let mut gateway = gateway(&server);
    for leverage in [
        2.4,
        1.0000000000000002,
        0.0,
        -1.0,
        f64::NAN,
        f64::INFINITY,
        i64::MAX as f64,
    ] {
        assert!(
            matches!(
                gateway.set_leverage(SymbolId(0), leverage).await,
                Err(VenueError::BadRequest(_))
            ),
            "accepted leverage {leverage}"
        );
    }
    assert!(
        server.requests().is_empty(),
        "invalid leverage reached HTTP"
    );
}

#[tokio::test(start_paused = true)]
async fn cancellation_never_defaults_malformed_per_order_evidence_to_success() {
    let server = TestServer::start(|request, prior| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, metadata(true)),
        "/api/v1/private/order/cancel_with_external" => {
            let data = [
                json!({}),
                json!({"errorCode":"0"}),
                json!({"errorCode":null}),
                json!({"errorCode":false}),
                json!({"errorCode":{"code":0}}),
                json!([]),
                json!({"errorCode":0,"externalOid":"another-order"}),
            ][prior]
                .clone();
            (
                200,
                json!({"success":true,"code":0,"data":data}).to_string(),
            )
        }
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    for _ in 0..7 {
        assert!(matches!(
            gateway.cancel_order(SymbolId(0), "audit-order").await,
            Err(VenueError::BadReply(_))
        ));
    }
}

#[tokio::test(start_paused = true)]
async fn integer_leverage_is_sent_unchanged_to_both_sides_and_above_maximum_is_refused() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, metadata(true)),
        "/api/v1/private/position/open_positions" => {
            (200, json!({"success":true,"code":0,"data":[]}).to_string())
        }
        "/api/v1/private/position/change_leverage" => (
            200,
            json!({"success":true,"code":0,"data":true}).to_string(),
        ),
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    gateway.set_leverage(SymbolId(0), 2.0).await.unwrap();
    let requests = server.to_path("/api/v1/private/position/change_leverage");
    assert_eq!(requests.len(), 2);
    for (request, side) in requests.iter().zip([1, 2]) {
        assert_eq!(request.json()["leverage"], 2);
        assert_eq!(request.json()["positionType"], side);
    }
    assert!(matches!(
        gateway.set_leverage(SymbolId(0), 101.0).await,
        Err(VenueError::BadRequest(_))
    ));
    assert_eq!(
        server
            .to_path("/api/v1/private/position/change_leverage")
            .len(),
        2
    );
}

#[tokio::test(start_paused = true)]
async fn leverage_below_the_published_minimum_never_changes_either_side() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => {
            let mut page: serde_json::Value = serde_json::from_str(&metadata(true)).unwrap();
            page["data"][0]["minLeverage"] = json!(5);
            (200, page.to_string())
        }
        other => panic!("below-minimum leverage reached private HTTP: {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    assert!(matches!(
        gateway.set_leverage(SymbolId(0), 4.0).await,
        Err(VenueError::BadRequest(_))
    ));
    assert_eq!(server.requests().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn held_position_leverage_names_the_position_and_keeps_the_integer_unchanged() {
    let server = TestServer::start(|request, _| match request.path.as_str() {
        "/api/v1/contract/detail" => (200, metadata(true)),
        "/api/v1/private/position/open_positions" => (
            200,
            json!({
                "success":true,"code":0,"data":[{
                    "positionId":"17","symbol":"BTC_USDT","holdVol":5,"positionType":1
                }]
            })
            .to_string(),
        ),
        "/api/v1/private/position/change_leverage" => (
            200,
            json!({"success":true,"code":0,"data":true}).to_string(),
        ),
        other => panic!("unexpected path {other}"),
    })
    .await;
    let mut gateway = gateway(&server);
    gateway.set_leverage(SymbolId(0), 7.0).await.unwrap();
    assert_eq!(
        server
            .only("/api/v1/private/position/change_leverage")
            .json(),
        json!({"positionId":"17","leverage":7})
    );
}
