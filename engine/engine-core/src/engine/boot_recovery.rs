use super::*;

mod configuration;
mod inputs;
mod reservations;
use configuration::{restore_configuration, ConfiguredStrategies};
use inputs::{restore_strategy_inputs, RecoveredStrategyInputs};
use reservations::restore_order_reservations;

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
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
    ) -> Result<Self, EngineError> {
        Self::boot_as_with_execution(
            settings,
            config_sha256,
            wal,
            risk,
            venue,
            strategies,
            sleeves,
            replayed,
            crate::strategy_process::host::CallbackExecution::Embedded,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn boot_as_isolated(
        settings: &EngineSection,
        config_sha256: &str,
        wal: W,
        risk: R,
        venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
        executable: std::path::PathBuf,
    ) -> Result<Self, EngineError> {
        Self::boot_as_with_execution(
            settings,
            config_sha256,
            wal,
            risk,
            venue,
            strategies,
            sleeves,
            replayed,
            crate::strategy_process::host::CallbackExecution::Isolated { executable },
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    async fn boot_as_with_execution(
        settings: &EngineSection,
        config_sha256: &str,
        mut wal: W,
        mut risk: R,
        mut venue: V,
        strategies: Vec<Box<dyn Strategy>>,
        sleeves: &[String],
        replayed: &[WalRecord],
        execution: crate::strategy_process::host::CallbackExecution,
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

        let require_exact_instruments = matches!(
            &execution,
            crate::strategy_process::host::CallbackExecution::Isolated { .. }
        );
        let mut callbacks = if require_exact_instruments {
            let reader = wal.callback_reader()?.ok_or_else(|| {
                EngineError::Boot("isolated callbacks require a durable callback reader".into())
            })?;
            crate::strategy_process::host::CallbackHost::new_paged(
                execution,
                &strategies,
                replayed,
                reader,
            )
        } else {
            crate::strategy_process::host::CallbackHost::new(execution, &strategies, replayed)
        }
        .map_err(EngineError::Boot)?;
        if callbacks.isolated() {
            let reader = wal.callback_reader()?.ok_or_else(|| {
                EngineError::Boot("isolated callbacks require an order source reader".into())
            })?;
            callbacks
                .order_news
                .attach(reader, replayed, strategies.len())
                .map_err(EngineError::Boot)?;
        }
        let dispatches =
            crate::order_dispatch::OrderDispatches::replay(replayed).map_err(EngineError::Boot)?;
        let boot_ms = clock::wall_ms();
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
            &market.table,
            &mut recovered_exec_ids,
            boot_ms,
            &mut callbacks,
        )
        .await?;
        let effective_owned: Vec<WalRecord>;
        let effective: &[WalRecord] = if recovery.records.is_empty() {
            replayed
        } else {
            effective_owned = replayed.iter().cloned().chain(recovery.records).collect();
            &effective_owned
        };

        let mut orders = LedgerOfOrders::try_from_records(effective).map_err(EngineError::Boot)?;
        // Same records, same join: a restart must not forget whose
        // position is whose, or the other sleeve trades straight into it.
        let mut attribution =
            Attribution::try_from_records(effective).map_err(EngineError::Boot)?;
        let mut fills = Fills::default();
        let already_closed = fills.try_seed_lots(effective).map_err(EngineError::Boot)?;
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
        let logged_exposure =
            crate::reconcile::physical_exposure(effective).map_err(EngineError::Boot)?;
        let intended_stops =
            crate::reconcile::intended_stops(effective).map_err(EngineError::Boot)?;
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
        // Legacy fills without an execution id retain their field-based overlap key.
        let mut recent_fills: VecDeque<(String, i64, f64)> = effective
            .iter()
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
            .collect();
        while recent_fills.len() > RECENT_FILLS_KEPT {
            recent_fills.pop_front();
        }

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
            effective,
            &account,
            &market.table,
            &rules,
            &instrument_specs,
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
            let owners = callbacks.isolated().then(|| {
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
            let sequence = wal.append(&ended)?;
            if let Some(owners) = owners {
                callbacks
                    .order_news
                    .record(sequence, &owners)
                    .map_err(EngineError::State)?;
            }
            orders.try_apply(&ended).map_err(EngineError::State)?;
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
        let registry = restore_order_reservations(&mut risk, &orders, boot_ms, &account, &working)?;
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
            portfolio_controls: crate::portfolio_control::PortfolioControls::replay(effective)
                .map_err(EngineError::Boot)?,
            portfolio_dirty: false,
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
            account_refresh_requested_after: None,
            account_refresh_started_ns: boot_account_started_ns,
            rotate_after_bytes: settings.wal_rotate_mb.saturating_mul(1024 * 1024),
            max_quote_age_ns: settings.max_quote_age_ms.saturating_mul(1_000_000),
            next_order_n: 0,
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
    pub(super) async fn recover_missed_fills(
        wal: &mut W,
        venue: &mut V,
        replayed: &[WalRecord],
        table: &SymbolTable,
        execution_ids: &mut ExecutionIds,
        fresh_start_ms: i64,
        callbacks: &mut crate::strategy_process::host::CallbackHost,
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
                ..
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
        let mut recovered_orders =
            LedgerOfOrders::try_from_records(replayed).map_err(EngineError::Boot)?;
        let mut recovered_attribution =
            Attribution::try_from_records(replayed).map_err(EngineError::Boot)?;
        let strategy_names = replayed
            .iter()
            .rev()
            .find_map(|record| match record {
                WalRecord::Names { strategies, .. } | WalRecord::SegmentBase { strategies, .. } => {
                    Some(strategies.clone())
                }
                _ => None,
            })
            .unwrap_or_default();
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
            if let Err(reason) = recovered_orders
                .validate_fill(&exec.client_order_id, symbol, exec.side, exec.qty, exec.px)
                .and_then(|()| {
                    recovered_orders.validate_fill_quantities(
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
            let owner = recovered_orders.owner_of(&client_order_id);
            let allocation = match recovered_attribution.prepare_portfolio_recovered_for_order(
                recovered_orders
                    .orders
                    .get(&client_order_id)
                    .map(|order| &order.request),
                &strategy_names,
                &record,
            ) {
                Ok(allocation) => allocation,
                Err(reason) => {
                    unknown_findings.push(Self::untrusted_fill_line(
                        &dedup_id,
                        &client_order_id,
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
            };
            if let (
                Some(prepared),
                WalRecord::RecoveredFill {
                    allocation: recorded,
                    ..
                },
            ) = (&allocation, &mut record)
            {
                if callbacks.isolated()
                    || prepared.allocation.policy
                        == engine_types::execution_allocation::AllocationPolicy::EmergencyNetFifo
                {
                    *recorded = Some(Box::new(prepared.allocation.clone()));
                }
            }
            if callbacks.isolated() {
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
            let sequence = wal.append(&record)?;
            if let WalRecord::RecoveredFill {
                callbacks: Some(owners),
                ..
            } = &record
            {
                callbacks
                    .order_news
                    .record(sequence, &owners.owners)
                    .map_err(EngineError::State)?;
            }
            if let Some(allocation) = allocation {
                recovered_attribution
                    .commit_portfolio_fill(allocation)
                    .map_err(EngineError::State)?;
            }
            out.push(record.clone());
            execution_ids.insert(dedup_id, now_ms);
            recovered_orders
                .try_apply(&record)
                .map_err(EngineError::State)?;
            if owner.is_some() {
                if let Some(order) = recovered_orders.orders.get(&client_order_id) {
                    recovered_attribution.remember_order_stop(&order.request);
                }
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
        specs: &std::collections::BTreeMap<SymbolId, engine_types::numeric::ExactInstrumentSpec>,
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
    use crate::strategy_process::host::{CallbackExecution, CallbackHost};
    use engine_types::strategy_process::CallbackEvent;

    #[tokio::test]
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
            WalRecord::Names {
                strategies: vec![strategies[0].name().into()],
                symbols: vec!["BTCUSDT".into()],
            },
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
            wal.append(row).unwrap();
        }
        wal.barrier().unwrap();
        let mut callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &strategies,
            &replay,
        )
        .unwrap();
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
        let outcome = Engine::<
            engine_wal::WalWriter,
            crate::tests::MockRisk,
            crate::tests::MockVenue,
        >::recover_missed_fills(
            &mut wal,
            &mut venue,
            &replay,
            &table,
            &mut ids,
            now,
            &mut callbacks,
        )
        .await
        .unwrap();
        assert!(
            callbacks.order_news.unread_for(StrategyId(0)),
            "initial recovery fill has no durable callback retry owner"
        );
        assert!(
            matches!(&outcome.records[0], WalRecord::RecoveredFill { callbacks: Some(owners), .. } if owners.owners == [StrategyId(0)])
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
