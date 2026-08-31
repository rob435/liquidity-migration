impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    async fn take_venue_completion(
        &mut self,
        completion: MutationCompletion,
    ) -> Result<(), EngineError> {
        let command_id = match &completion {
            MutationCompletion::Orders { command_id, .. }
            | MutationCompletion::Cancels { command_id, .. }
            | MutationCompletion::Amend { command_id, .. } => *command_id,
        };
        let pending = self.pending_mutations.remove(&command_id).ok_or_else(|| {
            EngineError::State(format!(
                "venue task returned unknown mutation command {command_id}"
            ))
        })?;

        match (pending, completion) {
            (
                PendingMutation::Orders {
                    requests,
                    timings,
                    queued_ns,
                },
                MutationCompletion::Orders {
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                    replies,
                    ..
                },
            ) => {
                if let Some(held) = rate_wait_ns {
                    self.ledger.record(Segment::QuotaHold, held);
                }
                if replies.len() != requests.len() {
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "venue returned {} answers for {} submitted orders; missing answers remain in flight",
                            replies.len(),
                            requests.len()
                        ),
                    })?;
                }
                tracing::debug!(
                    command_id,
                    queue_ns = started_ns.saturating_sub(queued_ns),
                    venue_ns = completed_ns.saturating_sub(started_ns),
                    "placement command completed"
                );
                let symbols: Vec<_> = requests.iter().map(|request| request.symbol).collect();
                let mut replies = replies.into_iter();
                for (request, (decided_ns, origin_ns)) in requests.into_iter().zip(timings) {
                    let core_handled_ns = clock::now_ns();
                    self.ledger
                        .record(Segment::Wire, completed_ns.saturating_sub(decided_ns));
                    self.ledger
                        .record(Segment::DispatchQueue, started_ns.saturating_sub(queued_ns));
                    self.ledger
                        .record(Segment::VenueTask, completed_ns.saturating_sub(started_ns));
                    self.ledger.record(
                        Segment::CoreResume,
                        core_handled_ns.saturating_sub(completed_ns),
                    );
                    let reply = replies.next().unwrap_or_else(|| {
                        Err(VenueError::BadReply(
                            "the venue omitted this order from its batch reply".to_string(),
                        ))
                    });
                    let (socket_write_ns, ack_timing_ns) = match &reply {
                        Ok(ack) => (
                            (ack.sent_ns > 0).then_some(ack.sent_ns),
                            Some(if ack.ack_ns > started_ns {
                                ack.ack_ns
                            } else {
                                completed_ns
                            }),
                        ),
                        Err(_) => (None, None),
                    };
                    self.wal.append(&WalRecord::VenueTiming {
                        command_id,
                        operation: "place".to_string(),
                        client_order_id: request.client_order_id.clone(),
                        queued_ns,
                        task_started_ns: started_ns,
                        socket_write_ns,
                        ack_ns: ack_timing_ns,
                        rate_wait_ns,
                        task_completed_ns: completed_ns,
                        core_handled_ns,
                        core_handled_wall_ns: clock::wall_ns(),
                    })?;
                    let update = match reply {
                        Ok(ack) => {
                            let ack_ns = if ack.ack_ns > started_ns {
                                ack.ack_ns
                            } else {
                                completed_ns
                            };
                            if ack.sent_ns > 0 {
                                self.ledger
                                    .record(Segment::Ack, ack_ns.saturating_sub(ack.sent_ns));
                            }
                            Some(OrderUpdate::Ack(ack))
                        }
                        Err(VenueError::Rejected { code, message }) => Some(OrderUpdate::Reject {
                            client_order_id: request.client_order_id.clone(),
                            code,
                            reason: message,
                        }),
                        Err(VenueError::BadRequest(detail)) => Some(OrderUpdate::Reject {
                            client_order_id: request.client_order_id.clone(),
                            code: 0,
                            reason: format!("never sent: {detail}"),
                        }),
                        Err(other) => {
                            tracing::error!(id = %request.client_order_id, error = %other, "send failed with no answer");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!(
                                    "{} sent with no answer ({other}); still counted as in flight",
                                    request.client_order_id
                                ),
                            })?;
                            None
                        }
                    };
                    self.ledger
                        .record(Segment::EndToEnd, clock::now_ns().saturating_sub(origin_ns));
                    if let Some(update) = update {
                        self.take_update(update).await?;
                    }
                }
                self.release_symbols(symbols);
            }
            (
                PendingMutation::Cancels {
                    requests,
                    queued_ns,
                },
                MutationCompletion::Cancels {
                    started_ns,
                    completed_ns,
                    timing,
                    rate_wait_ns,
                    replies,
                    ..
                },
            ) => {
                if let Some(held) = rate_wait_ns {
                    self.ledger.record(Segment::QuotaHold, held);
                }
                if replies.len() != requests.len() {
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!(
                            "venue returned {} answers for {} submitted cancels; missing answers remain in flight",
                            replies.len(),
                            requests.len()
                        ),
                    })?;
                }
                tracing::debug!(
                    command_id,
                    venue_ns = completed_ns.saturating_sub(started_ns),
                    "cancel command completed"
                );
                let symbols: Vec<_> = requests.iter().map(|(symbol, _)| *symbol).collect();
                if let Some(mark) = timing {
                    self.ledger
                        .record(Segment::Ack, mark.ack_ns.saturating_sub(mark.sent_ns));
                }
                let mut replies = replies.into_iter();
                let mut halt_failure = None;
                let accepted_deadline = clock::now_ns().saturating_add(HALT_CANCEL_CONFIRM_NS);
                for (_, client_order_id) in requests {
                    let core_handled_ns = clock::now_ns();
                    self.ledger
                        .record(Segment::DispatchQueue, started_ns.saturating_sub(queued_ns));
                    self.ledger
                        .record(Segment::VenueTask, completed_ns.saturating_sub(started_ns));
                    self.ledger.record(
                        Segment::CoreResume,
                        core_handled_ns.saturating_sub(completed_ns),
                    );
                    self.wal.append(&WalRecord::VenueTiming {
                        command_id,
                        operation: "cancel".to_string(),
                        client_order_id: client_order_id.clone(),
                        queued_ns,
                        task_started_ns: started_ns,
                        socket_write_ns: timing.map(|mark| mark.sent_ns),
                        ack_ns: timing.map(|mark| mark.ack_ns),
                        rate_wait_ns,
                        task_completed_ns: completed_ns,
                        core_handled_ns,
                        core_handled_wall_ns: clock::wall_ns(),
                    })?;
                    let reply = replies.next().unwrap_or_else(|| {
                        Err(VenueError::BadReply(
                            "the venue omitted this order from its cancel-batch reply".to_string(),
                        ))
                    });
                    let taken = match reply {
                        Ok(()) => true,
                        Err(VenueError::BadRequest(detail)) => {
                            tracing::error!(id = client_order_id, detail, "cancel never sent");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!("cancel of {client_order_id} never sent: {detail}"),
                            })?;
                            if self.halt_cancels.contains_key(&client_order_id) {
                                halt_failure = Some(format!("{client_order_id}: {detail}"));
                            }
                            false
                        }
                        Err(VenueError::Rejected { code, message }) => {
                            tracing::error!(id = client_order_id, code, message, "cancel rejected");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!(
                                    "cancel of {client_order_id} rejected ({code}: {message}); the order is still counted as working"
                                ),
                            })?;
                            if self.halt_cancels.contains_key(&client_order_id) {
                                halt_failure =
                                    Some(format!("{client_order_id}: {code}: {message}"));
                            }
                            false
                        }
                        Err(other) => {
                            tracing::error!(id = client_order_id, error = %other, "cancel failed with no answer");
                            self.wal.append(&WalRecord::Note {
                                source: "engine".into(),
                                text: format!(
                                    "cancel of {client_order_id} sent with no answer ({other}); the order is still counted as working"
                                ),
                            })?;
                            if self.halt_cancels.contains_key(&client_order_id) {
                                halt_failure = Some(format!("{client_order_id}: {other}"));
                            }
                            false
                        }
                    };
                    self.working.cancelled(&client_order_id, taken);
                    if taken {
                        if let Some(state) = self.halt_cancels.get_mut(&client_order_id) {
                            *state = HaltCancelState::AwaitingPrivate {
                                deadline_ns: accepted_deadline,
                            };
                        }
                    }
                }
                self.release_symbols(symbols);
                if let Some(detail) = halt_failure {
                    return Err(EngineError::State(format!(
                        "account-level halt left at least one opening cancel unconfirmed ({detail}); restarting for venue reconciliation"
                    )));
                }
            }
            (
                PendingMutation::Amend {
                    symbol,
                    client_order_id,
                    spec,
                    existing,
                    amended_intent,
                    remaining_qty,
                    old_px,
                    tif,
                    queued_ns,
                },
                MutationCompletion::Amend {
                    started_ns,
                    completed_ns,
                    timing,
                    rate_wait_ns,
                    reply,
                    ..
                },
            ) => {
                if let Some(held) = rate_wait_ns {
                    self.ledger.record(Segment::QuotaHold, held);
                }
                let core_handled_ns = clock::now_ns();
                self.ledger
                    .record(Segment::DispatchQueue, started_ns.saturating_sub(queued_ns));
                self.ledger
                    .record(Segment::VenueTask, completed_ns.saturating_sub(started_ns));
                self.ledger.record(
                    Segment::CoreResume,
                    core_handled_ns.saturating_sub(completed_ns),
                );
                if let Some(mark) = timing {
                    self.ledger
                        .record(Segment::Ack, mark.ack_ns.saturating_sub(mark.sent_ns));
                }
                self.wal.append(&WalRecord::VenueTiming {
                    command_id,
                    operation: "amend".to_string(),
                    client_order_id: client_order_id.clone(),
                    queued_ns,
                    task_started_ns: started_ns,
                    socket_write_ns: timing.map(|mark| mark.sent_ns),
                    ack_ns: timing.map(|mark| mark.ack_ns),
                    rate_wait_ns,
                    task_completed_ns: completed_ns,
                    core_handled_ns,
                    core_handled_wall_ns: clock::wall_ns(),
                })?;
                tracing::debug!(
                    command_id,
                    venue_ns = completed_ns.saturating_sub(started_ns),
                    "amend command completed"
                );
                match reply {
                    Ok(()) => {
                        // The venue took it and did not say at what price.
                        // It states that by republishing the order on the
                        // private stream, so the ambiguity stays open for
                        // that answer rather than being closed by pulling
                        // the order — which is the whole point of amending
                        // in place instead of replacing.
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!(
                                "amend of {client_order_id} was accepted; waiting for the private stream to say what price it is working at"
                            ),
                        })?;
                        self.amends_awaiting_price.insert(
                            client_order_id.clone(),
                            AwaitingAmend {
                                symbol,
                                existing,
                                amended_intent,
                                remaining_qty,
                                tif,
                                deadline_ns: clock::now_ns().saturating_add(AMEND_CONFIRM_NS),
                            },
                        );
                    }
                    Err(VenueError::BadRequest(detail)) => {
                        tracing::error!(id = client_order_id, detail, "amend never sent");
                        self.resolve_amend(
                            &client_order_id,
                            &existing,
                            &amended_intent,
                            remaining_qty,
                            old_px,
                            tif,
                        )?;
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!("amend of {client_order_id} never sent: {detail}"),
                        })?;
                    }
                    Err(VenueError::Rejected { code, message }) => {
                        self.resolve_amend(
                            &client_order_id,
                            &existing,
                            &amended_intent,
                            remaining_qty,
                            old_px,
                            tif,
                        )?;
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!(
                                "amend of {client_order_id} rejected by venue ({code}: {message})"
                            ),
                        })?;
                    }
                    Err(other) => {
                        tracing::error!(id = client_order_id, error = %other, "amend failed with no answer");
                        self.wal.append(&WalRecord::Note {
                            source: "engine".into(),
                            text: format!(
                                "amend of {client_order_id} sent with no answer ({other}); its price and size are unconfirmed"
                            ),
                        })?;
                        self.pending.push_front(Action::Cancel {
                            symbol,
                            client_order_id: client_order_id.clone(),
                        });
                    }
                }
                self.working
                    .amended(&client_order_id, spec.px, false, clock::now_ns());
                self.release_symbols([symbol]);
            }
            (pending, completion) => {
                let pending_kind = match pending {
                    PendingMutation::Orders { .. } => "orders",
                    PendingMutation::Cancels { .. } => "cancels",
                    PendingMutation::Amend { .. } => "amend",
                };
                let completion_kind = match completion {
                    MutationCompletion::Orders { .. } => "orders",
                    MutationCompletion::Cancels { .. } => "cancels",
                    MutationCompletion::Amend { .. } => "amend",
                };
                return Err(EngineError::State(format!(
                    "venue task returned {completion_kind} for pending {pending_kind} command {command_id}"
                )));
            }
        }
        Ok(())
    }

    /// Narrow an amend's conservative old/new reservation to the one price
    /// the order is actually working at.
    ///
    /// Called with the old price when the amend never took, and with the
    /// venue's own stated price when it did. Both are the same act: the
    /// range was held open because the price was unknown, and this is where
    /// it becomes known.
    /// Wait for the outstanding durability barrier, if there is one.
    ///
    /// Almost always free: the barrier was started when the order was sent,
    /// and the venue's round trip is longer than the disk's. What it costs
    /// when it is not free is recorded, because that is the number that says
    /// whether running the barrier beside the send is buying anything.
    fn settle_barrier(&mut self) -> Result<(), EngineError> {
        let Some(pending) = self.pending_barrier.take() else {
            return Ok(());
        };
        let began_ns = clock::now_ns();
        pending.wait()?;
        self.ledger.record(
            Segment::BarrierWait,
            clock::now_ns().saturating_sub(began_ns),
        );
        Ok(())
    }

    fn resolve_amend(
        &mut self,
        client_order_id: &str,
        existing: &crate::inflight::OrderRec,
        amended_intent: &Intent,
        remaining_qty: f64,
        effective_px: f64,
        tif: TimeInForce,
    ) -> Result<(), EngineError> {
        let resolved = WalRecord::AmendResolved {
            client_order_id: client_order_id.to_string(),
            effective_px,
        };
        self.wal.append(&resolved)?;
        self.orders.apply(&resolved);
        if !existing.request.reduce_only {
            let mut settled = amended_intent.clone();
            settled.kind = OrderKind::Limit {
                px: effective_px,
                tif,
            };
            self.risk
                .register_order(client_order_id, &settled, remaining_qty);
        }
        Ok(())
    }

    /// Pull any order whose accepted amend the private stream never
    /// explained. The fallback is exactly what an unamendable venue gets:
    /// take the order down, because an order resting at a price the engine
    /// cannot name is one it cannot price its own book against.
    fn pull_unconfirmed_amends(&mut self) -> Result<(), EngineError> {
        if self.amends_awaiting_price.is_empty() {
            return Ok(());
        }
        let now_ns = clock::now_ns();
        let overdue: Vec<String> = self
            .amends_awaiting_price
            .iter()
            .filter(|(_, awaiting)| now_ns >= awaiting.deadline_ns)
            .map(|(id, _)| id.clone())
            .collect();
        for client_order_id in overdue {
            let Some(awaiting) = self.amends_awaiting_price.remove(&client_order_id) else {
                continue;
            };
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "amend of {client_order_id} was accepted but its price was never stated within {} ms; cancellation is queued",
                    AMEND_CONFIRM_NS / 1_000_000
                ),
            })?;
            self.amends_pulled_unconfirmed += 1;
            self.pending.push_front(Action::Cancel {
                symbol: awaiting.symbol,
                client_order_id,
            });
        }
        Ok(())
    }

    /// Pull a resting order.
    ///
    /// Not the order path in miniature: there is no barrier before the wire.
    /// A cancel adds no exposure, and an order the log still shows working is
    /// recovered at the next boot whether or not the cancel survived a crash
    /// — so the fsync would buy nothing. `origin_ns` is taken but not
    /// recorded: the latency ledger measures the order path, and mixing a
    /// barrier-free cancel into the submit-result segment would flatter it.
    ///
    /// True means the venue took the change. False means the resting order
    /// is untouched, and whoever asked has to ask again.
    /// Move a held position's venue-native stop, with no order involved.
    ///
    /// The record goes down before the call, as an opening order's does: a
    /// crash between the two must leave the log claiming the tighter stop, so
    /// boot's repair puts that one back rather than the distance the position
    /// opened at. A failed call is logged and dropped -- the old stop is still
    /// standing, the position is still covered, and the next wake asks again.
    async fn process_set_stop(
        &mut self,
        symbol: SymbolId,
        trigger_px: f64,
    ) -> Result<(), EngineError> {
        let symbol_name = self.market.table.name(symbol).to_string();
        let refuse = |reason: &str| WalRecord::Note {
            source: "engine".into(),
            text: format!("stop on {symbol_name} not moved to {trigger_px}: {reason}"),
        };
        if !trigger_px.is_finite() || trigger_px <= 0.0 {
            self.wal
                .append(&refuse("trigger is not a positive finite price"))?;
            return Ok(());
        }
        let mut held = self.account.positions.iter().filter(|p| p.symbol == symbol);
        let Some(position) = held.next() else {
            self.wal
                .append(&refuse("the latest account view has no held position"))?;
            return Ok(());
        };
        if held.next().is_some() || !position.qty.is_finite() || position.qty <= 0.0 {
            self.wal.append(&refuse(
                "the latest position state is ambiguous or unreadable",
            ))?;
            return Ok(());
        }
        let remembered = self
            .intended_stops
            .get(&symbol.0)
            .filter(|stop| stop.side == position.side)
            .map(|stop| stop.trigger_px);
        let venue_stop =
            (position.stop_attached && position.stop_px.is_finite() && position.stop_px > 0.0)
                .then_some(position.stop_px);
        let baseline = match (position.side, remembered, venue_stop) {
            (Side::Buy, Some(a), Some(b)) => Some(a.max(b)),
            (Side::Sell, Some(a), Some(b)) => Some(a.min(b)),
            (_, a, b) => a.or(b),
        };
        let loosens = match (position.side, baseline) {
            (Side::Buy, Some(old)) => trigger_px < old,
            (Side::Sell, Some(old)) => trigger_px > old,
            (_, None) => false,
        };
        if loosens {
            self.wal
                .append(&refuse("the requested stop would loosen protection"))?;
            return Ok(());
        }
        self.wal.append(&WalRecord::StopSet {
            symbol,
            trigger_px,
            wall_ts_ms: clock::wall_ms(),
        })?;
        self.intended_stops.insert(
            symbol.0,
            reconcile::IntendedPositionStop {
                side: position.side,
                trigger_px,
            },
        );
        match self.venue.set_stop(symbol, trigger_px).await {
            Ok(()) => {
                tracing::info!(
                    symbol = self.market.table.name(symbol),
                    trigger_px,
                    "moved this position's stop in"
                );
                Ok(())
            }
            Err(e) => {
                tracing::error!(
                    symbol = self.market.table.name(symbol),
                    trigger_px,
                    error = %e,
                    "could not move this position's stop; the one it opened behind still stands"
                );
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "stop on {} not moved to {trigger_px}: {e}",
                        self.market.table.name(symbol)
                    ),
                })?;
                Ok(())
            }
        }
    }

    /// Record a bounded cancel group, then use the adapter's fastest safe
    /// route. Every answer stays joined to its own client id and the working
    /// supervisor only marks a pull accepted on `Ok`.
    async fn process_cancels(
        &mut self,
        requests: Vec<(SymbolId, String)>,
    ) -> Result<bool, EngineError> {
        if requests.is_empty() {
            return Ok(false);
        }
        if requests.len() > MAX_CANCELS_PER_BATCH {
            return Err(EngineError::State(format!(
                "cancel batch has {} orders; hard maximum is {MAX_CANCELS_PER_BATCH}",
                requests.len()
            )));
        }
        let wire_ns = clock::now_ns();
        for (symbol, client_order_id) in &requests {
            self.wal.append(&WalRecord::CancelSent {
                symbol: *symbol,
                client_order_id: client_order_id.clone(),
                wire_ns,
            })?;
        }
        let queued_ns = clock::now_ns();
        let command_id = self.venue.dispatch_cancels(requests.clone())?;
        self.mark_symbols_busy(requests.iter().map(|(symbol, _)| *symbol));
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Cancels {
                requests,
                queued_ns,
            },
        );
        Ok(true)
    }

    async fn process_amend(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        mut spec: AmendSpec,
        _origin_ns: u64,
    ) -> Result<bool, EngineError> {
        if !self.may_open || !self.private_stream_ready {
            let halt = if !self.may_open {
                "reconciliation opening latch is set"
            } else {
                "private account stream is not ready"
            };
            match self.orders.orders.get(client_order_id) {
                Some(order) if !order.request.reduce_only => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!("{client_order_id} not amended: {halt}"),
                    })?;
                    self.pending.push_front(Action::Cancel {
                        symbol,
                        client_order_id: client_order_id.to_string(),
                    });
                    return Ok(false);
                }
                None => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended while {halt}: order ownership and direction are unknown"
                        ),
                    })?;
                    return Ok(false);
                }
                Some(_) => {}
            }
        }
        if spec.qty.is_some() {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: quantity changes are unsupported until risk and ledger reservations can be resized atomically"),
            })?;
            return Ok(false);
        }
        if spec.px.is_none_or(|px| !px.is_finite() || px <= 0.0) {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: price is not positive and finite"),
            })?;
            return Ok(false);
        }
        if !self.venue.caps().amend_in_place {
            // No quiet fallback to cancel-and-replace. A replaced order is a
            // new order at the back of the queue at a fresh price — a
            // different trade from the one asked for, and the strategy would
            // never learn it had been substituted.
            tracing::warn!(
                id = client_order_id,
                "this venue cannot amend; the order is left alone"
            );
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: this venue cannot change a resting order in place, and cancel-and-replace is a different trade"
                ),
            })?;
            return Ok(false);
        }

        let Some(existing) = self.orders.orders.get(client_order_id).cloned() else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: order is absent from the durable ledger"
                ),
            })?;
            return Ok(false);
        };
        if !existing.in_flight() || existing.request.symbol != symbol {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: order is terminal or names a different symbol"
                ),
            })?;
            return Ok(false);
        }
        if existing.reservation_low_px.to_bits() != existing.reservation_high_px.to_bits() {
            // An order whose working price is unknown cannot be moved: the
            // next reservation would have to cover the range of a range. If
            // an answer is still owed the wait is measured in milliseconds
            // and the asker can come back; the confirmation deadline is what
            // pulls the order when no answer comes. Ambiguity with nothing
            // owed — a range left open across a restart — has no answer
            // coming, so that one is resolved the only way left.
            let awaited = self.amends_awaiting_price.contains_key(client_order_id);
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: if awaited {
                    format!(
                        "{client_order_id} not amended yet: the venue has not said what price its last amend left it at"
                    )
                } else {
                    format!(
                        "{client_order_id} not amended: its prior amend outcome is still ambiguous; cancellation queued"
                    )
                },
            })?;
            if !awaited {
                self.pending.push_front(Action::Cancel {
                    symbol,
                    client_order_id: client_order_id.to_string(),
                });
            }
            return Ok(false);
        }
        let OrderKind::Limit { px: old_px, tif } = existing.request.kind else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: only a resting limit order has a price to change"),
            })?;
            return Ok(false);
        };
        let Some(rule) = self.rules.get(symbol.0 as usize).copied().flatten() else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: instrument rules are unavailable"),
            })?;
            return Ok(false);
        };
        let requested_px = quantize::quantize_px(
            spec.px.expect("positive price checked above"),
            existing.request.side,
            &rule,
        );
        spec.px = Some(requested_px);
        let remaining_qty = existing.request.qty - existing.filled_qty;
        if !remaining_qty.is_finite() || remaining_qty <= 1e-9 {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "{client_order_id} not amended: no readable remaining quantity is working"
                ),
            })?;
            return Ok(false);
        }

        let amended_intent = Intent {
            strategy: existing.request.strategy,
            symbol,
            side: existing.request.side,
            qty: remaining_qty,
            kind: OrderKind::Limit {
                px: requested_px,
                tif,
            },
            stop: existing.request.stop,
            reduce_only: existing.request.reduce_only,
            tag: format!("amend:{client_order_id}"),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        if !existing.request.reduce_only {
            let verdict =
                self.risk
                    .assess_price_amend(client_order_id, &amended_intent, &self.account);
            let verdict = durable_risk_verdict(verdict, remaining_qty, true);
            self.wal.append(&WalRecord::Verdict {
                client_order_id: Some(client_order_id.to_string()),
                verdict: verdict.clone(),
            })?;
            match verdict {
                RiskVerdict::Allow { qty }
                    if qty.is_finite() && (qty - remaining_qty).abs() <= 1e-9 => {}
                RiskVerdict::Allow { qty } => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended: risk approved {qty}, but an in-place price amend cannot resize remaining quantity {remaining_qty}"
                        ),
                    })?;
                    return Ok(false);
                }
                RiskVerdict::Deny { reason } => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!(
                            "{client_order_id} not amended at {requested_px}: {reason:?}"
                        ),
                    })?;
                    return Ok(false);
                }
            }
        }

        // Repricing can multiply notional and stop distance just as surely as
        // a new order can. Journal and reserve the more expensive of old/new
        // before the wire. A crash or transport ambiguity therefore replays
        // the safe side; a definitive venue answer below narrows it back to
        // the price that is actually working.
        let sent = WalRecord::AmendSent {
            symbol,
            client_order_id: client_order_id.to_string(),
            spec,
            wire_ns: clock::now_ns(),
        };
        self.wal.append(&sent)?;
        self.orders.apply(&sent);
        if !existing.request.reduce_only {
            self.risk.register_order_price_range(
                client_order_id,
                &amended_intent,
                remaining_qty,
                old_px.min(requested_px),
                old_px.max(requested_px),
            );
            self.wal.barrier()?;
        }

        let queued_ns = clock::now_ns();
        let command_id = self
            .venue
            .dispatch_amend(symbol, client_order_id.to_string(), spec)?;
        self.mark_symbols_busy([symbol]);
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Amend {
                symbol,
                client_order_id: client_order_id.to_string(),
                spec,
                existing,
                amended_intent: Box::new(amended_intent),
                remaining_qty,
                old_px,
                tif,
                queued_ns,
            },
        );
        Ok(false)
    }

    /// Every order update, wherever it came from, goes through here.
    async fn take_update(&mut self, update: OrderUpdate) -> Result<(), EngineError> {
        // Before anything is done with news about an order: the record of the
        // order that earned it is on the disk. This is the wait the send no
        // longer pays.
        self.settle_barrier()?;
        if let OrderUpdate::FastFill {
            exec_id,
            client_order_id,
            venue_order_id,
            symbol,
            side,
            qty,
            px,
            is_maker,
            venue_ts_ms,
            recv_ns,
        } = &update
        {
            self.wal.append(&WalRecord::FastExecution {
                exec_id: exec_id.clone(),
                client_order_id: client_order_id.clone(),
                venue_order_id: venue_order_id.clone(),
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                is_maker: *is_maker,
                venue_ts_ms: *venue_ts_ms,
                recv_ns: *recv_ns,
            })?;
            self.route_order_update(update);
            return Ok(());
        }
        let stream_reset = matches!(&update, OrderUpdate::StreamReset { .. });
        if stream_reset {
            self.stream_resets += 1;
            // No other event is processed while this handler awaits the two
            // recovery reads, so clearing first is an immediate admission
            // barrier without unnecessarily cancelling healthy orders when
            // the resync succeeds.
            self.private_stream_ready = false;
            self.account.observed_ns = 0;
        }
        let fill_owner = match &update {
            OrderUpdate::Fill {
                client_order_id, ..
            } => self.orders.owner_of(client_order_id),
            _ => None,
        };
        let fill_request = match &update {
            OrderUpdate::Fill {
                client_order_id, ..
            } => self
                .orders
                .orders
                .get(client_order_id)
                .map(|order| order.request.clone()),
            _ => None,
        };
        let delivered_exec_id = match &update {
            OrderUpdate::Fill { exec_id, .. } if !exec_id.is_empty() => Some(exec_id.clone()),
            _ => None,
        };
        let dedup_seen_ms = clock::wall_ms();
        if let Some(exec_id) = delivered_exec_id.as_deref() {
            if !self
                .recovered_exec_ids
                .can_insert(exec_id, dedup_seen_ms)
                .map_err(|e| EngineError::State(e.to_string()))?
            {
                tracing::warn!(exec_id, "duplicate fill ignored");
                return Ok(());
            }
        }
        if let OrderUpdate::Fill {
            exec_id,
            client_order_id,
            symbol,
            side,
            qty,
            px,
            ..
        } = &update
        {
            if let Err(reason) =
                self.orders
                    .validate_fill(client_order_id, *symbol, *side, *qty, *px)
            {
                let finding = Self::untrusted_fill_line(
                    exec_id,
                    client_order_id,
                    *symbol,
                    *side,
                    *qty,
                    *px,
                    &reason,
                );
                if let Some(exec_id) = delivered_exec_id {
                    self.recovered_exec_ids.insert(exec_id, dedup_seen_ms);
                }
                self.may_open = false;
                tracing::error!(%finding, "untrusted fill left order and risk state unchanged");
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: dedup_seen_ms,
                    findings: vec![finding],
                    may_open: false,
                })?;
                self.wal.barrier()?;
                return Ok(());
            }
        }
        self.wal.append(&WalRecord::OrderUpdate {
            update: update.clone(),
        })?;
        if let Some(exec_id) = delivered_exec_id {
            self.recovered_exec_ids.insert(exec_id, dedup_seen_ms);
        }
        self.risk.on_update(&update);
        self.orders.apply_update(&update);
        // The venue naming the price a resting order is working at is the
        // answer an accepted amend was waiting for. It ends the ambiguity
        // the way a definitive rejection does, except that the order stays
        // where it is — with whatever queue position the venue left it.
        let stated_price = match &update {
            OrderUpdate::Amended {
                client_order_id,
                px,
                ..
            } => Some((client_order_id.clone(), *px)),
            _ => None,
        };
        if let Some((client_order_id, px)) = stated_price {
            if let Some(awaiting) = self.amends_awaiting_price.remove(&client_order_id) {
                self.amends_confirmed += 1;
                self.resolve_amend(
                    &client_order_id,
                    &awaiting.existing,
                    &awaiting.amended_intent,
                    awaiting.remaining_qty,
                    px,
                    awaiting.tif,
                )?;
                // The supervisor working this entry prices its next move
                // against where the order actually is, spends one of its
                // amend budget, and starts its cross grace from the cross
                // that really happened. Acceptance alone could tell it none
                // of that, because acceptance does not name a price.
                self.working
                    .amended(&client_order_id, Some(px), true, clock::now_ns());
            }
        }
        if let Some(client_order_id) = inflight::client_order_id(&update) {
            let still_live = self
                .orders
                .orders
                .get(client_order_id)
                .is_some_and(|order| order.in_flight());
            if !still_live {
                self.halt_cancels.remove(client_order_id);
                // An order that has ended has no price left to state. Its
                // reservation went with it: the ending is what released it.
                self.amends_awaiting_price.remove(client_order_id);
            }
        }
        // Only fills joined to orders this log sent enter trusted exposure.
        // Foreign fills remain durable records and latch entries off below.
        if let (
            Some(_),
            OrderUpdate::Fill {
                symbol, side, qty, ..
            },
        ) = (fill_owner, &update)
        {
            reconcile::note_owned_fill(
                &mut self.logged_exposure,
                &mut self.intended_stops,
                fill_request.as_ref(),
                *symbol,
                *side,
                *qty,
            );
        }
        // Remembered for gap recovery's dedup: a fill the stream DID deliver
        // near a gap's edge must not come back from the venue's history as a
        // recovered one.
        if let OrderUpdate::Fill {
            client_order_id,
            venue_ts_ms,
            qty,
            ..
        } = &update
        {
            self.recent_fills
                .push_back((client_order_id.clone(), *venue_ts_ms, *qty));
            while self.recent_fills.len() > RECENT_FILLS_KEPT {
                self.recent_fills.pop_front();
            }
        }
        if let OrderUpdate::Fill {
            client_order_id,
            symbol,
            ..
        } = &update
        {
            if fill_owner.is_none() {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: dedup_seen_ms,
                    findings: vec![Self::foreign_fill_line(client_order_id, *symbol)],
                    may_open: false,
                })?;
                self.wal.barrier()?;
            }
        }
        // Whose fill it was, before any strategy is woken, so the one that
        // placed the order sees its own position already changed. The ledger
        // is asked rather than the registry: the registry knows only this
        // boot's ids and the ones in flight when it started, and a fill can
        // still arrive for an order older than either.
        if let Some(id) = inflight::client_order_id(&update) {
            match self.orders.owner_of(id) {
                Some(sid) => {
                    self.attribution.on_update(sid, &update);
                    self.price_fill(sid, &update);
                    // Terminal news that ends size without a fill releases
                    // that much cover: the whole send on a reject, the
                    // unfilled remainder on a cancel. A fill releases nothing
                    // here — it stays covered until the account reading
                    // shows it, which is the whole point of the cover.
                    let released = self.orders.orders.get(id).and_then(|order| match &update {
                        OrderUpdate::Reject { .. } => {
                            Some((order.request.symbol, order.request.qty))
                        }
                        OrderUpdate::Cancelled { .. } => Some((
                            order.request.symbol,
                            (order.request.qty - order.filled_qty).max(0.0),
                        )),
                        _ => None,
                    });
                    if let Some((symbol, qty)) = released {
                        self.covers.release_newest(sid, symbol, qty);
                    }
                }
                // Charged to nobody on purpose. `reconcile` is what notices
                // the account holds more than the log accounts for, and it
                // already stops the engine opening on top of it.
                None if matches!(update, OrderUpdate::Fill { .. }) => tracing::warn!(
                    id,
                    "a fill for an order this log never recorded sending; it is charged to \
                     no strategy"
                ),
                None => {}
            }
        }

        // A private-stream gap may have swallowed fills. Refresh the account
        // reading now rather than trusting exposure across the gap.
        if stream_reset {
            self.fills.stream_gap();
            let mut account_refreshed = false;
            match self.venue.account_view().await {
                Ok(view) => {
                    self.adopt_view(view);
                    self.enforce_position_stop_intent().await?;
                    account_refreshed = true;
                }
                Err(e) => {
                    tracing::warn!(error = %e, "no fresh account reading after a stream gap");
                }
            }
            // The fills themselves CAN be repaired from the venue: its
            // execution history is asked for the gap, so the log keeps
            // accounting for what actually traded.
            self.recover_gap_fills().await?;
            if account_refreshed {
                self.private_stream_ready = true;
            }
            self.queue_halted_entry_cancels()?;
        }

        self.route_order_update(update);
        Ok(())
    }

    fn route_order_update(&mut self, update: OrderUpdate) {
        let now = clock::now_ns();
        let event = EngineEvent::Order(update.clone());
        match inflight::client_order_id(&update) {
            Some(id) => match self
                .registry
                .owner_of(id)
                .or_else(|| self.orders.owner_of(id))
            {
                Some(sid) => {
                    let Engine {
                        strategies,
                        market,
                        timers,
                        pending,
                        orders,
                        registry,
                        attribution,
                        covers,
                        strategy_checkpoints,
                        strategy_global_checkpoints,
                        strategy_events,
                        runtime_entries_enabled,
                        names,
                        account,
                        rules,
                        ..
                    } = self;
                    feed_strategy(
                        strategies,
                        market,
                        account,
                        rules,
                        timers,
                        pending,
                        orders,
                        registry,
                        attribution,
                        covers,
                        strategy_checkpoints,
                        strategy_global_checkpoints,
                        strategy_events,
                        names,
                        runtime_entries_enabled,
                        sid,
                        &event,
                        now,
                    );
                }
                None => {
                    let ours = self.registry.is_ours(id);
                    tracing::warn!(id, ours, "order update for an order no strategy owns");
                }
            },
            None => {
                // A stop belongs to a symbol, not to an order: tell whoever
                // watches that symbol.
                if let OrderUpdate::StopAttached { symbol, .. } = update {
                    let Engine {
                        strategies,
                        market,
                        timers,
                        pending,
                        routing,
                        orders,
                        registry,
                        attribution,
                        covers,
                        strategy_checkpoints,
                        strategy_global_checkpoints,
                        strategy_events,
                        runtime_entries_enabled,
                        names,
                        account,
                        rules,
                        ..
                    } = self;
                    for sid in routing.all_listeners(symbol) {
                        feed_strategy(
                            strategies,
                            market,
                            account,
                            rules,
                            timers,
                            pending,
                            orders,
                            registry,
                            attribution,
                            covers,
                            strategy_checkpoints,
                            strategy_global_checkpoints,
                            strategy_events,
                            names,
                            runtime_entries_enabled,
                            sid,
                            &event,
                            now,
                        );
                    }
                }
            }
        }
    }

    /// Price one fill against the book that was on the screen when its order
    /// left, and start its markout clock.
    ///
    /// The anchor comes off the order ledger rather than out of memory,
    /// because the ledger is rebuilt from the log at boot: a fill for an order
    /// sent before a restart is still priced against the right midpoint.
    fn price_fill(&mut self, strategy: StrategyId, update: &OrderUpdate) {
        let OrderUpdate::Fill {
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            is_maker,
            venue_ts_ms,
            ..
        } = update
        else {
            return;
        };
        let arrival_mid = self.arrival_mid_of(client_order_id);
        self.fills.on_fill(
            &execution::Fill {
                client_order_id: client_order_id.clone(),
                strategy,
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                is_maker: *is_maker,
                arrival_mid,
                venue_ts_ms: *venue_ts_ms,
            },
            clock::now_ns(),
        );
    }
}
