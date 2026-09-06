use super::*;
use crate::strategy_process::{
    host::{CallbackExecution, CallbackHost},
    CallbackProposal, StrategyProcess,
};
use engine_types::strategy_process::{CallbackReply, StrategyCallbackInput};
use engine_types::Quote;

type TestEngine = Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;

#[path = "../../../tests/fixtures/native-held.rs"]
mod native_fixture;
use native_fixture::held_plug;

fn quote(symbol: u16, recv_ns: u64) -> EngineEvent {
    EngineEvent::Market(MarketEvent::Quote {
        symbol: SymbolId(symbol),
        quote: Quote {
            bid_px: 29_999.0,
            ask_px: 30_001.0,
            bid_qty: 10.0,
            ask_qty: 10.0,
            recv_ns,
            ..Default::default()
        },
    })
}

fn prepare_market(engine: &mut TestEngine, event: &EngineEvent) -> StrategyCallbackInput {
    if let EngineEvent::Market(event) = event {
        engine.books.market.apply(event);
    }
    engine.host.callbacks.enqueue(StrategyId(0), event).unwrap();
    let mut input = engine.host.callbacks.unwritten.pop_front().unwrap();
    engine
        .host
        .callbacks
        .accept_volatile(input.clone())
        .unwrap();
    input.preparation = CallbackPreparation::Prepared {
        snapshot: engine
            .host
            .snapshot(&engine.books, StrategyId(0), clock::now_ns())
            .unwrap(),
    };
    engine.host.callbacks.state.prepared(input.clone()).unwrap();
    input
}

fn worker_proposal(engine: &TestEngine, input: &StrategyCallbackInput) -> CallbackProposal {
    use crate::strategy_process::wire::{read_record, write_record, Budget};
    use engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES;
    let runtime = engine
        .host
        .callbacks
        .state
        .committed
        .get(&StrategyId(0))
        .map(|state| state.runtime.clone())
        .unwrap_or_else(|| engine.host.strategies[0].runtime_state().unwrap().unwrap());
    let request = input.request(runtime).unwrap();
    let mut source = Vec::new();
    write_record(
        &mut source,
        &request,
        &mut Budget::new(MAX_PROCESS_PROPOSAL_BYTES),
    )
    .unwrap();
    let mut reply = Vec::new();
    crate::strategy_process::worker::serve(&mut source.as_slice(), &mut reply).unwrap();
    let mut reply = reply.as_slice();
    let mut budget = Budget::new(MAX_PROCESS_PROPOSAL_BYTES);
    let mut actions = Vec::new();
    let mut timers = Vec::new();
    loop {
        match read_record(&mut reply, &mut budget).unwrap() {
            CallbackReply::Action { action } => actions.push(action),
            CallbackReply::Timer { timer } => timers.push(timer),
            CallbackReply::Finished {
                callback_id,
                state,
                retained_signal_subscriptions,
            } => {
                return CallbackProposal {
                    callback_id,
                    state,
                    actions,
                    timers,
                    retained_signal_subscriptions,
                }
            }
            CallbackReply::Aborted { reason, .. } => panic!("{reason}"),
        }
    }
}

fn completion(
    engine: &mut TestEngine,
    input_id: u64,
    proposal: CallbackProposal,
) -> CallbackCompletion {
    engine
        .host
        .callbacks
        .expect_test_completion(StrategyId(0), input_id);
    let mut command = std::process::Command::new("/bin/sleep");
    command.arg("60");
    CallbackCompletion {
        strategy: StrategyId(0),
        input_id,
        result: Ok((StrategyProcess::spawn_command(command).unwrap(), proposal)),
    }
}

async fn accept(engine: &mut TestEngine, input: StrategyCallbackInput) -> Vec<Action> {
    let proposal = worker_proposal(engine, &input);
    let actions = proposal.actions.clone();
    let completion = completion(engine, input.callback_id, proposal);
    engine.on_strategy_callback(Some(completion)).unwrap();
    if engine.host.callbacks.write.is_some() {
        let durable = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(durable).unwrap();
        engine.drain(clock::now_ns()).await.unwrap();
    }
    actions
}

#[tokio::test(start_paused = true)]
async fn native_held_sleeves_do_not_log_the_270_symbol_runtime_on_ordinary_quotes() {
    for kind in ["long_native", "carry_native"] {
        let (mut engine, records) = crate::tests::recovery_inventory_fixture().await;
        let now = clock::wall_ms();
        engine.host.strategies = vec![held_plug(kind, now, engine.account().equity_usdt)];
        engine.host.names = vec![kind.into()];
        engine.host.timers = Timers::default();
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            &[],
        )
        .unwrap();
        for symbol in 1..270 {
            engine
                .books
                .market
                .add_symbol(&format!("TOKEN{symbol}USDT"));
        }
        for depth in &mut engine.books.market.depths {
            depth.bid_len = 10;
            depth.ask_len = 10;
            for level in 0..10 {
                depth.bids[level] = engine_types::BookLevel {
                    px: 29_999.0 - level as f64,
                    qty: 10.0,
                };
                depth.asks[level] = engine_types::BookLevel {
                    px: 30_001.0 + level as f64,
                    qty: 10.0,
                };
            }
        }
        for _ in 0..3 {
            let input = prepare_market(&mut engine, &quote(0, clock::now_ns()));
            assert!(serde_json::to_vec(input.snapshot().unwrap()).unwrap().len() > 180_000);
            let actions = accept(&mut engine, input).await;
            assert!(
                !actions
                    .iter()
                    .any(|action| matches!(action, Action::Place(_))),
                "held quantity changed: {kind} {actions:?}"
            );
        }
        let before = serde_json::to_vec(&*records.lock().unwrap()).unwrap().len();
        let runtime = engine.host.callbacks.state.committed[&StrategyId(0)].clone();
        for _ in 0..20 {
            tokio::time::advance(Duration::from_millis(1)).await;
            let input = prepare_market(&mut engine, &quote(0, clock::now_ns()));
            accept(&mut engine, input).await;
        }
        let bytes = serde_json::to_vec(&*records.lock().unwrap()).unwrap().len() - before;
        assert_eq!(
            bytes, 0,
            "{kind} persisted {bytes} bytes for 20 unchanged held quotes"
        );
        assert_eq!(
            engine.host.callbacks.state.committed[&StrategyId(0)],
            runtime
        );
        assert!(engine.host.callbacks.faults.is_empty());
    }
}

#[tokio::test(start_paused = true)]
async fn market_overload_keeps_one_invocation_and_one_latest_slot_per_symbol_without_faults() {
    let (mut engine, _) = crate::tests::recovery_inventory_fixture().await;
    engine.host.strategies = vec![held_plug(
        "long_native",
        clock::wall_ms(),
        engine.account().equity_usdt,
    )];
    engine.host.callbacks = CallbackHost::new(
        CallbackExecution::Isolated {
            executable: "/bin/false".into(),
        },
        &engine.host.strategies,
        &[],
    )
    .unwrap();
    for symbol in 1..270 {
        engine
            .books
            .market
            .add_symbol(&format!("TOKEN{symbol}USDT"));
    }
    for index in 0..27_000 {
        let event = quote((index % 270) as u16, index + 1);
        if let EngineEvent::Market(event) = &event {
            engine.books.market.apply(event);
        }
        assert!(engine.feed_one_strategy(StrategyId(0), &event, index + 1));
    }
    assert_eq!(engine.host.callbacks.unwritten.len(), 1);
    assert_eq!(engine.host.callbacks.retry_inputs.market.len(), 270);
    assert!(engine.host.callbacks.faults.is_empty());
    for (_, slot) in &engine.host.callbacks.retry_inputs.market {
        let MarketEvent::Quote { symbol, quote } = slot.latest(&engine.books.market, 0) else {
            panic!("quote slot")
        };
        assert_eq!(quote.recv_ns, 26_731 + u64::from(symbol.0));
    }
}

#[tokio::test(start_paused = true)]
async fn a_market_reduction_promotes_after_later_inputs_and_replays_every_durability_cut() {
    for fail_barrier in [false, true] {
        let (mut engine, records) = crate::tests::recovery_inventory_fixture().await;
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        engine.host.strategies = (0..2)
            .map(|id| engine_strategies::build_strategy("probe", StrategyId(id), &params).unwrap())
            .collect();
        engine.host.names = vec!["first".into(), "second".into()];
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            &[],
        )
        .unwrap();
        engine.ensure_callback_reader(&[]).unwrap();
        let input = prepare_market(&mut engine, &quote(0, clock::now_ns()));
        let old_id = input.callback_id;
        let mut proposal = worker_proposal(&engine, &input);
        proposal.actions = vec![Action::Place(engine_types::Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.001,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "market-exit".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        })];
        engine
            .host
            .callbacks
            .enqueue(StrategyId(1), &EngineEvent::Boot)
            .unwrap();
        let later_id = engine.host.callbacks.unwritten.front().unwrap().callback_id;
        let completion = completion(&mut engine, old_id, proposal);
        engine.on_strategy_callback(Some(completion)).unwrap();
        assert_eq!(engine.host.callbacks.deferred_completions.len(), 1);
        assert!(engine.host.effects.transitions.is_empty());
        engine.service_strategy_callbacks().unwrap();
        let durable = engine.host.callbacks.durable.recv().await;
        engine.on_callback_durable(durable).unwrap();
        if fail_barrier {
            engine.wal.fail_barrier_after = Some("strategy_process_transition_queued");
        }
        let result = engine.service_strategy_callbacks();
        assert_eq!(result.is_err(), fail_barrier);
        assert!(
            engine.host.effects.transitions.is_empty(),
            "an exit escaped before its barrier"
        );
        let rows: Vec<_> = records
            .lock()
            .unwrap()
            .iter()
            .filter(|record| {
                matches!(
                    record,
                    WalRecord::StrategyCallbackQueued { .. }
                        | WalRecord::StrategyCallbackPrepared { .. }
                        | WalRecord::StrategyProcessTransitionQueued { .. }
                )
            })
            .cloned()
            .collect();
        let promoted = rows
            .iter()
            .find_map(|record| match record {
                WalRecord::StrategyProcessTransitionQueued { input_id, .. } => Some(*input_id),
                _ => None,
            })
            .unwrap();
        assert!(promoted > later_id && later_id > old_id);
        for cut in 0..=rows.len() {
            let state =
                crate::strategy_process::state::CallbackState::replay(&rows[..cut], 2).unwrap();
            let effects = crate::effects::Effects::replay(&rows[..cut], 2).unwrap();
            if cut < rows.len() {
                assert!(!state.committed.contains_key(&StrategyId(0)));
                assert!(effects.transitions.is_empty());
            } else {
                assert_eq!(state.committed[&StrategyId(0)].last_callback_id, promoted);
                assert_eq!(effects.transitions.len(), 1);
                assert!(effects.transitions.values().any(|transition| matches!(&transition.effects[0], Action::Place(intent) if intent.reduce_only && intent.qty == 0.001)));
            }
        }
        if !fail_barrier {
            let durable = engine.host.callbacks.durable.recv().await;
            engine.on_callback_durable(durable).unwrap();
            assert_eq!(engine.host.effects.transitions.len(), 1);
        }
    }
}

#[tokio::test(start_paused = true)]
async fn failed_market_promotion_appends_never_publish_runtime_or_exit_effects() {
    for (failed_kind, accepted_rows) in [
        ("strategy_callback_queued", 0),
        ("strategy_callback_prepared", 1),
        ("strategy_process_transition_queued", 2),
    ] {
        let (mut engine, records) = crate::tests::recovery_inventory_fixture().await;
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        engine.host.strategies =
            vec![engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap()];
        engine.host.callbacks = CallbackHost::new(
            CallbackExecution::Isolated {
                executable: "/bin/false".into(),
            },
            &engine.host.strategies,
            &[],
        )
        .unwrap();
        let input = prepare_market(&mut engine, &quote(0, clock::now_ns()));
        let mut proposal = worker_proposal(&engine, &input);
        proposal.actions = vec![Action::Place(engine_types::Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.001,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "promotion-exit".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        })];
        engine.wal.fail_append(failed_kind);
        let completion = completion(&mut engine, input.callback_id, proposal);
        assert!(engine.on_strategy_callback(Some(completion)).is_err());
        assert!(engine.host.callbacks.state.committed.is_empty());
        assert!(engine.host.effects.transitions.is_empty());
        let rows: Vec<_> = records
            .lock()
            .unwrap()
            .iter()
            .filter(|row| {
                matches!(
                    row,
                    WalRecord::StrategyCallbackQueued { .. }
                        | WalRecord::StrategyCallbackPrepared { .. }
                        | WalRecord::StrategyProcessTransitionQueued { .. }
                )
            })
            .cloned()
            .collect();
        assert_eq!(rows.len(), accepted_rows, "{failed_kind}");
        let replayed = crate::strategy_process::state::CallbackState::replay(&rows, 1).unwrap();
        assert!(replayed.committed.is_empty());
        assert_eq!(replayed.inputs.len(), usize::from(accepted_rows > 0));
        assert!(crate::effects::Effects::replay(&rows, 1)
            .unwrap()
            .transitions
            .is_empty());
    }
}
