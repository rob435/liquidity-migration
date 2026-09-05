use super::*;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn maintain_signal_routes<M: MarketFeed>(
        &mut self,
        market: &mut M,
    ) -> Result<(), EngineError> {
        self.maintain_portfolio_routes(market)?;
        if !self.pending_mutations.is_empty() {
            return Ok(());
        }
        let producers: Vec<_> = self.signals.producers().cloned().collect();
        for mut producer in producers {
            let mut released = Vec::new();
            for route in &mut producer.routes {
                let sid = route.destination;
                if self.host.effects.earliest(sid).is_some()
                    || self.host.callbacks.order_news.unread_for(sid)
                    || self.host.callbacks.pages.owner_pending(sid)
                    || self
                        .host
                        .pending
                        .iter()
                        .chain(self.ready_actions.iter().map(|(action, _)| action))
                        .chain(self.dispatches.waiting.iter().map(|(action, _)| action))
                        .chain(
                            self.deferred_actions
                                .values()
                                .flat_map(|rows| rows.iter().map(|(action, _)| action)),
                        )
                        .any(|action| action.caller.is_none_or(|caller| caller == sid))
                    || self
                        .host
                        .events
                        .values()
                        .any(|event| event.destination == sid || event.source == sid)
                    || self.host.callbacks.state.inputs.values().any(|input| {
                        input.strategy == sid
                            && !matches!(
                                input.event,
                                engine_types::strategy_process::CallbackEvent::Signal { .. }
                            )
                    })
                    || self.host.callbacks.unwritten.iter().any(|input| {
                        input.strategy == sid
                            && !matches!(
                                input.event,
                                engine_types::strategy_process::CallbackEvent::Signal { .. }
                            )
                    })
                {
                    continue;
                }
                let manifest = if self.host.callbacks.isolated() {
                    self.host
                        .callbacks
                        .state
                        .committed
                        .get(&sid)
                        .and_then(|state| state.retained_signal_subscriptions.clone())
                } else {
                    self.host.strategies[sid.idx()].retained_signal_subscriptions()
                };
                let Some(mut needed) = manifest else {
                    continue;
                };
                needed.extend(self.host.strategies[sid.idx()].subscriptions());
                needed.extend(
                    self.signals
                        .suspensions()
                        .filter(|row| row.destination == sid)
                        .flat_map(|row| match row.reason {
                            engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget {
                                subscriptions,
                            } => subscriptions,
                        }),
                );
                needed.extend(
                    self.signals
                        .observations()
                        .chain(self.pending_signal_deliveries.iter())
                        .filter(|row| row.destination == sid)
                        .flat_map(|row| row.subscriptions.iter().cloned()),
                );
                needed.extend(
                    self.host
                        .callbacks
                        .state
                        .inputs
                        .values()
                        .chain(self.host.callbacks.unwritten.iter())
                        .filter(|input| input.strategy == sid)
                        .filter_map(|input| match &input.event {
                            engine_types::strategy_process::CallbackEvent::Signal {
                                observation,
                            } => Some(observation),
                            _ => None,
                        })
                        .flat_map(|row| row.subscriptions.iter().cloned()),
                );
                let protected: std::collections::BTreeSet<_> = self
                    .books
                    .attribution
                    .symbols(sid)
                    .chain(self.books.covers.symbols(sid))
                    .chain(
                        self.dispatches
                            .orders
                            .values()
                            .filter(|order| order.request.strategy == sid)
                            .map(|order| order.request.symbol),
                    )
                    .chain(
                        self.books
                            .orders
                            .in_flight()
                            .iter()
                            .filter(|order| order.request.strategy == sid)
                            .map(|order| order.request.symbol),
                    )
                    .chain(
                        self.books
                            .account
                            .positions
                            .iter()
                            .filter(|position| position.qty != 0.0)
                            .map(|position| position.symbol),
                    )
                    .collect();
                route.subscriptions.retain(|subscription| {
                    let keep = needed.contains(subscription)
                        || self
                            .books
                            .market
                            .table
                            .get(&subscription.symbol)
                            .is_some_and(|symbol| protected.contains(&symbol));
                    if !keep {
                        released.push((sid, subscription.clone()));
                    }
                    keep
                });
            }
            if released.is_empty() {
                continue;
            }
            self.persist_signal_lifecycle(producer)?;
            for (sid, subscription) in released {
                if let Some(symbol) = self.books.market.table.get(&subscription.symbol) {
                    let retained = self.signals.producer_routes().any(|route| {
                        route.destination == sid && route.subscriptions.contains(&subscription)
                    }) || self.host.strategies[sid.idx()]
                        .subscriptions()
                        .contains(&subscription);
                    if retained {
                        continue;
                    }
                    self.routing.remove(symbol, subscription.feed, sid);
                    if !self.portfolio_subscriptions.contains(&subscription)
                        && !self.routing.listens(symbol, subscription.feed)
                        && market.retire(&subscription.symbol, subscription.feed)
                    {
                        self.subscriptions.retain(|row| row != &subscription);
                    }
                }
            }
        }
        let suspended: Vec<_> = self.signals.suspensions().collect();
        for row in suspended {
            let routes: Vec<_> = self
                .signals
                .producer_routes()
                .filter(|route| route.destination == row.destination)
                .collect();
            let mut union = Vec::new();
            for subscription in routes.iter().flat_map(|route| &route.subscriptions) {
                if !union.contains(subscription) {
                    union.push(subscription.clone());
                }
            }
            let engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget { subscriptions } =
                &row.reason;
            for subscription in subscriptions {
                if !union.contains(subscription) {
                    union.push(subscription.clone());
                }
            }
            if !routes.is_empty() && union.len() <= engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS {
                self.wal.append(&WalRecord::SignalAdmissionChanged {
                    destination: row.destination,
                    suspension: None,
                })?;
                self.wal.barrier()?;
                self.signals
                    .set_suspension(row.destination, None, self.host.strategies.len())
                    .map_err(EngineError::State)?;
            }
        }
        Ok(())
    }
}
