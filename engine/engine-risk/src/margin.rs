use std::collections::BTreeMap;

use engine_types::ids::SymbolId;

#[derive(Clone, Copy, Debug)]
enum Phase {
    Queued,
    Attempted,
    Working { confirmed_ns: u64 },
}

#[derive(Clone, Debug)]
pub(crate) struct Reservation {
    symbol: SymbolId,
    quantity: Option<f64>,
    px: f64,
    phase: Phase,
}

#[derive(Debug)]
struct Retired {
    quantity: Option<f64>,
    px: f64,
    confirmed_ns: u64,
}

/// Exposure reservations end at a fill/cancel. Cached free margin may still
/// predate that event, so these holds end at a later account query's start.
#[derive(Debug, Default)]
pub(crate) struct MarginBook {
    // Ordered, because `required` sums these and the sum reaches the log:
    // hash order would make the verdict depend on the hash seed.
    active: BTreeMap<String, Reservation>,
    retired: BTreeMap<SymbolId, Retired>,
    frontier_ns: u64,
}

impl MarginBook {
    pub(crate) fn register(&mut self, id: &str, symbol: SymbolId, quantity: Option<f64>, px: f64) {
        self.active.insert(
            id.to_owned(),
            Reservation {
                symbol,
                quantity,
                px,
                phase: Phase::Queued,
            },
        );
    }

    pub(crate) fn set_quantity(&mut self, id: &str, quantity: Option<f64>) {
        if let Some(row) = self.active.get_mut(id) {
            row.quantity = quantity;
        }
    }

    pub(crate) fn take(&mut self, id: &str) -> Option<Reservation> {
        self.active.remove(id)
    }

    pub(crate) fn restore(&mut self, id: &str, row: Option<Reservation>) {
        if let Some(row) = row {
            self.active.insert(id.to_owned(), row);
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
            .and_modify(|retired| {
                retired.quantity = retired
                    .quantity
                    .zip(row.quantity)
                    .map(|(left, right)| left + right);
                retired.px = retired.px.max(row.px);
                retired.confirmed_ns = if retired.confirmed_ns == 0 || confirmed_ns == 0 {
                    0
                } else {
                    retired.confirmed_ns.max(confirmed_ns)
                };
            })
            .or_insert(Retired {
                quantity: row.quantity,
                px: row.px,
                confirmed_ns,
            });
    }

    pub(crate) fn observe(&mut self, query_started_ns: u64) {
        self.frontier_ns = self.frontier_ns.max(query_started_ns);
        self.retired
            .retain(|_, row| row.confirmed_ns == 0 || row.confirmed_ns > self.frontier_ns);
    }

    pub(crate) fn required(
        &self,
        query_started_ns: u64,
        leverage: f64,
        price: impl Fn(SymbolId) -> Option<f64>,
    ) -> Result<f64, &'static str> {
        if query_started_ns < self.frontier_ns {
            return Err("account query predates the margin confirmation frontier");
        }
        let mut total = 0.0;
        let mut add = |symbol, quantity: Option<f64>, px: f64| -> Result<(), &'static str> {
            let quantity = quantity.ok_or("an order's incremental physical margin is unknown")?;
            if !quantity.is_finite() || quantity < 0.0 {
                return Err("an order's margin quantity is unreadable");
            }
            if quantity == 0.0 {
                return Ok(());
            }
            let current = price(symbol).unwrap_or(0.0);
            let px = px.max(current);
            if !px.is_finite() || px <= 0.0 {
                return Err("no price for unreflected order margin");
            }
            total += quantity * px / leverage;
            if !total.is_finite() {
                return Err("unreflected order margin is unreadable");
            }
            Ok(())
        };
        for row in self.active.values() {
            if matches!(row.phase, Phase::Working { confirmed_ns } if confirmed_ns > 0 && query_started_ns >= confirmed_ns)
            {
                continue;
            }
            add(row.symbol, row.quantity, row.px)?;
        }
        for (symbol, row) in &self.retired {
            if row.confirmed_ns > 0 && query_started_ns >= row.confirmed_ns {
                continue;
            }
            add(*symbol, row.quantity, row.px)?;
        }
        Ok(total)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_holds_compact_by_symbol_and_only_a_later_scan_releases_them() {
        let mut margin = MarginBook::default();
        for i in 0..10_000 {
            let id = i.to_string();
            margin.register(&id, SymbolId(1), Some(1.0), 10.0);
            margin.retire(&id, i + 1);
        }
        assert!(margin.active.is_empty());
        assert_eq!(margin.retired.len(), 1);
        assert_eq!(margin.required(1, 2.0, |_| None), Ok(50_000.0));
        margin.observe(9_999);
        assert_eq!(margin.retired.len(), 1);
        margin.observe(10_000);
        assert!(margin.retired.is_empty());
        assert_eq!(margin.required(10_000, 2.0, |_| None), Ok(0.0));
    }

    #[test]
    fn zero_confirmation_is_not_an_acceptance_receipt() {
        let mut margin = MarginBook::default();
        margin.register("a", SymbolId(1), Some(1.0), 10.0);
        margin.accepted("a", 0);
        assert_eq!(margin.required(1_000, 2.0, |_| None), Ok(5.0));
        margin.retire("a", 0);
        margin.observe(2_000);
        assert_eq!(margin.required(2_000, 2.0, |_| None), Ok(5.0));
    }

    #[test]
    fn unreadable_reservation_never_becomes_free_margin() {
        for quantity in [None, Some(f64::INFINITY), Some(f64::NAN), Some(-1.0)] {
            let mut margin = MarginBook::default();
            margin.register("a", SymbolId(1), quantity, 10.0);
            assert!(margin.required(1, 2.0, |_| None).is_err());
        }
    }
}
