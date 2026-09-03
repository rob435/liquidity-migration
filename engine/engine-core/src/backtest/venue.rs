//! The simulated venue: the venue-side book, the matching, the account.
//!
//! One `SimulatedVenue` sits behind a mutex. The tape feed updates its books,
//! trades, marks and funding as rows are released; the gateway sends it
//! orders after their modelled flight; the private stream reads the updates
//! it queues. Everything it does happens at the virtual instant it is asked,
//! and nothing it does looks past that instant.
//!
//! Fill physics, in one place:
//!
//! | Order | Fills against | Fee | Partial |
//! | --- | --- | --- | --- |
//! | Market | the opposite side, level by level, until done or the side is empty; the rest is cancelled (IOC) | taker | yes, per level |
//! | Marketable limit | levels at or inside the limit; the rest rests (GTC), is cancelled (IOC), or the whole order is cancelled (PostOnly) | taker | yes |
//! | Resting limit | the displayed queue at its price: trades that reach the price eat the queue ahead, then us; the opposite touch crossing through the price fills the rest | maker | yes |
//! | Position stop | triggered on the **mark** (Bybit `slTriggerBy: MarkPrice`); fills the whole position by walking the book *at trigger time*, so a gap fills through the gap | taker | per level |
//! | Liquidation | equity at or below Σ maintenance margin; every position is closed by walking the book and every order pulled | taker | per level |
//!
//! Funding settles when the clock crosses the venue's published
//! `next_funding_time_ms`, at the rate quoted before the boundary, on the
//! position's notional at the mark. Margin: initial is notional over the
//! symbol's leverage; available is equity less initial margin on positions
//! and on resting opening orders. Rejections carry Bybit's codes.
//!
//! Not modelled: our fills do not consume the tape's liquidity (no impact),
//! nobody reacts to us, there is no liquidation fee, and no rate limit.

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};

use engine_types::{
    AccountIdentity, AccountView, AmendSpec, Depth, FeedError, ForcedClose, InstrumentRule,
    OrderAck, OrderFeed, OrderKind, OrderRequest, OrderUpdate, PositionView, Side, Symbol,
    SymbolId, TimeInForce, VenueCaps, VenueError, VenueExecution, VenueGateway, VenueOrder,
};

use super::scheduler::{Scheduler, WaiterKind};
use super::tape::TickerRow;
use crate::clock;

/// Bybit's own codes for the refusals this venue issues.
mod reject {
    pub const PARAMS: i64 = 10001;
    pub const INSUFFICIENT_BALANCE: i64 = 110007;
    pub const REDUCE_ONLY: i64 = 110017;
    pub const BELOW_MINIMUM: i64 = 110094;
    pub const ORDER_NOT_FOUND: i64 = 110001;
}

#[derive(Clone, Debug)]
pub struct VenueParams {
    pub initial_cash_usdt: f64,
    pub taker_fee_rate: f64,
    pub maker_fee_rate: f64,
    /// Full round trip of an order command: half to reach the venue, half
    /// for the reply.
    pub order_rtt_ns: u64,
    /// How long a private-stream update takes to reach the engine.
    pub private_latency_ns: u64,
    /// Leverage a symbol runs at until the engine sets one.
    pub default_leverage: f64,
    /// Maintenance margin as a fraction of notional.
    pub maintenance_margin_rate: f64,
}

#[derive(Clone, Debug, PartialEq)]
struct Position {
    side: Side,
    qty: f64,
    entry_px: f64,
    stop_px: Option<f64>,
    /// Venue fees paid opening what is still held. Follows the quantity out
    /// as it closes, so the closed part's fees can be set against the
    /// engine's own round-trip accounting.
    open_fees: f64,
}

#[derive(Clone, Debug)]
struct Resting {
    request: OrderRequest,
    px: f64,
    remaining: f64,
    tif: TimeInForce,
    /// Displayed size ahead of us at our price when we joined the queue.
    queue_ahead: f64,
    /// Whether filling this order can add exposure, for order margin.
    opens: bool,
}

#[derive(Clone, Debug, Default)]
struct Funding {
    rate: f64,
    next_ms: Option<i64>,
    settled_through_ms: i64,
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct EquityPoint {
    pub wall_ms: i64,
    pub cash_usdt: f64,
    pub unrealized_usdt: f64,
    pub equity_usdt: f64,
    pub initial_margin_usdt: f64,
    pub what: &'static str,
}

/// The venue's books at one instant, for the report and its reconciliation.
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize)]
pub struct Accounting {
    pub initial_cash_usdt: f64,
    pub cash_usdt: f64,
    pub realized_pnl_usdt: f64,
    pub fees_paid_usdt: f64,
    /// Fees on the entries of positions still open; the rest of
    /// `fees_paid_usdt` belongs to closed round trips.
    pub open_entry_fees_usdt: f64,
    pub funding_paid_usdt: f64,
    pub unrealized_usdt: f64,
    pub equity_usdt: f64,
    pub open_positions: usize,
    pub resting_orders: usize,
    pub fills: u64,
    pub maker_fills: u64,
    pub stop_fills: u64,
    pub liquidation_fills: u64,
    pub rejected_orders: u64,
    pub funding_settlements: u64,
    /// Stop or liquidation fills priced at the mark because the book side
    /// was empty at trigger time. Zero on a full tape.
    pub fills_without_book: u64,
    pub liquidated: bool,
}

pub struct SimulatedVenue {
    params: VenueParams,
    scheduler: Scheduler,
    symbols: Vec<Symbol>,
    rules: Vec<Option<InstrumentRule>>,
    books: Vec<Option<Depth>>,
    marks: Vec<Option<f64>>,
    lasts: Vec<Option<f64>>,
    funding: Vec<Funding>,
    leverage: Vec<Option<f64>>,
    positions: Vec<Option<Position>>,
    /// Keyed by client order id; a `BTreeMap` so iteration is one order.
    resting: BTreeMap<String, Resting>,
    private: VecDeque<(u64, OrderUpdate)>,
    cash: f64,
    accounting: Accounting,
    exec_counter: u64,
    order_counter: u64,
    pub equity_log: Vec<EquityPoint>,
}

impl SimulatedVenue {
    pub fn new(
        params: VenueParams,
        symbols: Vec<Symbol>,
        rules: &[(Symbol, InstrumentRule)],
        scheduler: Scheduler,
    ) -> Self {
        let n = symbols.len();
        let rules = symbols
            .iter()
            .map(|name| rules.iter().find(|(s, _)| s == name).map(|(_, r)| *r))
            .collect();
        let accounting = Accounting {
            initial_cash_usdt: params.initial_cash_usdt,
            cash_usdt: params.initial_cash_usdt,
            equity_usdt: params.initial_cash_usdt,
            ..Accounting::default()
        };
        SimulatedVenue {
            cash: params.initial_cash_usdt,
            params,
            scheduler,
            symbols,
            rules,
            books: vec![None; n],
            marks: vec![None; n],
            lasts: vec![None; n],
            funding: vec![Funding::default(); n],
            leverage: vec![None; n],
            positions: vec![None; n],
            resting: BTreeMap::new(),
            private: VecDeque::new(),
            accounting,
            exec_counter: 0,
            order_counter: 0,
            equity_log: Vec::new(),
        }
    }

    pub fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.symbols
            .iter()
            .position(|s| s == name)
            .map(|i| SymbolId(i as u16))
    }

    pub fn symbols(&self) -> &[Symbol] {
        &self.symbols
    }

    /// Whether a symbol has instrument rules, without which nothing is
    /// accepted on it.
    pub fn has_rules(&self, symbol: SymbolId) -> bool {
        self.rules
            .get(symbol.0 as usize)
            .is_some_and(Option::is_some)
    }

    // ------------------------------------------------------------ the tape

    pub fn on_book(&mut self, symbol: SymbolId, depth: &Depth) {
        let i = symbol.0 as usize;
        self.books[i] = Some(*depth);
        self.settle_funding_if_due(i);
        self.match_resting_against_touch(i);
    }

    pub fn on_trade(&mut self, symbol: SymbolId, price: f64, qty: f64, _buyer_aggressor: bool) {
        let i = symbol.0 as usize;
        self.lasts[i] = Some(price);
        self.settle_funding_if_due(i);
        let ids: Vec<String> = self
            .resting
            .iter()
            .filter(|(_, r)| r.request.symbol.0 as usize == i)
            .map(|(id, _)| id.clone())
            .collect();
        for id in ids {
            let Some(order) = self.resting.get_mut(&id) else {
                continue;
            };
            let reaches = match order.request.side {
                Side::Buy => price <= order.px,
                Side::Sell => price >= order.px,
            };
            if !reaches {
                continue;
            }
            let mut volume = qty;
            if order.queue_ahead > 0.0 {
                let eaten = order.queue_ahead.min(volume);
                order.queue_ahead -= eaten;
                volume -= eaten;
            }
            if volume <= 0.0 {
                continue;
            }
            let fill_qty = volume.min(order.remaining);
            let px = order.px;
            let request = order.request.clone();
            order.remaining -= fill_qty;
            let finished = order.remaining <= 1e-12;
            if finished {
                self.resting.remove(&id);
            }
            self.apply_fill(&request, fill_qty, px, true, None);
        }
    }

    pub fn on_ticker(&mut self, symbol: SymbolId, row: &TickerRow) {
        let i = symbol.0 as usize;
        if let Some(mark) = row.mark_price {
            if mark > 0.0 {
                self.marks[i] = Some(mark);
            }
        }
        if let Some(last) = row.last_price {
            if last > 0.0 {
                self.lasts[i] = Some(last);
            }
        }
        // Settle on the rate and boundary quoted BEFORE this row: the first
        // ticker after a boundary already names the next one.
        self.settle_funding_if_due(i);
        if let Some(rate) = row.funding_rate {
            self.funding[i].rate = rate;
        }
        if let Some(next) = row.next_funding_time_ms {
            if next > 0 {
                self.funding[i].next_ms = Some(next);
            }
        }
        self.check_stop(i);
        self.check_liquidation();
    }

    // -------------------------------------------------------------- orders

    /// The venue receives an order. Called by the gateway after the order's
    /// flight; the book it matches against is the book *now*.
    pub fn submit(&mut self, request: &OrderRequest) -> Result<String, VenueError> {
        let i = request.symbol.0 as usize;
        let venue_order_id = {
            self.order_counter += 1;
            format!("sim-order-{}", self.order_counter)
        };
        if let Err(error) = self.validate(request) {
            self.accounting.rejected_orders += 1;
            return Err(error);
        }
        let mut request = request.clone();
        if request.close_position {
            if let Some(position) = &self.positions[i] {
                request.qty = position.qty;
            }
        }
        match request.kind {
            OrderKind::Market => {
                let filled = self.walk_book(&request, None, false, None);
                let remaining = request.qty - filled;
                if remaining > 1e-12 {
                    self.queue_private(OrderUpdate::Cancelled {
                        client_order_id: request.client_order_id.clone(),
                        recv_ns: 0,
                    });
                }
            }
            OrderKind::Limit { px, tif } => {
                let marketable = match (request.side, self.book(i)) {
                    (Side::Buy, Some(book)) => book.best_ask().is_some_and(|a| a.px <= px),
                    (Side::Sell, Some(book)) => book.best_bid().is_some_and(|b| b.px >= px),
                    (_, None) => false,
                };
                if marketable && tif == TimeInForce::PostOnly {
                    self.queue_private(OrderUpdate::Cancelled {
                        client_order_id: request.client_order_id.clone(),
                        recv_ns: 0,
                    });
                    return Ok(venue_order_id);
                }
                let filled = if marketable {
                    self.walk_book(&request, Some(px), false, None)
                } else {
                    0.0
                };
                let remaining = request.qty - filled;
                if remaining > 1e-12 {
                    if tif == TimeInForce::Ioc {
                        self.queue_private(OrderUpdate::Cancelled {
                            client_order_id: request.client_order_id.clone(),
                            recv_ns: 0,
                        });
                    } else {
                        let queue_ahead = self.displayed_at(i, request.side, px);
                        let opens = self.opens_exposure(&request, remaining);
                        self.resting.insert(
                            request.client_order_id.clone(),
                            Resting {
                                px,
                                remaining,
                                tif,
                                queue_ahead,
                                opens,
                                request,
                            },
                        );
                    }
                }
            }
        }
        Ok(venue_order_id)
    }

    fn validate(&self, request: &OrderRequest) -> Result<(), VenueError> {
        let i = request.symbol.0 as usize;
        let rejected = |code: i64, message: String| VenueError::Rejected { code, message };
        let Some(rule) = self.rules.get(i).copied().flatten() else {
            return Err(rejected(
                reject::PARAMS,
                format!("{} has no instrument rule on this tape", self.name(i)),
            ));
        };
        if self.accounting.liquidated {
            return Err(rejected(
                reject::INSUFFICIENT_BALANCE,
                "account liquidated".to_string(),
            ));
        }
        if !request.qty.is_finite() || request.qty <= 0.0 {
            if !request.close_position {
                return Err(rejected(
                    reject::PARAMS,
                    format!("qty {} is not positive", request.qty),
                ));
            }
        } else if !on_grid(request.qty, rule.qty_step) {
            return Err(rejected(
                reject::PARAMS,
                format!(
                    "qty {} is not a multiple of qtyStep {}",
                    request.qty, rule.qty_step
                ),
            ));
        }
        let ref_px = match request.kind {
            OrderKind::Limit { px, .. } => {
                if !px.is_finite() || px <= 0.0 || !on_grid(px, rule.tick_size) {
                    return Err(rejected(
                        reject::PARAMS,
                        format!(
                            "price {px} is not a multiple of tickSize {}",
                            rule.tick_size
                        ),
                    ));
                }
                px
            }
            OrderKind::Market => match (request.side, self.book(i)) {
                (Side::Buy, Some(book)) => book.best_ask().map(|l| l.px).unwrap_or(0.0),
                (Side::Sell, Some(book)) => book.best_bid().map(|l| l.px).unwrap_or(0.0),
                (_, None) => 0.0,
            },
        };
        if ref_px <= 0.0 {
            return Err(rejected(
                reject::PARAMS,
                format!(
                    "{} has no book to price a market order against",
                    self.name(i)
                ),
            ));
        }
        let position = self.positions[i].as_ref();
        if request.reduce_only || request.close_position {
            let Some(position) = position else {
                return Err(rejected(
                    reject::REDUCE_ONLY,
                    "reduce-only order with no position".to_string(),
                ));
            };
            if position.side == request.side {
                return Err(rejected(
                    reject::REDUCE_ONLY,
                    "reduce-only order on the position's own side".to_string(),
                ));
            }
            if !request.close_position && request.qty > position.qty + 1e-12 {
                return Err(rejected(
                    reject::REDUCE_ONLY,
                    format!(
                        "reduce-only qty {} exceeds position {}",
                        request.qty, position.qty
                    ),
                ));
            }
            return Ok(());
        }
        if request.qty < rule.min_qty {
            return Err(rejected(
                reject::BELOW_MINIMUM,
                format!("qty {} is below minOrderQty {}", request.qty, rule.min_qty),
            ));
        }
        if request.qty * ref_px < rule.min_notional {
            return Err(rejected(
                reject::BELOW_MINIMUM,
                format!(
                    "notional {} is below minNotionalValue {}",
                    request.qty * ref_px,
                    rule.min_notional
                ),
            ));
        }
        let increase = match position {
            Some(p) if p.side != request.side => (request.qty - p.qty).max(0.0),
            _ => request.qty,
        };
        let additional_margin = increase * ref_px / self.leverage_of(i);
        let available = self.available_usdt();
        if additional_margin > available + 1e-9 {
            return Err(rejected(
                reject::INSUFFICIENT_BALANCE,
                format!(
                    "order needs {additional_margin:.4} USDT initial margin, {available:.4} available"
                ),
            ));
        }
        Ok(())
    }

    /// Fill `request` by walking the opposite side of the book. Returns the
    /// quantity filled. `limit` bounds the levels taken; `forced` marks a
    /// venue-started close. Levels are not consumed: our fills leave the
    /// tape's liquidity as it was.
    fn walk_book(
        &mut self,
        request: &OrderRequest,
        limit: Option<f64>,
        _reserved: bool,
        forced: Option<ForcedClose>,
    ) -> f64 {
        let i = request.symbol.0 as usize;
        let Some(book) = self.book(i) else {
            return 0.0;
        };
        let levels: Vec<engine_types::BookLevel> = match request.side {
            Side::Buy => book.asks[..book.ask_len as usize].to_vec(),
            Side::Sell => book.bids[..book.bid_len as usize].to_vec(),
        };
        let mut remaining = request.qty;
        let mut filled = 0.0;
        for level in levels {
            if remaining <= 1e-12 {
                break;
            }
            let inside = match (request.side, limit) {
                (_, None) => true,
                (Side::Buy, Some(px)) => level.px <= px,
                (Side::Sell, Some(px)) => level.px >= px,
            };
            if !inside {
                break;
            }
            let take = remaining.min(level.qty);
            if take <= 0.0 {
                continue;
            }
            self.apply_fill(request, take, level.px, false, forced);
            remaining -= take;
            filled += take;
        }
        filled
    }

    /// One execution: the position, the cash, the fee, and the private
    /// update the engine will read after its hop.
    fn apply_fill(
        &mut self,
        request: &OrderRequest,
        qty: f64,
        px: f64,
        is_maker: bool,
        forced: Option<ForcedClose>,
    ) {
        let i = request.symbol.0 as usize;
        let rate = if is_maker {
            self.params.maker_fee_rate
        } else {
            self.params.taker_fee_rate
        };
        let fee = (px * qty * rate).abs();
        self.cash -= fee;
        self.accounting.fees_paid_usdt += fee;
        self.accounting.fills += 1;
        if is_maker {
            self.accounting.maker_fills += 1;
        }
        match forced {
            Some(ForcedClose::StopLoss) => self.accounting.stop_fills += 1,
            Some(ForcedClose::Liquidation) => self.accounting.liquidation_fills += 1,
            _ => {}
        }

        let stop = request.stop.map(|s| s.trigger_px);
        match self.positions[i].take() {
            None => {
                self.positions[i] = Some(Position {
                    side: request.side,
                    qty,
                    entry_px: px,
                    stop_px: stop,
                    open_fees: fee,
                });
            }
            Some(mut position) if position.side == request.side => {
                let total = position.qty + qty;
                position.entry_px = (position.entry_px * position.qty + px * qty) / total;
                position.qty = total;
                position.open_fees += fee;
                if stop.is_some() {
                    position.stop_px = stop;
                }
                self.positions[i] = Some(position);
            }
            Some(mut position) => {
                let closed = qty.min(position.qty);
                let sign = if position.side == Side::Buy {
                    1.0
                } else {
                    -1.0
                };
                let pnl = (px - position.entry_px) * closed * sign;
                self.cash += pnl;
                self.accounting.realized_pnl_usdt += pnl;
                let share = if position.qty > 0.0 {
                    closed / position.qty
                } else {
                    1.0
                };
                position.open_fees -= position.open_fees * share;
                position.qty -= closed;
                let flipped = qty - closed;
                if position.qty > 1e-12 {
                    // The closing leg's fee belongs to the closed trip.
                    self.positions[i] = Some(position);
                } else if flipped > 1e-12 {
                    self.positions[i] = Some(Position {
                        side: request.side,
                        qty: flipped,
                        entry_px: px,
                        stop_px: stop,
                        // The whole fee was charged on `qty`; the part that
                        // opened the new side is its entry fee.
                        open_fees: fee * (flipped / qty),
                    });
                }
            }
        }

        let exec_id = {
            self.exec_counter += 1;
            format!("sim-exec-{}", self.exec_counter)
        };
        self.queue_private(OrderUpdate::Fill {
            exec_id,
            client_order_id: request.client_order_id.clone(),
            symbol: request.symbol,
            side: request.side,
            qty,
            px,
            fee: Some(fee),
            is_maker,
            forced_close: forced,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: 0,
        });
        self.record_equity(if forced.is_some() {
            "forced_close"
        } else {
            "fill"
        });
    }

    fn match_resting_against_touch(&mut self, i: usize) {
        let Some(book) = self.book(i) else {
            return;
        };
        let (best_bid, best_ask) = (book.best_bid(), book.best_ask());
        let crossed: Vec<String> = self
            .resting
            .iter()
            .filter(|(_, r)| r.request.symbol.0 as usize == i)
            .filter(|(_, r)| match r.request.side {
                Side::Buy => best_ask.is_some_and(|a| a.px <= r.px),
                Side::Sell => best_bid.is_some_and(|b| b.px >= r.px),
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in crossed {
            if let Some(order) = self.resting.remove(&id) {
                self.apply_fill(&order.request, order.remaining, order.px, true, None);
            }
        }
    }

    fn check_stop(&mut self, i: usize) {
        let Some(mark) = self.marks[i] else {
            return;
        };
        let Some(position) = self.positions[i].clone() else {
            return;
        };
        let Some(stop_px) = position.stop_px else {
            return;
        };
        let triggered = match position.side {
            Side::Buy => mark <= stop_px,
            Side::Sell => mark >= stop_px,
        };
        if !triggered {
            return;
        }
        self.force_close(i, &position, ForcedClose::StopLoss, mark);
    }

    fn check_liquidation(&mut self) {
        if self.accounting.liquidated {
            return;
        }
        let maintenance: f64 = (0..self.symbols.len())
            .filter_map(|i| {
                let p = self.positions[i].as_ref()?;
                Some(p.qty * self.reference_px(i, p.entry_px) * self.params.maintenance_margin_rate)
            })
            .sum();
        if maintenance <= 0.0 || self.equity_usdt() > maintenance {
            return;
        }
        self.accounting.liquidated = true;
        for i in 0..self.symbols.len() {
            if let Some(position) = self.positions[i].clone() {
                let mark = self.reference_px(i, position.entry_px);
                self.force_close(i, &position, ForcedClose::Liquidation, mark);
            }
        }
        let ids: Vec<String> = self.resting.keys().cloned().collect();
        for id in ids {
            self.resting.remove(&id);
            self.queue_private(OrderUpdate::Cancelled {
                client_order_id: id,
                recv_ns: 0,
            });
        }
        self.record_equity("liquidation");
    }

    /// Close a position the venue decided to close, by walking the book;
    /// at the mark when the side is empty, and counted when it is.
    fn force_close(&mut self, i: usize, position: &Position, why: ForcedClose, fallback_px: f64) {
        let request = OrderRequest {
            client_order_id: String::new(),
            strategy: engine_types::StrategyId(0),
            symbol: SymbolId(i as u16),
            side: position.side.flipped(),
            qty: position.qty,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: true,
        };
        let filled = self.walk_book(&request, None, false, Some(why));
        let remaining = position.qty - filled;
        if remaining > 1e-12 {
            self.accounting.fills_without_book += 1;
            let rest = OrderRequest {
                qty: remaining,
                ..request
            };
            self.apply_fill(&rest, remaining, fallback_px, false, Some(why));
        }
    }

    fn settle_funding_if_due(&mut self, i: usize) {
        let now_ms = clock::wall_ms();
        let Some(next_ms) = self.funding[i].next_ms else {
            return;
        };
        if now_ms < next_ms || self.funding[i].settled_through_ms >= next_ms {
            return;
        }
        self.funding[i].settled_through_ms = next_ms;
        let Some(position) = self.positions[i].as_ref() else {
            return;
        };
        let mark = self.reference_px(i, position.entry_px);
        let notional = position.qty * mark;
        let rate = self.funding[i].rate;
        // Positive rate: longs pay shorts.
        let payment = match position.side {
            Side::Buy => notional * rate,
            Side::Sell => -notional * rate,
        };
        self.cash -= payment;
        self.accounting.funding_paid_usdt += payment;
        self.accounting.funding_settlements += 1;
        self.record_equity("funding");
    }

    // ------------------------------------------------------------ account

    fn book(&self, i: usize) -> Option<&Depth> {
        self.books.get(i).and_then(Option::as_ref)
    }

    fn name(&self, i: usize) -> &str {
        self.symbols.get(i).map(String::as_str).unwrap_or("?")
    }

    fn leverage_of(&self, i: usize) -> f64 {
        self.leverage
            .get(i)
            .copied()
            .flatten()
            .unwrap_or(self.params.default_leverage)
            .max(1.0)
    }

    /// The price a position is valued at: the mark, else the last trade,
    /// else the book mid, else what was paid.
    fn reference_px(&self, i: usize, fallback: f64) -> f64 {
        self.marks[i]
            .or(self.lasts[i])
            .or_else(|| {
                let book = self.book(i)?;
                let bid = book.best_bid()?.px;
                let ask = book.best_ask()?.px;
                Some((bid + ask) / 2.0)
            })
            .unwrap_or(fallback)
    }

    fn displayed_at(&self, i: usize, side: Side, px: f64) -> f64 {
        let Some(book) = self.book(i) else {
            return 0.0;
        };
        let levels = match side {
            Side::Buy => &book.bids[..book.bid_len as usize],
            Side::Sell => &book.asks[..book.ask_len as usize],
        };
        levels
            .iter()
            .find(|l| l.px == px)
            .map(|l| l.qty)
            .unwrap_or(0.0)
    }

    fn opens_exposure(&self, request: &OrderRequest, qty: f64) -> bool {
        match self.positions[request.symbol.0 as usize].as_ref() {
            Some(p) if p.side != request.side => qty > p.qty,
            _ => true,
        }
    }

    fn unrealized_usdt(&self) -> f64 {
        (0..self.symbols.len())
            .filter_map(|i| {
                let p = self.positions[i].as_ref()?;
                let px = self.reference_px(i, p.entry_px);
                let sign = if p.side == Side::Buy { 1.0 } else { -1.0 };
                Some((px - p.entry_px) * p.qty * sign)
            })
            .sum()
    }

    fn equity_usdt(&self) -> f64 {
        self.cash + self.unrealized_usdt()
    }

    fn initial_margin_usdt(&self) -> f64 {
        let positions: f64 = (0..self.symbols.len())
            .filter_map(|i| {
                let p = self.positions[i].as_ref()?;
                Some(p.qty * self.reference_px(i, p.entry_px) / self.leverage_of(i))
            })
            .sum();
        let orders: f64 = self
            .resting
            .values()
            .filter(|r| r.opens)
            .map(|r| r.px * r.remaining / self.leverage_of(r.request.symbol.0 as usize))
            .sum();
        positions + orders
    }

    fn available_usdt(&self) -> f64 {
        (self.equity_usdt() - self.initial_margin_usdt()).max(0.0)
    }

    fn record_equity(&mut self, what: &'static str) {
        let unrealized = self.unrealized_usdt();
        self.equity_log.push(EquityPoint {
            wall_ms: clock::wall_ms(),
            cash_usdt: self.cash,
            unrealized_usdt: unrealized,
            equity_usdt: self.cash + unrealized,
            initial_margin_usdt: self.initial_margin_usdt(),
            what,
        });
    }

    fn queue_private(&mut self, mut update: OrderUpdate) {
        let deliver_at = self
            .scheduler
            .now_ns()
            .saturating_add(self.params.private_latency_ns);
        match &mut update {
            OrderUpdate::Fill { recv_ns, .. }
            | OrderUpdate::Cancelled { recv_ns, .. }
            | OrderUpdate::Amended { recv_ns, .. }
            | OrderUpdate::StopAttached { recv_ns, .. }
            | OrderUpdate::StreamReset { recv_ns }
            | OrderUpdate::FastFill { recv_ns, .. } => *recv_ns = deliver_at,
            OrderUpdate::Ack(_) | OrderUpdate::Reject { .. } => {}
        }
        self.private.push_back((deliver_at, update));
    }

    /// The venue's books now.
    pub fn accounting(&self) -> Accounting {
        let mut a = self.accounting.clone();
        a.cash_usdt = self.cash;
        a.unrealized_usdt = self.unrealized_usdt() + 0.0;
        a.equity_usdt = self.cash + a.unrealized_usdt;
        a.open_positions = self.positions.iter().flatten().count();
        a.resting_orders = self.resting.len();
        a.open_entry_fees_usdt = self
            .positions
            .iter()
            .flatten()
            .map(|p| p.open_fees)
            .sum::<f64>()
            + 0.0;
        a
    }

    pub fn account_view(&self) -> AccountView {
        let positions = (0..self.symbols.len())
            .filter_map(|i| {
                let p = self.positions[i].as_ref()?;
                Some(PositionView {
                    symbol: SymbolId(i as u16),
                    side: p.side,
                    qty: p.qty,
                    entry_px: p.entry_px,
                    stop_attached: p.stop_px.is_some(),
                    stop_px: p.stop_px.unwrap_or(0.0),
                    leverage: Some(self.leverage_of(i)),
                })
            })
            .collect();
        AccountView {
            equity_usdt: self.equity_usdt(),
            available_usdt: self.available_usdt(),
            positions,
            observed_ns: self.scheduler.now_ns(),
        }
    }

    fn working_orders(&self) -> Vec<VenueOrder> {
        self.resting
            .values()
            .map(|r| VenueOrder {
                client_order_id: r.request.client_order_id.clone(),
                symbol: self.name(r.request.symbol.0 as usize).to_string(),
                side: r.request.side,
                qty: r.request.qty,
                filled_qty: r.request.qty - r.remaining,
                reduce_only: r.request.reduce_only,
            })
            .collect()
    }

    fn cancel(&mut self, client_order_id: &str) -> Result<(), VenueError> {
        if self.resting.remove(client_order_id).is_none() {
            return Err(VenueError::Rejected {
                code: reject::ORDER_NOT_FOUND,
                message: format!("order {client_order_id} is not working"),
            });
        }
        self.queue_private(OrderUpdate::Cancelled {
            client_order_id: client_order_id.to_string(),
            recv_ns: 0,
        });
        Ok(())
    }

    fn amend(&mut self, client_order_id: &str, spec: AmendSpec) -> Result<(), VenueError> {
        let i;
        let side;
        let (px, remaining) = {
            let Some(order) = self.resting.get_mut(client_order_id) else {
                return Err(VenueError::Rejected {
                    code: reject::ORDER_NOT_FOUND,
                    message: format!("order {client_order_id} is not working"),
                });
            };
            i = order.request.symbol.0 as usize;
            side = order.request.side;
            let rule = self.rules[i].expect("a resting order has a rule");
            if let Some(px) = spec.px {
                if !on_grid(px, rule.tick_size) {
                    return Err(VenueError::Rejected {
                        code: reject::PARAMS,
                        message: format!(
                            "price {px} is not a multiple of tickSize {}",
                            rule.tick_size
                        ),
                    });
                }
            }
            if let Some(qty) = spec.qty {
                if !on_grid(qty, rule.qty_step) || qty <= 0.0 {
                    return Err(VenueError::Rejected {
                        code: reject::PARAMS,
                        message: format!(
                            "qty {qty} is not a multiple of qtyStep {}",
                            rule.qty_step
                        ),
                    });
                }
            }
            let filled = order.request.qty - order.remaining;
            let mut requeue = false;
            if let Some(px) = spec.px {
                if px != order.px {
                    order.px = px;
                    order.request.kind = OrderKind::Limit { px, tif: order.tif };
                    requeue = true;
                }
            }
            if let Some(qty) = spec.qty {
                if qty > order.request.qty {
                    requeue = true;
                }
                order.request.qty = qty;
                order.remaining = (qty - filled).max(0.0);
            }
            if requeue {
                order.queue_ahead = f64::NAN;
            }
            (order.px, order.remaining)
        };
        // A repriced or enlarged order goes to the back of its new queue.
        let displayed = self.displayed_at(i, side, px);
        if let Some(order) = self.resting.get_mut(client_order_id) {
            if order.queue_ahead.is_nan() {
                order.queue_ahead = displayed;
            }
        }
        self.queue_private(OrderUpdate::Amended {
            client_order_id: client_order_id.to_string(),
            px,
            qty: remaining,
            recv_ns: 0,
        });
        // A repriced order may now sit across the touch.
        self.match_resting_against_touch(i);
        Ok(())
    }

    fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        let i = symbol.0 as usize;
        let Some(position) = self.positions.get_mut(i).and_then(Option::as_mut) else {
            return Err(VenueError::Rejected {
                code: reject::PARAMS,
                message: format!("{} has no position to stop", self.name(i)),
            });
        };
        position.stop_px = Some(trigger_px);
        self.queue_private(OrderUpdate::StopAttached {
            symbol,
            trigger_px,
            recv_ns: 0,
        });
        // A stop set at or through the mark fires at once.
        self.check_stop(i);
        Ok(())
    }

    fn add_symbol(&mut self, name: &str) -> SymbolId {
        if let Some(id) = self.symbol_id(name) {
            return id;
        }
        self.symbols.push(name.to_string());
        self.rules.push(None);
        self.books.push(None);
        self.marks.push(None);
        self.lasts.push(None);
        self.funding.push(Funding::default());
        self.leverage.push(None);
        self.positions.push(None);
        SymbolId((self.symbols.len() - 1) as u16)
    }

    /// Give a late-admitted symbol its rules, from the same snapshot.
    pub fn set_rule(&mut self, symbol: SymbolId, rule: InstrumentRule) {
        if let Some(slot) = self.rules.get_mut(symbol.0 as usize) {
            *slot = Some(rule);
        }
    }

    fn pop_private_due(&mut self, now_ns: u64) -> Option<OrderUpdate> {
        match self.private.front() {
            Some((at, _)) if *at <= now_ns => self.private.pop_front().map(|(_, u)| u),
            _ => None,
        }
    }

    fn next_private_at(&self) -> Option<u64> {
        self.private.front().map(|(at, _)| *at)
    }

    pub fn private_pending(&self) -> usize {
        self.private.len()
    }

    /// The queued private updates, for tests that assert on their shape.
    #[cfg(test)]
    pub fn debug_private(&self) -> Vec<OrderUpdate> {
        self.private.iter().map(|(_, u)| u.clone()).collect()
    }
}

/// Whether `value` sits on a grid of `step`, within floating slack.
fn on_grid(value: f64, step: f64) -> bool {
    if !step.is_finite() || step <= 0.0 || !value.is_finite() {
        return false;
    }
    let steps = value / step;
    (steps - steps.round()).abs() < 1e-6
}

// ---------------------------------------------------------------- gateway

/// The engine's side of the venue: every command flies for half a round
/// trip, is answered by the venue as it stands then, and the reply flies
/// back for the other half.
pub struct SimVenueGateway {
    venue: Arc<Mutex<SimulatedVenue>>,
    scheduler: Scheduler,
    rtt_ns: u64,
}

impl SimVenueGateway {
    pub fn new(venue: Arc<Mutex<SimulatedVenue>>, scheduler: Scheduler, rtt_ns: u64) -> Self {
        SimVenueGateway {
            venue,
            scheduler,
            rtt_ns,
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, SimulatedVenue> {
        self.venue.lock().unwrap_or_else(|p| p.into_inner())
    }

    async fn half_flight(&self) {
        let half = std::time::Duration::from_nanos(self.rtt_ns / 2);
        self.scheduler.sleep(half, WaiterKind::Venue).await;
    }
}

#[engine_types::async_trait]
impl VenueGateway for SimVenueGateway {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            native_position_stop: true,
            amend_in_place: true,
            set_leverage: true,
            close_position_below_minimum: true,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            venue: "simulated".to_string(),
            user_id: "backtest".to_string(),
            realm: "backtest".to_string(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let sent_ns = self.scheduler.now_ns();
        self.half_flight().await;
        let venue_order_id = self.lock().submit(req);
        self.half_flight().await;
        let venue_order_id = venue_order_id?;
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id,
            sent_ns,
            ack_ns: self.scheduler.now_ns(),
        })
    }

    async fn cancel_order(
        &mut self,
        _symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        self.half_flight().await;
        let result = self.lock().cancel(client_order_id);
        self.half_flight().await;
        result
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.half_flight().await;
        let result = self.lock().amend(client_order_id, spec);
        self.half_flight().await;
        result
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        self.half_flight().await;
        let result = self.lock().set_stop(symbol, trigger_px);
        self.half_flight().await;
        result
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        Some(self.lock().add_symbol(symbol))
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        if !leverage.is_finite() || leverage < 1.0 {
            return Err(VenueError::Rejected {
                code: reject::PARAMS,
                message: format!("leverage {leverage} is not at least 1"),
            });
        }
        self.half_flight().await;
        {
            let mut venue = self.lock();
            if let Some(slot) = venue.leverage.get_mut(symbol.0 as usize) {
                *slot = Some(leverage);
            }
        }
        self.half_flight().await;
        Ok(())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        self.half_flight().await;
        let view = self.lock().account_view();
        self.half_flight().await;
        Ok(view)
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        let venue = self.lock();
        Ok(venue
            .symbols
            .iter()
            .zip(venue.rules.iter())
            .filter_map(|(name, rule)| rule.map(|r| (name.clone(), r)))
            .collect())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        Ok(self.lock().working_orders())
    }

    /// A fresh account: nothing traded before this run.
    async fn executions(
        &mut self,
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        Ok(Vec::new())
    }
}

// ------------------------------------------------------------ private feed

/// The private stream: every update the venue queued, delivered one hop
/// after the venue produced it. Cancel-safe — an update leaves the queue
/// only on the poll that returns it.
pub struct SimOrderFeed {
    venue: Arc<Mutex<SimulatedVenue>>,
    scheduler: Scheduler,
}

impl SimOrderFeed {
    pub fn new(venue: Arc<Mutex<SimulatedVenue>>, scheduler: Scheduler) -> Self {
        SimOrderFeed { venue, scheduler }
    }
}

impl OrderFeed for SimOrderFeed {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        loop {
            let next_at = {
                let mut venue = self.venue.lock().unwrap_or_else(|p| p.into_inner());
                if let Some(update) = venue.pop_private_due(self.scheduler.now_ns()) {
                    return Ok(update);
                }
                venue.next_private_at()
            };
            match next_at {
                Some(at) => self.scheduler.sleep_until(at, WaiterKind::Private).await,
                None => std::future::pending().await,
            }
        }
    }
}
