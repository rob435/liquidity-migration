use super::*;
use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};
use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};

struct Idle;
impl engine_types::Strategy for Idle {
    fn name(&self) -> &str {
        "physical-owner"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
}
fn request(id: &str, side: Side, quantity: &str) -> OrderRequest {
    let mut request = OrderRequest {
        client_order_id: id.into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side,
        qty: 0.0,
        kind: OrderKind::Market,
        stop: Some(engine_types::StopSpec { trigger_px: 90.0 }),
        reduce_only: side == Side::Sell,
        close_position: false,
        exact_terms: None,
        sleeve_effect: None,
    };
    let stop = (side == Side::Buy).then(|| Exact::parse_decimal("90").unwrap());
    ExactOrderTerms {
        quantity: Exact::parse_decimal(quantity).unwrap(),
        limit_price: None,
        stop_trigger_price: stop.clone(),
        physical_stop_trigger_price: stop,
        input_policy: OrderInputPolicy::StrategyShortestDecimal,
    }
    .apply_projection(&mut request)
    .unwrap();
    request
}
fn update(id: &str, execution: &str, side: Side, quantity: &str) -> OrderUpdate {
    let quantity = ExactNumber::venue_decimal(quantity).unwrap();
    OrderUpdate::Fill {
        client_order_id: id.into(),
        exec_id: execution.into(),
        allocation: None,
        symbol: SymbolId(0),
        side,
        qty: quantity.value.to_f64().unwrap(),
        px: 100.0,
        fee: Some(0.0),
        amounts: Some(Box::new(ExecutionAmounts {
            settlement_asset: AssetId::Named("USDT".into()),
            quantity,
            price: ExactNumber::venue_decimal("100").unwrap(),
            fee: Some(AssetAmount {
                asset: AssetId::Named("USDT".into()),
                amount: ExactNumber::venue_decimal("0").unwrap(),
            }),
        })),
        forced_close: None,
        is_maker: false,
        venue_ts_ms: clock::wall_ms(),
        recv_ns: clock::now_ns(),
    }
}

#[tokio::test]
async fn exact_physical_live_replay_rotation_restart_preserves_external_baseline() {
    let prior = vec![
        WalRecord::Names {
            strategies: vec!["physical-owner".into()],
            symbols: vec!["BTCUSDT".into()],
        },
        WalRecord::LatchCleared {
            wall_ts_ms: 1,
            note: "accepted manual half-unit".into(),
            findings: vec![],
            restated_exposure: vec![engine_types::SymbolTotal {
                symbol: SymbolId(0),
                signed_qty: 0.5,
                exact_signed_qty: Some(ExactNumber::legacy_binary64(0.5).unwrap()),
            }],
        },
    ];
    let (mut engine, records) =
        crate::tests::physical_recovery_test_fixture(Box::new(Idle), &prior, 0.5).await;
    let sent = WalRecord::OrderSent {
        dispatch: None,
        request: request("owned", Side::Buy, "0.3"),
        wire_ns: clock::now_ns(),
        arrival_mid: 100.0,
    };
    engine.wal.append(&sent).unwrap();
    engine.books.orders.try_apply(&sent).unwrap();
    engine
        .take_update(update("owned", "one", Side::Buy, "0.1"))
        .await
        .unwrap();
    engine
        .take_update(update("owned", "two", Side::Buy, "0.2"))
        .await
        .unwrap();
    let expected = Exact::parse_decimal("0.8").unwrap();
    assert_eq!(engine.logged_exposure.get(&SymbolId(0)), Some(&expected));
    assert_eq!(
        engine.books.attribution.snapshot().positions[0].signed_qty,
        Exact::parse_decimal("0.3").unwrap()
    );
    let history = prior
        .into_iter()
        .chain(records.lock().unwrap().clone())
        .collect::<Vec<_>>();
    assert_eq!(
        crate::reconcile::physical_exposure(&history).unwrap(),
        engine.logged_exposure
    );
    let base = engine.rotation_base(clock::wall_ms());
    let WalRecord::SegmentBase {
        logged_exposure, ..
    } = &base
    else {
        unreachable!()
    };
    assert_eq!(
        logged_exposure[0].exact_signed_qty.as_ref().unwrap().value,
        expected
    );
    let encoded = serde_json::to_vec(&base).unwrap();
    let decoded = serde_json::from_slice::<WalRecord>(&encoded).unwrap();
    let (restarted, _) =
        crate::tests::physical_recovery_test_fixture(Box::new(Idle), &[decoded], 0.8).await;
    assert_eq!(restarted.logged_exposure, engine.logged_exposure);
    assert_eq!(
        restarted.books.attribution.snapshot(),
        engine.books.attribution.snapshot()
    );
}
