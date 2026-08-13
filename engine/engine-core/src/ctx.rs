//! The window a strategy sees, and the timers it can set.
//!
//! A strategy gets read-only market state, the clock, a way to hand back an
//! intent, and one-shot timers. Nothing else: no venue, no log, no other
//! strategy's state. Timers are scoped per strategy, so two strategies may
//! both use timer 1 without colliding, and re-arming the same number before
//! it fires replaces the old one.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, VecDeque};

use engine_types::{
    Intent, MarketState, Quote, StrategyCtx, StrategyId, SymbolId, Ticker, TimerId,
};

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
    pub out: &'a mut VecDeque<Intent>,
    pub timers: &'a mut Timers,
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

    fn emit(&mut self, mut intent: Intent) {
        // The context knows whose callback this is, so the intent is filed
        // under that strategy whatever it claims.
        intent.strategy = self.strategy;
        if intent.decided_ns == 0 {
            intent.decided_ns = self.now_ns;
        }
        self.out.push_back(intent);
    }

    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        self.timers
            .arm(self.strategy, id, self.now_ns.saturating_add(after_ns));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderKind, Side};

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
        let mut ctx = Ctx {
            market: &market,
            now_ns: 42,
            strategy: StrategyId(3),
            out: &mut out,
            timers: &mut timers,
        };
        ctx.emit(Intent {
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
        let got = out.pop_front().unwrap();
        assert_eq!(got.strategy, StrategyId(3));
        assert_eq!(got.decided_ns, 42);
    }
}
