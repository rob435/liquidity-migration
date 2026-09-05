use super::*;

/// How a durable signal reaches a strategy: validated against the cursor,
/// held until every symbol it names is followed by all four id tables,
/// journaled with a barrier, then delivered. Gaps suspend the destination's
/// openings and ask the feed for the missing prefix.
impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn update_signal_requests<F: SignalFeed>(
        &self,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        let requests: Vec<_> = self
            .signals
            .gaps()
            .filter(|gap| self.signals.prefix_capacity(gap.destination))
            .map(|gap| engine_types::SignalGapRequest {
                source: gap.source.clone(),
                next_sequence: gap.next_sequence,
            })
            .collect();
        let blocked_destinations: Vec<_> = self
            .signal_dependencies
            .iter()
            .enumerate()
            .filter(|(id, _)| {
                let destination = StrategyId(*id as u16);
                self.signal_inputs_blocked(destination)
                    || self.signals.consumer_pending(destination)
                    || !self.signals.ordinary_capacity()
            })
            .map(|(id, _)| StrategyId(id as u16))
            .collect();
        feed.set_gap_requests(&requests, &blocked_destinations)
            .map_err(|error| EngineError::State(error.to_string()))
    }

    pub(super) fn accept_signal_frontiers<F: SignalFeed>(
        &mut self,
        frontiers: Vec<engine_types::SignalSourceFrontier>,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        if self.identities.scope.is_some() && self.signals.readiness_required() {
            return self.refuse_signal_readiness(
                "named source destinations require lifecycle readiness",
                feed,
            );
        }
        if self.signals.producers().next().is_some() {
            return self.refuse_signal_readiness(
                "managed producer cannot downgrade to readiness schema one",
                feed,
            );
        }
        let gaps = match self
            .signals
            .frontier_gaps(&frontiers, self.host.strategies.len())
        {
            Ok(gaps) => gaps,
            Err(reason) => return self.refuse_signal_readiness(&reason, feed),
        };
        for gap in gaps {
            if self.signals.gap_changed(&gap) {
                self.wal.append(&WalRecord::SignalGapRecorded {
                    wall_ts_ms: clock::wall_ms(),
                    gap: gap.clone(),
                })?;
                self.wal.barrier()?;
                self.signals.record_gap(gap);
            }
        }
        self.signals.set_frontiers(frontiers);
        self.update_signal_requests(feed)?;
        self.queue_halted_entry_cancels()?;
        Ok(())
    }

    pub(super) fn refuse_signal_readiness<F: SignalFeed>(
        &mut self,
        reason: &str,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        self.signals.begin_readiness_request();
        tracing::error!(%reason, "producer readiness refused; dependent growth remains suspended");
        self.wal.append(&WalRecord::Note {
            source: "signals".into(),
            text: format!("producer readiness refused: {reason}"),
        })?;
        self.queue_halted_entry_cancels()?;
        feed.request_lifecycle(
            self.signals.producers().cloned().collect(),
            self.signals.lifecycle_legacy_sources(),
        )
        .map_err(|error| EngineError::State(error.to_string()))
    }

    pub(super) fn persist_signal_lifecycle(
        &mut self,
        state: engine_types::SignalProducerLifecycle,
    ) -> Result<(), EngineError> {
        self.signals
            .validate_producer_lifecycle(&state, self.host.strategies.len())
            .map_err(EngineError::State)?;
        self.wal.append(&WalRecord::SignalProducerLifecycle {
            wall_ts_ms: clock::wall_ms(),
            state: state.clone(),
        })?;
        self.wal.barrier()?;
        self.signals
            .apply_producer_lifecycle(state, self.host.strategies.len())
            .map_err(EngineError::State)
    }

    pub(super) fn accept_signal_lifecycle<F: SignalFeed>(
        &mut self,
        response: engine_types::SignalLifecycleResponse,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        if response.schema_version != engine_types::SIGNAL_LIFECYCLE_SCHEMA_VERSION {
            return self.refuse_signal_readiness("unsupported producer lifecycle schema", feed);
        }
        if self.identities.scope.is_some() {
            let valid = response.source_sleeves.len() == response.producer.sources.len()
                && response.producer.sources.iter().all(|source| {
                    let bindings: Vec<_> = response
                        .source_sleeves
                        .iter()
                        .filter(|binding| binding.source == source.source)
                        .collect();
                    bindings.len() == 1
                        && self.identities.sleeves.get(source.destination.idx())
                            == Some(&bindings[0].sleeve)
                });
            if !valid {
                return self.refuse_signal_readiness(
                    "producer source destinations do not match the durable sleeve registry",
                    feed,
                );
            }
        }
        if response.producer.sealed
            && self.pending_signal_deliveries.iter().any(|pending| {
                response.producer.sources.iter().any(|source| {
                    source.source == pending.source && pending.sequence > source.published_through
                })
            })
        {
            return self.refuse_signal_readiness(
                "producer seal omits an input already waiting for symbol admission",
                feed,
            );
        }
        let state = match self
            .signals
            .plan_producer_report(&response.producer, self.host.strategies.len())
        {
            Ok(state) => state,
            Err(reason) => return self.refuse_signal_readiness(&reason, feed),
        };
        let gaps = match self.signals.lifecycle_gaps(&state) {
            Ok(gaps) => gaps,
            Err(reason) => return self.refuse_signal_readiness(&reason, feed),
        };
        if self
            .signals
            .producers()
            .find(|known| known.producer == state.producer)
            != Some(&state)
        {
            self.persist_signal_lifecycle(state.clone())?;
        }
        for gap in gaps {
            if self.signals.gap_changed(&gap) {
                self.wal.append(&WalRecord::SignalGapRecorded {
                    wall_ts_ms: clock::wall_ms(),
                    gap: gap.clone(),
                })?;
                self.wal.barrier()?;
                self.signals.record_gap(gap);
            }
        }
        if self.identities.scope.is_some() {
            self.signals.set_named_producer(&response.producer);
        }
        if state.legacy.is_empty()
            && !state.unresolved_tail
            && response.producer.epoch.is_some()
            && !response.producer.sealed
        {
            let frontiers = response.producer.sources;
            let gaps = match self
                .signals
                .frontier_gaps(&frontiers, self.host.strategies.len())
            {
                Ok(gaps) => gaps,
                Err(reason) => return self.refuse_signal_readiness(&reason, feed),
            };
            for gap in gaps {
                if self.signals.gap_changed(&gap) {
                    self.wal.append(&WalRecord::SignalGapRecorded {
                        wall_ts_ms: clock::wall_ms(),
                        gap: gap.clone(),
                    })?;
                    self.wal.barrier()?;
                    self.signals.record_gap(gap);
                }
            }
            self.signals.set_producer_frontiers(frontiers);
        } else {
            self.signals.clear_readiness();
        }
        self.advance_signal_lifecycles(feed)?;
        self.update_signal_requests(feed)?;
        self.queue_halted_entry_cancels()
    }

    pub(super) fn advance_signal_lifecycles<F: SignalFeed>(
        &mut self,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        let updates = self
            .signals
            .lifecycle_advances()
            .map_err(EngineError::State)?;
        if updates.is_empty() {
            return Ok(());
        }
        for state in updates {
            self.persist_signal_lifecycle(state)?;
        }
        self.signals.begin_readiness_request();
        feed.request_lifecycle(
            self.signals.producers().cloned().collect(),
            self.signals.lifecycle_legacy_sources(),
        )
        .map_err(|error| EngineError::State(error.to_string()))
    }

    pub(super) fn queue_signal_observation<F: SignalFeed>(
        &mut self,
        observation: SignalObservation,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        crate::signals::validate(&observation).map_err(EngineError::State)?;
        if observation.destination.0 as usize >= self.host.strategies.len() {
            return Err(EngineError::State(format!(
                "signal {} #{} addresses strategy {}, but only {} are configured",
                observation.source,
                observation.sequence,
                observation.destination.0,
                self.host.strategies.len()
            )));
        }
        if !crate::signals::signal_available(&observation, clock::wall_ms()) {
            return feed
                .defer_last(observation)
                .map_err(|error| EngineError::State(error.to_string()));
        }
        let admission = self
            .signals
            .classify(&observation)
            .map_err(EngineError::State)?;
        if admission != crate::signal_state::Admission::Duplicate
            && !self.host.callbacks.is_active(observation.destination)
        {
            return feed
                .defer_last(observation)
                .map_err(|error| EngineError::State(error.to_string()));
        }
        if admission != crate::signal_state::Admission::Duplicate
            && self.identities.scope.is_some()
            && self.signals.named_source_required(observation.destination)
            && !self
                .signals
                .named_source_verified(&observation.source, observation.destination)
        {
            return feed
                .defer_last(observation)
                .map_err(|error| EngineError::State(error.to_string()));
        }
        if admission == crate::signal_state::Admission::Unregistered {
            let producer = self
                .signals
                .producers()
                .find(|state| {
                    state
                        .routes
                        .iter()
                        .any(|route| route.destination == observation.destination)
                })
                .cloned();
            if let Some(mut state) = producer {
                if !state.unresolved_tail {
                    state.unresolved_tail = true;
                    self.persist_signal_lifecycle(state)?;
                }
            }
            self.signals.clear_readiness();
            self.queue_halted_entry_cancels()?;
            return feed
                .defer_last(observation)
                .map_err(|error| EngineError::State(error.to_string()));
        }
        if admission != crate::signal_state::Admission::Duplicate
            && self.signal_inputs_blocked(observation.destination)
            && !self
                .signals
                .gaps()
                .any(|gap| gap.source == observation.source)
        {
            return feed
                .defer_last(observation)
                .map_err(|error| EngineError::State(error.to_string()));
        }
        match admission {
            crate::signal_state::Admission::Duplicate => {
                return feed
                    .acknowledge_last()
                    .map_err(|error| EngineError::State(error.to_string()));
            }
            crate::signal_state::Admission::Gap(gap) => {
                if self.signals.gap_changed(&gap) {
                    self.wal.append(&WalRecord::SignalGapRecorded {
                        wall_ts_ms: clock::wall_ms(),
                        gap: gap.clone(),
                    })?;
                    self.wal.barrier()?;
                    tracing::error!(source = %gap.source, expected = gap.next_sequence,
                        observed = gap.observed_sequence, strategy = gap.destination.0,
                        "signal prefix missing; destination openings suspended until catch-up");
                    self.signals.record_gap(gap);
                    self.update_signal_requests(feed)?;
                    self.queue_halted_entry_cancels()?;
                }
                return feed
                    .defer_last(observation)
                    .map_err(|error| EngineError::State(error.to_string()));
            }
            crate::signal_state::Admission::Ready => {}
            crate::signal_state::Admission::Unregistered => {
                unreachable!("handled before admission")
            }
        }

        if !self.signals.can_accept(&observation) {
            return feed
                .defer_last(observation)
                .map_err(|error| EngineError::State(error.to_string()));
        }

        let mut durable = self
            .signals
            .route_subscriptions(&observation.source, observation.destination)
            .to_vec();
        for subscription in &observation.subscriptions {
            if !durable.contains(subscription) {
                durable.push(subscription.clone());
            }
        }
        if durable.len() > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS {
            return self.suspend_signal_subscription_budget(observation, feed);
        }

        for subscription in &observation.subscriptions {
            let listener = (observation.destination, subscription.feed);
            let subscribed = self.subscriptions.contains(subscription);
            if let Some(symbol) = self
                .books
                .market
                .table
                .get(&subscription.symbol)
                .filter(|_| subscribed)
            {
                self.routing
                    .add(symbol, subscription.feed, observation.destination);
                continue;
            }
            if let Some(wanted) = self
                .wanted_symbols
                .iter_mut()
                .find(|wanted| wanted.name == subscription.symbol)
            {
                if !wanted.listeners.contains(&listener) {
                    wanted.listeners.push(listener);
                }
            } else {
                self.wanted_symbols.push(WantedSymbol {
                    name: subscription.symbol.clone(),
                    listeners: vec![listener],
                });
            }
        }
        self.pending_signal_deliveries.push_back(observation);
        Ok(())
    }

    /// Append and barrier every fully admitted signal before reducer delivery.
    pub(super) fn accept_pending_signals<F: SignalFeed>(
        &mut self,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        let observations = std::mem::take(&mut self.pending_signal_deliveries);
        for observation in observations {
            if observation.subscriptions.iter().any(|subscription| {
                self.wanted_symbols
                    .iter()
                    .any(|wanted| wanted.name == subscription.symbol)
                    || self.symbol_admission.contains(&subscription.symbol)
            }) {
                self.pending_signal_deliveries.push_back(observation);
                continue;
            }
            if self
                .signals
                .classify(&observation)
                .map_err(EngineError::State)?
                != crate::signal_state::Admission::Ready
            {
                self.queue_signal_observation(observation, feed)?;
                continue;
            }
            // Symbol admission can yield while wall time is corrected.
            if !crate::signals::signal_available(&observation, clock::wall_ms()) {
                feed.defer_last(observation)
                    .map_err(|error| EngineError::State(error.to_string()))?;
                continue;
            }
            for subscription in &observation.subscriptions {
                let Some(symbol) = self.books.market.table.get(&subscription.symbol) else {
                    return Err(EngineError::State(format!(
                        "signal {} #{} symbol {} was not admitted",
                        observation.source, observation.sequence, subscription.symbol
                    )));
                };
                if !self.subscriptions.contains(subscription)
                    || self
                        .books
                        .rules
                        .get(symbol.0 as usize)
                        .copied()
                        .flatten()
                        .is_none()
                {
                    return Err(EngineError::State(format!(
                        "signal {} #{} symbol/feed/rule {} {:?} is incomplete",
                        observation.source,
                        observation.sequence,
                        subscription.symbol,
                        subscription.feed
                    )));
                }
            }
            if !self.signals.can_accept(&observation) {
                feed.defer_last(observation)
                    .map_err(|error| EngineError::State(error.to_string()))?;
                continue;
            }
            let known = self
                .signals
                .route_subscriptions(&observation.source, observation.destination);
            if known.len()
                + observation
                    .subscriptions
                    .iter()
                    .filter(|row| !known.contains(row))
                    .count()
                > engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
            {
                self.suspend_signal_subscription_budget(observation, feed)?;
                continue;
            }
            self.wal.append(&WalRecord::SignalObservation {
                wall_ts_ms: clock::wall_ms(),
                observation: observation.clone(),
            })?;
            self.wal.barrier()?;
            self.signals.accept(observation.clone());
            self.deliver_pending_signal_callbacks();
            feed.acknowledge_last()
                .map_err(|error| EngineError::State(error.to_string()))?;
        }
        self.update_signal_requests(feed)?;
        Ok(())
    }

    fn suspend_signal_subscription_budget<F: SignalFeed>(
        &mut self,
        observation: SignalObservation,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        let suspension = Some(
            engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget {
                subscriptions: observation.subscriptions.clone(),
            },
        );
        self.wal.append(&WalRecord::SignalAdmissionChanged {
            destination: observation.destination,
            suspension: suspension.clone(),
        })?;
        self.wal.barrier()?;
        self.signals
            .set_suspension(
                observation.destination,
                suspension,
                self.host.strategies.len(),
            )
            .map_err(EngineError::State)?;
        self.update_signal_requests(feed)?;
        self.queue_halted_entry_cancels()?;
        feed.defer_last(observation)
            .map_err(|error| EngineError::State(error.to_string()))
    }

    pub(super) fn deliver_pending_signal_callbacks(&mut self) {
        let rows: Vec<_> = self.signals.undelivered().cloned().collect();
        let now = clock::now_ns();
        for row in rows {
            if self.feed_one_strategy(row.destination, &EngineEvent::Signal(row.clone()), now) {
                self.signals.mark_delivered(&row.source, row.sequence);
            }
        }
    }
}

#[cfg(test)]
#[path = "signal_intake_tests.rs"]
mod tests;
