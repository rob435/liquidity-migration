//! What the kernel has approved and not yet seen in an account view: orders in
//! flight, and fills newer than the reading being judged against. The venue's
//! view is the truth about everything older.

use std::collections::HashMap;

use engine_types::ids::SymbolId;

#[derive(Clone, Debug)]
pub(crate) struct Pending {
    pub symbol: SymbolId,
    pub signed_qty: f64,
    pub reduce_only: bool,
    /// The price the order was approved at. An order in flight is valued at
    /// this or the current price, whichever is higher.
    pub px: f64,
}

fn pending_px(pending: &Pending, price: &impl Fn(SymbolId) -> Option<f64>) -> Option<f64> {
    let now = price(pending.symbol).unwrap_or(0.0);
    let px = now.max(pending.px);
    if px.is_finite() && px > 0.0 {
        Some(px)
    } else {
        None
    }
}

#[derive(Debug, Default)]
pub(crate) struct Book {
    px: HashMap<u16, f64>,
    pending: HashMap<String, Pending>,
    /// Every fill with when it arrived. A fill newer than the account view
    /// is in neither the view nor the reservations, and the envelope must
    /// still see it; entries the view has caught up with are pruned.
    recent_fills: Vec<(u64, u16, f64)>,
}

impl Book {
    pub(crate) fn observe_px(&mut self, symbol: SymbolId, px: f64) {
        if px.is_finite() && px > 0.0 {
            self.px.insert(symbol.0, px);
        }
    }

    pub(crate) fn px(&self, symbol: SymbolId) -> Option<f64> {
        self.px.get(&symbol.0).copied()
    }

    pub(crate) fn register(&mut self, client_order_id: &str, pending: Pending) {
        self.pending.insert(client_order_id.to_string(), pending);
    }

    pub(crate) fn forget(&mut self, client_order_id: &str) {
        self.pending.remove(client_order_id);
    }

    pub(crate) fn on_fill(
        &mut self,
        client_order_id: &str,
        symbol: SymbolId,
        signed_qty: f64,
        recv_ns: u64,
    ) {
        self.recent_fills.push((recv_ns, symbol.0, signed_qty));
        let Some(pending) = self.pending.get_mut(client_order_id) else {
            // A fill for an order the kernel never reserved — a second writer
            // on the account. The account view is what carries it.
            return;
        };
        // The reservation shrinks toward zero by what actually filled.
        let left = pending.signed_qty.abs() - signed_qty.abs();
        pending.signed_qty = if left > 0.0 {
            left * pending.signed_qty.signum()
        } else {
            0.0
        };
        // A used-up reservation is finished business. Kept, it grows this map
        // — and every per-assessment scan of it — by one entry for each order
        // the process ever fills.
        if pending.signed_qty == 0.0 {
            self.pending.remove(client_order_id);
        }
    }

    /// Quantity already spoken for by resting reduce-only orders on this
    /// symbol. A second full-size exit on top of these is a stack, not a
    /// retry — a rejected or cancelled exit is forgotten and frees it.
    pub(crate) fn pending_reduce_qty(&self, symbol: SymbolId) -> f64 {
        self.pending
            .values()
            .filter(|p| p.reduce_only && p.symbol == symbol)
            .map(|p| p.signed_qty.abs())
            .sum()
    }

    /// Per-symbol net quantity of fills newer than the account view, pruning
    /// what the view has caught up with. The envelope adds these to the
    /// view's positions so a just-filled order is never counted nowhere.
    pub(crate) fn fills_after(&mut self, observed_ns: u64) -> HashMap<u16, f64> {
        self.recent_fills.retain(|(ns, _, _)| *ns > observed_ns);
        let mut net: HashMap<u16, f64> = HashMap::new();
        for (_, symbol, qty) in &self.recent_fills {
            *net.entry(*symbol).or_insert(0.0) += qty;
        }
        net
    }

    /// Notional in flight per symbol, not yet visible in the account view.
    /// `None` when an in-flight order cannot be priced. Per symbol rather than
    /// one total because the per-symbol cap needs to know where it sits.
    pub(crate) fn pending_notional_by_symbol(
        &self,
        price: impl Fn(SymbolId) -> Option<f64>,
    ) -> Option<Vec<(u16, f64)>> {
        let mut out: Vec<(u16, f64)> = Vec::new();
        for pending in self.pending.values() {
            if pending.reduce_only || pending.signed_qty == 0.0 {
                continue;
            }
            let notional = pending.signed_qty.abs() * pending_px(pending, &price)?;
            match out.iter_mut().find(|(symbol, _)| *symbol == pending.symbol.0) {
                Some((_, running)) => *running += notional,
                None => out.push((pending.symbol.0, notional)),
            }
        }
        Some(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(symbol: u16, qty: f64) -> Pending {
        Pending {
            symbol: SymbolId(symbol),
            signed_qty: qty,
            reduce_only: false,
            px: 10.0,
        }
    }

    #[test]
    fn a_used_up_reservation_leaves_the_pending_map() {
        let mut book = Book::default();
        book.register("a-1", entry(4, 3.0));
        book.on_fill("a-1", SymbolId(4), 2.0, 1);
        assert_eq!(book.pending.len(), 1, "a partial fill keeps the reservation");
        book.on_fill("a-1", SymbolId(4), 1.0, 2);
        assert!(book.pending.is_empty(), "a fully filled order must not stay reserved");
        // What filled is the account view's to carry from here.
        assert_eq!(book.fills_after(0).get(&4).copied(), Some(3.0));
    }
}
