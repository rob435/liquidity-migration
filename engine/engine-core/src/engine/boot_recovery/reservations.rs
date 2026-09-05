use super::*;

pub(super) fn restore_order_reservations<R: RiskKernel>(
    risk: &mut R,
    orders: &LedgerOfOrders,
    boot_ms: i64,
) -> Result<OrderRegistry, EngineError> {
    let mut registry = OrderRegistry::new(OrderRegistry::boot_prefix(boot_ms));
    for order in orders.in_flight() {
        registry.own(&order.request.client_order_id, order.request.strategy);
        // The kernel's partition must keep charging last boot's working
        // orders, or a restart hands every share out twice.
        let request = &order.request;
        let remaining_qty = request.qty - order.filled_qty;
        if !remaining_qty.is_finite() || remaining_qty < -1e-9 {
            return Err(EngineError::Boot(format!(
                "in-flight order {} has impossible remaining quantity: request {}, filled {}",
                request.client_order_id, request.qty, order.filled_qty
            )));
        }
        if remaining_qty <= 1e-9 {
            continue;
        }
        risk.register_order_price_range(
            &request.client_order_id,
            &Intent {
                strategy: request.strategy,
                symbol: request.symbol,
                side: request.side,
                qty: remaining_qty,
                kind: request.kind,
                stop: request.stop,
                reduce_only: request.reduce_only,
                tag: "recovered".to_string(),
                decided_ns: 0,
                // The order is already at the venue; there is nothing
                // left to decide about how it was placed, and its
                // leverage was set before it went.
                work: None,
                leverage: None,
            },
            remaining_qty,
            order.reservation_low_px,
            order.reservation_high_px,
        );
    }
    Ok(registry)
}
