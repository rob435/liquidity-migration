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
            qty: 2.0,
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
            qty: 3.0,
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
            qty: 3.0,
            venue_reduce_only: false
        }
    );
}
