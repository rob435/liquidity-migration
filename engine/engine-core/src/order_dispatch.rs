use std::collections::{BTreeMap, BTreeSet, VecDeque};

use engine_types::order_dispatch::{OrderDispatchPhase, OrderDispatchState};
use engine_types::{WalError, WalRecord};

pub(crate) enum DispatchWrite {
    Stop(Vec<crate::engine::stop_runtime::DurableStop>),
    Portfolio,
    Queue(Vec<String>),
    Attempt(Vec<String>),
    Amend(Box<DurableAmend>),
}

pub(crate) struct DurableAmend {
    pub symbol: engine_types::SymbolId,
    pub client_order_id: String,
    pub spec: engine_types::AmendSpec,
    pub existing: crate::inflight::OrderRec,
    pub amended_intent: engine_types::Intent,
    pub remaining_qty: f64,
    pub old_px: f64,
    pub tif: engine_types::TimeInForce,
}

#[derive(Clone, Debug)]
pub(crate) struct RuntimeDispatch {
    pub state: OrderDispatchState,
    pub timing: Option<crate::ctx::CallbackTiming>,
}

impl std::ops::Deref for RuntimeDispatch {
    type Target = OrderDispatchState;

    fn deref(&self) -> &Self::Target {
        &self.state
    }
}

impl std::ops::DerefMut for RuntimeDispatch {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.state
    }
}

pub(crate) struct OrderDispatches {
    pub orders: BTreeMap<String, RuntimeDispatch>,
    pub write: Option<DispatchWrite>,
    pub barrier_started_ns: u64,
    pub durable: tokio::sync::mpsc::Receiver<Result<(), WalError>>,
    complete: tokio::sync::mpsc::Sender<Result<(), WalError>>,
    pub waiting: VecDeque<(crate::ctx::PendingAction, u64)>,
    pub unresolved: BTreeMap<String, String>,
    pub recovered: BTreeSet<String>,
    pub lookup_pending: BTreeSet<String>,
    /// Engine-clock instant before which an order is not looked up again.
    /// The engine's clock, not the wall's: the answer reaches the log.
    pub lookup_after: BTreeMap<String, u64>,
    pub lookups: tokio::sync::mpsc::Receiver<(
        String,
        Result<engine_types::orders::OrderLookup, engine_types::VenueError>,
    )>,
    pub lookup_results: tokio::sync::mpsc::Sender<(
        String,
        Result<engine_types::orders::OrderLookup, engine_types::VenueError>,
    )>,
}

impl OrderDispatches {
    pub fn replay(records: &[WalRecord]) -> Result<Self, String> {
        let mut orders = BTreeMap::new();
        for record in records {
            match record {
                WalRecord::SegmentBase {
                    pending_order_dispatches,
                    ..
                } => {
                    orders.clear();
                    for order in pending_order_dispatches {
                        Self::validate(order)?;
                        if orders
                            .insert(order.request.client_order_id.clone(), order.clone())
                            .is_some()
                        {
                            return Err("repeated dispatch in segment base".into());
                        }
                    }
                }
                WalRecord::OrderSent {
                    request,
                    dispatch: Some(dispatch),
                    ..
                } => {
                    let order = OrderDispatchState {
                        request: request.clone(),
                        intent: dispatch.intent.clone(),
                        origin_ns: dispatch.origin_ns,
                        phase: OrderDispatchPhase::Queued,
                    };
                    Self::validate(&order)?;
                    if orders
                        .insert(request.client_order_id.clone(), order)
                        .is_some()
                    {
                        return Err("repeated atomic order dispatch".into());
                    }
                }
                WalRecord::OrderDispatchQueued { order } => {
                    Self::validate(order)?;
                    if order.phase != OrderDispatchPhase::Queued {
                        return Err("queued dispatch has an attempted phase".into());
                    }
                    if let Some(previous) = orders.get(&order.request.client_order_id) {
                        if previous.phase != OrderDispatchPhase::Attempted
                            || previous.request != order.request
                            || previous.intent != order.intent
                        {
                            return Err("invalid or repeated queued order dispatch".into());
                        }
                    }
                    orders.insert(order.request.client_order_id.clone(), order.clone());
                }
                WalRecord::OrderDispatchAttempted { client_order_id } => {
                    let order = orders
                        .get_mut(client_order_id)
                        .ok_or("dispatch attempt has no queued order")?;
                    if order.phase != OrderDispatchPhase::Queued {
                        return Err("order dispatch attempt is repeated".into());
                    }
                    order.phase = OrderDispatchPhase::Attempted;
                }
                WalRecord::OrderDispatchCompleted { client_order_id } => {
                    orders.remove(client_order_id);
                }
                _ => {}
            }
        }
        let (complete, durable) = tokio::sync::mpsc::channel(1);
        let (lookup_results, lookups) = tokio::sync::mpsc::channel(10);
        let recovered = orders.keys().cloned().collect();
        Ok(Self {
            recovered,
            lookup_pending: BTreeSet::new(),
            lookup_after: BTreeMap::new(),
            lookups,
            lookup_results,
            orders: orders
                .into_iter()
                .map(|(id, state)| {
                    (
                        id,
                        RuntimeDispatch {
                            state,
                            timing: None,
                        },
                    )
                })
                .collect(),
            write: None,
            barrier_started_ns: 0,
            durable,
            complete,
            waiting: VecDeque::new(),
            unresolved: BTreeMap::new(),
        })
    }

    fn validate(order: &OrderDispatchState) -> Result<(), String> {
        let positive = |value: f64| value.is_finite() && value > 0.0;
        let valid_kind = |kind: engine_types::OrderKind| match kind {
            engine_types::OrderKind::Market => true,
            engine_types::OrderKind::Limit { px, .. } => positive(px),
        };
        if order.request.client_order_id.is_empty()
            || order.request.strategy != order.intent.strategy
            || order.request.symbol != order.intent.symbol
            || order.request.side != order.intent.side
            || !positive(order.request.qty)
            || !positive(order.intent.qty)
            || order.request.qty > order.intent.qty
            || order.request.is_sleeve_reduction() != order.intent.reduce_only
            || !valid_kind(order.request.kind)
            || !valid_kind(order.intent.kind)
            || order
                .request
                .stop
                .is_some_and(|stop| !positive(stop.trigger_px))
            || order
                .request
                .sleeve_stop()
                .is_some_and(|stop| !positive(stop.trigger_px))
            || order
                .intent
                .stop
                .is_some_and(|stop| !positive(stop.trigger_px))
        {
            return Err("order dispatch escapes its authorized intent".into());
        }
        if let Some(terms) = &order.request.exact_terms {
            terms
                .validate_projection(&order.request)
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    pub fn begin(&mut self, write: DispatchWrite, barrier: engine_types::wal::PendingBarrier) {
        assert!(self.write.is_none(), "dispatch durability has two owners");
        self.write = Some(write);
        self.barrier_started_ns = crate::clock::now_ns();
        let completed = self.complete.clone();
        if barrier.outstanding() {
            tokio::task::spawn_blocking(move || {
                let _ = completed.blocking_send(barrier.wait());
            });
        } else {
            let _ = completed.try_send(barrier.wait());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{Intent, OrderKind, OrderRequest, Side, StopSpec, StrategyId, SymbolId};

    fn order() -> OrderDispatchState {
        let intent = Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "durable-exit".into(),
            decided_ns: 1,
            work: None,
            leverage: None,
        };
        OrderDispatchState {
            request: OrderRequest {
                client_order_id: "exact-exit".into(),
                strategy: intent.strategy,
                symbol: intent.symbol,
                side: intent.side,
                qty: 0.5,
                kind: intent.kind,
                stop: None,
                reduce_only: true,
                close_position: false,
                sleeve_effect: None,
                exact_terms: None,
            },
            intent,
            phase: OrderDispatchPhase::Queued,
            origin_ns: 1,
        }
    }

    #[test]
    fn replay_refuses_dispatches_that_change_the_authorized_intent() {
        let valid = order();
        assert!(OrderDispatches::replay(&[WalRecord::OrderDispatchQueued {
            order: valid.clone()
        }])
        .is_ok());
        let mut cases = Vec::new();
        let mut changed = valid.clone();
        changed.request.reduce_only = false;
        cases.push(("logical effect", changed));
        let mut changed = valid.clone();
        changed.request.qty = 2.0;
        cases.push(("quantity", changed));
        let mut changed = valid.clone();
        changed.intent.qty = f64::NAN;
        cases.push(("intent quantity", changed));
        let mut changed = valid.clone();
        changed.request.kind = OrderKind::Limit {
            px: -1.0,
            tif: engine_types::TimeInForce::Gtc,
        };
        cases.push(("limit price", changed));
        let mut changed = valid.clone();
        changed.request.stop = Some(StopSpec {
            trigger_px: f64::NAN,
        });
        cases.push(("physical stop", changed));
        let mut changed = valid.clone();
        changed.request.sleeve_effect = Some(engine_types::orders::SleeveOrderEffect::Increase {
            stop: StopSpec { trigger_px: 100.0 },
        });
        cases.push(("sleeve effect", changed));
        let mut changed = valid;
        changed.request.exact_terms = Some(Box::new(engine_types::order_terms::ExactOrderTerms {
            quantity: engine_types::numeric::Exact::parse_decimal("0.7").unwrap(),
            limit_price: None,
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: engine_types::order_terms::OrderInputPolicy::StrategyShortestDecimal,
        }));
        cases.push(("exact projection", changed));
        for (name, order) in cases {
            assert!(
                OrderDispatches::replay(&[WalRecord::OrderDispatchQueued { order }]).is_err(),
                "replayed a dispatch with an unauthorized {name}"
            );
        }
    }
}
