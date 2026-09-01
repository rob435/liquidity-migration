use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::Value;
use sha2::{Digest, Sha256};
use signal_worker::live::{FUNDING_FETCH_CHUNK_SIZE, KLINE_FETCH_CHUNK_SIZE};
use signal_worker::model::{
    BinanceWhaleWire, BootstrapCoverage, BybitFundingWire, BybitInstrumentWire, BybitTickerWire,
    SourceCoverage, UniverseIdentity, UniverseMode, WireEvent,
};
use signal_worker::{
    DurableSignalWorker, SignalWorkerConfig, WorkerState, DAY_MS, HOUR_MS, SCHEMA_VERSION,
};

const LONG_SYMBOLS: usize = 120;
const CARRY_SYMBOLS: usize = 150;
const HISTORY_DAYS: i64 = 123;
const TICKER_STRESS_MINUTES: usize = 120;
const TICKS_PER_MINUTE: usize = 12;
const TICKER_CADENCE_MS: i64 = 5_000;
const OUTAGE_MINUTES: usize = 720;
const MAX_CHECKPOINT_BYTES: u64 = 128 * 1024 * 1024;
const MAX_JOURNAL_BYTES: u64 = 256 * 1024 * 1024;
const MAX_JOURNAL_ENTRIES: u64 = 1_024;

type TestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

#[test]
#[ignore = "release-only 270-symbol cold-start, outage, and restart resource envelope"]
fn full_population_outage_resource_envelope_is_bounded() -> TestResult {
    assert_eq!(KLINE_FETCH_CHUNK_SIZE, 1);
    assert_eq!(FUNDING_FETCH_CHUNK_SIZE, 1);
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let config = SignalWorkerConfig::load(
        repo.join("configs/signal-worker.mainnet.json"),
        repo.join("configs/long_native_v12.json"),
        repo.join("configs/lane2_carry_hold_v7.json"),
        repo.join("configs/operational.mainnet.json"),
        repo.join("deploy/engine.mainnet.toml.template"),
    )?;
    assert_eq!(config.long.cold_start_lookback_days, 100);
    assert_eq!(config.carry.minimum_replay_days, 90);
    assert_eq!(
        config
            .carry
            .vol_window_hours
            .saturating_add(config.carry.vol_return_lag_hours)
            .saturating_add(48),
        33 * 24,
    );

    let root = temporary_root();
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let (long_symbols, carry_symbols, symbols) = populations();
    let end_ms = 300 * DAY_MS;
    let start_ms = end_ms - HISTORY_DAYS * DAY_MS;
    let universe = UniverseIdentity {
        mode: UniverseMode::Pit,
        environment: "mainnet".into(),
        endpoint: "api.bybit.com".into(),
        snapshot_ts_ms: start_ms,
        available_at_ms: start_ms + 1,
        artifact_sha256: "1".repeat(64),
        file_sha256: "2".repeat(64),
        symbols: symbols.clone(),
        long_symbols: long_symbols.clone(),
        carry_symbols: carry_symbols.clone(),
    };
    let mut durable =
        DurableSignalWorker::open(config.clone(), universe.clone(), &state_dir, &spool_dir)?;

    commit(
        &mut durable,
        WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            observed_ts_ms: end_ms,
            available_at_ms: end_ms,
            rows: symbols
                .iter()
                .map(|symbol| instrument_wire(symbol, start_ms))
                .collect(),
        },
    )?;
    for (index, symbol) in symbols.iter().enumerate() {
        commit(
            &mut durable,
            WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 0,
                symbol: symbol.clone(),
                available_at_ms: end_ms,
                checked_from_ms: Some(start_ms),
                checked_through_ms: Some(end_ms),
                replace_coverage: false,
                rows: kline_rows(start_ms, end_ms, index),
            },
        )?;
    }
    for (chunk_index, chunk) in carry_symbols.chunks(FUNDING_FETCH_CHUNK_SIZE).enumerate() {
        let events = chunk
            .iter()
            .enumerate()
            .map(|(offset, symbol)| WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 0,
                symbol: symbol.clone(),
                available_at_ms: end_ms,
                checked_from_ms: Some(start_ms),
                checked_through_ms: Some(end_ms),
                replace_coverage: false,
                emit_lifecycle: false,
                rows: funding_rows(
                    start_ms,
                    end_ms,
                    chunk_index * FUNDING_FETCH_CHUNK_SIZE + offset,
                ),
            })
            .collect();
        commit_batch(&mut durable, events)?;
    }
    let (coverage, rows) = whale_rows(&carry_symbols, start_ms, end_ms);
    commit(
        &mut durable,
        WireEvent::BinanceWhaleBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            available_at_ms: end_ms,
            coverage,
            rows,
        },
    )?;
    commit(
        &mut durable,
        WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            observed_ts_ms: end_ms,
            available_at_ms: end_ms,
            rows: symbols.iter().map(|symbol| ticker_wire(symbol)).collect(),
        },
    )?;
    let state = durable.worker().state();
    let bootstrap = BootstrapCoverage {
        completed_at_ms: end_ms,
        kline_end_ms: end_ms,
        funding_end_ms: end_ms,
        whale_end_ms: end_ms,
        source_contract_sha256: state.source_contract_sha256.clone(),
        long_feature_sha256: state.long_feature_sha256.clone(),
        carry_feature_sha256: state.carry_feature_sha256.clone(),
    };
    commit(
        &mut durable,
        WireEvent::BootstrapComplete {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            coverage: bootstrap,
        },
    )?;

    assert_eq!(durable.worker().state().last_input_sequence, 424);
    assert_eq!(
        population_counts(durable.worker().state()),
        (736_560, 442_800, 1_050)
    );
    let cold_metrics = durable.durability_metrics()?;
    assert_eq!(cold_metrics.journal_entries_retained, 424);
    assert!((1..=MAX_JOURNAL_BYTES).contains(&cold_metrics.journal_bytes));
    assert_durability_bounds(&cold_metrics);

    commit(
        &mut durable,
        WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            observed_ts_ms: end_ms,
            data_through_ms: end_ms,
            gap_symbols: Vec::new(),
        },
    )?;
    commit(
        &mut durable,
        WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            observed_ts_ms: end_ms,
            data_through_ms: end_ms,
            gap_symbols: Vec::new(),
        },
    )?;
    assert_eq!(durable.worker().state().last_input_sequence, 426);
    assert_eq!(
        population_counts(durable.worker().state()),
        (412_560, 118_950, 1_050)
    );
    let cold_restart_identity = state_identity(durable.worker().state())?;
    drop(durable);

    let mut durable =
        DurableSignalWorker::open(config.clone(), universe.clone(), &state_dir, &spool_dir)?;
    assert_eq!(durable.worker().state().last_input_sequence, 426);
    assert_eq!(
        state_identity(durable.worker().state())?,
        cold_restart_identity
    );
    let cold_reopen_metrics = durable.durability_metrics()?;
    assert!(cold_reopen_metrics.checkpoint_bytes <= MAX_CHECKPOINT_BYTES);
    assert_eq!(cold_reopen_metrics.journal_bytes, 0);
    assert_eq!(cold_reopen_metrics.journal_entries_retained, 0);
    assert_durability_bounds(&cold_reopen_metrics);

    let pre_ticker_spool = durable.durability_metrics()?;
    let ticker_events = TICKER_STRESS_MINUTES * TICKS_PER_MINUTE;
    for tick in 1..=ticker_events {
        let observed_ts_ms = end_ms + i64::try_from(tick)? * TICKER_CADENCE_MS;
        commit(
            &mut durable,
            WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 0,
                observed_ts_ms,
                available_at_ms: observed_ts_ms,
                rows: symbols.iter().map(|symbol| ticker_wire(symbol)).collect(),
            },
        )?;
    }
    assert_eq!(durable.worker().state().last_input_sequence, 1_866);
    assert_eq!(
        population_counts(durable.worker().state()),
        (412_560, 118_950, 1_050)
    );
    let ticker_metrics = durable.durability_metrics()?;
    assert_eq!(ticker_metrics.spool_files, pre_ticker_spool.spool_files);
    assert_eq!(ticker_metrics.spool_bytes, pre_ticker_spool.spool_bytes);
    assert!(ticker_metrics.checkpoint_writes_session >= 2);
    assert!(ticker_metrics.checkpoint_bytes <= MAX_CHECKPOINT_BYTES);
    assert!(ticker_metrics.journal_bytes <= MAX_JOURNAL_BYTES);
    assert!(ticker_metrics.journal_entries_retained <= MAX_JOURNAL_ENTRIES);
    assert_durability_bounds(&ticker_metrics);

    let outage_start_ms = end_ms + i64::try_from(TICKER_STRESS_MINUTES)?.saturating_mul(60_000);
    apply_outage_minute(
        &mut durable,
        outage_start_ms,
        end_ms,
        1,
        &long_symbols,
        &carry_symbols,
    )?;
    let stable_spool = durable.durability_metrics()?;
    assert_eq!(stable_spool.spool_class_files["current"], 4);
    for minute in 2..=OUTAGE_MINUTES {
        apply_outage_minute(
            &mut durable,
            outage_start_ms,
            end_ms,
            minute,
            &long_symbols,
            &carry_symbols,
        )?;
    }

    assert_eq!(durable.worker().state().last_input_sequence, 3_306);
    assert_eq!(
        population_counts(durable.worker().state()),
        (410_880, 118_950, 1_050)
    );
    let outage_metrics = durable.durability_metrics()?;
    assert_eq!(outage_metrics.spool_files, stable_spool.spool_files);
    assert_eq!(outage_metrics.spool_bytes, stable_spool.spool_bytes);
    assert!(outage_metrics.replaceable_outputs_coalesced >= 2_800);
    assert!(outage_metrics.checkpoint_writes_session >= 3);
    assert_durability_bounds(&outage_metrics);
    assert!(outage_metrics.checkpoint_bytes <= MAX_CHECKPOINT_BYTES);
    assert!(outage_metrics.journal_bytes <= MAX_JOURNAL_BYTES);
    assert!(outage_metrics.journal_entries_retained <= MAX_JOURNAL_ENTRIES);

    let final_identity = state_identity(durable.worker().state())?;
    drop(durable);
    let reopened = DurableSignalWorker::open(config, universe, &state_dir, &spool_dir)?;
    assert_eq!(reopened.worker().state().last_input_sequence, 3_306);
    assert_eq!(state_identity(reopened.worker().state())?, final_identity);
    let reopened_metrics = reopened.durability_metrics()?;
    assert!(reopened_metrics.checkpoint_bytes <= MAX_CHECKPOINT_BYTES);
    assert_eq!(reopened_metrics.journal_bytes, 0);
    assert_eq!(reopened_metrics.journal_entries_retained, 0);
    assert_durability_bounds(&reopened_metrics);
    println!(
        "resource_envelope union=270 long=120 carry=150 cold_klines=736560 cold_funding=442800 ticker_cadence_ms=5000 ticker_events=1440 ticker_hours=2 outage_watermark_hours=12 final_klines=410880 final_funding=118950 checkpoint_bytes={} spool_files={} spool_bytes={} sequence=3306",
        reopened_metrics.checkpoint_bytes,
        reopened_metrics.spool_files,
        reopened_metrics.spool_bytes,
    );
    drop(reopened);
    fs::remove_dir_all(root)?;
    Ok(())
}

fn apply_outage_minute(
    durable: &mut DurableSignalWorker,
    outage_start_ms: i64,
    data_through_ms: i64,
    minute: usize,
    long_symbols: &[String],
    carry_symbols: &[String],
) -> TestResult {
    let observed_ts_ms = outage_start_ms + i64::try_from(minute)? * 60_000;
    commit(
        durable,
        WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            observed_ts_ms,
            data_through_ms,
            gap_symbols: long_symbols.to_vec(),
        },
    )?;
    commit(
        durable,
        WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 0,
            observed_ts_ms,
            data_through_ms,
            gap_symbols: carry_symbols.to_vec(),
        },
    )
}

fn commit(durable: &mut DurableSignalWorker, mut event: WireEvent) -> TestResult {
    let sequence = durable.worker().next_input_sequence()?;
    set_sequence(&mut event, sequence);
    durable.apply_and_commit(event)?;
    if durable.worker().state().last_input_sequence != sequence {
        return Err(format!("input sequence {sequence} was backpressured").into());
    }
    Ok(())
}

fn commit_batch(durable: &mut DurableSignalWorker, mut events: Vec<WireEvent>) -> TestResult {
    let first_sequence = durable.worker().next_input_sequence()?;
    let mut sequence = first_sequence;
    for event in &mut events {
        set_sequence(event, sequence);
        sequence = sequence
            .checked_add(1)
            .ok_or("input sequence exhausted in resource envelope")?;
    }
    let attempted = events.len();
    let receipt = durable.apply_many_and_commit(events)?;
    if !receipt.fully_committed() {
        return Err(format!(
            "funding chunk from input sequence {first_sequence} committed only {} of {attempted}",
            receipt.committed_events
        )
        .into());
    }
    Ok(())
}

fn set_sequence(event: &mut WireEvent, sequence: u64) {
    match event {
        WireEvent::BybitKlineBatch { sequence: slot, .. }
        | WireEvent::BybitFundingBatch { sequence: slot, .. }
        | WireEvent::BybitInstrumentSnapshot { sequence: slot, .. }
        | WireEvent::BybitTickerSnapshot { sequence: slot, .. }
        | WireEvent::BinanceWhaleBatch { sequence: slot, .. }
        | WireEvent::UniverseSnapshot { sequence: slot, .. }
        | WireEvent::BootstrapComplete { sequence: slot, .. }
        | WireEvent::Watermark { sequence: slot, .. }
        | WireEvent::LongWatermark { sequence: slot, .. }
        | WireEvent::CarryWatermark { sequence: slot, .. }
        | WireEvent::CarryScorerCatchupWatermark { sequence: slot, .. } => *slot = sequence,
    }
}

fn populations() -> (Vec<String>, Vec<String>, Vec<String>) {
    let mut long = vec!["BTCUSDT".to_owned(), "ETHUSDT".to_owned()];
    long.extend((0..LONG_SYMBOLS - 2).map(|index| format!("L{index:03}USDT")));
    let carry = (0..CARRY_SYMBOLS)
        .map(|index| format!("C{index:03}USDT"))
        .collect::<Vec<_>>();
    long.sort();
    let mut all = long.iter().chain(&carry).cloned().collect::<Vec<_>>();
    all.sort();
    (long, carry, all)
}

fn kline_rows(start_ms: i64, end_ms: i64, index: usize) -> Vec<Vec<Value>> {
    (start_ms..end_ms)
        .step_by(HOUR_MS as usize)
        .map(|open_ts_ms| {
            let close = 100.0
                + index as f64 / 10.0
                + (open_ts_ms - start_ms) as f64 / DAY_MS as f64 / 100.0;
            vec![
                Value::from(open_ts_ms),
                Value::from(format!("{close:.8}")),
                Value::from(format!("{:.8}", close + 1.0)),
                Value::from(format!("{:.8}", close - 1.0)),
                Value::from(format!("{close:.8}")),
                Value::from("100"),
                Value::from(format!("{:.8}", close * 100.0)),
            ]
        })
        .collect()
}

fn funding_rows(start_ms: i64, end_ms: i64, index: usize) -> Vec<BybitFundingWire> {
    ((start_ms + HOUR_MS)..=end_ms)
        .step_by(HOUR_MS as usize)
        .map(|funding_rate_timestamp| BybitFundingWire {
            funding_rate_timestamp: Value::from(funding_rate_timestamp),
            funding_rate: Value::from(format!("-{:.8}", 0.0001 + index as f64 / 10_000_000.0)),
            funding_interval_hour: Some(Value::from(1)),
        })
        .collect()
}

fn whale_rows(
    symbols: &[String],
    start_ms: i64,
    end_ms: i64,
) -> (Vec<SourceCoverage>, Vec<BinanceWhaleWire>) {
    let mut coverage = Vec::with_capacity(symbols.len());
    let mut rows = Vec::with_capacity(symbols.len() * HISTORY_DAYS as usize);
    for (index, symbol) in symbols.iter().enumerate() {
        coverage.push(SourceCoverage {
            symbol: symbol.clone(),
            checked_from_ms: start_ms,
            checked_through_ms: end_ms,
            replace_coverage: false,
        });
        for day_end_ms in ((start_ms + DAY_MS)..=end_ms).step_by(DAY_MS as usize) {
            rows.push(BinanceWhaleWire {
                symbol: symbol.clone(),
                day_end_ms: Value::from(day_end_ms),
                long_short_ratio: Some(Value::from(format!("{:.6}", 1.0 + index as f64 / 1_000.0))),
            });
        }
    }
    (coverage, rows)
}

fn instrument_wire(symbol: &str, launch_time_ms: i64) -> BybitInstrumentWire {
    BybitInstrumentWire {
        symbol: symbol.into(),
        contract_type: Some("LinearPerpetual".into()),
        symbol_type: None,
        status: Some("Trading".into()),
        base_coin: Some(symbol.trim_end_matches("USDT").into()),
        quote_coin: Some("USDT".into()),
        settle_coin: Some("USDT".into()),
        launch_time: Some(Value::from(launch_time_ms)),
        delivery_time: None,
        price_filter: BTreeMap::new(),
        lot_size_filter: BTreeMap::new(),
        funding_interval: Some(Value::from(60)),
        is_pre_listing: false,
    }
}

fn ticker_wire(symbol: &str) -> BybitTickerWire {
    let price = Some(Value::from("100"));
    BybitTickerWire {
        symbol: symbol.to_owned(),
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: price.clone(),
        mark_price: price.clone(),
        index_price: price.clone(),
        bid1_price: price.clone(),
        ask1_price: price,
        bid1_size: Some(Value::from("10")),
        ask1_size: Some(Value::from("10")),
        open_interest: Some(Value::from("1000")),
        open_interest_value: Some(Value::from("100000")),
        turnover24h: Some(Value::from("1000000")),
        volume24h: Some(Value::from("10000")),
        funding_rate: Some(Value::from("-0.001")),
        next_funding_time: Some(Value::from(301 * DAY_MS)),
    }
}

fn population_counts(state: &WorkerState) -> (usize, usize, usize) {
    (
        state.klines.values().map(BTreeMap::len).sum(),
        state.funding.values().map(BTreeMap::len).sum(),
        state.whales.values().map(BTreeMap::len).sum(),
    )
}

fn assert_durability_bounds(metrics: &signal_worker::worker::DurabilityMetrics) {
    assert!(metrics.spool_files <= metrics.spool_file_cap);
    assert!(metrics.spool_bytes <= metrics.spool_byte_cap);
    for (class, files) in &metrics.spool_class_files {
        assert!(files <= &metrics.spool_class_file_caps[class]);
    }
    for (class, bytes) in &metrics.spool_class_bytes {
        assert!(bytes <= &metrics.spool_class_byte_caps[class]);
    }
}

fn state_identity(state: &WorkerState) -> TestResult<(u64, String)> {
    let mut writer = HashWriter(Sha256::new());
    serde_json::to_writer(&mut writer, state)?;
    Ok((state.last_input_sequence, hex::encode(writer.0.finalize())))
}

struct HashWriter(Sha256);

impl Write for HashWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn temporary_root() -> PathBuf {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "signal-worker-resource-envelope-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed),
    ))
}
