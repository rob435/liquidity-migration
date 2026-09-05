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
        let gaps = match self
            .signals
            .frontier_gaps(&frontiers, self.host.strategies.len())
        {
            Ok(gaps) => gaps,
            Err(reason) => return self.refuse_signal_readiness(reason, feed),
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
        reason: String,
        feed: &mut F,
    ) -> Result<(), EngineError> {
        self.signals.begin_readiness_request();
        tracing::error!(%reason, "producer readiness refused; dependent growth remains suspended");
        self.wal.append(&WalRecord::Note {
            source: "signals".into(),
            text: format!("producer readiness refused: {reason}"),
        })?;
        self.queue_halted_entry_cancels()?;
        feed.request_readiness()
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
            return Err(EngineError::State(format!(
                "signal source {} would retain {} subscriptions for strategy {}; maximum is {}",
                observation.source,
                durable.len(),
                observation.destination.0,
                engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
            )));
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
        let now = clock::now_ns();
        for observation in observations {
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
            self.wal.append(&WalRecord::SignalObservation {
                wall_ts_ms: clock::wall_ms(),
                observation: observation.clone(),
            })?;
            self.wal.barrier()?;
            self.signals.accept(observation.clone());
            self.feed_one_strategy(
                observation.destination,
                &EngineEvent::Signal(observation),
                now,
            );
            feed.acknowledge_last()
                .map_err(|error| EngineError::State(error.to_string()))?;
        }
        self.update_signal_requests(feed)?;
        Ok(())
    }

    /// Start following symbols a durable observation names that the engine
    /// did not know.
    ///
    /// Every table that maps a name to a `SymbolId` has to gain the symbol in
    /// the same order, because the id is an index assigned by position. Four
    /// of them exist — the engine's own, the public feed's, the venue
    /// gateway's, and the private stream's — and if any two disagreed, an
    /// order meant for one symbol would be sent for another. So this is the
    /// only place that admits, it admits one name at a time, and it checks
    /// that all four agree before the symbol is usable. A disagreement drops
    /// the symbol rather than trading it: the engine carries on with the names
    /// it already had, and says loudly which one it refused.
    pub(super) async fn admit_wanted<M, O>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
    ) -> Result<(), EngineError>
    where
        M: engine_types::MarketFeed,
        O: engine_types::OrderFeed,
    {
        let wanted = std::mem::take(&mut self.wanted_symbols);
        let mut admitted = 0usize;
        for wanted in wanted {
            let name = wanted.name;
            let core_id = self.books.market.add_symbol(&name);
            let venue_id = self.venue.add_symbol_async(&name).await?;
            let mut feeds = Vec::new();
            for (_, feed) in &wanted.listeners {
                if !feeds.contains(feed) {
                    feeds.push(*feed);
                }
            }
            let feed_ids: Vec<_> = feeds
                .iter()
                .map(|feed| (*feed, market_feed.admit(&name, *feed)))
                .collect();
            if feed_ids.iter().any(|(_, id)| *id != Some(core_id)) || venue_id != Some(core_id) {
                tracing::error!(
                    symbol = %name,
                    ?core_id,
                    ?feed_ids,
                    ?venue_id,
                    "the parts of the engine disagree about this symbol's id; it will not be \
                     traded. Nothing else is affected — the ids already handed out do not move."
                );
                return Err(EngineError::State(format!(
                    "signal-required symbol {name} has inconsistent ids: core {:?}, feed {:?}, venue {:?}",
                    core_id, feed_ids, venue_id
                )));
            }
            order_feed.learn(&name, core_id);
            self.routing.size_to(self.books.market.table.len());
            for (strategy, feed) in wanted.listeners {
                self.routing.add(core_id, feed, strategy);
            }
            for feed in feeds {
                let subscription = Subscription {
                    symbol: name.clone(),
                    feed,
                };
                if !self.subscriptions.contains(&subscription) {
                    self.subscriptions.push(subscription);
                }
            }
            admitted += 1;
            tracing::info!(symbol = %name, id = core_id.0, "following a symbol a signal named");
        }
        if admitted == 0 {
            return Ok(());
        }
        // The table grew, so say what it is now. Ids are only appended, so
        // this is the earlier one plus the new names.
        let names = names_record(&self.host.names, &self.books.market);
        self.wal.append(&names)?;
        self.fills.learn(&names);
        // One venue read covers everything admitted this pass. Without a rule
        // there is no way to quantize, so the symbol is followed but nothing
        // can be sent for it — which is the same state as a symbol whose rule
        // was missing at boot.
        self.books.rules.resize(self.books.market.table.len(), None);
        match self.venue.instrument_rules().await {
            Ok(fetched) => {
                for (name, rule) in fetched {
                    if let Some(id) = self.books.market.table.get(&name) {
                        self.books.rules[id.0 as usize] = Some(rule);
                    }
                }
            }
            Err(e) => tracing::warn!(
                error = %e,
                "no instrument rules for the symbols just taken on; they cannot trade until \
                 the next attempt"
            ),
        }
        for observation in &self.pending_signal_deliveries {
            for subscription in &observation.subscriptions {
                let Some(symbol) = self.books.market.table.get(&subscription.symbol) else {
                    continue;
                };
                if self
                    .books
                    .rules
                    .get(symbol.0 as usize)
                    .copied()
                    .flatten()
                    .is_none()
                {
                    return Err(EngineError::State(format!(
                        "signal-required symbol {} has no venue instrument rule",
                        subscription.symbol
                    )));
                }
            }
        }
        Ok(())
    }
}
