//! External observations are admitted, journaled, routed, and retained as one
//! ordered input stream.

use super::*;
use engine_types::{Action, SignalObservation, SIGNAL_OBSERVATION_SCHEMA_VERSION};

struct FinishedSignals {
    rows: VecDeque<SignalObservation>,
    done: Option<tokio::sync::oneshot::Sender<()>>,
    gaps: Vec<engine_types::SignalGapRequest>,
    blocked_destinations: Vec<StrategyId>,
}

impl engine_types::SignalFeed for FinishedSignals {
    fn set_gap_requests(
        &mut self,
        gaps: &[engine_types::SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), engine_types::SignalError> {
        self.gaps = gaps.to_vec();
        self.blocked_destinations = blocked_destinations.to_vec();
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), engine_types::SignalError> {
        Ok(())
    }

    fn defer_last(
        &mut self,
        observation: SignalObservation,
    ) -> Result<(), engine_types::SignalError> {
        self.rows.push_back(observation);
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, engine_types::SignalError> {
        let eligible = self.rows.iter().position(|row| {
            self.gaps
                .iter()
                .find(|gap| gap.source == row.source)
                .map_or_else(
                    || !self.blocked_destinations.contains(&row.destination),
                    |gap| row.sequence <= gap.next_sequence,
                )
        });
        if let Some(row) = eligible.and_then(|index| self.rows.remove(index)) {
            return Ok(row);
        }
        if let Some(done) = self.done.take() {
            let _ = done.send(());
        }
        Err(engine_types::SignalError::Closed)
    }
}

struct QuietStrategy {
    name: &'static str,
    symbol: &'static str,
}

impl Strategy for QuietStrategy {
    fn name(&self) -> &str {
        self.name
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.into(),
            feed: Feed::Quote,
        }]
    }
}

struct SignalReceiverStrategy {
    signal_ids: Rc<RefCell<Vec<(SymbolId, String)>>>,
    market_names: Rc<RefCell<Vec<String>>>,
    done: Option<tokio::sync::oneshot::Sender<()>>,
}

impl Strategy for SignalReceiverStrategy {
    fn name(&self) -> &str {
        "destination"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "ETHUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_signal(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        let symbol = ctx
            .symbol_id("HELDUSDT")
            .expect("signal symbol is admitted");
        self.signal_ids.lock().unwrap().push((
            symbol,
            ctx.symbol_name(symbol)
                .expect("reverse symbol lookup")
                .into(),
        ));
        assert_eq!(ctx.strategy_id("destination"), Some(StrategyId(1)));
        ctx.emit(Action::ConsumeSignalObservation {
            strategy: StrategyId(99),
            source: observation.source.clone(),
            sequence: observation.sequence,
            observation_id: observation.observation_id.clone(),
        });
        if let Some(done) = self.done.take() {
            let _ = done.send(());
        }
    }

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        if let MarketEvent::Quote { symbol, .. } = event {
            self.market_names
                .lock()
                .unwrap()
                .push(ctx.symbol_name(*symbol).unwrap().into());
        }
    }
}

fn observation(sequence: u64, subscriptions: Vec<Subscription>) -> SignalObservation {
    observation_from("carry-worker", sequence, subscriptions)
}

fn observation_from(
    source: &str,
    sequence: u64,
    subscriptions: Vec<Subscription>,
) -> SignalObservation {
    let mut observation = SignalObservation {
        schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "native-carry-v1".into(),
        destination: StrategyId(1),
        source: source.into(),
        sequence,
        observation_id: format!("universe-{sequence}"),
        kind: "carry_universe".into(),
        observed_wall_ts_ms: 10,
        available_wall_ts_ms: 11,
        subscriptions,
        payload: vec![sequence as u8],
        content_sha256: String::new(),
    };
    observation.content_sha256 = crate::signals::content_sha256(&observation);
    observation
}

#[tokio::test(start_paused = true)]
async fn a_new_worker_generation_starts_at_sequence_one_after_an_old_cursor() {
    let old_source = "directional-public.g11111111111111111111111111111111.carry";
    let new_source = "directional-public.g22222222222222222222222222222222.carry";
    let old = observation_from(old_source, 9, Vec::new());
    let replayed = vec![
        WalRecord::Names {
            strategies: vec!["source".into(), "destination".into()],
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "HELDUSDT".into()],
        },
        WalRecord::SignalObservation {
            wall_ts_ms: 1,
            observation: old.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: 2,
            strategy: StrategyId(1),
            source: old.source,
            sequence: old.sequence,
            observation_id: old.observation_id,
        },
    ];
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let (done, stopped) = tokio::sync::oneshot::channel();
    let (mut engine, h) = build(
        allow_all(),
        vec![
            Box::new(QuietStrategy {
                name: "source",
                symbol: "BTCUSDT",
            }),
            Box::new(SignalReceiverStrategy {
                signal_ids: delivered.clone(),
                market_names: Rc::new(RefCell::new(Vec::new())),
                done: Some(done),
            }),
        ],
        &["BTCUSDT", "ETHUSDT", "HELDUSDT"],
        &replayed,
    )
    .await;
    let (sender, mut signals) = crate::signals::signal_channel();
    sender
        .try_send(observation_from(old_source, 1, Vec::new()))
        .unwrap();
    sender
        .try_send(observation_from(new_source, 1, Vec::new()))
        .unwrap();
    engine
        .run_with_signals(
            &mut ScriptFeed {
                events: VecDeque::new(),
                close_at_end: false,
                admitted: Rc::new(RefCell::new(Vec::new())),
                symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "HELDUSDT".into()],
                admits_wrongly: false,
            },
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = stopped.await;
            },
        )
        .await
        .unwrap();

    assert_eq!(delivered.lock().unwrap().len(), 1);
    let records = h.records.lock().unwrap();
    assert!(records.iter().any(|record| matches!(
        record,
        WalRecord::SignalObservation { observation, .. }
            if observation.source == new_source && observation.sequence == 1
    )));
    assert!(!records.iter().any(|record| matches!(
        record,
        WalRecord::SignalObservation { observation, .. }
            if observation.source == old_source && observation.sequence == 1
    )));
}

#[tokio::test(start_paused = true)]
async fn a_gap_retains_the_last_contiguous_cursor_without_delivering_the_later_row() {
    let source = "directional-public.g11111111111111111111111111111111.carry";
    let old = observation_from(source, 9, Vec::new());
    let replayed = vec![
        WalRecord::Names {
            strategies: vec!["source".into(), "destination".into()],
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "HELDUSDT".into()],
        },
        WalRecord::SignalObservation {
            wall_ts_ms: 1,
            observation: old.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: 2,
            strategy: StrategyId(1),
            source: old.source,
            sequence: old.sequence,
            observation_id: old.observation_id,
        },
    ];
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let (done, stopped) = tokio::sync::oneshot::channel();
    let (mut engine, h) = build(
        allow_all(),
        vec![
            Box::new(QuietStrategy {
                name: "source",
                symbol: "BTCUSDT",
            }),
            Box::new(SignalReceiverStrategy {
                signal_ids: delivered.clone(),
                market_names: Rc::new(RefCell::new(Vec::new())),
                done: None,
            }),
        ],
        &["BTCUSDT", "ETHUSDT", "HELDUSDT"],
        &replayed,
    )
    .await;
    let mut signals = FinishedSignals {
        rows: VecDeque::from([observation_from(source, 11, Vec::new())]),
        done: Some(done),
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    engine
        .run_with_signals(
            &mut ScriptFeed {
                events: VecDeque::new(),
                close_at_end: false,
                admitted: Rc::new(RefCell::new(Vec::new())),
                symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "HELDUSDT".into()],
                admits_wrongly: false,
            },
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = stopped.await;
            },
        )
        .await
        .expect("a gap is not an exit");

    assert!(
        delivered.lock().unwrap().is_empty(),
        "a gap must not reach the reducer"
    );
    let WalRecord::SegmentBase {
        signal_cursors,
        signal_gaps,
        ..
    } = engine.rotation_base(12)
    else {
        panic!()
    };
    assert_eq!(signal_cursors[0].sequence, 9);
    assert_eq!(signal_gaps[0].next_sequence, 10);
    assert_eq!(signal_gaps[0].observed_sequence, 11);
    let records = h.records.lock().unwrap();
    assert!(!records.iter().any(|record| matches!(
        record,
        WalRecord::SignalObservation { observation, .. }
            if observation.source == source && observation.sequence == 11
    )));
}

#[tokio::test(start_paused = true)]
async fn signal_admits_quote_and_ticker_everywhere_before_durable_delivery() {
    let signal_ids = Rc::new(RefCell::new(Vec::new()));
    let market_names = Rc::new(RefCell::new(Vec::new()));
    let (done, stopped) = tokio::sync::oneshot::channel();
    let strategies: Vec<Box<dyn Strategy>> = vec![
        Box::new(QuietStrategy {
            name: "source",
            symbol: "BTCUSDT",
        }),
        Box::new(SignalReceiverStrategy {
            signal_ids: signal_ids.clone(),
            market_names,
            done: Some(done),
        }),
    ];
    let (mut engine, h) = build(
        allow_all(),
        strategies,
        &["BTCUSDT", "ETHUSDT", "HELDUSDT"],
        &[],
    )
    .await;
    let admitted = Rc::new(RefCell::new(Vec::new()));
    let mut market = ScriptFeed {
        events: VecDeque::new(),
        close_at_end: false,
        admitted: admitted.clone(),
        symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        admits_wrongly: false,
    };
    let mut orders = ScriptOrderFeed::empty();
    let learned = orders.learned.clone();
    let (sender, mut signals) = crate::signals::signal_channel();
    let market_subscriptions = vec![
        Subscription {
            symbol: "HELDUSDT".into(),
            feed: Feed::Quote,
        },
        Subscription {
            symbol: "HELDUSDT".into(),
            feed: Feed::Ticker,
        },
    ];
    sender
        .try_send(observation(1, market_subscriptions.clone()))
        .unwrap();
    engine
        .run_with_signals(&mut market, &mut orders, &mut signals, async {
            let _ = stopped.await;
        })
        .await
        .unwrap();

    assert_eq!(
        *signal_ids.lock().unwrap(),
        vec![(SymbolId(2), "HELDUSDT".into())]
    );
    assert_eq!(
        *admitted.lock().unwrap(),
        vec![("HELDUSDT".into(), SymbolId(2))]
    );
    assert_eq!(
        *learned.lock().unwrap(),
        vec![("HELDUSDT".into(), SymbolId(2))]
    );
    let WalRecord::SegmentBase {
        signal_subscriptions,
        ..
    } = engine.rotation_base(recent_replay_ms())
    else {
        unreachable!()
    };
    assert_eq!(signal_subscriptions.len(), 1);
    assert_eq!(signal_subscriptions[0].subscriptions, market_subscriptions);
    let records = h.records.lock().unwrap();
    assert!(records.iter().any(|record| matches!(
        record,
        WalRecord::Names { symbols, .. }
            if symbols == &vec!["BTCUSDT".to_string(), "ETHUSDT".to_string(), "HELDUSDT".to_string()]
    )));
    assert!(records.iter().any(|record| matches!(
        record,
        WalRecord::SignalObservationConsumed {
            strategy: StrategyId(1),
            sequence: 1,
            ..
        }
    )));
    drop(records);
    let start = after_boot(&h.tape);
    let observed = after(&h.tape, &Step::Append("signal_observation".into()), start).unwrap();
    let barrier = after(&h.tape, &Step::Barrier, observed + 1).unwrap();
    let consumed = after(
        &h.tape,
        &Step::Append("signal_observation_consumed".into()),
        observed + 1,
    )
    .unwrap();
    assert!(observed < barrier && barrier < consumed);
}

#[tokio::test(start_paused = true)]
async fn consumed_and_rotated_universe_keeps_held_name_routed_after_restart() {
    let first = observation(
        1,
        vec![Subscription {
            symbol: "HELDUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let second = observation(2, Vec::new());
    let replayed = vec![
        WalRecord::Names {
            strategies: vec!["source".into(), "destination".into()],
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "HELDUSDT".into()],
        },
        WalRecord::SignalObservation {
            wall_ts_ms: 1,
            observation: first.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: 2,
            strategy: StrategyId(1),
            source: first.source.clone(),
            sequence: first.sequence,
            observation_id: first.observation_id.clone(),
        },
        WalRecord::SignalObservation {
            wall_ts_ms: 3,
            observation: second.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: 4,
            strategy: StrategyId(1),
            source: second.source.clone(),
            sequence: second.sequence,
            observation_id: second.observation_id.clone(),
        },
    ];
    let seen_a = Rc::new(RefCell::new(Vec::new()));
    let (mut engine_a, _) = build(
        allow_all(),
        vec![
            Box::new(QuietStrategy {
                name: "source",
                symbol: "BTCUSDT",
            }),
            Box::new(SignalReceiverStrategy {
                signal_ids: Rc::new(RefCell::new(Vec::new())),
                market_names: seen_a.clone(),
                done: None,
            }),
        ],
        &["BTCUSDT", "ETHUSDT", "HELDUSDT"],
        &replayed,
    )
    .await;
    engine_a
        .run(
            &mut ScriptFeed::quotes(SymbolId(2), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(&*seen_a.lock().unwrap(), &["HELDUSDT"]);

    let base = engine_a.rotation_base(recent_replay_ms());
    let WalRecord::SegmentBase {
        signal_observations,
        signal_cursors,
        signal_subscriptions,
        ..
    } = &base
    else {
        unreachable!()
    };
    assert!(signal_observations.is_empty());
    assert_eq!(signal_cursors[0].sequence, 2);
    assert_eq!(signal_subscriptions[0].subscriptions[0].symbol, "HELDUSDT");

    let seen_b = Rc::new(RefCell::new(Vec::new()));
    let (mut engine_b, _) = build(
        allow_all(),
        vec![
            Box::new(QuietStrategy {
                name: "source",
                symbol: "BTCUSDT",
            }),
            Box::new(SignalReceiverStrategy {
                signal_ids: Rc::new(RefCell::new(Vec::new())),
                market_names: seen_b.clone(),
                done: None,
            }),
        ],
        &["BTCUSDT", "ETHUSDT", "HELDUSDT"],
        &[base],
    )
    .await;
    engine_b
        .run(
            &mut ScriptFeed::quotes(SymbolId(2), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(&*seen_b.lock().unwrap(), &["HELDUSDT"]);
}

struct SequenceRecorder {
    name: &'static str,
    delivered: Rc<RefCell<Vec<(u16, String, u64)>>>,
}

impl Strategy for SequenceRecorder {
    fn name(&self) -> &str {
        self.name
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
    fn on_signal(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        self.delivered.lock().unwrap().push((
            observation.destination.0,
            observation.source.clone(),
            observation.sequence,
        ));
        ctx.emit(Action::ConsumeSignalObservation {
            strategy: observation.destination,
            source: observation.source.clone(),
            sequence: observation.sequence,
            observation_id: observation.observation_id.clone(),
        });
    }
}

fn source_row(source: &str, sequence: u64, destination: u16) -> SignalObservation {
    let mut row = observation_from(source, sequence, Vec::new());
    row.destination = StrategyId(destination);
    row.content_sha256 = crate::signals::content_sha256(&row);
    row
}

fn consumed_row(row: SignalObservation) -> Vec<WalRecord> {
    vec![
        WalRecord::SignalObservation {
            wall_ts_ms: recent_replay_ms(),
            observation: row.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: recent_replay_ms(),
            strategy: row.destination,
            source: row.source,
            sequence: row.sequence,
            observation_id: row.observation_id,
        },
    ]
}

#[tokio::test(start_paused = true)]
async fn missing_prefix_is_delivered_before_deferred_rows_without_blocking_an_independent_source() {
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let strategies: Vec<Box<dyn Strategy>> = vec![
        Box::new(SequenceRecorder {
            name: "left",
            delivered: delivered.clone(),
        }),
        Box::new(SequenceRecorder {
            name: "right",
            delivered: delivered.clone(),
        }),
    ];
    let prior = consumed_row(source_row("ordered", 9, 1));
    let (mut engine, harness) = build(allow_all(), strategies, &[], &prior).await;
    let (done, finished) = tokio::sync::oneshot::channel();
    let mut signals = FinishedSignals {
        rows: VecDeque::from([
            source_row("ordered", 11, 1),
            source_row("independent", 1, 0),
            source_row("ordered", 10, 1),
        ]),
        done: Some(done),
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = finished.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(
        *delivered.lock().unwrap(),
        vec![
            (0, "independent".into(), 1),
            (1, "ordered".into(), 10),
            (1, "ordered".into(), 11),
        ]
    );
    let WalRecord::SegmentBase {
        signal_cursors,
        signal_gaps,
        signal_observations,
        ..
    } = engine.rotation_base(10)
    else {
        panic!()
    };
    assert!(signal_gaps.is_empty());
    assert!(signal_observations.is_empty());
    assert_eq!(
        signal_cursors
            .iter()
            .find(|cursor| cursor.source == "ordered")
            .unwrap()
            .sequence,
        11
    );
    let gap = after(
        &harness.tape,
        &Step::Append("signal_gap_recorded".into()),
        0,
    )
    .unwrap();
    let accepted = after(
        &harness.tape,
        &Step::Append("signal_observation".into()),
        gap + 1,
    )
    .unwrap();
    assert!(after(&harness.tape, &Step::Barrier, gap + 1).unwrap() < accepted);
}

struct ScopedSignalBuyer {
    name: &'static str,
    symbol: &'static str,
    dependency: Option<&'static str>,
    reduce_only: bool,
}

impl Strategy for ScopedSignalBuyer {
    fn name(&self) -> &str {
        self.name
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.into(),
            feed: Feed::Quote,
        }]
    }
    fn input_dependencies(&self) -> Vec<String> {
        self.dependency.into_iter().map(str::to_string).collect()
    }
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, .. } = event else {
            return;
        };
        ctx.place(Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(99),
            symbol: *symbol,
            side: if self.reduce_only {
                Side::Sell
            } else {
                Side::Buy
            },
            qty: 0.01,
            kind: OrderKind::Market,
            stop: (!self.reduce_only).then_some(StopSpec {
                trigger_px: 29_000.0,
            }),
            reduce_only: self.reduce_only,
            tag: self.name.into(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }
}

fn scoped_buyers() -> Vec<Box<dyn Strategy>> {
    vec![
        Box::new(ScopedSignalBuyer {
            name: "independent",
            symbol: "BTCUSDT",
            dependency: None,
            reduce_only: false,
        }),
        Box::new(ScopedSignalBuyer {
            name: "source",
            symbol: "ETHUSDT",
            dependency: None,
            reduce_only: false,
        }),
        Box::new(ScopedSignalBuyer {
            name: "dependent",
            symbol: "SOLUSDT",
            dependency: Some("source"),
            reduce_only: false,
        }),
    ]
}

fn gap_history() -> Vec<WalRecord> {
    let mut records = vec![WalRecord::Names {
        strategies: vec!["independent".into(), "source".into(), "dependent".into()],
        symbols: vec!["BTCUSDT".into(), "ETHUSDT".into(), "SOLUSDT".into()],
    }];
    records.extend(consumed_row(source_row("worker.g1", 9, 1)));
    records.push(WalRecord::SignalGapRecorded {
        wall_ts_ms: recent_replay_ms(),
        gap: engine_types::SignalGap {
            source: "worker.g1".into(),
            destination: StrategyId(1),
            next_sequence: 10,
            observed_sequence: 11,
        },
    });
    records.extend(consumed_row(source_row("worker.g2", 1, 1)));
    records
}

#[tokio::test(start_paused = true)]
async fn source_gap_blocks_dependent_openings_across_restart_rotation_and_generation_change() {
    let prior = gap_history();
    let (engine, _) = build(
        allow_all(),
        scoped_buyers(),
        &["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        &prior,
    )
    .await;
    let rotated = engine.rotation_base(recent_replay_ms());
    let WalRecord::SegmentBase { signal_gaps, .. } = &rotated else {
        panic!()
    };
    assert_eq!(signal_gaps.len(), 1);
    assert_eq!(signal_gaps[0].source, "worker.g1");
    drop(engine);
    for records in [prior, vec![rotated]] {
        let (mut engine, harness) = build(
            allow_all(),
            scoped_buyers(),
            &["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            &records,
        )
        .await;
        let mut market = ScriptFeed::quotes(SymbolId(0), 1, true);
        market.symbols = vec!["BTCUSDT".into(), "ETHUSDT".into(), "SOLUSDT".into()];
        market
            .events
            .extend(ScriptFeed::quotes(SymbolId(1), 1, true).events);
        market
            .events
            .extend(ScriptFeed::quotes(SymbolId(2), 1, true).events);
        engine
            .run(
                &mut market,
                &mut ScriptOrderFeed::empty(),
                std::future::pending(),
            )
            .await
            .unwrap();
        let sends = harness.sends.lock().unwrap();
        assert_eq!(sends.len(), 1);
        assert_eq!(sends[0].strategy, StrategyId(0));
        assert_eq!(harness.records.lock().unwrap().iter().filter(|record| matches!(record,
            WalRecord::Verdict { verdict: RiskVerdict::Deny { reason: DenyReason::UnknownState { detail } }, .. }
                if detail.contains("signal_sequence_gap")
        )).count(), 2);
    }
}

struct SignalEditor {
    name: &'static str,
    symbol: &'static str,
    dependency: Option<&'static str>,
    actions: Vec<Action>,
}

impl Strategy for SignalEditor {
    fn name(&self) -> &str {
        self.name
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.into(),
            feed: Feed::Quote,
        }]
    }
    fn input_dependencies(&self) -> Vec<String> {
        self.dependency.into_iter().map(str::to_string).collect()
    }
    fn on_market(&mut self, _: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        for action in std::mem::take(&mut self.actions) {
            ctx.emit(action);
        }
    }
}

fn working_order(owner: u16, symbol: u16, reduce_only: bool) -> (WalRecord, VenueOrder) {
    let id = format!("eng-gap-{owner}-{reduce_only}");
    let side = if reduce_only { Side::Sell } else { Side::Buy };
    let record = WalRecord::OrderSent {
        dispatch: None,
        request: OrderRequest {
            client_order_id: id.clone(),
            strategy: StrategyId(owner),
            symbol: SymbolId(symbol),
            side,
            qty: 0.01,
            kind: OrderKind::Limit {
                px: 30_000.0,
                tif: TimeInForce::Gtc,
            },
            stop: (!reduce_only).then_some(StopSpec {
                trigger_px: 29_000.0,
            }),
            reduce_only,
            exact_terms: None,
            sleeve_effect: None,
            close_position: false,
        },
        wire_ns: 1,
        arrival_mid: 30_000.0,
    };
    let mut working = still_working(
        &id,
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"][symbol as usize],
        0.01,
    );
    working.side = side;
    working.reduce_only = reduce_only;
    (record, working)
}

#[tokio::test(start_paused = true)]
async fn a_source_gap_cancels_only_affected_openings_and_keeps_exit_edits_live() {
    let mut prior = gap_history();
    let mut working = Vec::new();
    for (owner, reduce_only) in [(0, false), (1, false), (2, false), (2, true)] {
        let (record, row) = working_order(owner, owner, reduce_only);
        prior.push(record);
        working.push(row);
    }
    let (mut held_order, _) = working_order(2, 2, false);
    let WalRecord::OrderSent { request, .. } = &mut held_order else {
        panic!()
    };
    request.client_order_id = "eng-held-2".into();
    prior.push(held_order);
    prior.push(WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "gap-edit-held-fill".into(),
            client_order_id: "eng-held-2".into(),
            symbol: SymbolId(2),
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: Some(0.01),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: recent_replay_ms(),
            recv_ns: 2,
        },
    });
    let edit = |symbol, id: &str| Action::Amend {
        symbol: SymbolId(symbol),
        client_order_id: id.into(),
        spec: AmendSpec {
            exact_terms: None,
            px: Some(30_001.0),
            qty: None,
        },
    };
    let strategies: Vec<Box<dyn Strategy>> = vec![
        Box::new(SignalEditor {
            name: "independent",
            symbol: "BTCUSDT",
            dependency: None,
            actions: vec![edit(0, "eng-gap-0-false")],
        }),
        Box::new(SignalEditor {
            name: "source",
            symbol: "ETHUSDT",
            dependency: None,
            actions: vec![edit(1, "eng-gap-1-false")],
        }),
        Box::new(SignalEditor {
            name: "dependent",
            symbol: "SOLUSDT",
            dependency: Some("source"),
            actions: vec![
                edit(2, "eng-gap-2-false"),
                edit(2, "eng-gap-2-true"),
                Action::SetStop {
                    symbol: SymbolId(2),
                    trigger_px: 29_500.0,
                },
            ],
        }),
    ];
    let (mut engine, harness) = build_with_venue_state(
        allow_all(),
        strategies,
        &["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        &prior,
        working,
        vec![engine_types::PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(2),
            side: Side::Buy,
            qty: 0.01,
            entry_px: 30_000.0,
            stop_attached: true,
            stop_px: 29_000.0,
            leverage: None,
        }],
    )
    .await;
    let mut market = ScriptFeed::quotes(SymbolId(0), 1, true);
    market.symbols = vec!["BTCUSDT".into(), "ETHUSDT".into(), "SOLUSDT".into()];
    market
        .events
        .extend(ScriptFeed::quotes(SymbolId(1), 1, true).events);
    market
        .events
        .extend(ScriptFeed::quotes(SymbolId(2), 1, true).events);
    engine
        .run(
            &mut market,
            &mut ScriptOrderFeed::empty(),
            std::future::pending(),
        )
        .await
        .unwrap();
    let mut cancelled: Vec<_> = harness
        .cancels
        .lock()
        .unwrap()
        .iter()
        .map(|(_, id)| id.clone())
        .collect();
    cancelled.sort();
    assert_eq!(cancelled, ["eng-gap-1-false", "eng-gap-2-false"]);
    let amended: Vec<_> = harness
        .amends
        .lock()
        .unwrap()
        .iter()
        .map(|(_, id, _)| id.clone())
        .collect();
    assert_eq!(amended, ["eng-gap-0-false", "eng-gap-2-true"]);
    assert!(harness
        .stops
        .lock()
        .unwrap()
        .contains(&(SymbolId(2), 29_500.0)));
    assert!(note_saying(&harness.records, "eng-gap-1-false not amended")
        .contains("signal_sequence_gap"));
}

#[tokio::test(start_paused = true)]
async fn a_source_gap_preserves_attributed_reduce_only_placements() {
    let mut prior = gap_history();
    let (record, _) = working_order(2, 2, false);
    prior.push(record);
    prior.push(WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "gap-held-fill".into(),
            client_order_id: "eng-gap-2-false".into(),
            symbol: SymbolId(2),
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: Some(0.01),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: recent_replay_ms(),
            recv_ns: 2,
        },
    });
    let mut strategies = scoped_buyers();
    strategies[2] = Box::new(ScopedSignalBuyer {
        name: "dependent",
        symbol: "SOLUSDT",
        dependency: Some("source"),
        reduce_only: true,
    });
    let (mut engine, harness) = build_with_venue_state(
        allow_all(),
        strategies,
        &["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        &prior,
        vec![],
        vec![engine_types::PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(2),
            side: Side::Buy,
            qty: 0.01,
            entry_px: 30_000.0,
            stop_attached: true,
            stop_px: 29_000.0,
            leverage: None,
        }],
    )
    .await;
    let mut market = ScriptFeed::quotes(SymbolId(2), 1, true);
    market.symbols = vec!["BTCUSDT".into(), "ETHUSDT".into(), "SOLUSDT".into()];
    engine
        .run(
            &mut market,
            &mut ScriptOrderFeed::empty(),
            std::future::pending(),
        )
        .await
        .unwrap();
    let sends = harness.sends.lock().unwrap();
    assert_eq!(sends.len(), 1);
    assert!(sends[0].reduce_only);
    assert_eq!(sends[0].strategy, StrategyId(2));
    assert_eq!(sends[0].qty, 0.01);
}

#[tokio::test(start_paused = true)]
async fn disk_spool_recovers_a_missing_prefix_after_engine_restart_and_rotation() {
    let directory = temp_path("signal-gap-restart");
    std::fs::create_dir_all(directory.path()).unwrap();
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let strategies = || {
        vec![Box::new(SequenceRecorder {
            name: "one",
            delivered: delivered.clone(),
        }) as Box<dyn Strategy>]
    };
    let source = "disk-worker.g1";
    let prior = consumed_row(source_row(source, 9, 0));
    let (mut engine, harness) = build(allow_all(), strategies(), &[], &prior).await;
    let later = source_row(source, 11, 0);
    let mut spool = crate::signals::SpoolSignalFeed::new(directory.path())
        .with_poll_interval(Duration::from_millis(1));
    let later_path = spool.path_for(&later);
    std::fs::write(&later_path, serde_json::to_vec(&later).unwrap()).unwrap();
    let records = harness.records.clone();
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut spool,
            async move {
                loop {
                    if records
                        .lock()
                        .unwrap()
                        .iter()
                        .any(|row| matches!(row, WalRecord::SignalGapRecorded { .. }))
                    {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            },
        )
        .await
        .unwrap();
    assert!(delivered.lock().unwrap().is_empty());
    assert!(later_path.exists());
    let rotated = engine.rotation_base(recent_replay_ms());
    drop(engine);
    drop(spool);
    let mut spool = crate::signals::SpoolSignalFeed::new(directory.path())
        .with_poll_interval(Duration::from_millis(1));
    let missing = source_row(source, 10, 0);
    std::fs::write(
        spool.path_for(&missing),
        serde_json::to_vec(&missing).unwrap(),
    )
    .unwrap();
    let (mut engine, harness) = build(allow_all(), strategies(), &[], &[rotated]).await;
    let seen = delivered.clone();
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut spool,
            async move {
                while seen.lock().unwrap().len() < 2 {
                    tokio::task::yield_now().await;
                }
            },
        )
        .await
        .unwrap();
    assert_eq!(
        *delivered.lock().unwrap(),
        [(0, source.into(), 10), (0, source.into(), 11)]
    );
    assert_eq!(
        harness
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|row| matches!(row, WalRecord::SignalObservation { .. }))
            .count(),
        2
    );
    let WalRecord::SegmentBase {
        signal_gaps,
        signal_cursors,
        ..
    } = engine.rotation_base(12)
    else {
        panic!()
    };
    assert!(signal_gaps.is_empty());
    assert_eq!(signal_cursors[0].sequence, 11);
}

#[tokio::test(start_paused = true)]
async fn signal_acceptance_and_gap_barrier_failures_never_acknowledge_the_spool_row() {
    use engine_types::SignalFeed;
    for (sequence, failed_kind) in [(1, "signal_observation"), (3, "signal_gap_recorded")] {
        let directory = temp_path("signal-barrier-failure");
        std::fs::create_dir_all(directory.path()).unwrap();
        let row = source_row("worker.g1", sequence, 0);
        let mut spool = crate::signals::SpoolSignalFeed::new(directory.path());
        let path = spool.path_for(&row);
        std::fs::write(&path, serde_json::to_vec(&row).unwrap()).unwrap();
        let tape = tape();
        let (mut wal, _) = MockWal::new(tape.clone());
        wal.fail_barrier_after = Some(failed_kind);
        let (risk, _) = MockRisk::with(allow_all());
        let (venue, _) = MockVenue::new(tape, &[]);
        let delivered = Rc::new(RefCell::new(Vec::new()));
        let mut engine = Engine::boot(
            &settings(),
            "signal-barrier-test",
            wal,
            risk,
            venue,
            vec![Box::new(SequenceRecorder {
                name: "one",
                delivered: delivered.clone(),
            })],
            &[],
        )
        .await
        .unwrap();
        let result = engine
            .run_with_signals(
                &mut ScriptFeed::quotes(SymbolId(0), 0, false),
                &mut ScriptOrderFeed::empty(),
                &mut spool,
                std::future::pending(),
            )
            .await;
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("test barrier failure"));
        assert!(delivered.lock().unwrap().is_empty());
        assert!(path.exists());
        let WalRecord::SegmentBase {
            signal_gaps,
            signal_cursors,
            ..
        } = engine.rotation_base(12)
        else {
            panic!()
        };
        assert!(signal_gaps.is_empty());
        assert!(signal_cursors.is_empty());
        drop(spool);
        let mut restarted = crate::signals::SpoolSignalFeed::new(directory.path());
        let recovered = tokio::time::timeout(Duration::from_secs(1), restarted.next_observation())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(recovered, row);
    }
}

#[tokio::test(start_paused = true)]
async fn newer_generations_wait_for_older_gaps_before_reducer_delivery() {
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let mut prior = consumed_row(source_row("worker.g1", 9, 0));
    prior.push(WalRecord::SignalGapRecorded {
        wall_ts_ms: recent_replay_ms(),
        gap: engine_types::SignalGap {
            source: "worker.g1".into(),
            destination: StrategyId(0),
            next_sequence: 10,
            observed_sequence: 11,
        },
    });
    let (mut engine, _) = build(
        allow_all(),
        vec![Box::new(SequenceRecorder {
            name: "one",
            delivered: delivered.clone(),
        })],
        &[],
        &prior,
    )
    .await;
    let (done, finished) = tokio::sync::oneshot::channel();
    let mut signals = FinishedSignals {
        rows: VecDeque::from([
            source_row("worker.g2", 1, 0),
            source_row("worker.g1", 10, 0),
            source_row("worker.g1", 11, 0),
        ]),
        done: Some(done),
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = finished.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(
        *delivered.lock().unwrap(),
        [
            (0, "worker.g1".into(), 10),
            (0, "worker.g1".into(), 11),
            (0, "worker.g2".into(), 1)
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn runtime_entry_permission_also_cancels_and_refuses_amends_only_for_its_owner() {
    let mut pause = engine_types::RuntimeControlRequest {
        schema_version: engine_types::STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
        strategy: StrategyId(1),
        strategy_name: "source".into(),
        request_id: "pause-source".into(),
        command: engine_types::RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
        content_sha256: String::new(),
    };
    pause.content_sha256 = crate::controls::content_sha256(&pause);
    let mut prior: Vec<_> = gap_history()
        .into_iter()
        .filter(|record| matches!(record, WalRecord::Names { .. }))
        .collect();
    prior.push(WalRecord::RuntimeControlAccepted {
        wall_ts_ms: recent_replay_ms(),
        request: pause,
    });
    let mut working = Vec::new();
    let mut strategies: Vec<Box<dyn Strategy>> = Vec::new();
    let mut market = ScriptFeed::quotes(SymbolId(0), 0, true);
    market.symbols = vec!["BTCUSDT".into(), "ETHUSDT".into(), "SOLUSDT".into()];
    for (owner, (name, symbol)) in [
        ("independent", "BTCUSDT"),
        ("source", "ETHUSDT"),
        ("dependent", "SOLUSDT"),
    ]
    .into_iter()
    .enumerate()
    {
        let (record, row) = working_order(owner as u16, owner as u16, false);
        strategies.push(Box::new(SignalEditor {
            name,
            symbol,
            dependency: (owner == 2).then_some("source"),
            actions: vec![Action::Amend {
                symbol: SymbolId(owner as u16),
                client_order_id: row.client_order_id.clone(),
                spec: AmendSpec {
                    exact_terms: None,
                    px: Some(30_001.0),
                    qty: None,
                },
            }],
        }));
        prior.push(record);
        working.push(row);
        market
            .events
            .extend(ScriptFeed::quotes(SymbolId(owner as u16), 1, true).events);
    }
    let (mut engine, harness) = build_with_venue_orders(
        allow_all(),
        strategies,
        &["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        &prior,
        working,
    )
    .await;
    engine
        .run(
            &mut market,
            &mut ScriptOrderFeed::empty(),
            std::future::pending(),
        )
        .await
        .unwrap();
    assert_eq!(
        *harness.cancels.lock().unwrap(),
        [(SymbolId(1), "eng-gap-1-false".into())]
    );
    let amended: Vec<_> = harness
        .amends
        .lock()
        .unwrap()
        .iter()
        .map(|(_, id, _)| id.clone())
        .collect();
    assert_eq!(amended, ["eng-gap-0-false", "eng-gap-2-false"]);
    assert!(note_saying(&harness.records, "eng-gap-1-false not amended")
        .contains("runtime_entries_disabled"));
}

#[tokio::test(start_paused = true)]
async fn a_noop_consumer_backpressures_its_next_row_and_preserves_an_independent_consumer() {
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, harness) = build(
        allow_all(),
        vec![
            Box::new(QuietStrategy {
                name: "noop",
                symbol: "BTCUSDT",
            }),
            Box::new(SequenceRecorder {
                name: "healthy",
                delivered: delivered.clone(),
            }),
        ],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let (done, finished) = tokio::sync::oneshot::channel();
    let mut signals = FinishedSignals {
        rows: VecDeque::from([
            source_row("noop.g1", 1, 0),
            source_row("noop.g1", 2, 0),
            source_row("healthy.g1", 1, 1),
        ]),
        done: Some(done),
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = finished.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(delivered.lock().unwrap().len(), 1);
    let records = harness.records.lock().unwrap();
    let noop_accepted = records.iter().filter(|record| matches!(record,
        WalRecord::SignalObservation { observation, .. } if observation.destination == StrategyId(0)
    )).count();
    assert_eq!(
        noop_accepted, 1,
        "an unfinished consumer owns only its current delivery"
    );
    assert_eq!(
        signals.rows.len(),
        1,
        "unaccepted bytes remain with the producer"
    );
    let WalRecord::SegmentBase {
        signal_observations,
        ..
    } = engine.rotation_base(recent_replay_ms())
    else {
        panic!()
    };
    assert_eq!(signal_observations.len(), 1);
}

struct RequiredProducerBuyer(ScopedSignalBuyer);

impl Strategy for RequiredProducerBuyer {
    fn name(&self) -> &str {
        self.0.name()
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        self.0.subscriptions()
    }
    fn requires_signal_readiness(&self) -> bool {
        true
    }
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        self.0.on_market(event, ctx);
    }
}

#[tokio::test(start_paused = true)]
async fn startup_frontier_absence_blocks_restored_openings() {
    let strategies = vec![Box::new(RequiredProducerBuyer(ScopedSignalBuyer {
        name: "restored",
        symbol: "BTCUSDT",
        dependency: None,
        reduce_only: false,
    })) as Box<dyn Strategy>];
    let prior = consumed_row(source_row("producer.g1", 9, 0));
    let (mut engine, harness) = build(allow_all(), strategies, &["BTCUSDT"], &prior).await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending(),
        )
        .await
        .unwrap();
    assert!(
        harness.sends.lock().unwrap().is_empty(),
        "restored growth requires producer participation before unseen history can be ruled out"
    );
}

#[tokio::test(start_paused = true)]
async fn an_absent_producer_does_not_strand_attributed_reductions_or_protective_stops() {
    let mut prior = consumed_row(source_row("producer.g1", 9, 0));
    let (record, _) = working_order(0, 0, false);
    prior.push(record);
    prior.push(WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "producer-outage-fill".into(),
            client_order_id: "eng-gap-0-false".into(),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            px: 30_000.0,
            fee: Some(0.01),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: recent_replay_ms(),
            recv_ns: 2,
        },
    });
    let (mut engine, harness) = build_with_venue_state(
        allow_all(),
        vec![Box::new(RequiredProducerBuyer(ScopedSignalBuyer {
            name: "restored",
            symbol: "BTCUSDT",
            dependency: None,
            reduce_only: true,
        }))],
        &["BTCUSDT"],
        &prior,
        vec![],
        vec![engine_types::PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            entry_px: 30_000.0,
            stop_attached: true,
            stop_px: 29_000.0,
            leverage: None,
        }],
    )
    .await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending(),
        )
        .await
        .unwrap();
    let sends = harness.sends.lock().unwrap();
    assert_eq!(sends.len(), 1);
    assert!(sends[0].reduce_only);
    assert_eq!(sends[0].qty, 0.01);
    assert!(
        harness.stops.lock().unwrap().is_empty(),
        "recovery retains the venue's existing protective stop"
    );
}

struct RejectingSignalConsumer;
impl Strategy for RejectingSignalConsumer {
    fn name(&self) -> &str {
        "rejecting"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
    fn on_signal(&mut self, row: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        ctx.emit(Action::RejectSignalObservation {
            strategy: row.destination,
            source: row.source.clone(),
            sequence: row.sequence,
            observation_id: row.observation_id.clone(),
            reason: "invalid payload".into(),
        });
    }
}

#[tokio::test(start_paused = true)]
async fn rejection_barrier_failure_retains_accepted_input_for_restart() {
    let tape = tape();
    let (mut wal, records) = MockWal::new(tape.clone());
    wal.fail_barrier_after = Some("signal_observation_rejected");
    let (risk, _) = MockRisk::with(allow_all());
    let (venue, _) = MockVenue::new(tape, &[]);
    let mut engine = Engine::boot(
        &settings(),
        "rejected-input",
        wal,
        risk,
        venue,
        vec![Box::new(RejectingSignalConsumer)],
        &[],
    )
    .await
    .unwrap();
    let mut signals = FinishedSignals {
        rows: VecDeque::from([source_row("reject.g1", 1, 0)]),
        done: None,
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    let result = engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            std::future::pending(),
        )
        .await;
    assert!(result
        .unwrap_err()
        .to_string()
        .contains("test barrier failure"));
    let WalRecord::SegmentBase {
        signal_observations,
        ..
    } = engine.rotation_base(recent_replay_ms())
    else {
        panic!()
    };
    assert_eq!(signal_observations.len(), 1);
    let log = records.lock().unwrap();
    assert!(!log
        .iter()
        .any(|row| matches!(row, WalRecord::SignalObservationConsumed { .. })));
    let cut = log
        .iter()
        .position(|row| matches!(row, WalRecord::SignalObservationRejected { .. }))
        .unwrap();
    let prefix = &log[..cut];
    assert_eq!(
        crate::signal_state::SignalState::replay(prefix, 1)
            .unwrap()
            .observations()
            .count(),
        1
    );
    assert_eq!(
        crate::signal_state::SignalState::replay(&log, 1)
            .unwrap()
            .observations()
            .count(),
        0
    );
}

#[tokio::test(start_paused = true)]
async fn rejecting_consumers_release_capacity_and_terminal_outcomes_survive_rotation() {
    let (mut engine, harness) = build(
        allow_all(),
        vec![Box::new(RejectingSignalConsumer)],
        &[],
        &[],
    )
    .await;
    let (done, finished) = tokio::sync::oneshot::channel();
    let mut signals = FinishedSignals {
        rows: (1..=300)
            .map(|seq| source_row("reject.g1", seq, 0))
            .collect(),
        done: Some(done),
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = finished.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(
        harness
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|row| matches!(row, WalRecord::SignalObservationRejected { .. }))
            .count(),
        300
    );
    let base = engine.rotation_base(recent_replay_ms());
    let state = crate::signal_state::SignalState::replay(&[base], 1).unwrap();
    assert_eq!(state.observations().count(), 0);
    assert_eq!(state.cursors().next().unwrap().sequence, 300);
    assert_eq!(
        state.classify(&source_row("reject.g1", 300, 0)).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
}

struct FrontierSignals {
    inner: FinishedSignals,
    frontiers: Option<Vec<engine_types::SignalSourceFrontier>>,
}
impl engine_types::SignalFeed for FrontierSignals {
    fn set_gap_requests(
        &mut self,
        gaps: &[engine_types::SignalGapRequest],
        blocked: &[StrategyId],
    ) -> Result<(), engine_types::SignalError> {
        self.inner.set_gap_requests(gaps, blocked)
    }
    fn acknowledge_last(&mut self) -> Result<(), engine_types::SignalError> {
        self.inner.acknowledge_last()
    }
    fn defer_last(&mut self, row: SignalObservation) -> Result<(), engine_types::SignalError> {
        self.inner.defer_last(row)
    }
    async fn next_observation(&mut self) -> Result<SignalObservation, engine_types::SignalError> {
        match self.inner.next_observation().await {
            Err(engine_types::SignalError::Closed) => std::future::pending().await,
            result => result,
        }
    }
    async fn next_event(
        &mut self,
    ) -> Result<engine_types::SignalFeedEvent, engine_types::SignalError> {
        if let Some(frontiers) = self.frontiers.take() {
            return Ok(engine_types::SignalFeedEvent::Ready(frontiers));
        }
        self.next_observation()
            .await
            .map(engine_types::SignalFeedEvent::Observation)
    }
}

struct MarketAfterSignals {
    ready: Option<tokio::sync::oneshot::Receiver<()>>,
    feed: ScriptFeed,
}
impl MarketFeed for MarketAfterSignals {
    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        self.feed.admit(symbol, feed)
    }
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if let Some(ready) = self.ready.as_mut() {
            let _ = ready.await;
        }
        self.ready = None;
        self.feed.next_event().await
    }
}

struct RequiredConsumingBuyer(RequiredProducerBuyer);
impl Strategy for RequiredConsumingBuyer {
    fn name(&self) -> &str {
        self.0.name()
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        self.0.subscriptions()
    }
    fn requires_signal_readiness(&self) -> bool {
        true
    }
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        self.0.on_market(event, ctx);
    }
    fn on_signal(&mut self, row: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        ctx.emit(Action::ConsumeSignalObservation {
            strategy: row.destination,
            source: row.source.clone(),
            sequence: row.sequence,
            observation_id: row.observation_id.clone(),
        });
    }
}

#[tokio::test(start_paused = true)]
async fn declared_producer_frontier_requires_all_missing_rows_before_restored_growth() {
    let strategies = vec![Box::new(RequiredConsumingBuyer(RequiredProducerBuyer(
        ScopedSignalBuyer {
            name: "restored",
            symbol: "BTCUSDT",
            dependency: None,
            reduce_only: false,
        },
    ))) as Box<dyn Strategy>];
    let (mut engine, harness) = build(
        allow_all(),
        strategies,
        &["BTCUSDT"],
        &consumed_row(source_row("producer.g1", 9, 0)),
    )
    .await;
    let (done, ready) = tokio::sync::oneshot::channel();
    let mut signals = FrontierSignals {
        inner: FinishedSignals {
            rows: VecDeque::from([
                source_row("producer.g1", 12, 0),
                source_row("producer.g1", 10, 0),
                source_row("producer.g1", 11, 0),
            ]),
            done: Some(done),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        frontiers: Some(vec![engine_types::SignalSourceFrontier {
            source: "producer.g1".into(),
            destination: StrategyId(0),
            published_through: 12,
        }]),
    };
    engine
        .run_with_signals(
            &mut MarketAfterSignals {
                ready: Some(ready),
                feed: ScriptFeed::quotes(SymbolId(0), 1, true),
            },
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            std::future::pending(),
        )
        .await
        .unwrap();
    assert_eq!(harness.sends.lock().unwrap().len(), 1);
    let records = harness.records.lock().unwrap();
    let accepted = records
        .iter()
        .filter_map(|row| match row {
            WalRecord::SignalObservation { observation, .. } => Some(observation.sequence),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(accepted, [10, 11, 12]);
    let gap = records.iter().position(|row| matches!(row, WalRecord::SignalGapRecorded { gap, .. } if gap.next_sequence == 10 && gap.observed_sequence == 12)).unwrap();
    let send = records
        .iter()
        .position(|row| matches!(row, WalRecord::OrderSent { .. }))
        .unwrap();
    assert!(gap < send);
}

struct RequiredQuiet;
impl Strategy for RequiredQuiet {
    fn name(&self) -> &str {
        "required"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn requires_signal_readiness(&self) -> bool {
        true
    }
}

#[tokio::test(start_paused = true)]
async fn startup_unready_cancels_existing_growth_but_keeps_reduction_orders() {
    let mut prior = consumed_row(source_row("producer.g1", 9, 0));
    let mut working = Vec::new();
    for reduce_only in [false, true] {
        let (record, order) = working_order(0, 0, reduce_only);
        prior.push(record);
        working.push(order);
    }
    let (mut engine, harness) = build_with_venue_state(
        allow_all(),
        vec![Box::new(RequiredQuiet)],
        &["BTCUSDT"],
        &prior,
        working,
        vec![],
    )
    .await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending(),
        )
        .await
        .unwrap();
    assert_eq!(
        *harness.cancels.lock().unwrap(),
        vec![(SymbolId(0), "eng-gap-0-false".into())],
        "existing growth needs the same producer readiness as a new opening"
    );
}

#[tokio::test(start_paused = true)]
async fn rewound_producer_refusal_keeps_the_engine_running_and_growth_blocked() {
    let strategies = vec![Box::new(RequiredProducerBuyer(ScopedSignalBuyer {
        name: "restored",
        symbol: "BTCUSDT",
        dependency: None,
        reduce_only: false,
    })) as Box<dyn Strategy>];
    let (mut engine, harness) = build(
        allow_all(),
        strategies,
        &["BTCUSDT"],
        &consumed_row(source_row("producer.g1", 9, 0)),
    )
    .await;
    let (done, ready) = tokio::sync::oneshot::channel();
    let mut signals = FrontierSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: Some(done),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        frontiers: Some(vec![engine_types::SignalSourceFrontier {
            source: "producer.g1".into(),
            destination: StrategyId(0),
            published_through: 0,
        }]),
    };
    engine
        .run_with_signals(
            &mut MarketAfterSignals {
                ready: Some(ready),
                feed: ScriptFeed::quotes(SymbolId(0), 1, true),
            },
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            std::future::pending(),
        )
        .await
        .unwrap();
    assert!(harness.sends.lock().unwrap().is_empty());
    assert!(note_saying(&harness.records, "producer readiness refused")
        .contains("rewound to 0 behind durable cursor 9"));
}
