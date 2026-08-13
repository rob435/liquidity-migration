//! The test bench for plugs. Every strategy in this crate is tested through
//! it, so it is worth reading before writing the next one.
//!
//! [`MockCtx`] is a stand-in for the engine's side of the strategy contract:
//! it holds the market picture, the clock, and it records what the strategy
//! did — the intents it emitted and the timers it armed. [`Harness`] pairs one
//! context with one strategy and gives each thing that can happen to a plug a
//! one-line name, so a test reads like the story it is testing:
//!
//! ```ignore
//! let mut h = Harness::new(strategy);
//! h.quote("BTCUSDT", 60_999.0, 61_000.0);   // the level is touched
//! h.ack("c1");
//! h.fill("c1", "BTCUSDT", Side::Buy, 0.01, 61_000.0);
//! assert_eq!(h.drain().len(), 1);
//! ```
//!
//! A plug is a pure state machine, so the bench needs no threads, no sleeps
//! and no fake sockets: time only moves when a test moves it.

use std::collections::HashMap;

use engine_types::{
    EngineEvent, Intent, MarketEvent, OrderAck, OrderUpdate, Quote, Side, Strategy, StrategyCtx,
    SymbolId, Ticker, TimerId,
};

/// A timer the strategy asked for, and when it comes due.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct ArmedTimer {
    pub id: TimerId,
    pub armed_ns: u64,
    pub due_ns: u64,
}

pub struct MockCtx {
    symbols: HashMap<String, SymbolId>,
    quotes: Vec<Quote>,
    tickers: Vec<Ticker>,
    now_ns: u64,
    /// Everything the strategy emitted, oldest first.
    pub emitted: Vec<Intent>,
    /// Timers still waiting to fire.
    pub timers: Vec<ArmedTimer>,
    /// Every arm_timer call ever made, including replacements.
    pub arm_calls: Vec<ArmedTimer>,
}

impl MockCtx {
    pub fn new() -> Self {
        Self {
            symbols: HashMap::new(),
            quotes: Vec::new(),
            tickers: Vec::new(),
            now_ns: 1_000,
            emitted: Vec::new(),
            timers: Vec::new(),
            arm_calls: Vec::new(),
        }
    }

    /// Intern a symbol, the way the engine does at boot from the union of all
    /// strategy subscriptions.
    pub fn add_symbol(&mut self, name: &str) -> SymbolId {
        if let Some(id) = self.symbols.get(name) {
            return *id;
        }
        let id = SymbolId(self.quotes.len() as u16);
        self.symbols.insert(name.to_string(), id);
        self.quotes.push(Quote::default());
        self.tickers.push(Ticker::default());
        id
    }

    pub fn id_of(&self, name: &str) -> SymbolId {
        *self.symbols.get(name).unwrap_or_else(|| panic!("symbol {name} was never interned"))
    }

    pub fn set_now(&mut self, now_ns: u64) {
        self.now_ns = now_ns;
    }
}

impl StrategyCtx for MockCtx {
    fn quote(&self, symbol: SymbolId) -> &Quote {
        &self.quotes[symbol.0 as usize]
    }

    fn ticker(&self, symbol: SymbolId) -> &Ticker {
        &self.tickers[symbol.0 as usize]
    }

    fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.symbols.get(name).copied()
    }

    fn now_ns(&self) -> u64 {
        self.now_ns
    }

    fn emit(&mut self, intent: Intent) {
        self.emitted.push(intent);
    }

    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        let armed = ArmedTimer { id, armed_ns: self.now_ns, due_ns: self.now_ns + after_ns };
        self.arm_calls.push(armed);
        // One shot per id: arming again replaces the pending one.
        self.timers.retain(|t| t.id != id);
        self.timers.push(armed);
    }
}

/// One strategy plus its context, with a verb for every event the engine can
/// deliver.
pub struct Harness {
    pub ctx: MockCtx,
    pub strategy: Box<dyn Strategy>,
}

impl Harness {
    /// Boot a plug: intern the symbols it subscribed to, then hand it events.
    pub fn new(strategy: Box<dyn Strategy>) -> Self {
        let mut ctx = MockCtx::new();
        for sub in strategy.subscriptions() {
            ctx.add_symbol(&sub.symbol);
        }
        Self { ctx, strategy }
    }

    fn deliver(&mut self, event: EngineEvent) {
        self.strategy.on_event(&event, &mut self.ctx);
    }

    /// A new best bid/ask. The market picture is updated first, then the
    /// strategy is woken — the order the engine core uses.
    pub fn quote(&mut self, symbol: &str, bid_px: f64, ask_px: f64) {
        let id = self.ctx.id_of(symbol);
        let quote = Quote {
            bid_px,
            bid_qty: 1.0,
            ask_px,
            ask_qty: 1.0,
            venue_ts_ms: 0,
            recv_ns: self.ctx.now_ns,
            seq: self.ctx.quotes[id.0 as usize].seq + 1,
        };
        self.ctx.quotes[id.0 as usize] = quote;
        self.deliver(EngineEvent::Market(MarketEvent::Quote { symbol: id, quote }));
    }

    pub fn feed_reset(&mut self) {
        let recv_ns = self.ctx.now_ns;
        self.deliver(EngineEvent::Market(MarketEvent::FeedReset { recv_ns }));
    }

    pub fn ack(&mut self, client_order_id: &str) {
        let ack = OrderAck {
            client_order_id: client_order_id.to_string(),
            venue_order_id: format!("v-{client_order_id}"),
            ack_ns: self.ctx.now_ns,
        };
        self.deliver(EngineEvent::Order(OrderUpdate::Ack(ack)));
    }

    pub fn fill(&mut self, client_order_id: &str, symbol: &str, side: Side, qty: f64, px: f64) {
        let id = self.ctx.id_of(symbol);
        self.deliver(EngineEvent::Order(OrderUpdate::Fill {
            client_order_id: client_order_id.to_string(),
            symbol: id,
            side,
            qty,
            px,
            fee: 0.0,
            venue_ts_ms: 0,
            recv_ns: self.ctx.now_ns,
        }));
    }

    pub fn reject(&mut self, client_order_id: &str, reason: &str) {
        self.deliver(EngineEvent::Order(OrderUpdate::Reject {
            client_order_id: client_order_id.to_string(),
            code: 10001,
            reason: reason.to_string(),
        }));
    }

    /// Move the clock to the next armed timer and fire it. Returns false when
    /// nothing was waiting.
    pub fn fire_next_timer(&mut self) -> bool {
        let Some(next) = self.ctx.timers.iter().copied().min_by_key(|t| t.due_ns) else {
            return false;
        };
        self.ctx.timers.retain(|t| t.id != next.id);
        self.ctx.set_now(next.due_ns);
        self.deliver(EngineEvent::Timer { id: next.id, now_ns: next.due_ns });
        true
    }

    /// Fire a timer the strategy never armed — the engine can echo one back
    /// that a plug does not recognise.
    pub fn fire_timer(&mut self, id: TimerId) {
        let now_ns = self.ctx.now_ns;
        self.deliver(EngineEvent::Timer { id, now_ns });
    }

    /// Take everything emitted since the last call.
    pub fn drain(&mut self) -> Vec<Intent> {
        std::mem::take(&mut self.ctx.emitted)
    }

    /// The single intent emitted since the last call. Panics on none or many,
    /// which is what "emit exactly once" tests want to see.
    pub fn one_intent(&mut self) -> Intent {
        let mut drained = self.drain();
        assert_eq!(drained.len(), 1, "expected exactly one intent, got {drained:?}");
        drained.remove(0)
    }
}
