use super::*;

#[tokio::test(start_paused = true)]
async fn legacy_owned_stops_use_matching_venue_entry_without_inventing_cost_basis() {
    for side in [Side::Buy, Side::Sell] {
        let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
        let mut state = engine.books.attribution.snapshot();
        let row = &mut state.positions[0];
        row.entry_value = None;
        row.signed_qty = if side == Side::Buy {
            Exact::one()
        } else {
            -Exact::one()
        };
        row.stop_px = Some(Exact::from_u64(if side == Side::Buy { 80 } else { 120 }));
        engine.books.attribution = crate::attribution::Attribution::restore(&state).unwrap();
        let position = &mut engine.books.account.positions[0];
        position.side = side;
        position.leverage = Some(10.0);
        let prior_records = engine.wal.snapshot_records().len();
        engine.cap_owned_stop_distances().unwrap();
        let capped = engine.books.attribution.snapshot();
        assert_eq!(
            capped.positions[0].stop_px,
            Some(Exact::from_u64(if side == Side::Buy { 95 } else { 105 }))
        );
        assert_eq!(capped.positions[0].entry_value, None);
        let mut replayed = crate::attribution::Attribution::restore(&state).unwrap();
        let records = engine.wal.snapshot_records();
        assert!(records[prior_records..]
            .iter()
            .any(|r| matches!(r, WalRecord::SleeveStopSet { .. })));
        for record in &records[prior_records..] {
            let record: WalRecord =
                serde_json::from_slice(&serde_json::to_vec(record).unwrap()).unwrap();
            replayed
                .apply_record(&record, &Default::default(), &[])
                .unwrap();
        }
        assert_eq!(replayed.snapshot(), capped);

        engine.books.attribution = crate::attribution::Attribution::restore(&state).unwrap();
        engine.books.account.positions[0].side = side.flipped();
        engine.cap_owned_stop_distances().unwrap();
        assert_eq!(engine.books.attribution.snapshot(), state);
        engine.books.account.positions[0].side = side;

        let mut shared = state.clone();
        let mut other = shared.positions[0].clone();
        other.strategy = StrategyId(1);
        shared.positions.push(other);
        engine.books.attribution = crate::attribution::Attribution::restore(&shared).unwrap();
        engine.cap_owned_stop_distances().unwrap();
        assert_eq!(engine.books.attribution.snapshot(), shared);

        engine.books.attribution = crate::attribution::Attribution::restore(&state).unwrap();
        engine.books.account.positions[0].qty = 2.0;
        engine.books.account.positions[0]
            .exact_amounts
            .as_deref_mut()
            .unwrap()
            .quantity = engine_types::numeric::ExactNumber::venue_decimal("2").unwrap();
        engine.cap_owned_stop_distances().unwrap();
        assert_eq!(
            engine.books.attribution.snapshot(),
            state,
            "foreign inventory must not supply a sleeve cost basis"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn held_lot_stops_tighten_to_leverage_and_liquidation_and_survive_replay() {
    use engine_types::numeric::ExactNumber;
    let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
    engine.books.account.positions[0].leverage = Some(10.0);
    engine.cap_owned_stop_distances().unwrap();
    let stop =
        |engine: &Engine<crate::tests::MockWal, engine_risk::Kernel, crate::tests::MockVenue>| {
            engine.books.attribution.snapshot().positions[0]
                .stop_px
                .clone()
                .unwrap()
        };
    assert_eq!(stop(&engine), Exact::parse_decimal("95").unwrap());
    let amounts = engine.books.account.positions[0]
        .exact_amounts
        .as_deref_mut()
        .unwrap();
    amounts.mark_price = Some(ExactNumber::venue_decimal("100").unwrap());
    amounts.liquidation_price = Some(ExactNumber::venue_decimal("98").unwrap());
    engine.cap_owned_stop_distances().unwrap();
    assert_eq!(stop(&engine), Exact::parse_decimal("99").unwrap());
    let state = engine.books.attribution.snapshot();
    let replayed = engine.books.attribution.replay_clone().unwrap();
    assert_eq!(replayed.snapshot(), state);
    engine.books.account.positions[0].leverage = Some(2.0);
    engine.books.account.positions[0]
        .exact_amounts
        .as_deref_mut()
        .unwrap()
        .liquidation_price = None;
    engine.cap_owned_stop_distances().unwrap();
    assert_eq!(
        stop(&engine),
        Exact::parse_decimal("99").unwrap(),
        "a relaxed venue limit must not loosen a durable stop"
    );
}

#[tokio::test(start_paused = true)]
async fn an_opposing_physical_position_still_bounds_owned_stop_distance_by_its_leverage() {
    let mut engine = crate::tests::shared_sleeves::exact_single_sleeve_engine("1", None).await;
    engine.books.account.positions[0].side = Side::Sell;
    engine.books.account.positions[0].stop_px = 110.0;
    engine.books.account.positions[0].leverage = Some(10.0);
    engine.cap_owned_stop_distances().unwrap();
    assert_eq!(
        engine.books.attribution.snapshot().positions[0].stop_px,
        Some(Exact::parse_decimal("95").unwrap())
    );
}

// Frozen equivalence reference: keep its decisions and operation order unchanged.
impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    async fn reference_enforce_position_stop_intent(&mut self) -> Result<(), EngineError> {
        if self.dispatches.write.is_some() {
            return Ok(());
        }
        let symbols: std::collections::BTreeSet<_> = self
            .books
            .account
            .positions
            .iter()
            .filter(|p| p.qty > 0.0)
            .map(|p| p.symbol)
            .chain(self.stop_repairs_pending.iter().copied())
            .collect();
        let mut repairs = Vec::new();
        let mut failures = Vec::new();
        for symbol in symbols {
            if self.busy_symbols.contains_key(&symbol) {
                continue;
            }
            if self.instrument_specs.contains_key(&symbol) {
                match self.native_stop_plan(symbol) {
                    Ok(NativeStopPlan::Required(stop)) => {
                        self.stop_repairs_pending.insert(symbol);
                        repairs.push(stop);
                    }
                    Ok(NativeStopPlan::Waiting) => {
                        self.stop_repairs_pending.insert(symbol);
                    }
                    Ok(NativeStopPlan::Satisfied) => {
                        self.stop_repairs_pending.remove(&symbol);
                    }
                    Err(reason) => {
                        self.stop_repairs_pending.insert(symbol);
                        failures.push(format!("{}: {reason}", symbol.0));
                    }
                }
            } else if let Some(position) = self
                .books
                .account
                .positions
                .iter()
                .find(|p| p.symbol == symbol && p.qty > 0.0)
            {
                if let Some(stop) = self
                    .intended_stops
                    .get(&symbol)
                    .filter(|s| s.side == position.side)
                {
                    repairs.push(DurableStop {
                        symbol,
                        side: position.side,
                        trigger_px: stop.trigger_px,
                        exact: None,
                    });
                } else if !position.stop_attached
                    || !position.stop_px.is_finite()
                    || position.stop_px <= 0.0
                {
                    failures.push(format!(
                        "{}: held position has no venue stop and no fill-owned durable stop intent",
                        symbol.0
                    ));
                }
            }
        }
        if !failures.is_empty() {
            self.may_open = false;
            self.portfolio_dirty = true;
            self.wal.append(&WalRecord::Reconciled {
                wall_ts_ms: clock::wall_ms(),
                findings: failures,
                may_open: false,
            })?;
        }
        repairs.truncate(MAX_ORDERS_PER_BATCH);
        self.queue_native_stops(repairs)?;
        self.service_portfolio_controls().await
    }
}

async fn enforcement_case(case: &str, reference: bool) -> (Option<String>, Vec<u8>) {
    let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
    engine.books.market.add_symbol("BTCUSDT");
    engine.books.account.positions.clear();
    engine.portfolio_dirty = false;
    engine.stop_repairs_pending.clear();
    if case != "empty" && case != "flat_pending_repair" {
        engine.books.account.positions = crate::tests::shared_sleeves::physical_long(1.0);
    }
    if matches!(
        case,
        "legacy_repair" | "append_failure" | "barrier_failure" | "busy" | "dispatch_pending"
    ) {
        engine.books.account.positions[0].stop_px = 0.0;
        engine.books.account.positions[0].stop_attached = false;
        engine.intended_stops.insert(
            SymbolId(0),
            reconcile::IntendedPositionStop {
                side: Side::Buy,
                trigger_px: 90.0,
            },
        );
    }
    match case {
        "missing_stop" => {
            engine.books.account.positions[0].stop_px = 0.0;
            engine.books.account.positions[0].stop_attached = false;
        }
        "mixed_repairs" => {
            engine.books.market.add_symbol("ETHUSDT");
            engine.books.account.positions[0].stop_px = 0.0;
            engine.books.account.positions[0].stop_attached = false;
            let mut other = engine.books.account.positions[0].clone();
            other.symbol = SymbolId(1);
            engine.books.account.positions.insert(0, other);
            engine.intended_stops.insert(
                SymbolId(1),
                reconcile::IntendedPositionStop {
                    side: Side::Buy,
                    trigger_px: 80.0,
                },
            );
        }
        "native_waiting" => {
            engine
                .instrument_specs
                .insert(SymbolId(0), crate::tests::shared_sleeves::spec());
            engine.private_stream_ready = false;
        }
        "flat_pending_repair" => {
            engine
                .instrument_specs
                .insert(SymbolId(0), crate::tests::shared_sleeves::spec());
            engine.stop_repairs_pending.insert(SymbolId(0));
        }
        "busy" => {
            engine.busy_symbols.insert(SymbolId(0), 1);
        }
        "dispatch_pending" => {
            engine.dispatches.write = Some(crate::order_dispatch::DispatchWrite::Portfolio);
        }
        "append_failure" => engine.wal.fail_append("stop_set"),
        "barrier_failure" => engine.wal.fail_barrier_after = Some("stop_set"),
        _ => {}
    }
    let record_start = records.lock().unwrap().len();
    let error = if reference {
        engine.reference_enforce_position_stop_intent().await
    } else {
        engine.enforce_position_stop_intent().await
    }
    .err()
    .map(|error| error.to_string());
    let stops = match &engine.dispatches.write {
        Some(crate::order_dispatch::DispatchWrite::Stop(stops)) => stops
            .iter()
            .map(|stop| serde_json::json!([stop.symbol, stop.side, stop.trigger_px, stop.exact]))
            .collect::<Vec<_>>(),
        _ => Vec::new(),
    };
    let write = match engine.dispatches.write {
        Some(crate::order_dispatch::DispatchWrite::Stop(_)) => "stop",
        Some(crate::order_dispatch::DispatchWrite::Portfolio) => "portfolio",
        None => "none",
        _ => panic!("unexpected dispatch kind"),
    };
    if case == "legacy_repair" {
        assert_eq!(stops.len(), 1);
        let durable = engine.dispatches.durable.recv().await.unwrap();
        engine
            .on_order_dispatch_durable(Some(durable))
            .await
            .unwrap();
        assert_eq!(engine.pending_mutations.len(), 1);
        let completion = engine.venue_completions.recv().await.unwrap();
        engine.take_venue_completion(completion).await.unwrap();
        assert!(records.lock().unwrap().iter().any(|record| matches!(record,
            WalRecord::Note { source, text } if source == "stop-supervisor" && text.contains("restored BTCUSDT Buy position stop"))));
    }
    if case == "missing_stop" {
        assert!(!engine.may_open);
        assert!(records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::Reconciled { findings, may_open: false, .. }
            if findings == &["0: held position has no venue stop and no fill-owned durable stop intent".to_string()])));
    }
    if case == "mixed_repairs" {
        let retained = records.lock().unwrap();
        assert!(matches!(
            &retained[record_start..],
            [
                WalRecord::Reconciled { .. },
                WalRecord::StopSet {
                    symbol: SymbolId(1),
                    trigger_px: 80.0,
                    ..
                }
            ]
        ));
        assert_eq!(stops.len(), 1);
    }
    let observed = serde_json::json!({
        "wal": &records.lock().unwrap()[record_start..], "dispatch_write": write, "stop_commands": stops,
        "may_open": engine.may_open, "dirty": engine.portfolio_dirty,
        "repairs": engine.stop_repairs_pending, "controls": engine.portfolio_controls.snapshot(),
    });
    (error, serde_json::to_vec(&observed).unwrap())
}

#[tokio::test(start_paused = true)]
async fn stop_enforcement_preserves_records_commands_and_errors_across_empty_and_held_states() {
    let _clock =
        engine_types::clock::install_virtual(1_800_000_000_000_000_000, 1_000_000_000).unwrap();
    for case in [
        "empty",
        "confirmed",
        "legacy_repair",
        "missing_stop",
        "mixed_repairs",
        "native_waiting",
        "flat_pending_repair",
        "busy",
        "dispatch_pending",
        "append_failure",
        "barrier_failure",
    ] {
        let expected = enforcement_case(case, true).await;
        let actual = enforcement_case(case, false).await;
        assert_eq!(actual, expected, "case {case}");
        if matches!(case, "append_failure" | "barrier_failure") {
            assert!(actual.0.is_some(), "{case}");
        }
    }
}
