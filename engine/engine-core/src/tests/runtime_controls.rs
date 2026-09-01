use super::*;
use engine_types::{
    Action, RuntimeControlCommand, RuntimeControlError, RuntimeControlFeed, RuntimeControlRequest,
    STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
};

fn request(
    strategy: u16,
    name: &str,
    request_id: &str,
    command: RuntimeControlCommand,
) -> RuntimeControlRequest {
    let mut request = RuntimeControlRequest {
        schema_version: STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
        strategy: StrategyId(strategy),
        strategy_name: name.into(),
        request_id: request_id.into(),
        command,
        content_sha256: String::new(),
    };
    request.content_sha256 = crate::controls::content_sha256(&request);
    request
}

struct OneControl(Option<RuntimeControlRequest>);

impl RuntimeControlFeed for OneControl {
    async fn next_request(&mut self) -> Result<RuntimeControlRequest, RuntimeControlError> {
        match self.0.take() {
            Some(request) => Ok(request),
            None => std::future::pending().await,
        }
    }
}

struct QueueControls {
    queue: VecDeque<RuntimeControlRequest>,
    last: Option<String>,
    rejected: Rc<RefCell<Vec<String>>>,
}

impl RuntimeControlFeed for QueueControls {
    async fn next_request(&mut self) -> Result<RuntimeControlRequest, RuntimeControlError> {
        match self.queue.pop_front() {
            Some(request) => {
                self.last = Some(request.request_id.clone());
                Ok(request)
            }
            None => std::future::pending().await,
        }
    }

    async fn reject_last(&mut self) -> Result<(), RuntimeControlError> {
        if let Some(id) = self.last.take() {
            self.rejected.lock().unwrap().push(id);
        }
        Ok(())
    }
}

struct GateProbe {
    name: &'static str,
    symbol: &'static str,
    seen: Rc<RefCell<Vec<(String, bool)>>>,
}

impl Strategy for GateProbe {
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
            .push((self.name.into(), ctx.entries_enabled(true)));
    }
}

fn two_quotes() -> ScriptFeed {
    let quote = |symbol| MarketEvent::Quote {
        symbol,
        quote: Quote {
            bid_px: 100.0,
            bid_qty: 10.0,
            ask_px: 100.5,
            ask_qty: 10.0,
            venue_ts_ms: 1,
            recv_ns: clock::now_ns(),
            seq: 1,
        },
    };
    ScriptFeed {
        events: VecDeque::from([quote(SymbolId(0)), quote(SymbolId(1))]),
        close_at_end: true,
        admitted: Rc::new(RefCell::new(Vec::new())),
        known: 2,
        admits_wrongly: false,
    }
}

#[tokio::test]
async fn runtime_entry_override_is_owner_scoped_and_survives_rotation() {
    let pause = request(
        1,
        "right",
        "pause-right-1",
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
    );
    let replayed = vec![WalRecord::RuntimeControlAccepted {
        wall_ts_ms: recent_replay_ms(),
        request: pause.clone(),
    }];
    let seen = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, _) = build(
        allow_all(),
        vec![
            Box::new(GateProbe {
                name: "left",
                symbol: "BTCUSDT",
                seen: seen.clone(),
            }),
            Box::new(GateProbe {
                name: "right",
                symbol: "ETHUSDT",
                seen: seen.clone(),
            }),
        ],
        &["BTCUSDT", "ETHUSDT"],
        &replayed,
    )
    .await;
    engine
        .run(
            &mut two_quotes(),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(
        &*seen.lock().unwrap(),
        &[("left".into(), true), ("right".into(), false)]
    );

    let base = engine.rotation_base(recent_replay_ms());
    assert!(matches!(
        &base,
        WalRecord::SegmentBase { runtime_control_requests, .. }
            if runtime_control_requests == &vec![pause]
    ));
    let restarted_seen = Rc::new(RefCell::new(Vec::new()));
    let (mut restarted, _) = build(
        allow_all(),
        vec![
            Box::new(GateProbe {
                name: "left",
                symbol: "BTCUSDT",
                seen: restarted_seen.clone(),
            }),
            Box::new(GateProbe {
                name: "right",
                symbol: "ETHUSDT",
                seen: restarted_seen.clone(),
            }),
        ],
        &["BTCUSDT", "ETHUSDT"],
        &[base],
    )
    .await;
    restarted
        .run(
            &mut two_quotes(),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(
        &*restarted_seen.lock().unwrap(),
        &[("left".into(), true), ("right".into(), false)]
    );
}

struct PermissionActor {
    seen: Rc<RefCell<Vec<bool>>>,
    done: Option<tokio::sync::oneshot::Sender<()>>,
}

impl Strategy for PermissionActor {
    fn name(&self) -> &str {
        "directional"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_entry_permission(
        &mut self,
        _request_id: &str,
        _entries_enabled: bool,
        ctx: &mut dyn StrategyCtx,
    ) {
        self.seen.lock().unwrap().push(ctx.entries_enabled(true));
        for reduce_only in [false, true] {
            ctx.place(Intent {
                strategy: StrategyId(99),
                symbol: SymbolId(0),
                side: Side::Sell,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only,
                tag: if reduce_only { "exit" } else { "grow" }.into(),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
        if let Some(done) = self.done.take() {
            let _ = done.send(());
        }
    }
}

#[tokio::test]
async fn live_pause_is_barriered_before_apply_and_core_still_allows_exit() {
    let seen = Rc::new(RefCell::new(Vec::new()));
    let (done, stopped) = tokio::sync::oneshot::channel();
    let (mut engine, harness) = build(
        allow_all(),
        vec![Box::new(PermissionActor {
            seen: seen.clone(),
            done: Some(done),
        })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let mut controls = OneControl(Some(request(
        0,
        "directional",
        "pause-1",
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
    )));
    engine
        .run_with_inputs(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut crate::signals::NoSignals,
            &mut controls,
            async {
                let _ = stopped.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(&*seen.lock().unwrap(), &[false]);
    assert!(harness
        .sends
        .lock()
        .unwrap()
        .iter()
        .all(|row| row.reduce_only));
    let start = after_boot(&harness.tape);
    let accepted = after(
        &harness.tape,
        &Step::Append("runtime_control_accepted".into()),
        start,
    )
    .unwrap();
    let barrier = after(&harness.tape, &Step::Barrier, accepted + 1).unwrap();
    let intent = after(&harness.tape, &Step::Append("intent".into()), accepted + 1).unwrap();
    assert!(accepted < barrier && barrier < intent);
    assert!(harness
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(
            record,
            WalRecord::Verdict {
                verdict: RiskVerdict::Deny {
                    reason: DenyReason::UnknownState { detail }
                },
                ..
            } if detail.contains("runtime entry permission")
        )));
}

#[tokio::test]
async fn stale_control_request_is_rejected_without_stopping_the_engine() {
    let seen = Rc::new(RefCell::new(Vec::new()));
    let (done, stopped) = tokio::sync::oneshot::channel();
    let (mut engine, harness) = build(
        allow_all(),
        vec![Box::new(PermissionActor {
            seen: seen.clone(),
            done: Some(done),
        })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    // Names a strategy outside the configured table: a request from another
    // generation, or one submitted with intentionally stale bytes.
    let stale = request(
        7,
        "ghost",
        "stale-1",
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
    );
    let pause = request(
        0,
        "directional",
        "pause-1",
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
    );
    let rejected = Rc::new(RefCell::new(Vec::new()));
    let mut controls = QueueControls {
        queue: VecDeque::from([stale, pause]),
        last: None,
        rejected: rejected.clone(),
    };
    engine
        .run_with_inputs(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut crate::signals::NoSignals,
            &mut controls,
            async {
                let _ = stopped.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(&*rejected.lock().unwrap(), &["stale-1"]);
    assert_eq!(&*seen.lock().unwrap(), &[false]);
    let accepted: Vec<String> = harness
        .records
        .lock()
        .unwrap()
        .iter()
        .filter_map(|record| match record {
            WalRecord::RuntimeControlAccepted { request, .. } => Some(request.request_id.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(accepted, vec!["pause-1".to_string()]);
}

#[tokio::test]
async fn durable_stale_request_cannot_wedge_engine_boot() {
    let spool = temp_path("engine-stale-control-spool");
    std::fs::create_dir(spool.path()).unwrap();
    let stale = request(
        7,
        "ghost",
        "stale-1",
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
    );
    let stale_path = crate::controls::submit(spool.path(), &stale).unwrap();
    let seen = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, _harness) = build(
        allow_all(),
        vec![Box::new(GateProbe {
            name: "directional",
            symbol: "BTCUSDT",
            seen: seen.clone(),
        })],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let mut controls = crate::controls::SpoolRuntimeControlFeed::new(spool.path())
        .with_poll_interval(Duration::from_millis(1));
    let marker = spool
        .path()
        .join(format!("{}.json.rejected", stale.content_sha256));
    let shutdown_marker = marker.clone();
    engine
        .run_with_inputs(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut crate::signals::NoSignals,
            &mut controls,
            async move {
                while !shutdown_marker.exists() {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
            },
        )
        .await
        .unwrap();
    assert!(!stale_path.exists());
    assert!(marker.exists());
    std::fs::remove_file(marker).unwrap();
    std::fs::remove_file(spool.path().join(".submit.lock")).unwrap();
    std::fs::remove_dir(spool.path()).unwrap();
}

struct FlattenActor {
    seen: Rc<RefCell<Vec<String>>>,
    acknowledge: bool,
}

impl Strategy for FlattenActor {
    fn name(&self) -> &str {
        "directional"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_flatten_directional(&mut self, request_id: &str, ctx: &mut dyn StrategyCtx) {
        self.seen.lock().unwrap().push(request_id.into());
        if self.acknowledge {
            ctx.emit(Action::ConsumeRuntimeControl {
                strategy: StrategyId(99),
                request_id: request_id.into(),
            });
        }
    }
}

#[tokio::test]
async fn flatten_replays_after_crash_and_rotation_until_strategy_acknowledges() {
    let pause = request(
        0,
        "directional",
        "pause-1",
        RuntimeControlCommand::SetEntriesEnabled {
            entries_enabled: false,
        },
    );
    let flatten = request(
        0,
        "directional",
        "flatten-1",
        RuntimeControlCommand::FlattenDirectional,
    );
    let replayed = vec![
        WalRecord::RuntimeControlAccepted {
            wall_ts_ms: recent_replay_ms(),
            request: pause,
        },
        WalRecord::RuntimeControlAccepted {
            wall_ts_ms: recent_replay_ms(),
            request: flatten,
        },
    ];
    let first_seen = Rc::new(RefCell::new(Vec::new()));
    let (first, _) = build(
        allow_all(),
        vec![Box::new(FlattenActor {
            seen: first_seen.clone(),
            acknowledge: false,
        })],
        &["BTCUSDT"],
        &replayed,
    )
    .await;
    assert_eq!(&*first_seen.lock().unwrap(), &["flatten-1"]);
    let base = first.rotation_base(recent_replay_ms());

    let second_seen = Rc::new(RefCell::new(Vec::new()));
    let (mut second, harness) = build(
        allow_all(),
        vec![Box::new(FlattenActor {
            seen: second_seen.clone(),
            acknowledge: true,
        })],
        &["BTCUSDT"],
        &[base],
    )
    .await;
    second
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(&*second_seen.lock().unwrap(), &["flatten-1"]);
    assert!(harness
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(
            record,
            WalRecord::RuntimeControlConsumed { strategy: StrategyId(0), request_id, .. }
                if request_id == "flatten-1"
        )));
    assert!(matches!(
        second.rotation_base(recent_replay_ms()),
        WalRecord::SegmentBase { runtime_control_consumed, .. }
            if runtime_control_consumed == vec![(StrategyId(0), "flatten-1".into())]
    ));
}
