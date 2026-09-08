use super::common::*;
use engine_risk::Kernel;
use engine_types::numeric::{AssetId, Exact, ExactNumber, ExecutionAmounts};
use engine_types::portfolio::{PortfolioPosition, PortfolioState};
use engine_types::risk::{
    AccountAmounts, ClosedTradeRow, DenyReason, PortfolioRiskVerdict, PositionAmounts, RiskKernel,
    RiskVerdict,
};
use engine_types::{OrderUpdate, Side};
fn dec(s: &str) -> Exact {
    Exact::parse_decimal(s).unwrap()
}
fn number(s: &str) -> ExactNumber {
    ExactNumber::venue_decimal(s).unwrap()
}
fn canonical_account(available: &str) -> engine_types::AccountView {
    let mut account = flat(250_000.0, SEC);
    account.available_usdt = dec(available).to_f64().unwrap();
    account.exact_amounts = Some(Box::new(AccountAmounts {
        initial_margin_rate: None,
        maintenance_margin_rate: None,
        equity_usdt: number("250000"),
        available_usdt: number(available),
    }));
    account
}
fn permissive() -> engine_risk::KernelConfig {
    let mut cfg = demo_config();
    cfg.leverage = 1.0;
    cfg.envelope.gross_notional_multiple = 1.0;
    cfg.envelope.max_component_gross_notional_usdt = 250_000.0;
    cfg
}
fn canonical_entry(qty: &str) -> engine_types::Intent {
    let mut intent = entry(
        CARRY,
        BUSDT,
        Side::Buy,
        dec(qty).to_f64().unwrap(),
        1.0,
        0.5,
        SEC,
    );
    intent.exact_quantity = Some(Box::new(dec(qty)));
    intent
}
#[test]
fn canonical_available_margin_rejects_the_order_binary64_would_fund_after_restart() {
    let account = canonical_account("0.9999999999999999999");
    let replay = serde_json::from_slice(&serde_json::to_vec(&account).unwrap()).unwrap();
    for account in [account, replay] {
        let mut kernel = Kernel::new(permissive()).unwrap();
        assert!(matches!(
            kernel.assess(
                &canonical_entry("1"),
                &account,
                canonical_entry("1").decided_ns
            ),
            RiskVerdict::Deny {
                reason: DenyReason::AvailableMarginExhausted { .. }
            }
        ));
    }
}
#[test]
fn two_sub_ulp_margin_reservations_cannot_spend_the_same_balance() {
    let account = canonical_account("1");
    let mut kernel = Kernel::new(permissive()).unwrap();
    let first = canonical_entry("0.5000000000000000001");
    assert!(matches!(
        kernel.assess(&first, &account, first.decided_ns),
        RiskVerdict::Allow { .. }
    ));
    kernel.register_order_with_account("first", &first, first.qty, &account);
    assert!(matches!(
        kernel.assess(
            &canonical_entry("0.5"),
            &account,
            canonical_entry("0.5").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::AvailableMarginExhausted { .. }
        }
    ));
    kernel.complete_order("first", 2 * SEC);
    assert!(matches!(
        kernel.assess(
            &canonical_entry("0.5"),
            &account,
            canonical_entry("0.5").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::AvailableMarginExhausted { .. }
        }
    ));
    let mut refreshed = account.clone();
    refreshed.observed_ns = 3 * SEC;
    let mut second = canonical_entry("0.5");
    second.decided_ns = 3 * SEC;
    kernel.observe_account_view(&refreshed);
    assert!(matches!(
        kernel.assess(&second, &refreshed, second.decided_ns),
        RiskVerdict::Allow { .. }
    ));
}
#[test]
fn canonical_held_notional_cannot_round_down_onto_the_gross_cap() {
    let mut cfg = permissive();
    cfg.envelope.max_component_gross_notional_usdt = 1.0;
    let mut account = canonical_account("100");
    let mut held = position(BUSDT, Side::Buy, 1.0, 1.0, true);
    held.exact_amounts = Some(Box::new(PositionAmounts {
        liquidation_price: None,
        mark_price: None,
        quantity: number("1.0000000000000000001"),
        entry_price: number("1"),
    }));
    account.positions.push(held);
    let mut kernel = Kernel::new(cfg).unwrap();
    assert!(matches!(
        kernel.assess(
            &canonical_entry("0.0000000000000000001"),
            &account,
            canonical_entry("0.0000000000000000001").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::ComponentGrossBreached { .. }
        }
    ));
}
#[test]
fn exact_sleeve_exit_and_private_partial_fill_keep_the_last_native_unit() {
    let quantity = dec("9007199254740993.0000000000000000001");
    let mut account = canonical_account("250000");
    let mut held = position(BUSDT, Side::Buy, quantity.to_f64().unwrap(), 1.0, true);
    held.exact_amounts = Some(Box::new(PositionAmounts {
        liquidation_price: None,
        mark_price: None,
        quantity: ExactNumber::derived(quantity.clone()),
        entry_price: number("1"),
    }));
    account.positions.push(held);
    let state = PortfolioState {
        schema_version: 2,
        positions: vec![PortfolioPosition {
            strategy: CARRY,
            symbol: BUSDT,
            signed_qty: quantity.clone(),
            entry_value: Some(quantity.clone()),
            stop_px: Some(dec("0.5")),
            settlement_asset: AssetId::Named("USDT".into()),
        }],
        ..Default::default()
    };
    let mut intent = canonical_entry("9007199254740993.0000000000000000001");
    intent.side = Side::Sell;
    intent.reduce_only = true;
    intent.stop = None;
    let mut kernel = Kernel::new(permissive()).unwrap();
    assert_eq!(
        kernel.assess_portfolio(&intent, &account, &state, intent.decided_ns),
        PortfolioRiskVerdict::Allow {
            qty: quantity.clone(),
            venue_reduce_only: true
        }
    );
    kernel.register_order_with_account("exit", &intent, intent.qty, &account);
    let fill_qty = dec("9007199254740993");
    let remaining = &quantity - &fill_qty;
    let update = OrderUpdate::Fill {
        allocation: None,
        exec_id: "native".into(),
        client_order_id: "exit".into(),
        symbol: BUSDT,
        side: Side::Sell,
        qty: fill_qty.to_f64().unwrap(),
        px: 1.0,
        fee: None,
        amounts: Some(Box::new(ExecutionAmounts {
            settlement_asset: AssetId::Named("USDT".into()),
            quantity: ExactNumber::derived(fill_qty),
            price: number("1"),
            fee: None,
        })),
        is_maker: false,
        forced_close: None,
        venue_ts_ms: 1,
        recv_ns: 2 * SEC,
    };
    kernel
        .on_update_with_exact_remaining(&update, &remaining)
        .unwrap();
    let interval = kernel.physical_exposure_interval(BUSDT, &account).unwrap();
    assert!(interval.low().is_zero());
    assert_eq!(interval.high(), &remaining);
}
#[test]
fn malformed_canonical_account_cannot_release_a_held_reservation() {
    let mut kernel = Kernel::new(permissive()).unwrap();
    let account = canonical_account("1");
    let intent = canonical_entry("1");
    kernel.register_order_with_account("held", &intent, 1.0, &account);
    kernel.complete_order("held", 2 * SEC);
    let mut malformed = account.clone();
    malformed.observed_ns = 3 * SEC;
    malformed.exact_amounts.as_mut().unwrap().available_usdt = number("2");
    kernel.observe_account_view(&malformed);
    assert!(matches!(
        kernel.assess(
            &canonical_entry("0.5"),
            &account,
            canonical_entry("0.5").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::AvailableMarginExhausted { .. }
        }
    ));
    assert!(matches!(
        kernel.assess(
            &canonical_entry("0.5"),
            &malformed,
            canonical_entry("0.5").decided_ns
        ),
        RiskVerdict::Deny { .. }
    ));
}
#[test]
fn exact_rolling_loss_sum_survives_cancellation_and_restart() {
    let mut kernel = Kernel::new(permissive()).unwrap();
    let rows = [
        ClosedTradeRow {
            unpriced: None,
            closed_ms: 1,
            net_usdt: 1e20,
            net_usdt_exact: Some(dec("100000000000000000000")),
        },
        ClosedTradeRow {
            unpriced: None,
            closed_ms: 2,
            net_usdt: -25000.0,
            net_usdt_exact: Some(dec("-25000")),
        },
        ClosedTradeRow {
            unpriced: None,
            closed_ms: 3,
            net_usdt: -1e20,
            net_usdt_exact: Some(dec("-100000000000000000000")),
        },
    ];
    for row in &rows {
        kernel.observe_closed_trade(row.clone());
    }
    assert!(kernel.rolling_loss().tripped);
    assert_eq!(kernel.rolling_loss().net_usdt, -25000.0);
    let encoded = serde_json::to_vec(&kernel.rolling_loss_rows()).unwrap();
    let replay: Vec<ClosedTradeRow> = serde_json::from_slice(&encoded).unwrap();
    let mut restarted = Kernel::new(permissive()).unwrap();
    restarted.restore_rolling_loss_rows(&replay);
    assert!(matches!(
        restarted.assess(
            &canonical_entry("1"),
            &canonical_account("100"),
            canonical_entry("1").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::RollingLossTripped { .. }
        }
    ));
}
#[test]
fn malformed_canonical_loss_never_disappears_from_a_live_window_or_replay() {
    let row = ClosedTradeRow {
        unpriced: None,
        closed_ms: 1,
        net_usdt: 0.0,
        net_usdt_exact: Some(dec("-25000")),
    };
    assert!(serde_json::from_slice::<ClosedTradeRow>(&serde_json::to_vec(&row).unwrap()).is_err());
    let mut kernel = Kernel::new(permissive()).unwrap();
    kernel.observe_closed_trade(row);
    let retained = kernel.rolling_loss_rows();
    assert_eq!(
        retained.len(),
        1,
        "rotation dropped the unreadable canonical loss"
    );
    assert!(
        serde_json::from_slice::<Vec<ClosedTradeRow>>(&serde_json::to_vec(&retained).unwrap())
            .is_err()
    );
    kernel.observe_wall_clock_ms(engine_risk::ROLLING_LOSS_WINDOW_MS + 100);
    assert_eq!(
        kernel.rolling_loss_rows(),
        retained,
        "expiry erased unresolved canonical evidence"
    );
    let mut restarted = Kernel::new(permissive()).unwrap();
    restarted.restore_rolling_loss_rows(&retained);
    assert!(restarted.rolling_loss().tripped);
    let mut account = canonical_account("100");
    account
        .positions
        .push(position(BUSDT, Side::Buy, 1.0, 1.0, true));
    assert!(matches!(
        kernel.assess(
            &exit(CARRY, BUSDT, Side::Sell, 1.0, 1.0, SEC),
            &account,
            SEC
        ),
        RiskVerdict::Allow { .. }
    ));
    assert!(kernel.rolling_loss().tripped);
    assert!(matches!(
        kernel.assess(
            &canonical_entry("1"),
            &canonical_account("100"),
            canonical_entry("1").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
}

#[test]
fn canonical_equity_crosses_the_expansion_deadband_before_projection() {
    let mut config = equity_tracking_config();
    config.envelope.reference_usdt = 1000.0;
    config.envelope.max_component_gross_notional_usdt = 2000.0;
    config.envelope.max_initial_margin_usdt = 1000.0;
    let mut account = canonical_account("1000");
    account.equity_usdt = 1050.0;
    account.exact_amounts.as_mut().unwrap().equity_usdt = number("1050.000000000000001");
    let mut kernel = Kernel::new(config).unwrap();
    kernel.observe_account_view(&account);
    assert_eq!(kernel.capital_reference_usdt(), 1050.0);
}

fn with_prices(mut intent: engine_types::Intent, limit: &str, stop: &str) -> engine_types::Intent {
    intent.exact_prices = Some(Box::new(engine_types::orders::IntentPrices {
        limit_price: Some(dec(limit)),
        stop_trigger_price: Some(dec(stop)),
    }));
    intent.kind = engine_types::OrderKind::Limit {
        px: dec(limit).to_f64().unwrap(),
        tif: engine_types::TimeInForce::Gtc,
    };
    intent.stop = Some(engine_types::StopSpec {
        trigger_px: dec(stop).to_f64().unwrap(),
    });
    intent
}

#[test]
fn canonical_order_price_changes_margin_admission_and_replayed_reservations() {
    let account = canonical_account("1");
    let order = with_prices(canonical_entry("1"), "1.0000000000000000001", "0.5");
    assert!(matches!(
        Kernel::new(permissive())
            .unwrap()
            .assess(&order, &account, order.decided_ns),
        RiskVerdict::Deny {
            reason: DenyReason::AvailableMarginExhausted { .. }
        }
    ));
    let order = with_prices(canonical_entry("0.5"), "1.0000000000000000001", "0.5");
    let replay = serde_json::from_slice(&serde_json::to_vec(&order).unwrap()).unwrap();
    for order in [order, replay] {
        let mut kernel = Kernel::new(permissive()).unwrap();
        kernel.register_order_with_account("known-native-price", &order, order.qty, &account);
        assert!(matches!(
            kernel.assess(
                &canonical_entry("0.5"),
                &account,
                canonical_entry("0.5").decided_ns
            ),
            RiskVerdict::Deny {
                reason: DenyReason::AvailableMarginExhausted { .. }
            }
        ));
    }
}

#[test]
fn canonical_short_stop_remains_protective_when_its_projection_equals_the_limit() {
    let mut intent = canonical_entry("1");
    intent.side = Side::Sell;
    let intent = with_prices(intent, "1", "1.0000000000000000001");
    let replay = serde_json::from_slice(&serde_json::to_vec(&intent).unwrap()).unwrap();
    for intent in [intent, replay] {
        assert_eq!(
            Kernel::new(permissive()).unwrap().assess(
                &intent,
                &canonical_account("10"),
                intent.decided_ns
            ),
            RiskVerdict::Allow { qty: 1.0 }
        );
    }
}

#[test]
fn canonical_ambiguous_price_range_reserves_the_higher_possible_native_tick() {
    let account = canonical_account("1");
    let order = with_prices(canonical_entry("0.5"), "1", "0.5");
    let mut kernel = Kernel::new(permissive()).unwrap();
    kernel.register_order_exact_price_range_with_account(
        "ambiguous",
        &order,
        &dec("0.5"),
        (&dec("1"), &dec("1.0000000000000000001")),
        &account,
    );
    assert!(matches!(
        kernel.assess(
            &canonical_entry("0.5"),
            &account,
            canonical_entry("0.5").decided_ns
        ),
        RiskVerdict::Deny {
            reason: DenyReason::AvailableMarginExhausted { .. }
        }
    ));
}

#[test]
fn contradictory_canonical_order_price_or_stop_cannot_authorize_an_order() {
    for (limit, stop) in [("2", "0.5"), ("1", "0.9")] {
        let mut intent = canonical_entry("1");
        intent.exact_prices = Some(Box::new(engine_types::orders::IntentPrices {
            limit_price: Some(dec(limit)),
            stop_trigger_price: Some(dec(stop)),
        }));
        assert!(matches!(
            Kernel::new(permissive()).unwrap().assess(
                &intent,
                &canonical_account("10"),
                intent.decided_ns
            ),
            RiskVerdict::Deny {
                reason: DenyReason::UnknownState { .. }
            }
        ));
    }
}

#[test]
fn scalar_strategy_prices_use_the_same_decimal_units_as_the_outbound_wire() {
    let intent = entry(CARRY, BUSDT, Side::Buy, 3.0, 0.1, 0.05, SEC);
    assert_eq!(intent.limit_price().unwrap(), Some(dec("0.1")));
    assert_eq!(intent.stop_price().unwrap(), Some(dec("0.05")));
    let mut kernel = Kernel::new(permissive()).unwrap();
    assert_eq!(
        kernel.assess(&intent, &canonical_account("0.3"), intent.decided_ns),
        RiskVerdict::Allow { qty: 3.0 }
    );
}

#[test]
fn canonical_position_stop_must_remain_serializable_before_risk_reads_it() {
    let base = dec("1e4096");
    let denominator = &base * &base;
    let stop = (&denominator + Exact::one())
        .checked_div(&denominator)
        .unwrap();
    assert_eq!(stop.to_f64().unwrap(), 1.0);
    let mut held = position(BUSDT, Side::Sell, 1.0, 0.5, true);
    held.stop_px = 1.0;
    held.exact_stop_px = Some(Box::new(stop));
    assert_eq!(
        held.stop_price(),
        Err(engine_types::numeric::ExactError::InputTooLarge)
    );
}

#[test]
fn native_valuation_debt_blocks_entries_through_rotation_but_keeps_reductions_and_expires() {
    use engine_types::risk::UnpricedTradeReason;
    for reason in [
        UnpricedTradeReason::FeeValue,
        UnpricedTradeReason::SettlementAsset,
    ] {
        let mut kernel = Kernel::new(permissive()).unwrap();
        let account = canonical_account("100");
        let row = ClosedTradeRow {
            unpriced: Some(reason),
            net_usdt_exact: None,
            closed_ms: 1000,
            net_usdt: 0.0,
        };
        kernel.observe_closed_trade(row.clone());
        let serialized = serde_json::to_vec(&kernel.rolling_loss_rows()).unwrap();
        let rows: Vec<ClosedTradeRow> = serde_json::from_slice(&serialized).unwrap();
        assert_eq!(rows, vec![row]);
        let mut restored = Kernel::new(permissive()).unwrap();
        restored.restore_rolling_loss_rows(&rows);
        for kernel in [&mut kernel, &mut restored] {
            assert!(kernel.rolling_loss().tripped);
            assert!(matches!(
                kernel.assess(
                    &canonical_entry("1"),
                    &account,
                    canonical_entry("1").decided_ns
                ),
                RiskVerdict::Deny {
                    reason: DenyReason::UnknownState { .. }
                }
            ));
            let mut held = account.clone();
            held.positions
                .push(position(BUSDT, Side::Buy, 1.0, 1.0, true));
            let mut exit = canonical_entry("1");
            exit.side = Side::Sell;
            exit.stop = None;
            exit.reduce_only = true;
            assert!(matches!(
                kernel.assess(&exit, &held, exit.decided_ns),
                RiskVerdict::Allow { .. }
            ));
            kernel.observe_wall_clock_ms(999 + engine_risk::ROLLING_LOSS_WINDOW_MS);
            assert!(kernel.rolling_loss().tripped);
            kernel.observe_wall_clock_ms(1000 + engine_risk::ROLLING_LOSS_WINDOW_MS);
            assert!(!kernel.rolling_loss().tripped);
            assert!(matches!(
                kernel.assess(
                    &canonical_entry("1"),
                    &account,
                    canonical_entry("1").decided_ns
                ),
                RiskVerdict::Allow { .. }
            ));
        }
    }
}

#[test]
fn reporting_projection_preserves_derived_loss_outside_binary64_range() {
    for amount in ["-1e-400", "-1e400"] {
        let amount = dec(amount);
        assert!(
            amount.to_f64().is_err(),
            "native input validation was weakened"
        );
        let row = ClosedTradeRow {
            unpriced: None,
            net_usdt: amount.reporting_f64(),
            net_usdt_exact: Some(amount.clone()),
            closed_ms: 1,
        };
        let replay: ClosedTradeRow =
            serde_json::from_slice(&serde_json::to_vec(&row).unwrap()).unwrap();
        assert_eq!(replay.net().unwrap(), Some(amount));
    }
    let contradictory = ClosedTradeRow {
        unpriced: Some(engine_types::risk::UnpricedTradeReason::FeeValue),
        net_usdt: 1.0,
        net_usdt_exact: None,
        closed_ms: 1,
    };
    assert!(
        serde_json::from_slice::<ClosedTradeRow>(&serde_json::to_vec(&contradictory).unwrap())
            .is_err()
    );
}
