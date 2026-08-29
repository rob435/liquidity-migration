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
    Action, Feed, InstrumentRule, Intent, MarketEvent, OrderKind, Side, StopSpec, Strategy,
    StrategyCtx, StrategyId, Subscription, SymbolId, TargetBook,
};

use super::plan::{plan, Held, PlanRules, Skipped, Step, SymbolFacts, Target};
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
    /// Names already announced as somebody else's -- another sleeve's, or the
    /// owner's own hand. That is account state, not book state: it stays true
    /// for as long as the position is open, so announcing it per book shouts
    /// the same fact every cycle. Pruned to what is still true, so a name that
    /// goes flat and comes back is news again.
    others_held_said: Vec<String>,
    /// Names that went flat while the book still wanted them. See the module
    /// note: something other than this plug closed them, and buying them back
    /// would undo it. Cleared per symbol when the book stops asking.
    closed_under_us: Vec<String>,
    /// Entries the kernel refused for the book in hand, with the reason it
    /// gave. A refused order never rests and never fills, so the reading
    /// stays flat and the book keeps wanting it: without this the next quote
    /// asks again, forever. Entries only — an exit is always retried.
    /// Cleared by the next book.
    refused_entries: Vec<(String, String)>,
    /// Working entries whose target changed while they were live. Kept until
    /// the order leaves the working set, including across repeated books.
    revoked_entries: Vec<String>,
    /// Entry-blocking skips from the latest plan, with the skip's name: a
    /// size below the entry floor, one the venue would round to nothing, a
    /// price or instrument rule missing. The producer reads these through
    /// the heartbeat, so an ask that can never fill is visible instead of
    /// squatting on a slot. Rebuilt on every wake.
    skipped_entries: Vec<(String, String)>,
    /// Which of the book's names were held last time `act` ran. Without it a
    /// position that *disappeared* cannot be told from one that was never
    /// there, and every unfilled entry would latch itself.
    was_held: Vec<String>,
    /// Names this plug has sent a reduce for and not yet seen go flat. A
    /// position that vanishes after we asked it to vanished because of us, so
    /// it must not latch — otherwise every ordinary exit would block the name
    /// the next time the book wanted it.
    we_reduced: Vec<String>,
}

// The sent-ahead cover records — the memory of what was sent that the account
// reading has not shown yet — live in the engine
// (`engine-core/src/covers.rs`). The plug reads that answer back as
// `ctx.in_flight` and adds it to the reading; it keeps no in-flight
// bookkeeping of its own.

/// Whether the book in hand asks for any of this name. Exactly zero is an
/// instruction to hold none, so it does not count as wanting it.
fn wants(targets: &[Target], symbol: &str) -> bool {
    targets
        .iter()
        .any(|target| target.symbol == symbol && target.notional_usdt != 0.0)
}

impl TargetBookFollower {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&[
            "symbols",
            "rest_entries",
            "hold_decision_price",
            "give_up_instead_of_crossing",
        ])?;

        // Entries rest at the touch and are worked by the engine instead of
        // crossing the spread. Off by default so turning it on is a decision
        // somebody made, not one they inherited.
        let rest_entries = p.bool_or("rest_entries", false)?;
        // How the resting is worked. Both off by default: the recipe they
        // change was measured over 199,785 paired attempts and these arms
        // were not.
        let work = engine_types::WorkPolicy {
            hold_decision_px: p.bool_or("hold_decision_price", false)?,
            give_up_instead_of_crossing: p.bool_or("give_up_instead_of_crossing", false)?,
            ..engine_types::WorkPolicy::default()
        };
        if !rest_entries && (work.hold_decision_px || work.give_up_instead_of_crossing) {
            return Err(p.invalid(
                "rest_entries",
                "hold_decision_price and give_up_instead_of_crossing only govern a resting \
                 entry; with rest_entries off nothing rests and they would sit here doing \
                 nothing",
            ));
        }

        let symbols = p.strings("symbols")?;
        if symbols.is_empty() {
            return Err(p.invalid(
                "symbols",
                "expected at least one venue symbol; a follower with no universe can trade nothing",
            ));
        }

        Ok(Self {
            entry_work: rest_entries.then_some(work),
            id,
            symbols,
            book: None,
            rules: PlanRules::FLEET,
            complained: Vec::new(),
            others_held_said: Vec::new(),
            closed_under_us: Vec::new(),
            refused_entries: Vec::new(),
            revoked_entries: Vec::new(),
            skipped_entries: Vec::new(),
            was_held: Vec::new(),
            we_reduced: Vec::new(),
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

        let now_ms = ctx.wall_ms();
        // One order at a time per symbol. Without this the same entry goes
        // out again on every quote until the fill news gets back, which is
        // several orders for one decision. A working entry is authorization
        // to add exposure, though: withdraw it as soon as the newest book no
        // longer names the target or its entry window has closed. Otherwise
        // an old touch can fill after the producer deliberately removed it.
        let mut working = Vec::new();
        ctx.resting(&mut working);
        self.revoked_entries.retain(|id| {
            working
                .iter()
                .any(|order| order.client_order_id == id.as_str())
        });
        let cancelled_entries: Vec<(SymbolId, String)> = working
            .iter()
            .filter(|order| {
                !order.reduce_only
                    && (self
                        .revoked_entries
                        .iter()
                        .any(|id| id == order.client_order_id)
                        || !book.targets.iter().any(|target| {
                            target.notional_usdt != 0.0
                                && ctx.symbol_id(&target.symbol) == Some(order.symbol)
                                && now_ms
                                    < target.entry_valid_until_ms.map_or_else(
                                        || {
                                            book.valid_until_ms
                                                .saturating_sub(self.rules.entry_cutoff_ms)
                                        },
                                        |deadline| {
                                            deadline.min(
                                                book.valid_until_ms
                                                    .saturating_sub(self.rules.entry_cutoff_ms),
                                            )
                                        },
                                    )
                        }))
            })
            .map(|order| (order.symbol, order.client_order_id.to_string()))
            .collect();
        let busy: Vec<SymbolId> = working.iter().map(|order| order.symbol).collect();
        drop(working);
        for (symbol, client_order_id) in cancelled_entries {
            ctx.cancel(symbol, &client_order_id);
        }

        let valid_until_ms = book.valid_until_ms;
        let mut targets: Vec<Target> = book
            .targets
            .iter()
            .map(|t| Target {
                symbol: t.symbol.clone(),
                notional_usdt: t.notional_usdt,
                stop_loss_fraction: t.stop_loss_fraction,
                entry_valid_until_ms: t.entry_valid_until_ms,
                target_qty: t.target_qty,
            })
            .collect();

        // Everything this plug could be holding: its own universe, anything
        // the book names, and every open claim recovered from the log. A
        // symbol the book has stopped naming is only exited if we look for it;
        // the recovered claims are what makes that true on the first book
        // after a restart too.
        let mut candidates: Vec<&str> = self.symbols.iter().map(String::as_str).collect();
        for target in &book.targets {
            if !candidates.contains(&target.symbol.as_str()) {
                candidates.push(target.symbol.as_str());
            }
        }
        let mut recovered = Vec::new();
        ctx.my_position_names(&mut recovered);
        for symbol in recovered {
            if !candidates.contains(&symbol) {
                candidates.push(symbol);
            }
        }
        // And whatever we were holding last time round. Without this an EMPTY
        // book -- the decision to hold nothing -- only ever closed the seed
        // list from the config, because a book with no targets adds no names
        // and the seed is tiny. It remains useful inside one process before a
        // fill is in the durable log; recovered claims cover the next boot.
        for symbol in &self.was_held {
            if !candidates.contains(&symbol.as_str()) {
                candidates.push(symbol.as_str());
            }
        }
        let mut held: Vec<String> = {
            let facts = CtxFacts { ctx: &*ctx };
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
                if !self.others_held_said.contains(&symbol) {
                    tracing::warn!(
                        symbol = %symbol,
                        "another strategy on this account is holding this name; leaving it alone"
                    );
                    self.others_held_said.push(symbol.clone());
                }
                foreign.push(symbol);
            }
        }
        targets.retain(|target| !foreign.contains(&target.symbol));
        held.retain(|symbol| !foreign.contains(symbol));

        // Exposure this engine has no fills for is not ours to touch. A
        // position the owner opened by hand reads exactly this way, and the
        // book is absolute: without this it is a name the book does not
        // mention, so the next pass would close it, and the pass after that
        // would close it again. Left alone entirely -- not entered, not
        // exited -- the same as another sleeve's name.
        //
        // `in_flight` is in the test because our own entry is held at the
        // venue before its fill reaches attribution, and for that moment it
        // would otherwise read as somebody else's.
        let mut unowned: Vec<String> = Vec::new();
        for symbol in targets
            .iter()
            .map(|target| target.symbol.clone())
            .chain(held.iter().cloned())
        {
            if unowned.contains(&symbol) {
                continue;
            }
            let hand_held = ctx.symbol_id(&symbol).is_some_and(|id| {
                ctx.position(id).is_some() && ctx.my_position(id) == 0.0 && ctx.in_flight(id) == 0.0
            });
            if hand_held {
                if !self.others_held_said.contains(&symbol) {
                    tracing::warn!(
                        symbol = %symbol,
                        "this account holds a position in this name that no order of \
                         ours ever opened; leaving it alone"
                    );
                    self.others_held_said.push(symbol.clone());
                }
                unowned.push(symbol);
            }
        }
        targets.retain(|target| !unowned.contains(&target.symbol));
        held.retain(|symbol| !unowned.contains(symbol));
        self.others_held_said
            .retain(|said| foreign.contains(said) || unowned.contains(said));

        // An entry the kernel just refused is left out of this pass entirely,
        // the same way a foreign holding is: planning it again would only
        // produce the same refusal on the next quote. The next book clears it.
        targets.retain(|target| !self.refused_entries.iter().any(|(name, _)| name == &target.symbol));
        held.retain(|symbol| !self.refused_entries.iter().any(|(name, _)| name == symbol));

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
            let facts = CtxFacts { ctx: &*ctx };
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
        // A skip on a name the book wants and nothing holds is an entry that
        // cannot happen, published so the producer can tell "on its way"
        // from "never going to fill". Skips on held names are resizes; they
        // are the engine's own business.
        self.skipped_entries.clear();
        for skip in &skipped {
            let (symbol, reason): (&String, &str) = match skip {
                Skipped::BelowEntryFloor { symbol, .. } => (symbol, "below_entry_floor"),
                Skipped::BelowVenueMinimum { symbol } => (symbol, "below_venue_minimum"),
                Skipped::EntryWindowClosed { symbol } => (symbol, "entry_window_closed"),
                Skipped::NoPrice { symbol } => (symbol, "no_price"),
                Skipped::NoInstrumentRule { symbol } => (symbol, "no_instrument_rule"),
                Skipped::TooSmallToBother { .. } => continue,
            };
            if wants(&targets, symbol) && !held.iter().any(|name| name == symbol) {
                self.skipped_entries.push((symbol.clone(), reason.to_string()));
            }
        }

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
            if busy.contains(&symbol) && !matches!(step, Step::Restop { .. }) {
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
            // Not an order: no size, no leverage, no working-order conflict.
            // It goes out on its own and the loop moves on.
            if let Step::Restop { stop_px, .. } = step {
                ctx.emit(Action::SetStop {
                    symbol,
                    trigger_px: stop_px,
                });
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
                // Handled above, before this match: it is not an order.
                Step::Restop { .. } => continue,
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
            // The engine books its own cover for this send the moment it
            // goes to the venue, at the quantized size that actually went,
            // and `held` above reads it back as `ctx.in_flight` — so the
            // window between the fill and the next reading cannot look flat.
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

    // Only books and quotes are acted on. Order news falls through to the
    // trait's do-nothing default: that bookkeeping is the engine's
    // (`covers.rs`), settled before this plug is even woken. A refusal is
    // recorded but never acted on, so nothing is ever emitted from inside an
    // order or refusal wake, which would re-emit into the queue being
    // drained — the next quote re-plans instead.

    fn on_intent_refused(
        &mut self,
        symbol: SymbolId,
        reduce_only: bool,
        reason: &str,
        ctx: &mut dyn StrategyCtx,
    ) {
        // Exits are never held back. Only an entry latches, and only until
        // the next book.
        if reduce_only {
            return;
        }
        let named = self
            .symbols
            .iter()
            .cloned()
            .chain(
                self.book
                    .iter()
                    .flat_map(|book| book.targets.iter().map(|target| target.symbol.clone())),
            )
            .find(|name| ctx.symbol_id(name) == Some(symbol));
        if let Some(name) = named {
            if !self.refused_entries.iter().any(|(seen, _)| seen == &name) {
                tracing::warn!(
                    symbol = %name,
                    reason,
                    "the kernel refused this entry; not asking again until the next book"
                );
                self.refused_entries.push((name, reason.to_string()));
            }
        }
    }

    fn entry_blockers(&self) -> Vec<(String, String)> {
        // Kernel refusals first, so the dedupe on the engine side keeps the
        // stronger news when a planner skip says the same thing.
        self.refused_entries
            .iter()
            .chain(self.skipped_entries.iter())
            .cloned()
            .collect()
    }

    fn on_targets(&mut self, book: &TargetBook, ctx: &mut dyn StrategyCtx) {
        if let Some(previous) = &self.book {
            let mut working = Vec::new();
            ctx.resting(&mut working);
            for order in working.iter().filter(|order| !order.reduce_only) {
                let old_target = previous
                    .targets
                    .iter()
                    .find(|target| ctx.symbol_id(&target.symbol) == Some(order.symbol));
                let new_target = book
                    .targets
                    .iter()
                    .find(|target| ctx.symbol_id(&target.symbol) == Some(order.symbol));
                if old_target != new_target
                    && !self
                        .revoked_entries
                        .iter()
                        .any(|id| id == order.client_order_id)
                {
                    self.revoked_entries
                        .push(order.client_order_id.to_string());
                }
            }
        }
        self.book = Some(book.clone());
        // A new book earns a fresh hearing, including for the names it could
        // not reach last time and the entries the kernel refused.
        self.complained.clear();
        self.refused_entries.clear();
        self.act(ctx);
    }

    // A price is what sizing needs, so a book that arrived before the first
    // quote is acted on at the next one. A ticker or a feed reset changes
    // nothing here: a reset clears every price, so there is nothing to size
    // against until the next quote — which will call `act` anyway.
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        if let MarketEvent::Quote { .. } = event {
            self.act(ctx);
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
        let position = self.ctx.position(id);
        let entry_px = position.as_ref().map(|p| p.entry_px).unwrap_or(0.0);
        let stop_px = position.as_ref().map(|p| p.stop_px).unwrap_or(0.0);
        // What the reading shows, plus what the engine says was sent and has
        // not appeared in it yet. Without the second part the window between
        // a fill and the next reading looks flat, and the plug buys the same
        // target twice.
        let signed = position
            .map(|p| if p.side == Side::Buy { p.qty } else { -p.qty })
            .unwrap_or(0.0)
            + self.ctx.in_flight(id);
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
            stop_px,
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
