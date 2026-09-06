use super::tests::{fixture, prepared_order};
use super::*;

fn source_samples(
    engine: &Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>,
) -> [u64; 4] {
    [
        Segment::Decide,
        Segment::Durable,
        Segment::Wire,
        Segment::EndToEnd,
    ]
    .map(|segment| engine.ledger.quantiles(segment).count)
}

#[tokio::test(start_paused = true)]
async fn replayed_effect_does_not_sample_a_previous_process_decision() {
    let old =
        engine_types::clock::install_virtual(1_700_000_000_000_000_000, 3_600_000_000_000).unwrap();
    let (mut engine, records) = fixture().await;
    let old_decided = clock::now_ns();
    let id = engine.host.effects.capture(
        StrategyId(0),
        vec![Action::Place(Intent {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.5,
            exact_prices: None,
            exact_quantity: None,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "replayed-clock-exit".into(),
            decided_ns: old_decided,
            work: None,
            leverage: None,
        })],
    );
    engine
        .host
        .effects
        .transitions
        .get_mut(&id)
        .unwrap()
        .order_ids[0] = Some("replayed-clock-exit".into());
    let row = WalRecord::StrategyTransitionQueued {
        transition: engine.host.effects.transitions[&id].clone(),
    };
    engine.wal.append(&row).unwrap();
    let bytes = serde_json::to_vec(&row).unwrap();
    let restored: WalRecord = serde_json::from_slice(&bytes).unwrap();
    engine.host.effects = crate::effects::Effects::replay(&[restored], 1).unwrap();
    drop(old);
    let _fresh =
        engine_types::clock::install_virtual(1_700_003_600_000_000_000, 50_000_000).unwrap();
    engine.ledger = crate::ledger::LatencyLedger::new(clock::now_ns());
    engine.restore_strategy_effects().await.unwrap();
    assert_eq!(
        engine.orders_sent, 1,
        "the saved reduction must still execute"
    );
    assert_eq!(
        source_samples(&engine),
        [0; 4],
        "replay invented a source duration"
    );
    assert!(records.lock().unwrap().iter().any(|row| matches!(row,
        WalRecord::Intent { intent } if intent.tag == "replayed-clock-exit" && intent.decided_ns == old_decided
    )), "the historical intent must remain intact");
    assert_eq!(engine.ledger.quantiles(Segment::VenueTask).count, 1);
}

#[tokio::test(start_paused = true)]
async fn replayed_queued_reduction_keeps_id_and_omits_old_source_samples_after_rotation() {
    for rotate in [false, true] {
        let old = engine_types::clock::install_virtual(1_700_000_000_000_000_000, 50_000_000_000)
            .unwrap();
        let (mut engine, records) = fixture().await;
        let order = prepared_order(&mut engine, "prior-epoch-reduction");
        let rows = if rotate {
            vec![engine.rotation_base(clock::wall_ms())]
        } else {
            records.lock().unwrap().clone()
        };
        let bytes = serde_json::to_vec(&rows).unwrap();
        let rows: Vec<WalRecord> = serde_json::from_slice(&bytes).unwrap();
        engine.dispatches = crate::order_dispatch::OrderDispatches::replay(&rows).unwrap();
        assert_eq!(
            engine.dispatches.orders["prior-epoch-reduction"]
                .intent
                .decided_ns,
            order.decided_ns
        );
        drop(old);
        let _fresh =
            engine_types::clock::install_virtual(1_700_000_050_000_000_000, 100_000_000).unwrap();
        engine.ledger = crate::ledger::LatencyLedger::new(clock::now_ns());
        engine.restore_order_dispatches().await.unwrap();
        assert_eq!(engine.pending_mutations.len(), 1);
        let completion = engine.venue_completions.recv().await.unwrap();
        engine.take_venue_completion(completion).await.unwrap();
        assert_eq!(
            source_samples(&engine),
            [0; 4],
            "rotate={rotate}: old clocks became fresh samples"
        );
        assert!(engine.dispatches.orders.is_empty());
        assert_eq!(engine.ledger.quantiles(Segment::DispatchQueue).count, 1);
        assert_eq!(engine.ledger.quantiles(Segment::VenueTask).count, 1);
        assert!(engine.ledger.quantiles(Segment::BarrierWait).count > 0);
        let after = records.lock().unwrap().clone();
        assert_eq!(after.iter().filter(|row| matches!(row, WalRecord::OrderDispatchAttempted { client_order_id } if client_order_id == "prior-epoch-reduction")).count(), 1);
        engine.restore_order_dispatches().await.unwrap();
        assert!(
            engine.pending_mutations.is_empty(),
            "completion must remain idempotent"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn current_process_dispatch_still_samples_its_observed_timing() {
    let _clock =
        engine_types::clock::install_virtual(1_700_000_000_000_000_000, 1_000_000_000).unwrap();
    let (mut engine, _) = fixture().await;
    let order = prepared_order(&mut engine, "same-epoch-reduction");
    engine.queue_order_dispatches(vec![order]).unwrap();
    engine_types::clock::advance_virtual_to(1_020_000_000).unwrap();
    while engine.dispatches.write.is_some() {
        let result = engine.dispatches.durable.recv().await;
        engine.on_order_dispatch_durable(result).await.unwrap();
    }
    let completion = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completion).await.unwrap();
    for segment in [Segment::Durable, Segment::Wire, Segment::EndToEnd] {
        let measured = engine.ledger.quantiles(segment);
        assert_eq!(measured.count, 1);
        assert!(measured.max_ns >= 20_000_000);
    }
}
