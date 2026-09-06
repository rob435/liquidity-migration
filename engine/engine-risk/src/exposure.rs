//! Reservations and fills newer than the causal account snapshot.
use engine_types::ids::{StrategyId, SymbolId};
use engine_types::numeric::Exact;
use engine_types::orders::Side;
use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub(crate) struct Pending {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub signed_qty: Option<Exact>,
    pub reduce_only: bool,
    pub px: Option<Exact>,
    pub stop_fraction: Option<Exact>,
}
#[derive(Clone, Debug)]
pub(crate) struct RecentExposure {
    pub signed_qty: Exact,
    pub stop_fraction: Option<Exact>,
}
#[derive(Debug, Default)]
pub(crate) struct Book {
    px: BTreeMap<u16, Exact>,
    pending: BTreeMap<String, Pending>,
    recent_fills: Vec<(u64, u16, Option<Exact>, Option<Exact>)>,
}
impl Book {
    pub(crate) fn observe_px(&mut self, symbol: SymbolId, px: f64) {
        if let Ok(px) = Exact::from_legacy_f64(px) {
            self.observe_exact_px(symbol, px);
        }
    }
    pub(crate) fn observe_exact_px(&mut self, symbol: SymbolId, px: Exact) {
        if px.is_positive() {
            self.px.insert(symbol.0, px);
        }
    }
    pub(crate) fn px(&self, symbol: SymbolId) -> Option<Exact> {
        self.px.get(&symbol.0).cloned()
    }
    pub(crate) fn register(&mut self, id: &str, pending: Pending) {
        self.pending.insert(id.into(), pending);
    }
    pub(crate) fn take(&mut self, id: &str) -> Option<Pending> {
        self.pending.remove(id)
    }
    pub(crate) fn contains(&self, id: &str) -> bool {
        self.pending.contains_key(id)
    }
    pub(crate) fn forget(&mut self, id: &str) {
        self.pending.remove(id);
    }
    pub(crate) fn on_fill_with_remaining(
        &mut self,
        id: &str,
        symbol: SymbolId,
        qty: Option<Exact>,
        recv_ns: u64,
        remaining: Option<Exact>,
    ) {
        let stop = self.pending.get(id).and_then(|p| p.stop_fraction.clone());
        self.recent_fills
            .push((recv_ns, symbol.0, qty.clone(), stop));
        let Some(pending) = self.pending.get_mut(id) else {
            return;
        };
        pending.signed_qty = pending.signed_qty.as_ref().and_then(|old| {
            let filled = qty.as_ref()?;
            let left = remaining.unwrap_or_else(|| old.abs() - filled.abs());
            Some(if left.is_positive() {
                if old.is_negative() {
                    -left
                } else {
                    left
                }
            } else {
                Exact::zero()
            })
        });
        if pending.signed_qty.as_ref().is_some_and(Exact::is_zero) {
            self.pending.remove(id);
        }
    }
    fn quantity(&self, filter: impl Fn(&Pending) -> bool) -> Result<Exact, &'static str> {
        self.pending
            .values()
            .filter(|p| filter(p))
            .try_fold(Exact::zero(), |sum, p| {
                Ok(sum
                    + p.signed_qty
                        .as_ref()
                        .ok_or("pending order quantity is unknown")?
                        .abs())
            })
    }
    pub(crate) fn pending_reduce_qty(&self, symbol: SymbolId) -> Result<Exact, &'static str> {
        self.quantity(|p| p.reduce_only && p.symbol == symbol)
    }
    pub(crate) fn owned_reduce_qty(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
    ) -> Result<Exact, &'static str> {
        self.quantity(|p| p.reduce_only && p.strategy == strategy && p.symbol == symbol)
    }
    pub(crate) fn owned_open_qty(
        &self,
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
    ) -> Result<Exact, &'static str> {
        self.quantity(|p| {
            !p.reduce_only
                && p.strategy == strategy
                && p.symbol == symbol
                && p.signed_qty
                    .as_ref()
                    .is_none_or(|q| q.is_negative() == (side == Side::Sell))
        })
    }
    pub(crate) fn pending_open_qty(
        &self,
        symbol: SymbolId,
        side: Side,
    ) -> Result<Exact, &'static str> {
        self.quantity(|p| {
            !p.reduce_only
                && p.symbol == symbol
                && p.signed_qty
                    .as_ref()
                    .is_none_or(|q| q.is_negative() == (side == Side::Sell))
        })
    }
    pub(crate) fn physical_interval(
        &self,
        symbol: SymbolId,
        settled: &Exact,
    ) -> Result<(Exact, Exact), &'static str> {
        self.pending
            .values()
            .filter(|p| p.symbol == symbol)
            .try_fold(
                (settled.clone(), settled.clone()),
                |(mut low, mut high), p| {
                    let qty = p
                        .signed_qty
                        .as_ref()
                        .ok_or("pending physical quantity is unknown")?;
                    if qty.is_negative() {
                        low += qty;
                    } else {
                        high += qty;
                    }
                    Ok((low, high))
                },
            )
    }
    pub(crate) fn fills_after(
        &mut self,
        observed_ns: u64,
    ) -> Result<BTreeMap<u16, RecentExposure>, &'static str> {
        self.prune_through(observed_ns);
        let mut net = BTreeMap::<u16, RecentExposure>::new();
        for (_, symbol, qty, stop) in &self.recent_fills {
            let qty = qty.as_ref().ok_or("recent execution quantity is unknown")?;
            let row = net.entry(*symbol).or_insert_with(|| RecentExposure {
                signed_qty: Exact::zero(),
                stop_fraction: Some(Exact::zero()),
            });
            row.signed_qty += qty;
            row.stop_fraction = row
                .stop_fraction
                .as_ref()
                .zip(stop.as_ref())
                .map(|(a, b)| a.max(b).clone());
        }
        Ok(net)
    }
    pub(crate) fn prune_through(&mut self, observed_ns: u64) {
        self.recent_fills.retain(|(ns, _, _, _)| *ns > observed_ns);
    }
    pub(crate) fn pending_risk_rows(
        &self,
        price: impl Fn(SymbolId) -> Option<Exact>,
    ) -> Result<Vec<(u16, Exact, Exact)>, &'static str> {
        let mut out = Vec::new();
        for pending in self.pending.values() {
            let qty = pending
                .signed_qty
                .as_ref()
                .ok_or("pending order quantity is unknown")?;
            if pending.reduce_only || qty.is_zero() {
                continue;
            }
            let fraction = pending
                .stop_fraction
                .clone()
                .ok_or("an in-flight opening order has no readable stop distance")?;
            let px = match (price(pending.symbol), pending.px.as_ref()) {
                (Some(a), Some(b)) => a.max(b.clone()),
                (Some(a), None) => a,
                (None, Some(b)) => b.clone(),
                (None, None) => return Err("no price for an in-flight opening order"),
            };
            out.push((pending.symbol.0, qty.abs() * px, fraction));
        }
        Ok(out)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn entry(symbol: u16, qty: i64) -> Pending {
        Pending {
            strategy: StrategyId(0),
            symbol: SymbolId(symbol),
            signed_qty: Some(Exact::from_i64(qty)),
            reduce_only: false,
            px: Some(Exact::from_u64(10)),
            stop_fraction: Some(Exact::parse_decimal("0.1").unwrap()),
        }
    }
    #[test]
    fn risk_rows_and_recent_fills_come_back_in_key_order() {
        let mut book = Book::default();
        for id in ["z-9", "a-1", "m-5", "b-2"] {
            book.register(id, entry(7, 1));
        }
        assert_eq!(
            book.pending.keys().map(String::as_str).collect::<Vec<_>>(),
            ["a-1", "b-2", "m-5", "z-9"]
        );
        assert_eq!(
            book.pending_risk_rows(|_| Some(Exact::from_u64(10)))
                .unwrap()
                .len(),
            4
        );
        for symbol in [9, 2, 5, 1] {
            book.on_fill_with_remaining("unknown", SymbolId(symbol), Some(Exact::one()), 100, None);
        }
        assert_eq!(
            book.fills_after(0)
                .unwrap()
                .keys()
                .copied()
                .collect::<Vec<_>>(),
            [1, 2, 5, 9]
        );
    }
    #[test]
    fn a_used_up_reservation_leaves_the_pending_map() {
        let mut book = Book::default();
        book.register("a-1", entry(4, 3));
        book.on_fill_with_remaining("a-1", SymbolId(4), Some(Exact::from_u64(2)), 1, None);
        assert_eq!(book.pending.len(), 1);
        book.on_fill_with_remaining("a-1", SymbolId(4), Some(Exact::one()), 2, None);
        assert!(book.pending.is_empty());
        assert_eq!(
            book.fills_after(0).unwrap()[&4].signed_qty,
            Exact::from_u64(3)
        );
    }
    #[test]
    fn a_fill_during_an_account_scan_survives_the_scan_start_stamp() {
        let mut book = Book::default();
        book.register("race-1", entry(7, 2));
        book.on_fill_with_remaining("race-1", SymbolId(7), Some(Exact::from_u64(2)), 150, None);
        assert_eq!(
            book.fills_after(100).unwrap()[&7].signed_qty,
            Exact::from_u64(2)
        );
        assert!(book.fills_after(200).unwrap().is_empty());
    }
}
