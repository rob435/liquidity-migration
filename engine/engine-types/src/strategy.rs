use crate::ids::{SymbolId, TimerId};
use crate::market::{MarketEvent, Quote, Subscription, Ticker};
use crate::orders::{Action, AmendSpec, Intent, OrderUpdate, RestingOrder};

/// Everything a strategy can be woken by.
#[derive(Clone, Debug, PartialEq)]
pub enum EngineEvent {
    Market(MarketEvent),
    Timer { id: TimerId, now_ns: u64 },
    Order(OrderUpdate),
}

/// The strategy's window into the engine. Read market state and your own
/// resting orders, emit actions, arm timers. No venue access, no log access,
/// no clock other than this.
pub trait StrategyCtx {
    fn quote(&self, symbol: SymbolId) -> &Quote;
    fn ticker(&self, symbol: SymbolId) -> &Ticker;
    fn symbol_id(&self, name: &str) -> Option<SymbolId>;
    fn now_ns(&self) -> u64;
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
}
