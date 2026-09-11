use engine_types::strategy_process::{CallbackSnapshot, OwnedOrderSnapshot, SnapshotCtx};
use engine_types::{OrderKind, Side, StrategyAccountSummary, StrategyCtx, StrategyId, SymbolId};

fn snapshot(remaining_qty: Option<f64>) -> CallbackSnapshot {
    CallbackSnapshot {
        strategy: StrategyId(0),
        now_ns: 1,
        wall_ms: 1,
        entries_enabled: false,
        account: StrategyAccountSummary {
            equity_usdt: 0.0,
            available_margin_usdt: 0.0,
            observed_ns: 0,
        },
        symbols: vec![],
        orders: vec![OwnedOrderSnapshot {
            id: "owned".into(),
            symbol: SymbolId(0),
            side: Side::Sell,
            kind: OrderKind::Market,
            qty: 0.3,
            filled_qty: 0.1,
            remaining_qty,
            reduce_only: true,
            acked: true,
            resting: true,
        }],
        global_checkpoint: None,
        strategy_names: vec!["owner".into()],
        strategy_events: vec![],
    }
}

#[test]
fn legacy_callback_json_omits_the_new_remainder_and_keeps_its_explicit_fallback() {
    let serialized = serde_json::to_value(snapshot(None)).unwrap();
    assert!(serialized["orders"][0].get("remaining_qty").is_none());
    let restored: CallbackSnapshot = serde_json::from_value(serialized).unwrap();
    let ctx = SnapshotCtx::new(&restored, |_| {}).unwrap();
    assert_eq!(ctx.order_facts("owned").unwrap().remaining_qty(), 0.3 - 0.1);
    assert_eq!(restored.orders[0].remaining_qty, None);
}

#[test]
fn canonical_remainder_is_preserved_and_invalid_transport_values_are_refused() {
    for remaining in [0.2, 1e-20, 0.0] {
        let bytes = serde_json::to_vec(&snapshot(Some(remaining))).unwrap();
        let restored: CallbackSnapshot = serde_json::from_slice(&bytes).unwrap();
        let ctx = SnapshotCtx::new(&restored, |_| {}).unwrap();
        assert_eq!(ctx.order_facts("owned").unwrap().remaining_qty(), remaining);
        let mut resting = vec![];
        ctx.resting(&mut resting);
        assert_eq!(resting[0].remaining_qty(), remaining);
    }
    for remaining in [-1.0, 0.4, f64::INFINITY, f64::NAN] {
        let invalid = snapshot(Some(remaining));
        assert!(SnapshotCtx::new(&invalid, |_| {}).is_err());
    }
}
