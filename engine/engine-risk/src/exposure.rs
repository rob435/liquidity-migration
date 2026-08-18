//! Per-strategy exposure. The venue's account view is per symbol and carries no
//! strategy attribution, so the kernel keeps its own book from what it approved
//! and what came back.

use std::collections::HashMap;

use engine_types::ids::{StrategyId, SymbolId};

#[derive(Clone, Debug)]
pub(crate) struct Pending {
    pub strategy: StrategyId,
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
    filled: HashMap<(u16, u16), f64>,
    /// Fills for orders the kernel never registered — a second writer on the
    /// account. Charged to every strategy's share until they net out.
    unattributed: HashMap<u16, f64>,
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
        match self.pending.get_mut(client_order_id) {
            Some(pending) => {
                let strategy = pending.strategy.0;
                // The reservation shrinks toward zero by what actually filled.
                let left = pending.signed_qty.abs() - signed_qty.abs();
                pending.signed_qty = if left > 0.0 {
                    left * pending.signed_qty.signum()
                } else {
                    0.0
                };
                // A used-up reservation is finished business. Kept, it grows
                // this map — and every per-assessment scan of it — by one
                // entry for each order the process ever fills.
                if pending.signed_qty == 0.0 {
                    self.pending.remove(client_order_id);
                }
                *self.filled.entry((strategy, symbol.0)).or_insert(0.0) += signed_qty;
            }
            None => {
                *self.unattributed.entry(symbol.0).or_insert(0.0) += signed_qty;
            }
        }
    }

    /// Symbols charged to this strategy that only the price feed or the account
    /// view can value: what it holds, and anything a second writer moved.
    fn held_symbols(&self, strategy: StrategyId) -> Vec<u16> {
        let mut symbols: Vec<u16> = Vec::new();
        let mut push = |symbol: u16| {
            if !symbols.contains(&symbol) {
                symbols.push(symbol);
            }
        };
        for ((owner, symbol), qty) in &self.filled {
            if *owner == strategy.0 && qty.abs() > 0.0 {
                push(*symbol);
            }
        }
        for (symbol, qty) in &self.unattributed {
            if qty.abs() > 0.0 {
                push(*symbol);
            }
        }
        symbols
    }

    /// Gross notional this strategy already spends, at current prices. `None`
    /// when a charged symbol cannot be priced — the kernel then fails closed.
    pub(crate) fn strategy_notional_usdt(
        &self,
        strategy: StrategyId,
        price: impl Fn(SymbolId) -> Option<f64>,
    ) -> Option<f64> {
        let mut total = 0.0;
        for symbol in self.held_symbols(strategy) {
            let px = price(SymbolId(symbol))?;
            let held = self
                .filled
                .get(&(strategy.0, symbol))
                .copied()
                .unwrap_or(0.0);
            let foreign = self.unattributed.get(&symbol).copied().unwrap_or(0.0);
            total += (held.abs() + foreign.abs()) * px;
        }
        for pending in self.pending.values() {
            if pending.strategy != strategy || pending.reduce_only || pending.signed_qty == 0.0 {
                continue;
            }
            total += pending.signed_qty.abs() * pending_px(pending, &price)?;
        }
        Some(total)
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

    fn entry(strategy: u16, symbol: u16, qty: f64) -> Pending {
        Pending {
            strategy: StrategyId(strategy),
            symbol: SymbolId(symbol),
            signed_qty: qty,
            reduce_only: false,
            px: 10.0,
        }
    }

    #[test]
    fn a_used_up_reservation_leaves_the_pending_map() {
        let mut book = Book::default();
        book.register("a-1", entry(0, 4, 3.0));
        book.on_fill("a-1", SymbolId(4), 2.0, 1);
        assert_eq!(book.pending.len(), 1, "a partial fill keeps the reservation");
        book.on_fill("a-1", SymbolId(4), 1.0, 2);
        assert!(book.pending.is_empty(), "a fully filled order must not stay reserved");
        // The fill accounting survives the reservation's removal.
        assert_eq!(book.filled.get(&(0, 4)).copied(), Some(3.0));
    }
}
