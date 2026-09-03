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
use crate::model::{
    BinanceWhaleObservation, BootstrapCoverage, CoverageInterval, DataRejection, HourlyKline,
    InstrumentObservation, InstrumentTradingInterval, MarketMark, NormalizedObservation,
    ObservationPayload, PresettlementPublicObservation, Readiness, SettledFunding,
    SignalPayloadEnvelope, TickerObservation, UniverseIdentity, WireEvent,
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

#[derive(Debug)]
pub struct WorkerError {
    category: &'static str,
    message: String,
}

impl WorkerError {
    pub fn config(message: impl Into<String>) -> Self {
        Self::new("config", message)
    }

    pub fn input(message: impl Into<String>) -> Self {
        Self::new("input", message)
    }

    pub fn state(message: impl Into<String>) -> Self {
        Self::new("state", message)
    }

    pub fn network(message: impl Into<String>) -> Self {
        Self::new("network", message)
    }

    pub fn io(context: &'static str, error: std::io::Error) -> Self {
        Self::new("io", format!("{context}: {error}"))
    }

    pub fn json(context: &'static str, error: serde_json::Error) -> Self {
        Self::new("json", format!("{context}: {error}"))
    }

    pub(crate) fn is_lane_local_source_failure(&self) -> bool {
        matches!(self.category, "input" | "network")
    }

    fn new(category: &'static str, message: impl Into<String>) -> Self {
        Self {
            category,
            message: message.into(),
        }
    }
}

impl fmt::Display for WorkerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.category, self.message)
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
    fn new(
        config: &SignalWorkerConfig,
        universe: UniverseIdentity,
        source_generation: String,
    ) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            config: config.identity.clone(),
            source_generation,
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
        let legacy_symbols = state
            .kline_checked_from_ms
            .keys()
            .chain(state.kline_checked_through_ms.keys())
            .cloned()
            .collect::<BTreeSet<_>>();
        if legacy_symbols.iter().any(|symbol| {
            state.kline_checked_from_ms.contains_key(symbol)
                != state.kline_checked_through_ms.contains_key(symbol)
        }) {
            return Err(WorkerError::state(
                "checkpoint kline coverage has only one legacy boundary",
            ));
        }
        if state.kline_coverage_intervals.is_empty() {
            for (symbol, checked_from_ms) in &state.kline_checked_from_ms {
                if let Some(checked_through_ms) =
                    state.kline_checked_through_ms.get(symbol).copied()
                {
                    state.kline_coverage_intervals.insert(
                        symbol.clone(),
                        vec![CoverageInterval {
                            checked_from_ms: *checked_from_ms,
                            checked_through_ms,
                        }],
                    );
                }
            }
        }
        validate_kline_coverage_intervals(&mut state, &config.long.regime_symbol)?;
        let carry_symbols = state
            .universe
            .carry_symbols
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        restore_source_coverage_intervals(
            &mut state.funding_checked_from_ms,
            &mut state.funding_checked_through_ms,
            &mut state.funding_coverage_intervals,
            &carry_symbols,
            "funding",
        )?;
        restore_source_coverage_intervals(
            &mut state.whale_checked_from_ms,
            &mut state.whale_checked_through_ms,
            &mut state.whale_coverage_intervals,
            &carry_symbols,
            "whale",
        )?;
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
                let normalized = normalize_kline_rows(&symbol, available_at_ms, &rows)?;
                for row in normalized {
                    merge_kline(
                        self.state.klines.entry(row.symbol.clone()).or_default(),
                        row,
                    )?;
                }
                if replace_coverage {
                    self.state.kline_checked_from_ms.remove(&symbol);
                    self.state.kline_checked_through_ms.remove(&symbol);
                    self.state.kline_coverage_intervals.remove(&symbol);
                }
                if checked_from_ms.is_some() != checked_through_ms.is_some() {
                    return Err(WorkerError::input(
                        "kline coverage frontier has only one boundary",
                    ));
                }
                if let (Some(checked_from_ms), Some(checked_through_ms)) =
                    (checked_from_ms, checked_through_ms)
                {
                    if checked_from_ms <= 0
                        || checked_from_ms % HOUR_MS != 0
                        || checked_through_ms % HOUR_MS != 0
                        || checked_from_ms >= checked_through_ms
                        || checked_through_ms > available_at_ms
                    {
                        return Err(WorkerError::input(
                            "kline coverage frontier has an invalid clock",
                        ));
                    }
                    merge_coverage_interval(
                        self.state
                            .kline_coverage_intervals
                            .entry(symbol.clone())
                            .or_default(),
                        CoverageInterval {
                            checked_from_ms,
                            checked_through_ms,
                        },
                    );
                    sync_legacy_kline_coverage(&mut self.state, &symbol);
                }
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
                let prune_clock_ms = self.state.last_observed_ts_ms;
                self.prune(prune_clock_ms);
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
                let normalized = normalize_funding_rows(&symbol, available_at_ms, &rows)?;
                let mut inserted = Vec::new();
                for row in normalized {
                    if merge_funding(
                        self.state.funding.entry(row.symbol.clone()).or_default(),
                        row.clone(),
                    )? {
                        inserted.push(row);
                    }
                }
                if emit_lifecycle
                    && !inserted.is_empty()
                    && self.state.last_carry_decision_ts_ms.is_some()
                {
                    let decision_ts_ms = self
                        .state
                        .last_carry_decision_ts_ms
                        .expect("checked CARRY decision cursor");
                    let lifecycle_rows = inserted
                        .into_iter()
                        .filter(|row| {
                            row.settlement_ts_ms > decision_ts_ms
                                && row.settlement_ts_ms <= available_at_ms
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
                merge_source_coverage(
                    &mut self.state.funding_checked_from_ms,
                    &mut self.state.funding_checked_through_ms,
                    &mut self.state.funding_coverage_intervals,
                    &symbol,
                    checked_from_ms,
                    checked_through_ms,
                    available_at_ms,
                    replace_coverage,
                    "funding",
                )?;
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
                let prune_clock_ms = self.state.last_observed_ts_ms;
                self.prune(prune_clock_ms);
            }
            WireEvent::BybitInstrumentSnapshot {
                observed_ts_ms,
                available_at_ms,
                rows,
                ..
            } => {
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
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
                let prune_clock_ms = self.state.last_observed_ts_ms;
                self.prune(prune_clock_ms);
            }
            WireEvent::BybitTickerSnapshot {
                observed_ts_ms,
                available_at_ms,
                rows,
                ..
            } => {
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
                    let expires_at_ms = oldest_actionable_clock_ms
                        .saturating_add(self.config.sources.mark_max_age_ms);
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
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
            }
            WireEvent::BinanceWhaleBatch {
                available_at_ms,
                coverage,
                rows,
                ..
            } => {
                for row in normalize_whales(available_at_ms, &rows)? {
                    merge_whale(
                        self.state.whales.entry(row.symbol.clone()).or_default(),
                        row,
                    )?;
                }
                for item in coverage {
                    if item.replace_coverage {
                        self.state.whale_coverage_intervals.remove(&item.symbol);
                    }
                    merge_source_coverage(
                        &mut self.state.whale_checked_from_ms,
                        &mut self.state.whale_checked_through_ms,
                        &mut self.state.whale_coverage_intervals,
                        &item.symbol,
                        Some(item.checked_from_ms),
                        Some(item.checked_through_ms),
                        available_at_ms,
                        item.replace_coverage,
                        "whale",
                    )?;
                }
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
                let prune_clock_ms = self.state.last_observed_ts_ms;
                self.prune(prune_clock_ms);
            }
            WireEvent::UniverseSnapshot { universe, .. } => {
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
            }
            WireEvent::LlmGateCandidates {
                observed_ts_ms,
                available_at_ms,
                decision_ts_ms,
                valid_until_ms,
                rows,
                ..
            } => {
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
                    let symbols: Vec<String> =
                        accepted.iter().map(|row| row.symbol.clone()).collect();
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
            }
            WireEvent::BootstrapComplete { coverage, .. } => {
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
        make_observation(
            &self.config,
            &self.state.universe,
            &self.state.source_generation,
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
        make_observation(
            &self.config,
            &self.state.universe,
            &self.state.source_generation,
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
            .kline_checked_from_ms
            .retain(|symbol, _| symbols.contains(symbol));
        self.state
            .kline_checked_through_ms
            .retain(|symbol, _| symbols.contains(symbol));
        self.state
            .kline_coverage_intervals
            .retain(|symbol, _| symbols.contains(symbol));
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
            if let Some(intervals) = self.state.kline_coverage_intervals.get_mut(&symbol) {
                retain_coverage_windows(intervals, &windows);
            }
            sync_legacy_kline_coverage(&mut self.state, &symbol);
        }
        self.state
            .kline_coverage_intervals
            .retain(|_, intervals| !intervals.is_empty());
        let funding_hours = required_carry_history_hours(&self.config, &self.state);
        let funding_cutoff =
            carry_retained_through_ms.saturating_sub(funding_hours.saturating_mul(HOUR_MS));
        self.state
            .funding_checked_from_ms
            .retain(|symbol, _| carry_symbols.contains(symbol));
        self.state
            .funding_checked_through_ms
            .retain(|symbol, _| carry_symbols.contains(symbol));
        self.state
            .funding_coverage_intervals
            .retain(|symbol, _| carry_symbols.contains(symbol));
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
            if let Some(intervals) = self.state.funding_coverage_intervals.get_mut(symbol) {
                retain_coverage_windows(intervals, &funding_windows);
            }
            sync_legacy_source_coverage(
                &mut self.state.funding_checked_from_ms,
                &mut self.state.funding_checked_through_ms,
                &self.state.funding_coverage_intervals,
                symbol,
            );
        }
        self.state
            .funding_coverage_intervals
            .retain(|_, intervals| !intervals.is_empty());
        let whale_cutoff = carry_retained_through_ms.saturating_sub(
            (self.config.carry.whale_change_lookback_hours
                + self.config.carry.whale_freshness_hours
                + 24)
                .saturating_mul(HOUR_MS),
        );
        self.state
            .whale_checked_from_ms
            .retain(|symbol, _| carry_symbols.contains(symbol));
        self.state
            .whale_checked_through_ms
            .retain(|symbol, _| carry_symbols.contains(symbol));
        self.state
            .whale_coverage_intervals
            .retain(|symbol, _| carry_symbols.contains(symbol));
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
            if let Some(intervals) = self.state.whale_coverage_intervals.get_mut(symbol) {
                retain_coverage_windows(intervals, &whale_windows);
            }
            sync_legacy_source_coverage(
                &mut self.state.whale_checked_from_ms,
                &mut self.state.whale_checked_through_ms,
                &self.state.whale_coverage_intervals,
                symbol,
            );
        }
        self.state
            .whale_coverage_intervals
            .retain(|_, intervals| !intervals.is_empty());
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

#[cfg(test)]
fn insert_exact<T: PartialEq>(
    rows: &mut BTreeMap<i64, T>,
    key: i64,
    value: T,
    label: &str,
) -> Result<(), WorkerError> {
    if let Some(existing) = rows.get(&key) {
        if existing == &value {
            return Ok(());
        }
        return Err(WorkerError::input(format!(
            "{label} history rewrote timestamp {key}"
        )));
    }
    rows.insert(key, value);
    Ok(())
}

fn merge_kline(rows: &mut BTreeMap<i64, HourlyKline>, row: HourlyKline) -> Result<(), WorkerError> {
    let key = row.open_ts_ms;
    if let Some(existing) = rows.get_mut(&key) {
        let same = existing.symbol == row.symbol
            && existing.open_ts_ms == row.open_ts_ms
            && existing.open == row.open
            && existing.high == row.high
            && existing.low == row.low
            && existing.close == row.close
            && existing.volume_base == row.volume_base
            && existing.turnover_quote == row.turnover_quote;
        if !same {
            return Err(WorkerError::input(format!(
                "kline history rewrote timestamp {key}"
            )));
        }
        existing.available_at_ms = existing.available_at_ms.min(row.available_at_ms);
        return Ok(());
    }
    rows.insert(key, row);
    Ok(())
}

fn merge_funding(
    rows: &mut BTreeMap<i64, SettledFunding>,
    row: SettledFunding,
) -> Result<bool, WorkerError> {
    let key = row.settlement_ts_ms;
    if let Some(existing) = rows.get_mut(&key) {
        let same = existing.symbol == row.symbol
            && existing.settlement_ts_ms == row.settlement_ts_ms
            && existing.rate == row.rate
            && existing.funding_interval_min == row.funding_interval_min;
        if !same {
            return Err(WorkerError::input(format!(
                "funding history rewrote timestamp {key}"
            )));
        }
        existing.available_at_ms = existing.available_at_ms.min(row.available_at_ms);
        return Ok(false);
    }
    rows.insert(key, row);
    Ok(true)
}

fn merge_whale(
    rows: &mut BTreeMap<i64, BinanceWhaleObservation>,
    row: BinanceWhaleObservation,
) -> Result<(), WorkerError> {
    let key = row.day_end_ms;
    if let Some(existing) = rows.get_mut(&key) {
        let same = existing.symbol == row.symbol
            && existing.day_end_ms == row.day_end_ms
            && existing.long_short_ratio == row.long_short_ratio;
        if !same {
            return Err(WorkerError::input(format!(
                "whale history rewrote timestamp {key}"
            )));
        }
        existing.available_at_ms = existing.available_at_ms.min(row.available_at_ms);
        return Ok(());
    }
    rows.insert(key, row);
    Ok(())
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

fn merge_coverage_interval(intervals: &mut Vec<CoverageInterval>, incoming: CoverageInterval) {
    intervals.push(incoming);
    intervals.sort_by_key(|interval| interval.checked_from_ms);
    let mut merged = Vec::<CoverageInterval>::with_capacity(intervals.len());
    for interval in intervals.drain(..) {
        if let Some(last) = merged.last_mut() {
            if interval.checked_from_ms <= last.checked_through_ms {
                last.checked_through_ms = last.checked_through_ms.max(interval.checked_through_ms);
                continue;
            }
        }
        merged.push(interval);
    }
    *intervals = merged;
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

fn retain_coverage_windows(intervals: &mut Vec<CoverageInterval>, windows: &[(i64, i64)]) {
    let mut retained = Vec::new();
    for interval in intervals.iter() {
        for (from, through) in windows {
            let checked_from_ms = interval.checked_from_ms.max(*from);
            let checked_through_ms = interval.checked_through_ms.min(*through);
            if checked_from_ms < checked_through_ms {
                merge_coverage_interval(
                    &mut retained,
                    CoverageInterval {
                        checked_from_ms,
                        checked_through_ms,
                    },
                );
            }
        }
    }
    *intervals = retained;
}

fn validate_kline_coverage_intervals(
    state: &mut WorkerState,
    regime_symbol: &str,
) -> Result<(), WorkerError> {
    let mut allowed = state
        .universe
        .long_symbols
        .iter()
        .chain(&state.universe.carry_symbols)
        .cloned()
        .collect::<BTreeSet<_>>();
    allowed.insert("BTCUSDT".to_owned());
    allowed.insert("ETHUSDT".to_owned());
    allowed.insert(regime_symbol.to_owned());
    for (symbol, intervals) in &mut state.kline_coverage_intervals {
        if !allowed.contains(symbol) || intervals.is_empty() {
            return Err(WorkerError::state(
                "checkpoint kline coverage cardinality is invalid",
            ));
        }
        let mut prior_through = None;
        for interval in intervals.iter() {
            if interval.checked_from_ms <= 0
                || interval.checked_from_ms % HOUR_MS != 0
                || interval.checked_through_ms % HOUR_MS != 0
                || interval.checked_from_ms >= interval.checked_through_ms
                || prior_through.is_some_and(|through| through >= interval.checked_from_ms)
            {
                return Err(WorkerError::state(
                    "checkpoint kline coverage intervals are not canonical",
                ));
            }
            prior_through = Some(interval.checked_through_ms);
        }
    }
    state.kline_checked_from_ms.clear();
    state.kline_checked_through_ms.clear();
    let symbols = state
        .kline_coverage_intervals
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    for symbol in symbols {
        sync_legacy_kline_coverage(state, &symbol);
    }
    Ok(())
}

fn restore_source_coverage_intervals(
    checked_from: &mut BTreeMap<String, i64>,
    checked_through: &mut BTreeMap<String, i64>,
    intervals_by_symbol: &mut BTreeMap<String, Vec<CoverageInterval>>,
    allowed: &BTreeSet<String>,
    label: &str,
) -> Result<(), WorkerError> {
    let legacy_symbols = checked_from
        .keys()
        .chain(checked_through.keys())
        .cloned()
        .collect::<BTreeSet<_>>();
    if legacy_symbols
        .iter()
        .any(|symbol| checked_from.contains_key(symbol) != checked_through.contains_key(symbol))
    {
        return Err(WorkerError::state(format!(
            "checkpoint {label} coverage has only one boundary"
        )));
    }
    if intervals_by_symbol.is_empty() {
        for symbol in legacy_symbols {
            intervals_by_symbol.insert(
                symbol.clone(),
                vec![CoverageInterval {
                    checked_from_ms: checked_from[&symbol],
                    checked_through_ms: checked_through[&symbol],
                }],
            );
        }
    }
    for (symbol, intervals) in intervals_by_symbol.iter_mut() {
        if !allowed.contains(symbol) || intervals.is_empty() {
            return Err(WorkerError::state(format!(
                "checkpoint {label} coverage cardinality is invalid"
            )));
        }
        let mut prior_through = None;
        for interval in intervals.iter() {
            if interval.checked_from_ms <= 0
                || interval.checked_from_ms % HOUR_MS != 0
                || interval.checked_through_ms % HOUR_MS != 0
                || interval.checked_from_ms >= interval.checked_through_ms
                || prior_through.is_some_and(|through| through >= interval.checked_from_ms)
            {
                return Err(WorkerError::state(format!(
                    "checkpoint {label} coverage intervals are not canonical"
                )));
            }
            prior_through = Some(interval.checked_through_ms);
        }
    }
    checked_from.clear();
    checked_through.clear();
    for (symbol, intervals) in intervals_by_symbol.iter() {
        if let [interval] = intervals.as_slice() {
            checked_from.insert(symbol.clone(), interval.checked_from_ms);
            checked_through.insert(symbol.clone(), interval.checked_through_ms);
        }
    }
    Ok(())
}

fn sync_legacy_kline_coverage(state: &mut WorkerState, symbol: &str) {
    match state
        .kline_coverage_intervals
        .get(symbol)
        .map(Vec::as_slice)
    {
        Some([interval]) => {
            state
                .kline_checked_from_ms
                .insert(symbol.to_owned(), interval.checked_from_ms);
            state
                .kline_checked_through_ms
                .insert(symbol.to_owned(), interval.checked_through_ms);
        }
        _ => {
            state.kline_checked_from_ms.remove(symbol);
            state.kline_checked_through_ms.remove(symbol);
        }
    }
}

fn sync_legacy_source_coverage(
    checked_from: &mut BTreeMap<String, i64>,
    checked_through: &mut BTreeMap<String, i64>,
    intervals_by_symbol: &BTreeMap<String, Vec<CoverageInterval>>,
    symbol: &str,
) {
    match intervals_by_symbol.get(symbol).map(Vec::as_slice) {
        Some([interval]) => {
            checked_from.insert(symbol.to_owned(), interval.checked_from_ms);
            checked_through.insert(symbol.to_owned(), interval.checked_through_ms);
        }
        _ => {
            checked_from.remove(symbol);
            checked_through.remove(symbol);
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn merge_source_coverage(
    checked_from: &mut BTreeMap<String, i64>,
    checked_through: &mut BTreeMap<String, i64>,
    intervals_by_symbol: &mut BTreeMap<String, Vec<CoverageInterval>>,
    symbol: &str,
    new_from: Option<i64>,
    new_through: Option<i64>,
    available_at_ms: i64,
    replace_coverage: bool,
    label: &str,
) -> Result<(), WorkerError> {
    if new_from.is_some() != new_through.is_some() {
        return Err(WorkerError::input(format!(
            "{label} coverage frontier has only one boundary"
        )));
    }
    let (Some(new_from), Some(new_through)) = (new_from, new_through) else {
        return Ok(());
    };
    if new_from <= 0
        || new_from % HOUR_MS != 0
        || new_through % HOUR_MS != 0
        || new_from >= new_through
        || new_through > available_at_ms
    {
        return Err(WorkerError::input(format!(
            "{label} coverage frontier has an invalid clock"
        )));
    }
    if replace_coverage {
        intervals_by_symbol.remove(symbol);
    }
    let intervals = intervals_by_symbol.entry(symbol.to_owned()).or_default();
    merge_coverage_interval(
        intervals,
        CoverageInterval {
            checked_from_ms: new_from,
            checked_through_ms: new_through,
        },
    );
    match intervals.as_slice() {
        [interval] => {
            checked_from.insert(symbol.to_owned(), interval.checked_from_ms);
            checked_through.insert(symbol.to_owned(), interval.checked_through_ms);
        }
        _ => {
            checked_from.remove(symbol);
            checked_through.remove(symbol);
        }
    }
    Ok(())
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
    let source = output_source(&config.routing.source, source_generation, long)?;
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
            let regenerated = encode_observations(&observations)?;
            if regenerated != entry.observation_json {
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
        self.worker.set_suppressed_output_kinds(suppressed);
        let apply_result = (|| {
            let mut observations = Vec::new();
            for event in &events {
                observations.extend(self.worker.apply(event.clone())?);
            }
            Ok::<_, WorkerError>(observations)
        })();
        self.worker.set_suppressed_output_kinds(BTreeSet::new());
        let observations = apply_result?;
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
            self.compact_current_checkpoint(&observation_json)?;
        } else {
            self.journal.append(&entry)?;
            self.journal_entries_retained = self.journal_entries_retained.saturating_add(1);
            for json in &observation_json {
                self.record_spool_write(json)?;
            }
        }
        Ok(observations)
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
        self.pending_next.save(self.worker.state())?;
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
mod tests {
    use super::*;
    use crate::config::{
        CarryFeatureConfig, LiveAcquisitionConfig, LongFeatureConfig, SignalRouting, SourceContract,
    };
    use crate::model::{
        BinanceWhaleObservation, BinanceWhaleWire, BybitFundingWire, BybitInstrumentWire,
        BybitTickerWire, Readiness, SourceCoverage, UniverseMode,
    };
    use crate::store::{AtomicJsonStore, SpoolWriter};
    use serde_json::Value;
    use std::collections::BTreeMap;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    #[test]
    fn a_contract_at_its_delivery_clock_is_not_trading_whatever_status_says() {
        let mut row = InstrumentObservation {
            symbol: "BTCUSDT".into(),
            observed_ts_ms: 10 * DAY_MS,
            available_at_ms: 10 * DAY_MS,
            contract_type: Some("LinearPerpetual".into()),
            symbol_type: None,
            status: Some("Trading".into()),
            base_coin: Some("BTC".into()),
            quote_coin: Some("USDT".into()),
            settle_coin: Some("USDT".into()),
            launch_time_ms: Some(1),
            delivery_time_ms: Some(0),
            tick_size: Some(0.1),
            qty_step: Some(0.001),
            min_order_qty: Some(0.001),
            min_notional_value: Some(5.0),
            max_order_qty: None,
            max_market_order_qty: None,
            funding_interval_min: Some(480),
            is_prelisting: false,
        };
        assert!(
            instrument_is_trading(&row, "USDT"),
            "a perpetual's clock is zero"
        );
        row.delivery_time_ms = None;
        assert!(instrument_is_trading(&row, "USDT"));
        row.delivery_time_ms = Some(11 * DAY_MS);
        assert!(
            instrument_is_trading(&row, "USDT"),
            "a dated contract trades until its clock"
        );
        row.delivery_time_ms = Some(10 * DAY_MS);
        assert!(
            !instrument_is_trading(&row, "USDT"),
            "at the clock it has stopped"
        );
        row.delivery_time_ms = Some(9 * DAY_MS);
        assert!(!instrument_is_trading(&row, "USDT"));
    }

    #[test]
    fn directional_symbols_request_quote_and_ticker_exactly_once() {
        let subscriptions = market_subscriptions(&[
            "ZUSDT".to_owned(),
            "BTCUSDT".to_owned(),
            "BTCUSDT".to_owned(),
        ])
        .unwrap();
        assert_eq!(
            subscriptions,
            vec![
                Subscription {
                    symbol: "BTCUSDT".to_owned(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "BTCUSDT".to_owned(),
                    feed: Feed::Ticker,
                },
                Subscription {
                    symbol: "ZUSDT".to_owned(),
                    feed: Feed::Quote,
                },
                Subscription {
                    symbol: "ZUSDT".to_owned(),
                    feed: Feed::Ticker,
                },
            ]
        );
    }

    #[test]
    fn directional_quote_and_ticker_pairs_obey_the_observation_limit() {
        let at_limit = (0..MAX_SIGNAL_SUBSCRIPTIONS / 2)
            .map(|index| format!("S{index:03}USDT"))
            .collect::<Vec<_>>();
        assert_eq!(
            market_subscriptions(&at_limit).unwrap().len(),
            MAX_SIGNAL_SUBSCRIPTIONS
        );

        let over_limit = (0..=MAX_SIGNAL_SUBSCRIPTIONS / 2)
            .map(|index| format!("S{index:03}USDT"))
            .collect::<Vec<_>>();
        let error = market_subscriptions(&over_limit).unwrap_err();
        assert!(error.to_string().contains("quote/ticker subscriptions"));
    }

    fn test_config() -> SignalWorkerConfig {
        SignalWorkerConfig {
            long: LongFeatureConfig {
                profile_name: "v12".into(),
                execution_strategy_id: "long_native_v12_wide_stop".into(),
                exclude_symbols: Vec::new(),
                universe_size: 50,
                universe_volume_window_days: 90,
                min_listing_history_days: 30,
                regime_symbol: "BTCUSDT".into(),
                regime_sma_days: 30,
                vol_estimate_window_days: 30,
                daily_min_hourly_bars: 20,
                cold_start_lookback_days: 100,
                pump_lookback_days: [3, 7],
                atr_window_days: 14,
                atr_min_samples: 7,
                btc_rv_window_days: 30,
                btc_rv_min_samples: 20,
                btc_rv_null_value: 0.8,
                regime_missing_is_on: false,
                median_fallback_to_daily_turnover: true,
            },
            carry: CarryFeatureConfig {
                config_id: "lane2_carry_hold_v7".into(),
                universe_top_n: 100,
                enter_bp: 10.0,
                persistence_window_settlements: Some(20),
                momentum_lookback_hours: 168,
                adv_window_hours: 24,
                return_lookback_hours: 72,
                vol_window_hours: 720,
                vol_return_lag_hours: 24,
                vol_required_finite_samples: 720,
                trail_window_hours: 24,
                trail_change_lookback_hours: 48,
                turn_growth_lookback_hours: 72,
                whale_change_lookback_hours: 72,
                whale_freshness_hours: 48,
                whale_feed_days: 6,
                settlement_age_reset_threshold_hours: 0.5,
                decision_phase_ms: 0,
                decision_kline_lag_ms: 1_200_000,
                minimum_replay_days: 90,
                minimum_decision_symbols: 50,
                minimum_funding_coverage: 0.5,
                standing_funding_max_age_hours: 25.0,
                presettlement_window_ms: 900_000,
                missing_conditioning: "fail_open".into(),
                missing_depth: "floor".into(),
                stale_whale: "null_fail_open".into(),
            },
            routing: SignalRouting {
                source: "directional_public_v1".into(),
                long_sleeve: "long".into(),
                carry_sleeve: "carry".into(),
            },
            sources: SourceContract {
                bybit_category: "linear".into(),
                bybit_settle_coin: "USDT".into(),
                bybit_mainnet_host: "api.bybit.com".into(),
                bybit_demo_host: "api-demo.bybit.com".into(),
                binance_host: "fapi.binance.com".into(),
                kline_interval_minutes: 60,
                funding_event_kind: "settlement".into(),
                whale_source: "binance_toptrader_position_long_short_ratio".into(),
                whale_period: "5m_eod".into(),
                mark_max_age_ms: 30_000,
                universe_identity_required: true,
            },
            live: LiveAcquisitionConfig {
                environment: "demo".into(),
                public_market_realm: "mainnet".into(),
                request_timeout_ms: 10_000,
                request_retries: 3,
                retry_base_ms: 500,
                ticker_cadence_ms: 5_000,
                instrument_cadence_ms: 3_600_000,
                funding_cadence_ms: 60_000,
                kline_cadence_ms: 60_000,
                whale_cadence_ms: 3_600_000,
                max_parallel_requests: 4,
                kline_page_limit: 1_000,
                funding_page_limit: 200,
                whale_page_limit: 500,
                instrument_max_pages: 10,
            },
            universe: crate::universe::UniverseRules::default(),
            llm_gate: crate::config::LlmGateConfig::default(),
            identity: ConfigIdentity {
                schema_version: SCHEMA_VERSION,
                signal_config_id: "test".into(),
                long_profile: "v12".into(),
                long_execution_strategy_id: "long_native_v12_wide_stop".into(),
                signal_config_sha256: "a".repeat(64),
                long_rule_sha256: "9".repeat(64),
                long_feature_contract_sha256: "8".repeat(64),
                carry_config_id: "lane2_carry_hold_v7".into(),
                carry_rule_sha256: "b".repeat(64),
                carry_feature_contract_sha256: "7".repeat(64),
                operational_profile_sha256: "c".repeat(64),
                engine_config_sha256: "d".repeat(64),
                long_decision_fingerprint: "e".repeat(64),
                carry_decision_fingerprint: "f".repeat(64),
            },
            long_destination: 1,
            carry_destination: 0,
            signal_path: PathBuf::from("signal.json"),
            long_rule_path: PathBuf::from("long.json"),
            carry_path: PathBuf::from("carry.json"),
            operational_path: PathBuf::from("operational.json"),
            engine_path: PathBuf::from("engine.toml"),
        }
    }

    fn test_universe() -> UniverseIdentity {
        UniverseIdentity {
            mode: UniverseMode::Pit,
            environment: "demo".into(),
            endpoint: "api-demo.bybit.com".into(),
            snapshot_ts_ms: DAY_MS,
            available_at_ms: DAY_MS + 1,
            artifact_sha256: "1".repeat(64),
            file_sha256: "2".repeat(64),
            symbols: vec!["BTCUSDT".into()],
            long_symbols: vec!["BTCUSDT".into()],
            carry_symbols: vec!["BTCUSDT".into()],
        }
    }

    fn readiness(reason: String) -> ObservationPayload {
        ObservationPayload::Readiness {
            readiness: Readiness {
                long_ready: false,
                carry_ready: false,
                universe_ready: true,
                reason,
                long_feature_ts_ms: None,
                carry_feature_ts_ms: None,
                rejected_symbols: Vec::new(),
            },
        }
    }

    fn empty_readiness() -> ObservationPayload {
        readiness("test".into())
    }

    fn ticker_wire(symbol: &str, price: f64) -> BybitTickerWire {
        let price = Some(serde_json::Value::from(price.to_string()));
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
            bid1_size: Some(serde_json::Value::from("1")),
            ask1_size: Some(serde_json::Value::from("1")),
            open_interest: None,
            open_interest_value: None,
            turnover24h: None,
            volume24h: None,
            funding_rate: None,
            next_funding_time: None,
        }
    }

    fn install_trading_instrument(worker: &mut SignalWorker, symbol: &str) {
        worker.state.instruments.insert(
            symbol.to_owned(),
            InstrumentObservation {
                symbol: symbol.to_owned(),
                observed_ts_ms: DAY_MS,
                available_at_ms: DAY_MS,
                contract_type: Some("LinearPerpetual".into()),
                symbol_type: None,
                status: Some("Trading".into()),
                base_coin: Some(symbol.trim_end_matches("USDT").into()),
                quote_coin: Some("USDT".into()),
                settle_coin: Some("USDT".into()),
                launch_time_ms: Some(1),
                delivery_time_ms: None,
                tick_size: Some(0.01),
                qty_step: Some(0.001),
                min_order_qty: Some(0.001),
                min_notional_value: Some(5.0),
                max_order_qty: None,
                max_market_order_qty: None,
                funding_interval_min: Some(480),
                is_prelisting: false,
            },
        );
        worker.state.instrument_trading_intervals.insert(
            symbol.to_owned(),
            vec![InstrumentTradingInterval {
                trading_from_ms: 1,
                trading_through_ms: None,
            }],
        );
    }

    fn compact_feature_config() -> SignalWorkerConfig {
        let mut config = test_config();
        config.long.universe_size = 1;
        config.long.universe_volume_window_days = 2;
        config.long.min_listing_history_days = 1;
        config.long.regime_sma_days = 2;
        config.long.vol_estimate_window_days = 2;
        config.long.daily_min_hourly_bars = 1;
        config.long.cold_start_lookback_days = 3;
        config.long.pump_lookback_days = [1, 2];
        config.long.atr_window_days = 2;
        config.long.atr_min_samples = 1;
        config.long.btc_rv_window_days = 2;
        config.long.btc_rv_min_samples = 1;
        config.carry.universe_top_n = 1;
        config.carry.persistence_window_settlements = None;
        config.carry.momentum_lookback_hours = 2;
        config.carry.adv_window_hours = 1;
        config.carry.return_lookback_hours = 1;
        config.carry.vol_window_hours = 4;
        config.carry.vol_return_lag_hours = 1;
        config.carry.vol_required_finite_samples = 4;
        config.carry.trail_window_hours = 1;
        config.carry.trail_change_lookback_hours = 2;
        config.carry.turn_growth_lookback_hours = 2;
        config.carry.minimum_replay_days = 0;
        config.carry.minimum_decision_symbols = 1;
        config.carry.minimum_funding_coverage = 1.0;
        config.carry.decision_kline_lag_ms = 0;
        config
    }

    fn install_compact_history(worker: &mut SignalWorker, through_day: i64) {
        install_trading_instrument(worker, "BTCUSDT");
        let klines = worker.state.klines.entry("BTCUSDT".into()).or_default();
        for open_ts_ms in (DAY_MS..through_day * DAY_MS).step_by(HOUR_MS as usize) {
            klines.insert(
                open_ts_ms,
                HourlyKline {
                    symbol: "BTCUSDT".into(),
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
        let funding = worker.state.funding.entry("BTCUSDT".into()).or_default();
        for settlement_ts_ms in (DAY_MS..=through_day * DAY_MS).step_by(HOUR_MS as usize) {
            funding.insert(
                settlement_ts_ms,
                SettledFunding {
                    symbol: "BTCUSDT".into(),
                    settlement_ts_ms,
                    available_at_ms: settlement_ts_ms,
                    rate: -0.001,
                    funding_interval_min: 60,
                },
            );
        }
    }

    fn compact_kline_rows(day: i64) -> Vec<Vec<Value>> {
        ((day - 1) * DAY_MS..day * DAY_MS)
            .step_by(HOUR_MS as usize)
            .map(|open_ts_ms| {
                vec![
                    Value::from(open_ts_ms),
                    Value::from("100"),
                    Value::from("200"),
                    Value::from("1"),
                    Value::from((100.0 + open_ts_ms as f64 / DAY_MS as f64).to_string()),
                    Value::from("1"),
                    Value::from("100"),
                ]
            })
            .collect()
    }

    fn compact_funding_rows(day: i64) -> Vec<BybitFundingWire> {
        (((day - 1) * DAY_MS + HOUR_MS)..=day * DAY_MS)
            .step_by(HOUR_MS as usize)
            .map(|settlement_ts_ms| BybitFundingWire {
                funding_rate_timestamp: Value::from(settlement_ts_ms),
                funding_rate: Value::from("-0.001"),
                funding_interval_hour: Some(Value::from(1)),
            })
            .collect()
    }

    fn trading_instrument_wire(symbol: &str, launch_time_ms: i64) -> BybitInstrumentWire {
        BybitInstrumentWire {
            symbol: symbol.to_owned(),
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

    fn closed_instrument_wire(
        symbol: &str,
        launch_time_ms: i64,
        delivery_time_ms: i64,
    ) -> BybitInstrumentWire {
        let mut row = trading_instrument_wire(symbol, launch_time_ms);
        row.status = Some("Closed".into());
        row.delivery_time = Some(Value::from(delivery_time_ms));
        row
    }

    fn clone_symbol_history(worker: &mut SignalWorker, source: &str, target: &str) {
        let klines = worker.state.klines[source]
            .values()
            .cloned()
            .map(|mut row| {
                row.symbol = target.to_owned();
                (row.open_ts_ms, row)
            })
            .collect();
        worker.state.klines.insert(target.to_owned(), klines);
        let funding = worker.state.funding[source]
            .values()
            .cloned()
            .map(|mut row| {
                row.symbol = target.to_owned();
                (row.settlement_ts_ms, row)
            })
            .collect();
        worker.state.funding.insert(target.to_owned(), funding);
    }

    fn worker_with_newer_prunable_state() -> SignalWorker {
        let newer_at_ms = 200 * DAY_MS;
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        worker.state.last_observed_ts_ms = newer_at_ms;
        let kline_ts_ms = newer_at_ms - HOUR_MS;
        worker
            .state
            .klines
            .entry("BTCUSDT".into())
            .or_default()
            .insert(
                kline_ts_ms,
                HourlyKline {
                    symbol: "BTCUSDT".into(),
                    open_ts_ms: kline_ts_ms,
                    available_at_ms: newer_at_ms,
                    open: 100.0,
                    high: 101.0,
                    low: 99.0,
                    close: 100.0,
                    volume_base: 1.0,
                    turnover_quote: 100.0,
                },
            );
        let funding_ts_ms = newer_at_ms - HOUR_MS;
        worker
            .state
            .funding
            .entry("BTCUSDT".into())
            .or_default()
            .insert(
                funding_ts_ms,
                SettledFunding {
                    symbol: "BTCUSDT".into(),
                    settlement_ts_ms: funding_ts_ms,
                    available_at_ms: newer_at_ms,
                    rate: -0.001,
                    funding_interval_min: 60,
                },
            );
        let whale_ts_ms = newer_at_ms - DAY_MS;
        worker
            .state
            .whales
            .entry("BTCUSDT".into())
            .or_default()
            .insert(
                whale_ts_ms,
                BinanceWhaleObservation {
                    symbol: "BTCUSDT".into(),
                    day_end_ms: whale_ts_ms,
                    available_at_ms: newer_at_ms,
                    long_short_ratio: Some(1.0),
                },
            );
        worker.state.instrument_trading_intervals.insert(
            "BTCUSDT".into(),
            vec![InstrumentTradingInterval {
                trading_from_ms: newer_at_ms - 2 * DAY_MS,
                trading_through_ms: Some(newer_at_ms - DAY_MS),
            }],
        );
        worker
    }

    fn assert_stale_source_event_preserves_newer_state(label: &str, event: WireEvent) {
        let newer_at_ms = 200 * DAY_MS;
        let mut worker = worker_with_newer_prunable_state();
        worker.apply(event).unwrap();

        assert_eq!(worker.state.last_observed_ts_ms, newer_at_ms, "{label}");
        assert!(
            worker.state.klines["BTCUSDT"].contains_key(&(newer_at_ms - HOUR_MS)),
            "{label}"
        );
        assert!(
            worker.state.funding["BTCUSDT"].contains_key(&(newer_at_ms - HOUR_MS)),
            "{label}"
        );
        assert!(
            worker.state.whales["BTCUSDT"].contains_key(&(newer_at_ms - DAY_MS)),
            "{label}"
        );
        assert_eq!(
            worker.state.instrument_trading_intervals["BTCUSDT"],
            vec![InstrumentTradingInterval {
                trading_from_ms: newer_at_ms - 2 * DAY_MS,
                trading_through_ms: Some(newer_at_ms - DAY_MS),
            }],
            "{label}"
        );
    }

    fn temporary_root(label: &str) -> PathBuf {
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        std::env::temp_dir().join(format!(
            "signal-worker-{label}-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    #[test]
    fn duplicate_public_rows_must_be_byte_equivalent() {
        let mut rows = BTreeMap::new();
        insert_exact(&mut rows, 1, 2_u64, "row").unwrap();
        insert_exact(&mut rows, 1, 2_u64, "row").unwrap();
        assert!(insert_exact(&mut rows, 1, 3_u64, "row").is_err());
    }

    #[test]
    fn disjoint_kline_windows_survive_restart_without_claiming_the_gap() {
        let config = test_config();
        let universe = test_universe();
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        for (sequence, checked_from_ms) in [(1, 10 * DAY_MS), (2, 100 * DAY_MS)] {
            worker
                .apply(WireEvent::BybitKlineBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "BTCUSDT".into(),
                    available_at_ms: checked_from_ms + DAY_MS,
                    checked_from_ms: Some(checked_from_ms),
                    checked_through_ms: Some(checked_from_ms + DAY_MS),
                    replace_coverage: false,
                    rows: Vec::new(),
                })
                .unwrap();
        }
        assert_eq!(worker.state.kline_coverage_intervals["BTCUSDT"].len(), 2);
        assert!(!worker.state.kline_checked_from_ms.contains_key("BTCUSDT"));
        assert!(!worker
            .state
            .kline_checked_through_ms
            .contains_key("BTCUSDT"));

        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        let intervals = &restored.state.kline_coverage_intervals["BTCUSDT"];
        assert_eq!(intervals.len(), 2);
        assert_eq!(intervals[0].checked_through_ms, 11 * DAY_MS);
        assert_eq!(intervals[1].checked_from_ms, 100 * DAY_MS);
    }

    #[test]
    fn fragmented_source_coverage_survives_and_repeated_fetch_converges() {
        let config = test_config();
        let universe = test_universe();
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        let base = 10 * DAY_MS;
        let available_at_ms = 11 * DAY_MS;
        let mut sequence = 1;

        for index in 0..6 {
            let checked_from_ms = base + index * 2 * HOUR_MS;
            worker
                .apply(WireEvent::BybitKlineBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "BTCUSDT".into(),
                    available_at_ms,
                    checked_from_ms: Some(checked_from_ms),
                    checked_through_ms: Some(checked_from_ms + HOUR_MS),
                    replace_coverage: false,
                    rows: Vec::new(),
                })
                .unwrap();
            sequence += 1;
        }
        for index in 0..6 {
            let checked_from_ms = base + index * 2 * HOUR_MS;
            worker
                .apply(WireEvent::BybitFundingBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "BTCUSDT".into(),
                    available_at_ms,
                    checked_from_ms: Some(checked_from_ms),
                    checked_through_ms: Some(checked_from_ms + HOUR_MS),
                    replace_coverage: false,
                    emit_lifecycle: false,
                    rows: Vec::new(),
                })
                .unwrap();
            sequence += 1;
        }
        for index in 0..6 {
            let checked_from_ms = base + index * 2 * HOUR_MS;
            worker
                .apply(WireEvent::BinanceWhaleBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    available_at_ms,
                    coverage: vec![SourceCoverage {
                        symbol: "BTCUSDT".into(),
                        checked_from_ms,
                        checked_through_ms: checked_from_ms + HOUR_MS,
                        replace_coverage: false,
                    }],
                    rows: Vec::new(),
                })
                .unwrap();
            sequence += 1;
        }

        for intervals in [
            &worker.state.kline_coverage_intervals["BTCUSDT"],
            &worker.state.funding_coverage_intervals["BTCUSDT"],
            &worker.state.whale_coverage_intervals["BTCUSDT"],
        ] {
            assert_eq!(intervals.len(), 6);
            assert_eq!(intervals[0].checked_from_ms, base);
            assert_eq!(intervals[1].checked_from_ms, base + 2 * HOUR_MS);
            assert!(intervals
                .windows(2)
                .all(|pair| pair[0].checked_through_ms < pair[1].checked_from_ms));
        }

        let before = (
            worker.state.kline_coverage_intervals.clone(),
            worker.state.funding_coverage_intervals.clone(),
            worker.state.whale_coverage_intervals.clone(),
        );
        let repeated_from_ms = base + 4 * HOUR_MS;
        worker
            .apply(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms,
                checked_from_ms: Some(repeated_from_ms),
                checked_through_ms: Some(repeated_from_ms + HOUR_MS),
                replace_coverage: false,
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
        worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms,
                checked_from_ms: Some(repeated_from_ms),
                checked_through_ms: Some(repeated_from_ms + HOUR_MS),
                replace_coverage: false,
                emit_lifecycle: false,
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
        worker
            .apply(WireEvent::BinanceWhaleBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                available_at_ms,
                coverage: vec![SourceCoverage {
                    symbol: "BTCUSDT".into(),
                    checked_from_ms: repeated_from_ms,
                    checked_through_ms: repeated_from_ms + HOUR_MS,
                    replace_coverage: false,
                }],
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
        assert_eq!(
            before,
            (
                worker.state.kline_coverage_intervals.clone(),
                worker.state.funding_coverage_intervals.clone(),
                worker.state.whale_coverage_intervals.clone(),
            )
        );

        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        assert_eq!(restored.state.last_input_sequence, sequence - 1);
        assert_eq!(restored.state.whale_coverage_intervals["BTCUSDT"].len(), 6);
    }

    #[test]
    fn prune_splits_source_coverage_and_restart_cannot_overclaim_the_gap() {
        let config = test_config();
        let universe = test_universe();
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        worker.state.last_carry_decision_ts_ms = Some(10 * DAY_MS);
        let broad = vec![CoverageInterval {
            checked_from_ms: DAY_MS,
            checked_through_ms: 200 * DAY_MS,
        }];
        worker
            .state
            .funding_coverage_intervals
            .insert("BTCUSDT".into(), broad.clone());
        worker
            .state
            .whale_coverage_intervals
            .insert("BTCUSDT".into(), broad);
        worker.prune(200 * DAY_MS);

        for intervals in [
            &worker.state.funding_coverage_intervals["BTCUSDT"],
            &worker.state.whale_coverage_intervals["BTCUSDT"],
        ] {
            assert_eq!(intervals.len(), 2);
            assert!(intervals[0].checked_through_ms <= 11 * DAY_MS);
            assert!(intervals[1].checked_from_ms > 100 * DAY_MS);
            assert!(intervals[0].checked_through_ms < intervals[1].checked_from_ms);
        }
        assert!(!worker.state.funding_checked_from_ms.contains_key("BTCUSDT"));
        assert!(!worker.state.whale_checked_from_ms.contains_key("BTCUSDT"));

        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        assert_eq!(
            restored.state.funding_coverage_intervals["BTCUSDT"].len(),
            2
        );
        assert_eq!(restored.state.whale_coverage_intervals["BTCUSDT"].len(), 2);
        assert!(!restored
            .state
            .funding_checked_through_ms
            .contains_key("BTCUSDT"));
        assert!(!restored
            .state
            .whale_checked_through_ms
            .contains_key("BTCUSDT"));
    }

    #[test]
    fn stale_kline_availability_cannot_roll_back_pruning() {
        assert_stale_source_event_preserves_newer_state(
            "kline",
            WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 20 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                rows: Vec::new(),
            },
        );
    }

    #[test]
    fn stale_funding_availability_cannot_roll_back_pruning() {
        assert_stale_source_event_preserves_newer_state(
            "funding",
            WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 20 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: false,
                rows: Vec::new(),
            },
        );
    }

    #[test]
    fn stale_instrument_availability_cannot_roll_back_pruning() {
        assert_stale_source_event_preserves_newer_state(
            "instrument",
            WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 20 * DAY_MS,
                available_at_ms: 20 * DAY_MS,
                rows: Vec::new(),
            },
        );
    }

    #[test]
    fn stale_whale_availability_cannot_roll_back_pruning() {
        assert_stale_source_event_preserves_newer_state(
            "whale",
            WireEvent::BinanceWhaleBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                available_at_ms: 20 * DAY_MS,
                coverage: Vec::new(),
                rows: Vec::new(),
            },
        );
    }

    #[test]
    fn source_ingestion_stays_bounded_when_no_watermark_can_complete() {
        let config = test_config();
        let mut universe = test_universe();
        universe.symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
        universe.long_symbols = vec!["AAAUSDT".into()];
        universe.carry_symbols = vec!["AAAUSDT".into()];
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        let mut sequence = 1_u64;
        for day in 1..=180_i64 {
            let start = day * DAY_MS;
            let end = start + DAY_MS;
            let kline_rows = (0..24_i64)
                .map(|hour| {
                    vec![
                        Value::from(start + hour * HOUR_MS),
                        Value::from("1"),
                        Value::from("1"),
                        Value::from("1"),
                        Value::from("1"),
                        Value::from("1"),
                        Value::from("1"),
                    ]
                })
                .collect();
            worker
                .apply(WireEvent::BybitKlineBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "AAAUSDT".into(),
                    available_at_ms: end,
                    checked_from_ms: Some(start),
                    checked_through_ms: Some(end),
                    replace_coverage: false,
                    rows: kline_rows,
                })
                .unwrap();
            sequence += 1;
            let funding_rows = [8_i64, 16, 24]
                .into_iter()
                .map(|hour| BybitFundingWire {
                    funding_rate_timestamp: Value::from(start + hour * HOUR_MS),
                    funding_rate: Value::from("-0.001"),
                    funding_interval_hour: Some(Value::from(8)),
                })
                .collect();
            worker
                .apply(WireEvent::BybitFundingBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "AAAUSDT".into(),
                    available_at_ms: end,
                    checked_from_ms: Some(start),
                    checked_through_ms: Some(end),
                    replace_coverage: false,
                    emit_lifecycle: false,
                    rows: funding_rows,
                })
                .unwrap();
            sequence += 1;
            worker
                .apply(WireEvent::BinanceWhaleBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    available_at_ms: end,
                    coverage: vec![SourceCoverage {
                        symbol: "AAAUSDT".into(),
                        checked_from_ms: start,
                        checked_through_ms: end,
                        replace_coverage: false,
                    }],
                    rows: vec![BinanceWhaleWire {
                        symbol: "AAAUSDT".into(),
                        day_end_ms: Value::from(end),
                        long_short_ratio: Some(Value::from("1.1")),
                    }],
                })
                .unwrap();
            sequence += 1;
        }

        let carry_hours = required_carry_history_hours(&config, &worker.state) as usize;
        assert!(worker.state.klines["AAAUSDT"].len() <= carry_hours + 1);
        assert!(worker.state.funding["AAAUSDT"].len() <= carry_hours / 8 + 2);
        assert!(worker.state.whales["AAAUSDT"].len() <= 8);
        assert!(worker.state.kline_coverage_intervals["AAAUSDT"].len() <= 2);
        assert!(worker.state.funding_coverage_intervals["AAAUSDT"].len() <= 2);
        assert!(worker.state.whale_coverage_intervals["AAAUSDT"].len() <= 2);
        assert!(!worker.state.klines.contains_key("BTCUSDT"));

        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        assert_eq!(
            restored.state.klines["AAAUSDT"].len(),
            worker.state.klines["AAAUSDT"].len()
        );
        assert_eq!(
            restored.state.funding["AAAUSDT"].len(),
            worker.state.funding["AAAUSDT"].len()
        );
        assert_eq!(
            restored.state.whales["AAAUSDT"].len(),
            worker.state.whales["AAAUSDT"].len()
        );
    }

    #[test]
    fn restore_rejects_noncanonical_source_coverage_intervals() {
        let config = test_config();
        let universe = test_universe();
        let mut state = SignalWorker::with_universe(config.clone(), universe.clone())
            .unwrap()
            .state
            .clone();
        state.funding_coverage_intervals.insert(
            "BTCUSDT".into(),
            vec![
                CoverageInterval {
                    checked_from_ms: 2 * DAY_MS,
                    checked_through_ms: 4 * DAY_MS,
                },
                CoverageInterval {
                    checked_from_ms: 3 * DAY_MS,
                    checked_through_ms: 5 * DAY_MS,
                },
            ],
        );
        assert!(SignalWorker::restore(config, state).is_err());
    }

    #[test]
    fn long_fast_forward_records_the_exact_skipped_range() {
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        worker.state.last_long_feature_ts_ms = Some(10 * DAY_MS);
        worker.record_long_fast_forward(14 * DAY_MS);
        assert_eq!(worker.state.long_skipped_generation_count, 3);
        assert_eq!(
            worker.state.last_long_skipped_first_ts_ms,
            Some(11 * DAY_MS)
        );
        assert_eq!(worker.state.last_long_skipped_last_ts_ms, Some(13 * DAY_MS));
        worker.state.last_long_feature_ts_ms = Some(14 * DAY_MS);
        worker.record_long_fast_forward(15 * DAY_MS);
        assert_eq!(worker.state.long_skipped_generation_count, 3);
    }

    #[test]
    fn paused_engine_coalesces_actionable_generations_and_republishes_current_state() {
        let config = compact_feature_config();
        let universe = test_universe();
        let root = temporary_root("paused-engine-current");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
        let spool = SpoolWriter::new(&spool_dir).unwrap();

        let mut seeded = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        install_compact_history(&mut seeded, 9);
        let long = seeded
            .apply(WireEvent::LongWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 8 * DAY_MS,
                data_through_ms: 8 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        let carry = seeded
            .apply(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 8 * DAY_MS,
                data_through_ms: 8 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(long.len(), 1);
        assert_eq!(long[0].kind, "long_feature_batch");
        assert_eq!(carry.len(), 1);
        assert_eq!(carry[0].kind, "carry_feature_batch");
        checkpoint.save(seeded.state()).unwrap();
        let old_long_path = spool.write(&long[0]).unwrap();
        let old_carry_path = spool.write(&carry[0]).unwrap();

        let mut durable = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        let mut sequence = 3;
        for day in [9, 10] {
            durable
                .apply_and_commit(WireEvent::BybitKlineBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "BTCUSDT".into(),
                    available_at_ms: day * DAY_MS,
                    checked_from_ms: None,
                    checked_through_ms: None,
                    replace_coverage: false,
                    rows: compact_kline_rows(day),
                })
                .unwrap();
            sequence += 1;
            durable
                .apply_and_commit(WireEvent::BybitFundingBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "BTCUSDT".into(),
                    available_at_ms: day * DAY_MS,
                    checked_from_ms: None,
                    checked_through_ms: None,
                    replace_coverage: false,
                    emit_lifecycle: false,
                    rows: compact_funding_rows(day),
                })
                .unwrap();
            sequence += 1;
            durable
                .apply_and_commit(WireEvent::LongWatermark {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    observed_ts_ms: day * DAY_MS,
                    data_through_ms: day * DAY_MS,
                    gap_symbols: Vec::new(),
                })
                .unwrap();
            sequence += 1;
            durable
                .apply_and_commit(WireEvent::CarryWatermark {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    observed_ts_ms: day * DAY_MS,
                    data_through_ms: day * DAY_MS,
                    gap_symbols: Vec::new(),
                })
                .unwrap();
            sequence += 1;
        }
        assert_eq!(
            durable.worker.state.last_long_feature_ts_ms,
            Some(10 * DAY_MS)
        );
        assert_eq!(
            durable.worker.state.pending_long_refresh_feature_ts_ms,
            Some(10 * DAY_MS)
        );
        assert_eq!(durable.worker.state.long_skipped_generation_count, 2);
        assert_eq!(
            durable.worker.state.last_long_skipped_first_ts_ms,
            Some(8 * DAY_MS)
        );
        assert_eq!(
            durable.worker.state.last_long_skipped_last_ts_ms,
            Some(9 * DAY_MS)
        );
        assert_eq!(
            durable.worker.state.last_carry_decision_ts_ms,
            Some(8 * DAY_MS)
        );
        assert_eq!(
            durable.worker.state.last_carry_scorer_ts_ms,
            Some(10 * DAY_MS)
        );
        drop(durable);

        let mut durable =
            DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir)
                .unwrap();
        assert_eq!(durable.worker.state.last_input_sequence, 10);
        assert_eq!(
            durable.worker.state.last_carry_decision_ts_ms,
            Some(8 * DAY_MS)
        );
        assert_eq!(
            durable.worker.state.last_carry_scorer_ts_ms,
            Some(10 * DAY_MS)
        );
        std::fs::remove_file(old_long_path).unwrap();
        std::fs::remove_file(old_carry_path).unwrap();

        let refreshed_long = durable
            .apply_and_commit(WireEvent::LongWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 11,
                observed_ts_ms: 10 * DAY_MS + 1,
                data_through_ms: 10 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(refreshed_long.len(), 1);
        assert_eq!(refreshed_long[0].kind, "long_feature_batch");
        let refreshed_carry = durable
            .apply_and_commit(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 12,
                observed_ts_ms: 10 * DAY_MS + 2,
                data_through_ms: 10 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(refreshed_carry.len(), 1);
        assert_eq!(refreshed_carry[0].kind, "carry_feature_batch");
        assert_eq!(refreshed_carry[0].observed_wall_ts_ms, 10 * DAY_MS + 2);
        assert_eq!(refreshed_carry[0].available_wall_ts_ms, 10 * DAY_MS + 2);
        assert_eq!(
            durable.worker.state.last_carry_decision_ts_ms,
            Some(10 * DAY_MS)
        );
        assert_eq!(
            durable.worker.state.last_carry_scorer_ts_ms,
            Some(10 * DAY_MS)
        );

        let duplicate_long = durable
            .apply_and_commit(WireEvent::LongWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 13,
                observed_ts_ms: 10 * DAY_MS + 3,
                data_through_ms: 10 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert!(duplicate_long.is_empty());
        let duplicate_carry = durable
            .apply_and_commit(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 14,
                observed_ts_ms: 10 * DAY_MS + 4,
                data_through_ms: 10 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert!(duplicate_carry
            .iter()
            .all(|observation| observation.kind == "readiness"));
        let inventory = spool.inventory().unwrap();
        assert!(inventory
            .replaceable_paths
            .contains_key("long_feature_batch"));
        assert!(inventory
            .replaceable_paths
            .contains_key("carry_feature_batch"));
        assert!(inventory.files <= 5);
        assert!(inventory.classes["current"].files <= 3);
        assert_eq!(inventory.classes["catchup"].files, 2);
        assert!(inventory.classes["current"]
            .newest_path
            .as_ref()
            .is_some_and(|path| path.exists()));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn one_spool_class_at_cap_does_not_block_unrelated_source_commits() {
        let root = temporary_root("class-backpressure");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let mut durable = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        durable.spool_classes.insert(
            "lifecycle".into(),
            SpoolClassInventory {
                files: LIFECYCLE_SPOOL_FILE_CAP,
                bytes: 0,
                oldest_path: None,
                newest_path: None,
            },
        );
        durable.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
        let blocked = durable
            .apply_and_commit(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 3 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: true,
                rows: vec![BybitFundingWire {
                    funding_rate_timestamp: Value::from(3 * DAY_MS),
                    funding_rate: Value::from("-0.001"),
                    funding_interval_hour: Some(Value::from(1)),
                }],
            })
            .unwrap();
        assert!(blocked.is_empty());
        assert_eq!(durable.worker.state.last_input_sequence, 0);
        assert!(durable.spool_backpressured_for("lifecycle"));
        assert!(!durable.spool_backpressured_for("current"));

        durable
            .apply_and_commit(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 2 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                rows: Vec::new(),
            })
            .unwrap();
        assert_eq!(durable.worker.state.last_input_sequence, 1);
        let metrics = durable.durability_metrics().unwrap();
        assert_eq!(
            metrics.spool_class_file_caps["lifecycle"],
            LIFECYCLE_SPOOL_FILE_CAP
        );
        assert_eq!(
            metrics.spool_class_files["lifecycle"],
            LIFECYCLE_SPOOL_FILE_CAP
        );
        assert_eq!(metrics.spool_backpressured_classes, vec!["lifecycle"]);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn batch_receipt_exposes_the_uncommitted_suffix_for_exact_retry() {
        let root = temporary_root("batch-backpressure-receipt");
        let mut durable = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            root.join("state"),
            root.join("spool"),
        )
        .unwrap();
        durable.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
        durable.spool_classes.insert(
            "lifecycle".into(),
            SpoolClassInventory {
                files: LIFECYCLE_SPOOL_FILE_CAP - 1,
                bytes: 0,
                oldest_path: None,
                newest_path: None,
            },
        );
        let event = |sequence, settlement_ts_ms| WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence,
            symbol: "BTCUSDT".into(),
            available_at_ms: settlement_ts_ms,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![BybitFundingWire {
                funding_rate_timestamp: Value::from(settlement_ts_ms),
                funding_rate: Value::from("-0.001"),
                funding_interval_hour: Some(Value::from(1)),
            }],
        };
        let receipt = durable
            .apply_many_and_commit(vec![event(1, 3 * DAY_MS), event(2, 4 * DAY_MS)])
            .unwrap();
        assert_eq!(receipt.attempted_events, 2);
        assert_eq!(receipt.committed_events, 1);
        assert!(!receipt.fully_committed());
        assert_eq!(durable.worker.state.last_input_sequence, 1);
        assert!(durable.worker.state.funding["BTCUSDT"].contains_key(&(3 * DAY_MS)));
        assert!(!durable.worker.state.funding["BTCUSDT"].contains_key(&(4 * DAY_MS)));

        let sentinel = durable.spool_classes["lifecycle"]
            .oldest_path
            .clone()
            .unwrap();
        std::fs::remove_file(sentinel).unwrap();
        durable.refresh_spool_backpressure().unwrap();
        let retried = durable
            .apply_many_and_commit(vec![event(2, 4 * DAY_MS)])
            .unwrap();
        assert!(retried.fully_committed());
        assert_eq!(durable.worker.state.last_input_sequence, 2);
        assert!(durable.worker.state.funding["BTCUSDT"].contains_key(&(4 * DAY_MS)));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn deleting_the_oldest_class_file_immediately_releases_backpressure() {
        let root = temporary_root("class-oldest-sentinel");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let config = test_config();
        let universe = test_universe();
        let mut durable = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        for sequence in 1..=2 {
            let observation = make_observation(
                &config,
                &universe,
                REPLAY_SOURCE_GENERATION,
                false,
                sequence,
                "funding_update",
                2 * DAY_MS + sequence as i64,
                2 * DAY_MS + sequence as i64,
                ObservationPayload::FundingUpdate {
                    decision_ts_ms: 2 * DAY_MS,
                    settled_funding: Vec::new(),
                },
                Vec::new(),
            )
            .unwrap();
            durable.spool.write(&observation).unwrap();
        }
        let inventory = durable.spool.inventory().unwrap();
        let lifecycle = inventory.classes["lifecycle"].clone();
        let oldest = lifecycle.oldest_path.clone().unwrap();
        let newest = lifecycle.newest_path.clone().unwrap();
        assert_ne!(oldest, newest);
        durable.spool_files = inventory.files;
        durable.spool_bytes = inventory.bytes;
        durable.spool_classes = inventory.classes;
        durable.spool_classes.get_mut("lifecycle").unwrap().files = LIFECYCLE_SPOOL_FILE_CAP;
        durable.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
        std::fs::remove_file(&oldest).unwrap();
        assert!(newest.exists());

        let emitted = durable
            .apply_and_commit(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 3 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: true,
                rows: vec![BybitFundingWire {
                    funding_rate_timestamp: Value::from(3 * DAY_MS),
                    funding_rate: Value::from("-0.001"),
                    funding_interval_hour: Some(Value::from(1)),
                }],
            })
            .unwrap();
        assert_eq!(emitted.len(), 1);
        assert_eq!(durable.worker.state.last_input_sequence, 1);
        assert!(!durable.spool_backpressured_for("lifecycle"));
        assert_eq!(durable.spool_classes["lifecycle"].files, 2);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn carry_watermark_preflight_reserves_only_the_state_relevant_class() {
        let root = temporary_root("carry-class-preflight-current");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let mut durable = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        durable.pending_replaceable_paths.insert(
            "carry_feature_batch".into(),
            state_dir.join("checkpoint.json"),
        );
        durable.spool_classes.insert(
            "catchup".into(),
            SpoolClassInventory {
                files: CATCHUP_SPOOL_FILE_CAP,
                bytes: 0,
                oldest_path: None,
                newest_path: None,
            },
        );
        durable.worker.state.last_carry_decision_ts_ms = Some(DAY_MS);
        durable.worker.state.last_carry_scorer_ts_ms = Some(DAY_MS);
        let current = durable
            .apply_and_commit(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                data_through_ms: 2 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert!(current
            .iter()
            .all(|observation| observation.kind == "readiness"));
        assert_eq!(durable.worker.state.last_input_sequence, 1);

        let blocked_root = temporary_root("carry-class-preflight-catchup");
        let blocked_state = blocked_root.join("state");
        let mut blocked = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            &blocked_state,
            blocked_root.join("spool"),
        )
        .unwrap();
        blocked.pending_replaceable_paths.insert(
            "carry_feature_batch".into(),
            blocked_state.join("checkpoint.json"),
        );
        blocked.spool_classes.insert(
            "catchup".into(),
            SpoolClassInventory {
                files: CATCHUP_SPOOL_FILE_CAP,
                bytes: 0,
                oldest_path: None,
                newest_path: None,
            },
        );
        blocked.worker.state.last_carry_decision_ts_ms = Some(DAY_MS);
        blocked.worker.state.last_carry_scorer_ts_ms = Some(0);
        assert!(blocked
            .apply_and_commit(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                data_through_ms: 2 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap()
            .is_empty());
        assert_eq!(blocked.worker.state.last_input_sequence, 0);
        assert!(blocked.spool_backpressured_for("catchup"));
        std::fs::remove_dir_all(root).unwrap();
        std::fs::remove_dir_all(blocked_root).unwrap();
    }

    #[test]
    fn projected_class_caps_do_not_overshoot_at_the_file_or_byte_edge() {
        let root = temporary_root("projected-class-file-cap");
        let mut durable = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            root.join("state"),
            root.join("spool"),
        )
        .unwrap();
        durable.spool_classes.insert(
            "current".into(),
            SpoolClassInventory {
                files: CURRENT_SPOOL_FILE_CAP - 1,
                bytes: 0,
                oldest_path: None,
                newest_path: None,
            },
        );
        assert_eq!(
            durable
                .apply_and_commit(WireEvent::BybitTickerSnapshot {
                    schema_version: SCHEMA_VERSION,
                    sequence: 1,
                    observed_ts_ms: 2 * DAY_MS,
                    available_at_ms: 2 * DAY_MS,
                    rows: vec![ticker_wire("BTCUSDT", 100.0)],
                })
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            durable.spool_classes["current"].files,
            CURRENT_SPOOL_FILE_CAP
        );
        assert!(durable
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 2 * DAY_MS + 1,
                available_at_ms: 2 * DAY_MS + 1,
                rows: vec![ticker_wire("BTCUSDT", 101.0)],
            })
            .unwrap()
            .is_empty());
        assert_eq!(durable.worker.state.last_input_sequence, 2);
        assert_eq!(
            durable.spool_classes["current"].files,
            CURRENT_SPOOL_FILE_CAP
        );

        let byte_root = temporary_root("projected-class-byte-cap");
        let mut byte_blocked = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            byte_root.join("state"),
            byte_root.join("spool"),
        )
        .unwrap();
        byte_blocked.spool_classes.insert(
            "current".into(),
            SpoolClassInventory {
                files: 0,
                bytes: CURRENT_SPOOL_BYTE_SOFT_THRESHOLD.saturating_sub(1),
                oldest_path: None,
                newest_path: None,
            },
        );
        let allowed = byte_blocked
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS,
                rows: vec![ticker_wire("BTCUSDT", 100.0)],
            })
            .unwrap();
        assert_eq!(allowed.len(), 1);
        assert!(byte_blocked.spool_classes["current"].bytes <= CURRENT_SPOOL_BYTE_CAP);
        byte_blocked.pending_replaceable_paths.clear();
        assert!(byte_blocked
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 2 * DAY_MS + 1,
                available_at_ms: 2 * DAY_MS + 1,
                rows: vec![ticker_wire("BTCUSDT", 101.0)],
            })
            .unwrap()
            .is_empty());
        assert_eq!(byte_blocked.worker.state.last_input_sequence, 1);
        std::fs::remove_dir_all(root).unwrap();
        std::fs::remove_dir_all(byte_root).unwrap();
    }

    #[test]
    fn small_lifecycle_and_catchup_items_reach_real_thresholds_not_file_worst_cases() {
        let lifecycle_root = temporary_root("small-lifecycle-spool-items");
        let mut lifecycle = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            lifecycle_root.join("state"),
            lifecycle_root.join("spool"),
        )
        .unwrap();
        lifecycle.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
        lifecycle.spool_classes.insert(
            "lifecycle".into(),
            SpoolClassInventory {
                files: 100,
                bytes: LIFECYCLE_SPOOL_BYTE_SOFT_THRESHOLD - 1,
                oldest_path: None,
                newest_path: None,
            },
        );
        lifecycle.spool_files = 100;
        lifecycle.spool_bytes = LIFECYCLE_SPOOL_BYTE_SOFT_THRESHOLD - 1;
        let rows = lifecycle
            .apply_and_commit(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 3 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: true,
                rows: vec![BybitFundingWire {
                    funding_rate_timestamp: Value::from(3 * DAY_MS),
                    funding_rate: Value::from("-0.001"),
                    funding_interval_hour: Some(Value::from(1)),
                }],
            })
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(lifecycle.spool_classes["lifecycle"].files, 101);
        assert!(lifecycle.spool_classes["lifecycle"].bytes <= LIFECYCLE_SPOOL_BYTE_CAP);

        let catchup_root = temporary_root("small-catchup-spool-items");
        let config = compact_feature_config();
        let mut catchup = DurableSignalWorker::open_with_universe(
            config,
            test_universe(),
            catchup_root.join("state"),
            catchup_root.join("spool"),
        )
        .unwrap();
        install_compact_history(&mut catchup.worker, 16);
        catchup.worker.state.last_carry_decision_ts_ms = Some(8 * DAY_MS);
        catchup.worker.state.last_carry_scorer_ts_ms = Some(8 * DAY_MS);
        catchup.spool_classes.insert(
            "catchup".into(),
            SpoolClassInventory {
                files: 100,
                bytes: CATCHUP_SPOOL_BYTE_SOFT_THRESHOLD - 1,
                oldest_path: None,
                newest_path: None,
            },
        );
        catchup.spool_files = 100;
        catchup.spool_bytes = CATCHUP_SPOOL_BYTE_SOFT_THRESHOLD - 1;
        let rows = catchup
            .apply_and_commit(WireEvent::CarryScorerCatchupWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 16 * DAY_MS,
                decision_through_ms: 15 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(rows.len(), MAX_CARRY_SCORER_CATCHUP_DAYS as usize);
        assert_eq!(
            catchup.spool_classes["catchup"].files,
            100 + MAX_CARRY_SCORER_CATCHUP_DAYS as u64
        );
        assert!(catchup.spool_classes["catchup"].bytes <= CATCHUP_SPOOL_BYTE_CAP);
        assert_eq!(
            catchup.durability_metrics().unwrap().spool_class_byte_caps["catchup"],
            CATCHUP_SPOOL_BYTE_CAP
        );
        std::fs::remove_dir_all(lifecycle_root).unwrap();
        std::fs::remove_dir_all(catchup_root).unwrap();
    }

    #[test]
    fn pending_replaceable_watermarks_do_not_reserve_files_they_cannot_emit() {
        let root = temporary_root("suppressed-watermark-preflight");
        let state_dir = root.join("state");
        let mut durable = DurableSignalWorker::open_with_universe(
            compact_feature_config(),
            test_universe(),
            &state_dir,
            root.join("spool"),
        )
        .unwrap();
        install_compact_history(&mut durable.worker, 10);
        durable.worker.state.last_carry_decision_ts_ms = Some(10 * DAY_MS);
        durable.worker.state.last_carry_scorer_ts_ms = Some(10 * DAY_MS);
        let sentinel = state_dir.join("checkpoint.json");
        for kind in [
            "market_snapshot",
            "readiness",
            "long_feature_batch",
            "carry_feature_batch",
        ] {
            durable
                .pending_replaceable_paths
                .insert(kind.to_owned(), sentinel.clone());
        }
        durable.spool_classes.insert(
            "current".into(),
            SpoolClassInventory {
                files: CURRENT_SPOOL_FILE_CAP,
                bytes: CURRENT_SPOOL_BYTE_SOFT_THRESHOLD,
                oldest_path: None,
                newest_path: None,
            },
        );
        durable.spool_files = CURRENT_SPOOL_FILE_CAP;
        durable.spool_bytes = CURRENT_SPOOL_BYTE_SOFT_THRESHOLD;

        assert!(durable
            .apply_and_commit(WireEvent::LongWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 10 * DAY_MS,
                data_through_ms: 10 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap()
            .is_empty());
        assert_eq!(durable.worker.state.last_input_sequence, 1);
        assert!(durable
            .apply_and_commit(WireEvent::Watermark {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 10 * DAY_MS + 1,
            })
            .unwrap()
            .is_empty());
        assert_eq!(durable.worker.state.last_input_sequence, 2);
        assert_eq!(
            durable.spool_classes["current"].files,
            CURRENT_SPOOL_FILE_CAP
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn duplicate_history_keeps_first_availability_and_rejects_rewrite() {
        let mut rows = BTreeMap::new();
        let row = BinanceWhaleObservation {
            symbol: "BTCUSDT".into(),
            day_end_ms: 86_400_000,
            available_at_ms: 86_400_100,
            long_short_ratio: Some(1.0),
        };
        merge_whale(&mut rows, row.clone()).unwrap();
        let mut later = row.clone();
        later.available_at_ms += 100;
        merge_whale(&mut rows, later).unwrap();
        assert_eq!(rows[&row.day_end_ms].available_at_ms, row.available_at_ms);
        let mut conflict = row;
        conflict.long_short_ratio = Some(2.0);
        assert!(merge_whale(&mut rows, conflict).is_err());
    }

    #[test]
    fn feature_marks_keep_the_ticker_clock_and_expire() {
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        worker.state.tickers.insert(
            "BTCUSDT".into(),
            TickerObservation {
                symbol: "BTCUSDT".into(),
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS + 10,
                mark_observed_ts_ms: Some(2 * DAY_MS),
                funding_observed_ts_ms: None,
                schedule_observed_ts_ms: None,
                last_price: Some(100.0),
                mark_price: Some(101.0),
                index_price: Some(100.5),
                bid1_price: Some(100.0),
                ask1_price: Some(102.0),
                bid1_size: Some(1.0),
                ask1_size: Some(1.0),
                open_interest: None,
                open_interest_value: None,
                turnover_24h: None,
                volume_24h: None,
                funding_rate: None,
                next_funding_time_ms: None,
            },
        );
        let symbols = vec!["BTCUSDT".to_owned()];
        let marks = worker.current_marks(&symbols, 2 * DAY_MS + 30_000);
        assert_eq!(marks.len(), 1);
        assert_eq!(marks[0].observed_ts_ms, 2 * DAY_MS);
        assert!(worker
            .current_marks(&symbols, 2 * DAY_MS + 30_001)
            .is_empty());
    }

    #[test]
    fn presettlement_expiry_uses_the_older_schedule_clock() {
        let worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        let schedule_clock = 10 * DAY_MS;
        let observed_ts_ms = schedule_clock + worker.config.sources.mark_max_age_ms - 1;
        let row = TickerObservation {
            symbol: "BTCUSDT".into(),
            observed_ts_ms,
            available_at_ms: observed_ts_ms,
            mark_observed_ts_ms: Some(observed_ts_ms),
            funding_observed_ts_ms: Some(observed_ts_ms),
            schedule_observed_ts_ms: Some(schedule_clock),
            last_price: Some(100.0),
            mark_price: Some(100.0),
            index_price: Some(100.0),
            bid1_price: Some(99.0),
            ask1_price: Some(101.0),
            bid1_size: Some(1.0),
            ask1_size: Some(1.0),
            open_interest: None,
            open_interest_value: None,
            turnover_24h: None,
            volume_24h: None,
            funding_rate: Some(-0.001),
            next_funding_time_ms: Some(observed_ts_ms + 1),
        };

        let (_, presettlement) = worker.public_market_rows(&[row], observed_ts_ms);

        assert_eq!(presettlement.len(), 1);
        assert_eq!(presettlement[0].observed_ts_ms, schedule_clock);
        assert_eq!(
            presettlement[0]
                .observed_ts_ms
                .saturating_add(worker.config.sources.mark_max_age_ms),
            schedule_clock + worker.config.sources.mark_max_age_ms
        );
        assert!(
            presettlement[0]
                .observed_ts_ms
                .saturating_add(worker.config.sources.mark_max_age_ms)
                < observed_ts_ms.saturating_add(worker.config.sources.mark_max_age_ms)
        );
    }

    #[test]
    fn late_rest_ticker_cannot_roll_back_ws_state_or_output_clock() {
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        let mut ws = ticker_wire("BTCUSDT", 110.0);
        ws.mark_observed_ts_ms = Some(3_000);
        let first = worker
            .apply(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 3_500,
                available_at_ms: 3_500,
                rows: vec![ws],
            })
            .unwrap();
        assert_eq!(first.len(), 1);

        let second = worker
            .apply(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 2_000,
                available_at_ms: 4_000,
                rows: vec![ticker_wire("BTCUSDT", 90.0)],
            })
            .unwrap();
        assert_eq!(worker.state.tickers["BTCUSDT"].mark_price, Some(110.0));
        assert_eq!(worker.state.tickers["BTCUSDT"].observed_ts_ms, 3_500);
        assert_eq!(second.len(), 1);
        assert_eq!(second[0].observed_wall_ts_ms, 3_500);
        let envelope: SignalPayloadEnvelope = serde_json::from_slice(&second[0].payload).unwrap();
        let ObservationPayload::MarketSnapshot { tickers, marks, .. } = envelope.payload else {
            panic!("expected market snapshot");
        };
        assert_eq!(tickers[0].mark_price, Some(110.0));
        assert_eq!(tickers[0].observed_ts_ms, 3_500);
        assert_eq!(marks[0].mark_px, 110.0);
    }

    #[test]
    fn ticker_snapshot_already_expired_at_delivery_is_not_sequenced() {
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        let observed_ts_ms = 2 * DAY_MS;
        let output = worker
            .apply(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms,
                available_at_ms: observed_ts_ms + 30_001,
                rows: vec![ticker_wire("BTCUSDT", 100.0)],
            })
            .unwrap();
        assert!(output.is_empty());
        assert_eq!(worker.state.carry_output_sequence, 0);
        assert_eq!(worker.state.tickers["BTCUSDT"].mark_price, Some(100.0));
    }

    #[test]
    fn duplicate_funding_is_stored_once_and_not_reemitted() {
        let config = test_config();
        let universe = test_universe();
        let mut worker = SignalWorker::with_universe(config, universe).unwrap();
        worker.state.last_carry_decision_ts_ms = Some(DAY_MS - 1);
        let wire = BybitFundingWire {
            funding_rate_timestamp: serde_json::Value::from(DAY_MS),
            funding_rate: serde_json::Value::from("-0.001"),
            funding_interval_hour: Some(serde_json::Value::from(8)),
        };
        let first = worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 2 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: true,
                rows: vec![wire.clone()],
            })
            .unwrap();
        assert_eq!(first.len(), 1);
        let payload: SignalPayloadEnvelope = serde_json::from_slice(&first[0].payload).unwrap();
        let ObservationPayload::FundingUpdate {
            decision_ts_ms,
            settled_funding,
        } = payload.payload
        else {
            panic!("expected funding update");
        };
        assert_eq!(decision_ts_ms, DAY_MS - 1);
        assert_eq!(settled_funding.len(), 1);
        let duplicate = worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                symbol: "BTCUSDT".into(),
                available_at_ms: 2 * DAY_MS + 1,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: true,
                rows: vec![wire],
            })
            .unwrap();
        assert!(duplicate.is_empty());
        assert_eq!(worker.state.funding["BTCUSDT"].len(), 1);
        assert_eq!(
            worker.state.funding["BTCUSDT"][&DAY_MS].available_at_ms,
            2 * DAY_MS
        );
    }

    #[test]
    fn funding_lifecycle_emits_only_rows_after_the_bound_decision() {
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
        let observations = worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                symbol: "BTCUSDT".into(),
                available_at_ms: 4 * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: true,
                rows: vec![
                    BybitFundingWire {
                        funding_rate_timestamp: Value::from(DAY_MS),
                        funding_rate: Value::from("-0.001"),
                        funding_interval_hour: Some(Value::from(8)),
                    },
                    BybitFundingWire {
                        funding_rate_timestamp: Value::from(3 * DAY_MS),
                        funding_rate: Value::from("-0.002"),
                        funding_interval_hour: Some(Value::from(8)),
                    },
                ],
            })
            .unwrap();
        assert_eq!(observations.len(), 1);
        assert_eq!(observations[0].kind, "funding_update");
        assert_eq!(observations[0].observed_wall_ts_ms, 3 * DAY_MS);
        let payload: SignalPayloadEnvelope =
            serde_json::from_slice(&observations[0].payload).unwrap();
        let ObservationPayload::FundingUpdate {
            decision_ts_ms,
            settled_funding,
        } = payload.payload
        else {
            panic!("expected funding update");
        };
        assert_eq!(decision_ts_ms, 2 * DAY_MS);
        assert_eq!(settled_funding.len(), 1);
        assert_eq!(settled_funding[0].settlement_ts_ms, 3 * DAY_MS);
        assert_eq!(worker.state.funding["BTCUSDT"].len(), 2);
    }

    #[test]
    fn carry_scorer_catchup_is_bounded_ordered_and_has_no_market_payload() {
        let mut config = test_config();
        config.carry.minimum_decision_symbols = 1;
        let mut worker = SignalWorker::with_universe(config, test_universe()).unwrap();
        install_trading_instrument(&mut worker, "BTCUSDT");
        worker.state.last_carry_decision_ts_ms = Some(40 * DAY_MS);
        let history = worker.state.klines.entry("BTCUSDT".into()).or_default();
        for open_ts_ms in (5 * DAY_MS..50 * DAY_MS).step_by(HOUR_MS as usize) {
            history.insert(
                open_ts_ms,
                HourlyKline {
                    symbol: "BTCUSDT".into(),
                    open_ts_ms,
                    available_at_ms: 50 * DAY_MS,
                    open: 100.0,
                    high: 101.0,
                    low: 99.0,
                    close: 100.0 + open_ts_ms as f64 / DAY_MS as f64,
                    volume_base: 1.0,
                    turnover_quote: 100.0,
                },
            );
        }
        let funding = worker.state.funding.entry("BTCUSDT".into()).or_default();
        for settlement_ts_ms in (30 * DAY_MS..=42 * DAY_MS).step_by((8 * HOUR_MS) as usize) {
            funding.insert(
                settlement_ts_ms,
                SettledFunding {
                    symbol: "BTCUSDT".into(),
                    settlement_ts_ms,
                    available_at_ms: settlement_ts_ms,
                    rate: -0.001,
                    funding_interval_min: 480,
                },
            );
        }

        let observations = worker
            .apply(WireEvent::CarryScorerCatchupWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 50 * DAY_MS,
                decision_through_ms: 42 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(observations.len(), 2);
        assert_eq!(worker.state.last_carry_decision_ts_ms, Some(40 * DAY_MS));
        assert_eq!(worker.state.last_carry_scorer_ts_ms, Some(42 * DAY_MS));
        for (index, observation) in observations.iter().enumerate() {
            assert_eq!(observation.kind, "carry_scorer_catchup");
            let envelope: SignalPayloadEnvelope =
                serde_json::from_slice(&observation.payload).unwrap();
            let ObservationPayload::CarryScorerCatchup {
                decision_ts_ms,
                rows,
                rejections,
            } = envelope.payload
            else {
                panic!("expected scorer catch-up payload");
            };
            assert_eq!(decision_ts_ms, (41 + index as i64) * DAY_MS);
            assert_eq!(rows.len(), 1);
            assert!(rejections.is_empty());
        }
    }

    #[test]
    fn optional_whale_absence_keeps_carry_live_with_a_null_feature() {
        let mut worker =
            SignalWorker::with_universe(compact_feature_config(), test_universe()).unwrap();
        install_compact_history(&mut worker, 10);
        assert!(worker.state.whales.is_empty());
        let observations = worker
            .apply(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 10 * DAY_MS,
                data_through_ms: 10 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        let carry = observations
            .iter()
            .find(|observation| observation.kind == "carry_feature_batch")
            .expect("optional whale absence must not suppress the CARRY decision");
        let envelope: SignalPayloadEnvelope = serde_json::from_slice(&carry.payload).unwrap();
        let ObservationPayload::CarryFeatureBatch { rows, .. } = envelope.payload else {
            panic!("expected CARRY feature payload");
        };
        assert!(!rows.is_empty());
        assert!(rows.iter().all(|row| row.d_tt_ls_3d.is_none()));
        assert_eq!(worker.state.last_carry_decision_ts_ms, Some(10 * DAY_MS));
    }

    #[test]
    fn carry_catchup_uses_instrument_status_at_each_historical_decision() {
        let config = compact_feature_config();
        let mut universe = test_universe();
        universe.symbols = vec!["BTCUSDT".into(), "ETHUSDT".into()];
        universe.carry_symbols = universe.symbols.clone();
        let mut worker = SignalWorker::with_universe(config, universe).unwrap();
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS,
                rows: vec![
                    trading_instrument_wire("BTCUSDT", DAY_MS),
                    trading_instrument_wire("ETHUSDT", DAY_MS),
                ],
            })
            .unwrap();
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 10 * DAY_MS,
                available_at_ms: 10 * DAY_MS,
                rows: vec![
                    closed_instrument_wire("BTCUSDT", DAY_MS, 10 * DAY_MS),
                    trading_instrument_wire("ETHUSDT", DAY_MS),
                ],
            })
            .unwrap();
        assert_eq!(
            worker.state.instrument_trading_intervals["BTCUSDT"][0].trading_through_ms,
            Some(10 * DAY_MS)
        );
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 3,
                observed_ts_ms: 11 * DAY_MS,
                available_at_ms: 11 * DAY_MS,
                rows: vec![
                    closed_instrument_wire("BTCUSDT", DAY_MS, 10 * DAY_MS),
                    trading_instrument_wire("ETHUSDT", DAY_MS),
                ],
            })
            .unwrap();
        assert_eq!(
            worker.state.instruments["BTCUSDT"].status.as_deref(),
            Some("Closed")
        );
        assert!(!worker
            .state
            .instrument_status_unknown_since_ms
            .contains_key("BTCUSDT"));
        assert_eq!(
            worker.state.instrument_trading_intervals["BTCUSDT"],
            vec![InstrumentTradingInterval {
                trading_from_ms: DAY_MS,
                trading_through_ms: Some(10 * DAY_MS),
            }]
        );
        install_compact_history(&mut worker, 13);
        clone_symbol_history(&mut worker, "BTCUSDT", "ETHUSDT");
        worker.state.instruments.remove("BTCUSDT");
        worker.state.instrument_trading_intervals.insert(
            "BTCUSDT".into(),
            vec![InstrumentTradingInterval {
                trading_from_ms: DAY_MS,
                trading_through_ms: Some(10 * DAY_MS),
            }],
        );
        worker.state.instrument_trading_intervals.insert(
            "ETHUSDT".into(),
            vec![InstrumentTradingInterval {
                trading_from_ms: DAY_MS,
                trading_through_ms: None,
            }],
        );
        worker.state.last_carry_decision_ts_ms = Some(7 * DAY_MS);
        worker.state.last_carry_scorer_ts_ms = Some(7 * DAY_MS);

        let observations = worker
            .apply(WireEvent::CarryScorerCatchupWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: 4,
                observed_ts_ms: 12 * DAY_MS,
                decision_through_ms: 11 * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(observations.len(), 4);
        for (index, observation) in observations.iter().enumerate() {
            let envelope: SignalPayloadEnvelope =
                serde_json::from_slice(&observation.payload).unwrap();
            let ObservationPayload::CarryScorerCatchup {
                decision_ts_ms,
                rows,
                rejections,
            } = envelope.payload
            else {
                panic!("expected scorer catch-up payload");
            };
            let day = 8 + index as i64;
            assert_eq!(decision_ts_ms, day * DAY_MS);
            if day < 10 {
                assert_eq!(rows.len(), 2);
                assert!(rejections.is_empty());
            } else {
                assert_eq!(rows.len(), 1);
                assert_eq!(rows[0].symbol, "ETHUSDT");
                assert_eq!(rejections.len(), 1);
                assert_eq!(rejections[0].symbol, "BTCUSDT");
                assert_eq!(rejections[0].reason, "instrument_not_trading");
            }
            assert!(rows.iter().all(|row| row.d_tt_ls_3d.is_none()));
        }
        assert_eq!(worker.state.last_carry_decision_ts_ms, Some(7 * DAY_MS));
        assert_eq!(worker.state.last_carry_scorer_ts_ms, Some(11 * DAY_MS));
    }

    #[test]
    fn missing_then_recovered_instrument_preserves_the_unknown_historical_gap() {
        let config = compact_feature_config();
        let universe = test_universe();
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS,
                rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
            })
            .unwrap();
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 5 * DAY_MS,
                available_at_ms: 5 * DAY_MS,
                rows: Vec::new(),
            })
            .unwrap();
        let interval = &worker.state.instrument_trading_intervals["BTCUSDT"][0];
        assert_eq!(interval.trading_from_ms, DAY_MS);
        assert_eq!(interval.trading_through_ms, Some(5 * DAY_MS));
        assert_eq!(
            worker.state.instrument_status_unknown_since_ms["BTCUSDT"],
            5 * DAY_MS
        );
        assert!(worker.was_trading_instrument_at("BTCUSDT", 4 * DAY_MS));
        assert!(!worker.was_trading_instrument_at("BTCUSDT", 5 * DAY_MS));
        assert!(!worker.is_trading_instrument("BTCUSDT"));

        let mut legacy_state = worker.state.clone();
        legacy_state
            .instrument_trading_intervals
            .get_mut("BTCUSDT")
            .unwrap()[0]
            .trading_through_ms = None;
        let restored_legacy = SignalWorker::restore(config.clone(), legacy_state).unwrap();
        assert_eq!(
            restored_legacy.state.instrument_trading_intervals["BTCUSDT"][0].trading_through_ms,
            Some(5 * DAY_MS)
        );

        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 3,
                observed_ts_ms: 7 * DAY_MS,
                available_at_ms: 7 * DAY_MS,
                rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
            })
            .unwrap();
        assert!(!worker
            .state
            .instrument_status_unknown_since_ms
            .contains_key("BTCUSDT"));
        assert_eq!(
            worker.state.instrument_trading_intervals["BTCUSDT"],
            vec![
                InstrumentTradingInterval {
                    trading_from_ms: DAY_MS,
                    trading_through_ms: Some(5 * DAY_MS),
                },
                InstrumentTradingInterval {
                    trading_from_ms: 7 * DAY_MS,
                    trading_through_ms: None,
                },
            ]
        );
        assert!(!worker.was_trading_instrument_at("BTCUSDT", 6 * DAY_MS));
        assert!(worker.was_trading_instrument_at("BTCUSDT", 7 * DAY_MS));
        assert!(worker.is_trading_instrument("BTCUSDT"));

        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        assert!(!restored.was_trading_instrument_at("BTCUSDT", 6 * DAY_MS));
        assert!(restored.was_trading_instrument_at("BTCUSDT", 7 * DAY_MS));
    }

    #[test]
    fn repeated_instrument_omission_recovery_survives_past_the_old_cap_and_restart() {
        let config = compact_feature_config();
        let universe = test_universe();
        let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        let base = 10 * DAY_MS;
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: base,
                available_at_ms: base,
                rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
            })
            .unwrap();

        let mut sequence = 2;
        for cycle in 0..40 {
            let missing_at_ms = base + (2 * cycle + 1) * HOUR_MS;
            worker
                .apply(WireEvent::BybitInstrumentSnapshot {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    observed_ts_ms: missing_at_ms,
                    available_at_ms: missing_at_ms,
                    rows: Vec::new(),
                })
                .unwrap();
            sequence += 1;
            let recovered_at_ms = missing_at_ms + HOUR_MS;
            worker
                .apply(WireEvent::BybitInstrumentSnapshot {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    observed_ts_ms: recovered_at_ms,
                    available_at_ms: recovered_at_ms,
                    rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
                })
                .unwrap();
            sequence += 1;
            assert!(!worker.was_trading_instrument_at("BTCUSDT", missing_at_ms));
            assert!(worker.was_trading_instrument_at("BTCUSDT", recovered_at_ms));
        }

        let expected = worker.state.instrument_trading_intervals["BTCUSDT"].clone();
        assert_eq!(expected.len(), 41);
        assert_eq!(expected[0].trading_through_ms, Some(base + HOUR_MS));
        assert_eq!(expected[40].trading_from_ms, base + 80 * HOUR_MS);
        assert_eq!(expected[40].trading_through_ms, None);

        let mut restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        assert_eq!(
            restored.state.instrument_trading_intervals["BTCUSDT"],
            expected
        );
        let missing_at_ms = base + 81 * HOUR_MS;
        restored
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms: missing_at_ms,
                available_at_ms: missing_at_ms,
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
        let recovered_at_ms = missing_at_ms + HOUR_MS;
        restored
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms: recovered_at_ms,
                available_at_ms: recovered_at_ms,
                rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
            })
            .unwrap();
        assert_eq!(
            restored.state.instrument_trading_intervals["BTCUSDT"].len(),
            42
        );
        assert!(!restored.was_trading_instrument_at("BTCUSDT", missing_at_ms));
        assert!(restored.was_trading_instrument_at("BTCUSDT", recovered_at_ms));
    }

    #[test]
    fn restore_preserves_history_across_operational_changes_and_resets_only_changed_physics() {
        let config = test_config();
        let universe = test_universe();
        let mut state = SignalWorker::with_universe(config.clone(), universe.clone())
            .unwrap()
            .state
            .clone();
        state.last_input_sequence = 17;
        state.long_output_sequence = 3;
        state.carry_output_sequence = 5;
        state.last_long_feature_ts_ms = Some(9 * DAY_MS);
        state.last_carry_decision_ts_ms = Some(9 * DAY_MS);
        state.last_carry_upcoming_ts_ms = Some(10 * DAY_MS);

        let mut operational = config.clone();
        operational.identity.operational_profile_sha256 = "3".repeat(64);
        operational.identity.engine_config_sha256 = "4".repeat(64);
        let restored = SignalWorker::restore(operational, state.clone()).unwrap();
        assert_eq!(restored.state.last_input_sequence, 17);
        assert_eq!(restored.state.long_output_sequence, 3);
        assert_eq!(restored.state.carry_output_sequence, 5);
        assert_eq!(restored.state.last_long_feature_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_decision_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_scorer_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, Some(10 * DAY_MS));

        let mut mark_physics = config.clone();
        mark_physics.sources.mark_max_age_ms += 1;
        mark_physics.identity.long_decision_fingerprint = "1".repeat(64);
        mark_physics.identity.carry_decision_fingerprint = "2".repeat(64);
        let restored = SignalWorker::restore(mark_physics, state.clone()).unwrap();
        assert_eq!(restored.state.last_input_sequence, 17);
        assert_eq!(restored.state.long_output_sequence, 3);
        assert_eq!(restored.state.carry_output_sequence, 5);
        assert_eq!(restored.state.last_long_feature_ts_ms, None);
        assert_eq!(restored.state.last_carry_decision_ts_ms, None);
        assert_eq!(restored.state.last_carry_scorer_ts_ms, None);
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, None);

        let mut long_changed = config.clone();
        long_changed.long.regime_sma_days += 1;
        let restored = SignalWorker::restore(long_changed, state.clone()).unwrap();
        assert_eq!(restored.state.last_long_feature_ts_ms, None);
        assert_eq!(restored.state.last_carry_decision_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_scorer_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, Some(10 * DAY_MS));

        let mut carry_changed = config;
        carry_changed.identity.carry_decision_fingerprint = "0".repeat(64);
        let restored = SignalWorker::restore(carry_changed, state.clone()).unwrap();
        assert_eq!(restored.state.last_long_feature_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_decision_ts_ms, None);
        assert_eq!(restored.state.last_carry_scorer_ts_ms, None);
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, None);
        assert_eq!(restored.state.last_input_sequence, 17);

        let mut source_changed = test_config();
        source_changed.routing.source = "different_source".into();
        let error = match SignalWorker::restore(source_changed, state) {
            Ok(_) => panic!("a new source cannot inherit another source's sequence"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("source contract has drifted"));
    }

    #[test]
    fn pending_multi_output_transaction_recovers_every_crash_boundary() {
        let config = test_config();
        let universe = test_universe();
        let initial = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
        let prior_json = serde_json::to_vec(initial.state()).unwrap();
        let mut next_state = initial.state().clone();
        next_state.last_input_sequence = 1;
        next_state.long_output_sequence = 1;
        next_state.carry_output_sequence = 1;
        next_state.last_observed_ts_ms = 2 * DAY_MS;
        let next_json = serde_json::to_vec(&next_state).unwrap();
        let observations = [
            make_observation(
                &config,
                &universe,
                &initial.state.source_generation,
                true,
                1,
                "long_feature_batch",
                2 * DAY_MS,
                2 * DAY_MS + 1,
                empty_readiness(),
                Vec::new(),
            )
            .unwrap(),
            make_observation(
                &config,
                &universe,
                &initial.state.source_generation,
                false,
                1,
                "carry_feature_batch",
                2 * DAY_MS,
                2 * DAY_MS + 1,
                empty_readiness(),
                Vec::new(),
            )
            .unwrap(),
        ];
        let observation_json = observations
            .iter()
            .map(|row| serde_json::to_string(row).unwrap())
            .collect::<Vec<_>>();
        let transaction = PendingTransaction {
            schema_version: SCHEMA_VERSION,
            prior_state_sha256: sha256_hex(&prior_json),
            next_state_sha256: sha256_hex(&next_json),
            observation_json: observation_json.clone(),
        };

        for phase in 0..=5 {
            let root = temporary_root(&format!("crash-{phase}"));
            let state_dir = root.join("state");
            let spool_dir = root.join("spool");
            let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
            let pending = AtomicJsonStore::new(state_dir.join("pending-transaction.json"));
            let pending_next = AtomicJsonStore::new(state_dir.join("pending-next-state.json"));
            let spool = SpoolWriter::new(&spool_dir).unwrap();
            checkpoint.save_bytes(&prior_json).unwrap();
            if phase > 0 {
                pending_next.save_bytes(&next_json).unwrap();
            }
            if phase > 1 {
                pending.save(&transaction).unwrap();
            }
            if phase > 2 {
                spool.write_encoded(observation_json[0].as_bytes()).unwrap();
            }
            if phase > 3 {
                spool.write_encoded(observation_json[1].as_bytes()).unwrap();
            }
            if phase > 4 {
                checkpoint.replace_from(&pending_next).unwrap();
            }

            let durable = DurableSignalWorker::open_with_universe(
                config.clone(),
                universe.clone(),
                &state_dir,
                &spool_dir,
            )
            .unwrap();
            if phase <= 1 {
                assert_eq!(durable.worker.state.last_input_sequence, 0);
                assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 0);
                assert!(!pending_next.path().exists());
            } else {
                assert_eq!(durable.worker.state.last_input_sequence, 1);
                assert_eq!(durable.worker.state.long_output_sequence, 1);
                assert_eq!(durable.worker.state.carry_output_sequence, 1);
                assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 2);
                assert!(!pending.path().exists());
                assert_eq!(checkpoint.load_bytes().unwrap().unwrap(), next_json);
                drop(durable);
                let reopened = DurableSignalWorker::open_with_universe(
                    config.clone(),
                    universe.clone(),
                    &state_dir,
                    &spool_dir,
                )
                .unwrap();
                assert_eq!(reopened.worker.state.last_input_sequence, 1);
                assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 2);
            }
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn five_second_samples_journal_without_rewriting_the_full_checkpoint() {
        let config = test_config();
        let universe = test_universe();
        let root = temporary_root("hot-journal");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));

        let mut durable = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        let initial = durable.durability_metrics().unwrap();
        let checkpoint_hash = checkpoint.sha256().unwrap().unwrap();
        for sequence in 1..=12_u64 {
            let observed_ts_ms = 2 * DAY_MS + i64::try_from(sequence).unwrap() * 5_000;
            durable
                .apply_and_commit(WireEvent::BybitTickerSnapshot {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    observed_ts_ms,
                    available_at_ms: observed_ts_ms + 1,
                    rows: vec![ticker_wire("BTCUSDT", 100.0 + sequence as f64)],
                })
                .unwrap();
        }
        let hot = durable.durability_metrics().unwrap();
        assert_eq!(
            hot.checkpoint_writes_session,
            initial.checkpoint_writes_session
        );
        assert_eq!(checkpoint.sha256().unwrap().unwrap(), checkpoint_hash);
        assert_eq!(hot.journal_entries_retained, 12);
        assert!(hot.journal_bytes > 0);
        assert_eq!(hot.spool_files, 1);
        assert_eq!(hot.replaceable_outputs_coalesced, 11);
        assert_eq!(
            checkpoint
                .load::<WorkerState>()
                .unwrap()
                .unwrap()
                .last_input_sequence,
            0
        );
        drop(durable);

        let restored =
            DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir)
                .unwrap();
        assert_eq!(restored.worker.state.last_input_sequence, 12);
        assert_eq!(restored.durability_metrics().unwrap().journal_bytes, 0);
        assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 1);
        assert_eq!(
            restored
                .durability_metrics()
                .unwrap()
                .replaceable_outputs_coalesced,
            0,
            "session metrics reset after recovery"
        );
        assert_eq!(
            checkpoint
                .load::<WorkerState>()
                .unwrap()
                .unwrap()
                .last_input_sequence,
            12
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn ticker_field_clock_survives_journal_replay_without_extending_ttl() {
        let config = test_config();
        let universe = test_universe();
        let root = temporary_root("ticker-field-clock");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let field_ts = 2 * DAY_MS;
        let mut wire = ticker_wire("BTCUSDT", 100.0);
        wire.mark_observed_ts_ms = Some(field_ts);
        {
            let mut durable = DurableSignalWorker::open_with_universe(
                config.clone(),
                universe.clone(),
                &state_dir,
                &spool_dir,
            )
            .unwrap();
            durable
                .apply_and_commit(WireEvent::BybitTickerSnapshot {
                    schema_version: SCHEMA_VERSION,
                    sequence: 1,
                    observed_ts_ms: field_ts + 20_000,
                    available_at_ms: field_ts + 20_001,
                    rows: vec![wire],
                })
                .unwrap();
        }
        let restored =
            DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir)
                .unwrap();
        let symbols = vec!["BTCUSDT".to_owned()];
        assert_eq!(
            restored
                .worker()
                .current_marks(&symbols, field_ts + 30_000)
                .len(),
            1
        );
        assert!(restored
            .worker()
            .current_marks(&symbols, field_ts + 30_001)
            .is_empty());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn bounded_source_deltas_compact_into_one_checkpoint() {
        let config = test_config();
        let universe = test_universe();
        let root = temporary_root("journal-compaction");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let mut durable =
            DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir)
                .unwrap();
        let initial_writes = durable
            .durability_metrics()
            .unwrap()
            .checkpoint_writes_session;
        durable
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS + 1,
                rows: vec![ticker_wire("BTCUSDT", 100.0)],
            })
            .unwrap();
        durable
            .apply_and_commit(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 2 * DAY_MS + 2,
                available_at_ms: 2 * DAY_MS + 3,
                rows: Vec::new(),
            })
            .unwrap();
        let journaled = durable.durability_metrics().unwrap();
        assert_eq!(journaled.checkpoint_writes_session, initial_writes);
        assert_eq!(journaled.journal_entries_retained, 2);
        assert!(journaled.journal_bytes > 0);
        durable.compact_current_checkpoint(&[]).unwrap();
        let compacted = durable.durability_metrics().unwrap();
        assert_eq!(compacted.checkpoint_writes_session, initial_writes + 1);
        assert_eq!(compacted.journal_bytes, 0);
        assert_eq!(
            AtomicJsonStore::new(state_dir.join("checkpoint.json"))
                .load::<WorkerState>()
                .unwrap()
                .unwrap()
                .last_input_sequence,
            2
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn durable_checkpoint_owns_the_output_source_generation() {
        let config = test_config();
        let universe = test_universe();
        let root = temporary_root("source-generation");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");

        let mut first = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        let generation = first.worker.state.source_generation.clone();
        let first_rows = first
            .apply_and_commit(WireEvent::Watermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
            })
            .unwrap();
        assert_eq!(first_rows.len(), 1);
        assert_eq!(first_rows[0].sequence, 1);
        assert!(first_rows[0].source.contains(&generation));
        drop(first);

        let mut restored = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        assert_eq!(restored.worker.state.source_generation, generation);
        let restored_rows = restored
            .apply_and_commit(WireEvent::Watermark {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 2 * DAY_MS + 1,
            })
            .unwrap();
        assert!(restored_rows.is_empty());
        assert_eq!(restored.worker.state.carry_output_sequence, 1);
        drop(restored);

        std::fs::remove_dir_all(&state_dir).unwrap();
        std::fs::remove_dir_all(&spool_dir).unwrap();
        let mut replacement =
            DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir)
                .unwrap();
        let replacement_generation = replacement.worker.state.source_generation.clone();
        assert_ne!(replacement_generation, generation);
        let replacement_rows = replacement
            .apply_and_commit(WireEvent::Watermark {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
            })
            .unwrap();
        assert_eq!(replacement_rows.len(), 1);
        assert_eq!(replacement_rows[0].sequence, 1);
        assert!(replacement_rows[0].source.contains(&replacement_generation));
        assert_ne!(replacement_rows[0].source, first_rows[0].source);

        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn checkpoint_without_a_generation_adopts_a_new_output_namespace() {
        let config = test_config();
        let universe = test_universe();
        let root = temporary_root("adopt-source-generation");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
        let mut state = SignalWorker::with_universe(config.clone(), universe.clone())
            .unwrap()
            .state
            .clone();
        state.last_input_sequence = 17;
        state.long_output_sequence = 3;
        state.carry_output_sequence = 5;
        state.last_long_feature_ts_ms = Some(9 * DAY_MS);
        state.last_carry_decision_ts_ms = Some(9 * DAY_MS);
        state.last_carry_upcoming_ts_ms = Some(10 * DAY_MS);
        let mut legacy = serde_json::to_value(state).unwrap();
        legacy.as_object_mut().unwrap().remove("source_generation");
        checkpoint.save(&legacy).unwrap();

        let durable =
            DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir)
                .unwrap();
        assert_ne!(
            durable.worker.state.source_generation,
            REPLAY_SOURCE_GENERATION
        );
        assert_eq!(durable.worker.state.last_input_sequence, 17);
        assert_eq!(durable.worker.state.long_output_sequence, 0);
        assert_eq!(durable.worker.state.carry_output_sequence, 0);
        assert_eq!(durable.worker.state.last_long_feature_ts_ms, None);
        assert_eq!(durable.worker.state.last_carry_decision_ts_ms, None);
        assert_eq!(durable.worker.state.last_carry_upcoming_ts_ms, None);
        let persisted: WorkerState = checkpoint.load().unwrap().unwrap();
        assert_eq!(
            persisted.source_generation,
            durable.worker.state.source_generation
        );

        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn signal_payload_accepts_exactly_16_mib_and_refuses_the_next_byte() {
        let config = test_config();
        let universe = test_universe();
        let baseline = make_observation(
            &config,
            &universe,
            REPLAY_SOURCE_GENERATION,
            false,
            1,
            "readiness",
            2 * DAY_MS,
            2 * DAY_MS + 1,
            readiness(String::new()),
            Vec::new(),
        )
        .unwrap();
        let fill = MAX_SIGNAL_OBSERVATION_BYTES - baseline.payload.len();
        let exact = make_observation(
            &config,
            &universe,
            REPLAY_SOURCE_GENERATION,
            false,
            1,
            "readiness",
            2 * DAY_MS,
            2 * DAY_MS + 1,
            readiness("x".repeat(fill)),
            Vec::new(),
        )
        .unwrap();
        assert_eq!(MAX_SIGNAL_OBSERVATION_BYTES, 16 * 1024 * 1024);
        assert_eq!(exact.payload.len(), MAX_SIGNAL_OBSERVATION_BYTES);
        assert!(
            u64::try_from(serde_json::to_vec(&exact).unwrap().len()).unwrap()
                <= MAX_SPOOL_OBSERVATION_FILE_BYTES
        );
        drop(exact);
        let error = make_observation(
            &config,
            &universe,
            REPLAY_SOURCE_GENERATION,
            false,
            1,
            "readiness",
            2 * DAY_MS,
            2 * DAY_MS + 1,
            readiness("x".repeat(fill + 1)),
            Vec::new(),
        )
        .unwrap_err();
        assert!(error.to_string().contains("16777216 bytes"));
    }

    fn gate_row(symbol: &str, score: f64, trigger_ts_ms: i64) -> crate::model::LlmGateCandidate {
        crate::model::LlmGateCandidate {
            symbol: symbol.into(),
            score,
            band: "core".into(),
            trigger_ts_ms,
            trigger_price: 100.0,
            atr_pct: 0.05,
            sigma_daily_30d: Some(0.04),
            turnover_rank: Some(4.0),
            trigger_window_h: Some(4),
        }
    }

    #[test]
    fn an_unresolved_worker_refuses_every_input_until_a_universe_snapshot_arrives() {
        let mut worker = SignalWorker::new(test_config()).unwrap();
        assert!(!crate::universe::universe_is_resolved(
            &worker.state.universe
        ));
        assert_eq!(worker.state.universe.environment, "demo");
        assert_eq!(worker.state.universe.endpoint, "api-demo.bybit.com");
        let refused = worker.apply(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: DAY_MS,
            available_at_ms: DAY_MS,
            rows: vec![ticker_wire("BTCUSDT", 100.0)],
        });
        assert!(refused.is_err());
        let resolved = worker
            .apply(WireEvent::UniverseSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                universe: test_universe(),
            })
            .unwrap();
        assert!(resolved.is_empty());
        assert!(crate::universe::universe_is_resolved(
            &worker.state.universe
        ));
        let accepted = worker
            .apply(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: DAY_MS + 2,
                available_at_ms: DAY_MS + 2,
                rows: vec![ticker_wire("BTCUSDT", 100.0)],
            })
            .unwrap();
        assert_eq!(accepted.len(), 1);
    }

    #[test]
    fn a_universe_with_new_membership_replaces_the_old_one_and_drops_its_symbols() {
        let mut first = test_universe();
        first.symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
        first.long_symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
        first.carry_symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
        let mut worker = SignalWorker::with_universe(test_config(), first).unwrap();
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: DAY_MS + 1,
                available_at_ms: DAY_MS + 1,
                rows: vec![
                    trading_instrument_wire("AAAUSDT", 1),
                    trading_instrument_wire("BTCUSDT", 1),
                    trading_instrument_wire("ETHUSDT", 1),
                ],
            })
            .unwrap();
        worker
            .apply(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: DAY_MS + 2,
                available_at_ms: DAY_MS + 2,
                rows: vec![ticker_wire("AAAUSDT", 1.0), ticker_wire("BTCUSDT", 100.0)],
            })
            .unwrap();
        assert!(worker.state.tickers.contains_key("AAAUSDT"));
        assert!(worker.state.instruments.contains_key("AAAUSDT"));

        let mut second = test_universe();
        second.snapshot_ts_ms = DAY_MS + 3;
        second.available_at_ms = DAY_MS + 3;
        second.artifact_sha256 = "3".repeat(64);
        let out = worker
            .apply(WireEvent::UniverseSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 3,
                universe: second.clone(),
            })
            .unwrap();
        assert!(out.is_empty());
        assert_eq!(worker.state.universe, second);
        assert!(!worker.state.tickers.contains_key("AAAUSDT"));
        assert!(!worker.state.instruments.contains_key("AAAUSDT"));
        assert!(worker.state.tickers.contains_key("BTCUSDT"));

        // The same membership with a newer clock is installed without pruning
        // work, and a checkpoint from it restores under a config that names no
        // universe at all.
        let mut third = second.clone();
        third.snapshot_ts_ms = DAY_MS + 4;
        third.available_at_ms = DAY_MS + 4;
        worker
            .apply(WireEvent::UniverseSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 4,
                universe: third.clone(),
            })
            .unwrap();
        assert_eq!(worker.state.universe.snapshot_ts_ms, DAY_MS + 4);
        let restored = SignalWorker::restore(test_config(), worker.state.clone()).unwrap();
        assert_eq!(restored.state.universe, third);
    }

    /// The spool preflight must count the gate's row, or every gate
    /// publication is refused as an underestimated batch and the worker exits.
    #[test]
    fn a_gate_publication_passes_the_spool_preflight() {
        let root = temporary_root("gate-preflight");
        let mut durable = DurableSignalWorker::open_with_universe(
            test_config(),
            test_universe(),
            root.join("state"),
            root.join("spool"),
        )
        .unwrap();
        let read_at_ms = 10 * DAY_MS;
        let decision_ts_ms = read_at_ms - 60_000;
        let out = durable
            .apply_and_commit(WireEvent::LlmGateCandidates {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: decision_ts_ms,
                available_at_ms: read_at_ms,
                decision_ts_ms,
                valid_until_ms: decision_ts_ms + HOUR_MS,
                rows: vec![gate_row("BTCUSDT", 7.0, read_at_ms - 20 * 60_000)],
            })
            .unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].kind, "llm_gate_candidates");
        assert_eq!(
            std::fs::read_dir(root.join("spool"))
                .unwrap()
                .filter(|entry| {
                    entry
                        .as_ref()
                        .is_ok_and(|entry| entry.path().extension().is_some_and(|e| e == "json"))
                })
                .count(),
            1
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn a_gate_publication_becomes_one_long_observation_over_the_tradable_set() {
        let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
        let read_at_ms = 10 * DAY_MS;
        let decision_ts_ms = read_at_ms - 60_000;
        let out = worker
            .apply(WireEvent::LlmGateCandidates {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: decision_ts_ms,
                available_at_ms: read_at_ms,
                decision_ts_ms,
                valid_until_ms: decision_ts_ms + HOUR_MS,
                rows: vec![
                    gate_row("BTCUSDT", 7.0, read_at_ms - 20 * 60_000),
                    // Not in the tradable set: dropped, never an error.
                    gate_row("OTHERUSDT", 9.0, read_at_ms - 60_000),
                    // Below the score bar.
                    gate_row("BTCUSDT", 5.0, read_at_ms - 60_000),
                ],
            })
            .unwrap();
        assert_eq!(out.len(), 1);
        let observation = &out[0];
        assert_eq!(observation.kind, "llm_gate_candidates");
        assert_eq!(
            observation.destination,
            StrategyId(test_config().long_destination)
        );
        assert_eq!(observation.observed_wall_ts_ms, decision_ts_ms);
        assert_eq!(observation.available_wall_ts_ms, read_at_ms);
        assert_eq!(observation.sequence, 1);
        assert_eq!(
            observation
                .subscriptions
                .iter()
                .map(|row| row.symbol.as_str())
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["BTCUSDT"])
        );
        let envelope: SignalPayloadEnvelope = serde_json::from_slice(&observation.payload).unwrap();
        let ObservationPayload::LlmGateCandidates {
            decision_ts_ms: published,
            valid_until_ms,
            rows,
            ..
        } = envelope.payload
        else {
            panic!("expected gate candidates");
        };
        assert_eq!(published, decision_ts_ms);
        assert_eq!(valid_until_ms, decision_ts_ms + HOUR_MS);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].symbol, "BTCUSDT");
        assert_eq!(rows[0].score, 7.0);
        assert_eq!(worker.state.long_output_sequence, 1);

        // A stale trigger is filtered, and an empty publication still travels:
        // it is how the ledger withdraws standing candidates.
        let out = worker
            .apply(WireEvent::LlmGateCandidates {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: decision_ts_ms + 60_000,
                available_at_ms: read_at_ms + 60_000,
                decision_ts_ms: decision_ts_ms + 60_000,
                valid_until_ms: decision_ts_ms + 60_000 + HOUR_MS,
                rows: vec![gate_row("BTCUSDT", 8.0, read_at_ms - 2 * HOUR_MS)],
            })
            .unwrap();
        assert_eq!(out.len(), 1);
        let envelope: SignalPayloadEnvelope = serde_json::from_slice(&out[0].payload).unwrap();
        let ObservationPayload::LlmGateCandidates { rows, .. } = envelope.payload else {
            panic!("expected gate candidates");
        };
        assert!(rows.is_empty());
        assert_eq!(out[0].sequence, 2);
    }
}
