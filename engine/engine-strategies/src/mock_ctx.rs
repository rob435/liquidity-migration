//! The test bench for plugs. Every strategy in this crate is tested through
//! it, so it is worth reading before writing the next one.
//!
//! [`MockCtx`] is a stand-in for the engine's side of the strategy contract:
//! it holds the market picture, the clock, the strategy's own resting orders,
//! and it records what the strategy did — the actions it emitted and the
//! timers it armed. [`Harness`] pairs one context with one strategy and gives
//! each thing that can happen to a plug a one-line name, so a test reads like
//! the story it is testing:
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

use std::collections::{HashMap, HashSet};

use engine_types::{
    Action, EngineEvent, InstrumentRule, Intent, MarketEvent, OrderAck, OrderKind,
    OrderUpdate, PositionView, Quote, RestingOrder, Side, Strategy, StrategyCtx, SymbolId,
    TargetBook, Ticker, TimerId,
};

/// A timer the strategy asked for, and when it comes due.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct ArmedTimer {
    pub id: TimerId,
    pub armed_ns: u64,
    pub due_ns: u64,
}

/// A working order for the strategy under test to find. Owned here because
/// the real contract hands out borrows of the engine's own book.
#[derive(Clone, Debug, PartialEq)]
pub struct RestingSeed {
    pub client_order_id: String,
    pub symbol: SymbolId,
    pub side: Side,
    pub kind: OrderKind,
    pub qty: f64,
    pub filled_qty: f64,
    pub reduce_only: bool,
    pub acked: bool,
}

pub struct MockCtx {
    symbols: HashMap<String, SymbolId>,
    quotes: Vec<Quote>,
    tickers: Vec<Ticker>,
    /// What the engine's account reading would say is held. Empty unless a
    /// test seeds it.
    positions: HashMap<SymbolId, PositionView>,
    /// Symbols another strategy on this account is holding. Kept apart from
    /// `positions` on purpose: the whole point of `foreign_position` is that
    /// the account reading cannot tell you this, so a mock that derived one
    /// from the other could not show the difference.
    foreign: HashSet<SymbolId>,
    /// What this strategy's own fills add up to, signed, positive long. The
    /// engine keeps this from the log; the bench keeps it from the fills a
    /// test delivers, so a plug that reads its own inventory reads the same
    /// number here that it would live.
    mine: HashMap<SymbolId, f64>,
    /// What the engine's cover book would say this strategy has sent that
    /// the account reading has not absorbed yet, signed, positive long. The
    /// engine books and releases the real one (`engine-core/src/covers.rs`);
    /// the bench holds whatever a test seeds, because the registering and
    /// releasing are the engine's behavior and are tested there.
    in_flight: HashMap<SymbolId, f64>,
    /// What the venue said about tick and step. Empty unless a test seeds it,
    /// and an unseeded symbol reads the way an unknown one does: nothing can
    /// be quantized for it.
    rules: HashMap<SymbolId, InstrumentRule>,
    now_ns: u64,
    /// The wall clock, separate from `now_ns` on purpose: a target book's
    /// validity window is wall time, and a test has to be able to move it
    /// without touching the monotonic one.
    wall_ms: i64,
    /// Everything the strategy emitted, oldest first.
    pub emitted: Vec<Action>,
    /// Timers still waiting to fire.
    pub timers: Vec<ArmedTimer>,
    /// Every arm_timer call ever made, including replacements.
    pub arm_calls: Vec<ArmedTimer>,
    /// What the engine would say is still working for this strategy. Empty
    /// unless a test seeds it.
    pub resting: Vec<RestingSeed>,
}

impl MockCtx {
    pub fn new() -> Self {
        Self {
            symbols: HashMap::new(),
            quotes: Vec::new(),
            tickers: Vec::new(),
            positions: HashMap::new(),
            foreign: HashSet::new(),
            mine: HashMap::new(),
            in_flight: HashMap::new(),
            rules: HashMap::new(),
            now_ns: 1_000,
            // An ordinary unix millisecond stamp, so anything that reads like
            // a real clock reads like a real clock in tests too.
            wall_ms: 1_700_000_000_000,
            emitted: Vec::new(),
            timers: Vec::new(),
            arm_calls: Vec::new(),
            resting: Vec::new(),
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

    pub fn set_wall_ms(&mut self, wall_ms: i64) {
        self.wall_ms = wall_ms;
    }

    /// Seed what the account reading says is held, as this strategy's own.
    /// Interns the symbol if it is new, the way a subscription would have.
    ///
    /// It clears any foreign mark, so seeding a position after
    /// [`MockCtx::set_foreign_position`] is how a test says the other sleeve
    /// let the name go — including seeding it flat.
    pub fn set_position(&mut self, symbol: &str, side: Side, qty: f64, entry_px: f64) {
        let id = self.add_symbol(symbol);
        self.foreign.remove(&id);
        self.positions.insert(
            id,
            PositionView {
                symbol: id,
                side,
                qty,
                entry_px,
                stop_attached: true,
                leverage: None
            },
        );
    }

    /// Seed a holding the venue reports that belongs to *another* strategy on
    /// the same account — the other sleeve's position, in other words. It
    /// shows in `position` and is foreign to whoever is asking, which is the
    /// exact shape that used to make one follower close another's book.
    pub fn set_foreign_position(&mut self, symbol: &str, side: Side, qty: f64, entry_px: f64) {
        self.set_position(symbol, side, qty, entry_px);
        let id = self.id_of(symbol);
        self.foreign.insert(id);
    }

    /// Charge a fill to this strategy's own running position, the way the
    /// engine's attribution does before it wakes anybody.
    pub fn charge_fill(&mut self, symbol: SymbolId, side: Side, qty: f64) {
        let signed = match side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        *self.mine.entry(symbol).or_insert(0.0) += signed;
    }

    /// Start a test with this strategy already holding something of its own,
    /// without walking it through the fills that got there.
    pub fn set_my_position(&mut self, symbol: &str, signed_qty: f64) {
        let id = self.add_symbol(symbol);
        self.mine.insert(id, signed_qty);
    }

    /// Seed what the engine's cover book would answer for this symbol: the
    /// signed quantity sent that the account reading has not shown yet. Zero
    /// clears it, the way a reading that caught up would.
    pub fn set_in_flight(&mut self, symbol: &str, signed_qty: f64) {
        let id = self.add_symbol(symbol);
        if signed_qty == 0.0 {
            self.in_flight.remove(&id);
        } else {
            self.in_flight.insert(id, signed_qty);
        }
    }

    /// Seed the venue's tick, step and minimums for a symbol.
    pub fn set_rule(&mut self, symbol: &str, rule: InstrumentRule) {
        let id = self.add_symbol(symbol);
        self.rules.insert(id, rule);
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

    fn position(&self, symbol: SymbolId) -> Option<PositionView> {
        // Flat is not a position, same as the engine's own context.
        self.positions.get(&symbol).filter(|p| p.qty > 0.0).cloned()
    }

    fn foreign_position(&self, symbol: SymbolId) -> bool {
        self.foreign.contains(&symbol)
    }

    fn my_position(&self, symbol: SymbolId) -> f64 {
        self.mine.get(&symbol).copied().unwrap_or(0.0)
    }

    fn in_flight(&self, symbol: SymbolId) -> f64 {
        self.in_flight.get(&symbol).copied().unwrap_or(0.0)
    }

    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule> {
        self.rules.get(&symbol).copied()
    }

    fn wall_ms(&self) -> i64 {
        self.wall_ms
    }

    fn emit(&mut self, action: Action) {
        self.emitted.push(action);
    }

    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        let armed = ArmedTimer { id, armed_ns: self.now_ns, due_ns: self.now_ns + after_ns };
        self.arm_calls.push(armed);
        // One shot per id: arming again replaces the pending one.
        self.timers.retain(|t| t.id != id);
        self.timers.push(armed);
    }

    fn resting<'a>(&'a self, out: &mut Vec<RestingOrder<'a>>) {
        for seed in &self.resting {
            out.push(RestingOrder {
                client_order_id: seed.client_order_id.as_str(),
                symbol: seed.symbol,
                side: seed.side,
                kind: seed.kind,
                qty: seed.qty,
                filled_qty: seed.filled_qty,
                reduce_only: seed.reduce_only,
                acked: seed.acked,
            });
        }
    }

    // `order_facts` is left at the trait's default (`None`): this bench keeps
    // no order ledger, and the cover bookkeeping that used to need the lookup
    // lives in the engine now.
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

    /// A target book reaching the strategies. Only ever delivered when a
    /// whole book was read, which is why there is no verb here for "the book
    /// was unreadable": that case delivers nothing at all.
    pub fn targets(&mut self, book: TargetBook) {
        self.deliver(EngineEvent::Targets(book));
    }

    /// The kernel refused an intent the strategy placed. The engine delivers
    /// this after the refusal is journaled, so nothing is resting and the
    /// account reading is unchanged.
    pub fn refuse(&mut self, symbol: SymbolId, reduce_only: bool) {
        self.deliver(EngineEvent::IntentRefused { symbol, reduce_only });
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

    /// A fill that crossed the spread. The common case in these tests, and
    /// the one a strategy that sends market orders always gets.
    pub fn fill(&mut self, client_order_id: &str, symbol: &str, side: Side, qty: f64, px: f64) {
        self.fill_as(client_order_id, symbol, side, qty, px, false);
    }

    /// A fill where we were the resting side. Only a strategy that quotes
    /// gets these, and telling them apart is the whole point of the flag.
    pub fn maker_fill(
        &mut self,
        client_order_id: &str,
        symbol: &str,
        side: Side,
        qty: f64,
        px: f64,
    ) {
        self.fill_as(client_order_id, symbol, side, qty, px, true);
    }

    fn fill_as(
        &mut self,
        client_order_id: &str,
        symbol: &str,
        side: Side,
        qty: f64,
        px: f64,
        is_maker: bool,
    ) {
        let id = self.ctx.id_of(symbol);
        // Before the strategy hears about it, exactly as the engine charges
        // attribution before it wakes anybody: a plug that reads its own
        // position inside the fill callback must already see the fill.
        self.ctx.charge_fill(id, side, qty);
        self.deliver(EngineEvent::Order(OrderUpdate::Fill {
            client_order_id: client_order_id.to_string(),
            symbol: id,
            side,
            qty,
            px,
            fee: 0.0,
            is_maker,
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

    /// Give the strategy a working order to find through `ctx.resting`.
    pub fn rest(&mut self, seed: RestingSeed) {
        self.ctx.resting.push(seed);
    }

    /// Take every action emitted since the last call.
    pub fn drain_actions(&mut self) -> Vec<Action> {
        std::mem::take(&mut self.ctx.emitted)
    }

    /// Take the orders placed since the last call. A plug that only places is
    /// read through this one; a plug that also pulls or moves orders is read
    /// through [`Harness::drain_actions`].
    pub fn drain(&mut self) -> Vec<Intent> {
        self.drain_actions()
            .into_iter()
            .filter_map(|action| match action {
                Action::Place(intent) => Some(intent),
                _ => None,
            })
            .collect()
    }

    /// The single intent emitted since the last call. Panics on none or many,
    /// which is what "emit exactly once" tests want to see.
    pub fn one_intent(&mut self) -> Intent {
        let mut drained = self.drain();
        assert_eq!(drained.len(), 1, "expected exactly one intent, got {drained:?}");
        drained.remove(0)
    }

    /// The single action emitted since the last call, whatever kind it is.
    pub fn one_action(&mut self) -> Action {
        let mut drained = self.drain_actions();
        assert_eq!(drained.len(), 1, "expected exactly one action, got {drained:?}");
        drained.remove(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{StrategyId, Subscription};

    /// Pulls whatever the engine says it has working, on the first quote.
    struct Puller;

    impl Strategy for Puller {
        fn name(&self) -> &str {
            "puller"
        }

        fn subscriptions(&self) -> Vec<Subscription> {
            vec![Subscription {
                symbol: "BTCUSDT".to_string(),
                feed: engine_types::Feed::Quote,
            }]
        }

        fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
            if !matches!(event, EngineEvent::Market(MarketEvent::Quote { .. })) {
                return;
            }
            let mut mine = Vec::new();
            ctx.resting(&mut mine);
            let pulls: Vec<(SymbolId, String)> = mine
                .iter()
                .map(|o| (o.symbol, o.client_order_id.to_string()))
                .collect();
            for (symbol, id) in pulls {
                ctx.cancel(symbol, &id);
            }
        }
    }

    #[test]
    fn a_plug_reads_its_resting_orders_and_can_pull_one() {
        let mut h = Harness::new(Box::new(Puller));
        let symbol = h.ctx.id_of("BTCUSDT");
        h.rest(RestingSeed {
            client_order_id: "eng-4".to_string(),
            symbol,
            side: Side::Buy,
            kind: OrderKind::Limit { px: 60_000.0, tif: engine_types::TimeInForce::Gtc },
            qty: 0.01,
            filled_qty: 0.0,
            reduce_only: false,
            acked: true,
        });
        h.quote("BTCUSDT", 60_999.0, 61_000.0);
        assert_eq!(
            h.one_action(),
            Action::Cancel { symbol, client_order_id: "eng-4".to_string() }
        );
        assert!(h.drain().is_empty(), "a cancel is not a placement");
    }

    #[test]
    fn an_empty_book_is_the_default() {
        let mut h = Harness::new(Box::new(Puller));
        h.quote("BTCUSDT", 60_999.0, 61_000.0);
        assert!(h.drain_actions().is_empty());
    }

    #[test]
    fn a_strategy_id_on_an_emitted_intent_is_left_alone_here() {
        // The engine's own context rewrites it; the mock deliberately does
        // not, so a test can see exactly what the plug asked for.
        let mut ctx = MockCtx::new();
        ctx.add_symbol("BTCUSDT");
        ctx.place(Intent {
            strategy: StrategyId(9),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            tag: "t".into(),
            decided_ns: 7,
            work: None,
            leverage: None,
        });
        let Action::Place(intent) = ctx.emitted.remove(0) else {
            panic!("expected a placement");
        };
        assert_eq!(intent.strategy, StrategyId(9));
    }
}
