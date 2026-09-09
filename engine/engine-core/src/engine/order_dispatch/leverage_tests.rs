use super::*;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

type TestEngine = Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;

struct Fixture {
    engine: TestEngine,
    records: Arc<Mutex<Vec<WalRecord>>>,
    control: crate::tests::LeverageControl,
    prepared: PreparedOrder,
}

async fn prepare() -> Fixture {
    let (mut engine, records) = super::tests::fixture().await;
    let (venue, control) = crate::tests::controlled_leverage_venue();
    (engine.venue, engine.venue_completions) = crate::venue_runtime::VenueClient::spawn(venue);
    engine.books.market.apply(&MarketEvent::Quote {
        symbol: SymbolId(0),
        quote: engine_types::Quote {
            bid_px: 99.0,
            ask_px: 101.0,
            recv_ns: clock::now_ns(),
            ..Default::default()
        },
    });
    let intent = Intent {
        exact_prices: None,
        exact_quantity: None,
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.1,
        kind: OrderKind::Market,
        stop: Some(engine_types::StopSpec { trigger_px: 90.0 }),
        reduce_only: false,
        tag: "leverage-liveness".into(),
        decided_ns: clock::now_ns(),
        work: None,
        leverage: Some(2.0),
    };
    let prepared = tokio::time::timeout(
        Duration::from_millis(1),
        engine.prepare_intent(
            intent,
            Some("leverage-owned".into()),
            clock::now_ns(),
            None,
            &mut HashMap::new(),
        ),
    )
    .await
    .expect("order admission awaited leverage I/O on the account owner")
    .unwrap()
    .expect("the fixture opening must be admitted");
    assert!(control.calls.lock().unwrap().is_empty());
    assert!(control.sends.lock().unwrap().is_empty());
    assert!(records.lock().unwrap().iter().any(|record| {
        matches!(record, WalRecord::OrderSent { request, dispatch: Some(_), .. }
            if request.client_order_id == "leverage-owned")
    }));
    Fixture {
        engine,
        records,
        control,
        prepared,
    }
}

async fn start() -> (
    TestEngine,
    Arc<Mutex<Vec<WalRecord>>>,
    crate::tests::LeverageControl,
) {
    let Fixture {
        mut engine,
        records,
        control,
        prepared,
    } = prepare().await;
    engine.queue_order_dispatches(vec![prepared]).unwrap();
    let result = engine.dispatches.durable.recv().await;
    engine.on_order_dispatch_durable(result).await.unwrap();
    tokio::task::yield_now().await;
    assert_eq!(*control.calls.lock().unwrap(), vec![(SymbolId(0), 2.0)]);
    assert!(control.sends.lock().unwrap().is_empty());
    assert_eq!(
        engine.dispatches.orders["leverage-owned"].phase,
        OrderDispatchPhase::Queued
    );
    (engine, records, control)
}

#[tokio::test(start_paused = true)]
async fn slow_leverage_does_not_hold_order_admission_or_precede_durable_ownership() {
    let (mut engine, records, control) = start().await;
    for _ in 0..10 {
        engine.service_order_dispatches().await.unwrap();
    }
    assert!(!records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(record, WalRecord::OrderDispatchAttempted { .. })));
    assert!(engine.dispatches.write.is_none());
    assert_eq!(engine.pending_mutations.len(), 1);
    assert_eq!(control.calls.lock().unwrap().len(), 1);
    control.release.notify_one();
}

#[tokio::test(start_paused = true)]
async fn leverage_cannot_cross_a_failed_ownership_barrier() {
    let Fixture {
        mut engine,
        control,
        prepared,
        ..
    } = prepare().await;
    engine.wal.fail_barrier_after = Some("order_sent");
    assert!(engine.queue_order_dispatches(vec![prepared]).is_err());
    tokio::task::yield_now().await;
    assert!(control.calls.lock().unwrap().is_empty());
    assert!(control.sends.lock().unwrap().is_empty());
    assert!(engine.pending_mutations.is_empty());
    assert_eq!(
        engine.dispatches.orders["leverage-owned"].phase,
        OrderDispatchPhase::Queued
    );
}

#[tokio::test(start_paused = true)]
async fn a_fill_and_local_cancel_do_not_wait_for_leverage_and_late_success_cannot_revive() {
    let (mut engine, _, control) = start().await;
    let _working = super::tests::prepared_order(&mut engine, "fill-during-leverage");
    engine
        .complete_order_dispatch("fill-during-leverage")
        .unwrap();
    let fill = OrderUpdate::Fill {
        allocation: None,
        amounts: None,
        exec_id: "leverage-race-fill".into(),
        client_order_id: "fill-during-leverage".into(),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 0.25,
        px: 100.0,
        fee: Some(0.0),
        is_maker: false,
        forced_close: None,
        venue_ts_ms: clock::wall_ms(),
        recv_ns: clock::now_ns(),
    };
    tokio::time::timeout(Duration::from_millis(1), engine.take_update(fill))
        .await
        .expect("a fill waited for leverage")
        .unwrap();
    assert_eq!(
        engine
            .books
            .attribution
            .signed_exact(StrategyId(0), SymbolId(0)),
        engine_types::numeric::Exact::parse_decimal("0.75").unwrap()
    );
    engine.may_open = false;
    tokio::time::timeout(
        Duration::from_millis(1),
        engine.process_cancels(vec![(SymbolId(0), "leverage-owned".into())]),
    )
    .await
    .expect("an unsent cancellation waited for leverage")
    .unwrap();
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    engine.service_order_dispatches().await.unwrap();
    assert!(!engine.dispatches.orders.contains_key("leverage-owned"));
    assert!(!engine.leverage_at.contains_key(&SymbolId(0)));
    assert!(control.sends.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn a_pause_during_leverage_refuses_the_dependent_opening_without_transmitting() {
    let (mut engine, _, control) = start().await;
    engine.may_open = false;
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
    assert!(!engine.leverage_at.contains_key(&SymbolId(0)));
    assert!(control.sends.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn an_ambiguous_leverage_reply_does_not_assert_that_administration_was_not_accepted() {
    let (mut engine, _, control) = start().await;
    control.release.notify_one();
    let mut completion = engine.venue_completions.recv().await.unwrap();
    match &mut completion {
        MutationCompletion::Leverage { reply, .. } => {
            *reply = Err(VenueError::Transport(
                "reply lost after changing one side".into(),
            ))
        }
        _ => panic!("expected leverage completion"),
    }
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(
        engine.books.account.observed_ns, 0,
        "uncertain administration retained a trusted account snapshot"
    );
    assert!(!engine.leverage_at.contains_key(&SymbolId(0)));
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
    assert!(control.sends.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn an_account_observation_during_leverage_prevents_a_stale_completion_from_authorizing() {
    let (mut engine, _, control) = start().await;
    let mut newer = engine.books.account.clone();
    newer.available_usdt -= 1.0;
    newer.exact_amounts = None;
    newer.observed_ns += 1;
    engine.adopt_view(newer.clone());
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(engine.books.account, newer);
    assert!(!engine.leverage_at.contains_key(&SymbolId(0)));
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
    assert!(control.sends.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn a_locally_refused_leverage_request_does_not_invalidate_cleanup_account_state() {
    let (mut engine, _, control) = start().await;
    let account = engine.books.account.clone();
    control.release.notify_one();
    let mut completion = engine.venue_completions.recv().await.unwrap();
    match &mut completion {
        MutationCompletion::Leverage { reply, .. } => {
            *reply = Err(VenueError::BadRequest(
                "fractional leverage is unsupported".into(),
            ))
        }
        _ => panic!("expected leverage completion"),
    }
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(engine.books.account, account);
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
    assert!(control.sends.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn changed_held_position_leverage_needs_account_readback_before_an_opening() {
    let (mut engine, _, control) = start().await;
    // Supply the same held snapshot on both sides of the administrative wait.
    // The set reply alone does not establish its new margin or effective leverage.
    let mut account = engine.books.account.clone();
    account.positions.push(engine_types::PositionView {
        exact_amounts: None,
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 1.0,
        entry_px: 100.0,
        stop_attached: true,
        stop_px: 90.0,
        exact_stop_px: None,
        leverage: Some(1.0),
    });
    engine.books.account = account.clone();
    for pending in engine.pending_mutations.values_mut() {
        if let PendingMutation::Leverage {
            account: before, ..
        } = pending
        {
            *before = Box::new(account.clone());
        }
    }
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(engine.books.account.observed_ns, 0);
    assert!(!engine.leverage_at.contains_key(&SymbolId(0)));
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
    assert!(control.sends.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn confirmed_leverage_requires_a_separate_durable_order_attempt_and_is_cached_once() {
    let (mut engine, records, control) = start().await;
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(engine.leverage_at.get(&SymbolId(0)), Some(&2.0));
    assert_eq!(
        engine.dispatches.orders["leverage-owned"].phase,
        OrderDispatchPhase::Queued
    );
    engine.service_order_dispatches().await.unwrap();
    assert!(control.sends.lock().unwrap().is_empty());
    let result = engine.dispatches.durable.recv().await;
    engine.on_order_dispatch_durable(result).await.unwrap();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(control.calls.lock().unwrap().len(), 1);
    assert_eq!(control.sends.lock().unwrap().len(), 1);
    assert!(
        crate::order_dispatch::OrderDispatches::replay(&records.lock().unwrap())
            .unwrap()
            .orders
            .is_empty()
    );
}

#[tokio::test(start_paused = true)]
async fn a_ready_order_is_submitted_before_a_conflicting_leverage_administration() {
    let (mut engine, _, control) = start().await;
    let mut intent = engine.dispatches.orders["leverage-owned"].intent.clone();
    intent.leverage = Some(3.0);
    intent.decided_ns = clock::now_ns();
    let second = engine
        .prepare_intent(
            intent,
            Some("leverage-second".into()),
            clock::now_ns(),
            None,
            &mut HashMap::new(),
        )
        .await
        .unwrap()
        .unwrap();
    engine.queue_order_dispatches(vec![second]).unwrap();
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    engine.service_order_dispatches().await.unwrap();
    assert!(
        matches!(&engine.dispatches.write,
        Some(DispatchWrite::Attempt(ids)) if ids == &["leverage-owned"]),
        "another leverage setup overtook its already-confirmed dependent order"
    );
    let result = engine.dispatches.durable.recv().await;
    engine.on_order_dispatch_durable(result).await.unwrap();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    assert_eq!(
        control.sends.lock().unwrap()[0].client_order_id,
        "leverage-owned"
    );
    assert_eq!(control.calls.lock().unwrap().len(), 1);
    assert_eq!(
        engine.dispatches.orders["leverage-second"].phase,
        OrderDispatchPhase::Queued
    );
}

#[tokio::test(start_paused = true)]
async fn leverage_invalidated_during_the_attempt_barrier_cannot_be_used_on_the_wire() {
    let (mut engine, _, control) = start().await;
    control.release.notify_one();
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    engine.service_order_dispatches().await.unwrap();
    engine.leverage_at.remove(&SymbolId(0));
    let result = engine.dispatches.durable.recv().await;
    engine.on_order_dispatch_durable(result).await.unwrap();
    assert!(control.sends.lock().unwrap().is_empty());
    assert!(!engine.books.orders.orders["leverage-owned"].in_flight());
}

#[tokio::test(start_paused = true)]
async fn restart_during_leverage_restores_a_never_transmitted_opening_without_resending() {
    let (_engine, records, control) = start().await;
    let records = records.lock().unwrap().clone();
    let (mut restarted, _) = super::tests::fixture().await;
    for record in &records {
        restarted.books.orders.apply(record);
    }
    restarted
        .books
        .registry
        .own("leverage-owned", StrategyId(0));
    restarted.dispatches = crate::order_dispatch::OrderDispatches::replay(&records).unwrap();
    assert_eq!(
        restarted.dispatches.orders["leverage-owned"].phase,
        OrderDispatchPhase::Queued
    );
    restarted.restore_order_dispatches().await.unwrap();
    assert!(restarted.pending_mutations.is_empty());
    assert!(!restarted.books.orders.orders["leverage-owned"].in_flight());
    assert!(restarted.leverage_at.is_empty());
    control.release.notify_one();
}
