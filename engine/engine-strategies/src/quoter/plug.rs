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
//! ## What this is still not
//!
//! There is no adverse-selection model and no queue-position estimate, and it
//! has never been graded. What it now has is the engine's own measurement:
//! every fill it takes is priced and marked out, so `engine fills` can say
//! whether the quoting pays.

use std::collections::HashMap;

use engine_types::{
    EngineEvent, Feed, InstrumentRule, Intent, MarketEvent, OrderKind, OrderUpdate, Side, StopSpec,
    Strategy, StrategyCtx, StrategyId, Subscription, SymbolId, TimeInForce,
};

use super::plan::{plan_quotes, QuoteRules, QuoteStep, Resting};
use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "quoter";

const QUOTE_TAG: &str = "quote";

/// How often the same order may be asked to cancel.
///
/// An order stays in this strategy's own book until the log records it
/// cancelled, and the venue takes a round trip to say so -- about 175 ms from
/// this box. Every price in between would otherwise ask again, and a liquid
/// symbol delivers tens of prices a second: one order, tens of signed venue
/// calls, straight into the venue's order-rate limit and blocking the loop
/// each time.
///
/// Paced rather than latched, for the reason the engine's own working
/// supervisor paces it (`engine-core/src/working/plan.rs`): a cancel the venue
/// refused leaves the order resting, and a strategy that asked once and never
/// again would leave it there for good.
const CANCEL_AGAIN_AFTER_NS: u64 = 1_000_000_000;

/// What this strategy has asked the venue to do about one of its orders.
///
/// It has to remember, because the engine's order ledger does not. That ledger
/// records the price an order was *sent* at and has no `AmendSent` arm at all
/// (`engine-core/src/inflight.rs`), so `StrategyCtx::resting` reports the
/// original price for the order's whole life however many times it has been
/// moved since. A quoter measuring drift against that would find the same
/// drift on every price and ask again for ever -- worse than asking twice to
/// cancel, which at least ends when the venue confirms.
///
/// The engine's own working supervisor keeps exactly this, for exactly this
/// reason (`working.rs`, `WorkState::px` updated in `amended`).
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
    /// Scratch, kept between wakes so reading our own book allocates nothing
    /// on the hot path.
    working: Vec<Resting>,
    /// What we have already asked the venue about each order. Pruned every
    /// pass to what is still working, so it cannot outgrow the book.
    asked: HashMap<String, Asked>,
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

        let ids = vec![None; symbol_names.len()];
        Ok(Self {
            id,
            symbol_names,
            ids,
            rules: QuoteRules {
                half_spread,
                requote_tolerance: requote,
                skew,
                qty: p.positive("qty")?,
                // In base units, so it is a per-symbol ceiling and the same
                // number means different money on different coins. That is the
                // contract this plug was written to; a notional ceiling would
                // be a different strategy.
                max_position: p.positive("max_position")?,
                stop_loss_fraction,
            },
            working: Vec::new(),
            asked: HashMap::new(),
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

    /// Ask the venue to pull an order, unless we have just asked.
    fn pull(&mut self, symbol: SymbolId, id: &str, now_ns: u64, ctx: &mut dyn StrategyCtx) {
        if let Some(asked) = self.asked.get(id) {
            if now_ns.saturating_sub(asked.at_ns) < CANCEL_AGAIN_AFTER_NS {
                return;
            }
        }
        self.asked.insert(id.to_string(), Asked { at_ns: now_ns, moved_to: None });
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
            if asked.moved_to.is_some_and(|at| (at - px).abs() < tick.max(0.0) * 0.5) {
                return;
            }
        }
        self.asked.insert(id.to_string(), Asked { at_ns: now_ns, moved_to: Some(px) });
        ctx.amend(symbol, id, engine_types::AmendSpec { px: Some(px), qty: None });
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
            let mine: Vec<String> =
                self.working.iter().map(|o| o.client_order_id.clone()).collect();
            for id in mine {
                self.pull(symbol, &id, now_ns, ctx);
            }
            return;
        }

        let Some(rule) = ctx.instrument(symbol) else {
            return;
        };
        let quote = *ctx.quote(symbol);
        // This strategy's own fills, not the account's reading. See the header.
        let position = ctx.my_position(symbol);
        let steps = plan_quotes(quote.bid_px, quote.ask_px, position, &self.working, self.rules);
        for step in steps {
            match step {
                QuoteStep::Place { side, px, qty, stop_px } => {
                    self.place(symbol, side, px, qty, stop_px, &rule, ctx)
                }
                QuoteStep::Move { client_order_id, px } => {
                    self.move_to(symbol, &client_order_id, px, rule.tick_size, now_ns, ctx)
                }
                QuoteStep::Pull { client_order_id } => {
                    self.pull(symbol, &client_order_id, now_ns, ctx)
                }
            }
        }
    }

    fn pull_all_on_feed_reset(&mut self, ctx: &mut dyn StrategyCtx) {
        let mut resting = Vec::new();
        ctx.resting(&mut resting);
        let mine: Vec<(SymbolId, String)> = resting.iter()
            .filter(|order| self.mine(order.symbol))
            .map(|order| (order.symbol, order.client_order_id.to_string())).collect();
        let now_ns = ctx.now_ns();
        for (symbol, id) in mine { self.pull(symbol, &id, now_ns, ctx); }
    }

    #[allow(clippy::too_many_arguments)]
    fn place(
        &self,
        symbol: SymbolId,
        side: Side,
        px: f64,
        qty: f64,
        stop_px: f64,
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
            kind: OrderKind::Limit { px, tif: TimeInForce::PostOnly },
            stop: Some(StopSpec { trigger_px: stop_px }),
            reduce_only: false,
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
            .map(|symbol| Subscription { symbol: symbol.clone(), feed: Feed::Quote })
            .collect()
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        self.resolve(&*ctx);
        let symbol = match event {
            EngineEvent::Market(MarketEvent::Quote { symbol, .. }) => *symbol,
            EngineEvent::Market(MarketEvent::FeedReset { .. }) => {
                self.pull_all_on_feed_reset(ctx);
                return;
            }
            // A fill changed the inventory and took a quote out of the book.
            // Waiting for the next price to notice would leave the maker
            // one-sided for as long as the market is quiet — which is exactly
            // when it is least able to afford it.
            EngineEvent::Order(OrderUpdate::Fill { symbol, .. }) => *symbol,
            _ => return,
        };
        if self.mine(symbol) {
            self.requote(symbol, ctx);
        }
    }
}
