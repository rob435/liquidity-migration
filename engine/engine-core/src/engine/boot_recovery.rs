use super::*;

mod configuration;
mod inputs;
mod reservations;
use configuration::{restore_configuration, ConfiguredStrategies};
use inputs::{restore_strategy_inputs, RecoveredStrategyInputs};
use reservations::restore_order_reservations;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Come up: read the log back, say who we are in it, learn what the
    /// strategies want, then ask the venue for the instrument rules and the
    /// account before the first message is allowed in.
    ///
    /// Strategy plug names are acceptable for simple callers. Fleet assembly
    /// uses [`Engine::boot_as`] so logs and heartbeats carry sleeve names.
    pub async fn boot(
        settings: &EngineSection,
        config_sha256: &str,
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        Engine::boot_as(
            settings,
            config_sha256,
            wal,
            risk,
            venue,
            strategies,
            &[],
            replayed,
        )
        .await
    }

    /// The same, with each sleeve's own name from its config block.
    ///
    /// `sleeves[i]` names the strategy in position `i`; a short list, or an
    /// entry that is empty, falls back to that strategy's plug name. This is
    /// what goes in the log's id table and in the heartbeat, so `engine fills`
    /// can say which sleeve's trading cost what.
    #[allow(clippy::too_many_arguments)]
    pub async fn boot_as(
        settings: &EngineSection,
        config_sha256: &str,
        mut wal: W,
        mut risk: R,
        mut venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        if !(1..=crate::config::MAX_GROUP_FLUSH_MS).contains(&settings.group_flush_ms) {
            return Err(EngineError::Boot(format!(
                "group_flush_ms must be between 1 and {}",
                crate::config::MAX_GROUP_FLUSH_MS
            )));
        }
        // The order/attribution/exposure scans happen AFTER fill recovery
        // below, so a fill the venue saw while this process was down seeds
        // every accounting view the same way a delivered one would have.

        let boot_ms = clock::wall_ms();
        let ConfiguredStrategies {
            market,
            mut routing,
            names,
            mut subscriptions,
            signal_dependencies,
            initial_global_checkpoints,
        } = restore_configuration(&strategies, sleeves, replayed)?;
        wal.append(&WalRecord::Boot {
            version: ENGINE_VERSION.to_string(),
            config_sha256: config_sha256.to_string(),
            wall_ts_ms: boot_ms,
            commit: ENGINE_COMMIT.to_string(),
        })?;
        wal.append(&WalRecord::Note {
            source: "engine".into(),
            text: "live: orders are sent, each one gated by the risk kernel".to_string(),
        })?;
        // Say what the ids mean before any record uses one. Without this every
        // later line names a number, and a log read a week later cannot say
        // which coin an order was for.
        wal.append(&names_record(&names, &market))?;
        for state in initial_global_checkpoints.values() {
            wal.append(&WalRecord::StrategyGlobalCheckpoint {
                wall_ts_ms: boot_ms,
                strategy: state.strategy,
                checkpoint: state.checkpoint.clone(),
                provenance: None,
            })?;
        }
        if !initial_global_checkpoints.is_empty() {
            wal.barrier()?;
        }

        let mut rules = vec![None; market.table.len()];
        for (name, rule) in venue.instrument_rules().await? {
            if let Some(id) = market.table.get(&name) {
                rules[id.0 as usize] = Some(rule);
            }
        }
        let mut missing: Vec<&str> = Vec::new();
        for subscription in &subscriptions {
            let Some(id) = market.table.get(&subscription.symbol) else {
                continue;
            };
            if rules[id.0 as usize].is_none() && !missing.contains(&subscription.symbol.as_str()) {
                missing.push(subscription.symbol.as_str());
            }
        }
        if !missing.is_empty() {
            return Err(EngineError::Boot(format!(
                "venue returned no instrument rules for configured symbols: {}",
                missing.join(", ")
            )));
        }

        let account = venue.account_view().await?;
        risk.observe_account_view(&account);

        // Fills the venue saw and this log never heard: a stop that fired
        // during a deploy window, an execution inside a private-stream gap.
        // Recovered from the venue's own history and made durable before the
        // log is compared to the venue, so what actually traded is a fill in
        // the log rather than a finding against it.
        let mut recovered_exec_ids = ExecutionIds::from_records(replayed, boot_ms)
            .map_err(|e| EngineError::State(e.to_string()))?;
        let recovery = Self::recover_missed_fills(
            &mut wal,
            &mut venue,
            replayed,
            &market.table,
            &mut recovered_exec_ids,
            boot_ms,
        )
        .await?;
        let effective_owned: Vec<WalRecord>;
        let effective: &[WalRecord] = if recovery.records.is_empty() {
            replayed
        } else {
            effective_owned = replayed.iter().cloned().chain(recovery.records).collect();
            &effective_owned
        };

        let mut orders = LedgerOfOrders::from_records(effective);
        // Same records, same join: a restart must not forget whose
        // position is whose, or the other sleeve trades straight into it.
        let mut attribution = Attribution::from_records(effective);
        let mut fills = Fills::default();
        let already_closed = fills.seed_lots(effective);
        // The rolling loss window, put back before anything can be assessed
        // against it. The order is a contract with the kernel: the newest
        // restatement SETS the window, this segment's own closes go on top,
        // and the clock then drops whatever is older than the window.
        if let Some(rows) = effective.iter().rev().find_map(|record| match record {
            WalRecord::SegmentBase {
                rolling_loss_rows, ..
            } => Some(rolling_loss_rows),
            _ => None,
        }) {
            risk.restore_rolling_loss_rows(rows);
        }
        for trade in &already_closed {
            if let Some(round_trip) = &trade.round_trip {
                risk.observe_closed_trade(engine_types::risk::ClosedTradeRow {
                    closed_ms: trade.closed_ms,
                    net_usdt: round_trip.net_usdt,
                });
            }
        }
        risk.observe_wall_clock_ms(clock::wall_ms());
        // Seeded by the same scans reconcile trusts and kept live from here
        // on, because a rotation restates them into the new segment's first
        // record and must say exactly what a replay would have said.
        let logged_exposure = crate::reconcile::logged_exposure(effective);
        let intended_stops = crate::reconcile::intended_stops(effective);
        let RecoveredStrategyInputs {
            strategy_checkpoints,
            strategy_global_checkpoints,
            strategy_events,
            signals,
            runtime_control_requests,
            runtime_control_consumed,
            runtime_entries_enabled,
            routes,
        } = restore_strategy_inputs(
            effective,
            &strategies,
            &names,
            &market.table,
            initial_global_checkpoints,
        )?;
        for (symbol, destination, subscription) in routes {
            routing.add(symbol, subscription.feed, destination);
            if !subscriptions.contains(&subscription) {
                subscriptions.push(subscription);
            }
        }
        // A gap-recovery pass reaches back past this boot, and the venue hands
        // back everything in that window — the last run's ordinary fills
        // included. A delivered fill carries no venue execution id, so only
        // this can tell the pass it already has one.
        let mut recent_fills: VecDeque<(String, i64, f64)> = effective
            .iter()
            .filter_map(|record| match record {
                WalRecord::OrderUpdate {
                    update:
                        OrderUpdate::Fill {
                            client_order_id,
                            venue_ts_ms,
                            qty,
                            ..
                        },
                } => Some((client_order_id.clone(), *venue_ts_ms, *qty)),
                _ => None,
            })
            .collect();
        while recent_fills.len() > RECENT_FILLS_KEPT {
            recent_fills.pop_front();
        }

        // What the log believes against what the venue says. Boot is the one
        // moment the two can be compared: from here on the engine only ever
        // learns about its own orders.
        let (may_open, vanished) = Self::reconcile_with_venue(
            &mut wal,
            &mut venue,
            &orders,
            effective,
            &account,
            &market.table,
            &rules,
        )
        .await?;

        // An order the log shows in flight that the venue is not working
        // ended while the engine was down, and no update for it will ever
        // arrive. Left "in flight" it would charge the kernel's partition on
        // every future boot and hold the one-order-per-symbol gate closed
        // against that symbol — exits included — until somebody hand-fixed
        // the venue. The venue's own working-order listing is evidence, not
        // a guess, so the ending is written down as what it was.
        for client_order_id in vanished {
            tracing::warn!(
                id = %client_order_id,
                "this order ended while the engine was down; recording the ending"
            );
            let ended = WalRecord::OrderUpdate {
                update: OrderUpdate::Cancelled {
                    client_order_id,
                    recv_ns: clock::now_ns(),
                },
            };
            wal.append(&ended)?;
            orders.apply(&ended);
        }
        let recovered = orders.in_flight().len();

        // A sleeve's claim on a symbol the venue holds nothing of is a close
        // this log never got to charge (a hand close, an inherited position
        // wound down), and it would lock every other sleeve out of the name
        // for good. The venue reading is the authority on what is
        // held, so flat clears the claim; a symbol with an order still in
        // flight is left alone.
        let in_flight_symbols: std::collections::HashSet<SymbolId> = orders
            .in_flight()
            .iter()
            .map(|order| order.request.symbol)
            .collect();
        let stale_claims = attribution.drop_where_flat(|symbol| {
            !in_flight_symbols.contains(&symbol)
                && !account
                    .positions
                    .iter()
                    .any(|p| p.symbol == symbol && p.qty > 0.0)
        });
        if !stale_claims.is_empty() {
            let words = stale_claims
                .iter()
                .map(|(strategy, symbol, qty)| {
                    format!(
                        "{} {} {qty}",
                        names
                            .get(strategy.0 as usize)
                            .map(String::as_str)
                            .unwrap_or("unknown"),
                        market.table.name(*symbol)
                    )
                })
                .collect::<Vec<_>>()
                .join(", ");
            tracing::warn!(
                claims = %words,
                "dropping sleeve claims on symbols the venue holds nothing of"
            );
            // Durable, not a note: a later boot replays the drop instead of
            // rebuilding the residue from the old fills — by then another
            // sleeve may hold the symbol, and a venue no longer flat would
            // make the residue undroppable.
            // The same names, out of the position accounting too: a claim the
            // venue does not back has no exit price, so its trip cannot be
            // reported and must not sit waiting for one.
            let gone: std::collections::HashSet<(String, String)> = stale_claims
                .iter()
                .map(|(strategy, symbol, _)| {
                    (
                        names.get(strategy.0 as usize).cloned().unwrap_or_default(),
                        market.table.name(*symbol).to_string(),
                    )
                })
                .collect();
            fills.lots().drop_symbols(|sleeve, symbol| {
                gone.contains(&(sleeve.to_string(), symbol.to_string()))
            });
            wal.append(&WalRecord::ClaimsDropped {
                wall_ts_ms: clock::wall_ms(),
                rows: stale_claims
                    .iter()
                    .map(|(strategy, symbol, qty)| engine_types::FilledTotal {
                        strategy: *strategy,
                        symbol: *symbol,
                        signed_qty: *qty,
                    })
                    .collect(),
            })?;
            wal.barrier()?;
        }

        // Nothing is lost by the rounding `boot_prefix` does: the stamp only
        // separates one boot's ids from another's, and `mint_unused` already
        // refuses any id the replayed log has seen.
        let registry = restore_order_reservations(&mut risk, &orders, boot_ms)?;
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                ids = ?orders.in_flight_ids(),
                "orders were in flight when the engine last stopped; they are not re-sent"
            );
        }

        let now = clock::now_ns();
        let (venue, venue_completions) = VenueClient::spawn(venue);
        let mut engine = Engine {
            refusals: HashMap::new(),
            wal,
            risk,
            venue,
            venue_completions,
            pending_mutations: HashMap::new(),
            busy_symbols: HashMap::new(),
            deferred_actions: HashMap::new(),
            ready_actions: VecDeque::new(),
            _venue: std::marker::PhantomData,
            host: StrategyHost {
                effects: crate::effects::Effects::replay(replayed, names.len())
                    .map_err(EngineError::Boot)?,
                strategies,
                names,
                timers: Timers::default(),
                pending: VecDeque::new(),
                checkpoints: strategy_checkpoints,
                global_checkpoints: strategy_global_checkpoints,
                events: strategy_events,
                entries_enabled: runtime_entries_enabled,
            },
            books: Books {
                market,
                account,
                rules,
                orders,
                registry,
                attribution,
                // Empty on purpose: boot compares the log against the venue
                // directly, which is a better answer than a memory of what was
                // in flight.
                covers: CoverBook::default(),
            },
            routing,
            drain_progress: None,
            signals,
            signal_dependencies,
            runtime_control_requests,
            runtime_control_consumed,
            pending_signal_deliveries: VecDeque::new(),
            // Deliberately not restored from the log. The window is measured
            // from a monotonic clock that does not survive a restart, and the
            // venue's own creation time is not something this engine can ask
            // for — so a recovered order is left alone rather than worked
            // from a made-up deadline.
            working: WorkingOrders::default(),
            halt_cancels: std::collections::HashMap::new(),
            amends_awaiting_price: HashMap::new(),
            amends_confirmed: 0,
            amends_pulled_unconfirmed: 0,
            stream_resets: 0,
            pending_barrier: None,
            halt_cancel_queue: VecDeque::new(),
            wanted_symbols: Vec::new(),
            leverage_at: std::collections::HashMap::new(),
            may_open,
            private_stream_ready: true,
            logged_exposure,
            intended_stops,
            confirmed_stop_moves: std::collections::BTreeMap::new(),
            recovered_until_ms: recovery.through_ms,
            next_history_checkpoint_ms: recovery
                .through_ms
                .saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS),
            recovered_exec_ids,
            recent_fills,
            ledger: LatencyLedger::new(now),
            // Its cost rows are a running score for the run in front of you,
            // and the whole history is one `engine fills` away; its open
            // positions were rebuilt above, because a close priced without
            // its entry is a number about nothing.
            fills,
            heartbeat: None,
            trades: None,
            leverage_authority: settings.leverage_authority,
            group_flush: Duration::from_millis(settings.group_flush_ms.max(1)),
            refresh_after_ns: settings.account_view_max_age_ms.saturating_mul(1_000_000) / 2,
            rotate_after_bytes: settings.wal_rotate_mb.saturating_mul(1024 * 1024),
            max_quote_age_ns: settings.max_quote_age_ms.saturating_mul(1_000_000),
            next_order_n: 0,
            orders_sent: 0,
            events_seen: 0,
            subscriptions,
        };
        engine
            .fills
            .learn(&names_record(&engine.host.names, &engine.books.market));
        engine.restore_strategy_effects().await?;
        engine.wake_restored_strategies()?;
        engine.redeliver_durable_strategy_inputs();
        engine.queue_halted_entry_cancels()?;
        Ok(engine)
    }

    /// Ask the venue what traded on this account since the log's newest
    /// stamp, and write down every execution the log has never seen.
    ///
    /// Success is durable before the reconcile that would otherwise have
    /// read what actually traded as somebody else's trading. Failure aborts
    /// boot: without the missing interval the log cannot prove its exposure.
    async fn recover_missed_fills(
        wal: &mut W,
        venue: &mut V,
        replayed: &[WalRecord],
        table: &SymbolTable,
        execution_ids: &mut ExecutionIds,
        fresh_start_ms: i64,
    ) -> Result<RecoveryOutcome, EngineError> {
        let now_ms = clock::wall_ms();
        let newest = match execution_history_through_ms(replayed) {
            Some(stamp) => stamp,
            None if replayed.is_empty() => fresh_start_ms,
            None => {
                return Err(EngineError::Boot(
                    "the existing log has no durable execution-history boundary".to_string(),
                ))
            }
        };
        let since = newest.saturating_sub(RECOVERY_PAD_MS);
        if since < now_ms - RECOVERY_REACH_MS {
            return Err(EngineError::Boot(format!(
                "the log is {} ms behind, beyond the venue execution-history reach of {} ms",
                now_ms - newest,
                RECOVERY_REACH_MS
            )));
        }
        if since >= now_ms {
            return Ok(RecoveryOutcome {
                records: Vec::new(),
                through_ms: newest,
            });
        }
        let mut execs = venue.executions(since, now_ms).await.map_err(|e| {
            EngineError::Boot(format!(
                "cannot read execution history for the recovery interval: {e}"
            ))
        })?;
        let mut delivered: std::collections::HashMap<(String, i64, u64), usize> =
            std::collections::HashMap::new();
        for record in replayed {
            if let WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        client_order_id,
                        venue_ts_ms,
                        qty,
                        ..
                    },
            } = record
            {
                if exec_id.is_empty() && *venue_ts_ms >= since {
                    *delivered
                        .entry((client_order_id.clone(), *venue_ts_ms, qty.to_bits()))
                        .or_default() += 1;
                }
            }
        }
        execs.sort_by_key(|exec| exec.venue_ts_ms);
        let mut out = Vec::new();
        let mut recovered = 0usize;
        let mut unknown_findings = Vec::new();
        let mut recovered_orders = LedgerOfOrders::from_records(replayed);
        let mut recovered_attribution = Attribution::from_records(replayed);
        for exec in execs {
            if execution_ids.contains(&exec.exec_id, now_ms) {
                continue;
            }
            let key = (
                exec.client_order_id.clone(),
                exec.venue_ts_ms,
                exec.qty.to_bits(),
            );
            let same_delivered = delivered.get_mut(&key).is_some_and(|count| {
                if *count == 0 {
                    false
                } else {
                    *count -= 1;
                    true
                }
            });
            if same_delivered {
                continue;
            }
            execution_ids
                .can_insert(&exec.exec_id, now_ms)
                .map_err(|e| EngineError::State(e.to_string()))?;
            let Some(symbol) = table.get(&exec.symbol) else {
                // The configured symbol table cannot safely absorb this
                // quantity, but silently dropping it would make a foreign
                // round trip invisible whenever the final account is flat.
                let finding = Self::foreign_unmapped_execution_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    &exec.symbol,
                    exec.qty,
                );
                let note = WalRecord::Note {
                    source: "fill-recovery".into(),
                    text: finding.clone(),
                };
                wal.append(&note)?;
                execution_ids.insert(exec.exec_id, now_ms);
                out.push(note);
                unknown_findings.push(finding);
                continue;
            };
            let dedup_id = exec.exec_id.clone();
            if let Err(reason) = recovered_orders.validate_fill(
                &exec.client_order_id,
                symbol,
                exec.side,
                exec.qty,
                exec.px,
            ) {
                unknown_findings.push(Self::untrusted_fill_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    symbol,
                    exec.side,
                    exec.qty,
                    exec.px,
                    &reason,
                ));
                execution_ids.insert(dedup_id, now_ms);
                recovered += 1;
                continue;
            }
            let client_order_id = exec.client_order_id.clone();
            let record = WalRecord::RecoveredFill {
                exec_id: exec.exec_id,
                client_order_id: exec.client_order_id,
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
            wal.append(&record)?;
            out.push(record.clone());
            execution_ids.insert(dedup_id, now_ms);
            let owner = recovered_orders.owner_of(&client_order_id).or_else(|| {
                attribution::forced_close_owner(
                    &recovered_attribution,
                    &client_order_id,
                    symbol,
                    exec.side,
                    exec.forced_close,
                )
            });
            recovered_orders.apply(&record);
            if let Some(owner) = owner {
                recovered_attribution.note(owner, symbol, exec.side, exec.qty);
            }
            recovered += 1;
        }
        if !unknown_findings.is_empty() {
            let latch = WalRecord::Reconciled {
                wall_ts_ms: now_ms,
                findings: unknown_findings,
                may_open: false,
            };
            wal.append(&latch)?;
            out.push(latch);
        }
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                "recovered fills the private stream never delivered"
            );
        }
        let checkpoint = WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: now_ms,
        };
        wal.append(&checkpoint)?;
        out.push(checkpoint);
        wal.barrier()?;
        Ok(RecoveryOutcome {
            records: out,
            through_ms: now_ms,
        })
    }

    /// Compare the log against the venue, write down what was found, and say
    /// whether the engine may open new exposure.
    ///
    /// The latch is durable. If an earlier boot found something it could not
    /// explain and stopped opening, this one starts stopped too — a restart
    /// that cleared it would turn "stop and tell somebody" into "stop until
    /// the next crash", which is no protection at all on a process that gets
    /// restarted by a supervisor.
    ///
    /// Nothing here cancels anything. An order the engine did not place is
    /// not its to take down, and a position it cannot account for is not its
    /// to close. It says so, repairs the stops it has evidence for, and
    /// stops adding.
    #[allow(clippy::too_many_arguments)]
    async fn reconcile_with_venue(
        wal: &mut W,
        venue: &mut V,
        orders: &LedgerOfOrders,
        replayed: &[WalRecord],
        account: &AccountView,
        table: &SymbolTable,
        rules: &[Option<InstrumentRule>],
    ) -> Result<(bool, Vec<String>), EngineError> {
        let latched = replayed.iter().rev().find_map(|record| match record {
            WalRecord::Reconciled { may_open, .. } => Some(*may_open),
            // A rotation restated the latch; nothing between it and the end
            // of the log has said otherwise or the scan would have stopped
            // there first.
            WalRecord::SegmentBase { may_open, .. } => Some(*may_open),
            // An operator ran `reconcile-clear`: the deliberate look the
            // latch waits for. It resets the memory, not the check — the
            // comparison below still latches again on anything that stands.
            WalRecord::LatchCleared { .. } => Some(true),
            _ => None,
        });

        let working = match venue.working_orders().await {
            Ok(rows) => rows,
            Err(e) => {
                // Not knowing what the venue is working is exactly the state
                // this check exists to catch, so it is not something to
                // shrug at and carry on from.
                return Err(EngineError::Boot(format!(
                    "cannot read what the venue is working, so there is no way to tell \
                     whose orders are out there: {e}"
                )));
            }
        };

        let found = reconcile::reconcile(
            orders,
            replayed,
            &working,
            account,
            |name| table.get(name),
            |id| {
                rules
                    .get(id.0 as usize)
                    .and_then(|r| r.as_ref())
                    .map(|r| r.qty_step)
            },
            |id| {
                rules
                    .get(id.0 as usize)
                    .and_then(|r| r.as_ref())
                    .map(|r| r.tick_size)
            },
        );

        let mut finding_lines = found.lines();
        for line in &finding_lines {
            tracing::warn!(finding = %line, "reconciliation");
        }

        // A stop the log says belongs somewhere, that the venue does not have.
        // Putting it back is the one repair the engine can make from evidence
        // rather than from a guess.
        let mut repair_failed = false;
        for (symbol, trigger_px) in found.stop_repairs() {
            match venue.set_stop(symbol, trigger_px).await {
                Ok(()) => tracing::info!(
                    symbol = table.name(symbol),
                    trigger_px,
                    "restored the fill-owned durable position stop"
                ),
                Err(e) => {
                    repair_failed = true;
                    let line = format!(
                        "{}: failed to restore durable stop {trigger_px}: {e}",
                        table.name(symbol)
                    );
                    tracing::error!(
                        symbol = table.name(symbol),
                        trigger_px,
                        error = %e,
                        "could not put the stop back; opening remains latched off"
                    );
                    finding_lines.push(line);
                }
            }
        }

        let may_open = latched.unwrap_or(true) && !found.must_not_open() && !repair_failed;
        if latched == Some(false) && !found.must_not_open() {
            tracing::error!(
                "an earlier boot stopped this engine opening new positions and nothing here \
                 clears that; it will reduce only until somebody looks at the log"
            );
        }
        if !may_open {
            tracing::error!(
                "this engine will not open new positions: the account holds orders or exposure \
                 its own log cannot account for"
            );
        }

        wal.append(&WalRecord::Reconciled {
            wall_ts_ms: clock::wall_ms(),
            findings: finding_lines,
            may_open,
        })?;
        // Durable before trading starts: a crash between here and the first
        // order must not lose a latch that was just set.
        wal.barrier()?;
        Ok((may_open, found.vanished()))
    }

    pub(super) async fn checkpoint_history_if_due(&mut self) -> Result<(), EngineError> {
        if clock::wall_ms() < self.next_history_checkpoint_ms {
            return Ok(());
        }
        self.renew_execution_history().await
    }

    pub(crate) async fn renew_execution_history(&mut self) -> Result<(), EngineError> {
        self.recover_history("while renewing the durable execution-history checkpoint")
            .await
    }

    /// After a private-stream gap: ask the venue what traded while the stream
    /// was away. The same pass also renews the quiet-run checkpoint.
    pub(super) async fn recover_gap_fills(&mut self) -> Result<(), EngineError> {
        self.recover_history("after a private-stream gap").await
    }

    /// Fold one complete execution-history interval through the ordinary fill
    /// books, then write its boundary after every returned row. Failure stops
    /// the run: advancing without the read would make the next boot trust a
    /// hole, and continuing until the venue forgets it would make repair
    /// impossible.
    async fn recover_history(&mut self, context: &str) -> Result<(), EngineError> {
        let now_ms = clock::wall_ms();
        let since = (self.recovered_until_ms - RECOVERY_PAD_MS).max(now_ms - RECOVERY_REACH_MS);
        if since >= now_ms {
            self.next_history_checkpoint_ms = now_ms.saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS);
            return Ok(());
        }
        let mut execs = match self.venue.executions(since, now_ms).await {
            Ok(execs) => execs,
            Err(error) => {
                self.may_open = false;
                self.wal.append(&WalRecord::Reconciled {
                    wall_ts_ms: now_ms,
                    findings: vec![format!(
                        "execution history is unavailable {context}: {error}"
                    )],
                    may_open: false,
                })?;
                self.wal.barrier()?;
                return Err(EngineError::Venue(error));
            }
        };
        execs.sort_by_key(|exec| exec.venue_ts_ms);
        let mut delivered_counts: std::collections::HashMap<(String, i64, u64), usize> =
            std::collections::HashMap::new();
        for (id, ts, qty) in &self.recent_fills {
            *delivered_counts
                .entry((id.clone(), *ts, qty.to_bits()))
                .or_default() += 1;
        }
        let mut recovered = 0usize;
        let mut foreign = Vec::new();
        for exec in execs {
            if self.recovered_exec_ids.contains(&exec.exec_id, now_ms) {
                continue;
            }
            let key = (
                exec.client_order_id.clone(),
                exec.venue_ts_ms,
                exec.qty.to_bits(),
            );
            let same_delivered = delivered_counts.get_mut(&key).is_some_and(|count| {
                if *count == 0 {
                    false
                } else {
                    *count -= 1;
                    true
                }
            });
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
                foreign.push(finding);
                recovered += 1;
                continue;
            };
            if let Err(reason) = self.books.orders.validate_fill(
                &exec.client_order_id,
                symbol,
                exec.side,
                exec.qty,
                exec.px,
            ) {
                foreign.push(Self::untrusted_fill_line(
                    &exec.exec_id,
                    &exec.client_order_id,
                    symbol,
                    exec.side,
                    exec.qty,
                    exec.px,
                    &reason,
                ));
                self.recovered_exec_ids.insert(exec.exec_id, now_ms);
                recovered += 1;
                continue;
            }
            let record = WalRecord::RecoveredFill {
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
            self.wal.append(&record)?;
            self.recovered_exec_ids.insert(exec.exec_id.clone(), now_ms);
            // The order ledger first; then, for a close the venue itself
            // started, the position it reduced.
            let owner = self
                .books
                .orders
                .owner_of(&exec.client_order_id)
                .or_else(|| {
                    attribution::forced_close_owner(
                        &self.books.attribution,
                        &exec.client_order_id,
                        symbol,
                        exec.side,
                        exec.forced_close,
                    )
                });
            let owned_request = self
                .books
                .orders
                .orders
                .get(&exec.client_order_id)
                .map(|order| order.request.clone());
            self.books.orders.apply(&record);
            if let Some(sid) = owner {
                reconcile::note_owned_fill(
                    &mut self.logged_exposure,
                    &mut self.intended_stops,
                    owned_request.as_ref(),
                    symbol,
                    exec.side,
                    exec.qty,
                );
                self.books
                    .attribution
                    .note(sid, symbol, exec.side, exec.qty);
                // What it cost is the same question whichever way it arrived,
                // and the anchor is the book its own order left at.
                let late_ns = now_ms
                    .saturating_sub(exec.venue_ts_ms)
                    .max(0)
                    .saturating_mul(1_000_000) as u64;
                // Dated to when it traded, not to when it was found, or a
                // trade from minutes ago is marked against this minute's book
                // and the number is read as a one-second fact.
                self.fills.on_recovered_fill(
                    &execution::Fill {
                        client_order_id: exec.client_order_id.clone(),
                        strategy: sid,
                        symbol,
                        side: exec.side,
                        qty: exec.qty,
                        px: exec.px,
                        fee: exec.fee,
                        is_maker: exec.is_maker,
                        arrival_mid: self.arrival_mid_of(&exec.client_order_id),
                        venue_ts_ms: exec.venue_ts_ms,
                    },
                    clock::now_ns().checked_sub(late_ns),
                );
            } else {
                foreign.push(Self::foreign_fill_line(&exec.client_order_id, symbol));
            }
            // The kernel reserved this order's size when it approved it, and
            // only a fill releases the reservation. Skipping it here leaves the
            // position counted twice — once as a reservation that never ends,
            // once in the account view — and every later entry judged against
            // the sum.
            self.risk.on_update(&OrderUpdate::Fill {
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
            });
            recovered += 1;
        }
        if recovered > 0 {
            tracing::warn!(count = recovered, "recovered fills from execution history");
        }
        if !foreign.is_empty() {
            self.may_open = false;
            self.wal.append(&WalRecord::Reconciled {
                wall_ts_ms: now_ms,
                findings: foreign,
                may_open: false,
            })?;
        }
        self.wal.append(&WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: now_ms,
        })?;
        self.wal.barrier()?;
        self.recovered_until_ms = now_ms;
        self.next_history_checkpoint_ms = now_ms.saturating_add(HISTORY_CHECKPOINT_INTERVAL_MS);
        Ok(())
    }

    pub(super) fn foreign_fill_line(client_order_id: &str, symbol: SymbolId) -> String {
        format!(
            "symbol {}: a fill names an order this engine did not send ({})",
            symbol.0,
            if client_order_id.is_empty() {
                "blank client id"
            } else {
                client_order_id
            }
        )
    }

    fn foreign_unmapped_execution_line(
        exec_id: &str,
        client_order_id: &str,
        symbol: &str,
        qty: f64,
    ) -> String {
        format!(
            "venue symbol {symbol}: execution {} for quantity {qty} cannot be mapped to the configured symbol table (order {})",
            if exec_id.is_empty() { "<blank>" } else { exec_id },
            if client_order_id.is_empty() {
                "<blank>"
            } else {
                client_order_id
            }
        )
    }

    pub(super) fn untrusted_fill_line(
        exec_id: &str,
        client_order_id: &str,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        reason: &str,
    ) -> String {
        format!(
            "symbol {}: an untrusted fill for order {} (execution {}, side {side:?}, quantity {qty}, price {px}) was not applied: {reason}",
            symbol.0,
            if client_order_id.is_empty() {
                "<blank>"
            } else {
                client_order_id
            },
            if exec_id.is_empty() { "<blank>" } else { exec_id },
        )
    }
}
