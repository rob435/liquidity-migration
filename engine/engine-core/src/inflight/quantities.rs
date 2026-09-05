use super::*;
use engine_types::numeric::{Exact, ExecutionAmounts};
use engine_types::wal::OrderFillQuantity;

pub(super) fn initial(request: &OrderRequest) -> OrderFillQuantity {
    if request.exact_terms.is_some() {
        OrderFillQuantity::Exact {
            quantity: Exact::zero(),
        }
    } else {
        OrderFillQuantity::LegacyBinary64 { quantity: 0.0 }
    }
}

pub(super) fn restore(open: &engine_types::OpenOrderState) -> OrderFillQuantity {
    open.fill_quantity
        .clone()
        .unwrap_or(OrderFillQuantity::LegacyBinary64 {
            quantity: open.filled_qty,
        })
}

fn projection(frontier: &OrderFillQuantity) -> Result<f64, String> {
    let value = match frontier {
        OrderFillQuantity::Exact { quantity } => {
            quantity.validate_storage().map_err(|e| e.to_string())?;
            quantity.to_f64().map_err(|e| e.to_string())?
        }
        OrderFillQuantity::LegacyBinary64 { quantity } => *quantity,
    };
    if !value.is_finite() || value < 0.0 {
        return Err("invalid cumulative order fill quantity".into());
    }
    Ok(value)
}

impl OrderRec {
    pub(crate) fn remaining_qty(&self) -> Result<f64, String> {
        match &self.fill_quantity {
            OrderFillQuantity::Exact { quantity } => {
                let requested = self
                    .request
                    .exact_terms
                    .as_ref()
                    .ok_or("exact fill frontier has no exact order terms")?;
                (requested.quantity.clone() - quantity.clone())
                    .max(Exact::zero())
                    .to_f64()
                    .map_err(|e| e.to_string())
            }
            OrderFillQuantity::LegacyBinary64 { quantity } => {
                Ok((self.request.qty - quantity).max(0.0))
            }
        }
    }

    fn next_fill(
        &self,
        qty: f64,
        amounts: Option<&ExecutionAmounts>,
    ) -> Result<(OrderFillQuantity, f64, bool), String> {
        if !qty.is_finite() || qty <= 0.0 {
            return Err("invalid order fill quantity".into());
        }
        let frontier = match (&self.fill_quantity, amounts) {
            (OrderFillQuantity::Exact { quantity }, Some(amounts)) => {
                amounts
                    .quantity
                    .validate_provenance()
                    .map_err(|e| e.to_string())?;
                if amounts.quantity.value.to_f64().map_err(|e| e.to_string())? != qty
                    || !amounts.quantity.value.is_positive()
                {
                    return Err("order fill quantity disagrees with its exact value".into());
                }
                let requested = self
                    .request
                    .exact_terms
                    .as_ref()
                    .ok_or("exact fill frontier has no exact order terms")?;
                let next = quantity.clone() + &amounts.quantity.value;
                if next > requested.quantity {
                    return Err("exact execution exceeds the order's remaining quantity".into());
                }
                let remaining = &requested.quantity - &next;
                remaining.to_f64().map_err(|e| e.to_string())?;
                OrderFillQuantity::Exact { quantity: next }
            }
            _ => OrderFillQuantity::LegacyBinary64 {
                quantity: projection(&self.fill_quantity)? + qty,
            },
        };
        let filled = projection(&frontier)?;
        let done = match &frontier {
            OrderFillQuantity::Exact { quantity } => {
                quantity
                    == &self
                        .request
                        .exact_terms
                        .as_ref()
                        .ok_or("missing exact order quantity")?
                        .quantity
            }
            OrderFillQuantity::LegacyBinary64 { .. } => filled + QTY_EPS >= self.request.qty,
        };
        Ok((frontier, filled, done))
    }

    pub(super) fn commit_fill(&mut self, qty: f64, amounts: Option<&ExecutionAmounts>) {
        let (frontier, filled, done) = self.next_fill(qty, amounts).expect("validated order fill");
        self.fill_quantity = frontier;
        self.filled_qty = filled;
        if done {
            self.ending = Some(Ending::Filled);
        }
    }
}

impl LedgerOfOrders {
    pub(crate) fn validate_fill_quantities(
        &self,
        id: &str,
        qty: f64,
        amounts: Option<&ExecutionAmounts>,
    ) -> Result<(), String> {
        if let Some(order) = self.orders.get(id) {
            order.next_fill(qty, amounts)?;
        }
        Ok(())
    }

    pub(crate) fn validate_record_quantities(&self, record: &WalRecord) -> Result<(), String> {
        match record {
            WalRecord::OrderSent { request, .. } => {
                if let Some(terms) = &request.exact_terms {
                    terms
                        .validate_projection(request)
                        .map_err(|e| e.to_string())?;
                }
            }
            WalRecord::AmendSent { spec, .. } => {
                if spec.px.is_some_and(|px| !px.is_finite() || px <= 0.0)
                    || spec.qty.is_some_and(|qty| !qty.is_finite() || qty <= 0.0)
                {
                    return Err("invalid amendment amounts".into());
                }
                if let Some(terms) = &spec.exact_terms {
                    terms.validate_projection(spec).map_err(|e| e.to_string())?;
                }
            }
            WalRecord::AmendResolved {
                effective_px,
                exact_effective_px,
                ..
            } => {
                if !effective_px.is_finite() || *effective_px <= 0.0 {
                    return Err("invalid effective amendment price".into());
                }
                if let Some(number) = exact_effective_px {
                    number.value.validate_storage().map_err(|e| e.to_string())?;
                    number.validate_provenance().map_err(|e| e.to_string())?;
                    if number.value.to_f64().map_err(|e| e.to_string())? != *effective_px {
                        return Err("effective amendment price disagrees with exact amount".into());
                    }
                }
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Amended {
                        px,
                        qty,
                        exact_terms,
                        ..
                    },
                ..
            } => {
                if !px.is_finite() || *px <= 0.0 || !qty.is_finite() || *qty < 0.0 {
                    return Err("invalid amended order state".into());
                }
                if let Some(terms) = exact_terms {
                    terms
                        .validate_projection(*px, *qty)
                        .map_err(|e| e.to_string())?;
                }
            }
            WalRecord::SegmentBase { open_orders, .. } => {
                for open in open_orders {
                    if let Some(terms) = &open.request.exact_terms {
                        terms
                            .validate_projection(&open.request)
                            .map_err(|e| e.to_string())?;
                    }
                    let frontier = restore(open);
                    if projection(&frontier)? != open.filled_qty {
                        return Err(
                            "order fill frontier disagrees with its quantity projection".into()
                        );
                    }
                    if let OrderFillQuantity::Exact { quantity } = frontier {
                        let requested = open
                            .request
                            .exact_terms
                            .as_ref()
                            .ok_or("exact fill frontier has no exact order terms")?;
                        if quantity >= requested.quantity {
                            return Err(
                                "an exact completed order cannot be restated as open".into()
                            );
                        }
                        (&requested.quantity - &quantity)
                            .to_f64()
                            .map_err(|e| e.to_string())?;
                    }
                }
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        client_order_id,
                        qty,
                        amounts,
                        ..
                    },
                ..
            } => {
                self.validate_fill_quantities(client_order_id, *qty, amounts.as_deref())?;
            }
            WalRecord::RecoveredFill {
                client_order_id,
                qty,
                amounts,
                ..
            } => {
                self.validate_fill_quantities(client_order_id, *qty, amounts.as_ref())?;
            }
            _ => {}
        }
        Ok(())
    }
}
