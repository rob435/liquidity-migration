use crate::ids::{SymbolId, TimerId};
use crate::market::{MarketEvent, Quote, Subscription, Ticker};
use crate::orders::{Action, AmendSpec, InstrumentRule, Intent, OrderFacts, OrderUpdate, RestingOrder};
use crate::risk::PositionView;
use crate::targets::TargetBook;

/// Everything a strategy can be woken by.
#[derive(Clone, Debug, PartialEq)]
pub enum EngineEvent {
    Market(MarketEvent),
    Timer { id: TimerId, now_ns: u64 },
    Order(OrderUpdate),
    /// A new target book from the research system.
    ///
    /// It arrives only when a whole book was read and understood. No file,
    /// an unreadable one, or a version the engine does not know delivers
    /// nothing at all — strategies are not woken, which is *no decision*,
    /// and a follower goes on holding what it holds. An empty book is the
    /// opposite: it is delivered, and it says hold nothing.
    Targets(TargetBook),
    /// An intent this strategy placed died inside the engine before it
    /// became an order: the risk kernel refused it, it could not be
    /// quantized, its leverage could not be set, or the engine is latched
    /// against opening. No order exists and no fill will ever come of it.
    /// The engine's own in-flight accounting (read back through
    /// [`StrategyCtx::in_flight`]) is already settled before this arrives:
    /// a refused entry was never booked, and a refused exit has dropped
    /// every cover on the symbol. (An order the VENUE ends arrives as
    /// [`EngineEvent::Order`] news instead, with its id.)
    IntentRefused { symbol: SymbolId, reduce_only: bool, reason: String },
}

/// The strategy's window into the engine. Read market state, the account
/// reading, and your own resting orders; emit actions, arm timers. No venue
/// access, no log access, no clock other than these.
pub trait StrategyCtx {
    fn quote(&self, symbol: SymbolId) -> &Quote;
    fn ticker(&self, symbol: SymbolId) -> &Ticker;
    fn symbol_id(&self, name: &str) -> Option<SymbolId>;
    fn now_ns(&self) -> u64;
    /// What the engine's latest account reading says is held in this symbol,
    /// or `None` when it is flat or the reading does not mention it. This is
    /// the venue's own picture, refreshed on the engine's schedule — not a
    /// running total of the strategy's fills.
    fn position(&self, symbol: SymbolId) -> Option<PositionView>;
    /// Whether a strategy other than this one is holding this symbol.
    ///
    /// [`StrategyCtx::position`] above is the account's holding and says
    /// nothing about whose it is, so on an account running two strategies it
    /// is not enough to act on: the second one would size against exposure it
    /// never opened, and exit it. This answers the question that matters
    /// before touching a name at all.
    ///
    /// It is asked per symbol rather than per quantity because the venue's
    /// stop is attached to the *position*, and there is one position per
    /// symbol. Two strategies holding one symbol would have one stop between
    /// them, set by whichever placed the last opening order — so sharing is
    /// not something to be sized around. Whoever got there first keeps the
    /// name until it is flat.
    ///
    /// Exposure the engine's own log has no fills for belongs to nobody here:
    /// a hand trade reads as `false`. Boot's reconciliation is what notices
    /// that and stops the engine opening on top of it.
    fn foreign_position(&self, symbol: SymbolId) -> bool;
    /// This strategy's own signed position in a symbol, summed from the fills
    /// of the orders it placed. Positive is long.
    ///
    /// [`StrategyCtx::position`] above is the venue's account reading, and it
    /// is the wrong number for a strategy to hold inventory against, twice
    /// over. It is refreshed on the engine's own schedule — seconds — so a
    /// strategy that has just filled goes on acting as though it had not,
    /// which for a maker means quoting the same side again and again. And on
    /// an account running two sleeves it is the sum of both, so one sleeve
    /// would count the other's position as its own.
    ///
    /// This is the strategy's own trading and nobody else's. It moves the
    /// instant a fill arrives, before the strategy is woken. What it is *not*
    /// is a second opinion about what the account holds: hand-placed exposure
    /// belongs to nobody here and reads as zero, and where this and the
    /// account reading disagree the account reading is the fact.
    fn my_position(&self, symbol: SymbolId) -> f64;
    /// Names this strategy's fills still claim as open, appended to `out`.
    ///
    /// This is the restart-safe complement to [`StrategyCtx::my_position`]:
    /// a strategy can ask about one known name above, while this lets it find
    /// names recovered from the log that are absent from its current decision
    /// and boot subscription seed. The venue reading remains the fact about
    /// whether any returned name is actually held.
    fn my_position_names<'a>(&'a self, out: &mut Vec<&'a str>) {
        let _ = out;
    }
    /// Signed quantity this strategy has sent that the engine's account
    /// reading has not yet absorbed. Positive is long.
    ///
    /// The gap it bridges: a fully filled order leaves
    /// [`StrategyCtx::resting`] the instant the fill lands, while the account
    /// reading refreshes on the engine's own schedule — seconds. In that
    /// window the position exists at the venue and shows nowhere else a
    /// strategy can look, and a strategy deciding from "target minus
    /// position" sends the same target twice. The reading plus this closes
    /// the window.
    ///
    /// The engine books the cover itself when it hands an order to the
    /// venue, at the quantized size that actually went, and releases it as
    /// the reading absorbs it (or as a reject or cancel ends the unfilled
    /// part). It is not a second position record: where this and the reading
    /// disagree about what is held, the reading is the fact, and a refused
    /// exit clears every cover on its symbol for exactly that reason. Zero
    /// for a context double that keeps no cover book.
    fn in_flight(&self, symbol: SymbolId) -> f64 {
        let _ = symbol;
        0.0
    }
    /// Tick, step and minimums, as the venue stated them at boot. `None`
    /// means nothing can be quantized for that symbol, so nothing can be
    /// sent for it either.
    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule>;
    /// Wall-clock milliseconds since the unix epoch, comparable with venue
    /// timestamps. Every other clock in here is monotonic on purpose; this
    /// one exists because a target book carries a validity window written by
    /// another process, and only a clock of the same kind can say whether it
    /// has run out. Never measure latency with it — it can be stepped, and
    /// two readings can come back in the wrong order.
    fn wall_ms(&self) -> i64;
    /// Hand an action to the engine. The risk kernel still gates every send.
    fn emit(&mut self, action: Action);
    /// One-shot timer; fires as [`EngineEvent::Timer`] after `after_ns`.
    fn arm_timer(&mut self, id: TimerId, after_ns: u64);
    /// This strategy's own orders that the log says are still working,
    /// appended to `out`. The engine mints order ids, so this is how a
    /// quoting strategy finds the order it wants to pull or move. Pass a
    /// buffer you keep between wakes and the read allocates nothing.
    fn resting<'a>(&'a self, out: &mut Vec<RestingOrder<'a>>);

    /// What the log says about one of this strategy's own orders: its
    /// symbol, its size, and how much of it filled. `None` for an id this
    /// engine never minted, an order another strategy placed, or a context
    /// double that keeps no ledger. The terminal order news (`Reject`,
    /// `Cancelled`) carries only the id, and a strategy keeping any record
    /// of its own by symbol needs the way back.
    fn order_facts(&self, client_order_id: &str) -> Option<OrderFacts> {
        let _ = client_order_id;
        None
    }

    fn place(&mut self, intent: Intent) {
        self.emit(Action::Place(intent));
    }

    fn cancel(&mut self, symbol: SymbolId, client_order_id: &str) {
        self.emit(Action::Cancel {
            symbol,
            client_order_id: client_order_id.to_string(),
        });
    }

    fn amend(&mut self, symbol: SymbolId, client_order_id: &str, spec: AmendSpec) {
        self.emit(Action::Amend {
            symbol,
            client_order_id: client_order_id.to_string(),
            spec,
        });
    }
}

/// A pluggable strategy. Built by the registry from a name and a TOML
/// config block emitted by the research system. Strategies are synchronous
/// and single-threaded by construction: `on_event` runs on the engine loop
/// and must return quickly.
///
/// A strategy overrides only the per-event hooks it acts on — `on_market`,
/// `on_timer`, `on_order`, `on_targets`, `on_intent_refused` — and the ones
/// it ignores do nothing by default. The one exhaustive match over
/// [`EngineEvent`] lives in the provided `on_event` below and nowhere else,
/// so adding a new kind of event means: add the variant, add a hook with a
/// do-nothing default body, route it in that one match. No strategy needs an
/// edit.
pub trait Strategy {
    fn name(&self) -> &str;
    /// Market data wanted, collected once at boot.
    fn subscriptions(&self) -> Vec<Subscription>;

    /// Every wake, routed. The engine calls this and only this; strategies
    /// override the hooks below instead, and only under a reason as good as
    /// this trait doc would they override the routing itself.
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Market(market) => self.on_market(market, ctx),
            EngineEvent::Timer { id, now_ns } => self.on_timer(*id, *now_ns, ctx),
            EngineEvent::Order(update) => self.on_order(update, ctx),
            EngineEvent::Targets(book) => self.on_targets(book, ctx),
            EngineEvent::IntentRefused {
                symbol,
                reduce_only,
                reason,
            } => self.on_intent_refused(*symbol, *reduce_only, reason, ctx),
        }
    }

    /// A market message: a quote, a ticker, or a feed reset.
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let _ = (event, ctx);
    }

    /// A timer this strategy armed has come due.
    fn on_timer(&mut self, id: TimerId, now_ns: u64, ctx: &mut dyn StrategyCtx) {
        let _ = (id, now_ns, ctx);
    }

    /// News about one of this strategy's own orders.
    fn on_order(&mut self, update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        let _ = (update, ctx);
    }

    /// A new target book from the research system. See
    /// [`EngineEvent::Targets`] for what arriving — and not arriving — means.
    fn on_targets(&mut self, book: &TargetBook, ctx: &mut dyn StrategyCtx) {
        let _ = (book, ctx);
    }

    /// An intent this strategy placed died inside the engine before it
    /// became an order, with the reason the engine logged. See
    /// [`EngineEvent::IntentRefused`].
    fn on_intent_refused(
        &mut self,
        symbol: SymbolId,
        reduce_only: bool,
        reason: &str,
        ctx: &mut dyn StrategyCtx,
    ) {
        let _ = (symbol, reduce_only, reason, ctx);
    }

    /// Why this strategy is not opening each name it is asking for right
    /// now, as (symbol name, reason) pairs, for the heartbeat.
    ///
    /// A target-book producer writes an absolute ask and learns what became
    /// of it only through the engine's heartbeat; without this an entry the
    /// kernel refused, or a size below the entry floor, is invisible to it,
    /// and the ask squats on a capacity slot until the producer's own
    /// deadline drops it. Refusals the kernel delivered and skips the
    /// planner took both belong here. Empty by default.
    fn entry_blockers(&self) -> Vec<(String, String)> {
        Vec::new()
    }

    /// Whether this strategy acts on [`EngineEvent::Targets`].
    ///
    /// Books are routed, not broadcast: each one goes to the single strategy
    /// whose config named its path. The engine uses this to check the wiring
    /// at boot — a book path given to a strategy that ignores books is a file
    /// nobody reads, and a book-follower with no path is a sleeve that will
    /// never be told what to hold.
    ///
    /// Default false, which is right for anything that decides for itself.
    fn follows_a_target_book(&self) -> bool {
        false
    }
}
