//! `quoter`: hold a two-sided quote around mid, and keep it there.
//!
//! The other kind of strategy. It decides in the loop rather than following a
//! book, and it needs the whole order vocabulary: quotes have to be pulled
//! when the book breaks or inventory fills, and moved when the market walks
//! away. Placing alone is not market making.
//!
//! The arithmetic — where a quote belongs, when it is worth moving, which
//! side stops being quoted — is [`super::plan::plan_quotes`], and it is pure.
//! This plug only turns its answers into actions and remembers which order is
//! which.
//!
//! ## What this is not
//!
//! There is no adverse-selection model, no queue-position estimate, no
//! skewing of the quote by inventory beyond stopping a side outright, and no
//! measurement of whether any of it makes money. It is a working maker on the
//! engine's contracts, not a strategy anybody has graded.

use engine_types::{
    EngineEvent, Feed, InstrumentRule, Intent, MarketEvent, OrderKind, Side, StopSpec, Strategy,
    StrategyCtx, StrategyId, Subscription, SymbolId, TimeInForce,
};

use super::plan::{plan_quotes, QuoteRules, QuoteStep, Resting};
use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "quoter";

const QUOTE_TAG: &str = "quote";

pub struct Quoter {
    id: StrategyId,
    symbol_name: String,
    symbol: Option<SymbolId>,
    rules: QuoteRules,
    /// Scratch, kept between wakes so reading our own book allocates nothing
    /// on the hot path.
    working: Vec<(String, Side, f64)>,
}

impl Quoter {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&[
            "symbol",
            "half_spread_bps",
            "requote_bps",
            "qty",
            "max_position",
            "stop_loss_fraction",
        ])?;

        let symbol_name = p.string("symbol")?;
        if symbol_name.is_empty() {
            return Err(p.invalid("symbol", "expected a venue symbol, got an empty string"));
        }
        let half_spread = p.positive("half_spread_bps")? / 10_000.0;
        let requote = p.positive("requote_bps")? / 10_000.0;
        // A quote that has to move further than it sits from mid would never
        // be moved at all, which is a maker standing still in a moving market.
        if requote >= half_spread {
            return Err(p.invalid(
                "requote_bps",
                "expected less than half_spread_bps; a tolerance wider than the quote's own \
                 distance from mid means a quote is never moved",
            ));
        }
        let stop_loss_fraction = p.positive("stop_loss_fraction")?;
        if stop_loss_fraction >= 1.0 {
            return Err(p.invalid(
                "stop_loss_fraction",
                "expected a fraction below 1; a stop at or past 100% is not a stop",
            ));
        }

        Ok(Self {
            id,
            symbol_name,
            symbol: None,
            rules: QuoteRules {
                half_spread,
                requote_tolerance: requote,
                qty: p.positive("qty")?,
                max_position: p.positive("max_position")?,
                stop_loss_fraction,
            },
            working: Vec::new(),
        })
    }

    fn resolve(&mut self, ctx: &dyn StrategyCtx) -> Option<SymbolId> {
        if self.symbol.is_none() {
            self.symbol = ctx.symbol_id(&self.symbol_name);
        }
        self.symbol
    }

    fn requote(&mut self, symbol: SymbolId, ctx: &mut dyn StrategyCtx) {
        let quote = *ctx.quote(symbol);
        let position = match ctx.position(symbol) {
            Some(p) if p.side == Side::Buy => p.qty,
            Some(p) => -p.qty,
            None => 0.0,
        };

        // Our own working orders on this symbol, as the planner wants them.
        self.working.clear();
        let mut out = Vec::new();
        ctx.resting(&mut out);
        for order in out.iter().filter(|o| o.symbol == symbol) {
            if let Some(px) = order.px() {
                self.working.push((order.client_order_id.to_string(), order.side, px));
            }
        }
        drop(out);
        let resting: Vec<Resting> = self
            .working
            .iter()
            .map(|(id, side, px)| Resting { client_order_id: id.clone(), side: *side, px: *px })
            .collect();

        let Some(rule) = ctx.instrument(symbol) else {
            return;
        };
        for step in plan_quotes(quote.bid_px, quote.ask_px, position, &resting, self.rules) {
            match step {
                QuoteStep::Place { side, px, qty, stop_px } => {
                    self.place(symbol, side, px, qty, stop_px, &rule, ctx)
                }
                QuoteStep::Move { client_order_id, px } => {
                    ctx.amend(
                        symbol,
                        &client_order_id,
                        engine_types::AmendSpec { px: Some(px), qty: None },
                    );
                }
                QuoteStep::Pull { client_order_id } => ctx.cancel(symbol, &client_order_id),
            }
        }
    }

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
        vec![Subscription { symbol: self.symbol_name.clone(), feed: Feed::Quote }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event else {
            return;
        };
        let Some(mine) = self.resolve(&*ctx) else {
            return;
        };
        if *symbol == mine {
            self.requote(mine, ctx);
        }
    }
}
