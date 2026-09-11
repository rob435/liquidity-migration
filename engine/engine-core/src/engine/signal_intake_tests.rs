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

#[tokio::test(start_paused = true)]
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
                source_sleeves: Vec::new(),
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
                source_sleeves: Vec::new(),
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

#[tokio::test(start_paused = true)]
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
                source_sleeves: Vec::new(),
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
                source_sleeves: Vec::new(),
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

#[tokio::test(start_paused = true)]
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
            subscriptions: row.subscriptions
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

#[tokio::test(start_paused = true)]
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
                source_sleeves: Vec::new(),
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
    let reader = engine.wal.callback_reader().unwrap().unwrap();
    engine
        .host
        .callbacks
        .order_news
        .attach(reader, &crate::assembly::BootReplay::dense(&[]), 2)
        .unwrap();
    engine
        .host
        .callbacks
        .order_news
        .record(1, &[StrategyId(0)])
        .unwrap();
    engine.maintain_signal_routes(&mut market).unwrap();
    assert!(
        market.retired.is_empty(),
        "unread order callback source still owns its route"
    );
    let origin = engine.host.callbacks.order_news.origin(1).unwrap();
    engine
        .host
        .callbacks
        .order_news
        .accepted(StrategyId(0), origin);
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

#[tokio::test(start_paused = true)]
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
                source_sleeves: Vec::new(),
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

#[tokio::test(start_paused = true)]
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
                source_sleeves: Vec::new(),
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

#[tokio::test(start_paused = true)]
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
                source_sleeves: Vec::new(),
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

#[tokio::test(start_paused = true)]
async fn named_source_bindings_precede_fresh_input_and_refuse_numeric_reassignment() {
    use engine_types::identity::{InstrumentScope, SignalSourceSleeve, SleeveKey};
    use engine_types::{SignalLifecycleResponse, SignalProducerReport, SignalSourceFrontier};
    let (mut engine, records) = crate::tests::lifecycle_test_fixture(vec![
        Box::new(Consumer("long", false)),
        Box::new(Consumer("carry", false)),
    ])
    .await;
    engine.identities.scope = Some(InstrumentScope {
        venue: "mock".into(),
        environment: "demo".into(),
    });
    engine
        .signals
        .require_readiness([StrategyId(0), StrategyId(1)]);
    let generation = "a".repeat(32);
    let mut feed = LifecycleSignals {
        inner: FinishedSignals {
            rows: VecDeque::new(),
            done: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
        },
        requests: Vec::new(),
    };
    let wrong = source_row(&format!("native.g{generation}.long"), 1, 1);
    engine
        .queue_signal_observation(wrong.clone(), &mut feed)
        .unwrap();
    engine.accept_pending_signals(&mut feed).unwrap();
    assert_eq!(feed.inner.rows.iter().collect::<Vec<_>>(), vec![&wrong]);
    assert!(engine.signals.observations().next().is_none());
    assert!(!records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(record, WalRecord::SignalObservation { .. })));
    let report = SignalProducerReport {
        producer: "native".into(),
        epoch: None,
        generation,
        sealed: true,
        sources: vec![
            SignalSourceFrontier {
                source: wrong.source.clone(),
                destination: StrategyId(1),
                published_through: 1,
            },
            SignalSourceFrontier {
                source: wrong.source.replace(".long", ".carry"),
                destination: StrategyId(0),
                published_through: 0,
            },
        ],
    };
    let bindings = vec![
        SignalSourceSleeve {
            source: report.sources[0].source.clone(),
            sleeve: SleeveKey::new("long").unwrap(),
        },
        SignalSourceSleeve {
            source: report.sources[1].source.clone(),
            sleeve: SleeveKey::new("carry").unwrap(),
        },
    ];
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "fresh".into(),
                producer: report.clone(),
                source_sleeves: bindings.clone(),
            },
            &mut feed,
        )
        .unwrap();
    assert!(engine.signals.producers().next().is_none());
    assert!(!engine
        .signals
        .named_source_verified(&wrong.source, StrategyId(1)));
    assert!(records.lock().unwrap().iter().any(|record| matches!(record, WalRecord::Note { text, .. } if text.contains("do not match the durable sleeve registry"))));
    let mut correct = report;
    correct.sources[0].destination = StrategyId(0);
    correct.sources[1].destination = StrategyId(1);
    engine
        .accept_signal_lifecycle(
            SignalLifecycleResponse {
                schema_version: 2,
                boot_nonce: "correct".into(),
                producer: correct.clone(),
                source_sleeves: bindings,
            },
            &mut feed,
        )
        .unwrap();
    assert!(engine
        .signals
        .named_source_verified(&wrong.source, StrategyId(0)));
    assert!(!engine
        .signals
        .named_source_verified(&wrong.source, StrategyId(1)));
    let mut tail = source_row(&wrong.source, 1, 0);
    tail.payload = vec![2];
    tail.content_sha256 = crate::signals::content_sha256(&tail);
    engine
        .queue_signal_observation(tail.clone(), &mut feed)
        .unwrap();
    engine.accept_pending_signals(&mut feed).unwrap();
    assert_eq!(
        engine.signals.observations().cloned().collect::<Vec<_>>(),
        [tail.clone()]
    );
    let mut restored =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 2)
            .unwrap();
    restored.require_readiness([StrategyId(0), StrategyId(1)]);
    assert!(
        !restored.named_source_verified(&tail.source, StrategyId(0)),
        "each engine boot needs a fresh named response"
    );
    assert_eq!(restored.observations().cloned().collect::<Vec<_>>(), [tail]);
    assert_eq!(
        restored.classify(&wrong).unwrap(),
        crate::signal_state::Admission::Unregistered
    );
}

struct CountSignalCallbacks(std::sync::Arc<std::sync::atomic::AtomicUsize>);
impl Strategy for CountSignalCallbacks {
    fn name(&self) -> &str {
        "retained-noop"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }
    fn on_signal(&mut self, _: &SignalObservation, _: &mut dyn StrategyCtx) {
        self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }
}

#[tokio::test(start_paused = true)]
async fn noop_signal_delivery_survives_rotation_restart_without_consumption_or_redelivery() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let calls = std::sync::Arc::new(AtomicUsize::new(0));
    let (mut engine, records) =
        crate::tests::lifecycle_test_fixture(vec![Box::new(CountSignalCallbacks(calls.clone()))])
            .await;
    let row = source_row("noop-durable", 1, 0);
    let mut feed = FinishedSignals {
        rows: VecDeque::new(),
        done: None,
        gaps: Vec::new(),
        blocked_destinations: Vec::new(),
    };
    engine
        .queue_signal_observation(row.clone(), &mut feed)
        .unwrap();
    engine.accept_pending_signals(&mut feed).unwrap();
    engine.drain(clock::now_ns()).await.unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        engine.signals.observations().collect::<Vec<_>>(),
        vec![&row]
    );
    assert_eq!(engine.signals.undelivered().count(), 0);
    assert!(!records.lock().unwrap().iter().any(|record| matches!(
        record,
        WalRecord::SignalObservationConsumed { .. } | WalRecord::SignalObservationRejected { .. }
    )));
    let base = engine.rotation_base(clock::wall_ms());
    let encoded = serde_json::to_vec(&base).unwrap();
    let decoded = serde_json::from_slice::<WalRecord>(&encoded).unwrap();
    engine.signals = crate::signal_state::SignalState::replay(&[decoded], 1).unwrap();
    assert_eq!(
        engine.signals.callback_deliveries(),
        [engine_types::strategy_process::SignalCallbackDelivery {
            strategy: row.destination,
            source: row.source.clone(),
            sequence: row.sequence,
            observation_id: row.observation_id.clone(),
        }]
    );
    engine.deliver_pending_signal_callbacks();
    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "a retained no-op input was launched again after rotation"
    );
    assert_eq!(
        engine.signals.observations().collect::<Vec<_>>(),
        vec![&row]
    );
    assert_eq!(
        engine.signals.classify(&row).unwrap(),
        crate::signal_state::Admission::Duplicate
    );
    let twice =
        crate::signal_state::SignalState::replay(&[engine.rotation_base(clock::wall_ms())], 1)
            .unwrap();
    assert_eq!(twice.undelivered().count(), 0);
    assert_eq!(twice.observations().collect::<Vec<_>>(), vec![&row]);
}

#[tokio::test(start_paused = true)]
async fn signal_callback_markers_refuse_wrong_identity_duplicate_markers_and_changed_input() {
    use engine_types::strategy_process::{
        CallbackEvent, CallbackPreparation, SignalCallbackDelivery, StrategyCallbackInput,
    };
    let (engine, _) =
        crate::tests::lifecycle_test_fixture(vec![Box::new(Consumer("marker-owner", false))]).await;
    let row = source_row("marker-source", 1, 0);
    let accepted = WalRecord::SignalObservation {
        wall_ts_ms: 2,
        observation: row.clone(),
    };
    let queued = |observation: SignalObservation| {
        WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                input: StrategyCallbackInput {
                    order_origin: None,
                    callback_id: 1,
                    strategy: row.destination,
                    event: CallbackEvent::Signal { observation },
                    preparation: CallbackPreparation::Queued,
                },
            },
        )
    };
    let replay =
        crate::signal_state::SignalState::replay(&[accepted.clone(), queued(row.clone())], 1)
            .unwrap();
    assert_eq!(replay.observations().collect::<Vec<_>>(), vec![&row]);
    assert_eq!(replay.undelivered().count(), 0);
    let mut altered = row.clone();
    altered.payload = vec![9];
    altered.content_sha256 = crate::signals::content_sha256(&altered);
    assert_eq!(
        crate::signal_state::SignalState::replay(&[accepted, queued(altered)], 1).unwrap_err(),
        "queued callback changes its accepted signal observation"
    );
    let exact = SignalCallbackDelivery {
        strategy: row.destination,
        source: row.source.clone(),
        sequence: row.sequence,
        observation_id: row.observation_id.clone(),
    };
    for malformed in [
        SignalCallbackDelivery {
            strategy: StrategyId(1),
            ..exact.clone()
        },
        SignalCallbackDelivery {
            source: "other-source".into(),
            ..exact.clone()
        },
        SignalCallbackDelivery {
            sequence: 2,
            ..exact.clone()
        },
        SignalCallbackDelivery {
            observation_id: "other-id".into(),
            ..exact.clone()
        },
    ] {
        let mut base = engine.rotation_base(clock::wall_ms());
        if let WalRecord::SegmentBase {
            signal_observations,
            signal_cursors,
            signal_subscriptions,
            signal_callback_deliveries,
            ..
        } = &mut base
        {
            signal_observations.push(row.clone());
            signal_cursors.extend(replay.cursors().cloned());
            signal_subscriptions.extend(replay.subscriptions().cloned());
            signal_callback_deliveries.push(malformed.clone());
        }
        let error = crate::signal_state::SignalState::replay(&[base], 2).unwrap_err();
        assert_eq!(
            error,
            if malformed.source != row.source || malformed.sequence != row.sequence {
                "callback delivery has no retained signal observation"
            } else {
                "callback delivery changes its accepted signal identity"
            },
            "malformed marker failed for an unrelated reason: {malformed:?}"
        );
    }
    let mut repeated = engine.rotation_base(clock::wall_ms());
    if let WalRecord::SegmentBase {
        signal_observations,
        signal_cursors,
        signal_subscriptions,
        signal_callback_deliveries,
        ..
    } = &mut repeated
    {
        signal_observations.push(row);
        signal_cursors.extend(replay.cursors().cloned());
        signal_subscriptions.extend(replay.subscriptions().cloned());
        signal_callback_deliveries.extend([exact.clone(), exact]);
    }
    assert_eq!(
        crate::signal_state::SignalState::replay(&[repeated], 1).unwrap_err(),
        "rotation repeats a signal callback delivery marker"
    );
}
