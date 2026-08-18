//! The wiring. Config in, market picture in, actions out — and no decisions.
//!
//! Everything here is the same in every strategy, which is why it is worth
//! copying rather than writing again. The only interesting line is the call
//! to [`plan`].

use engine_types::{
    Feed, InstrumentRule, Intent, MarketEvent, OrderKind, Side, StopSpec, Strategy,
    StrategyCtx, StrategyId, Subscription, SymbolId, TimeInForce,
};

use super::plan::{Held, Rules, Step, plan};
use crate::BuildError;
use crate::params::Params;

/// The name a config block uses. Change it when you copy this.
pub const NAME: &str = "template";

/// Every intent carries a short tag that shows up in the log next to the
/// order. Make it say which strategy and which reason, so a log line is
/// readable a month later without the code in front of you.
const TAG: &str = "template";

pub struct Template {
    id: StrategyId,
    /// The venue's spelling, kept because ids are not known until the engine
    /// has interned the symbol — which happens after the strategy is built.
    symbol_name: String,
    symbol: Option<SymbolId>,
    rules: Rules,
}

impl Template {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;

        let symbol_name = p.string("symbol")?;
        if symbol_name.is_empty() {
            return Err(p.invalid("symbol", "expected a venue symbol, got an empty string"));
        }
        let side = p.string("side")?;
        let magnitude = p.positive("target_qty")?;
        let target_qty = match side.as_str() {
            "buy" => magnitude,
            "sell" => -magnitude,
            other => {
                return Err(p.invalid("side", format!("expected \"buy\" or \"sell\", got {other:?}")))
            }
        };
        let tolerance_fraction = p.positive("tolerance_fraction")?;
        if tolerance_fraction >= 1.0 {
            return Err(p.invalid(
                "tolerance_fraction",
                "expected a fraction below 1; a tolerance of the whole position means the \
                 position is never corrected",
            ));
        }
        let stop_loss_fraction = p.positive("stop_loss_fraction")?;
        if stop_loss_fraction >= 1.0 {
            return Err(p.invalid(
                "stop_loss_fraction",
                "expected a fraction below 1; a stop at or past 100% is not a stop",
            ));
        }

        // Last, and every parameter above must already have been read. A key
        // this strategy does not know is a typo in a config the research
        // system wrote, and a typo that is ignored is a silent change of
        // behaviour.
        p.reject_unknown(&["symbol", "side", "target_qty", "tolerance_fraction", "stop_loss_fraction"])?;

        Ok(Self { id, symbol_name, symbol: None, rules: Rules { target_qty, tolerance_fraction, stop_loss_fraction } })
    }

    /// Symbols are interned by the engine at boot from the union of every
    /// strategy's subscriptions, so the id is not available while the plug is
    /// being built. Resolve on first use and keep it.
    fn resolve(&mut self, ctx: &dyn StrategyCtx) -> Option<SymbolId> {
        if self.symbol.is_none() {
            self.symbol = ctx.symbol_id(&self.symbol_name);
        }
        self.symbol
    }

    fn act(&mut self, symbol: SymbolId, ctx: &mut dyn StrategyCtx) {
        // One order at a time. An order already working is the previous
        // answer to this same question, and sending another before it
        // finishes is how a position ends up at twice its target.
        let mut working = Vec::new();
        ctx.resting(&mut working);
        if working.iter().any(|o| o.symbol == symbol) {
            return;
        }
        drop(working);

        let Some(rule) = ctx.instrument(symbol) else {
            // No tick or step for this symbol means nothing can be sent for
            // it, so there is nothing to decide.
            return;
        };
        let quote = ctx.quote(symbol);
        let mark_px = mid(quote.bid_px, quote.ask_px);
        let held = ctx.position(symbol).map(|p| Held {
            side: p.side,
            qty: p.qty,
            entry_px: p.entry_px,
        });

        match plan(held, mark_px, self.rules) {
            None => {}
            Some(Step::Enter { side, qty, stop_px }) => {
                self.send(symbol, side, qty, Some(stop_px), false, mark_px, &rule, ctx)
            }
            Some(Step::Reduce { side, qty }) => {
                self.send(symbol, side, qty, None, true, mark_px, &rule, ctx)
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn send(
        &self,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        stop_px: Option<f64>,
        reduce_only: bool,
        mark_px: f64,
        rule: &InstrumentRule,
        ctx: &mut dyn StrategyCtx,
    ) {
        // The engine quantizes again before the wire. Checking here only
        // avoids emitting an intent that could never become an order, which
        // would otherwise show up in the log as a decision the venue refused.
        if qty < rule.min_qty || qty * mark_px < rule.min_notional {
            return;
        }
        ctx.place(Intent {
            strategy: self.id,
            symbol,
            side,
            qty,
            // An entry can wait for a better price; an exit cannot. A resting
            // exit that does not fill is exposure nobody wanted, still on the
            // book, so exits cross and entries rest. Every sleeve in this
            // system works that way.
            kind: if reduce_only {
                OrderKind::Market
            } else {
                OrderKind::Limit { px: mark_px, tif: TimeInForce::Gtc }
            },
            stop: stop_px.map(|trigger_px| StopSpec { trigger_px }),
            reduce_only,
            tag: TAG.to_string(),
            decided_ns: ctx.now_ns(),
            work: None,
            // This plug sizes in quantity, not margin, so it leaves the
            // symbol's leverage alone.
            leverage: None,
        });
    }
}

fn mid(bid_px: f64, ask_px: f64) -> f64 {
    if bid_px > 0.0 && ask_px > 0.0 {
        (bid_px + ask_px) / 2.0
    } else {
        0.0
    }
}

impl Strategy for Template {
    fn name(&self) -> &str {
        NAME
    }

    /// What market data to receive. Collected once at boot across every
    /// strategy, so asking for a feed twice costs nothing.
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription { symbol: self.symbol_name.clone(), feed: Feed::Quote }]
    }

    // Override only the hooks this strategy acts on — quotes, here. Every
    // other event — order updates, timers, target books, refused intents —
    // falls through to the trait's do-nothing defaults, so a new kind of
    // event never needs an edit in a plug that ignores it.
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, .. } = event else {
            return;
        };
        let Some(mine) = self.resolve(&*ctx) else {
            return;
        };
        if *symbol == mine {
            self.act(mine, ctx);
        }
    }
}
