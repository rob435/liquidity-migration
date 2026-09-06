use super::common::*;
use engine_risk::Kernel;
use engine_types::numeric::{AssetId, Exact};
use engine_types::portfolio::{PortfolioPosition, PortfolioState};
use engine_types::risk::{DenyReason, PortfolioRiskVerdict, RiskKernel, RiskVerdict};
use engine_types::{OrderAck, OrderUpdate, Side};
fn binary(value: f64) -> Exact {
    Exact::from_legacy_f64(value).unwrap()
}

fn five_free() -> engine_types::AccountView {
    let mut account = flat(250_000.0, SEC);
    account.available_usdt = 5.0;
    account
}
fn second() -> engine_types::Intent {
    entry(LONG, CUSDT, Side::Buy, 0.1, 10.0, 9.0, 4 * SEC)
}
fn denied_margin(verdict: RiskVerdict) {
    assert!(
        matches!(
            verdict,
            RiskVerdict::Deny {
                reason: DenyReason::AvailableMarginExhausted { .. }
            }
        ),
        "unreflected order consumed the same free margin twice: {verdict:?}"
    );
}
fn reserve(kernel: &mut Kernel, account: &engine_types::AccountView) {
    kernel.register_order_with_account(
        "first",
        &entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC),
        1.0,
        account,
    );
}

#[test]
fn queued_and_attempted_orders_cannot_share_the_same_cached_free_margin() {
    let account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    kernel.mark_order_attempted("first");
    let mut later = account.clone();
    later.observed_ns = 3 * SEC;
    denied_margin(kernel.assess(&second(), &later, second().decided_ns));
}

#[test]
fn a_scan_started_before_acceptance_cannot_release_margin_when_it_finishes_later() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    kernel.on_update(&OrderUpdate::Ack(OrderAck {
        client_order_id: "first".into(),
        venue_order_id: "7".into(),
        sent_ns: SEC,
        ack_ns: 2 * SEC,
    }));
    account.observed_ns = SEC + SEC / 2;
    kernel.observe_account_view(&account);
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    account.observed_ns = 3 * SEC;
    kernel.observe_account_view(&account);
    assert_eq!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Allow { qty: 0.1 }
    );
}

#[test]
fn a_terminal_order_retains_its_unreflected_margin_until_a_post_change_scan() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    kernel.on_update(&OrderUpdate::Cancelled {
        client_order_id: "first".into(),
        recv_ns: 2 * SEC,
    });
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    account.observed_ns = 3 * SEC;
    kernel.observe_account_view(&account);
    assert_eq!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Allow { qty: 0.1 }
    );
}

#[test]
fn a_complete_fill_retains_unreflected_margin_until_a_post_fill_scan() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    kernel.on_update(&OrderUpdate::Fill {
        allocation: None,
        exec_id: "fill".into(),
        client_order_id: "first".into(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 1.0,
        px: 10.0,
        fee: Some(0.0),
        amounts: None,
        is_maker: false,
        forced_close: None,
        venue_ts_ms: 1,
        recv_ns: 2 * SEC,
    });
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    account
        .positions
        .push(position(BUSDT, Side::Buy, 1.0, 10.0, true));
    account.observed_ns = 3 * SEC;
    kernel.observe_account_view(&account);
    assert_eq!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Allow { qty: 0.1 }
    );
}

#[test]
fn restored_working_order_requires_confirmation_and_a_new_query_in_this_epoch() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    kernel.mark_order_accepted("first", 2 * SEC);
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    account.observed_ns = 3 * SEC;
    kernel.observe_account_view(&account);
    assert_eq!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Allow { qty: 0.1 }
    );
}

#[test]
fn pending_virtual_exits_reserve_cumulative_physical_margin_before_any_fill() {
    let state = PortfolioState {
        positions: vec![
            PortfolioPosition {
                strategy: CARRY,
                symbol: BUSDT,
                signed_qty: Exact::from_i64(2),
                entry_value: Some(Exact::from_i64(20)),
                stop_px: Some(Exact::from_i64(9)),
                settlement_asset: AssetId::Named("USDT".into()),
            },
            PortfolioPosition {
                strategy: LONG,
                symbol: BUSDT,
                signed_qty: Exact::from_i64(-2),
                entry_value: Some(Exact::from_i64(20)),
                stop_px: Some(Exact::from_i64(11)),
                settlement_asset: AssetId::Named("USDT".into()),
            },
        ],
        ..Default::default()
    };
    let account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.observe_price(BUSDT, 10.0);
    let intent = exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, SEC);
    assert_eq!(
        kernel.assess_portfolio(&intent, &account, &state, intent.decided_ns),
        PortfolioRiskVerdict::Allow {
            qty: engine_types::numeric::Exact::parse_decimal("1.0").unwrap(),
            venue_reduce_only: false
        }
    );
    kernel.register_order_with_account("first", &intent, 1.0, &account);
    let verdict = kernel.assess_portfolio(&intent, &account, &state, intent.decided_ns);
    assert!(
        matches!(
            verdict,
            PortfolioRiskVerdict::Deny {
                reason: DenyReason::AvailableMarginExhausted { .. }
            }
        ),
        "virtual exits reused spare margin: {verdict:?}"
    );
}

#[test]
fn a_partial_fill_and_late_ack_keep_the_newest_margin_confirmation() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    kernel.on_update(&OrderUpdate::Fill {
        allocation: None,
        exec_id: "partial".into(),
        client_order_id: "first".into(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 0.5,
        px: 10.0,
        fee: Some(0.0),
        amounts: None,
        is_maker: false,
        forced_close: None,
        venue_ts_ms: 1,
        recv_ns: 3 * SEC,
    });
    kernel.on_update(&OrderUpdate::Ack(OrderAck {
        client_order_id: "first".into(),
        venue_order_id: "7".into(),
        sent_ns: SEC,
        ack_ns: 2 * SEC,
    }));
    account
        .positions
        .push(position(BUSDT, Side::Buy, 0.5, 10.0, true));
    account.observed_ns = 2 * SEC;
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    account.observed_ns = 4 * SEC;
    kernel.observe_account_view(&account);
    assert_eq!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Allow { qty: 0.1 }
    );
}

#[test]
fn a_stale_response_cannot_reuse_margin_released_by_a_newer_scan() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    kernel.on_update(&OrderUpdate::Cancelled {
        client_order_id: "first".into(),
        recv_ns: 2 * SEC,
    });
    account.observed_ns = 3 * SEC;
    kernel.observe_account_view(&account);
    account.observed_ns = SEC;
    assert!(matches!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
}

#[test]
fn a_proven_native_reduction_flows_while_unreflected_margin_is_exhausted() {
    let mut account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    account
        .positions
        .push(position(CUSDT, Side::Buy, 1.0, 10.0, true));
    let intent = exit(LONG, CUSDT, Side::Sell, 1.0, 10.0, 4 * SEC);
    assert_eq!(
        kernel.assess(&intent, &account, intent.decided_ns),
        RiskVerdict::Allow { qty: 1.0 }
    );
    kernel.register_order_with_account("exit", &intent, 1.0, &account);
}

#[test]
fn assessing_a_price_amend_does_not_count_its_own_margin_twice_or_release_it() {
    let account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    reserve(&mut kernel, &account);
    let amend = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, 4 * SEC);
    assert_eq!(
        kernel.assess_price_amend("first", &amend, &account, amend.decided_ns),
        RiskVerdict::Allow { qty: 1.0 }
    );
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
}

#[test]
fn a_certain_native_exit_keeps_zero_margin_when_restored_with_a_price_range() {
    let mut account = five_free();
    account.available_usdt = 0.5;
    account
        .positions
        .push(position(BUSDT, Side::Buy, 2.0, 10.0, true));
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.register_order_price_range_with_account(
        "exit",
        &exit(CARRY, BUSDT, Side::Sell, 1.0, 10.0, SEC),
        1.0,
        (8.0, 12.0),
        &account,
    );
    assert_eq!(
        kernel.assess(&second(), &account, second().decided_ns),
        RiskVerdict::Allow { qty: 0.1 }
    );
}

#[test]
fn physical_interval_uses_the_same_pending_and_unabsorbed_fills_as_admission() {
    let account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.register_order_with_account(
        "buy",
        &entry(CARRY, BUSDT, Side::Buy, 2.0, 10.0, 9.0, SEC),
        2.0,
        &account,
    );
    kernel.on_update(&OrderUpdate::Fill {
        allocation: None,
        exec_id: "partial".into(),
        client_order_id: "buy".into(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 0.5,
        px: 10.0,
        fee: Some(0.0),
        amounts: None,
        is_maker: false,
        forced_close: None,
        venue_ts_ms: 1,
        recv_ns: 2 * SEC,
    });
    kernel.register_order(
        "sell",
        &entry(LONG, BUSDT, Side::Sell, 1.0, 10.0, 11.0, SEC),
        1.0,
    );
    let interval = kernel.physical_exposure_interval(BUSDT, &account).unwrap();
    assert_eq!(
        (interval.low(), interval.high()),
        (&binary(-0.5), &binary(2.0))
    );
    assert!(!interval.certainly_reduces(Side::Sell, &binary(0.1)));
    let after = interval.after(Side::Sell, &binary(0.5)).unwrap();
    assert_eq!((after.low(), after.high()), (&binary(-1.0), &binary(1.5)));
    for (low, high) in [(f64::NAN, 1.0), (0.0, f64::INFINITY), (1.0, -1.0)] {
        assert!(engine_types::risk::PhysicalExposureInterval::try_new(low, high).is_err());
    }
    let huge = engine_types::risk::PhysicalExposureInterval::try_new(1e308, 1e308).unwrap();
    assert_eq!(
        huge.after(Side::Buy, &binary(1e308)).unwrap().low(),
        &(binary(1e308) + binary(1e308))
    );
}

#[test]
fn canonical_completion_removes_float_residue_but_preserves_unabsorbed_fills() {
    let mut account = five_free();
    account
        .positions
        .push(position(BUSDT, Side::Buy, 0.6, 10.0, true));
    let mut kernel = Kernel::new(demo_config()).unwrap();
    kernel.register_order_with_account(
        "exit",
        &exit(CARRY, BUSDT, Side::Sell, 0.6, 10.0, SEC),
        0.6,
        &account,
    );
    for index in 0..6 {
        kernel.on_update(&OrderUpdate::Fill {
            allocation: None,
            exec_id: format!("fill-{index}"),
            client_order_id: "exit".into(),
            symbol: BUSDT,
            side: Side::Sell,
            qty: 0.1,
            px: 10.0,
            fee: Some(0.0),
            amounts: None,
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 1,
            recv_ns: 2 * SEC + index,
        });
    }
    kernel.complete_order("exit", 3 * SEC);
    let interval = kernel.physical_exposure_interval(BUSDT, &account).unwrap();
    assert_eq!(
        interval.low(),
        &(binary(0.6) - binary(0.1) * Exact::from_u64(6)),
        "completed order retains a false pending quantity"
    );
    assert_eq!(
        interval.high(),
        &(binary(0.6) - binary(0.1) * Exact::from_u64(6)),
        "completion erased unabsorbed physical fills"
    );
}

#[test]
fn dispatch_reassessment_excludes_only_its_pending_order_and_restores_the_hold() {
    let account = five_free();
    let mut kernel = Kernel::new(demo_config()).unwrap();
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, SEC);
    kernel.register_order_with_account("first", &intent, 1.0, &account);
    let before = kernel.physical_exposure_interval(BUSDT, &account).unwrap();
    assert_eq!((before.low(), before.high()), (&binary(0.0), &binary(1.0)));
    let excluded = kernel
        .physical_exposure_interval_excluding("first", BUSDT, &account)
        .unwrap();
    assert_eq!(
        (excluded.low(), excluded.high()),
        (&binary(0.0), &binary(0.0))
    );
    let state = PortfolioState::default();
    assert!(matches!(
        kernel.reassess_portfolio_order("first", &intent, &account, &state, intent.decided_ns),
        PortfolioRiskVerdict::Allow { qty, .. } if qty == Exact::one()
    ));
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
    assert_eq!(
        kernel.physical_exposure_interval(BUSDT, &account).unwrap(),
        before
    );
    assert!(kernel
        .physical_exposure_interval_excluding("first", CUSDT, &account)
        .is_err());
    assert_eq!(
        kernel.physical_exposure_interval(BUSDT, &account).unwrap(),
        before
    );
    let mut foreign = intent.clone();
    foreign.strategy = LONG;
    assert!(matches!(
        kernel.reassess_portfolio_order("first", &foreign, &account, &state, foreign.decided_ns),
        PortfolioRiskVerdict::Deny {
            reason: DenyReason::UnknownState { .. }
        }
    ));
    denied_margin(kernel.assess(&second(), &account, second().decided_ns));
}

#[test]
fn canonical_partial_quantity_replaces_subtraction_drift_without_losing_fill_or_margin() {
    let mut kernel = Kernel::new(demo_config()).unwrap();
    let account = five_free();
    let intent = entry(CARRY, BUSDT, Side::Buy, 0.6, 10.0, 9.0, SEC);
    kernel.register_order_with_account("fractional", &intent, 0.6, &account);
    let mut update = OrderUpdate::Fill {
        allocation: None,
        exec_id: "one".into(),
        client_order_id: "fractional".into(),
        symbol: BUSDT,
        side: Side::Buy,
        qty: 0.1,
        px: 10.0,
        fee: None,
        amounts: None,
        is_maker: false,
        forced_close: None,
        venue_ts_ms: 1,
        recv_ns: 2 * SEC,
    };
    for remaining in [0.5, 0.4, 0.3, 0.2, 0.1] {
        kernel.on_update_with_remaining(&update, remaining).unwrap();
    }
    denied_margin(kernel.assess(
        &entry(LONG, CUSDT, Side::Buy, 0.5, 10.0, 9.0, 3 * SEC),
        &account,
        3 * SEC,
    ));
    let mut caught_up = account.clone();
    caught_up.observed_ns = 3 * SEC;
    caught_up.positions = vec![position(BUSDT, Side::Buy, 0.5, 10.0, true)];
    let interval = kernel
        .physical_exposure_interval(BUSDT, &caught_up)
        .unwrap();
    assert_eq!(
        (interval.low(), interval.high()),
        (&binary(0.5), &(binary(0.5) + binary(0.1))),
        "risk kept a different pending remainder than the canonical order ledger"
    );
    assert!(kernel.on_update_with_remaining(&update, f64::NAN).is_err());
    assert_eq!(
        kernel
            .physical_exposure_interval(BUSDT, &caught_up)
            .unwrap(),
        interval
    );
    if let OrderUpdate::Fill { recv_ns, .. } = &mut update {
        *recv_ns = 4 * SEC;
    }
    kernel.on_update_with_remaining(&update, 0.0).unwrap();
    let recent = kernel
        .physical_exposure_interval(BUSDT, &caught_up)
        .unwrap();
    assert_eq!(recent.low(), &(binary(0.5) + binary(0.1)));
    assert!(
        recent.low().is_positive(),
        "canonical completion discarded actual recent fills"
    );
    assert_eq!(recent.low(), recent.high());
}
