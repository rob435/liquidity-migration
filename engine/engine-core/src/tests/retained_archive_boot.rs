use super::*;
use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};
use engine_types::strategy_process::{
    CallbackEvent, CallbackOrderOrigin, CallbackSourceFrontier, CallbackWalCursor,
};

fn decimal(value: &str) -> Exact {
    Exact::parse_decimal(value).unwrap()
}

fn probe() -> Box<dyn Strategy> {
    engine_strategies::build_strategy(
        "probe",
        StrategyId(0),
        &toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap(),
    )
    .unwrap()
}

fn late_fill(id: &str, time: i64) -> VenueExecution {
    VenueExecution {
        exec_id: "retained-limit-late-fill".into(),
        client_order_id: id.into(),
        symbol: "BTCUSDT".into(),
        side: Side::Buy,
        qty: 0.5,
        px: 100.0,
        fee: Some(0.001),
        is_maker: true,
        forced_close: None,
        venue_ts_ms: time,
        amounts: Some(ExecutionAmounts {
            quantity: ExactNumber::venue_decimal("0.5").unwrap(),
            price: ExactNumber::venue_decimal("100").unwrap(),
            fee: Some(AssetAmount {
                asset: AssetId::Named("USDT".into()),
                amount: ExactNumber::venue_decimal("0.001").unwrap(),
            }),
            settlement_asset: AssetId::Named("USDT".into()),
        }),
    }
}

async fn boot(
    path: &std::path::Path,
    execution: &VenueExecution,
) -> Engine<engine_wal::WalWriter, MockRisk, MockVenue> {
    let (wal, records) = crate::assembly::wal(path).unwrap();
    let (mut venue, sends) = MockVenue::new(tape(), &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), shared_sleeves::spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(vec![engine_types::PositionView {
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.5,
            entry_px: 100.0,
            exact_amounts: Some(Box::new(engine_types::risk::PositionAmounts {
                quantity: ExactNumber::venue_decimal("0.5").unwrap(),
                entry_price: ExactNumber::venue_decimal("100").unwrap(),
            })),
            stop_px: 90.0,
            exact_stop_px: Some(Box::new(decimal("90"))),
            stop_attached: true,
            leverage: Some(2.0),
        }]);
    *venue.executions.lock().unwrap() = Some(vec![execution.clone()]);
    let stops = venue.stops.clone();
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let (risk, _) = MockRisk::with(allow_all());
    let mut configured = settings();
    configured.wal_path = path.into();
    let result = Engine::boot_as_exact(
        &configured,
        "retained-archive-regression",
        wal,
        risk,
        venue,
        vec![probe()],
        &["probe".into()],
        &records,
    )
    .await;
    let engine = result.unwrap_or_else(|error| panic!("retained archive boot failed: {error}"));
    assert!(sends.lock().unwrap().is_empty());
    assert!(stops.lock().unwrap().is_empty());
    assert!(cancels.lock().unwrap().is_empty());
    assert!(amends.lock().unwrap().is_empty());
    engine
}

fn recovered_callbacks(wal: &mut engine_wal::WalWriter) -> Vec<CallbackEvent> {
    let mut reader = wal.callback_reader().unwrap().unwrap();
    let mut cursor = CallbackWalCursor {
        segment: 1,
        sequence: 1,
        offset: 8,
    };
    let mut events = Vec::new();
    while let Some(record) = reader.next(cursor).unwrap() {
        cursor = record.next;
        if let Some((owners, event)) = record.source {
            assert_eq!(owners, [StrategyId(0)]);
            if matches!(event, CallbackEvent::Order { .. }) {
                events.push(event);
            }
        }
    }
    events
}

#[tokio::test(start_paused = true)]
async fn embedded_boot_recovers_epoch_lineage_and_callbacks_from_a_heterogeneous_archive() {
    let now = recent_replay_ms();
    let archived_epoch = now - now.rem_euclid(1000) + 60_000;
    let id = format!("eng-{archived_epoch}-1");
    let (prior, _) = callback_test_fixture(vec![probe()]).await;
    let mut base = prior.rotation_base(now);
    drop(prior);
    if let WalRecord::SegmentBase {
        order_id_epoch_ms,
        execution_history_through_ms,
        strategy_callback_sources,
        ..
    } = &mut base
    {
        *order_id_epoch_ms = None;
        *execution_history_through_ms = Some(now - 1);
        *strategy_callback_sources = vec![CallbackSourceFrontier {
            strategy: StrategyId(0),
            cursor: CallbackWalCursor {
                segment: 1,
                sequence: 1,
                offset: 8,
            },
            accepted: None,
            latest: CallbackOrderOrigin {
                segment: 1,
                sequence: 4,
            },
        }];
    } else {
        unreachable!();
    }
    let path = temp_path("retained-heterogeneous-boot");
    let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
    let request = OrderRequest {
        client_order_id: id.clone(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        kind: OrderKind::Limit {
            px: 100.0,
            tif: TimeInForce::Gtc,
        },
        stop: Some(StopSpec { trigger_px: 90.0 }),
        reduce_only: false,
        close_position: false,
        sleeve_effect: None,
        exact_terms: None,
    };
    let rejection = OrderUpdate::Reject {
        client_order_id: id.clone(),
        code: 1,
        reason: "late fill follows the archived rejection".into(),
    };
    for record in [
        WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
            strategies: vec!["probe".into()],
            symbols: vec!["BTCUSDT".into()],
        }),
        WalRecord::StrategyEventPublished {
            wall_ts_ms: now - 2,
            event: engine_types::StrategyEvent {
                source: StrategyId(0),
                destination: StrategyId(0),
                kind: "unrelated-archive-event".into(),
                event_id: "archived-event".into(),
                payload: vec![1, 2, 3],
            },
        },
        WalRecord::OrderSent {
            dispatch: None,
            request,
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        WalRecord::OrderUpdate {
            callbacks: Some(vec![StrategyId(0)]),
            update: rejection.clone(),
        },
    ] {
        crate::testpath::append_history(&mut wal, &path, &record).unwrap();
    }
    wal.barrier().unwrap();
    let archive = std::fs::read(&path).unwrap();
    assert!(wal.rotate(&base).unwrap());
    drop(wal);
    let (_, newest) = crate::assembly::wal(&path).unwrap();
    assert_eq!(newest.len(), 1);
    assert!(!crate::inflight::LedgerOfOrders::try_from_records(&newest)
        .unwrap()
        .contains(&id));

    let execution = late_fill(&id, now);
    let mut previous_epoch = archived_epoch;
    for pass in 0..3 {
        let mut engine = boot(&path, &execution).await;
        let snapshot = engine.rotation_base(clock::wall_ms());
        let WalRecord::SegmentBase {
            order_id_epoch_ms: Some(epoch),
            may_open,
            strategy_callback_sources,
            portfolio: Some(portfolio),
            open_trade_lots: Some(lots),
            ..
        } = &snapshot
        else {
            panic!("boot must retain canonical accounting");
        };
        assert!(*epoch > previous_epoch);
        previous_epoch = *epoch;
        assert!(*may_open);
        assert_eq!(strategy_callback_sources.len(), 1);
        assert!(strategy_callback_sources[0]
            .accepted
            .is_some_and(|accepted| {
                accepted
                    >= CallbackOrderOrigin {
                        segment: 1,
                        sequence: 4,
                    }
                    && accepted <= strategy_callback_sources[0].latest
            }));
        assert!(strategy_callback_sources[0].latest.segment >= 2);
        assert_eq!(portfolio.positions.len(), 1);
        assert_eq!(portfolio.positions[0].signed_qty, decimal("0.5"));
        assert_eq!(portfolio.positions[0].entry_value, Some(decimal("50")));
        assert_eq!(lots.len(), 1);
        assert_eq!(lots[0].fills, 1);
        assert_eq!(lots[0].fees, Some(decimal("0.001")));
        let callbacks = recovered_callbacks(&mut engine.wal);
        assert_eq!(callbacks.len(), 2, "pass {pass}: {callbacks:?}");
        assert_eq!(
            callbacks[0],
            CallbackEvent::Order {
                update: rejection.clone()
            }
        );
        assert!(matches!(&callbacks[1], CallbackEvent::Order {
            update: OrderUpdate::Fill { exec_id, amounts: Some(amounts), .. }
        } if exec_id == &execution.exec_id && amounts.quantity.value == decimal("0.5")));
        engine.wal.barrier().unwrap();
        let (records, torn) = engine_wal::replay_chain(&path).unwrap();
        assert!(!torn);
        assert!(
            !records.iter().any(|(_, record)| matches!(
                record,
                WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { .. }
                ) | WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared { .. }
                ) | WalRecord::Retained(
                    engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued { .. }
                )
            )),
            "embedded recovery must retire source ownership through the state restatement"
        );
        if pass == 1 {
            assert!(engine.wal.rotate(&snapshot).unwrap());
        }
        drop(engine);
        assert_eq!(std::fs::read(&path).unwrap(), archive);
    }
}
