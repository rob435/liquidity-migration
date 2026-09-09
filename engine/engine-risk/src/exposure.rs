//! Reservations and fills newer than the causal account snapshot.
use engine_types::ids::{StrategyId, SymbolId};
use engine_types::numeric::{Exact, ExactSum};
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
struct PendingInterval {
    rows: usize,
    unknown: usize,
    low: Exact,
    high: Exact,
}
impl PendingInterval {
    fn insert(&mut self, pending: &Pending) {
        self.rows += 1;
        match &pending.signed_qty {
            Some(qty) if qty.is_negative() => self.low += qty,
            Some(qty) => self.high += qty,
            None => self.unknown += 1,
        }
    }
    fn remove(&mut self, pending: &Pending) {
        self.rows -= 1;
        match &pending.signed_qty {
            Some(qty) if qty.is_negative() => self.low -= qty,
            Some(qty) => self.high -= qty,
            None => self.unknown -= 1,
        }
    }
}
#[derive(Debug, Default)]
pub(crate) struct Book {
    px: BTreeMap<u16, Exact>,
    pending: BTreeMap<String, Pending>,
    pending_by_symbol: BTreeMap<SymbolId, PendingInterval>,
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
    pub(crate) fn px(&self, symbol: SymbolId) -> Option<&Exact> {
        self.px.get(&symbol.0)
    }
    pub(crate) fn register(&mut self, id: &str, pending: Pending) {
        self.forget(id);
        self.pending_by_symbol
            .entry(pending.symbol)
            .or_default()
            .insert(&pending);
        self.pending.insert(id.into(), pending);
    }
    pub(crate) fn take(&mut self, id: &str) -> Option<Pending> {
        let pending = self.pending.remove(id)?;
        let interval = self
            .pending_by_symbol
            .get_mut(&pending.symbol)
            .expect("registered pending symbol");
        interval.remove(&pending);
        if interval.rows == 0 {
            self.pending_by_symbol.remove(&pending.symbol);
        }
        Some(pending)
    }
    pub(crate) fn contains(&self, id: &str) -> bool {
        self.pending.contains_key(id)
    }
    pub(crate) fn forget(&mut self, id: &str) {
        self.take(id);
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
        let Some(mut pending) = self.take(id) else {
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
        if !pending.signed_qty.as_ref().is_some_and(Exact::is_zero) {
            self.register(id, pending);
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
    pub(crate) fn pending_symbol_notional(
        &self,
        symbol: SymbolId,
        price: &Exact,
    ) -> Result<Exact, &'static str> {
        self.pending
            .values()
            .filter(|p| p.symbol == symbol && !p.reduce_only)
            .try_fold(Exact::zero(), |sum, p| {
                Ok(sum
                    + p.signed_qty
                        .as_ref()
                        .ok_or("pending order quantity is unknown")?
                        .abs()
                        * p.px
                            .as_ref()
                            .ok_or("pending order price is unknown")?
                            .max(price))
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
        match self.pending_by_symbol.get(&symbol) {
            Some(interval) if interval.unknown > 0 => Err("pending physical quantity is unknown"),
            Some(interval) => Ok((settled + &interval.low, settled + &interval.high)),
            None => Ok((settled.clone(), settled.clone())),
        }
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
    pub(crate) fn pending_risk_rows<'a>(
        &'a self,
        price: impl Fn(SymbolId) -> Option<&'a Exact>,
    ) -> Result<Vec<(Exact, Exact)>, &'static str> {
        let mut quantities = BTreeMap::<(&Exact, &Exact), ExactSum>::new();
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
                .as_ref()
                .ok_or("an in-flight opening order has no readable stop distance")?;
            let px = match (price(pending.symbol), pending.px.as_ref()) {
                (Some(a), Some(b)) => a.max(b),
                (Some(a), None) => a,
                (None, Some(b)) => b,
                (None, None) => return Err("no price for an in-flight opening order"),
            };
            let total = quantities.entry((px, fraction)).or_default();
            if qty.is_negative() {
                *total += &-qty;
            } else {
                *total += qty;
            }
        }
        Ok(quantities
            .into_iter()
            .map(|((px, fraction), qty)| (qty.finish() * px, fraction.clone()))
            .collect())
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
    fn reference_pending_risk_rows(
        book: &Book,
        price: impl Fn(SymbolId) -> Option<Exact>,
    ) -> Result<Vec<(u16, Exact, Exact)>, &'static str> {
        let mut out = Vec::new();
        for pending in book.pending.values() {
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
    fn reference_interval(
        book: &Book,
        symbol: SymbolId,
        settled: &Exact,
    ) -> Result<(Exact, Exact), &'static str> {
        book.pending
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

    fn assert_intervals(book: &Book, step: u64) {
        for symbol in (0..9).map(SymbolId) {
            let settled = Exact::from_ratio(&(step as i64 - 2000).to_string(), "37").unwrap();
            let actual = book.physical_interval(symbol, &settled);
            let expected = reference_interval(book, symbol, &settled);
            assert_eq!(actual, expected, "step {step}, symbol {symbol:?}");
            assert_eq!(
                serde_json::to_vec(&actual).unwrap(),
                serde_json::to_vec(&expected).unwrap(),
                "canonical bytes at step {step}, symbol {symbol:?}"
            );
        }
    }

    #[test]
    fn physical_intervals_match_rowwise_reference_across_reservation_lifecycle() {
        let mut book = Book::default();
        let mut seed = 0x4791_ab63_f527_8e2du64;
        for step in 0..4096u64 {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            let id = format!("order-{}", seed % 61);
            let symbol = SymbolId(((seed >> 8) % 8) as u16);
            let qty = match (seed >> 16) % 13 {
                0 => None,
                1 => Some(Exact::zero()),
                2 => Some(Exact::parse_decimal("-1e200").unwrap()),
                3 => Some(Exact::parse_decimal("1e-200").unwrap()),
                _ => Some(
                    Exact::from_ratio(
                        &(((seed >> 24) % 2001) as i64 - 1000).to_string(),
                        &((seed >> 40) % 101 + 1).to_string(),
                    )
                    .unwrap(),
                ),
            };
            match (seed >> 48) % 7 {
                0..=2 => {
                    let mut pending = entry(symbol.0, 1);
                    pending.signed_qty = qty.clone();
                    pending.strategy = StrategyId(((seed >> 56) % 3) as u16);
                    pending.reduce_only = seed & 1 == 0;
                    book.register(&id, pending);
                }
                3 | 4 => book.on_fill_with_remaining(
                    &id,
                    symbol,
                    qty.clone(),
                    step,
                    (seed & 2 == 0).then(|| Exact::from_ratio("2", "7").unwrap()),
                ),
                5 => book.forget(&id),
                _ => {
                    let previous = book.take(&id);
                    assert_intervals(&book, step);
                    if let Some(previous) = previous {
                        book.register(&id, previous);
                    }
                }
            }
            book.observe_exact_px(symbol, Exact::from_u64(step + 1));
            book.prune_through(step.saturating_sub(3));
            assert_intervals(&book, step);
            if step.is_multiple_of(97) {
                let mut restored = Book::default();
                for (id, pending) in &book.pending {
                    restored.register(id, pending.clone());
                }
                assert_intervals(&restored, step);
                for symbol in (0..9).map(SymbolId) {
                    assert_eq!(
                        restored.physical_interval(symbol, &Exact::one()),
                        book.physical_interval(symbol, &Exact::one())
                    );
                }
            }
        }
        for id in book.pending.keys().cloned().collect::<Vec<_>>() {
            book.forget(&id);
        }
        assert_intervals(&book, 4096);
    }
    #[test]
    fn pending_valuation_matches_original_rows_and_price_read_order() {
        use crate::config::EnvelopeConfig;
        use crate::envelope::Envelope;
        use std::cell::RefCell;
        let envelope = Envelope::new(EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: 1000.0,
            equity_fraction: 1.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: 10.0,
            disaster_stop_fraction: 0.35,
            max_component_gross_notional_usdt: 10000.0,
            max_symbol_notional_usdt: 10000.0,
            max_initial_margin_usdt: 1000.0,
        });
        let mut book = Book::default();
        for step in 0..512u64 {
            let id = format!("order-{}", step % 29);
            let mut pending = entry((step % 7) as u16, 1);
            pending.signed_qty = (!step.is_multiple_of(37))
                .then(|| Exact::from_ratio(&(step as i64 - 256).to_string(), "37").unwrap());
            pending.reduce_only = step.is_multiple_of(11);
            pending.px = (!step.is_multiple_of(13))
                .then(|| Exact::from_ratio(&(step % 17 + 1).to_string(), "3").unwrap());
            pending.stop_fraction = (!step.is_multiple_of(19)).then(|| {
                Exact::parse_decimal(
                    ["0.1", "0.35", "0.35000000000000000000001", "0.7"][(step % 4) as usize],
                )
                .unwrap()
            });
            book.register(&id, pending);
            if step.is_multiple_of(3) {
                book.forget(&format!("order-{}", (step + 7) % 29));
            }
            if step.is_multiple_of(5) {
                book.on_fill_with_remaining(
                    &id,
                    SymbolId((step % 7) as u16),
                    Some(Exact::parse_decimal("0.1").unwrap()),
                    step,
                    None,
                );
            }
            let removed = step.is_multiple_of(7).then(|| book.take(&id)).flatten();
            for query in 0..3 {
                let actual_reads = RefCell::new(Vec::new());
                let expected_reads = RefCell::new(Vec::new());
                let quotes: BTreeMap<_, _> = (0..7)
                    .filter_map(|symbol| {
                        let price = match query {
                            0 => return None,
                            1 => Exact::from_u64(10000),
                            _ => Exact::from_ratio(&(step + u64::from(symbol)).to_string(), "7")
                                .unwrap(),
                        };
                        Some((SymbolId(symbol), price))
                    })
                    .collect();
                let price = |symbol: SymbolId| quotes.get(&symbol);
                let actual = book
                    .pending_risk_rows(|symbol| {
                        actual_reads.borrow_mut().push(symbol);
                        price(symbol)
                    })
                    .map(|rows| envelope.pending_totals(&rows));
                let expected = reference_pending_risk_rows(&book, |symbol| {
                    expected_reads.borrow_mut().push(symbol);
                    price(symbol).cloned()
                })
                .map(|rows| {
                    rows.iter().fold(
                        (Exact::zero(), Exact::zero()),
                        |(gross, loss), (_, notional, fraction)| {
                            (
                                gross + notional,
                                loss + envelope.modelled_stop_charge_usdt(notional, fraction),
                            )
                        },
                    )
                });
                assert_eq!(actual, expected, "step {step}, query {query}");
                assert_eq!(
                    serde_json::to_vec(&actual).unwrap(),
                    serde_json::to_vec(&expected).unwrap()
                );
                assert_eq!(actual_reads.into_inner(), expected_reads.into_inner());
            }
            if let Some(previous) = removed {
                book.register(&id, previous);
            }
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
        let price = Exact::from_u64(10);
        assert_eq!(
            book.pending_risk_rows(|_| Some(&price))
                .unwrap()
                .into_iter()
                .fold(Exact::zero(), |sum, (notional, _)| sum + notional),
            Exact::from_u64(40)
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
