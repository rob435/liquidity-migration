//! Cash margin remains reserved until an account query starts after confirmation.
use engine_types::ids::SymbolId;
use engine_types::numeric::{Exact, ExactSum};
use std::collections::BTreeMap;
#[derive(Debug)]
enum Phase {
    Queued,
    Attempted,
    Working { confirmed_ns: u64 },
}
#[derive(Debug)]
pub(crate) struct Reservation {
    symbol: SymbolId,
    quantity: Option<Exact>,
    px: Option<Exact>,
    phase: Phase,
}
#[derive(Debug)]
struct Retired {
    quantity: Option<Exact>,
    px: Option<Exact>,
    confirmed_ns: u64,
}
#[derive(Debug, Default)]
pub(crate) struct MarginBook {
    active: BTreeMap<String, Reservation>,
    retired: BTreeMap<SymbolId, Retired>,
    frontier_ns: u64,
}
fn max_price(a: Option<Exact>, b: Option<Exact>) -> Option<Exact> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.max(b)),
        (a, b) => a.or(b),
    }
}
impl MarginBook {
    pub(crate) fn register(
        &mut self,
        id: &str,
        symbol: SymbolId,
        quantity: Option<Exact>,
        px: Option<Exact>,
    ) {
        self.active.insert(
            id.into(),
            Reservation {
                symbol,
                quantity,
                px,
                phase: Phase::Queued,
            },
        );
    }
    pub(crate) fn set_quantity(&mut self, id: &str, quantity: Option<Exact>) {
        if let Some(row) = self.active.get_mut(id) {
            row.quantity = quantity;
        }
    }
    pub(crate) fn take(&mut self, id: &str) -> Option<Reservation> {
        self.active.remove(id)
    }
    pub(crate) fn restore(&mut self, id: &str, row: Option<Reservation>) {
        if let Some(row) = row {
            self.active.insert(id.into(), row);
        }
    }
    pub(crate) fn attempted(&mut self, id: &str) {
        if let Some(row) = self.active.get_mut(id) {
            row.phase = Phase::Attempted;
        }
    }
    pub(crate) fn accepted(&mut self, id: &str, confirmed_ns: u64) {
        if let Some(row) = self.active.get_mut(id) {
            let earlier = match row.phase {
                Phase::Working { confirmed_ns } => confirmed_ns,
                _ => 0,
            };
            row.phase = Phase::Working {
                confirmed_ns: confirmed_ns.max(earlier),
            };
        }
    }
    pub(crate) fn retire(&mut self, id: &str, confirmed_ns: u64) {
        let Some(row) = self.active.remove(id) else {
            return;
        };
        let earlier = match row.phase {
            Phase::Working { confirmed_ns } => confirmed_ns,
            _ => 0,
        };
        let confirmed_ns = confirmed_ns.max(earlier);
        if confirmed_ns > 0 && self.frontier_ns >= confirmed_ns {
            return;
        }
        self.retired
            .entry(row.symbol)
            .and_modify(|old| {
                old.quantity = old
                    .quantity
                    .as_ref()
                    .zip(row.quantity.as_ref())
                    .map(|(a, b)| a + b);
                old.px = max_price(old.px.take(), row.px.clone());
                old.confirmed_ns = if old.confirmed_ns == 0 || confirmed_ns == 0 {
                    0
                } else {
                    old.confirmed_ns.max(confirmed_ns)
                };
            })
            .or_insert(Retired {
                quantity: row.quantity,
                px: row.px,
                confirmed_ns,
            });
    }
    pub(crate) fn observe(&mut self, ns: u64) {
        self.frontier_ns = self.frontier_ns.max(ns);
        self.retired
            .retain(|_, row| row.confirmed_ns == 0 || row.confirmed_ns > self.frontier_ns);
    }
    pub(crate) fn required(
        &self,
        ns: u64,
        leverage: &Exact,
        price: impl Fn(SymbolId) -> Option<Exact>,
    ) -> Result<Exact, &'static str> {
        if ns < self.frontier_ns {
            return Err("account query predates the margin confirmation frontier");
        }
        let mut quantities = BTreeMap::<Exact, ExactSum>::new();
        let mut add =
            |symbol, qty: &Option<Exact>, px: &Option<Exact>| -> Result<(), &'static str> {
                let qty = qty
                    .as_ref()
                    .ok_or("an order's incremental physical margin is unknown")?;
                if qty.is_negative() {
                    return Err("an order's margin quantity is unreadable");
                }
                if qty.is_zero() {
                    return Ok(());
                }
                let px = max_price(px.clone(), price(symbol))
                    .filter(Exact::is_positive)
                    .ok_or("no price for unreflected order margin")?;
                if leverage.is_zero() {
                    return Err("unreadable leverage");
                }
                *quantities.entry(px).or_default() += qty;
                Ok(())
            };
        for row in self.active.values() {
            if matches!(row.phase,Phase::Working{confirmed_ns} if confirmed_ns>0&&ns>=confirmed_ns)
            {
                continue;
            }
            add(row.symbol, &row.quantity, &row.px)?;
        }
        for (symbol, row) in &self.retired {
            if row.confirmed_ns > 0 && ns >= row.confirmed_ns {
                continue;
            }
            add(*symbol, &row.quantity, &row.px)?;
        }
        let mut total = Exact::zero();
        for (px, qty) in quantities {
            total += qty.finish() * px;
        }
        if total.is_zero() {
            Ok(total)
        } else {
            total
                .checked_div(leverage)
                .map_err(|_| "unreadable leverage")
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn reference_required(
        book: &MarginBook,
        ns: u64,
        leverage: &Exact,
        price: impl Fn(SymbolId) -> Option<Exact>,
    ) -> Result<Exact, &'static str> {
        if ns < book.frontier_ns {
            return Err("account query predates the margin confirmation frontier");
        }
        let mut total = Exact::zero();
        let mut add =
            |symbol, qty: &Option<Exact>, px: &Option<Exact>| -> Result<(), &'static str> {
                let qty = qty
                    .as_ref()
                    .ok_or("an order's incremental physical margin is unknown")?;
                if qty.is_negative() {
                    return Err("an order's margin quantity is unreadable");
                }
                if qty.is_zero() {
                    return Ok(());
                }
                let px = max_price(px.clone(), price(symbol))
                    .filter(Exact::is_positive)
                    .ok_or("no price for unreflected order margin")?;
                total += (qty * px)
                    .checked_div(leverage)
                    .map_err(|_| "unreadable leverage")?;
                Ok(())
            };
        for row in book.active.values() {
            if matches!(row.phase,Phase::Working{confirmed_ns} if confirmed_ns>0&&ns>=confirmed_ns)
            {
                continue;
            }
            add(row.symbol, &row.quantity, &row.px)?;
        }
        for (symbol, row) in &book.retired {
            if row.confirmed_ns > 0 && ns >= row.confirmed_ns {
                continue;
            }
            add(*symbol, &row.quantity, &row.px)?;
        }
        Ok(total)
    }
    #[test]
    fn margin_matches_rowwise_reference_across_lifecycle_and_price_changes() {
        let mut book = MarginBook::default();
        let mut seed = 0x918b_447a_6c31_702du64;
        for step in 0..1024u64 {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            let id = format!("order-{}", seed % 37);
            let symbol = SymbolId((seed % 7) as u16);
            let quantity = (!seed.is_multiple_of(17))
                .then(|| Exact::from_ratio(&(seed % 1000).to_string(), "37").unwrap());
            let price = (!seed.is_multiple_of(19))
                .then(|| Exact::from_ratio(&(seed % 10000 + 1).to_string(), "13").unwrap());
            match seed % 7 {
                0 | 1 => book.register(&id, symbol, quantity, price),
                2 => book.set_quantity(&id, quantity),
                3 => book.accepted(&id, step),
                4 => book.attempted(&id),
                5 => book.retire(&id, step),
                _ => book.observe(step.saturating_sub(3)),
            }
            let removed = if step.is_multiple_of(11) {
                book.take(&id)
            } else {
                None
            };
            for ns in [0, step.saturating_sub(7), step + 1] {
                for leverage in [
                    Exact::zero(),
                    Exact::from_i64(-2),
                    Exact::from_ratio("17", "3").unwrap(),
                ] {
                    let quote = |symbol: SymbolId| {
                        if step.is_multiple_of(4) {
                            return Some(Exact::from_u64(10000));
                        }
                        (!(step + u64::from(symbol.0)).is_multiple_of(5)).then(|| {
                            Exact::from_ratio(&(step + u64::from(symbol.0) + 1).to_string(), "7")
                                .unwrap()
                        })
                    };
                    assert_eq!(
                        book.required(ns, &leverage, quote),
                        reference_required(&book, ns, &leverage, quote),
                        "step {step}, query {ns}, leverage {leverage}"
                    );
                }
            }
            book.restore(&id, removed);
        }
    }
    #[test]
    fn terminal_holds_compact_by_symbol_and_only_a_later_scan_releases_them() {
        let mut margin = MarginBook::default();
        for i in 0..10000 {
            let id = i.to_string();
            margin.register(
                &id,
                SymbolId(1),
                Some(Exact::one()),
                Some(Exact::from_u64(10)),
            );
            margin.retire(&id, i + 1);
        }
        assert!(margin.active.is_empty());
        assert_eq!(margin.retired.len(), 1);
        assert_eq!(
            margin.required(1, &Exact::from_u64(2), |_| None),
            Ok(Exact::from_u64(50000))
        );
        margin.observe(9999);
        assert_eq!(margin.retired.len(), 1);
        margin.observe(10000);
        assert!(margin.retired.is_empty());
    }
    #[test]
    fn zero_confirmation_is_not_an_acceptance_receipt() {
        let mut margin = MarginBook::default();
        margin.register(
            "a",
            SymbolId(1),
            Some(Exact::one()),
            Some(Exact::from_u64(10)),
        );
        margin.accepted("a", 0);
        assert_eq!(
            margin.required(1000, &Exact::from_u64(2), |_| None),
            Ok(Exact::from_u64(5))
        );
        margin.retire("a", 0);
        margin.observe(2000);
        assert_eq!(
            margin.required(2000, &Exact::from_u64(2), |_| None),
            Ok(Exact::from_u64(5))
        );
    }
    #[test]
    fn unreadable_reservation_never_becomes_free_margin() {
        for qty in [None, Some(Exact::from_i64(-1))] {
            let mut margin = MarginBook::default();
            margin.register("a", SymbolId(1), qty, Some(Exact::from_u64(10)));
            assert!(margin.required(1, &Exact::from_u64(2), |_| None).is_err());
        }
    }
}
