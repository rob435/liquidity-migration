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
//! assert_eq!(h.drain_actions().len(), 1);
//! ```
//!
//! A plug is a pure state machine, so the bench needs no threads, no sleeps
//! and no fake sockets: time only moves when a test moves it.

use std::collections::{HashMap, HashSet};

use engine_types::{
    Action, Depth, EngineEvent, InstrumentRule, Intent, MarketEvent, OrderKind, OrderUpdate,
    PositionView, Quote, RestingOrder, Side, Strategy, StrategyAccountSummary, StrategyCheckpoint,
    StrategyCtx, StrategyId, SymbolId, Ticker, TimerId, TradeFlow,
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
    depths: Vec<Depth>,
    trades: Vec<TradeFlow>,
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
    checkpoints: HashMap<SymbolId, StrategyCheckpoint>,
    global_checkpoint: Option<StrategyCheckpoint>,
    strategy_ids: HashMap<String, StrategyId>,
    now_ns: u64,
    /// The wall clock, separate from monotonic strategy time.
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
    account_equity_usdt: f64,
    account_available_margin_usdt: f64,
}

impl MockCtx {
    pub fn new() -> Self {
        Self {
            symbols: HashMap::new(),
            quotes: Vec::new(),
            depths: Vec::new(),
            trades: Vec::new(),
            tickers: Vec::new(),
            positions: HashMap::new(),
            foreign: HashSet::new(),
            mine: HashMap::new(),
            in_flight: HashMap::new(),
            rules: HashMap::new(),
            checkpoints: HashMap::new(),
            global_checkpoint: None,
            strategy_ids: HashMap::new(),
            now_ns: 1_000,
            // An ordinary unix millisecond stamp, so anything that reads like
            // a real clock reads like a real clock in tests too.
            wall_ms: 1_700_000_000_000,
            emitted: Vec::new(),
            timers: Vec::new(),
            arm_calls: Vec::new(),
            resting: Vec::new(),
            account_equity_usdt: 1_000.0,
            account_available_margin_usdt: 1_000.0,
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
        self.depths.push(Depth::default());
        self.trades.push(TradeFlow::default());
        self.tickers.push(Ticker::default());
        id
    }

    pub fn id_of(&self, name: &str) -> SymbolId {
        *self
            .symbols
            .get(name)
            .unwrap_or_else(|| panic!("symbol {name} was never interned"))
    }

    pub fn set_now(&mut self, now_ns: u64) {
        self.now_ns = now_ns;
    }

    pub fn set_account_summary(&mut self, equity_usdt: f64, available_margin_usdt: f64) {
        self.account_equity_usdt = equity_usdt;
        self.account_available_margin_usdt = available_margin_usdt;
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
        // A seeded holding belongs to this strategy unless the test says otherwise.
        self.mine.insert(
            id,
            match side {
                Side::Buy => qty,
                Side::Sell => -qty,
            },
        );
        self.positions.insert(
            id,
            PositionView {
                symbol: id,
                side,
                qty,
                entry_px,
                stop_attached: true,
                stop_px: 0.0,
                leverage: None,
            },
        );
    }

    /// Seed a holding the venue reports that no order of this engine's ever
    /// opened -- a position the owner placed by hand. It shows in `position`
    /// and in nobody's attribution, which is the shape that used to make the
    /// book close the owner's own trade.
    pub fn set_hand_position(&mut self, symbol: &str, side: Side, qty: f64, entry_px: f64) {
        self.set_position(symbol, side, qty, entry_px);
        let id = self.id_of(symbol);
        self.mine.remove(&id);
    }

    /// Seed a holding the venue reports that belongs to *another* strategy on
    /// the same account — the other sleeve's position, in other words. It
    /// shows in `position` and is foreign to whoever is asking, which is the
    /// exact shape that used to make one follower close another's book.
    pub fn set_foreign_position(&mut self, symbol: &str, side: Side, qty: f64, entry_px: f64) {
        self.set_position(symbol, side, qty, entry_px);
        let id = self.id_of(symbol);
        self.mine.remove(&id);
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

    /// Seed the venue's tick, step and minimums for a symbol.
    pub fn set_rule(&mut self, symbol: &str, rule: InstrumentRule) {
        let id = self.add_symbol(symbol);
        self.rules.insert(id, rule);
    }

    pub fn set_wall_ms(&mut self, wall_ms: i64) {
        self.wall_ms = wall_ms;
    }

    pub fn set_global_checkpoint(&mut self, checkpoint: StrategyCheckpoint) {
        self.global_checkpoint = Some(checkpoint);
    }

    pub fn set_strategy_id(&mut self, name: &str, id: StrategyId) {
        self.strategy_ids.insert(name.to_owned(), id);
    }
}

impl StrategyCtx for MockCtx {
    fn account_summary(&self) -> StrategyAccountSummary {
        StrategyAccountSummary {
            equity_usdt: self.account_equity_usdt,
            available_margin_usdt: self.account_available_margin_usdt,
            observed_ns: self.now_ns,
        }
    }

    fn quote(&self, symbol: SymbolId) -> &Quote {
        &self.quotes[symbol.0 as usize]
    }

    fn depth(&self, symbol: SymbolId) -> &Depth {
        &self.depths[symbol.0 as usize]
    }

    fn trade_flow(&self, symbol: SymbolId) -> &TradeFlow {
        &self.trades[symbol.0 as usize]
    }

    fn ticker(&self, symbol: SymbolId) -> &Ticker {
        &self.tickers[symbol.0 as usize]
    }

    fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.symbols.get(name).copied()
    }

    fn symbol_name(&self, symbol: SymbolId) -> Option<&str> {
        self.symbols
            .iter()
            .find_map(|(name, known)| (*known == symbol).then_some(name.as_str()))
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

    fn my_position_names<'a>(&'a self, out: &mut Vec<&'a str>) {
        let start = out.len();
        out.extend(self.symbols.iter().filter_map(|(name, symbol)| {
            self.mine
                .get(symbol)
                .is_some_and(|qty| qty.abs() >= 1e-9)
                .then_some(name.as_str())
        }));
        out[start..].sort_unstable();
    }

    fn in_flight(&self, symbol: SymbolId) -> f64 {
        self.in_flight.get(&symbol).copied().unwrap_or(0.0)
    }

    fn my_position_facts(&self, symbol: SymbolId) -> Option<engine_types::StrategyPositionFacts> {
        let attributed_signed_qty = self.my_position(symbol);
        let in_flight_signed_qty = self.in_flight(symbol);
        if attributed_signed_qty == 0.0 && in_flight_signed_qty == 0.0 {
            return None;
        }
        Some(engine_types::StrategyPositionFacts {
            symbol,
            attributed_signed_qty,
            venue: self.position(symbol),
            in_flight_signed_qty,
        })
    }

    fn my_positions(&self, out: &mut Vec<engine_types::StrategyPositionFacts>) {
        let symbols = self
            .mine
            .keys()
            .chain(self.in_flight.keys())
            .map(|symbol| symbol.0)
            .collect::<std::collections::BTreeSet<_>>();
        out.extend(
            symbols
                .into_iter()
                .filter_map(|symbol| self.my_position_facts(SymbolId(symbol))),
        );
    }

    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule> {
        self.rules.get(&symbol).copied()
    }

    fn wall_ms(&self) -> i64 {
        self.wall_ms
    }

    fn strategy_checkpoint(&self, symbol: SymbolId) -> Option<&StrategyCheckpoint> {
        self.checkpoints.get(&symbol)
    }

    fn strategy_global_checkpoint(&self) -> Option<&StrategyCheckpoint> {
        self.global_checkpoint.as_ref()
    }

    fn strategy_id(&self, name: &str) -> Option<StrategyId> {
        self.strategy_ids.get(name).copied()
    }

    fn emit(&mut self, action: Action) {
        self.emitted.push(action);
    }

    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        let armed = ArmedTimer {
            id,
            armed_ns: self.now_ns,
            due_ns: self.now_ns + after_ns,
        };
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

    fn order_facts(&self, client_order_id: &str) -> Option<engine_types::OrderFacts> {
        let order = self
            .resting
            .iter()
            .find(|order| order.client_order_id == client_order_id)?;
        Some(engine_types::OrderFacts {
            symbol: order.symbol,
            side: order.side,
            qty: order.qty,
            filled_qty: order.filled_qty,
            reduce_only: order.reduce_only,
        })
    }
}

/// One strategy plus its context, with a verb for every event the engine can
/// deliver.
pub struct Harness {
    pub ctx: MockCtx,
    pub strategy: Box<dyn Strategy>,
}

impl Harness {
    /// Build the plug bench and intern the symbols it subscribed to.
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

    /// Deliver the same post-restore wake that engine boot delivers.
    pub fn boot(&mut self) {
        self.deliver(EngineEvent::Boot);
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
        if quote.supersedes(&self.ctx.quotes[id.0 as usize]) {
            self.ctx.quotes[id.0 as usize] = quote;
        }
        self.deliver(EngineEvent::Market(MarketEvent::Quote {
            symbol: id,
            quote,
        }));
    }

    /// A reconstructed book update. Levels must already be in venue order:
    /// bids descending and asks ascending.
    pub fn depth(&mut self, symbol: &str, bids: &[(f64, f64)], asks: &[(f64, f64)]) {
        let id = self.ctx.id_of(symbol);
        let mut depth = Depth {
            bid_len: bids.len().min(engine_types::BOOK_DEPTH) as u8,
            ask_len: asks.len().min(engine_types::BOOK_DEPTH) as u8,
            ..Depth::default()
        };
        for (slot, (px, qty)) in depth.bids.iter_mut().zip(bids) {
            *slot = engine_types::BookLevel { px: *px, qty: *qty };
        }
        for (slot, (px, qty)) in depth.asks.iter_mut().zip(asks) {
            *slot = engine_types::BookLevel { px: *px, qty: *qty };
        }
        depth.recv_ns = self.ctx.now_ns;
        depth.seq = self.ctx.depths[id.0 as usize].seq + 1;
        depth.update_id = depth.seq;
        self.ctx.depths[id.0 as usize] = depth;
        // The same arbitration the engine's own `MarketState::apply` runs, so
        // a strategy test sees the touch this strategy would really read.
        let touch = depth.quote();
        if touch.supersedes(&self.ctx.quotes[id.0 as usize]) {
            self.ctx.quotes[id.0 as usize] = touch;
        }
        self.deliver(EngineEvent::Market(MarketEvent::Depth {
            symbol: id,
            depth,
        }));
    }

    pub fn trades(&mut self, symbol: &str, buy_qty: f64, sell_qty: f64, last_px: f64) {
        let id = self.ctx.id_of(symbol);
        let trades = TradeFlow {
            buy_qty,
            sell_qty,
            last_px,
            trade_count: ((buy_qty > 0.0) as u16) + ((sell_qty > 0.0) as u16),
            seq: self.ctx.trades[id.0 as usize].seq + 1,
            venue_ts_ms: 0,
            recv_ns: self.ctx.now_ns,
        };
        self.ctx.trades[id.0 as usize] = trades;
        self.deliver(EngineEvent::Market(MarketEvent::Trades {
            symbol: id,
            trades,
        }));
    }

    pub fn feed_reset(&mut self) {
        let recv_ns = self.ctx.now_ns;
        self.deliver(EngineEvent::Market(MarketEvent::FeedReset { recv_ns }));
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

    pub fn fast_fill(
        &mut self,
        exec_id: &str,
        client_order_id: &str,
        symbol: &str,
        side: Side,
        qty: f64,
        px: f64,
    ) {
        let id = self.ctx.id_of(symbol);
        self.deliver(EngineEvent::Order(OrderUpdate::FastFill {
            exec_id: exec_id.to_string(),
            client_order_id: client_order_id.to_string(),
            venue_order_id: format!("v-{client_order_id}"),
            symbol: id,
            side,
            qty,
            px,
            is_maker: true,
            venue_ts_ms: 0,
            recv_ns: self.ctx.now_ns,
        }));
    }

    pub fn maker_fill_with_exec(
        &mut self,
        exec_id: &str,
        client_order_id: &str,
        symbol: &str,
        side: Side,
        qty: f64,
        px: f64,
    ) {
        let id = self.ctx.id_of(symbol);
        self.ctx.charge_fill(id, side, qty);
        self.deliver(EngineEvent::Order(OrderUpdate::Fill {
            exec_id: exec_id.to_string(),
            client_order_id: client_order_id.to_string(),
            symbol: id,
            side,
            qty,
            px,
            fee: Some(0.0),
            is_maker: true,
            forced_close: None,
            venue_ts_ms: 0,
            recv_ns: self.ctx.now_ns,
        }));
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
            exec_id: String::new(),
            client_order_id: client_order_id.to_string(),
            symbol: id,
            side,
            qty,
            px,
            fee: Some(0.0),
            is_maker,
            forced_close: None,
            venue_ts_ms: 0,
            recv_ns: self.ctx.now_ns,
        }));
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

    /// The single action emitted since the last call, whatever kind it is.
    pub fn one_action(&mut self) -> Action {
        let mut drained = self.drain_actions();
        assert_eq!(
            drained.len(),
            1,
            "expected exactly one action, got {drained:?}"
        );
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
            kind: OrderKind::Limit {
                px: 60_000.0,
                tif: engine_types::TimeInForce::Gtc,
            },
            qty: 0.01,
            filled_qty: 0.0,
            reduce_only: false,
            acked: true,
        });
        h.quote("BTCUSDT", 60_999.0, 61_000.0);
        assert_eq!(
            h.one_action(),
            Action::Cancel {
                symbol,
                client_order_id: "eng-4".to_string()
            }
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
