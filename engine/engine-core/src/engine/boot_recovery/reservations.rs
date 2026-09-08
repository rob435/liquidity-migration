use super::*;

pub(super) fn restore_order_reservations<R: RiskKernel>(
    risk: &mut R,
    orders: &LedgerOfOrders,
    boot_ms: i64,
    account: &AccountView,
    working: &std::collections::BTreeSet<String>,
) -> Result<OrderRegistry, EngineError> {
    let mut registry = OrderRegistry::new(OrderRegistry::boot_prefix(boot_ms));
    for order in orders.in_flight() {
        if let Some(owner) = order.request.sleeve_owner() {
            registry.own(&order.request.client_order_id, owner);
        }
        // The kernel's partition must keep charging last boot's working
        // orders, or a restart hands every share out twice.
        let request = &order.request;
        let remaining_qty = order.remaining_qty().map_err(EngineError::Boot)?;
        if !remaining_qty.is_finite() || remaining_qty < -1e-9 {
            return Err(EngineError::Boot(format!(
                "in-flight order {} has impossible remaining quantity: request {}, filled {}",
                request.client_order_id,
                request.qty,
                order.filled_qty().map_err(EngineError::Boot)?
            )));
        }
        if remaining_qty == 0.0 {
            continue;
        }
        risk.register_order_exact_price_range_with_account(
            &request.client_order_id,
            &Intent {
                exact_prices: request.canonical_intent_prices(),
                exact_quantity: Some(Box::new(
                    order.remaining_exact().map_err(EngineError::Boot)?,
                )),
                strategy: request.strategy,
                symbol: request.symbol,
                side: request.side,
                qty: remaining_qty,
                kind: request.kind,
                stop: request.sleeve_stop(),
                reduce_only: request.is_sleeve_reduction(),
                tag: "recovered".to_string(),
                decided_ns: 0,
                // The order is already at the venue; there is nothing
                // left to decide about how it was placed, and its
                // leverage was set before it went.
                work: None,
                leverage: None,
            },
            &order.remaining_exact().map_err(EngineError::Boot)?,
            (&order.exact_price_range.low, &order.exact_price_range.high),
            account,
        );
        if working.contains(&request.client_order_id) {
            risk.mark_order_accepted(&request.client_order_id, clock::now_ns());
        }
    }
    Ok(registry)
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_risk::{EnvelopeConfig, Kernel, KernelConfig};

    fn kernel() -> Kernel {
        Kernel::new(KernelConfig {
            max_account_view_age_ns: 120_000_000_000,
            envelope: EnvelopeConfig {
                tracks_equity: false,
                reference_usdt: 1000.0,
                equity_fraction: 1.0,
                floor_usdt: 100.0,
                expand_dead_band_fraction: 0.05,
                gross_notional_multiple: 2.0,
                disaster_stop_fraction: 0.35,
                max_component_gross_notional_usdt: 2000.0,
                max_symbol_notional_usdt: 2000.0,
                max_initial_margin_usdt: 1000.0,
            },
            leverage: 2.0,
            qty_tolerance: 1e-12,
            max_rolling_loss_fraction: 0.1,
        })
        .unwrap()
    }
    fn intent(qty: f64) -> Intent {
        Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            kind: OrderKind::Limit {
                px: 10.0,
                tif: engine_types::TimeInForce::Gtc,
            },
            stop: Some(StopSpec { trigger_px: 9.0 }),
            reduce_only: false,
            tag: "margin-restart".into(),
            decided_ns: 3_000_000_000,
            work: None,
            leverage: None,
        }
    }
    fn orders() -> LedgerOfOrders {
        LedgerOfOrders::from_records(&[WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "old-working".into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: intent(1.0).kind,
                stop: intent(1.0).stop,
                reduce_only: false,
                close_position: false,
                exact_terms: None,
                sleeve_effect: None,
            },
            wire_ns: 1,
            arrival_mid: 10.0,
        }])
    }
    #[test]
    fn restored_working_margin_waits_for_a_scan_after_current_boot_confirmation() {
        let _clock = engine_types::clock::install_virtual(clock::wall_ns(), 2_000_000_000).unwrap();
        let mut kernel = kernel();
        let orders = orders();
        let mut account = AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 0.5,
            positions: vec![],
            observed_ns: 1_000_000_000,
        };
        restore_order_reservations(
            &mut kernel,
            &orders,
            1,
            &account,
            &std::collections::BTreeSet::from(["old-working".into()]),
        )
        .unwrap();
        assert!(matches!(
            kernel.assess(&intent(0.01), &account, intent(0.01).decided_ns),
            RiskVerdict::Deny {
                reason: DenyReason::AvailableMarginExhausted { .. }
            }
        ));
        account.observed_ns = 3_000_000_000;
        kernel.observe_account_view(&account);
        assert!(
            matches!(
                kernel.assess(&intent(0.01), &account, intent(0.01).decided_ns),
                RiskVerdict::Allow { .. }
            ),
            "the current scan already includes the venue-working reservation"
        );
    }
    #[test]
    fn restored_order_reservation_preserves_canonical_quantity_beyond_its_projection() {
        use engine_types::numeric::Exact;
        let quantity = Exact::parse_decimal("0.100000000000000001").unwrap();
        let mut request = orders().orders["old-working"].request.clone();
        engine_types::order_terms::ExactOrderTerms {
            quantity: quantity.clone(),
            limit_price: Some(Exact::from_u64(10)),
            stop_trigger_price: Some(Exact::from_u64(9)),
            physical_stop_trigger_price: Some(Exact::from_u64(9)),
            input_policy: engine_types::order_terms::OrderInputPolicy::CanonicalPortfolio,
        }
        .apply_projection(&mut request)
        .unwrap();
        let orders = LedgerOfOrders::from_records(&[WalRecord::OrderSent {
            dispatch: None,
            request,
            wire_ns: 1,
            arrival_mid: 10.0,
        }]);
        let account = AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: vec![],
            observed_ns: 1,
        };
        let mut kernel = kernel();
        restore_order_reservations(
            &mut kernel,
            &orders,
            1,
            &account,
            &std::collections::BTreeSet::new(),
        )
        .unwrap();
        let interval = kernel
            .physical_exposure_interval(SymbolId(0), &account)
            .unwrap();
        assert_eq!(
            interval.high(),
            &quantity,
            "reboot reconstructed an order reservation from its rounded display quantity"
        );
    }
}
