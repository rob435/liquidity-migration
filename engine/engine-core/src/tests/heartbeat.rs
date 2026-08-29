//! The heartbeat file.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

// The heartbeat file
// ---------------------------------------------------------------------------

/// Every log line written while this is installed, as "target: message".
/// `tracing` keeps no reader of its own, and one of the things worth proving
/// about the heartbeat is that an engine with none configured says nothing at
/// all — which needs a reader that would have heard it.
#[derive(Clone, Default)]
struct Heard(Arc<Mutex<Vec<String>>>);

impl Heard {
    fn lines(&self) -> Vec<String> {
        self.0.lock().expect("nothing panicked while holding this").clone()
    }

    fn about_the_heartbeat(&self) -> Vec<String> {
        self.lines()
            .into_iter()
            .filter(|line| line.contains("heartbeat"))
            .collect()
    }
}

impl tracing::Subscriber for Heard {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }

    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }

    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}

    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}

    fn event(&self, event: &tracing::Event<'_>) {
        let mut said = Said(String::new());
        event.record(&mut said);
        self.0
            .lock()
            .expect("nothing panicked while holding this")
            .push(format!("{}: {}", event.metadata().target(), said.0));
    }

    fn enter(&self, _: &tracing::span::Id) {}

    fn exit(&self, _: &tracing::span::Id) {}
}

/// The words of one log line. Everything else about the event is dropped.
struct Said(String);

impl tracing::field::Visit for Said {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            self.0 = format!("{value:?}");
        }
    }
}

/// A tick every few milliseconds, so a test does not sit through the shipping
/// quarter-second.
fn quick_tick() -> EngineSection {
    let mut settings = settings();
    settings.group_flush_ms = 5;
    settings
}

/// A heartbeat that beats on every tick, for the same reason.
fn every_tick(path: &std::path::Path) -> Heartbeat {
    Heartbeat::with_every(
        path.to_path_buf(),
        Some(AccountIdentity {
            venue: "bybit".into(),
            user_id: "6039967".into(),
            realm: "demo".into(),
        }),
        Some(std::path::PathBuf::from(
            "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock",
        )),
        Duration::from_millis(1),
    )
}

fn heartbeat_at(path: &std::path::Path) -> serde_json::Map<String, serde_json::Value> {
    let raw = std::fs::read_to_string(path).expect("a heartbeat was written");
    serde_json::from_str::<serde_json::Value>(&raw)
        .unwrap_or_else(|e| panic!("the heartbeat is not JSON ({e}): {raw}"))
        .as_object()
        .expect("the heartbeat is an object")
        .clone()
}

struct BlockedStrategy {
    rows: Vec<(String, String)>,
}

impl Strategy for BlockedStrategy {
    fn name(&self) -> &str {
        "blocked_test"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }

    fn entry_blockers(&self) -> Vec<(String, String)> {
        self.rows.clone()
    }
}

#[test]
fn blockers_are_deduplicated_per_configured_strategy_and_symbol() {
    let strategies: Vec<Box<dyn Strategy>> = vec![
        Box::new(BlockedStrategy {
            rows: vec![
                ("BTCUSDT".to_string(), "kernel_refusal".to_string()),
                ("BTCUSDT".to_string(), "planner_skip".to_string()),
            ],
        }),
        Box::new(BlockedStrategy {
            rows: vec![("BTCUSDT".to_string(), "carry_skip".to_string())],
        }),
    ];
    let configured = vec!["target_book_long".to_string(), "target_book_carry".to_string()];

    let rows = crate::engine::named_entry_blockers(&strategies, &configured);

    assert_eq!(
        rows,
        vec![
            (
                "target_book_carry".to_string(),
                "BTCUSDT".to_string(),
                "carry_skip".to_string(),
            ),
            (
                "target_book_long".to_string(),
                "BTCUSDT".to_string(),
                "kernel_refusal".to_string(),
            ),
        ]
    );
}

#[tokio::test]
async fn a_running_engine_leaves_a_heartbeat_saying_how_it_is() {
    let path = temp_path("heartbeat-running");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with(
        &quick_tick(),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    engine.write_heartbeat(every_tick(path.path()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 2, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let fields = heartbeat_at(path.path());
    assert_eq!(fields["mode"], "live", "this run was sending orders");
    assert_eq!(fields["may_open"].as_bool(), Some(true));
    assert_eq!(fields["account_user_id"], "6039967");
    assert_eq!(fields["realm"], "demo");
    assert_eq!(
        fields["lease_path"],
        "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock"
    );
    assert_eq!(fields["strategies"][0], "buyer");
    assert_eq!(fields["market_events"], 2, "both quotes were seen");
    assert_eq!(
        fields["orders_sent"].as_u64(),
        Some(h.sends.lock().unwrap().len() as u64),
        "the count in the file is the count that went out"
    );
    // An outside reader has no access to the engine's monotonic clock, so the
    // stamp has to be a wall clock it can age against its own. Spelled the
    // way the log's own Boot and Reconciled records spell it.
    let wall_ts_ms = fields["wall_ts_ms"].as_i64().expect("a wall-clock stamp");
    let age_ms = crate::clock::wall_ms() - wall_ts_ms;
    assert!((0..60_000).contains(&age_ms), "the stamp is not a live wall clock: {age_ms}ms old");
}


#[tokio::test]
async fn an_engine_latched_out_of_opening_says_so_in_its_heartbeat() {
    // The field the whole file is for. This engine is running, reading the
    // market, and refusing every entry; from the outside it is indistinguish-
    // able from a healthy one unless it says this.
    let path = temp_path("heartbeat-latched");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with(
        &quick_tick(),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        vec![someone_elses_order("BTCUSDT")],
    )
    .await;
    engine.write_heartbeat(every_tick(path.path()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    assert!(h.sends.lock().unwrap().is_empty(), "it is latched, so nothing went out");
    let fields = heartbeat_at(path.path());
    assert_eq!(fields["may_open"].as_bool(), Some(false));
    assert!(
        fields["market_events"].as_u64().unwrap_or(0) > 0,
        "and it looks alive, which is the point: {fields:?}"
    );
}

#[tokio::test]
async fn a_heartbeat_that_cannot_be_written_does_not_stop_the_engine() {
    // Telemetry. The loop does not depend on it, and a full disk or a wrong
    // path must not be the reason an order does not go out.
    let nowhere = temp_path("heartbeat-unwritable");
    let path = nowhere.path().join("no-such-directory").join("beat.json");
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with(
        &quick_tick(),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    engine.write_heartbeat(every_tick(&path));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    let heard = Heard::default();
    let guard = tracing::subscriber::set_default(heard.clone());
    let outcome = engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .expect("a heartbeat it cannot write is not a reason to stop");
    drop(guard);

    assert_eq!(outcome.market_events, 1, "the loop kept running");
    assert_eq!(h.sends.lock().unwrap().len(), 1, "and the order still went out");
    assert!(!path.exists(), "nothing was written");
    // Said once, not on every one of the several ticks in that window: this
    // path is wrong every time it is tried.
    let complaints = heard.about_the_heartbeat();
    assert_eq!(complaints.len(), 1, "{complaints:?}");
    assert!(complaints[0].contains("cannot write the heartbeat"), "{complaints:?}");
}

#[tokio::test]
async fn no_heartbeat_configured_writes_nothing_and_says_nothing() {
    // Silence has to be genuine silence. A warning every few seconds about a
    // file nobody asked for would be noise in every log the fleet keeps, and
    // the test above proves these ears can hear a heartbeat complaining.
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, _h) = build_with(
        &quick_tick(),
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    let heard = Heard::default();
    let guard = tracing::subscriber::set_default(heard.clone());
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();
    drop(guard);

    let said = heard.about_the_heartbeat();
    assert!(said.is_empty(), "an engine with no heartbeat still talked about one: {said:?}");
}

#[test]
fn no_configured_path_means_no_heartbeat_writer_at_all() {
    // The other half of staying quiet: nothing is built, so nothing can be
    // said and no file can appear.
    let mut named = settings();
    assert!(crate::assembly::heartbeat(&named, None, None).is_none());
    named.heartbeat_path = Some("var/engine-heartbeat.json".into());
    let built = crate::assembly::heartbeat(&named, None, None).expect("a path means a heartbeat");
    assert_eq!(built.path(), std::path::Path::new("var/engine-heartbeat.json"));
}
