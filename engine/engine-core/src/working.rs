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

use engine_types::order_terms::{strategy_decimal, ExactAmendTerms, OrderInputPolicy};
use engine_types::{Action, AmendSpec, InstrumentRule, MarketState, Quote, SymbolId, WorkPolicy};

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
    cancel_on_recovery: bool,
    cross_requested: bool,
    cross_confirmed: bool,
}

/// The orders this engine is working, by the client order id it minted.
#[derive(Default)]
pub struct WorkingOrders {
    orders: BTreeMap<String, Worked>,
}

impl WorkingOrders {
    pub fn recover(
        ledger: &LedgerOfOrders,
        venue_ids: &std::collections::BTreeSet<String>,
        now_ns: u64,
    ) -> Self {
        let mut restored = Self::default();
        for record in ledger.in_flight() {
            let request = &record.request;
            let (Some(policy), engine_types::OrderKind::Limit { px, .. }) =
                (record.entry_work, request.kind)
            else {
                continue;
            };
            if request.is_sleeve_reduction() || !venue_ids.contains(&request.client_order_id) {
                continue;
            }
            restored.orders.insert(
                request.client_order_id.clone(),
                Worked {
                    symbol: request.symbol,
                    policy,
                    state: WorkState::new(request.side, px, record.arrival_mid, now_ns),
                    cancel_on_recovery: true,
                    cross_requested: false,
                    cross_confirmed: false,
                },
            );
        }
        restored
    }

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
                cancel_on_recovery: false,
                cross_requested: false,
                cross_confirmed: false,
            },
        );
    }

    pub fn crossing_candidates(&self) -> impl Iterator<Item = (&str, SymbolId)> {
        self.orders
            .iter()
            .filter(|(_, w)| w.cross_requested && !w.cross_confirmed)
            .map(|(id, w)| (id.as_str(), w.symbol))
    }

    pub fn waiting_to_cross(&self, id: &str) -> bool {
        self.orders
            .get(id)
            .is_some_and(|w| w.cross_requested && !w.cross_confirmed)
    }

    pub fn confirm_cross(&mut self, id: &str) {
        if let Some(worked) = self.orders.get_mut(id) {
            worked.cross_confirmed = true;
        }
    }

    pub fn retry_cross_cancel(&mut self, id: &str) {
        if let Some(worked) = self.orders.get_mut(id) {
            worked.state.cancel_requested = false;
        }
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
            if worked.cross_requested {
                let expired = now_ns.saturating_sub(worked.state.cross_started_ns)
                    >= worked.policy.cross_grace_ms.saturating_mul(1_000_000);
                if !record.in_flight() && worked.cross_confirmed {
                    if !expired && worked.cross_confirmed {
                        if let Some(intent) = cross_remainder(record, market, now_ns) {
                            out.push_back(Action::Place(intent));
                        }
                    }
                    done.push(id.clone());
                } else if record.in_flight()
                    && !worked.state.cancel_requested
                    && now_ns.saturating_sub(worked.state.last_cancel_try_ns)
                        >= worked.policy.reprice_ms.saturating_mul(1_000_000)
                {
                    apply(
                        id,
                        worked,
                        WorkDecision {
                            step: WorkStep::Cancel,
                            looked: true,
                        },
                        now_ns,
                        out,
                    );
                }
                continue;
            }
            // Filled, cancelled, rejected, or written down in shadow and
            // never sent: its life is over.
            if !record.in_flight() {
                done.push(id.clone());
                continue;
            }
            // The old monotonic deadline is gone. Cancel the recovered
            // remainder and let the strategy re-evaluate after its ending.
            if worked.cancel_on_recovery {
                if !worked.state.cancel_requested
                    && (worked.state.last_cancel_try_ns == 0
                        || now_ns.saturating_sub(worked.state.last_cancel_try_ns)
                            >= worked.policy.reprice_ms.saturating_mul(1_000_000))
                {
                    apply(
                        id,
                        worked,
                        WorkDecision {
                            step: WorkStep::Cancel,
                            looked: true,
                        },
                        now_ns,
                        out,
                    );
                }
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
            let mut decision = plan::plan_work(&worked.state, touch, &rule, now_ns, &worked.policy);
            if matches!(
                record.request.kind,
                engine_types::OrderKind::Limit {
                    tif: engine_types::TimeInForce::PostOnly,
                    ..
                }
            ) && matches!(
                decision.step,
                WorkStep::Cross { .. } | WorkStep::CrossUnpriced
            ) {
                worked.cross_requested = true;
                start_crossing(worked, now_ns);
                decision.step = WorkStep::Cancel;
            }
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
            exact_terms: strategy_decimal(px).ok().map(|limit_price| {
                Box::new(ExactAmendTerms {
                    quantity: None,
                    limit_price: Some(limit_price),
                    input_policy: OrderInputPolicy::StrategyShortestDecimal,
                })
            }),
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

fn cross_remainder(
    record: &crate::inflight::OrderRec,
    market: &MarketState,
    now_ns: u64,
) -> Option<engine_types::Intent> {
    let request = &record.request;
    let quantity = record.remaining_exact().ok()?;
    if !quantity.is_positive() {
        return None;
    }
    let touch = touch_of(market.quotes.get(request.symbol.0 as usize)?);
    if !touch.readable() {
        return None;
    }
    let price = match request.side {
        engine_types::Side::Buy => touch.ask_px,
        engine_types::Side::Sell => touch.bid_px,
    };
    let mut prices = request.canonical_intent_prices().unwrap_or_else(|| {
        Box::new(engine_types::orders::IntentPrices {
            limit_price: None,
            stop_trigger_price: request
                .sleeve_stop()
                .and_then(|s| strategy_decimal(s.trigger_px).ok()),
        })
    });
    prices.limit_price = Some(strategy_decimal(price).ok()?);
    Some(engine_types::Intent {
        strategy: request.strategy,
        symbol: request.symbol,
        side: request.side,
        qty: quantity.to_f64().ok()?,
        exact_quantity: Some(Box::new(quantity)),
        exact_prices: Some(prices),
        kind: engine_types::OrderKind::Limit {
            px: price,
            tif: engine_types::TimeInForce::Ioc,
        },
        stop: request.sleeve_stop(),
        reduce_only: false,
        tag: cross_tag(&request.client_order_id),
        decided_ns: now_ns,
        work: None,
        leverage: None,
    })
}

/// The tag a crossed remainder carries. Read back by [`worked_order_of`],
/// which is the only reader: format and parse stay together.
fn cross_tag(client_order_id: &str) -> String {
    format!("work-cross:{client_order_id}")
}

/// The resting order one of this supervisor's own actions continues, for
/// `Cause::Working` on the log. A crossed remainder is a fresh order, so its
/// predecessor is named by the tag it carries; an amend and a cancel name it
/// directly.
pub(crate) fn worked_order_of(action: &engine_types::Action) -> String {
    match action {
        Action::Amend {
            client_order_id, ..
        }
        | Action::Cancel {
            client_order_id, ..
        } => client_order_id.clone(),
        Action::Place(intent) => intent
            .tag
            .strip_prefix("work-cross:")
            .unwrap_or_default()
            .to_string(),
        _ => String::new(),
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
