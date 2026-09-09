use super::*;
use engine_types::{Action, Quote, SignalError, SignalObservation, Strategy, StrategyCtx};
use std::sync::atomic::{AtomicUsize, Ordering};

struct Consumer;
impl Strategy for Consumer {
    fn name(&self) -> &str {
        "catalog-owner"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
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
struct Signals {
    acknowledged: usize,
    deferred: Vec<SignalObservation>,
}
impl SignalFeed for Signals {
    fn set_gap_requests(
        &mut self,
        _: &[engine_types::SignalGapRequest],
        _: &[StrategyId],
    ) -> Result<(), SignalError> {
        Ok(())
    }
    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        self.acknowledged += 1;
        Ok(())
    }
    fn defer_last(&mut self, row: SignalObservation) -> Result<(), SignalError> {
        self.deferred.push(row);
        Ok(())
    }
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        std::future::pending().await
    }
}

struct Feeds {
    names: engine_types::SymbolTable,
    learned: Vec<(String, SymbolId)>,
}
impl Feeds {
    fn btc() -> Self {
        let mut names = engine_types::SymbolTable::default();
        names.intern("BTCUSDT");
        Self {
            names,
            learned: Vec::new(),
        }
    }
}
impl MarketFeed for Feeds {
    fn admit(&mut self, symbol: &str, _: Feed) -> Option<SymbolId> {
        Some(self.names.intern(symbol))
    }
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        std::future::pending().await
    }
}
impl OrderFeed for Feeds {
    fn learn(&mut self, symbol: &str, id: SymbolId) {
        self.learned.push((symbol.into(), id));
    }
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        std::future::pending().await
    }
}

fn row() -> SignalObservation {
    let mut row = SignalObservation {
        schema_version: engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "catalog-test".into(),
        destination: StrategyId(0),
        source: "catalog-source".into(),
        sequence: 1,
        observation_id: "catalog-1".into(),
        kind: "catalog-test".into(),
        observed_wall_ts_ms: 1,
        available_wall_ts_ms: 2,
        subscriptions: vec![Subscription {
            symbol: "ETHUSDT".into(),
            feed: Feed::Quote,
        }],
        payload: vec![1],
        content_sha256: String::new(),
    };
    row.content_sha256 = crate::signals::content_sha256(&row);
    row
}
fn rule() -> engine_types::InstrumentRule {
    engine_types::InstrumentRule {
        tick_size: 0.5,
        qty_step: 0.001,
        min_qty: 0.001,
        min_notional: 5.0,
    }
}
struct StalledCatalog(Arc<AtomicUsize>);
#[engine_types::async_trait]
impl InstrumentCatalogClient for StalledCatalog {
    async fn fetch(&self) -> Result<InstrumentCatalog, VenueError> {
        self.0.fetch_add(1, Ordering::SeqCst);
        std::future::pending().await
    }
}

async fn owned_exit(
    engine: &mut Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>,
    sends: &Arc<std::sync::Mutex<Vec<OrderRequest>>>,
) {
    let now = clock::now_ns();
    engine.books.market.apply(&MarketEvent::Quote {
        symbol: SymbolId(0),
        quote: Quote {
            bid_px: 30_000.0,
            ask_px: 30_001.0,
            bid_qty: 1.0,
            ask_qty: 1.0,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: now,
            seq: 1,
        },
    });
    engine.host.pending.push_back(
        Action::Place(Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: "catalog-outage-exit".into(),
            decided_ns: now,
            work: None,
            leverage: None,
        })
        .into(),
    );
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            engine.drain(clock::now_ns()).await.unwrap();
            if !sends.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("symbol admission held an owned reduction");
    {
        let sent = sends.lock().unwrap();
        assert_eq!(sent.len(), 1);
        assert_eq!(
            (sent[0].strategy, sent[0].symbol, sent[0].side, sent[0].qty),
            (StrategyId(0), SymbolId(0), Side::Sell, 0.01)
        );
        assert!(sent[0].reduce_only);
    }
    tokio::time::timeout(
        Duration::from_millis(100),
        engine.venue.set_stop(SymbolId(0), 29_000.0),
    )
    .await
    .expect("catalog read held protective stop")
    .unwrap();
}

#[tokio::test(start_paused = true)]
async fn catalog_outage_retains_one_exact_input_and_keeps_owned_exits_and_stops_live() {
    let (mut engine, records, sends) =
        crate::tests::symbol_admission_test_fixture(Box::new(Consumer)).await;
    let calls = Arc::new(AtomicUsize::new(0));
    engine.symbol_admission.client = Some(Arc::new(StalledCatalog(calls.clone())));
    let mut signals = Signals::default();
    let input = row();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    for _ in 0..32 {
        tokio::time::timeout(
            Duration::from_millis(100),
            engine.admit_wanted(&mut market, &mut orders),
        )
        .await
        .expect("catalog fetch blocked the engine")
        .unwrap();
        tokio::task::yield_now().await;
        engine.accept_pending_signals(&mut signals).unwrap();
    }
    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "one owner must bound catalog work"
    );
    assert_eq!(
        engine.pending_signal_deliveries.iter().collect::<Vec<_>>(),
        vec![&input]
    );
    assert_eq!(engine.wanted_symbols.len(), 1);
    assert_eq!(signals.acknowledged, 0);
    assert!(engine.signals.observations().next().is_none());
    assert!(!records
        .lock()
        .unwrap()
        .iter()
        .any(|record| matches!(record, WalRecord::SignalObservation { .. })));
    owned_exit(&mut engine, &sends).await;
    assert_eq!(engine.pending_signal_deliveries, [input]);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[tokio::test(start_paused = true)]
async fn exhausted_symbol_ids_refuse_new_inputs_without_recycling_existing_exit_ids() {
    use engine_types::identity::{InstrumentBinding, InstrumentIdentity, DENSE_ID_CAPACITY};
    let (mut engine, _, sends) =
        crate::tests::symbol_admission_test_fixture(Box::new(Consumer)).await;
    for index in 1..DENSE_ID_CAPACITY {
        engine.identities.instruments.push(InstrumentBinding {
            symbol: format!("OLD{index}USDT"),
            identity: InstrumentIdentity::Unresolved,
        });
    }
    engine.identities.validate().unwrap();
    let before = engine.identities.clone();
    engine
        .symbol_admission
        .catalog
        .rules
        .push(("ETHUSDT".into(), rule()));
    let input = row();
    let mut signals = Signals::default();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    engine
        .admit_wanted(&mut Feeds::btc(), &mut Feeds::btc())
        .await
        .unwrap();
    engine.accept_pending_signals(&mut signals).unwrap();
    assert!(matches!(
        engine.symbol_admission.failure,
        Some(AdmissionFailure::Identity(
            IdentityError::SymbolIdsExhausted
        ))
    ));
    assert_eq!(engine.identities, before);
    assert_eq!(engine.books.market.table.get("BTCUSDT"), Some(SymbolId(0)));
    assert_eq!(engine.books.market.table.get("ETHUSDT"), None);
    assert_eq!(engine.pending_signal_deliveries, [input]);
    assert_eq!(signals.acknowledged, 0);
    owned_exit(&mut engine, &sends).await;
}

#[tokio::test(start_paused = true)]
async fn symbol_identity_is_durable_before_installation_and_replays_a_crash_before_names() {
    let (mut engine, records, _) =
        crate::tests::symbol_admission_test_fixture(Box::new(Consumer)).await;
    engine
        .symbol_admission
        .catalog
        .rules
        .push(("ETHUSDT".into(), rule()));
    let mut signals = Signals::default();
    engine
        .queue_signal_observation(row(), &mut signals)
        .unwrap();
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    engine.admit_wanted(&mut market, &mut orders).await.unwrap();
    assert!(matches!(
        engine.symbol_admission.phase,
        Phase::Installing { .. }
    ));
    assert_eq!(engine.books.market.table.get("ETHUSDT"), None);
    let crash_prefix = records.lock().unwrap().clone();
    assert!(
        matches!(crash_prefix.last(), Some(WalRecord::IdentityState { state, .. }) if state.instruments[1].symbol == "ETHUSDT")
    );
    assert_eq!(
        crate::assembly::symbol_order(&crash_prefix, &[]).unwrap(),
        ["BTCUSDT", "ETHUSDT"]
    );
    for _ in 0..100 {
        engine.admit_wanted(&mut market, &mut orders).await.unwrap();
        if !engine.symbol_admission.busy() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
    assert_eq!(engine.books.market.table.get("ETHUSDT"), Some(SymbolId(1)));
    assert_eq!(market.names.get("ETHUSDT"), Some(SymbolId(1)));
    assert_eq!(orders.learned, [("ETHUSDT".into(), SymbolId(1))]);
    engine.accept_pending_signals(&mut signals).unwrap();
    engine.drain(clock::now_ns()).await.unwrap();
    assert_eq!(signals.acknowledged, 1);
    assert_eq!(
        crate::identities::replay_identities(&[engine.rotation_base(clock::wall_ms())]).unwrap(),
        Some(engine.identities.clone())
    );
}

#[tokio::test(start_paused = true)]
async fn symbol_identity_barrier_failure_never_installs_or_acknowledges_the_input() {
    let (mut engine, _, _) = crate::tests::symbol_admission_test_fixture(Box::new(Consumer)).await;
    engine
        .symbol_admission
        .catalog
        .rules
        .push(("ETHUSDT".into(), rule()));
    let before = engine.identities.clone();
    engine.wal.fail_barrier_after = Some("identity_state");
    let input = row();
    let mut signals = Signals::default();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    assert!(engine
        .admit_wanted(&mut market, &mut orders)
        .await
        .unwrap_err()
        .to_string()
        .contains("test barrier failure"));
    assert_eq!(engine.identities, before);
    assert!(!engine.symbol_admission.busy());
    assert_eq!(market.names.get("ETHUSDT"), None);
    assert!(orders.learned.is_empty());
    assert_eq!(signals.acknowledged, 0);
    assert_eq!(engine.pending_signal_deliveries, [input]);
}

struct RecoveringCatalog {
    calls: AtomicUsize,
    available: std::sync::atomic::AtomicBool,
    wake: tokio::sync::Notify,
}
#[engine_types::async_trait]
impl InstrumentCatalogClient for RecoveringCatalog {
    async fn fetch(&self) -> Result<InstrumentCatalog, VenueError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        while !self.available.load(Ordering::SeqCst) {
            self.wake.notified().await;
        }
        Ok(crate::tests::test_instrument_catalog(&[
            "BTCUSDT", "ETHUSDT",
        ]))
    }
}

#[tokio::test(start_paused = true)]
async fn catalog_outage_restart_restores_native_metadata_for_exits_and_holds_growth_until_refresh()
{
    let client = Arc::new(RecoveringCatalog {
        calls: AtomicUsize::new(0),
        available: std::sync::atomic::AtomicBool::new(false),
        wake: tokio::sync::Notify::new(),
    });
    let (mut engine, _, sends) = tokio::time::timeout(
        Duration::from_millis(200),
        crate::tests::catalog_restart_test_fixture(Box::new(Consumer), None, client.clone()),
    )
    .await
    .expect("restart waited for unavailable public metadata");
    assert_eq!(client.calls.load(Ordering::SeqCst), 0);
    assert_eq!(
        engine.opening_permission_reason(StrategyId(0)),
        Some(super::super::intent_admission::OpeningRefusal::InstrumentCatalogUnready)
    );
    assert_eq!(
        engine.instrument_specs[&SymbolId(0)].native_symbol,
        "BTCUSDT"
    );
    let mut signals = Signals::default();
    let input = row();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    engine
        .admit_wanted(&mut Feeds::btc(), &mut Feeds::btc())
        .await
        .unwrap();
    tokio::task::yield_now().await;
    assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    let base = engine.rotation_base(clock::wall_ms());
    assert!(
        matches!(&base, WalRecord::SegmentBase { instrument_catalog: Some(checkpoint), .. } if checkpoint.specs[0].1.native_symbol == "BTCUSDT")
    );
    owned_exit(&mut engine, &sends).await;
    assert_eq!(signals.acknowledged, 0);
    assert_eq!(
        engine.pending_signal_deliveries.iter().collect::<Vec<_>>(),
        vec![&input]
    );
    drop(engine);

    let (mut restarted, records, _) = tokio::time::timeout(
        Duration::from_millis(200),
        crate::tests::catalog_restart_test_fixture(
            Box::new(Consumer),
            Some(vec![base]),
            client.clone(),
        ),
    )
    .await
    .expect("rotated restart lost retained metadata");
    assert!(restarted.symbol_admission.refresh_required());
    restarted
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    client.available.store(true, Ordering::SeqCst);
    client.wake.notify_waiters();
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            restarted.symbol_admission.retry_after_ns = 0;
            restarted
                .admit_wanted(&mut market, &mut orders)
                .await
                .unwrap();
            restarted.accept_pending_signals(&mut signals).unwrap();
            if signals.acknowledged == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("metadata recovery did not resume retained input");
    assert_eq!(
        client.calls.load(Ordering::SeqCst),
        2,
        "one fetch per boot owns recovery"
    );
    assert!(!restarted.symbol_admission.refresh_required());
    assert_eq!(restarted.opening_permission_reason(StrategyId(0)), None);
    assert_eq!(
        restarted.books.market.table.get("ETHUSDT"),
        Some(SymbolId(1))
    );
    let records = records.lock().unwrap();
    assert_eq!(
        records
            .iter()
            .filter(|record| matches!(record, WalRecord::InstrumentCatalogCheckpoint { .. }))
            .count(),
        1
    );
    assert_eq!(records.iter().filter(|record| matches!(record, WalRecord::SignalObservation { observation, .. } if observation == &input)).count(), 1);
}

struct DelistedCatalog;
#[engine_types::async_trait]
impl InstrumentCatalogClient for DelistedCatalog {
    async fn fetch(&self) -> Result<InstrumentCatalog, VenueError> {
        Ok(crate::tests::test_instrument_catalog(&["ETHUSDT"]))
    }
}

/// A venue table that lists BTCUSDT and nothing else, counting every fetch.
struct BtcOnlyCatalog(Arc<AtomicUsize>);
#[engine_types::async_trait]
impl InstrumentCatalogClient for BtcOnlyCatalog {
    async fn fetch(&self) -> Result<InstrumentCatalog, VenueError> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(crate::tests::test_instrument_catalog(&["BTCUSDT"]))
    }
}

#[tokio::test(start_paused = true)]
async fn a_name_the_venue_does_not_list_is_dropped_without_refetching_the_table() {
    // The table is fresh and authoritative; asking for it again cannot list
    // the name, and nothing can be priced or ordered in it.
    let calls = Arc::new(AtomicUsize::new(0));
    let (mut engine, _records, _sends) = crate::tests::catalog_restart_test_fixture(
        Box::new(Consumer),
        None,
        Arc::new(BtcOnlyCatalog(calls.clone())),
    )
    .await;
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    tokio::time::timeout(Duration::from_secs(1), async {
        while engine.symbol_admission.busy() {
            engine.symbol_admission.retry_after_ns = 0;
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let fetched_once = calls.load(Ordering::SeqCst);
    assert!(fetched_once >= 1);
    assert!(engine.symbol_admission.listed("BTCUSDT"));
    assert!(!engine.symbol_admission.listed("ETHUSDT"));

    // ETHUSDT is wanted by a signal and absent from the venue's table.
    let mut signals = Signals::default();
    engine
        .queue_signal_observation(row(), &mut signals)
        .unwrap();
    for _ in 0..40 {
        engine.symbol_admission.retry_after_ns = 0;
        engine.admit_wanted(&mut market, &mut orders).await.unwrap();
        tokio::task::yield_now().await;
    }
    assert_eq!(
        calls.load(Ordering::SeqCst),
        fetched_once,
        "an unlisted name refetched the venue table"
    );
    assert!(
        !engine
            .wanted_symbols
            .iter()
            .any(|wanted| wanted.name == "ETHUSDT"),
        "an unlisted name still holds the symbol admission queue"
    );
    assert!(
        engine.symbol_admission.failure.is_none(),
        "an unlisted name was reported as a metadata failure: {:?}",
        engine.symbol_admission.failure
    );
}

#[tokio::test(start_paused = true)]
async fn an_unlisted_subscription_is_dropped_and_its_batch_is_delivered() {
    // Observed live on 2026-09-08: the mexc engine's head-of-line CARRY batch
    // named symbols MEXC does not list, so the row was re-queued forever and
    // no signal file behind it was ever read.
    let calls = Arc::new(AtomicUsize::new(0));
    let (mut engine, records, _sends) = crate::tests::catalog_restart_test_fixture(
        Box::new(Consumer),
        None,
        Arc::new(BtcOnlyCatalog(calls.clone())),
    )
    .await;
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    tokio::time::timeout(Duration::from_secs(1), async {
        while engine.symbol_admission.busy() {
            engine.symbol_admission.retry_after_ns = 0;
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let fetched = calls.load(Ordering::SeqCst);

    let mut input = row();
    input.subscriptions = ["BTCUSDT", "ETHUSDT"]
        .map(|symbol| Subscription {
            symbol: symbol.into(),
            feed: Feed::Quote,
        })
        .to_vec();
    input.content_sha256 = crate::signals::content_sha256(&input);
    let mut signals = Signals::default();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            engine.symbol_admission.retry_after_ns = 0;
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            engine.accept_pending_signals(&mut signals).unwrap();
            if signals.acknowledged == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("the batch never reached its destination");
    assert_eq!(
        calls.load(Ordering::SeqCst),
        fetched,
        "an unlisted subscription refetched the venue table"
    );
    assert!(
        !engine
            .wanted_symbols
            .iter()
            .any(|wanted| wanted.name == "ETHUSDT"),
        "the unlisted subscription still holds the symbol admission queue"
    );
    assert!(engine.pending_signal_deliveries.is_empty());
    assert!(engine.symbol_admission.failure.is_none());
    assert!(engine
        .routing
        .quote_listeners(SymbolId(0))
        .contains(&StrategyId(0)));
    assert!(engine.books.market.table.get("ETHUSDT").is_none());
    assert_eq!(
        records
            .lock()
            .unwrap()
            .iter()
            .filter(|record| matches!(record, WalRecord::SignalObservation { observation, .. } if observation == &input))
            .count(),
        1,
        "the observation is journaled once, byte for byte"
    );
}

#[tokio::test(start_paused = true)]
async fn a_delisted_name_with_a_retained_rule_keeps_its_market_subscription() {
    // The fresh table drops BTCUSDT; `retain_previous` keeps its rule so an
    // open position can exit, and the subscription must follow the price.
    let (mut engine, _records, sends) = crate::tests::catalog_restart_test_fixture(
        Box::new(Consumer),
        None,
        Arc::new(DelistedCatalog),
    )
    .await;
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    tokio::time::timeout(Duration::from_secs(1), async {
        while engine.symbol_admission.busy() {
            engine.symbol_admission.retry_after_ns = 0;
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert!(!engine.symbol_admission.listed("BTCUSDT"));

    let mut input = row();
    input.subscriptions = vec![Subscription {
        symbol: "BTCUSDT".into(),
        feed: Feed::Ticker,
    }];
    input.content_sha256 = crate::signals::content_sha256(&input);
    let mut signals = Signals::default();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            engine.symbol_admission.retry_after_ns = 0;
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            engine.accept_pending_signals(&mut signals).unwrap();
            if signals.acknowledged == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("a delisted name's subscription was never installed");
    assert!(engine
        .routing
        .ticker_listeners(SymbolId(0))
        .contains(&StrategyId(0)));
    assert!(engine.subscriptions.contains(&input.subscriptions[0]));
    owned_exit(&mut engine, &sends).await;
}

#[tokio::test(start_paused = true)]
async fn refreshed_catalog_retains_omitted_native_exit_metadata_without_reopening_growth() {
    let (mut engine, records, sends) = crate::tests::catalog_restart_test_fixture(
        Box::new(Consumer),
        None,
        Arc::new(DelistedCatalog),
    )
    .await;
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    tokio::time::timeout(Duration::from_secs(1), async {
        while engine.symbol_admission.busy() {
            engine.symbol_admission.retry_after_ns = 0;
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert!(!engine.symbol_admission.refresh_required());
    assert!(!engine.symbol_admission.listed("BTCUSDT"));
    assert!(engine.symbol_admission.listed("ETHUSDT"));
    assert_eq!(
        engine.instrument_specs[&SymbolId(0)].native_symbol,
        "BTCUSDT"
    );
    assert!(engine
        .symbol_admission
        .checkpoint
        .as_ref()
        .unwrap()
        .specs
        .iter()
        .any(|(name, _)| name == "BTCUSDT"));
    engine.books.market.apply(&MarketEvent::Quote {
        symbol: SymbolId(0),
        quote: Quote {
            bid_px: 30_000.0,
            ask_px: 30_001.0,
            bid_qty: 1.0,
            ask_qty: 1.0,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: clock::now_ns(),
            seq: 1,
        },
    });
    engine.host.pending.push_back(
        Action::Place(Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec {
                trigger_px: 29_000.0,
            }),
            reduce_only: false,
            tag: "delisted-growth".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        })
        .into(),
    );
    engine.drain(clock::now_ns()).await.unwrap();
    assert!(sends.lock().unwrap().is_empty());
    assert!(records.lock().unwrap().iter().any(|record| matches!(record,
        WalRecord::Verdict { verdict: RiskVerdict::Deny { reason: engine_types::DenyReason::UnknownState { detail } }, .. }
            if detail == "instrument_unlisted: retained native metadata permits reductions and stops only")));
    owned_exit(&mut engine, &sends).await;
}

#[tokio::test(start_paused = true)]
async fn catalog_durability_stall_retains_metadata_without_holding_existing_protection() {
    stalled_metadata_durability(true).await;
}

#[tokio::test(start_paused = true)]
async fn identity_durability_stall_retains_input_without_holding_existing_protection() {
    stalled_metadata_durability(false).await;
}

async fn stalled_metadata_durability(refresh: bool) {
    let (mut engine, _, sends) =
        crate::tests::symbol_admission_test_fixture(Box::new(Consumer)).await;
    engine
        .wal
        .delay_metadata_barriers(Duration::from_millis(300));
    let mut signals = Signals::default();
    let input = row();
    engine
        .queue_signal_observation(input.clone(), &mut signals)
        .unwrap();
    let (mut market, mut orders) = (Feeds::btc(), Feeds::btc());
    let before = engine.identities.clone();
    let started = std::time::Instant::now();
    if refresh {
        engine
            .retain_refreshed_catalog(RefreshedCatalog {
                catalog: crate::tests::test_instrument_catalog(&["BTCUSDT", "ETHUSDT"]),
                listed: ["BTCUSDT".into(), "ETHUSDT".into()].into_iter().collect(),
            })
            .unwrap();
    } else {
        engine
            .symbol_admission
            .catalog
            .rules
            .push(("ETHUSDT".into(), rule()));
        engine.admit_wanted(&mut market, &mut orders).await.unwrap();
    }
    assert!(
        started.elapsed() < Duration::from_millis(100),
        "metadata durability blocked engine for {:?}",
        started.elapsed()
    );
    assert_eq!(engine.identities, before);
    assert_eq!(engine.books.market.table.get("ETHUSDT"), None);
    engine.accept_pending_signals(&mut signals).unwrap();
    assert_eq!(signals.acknowledged, 0);
    assert_eq!(
        engine.pending_signal_deliveries.iter().collect::<Vec<_>>(),
        vec![&input]
    );
    tokio::time::timeout(
        Duration::from_millis(100),
        engine.venue.set_stop(SymbolId(0), 29_000.0),
    )
    .await
    .expect("metadata durability held an existing protective stop")
    .unwrap();
    assert_eq!(engine.books.market.table.get("ETHUSDT"), None);
    engine.wal.delay_metadata_barriers(Duration::ZERO);
    owned_exit(&mut engine, &sends).await;
    assert_eq!(engine.books.market.table.get("ETHUSDT"), None);
    assert_eq!(signals.acknowledged, 0);
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            engine.admit_wanted(&mut market, &mut orders).await.unwrap();
            if engine.books.market.table.get("ETHUSDT").is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("durable metadata never resumed installation");
    engine.accept_pending_signals(&mut signals).unwrap();
    assert_eq!(signals.acknowledged, 1);
    assert_eq!(engine.books.market.table.get("ETHUSDT"), Some(SymbolId(1)));
}
