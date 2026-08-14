//! `target_book`: hold what the research system's latest book says to hold.
//!
//! The plug does no deciding. It keeps the newest book, asks the engine what
//! is actually held and what things cost, hands both to [`plan`], and sends
//! whatever comes back. Everything interesting about the arithmetic — the
//! dead band, the entry floor, exits before entries, a flip closing before it
//! reverses — lives in that pure function and is tested there.
//!
//! Two absences that look alike and are not:
//!
//! - **No book.** Nothing has arrived, or what arrived was unreadable. The
//!   engine does not wake this plug at all, so it holds what it holds. That
//!   is the whole reason the watcher refuses rather than delivering an empty
//!   book on a bad read.
//! - **An empty book.** A book arrived and names nothing. That is a decision
//!   to hold nothing, and everything open is exited.
//!
//! ## Known limit: the symbol list is fixed at boot
//!
//! The engine collects subscriptions once, when it starts, so this plug
//! cannot follow a symbol that first appears in a book written later — there
//! is no price for it, no instrument rule, and no `SymbolId` to place an
//! order against. The `symbols` list in the config block is therefore the
//! real universe, and a book naming anything outside it is logged and
//! skipped, not traded. Widening the universe means editing the config and
//! restarting. That is a genuine gap, not a policy.
//!
//! ## Known limit: positions are read per symbol, not per plug
//!
//! What is held comes from the account reading, and a venue holds one
//! position per symbol however many plugs are running. So two plugs
//! configured on the same symbol would each read the other's exposure as
//! their own. Give the follower symbols nobody else trades.

use engine_types::{
    EngineEvent, Feed, InstrumentRule, Intent, MarketEvent, OrderKind, StopSpec, Strategy,
    StrategyCtx, StrategyId, Subscription, SymbolId, TargetBook,
};

use super::plan::{plan, Held, PlanRules, Step, SymbolFacts, Target};
use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "target_book";

const ENTER_TAG: &str = "book-enter";
const EXIT_TAG: &str = "book-exit";
const RESIZE_TAG: &str = "book-resize";

pub struct TargetBookFollower {
    id: StrategyId,
    /// The universe, fixed at boot. See the module note above.
    symbols: Vec<String>,
    /// The newest book. `None` until one arrives, and that means no decision.
    book: Option<TargetBook>,
    rules: PlanRules,
    /// Symbols already complained about for the book in hand. This runs on
    /// every quote, so without it one unreachable name in a book writes a
    /// warning a hundred times a second until the next book lands.
    complained: Vec<String>,
}

impl TargetBookFollower {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&["symbols"])?;

        let symbols = p.strings("symbols")?;
        if symbols.is_empty() {
            return Err(p.invalid(
                "symbols",
                "expected at least one venue symbol; a follower with no universe can trade nothing",
            ));
        }

        Ok(Self {
            id,
            symbols,
            book: None,
            rules: PlanRules::FLEET,
            complained: Vec::new(),
        })
    }

    /// Work out the difference between the book and what is held, and send
    /// it. Called on a new book and on every quote: a book that could not be
    /// acted on when it arrived — no price yet, an order still working — gets
    /// another chance on the next price.
    fn act(&mut self, ctx: &mut dyn StrategyCtx) {
        let Some(book) = &self.book else {
            return;
        };

        // One order at a time per symbol. Without this the same entry goes
        // out again on every quote until the fill news gets back, which is
        // several orders for one decision.
        let mut working = Vec::new();
        ctx.resting(&mut working);
        let busy: Vec<SymbolId> = working.iter().map(|order| order.symbol).collect();
        drop(working);

        let now_ms = ctx.wall_ms();
        let valid_until_ms = book.valid_until_ms;
        let targets: Vec<Target> = book
            .targets
            .iter()
            .map(|t| Target {
                symbol: t.symbol.clone(),
                notional_usdt: t.notional_usdt,
                stop_loss_fraction: t.stop_loss_fraction,
            })
            .collect();

        let steps = {
            let facts = CtxFacts { ctx: &*ctx };
            // Everything this plug could be holding: its own universe, plus
            // anything the book names. A symbol the book has stopped naming
            // is only exited if we go looking for it.
            let mut candidates: Vec<&str> = self.symbols.iter().map(String::as_str).collect();
            for target in &book.targets {
                if !candidates.contains(&target.symbol.as_str()) {
                    candidates.push(target.symbol.as_str());
                }
            }
            let held: Vec<String> = candidates
                .into_iter()
                .filter(|symbol| facts.held(symbol).is_some())
                .map(str::to_string)
                .collect();

            plan(&targets, &held, &facts, now_ms, valid_until_ms, self.rules).steps
        };

        let decided_ns = ctx.now_ns();
        let mut unreachable: Vec<String> = Vec::new();
        for step in steps {
            let Some(symbol) = ctx.symbol_id(step.symbol()) else {
                if !self.complained.iter().any(|said| said == step.symbol()) {
                    tracing::warn!(
                        symbol = step.symbol(),
                        "the book names a symbol outside this plug's universe; nothing sent"
                    );
                    unreachable.push(step.symbol().to_string());
                }
                continue;
            };
            // Routine and short-lived, so it is not a warning: the order is
            // out, and the next wake after the fill news will pick this up.
            if busy.contains(&symbol) {
                tracing::debug!(
                    symbol = step.symbol(),
                    "an order of ours is still working here; leaving this step for the next wake"
                );
                continue;
            }
            let intent = match step {
                Step::Enter {
                    side,
                    qty,
                    stop_px,
                    ..
                } => Intent {
                    strategy: self.id,
                    symbol,
                    side,
                    qty,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec {
                        trigger_px: stop_px,
                    }),
                    reduce_only: false,
                    tag: ENTER_TAG.to_string(),
                    decided_ns,
                },
                Step::Exit { side, qty, .. } => Intent {
                    strategy: self.id,
                    symbol,
                    side,
                    qty,
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: true,
                    tag: EXIT_TAG.to_string(),
                    decided_ns,
                },
                // A resize that adds is an opening order, and the risk kernel
                // refuses one with no stop. The planner says whether this is
                // one and where the stop belongs; a shrink carries none,
                // because a reduce-only order that names a stop is refused by
                // the venue instead.
                Step::Resize {
                    side,
                    qty,
                    reduce_only,
                    stop_px,
                    ..
                } => Intent {
                    strategy: self.id,
                    symbol,
                    side,
                    qty,
                    kind: OrderKind::Market,
                    stop: stop_px.map(|trigger_px| StopSpec { trigger_px }),
                    reduce_only,
                    tag: RESIZE_TAG.to_string(),
                    decided_ns,
                },
            };
            ctx.place(intent);
        }
        self.complained.append(&mut unreachable);
    }
}

impl Strategy for TargetBookFollower {
    fn name(&self) -> &str {
        NAME
    }

    /// The universe from the config block. The engine asks once, at boot, so
    /// this cannot grow with a later book — see the module note.
    fn subscriptions(&self) -> Vec<Subscription> {
        self.symbols
            .iter()
            .map(|symbol| Subscription {
                symbol: symbol.clone(),
                feed: Feed::Quote,
            })
            .collect()
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Targets(book) => {
                self.book = Some(book.clone());
                // A new book earns a fresh hearing, including for the names
                // it could not reach last time.
                self.complained.clear();
                self.act(ctx);
            }
            // A price is what sizing needs, so a book that arrived before the
            // first quote is acted on here instead.
            EngineEvent::Market(MarketEvent::Quote { .. }) => self.act(ctx),
            // A feed reset clears every price, so there is nothing to size
            // against until the next quote — which will call `act` anyway.
            // Order news changes the account reading, and that arrives on the
            // engine's own refresh, not from here.
            EngineEvent::Market(_) | EngineEvent::Timer { .. } | EngineEvent::Order(_) => {}
        }
    }
}

/// The planner's view of one symbol, answered from the engine's context.
struct CtxFacts<'a> {
    ctx: &'a dyn StrategyCtx,
}

impl SymbolFacts for CtxFacts<'_> {
    fn held(&self, symbol: &str) -> Option<Held> {
        let id = self.ctx.symbol_id(symbol)?;
        let position = self.ctx.position(id)?;
        // Value it at the market when there is a market, and at what it was
        // opened at when there is not. An exit needs only a side and a size,
        // so a symbol whose feed has gone quiet can still be closed.
        let px = market_px(self.ctx, id).unwrap_or(position.entry_px);
        Some(Held {
            qty: position.qty,
            side: position.side,
            px,
            entry_px: position.entry_px,
        })
    }

    fn price(&self, symbol: &str) -> Option<f64> {
        let id = self.ctx.symbol_id(symbol)?;
        market_px(self.ctx, id)
    }

    fn rule(&self, symbol: &str) -> Option<InstrumentRule> {
        let id = self.ctx.symbol_id(symbol)?;
        self.ctx.instrument(id)
    }
}

/// Mid when both sides of the book are there, else the ticker's last or mark.
/// Zero is not a price: an empty book side reads as zero, and so does a
/// symbol whose feed has just reset.
fn market_px(ctx: &dyn StrategyCtx, symbol: SymbolId) -> Option<f64> {
    let quote = ctx.quote(symbol);
    if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
        return Some((quote.bid_px + quote.ask_px) / 2.0);
    }
    let ticker = ctx.ticker(symbol);
    [ticker.last_px, ticker.mark_px]
        .into_iter()
        .find(|px| *px > 0.0)
}

#[cfg(test)]
mod tests;
