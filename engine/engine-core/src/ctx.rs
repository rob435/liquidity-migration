//! The window a strategy sees, and the timers it can set.
//!
//! A strategy gets read-only market state, the account reading, the
//! instrument rules, the clock, its own resting orders, a way to hand back an
//! action, and one-shot timers. Nothing else: no venue, no log, no other
//! strategy's state. Timers are scoped per strategy, so two strategies may
//! both use timer 1 without colliding, and re-arming the same number before
//! it fires replaces the old one.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, VecDeque};

use engine_types::{
    AccountView, Action, InstrumentRule, MarketState, PositionView, Quote, RestingOrder,
    StrategyCtx, StrategyId, SymbolId, Ticker, TimerId,
};

use crate::attribution::Attribution;
use crate::covers::CoverBook;
use crate::inflight::{LedgerOfOrders, OrderRegistry};

#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct Pending {
    deadline_ns: u64,
    strategy: u16,
    timer: u32,
}

#[derive(Default)]
pub struct Timers {
    heap: BinaryHeap<Reverse<Pending>>,
    armed: HashMap<(u16, u32), u64>,
}

impl Timers {
    pub fn arm(&mut self, strategy: StrategyId, timer: TimerId, deadline_ns: u64) {
        self.armed.insert((strategy.0, timer.0), deadline_ns);
        self.heap.push(Reverse(Pending {
            deadline_ns,
            strategy: strategy.0,
            timer: timer.0,
        }));
    }

    /// When the next live timer is due, ignoring entries left behind by a
    /// re-arm.
    pub fn next_deadline(&mut self) -> Option<u64> {
        loop {
            let top = self.heap.peek()?.0;
            if self.armed.get(&(top.strategy, top.timer)) == Some(&top.deadline_ns) {
                return Some(top.deadline_ns);
            }
            self.heap.pop();
        }
    }

    pub fn pop_due(&mut self, now_ns: u64) -> Option<(StrategyId, TimerId)> {
        loop {
            let top = self.heap.peek()?.0;
            let live = self.armed.get(&(top.strategy, top.timer)) == Some(&top.deadline_ns);
            if live && top.deadline_ns > now_ns {
                return None;
            }
            self.heap.pop();
            if live {
                self.armed.remove(&(top.strategy, top.timer));
                return Some((StrategyId(top.strategy), TimerId(top.timer)));
            }
        }
    }

    pub fn armed_count(&self) -> usize {
        self.armed.len()
    }
}

/// Handed to one strategy for the length of one callback.
pub struct Ctx<'a> {
    pub market: &'a MarketState,
    /// The engine's own account reading, the same one the risk kernel judges
    /// against. Shared rather than copied per strategy: one reading, one
    /// truth, and nobody can edit it on the way past.
    pub account: &'a AccountView,
    /// The venue's instrument rules, indexed by symbol, exactly as the
    /// engine quantizes against.
    pub rules: &'a [Option<InstrumentRule>],
    pub now_ns: u64,
    pub strategy: StrategyId,
    pub out: &'a mut VecDeque<Action>,
    pub timers: &'a mut Timers,
    /// What the log says is still out there. Read-only, and filtered by the
    /// registry below before a strategy is shown any of it.
    pub orders: &'a LedgerOfOrders,
    /// Who placed each order. Without it one strategy could read — and then
    /// cancel — another's working orders.
    pub registry: &'a OrderRegistry,
    /// Whose each position is, summed from the fills of the orders each
    /// strategy placed. The account reading above is per symbol and says
    /// nothing about whose a position is.
    pub attribution: &'a Attribution,
    /// What each strategy has sent that the account reading has not yet
    /// absorbed. The engine books and releases these; a strategy reads its
    /// own sum through `in_flight`.
    pub covers: &'a CoverBook,
}

impl StrategyCtx for Ctx<'_> {
    fn quote(&self, symbol: SymbolId) -> &Quote {
        self.market.quote(symbol)
    }

    fn depth(&self, symbol: SymbolId) -> &engine_types::Depth {
        self.market.depth(symbol)
    }

    fn trade_flow(&self, symbol: SymbolId) -> &engine_types::TradeFlow {
        self.market.trade_flow(symbol)
    }

    fn ticker(&self, symbol: SymbolId) -> &Ticker {
        self.market.ticker(symbol)
    }

    fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.market.table.get(name)
    }

    fn now_ns(&self) -> u64 {
        self.now_ns
    }

    fn position(&self, symbol: SymbolId) -> Option<PositionView> {
        // A row saying zero is a flat symbol, and flat is not a position: an
        // exit sized off one would be an order for nothing.
        self.account
            .positions
            .iter()
            .find(|p| p.symbol == symbol && p.qty > 0.0)
            .cloned()
    }

    fn foreign_position(&self, symbol: SymbolId) -> bool {
        self.attribution.held_by_another(self.strategy, symbol)
    }

    fn my_position(&self, symbol: SymbolId) -> f64 {
        self.attribution.signed(self.strategy, symbol)
    }

    fn my_position_names<'a>(&'a self, out: &mut Vec<&'a str>) {
        let start = out.len();
        out.extend(
            self.attribution
                .symbols(self.strategy)
                .map(|symbol| self.market.table.name(symbol)),
        );
        out[start..].sort_unstable();
    }

    fn in_flight(&self, symbol: SymbolId) -> f64 {
        self.covers.in_flight(self.strategy, symbol)
    }

    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule> {
        self.rules.get(symbol.0 as usize).copied().flatten()
    }

    fn wall_ms(&self) -> i64 {
        // Read when asked rather than stamped once per wake, so a strategy
        // that never asks the wall clock never pays for it.
        engine_types::clock::wall_ms()
    }

    fn emit(&mut self, action: Action) {
        // The context knows whose callback this is, so the intent is filed
        // under that strategy whatever it claims. A cancel or an amend names
        // an order id instead, and the id already carries its owner.
        let action = match action {
            Action::Place(mut intent) => {
                intent.strategy = self.strategy;
                if intent.decided_ns == 0 {
                    intent.decided_ns = self.now_ns;
                }
                Action::Place(intent)
            }
            Action::RecordQuoteFill { mut features } => {
                features.strategy = self.strategy;
                Action::RecordQuoteFill { features }
            }
            other => other,
        };
        self.out.push_back(action);
    }

    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        self.timers
            .arm(self.strategy, id, self.now_ns.saturating_add(after_ns));
    }

    fn resting<'a>(&'a self, out: &mut Vec<RestingOrder<'a>>) {
        for (id, order) in &self.orders.orders {
            // Someone else's order is none of this strategy's business, and
            // an order the log has already ended cannot be pulled or moved.
            if !order.in_flight() || self.registry.owner_of(id) != Some(self.strategy) {
                continue;
            }
            let request = &order.request;
            out.push(RestingOrder {
                client_order_id: id.as_str(),
                symbol: request.symbol,
                side: request.side,
                kind: request.kind,
                qty: request.qty,
                filled_qty: order.filled_qty,
                reduce_only: request.reduce_only,
                acked: order.acked,
            });
        }
    }

    fn order_facts(&self, client_order_id: &str) -> Option<engine_types::OrderFacts> {
        // The ledger rather than the registry, for the same reason
        // `owner_of` prefers it: terminal news can arrive for an order an
        // earlier boot sent. Another strategy's order stays none of this
        // one's business.
        let order = self.orders.orders.get(client_order_id)?;
        if order.request.strategy != self.strategy {
            return None;
        }
        Some(engine_types::OrderFacts {
            symbol: order.request.symbol,
            side: order.request.side,
            qty: order.request.qty,
            filled_qty: order.filled_qty,
            reduce_only: order.request.reduce_only,
        })
    }
}

#[cfg(test)]
mod tests {
    use std::sync::OnceLock;

    use super::*;
    use engine_types::{Intent, OrderKind, OrderRequest, OrderUpdate, Side, WalRecord};

    fn sent(id: &str, strategy: StrategyId) -> WalRecord {
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: id.into(),
                strategy,
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Limit {
                    px: 100.0,
                    tif: engine_types::TimeInForce::Gtc,
                },
                stop: None,
                reduce_only: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    /// An account reading with nothing open, which is what most of these
    /// tests want the context to be sitting on.
    fn flat_account() -> AccountView {
        AccountView {
            equity_usdt: 1_000.0,
            available_usdt: 1_000.0,
            positions: Vec::new(),
            observed_ns: 1,
        }
    }

    /// One context over a hand-built book, so the filters can be read off
    /// directly instead of through a whole engine run.
    fn ctx_over<'a>(
        market: &'a MarketState,
        out: &'a mut VecDeque<Action>,
        timers: &'a mut Timers,
        orders: &'a LedgerOfOrders,
        registry: &'a OrderRegistry,
        strategy: StrategyId,
    ) -> Ctx<'a> {
        /// An empty attribution: no fill has been charged to anybody, which is
        /// what every test that is not about attribution means.
        static NOBODY: OnceLock<Attribution> = OnceLock::new();
        static FLAT: OnceLock<AccountView> = OnceLock::new();
        /// An empty cover book: nothing sent ahead of the reading.
        static NO_COVERS: OnceLock<CoverBook> = OnceLock::new();
        Ctx {
            market,
            account: FLAT.get_or_init(flat_account),
            rules: &[],
            now_ns: 42,
            strategy,
            out,
            timers,
            orders,
            registry,
            attribution: NOBODY.get_or_init(Attribution::default),
            covers: NO_COVERS.get_or_init(CoverBook::default),
        }
    }

    #[test]
    fn timers_fire_in_time_order_and_are_scoped_per_strategy() {
        let mut timers = Timers::default();
        timers.arm(StrategyId(1), TimerId(1), 300);
        timers.arm(StrategyId(0), TimerId(1), 100);
        assert_eq!(timers.next_deadline(), Some(100));
        assert_eq!(timers.pop_due(150), Some((StrategyId(0), TimerId(1))));
        assert_eq!(timers.pop_due(150), None, "strategy 1's timer is not due");
        assert_eq!(timers.pop_due(300), Some((StrategyId(1), TimerId(1))));
        assert_eq!(timers.pop_due(300), None);
        assert_eq!(timers.armed_count(), 0);
    }

    #[test]
    fn rearming_the_same_number_replaces_the_old_deadline() {
        let mut timers = Timers::default();
        timers.arm(StrategyId(0), TimerId(7), 100);
        timers.arm(StrategyId(0), TimerId(7), 500);
        assert_eq!(timers.next_deadline(), Some(500));
        assert_eq!(timers.pop_due(200), None);
        assert_eq!(timers.pop_due(600), Some((StrategyId(0), TimerId(7))));
        assert_eq!(timers.pop_due(600), None, "only one firing");
    }

    #[test]
    fn emit_files_the_intent_under_the_calling_strategy() {
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let mut ctx = ctx_over(
            &market,
            &mut out,
            &mut timers,
            &orders,
            &registry,
            StrategyId(3),
        );
        ctx.place(Intent {
            strategy: StrategyId(9),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            tag: "t".into(),
            decided_ns: 0,
            work: None,
            leverage: None,
        });
        let Some(Action::Place(got)) = out.pop_front() else {
            panic!("expected a placement");
        };
        assert_eq!(got.strategy, StrategyId(3));
        assert_eq!(got.decided_ns, 42);
    }

    #[test]
    fn a_cancel_reaches_the_engine_naming_the_order_it_pulls() {
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let mut ctx = ctx_over(
            &market,
            &mut out,
            &mut timers,
            &orders,
            &registry,
            StrategyId(3),
        );
        ctx.cancel(SymbolId(1), "eng-7");
        assert_eq!(
            out.pop_front(),
            Some(Action::Cancel {
                symbol: SymbolId(1),
                client_order_id: "eng-7".into(),
            })
        );
    }

    #[test]
    fn resting_shows_a_strategy_its_own_working_orders_and_nobody_elses() {
        // A strategy that could read another's book could cancel it too.
        let market = MarketState::default();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::from_records(&[
            sent("mine-open", StrategyId(1)),
            sent("theirs", StrategyId(2)),
            sent("mine-filled", StrategyId(1)),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill {
                    exec_id: String::new(),
                    client_order_id: "mine-filled".into(),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 100.0,
                    fee: 0.0,
                    is_maker: false,
                    venue_ts_ms: 0,
                    recv_ns: 0,
                },
            },
        ]);
        // The filled one stays owned: ownership outlives the order, so this
        // is the in-flight filter being tested and not a missing owner.
        let mut registry = OrderRegistry::new("eng-1-".into());
        registry.own("mine-open", StrategyId(1));
        registry.own("mine-filled", StrategyId(1));
        registry.own("theirs", StrategyId(2));

        let mut out = VecDeque::new();
        let ctx = ctx_over(
            &market,
            &mut out,
            &mut timers,
            &orders,
            &registry,
            StrategyId(1),
        );
        let mut seen = Vec::new();
        ctx.resting(&mut seen);
        let ids: Vec<&str> = seen.iter().map(|o| o.client_order_id).collect();
        assert_eq!(
            ids,
            vec!["mine-open"],
            "own and still working, nothing else"
        );
        assert_eq!(seen[0].px(), Some(100.0));
        assert_eq!(seen[0].remaining_qty(), 1.0);
        assert!(!seen[0].acked, "no ack has arrived for it");

        let mut out = VecDeque::new();
        let ctx = ctx_over(
            &market,
            &mut out,
            &mut timers,
            &orders,
            &registry,
            StrategyId(2),
        );
        let mut seen = Vec::new();
        ctx.resting(&mut seen);
        let ids: Vec<&str> = seen.iter().map(|o| o.client_order_id).collect();
        assert_eq!(ids, vec!["theirs"]);
    }

    const RULE: InstrumentRule = InstrumentRule {
        tick_size: 0.5,
        qty_step: 0.001,
        min_qty: 0.001,
        min_notional: 5.0,
    };

    fn holding(symbol: SymbolId, side: Side, qty: f64) -> PositionView {
        PositionView {
            symbol,
            side,
            qty,
            entry_px: 100.0,
            stop_attached: true,
            stop_px: 0.0,
            leverage: None,
        }
    }

    #[test]
    fn a_strategy_reads_the_position_the_rule_and_the_wall_clock_the_engine_holds() {
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let account = AccountView {
            positions: vec![holding(SymbolId(1), Side::Sell, 3.0)],
            ..flat_account()
        };
        let rules = [None, Some(RULE)];
        let attribution = Attribution::default();
        let covers = CoverBook::default();
        let ctx = Ctx {
            market: &market,
            account: &account,
            rules: &rules,
            now_ns: 42,
            strategy: StrategyId(0),
            out: &mut out,
            timers: &mut timers,
            orders: &orders,
            registry: &registry,
            attribution: &attribution,
            covers: &covers,
        };

        assert_eq!(
            ctx.position(SymbolId(1)),
            Some(holding(SymbolId(1), Side::Sell, 3.0))
        );
        assert_eq!(
            ctx.position(SymbolId(0)),
            None,
            "nothing is held in that one"
        );
        assert_eq!(ctx.instrument(SymbolId(1)), Some(RULE));
        assert_eq!(
            ctx.instrument(SymbolId(0)),
            None,
            "the venue named no rule for it"
        );
        assert_eq!(
            ctx.instrument(SymbolId(7)),
            None,
            "past the end of the table"
        );

        // Wall time, not the monotonic stamp: a book's validity window can
        // only be judged against a clock of the same kind.
        let wall = ctx.wall_ms();
        let now = crate::clock::wall_ms();
        assert!(
            wall > 1_600_000_000_000,
            "expected a unix millisecond stamp, got {wall}"
        );
        assert!(
            (now - wall).abs() < 60_000,
            "the ctx clock is the engine's: {wall} vs {now}"
        );
        assert_ne!(
            wall as u64,
            ctx.now_ns(),
            "the two clocks are not the same clock"
        );
    }

    #[test]
    fn a_flat_row_in_the_account_reading_is_not_a_position() {
        // A venue that reports a symbol it once held with size zero must not
        // read as something to exit: the exit would be an order for nothing.
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let account = AccountView {
            positions: vec![holding(SymbolId(0), Side::Buy, 0.0)],
            ..flat_account()
        };
        let attribution = Attribution::default();
        let covers = CoverBook::default();
        let ctx = Ctx {
            market: &market,
            account: &account,
            rules: &[],
            now_ns: 42,
            strategy: StrategyId(0),
            out: &mut out,
            timers: &mut timers,
            orders: &orders,
            registry: &registry,
            attribution: &attribution,
            covers: &covers,
        };
        assert_eq!(ctx.position(SymbolId(0)), None);
    }

    #[test]
    fn resting_appends_and_leaves_the_buffer_reusable() {
        // The contract is "appended to out", so a strategy can keep one
        // buffer between wakes and pay nothing for the read.
        let market = MarketState::default();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::from_records(&[sent("a", StrategyId(0))]);
        let mut registry = OrderRegistry::default();
        registry.own("a", StrategyId(0));
        let mut out = VecDeque::new();
        let ctx = ctx_over(
            &market,
            &mut out,
            &mut timers,
            &orders,
            &registry,
            StrategyId(0),
        );
        let mut seen = Vec::with_capacity(8);
        ctx.resting(&mut seen);
        ctx.resting(&mut seen);
        assert_eq!(seen.len(), 2, "the second read appended, it did not clear");
        seen.clear();
        ctx.resting(&mut seen);
        assert_eq!(seen.len(), 1);
    }
}
