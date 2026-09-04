use super::*;
use engine_types::{SignalError, SignalFeed, SignalGapRequest, SignalObservation};

struct SignalCollector(&'static str, Rc<RefCell<Vec<SignalObservation>>>);

impl Strategy for SignalCollector {
    fn name(&self) -> &str {
        self.0
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }

    fn on_signal(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        self.1.lock().unwrap().push(observation.clone());
        ctx.emit(engine_types::Action::ConsumeSignalObservation {
            strategy: observation.destination,
            source: observation.source.clone(),
            sequence: observation.sequence,
            observation_id: observation.observation_id.clone(),
        });
    }
}

struct UnfilteredSignals {
    next: Option<SignalObservation>,
    deferred: Option<SignalObservation>,
    acknowledged: bool,
    done: Option<tokio::sync::oneshot::Sender<()>>,
}

impl SignalFeed for UnfilteredSignals {
    fn set_gap_requests(
        &mut self,
        _gaps: &[SignalGapRequest],
        _blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        self.acknowledged = true;
        Ok(())
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        self.deferred = Some(observation);
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        if let Some(row) = self.next.take() {
            return Ok(row);
        }
        if let Some(done) = self.done.take() {
            let _ = done.send(());
        }
        std::future::pending().await
    }
}

fn observation(available_ms: i64) -> SignalObservation {
    let mut observation = SignalObservation {
        schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "availability-test".into(),
        destination: StrategyId(0),
        source: "source".into(),
        sequence: 1,
        observation_id: "source-1".into(),
        kind: "test".into(),
        observed_wall_ts_ms: 1,
        available_wall_ts_ms: available_ms,
        subscriptions: Vec::new(),
        payload: b"{}".to_vec(),
        content_sha256: String::new(),
    };
    observation.content_sha256 = crate::signals::content_sha256(&observation);
    observation
}

#[tokio::test]
async fn future_availability_custom_feed_cannot_advance_the_wal_or_invoke_a_strategy() {
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, harness) = build(
        allow_all(),
        vec![Box::new(SignalCollector(
            "signal-availability",
            delivered.clone(),
        ))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let _clock = engine_types::clock::install_virtual(1_000_000_000, 0).unwrap();
    let row = observation(1_100);
    let (done, stopped) = tokio::sync::oneshot::channel();
    let mut signals = UnfilteredSignals {
        next: Some(row.clone()),
        deferred: None,
        acknowledged: false,
        done: Some(done),
    };
    engine
        .run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = stopped.await;
            },
        )
        .await
        .unwrap();
    assert!(delivered.lock().unwrap().is_empty());
    assert!(!harness
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(
            record,
            WalRecord::SignalObservation { .. } | WalRecord::SignalGapRecorded { .. }
        )));
    assert!(!signals.acknowledged);
    assert_eq!(signals.deferred, Some(row));
}

struct ClockRollbackMarket {
    inner: ScriptFeed,
    clock: Option<engine_types::clock::VirtualClockGuard>,
}

impl MarketFeed for ClockRollbackMarket {
    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        drop(self.clock.take());
        self.clock = Some(engine_types::clock::install_virtual(900_000_000, 0).unwrap());
        self.inner.admit(symbol, feed)
    }

    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        self.inner.next_event().await
    }
}

#[tokio::test]
async fn future_availability_is_rechecked_after_symbol_admission() {
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, harness) = build(
        allow_all(),
        vec![Box::new(SignalCollector(
            "signal-availability",
            delivered.clone(),
        ))],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let mut market = ClockRollbackMarket {
        inner: ScriptFeed::quotes(SymbolId(0), 0, false),
        clock: Some(engine_types::clock::install_virtual(1_000_000_000, 0).unwrap()),
    };
    let mut row = observation(1_000);
    row.subscriptions.push(Subscription {
        symbol: "ETHUSDT".into(),
        feed: Feed::Quote,
    });
    row.content_sha256 = crate::signals::content_sha256(&row);
    let (done, stopped) = tokio::sync::oneshot::channel();
    let mut signals = UnfilteredSignals {
        next: Some(row.clone()),
        deferred: None,
        acknowledged: false,
        done: Some(done),
    };
    engine
        .run_with_signals(
            &mut market,
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            async {
                let _ = stopped.await;
            },
        )
        .await
        .unwrap();
    assert_eq!(crate::clock::wall_ms(), 900);
    assert!(delivered.lock().unwrap().is_empty());
    assert!(!harness
        .records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(record, WalRecord::SignalObservation { .. })));
    assert!(!signals.acknowledged);
    assert_eq!(signals.deferred, Some(row));
}

#[tokio::test]
async fn future_availability_preserves_source_prefixes_while_independent_destinations_flow() {
    let delivered = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, harness) = build(
        allow_all(),
        vec![
            Box::new(SignalCollector("future-destination", delivered.clone())),
            Box::new(SignalCollector("ready-destination", delivered.clone())),
        ],
        &["BTCUSDT"],
        &[],
    )
    .await;
    let _clock = engine_types::clock::install_virtual(1_000_000_000, 0).unwrap();
    let future = observation(1_100);
    let mut later = observation(2);
    later.sequence = 2;
    later.observation_id = "source-2".into();
    later.content_sha256 = crate::signals::content_sha256(&later);
    let mut independent = observation(2);
    independent.destination = StrategyId(1);
    independent.source = "independent".into();
    independent.observation_id = "independent-1".into();
    independent.content_sha256 = crate::signals::content_sha256(&independent);
    let (sender, mut signals) = crate::signals::signal_channel();
    for row in [&future, &later, &independent] {
        sender.try_send(row.clone()).unwrap();
    }
    let advance_and_stop = async {
        loop {
            if !delivered.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        assert_eq!(*delivered.lock().unwrap(), vec![independent.clone()]);
        assert!(harness.records.lock().unwrap().iter().any(|record| matches!(
            record,
            WalRecord::SignalGapRecorded { gap, .. }
                if gap.source == "source" && gap.next_sequence == 1 && gap.observed_sequence == 2
        )));
        engine_types::clock::advance_virtual_to(100_000_000).unwrap();
        loop {
            if delivered.lock().unwrap().len() == 3 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    };
    tokio::time::timeout(
        Duration::from_secs(2),
        engine.run_with_signals(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut ScriptOrderFeed::empty(),
            &mut signals,
            advance_and_stop,
        ),
    )
    .await
    .expect("the missing prefix becomes deliverable without restarting")
    .unwrap();
    assert_eq!(*delivered.lock().unwrap(), vec![independent, future, later]);
    for record in harness.records.lock().unwrap().iter() {
        if let WalRecord::SignalObservation {
            wall_ts_ms,
            observation,
        } = record
        {
            assert!(*wall_ts_ms >= observation.available_wall_ts_ms);
        }
    }
}
