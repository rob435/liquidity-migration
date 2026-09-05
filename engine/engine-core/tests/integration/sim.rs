//! The simulator, run against the library as shipped: `cfg(test)` shortens
//! the engine's confirmation windows inside its own unit tests, which is not
//! the engine the fleet runs.

use std::path::PathBuf;

use engine_core::sim::{run_seed, FaultRates, SimOptions};

fn scratch(tag: &str) -> PathBuf {
    std::env::temp_dir().join(format!("engine-sim-{}-{tag}", std::process::id()))
}

fn options(seed: u64, tag: &str) -> SimOptions {
    let mut opts = SimOptions::new(seed, scratch(tag));
    opts.seconds = 300;
    opts.symbols = 2;
    opts
}

#[tokio::test]
async fn without_faults_the_simulation_keeps_the_backtest_promise() {
    let mut opts = options(1, "clean");
    opts.crashes = 0;
    opts.faults = FaultRates::NONE;
    let first = run_seed(opts.clone()).await.expect("the world runs");
    assert!(first.passed(), "{:#?}", first.failures());
    assert!(first.venue.fills > 0, "{:#?}", first.venue);
    assert!(first.orders_sent > 2, "{}", first.orders_sent);
    assert!(first.faults.is_empty(), "{:?}", first.faults);
    assert_eq!(first.segments, 1);
    let second = run_seed(opts).await.expect("the world runs again");
    assert_eq!(first.wal_sha256, second.wal_sha256, "one seed, one log");
    assert_eq!(first.venue, second.venue);
}

#[tokio::test]
async fn faults_and_a_death_leave_the_log_and_the_venue_agreeing() {
    let mut injected = std::collections::BTreeSet::new();
    for seed in 1..=6u64 {
        let mut opts = options(seed, "faulty");
        opts.crashes = 1;
        opts.faults = FaultRates::LIGHT;
        let report = run_seed(opts).await.expect("the world runs");
        assert!(report.passed(), "seed {seed}: {:#?}", report.failures());
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

#[tokio::test]
async fn one_seed_replays_byte_for_byte_under_heavy_faults() {
    let mut opts = options(7, "heavy");
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
