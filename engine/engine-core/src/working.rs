//! Working the entries that are resting at the venue.
//!
//! Crossing the spread on the way in pays the taker fee. An intent that
//! carries a [`WorkPolicy`] instead goes out as a limit at the touch, and this
//! supervisor advances it: it moves with the book, escalates as the window
//! runs out, crosses at the end, and pulls the order if the cross cannot clear
//! it. The Rust tests in this module are the executable policy contract.
//!
//! It lives in the core rather than in a strategy on purpose. Every strategy
//! should get this, and no strategy should be writing a repricing loop — a
//! plug that had to reprice its own entries would be two decisions in one
//! object, and the second one would be wrong.
//!
//! It runs on the engine's existing group-flush tick, on the same single
//! thread as everything else. No thread, no task, no sleep: the loop's whole
//! latency story rests on there being nothing else running.
//!
//! Every action it wants goes into the same queue a strategy's actions go
//! into, so the flood cap in `drain` bounds it too. It never talks to the
//! venue itself.

pub mod plan;

use std::collections::BTreeMap;
use std::collections::VecDeque;

use engine_types::{
    Action, AmendSpec, InstrumentRule, MarketState, Quote, SymbolId, WorkPolicy,
};

use crate::inflight::LedgerOfOrders;
use plan::{WorkDecision, WorkState, WorkStep};

/// The touch as this module reads it, out of the engine's market picture.
pub fn touch_of(quote: &Quote) -> plan::Touch {
    plan::Touch {
        bid_px: quote.bid_px,
        bid_qty: quote.bid_qty,
        ask_px: quote.ask_px,
        ask_qty: quote.ask_qty,
    }
}

struct Worked {
    symbol: SymbolId,
    policy: WorkPolicy,
    state: WorkState,
}

/// The orders this engine is working, by the client order id it minted.
#[derive(Default)]
pub struct WorkingOrders {
    orders: BTreeMap<String, Worked>,
}

impl WorkingOrders {
    /// Start working an order that has just gone out. Build the state with
    /// [`WorkState::new`], from the price that is actually resting.
    pub fn take_on(
        &mut self,
        client_order_id: &str,
        symbol: SymbolId,
        policy: WorkPolicy,
        state: WorkState,
    ) {
        self.orders.insert(
            client_order_id.to_string(),
            Worked {
                symbol,
                policy,
                state,
            },
        );
    }

    pub fn len(&self) -> usize {
        self.orders.len()
    }

    pub fn is_empty(&self) -> bool {
        self.orders.is_empty()
    }

    /// One pass over every worked order: adopt the clock, decide, and queue
    /// whatever the decision asks for.
    pub fn pass(
        &mut self,
        now_ns: u64,
        market: &MarketState,
        rules: &[Option<InstrumentRule>],
        ledger: &LedgerOfOrders,
        out: &mut VecDeque<Action>,
    ) {
        let mut done: Vec<String> = Vec::new();
        for (id, worked) in self.orders.iter_mut() {
            // The log has no such order. Nothing to work, and nothing this
            // pass could do about it.
            let Some(record) = ledger.orders.get(id) else {
                done.push(id.clone());
                continue;
            };
            // Filled, cancelled, rejected, or written down in shadow and
            // never sent: its life is over.
            if !record.in_flight() {
                done.push(id.clone());
                continue;
            }
            let Some(rule) = rules.get(worked.symbol.0 as usize).copied().flatten() else {
                continue;
            };
            let touch = market
                .quotes
                .get(worked.symbol.0 as usize)
                .map(touch_of)
                .unwrap_or_default();
            let decision = plan::plan_work(&worked.state, touch, &rule, now_ns, &worked.policy);
            apply(id, worked, decision, now_ns, out);
        }
        for id in done {
            self.orders.remove(&id);
        }
    }

    /// What the venue did with a move this supervisor asked for.
    ///
    /// Only an amend the venue took changes anything. One it refused leaves
    /// the order resting where it was, at the price it was already at.
    pub fn amended(&mut self, client_order_id: &str, px: Option<f64>, taken: bool, now_ns: u64) {
        let Some(worked) = self.orders.get_mut(client_order_id) else {
            return;
        };
        if !taken {
            return;
        }
        let Some(px) = px else {
            return;
        };
        worked.state.px = px;
        if worked.state.cross_started_ns == 0 {
            worked.state.amends += 1;
            return;
        }
        // The venue took the crossing price, so the grace now runs from the
        // cross that actually happened rather than from the first attempt.
        worked.state.crossed = true;
        worked.state.cross_started_ns = now_ns;
    }

    /// What the venue did with the cancel this supervisor asked for.
    ///
    /// Latched on success only. This is the one cancel in the engine's
    /// working path, so a failure that latched here would leave a marketable
    /// limit resting at the venue with nothing left to take it down.
    pub fn cancelled(&mut self, client_order_id: &str, taken: bool) {
        let Some(worked) = self.orders.get_mut(client_order_id) else {
            return;
        };
        if taken {
            worked.state.cancel_requested = true;
        }
    }
}

/// Record what this pass decided and queue the action it asks for.
fn apply(
    id: &str,
    worked: &mut Worked,
    decision: WorkDecision,
    now_ns: u64,
    out: &mut VecDeque<Action>,
) {
    if decision.looked {
        // Stamped before anything is acted on, so a symbol whose book could
        // not be read stays paced on the cadence instead of being retried on
        // every tick.
        worked.state.last_look_ns = now_ns;
    }
    let symbol = worked.symbol;
    let reprice = |px: f64| Action::Amend {
        symbol,
        client_order_id: id.to_string(),
        // Price only. An amend that raised the size would have to be made
        // durable before the wire, and that fsync would land on every single
        // reprice.
        spec: AmendSpec {
            px: Some(px),
            qty: None,
        },
    };
    match decision.step {
        WorkStep::Hold => {}
        WorkStep::Move { px } => out.push_back(reprice(px)),
        WorkStep::Cross { px } => {
            start_crossing(worked, now_ns);
            out.push_back(reprice(px));
        }
        WorkStep::CrossUnpriced => start_crossing(worked, now_ns),
        WorkStep::Cancel => {
            worked.state.last_cancel_try_ns = now_ns;
            out.push_back(Action::Cancel {
                symbol,
                client_order_id: id.to_string(),
            });
        }
    }
}

/// The grace clock starts at the first cross attempt, priced or not, so an
/// order whose book was dark at the window end keeps trying instead of resting
/// untouched until the cancel. `crossed` is deliberately not set here: only an
/// amend the venue took has actually crossed.
fn start_crossing(worked: &mut Worked, now_ns: u64) {
    worked.state.last_cross_try_ns = now_ns;
    if worked.state.cross_started_ns == 0 {
        worked.state.cross_started_ns = now_ns;
    }
}

#[cfg(test)]
mod tests;
