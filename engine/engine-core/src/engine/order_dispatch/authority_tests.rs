//! What happens to an opening that is already in the venue queue when the
//! permission it was admitted under goes away.
//!
//! The worker is held on an administrative call issued straight at the venue
//! task, so it never touches the engine's symbol or mutation bookkeeping and
//! the opening under test can still be dispatched behind it.

use super::*;
use engine_types::order_dispatch::OrderDispatchState;

type TestEngine = Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;

struct Held {
    engine: TestEngine,
    records: std::sync::Arc<std::sync::Mutex<Vec<WalRecord>>>,
    control: crate::tests::LeverageControl,
}

/// An engine whose venue task is parked inside `set_leverage`.
async fn held_worker() -> Held {
    let (mut engine, records) = super::tests::fixture().await;
    let (venue, control) = crate::tests::controlled_leverage_venue();
    (engine.venue, engine.venue_completions) =
        crate::venue_runtime::VenueClient::spawn(venue, engine.authority.clone());
    engine.venue.dispatch_leverage(SymbolId(0), 7.0).unwrap();
    tokio::task::yield_now().await;
    assert_eq!(*control.calls.lock().unwrap(), vec![(SymbolId(0), 7.0)]);
    Held {
        engine,
        records,
        control,
    }
}

/// One opening, durably recorded and queued the way admission leaves it.
fn prepared_opening(engine: &mut TestEngine, id: &str) -> PreparedOrder {
    let intent = Intent {
        exact_prices: None,
        exact_quantity: None,
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.1,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: false,
        tag: "send-boundary".into(),
        decided_ns: clock::now_ns(),
        work: None,
        leverage: None,
    };
    let request = OrderRequest {
        client_order_id: id.into(),
        strategy: intent.strategy,
        symbol: intent.symbol,
        side: intent.side,
        qty: intent.qty,
        kind: intent.kind,
        stop: None,
        reduce_only: false,
        close_position: false,
        sleeve_effect: None,
        exact_terms: None,
    };
    let record = WalRecord::OrderSent {
        dispatch: Some(Box::new(
            engine_types::order_dispatch::QueuedOrderDispatch {
                intent: intent.clone(),
                origin_ns: intent.decided_ns,
            },
        )),
        request: request.clone(),
        wire_ns: clock::now_ns(),
        arrival_mid: 100.0,
    };
    engine.wal.append(&record).unwrap();
    engine.books.registry.own(id, StrategyId(0));
    engine.books.orders.apply(&record);
    engine.dispatches.orders.insert(
        id.into(),
        crate::order_dispatch::RuntimeDispatch {
            state: OrderDispatchState {
                request: request.clone(),
                intent: intent.clone(),
                phase: OrderDispatchPhase::Queued,
                origin_ns: intent.decided_ns,
            },
            timing: Some(crate::ctx::CallbackTiming {
                origin_ns: Some(intent.decided_ns),
                decided_ns: intent.decided_ns,
            }),
        },
    );
    PreparedOrder {
        decided_ns: intent.decided_ns,
        origin_ns: intent.decided_ns,
        intent,
        request,
    }
}

/// Take the opening all the way to `dispatch_orders`: attempted, reserved,
/// and waiting in the venue queue behind the held administration.
async fn queue_opening(engine: &mut TestEngine, id: &str) -> engine_types::CommandAuthority {
    let prepared = prepared_opening(engine, id);
    engine.queue_order_dispatches(vec![prepared]).unwrap();
    let result = engine.dispatches.durable.recv().await;
    engine.on_order_dispatch_durable(result).await.unwrap();
    assert_eq!(
        engine.dispatches.orders[id].phase,
        OrderDispatchPhase::Attempted
    );
    let Some(PendingMutation::Orders {
        authority: Some(authority),
        ..
    }) = engine.pending_mutations.values().next()
    else {
        panic!("the queued opening carries no send authority");
    };
    *authority
}

fn rejected_for(records: &[WalRecord], id: &str) -> Option<String> {
    records.iter().find_map(|record| match record {
        WalRecord::OrderUpdate {
            update:
                OrderUpdate::Reject {
                    client_order_id,
                    reason,
                    ..
                },
            ..
        } if client_order_id == id => Some(reason.clone()),
        _ => None,
    })
}

/// Answer the held leverage, then settle the placement command behind it.
async fn release_and_settle(engine: &mut TestEngine, control: &crate::tests::LeverageControl) {
    control.release.notify_one();
    // The direct administration has no pending mutation of its own.
    let leverage = engine.venue_completions.recv().await.unwrap();
    assert!(matches!(
        leverage,
        crate::venue_runtime::MutationCompletion::Leverage { .. }
    ));
    let placement = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(placement).await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_halt_acknowledged_while_an_opening_waits_in_the_venue_queue_retires_it_unsent() {
    let Held {
        mut engine,
        records,
        control,
    } = held_worker().await;
    let authority = queue_opening(&mut engine, "halted-opening").await;
    assert_eq!(authority.epoch, 1);

    engine.latch_closed();
    assert_eq!(engine.authority.current(), 2);
    release_and_settle(&mut engine, &control).await;

    assert!(
        control.sends.lock().unwrap().is_empty(),
        "the halted opening reached the venue"
    );
    let reason = rejected_for(&records.lock().unwrap(), "halted-opening")
        .expect("the retired opening was never journaled as refused");
    assert_eq!(reason, "never sent: authority: epoch 1 superseded by 2");
    assert!(
        !engine.dispatches.orders.contains_key("halted-opening"),
        "the reservation was left open"
    );
    assert!(!engine.books.orders.orders["halted-opening"].in_flight());
    assert!(
        engine.dispatches.lookup_pending.is_empty() && engine.dispatches.unresolved.is_empty(),
        "an order that was never sent started a status lookup"
    );
}

#[tokio::test(start_paused = true)]
async fn an_opening_that_expires_in_the_venue_queue_is_rejected_without_a_venue_answer() {
    let Held {
        mut engine,
        records,
        control,
    } = held_worker().await;
    let clock_guard = engine_types::clock::install_virtual(
        engine_types::clock::wall_ns(),
        engine_types::clock::mono_ns(),
    )
    .unwrap();
    let authority = queue_opening(&mut engine, "stale-opening").await;

    engine_types::clock::advance_virtual_to(authority.expires_at_ns + 1_500_000_000).unwrap();
    assert_eq!(
        engine.authority.current(),
        authority.epoch,
        "the test must exercise expiry, not supersession"
    );
    release_and_settle(&mut engine, &control).await;

    assert!(
        control.sends.lock().unwrap().is_empty(),
        "the expired opening reached the venue"
    );
    let reason = rejected_for(&records.lock().unwrap(), "stale-opening")
        .expect("the expired opening was never journaled as refused");
    assert_eq!(
        reason, "never sent: authority: expired after 11500 ms in the venue queue",
        "the refusal does not name how long the opening waited"
    );
    assert!(!engine.dispatches.orders.contains_key("stale-opening"));
    assert!(!engine.books.orders.orders["stale-opening"].in_flight());
    drop(clock_guard);
}

#[tokio::test(start_paused = true)]
async fn an_opening_already_handed_to_the_gateway_survives_an_epoch_advance_as_attempted() {
    let (mut engine, records) = super::tests::fixture().await;
    let (venue, sends, _) = crate::tests::delayed_send_venue(Duration::from_millis(50));
    (engine.venue, engine.venue_completions) =
        crate::venue_runtime::VenueClient::spawn(venue, engine.authority.clone());
    // `on_order_dispatch_durable` yields to the venue task, so the gateway
    // already holds this request when the halt below lands.
    queue_opening(&mut engine, "in-flight-opening").await;
    assert_eq!(sends.lock().unwrap().len(), 1);

    engine.latch_closed();
    assert_eq!(engine.authority.current(), 2);
    let placement = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(placement).await.unwrap();

    assert_eq!(
        rejected_for(&records.lock().unwrap(), "in-flight-opening"),
        None,
        "a request the gateway already held was retired as never sent"
    );
    assert!(engine.books.orders.orders["in-flight-opening"].in_flight());
    assert!(!engine.dispatches.orders.contains_key("in-flight-opening"));
}

#[tokio::test(start_paused = true)]
async fn a_slow_leverage_administration_does_not_delay_a_queued_cancel() {
    let Held {
        mut engine,
        control,
        ..
    } = held_worker().await;
    queue_opening(&mut engine, "queued-opening").await;
    let working = super::tests::prepared_order(&mut engine, "already-working");
    engine
        .complete_order_dispatch(&working.request.client_order_id)
        .unwrap();
    engine
        .process_cancels(vec![(SymbolId(0), "already-working".into())])
        .await
        .unwrap();

    control.release.notify_one();
    for _ in 0..3 {
        let completion = engine.venue_completions.recv().await.unwrap();
        if !matches!(
            completion,
            crate::venue_runtime::MutationCompletion::Leverage { .. }
        ) {
            engine.take_venue_completion(completion).await.unwrap();
        }
    }

    let tape = control.tape.lock().unwrap().clone();
    let cancel = tape
        .iter()
        .position(|step| *step == crate::tests::Step::Cancel("already-working".into()))
        .expect("the cancel never reached the venue");
    let opening = tape
        .iter()
        .position(|step| *step == crate::tests::Step::Send("queued-opening".into()))
        .expect("the opening never reached the venue");
    assert!(
        cancel < opening,
        "the cancel waited behind an opening queued before it; tape was {tape:?}"
    );
}

/// A resting limit order the durable ledger knows and the venue is working.
fn resting_limit(engine: &mut TestEngine, id: &str, reduce_only: bool) {
    let request = OrderRequest {
        client_order_id: id.into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 0.1,
        kind: OrderKind::Limit {
            px: 100.0,
            tif: engine_types::TimeInForce::Gtc,
        },
        stop: None,
        reduce_only,
        close_position: false,
        sleeve_effect: None,
        exact_terms: None,
    };
    let record = WalRecord::OrderSent {
        dispatch: None,
        request,
        wire_ns: clock::now_ns(),
        arrival_mid: 100.0,
    };
    engine.wal.append(&record).unwrap();
    engine.books.registry.own(id, StrategyId(0));
    engine.books.orders.apply(&record);
}

/// Reprice a resting order and take it through its durability barrier, the
/// way the loop does, leaving the command in the venue queue.
async fn queue_reprice(engine: &mut TestEngine, id: &str) {
    engine
        .process_amend(
            SymbolId(0),
            id,
            engine_types::AmendSpec {
                px: Some(101.0),
                qty: None,
                exact_terms: None,
            },
            clock::now_ns(),
        )
        .await
        .unwrap();
    let result = engine.dispatches.durable.recv().await;
    engine.on_order_dispatch_durable(result).await.unwrap();
    assert!(
        !engine.pending_mutations.is_empty(),
        "the reprice never reached the venue queue"
    );
}

/// Answer the held leverage, then settle the amend command behind it.
async fn release_and_settle_amend(
    engine: &mut TestEngine,
    control: &crate::tests::LeverageControl,
) {
    control.release.notify_one();
    let leverage = engine.venue_completions.recv().await.unwrap();
    assert!(matches!(
        leverage,
        crate::venue_runtime::MutationCompletion::Leverage { .. }
    ));
    let amend = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(amend).await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn an_openings_reprice_waiting_in_the_venue_queue_is_refused_when_the_epoch_advances() {
    let Held {
        mut engine,
        control,
        ..
    } = held_worker().await;
    resting_limit(&mut engine, "opening-reprice", false);
    queue_reprice(&mut engine, "opening-reprice").await;

    engine.authority.advance();
    release_and_settle_amend(&mut engine, &control).await;

    let tape = control.tape.lock().unwrap().clone();
    assert!(
        !tape.contains(&crate::tests::Step::Amend("opening-reprice".into())),
        "an opening's reprice carried no authority; tape was {tape:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn a_reduce_only_orders_reprice_reaches_the_venue_after_the_epoch_advances() {
    let Held {
        mut engine,
        control,
        ..
    } = held_worker().await;
    resting_limit(&mut engine, "exit-reprice", true);
    queue_reprice(&mut engine, "exit-reprice").await;

    engine.authority.advance();
    release_and_settle_amend(&mut engine, &control).await;

    let tape = control.tape.lock().unwrap().clone();
    assert!(
        tape.contains(&crate::tests::Step::Amend("exit-reprice".into())),
        "an exit's reprice was refused at the send boundary; tape was {tape:?}"
    );
}

/// A dispatch lane the engine has filled is the engine's own backpressure,
/// not a venue fault: the opening is never sent, its reservation is released,
/// and the loop goes on. A refusal raised to the run loop ends the process.
#[tokio::test(start_paused = true)]
async fn a_full_ordinary_lane_refuses_an_opening_unsent_and_the_engine_keeps_running() {
    let Held {
        mut engine,
        records,
        control,
    } = held_worker().await;
    // Symbol admission is answered on its own oneshot, so filling the lane
    // with it never enters the engine's mutation bookkeeping.
    for _ in 0..crate::venue_runtime::COMMAND_CAPACITY {
        engine
            .venue
            .dispatch_symbol_admission(None, Vec::new())
            .expect("the ordinary lane refused before it was full");
    }
    assert!(engine
        .venue
        .dispatch_symbol_admission(None, Vec::new())
        .is_err());

    let prepared = prepared_opening(&mut engine, "lane-full-opening");
    engine.queue_order_dispatches(vec![prepared]).unwrap();
    let result = engine.dispatches.durable.recv().await;
    engine
        .on_order_dispatch_durable(result)
        .await
        .expect("a full dispatch lane ended the run");

    assert!(
        control.sends.lock().unwrap().is_empty(),
        "the refused opening reached the venue"
    );
    let reason = rejected_for(&records.lock().unwrap(), "lane-full-opening")
        .expect("the refused opening was never journaled as refused");
    assert!(
        reason.starts_with("never sent: venue dispatch lane full"),
        "{reason}"
    );
    assert!(
        !engine.dispatches.orders.contains_key("lane-full-opening"),
        "the reservation was left open"
    );
    assert!(!engine.books.orders.orders["lane-full-opening"].in_flight());
    assert!(
        engine.pending_mutations.is_empty(),
        "an opening that was never sent is still counted in flight"
    );

    // Risk-off has its own lane, and the worker takes it first.
    let working = super::tests::prepared_order(&mut engine, "protective");
    engine
        .complete_order_dispatch(&working.request.client_order_id)
        .unwrap();
    assert!(engine
        .process_cancels(vec![(SymbolId(0), "protective".into())])
        .await
        .expect("the cancel was refused"));
    control.release.notify_one();
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
    assert!(
        control
            .tape
            .lock()
            .unwrap()
            .contains(&crate::tests::Step::Cancel("protective".into())),
        "the protective cancel never reached the venue"
    );
    engine
        .service_order_dispatches()
        .await
        .expect("the next pass after a refused lane");
}
