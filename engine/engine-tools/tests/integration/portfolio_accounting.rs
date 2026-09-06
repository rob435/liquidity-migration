use engine_core::attribution::Attribution;
use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};
use engine_types::{Side, StrategyId, SymbolId, WalRecord};

fn recovered(id: &str, side: Side, qty: &str, price: &str, asset: &str, fee: &str) -> WalRecord {
    let quantity = ExactNumber::venue_decimal(qty).unwrap();
    let px = ExactNumber::venue_decimal(price).unwrap();
    let fee = ExactNumber::venue_decimal(fee).unwrap();
    WalRecord::RecoveredFill {
        callbacks: None,
        exec_id: id.into(),
        client_order_id: id.into(),
        symbol: SymbolId(0),
        side,
        qty: quantity.value.to_f64().unwrap(),
        px: px.value.to_f64().unwrap(),
        fee: None,
        amounts: Some(ExecutionAmounts {
            settlement_asset: AssetId::Named("USDT".into()),
            quantity,
            price: px,
            fee: Some(AssetAmount {
                asset: AssetId::Named(asset.into()),
                amount: fee,
            }),
        }),
        allocation: None,
        is_maker: false,
        forced_close: None,
        venue_ts_ms: 1,
        recovered_wall_ts_ms: 2,
    }
}
fn exact_field(row: &serde_json::Value, name: &str) -> Exact {
    serde_json::from_value(row[name].clone()).unwrap()
}

#[test]
fn recovered_partial_close_retains_exact_cashflow_realized_and_foreign_asset_fees() {
    let mut attribution = Attribution::default();
    attribution
        .try_on_recovered(
            StrategyId(0),
            &recovered("open", Side::Buy, "0.3", "100", "BNB", "0.0003"),
        )
        .unwrap();
    attribution
        .try_on_recovered(
            StrategyId(0),
            &recovered("close", Side::Sell, "0.1", "110", "USDT", "0.01"),
        )
        .unwrap();
    assert_eq!(attribution.signed(StrategyId(0), SymbolId(0)), 0.2);
    let state = serde_json::to_value(attribution.snapshot()).unwrap();
    let rows = state["accounting"]
        .as_array()
        .expect("execution accounting was discarded from portfolio snapshot");
    let usdt = rows
        .iter()
        .find(|row| row["asset"] == serde_json::json!({"Named":"USDT"}))
        .expect("USDT accounting missing");
    let bnb = rows
        .iter()
        .find(|row| row["asset"] == serde_json::json!({"Named":"BNB"}))
        .expect("foreign fee asset disappeared");
    assert_eq!(
        exact_field(usdt, "execution_cash_flow"),
        Exact::from_i64(-19)
    );
    assert_eq!(exact_field(usdt, "realized_gross"), Exact::one());
    assert_eq!(
        exact_field(usdt, "fees"),
        Exact::parse_decimal("0.01").unwrap()
    );
    assert_eq!(
        exact_field(bnb, "fees"),
        Exact::parse_decimal("0.0003").unwrap()
    );
    assert_eq!(exact_field(bnb, "realized_gross"), Exact::zero());
}

#[test]
fn serialized_portfolio_restores_cashflow_and_fees_then_retains_totals_after_the_last_close() {
    let mut original = Attribution::default();
    original
        .try_on_recovered(
            StrategyId(0),
            &recovered("open", Side::Buy, "0.3", "100", "BNB", "0.0003"),
        )
        .unwrap();
    original
        .try_on_recovered(
            StrategyId(0),
            &recovered("partial", Side::Sell, "0.1", "110", "USDT", "0.01"),
        )
        .unwrap();
    let encoded = serde_json::to_string(&original.snapshot()).unwrap();
    let decoded = serde_json::from_str(&encoded).unwrap();
    let mut restored = Attribution::restore(&decoded).unwrap();
    assert_eq!(restored.snapshot(), original.snapshot());
    restored
        .try_on_recovered(
            StrategyId(0),
            &recovered("last", Side::Sell, "0.2", "120", "BNB", "0.001"),
        )
        .unwrap();
    assert_eq!(restored.signed(StrategyId(0), SymbolId(0)), 0.0);
    let state = restored.snapshot();
    assert!(state.positions.is_empty());
    assert!(state.unvalued.is_empty());
    assert!(state.accounting_complete_from_start);
    let usdt = state
        .accounting
        .iter()
        .find(|row| row.asset == AssetId::Named("USDT".into()))
        .unwrap();
    let bnb = state
        .accounting
        .iter()
        .find(|row| row.asset == AssetId::Named("BNB".into()))
        .unwrap();
    assert_eq!(usdt.execution_cash_flow, Exact::from_i64(5));
    assert_eq!(usdt.realized_gross, Exact::from_i64(5));
    assert_eq!(usdt.fees, Exact::parse_decimal("0.01").unwrap());
    assert_eq!(bnb.fees, Exact::parse_decimal("0.0013").unwrap());
    let empty_position_restart = Attribution::restore(
        &serde_json::from_str(&serde_json::to_string(&state).unwrap()).unwrap(),
    )
    .unwrap();
    assert_eq!(empty_position_restart.snapshot(), state);
}

#[test]
fn quantity_only_inputs_keep_uncertainty_after_restore_and_after_positions_become_flat() {
    let mut attribution = Attribution::default();
    attribution.note(StrategyId(0), SymbolId(0), Side::Buy, 1.0);
    let state = attribution.snapshot();
    assert!(state.accounting.is_empty());
    assert_eq!(state.unvalued.len(), 1);
    assert_eq!(state.unvalued[0].execution_cash_flow_events, 1);
    assert_eq!(state.unvalued[0].fee_events, 1);
    let mut restored = Attribution::restore(
        &serde_json::from_str(&serde_json::to_string(&state).unwrap()).unwrap(),
    )
    .unwrap();
    restored.note(StrategyId(0), SymbolId(0), Side::Sell, 1.0);
    let closed = restored.snapshot();
    assert!(closed.positions.is_empty());
    assert!(closed.accounting.is_empty());
    assert_eq!(closed.unvalued[0].execution_cash_flow_events, 2);
    assert_eq!(closed.unvalued[0].fee_events, 2);
    assert_eq!(closed.unvalued[0].realized_events, 1);
}
