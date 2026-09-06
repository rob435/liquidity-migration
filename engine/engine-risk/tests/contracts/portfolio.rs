use super::common::*;
use engine_risk::Kernel;
use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio::{PortfolioPosition, PortfolioState};
use engine_types::risk::{DenyReason, PortfolioRiskVerdict, RiskKernel};
use engine_types::{Side, StrategyId, SymbolId};

fn held(strategy: StrategyId, symbol: SymbolId, qty: i64) -> PortfolioPosition {
    PortfolioPosition {
        strategy,
        symbol,
        signed_qty: Exact::from_i64(qty),
        entry_value: Some(Exact::from_i64(qty.abs() * 10)),
        stop_px: Some(Exact::from_i64(if qty > 0 { 9 } else { 11 })),
        settlement_asset: AssetId::Named("USDT".into()),
    }
}
fn portfolio(rows: Vec<PortfolioPosition>) -> PortfolioState {
    PortfolioState {
        schema_version: 1,
        positions: rows,
        ..Default::default()
    }
}

#[test]
fn opposing_virtual_inventory_counts_against_gross_after_physical_flat_and_restart() {
    let state = portfolio(vec![held(CARRY, BUSDT, 30_000), held(LONG, BUSDT, -30_000)]);
    let restart = serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
    for state in [state, restart] {
        let mut kernel = Kernel::new(demo_config()).unwrap();
        kernel.observe_price(BUSDT, 10.0);
        let verdict = kernel.assess_portfolio(
            &entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC),
            &flat(250_000.0, SEC),
            &state,
        );
        assert!(
            matches!(
                verdict,
                PortfolioRiskVerdict::Deny {
                    reason: DenyReason::EnvelopeBreached { .. }
                }
            ),
            "{verdict:?}"
        );
    }
}

#[test]
fn virtual_exit_clamps_to_owner_and_survives_physical_flatness() {
    let state = portfolio(vec![held(CARRY, BUSDT, 2), held(LONG, BUSDT, -2)]);
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.observe_price(BUSDT, 10.0);
    let intent = exit(CARRY, BUSDT, Side::Sell, 10.0, 10.0, SEC);
    assert_eq!(
        kernel.assess_portfolio(&intent, &flat(250_000.0, SEC), &state),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("2.0").unwrap(),
            venue_reduce_only: false
        }
    );
}

#[test]
fn sibling_exit_reservations_cannot_consume_another_sleeves_exit_capacity() {
    let state = portfolio(vec![held(CARRY, BUSDT, 2), held(LONG, BUSDT, 3)]);
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.observe_price(BUSDT, 10.0);
    kernel.register_order(
        "carry-exit",
        &exit(CARRY, BUSDT, Side::Sell, 2.0, 10.0, SEC),
        2.0,
    );
    let account = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 5.0, 10.0, true)],
        SEC,
    );
    assert_eq!(
        kernel.assess_portfolio(
            &exit(LONG, BUSDT, Side::Sell, 9.0, 10.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("3.0").unwrap(),
            venue_reduce_only: true
        }
    );
    assert!(matches!(
        kernel.assess_portfolio(
            &exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Deny { .. }
    ));
}

#[test]
fn a_new_opposing_sleeve_may_cross_physical_flat_within_existing_gross_caps() {
    let state = portfolio(vec![held(CARRY, BUSDT, 2)]);
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.observe_price(BUSDT, 10.0);
    let account = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 2.0, 10.0, true)],
        SEC,
    );
    assert_eq!(
        kernel.assess_portfolio(
            &entry(LONG, BUSDT, Side::Sell, 3.0, 10.0, 11.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("3.0").unwrap(),
            venue_reduce_only: false
        }
    );
}

fn exact_held(qty: &str) -> PortfolioPosition {
    let quantity = Exact::parse_decimal(qty).unwrap();
    PortfolioPosition {
        strategy: CARRY,
        symbol: BUSDT,
        entry_value: Some(quantity.abs() * Exact::from_i64(10)),
        signed_qty: quantity,
        stop_px: Some(Exact::from_i64(9)),
        settlement_asset: AssetId::Named("USDT".into()),
    }
}

#[test]
fn a_legal_exact_position_below_legacy_dust_tolerance_can_reduce() {
    let mut kernel = Kernel::new(demo_config()).unwrap();
    let state = portfolio(vec![exact_held("0.0000000000001")]);
    let account = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 1e-13, 10.0, false)],
        SEC,
    );
    assert_eq!(
        kernel.assess_portfolio(
            &exit(CARRY, BUSDT, Side::Sell, 1e-13, 10.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("1e-13").unwrap(),
            venue_reduce_only: true
        }
    );
}

#[test]
fn reduction_direction_does_not_multiply_tiny_quantities_to_zero() {
    let mut config = demo_config();
    config.qty_tolerance = 0.0;
    let mut kernel = Kernel::new(config).unwrap();
    let state = portfolio(vec![exact_held("1e-200")]);
    let mut account = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 1e-200, 10.0, false)],
        SEC,
    );
    account.positions[0].exact_amounts = Some(Box::new(engine_types::risk::PositionAmounts {
        quantity: engine_types::numeric::ExactNumber::venue_decimal("1e-200").unwrap(),
        entry_price: engine_types::numeric::ExactNumber::venue_decimal("10").unwrap(),
    }));
    assert_eq!(
        kernel.assess_portfolio(
            &exit(CARRY, BUSDT, Side::Sell, 1e-200, 10.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("1e-200").unwrap(),
            venue_reduce_only: true
        }
    );
}

#[test]
fn virtual_exit_accepts_a_profit_locking_stop_on_the_surviving_short() {
    let mut short = held(LONG, BUSDT, -2);
    short.stop_px = Some(Exact::from_i64(9));
    let state = portfolio(vec![held(CARRY, BUSDT, 3), short]);
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.observe_price(BUSDT, 8.0);
    let mut physical = position(BUSDT, Side::Buy, 1.0, 10.0, true);
    physical.stop_px = 7.0;
    let account = view(250_000.0, vec![physical], SEC);
    assert_eq!(
        kernel.assess_portfolio(
            &exit(CARRY, BUSDT, Side::Sell, 3.0, 8.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("3.0").unwrap(),
            venue_reduce_only: false
        }
    );
}

#[test]
fn a_stop_already_crossed_by_current_price_cannot_authorize_more_portfolio_risk() {
    let state = portfolio(vec![held(CARRY, BUSDT, 2), held(LONG, BUSDT, -2)]);
    for current in [8.0, 12.0] {
        let mut kernel = Kernel::new(demo_config()).unwrap();
        kernel.observe_price(BUSDT, current);
        let verdict = kernel.assess_portfolio(
            &entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC),
            &flat(250_000.0, SEC),
            &state,
        );
        assert!(
            matches!(verdict, PortfolioRiskVerdict::Deny { .. }),
            "crossed stop allowed at{current}: {verdict:?}"
        );
    }
}

#[test]
fn pending_opposite_order_does_not_hide_margin_when_a_virtual_exit_fills_first() {
    let state = portfolio(vec![held(CARRY, BUSDT, 2), held(LONG, BUSDT, -2)]);
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.observe_price(BUSDT, 10.0);
    kernel.register_order(
        "pending-buy",
        &entry(CARRY, BUSDT, Side::Buy, 2.0, 10.0, 9.0, SEC),
        2.0,
    );
    kernel.mark_order_accepted("pending-buy", SEC);
    let mut account = flat(250_000.0, SEC);
    account.available_usdt = 4.99;
    let verdict = kernel.assess_portfolio(
        &exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, SEC),
        &account,
        &state,
    );
    assert!(
        matches!(
            verdict,
            PortfolioRiskVerdict::Deny {
                reason: DenyReason::AvailableMarginExhausted {
                    additional_margin_usdt: 5.0,
                    ..
                }
            }
        ),
        "pending buy hid required margin: {verdict:?}"
    );
    account.available_usdt = 5.0;
    assert_eq!(
        kernel.assess_portfolio(
            &exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, SEC),
            &account,
            &state
        ),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("1.0").unwrap(),
            venue_reduce_only: false
        }
    );
}

#[test]
fn a_tiny_unallocated_quantity_with_material_notional_counts_against_portfolio_caps() {
    let mut config = demo_config();
    config.envelope.max_component_gross_notional_usdt = 50.0;
    config.envelope.max_initial_margin_usdt = 25.0;
    let mut kernel = Kernel::new(config).unwrap();
    kernel.observe_price(BUSDT, 1e15);
    let account = view(
        250_000.0,
        vec![position(BUSDT, Side::Buy, 1e-13, 1e15, true)],
        SEC,
    );
    let verdict = kernel.assess_portfolio(
        &entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC),
        &account,
        &portfolio(vec![]),
    );
    assert!(
        matches!(
            verdict,
            PortfolioRiskVerdict::Deny {
                reason: DenyReason::ComponentGrossBreached { .. }
            }
        ),
        "material residual disappeared as dust: {verdict:?}"
    );
}

#[test]
fn native_position_stop_must_still_be_executable_at_current_price() {
    use engine_types::risk::RiskVerdict;
    for (side, current) in [(Side::Buy, 8.0), (Side::Sell, 12.0)] {
        let mut kernel = Kernel::new(demo_config()).unwrap();
        kernel.observe_price(BUSDT, current);
        let account = view(250_000.0, vec![position(BUSDT, side, 2.0, 10.0, true)], SEC);
        let verdict = kernel.assess(
            &entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC),
            &account,
        );
        assert!(
            matches!(verdict, RiskVerdict::Deny { .. }),
            "crossed native stop allowed more risk: {verdict:?}"
        );
    }
}

#[test]
fn tiny_opposing_native_rows_do_not_hide_the_one_way_account_violation() {
    let mut config = demo_config();
    config.qty_tolerance = 0.0;
    let mut kernel = Kernel::new(config).unwrap();
    let account = view(
        250_000.0,
        vec![
            position(BUSDT, Side::Buy, 1e-200, 10.0, true),
            position(BUSDT, Side::Sell, 1e-200, 10.0, true),
        ],
        SEC,
    );
    let verdict = kernel.assess_portfolio(
        &entry(CARRY, CUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC),
        &account,
        &portfolio(vec![]),
    );
    assert!(
        matches!(verdict, PortfolioRiskVerdict::Deny { .. }),
        "tiny hedge rows appeared one-way: {verdict:?}"
    );
}

#[test]
fn native_position_totals_beyond_binary64_keep_their_exact_direction() {
    let mut kernel = Kernel::new(demo_config()).unwrap();
    let account = view(
        250_000.0,
        vec![
            position(BUSDT, Side::Buy, 1e308, 10.0, true),
            position(BUSDT, Side::Buy, 1e308, 10.0, true),
        ],
        SEC,
    );
    let verdict = kernel.assess_portfolio(
        &exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, SEC),
        &account,
        &portfolio(vec![held(CARRY, BUSDT, 1)]),
    );
    assert!(matches!(
        verdict,
        PortfolioRiskVerdict::Allow {
            venue_reduce_only: true,
            ..
        }
    ));
    let interval = kernel.physical_exposure_interval(BUSDT, &account).unwrap();
    let total = Exact::from_legacy_f64(1e308).unwrap() * Exact::from_u64(2);
    assert_eq!(interval.low(), &total);
    assert_eq!(interval.high(), &total);
    assert!(interval.certainly_reduces(Side::Sell, &Exact::one()));
    assert!(!interval.certainly_reduces(Side::Buy, &Exact::one()));
}

#[test]
fn contradictory_exact_account_stop_cannot_authorize_a_physical_interval() {
    for raw in ["11", "-1", "1e-400"] {
        let mut row = position(BUSDT, Side::Buy, 1.0, 10.0, true);
        row.exact_stop_px = Some(Box::new(Exact::parse_decimal(raw).unwrap()));
        let account = view(250_000.0, vec![row], SEC);
        let mut kernel = Kernel::new(demo_config()).unwrap();
        assert!(
            kernel.physical_exposure_interval(BUSDT, &account).is_err(),
            "inconsistent native stop {raw} retained account authority"
        );
    }
}
