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
            let Some(exec) = batch.rows.pop_front() else {
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
            let used = batch.delivered.entry(key).or_default();
            let same_delivered = *used < delivered;
            if same_delivered {
                *used += 1;
            }
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
            let allocation = match self
                .books
                .attribution
                .prepare_portfolio_recovered_for_order(
                    owned_request.as_ref(),
                    &self.host.names,
                    &record,
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
            let callbacks = self.host.callbacks.isolated().then_some(owners);
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
            let sequence = self.wal.append(&record)?;
            if let Some(allocation) = allocation {
                self.books
                    .attribution
                    .commit_portfolio_fill(allocation)
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
                    &reconcile::fill_quantity(exec.qty, exec.amounts.as_ref())
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
                    .unwrap_or_else(|| owner.map(|sid| vec![(sid, update)]).unwrap_or_default());
                for (sid, update) in slices {
                    let OrderUpdate::Fill {
                        qty, fee, amounts, ..
                    } = update
                    else {
                        unreachable!()
                    };
                    self.fills
                        .on_recovered_fill_with_quantity(
                            &execution::Fill {
                                client_order_id: exec.client_order_id.clone(),
                                strategy: sid,
                                symbol,
                                side: exec.side,
                                qty,
                                px: exec.px,
                                fee,
                                is_maker: exec.is_maker,
                                arrival_mid: self.arrival_mid_of(&exec.client_order_id),
                                venue_ts_ms: exec.venue_ts_ms,
                            },
                            clock::now_ns().checked_sub(late_ns),
                            amounts.as_deref().map(|a| &a.quantity.value),
                        )
                        .map_err(EngineError::State)?;
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
            self.route_order_update(update, sequence, callbacks.as_deref())?;
            batch.recovered += 1;
        }

        if !batch.rows.is_empty() {
            self.recovery.phase = Phase::Applying(Box::new(batch));
            return Ok(());
        }
        if batch.recovered > 0 {
            tracing::warn!(
                count = batch.recovered,
                "recovered fills from execution history"
            );
        }
        if !batch.foreign.is_empty() {
            self.may_open = false;
            self.wal.append(&WalRecord::Reconciled {
                wall_ts_ms: now_ms,
                findings: batch.foreign,
                may_open: false,
            })?;
        }
        self.wal.append(&WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: now_ms,
        })?;
        self.publish_history(batch.query, batch.account, Some(now_ms))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy_process::host::{CallbackExecution, CallbackHost};

    #[tokio::test]
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
            query,
            account: Ok(engine.account().clone()),
            rows: rows.clone().into(),
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
            &engine.books.market.table,
            &mut ids,
            now,
            &mut callbacks,
        )
        .await
        .unwrap();
        assert_eq!(
            outcome
                .records
                .iter()
                .filter(|row| matches!(row, WalRecord::RecoveredFill { .. }))
                .count(),
            64,
            "restart skipped or repeated an already committed prefix execution"
        );
        cut.extend(outcome.records);
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
