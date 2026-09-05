use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::Path;

use engine_types::{
    Feed, StrategyId, Subscription, MAX_SIGNAL_OBSERVATION_BYTES, MAX_SIGNAL_SUBSCRIPTIONS,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::config::{carry_source_history_hours, sha256_hex, ConfigIdentity, SignalWorkerConfig};
use crate::features::{
    build_carry_features, build_carry_features_at, build_carry_replay_features,
    build_long_features, FundingHistory, KlineHistory, WhaleHistory,
};
use crate::history::{merge_row, CoverageMut, CoverageRef};
use crate::model::{
    BootstrapCoverage, CoverageInterval, DataRejection, InstrumentObservation,
    InstrumentTradingInterval, MarketMark, NormalizedObservation, ObservationPayload,
    PresettlementPublicObservation, Readiness, SettledFunding, SignalPayloadEnvelope,
    TickerObservation, UniverseIdentity, WireEvent,
};
use crate::normalize::{
    normalize_funding_rows, normalize_instruments, normalize_kline_rows, normalize_tickers,
    normalize_whales, normalized_symbol, validate_universe,
};
use crate::store::{
    cleanup_atomic_temporary_files, json_size, spool_class, AppendJournal, AtomicJsonStore,
    SpoolClassInventory, SpoolWriter,
};
use crate::universe::{same_membership, universe_is_resolved, unresolved_universe};
use crate::{DAY_MS, HOUR_MS, SCHEMA_VERSION};

mod lifecycle;
pub use lifecycle::WorkerSignalLifecycle;

pub(crate) fn required_carry_history_hours(
    config: &SignalWorkerConfig,
    state: &WorkerState,
) -> i64 {
    carry_source_history_hours(&config.carry, state.last_carry_decision_ts_ms.is_none())
        .unwrap_or(i64::MAX)
}

fn validate_gap_symbols(
    symbols: &[String],
    universe: &[String],
    sleeve: &str,
) -> Result<(), WorkerError> {
    let allowed: BTreeSet<&str> = universe.iter().map(String::as_str).collect();
    let mut unique = BTreeSet::new();
    for symbol in symbols {
        if !allowed.contains(symbol.as_str()) || !unique.insert(symbol.as_str()) {
            return Err(WorkerError::input(format!(
                "{sleeve} source-gap symbols are not an exclusive universe subset"
            )));
        }
    }
    Ok(())
}

const SOURCE_GENERATION_BYTES: usize = 16;
const REPLAY_SOURCE_GENERATION: &str = "00000000000000000000000000000000";
const SIGNAL_SOURCE_BYTES_MAX: usize = 256;
const MAX_INPUT_JOURNAL_ENTRIES: u64 = 1_024;
const MAX_INPUT_JOURNAL_BYTES: u64 = 256 * 1024 * 1024;
const MAX_INPUT_JOURNAL_ENTRY_BYTES: usize = 64 * 1024 * 1024;
const MAX_CHECKPOINT_AGE_SECS: u64 = 3_600;
const MAX_INPUT_BATCH_EVENTS: usize = 16;
const MAX_INPUT_BATCH_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CARRY_SCORER_CATCHUP_DAYS: i64 = 7;
const MAX_SPOOL_FILES: u64 = 4_096;
const MAX_SPOOL_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_SPOOL_OBSERVATION_FILE_BYTES: u64 = 80 * 1024 * 1024;
const MAX_SPOOL_EVENT_FILES: u64 = MAX_CARRY_SCORER_CATCHUP_DAYS as u64;
const MAX_SPOOL_EVENT_BYTES: u64 = MAX_SPOOL_EVENT_FILES * MAX_SPOOL_OBSERVATION_FILE_BYTES;
const SPOOL_BYTE_SOFT_THRESHOLD: u64 = MAX_SPOOL_BYTES - MAX_SPOOL_EVENT_BYTES;
const CURRENT_SPOOL_FILE_CAP: u64 = 8;
const CURRENT_SPOOL_BYTE_CAP: u64 = 512 * 1024 * 1024;
const CURRENT_SPOOL_BYTE_SOFT_THRESHOLD: u64 =
    CURRENT_SPOOL_BYTE_CAP - 2 * MAX_SPOOL_OBSERVATION_FILE_BYTES;
const LIFECYCLE_SPOOL_FILE_CAP: u64 = 2_048;
const LIFECYCLE_SPOOL_BYTE_CAP: u64 = 512 * 1024 * 1024;
const LIFECYCLE_SPOOL_BYTE_SOFT_THRESHOLD: u64 =
    LIFECYCLE_SPOOL_BYTE_CAP - MAX_SPOOL_OBSERVATION_FILE_BYTES;
const CATCHUP_SPOOL_FILE_CAP: u64 = 1_024;
const CATCHUP_SPOOL_BYTE_CAP: u64 = 1024 * 1024 * 1024;
const CATCHUP_SPOOL_BYTE_SOFT_THRESHOLD: u64 = CATCHUP_SPOOL_BYTE_CAP - MAX_SPOOL_EVENT_BYTES;
const OTHER_SPOOL_FILE_CAP: u64 = 1_024;
const OTHER_SPOOL_BYTE_CAP: u64 = 768 * 1024 * 1024;
const OTHER_SPOOL_BYTE_SOFT_THRESHOLD: u64 =
    OTHER_SPOOL_BYTE_CAP - MAX_SPOOL_OBSERVATION_FILE_BYTES;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WorkerErrorCategory {
    Config,
    Input,
    State,
    Network,
    Io,
    Json,
}

impl WorkerErrorCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Config => "config",
            Self::Input => "input",
            Self::State => "state",
            Self::Network => "network",
            Self::Io => "io",
            Self::Json => "json",
        }
    }
}

#[derive(Debug)]
pub struct WorkerError {
    category: WorkerErrorCategory,
    message: String,
}

impl WorkerError {
    pub fn config(message: impl Into<String>) -> Self {
        Self::new(WorkerErrorCategory::Config, message)
    }

    pub fn input(message: impl Into<String>) -> Self {
        Self::new(WorkerErrorCategory::Input, message)
    }

    pub fn state(message: impl Into<String>) -> Self {
        Self::new(WorkerErrorCategory::State, message)
    }

    pub fn network(message: impl Into<String>) -> Self {
        Self::new(WorkerErrorCategory::Network, message)
    }

    pub fn io(context: &'static str, error: std::io::Error) -> Self {
        Self::new(WorkerErrorCategory::Io, format!("{context}: {error}"))
    }

    pub fn json(context: &'static str, error: serde_json::Error) -> Self {
        Self::new(WorkerErrorCategory::Json, format!("{context}: {error}"))
    }

    pub fn category(&self) -> WorkerErrorCategory {
        self.category
    }

    pub(crate) fn is_lane_local_source_failure(&self) -> bool {
        matches!(
            self.category,
            WorkerErrorCategory::Input | WorkerErrorCategory::Network
        )
    }

    fn new(category: WorkerErrorCategory, message: impl Into<String>) -> Self {
        Self {
            category,
            message: message.into(),
        }
    }
}

impl fmt::Display for WorkerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.category.as_str(), self.message)
    }
}

impl std::error::Error for WorkerError {}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerState {
    pub schema_version: u32,
    pub config: ConfigIdentity,
    #[serde(default)]
    pub source_generation: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signal_lifecycle: Option<WorkerSignalLifecycle>,
    pub source_contract_sha256: String,
    pub long_feature_sha256: String,
    pub carry_feature_sha256: String,
    pub long_destination: u16,
    pub carry_destination: u16,
    pub universe: UniverseIdentity,
    pub last_input_sequence: u64,
    pub long_output_sequence: u64,
    pub carry_output_sequence: u64,
    pub last_observed_ts_ms: i64,
    pub last_long_feature_ts_ms: Option<i64>,
    #[serde(default)]
    pub last_long_output_available_at_ms: Option<i64>,
    #[serde(default)]
    pub pending_long_refresh_feature_ts_ms: Option<i64>,
    #[serde(default)]
    pub long_skipped_generation_count: u64,
    #[serde(default)]
    pub last_long_skipped_first_ts_ms: Option<i64>,
    #[serde(default)]
    pub last_long_skipped_last_ts_ms: Option<i64>,
    pub last_carry_decision_ts_ms: Option<i64>,
    #[serde(default)]
    pub last_carry_output_available_at_ms: Option<i64>,
    #[serde(default)]
    pub last_carry_scorer_ts_ms: Option<i64>,
    #[serde(default)]
    pub last_carry_upcoming_ts_ms: Option<i64>,
    #[serde(default)]
    pub bootstrap_coverage: Option<BootstrapCoverage>,
    pub klines: KlineHistory,
    #[serde(default)]
    pub kline_checked_from_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub kline_checked_through_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub kline_coverage_intervals: BTreeMap<String, Vec<CoverageInterval>>,
    pub funding: FundingHistory,
    #[serde(default)]
    pub funding_checked_from_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub funding_checked_through_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub funding_coverage_intervals: BTreeMap<String, Vec<CoverageInterval>>,
    pub whales: WhaleHistory,
    #[serde(default)]
    pub whale_checked_from_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub whale_checked_through_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub whale_coverage_intervals: BTreeMap<String, Vec<CoverageInterval>>,
    pub instruments: BTreeMap<String, InstrumentObservation>,
    #[serde(default)]
    pub instrument_trading_intervals: BTreeMap<String, Vec<InstrumentTradingInterval>>,
    #[serde(default)]
    pub instrument_status_unknown_since_ms: BTreeMap<String, i64>,
    pub tickers: BTreeMap<String, TickerObservation>,
}

impl WorkerState {
    pub(crate) fn kline_coverage(&self) -> CoverageRef<'_> {
        CoverageRef::new(
            &self.kline_checked_from_ms,
            &self.kline_checked_through_ms,
            &self.kline_coverage_intervals,
        )
    }

    pub(crate) fn funding_coverage(&self) -> CoverageRef<'_> {
        CoverageRef::new(
            &self.funding_checked_from_ms,
            &self.funding_checked_through_ms,
            &self.funding_coverage_intervals,
        )
    }

    pub(crate) fn whale_coverage(&self) -> CoverageRef<'_> {
        CoverageRef::new(
            &self.whale_checked_from_ms,
            &self.whale_checked_through_ms,
            &self.whale_coverage_intervals,
        )
    }

    pub(crate) fn kline_coverage_mut(&mut self) -> CoverageMut<'_> {
        CoverageMut::new(
            &mut self.kline_checked_from_ms,
            &mut self.kline_checked_through_ms,
            &mut self.kline_coverage_intervals,
            "kline",
        )
    }

    pub(crate) fn funding_coverage_mut(&mut self) -> CoverageMut<'_> {
        CoverageMut::new(
            &mut self.funding_checked_from_ms,
            &mut self.funding_checked_through_ms,
            &mut self.funding_coverage_intervals,
            "funding",
        )
    }

    pub(crate) fn whale_coverage_mut(&mut self) -> CoverageMut<'_> {
        CoverageMut::new(
            &mut self.whale_checked_from_ms,
            &mut self.whale_checked_through_ms,
            &mut self.whale_coverage_intervals,
            "whale",
        )
    }

    fn new(
        config: &SignalWorkerConfig,
        universe: UniverseIdentity,
        source_generation: String,
    ) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            config: config.identity.clone(),
            source_generation,
            signal_lifecycle: None,
            source_contract_sha256: source_history_hash(config),
            long_feature_sha256: state_part_hash(&config.long),
            carry_feature_sha256: state_part_hash(&config.carry),
            long_destination: config.long_destination,
            carry_destination: config.carry_destination,
            universe,
            last_input_sequence: 0,
            long_output_sequence: 0,
            carry_output_sequence: 0,
            last_observed_ts_ms: 0,
            last_long_feature_ts_ms: None,
            last_long_output_available_at_ms: None,
            pending_long_refresh_feature_ts_ms: None,
            long_skipped_generation_count: 0,
            last_long_skipped_first_ts_ms: None,
            last_long_skipped_last_ts_ms: None,
            last_carry_decision_ts_ms: None,
            last_carry_output_available_at_ms: None,
            last_carry_scorer_ts_ms: None,
            last_carry_upcoming_ts_ms: None,
            bootstrap_coverage: None,
            klines: BTreeMap::new(),
            kline_checked_from_ms: BTreeMap::new(),
            kline_checked_through_ms: BTreeMap::new(),
            kline_coverage_intervals: BTreeMap::new(),
            funding: BTreeMap::new(),
            funding_checked_from_ms: BTreeMap::new(),
            funding_checked_through_ms: BTreeMap::new(),
            funding_coverage_intervals: BTreeMap::new(),
            whales: BTreeMap::new(),
            whale_checked_from_ms: BTreeMap::new(),
            whale_checked_through_ms: BTreeMap::new(),
            whale_coverage_intervals: BTreeMap::new(),
            instruments: BTreeMap::new(),
            instrument_trading_intervals: BTreeMap::new(),
            instrument_status_unknown_since_ms: BTreeMap::new(),
            tickers: BTreeMap::new(),
        }
    }
}

struct HistoryBatch<R> {
    symbol: String,
    available_at_ms: i64,
    checked_from_ms: Option<i64>,
    checked_through_ms: Option<i64>,
    replace_coverage: bool,
    rows: R,
}

#[derive(Clone)]
pub struct SignalWorker {
    config: SignalWorkerConfig,
    state: WorkerState,
    suppressed_output_kinds: BTreeSet<&'static str>,
}

/// The venue host whose instrument list bounds what this realm's account may
/// trade. Demo observes mainnet market data but can only trade what the demo
/// venue lists.
pub fn realm_endpoint(config: &SignalWorkerConfig) -> &str {
    match config.live.environment.as_str() {
        "demo" => config.sources.bybit_demo_host.as_str(),
        _ => config.sources.bybit_mainnet_host.as_str(),
    }
}

impl SignalWorker {
    /// A worker that has not derived its universe yet. It refuses every input
    /// until the universe snapshot that resolves it arrives.
    pub fn new(config: SignalWorkerConfig) -> Result<Self, WorkerError> {
        let universe = unresolved_universe(&config.live.environment, realm_endpoint(&config));
        Self::new_with_source_generation(config, universe, REPLAY_SOURCE_GENERATION.to_owned())
    }

    /// A worker that starts from an already derived universe.
    pub fn with_universe(
        config: SignalWorkerConfig,
        universe: UniverseIdentity,
    ) -> Result<Self, WorkerError> {
        Self::new_with_source_generation(config, universe, REPLAY_SOURCE_GENERATION.to_owned())
    }

    fn new_with_source_generation(
        config: SignalWorkerConfig,
        universe: UniverseIdentity,
        source_generation: String,
    ) -> Result<Self, WorkerError> {
        let universe = if universe_is_resolved(&universe) {
            let observed = universe.available_at_ms;
            validate_universe(universe, observed)?
        } else {
            universe
        };
        if universe.environment != config.live.environment {
            return Err(WorkerError::config(
                "candidate universe environment disagrees with signal config",
            ));
        }
        validate_source_generation(&source_generation)?;
        output_source(&config.routing.source, &source_generation, true)?;
        output_source(&config.routing.source, &source_generation, false)?;
        Ok(Self {
            state: WorkerState::new(&config, universe, source_generation),
            config,
            suppressed_output_kinds: BTreeSet::new(),
        })
    }

    pub fn restore(config: SignalWorkerConfig, state: WorkerState) -> Result<Self, WorkerError> {
        if state.schema_version != SCHEMA_VERSION {
            return Err(WorkerError::state("checkpoint schema has drifted"));
        }
        if state.universe.environment != config.live.environment {
            return Err(WorkerError::state(
                "checkpoint universe belongs to another realm",
            ));
        }
        let current_source = source_history_hash(&config);
        if state.source_contract_sha256 != current_source {
            return Err(WorkerError::state(
                "checkpoint public source contract has drifted; a new cold start is required",
            ));
        }
        if state.long_destination != config.long_destination
            || state.carry_destination != config.carry_destination
        {
            return Err(WorkerError::state(
                "engine strategy slot order changed for a directional sleeve",
            ));
        }
        if state.last_input_sequence == u64::MAX
            || state.long_output_sequence == u64::MAX
            || state.carry_output_sequence == u64::MAX
        {
            return Err(WorkerError::state("checkpoint sequence is exhausted"));
        }
        let mut state = state;
        let mut kline_symbols = state
            .universe
            .long_symbols
            .iter()
            .chain(&state.universe.carry_symbols)
            .cloned()
            .collect::<BTreeSet<_>>();
        kline_symbols.insert("BTCUSDT".to_owned());
        kline_symbols.insert("ETHUSDT".to_owned());
        kline_symbols.insert(config.long.regime_symbol.clone());
        state.kline_coverage_mut().restore(&kline_symbols)?;
        let carry_symbols = state
            .universe
            .carry_symbols
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        state.funding_coverage_mut().restore(&carry_symbols)?;
        state.whale_coverage_mut().restore(&carry_symbols)?;
        restore_instrument_trading_intervals(&mut state, &config)?;
        if state.last_carry_scorer_ts_ms.is_none() {
            state.last_carry_scorer_ts_ms = state.last_carry_decision_ts_ms;
        }
        if state
            .last_long_output_available_at_ms
            .is_some_and(|clock| clock <= 0 || clock > state.last_observed_ts_ms)
            || state
                .pending_long_refresh_feature_ts_ms
                .is_some_and(|feature| {
                    feature <= 0 || Some(feature) != state.last_long_feature_ts_ms
                })
            || (state.last_long_output_available_at_ms.is_some()
                && state.last_long_feature_ts_ms.is_none())
            || state
                .last_carry_output_available_at_ms
                .is_some_and(|clock| clock <= 0 || clock > state.last_observed_ts_ms)
            || (state.last_carry_output_available_at_ms.is_some()
                && state.last_carry_decision_ts_ms.is_none())
        {
            return Err(WorkerError::state(
                "checkpoint signal publication clocks are invalid",
            ));
        }
        if state.source_generation.is_empty() {
            state.source_generation = random_source_generation()?;
            state.long_output_sequence = 0;
            state.carry_output_sequence = 0;
            state.last_long_feature_ts_ms = None;
            state.last_long_output_available_at_ms = None;
            state.pending_long_refresh_feature_ts_ms = None;
            state.last_carry_decision_ts_ms = None;
            state.last_carry_output_available_at_ms = None;
            state.last_carry_scorer_ts_ms = None;
            state.last_carry_upcoming_ts_ms = None;
        }
        validate_source_generation(&state.source_generation)?;
        output_source(&config.routing.source, &state.source_generation, true)?;
        output_source(&config.routing.source, &state.source_generation, false)?;
        let long_feature_sha256 = state_part_hash(&config.long);
        let carry_feature_sha256 = state_part_hash(&config.carry);
        if state.long_feature_sha256 != long_feature_sha256
            || state.config.long_decision_fingerprint != config.identity.long_decision_fingerprint
        {
            state.last_long_feature_ts_ms = None;
            state.last_long_output_available_at_ms = None;
            state.pending_long_refresh_feature_ts_ms = None;
        }
        if state.carry_feature_sha256 != carry_feature_sha256
            || state.config.carry_decision_fingerprint != config.identity.carry_decision_fingerprint
        {
            state.last_carry_decision_ts_ms = None;
            state.last_carry_output_available_at_ms = None;
            state.last_carry_scorer_ts_ms = None;
            state.last_carry_upcoming_ts_ms = None;
        }
        state.config = config.identity.clone();
        state.long_feature_sha256 = long_feature_sha256;
        state.carry_feature_sha256 = carry_feature_sha256;
        let mut worker = Self {
            config,
            state,
            suppressed_output_kinds: BTreeSet::new(),
        };
        worker.retain_owned_tickers();
        if worker.state.last_observed_ts_ms > 0 {
            worker.prune(worker.state.last_observed_ts_ms);
        }
        Ok(worker)
    }

    pub fn state(&self) -> &WorkerState {
        &self.state
    }

    pub fn next_input_sequence(&self) -> Result<u64, WorkerError> {
        self.state
            .last_input_sequence
            .checked_add(1)
            .ok_or_else(|| WorkerError::state("input sequence exhausted"))
    }

    fn set_suppressed_output_kinds(&mut self, kinds: BTreeSet<&'static str>) {
        self.suppressed_output_kinds = kinds;
    }

    pub fn apply(&mut self, event: WireEvent) -> Result<Vec<NormalizedObservation>, WorkerError> {
        if event.schema_version() != SCHEMA_VERSION {
            return Err(WorkerError::input(format!(
                "wire schema {} is unsupported",
                event.schema_version()
            )));
        }
        let expected = self.next_input_sequence()?;
        if event.sequence() != expected {
            return Err(WorkerError::input(format!(
                "wire sequence gap: expected {expected}, got {}",
                event.sequence()
            )));
        }
        if !universe_is_resolved(&self.state.universe)
            && !matches!(event, WireEvent::UniverseSnapshot { .. })
        {
            return Err(WorkerError::input(
                "universe is unresolved; the first input must be a universe snapshot",
            ));
        }
        let mut observations = Vec::new();
        let event_sequence = event.sequence();
        match event {
            WireEvent::BybitKlineBatch {
                symbol,
                available_at_ms,
                checked_from_ms,
                checked_through_ms,
                replace_coverage,
                rows,
                ..
            } => {
                self.apply_kline_batch(HistoryBatch {
                    symbol,
                    available_at_ms,
                    checked_from_ms,
                    checked_through_ms,
                    replace_coverage,
                    rows,
                })?;
            }
            WireEvent::BybitFundingBatch {
                symbol,
                available_at_ms,
                checked_from_ms,
                checked_through_ms,
                replace_coverage,
                emit_lifecycle,
                rows,
                ..
            } => {
                observations.extend(self.apply_funding_batch(
                    HistoryBatch {
                        symbol,
                        available_at_ms,
                        checked_from_ms,
                        checked_through_ms,
                        replace_coverage,
                        rows,
                    },
                    emit_lifecycle,
                )?);
            }
            WireEvent::BybitInstrumentSnapshot {
                observed_ts_ms,
                available_at_ms,
                rows,
                ..
            } => {
                self.apply_instruments(observed_ts_ms, available_at_ms, rows)?;
            }
            WireEvent::BybitTickerSnapshot {
                observed_ts_ms,
                available_at_ms,
                rows,
                ..
            } => {
                observations.extend(self.apply_tickers(observed_ts_ms, available_at_ms, rows)?);
            }
            WireEvent::BinanceWhaleBatch {
                available_at_ms,
                coverage,
                rows,
                ..
            } => {
                self.apply_whales(available_at_ms, coverage, rows)?;
            }
            WireEvent::UniverseSnapshot { universe, .. } => {
                self.apply_universe(universe)?;
            }
            WireEvent::LlmGateCandidates {
                observed_ts_ms,
                available_at_ms,
                decision_ts_ms,
                valid_until_ms,
                rows,
                ..
            } => {
                observations.extend(self.apply_gate_candidates(
                    observed_ts_ms,
                    available_at_ms,
                    decision_ts_ms,
                    valid_until_ms,
                    rows,
                )?);
            }
            WireEvent::BootstrapComplete { coverage, .. } => {
                self.apply_bootstrap(coverage)?;
            }
            WireEvent::Watermark { observed_ts_ms, .. } => {
                if observed_ts_ms <= 0 || observed_ts_ms < self.state.last_observed_ts_ms {
                    return Err(WorkerError::input("watermark moved backwards"));
                }
                self.state.last_observed_ts_ms = observed_ts_ms;
                observations.extend(self.build_at_watermark(
                    observed_ts_ms,
                    observed_ts_ms,
                    true,
                    true,
                    &[],
                    &[],
                )?);
                self.prune(observed_ts_ms);
            }
            WireEvent::LongWatermark {
                observed_ts_ms,
                data_through_ms,
                gap_symbols,
                ..
            } => {
                if observed_ts_ms <= 0
                    || data_through_ms <= 0
                    || data_through_ms > observed_ts_ms
                    || observed_ts_ms < self.state.last_observed_ts_ms
                {
                    return Err(WorkerError::input("watermark moved backwards"));
                }
                self.state.last_observed_ts_ms = observed_ts_ms;
                observations.extend(self.build_at_watermark(
                    data_through_ms,
                    observed_ts_ms,
                    true,
                    false,
                    &gap_symbols,
                    &[],
                )?);
                self.prune(observed_ts_ms);
            }
            WireEvent::CarryWatermark {
                observed_ts_ms,
                data_through_ms,
                gap_symbols,
                ..
            } => {
                if observed_ts_ms <= 0
                    || data_through_ms <= 0
                    || data_through_ms > observed_ts_ms
                    || observed_ts_ms < self.state.last_observed_ts_ms
                {
                    return Err(WorkerError::input("watermark moved backwards"));
                }
                self.state.last_observed_ts_ms = observed_ts_ms;
                observations.extend(self.build_at_watermark(
                    data_through_ms,
                    observed_ts_ms,
                    false,
                    true,
                    &[],
                    &gap_symbols,
                )?);
                self.prune(observed_ts_ms);
            }
            WireEvent::CarryScorerCatchupWatermark {
                observed_ts_ms,
                decision_through_ms,
                gap_symbols,
                ..
            } => {
                if observed_ts_ms <= 0
                    || decision_through_ms <= 0
                    || decision_through_ms > observed_ts_ms
                    || observed_ts_ms < self.state.last_observed_ts_ms
                {
                    return Err(WorkerError::input(
                        "CARRY catch-up watermark moved backwards",
                    ));
                }
                self.state.last_observed_ts_ms = observed_ts_ms;
                observations.extend(self.build_carry_scorer_catchup(
                    decision_through_ms,
                    observed_ts_ms,
                    &gap_symbols,
                )?);
                self.prune(observed_ts_ms);
            }
        }
        self.state.last_input_sequence = event_sequence;
        Ok(observations)
    }

    fn apply_kline_batch(
        &mut self,
        batch: HistoryBatch<Vec<Vec<serde_json::Value>>>,
    ) -> Result<(), WorkerError> {
        let HistoryBatch {
            symbol,
            available_at_ms,
            checked_from_ms,
            checked_through_ms,
            replace_coverage,
            rows,
        } = batch;
        let normalized = normalize_kline_rows(&symbol, available_at_ms, &rows)?;
        for row in normalized {
            merge_row(
                self.state.klines.entry(row.symbol.clone()).or_default(),
                row,
            )?;
        }
        // Kline replacement revokes coverage even if its frontier is absent or invalid.
        if replace_coverage {
            self.state.kline_checked_from_ms.remove(&symbol);
            self.state.kline_checked_through_ms.remove(&symbol);
            self.state.kline_coverage_intervals.remove(&symbol);
        }
        self.state.kline_coverage_mut().merge(
            &symbol,
            checked_from_ms,
            checked_through_ms,
            available_at_ms,
            replace_coverage,
        )?;
        self.state.last_observed_ts_ms = self.state.last_observed_ts_ms.max(available_at_ms);
        let prune_clock_ms = self.state.last_observed_ts_ms;
        self.prune(prune_clock_ms);
        Ok(())
    }

    fn apply_funding_batch(
        &mut self,
        batch: HistoryBatch<Vec<crate::model::BybitFundingWire>>,
        emit_lifecycle: bool,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        let HistoryBatch {
            symbol,
            available_at_ms,
            checked_from_ms,
            checked_through_ms,
            replace_coverage,
            rows,
        } = batch;
        let mut observations = Vec::new();
        let normalized = normalize_funding_rows(&symbol, available_at_ms, &rows)?;
        let mut inserted = Vec::new();
        for row in normalized {
            if merge_row(
                self.state.funding.entry(row.symbol.clone()).or_default(),
                row.clone(),
            )? {
                inserted.push(row);
            }
        }
        if emit_lifecycle && !inserted.is_empty() && self.state.last_carry_decision_ts_ms.is_some()
        {
            let decision_ts_ms = self
                .state
                .last_carry_decision_ts_ms
                .expect("checked CARRY decision cursor");
            let lifecycle_rows = inserted
                .into_iter()
                .filter(|row| {
                    row.settlement_ts_ms > decision_ts_ms && row.settlement_ts_ms <= available_at_ms
                })
                .collect::<Vec<_>>();
            let observed = lifecycle_rows.iter().map(|row| row.settlement_ts_ms).max();
            if let Some(observed) = observed {
                observations.push(self.carry_observation(
                    "funding_update",
                    observed,
                    available_at_ms,
                    ObservationPayload::FundingUpdate {
                        decision_ts_ms,
                        settled_funding: lifecycle_rows,
                    },
                    Vec::new(),
                )?);
            }
        }
        self.state.funding_coverage_mut().merge(
            &symbol,
            checked_from_ms,
            checked_through_ms,
            available_at_ms,
            replace_coverage,
        )?;
        self.state.last_observed_ts_ms = self.state.last_observed_ts_ms.max(available_at_ms);
        let prune_clock_ms = self.state.last_observed_ts_ms;
        self.prune(prune_clock_ms);
        Ok(observations)
    }

    fn apply_instruments(
        &mut self,
        observed_ts_ms: i64,
        available_at_ms: i64,
        rows: Vec<crate::model::BybitInstrumentWire>,
    ) -> Result<(), WorkerError> {
        let allowed = self.owned_market_symbols();
        let next = normalize_instruments(observed_ts_ms, available_at_ms, &rows)?
            .into_iter()
            .filter(|row| allowed.contains(&row.symbol))
            .map(|row| (row.symbol.clone(), row))
            .collect::<BTreeMap<_, _>>();
        update_instrument_trading_intervals(
            &mut self.state.instrument_trading_intervals,
            &self.state.instruments,
            &next,
            &allowed,
            observed_ts_ms,
            &self.config.sources.bybit_settle_coin,
        )?;
        let mut current = next.clone();
        for symbol in &allowed {
            if let Some(row) = next.get(symbol) {
                if instrument_is_trading(row, &self.config.sources.bybit_settle_coin)
                    || row.delivery_time_ms.is_some_and(|clock| clock > 0)
                {
                    self.state.instrument_status_unknown_since_ms.remove(symbol);
                } else {
                    self.state
                        .instrument_status_unknown_since_ms
                        .entry(symbol.clone())
                        .or_insert(observed_ts_ms);
                }
                continue;
            }
            self.state
                .instrument_status_unknown_since_ms
                .entry(symbol.clone())
                .or_insert(observed_ts_ms);
            if let Some(prior) = self.state.instruments.get(symbol) {
                let mut unknown = prior.clone();
                unknown.observed_ts_ms = observed_ts_ms;
                unknown.available_at_ms = available_at_ms;
                unknown.status = None;
                current.insert(symbol.clone(), unknown);
            }
        }
        self.state.instruments = current;
        self.state.last_observed_ts_ms = self.state.last_observed_ts_ms.max(available_at_ms);
        let prune_clock_ms = self.state.last_observed_ts_ms;
        self.prune(prune_clock_ms);
        Ok(())
    }

    fn apply_tickers(
        &mut self,
        observed_ts_ms: i64,
        available_at_ms: i64,
        rows: Vec<crate::model::BybitTickerWire>,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        let mut observations = Vec::new();
        let allowed = self.owned_market_symbols();
        let rows = rows
            .into_iter()
            .filter(|row| allowed.contains(&row.symbol))
            .collect::<Vec<_>>();
        let normalized = normalize_tickers(observed_ts_ms, available_at_ms, &rows)?;
        let touched = normalized
            .iter()
            .map(|row| row.symbol.clone())
            .collect::<BTreeSet<_>>();
        for row in normalized {
            merge_ticker_observation(&mut self.state.tickers, row);
        }
        let carry_tickers: Vec<TickerObservation> = touched
            .iter()
            .filter(|symbol| self.state.universe.carry_symbols.contains(symbol))
            .filter_map(|symbol| self.state.tickers.get(symbol).cloned())
            .collect();
        let snapshot_observed_ts_ms = carry_tickers
            .iter()
            .flat_map(|row| {
                [
                    Some(row.observed_ts_ms),
                    row.mark_observed_ts_ms,
                    row.funding_observed_ts_ms,
                    row.schedule_observed_ts_ms,
                ]
                .into_iter()
                .flatten()
            })
            .fold(observed_ts_ms, i64::max)
            .min(available_at_ms);
        let (marks, presettlement) =
            self.public_market_rows(&carry_tickers, snapshot_observed_ts_ms);
        if (!marks.is_empty() || !presettlement.is_empty())
            && !self.suppressed_output_kinds.contains("market_snapshot")
        {
            let oldest_actionable_clock_ms = marks
                .iter()
                .map(|row| row.observed_ts_ms)
                .chain(presettlement.iter().map(|row| row.observed_ts_ms))
                .min()
                .unwrap_or(snapshot_observed_ts_ms);
            let expires_at_ms =
                oldest_actionable_clock_ms.saturating_add(self.config.sources.mark_max_age_ms);
            if expires_at_ms >= available_at_ms {
                observations.push(self.carry_observation(
                    "market_snapshot",
                    snapshot_observed_ts_ms,
                    available_at_ms,
                    ObservationPayload::MarketSnapshot {
                        expires_at_ms,
                        tickers: carry_tickers,
                        marks,
                        presettlement,
                    },
                    Vec::new(),
                )?);
            }
        }
        self.state.last_observed_ts_ms = self.state.last_observed_ts_ms.max(available_at_ms);
        Ok(observations)
    }

    fn apply_whales(
        &mut self,
        available_at_ms: i64,
        coverage: Vec<crate::model::SourceCoverage>,
        rows: Vec<crate::model::BinanceWhaleWire>,
    ) -> Result<(), WorkerError> {
        for row in normalize_whales(available_at_ms, &rows)? {
            merge_row(
                self.state.whales.entry(row.symbol.clone()).or_default(),
                row,
            )?;
        }
        for item in coverage {
            self.state.whale_coverage_mut().merge(
                &item.symbol,
                Some(item.checked_from_ms),
                Some(item.checked_through_ms),
                available_at_ms,
                item.replace_coverage,
            )?;
        }
        self.state.last_observed_ts_ms = self.state.last_observed_ts_ms.max(available_at_ms);
        let prune_clock_ms = self.state.last_observed_ts_ms;
        self.prune(prune_clock_ms);
        Ok(())
    }

    fn apply_universe(&mut self, universe: UniverseIdentity) -> Result<(), WorkerError> {
        let available_at_ms = universe.available_at_ms;
        let universe = validate_universe(universe, available_at_ms)?;
        if universe.environment != self.config.live.environment {
            return Err(WorkerError::input(
                "universe event environment disagrees with config",
            ));
        }
        let changed = !same_membership(&self.state.universe, &universe);
        self.state.universe = universe;
        if changed {
            self.retain_owned_tickers();
            if self.state.last_observed_ts_ms > 0 {
                let prune_clock_ms = self.state.last_observed_ts_ms;
                self.prune(prune_clock_ms);
            }
        }
        Ok(())
    }

    fn apply_gate_candidates(
        &mut self,
        observed_ts_ms: i64,
        available_at_ms: i64,
        decision_ts_ms: i64,
        valid_until_ms: i64,
        rows: Vec<crate::model::LlmGateCandidate>,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        let mut observations = Vec::new();
        if observed_ts_ms <= 0
            || available_at_ms < observed_ts_ms
            || decision_ts_ms <= 0
            || valid_until_ms <= decision_ts_ms
        {
            return Err(WorkerError::input("LLM gate publication clock is invalid"));
        }
        let tradable: BTreeSet<&str> = self
            .state
            .universe
            .symbols
            .iter()
            .map(String::as_str)
            .collect();
        let gate = &self.config.llm_gate;
        let mut accepted = Vec::new();
        let mut seen = BTreeSet::new();
        for row in rows {
            let symbol = normalized_symbol(&row.symbol)?;
            if !tradable.contains(symbol.as_str()) || !seen.insert(symbol.clone()) {
                continue;
            }
            let usable = row.score.is_finite()
                && row.score >= gate.min_score
                && matches!(row.band.as_str(), "core" | "wide")
                && row.trigger_ts_ms > 0
                && row.trigger_ts_ms <= available_at_ms
                && available_at_ms - row.trigger_ts_ms <= gate.trigger_max_age_ms
                && row.trigger_price.is_finite()
                && row.trigger_price > 0.0
                && row.atr_pct.is_finite()
                && row.atr_pct > 0.0
                && row.atr_pct < 1.0
                && row
                    .sigma_daily_30d
                    .is_none_or(|value| value.is_finite() && value >= 0.0)
                && row
                    .turnover_rank
                    .is_none_or(|value| value.is_finite() && value >= 1.0)
                && row.trigger_window_h.is_none_or(|value| value > 0);
            if usable {
                accepted.push(crate::model::LlmGateCandidate { symbol, ..row });
            }
        }
        accepted.sort_by(|a, b| a.symbol.cmp(&b.symbol));
        if !self.suppressed_output_kinds.contains("llm_gate_candidates") {
            let symbols: Vec<String> = accepted.iter().map(|row| row.symbol.clone()).collect();
            let subscriptions = market_subscriptions(&symbols)?;
            let btc_rv_30 = crate::features::current_btc_rv_30(
                &self.state.klines,
                available_at_ms,
                &self.config.long,
            );
            observations.push(self.long_observation(
                "llm_gate_candidates",
                observed_ts_ms,
                available_at_ms,
                ObservationPayload::LlmGateCandidates {
                    decision_ts_ms,
                    valid_until_ms,
                    btc_rv_30,
                    rows: accepted,
                },
                subscriptions,
            )?);
        }
        Ok(observations)
    }

    fn apply_bootstrap(&mut self, coverage: BootstrapCoverage) -> Result<(), WorkerError> {
        if coverage.completed_at_ms <= 0
            || coverage.kline_end_ms <= 0
            || coverage.kline_end_ms % HOUR_MS != 0
            || coverage.kline_end_ms > coverage.completed_at_ms
            || coverage.funding_end_ms <= 0
            || coverage.funding_end_ms > coverage.completed_at_ms
            || coverage.whale_end_ms <= 0
            || coverage.whale_end_ms > coverage.completed_at_ms
            || coverage.source_contract_sha256 != self.state.source_contract_sha256
            || coverage.long_feature_sha256 != self.state.long_feature_sha256
            || coverage.carry_feature_sha256 != self.state.carry_feature_sha256
        {
            return Err(WorkerError::input(
                "cold-bootstrap coverage marker is invalid",
            ));
        }
        self.state.bootstrap_coverage = Some(coverage);
        Ok(())
    }

    fn build_at_watermark(
        &mut self,
        data_through_ms: i64,
        available_at_ms: i64,
        include_long: bool,
        include_carry: bool,
        long_gap_symbols: &[String],
        carry_gap_symbols: &[String],
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        let mut out = Vec::new();
        validate_gap_symbols(long_gap_symbols, &self.state.universe.long_symbols, "LONG")?;
        validate_gap_symbols(
            carry_gap_symbols,
            &self.state.universe.carry_symbols,
            "CARRY",
        )?;
        let long_gap_set: BTreeSet<&str> = long_gap_symbols.iter().map(String::as_str).collect();
        let carry_gap_set: BTreeSet<&str> = carry_gap_symbols.iter().map(String::as_str).collect();
        let active_long_symbols = self
            .state
            .universe
            .long_symbols
            .iter()
            .filter(|symbol| !long_gap_set.contains(symbol.as_str()))
            .filter(|symbol| self.is_trading_instrument(symbol))
            .cloned()
            .collect::<Vec<_>>();
        let active_carry_symbols = self
            .state
            .universe
            .carry_symbols
            .iter()
            .filter(|symbol| !carry_gap_set.contains(symbol.as_str()))
            .filter(|symbol| self.is_trading_instrument(symbol))
            .cloned()
            .collect::<Vec<_>>();
        let mut long = build_long_features(
            &self.state.klines,
            &active_long_symbols,
            data_through_ms,
            &self.config.long,
        );
        long.rejections.extend(
            long_gap_symbols
                .iter()
                .filter(|symbol| !self.config.long.exclude_symbols.contains(symbol))
                .map(|symbol| DataRejection {
                    symbol: symbol.clone(),
                    reason: "source_gap_unchecked_through".to_owned(),
                    first_missing_ts_ms: Some(data_through_ms.saturating_sub(HOUR_MS)),
                }),
        );
        long.rejections.extend(
            self.state
                .universe
                .long_symbols
                .iter()
                .filter(|symbol| !long_gap_set.contains(symbol.as_str()))
                .filter(|symbol| !self.is_trading_instrument(symbol))
                .filter(|symbol| !self.config.long.exclude_symbols.contains(symbol))
                .map(|symbol| DataRejection {
                    symbol: symbol.clone(),
                    reason: "instrument_not_trading".to_owned(),
                    first_missing_ts_ms: None,
                }),
        );
        long.rejections
            .sort_by(|left, right| left.symbol.cmp(&right.symbol));
        let long_ready = long.feature_ts_ms.is_some() && !long.rows.is_empty();
        if include_long {
            if let Some(feature_ts_ms) = long.feature_ts_ms {
                let current_is_new = self.state.last_long_feature_ts_ms < Some(feature_ts_ms);
                let refresh_pending =
                    self.state.pending_long_refresh_feature_ts_ms == Some(feature_ts_ms);
                if (current_is_new || refresh_pending) && !long.rows.is_empty() {
                    if self.suppressed_output_kinds.contains("long_feature_batch") {
                        if current_is_new {
                            if let Some(prior_feature_ts_ms) = self.state.last_long_feature_ts_ms {
                                self.record_long_skipped_range(
                                    prior_feature_ts_ms,
                                    feature_ts_ms.saturating_sub(DAY_MS),
                                );
                            }
                            self.state.last_long_feature_ts_ms = Some(feature_ts_ms);
                            self.state.pending_long_refresh_feature_ts_ms = Some(feature_ts_ms);
                        }
                    } else {
                        if current_is_new {
                            self.record_long_fast_forward(feature_ts_ms);
                        }
                        let marks =
                            self.current_marks(&self.state.universe.long_symbols, available_at_ms);
                        let accepted_symbols = long
                            .rows
                            .iter()
                            .map(|row| row.symbol.clone())
                            .collect::<Vec<_>>();
                        let subscriptions = market_subscriptions(&accepted_symbols)?;
                        out.push(self.long_observation(
                            "long_feature_batch",
                            available_at_ms,
                            available_at_ms,
                            ObservationPayload::LongFeatureBatch {
                                decision_ts_ms: available_at_ms,
                                feature_ts_ms,
                                rows: long.rows,
                                marks,
                                cold_start_fallback_count: long.fallback_count,
                                rejections: long.rejections.clone(),
                            },
                            subscriptions,
                        )?);
                        self.state.last_long_feature_ts_ms = Some(feature_ts_ms);
                        self.state.last_long_output_available_at_ms = Some(available_at_ms);
                        self.state.pending_long_refresh_feature_ts_ms = None;
                    }
                }
            }
        }
        if !include_carry {
            return Ok(out);
        }
        let mut carry = build_carry_features(
            &self.state.klines,
            &self.state.funding,
            &self.state.whales,
            &active_carry_symbols,
            data_through_ms,
            &self.config.carry,
        );
        carry
            .rejections
            .extend(carry_gap_symbols.iter().map(|symbol| DataRejection {
                symbol: symbol.clone(),
                reason: "source_gap_unchecked_through".to_owned(),
                first_missing_ts_ms: Some(data_through_ms.saturating_sub(HOUR_MS)),
            }));
        carry.rejections.extend(
            self.state
                .universe
                .carry_symbols
                .iter()
                .filter(|symbol| !carry_gap_set.contains(symbol.as_str()))
                .filter(|symbol| !self.is_trading_instrument(symbol))
                .map(|symbol| DataRejection {
                    symbol: symbol.clone(),
                    reason: "instrument_not_trading".to_owned(),
                    first_missing_ts_ms: None,
                }),
        );
        carry
            .rejections
            .sort_by(|left, right| left.symbol.cmp(&right.symbol));
        if carry.decision_ts_ms.is_some_and(|decision_ts_ms| {
            carry_funding_coverage(
                &carry.rows,
                decision_ts_ms,
                active_carry_symbols.len(),
                self.config.carry.persistence_window_settlements.is_some(),
            ) < self.config.carry.minimum_funding_coverage
        }) {
            carry.rows.clear();
        }
        if let Some(decision_ts_ms) = carry.decision_ts_ms {
            let current_is_new = self.state.last_carry_decision_ts_ms < Some(decision_ts_ms);
            let scorer_is_new = self
                .state
                .last_carry_scorer_ts_ms
                .or(self.state.last_carry_decision_ts_ms)
                < Some(decision_ts_ms);
            let upcoming_ts_ms = decision_ts_ms + DAY_MS;
            let upcoming_rows = if upcoming_ts_ms <= data_through_ms
                && self.state.last_carry_upcoming_ts_ms < Some(upcoming_ts_ms)
            {
                let upcoming = build_carry_features_at(
                    &self.state.klines,
                    &self.state.funding,
                    &self.state.whales,
                    &active_carry_symbols,
                    upcoming_ts_ms,
                    available_at_ms,
                    &self.config.carry,
                );
                if upcoming.rejections.is_empty()
                    && upcoming.rows.len() == active_carry_symbols.len()
                    && carry_funding_coverage(
                        &upcoming.rows,
                        upcoming_ts_ms,
                        active_carry_symbols.len(),
                        self.config.carry.persistence_window_settlements.is_some(),
                    ) >= self.config.carry.minimum_funding_coverage
                {
                    upcoming.rows
                } else {
                    Vec::new()
                }
            } else {
                Vec::new()
            };
            let upcoming_is_new = !upcoming_rows.is_empty();
            if (current_is_new || upcoming_is_new) && !carry.rows.is_empty() {
                let rows = if self.state.last_carry_decision_ts_ms.is_some() {
                    carry.rows.clone()
                } else {
                    build_carry_replay_features(
                        &self.state.klines,
                        &self.state.funding,
                        &self.state.whales,
                        &active_carry_symbols,
                        decision_ts_ms,
                        available_at_ms,
                        &self.config.carry,
                    )
                    .rows
                };
                if self.suppressed_output_kinds.contains("carry_feature_batch") {
                    if current_is_new && scorer_is_new {
                        out.push(self.carry_observation(
                            "carry_scorer_catchup",
                            available_at_ms,
                            available_at_ms,
                            ObservationPayload::CarryScorerCatchup {
                                decision_ts_ms,
                                rows,
                                rejections: carry.rejections.clone(),
                            },
                            Vec::new(),
                        )?);
                        self.state.last_carry_scorer_ts_ms = Some(decision_ts_ms);
                    }
                } else {
                    let marks =
                        self.current_marks(&self.state.universe.carry_symbols, available_at_ms);
                    let settled_funding = if current_is_new {
                        self.funding_between(self.state.last_carry_decision_ts_ms, decision_ts_ms)
                    } else {
                        Vec::new()
                    };
                    let ticker_rows: Vec<TickerObservation> = self
                        .state
                        .tickers
                        .values()
                        .filter(|row| self.state.universe.carry_symbols.contains(&row.symbol))
                        .cloned()
                        .collect();
                    let (_, presettlement) = self.public_market_rows(&ticker_rows, available_at_ms);
                    let accepted_symbols = carry
                        .rows
                        .iter()
                        .filter(|row| row.bar_ts_ms == decision_ts_ms)
                        .map(|row| row.symbol.clone())
                        .collect::<Vec<_>>();
                    let subscriptions = market_subscriptions(&accepted_symbols)?;
                    out.push(self.carry_observation(
                        "carry_feature_batch",
                        available_at_ms,
                        available_at_ms,
                        ObservationPayload::CarryFeatureBatch {
                            decision_ts_ms,
                            rows,
                            upcoming_rows,
                            settled_funding,
                            presettlement,
                            marks,
                            rejections: carry.rejections.clone(),
                        },
                        subscriptions,
                    )?);
                    if current_is_new {
                        self.state.last_carry_decision_ts_ms = Some(decision_ts_ms);
                        self.state.last_carry_scorer_ts_ms = Some(decision_ts_ms);
                        self.state.last_carry_output_available_at_ms = Some(available_at_ms);
                    }
                    if upcoming_is_new {
                        self.state.last_carry_upcoming_ts_ms = Some(upcoming_ts_ms);
                    }
                }
            }
        }
        if out.is_empty() && !self.suppressed_output_kinds.contains("readiness") {
            let funding_rows = carry
                .rows
                .iter()
                .filter(|row| row.by_funding.is_some())
                .count();
            let funding_coverage = if carry.rows.is_empty() {
                0.0
            } else {
                funding_rows as f64 / carry.rows.len() as f64
            };
            let carry_ready = carry.rows.len() >= self.config.carry.minimum_decision_symbols
                && funding_coverage >= self.config.carry.minimum_funding_coverage;
            let mut rejected = long.rejections;
            rejected.extend(carry.rejections);
            rejected.sort_by(|a, b| (&a.symbol, &a.reason).cmp(&(&b.symbol, &b.reason)));
            let readiness = Readiness {
                long_ready,
                carry_ready,
                universe_ready: universe_is_resolved(&self.state.universe),
                reason: if long_ready && carry_ready {
                    "ready_no_new_decision".to_owned()
                } else {
                    "cold_start_or_gap".to_owned()
                },
                long_feature_ts_ms: long.feature_ts_ms,
                carry_feature_ts_ms: carry.decision_ts_ms,
                rejected_symbols: rejected,
            };
            out.push(self.carry_observation(
                "readiness",
                data_through_ms,
                available_at_ms,
                ObservationPayload::Readiness { readiness },
                Vec::new(),
            )?);
        }
        Ok(out)
    }

    fn build_carry_scorer_catchup(
        &mut self,
        decision_through_ms: i64,
        available_at_ms: i64,
        carry_gap_symbols: &[String],
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        validate_gap_symbols(
            carry_gap_symbols,
            &self.state.universe.carry_symbols,
            "CARRY",
        )?;
        let Some(mut decision_ts_ms) = self
            .state
            .last_carry_scorer_ts_ms
            .or(self.state.last_carry_decision_ts_ms)
            .map(|last| last.saturating_add(DAY_MS))
        else {
            return Err(WorkerError::state(
                "CARRY scorer catch-up requires a seeded producer cursor",
            ));
        };
        let maximum_through_ms = decision_ts_ms
            .saturating_add((MAX_CARRY_SCORER_CATCHUP_DAYS - 1).saturating_mul(DAY_MS));
        if decision_through_ms > maximum_through_ms {
            return Err(WorkerError::input(
                "CARRY scorer catch-up exceeds the bounded daily chunk",
            ));
        }
        let gaps = carry_gap_symbols.iter().collect::<BTreeSet<_>>();
        let mut observations = Vec::new();
        while decision_ts_ms <= decision_through_ms {
            let active = self
                .state
                .universe
                .carry_symbols
                .iter()
                .filter(|symbol| !gaps.contains(symbol))
                .filter(|symbol| self.was_trading_instrument_at(symbol, decision_ts_ms))
                .cloned()
                .collect::<Vec<_>>();
            let mut build = build_carry_features_at(
                &self.state.klines,
                &self.state.funding,
                &self.state.whales,
                &active,
                decision_ts_ms,
                available_at_ms,
                &self.config.carry,
            );
            build
                .rejections
                .extend(carry_gap_symbols.iter().map(|symbol| DataRejection {
                    symbol: symbol.clone(),
                    reason: "source_gap_unchecked_through".to_owned(),
                    first_missing_ts_ms: Some(decision_ts_ms.saturating_sub(HOUR_MS)),
                }));
            build.rejections.extend(
                self.state
                    .universe
                    .carry_symbols
                    .iter()
                    .filter(|symbol| !gaps.contains(symbol))
                    .filter(|symbol| !self.was_trading_instrument_at(symbol, decision_ts_ms))
                    .map(|symbol| DataRejection {
                        symbol: symbol.clone(),
                        reason: "instrument_not_trading".to_owned(),
                        first_missing_ts_ms: None,
                    }),
            );
            build
                .rejections
                .sort_by(|left, right| left.symbol.cmp(&right.symbol));
            if build.decision_ts_ms != Some(decision_ts_ms) || build.rows.is_empty() {
                break;
            }
            if carry_funding_coverage(
                &build.rows,
                decision_ts_ms,
                active.len(),
                self.config.carry.persistence_window_settlements.is_some(),
            ) < self.config.carry.minimum_funding_coverage
            {
                break;
            }
            observations.push(self.carry_observation(
                "carry_scorer_catchup",
                available_at_ms,
                available_at_ms,
                ObservationPayload::CarryScorerCatchup {
                    decision_ts_ms,
                    rows: build.rows,
                    rejections: build.rejections,
                },
                Vec::new(),
            )?);
            self.state.last_carry_scorer_ts_ms = Some(decision_ts_ms);
            decision_ts_ms = decision_ts_ms.saturating_add(DAY_MS);
        }
        Ok(observations)
    }

    fn public_market_rows(
        &self,
        rows: &[TickerObservation],
        observed_ts_ms: i64,
    ) -> (Vec<MarketMark>, Vec<PresettlementPublicObservation>) {
        let allowed: BTreeSet<&str> = self
            .state
            .universe
            .carry_symbols
            .iter()
            .map(String::as_str)
            .collect();
        let mut marks = Vec::new();
        let mut presettlement = Vec::new();
        for row in rows
            .iter()
            .filter(|row| allowed.contains(row.symbol.as_str()))
        {
            let mark_observed_ts_ms = row.mark_observed_ts_ms.unwrap_or(row.observed_ts_ms);
            let mark_is_fresh = mark_observed_ts_ms <= observed_ts_ms
                && observed_ts_ms.saturating_sub(mark_observed_ts_ms)
                    <= self.config.sources.mark_max_age_ms;
            let funding_observed_ts_ms = row.funding_observed_ts_ms.unwrap_or(row.observed_ts_ms);
            let funding_is_fresh = funding_observed_ts_ms <= observed_ts_ms
                && observed_ts_ms.saturating_sub(funding_observed_ts_ms)
                    <= self.config.sources.mark_max_age_ms;
            let schedule_observed_ts_ms = row.schedule_observed_ts_ms.unwrap_or(row.observed_ts_ms);
            let schedule_is_fresh = schedule_observed_ts_ms <= observed_ts_ms
                && observed_ts_ms.saturating_sub(schedule_observed_ts_ms)
                    <= self.config.sources.mark_max_age_ms;
            if let Some(mark_px) = row.mark_price.filter(|_| mark_is_fresh) {
                marks.push(MarketMark {
                    symbol: row.symbol.clone(),
                    observed_ts_ms: mark_observed_ts_ms,
                    mark_px,
                });
            }
            if let (Some(settlement_ts_ms), Some(running_rate)) = (
                row.next_funding_time_ms.filter(|_| schedule_is_fresh),
                row.funding_rate.filter(|_| funding_is_fresh),
            ) {
                let remaining = settlement_ts_ms - observed_ts_ms;
                if (0..=self.config.carry.presettlement_window_ms).contains(&remaining) {
                    presettlement.push(PresettlementPublicObservation {
                        symbol: row.symbol.clone(),
                        observed_ts_ms: funding_observed_ts_ms.min(schedule_observed_ts_ms),
                        settlement_ts_ms,
                        running_rate,
                        mark_px: row.mark_price.filter(|_| mark_is_fresh),
                    });
                }
            }
        }
        marks.sort_by(|a, b| a.symbol.cmp(&b.symbol));
        presettlement.sort_by(|a, b| a.symbol.cmp(&b.symbol));
        (marks, presettlement)
    }

    fn is_trading_instrument(&self, symbol: &str) -> bool {
        self.state
            .instruments
            .get(symbol)
            .is_some_and(|row| instrument_is_trading(row, &self.config.sources.bybit_settle_coin))
    }

    fn was_trading_instrument_at(&self, symbol: &str, decision_ts_ms: i64) -> bool {
        if self
            .state
            .instrument_status_unknown_since_ms
            .get(symbol)
            .is_some_and(|unknown_since| decision_ts_ms >= *unknown_since)
        {
            return false;
        }
        self.state
            .instrument_trading_intervals
            .get(symbol)
            .is_some_and(|intervals| {
                intervals.iter().any(|interval| {
                    interval.trading_from_ms <= decision_ts_ms
                        && interval
                            .trading_through_ms
                            .is_none_or(|through| decision_ts_ms < through)
                })
            })
    }

    fn owned_market_symbols(&self) -> BTreeSet<String> {
        self.state
            .universe
            .long_symbols
            .iter()
            .chain(&self.state.universe.carry_symbols)
            .cloned()
            .chain([self.config.long.regime_symbol.clone(), "ETHUSDT".to_owned()])
            .collect()
    }

    fn retain_owned_tickers(&mut self) {
        let allowed = self.owned_market_symbols();
        self.state
            .tickers
            .retain(|symbol, _| allowed.contains(symbol));
        self.state
            .instruments
            .retain(|symbol, _| allowed.contains(symbol));
        self.state
            .instrument_trading_intervals
            .retain(|symbol, _| allowed.contains(symbol));
        self.state
            .instrument_status_unknown_since_ms
            .retain(|symbol, _| allowed.contains(symbol));
    }

    fn current_marks(&self, symbols: &[String], observed_ts_ms: i64) -> Vec<MarketMark> {
        symbols
            .iter()
            .filter_map(|symbol| {
                self.state.tickers.get(symbol).and_then(|ticker| {
                    let mark_observed_ts_ms =
                        ticker.mark_observed_ts_ms.unwrap_or(ticker.observed_ts_ms);
                    (ticker.available_at_ms <= observed_ts_ms
                        && observed_ts_ms.saturating_sub(mark_observed_ts_ms)
                            <= self.config.sources.mark_max_age_ms)
                        .then_some(ticker)
                        .and_then(|ticker| {
                            ticker.mark_price.map(|mark_px| MarketMark {
                                symbol: symbol.clone(),
                                observed_ts_ms: mark_observed_ts_ms,
                                mark_px,
                            })
                        })
                })
            })
            .collect()
    }

    fn funding_between(&self, previous: Option<i64>, decision_ts_ms: i64) -> Vec<SettledFunding> {
        let lower = previous.unwrap_or(decision_ts_ms - DAY_MS);
        let allowed: BTreeSet<&str> = self
            .state
            .universe
            .carry_symbols
            .iter()
            .map(String::as_str)
            .collect();
        let mut rows: Vec<SettledFunding> = self
            .state
            .funding
            .iter()
            .filter(|(symbol, _)| allowed.contains(symbol.as_str()))
            .flat_map(|(_, rows)| {
                rows.range((lower + 1)..=decision_ts_ms)
                    .map(|(_, row)| row.clone())
            })
            .collect();
        rows.sort_by(|a, b| (a.settlement_ts_ms, &a.symbol).cmp(&(b.settlement_ts_ms, &b.symbol)));
        rows
    }

    fn long_observation(
        &mut self,
        kind: &str,
        observed_wall_ts_ms: i64,
        available_wall_ts_ms: i64,
        payload: ObservationPayload,
        subscriptions: Vec<Subscription>,
    ) -> Result<NormalizedObservation, WorkerError> {
        self.state.long_output_sequence = self
            .state
            .long_output_sequence
            .checked_add(1)
            .ok_or_else(|| WorkerError::state("LONG output sequence exhausted"))?;
        make_observation_in_epoch(
            &self.config,
            &self.state.universe,
            &self.state.source_generation,
            self.state
                .signal_lifecycle
                .as_ref()
                .and_then(|state| state.epoch),
            true,
            self.state.long_output_sequence,
            kind,
            observed_wall_ts_ms,
            available_wall_ts_ms,
            payload,
            subscriptions,
        )
    }

    fn carry_observation(
        &mut self,
        kind: &str,
        observed_wall_ts_ms: i64,
        available_wall_ts_ms: i64,
        payload: ObservationPayload,
        subscriptions: Vec<Subscription>,
    ) -> Result<NormalizedObservation, WorkerError> {
        self.state.carry_output_sequence = self
            .state
            .carry_output_sequence
            .checked_add(1)
            .ok_or_else(|| WorkerError::state("CARRY output sequence exhausted"))?;
        make_observation_in_epoch(
            &self.config,
            &self.state.universe,
            &self.state.source_generation,
            self.state
                .signal_lifecycle
                .as_ref()
                .and_then(|state| state.epoch),
            false,
            self.state.carry_output_sequence,
            kind,
            observed_wall_ts_ms,
            available_wall_ts_ms,
            payload,
            subscriptions,
        )
    }

    fn record_long_fast_forward(&mut self, feature_ts_ms: i64) {
        let Some(last_ts_ms) = self.state.last_long_feature_ts_ms else {
            return;
        };
        let skipped = feature_ts_ms
            .saturating_sub(last_ts_ms)
            .saturating_div(DAY_MS)
            .saturating_sub(1);
        if skipped <= 0 {
            return;
        }
        self.state.long_skipped_generation_count = self
            .state
            .long_skipped_generation_count
            .saturating_add(u64::try_from(skipped).unwrap_or(u64::MAX));
        self.state.last_long_skipped_first_ts_ms = Some(last_ts_ms.saturating_add(DAY_MS));
        self.state.last_long_skipped_last_ts_ms = Some(feature_ts_ms.saturating_sub(DAY_MS));
    }

    fn record_long_skipped_range(&mut self, first_ts_ms: i64, last_ts_ms: i64) {
        if first_ts_ms <= 0
            || last_ts_ms < first_ts_ms
            || first_ts_ms.rem_euclid(DAY_MS) != 0
            || last_ts_ms.rem_euclid(DAY_MS) != 0
        {
            return;
        }
        let skipped = last_ts_ms
            .saturating_sub(first_ts_ms)
            .saturating_div(DAY_MS)
            .saturating_add(1);
        self.state.long_skipped_generation_count = self
            .state
            .long_skipped_generation_count
            .saturating_add(u64::try_from(skipped).unwrap_or(u64::MAX));
        if self
            .state
            .last_long_skipped_last_ts_ms
            .is_some_and(|last| last.saturating_add(DAY_MS) == first_ts_ms)
        {
            self.state.last_long_skipped_last_ts_ms = Some(last_ts_ms);
        } else {
            self.state.last_long_skipped_first_ts_ms = Some(first_ts_ms);
            self.state.last_long_skipped_last_ts_ms = Some(last_ts_ms);
        }
    }

    fn prune(&mut self, observed_ts_ms: i64) {
        let retained_through_ms = observed_ts_ms - observed_ts_ms.rem_euclid(HOUR_MS);
        let carry_retained_through_ms = self
            .state
            .last_carry_scorer_ts_ms
            .or(self.state.last_carry_decision_ts_ms)
            .map(|last| last.saturating_add(DAY_MS).min(retained_through_ms))
            .unwrap_or(retained_through_ms);
        let mut long_symbols: BTreeSet<String> =
            self.state.universe.long_symbols.iter().cloned().collect();
        long_symbols.insert(self.config.long.regime_symbol.clone());
        long_symbols.insert("ETHUSDT".to_owned());
        let carry_symbols: BTreeSet<String> =
            self.state.universe.carry_symbols.iter().cloned().collect();
        let symbols: BTreeSet<String> = long_symbols.union(&carry_symbols).cloned().collect();
        let carry_cursor_ms = self
            .state
            .last_carry_scorer_ts_ms
            .or(self.state.last_carry_decision_ts_ms);
        let cold_carry_instrument_hours = required_carry_history_hours(&self.config, &self.state)
            .max(
                i64::try_from(self.config.carry.whale_feed_days)
                    .unwrap_or(i64::MAX / 24)
                    .saturating_mul(24),
            );
        let mut instrument_retained_from_ms = BTreeMap::new();
        self.state
            .klines
            .retain(|symbol, _| symbols.contains(symbol));
        self.state
            .kline_coverage_mut()
            .retain_symbols(|symbol| symbols.contains(symbol));
        for symbol in symbols {
            let long_cutoff = long_symbols.contains(&symbol).then(|| {
                retained_through_ms.saturating_sub(
                    i64::try_from(self.config.long.cold_start_lookback_days)
                        .unwrap_or(i64::MAX / 24)
                        .saturating_mul(24)
                        .saturating_add(48)
                        .saturating_mul(HOUR_MS),
                )
            });
            let carry_cutoff = carry_symbols.contains(&symbol).then(|| {
                carry_retained_through_ms.saturating_sub(
                    required_carry_history_hours(&self.config, &self.state).saturating_mul(HOUR_MS),
                )
            });
            let carry_instrument_cutoff = carry_symbols.contains(&symbol).then(|| {
                carry_cursor_ms.unwrap_or_else(|| {
                    carry_retained_through_ms
                        .saturating_sub(cold_carry_instrument_hours.saturating_mul(HOUR_MS))
                })
            });
            if let Some(retained_from_ms) =
                long_cutoff.into_iter().chain(carry_instrument_cutoff).min()
            {
                instrument_retained_from_ms.insert(symbol.clone(), retained_from_ms);
            }
            let windows = long_cutoff
                .map(|from| (from, retained_through_ms))
                .into_iter()
                .chain(carry_cutoff.map(|from| (from, carry_retained_through_ms)))
                .collect::<Vec<_>>();
            if let Some(rows) = self.state.klines.get_mut(&symbol) {
                rows.retain(|timestamp, _| {
                    windows
                        .iter()
                        .any(|(from, through)| from <= timestamp && timestamp < through)
                });
            }
            self.state
                .kline_coverage_mut()
                .retain_windows(&symbol, &windows);
        }
        self.state.kline_coverage_mut().drop_empty();
        let funding_hours = required_carry_history_hours(&self.config, &self.state);
        let funding_cutoff =
            carry_retained_through_ms.saturating_sub(funding_hours.saturating_mul(HOUR_MS));
        self.state
            .funding_coverage_mut()
            .retain_symbols(|symbol| carry_symbols.contains(symbol));
        let current_funding_cutoff =
            retained_through_ms.saturating_sub(funding_hours.saturating_mul(HOUR_MS));
        let funding_windows = [
            (funding_cutoff, carry_retained_through_ms),
            (current_funding_cutoff, retained_through_ms),
        ];
        for rows in self.state.funding.values_mut() {
            rows.retain(|timestamp, _| {
                funding_windows
                    .iter()
                    .any(|(from, through)| from <= timestamp && timestamp <= through)
            });
        }
        for symbol in &carry_symbols {
            self.state
                .funding_coverage_mut()
                .retain_windows(symbol, &funding_windows);
        }
        self.state.funding_coverage_mut().drop_empty();
        let whale_cutoff = carry_retained_through_ms.saturating_sub(
            (self.config.carry.whale_change_lookback_hours
                + self.config.carry.whale_freshness_hours
                + 24)
                .saturating_mul(HOUR_MS),
        );
        self.state
            .whale_coverage_mut()
            .retain_symbols(|symbol| carry_symbols.contains(symbol));
        let current_whale_cutoff = retained_through_ms.saturating_sub(
            (self.config.carry.whale_change_lookback_hours
                + self.config.carry.whale_freshness_hours
                + 24)
                .saturating_mul(HOUR_MS),
        );
        let whale_windows = [
            (whale_cutoff, carry_retained_through_ms),
            (current_whale_cutoff, retained_through_ms),
        ];
        for rows in self.state.whales.values_mut() {
            rows.retain(|timestamp, _| {
                whale_windows
                    .iter()
                    .any(|(from, through)| from <= timestamp && timestamp <= through)
            });
        }
        for symbol in &carry_symbols {
            self.state
                .whale_coverage_mut()
                .retain_windows(symbol, &whale_windows);
        }
        self.state.whale_coverage_mut().drop_empty();
        for (symbol, intervals) in &mut self.state.instrument_trading_intervals {
            let Some(retained_from_ms) = instrument_retained_from_ms.get(symbol) else {
                intervals.clear();
                continue;
            };
            intervals.retain(|interval| {
                interval
                    .trading_through_ms
                    .is_none_or(|through| through > *retained_from_ms)
            });
        }
        self.state
            .instrument_trading_intervals
            .retain(|_, intervals| !intervals.is_empty());
    }
}

/// Bybit publishes `deliveryTime: "0"` on a perpetual and a real clock on a
/// dated or delisting contract. At or past that clock the contract is not
/// trading, whatever `status` still says.
fn instrument_is_trading(row: &InstrumentObservation, settle_coin: &str) -> bool {
    row.status.as_deref() == Some("Trading")
        && row.settle_coin.as_deref() == Some(settle_coin)
        && !row.is_prelisting
        && row
            .delivery_time_ms
            .is_none_or(|clock| clock <= 0 || clock > row.observed_ts_ms)
}

fn restore_instrument_trading_intervals(
    state: &mut WorkerState,
    config: &SignalWorkerConfig,
) -> Result<(), WorkerError> {
    let allowed = state
        .universe
        .long_symbols
        .iter()
        .chain(&state.universe.carry_symbols)
        .cloned()
        .chain([config.long.regime_symbol.clone(), "ETHUSDT".to_owned()])
        .collect::<BTreeSet<_>>();
    if state
        .instrument_trading_intervals
        .keys()
        .any(|symbol| !allowed.contains(symbol))
        || state
            .instrument_status_unknown_since_ms
            .iter()
            .any(|(symbol, clock)| {
                !allowed.contains(symbol) || *clock <= 0 || *clock > state.last_observed_ts_ms
            })
    {
        return Err(WorkerError::state(
            "checkpoint instrument history contains an invalid symbol or clock",
        ));
    }
    for (symbol, row) in &state.instruments {
        if !allowed.contains(symbol) {
            return Err(WorkerError::state(
                "checkpoint instrument inventory contains an unowned symbol",
            ));
        }
        if state.instrument_trading_intervals.contains_key(symbol) {
            continue;
        }
        let interval = if instrument_is_trading(row, &config.sources.bybit_settle_coin) {
            let from = row
                .launch_time_ms
                .filter(|clock| *clock > 0 && *clock <= row.observed_ts_ms)
                .unwrap_or(row.observed_ts_ms);
            Some(InstrumentTradingInterval {
                trading_from_ms: from,
                trading_through_ms: row.delivery_time_ms.filter(|clock| *clock > from),
            })
        } else {
            row.launch_time_ms
                .zip(row.delivery_time_ms)
                .filter(|(from, through)| {
                    *from > 0 && *from < *through && *through <= row.observed_ts_ms
                })
                .map(|(from, through)| InstrumentTradingInterval {
                    trading_from_ms: from,
                    trading_through_ms: Some(through),
                })
        };
        if let Some(interval) = interval {
            state
                .instrument_trading_intervals
                .insert(symbol.clone(), vec![interval]);
        }
    }
    for (symbol, unknown_since_ms) in &state.instrument_status_unknown_since_ms {
        if let Some(intervals) = state.instrument_trading_intervals.get_mut(symbol) {
            close_active_trading_interval(intervals, *unknown_since_ms, None);
        }
    }
    for intervals in state.instrument_trading_intervals.values() {
        if intervals.is_empty() {
            return Err(WorkerError::state(
                "checkpoint instrument trading history cardinality is invalid",
            ));
        }
        let mut prior_through = None;
        for (index, interval) in intervals.iter().enumerate() {
            if interval.trading_from_ms <= 0
                || interval
                    .trading_through_ms
                    .is_some_and(|through| through <= interval.trading_from_ms)
                || prior_through.is_some_and(|through| through > interval.trading_from_ms)
                || (interval.trading_through_ms.is_none() && index + 1 != intervals.len())
            {
                return Err(WorkerError::state(
                    "checkpoint instrument trading history is not canonical",
                ));
            }
            prior_through = interval.trading_through_ms;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn update_instrument_trading_intervals(
    intervals_by_symbol: &mut BTreeMap<String, Vec<InstrumentTradingInterval>>,
    prior: &BTreeMap<String, InstrumentObservation>,
    next: &BTreeMap<String, InstrumentObservation>,
    allowed: &BTreeSet<String>,
    observed_ts_ms: i64,
    settle_coin: &str,
) -> Result<(), WorkerError> {
    for symbol in allowed {
        let intervals = intervals_by_symbol.entry(symbol.clone()).or_default();
        let prior_trading = prior
            .get(symbol)
            .is_some_and(|row| instrument_is_trading(row, settle_coin));
        let next_row = next.get(symbol);
        let next_trading = next_row.is_some_and(|row| instrument_is_trading(row, settle_coin));
        let history_active = intervals.last().is_some_and(|interval| {
            interval.trading_from_ms <= observed_ts_ms
                && interval
                    .trading_through_ms
                    .is_none_or(|through| observed_ts_ms < through)
        });
        if next_row.is_none() {
            if history_active {
                close_active_trading_interval(intervals, observed_ts_ms, None);
            }
            continue;
        }
        if next_trading {
            if !prior_trading && !history_active {
                let row = next_row.expect("checked next trading instrument");
                let from = if intervals.is_empty() {
                    row.launch_time_ms
                        .filter(|clock| *clock > 0 && *clock <= observed_ts_ms)
                        .unwrap_or(observed_ts_ms)
                } else {
                    observed_ts_ms
                };
                let through = row.delivery_time_ms.filter(|clock| *clock > from);
                if through.is_some_and(|clock| clock <= observed_ts_ms) {
                    return Err(WorkerError::input(
                        "Trading instrument has already passed its delivery time",
                    ));
                }
                intervals.push(InstrumentTradingInterval {
                    trading_from_ms: from,
                    trading_through_ms: through,
                });
            } else if let Some(row) = next_row {
                if let Some(last) = intervals.last_mut() {
                    if last.trading_through_ms.is_none() {
                        last.trading_through_ms =
                            row.delivery_time_ms.filter(|clock| *clock > observed_ts_ms);
                    }
                }
            }
        } else if prior_trading || history_active {
            close_active_trading_interval(
                intervals,
                observed_ts_ms,
                next_row.and_then(|row| row.delivery_time_ms),
            );
        } else if intervals.is_empty() {
            if let Some(row) = next_row {
                if let Some((from, through)) =
                    row.launch_time_ms
                        .zip(row.delivery_time_ms)
                        .filter(|(from, through)| {
                            *from > 0 && *from < *through && *through <= observed_ts_ms
                        })
                {
                    intervals.push(InstrumentTradingInterval {
                        trading_from_ms: from,
                        trading_through_ms: Some(through),
                    });
                }
            }
        }
    }
    intervals_by_symbol.retain(|_, intervals| !intervals.is_empty());
    Ok(())
}

fn merge_ticker_observation(
    rows: &mut BTreeMap<String, TickerObservation>,
    incoming: TickerObservation,
) {
    let Some(existing) = rows.get_mut(&incoming.symbol) else {
        rows.insert(incoming.symbol.clone(), incoming);
        return;
    };

    if incoming.observed_ts_ms >= existing.observed_ts_ms {
        existing.last_price = incoming.last_price;
        existing.index_price = incoming.index_price;
        existing.bid1_price = incoming.bid1_price;
        existing.ask1_price = incoming.ask1_price;
        existing.bid1_size = incoming.bid1_size;
        existing.ask1_size = incoming.ask1_size;
        existing.open_interest = incoming.open_interest;
        existing.open_interest_value = incoming.open_interest_value;
        existing.turnover_24h = incoming.turnover_24h;
        existing.volume_24h = incoming.volume_24h;
        existing.observed_ts_ms = incoming.observed_ts_ms;
    }
    if incoming.mark_observed_ts_ms.is_some_and(|clock| {
        existing
            .mark_observed_ts_ms
            .is_none_or(|existing_clock| clock >= existing_clock)
    }) {
        existing.mark_price = incoming.mark_price;
        existing.mark_observed_ts_ms = incoming.mark_observed_ts_ms;
    }
    if incoming.funding_observed_ts_ms.is_some_and(|clock| {
        existing
            .funding_observed_ts_ms
            .is_none_or(|existing_clock| clock >= existing_clock)
    }) {
        existing.funding_rate = incoming.funding_rate;
        existing.funding_observed_ts_ms = incoming.funding_observed_ts_ms;
    }
    if incoming.schedule_observed_ts_ms.is_some_and(|clock| {
        existing
            .schedule_observed_ts_ms
            .is_none_or(|existing_clock| clock >= existing_clock)
    }) {
        existing.next_funding_time_ms = incoming.next_funding_time_ms;
        existing.schedule_observed_ts_ms = incoming.schedule_observed_ts_ms;
    }
    existing.available_at_ms = existing.available_at_ms.max(incoming.available_at_ms);
}

fn close_active_trading_interval(
    intervals: &mut Vec<InstrumentTradingInterval>,
    unknown_at_ms: i64,
    explicit_through_ms: Option<i64>,
) {
    let Some(last) = intervals.last() else {
        return;
    };
    if last.trading_from_ms > unknown_at_ms
        || last
            .trading_through_ms
            .is_some_and(|through| through <= unknown_at_ms)
    {
        return;
    }
    let through = explicit_through_ms
        .filter(|clock| *clock > last.trading_from_ms && *clock <= unknown_at_ms)
        .unwrap_or(unknown_at_ms);
    if through <= last.trading_from_ms {
        intervals.pop();
    } else if let Some(last) = intervals.last_mut() {
        last.trading_through_ms = Some(through);
    }
}

fn carry_funding_coverage(
    rows: &[crate::model::CarryFeatureRow],
    decision_ts_ms: i64,
    expected_symbols: usize,
    require_persistence: bool,
) -> f64 {
    if expected_symbols == 0 {
        return 0.0;
    }
    let present = rows
        .iter()
        .filter(|row| {
            row.bar_ts_ms == decision_ts_ms
                && row.by_funding.is_some()
                && row.trail_fund_24h.is_some()
                && (!require_persistence || row.crowd_persistence.is_some())
        })
        .count();
    present as f64 / expected_symbols as f64
}

fn market_subscriptions(symbols: &[String]) -> Result<Vec<Subscription>, WorkerError> {
    let unique: BTreeSet<&str> = symbols.iter().map(String::as_str).collect();
    let subscription_count = unique.len().checked_mul(2).ok_or_else(|| {
        WorkerError::config("signal universe quote/ticker subscription count overflowed")
    })?;
    if subscription_count > MAX_SIGNAL_SUBSCRIPTIONS {
        return Err(WorkerError::config(format!(
            "signal universe requests {subscription_count} quote/ticker subscriptions; maximum is {MAX_SIGNAL_SUBSCRIPTIONS}"
        )));
    }
    let mut subscriptions = Vec::with_capacity(subscription_count);
    for symbol in unique {
        subscriptions.push(Subscription {
            symbol: symbol.to_owned(),
            feed: Feed::Quote,
        });
        subscriptions.push(Subscription {
            symbol: symbol.to_owned(),
            feed: Feed::Ticker,
        });
    }
    Ok(subscriptions)
}

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
fn make_observation(
    config: &SignalWorkerConfig,
    universe: &UniverseIdentity,
    source_generation: &str,
    long: bool,
    sequence: u64,
    kind: &str,
    observed_wall_ts_ms: i64,
    available_wall_ts_ms: i64,
    payload: ObservationPayload,
    subscriptions: Vec<Subscription>,
) -> Result<NormalizedObservation, WorkerError> {
    make_observation_in_epoch(
        config,
        universe,
        source_generation,
        None,
        long,
        sequence,
        kind,
        observed_wall_ts_ms,
        available_wall_ts_ms,
        payload,
        subscriptions,
    )
}

#[allow(clippy::too_many_arguments)]
fn make_observation_in_epoch(
    config: &SignalWorkerConfig,
    universe: &UniverseIdentity,
    source_generation: &str,
    epoch: Option<u64>,
    long: bool,
    sequence: u64,
    kind: &str,
    observed_wall_ts_ms: i64,
    available_wall_ts_ms: i64,
    payload: ObservationPayload,
    subscriptions: Vec<Subscription>,
) -> Result<NormalizedObservation, WorkerError> {
    if observed_wall_ts_ms <= 0 || available_wall_ts_ms < observed_wall_ts_ms {
        return Err(WorkerError::state(
            "signal availability must be at or after a positive observation time",
        ));
    }
    let envelope = SignalPayloadEnvelope {
        schema_version: SCHEMA_VERSION,
        config: config.identity.clone(),
        universe: Some(universe.clone()),
        payload,
    };
    let payload = serde_json::to_vec(&envelope)
        .map_err(|error| WorkerError::json("encode normalized signal payload", error))?;
    if payload.len() > MAX_SIGNAL_OBSERVATION_BYTES {
        return Err(WorkerError::state(format!(
            "normalized signal payload exceeds {MAX_SIGNAL_OBSERVATION_BYTES} bytes"
        )));
    }
    let source = match epoch {
        Some(epoch) => lifecycle::managed_output_source(
            &config.routing.source,
            source_generation,
            epoch,
            long,
        )?,
        None => output_source(&config.routing.source, source_generation, long)?,
    };
    let destination = StrategyId(if long {
        config.long_destination
    } else {
        config.carry_destination
    });
    let decision_fingerprint = if long {
        config.identity.long_decision_fingerprint.clone()
    } else {
        config.identity.carry_decision_fingerprint.clone()
    };
    let observation_id = semantic_id(
        &source,
        destination,
        sequence,
        kind,
        observed_wall_ts_ms,
        available_wall_ts_ms,
        &payload,
    );
    let mut observation = NormalizedObservation {
        schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint,
        destination,
        source,
        sequence,
        observation_id,
        kind: kind.to_owned(),
        observed_wall_ts_ms,
        available_wall_ts_ms,
        subscriptions,
        payload,
        content_sha256: String::new(),
    };
    observation.content_sha256 = sha256_hex(&observation.canonical_envelope_bytes());
    Ok(observation)
}

fn semantic_id(
    source: &str,
    destination: StrategyId,
    sequence: u64,
    kind: &str,
    observed: i64,
    available: i64,
    payload: &[u8],
) -> String {
    let mut hasher = Sha256::new();
    for value in [source.as_bytes(), kind.as_bytes(), payload] {
        hasher.update((value.len() as u64).to_le_bytes());
        hasher.update(value);
    }
    hasher.update(destination.0.to_le_bytes());
    hasher.update(sequence.to_le_bytes());
    hasher.update(observed.to_le_bytes());
    hasher.update(available.to_le_bytes());
    hex::encode(hasher.finalize())
}

fn state_part_hash<T: Serialize>(value: &T) -> String {
    let bytes = serde_json::to_vec(value).expect("typed state identity must serialize");
    sha256_hex(&bytes)
}

fn source_history_hash(config: &SignalWorkerConfig) -> String {
    let source = &config.sources;
    state_part_hash(&(
        &source.bybit_category,
        &source.bybit_settle_coin,
        &source.bybit_mainnet_host,
        &source.bybit_demo_host,
        &source.binance_host,
        source.kline_interval_minutes,
        &source.funding_event_kind,
        &source.whale_source,
        &source.whale_period,
        source.universe_identity_required,
        &config.live.public_market_realm,
        &config.routing.source,
    ))
}

fn validate_source_generation(value: &str) -> Result<(), WorkerError> {
    if value.len() != SOURCE_GENERATION_BYTES * 2
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(WorkerError::state(
            "signal source generation must be 32 lowercase hex characters",
        ));
    }
    Ok(())
}

fn random_source_generation() -> Result<String, WorkerError> {
    loop {
        let mut bytes = [0_u8; SOURCE_GENERATION_BYTES];
        getrandom::fill(&mut bytes).map_err(|error| {
            WorkerError::state(format!("cannot generate signal source identity: {error}"))
        })?;
        let generation = hex::encode(bytes);
        if generation != REPLAY_SOURCE_GENERATION {
            return Ok(generation);
        }
    }
}

fn output_source(base: &str, generation: &str, long: bool) -> Result<String, WorkerError> {
    let lane = if long { "long" } else { "carry" };
    let source = if generation == REPLAY_SOURCE_GENERATION {
        format!("{base}.{lane}")
    } else {
        format!("{base}.g{generation}.{lane}")
    };
    if source.len() > SIGNAL_SOURCE_BYTES_MAX {
        return Err(WorkerError::config(format!(
            "signal source namespace is {} bytes; maximum is {SIGNAL_SOURCE_BYTES_MAX}",
            source.len()
        )));
    }
    Ok(source)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PendingTransaction {
    schema_version: u32,
    prior_state_sha256: String,
    next_state_sha256: String,
    observation_json: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct InputJournalEntry {
    schema_version: u32,
    replay_config: SignalWorkerConfig,
    events: Vec<WireEvent>,
    #[serde(default)]
    suppressed_output_kinds: Vec<String>,
    observation_json: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DurabilityMetrics {
    pub checkpoint_bytes: u64,
    pub journal_bytes: u64,
    pub checkpoint_writes_session: u64,
    pub journal_entries_retained: u64,
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

#[derive(Clone, Debug, PartialEq)]
pub struct BatchCommitReceipt {
    pub observations: Vec<NormalizedObservation>,
    pub attempted_events: usize,
    pub committed_events: usize,
}

impl BatchCommitReceipt {
    pub fn fully_committed(&self) -> bool {
        self.attempted_events == self.committed_events
    }
}

pub struct DurableSignalWorker {
    worker: SignalWorker,
    checkpoint: AtomicJsonStore,
    pending: AtomicJsonStore,
    pending_next: AtomicJsonStore,
    journal: AppendJournal,
    spool: SpoolWriter,
    checkpoint_sha256: String,
    checkpoint_writes_session: u64,
    journal_entries_retained: u64,
    spool_files: u64,
    spool_bytes: u64,
    pending_replaceable_paths: BTreeMap<String, std::path::PathBuf>,
    spool_classes: BTreeMap<String, SpoolClassInventory>,
    replaceable_outputs_coalesced: u64,
    spool_backpressured: bool,
    spool_backpressured_classes: BTreeSet<String>,
    publication_pending: bool,
}

impl DurableSignalWorker {
    pub fn open(
        config: SignalWorkerConfig,
        state_dir: impl AsRef<Path>,
        spool_dir: impl AsRef<Path>,
    ) -> Result<Self, WorkerError> {
        let universe = unresolved_universe(&config.live.environment, realm_endpoint(&config));
        Self::open_with_universe(config, universe, state_dir, spool_dir)
    }

    /// Seeds a missing checkpoint with `universe`. An existing checkpoint keeps
    /// the universe it recorded; the live runner refreshes it from the venue.
    pub fn open_with_universe(
        config: SignalWorkerConfig,
        universe: UniverseIdentity,
        state_dir: impl AsRef<Path>,
        spool_dir: impl AsRef<Path>,
    ) -> Result<Self, WorkerError> {
        std::fs::create_dir_all(state_dir.as_ref())
            .map_err(|error| WorkerError::io("create signal state directory", error))?;
        cleanup_atomic_temporary_files(
            state_dir.as_ref(),
            &[
                "checkpoint.json",
                "pending-transaction.json",
                "pending-next-state.json",
            ],
        )?;
        let checkpoint = AtomicJsonStore::new(state_dir.as_ref().join("checkpoint.json"));
        let pending = AtomicJsonStore::new(state_dir.as_ref().join("pending-transaction.json"));
        let pending_next = AtomicJsonStore::new(state_dir.as_ref().join("pending-next-state.json"));
        let journal = AppendJournal::new(state_dir.as_ref().join("hot-input-journal.jsonl"));
        let spool = SpoolWriter::new(spool_dir.as_ref())?;
        let mut checkpoint_writes_session = 0_u64;
        if !checkpoint.path().exists() {
            let initial = SignalWorker::new_with_source_generation(
                config.clone(),
                universe.clone(),
                random_source_generation()?,
            )?;
            checkpoint.save(initial.state())?;
            checkpoint_writes_session = checkpoint_writes_session.saturating_add(1);
        }
        if let Some(transaction) = pending.load::<PendingTransaction>()? {
            finish_pending(&checkpoint, &pending, &pending_next, &spool, &transaction)?;
        } else {
            pending_next.remove()?;
        }
        let state = checkpoint
            .load::<WorkerState>()?
            .ok_or_else(|| WorkerError::state("durable checkpoint disappeared"))?;
        let checkpoint_needs_adoption = state.source_generation.is_empty()
            || state.config != config.identity
            || state.long_feature_sha256 != state_part_hash(&config.long)
            || state.carry_feature_sha256 != state_part_hash(&config.carry);
        let mut checkpoint_state = Some(state);
        let mut replay_worker: Option<SignalWorker> = None;
        let mut applied_journal = 0_u64;
        let journal_entries = journal.replay::<InputJournalEntry, _>(|entry| {
            validate_hot_journal_entry(&entry)?;
            let current_sequence = replay_worker
                .as_ref()
                .map(|worker| worker.state.last_input_sequence)
                .or_else(|| {
                    checkpoint_state
                        .as_ref()
                        .map(|state| state.last_input_sequence)
                })
                .ok_or_else(|| WorkerError::state("journal replay lost worker state"))?;
            let first_sequence = entry.events[0].sequence();
            let last_sequence = entry.events[entry.events.len() - 1].sequence();
            if last_sequence <= current_sequence {
                return Ok(());
            }
            if first_sequence != current_sequence.saturating_add(1) {
                return Err(WorkerError::state(format!(
                    "input journal gap: expected {}, got {first_sequence}",
                    current_sequence.saturating_add(1)
                )));
            }
            if replay_worker.is_none() {
                let state = checkpoint_state
                    .take()
                    .ok_or_else(|| WorkerError::state("journal replay lost checkpoint"))?;
                replay_worker = Some(SignalWorker::restore(entry.replay_config.clone(), state)?);
            }
            let worker = replay_worker
                .as_mut()
                .ok_or_else(|| WorkerError::state("journal replay worker is absent"))?;
            if worker.config != entry.replay_config {
                return Err(WorkerError::state(
                    "input journal contains more than one runtime configuration",
                ));
            }
            let suppressed = entry
                .suppressed_output_kinds
                .iter()
                .filter_map(|kind| match kind.as_str() {
                    "market_snapshot" => Some("market_snapshot"),
                    "readiness" => Some("readiness"),
                    "long_feature_batch" => Some("long_feature_batch"),
                    "carry_feature_batch" => Some("carry_feature_batch"),
                    "llm_gate_candidates" => Some("llm_gate_candidates"),
                    _ => None,
                })
                .collect();
            worker.set_suppressed_output_kinds(suppressed);
            let mut observations = Vec::new();
            for event in entry.events {
                observations.extend(worker.apply(event)?);
            }
            worker.set_suppressed_output_kinds(BTreeSet::new());
            // Compared as values: the journal holds the bytes as they were
            // written, and the payload's wire encoding changed 2026-09-03.
            let journaled = entry
                .observation_json
                .iter()
                .map(|json| {
                    serde_json::from_str::<NormalizedObservation>(json)
                        .map_err(|error| WorkerError::json("parse journaled observation", error))
                })
                .collect::<Result<Vec<_>, _>>()?;
            if observations != journaled {
                return Err(WorkerError::state(
                    "input journal replay changed signal observation bytes",
                ));
            }
            for json in &entry.observation_json {
                spool.write_encoded(json.as_bytes())?;
            }
            applied_journal = applied_journal.saturating_add(1);
            Ok(())
        })?;
        let historical_worker = if let Some(worker) = replay_worker {
            worker
        } else {
            let state = checkpoint_state
                .take()
                .ok_or_else(|| WorkerError::state("checkpoint state is absent"))?;
            SignalWorker::restore(config.clone(), state)?
        };
        let needs_adoption = checkpoint_needs_adoption || historical_worker.config != config;
        let worker = if needs_adoption {
            SignalWorker::restore(config, historical_worker.state)?
        } else {
            historical_worker
        };
        if applied_journal > 0 || needs_adoption {
            checkpoint.save(worker.state())?;
            checkpoint_writes_session = checkpoint_writes_session.saturating_add(1);
        }
        if journal_entries > 0 {
            journal.remove()?;
        }
        let checkpoint_sha256 = checkpoint
            .sha256()?
            .ok_or_else(|| WorkerError::state("durable checkpoint disappeared"))?;
        let spool_inventory = spool.inventory()?;
        Ok(Self {
            worker,
            checkpoint,
            pending,
            pending_next,
            journal,
            spool,
            checkpoint_sha256,
            checkpoint_writes_session,
            journal_entries_retained: 0,
            spool_files: spool_inventory.files,
            spool_bytes: spool_inventory.bytes,
            pending_replaceable_paths: spool_inventory.replaceable_paths,
            spool_classes: spool_inventory.classes,
            replaceable_outputs_coalesced: 0,
            spool_backpressured: false,
            spool_backpressured_classes: BTreeSet::new(),
            publication_pending: false,
        })
    }

    pub fn worker(&self) -> &SignalWorker {
        &self.worker
    }

    pub fn apply_and_commit(
        &mut self,
        event: WireEvent,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        Ok(self
            .apply_many_and_commit(std::iter::once(event))?
            .observations)
    }

    pub fn apply_many_and_commit(
        &mut self,
        events: impl IntoIterator<Item = WireEvent>,
    ) -> Result<BatchCommitReceipt, WorkerError> {
        let events = events.into_iter().collect::<Vec<_>>();
        let attempted_events = events.len();
        let prior_sequence = self.worker.state.last_input_sequence;
        let mut all_observations = Vec::new();
        let mut batch = Vec::with_capacity(MAX_INPUT_BATCH_EVENTS);
        let mut batch_bytes = 0_u64;
        for event in events {
            let event_bytes = json_size(&event)?;
            if event_may_emit(&event) {
                if !batch.is_empty() {
                    let target_sequence = batch
                        .last()
                        .map(WireEvent::sequence)
                        .expect("checked nonempty input batch");
                    all_observations.extend(self.commit_event_batch(std::mem::take(&mut batch))?);
                    batch_bytes = 0;
                    if self.worker.state.last_input_sequence < target_sequence {
                        return Ok(self.batch_commit_receipt(
                            prior_sequence,
                            attempted_events,
                            all_observations,
                        ));
                    }
                }
                let target_sequence = event.sequence();
                all_observations.extend(self.commit_event_batch(vec![event])?);
                if self.worker.state.last_input_sequence < target_sequence {
                    return Ok(self.batch_commit_receipt(
                        prior_sequence,
                        attempted_events,
                        all_observations,
                    ));
                }
                continue;
            }
            if !batch.is_empty()
                && (batch.len() >= MAX_INPUT_BATCH_EVENTS
                    || batch_bytes.saturating_add(event_bytes) > MAX_INPUT_BATCH_BYTES)
            {
                let target_sequence = batch
                    .last()
                    .map(WireEvent::sequence)
                    .expect("checked nonempty input batch");
                all_observations.extend(self.commit_event_batch(std::mem::take(&mut batch))?);
                batch_bytes = 0;
                if self.worker.state.last_input_sequence < target_sequence {
                    return Ok(self.batch_commit_receipt(
                        prior_sequence,
                        attempted_events,
                        all_observations,
                    ));
                }
            }
            batch_bytes = batch_bytes.saturating_add(event_bytes);
            batch.push(event);
        }
        if !batch.is_empty() {
            all_observations.extend(self.commit_event_batch(batch)?);
        }
        Ok(self.batch_commit_receipt(prior_sequence, attempted_events, all_observations))
    }

    fn batch_commit_receipt(
        &self,
        prior_sequence: u64,
        attempted_events: usize,
        observations: Vec<NormalizedObservation>,
    ) -> BatchCommitReceipt {
        let committed_events = self
            .worker
            .state
            .last_input_sequence
            .saturating_sub(prior_sequence);
        BatchCommitReceipt {
            observations,
            attempted_events,
            committed_events: usize::try_from(committed_events).unwrap_or(usize::MAX),
        }
    }

    fn commit_event_batch(
        &mut self,
        events: Vec<WireEvent>,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        if self.publication_pending {
            return Err(WorkerError::state(
                "publication recovery requires reopening the worker",
            ));
        }
        if self
            .worker
            .state
            .signal_lifecycle
            .as_ref()
            .is_some_and(|state| state.sealed)
        {
            return Ok(Vec::new());
        }
        self.compact_if_due()?;
        self.refresh_spool_inventory_if_needed()?;
        let mut projected_by_class = BTreeMap::<&'static str, u64>::new();
        for event in &events {
            for (class, files) in self.projected_spool_files(event) {
                let projected = projected_by_class.entry(class).or_default();
                *projected = projected.saturating_add(files);
            }
        }
        let blocked_classes = projected_by_class
            .iter()
            .filter_map(|(class, projected_files)| {
                let inventory = self.spool_classes.get(*class);
                let (file_cap, byte_cap) = spool_class_caps(class);
                let byte_soft_threshold = spool_class_byte_soft_threshold(class);
                let files = inventory.map_or(0, |row| row.files);
                let bytes = inventory.map_or(0, |row| row.bytes);
                (files.saturating_add(*projected_files) > file_cap
                    || bytes > byte_cap
                    || (*projected_files > 0 && bytes >= byte_soft_threshold))
                    .then_some(*class)
            })
            .collect::<BTreeSet<_>>();
        if !blocked_classes.is_empty() {
            self.spool_backpressured_classes
                .extend(blocked_classes.into_iter().map(str::to_owned));
            return Ok(Vec::new());
        }
        let projected_files = projected_by_class.values().copied().sum::<u64>();
        if self.spool_files.saturating_add(projected_files) > MAX_SPOOL_FILES
            || self.spool_bytes > MAX_SPOOL_BYTES
            || (projected_files > 0 && self.spool_bytes >= SPOOL_BYTE_SOFT_THRESHOLD)
        {
            self.spool_backpressured = true;
            return Ok(Vec::new());
        }
        self.spool_backpressured = false;
        let suppressed = self
            .pending_replaceable_paths
            .keys()
            .filter_map(|kind| match kind.as_str() {
                "market_snapshot" => Some("market_snapshot"),
                "readiness" => Some("readiness"),
                "long_feature_batch" => Some("long_feature_batch"),
                "carry_feature_batch" => Some("carry_feature_batch"),
                _ => None,
            })
            .collect::<BTreeSet<_>>();
        if !suppressed.is_empty() {
            self.replaceable_outputs_coalesced =
                self.replaceable_outputs_coalesced.saturating_add(1);
        }
        let suppressed_output_kinds = suppressed.iter().map(ToString::to_string).collect();
        let (candidate, observations) = self.prepare_event_batch(&events, suppressed)?;
        let observation_json = encode_observations(&observations)?;
        let mut actual_by_class = BTreeMap::<&str, (u64, u64)>::new();
        for (observation, json) in observations.iter().zip(&observation_json) {
            let encoded_bytes = u64::try_from(json.len()).unwrap_or(u64::MAX);
            if encoded_bytes > MAX_SPOOL_OBSERVATION_FILE_BYTES {
                return Err(WorkerError::state(
                    "encoded signal observation exceeds the bounded spool file envelope",
                ));
            }
            let actual = actual_by_class
                .entry(spool_class(&observation.kind))
                .or_default();
            actual.0 = actual.0.saturating_add(1);
            actual.1 = actual.1.saturating_add(encoded_bytes);
        }
        let mut actual_files = 0_u64;
        let mut actual_bytes = 0_u64;
        for (class, (files, bytes)) in &actual_by_class {
            if *files > projected_by_class.get(class).copied().unwrap_or(0) {
                return Err(WorkerError::state(
                    "spool class preflight underestimated an emitted observation batch",
                ));
            }
            let inventory = self.spool_classes.get(*class);
            let (file_cap, byte_cap) = spool_class_caps(class);
            if inventory.map_or(0, |row| row.files).saturating_add(*files) > file_cap
                || inventory.map_or(0, |row| row.bytes).saturating_add(*bytes) > byte_cap
            {
                return Err(WorkerError::state(
                    "spool class crossed its advertised hard quota",
                ));
            }
            actual_files = actual_files.saturating_add(*files);
            actual_bytes = actual_bytes.saturating_add(*bytes);
        }
        if self.spool_files.saturating_add(actual_files) > MAX_SPOOL_FILES
            || self.spool_bytes.saturating_add(actual_bytes) > MAX_SPOOL_BYTES
        {
            return Err(WorkerError::state(
                "signal spool crossed its advertised hard quota",
            ));
        }
        let entry = InputJournalEntry {
            schema_version: SCHEMA_VERSION,
            replay_config: self.worker.config.clone(),
            events,
            suppressed_output_kinds,
            observation_json: observation_json.clone(),
        };
        let entry_bytes = json_size(&entry)?;
        let projected_bytes = self
            .journal
            .len()?
            .saturating_add(entry_bytes)
            .saturating_add(1);
        if entry_bytes > MAX_INPUT_JOURNAL_ENTRY_BYTES as u64
            || projected_bytes > MAX_INPUT_JOURNAL_BYTES
            || self.journal_entries_retained.saturating_add(1) > MAX_INPUT_JOURNAL_ENTRIES
        {
            self.compact_candidate_checkpoint(&candidate, &observation_json)?;
            self.worker = candidate;
        } else {
            self.journal.append(&entry)?;
            self.worker = candidate;
            self.journal_entries_retained = self.journal_entries_retained.saturating_add(1);
            self.publication_pending = true;
            for json in &observation_json {
                self.record_spool_write(json)?;
            }
            self.publication_pending = false;
        }
        Ok(observations)
    }

    fn prepare_event_batch(
        &self,
        events: &[WireEvent],
        suppressed: BTreeSet<&'static str>,
    ) -> Result<(SignalWorker, Vec<NormalizedObservation>), WorkerError> {
        let mut candidate = self.worker.clone();
        candidate.set_suppressed_output_kinds(suppressed);
        let mut observations = Vec::new();
        for event in events {
            observations.extend(candidate.apply(event.clone())?);
        }
        candidate.set_suppressed_output_kinds(BTreeSet::new());
        Ok((candidate, observations))
    }

    pub fn durability_metrics(&self) -> Result<DurabilityMetrics, WorkerError> {
        let mut spool_class_files = BTreeMap::new();
        let mut spool_class_bytes = BTreeMap::new();
        let mut spool_class_file_caps = BTreeMap::new();
        let mut spool_class_byte_caps = BTreeMap::new();
        let mut spool_class_byte_soft_thresholds = BTreeMap::new();
        for class in ["current", "lifecycle", "catchup", "other"] {
            let inventory = self.spool_classes.get(class);
            spool_class_files.insert(class.to_owned(), inventory.map_or(0, |row| row.files));
            spool_class_bytes.insert(class.to_owned(), inventory.map_or(0, |row| row.bytes));
            let (file_cap, byte_cap) = spool_class_caps(class);
            spool_class_file_caps.insert(class.to_owned(), file_cap);
            spool_class_byte_caps.insert(class.to_owned(), byte_cap);
            spool_class_byte_soft_thresholds
                .insert(class.to_owned(), spool_class_byte_soft_threshold(class));
        }
        Ok(DurabilityMetrics {
            checkpoint_bytes: self.checkpoint.len()?,
            journal_bytes: self.journal.len()?,
            checkpoint_writes_session: self.checkpoint_writes_session,
            journal_entries_retained: self.journal_entries_retained,
            spool_files: self.spool_files,
            spool_bytes: self.spool_bytes,
            spool_file_cap: MAX_SPOOL_FILES,
            spool_byte_cap: MAX_SPOOL_BYTES,
            spool_byte_soft_threshold: SPOOL_BYTE_SOFT_THRESHOLD,
            replaceable_outputs_coalesced: self.replaceable_outputs_coalesced,
            spool_backpressured: self.spool_backpressured,
            spool_class_files,
            spool_class_bytes,
            spool_class_file_caps,
            spool_class_byte_caps,
            spool_class_byte_soft_thresholds,
            spool_backpressured_classes: self.spool_backpressured_classes.iter().cloned().collect(),
        })
    }

    pub fn spool_backpressured_for(&self, class: &str) -> bool {
        self.spool_backpressured || self.spool_backpressured_classes.contains(class)
    }

    pub fn refresh_spool_backpressure(&mut self) -> Result<(), WorkerError> {
        self.refresh_spool_inventory_if_needed()
    }

    fn projected_spool_files(&self, event: &WireEvent) -> BTreeMap<&'static str, u64> {
        let mut projected = BTreeMap::new();
        match event {
            WireEvent::BybitFundingBatch { emit_lifecycle, .. } => {
                if *emit_lifecycle && self.worker.state.last_carry_decision_ts_ms.is_some() {
                    projected.insert("lifecycle", 1);
                }
            }
            WireEvent::BybitTickerSnapshot { .. } => {
                if !self
                    .pending_replaceable_paths
                    .contains_key("market_snapshot")
                {
                    projected.insert("current", 1);
                }
            }
            WireEvent::LongWatermark { .. } => {
                if !self
                    .pending_replaceable_paths
                    .contains_key("long_feature_batch")
                {
                    projected.insert("current", 1);
                }
            }
            WireEvent::CarryWatermark {
                data_through_ms, ..
            } => {
                let decision_ts_ms = carry_decision_at(
                    *data_through_ms,
                    self.worker.config.carry.decision_phase_ms,
                    self.worker.config.carry.decision_kline_lag_ms,
                );
                let scorer_is_behind = decision_ts_ms.is_some_and(|decision| {
                    self.worker
                        .state
                        .last_carry_scorer_ts_ms
                        .or(self.worker.state.last_carry_decision_ts_ms)
                        < Some(decision)
                });
                if self
                    .pending_replaceable_paths
                    .contains_key("carry_feature_batch")
                    && scorer_is_behind
                {
                    projected.insert("catchup", 1);
                    if !self.pending_replaceable_paths.contains_key("readiness") {
                        projected.insert("current", 1);
                    }
                } else if !self
                    .pending_replaceable_paths
                    .contains_key("carry_feature_batch")
                    || !self.pending_replaceable_paths.contains_key("readiness")
                {
                    projected.insert("current", 1);
                }
            }
            WireEvent::CarryScorerCatchupWatermark {
                decision_through_ms,
                ..
            } => {
                let files = self
                    .worker
                    .state
                    .last_carry_scorer_ts_ms
                    .or(self.worker.state.last_carry_decision_ts_ms)
                    .map(|last| {
                        decision_through_ms
                            .saturating_sub(last)
                            .saturating_div(DAY_MS)
                            .clamp(0, MAX_CARRY_SCORER_CATCHUP_DAYS)
                    })
                    .unwrap_or(MAX_CARRY_SCORER_CATCHUP_DAYS);
                if files > 0 {
                    projected.insert(
                        "catchup",
                        u64::try_from(files).unwrap_or(MAX_CARRY_SCORER_CATCHUP_DAYS as u64),
                    );
                }
            }
            WireEvent::LlmGateCandidates { .. } => {
                projected.insert("current", 1);
            }
            WireEvent::Watermark { observed_ts_ms, .. } => {
                let long_files = u64::from(
                    !self
                        .pending_replaceable_paths
                        .contains_key("long_feature_batch"),
                );
                let carry_files = u64::from(
                    !self
                        .pending_replaceable_paths
                        .contains_key("carry_feature_batch"),
                );
                let readiness_files =
                    u64::from(!self.pending_replaceable_paths.contains_key("readiness"));
                let current_files = long_files.saturating_add(carry_files).max(readiness_files);
                if current_files > 0 {
                    projected.insert("current", current_files);
                }
                let decision_ts_ms = carry_decision_at(
                    *observed_ts_ms,
                    self.worker.config.carry.decision_phase_ms,
                    self.worker.config.carry.decision_kline_lag_ms,
                );
                if self
                    .pending_replaceable_paths
                    .contains_key("carry_feature_batch")
                    && decision_ts_ms.is_some_and(|decision| {
                        self.worker
                            .state
                            .last_carry_scorer_ts_ms
                            .or(self.worker.state.last_carry_decision_ts_ms)
                            < Some(decision)
                    })
                {
                    projected.insert("catchup", 1);
                }
            }
            _ => {}
        }
        projected
    }

    fn refresh_spool_inventory_if_needed(&mut self) -> Result<(), WorkerError> {
        let replaceable_drained = self
            .pending_replaceable_paths
            .values()
            .any(|path| !path.exists());
        let class_sentinel_drained = self.spool_classes.values().any(|inventory| {
            inventory
                .oldest_path
                .as_ref()
                .is_some_and(|path| !path.exists())
        });
        if replaceable_drained || class_sentinel_drained {
            let inventory = self.spool.inventory()?;
            self.spool_files = inventory.files;
            self.spool_bytes = inventory.bytes;
            self.pending_replaceable_paths = inventory.replaceable_paths;
            self.spool_classes = inventory.classes;
            self.spool_backpressured = false;
            self.spool_backpressured_classes.retain(|class| {
                let inventory = self.spool_classes.get(class);
                let (file_cap, byte_cap) = spool_class_caps(class);
                let byte_soft_threshold = spool_class_byte_soft_threshold(class);
                inventory.is_some_and(|row| {
                    row.files >= file_cap
                        || row.bytes >= byte_cap
                        || row.bytes >= byte_soft_threshold
                })
            });
        }
        Ok(())
    }

    fn record_spool_write(&mut self, json: &str) -> Result<(), WorkerError> {
        let (path, observation) = self.spool.write_encoded_observation(json.as_bytes())?;
        self.spool_files = self.spool_files.saturating_add(1);
        self.spool_bytes = self
            .spool_bytes
            .saturating_add(u64::try_from(json.len()).unwrap_or(u64::MAX));
        let class = spool_class(&observation.kind).to_owned();
        let inventory = self.spool_classes.entry(class).or_default();
        inventory.files = inventory.files.saturating_add(1);
        inventory.bytes = inventory
            .bytes
            .saturating_add(u64::try_from(json.len()).unwrap_or(u64::MAX));
        if inventory
            .oldest_path
            .as_ref()
            .is_none_or(|oldest| path < *oldest)
        {
            inventory.oldest_path = Some(path.clone());
        }
        if inventory
            .newest_path
            .as_ref()
            .is_none_or(|newest| path > *newest)
        {
            inventory.newest_path = Some(path.clone());
        }
        if matches!(
            observation.kind.as_str(),
            "market_snapshot" | "readiness" | "long_feature_batch" | "carry_feature_batch"
        ) {
            self.pending_replaceable_paths
                .insert(observation.kind, path);
        }
        Ok(())
    }

    fn compact_if_due(&mut self) -> Result<(), WorkerError> {
        if self.journal_entries_retained == 0 {
            return Ok(());
        }
        let checkpoint_old = self
            .checkpoint
            .age()?
            .is_some_and(|age| age.as_secs() >= MAX_CHECKPOINT_AGE_SECS);
        if self.journal_entries_retained >= MAX_INPUT_JOURNAL_ENTRIES
            || self.journal.len()? >= MAX_INPUT_JOURNAL_BYTES
            || checkpoint_old
        {
            self.compact_current_checkpoint(&[])?;
        }
        Ok(())
    }

    fn compact_current_checkpoint(
        &mut self,
        observation_json: &[String],
    ) -> Result<(), WorkerError> {
        let candidate = self.worker.clone();
        self.compact_candidate_checkpoint(&candidate, observation_json)
    }

    fn compact_candidate_checkpoint(
        &mut self,
        candidate: &SignalWorker,
        observation_json: &[String],
    ) -> Result<(), WorkerError> {
        self.pending_next.save(candidate.state())?;
        let next_state_sha256 = self
            .pending_next
            .sha256()?
            .ok_or_else(|| WorkerError::state("pending next state disappeared"))?;
        let transaction = PendingTransaction {
            schema_version: SCHEMA_VERSION,
            prior_state_sha256: self.checkpoint_sha256.clone(),
            next_state_sha256: next_state_sha256.clone(),
            observation_json: observation_json.to_vec(),
        };
        self.pending.save(&transaction)?;
        for json in observation_json {
            self.record_spool_write(json)?;
        }
        self.checkpoint.replace_from(&self.pending_next)?;
        self.checkpoint_writes_session = self.checkpoint_writes_session.saturating_add(1);
        self.pending.remove()?;
        self.journal.remove()?;
        self.journal_entries_retained = 0;
        self.checkpoint_sha256 = next_state_sha256;
        Ok(())
    }
}

fn finish_pending(
    checkpoint: &AtomicJsonStore,
    pending_store: &AtomicJsonStore,
    pending_next: &AtomicJsonStore,
    spool: &SpoolWriter,
    transaction: &PendingTransaction,
) -> Result<(), WorkerError> {
    if transaction.schema_version != SCHEMA_VERSION {
        return Err(WorkerError::state(
            "pending transaction schema or next-state hash is invalid",
        ));
    }
    let current_hash = checkpoint
        .sha256()?
        .ok_or_else(|| WorkerError::state("pending transaction has no checkpoint"))?;
    if current_hash != transaction.prior_state_sha256
        && current_hash != transaction.next_state_sha256
    {
        return Err(WorkerError::state(
            "pending transaction does not follow the durable checkpoint",
        ));
    }
    if current_hash == transaction.prior_state_sha256 {
        let next_hash = pending_next
            .sha256()?
            .ok_or_else(|| WorkerError::state("pending transaction has no next state"))?;
        if next_hash != transaction.next_state_sha256 {
            return Err(WorkerError::state(
                "pending transaction schema or next-state hash is invalid",
            ));
        }
    }
    for json in &transaction.observation_json {
        spool.write_encoded(json.as_bytes())?;
    }
    if current_hash == transaction.prior_state_sha256 {
        checkpoint.replace_from(pending_next)?;
    } else {
        pending_next.remove()?;
    }
    pending_store.remove()?;
    Ok(())
}

fn validate_hot_journal_entry(entry: &InputJournalEntry) -> Result<(), WorkerError> {
    if entry.schema_version != SCHEMA_VERSION || entry.events.is_empty() {
        return Err(WorkerError::state("input journal entry schema is invalid"));
    }
    let suppressed = entry
        .suppressed_output_kinds
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    if suppressed.len() != entry.suppressed_output_kinds.len()
        || suppressed.iter().any(|kind| {
            !matches!(
                *kind,
                "market_snapshot" | "readiness" | "long_feature_batch" | "carry_feature_batch"
            )
        })
    {
        return Err(WorkerError::state(
            "input journal suppression set is invalid",
        ));
    }
    let mut expected = entry.events[0].sequence();
    for event in &entry.events {
        if event.schema_version() != SCHEMA_VERSION || event.sequence() != expected {
            return Err(WorkerError::state(
                "input journal entry is not a contiguous source batch",
            ));
        }
        expected = expected
            .checked_add(1)
            .ok_or_else(|| WorkerError::state("input journal sequence is exhausted"))?;
    }
    Ok(())
}

fn event_may_emit(event: &WireEvent) -> bool {
    matches!(
        event,
        WireEvent::BybitFundingBatch { .. }
            | WireEvent::BybitTickerSnapshot { .. }
            | WireEvent::UniverseSnapshot { .. }
            | WireEvent::LlmGateCandidates { .. }
            | WireEvent::Watermark { .. }
            | WireEvent::LongWatermark { .. }
            | WireEvent::CarryWatermark { .. }
            | WireEvent::CarryScorerCatchupWatermark { .. }
    )
}

pub(crate) fn spool_class_caps(class: &str) -> (u64, u64) {
    match class {
        "current" => (CURRENT_SPOOL_FILE_CAP, CURRENT_SPOOL_BYTE_CAP),
        "lifecycle" => (LIFECYCLE_SPOOL_FILE_CAP, LIFECYCLE_SPOOL_BYTE_CAP),
        "catchup" => (CATCHUP_SPOOL_FILE_CAP, CATCHUP_SPOOL_BYTE_CAP),
        _ => (OTHER_SPOOL_FILE_CAP, OTHER_SPOOL_BYTE_CAP),
    }
}

fn spool_class_byte_soft_threshold(class: &str) -> u64 {
    match class {
        "current" => CURRENT_SPOOL_BYTE_SOFT_THRESHOLD,
        "lifecycle" => LIFECYCLE_SPOOL_BYTE_SOFT_THRESHOLD,
        "catchup" => CATCHUP_SPOOL_BYTE_SOFT_THRESHOLD,
        _ => OTHER_SPOOL_BYTE_SOFT_THRESHOLD,
    }
}

fn carry_decision_at(
    observed_ts_ms: i64,
    decision_phase_ms: i64,
    decision_kline_lag_ms: i64,
) -> Option<i64> {
    let day = observed_ts_ms.saturating_sub(observed_ts_ms.rem_euclid(DAY_MS));
    let mut decision_ts_ms = day.saturating_add(decision_phase_ms);
    if observed_ts_ms < decision_ts_ms.saturating_add(decision_kline_lag_ms) {
        decision_ts_ms = decision_ts_ms.saturating_sub(DAY_MS);
    }
    (decision_ts_ms > 0).then_some(decision_ts_ms)
}

fn encode_observations(observations: &[NormalizedObservation]) -> Result<Vec<String>, WorkerError> {
    observations
        .iter()
        .map(|observation| {
            serde_json::to_string(observation)
                .map_err(|error| WorkerError::json("encode pending observation", error))
        })
        .collect()
}

#[cfg(test)]
mod tests;
