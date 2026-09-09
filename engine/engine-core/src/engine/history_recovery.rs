use super::account_recovery::{HistoryBatch, Phase, HISTORY_ROWS_PER_TURN};
use super::*;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) async fn checkpoint_history_if_due(&mut self) -> Result<(), EngineError> {
        if clock::wall_ms() >= self.next_history_checkpoint_ms {
            self.recovery.history_requested = true;
        }
        self.service_account_recovery().await
    }

    #[cfg(test)]
    pub(crate) async fn renew_execution_history(&mut self) -> Result<(), EngineError> {
        self.recovery.history_requested = true;
        loop {
            self.service_order_lineage().await?;
            self.service_account_recovery().await?;
            if !self.recovery.history_requested && !self.recovery.uncommitted() {
                break;
            }
            if self.recovery.waiting() {
                let completion =
                    tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.recovery.completed.recv())
                        .await
                        .map_err(|_| EngineError::State("history renewal timed out".into()))?
                        .ok_or_else(|| EngineError::State("recovery task stopped".into()))?;
                self.on_recovery_completion(completion).await?;
            } else {
                tokio::task::yield_now().await;
            }
        }
        Ok(())
    }

    pub(super) fn apply_history_batch(
        &mut self,
        mut batch: HistoryBatch,
    ) -> Result<(), EngineError> {
        let now_ms = batch.query.history.expect("history batch interval").1;
        for _ in 0..HISTORY_ROWS_PER_TURN {
            let next = match batch.resume.take() {
                Some(exec) => Some(exec),
                None => batch.rows.pop_front()?,
            };
            let Some(exec) = next else {
                break;
            };

            if self.recovered_exec_ids.contains(&exec.exec_id, now_ms) {
                continue;
            }
            let key = (
                exec.client_order_id.clone(),
                exec.venue_ts_ms,
                exec.qty.to_bits(),
            );
            let delivered = self
                .recent_fills
                .iter()
                .filter(|(id, ts, qty)| id == &key.0 && *ts == key.1 && qty.to_bits() == key.2)
                .count();
            let same_delivered = delivered > 0 && {
                let used = batch.delivered.entry(key).or_default();
                let same = *used < delivered;
                if same {
                    *used += 1;
                }
                same
            };
            if same_delivered {
                continue;
            }
            self.recovered_exec_ids
                .can_insert(&exec.exec_id, now_ms)
                .map_err(|e| EngineError::State(e.to_string()))?;
            let Some(symbol) = self.books.market.table.get(&exec.symbol) else {
                let finding = Self::foreign_unmapped_execution_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    &exec.symbol,
                    exec.qty,
                );
                self.wal.append(&WalRecord::Note {
                    source: "fill-recovery".into(),
                    text: finding.clone(),
                })?;
                self.recovered_exec_ids.insert(exec.exec_id, now_ms);
                batch.foreign.push(finding);
                batch.recovered += 1;
                continue;
            };
            if !self.require_order_lineage(&exec.client_order_id, Some(symbol), None)? {
                batch.resume = Some(exec);
                self.recovery.phase = Phase::Applying(Box::new(batch));
                return Ok(());
            }
            if let Err(reason) = self
                .books
                .orders
                .validate_fill(&exec.client_order_id, symbol, exec.side, exec.qty, exec.px)
                .and_then(|()| {
                    self.books.orders.validate_fill_quantities(
                        &exec.client_order_id,
                        exec.qty,
                        exec.amounts.as_ref(),
                    )
                })
                .and_then(|()| {
                    exec.amounts.as_ref().map_or(Ok(()), |values| {
                        values
                            .validate_projection(exec.qty, exec.px, exec.fee)
                            .map_err(|error| error.to_string())
                    })
                })
            {
                batch.foreign.push(Self::untrusted_fill_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    symbol,
                    exec.side,
                    exec.qty,
                    exec.px,
                    &reason,
                ));
                self.recovered_exec_ids.insert(exec.exec_id, now_ms);
                batch.recovered += 1;
                continue;
            }
            let mut record = WalRecord::RecoveredFill {
                callbacks: None,
                allocation: None,
                amounts: exec.amounts.clone(),
                exec_id: exec.exec_id.clone(),
                client_order_id: exec.client_order_id.clone(),
                symbol,
                side: exec.side,
                qty: exec.qty,
                px: exec.px,
                fee: exec.fee,
                is_maker: exec.is_maker,
                forced_close: exec.forced_close,
                venue_ts_ms: exec.venue_ts_ms,
                recovered_wall_ts_ms: now_ms,
            };
            let owner = self.books.orders.owner_of(&exec.client_order_id);
            let owned_request = self
                .books
                .orders
                .orders
                .get(&exec.client_order_id)
                .map(|order| order.request.clone());
            let allocation = match self.books.attribution.prepare_portfolio_recovered_on_grid(
                owned_request.as_ref(),
                &self.host.names,
                &record,
                self.instrument_specs
                    .get(&symbol)
                    .and_then(|spec| spec.qty_step.as_ref()),
            ) {
                Ok(allocation) => allocation,
                Err(reason) => {
                    batch.foreign.push(Self::untrusted_fill_line(
                        &exec.exec_id,
                        &exec.client_order_id,
                        symbol,
                        exec.side,
                        exec.qty,
                        exec.px,
                        &reason,
                    ));
                    self.recovered_exec_ids.insert(exec.exec_id, now_ms);
                    batch.recovered += 1;
                    continue;
                }
            };
            if let (
                Some(prepared),
                WalRecord::RecoveredFill {
                    allocation: recorded,
                    ..
                },
            ) = (&allocation, &mut record)
            {
                *recorded = Some(Box::new(prepared.allocation.clone()));
            }
            let owned = allocation.is_some();
            let recv_ns = clock::now_ns();
            let update = crate::portfolio_allocation::recovered_update(&record, recv_ns)
                .expect("recovered fill");
            let owners: Vec<StrategyId> = crate::portfolio_allocation::slice_updates(&update)
                .map_err(EngineError::State)?
                .map(|slices| slices.into_iter().map(|(owner, _)| owner).collect())
                .unwrap_or_else(|| owner.into_iter().collect());
            let callbacks = self.host.callbacks.recovering.then_some(owners);
            if let Some(owners) = &callbacks {
                self.ensure_callback_reader(&[])?;
                let WalRecord::RecoveredFill {
                    callbacks: recorded,
                    ..
                } = &mut record
                else {
                    unreachable!()
                };
                *recorded = Some(engine_types::wal::RecoveredCallbacks {
                    owners: owners.clone(),
                    recv_ns,
                });
            }
            let offset = self.wal.segment_size();
            let sequence = self.wal.append(&record)?;
            let recorded = record.records_allocation();
            if let Some(allocation) = allocation {
                self.books
                    .attribution
                    .commit_portfolio_fill(allocation, recorded)
                    .map_err(EngineError::State)?;
            }
            self.recovered_exec_ids.insert(exec.exec_id.clone(), now_ms);
            self.books
                .orders
                .try_apply(&record)
                .map_err(EngineError::State)?;
            self.portfolio_controls
                .apply(&record)
                .map_err(EngineError::State)?;
            self.portfolio_physical_after
                .insert(symbol, clock::now_ns());
            self.portfolio_controls
                .retain_native_offsets(&self.books.attribution.snapshot());
            if owned {
                reconcile::note_owned_fill(
                    &mut self.logged_exposure,
                    &mut self.intended_stops,
                    owned_request.as_ref(),
                    symbol,
                    exec.side,
                    &crate::portfolio_allocation::fill_quantity(
                        exec.qty,
                        exec.amounts.as_ref(),
                        match &record {
                            WalRecord::RecoveredFill { allocation, .. } => allocation.as_deref(),
                            _ => None,
                        },
                    )
                    .map_err(EngineError::State)?,
                )
                .map_err(EngineError::State)?;
                if let Some(request) = owned_request.as_ref() {
                    self.books.attribution.remember_order_stop(request);
                }
                // What it cost is the same question whichever way it arrived,
                // and the anchor is the book its own order left at.
                let late_ns = now_ms
                    .saturating_sub(exec.venue_ts_ms)
                    .max(0)
                    .saturating_mul(1_000_000) as u64;
                // Dated to when it traded, not to when it was found, or a
                // trade from minutes ago is marked against this minute's book
                // and the number is read as a one-second fact.
                let update =
                    crate::portfolio_allocation::recovered_update(&record, clock::now_ns())
                        .expect("recovered fill");
                let slices = crate::portfolio_allocation::slice_updates(&update)
                    .map_err(EngineError::State)?
                    .unwrap_or_else(|| {
                        owner
                            .map(|sid| vec![(sid, update.clone())])
                            .unwrap_or_default()
                    });
                for (sid, _) in slices {
                    let OrderUpdate::Fill {
                        qty,
                        fee,
                        amounts,
                        allocation,
                        ..
                    } = &update
                    else {
                        unreachable!()
                    };
                    let fill = execution::Fill {
                        amounts: amounts.clone(),
                        client_order_id: exec.client_order_id.clone(),
                        strategy: sid,
                        symbol,
                        side: exec.side,
                        qty: *qty,
                        px: exec.px,
                        fee: *fee,
                        is_maker: exec.is_maker,
                        arrival_mid: self.arrival_mid_of(&exec.client_order_id),
                        venue_ts_ms: exec.venue_ts_ms,
                    };
                    if let Some(allocation) = allocation {
                        self.fills
                            .on_allocated_fill(
                                &fill,
                                clock::now_ns().checked_sub(late_ns),
                                true,
                                allocation,
                            )
                            .map_err(EngineError::State)?;
                    } else {
                        self.fills
                            .on_recovered_fill_with_quantity(
                                &fill,
                                clock::now_ns().checked_sub(late_ns),
                                amounts.as_deref().map(|a| &a.quantity.value),
                            )
                            .map_err(EngineError::State)?;
                    }
                }
            } else {
                batch
                    .foreign
                    .push(Self::foreign_fill_line(&exec.client_order_id, symbol));
            }
            // The kernel reserved this order's size when it approved it, and
            // only a fill releases the reservation. Skipping it here leaves the
            // position counted twice — once as a reservation that never ends,
            // once in the account view — and every later entry judged against
            // the sum.
            self.update_risk_from_canonical_order(&OrderUpdate::Fill {
                allocation: None,
                amounts: exec.amounts.clone().map(Box::new),
                exec_id: exec.exec_id.clone(),
                client_order_id: exec.client_order_id.clone(),
                symbol,
                side: exec.side,
                qty: exec.qty,
                px: exec.px,
                fee: exec.fee,
                is_maker: exec.is_maker,
                forced_close: exec.forced_close,
                venue_ts_ms: exec.venue_ts_ms,
                // The engine's own clock, not the venue's: `recv_ns` is what
                // the kernel compares against the account view's stamp, and the
                // two must come from one clock.
                recv_ns: clock::now_ns(),
            })?;
            if self
                .books
                .orders
                .orders
                .get(&exec.client_order_id)
                .is_some_and(|order| !order.in_flight())
            {
                self.risk
                    .complete_order(&exec.client_order_id, clock::now_ns());
            }
            self.route_order_update(update, sequence, offset, callbacks.as_deref())?;
            batch.recovered += 1;
        }

        if !batch.foreign.is_empty() {
            batch.untrusted = true;
            self.latch_closed();
            record_latch(&mut self.wal, now_ms, std::mem::take(&mut batch.foreign))?;
        }
        if batch.resume.is_some() || !batch.rows.is_empty() {
            self.recovery.phase = Phase::Applying(Box::new(batch));
            return Ok(());
        }
        if batch.recovered > 0 {
            tracing::warn!(
                count = batch.recovered,
                "recovered fills from execution history"
            );
        }
        let account_matches = match &batch.account {
            Ok(account) => {
                super::account_recovery::history_account_matches(account, &self.logged_exposure)?
            }
            Err(_) => false,
        };
        let through_ms = if account_matches
            && !batch.untrusted
            && self.may_open
            && !self
                .books
                .orders
                .orders
                .values()
                .any(|order| order.in_flight())
            && self.dispatches.unresolved.is_empty()
            && self.dispatches.orders.is_empty()
        {
            now_ms
        } else {
            self.recovered_until_ms
        };
        if through_ms > self.recovered_until_ms {
            self.wal.append(&WalRecord::ExecutionHistoryCheckpoint {
                through_wall_ts_ms: through_ms,
            })?;
        }
        self.publish_history(batch.query, batch.account, Some(through_ms))
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;
    use crate::callback_recovery::host::{CallbackExecution, CallbackHost};

    #[tokio::test(start_paused = true)]
    async fn empty_or_unrelated_history_cannot_move_past_a_delayed_known_fill_across_restart() {
        for unrelated in [false, true] {
            let (mut engine, records) = crate::tests::recovery_inventory_fixture().await;
            let now = clock::wall_ms();
            engine.recovered_until_ms = now - 600_000;
            let before = engine.recovered_until_ms;
            let mut request = engine
                .books
                .orders
                .orders
                .values()
                .next()
                .unwrap()
                .request
                .clone();
            request.client_order_id = "delayed-known-exit".into();
            request.qty = 0.001;
            request.side = Side::Sell;
            request.reduce_only = true;
            request.stop = None;
            let sent = WalRecord::OrderSent {
                request,
                dispatch: None,
                arrival_mid: 30_000.0,
                wire_ns: clock::now_ns(),
            };
            engine.wal.append(&sent).unwrap();
            engine.books.orders.try_apply(&sent).unwrap();
            let delayed = engine_types::VenueExecution {
                exec_id: "late-known-fill".into(),
                client_order_id: "delayed-known-exit".into(),
                symbol: "BTCUSDT".into(),
                side: Side::Sell,
                qty: 0.001,
                px: 30_000.0,
                fee: Some(0.0),
                amounts: None,
                is_maker: false,
                forced_close: None,
                venue_ts_ms: now - 300_000,
            };
            let rows = if unrelated {
                vec![engine_types::VenueExecution {
                    symbol: "UNRELATEDUSDT".into(),
                    exec_id: "unrelated-history-fill".into(),
                    client_order_id: "manual-unrelated".into(),
                    venue_ts_ms: now - 1,
                    ..delayed.clone()
                }]
            } else {
                Vec::new()
            };
            let query = super::account_recovery::Query {
                started_ns: clock::now_ns(),
                generation: engine.recovery.generation,
                history: Some((before - RECOVERY_PAD_MS, now)),
            };
            engine
                .apply_history_batch(HistoryBatch {
                    resume: None,
                    untrusted: false,
                    query,
                    account: Ok(engine.account().clone()),
                    rows: engine_types::ExecutionHistory::from_rows(rows).unwrap(),
                    delivered: HashMap::new(),
                    recovered: 0,
                    foreign: Vec::new(),
                })
                .unwrap();
            let durable = engine.recovery.completed.recv().await.unwrap();
            engine.on_recovery_completion(durable).await.unwrap();
            assert_eq!(
                engine.recovered_until_ms, before,
                "a response with unrelated={unrelated} skipped an unresolved fill"
            );
            assert_eq!(
                engine.next_history_checkpoint_ms,
                now + HISTORY_CHECKPOINT_INTERVAL_MS
            );

            let base = engine.rotation_base(now);
            let since = execution_history_through_ms(std::slice::from_ref(&base)).unwrap()
                - RECOVERY_PAD_MS;
            let rows = [delayed]
                .into_iter()
                .filter(|row| row.venue_ts_ms >= since && row.venue_ts_ms <= now)
                .collect();
            let mut venue = crate::tests::recovery_venue_fixture(rows);
            let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
            let mut ids = ExecutionIds::from_records(std::slice::from_ref(&base), now).unwrap();
            let outcome = TestRecovery::recover_missed_fills(
                &mut engine.wal,
                &mut venue,
                std::slice::from_ref(&base),
                &crate::order_dispatch::OrderDispatches::replay(std::slice::from_ref(&base))
                    .unwrap(),
                &engine.books.market.table,
                &mut ids,
                now,
                &mut callbacks,
                &engine.books.account,
                &mut engine.risk,
                None,
            )
            .await
            .unwrap();
            assert_eq!(records.lock().unwrap().iter().filter(|record| matches!(record, WalRecord::RecoveredFill { exec_id, .. } if exec_id == "late-known-fill")).count(), 1);
            assert!(!outcome.orders.orders["delayed-known-exit"].in_flight());
        }
    }

    #[tokio::test(start_paused = true)]
    async fn quarantined_net_neutral_history_keeps_its_cursor_after_deduplication() {
        let (mut engine, _) = crate::tests::lifecycle_test_fixture(vec![]).await;
        let now = clock::wall_ms();
        let before = now - 10_000;
        engine.recovered_until_ms = before;
        let mut rows = Vec::new();
        for (side, exec_id) in [(Side::Buy, "unowned-buy"), (Side::Sell, "unowned-sell")] {
            engine
                .take_update(OrderUpdate::Fill {
                    client_order_id: "eng-unknown-order".into(),
                    exec_id: exec_id.into(),
                    symbol: SymbolId(0),
                    side,
                    qty: 1.0,
                    px: 100.0,
                    fee: Some(0.0),
                    amounts: None,
                    allocation: None,
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: now,
                    recv_ns: clock::now_ns(),
                })
                .await
                .unwrap();
            rows.push(engine_types::VenueExecution {
                client_order_id: "eng-unknown-order".into(),
                exec_id: exec_id.into(),
                symbol: "BTCUSDT".into(),
                side,
                qty: 1.0,
                px: 100.0,
                fee: Some(0.0),
                amounts: None,
                is_maker: false,
                forced_close: None,
                venue_ts_ms: now,
            });
        }
        assert!(!engine.may_open);
        engine
            .apply_history_batch(HistoryBatch {
                resume: None,
                untrusted: false,
                query: super::account_recovery::Query {
                    started_ns: clock::now_ns(),
                    generation: engine.recovery.generation,
                    history: Some((before, now)),
                },
                account: Ok(engine.account().clone()),
                rows: engine_types::ExecutionHistory::from_rows(rows).unwrap(),
                delivered: HashMap::new(),
                recovered: 0,
                foreign: Vec::new(),
            })
            .unwrap();
        let durable = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(durable).await.unwrap();
        assert_eq!(
            engine.recovered_until_ms, before,
            "deduplication hid an unresolved net-neutral pair from history completeness"
        );
    }

    type TestRecovery =
        Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;

    const TERMINAL_DISPATCH_ID: &str = "eng-terminal-dispatch-cut";

    async fn terminal_dispatch_crash_cut(
        rotated: bool,
    ) -> (TestRecovery, engine_wal::WalWriter, Vec<WalRecord>, i64) {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let passive = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, _) = crate::tests::callback_test_fixture(vec![passive]).await;
        assert_eq!(engine.host.strategies.len(), 1);
        assert_eq!(engine.books.market.table.get("BTCUSDT"), Some(SymbolId(0)));
        let now = clock::wall_ms();
        let before = now - 10_000;
        engine.recovered_until_ms = before;
        assert!(engine.may_open);
        assert!(engine.account().positions.is_empty());
        let request = OrderRequest {
            client_order_id: TERMINAL_DISPATCH_ID.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: 90.0 }),
            reduce_only: false,
            close_position: false,
            exact_terms: None,
            sleeve_effect: None,
        };
        let intent = Intent {
            exact_quantity: None,
            exact_prices: None,
            strategy: request.strategy,
            symbol: request.symbol,
            side: request.side,
            qty: request.qty,
            kind: request.kind,
            stop: request.stop,
            reduce_only: request.reduce_only,
            tag: "terminal-dispatch-cut".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        };
        let cut = vec![
            engine.rotation_base(now),
            WalRecord::OrderSent {
                request,
                dispatch: Some(Box::new(
                    engine_types::order_dispatch::QueuedOrderDispatch {
                        intent,
                        origin_ns: clock::now_ns(),
                    },
                )),
                wire_ns: clock::now_ns(),
                arrival_mid: 100.0,
            },
            WalRecord::OrderDispatchAttempted {
                client_order_id: TERMINAL_DISPATCH_ID.into(),
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Reject {
                    client_order_id: TERMINAL_DISPATCH_ID.into(),
                    code: 1,
                    reason: "crash before dispatch completion".into(),
                },
            },
        ];
        engine.books.orders = LedgerOfOrders::try_from_records(&cut).unwrap();
        engine.dispatches = crate::order_dispatch::OrderDispatches::replay(&cut).unwrap();
        let path = crate::testpath::temp_path("terminal-dispatch-history-cut");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for row in cut {
            wal.append(&row).unwrap();
        }
        wal.barrier().unwrap();
        if rotated {
            wal.rotate(&engine.rotation_base(now)).unwrap();
        }
        drop(wal);
        let (wal, records) = engine_wal::open_current(&path).unwrap();
        let records = records.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        engine.books.orders = LedgerOfOrders::try_from_records(&records).unwrap();
        engine.dispatches = crate::order_dispatch::OrderDispatches::replay(&records).unwrap();
        assert!(!engine.books.orders.orders[TERMINAL_DISPATCH_ID].in_flight());
        assert_eq!(engine.dispatches.orders.len(), 1);
        assert!(engine.dispatches.unresolved.is_empty());
        (engine, wal, records, before)
    }

    #[tokio::test(start_paused = true)]
    async fn boot_history_waits_for_terminal_dispatch_completion_across_restart() {
        for rotated in [false, true] {
            let (mut engine, mut wal, mut replay, before) =
                terminal_dispatch_crash_cut(rotated).await;
            let now = clock::wall_ms();
            let mut venue = crate::tests::recovery_venue_fixture(vec![]);
            let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
            let mut ids = ExecutionIds::from_records(&replay, now).unwrap();
            for completed in [false, true] {
                if completed {
                    let record = WalRecord::OrderDispatchCompleted {
                        client_order_id: TERMINAL_DISPATCH_ID.into(),
                    };
                    wal.append(&record).unwrap();
                    wal.barrier().unwrap();
                    replay.push(record);
                    engine.dispatches =
                        crate::order_dispatch::OrderDispatches::replay(&replay).unwrap();
                }
                let outcome = Engine::<
                    engine_wal::WalWriter,
                    crate::tests::MockRisk,
                    crate::tests::MockVenue,
                >::recover_missed_fills(
                    &mut wal,
                    &mut venue,
                    &replay,
                    &engine.dispatches,
                    &engine.books.market.table,
                    &mut ids,
                    now,
                    &mut callbacks,
                    &engine.books.account,
                    &mut engine.risk,
                    None,
                )
                .await
                .unwrap();
                if completed {
                    assert!(
                        outcome.through_ms >= now,
                        "completed dispatch prevented history progress after rotation={rotated}"
                    );
                } else {
                    assert_eq!(outcome.through_ms, before, "terminal order hid a pending durable dispatch at boot after rotation={rotated}");
                }
            }
        }
    }

    #[tokio::test(start_paused = true)]
    async fn runtime_history_waits_for_terminal_dispatch_completion_across_restart() {
        for rotated in [false, true] {
            let (mut engine, _wal, _replay, before) = terminal_dispatch_crash_cut(rotated).await;
            let now = clock::wall_ms();
            for completed in [false, true] {
                if completed {
                    engine
                        .complete_order_dispatch(TERMINAL_DISPATCH_ID)
                        .unwrap();
                    engine.wal.barrier().unwrap();
                    assert!(engine.dispatches.orders.is_empty());
                }
                engine
                    .apply_history_batch(HistoryBatch {
                        resume: None,
                        untrusted: false,
                        query: super::account_recovery::Query {
                            started_ns: clock::now_ns(),
                            generation: engine.recovery.generation,
                            history: Some((before, now)),
                        },
                        account: Ok(engine.account().clone()),
                        rows: engine_types::ExecutionHistory::from_rows(vec![]).unwrap(),
                        delivered: HashMap::new(),
                        recovered: 0,
                        foreign: Vec::new(),
                    })
                    .unwrap();
                let durable = engine.recovery.completed.recv().await.unwrap();
                engine.on_recovery_completion(durable).await.unwrap();
                if completed {
                    assert_eq!(engine.recovered_until_ms, now, "completed dispatch prevented runtime history progress after rotation={rotated}");
                } else {
                    assert_eq!(engine.recovered_until_ms, before, "terminal order hid a pending durable dispatch at runtime after rotation={rotated}");
                }
            }
        }
    }

    #[tokio::test(start_paused = true)]
    async fn history_batches_yield_to_private_fills_and_restart_an_uncheckpointed_prefix_once() {
        let (mut engine, records) = crate::tests::recovery_inventory_fixture().await;
        let now = clock::wall_ms();
        let base = engine.rotation_base(now);
        let rows: Vec<_> = (0..96)
            .map(|index| engine_types::VenueExecution {
                exec_id: format!("gap-partial-{index}"),
                client_order_id: String::new(),
                symbol: "BTCUSDT".into(),
                side: Side::Sell,
                qty: 0.0001,
                px: 29_000.0,
                fee: None,
                amounts: None,
                is_maker: false,
                forced_close: Some(engine_types::ForcedClose::StopLoss),
                venue_ts_ms: now - 1,
            })
            .collect();
        let query = super::account_recovery::Query {
            started_ns: clock::now_ns(),
            generation: engine.recovery.generation,
            history: Some((now - 100, now)),
        };
        let batch = HistoryBatch {
            resume: None,
            untrusted: false,
            query,
            account: Ok(engine.account().clone()),
            rows: engine_types::ExecutionHistory::from_rows(rows.clone()).unwrap(),
            delivered: HashMap::new(),
            recovered: 0,
            foreign: Vec::new(),
        };
        engine.apply_history_batch(batch).unwrap();
        let prefix: Vec<_> = records
            .lock()
            .unwrap()
            .iter()
            .filter(|row| matches!(row, WalRecord::RecoveredFill { .. }))
            .cloned()
            .collect();
        assert_eq!(
            prefix.len(),
            32,
            "history application monopolized the core beyond its cooperative batch"
        );
        assert!(engine.recovery.applying());
        let concurrent = &rows[35];
        engine
            .take_update(OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: concurrent.exec_id.clone(),
                client_order_id: String::new(),
                symbol: SymbolId(0),
                side: concurrent.side,
                qty: concurrent.qty,
                px: concurrent.px,
                fee: concurrent.fee,
                is_maker: false,
                forced_close: concurrent.forced_close,
                venue_ts_ms: concurrent.venue_ts_ms,
                recv_ns: clock::now_ns(),
            })
            .await
            .unwrap();
        while engine.recovery.applying() {
            engine.service_account_recovery().await.unwrap();
        }
        assert!(engine.recovery.uncommitted());
        assert_eq!(
            records
                .lock()
                .unwrap()
                .iter()
                .filter(|row| matches!(row, WalRecord::RecoveredFill { .. }))
                .count(),
            95
        );
        assert!(
            (engine.books.attribution.signed(StrategyId(0), SymbolId(0)) - 0.0004).abs() < 1e-14
        );
        let completion = engine.recovery.completed.recv().await.unwrap();
        engine.on_recovery_completion(completion).await.unwrap();

        let mut cut = vec![base];
        cut.extend(prefix);
        let path = crate::testpath::temp_path("history-prefix-restart");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for row in &cut {
            wal.append(row).unwrap();
        }
        wal.barrier().unwrap();
        drop(wal);
        let (mut wal, actual) = engine_wal::WalWriter::open(&path).unwrap();
        let actual: Vec<_> = actual.into_iter().map(|(_, row)| row).collect();
        let mut venue = crate::tests::recovery_venue_fixture(rows);
        let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
        let mut ids = ExecutionIds::from_records(&actual, now).unwrap();
        let outcome = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &actual,
            &crate::order_dispatch::OrderDispatches::replay(&actual).unwrap(),
            &engine.books.market.table,
            &mut ids,
            now,
            &mut callbacks,
            &engine.books.account,
            &mut engine.risk,
            None,
        )
        .await
        .unwrap();
        let persisted = engine_wal::replay(&path).unwrap();
        assert_eq!(
            persisted
                .iter()
                .skip(actual.len())
                .filter(|(_, row)| matches!(row, WalRecord::RecoveredFill { .. }))
                .count(),
            64,
            "restart skipped or repeated an already committed prefix execution"
        );
        let cut = persisted
            .into_iter()
            .map(|(_, row)| row)
            .collect::<Vec<_>>();
        assert_eq!(
            outcome.attribution.snapshot(),
            Attribution::try_from_records(&cut).unwrap().snapshot()
        );
        assert!(
            (Attribution::try_from_records(&cut)
                .unwrap()
                .signed(StrategyId(0), SymbolId(0))
                - 0.0004)
                .abs()
                < 1e-14
        );
    }
}
