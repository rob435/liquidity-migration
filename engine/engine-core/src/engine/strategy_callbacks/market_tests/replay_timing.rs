use super::*;

#[tokio::test(start_paused = true)]
async fn replayed_callback_keeps_new_completion_without_inventing_source_time() {
    let old =
        engine_types::clock::install_virtual(1_700_000_000_000_000_000, 50_000_000_000).unwrap();
    let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
    let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
    let (mut engine, _) = crate::tests::callback_test_fixture(vec![strategy]).await;
    engine
        .books
        .attribution
        .note(StrategyId(0), SymbolId(0), Side::Buy, 1.0);
    let execution = || CallbackExecution::Isolated {
        executable: "/bin/false".into(),
    };
    engine.host.callbacks = CallbackHost::new(execution(), &engine.host.strategies, &[]).unwrap();
    let input = prepare_market(&mut engine, &quote(0, clock::now_ns()));
    let mut queued = input.clone();
    queued.preparation = CallbackPreparation::Queued;
    let rows = [
        WalRecord::StrategyCallbackQueued { input: queued },
        WalRecord::StrategyCallbackPrepared {
            input: input.clone(),
        },
    ];
    let encoded = serde_json::to_vec(&rows).unwrap();
    let rows: Vec<WalRecord> = serde_json::from_slice(&encoded).unwrap();
    engine.host.callbacks = CallbackHost::new(execution(), &engine.host.strategies, &rows).unwrap();
    drop(old);
    let _fresh =
        engine_types::clock::install_virtual(1_700_000_050_000_000_000, 100_000_000).unwrap();
    engine.ledger = crate::ledger::LatencyLedger::new(clock::now_ns());
    let mut proposal = worker_proposal(&engine, &input);
    proposal.actions = vec![Action::Place(Intent {
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 0.5,
        exact_prices: None,
        exact_quantity: None,
        kind: OrderKind::Market,
        stop: None,
        reduce_only: true,
        tag: "replayed-input-exit".into(),
        decided_ns: input.snapshot().unwrap().now_ns,
        work: None,
        leverage: None,
    })];
    let completed = completion(&mut engine, input.callback_id, proposal);
    engine.on_strategy_callback(Some(completed)).unwrap();
    let durable = engine.host.callbacks.durable.recv().await;
    engine.on_callback_durable(durable).unwrap();
    engine_types::clock::advance_virtual_to(120_000_000).unwrap();
    engine.drain(clock::now_ns()).await.unwrap();
    while engine.dispatches.write.is_some() {
        let durable = engine.dispatches.durable.recv().await;
        engine.on_order_dispatch_durable(durable).await.unwrap();
    }
    let completed = engine.venue_completions.recv().await.unwrap();
    engine.take_venue_completion(completed).await.unwrap();
    assert_eq!(engine.orders_sent, 1);
    for segment in [Segment::Decide, Segment::EndToEnd] {
        assert_eq!(
            engine.ledger.quantiles(segment).count,
            0,
            "prior callback input fabricated {segment:?}"
        );
    }
    for segment in [Segment::Durable, Segment::Wire] {
        let measured = engine.ledger.quantiles(segment);
        assert_eq!(measured.count, 1, "current completion lost {segment:?}");
        assert!(measured.max_ns >= 20_000_000);
    }
    engine.host.callbacks.stop().await;
}
