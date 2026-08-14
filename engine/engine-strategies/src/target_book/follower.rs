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
//! ## A name that goes flat under us is not re-entered
//!
//! The book says hold something; the venue says we hold none of it; nothing
//! this plug sent closed it. A venue stop fired, or somebody closed it by
//! hand, or it was liquidated. Acting on the book alone would buy it straight
//! back on the next quote — the stop undone seconds after it worked, which is
//! not a stop at all.
//!
//! So the name is latched and left completely alone: no entry, and no exit
//! either. The latch lifts when the producer stops asking for the symbol,
//! because that is the producer having taken the news into account. It
//! deliberately does *not* lift on the next book: a producer writing the same
//! decision every minute would otherwise clear the latch every minute, which
//! is the loop this exists to stop.
//!
//! ## A name another strategy holds is not ours
//!
//! The venue holds one position per symbol however many plugs run, and the
//! account reading says nothing about whose it is. So this plug asks the
//! engine, which minted every order and knows: a name another strategy is
//! holding is skipped entirely — not entered, not resized, not exited.
//!
//! It is refused wholesale rather than shared pro-rata because the venue's
//! stop is attached to the *position*, and there is one position per symbol.
//! Two plugs holding one name would have one stop between them, set by
//! whichever placed the last opening order — so the second one's stop would
//! quietly replace the first one's. Whoever got there first keeps the name
//! until it is flat.
//!
//! ## The symbol list is a seed, not a ceiling
//!
//! The `symbols` list is what this plug subscribes to at boot. A book naming
//! something outside it is not skipped — the engine takes the name on and
//! subscribes to it (`engine.rs`, `admit_wanted`), and the plug trades it once
//! it has a price and an instrument rule.

use engine_types::{
    EngineEvent, Feed, InstrumentRule, Intent, MarketEvent, OrderKind, Side, StopSpec,
    Strategy,
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
    /// What this plug subscribes to at boot. A seed, not a ceiling: see
    /// the module note.
    symbols: Vec<String>,
    /// The newest book. `None` until one arrives, and that means no decision.
    book: Option<TargetBook>,
    rules: PlanRules,
    /// How an entry is placed. `None` crosses the spread as it always did;
    /// `Some` rests it at the touch and lets the engine work it in.
    entry_work: Option<engine_types::WorkPolicy>,
    /// Symbols already complained about for the book in hand. This runs on
    /// every quote, so without it one unreachable name in a book writes a
    /// warning a hundred times a second until the next book lands.
    complained: Vec<String>,
    /// Names that went flat while the book still wanted them. See the module
    /// note: something other than this plug closed them, and buying them back
    /// would undo it. Cleared per symbol when the book stops asking.
    closed_under_us: Vec<String>,
    /// Which of the book's names were held last time `act` ran. Without it a
    /// position that *disappeared* cannot be told from one that was never
    /// there, and every unfilled entry would latch itself.
    was_held: Vec<String>,
    /// Names this plug has sent a reduce for and not yet seen go flat. A
    /// position that vanishes after we asked it to vanished because of us, so
    /// it must not latch — otherwise every ordinary exit would block the name
    /// the next time the book wanted it.
    we_reduced: Vec<String>,
    /// Signed quantity this plug has sent that the account reading has not
    /// caught up with yet, per symbol.
    ///
    /// A resting order covers the gap between sending and filling, but not
    /// the gap between filling and *seeing* the fill: the log ends a fully
    /// filled order at once, while the account reading is up to half its
    /// staleness bound behind. In that window the position exists at the
    /// venue and shows nowhere the plug can look, and a plug that decides
    /// from "target minus position" would buy it a second time.
    sent_ahead: Vec<SentAhead>,
}

/// One symbol's sent-but-not-yet-visible quantity, with the reading it was
/// sent against. When the reading moves off `view_at_send` it has taken the
/// fill into account and the record is dropped — no timer, no guess.
#[derive(Copy, Clone, Debug)]
struct SentAhead {
    symbol: SymbolId,
    view_at_send: f64,
    sent: f64,
}

/// Whether the book in hand asks for any of this name. Exactly zero is an
/// instruction to hold none, so it does not count as wanting it.
fn wants(targets: &[Target], symbol: &str) -> bool {
    targets
        .iter()
        .any(|target| target.symbol == symbol && target.notional_usdt != 0.0)
}

/// What the account reading says is held, signed. Positive is long.
fn view_signed(ctx: &dyn StrategyCtx, id: SymbolId) -> f64 {
    match ctx.position(id) {
        Some(p) if p.side == Side::Buy => p.qty,
        Some(p) => -p.qty,
        None => 0.0,
    }
}

impl TargetBookFollower {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&["symbols", "rest_entries"])?;

        // Entries rest at the touch and are worked by the engine instead of
        // crossing the spread. Off by default so turning it on is a decision
        // somebody made, not one they inherited.
        let rest_entries = p.bool_or("rest_entries", false)?;

        let symbols = p.strings("symbols")?;
        if symbols.is_empty() {
            return Err(p.invalid(
                "symbols",
                "expected at least one venue symbol; a follower with no universe can trade nothing",
            ));
        }

        Ok(Self {
            entry_work: rest_entries.then(engine_types::WorkPolicy::default),
            id,
            symbols,
            book: None,
            rules: PlanRules::FLEET,
            complained: Vec::new(),
            closed_under_us: Vec::new(),
            was_held: Vec::new(),
            we_reduced: Vec::new(),
            sent_ahead: Vec::new(),
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

        // A reading that has moved since a send has taken that fill into
        // account, so the record is no longer needed. Anything still here is
        // exposure the reading cannot see yet.
        self.sent_ahead
            .retain(|record| view_signed(&*ctx, record.symbol) == record.view_at_send);

        let now_ms = ctx.wall_ms();
        let valid_until_ms = book.valid_until_ms;
        let mut targets: Vec<Target> = book
            .targets
            .iter()
            .map(|t| Target {
                symbol: t.symbol.clone(),
                notional_usdt: t.notional_usdt,
                stop_loss_fraction: t.stop_loss_fraction,
            })
            .collect();

        // Everything this plug could be holding: its own universe, plus
        // anything the book names. A symbol the book has stopped naming
        // is only exited if we go looking for it.
        let mut candidates: Vec<&str> = self.symbols.iter().map(String::as_str).collect();
        for target in &book.targets {
            if !candidates.contains(&target.symbol.as_str()) {
                candidates.push(target.symbol.as_str());
            }
        }
        let mut held: Vec<String> = {
            let facts = CtxFacts { ctx: &*ctx, sent_ahead: &self.sent_ahead };
            candidates
                .into_iter()
                .filter(|symbol| facts.held(symbol).is_some())
                .map(str::to_string)
                .collect()
        };

        // A name the other sleeve is holding is not ours at all. See the
        // module note: the venue's stop belongs to the position, and there is
        // one position per symbol, so two plugs cannot both hold one name
        // without one of them silently overwriting the other's stop.
        let mut foreign: Vec<String> = Vec::new();
        for symbol in targets
            .iter()
            .map(|target| target.symbol.clone())
            .chain(held.iter().cloned())
        {
            if foreign.contains(&symbol) {
                continue;
            }
            if ctx
                .symbol_id(&symbol)
                .is_some_and(|id| ctx.foreign_position(id))
            {
                if !self.complained.contains(&symbol) {
                    tracing::warn!(
                        symbol = %symbol,
                        "another strategy on this account is holding this name; leaving it alone"
                    );
                    self.complained.push(symbol.clone());
                }
                foreign.push(symbol);
            }
        }
        targets.retain(|target| !foreign.contains(&target.symbol));
        held.retain(|symbol| !foreign.contains(symbol));

        // The latch lifts when the producer stops asking for the name, not
        // when the next book lands. See the module note: a producer writing
        // the same decision every minute would clear it every minute.
        self.closed_under_us.retain(|symbol| wants(&targets, symbol));
        for symbol in &self.was_held {
            if held.contains(symbol) {
                continue;
            }
            // It has gone flat. If we asked for that, it is ours and the
            // record is spent; either way nothing latches on our own exit.
            if let Some(at) = self.we_reduced.iter().position(|name| name == symbol) {
                self.we_reduced.swap_remove(at);
                continue;
            }
            if !wants(&targets, symbol) || self.closed_under_us.contains(symbol) {
                continue;
            }
            tracing::warn!(
                symbol = %symbol,
                "this went flat while the book still wanted it, and nothing this plug sent \
                 closed it; leaving it alone until the book stops asking"
            );
            self.closed_under_us.push(symbol.clone());
        }
        self.was_held.clear();
        self.was_held.extend(held.iter().cloned());

        // Latched names are left completely alone: no entry, and no exit
        // either, because an exit for something we are not holding is an
        // order for nothing and an exit for something somebody else opened is
        // not ours to send.
        targets.retain(|target| !self.closed_under_us.contains(&target.symbol));
        held.retain(|symbol| !self.closed_under_us.contains(symbol));

        let (steps, skipped) = {
            let facts = CtxFacts { ctx: &*ctx, sent_ahead: &self.sent_ahead };
            let plan = plan(&targets, &held, &facts, now_ms, valid_until_ms, self.rules);
            (plan.steps, plan.skipped)
        };
        // What the plug decided and why, on every wake. Off at info, because
        // this runs per quote; on when something has plainly not happened and
        // the log otherwise says nothing at all about the decision.
        tracing::debug!(
            strategy = self.id.0,
            targets = targets.len(),
            held = held.len(),
            latched = self.closed_under_us.len(),
            foreign = foreign.len(),
            now_ms,
            valid_until_ms,
            steps = steps.len(),
            ?skipped,
            "the book and what is held came out as this"
        );

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
            // What the book asked this name to be held at. The planner is
            // about sizing and deliberately does not carry it, so it is read
            // back from the book here.
            let want_leverage = self.book.as_ref().and_then(|book| {
                book.targets
                    .iter()
                    .find(|t| t.symbol == step.symbol())
                    .map(|t| t.leverage)
            });
            // Remember a reduce before it goes out, so the flat that follows
            // is read as ours rather than as somebody else closing the name.
            if matches!(step, Step::Exit { .. } | Step::Resize { reduce_only: true, .. })
                && !self.we_reduced.iter().any(|name| name == step.symbol())
            {
                self.we_reduced.push(step.symbol().to_string());
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
                    work: self.entry_work,
                    leverage: want_leverage,
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
                    work: None,
                    // An exit at the wrong leverage is still an exit. Making
                    // it wait on a round trip would be the wrong trade.
                    leverage: None,
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
                    // A shrink is an exit in all but name, and an exit never
                    // rests. Only the half that adds exposure is worked.
                    work: if reduce_only { None } else { self.entry_work },
                    // Same split: the half that adds margin is the half whose
                    // leverage matters.
                    leverage: if reduce_only { None } else { want_leverage },
                },
            };
            // Remember it against the reading it was decided from, so the
            // window between the fill and the next reading cannot look flat.
            let signed = match intent.side {
                Side::Buy => intent.qty,
                Side::Sell => -intent.qty,
            };
            self.sent_ahead.push(SentAhead {
                symbol,
                view_at_send: view_signed(&*ctx, symbol),
                sent: signed,
            });
            ctx.place(intent);
        }
        self.complained.append(&mut unreachable);
    }
}

impl Strategy for TargetBookFollower {
    fn name(&self) -> &str {
        NAME
    }

    /// Holding what a book says is the whole of this plug, so it must be
    /// given a book path — the engine refuses a config where it is not.
    fn follows_a_target_book(&self) -> bool {
        true
    }

    /// The universe from the config block. The engine asks once, at boot,
    /// and takes on anything a later book names on top — see the module note.
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
    sent_ahead: &'a [SentAhead],
}

impl CtxFacts<'_> {
    /// Signed quantity sent for this symbol that the reading has not shown
    /// yet. Positive is long.
    fn ahead(&self, id: SymbolId) -> f64 {
        self.sent_ahead
            .iter()
            .filter(|record| record.symbol == id)
            .map(|record| record.sent)
            .sum()
    }
}

impl SymbolFacts for CtxFacts<'_> {
    fn held(&self, symbol: &str) -> Option<Held> {
        let Some(id) = self.ctx.symbol_id(symbol) else {
            return None;
        };
        let position = self.ctx.position(id);
        let entry_px = position.as_ref().map(|p| p.entry_px).unwrap_or(0.0);
        let ahead = self.ahead(id);
        // What the reading shows, plus what was sent and has not appeared in
        // it yet. Without the second part the window between a fill and the
        // next reading looks flat, and the plug buys the same target twice.
        let signed = position
            .map(|p| if p.side == Side::Buy { p.qty } else { -p.qty })
            .unwrap_or(0.0)
            + ahead;
        if signed.abs() <= f64::EPSILON {
            return None;
        }
        // Value it at the market when there is a market, and at what it was
        // opened at when there is not. An exit needs only a side and a size,
        // so a symbol whose feed has gone quiet can still be closed.
        let px = market_px(self.ctx, id).unwrap_or(entry_px);
        Some(Held {
            qty: signed.abs(),
            side: if signed > 0.0 { Side::Buy } else { Side::Sell },
            px,
            entry_px: if entry_px > 0.0 { entry_px } else { px },
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
