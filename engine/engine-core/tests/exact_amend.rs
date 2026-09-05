use engine_core::inflight::LedgerOfOrders;
use engine_types::{
    AmendSpec, OrderKind, OrderRequest, Side, StrategyId, SymbolId, TimeInForce, WalRecord,
};
fn request(id: &str, qty: f64) -> OrderRequest {
    OrderRequest {
        client_order_id: id.into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        exact_terms: None,
        sleeve_effect: None,
        close_position: false,
    }
}
#[test]
fn uncertain_amend_retains_the_effective_typed_request_across_serialization() {
    use engine_types::numeric::Exact;
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    let mut original = request("typed", 1.0);
    original.kind = OrderKind::Limit {
        px: 100.0,
        tif: TimeInForce::Gtc,
    };
    original.stop = None;
    original.exact_terms = Some(Box::new(ExactOrderTerms {
        quantity: Exact::one(),
        limit_price: Some(Exact::parse_decimal("100").unwrap()),
        stop_trigger_price: None,
        physical_stop_trigger_price: None,
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    }));
    let records = [
        WalRecord::OrderSent {
            dispatch: None,
            request: original,
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        WalRecord::AmendSent {
            symbol: SymbolId(0),
            client_order_id: "typed".into(),
            spec: AmendSpec {
                px: Some(1000.0),
                qty: None,
                exact_terms: None,
            },
            wire_ns: 2,
        },
    ];
    let bytes = serde_json::to_vec(&records).unwrap();
    let records = serde_json::from_slice::<Vec<WalRecord>>(&bytes).unwrap();
    let ledger = LedgerOfOrders::try_from_records(&records).unwrap();
    let row = &ledger.orders["typed"];
    assert!(
        row.request
            .exact_terms
            .as_ref()
            .unwrap()
            .validate_projection(&row.request)
            .is_ok(),
        "uncertain amend changed the effective request without changing its exact terms"
    );
    assert!(matches!(
        row.request.kind,
        OrderKind::Limit { px: 100.0, .. }
    ));
    assert_eq!(
        (row.reservation_low_px, row.reservation_high_px),
        (100.0, 1000.0)
    );
}

fn typed_sent() -> WalRecord {
    use engine_types::numeric::Exact;
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    let mut order = request("typed", 1.0);
    order.kind = OrderKind::Limit {
        px: 100.0,
        tif: TimeInForce::Gtc,
    };
    order.exact_terms = Some(Box::new(ExactOrderTerms {
        quantity: Exact::one(),
        limit_price: Some(Exact::from_i64(100)),
        stop_trigger_price: None,
        physical_stop_trigger_price: None,
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    }));
    WalRecord::OrderSent {
        dispatch: None,
        request: order,
        wire_ns: 1,
        arrival_mid: 100.0,
    }
}
fn resolved(px: f64, exact: serde_json::Value) -> WalRecord {
    serde_json::from_value(serde_json::json!({"kind":"amend_resolved","client_order_id":"typed","effective_px":px,"exact_effective_px":exact})).unwrap()
}
#[test]
fn actual_amend_price_survives_json_replay_without_a_binary64_round_trip() {
    let number =
        engine_types::numeric::ExactNumber::venue_decimal("100.123456789012345678901").unwrap();
    let resolution = resolved(
        number.value.to_f64().unwrap(),
        serde_json::to_value(&number).unwrap(),
    );
    let records = serde_json::from_slice::<Vec<WalRecord>>(
        &serde_json::to_vec(&[typed_sent(), resolution]).unwrap(),
    )
    .unwrap();
    let ledger = LedgerOfOrders::try_from_records(&records).unwrap();
    let row = &ledger.orders["typed"];
    assert_eq!(
        row.request.exact_terms.as_ref().unwrap().limit_price,
        Some(number.value)
    );
    row.request
        .exact_terms
        .as_ref()
        .unwrap()
        .validate_projection(&row.request)
        .unwrap();
}
#[test]
fn contradictory_effective_price_is_rejected_before_mutating_the_order() {
    let number = engine_types::numeric::ExactNumber::venue_decimal("101").unwrap();
    let resolution = resolved(102.0, serde_json::to_value(number).unwrap());
    let mut ledger = LedgerOfOrders::try_from_records(&[typed_sent()]).unwrap();
    assert!(
        ledger.try_apply(&resolution).is_err(),
        "contradictory effective price was accepted"
    );
    assert!(matches!(
        ledger.orders["typed"].request.kind,
        OrderKind::Limit { px: 100.0, .. }
    ));
}
#[test]
fn legacy_resolution_keeps_binary64_value_as_explicit_migration_input() {
    let resolution = resolved(100.1, serde_json::Value::Null);
    let ledger = LedgerOfOrders::try_from_records(&[typed_sent(), resolution]).unwrap();
    assert_eq!(
        ledger.orders["typed"]
            .request
            .exact_terms
            .as_ref()
            .unwrap()
            .limit_price,
        Some(engine_types::numeric::Exact::from_legacy_f64(100.1).unwrap())
    );
}
