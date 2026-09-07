#[path = "legacy.rs"]
mod legacy;
use super::*;
use crate::testpath::temp_path;

/// Every key the file carries, in the order it must read in.
const KEYS: [&str; 66] = [
    "account_available_usdt",
    "account_equity_usdt",
    "account_observed_wall_ts_ms",
    "account_user_id",
    "ack_p50_ns",
    "ack_p999_ns",
    "ack_p99_ns",
    "amends_confirmed",
    "amends_pulled_unconfirmed",
    "barrier_wait_p999_ns",
    "barrier_wait_p99_ns",
    "core_resume_p50_ns",
    "core_resume_p999_ns",
    "core_resume_p99_ns",
    "decide_p50_ns",
    "decide_p999_ns",
    "decide_p99_ns",
    "dispatch_queue_p50_ns",
    "dispatch_queue_p999_ns",
    "dispatch_queue_p99_ns",
    "durable_p50_ns",
    "durable_p999_ns",
    "durable_p99_ns",
    "end_to_end_p50_ns",
    "end_to_end_p999_ns",
    "end_to_end_p99_ns",
    "engine_commit",
    "engine_version",
    "entry_blockers",
    "fill_all_in_arrival_bps",
    "fill_arrival_shortfall_bps",
    "fill_fee_coverage",
    "fill_markout_1m_our_way_bps",
    "fills",
    "fills_maker_share",
    "lease_path",
    "market_events",
    "may_open",
    "mode",
    "orders_sent",
    "pending_flatten_requests",
    "pid",
    "positions",
    "quota_hold_p999_ns",
    "quota_hold_p99_ns",
    "realm",
    "rolling_loss_limit_usdt",
    "rolling_loss_net_usdt",
    "rolling_loss_trades",
    "rolling_loss_tripped",
    "rolling_loss_window_ms",
    "strategies",
    "strategy_entries_enabled",
    "strategy_errors",
    "stream_resets",
    "uptime_s",
    "venue",
    "venue_clock_offset_ms",
    "venue_task_p50_ns",
    "venue_task_p999_ns",
    "venue_task_p99_ns",
    "wall_ts_ms",
    "wire_p50_ns",
    "wire_p999_ns",
    "wire_p99_ns",
    "working_entries",
];

fn measured(count: u64, p50_ns: u64, p99_ns: u64) -> Quantiles {
    Quantiles {
        count,
        p50_ns,
        p90_ns: p50_ns,
        p99_ns,
        p999_ns: p99_ns,
        max_ns: p99_ns,
    }
}

/// An engine that has traded a little: one fill, and it cost something.
pub(super) fn some_costs() -> Costs {
    let mut fills = crate::execution::Fills::default();
    fills.on_fill(
        &crate::execution::Fill {
            amounts: None,
            client_order_id: "eng-1".into(),
            strategy: engine_types::StrategyId(0),
            symbol: engine_types::SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            px: 101.0,
            fee: Some(0.0555),
            is_maker: true,
            arrival_mid: 100.0,
            venue_ts_ms: 1,
        },
        0,
    );
    fills.total()
}

fn facts<'a>(
    strategies: &'a [String],
    held: &'a [(String, Side, f64, f64, Option<String>)],
) -> Facts<'a> {
    // An engine that has not filled anything, which is what every test
    // that is not about fill costs means.
    static NOTHING_YET: std::sync::OnceLock<Costs> = std::sync::OnceLock::new();
    static NO_BLOCKERS: std::sync::OnceLock<Vec<(String, String, String)>> =
        std::sync::OnceLock::new();
    static NO_STRATEGY_ERRORS: std::sync::OnceLock<Vec<(String, String)>> =
        std::sync::OnceLock::new();
    static NO_WORKING: std::sync::OnceLock<Vec<(String, String)>> = std::sync::OnceLock::new();
    static ALL_ENABLED: std::sync::OnceLock<Vec<(String, bool)>> = std::sync::OnceLock::new();
    Facts {
        costs: NOTHING_YET.get_or_init(Costs::default),
        may_open: true,
        market_events: 1234,
        orders_sent: 7,
        strategies,
        strategy_entries_enabled: ALL_ENABLED
            .get_or_init(|| strategies.iter().map(|name| (name.clone(), true)).collect()),
        pending_flatten_requests: &[],
        decide: measured(7, 83, 400),
        durable: measured(7, 10_000, 20_000),
        wire: measured(7, 2_600_000, 4_100_000),
        ack: measured(7, 2_500_000, 4_000_000),
        dispatch_queue: measured(7, 1_000, 2_000),
        venue_task: measured(7, 2_550_000, 4_050_000),
        core_resume: measured(7, 2_000, 3_000),
        end_to_end: measured(7, 2_700_000, 4_200_000),
        barrier_wait: measured(7, 1_000, 1_600_000),
        quota_hold: measured(7, 1, 90_000_000),
        amends_confirmed: 4,
        amends_pulled_unconfirmed: 1,
        stream_resets: 2,
        uptime_s: 7_460,
        venue_clock_offset_ms: Some(-12),
        equity_usdt: 10_250.5,
        available_usdt: 4_100.25,
        // Two seconds old, on the engine's own monotonic clock.
        account_age_ns: Some(2_000_000_000),
        holdings: held,
        entry_blockers: NO_BLOCKERS.get_or_init(Vec::new),
        strategy_errors: NO_STRATEGY_ERRORS.get_or_init(Vec::new),
        working_entries: NO_WORKING.get_or_init(Vec::new),
        rolling_loss: None,
    }
}

#[test]
fn p999_reaches_the_heartbeat_without_turning_absence_into_zero() {
    let strategies = vec!["long".to_string()];
    let held = Vec::new();
    let mut facts = facts(&strategies, &held);
    let stages = [
        ("decide", &mut facts.decide),
        ("durable", &mut facts.durable),
        ("wire", &mut facts.wire),
        ("ack", &mut facts.ack),
        ("dispatch_queue", &mut facts.dispatch_queue),
        ("venue_task", &mut facts.venue_task),
        ("core_resume", &mut facts.core_resume),
        ("end_to_end", &mut facts.end_to_end),
        ("barrier_wait", &mut facts.barrier_wait),
        ("quota_hold", &mut facts.quota_hold),
    ];
    let expected: Vec<_> = stages
        .into_iter()
        .map(|(name, q)| {
            q.p999_ns = q.p99_ns + 10;
            q.max_ns = q.p999_ns;
            (format!("{name}_p999_ns"), q.p999_ns)
        })
        .collect();
    let heartbeat = Heartbeat::new("unused.json".into(), None, None);
    let measured = parsed(&heartbeat.render(&facts, 1));
    for (key, ns) in &expected {
        assert_eq!(measured[key].as_u64(), Some(*ns), "{key}");
    }
    for q in [
        &mut facts.decide,
        &mut facts.durable,
        &mut facts.wire,
        &mut facts.ack,
        &mut facts.dispatch_queue,
        &mut facts.venue_task,
        &mut facts.core_resume,
        &mut facts.end_to_end,
        &mut facts.barrier_wait,
        &mut facts.quota_hold,
    ] {
        q.count = 0;
    }
    let empty = parsed(&heartbeat.render(&facts, 2));
    for (key, _) in &expected {
        assert_eq!(empty.get(key), Some(&serde_json::Value::Null), "{key}");
    }
    facts.decide.count = 1;
    facts.decide.p999_ns = 0;
    let zero = parsed(&heartbeat.render(&facts, 3));
    assert_eq!(zero["decide_p999_ns"], 0);
}

#[test]
fn the_execution_health_numbers_reach_the_beat() {
    // The watchdog's daily digest is built from these exact keys. A
    // missing one degrades silently there — the digest prints a dash —
    // so their presence is pinned here, where they are written.
    let strategies = vec!["maker_canary".to_string()];
    let held = Vec::new();
    let beat = Heartbeat::new(std::env::temp_dir().join("beat-health.json"), None, None)
        .render(&facts(&strategies, &held), 1_756_500_000_000);
    let parsed: serde_json::Value = serde_json::from_str(&beat).expect("one line of JSON");
    assert_eq!(parsed["amends_confirmed"], 4);
    assert_eq!(parsed["amends_pulled_unconfirmed"], 1);
    assert_eq!(parsed["stream_resets"], 2);
    assert_eq!(parsed["uptime_s"], 7_460);
    assert_eq!(parsed["venue_clock_offset_ms"], -12);
    assert_eq!(parsed["quota_hold_p99_ns"], 90_000_000);
    assert_eq!(parsed["barrier_wait_p99_ns"], 1_600_000);
}

#[test]
fn pending_flatten_acknowledgements_reach_the_beat() {
    let strategies = vec!["long".to_string(), "carry".to_string()];
    let held = Vec::new();
    let pending = vec![("carry".to_string(), "flatten-carry-42".to_string())];
    let mut facts = facts(&strategies, &held);
    facts.pending_flatten_requests = &pending;
    let beat = Heartbeat::new(std::env::temp_dir().join("beat-controls.json"), None, None)
        .render(&facts, 1_756_500_000_000);
    let parsed: serde_json::Value = serde_json::from_str(&beat).expect("one line of JSON");
    assert_eq!(
        parsed["pending_flatten_requests"],
        serde_json::json!([{
            "strategy": "carry",
            "request_id": "flatten-carry-42",
        }])
    );
}

/// One position, so the shape of the array is exercised rather than only
/// the empty case.
fn one_holding() -> Vec<(String, Side, f64, f64, Option<String>)> {
    vec![(
        "HOMEUSDT".to_string(),
        Side::Buy,
        14_110.0,
        0.009_7,
        Some("long".to_string()),
    )]
}

#[test]
fn the_account_reading_is_stamped_on_the_wall_clock_beside_its_own() {
    // The fault this exists for, found on a live deploy: the engine's
    // clock is monotonic and starts near boot, so the raw `observed_ns`
    // was a few seconds. A producer comparing it against the wall clock
    // read a healthy engine as fifty-six thousand years stale and blocked
    // every entry, quietly, per cycle.
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string()];
    let held = one_holding();
    let wall_ts_ms = 1_786_737_867_645_i64;

    let fields = parsed(&beat.render(&facts(&names, &held), wall_ts_ms));

    let observed = fields["account_observed_wall_ts_ms"]
        .as_i64()
        .expect("a whole number of milliseconds");
    assert_eq!(observed, wall_ts_ms - 2_000, "two seconds before this beat");
    assert!(
        observed > 1_700_000_000_000,
        "a unix millisecond stamp, not a count since boot: {observed}"
    );
}

#[test]
fn what_is_held_is_published_by_name_for_observers_to_read() {
    // Publish the account reading by symbol so operators can distinguish
    // a requested entry from a position the venue actually holds.
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string()];
    let held = one_holding();

    let fields = parsed(&beat.render(&facts(&names, &held), 1_755_000_000_000));

    let rows = fields["positions"].as_array().expect("an array");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["symbol"], "HOMEUSDT", "by name, not by engine id");
    assert_eq!(rows[0]["side"], "long");
    assert_eq!(rows[0]["qty"].as_f64(), Some(14_110.0));
    assert!(rows[0]["entry_px"].as_f64().is_some_and(|px| px > 0.0));
    assert_eq!(rows[0]["strategy"], "long");
}

#[test]
fn an_unattributed_account_position_does_not_guess_a_strategy() {
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string(), "carry".to_string()];
    let held = vec![("HOMEUSDT".to_string(), Side::Buy, 14_110.0, 0.009_7, None)];

    let fields = parsed(&beat.render(&facts(&names, &held), 1_755_000_000_000));

    assert!(fields["positions"][0]["strategy"].is_null());
}

#[test]
fn holding_nothing_is_an_empty_array_and_not_a_missing_key() {
    // An empty account reading differs from a missing field in consumers.
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string()];

    let fields = parsed(&beat.render(&facts(&names, &[]), 1_755_000_000_000));

    assert_eq!(
        fields["positions"].as_array().map(Vec::len),
        Some(0),
        "present and empty, never absent"
    );
}

#[test]
fn a_blocked_entry_is_published_with_its_reason() {
    // A refused entry never becomes a position. The heartbeat carries the
    // kernel's reason so it differs from an order still in flight.
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string()];
    let held = one_holding();
    let blockers = vec![
        (
            "long".to_string(),
            "KAITOUSDT".to_string(),
            "below_entry_floor".to_string(),
        ),
        (
            "long".to_string(),
            "SOMIUSDT".to_string(),
            "AvailableMarginExhausted { additional_margin_usdt: 12.0, available_usdt: 0.5 }"
                .to_string(),
        ),
    ];
    let mut facts = facts(&names, &held);
    facts.entry_blockers = &blockers;

    let fields = parsed(&beat.render(&facts, 1_755_000_000_000));

    let rows = fields["entry_blockers"].as_array().expect("an array");
    assert_eq!(rows.len(), 2, "one reason per blocked name");
    assert_eq!(rows[0]["strategy"], "long");
    assert_eq!(rows[0]["symbol"], "KAITOUSDT");
    assert_eq!(rows[0]["reason"], "below_entry_floor");
    assert_eq!(rows[1]["symbol"], "SOMIUSDT");
    assert!(
        rows[1]["reason"]
            .as_str()
            .unwrap_or("")
            .contains("AvailableMargin"),
        "the kernel's own reason text crosses: {}",
        rows[1]["reason"]
    );
}

#[test]
fn same_symbol_blockers_keep_their_strategy_identity() {
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string(), "carry".to_string()];
    let held = one_holding();
    let blockers = vec![
        (
            "long".to_string(),
            "KAITOUSDT".to_string(),
            "below_entry_floor".to_string(),
        ),
        (
            "carry".to_string(),
            "KAITOUSDT".to_string(),
            "available_margin_exhausted".to_string(),
        ),
    ];
    let mut facts = facts(&names, &held);
    facts.entry_blockers = &blockers;

    let fields = parsed(&beat.render(&facts, 1_755_000_000_000));
    let rows = fields["entry_blockers"].as_array().expect("an array");

    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["symbol"], "KAITOUSDT");
    assert_eq!(rows[1]["symbol"], "KAITOUSDT");
    assert_eq!(rows[0]["strategy"], "long");
    assert_eq!(rows[1]["strategy"], "carry");
}

#[test]
fn nothing_blocked_is_an_empty_array_and_not_a_missing_key() {
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["long".to_string()];

    let fields = parsed(&beat.render(&facts(&names, &[]), 1_755_000_000_000));

    assert_eq!(
        fields["entry_blockers"].as_array().map(Vec::len),
        Some(0),
        "present and empty, never absent"
    );
}

#[test]
fn unfinished_entries_keep_their_strategy_and_symbol() {
    let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
    let names = vec!["exodus".to_string()];
    let pending = vec![("exodus".to_string(), "DYNAMICUSDT".to_string())];
    let mut facts = facts(&names, &[]);
    facts.working_entries = &pending;

    let fields = parsed(&beat.render(&facts, 1_755_000_000_000));

    assert_eq!(fields["working_entries"][0]["strategy"], "exodus");
    assert_eq!(fields["working_entries"][0]["symbol"], "DYNAMICUSDT");
}

fn on_the_demo_account(path: PathBuf) -> Heartbeat {
    Heartbeat::new(
        path,
        Some(AccountIdentity {
            venue: "bybit".into(),
            user_id: "6039967".into(),
            realm: "demo".into(),
        }),
        Some(PathBuf::from(
            "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock",
        )),
    )
}

fn parsed(raw: &str) -> serde_json::Map<String, serde_json::Value> {
    serde_json::from_str::<serde_json::Value>(raw)
        .unwrap_or_else(|e| panic!("the heartbeat is not JSON ({e}): {raw}"))
        .as_object()
        .expect("the heartbeat is an object")
        .clone()
}

#[test]
fn the_heartbeat_says_who_and_what_this_engine_is() {
    let names = vec!["long".to_string(), "carry".to_string()];
    let held = one_holding();
    let raw =
        on_the_demo_account("unused.json".into()).render(&facts(&names, &held), 1_755_000_000_000);

    assert!(
        raw.ends_with('\n'),
        "a newline after it, like the lease note: {raw:?}"
    );
    assert_eq!(raw.lines().count(), 1, "one line: {raw:?}");
    let fields = parsed(&raw);

    let mut keys: Vec<&str> = fields.keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(keys, KEYS, "the file carries exactly these");

    assert_eq!(fields["account_user_id"], "6039967");
    assert_eq!(fields["realm"], "demo");
    assert_eq!(
        fields["venue"], "bybit",
        "with four venues a heartbeat has to say which one it is on"
    );
    assert_eq!(
        fields["lease_path"],
        "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock"
    );
    assert_eq!(fields["may_open"].as_bool(), Some(true));
    assert_eq!(fields["market_events"], 1234);
    assert_eq!(fields["orders_sent"], 7);
    assert_eq!(fields["wall_ts_ms"], 1_755_000_000_000i64);
    assert_eq!(fields["pid"].as_u64(), Some(u64::from(std::process::id())));
    assert_eq!(fields["engine_version"], ENGINE_VERSION);
    assert_eq!(fields["engine_commit"], ENGINE_COMMIT);
    assert_eq!(fields["strategies"][0], "long");
    assert_eq!(fields["strategies"][1], "carry");
    assert_eq!(fields["decide_p50_ns"], 83);
    assert_eq!(fields["wire_p99_ns"], 4_100_000);
}

#[test]
fn the_rolling_loss_window_is_published_under_the_names_the_watchdog_reads() {
    // `scripts/runtime/check_fleet_liveness.py` reads these three by
    // name to tell the owner the engine has stopped opening.
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut facts = facts(&names, &held);
    facts.rolling_loss = Some(RollingLossView {
        window_ms: 86_400_000,
        trades: 3,
        net_usdt: -25_500.5,
        limit_usdt: 25_000.0,
        tripped: true,
    });

    let fields =
        parsed(&on_the_demo_account("unused.json".into()).render(&facts, 1_755_000_000_000));

    assert_eq!(fields["rolling_loss_window_ms"], 86_400_000i64);
    assert_eq!(fields["rolling_loss_trades"], 3);
    assert_eq!(fields["rolling_loss_net_usdt"].as_f64(), Some(-25_500.5));
    assert_eq!(fields["rolling_loss_limit_usdt"].as_f64(), Some(25_000.0));
    assert_eq!(fields["rolling_loss_tripped"].as_bool(), Some(true));
}

#[test]
fn an_empty_window_says_null_rather_than_a_net_of_zero() {
    // Nothing closed and nothing lost are different answers, and a zero
    // here would read as the second.
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut facts = facts(&names, &held);
    facts.rolling_loss = Some(RollingLossView {
        window_ms: 86_400_000,
        trades: 0,
        net_usdt: 0.0,
        limit_usdt: 25_000.0,
        tripped: false,
    });

    let fields =
        parsed(&on_the_demo_account("unused.json".into()).render(&facts, 1_755_000_000_000));

    assert_eq!(fields["rolling_loss_trades"], 0);
    assert!(fields["rolling_loss_net_usdt"].is_null());
    assert_eq!(fields["rolling_loss_limit_usdt"].as_f64(), Some(25_000.0));
    assert_eq!(fields["rolling_loss_tripped"].as_bool(), Some(false));
}

#[test]
fn a_kernel_that_keeps_no_window_says_null_and_is_not_tripped() {
    let names = vec!["long".to_string()];
    let held = one_holding();

    let fields = parsed(
        &on_the_demo_account("unused.json".into()).render(&facts(&names, &held), 1_755_000_000_000),
    );

    assert!(fields["rolling_loss_window_ms"].is_null());
    assert!(fields["rolling_loss_trades"].is_null());
    assert!(fields["rolling_loss_net_usdt"].is_null());
    assert!(fields["rolling_loss_limit_usdt"].is_null());
    assert_eq!(
        fields["rolling_loss_tripped"].as_bool(),
        Some(false),
        "whether trading is held back has no unknown state"
    );
}

#[test]
fn the_keys_read_in_order_in_the_file_itself() {
    // Stable key order keeps a diff focused on values that changed.
    let names = vec!["long".to_string()];
    let held = one_holding();
    let raw =
        on_the_demo_account("unused.json".into()).render(&facts(&names, &held), 1_755_000_000_000);
    let mut last = 0;
    for key in KEYS {
        let at = raw
            .find(&format!("\"{key}\": "))
            .unwrap_or_else(|| panic!("no {key} in {raw}"));
        assert!(at >= last, "{key} is out of order: {raw}");
        last = at;
    }
}

#[test]
fn it_always_says_live() {
    let names = vec!["long".to_string()];
    let held = one_holding();
    let beat = on_the_demo_account("unused.json".into());

    assert_eq!(
        parsed(&beat.render(&facts(&names, &held), 1))["mode"],
        "live"
    );
}

#[test]
fn an_engine_that_will_not_open_says_so() {
    // The reason this field is here at all: everything else about a
    // latched engine reads healthy.
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut latched = facts(&names, &held);
    latched.may_open = false;
    let fields = parsed(&on_the_demo_account("unused.json".into()).render(&latched, 1));
    assert_eq!(fields["may_open"].as_bool(), Some(false));
}

#[test]
fn a_latency_nobody_has_measured_is_null_and_not_zero() {
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut quiet = facts(&names, &held);
    quiet.decide = measured(0, 0, 0);
    quiet.wire = measured(0, 0, 0);
    let fields = parsed(&on_the_demo_account("unused.json".into()).render(&quiet, 1));
    for key in [
        "decide_p50_ns",
        "decide_p99_ns",
        "wire_p50_ns",
        "wire_p99_ns",
    ] {
        assert!(
            fields[key].is_null(),
            "{key} would read as instant: {fields:?}"
        );
    }
}

#[test]
fn what_this_run_does_not_know_is_null_rather_than_invented() {
    // A shadow run holds no lease, and one that cannot reach the venue
    // never learns the account number.
    let names = vec!["long".to_string()];
    let held = one_holding();
    let fields =
        parsed(&Heartbeat::new("unused.json".into(), None, None).render(&facts(&names, &held), 1));
    for key in ["account_user_id", "realm", "lease_path"] {
        assert!(fields[key].is_null(), "{key} was guessed at: {fields:?}");
    }
}

#[test]
fn each_heartbeat_replaces_the_file_instead_of_writing_over_it() {
    // The atomicity proof, and it does not need a race to catch it. A
    // second name for the file follows the bytes that were there when the
    // link was made. If the writer edited the file in place, the link
    // would show the new heartbeat; because it renames a fresh file into
    // place, the link keeps the old one — which is exactly why a reader
    // holding the file open never sees half of anything.
    let path = temp_path("heartbeat-replaced");
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut beat = on_the_demo_account(path.path().to_path_buf());

    let mut first = facts(&names, &held);
    first.market_events = 11;
    beat.write(1, &first);

    let link = temp_path("heartbeat-link");
    std::fs::hard_link(path.path(), link.path()).expect("a second name for the file");

    let mut second = facts(&names, &held);
    second.market_events = 22;
    beat.write(1, &second);

    let now = std::fs::read_to_string(path.path()).expect("the heartbeat is there");
    let then = std::fs::read_to_string(link.path()).expect("the old one is still there");
    assert_eq!(
        parsed(&now)["market_events"],
        22,
        "the newest heartbeat is at the path"
    );
    assert_eq!(
        parsed(&then)["market_events"],
        11,
        "the file was edited in place, so a reader can see half a heartbeat"
    );
}

#[test]
fn the_temp_file_sits_beside_the_heartbeat_and_does_not_stay() {
    // Beside it because a rename is one step only within one filesystem;
    // a temp file in /tmp would fail across a mount and there would be no
    // heartbeat at all.
    let path = temp_path("heartbeat-temp");
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut beat = on_the_demo_account(path.path().to_path_buf());
    assert_eq!(
        beat.temp_path().parent(),
        path.path().parent(),
        "the temp file has to be on the same filesystem"
    );
    beat.write(1, &facts(&names, &held));
    assert!(path.path().exists(), "the heartbeat landed");
    assert!(!beat.temp_path().exists(), "the temp file was left behind");
}

#[test]
fn a_heartbeat_that_cannot_be_written_leaves_no_file_and_no_half_file() {
    let missing = temp_path("heartbeat-nowhere");
    let path = missing.path().join("no-such-directory").join("beat.json");
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut beat = Heartbeat::new(path.clone(), None, None);
    beat.write(1, &facts(&names, &held));
    assert!(!path.exists(), "nothing should have been written");
    assert!(!beat.temp_path().exists(), "nor a temp file");
}

#[test]
fn systemd_liveness_follows_a_successfully_published_heartbeat() {
    use std::os::unix::net::UnixDatagram;
    if let Ok(case) = std::env::var("HEARTBEAT_NOTIFY_TEST_CASE") {
        let path = temp_path("heartbeat-notify-child");
        let output = if case == "valid" {
            path.path().to_path_buf()
        } else {
            path.path().join("missing").join("heartbeat.json")
        };
        let mut beat = Heartbeat::new(output, None, None);
        beat.write(1, &facts(&["long".to_string()], &[]));
        return;
    }
    for case in ["valid", "unwritable"] {
        let socket_path = temp_path("heartbeat-notify-socket");
        let receiver = UnixDatagram::bind(socket_path.path()).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "heartbeat::tests::systemd_liveness_follows_a_successfully_published_heartbeat",
            ])
            .env("NOTIFY_SOCKET", socket_path.path())
            .env("HEARTBEAT_NOTIFY_TEST_CASE", case)
            .env_remove("WATCHDOG_PID")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let mut bytes = [0; 64];
        if case == "valid" {
            let size = receiver
                .recv(&mut bytes)
                .expect("published heartbeat must notify systemd");
            assert_eq!(&bytes[..size], b"READY=1\nWATCHDOG=1");
        } else {
            assert_eq!(
                receiver.recv(&mut bytes).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
        }
    }
}

#[test]
fn the_cadence_holds_it_back_between_beats() {
    let path = temp_path("heartbeat-cadence");
    let mut beat = Heartbeat::with_every(
        path.path().to_path_buf(),
        None,
        None,
        Duration::from_secs(5),
    );
    let names = vec!["long".to_string()];
    let held = one_holding();

    // Due straight away: the first tick of a run writes one.
    assert!(beat.due(0));
    beat.write(1_000, &facts(&names, &held));
    assert!(
        !beat.due(1_000 + 4_999_999_999),
        "under five seconds is too soon"
    );
    assert!(
        beat.due(1_000 + 5_000_000_000),
        "five seconds on, it is due"
    );
}

#[test]
fn a_write_that_cannot_land_does_not_start_retrying_every_tick() {
    // The tick comes round four times a second. A wrong path is wrong
    // every one of them, and answering by trying again immediately would
    // put a failing file write in front of the account refresh.
    let missing = temp_path("heartbeat-no-retry");
    let path = missing.path().join("no-such-directory").join("beat.json");
    let names = vec!["long".to_string()];
    let held = one_holding();
    let mut beat = Heartbeat::with_every(path, None, None, Duration::from_secs(5));
    beat.write(1_000, &facts(&names, &held));
    assert!(
        !beat.due(1_000 + 4_999_999_999),
        "a failed write reset the cadence"
    );
}

#[test]
fn typed_output_matches_legacy_bytes_for_missing_extreme_and_escaped_values() {
    let names = vec!["long\"\\\n雪".to_owned(), "carry".to_owned()];
    let account = AccountIdentity {
        venue: "bybit".to_owned(),
        realm: "demo".to_owned(),
        user_id: "u\"\\\n雪".to_owned(),
    };
    let heartbeat = Heartbeat::new(
        "unused.json".into(),
        Some(account),
        Some("/a/\"b\\c".into()),
    );
    for value in [
        0.0,
        -0.0,
        1.0,
        1.23456789012345,
        1e-100,
        1e100,
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
    ] {
        let holdings = vec![(
            "BTC\"USDT".to_owned(),
            Side::Sell,
            value,
            value,
            Some(names[0].clone()),
        )];
        let blockers = vec![(names[0].clone(), "BTCUSDT".into(), "missing\nprice".into())];
        let errors = vec![(names[0].clone(), "rejected\n\\\"".into())];
        let permissions = vec![(names[0].clone(), false)];
        let flatten = vec![(names[0].clone(), "id-1".into())];
        let entries = vec![(names[1].clone(), "ETHUSDT".into())];
        let mut facts = facts(&names, &holdings);
        facts.available_usdt = value;
        facts.equity_usdt = value;
        facts.entry_blockers = &blockers;
        facts.strategy_errors = &errors;
        facts.strategy_entries_enabled = &permissions;
        facts.pending_flatten_requests = &flatten;
        facts.working_entries = &entries;
        for account_age in [None, Some(0), Some(123_000_000)] {
            facts.account_age_ns = account_age;
            assert_eq!(
                heartbeat.render(&facts, 123456),
                legacy::render(&heartbeat, &facts, 123456)
            );
        }
    }
}
