//! External observations are admitted, journaled, routed, and retained as one
//! ordered input stream.

use super::*;
use engine_types::{Action, SignalObservation, SIGNAL_OBSERVATION_SCHEMA_VERSION};

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

#[tokio::test]
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
                known: 3,
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

#[tokio::test]
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
    let (mut engine, h) = build(allow_all(), strategies, &["BTCUSDT", "ETHUSDT"], &[]).await;
    let admitted = Rc::new(RefCell::new(Vec::new()));
    let mut market = ScriptFeed {
        events: VecDeque::new(),
        close_at_end: false,
        admitted: admitted.clone(),
        known: 2,
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

#[tokio::test]
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
