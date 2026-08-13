use crate::ids::{SymbolId, TimerId};
use crate::market::{MarketEvent, Quote, Subscription, Ticker};
use crate::orders::{Intent, OrderUpdate};

/// Everything a strategy can be woken by.
#[derive(Clone, Debug, PartialEq)]
pub enum EngineEvent {
    Market(MarketEvent),
    Timer { id: TimerId, now_ns: u64 },
    Order(OrderUpdate),
}

/// The strategy's window into the engine. Read market state, emit intents,
/// arm timers. No venue access, no log access, no clock other than this.
pub trait StrategyCtx {
    fn quote(&self, symbol: SymbolId) -> &Quote;
    fn ticker(&self, symbol: SymbolId) -> &Ticker;
    fn symbol_id(&self, name: &str) -> Option<SymbolId>;
    fn now_ns(&self) -> u64;
    /// Hand an intent to the engine. The risk kernel still gates it.
    fn emit(&mut self, intent: Intent);
    /// One-shot timer; fires as `EngineEvent::Timer` after `after_ns`.
    fn arm_timer(&mut self, id: TimerId, after_ns: u64);
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
