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

#[tokio::test]
async fn zero_delay_rearming_returns_to_private_input_before_firing_again() {
    assert_eq!(
        observe_before_private_close(1, 1000, false).await,
        [TimerId(0)]
    );
}

#[tokio::test]
async fn a_large_due_timer_set_yields_before_exhausting_the_set() {
    let fired = observe_before_private_close(200, 0, false).await;
    assert_eq!(
        fired,
        (0..crate::engine::MAX_TIMER_CALLBACKS_PER_TURN as u32)
            .map(TimerId)
            .collect::<Vec<_>>()
    );
}

#[tokio::test]
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

#[tokio::test]
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
