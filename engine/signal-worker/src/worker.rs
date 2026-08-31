use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::Path;

use engine_types::{
    Feed, StrategyId, Subscription, MAX_SIGNAL_OBSERVATION_BYTES, MAX_SIGNAL_SUBSCRIPTIONS,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::config::{sha256_hex, ConfigIdentity, SignalWorkerConfig};
use crate::features::{
    build_carry_features, build_carry_features_at, build_carry_replay_features,
    build_long_features, FundingHistory, KlineHistory, WhaleHistory,
};
use crate::model::{
    BinanceWhaleObservation, HourlyKline, InstrumentObservation, MarketMark, NormalizedObservation,
    ObservationPayload, PresettlementPublicObservation, Readiness, SettledFunding,
    SignalPayloadEnvelope, TickerObservation, UniverseIdentity, WireEvent,
};
use crate::normalize::{
    normalize_funding_rows, normalize_instruments, normalize_kline_rows, normalize_tickers,
    normalize_whales, validate_universe,
};
use crate::store::{AtomicJsonStore, SpoolWriter};
use crate::{DAY_MS, HOUR_MS, SCHEMA_VERSION};

const SOURCE_GENERATION_BYTES: usize = 16;
const REPLAY_SOURCE_GENERATION: &str = "00000000000000000000000000000000";
const SIGNAL_SOURCE_BYTES_MAX: usize = 256;

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
    pub last_carry_decision_ts_ms: Option<i64>,
    #[serde(default)]
    pub last_carry_upcoming_ts_ms: Option<i64>,
    pub klines: KlineHistory,
    pub funding: FundingHistory,
    pub whales: WhaleHistory,
    pub instruments: BTreeMap<String, InstrumentObservation>,
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
            last_carry_decision_ts_ms: None,
            last_carry_upcoming_ts_ms: None,
            klines: BTreeMap::new(),
            funding: BTreeMap::new(),
            whales: BTreeMap::new(),
            instruments: BTreeMap::new(),
            tickers: BTreeMap::new(),
        }
    }
}

#[derive(Clone)]
pub struct SignalWorker {
    config: SignalWorkerConfig,
    state: WorkerState,
}

impl SignalWorker {
    pub fn new(
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
        let observed = universe.available_at_ms;
        let universe = validate_universe(universe, observed)?;
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
        })
    }

    pub fn restore(
        config: SignalWorkerConfig,
        universe: UniverseIdentity,
        state: WorkerState,
    ) -> Result<Self, WorkerError> {
        if state.schema_version != SCHEMA_VERSION {
            return Err(WorkerError::state("checkpoint schema has drifted"));
        }
        if state.universe != universe {
            return Err(WorkerError::state(
                "checkpoint belongs to a different reviewed universe artifact",
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
        if state.source_generation.is_empty() {
            state.source_generation = random_source_generation()?;
            state.long_output_sequence = 0;
            state.carry_output_sequence = 0;
            state.last_long_feature_ts_ms = None;
            state.last_carry_decision_ts_ms = None;
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
        }
        if state.carry_feature_sha256 != carry_feature_sha256
            || state.config.carry_decision_fingerprint != config.identity.carry_decision_fingerprint
        {
            state.last_carry_decision_ts_ms = None;
            state.last_carry_upcoming_ts_ms = None;
        }
        state.config = config.identity.clone();
        state.long_feature_sha256 = long_feature_sha256;
        state.carry_feature_sha256 = carry_feature_sha256;
        Ok(Self { config, state })
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
        let mut observations = Vec::new();
        let event_sequence = event.sequence();
        match event {
            WireEvent::BybitKlineBatch {
                symbol,
                available_at_ms,
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
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
            }
            WireEvent::BybitFundingBatch {
                symbol,
                available_at_ms,
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
                if !inserted.is_empty() && self.state.last_carry_decision_ts_ms.is_some() {
                    let observed = inserted
                        .iter()
                        .map(|row| row.settlement_ts_ms)
                        .max()
                        .unwrap_or(available_at_ms);
                    observations.push(self.carry_observation(
                        "funding_update",
                        observed,
                        available_at_ms,
                        ObservationPayload::FundingUpdate {
                            settled_funding: inserted,
                        },
                        Vec::new(),
                    )?);
                }
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
            }
            WireEvent::BybitInstrumentSnapshot {
                observed_ts_ms,
                available_at_ms,
                rows,
                ..
            } => {
                self.state.instruments =
                    normalize_instruments(observed_ts_ms, available_at_ms, &rows)?
                        .into_iter()
                        .map(|row| (row.symbol.clone(), row))
                        .collect();
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
            }
            WireEvent::BybitTickerSnapshot {
                observed_ts_ms,
                available_at_ms,
                rows,
                ..
            } => {
                let normalized = normalize_tickers(observed_ts_ms, available_at_ms, &rows)?;
                self.state.tickers = normalized
                    .iter()
                    .cloned()
                    .map(|row| (row.symbol.clone(), row))
                    .collect();
                let carry_tickers: Vec<TickerObservation> = normalized
                    .into_iter()
                    .filter(|row| self.state.universe.carry_symbols.contains(&row.symbol))
                    .collect();
                let (marks, presettlement) =
                    self.public_market_rows(&carry_tickers, observed_ts_ms);
                if !marks.is_empty() || !presettlement.is_empty() {
                    observations.push(self.carry_observation(
                        "market_snapshot",
                        observed_ts_ms,
                        available_at_ms,
                        ObservationPayload::MarketSnapshot {
                            tickers: carry_tickers,
                            marks,
                            presettlement,
                        },
                        Vec::new(),
                    )?);
                }
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
            }
            WireEvent::BinanceWhaleBatch {
                available_at_ms,
                rows,
                ..
            } => {
                for row in normalize_whales(available_at_ms, &rows)? {
                    merge_whale(
                        self.state.whales.entry(row.symbol.clone()).or_default(),
                        row,
                    )?;
                }
                self.state.last_observed_ts_ms =
                    self.state.last_observed_ts_ms.max(available_at_ms);
            }
            WireEvent::UniverseSnapshot { universe, .. } => {
                let available_at_ms = universe.available_at_ms;
                let universe = validate_universe(universe, available_at_ms)?;
                if universe.environment != self.config.live.environment {
                    return Err(WorkerError::input(
                        "universe event environment disagrees with config",
                    ));
                }
                if universe.file_sha256 != self.state.universe.file_sha256
                    || universe.artifact_sha256 != self.state.universe.artifact_sha256
                {
                    return Err(WorkerError::input(
                        "live universe cannot replace the reviewed artifact",
                    ));
                }
                self.state.universe = universe;
            }
            WireEvent::Watermark { observed_ts_ms, .. } => {
                if observed_ts_ms <= 0 || observed_ts_ms < self.state.last_observed_ts_ms {
                    return Err(WorkerError::input("watermark moved backwards"));
                }
                self.state.last_observed_ts_ms = observed_ts_ms;
                observations.extend(self.build_at_watermark(observed_ts_ms)?);
                self.prune(observed_ts_ms);
            }
        }
        self.state.last_input_sequence = event_sequence;
        Ok(observations)
    }

    fn build_at_watermark(
        &mut self,
        observed_ts_ms: i64,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        let mut out = Vec::new();
        let long = build_long_features(
            &self.state.klines,
            &self.state.universe.long_symbols,
            observed_ts_ms,
            &self.config.long,
        );
        let long_ready = long.feature_ts_ms.is_some() && !long.rows.is_empty();
        if let Some(feature_ts_ms) = long.feature_ts_ms {
            if self.state.last_long_feature_ts_ms < Some(feature_ts_ms) && !long.rows.is_empty() {
                let marks = self.current_marks(&self.state.universe.long_symbols, observed_ts_ms);
                let subscriptions = ticker_subscriptions(&self.state.universe.long_symbols)?;
                out.push(self.long_observation(
                    "long_feature_batch",
                    feature_ts_ms,
                    observed_ts_ms,
                    ObservationPayload::LongFeatureBatch {
                        decision_ts_ms: observed_ts_ms,
                        feature_ts_ms,
                        rows: long.rows,
                        marks,
                        cold_start_fallback_count: long.fallback_count,
                        rejections: long.rejections.clone(),
                    },
                    subscriptions,
                )?);
                self.state.last_long_feature_ts_ms = Some(feature_ts_ms);
            }
        }
        let carry = build_carry_features(
            &self.state.klines,
            &self.state.funding,
            &self.state.whales,
            &self.state.universe.carry_symbols,
            observed_ts_ms,
            &self.config.carry,
        );
        if let Some(decision_ts_ms) = carry.decision_ts_ms {
            let current_is_new = self.state.last_carry_decision_ts_ms < Some(decision_ts_ms);
            let upcoming_ts_ms = decision_ts_ms + DAY_MS;
            let upcoming_rows = if upcoming_ts_ms <= observed_ts_ms
                && self.state.last_carry_upcoming_ts_ms < Some(upcoming_ts_ms)
            {
                build_carry_features_at(
                    &self.state.klines,
                    &self.state.funding,
                    &self.state.whales,
                    &self.state.universe.carry_symbols,
                    upcoming_ts_ms,
                    observed_ts_ms,
                    &self.config.carry,
                )
                .rows
            } else {
                Vec::new()
            };
            let upcoming_is_new = !upcoming_rows.is_empty();
            if (current_is_new || upcoming_is_new) && !carry.rows.is_empty() {
                let replay = build_carry_replay_features(
                    &self.state.klines,
                    &self.state.funding,
                    &self.state.whales,
                    &self.state.universe.carry_symbols,
                    decision_ts_ms,
                    observed_ts_ms,
                    &self.config.carry,
                );
                let marks = self.current_marks(&self.state.universe.carry_symbols, observed_ts_ms);
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
                let (_, presettlement) = self.public_market_rows(&ticker_rows, observed_ts_ms);
                let subscriptions = ticker_subscriptions(&self.state.universe.carry_symbols)?;
                out.push(self.carry_observation(
                    "carry_feature_batch",
                    observed_ts_ms,
                    observed_ts_ms,
                    ObservationPayload::CarryFeatureBatch {
                        decision_ts_ms,
                        rows: replay.rows,
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
                }
                if upcoming_is_new {
                    self.state.last_carry_upcoming_ts_ms = Some(upcoming_ts_ms);
                }
            }
        }
        if out.is_empty() {
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
                universe_ready: true,
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
                observed_ts_ms,
                observed_ts_ms,
                ObservationPayload::Readiness { readiness },
                Vec::new(),
            )?);
        }
        Ok(out)
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
            if let Some(mark_px) = row.mark_price {
                marks.push(MarketMark {
                    symbol: row.symbol.clone(),
                    observed_ts_ms,
                    mark_px,
                });
            }
            if let (Some(settlement_ts_ms), Some(running_rate)) =
                (row.next_funding_time_ms, row.funding_rate)
            {
                let remaining = settlement_ts_ms - observed_ts_ms;
                if (0..=self.config.carry.presettlement_window_ms).contains(&remaining) {
                    presettlement.push(PresettlementPublicObservation {
                        symbol: row.symbol.clone(),
                        observed_ts_ms,
                        settlement_ts_ms,
                        running_rate,
                        mark_px: row.mark_price,
                    });
                }
            }
        }
        marks.sort_by(|a, b| a.symbol.cmp(&b.symbol));
        presettlement.sort_by(|a, b| a.symbol.cmp(&b.symbol));
        (marks, presettlement)
    }

    fn current_marks(&self, symbols: &[String], observed_ts_ms: i64) -> Vec<MarketMark> {
        symbols
            .iter()
            .filter_map(|symbol| {
                self.state.tickers.get(symbol).and_then(|ticker| {
                    (ticker.available_at_ms <= observed_ts_ms
                        && observed_ts_ms.saturating_sub(ticker.observed_ts_ms)
                            <= self.config.sources.mark_max_age_ms)
                        .then_some(ticker)
                        .and_then(|ticker| {
                            ticker.mark_price.map(|mark_px| MarketMark {
                                symbol: symbol.clone(),
                                observed_ts_ms: ticker.observed_ts_ms,
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

    fn prune(&mut self, observed_ts_ms: i64) {
        let long_hours = self.config.long.cold_start_lookback_days.saturating_mul(24);
        let carry_feature_hours = usize::try_from(
            (self.config.carry.vol_window_hours + self.config.carry.vol_return_lag_hours).max(
                self.config.carry.turn_growth_lookback_hours + self.config.carry.adv_window_hours,
            ),
        )
        .unwrap_or(usize::MAX);
        let carry_hours = self
            .config
            .carry
            .minimum_replay_days
            .saturating_mul(24)
            .saturating_add(carry_feature_hours);
        let retain_hours = long_hours.max(carry_hours).saturating_add(48);
        let cutoff = observed_ts_ms.saturating_sub(
            i64::try_from(retain_hours)
                .unwrap_or(i64::MAX / HOUR_MS)
                .saturating_mul(HOUR_MS),
        );
        for rows in self.state.klines.values_mut() {
            *rows = rows.split_off(&cutoff);
        }
        let funding_cutoff = observed_ts_ms.saturating_sub(
            i64::try_from(retain_hours)
                .unwrap_or(i64::MAX / HOUR_MS)
                .saturating_mul(HOUR_MS),
        );
        for rows in self.state.funding.values_mut() {
            *rows = rows.split_off(&funding_cutoff);
        }
        let whale_cutoff = observed_ts_ms.saturating_sub(
            (self.config.carry.whale_change_lookback_hours
                + self.config.carry.whale_freshness_hours
                + 24)
                .saturating_mul(HOUR_MS),
        );
        for rows in self.state.whales.values_mut() {
            *rows = rows.split_off(&whale_cutoff);
        }
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

fn ticker_subscriptions(symbols: &[String]) -> Result<Vec<Subscription>, WorkerError> {
    let unique: BTreeSet<&str> = symbols.iter().map(String::as_str).collect();
    if unique.len() > MAX_SIGNAL_SUBSCRIPTIONS {
        return Err(WorkerError::config(format!(
            "signal universe requests {} ticker subscriptions; maximum is {}",
            unique.len(),
            MAX_SIGNAL_SUBSCRIPTIONS
        )));
    }
    Ok(unique
        .into_iter()
        .map(|symbol| Subscription {
            symbol: symbol.to_owned(),
            feed: Feed::Ticker,
        })
        .collect())
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
    next_state_json: String,
    observation_json: Vec<String>,
}

pub struct DurableSignalWorker {
    worker: SignalWorker,
    checkpoint: AtomicJsonStore,
    pending: AtomicJsonStore,
    spool: SpoolWriter,
}

impl DurableSignalWorker {
    pub fn open(
        config: SignalWorkerConfig,
        universe: UniverseIdentity,
        state_dir: impl AsRef<Path>,
        spool_dir: impl AsRef<Path>,
    ) -> Result<Self, WorkerError> {
        let checkpoint = AtomicJsonStore::new(state_dir.as_ref().join("checkpoint.json"));
        let pending = AtomicJsonStore::new(state_dir.as_ref().join("pending-transaction.json"));
        let spool = SpoolWriter::new(spool_dir.as_ref())?;
        let mut state_json = if let Some(bytes) = checkpoint.load_bytes()? {
            bytes
        } else {
            let initial = SignalWorker::new_with_source_generation(
                config.clone(),
                universe.clone(),
                random_source_generation()?,
            )?;
            let bytes = serde_json::to_vec(initial.state())
                .map_err(|error| WorkerError::json("encode initial checkpoint", error))?;
            checkpoint.save_bytes(&bytes)?;
            bytes
        };
        if let Some(transaction) = pending.load::<PendingTransaction>()? {
            state_json = finish_pending(&checkpoint, &pending, &spool, &state_json, &transaction)?;
        }
        let state: WorkerState = serde_json::from_slice(&state_json)
            .map_err(|error| WorkerError::json("parse worker checkpoint", error))?;
        let worker = SignalWorker::restore(config, universe, state)?;
        let adopted = serde_json::to_vec(worker.state())
            .map_err(|error| WorkerError::json("encode adopted checkpoint", error))?;
        if adopted != state_json {
            checkpoint.save_bytes(&adopted)?;
        }
        Ok(Self {
            worker,
            checkpoint,
            pending,
            spool,
        })
    }

    pub fn worker(&self) -> &SignalWorker {
        &self.worker
    }

    pub fn apply_and_commit(
        &mut self,
        event: WireEvent,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        self.apply_many_and_commit(std::iter::once(event))
    }

    pub fn apply_many_and_commit(
        &mut self,
        events: impl IntoIterator<Item = WireEvent>,
    ) -> Result<Vec<NormalizedObservation>, WorkerError> {
        let prior_json = serde_json::to_vec(self.worker.state())
            .map_err(|error| WorkerError::json("encode prior checkpoint", error))?;
        let mut next = self.worker.clone();
        let mut observations = Vec::new();
        let mut applied = false;
        for event in events {
            observations.extend(next.apply(event)?);
            applied = true;
        }
        if !applied {
            return Ok(Vec::new());
        }
        let next_json = serde_json::to_vec(next.state())
            .map_err(|error| WorkerError::json("encode next checkpoint", error))?;
        let observation_json = observations
            .iter()
            .map(|observation| {
                serde_json::to_string(observation)
                    .map_err(|error| WorkerError::json("encode pending observation", error))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let transaction = PendingTransaction {
            schema_version: SCHEMA_VERSION,
            prior_state_sha256: sha256_hex(&prior_json),
            next_state_sha256: sha256_hex(&next_json),
            next_state_json: String::from_utf8(next_json)
                .expect("serde_json always produces UTF-8"),
            observation_json,
        };
        self.pending.save(&transaction)?;
        for json in &transaction.observation_json {
            self.spool.write_encoded(json.as_bytes())?;
        }
        self.checkpoint
            .save_bytes(transaction.next_state_json.as_bytes())?;
        self.pending.remove()?;
        self.worker = next;
        Ok(observations)
    }
}

fn finish_pending(
    checkpoint: &AtomicJsonStore,
    pending_store: &AtomicJsonStore,
    spool: &SpoolWriter,
    current_state_json: &[u8],
    transaction: &PendingTransaction,
) -> Result<Vec<u8>, WorkerError> {
    if transaction.schema_version != SCHEMA_VERSION
        || sha256_hex(transaction.next_state_json.as_bytes()) != transaction.next_state_sha256
    {
        return Err(WorkerError::state(
            "pending transaction schema or next-state hash is invalid",
        ));
    }
    let current_hash = sha256_hex(current_state_json);
    if current_hash != transaction.prior_state_sha256
        && current_hash != transaction.next_state_sha256
    {
        return Err(WorkerError::state(
            "pending transaction does not follow the durable checkpoint",
        ));
    }
    for json in &transaction.observation_json {
        spool.write_encoded(json.as_bytes())?;
    }
    if current_hash == transaction.prior_state_sha256 {
        checkpoint.save_bytes(transaction.next_state_json.as_bytes())?;
    }
    pending_store.remove()?;
    Ok(transaction.next_state_json.as_bytes().to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{
        CarryFeatureConfig, LiveAcquisitionConfig, LongFeatureConfig, SignalRouting, SourceContract,
    };
    use crate::model::{BinanceWhaleObservation, BybitFundingWire, Readiness, UniverseMode};
    use crate::store::{AtomicJsonStore, SpoolWriter};
    use std::collections::BTreeMap;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

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
        let mut worker = SignalWorker::new(test_config(), test_universe()).unwrap();
        worker.state.tickers.insert(
            "BTCUSDT".into(),
            TickerObservation {
                symbol: "BTCUSDT".into(),
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS + 10,
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
    fn duplicate_funding_is_stored_once_and_not_reemitted() {
        let config = test_config();
        let universe = test_universe();
        let mut worker = SignalWorker::new(config, universe).unwrap();
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
                rows: vec![wire.clone()],
            })
            .unwrap();
        assert_eq!(first.len(), 1);
        let duplicate = worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                symbol: "BTCUSDT".into(),
                available_at_ms: 2 * DAY_MS + 1,
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
    fn restore_preserves_history_across_operational_changes_and_resets_only_changed_physics() {
        let config = test_config();
        let universe = test_universe();
        let mut state = SignalWorker::new(config.clone(), universe.clone())
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
        let restored = SignalWorker::restore(operational, universe.clone(), state.clone()).unwrap();
        assert_eq!(restored.state.last_input_sequence, 17);
        assert_eq!(restored.state.long_output_sequence, 3);
        assert_eq!(restored.state.carry_output_sequence, 5);
        assert_eq!(restored.state.last_long_feature_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_decision_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, Some(10 * DAY_MS));

        let mut mark_physics = config.clone();
        mark_physics.sources.mark_max_age_ms += 1;
        mark_physics.identity.long_decision_fingerprint = "1".repeat(64);
        mark_physics.identity.carry_decision_fingerprint = "2".repeat(64);
        let restored =
            SignalWorker::restore(mark_physics, universe.clone(), state.clone()).unwrap();
        assert_eq!(restored.state.last_input_sequence, 17);
        assert_eq!(restored.state.long_output_sequence, 3);
        assert_eq!(restored.state.carry_output_sequence, 5);
        assert_eq!(restored.state.last_long_feature_ts_ms, None);
        assert_eq!(restored.state.last_carry_decision_ts_ms, None);
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, None);

        let mut long_changed = config.clone();
        long_changed.long.regime_sma_days += 1;
        let restored =
            SignalWorker::restore(long_changed, universe.clone(), state.clone()).unwrap();
        assert_eq!(restored.state.last_long_feature_ts_ms, None);
        assert_eq!(restored.state.last_carry_decision_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, Some(10 * DAY_MS));

        let mut carry_changed = config;
        carry_changed.identity.carry_decision_fingerprint = "0".repeat(64);
        let restored = SignalWorker::restore(carry_changed, universe, state.clone()).unwrap();
        assert_eq!(restored.state.last_long_feature_ts_ms, Some(9 * DAY_MS));
        assert_eq!(restored.state.last_carry_decision_ts_ms, None);
        assert_eq!(restored.state.last_carry_upcoming_ts_ms, None);
        assert_eq!(restored.state.last_input_sequence, 17);

        let mut source_changed = test_config();
        source_changed.routing.source = "different_source".into();
        let error = match SignalWorker::restore(source_changed, test_universe(), state) {
            Ok(_) => panic!("a new source cannot inherit another source's sequence"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("source contract has drifted"));
    }

    #[test]
    fn pending_multi_output_transaction_recovers_every_crash_boundary() {
        let config = test_config();
        let universe = test_universe();
        let initial = SignalWorker::new(config.clone(), universe.clone()).unwrap();
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
            next_state_json: String::from_utf8(next_json.clone()).unwrap(),
            observation_json: observation_json.clone(),
        };

        for phase in 0..=4 {
            let root = temporary_root(&format!("crash-{phase}"));
            let state_dir = root.join("state");
            let spool_dir = root.join("spool");
            let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
            let pending = AtomicJsonStore::new(state_dir.join("pending-transaction.json"));
            let spool = SpoolWriter::new(&spool_dir).unwrap();
            checkpoint.save_bytes(&prior_json).unwrap();
            if phase > 0 {
                pending.save(&transaction).unwrap();
            }
            if phase > 1 {
                spool.write_encoded(observation_json[0].as_bytes()).unwrap();
            }
            if phase > 2 {
                spool.write_encoded(observation_json[1].as_bytes()).unwrap();
            }
            if phase > 3 {
                checkpoint.save_bytes(&next_json).unwrap();
            }

            let durable =
                DurableSignalWorker::open(config.clone(), universe.clone(), &state_dir, &spool_dir)
                    .unwrap();
            if phase == 0 {
                assert_eq!(durable.worker.state.last_input_sequence, 0);
                assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 0);
            } else {
                assert_eq!(durable.worker.state.last_input_sequence, 1);
                assert_eq!(durable.worker.state.long_output_sequence, 1);
                assert_eq!(durable.worker.state.carry_output_sequence, 1);
                assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 2);
                assert!(!pending.path().exists());
                assert_eq!(checkpoint.load_bytes().unwrap().unwrap(), next_json);
                drop(durable);
                let reopened = DurableSignalWorker::open(
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
    fn durable_checkpoint_owns_the_output_source_generation() {
        let config = test_config();
        let universe = test_universe();
        let root = temporary_root("source-generation");
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");

        let mut first =
            DurableSignalWorker::open(config.clone(), universe.clone(), &state_dir, &spool_dir)
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

        let mut restored =
            DurableSignalWorker::open(config.clone(), universe.clone(), &state_dir, &spool_dir)
                .unwrap();
        assert_eq!(restored.worker.state.source_generation, generation);
        let restored_rows = restored
            .apply_and_commit(WireEvent::Watermark {
                schema_version: SCHEMA_VERSION,
                sequence: 2,
                observed_ts_ms: 2 * DAY_MS + 1,
            })
            .unwrap();
        assert_eq!(restored_rows.len(), 1);
        assert_eq!(restored_rows[0].sequence, 2);
        assert_eq!(restored_rows[0].source, first_rows[0].source);
        drop(restored);

        std::fs::remove_dir_all(&state_dir).unwrap();
        let mut replacement =
            DurableSignalWorker::open(config, universe, &state_dir, &spool_dir).unwrap();
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
        let mut state = SignalWorker::new(config.clone(), universe.clone())
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

        let durable = DurableSignalWorker::open(config, universe, &state_dir, &spool_dir).unwrap();
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
}
