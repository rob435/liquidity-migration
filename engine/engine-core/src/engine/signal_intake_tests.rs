use super::*;
use engine_types::{Action, SignalObservation, Strategy, StrategyCtx, Subscription};

struct Consumer(&'static str, bool);
impl Strategy for Consumer {
    fn name(&self) -> &str {
        self.0
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
    fn runtime_state(
        &self,
    ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String> {
        Ok(Some(engine_types::strategy_process::StrategyRuntimeState {
            schema_version: 1,
            kind: "test".into(),
            configuration_sha256: "a".repeat(64),
            payload: Vec::new(),
        }))
    }
    fn on_signal(&mut self, row: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        if self.1 {
            ctx.emit(Action::RejectSignalObservation {
                strategy: row.destination,
                source: row.source.clone(),
                sequence: row.sequence,
                observation_id: row.observation_id.clone(),
                reason: "invalid payload".into(),
            });
        }
    }
}

fn source_row(source: &str, sequence: u64, destination: u16) -> SignalObservation {
    let mut row = SignalObservation {
        schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "test".into(),
        destination: StrategyId(destination),
        source: source.into(),
        sequence,
        observation_id: format!("{source}-{sequence}"),
        kind: "test".into(),
        observed_wall_ts_ms: 1,
        available_wall_ts_ms: 2,
        subscriptions: Vec::new(),
        payload: vec![1],
        content_sha256: String::new(),
    };
    row.content_sha256 = crate::signals::content_sha256(&row);
    row
}

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
        blocked: &[StrategyId],
    ) -> Result<(), engine_types::SignalError> {
        self.gaps = gaps.to_vec();
        self.blocked_destinations = blocked.to_vec();
        Ok(())
    }
    fn acknowledge_last(&mut self) -> Result<(), engine_types::SignalError> {
        Ok(())
    }
    fn defer_last(&mut self, row: SignalObservation) -> Result<(), engine_types::SignalError> {
        self.rows.push_back(row);
        Ok(())
    }
    async fn next_observation(&mut self) -> Result<SignalObservation, engine_types::SignalError> {
        let _ = self.done.take();
        self.rows
            .pop_front()
            .ok_or(engine_types::SignalError::Closed)
    }
}

struct LifecycleSignals {
    inner: FinishedSignals,
    requests: Vec<Vec<engine_types::SignalProducerLifecycle>>,
}

impl engine_types::SignalFeed for LifecycleSignals {
    fn request_lifecycle(
        &mut self,
        producers: Vec<engine_types::SignalProducerLifecycle>,
        _legacy: Vec<engine_types::SignalSourceFrontier>,
    ) -> Result<(), engine_types::SignalError> {
        self.requests.push(producers);
        Ok(())
    }
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
        self.inner.next_observation().await
    }
}

#[tokio::test]
async fn lifecycle_close_barrier_failure_retains_epoch_and_never_grants_a_successor() {
    use engine_types::{SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier};
    let (mut engine, records) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(Consumer("long", true)),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let generation = "a".repeat(32);
    let discovery = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation: generation.clone(),
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: format!("native.g{generation}.long"),
                destination: StrategyId(0),
                published_through: 0,
            },
            SignalSourceFrontier {
                source: format!("native.g{generation}.carry"),
                destination: StrategyId(1),
                published_through: 0,
            },
        ],
    };
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "discover".into(),
                producer: discovery,
            },
            &mut feed,
        )
        .unwrap();
    assert_eq!(feed.requests.len(), 1);
    let active = engine
        .signals
        .producers()
        .next()
        .unwrap()
        .active
        .clone()
        .unwrap();
    let input = source_row(&active.sources[0].source, 1, 0);
    engine
        .queue_signal_observation(input.clone(), &mut feed)
        .unwrap();
    engine.accept_pending_signals(&mut feed).unwrap();
    assert_eq!(engine.signals.observations().count(), 1);
    let mut sealed = SignalProducerReport {
        producer: "native".into(),
        epoch: Some(1),
        generation,
        sealed: true,
        sources: active.sources,
    };
    sealed.sources[0].published_through = 1;
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "seal".into(),
                producer: sealed,
            },
            &mut feed,
        )
        .unwrap();
    assert_eq!(
        feed.requests.len(),
        1,
        "acceptance alone cannot grant the next generation"
    );
    engine.drain(clock::now_ns()).await.unwrap();
    assert_eq!(engine.signals.observations().count(), 0);
    engine.wal.fail_barrier_after = Some("signal_producer_lifecycle");
    assert!(engine
        .advance_signal_lifecycles(&mut feed)
        .unwrap_err()
        .to_string()
        .contains("test barrier failure"));
    assert_eq!(
        feed.requests.len(),
        1,
        "failed close barrier cannot publish a grant"
    );
    assert_eq!(
        engine.signals.producers().next().unwrap().retired_through,
        0
    );
    assert_eq!(engine.signals.cursors().count(), 1);
    let records = records.lock().unwrap().clone();
    let before_close =
        crate::signal_state::SignalState::replay(&records[..records.len() - 1], 2).unwrap();
    assert_eq!(before_close.producers().next().unwrap().retired_through, 0);
    assert_eq!(before_close.observations().count(), 0);
    let after_close = crate::signal_state::SignalState::replay(&records, 2).unwrap();
    assert_eq!(after_close.producers().next().unwrap().retired_through, 1);
    assert_eq!(
        after_close.classify(&input).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
    engine.wal.fail_barrier_after = None;
    engine.advance_signal_lifecycles(&mut feed).unwrap();
    assert_eq!(feed.requests.len(), 2);
    let rotated =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 2)
            .unwrap();
    assert_eq!(
        rotated.classify(&input).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
    assert_eq!(rotated.cursors().count(), 0);
    let directory = crate::testpath::temp_path("lifecycle-wal-replay");
    std::fs::create_dir_all(directory.path()).unwrap();
    let family = directory.path().join("engine.wal");
    {
        let (mut disk, _) = engine_wal::WalWriter::open(&family).unwrap();
        for record in &records {
            disk.append(record).unwrap();
        }
        disk.barrier().unwrap();
        disk.rotate(&engine.rotation_base(clock::wall_ms()))
            .unwrap();
    }
    let (writer, latest) = engine_wal::open_current(&family).unwrap();
    drop(writer);
    assert_eq!(
        latest.len(),
        1,
        "current boot selects the completed v4 restatement"
    );
    let latest = latest.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
    let disk_state = crate::signal_state::SignalState::replay(&latest, 2).unwrap();
    assert_eq!(
        disk_state.classify(&input).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
    assert_eq!(disk_state.producers().next().unwrap().retired_through, 1);
    let (chain, torn) = engine_wal::replay_chain(&family).unwrap();
    assert!(!torn);
    let chain = chain.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
    assert_eq!(
        crate::signal_state::SignalState::replay(&chain, 2)
            .unwrap()
            .producers()
            .collect::<Vec<_>>(),
        disk_state.producers().collect::<Vec<_>>()
    );
    let segment = directory.path().join("engine.wal.000002");
    let bytes = std::fs::read(&segment).unwrap();
    std::fs::write(&segment, &bytes[..bytes.len() - 1]).unwrap();
    let (writer, fallback) = engine_wal::open_current(&family).unwrap();
    drop(writer);
    let fallback = fallback.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
    assert_eq!(
        crate::signal_state::SignalState::replay(&fallback, 2)
            .unwrap()
            .producers()
            .collect::<Vec<_>>(),
        disk_state.producers().collect::<Vec<_>>(),
        "interrupted rotation recovers the durable predecessor with the same retirement floor"
    );
    std::fs::remove_dir_all(directory.path()).unwrap();
}

#[tokio::test]
async fn accepted_signal_retries_a_full_callback_inbox_once_and_replays_after_restart() {
    use crate::strategy_process::{
        host::{CallbackExecution, CallbackHost},
        state::CallbackState,
    };
    use engine_types::strategy_process::{
        CallbackEvent, StrategyProcessState, MAX_PROCESS_PROPOSAL_BYTES,
    };
    let (mut engine, records) =
        crate::tests::lifecycle_test_fixture(vec![Box::new(Consumer("long", false))]).await;
    engine.host.callbacks = CallbackHost::new(
        CallbackExecution::Isolated {
            executable: "/unused-test-worker".into(),
        },
        &engine.host.strategies,
        &[],
    )
    .unwrap();
    assert!(engine.feed_one_strategy(StrategyId(0), &EngineEvent::Boot, 1));
    let mut filler = engine.host.callbacks.unwritten.pop_front().unwrap();
    // Construct an actual full serialized inbox without launching its worker.
    engine.host.callbacks = CallbackHost::new(
        CallbackExecution::Isolated {
            executable: "/unused-test-worker".into(),
        },
        &engine.host.strategies,
        &[],
    )
    .unwrap();
    let mut large = source_row("filler", 1, 0);
    large.payload.clear();
    filler.event = CallbackEvent::Signal {
        observation: large.clone(),
    };
    filler.preparation = engine_types::strategy_process::CallbackPreparation::Prepared {
        snapshot: engine_types::strategy_process::CallbackSnapshot {
            strategy: StrategyId(0),
            now_ns: 1,
            wall_ms: 1,
            entries_enabled: true,
            account: engine_types::StrategyAccountSummary {
                equity_usdt: 0.0,
                available_margin_usdt: 0.0,
                observed_ns: 0,
            },
            symbols: Vec::new(),
            orders: Vec::new(),
            global_checkpoint: None,
            strategy_names: vec!["long".into()],
            strategy_events: Vec::new(),
        },
    };
    let overhead = CallbackState::size(&filler).unwrap();
    large.payload = vec![255; (MAX_PROCESS_PROPOSAL_BYTES - overhead) / 4];
    filler.event = CallbackEvent::Signal { observation: large };
    assert!(MAX_PROCESS_PROPOSAL_BYTES - CallbackState::size(&filler).unwrap() < 5);
    engine.host.callbacks.state.accept(filler.clone()).unwrap();
    let mut feed = FinishedSignals {
        rows: VecDeque::new(),
        done: None,
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    let input = source_row("durable", 1, 0);
    engine
        .queue_signal_observation(input.clone(), &mut feed)
        .unwrap();
    engine.accept_pending_signals(&mut feed).unwrap();
    assert_eq!(
        engine.signals.undelivered().count(),
        1,
        "full inbox cannot mark delivery complete"
    );
    assert!(engine.host.callbacks.unwritten.is_empty());
    assert_eq!(engine.signals.cursors().next().unwrap().sequence, 1);
    let runtime = engine.host.strategies[0].runtime_state().unwrap().unwrap();
    engine
        .host
        .callbacks
        .state
        .commit(
            filler.callback_id,
            StrategyProcessState {
                strategy: StrategyId(0),
                last_callback_id: filler.callback_id,
                retained_signal_subscriptions: None,
                runtime,
                timers: Vec::new(),
            },
        )
        .unwrap();
    engine.deliver_pending_signal_callbacks();
    assert_eq!(engine.signals.undelivered().count(), 0);
    assert_eq!(engine.host.callbacks.unwritten.len(), 1);
    assert_eq!(
        engine.host.callbacks.unwritten.front().unwrap().event,
        CallbackEvent::Signal {
            observation: input.clone()
        }
    );
    engine.deliver_pending_signal_callbacks();
    assert_eq!(
        engine.host.callbacks.unwritten.len(),
        1,
        "successful admission is not repeatedly offered on every turn"
    );
    let queued = engine.host.callbacks.unwritten.pop_front().unwrap();
    let mut durable = records.lock().unwrap().clone();
    durable.push(WalRecord::StrategyCallbackQueued { input: queued });
    engine.signals = crate::signal_state::SignalState::replay(&durable, 1).unwrap();
    engine.host.callbacks = CallbackHost::new(
        CallbackExecution::Isolated {
            executable: "/unused-test-worker".into(),
        },
        &engine.host.strategies,
        &durable,
    )
    .unwrap();
    engine.deliver_pending_signal_callbacks();
    assert_eq!(engine.signals.undelivered().count(), 0);
    assert!(
        engine.host.callbacks.unwritten.is_empty(),
        "replay must join the existing durable callback"
    );
    assert_eq!(engine.host.callbacks.state.inputs.len(), 1);
    assert_eq!(
        engine.signals.observations().count(),
        1,
        "callback admission does not consume the input"
    );
}

#[tokio::test]
async fn lifecycle_seal_cannot_omit_an_observation_waiting_for_symbol_admission() {
    use engine_types::{SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier};
    let (mut engine, _) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(Consumer("long", true)),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let generation = "a".repeat(32);
    let discovery = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation: generation.clone(),
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: format!("native.g{generation}.long"),
                destination: StrategyId(0),
                published_through: 0,
            },
            SignalSourceFrontier {
                source: format!("native.g{generation}.carry"),
                destination: StrategyId(1),
                published_through: 0,
            },
        ],
    };
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "discover".into(),
                producer: discovery,
            },
            &mut feed,
        )
        .unwrap();
    let active = engine
        .signals
        .producers()
        .next()
        .unwrap()
        .active
        .clone()
        .unwrap();
    engine
        .queue_signal_observation(source_row(&active.sources[0].source, 1, 0), &mut feed)
        .unwrap();
    assert_eq!(engine.pending_signal_deliveries.len(), 1);
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "seal".into(),
                producer: SignalProducerReport {
                    producer: "native".into(),
                    epoch: Some(1),
                    generation,
                    sealed: true,
                    sources: active.sources,
                },
            },
            &mut feed,
        )
        .unwrap();
    let producer = engine.signals.producers().next().unwrap();
    assert_eq!(
        producer.retired_through, 0,
        "a seal cannot turn an already observed tail into a retired duplicate"
    );
    assert!(!producer.active.as_ref().unwrap().sealed);
    assert_eq!(engine.pending_signal_deliveries.len(), 1);
    assert!(engine.signals.cursors().next().is_none());
}

#[tokio::test]
async fn signal_subscription_budget_suspends_only_its_destination_without_losing_the_row() {
    let (mut engine, records) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(Consumer("long", false)),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    for batch in 0..8 {
        let mut row = source_row("legacy.long", batch + 1, 0);
        row.subscriptions = (0..512)
            .map(|index| Subscription {
                symbol: format!("S{}USDT", batch * 512 + index),
                feed: engine_types::Feed::Quote,
            })
            .collect();
        row.content_sha256 = crate::signals::content_sha256(&row);
        engine.signals.accept(row.clone());
        engine.signals.consume(&row.source, row.sequence);
    }
    let before = engine.rotation_base(clock::wall_ms());
    let mut feed = FinishedSignals {
        rows: VecDeque::new(),
        done: None,
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    let mut row = source_row("legacy.long", 9, 0);
    row.subscriptions = vec![Subscription {
        symbol: "NEWUSDT".into(),
        feed: engine_types::Feed::Quote,
    }];
    row.content_sha256 = crate::signals::content_sha256(&row);
    assert!(
        engine
            .queue_signal_observation(row.clone(), &mut feed)
            .is_ok(),
        "subscription overload must preserve account and exit service"
    );
    assert_eq!(feed.rows.front(), Some(&row));
    assert_eq!(engine.signals.cursors().next().unwrap().sequence, 8);
    assert!(engine.signals.observations().next().is_none());
    assert!(
        engine.signals.gaps().next().is_none(),
        "a capacity suspension is not fabricated missing history"
    );
    assert!(engine.signals.blocked(StrategyId(0)));
    assert!(!engine.signals.blocked(StrategyId(1)));
    assert_eq!(
        engine.signals.suspensions().next().unwrap().reason,
        engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget {
            subscriptions: row.subscriptions.clone()
        }
    );
    let suspended = records.lock().unwrap().last().cloned().unwrap();
    let replayed = crate::signal_state::SignalState::replay(&[before, suspended], 2).unwrap();
    assert!(replayed.blocked(StrategyId(0)));
    let rotated =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 2)
            .unwrap();
    assert!(rotated.blocked(StrategyId(0)));
    assert_eq!(
        rotated
            .route_subscriptions("legacy.long", StrategyId(0))
            .len(),
        engine_types::MAX_DURABLE_SIGNAL_SUBSCRIPTIONS
    );
}

struct RouteConsumer(std::sync::Arc<std::sync::Mutex<Vec<Subscription>>>);
impl Strategy for RouteConsumer {
    fn name(&self) -> &str {
        "long"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
    fn retained_signal_subscriptions(&self) -> Option<Vec<Subscription>> {
        Some(self.0.lock().unwrap().clone())
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
#[derive(Default)]
struct RetiringMarket {
    retired: Vec<Subscription>,
}
impl MarketFeed for RetiringMarket {
    async fn next_event(&mut self) -> Result<MarketEvent, engine_types::FeedError> {
        std::future::pending().await
    }
    fn retire(&mut self, symbol: &str, feed: engine_types::Feed) -> bool {
        self.retired.push(Subscription {
            symbol: symbol.into(),
            feed,
        });
        true
    }
}

#[tokio::test]
async fn signal_route_release_waits_for_consumption_and_keeps_candidates_positions_and_orders() {
    use engine_types::{SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier};
    let needed = std::sync::Arc::new(std::sync::Mutex::new(vec![Subscription {
        symbol: "CANDIDATE".into(),
        feed: engine_types::Feed::Quote,
    }]));
    let (mut engine, _) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(RouteConsumer(needed.clone())),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let generation = "a".repeat(32);
    let report = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation: generation.clone(),
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: format!("native.g{generation}.long"),
                destination: StrategyId(0),
                published_through: 0,
            },
            SignalSourceFrontier {
                source: format!("native.g{generation}.carry"),
                destination: StrategyId(1),
                published_through: 0,
            },
        ],
    };
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "test".into(),
                producer: report,
            },
            &mut feed,
        )
        .unwrap();
    let source = engine
        .signals
        .producers()
        .next()
        .unwrap()
        .active
        .as_ref()
        .unwrap()
        .sources[0]
        .source
        .clone();
    let rows: Vec<_> = ["HELD", "WORKING", "CANDIDATE", "HISTORICAL"]
        .into_iter()
        .map(|symbol| Subscription {
            symbol: symbol.into(),
            feed: engine_types::Feed::Quote,
        })
        .collect();
    for row in &rows {
        let id = engine.books.market.add_symbol(&row.symbol);
        engine.routing.add(id, row.feed, StrategyId(0));
        engine.subscriptions.push(row.clone());
    }
    let held = engine.books.market.table.get("HELD").unwrap();
    let working = engine.books.market.table.get("WORKING").unwrap();
    engine
        .books
        .attribution
        .note(StrategyId(0), held, Side::Buy, 1.0);
    engine.books.orders.apply(&WalRecord::OrderSent {
        dispatch: None,
        request: OrderRequest {
            exact_terms: None,
            sleeve_effect: None,
            client_order_id: "pending".into(),
            strategy: StrategyId(0),
            symbol: working,
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Limit {
                px: 1.0,
                tif: TimeInForce::Gtc,
            },
            stop: None,
            reduce_only: false,
            close_position: false,
        },
        wire_ns: 1,
        arrival_mid: 1.0,
    });
    let mut input = source_row(&source, 1, 0);
    input.subscriptions = rows.clone();
    input.content_sha256 = crate::signals::content_sha256(&input);
    engine.signals.accept(input.clone());
    let mut market = RetiringMarket::default();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert!(
        market.retired.is_empty(),
        "an accepted unconsumed row still owns every requested route"
    );
    engine
        .signals
        .set_suspension(
            StrategyId(0),
            Some(
                engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget {
                    subscriptions: vec![rows[2].clone()],
                },
            ),
            2,
        )
        .unwrap();
    engine.signals.consume(&source, 1);
    engine.wal.fail_barrier_after = Some("signal_producer_lifecycle");
    assert!(engine.maintain_signal_routes(&mut market).is_err());
    assert!(
        market.retired.is_empty(),
        "no route removal before its WAL barrier"
    );
    assert_eq!(
        engine.signals.route_subscriptions(&source, StrategyId(0)),
        rows
    );
    engine.wal.fail_barrier_after = None;
    engine.maintain_signal_routes(&mut market).unwrap();
    assert!(
        engine.signals.suspensions().next().is_none(),
        "released ownership resumes a capacity-suspended destination"
    );
    assert_eq!(market.retired, vec![rows[3].clone()]);
    assert_eq!(
        engine.signals.route_subscriptions(&source, StrategyId(0)),
        &rows[..3]
    );
    assert!(engine
        .routing
        .quote_listeners(held)
        .contains(&StrategyId(0)));
    assert!(engine
        .routing
        .quote_listeners(working)
        .contains(&StrategyId(0)));
    let historical = engine.books.market.table.get("HISTORICAL").unwrap();
    assert!(engine.routing.quote_listeners(historical).is_empty());
    let base = engine.rotation_base(clock::wall_ms());
    let restored =
        crate::signal_state::SignalState::replay(std::slice::from_ref(&base), 2).unwrap();
    assert_eq!(
        restored.route_subscriptions(&source, StrategyId(0)),
        &rows[..3]
    );
    assert_eq!(crate::signals::active_subscriptions(&[base]), &rows[..3]);
    assert_eq!(
        restored.classify(&input).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
    needed.lock().unwrap().clear();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert_eq!(market.retired.last(), Some(&rows[2]));
    assert_eq!(
        engine.signals.route_subscriptions(&source, StrategyId(0)),
        &rows[..2]
    );
}

#[tokio::test]
async fn consumed_route_churn_crosses_the_old_lifetime_cap_without_releasing_live_ownership() {
    use engine_types::{
        Feed, InstrumentRule, SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier,
    };
    let needed = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let (mut engine, _) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(RouteConsumer(needed)),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let generation = "a".repeat(32);
    let report = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation: generation.clone(),
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: format!("native.g{generation}.long"),
                destination: StrategyId(0),
                published_through: 0,
            },
            SignalSourceFrontier {
                source: format!("native.g{generation}.carry"),
                destination: StrategyId(1),
                published_through: 0,
            },
        ],
    };
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "test".into(),
                producer: report,
            },
            &mut feed,
        )
        .unwrap();
    let source = engine
        .signals
        .producers()
        .next()
        .unwrap()
        .active
        .as_ref()
        .unwrap()
        .sources[0]
        .source
        .clone();
    let mut market = RetiringMarket::default();
    for batch in 0..10 {
        let mut row = source_row(&source, batch + 1, 0);
        row.subscriptions = (0..512)
            .map(|index| Subscription {
                symbol: format!("S{}USDT", batch * 512 + index),
                feed: Feed::Quote,
            })
            .collect();
        row.content_sha256 = crate::signals::content_sha256(&row);
        engine
            .queue_signal_observation(row.clone(), &mut feed)
            .unwrap();
        for sub in &row.subscriptions {
            let symbol = engine.books.market.add_symbol(&sub.symbol);
            engine.routing.add(symbol, sub.feed, StrategyId(0));
            engine.subscriptions.push(sub.clone());
        }
        engine.books.rules.resize(
            engine.books.market.table.len(),
            Some(InstrumentRule {
                tick_size: 0.01,
                qty_step: 0.001,
                min_qty: 0.001,
                min_notional: 1.0,
            }),
        );
        engine.wanted_symbols.clear();
        engine.accept_pending_signals(&mut feed).unwrap();
        engine.drain(clock::now_ns()).await.unwrap();
        assert_eq!(
            engine.signals.cursors().next().unwrap().sequence,
            batch + 1,
            "historical subscriptions cannot stall the next generation of candidates"
        );
        engine.maintain_signal_routes(&mut market).unwrap();
    }
    assert_eq!(market.retired.len(), 5120);
    assert!(engine.subscriptions.is_empty());
    assert!(engine
        .signals
        .route_subscriptions(&source, StrategyId(0))
        .is_empty());
    assert!(engine.signals.suspensions().next().is_none());
    assert_eq!(
        engine.books.market.table.get("S0USDT"),
        Some(SymbolId(0)),
        "retirement does not renumber accounting identities"
    );
    let restored =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 2)
            .unwrap();
    assert_eq!(restored.cursors().count(), 1);
    assert!(restored
        .producer_routes()
        .all(|route| route.subscriptions.is_empty()));
}

#[tokio::test]
async fn signal_route_budget_releases_obsolete_routes_while_a_replacement_row_waits_for_admission()
{
    use engine_types::{
        Feed, InstrumentRule, SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier,
    };
    let needed = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let (mut engine, _) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(RouteConsumer(needed)),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let generation = "a".repeat(32);
    let report = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation: generation.clone(),
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: format!("native.g{generation}.long"),
                destination: StrategyId(0),
                published_through: 0,
            },
            SignalSourceFrontier {
                source: format!("native.g{generation}.carry"),
                destination: StrategyId(1),
                published_through: 0,
            },
        ],
    };
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "test".into(),
                producer: report,
            },
            &mut feed,
        )
        .unwrap();
    let source = engine
        .signals
        .producers()
        .next()
        .unwrap()
        .active
        .as_ref()
        .unwrap()
        .sources[0]
        .source
        .clone();
    for batch in 0..8 {
        let mut row = source_row(&source, batch + 1, 0);
        row.subscriptions = (0..512)
            .map(|index| Subscription {
                symbol: format!("S{}USDT", batch * 512 + index),
                feed: Feed::Quote,
            })
            .collect();
        row.content_sha256 = crate::signals::content_sha256(&row);
        for sub in &row.subscriptions {
            let symbol = engine.books.market.add_symbol(&sub.symbol);
            engine.routing.add(symbol, sub.feed, StrategyId(0));
            engine.subscriptions.push(sub.clone());
        }
        engine.signals.accept(row.clone());
        engine.signals.consume(&source, row.sequence);
    }
    let mut replacement = source_row(&source, 9, 0);
    replacement.subscriptions = vec![
        Subscription {
            symbol: "S0USDT".into(),
            feed: Feed::Quote,
        },
        Subscription {
            symbol: "NEWUSDT".into(),
            feed: Feed::Quote,
        },
    ];
    replacement.content_sha256 = crate::signals::content_sha256(&replacement);
    let new_symbol = engine.books.market.add_symbol("NEWUSDT");
    engine.routing.add(new_symbol, Feed::Quote, StrategyId(0));
    engine
        .subscriptions
        .push(replacement.subscriptions[1].clone());
    engine.books.rules.resize(
        engine.books.market.table.len(),
        Some(InstrumentRule {
            tick_size: 0.01,
            qty_step: 0.001,
            min_qty: 0.001,
            min_notional: 1.0,
        }),
    );
    engine
        .signals
        .set_suspension(
            StrategyId(0),
            Some(
                engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget {
                    subscriptions: replacement.subscriptions.clone(),
                },
            ),
            2,
        )
        .unwrap();
    engine
        .pending_signal_deliveries
        .push_back(replacement.clone());
    let mut market = RetiringMarket::default();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert_eq!(
        market.retired.len(),
        4095,
        "a pre-admission replacement leases its requested routes, not all history"
    );
    assert!(engine.signals.suspensions().next().is_none());
    assert_eq!(engine.pending_signal_deliveries.front(), Some(&replacement));
    assert_eq!(
        engine.signals.route_subscriptions(&source, StrategyId(0)),
        &replacement.subscriptions[..1]
    );
    assert!(engine
        .routing
        .quote_listeners(SymbolId(0))
        .contains(&StrategyId(0)));
    assert!(engine
        .routing
        .quote_listeners(new_symbol)
        .contains(&StrategyId(0)));
    engine.accept_pending_signals(&mut feed).unwrap();
    engine.drain(clock::now_ns()).await.unwrap();
    assert!(engine.pending_signal_deliveries.is_empty());
    assert!(
        engine.signals.observations().next().is_none(),
        "the deferred row reaches its durable terminal consumption"
    );
    assert_eq!(engine.signals.cursors().next().unwrap().sequence, 9);
    engine.maintain_signal_routes(&mut market).unwrap();
    assert_eq!(market.retired.len(), 4097);
    assert!(engine
        .signals
        .route_subscriptions(&source, StrategyId(0))
        .is_empty());
    let restored =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 2)
            .unwrap();
    assert_eq!(
        restored.classify(&replacement).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
    assert!(restored.suspensions().next().is_none());
    assert!(restored.observations().next().is_none());
}

#[tokio::test]
async fn signal_route_budget_waits_for_the_entire_blocked_row_across_restart_without_wal_churn() {
    use engine_types::{
        Feed, InstrumentRule, SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier,
    };
    let needed = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let (mut engine, records) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(RouteConsumer(needed.clone())),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let generation = "a".repeat(32);
    let report = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation: generation.clone(),
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: format!("native.g{generation}.long"),
                destination: StrategyId(0),
                published_through: 0,
            },
            SignalSourceFrontier {
                source: format!("native.g{generation}.carry"),
                destination: StrategyId(1),
                published_through: 0,
            },
        ],
    };
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "test".into(),
                producer: report,
            },
            &mut feed,
        )
        .unwrap();
    let source = engine
        .signals
        .producers()
        .next()
        .unwrap()
        .active
        .as_ref()
        .unwrap()
        .sources[0]
        .source
        .clone();
    for batch in 0..8 {
        let mut row = source_row(&source, batch + 1, 0);
        row.subscriptions = (0..512)
            .map(|index| Subscription {
                symbol: format!("S{}USDT", batch * 512 + index),
                feed: Feed::Quote,
            })
            .collect();
        row.content_sha256 = crate::signals::content_sha256(&row);
        for sub in &row.subscriptions {
            let symbol = engine.books.market.add_symbol(&sub.symbol);
            engine.routing.add(symbol, sub.feed, StrategyId(0));
            engine.subscriptions.push(sub.clone());
        }
        needed
            .lock()
            .unwrap()
            .extend(row.subscriptions.iter().cloned());
        engine.signals.accept(row.clone());
        engine.signals.consume(&source, row.sequence);
    }
    let mut replacement = source_row(&source, 9, 0);
    replacement.subscriptions = ["NEW1USDT", "NEW2USDT"]
        .map(|symbol| Subscription {
            symbol: symbol.into(),
            feed: Feed::Quote,
        })
        .to_vec();
    replacement.content_sha256 = crate::signals::content_sha256(&replacement);
    engine
        .queue_signal_observation(replacement.clone(), &mut feed)
        .unwrap();
    assert_eq!(feed.inner.rows.front(), Some(&replacement));
    needed.lock().unwrap().pop();
    let mut market = RetiringMarket::default();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert_eq!(market.retired.len(), 1);
    assert_eq!(
        engine.signals.suspensions().count(),
        1,
        "one released slot cannot resume a row requiring two new subscriptions"
    );
    let count = records.lock().unwrap().len();
    for _ in 0..16 {
        engine.maintain_signal_routes(&mut market).unwrap();
    }
    assert_eq!(
        records.lock().unwrap().len(),
        count,
        "capacity waits cannot churn WAL resume records"
    );
    engine.signals =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 2)
            .unwrap();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert_eq!(
        engine.signals.suspensions().next().unwrap().reason,
        engine_types::SignalAdmissionSuspensionReason::SubscriptionBudget {
            subscriptions: replacement.subscriptions.clone(),
        }
    );
    needed.lock().unwrap().pop();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert!(engine.signals.suspensions().next().is_none());
    let deferred = feed.inner.rows.pop_front().unwrap();
    engine
        .queue_signal_observation(deferred, &mut feed)
        .unwrap();
    for sub in &replacement.subscriptions {
        let symbol = engine.books.market.add_symbol(&sub.symbol);
        engine.routing.add(symbol, sub.feed, StrategyId(0));
        engine.subscriptions.push(sub.clone());
    }
    engine.books.rules.resize(
        engine.books.market.table.len(),
        Some(InstrumentRule {
            tick_size: 0.01,
            qty_step: 0.001,
            min_qty: 0.001,
            min_notional: 1.0,
        }),
    );
    engine.wanted_symbols.clear();
    engine.accept_pending_signals(&mut feed).unwrap();
    engine.drain(clock::now_ns()).await.unwrap();
    assert!(engine.signals.observations().next().is_none());
    assert_eq!(engine.signals.cursors().next().unwrap().sequence, 9);
    assert_eq!(
        engine
            .signals
            .route_subscriptions(&source, StrategyId(0))
            .len(),
        4096
    );
    engine.maintain_signal_routes(&mut market).unwrap();
    assert_eq!(
        engine
            .signals
            .route_subscriptions(&source, StrategyId(0))
            .len(),
        4094
    );
}
