//! `quoter`: hold a two-sided quote around the market, and keep it there.
//!
//! The other kind of strategy. It decides in the loop rather than following a
//! book, and it needs the whole order vocabulary: quotes have to be pulled
//! when the book breaks or inventory fills, and moved when the market walks
//! away. Placing alone is not market making.
//!
//! The arithmetic — where a quote belongs, when it is worth moving, which side
//! stops being quoted — is [`super::plan`], and it is pure. This plug only
//! turns its answers into actions and remembers which order is which.
//!
//! ## Three things it reads that a naive maker gets wrong
//!
//! - **Its own position, not the account's.** [`StrategyCtx::my_position`] is
//!   this strategy's own fills; `position` is the venue's account reading,
//!   which lags by seconds and, on an account running two sleeves, is the sum
//!   of both. A maker quoting off the account reading goes on offering the
//!   same side it has just been filled on until the reading catches up, which
//!   is the exact behaviour that turns a maker into a position.
//! - **A fill is a wake.** After one, inventory has changed and a quote has
//!   left the book, so the quotes are rebuilt then and there rather than on
//!   the next price.
//! - **A name another sleeve is holding is left alone.** There is one venue
//!   stop per position, so two sleeves in one symbol would have one stop
//!   between them, set by whoever placed the last opening order.
//!
//! The live path prices from the reconstructed L50 book, aggressor trades,
//! short-horizon movement and inventory. Queue value raises the bar for
//! moving a good resting order. The old midpoint path remains available to
//! replay old configs exactly; neither path is evidence of profit until a
//! forward run grades its fills and markouts.

use std::collections::{HashMap, HashSet, VecDeque};

use engine_types::{
    BookLevel, EngineEvent, Feed, InstrumentRule, Intent, MarketEvent, OrderKind,
    OrderUpdate, QuoteFillFeatures, Side, StopSpec, Strategy, StrategyCtx, StrategyId,
    Subscription, SymbolId, TimeInForce,
};

use super::plan::{
    executable_quote_px, flow_score, plan_quotes_protected, price_rule, queue_ahead, quote_stop_px,
    reduce_micro, MicroRules, MicroState, QuoteRules, QuoteStep, Resting, SignalInput,
};
use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "quoter";

const QUOTE_TAG: &str = "quote";

/// How often the same order may be asked to cancel.
///
/// An order stays in this strategy's own book until the log records it
/// cancelled, and the venue takes a round trip to say so. Every price in
/// between would otherwise ask again, and a liquid
/// symbol delivers tens of prices a second: one order, tens of signed venue
/// calls, straight into the venue's order-rate limit and blocking the loop
/// each time.
///
/// Paced rather than latched, for the reason the engine's own working
/// supervisor paces it (`engine-core/src/working/plan.rs`): a cancel the venue
/// refused leaves the order resting, and a strategy that asked once and never
/// again would leave it there for good.
const CANCEL_AGAIN_AFTER_NS: u64 = 1_000_000_000;
const FAST_FILL_MEMORY: usize = 8192;

/// What this strategy has asked the venue to do about one of its orders.
///
/// It has to remember because market events can arrive while the venue's amend
/// acknowledgement is still in flight. The engine ledger reserves the
/// conservative old/new price and resolves it when the venue answers, but the
/// strategy must still suppress duplicate requests during that round trip.
///
/// The engine's own working supervisor keeps the same short-lived request
/// memory for the same reason.
#[derive(Copy, Clone, Debug)]
struct Asked {
    /// When we last asked for anything about this order.
    at_ns: u64,
    /// The price we last asked it to move to, when we have.
    moved_to: Option<f64>,
}

pub struct Quoter {
    id: StrategyId,
    /// The venue's spellings, and the ids once the engine has interned them.
    /// Kept in step by position, so `ids[i]` is `symbol_names[i]`.
    symbol_names: Vec<String>,
    ids: Vec<Option<SymbolId>>,
    rules: QuoteRules,
    micro_rules: MicroRules,
    quote_enabled: bool,
    micro: HashMap<SymbolId, MicroState>,
    /// Scratch, kept between wakes so reading our own book allocates nothing
    /// on the hot path.
    working: Vec<Resting>,
    /// What we have already asked the venue about each order. Pruned every
    /// pass to what is still working, so it cannot outgrow the book.
    asked: HashMap<String, Asked>,
    /// Quantity the fast fill stream has reported before the authoritative
    /// fee-bearing fill reaches the engine's durable position ledger.
    fast_inventory: HashMap<SymbolId, f64>,
    fast_fills: HashMap<String, (SymbolId, f64)>,
    fast_fill_order: VecDeque<String>,
    /// A reduce-only market exit has been emitted for this symbol. It stays
    /// here through partial fills so a busy market-data stream cannot emit a
    /// second exit while the first one is still working.
    flatten_pending: HashSet<SymbolId>,
}

impl Quoter {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&[
            "symbols",
            "half_spread_bps",
            "requote_bps",
            "skew_bps",
            "qty",
            "max_position",
            "stop_loss_fraction",
            "maker_fee_bps",
            "min_edge_bps",
            "volatility_multiplier",
            "toxicity_bps",
            "book_lean_bps",
            "trade_lean_bps",
            "signal_half_life_ms",
            "flow_fast_half_life_ms",
            "flow_slow_half_life_ms",
            "flow_fast_weight",
            "flow_slow_weight",
            "flow_response_bps",
            "flow_max_widen_bps",
            "flow_pull_score",
            "flow_depth_bps",
            "flow_volatility_depth_multiplier",
            "flow_max_score",
            "queue_reprice_edge_bps",
            "qty_usdt",
            "max_position_usdt",
            "quote_enabled",
        ])?;

        let symbol_names = p.strings("symbols")?;
        if symbol_names.is_empty() {
            return Err(p.invalid("symbols", "expected at least one venue symbol, got none"));
        }
        if let Some(blank) = symbol_names.iter().position(|s| s.is_empty()) {
            return Err(p.invalid(
                "symbols",
                format!("entry {blank} is an empty string, which is not a venue symbol"),
            ));
        }
        let half_spread = p.positive("half_spread_bps")? / 10_000.0;
        let requote = p.positive("requote_bps")? / 10_000.0;
        // A quote that has to move further than it sits from the centre would
        // never be moved at all, which is a maker standing still in a moving
        // market.
        if requote >= half_spread {
            return Err(p.invalid(
                "requote_bps",
                "expected less than half_spread_bps; a tolerance wider than the quote's own \
                 distance from the centre means a quote is never moved",
            ));
        }
        // Absent means no lean: quote symmetrically whatever is held, which
        // is the strategy exactly as it was before the lean existed. Present
        // and it must be a real number, the same way every other optional dial
        // in this crate reads.
        let skew = p.opt_positive("skew_bps")?.unwrap_or(0.0) / 10_000.0;
        if skew > half_spread {
            return Err(p.invalid(
                "skew_bps",
                "expected no more than half_spread_bps; a lean wider than the quote's own \
                 half-spread would put the far side of the quote through the market at full \
                 inventory, which is a taker",
            ));
        }
        let stop_loss_fraction = p.positive("stop_loss_fraction")?;
        if stop_loss_fraction >= 1.0 {
            return Err(p.invalid(
                "stop_loss_fraction",
                "expected a fraction below 1; a stop at or past 100% is not a stop",
            ));
        }

        let qty = p.opt_positive("qty")?;
        let qty_usdt = p.opt_positive("qty_usdt")?;
        if qty.is_some() == qty_usdt.is_some() {
            return Err(p.invalid("qty", "set exactly one of qty or qty_usdt"));
        }
        let max_position = p.opt_positive("max_position")?;
        let max_position_usdt = p.opt_positive("max_position_usdt")?;
        if max_position.is_some() == max_position_usdt.is_some() {
            return Err(p.invalid(
                "max_position",
                "set exactly one of max_position or max_position_usdt",
            ));
        }

        let maker_fee = p.opt_nonnegative("maker_fee_bps")?.unwrap_or(0.0) / 10_000.0;
        let min_edge = p.opt_nonnegative("min_edge_bps")?.unwrap_or(0.0) / 10_000.0;
        let volatility_multiplier = p.opt_nonnegative("volatility_multiplier")?.unwrap_or(0.0);
        let toxicity = p.opt_nonnegative("toxicity_bps")?.unwrap_or(0.0) / 10_000.0;
        let book_lean = p.opt_nonnegative("book_lean_bps")?.unwrap_or(0.0) / 10_000.0;
        let trade_lean = p.opt_nonnegative("trade_lean_bps")?.unwrap_or(0.0) / 10_000.0;
        let signal_half_life_ns = p
            .opt_positive("signal_half_life_ms")?
            .unwrap_or(250.0)
            .mul_add(1_000_000.0, 0.0)
            .round() as u64;
        let flow_fast_half_life_ns = p
            .opt_positive("flow_fast_half_life_ms")?
            .unwrap_or(250.0)
            .mul_add(1_000_000.0, 0.0)
            .round() as u64;
        let flow_slow_half_life_ns = p
            .opt_positive("flow_slow_half_life_ms")?
            .unwrap_or(3_000.0)
            .mul_add(1_000_000.0, 0.0)
            .round() as u64;
        let flow_fast_weight = p.opt_nonnegative("flow_fast_weight")?.unwrap_or(0.65);
        let flow_slow_weight = p.opt_nonnegative("flow_slow_weight")?.unwrap_or(0.35);
        let flow_response = p.opt_nonnegative("flow_response_bps")?.unwrap_or(0.0) / 10_000.0;
        let flow_max_widen = p.opt_nonnegative("flow_max_widen_bps")?.unwrap_or(8.0) / 10_000.0;
        let flow_pull_score = p.opt_positive("flow_pull_score")?;
        let flow_depth_bps = p.opt_positive("flow_depth_bps")?.unwrap_or(10.0);
        let flow_volatility_depth_multiplier = p
            .opt_nonnegative("flow_volatility_depth_multiplier")?
            .unwrap_or(2.0);
        let flow_max_score = p.opt_positive("flow_max_score")?.unwrap_or(4.0);
        let flow_enabled = flow_response > 0.0 || flow_pull_score.is_some();
        if flow_enabled && flow_slow_half_life_ns <= flow_fast_half_life_ns {
            return Err(p.invalid(
                "flow_slow_half_life_ms",
                "expected longer than flow_fast_half_life_ms",
            ));
        }
        if flow_enabled && flow_fast_weight + flow_slow_weight <= 0.0 {
            return Err(p.invalid(
                "flow_fast_weight",
                "fast and slow flow weights cannot both be zero",
            ));
        }
        let queue_reprice_edge =
            p.opt_nonnegative("queue_reprice_edge_bps")?.unwrap_or(0.0) / 10_000.0;
        let adaptive = maker_fee > 0.0
            || min_edge > 0.0
            || volatility_multiplier > 0.0
            || toxicity > 0.0
            || book_lean > 0.0
            || trade_lean > 0.0
            || flow_enabled
            || queue_reprice_edge > 0.0
            || qty_usdt.is_some()
            || max_position_usdt.is_some();
        let quote_enabled = p.bool_or("quote_enabled", true)?;

        let ids = vec![None; symbol_names.len()];
        Ok(Self {
            id,
            symbol_names,
            ids,
            rules: QuoteRules {
                half_spread,
                requote_tolerance: requote,
                skew,
                qty: qty.unwrap_or(0.0),
                // In base units, so it is a per-symbol ceiling and the same
                // number means different money on different coins. That is the
                // contract this plug was written to; a notional ceiling would
                // be a different strategy.
                max_position: max_position.unwrap_or(0.0),
                stop_loss_fraction,
            },
            micro_rules: MicroRules {
                maker_fee,
                min_edge,
                volatility_multiplier,
                toxicity,
                book_lean,
                trade_lean,
                signal_half_life_ns,
                flow_fast_half_life_ns,
                flow_slow_half_life_ns,
                flow_fast_weight,
                flow_slow_weight,
                flow_response,
                flow_max_widen,
                flow_pull_score,
                flow_depth_bps,
                flow_volatility_depth_multiplier,
                flow_max_score,
                queue_reprice_edge,
                qty_usdt,
                max_position_usdt,
                adaptive,
            },
            quote_enabled,
            micro: HashMap::new(),
            working: Vec::new(),
            asked: HashMap::new(),
            fast_inventory: HashMap::new(),
            fast_fills: HashMap::new(),
            fast_fill_order: VecDeque::new(),
            flatten_pending: HashSet::new(),
        })
    }

    /// Fill in the ids the engine handed out. Symbols are interned at boot
    /// from the union of every strategy's subscriptions, so this resolves on
    /// the first event and is kept from then on.
    fn resolve(&mut self, ctx: &dyn StrategyCtx) {
        for (index, name) in self.symbol_names.iter().enumerate() {
            if self.ids[index].is_none() {
                self.ids[index] = ctx.symbol_id(name);
            }
        }
    }

    fn mine(&self, symbol: SymbolId) -> bool {
        self.ids.contains(&Some(symbol))
    }

    fn reduce_signal(&mut self, symbol: SymbolId, input: SignalInput<'_>) {
        let prior = self.micro.get(&symbol).copied().unwrap_or_default();
        self.micro
            .insert(symbol, reduce_micro(prior, input, self.micro_rules));
    }

    /// Ask the venue to pull an order, unless we have just asked.
    fn pull(&mut self, symbol: SymbolId, id: &str, now_ns: u64, ctx: &mut dyn StrategyCtx) {
        if let Some(asked) = self.asked.get(id) {
            if now_ns.saturating_sub(asked.at_ns) < CANCEL_AGAIN_AFTER_NS {
                return;
            }
        }
        self.asked.insert(
            id.to_string(),
            Asked {
                at_ns: now_ns,
                moved_to: None,
            },
        );
        ctx.cancel(symbol, id);
    }

    /// Ask the venue to move an order, unless we have already asked it to go
    /// to this price.
    ///
    /// The test is against the price we asked for, not the one the ledger
    /// reports, because the ledger's is the price the order was sent at and
    /// never changes. Half a tick is the same threshold the engine's own
    /// supervisor uses to decide a move is not worth the queue position it
    /// gives up (`working/plan.rs`).
    fn move_to(
        &mut self,
        symbol: SymbolId,
        id: &str,
        px: f64,
        tick: f64,
        now_ns: u64,
        ctx: &mut dyn StrategyCtx,
    ) {
        if let Some(asked) = self.asked.get(id) {
            if asked
                .moved_to
                .is_some_and(|at| (at - px).abs() < tick.max(0.0) * 0.5)
            {
                return;
            }
        }
        self.asked.insert(
            id.to_string(),
            Asked {
                at_ns: now_ns,
                moved_to: Some(px),
            },
        );
        ctx.amend(
            symbol,
            id,
            engine_types::AmendSpec {
                px: Some(px),
                qty: None,
            },
        );
    }

    fn requote(&mut self, symbol: SymbolId, ctx: &mut dyn StrategyCtx) {
        // Our own working orders on this symbol, as the planner wants them.
        // The buffer is reused between wakes, so this allocates only when a
        // quote's id is longer than the last one that lived in the slot.
        self.working.clear();
        let mut out = Vec::new();
        ctx.resting(&mut out);
        // Pruned against the WHOLE book, every symbol of it, before it is
        // narrowed below. Pruning against one symbol's slice threw away every
        // other symbol's record, so a price in one name un-paced the next
        // price in another -- the loop this exists to stop, back again on any
        // config with two symbols in it.
        if !self.asked.is_empty() {
            let book = &out;
            self.asked
                .retain(|id, _| book.iter().any(|o| o.client_order_id == id));
        }
        let position = ctx.my_position(symbol)
            + self
                .fast_inventory
                .get(&symbol)
                .copied()
                .unwrap_or_default();
        let position_tolerance = (position.abs() * 1e-9).max(1e-12);
        let unsafe_quotes: Vec<String> = out
            .iter()
            .filter(|order| order.symbol == symbol)
            .filter(|order| order.px().is_some())
            .filter(|order| {
                let should_reduce = match order.side {
                    Side::Buy => position < -position_tolerance,
                    Side::Sell => position > position_tolerance,
                };
                order.reduce_only != should_reduce
                    || (should_reduce
                        && order.remaining_qty() > position.abs() + position_tolerance)
            })
            .map(|order| order.client_order_id.to_string())
            .collect();
        for order in out.iter().filter(|o| o.symbol == symbol) {
            if let Some(px) = order.px() {
                self.working.push(Resting {
                    client_order_id: order.client_order_id.to_string(),
                    side: order.side,
                    px,
                });
            }
        }
        drop(out);
        let now_ns = ctx.now_ns();

        // Somebody else's name. Pull anything of ours that is resting in it
        // and leave it: there is one venue stop per position, so two sleeves
        // here would have one stop between them.
        if ctx.foreign_position(symbol) {
            let mine: Vec<String> = self
                .working
                .iter()
                .map(|o| o.client_order_id.clone())
                .collect();
            for id in mine {
                self.pull(symbol, &id, now_ns, ctx);
            }
            return;
        }

        if !self.quote_enabled {
            let mine: Vec<String> = self
                .working
                .iter()
                .map(|order| order.client_order_id.clone())
                .collect();
            for id in mine {
                self.pull(symbol, &id, now_ns, ctx);
            }
            if !self.working.is_empty() || self.flatten_pending.contains(&symbol) {
                return;
            }
            if position.abs() > 1e-12 {
                self.flatten_pending.insert(symbol);
                ctx.place(Intent {
                    strategy: self.id,
                    symbol,
                    side: if position > 0.0 {
                        Side::Sell
                    } else {
                        Side::Buy
                    },
                    qty: position.abs(),
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: true,
                    tag: "quote-drain".to_string(),
                    decided_ns: now_ns,
                    work: None,
                    leverage: None,
                });
            }
            return;
        }

        // A flat book has two opening quotes. Once either fills, the other
        // side is an exit, not permission to pass through flat and open the
        // opposite position. Pull the stale shape first and wait for its
        // terminal update before replacing it. Cancel and replace in one wake
        // would leave both orders live during the venue round trip.
        if !unsafe_quotes.is_empty() {
            for id in unsafe_quotes {
                self.pull(symbol, &id, now_ns, ctx);
            }
            return;
        }

        let Some(rule) = ctx.instrument(symbol) else {
            return;
        };
        let quote = *ctx.quote(symbol);
        let depth = *ctx.depth(symbol);
        // This strategy's own fills, not the account's reading. See the header.
        let priced = price_rule(
            quote,
            &depth,
            self.micro.get(&symbol),
            &self.working,
            self.rules,
            self.micro_rules,
        );
        let steps = plan_quotes_protected(
            quote.bid_px,
            quote.ask_px,
            priced.fair_px,
            position,
            &self.working,
            priced.rules,
            priced.protection,
        );
        for step in steps {
            match step {
                QuoteStep::Place { side, px, qty, .. } => {
                    let px =
                        executable_quote_px(side, px, quote.bid_px, quote.ask_px, rule.tick_size);
                    let reduce_only = match side {
                        Side::Buy => position < -position_tolerance,
                        Side::Sell => position > position_tolerance,
                    };
                    let qty = if reduce_only {
                        qty.min(position.abs())
                    } else {
                        qty
                    };
                    let stop = (!reduce_only).then(|| StopSpec {
                        trigger_px: quote_stop_px(px, side, priced.rules.stop_loss_fraction),
                    });
                    self.place(symbol, side, px, qty, stop, reduce_only, &rule, ctx)
                }
                QuoteStep::Move {
                    client_order_id,
                    px,
                } => {
                    let side = self
                        .working
                        .iter()
                        .find(|order| order.client_order_id == client_order_id)
                        .map(|order| order.side)
                        .unwrap_or(Side::Buy);
                    let px =
                        executable_quote_px(side, px, quote.bid_px, quote.ask_px, rule.tick_size);
                    self.move_to(symbol, &client_order_id, px, rule.tick_size, now_ns, ctx)
                }
                QuoteStep::Pull { client_order_id } => {
                    self.pull(symbol, &client_order_id, now_ns, ctx)
                }
            }
        }
    }

    fn pull_all_on_feed_reset(&mut self, ctx: &mut dyn StrategyCtx) {
        self.micro.clear();
        self.fast_inventory.clear();
        self.fast_fills.clear();
        self.fast_fill_order.clear();
        let mut resting = Vec::new();
        ctx.resting(&mut resting);
        let mine: Vec<(SymbolId, String)> = resting
            .iter()
            .filter(|order| self.mine(order.symbol))
            .map(|order| (order.symbol, order.client_order_id.to_string()))
            .collect();
        let now_ns = ctx.now_ns();
        for (symbol, id) in mine {
            self.pull(symbol, &id, now_ns, ctx);
        }
    }

    fn note_fast_fill(&mut self, exec_id: &str, symbol: SymbolId, side: Side, qty: f64) {
        if self.fast_fills.contains_key(exec_id) {
            return;
        }
        let signed = match side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        self.fast_fills
            .insert(exec_id.to_string(), (symbol, signed));
        self.fast_fill_order.push_back(exec_id.to_string());
        *self.fast_inventory.entry(symbol).or_default() += signed;
        while self.fast_fill_order.len() > FAST_FILL_MEMORY {
            if let Some(old) = self.fast_fill_order.pop_front() {
                if let Some((old_symbol, old_signed)) = self.fast_fills.remove(&old) {
                    *self.fast_inventory.entry(old_symbol).or_default() -= old_signed;
                }
            }
        }
    }

    fn settle_fast_fill(&mut self, exec_id: &str) {
        let Some((symbol, signed)) = self.fast_fills.remove(exec_id) else {
            return;
        };
        *self.fast_inventory.entry(symbol).or_default() -= signed;
        if self
            .fast_inventory
            .get(&symbol)
            .is_some_and(|quantity| quantity.abs() < 1e-12)
        {
            self.fast_inventory.remove(&symbol);
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn record_fill_features(
        &self,
        exec_id: &str,
        client_order_id: &str,
        symbol: SymbolId,
        side: Side,
        px: f64,
        is_maker: bool,
        recv_ns: u64,
        ctx: &mut dyn StrategyCtx,
    ) {
        let quote = *ctx.quote(symbol);
        let depth = *ctx.depth(symbol);
        let state = self.micro.get(&symbol).filter(|state| state.has_depth);
        let spread_bps = (quote.bid_px > 0.0 && quote.ask_px >= quote.bid_px).then(|| {
            let mid = (quote.bid_px + quote.ask_px) / 2.0;
            (quote.ask_px - quote.bid_px) / mid * 10_000.0
        });
        let queue_ahead_usdt = state.and_then(|_| {
            (px > 0.0 && depth.bid_len > 0 && depth.ask_len > 0)
                .then(|| queue_ahead(&depth, side, px) * px)
        });
        let has_flow = state.is_some_and(|state| state.has_flow);
        ctx.emit(engine_types::Action::RecordQuoteFill {
            features: QuoteFillFeatures {
                strategy: self.id,
                symbol,
                exec_id: exec_id.to_string(),
                client_order_id: client_order_id.to_string(),
                side,
                is_maker,
                recv_ns,
                flow_fast: state.filter(|_| has_flow).map(|state| state.flow_fast),
                flow_slow: state.filter(|_| has_flow).map(|state| state.flow_slow),
                flow_score: state
                    .filter(|_| has_flow)
                    .map(|state| flow_score(state, self.micro_rules)),
                last_depth_ratio: state
                    .filter(|_| has_flow)
                    .map(|state| state.last_depth_ratio),
                same_side_depth_usdt: state.and_then(|state| {
                    let depth = match side {
                        Side::Buy => state.bid_depth_usdt,
                        Side::Sell => state.ask_depth_usdt,
                    };
                    (depth > 0.0).then_some(depth)
                }),
                spread_bps,
                volatility_bps: state.map(|state| state.variance.max(0.0).sqrt() * 10_000.0),
                queue_ahead_usdt,
            },
        });
    }

    #[allow(clippy::too_many_arguments)]
    fn place(
        &self,
        symbol: SymbolId,
        side: Side,
        px: f64,
        qty: f64,
        stop: Option<StopSpec>,
        reduce_only: bool,
        rule: &InstrumentRule,
        ctx: &mut dyn StrategyCtx,
    ) {
        // A quote below the venue's minimum is not a quote. The engine
        // quantizes again before sending; this only avoids emitting an intent
        // that could never become an order.
        if qty * px < rule.min_notional || qty < rule.min_qty {
            return;
        }
        ctx.place(Intent {
            strategy: self.id,
            symbol,
            side,
            qty,
            // Post-only: a maker that crosses has stopped being a maker, and
            // pays the taker fee for the privilege.
            kind: OrderKind::Limit {
                px,
                tif: TimeInForce::PostOnly,
            },
            stop,
            reduce_only,
            tag: QUOTE_TAG.to_string(),
            decided_ns: ctx.now_ns(),
            // A maker already places where it means to and moves its own
            // quotes; handing them to the engine's supervisor as well would
            // give one order two minds.
            work: None,
            // This plug sizes in quantity, not margin, so it leaves the
            // symbol's leverage alone.
            leverage: None,
        });
    }
}

impl Strategy for Quoter {
    fn name(&self) -> &str {
        NAME
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        self.symbol_names
            .iter()
            .flat_map(|symbol| {
                [Feed::Quote, Feed::Depth, Feed::Trades]
                    .into_iter()
                    .map(move |feed| Subscription {
                        symbol: symbol.clone(),
                        feed,
                    })
            })
            .collect()
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        self.resolve(&*ctx);
        let symbol = match event {
            EngineEvent::Market(MarketEvent::Depth { symbol, depth }) => {
                self.reduce_signal(
                    *symbol,
                    SignalInput::Depth {
                        bids: &depth.bids[..depth.bid_len as usize],
                        asks: &depth.asks[..depth.ask_len as usize],
                        recv_ns: depth.recv_ns,
                    },
                );
                *symbol
            }
            EngineEvent::Market(MarketEvent::Trades { symbol, trades }) => {
                self.reduce_signal(*symbol, SignalInput::Trades(*trades));
                *symbol
            }
            // The touch, on its own faster topic. The venue publishes it
            // about twice as often as the deep book, and it is what the
            // quoted price is built from, so taking it here is up to one
            // publication interval of staleness removed from every quote.
            // The book and queue terms stay on the deep book, which is the
            // only thing that carries them.
            EngineEvent::Market(MarketEvent::Quote { symbol, quote }) => {
                self.reduce_signal(
                    *symbol,
                    SignalInput::Touch {
                        bid: BookLevel {
                            px: quote.bid_px,
                            qty: quote.bid_qty,
                        },
                        ask: BookLevel {
                            px: quote.ask_px,
                            qty: quote.ask_qty,
                        },
                        recv_ns: quote.recv_ns,
                    },
                );
                *symbol
            }
            EngineEvent::Market(MarketEvent::FeedReset { .. }) => {
                self.pull_all_on_feed_reset(ctx);
                return;
            }
            // A fill changed the inventory and took a quote out of the book.
            // Waiting for the next price to notice would leave the maker
            // one-sided for as long as the market is quiet — which is exactly
            // when it is least able to afford it.
            EngineEvent::Order(OrderUpdate::FastFill {
                exec_id,
                symbol,
                side,
                qty,
                ..
            }) => {
                self.note_fast_fill(exec_id, *symbol, *side, *qty);
                *symbol
            }
            EngineEvent::Order(OrderUpdate::Fill {
                exec_id,
                client_order_id,
                symbol,
                side,
                px,
                is_maker,
                recv_ns,
                ..
            }) => {
                self.record_fill_features(
                    exec_id,
                    client_order_id,
                    *symbol,
                    *side,
                    *px,
                    *is_maker,
                    *recv_ns,
                    ctx,
                );
                self.settle_fast_fill(exec_id);
                if ctx
                    .order_facts(client_order_id)
                    .is_some_and(|order| order.reduce_only && order.filled_qty + 1e-12 >= order.qty)
                {
                    self.flatten_pending.remove(symbol);
                }
                *symbol
            }
            EngineEvent::Order(OrderUpdate::Reject {
                client_order_id, ..
            })
            | EngineEvent::Order(OrderUpdate::Cancelled {
                client_order_id, ..
            }) => {
                let Some(order) = ctx.order_facts(client_order_id) else {
                    return;
                };
                if order.reduce_only {
                    self.flatten_pending.remove(&order.symbol);
                }
                order.symbol
            }
            EngineEvent::IntentRefused {
                symbol,
                reduce_only: true,
                ..
            } => {
                self.flatten_pending.remove(symbol);
                *symbol
            }
            _ => return,
        };
        if self.mine(symbol) {
            self.requote(symbol, ctx);
        }
    }
}
