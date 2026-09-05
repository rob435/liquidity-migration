use std::path::Path;
use std::process::Command;
use std::time::Duration;

use engine_core::strategy_process::StrategyProcess;
use engine_types::strategy_process::{
    CallbackEvent, CallbackRequest, CallbackSnapshot, DepthSnapshot, OwnedOrderSnapshot,
    SymbolSnapshot, STRATEGY_PROCESS_SCHEMA,
};
use engine_types::{
    Action, Depth, InstrumentRule, OrderKind, Quote, Side, StrategyAccountSummary, StrategyId,
    SymbolId, TimeInForce,
};

fn request(kind: &str, params: &str) -> CallbackRequest {
    let params = toml::from_str(params).unwrap();
    let strategy = engine_strategies::build_strategy(kind, StrategyId(0), &params).unwrap();
    CallbackRequest {
        schema_version: STRATEGY_PROCESS_SCHEMA,
        callback_id: 7,
        state: strategy.runtime_state().unwrap().unwrap(),
        event: CallbackEvent::Boot,
        snapshot: CallbackSnapshot {
            strategy: StrategyId(0),
            now_ns: 1_000_000_000,
            wall_ms: 60_000,
            entries_enabled: true,
            account: StrategyAccountSummary {
                equity_usdt: 1000.0,
                available_margin_usdt: 1000.0,
                observed_ns: 1,
            },
            symbols: vec![SymbolSnapshot {
                id: SymbolId(0),
                name: "BTCUSDT".into(),
                quote: Quote {
                    bid_px: 99.0,
                    ask_px: 101.0,
                    recv_ns: 1_000_000_000,
                    ..Quote::default()
                },
                depth: DepthSnapshot::from(&Depth::default()),
                trades: Default::default(),
                ticker: Default::default(),
                instrument: Some(InstrumentRule {
                    tick_size: 0.1,
                    qty_step: 0.01,
                    min_qty: 0.01,
                    min_notional: 1.0,
                }),
                position: None,
                foreign_position: false,
                my_position: 0.0,
                in_flight: 0.0,
                facts: None,
                checkpoint: None,
            }],
            orders: Vec::new(),
            global_checkpoint: None,
            strategy_names: vec![kind.into()],
            strategy_events: Vec::new(),
        },
    }
}

fn probe() -> CallbackRequest {
    request("probe", "symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false")
}

fn quoter() -> CallbackRequest {
    request("quoter", "symbols = ['BTCUSDT']\nhalf_spread_bps = 5\nrequote_bps = 1\nskew_bps = 1\nqty = 0.01\nmax_position = 1\nstop_loss_fraction = 0.05\nquote_enabled = false")
}

fn order(id: String) -> OwnedOrderSnapshot {
    OwnedOrderSnapshot {
        id,
        symbol: SymbolId(0),
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 98.0,
            tif: TimeInForce::Gtc,
        },
        qty: 0.01,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
        resting: true,
    }
}

fn worker() -> StrategyProcess {
    StrategyProcess::spawn(Path::new(env!("CARGO_BIN_EXE_engine"))).unwrap()
}

#[tokio::test]
async fn registered_private_state_and_timers_return_only_in_a_complete_proposal() {
    let input = probe();
    let original = input.state.clone();
    let (_process, proposal) = worker().call(input, Duration::from_secs(10)).await.unwrap();
    assert!(proposal.actions.is_empty());
    assert_eq!(proposal.timers.len(), 1);
    assert_eq!(proposal.timers[0].deadline_wall_ms, 120_000);
    assert_ne!(
        proposal.state.payload, original.payload,
        "the resolved private symbol cache is part of runtime state"
    );
    let restored = engine_strategies::runtime::restore(&proposal.state).unwrap();
    assert_eq!(restored.runtime_state().unwrap().unwrap(), proposal.state);
}

#[tokio::test]
async fn a_process_flood_preserves_the_exit_after_1024_opening_order_cancels() {
    let mut input = quoter();
    input.snapshot.orders = (0..1024)
        .map(|index| {
            let mut order = order(format!("eng-resting-{index}"));
            order.symbol = SymbolId(1);
            order
        })
        .collect();
    let mut sibling = input.snapshot.symbols[0].clone();
    sibling.id = SymbolId(1);
    sibling.name = "ETHUSDT".into();
    input.snapshot.symbols.push(sibling);
    input.snapshot.symbols[0].my_position = 0.5;
    let (_process, proposal) = worker().call(input, Duration::from_secs(10)).await.unwrap();
    assert_eq!(
        proposal
            .actions
            .iter()
            .filter(|action| matches!(action, Action::Cancel { .. }))
            .count(),
        1024
    );
    let exit = proposal.actions.iter().position(|action| matches!(action, Action::Place(intent) if intent.reduce_only && intent.qty == 0.5)).expect("retained exit");
    assert_eq!(
        exit, 1024,
        "the bounded process stream keeps the entire ordered suffix"
    );
}

#[tokio::test]
async fn an_overflow_after_emitted_cancels_aborts_the_entire_proposal() {
    let mut input = quoter();
    input.snapshot.orders = (0..15_000)
        .map(|index| order(format!("eng-{index}-{}", "x".repeat(300))))
        .collect();
    let result = worker().call(input, Duration::from_secs(30)).await;
    let error = match result {
        Ok(_) => panic!("oversized private state was committed"),
        Err(error) => error,
    };
    assert!(error.contains("state exceeds its byte limit"), "{error}");
}

#[tokio::test]
async fn a_stalled_process_is_killed_while_the_core_executor_keeps_running() {
    let mut command = Command::new("/bin/sleep");
    command.arg("60");
    let process = StrategyProcess::spawn_command(command).unwrap();
    let pid = process.id();
    let progress = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let observed = progress.clone();
    let independent = tokio::spawn(async move {
        for _ in 0..5 {
            tokio::time::sleep(Duration::from_millis(2)).await;
            observed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }
    });
    let result = process.call(probe(), Duration::from_millis(40)).await;
    assert!(result.is_err());
    independent.await.unwrap();
    assert_eq!(progress.load(std::sync::atomic::Ordering::SeqCst), 5);
    assert_eq!(
        unsafe { libc::kill(pid as i32, 0) },
        -1,
        "stalled child remains alive"
    );
}

#[tokio::test]
async fn cancellation_of_a_callback_kills_and_reaps_its_owned_process() {
    let mut command = Command::new("/bin/sleep");
    command.arg("60");
    let process = StrategyProcess::spawn_command(command).unwrap();
    let pid = process.id();
    assert!(tokio::time::timeout(
        Duration::from_millis(30),
        process.call(probe(), Duration::from_secs(60))
    )
    .await
    .is_err());
    assert_eq!(
        unsafe { libc::kill(pid as i32, 0) },
        -1,
        "cancelled callback orphaned its process"
    );
}

#[tokio::test]
async fn restarted_quoter_does_not_reuse_a_previous_engines_monotonic_cancel_deadline() {
    let mut input = quoter();
    input.snapshot.orders.push(order("eng-resting".into()));
    let mut payload: serde_json::Value = serde_json::from_slice(&input.state.payload).unwrap();
    payload["asked"] =
        serde_json::json!({"eng-resting": {"at_ns": 9_000_000_000_000_u64, "moved_to": null}});
    input.state.payload = serde_json::to_vec(&payload).unwrap();
    let (_, proposal) = worker().call(input, Duration::from_secs(10)).await.unwrap();
    assert_eq!(proposal.actions.iter().filter(|action| matches!(action, Action::Cancel { client_order_id, .. } if client_order_id == "eng-resting")).count(), 1);
}

#[tokio::test]
async fn a_descendant_cannot_hold_the_core_after_its_callback_times_out() {
    let mut command = Command::new("/bin/sh");
    command.arg("-c").arg("sleep 3 & wait");
    let process = StrategyProcess::spawn_command(command).unwrap();
    let started = std::time::Instant::now();
    assert!(process
        .call(probe(), Duration::from_millis(30))
        .await
        .is_err());
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "a descendant retained the callback stdout after its parent was killed: {:?}",
        started.elapsed()
    );
}

#[tokio::test]
async fn replayed_private_retry_state_preserves_exact_exit_order_and_bytes() {
    let mut input = quoter();
    let mut sibling = input.snapshot.symbols[0].clone();
    sibling.id = SymbolId(1);
    sibling.name = "ETHUSDT".into();
    sibling.my_position = 0.25;
    input.snapshot.symbols.push(sibling);
    input.snapshot.symbols[0].my_position = 0.5;
    input.event = CallbackEvent::Timer {
        id: engine_types::TimerId(1),
        now_ns: input.snapshot.now_ns,
    };
    let mut state: serde_json::Value = serde_json::from_slice(&input.state.payload).unwrap();
    state["flatten_retry"] = serde_json::json!([0, 1]);
    input.state.payload = serde_json::to_vec(&state).unwrap();
    let mut previous = None;
    for _ in 0..20 {
        let (_worker, proposal) = worker()
            .call(input.clone(), Duration::from_secs(10))
            .await
            .unwrap();
        let exits: Vec<_> = proposal
            .actions
            .iter()
            .filter_map(|action| match action {
                Action::Place(intent) if intent.reduce_only => Some(intent.symbol),
                _ => None,
            })
            .collect();
        assert_eq!(exits.len(), 2);
        let result = (proposal.actions, proposal.state);
        if let Some(previous) = &previous {
            assert!(&result == previous, "replaying the same prepared invocation changed its ordered effects or private state");
        }
        previous = Some(result);
    }
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn allocation_flood_is_stopped_by_the_os_while_the_core_keeps_running() {
    let mut command = Command::new("python3");
    command
        .arg("-c")
        .arg(include_str!("fixtures/strategy-allocation-flood.py"));
    let process = StrategyProcess::spawn_command(command).unwrap();
    let progress = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let observed = progress.clone();
    let independent = tokio::spawn(async move {
        for _ in 0..10 {
            tokio::time::sleep(Duration::from_millis(2)).await;
            observed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }
    });
    let began = std::time::Instant::now();
    let result = process.call(probe(), Duration::from_secs(10)).await;
    assert!(
        result.is_err(),
        "callback allocated beyond its hard address-space limit"
    );
    assert!(
        began.elapsed() < Duration::from_secs(5),
        "only the callback deadline stopped the allocator"
    );
    independent.await.unwrap();
    assert_eq!(progress.load(std::sync::atomic::Ordering::SeqCst), 10);
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn process_limits_allow_threads_but_deny_new_processes() {
    let mut command = Command::new("python3");
    command.arg("-c").arg(r#"
import errno, json, os, struct, sys, threading
source = sys.stdin.buffer
payload = bytearray()
while True:
    count = struct.unpack('<I', source.read(4))[0]
    if not count: break
    payload.extend(source.read(count))
request = json.loads(payload)
ran = []
thread = threading.Thread(target=lambda: ran.append(True))
thread.start()
thread.join()
assert ran == [True]
try:
    pid = os.fork()
except OSError as error:
    assert error.errno == errno.EPERM
else:
    if pid == 0: os._exit(0)
    os.waitpid(pid, 0)
    raise RuntimeError('callback escaped its process owner')
reply = json.dumps({'kind':'finished','callback_id':request['callback_id'],'state':request['state']}).encode()
sys.stdout.buffer.write(struct.pack('<I',len(reply)) + reply + bytes(4))
sys.stdout.buffer.flush()
"#);
    let (_process, proposal) = StrategyProcess::spawn_command(command)
        .unwrap()
        .call(probe(), Duration::from_secs(5))
        .await
        .unwrap();
    assert!(proposal.actions.is_empty());
}

struct ExactBenchVenue(engine_core::bench::HttpVenue);

#[engine_types::async_trait]
impl engine_types::VenueGateway for ExactBenchVenue {
    fn caps(&self) -> engine_types::VenueCaps {
        self.0.caps()
    }
    async fn account_identity(
        &mut self,
    ) -> Result<engine_types::AccountIdentity, engine_types::VenueError> {
        self.0.account_identity().await
    }
    async fn send_order(
        &mut self,
        request: &engine_types::OrderRequest,
    ) -> Result<engine_types::OrderAck, engine_types::VenueError> {
        self.0.send_order(request).await
    }
    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        id: &str,
    ) -> Result<(), engine_types::VenueError> {
        self.0.cancel_order(symbol, id).await
    }
    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        id: &str,
        spec: engine_types::AmendSpec,
    ) -> Result<(), engine_types::VenueError> {
        self.0.amend_order(symbol, id, spec).await
    }
    async fn set_stop(
        &mut self,
        symbol: SymbolId,
        px: f64,
    ) -> Result<(), engine_types::VenueError> {
        self.0.set_stop(symbol, px).await
    }
    async fn account_view(
        &mut self,
    ) -> Result<engine_types::AccountView, engine_types::VenueError> {
        self.0.account_view().await
    }
    async fn instrument_rules(
        &mut self,
    ) -> Result<Vec<(String, InstrumentRule)>, engine_types::VenueError> {
        self.0.instrument_rules().await
    }
    async fn working_orders(
        &mut self,
    ) -> Result<Vec<engine_types::VenueOrder>, engine_types::VenueError> {
        self.0.working_orders().await
    }
    async fn executions(
        &mut self,
        start: i64,
        end: i64,
    ) -> Result<Vec<engine_types::VenueExecution>, engine_types::VenueError> {
        self.0.executions(start, end).await
    }
    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(String, engine_types::numeric::ExactInstrumentSpec)>, engine_types::VenueError>
    {
        use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
        let decimal = |text: &str| text.parse::<Exact>().unwrap();
        Ok(vec![(
            "BTCUSDT".into(),
            ExactInstrumentSpec {
                native_symbol: "BTCUSDT".into(),
                base_asset: AssetId::Named("BTC".into()),
                quote_asset: AssetId::Named("USDT".into()),
                settlement_asset: AssetId::Named("USDT".into()),
                tick_size: Some(decimal("0.5")),
                min_price: None,
                max_price: None,
                price_precision: PricePrecision::Tick,
                qty_step: Some(decimal("0.001")),
                min_qty: Some(decimal("0.001")),
                market_qty_step: Some(decimal("0.001")),
                market_min_qty: Some(decimal("0.001")),
                max_qty: None,
                max_market_qty: None,
                min_notional: Some(decimal("5")),
                contract_multiplier: Some(Exact::one()),
                fee_assets: Some(vec![AssetId::Named("USDT".into())]),
                fee_step: None,
            },
        )])
    }
}

struct PendingMarket;
impl engine_types::MarketFeed for PendingMarket {
    async fn next_event(&mut self) -> Result<engine_types::MarketEvent, engine_types::FeedError> {
        std::future::pending().await
    }
}

async fn wait_for_commits(path: std::path::PathBuf, expected: usize) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        let bytes = std::fs::read(&path).unwrap_or_default();
        let needle = b"strategy_process_transition_queued";
        if bytes
            .windows(needle.len())
            .filter(|window| *window == needle)
            .count()
            >= expected
        {
            return;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "registered callback did not commit before the test deadline"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test]
async fn registered_engine_callback_restarts_from_prepared_and_committed_cuts() {
    use engine_types::Wal;
    let root = std::env::temp_dir().join(format!(
        "strategy-process-engine-{}-{}",
        std::process::id(),
        engine_types::clock::mono_ns()
    ));
    std::fs::create_dir_all(&root).unwrap();
    let address = engine_core::bench::start_mock_venue().unwrap();
    let mut previous = Vec::new();
    for pass in 0..3 {
        let path = root.join(format!("{pass}.wal"));
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        let replayed = if pass == 0 {
            Vec::new()
        } else {
            let cut = previous
                .iter()
                .position(|record| {
                    if pass == 1 {
                        matches!(
                            record,
                            engine_types::WalRecord::StrategyCallbackPrepared { .. }
                        )
                    } else {
                        matches!(
                            record,
                            engine_types::WalRecord::StrategyProcessTransitionQueued { .. }
                        )
                    }
                })
                .unwrap();
            previous[..=cut].to_vec()
        };
        for record in &replayed {
            wal.append(record).unwrap();
        }
        wal.barrier().unwrap();
        let settings: engine_core::config::EngineSection = toml::from_str(&format!(
            "wal_path = {:?}\ngroup_flush_ms = 5\nwal_rotate_mb = 0",
            path.to_str().unwrap()
        ))
        .unwrap();
        let strategy = engine_strategies::runtime::restore(&probe().state).unwrap();
        let mut engine = engine_core::engine::Engine::boot_as_isolated(
            &settings,
            "process-regression",
            wal,
            engine_core::bench::AllowEverything,
            ExactBenchVenue(engine_core::bench::HttpVenue::new(
                address,
                vec!["BTCUSDT".into()],
            )),
            vec![strategy],
            &["probe".into()],
            &replayed,
            env!("CARGO_BIN_EXE_engine").into(),
        )
        .await
        .unwrap();
        let expected = if pass == 0 { 1 } else { 2 };
        engine
            .run(
                &mut PendingMarket,
                &mut engine_core::bench::SilentOrderFeed,
                wait_for_commits(path.clone(), expected),
            )
            .await
            .unwrap();
        drop(engine);
        let (_wal, records) = engine_wal::WalWriter::open(&path).unwrap();
        let records: Vec<_> = records.into_iter().map(|(_, record)| record).collect();
        let callbacks =
            engine_core::strategy_process::state::CallbackState::replay(&records, 1).unwrap();
        assert!(
            callbacks.inputs.is_empty(),
            "a committed callback was queued again"
        );
        assert_eq!(
            callbacks.committed[&StrategyId(0)].timers.len(),
            1,
            "restart duplicated a one-shot timer owner"
        );
        assert_eq!(
            records
                .iter()
                .filter(|record| matches!(
                    record,
                    engine_types::WalRecord::StrategyProcessTransitionQueued { .. }
                ))
                .count(),
            expected
        );
        if pass == 0 {
            previous = records;
        }
    }
    std::fs::remove_dir_all(root).unwrap();
}
