use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot, Semaphore};
use tokio::task::JoinSet;
use tokio::time::MissedTickBehavior;

use crate::bybit_ws::{
    ticker_wire, BybitPublicStream, ConfirmedKline, StreamEvent, StreamHealth, TickerSample,
};
use crate::config::SignalWorkerConfig;
use crate::http::{percent_encode, wall_ms, PublicHttpClient};
use crate::model::{
    BinanceWhaleWire, BootstrapCoverage, BybitFundingWire, BybitInstrumentWire, BybitTickerWire,
    InstrumentTradingInterval, SourceCoverage, WireEvent,
};
use crate::normalize::{
    normalize_funding_rows, normalize_instruments, normalize_kline_rows, normalize_whales,
};
use crate::store::atomic_write;
use crate::worker::{
    required_carry_history_hours, spool_class_caps, DurableSignalWorker, WorkerError,
};
use crate::{DAY_MS, HOUR_MS, SCHEMA_VERSION};

const FIVE_MIN_MS: i64 = 300_000;
const KLINE_PUBLICATION_LAG_MS: i64 = 60_000;
const FUNDING_PUBLICATION_LAG_MS: i64 = 5 * 60_000;
const CARRY_CATCHUP_CHUNK_DAYS: i64 = 1;
const STARTUP_MAX_MS: i64 = 120 * 60_000;
const TRANSIENT_RECOVERY_MAX_MS: i64 = 2 * 60_000;
pub const KLINE_FETCH_CHUNK_SIZE: usize = 1;
pub const FUNDING_FETCH_CHUNK_SIZE: usize = 1;
pub const WHALE_FETCH_CHUNK_SIZE: usize = 1;
const LANE_COMPLETION_QUEUE_CAPACITY: usize = 1;

#[derive(Clone, Debug)]
pub struct LiveRunOptions {
    pub state_dir: PathBuf,
    pub spool_dir: PathBuf,
    pub heartbeat: PathBuf,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerHeartbeat {
    pub schema_version: u32,
    pub kind: String,
    pub status: String,
    pub pid: u32,
    pub updated_at_ms: i64,
    pub public_market_realm: String,
    pub public_bybit_host: String,
    pub credential_free: bool,
    pub signal_config_sha256: String,
    pub long_rule_sha256: String,
    pub long_feature_contract_sha256: String,
    pub carry_config_sha256: String,
    pub carry_feature_contract_sha256: String,
    pub operational_config_sha256: String,
    pub engine_config_sha256: String,
    pub universe_artifact_sha256: String,
    pub universe_file_sha256: String,
    pub universe_snapshot_ts_ms: i64,
    pub universe_symbols: usize,
    pub universe_long_symbols: usize,
    pub universe_carry_symbols: usize,
    pub llm_gate_enabled: bool,
    pub llm_gate_last_decision_ts_ms: Option<i64>,
    pub llm_gate_last_candidates: usize,
    pub source_generation: String,
    pub last_input_sequence: u64,
    pub long_output_sequence: u64,
    pub carry_output_sequence: u64,
    pub last_observed_ts_ms: i64,
    pub last_long_feature_ts_ms: Option<i64>,
    pub long_skipped_generation_count: u64,
    pub last_long_skipped_first_ts_ms: Option<i64>,
    pub last_long_skipped_last_ts_ms: Option<i64>,
    pub last_carry_decision_ts_ms: Option<i64>,
    pub last_carry_scorer_ts_ms: Option<i64>,
    pub last_carry_upcoming_ts_ms: Option<i64>,
    pub last_long_cycle_completed_wall_ts_ms: Option<i64>,
    pub last_carry_cycle_completed_wall_ts_ms: Option<i64>,
    pub long_cycle_cadence_ms: u64,
    pub carry_cycle_cadence_ms: u64,
    pub rest_ticker_last_success_wall_ts_ms: Option<i64>,
    pub rest_ticker_last_failure_wall_ts_ms: Option<i64>,
    pub rest_ticker_success_count: u64,
    pub rest_ticker_failure_count: u64,
    pub bybit_ws_connected: bool,
    pub bybit_ws_epoch: u64,
    pub bybit_ws_gap_open: bool,
    pub bybit_ws_gap_open_since_wall_ts_ms: Option<i64>,
    pub bybit_ws_reconnect_count: u64,
    pub bybit_ws_fault_count: u64,
    pub bybit_ws_last_frame_ts_ms: Option<i64>,
    pub bybit_ws_ticker_rows: usize,
    pub bybit_ws_ticker_capacity: usize,
    pub bybit_ws_ticker_coverage_complete: bool,
    pub bybit_ws_ticker_topics_accepted: usize,
    pub bybit_ws_ticker_topics_quarantined: usize,
    pub bybit_ws_kline_topics_accepted: usize,
    pub bybit_ws_kline_topics_quarantined: usize,
    pub bybit_ws_queued_frames: usize,
    pub bybit_ws_queue_capacity: usize,
    pub spool_files: u64,
    pub spool_bytes: u64,
    pub spool_file_cap: u64,
    pub spool_byte_cap: u64,
    pub spool_byte_soft_threshold: u64,
    pub replaceable_outputs_coalesced: u64,
    pub spool_backpressured: bool,
    pub spool_class_files: BTreeMap<String, u64>,
    pub spool_class_bytes: BTreeMap<String, u64>,
    pub spool_class_file_caps: BTreeMap<String, u64>,
    pub spool_class_byte_caps: BTreeMap<String, u64>,
    pub spool_class_byte_soft_thresholds: BTreeMap<String, u64>,
    pub spool_backpressured_classes: Vec<String>,
}

pub struct LiveRunner {
    config: SignalWorkerConfig,
    durable: DurableSignalWorker,
    bybit: PublicHttpClient,
    /// The realm's own venue host, whose instrument list bounds what the
    /// account may trade. Mainnet's is the public host; demo's is the demo
    /// venue.
    bybit_instruments: PublicHttpClient,
    binance: PublicHttpClient,
    heartbeat_path: PathBuf,
    last_gate_decision_ts_ms: Option<i64>,
    last_gate_candidates: usize,
    last_long_cycle_completed_wall_ts_ms: Option<i64>,
    last_carry_cycle_completed_wall_ts_ms: Option<i64>,
    rest_ticker_last_success_wall_ts_ms: Option<i64>,
    rest_ticker_last_failure_wall_ts_ms: Option<i64>,
    rest_ticker_success_count: u64,
    rest_ticker_failure_count: u64,
}

#[derive(Default)]
struct LaneState {
    instruments: bool,
    tickers: bool,
    gate: bool,
    funding: bool,
    whales: bool,
    repair: bool,
    // The newest epoch the live stream has reported. Only
    // `mark_gap_repaired(epoch)` closes the WebSocket gap, and callers that
    // restart the repair lane without an epoch must not erase it.
    repair_epoch: Option<u64>,
    instruments_ready: bool,
    funding_ready: bool,
    repair_failure_count: usize,
    repair_failure_samples: Vec<(String, String)>,
}

enum LaneCompletion {
    Instruments(Result<FetchedUniverseInputs, WorkerError>),
    Tickers(Result<FetchedTickers, WorkerError>),
    Gate(Result<Option<FetchedGate>, WorkerError>),
    FundingChunk {
        result: Result<FetchedFunding, WorkerError>,
        resume: oneshot::Sender<bool>,
    },
    FundingFinished {
        succeeded: bool,
    },
    WhaleChunk {
        result: Result<FetchedWhales, WorkerError>,
        resume: oneshot::Sender<bool>,
    },
    WhaleFinished,
    RepairChunk {
        result: Result<FetchedKlineJobs, WorkerError>,
        resume: oneshot::Sender<bool>,
    },
    RepairFinished {
        end_ms: i64,
        epoch: Option<u64>,
    },
}

struct FetchedInstruments {
    observed_ts_ms: i64,
    available_at_ms: i64,
    rows: Vec<BybitInstrumentWire>,
}

/// The venue's whole instrument list from the realm host plus the whole
/// ticker page from the public host: everything the universe is derived from.
struct FetchedUniverseInputs {
    instruments: FetchedInstruments,
    tickers: FetchedTickers,
}

/// One read of the LLM gate's candidates file.
#[derive(Debug)]
struct FetchedGate {
    read_at_ms: i64,
    decision_ts_ms: i64,
    valid_until_ms: i64,
    rows: Vec<crate::model::LlmGateCandidate>,
}

struct FetchedWhales {
    available_at_ms: i64,
    rows: Vec<BinanceWhaleWire>,
    coverage: Vec<SourceCoverage>,
}

struct FetchedTickers {
    request_started_at_ms: i64,
    observed_ts_ms: i64,
    available_at_ms: i64,
    rows: Vec<BybitTickerWire>,
}

struct FetchedKlineBatch {
    rows: Vec<Vec<Value>>,
    available_at_ms: i64,
    checked_from_ms: Option<i64>,
    checked_through_ms: Option<i64>,
}

struct FetchedFundingBatch {
    rows: Vec<BybitFundingWire>,
    available_at_ms: i64,
    checked_from_ms: Option<i64>,
    checked_through_ms: Option<i64>,
    emit_lifecycle: bool,
}

struct FetchedFunding {
    batches: Vec<(String, FetchedFundingBatch)>,
    failures: Vec<(String, String)>,
}

type FundingJob = (String, i64, i64, bool);
type KlineJob = (String, i64, i64);
type WhaleJob = (String, i64, i64);

fn funding_job_chunks(jobs: &[FundingJob]) -> std::slice::Chunks<'_, FundingJob> {
    jobs.chunks(FUNDING_FETCH_CHUNK_SIZE)
}

fn kline_job_chunks(jobs: &[KlineJob]) -> std::slice::Chunks<'_, KlineJob> {
    jobs.chunks(KLINE_FETCH_CHUNK_SIZE)
}

fn whale_job_chunks(jobs: &[WhaleJob]) -> std::slice::Chunks<'_, WhaleJob> {
    jobs.chunks(WHALE_FETCH_CHUNK_SIZE)
}

fn lane_source_failure(label: &str, error: WorkerError) -> Result<(), WorkerError> {
    if !error.is_lane_local_source_failure() {
        return Err(error);
    }
    eprintln!("signal-worker: {label}: {error}");
    Ok(())
}

struct FetchedKlineJobs {
    batches: Vec<(String, FetchedKlineBatch)>,
    failures: Vec<(String, String)>,
}

fn validate_fetched_instruments(fetched: &FetchedInstruments) -> Result<(), WorkerError> {
    normalize_instruments(
        fetched.observed_ts_ms,
        fetched.available_at_ms,
        &fetched.rows,
    )
    .map(drop)
}

/// A whole ticker page: rows that fail are left out downstream, but a page
/// with rows and nothing usable is a failed fetch.
fn validate_fetched_tickers(fetched: &FetchedTickers) -> Result<(), WorkerError> {
    let (kept, rejected) = crate::normalize::normalize_tickers_reporting(
        fetched.observed_ts_ms,
        fetched.available_at_ms,
        &fetched.rows,
    )?;
    if kept.is_empty() && !rejected.rows.is_empty() {
        return Err(WorkerError::input(format!(
            "no usable ticker rows: {}",
            rejected.summary("ticker").unwrap_or_default()
        )));
    }
    Ok(())
}

/// One WebSocket sample: every row must be right, or the stream has a gap.
fn validate_stream_ticker_sample(fetched: &FetchedTickers) -> Result<(), WorkerError> {
    for row in &fetched.rows {
        crate::normalize::normalize_ticker_strict(
            fetched.observed_ts_ms,
            fetched.available_at_ms,
            row,
        )?;
    }
    Ok(())
}

fn validate_instrument_source_against_state(
    state: &crate::worker::WorkerState,
    fetched: &FetchedInstruments,
) -> Result<(), WorkerError> {
    normalize_instruments(
        fetched.observed_ts_ms,
        fetched.available_at_ms,
        &fetched.rows,
    )?;
    if state
        .instruments
        .values()
        .map(|row| row.observed_ts_ms)
        .max()
        .is_some_and(|latest| fetched.observed_ts_ms < latest)
    {
        return Err(WorkerError::input(
            "Bybit instrument snapshot moved backwards",
        ));
    }
    Ok(())
}

fn validate_funding_source_against_state(
    state: &crate::worker::WorkerState,
    fetched: &FetchedFunding,
) -> Result<(), WorkerError> {
    let mut seen = BTreeMap::new();
    for (symbol, batch) in &fetched.batches {
        for row in normalize_funding_rows(symbol, batch.available_at_ms, &batch.rows)? {
            if let Some(existing) = state
                .funding
                .get(&row.symbol)
                .and_then(|history| history.get(&row.settlement_ts_ms))
            {
                if existing.symbol != row.symbol
                    || existing.settlement_ts_ms != row.settlement_ts_ms
                    || existing.rate != row.rate
                    || existing.funding_interval_min != row.funding_interval_min
                {
                    return Err(WorkerError::input(format!(
                        "funding history rewrote timestamp {}",
                        row.settlement_ts_ms
                    )));
                }
            }
            let key = (row.symbol.clone(), row.settlement_ts_ms);
            if let Some(existing) = seen.insert(key, row.clone()) {
                if existing.rate != row.rate
                    || existing.funding_interval_min != row.funding_interval_min
                {
                    return Err(WorkerError::input(format!(
                        "funding fetch rewrote timestamp {}",
                        row.settlement_ts_ms
                    )));
                }
            }
        }
    }
    Ok(())
}

fn validate_whale_source_against_state(
    state: &crate::worker::WorkerState,
    fetched: &FetchedWhales,
) -> Result<(), WorkerError> {
    let mut seen = BTreeMap::new();
    for row in normalize_whales(fetched.available_at_ms, &fetched.rows)? {
        if let Some(existing) = state
            .whales
            .get(&row.symbol)
            .and_then(|history| history.get(&row.day_end_ms))
        {
            if existing.symbol != row.symbol
                || existing.day_end_ms != row.day_end_ms
                || existing.long_short_ratio != row.long_short_ratio
            {
                return Err(WorkerError::input(format!(
                    "whale history rewrote timestamp {}",
                    row.day_end_ms
                )));
            }
        }
        let key = (row.symbol.clone(), row.day_end_ms);
        if let Some(existing) = seen.insert(key, row.clone()) {
            if existing.long_short_ratio != row.long_short_ratio {
                return Err(WorkerError::input(format!(
                    "whale fetch rewrote timestamp {}",
                    row.day_end_ms
                )));
            }
        }
    }
    Ok(())
}

fn validate_kline_source_against_state(
    state: &crate::worker::WorkerState,
    fetched: &FetchedKlineJobs,
) -> Result<(), WorkerError> {
    let mut seen = BTreeMap::new();
    for (symbol, batch) in &fetched.batches {
        for row in normalize_kline_rows(symbol, batch.available_at_ms, &batch.rows)? {
            if let Some(existing) = state
                .klines
                .get(&row.symbol)
                .and_then(|history| history.get(&row.open_ts_ms))
            {
                if !same_kline_value(existing, &row) {
                    return Err(WorkerError::input(format!(
                        "kline history rewrote timestamp {}",
                        row.open_ts_ms
                    )));
                }
            }
            let key = (row.symbol.clone(), row.open_ts_ms);
            if let Some(existing) = seen.insert(key, row.clone()) {
                if !same_kline_value(&existing, &row) {
                    return Err(WorkerError::input(format!(
                        "kline fetch rewrote timestamp {}",
                        row.open_ts_ms
                    )));
                }
            }
        }
    }
    Ok(())
}

fn same_kline_value(left: &crate::model::HourlyKline, right: &crate::model::HourlyKline) -> bool {
    left.symbol == right.symbol
        && left.open_ts_ms == right.open_ts_ms
        && left.open == right.open
        && left.high == right.high
        && left.low == right.low
        && left.close == right.close
        && left.volume_base == right.volume_base
        && left.turnover_quote == right.turnover_quote
}

impl LiveRunner {
    pub async fn open_responsive(
        config: SignalWorkerConfig,
        options: LiveRunOptions,
    ) -> Result<Option<Self>, WorkerError> {
        write_provisional_heartbeat(&config, None, &options.heartbeat, "starting")?;
        let state_dir = options.state_dir.clone();
        let spool_dir = options.spool_dir.clone();
        let recovery_config = config.clone();
        let (recovery_tx, mut recovery_rx) = tokio::sync::oneshot::channel();
        std::thread::Builder::new()
            .name("signal-worker-recovery".to_owned())
            .spawn(move || {
                let result = DurableSignalWorker::open(recovery_config, state_dir, spool_dir);
                let _ = recovery_tx.send(result);
            })
            .map_err(|error| WorkerError::io("spawn signal-worker recovery", error))?;
        let mut heartbeat_tick = cadence(config.live.ticker_cadence_ms.min(5_000));
        heartbeat_tick.tick().await;
        let shutdown = shutdown_signal();
        tokio::pin!(shutdown);
        let durable = loop {
            tokio::select! {
                result = &mut recovery_rx => {
                    break result
                        .map_err(|_| WorkerError::state("signal-worker recovery task stopped"))??;
                }
                _ = heartbeat_tick.tick() => {
                    write_provisional_heartbeat(&config, None, &options.heartbeat, "starting")?;
                }
                signal = &mut shutdown => {
                    signal?;
                    write_provisional_heartbeat(&config, None, &options.heartbeat, "stopped")?;
                    return Ok(None);
                }
            }
        };
        let bybit_host = match config.live.public_market_realm.as_str() {
            "mainnet" => &config.sources.bybit_mainnet_host,
            _ => return Err(WorkerError::config("unsupported public market realm")),
        };
        let request_budget = Arc::new(Semaphore::new(config.live.max_parallel_requests));
        let bybit = PublicHttpClient::new(
            bybit_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            Arc::clone(&request_budget),
        )?;
        let bybit_instruments = PublicHttpClient::new(
            crate::worker::realm_endpoint(&config),
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            Arc::clone(&request_budget),
        )?;
        let binance = PublicHttpClient::new(
            &config.sources.binance_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            request_budget,
        )?;
        Ok(Some(Self {
            config,
            durable,
            bybit,
            bybit_instruments,
            binance,
            heartbeat_path: options.heartbeat,
            last_gate_decision_ts_ms: None,
            last_gate_candidates: 0,
            last_long_cycle_completed_wall_ts_ms: None,
            last_carry_cycle_completed_wall_ts_ms: None,
            rest_ticker_last_success_wall_ts_ms: None,
            rest_ticker_last_failure_wall_ts_ms: None,
            rest_ticker_success_count: 0,
            rest_ticker_failure_count: 0,
        }))
    }

    pub fn new(config: SignalWorkerConfig, options: LiveRunOptions) -> Result<Self, WorkerError> {
        let universe = crate::universe::unresolved_universe(
            &config.live.environment,
            crate::worker::realm_endpoint(&config),
        );
        Self::new_with_universe(config, universe, options)
    }

    /// Seeds a missing checkpoint with `universe`; the live lanes refresh it.
    pub fn new_with_universe(
        config: SignalWorkerConfig,
        universe: crate::model::UniverseIdentity,
        options: LiveRunOptions,
    ) -> Result<Self, WorkerError> {
        let bybit_host = match config.live.public_market_realm.as_str() {
            "mainnet" => &config.sources.bybit_mainnet_host,
            _ => return Err(WorkerError::config("unsupported public market realm")),
        };
        let request_budget = Arc::new(Semaphore::new(config.live.max_parallel_requests));
        let bybit = PublicHttpClient::new(
            bybit_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            Arc::clone(&request_budget),
        )?;
        let bybit_instruments = PublicHttpClient::new(
            crate::worker::realm_endpoint(&config),
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            Arc::clone(&request_budget),
        )?;
        let binance = PublicHttpClient::new(
            &config.sources.binance_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            request_budget,
        )?;
        let durable = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe,
            options.state_dir,
            options.spool_dir,
        )?;
        Ok(Self {
            config,
            durable,
            bybit,
            bybit_instruments,
            binance,
            heartbeat_path: options.heartbeat,
            last_gate_decision_ts_ms: None,
            last_gate_candidates: 0,
            last_long_cycle_completed_wall_ts_ms: None,
            last_carry_cycle_completed_wall_ts_ms: None,
            rest_ticker_last_success_wall_ts_ms: None,
            rest_ticker_last_failure_wall_ts_ms: None,
            rest_ticker_success_count: 0,
            rest_ticker_failure_count: 0,
        })
    }

    pub async fn bootstrap(&mut self) -> Result<(), WorkerError> {
        if !self.needs_cold_bootstrap() {
            return Ok(());
        }
        self.refresh_instruments().await?;
        self.refresh_tickers().await?;
        let now = wall_ms()?;
        let end = closed_kline_end(now);
        let carry_replay_hours =
            required_carry_history_hours(&self.config, self.durable.worker().state());
        let long_hours = i64::try_from(self.config.long.cold_start_lookback_days)
            .unwrap_or(i64::MAX / 24)
            .saturating_mul(24)
            .saturating_add(48);
        let start = end.saturating_sub(long_hours.max(carry_replay_hours) * HOUR_MS);
        self.refresh_klines(start, end).await?;
        let funding_end = now.saturating_sub(FUNDING_PUBLICATION_LAG_MS);
        self.refresh_funding(start, funding_end).await?;
        let whale_days = i64::try_from(self.config.carry.whale_feed_days)
            .map_err(|_| WorkerError::config("whale feed days exceed i64"))?;
        self.refresh_whales(now.saturating_sub(whale_days * DAY_MS), now)
            .await?;
        self.refresh_tickers().await?;
        let state = self.durable.worker().state();
        self.commit(WireEvent::BootstrapComplete {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            coverage: BootstrapCoverage {
                completed_at_ms: wall_ms()?,
                kline_end_ms: end,
                funding_end_ms: funding_end,
                whale_end_ms: now,
                source_contract_sha256: state.source_contract_sha256.clone(),
                long_feature_sha256: state.long_feature_sha256.clone(),
                carry_feature_sha256: state.carry_feature_sha256.clone(),
            },
        })?;
        if let Some(gap_symbols) = self.long_gap_symbols(end) {
            self.long_watermark(end, gap_symbols)?;
        }
        let mut ready = LaneState {
            instruments_ready: true,
            funding_ready: true,
            ..LaneState::default()
        };
        self.try_carry_watermark(&mut ready, None)?;
        Ok(())
    }

    /// A worker with no derived universe cannot own a symbol or publish an
    /// observation, so the first refresh happens before the lanes start. A
    /// venue fault here is retried with backoff rather than left to systemd.
    async fn resolve_universe(&mut self) -> Result<(), WorkerError> {
        let mut delay_ms = self.config.live.retry_base_ms.max(500);
        loop {
            if crate::universe::universe_is_resolved(&self.durable.worker().state().universe) {
                return Ok(());
            }
            match self.refresh_instruments().await {
                Ok(()) => {}
                Err(error) if error.is_lane_local_source_failure() => {
                    eprintln!("signal-worker: universe refresh failed, retrying: {error}");
                    self.write_heartbeat("starting", None)?;
                    tokio::time::sleep(Duration::from_millis(delay_ms)).await;
                    delay_ms = delay_ms.saturating_mul(2).min(60_000);
                }
                Err(error) => return Err(error),
            }
        }
    }

    pub async fn run(mut self) -> Result<(), WorkerError> {
        self.write_heartbeat("starting", None)?;
        self.resolve_universe().await?;
        let run_started_at_ms = wall_ms()?;
        let symbols = self.kline_symbols();
        let pending_limit = pending_kline_limit(symbols.len());
        let mut stream = BybitPublicStream::spawn(
            self.stream_symbols(),
            self.config.live.request_timeout_ms,
            self.config.live.retry_base_ms,
        )?;
        let (lane_tx, mut lane_rx) = mpsc::channel(LANE_COMPLETION_QUEUE_CAPACITY);
        let mut lanes = LaneState {
            instruments_ready: !self.durable.worker().state().instruments.is_empty(),
            ..LaneState::default()
        };
        let mut pending_klines = BTreeMap::<(String, i64), ConfirmedKline>::new();
        let mut transient_recovery_started_at_ms = None;

        let mut ticker_tick = cadence(self.config.live.ticker_cadence_ms);
        let mut instrument_tick = cadence(self.config.live.instrument_cadence_ms);
        let mut funding_tick = cadence(self.config.live.funding_cadence_ms);
        let mut kline_tick = cadence(self.config.live.kline_cadence_ms);
        let mut whale_tick = cadence(self.config.live.whale_cadence_ms);
        let mut gate_tick = cadence(self.config.llm_gate.poll_cadence_ms);
        let mut heartbeat_tick = cadence(self.config.live.ticker_cadence_ms.min(5_000));
        ticker_tick.tick().await;
        instrument_tick.tick().await;
        funding_tick.tick().await;
        kline_tick.tick().await;
        whale_tick.tick().await;
        gate_tick.tick().await;
        heartbeat_tick.tick().await;
        let shutdown = shutdown_signal();
        tokio::pin!(shutdown);

        lanes.instruments = true;
        spawn_instrument_lane(
            lane_tx.clone(),
            self.bybit_instruments.clone(),
            self.bybit.clone(),
            self.config.sources.bybit_category.clone(),
            self.config.live.instrument_max_pages,
        );
        lanes.tickers = true;
        self.spawn_ticker_lane(lane_tx.clone())?;
        lanes.whales = true;
        self.spawn_whale_lane(lane_tx.clone())?;
        self.start_kline_repair(&lane_tx, &mut lanes, None)?;

        loop {
            tokio::select! {
                event = stream.next_event() => {
                    let event = event.ok_or_else(|| WorkerError::network("Bybit public stream task stopped"))?;
                    self.handle_stream_event(
                        event,
                        &mut stream,
                        &mut pending_klines,
                        pending_limit,
                        &lane_tx,
                        &mut lanes,
                    )?;
                }
                completion = lane_rx.recv() => {
                    let completion = completion.ok_or_else(|| WorkerError::state("public source lane channel closed"))?;
                    self.handle_lane_completion(
                        completion,
                        &mut stream,
                        &mut pending_klines,
                        &lane_tx,
                        &mut lanes,
                    )?;
                }
                _ = ticker_tick.tick() => {
                    let now_ms = wall_ms()?;
                    if let Some(sample) = stream.sample_tickers(now_ms, self.config.sources.mark_max_age_ms) {
                        self.commit_stream_ticker_sample(&mut stream, sample)?;
                    }
                    if (!stream.health().ticker_coverage_complete || !stream.health().connected)
                        && !lanes.tickers
                    {
                        lanes.tickers = true;
                        self.spawn_ticker_lane(lane_tx.clone())?;
                    }
                }
                _ = instrument_tick.tick() => {
                    if !lanes.instruments && !lanes.funding {
                        lanes.instruments = true;
                        spawn_instrument_lane(
                            lane_tx.clone(),
                            self.bybit_instruments.clone(),
                            self.bybit.clone(),
                            self.config.sources.bybit_category.clone(),
                            self.config.live.instrument_max_pages,
                        );
                    }
                }
                _ = gate_tick.tick() => {
                    if self.config.llm_gate.enabled && !lanes.gate && lanes.instruments_ready {
                        lanes.gate = true;
                        spawn_gate_lane(lane_tx.clone(), self.config.llm_gate.candidates_path.clone());
                    }
                }
                _ = funding_tick.tick() => {
                    if !lanes.funding && !lanes.instruments && lanes.instruments_ready {
                        lanes.funding = true;
                        self.spawn_funding_lane(lane_tx.clone())?;
                    }
                }
                _ = kline_tick.tick() => {
                    self.flush_pending_klines_or_recover(
                        &mut stream,
                        &mut pending_klines,
                        &lane_tx,
                        &mut lanes,
                    )?;
                    self.advance_kline_watermark(&mut stream, &lane_tx, &mut lanes)?;
                }
                _ = whale_tick.tick() => {
                    if !lanes.whales {
                        lanes.whales = true;
                        self.spawn_whale_lane(lane_tx.clone())?;
                    }
                }
                _ = heartbeat_tick.tick() => {
                    let health = stream.health();
                    let now_ms = wall_ms()?;
                    let status = heartbeat_status(
                        &health,
                        lanes.repair,
                        [
                            (
                                self.last_long_cycle_completed_wall_ts_ms,
                                self.config.live.kline_cadence_ms,
                            ),
                            (
                                self.last_carry_cycle_completed_wall_ts_ms,
                                self.config.live.kline_cadence_ms,
                            ),
                        ],
                        run_started_at_ms,
                        now_ms,
                        self.config.sources.mark_max_age_ms,
                        &mut transient_recovery_started_at_ms,
                    );
                    self.write_heartbeat(status, Some(health))?;
                }
                signal = &mut shutdown => {
                    signal?;
                    self.write_heartbeat("stopped", Some(stream.health()))?;
                    return Ok(());
                }
            }
        }
    }

    fn handle_stream_event(
        &mut self,
        event: StreamEvent,
        stream: &mut BybitPublicStream,
        pending: &mut BTreeMap<(String, i64), ConfirmedKline>,
        pending_limit: usize,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
    ) -> Result<(), WorkerError> {
        match event {
            StreamEvent::EpochStarted {
                epoch, reconnected, ..
            } => {
                if reconnected {
                    eprintln!("signal-worker: Bybit public stream entered epoch {epoch}");
                }
                self.flush_pending_klines_or_recover(stream, pending, lane_tx, lanes)?;
                self.start_kline_repair(lane_tx, lanes, Some(epoch))?;
                if !lanes.tickers {
                    lanes.tickers = true;
                    self.spawn_ticker_lane(lane_tx.clone())?;
                }
            }
            StreamEvent::GapOpened { epoch, .. } => {
                eprintln!("signal-worker: Bybit public stream gap opened in epoch {epoch}");
                self.flush_pending_klines_or_recover(stream, pending, lane_tx, lanes)?;
                self.advance_kline_watermark(stream, lane_tx, lanes)?;
            }
            StreamEvent::KlineClosed(row) => {
                if let Err(error) = normalize_kline_rows(
                    &row.symbol,
                    row.available_at_ms,
                    std::slice::from_ref(&row.row),
                ) {
                    self.recover_stream_source_fault(stream, pending, lane_tx, lanes, error)?;
                    return Ok(());
                }
                let open_ts_ms = wire_i64(row.row.first(), "Bybit WebSocket kline timestamp")?;
                let key = (row.symbol.clone(), open_ts_ms);
                if let Some(existing) = pending.get_mut(&key) {
                    if existing.row != row.row {
                        pending.remove(&key);
                        self.recover_stream_source_fault(
                            stream,
                            pending,
                            lane_tx,
                            lanes,
                            WorkerError::input(format!(
                                "Bybit WebSocket kline rewrote {} at {open_ts_ms}",
                                row.symbol
                            )),
                        )?;
                        return Ok(());
                    }
                    existing.available_at_ms = existing.available_at_ms.min(row.available_at_ms);
                } else {
                    pending.insert(key, row);
                }
                if pending.len() >= pending_limit {
                    self.flush_pending_klines_or_recover(stream, pending, lane_tx, lanes)?;
                }
            }
            StreamEvent::Fault(error) => {
                eprintln!("signal-worker: Bybit public stream: {error}");
            }
        }
        Ok(())
    }

    fn commit_stream_ticker_sample(
        &mut self,
        stream: &mut BybitPublicStream,
        sample: TickerSample,
    ) -> Result<(), WorkerError> {
        let fetched = FetchedTickers {
            request_started_at_ms: sample.observed_ts_ms,
            observed_ts_ms: sample.observed_ts_ms,
            available_at_ms: sample.available_at_ms,
            rows: sample.rows,
        };
        if let Err(error) = validate_stream_ticker_sample(&fetched) {
            lane_source_failure("Bybit WebSocket ticker lane", error)?;
            stream.mark_source_fault(wall_ms()?);
            return Ok(());
        }
        self.commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: fetched.observed_ts_ms,
            available_at_ms: fetched.available_at_ms,
            rows: fetched.rows,
        })
    }

    fn recover_stream_source_fault(
        &mut self,
        stream: &mut BybitPublicStream,
        pending: &mut BTreeMap<(String, i64), ConfirmedKline>,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
        error: WorkerError,
    ) -> Result<(), WorkerError> {
        lane_source_failure("Bybit WebSocket kline lane", error)?;
        stream.mark_source_fault(wall_ms()?);
        match self.prepare_pending_klines(pending) {
            Ok(Some(fetched)) => {
                self.commit_kline_batches(fetched.batches)?;
            }
            Ok(None) => {}
            Err(error) => {
                lane_source_failure("Bybit WebSocket pending kline lane", error)?;
            }
        }
        let health = stream.health();
        let epoch = health.connected.then_some(health.epoch);
        self.start_kline_repair(lane_tx, lanes, epoch)
    }

    fn handle_lane_completion(
        &mut self,
        completion: LaneCompletion,
        stream: &mut BybitPublicStream,
        pending: &mut BTreeMap<(String, i64), ConfirmedKline>,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
    ) -> Result<(), WorkerError> {
        match completion {
            LaneCompletion::Instruments(result) => {
                lanes.instruments = false;
                match result {
                    Ok(fetched) => {
                        if let Err(error) = self.commit_universe_inputs(fetched) {
                            lane_source_failure("instrument lane", error)?;
                            lanes.instruments_ready =
                                !self.durable.worker().state().instruments.is_empty();
                            return Ok(());
                        }
                        if let Err(error) = self.validate_candidate_instruments() {
                            eprintln!("signal-worker: instrument inventory degraded: {error}");
                        }
                        self.reconfigure_stream(stream)?;
                        lanes.instruments_ready = true;
                        if !lanes.repair {
                            self.start_kline_repair(lane_tx, lanes, None)?;
                        }
                        let long_end_ms = closed_kline_end(wall_ms()?);
                        if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                            self.long_watermark(long_end_ms, gap_symbols)?;
                        }
                        if !lanes.funding {
                            lanes.funding = true;
                            self.spawn_funding_lane(lane_tx.clone())?;
                        }
                    }
                    Err(error) => {
                        lane_source_failure("instrument lane", error)?;
                        lanes.instruments_ready =
                            !self.durable.worker().state().instruments.is_empty();
                    }
                }
            }
            LaneCompletion::Gate(result) => {
                lanes.gate = false;
                match result {
                    Ok(Some(fetched)) => {
                        if self.last_gate_decision_ts_ms == Some(fetched.decision_ts_ms) {
                            return Ok(());
                        }
                        let candidates = fetched.rows.len();
                        self.commit(WireEvent::LlmGateCandidates {
                            schema_version: SCHEMA_VERSION,
                            sequence: self.next_sequence()?,
                            observed_ts_ms: fetched.decision_ts_ms.min(fetched.read_at_ms),
                            available_at_ms: fetched.read_at_ms,
                            decision_ts_ms: fetched.decision_ts_ms,
                            valid_until_ms: fetched.valid_until_ms,
                            rows: fetched.rows,
                        })?;
                        self.last_gate_decision_ts_ms = Some(fetched.decision_ts_ms);
                        self.last_gate_candidates = candidates;
                    }
                    Ok(None) => {}
                    Err(error) => lane_source_failure("LLM gate lane", error)?,
                }
            }
            LaneCompletion::Tickers(result) => {
                lanes.tickers = false;
                match result {
                    Ok(mut tickers) => {
                        if let Err(error) = validate_fetched_tickers(&tickers) {
                            lane_source_failure("ticker fallback lane", error)?;
                            self.rest_ticker_failure_count =
                                self.rest_ticker_failure_count.saturating_add(1);
                            self.rest_ticker_last_failure_wall_ts_ms = Some(wall_ms()?);
                            return Ok(());
                        }
                        let health = stream.health();
                        if health.connected {
                            stream.reconcile_tickers(
                                health.epoch,
                                &tickers.rows,
                                tickers.request_started_at_ms,
                                tickers.available_at_ms,
                            );
                            if let Some(sample) = stream.sample_tickers(
                                tickers.available_at_ms,
                                self.config.sources.mark_max_age_ms,
                            ) {
                                tickers.observed_ts_ms = sample.observed_ts_ms;
                                tickers.available_at_ms = sample.available_at_ms;
                                tickers.rows = sample.rows;
                            }
                        }
                        self.commit(WireEvent::BybitTickerSnapshot {
                            schema_version: SCHEMA_VERSION,
                            sequence: self.next_sequence()?,
                            observed_ts_ms: tickers.observed_ts_ms,
                            available_at_ms: tickers.available_at_ms,
                            rows: tickers.rows,
                        })?;
                        self.rest_ticker_success_count =
                            self.rest_ticker_success_count.saturating_add(1);
                        self.rest_ticker_last_success_wall_ts_ms = Some(wall_ms()?);
                    }
                    Err(error) => {
                        lane_source_failure("ticker fallback lane", error)?;
                        self.rest_ticker_failure_count =
                            self.rest_ticker_failure_count.saturating_add(1);
                        self.rest_ticker_last_failure_wall_ts_ms = Some(wall_ms()?);
                    }
                }
            }
            LaneCompletion::FundingChunk { result, resume } => {
                let continue_lane = match result {
                    Ok(fetched) => {
                        if let Err(error) = validate_funding_source_against_state(
                            self.durable.worker().state(),
                            &fetched,
                        ) {
                            lane_source_failure("funding lane chunk", error)?;
                            let _ = resume.send(false);
                            return Ok(());
                        }
                        let failure_count = fetched.failures.len();
                        let samples = fetched
                            .failures
                            .iter()
                            .take(3)
                            .map(|(symbol, error)| format!("{symbol}: {error}"))
                            .collect::<Vec<_>>()
                            .join("; ");
                        let committed = self.commit_funding_batches(fetched.batches)?;
                        if failure_count > 0 {
                            eprintln!(
                                "signal-worker: funding lane chunk: {failure_count} symbol failures; {samples}"
                            );
                        }
                        committed
                    }
                    Err(error) => {
                        lane_source_failure("funding lane chunk", error)?;
                        false
                    }
                };
                let _ = resume.send(continue_lane);
            }
            LaneCompletion::FundingFinished { succeeded } => {
                lanes.funding = false;
                lanes.funding_ready = succeeded;
                self.try_carry_watermark(lanes, Some(lane_tx))?;
            }
            LaneCompletion::WhaleChunk { result, resume } => {
                let continue_lane = match result {
                    Ok(fetched) => {
                        if let Err(error) = validate_whale_source_against_state(
                            self.durable.worker().state(),
                            &fetched,
                        ) {
                            lane_source_failure("Binance whale lane chunk", error)?;
                            true
                        } else {
                            self.commit_whale_batch(fetched)?;
                            true
                        }
                    }
                    Err(error) => {
                        lane_source_failure("Binance whale lane chunk", error)?;
                        false
                    }
                };
                let _ = resume.send(continue_lane);
            }
            LaneCompletion::WhaleFinished => {
                lanes.whales = false;
                self.try_carry_watermark(lanes, Some(lane_tx))?;
            }
            LaneCompletion::RepairChunk { result, resume } => {
                let continue_lane = match result {
                    Ok(fetched) => {
                        if let Err(error) = validate_kline_source_against_state(
                            self.durable.worker().state(),
                            &fetched,
                        ) {
                            let sample = error.to_string();
                            lane_source_failure("kline repair lane chunk", error)?;
                            lanes.repair_failure_count =
                                lanes.repair_failure_count.saturating_add(1);
                            if lanes.repair_failure_samples.len() < 3 {
                                lanes
                                    .repair_failure_samples
                                    .push(("lane".to_owned(), sample.chars().take(160).collect()));
                            }
                            let long_end_ms = closed_kline_end(wall_ms()?);
                            if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                                self.long_watermark(long_end_ms, gap_symbols)?;
                            }
                            let _ = resume.send(false);
                            return Ok(());
                        }
                        let committed = self.commit_kline_batches(fetched.batches)?;
                        lanes.repair_failure_count = lanes
                            .repair_failure_count
                            .saturating_add(fetched.failures.len());
                        for (symbol, error) in fetched.failures {
                            if lanes.repair_failure_samples.len() < 3 {
                                lanes
                                    .repair_failure_samples
                                    .push((symbol, error.chars().take(160).collect()));
                            }
                        }
                        if committed {
                            let long_end_ms = closed_kline_end(wall_ms()?);
                            if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                                self.long_watermark(long_end_ms, gap_symbols)?;
                            }
                        }
                        committed
                    }
                    Err(error) => {
                        let sample = error.to_string();
                        lane_source_failure("kline repair lane chunk", error)?;
                        lanes.repair_failure_count = lanes.repair_failure_count.saturating_add(1);
                        if lanes.repair_failure_samples.len() < 3 {
                            lanes
                                .repair_failure_samples
                                .push(("lane".to_owned(), sample.chars().take(160).collect()));
                        }
                        let long_end_ms = closed_kline_end(wall_ms()?);
                        if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                            self.long_watermark(long_end_ms, gap_symbols)?;
                        }
                        false
                    }
                };
                let _ = resume.send(continue_lane);
            }
            LaneCompletion::RepairFinished { end_ms, epoch } => {
                lanes.repair = false;
                let repaired_epoch = lanes.repair_epoch.or(epoch);
                let pending_committed =
                    self.flush_pending_klines_or_recover(stream, pending, lane_tx, lanes)?;
                if !pending_committed {
                    return Ok(());
                }
                let current_end = closed_kline_end(wall_ms()?);
                let coverage_complete = self.kline_repair_jobs(current_end).is_empty();
                if let Some(epoch) = repaired_epoch {
                    if coverage_complete {
                        stream.mark_gap_repaired(epoch);
                    }
                }
                if lanes.repair_failure_count > 0 {
                    let samples = lanes
                        .repair_failure_samples
                        .iter()
                        .map(|(symbol, error)| format!("{symbol}: {error}"))
                        .collect::<Vec<_>>()
                        .join("; ");
                    eprintln!(
                        "signal-worker: kline repair through {end_ms}: {} failures; {samples}",
                        lanes.repair_failure_count
                    );
                }
                lanes.repair_failure_count = 0;
                lanes.repair_failure_samples.clear();
                if let Some(gap_symbols) = self.long_gap_symbols(end_ms) {
                    self.long_watermark(end_ms, gap_symbols)?;
                }
                self.try_carry_watermark(lanes, Some(lane_tx))?;
            }
        }
        Ok(())
    }

    fn flush_pending_klines_or_recover(
        &mut self,
        stream: &mut BybitPublicStream,
        pending: &mut BTreeMap<(String, i64), ConfirmedKline>,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
    ) -> Result<bool, WorkerError> {
        let fetched = match self.prepare_pending_klines(pending) {
            Ok(Some(fetched)) => fetched,
            Ok(None) => return Ok(true),
            Err(error) if error.is_lane_local_source_failure() => {
                lane_source_failure("Bybit WebSocket pending kline lane", error)?;
                stream.mark_source_fault(wall_ms()?);
                let health = stream.health();
                let epoch = health.connected.then_some(health.epoch);
                self.start_kline_repair(lane_tx, lanes, epoch)?;
                return Ok(false);
            }
            Err(error) => return Err(error),
        };
        self.commit_kline_batches(fetched.batches)
    }

    fn prepare_pending_klines(
        &self,
        pending: &mut BTreeMap<(String, i64), ConfirmedKline>,
    ) -> Result<Option<FetchedKlineJobs>, WorkerError> {
        if pending.is_empty() {
            return Ok(None);
        }
        let staged = std::mem::take(pending);
        let mut grouped = BTreeMap::<String, FetchedKlineBatch>::new();
        for (_, row) in staged {
            let entry = grouped
                .entry(row.symbol)
                .or_insert_with(|| FetchedKlineBatch {
                    rows: Vec::new(),
                    available_at_ms: row.available_at_ms,
                    checked_from_ms: None,
                    checked_through_ms: None,
                });
            entry.rows.push(row.row);
            entry.available_at_ms = entry.available_at_ms.max(row.available_at_ms);
        }
        for batch in grouped.values_mut() {
            batch
                .rows
                .sort_by_key(|row| wire_i64(row.first(), "Bybit WebSocket kline timestamp").ok());
            let opens = batch
                .rows
                .iter()
                .map(|row| wire_i64(row.first(), "Bybit WebSocket kline timestamp"))
                .collect::<Result<Vec<_>, _>>()?;
            if !opens.is_empty()
                && opens
                    .windows(2)
                    .all(|pair| pair[1] == pair[0].saturating_add(HOUR_MS))
            {
                batch.checked_from_ms = opens.first().copied();
                batch.checked_through_ms = opens.last().map(|last| last.saturating_add(HOUR_MS));
            }
        }
        let fetched = FetchedKlineJobs {
            batches: grouped.into_iter().collect(),
            failures: Vec::new(),
        };
        validate_kline_source_against_state(self.durable.worker().state(), &fetched)?;
        Ok(Some(fetched))
    }

    fn commit_kline_batches(
        &mut self,
        fetched: impl IntoIterator<Item = (String, FetchedKlineBatch)>,
    ) -> Result<bool, WorkerError> {
        let mut sequence = self.next_sequence()?;
        let mut events = Vec::new();
        for (symbol, batch) in fetched {
            if batch.rows.is_empty() && batch.checked_through_ms.is_none() {
                continue;
            }
            events.push(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol,
                available_at_ms: batch.available_at_ms,
                checked_from_ms: batch.checked_from_ms,
                checked_through_ms: batch.checked_through_ms,
                replace_coverage: false,
                rows: batch.rows,
            });
            sequence = sequence
                .checked_add(1)
                .ok_or_else(|| WorkerError::state("input sequence exhausted"))?;
        }
        self.commit_many(events)
    }

    fn commit_funding_batches(
        &mut self,
        fetched: Vec<(String, FetchedFundingBatch)>,
    ) -> Result<bool, WorkerError> {
        let mut sequence = self.next_sequence()?;
        let mut events = Vec::with_capacity(fetched.len());
        for (symbol, batch) in fetched {
            let normalized = normalize_funding_rows(&symbol, batch.available_at_ms, &batch.rows)?;
            let existing = self.durable.worker().state().funding.get(&symbol);
            let changes_state = normalized.iter().any(|row| {
                match existing.and_then(|history| history.get(&row.settlement_ts_ms)) {
                    None => true,
                    Some(old) => {
                        old.symbol != row.symbol
                            || old.rate != row.rate
                            || old.funding_interval_min != row.funding_interval_min
                            || row.available_at_ms < old.available_at_ms
                    }
                }
            });
            let state = self.durable.worker().state();
            let replace_coverage = false;
            let coverage_advances = match (batch.checked_from_ms, batch.checked_through_ms) {
                (Some(from), Some(through)) => !source_coverage_contains(
                    &state.funding_checked_from_ms,
                    &state.funding_checked_through_ms,
                    &state.funding_coverage_intervals,
                    &symbol,
                    from,
                    through,
                ),
                (None, None) => false,
                _ => return Err(WorkerError::state("funding fetch coverage is incomplete")),
            };
            if !changes_state && !coverage_advances {
                continue;
            }
            events.push(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol,
                available_at_ms: batch.available_at_ms,
                checked_from_ms: batch.checked_from_ms,
                checked_through_ms: batch.checked_through_ms,
                replace_coverage,
                emit_lifecycle: batch.emit_lifecycle,
                rows: batch.rows,
            });
            sequence = sequence
                .checked_add(1)
                .ok_or_else(|| WorkerError::state("input sequence exhausted"))?;
        }
        self.commit_many(events)
    }

    fn commit_whale_batch(&mut self, mut fetched: FetchedWhales) -> Result<(), WorkerError> {
        let normalized = normalize_whales(fetched.available_at_ms, &fetched.rows)?;
        let state = self.durable.worker().state();
        let changes_state = normalized.iter().any(|row| {
            match state
                .whales
                .get(&row.symbol)
                .and_then(|history| history.get(&row.day_end_ms))
            {
                None => true,
                Some(old) => {
                    old.symbol != row.symbol
                        || old.long_short_ratio != row.long_short_ratio
                        || row.available_at_ms < old.available_at_ms
                }
            }
        });
        for coverage in &mut fetched.coverage {
            coverage.replace_coverage = false;
        }
        let coverage_advances = fetched.coverage.iter().any(|coverage| {
            !source_coverage_contains(
                &state.whale_checked_from_ms,
                &state.whale_checked_through_ms,
                &state.whale_coverage_intervals,
                &coverage.symbol,
                coverage.checked_from_ms,
                coverage.checked_through_ms,
            )
        });
        if !changes_state && !coverage_advances {
            return Ok(());
        }
        self.commit(WireEvent::BinanceWhaleBatch {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            available_at_ms: fetched.available_at_ms,
            coverage: fetched.coverage,
            rows: fetched.rows,
        })
    }

    fn advance_kline_watermark(
        &mut self,
        stream: &mut BybitPublicStream,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
    ) -> Result<(), WorkerError> {
        if lanes.repair {
            return Ok(());
        }
        self.durable.refresh_spool_backpressure()?;
        if self.durable.durability_metrics()?.spool_backpressured {
            return Ok(());
        }
        let now_ms = wall_ms()?;
        let end_ms = closed_kline_end(now_ms);
        let jobs = self.kline_repair_jobs(end_ms);
        let health = stream.health();
        if jobs.is_empty() {
            if let Some(gap_symbols) = self.long_gap_symbols(end_ms) {
                self.long_watermark(end_ms, gap_symbols)?;
            }
            self.try_carry_watermark(lanes, Some(lane_tx))?;
        }
        if health.gap_open || !health.connected || !jobs.is_empty() {
            let epoch = (health.connected && health.gap_open).then_some(health.epoch);
            self.start_kline_repair(lane_tx, lanes, epoch)?;
        }
        Ok(())
    }

    fn start_kline_repair(
        &self,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
        epoch: Option<u64>,
    ) -> Result<(), WorkerError> {
        if epoch.is_some() {
            lanes.repair_epoch = epoch;
        }
        if lanes.repair {
            return Ok(());
        }
        let now_ms = wall_ms()?;
        let end_ms = closed_kline_end(now_ms);
        let mut jobs = self.kline_repair_jobs(end_ms);
        let scheduled: BTreeSet<String> =
            jobs.iter().map(|(symbol, _, _)| symbol.clone()).collect();
        let mut overlap = self
            .kline_symbols()
            .into_iter()
            .filter(|symbol| !scheduled.contains(symbol.as_str()))
            .map(|symbol| {
                let (_, required_end) = self.required_kline_range(&symbol, end_ms);
                (symbol, required_end.saturating_sub(HOUR_MS), required_end)
            })
            .filter(|(_, start, end)| *start > 0 && start < end)
            .collect::<Vec<_>>();
        jobs.append(&mut overlap);
        let mut long_priority = self
            .durable
            .worker()
            .state()
            .universe
            .long_symbols
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        long_priority.insert(self.config.long.regime_symbol.clone());
        long_priority.insert("ETHUSDT".to_owned());
        jobs.sort_by(|left, right| {
            (
                !long_priority.contains(&left.0),
                left.2 != end_ms,
                &left.0,
                left.1,
            )
                .cmp(&(
                    !long_priority.contains(&right.0),
                    right.2 != end_ms,
                    &right.0,
                    right.1,
                ))
        });
        lanes.repair = true;
        spawn_repair_lane(
            lane_tx.clone(),
            self.bybit.clone(),
            self.config.sources.bybit_category.clone(),
            self.config.live.kline_page_limit,
            self.config.live.max_parallel_requests,
            jobs,
            end_ms,
            lanes.repair_epoch,
        );
        Ok(())
    }

    fn kline_repair_jobs(&self, end_ms: i64) -> Vec<(String, i64, i64)> {
        let state = self.durable.worker().state();
        self.owned_symbols()
            .into_iter()
            .flat_map(|symbol| {
                self.required_kline_ranges(&symbol, end_ms)
                    .into_iter()
                    .filter_map(move |(required_start, required_end)| {
                        let start = kline_coverage_repair_start(
                            state,
                            &symbol,
                            required_start,
                            required_end,
                        );
                        if start < required_end {
                            return Some((symbol.clone(), start, required_end));
                        }
                        first_missing_kline_hour(state, &symbol, required_start, required_end).map(
                            |missing| {
                                (
                                    symbol.clone(),
                                    missing,
                                    missing.saturating_add(HOUR_MS).min(required_end),
                                )
                            },
                        )
                    })
            })
            .collect()
    }

    fn spawn_funding_lane(&self, lane_tx: mpsc::Sender<LaneCompletion>) -> Result<(), WorkerError> {
        let now_ms = wall_ms()?;
        let current_end_ms = now_ms.saturating_sub(FUNDING_PUBLICATION_LAG_MS)
            - now_ms
                .saturating_sub(FUNDING_PUBLICATION_LAG_MS)
                .rem_euclid(HOUR_MS);
        let historical_end_ms = self
            .carry_source_through(closed_kline_end(now_ms))
            .min(current_end_ms);
        let state = self.durable.worker().state();
        let intervals = Arc::new(state.instruments.clone());
        let lifecycle_current = state
            .last_carry_decision_ts_ms
            .is_some_and(|last| last >= self.latest_carry_decision(closed_kline_end(now_ms)));
        let history_ms = required_carry_history_hours(&self.config, state).saturating_mul(HOUR_MS);
        let mut jobs = Vec::new();
        for symbol in &state.universe.carry_symbols {
            let interval_ms = intervals
                .get(symbol)
                .and_then(|row| row.funding_interval_min)
                .unwrap_or(60)
                .max(60)
                .saturating_mul(60_000);
            let symbol_current_end_ms = current_end_ms - current_end_ms.rem_euclid(interval_ms);
            let symbol_historical_end_ms =
                historical_end_ms - historical_end_ms.rem_euclid(interval_ms);
            let mut ranges = BTreeMap::<(i64, i64), bool>::new();
            if instrument_trading_at(state, symbol, symbol_historical_end_ms) {
                for range in instrument_source_ranges(
                    state,
                    symbol,
                    symbol_historical_end_ms.saturating_sub(history_ms),
                    symbol_historical_end_ms,
                    interval_ms,
                ) {
                    ranges.entry(range).or_insert(false);
                }
            }
            if current_trading_instrument(state, symbol, &self.config.sources.bybit_settle_coin) {
                for range in instrument_source_ranges(
                    state,
                    symbol,
                    symbol_current_end_ms.saturating_sub(history_ms),
                    symbol_current_end_ms,
                    interval_ms,
                ) {
                    ranges
                        .entry(range)
                        .and_modify(|emit| *emit |= lifecycle_current)
                        .or_insert(lifecycle_current);
                }
            }
            for ((required_start, end_ms), emit_lifecycle) in ranges {
                if required_start >= end_ms {
                    continue;
                }
                if source_coverage_contains(
                    &state.funding_checked_from_ms,
                    &state.funding_checked_through_ms,
                    &state.funding_coverage_intervals,
                    symbol,
                    required_start,
                    end_ms,
                ) {
                    continue;
                }
                let start = source_coverage_repair_start(
                    &state.funding_checked_from_ms,
                    &state.funding_checked_through_ms,
                    &state.funding_coverage_intervals,
                    symbol,
                    required_start,
                    end_ms,
                )
                .min(end_ms.saturating_sub(interval_ms))
                .max(required_start);
                if start < end_ms {
                    jobs.push((symbol.clone(), start, end_ms, emit_lifecycle));
                }
            }
        }
        let client = self.bybit.clone();
        let category = self.config.sources.bybit_category.clone();
        let page_limit = self.config.live.funding_page_limit;
        let parallel = self.config.live.max_parallel_requests;
        spawn_funding_fetch_lane(
            lane_tx, client, category, page_limit, parallel, jobs, intervals,
        );
        Ok(())
    }

    fn spawn_whale_lane(&self, lane_tx: mpsc::Sender<LaneCompletion>) -> Result<(), WorkerError> {
        let now_ms = wall_ms()?;
        let current_end_ms = now_ms - now_ms.rem_euclid(DAY_MS);
        let historical_end_ms = self
            .carry_source_through(closed_kline_end(now_ms))
            .min(current_end_ms);
        let whale_days = i64::try_from(self.config.carry.whale_feed_days)
            .map_err(|_| WorkerError::config("whale feed days exceed i64"))?;
        let state = self.durable.worker().state();
        let history_ms = whale_days.saturating_mul(DAY_MS);
        let mut jobs = Vec::new();
        for symbol in &state.universe.carry_symbols {
            let mut ranges = BTreeSet::new();
            if instrument_trading_at(state, symbol, historical_end_ms) {
                ranges.extend(instrument_source_ranges(
                    state,
                    symbol,
                    historical_end_ms.saturating_sub(history_ms),
                    historical_end_ms,
                    DAY_MS,
                ));
            }
            if current_trading_instrument(state, symbol, &self.config.sources.bybit_settle_coin) {
                ranges.extend(instrument_source_ranges(
                    state,
                    symbol,
                    current_end_ms.saturating_sub(history_ms),
                    current_end_ms,
                    DAY_MS,
                ));
            }
            for (required_start, end_ms) in ranges {
                if source_coverage_contains(
                    &state.whale_checked_from_ms,
                    &state.whale_checked_through_ms,
                    &state.whale_coverage_intervals,
                    symbol,
                    required_start,
                    end_ms,
                ) {
                    continue;
                }
                let append_from = source_coverage_repair_start(
                    &state.whale_checked_from_ms,
                    &state.whale_checked_through_ms,
                    &state.whale_coverage_intervals,
                    symbol,
                    required_start,
                    end_ms,
                );
                let start = append_from
                    .min(end_ms.saturating_sub(DAY_MS))
                    .max(required_start);
                if start < end_ms {
                    jobs.push((symbol.clone(), start, end_ms));
                }
            }
        }
        let client = self.binance.clone();
        let page_limit = self.config.live.whale_page_limit;
        let parallel = self.config.live.max_parallel_requests;
        spawn_whale_fetch_lane(lane_tx, client, page_limit, parallel, jobs);
        Ok(())
    }

    fn spawn_ticker_lane(&self, lane_tx: mpsc::Sender<LaneCompletion>) -> Result<(), WorkerError> {
        let client = self.bybit.clone();
        let category = self.config.sources.bybit_category.clone();
        let allowed = self.kline_symbols().into_iter().collect();
        tokio::spawn(async move {
            let result = fetch_ticker_snapshot(client, category, allowed).await;
            let _ = lane_tx.send(LaneCompletion::Tickers(result)).await;
        });
        Ok(())
    }

    async fn refresh_instruments(&mut self) -> Result<(), WorkerError> {
        let fetched = fetch_universe_inputs(
            self.bybit_instruments.clone(),
            self.bybit.clone(),
            self.config.sources.bybit_category.clone(),
            self.config.live.instrument_max_pages,
        )
        .await?;
        self.commit_universe_inputs(fetched)?;
        self.validate_candidate_instruments()
    }

    /// Derive the universe from a fresh venue page pair, install it when its
    /// membership moved, then record the instrument snapshot the owned symbols
    /// are read from. The universe goes first so a symbol that just entered has
    /// instrument facts in the same commit.
    fn commit_universe_inputs(
        &mut self,
        fetched: FetchedUniverseInputs,
    ) -> Result<(), WorkerError> {
        validate_instrument_source_against_state(
            self.durable.worker().state(),
            &fetched.instruments,
        )?;
        validate_fetched_tickers(&fetched.tickers)?;
        let (instruments, rejected) = crate::normalize::normalize_instruments_reporting(
            fetched.instruments.observed_ts_ms,
            fetched.instruments.available_at_ms,
            &fetched.instruments.rows,
        )?;
        if let Some(summary) = rejected.summary("instrument") {
            eprintln!("signal-worker: instrument lane: {summary}");
        }
        let (tickers, rejected) = crate::normalize::normalize_tickers_reporting(
            fetched.tickers.observed_ts_ms,
            fetched.tickers.available_at_ms,
            &fetched.tickers.rows,
        )?;
        if let Some(summary) = rejected.summary("ticker") {
            eprintln!("signal-worker: instrument lane: {summary}");
        }
        let (current_resolved, previous) = {
            let state = self.durable.worker().state();
            (
                crate::universe::universe_is_resolved(&state.universe),
                state.universe.clone(),
            )
        };
        let derived = crate::universe::derive_universe(
            &self.config.universe,
            crate::universe::UniverseInputs {
                environment: &self.config.live.environment,
                endpoint: crate::worker::realm_endpoint(&self.config),
                snapshot_ts_ms: fetched
                    .instruments
                    .observed_ts_ms
                    .min(fetched.tickers.observed_ts_ms),
                available_at_ms: fetched
                    .instruments
                    .available_at_ms
                    .max(fetched.tickers.available_at_ms),
                instruments: &instruments,
                tickers: &tickers,
                previous: current_resolved.then_some(&previous),
            },
        )?;
        if !current_resolved || !crate::universe::same_membership(&previous, &derived) {
            self.commit(WireEvent::UniverseSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: self.next_sequence()?,
                universe: derived,
            })?;
        }
        self.commit(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: fetched.instruments.observed_ts_ms,
            available_at_ms: fetched.instruments.available_at_ms,
            rows: fetched.instruments.rows,
        })
    }

    async fn refresh_tickers(&mut self) -> Result<(), WorkerError> {
        let query = format!(
            "category={}",
            percent_encode(&self.config.sources.bybit_category)
        );
        let (payload, available) = self.bybit.get("/v5/market/tickers", &query).await?;
        let allowed: BTreeSet<String> = self.kline_symbols().into_iter().collect();
        let rows = result_list(bybit_result(&payload)?)?
            .iter()
            .filter(|value| {
                value
                    .get("symbol")
                    .and_then(Value::as_str)
                    .is_some_and(|symbol| allowed.contains(&symbol.to_ascii_uppercase()))
            })
            .map(ticker_wire)
            .collect::<Result<Vec<_>, _>>()?;
        self.commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: available,
            available_at_ms: available,
            rows,
        })
    }

    async fn refresh_klines(&mut self, start: i64, end: i64) -> Result<(), WorkerError> {
        if end <= start {
            return Ok(());
        }
        let jobs = self
            .kline_symbols()
            .into_iter()
            .flat_map(|symbol| {
                self.required_kline_ranges(&symbol, end).into_iter().map(
                    move |(symbol_start, symbol_end)| {
                        (symbol.clone(), symbol_start.max(start), symbol_end)
                    },
                )
            })
            .filter(|(_, symbol_start, symbol_end)| symbol_start < symbol_end)
            .collect::<Vec<_>>();
        for chunk in kline_job_chunks(&jobs) {
            let fetched = self.fetch_kline_jobs(chunk.to_vec()).await?;
            if !self.commit_kline_batches(fetched.batches)? {
                return Err(WorkerError::state(
                    "cold kline hydration paused by signal spool backpressure",
                ));
            }
            if !fetched.failures.is_empty() {
                return Err(WorkerError::network(format!(
                    "cold kline hydration failed for {} symbols: {}",
                    fetched.failures.len(),
                    fetched
                        .failures
                        .iter()
                        .take(3)
                        .map(|(symbol, error)| format!("{symbol}: {error}"))
                        .collect::<Vec<_>>()
                        .join("; ")
                )));
            }
        }
        Ok(())
    }

    async fn fetch_kline_jobs(
        &self,
        jobs: Vec<(String, i64, i64)>,
    ) -> Result<FetchedKlineJobs, WorkerError> {
        fetch_kline_jobs_bounded(
            self.bybit.clone(),
            self.config.sources.bybit_category.clone(),
            self.config.live.kline_page_limit,
            self.config.live.max_parallel_requests,
            jobs,
        )
        .await
    }

    async fn refresh_funding(&mut self, start: i64, end: i64) -> Result<(), WorkerError> {
        if end <= start {
            return Ok(());
        }
        let state = self.durable.worker().state();
        let instruments = Arc::new(state.instruments.clone());
        let jobs = state
            .universe
            .carry_symbols
            .iter()
            .map(|symbol| (symbol.clone(), start, end, false))
            .collect::<Vec<_>>();
        let mut failures = Vec::new();
        for chunk in funding_job_chunks(&jobs) {
            let fetched = fetch_funding_batches(
                self.bybit.clone(),
                self.config.sources.bybit_category.clone(),
                self.config.live.funding_page_limit,
                self.config.live.max_parallel_requests,
                chunk.to_vec(),
                Arc::clone(&instruments),
            )
            .await?;
            if !self.commit_funding_batches(fetched.batches)? {
                return Err(WorkerError::state(
                    "cold funding hydration paused by signal spool backpressure",
                ));
            }
            failures.extend(fetched.failures);
        }
        if failures.is_empty() {
            return Ok(());
        }
        Err(WorkerError::network(format!(
            "cold funding hydration failed for {} symbols: {}",
            failures.len(),
            failures
                .iter()
                .take(3)
                .map(|(symbol, error)| format!("{symbol}: {error}"))
                .collect::<Vec<_>>()
                .join("; ")
        )))
    }

    async fn refresh_whales(&mut self, start: i64, end: i64) -> Result<(), WorkerError> {
        let jobs = self
            .durable
            .worker()
            .state()
            .universe
            .carry_symbols
            .iter()
            .map(|symbol| (symbol.clone(), start, end))
            .collect::<Vec<_>>();
        for chunk in whale_job_chunks(&jobs) {
            let fetched = fetch_whale_batch(
                self.binance.clone(),
                self.config.live.whale_page_limit,
                self.config.live.max_parallel_requests,
                chunk.to_vec(),
            )
            .await?;
            self.commit_whale_batch(fetched)?;
        }
        Ok(())
    }

    fn long_watermark(
        &mut self,
        data_through_ms: i64,
        gap_symbols: Vec<String>,
    ) -> Result<(), WorkerError> {
        let observed_ts_ms = wall_ms()?.max(self.durable.worker().state().last_observed_ts_ms);
        let skipped_before = self.durable.worker().state().long_skipped_generation_count;
        let committed = self.commit_with_receipt(WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms,
            data_through_ms,
            gap_symbols,
        })?;
        if committed {
            self.last_long_cycle_completed_wall_ts_ms = Some(wall_ms()?);
        }
        let state = self.durable.worker().state();
        if state.long_skipped_generation_count > skipped_before {
            eprintln!(
                "signal-worker: LONG fast-forward skipped {} stale generations from {} through {}",
                state.long_skipped_generation_count - skipped_before,
                state.last_long_skipped_first_ts_ms.unwrap_or_default(),
                state.last_long_skipped_last_ts_ms.unwrap_or_default(),
            );
        }
        Ok(())
    }

    fn carry_watermark(
        &mut self,
        data_through_ms: i64,
        gap_symbols: Vec<String>,
    ) -> Result<(), WorkerError> {
        let observed_ts_ms = wall_ms()?.max(self.durable.worker().state().last_observed_ts_ms);
        let committed = self.commit_with_receipt(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms,
            data_through_ms,
            gap_symbols,
        })?;
        if committed {
            self.last_carry_cycle_completed_wall_ts_ms = Some(wall_ms()?);
        }
        Ok(())
    }

    fn try_carry_watermark(
        &mut self,
        lanes: &mut LaneState,
        lane_tx: Option<&mpsc::Sender<LaneCompletion>>,
    ) -> Result<(), WorkerError> {
        if carry_required_lanes_pending(lanes) {
            return Ok(());
        }
        let now_ms = wall_ms()?;
        let data_through_ms = closed_kline_end(now_ms);
        let latest_decision_ms = self.latest_carry_decision(data_through_ms);
        let source_through_ms = self.carry_source_through(data_through_ms);
        if source_through_ms <= 0 || latest_decision_ms <= 0 {
            return Ok(());
        }
        let state = self.durable.worker().state();
        let required = self
            .config
            .carry
            .minimum_decision_symbols
            .min(state.universe.carry_symbols.len());
        let historical_target = source_through_ms < latest_decision_ms;
        let active_symbols = state
            .universe
            .carry_symbols
            .iter()
            .filter(|symbol| {
                if historical_target {
                    instrument_trading_at(state, symbol, source_through_ms)
                } else {
                    current_trading_instrument(
                        state,
                        symbol,
                        &self.config.sources.bybit_settle_coin,
                    )
                }
            })
            .cloned()
            .collect::<Vec<_>>();
        let gap_symbols = active_symbols
            .iter()
            .filter(|symbol| {
                let required_start = self.required_carry_start(source_through_ms);
                let funding_interval_ms = state
                    .instruments
                    .get(symbol.as_str())
                    .and_then(|row| row.funding_interval_min)
                    .unwrap_or(60)
                    .max(60)
                    .saturating_mul(60_000);
                let kline_ranges = instrument_source_ranges(
                    state,
                    symbol,
                    required_start,
                    source_through_ms,
                    HOUR_MS,
                );
                let funding_ranges = instrument_source_ranges(
                    state,
                    symbol,
                    required_start,
                    source_through_ms,
                    funding_interval_ms,
                );
                !kline_ranges.iter().all(|(start, through)| {
                    kline_coverage_contains(state, symbol, *start, *through)
                        && first_missing_kline_hour(state, symbol, *start, *through).is_none()
                }) || !funding_ranges.iter().all(|(start, through)| {
                    source_coverage_contains(
                        &state.funding_checked_from_ms,
                        &state.funding_checked_through_ms,
                        &state.funding_coverage_intervals,
                        symbol,
                        *start,
                        *through,
                    )
                })
            })
            .cloned()
            .collect::<Vec<_>>();
        let covered = active_symbols.len().saturating_sub(gap_symbols.len());
        if covered < required {
            return Ok(());
        }
        if let Some(last) = self
            .durable
            .worker()
            .state()
            .last_carry_scorer_ts_ms
            .or(self.durable.worker().state().last_carry_decision_ts_ms)
        {
            let catchup_through_ms = source_through_ms.min(latest_decision_ms - DAY_MS);
            if catchup_through_ms > last {
                let observed_ts_ms =
                    wall_ms()?.max(self.durable.worker().state().last_observed_ts_ms);
                let committed =
                    self.commit_with_receipt(WireEvent::CarryScorerCatchupWatermark {
                        schema_version: SCHEMA_VERSION,
                        sequence: self.next_sequence()?,
                        observed_ts_ms,
                        decision_through_ms: catchup_through_ms,
                        gap_symbols: gap_symbols.clone(),
                    })?;
                if committed {
                    self.last_carry_cycle_completed_wall_ts_ms = Some(wall_ms()?);
                }
            }
        }
        let catchup_pending = self
            .durable
            .worker()
            .state()
            .last_carry_scorer_ts_ms
            .or(self.durable.worker().state().last_carry_decision_ts_ms)
            .is_some_and(|last| last < latest_decision_ms.saturating_sub(DAY_MS));
        if catchup_pending {
            self.durable.refresh_spool_backpressure()?;
            if self.durable.spool_backpressured_for("catchup") {
                return Ok(());
            }
            if let Some(lane_tx) = lane_tx {
                if !lanes.repair {
                    self.start_kline_repair(lane_tx, lanes, None)?;
                }
                if !lanes.funding && !lanes.instruments && lanes.instruments_ready {
                    lanes.funding = true;
                    self.spawn_funding_lane(lane_tx.clone())?;
                }
                if !lanes.whales {
                    lanes.whales = true;
                    self.spawn_whale_lane(lane_tx.clone())?;
                }
            }
            return Ok(());
        }
        if source_through_ms >= latest_decision_ms {
            self.carry_watermark(data_through_ms, gap_symbols)?;
        }
        Ok(())
    }

    fn long_gap_symbols(&self, data_through_ms: i64) -> Option<Vec<String>> {
        let state = self.durable.worker().state();
        for symbol in [self.config.long.regime_symbol.as_str(), "ETHUSDT"] {
            if !current_trading_instrument(state, symbol, &self.config.sources.bybit_settle_coin) {
                return None;
            }
            let ranges = instrument_source_ranges(
                state,
                symbol,
                self.required_long_start(data_through_ms),
                data_through_ms,
                HOUR_MS,
            );
            if ranges.is_empty()
                || ranges.iter().any(|(start, through)| {
                    !kline_coverage_contains(state, symbol, *start, *through)
                        || first_missing_kline_hour(state, symbol, *start, *through).is_some()
                })
            {
                return None;
            }
        }
        Some(
            state
                .universe
                .long_symbols
                .iter()
                .filter(|symbol| {
                    current_trading_instrument(
                        state,
                        symbol,
                        &self.config.sources.bybit_settle_coin,
                    )
                })
                .filter(|symbol| {
                    let ranges = instrument_source_ranges(
                        state,
                        symbol,
                        self.required_long_start(data_through_ms),
                        data_through_ms,
                        HOUR_MS,
                    );
                    ranges.is_empty()
                        || ranges.iter().any(|(start, through)| {
                            !kline_coverage_contains(state, symbol, *start, *through)
                                || first_missing_kline_hour(state, symbol, *start, *through)
                                    .is_some()
                        })
                })
                .cloned()
                .collect(),
        )
    }

    fn next_sequence(&self) -> Result<u64, WorkerError> {
        self.durable.worker().next_input_sequence()
    }

    fn commit(&mut self, event: WireEvent) -> Result<(), WorkerError> {
        self.durable.apply_and_commit(event).map(|_| ())
    }

    fn commit_with_receipt(&mut self, event: WireEvent) -> Result<bool, WorkerError> {
        let sequence = event.sequence();
        self.durable.apply_and_commit(event)?;
        Ok(self.durable.worker().state().last_input_sequence >= sequence)
    }

    fn commit_many(&mut self, events: Vec<WireEvent>) -> Result<bool, WorkerError> {
        Ok(self
            .durable
            .apply_many_and_commit(events)?
            .fully_committed())
    }

    fn kline_symbols(&self) -> Vec<String> {
        let state = self.durable.worker().state();
        let mut symbols = self.owned_symbols().into_iter().collect::<BTreeSet<_>>();
        if !state.instruments.is_empty() {
            symbols.retain(|symbol| {
                symbol == &self.config.long.regime_symbol
                    || symbol == "BTCUSDT"
                    || symbol == "ETHUSDT"
                    || state.instruments.get(symbol).is_some_and(|row| {
                        row.status.as_deref() == Some("Trading")
                            && row.settle_coin.as_deref()
                                == Some(self.config.sources.bybit_settle_coin.as_str())
                            && !row.is_prelisting
                    })
            });
        }
        symbols.into_iter().collect()
    }

    fn owned_symbols(&self) -> Vec<String> {
        let state = self.durable.worker().state();
        let mut symbols: BTreeSet<String> = state
            .universe
            .long_symbols
            .iter()
            .chain(&state.universe.carry_symbols)
            .cloned()
            .collect();
        symbols.insert(self.config.long.regime_symbol.clone());
        symbols.insert("ETHUSDT".to_owned());
        symbols.into_iter().collect()
    }

    fn stream_symbols(&self) -> Vec<String> {
        let state = self.durable.worker().state();
        let mut critical = BTreeSet::from([
            self.config.long.regime_symbol.clone(),
            "BTCUSDT".to_owned(),
            "ETHUSDT".to_owned(),
        ]);
        if state.instruments.is_empty() {
            return critical.into_iter().collect();
        }
        for symbol in self.kline_symbols() {
            if state.instruments.get(&symbol).is_some_and(|row| {
                row.status.as_deref() == Some("Trading")
                    && row.settle_coin.as_deref()
                        == Some(self.config.sources.bybit_settle_coin.as_str())
                    && !row.is_prelisting
            }) {
                critical.insert(symbol);
            }
        }
        critical.into_iter().collect()
    }

    fn reconfigure_stream(&self, stream: &mut BybitPublicStream) -> Result<(), WorkerError> {
        let desired = self.stream_symbols().into_iter().collect::<BTreeSet<_>>();
        if &desired == stream.symbols() {
            return Ok(());
        }
        *stream = BybitPublicStream::spawn(
            desired.into_iter().collect(),
            self.config.live.request_timeout_ms,
            self.config.live.retry_base_ms,
        )?;
        Ok(())
    }

    fn required_kline_range(&self, symbol: &str, end_ms: i64) -> (i64, i64) {
        let ranges = self.required_kline_ranges(symbol, end_ms);
        let start = ranges
            .iter()
            .map(|(start, _)| *start)
            .min()
            .unwrap_or(end_ms);
        let through = ranges
            .iter()
            .map(|(_, through)| *through)
            .max()
            .unwrap_or(end_ms);
        (start, through)
    }

    fn required_kline_ranges(&self, symbol: &str, end_ms: i64) -> Vec<(i64, i64)> {
        let state = self.durable.worker().state();
        let long_support = state
            .universe
            .long_symbols
            .iter()
            .any(|value| value == symbol)
            || symbol == self.config.long.regime_symbol
            || symbol == "ETHUSDT";
        let carry_support = state
            .universe
            .carry_symbols
            .iter()
            .any(|value| value == symbol);
        let carry_end_ms = self.carry_source_through(end_ms);
        let mut ranges = Vec::new();
        if long_support
            && current_trading_instrument(state, symbol, &self.config.sources.bybit_settle_coin)
        {
            ranges.extend(instrument_source_ranges(
                state,
                symbol,
                self.required_long_start(end_ms),
                end_ms,
                HOUR_MS,
            ));
        }
        if carry_support {
            let latest_carry_decision_ms = self.latest_carry_decision(end_ms);
            if carry_end_ms < latest_carry_decision_ms {
                if instrument_trading_at(state, symbol, carry_end_ms) {
                    ranges.extend(instrument_source_ranges(
                        state,
                        symbol,
                        self.required_carry_start(carry_end_ms),
                        carry_end_ms,
                        HOUR_MS,
                    ));
                }
                if current_trading_instrument(state, symbol, &self.config.sources.bybit_settle_coin)
                {
                    ranges.extend(instrument_source_ranges(
                        state,
                        symbol,
                        self.required_carry_start(latest_carry_decision_ms),
                        latest_carry_decision_ms,
                        HOUR_MS,
                    ));
                }
            } else if current_trading_instrument(
                state,
                symbol,
                &self.config.sources.bybit_settle_coin,
            ) {
                ranges.extend(instrument_source_ranges(
                    state,
                    symbol,
                    self.required_carry_start(carry_end_ms),
                    carry_end_ms,
                    HOUR_MS,
                ));
            }
        }
        ranges.sort_by_key(|(start, _)| *start);
        let mut merged = Vec::<(i64, i64)>::new();
        for range in ranges {
            if let Some(last) = merged.last_mut() {
                if range.0 <= last.1 {
                    last.1 = last.1.max(range.1);
                    continue;
                }
            }
            merged.push(range);
        }
        merged
    }

    fn required_long_start(&self, end_ms: i64) -> i64 {
        end_ms.saturating_sub(
            i64::try_from(self.config.long.cold_start_lookback_days)
                .unwrap_or(i64::MAX / 24)
                .saturating_mul(24)
                .saturating_add(48)
                .saturating_mul(HOUR_MS),
        )
    }

    fn required_carry_start(&self, end_ms: i64) -> i64 {
        end_ms.saturating_sub(
            required_carry_history_hours(&self.config, self.durable.worker().state())
                .saturating_mul(HOUR_MS),
        )
    }

    fn carry_source_through(&self, current_end_ms: i64) -> i64 {
        let latest_decision_ms = self.latest_carry_decision(current_end_ms);
        self.durable
            .worker()
            .state()
            .last_carry_scorer_ts_ms
            .or(self.durable.worker().state().last_carry_decision_ts_ms)
            .map(|last| {
                last.saturating_add(CARRY_CATCHUP_CHUNK_DAYS.saturating_mul(DAY_MS))
                    .min(latest_decision_ms)
                    .max(0)
            })
            .unwrap_or(current_end_ms)
    }

    fn latest_carry_decision(&self, current_end_ms: i64) -> i64 {
        let day = current_end_ms - current_end_ms.rem_euclid(DAY_MS);
        let mut latest_decision_ms = day.saturating_add(self.config.carry.decision_phase_ms);
        if current_end_ms
            < latest_decision_ms.saturating_add(self.config.carry.decision_kline_lag_ms)
        {
            latest_decision_ms = latest_decision_ms.saturating_sub(DAY_MS);
        }
        latest_decision_ms.max(0)
    }

    fn needs_cold_bootstrap(&self) -> bool {
        let state = self.durable.worker().state();
        let Some(coverage) = state.bootstrap_coverage.as_ref() else {
            return true;
        };
        if coverage.completed_at_ms <= 0
            || coverage.kline_end_ms <= 0
            || coverage.funding_end_ms <= 0
            || coverage.whale_end_ms <= 0
            || coverage.kline_end_ms > coverage.completed_at_ms
            || coverage.funding_end_ms > coverage.completed_at_ms
            || coverage.whale_end_ms > coverage.completed_at_ms
            || coverage.source_contract_sha256 != state.source_contract_sha256
            || coverage.long_feature_sha256 != state.long_feature_sha256
            || coverage.carry_feature_sha256 != state.carry_feature_sha256
        {
            return true;
        }
        let kline_incomplete = self.owned_symbols().iter().any(|symbol| {
            self.required_kline_ranges(symbol, coverage.kline_end_ms)
                .into_iter()
                .any(|(required_start, required_through)| {
                    !kline_coverage_contains(state, symbol, required_start, required_through)
                        || first_missing_kline_hour(state, symbol, required_start, required_through)
                            .is_some()
                })
        });
        let funding_incomplete = state.universe.carry_symbols.iter().any(|symbol| {
            if !instrument_trading_at(state, symbol, coverage.funding_end_ms) {
                return false;
            }
            let interval_ms = state
                .instruments
                .get(symbol)
                .and_then(|row| row.funding_interval_min)
                .unwrap_or(60)
                .max(60)
                .saturating_mul(60_000);
            instrument_source_ranges(
                state,
                symbol,
                self.required_carry_start(coverage.funding_end_ms),
                coverage.funding_end_ms,
                interval_ms,
            )
            .into_iter()
            .any(|(start, through)| {
                !source_coverage_contains(
                    &state.funding_checked_from_ms,
                    &state.funding_checked_through_ms,
                    &state.funding_coverage_intervals,
                    symbol,
                    start,
                    through,
                )
            })
        });
        let instruments_incomplete = state
            .universe
            .symbols
            .iter()
            .any(|symbol| !state.instruments.contains_key(symbol));
        kline_incomplete || funding_incomplete || instruments_incomplete
    }

    fn validate_candidate_instruments(&self) -> Result<(), WorkerError> {
        let state = self.durable.worker().state();
        let mut invalid = Vec::new();
        for symbol in self.owned_symbols() {
            if symbol == self.config.long.regime_symbol || symbol == "ETHUSDT" {
                continue;
            }
            let Some(row) = state.instruments.get(&symbol) else {
                invalid.push(format!("{symbol}:absent"));
                continue;
            };
            if row.settle_coin.as_deref() != Some(self.config.sources.bybit_settle_coin.as_str())
                || row.contract_type.as_deref() != Some("LinearPerpetual")
            {
                invalid.push(format!("{symbol}:not_usdt_linear"));
            }
        }
        if invalid.is_empty() {
            Ok(())
        } else {
            let samples = invalid
                .iter()
                .take(3)
                .cloned()
                .collect::<Vec<_>>()
                .join(", ");
            Err(WorkerError::network(format!(
                "{} universe members lack recognized Bybit metadata; {samples}",
                invalid.len()
            )))
        }
    }

    fn write_heartbeat(
        &self,
        status: &str,
        stream_health: Option<StreamHealth>,
    ) -> Result<(), WorkerError> {
        let state = self.durable.worker().state();
        let durability = self.durable.durability_metrics()?;
        let stream_health = stream_health.unwrap_or_default();
        let heartbeat = WorkerHeartbeat {
            schema_version: SCHEMA_VERSION,
            kind: "liquidity_migration_signal_worker_heartbeat".to_owned(),
            status: status.to_owned(),
            pid: std::process::id(),
            updated_at_ms: wall_ms()?,
            public_market_realm: self.config.live.public_market_realm.clone(),
            public_bybit_host: self.config.sources.bybit_mainnet_host.clone(),
            credential_free: true,
            signal_config_sha256: self.config.identity.signal_config_sha256.clone(),
            long_rule_sha256: self.config.identity.long_rule_sha256.clone(),
            long_feature_contract_sha256: self.config.identity.long_feature_contract_sha256.clone(),
            carry_config_sha256: self.config.identity.carry_rule_sha256.clone(),
            carry_feature_contract_sha256: self
                .config
                .identity
                .carry_feature_contract_sha256
                .clone(),
            operational_config_sha256: self.config.identity.operational_profile_sha256.clone(),
            engine_config_sha256: self.config.identity.engine_config_sha256.clone(),
            universe_artifact_sha256: state.universe.artifact_sha256.clone(),
            universe_file_sha256: state.universe.file_sha256.clone(),
            universe_snapshot_ts_ms: state.universe.snapshot_ts_ms,
            universe_symbols: state.universe.symbols.len(),
            universe_long_symbols: state.universe.long_symbols.len(),
            universe_carry_symbols: state.universe.carry_symbols.len(),
            llm_gate_enabled: self.config.llm_gate.enabled,
            llm_gate_last_decision_ts_ms: self.last_gate_decision_ts_ms,
            llm_gate_last_candidates: self.last_gate_candidates,
            source_generation: state.source_generation.clone(),
            last_input_sequence: state.last_input_sequence,
            long_output_sequence: state.long_output_sequence,
            carry_output_sequence: state.carry_output_sequence,
            last_observed_ts_ms: state.last_observed_ts_ms,
            last_long_feature_ts_ms: state.last_long_feature_ts_ms,
            long_skipped_generation_count: state.long_skipped_generation_count,
            last_long_skipped_first_ts_ms: state.last_long_skipped_first_ts_ms,
            last_long_skipped_last_ts_ms: state.last_long_skipped_last_ts_ms,
            last_carry_decision_ts_ms: state.last_carry_decision_ts_ms,
            last_carry_scorer_ts_ms: state.last_carry_scorer_ts_ms,
            last_carry_upcoming_ts_ms: state.last_carry_upcoming_ts_ms,
            last_long_cycle_completed_wall_ts_ms: self.last_long_cycle_completed_wall_ts_ms,
            last_carry_cycle_completed_wall_ts_ms: self.last_carry_cycle_completed_wall_ts_ms,
            long_cycle_cadence_ms: self.config.live.kline_cadence_ms,
            carry_cycle_cadence_ms: self.config.live.kline_cadence_ms,
            rest_ticker_last_success_wall_ts_ms: self.rest_ticker_last_success_wall_ts_ms,
            rest_ticker_last_failure_wall_ts_ms: self.rest_ticker_last_failure_wall_ts_ms,
            rest_ticker_success_count: self.rest_ticker_success_count,
            rest_ticker_failure_count: self.rest_ticker_failure_count,
            bybit_ws_connected: stream_health.connected,
            bybit_ws_epoch: stream_health.epoch,
            bybit_ws_gap_open: stream_health.gap_open,
            bybit_ws_gap_open_since_wall_ts_ms: stream_health.gap_open_since_ms,
            bybit_ws_reconnect_count: stream_health.reconnect_count,
            bybit_ws_fault_count: stream_health.fault_count,
            bybit_ws_last_frame_ts_ms: stream_health.last_frame_ts_ms,
            bybit_ws_ticker_rows: stream_health.ticker_rows,
            bybit_ws_ticker_capacity: stream_health.ticker_capacity,
            bybit_ws_ticker_coverage_complete: stream_health.ticker_coverage_complete,
            bybit_ws_ticker_topics_accepted: stream_health.ticker_topics_accepted,
            bybit_ws_ticker_topics_quarantined: stream_health.ticker_topics_quarantined,
            bybit_ws_kline_topics_accepted: stream_health.kline_topics_accepted,
            bybit_ws_kline_topics_quarantined: stream_health.kline_topics_quarantined,
            bybit_ws_queued_frames: stream_health.queued_frames,
            bybit_ws_queue_capacity: stream_health.queue_capacity,
            spool_files: durability.spool_files,
            spool_bytes: durability.spool_bytes,
            spool_file_cap: durability.spool_file_cap,
            spool_byte_cap: durability.spool_byte_cap,
            spool_byte_soft_threshold: durability.spool_byte_soft_threshold,
            replaceable_outputs_coalesced: durability.replaceable_outputs_coalesced,
            spool_backpressured: durability.spool_backpressured,
            spool_class_files: durability.spool_class_files,
            spool_class_bytes: durability.spool_class_bytes,
            spool_class_file_caps: durability.spool_class_file_caps,
            spool_class_byte_caps: durability.spool_class_byte_caps,
            spool_class_byte_soft_thresholds: durability.spool_class_byte_soft_thresholds,
            spool_backpressured_classes: durability.spool_backpressured_classes,
        };
        let bytes = serde_json::to_vec(&heartbeat)
            .map_err(|error| WorkerError::json("encode worker heartbeat", error))?;
        atomic_write(&self.heartbeat_path, &bytes)
    }
}

fn write_provisional_heartbeat(
    config: &SignalWorkerConfig,
    universe: Option<&crate::model::UniverseIdentity>,
    path: &Path,
    status: &str,
) -> Result<(), WorkerError> {
    let ticker_capacity = universe.map_or(0, |universe| {
        universe
            .long_symbols
            .iter()
            .chain(&universe.carry_symbols)
            .map(String::as_str)
            .chain([config.long.regime_symbol.as_str(), "ETHUSDT"])
            .collect::<BTreeSet<_>>()
            .len()
    });
    let heartbeat = WorkerHeartbeat {
        schema_version: SCHEMA_VERSION,
        kind: "liquidity_migration_signal_worker_heartbeat".to_owned(),
        status: status.to_owned(),
        pid: std::process::id(),
        updated_at_ms: wall_ms()?,
        public_market_realm: config.live.public_market_realm.clone(),
        public_bybit_host: config.sources.bybit_mainnet_host.clone(),
        credential_free: true,
        signal_config_sha256: config.identity.signal_config_sha256.clone(),
        long_rule_sha256: config.identity.long_rule_sha256.clone(),
        long_feature_contract_sha256: config.identity.long_feature_contract_sha256.clone(),
        carry_config_sha256: config.identity.carry_rule_sha256.clone(),
        carry_feature_contract_sha256: config.identity.carry_feature_contract_sha256.clone(),
        operational_config_sha256: config.identity.operational_profile_sha256.clone(),
        engine_config_sha256: config.identity.engine_config_sha256.clone(),
        universe_artifact_sha256: universe
            .map(|u| u.artifact_sha256.clone())
            .unwrap_or_default(),
        universe_file_sha256: universe.map(|u| u.file_sha256.clone()).unwrap_or_default(),
        universe_snapshot_ts_ms: universe.map_or(0, |u| u.snapshot_ts_ms),
        universe_symbols: universe.map_or(0, |u| u.symbols.len()),
        universe_long_symbols: universe.map_or(0, |u| u.long_symbols.len()),
        universe_carry_symbols: universe.map_or(0, |u| u.carry_symbols.len()),
        llm_gate_enabled: config.llm_gate.enabled,
        llm_gate_last_decision_ts_ms: None,
        llm_gate_last_candidates: 0,
        source_generation: String::new(),
        last_input_sequence: 0,
        long_output_sequence: 0,
        carry_output_sequence: 0,
        last_observed_ts_ms: 0,
        last_long_feature_ts_ms: None,
        long_skipped_generation_count: 0,
        last_long_skipped_first_ts_ms: None,
        last_long_skipped_last_ts_ms: None,
        last_carry_decision_ts_ms: None,
        last_carry_scorer_ts_ms: None,
        last_carry_upcoming_ts_ms: None,
        last_long_cycle_completed_wall_ts_ms: None,
        last_carry_cycle_completed_wall_ts_ms: None,
        long_cycle_cadence_ms: config.live.kline_cadence_ms,
        carry_cycle_cadence_ms: config.live.kline_cadence_ms,
        rest_ticker_last_success_wall_ts_ms: None,
        rest_ticker_last_failure_wall_ts_ms: None,
        rest_ticker_success_count: 0,
        rest_ticker_failure_count: 0,
        bybit_ws_connected: false,
        bybit_ws_epoch: 0,
        bybit_ws_gap_open: true,
        bybit_ws_gap_open_since_wall_ts_ms: Some(wall_ms()?),
        bybit_ws_reconnect_count: 0,
        bybit_ws_fault_count: 0,
        bybit_ws_last_frame_ts_ms: None,
        bybit_ws_ticker_rows: 0,
        bybit_ws_ticker_capacity: ticker_capacity,
        bybit_ws_ticker_coverage_complete: false,
        bybit_ws_ticker_topics_accepted: 0,
        bybit_ws_ticker_topics_quarantined: 0,
        bybit_ws_kline_topics_accepted: 0,
        bybit_ws_kline_topics_quarantined: 0,
        bybit_ws_queued_frames: 0,
        bybit_ws_queue_capacity: 0,
        spool_files: 0,
        spool_bytes: 0,
        spool_file_cap: 0,
        spool_byte_cap: 0,
        spool_byte_soft_threshold: 0,
        replaceable_outputs_coalesced: 0,
        spool_backpressured: false,
        spool_class_files: ["current", "lifecycle", "catchup", "other"]
            .into_iter()
            .map(|class| (class.to_owned(), 0))
            .collect(),
        spool_class_bytes: ["current", "lifecycle", "catchup", "other"]
            .into_iter()
            .map(|class| (class.to_owned(), 0))
            .collect(),
        spool_class_file_caps: ["current", "lifecycle", "catchup", "other"]
            .into_iter()
            .map(|class| (class.to_owned(), spool_class_caps(class).0))
            .collect(),
        spool_class_byte_caps: ["current", "lifecycle", "catchup", "other"]
            .into_iter()
            .map(|class| (class.to_owned(), spool_class_caps(class).1))
            .collect(),
        spool_class_byte_soft_thresholds: ["current", "lifecycle", "catchup", "other"]
            .into_iter()
            .map(|class| (class.to_owned(), 0))
            .collect(),
        spool_backpressured_classes: Vec::new(),
    };
    let bytes = serde_json::to_vec(&heartbeat)
        .map_err(|error| WorkerError::json("encode provisional worker heartbeat", error))?;
    atomic_write(path, &bytes)
}

fn cadence(cadence_ms: u64) -> tokio::time::Interval {
    let mut interval = tokio::time::interval(Duration::from_millis(cadence_ms));
    interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    interval
}

fn pending_kline_limit(symbols: usize) -> usize {
    symbols.saturating_mul(4).clamp(64, 4_096)
}

fn runtime_status(
    health: &StreamHealth,
    repair_running: bool,
    now_ms: i64,
    max_frame_age_ms: i64,
) -> &'static str {
    if stream_inputs_healthy(health, now_ms, max_frame_age_ms)
        && !health.gap_open
        && !repair_running
    {
        "ready"
    } else {
        "degraded"
    }
}

fn stream_inputs_healthy(health: &StreamHealth, now_ms: i64, max_frame_age_ms: i64) -> bool {
    stream_transport_healthy(health, now_ms, max_frame_age_ms) && health.ticker_coverage_complete
}

fn stream_transport_healthy(health: &StreamHealth, now_ms: i64, max_frame_age_ms: i64) -> bool {
    let frame_fresh = health
        .last_frame_ts_ms
        .is_some_and(|last| last <= now_ms && now_ms.saturating_sub(last) <= max_frame_age_ms);
    health.connected
        && health.ticker_capacity > 0
        && health.ticker_topics_quarantined == 0
        && health.kline_topics_quarantined == 0
        && health.ticker_topics_accepted == health.ticker_capacity
        && health.kline_topics_accepted == health.ticker_capacity
        && frame_fresh
}

fn transient_recovery_acceptable(
    health: &StreamHealth,
    repair_running: bool,
    transport_healthy: bool,
    recovery_started_at_ms: &mut Option<i64>,
    now_ms: i64,
) -> bool {
    if !transport_healthy {
        *recovery_started_at_ms = None;
        return false;
    }
    if health.ticker_coverage_complete && !health.gap_open && !repair_running {
        *recovery_started_at_ms = None;
        return true;
    }
    let started_at_ms = *recovery_started_at_ms.get_or_insert(now_ms);
    now_ms.saturating_sub(started_at_ms) < TRANSIENT_RECOVERY_MAX_MS
}

/// One producer verdict: `starting` for bounded cold fill, `recovering` for a
/// short repair on an otherwise sound transport, and `degraded` for a fault.
fn heartbeat_status(
    health: &StreamHealth,
    repair_running: bool,
    cycles: [(Option<i64>, u64); 2],
    started_at_ms: i64,
    now_ms: i64,
    max_frame_age_ms: i64,
    recovery_started_at_ms: &mut Option<i64>,
) -> &'static str {
    let transport_healthy = stream_transport_healthy(health, now_ms, max_frame_age_ms);
    let recovery_acceptable = transient_recovery_acceptable(
        health,
        repair_running,
        transport_healthy,
        recovery_started_at_ms,
        now_ms,
    );
    let live_status = match runtime_status(health, repair_running, now_ms, max_frame_age_ms) {
        "ready" => "ready",
        _ if transport_healthy && recovery_acceptable => "recovering",
        _ => "degraded",
    };
    startup_runtime_status(
        live_status,
        cycles,
        transport_healthy,
        started_at_ms,
        now_ms,
    )
}

fn startup_runtime_status(
    base_status: &'static str,
    cycles: [(Option<i64>, u64); 2],
    startup_inputs_healthy: bool,
    started_at_ms: i64,
    now_ms: i64,
) -> &'static str {
    if cycles
        .iter()
        .any(|(completed_at_ms, _)| completed_at_ms.is_none())
        && startup_inputs_healthy
        && now_ms.saturating_sub(started_at_ms) < STARTUP_MAX_MS
    {
        "starting"
    } else if cycles.iter().any(|(completed_at_ms, cadence_ms)| {
        let window_ms = i64::try_from(*cadence_ms)
            .unwrap_or(i64::MAX / 3)
            .saturating_mul(3);
        !completed_at_ms.is_some_and(|completed_at_ms| {
            completed_at_ms <= now_ms && now_ms.saturating_sub(completed_at_ms) <= window_ms
        })
    }) {
        "degraded"
    } else {
        base_status
    }
}

fn carry_required_lanes_pending(lanes: &LaneState) -> bool {
    lanes.instruments || !lanes.instruments_ready || lanes.funding || !lanes.funding_ready
}

fn spawn_instrument_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    instrument_client: PublicHttpClient,
    ticker_client: PublicHttpClient,
    category: String,
    max_pages: usize,
) {
    tokio::spawn(async move {
        let result =
            fetch_universe_inputs(instrument_client, ticker_client, category, max_pages).await;
        let _ = lane_tx.send(LaneCompletion::Instruments(result)).await;
    });
}

fn spawn_gate_lane(lane_tx: mpsc::Sender<LaneCompletion>, path: PathBuf) {
    tokio::spawn(async move {
        let result = tokio::task::spawn_blocking(move || read_gate_candidates(&path))
            .await
            .map_err(|error| WorkerError::state(format!("LLM gate read task stopped: {error}")))
            .and_then(|result| result);
        let _ = lane_tx.send(LaneCompletion::Gate(result)).await;
    });
}

/// Read the ledger's publication whole. An absent file is the steady state
/// before the ledger's first run and reads as nothing; a malformed one is a
/// source fault for the lane, never a worker error.
fn read_gate_candidates(path: &Path) -> Result<Option<FetchedGate>, WorkerError> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(WorkerError::io("read LLM gate candidates", error)),
    };
    let read_at_ms = wall_ms()?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| WorkerError::input(format!("LLM gate candidates JSON: {error}")))?;
    let object = value
        .as_object()
        .ok_or_else(|| WorkerError::input("LLM gate candidates must be an object"))?;
    let clock = |key: &str| -> Result<i64, WorkerError> {
        object
            .get(key)
            .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
            .filter(|v| *v > 0)
            .ok_or_else(|| WorkerError::input(format!("LLM gate candidates lack {key}")))
    };
    let decision_ts_ms = clock("decision_ts_ms")?;
    let valid_until_ms = clock("valid_until_ms")?;
    let number = |row: &serde_json::Map<String, Value>, key: &str| -> Option<f64> {
        row.get(key)
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite())
    };
    let mut rows = Vec::new();
    for event in object
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::input("LLM gate candidates lack events"))?
    {
        let Some(row) = event.as_object() else {
            return Err(WorkerError::input("LLM gate event is not an object"));
        };
        let symbol = row
            .get("symbol")
            .and_then(Value::as_str)
            .ok_or_else(|| WorkerError::input("LLM gate event lacks a symbol"))?;
        rows.push(crate::model::LlmGateCandidate {
            symbol: symbol.trim().to_ascii_uppercase(),
            score: number(row, "score").unwrap_or(0.0),
            band: row
                .get("band")
                .and_then(Value::as_str)
                .unwrap_or("core")
                .to_owned(),
            trigger_ts_ms: number(row, "trigger_ts_ms").map_or(0, |v| v as i64),
            trigger_price: number(row, "trigger_price").unwrap_or(0.0),
            atr_pct: number(row, "atr_pct").unwrap_or(0.0),
            sigma_daily_30d: number(row, "sigma_daily_30d"),
            turnover_rank: number(row, "turnover_rank"),
            trigger_window_h: number(row, "trigger_window_h").map(|v| v as i64),
        });
    }
    Ok(Some(FetchedGate {
        read_at_ms,
        decision_ts_ms,
        valid_until_ms,
        rows,
    }))
}

async fn fetch_universe_inputs(
    instrument_client: PublicHttpClient,
    ticker_client: PublicHttpClient,
    category: String,
    max_pages: usize,
) -> Result<FetchedUniverseInputs, WorkerError> {
    let instruments =
        fetch_instrument_snapshot(instrument_client, category.clone(), max_pages).await?;
    let tickers = fetch_ticker_page(ticker_client, category).await?;
    Ok(FetchedUniverseInputs {
        instruments,
        tickers,
    })
}

/// The whole ticker page, unfiltered: the universe ranks every listed name.
async fn fetch_ticker_page(
    client: PublicHttpClient,
    category: String,
) -> Result<FetchedTickers, WorkerError> {
    let request_started_at_ms = wall_ms()?;
    let query = format!("category={}", percent_encode(&category));
    let (payload, available_at_ms) = client.get("/v5/market/tickers", &query).await?;
    let rows = result_list(bybit_result(&payload)?)?
        .iter()
        .map(ticker_wire)
        .collect::<Result<Vec<_>, _>>()?;
    let fetched = FetchedTickers {
        request_started_at_ms,
        observed_ts_ms: available_at_ms,
        available_at_ms,
        rows,
    };
    validate_fetched_tickers(&fetched)?;
    Ok(fetched)
}

#[allow(clippy::too_many_arguments)]
fn spawn_funding_fetch_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<FundingJob>,
    instruments: Arc<BTreeMap<String, crate::model::InstrumentObservation>>,
) {
    tokio::spawn(async move {
        let mut succeeded = true;
        for chunk in funding_job_chunks(&jobs) {
            let result = fetch_funding_batches(
                client.clone(),
                category.clone(),
                page_limit,
                max_parallel,
                chunk.to_vec(),
                Arc::clone(&instruments),
            )
            .await;
            let fetched_without_failures = result
                .as_ref()
                .is_ok_and(|fetched| fetched.failures.is_empty());
            let (resume_tx, resume_rx) = oneshot::channel();
            if lane_tx
                .send(LaneCompletion::FundingChunk {
                    result,
                    resume: resume_tx,
                })
                .await
                .is_err()
            {
                return;
            }
            match resume_rx.await {
                Ok(true) => {
                    if !fetched_without_failures {
                        succeeded = false;
                    }
                }
                _ => {
                    succeeded = false;
                    break;
                }
            }
        }
        let _ = lane_tx
            .send(LaneCompletion::FundingFinished { succeeded })
            .await;
    });
}

fn spawn_whale_fetch_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    client: PublicHttpClient,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<WhaleJob>,
) {
    tokio::spawn(async move {
        for chunk in whale_job_chunks(&jobs) {
            let result =
                fetch_whale_batch(client.clone(), page_limit, max_parallel, chunk.to_vec()).await;
            if !send_whale_chunk_and_wait(&lane_tx, result).await {
                break;
            }
        }
        let _ = lane_tx.send(LaneCompletion::WhaleFinished).await;
    });
}

async fn send_whale_chunk_and_wait(
    lane_tx: &mpsc::Sender<LaneCompletion>,
    result: Result<FetchedWhales, WorkerError>,
) -> bool {
    let (resume_tx, resume_rx) = oneshot::channel();
    if lane_tx
        .send(LaneCompletion::WhaleChunk {
            result,
            resume: resume_tx,
        })
        .await
        .is_err()
    {
        return false;
    }
    resume_rx.await == Ok(true)
}

#[allow(clippy::too_many_arguments)]
fn spawn_repair_lane(
    lane_tx: mpsc::Sender<LaneCompletion>,
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<(String, i64, i64)>,
    end_ms: i64,
    epoch: Option<u64>,
) {
    tokio::spawn(async move {
        for chunk in kline_job_chunks(&jobs) {
            let result = fetch_kline_jobs_bounded(
                client.clone(),
                category.clone(),
                page_limit,
                max_parallel,
                chunk.to_vec(),
            )
            .await;
            if !send_repair_chunk_and_wait(&lane_tx, result).await {
                break;
            }
        }
        let _ = lane_tx
            .send(LaneCompletion::RepairFinished { end_ms, epoch })
            .await;
    });
}

async fn send_repair_chunk_and_wait(
    lane_tx: &mpsc::Sender<LaneCompletion>,
    result: Result<FetchedKlineJobs, WorkerError>,
) -> bool {
    let (resume_tx, resume_rx) = oneshot::channel();
    if lane_tx
        .send(LaneCompletion::RepairChunk {
            result,
            resume: resume_tx,
        })
        .await
        .is_err()
    {
        return false;
    }
    resume_rx.await == Ok(true)
}

async fn fetch_instrument_snapshot(
    client: PublicHttpClient,
    category: String,
    max_pages: usize,
) -> Result<FetchedInstruments, WorkerError> {
    let observed_ts_ms = wall_ms()?;
    let mut by_symbol = BTreeMap::new();
    let mut available_at_ms = observed_ts_ms;
    for status in ["Closed", "Delivering", "Trading"] {
        let mut cursor: Option<String> = None;
        for _ in 0..max_pages {
            let mut query = format!(
                "category={}&status={status}&limit=1000",
                percent_encode(&category)
            );
            if let Some(value) = &cursor {
                query.push_str("&cursor=");
                query.push_str(&percent_encode(value));
            }
            let (payload, received) = client.get("/v5/market/instruments-info", &query).await?;
            available_at_ms = available_at_ms.max(received);
            let result = bybit_result(&payload)?;
            for value in result_list(result)? {
                let row = instrument_wire(value)?;
                by_symbol.insert(row.symbol.to_ascii_uppercase(), row);
            }
            let next = result
                .get("nextPageCursor")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned);
            if next.is_none() {
                cursor = None;
                break;
            }
            if next == cursor {
                return Err(WorkerError::network(format!(
                    "Bybit {status} instruments cursor did not advance"
                )));
            }
            cursor = next;
        }
        if cursor.is_some() {
            return Err(WorkerError::network(format!(
                "Bybit {status} instruments pagination exceeded configured page bound"
            )));
        }
    }
    let fetched = FetchedInstruments {
        observed_ts_ms,
        available_at_ms,
        rows: by_symbol.into_values().collect(),
    };
    validate_fetched_instruments(&fetched)?;
    Ok(fetched)
}

async fn fetch_ticker_snapshot(
    client: PublicHttpClient,
    category: String,
    allowed: BTreeSet<String>,
) -> Result<FetchedTickers, WorkerError> {
    let request_started_at_ms = wall_ms()?;
    let query = format!("category={}", percent_encode(&category));
    let (payload, available_at_ms) = client.get("/v5/market/tickers", &query).await?;
    let rows = result_list(bybit_result(&payload)?)?
        .iter()
        .filter(|value| {
            value
                .get("symbol")
                .and_then(Value::as_str)
                .is_some_and(|symbol| allowed.contains(&symbol.to_ascii_uppercase()))
        })
        .map(ticker_wire)
        .collect::<Result<Vec<_>, _>>()?;
    let fetched = FetchedTickers {
        request_started_at_ms,
        observed_ts_ms: available_at_ms,
        available_at_ms,
        rows,
    };
    validate_fetched_tickers(&fetched)?;
    Ok(fetched)
}

async fn fetch_kline_jobs_bounded(
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<(String, i64, i64)>,
) -> Result<FetchedKlineJobs, WorkerError> {
    if jobs.len() > KLINE_FETCH_CHUNK_SIZE {
        return Err(WorkerError::state(format!(
            "kline fetch retained {} jobs; maximum chunk is {KLINE_FETCH_CHUNK_SIZE}",
            jobs.len()
        )));
    }
    let limiter = Arc::new(Semaphore::new(max_parallel));
    let mut tasks = JoinSet::new();
    for (symbol, start, end) in jobs {
        let limiter = Arc::clone(&limiter);
        let client = client.clone();
        let category = category.clone();
        tasks.spawn(async move {
            let result = match limiter.acquire_owned().await {
                Ok(_permit) => {
                    fetch_klines(client, &category, page_limit, &symbol, start, end).await
                }
                Err(_) => Err(WorkerError::state(
                    "public request concurrency limiter closed",
                )),
            };
            (symbol, start, end, result)
        });
    }
    let mut fetched = Vec::new();
    let mut failures = Vec::new();
    while let Some(joined) = tasks.join_next().await {
        let (symbol, start, end, result) = joined
            .map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?;
        match result {
            Ok((rows, available_at_ms)) => {
                fetched.push((
                    symbol,
                    FetchedKlineBatch {
                        rows,
                        available_at_ms,
                        checked_from_ms: Some(start),
                        checked_through_ms: Some(end),
                    },
                ));
            }
            Err(error) if error.is_lane_local_source_failure() => {
                failures.push((symbol, error.to_string()));
            }
            Err(error) => return Err(error),
        }
    }
    fetched.sort_by(|left, right| {
        (&left.0, left.1.checked_from_ms).cmp(&(&right.0, right.1.checked_from_ms))
    });
    failures.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(FetchedKlineJobs {
        batches: fetched,
        failures,
    })
}

#[allow(clippy::too_many_arguments)]
async fn fetch_funding_batches(
    client: PublicHttpClient,
    category: String,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<FundingJob>,
    instruments: Arc<BTreeMap<String, crate::model::InstrumentObservation>>,
) -> Result<FetchedFunding, WorkerError> {
    if jobs.len() > FUNDING_FETCH_CHUNK_SIZE {
        return Err(WorkerError::state(format!(
            "funding fetch retained {} jobs; maximum chunk is {FUNDING_FETCH_CHUNK_SIZE}",
            jobs.len()
        )));
    }
    let limiter = Arc::new(Semaphore::new(max_parallel));
    let mut tasks = JoinSet::new();
    for (symbol, start_ms, end_ms, emit_lifecycle) in jobs {
        let interval_hours = instruments
            .get(&symbol)
            .and_then(|row| row.funding_interval_min)
            .filter(|minutes| *minutes > 0 && *minutes % 60 == 0)
            .map(|minutes| minutes / 60);
        let limiter = Arc::clone(&limiter);
        let client = client.clone();
        let category = category.clone();
        tasks.spawn(async move {
            let result = match limiter.acquire_owned().await {
                Ok(_permit) => {
                    fetch_funding(
                        client,
                        &category,
                        page_limit,
                        &symbol,
                        start_ms,
                        end_ms,
                        interval_hours,
                    )
                    .await
                }
                Err(_) => Err(WorkerError::state(
                    "public request concurrency limiter closed",
                )),
            };
            (
                symbol,
                start_ms,
                end_ms,
                emit_lifecycle,
                interval_hours,
                result,
            )
        });
    }
    let mut batches = Vec::new();
    let mut failures = Vec::new();
    while let Some(joined) = tasks.join_next().await {
        let (symbol, checked_from_ms, checked_through_ms, emit_lifecycle, interval_hours, result) =
            joined
                .map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?;
        match result {
            Ok((rows, available_at_ms)) => {
                let intervals = interval_hours
                    .map(|hours| hours.saturating_mul(HOUR_MS))
                    .filter(|interval_ms| *interval_ms > 0)
                    .map(|interval_ms| {
                        complete_funding_coverage(
                            checked_from_ms,
                            checked_through_ms,
                            interval_ms,
                            &rows,
                        )
                    })
                    .transpose()?
                    .unwrap_or_default();
                if intervals.is_empty() {
                    batches.push((
                        symbol,
                        FetchedFundingBatch {
                            rows,
                            available_at_ms,
                            checked_from_ms: None,
                            checked_through_ms: None,
                            emit_lifecycle,
                        },
                    ));
                } else {
                    let mut rows = Some(rows);
                    for (index, (from, through)) in intervals.into_iter().enumerate() {
                        batches.push((
                            symbol.clone(),
                            FetchedFundingBatch {
                                rows: rows.take().unwrap_or_default(),
                                available_at_ms,
                                checked_from_ms: Some(from),
                                checked_through_ms: Some(through),
                                emit_lifecycle: emit_lifecycle && index == 0,
                            },
                        ));
                    }
                }
            }
            Err(error) if error.is_lane_local_source_failure() => {
                failures.push((symbol, error.to_string()));
            }
            Err(error) => return Err(error),
        }
    }
    batches.sort_by(|left, right| {
        (&left.0, left.1.checked_from_ms).cmp(&(&right.0, right.1.checked_from_ms))
    });
    failures.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(FetchedFunding { batches, failures })
}

fn complete_funding_coverage(
    start_ms: i64,
    end_ms: i64,
    interval_ms: i64,
    rows: &[BybitFundingWire],
) -> Result<Vec<(i64, i64)>, WorkerError> {
    if start_ms >= end_ms || interval_ms <= 0 || interval_ms % HOUR_MS != 0 {
        return Ok(Vec::new());
    }
    let timestamps = rows
        .iter()
        .map(|row| {
            wire_i64(
                Some(&row.funding_rate_timestamp),
                "Bybit complete funding timestamp",
            )
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    let mut groups = Vec::<(i64, i64)>::new();
    for timestamp in timestamps
        .into_iter()
        .filter(|timestamp| start_ms <= *timestamp && *timestamp <= end_ms)
    {
        if let Some((_, last)) = groups.last_mut() {
            if timestamp == last.saturating_add(interval_ms) {
                *last = timestamp;
                continue;
            }
        }
        groups.push((timestamp, timestamp));
    }
    Ok(groups
        .into_iter()
        .filter_map(|(first, last)| {
            let from = if first.saturating_sub(start_ms) <= interval_ms {
                start_ms
            } else {
                first
            };
            let through = if end_ms.saturating_sub(last) < interval_ms {
                end_ms
            } else {
                last
            };
            (from < through).then_some((from, through))
        })
        .collect())
}

async fn fetch_whale_batch(
    client: PublicHttpClient,
    page_limit: usize,
    max_parallel: usize,
    jobs: Vec<(String, i64, i64)>,
) -> Result<FetchedWhales, WorkerError> {
    if jobs.len() > WHALE_FETCH_CHUNK_SIZE {
        return Err(WorkerError::state(format!(
            "whale fetch retained {} jobs; maximum chunk is {WHALE_FETCH_CHUNK_SIZE}",
            jobs.len()
        )));
    }
    let limiter = Arc::new(Semaphore::new(max_parallel));
    let mut tasks = JoinSet::new();
    let mut available_at_ms = 0;
    for (symbol, start_ms, end_ms) in jobs {
        available_at_ms = available_at_ms.max(end_ms);
        let limiter = Arc::clone(&limiter);
        let client = client.clone();
        tasks.spawn(async move {
            let result = match limiter.acquire_owned().await {
                Ok(_permit) => {
                    fetch_whale_symbol(client, page_limit, &symbol, start_ms, end_ms).await
                }
                Err(_) => Err(WorkerError::state(
                    "public request concurrency limiter closed",
                )),
            };
            (symbol, start_ms, end_ms, result)
        });
    }
    let mut rows = Vec::new();
    let mut coverage = Vec::new();
    while let Some(joined) = tasks.join_next().await {
        let (symbol, start_ms, end_ms, result) = joined
            .map_err(|error| WorkerError::state(format!("public fetch task failed: {error}")))?;
        match result {
            Ok((mut found, received)) => {
                coverage.extend(complete_whale_coverage(&symbol, start_ms, end_ms, &found)?);
                rows.append(&mut found);
                available_at_ms = available_at_ms.max(received);
            }
            Err(error) => return Err(error),
        }
    }
    rows.sort_by(|left, right| {
        let left_ts =
            wire_i64(Some(&left.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
        let right_ts =
            wire_i64(Some(&right.day_end_ms), "Binance whale timestamp").unwrap_or(i64::MAX);
        (&left.symbol, left_ts).cmp(&(&right.symbol, right_ts))
    });
    Ok(FetchedWhales {
        available_at_ms,
        rows,
        coverage,
    })
}

fn complete_whale_coverage(
    symbol: &str,
    start_ms: i64,
    end_ms: i64,
    rows: &[BinanceWhaleWire],
) -> Result<Vec<SourceCoverage>, WorkerError> {
    rows.iter()
        .filter_map(|row| {
            let day_end_ms = match wire_i64(
                Some(&row.day_end_ms),
                "Binance complete whale day timestamp",
            ) {
                Ok(value) => value,
                Err(error) => return Some(Err(error)),
            };
            (day_end_ms > start_ms && day_end_ms <= end_ms).then_some(Ok(SourceCoverage {
                symbol: symbol.to_owned(),
                checked_from_ms: day_end_ms.saturating_sub(DAY_MS),
                checked_through_ms: day_end_ms,
                replace_coverage: false,
            }))
        })
        .collect()
}

fn current_trading_instrument(
    state: &crate::worker::WorkerState,
    symbol: &str,
    settle_coin: &str,
) -> bool {
    state.instruments.get(symbol).is_some_and(|row| {
        row.status.as_deref() == Some("Trading")
            && row.settle_coin.as_deref() == Some(settle_coin)
            && !row.is_prelisting
    })
}

fn instrument_trading_at(
    state: &crate::worker::WorkerState,
    symbol: &str,
    timestamp_ms: i64,
) -> bool {
    trading_intervals_contain(
        state
            .instrument_trading_intervals
            .get(symbol)
            .map(Vec::as_slice),
        state
            .instrument_status_unknown_since_ms
            .get(symbol)
            .copied(),
        timestamp_ms,
    )
}

fn instrument_source_ranges(
    state: &crate::worker::WorkerState,
    symbol: &str,
    required_from_ms: i64,
    required_through_ms: i64,
    alignment_ms: i64,
) -> Vec<(i64, i64)> {
    bounded_instrument_source_ranges(
        state
            .instrument_trading_intervals
            .get(symbol)
            .map(Vec::as_slice),
        state
            .instrument_status_unknown_since_ms
            .get(symbol)
            .copied(),
        required_from_ms,
        required_through_ms,
        alignment_ms,
    )
}

fn trading_intervals_contain(
    intervals: Option<&[InstrumentTradingInterval]>,
    unknown_since_ms: Option<i64>,
    timestamp_ms: i64,
) -> bool {
    if unknown_since_ms.is_some_and(|unknown_since| timestamp_ms >= unknown_since) {
        return false;
    }
    intervals.is_some_and(|intervals| {
        intervals.iter().any(|interval| {
            interval.trading_from_ms <= timestamp_ms
                && interval
                    .trading_through_ms
                    .is_none_or(|through| timestamp_ms < through)
        })
    })
}

fn bounded_instrument_source_ranges(
    intervals: Option<&[InstrumentTradingInterval]>,
    unknown_through_ms: Option<i64>,
    required_from_ms: i64,
    required_through_ms: i64,
    alignment_ms: i64,
) -> Vec<(i64, i64)> {
    if required_from_ms >= required_through_ms || alignment_ms <= 0 {
        return Vec::new();
    }
    let mut ranges = intervals
        .into_iter()
        .flatten()
        .filter_map(|interval| {
            let from = required_from_ms.max(align_up(interval.trading_from_ms, alignment_ms));
            let eligible_through_ms = interval
                .trading_through_ms
                .into_iter()
                .chain(unknown_through_ms)
                .min()
                .unwrap_or(required_through_ms);
            let through = align_down(required_through_ms.min(eligible_through_ms), alignment_ms);
            (from < through).then_some((from, through))
        })
        .collect::<Vec<_>>();
    ranges.sort_unstable();
    let mut merged = Vec::<(i64, i64)>::new();
    for range in ranges {
        if let Some(last) = merged.last_mut() {
            if range.0 <= last.1 {
                last.1 = last.1.max(range.1);
                continue;
            }
        }
        merged.push(range);
    }
    merged
}

fn align_up(value: i64, alignment: i64) -> i64 {
    let remainder = value.rem_euclid(alignment);
    if remainder == 0 {
        value
    } else {
        value.saturating_add(alignment.saturating_sub(remainder))
    }
}

fn align_down(value: i64, alignment: i64) -> i64 {
    value.saturating_sub(value.rem_euclid(alignment))
}

fn coverage_contains(
    checked_from: &BTreeMap<String, i64>,
    checked_through: &BTreeMap<String, i64>,
    symbol: &str,
    required_from_ms: i64,
    required_through_ms: i64,
) -> bool {
    matches!(
        (
            checked_from.get(symbol).copied(),
            checked_through.get(symbol).copied(),
        ),
        (Some(from), Some(through))
            if from <= required_from_ms && through >= required_through_ms
    )
}

fn source_coverage_contains(
    checked_from: &BTreeMap<String, i64>,
    checked_through: &BTreeMap<String, i64>,
    intervals_by_symbol: &BTreeMap<String, Vec<crate::model::CoverageInterval>>,
    symbol: &str,
    required_from_ms: i64,
    required_through_ms: i64,
) -> bool {
    intervals_by_symbol.get(symbol).is_some_and(|intervals| {
        intervals.iter().any(|interval| {
            interval.checked_from_ms <= required_from_ms
                && interval.checked_through_ms >= required_through_ms
        })
    }) || coverage_contains(
        checked_from,
        checked_through,
        symbol,
        required_from_ms,
        required_through_ms,
    )
}

fn source_coverage_repair_start(
    checked_from: &BTreeMap<String, i64>,
    checked_through: &BTreeMap<String, i64>,
    intervals_by_symbol: &BTreeMap<String, Vec<crate::model::CoverageInterval>>,
    symbol: &str,
    required_start_ms: i64,
    required_through_ms: i64,
) -> i64 {
    if let Some(interval) = intervals_by_symbol.get(symbol).and_then(|intervals| {
        intervals.iter().find(|interval| {
            interval.checked_from_ms <= required_start_ms
                && interval.checked_through_ms > required_start_ms
        })
    }) {
        return interval.checked_through_ms.min(required_through_ms);
    }
    coverage_repair_start(
        required_start_ms,
        required_through_ms,
        checked_from.get(symbol).copied(),
        checked_through.get(symbol).copied(),
    )
}

fn kline_coverage_contains(
    state: &crate::worker::WorkerState,
    symbol: &str,
    required_from_ms: i64,
    required_through_ms: i64,
) -> bool {
    state
        .kline_coverage_intervals
        .get(symbol)
        .is_some_and(|intervals| {
            intervals.iter().any(|interval| {
                interval.checked_from_ms <= required_from_ms
                    && interval.checked_through_ms >= required_through_ms
            })
        })
        || coverage_contains(
            &state.kline_checked_from_ms,
            &state.kline_checked_through_ms,
            symbol,
            required_from_ms,
            required_through_ms,
        )
}

fn kline_coverage_repair_start(
    state: &crate::worker::WorkerState,
    symbol: &str,
    required_start_ms: i64,
    required_through_ms: i64,
) -> i64 {
    if let Some(interval) = state
        .kline_coverage_intervals
        .get(symbol)
        .and_then(|intervals| {
            intervals.iter().find(|interval| {
                interval.checked_from_ms <= required_start_ms
                    && interval.checked_through_ms > required_start_ms
            })
        })
    {
        return interval.checked_through_ms.min(required_through_ms);
    }
    coverage_repair_start(
        required_start_ms,
        required_through_ms,
        state.kline_checked_from_ms.get(symbol).copied(),
        state.kline_checked_through_ms.get(symbol).copied(),
    )
}

fn first_missing_kline_hour(
    state: &crate::worker::WorkerState,
    symbol: &str,
    start_ms: i64,
    end_ms: i64,
) -> Option<i64> {
    let Some(rows) = state.klines.get(symbol) else {
        return (start_ms < end_ms).then_some(start_ms);
    };
    let mut timestamp = start_ms;
    while timestamp < end_ms {
        if !rows.contains_key(&timestamp) {
            return Some(timestamp);
        }
        timestamp = timestamp.saturating_add(HOUR_MS);
    }
    None
}

fn closed_kline_end(now_ms: i64) -> i64 {
    let publishable_ms = now_ms.saturating_sub(KLINE_PUBLICATION_LAG_MS);
    publishable_ms - publishable_ms.rem_euclid(HOUR_MS)
}

fn coverage_repair_start(
    required_start: i64,
    end_ms: i64,
    checked_from: Option<i64>,
    checked_through: Option<i64>,
) -> i64 {
    match (checked_from, checked_through) {
        (Some(from), Some(through)) if from <= required_start => {
            through.max(required_start).min(end_ms)
        }
        _ => required_start,
    }
}

fn source_grid_slots(
    start_ms: i64,
    end_ms: i64,
    step_ms: i64,
    end_inclusive: bool,
) -> Result<usize, WorkerError> {
    if start_ms < 0 || end_ms < start_ms || step_ms <= 0 {
        return Err(WorkerError::state("source fetch range is invalid"));
    }
    let remainder = start_ms.rem_euclid(step_ms);
    let first = if remainder == 0 {
        start_ms
    } else {
        start_ms
            .checked_add(step_ms - remainder)
            .ok_or_else(|| WorkerError::state("source fetch range overflowed"))?
    };
    let Some(last_bound) = end_inclusive
        .then_some(end_ms)
        .or_else(|| end_ms.checked_sub(1))
    else {
        return Ok(0);
    };
    if first > last_bound {
        return Ok(0);
    }
    usize::try_from((last_bound - first) / step_ms + 1)
        .map_err(|_| WorkerError::state("source fetch row bound exceeds usize"))
}

fn validate_source_grid_timestamp(
    timestamp_ms: i64,
    start_ms: i64,
    end_ms: i64,
    step_ms: i64,
    end_inclusive: bool,
    label: &str,
) -> Result<(), WorkerError> {
    let in_range = timestamp_ms >= start_ms
        && if end_inclusive {
            timestamp_ms <= end_ms
        } else {
            timestamp_ms < end_ms
        };
    if !in_range || timestamp_ms.rem_euclid(step_ms) != 0 {
        return Err(WorkerError::network(format!(
            "{label} is outside the requested source grid"
        )));
    }
    Ok(())
}

fn validate_source_page_rows(actual: usize, limit: usize, label: &str) -> Result<(), WorkerError> {
    if actual > limit {
        return Err(WorkerError::network(format!(
            "{label} response exceeded the requested page limit"
        )));
    }
    Ok(())
}

fn whale_fetch_bounds(start_ms: i64, end_ms: i64) -> Result<(i64, i64, usize), WorkerError> {
    let query_start_ms = start_ms - start_ms.rem_euclid(FIVE_MIN_MS);
    let query_end_ms = end_ms - end_ms.rem_euclid(FIVE_MIN_MS);
    let retained_row_cap = source_grid_slots(start_ms, end_ms, FIVE_MIN_MS, true)?;
    Ok((query_start_ms, query_end_ms, retained_row_cap))
}

async fn fetch_klines(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<(Vec<Vec<Value>>, i64), WorkerError> {
    let page_row_cap = page_limit;
    let retained_row_cap = source_grid_slots(start, end, HOUR_MS, false)?;
    let limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("kline page limit exceeds i64"))?;
    let span = (limit - 1).max(0) * HOUR_MS;
    let mut cursor = start;
    let mut by_time = BTreeMap::<i64, Vec<Value>>::new();
    let mut available = start;
    while cursor < end {
        let window_end = (cursor + span).min(end - HOUR_MS);
        let query = format!(
            "category={}&symbol={}&interval=60&start={cursor}&end={window_end}&limit={limit}",
            percent_encode(category),
            percent_encode(symbol),
        );
        let mut list = Vec::new();
        for _ in 0..2 {
            let (payload, received) = client.get("/v5/market/kline", &query).await?;
            available = available.max(received);
            list = result_list(bybit_result(&payload)?)?.to_vec();
            validate_source_page_rows(list.len(), page_row_cap, "Bybit kline")?;
            if !list.is_empty() {
                break;
            }
        }
        for value in list {
            let row = value
                .as_array()
                .ok_or_else(|| WorkerError::network("Bybit kline row is not an array"))?
                .clone();
            let ts = wire_i64(row.first(), "Bybit kline timestamp")?;
            validate_source_grid_timestamp(
                ts,
                start,
                end,
                HOUR_MS,
                false,
                "Bybit kline timestamp",
            )?;
            match by_time.get(&ts) {
                Some(existing) if existing != &row => {
                    return Err(WorkerError::network(
                        "Bybit kline pagination returned conflicting duplicate",
                    ));
                }
                Some(_) => {}
                None => {
                    if by_time.len() >= retained_row_cap {
                        return Err(WorkerError::network(
                            "Bybit kline response exceeded the requested grid cardinality",
                        ));
                    }
                    by_time.insert(ts, row);
                }
            }
        }
        cursor = window_end.saturating_add(HOUR_MS);
    }
    let rows = by_time.into_values().collect::<Vec<_>>();
    normalize_kline_rows(symbol, available, &rows)?;
    Ok((rows, available))
}

#[allow(clippy::too_many_arguments)]
async fn fetch_funding(
    client: PublicHttpClient,
    category: &str,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
    interval_hours: Option<i64>,
) -> Result<(Vec<BybitFundingWire>, i64), WorkerError> {
    let page_row_cap = page_limit;
    let retained_row_cap = source_grid_slots(start, end, HOUR_MS, true)?;
    let interval = interval_hours.map(Value::from);
    let mut cursor = start;
    let mut available = start;
    let mut by_time = BTreeMap::new();
    let page_limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("funding page limit exceeds i64"))?;
    let window_span = (page_limit - 1).max(0) * HOUR_MS;
    while cursor <= end {
        let window_end = cursor.saturating_add(window_span).min(end);
        let query = format!(
            "category={}&symbol={}&startTime={cursor}&endTime={window_end}&limit={page_limit}",
            percent_encode(category),
            percent_encode(symbol),
        );
        let (payload, received) = client.get("/v5/market/funding/history", &query).await?;
        available = available.max(received);
        let list = result_list(bybit_result(&payload)?)?;
        validate_source_page_rows(list.len(), page_row_cap, "Bybit funding")?;
        for value in list {
            let timestamp = value
                .get("fundingRateTimestamp")
                .cloned()
                .ok_or_else(|| WorkerError::network("Bybit funding row lacks timestamp"))?;
            let rate = value
                .get("fundingRate")
                .cloned()
                .ok_or_else(|| WorkerError::network("Bybit funding row lacks rate"))?;
            let key = wire_i64(Some(&timestamp), "Bybit funding timestamp")?;
            let row = BybitFundingWire {
                funding_rate_timestamp: timestamp,
                funding_rate: rate,
                funding_interval_hour: interval.clone(),
            };
            validate_source_grid_timestamp(
                key,
                start,
                end,
                HOUR_MS,
                true,
                "Bybit funding timestamp",
            )?;
            if !by_time.contains_key(&key) && by_time.len() >= retained_row_cap {
                return Err(WorkerError::network(
                    "Bybit funding response exceeded the requested grid cardinality",
                ));
            }
            if let Some(existing) = by_time.insert(key, row.clone()) {
                if existing != row {
                    return Err(WorkerError::network(
                        "Bybit funding pagination returned conflicting duplicate",
                    ));
                }
            }
        }
        if window_end == end {
            break;
        }
        cursor = window_end.saturating_add(1);
    }
    let rows = by_time.into_values().collect::<Vec<_>>();
    normalize_funding_rows(symbol, available, &rows)?;
    Ok((rows, available))
}

async fn fetch_whale_symbol(
    client: PublicHttpClient,
    page_limit: usize,
    symbol: &str,
    start: i64,
    end: i64,
) -> Result<(Vec<BinanceWhaleWire>, i64), WorkerError> {
    let page_row_cap = page_limit;
    let (query_start, query_end, retained_row_cap) = whale_fetch_bounds(start, end)?;
    let page_limit = i64::try_from(page_limit)
        .map_err(|_| WorkerError::config("whale page limit exceeds i64"))?;
    let mut cursor = query_start;
    let mut by_time = BTreeMap::<i64, Option<Value>>::new();
    let mut available = start;
    while cursor <= query_end {
        let window_end = cursor
            .saturating_add((page_limit - 1).max(0).saturating_mul(FIVE_MIN_MS))
            .min(query_end);
        let query = format!(
            "symbol={}&period=5m&startTime={cursor}&endTime={window_end}&limit={page_limit}",
            percent_encode(symbol),
        );
        let (payload, received) = client
            .get("/futures/data/topLongShortPositionRatio", &query)
            .await?;
        available = available.max(received);
        let list = payload
            .as_array()
            .ok_or_else(|| WorkerError::network("Binance whale response is not a list"))?;
        validate_source_page_rows(list.len(), page_row_cap, "Binance whale")?;
        for value in list {
            let timestamp = wire_i64(value.get("timestamp"), "Binance whale timestamp")?;
            let ratio = value.get("longShortRatio").cloned();
            validate_source_grid_timestamp(
                timestamp,
                query_start,
                query_end,
                FIVE_MIN_MS,
                true,
                "Binance whale timestamp",
            )?;
            if timestamp < start || timestamp > end {
                continue;
            }
            if !by_time.contains_key(&timestamp) && by_time.len() >= retained_row_cap {
                return Err(WorkerError::network(
                    "Binance whale response exceeded the requested grid cardinality",
                ));
            }
            if let Some(existing) = by_time.insert(timestamp, ratio.clone()) {
                if existing != ratio {
                    return Err(WorkerError::network(
                        "Binance whale pagination returned conflicting duplicate",
                    ));
                }
            }
        }
        if window_end == query_end {
            break;
        }
        cursor = window_end.saturating_add(FIVE_MIN_MS);
    }
    let first_day_end = (start - start.rem_euclid(DAY_MS)).saturating_add(DAY_MS);
    let complete_end = end - end.rem_euclid(DAY_MS);
    let mut rows = Vec::new();
    let mut day_end = first_day_end;
    while day_end <= complete_end {
        let day_start = day_end - DAY_MS;
        let complete = (0..(DAY_MS / FIVE_MIN_MS))
            .all(|offset| by_time.contains_key(&(day_start + offset * FIVE_MIN_MS)));
        if complete {
            rows.push(BinanceWhaleWire {
                symbol: symbol.to_owned(),
                day_end_ms: Value::from(day_end),
                long_short_ratio: by_time.get(&(day_end - FIVE_MIN_MS)).cloned().flatten(),
            });
        }
        day_end = day_end.saturating_add(DAY_MS);
    }
    normalize_whales(available, &rows)?;
    Ok((rows, available))
}

fn bybit_result(payload: &Value) -> Result<&Value, WorkerError> {
    if payload.get("retCode").and_then(Value::as_i64) != Some(0) {
        return Err(WorkerError::network(format!(
            "Bybit retCode={} retMsg={}",
            payload.get("retCode").unwrap_or(&Value::Null),
            payload.get("retMsg").unwrap_or(&Value::Null)
        )));
    }
    payload
        .get("result")
        .ok_or_else(|| WorkerError::network("Bybit response lacks result"))
}

fn result_list(result: &Value) -> Result<&Vec<Value>, WorkerError> {
    result
        .get("list")
        .and_then(Value::as_array)
        .ok_or_else(|| WorkerError::network("Bybit result lacks list"))
}

fn instrument_wire(value: &Value) -> Result<BybitInstrumentWire, WorkerError> {
    let symbol = value
        .get("symbol")
        .and_then(Value::as_str)
        .ok_or_else(|| WorkerError::network("Bybit instrument lacks symbol"))?
        .to_owned();
    Ok(BybitInstrumentWire {
        symbol,
        contract_type: text(value, "contractType"),
        symbol_type: text(value, "symbolType"),
        status: text(value, "status"),
        base_coin: text(value, "baseCoin"),
        quote_coin: text(value, "quoteCoin"),
        settle_coin: text(value, "settleCoin"),
        launch_time: value.get("launchTime").cloned(),
        delivery_time: value.get("deliveryTime").cloned(),
        price_filter: object_map(value, "priceFilter")?,
        lot_size_filter: object_map(value, "lotSizeFilter")?,
        funding_interval: value.get("fundingInterval").cloned(),
        is_pre_listing: value
            .get("isPreListing")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

fn object_map(value: &Value, key: &str) -> Result<BTreeMap<String, Value>, WorkerError> {
    value
        .get(key)
        .and_then(Value::as_object)
        .map(|object| {
            object
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect()
        })
        .ok_or_else(|| WorkerError::network(format!("Bybit instrument lacks {key}")))
}

fn text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_owned)
}

fn wire_i64(value: Option<&Value>, label: &str) -> Result<i64, WorkerError> {
    match value {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.parse().ok(),
        _ => None,
    }
    .ok_or_else(|| WorkerError::network(format!("{label} is not an integer")))
}

async fn shutdown_signal() -> Result<(), WorkerError> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .map_err(|error| WorkerError::io("install SIGTERM handler", error))?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result.map_err(|error| WorkerError::io("wait for SIGINT", error)),
            _ = terminate.recv() => Ok(()),
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c()
        .await
        .map_err(|error| WorkerError::io("wait for shutdown", error))
}

pub fn heartbeat_path_parent(path: &Path) -> Result<&Path, WorkerError> {
    path.parent()
        .ok_or_else(|| WorkerError::config("heartbeat path has no parent"))
}

#[cfg(test)]
mod tests {
    use super::{
        bounded_instrument_source_ranges, carry_required_lanes_pending, closed_kline_end,
        complete_funding_coverage, complete_whale_coverage, coverage_repair_start,
        funding_job_chunks, heartbeat_status, kline_job_chunks, runtime_status,
        send_repair_chunk_and_wait, send_whale_chunk_and_wait, source_coverage_contains,
        source_grid_slots, startup_runtime_status, stream_transport_healthy,
        trading_intervals_contain, transient_recovery_acceptable,
        validate_instrument_source_against_state, validate_source_grid_timestamp,
        validate_source_page_rows, whale_fetch_bounds, whale_job_chunks, FetchedFunding,
        FetchedFundingBatch, FetchedInstruments, FetchedKlineBatch, FetchedKlineJobs,
        FetchedTickers, FetchedUniverseInputs, FetchedWhales, LaneCompletion, LaneState,
        LiveRunOptions, LiveRunner, StreamEvent, StreamHealth, TickerSample,
        FUNDING_FETCH_CHUNK_SIZE, KLINE_FETCH_CHUNK_SIZE, LANE_COMPLETION_QUEUE_CAPACITY,
        STARTUP_MAX_MS, TRANSIENT_RECOVERY_MAX_MS, WHALE_FETCH_CHUNK_SIZE,
    };
    use crate::bybit_ws::BybitPublicStream;
    use crate::config::SignalWorkerConfig;
    use crate::model::{
        BinanceWhaleWire, BootstrapCoverage, BybitFundingWire, BybitInstrumentWire,
        BybitTickerWire, CoverageInterval, HourlyKline, InstrumentTradingInterval,
        ObservationPayload, SettledFunding, SignalPayloadEnvelope, UniverseIdentity, UniverseMode,
        WireEvent,
    };
    use crate::store::AtomicJsonStore;
    use crate::worker::{SignalWorker, WorkerError};
    use crate::SCHEMA_VERSION;
    use crate::{DAY_MS, HOUR_MS};
    use serde_json::Value;
    use std::collections::BTreeMap;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    #[test]
    fn production_frontier_plans_only_unchecked_or_rebased_ranges() {
        let start = 100 * HOUR_MS;
        let end = start + 5 * HOUR_MS;
        assert_eq!(coverage_repair_start(start, end, None, None), start);
        assert_eq!(
            coverage_repair_start(start, end, Some(start), Some(end)),
            end
        );
        assert_eq!(
            coverage_repair_start(start, end, Some(start), Some(end - HOUR_MS)),
            end - HOUR_MS
        );
        assert_eq!(
            coverage_repair_start(start, end, Some(start + HOUR_MS), Some(end)),
            start,
            "a wider requirement rebases instead of pretending old coverage still exists"
        );
        assert_eq!(
            coverage_repair_start(
                start,
                end,
                Some(start - 10 * HOUR_MS),
                Some(start - HOUR_MS)
            ),
            start,
            "a wholly retired frontier is replaced after long downtime"
        );
    }

    #[test]
    fn publication_horizon_does_not_claim_the_just_closed_hour() {
        let boundary = 1_000 * HOUR_MS;
        assert_eq!(closed_kline_end(boundary + 59_999), boundary - HOUR_MS);
        assert_eq!(closed_kline_end(boundary + 60_000), boundary);
    }

    #[test]
    fn runtime_stays_starting_only_for_the_bounded_cycle_warmup() {
        assert_eq!(
            startup_runtime_status(
                "degraded",
                [(None, 60_000), (None, 60_000)],
                true,
                1_000,
                STARTUP_MAX_MS,
            ),
            "starting"
        );
        assert_eq!(
            startup_runtime_status(
                "ready",
                [(Some(2_000), 60_000), (Some(3_000), 60_000)],
                true,
                1_000,
                3_000,
            ),
            "ready"
        );
        assert_eq!(
            startup_runtime_status(
                "degraded",
                [(Some(2_000), 60_000), (None, 60_000)],
                true,
                1_000,
                STARTUP_MAX_MS + 1_000,
            ),
            "degraded"
        );
        assert_eq!(
            startup_runtime_status(
                "degraded",
                [(None, 60_000), (None, 60_000)],
                false,
                1_000,
                60_000,
            ),
            "degraded",
            "a disconnected or incomplete stream is not healthy warmup"
        );
        assert_eq!(
            startup_runtime_status(
                "ready",
                [(Some(1_000), 60_000), (Some(200_000), 60_000)],
                true,
                1_000,
                200_001,
            ),
            "degraded",
            "a completed LONG lane cannot remain ready after it stalls"
        );
        assert_eq!(
            startup_runtime_status(
                "ready",
                [(Some(200_000), 60_000), (Some(200_002), 60_000)],
                true,
                1_000,
                200_001,
            ),
            "degraded",
            "a future cycle timestamp is not fresh"
        );
    }

    #[test]
    fn a_cold_start_still_filling_ticker_coverage_is_starting_not_degraded() {
        let started_at_ms = 1_000_000;
        let now_ms = started_at_ms + 274_000;
        // What a worker 274 s into its cold start has: socket up, every topic
        // accepted, frames arriving, the boot repair gap still open and the
        // ticker cache still filling.
        let booting = StreamHealth {
            connected: true,
            epoch: 1,
            gap_open: true,
            gap_open_since_ms: Some(started_at_ms + 13_000),
            last_frame_ts_ms: Some(now_ms - 1),
            ticker_capacity: 2,
            ticker_coverage_complete: false,
            ticker_topics_accepted: 2,
            kline_topics_accepted: 2,
            ..StreamHealth::default()
        };
        let cycles = [(None, 60_000), (None, 60_000)];
        let mut old_recovery = Some(started_at_ms + 1);
        assert_eq!(runtime_status(&booting, true, now_ms, 30_000), "degraded");
        assert_eq!(
            heartbeat_status(
                &booting,
                true,
                cycles,
                started_at_ms,
                now_ms,
                30_000,
                &mut old_recovery,
            ),
            "starting",
            "a sound stream that has not finished filling coverage is warmup, not a fault"
        );

        let disconnected = StreamHealth {
            connected: false,
            ..booting.clone()
        };
        let mut disconnected_recovery = None;
        assert_eq!(
            heartbeat_status(
                &disconnected,
                true,
                cycles,
                started_at_ms,
                now_ms,
                30_000,
                &mut disconnected_recovery,
            ),
            "degraded",
            "a disconnected stream is a fault from the first heartbeat"
        );

        let refused = StreamHealth {
            ticker_topics_quarantined: 1,
            ..booting.clone()
        };
        let mut refused_recovery = None;
        assert_eq!(
            heartbeat_status(
                &refused,
                true,
                cycles,
                started_at_ms,
                now_ms,
                30_000,
                &mut refused_recovery,
            ),
            "degraded",
            "a refused topic never fills, so it is not warmup"
        );

        let mut expired_startup_recovery = Some(started_at_ms + 1);
        assert_eq!(
            heartbeat_status(
                &booting,
                true,
                cycles,
                started_at_ms,
                started_at_ms + STARTUP_MAX_MS,
                30_000,
                &mut expired_startup_recovery,
            ),
            "degraded",
            "past the cold-start bound an unfinished backfill is a fault"
        );
        let mut completed_cycle_recovery = Some(started_at_ms + 1);
        assert_eq!(
            heartbeat_status(
                &booting,
                false,
                [
                    (Some(now_ms - 1_000), 60_000),
                    (Some(now_ms - 1_000), 60_000)
                ],
                started_at_ms,
                now_ms,
                30_000,
                &mut completed_cycle_recovery,
            ),
            "degraded",
            "once both cycles have run, incomplete coverage is the live verdict again"
        );
    }

    #[tokio::test]
    async fn a_live_epoch_adopts_the_repair_already_started_at_boot() {
        let root = temporary_root("repair-adopts-epoch");
        let _ = std::fs::remove_dir_all(&root);
        let options = LiveRunOptions {
            state_dir: root.join("state"),
            spool_dir: root.join("spool"),
            heartbeat: root.join("heartbeat.json"),
        };
        let runner =
            LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
        let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
        let mut lanes = LaneState {
            repair: true,
            ..LaneState::default()
        };

        runner
            .start_kline_repair(&lane_tx, &mut lanes, Some(7))
            .unwrap();

        assert!(lanes.repair);
        assert_eq!(lanes.repair_epoch, Some(7));
        drop(runner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn a_repair_restarted_without_an_epoch_keeps_the_live_one() {
        // The carry catch-up and the instrument lane restart the repair lane
        // with no epoch, and only `mark_gap_repaired(epoch)` closes the
        // WebSocket gap. Dropping the epoch there left the gap open for the
        // life of the process, however complete the coverage became.
        let root = temporary_root("repair-restart-keeps-epoch");
        let _ = std::fs::remove_dir_all(&root);
        let options = LiveRunOptions {
            state_dir: root.join("state"),
            spool_dir: root.join("spool"),
            heartbeat: root.join("heartbeat.json"),
        };
        let mut runner =
            LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
        let mut stream =
            BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap();
        let mut pending = BTreeMap::new();
        let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
        let mut lanes = LaneState {
            repair: true,
            repair_epoch: Some(4),
            ..LaneState::default()
        };

        runner
            .handle_lane_completion(
                LaneCompletion::RepairFinished {
                    end_ms: 100 * DAY_MS,
                    epoch: None,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .unwrap();
        assert_eq!(
            lanes.repair_epoch,
            Some(4),
            "a finished repair leaves the live epoch for the next pass"
        );

        lanes.repair = false;
        runner
            .start_kline_repair(&lane_tx, &mut lanes, None)
            .unwrap();
        assert!(lanes.repair);
        assert_eq!(
            lanes.repair_epoch,
            Some(4),
            "an epoch-less restart keeps the epoch that closes the gap"
        );

        drop(stream);
        drop(runner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn runtime_ready_requires_exact_live_ticker_and_kline_topics() {
        let now_ms = 1_000_000;
        let mut health = StreamHealth {
            connected: true,
            gap_open: false,
            last_frame_ts_ms: Some(now_ms - 1),
            ticker_capacity: 2,
            ticker_coverage_complete: true,
            ticker_topics_accepted: 2,
            kline_topics_accepted: 2,
            ..StreamHealth::default()
        };
        assert_eq!(runtime_status(&health, false, now_ms, 30_000), "ready");

        health.kline_topics_accepted = 1;
        assert_eq!(runtime_status(&health, false, now_ms, 30_000), "degraded");
        health.kline_topics_accepted = 2;
        health.ticker_topics_quarantined = 1;
        assert_eq!(runtime_status(&health, false, now_ms, 30_000), "degraded");
        health.ticker_topics_quarantined = 0;
        health.ticker_coverage_complete = false;
        assert_eq!(runtime_status(&health, false, now_ms, 30_000), "degraded");
    }

    #[test]
    fn transient_recovery_is_bounded_and_transport_failures_are_immediate() {
        let started_at_ms = 1_000_000;
        let mut health = StreamHealth {
            connected: true,
            gap_open: true,
            last_frame_ts_ms: Some(started_at_ms + 59_999),
            ticker_capacity: 2,
            ticker_coverage_complete: false,
            ticker_topics_accepted: 2,
            kline_topics_accepted: 2,
            ..StreamHealth::default()
        };
        let mut recovery_started_at_ms = None;

        assert!(transient_recovery_acceptable(
            &health,
            true,
            true,
            &mut recovery_started_at_ms,
            started_at_ms + 60_000,
        ));
        assert_eq!(recovery_started_at_ms, Some(started_at_ms + 60_000));
        assert!(!transient_recovery_acceptable(
            &health,
            true,
            true,
            &mut recovery_started_at_ms,
            started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
        ));

        health.ticker_coverage_complete = true;
        health.gap_open = false;
        assert!(transient_recovery_acceptable(
            &health,
            false,
            true,
            &mut recovery_started_at_ms,
            started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
        ));
        assert_eq!(recovery_started_at_ms, None);

        health.ticker_coverage_complete = false;
        assert!(!transient_recovery_acceptable(
            &health,
            false,
            false,
            &mut recovery_started_at_ms,
            started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
        ));
        assert_eq!(recovery_started_at_ms, None);
        assert!(stream_transport_healthy(
            &health,
            started_at_ms + 60_000,
            180_000,
        ));

        health.gap_open = true;
        health.last_frame_ts_ms = Some(started_at_ms + 59_999);
        let cycles = [
            (Some(started_at_ms + 59_000), 60_000),
            (Some(started_at_ms + 59_000), 60_000),
        ];
        let mut heartbeat_recovery = None;
        assert_eq!(
            heartbeat_status(
                &health,
                true,
                cycles,
                started_at_ms - STARTUP_MAX_MS,
                started_at_ms + 60_000,
                180_000,
                &mut heartbeat_recovery,
            ),
            "recovering"
        );
        health.last_frame_ts_ms = Some(started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS - 1);
        assert_eq!(
            heartbeat_status(
                &health,
                true,
                cycles,
                started_at_ms - STARTUP_MAX_MS,
                started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
                180_000,
                &mut heartbeat_recovery,
            ),
            "degraded"
        );
    }

    #[test]
    fn optional_whale_lane_never_blocks_a_carry_cycle() {
        let lanes = LaneState {
            instruments_ready: true,
            funding_ready: true,
            whales: true,
            ..LaneState::default()
        };
        assert!(!carry_required_lanes_pending(&lanes));
    }

    #[test]
    fn funding_fetch_chunks_bound_retained_results_independently_of_population() {
        let jobs = (0..10_003)
            .map(|index| (format!("S{index:05}USDT"), 1, 2, false))
            .collect::<Vec<_>>();
        let sizes = funding_job_chunks(&jobs)
            .map(|chunk| chunk.len())
            .collect::<Vec<_>>();

        assert_eq!(sizes.iter().sum::<usize>(), jobs.len());
        assert_eq!(sizes.iter().copied().max(), Some(FUNDING_FETCH_CHUNK_SIZE));
        assert!(sizes
            .iter()
            .all(|size| *size > 0 && *size <= FUNDING_FETCH_CHUNK_SIZE));
        assert_eq!(sizes.last(), Some(&1));
        let retained_order = funding_job_chunks(&jobs)
            .flat_map(|chunk| chunk.iter().map(|job| job.0.as_str()))
            .collect::<Vec<_>>();
        assert_eq!(
            retained_order,
            jobs.iter().map(|job| job.0.as_str()).collect::<Vec<_>>()
        );
    }

    #[test]
    fn source_pagination_grids_have_exact_per_job_row_ceilings() {
        let start = 100 * DAY_MS;
        let carry_end = start + crate::config::MAX_CARRY_SOURCE_HISTORY_HOURS * HOUR_MS;
        let widest_merged_kline_end =
            start + 3 * crate::config::MAX_CARRY_SOURCE_HISTORY_HOURS * HOUR_MS;
        assert_eq!(
            source_grid_slots(start, widest_merged_kline_end, HOUR_MS, false).unwrap(),
            13_104
        );
        assert_eq!(
            source_grid_slots(start, carry_end, HOUR_MS, true).unwrap(),
            4_369
        );
        let whale_end = start + crate::config::MAX_WHALE_FEED_DAYS as i64 * DAY_MS;
        assert_eq!(
            source_grid_slots(start, whale_end, super::FIVE_MIN_MS, true).unwrap(),
            8_641
        );
        validate_source_grid_timestamp(start, start, carry_end, HOUR_MS, false, "test kline")
            .unwrap();
        assert!(validate_source_grid_timestamp(
            start + 1,
            start,
            carry_end,
            HOUR_MS,
            false,
            "test kline",
        )
        .is_err());
        assert!(validate_source_grid_timestamp(
            carry_end,
            start,
            carry_end,
            HOUR_MS,
            false,
            "test kline",
        )
        .is_err());
        validate_source_page_rows(200, 200, "test funding").unwrap();
        assert!(validate_source_page_rows(201, 200, "test funding").is_err());
    }

    #[test]
    fn unaligned_cold_bootstrap_whale_bounds_keep_the_floor_point_fetchable() {
        let now_ms = 200 * DAY_MS + 12_345;
        let start_ms = now_ms - crate::config::MAX_WHALE_FEED_DAYS as i64 * DAY_MS;
        let (query_start_ms, query_end_ms, retained_row_cap) =
            whale_fetch_bounds(start_ms, now_ms).unwrap();

        assert!(query_start_ms < start_ms);
        assert_eq!(query_start_ms.rem_euclid(super::FIVE_MIN_MS), 0);
        assert_eq!(query_end_ms.rem_euclid(super::FIVE_MIN_MS), 0);
        assert_eq!(retained_row_cap, 8_640);
        validate_source_grid_timestamp(
            query_start_ms,
            query_start_ms,
            query_end_ms,
            super::FIVE_MIN_MS,
            true,
            "test whale",
        )
        .unwrap();
    }

    #[tokio::test]
    async fn repair_fetch_waits_for_commit_ack_before_retaining_the_next_result() {
        assert_eq!(KLINE_FETCH_CHUNK_SIZE, 1);
        assert_eq!(LANE_COMPLETION_QUEUE_CAPACITY, 1);
        let jobs = (0..10_003)
            .map(|index| (format!("S{index:05}USDT"), 1, 2))
            .collect::<Vec<_>>();
        assert_eq!(kline_job_chunks(&jobs).count(), jobs.len());
        assert!(kline_job_chunks(&jobs).all(|chunk| chunk.len() == 1));

        let (lane_tx, mut lane_rx) = tokio::sync::mpsc::channel(1);
        let producer = tokio::spawn(async move {
            for _ in 0..2 {
                if !send_repair_chunk_and_wait(
                    &lane_tx,
                    Ok(FetchedKlineJobs {
                        batches: Vec::new(),
                        failures: Vec::new(),
                    }),
                )
                .await
                {
                    return false;
                }
            }
            true
        });

        let first = lane_rx.recv().await.expect("first repair result");
        assert!(!producer.is_finished());
        assert!(matches!(
            lane_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        ));
        let LaneCompletion::RepairChunk { resume, .. } = first else {
            panic!("repair producer sent the wrong completion")
        };
        resume.send(true).expect("acknowledge first repair commit");

        let second = lane_rx.recv().await.expect("second repair result");
        assert!(!producer.is_finished());
        assert!(matches!(
            lane_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        ));
        let LaneCompletion::RepairChunk { resume, .. } = second else {
            panic!("repair producer sent the wrong completion")
        };
        resume.send(false).expect("refuse second repair commit");
        assert!(!producer.await.expect("repair producer joins"));
    }

    #[tokio::test]
    async fn whale_fetch_waits_for_commit_ack_before_retaining_the_next_result() {
        assert_eq!(WHALE_FETCH_CHUNK_SIZE, 1);
        assert_eq!(LANE_COMPLETION_QUEUE_CAPACITY, 1);
        let jobs = (0..10_003)
            .map(|index| (format!("S{index:05}USDT"), 1, 2))
            .collect::<Vec<_>>();
        assert_eq!(whale_job_chunks(&jobs).count(), jobs.len());
        assert!(whale_job_chunks(&jobs).all(|chunk| chunk.len() == 1));

        let (lane_tx, mut lane_rx) = tokio::sync::mpsc::channel(1);
        let producer = tokio::spawn(async move {
            for _ in 0..2 {
                if !send_whale_chunk_and_wait(
                    &lane_tx,
                    Ok(FetchedWhales {
                        available_at_ms: DAY_MS,
                        rows: Vec::new(),
                        coverage: Vec::new(),
                    }),
                )
                .await
                {
                    return false;
                }
            }
            true
        });

        let first = lane_rx.recv().await.expect("first whale result");
        assert!(!producer.is_finished());
        assert!(matches!(
            lane_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        ));
        let LaneCompletion::WhaleChunk { resume, .. } = first else {
            panic!("whale producer sent the wrong completion")
        };
        resume.send(true).expect("acknowledge first whale commit");

        let second = lane_rx.recv().await.expect("second whale result");
        assert!(!producer.is_finished());
        assert!(matches!(
            lane_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        ));
        let LaneCompletion::WhaleChunk { resume, .. } = second else {
            panic!("whale producer sent the wrong completion")
        };
        resume.send(false).expect("refuse second whale commit");
        assert!(!producer.await.expect("whale producer joins"));
    }

    #[tokio::test]
    async fn malformed_source_lanes_retry_without_stopping_long() {
        let root = temporary_root("lane-source-errors");
        let _ = std::fs::remove_dir_all(&root);
        let universe = test_universe();
        let options = LiveRunOptions {
            state_dir: root.join("state"),
            spool_dir: root.join("spool"),
            heartbeat: root.join("heartbeat.json"),
        };
        let mut runner =
            LiveRunner::new_with_universe(checked_demo_config(), universe, options).unwrap();
        let mut stream =
            BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap();
        let mut pending = BTreeMap::new();
        let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
        let mut lanes = LaneState {
            instruments: true,
            tickers: true,
            funding: true,
            whales: true,
            repair: true,
            ..LaneState::default()
        };
        let available_at_ms = 100 * DAY_MS;

        runner
            .handle_lane_completion(
                LaneCompletion::Instruments(Ok(FetchedUniverseInputs {
                    instruments: FetchedInstruments {
                        observed_ts_ms: available_at_ms,
                        available_at_ms,
                        rows: vec![instrument_wire(
                            "BTCUSDT",
                            "Trading",
                            DAY_MS,
                            Some(50 * DAY_MS),
                        )],
                    },
                    tickers: FetchedTickers {
                        request_started_at_ms: available_at_ms,
                        observed_ts_ms: available_at_ms,
                        available_at_ms,
                        rows: Vec::new(),
                    },
                })),
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("malformed instrument input stays lane-local");
        assert!(!lanes.instruments, "instrument cadence can retry the lane");

        runner
            .handle_lane_completion(
                LaneCompletion::Tickers(Ok(FetchedTickers {
                    request_started_at_ms: available_at_ms,
                    observed_ts_ms: available_at_ms,
                    available_at_ms,
                    rows: vec![ticker_wire_with_mark("not-a-number")],
                })),
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("malformed ticker input stays lane-local");
        assert!(!lanes.tickers, "ticker fallback can retry the lane");
        assert_eq!(runner.rest_ticker_failure_count, 1);

        let (funding_resume, funding_ack) = tokio::sync::oneshot::channel();
        runner
            .handle_lane_completion(
                LaneCompletion::FundingChunk {
                    result: Ok(FetchedFunding {
                        batches: vec![(
                            "BTCUSDT".into(),
                            FetchedFundingBatch {
                                rows: vec![BybitFundingWire {
                                    funding_rate_timestamp: Value::from(available_at_ms),
                                    funding_rate: Value::from("not-a-number"),
                                    funding_interval_hour: Some(Value::from(1)),
                                }],
                                available_at_ms,
                                checked_from_ms: None,
                                checked_through_ms: None,
                                emit_lifecycle: false,
                            },
                        )],
                        failures: Vec::new(),
                    }),
                    resume: funding_resume,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("malformed funding input stays lane-local");
        assert!(!funding_ack.await.expect("funding producer receives ack"));
        runner
            .handle_lane_completion(
                LaneCompletion::FundingFinished { succeeded: false },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .unwrap();
        assert!(!lanes.funding);
        assert!(!lanes.funding_ready);

        let (whale_resume, whale_ack) = tokio::sync::oneshot::channel();
        runner
            .handle_lane_completion(
                LaneCompletion::WhaleChunk {
                    result: Ok(FetchedWhales {
                        available_at_ms,
                        rows: vec![BinanceWhaleWire {
                            symbol: "BTCUSDT".into(),
                            day_end_ms: Value::from(available_at_ms),
                            long_short_ratio: Some(Value::from("not-a-number")),
                        }],
                        coverage: Vec::new(),
                    }),
                    resume: whale_resume,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("malformed optional whale input stays lane-local");
        assert!(whale_ack.await.expect("whale producer receives ack"));
        runner
            .handle_lane_completion(
                LaneCompletion::WhaleFinished,
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .unwrap();
        assert!(!lanes.whales);

        let (repair_resume, repair_ack) = tokio::sync::oneshot::channel();
        runner
            .handle_lane_completion(
                LaneCompletion::RepairChunk {
                    result: Ok(FetchedKlineJobs {
                        batches: vec![(
                            "BTCUSDT".into(),
                            FetchedKlineBatch {
                                rows: vec![vec![
                                    Value::from(available_at_ms - HOUR_MS),
                                    Value::from("not-a-number"),
                                    Value::from("101"),
                                    Value::from("99"),
                                    Value::from("100"),
                                    Value::from("1"),
                                    Value::from("100"),
                                ]],
                                available_at_ms,
                                checked_from_ms: None,
                                checked_through_ms: None,
                            },
                        )],
                        failures: Vec::new(),
                    }),
                    resume: repair_resume,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("malformed repair input stays lane-local");
        assert!(!repair_ack.await.expect("repair producer receives ack"));
        runner
            .handle_lane_completion(
                LaneCompletion::RepairFinished {
                    end_ms: available_at_ms,
                    epoch: None,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .unwrap();
        assert!(!lanes.repair, "kline cadence can retry the repair lane");

        assert_eq!(runner.durable.worker().state().last_input_sequence, 0);
        runner
            .long_watermark(available_at_ms, Vec::new())
            .expect("LONG remains runnable after optional source failures");
        assert_eq!(runner.durable.worker().state().last_input_sequence, 1);

        drop(stream);
        drop(runner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn malformed_websocket_rows_open_a_repairable_gap_without_stopping_long() {
        let root = temporary_root("websocket-source-errors");
        let _ = std::fs::remove_dir_all(&root);
        let options = LiveRunOptions {
            state_dir: root.join("state"),
            spool_dir: root.join("spool"),
            heartbeat: root.join("heartbeat.json"),
        };
        let mut runner =
            LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
        let mut stream =
            BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap();
        let available_at_ms = 100 * DAY_MS;

        runner
            .commit_stream_ticker_sample(
                &mut stream,
                TickerSample {
                    observed_ts_ms: available_at_ms,
                    available_at_ms,
                    rows: vec![ticker_wire_with_mark("not-a-number")],
                },
            )
            .expect("malformed WebSocket ticker stays source-local");
        assert!(stream.health().gap_open);
        assert_eq!(stream.health().fault_count, 1);
        assert_eq!(runner.durable.worker().state().last_input_sequence, 0);

        let mut pending = BTreeMap::new();
        let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
        let mut lanes = LaneState::default();
        runner
            .handle_stream_event(
                StreamEvent::KlineClosed(crate::bybit_ws::ConfirmedKline {
                    symbol: "BTCUSDT".into(),
                    available_at_ms,
                    row: vec![
                        Value::from(available_at_ms - HOUR_MS),
                        Value::from("not-a-number"),
                        Value::from("101"),
                        Value::from("99"),
                        Value::from("100"),
                        Value::from("1"),
                        Value::from("100"),
                    ],
                }),
                &mut stream,
                &mut pending,
                64,
                &lane_tx,
                &mut lanes,
            )
            .expect("malformed WebSocket kline stays source-local");
        assert!(stream.health().gap_open);
        assert_eq!(stream.health().fault_count, 2);
        assert!(lanes.repair, "the REST repair lane is scheduled");
        assert_eq!(runner.durable.worker().state().last_input_sequence, 0);

        runner
            .long_watermark(available_at_ms, Vec::new())
            .expect("LONG remains runnable after WebSocket source faults");
        assert_eq!(runner.durable.worker().state().last_input_sequence, 1);

        drop(stream);
        drop(runner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn revised_source_history_is_rejected_before_durable_mutation() {
        let root = temporary_root("lane-source-rewrite");
        let _ = std::fs::remove_dir_all(&root);
        let options = LiveRunOptions {
            state_dir: root.join("state"),
            spool_dir: root.join("spool"),
            heartbeat: root.join("heartbeat.json"),
        };
        let mut runner =
            LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
        let available_at_ms = 100 * DAY_MS;
        let open_ts_ms = available_at_ms - HOUR_MS;
        runner
            .commit(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence: runner.next_sequence().unwrap(),
                symbol: "BTCUSDT".into(),
                available_at_ms,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                rows: vec![kline_wire(open_ts_ms, "100")],
            })
            .unwrap();
        runner
            .commit(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: runner.next_sequence().unwrap(),
                symbol: "BTCUSDT".into(),
                available_at_ms,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: false,
                rows: vec![BybitFundingWire {
                    funding_rate_timestamp: Value::from(available_at_ms),
                    funding_rate: Value::from("0.001"),
                    funding_interval_hour: Some(Value::from(1)),
                }],
            })
            .unwrap();
        runner
            .commit(WireEvent::BinanceWhaleBatch {
                schema_version: SCHEMA_VERSION,
                sequence: runner.next_sequence().unwrap(),
                available_at_ms,
                coverage: Vec::new(),
                rows: vec![BinanceWhaleWire {
                    symbol: "BTCUSDT".into(),
                    day_end_ms: Value::from(available_at_ms),
                    long_short_ratio: Some(Value::from("1.2")),
                }],
            })
            .unwrap();
        let state_before = serde_json::to_vec(runner.durable.worker().state()).unwrap();
        let mut stream =
            BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap();
        let mut pending = BTreeMap::new();
        let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
        let mut lanes = LaneState {
            funding: true,
            whales: true,
            repair: true,
            ..LaneState::default()
        };

        let (funding_resume, funding_ack) = tokio::sync::oneshot::channel();
        runner
            .handle_lane_completion(
                LaneCompletion::FundingChunk {
                    result: Ok(FetchedFunding {
                        batches: vec![(
                            "BTCUSDT".into(),
                            FetchedFundingBatch {
                                rows: vec![BybitFundingWire {
                                    funding_rate_timestamp: Value::from(available_at_ms),
                                    funding_rate: Value::from("0.002"),
                                    funding_interval_hour: Some(Value::from(1)),
                                }],
                                available_at_ms,
                                checked_from_ms: None,
                                checked_through_ms: None,
                                emit_lifecycle: false,
                            },
                        )],
                        failures: Vec::new(),
                    }),
                    resume: funding_resume,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("revised funding stays lane-local");
        assert!(!funding_ack.await.unwrap());
        assert_eq!(
            serde_json::to_vec(runner.durable.worker().state()).unwrap(),
            state_before
        );

        let (whale_resume, whale_ack) = tokio::sync::oneshot::channel();
        runner
            .handle_lane_completion(
                LaneCompletion::WhaleChunk {
                    result: Ok(FetchedWhales {
                        available_at_ms,
                        rows: vec![BinanceWhaleWire {
                            symbol: "BTCUSDT".into(),
                            day_end_ms: Value::from(available_at_ms),
                            long_short_ratio: Some(Value::from("1.3")),
                        }],
                        coverage: Vec::new(),
                    }),
                    resume: whale_resume,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("revised optional whale history stays lane-local");
        assert!(whale_ack.await.unwrap());
        assert_eq!(
            serde_json::to_vec(runner.durable.worker().state()).unwrap(),
            state_before
        );

        let (repair_resume, repair_ack) = tokio::sync::oneshot::channel();
        runner
            .handle_lane_completion(
                LaneCompletion::RepairChunk {
                    result: Ok(FetchedKlineJobs {
                        batches: vec![(
                            "BTCUSDT".into(),
                            FetchedKlineBatch {
                                rows: vec![kline_wire(open_ts_ms, "101")],
                                available_at_ms,
                                checked_from_ms: None,
                                checked_through_ms: None,
                            },
                        )],
                        failures: Vec::new(),
                    }),
                    resume: repair_resume,
                },
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect("revised repair history stays lane-local");
        assert!(!repair_ack.await.unwrap());
        assert_eq!(
            serde_json::to_vec(runner.durable.worker().state()).unwrap(),
            state_before
        );

        lanes.repair = false;
        pending.insert(
            ("BTCUSDT".into(), open_ts_ms),
            crate::bybit_ws::ConfirmedKline {
                symbol: "BTCUSDT".into(),
                available_at_ms,
                row: kline_wire(open_ts_ms, "101"),
            },
        );
        assert!(!runner
            .flush_pending_klines_or_recover(&mut stream, &mut pending, &lane_tx, &mut lanes,)
            .expect("a durable-history WS rewrite stays source-local"));
        assert!(pending.is_empty());
        assert!(stream.health().gap_open);
        assert!(
            lanes.repair,
            "the durable WS conflict schedules REST repair"
        );
        assert_eq!(
            serde_json::to_vec(runner.durable.worker().state()).unwrap(),
            state_before
        );

        drop(stream);
        drop(runner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn durable_lane_commit_error_still_terminates_the_shared_loop() {
        let root = temporary_root("lane-durable-error");
        let _ = std::fs::remove_dir_all(&root);
        let state_dir = root.join("state");
        let options = LiveRunOptions {
            state_dir: state_dir.clone(),
            spool_dir: root.join("spool"),
            heartbeat: root.join("heartbeat.json"),
        };
        let mut runner =
            LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
        let mut stream =
            BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap();
        let mut pending = BTreeMap::new();
        let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
        let mut lanes = LaneState {
            tickers: true,
            ..LaneState::default()
        };
        std::fs::remove_dir_all(&state_dir).unwrap();
        std::fs::write(&state_dir, b"block journal directory recreation").unwrap();
        let observed_ts_ms = 100 * DAY_MS;

        let error = runner
            .handle_lane_completion(
                LaneCompletion::Tickers(Ok(FetchedTickers {
                    request_started_at_ms: observed_ts_ms,
                    observed_ts_ms,
                    available_at_ms: observed_ts_ms,
                    rows: vec![ticker_wire_with_mark("100")],
                })),
                &mut stream,
                &mut pending,
                &lane_tx,
                &mut lanes,
            )
            .expect_err("durable journal errors remain process-fatal");
        assert!(error.to_string().starts_with("io:"), "{error}");

        drop(stream);
        drop(runner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn only_source_failure_categories_are_lane_local() {
        assert!(WorkerError::input("bad venue row").is_lane_local_source_failure());
        assert!(WorkerError::network("venue unavailable").is_lane_local_source_failure());
        assert!(!WorkerError::state("broken invariant").is_lane_local_source_failure());
        assert!(!WorkerError::config("bad config").is_lane_local_source_failure());
        assert!(
            !WorkerError::io("durable write", std::io::Error::other("disk failure"))
                .is_lane_local_source_failure()
        );
    }

    #[test]
    fn delivery_bounds_kline_funding_and_whale_plans_end_exclusively() {
        let launch = 10 * HOUR_MS + HOUR_MS / 2;
        let delivery = 20 * HOUR_MS + HOUR_MS / 2;
        let intervals = [InstrumentTradingInterval {
            trading_from_ms: launch,
            trading_through_ms: Some(delivery),
        }];
        assert_eq!(
            bounded_instrument_source_ranges(
                Some(&intervals),
                None,
                5 * HOUR_MS,
                30 * HOUR_MS,
                HOUR_MS,
            ),
            vec![(11 * HOUR_MS, 20 * HOUR_MS)]
        );
        assert_eq!(
            bounded_instrument_source_ranges(Some(&intervals), None, 0, 2 * DAY_MS, DAY_MS,),
            Vec::<(i64, i64)>::new(),
            "no complete UTC whale day exists inside a same-day listing interval"
        );
        assert!(trading_intervals_contain(
            Some(&intervals),
            None,
            delivery - 1
        ));
        assert!(!trading_intervals_contain(Some(&intervals), None, delivery));
        assert_eq!(
            bounded_instrument_source_ranges(
                Some(&intervals),
                Some(18 * HOUR_MS + HOUR_MS / 2),
                5 * HOUR_MS,
                30 * HOUR_MS,
                HOUR_MS,
            ),
            vec![(11 * HOUR_MS, 18 * HOUR_MS)],
            "an unknown authoritative status fails closed without inventing a delivery clock"
        );
    }

    #[test]
    fn incomplete_whale_day_stays_uncovered_until_a_complete_row_arrives() {
        let start = 100 * crate::DAY_MS;
        let end = start + crate::DAY_MS;
        assert!(complete_whale_coverage("BTCUSDT", start, end, &[])
            .unwrap()
            .is_empty());
        let complete = complete_whale_coverage(
            "BTCUSDT",
            start,
            end,
            &[BinanceWhaleWire {
                symbol: "BTCUSDT".into(),
                day_end_ms: Value::from(end),
                long_short_ratio: Some(Value::from("1.2")),
            }],
        )
        .unwrap();
        assert_eq!(complete.len(), 1);
        assert_eq!(complete[0].checked_from_ms, start);
        assert_eq!(complete[0].checked_through_ms, end);
    }

    #[test]
    fn funding_coverage_keeps_empty_late_and_internal_holes_retryable() {
        let start = 100 * HOUR_MS;
        let end = start + 6 * HOUR_MS;
        let row = |timestamp| BybitFundingWire {
            funding_rate_timestamp: Value::from(timestamp),
            funding_rate: Value::from("-0.001"),
            funding_interval_hour: Some(Value::from(1)),
        };
        assert!(complete_funding_coverage(start, end, HOUR_MS, &[])
            .unwrap()
            .is_empty());
        let late = (1..6)
            .map(|offset| row(start + offset * HOUR_MS))
            .collect::<Vec<_>>();
        assert_eq!(
            complete_funding_coverage(start, end, HOUR_MS, &late).unwrap(),
            vec![(start, end - HOUR_MS)]
        );
        let complete = (1..=6)
            .map(|offset| row(start + offset * HOUR_MS))
            .collect::<Vec<_>>();
        assert_eq!(
            complete_funding_coverage(start, end, HOUR_MS, &complete).unwrap(),
            vec![(start, end)]
        );
        let with_hole = complete
            .into_iter()
            .filter(|item| item.funding_rate_timestamp != start + 3 * HOUR_MS)
            .collect::<Vec<_>>();
        let intervals = complete_funding_coverage(start, end, HOUR_MS, &with_hole).unwrap();
        assert_eq!(intervals.len(), 2);
        assert!(intervals[0].1 < intervals[1].0);
        assert!(intervals
            .iter()
            .all(|(from, through)| !(*from <= start + 3 * HOUR_MS
                && *through >= start + 3 * HOUR_MS)));
    }

    #[test]
    fn retained_fragmented_coverage_converges_without_refetching_a_known_run() {
        let base = 100 * HOUR_MS;
        let intervals = (0..6)
            .map(|index| CoverageInterval {
                checked_from_ms: base + index * 2 * HOUR_MS,
                checked_through_ms: base + (index * 2 + 1) * HOUR_MS,
            })
            .collect::<Vec<_>>();
        let coverage = BTreeMap::from([("BTCUSDT".to_owned(), intervals.clone())]);
        let empty = BTreeMap::new();

        for interval in &intervals {
            assert!(source_coverage_contains(
                &empty,
                &empty,
                &coverage,
                "BTCUSDT",
                interval.checked_from_ms,
                interval.checked_through_ms,
            ));
        }
        assert!(!source_coverage_contains(
            &empty,
            &empty,
            &coverage,
            "BTCUSDT",
            base + HOUR_MS,
            base + 2 * HOUR_MS,
        ));
    }

    #[test]
    fn durable_carry_catchup_crosses_delivery_without_post_delivery_refetch() {
        let config = checked_demo_config();
        let universe = UniverseIdentity {
            mode: UniverseMode::Pit,
            environment: "demo".into(),
            endpoint: "api-demo.bybit.com".into(),
            snapshot_ts_ms: 100 * DAY_MS,
            available_at_ms: 100 * DAY_MS + 1,
            artifact_sha256: "1".repeat(64),
            file_sha256: "2".repeat(64),
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
            long_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
            carry_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        };
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 212 * DAY_MS,
                available_at_ms: 212 * DAY_MS,
                rows: vec![
                    instrument_wire("BTCUSDT", "Closed", 100 * DAY_MS, Some(210 * DAY_MS)),
                    instrument_wire("ETHUSDT", "Trading", 100 * DAY_MS, None),
                ],
            })
            .unwrap();
        let mut state = worker.state().clone();
        for symbol in ["BTCUSDT", "ETHUSDT"] {
            let klines = state.klines.entry(symbol.into()).or_default();
            for open_ts_ms in (100 * DAY_MS..212 * DAY_MS).step_by(HOUR_MS as usize) {
                klines.insert(
                    open_ts_ms,
                    HourlyKline {
                        symbol: symbol.into(),
                        open_ts_ms,
                        available_at_ms: open_ts_ms + HOUR_MS,
                        open: 100.0,
                        high: 102.0,
                        low: 99.0,
                        close: 100.0 + open_ts_ms as f64 / DAY_MS as f64,
                        volume_base: 1.0,
                        turnover_quote: 100.0,
                    },
                );
            }
            let funding = state.funding.entry(symbol.into()).or_default();
            for settlement_ts_ms in
                (100 * DAY_MS + HOUR_MS..=212 * DAY_MS).step_by(HOUR_MS as usize)
            {
                funding.insert(
                    settlement_ts_ms,
                    SettledFunding {
                        symbol: symbol.into(),
                        settlement_ts_ms,
                        available_at_ms: settlement_ts_ms,
                        rate: -0.001,
                        funding_interval_min: 60,
                    },
                );
            }
            let through = if symbol == "BTCUSDT" {
                210 * DAY_MS
            } else {
                212 * DAY_MS
            };
            state.kline_coverage_intervals.insert(
                symbol.into(),
                vec![CoverageInterval {
                    checked_from_ms: 100 * DAY_MS,
                    checked_through_ms: through,
                }],
            );
            state.funding_coverage_intervals.insert(
                symbol.into(),
                vec![CoverageInterval {
                    checked_from_ms: 100 * DAY_MS,
                    checked_through_ms: through,
                }],
            );
        }
        state.last_carry_decision_ts_ms = Some(207 * DAY_MS);
        state.last_carry_scorer_ts_ms = Some(207 * DAY_MS);
        state.last_observed_ts_ms = 212 * DAY_MS;
        state.bootstrap_coverage = Some(BootstrapCoverage {
            completed_at_ms: 212 * DAY_MS,
            kline_end_ms: 212 * DAY_MS,
            funding_end_ms: 212 * DAY_MS,
            whale_end_ms: 212 * DAY_MS,
            source_contract_sha256: state.source_contract_sha256.clone(),
            long_feature_sha256: state.long_feature_sha256.clone(),
            carry_feature_sha256: state.carry_feature_sha256.clone(),
        });

        let root = temporary_root("delivery-catchup");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        std::fs::create_dir_all(&state_dir).unwrap();
        AtomicJsonStore::new(state_dir.join("checkpoint.json"))
            .save(&state)
            .unwrap();
        let options = super::LiveRunOptions {
            state_dir: state_dir.clone(),
            spool_dir: spool_dir.clone(),
            heartbeat: root.join("heartbeat.json"),
        };
        let mut runner =
            super::LiveRunner::new_with_universe(config.clone(), universe.clone(), options.clone())
                .unwrap();
        assert!(runner.kline_repair_jobs(212 * DAY_MS).is_empty());
        assert!(!runner.needs_cold_bootstrap());

        let mut seen = BTreeMap::<i64, (Vec<String>, Vec<String>)>::new();
        for day in 208..=210 {
            let observations = runner
                .durable
                .apply_and_commit(WireEvent::CarryScorerCatchupWatermark {
                    schema_version: SCHEMA_VERSION,
                    sequence: runner.durable.worker().next_input_sequence().unwrap(),
                    observed_ts_ms: 212 * DAY_MS,
                    decision_through_ms: day * DAY_MS,
                    gap_symbols: Vec::new(),
                })
                .unwrap();
            assert_eq!(observations.len(), 1);
            let envelope: SignalPayloadEnvelope =
                serde_json::from_slice(&observations[0].payload).unwrap();
            let ObservationPayload::CarryScorerCatchup {
                decision_ts_ms,
                rows,
                rejections,
            } = envelope.payload
            else {
                panic!("expected scorer-only CARRY catch-up");
            };
            seen.insert(
                decision_ts_ms,
                (
                    rows.into_iter().map(|row| row.symbol).collect(),
                    rejections.into_iter().map(|row| row.symbol).collect(),
                ),
            );
        }
        assert!(seen[&(208 * DAY_MS)].0.contains(&"BTCUSDT".into()));
        assert!(seen[&(209 * DAY_MS)].0.contains(&"BTCUSDT".into()));
        assert!(!seen[&(210 * DAY_MS)].0.contains(&"BTCUSDT".into()));
        assert!(seen[&(210 * DAY_MS)].1.contains(&"BTCUSDT".into()));

        drop(runner);
        let reopened = super::LiveRunner::new_with_universe(config, universe, options).unwrap();
        assert_eq!(
            reopened.durable.worker().state().last_carry_scorer_ts_ms,
            Some(210 * DAY_MS)
        );
        assert!(!reopened.needs_cold_bootstrap());
        assert!(reopened
            .kline_repair_jobs(212 * DAY_MS)
            .iter()
            .all(|(symbol, _, through)| symbol != "BTCUSDT" || *through <= 210 * DAY_MS));
        std::fs::remove_dir_all(root).unwrap();
    }

    fn checked_demo_config() -> SignalWorkerConfig {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        SignalWorkerConfig::load(
            root.join("configs/signal-worker.demo.json"),
            root.join("configs/long_native_v12.json"),
            root.join("configs/lane2_carry_hold_v7.json"),
            root.join("configs/operational.json"),
            root.join("deploy/engine.demo.toml.template"),
        )
        .unwrap()
    }

    fn test_universe() -> UniverseIdentity {
        UniverseIdentity {
            mode: UniverseMode::Pit,
            environment: "demo".into(),
            endpoint: "api-demo.bybit.com".into(),
            snapshot_ts_ms: 100 * DAY_MS,
            available_at_ms: 100 * DAY_MS + 1,
            artifact_sha256: "1".repeat(64),
            file_sha256: "2".repeat(64),
            symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
            long_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
            carry_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        }
    }

    fn ticker_wire_with_mark(mark: &str) -> BybitTickerWire {
        BybitTickerWire {
            symbol: "BTCUSDT".into(),
            mark_observed_ts_ms: None,
            funding_observed_ts_ms: None,
            schedule_observed_ts_ms: None,
            last_price: None,
            mark_price: Some(Value::from(mark)),
            index_price: None,
            bid1_price: None,
            ask1_price: None,
            bid1_size: None,
            ask1_size: None,
            open_interest: None,
            open_interest_value: None,
            turnover24h: None,
            volume24h: None,
            funding_rate: None,
            next_funding_time: None,
        }
    }

    fn kline_wire(open_ts_ms: i64, close: &str) -> Vec<Value> {
        vec![
            Value::from(open_ts_ms),
            Value::from("100"),
            Value::from("110"),
            Value::from("90"),
            Value::from(close),
            Value::from("1"),
            Value::from("100"),
        ]
    }

    fn instrument_wire(
        symbol: &str,
        status: &str,
        launch_time_ms: i64,
        delivery_time_ms: Option<i64>,
    ) -> BybitInstrumentWire {
        BybitInstrumentWire {
            symbol: symbol.into(),
            contract_type: Some("LinearPerpetual".into()),
            symbol_type: None,
            status: Some(status.into()),
            base_coin: Some(symbol.trim_end_matches("USDT").into()),
            quote_coin: Some("USDT".into()),
            settle_coin: Some("USDT".into()),
            launch_time: Some(Value::from(launch_time_ms)),
            delivery_time: delivery_time_ms.map(Value::from),
            price_filter: BTreeMap::new(),
            lot_size_filter: BTreeMap::new(),
            funding_interval: Some(Value::from(60)),
            is_pre_listing: false,
        }
    }

    /// Bybit's real shape: every perpetual carries `deliveryTime: "0"`. The
    /// instrument lane must accept the venue's whole list, or the worker runs
    /// with no instrument table at all.
    #[test]
    fn a_snapshot_of_perpetuals_with_zero_delivery_clocks_passes_source_validation() {
        let worker = crate::worker::SignalWorker::new(checked_demo_config()).unwrap();
        let observed = 100 * DAY_MS;
        let fetched = FetchedInstruments {
            observed_ts_ms: observed,
            available_at_ms: observed + 5,
            rows: vec![
                instrument_wire("BTCUSDT", "Trading", DAY_MS, Some(0)),
                instrument_wire("ETHUSDT", "Trading", DAY_MS, Some(0)),
                instrument_wire("ASPUSDT", "Trading", DAY_MS, Some(observed + DAY_MS)),
                instrument_wire("OLDUSDT", "Closed", DAY_MS, Some(observed - DAY_MS)),
            ],
        };
        validate_instrument_source_against_state(worker.state(), &fetched)
            .expect("the venue's own list is valid input");
    }

    /// The venue's own lists, when `LM_BYBIT_INSTRUMENTS_JSON` names them
    /// (colon-separated `instruments-info` responses), through the same wire
    /// parse and normalisation the lane uses. Run by hand:
    /// `LM_BYBIT_INSTRUMENTS_JSON=a.json:b.json cargo test -p signal-worker -- --ignored the_venues_real`.
    #[test]
    #[ignore = "reads the venue's instrument lists from LM_BYBIT_INSTRUMENTS_JSON"]
    fn the_venues_real_instrument_lists_normalize() {
        let paths = std::env::var("LM_BYBIT_INSTRUMENTS_JSON").expect("LM_BYBIT_INSTRUMENTS_JSON");
        let mut all = Vec::new();
        for path in paths.split(':') {
            let payload: Value =
                serde_json::from_slice(&std::fs::read(path).expect("readable file")).unwrap();
            for value in payload["result"]["list"].as_array().expect("result.list") {
                all.push(super::instrument_wire(value).expect("wire row"));
            }
        }
        let observed = 1_788_436_000_000;
        let (rows, rejected) =
            crate::normalize::normalize_instruments_reporting(observed, observed + 1, &all)
                .expect("whole list");
        eprintln!(
            "rows {} rejected {} ({:?})",
            rows.len(),
            rejected.rows.len(),
            rejected.summary("instrument")
        );
        let bad_reasons = rejected
            .rows
            .iter()
            .filter(|(_, reason)| !reason.contains("invalid symbol"))
            .collect::<Vec<_>>();
        assert!(
            bad_reasons.is_empty(),
            "rows refused for a reason other than a dated name: {bad_reasons:?}"
        );
        assert!(rows.len() > 800, "{}", rows.len());

        if let Ok(path) = std::env::var("LM_BYBIT_TICKERS_JSON") {
            let payload: Value =
                serde_json::from_slice(&std::fs::read(path).expect("readable file")).unwrap();
            let rows = payload["result"]["list"]
                .as_array()
                .expect("result.list")
                .iter()
                .map(|value| crate::bybit_ws::ticker_wire(value).expect("ticker wire row"))
                .collect::<Vec<_>>();
            let (kept, rejected) =
                crate::normalize::normalize_tickers_reporting(observed, observed + 1, &rows)
                    .expect("whole ticker page");
            eprintln!(
                "tickers {} rejected {} ({:?})",
                kept.len(),
                rejected.rows.len(),
                rejected.summary("ticker")
            );
            let bad_reasons = rejected
                .rows
                .iter()
                .filter(|(_, reason)| !reason.contains("invalid symbol"))
                .collect::<Vec<_>>();
            assert!(bad_reasons.is_empty(), "{bad_reasons:?}");
            assert!(kept.len() > 800, "{}", kept.len());
        }
    }

    fn temporary_root(label: &str) -> PathBuf {
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        std::env::temp_dir().join(format!(
            "signal-worker-live-{label}-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed),
        ))
    }

    #[test]
    fn the_gate_file_is_read_whole_and_an_absent_one_is_nothing() {
        let root = temporary_root("gate-file");
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("llm-gate-candidates.json");
        assert!(super::read_gate_candidates(&path).unwrap().is_none());
        std::fs::write(
            &path,
            serde_json::json!({
                "decision_ts_ms": 1_787_000_000_000_i64,
                "valid_until_ms": 1_787_003_600_000_i64,
                "events": [{
                    "symbol": "aaausdt",
                    "score": 7,
                    "band": "wide",
                    "trigger_ts_ms": 1_786_999_400_000_i64,
                    "trigger_price": 10.0,
                    "atr_pct": 0.05,
                    "sigma_daily_30d": 0.03,
                    "turnover_rank": 14,
                    "trigger_window_h": 4,
                    "a_future_field": "is ignored"
                }]
            })
            .to_string(),
        )
        .unwrap();
        let fetched = super::read_gate_candidates(&path).unwrap().unwrap();
        assert_eq!(fetched.decision_ts_ms, 1_787_000_000_000);
        assert_eq!(fetched.valid_until_ms, 1_787_003_600_000);
        assert!(fetched.read_at_ms > fetched.decision_ts_ms);
        assert_eq!(fetched.rows.len(), 1);
        let row = &fetched.rows[0];
        assert_eq!(row.symbol, "AAAUSDT");
        assert_eq!(row.score, 7.0);
        assert_eq!(row.band, "wide");
        assert_eq!(row.trigger_ts_ms, 1_786_999_400_000);
        assert_eq!(row.sigma_daily_30d, Some(0.03));
        assert_eq!(row.turnover_rank, Some(14.0));
        assert_eq!(row.trigger_window_h, Some(4));
        std::fs::write(&path, b"{not json").unwrap();
        let error = super::read_gate_candidates(&path).unwrap_err();
        assert!(error.is_lane_local_source_failure());
        std::fs::remove_dir_all(root).unwrap();
    }
}
