use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot, Semaphore};
use tokio::time::MissedTickBehavior;

use crate::config::SignalWorkerConfig;
use crate::features::carry_decision_at;
use crate::http::{wall_ms, PublicHttpClient};
use crate::model::{
    BinanceWhaleWire, BootstrapCoverage, BybitFundingWire, InstrumentTradingInterval,
    SourceCoverage, WireEvent,
};
use crate::normalize::{
    normalize_funding_rows, normalize_instruments, normalize_kline_rows, normalize_whales,
};
use crate::store::atomic_write;
use crate::venue::{
    open_public_venue, source_grid_slots, validate_fetched_tickers, validate_source_grid_timestamp,
    validate_source_page_rows, wire_i64, ConfirmedKline, FetchedInstruments, FetchedTickers,
    PublicStream, PublicVenue, StreamContinuity, StreamEvent, StreamHealth, TickerSample,
};
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
// Every epoch, the boot one included, opens a repair gap, and the first repair
// refills an hour of klines for the whole universe rather than one reconnect's
// window. A chosen bound above the observed ~190 s boot pass, not a measured
// limit, and far short of STARTUP_MAX_MS so a wedged boot repair still pages.
const BOOT_REPAIR_MAX_MS: i64 = 10 * 60_000;
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
    /// Wall time before which a carry cycle stopped at the standing decision
    /// boundary is not overdue: the boundary's own funding print is not
    /// publishable, and its decision is not due, until then. Readers measure
    /// carry staleness from the later of this and the last completion.
    pub carry_cycle_not_before_wall_ts_ms: Option<i64>,
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
    /// The frame-age limit this worker applies to `bybit_ws_last_frame_ts_ms`
    /// when it decides transport health, so a reader can name the clause that
    /// failed instead of guessing the limit.
    pub bybit_ws_max_frame_age_ms: i64,
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
    shutdown: Option<ShutdownSignal>,
    config: SignalWorkerConfig,
    durable: DurableSignalWorker,
    /// The realm's public data source, named by `sources.public_venue`.
    venue: Arc<dyn PublicVenue>,
    binance: PublicHttpClient,
    /// Present exactly when `universe.listed_on` names a venue.
    listing_source: Option<ListingSource>,
    /// The last listing that arrived. It stands through a failed fetch, and
    /// until the first one arrives the universe cannot be derived at all.
    last_listing: Option<BTreeSet<String>>,
    listing_missing_reported: bool,
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
    // An instrument refresh the hourly cadence asked for that a busy lane held
    // off. The cadence is `instrument_cadence_ms` (1 h) while the funding lane
    // that blocks it runs every `funding_cadence_ms` (60 s), so a dropped tick
    // is the table standing still for an hour or longer.
    instruments_due: bool,
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

/// The venue's whole instrument list from the realm host plus the whole
/// ticker page from the public host: everything the universe is derived from.
struct FetchedUniverseInputs {
    instruments: FetchedInstruments,
    tickers: FetchedTickers,
    /// `None` when no rule names a listing venue; `Err` when that venue's
    /// listing fetch failed and the last good listing has to stand.
    listing: Option<Result<BTreeSet<String>, WorkerError>>,
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

fn open_listing_source(
    config: &SignalWorkerConfig,
    request_budget: Arc<Semaphore>,
) -> Result<Option<ListingSource>, WorkerError> {
    config
        .universe
        .listed_on
        .as_deref()
        .map(|venue| {
            ListingSource::new(
                ListingVenue::parse(venue)?,
                config.live.request_timeout_ms,
                config.live.request_retries,
                config.live.retry_base_ms,
                request_budget,
            )
        })
        .transpose()
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
                // The interval is stamped from the current instrument, not read
                // back from the venue, so it is not part of the settled
                // identity. See `SettledFunding`'s `HistoryRow::same_value`.
                if existing.symbol != row.symbol
                    || existing.settlement_ts_ms != row.settlement_ts_ms
                    || existing.rate != row.rate
                {
                    return Err(WorkerError::input(format!(
                        "funding history rewrote {} at timestamp {}",
                        row.symbol, row.settlement_ts_ms
                    )));
                }
            }
            let key = (row.symbol.clone(), row.settlement_ts_ms);
            if let Some(existing) = seen.insert(key, row.clone()) {
                if existing.rate != row.rate {
                    return Err(WorkerError::input(format!(
                        "funding fetch rewrote {} at timestamp {}",
                        row.symbol, row.settlement_ts_ms
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
        let mut shutdown = ShutdownSignal::install()?;
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
        let durable = loop {
            tokio::select! {
                result = &mut recovery_rx => {
                    break result
                        .map_err(|_| WorkerError::state("signal-worker recovery task stopped"))??;
                }
                _ = heartbeat_tick.tick() => {
                    write_provisional_heartbeat(&config, None, &options.heartbeat, "starting")?;
                }
                signal = shutdown.recv() => {
                    signal?;
                    write_provisional_heartbeat(&config, None, &options.heartbeat, "stopped")?;
                    return Ok(None);
                }
            }
        };
        let request_budget = Arc::new(Semaphore::new(config.live.max_parallel_requests));
        let venue =
            open_public_venue(config.public_venue()?, &config, Arc::clone(&request_budget))?;
        venue.seed_listing_history(listing_history_seed(durable.worker().state()));
        let listing_source = open_listing_source(&config, Arc::clone(&request_budget))?;
        let binance = PublicHttpClient::new(
            &config.sources.binance_host,
            config.live.request_timeout_ms,
            config.live.request_retries,
            config.live.retry_base_ms,
            request_budget,
        )?;
        Ok(Some(Self {
            shutdown: Some(shutdown),
            config,
            durable,
            venue,
            binance,
            listing_source,
            last_listing: None,
            listing_missing_reported: false,
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
        let request_budget = Arc::new(Semaphore::new(config.live.max_parallel_requests));
        let venue =
            open_public_venue(config.public_venue()?, &config, Arc::clone(&request_budget))?;
        let listing_source = open_listing_source(&config, Arc::clone(&request_budget))?;
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
        venue.seed_listing_history(listing_history_seed(durable.worker().state()));
        Ok(Self {
            shutdown: None,
            config,
            durable,
            venue,
            binance,
            listing_source,
            last_listing: None,
            listing_missing_reported: false,
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

    /// The venue's own tables (MEXC's contract table, Hyperliquid's coin
    /// spellings) live in this process and are filled by an instrument read.
    /// A restored universe skips `resolve_universe`'s read, and the stream and
    /// the first kline repair need those tables, so read once before either.
    /// A venue fault here is retried with backoff rather than left to systemd.
    async fn read_instruments_before_the_stream(&mut self) -> Result<(), WorkerError> {
        let mut delay_ms = self.config.live.retry_base_ms.max(500);
        loop {
            match self.refresh_instruments().await {
                Ok(()) => return Ok(()),
                Err(error) if error.is_lane_local_source_failure() => {
                    eprintln!("signal-worker: instrument read before the stream failed, retrying: {error}");
                    self.write_heartbeat("starting", None)?;
                    tokio::time::sleep(Duration::from_millis(delay_ms)).await;
                    delay_ms = delay_ms.saturating_mul(2).min(60_000);
                }
                Err(error) => return Err(error),
            }
        }
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

    async fn resolve_named_destinations(&mut self) -> Result<(), WorkerError> {
        self.durable.require_named_destinations();
        let mut last_error = None;
        loop {
            match self.durable.respond_to_readiness_request() {
                Ok(()) => last_error = None,
                Err(error) => {
                    let text = error.to_string();
                    if last_error.as_ref() != Some(&text) {
                        eprintln!("signal-worker: waiting for engine destination registry: {text}");
                    }
                    last_error = Some(text);
                }
            }
            if self.durable.destinations_verified()
                && !self
                    .durable
                    .worker()
                    .state()
                    .signal_lifecycle
                    .as_ref()
                    .is_some_and(|state| state.sealed)
            {
                return Ok(());
            }
            self.write_heartbeat("starting", None)?;
            tokio::time::sleep(Duration::from_millis(
                self.config.live.retry_base_ms.clamp(500, 1_000),
            ))
            .await;
        }
    }

    pub async fn run(mut self) -> Result<(), WorkerError> {
        let mut shutdown = match self.shutdown.take() {
            Some(shutdown) => shutdown,
            None => ShutdownSignal::install()?,
        };
        self.write_heartbeat("starting", None)?;
        tokio::select! {
            biased;
            signal = shutdown.recv() => {
                signal?;
                self.write_heartbeat("stopped", None)?;
                return Ok(());
            }
            result = async {
                self.resolve_named_destinations().await?;
                self.resolve_universe().await
            } => result?,
        }
        self.durable.respond_to_readiness_request()?;
        self.read_instruments_before_the_stream().await?;
        let run_started_at_ms = wall_ms()?;
        let symbols = self.kline_symbols();
        let pending_limit = pending_kline_limit(symbols.len());
        let mut stream = self.venue.open_stream(self.stream_symbols())?;
        let (lane_tx, mut lane_rx) = mpsc::channel(LANE_COMPLETION_QUEUE_CAPACITY);
        let mut lanes = LaneState {
            instruments_ready: !self.durable.worker().state().instruments.is_empty(),
            ..LaneState::default()
        };
        let mut pending_klines = BTreeMap::<(String, i64), ConfirmedKline>::new();
        let mut recovery = RecoveryState::default();

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
        lanes.instruments = true;
        spawn_instrument_lane(
            lane_tx.clone(),
            Arc::clone(&self.venue),
            self.listing_source.clone(),
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
                        LaneContext {
                            stream: &mut stream,
                            pending: &mut pending_klines,
                            lane_tx: &lane_tx,
                            lanes: &mut lanes,
                        },
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
                    lanes.instruments_due = true;
                    self.start_instrument_lane_if_due(&lane_tx, &mut lanes);
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
                    self.durable.respond_to_readiness_request()?;
                    let health = stream.health();
                    let now_ms = wall_ms()?;
                    let status = heartbeat_status(
                        &health,
                        lanes.repair,
                        [
                            CycleFreshness::on_cadence(
                                self.last_long_cycle_completed_wall_ts_ms,
                                self.config.live.kline_cadence_ms,
                            ),
                            CycleFreshness::due_after(
                                self.last_carry_cycle_completed_wall_ts_ms,
                                self.config.live.kline_cadence_ms,
                                self.carry_cycle_not_before(now_ms),
                            ),
                        ],
                        run_started_at_ms,
                        now_ms,
                        self.config.sources.mark_max_age_ms,
                        &mut recovery,
                    );
                    self.write_heartbeat(status, Some(health))?;
                }
                signal = shutdown.recv() => {
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
        stream: &mut Box<dyn PublicStream>,
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
        stream: &mut Box<dyn PublicStream>,
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
        stream: &mut Box<dyn PublicStream>,
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

    fn flush_pending_klines_or_recover(
        &mut self,
        stream: &mut Box<dyn PublicStream>,
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
                            || row.available_at_ms < old.available_at_ms
                    }
                }
            });
            let state = self.durable.worker().state();
            let replace_coverage = false;
            let coverage_advances = match (batch.checked_from_ms, batch.checked_through_ms) {
                (Some(from), Some(through)) => {
                    !state.funding_coverage().contains(&symbol, from, through)
                }
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
            !state.whale_coverage().contains(
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
        stream: &mut Box<dyn PublicStream>,
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
            Arc::clone(&self.venue),
            self.config.live.kline_page_limit,
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
                        let start = state.kline_coverage().repair_start(
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

    /// Start the instrument lane when the cadence has asked for it and the
    /// lanes it shares a venue with are idle. The request survives a busy lane:
    /// dropping it instead leaves the instrument table and the traded universe
    /// frozen until the next hourly tick, and the funding lane that blocks it
    /// runs every 60 s, so a dropped tick repeats.
    fn start_instrument_lane_if_due(
        &self,
        lane_tx: &mpsc::Sender<LaneCompletion>,
        lanes: &mut LaneState,
    ) {
        if !lanes.instruments_due {
            return;
        }
        if lanes.instruments {
            // A refresh already in flight is the refresh this tick asked for.
            lanes.instruments_due = false;
            return;
        }
        if lanes.funding {
            return;
        }
        lanes.instruments_due = false;
        lanes.instruments = true;
        spawn_instrument_lane(
            lane_tx.clone(),
            Arc::clone(&self.venue),
            self.listing_source.clone(),
            self.config.live.instrument_max_pages,
        );
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
            .is_some_and(|last| last >= self.latest_carry_decision(now_ms));
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
            if current_trading_instrument(state, symbol, self.venue.settle_coin()) {
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
                if state
                    .funding_coverage()
                    .contains(symbol, required_start, end_ms)
                {
                    continue;
                }
                let start = state
                    .funding_coverage()
                    .repair_start(symbol, required_start, end_ms)
                    .min(end_ms.saturating_sub(interval_ms))
                    .max(required_start);
                if start < end_ms {
                    jobs.push((symbol.clone(), start, end_ms, emit_lifecycle));
                }
            }
        }
        let page_limit = self.config.live.funding_page_limit;
        spawn_funding_fetch_lane(
            lane_tx,
            Arc::clone(&self.venue),
            page_limit,
            jobs,
            intervals,
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
            if current_trading_instrument(state, symbol, self.venue.settle_coin()) {
                ranges.extend(instrument_source_ranges(
                    state,
                    symbol,
                    current_end_ms.saturating_sub(history_ms),
                    current_end_ms,
                    DAY_MS,
                ));
            }
            for (required_start, end_ms) in ranges {
                if state
                    .whale_coverage()
                    .contains(symbol, required_start, end_ms)
                {
                    continue;
                }
                let append_from =
                    state
                        .whale_coverage()
                        .repair_start(symbol, required_start, end_ms);
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
        spawn_whale_fetch_lane(lane_tx, client, page_limit, jobs);
        Ok(())
    }

    fn spawn_ticker_lane(&self, lane_tx: mpsc::Sender<LaneCompletion>) -> Result<(), WorkerError> {
        let venue = Arc::clone(&self.venue);
        let allowed = self.kline_symbols().into_iter().collect();
        tokio::spawn(async move {
            let result = venue.ticker_snapshot(allowed).await;
            let _ = lane_tx.send(LaneCompletion::Tickers(result)).await;
        });
        Ok(())
    }

    async fn refresh_instruments(&mut self) -> Result<(), WorkerError> {
        let fetched = fetch_universe_inputs(
            self.venue.as_ref(),
            self.listing_source.clone(),
            self.config.live.instrument_max_pages,
        )
        .await?;
        self.commit_universe_inputs(fetched)?;
        self.validate_candidate_instruments()
    }

    /// Install a fresh listing, or keep the last good one when the fetch
    /// failed. Said once each way: a lane that logged every cadence would say
    /// the same thing every hour for as long as the venue is down.
    fn absorb_listing(&mut self, listing: Option<Result<BTreeSet<String>, WorkerError>>) {
        match listing {
            Some(Ok(listed)) => {
                if self.listing_missing_reported {
                    eprintln!(
                        "signal-worker: venue listing back with {} symbols",
                        listed.len()
                    );
                    self.listing_missing_reported = false;
                }
                self.last_listing = Some(listed);
            }
            Some(Err(error)) => {
                if !self.listing_missing_reported {
                    eprintln!(
                        "signal-worker: venue listing fetch failed, keeping the last one: {error}"
                    );
                    self.listing_missing_reported = true;
                }
            }
            None => {}
        }
    }

    /// Derive the universe from a fresh venue page pair, install it when its
    /// membership moved, then record the instrument snapshot the owned symbols
    /// are read from. The universe goes first so a symbol that just entered has
    /// instrument facts in the same commit.
    fn commit_universe_inputs(
        &mut self,
        fetched: FetchedUniverseInputs,
    ) -> Result<(), WorkerError> {
        self.absorb_listing(fetched.listing);
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
                endpoint: self.config.universe_endpoint(),
                settle_coin: self.venue.settle_coin(),
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
                listing: self.last_listing.as_ref(),
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
        let allowed: BTreeSet<String> = self.kline_symbols().into_iter().collect();
        let fetched = self.venue.ticker_snapshot(allowed).await?;
        self.commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: self.next_sequence()?,
            observed_ts_ms: fetched.observed_ts_ms,
            available_at_ms: fetched.available_at_ms,
            rows: fetched.rows,
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
        for job in jobs {
            let fetched = self.fetch_kline_job(job).await?;
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

    async fn fetch_kline_job(&self, job: KlineJob) -> Result<FetchedKlineJobs, WorkerError> {
        fetch_kline_job(self.venue.as_ref(), self.config.live.kline_page_limit, job).await
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
        for job in jobs {
            let fetched = fetch_funding_job(
                self.venue.as_ref(),
                self.config.live.funding_page_limit,
                job,
                &instruments,
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
        for job in jobs {
            let fetched =
                fetch_whale_job(self.binance.clone(), self.config.live.whale_page_limit, job)
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
        let latest_decision_ms = self.latest_carry_decision(now_ms);
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
                    current_trading_instrument(state, symbol, self.venue.settle_coin())
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
                    state.kline_coverage().contains(symbol, *start, *through)
                        && first_missing_kline_hour(state, symbol, *start, *through).is_none()
                }) || !funding_ranges.iter().all(|(start, through)| {
                    state.funding_coverage().contains(symbol, *start, *through)
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
            .or_else(|| self.durable.worker().state().last_carry_decision_ts_ms)
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
            .or_else(|| self.durable.worker().state().last_carry_decision_ts_ms)
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
            if !current_trading_instrument(state, symbol, self.venue.settle_coin()) {
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
                    !state.kline_coverage().contains(symbol, *start, *through)
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
                    current_trading_instrument(state, symbol, self.venue.settle_coin())
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
                            !state.kline_coverage().contains(symbol, *start, *through)
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
                            && row.settle_coin.as_deref() == Some(self.venue.settle_coin())
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
                    && row.settle_coin.as_deref() == Some(self.venue.settle_coin())
                    && !row.is_prelisting
            }) {
                critical.insert(symbol);
            }
        }
        critical.into_iter().collect()
    }

    /// The symbol set a universe refresh moved to, with the transport history
    /// the replacement stream must carry over. `None` when the set is unchanged.
    fn stream_reconfiguration(
        &self,
        stream: &dyn PublicStream,
    ) -> Option<(Vec<String>, StreamContinuity)> {
        let desired = self.stream_symbols().into_iter().collect::<BTreeSet<_>>();
        if &desired == stream.symbols() {
            return None;
        }
        Some((
            desired.into_iter().collect(),
            StreamContinuity::from(&stream.health()),
        ))
    }

    fn reconfigure_stream(&self, stream: &mut Box<dyn PublicStream>) -> Result<(), WorkerError> {
        let Some((desired, continuity)) = self.stream_reconfiguration(&**stream) else {
            return Ok(());
        };
        *stream = self.venue.open_stream_continuing(desired, continuity)?;
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
        if long_support && current_trading_instrument(state, symbol, self.venue.settle_coin()) {
            ranges.extend(instrument_source_ranges(
                state,
                symbol,
                self.required_long_start(end_ms),
                end_ms,
                HOUR_MS,
            ));
        }
        if carry_support {
            let latest_carry_decision_ms =
                carry_decision_at(end_ms, self.config.carry.decision_phase_ms, 0).unwrap_or(0);
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
                if current_trading_instrument(state, symbol, self.venue.settle_coin()) {
                    ranges.extend(instrument_source_ranges(
                        state,
                        symbol,
                        self.required_carry_start(latest_carry_decision_ms),
                        latest_carry_decision_ms,
                        HOUR_MS,
                    ));
                }
            } else if current_trading_instrument(state, symbol, self.venue.settle_coin()) {
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
        let latest_decision_ms =
            carry_decision_at(current_end_ms, self.config.carry.decision_phase_ms, 0).unwrap_or(0);
        self.durable
            .worker()
            .state()
            .last_carry_scorer_ts_ms
            .or_else(|| self.durable.worker().state().last_carry_decision_ts_ms)
            .map(|last| {
                last.saturating_add(CARRY_CATCHUP_CHUNK_DAYS.saturating_mul(DAY_MS))
                    .min(latest_decision_ms)
                    .max(0)
            })
            .unwrap_or(current_end_ms)
    }

    fn carry_cycle_not_before(&self, now_ms: i64) -> Option<i64> {
        carry_cycle_not_before(
            now_ms,
            self.config.carry.decision_phase_ms,
            self.config.carry.decision_kline_lag_ms,
            self.config.live.funding_cadence_ms,
        )
    }

    fn latest_carry_decision(&self, observed_ts_ms: i64) -> i64 {
        carry_decision_at(
            observed_ts_ms,
            self.config.carry.decision_phase_ms,
            self.config.carry.decision_kline_lag_ms,
        )
        .unwrap_or(0)
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
                    !state
                        .kline_coverage()
                        .contains(symbol, required_start, required_through)
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
            .any(|(start, through)| !state.funding_coverage().contains(symbol, start, through))
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
            if row.settle_coin.as_deref() != Some(self.venue.settle_coin())
                || row.contract_type.as_deref() != Some("LinearPerpetual")
            {
                let settle_coin = self.venue.settle_coin().to_ascii_lowercase();
                invalid.push(format!("{symbol}:not_{settle_coin}_linear"));
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
            carry_cycle_not_before_wall_ts_ms: self.carry_cycle_not_before(wall_ms()?),
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
            bybit_ws_max_frame_age_ms: self.config.sources.mark_max_age_ms,
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
        carry_cycle_not_before_wall_ts_ms: carry_cycle_not_before(
            wall_ms()?,
            config.carry.decision_phase_ms,
            config.carry.decision_kline_lag_ms,
            config.live.funding_cadence_ms,
        ),
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
        bybit_ws_max_frame_age_ms: config.sources.mark_max_age_ms,
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

/// The cold-start repair on a sound transport: a gap or repair on a process
/// that has never reported `ready`, with coverage already full. It outlives the
/// cycle warmup the `starting` grace keys on, so it needs its own bound.
/// The launch times a restored checkpoint already knows, for the venue.
fn listing_history_seed(state: &crate::worker::WorkerState) -> BTreeMap<String, i64> {
    state
        .instruments
        .iter()
        .filter_map(|(symbol, row)| row.launch_time_ms.map(|ms| (symbol.clone(), ms)))
        .collect()
}

fn boot_repair_acceptable(
    health: &StreamHealth,
    repair_running: bool,
    transport_healthy: bool,
    reached_ready: bool,
    started_at_ms: i64,
    now_ms: i64,
) -> bool {
    !reached_ready
        && transport_healthy
        && health.ticker_coverage_complete
        && (health.gap_open || repair_running)
        && now_ms.saturating_sub(started_at_ms) < BOOT_REPAIR_MAX_MS
}

/// What one process carries between heartbeats: when the current recovery
/// started, and whether it has ever been `ready`.
#[derive(Debug, Default)]
struct RecoveryState {
    transient_started_at_ms: Option<i64>,
    reached_ready: bool,
}

/// When the standing decision boundary's own carry cycle first becomes overdue.
/// Two waits the worker guarantees itself, whichever is later: the boundary's
/// funding settlement is not publishable until `FUNDING_PUBLICATION_LAG_MS`
/// after it and `spawn_funding_lane` will not ask before then, and the day's
/// decision is not due until `decision_kline_lag_ms` after it, so nothing is
/// lost while the lane works the roll. Before this instant a carry cycle
/// stopped at the boundary is arithmetic, not a fault.
fn carry_cycle_not_before(
    now_ms: i64,
    decision_phase_ms: i64,
    decision_kline_lag_ms: i64,
    funding_cadence_ms: u64,
) -> Option<i64> {
    let funding_supply_ms =
        FUNDING_PUBLICATION_LAG_MS.saturating_add(i64::try_from(funding_cadence_ms).unwrap_or(0));
    carry_decision_at(now_ms, decision_phase_ms, 0)
        .map(|boundary_ms| boundary_ms.saturating_add(decision_kline_lag_ms.max(funding_supply_ms)))
}

/// One lane's freshness contract: when it last completed, how often it should,
/// and the wall time before which it cannot have completed for the boundary it
/// is working on. `not_before_ms` is `None` for a lane whose completion waits
/// on nothing but its own cadence.
#[derive(Clone, Copy, Debug)]
struct CycleFreshness {
    completed_at_ms: Option<i64>,
    cadence_ms: u64,
    not_before_ms: Option<i64>,
}

impl CycleFreshness {
    fn on_cadence(completed_at_ms: Option<i64>, cadence_ms: u64) -> Self {
        Self {
            completed_at_ms,
            cadence_ms,
            not_before_ms: None,
        }
    }

    fn due_after(
        completed_at_ms: Option<i64>,
        cadence_ms: u64,
        not_before_ms: Option<i64>,
    ) -> Self {
        Self {
            completed_at_ms,
            cadence_ms,
            not_before_ms,
        }
    }

    /// Three cadences cover one tick plus one pass. The window runs from the
    /// later of the last completion and the moment the lane became due, so a
    /// lane waiting for a boundary's inputs to exist is not a stall.
    fn stale_at(&self, now_ms: i64) -> bool {
        let window_ms = i64::try_from(self.cadence_ms)
            .unwrap_or(i64::MAX / 3)
            .saturating_mul(3);
        let Some(completed_at_ms) = self.completed_at_ms else {
            return true;
        };
        if completed_at_ms > now_ms {
            return true;
        }
        let due_from_ms = self.not_before_ms.map_or(completed_at_ms, |not_before_ms| {
            completed_at_ms.max(not_before_ms)
        });
        now_ms.saturating_sub(due_from_ms) > window_ms
    }
}

/// One producer verdict: `starting` for bounded cold fill, `recovering` for a
/// short repair on an otherwise sound transport, and `degraded` for a fault.
fn heartbeat_status(
    health: &StreamHealth,
    repair_running: bool,
    cycles: [CycleFreshness; 2],
    started_at_ms: i64,
    now_ms: i64,
    max_frame_age_ms: i64,
    recovery: &mut RecoveryState,
) -> &'static str {
    let transport_healthy = stream_transport_healthy(health, now_ms, max_frame_age_ms);
    let boot_repair = boot_repair_acceptable(
        health,
        repair_running,
        transport_healthy,
        recovery.reached_ready,
        started_at_ms,
        now_ms,
    );
    // The boot repair is bounded by BOOT_REPAIR_MAX_MS, so it must not also
    // spend the transient window: a boot gap holds the window open from the
    // first heartbeat, and the first coverage dip after two minutes of it would
    // otherwise get no grace at all. Held only while the boot repair itself is
    // the verdict, so a boot repair that will not close still pages at its own
    // bound.
    if boot_repair {
        recovery.transient_started_at_ms = None;
    }
    let recovery_acceptable = transient_recovery_acceptable(
        health,
        repair_running,
        transport_healthy,
        &mut recovery.transient_started_at_ms,
        now_ms,
    );
    let live_status = match runtime_status(health, repair_running, now_ms, max_frame_age_ms) {
        "ready" => {
            recovery.reached_ready = true;
            "ready"
        }
        _ if transport_healthy && (recovery_acceptable || boot_repair) => "recovering",
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
    cycles: [CycleFreshness; 2],
    startup_inputs_healthy: bool,
    started_at_ms: i64,
    now_ms: i64,
) -> &'static str {
    if cycles.iter().any(|cycle| cycle.completed_at_ms.is_none())
        && startup_inputs_healthy
        && now_ms.saturating_sub(started_at_ms) < STARTUP_MAX_MS
    {
        "starting"
    } else if cycles.iter().any(|cycle| cycle.stale_at(now_ms)) {
        "degraded"
    } else {
        base_status
    }
}

fn carry_required_lanes_pending(lanes: &LaneState) -> bool {
    lanes.instruments || !lanes.instruments_ready || lanes.funding || !lanes.funding_ready
}

struct ShutdownSignal {
    #[cfg(unix)]
    terminate: tokio::signal::unix::Signal,
    #[cfg(unix)]
    interrupt: tokio::signal::unix::Signal,
}

impl ShutdownSignal {
    fn install() -> Result<Self, WorkerError> {
        Ok(Self {
            #[cfg(unix)]
            terminate: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .map_err(|error| WorkerError::io("install SIGTERM handler", error))?,
            #[cfg(unix)]
            interrupt: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
                .map_err(|error| WorkerError::io("install SIGINT handler", error))?,
        })
    }

    async fn recv(&mut self) -> Result<(), WorkerError> {
        #[cfg(unix)]
        {
            tokio::select! {
                _ = self.interrupt.recv() => Ok(()),
                _ = self.terminate.recv() => Ok(()),
            }
        }
        #[cfg(not(unix))]
        tokio::signal::ctrl_c()
            .await
            .map_err(|error| WorkerError::io("wait for shutdown", error))
    }
}

pub fn heartbeat_path_parent(path: &Path) -> Result<&Path, WorkerError> {
    path.parent()
        .ok_or_else(|| WorkerError::config("heartbeat path has no parent"))
}

#[cfg(test)]
mod tests;

mod lanes;
use lanes::LaneContext;
mod acquisition;
use acquisition::*;
