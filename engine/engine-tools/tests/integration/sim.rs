//! The simulator, run against the library as shipped: `cfg(test)` shortens
//! the engine's confirmation windows inside its own unit tests, which is not
//! the engine the fleet runs.

use std::path::PathBuf;

use engine_tools::sim::{run_seed, FaultRates, SimOptions};
use tokio::sync::Mutex;

/// One simulation at a time. The engine's dispatch and drain deadlines read
/// the wall clock, so two seeds sharing the machine can take different paths
/// and a replay check would then measure load, not determinism.
static ONE_AT_A_TIME: Mutex<()> = Mutex::const_new(());

fn scratch(tag: &str) -> PathBuf {
    std::env::temp_dir().join(format!("engine-sim-{}-{tag}", std::process::id()))
}

fn options(seed: u64, tag: &str) -> SimOptions {
    let mut opts = SimOptions::new(seed, scratch(tag));
    opts.seconds = 300;
    opts.symbols = 2;
    opts
}

/// The fee snapshot the quoter fingerprints below were taken against: the
/// `configs/bybit_fee_rates.json` bytes the binary embeds. Its maximum maker
/// and taker rate price every fill, so a refreshed snapshot moves every
/// quoter log without changing a single record.
const PINNED_FEE_SNAPSHOT: &str =
    "13ea6684a9f394b3dd83663f538bf0b5af3ab09d6c17f2deac5254be9c25bd1d";

/// Seed 1, 300 s, two symbols, no faults, no death. The counts and both
/// fingerprints are functions of the engine's code, the simulated venue and
/// the seed alone: a refactor leaves them; a change to what the quoter's run
/// writes moves them and is re-pinned with its change point in CHANGELOG.md.
const QUOTER_CLEAN_LOG: &str = "a19fe49ee0c57d3f07c2b1b663c7240f7b8ce77987cf23fbfbb243a37398a01b";
/// Seed 7, 300 s, two symbols, heavy faults, two deaths.
const QUOTER_HEAVY_LOG: &str = "b743e2bceedabff2e7e6bda60ac275a6dac72a96cacbd95786dee7ff5f760a57";

/// The quoter's log, record for record, with the `Boot` record left out.
///
/// `SimReport::wal_sha256` cannot be pinned: the `Boot` record carries the git
/// commit the binary was built from (`engine-core/build.rs`), plus `-dirty`,
/// so the whole-file hash moves with every commit. Everything after `Boot` is
/// a function of the code and the seed alone, and that is what a refactor must
/// leave alone.
fn log_fingerprint(dir: &std::path::Path) -> String {
    use sha2::Digest;
    let mut hasher = sha2::Sha256::new();
    for (_, record) in engine_wal::replay(dir.join("run.wal")).unwrap() {
        if matches!(record, engine_types::WalRecord::Boot { .. }) {
            continue;
        }
        hasher.update(serde_json::to_vec(&record).unwrap());
        hasher.update([b'\n']);
    }
    hex::encode(hasher.finalize())
}

fn assert_pinned_log(report: &engine_tools::sim::SimReport, dir: &std::path::Path, expected: &str) {
    let snapshot = report.fee_snapshot_sha256.as_deref().unwrap_or("none");
    if snapshot != PINNED_FEE_SNAPSHOT {
        eprintln!(
            "seed {}: fee snapshot is {snapshot}, the pin was taken against {PINNED_FEE_SNAPSHOT}; the fingerprint is not comparable",
            report.seed
        );
        return;
    }
    assert_eq!(
        log_fingerprint(dir),
        expected,
        "seed {}: the quoter's log changed under an unchanged fee snapshot",
        report.seed
    );
}

fn assert_order_terms_and_simulated_fill_boundary(dir: &std::path::Path) {
    let mut orders = 0;
    let mut fills = 0;
    let mut legacy_frontiers = 0;
    let mut ledger = engine_core::inflight::LedgerOfOrders::default();
    for (_, record) in engine_wal::replay(dir.join("run.wal")).unwrap() {
        ledger.try_apply(&record).unwrap();
        match record {
            engine_types::WalRecord::OrderSent { request, .. } => {
                assert!(request.exact_terms.is_some());
                orders += 1;
            }
            engine_types::WalRecord::OrderUpdate {
                update:
                    engine_types::OrderUpdate::Fill {
                        amounts,
                        client_order_id,
                        ..
                    },
                ..
            } => {
                assert!(
                    amounts.is_none(),
                    "the simulator still models binary64 fills"
                );
                fills += 1;
                if let Some(order) = ledger.orders.get(&client_order_id) {
                    assert!(matches!(
                        order.fill_quantity,
                        engine_types::wal::OrderFillQuantity::LegacyBinary64 { .. }
                    ));
                    legacy_frontiers += 1;
                }
            }
            _ => {}
        }
    }
    assert!(orders > 0);
    assert!(fills > 0);
    assert!(legacy_frontiers > 0);
}

#[tokio::test(start_paused = true)]
async fn without_faults_the_simulation_keeps_the_backtest_promise() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut opts = options(1, "clean");
    opts.crashes = 0;
    opts.faults = FaultRates::NONE;
    opts.keep = true;
    let dir = opts.dir.clone();
    let first = run_seed(opts.clone()).await.expect("the world runs");
    assert!(first.passed(), "{:#?}", first.failures());
    assert!(first.venue.fills > 0, "{:#?}", first.venue);
    assert!(first.orders_sent > 2, "{}", first.orders_sent);
    assert!(first.faults.is_empty(), "{:?}", first.faults);
    assert_eq!(first.segments, 1);
    assert_eq!(first.wal_records, 10897);
    assert_pinned_log(&first, &dir, QUOTER_CLEAN_LOG);
    assert_order_terms_and_simulated_fill_boundary(&dir);
    let second = run_seed(opts).await.expect("the world runs again");
    assert_eq!(first.wal_sha256, second.wal_sha256, "one seed, one log");
    assert_eq!(first.venue, second.venue);
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test(start_paused = true)]
async fn faults_and_a_death_leave_the_log_and_the_venue_agreeing() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut injected = std::collections::BTreeSet::new();
    // Record counts, unlike the log's file hash, are a function of the code
    // and the seed alone.
    let mut counts = Vec::new();
    for (seed, records) in [
        (1u64, 11387),
        (2, 11074),
        (3, 12242),
        (4, 11588),
        (5, 10901),
        (6, 11027),
    ] {
        let mut opts = options(seed, "faulty");
        opts.crashes = 1;
        opts.faults = FaultRates::LIGHT;
        let report = run_seed(opts).await.expect("the world runs");
        assert!(report.passed(), "seed {seed}: {:#?}", report.failures());
        counts.push((seed, records, report.wal_records));
        assert_eq!(report.crashes_injected, 1, "seed {seed}");
        // One boot, one after the death, one after each exit the engine chose.
        assert_eq!(
            report.segments,
            2 + report.restarts,
            "seed {seed}: {:?}",
            report.restart_reasons
        );
        injected.extend(report.faults.keys().cloned());
    }
    // Every seed is reported before any one of them fails.
    let mismatched: Vec<_> = counts
        .iter()
        .filter(|(_, pinned, actual)| pinned != actual)
        .collect();
    assert!(
        mismatched.is_empty(),
        "pinned WAL record counts moved: {mismatched:?} as (seed, pinned, actual); every seed: {counts:?}"
    );
    for kind in [
        "venue.reply_lost",
        "private.drop",
        "private.duplicate",
        "process.death",
    ] {
        assert!(
            injected.contains(kind),
            "{kind} never happened in {injected:?}"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn one_seed_replays_byte_for_byte_under_heavy_faults() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut opts = options(7, "heavy");
    opts.crashes = 2;
    opts.faults = FaultRates::HEAVY;
    opts.keep = true;
    let dir = opts.dir.clone();
    let first = run_seed(opts.clone()).await.expect("the world runs");
    assert!(first.passed(), "{:#?}", first.failures());
    assert_eq!(first.crashes_injected, 2);
    assert_eq!(first.wal_records, 10855);
    assert_pinned_log(&first, &dir, QUOTER_HEAVY_LOG);
    assert_order_terms_and_simulated_fill_boundary(&dir);
    // Every halt cancel the venue refused, never answered or never confirmed
    // was settled by a status read. A boot whose account read fails still
    // exits for its supervisor; a reconciliation exit is that lane failing.
    assert!(
        first
            .restart_reasons
            .iter()
            .all(|reason| !reason.contains("venue reconciliation needed")),
        "{:?}",
        first.restart_reasons
    );
    let second = run_seed(opts).await.expect("the world runs again");
    assert_eq!(first.wal_sha256, second.wal_sha256);
    assert_eq!(first.faults, second.faults);
    assert_eq!(first.venue, second.venue);
    std::fs::remove_dir_all(dir).unwrap();
}

fn realm_options(seed: u64, tag: &str, hours: u64) -> SimOptions {
    let mut opts = SimOptions::realm(seed, scratch(tag), engine_tools::sim::Realm::Mexc);
    opts.hours(hours);
    // Every symbol carries an entry trigger every day, so the cell's coverage
    // does not depend on the draw.
    opts.pump_probability = 1.0;
    opts
}

/// The funded forward test: the mexc template's own generated blocks, on the
/// fleet's operational profile, against the synthetic producer.
#[tokio::test(start_paused = true)]
async fn a_realm_s_own_blocks_trade_under_the_producer() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut opts = realm_options(1, "realm-clean", 3);
    opts.crashes = 0;
    opts.faults = FaultRates::NONE;
    opts.keep = true;
    let dir = opts.dir.clone();
    let first = run_seed(opts.clone()).await.expect("the world runs");
    assert!(first.passed(), "{:#?}", first.failures());
    assert!(first.faults.is_empty(), "{:?}", first.faults);
    assert_eq!(first.segments, 1);
    assert!(first.signals_published > 6, "{}", first.signals_published);
    assert_eq!(first.signals_consumed, first.signals_published);
    assert_eq!(first.signals_rejected, 0);
    assert!(
        first.strategy_errors.is_empty(),
        "{:?}",
        first.strategy_errors
    );
    assert!(
        first.orders_by_sleeve.get("long").copied().unwrap_or(0) > 0,
        "{:?}",
        first.orders_by_sleeve
    );
    assert!(
        first.fills_by_sleeve.get("long").copied().unwrap_or(0) > 0,
        "{:?}",
        first.fills_by_sleeve
    );
    // The shock takes one symbol 20 % down and holds it there, so the native
    // position stop triggers on the mark and fills by walking the book.
    assert!(first.venue.stop_fills > 0, "{:#?}", first.venue);
    let second = run_seed(opts).await.expect("the world runs again");
    assert_eq!(first.wal_sha256, second.wal_sha256, "one seed, one log");
    assert_eq!(first.venue, second.venue);
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test(start_paused = true)]
async fn realm_signal_faults_and_a_death_leave_the_log_and_the_sleeves_agreeing() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut injected = std::collections::BTreeSet::new();
    for seed in 1..=6u64 {
        let mut opts = realm_options(seed, "realm-faulty", 2);
        opts.crashes = 1;
        opts.faults = FaultRates::LIGHT;
        let report = run_seed(opts).await.expect("the world runs");
        assert!(report.passed(), "seed {seed}: {:#?}", report.failures());
        assert!(
            report.strategy_errors.is_empty(),
            "seed {seed}: {:?}",
            report.strategy_errors
        );
        assert_eq!(report.crashes_injected, 1, "seed {seed}");
        assert_eq!(
            report.segments,
            2 + report.restarts,
            "seed {seed}: {:?}",
            report.restart_reasons
        );
        assert!(
            report.fills_by_sleeve.get("long").copied().unwrap_or(0) > 0,
            "seed {seed}: {:?}",
            report.fills_by_sleeve
        );
        injected.extend(report.faults.keys().cloned());
    }
    for kind in ["signal.delay", "signal.withhold", "process.death"] {
        assert!(
            injected.contains(kind),
            "{kind} never happened in {injected:?}"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn one_realm_seed_replays_byte_for_byte_under_heavy_faults() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut opts = realm_options(7, "realm-heavy", 2);
    opts.crashes = 2;
    opts.faults = FaultRates::HEAVY;
    let first = run_seed(opts.clone()).await.expect("the world runs");
    assert!(first.passed(), "{:#?}", first.failures());
    assert_eq!(first.crashes_injected, 2);
    let second = run_seed(opts).await.expect("the world runs again");
    assert_eq!(first.wal_sha256, second.wal_sha256);
    assert_eq!(first.faults, second.faults);
    assert_eq!(first.venue, second.venue);
}

/// A whole trading day's worth of hourly publications, which is where the
/// engine's first leverage administration per symbol has to survive an
/// account refresh landing inside its round trip.
#[tokio::test(start_paused = true)]
async fn a_realm_day_opens_every_symbol_it_decides_on() {
    let _alone = ONE_AT_A_TIME.lock().await;
    let mut opts = realm_options(1, "realm-day", 12);
    opts.pump_probability = 0.5;
    opts.crashes = 0;
    opts.faults = FaultRates::NONE;
    let report = run_seed(opts).await.expect("the world runs");
    assert!(report.passed(), "{:#?}", report.failures());
    assert!(
        report.fills_by_sleeve.get("long").copied().unwrap_or(0) > 0,
        "{:?} orders, {:?} fills",
        report.orders_by_sleeve,
        report.fills_by_sleeve
    );
}
