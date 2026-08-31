//! Durable strategy checkpoints sit in the WAL, but their bytes belong to the
//! strategy that wrote them.

use super::*;

fn checkpoint() -> StrategyCheckpoint {
    StrategyCheckpoint {
        schema_version: 7,
        decision_fingerprint: "decision-v7".to_string(),
        payload: br#"{"phase":"consumed"}"#.to_vec(),
    }
}

struct StrictBootCheckpoint;

impl Strategy for StrictBootCheckpoint {
    fn name(&self) -> &str {
        "strict-boot"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
        Some(StrategyCheckpointIdentity {
            schema_version: 7,
            decision_fingerprint: "decision-v7".into(),
        })
    }

    fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
        Some(checkpoint())
    }

    fn validate_checkpoint(&self, candidate: &StrategyCheckpoint) -> Result<(), String> {
        if candidate == &checkpoint() {
            Ok(())
        } else {
            Err("checkpoint bytes are not canonical strict-boot state".into())
        }
    }
}

#[tokio::test]
async fn fresh_boot_barriers_canonical_initial_state_before_reading_the_venue() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (venue, _) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _) = MockRisk::with(allow_all());
    Engine::boot(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(StrictBootCheckpoint)],
        &[],
    )
    .await
    .expect("fresh boot seeds canonical state");

    let saved = at(&tape, &Step::Append("strategy_global_checkpoint".into()))
        .expect("initial checkpoint appended");
    let barrier = after(&tape, &Step::Barrier, saved + 1).expect("initial checkpoint barrier");
    let rules = at(&tape, &Step::ReadRules).expect("venue rules read");
    assert!(saved < barrier && barrier < rules);
    assert!(records.lock().unwrap().iter().any(|record| matches!(
        record,
        WalRecord::StrategyGlobalCheckpoint {
            strategy: StrategyId(0),
            checkpoint: candidate,
            provenance: None,
            ..
        } if candidate == &checkpoint()
    )));
}

#[tokio::test]
async fn bad_or_missing_native_state_fails_before_any_venue_read() {
    for replayed in [
        vec![WalRecord::Names {
            strategies: vec!["strict-boot".into()],
            symbols: vec!["BTCUSDT".into()],
        }],
        vec![
            WalRecord::Names {
                strategies: vec!["strict-boot".into()],
                symbols: vec!["BTCUSDT".into()],
            },
            WalRecord::StrategyGlobalCheckpoint {
                wall_ts_ms: recent_replay_ms(),
                strategy: StrategyId(0),
                checkpoint: StrategyCheckpoint {
                    payload: b"arbitrary".to_vec(),
                    ..checkpoint()
                },
                provenance: None,
            },
        ],
    ] {
        let tape = tape();
        let (wal, _) = MockWal::new(tape.clone());
        let (venue, _) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
        let (risk, _) = MockRisk::with(allow_all());
        let result = Engine::boot(
            &settings(),
            "0",
            wal,
            risk,
            venue,
            vec![Box::new(StrictBootCheckpoint)],
            &replayed,
        )
        .await;
        assert!(result.is_err());
        assert!(!tape
            .lock()
            .unwrap()
            .iter()
            .any(|step| matches!(step, Step::ReadRules | Step::ReadAccount)));
    }
}

struct CheckpointThenBuyer {
    fired: bool,
}

impl Strategy for CheckpointThenBuyer {
    fn name(&self) -> &str {
        "checkpoint-buyer"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".to_string(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event else {
            return;
        };
        if self.fired {
            return;
        }
        self.fired = true;
        ctx.emit(engine_types::Action::SetStrategyCheckpoint {
            // The context must replace this untrusted owner with its own id.
            strategy: StrategyId(91),
            symbol: *symbol,
            checkpoint: checkpoint(),
        });
        ctx.place(Intent {
            strategy: StrategyId(91),
            symbol: *symbol,
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec {
                trigger_px: quote.bid_px * 0.99,
            }),
            reduce_only: false,
            tag: "checkpointed-entry".to_string(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }
}

#[tokio::test]
async fn checkpoint_owner_and_barrier_precede_the_dependent_entry() {
    let strategy = CheckpointThenBuyer { fired: false };
    let (mut engine, h) = build(allow_all(), vec![Box::new(strategy)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .expect("checkpointed entry runs");

    let start = after_boot(&h.tape);
    let checkpoint_at = after(
        &h.tape,
        &Step::Append("strategy_checkpoint".to_string()),
        start,
    )
    .expect("checkpoint appended");
    let barrier_at =
        after(&h.tape, &Step::Barrier, checkpoint_at + 1).expect("checkpoint covered by a barrier");
    let intent_at = after(
        &h.tape,
        &Step::Append("intent".to_string()),
        checkpoint_at + 1,
    )
    .expect("dependent intent appended");
    assert!(
        checkpoint_at < barrier_at && barrier_at < intent_at,
        "the one-shot checkpoint must be durable before its entry is admitted"
    );

    let saved = h
        .records
        .lock()
        .unwrap()
        .iter()
        .find_map(|record| match record {
            WalRecord::StrategyCheckpoint {
                strategy,
                symbol,
                checkpoint,
                ..
            } => Some((*strategy, *symbol, checkpoint.clone())),
            _ => None,
        })
        .expect("checkpoint record");
    assert_eq!(saved, (StrategyId(0), SymbolId(0), checkpoint()));
}

#[tokio::test]
async fn failure_between_checkpoint_and_intent_is_deliberately_fail_closed() {
    let tape = tape();
    let (mut wal, records) = MockWal::new(tape.clone());
    wal.fail_on = Some("intent".to_string());
    let (venue, sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    let (risk, _) = MockRisk::with(allow_all());
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        risk,
        venue,
        vec![Box::new(CheckpointThenBuyer { fired: false })],
        &[],
    )
    .await
    .expect("boot");
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let result = engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await;
    assert!(
        result.is_err(),
        "the failed intent append must stop the run"
    );
    assert!(
        sends.lock().unwrap().is_empty(),
        "nothing reached the venue"
    );
    assert!(records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(record, WalRecord::StrategyCheckpoint { .. })));
    assert!(
        !records
            .lock()
            .unwrap()
            .iter()
            .any(|record| matches!(record, WalRecord::Intent { .. })),
        "the durable consume can exist without an entry intent"
    );
    let checkpoint_at =
        at(&tape, &Step::Append("strategy_checkpoint".to_string())).expect("checkpoint appended");
    assert!(
        after(&tape, &Step::Barrier, checkpoint_at + 1).is_some(),
        "the fail-closed consume is durable"
    );
}

struct CheckpointReader {
    seen: Rc<RefCell<Vec<Option<StrategyCheckpoint>>>>,
}

impl Strategy for CheckpointReader {
    fn name(&self) -> &str {
        "checkpoint-reader"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".to_string(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            self.seen
                .lock()
                .unwrap()
                .push(ctx.strategy_checkpoint(*symbol).cloned());
        }
    }
}

#[tokio::test]
async fn a_booted_strategy_reads_its_latest_checkpoint() {
    let seen = Rc::new(RefCell::new(Vec::new()));
    let replayed = replay_with_history_boundary(&[
        WalRecord::Names {
            strategies: vec!["checkpoint-reader".to_string()],
            symbols: vec!["BTCUSDT".to_string()],
        },
        WalRecord::StrategyCheckpoint {
            wall_ts_ms: recent_replay_ms(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            checkpoint: checkpoint(),
        },
    ]);
    let (mut engine, _) = build(
        allow_all(),
        vec![Box::new(CheckpointReader { seen: seen.clone() })],
        &["BTCUSDT"],
        &replayed,
    )
    .await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .expect("replayed checkpoint is readable");
    assert_eq!(&*seen.lock().unwrap(), &[Some(checkpoint())]);
}

struct BootGlobalCheckpointReader {
    seen: Rc<RefCell<Vec<Option<StrategyCheckpoint>>>>,
}

impl Strategy for BootGlobalCheckpointReader {
    fn name(&self) -> &str {
        "boot-global-checkpoint-reader"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
        Some(StrategyCheckpointIdentity {
            schema_version: 7,
            decision_fingerprint: "decision-v7".into(),
        })
    }

    fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
        Some(checkpoint())
    }

    fn validate_checkpoint(&self, candidate: &StrategyCheckpoint) -> Result<(), String> {
        (candidate == &checkpoint())
            .then_some(())
            .ok_or_else(|| "unexpected boot checkpoint".to_owned())
    }

    fn on_boot(&mut self, ctx: &mut dyn StrategyCtx) {
        self.seen
            .lock()
            .unwrap()
            .push(ctx.strategy_global_checkpoint().cloned());
    }
}

#[tokio::test]
async fn restored_strategy_is_woken_with_its_global_checkpoint_before_market_news() {
    let seen = Rc::new(RefCell::new(Vec::new()));
    let replayed = replay_with_history_boundary(&[
        WalRecord::Names {
            strategies: vec!["boot-global-checkpoint-reader".into()],
            symbols: vec!["BTCUSDT".into()],
        },
        WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: recent_replay_ms(),
            strategy: StrategyId(0),
            checkpoint: checkpoint(),
            provenance: None,
        },
    ]);

    let (_engine, _) = build(
        allow_all(),
        vec![Box::new(BootGlobalCheckpointReader { seen: seen.clone() })],
        &["BTCUSDT"],
        &replayed,
    )
    .await;

    assert_eq!(&*seen.lock().unwrap(), &[Some(checkpoint())]);
}

struct GlobalCheckpointThenBuyer {
    fired: bool,
}

impl Strategy for GlobalCheckpointThenBuyer {
    fn name(&self) -> &str {
        "global-checkpoint-buyer"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, quote } = event else {
            return;
        };
        if self.fired {
            return;
        }
        self.fired = true;
        ctx.emit(engine_types::Action::SetStrategyGlobalCheckpoint {
            strategy: StrategyId(77),
            checkpoint: checkpoint(),
        });
        ctx.place(Intent {
            strategy: StrategyId(77),
            symbol: *symbol,
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec {
                trigger_px: quote.bid_px * 0.99,
            }),
            reduce_only: false,
            tag: "global-checkpointed-entry".into(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }
}

#[tokio::test]
async fn global_checkpoint_owner_and_barrier_precede_the_dependent_entry() {
    let (mut engine, h) = build(
        allow_all(),
        vec![Box::new(GlobalCheckpointThenBuyer { fired: false })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let start = after_boot(&h.tape);
    let saved = after(
        &h.tape,
        &Step::Append("strategy_global_checkpoint".into()),
        start,
    )
    .unwrap();
    let barrier = after(&h.tape, &Step::Barrier, saved + 1).unwrap();
    let intent = after(&h.tape, &Step::Append("intent".into()), saved + 1).unwrap();
    assert!(saved < barrier && barrier < intent);
    assert!(h.records.lock().unwrap().iter().any(|record| matches!(
        record,
        WalRecord::StrategyGlobalCheckpoint {
            strategy: StrategyId(0),
            checkpoint: saved,
            provenance: None,
            ..
        } if saved == &checkpoint()
    )));
    let WalRecord::SegmentBase {
        strategy_global_checkpoints,
        ..
    } = engine.rotation_base(recent_replay_ms())
    else {
        unreachable!()
    };
    assert_eq!(strategy_global_checkpoints.len(), 1);
    assert_eq!(strategy_global_checkpoints[0].strategy, StrategyId(0));
    assert_eq!(strategy_global_checkpoints[0].checkpoint, checkpoint());
}

struct GlobalCheckpointReader {
    seen: Rc<RefCell<Vec<Option<StrategyCheckpoint>>>>,
    symbol: &'static str,
    name: &'static str,
}

impl Strategy for GlobalCheckpointReader {
    fn name(&self) -> &str {
        self.name
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.into(),
            feed: Feed::Quote,
        }]
    }

    fn on_market(&mut self, _event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        self.seen
            .lock()
            .unwrap()
            .push(ctx.strategy_global_checkpoint().cloned());
    }
}

#[tokio::test]
async fn global_checkpoint_is_visible_only_to_its_owner_after_restart() {
    let owner = Rc::new(RefCell::new(Vec::new()));
    let other = Rc::new(RefCell::new(Vec::new()));
    let replayed = vec![
        WalRecord::Names {
            strategies: vec!["owner".into(), "other".into()],
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        },
        WalRecord::StrategyGlobalCheckpoint {
            wall_ts_ms: recent_replay_ms(),
            strategy: StrategyId(0),
            checkpoint: checkpoint(),
            provenance: None,
        },
    ];
    let (mut engine, _) = build(
        allow_all(),
        vec![
            Box::new(GlobalCheckpointReader {
                seen: owner.clone(),
                symbol: "BTCUSDT",
                name: "owner",
            }),
            Box::new(GlobalCheckpointReader {
                seen: other.clone(),
                symbol: "ETHUSDT",
                name: "other",
            }),
        ],
        &["BTCUSDT", "ETHUSDT"],
        &replayed,
    )
    .await;
    let feed = vec![
        MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote::default(),
        },
        MarketEvent::Quote {
            symbol: SymbolId(1),
            quote: Quote::default(),
        },
    ];
    engine
        .run(
            &mut ScriptFeed {
                events: feed.into(),
                close_at_end: true,
                admitted: Rc::new(RefCell::new(Vec::new())),
                known: 2,
                admits_wrongly: false,
            },
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(&*owner.lock().unwrap(), &[Some(checkpoint())]);
    assert_eq!(&*other.lock().unwrap(), &[None]);
}
