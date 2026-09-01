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

use engine_types::{ForcedClose, MarketState, OrderUpdate, Side, StrategyId, SymbolId, WalRecord};

use crate::replay::LogNames;

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

/// The midpoint of a two-sided book, and when that book arrived.
///
/// A crossed or one-sided book is not a price. The Python calls this a book
/// that is not healthy and refuses to mark against it; so does this.
pub fn healthy_mid(market: &MarketState, symbol: SymbolId) -> Option<(f64, u64)> {
    let quote = market.quotes.get(symbol.0 as usize)?;
    if !usable(quote.bid_px) || !usable(quote.ask_px) || quote.ask_px < quote.bid_px {
        return None;
    }
    Some(((quote.bid_px + quote.ask_px) / 2.0, quote.recv_ns))
}

/// The midpoint, but only from a book that has arrived since a fill.
///
/// A markout asks where the price went *after* we traded. If no price has
/// arrived since, there is no answer yet — and the book still sitting there is
/// not evidence that nothing moved, it is evidence that nobody told us.
///
/// This is not a staleness threshold and deliberately not one: any constant
/// would be wrong for both a name that trades every millisecond and one that
/// trades twice an hour. The requirement is logical rather than numerical, so
/// it needs no tuning and cannot be tuned wrong.
///
/// It matters because the failure is invisible without it. A halted or
/// delisted symbol keeps its last quote for ever, and every horizon then marks
/// against the identical mid — four consistent numbers that read exactly like
/// a measurement and are a measurement of nothing. This fleet has been bitten
/// by a delisted symbol before.
pub fn mid_after(market: &MarketState, symbol: SymbolId, after_ns: u64) -> Option<f64> {
    let (mid, recv_ns) = healthy_mid(market, symbol)?;
    (recv_ns > after_ns).then_some(mid)
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
#[derive(Clone, Debug, PartialEq)]
pub struct Costs {
    pub fills: u64,
    /// Fills where we were the resting side, and what they traded.
    pub maker_fills: u64,
    pub maker_notional_usdt: f64,
    pub notional_usdt: f64,
    /// What the venue charged, in account currency. Negative is a rebate;
    /// `None` means at least one fill did not state a fee.
    pub fee_usdt: Option<f64>,
    pub arrival_shortfall: Weighted,
    pub fee: Weighted,
    /// `arrival_shortfall_bps + fee_bps`, accumulated per fill and only where
    /// both existed.
    ///
    /// Not the sum of the two means beside it: the fee is measured on every
    /// fill and the shortfall only on fills whose book could be read, so
    /// adding the means adds two numbers taken over different populations.
    /// The doc's rule -- null unless both operands are -- holds at the fill
    /// grain, and this is what carries it up to the rollup.
    pub all_in: Weighted,
    /// One per entry in [`HORIZONS_MS`].
    pub markout: [Weighted; HORIZONS_MS.len()],
    /// Horizons that came and went without a readable book.
    pub marks_unmeasurable: u64,
    /// Horizons whose book was read, too long after the horizon for the number
    /// to be that horizon's. Kept apart from the line above because they say
    /// different things about the run: one is a market that went quiet, the
    /// other is this process falling behind.
    pub marks_late: u64,
}

impl Default for Costs {
    fn default() -> Self {
        Costs {
            fills: 0,
            maker_fills: 0,
            maker_notional_usdt: 0.0,
            notional_usdt: 0.0,
            fee_usdt: Some(0.0),
            arrival_shortfall: Weighted::default(),
            fee: Weighted::default(),
            all_in: Weighted::default(),
            markout: [Weighted::default(); HORIZONS_MS.len()],
            marks_unmeasurable: 0,
            marks_late: 0,
        }
    }
}

impl Costs {
    /// The share of traded notional that rested. The number a maker lives on,
    /// and the one `STATE.md` says the funded grade is waiting for.
    ///
    /// By notional rather than by fill count, so one large taker fill cannot
    /// hide behind twenty small maker ones.
    pub fn maker_share(&self) -> Option<f64> {
        (self.notional_usdt > 0.0).then(|| self.maker_notional_usdt / self.notional_usdt)
    }

    /// `arrival_shortfall_bps + fee_bps`, over the fills that had both.
    pub fn all_in_arrival_bps(&self) -> Option<f64> {
        self.all_in.mean()
    }

    /// How much of the traded notional had a midpoint to measure against.
    /// Below 1 the arrival numbers describe only part of the trading.
    pub fn arrival_coverage(&self) -> Option<f64> {
        (self.notional_usdt > 0.0).then(|| self.arrival_shortfall.weight / self.notional_usdt)
    }

    /// How much traded notional carried a venue-stated fee. A zero fee still
    /// has coverage; an absent fee does not.
    pub fn fee_coverage(&self) -> Option<f64> {
        (self.notional_usdt > 0.0).then(|| self.fee.weight / self.notional_usdt)
    }

    pub fn merge(&mut self, other: &Costs) {
        self.fills += other.fills;
        self.maker_fills += other.maker_fills;
        self.maker_notional_usdt += other.maker_notional_usdt;
        self.notional_usdt += other.notional_usdt;
        self.fee_usdt = match (self.fee_usdt, other.fee_usdt) {
            (Some(mine), Some(theirs)) => Some(mine + theirs),
            _ => None,
        };
        self.arrival_shortfall.merge(&other.arrival_shortfall);
        self.fee.merge(&other.fee);
        self.all_in.merge(&other.all_in);
        for (mine, theirs) in self.markout.iter_mut().zip(other.markout.iter()) {
            mine.merge(theirs);
        }
        self.marks_unmeasurable += other.marks_unmeasurable;
        self.marks_late += other.marks_late;
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
    pub fee: Option<f64>,
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
    /// Keyed by the sleeve's and the coin's *names* and ordered, so a report
    /// reads the same way twice.
    ///
    /// Not by id. An id means whatever the table in force said it meant, and
    /// that table is rebuilt every boot: symbol 8 has been both HYPEUSDT and
    /// BICOUSDT in one log, and strategy 3 was a sleeve since retired. Keyed
    /// by id, a report over a log that spans boots adds two coins' trading
    /// into one row and labels it with whichever name the last table carried.
    by_key: BTreeMap<(String, String), Costs>,
    /// What the ids mean right now, kept current by [`Fills::learn`].
    names: LogNames,
    /// The same fills again, gathered into positions rather than into costs,
    /// so a sleeve going flat can say what the trip made.
    lots: roundtrip::Lots,
    pending: VecDeque<Owed>,
    /// Fills dropped from `pending` because too many were waiting at once.
    pub dropped: u64,
    /// Private-stream reconnections. Every one is a window in which fills
    /// happened and were never delivered.
    pub stream_gaps: u64,
    /// Fills the stream never delivered and the venue's own execution history
    /// gave up afterwards. Counted separately because they are the answer to
    /// `stream_gaps`: without them a reader has to assume the worst.
    pub recovered: u64,
}

impl Fills {
    /// The id tables changed. Both callers say so at the moment the change is
    /// recorded, which is what lets a row be keyed by name rather than by an
    /// id whose meaning does not survive the next boot.
    pub fn learn(&mut self, record: &WalRecord) {
        self.names.learn(record);
    }

    /// What a row for this fill or mark is called, as the ids read right now.
    fn key(&self, strategy: StrategyId, symbol: SymbolId) -> (String, String) {
        (self.names.strategy(strategy), self.names.symbol(symbol))
    }

    /// The private stream reconnected, so fills were delivered to nobody while
    /// it was down.
    ///
    /// The engine asks the venue for its own execution history afterwards and
    /// recovers what it can; a recovered fill is priced here like any other,
    /// and counted again as recovered so a report can say how much of itself
    /// arrived the slow way.
    pub fn stream_gap(&mut self) {
        self.stream_gaps += 1;
    }

    /// A fill the private stream never delivered, read back from the venue's
    /// own execution history. Priced exactly like a delivered one.
    ///
    /// `filled_ns` is when it happened, not when it was found. A recovery that
    /// runs seconds later still gets real markouts; one that runs minutes later
    /// finds every horizon already past, and the lateness bound throws those
    /// reads away rather than marking a stale trade against a fresh book.
    /// `None` is a fill older than the engine's clock, whose origin is this
    /// process: it cost what it cost, and it is owed no mark at all.
    pub fn on_recovered_fill(&mut self, fill: &Fill, filled_ns: Option<u64>) {
        self.recovered += 1;
        match filled_ns {
            Some(filled_ns) => self.on_fill(fill, filled_ns),
            None => {
                self.price(fill);
            }
        }
    }

    /// Price one fill and start its markout clock. `filled_ns` is when the
    /// fill happened on the engine's own clock.
    pub fn on_fill(&mut self, fill: &Fill, filled_ns: u64) {
        let notional = self.price(fill);

        // `usable`, not `is_finite`: a zero-notional fill would otherwise be
        // owed marks that are measured, written to the log, and then dropped
        // by a weight of zero -- counted neither in the average nor in the
        // tally of what could not be measured.
        if !usable(fill.px) || !usable(notional) {
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
            filled_ns,
            owed: (1u8 << HORIZONS_MS.len()) - 1,
        });
    }

    /// What one fill cost, folded into its row. Returns the notional, which is
    /// the weight every mark it is later owed carries.
    fn price(&mut self, fill: &Fill) -> f64 {
        let notional = (fill.px * fill.qty).abs();
        let key = self.key(fill.strategy, fill.symbol);
        self.lots.on_fill(&key.0, &key.1, fill);
        let costs = self.by_key.entry(key).or_default();
        costs.fills += 1;
        if fill.is_maker {
            costs.maker_fills += 1;
        }
        if notional.is_finite() {
            costs.notional_usdt += notional;
            if fill.is_maker {
                costs.maker_notional_usdt += notional;
            }
        }
        let stated_fee = fill.fee.filter(|fee| fee.is_finite());
        costs.fee_usdt = match (costs.fee_usdt, stated_fee) {
            (Some(total), Some(fee)) => Some(total + fee),
            _ => None,
        };
        let shortfall = arrival_shortfall_bps(fill.side, fill.px, fill.arrival_mid);
        let fee = stated_fee.and_then(|fee| fee_bps(fee, fill.px, fill.qty));
        if let Some(bps) = shortfall {
            costs.arrival_shortfall.add(bps, notional);
        }
        if let Some(bps) = fee {
            costs.fee.add(bps, notional);
        }
        if let (Some(shortfall), Some(fee)) = (shortfall, fee) {
            costs.all_in.add(shortfall + fee, notional);
        }
        notional
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
                // "The first healthy midpoint at or after h" — anchored at
                // the horizon, not the fill. A book that spoke once just
                // after the fill and then went quiet would otherwise hand
                // that early mid to the 1m/5m columns.
                let horizon_ns = owed
                    .filled_ns
                    .saturating_add((*horizon_ms).saturating_mul(1_000_000));
                let mid = mid_after(market, owed.symbol, horizon_ns);
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
        // A mark read long after its horizon is not that horizon. The engine
        // looks on a 250 ms tick, so every mark is a little late and that is
        // priced in; one that arrives twenty seconds late -- a stall, a paused
        // machine, a backlog replayed after a reconnect -- would otherwise be
        // averaged into the one-second column at full weight and read as a
        // one-second fact.
        if mark.actual_horizon_ms > mark.horizon_ms.saturating_add(LATENESS_BOUND_MS) {
            let key = self.key(mark.strategy, mark.symbol);
            self.by_key.entry(key).or_default().marks_late += 1;
            return;
        }
        let Some(index) = HORIZONS_MS.iter().position(|h| *h == mark.horizon_ms) else {
            // A horizon this build does not measure. Counting it into the
            // wrong bucket would be worse than not counting it.
            return;
        };
        let key = self.key(mark.strategy, mark.symbol);
        let costs = self.by_key.entry(key).or_default();
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

    /// Per sleeve and coin, in a stable order.
    pub fn rows(&self) -> impl Iterator<Item = (&str, &str, &Costs)> {
        self.by_key
            .iter()
            .map(|((strategy, symbol), costs)| (strategy.as_str(), symbol.as_str(), costs))
    }

    /// One sleeve's trading, across every coin it touched.
    pub fn for_strategy(&self, strategy: &str) -> Costs {
        let mut total = Costs::default();
        for ((sleeve, _), costs) in self.by_key.iter() {
            if sleeve == strategy {
                total.merge(costs);
            }
        }
        total
    }

    /// The sleeve a close the venue itself started belongs to: the one open
    /// lot in that coin whose side this fill reduces. The same question
    /// `attribution` answers off its claims, asked here of the positions,
    /// because a lot is keyed by name rather than by id.
    fn forced_close_owner(
        &self,
        client_order_id: &str,
        symbol: SymbolId,
        side: Side,
        forced_close: Option<ForcedClose>,
    ) -> Option<StrategyId> {
        if !client_order_id.is_empty() || forced_close.is_none() {
            return None;
        }
        let (sleeve, signed_qty) = self.lots.sole_holder(&self.names.symbol(symbol))?;
        if !crate::attribution::reduces(signed_qty, side) {
            return None;
        }
        let place = self.names.strategies.iter().position(|s| s == sleeve)?;
        Some(StrategyId(place as u16))
    }

    /// How many fills are still waiting for a mark.
    pub fn pending(&self) -> usize {
        self.pending.len()
    }

    /// Every round trip this has seen close, in the order they closed.
    pub fn closed(&self) -> &[roundtrip::ClosedTrade] {
        self.lots.closed()
    }

    /// The round trips that closed since this was last asked, and clear them.
    pub fn take_closed(&mut self) -> Vec<roundtrip::ClosedTrade> {
        self.lots.take_closed()
    }

    /// The open positions, for a caller that has to drop some of them.
    pub fn lots(&mut self) -> &mut roundtrip::Lots {
        &mut self.lots
    }

    /// Adopt the open positions a log leaves behind, and nothing else from it.
    ///
    /// The cost rows are this run's on purpose (struct note above), but a
    /// position is not: a sleeve that opened before a restart is still
    /// holding, and a close priced without its entry is a number about
    /// nothing. The trips those records already closed are dropped rather
    /// than announced a second time.
    pub fn seed_lots(&mut self, records: &[WalRecord]) {
        let mut rebuilt = Fills::from_records(records);
        rebuilt.lots.take_closed();
        self.lots = rebuilt.lots;
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
            // Before the record is folded, never after: a row is keyed by what
            // its ids meant at its own place in the log, not at the end of it.
            me.learn(record);
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
                            forced_close,
                            venue_ts_ms,
                            ..
                        },
                } => {
                    // A fill for an order this log never sent belongs to
                    // somebody else on the account, exactly as `attribution`
                    // reads it, unless the venue named it a close of a
                    // position one sleeve holds. Pricing anything else would
                    // be pricing a stranger's trade. A close nobody ordered
                    // has no order to anchor it, so its arrival midpoint is
                    // zero and yields no shortfall.
                    let Some((strategy, arrival_mid)) =
                        sent.get(client_order_id.as_str()).copied().or_else(|| {
                            me.forced_close_owner(client_order_id, *symbol, *side, *forced_close)
                                .map(|strategy| (strategy, 0.0))
                        })
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
                WalRecord::OrderUpdate {
                    update: OrderUpdate::StreamReset { .. },
                } => me.stream_gap(),
                // A claim boot found the venue does not back. There is no
                // exit price for it, so it is forgotten rather than reported.
                WalRecord::ClaimsDropped { rows, .. } => {
                    let gone: Vec<(String, String)> = rows
                        .iter()
                        .map(|row| (me.names.strategy(row.strategy), me.names.symbol(row.symbol)))
                        .collect();
                    me.lots.drop_symbols(|sleeve, symbol| {
                        gone.iter().any(|(s, y)| s == sleeve && y == symbol)
                    });
                }
                // What the stream missed, read back off the venue. The same
                // two joins as a delivered fill -- through the order that
                // produced it, or through the position a venue-named close
                // reduced -- so anything else is priced for nobody.
                WalRecord::RecoveredFill {
                    client_order_id,
                    symbol,
                    side,
                    qty,
                    px,
                    fee,
                    is_maker,
                    forced_close,
                    venue_ts_ms,
                    ..
                } => {
                    let Some((strategy, arrival_mid)) =
                        sent.get(client_order_id.as_str()).copied().or_else(|| {
                            me.forced_close_owner(client_order_id, *symbol, *side, *forced_close)
                                .map(|strategy| (strategy, 0.0))
                        })
                    else {
                        continue;
                    };
                    me.on_recovered_fill(
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
                        Some(0),
                    );
                }
                // A rotation restated every still-open order, so a fill that
                // lands after the rotation can still be priced against the
                // midpoint its order left at. The cost totals themselves are
                // NOT restated: a report over one segment covers that
                // segment's fills, and the whole history is a chain read
                // away.
                WalRecord::SegmentBase {
                    open_orders,
                    attribution,
                    ..
                } => {
                    for open in open_orders {
                        sent.insert(
                            open.request.client_order_id.as_str(),
                            (open.request.strategy, open.arrival_mid),
                        );
                    }
                    // What each sleeve was HOLDING, which the cost totals
                    // above have no use for and a position cannot do
                    // without: the fills that opened it are in the segment
                    // before this one, and this record is the only thing
                    // that carries the position across the boundary.
                    let held: Vec<(String, String, f64)> = attribution
                        .iter()
                        .map(|row| {
                            (
                                me.names.strategy(row.strategy),
                                me.names.symbol(row.symbol),
                                row.signed_qty,
                            )
                        })
                        .collect();
                    me.lots.restate(&held);
                }
                _ => {}
            }
        }
        // Replaying a log is not trading: nothing is owed a future mark,
        // because every mark this log will ever hold is already in it. The
        // drop count goes with the queue -- it counts this replay popping its
        // own transient queue, which is not something that happened to the
        // run, and reporting it as one would send a reader looking for an
        // incident that never was.
        me.pending.clear();
        me.dropped = 0;
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

pub mod report;
pub mod roundtrip;

#[cfg(test)]
mod tests;
