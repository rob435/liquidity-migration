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
    };
    let result = run(&options);
    assert_eq!(result.events, 100);
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
