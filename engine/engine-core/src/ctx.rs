//! The window a strategy sees, and the timers it can set.
//!
//! A strategy gets read-only market state, the clock, its own resting orders,
//! a way to hand back an action, and one-shot timers. Nothing else: no venue,
//! no log, no other strategy's state. Timers are scoped per strategy, so two
//! strategies may both use timer 1 without colliding, and re-arming the same
//! number before it fires replaces the old one.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, VecDeque};

use engine_types::{
    Action, MarketState, Quote, RestingOrder, StrategyCtx, StrategyId, SymbolId, Ticker, TimerId,
};

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
}

impl StrategyCtx for Ctx<'_> {
    fn quote(&self, symbol: SymbolId) -> &Quote {
        self.market.quote(symbol)
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
}

#[cfg(test)]
mod tests {
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
        Ctx {
            market,
            now_ns: 42,
            strategy,
            out,
            timers,
            orders,
            registry,
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
                    client_order_id: "mine-filled".into(),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 100.0,
                    fee: 0.0,
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
        assert_eq!(ids, vec!["mine-open"], "own and still working, nothing else");
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
