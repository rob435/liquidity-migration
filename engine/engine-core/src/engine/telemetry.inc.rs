impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Write down what any position that just closed made.
    ///
    /// Drained whether or not a file was configured: the list would otherwise
    /// grow for the life of a process nobody asked to report on itself.
    fn record_trades(&mut self) {
        let closed = self.fills.take_closed();
        if closed.is_empty() {
            return;
        }
        if let Some(trades) = self.trades.as_mut() {
            trades.write(&closed);
        }
    }

    /// Write the heartbeat file, when one was asked for and its own cadence
    /// has come round.
    ///
    /// Nothing here returns an error, because there is nothing an error here
    /// should change: the file is how something outside tells whether this
    /// engine is well, and an engine that stopped trading because it could
    /// not describe itself would be a worse answer than one nobody can see.
    fn beat(&mut self, now_ns: u64) {
        let Engine {
            heartbeat,
            ledger,
            fills,
            names,
            may_open,
            private_stream_ready,
            events_seen,
            orders_sent,
            account,
            market,
            strategies,
            attribution,
            orders,
            covers,
            amends_confirmed,
            amends_pulled_unconfirmed,
            stream_resets,
            runtime_entries_enabled,
            runtime_control_requests,
            runtime_control_consumed,
            ..
        } = self;
        let Some(heartbeat) = heartbeat.as_mut() else {
            return;
        };
        if !heartbeat.due(now_ns) {
            return;
        }
        // Why each asked-for name is not being opened, straight from the
        // strategies. This is read-only operator evidence for the native
        // reducer's desired entry.
        // Strategy identity is part of the key: two sleeves may ask for the
        // same symbol and need their own answer. Within one sleeve the first
        // reason wins, so its kernel refusal still outranks a planner skip.
        let blockers = named_entry_blockers(strategies, names);
        let strategy_errors = named_strategy_errors(strategies, names);
        let strategy_entries_enabled: Vec<(String, bool)> = strategies
            .iter()
            .enumerate()
            .filter_map(|(index, strategy)| {
                let id = u16::try_from(index).ok()?;
                Some((
                    names.get(index)?.clone(),
                    strategy.configured_entries_enabled()
                        && runtime_entries_enabled.get(&id).copied().unwrap_or(true),
                ))
            })
            .collect();
        let pending_flatten_requests: Vec<(String, String)> = runtime_control_requests
            .iter()
            .filter(|request| {
                matches!(
                    request.command,
                    engine_types::RuntimeControlCommand::FlattenDirectional
                ) && !runtime_control_consumed
                    .contains(&(request.strategy.0, request.request_id.clone()))
            })
            .filter_map(|request| {
                Some((
                    names.get(usize::from(request.strategy.0))?.clone(),
                    request.request_id.clone(),
                ))
            })
            .collect();
        let working_entries: Vec<(String, String)> = orders
            .opening_symbols()
            .chain(covers.opening_symbols())
            .filter_map(|(strategy, symbol)| {
                Some((
                    names.get(usize::from(strategy.0))?.clone(),
                    market.table.name(symbol).to_string(),
                ))
            })
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        // Named because outside observers and compatibility clients do not
        // share the engine's numeric symbol table. Flat rows are dropped as
        // reader of this view drops them: flat is not a holding.
        let holdings: Vec<(String, engine_types::Side, f64, f64, Option<String>)> = account
            .positions
            .iter()
            .filter(|p| p.qty > 0.0)
            .map(|p| {
                (
                    market.table.name(p.symbol).to_string(),
                    p.side,
                    p.qty,
                    p.entry_px,
                    attribution
                        .sole_owner(p.symbol)
                        .filter(|owner| {
                            let venue_signed = match p.side {
                                engine_types::Side::Buy => p.qty,
                                engine_types::Side::Sell => -p.qty,
                            };
                            (attribution.signed(*owner, p.symbol) - venue_signed).abs() < 1e-9
                        })
                        .and_then(|owner| names.get(usize::from(owner.0)))
                        .cloned(),
                )
            })
            .collect();
        // Rolled up once, here, rather than kept as a running total: the
        // per-sleeve rows are what the ledger is for, and adding them up is
        // cheaper than keeping a second copy correct.
        let costs = fills.total();
        let effective_may_open = *may_open && *private_stream_ready;
        // How far the venue's clock sits from this box's, read off the
        // freshest quote: its venue stamp against the wall clock, minus the
        // time it has spent here since the socket read. Both clocks are
        // sampled together, here, where the number is made. A drifting box
        // makes every venue-stamp comparison quietly wrong, and nothing else
        // measures that.
        let wall_ts_ms = clock::wall_ms();
        let venue_clock_offset_ms = market
            .quotes
            .iter()
            .filter(|quote| quote.venue_ts_ms > 0 && quote.recv_ns > 0)
            .max_by_key(|quote| quote.recv_ns)
            .map(|quote| {
                venue_minus_local_ms(quote.venue_ts_ms, quote.recv_ns, now_ns, wall_ts_ms)
            });
        heartbeat.write(
            now_ns,
            &heartbeat::Facts {
                may_open: effective_may_open,
                market_events: *events_seen,
                orders_sent: *orders_sent,
                strategies: names,
                strategy_entries_enabled: &strategy_entries_enabled,
                pending_flatten_requests: &pending_flatten_requests,
                decide: ledger.quantiles(Segment::Decide),
                durable: ledger.quantiles(Segment::Durable),
                wire: ledger.quantiles(Segment::Wire),
                ack: ledger.quantiles(Segment::Ack),
                dispatch_queue: ledger.quantiles(Segment::DispatchQueue),
                venue_task: ledger.quantiles(Segment::VenueTask),
                core_resume: ledger.quantiles(Segment::CoreResume),
                end_to_end: ledger.quantiles(Segment::EndToEnd),
                barrier_wait: ledger.quantiles(Segment::BarrierWait),
                quota_hold: ledger.quantiles(Segment::QuotaHold),
                amends_confirmed: *amends_confirmed,
                amends_pulled_unconfirmed: *amends_pulled_unconfirmed,
                stream_resets: *stream_resets,
                // The monotonic clock's origin is this process's first tick,
                // so "now" on it is the age of the run.
                uptime_s: now_ns / 1_000_000_000,
                venue_clock_offset_ms,
                equity_usdt: account.equity_usdt,
                available_usdt: account.available_usdt,
                // The age, not the stamp: this engine's clock is monotonic
                // and means nothing outside this process.
                account_age_ns: (account.observed_ns != 0)
                    .then(|| now_ns.saturating_sub(account.observed_ns)),
                holdings: &holdings,
                entry_blockers: &blockers,
                strategy_errors: &strategy_errors,
                working_entries: &working_entries,
                costs: &costs,
            },
        );
    }

    pub fn strategy_names(&self) -> &[String] {
        &self.names
    }

    /// What the fills have cost so far this run.
    pub fn fills(&self) -> &Fills {
        &self.fills
    }
}
