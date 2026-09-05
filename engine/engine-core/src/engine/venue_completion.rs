use super::*;
use engine_types::{OrderAck, VenueMutationTiming};

/// The clocks every completed venue command carries: when it was queued
/// behind the venue task, when the task took it, when the venue answered,
/// and how long the request quota held it back.
struct CompletionClocks {
    command_id: u64,
    queued_ns: u64,
    started_ns: u64,
    completed_ns: u64,
    rate_wait_ns: Option<u64>,
}

struct CompletedOrders {
    clocks: CompletionClocks,
    requests: Vec<OrderRequest>,
    timings: Vec<(u64, u64)>,
    replies: Vec<Result<OrderAck, VenueError>>,
}

struct CompletedCancels {
    clocks: CompletionClocks,
    requests: Vec<(SymbolId, String)>,
    timing: Option<VenueMutationTiming>,
    replies: Vec<Result<(), VenueError>>,
}

struct CompletedAmend {
    clocks: CompletionClocks,
    symbol: SymbolId,
    client_order_id: String,
    spec: AmendSpec,
    existing: crate::inflight::OrderRec,
    amended_intent: Box<Intent>,
    remaining_qty: f64,
    old_px: f64,
    tif: TimeInForce,
    timing: Option<VenueMutationTiming>,
    reply: Result<(), VenueError>,
}

enum CompletedMutation {
    SetStop {
        clocks: CompletionClocks,
        stop: stop_runtime::DurableStop,
        reply: Result<(), VenueError>,
    },
    Orders(CompletedOrders),
    Cancels(CompletedCancels),
    Amend(Box<CompletedAmend>),
}

impl CompletedMutation {
    fn bind(
        pending: PendingMutation,
        completion: MutationCompletion,
        command_id: u64,
    ) -> Result<Self, EngineError> {
        match (pending, completion) {
            (
                PendingMutation::SetStop { stop, queued_ns },
                MutationCompletion::SetStop {
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                    reply,
                    ..
                },
            ) => Ok(Self::SetStop {
                clocks: CompletionClocks {
                    command_id,
                    queued_ns,
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                },
                stop,
                reply,
            }),
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
                let clocks = CompletionClocks {
                    command_id,
                    queued_ns,
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                };
                Ok(Self::Orders(CompletedOrders {
                    clocks,
                    requests,
                    timings,
                    replies,
                }))
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
                let clocks = CompletionClocks {
                    command_id,
                    queued_ns,
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                };
                Ok(Self::Cancels(CompletedCancels {
                    clocks,
                    requests,
                    timing,
                    replies,
                }))
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
                let clocks = CompletionClocks {
                    command_id,
                    queued_ns,
                    started_ns,
                    completed_ns,
                    rate_wait_ns,
                };
                Ok(Self::Amend(Box::new(CompletedAmend {
                    clocks,
                    symbol,
                    client_order_id,
                    spec,
                    existing: *existing,
                    amended_intent,
                    remaining_qty,
                    old_px,
                    tif,
                    timing,
                    reply,
                })))
            }
            (pending, completion) => {
                let pending_kind = match pending {
                    PendingMutation::Orders { .. } => "orders",
                    PendingMutation::Cancels { .. } => "cancels",
                    PendingMutation::Amend { .. } => "amend",
                    PendingMutation::SetStop { .. } => "stop",
                };
                let completion_kind = match completion {
                    MutationCompletion::Orders { .. } => "orders",
                    MutationCompletion::Cancels { .. } => "cancels",
                    MutationCompletion::Amend { .. } => "amend",
                    MutationCompletion::SetStop { .. } => "stop",
                };
                Err(EngineError::State(format!(
                    "venue task returned {completion_kind} for pending {pending_kind} command {command_id}"
                )))
            }
        }
    }
}

/// Fill identity is captured before the order ledger is changed. Only the
/// journal phase creates this value, after validation and WAL append succeed.
struct CallbackOwners<'a> {
    names: &'a [String],
    destinations: Option<Vec<StrategyId>>,
}

struct JournaledUpdate {
    update: OrderUpdate,
    sequence: u64,
    callbacks: Option<Vec<StrategyId>>,
    fill_owner: Option<StrategyId>,
    fill_request: Option<OrderRequest>,
    dedup_seen_ms: i64,
    allocation: Option<crate::attribution::PreparedPortfolioFill>,
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// The request-quota hold, once per completed command.
    fn record_quota_hold(&mut self, clocks: &CompletionClocks) {
        if let Some(held) = clocks.rate_wait_ns {
            self.ledger.record(Segment::QuotaHold, held);
        }
    }

    /// A batch reply the venue left short is not an error here: the orders
    /// it did not answer for stay in flight and the private stream or a
    /// recovery pass settles them. It is written down.
    fn note_missing_replies(
        &mut self,
        replies: usize,
        requests: usize,
        what: &str,
    ) -> Result<(), EngineError> {
        if replies != requests {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!(
                    "venue returned {replies} answers for {requests} submitted {what}; missing answers remain in flight"
                ),
            })?;
        }
        Ok(())
    }

    /// One order's share of a completed command: the three engine-side
    /// segments and the durable `VenueTiming` row. Returns the moment the
    /// core handled it, for the caller's own end-to-end segment.
    fn journal_venue_timing(
        &mut self,
        clocks: &CompletionClocks,
        operation: &str,
        client_order_id: &str,
        socket_write_ns: Option<u64>,
        ack_ns: Option<u64>,
    ) -> Result<u64, EngineError> {
        let core_handled_ns = clock::now_ns();
        self.ledger.record(
            Segment::DispatchQueue,
            clocks.started_ns.saturating_sub(clocks.queued_ns),
        );
        self.ledger.record(
            Segment::VenueTask,
            clocks.completed_ns.saturating_sub(clocks.started_ns),
        );
        self.ledger.record(
            Segment::CoreResume,
            core_handled_ns.saturating_sub(clocks.completed_ns),
        );
        self.wal.append(&WalRecord::VenueTiming {
            command_id: clocks.command_id,
            operation: operation.to_string(),
            client_order_id: client_order_id.to_string(),
            queued_ns: clocks.queued_ns,
            task_started_ns: clocks.started_ns,
            socket_write_ns,
            ack_ns,
            rate_wait_ns: clocks.rate_wait_ns,
            task_completed_ns: clocks.completed_ns,
            core_handled_ns,
            core_handled_wall_ns: clock::wall_ns(),
        })?;
        Ok(core_handled_ns)
    }

    pub(super) async fn take_venue_completion(
        &mut self,
        completion: MutationCompletion,
    ) -> Result<(), EngineError> {
        let command_id = match &completion {
            MutationCompletion::Orders { command_id, .. }
            | MutationCompletion::Cancels { command_id, .. }
            | MutationCompletion::Amend { command_id, .. }
            | MutationCompletion::SetStop { command_id, .. } => *command_id,
        };
        let pending = self.pending_mutations.remove(&command_id).ok_or_else(|| {
            EngineError::State(format!(
                "venue task returned unknown mutation command {command_id}"
            ))
        })?;

        match CompletedMutation::bind(pending, completion, command_id)? {
            CompletedMutation::SetStop {
                clocks,
                stop,
                reply,
            } => {
                self.record_quota_hold(&clocks);
                self.journal_venue_timing(&clocks, "stop", "", None, None)?;
                self.complete_stop(stop, reply)
            }
            CompletedMutation::Orders(completed) => self.complete_orders(completed).await,
            CompletedMutation::Cancels(completed) => self.complete_cancels(completed),
            CompletedMutation::Amend(completed) => self.complete_amend(*completed),
        }
    }

    async fn complete_orders(&mut self, completed: CompletedOrders) -> Result<(), EngineError> {
        let CompletedOrders {
            clocks,
            requests,
            timings,
            replies,
        } = completed;
        let CompletionClocks {
            command_id,
            queued_ns,
            started_ns,
            completed_ns,
            ..
        } = clocks;
        self.record_quota_hold(&clocks);
        self.note_missing_replies(replies.len(), requests.len(), "orders")?;
        tracing::debug!(
            command_id,
            queue_ns = started_ns.saturating_sub(queued_ns),
            venue_ns = completed_ns.saturating_sub(started_ns),
            "placement command completed"
        );
        let symbols: Vec<_> = requests.iter().map(|request| request.symbol).collect();
        let mut replies = replies.into_iter();
        for (request, (decided_ns, origin_ns)) in requests.into_iter().zip(timings) {
            self.ledger
                .record(Segment::Wire, completed_ns.saturating_sub(decided_ns));
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
            self.journal_venue_timing(
                &clocks,
                "place",
                &request.client_order_id,
                socket_write_ns,
                ack_timing_ns,
            )?;
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
                    self.dispatches
                        .unresolved
                        .insert(request.client_order_id.clone(), other.to_string());
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
        Ok(())
    }

    fn complete_cancels(&mut self, completed: CompletedCancels) -> Result<(), EngineError> {
        let CompletedCancels {
            clocks,
            requests,
            timing,
            replies,
        } = completed;
        let CompletionClocks {
            command_id,
            started_ns,
            completed_ns,
            ..
        } = clocks;
        self.record_quota_hold(&clocks);
        self.note_missing_replies(replies.len(), requests.len(), "cancels")?;
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
            self.journal_venue_timing(
                &clocks,
                "cancel",
                &client_order_id,
                timing.map(|mark| mark.sent_ns),
                timing.map(|mark| mark.ack_ns),
            )?;
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
                        halt_failure = Some(format!("{client_order_id}: {code}: {message}"));
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
        Ok(())
    }

    fn complete_amend(&mut self, completed: CompletedAmend) -> Result<(), EngineError> {
        let CompletedAmend {
            clocks,
            symbol,
            client_order_id,
            spec,
            existing,
            amended_intent,
            remaining_qty,
            old_px,
            tif,
            timing,
            reply,
        } = completed;
        let CompletionClocks {
            command_id,
            started_ns,
            completed_ns,
            ..
        } = clocks;
        self.record_quota_hold(&clocks);
        if let Some(mark) = timing {
            self.ledger
                .record(Segment::Ack, mark.ack_ns.saturating_sub(mark.sent_ns));
        }
        self.journal_venue_timing(
            &clocks,
            "amend",
            &client_order_id,
            timing.map(|mark| mark.sent_ns),
            timing.map(|mark| mark.ack_ns),
        )?;
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
                if self
                    .books
                    .orders
                    .orders
                    .get(&client_order_id)
                    .is_none_or(|row| {
                        !row.in_flight() || row.reservation_low_px == row.reservation_high_px
                    })
                {
                    self.release_symbols([symbol]);
                    return Ok(());
                }
                self.amends_awaiting_price.insert(
                    client_order_id.clone(),
                    AwaitingAmend {
                        symbol,
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
                    &amended_intent,
                    remaining_qty,
                    old_px,
                    tif,
                    existing
                        .request
                        .exact_terms
                        .as_ref()
                        .and_then(|terms| terms.limit_price.clone())
                        .map(engine_types::numeric::ExactNumber::derived),
                )?;
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!("amend of {client_order_id} never sent: {detail}"),
                })?;
            }
            Err(VenueError::Rejected { code, message }) => {
                self.resolve_amend(
                    &client_order_id,
                    &amended_intent,
                    remaining_qty,
                    old_px,
                    tif,
                    existing
                        .request
                        .exact_terms
                        .as_ref()
                        .and_then(|terms| terms.limit_price.clone())
                        .map(engine_types::numeric::ExactNumber::derived),
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
                self.host.pending.push_front(
                    Action::Cancel {
                        symbol,
                        client_order_id: client_order_id.clone(),
                    }
                    .into(),
                );
            }
        }
        self.working
            .amended(&client_order_id, spec.px, false, clock::now_ns());
        self.release_symbols([symbol]);
        Ok(())
    }

    /// Narrow an amend's conservative old/new reservation to the one price
    /// the order is actually working at.
    ///
    /// Called with the old price when the amend never took, and with the
    /// venue's own stated price when it did. Both are the same act: the
    /// range was held open because the price was unknown, and this is where
    /// it becomes known.
    fn resolve_amend(
        &mut self,
        client_order_id: &str,
        amended_intent: &Intent,
        remaining_qty: f64,
        effective_px: f64,
        tif: TimeInForce,
        exact_effective_px: Option<engine_types::numeric::ExactNumber>,
    ) -> Result<(), EngineError> {
        let exact_effective_px = Some(
            exact_effective_px.unwrap_or(
                engine_types::numeric::ExactNumber::legacy_binary64(effective_px)
                    .map_err(|e| EngineError::State(e.to_string()))?,
            ),
        );
        let resolved = WalRecord::AmendResolved {
            client_order_id: client_order_id.to_string(),
            effective_px,
            exact_effective_px,
        };
        self.books
            .orders
            .validate_record_quantities(&resolved)
            .map_err(EngineError::State)?;
        self.wal.append(&resolved)?;
        self.books
            .orders
            .try_apply(&resolved)
            .map_err(EngineError::State)?;
        let mut settled = amended_intent.clone();
        settled.kind = OrderKind::Limit {
            px: effective_px,
            tif,
        };
        let remaining_qty = self
            .books
            .orders
            .orders
            .get(client_order_id)
            .map(|order| order.remaining_qty())
            .transpose()
            .map_err(EngineError::State)?
            .unwrap_or(remaining_qty);
        if self
            .books
            .orders
            .orders
            .get(client_order_id)
            .is_some_and(|order| order.in_flight())
            && remaining_qty > 0.0
        {
            self.risk.register_order_with_account(
                client_order_id,
                &settled,
                remaining_qty,
                &self.books.account,
            );
            self.risk
                .mark_order_accepted(client_order_id, clock::now_ns());
        }
        Ok(())
    }

    /// Pull any order whose accepted amend the private stream never
    /// explained. The fallback is exactly what an unamendable venue gets:
    /// take the order down, because an order resting at a price the engine
    /// cannot name is one it cannot price its own book against.
    pub(super) fn pull_unconfirmed_amends(&mut self) -> Result<(), EngineError> {
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
            self.host.pending.push_front(
                Action::Cancel {
                    symbol: awaiting.symbol,
                    client_order_id,
                }
                .into(),
            );
        }
        Ok(())
    }

    /// Pull a resting order.
    ///
    /// Record a bounded cancel group, then use the adapter's fastest safe
    /// route. Every answer stays joined to its own client id and the working
    /// supervisor only marks a pull accepted on `Ok`.
    pub(super) async fn process_cancels(
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
        let mut wire_requests = Vec::with_capacity(requests.len());
        for (symbol, id) in requests {
            if self.dispatches.orders.get(&id).is_some_and(|order| {
                order.phase == engine_types::order_dispatch::OrderDispatchPhase::Queued
            }) {
                self.take_update(OrderUpdate::Cancelled {
                    client_order_id: id.clone(),
                    recv_ns: clock::now_ns(),
                })
                .await?;
                self.complete_order_dispatch(&id)?;
            } else {
                wire_requests.push((symbol, id));
            }
        }
        let requests = wire_requests;
        if requests.is_empty() {
            return Ok(false);
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

    pub(super) async fn process_amend(
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
            match self.books.orders.orders.get(client_order_id) {
                Some(order) if !order.request.is_sleeve_reduction() => {
                    self.wal.append(&WalRecord::Note {
                        source: "risk".into(),
                        text: format!("{client_order_id} not amended: {halt}"),
                    })?;
                    self.host.pending.push_front(
                        Action::Cancel {
                            symbol,
                            client_order_id: client_order_id.to_string(),
                        }
                        .into(),
                    );
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
        if let Some((reason, owned_symbol)) = self
            .books
            .orders
            .orders
            .get(client_order_id)
            .filter(|order| !order.request.is_sleeve_reduction())
            .and_then(|order| {
                self.opening_permission_reason(order.request.strategy)
                    .map(|reason| (reason, order.request.symbol))
            })
        {
            self.wal.append(&WalRecord::Note {
                source: "risk".into(),
                text: format!("{client_order_id} not amended: {reason}; cancellation queued"),
            })?;
            self.enqueue_halt_cancel(owned_symbol, client_order_id.to_string());
            return Ok(false);
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

        let Some(existing) = self.books.orders.orders.get(client_order_id).cloned() else {
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
                self.host.pending.push_front(
                    Action::Cancel {
                        symbol,
                        client_order_id: client_order_id.to_string(),
                    }
                    .into(),
                );
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
        let Some(rule) = self.books.rules.get(symbol.0 as usize).copied().flatten() else {
            self.wal.append(&WalRecord::Note {
                source: "engine".into(),
                text: format!("{client_order_id} not amended: instrument rules are unavailable"),
            })?;
            return Ok(false);
        };
        let requested_px = if let Some(instrument) = self.instrument_specs.get(&symbol) {
            let reference = self.reference_px(symbol, &OrderKind::Market);
            let terms = match engine_types::order_terms::quantize_amend(
                instrument,
                &existing.request,
                &spec,
                reference,
            ) {
                Ok(terms) => terms,
                Err(error) => {
                    self.wal.append(&WalRecord::Note {
                        source: "engine".into(),
                        text: format!("{client_order_id} not amended: {error}"),
                    })?;
                    return Ok(false);
                }
            };
            terms
                .apply_projection(&mut spec)
                .map_err(|e| EngineError::State(e.to_string()))?;
            spec.px.expect("price amendment retains price")
        } else {
            if self.require_exact_instruments || existing.request.exact_terms.is_some() {
                self.wal.append(&WalRecord::Note {
                    source: "engine".into(),
                    text: format!(
                        "{client_order_id} not amended: exact instrument metadata is unavailable"
                    ),
                })?;
                return Ok(false);
            }
            let px = quantize::quantize_px(
                spec.px.expect("positive price checked above"),
                existing.request.side,
                &rule,
            );
            spec.px = Some(px);
            spec.exact_terms = None;
            px
        };
        if requested_px == old_px
            && spec
                .exact_terms
                .as_ref()
                .and_then(|terms| terms.limit_price.as_ref())
                .is_none_or(|px| {
                    existing
                        .request
                        .exact_terms
                        .as_ref()
                        .and_then(|terms| terms.limit_price.as_ref())
                        == Some(px)
                })
        {
            return Ok(false);
        }
        let remaining_qty = existing.remaining_qty().map_err(EngineError::State)?;
        if !remaining_qty.is_finite() || remaining_qty <= 0.0 {
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
            stop: existing.request.sleeve_stop(),
            reduce_only: existing.request.is_sleeve_reduction(),
            tag: format!("amend:{client_order_id}"),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        if self.instrument_specs.contains_key(&symbol) || !existing.request.is_sleeve_reduction() {
            let verdict = if self.instrument_specs.contains_key(&symbol) {
                match self.risk.reassess_portfolio_order(
                    client_order_id,
                    &amended_intent,
                    &self.books.account,
                    &self.books.attribution.snapshot(),
                ) {
                    engine_types::risk::PortfolioRiskVerdict::Allow { qty, .. } => {
                        RiskVerdict::Allow { qty }
                    }
                    engine_types::risk::PortfolioRiskVerdict::Deny { reason } => {
                        RiskVerdict::Deny { reason }
                    }
                }
            } else if self.symbol_owned_by_another(existing.request.strategy, symbol) {
                RiskVerdict::Deny {
                    reason: DenyReason::UnknownState {
                        detail: "foreign_strategy_owner: another strategy owns exposure or a live opening order on this symbol".into(),
                    },
                }
            } else {
                self.risk
                    .assess_price_amend(client_order_id, &amended_intent, &self.books.account)
            };
            let verdict = durable_risk_verdict(verdict, remaining_qty, true);
            self.wal.append(&WalRecord::Verdict {
                client_order_id: Some(client_order_id.to_string()),
                verdict: verdict.clone(),
            })?;
            match verdict {
                RiskVerdict::Allow { qty } if qty.is_finite() && qty == remaining_qty => {}
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
            spec: spec.clone(),
            wire_ns: clock::now_ns(),
        };
        self.wal.append(&sent)?;
        self.books
            .orders
            .try_apply(&sent)
            .map_err(EngineError::State)?;
        self.risk.register_order_price_range_with_account(
            client_order_id,
            &amended_intent,
            remaining_qty,
            (old_px.min(requested_px), old_px.max(requested_px)),
            &self.books.account,
        );
        let barrier = self.wal.barrier_begin()?;
        self.dispatches.begin(
            crate::order_dispatch::DispatchWrite::Amend(Box::new(
                crate::order_dispatch::DurableAmend {
                    symbol,
                    client_order_id: client_order_id.to_owned(),
                    spec,
                    existing,
                    amended_intent,
                    remaining_qty,
                    old_px,
                    tif,
                },
            )),
            barrier,
        );
        Ok(false)
    }

    pub(super) fn dispatch_durable_amend(
        &mut self,
        amend: crate::order_dispatch::DurableAmend,
    ) -> Result<(), EngineError> {
        let crate::order_dispatch::DurableAmend {
            symbol,
            client_order_id,
            spec,
            existing,
            amended_intent,
            remaining_qty,
            old_px,
            tif,
        } = amend;
        let current = self
            .books
            .orders
            .orders
            .get(&client_order_id)
            .ok_or_else(|| EngineError::State("durable amendment lost its order".into()))?;
        if !current.in_flight() {
            return Ok(());
        }
        let remaining_now = current.remaining_qty().map_err(EngineError::State)?;
        let changed = remaining_now != remaining_qty
            || current.reservation_low_px == current.reservation_high_px;
        let permission = existing.request.is_sleeve_reduction()
            || (self.may_open
                && self.private_stream_ready
                && self
                    .opening_permission_reason(existing.request.strategy)
                    .is_none());
        let risk = if self.instrument_specs.contains_key(&symbol) {
            matches!(self.risk.reassess_portfolio_order(&client_order_id, &amended_intent, &self.books.account, &self.books.attribution.snapshot()), engine_types::risk::PortfolioRiskVerdict::Allow { qty, .. } if qty == remaining_qty)
        } else if !existing.request.is_sleeve_reduction() {
            matches!(self.risk.assess_price_amend(&client_order_id, &amended_intent, &self.books.account), RiskVerdict::Allow { qty } if qty == remaining_qty)
        } else {
            true
        };
        if changed || !permission || !risk {
            if current.reservation_low_px != current.reservation_high_px {
                self.resolve_amend(
                    &client_order_id,
                    &amended_intent,
                    remaining_now,
                    old_px,
                    tif,
                    existing
                        .request
                        .exact_terms
                        .as_ref()
                        .and_then(|terms| terms.limit_price.clone())
                        .map(engine_types::numeric::ExactNumber::derived),
                )?;
            }
            self.wal.append(&WalRecord::Note { source: "engine".into(), text: format!("{client_order_id} amendment never sent: order or admission changed while awaiting durability") })?;
            return Ok(());
        }
        self.risk.mark_order_attempted(&client_order_id);
        let queued_ns = clock::now_ns();
        let command_id =
            self.venue
                .dispatch_amend(symbol, client_order_id.clone(), spec.clone())?;
        self.mark_symbols_busy([symbol]);
        self.pending_mutations.insert(
            command_id,
            PendingMutation::Amend {
                symbol,
                client_order_id,
                spec,
                existing: Box::new(existing),
                amended_intent: Box::new(amended_intent),
                remaining_qty,
                old_px,
                tif,
                queued_ns,
            },
        );
        Ok(())
    }

    /// Every order update, wherever it came from, goes through here.
    pub(super) async fn take_update(&mut self, update: OrderUpdate) -> Result<(), EngineError> {
        let callbacks = self
            .host
            .callbacks
            .isolated()
            .then(|| self.order_callback_owners(&update));
        if callbacks.is_some() {
            self.ensure_callback_reader(&[])?;
        }
        if Self::journal_fast_execution(&update, &mut self.wal)? {
            let sequence = if callbacks.is_some() {
                self.wal.append(&WalRecord::OrderUpdate {
                    callbacks: callbacks.clone(),
                    update: update.clone(),
                })?
            } else {
                0
            };
            self.route_order_update(update, sequence, callbacks.as_deref())?;
            return Ok(());
        }
        let stream_reset = matches!(&update, OrderUpdate::StreamReset { .. });
        if stream_reset {
            self.stream_resets += 1;
            self.private_stream_ready = false;
            self.books.account.observed_ns = 0;
        }
        let Some(journaled) = Self::journal_update(
            update,
            &mut self.wal,
            &self.books.orders,
            &self.books.attribution,
            CallbackOwners {
                names: &self.host.names,
                destinations: callbacks,
            },
            &mut self.recovered_exec_ids,
            &mut self.may_open,
        )?
        else {
            return Ok(());
        };
        let sequence = journaled.sequence;
        let callbacks = journaled.callbacks.clone();
        let update = self.apply_journaled_update(journaled)?;
        self.observe_order_dispatch(&update)?;
        if stream_reset {
            self.refresh_private_stream_after_gap().await?;
        }
        self.route_order_update(update, sequence, callbacks.as_deref())?;
        Ok(())
    }

    fn journal_fast_execution(update: &OrderUpdate, wal: &mut W) -> Result<bool, EngineError> {
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
        } = update
        {
            wal.append(&WalRecord::FastExecution {
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
            return Ok(true);
        }
        Ok(false)
    }

    fn journal_update(
        mut update: OrderUpdate,
        wal: &mut W,
        orders: &LedgerOfOrders,
        attribution: &Attribution,
        callback_owners: CallbackOwners<'_>,
        recovered_exec_ids: &mut ExecutionIds,
        may_open: &mut bool,
    ) -> Result<Option<JournaledUpdate>, EngineError> {
        let CallbackOwners {
            names: strategy_names,
            destinations: mut callbacks,
        } = callback_owners;
        if matches!(&update, OrderUpdate::Amended { .. }) {
            let record = WalRecord::OrderUpdate {
                callbacks: None,
                update: update.clone(),
            };
            if let Err(reason) = orders.validate_record_quantities(&record) {
                *may_open = false;
                wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: clock::wall_ms(),
                    findings: vec![format!("untrusted amended order state: {reason}")],
                    may_open: false,
                })?;
                wal.barrier()?;
                return Ok(None);
            }
        }
        let fill_owner = match &update {
            OrderUpdate::Fill {
                client_order_id, ..
            } => orders.owner_of(client_order_id),
            _ => None,
        };
        let fill_request = match &update {
            OrderUpdate::Fill {
                client_order_id, ..
            } => orders
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
        let mut allocation = None;
        if let Some(exec_id) = delivered_exec_id.as_deref() {
            if !recovered_exec_ids
                .can_insert(exec_id, dedup_seen_ms)
                .map_err(|e| EngineError::State(e.to_string()))?
            {
                tracing::warn!(exec_id, "duplicate fill ignored");
                return Ok(None);
            }
        }
        if let OrderUpdate::Fill {
            exec_id,
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            amounts,
            ..
        } = &update
        {
            if let Err(reason) = orders
                .validate_fill(client_order_id, *symbol, *side, *qty, *px)
                .and_then(|()| {
                    orders.validate_fill_quantities(client_order_id, *qty, amounts.as_deref())
                })
                .and_then(|()| {
                    amounts.as_ref().map_or(Ok(()), |values| {
                        values
                            .validate_projection(*qty, *px, *fee)
                            .map_err(|error| error.to_string())
                    })
                })
                .and_then(|()| {
                    allocation = attribution.prepare_portfolio_update_for_order(
                        fill_request.as_ref(),
                        strategy_names,
                        &update,
                    )?;
                    Ok(())
                })
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
                    recovered_exec_ids.insert(exec_id, dedup_seen_ms);
                }
                *may_open = false;
                tracing::error!(%finding, "untrusted fill left order and risk state unchanged");
                wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: dedup_seen_ms,
                    findings: vec![finding],
                    may_open: false,
                })?;
                wal.barrier()?;
                return Ok(None);
            }
        }
        if let (
            Some(prepared),
            OrderUpdate::Fill {
                allocation: recorded,
                ..
            },
        ) = (&allocation, &mut update)
        {
            if callbacks.is_some()
                || prepared.allocation.policy
                    == engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo
            {
                *recorded = Some(Box::new(prepared.allocation.clone()));
            }
            if let Some(owners) = &mut callbacks {
                *owners = prepared
                    .allocation
                    .slices
                    .iter()
                    .map(|slice| slice.strategy)
                    .collect();
                owners.sort();
                owners.dedup();
            }
        }
        let sequence = wal.append(&WalRecord::OrderUpdate {
            callbacks: callbacks.clone(),
            update: update.clone(),
        })?;
        if let Some(exec_id) = delivered_exec_id {
            recovered_exec_ids.insert(exec_id, dedup_seen_ms);
        }
        Ok(Some(JournaledUpdate {
            update,
            sequence,
            callbacks,
            fill_owner,
            fill_request,
            dedup_seen_ms,
            allocation,
        }))
    }

    fn apply_journaled_update(
        &mut self,
        journaled: JournaledUpdate,
    ) -> Result<OrderUpdate, EngineError> {
        let JournaledUpdate {
            update,
            fill_owner,
            fill_request,
            dedup_seen_ms,
            allocation,
            ..
        } = journaled;
        let owned_fill = allocation.is_some();
        if let Some(allocation) = allocation {
            self.books
                .attribution
                .commit_portfolio_fill(allocation)
                .map_err(EngineError::State)?;
        }
        self.portfolio_controls
            .apply(&WalRecord::OrderUpdate {
                callbacks: None,
                update: update.clone(),
            })
            .map_err(EngineError::State)?;
        self.portfolio_controls
            .retain_native_offsets(&self.books.attribution.snapshot());
        self.observe_portfolio_physical_update(&update);
        self.books
            .orders
            .try_apply_update(&update)
            .map_err(EngineError::State)?;
        self.update_risk_from_canonical_order(&update)?;
        self.resolve_private_order_state(&update)?;
        self.update_fill_exposure(&update, owned_fill, fill_request.as_ref(), dedup_seen_ms)?;
        self.attribute_order_update(&update, fill_owner)?;
        Ok(update)
    }

    pub(super) fn update_risk_from_canonical_order(
        &mut self,
        update: &OrderUpdate,
    ) -> Result<(), EngineError> {
        if let OrderUpdate::Fill {
            client_order_id, ..
        } = update
        {
            if let Some(order) = self.books.orders.orders.get(client_order_id) {
                let remaining = if order.in_flight() {
                    order.remaining_qty().map_err(EngineError::State)?
                } else {
                    0.0
                };
                return self
                    .risk
                    .on_update_with_remaining(update, remaining)
                    .map_err(|e| EngineError::State(format!("{e:?}")));
            }
        }
        self.risk.on_update(update);
        Ok(())
    }

    fn resolve_private_order_state(&mut self, update: &OrderUpdate) -> Result<(), EngineError> {
        // The venue naming the price a resting order is working at is the
        // answer an accepted amend was waiting for. It ends the ambiguity
        // the way a definitive rejection does, except that the order stays
        // where it is — with whatever queue position the venue left it.
        let stated_price = match update {
            OrderUpdate::Amended {
                client_order_id,
                px,
                exact_terms,
                ..
            } => Some((
                client_order_id.clone(),
                *px,
                exact_terms.as_ref().map(|terms| terms.price.clone()),
            )),
            _ => None,
        };
        if let Some((client_order_id, px, exact_price)) = stated_price {
            let awaiting = self
                .amends_awaiting_price
                .remove(&client_order_id)
                .or_else(|| {
                    let row = self.books.orders.orders.get(&client_order_id)?;
                    if !row.in_flight() || row.reservation_low_px == row.reservation_high_px {
                        return None;
                    }
                    let OrderKind::Limit { tif, .. } = row.request.kind else {
                        return None;
                    };
                    Some(AwaitingAmend {
                        symbol: row.request.symbol,
                        amended_intent: Box::new(Intent {
                            strategy: row.request.strategy,
                            symbol: row.request.symbol,
                            side: row.request.side,
                            qty: row.remaining_qty().ok()?,
                            kind: row.request.kind,
                            stop: row.request.sleeve_stop(),
                            reduce_only: row.request.is_sleeve_reduction(),
                            tag: format!("amend:{client_order_id}"),
                            decided_ns: clock::now_ns(),
                            work: None,
                            leverage: None,
                        }),
                        remaining_qty: row.remaining_qty().ok()?,
                        tif,
                        deadline_ns: clock::now_ns(),
                    })
                });
            if let Some(awaiting) = awaiting {
                self.amends_confirmed += 1;
                self.resolve_amend(
                    &client_order_id,
                    &awaiting.amended_intent,
                    awaiting.remaining_qty,
                    px,
                    awaiting.tif,
                    exact_price,
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
        if let Some(client_order_id) = inflight::client_order_id(update) {
            let still_live = self
                .books
                .orders
                .orders
                .get(client_order_id)
                .is_some_and(|order| order.in_flight());
            if !still_live {
                self.risk.complete_order(client_order_id, clock::now_ns());
                self.halt_cancels.remove(client_order_id);
                // An order that has ended has no price left to state. Its
                // reservation went with it: the ending is what released it.
                self.amends_awaiting_price.remove(client_order_id);
            }
        }
        Ok(())
    }

    fn update_fill_exposure(
        &mut self,
        update: &OrderUpdate,
        owned_fill: bool,
        fill_request: Option<&OrderRequest>,
        dedup_seen_ms: i64,
    ) -> Result<(), EngineError> {
        // Only fills joined to orders this log sent enter trusted exposure.
        // Foreign fills remain durable records and latch entries off below.
        if let (
            true,
            OrderUpdate::Fill {
                symbol,
                side,
                qty,
                amounts,
                ..
            },
        ) = (owned_fill, update)
        {
            reconcile::note_owned_fill(
                &mut self.logged_exposure,
                &mut self.intended_stops,
                fill_request,
                *symbol,
                *side,
                &reconcile::fill_quantity(*qty, amounts.as_deref()).map_err(EngineError::State)?,
            )
            .map_err(EngineError::State)?;
        }
        // Remembered for gap recovery's dedup: a fill the stream DID deliver
        // near a gap's edge must not come back from the venue's history as a
        // recovered one.
        if let OrderUpdate::Fill {
            exec_id,
            client_order_id,
            venue_ts_ms,
            qty,
            ..
        } = update
        {
            if exec_id.is_empty() {
                self.recent_fills
                    .push_back((client_order_id.clone(), *venue_ts_ms, *qty));
            }
            while self.recent_fills.len() > RECENT_FILLS_KEPT {
                self.recent_fills.pop_front();
            }
        }
        if let OrderUpdate::Fill {
            client_order_id,
            symbol,
            ..
        } = update
        {
            if !owned_fill {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: dedup_seen_ms,
                    findings: vec![Self::foreign_fill_line(client_order_id, *symbol)],
                    may_open: false,
                })?;
                self.wal.barrier()?;
            }
        }
        Ok(())
    }

    fn attribute_order_update(
        &mut self,
        update: &OrderUpdate,
        fill_owner: Option<StrategyId>,
    ) -> Result<(), EngineError> {
        if let Some(slices) =
            crate::portfolio_allocation::slice_updates(update).map_err(EngineError::State)?
        {
            for (owner, slice) in slices {
                self.price_fill(owner, &slice)?;
            }
            return Ok(());
        }
        // Whose fill it was, before any strategy is woken, so the one that
        // placed the order sees its own position already changed. The ledger
        // is asked rather than the registry: the registry knows only this
        // boot's ids and the ones in flight when it started, and a fill can
        // still arrive for an order older than either.
        if let Some(id) = inflight::client_order_id(update) {
            match self.books.orders.owner_of(id).or(fill_owner) {
                Some(sid) => {
                    if let Some(order) = self.books.orders.orders.get(id) {
                        self.books.attribution.remember_order_stop(&order.request);
                    }
                    self.price_fill(sid, update)?;
                    // Terminal news that ends size without a fill releases
                    // that much cover: the whole send on a reject, the
                    // unfilled remainder on a cancel. A fill releases nothing
                    // here — it stays covered until the account reading
                    // shows it, which is the whole point of the cover.
                    let released =
                        self.books
                            .orders
                            .orders
                            .get(id)
                            .and_then(|order| match update {
                                OrderUpdate::Reject { .. } => {
                                    Some((order.request.symbol, order.request.qty))
                                }
                                OrderUpdate::Cancelled { .. } => Some((
                                    order.request.symbol,
                                    order.remaining_qty().expect("validated order quantity"),
                                )),
                                _ => None,
                            });
                    if let Some((symbol, qty)) = released {
                        self.books.covers.release_newest(sid, symbol, qty);
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
        Ok(())
    }

    async fn refresh_private_stream_after_gap(&mut self) -> Result<(), EngineError> {
        self.fills.stream_gap();
        self.recovery.reconnected();
        self.request_account_refresh_after(clock::now_ns());
        self.launch_account_recovery(true);
        self.queue_halted_entry_cancels()?;
        Ok(())
    }

    fn order_callback_owners(&self, update: &OrderUpdate) -> Vec<StrategyId> {
        if let Ok(Some(slices)) = crate::portfolio_allocation::slice_updates(update) {
            return slices.into_iter().map(|(owner, _)| owner).collect();
        }
        match inflight::client_order_id(update) {
            Some(id) => self
                .books
                .registry
                .owner_of(id)
                .or_else(|| self.books.orders.owner_of(id))
                .into_iter()
                .collect(),
            None => match update {
                OrderUpdate::StopAttached { symbol, .. } => self.routing.all_listeners(*symbol),
                _ => Vec::new(),
            },
        }
    }

    pub(super) fn route_order_update(
        &mut self,
        update: OrderUpdate,
        sequence: u64,
        callbacks: Option<&[StrategyId]>,
    ) -> Result<(), EngineError> {
        let owners = callbacks
            .map(<[StrategyId]>::to_vec)
            .unwrap_or_else(|| self.order_callback_owners(&update));
        let ready: Vec<_> = owners
            .iter()
            .copied()
            .filter(|owner| !self.host.callbacks.order_news.unread_for(*owner))
            .collect();
        if callbacks.is_some() {
            self.host
                .callbacks
                .order_news
                .record(sequence, &owners)
                .map_err(EngineError::State)?;
        }
        for owner in owners {
            let view = crate::strategy_process::order_news::OrderNews::slice(&update, owner)
                .map_err(EngineError::State)?;
            if callbacks.is_some() {
                if !ready.contains(&owner) {
                    continue;
                }
                let origin = self
                    .host
                    .callbacks
                    .order_news
                    .origin(sequence)
                    .map_err(EngineError::State)?;
                if let Err(error) = self.host.callbacks.enqueue_order(owner, view, origin) {
                    self.host.callbacks.faults.insert(owner, error);
                }
            } else {
                self.host.feed(
                    &self.books,
                    owner,
                    &EngineEvent::Order(view),
                    clock::now_ns(),
                );
            }
        }
        Ok(())
    }

    /// Price one fill against the book that was on the screen when its order
    /// left, and start its markout clock.
    ///
    /// The anchor comes off the order ledger rather than out of memory,
    /// because the ledger is rebuilt from the log at boot: a fill for an order
    /// sent before a restart is still priced against the right midpoint.
    fn price_fill(
        &mut self,
        strategy: StrategyId,
        update: &OrderUpdate,
    ) -> Result<(), EngineError> {
        let OrderUpdate::Fill {
            amounts,
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
            return Ok(());
        };
        let arrival_mid = self.arrival_mid_of(client_order_id);
        self.fills
            .on_fill_with_quantity(
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
                amounts.as_deref().map(|a| &a.quantity.value),
            )
            .map_err(EngineError::State)
    }
}
