use super::*;

// Frozen equivalence reference: keep its decisions and operation order unchanged.
impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    fn reference_observe_virtual_stops(&mut self, event: &MarketEvent) -> Result<(), EngineError> {
        let (symbol, bid, ask) = match event {
            MarketEvent::Quote { symbol, quote } => (*symbol, quote.bid_px, quote.ask_px),
            MarketEvent::Depth { symbol, depth } => {
                let quote = depth.quote();
                (*symbol, quote.bid_px, quote.ask_px)
            }
            MarketEvent::Trades { symbol, trades } => (*symbol, trades.last_px, trades.last_px),
            MarketEvent::Ticker { symbol, ticker } => (*symbol, ticker.last_px, ticker.last_px),
            MarketEvent::FeedReset { .. } => return Ok(()),
        };
        if !self.instrument_specs.contains_key(&symbol) {
            return Ok(());
        }
        let state = self.books.attribution.snapshot();
        for row in state.positions.iter().filter(|row| row.symbol == symbol) {
            if self.portfolio_controls.emergencies.contains_key(&symbol) {
                continue;
            }
            let Some(stop) = &row.stop_px else { continue };
            let value = if row.signed_qty.is_positive() {
                bid
            } else {
                ask
            };
            if !value.is_finite() || value <= 0.0 {
                continue;
            }
            let price =
                Exact::from_legacy_f64(value).map_err(|e| EngineError::State(e.to_string()))?;
            let triggered = if row.signed_qty.is_positive() {
                &price <= stop
            } else {
                &price >= stop
            };
            if triggered {
                self.request_portfolio_exit(
                    row.strategy,
                    symbol,
                    if row.signed_qty.is_positive() {
                        Side::Buy
                    } else {
                        Side::Sell
                    },
                    Exact::zero(),
                    Some(price),
                )?;
            }
        }
        Ok(())
    }
    async fn reference_service_portfolio_controls(&mut self) -> Result<(), EngineError> {
        self.reference_advance_portfolio_controls().await?;
        if self.portfolio_dirty && self.dispatches.write.is_none() {
            let barrier = self.begin_dispatch_barrier()?;
            self.portfolio_dirty = false;
            self.dispatches
                .begin(crate::order_dispatch::DispatchWrite::Portfolio, barrier);
        }
        Ok(())
    }
    async fn reference_advance_portfolio_controls(&mut self) -> Result<(), EngineError> {
        if self.dispatches.write.is_some() {
            return Ok(());
        }
        if self.portfolio_dirty {
            let barrier = self.begin_dispatch_barrier()?;
            self.portfolio_dirty = false;
            self.dispatches
                .begin(crate::order_dispatch::DispatchWrite::Portfolio, barrier);
            return Ok(());
        }
        if let Some((symbol, price)) = self
            .portfolio_controls
            .native_pending
            .iter()
            .next()
            .map(|(symbol, price)| (*symbol, price.clone()))
        {
            return self.start_portfolio_emergency(
                symbol,
                price,
                PortfolioEmergencyReason::NativeClose,
            );
        }
        let mut controls: Vec<_> = self
            .portfolio_controls
            .emergencies
            .values()
            .map(|state| (state.id, Some(state.clone()), None))
            .chain(
                self.portfolio_controls
                    .exits
                    .values()
                    .map(|state| (state.id, None, Some(state.clone()))),
            )
            .collect();
        controls.sort_by_key(|(id, _, _)| (*id <= self.portfolio_cursor, *id));
        for (id, emergency, exit) in controls.into_iter().take(MAX_ORDERS_PER_BATCH) {
            self.portfolio_cursor = id;
            if let Some(state) = emergency {
                self.service_portfolio_emergency(state).await?;
            }
            if let Some(state) = exit {
                if !self
                    .portfolio_controls
                    .emergencies
                    .contains_key(&state.symbol)
                {
                    self.service_portfolio_exit(state).await?;
                }
            }
            if self.portfolio_dirty || self.dispatches.write.is_some() {
                break;
            }
        }
        Ok(())
    }
}

type TestEngine = Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;

fn exit(id: u64, strategy: u16, symbol: u16) -> PortfolioExit {
    PortfolioExit {
        id,
        strategy: StrategyId(strategy),
        symbol: SymbolId(symbol),
        position_side: Side::Buy,
        target_remaining: Exact::zero(),
        trigger_price: Some(Exact::from_i64(90)),
        started_ms: clock::wall_ms(),
        attempt: 0,
        order_id: None,
    }
}

fn emergency(id: u64, symbol: u16) -> PortfolioEmergency {
    PortfolioEmergency {
        id,
        symbol: SymbolId(symbol),
        reference_price: Exact::from_i64(100),
        reason: PortfolioEmergencyReason::NativeClose,
        phase: PortfolioEmergencyPhase::ResolveOrders,
        started_ms: clock::wall_ms(),
        attempt: 0,
        order_id: None,
    }
}

fn observation(engine: &TestEngine, start: usize) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "wal": &engine.wal.snapshot_records()[start..],
        "controls": engine.portfolio_controls.snapshot(),
        "cursor": engine.portfolio_cursor, "dirty": engine.portfolio_dirty,
        "barrier_pending": engine.dispatches.write.is_some(),
        "repairs": engine.stop_repairs_pending, "may_open": engine.may_open,
        "pending_actions": engine.host.pending.iter().map(|pending| &pending.action).collect::<Vec<_>>(),
    })).unwrap()
}

async fn control_case(case: &str, reference: bool) -> (Option<String>, Vec<u8>) {
    let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
    engine.books.market.add_symbol("BTCUSDT");
    engine.portfolio_dirty = false;
    engine.books.account.positions.clear();
    for symbol in ["ETHUSDT", "SOLUSDT"] {
        engine.books.market.add_symbol(symbol);
    }
    match case {
        "dirty" | "dirty_before_native" => engine.portfolio_dirty = true,
        "dispatch_pending" => {
            engine.portfolio_dirty = true;
            engine.dispatches.write = Some(crate::order_dispatch::DispatchWrite::Portfolio);
        }
        _ => {}
    }
    if matches!(
        case,
        "native_pending" | "dirty_before_native" | "native_before_exit" | "native_append_failure"
    ) {
        engine
            .portfolio_controls
            .native_pending
            .insert(SymbolId(0), Exact::from_i64(100));
    }
    if matches!(
        case,
        "exit"
            | "native_before_exit"
            | "ordered_controls"
            | "busy_emergency"
            | "control_append_failure"
            | "control_barrier_failure"
    ) {
        engine
            .portfolio_controls
            .apply(&WalRecord::PortfolioExitChanged {
                state: exit(1, 0, 1),
            })
            .unwrap();
    }
    if matches!(case, "emergency" | "ordered_controls" | "busy_emergency") {
        engine
            .portfolio_controls
            .apply(&WalRecord::PortfolioEmergencyChanged {
                state: emergency(if case == "emergency" { 1 } else { 2 }, 0),
            })
            .unwrap();
    }
    if case == "ordered_controls" {
        engine.portfolio_cursor = 1;
    }
    if case == "busy_emergency" {
        engine.busy_symbols.insert(SymbolId(0), 1);
        engine.portfolio_cursor = 1;
    }
    if case == "native_append_failure" {
        engine.wal.fail_append("portfolio_emergency_changed");
    }
    if case == "control_append_failure" {
        engine.wal.fail_append("portfolio_exit_completed");
    }
    if case == "control_barrier_failure" {
        engine.wal.fail_barrier_after = Some("portfolio_exit_completed");
    }
    let start = records.lock().unwrap().len();
    let result = if reference {
        engine.reference_service_portfolio_controls().await
    } else {
        engine.service_portfolio_controls().await
    };
    let error = result.err().map(|error| error.to_string());
    let new_records = &engine.wal.snapshot_records()[start..];
    match case {
        "empty" | "dispatch_pending" | "dirty" | "dirty_before_native" => {
            assert!(new_records.is_empty())
        }
        "native_pending" | "native_before_exit" => assert!(
            matches!(new_records.first(), Some(WalRecord::PortfolioEmergencyChanged { state }) if state.id > 0)
        ),
        "exit" | "busy_emergency" => assert!(matches!(
            new_records.first(),
            Some(WalRecord::PortfolioExitCompleted { id: 1, .. })
        )),
        "emergency" | "ordered_controls" => assert!(
            matches!(new_records.first(), Some(WalRecord::PortfolioEmergencyChanged { state }) if state.id == (if case == "emergency" { 1 } else { 2 }) && state.phase == PortfolioEmergencyPhase::CloseNet)
        ),
        _ => {}
    }
    if matches!(case, "dirty" | "dirty_before_native") {
        assert!(engine.dispatches.write.is_some());
    }
    (error, observation(&engine, start))
}

#[tokio::test(start_paused = true)]
async fn flat_account_control_work_preserves_ordered_records_barriers_and_errors() {
    let _clock =
        engine_types::clock::install_virtual(1_800_000_000_000_000_000, 1_000_000_000).unwrap();
    for case in [
        "empty",
        "dirty",
        "dispatch_pending",
        "dirty_before_native",
        "native_pending",
        "native_before_exit",
        "exit",
        "emergency",
        "ordered_controls",
        "busy_emergency",
        "native_append_failure",
        "control_append_failure",
        "control_barrier_failure",
    ] {
        let expected = control_case(case, true).await;
        let actual = control_case(case, false).await;
        assert_eq!(actual, expected, "case {case}");
        if case.ends_with("failure") {
            assert!(actual.0.is_some(), "{case}");
        }
    }
}

async fn virtual_stop_case(case: &str, reference: bool) -> (Option<String>, Vec<u8>) {
    let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
    engine.books.market.add_symbol("BTCUSDT");
    engine.portfolio_dirty = false;
    engine
        .instrument_specs
        .insert(SymbolId(0), crate::tests::shared_sleeves::spec());
    engine.books.market.add_symbol("ETHUSDT");
    let mut state = engine_types::portfolio::PortfolioState {
        schema_version: 2,
        ..Default::default()
    };
    if case != "empty" {
        for (strategy, symbol, qty, stop) in [
            (0, 0, "1", "90"),
            (1, 0, "-1", "110"),
            (2, 1, "1", "90"),
            (3, 0, "1", "90"),
        ] {
            state
                .positions
                .push(engine_types::portfolio::PortfolioPosition {
                    strategy: StrategyId(strategy),
                    symbol: SymbolId(symbol),
                    signed_qty: qty.parse().unwrap(),
                    entry_value: None,
                    stop_px: Some(stop.parse().unwrap()),
                    settlement_asset: AssetId::Unknown,
                });
        }
    }
    engine.books.attribution = crate::attribution::Attribution::restore(&state).unwrap();
    if case == "missing_metadata" {
        engine.instrument_specs.clear();
    }
    if case == "emergency" {
        engine
            .portfolio_controls
            .apply(&WalRecord::PortfolioEmergencyChanged {
                state: emergency(1, 0),
            })
            .unwrap();
    }
    if case == "existing_exit" {
        engine
            .portfolio_controls
            .apply(&WalRecord::PortfolioExitChanged {
                state: exit(1, 0, 0),
            })
            .unwrap();
    }
    if case == "append_failure" {
        engine.wal.fail_append("portfolio_exit_changed");
    }
    if case == "capacity_after_first" {
        let mut base = engine.rotation_base(clock::wall_ms());
        let WalRecord::SegmentBase {
            portfolio_control, ..
        } = &mut base
        else {
            unreachable!()
        };
        portfolio_control.next_id = u64::MAX - 1;
        engine.portfolio_controls.apply(&base).unwrap();
    }
    let (bid, ask) = match case {
        "untriggered" => (100.0, 101.0),
        "invalid_price" => (f64::NAN, -1.0),
        _ => (80.0, 120.0),
    };
    let event = if case == "reset" {
        MarketEvent::FeedReset {
            recv_ns: clock::now_ns(),
        }
    } else {
        MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: engine_types::Quote {
                bid_px: bid,
                ask_px: ask,
                ..Default::default()
            },
        }
    };
    let start = records.lock().unwrap().len();
    let error = if reference {
        engine.reference_observe_virtual_stops(&event)
    } else {
        engine.observe_virtual_stops(&event)
    }
    .err()
    .map(|e| e.to_string());
    if case == "triggered" {
        let owners = engine.wal.snapshot_records()[start..]
            .iter()
            .filter_map(|record| match record {
                WalRecord::PortfolioExitChanged { state } => {
                    Some((state.strategy, state.position_side))
                }
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(
            owners,
            [
                (StrategyId(0), Side::Buy),
                (StrategyId(1), Side::Sell),
                (StrategyId(3), Side::Buy)
            ]
        );
    }
    if case == "capacity_after_first" {
        assert!(error
            .as_deref()
            .unwrap()
            .contains("portfolio control ID capacity exhausted"));
        assert!(
            matches!(&engine.wal.snapshot_records()[start..], [WalRecord::PortfolioExitChanged { state }] if state.strategy == StrategyId(0))
        );
    }
    (error, observation(&engine, start))
}

#[tokio::test(start_paused = true)]
async fn virtual_stop_targeted_reads_preserve_owner_order_and_refusals() {
    let _clock =
        engine_types::clock::install_virtual(1_800_000_000_000_000_000, 1_000_000_000).unwrap();
    for case in [
        "empty",
        "triggered",
        "untriggered",
        "invalid_price",
        "missing_metadata",
        "emergency",
        "existing_exit",
        "reset",
        "append_failure",
        "capacity_after_first",
    ] {
        let expected = virtual_stop_case(case, true).await;
        let actual = virtual_stop_case(case, false).await;
        assert_eq!(actual, expected, "case {case}");
        if case == "append_failure" {
            assert!(actual.0.is_some());
        }
    }
}
