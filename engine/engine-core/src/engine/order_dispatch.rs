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
        self.begin_order_preparation(ids)?;
        Ok(true)
    }

    fn missing_leverage(&self, id: &str) -> Option<(SymbolId, f64)> {
        let order = self.dispatches.orders.get(id)?;
        if order.intent.reduce_only {
            return None;
        }
        let want = order.intent.leverage?;
        (self.leverage_at.get(&order.request.symbol) != Some(&want))
            .then_some((order.request.symbol, want))
    }

    fn leverage_pending(&self) -> bool {
        self.pending_mutations
            .values()
            .any(|pending| matches!(pending, PendingMutation::Leverage { .. }))
    }

    fn begin_order_preparation(&mut self, mut ids: Vec<String>) -> Result<(), EngineError> {
        let administration_available = !self.leverage_pending();
        ids.retain(|id| {
            self.dispatches.orders.get(id).is_some_and(|order| {
                !self.busy_symbols.contains_key(&order.request.symbol)
                    && (administration_available || self.missing_leverage(id).is_none())
            })
        });
        if ids.is_empty() {
            return Ok(());
        }
        let ready: Vec<_> = ids
            .iter()
            .filter(|id| self.missing_leverage(id).is_none())
            .cloned()
            .collect();
        if ready.is_empty() {
            // OrderSent already contains the dependent decision and reservation.
            // An administrative write cannot overtake that ownership barrier.
            let barrier = self.begin_dispatch_barrier()?;
            self.dispatches.begin(DispatchWrite::Leverage(ids), barrier);
            Ok(())
        } else {
            // Do not change the same symbol to the next requested leverage
            // before dispatching the orders its last confirmation unlocked.
            self.begin_order_attempt(ready)
        }
    }

    fn begin_order_attempt(&mut self, ids: Vec<String>) -> Result<(), EngineError> {
        for id in &ids {
            self.wal.append(&WalRecord::OrderDispatchAttempted {
                client_order_id: id.clone(),
            })?;
        }
        let barrier = self.begin_dispatch_barrier()?;
        self.dispatches.begin(DispatchWrite::Attempt(ids), barrier);
        Ok(())
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
                .filter(|(id, order)| {
                    order.phase == OrderDispatchPhase::Queued
                        && !self.busy_symbols.contains_key(&order.request.symbol)
                        && (!self.leverage_pending() || self.missing_leverage(id).is_none())
                })
                .take(MAX_ORDERS_PER_BATCH)
                .map(|(id, _)| id.clone())
                .collect();
            if !queued.is_empty() {
                self.begin_order_preparation(queued)?;
            }
        }
        let now = clock::now_ns();
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
            self.start_order_lookup(id, symbol)?;
        }
        if self.dispatches.lookup_pending.is_empty() {
            let next = self
                .working
                .crossing_candidates()
                .find(|(id, symbol)| {
                    !self.busy_symbols.contains_key(symbol)
                        && self
                            .dispatches
                            .lookup_after
                            .get(*id)
                            .is_none_or(|deadline| *deadline <= now)
                })
                .map(|(id, symbol)| (id.to_owned(), symbol));
            if let Some((id, symbol)) = next {
                self.start_order_lookup(id, symbol)?;
            }
        }
        Ok(())
    }

    /// One status read of `id` at the venue, answered on `dispatches.lookups`.
    /// The lane holds one read at a time; callers check `lookup_pending` first.
    pub(super) fn start_order_lookup(
        &mut self,
        id: String,
        symbol: SymbolId,
    ) -> Result<(), VenueError> {
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
        Ok(())
    }

    /// A status read came back. An ambiguous send and a halt cancel can be
    /// waiting on the same order; the send is settled first, and the halt
    /// then sees whatever that left of the order.
    pub(super) async fn on_order_lookup(
        &mut self,
        id: String,
        result: Result<OrderLookup, VenueError>,
    ) -> Result<(), EngineError> {
        self.dispatches.lookup_pending.remove(&id);
        self.dispatches
            .lookup_after
            .insert(id.clone(), clock::now_ns().saturating_add(1_000_000_000));
        let halted = matches!(
            self.halt_cancels.get(&id),
            Some(HaltCancelState::Resolving { .. })
        );
        let result = result.map_err(|error| error.to_string());
        if self.dispatches.orders.contains_key(&id) {
            match &result {
                Ok(lookup) => self.apply_order_lookup(&id, lookup.clone()).await?,
                Err(error) => note_unresolved(
                    &mut self.dispatches,
                    &self.authority,
                    id.clone(),
                    error.clone(),
                ),
            }
        }
        if self.working.waiting_to_cross(&id) {
            self.apply_cross_lookup(&id, &result).await?;
        }
        if halted {
            self.apply_halt_lookup(&id, result).await?;
        }
        Ok(())
    }

    async fn apply_cross_lookup(
        &mut self,
        id: &str,
        result: &Result<OrderLookup, String>,
    ) -> Result<(), EngineError> {
        let Some(order) = self.books.orders.orders.get(id) else {
            return Ok(());
        };
        let name = self.books.market.table.name(order.request.symbol);
        match result {
            Ok(OrderLookup::Terminal { status, row })
                if row.client_order_id == id && row.symbol == name =>
            {
                let known = order.filled_exact().map_err(EngineError::State)?;
                if row.filled_qty.value != known {
                    self.recovery.history_requested = true;
                    note_unresolved(
                        &mut self.dispatches,
                        &self.authority,
                        id.into(),
                        "passive cancel fill total differs from recovered executions".into(),
                    );
                    return Ok(());
                }
                let in_flight = order.in_flight();
                let update = match status {
                    TerminalOrderStatus::Rejected => OrderUpdate::Reject {
                        client_order_id: id.into(),
                        code: 0,
                        reason: "passive cancel terminal lookup".into(),
                    },
                    _ => OrderUpdate::Cancelled {
                        client_order_id: id.into(),
                        recv_ns: clock::now_ns(),
                    },
                };
                if in_flight {
                    self.take_update(update).await?;
                }
                self.dispatches.unresolved.remove(id);
                self.working.confirm_cross(id);
            }
            Ok(OrderLookup::Working(row)) if row.client_order_id == id && row.symbol == name => {
                self.working.retry_cross_cancel(id);
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) async fn on_order_dispatch_durable(
        &mut self,
        result: Option<Result<(), WalError>>,
    ) -> Result<(), EngineError> {
        result.ok_or(EngineError::TaskStopped {
            task: EngineTask::DispatchDurability,
            detail: "",
        })??;
        let retirements = std::mem::take(&mut self.dispatches.strategy_runtime_retirements);
        if !retirements.is_empty() {
            // The reducer that decided a queued opening is gone.
            self.supersede_openings();
        }
        for owner in retirements {
            self.host.callbacks.state.forget_process(owner);
        }
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
            DispatchWrite::Leverage(ids) => self.dispatch_durable_leverage(ids).await?,
            DispatchWrite::Portfolio => self.service_portfolio_controls().await?,
            DispatchWrite::Stop(stops) => self.dispatch_durable_stops(stops)?,
            DispatchWrite::Amend(amend) => self.dispatch_durable_amend(*amend)?,
            DispatchWrite::Attempt(ids) => {
                let mut requests = Vec::new();
                let mut timings = Vec::new();
                for id in ids {
                    if self.refuse_changed_dispatch(&id).await? {
                        continue;
                    }
                    if self.missing_leverage(&id).is_some() {
                        self.refuse_unsent_dispatch(
                            &id,
                            "leverage confirmation changed during durability wait",
                        )
                        .await?;
                        continue;
                    }
                    let order = self
                        .dispatches
                        .orders
                        .get_mut(&id)
                        .expect("authorized dispatch");
                    order.phase = OrderDispatchPhase::Attempted;
                    self.risk.mark_order_attempted(&id);
                    if let Some(timing) = order.timing {
                        self.ledger.record(
                            Segment::Durable,
                            clock::now_ns().saturating_sub(timing.decided_ns),
                        );
                    }
                    requests.push(order.request.clone());
                    timings.push(order.timing);
                }
                if !requests.is_empty() {
                    let queued_ns = clock::now_ns();
                    // Only a group that adds exposure can be refused at the
                    // send boundary; an all-reducing group carries no
                    // authority, so nothing can stop it reaching the venue.
                    let authority = (crate::venue_runtime::send_class(&requests)
                        == crate::venue_runtime::DispatchClass::Opening)
                        .then(|| self.mint_authority());
                    let command_id = self.venue.dispatch_orders(requests.clone(), authority)?;
                    self.mark_symbols_busy(requests.iter().map(|request| request.symbol));
                    self.pending_mutations.insert(
                        command_id,
                        PendingMutation::Orders {
                            requests,
                            timings,
                            queued_ns,
                            authority,
                        },
                    );
                    // The venue actor shares this executor; start I/O before route maintenance.
                    tokio::task::yield_now().await;
                }
            }
        }
        if self.dispatches.write.is_none() {
            self.ready_actions.append(&mut self.dispatches.waiting);
        }
        Ok(())
    }

    async fn dispatch_durable_leverage(&mut self, ids: Vec<String>) -> Result<(), EngineError> {
        let mut selected = None;
        let mut orders = Vec::new();
        for id in ids {
            if self.refuse_changed_dispatch(&id).await? {
                continue;
            }
            if let Some(want) = self.missing_leverage(&id) {
                if selected.is_none() {
                    selected = Some(want);
                }
                if selected == Some(want) {
                    orders.push(id);
                }
            }
        }
        if let Some((symbol, want)) = selected {
            if self.leverage_pending() {
                return Err(EngineError::State(
                    "leverage administration has two owners".into(),
                ));
            }
            let queued_ns = clock::now_ns();
            let command_id = self.venue.dispatch_leverage(symbol, want)?;
            self.leverage_at.remove(&symbol);
            self.mark_symbols_busy([symbol]);
            self.pending_mutations.insert(
                command_id,
                PendingMutation::Leverage {
                    symbol,
                    want,
                    orders,
                    account: Box::new(self.books.account.clone()),
                    queued_ns,
                },
            );
        }
        Ok(())
    }

    pub(super) async fn complete_leverage(
        &mut self,
        symbol: SymbolId,
        want: f64,
        orders: Vec<String>,
        account: AccountView,
        reply: Result<(), VenueError>,
    ) -> Result<(), EngineError> {
        let held_needs_readback = account
            .positions
            .iter()
            .any(|position| position.symbol == symbol && position.leverage != Some(want));
        let refusal = match reply {
            Err(VenueError::BadRequest(detail)) => {
                Some(format!("leverage request was refused locally: {detail}"))
            }
            Err(error) => {
                // Either side of a multi-call administration may have changed.
                // This is not a negative acknowledgement of that mutation.
                self.books.account.observed_ns = 0;
                Some(format!(
                    "leverage {want} was not confirmed ({error}); account refresh required"
                ))
            }
            Ok(()) if account_reading_changed(&account, &self.books.account) => {
                Some("account state changed during leverage administration".into())
            }
            Ok(()) if held_needs_readback => {
                self.books.account.observed_ns = 0;
                Some("held-position leverage requires a fresh account readback".into())
            }
            Ok(()) => None,
        };
        let mut authorized = false;
        for id in orders {
            // Cancellation or a private terminal event may already have retired
            // this dependent order. Late administration must never recreate it.
            if !self
                .dispatches
                .orders
                .get(&id)
                .is_some_and(|order| order.phase == OrderDispatchPhase::Queued)
            {
                continue;
            }
            if let Some(reason) = &refusal {
                self.refuse_unsent_dispatch(&id, reason).await?;
            } else if !self.refuse_changed_dispatch(&id).await? {
                authorized = true;
            }
        }
        if authorized {
            self.leverage_at.insert(symbol, want);
        } else {
            self.leverage_at.remove(&symbol);
        }
        self.release_symbols([symbol]);
        Ok(())
    }

    async fn refuse_unsent_dispatch(&mut self, id: &str, reason: &str) -> Result<(), EngineError> {
        self.take_update(OrderUpdate::Reject {
            client_order_id: id.into(),
            code: 0,
            reason: format!("never sent: {reason}"),
        })
        .await?;
        self.complete_order_dispatch(id)
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
                .signed_exact(order.intent.strategy, order.intent.symbol);
            let reduces = match order.intent.side {
                Side::Sell => owned.is_positive(),
                Side::Buy => owned.is_negative(),
            };
            let quantity = order
                .request
                .exact_terms
                .as_ref()
                .map(|terms| Ok(terms.quantity.clone()))
                .unwrap_or_else(|| engine_types::numeric::Exact::from_legacy_f64(order.request.qty))
                .map_err(|error| EngineError::State(error.to_string()))?;
            (!reduces || quantity > owned.abs())
                .then(|| "allocated position changed before dispatch".into())
        };
        if refusal.is_none()
            && order.request.exact_terms.is_some()
            && !order.request.is_portfolio_reduction()
        {
            let mut intent = order.intent.clone();
            intent.qty = order.request.qty;
            intent.exact_prices = order.request.canonical_intent_prices();
            intent.exact_quantity = order
                .request
                .exact_terms
                .as_ref()
                .map(|terms| Box::new(terms.quantity.clone()));
            refusal = match self.risk.reassess_portfolio_order(
                id,
                &intent,
                &self.books.account,
                &self.books.attribution.snapshot(),
                clock::now_ns(),
            ) {
                engine_types::risk::PortfolioRiskVerdict::Allow { qty, .. }
                    if intent.quantity().is_ok_and(|requested| qty == requested) =>
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
                        Ok(Err(error)) => note_unresolved(
                            &mut self.dispatches,
                            &self.authority,
                            id,
                            error.to_string(),
                        ),
                        Err(_) => note_unresolved(
                            &mut self.dispatches,
                            &self.authority,
                            id,
                            "order lookup timed out".into(),
                        ),
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
                    .map(|known| known.filled_exact())
                    .transpose()
                    .map_err(EngineError::State)?
                    .unwrap_or_else(engine_types::numeric::Exact::zero);
                // Legacy orders retain their binary64 fill frontier. Exact
                // orders need equality: a lower total is conflicting evidence,
                // not permission to erase fills or retire a reservation.
                if row.filled_qty.value > known
                    || (order.request.exact_terms.is_some() && row.filled_qty.value != known)
                {
                    self.recovery.history_requested = true;
                    note_unresolved(
                        &mut self.dispatches,
                        &self.authority,
                        id.into(),
                        "terminal fill total disagrees with durable execution history".into(),
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
                // Only an attempt can be taken back. The log's reader accepts
                // a queued record only over an attempted one, so a second
                // "never accepted" for an order already back in the queue
                // must restate nothing — it would make the log unreadable.
                if order.phase == OrderDispatchPhase::Attempted {
                    let mut queued = order;
                    queued.phase = OrderDispatchPhase::Queued;
                    self.wal.append(&WalRecord::OrderDispatchQueued {
                        order: queued.state.clone(),
                    })?;
                    self.dispatches.orders.insert(id.into(), queued);
                }
                self.dispatches.unresolved.remove(id);
                // A later recovery pass dispatches only a still-valid reduction;
                // opening decisions require a fresh callback after restart.
            }
            OrderLookup::Unknown { reason } => {
                note_unresolved(&mut self.dispatches, &self.authority, id.into(), reason)
            }
            OrderLookup::Unavailable => note_unresolved(
                &mut self.dispatches,
                &self.authority,
                id.into(),
                "venue cannot authoritatively look up this client order ID".into(),
            ),
            _ => note_unresolved(
                &mut self.dispatches,
                &self.authority,
                id.into(),
                "venue order lookup returned another order identity".into(),
            ),
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

    pub(super) async fn fixture() -> (TestEngine, std::sync::Arc<std::sync::Mutex<Vec<WalRecord>>>)
    {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, records) = crate::tests::callback_test_fixture(vec![strategy]).await;
        engine
            .books
            .attribution
            .note(StrategyId(0), SymbolId(0), Side::Buy, 1.0);
        (engine, records)
    }

    pub(super) fn prepared_order(engine: &mut TestEngine, id: &str) -> PreparedOrder {
        prepared_order_with_terms(engine, id, false)
    }

    fn prepared_order_with_terms(engine: &mut TestEngine, id: &str, exact: bool) -> PreparedOrder {
        let intent = Intent {
            exact_prices: None,
            exact_quantity: None,
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
        let mut request = OrderRequest {
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
        if exact {
            engine_types::order_terms::ExactOrderTerms {
                quantity: engine_types::numeric::Exact::parse_decimal("0.5").unwrap(),
                limit_price: None,
                stop_trigger_price: None,
                physical_stop_trigger_price: None,
                input_policy: engine_types::order_terms::OrderInputPolicy::StrategyShortestDecimal,
            }
            .apply_projection(&mut request)
            .unwrap();
        }
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
            crate::order_dispatch::RuntimeDispatch {
                state: OrderDispatchState {
                    request: request.clone(),
                    intent: intent.clone(),
                    phase: OrderDispatchPhase::Queued,
                    origin_ns: intent.decided_ns,
                },
                timing: Some(crate::ctx::CallbackTiming {
                    origin_ns: Some(intent.decided_ns),
                    decided_ns: intent.decided_ns,
                }),
            },
        );
        PreparedOrder {
            decided_ns: intent.decided_ns,
            origin_ns: intent.decided_ns,
            intent,
            request,
        }
    }

    async fn recover_lookup_fill(
        engine: &mut TestEngine,
        id: &str,
        exec_id: &str,
        quantity: engine_types::numeric::Exact,
    ) {
        use engine_types::numeric::{AssetAmount, AssetId, ExactNumber, ExecutionAmounts};
        let execution = engine_types::VenueExecution {
            client_order_id: id.into(),
            exec_id: exec_id.into(),
            symbol: "BTCUSDT".into(),
            side: Side::Sell,
            qty: quantity.to_f64().unwrap(),
            px: 100.0,
            fee: Some(0.0),
            amounts: Some(ExecutionAmounts {
                settlement_asset: AssetId::Named("USDT".into()),
                quantity: ExactNumber::derived(quantity),
                price: ExactNumber::venue_decimal("100").unwrap(),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0").unwrap(),
                }),
            }),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: clock::wall_ms(),
        };
        engine.recovery.history_requested = true;
        engine
            .apply_history_batch(super::super::account_recovery::HistoryBatch {
                query: super::super::account_recovery::Query {
                    started_ns: clock::now_ns(),
                    generation: engine.recovery.generation,
                    history: Some((clock::wall_ms() - 1_000, clock::wall_ms())),
                },
                account: Err(VenueError::Transport(
                    "account refresh remains pending".into(),
                )),
                rows: engine_types::ExecutionHistory::from_rows([execution]).unwrap(),
                resume: None,
                untrusted: false,
                delivered: Default::default(),
                recovered: 0,
                foreign: Vec::new(),
            })
            .unwrap();
        assert!(engine.recovery.uncommitted());
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();
        assert!(!engine.recovery.uncommitted());
    }

    #[tokio::test(start_paused = true)]
    async fn passive_cross_waits_for_sub_projection_fills_from_history() {
        use engine_types::numeric::{Exact, ExactNumber};
        use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
        use engine_types::orders::{OrderLookupRow, TimeInForce};
        let (mut engine, _) = fixture().await;
        let id = "passive-late-fill";
        let mut request = OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 1.0,
            kind: OrderKind::Limit {
                px: 101.0,
                tif: TimeInForce::PostOnly,
            },
            stop: Some(engine_types::StopSpec { trigger_px: 110.0 }),
            reduce_only: false,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        };
        ExactOrderTerms {
            quantity: Exact::one(),
            limit_price: Some(Exact::from_u64(101)),
            stop_trigger_price: Some(Exact::from_u64(110)),
            physical_stop_trigger_price: Some(Exact::from_u64(110)),
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(&mut request)
        .unwrap();
        let sent = WalRecord::OrderSent {
            dispatch: None,
            request,
            arrival_mid: 100.0,
            wire_ns: clock::now_ns(),
        };
        engine.books.orders.apply(&sent);
        engine.books.registry.own(id, StrategyId(0));
        engine.wal.append(&sent).unwrap();
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: 99.0,
                ask_px: 101.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        let policy = engine_types::WorkPolicy {
            window_ms: 1,
            ..Default::default()
        };
        let now = clock::now_ns();
        engine.working.take_on(
            id,
            SymbolId(0),
            policy,
            crate::working::plan::WorkState::new(Side::Sell, 101.0, 100.0, now),
        );
        let pass = |engine: &mut TestEngine| {
            let mut actions = std::collections::VecDeque::new();
            engine.working.pass(
                now + 2_000_000,
                &engine.books.market,
                &engine.books.rules,
                &engine.books.orders,
                &mut actions,
            );
            actions
        };
        assert!(matches!(
            pass(&mut engine).front(),
            Some(engine_types::Action::Cancel { .. })
        ));
        recover_lookup_fill(
            &mut engine,
            id,
            "known-passive",
            Exact::parse_decimal("0.25").unwrap(),
        )
        .await;
        let lookup = || OrderLookup::Terminal {
            status: TerminalOrderStatus::Cancelled,
            row: OrderLookupRow {
                symbol: "BTCUSDT".into(),
                client_order_id: id.into(),
                venue_order_id: "venue-passive".into(),
                filled_qty: ExactNumber::venue_decimal("0.250000000000000001").unwrap(),
            },
        };
        engine
            .on_order_lookup(id.into(), Ok(lookup()))
            .await
            .unwrap();
        assert!(engine.dispatches.unresolved.contains_key(id));
        assert!(engine.recovery.history_requested);
        assert!(!pass(&mut engine)
            .iter()
            .any(|a| matches!(a, engine_types::Action::Place(_))));
        recover_lookup_fill(
            &mut engine,
            id,
            "late-passive",
            Exact::parse_decimal("0.000000000000000001").unwrap(),
        )
        .await;
        engine
            .on_order_lookup(id.into(), Ok(lookup()))
            .await
            .unwrap();
        assert!(!engine.dispatches.unresolved.contains_key(id));
        let mut actions = pass(&mut engine);
        let [engine_types::Action::Place(intent)] = actions.make_contiguous() else {
            panic!("{actions:?}");
        };
        assert_eq!(
            intent.quantity().unwrap(),
            Exact::parse_decimal("0.749999999999999999").unwrap()
        );
        assert_eq!(
            intent.kind,
            OrderKind::Limit {
                px: 99.0,
                tif: TimeInForce::Ioc
            }
        );
        assert!(pass(&mut engine).is_empty());
    }

    async fn exact_terminal_lookup_waits_for_history(halt: bool, known: &str, venue: &str) {
        use engine_types::numeric::{Exact, ExactNumber};
        let (mut engine, records) = fixture().await;
        let id = "terminal-exact-history";
        let _ = prepared_order_with_terms(&mut engine, id, true);
        let known = Exact::parse_decimal(known).unwrap();
        let venue = ExactNumber::venue_decimal(venue).unwrap();
        assert!(venue.value > known);
        assert!(venue.value.to_f64().unwrap() <= known.to_f64().unwrap() + 1e-12);
        if !known.is_zero() {
            recover_lookup_fill(&mut engine, id, "known-before-lookup", known.clone()).await;
        }
        engine.recovery.history_requested = false;
        if halt {
            engine.halt_cancels.insert(
                id.into(),
                HaltCancelState::Resolving {
                    deadline_ns: clock::now_ns() + 10_000_000_000,
                    retry_after_ns: clock::now_ns(),
                },
            );
        }
        let lookup = || OrderLookup::Terminal {
            status: TerminalOrderStatus::Cancelled,
            row: OrderLookupRow {
                symbol: "BTCUSDT".into(),
                client_order_id: id.into(),
                venue_order_id: "venue-terminal".into(),
                filled_qty: venue.clone(),
            },
        };
        for _ in 0..2 {
            if halt {
                engine.apply_halt_lookup(id, Ok(lookup())).await.unwrap();
                assert!(engine.recovery.history_requested);
                assert!(engine.halt_cancels.contains_key(id));
            } else {
                engine.apply_order_lookup(id, lookup()).await.unwrap();
                assert!(engine.recovery.history_requested);
                assert!(engine.dispatches.unresolved.contains_key(id));
                assert!(engine.dispatches.orders.contains_key(id));
            }
            assert!(engine.books.orders.orders[id].in_flight());
        }
        assert!(!records.lock().unwrap().iter().any(|record| matches!(
            record,
            WalRecord::OrderUpdate { update: OrderUpdate::Cancelled { client_order_id, .. }, .. }
                if client_order_id == id
        )));
        recover_lookup_fill(
            &mut engine,
            id,
            "recovered-after-lookup",
            &venue.value - &known,
        )
        .await;
        if halt {
            engine.apply_halt_lookup(id, Ok(lookup())).await.unwrap();
            assert!(!engine.halt_cancels.contains_key(id));
        } else {
            engine.apply_order_lookup(id, lookup()).await.unwrap();
            assert!(!engine.dispatches.unresolved.contains_key(id));
            assert!(!engine.dispatches.orders.contains_key(id));
        }
        assert!(!engine.books.orders.orders[id].in_flight());
        assert!(matches!(
            &engine.books.orders.orders[id].fill_quantity,
            engine_types::wal::OrderFillQuantity::Exact { quantity } if *quantity == venue.value
        ));
        let ledger =
            crate::inflight::LedgerOfOrders::try_from_records(&records.lock().unwrap()).unwrap();
        assert!(!ledger.orders[id].in_flight());
        assert_eq!(
            ledger.orders[id].fill_quantity,
            engine.books.orders.orders[id].fill_quantity
        );
    }

    #[tokio::test(start_paused = true)]
    async fn terminal_dispatch_lookup_waits_for_sub_epsilon_fill_history() {
        exact_terminal_lookup_waits_for_history(false, "0", "0.0000000000005").await;
    }

    #[tokio::test(start_paused = true)]
    async fn terminal_dispatch_lookup_preserves_distinct_exact_fill_totals() {
        exact_terminal_lookup_waits_for_history(false, "0.1", "0.100000000000000001").await;
    }

    #[tokio::test(start_paused = true)]
    async fn terminal_halt_lookup_waits_for_sub_epsilon_fill_history() {
        exact_terminal_lookup_waits_for_history(true, "0", "0.0000000000005").await;
    }

    #[tokio::test(start_paused = true)]
    async fn terminal_halt_lookup_preserves_distinct_exact_fill_totals() {
        exact_terminal_lookup_waits_for_history(true, "0.1", "0.100000000000000001").await;
    }

    #[tokio::test(start_paused = true)]
    async fn terminal_lookup_preserves_the_legacy_binary64_fill_frontier() {
        use engine_types::numeric::{Exact, ExactNumber};
        for halt in [false, true] {
            let (mut engine, _) = fixture().await;
            let id = "legacy-terminal";
            let _ = prepared_order(&mut engine, id);
            recover_lookup_fill(
                &mut engine,
                id,
                "legacy-known",
                Exact::parse_decimal("0.1").unwrap(),
            )
            .await;
            let binary = Exact::from_legacy_f64(0.1).unwrap();
            assert_eq!(
                engine.books.orders.orders[id].filled_exact().unwrap(),
                binary
            );
            let venue = ExactNumber::venue_decimal("0.100000000000000003").unwrap();
            assert!(venue.value > Exact::parse_decimal("0.1").unwrap());
            assert!(venue.value < binary);
            let lookup = OrderLookup::Terminal {
                status: TerminalOrderStatus::Cancelled,
                row: OrderLookupRow {
                    symbol: "BTCUSDT".into(),
                    client_order_id: id.into(),
                    venue_order_id: "legacy-venue".into(),
                    filled_qty: venue,
                },
            };
            if halt {
                engine.halt_cancels.insert(
                    id.into(),
                    HaltCancelState::Resolving {
                        deadline_ns: clock::now_ns() + 10_000_000_000,
                        retry_after_ns: clock::now_ns(),
                    },
                );
                engine.apply_halt_lookup(id, Ok(lookup)).await.unwrap();
            } else {
                engine.apply_order_lookup(id, lookup).await.unwrap();
            }
            assert!(!engine.books.orders.orders[id].in_flight());
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_lower_exact_terminal_fill_total_cannot_retire_the_order_or_erase_fills() {
        use engine_types::numeric::{Exact, ExactNumber};
        for halt in [false, true] {
            let (mut engine, records) = fixture().await;
            let id = "contradictory-terminal-fill";
            let _ = prepared_order_with_terms(&mut engine, id, true);
            let known = Exact::parse_decimal("0.25").unwrap();
            recover_lookup_fill(&mut engine, id, "durable-partial-fill", known.clone()).await;
            engine.recovery.history_requested = false;
            let deadline_ns = clock::now_ns() + 10_000_000_000;
            if halt {
                engine.halt_cancels.insert(
                    id.into(),
                    HaltCancelState::Resolving {
                        deadline_ns,
                        retry_after_ns: clock::now_ns(),
                    },
                );
            }
            let lookup = |quantity| OrderLookup::Terminal {
                status: TerminalOrderStatus::Cancelled,
                row: OrderLookupRow {
                    symbol: "BTCUSDT".into(),
                    client_order_id: id.into(),
                    venue_order_id: "same-venue-order".into(),
                    filled_qty: ExactNumber::venue_decimal(quantity).unwrap(),
                },
            };
            for _ in 0..2 {
                if halt {
                    engine
                        .apply_halt_lookup(id, Ok(lookup("0.2")))
                        .await
                        .unwrap();
                    assert!(matches!(engine.halt_cancels.get(id),
                        Some(HaltCancelState::Resolving { deadline_ns: kept, .. }) if *kept == deadline_ns));
                } else {
                    engine.apply_order_lookup(id, lookup("0.2")).await.unwrap();
                    assert!(engine.dispatches.unresolved.contains_key(id));
                    assert!(engine.dispatches.orders.contains_key(id));
                }
                assert!(engine.recovery.history_requested);
                assert!(engine.books.orders.orders[id].in_flight());
                assert_eq!(
                    engine.books.orders.orders[id].filled_exact().unwrap(),
                    known
                );
            }
            assert!(!records.lock().unwrap().iter().any(|record| matches!(record,
                WalRecord::OrderUpdate { update: OrderUpdate::Cancelled { client_order_id, .. }, .. }
                    if client_order_id == id)));
            if halt {
                engine
                    .apply_halt_lookup(id, Ok(lookup("0.25")))
                    .await
                    .unwrap();
                assert!(!engine.halt_cancels.contains_key(id));
            } else {
                engine.apply_order_lookup(id, lookup("0.25")).await.unwrap();
                assert!(!engine.dispatches.unresolved.contains_key(id));
            }
            let replay =
                crate::inflight::LedgerOfOrders::try_from_records(&records.lock().unwrap())
                    .unwrap();
            assert!(!replay.orders[id].in_flight());
            assert_eq!(replay.orders[id].filled_exact().unwrap(), known);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn an_order_sent_record_also_owns_its_unsent_dispatch_at_the_crash_cut() {
        let (mut engine, records) = fixture().await;
        let intent = Intent {
            exact_prices: None,
            exact_quantity: None,
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
                vec![(intent, Some("atomic-unsent".into()), None, None)],
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
        engine.wal.fail_barrier_after = Some("order_dispatch_attempted");
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
        assert!(records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::OrderDispatchAttempted { client_order_id } if client_order_id == "queued-slow")));
        assert!(
            crate::order_dispatch::OrderDispatches::replay(&records.lock().unwrap())
                .unwrap()
                .orders
                .is_empty()
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_lost_send_reply_blocks_growth_before_the_first_lookup_returns() {
        let (mut engine, _) = fixture().await;
        let prepared = prepared_order(&mut engine, "lost-send-reply");
        engine.queue_order_dispatches(vec![prepared]).unwrap();
        let result = engine.dispatches.durable.recv().await;
        engine.on_order_dispatch_durable(result).await.unwrap();
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
        let _prepared = prepared_order(&mut engine, "queued-restart");
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

    pub(super) async fn fixture(
    ) -> Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue> {
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
                    exact_prices: None,
                    exact_quantity: None,
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
                None,
                None,
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

#[cfg(test)]
mod replay_timing_tests;

#[cfg(test)]
mod leverage_tests;

#[cfg(test)]
mod authority_tests;
