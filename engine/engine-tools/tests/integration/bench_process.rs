use engine_core::ledger::Segment;
use engine_tools::bench::{self, BenchOptions};
use engine_types::WalRecord;

struct TempPath(std::path::PathBuf);
impl TempPath {
    fn path(&self) -> &std::path::Path {
        &self.0
    }
}
impl Drop for TempPath {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
fn temp_path(name: &str) -> TempPath {
    TempPath(std::env::temp_dir().join(format!(
        "{name}-{}-{}.wal",
        std::process::id(),
        engine_types::clock::mono_ns()
    )))
}

fn run_parent(name: &str) -> bool {
    const CHILD: &str = "ENGINE_BENCH_PROCESS_CASE";
    if std::env::var(CHILD).as_deref() == Ok(name) {
        return false;
    }
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", &format!("bench_process::{name}"), "--nocapture"])
        .env(CHILD, name)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    true
}

fn run(options: &BenchOptions) -> bench::BenchResult {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(bench::run(options))
        .expect("bench")
}

#[test]
fn the_bench_runs_the_real_loop_and_fills_the_histograms() {
    if run_parent("the_bench_runs_the_real_loop_and_fills_the_histograms") {
        return;
    }
    let path = temp_path("bench-smoke");
    let options = BenchOptions {
        events: 100,
        rate: 100,
        every_nth: 1,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
        fills: false,
        venue_delay: std::time::Duration::ZERO,
        ..BenchOptions::default()
    };
    let result = run(&options);
    assert_eq!(result.events, 100);
    assert!(
        result.contention.is_none(),
        "the plain workload reports no contention section"
    );
    assert!(
        result.orders > 0 && result.orders <= 100,
        "ready embedded callback produces real measured orders: {}",
        result.orders
    );
    assert_eq!(result.callback_execution, "embedded");
    assert_eq!(
        result.orders,
        result
            .segments
            .iter()
            .find(|(segment, _)| *segment == Segment::Wire)
            .unwrap()
            .1
            .count
    );
    assert_eq!(
        result.orders + result.orders_not_submitted,
        result.order_opportunities
    );
    for (segment, q) in &result.segments {
        assert!(q.count > 0, "{segment:?} recorded nothing");
        assert!(q.max_ns >= q.p50_ns);
    }
    let report = engine_core::replay::read(path.path()).unwrap();
    assert!(!report.torn_tail);
    let (replayed, torn) = engine_wal::replay_scan(path.path()).unwrap();
    assert!(!torn);
    assert_eq!(report.records, replayed.len());
    assert!(replayed.iter().all(|(_, record)| !matches!(
        record,
        WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued { .. }
        ) | WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyCallbackQueued { .. }
        ) | WalRecord::Retained(
            engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared { .. }
        )
    )));
    let completed: Vec<_> = replayed
        .iter()
        .filter_map(|(_, record)| match record {
            WalRecord::VenueTiming {
                operation,
                client_order_id,
                ..
            } if operation == "place" => Some(client_order_id),
            _ => None,
        })
        .collect();
    assert_eq!(completed.len() as u64, result.orders);
    for id in completed {
        assert!(replayed.iter().any(|(_, record)| matches!(record, WalRecord::OrderSent { dispatch: Some(_), request, .. } if &request.client_order_id == id)));
        assert!(replayed.iter().any(|(_, record)| matches!(record, WalRecord::OrderDispatchAttempted { client_order_id } if client_order_id == id)));
    }
    assert!(result.table().contains("decision to dispatch ready"));
    assert_eq!(result.completed_latency_windows, 0);
    for kind in ["callback commit", "dispatch queued"] {
        assert!(result
            .barriers
            .iter()
            .all(|row| row.records != kind || row.confirmation.count == 0));
    }
    let row = result
        .barriers
        .iter()
        .find(|row| row.records == "attempted send + dispatch queued")
        .unwrap();
    assert_eq!(
        row.confirmation.count, result.orders,
        "one barrier per order"
    );
    assert_eq!(row.failures, 0);
    assert!(row.confirmation.max_ns >= row.request.p50_ns);
    assert!(result
        .as_json()
        .contains("\"callback_execution\":\"embedded\""));
}

#[test]
fn the_bench_can_fill_what_it_accepts_and_the_whole_cost_path_runs() {
    if run_parent("the_bench_can_fill_what_it_accepts_and_the_whole_cost_path_runs") {
        return;
    }
    // Every other test here drives one piece. This drives the loop: a real
    // engine, a real log with its fsync, orders that come back filled, and a
    // markout queue drained by the group-flush tick.
    //
    // Four seconds of real process time matures the one-second markout.
    let path = temp_path("bench-fills");
    let options = BenchOptions {
        events: 40,
        rate: 10,
        every_nth: 1,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
        fills: true,
        venue_delay: std::time::Duration::ZERO,
        ..BenchOptions::default()
    };
    let result = run(&options);
    assert!(result.orders > 0, "no orders, nothing to price");

    let (replayed, _torn) = engine_wal::replay_scan(path.path()).expect("the log reads back");
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    let costs = engine_core::execution::Fills::from_records(&records).total();

    // All but at most the last. The run ends when the market feed closes, and
    // a fill still in the channel at that moment is never read -- which is the
    // truthful shape of a process that stops, not something to pad over.
    assert!(
        costs.fills + 1 >= result.orders && costs.fills <= result.orders,
        "{} fills for {} orders",
        costs.fills,
        result.orders
    );
    assert!(costs.notional_usdt > 0.0);
    // The venue charges a flat two basis points, so this is arithmetic and not
    // a range: if it drifts, the fee is not reaching the ledger.
    let fee = costs.fee.mean().expect("the fee was priced");
    assert!((fee - 2.0).abs() < 0.01, "expected 2 bp of fee, got {fee}");
    // Priced against the book each order left at, which the log carries.
    let arrival = costs
        .arrival_shortfall
        .mean()
        .expect("the arrival was priced");
    assert!(
        arrival.abs() < 5.0,
        "half a tick on a 30,000 book, got {arrival}"
    );
    // Equal to the sum of the two means only because every fill here had both
    // halves; it is accumulated per fill, so the float arithmetic is not the
    // same order and the last bit differs.
    let all_in = costs.all_in_arrival_bps().expect("both halves, every fill");
    assert!(
        (all_in - (arrival + fee)).abs() < 1e-9,
        "{all_in} vs {}",
        arrival + fee
    );

    // And the part nothing else exercises: a horizon came round, the tick read
    // the book, and the mark went into the log.
    let marks = records
        .iter()
        .filter(|r| matches!(r, WalRecord::Markout { .. }))
        .count();
    assert!(
        marks > 0,
        "no markout came due in {} records",
        records.len()
    );
    assert!(
        costs.markout[0].mean().is_some(),
        "the one-second bucket is empty despite {marks} mark(s)"
    );
}

/// Answered pulls that end a contention run. The run is measured in cycles,
/// not seconds: a box that answers slowly takes longer and still gets here.
const PULLS: u64 = 12;

/// The `--contention` defaults, the 200 ms venue included, ended after
/// `PULLS` answered pulls; `events` is the ceiling a run that never cycles
/// hits. Four symbols, so the engine can have four placement commands
/// outstanding and the venue task answers one at a time.
fn contention_options(path: &std::path::Path, ttl_ms: u64) -> BenchOptions {
    BenchOptions {
        events: 20_000,
        wal_path: path.to_path_buf(),
        ttl_ms,
        pulls: Some(PULLS),
        ..BenchOptions::contention()
    }
}

/// How long each `cancel` in the log waited for the venue task.
fn cancel_waits(records: &[WalRecord]) -> Vec<u64> {
    records
        .iter()
        .filter_map(|record| match record {
            WalRecord::VenueTiming {
                operation,
                queued_ns,
                task_started_ns,
                ..
            } if operation == "cancel" => Some(task_started_ns.saturating_sub(*queued_ns)),
            _ => None,
        })
        .collect()
}

fn expired_openings(records: &[WalRecord]) -> u64 {
    records
        .iter()
        .filter(|record| {
            matches!(
                record,
                WalRecord::OrderUpdate {
                    update: engine_types::OrderUpdate::Reject { reason, .. },
                    ..
                } if reason.contains("authority: expired")
            )
        })
        .count() as u64
}

#[test]
fn a_cancel_behind_a_slow_opening_waits_for_the_gateway_call_in_flight() {
    if run_parent("a_cancel_behind_a_slow_opening_waits_for_the_gateway_call_in_flight") {
        return;
    }
    let path = temp_path("bench-contention");
    let options = contention_options(path.path(), 10_000);
    let result = run(&options);
    let contention = result.contention.clone().expect("the contention section");
    assert_eq!(contention.venue_delay_ms, 200);
    assert_eq!(contention.cancel_after, 3);
    assert_eq!(contention.symbols, 4);
    // The run ends on the last answered pull, whose own timing row can still
    // be on its way to the log; every pull follows an answered opening.
    assert!(
        contention.cancels_sent + 1 >= PULLS && contention.openings_sent >= contention.cancels_sent,
        "the workload did not cycle: {} openings, {} cancels",
        contention.openings_sent,
        contention.cancels_sent
    );
    let delay_ns = 200_000_000u64;
    // The precondition: openings really did pile up behind each other, so the
    // ceiling below was measured under contention and not on an idle task.
    assert!(
        contention.opening_queue_wait.max_ns * 2 > delay_ns * 3,
        "no opening waited for more than one venue call ({} ns)",
        contention.opening_queue_wait.max_ns
    );
    // And a cancel really did have to wait for a call in flight.
    assert!(
        contention.cancel_queue_wait.max_ns * 4 >= delay_ns * 3,
        "no cancel waited for a venue call ({} ns)",
        contention.cancel_queue_wait.max_ns
    );
    // The claim: risk-off waits for the one gateway call in flight and never
    // for the openings queued behind it. The engine holds a symbol busy until
    // its own command completes, so a cancel reaches the venue task only after
    // its order's placement was answered -- by which time the task has already
    // started another symbol's opening. That one call is the whole wait.
    assert!(
        contention.cancel_queue_wait.max_ns < delay_ns * 2,
        "a cancel waited {} ns, past the one call it should have waited for",
        contention.cancel_queue_wait.max_ns
    );
    assert!(
        contention.opening_queue_wait.max_ns * 2 > contention.cancel_queue_wait.max_ns * 3,
        "the worst opening wait {} ns is not half again the worst cancel wait {} ns",
        contention.opening_queue_wait.max_ns,
        contention.cancel_queue_wait.max_ns
    );
    // The pretend venue answers a pull at once; nothing else would let the
    // wait above be read as the queue rather than the call.
    assert!(
        contention.cancel_venue_span.p50_ns < delay_ns,
        "the pretend venue held a cancel for {} ns",
        contention.cancel_venue_span.p50_ns
    );
    assert!(result.table().contains("cancel wait for the task"));
    assert!(result.as_json().contains("\"never_sent_expired\":"));

    let (replayed, torn) = engine_wal::replay_scan(path.path()).expect("the log reads back");
    assert!(!torn);
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    assert_eq!(
        contention.never_sent_expired,
        expired_openings(&records),
        "the reported expiry count is not the log's"
    );
    assert_eq!(
        contention.never_sent_expired, 0,
        "a 10 s dispatch TTL expired an opening behind three 200 ms calls"
    );
    assert_eq!(
        contention.cancel_queue_wait.count as usize,
        cancel_waits(&records).len()
    );
}

#[test]
fn a_dispatch_ttl_under_the_queue_refuses_openings_unsent() {
    if run_parent("a_dispatch_ttl_under_the_queue_refuses_openings_unsent") {
        return;
    }
    // Three openings queue behind the one the venue is answering, so the
    // hindmost waits two to three venue calls -- 400 to 600 ms. A 300 ms TTL
    // is under that and over one call, so some expire and some still go.
    let path = temp_path("bench-contention-ttl");
    let options = contention_options(path.path(), 300);
    let result = run(&options);
    let contention = result.contention.expect("the contention section");
    assert_eq!(contention.ttl_ms, 300);
    assert!(
        contention.never_sent_expired > 0,
        "a 300 ms TTL expired nothing behind a 200 ms venue with three openings queued"
    );
    assert!(
        contention.openings_sent > 0,
        "a 300 ms TTL refused every opening"
    );
    let (replayed, _torn) = engine_wal::replay_scan(path.path()).expect("the log reads back");
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    assert_eq!(
        contention.never_sent_expired,
        expired_openings(&records),
        "the reported expiry count is not the log's"
    );
    assert_eq!(
        contention.openings_sent + contention.never_sent_expired,
        contention.opening_queue_wait.count,
        "every placement row is either answered or expired"
    );
}

#[test]
fn the_bench_fills_nothing_unless_it_is_asked_to() {
    if run_parent("the_bench_fills_nothing_unless_it_is_asked_to") {
        return;
    }
    // The default has to stay the run the latency table was measured on.
    let path = temp_path("bench-no-fills");
    let options = BenchOptions {
        events: 4,
        rate: 2,
        every_nth: 1,
        symbols: vec!["BTCUSDT".to_string()],
        wal_path: path.path().to_path_buf(),
        ..BenchOptions::default()
    };
    assert!(!options.fills, "off unless asked");
    run(&options);
    let (replayed, _torn) = engine_wal::replay_scan(path.path()).expect("the log reads back");
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    assert_eq!(
        engine_core::execution::Fills::from_records(&records)
            .total()
            .fills,
        0
    );
}
