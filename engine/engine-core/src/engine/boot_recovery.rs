use super::*;

mod configuration;
mod inputs;
mod reservations;
use configuration::{restore_configuration, ConfiguredStrategies};
use inputs::{restore_strategy_inputs, RecoveredStrategyInputs};
use reservations::restore_order_reservations;

fn recent_legacy_fills(records: &[WalRecord]) -> VecDeque<(String, i64, f64)> {
    let mut recent: VecDeque<_> = records
        .iter()
        .rev()
        .filter_map(|record| match record {
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        client_order_id,
                        venue_ts_ms,
                        qty,
                        ..
                    },
                ..
            } if exec_id.is_empty() => Some((client_order_id.clone(), *venue_ts_ms, *qty)),
            _ => None,
        })
        .take(RECENT_FILLS_KEPT)
        .collect();
    recent.make_contiguous().reverse();
    recent
}

type LegacyOverlapCounts<'a> =
    std::collections::HashMap<&'a str, std::collections::HashMap<(i64, u64), usize>>;

fn legacy_overlap_counts(records: &[WalRecord], since: i64) -> LegacyOverlapCounts<'_> {
    let mut counts = LegacyOverlapCounts::new();
    for record in records {
        if let WalRecord::OrderUpdate {
            update:
                OrderUpdate::Fill {
                    exec_id,
                    client_order_id,
                    venue_ts_ms,
                    qty,
                    ..
                },
            ..
        } = record
        {
            if exec_id.is_empty() && *venue_ts_ms >= since {
                *counts
                    .entry(client_order_id.as_str())
                    .or_default()
                    .entry((*venue_ts_ms, qty.to_bits()))
                    .or_default() += 1;
            }
        }
    }
    counts
}

pub(super) struct RecoveryOutcome {
    pub(super) orders: LedgerOfOrders,
    pub(super) attribution: Attribution,
    fills: Fills,
    portfolio_controls: crate::portfolio_control::PortfolioControls,
    physical: reconcile::PhysicalExposure,
    intended: BTreeMap<SymbolId, reconcile::IntendedPositionStop>,
    latched: bool,
    pub(super) through_ms: i64,
}

impl RecoveryOutcome {
    #[cfg(test)]
    fn replay<R: RiskKernel>(
        records: &[WalRecord],
        risk: &mut R,
        through_ms: i64,
    ) -> Result<Self, EngineError> {
        let mut state = Self::prepare(records, None, through_ms)?;
        state.seed_loss(records, risk);
        Ok(state)
    }

    fn seed_loss<R: RiskKernel>(&mut self, records: &[WalRecord], risk: &mut R) {
        if let Some(rows) = records.iter().rev().find_map(|record| match record {
            WalRecord::SegmentBase {
                rolling_loss_rows, ..
            } => Some(rolling_loss_rows.as_slice()),
            _ => None,
        }) {
            risk.restore_rolling_loss_rows(rows);
        }
        for trade in self.fills.take_closed() {
            if let Some(row) = trade.loss_row() {
                risk.observe_closed_trade(row);
            }
        }
    }

    fn prepare(
        records: &[WalRecord],
        pending: Option<&WalRecord>,
        through_ms: i64,
    ) -> Result<Self, EngineError> {
        let (physical, intended, _, _) =
            reconcile::position_state_with_adoption(records, pending, false)
                .map_err(EngineError::Boot)?;
        Ok(Self {
            orders: LedgerOfOrders::try_from_records(records).map_err(EngineError::Boot)?,
            attribution: crate::legacy_quantity::Replay::new(records, pending)
                .and_then(|replay| replay.finish())
                .map_err(EngineError::Boot)?,
            fills: Fills::recovery_lots(records, pending).map_err(EngineError::Boot)?,
            portfolio_controls: crate::portfolio_control::PortfolioControls::replay(records)
                .map_err(EngineError::Boot)?,
            physical,
            intended,
            latched: false,
            through_ms,
        })
    }

    fn reject<W: Wal>(
        &mut self,
        wal: &mut W,
        now_ms: i64,
        finding: String,
    ) -> Result<(), EngineError> {
        wal.append(&WalRecord::Reconciled {
            wall_ts_ms: now_ms,
            findings: vec![finding],
            may_open: false,
        })?;
        self.latched = true;
        Ok(())
    }
}

struct ReconciledOrders {
    stop_repairs_pending: std::collections::BTreeSet<SymbolId>,
    may_open: bool,
    vanished: Vec<String>,
    working: std::collections::BTreeSet<String>,
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Come up: read the log back, say who we are in it, learn what the
    /// strategies want, then ask the venue for the instrument rules and the
    /// account before the first message is allowed in.
    ///
    /// Strategy plug names are acceptable for simple callers. Fleet assembly
    /// uses [`Engine::boot_as_exact`] so logs and heartbeats carry sleeve names.
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
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        Self::boot_as_with_instruments(
            settings,
            config_sha256,
            wal,
            risk,
            venue,
            strategies,
            sleeves,
            replayed,
            false,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn boot_as_exact(
        settings: &EngineSection,
        config_sha256: &str,
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        Self::boot_as_with_instruments(
            settings,
            config_sha256,
            wal,
            risk,
            venue,
            strategies,
            sleeves,
            replayed,
            true,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    async fn boot_as_with_instruments(
        settings: &EngineSection,
        config_sha256: &str,
        mut wal: W,
        mut risk: R,
        mut venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
        require_exact_instruments: bool,
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

        use crate::callback_recovery::host::{CallbackExecution, CallbackHost};
        let mut callbacks = match wal.callback_reader()? {
            Some(reader) => {
                CallbackHost::new_paged(CallbackExecution::Embedded, &strategies, replayed, reader)
            }
            None => CallbackHost::new(CallbackExecution::Embedded, &strategies, replayed),
        }
        .map_err(EngineError::Boot)?;
        if callbacks.recovering {
            let reader = wal.callback_reader()?.ok_or_else(|| {
                EngineError::Boot("retained callbacks require their WAL reader".into())
            })?;
            callbacks
                .order_news
                .attach(reader, replayed, strategies.len())
                .map_err(EngineError::Boot)?;
        }
        let dispatches =
            crate::order_dispatch::OrderDispatches::replay(replayed).map_err(EngineError::Boot)?;
        let boot_ms = clock::wall_ms();
        let order_id_epoch_ms =
            super::order_epoch::select_boot_epoch(&mut wal, replayed, boot_ms).await?;
        let ConfiguredStrategies {
            market,
            mut routing,
            names,
            mut subscriptions,
            signal_dependencies,
            initial_global_checkpoints,
        } = restore_configuration(&strategies, sleeves, replayed)?;
        let scope = if require_exact_instruments {
            let account = venue.account_identity().await?;
            Some(engine_types::identity::InstrumentScope {
                venue: account.venue,
                environment: account.realm,
            })
        } else {
            None
        };
        let requested_symbols = (0..market.table.len())
            .map(|index| market.table.name(SymbolId(index as u16)).to_string())
            .collect::<Vec<_>>();
        let mut reserved = crate::identities::plan_identities(
            replayed,
            &names,
            scope.as_ref(),
            &Default::default(),
            &[],
        )
        .map_err(|error| EngineError::Boot(error.to_string()))?;
        crate::identities::reserve_symbol_names(&mut reserved, &requested_symbols)
            .map_err(|error| EngineError::Boot(error.to_string()))?;
        wal.append(&WalRecord::ExecutionPrecisionV1)?;
        wal.append(&WalRecord::OrderIdEpoch {
            epoch_ms: order_id_epoch_ms,
        })?;
        wal.barrier()?;
        if reserved.changed {
            wal.append(&WalRecord::IdentityState {
                wall_ts_ms: boot_ms,
                state: reserved.state.clone(),
            })?;
            wal.barrier()?;
        }
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

        let catalog_client: Option<
            std::sync::Arc<dyn engine_types::orders::InstrumentCatalogClient>,
        > = venue.instrument_catalog_client().map(std::sync::Arc::from);
        let prior_catalog = super::symbol_admission::replay_catalog(replayed)?;
        let catalog_refresh_required = prior_catalog.is_some();
        let catalog = if let Some(checkpoint) = &prior_catalog {
            venue.restore_instrument_catalog(checkpoint)?
        } else {
            match &catalog_client {
                Some(client) => client.fetch().await?,
                None => engine_types::orders::InstrumentCatalog {
                    cache: None,
                    rules: venue.instrument_rules().await?,
                    specs: match venue.instrument_specs().await {
                        Ok(specs) => specs,
                        Err(error) if require_exact_instruments => {
                            return Err(EngineError::Boot(format!(
                                "exact instrument catalog unavailable: {error}"
                            )))
                        }
                        Err(_) => Vec::new(),
                    },
                },
            }
        };
        let catalog_checkpoint = if catalog.cache.is_some() {
            Some(Box::new(catalog.checkpoint()?))
        } else {
            None
        };
        if catalog_checkpoint != prior_catalog {
            if let Some(checkpoint) = &catalog_checkpoint {
                wal.append(&WalRecord::InstrumentCatalogCheckpoint {
                    wall_ts_ms: boot_ms,
                    checkpoint: checkpoint.clone(),
                })?;
                wal.barrier()?;
            }
        }
        if catalog.cache.is_some() {
            venue.install_instrument_catalog(&catalog)?;
        }
        let native_symbols = catalog
            .specs
            .iter()
            .map(|(alias, spec)| (alias.clone(), spec.native_symbol.clone()))
            .collect();
        let identity_plan = crate::identities::plan_identities(
            &[WalRecord::IdentityState {
                wall_ts_ms: boot_ms,
                state: reserved.state,
            }],
            &names,
            scope.as_ref(),
            &native_symbols,
            &[],
        )
        .map_err(|error| EngineError::Boot(error.to_string()))?;
        let identities = identity_plan.state;
        if identity_plan.changed {
            wal.append(&WalRecord::IdentityState {
                wall_ts_ms: boot_ms,
                state: identities.clone(),
            })?;
            wal.barrier()?;
        }
        let mut rules = vec![None; market.table.len()];
        for (name, rule) in &catalog.rules {
            if let Some(id) = market.table.get(name) {
                rules[id.0 as usize] = Some(*rule);
            }
        }
        let instrument_specs: std::collections::BTreeMap<
            SymbolId,
            engine_types::numeric::ExactInstrumentSpec,
        > = catalog
            .specs
            .iter()
            .filter_map(|(name, spec)| market.table.get(name).map(|id| (id, spec.clone())))
            .collect();
        let mut missing: Vec<&str> = Vec::new();
        for subscription in &subscriptions {
            let Some(id) = market.table.get(&subscription.symbol) else {
                continue;
            };
            if rules[id.0 as usize].is_none() && !missing.contains(&subscription.symbol.as_str()) {
                missing.push(subscription.symbol.as_str());
            }
        }
        if !missing.is_empty() && !catalog_refresh_required {
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
            &dispatches,
            &market.table,
            &mut recovered_exec_ids,
            boot_ms,
            &mut callbacks,
            &account,
            &mut risk,
            (!instrument_specs.is_empty() || require_exact_instruments)
                .then_some(&instrument_specs),
        )
        .await?;
        let RecoveryOutcome {
            mut orders,
            attribution,
            fills,
            mut portfolio_controls,
            physical: logged_exposure,
            intended: intended_stops,
            latched: recovery_latched,
            through_ms: recovered_through_ms,
        } = recovery;
        risk.observe_wall_clock_ms(clock::wall_ms());
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
            replayed,
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
        // Legacy fills without an execution id retain their field-based overlap key.
        let recent_fills = recent_legacy_fills(replayed);

        // What the log believes against what the venue says. Boot is the one
        // moment the two can be compared: from here on the engine only ever
        // learns about its own orders.
        let ReconciledOrders {
            may_open,
            vanished,
            working,
            stop_repairs_pending,
        } = Self::reconcile_with_venue(
            &mut wal,
            &mut venue,
            &orders,
            replayed,
            &account,
            &market.table,
            &rules,
            &instrument_specs,
            (&logged_exposure, &intended_stops),
            recovery_latched,
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
            if dispatches.orders.contains_key(&client_order_id) {
                continue;
            }
            tracing::warn!(
                id = %client_order_id,
                "this order ended while the engine was down; recording the ending"
            );
            let owners = callbacks.recovering.then(|| {
                orders
                    .owner_of(&client_order_id)
                    .into_iter()
                    .collect::<Vec<_>>()
            });
            let ended = WalRecord::OrderUpdate {
                callbacks: owners.clone(),
                update: OrderUpdate::Cancelled {
                    client_order_id,
                    recv_ns: clock::now_ns(),
                },
            };
            let offset = wal.segment_size();
            let sequence = wal.append(&ended)?;
            if let Some(owners) = owners {
                callbacks
                    .order_news
                    .record_at(sequence, offset, &owners)
                    .map_err(EngineError::State)?;
            }
            orders.try_apply(&ended).map_err(EngineError::State)?;
            portfolio_controls.retire_completed_orders(&orders);
        }
        let recovered = orders.in_flight().len();

        let registry =
            restore_order_reservations(&mut risk, &orders, order_id_epoch_ms, &account, &working)?;
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                ids = ?orders.in_flight_ids(),
                "orders were in flight when the engine last stopped; they are not re-sent"
            );
        }

        let boot_account_started_ns = account.observed_ns;
        let now = clock::now_ns();
        let recovery_reads = account_recovery::Recovery::new(venue.account_recovery_client());
        let (venue, venue_completions) = VenueClient::spawn(venue);
        let mut engine = Engine {
            refusals: BTreeMap::new(),
            wal,
            risk,
            venue,
            venue_completions,
            recovery: recovery_reads,
            pending_mutations: BTreeMap::new(),
            busy_symbols: BTreeMap::new(),
            order_lineage: order_lineage::OrderLineage::default(),
            deferred_actions: BTreeMap::new(),
            ready_actions: VecDeque::new(),
            _venue: std::marker::PhantomData,
            host: StrategyHost {
                callbacks,
                effects: crate::effects::Effects::replay(replayed, names.len())
                    .map_err(EngineError::Boot)?,
                strategies,
                names,
                timers: Timers::default(),
                pending: VecDeque::new(),
                callback_actions: VecDeque::new(),
                checkpoints: strategy_checkpoints,
                global_checkpoints: strategy_global_checkpoints,
                events: strategy_events,
                entries_enabled: runtime_entries_enabled,
            },
            books: Books {
                portfolio_symbols: instrument_specs.keys().copied().collect(),
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
            instrument_specs,
            require_exact_instruments,
            identities,
            routing,
            drain_progress: None,
            suspended_wakes: Default::default(),
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
            halt_cancels: BTreeMap::new(),
            amends_awaiting_price: BTreeMap::new(),
            amends_confirmed: 0,
            amends_pulled_unconfirmed: 0,
            stream_resets: 0,
            dispatches,
            portfolio_controls,
            portfolio_dirty: false,
            strategy_barrier_pending: false,
            strategy_runtime_retirements: BTreeSet::new(),
            portfolio_cursor: 0,
            portfolio_physical_after: BTreeMap::new(),
            halt_cancel_queue: VecDeque::new(),
            wanted_symbols: Vec::new(),
            symbol_admission: super::symbol_admission::SymbolAdmission::new(
                catalog,
                catalog_client,
                catalog_checkpoint,
                catalog_refresh_required,
            ),
            leverage_at: BTreeMap::new(),
            may_open,
            private_stream_ready: true,
            logged_exposure,
            intended_stops,
            stop_repairs_pending,
            confirmed_native_stops: std::collections::BTreeMap::new(),
            confirmed_stop_moves: std::collections::BTreeMap::new(),
            recovered_until_ms: recovered_through_ms,
            next_history_checkpoint_ms: recovered_through_ms
                .max(boot_ms)
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
            account_refresh_requested_after: None,
            account_refresh_started_ns: boot_account_started_ns,
            rotate_after_bytes: settings.wal_rotate_mb.saturating_mul(1024 * 1024),
            max_quote_age_ns: settings.max_quote_age_ms.saturating_mul(1_000_000),
            next_order_n: 0,
            order_id_epoch_ms,
            orders_sent: 0,
            events_seen: 0,
            subscriptions,
            portfolio_subscriptions: Vec::new(),
        };
        engine
            .fills
            .learn(&names_record(&engine.host.names, &engine.books.market));
        engine.ensure_callback_reader(replayed)?;
        engine.enforce_position_stop_intent().await?;
        engine.restore_order_dispatches().await?;
        engine.restore_strategy_effects().await?;
        engine.restore_strategy_callbacks().await?;
        engine.restore_portfolio_routes()?;
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
    #[allow(clippy::too_many_arguments)]
    pub(super) async fn recover_missed_fills(
        wal: &mut W,
        venue: &mut V,
        replayed: &[WalRecord],
        dispatches: &crate::order_dispatch::OrderDispatches,
        table: &SymbolTable,
        execution_ids: &mut ExecutionIds,
        fresh_start_ms: i64,
        callbacks: &mut crate::callback_recovery::host::CallbackHost,
        account: &AccountView,
        risk: &mut R,
        specs: Option<&BTreeMap<SymbolId, engine_types::numeric::ExactInstrumentSpec>>,
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
        let adoption = specs
            .map(|specs| crate::legacy_quantity::plan(replayed, specs, now_ms))
            .transpose()
            .map_err(EngineError::Boot)?
            .flatten();
        let mut recovered_state = RecoveryOutcome::prepare(replayed, adoption.as_ref(), newest)?;
        if let Some(record) = &adoption {
            wal.append(record)?;
            wal.barrier()?;
        }
        recovered_state.seed_loss(replayed, risk);

        let since = newest.saturating_sub(RECOVERY_PAD_MS);
        if since < now_ms - RECOVERY_REACH_MS {
            return Err(EngineError::Boot(format!(
                "the log is {} ms behind, beyond the venue execution-history reach of {} ms",
                now_ms - newest,
                RECOVERY_REACH_MS
            )));
        }
        if since >= now_ms {
            return Ok(recovered_state);
        }
        let execs = venue.executions(since, now_ms).await.map_err(|e| {
            EngineError::Boot(format!(
                "cannot read execution history for the recovery interval: {e}"
            ))
        })?;
        let mut delivered = legacy_overlap_counts(replayed, since);
        let mut recovered = 0usize;
        let strategy_names = crate::replay::LogNames::of_log(replayed).strategies;
        for exec in execs {
            let exec = exec?;
            if execution_ids.contains(&exec.exec_id, now_ms) {
                continue;
            }
            let same_delivered = delivered
                .get_mut(exec.client_order_id.as_str())
                .and_then(|rows| rows.get_mut(&(exec.venue_ts_ms, exec.qty.to_bits())))
                .is_some_and(|count| {
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
                recovered_state.reject(wal, now_ms, finding)?;
                continue;
            };
            let dedup_id = exec.exec_id.clone();
            if !recovered_state.orders.contains(&exec.client_order_id)
                && exec.client_order_id.starts_with("eng-")
                && wal.supports_order_lineage_archive()
            {
                let reader = wal
                    .order_lineage_reader(&exec.client_order_id)?
                    .ok_or_else(|| {
                        EngineError::Boot("order lineage archive reader is unavailable".into())
                    })?;
                let id = exec.client_order_id.clone();
                let row = order_lineage::load_order_lineage(reader, id)
                    .await
                    .map_err(EngineError::Boot)?;
                if let Some(row) = row {
                    order_lineage::activate_order_lineage(wal, &mut recovered_state.orders, row)?;
                }
            }
            if let Err(reason) = recovered_state
                .orders
                .validate_fill(&exec.client_order_id, symbol, exec.side, exec.qty, exec.px)
                .and_then(|()| {
                    recovered_state.orders.validate_fill_quantities(
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
                recovered_state.reject(
                    wal,
                    now_ms,
                    Self::untrusted_fill_line(
                        &exec.exec_id,
                        &exec.client_order_id,
                        symbol,
                        exec.side,
                        exec.qty,
                        exec.px,
                        &reason,
                    ),
                )?;
                execution_ids.insert(dedup_id, now_ms);
                recovered += 1;
                continue;
            }
            let client_order_id = exec.client_order_id.clone();
            let mut record = WalRecord::RecoveredFill {
                callbacks: None,
                allocation: None,
                amounts: exec.amounts.clone(),
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
            let owner = recovered_state.orders.owner_of(&client_order_id);
            let allocation = match recovered_state
                .attribution
                .prepare_portfolio_recovered_on_grid(
                    recovered_state
                        .orders
                        .orders
                        .get(&client_order_id)
                        .map(|order| &order.request),
                    &strategy_names,
                    &record,
                    specs
                        .and_then(|specs| specs.get(&symbol))
                        .and_then(|spec| spec.qty_step.as_ref()),
                ) {
                Ok(allocation) => allocation,
                Err(reason) => {
                    recovered_state.reject(
                        wal,
                        now_ms,
                        Self::untrusted_fill_line(
                            &dedup_id,
                            &client_order_id,
                            symbol,
                            exec.side,
                            exec.qty,
                            exec.px,
                            &reason,
                        ),
                    )?;
                    execution_ids.insert(dedup_id, now_ms);
                    recovered += 1;
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
                if callbacks.recovering
                    || prepared.allocation.policy
                        == engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo
                {
                    *recorded = Some(Box::new(prepared.allocation.clone()));
                }
            }
            if callbacks.recovering {
                let owners = allocation
                    .as_ref()
                    .map(|prepared| {
                        prepared
                            .allocation
                            .slices
                            .iter()
                            .map(|slice| slice.strategy)
                            .collect()
                    })
                    .unwrap_or_default();
                let WalRecord::RecoveredFill {
                    callbacks: recorded,
                    ..
                } = &mut record
                else {
                    unreachable!()
                };
                *recorded = Some(engine_types::wal::RecoveredCallbacks {
                    owners,
                    recv_ns: clock::now_ns(),
                });
            }
            let offset = wal.segment_size();
            let sequence = wal.append(&record)?;
            if let WalRecord::RecoveredFill {
                callbacks: Some(owners),
                ..
            } = &record
            {
                callbacks
                    .order_news
                    .record_at(sequence, offset, &owners.owners)
                    .map_err(EngineError::State)?;
            }
            let owned = allocation.is_some();
            let analytic_owner = owner.or_else(|| {
                allocation.as_ref().and_then(|prepared| {
                    (prepared.allocation.slices.len() == 1)
                        .then(|| prepared.allocation.slices[0].strategy)
                })
            });
            if let Some(allocation) = allocation {
                recovered_state
                    .attribution
                    .commit_portfolio_fill(allocation)
                    .map_err(EngineError::State)?;
            }
            if owned {
                let request = recovered_state
                    .orders
                    .orders
                    .get(&client_order_id)
                    .map(|order| &order.request);
                reconcile::note_owned_fill(
                    &mut recovered_state.physical,
                    &mut recovered_state.intended,
                    request,
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
                let update = crate::portfolio_allocation::recovered_update(&record, 0)
                    .expect("recovered fill");
                let slices = crate::portfolio_allocation::slice_updates(&update)
                    .map_err(EngineError::State)?
                    .unwrap_or_else(|| {
                        analytic_owner
                            .map(|id| vec![(id, update.clone())])
                            .unwrap_or_default()
                    });
                for (strategy, _) in slices {
                    let OrderUpdate::Fill {
                        amounts,
                        allocation,
                        qty,
                        px,
                        fee,
                        side,
                        venue_ts_ms,
                        is_maker,
                        ..
                    } = update.clone()
                    else {
                        unreachable!()
                    };
                    let sleeve = strategy_names.get(strategy.idx()).ok_or_else(|| {
                        EngineError::Boot("recovered lot has no durable sleeve name".into())
                    })?;
                    let fill = execution::Fill {
                        amounts,
                        qty,
                        px,
                        fee,
                        side,
                        venue_ts_ms,
                        is_maker,
                        client_order_id: client_order_id.clone(),
                        strategy,
                        symbol,
                        arrival_mid: recovered_state
                            .orders
                            .orders
                            .get(&client_order_id)
                            .map_or(0.0, |order| order.arrival_mid),
                    };
                    if let Some(allocation) = allocation {
                        let (part, economics) = execution::allocated_fill(&fill, &allocation)
                            .map_err(EngineError::State)?;
                        recovered_state
                            .fills
                            .lots()
                            .on_fill_with_economics(
                                sleeve,
                                table.name(symbol),
                                &part,
                                Some(&economics.quantity),
                                Some(&economics),
                            )
                            .map_err(EngineError::State)?;
                    } else {
                        recovered_state
                            .fills
                            .lots()
                            .on_fill_with_quantity(sleeve, table.name(symbol), &fill, None)
                            .map_err(EngineError::State)?;
                    }
                    for trade in recovered_state.fills.take_closed() {
                        if let Some(row) = trade.loss_row() {
                            risk.observe_closed_trade(row);
                        }
                    }
                }
            } else {
                recovered_state.reject(
                    wal,
                    now_ms,
                    Self::foreign_fill_line(&client_order_id, symbol),
                )?;
            }
            execution_ids.insert(dedup_id, now_ms);
            recovered_state
                .orders
                .try_apply(&record)
                .map_err(EngineError::State)?;
            recovered_state
                .portfolio_controls
                .apply(&record)
                .map_err(EngineError::State)?;
            recovered_state
                .portfolio_controls
                .retire_completed_orders(&recovered_state.orders);
            if owner.is_some() {
                if let Some(order) = recovered_state.orders.orders.get(&client_order_id) {
                    recovered_state
                        .attribution
                        .remember_order_stop(&order.request);
                }
            }
            if wal.supports_order_lineage_archive() {
                order_lineage::trim_boot_order_cache(&mut recovered_state.orders)
                    .map_err(EngineError::Boot)?;
            }
            recovered += 1;
        }
        if recovered > 0 {
            tracing::warn!(
                count = recovered,
                "recovered fills the private stream never delivered"
            );
        }
        let through_ms = if !recovered_state.latched
            && dispatches.orders.is_empty()
            && super::account_recovery::history_account_matches(account, &recovered_state.physical)?
            && !recovered_state
                .orders
                .orders
                .values()
                .any(|order| order.in_flight())
        {
            now_ms
        } else {
            newest
        };
        let checkpoint = WalRecord::ExecutionHistoryCheckpoint {
            through_wall_ts_ms: through_ms,
        };
        wal.append(&checkpoint)?;
        wal.barrier()?;
        recovered_state.through_ms = through_ms;
        recovered_state
            .portfolio_controls
            .retain_native_offsets(&recovered_state.attribution.snapshot());
        Ok(recovered_state)
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
        specs: &std::collections::BTreeMap<SymbolId, engine_types::numeric::ExactInstrumentSpec>,
        positions: (
            &reconcile::PhysicalExposure,
            &BTreeMap<SymbolId, reconcile::IntendedPositionStop>,
        ),
        recovery_latched: bool,
    ) -> Result<ReconciledOrders, EngineError> {
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

        let found = reconcile::reconcile_positions(
            orders,
            replayed,
            positions,
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
        )
        .map_err(EngineError::Boot)?;

        let mut finding_lines = found.lines();
        for line in &finding_lines {
            tracing::warn!(finding = %line, "reconciliation");
        }

        // A stop the log says belongs somewhere, that the venue does not have.
        // Putting it back is the one repair the engine can make from evidence
        // rather than from a guess.
        let stop_repairs_pending = account
            .positions
            .iter()
            .filter(|p| p.qty > 0.0 && specs.contains_key(&p.symbol))
            .map(|p| p.symbol)
            .collect();
        let mut repair_failed = false;
        for (symbol, trigger_px) in found.stop_repairs() {
            if specs.contains_key(&symbol) {
                continue;
            }
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

        let may_open = !recovery_latched
            && latched.unwrap_or(true)
            && !found.must_not_open()
            && !repair_failed;
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
        Ok(ReconciledOrders {
            stop_repairs_pending,
            may_open,
            vanished: found.vanished(),
            working: working
                .into_iter()
                .map(|order| order.client_order_id)
                .collect(),
        })
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

    pub(super) fn foreign_unmapped_execution_line(
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

#[cfg(test)]
mod callback_recovery_tests {
    use super::*;
    use crate::callback_recovery::host::{CallbackExecution, CallbackHost};
    use engine_types::strategy_process::CallbackEvent;

    #[tokio::test(start_paused = true)]
    async fn initial_recovery_sources_keep_actual_sequences_after_new_boot_frames() {
        let now = clock::wall_ms();
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let plug = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let strategies = vec![plug];
        let request = OrderRequest {
            client_order_id: "owned-before-restart".into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.02,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: 90.0 }),
            reduce_only: false,
            close_position: false,
            sleeve_effect: None,
            exact_terms: None,
        };
        let replay = vec![
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
                strategies: vec![strategies[0].name().into()],
                symbols: vec!["BTCUSDT".into()],
            }),
            WalRecord::OrderSent {
                dispatch: None,
                request: request.clone(),
                arrival_mid: 100.0,
                wire_ns: 1,
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Fill {
                    allocation: None,
                    amounts: None,
                    exec_id: "opening-exec".into(),
                    client_order_id: request.client_order_id,
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 0.02,
                    px: 100.0,
                    fee: Some(0.0),
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: now - 10,
                    recv_ns: 1,
                },
            },
            WalRecord::ExecutionHistoryCheckpoint {
                through_wall_ts_ms: now - 10,
            },
        ];
        let path = crate::testpath::temp_path("initial-recovery-source");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for row in &replay {
            crate::testpath::append_history(&mut wal, &path, row).unwrap();
        }
        wal.barrier().unwrap();
        let mut callbacks =
            CallbackHost::new(CallbackExecution::Embedded, &strategies, &replay).unwrap();
        callbacks.recovering = true;
        callbacks
            .order_news
            .attach(wal.callback_reader().unwrap().unwrap(), &replay, 1)
            .unwrap();
        for marker in ["boot", "identity", "catalog"] {
            wal.append(&WalRecord::Note {
                source: marker.into(),
                text: "new boot prefix".into(),
            })
            .unwrap();
        }
        let mut venue = crate::tests::recovery_venue_fixture(vec![engine_types::VenueExecution {
            exec_id: "recovered-during-boot".into(),
            client_order_id: String::new(),
            symbol: "BTCUSDT".into(),
            side: Side::Sell,
            qty: 0.005,
            px: 90.0,
            fee: None,
            amounts: None,
            is_maker: false,
            forced_close: Some(engine_types::ForcedClose::StopLoss),
            venue_ts_ms: now - 1,
        }]);
        let mut table = SymbolTable::default();
        table.intern("BTCUSDT");
        let mut ids = ExecutionIds::from_records(&replay, now).unwrap();
        let source_offset = wal.segment_size();
        let _outcome = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &replay,
            &crate::order_dispatch::OrderDispatches::replay(&replay).unwrap(),
            &table,
            &mut ids,
            now,
            &mut callbacks,
            &AccountView {
                exact_amounts: None,
                equity_usdt: 10_000.0,
                available_usdt: 10_000.0,
                positions: Vec::new(),
                observed_ns: clock::now_ns(),
            },
            &mut crate::tests::MockRisk::with(crate::tests::allow_all()).0,
            None,
        )
        .await
        .unwrap();
        assert_eq!(
            callbacks.order_news.snapshot()[0].cursor.offset,
            source_offset,
            "a newly recovered fill must retain its physical frame offset instead of scanning old headers"
        );
        assert!(
            callbacks.order_news.unread_for(StrategyId(0)),
            "initial recovery fill has no durable callback retry owner"
        );
        assert!(
            engine_wal::replay(&path).unwrap().iter().any(|(_, record)| matches!(record, WalRecord::RecoveredFill { callbacks: Some(owners), .. } if owners.owners == [StrategyId(0)]))
        );
        loop {
            callbacks.order_news.start_read();
            let completion = callbacks.order_news.completed.recv().await.unwrap();
            let (owner, cursor, record) = callbacks.order_news.returned(completion).unwrap();
            if let Some((owners, CallbackEvent::Order { update })) = record.source {
                assert_eq!(
                    cursor.sequence,
                    replay.len() as u64 + 4,
                    "source used a replay-vector index instead of the actual WAL sequence"
                );
                assert_eq!(owners, [owner]);
                assert!(
                    matches!(update, OrderUpdate::Fill { exec_id, qty, fee: None, .. } if exec_id == "recovered-during-boot" && qty == 0.005)
                );
                break;
            }
            callbacks.order_news.advance(owner, record.next);
        }
    }
}

// A child process gives this heap regression a private high-water measurement.
#[cfg(test)]
mod memory_tests {
    use super::*;
    use crate::callback_recovery::host::{CallbackExecution, CallbackHost};

    #[tokio::test(start_paused = true)]
    async fn boot_recovers_a_rejected_order_from_archives_before_charging_its_late_fill() {
        use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};
        let prior = crate::tests::shared_sleeves::fragmented_engine().await;
        let now = clock::wall_ms();
        let mut base = prior.rotation_base(now);
        let id = "eng-boot-archived-late-1";
        let mut request = OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.4,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: false,
            exact_terms: None,
            sleeve_effect: Some(engine_types::orders::SleeveOrderEffect::Reduce),
        };
        engine_types::order_terms::ExactOrderTerms {
            quantity: Exact::parse_decimal("0.4").unwrap(),
            limit_price: None,
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: engine_types::order_terms::OrderInputPolicy::CanonicalPortfolio,
        }
        .apply_projection(&mut request)
        .unwrap();
        let path = crate::testpath::temp_path("boot-archived-terminal-lineage");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        wal.append(&base).unwrap();
        wal.append(&WalRecord::OrderSent {
            request,
            dispatch: None,
            wire_ns: clock::now_ns(),
            arrival_mid: 100.0,
        })
        .unwrap();
        wal.append(&WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Reject {
                client_order_id: id.into(),
                code: 1,
                reason: "late execution after rejection".into(),
            },
        })
        .unwrap();
        if let WalRecord::SegmentBase { open_orders, .. } = &mut base {
            open_orders.clear();
        }
        wal.rotate(&base).unwrap();
        drop(wal);
        let (mut wal, records) = engine_wal::open_current(&path).unwrap();
        let replay: Vec<_> = records.into_iter().map(|(_, row)| row).collect();
        assert!(!LedgerOfOrders::try_from_records(&replay)
            .unwrap()
            .contains(id));
        let execution = engine_types::VenueExecution {
            client_order_id: id.into(),
            exec_id: "boot-cold-fill".into(),
            symbol: "BTCUSDT".into(),
            side: Side::Sell,
            qty: 0.1,
            px: 99.0,
            fee: Some(0.001),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: now,
            amounts: Some(ExecutionAmounts {
                quantity: ExactNumber::venue_decimal("0.1").unwrap(),
                price: ExactNumber::venue_decimal("99").unwrap(),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0.001").unwrap(),
                }),
                settlement_asset: AssetId::Named("USDT".into()),
            }),
        };
        let mut venue = crate::tests::recovery_venue_fixture(vec![execution.clone()]);
        let mut table = SymbolTable::default();
        table.intern("BTCUSDT");
        let mut ids = ExecutionIds::from_records(&replay, now).unwrap();
        let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
        let (mut risk, _) = crate::tests::MockRisk::with(crate::tests::allow_all());
        let account = AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: crate::tests::shared_sleeves::physical_long(0.9),
            observed_ns: clock::now_ns(),
        };
        let recovered = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &replay,
            &crate::order_dispatch::OrderDispatches::replay(&replay).unwrap(),
            &table,
            &mut ids,
            now,
            &mut callbacks,
            &account,
            &mut risk,
            None,
        )
        .await
        .unwrap();
        assert!(
            !recovered.latched,
            "known archived order was charged to a stranger"
        );
        assert_eq!(
            recovered
                .attribution
                .signed_exact(StrategyId(0), SymbolId(0)),
            Exact::parse_decimal("0.3").unwrap()
        );
        assert_eq!(
            recovered
                .attribution
                .signed_exact(StrategyId(1), SymbolId(0)),
            Exact::parse_decimal("0.6").unwrap()
        );
        let current: Vec<_> = engine_wal::replay_current(&path)
            .unwrap()
            .0
            .into_iter()
            .map(|(_, row)| row)
            .collect();
        let restored = current
            .iter()
            .position(|row| matches!(row, WalRecord::OrderLineageRestored { .. }))
            .unwrap();
        let filled = current
            .iter()
            .position(|row| matches!(row, WalRecord::RecoveredFill { .. }))
            .unwrap();
        assert!(restored < filled);
        assert_eq!(
            Attribution::try_from_records(&current).unwrap().snapshot(),
            recovered.attribution.snapshot()
        );
        assert_eq!(
            Fills::try_from_records(&current).unwrap().open_trade_lots(),
            recovered.fills.open_trade_lots()
        );
        let mut ids = ExecutionIds::from_records(&current, now).unwrap();
        let mut venue = crate::tests::recovery_venue_fixture(vec![execution]);
        let repeated = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &current,
            &crate::order_dispatch::OrderDispatches::replay(&current).unwrap(),
            &table,
            &mut ids,
            now,
            &mut callbacks,
            &account,
            &mut risk,
            None,
        )
        .await
        .unwrap();
        assert_eq!(
            repeated.attribution.snapshot(),
            recovered.attribution.snapshot()
        );
        assert_eq!(
            engine_wal::replay_current(&path)
                .unwrap()
                .0
                .iter()
                .filter(|(_, row)| matches!(row, WalRecord::RecoveredFill { .. }))
                .count(),
            1
        );
    }

    #[tokio::test(start_paused = true)]
    async fn an_untrusted_net_neutral_history_page_cannot_advance_the_boot_checkpoint() {
        let now = clock::wall_ms();
        let through = now - 60_000;
        let rows = [Side::Buy, Side::Sell]
            .into_iter()
            .enumerate()
            .map(|(index, side)| engine_types::VenueExecution {
                exec_id: format!("foreign-neutral-{index}"),
                client_order_id: "manual".into(),
                symbol: "BTCUSDT".into(),
                side,
                qty: 1.0,
                px: 100.0,
                fee: Some(0.0),
                amounts: None,
                is_maker: false,
                forced_close: None,
                venue_ts_ms: now - 1,
            })
            .collect();
        let mut venue = crate::tests::recovery_venue_fixture(rows);
        let path = crate::testpath::temp_path("untrusted-boot-history-boundary");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let mut table = SymbolTable::default();
        table.intern("BTCUSDT");
        let replay = [
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
                strategies: Vec::new(),
                symbols: vec!["BTCUSDT".into()],
            }),
            WalRecord::ExecutionHistoryCheckpoint {
                through_wall_ts_ms: through,
            },
        ];
        let mut ids = ExecutionIds::from_records(&replay, now).unwrap();
        let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
        let (mut risk, _) = crate::tests::MockRisk::with(crate::tests::allow_all());
        let account = AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        };
        let recovered = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &replay,
            &crate::order_dispatch::OrderDispatches::replay(&replay).unwrap(),
            &table,
            &mut ids,
            now,
            &mut callbacks,
            &account,
            &mut risk,
            None,
        )
        .await
        .unwrap();
        assert!(recovered.latched);
        assert!(recovered.physical.is_empty());
        assert_eq!(recovered.through_ms, through);
        let records = engine_wal::replay(&path)
            .unwrap()
            .into_iter()
            .map(|(_, record)| record)
            .collect::<Vec<_>>();
        assert_eq!(execution_history_through_ms(&records), Some(through));
        assert_eq!(
            records
                .iter()
                .filter(|row| matches!(row, WalRecord::RecoveredFill { .. }))
                .count(),
            2
        );
        assert!(records.iter().any(|row| matches!(
            row,
            WalRecord::Reconciled {
                may_open: false,
                ..
            }
        )));
    }

    fn peak_resident_bytes() -> u64 {
        let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
        // getrusage initializes the supplied rusage on success.
        assert_eq!(
            unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) },
            0
        );
        let usage = unsafe { usage.assume_init() };
        #[cfg(target_os = "macos")]
        let scale = 1;
        #[cfg(not(target_os = "macos"))]
        let scale = 1024;
        usage.ru_maxrss as u64 * scale
    }

    #[test]
    fn recent_legacy_overlap_clones_only_the_retained_tail() {
        const CHILD: &str = "TIER1_LEGACY_OVERLAP_MEMORY_CHILD";
        if std::env::var_os(CHILD).is_none() {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "engine::boot_recovery::memory_tests::recent_legacy_overlap_clones_only_the_retained_tail", "--nocapture"])
                .env(CHILD, "1").output().unwrap();
            assert!(
                output.status.success(),
                "{}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            eprint!("{}", String::from_utf8_lossy(&output.stderr));
            return;
        }
        let records: Vec<_> = (0..10_000)
            .map(|index| WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Fill {
                    allocation: None,
                    amounts: None,
                    exec_id: String::new(),
                    client_order_id: format!("{index:05}{}", "x".repeat(8192)),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 100.0,
                    fee: None,
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: index,
                    recv_ns: 0,
                },
            })
            .collect();
        let before = peak_resident_bytes();
        let recent = recent_legacy_fills(&records);
        let mut counts = legacy_overlap_counts(&records, 0);
        let growth = peak_resident_bytes().saturating_sub(before);
        assert_eq!(counts.len(), 10_000);
        assert_eq!(
            counts
                .get_mut(recent.back().unwrap().0.as_str())
                .unwrap()
                .remove(&(9999, 1.0_f64.to_bits())),
            Some(1)
        );
        assert_eq!(recent.len(), RECENT_FILLS_KEPT);
        assert_eq!(recent.front().unwrap().1, 10_000 - RECENT_FILLS_KEPT as i64);
        assert_eq!(recent.back().unwrap().1, 9999);
        assert!(recent
            .iter()
            .zip(recent.iter().skip(1))
            .all(|(a, b)| a.1 < b.1));
        eprintln!(
            "legacy-overlap rows=10000 retained={} peak_resident_growth_bytes={growth}",
            recent.len()
        );
        assert!(
            growth < 32 * 1024 * 1024,
            "legacy overlap cloned the full source: peak grew {growth} bytes"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn boot_history_memory_does_not_grow_with_recovered_wal_payload() {
        const CHILD: &str = "TIER1_BOOT_RECOVERY_MEMORY_CHILD";
        if std::env::var_os(CHILD).is_none() {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "engine::boot_recovery::memory_tests::boot_history_memory_does_not_grow_with_recovered_wal_payload", "--nocapture"])
                .env(CHILD, "1").output().unwrap();
            assert!(
                output.status.success(),
                "{}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            eprint!("{}", String::from_utf8_lossy(&output.stderr));
            return;
        }
        let now = clock::wall_ms();
        let mut history = engine_types::ExecutionHistoryBuilder::default();
        for index in 0..2048 {
            history
                .push(engine_types::VenueExecution {
                    exec_id: format!("memory-execution-{index}"),
                    client_order_id: "x".repeat(32 * 1024),
                    symbol: "BTCUSDT".into(),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 100.0,
                    fee: Some(0.0),
                    amounts: None,
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: now - 1,
                })
                .unwrap();
        }
        let mut venue = crate::tests::recovery_spool_fixture(history.finish().unwrap());
        let path = crate::testpath::temp_path("bounded-boot-history");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let mut table = SymbolTable::default();
        table.intern("BTCUSDT");
        let replay = [
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
                strategies: Vec::new(),
                symbols: vec!["BTCUSDT".into()],
            }),
            WalRecord::ExecutionHistoryCheckpoint {
                through_wall_ts_ms: now - 60_000,
            },
        ];
        let mut ids = ExecutionIds::from_records(&replay, now).unwrap();
        let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
        let (mut risk, _) = crate::tests::MockRisk::with(crate::tests::allow_all());
        let account = AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        };
        let before = peak_resident_bytes();
        let recovered = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &replay,
            &crate::order_dispatch::OrderDispatches::replay(&replay).unwrap(),
            &table,
            &mut ids,
            now,
            &mut callbacks,
            &account,
            &mut risk,
            None,
        )
        .await
        .unwrap();
        let growth = peak_resident_bytes().saturating_sub(before);
        assert!(
            recovered.latched,
            "foreign fills must remain a durable reconciliation finding"
        );
        assert!(recovered.orders.orders.is_empty());
        assert_eq!(ids.len(), 2048);
        assert!(std::fs::metadata(&path).unwrap().len() > 64 * 1024 * 1024);
        eprintln!("boot-history rows=2048 payload=64MiB peak_resident_growth_bytes={growth}");
        assert!(
            growth < 32 * 1024 * 1024,
            "boot retained recovered payloads: peak grew {growth} bytes"
        );
    }
}

#[cfg(test)]
mod valuation_recovery_tests {
    use super::*;
    use crate::callback_recovery::host::{CallbackExecution, CallbackHost};
    use engine_types::numeric::{AssetAmount, AssetId, ExactNumber, ExecutionAmounts};
    use engine_types::risk::UnpricedTradeReason;

    #[tokio::test(start_paused = true)]
    async fn streamed_unvalued_close_retains_identical_loss_debt_when_its_wal_replays() {
        let now = clock::wall_ms();
        let amounts = |price: &str| {
            Box::new(ExecutionAmounts {
                settlement_asset: AssetId::Unknown,
                quantity: ExactNumber::venue_decimal("1").unwrap(),
                price: ExactNumber::venue_decimal(price).unwrap(),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0").unwrap(),
                }),
            })
        };
        let replay = vec![
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
                strategies: vec!["owner".into()],
                symbols: vec!["BTCUSDT".into()],
            }),
            WalRecord::OrderSent {
                dispatch: None,
                request: OrderRequest {
                    client_order_id: "eng-native-open".into(),
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec { trigger_px: 90.0 }),
                    reduce_only: false,
                    exact_terms: None,
                    sleeve_effect: None,
                    close_position: false,
                },
                wire_ns: 1,
                arrival_mid: 100.0,
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Fill {
                    allocation: None,
                    amounts: Some(amounts("100")),
                    exec_id: "native-open".into(),
                    client_order_id: "eng-native-open".into(),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 100.0,
                    fee: Some(0.0),
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: now - 10,
                    recv_ns: 1,
                },
            },
            WalRecord::ExecutionHistoryCheckpoint {
                through_wall_ts_ms: now - 10,
            },
        ];
        let mut venue = crate::tests::recovery_venue_fixture(vec![engine_types::VenueExecution {
            exec_id: "native-close-during-recovery".into(),
            client_order_id: String::new(),
            symbol: "BTCUSDT".into(),
            side: Side::Sell,
            qty: 1.0,
            px: 90.0,
            fee: Some(0.0),
            amounts: Some(*amounts("90")),
            is_maker: false,
            forced_close: Some(engine_types::ForcedClose::StopLoss),
            venue_ts_ms: now - 1,
        }]);
        let path = crate::testpath::temp_path("unvalued-recovery-loss");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        for row in &replay {
            crate::testpath::append_history(&mut wal, &path, row).unwrap();
        }
        wal.barrier().unwrap();
        let mut table = SymbolTable::default();
        table.intern("BTCUSDT");
        let mut ids = ExecutionIds::from_records(&replay, now).unwrap();
        let mut callbacks = CallbackHost::new(CallbackExecution::Embedded, &[], &[]).unwrap();
        let (mut risk, _) = crate::tests::MockRisk::with(crate::tests::allow_all());
        let account = AccountView {
            exact_amounts: None,
            equity_usdt: 1000.0,
            available_usdt: 1000.0,
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        };
        let recovered = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &replay,
            &crate::order_dispatch::OrderDispatches::replay(&replay).unwrap(),
            &table,
            &mut ids,
            now,
            &mut callbacks,
            &account,
            &mut risk,
            None,
        )
        .await
        .unwrap();
        assert!(!recovered.latched);
        let expected = engine_types::risk::ClosedTradeRow {
            unpriced: Some(UnpricedTradeReason::SettlementAsset),
            net_usdt_exact: None,
            closed_ms: now - 1,
            net_usdt: 0.0,
        };
        assert_eq!(risk.rolling_loss_rows(), vec![expected.clone()]);
        let rows = engine_wal::replay(&path)
            .unwrap()
            .into_iter()
            .map(|(_, row)| row)
            .collect::<Vec<_>>();
        assert!(rows.iter().any(|row| matches!(row, WalRecord::RecoveredFill { exec_id, .. } if exec_id == "native-close-during-recovery")));
        let (mut restarted_risk, _) = crate::tests::MockRisk::with(crate::tests::allow_all());
        RecoveryOutcome::replay(&rows, &mut restarted_risk, now).unwrap();
        assert_eq!(restarted_risk.rolling_loss_rows(), vec![expected]);
    }
}
