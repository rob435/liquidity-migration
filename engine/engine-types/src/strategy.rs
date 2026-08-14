use crate::ids::{SymbolId, TimerId};
use crate::market::{MarketEvent, Quote, Subscription, Ticker};
use crate::orders::{Action, AmendSpec, InstrumentRule, Intent, OrderUpdate, RestingOrder};
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
pub trait Strategy {
    fn name(&self) -> &str;
    /// Market data wanted, collected once at boot.
    fn subscriptions(&self) -> Vec<Subscription>;
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx);

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
