use super::*;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Write down what any position that just closed made, and tell the risk
    /// kernel what it lost.
    ///
    /// Drained whether or not a file was configured: the list would otherwise
    /// grow for the life of a process nobody asked to report on itself. The
    /// kernel hears every priced trip for the same reason — the rolling loss
    /// window counts what this engine did, not what somebody chose to file.
    /// A native close without valued net retains accounting debt in the window.
    pub(super) fn record_trades(&mut self) {
        let closed = self.fills.take_closed();
        if closed.is_empty() {
            return;
        }
        for trade in &closed {
            if let Some(row) = trade.loss_row() {
                if let Some(canary) = self.canary.as_mut() {
                    canary.observe_closed_trip(&row);
                }
                self.risk.observe_closed_trade(row);
            }
        }
        // A closed round trip is the only thing that can trip the window;
        // ageing it only ever lets it go. An opening already queued behind
        // the trip must not be sent under the permission it lost.
        let tripped = self
            .risk
            .rolling_loss()
            .is_some_and(|window| window.tripped);
        if tripped && !self.rolling_loss_tripped {
            self.supersede_openings();
        }
        self.rolling_loss_tripped = tripped;
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
    pub(super) fn beat(&mut self, now_ns: u64) {
        let Engine {
            heartbeat,
            host,
            books,
            ledger,
            fills,
            may_open,
            private_stream_ready,
            private_stream_unready_since_ns,
            events_seen,
            orders_sent,
            risk,
            amends_confirmed,
            amends_pulled_unconfirmed,
            stream_resets,
            runtime_control_requests,
            runtime_control_consumed,
            ..
        } = self;
        let StrategyHost {
            strategies,
            names,
            callbacks,
            entries_enabled: runtime_entries_enabled,
            ..
        } = host;
        let Books {
            account,
            market,
            attribution,
            orders,
            covers,
            ..
        } = books;
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
        let strategy_errors = named_strategy_errors(strategies, names, &callbacks.faults);
        // The rolling loss window is an account-wide gate the kernel applies
        // to every entry, so a sleeve whose own switches are all on is still
        // opening nothing while it is tripped. Reporting the switches alone
        // reads as "trading normally" on an account that is refusing.
        let rolling_loss = risk.rolling_loss();
        let rolling_loss_tripped = rolling_loss.as_ref().is_some_and(|view| view.tripped);
        let strategy_entries_enabled: Vec<(String, bool)> = strategies
            .iter()
            .enumerate()
            .filter_map(|(index, strategy)| {
                let id = StrategyId(u16::try_from(index).ok()?);
                Some((
                    names.get(index)?.clone(),
                    !rolling_loss_tripped
                        && strategy.configured_entries_enabled()
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
                    .contains(&(request.strategy, request.request_id.clone()))
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
        // The latch and the readiness bit are published apart because they
        // are different faults with different operators. `may_open` false is
        // permanent until somebody clears it; readiness false is a sweep in
        // progress, and on a venue whose execution history is the authority
        // that happens on a timer while the socket is healthy. Rolling them
        // into one field made every paced re-read look like a latched engine.
        let private_stream_unready_ms = private_stream_unready_since_ns
            .map(|since_ns| now_ns.saturating_sub(since_ns) / 1_000_000);
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
                may_open: *may_open,
                private_stream_ready: *private_stream_ready,
                private_stream_unready_ms,
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
                account_metrics: Some(account),
                entry_blockers: &blockers,
                strategy_errors: &strategy_errors,
                working_entries: &working_entries,
                costs: &costs,
                rolling_loss,
            },
        );
    }

    pub fn strategy_names(&self) -> &[String] {
        &self.host.names
    }

    /// What the fills have cost so far this run.
    pub fn fills(&self) -> &Fills {
        &self.fills
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NANOS_PER_SEC: u64 = 1_000_000_000;

    #[tokio::test(start_paused = true)]
    async fn callback_process_faults_appear_under_their_configured_sleeve() {
        let path = crate::testpath::temp_path("heartbeat-callback-error");
        let params = toml::from_str("symbol='BTCUSDT'\nevery_s=60\nenabled=false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, _) = crate::tests::callback_test_fixture(vec![strategy]).await;
        engine
            .host
            .callbacks
            .faults
            .insert(StrategyId(0), "LONG filled state is invalid".into());
        engine.write_heartbeat(Heartbeat::with_every(
            path.to_path_buf(),
            None,
            None,
            Duration::from_millis(1),
        ));
        engine.beat(clock::now_ns());
        let fields: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            fields["strategy_errors"],
            serde_json::json!([{
                "strategy": "probe",
                "error": "LONG filled state is invalid",
            }])
        );
    }

    /// Incident `mexc-a361f5d18861421a`: MEXC resyncs on a 600 s timer while
    /// the socket is healthy, so a heartbeat that published `may_open &&
    /// private_stream_ready` told the fleet watchdog the engine was latched
    /// once every ten minutes, and the watchdog woke an on-call engineer.
    #[tokio::test(start_paused = true)]
    async fn a_paced_resync_does_not_publish_a_latched_engine() {
        let path = crate::testpath::temp_path("heartbeat-paced-resync");
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        engine.write_heartbeat(Heartbeat::with_every(
            path.to_path_buf(),
            None,
            None,
            Duration::from_millis(1),
        ));
        assert!(engine.may_open && engine.private_stream_ready);

        // What the venue sends on its timer with the socket still up.
        engine
            .take_update_ready(engine_types::OrderUpdate::StreamReset {
                recv_ns: clock::now_ns(),
            })
            .await
            .unwrap();
        assert!(
            engine.may_open && !engine.private_stream_ready,
            "a paced re-read must not touch the operator latch"
        );

        // The engine's clock is the real monotonic one, so the age is driven
        // through the beat's own stamp rather than a paused timer.
        let since_ns = engine
            .private_stream_unready_since_ns
            .expect("clearing readiness must stamp when the outage began");
        engine.beat(since_ns + 200 * NANOS_PER_SEC);
        let fields: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            fields["may_open"],
            serde_json::json!(true),
            "the sweep was published as a latched engine and paged CRITICAL"
        );
        assert_eq!(fields["private_stream_ready"], serde_json::json!(false));
        assert_eq!(
            fields["private_stream_unready_ms"],
            serde_json::json!(200_000),
            "the age a watcher thresholds on must be the age of the outage"
        );

        // A second reset before recovery is the same outage, not a fresh one:
        // a stream that never returns must keep ageing past any dwell.
        engine
            .take_update_ready(engine_types::OrderUpdate::StreamReset {
                recv_ns: clock::now_ns(),
            })
            .await
            .unwrap();
        assert_eq!(engine.private_stream_unready_since_ns, Some(since_ns));
        engine.beat(since_ns + 400 * NANOS_PER_SEC);
        let fields: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            fields["private_stream_unready_ms"],
            serde_json::json!(400_000),
            "a repeated reset restarted the clock and hid a stuck stream"
        );

        // Recovery clears both, so the next sweep starts its own clock.
        engine.restore_private_stream_ready();
        engine.beat(since_ns + 500 * NANOS_PER_SEC);
        let fields: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(fields["private_stream_ready"], serde_json::json!(true));
        assert_eq!(
            fields["private_stream_unready_ms"],
            serde_json::json!(null),
            "a recovered stream must not keep reporting an outage"
        );
    }
}
