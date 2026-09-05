use super::*;
use crate::order_dispatch::DispatchWrite;
use engine_types::order_dispatch::OrderDispatchPhase;
use engine_types::orders::{OrderLookup, TerminalOrderStatus};

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn queue_order_dispatches(
        &mut self,
        prepared: Vec<PreparedOrder>,
    ) -> Result<bool, EngineError> {
        if self.dispatches.write.is_some() {
            return Err(EngineError::State(
                "order admission overtook dispatch durability".into(),
            ));
        }
        let mut ids = Vec::with_capacity(prepared.len());
        for order in prepared {
            let mut intent = order.intent;
            intent.decided_ns = order.decided_ns;
            let id = order.request.client_order_id.clone();
            if !self.dispatches.orders.get(&id).is_some_and(|known| {
                known.request == order.request
                    && known.intent == intent
                    && known.phase == OrderDispatchPhase::Queued
                    && known.origin_ns == order.origin_ns
            }) {
                return Err(EngineError::State(
                    "prepared order has no atomic dispatch owner".into(),
                ));
            }
            ids.push(id);
        }
        if ids.is_empty() {
            return Ok(false);
        }
        let barrier = self.wal.barrier_begin()?;
        self.dispatches.begin(DispatchWrite::Queue(ids), barrier);
        Ok(true)
    }

    pub(super) async fn service_order_dispatches(&mut self) -> Result<(), EngineError> {
        if self.dispatches.write.is_some() {
            match self.dispatches.durable.try_recv() {
                Ok(result) => self.on_order_dispatch_durable(Some(result)).await?,
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => {}
                Err(_) => {
                    return Err(EngineError::TaskStopped {
                        task: EngineTask::DispatchDurability,
                        detail: "",
                    })
                }
            }
        }
        while let Ok((id, result)) = self.dispatches.lookups.try_recv() {
            self.on_order_lookup(id, result).await?;
        }
        if self.dispatches.write.is_none() {
            let queued: Vec<_> = self
                .dispatches
                .orders
                .iter()
                .filter(|(_, order)| order.phase == OrderDispatchPhase::Queued)
                .take(MAX_ORDERS_PER_BATCH)
                .map(|(id, _)| id.clone())
                .collect();
            if !queued.is_empty() {
                let barrier = self.wal.barrier_begin()?;
                self.dispatches.begin(DispatchWrite::Queue(queued), barrier);
            }
        }
        let now = std::time::Instant::now();
        let lookups: Vec<_> = self
            .dispatches
            .orders
            .iter()
            .filter(|(id, order)| {
                order.phase == OrderDispatchPhase::Attempted
                    && !self.dispatches.lookup_pending.contains(*id)
                    && !self.busy_symbols.contains_key(&order.request.symbol)
                    && self
                        .dispatches
                        .lookup_after
                        .get(*id)
                        .is_none_or(|deadline| *deadline <= now)
            })
            .take(usize::from(self.dispatches.lookup_pending.is_empty()))
            .map(|(id, order)| (id.clone(), order.request.symbol))
            .collect();
        for (id, symbol) in lookups {
            let receive = self
                .venue
                .dispatch_order_status(self.books.market.table.name(symbol), &id)?;
            self.dispatches.lookup_pending.insert(id.clone());
            let results = self.dispatches.lookup_results.clone();
            tokio::spawn(async move {
                let result = match tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, receive).await {
                    Ok(Ok(result)) => result,
                    Ok(Err(_)) => Err(VenueError::Transport("order lookup task stopped".into())),
                    Err(_) => Err(VenueError::Transport("order lookup timed out".into())),
                };
                let _ = results.send((id, result)).await;
            });
        }
        Ok(())
    }

    pub(super) async fn on_order_lookup(
        &mut self,
        id: String,
        result: Result<OrderLookup, VenueError>,
    ) -> Result<(), EngineError> {
        self.dispatches.lookup_pending.remove(&id);
        self.dispatches.lookup_after.insert(
            id.clone(),
            std::time::Instant::now() + Duration::from_secs(1),
        );
        if !self.dispatches.orders.contains_key(&id) {
            return Ok(());
        }
        match result {
            Ok(lookup) => self.apply_order_lookup(&id, lookup).await,
            Err(error) => {
                self.dispatches.unresolved.insert(id, error.to_string());
                Ok(())
            }
        }
    }

    pub(super) async fn on_order_dispatch_durable(
        &mut self,
        result: Option<Result<(), WalError>>,
    ) -> Result<(), EngineError> {
        result.ok_or(EngineError::TaskStopped {
            task: EngineTask::DispatchDurability,
            detail: "",
        })??;
        self.ledger.record(
            Segment::BarrierWait,
            clock::now_ns().saturating_sub(self.dispatches.barrier_started_ns),
        );
        let write = self
            .dispatches
            .write
            .take()
            .ok_or_else(|| EngineError::State("dispatch barrier result has no owner".into()))?;
        match write {
            DispatchWrite::Portfolio => self.service_portfolio_controls().await?,
            DispatchWrite::Stop(stops) => self.dispatch_durable_stops(stops)?,
            DispatchWrite::Amend(amend) => self.dispatch_durable_amend(*amend)?,
            DispatchWrite::Queue(ids) => {
                let mut authorized = Vec::new();
                for id in ids {
                    if self.refuse_changed_dispatch(&id).await? {
                        continue;
                    }
                    self.wal.append(&WalRecord::OrderDispatchAttempted {
                        client_order_id: id.clone(),
                    })?;
                    authorized.push(id);
                }
                if !authorized.is_empty() {
                    let barrier = self.wal.barrier_begin()?;
                    self.dispatches
                        .begin(DispatchWrite::Attempt(authorized), barrier);
                }
            }
            DispatchWrite::Attempt(ids) => {
                let mut requests = Vec::new();
                let mut timings = Vec::new();
                for id in ids {
                    if self.refuse_changed_dispatch(&id).await? {
                        continue;
                    }
                    let order = self
                        .dispatches
                        .orders
                        .get_mut(&id)
                        .expect("authorized dispatch");
                    order.phase = OrderDispatchPhase::Attempted;
                    self.risk.mark_order_attempted(&id);
                    self.ledger.record(
                        Segment::Durable,
                        clock::now_ns().saturating_sub(order.intent.decided_ns),
                    );
                    requests.push(order.request.clone());
                    timings.push((order.intent.decided_ns, order.origin_ns));
                }
                if !requests.is_empty() {
                    let queued_ns = clock::now_ns();
                    let command_id = self.venue.dispatch_orders(requests.clone())?;
                    self.mark_symbols_busy(requests.iter().map(|request| request.symbol));
                    self.pending_mutations.insert(
                        command_id,
                        PendingMutation::Orders {
                            requests,
                            timings,
                            queued_ns,
                        },
                    );
                }
            }
        }
        if self.dispatches.write.is_none() {
            self.ready_actions.append(&mut self.dispatches.waiting);
        }
        Ok(())
    }

    async fn refuse_changed_dispatch(&mut self, id: &str) -> Result<bool, EngineError> {
        let Some(order) = self.dispatches.orders.get(id).cloned() else {
            return Ok(true);
        };
        if self
            .books
            .orders
            .orders
            .get(id)
            .is_some_and(|order| !order.in_flight())
        {
            self.complete_order_dispatch(id)?;
            return Ok(true);
        }
        let mut refusal = if order.request.is_portfolio_reduction() {
            self.emergency_order_refusal(&order.request, Some(id))
        } else if !order.intent.reduce_only {
            if self.dispatches.recovered.contains(id) {
                Some("opening decision belongs to the previous engine epoch".into())
            } else {
                self.opening_refusal(order.intent.strategy)
                    .map(|reason| reason.to_string())
            }
        } else {
            let owned = self
                .books
                .attribution
                .signed(order.intent.strategy, order.intent.symbol);
            let reduces = match order.intent.side {
                Side::Sell => owned > 0.0,
                Side::Buy => owned < 0.0,
            };
            (!reduces || order.request.qty > owned.abs() + 1e-12)
                .then(|| "allocated position changed before dispatch".into())
        };
        if refusal.is_none()
            && order.request.exact_terms.is_some()
            && !order.request.is_portfolio_reduction()
        {
            let mut intent = order.intent.clone();
            intent.qty = order.request.qty;
            refusal = match self.risk.reassess_portfolio_order(
                id,
                &intent,
                &self.books.account,
                &self.books.attribution.snapshot(),
            ) {
                engine_types::risk::PortfolioRiskVerdict::Allow { qty, .. }
                    if qty == order.request.qty =>
                {
                    None
                }
                other => Some(format!("portfolio risk changed before dispatch: {other:?}")),
            };
            if refusal.is_none() {
                refusal = match self.instrument_specs.get(&intent.symbol).cloned() {
                    Some(spec) => match self.physical_order_plan(&order.request, &spec, Some(id)) {
                        Ok(plan)
                            if plan.reduce_only == order.request.reduce_only
                                && plan.native_stop.as_ref().map(|stop| &stop.trigger_price)
                                    == order.request.exact_terms.as_ref().and_then(|terms| {
                                        terms.physical_stop_trigger_price.as_ref()
                                    }) =>
                        {
                            None
                        }
                        Ok(_) => Some(
                            "physical direction or aggregate protection changed before dispatch"
                                .into(),
                        ),
                        Err(reason) => Some(reason),
                    },
                    None => Some("exact instrument metadata disappeared before dispatch".into()),
                };
            }
        }
        if let Some(reason) = refusal {
            self.take_update(OrderUpdate::Reject {
                client_order_id: id.into(),
                code: 0,
                reason: format!("never sent: {reason}"),
            })
            .await?;
            self.complete_order_dispatch(id)?;
            return Ok(true);
        }
        Ok(false)
    }

    pub(super) fn complete_order_dispatch(&mut self, id: &str) -> Result<(), EngineError> {
        if self.dispatches.orders.remove(id).is_some() {
            self.wal.append(&WalRecord::OrderDispatchCompleted {
                client_order_id: id.into(),
            })?;
        }
        self.dispatches.unresolved.remove(id);
        self.dispatches.recovered.remove(id);
        self.dispatches.lookup_after.remove(id);
        Ok(())
    }

    pub(super) fn observe_order_dispatch(
        &mut self,
        update: &OrderUpdate,
    ) -> Result<(), EngineError> {
        let id = match update {
            OrderUpdate::Ack(ack) => &ack.client_order_id,
            OrderUpdate::Reject {
                client_order_id, ..
            }
            | OrderUpdate::Fill {
                client_order_id, ..
            }
            | OrderUpdate::Cancelled {
                client_order_id, ..
            } => client_order_id,
            _ => return Ok(()),
        };
        self.complete_order_dispatch(id)
    }

    pub(super) async fn restore_order_dispatches(&mut self) -> Result<(), EngineError> {
        let pending: Vec<_> = self.dispatches.orders.keys().cloned().collect();
        for id in pending {
            let order = self
                .dispatches
                .orders
                .get(&id)
                .cloned()
                .expect("restored dispatch");
            if self
                .books
                .orders
                .orders
                .get(&id)
                .is_some_and(|known| !known.in_flight())
            {
                self.complete_order_dispatch(&id)?;
                continue;
            }
            match order.phase {
                OrderDispatchPhase::Queued => {
                    // An opening queued in a previous clock epoch must obtain
                    // a fresh strategy decision; reductions retain their ID.
                    if !order.intent.reduce_only {
                        self.take_update(OrderUpdate::Reject {
                            client_order_id: id.clone(),
                            code: 0,
                            reason:
                                "never sent: opening decision belongs to the previous engine epoch"
                                    .into(),
                        })
                        .await?;
                        self.complete_order_dispatch(&id)?;
                        continue;
                    }
                    if self.refuse_changed_dispatch(&id).await? {
                        continue;
                    }
                    self.wal.append(&WalRecord::OrderDispatchAttempted {
                        client_order_id: id.clone(),
                    })?;
                    let barrier = self.wal.barrier_begin()?;
                    self.dispatches
                        .begin(DispatchWrite::Attempt(vec![id]), barrier);
                    let result = self.dispatches.durable.recv().await;
                    self.on_order_dispatch_durable(result).await?;
                }
                OrderDispatchPhase::Attempted => {
                    let status = tokio::time::timeout(
                        MUTATION_DRAIN_TIMEOUT,
                        self.venue.order_status_named(
                            self.books.market.table.name(order.request.symbol),
                            &id,
                        ),
                    )
                    .await;
                    match status {
                        Ok(Ok(lookup)) => self.apply_order_lookup(&id, lookup).await?,
                        Ok(Err(error)) => {
                            self.dispatches.unresolved.insert(id, error.to_string());
                        }
                        Err(_) => {
                            self.dispatches
                                .unresolved
                                .insert(id, "order lookup timed out".into());
                        }
                    }
                }
            }
        }
        Ok(())
    }

    pub(super) async fn apply_order_lookup(
        &mut self,
        id: &str,
        lookup: OrderLookup,
    ) -> Result<(), EngineError> {
        let Some(order) = self.dispatches.orders.get(id).cloned() else {
            return Ok(());
        };
        let identity = |row: &engine_types::orders::OrderLookupRow| {
            row.client_order_id == id
                && row.symbol == self.books.market.table.name(order.request.symbol)
        };
        match lookup {
            OrderLookup::Working(row) if identity(&row) => {
                self.take_update(OrderUpdate::Ack(engine_types::OrderAck {
                    client_order_id: id.into(),
                    venue_order_id: row.venue_order_id,
                    sent_ns: 0,
                    ack_ns: clock::now_ns(),
                }))
                .await?;
                self.complete_order_dispatch(id)?;
            }
            OrderLookup::Terminal { status, row } if identity(&row) => {
                let known = self
                    .books
                    .orders
                    .orders
                    .get(id)
                    .map_or(0.0, |known| known.filled_qty);
                if row
                    .filled_qty
                    .value
                    .to_f64()
                    .map_err(|error| EngineError::State(error.to_string()))?
                    > known + 1e-12
                {
                    self.dispatches.unresolved.insert(
                        id.into(),
                        "terminal order contains fills outside recovered execution history".into(),
                    );
                    return Ok(());
                }
                let update = match status {
                    TerminalOrderStatus::Rejected => OrderUpdate::Reject {
                        client_order_id: id.into(),
                        code: 0,
                        reason: "venue terminal order lookup: rejected".into(),
                    },
                    TerminalOrderStatus::Filled | TerminalOrderStatus::Cancelled => {
                        OrderUpdate::Cancelled {
                            client_order_id: id.into(),
                            recv_ns: clock::now_ns(),
                        }
                    }
                };
                self.take_update(update).await?;
                self.complete_order_dispatch(id)?;
            }
            OrderLookup::NeverAccepted => {
                let mut queued = order;
                queued.phase = OrderDispatchPhase::Queued;
                self.wal.append(&WalRecord::OrderDispatchQueued {
                    order: queued.clone(),
                })?;
                self.dispatches.orders.insert(id.into(), queued);
                self.dispatches.unresolved.remove(id);
                // A later recovery pass dispatches only a still-valid reduction;
                // opening decisions require a fresh callback after restart.
            }
            OrderLookup::Unknown { reason } => {
                self.dispatches.unresolved.insert(id.into(), reason);
            }
            OrderLookup::Unavailable => {
                self.dispatches.unresolved.insert(
                    id.into(),
                    "venue cannot authoritatively look up this client order ID".into(),
                );
            }
            _ => {
                self.dispatches.unresolved.insert(
                    id.into(),
                    "venue order lookup returned another order identity".into(),
                );
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::order_dispatch::OrderDispatchState;
    use engine_types::orders::OrderLookupRow;

    type TestEngine =
        Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;

    async fn fixture() -> (TestEngine, std::sync::Arc<std::sync::Mutex<Vec<WalRecord>>>) {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, records) = crate::tests::callback_test_fixture(vec![strategy]).await;
        engine
            .books
            .attribution
            .note(StrategyId(0), SymbolId(0), Side::Buy, 1.0);
        (engine, records)
    }

    fn prepared_order(engine: &mut TestEngine, id: &str) -> PreparedOrder {
        let intent = Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.5,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "dispatch-regression".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        let request = OrderRequest {
            client_order_id: id.into(),
            strategy: intent.strategy,
            symbol: intent.symbol,
            side: intent.side,
            qty: intent.qty,
            kind: intent.kind,
            stop: None,
            reduce_only: true,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        };
        let record = WalRecord::OrderSent {
            dispatch: Some(Box::new(
                engine_types::order_dispatch::QueuedOrderDispatch {
                    intent: intent.clone(),
                    origin_ns: intent.decided_ns,
                },
            )),
            request: request.clone(),
            wire_ns: clock::now_ns(),
            arrival_mid: 100.0,
        };
        engine.wal.append(&record).unwrap();
        engine.books.registry.own(id, StrategyId(0));
        engine.books.orders.apply(&record);
        engine.dispatches.orders.insert(
            id.into(),
            OrderDispatchState {
                request: request.clone(),
                intent: intent.clone(),
                phase: OrderDispatchPhase::Queued,
                origin_ns: intent.decided_ns,
            },
        );
        PreparedOrder {
            decided_ns: intent.decided_ns,
            origin_ns: intent.decided_ns,
            intent,
            request,
        }
    }

    #[cfg(target_os = "linux")]
    #[tokio::test(start_paused = true)]
    async fn an_allocating_callback_cannot_hold_private_updates_or_a_committed_reduction() {
        let (mut engine, records) = fixture().await;
        let prepared = prepared_order(&mut engine, "independent-exit");
        let mut command = std::process::Command::new("python3");
        command.arg("-c").arg(include_str!(
            "../../tests/fixtures/strategy-allocation-flood.py"
        ));
        let process = crate::strategy_process::StrategyProcess::spawn_command(command).unwrap();
        let request = engine_types::strategy_process::CallbackRequest {
            schema_version: engine_types::strategy_process::STRATEGY_PROCESS_SCHEMA,
            callback_id: 1,
            state: engine.host.strategies[0].runtime_state().unwrap().unwrap(),
            event: engine_types::strategy_process::CallbackEvent::Boot,
            snapshot: engine
                .host
                .snapshot(&engine.books, StrategyId(0), clock::now_ns())
                .unwrap(),
        };
        let callback = process.call(request, Duration::from_secs(10));
        let independent = async {
            engine
                .take_update(OrderUpdate::Ack(engine_types::OrderAck {
                    client_order_id: "independent-private".into(),
                    venue_order_id: "private".into(),
                    sent_ns: 1,
                    ack_ns: 2,
                }))
                .await
                .unwrap();
            engine.queue_order_dispatches(vec![prepared]).unwrap();
            for _ in 0..2 {
                let result = engine.dispatches.durable.recv().await;
                engine.on_order_dispatch_durable(result).await.unwrap();
            }
            let completion =
                tokio::time::timeout(Duration::from_secs(1), engine.venue_completions.recv())
                    .await
                    .unwrap()
                    .unwrap();
            engine.take_venue_completion(completion).await.unwrap();
            assert!(engine.dispatches.orders.is_empty());
            assert!(records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::OrderUpdate { update: OrderUpdate::Ack(ack), .. } if ack.client_order_id == "independent-exit")));
        };
        let (callback, ()) = tokio::join!(callback, independent);
        assert!(callback.is_err(), "the callback escaped its memory owner");
        assert!(records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::OrderUpdate { update: OrderUpdate::Ack(ack), .. } if ack.client_order_id == "independent-private")));
    }

    #[tokio::test(start_paused = true)]
    async fn an_order_sent_record_also_owns_its_unsent_dispatch_at_the_crash_cut() {
        let (mut engine, records) = fixture().await;
        let intent = Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.5,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "atomic-cut".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        engine
            .process_intents(
                vec![(intent, Some("atomic-unsent".into()))],
                clock::now_ns(),
            )
            .await
            .unwrap();
        let records = records.lock().unwrap().clone();
        let cut = records.iter().position(|record| matches!(record, WalRecord::OrderSent { request, .. } if request.client_order_id == "atomic-unsent")).expect("the accepted reduction was journaled");
        let restored = crate::order_dispatch::OrderDispatches::replay(&records[..=cut]).unwrap();
        assert!(
            restored.orders.contains_key("atomic-unsent"),
            "a crash after OrderSent discarded the unsent reducing effect"
        );
        assert_eq!(
            restored.orders["atomic-unsent"].phase,
            OrderDispatchPhase::Queued
        );
        engine.dispatches = restored;
        engine.restore_order_dispatches().await.unwrap();
        assert_eq!(
            engine.pending_mutations.len(),
            1,
            "the queued reducing request resumes exactly once"
        );
        let completion = engine.venue_completions.recv().await.unwrap();
        engine.take_venue_completion(completion).await.unwrap();
        assert!(engine.dispatches.orders.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn a_failed_order_dispatch_barrier_never_reaches_the_venue() {
        let (mut engine, _) = fixture().await;
        let prepared = prepared_order(&mut engine, "queued-fail");
        engine.wal.fail_barrier_after = Some("order_sent");
        assert!(engine.queue_order_dispatches(vec![prepared]).is_err());
        assert!(engine.pending_mutations.is_empty());
        assert_eq!(
            engine.dispatches.orders["queued-fail"].phase,
            OrderDispatchPhase::Queued
        );
    }

    #[tokio::test(start_paused = true)]
    async fn slow_order_fsync_does_not_block_cancels_and_an_unsent_order_can_be_cancelled() {
        let (mut engine, records) = fixture().await;
        let first = prepared_order(&mut engine, "queued-slow");
        let _other = prepared_order(&mut engine, "already-working");
        engine.complete_order_dispatch("already-working").unwrap();
        engine
            .wal
            .delay_callback_barriers(Duration::from_millis(250));
        let began = std::time::Instant::now();
        engine.queue_order_dispatches(vec![first]).unwrap();
        assert!(began.elapsed() < Duration::from_millis(100));
        assert!(engine.pending_mutations.is_empty());
        engine
            .process_cancels(vec![(SymbolId(0), "already-working".into())])
            .await
            .unwrap();
        let completion =
            tokio::time::timeout(Duration::from_millis(100), engine.venue_completions.recv())
                .await
                .expect("a cancel must not wait for an unrelated fsync")
                .unwrap();
        engine.take_venue_completion(completion).await.unwrap();
        engine
            .process_cancels(vec![(SymbolId(0), "queued-slow".into())])
            .await
            .unwrap();
        assert!(!engine.books.orders.orders["queued-slow"].in_flight());
        let result = engine.dispatches.durable.recv().await;
        engine.on_order_dispatch_durable(result).await.unwrap();
        assert!(engine.dispatches.orders.is_empty());
        assert!(engine.pending_mutations.is_empty());
        assert!(!records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::OrderDispatchAttempted { client_order_id } if client_order_id == "queued-slow")));
    }

    #[tokio::test(start_paused = true)]
    async fn a_lost_send_reply_blocks_growth_before_the_first_lookup_returns() {
        let (mut engine, _) = fixture().await;
        let prepared = prepared_order(&mut engine, "lost-send-reply");
        engine.queue_order_dispatches(vec![prepared]).unwrap();
        for _ in 0..2 {
            let result = engine.dispatches.durable.recv().await;
            engine.on_order_dispatch_durable(result).await.unwrap();
        }
        let mut completion = engine.venue_completions.recv().await.unwrap();
        if let MutationCompletion::Orders { replies, .. } = &mut completion {
            *replies = vec![Err(VenueError::Transport(
                "response lost after acceptance".into(),
            ))];
        } else {
            panic!("expected order dispatch");
        }
        engine.take_venue_completion(completion).await.unwrap();
        assert!(engine.dispatches.lookup_pending.is_empty());
        assert!(
            engine.dispatches.unresolved.contains_key("lost-send-reply"),
            "growth remained enabled until the asynchronous lookup finished"
        );
        assert!(engine.opening_refusal(StrategyId(0)).is_some());
        assert!(engine.books.orders.orders["lost-send-reply"].in_flight());
        engine
            .apply_order_lookup(
                "lost-send-reply",
                OrderLookup::Working(OrderLookupRow {
                    symbol: "BTCUSDT".into(),
                    client_order_id: "lost-send-reply".into(),
                    venue_order_id: "confirmed".into(),
                    filled_qty: engine_types::numeric::ExactNumber::venue_decimal("0").unwrap(),
                }),
            )
            .await
            .unwrap();
        assert!(engine.dispatches.unresolved.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn queued_restart_sends_once_but_attempted_unknown_restart_never_resends() {
        let (mut engine, records) = fixture().await;
        let prepared = prepared_order(&mut engine, "queued-restart");
        engine.queue_order_dispatches(vec![prepared]).unwrap();
        let queued_records = records.lock().unwrap().clone();
        engine.dispatches =
            crate::order_dispatch::OrderDispatches::replay(&queued_records).unwrap();
        engine.restore_order_dispatches().await.unwrap();
        assert_eq!(engine.pending_mutations.len(), 1);
        let attempted_records = records.lock().unwrap().clone();
        assert_eq!(
            crate::order_dispatch::OrderDispatches::replay(&attempted_records)
                .unwrap()
                .orders["queued-restart"]
                .phase,
            OrderDispatchPhase::Attempted
        );
        let completion = engine.venue_completions.recv().await.unwrap();
        engine.take_venue_completion(completion).await.unwrap();
        assert!(engine.dispatches.orders.is_empty());
        assert!(
            crate::order_dispatch::OrderDispatches::replay(&records.lock().unwrap())
                .unwrap()
                .orders
                .is_empty()
        );
        let (mut restart, _) = fixture().await;
        let _ = prepared_order(&mut restart, "queued-restart");
        restart.dispatches =
            crate::order_dispatch::OrderDispatches::replay(&attempted_records).unwrap();
        restart.restore_order_dispatches().await.unwrap();
        assert!(restart.pending_mutations.is_empty());
        assert!(restart.dispatches.unresolved.contains_key("queued-restart"));
        assert!(restart.opening_refusal(StrategyId(0)).is_some());
        restart
            .apply_order_lookup(
                "queued-restart",
                OrderLookup::Working(OrderLookupRow {
                    symbol: "BTCUSDT".into(),
                    client_order_id: "queued-restart".into(),
                    venue_order_id: "venue-known".into(),
                    filled_qty: engine_types::numeric::ExactNumber::venue_decimal("0").unwrap(),
                }),
            )
            .await
            .unwrap();
        assert!(restart.dispatches.orders.is_empty());
        assert!(restart.dispatches.unresolved.is_empty());
        assert!(restart.pending_mutations.is_empty());
    }
}

#[cfg(test)]
mod portfolio_tests {
    use std::collections::HashMap;

    use super::*;
    use engine_types::Quote;

    async fn fixture() -> Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue>
    {
        let mut engine = crate::tests::shared_sleeves::balanced_engine().await;
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                bid_qty: 10.0,
                ask_qty: 10.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        engine.risk.observe_price(SymbolId(0), 100.0);
        engine
    }

    async fn exit(
        engine: &mut Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue>,
    ) -> PreparedOrder {
        engine
            .prepare_intent(
                Intent {
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side: Side::Sell,
                    qty: 1.0,
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: true,
                    tag: "owned-exit".into(),
                    decided_ns: clock::now_ns(),
                    work: None,
                    leverage: None,
                },
                Some("eng-exit-recheck".into()),
                clock::now_ns(),
                &mut HashMap::new(),
            )
            .await
            .unwrap()
            .unwrap()
    }

    #[tokio::test(start_paused = true)]
    async fn queued_shared_exit_rechecks_surviving_stop_and_excludes_its_own_reservation() {
        let mut engine = fixture().await;
        let order = exit(&mut engine).await;
        assert!(
            !order.request.reduce_only,
            "closing the long sleeve exposes the short sleeve"
        );
        assert_eq!(order.request.stop.unwrap().trigger_px, 110.0);
        assert!(
            !engine
                .refuse_changed_dispatch(&order.request.client_order_id)
                .await
                .unwrap(),
            "the unchanged order must not double count itself"
        );
        engine
            .books
            .attribution
            .set_sleeve_stop_exact(
                StrategyId(1),
                SymbolId(0),
                Side::Sell,
                engine_types::numeric::Exact::parse_decimal("105").unwrap(),
            )
            .unwrap();
        assert!(
            engine
                .refuse_changed_dispatch(&order.request.client_order_id)
                .await
                .unwrap(),
            "durability wait retained a weaker physical stop than the surviving sleeve owns"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn queued_virtual_exit_cannot_create_physical_growth_after_private_gap() {
        let mut engine = fixture().await;
        let order = exit(&mut engine).await;
        engine.private_stream_ready = false;
        assert!(
            engine
                .refuse_changed_dispatch(&order.request.client_order_id)
                .await
                .unwrap(),
            "virtual reduction became unprotected physical growth during private recovery"
        );
    }
}
