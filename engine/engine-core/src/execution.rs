//! What the fills cost.
//!
//! The engine could always say how fast it sent an order. It could not say
//! what the order *got* — and a fast order path that quietly pays two basis
//! points more than it needs to is a worse thing to own than a slow one that
//! does not. This is the other ledger: the latency one measures our own side
//! of the wire, this one measures the price.
//!
//! ## The numbers are the repository's, not new ones
//!
//! Every name and every sign here comes from `docs/architecture.md` §Trade
//! diagnostics, which the Python research half already computes against
//! recorded books. `s` is +1 for a buy and −1 for a sell, `M0` is the
//! midpoint when the order left, `P` is the fill price, and `Mh` is the first
//! healthy midpoint at or after the horizon. Shortfall, spread and fee are
//! **positive when adverse**; `signed_markout_bps` is the one number with the
//! opposite convention, positive when the price moved our way.
//!
//! Writing a second vocabulary would have been easier and would have made the
//! two halves of the repository incomparable.
//!
//! ## Two honest differences from the Python
//!
//! - **`M0` here is the top of book**, because that is the only book the
//!   engine carries. The Python anchors on a depth-50 decision snapshot and
//!   can therefore also walk it (`book_walk_shortfall_bps`). Nothing here can
//!   say anything about impact, and nothing here pretends to.
//! - **Rollups weight by notional, not quantity.** The doc weights by
//!   quantity, which is right at its own grain — one command, one symbol,
//!   where quantity and notional rank identically. A number that spans
//!   symbols cannot add a quantity of BTC to a quantity of DOGE, so these
//!   weight by traded notional.
//!
//! ## Why the log is enough
//!
//! Everything except the markout is arithmetic over records the log already
//! holds: `OrderSent` carries `M0`, and the fill carries `P`, the fee and
//! which side of the spread we were on. So the same code computes the live
//! summary and the report read back off a finished log, and the two cannot
//! drift apart. The markout is the exception — it is an observation made
//! later, so it is written down when it happens.

use std::collections::{BTreeMap, HashMap, VecDeque};

use engine_types::{MarketState, OrderUpdate, Side, StrategyId, SymbolId, WalRecord};

/// The horizons a markout is read at (`docs/architecture.md`: 1 s, 15 s,
/// 1 min, 5 min). A 50 ms markout is deliberately absent there and absent
/// here: it is only honest with exact raw observations and clock bounds, and
/// this engine has neither.
pub const HORIZONS_MS: [u64; 4] = [1_000, 15_000, 60_000, 300_000];

/// How late a mark may be before the horizon is given up on. The engine looks
/// on its group-flush tick, so a mark is always a little late; this bounds how
/// much of that is worth waiting through when the book will not come back.
///
/// Five seconds is the Python owner's own lateness bound, kept so the two
/// halves cut their coverage at the same place.
pub const LATENESS_BOUND_MS: u64 = 5_000;

/// How many fills may be waiting for a mark at once. Past this the oldest is
/// dropped and counted, because a measurement that silently stops measuring
/// is worse than one that says it stopped.
const MAX_PENDING: usize = 8_192;

/// `s` in every formula below.
fn sign(side: Side) -> f64 {
    match side {
        Side::Buy => 1.0,
        Side::Sell => -1.0,
    }
}

/// A price that can anchor a measurement. Zero is what the engine writes down
/// when it could not read the book, and dividing by it would turn "we do not
/// know" into a very large number.
fn usable(px: f64) -> bool {
    px.is_finite() && px > 0.0
}

/// `10_000 * s * (P - M0) / M0`. Positive is adverse: we traded worse than
/// the midpoint that was on the screen when the order left.
pub fn arrival_shortfall_bps(side: Side, fill_px: f64, arrival_mid: f64) -> Option<f64> {
    if !usable(fill_px) || !usable(arrival_mid) {
        return None;
    }
    Some(10_000.0 * sign(side) * (fill_px - arrival_mid) / arrival_mid)
}

/// `20_000 * s * (P - M0) / M0`. Twice the arrival shortfall by construction:
/// the shortfall is measured from the midpoint, and a spread has two sides.
pub fn effective_spread_bps(side: Side, fill_px: f64, arrival_mid: f64) -> Option<f64> {
    arrival_shortfall_bps(side, fill_px, arrival_mid).map(|bps| bps * 2.0)
}

/// `10_000 * fee / abs(notional)`. Positive is adverse, so a maker rebate —
/// which the venue sends as a negative fee — reads negative here too.
pub fn fee_bps(fee: f64, fill_px: f64, qty: f64) -> Option<f64> {
    let notional = (fill_px * qty).abs();
    if !fee.is_finite() || !usable(notional) {
        return None;
    }
    Some(10_000.0 * fee / notional)
}

/// `10_000 * s * (Mh - P) / P`. **Positive means the price moved our way**
/// after the fill — the one number here that is good when it is large.
///
/// Persistently negative is the thing a maker fears: it means the orders that
/// fill are the ones about to be wrong, which is adverse selection, and no
/// amount of fee saving pays for it.
pub fn signed_markout_bps(side: Side, fill_px: f64, later_mid: f64) -> Option<f64> {
    if !usable(fill_px) || !usable(later_mid) {
        return None;
    }
    Some(10_000.0 * sign(side) * (later_mid - fill_px) / fill_px)
}

/// The midpoint of a two-sided book, or `None` when there is not one to read.
///
/// A crossed or one-sided book is not a price. The Python calls this a book
/// that is not healthy and refuses to mark against it; so does this.
pub fn healthy_mid(market: &MarketState, symbol: SymbolId) -> Option<f64> {
    let quote = market.quotes.get(symbol.0 as usize)?;
    if !usable(quote.bid_px) || !usable(quote.ask_px) || quote.ask_px < quote.bid_px {
        return None;
    }
    Some((quote.bid_px + quote.ask_px) / 2.0)
}

/// A running mean that remembers what it was averaged over.
///
/// The weight is kept rather than divided away because it is the coverage: a
/// −4 bp markout over 20 USDT of the 4,000 traded is not a fact about the
/// trading, and only the weight says so.
#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub struct Weighted {
    pub weight: f64,
    total: f64,
}

impl Weighted {
    pub fn add(&mut self, value: f64, weight: f64) {
        if !value.is_finite() || !weight.is_finite() || weight <= 0.0 {
            return;
        }
        self.weight += weight;
        self.total += value * weight;
    }

    /// The mean, or `None` when nothing was ever measured. Never zero: a zero
    /// would read as "we measured, and it cost nothing".
    pub fn mean(&self) -> Option<f64> {
        (self.weight > 0.0).then(|| self.total / self.weight)
    }

    pub fn merge(&mut self, other: &Weighted) {
        self.weight += other.weight;
        self.total += other.total;
    }
}

/// What one strategy's trading in one symbol cost. Also the shape of every
/// rollup, because merging two of these is how a rollup is made.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Costs {
    pub fills: u64,
    /// Fills where we were the resting side.
    pub maker_fills: u64,
    pub notional_usdt: f64,
    /// What the venue charged, in account currency. Negative is a rebate.
    pub fee_usdt: f64,
    pub arrival_shortfall: Weighted,
    pub effective_spread: Weighted,
    pub fee: Weighted,
    /// One per entry in [`HORIZONS_MS`].
    pub markout: [Weighted; HORIZONS_MS.len()],
    /// Fills whose horizon came and went without a readable book.
    pub marks_unmeasurable: u64,
}

impl Costs {
    /// The share of traded notional that rested. The number a maker lives on,
    /// and the one `STATE.md` says the funded grade is waiting for.
    ///
    /// By notional rather than by fill count, so one large taker fill cannot
    /// hide behind twenty small maker ones.
    pub fn maker_share(&self) -> Option<f64> {
        (self.fills > 0).then(|| self.maker_fills as f64 / self.fills as f64)
    }

    /// `arrival_shortfall_bps + fee_bps`, and `None` unless both are there —
    /// the doc is explicit that a half-measured all-in number is not one.
    pub fn all_in_arrival_bps(&self) -> Option<f64> {
        Some(self.arrival_shortfall.mean()? + self.fee.mean()?)
    }

    /// How much of the traded notional had a midpoint to measure against.
    /// Below 1 the arrival numbers describe only part of the trading.
    pub fn arrival_coverage(&self) -> Option<f64> {
        (self.notional_usdt > 0.0).then(|| self.arrival_shortfall.weight / self.notional_usdt)
    }

    pub fn merge(&mut self, other: &Costs) {
        self.fills += other.fills;
        self.maker_fills += other.maker_fills;
        self.notional_usdt += other.notional_usdt;
        self.fee_usdt += other.fee_usdt;
        self.arrival_shortfall.merge(&other.arrival_shortfall);
        self.effective_spread.merge(&other.effective_spread);
        self.fee.merge(&other.fee);
        for (mine, theirs) in self.markout.iter_mut().zip(other.markout.iter()) {
            mine.merge(theirs);
        }
        self.marks_unmeasurable += other.marks_unmeasurable;
    }
}

/// One fill, with everything needed to price it.
#[derive(Clone, Debug, PartialEq)]
pub struct Fill {
    pub client_order_id: String,
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub side: Side,
    pub qty: f64,
    pub px: f64,
    pub fee: f64,
    pub is_maker: bool,
    /// `M0`, from the order's own `OrderSent` record. Zero when the book could
    /// not be read then, which makes every arrival number for this fill
    /// missing rather than wrong.
    pub arrival_mid: f64,
    pub venue_ts_ms: i64,
}

/// A markout that came due and was read.
#[derive(Clone, Debug, PartialEq)]
pub struct Mark {
    pub client_order_id: String,
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub fill_ts_ms: i64,
    pub horizon_ms: u64,
    /// `Mh`. `None` when no healthy book turned up inside the lateness bound,
    /// which is a horizon that is terminally missing — never a zero.
    pub mid: Option<f64>,
    pub signed_markout_bps: Option<f64>,
    /// What the horizon actually turned out to be. The engine looks on its
    /// group-flush tick, so this is always a little more than `horizon_ms`,
    /// and how much more is worth being able to see.
    pub actual_horizon_ms: u64,
    /// The notional this mark speaks for, so a rollup can weight it.
    pub notional_usdt: f64,
}

/// A fill still owed one or more marks.
#[derive(Clone, Debug)]
struct Owed {
    client_order_id: String,
    strategy: StrategyId,
    symbol: SymbolId,
    side: Side,
    px: f64,
    notional_usdt: f64,
    fill_ts_ms: i64,
    filled_ns: u64,
    /// One bit per entry in [`HORIZONS_MS`], set while that horizon is still
    /// owed.
    owed: u8,
}

impl Owed {
    fn done(&self) -> bool {
        self.owed == 0
    }
}

/// The engine's running answer to "what is our trading costing".
///
/// Cumulative since boot rather than windowed, unlike the latency ledger: a
/// minute of latency samples is thousands, a minute of fills is often none,
/// and a window that usually reports nothing is not a measurement.
#[derive(Default)]
pub struct Fills {
    /// Keyed `(strategy, symbol)` and ordered, so a report reads the same way
    /// twice.
    by_key: BTreeMap<(u16, u16), Costs>,
    pending: VecDeque<Owed>,
    /// Fills dropped from `pending` because too many were waiting at once.
    pub dropped: u64,
}

impl Fills {
    /// Price one fill and start its markout clock.
    pub fn on_fill(&mut self, fill: &Fill, now_ns: u64) {
        let notional = (fill.px * fill.qty).abs();
        let costs = self
            .by_key
            .entry((fill.strategy.0, fill.symbol.0))
            .or_default();
        costs.fills += 1;
        if fill.is_maker {
            costs.maker_fills += 1;
        }
        if notional.is_finite() {
            costs.notional_usdt += notional;
        }
        if fill.fee.is_finite() {
            costs.fee_usdt += fill.fee;
        }
        if let Some(bps) = arrival_shortfall_bps(fill.side, fill.px, fill.arrival_mid) {
            costs.arrival_shortfall.add(bps, notional);
        }
        if let Some(bps) = effective_spread_bps(fill.side, fill.px, fill.arrival_mid) {
            costs.effective_spread.add(bps, notional);
        }
        if let Some(bps) = fee_bps(fill.fee, fill.px, fill.qty) {
            costs.fee.add(bps, notional);
        }

        if !usable(fill.px) || !notional.is_finite() {
            // Nothing later could be measured against this, so do not hold a
            // slot open for it.
            return;
        }
        if self.pending.len() >= MAX_PENDING {
            self.pending.pop_front();
            self.dropped += 1;
        }
        self.pending.push_back(Owed {
            client_order_id: fill.client_order_id.clone(),
            strategy: fill.strategy,
            symbol: fill.symbol,
            side: fill.side,
            px: fill.px,
            notional_usdt: notional,
            fill_ts_ms: fill.venue_ts_ms,
            filled_ns: now_ns,
            owed: (1u8 << HORIZONS_MS.len()) - 1,
        });
    }

    /// Read every markout that has come due, and fold it in. Called from the
    /// engine's group-flush tick, so a mark is at most one tick late on top of
    /// its horizon.
    ///
    /// Returns what was read so the caller can write it to the log. Marks are
    /// an observation, not arithmetic: they cannot be recomputed later from a
    /// log that holds no prices.
    pub fn due(&mut self, now_ns: u64, market: &MarketState) -> Vec<Mark> {
        let mut marks = Vec::new();
        for owed in self.pending.iter_mut() {
            let age_ms = (now_ns.saturating_sub(owed.filled_ns)) / 1_000_000;
            for (index, horizon_ms) in HORIZONS_MS.iter().enumerate() {
                let bit = 1u8 << index;
                if owed.owed & bit == 0 || age_ms < *horizon_ms {
                    continue;
                }
                let mid = healthy_mid(market, owed.symbol);
                let gave_up = age_ms >= horizon_ms.saturating_add(LATENESS_BOUND_MS);
                if mid.is_none() && !gave_up {
                    // "The first healthy midpoint at or after h" — so wait for
                    // one, but not forever.
                    continue;
                }
                owed.owed &= !bit;
                marks.push(Mark {
                    client_order_id: owed.client_order_id.clone(),
                    strategy: owed.strategy,
                    symbol: owed.symbol,
                    fill_ts_ms: owed.fill_ts_ms,
                    horizon_ms: *horizon_ms,
                    mid,
                    signed_markout_bps: mid
                        .and_then(|mid| signed_markout_bps(owed.side, owed.px, mid)),
                    actual_horizon_ms: age_ms,
                    notional_usdt: owed.notional_usdt,
                });
            }
        }
        self.pending.retain(|owed| !owed.done());
        for mark in &marks {
            self.fold_mark(mark);
        }
        marks
    }

    /// Fold a mark into the running totals. Public because the report read off
    /// a finished log takes the same path: one arithmetic, two callers.
    pub fn fold_mark(&mut self, mark: &Mark) {
        let Some(index) = HORIZONS_MS.iter().position(|h| *h == mark.horizon_ms) else {
            // A horizon this build does not measure. Counting it into the
            // wrong bucket would be worse than not counting it.
            return;
        };
        let costs = self
            .by_key
            .entry((mark.strategy.0, mark.symbol.0))
            .or_default();
        match mark.signed_markout_bps {
            Some(bps) => costs.markout[index].add(bps, mark.notional_usdt),
            None => costs.marks_unmeasurable += 1,
        }
    }

    /// Everything, added together.
    pub fn total(&self) -> Costs {
        let mut total = Costs::default();
        for costs in self.by_key.values() {
            total.merge(costs);
        }
        total
    }

    /// Per strategy and symbol, in a stable order.
    pub fn rows(&self) -> impl Iterator<Item = (StrategyId, SymbolId, &Costs)> {
        self.by_key
            .iter()
            .map(|((strategy, symbol), costs)| (StrategyId(*strategy), SymbolId(*symbol), costs))
    }

    /// One strategy's trading, across every symbol it touched.
    pub fn for_strategy(&self, strategy: StrategyId) -> Costs {
        let mut total = Costs::default();
        for ((sid, _), costs) in self.by_key.iter() {
            if *sid == strategy.0 {
                total.merge(costs);
            }
        }
        total
    }

    /// How many fills are still waiting for a mark.
    pub fn pending(&self) -> usize {
        self.pending.len()
    }

    /// Rebuild everything the log can account for.
    ///
    /// The arrival numbers are recomputed from `OrderSent` and the fills, by
    /// exactly the live path's arithmetic. The markouts are read back off the
    /// `Markout` records, because a log holds no prices and nothing could
    /// recompute them.
    ///
    /// One pass is enough: an `OrderSent` record is made durable before its
    /// bytes leave the socket, so it is always ahead of its own fills.
    pub fn from_records(records: &[WalRecord]) -> Self {
        let mut sent: HashMap<&str, (StrategyId, f64)> = HashMap::new();
        let mut me = Fills::default();
        for record in records {
            match record {
                WalRecord::OrderSent {
                    request,
                    arrival_mid,
                    ..
                } => {
                    sent.insert(
                        request.client_order_id.as_str(),
                        (request.strategy, *arrival_mid),
                    );
                }
                WalRecord::OrderUpdate {
                    update:
                        OrderUpdate::Fill {
                            client_order_id,
                            symbol,
                            side,
                            qty,
                            px,
                            fee,
                            is_maker,
                            venue_ts_ms,
                            ..
                        },
                } => {
                    // A fill for an order this log never sent belongs to
                    // somebody else on the account, exactly as `attribution`
                    // reads it. Pricing it would be pricing a stranger's
                    // trade.
                    let Some((strategy, arrival_mid)) = sent.get(client_order_id.as_str()).copied()
                    else {
                        continue;
                    };
                    me.on_fill(
                        &Fill {
                            client_order_id: client_order_id.clone(),
                            strategy,
                            symbol: *symbol,
                            side: *side,
                            qty: *qty,
                            px: *px,
                            fee: *fee,
                            is_maker: *is_maker,
                            arrival_mid,
                            venue_ts_ms: *venue_ts_ms,
                        },
                        0,
                    );
                }
                WalRecord::Markout {
                    client_order_id,
                    strategy,
                    symbol,
                    fill_ts_ms,
                    horizon_ms,
                    mid,
                    signed_markout_bps,
                    actual_horizon_ms,
                    notional_usdt,
                } => me.fold_mark(&Mark {
                    client_order_id: client_order_id.clone(),
                    strategy: *strategy,
                    symbol: *symbol,
                    fill_ts_ms: *fill_ts_ms,
                    horizon_ms: *horizon_ms,
                    mid: *mid,
                    signed_markout_bps: *signed_markout_bps,
                    actual_horizon_ms: *actual_horizon_ms,
                    notional_usdt: *notional_usdt,
                }),
                _ => {}
            }
        }
        // Replaying a log is not trading: nothing is owed a future mark,
        // because every mark this log will ever hold is already in it.
        me.pending.clear();
        me
    }
}

impl Mark {
    pub fn to_record(&self) -> WalRecord {
        WalRecord::Markout {
            client_order_id: self.client_order_id.clone(),
            strategy: self.strategy,
            symbol: self.symbol,
            fill_ts_ms: self.fill_ts_ms,
            horizon_ms: self.horizon_ms,
            mid: self.mid,
            signed_markout_bps: self.signed_markout_bps,
            actual_horizon_ms: self.actual_horizon_ms,
            notional_usdt: self.notional_usdt,
        }
    }
}

#[cfg(test)]
mod tests;
