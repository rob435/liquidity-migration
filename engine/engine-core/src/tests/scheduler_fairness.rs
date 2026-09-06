use super::*;

struct TimerProbe {
    armed: u32,
    repeat_limit: usize,
    replace_next: bool,
    fired: Rc<RefCell<Vec<TimerId>>>,
}

impl Strategy for TimerProbe {
    fn name(&self) -> &str {
        "timer-probe"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }

    fn on_boot(&mut self, ctx: &mut dyn StrategyCtx) {
        for id in 0..self.armed {
            ctx.arm_timer(TimerId(id), 0);
        }
    }

    fn on_timer(&mut self, id: TimerId, _: u64, ctx: &mut dyn StrategyCtx) {
        let mut fired = self.fired.lock().unwrap();
        fired.push(id);
        if self.replace_next && id == TimerId(0) {
            ctx.arm_timer(TimerId(1), 1_000_000_000);
        }
        if fired.len() < self.repeat_limit {
            ctx.arm_timer(id, 0);
        }
    }
}

struct PrivateCloseAfterTimer(Rc<RefCell<Vec<TimerId>>>);

impl OrderFeed for PrivateCloseAfterTimer {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        if self.0.lock().unwrap().is_empty() {
            std::future::pending().await
        } else {
            Err(FeedError::Closed)
        }
    }
}

async fn observe_before_private_close(
    armed: u32,
    repeat_limit: usize,
    replace_next: bool,
) -> Vec<TimerId> {
    let fired = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, _) = build(
        allow_all(),
        vec![Box::new(TimerProbe {
            armed,
            repeat_limit,
            replace_next,
            fired: fired.clone(),
        })],
        &[],
        &[],
    )
    .await;
    let outcome = engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut PrivateCloseAfterTimer(fired.clone()),
            std::future::pending(),
        )
        .await
        .unwrap();
    assert_eq!(outcome.stopped_by, StopReason::FeedClosed);
    let result = fired.lock().unwrap().clone();
    result
}

#[tokio::test(start_paused = true)]
async fn zero_delay_rearming_returns_to_private_input_before_firing_again() {
    assert_eq!(
        observe_before_private_close(1, 1000, false).await,
        [TimerId(0)]
    );
}

#[tokio::test(start_paused = true)]
async fn a_large_due_timer_set_yields_before_exhausting_the_set() {
    let fired = observe_before_private_close(200, 0, false).await;
    assert_eq!(
        fired,
        (0..crate::engine::MAX_TIMER_CALLBACKS_PER_TURN as u32)
            .map(TimerId)
            .collect::<Vec<_>>()
    );
}

#[tokio::test(start_paused = true)]
async fn an_earlier_callback_can_replace_another_snapshotted_timer() {
    assert_eq!(observe_before_private_close(2, 0, true).await, [TimerId(0)]);
}

struct ImmediateTimer;

struct NoTicks;

impl crate::engine::LoopTimer for ImmediateTimer {
    type Sleep = std::future::Ready<()>;
    type Interval = NoTicks;

    fn sleep(&self, _: Duration) -> Self::Sleep {
        std::future::ready(())
    }

    fn interval(&self, _: Duration) -> Self::Interval {
        NoTicks
    }
}

impl crate::engine::LoopInterval for NoTicks {
    async fn tick(&mut self) {
        std::future::pending().await
    }
}

struct PrivateCloseFromTask(tokio::sync::oneshot::Receiver<()>);

impl OrderFeed for PrivateCloseFromTask {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        (&mut self.0).await.unwrap();
        Err(FeedError::Closed)
    }
}

#[tokio::test(start_paused = true)]
async fn immediate_timers_let_the_private_feed_task_run() {
    let fired = Rc::new(RefCell::new(Vec::new()));
    let repeat_limit = 1000;
    let (mut engine, _) = build(
        allow_all(),
        vec![Box::new(TimerProbe {
            armed: 1,
            repeat_limit,
            replace_next: false,
            fired: fired.clone(),
        })],
        &[],
        &[],
    )
    .await;
    let (tx, rx) = tokio::sync::oneshot::channel();
    let observed = fired.clone();
    let task = tokio::spawn(async move {
        while observed.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
        tx.send(()).unwrap();
    });
    let outcome = engine
        .run_with_inputs_on(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut PrivateCloseFromTask(rx),
            &mut crate::signals::NoSignals,
            &mut crate::controls::NoControls,
            std::future::pending(),
            ImmediateTimer,
        )
        .await
        .unwrap();
    task.await.unwrap();
    assert_eq!(outcome.stopped_by, StopReason::FeedClosed);
    let callbacks = fired.lock().unwrap().len();
    assert!(callbacks > 0);
    assert!(
        callbacks < repeat_limit,
        "private task first ran after all {callbacks} callbacks"
    );
}

struct DurableFlood;
impl Strategy for DurableFlood {
    fn name(&self) -> &str {
        "durable-flood"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
    fn on_boot(&mut self, ctx: &mut dyn StrategyCtx) {
        if ctx.strategy_global_checkpoint().is_some() {
            return;
        }
        for index in 0..1024_u64 {
            ctx.emit(engine_types::Action::SetStrategyGlobalCheckpoint {
                strategy: StrategyId(0),
                checkpoint: StrategyCheckpoint {
                    schema_version: 1,
                    decision_fingerprint: "durable-flood".into(),
                    payload: index.to_le_bytes().to_vec(),
                },
            });
        }
    }
}

#[tokio::test(start_paused = true)]
async fn durable_effect_flood_yields_to_private_input_and_restarts_its_suffix() {
    let (mut engine, h) = build(allow_all(), vec![Box::new(DurableFlood)], &[], &[]).await;
    let records = h.records.clone();
    let (tx, rx) = tokio::sync::oneshot::channel();
    let task = tokio::spawn(async move {
        loop {
            if records
                .lock()
                .unwrap()
                .iter()
                .any(|record| matches!(record, WalRecord::StrategyGlobalCheckpoint { .. }))
            {
                break;
            }
            tokio::task::yield_now().await;
        }
        tx.send(()).unwrap();
    });
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut PrivateCloseFromTask(rx),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    task.await.unwrap();
    let records = h.records.lock().unwrap().clone();
    let applied = records
        .iter()
        .filter(|record| matches!(record, WalRecord::StrategyGlobalCheckpoint { .. }))
        .count();
    assert!(
        applied <= 256,
        "durable effects starved the private input: {applied}"
    );
    for replayed in [records, vec![engine.rotation_base(recent_replay_ms())]] {
        let (mut restarted, recovered) =
            build(allow_all(), vec![Box::new(DurableFlood)], &[], &replayed).await;
        restarted.finish().await.unwrap();
        let remaining = recovered
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|record| matches!(record, WalRecord::StrategyGlobalCheckpoint { .. }))
            .count();
        assert_eq!(
            applied + remaining,
            1024,
            "restart must execute each retained checkpoint exactly once"
        );
    }
}

struct MarketTimerProbe(Option<TimerProbe>);
impl Strategy for MarketTimerProbe {
    fn name(&self) -> &str {
        "market-timer-probe"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn on_boot(&mut self, ctx: &mut dyn StrategyCtx) {
        if let Some(timer) = &mut self.0 {
            timer.on_boot(ctx);
        }
    }
    fn on_timer(&mut self, id: TimerId, now: u64, ctx: &mut dyn StrategyCtx) {
        if let Some(timer) = &mut self.0 {
            timer.on_timer(id, now, ctx);
        }
    }
}

struct ReadyMaintenance;
struct EveryTurn;
impl crate::engine::LoopTimer for ReadyMaintenance {
    type Sleep = std::future::Pending<()>;
    type Interval = EveryTurn;
    fn sleep(&self, _: Duration) -> Self::Sleep {
        std::future::pending()
    }
    fn interval(&self, _: Duration) -> Self::Interval {
        EveryTurn
    }
}
impl crate::engine::LoopInterval for EveryTurn {
    async fn tick(&mut self) {}
}

struct MarketSnapshot {
    feed: ScriptFeed,
    tape: Tape,
    before_close: Vec<Step>,
    heartbeat: Option<std::path::PathBuf>,
    heartbeat_before_close: bool,
}
impl MarketFeed for MarketSnapshot {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self.feed.events.is_empty() {
            self.before_close = self.tape.lock().unwrap().clone();
            self.heartbeat_before_close = self.heartbeat.as_ref().is_some_and(|p| p.exists());
        }
        self.feed.next_event().await
    }
}

#[tokio::test(start_paused = true)]
async fn due_timer_runs_before_a_continuously_ready_market_closes() {
    let fired = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, _) = build(
        allow_all(),
        vec![Box::new(MarketTimerProbe(Some(TimerProbe {
            armed: 1,
            repeat_limit: 0,
            replace_next: false,
            fired: fired.clone(),
        })))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let outcome = engine
        .run_with_inputs_on(
            &mut ScriptFeed::quotes(SymbolId(0), 64, true),
            &mut ScriptOrderFeed::empty(),
            &mut crate::signals::NoSignals,
            &mut crate::controls::NoControls,
            std::future::pending(),
            crate::engine::SystemTimer,
        )
        .await
        .unwrap();
    assert_eq!(outcome.market_events, 64);
    assert_eq!(
        *fired.lock().unwrap(),
        [TimerId(0)],
        "ready market starved the due timer"
    );
}

#[tokio::test(start_paused = true)]
async fn ready_market_cannot_hide_flush_heartbeat_or_account_refresh() {
    let _clock = engine_types::clock::install_virtual(clock::wall_ns(), 0).unwrap();
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(MarketTimerProbe(Some(TimerProbe {
            armed: 1,
            repeat_limit: 1000,
            replace_next: false,
            fired: Rc::new(RefCell::new(Vec::new())),
        })))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let path = temp_path("ready-market-heartbeat");
    engine.write_heartbeat(Heartbeat::with_every(
        path.path().to_path_buf(),
        None,
        None,
        Duration::ZERO,
    ));
    engine_types::clock::advance_virtual_to(120_000_000_000).unwrap();
    h.tape.lock().unwrap().clear();
    let mut market = MarketSnapshot {
        feed: ScriptFeed::quotes(SymbolId(0), 64, true),
        tape: h.tape.clone(),
        before_close: vec![],
        heartbeat: Some(path.path().to_path_buf()),
        heartbeat_before_close: false,
    };
    engine
        .run_with_inputs_on(
            &mut market,
            &mut ScriptOrderFeed::empty(),
            &mut crate::signals::NoSignals,
            &mut crate::controls::NoControls,
            std::future::pending(),
            ReadyMaintenance,
        )
        .await
        .unwrap();
    assert!(
        market.before_close.contains(&Step::Flush),
        "ready market postponed WAL flush until shutdown"
    );
    assert!(
        market.heartbeat_before_close,
        "ready market postponed heartbeat until shutdown"
    );
    assert!(
        market.before_close.contains(&Step::ReadAccount),
        "ready market postponed a due account refresh"
    );
}

#[tokio::test(start_paused = true)]
async fn zero_delay_rearming_does_not_exhaust_its_flood_before_ready_market_input() {
    let fired = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, _) = build(
        allow_all(),
        vec![Box::new(MarketTimerProbe(Some(TimerProbe {
            armed: 1,
            repeat_limit: 1000,
            replace_next: false,
            fired: fired.clone(),
        })))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let outcome = engine
        .run_with_inputs_on(
            &mut ScriptFeed::quotes(SymbolId(0), 64, true),
            &mut ScriptOrderFeed::empty(),
            &mut crate::signals::NoSignals,
            &mut crate::controls::NoControls,
            std::future::pending(),
            ImmediateTimer,
        )
        .await
        .unwrap();
    assert_eq!(outcome.market_events, 64);
    let callbacks = fired.lock().unwrap().len();
    assert!(
        (1..=65).contains(&callbacks),
        "unbalanced timer/market service: {callbacks} timers for 64 quotes"
    );
}

struct OrdinaryProbe {
    market_permissions: Rc<RefCell<Vec<bool>>>,
    signals: Rc<RefCell<Vec<u64>>>,
}
impl Strategy for OrdinaryProbe {
    fn name(&self) -> &str {
        "ordinary-probe"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn on_market(&mut self, _: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        self.market_permissions
            .lock()
            .unwrap()
            .push(ctx.entries_enabled(true));
    }
    fn on_signal(&mut self, row: &engine_types::SignalObservation, ctx: &mut dyn StrategyCtx) {
        self.signals.lock().unwrap().push(row.sequence);
        ctx.emit(engine_types::Action::ConsumeSignalObservation {
            strategy: StrategyId(0),
            source: row.source.clone(),
            sequence: row.sequence,
            observation_id: row.observation_id.clone(),
        });
    }
}
struct ReadyControls(VecDeque<engine_types::RuntimeControlRequest>);
impl engine_types::RuntimeControlFeed for ReadyControls {
    async fn next_request(
        &mut self,
    ) -> Result<engine_types::RuntimeControlRequest, engine_types::RuntimeControlError> {
        match self.0.pop_front() {
            Some(row) => Ok(row),
            None => std::future::pending().await,
        }
    }
}
struct ReadySignals(VecDeque<engine_types::SignalObservation>);
impl engine_types::SignalFeed for ReadySignals {
    fn defer_last(
        &mut self,
        row: engine_types::SignalObservation,
    ) -> Result<(), engine_types::SignalError> {
        self.0.push_front(row);
        Ok(())
    }
    fn set_gap_requests(
        &mut self,
        _: &[engine_types::SignalGapRequest],
        _: &[StrategyId],
    ) -> Result<(), engine_types::SignalError> {
        Ok(())
    }
    fn acknowledge_last(&mut self) -> Result<(), engine_types::SignalError> {
        Ok(())
    }
    async fn next_observation(
        &mut self,
    ) -> Result<engine_types::SignalObservation, engine_types::SignalError> {
        match self.0.pop_front() {
            Some(row) => Ok(row),
            None => std::future::pending().await,
        }
    }
}

#[tokio::test(start_paused = true)]
async fn ready_control_signal_and_market_floods_each_make_bounded_progress() {
    let seen = Rc::new(RefCell::new(Vec::new()));
    let signals = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(OrdinaryProbe {
            market_permissions: seen.clone(),
            signals: signals.clone(),
        })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let mut controls = ReadyControls(
        (1..=1000)
            .map(|id| {
                let mut row = engine_types::RuntimeControlRequest {
                    schema_version: engine_types::STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
                    strategy: StrategyId(0),
                    strategy_name: "ordinary-probe".into(),
                    request_id: format!("pause-{id}"),
                    command: engine_types::RuntimeControlCommand::SetEntriesEnabled {
                        entries_enabled: false,
                    },
                    content_sha256: String::new(),
                };
                row.content_sha256 = crate::controls::content_sha256(&row);
                row
            })
            .collect(),
    );
    let mut feed = ReadySignals(
        (1..=1000)
            .map(|sequence| {
                let mut row = engine_types::SignalObservation {
                    schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
                    decision_fingerprint: "ordinary-probe".into(),
                    destination: StrategyId(0),
                    source: "source".into(),
                    sequence,
                    observation_id: format!("source-{sequence}"),
                    kind: "test".into(),
                    observed_wall_ts_ms: 1,
                    available_wall_ts_ms: 1,
                    subscriptions: vec![],
                    payload: b"{}".to_vec(),
                    content_sha256: String::new(),
                };
                row.content_sha256 = crate::signals::content_sha256(&row);
                row
            })
            .collect(),
    );
    h.tape.lock().unwrap().clear();
    let mut market = MarketSnapshot {
        feed: ScriptFeed::quotes(SymbolId(0), 64, true),
        tape: h.tape.clone(),
        before_close: vec![],
        heartbeat: None,
        heartbeat_before_close: false,
    };
    let outcome = engine
        .run_with_inputs_on(
            &mut market,
            &mut ScriptOrderFeed::empty(),
            &mut feed,
            &mut controls,
            std::future::pending(),
            ReadyMaintenance,
        )
        .await
        .unwrap();
    assert_eq!(outcome.market_events, 64);
    let permissions = seen.lock().unwrap();
    assert_eq!(permissions.len(), 64);
    assert!(
        permissions.iter().skip(1).all(|enabled| !enabled),
        "ready market starved entry-disable control"
    );
    let delivered = signals.lock().unwrap();
    assert!(
        (1..=65).contains(&delivered.len()),
        "unbalanced signal/market service: {} signals",
        delivered.len()
    );
    assert_eq!(*delivered, (1..=delivered.len() as u64).collect::<Vec<_>>());
    let consumed: Vec<_> = h
        .records
        .lock()
        .unwrap()
        .iter()
        .filter_map(|row| match row {
            WalRecord::SignalObservationConsumed {
                strategy,
                source,
                sequence,
                observation_id,
                ..
            } => {
                assert_eq!(*strategy, StrategyId(0));
                assert_eq!(source, "source");
                assert_eq!(observation_id, &format!("source-{sequence}"));
                Some(*sequence)
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        consumed, *delivered,
        "every delivered signal must commit its consumption exactly once"
    );
    let accepted = h
        .records
        .lock()
        .unwrap()
        .iter()
        .filter(|row| matches!(row, WalRecord::RuntimeControlAccepted { .. }))
        .count();
    assert!(
        (1..=65).contains(&accepted),
        "unbalanced control/market service: {accepted} controls"
    );
    assert!(
        market.before_close.contains(&Step::Flush),
        "input floods starved maintenance"
    );
    assert!(
        controls.0.len() >= 935 && feed.0.len() >= 935,
        "ordinary floods must not drain before ready market"
    );
}

struct PrivateCloseAfterFlush(Tape);
impl OrderFeed for PrivateCloseAfterFlush {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        if self.0.lock().unwrap().contains(&Step::Flush) {
            Err(FeedError::Closed)
        } else {
            std::future::pending().await
        }
    }
}

#[tokio::test(start_paused = true)]
async fn private_input_ready_during_maintenance_precedes_the_due_timer() {
    let fired = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(TimerProbe {
            armed: 1,
            repeat_limit: 0,
            replace_next: false,
            fired: fired.clone(),
        })],
        &[],
        &[],
    )
    .await;
    h.tape.lock().unwrap().clear();
    let outcome = engine
        .run_with_inputs_on(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut PrivateCloseAfterFlush(h.tape),
            &mut crate::signals::NoSignals,
            &mut crate::controls::NoControls,
            std::future::pending(),
            ReadyMaintenance,
        )
        .await
        .unwrap();
    assert_eq!(outcome.stopped_by, StopReason::FeedClosed);
    assert!(
        fired.lock().unwrap().is_empty(),
        "maintenance must return to private input before a due timer"
    );
}
