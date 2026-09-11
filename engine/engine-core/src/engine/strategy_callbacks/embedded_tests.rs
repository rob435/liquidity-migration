use super::*;
use engine_types::{StrategyCheckpoint, StrategyCtx, TimerId};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

struct CallbackProbe {
    panic: bool,
    calls: Arc<AtomicUsize>,
    checkpoint: bool,
}
impl Strategy for CallbackProbe {
    fn name(&self) -> &str {
        "embedded-regression"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if !matches!(event, EngineEvent::Market(_)) {
            return;
        }
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.checkpoint {
            ctx.emit(Action::SetStrategyGlobalCheckpoint {
                strategy: StrategyId(0),
                checkpoint: checkpoint(),
            });
        }
        if self.panic {
            ctx.arm_timer(TimerId(42), 1);
            panic!("embedded regression panic");
        }
    }
}
fn checkpoint() -> StrategyCheckpoint {
    StrategyCheckpoint {
        schema_version: 1,
        decision_fingerprint: "embedded-regression".into(),
        payload: vec![1, 2, 3],
    }
}
fn quote() -> EngineEvent {
    EngineEvent::Market(MarketEvent::Quote {
        symbol: SymbolId(0),
        quote: engine_types::Quote {
            bid_px: 99.0,
            ask_px: 101.0,
            recv_ns: clock::now_ns(),
            ..Default::default()
        },
    })
}

#[tokio::test(start_paused = true)]
async fn embedded_panic_faults_only_its_sleeve_and_cancels_its_orders() {
    let failed = Arc::new(AtomicUsize::new(0));
    let sibling = Arc::new(AtomicUsize::new(0));
    let (mut engine, records, cancels) = crate::tests::callback_cancellation_fixture(vec![
        Box::new(CallbackProbe {
            panic: true,
            calls: failed.clone(),
            checkpoint: true,
        }),
        Box::new(CallbackProbe {
            panic: false,
            calls: sibling.clone(),
            checkpoint: false,
        }),
    ])
    .await;
    for owner in [0, 1] {
        let id = format!("panic-owned-{owner}");
        let record = WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: id.clone(),
                strategy: StrategyId(owner),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.1,
                kind: OrderKind::Limit {
                    px: 99.0,
                    tif: engine_types::TimeInForce::Gtc,
                },
                stop: None,
                reduce_only: false,
                close_position: false,
                sleeve_effect: None,
                exact_terms: None,
            },
            arrival_mid: 100.0,
            wire_ns: 1,
        };
        engine.books.orders.try_apply(&record).unwrap();
        engine.books.registry.own(&id, StrategyId(owner));
    }
    records.lock().unwrap().clear();
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    engine
        .host
        .feed(&engine.books, StrategyId(1), &quote(), clock::now_ns());
    assert!(engine.host.callbacks.faults.contains_key(&StrategyId(0)));
    assert!(!engine.host.callbacks.faults.contains_key(&StrategyId(1)));
    assert!(engine.host.pending.iter().any(|row| matches!(&row.action, Action::Cancel { client_order_id, .. } if client_order_id == "panic-owned-0")));
    assert!(!engine.host.pending.iter().any(|row| matches!(&row.action, Action::Cancel { client_order_id, .. } if client_order_id == "panic-owned-1")));
    assert!(!engine
        .host
        .pending
        .iter()
        .any(|row| matches!(&row.action, Action::SetStrategyGlobalCheckpoint { .. })));
    assert!(!engine.host.timers.is_armed(StrategyId(0), TimerId(42)));
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    engine
        .host
        .feed(&engine.books, StrategyId(1), &quote(), clock::now_ns());
    assert_eq!(failed.load(Ordering::SeqCst), 1);
    assert_eq!(sibling.load(Ordering::SeqCst), 2);
    engine.drain(clock::now_ns()).await.unwrap();
    while !engine.pending_mutations.is_empty() {
        let completion = engine.venue_completions.recv().await.unwrap();
        engine.take_venue_completion(completion).await.unwrap();
    }
    assert_eq!(
        *cancels.lock().unwrap(),
        vec![(SymbolId(0), "panic-owned-0".into())]
    );
    engine
        .on_market(&MarketEvent::FeedReset {
            recv_ns: clock::now_ns(),
        })
        .await
        .unwrap();
    assert_eq!(failed.load(Ordering::SeqCst), 1);
    assert_eq!(sibling.load(Ordering::SeqCst), 3);
    assert!(records
        .lock()
        .unwrap()
        .iter()
        .any(|row| matches!(row, WalRecord::CancelSent { .. })));
}

#[tokio::test(start_paused = true)]
async fn embedded_unchanged_checkpoint_writes_zero_wal_bytes() {
    let (mut engine, records) =
        crate::tests::callback_test_fixture(vec![Box::new(CallbackProbe {
            panic: false,
            calls: Arc::new(AtomicUsize::new(0)),
            checkpoint: true,
        })])
        .await;
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    engine.drain(clock::now_ns()).await.unwrap();
    assert_eq!(
        engine.host.global_checkpoints[&StrategyId(0)].checkpoint,
        checkpoint()
    );
    records.lock().unwrap().clear();
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    engine.drain(clock::now_ns()).await.unwrap();
    let bytes: usize = records
        .lock()
        .unwrap()
        .iter()
        .map(|row| serde_json::to_vec(row).unwrap().len())
        .sum();
    assert_eq!(
        bytes, 0,
        "unchanged embedded state wrote callback WAL records"
    );
}

#[tokio::test(start_paused = true)]
async fn embedded_unchanged_checkpoint_behind_a_held_action_writes_zero_wal_bytes() {
    struct HeldCheckpoint {
        emitted: bool,
    }
    impl Strategy for HeldCheckpoint {
        fn name(&self) -> &str {
            "held-checkpoint"
        }
        fn subscriptions(&self) -> Vec<Subscription> {
            Vec::new()
        }
        fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
            if !matches!(event, EngineEvent::Market(_)) {
                return;
            }
            if !self.emitted {
                self.emitted = true;
                ctx.place(Intent {
                    exact_prices: None,
                    exact_quantity: None,
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 0.1,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec { trigger_px: 90.0 }),
                    reduce_only: false,
                    tag: "held-checkpoint".into(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                });
            }
            ctx.emit(Action::SetStrategyGlobalCheckpoint {
                strategy: StrategyId(0),
                checkpoint: checkpoint(),
            });
        }
    }
    let (mut engine, records) =
        crate::tests::callback_test_fixture(vec![Box::new(HeldCheckpoint { emitted: false })])
            .await;
    let (release, barrier) = std::sync::mpsc::channel();
    engine.dispatches.begin(
        crate::order_dispatch::DispatchWrite::Attempt(Vec::new()),
        engine_types::wal::PendingBarrier::running(barrier),
    );
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    let transition = engine.host.effects.transitions.values().next().unwrap().id;
    engine.drain(clock::now_ns()).await.unwrap();
    assert!(engine.host.pending.is_empty());
    assert_eq!(
        engine.dispatches.waiting.len(),
        2,
        "scheduler must park placement and checkpoint behind the unresolved barrier"
    );
    assert!(engine.host.global_checkpoints.is_empty());
    records.lock().unwrap().clear();
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    assert!(
        engine.host.pending.is_empty(),
        "callback duplicated a checkpoint held outside the active queue"
    );
    assert_eq!(engine.host.effects.transitions.len(), 1);
    assert!(engine.host.effects.transitions.contains_key(&transition));
    assert!(records.lock().unwrap().is_empty());
    release.send(Ok(())).unwrap();
    while engine.dispatches.write.is_some() {
        let result = engine.dispatches.durable.recv().await;
        engine.on_order_dispatch_durable(result).await.unwrap();
    }
    engine.drain(clock::now_ns()).await.unwrap();
    assert_eq!(
        engine.host.global_checkpoints[&StrategyId(0)].checkpoint,
        checkpoint()
    );
}

#[tokio::test(start_paused = true)]
async fn embedded_retained_prepared_input_restarts_once_with_runtime_and_timers() {
    use crate::callback_recovery::{
        host::{CallbackExecution, CallbackHost},
        state::CallbackState,
    };
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, StrategyCallbackInput, StrategyProcessState,
        StrategyTimerState,
    };
    let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
    let probe = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
    let (mut engine, records) = crate::tests::callback_test_fixture(vec![probe]).await;
    let mut runtime = engine.host.strategies[0].runtime_state().unwrap().unwrap();
    let mut private: serde_json::Value = serde_json::from_slice(&runtime.payload).unwrap();
    private["fired"] = serde_json::json!(17);
    runtime.payload = serde_json::to_vec(&private).unwrap();
    engine_strategies::runtime::restore(&runtime).unwrap();
    let now = clock::now_ns();
    let wall = clock::wall_ms();
    let mut actions = VecDeque::new();
    let mut timers = crate::ctx::Timers::default();
    let snapshot = crate::ctx::Ctx {
        books: &engine.books,
        now_ns: now,
        strategy: StrategyId(0),
        out: &mut actions,
        timers: &mut timers,
        checkpoints: &engine.host.checkpoints,
        global_checkpoints: &engine.host.global_checkpoints,
        strategy_events: &engine.host.events,
        strategy_names: &engine.host.names,
        runtime_entries_enabled: Some(false),
    }
    .callback_snapshot()
    .unwrap();
    let mut base = engine.rotation_base(wall);
    let WalRecord::SegmentBase {
        strategy_processes,
        strategy_callbacks,
        ..
    } = &mut base
    else {
        unreachable!()
    };
    *strategy_processes = vec![StrategyProcessState {
        strategy: StrategyId(0),
        last_callback_id: 0,
        runtime: runtime.clone(),
        timers: vec![StrategyTimerState {
            id: TimerId(99),
            deadline_ns: now + 30_000_000_000,
            deadline_wall_ms: wall + 30_000,
        }],
        retained_signal_subscriptions: None,
    }];
    *strategy_callbacks = vec![StrategyCallbackInput {
        callback_id: 1,
        order_origin: None,
        strategy: StrategyId(0),
        event: CallbackEvent::Boot,
        preparation: CallbackPreparation::Prepared { snapshot },
    }];
    records.lock().unwrap().clear();
    engine.wal.append(&base).unwrap();
    engine.wal.barrier().unwrap();
    engine.host.callbacks = CallbackHost::new(
        CallbackExecution::Embedded,
        &engine.host.strategies,
        &[base],
    )
    .unwrap();
    engine.restore_strategy_callbacks().await.unwrap();
    let committed = &engine.host.callbacks.state.committed[&StrategyId(0)];
    assert_eq!(committed.last_callback_id, 1);
    let restored_private: serde_json::Value =
        serde_json::from_slice(&committed.runtime.payload).unwrap();
    assert_eq!(restored_private["fired"], 17);
    assert!(committed.timers.iter().any(|timer| timer.id == TimerId(99)));
    assert!(committed
        .timers
        .iter()
        .any(|timer| timer.id == engine_strategies::probe::FIRE));
    assert!(engine.host.callbacks.state.inputs.is_empty());
    let after = records.lock().unwrap().clone();
    assert!(!after.iter().any(|record| matches!(
        record,
        WalRecord::Retained(engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { .. })
            | WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared { .. }
            )
            | WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued { .. }
            )
    )));
    let replayed = CallbackState::replay(&after, 1).unwrap();
    assert!(replayed.inputs.is_empty());
    assert_eq!(replayed.committed, engine.host.callbacks.state.committed);
    let before_restart = replayed.committed;
    records.lock().unwrap().clear();
    engine.host.callbacks =
        CallbackHost::new(CallbackExecution::Embedded, &engine.host.strategies, &after).unwrap();
    engine.restore_strategy_callbacks().await.unwrap();
    assert!(
        records.lock().unwrap().is_empty(),
        "a completed retained callback was executed again"
    );
    assert_eq!(engine.host.callbacks.state.committed, before_restart);
}

#[tokio::test(start_paused = true)]
async fn embedded_global_checkpoint_supersedes_legacy_runtime_at_the_durable_transition() {
    use crate::callback_recovery::{
        host::{CallbackExecution, CallbackHost},
        paging::CallbackPages,
        state::CallbackState,
    };
    use engine_types::strategy_process::StrategyProcessState;
    let (mut engine, records) =
        crate::tests::callback_test_fixture(vec![Box::new(CallbackProbe {
            panic: false,
            calls: Arc::new(AtomicUsize::new(0)),
            checkpoint: true,
        })])
        .await;
    let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
    let runtime = engine_strategies::build_strategy("probe", StrategyId(0), &params)
        .unwrap()
        .runtime_state()
        .unwrap()
        .unwrap();
    let prior = StrategyProcessState {
        strategy: StrategyId(0),
        last_callback_id: 0,
        runtime,
        timers: Vec::new(),
        retained_signal_subscriptions: Some(vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: engine_types::Feed::Quote,
        }]),
    };
    engine
        .host
        .callbacks
        .state
        .committed
        .insert(StrategyId(0), prior.clone());
    let base = engine.rotation_base(clock::wall_ms());
    engine.wal.append(&base).unwrap();
    engine.wal.barrier().unwrap();
    engine
        .host
        .feed(&engine.books, StrategyId(0), &quote(), clock::now_ns());
    assert_eq!(
        engine.host.callbacks.state.committed[&StrategyId(0)],
        prior,
        "unwritten replacement discarded retained runtime"
    );
    let id = engine.host.effects.transitions.values().next().unwrap().id;
    engine.journal_transition(id).unwrap();
    assert_eq!(
        engine.host.callbacks.state.committed[&StrategyId(0)],
        prior,
        "unflushed replacement discarded retained runtime"
    );
    engine.flush_strategy_prefix().unwrap();
    assert!(!engine
        .host
        .callbacks
        .state
        .committed
        .contains_key(&StrategyId(0)));
    let written = records.lock().unwrap().clone();
    let old = CallbackState::replay(std::slice::from_ref(&base), 1).unwrap();
    assert_eq!(
        old.committed[&StrategyId(0)].retained_signal_subscriptions,
        prior.retained_signal_subscriptions
    );
    assert!(CallbackState::replay(&written, 1)
        .unwrap()
        .committed
        .is_empty());
    assert!(
        CallbackPages::replay(&crate::assembly::BootReplay::dense(&written), 1, 1)
            .unwrap()
            .0
            .committed
            .is_empty()
    );
    engine.drain(clock::now_ns()).await.unwrap();
    assert_eq!(
        engine.host.global_checkpoints[&StrategyId(0)].checkpoint,
        checkpoint()
    );
    let rotated = engine.rotation_base(clock::wall_ms());
    engine.host.callbacks = CallbackHost::new(
        CallbackExecution::Embedded,
        &engine.host.strategies,
        &[rotated],
    )
    .unwrap();
    engine.restore_strategy_callbacks().await.unwrap();
    assert_eq!(
        engine.host.global_checkpoints[&StrategyId(0)].checkpoint,
        checkpoint()
    );
}
