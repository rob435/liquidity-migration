//! CARRY's portfolio lifecycle, handoff, and target-planner reducer.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::Side;
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::scorer::{
    score_decision, CarryDecision, CarryFeatureRow, CarryRuleConfig, ScorerState, DAY_MS,
};
use crate::native_common::{
    checkpoint_payload, config_fingerprint, hex_digest, order_effects, valid_sha256, valid_symbol,
    CarryPresettlementFire, Effect, ExecutionOutput, PlannedTarget, PlannerFacts,
    DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};
use crate::position_plan::{plan as plan_targets, PlanRules, Step, Target};

pub const CONTRACT_SCHEMA_VERSION: u16 = 1;
// Admission cycles are one minute, matching the registered idle-cycle clock.
pub const ENTRY_CYCLE_MS: i64 = 60_000;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionRules {
    pub entry_floor_usdt: f64,
    pub resize_floor_usdt: f64,
    pub resize_floor_fraction: f64,
    pub engine_entry_cutoff_ms: i64,
    pub signal_validity_ms: i64,
    pub book_validity_ms: i64,
    pub presettlement_window_ms: i64,
}

impl ExecutionRules {
    fn validate(&self) -> Result<(), &'static str> {
        if [self.entry_floor_usdt, self.resize_floor_usdt]
            .iter()
            .any(|value| !value.is_finite() || *value < 0.0)
            || !self.resize_floor_fraction.is_finite()
            || !(0.0..1.0).contains(&self.resize_floor_fraction)
            || self.engine_entry_cutoff_ms <= 0
            || self.signal_validity_ms <= self.engine_entry_cutoff_ms
            || self.book_validity_ms <= self.signal_validity_ms
            || self.presettlement_window_ms <= 0
        {
            return Err("CARRY execution rules are invalid");
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyConfig {
    pub schema_version: u16,
    pub profile_name: String,
    pub environment: String,
    pub rule_sha256: String,
    pub feature_contract_sha256: String,
    pub operational_profile_sha256: String,
    pub entries_enabled: bool,
    /// Stable engine `sleeve`, not the Rust plug name.
    pub exodus_sleeve_name: String,
    pub rule: CarryRuleConfig,
    pub exit_bp: f64,
    pub early_exit_enabled: bool,
    pub presettlement_exit_enabled: bool,
    pub notional_multiplier: f64,
    pub entry_leverage: f64,
    pub stop_loss_fraction: f64,
    pub max_new_entries_per_cycle: usize,
    pub capital_reference_usdt: f64,
    pub rest_entries: bool,
    pub hold_decision_price: bool,
    pub give_up_instead_of_crossing: bool,
    pub execution: ExecutionRules,
}

impl StrategyConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != CONTRACT_SCHEMA_VERSION
            || self.profile_name.is_empty()
            || self.environment.is_empty()
            || self.exodus_sleeve_name.is_empty()
            || !valid_sha256(&self.rule_sha256)
            || !valid_sha256(&self.feature_contract_sha256)
            || !valid_sha256(&self.operational_profile_sha256)
        {
            return Err("CARRY config identity is invalid");
        }
        self.rule.validate()?;
        self.execution.validate()?;
        if [
            self.exit_bp,
            self.notional_multiplier,
            self.entry_leverage,
            self.stop_loss_fraction,
        ]
        .iter()
        .any(|value| !value.is_finite() || *value <= 0.0)
            || self.stop_loss_fraction >= 1.0
            || !self.capital_reference_usdt.is_finite()
            || self.capital_reference_usdt < 0.0
            || self.max_new_entries_per_cycle == 0
            || (!self.rest_entries
                && (self.hold_decision_price || self.give_up_instead_of_crossing))
        {
            return Err("CARRY config field is invalid");
        }
        Ok(())
    }

    pub fn fingerprint(&self) -> String {
        decision_fingerprint(self)
    }
}

#[derive(Serialize)]
struct DecisionIdentity<'a> {
    schema_version: u16,
    sleeve: &'static str,
    profile_name: &'a str,
    environment: &'a str,
    rule_sha256: &'a str,
    feature_contract_sha256: &'a str,
    rule: &'a CarryRuleConfig,
}

/// Stable identity for the registered daily scorer. Runtime entry switches,
/// capital limits, and execution policy may change without abandoning the
/// portfolio checkpoint they are meant to manage.
pub fn decision_fingerprint(config: &StrategyConfig) -> String {
    config_fingerprint(&DecisionIdentity {
        schema_version: CONTRACT_SCHEMA_VERSION,
        sleeve: "carry_native",
        profile_name: &config.profile_name,
        environment: &config.environment,
        rule_sha256: &config.rule_sha256,
        feature_contract_sha256: &config.feature_contract_sha256,
        rule: &config.rule,
    })
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SettledFundingObservation {
    pub symbol: String,
    pub settlement_ts_ms: i64,
    #[serde(default)]
    pub available_at_ms: Option<i64>,
    pub rate: f64,
    #[serde(default)]
    pub funding_interval_min: Option<i64>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PresettlementObservation {
    pub symbol: String,
    pub observed_ts_ms: i64,
    pub settlement_ts_ms: i64,
    pub running_rate: f64,
    pub mark_px: Option<f64>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CarrySignalBatch {
    pub schema_version: u16,
    pub decision_ts_ms: i64,
    pub rows: Vec<CarryFeatureRow>,
    #[serde(default)]
    pub upcoming_rows: Vec<CarryFeatureRow>,
    pub settled_funding: Vec<SettledFundingObservation>,
    pub presettlement: Vec<PresettlementObservation>,
    #[serde(default)]
    pub marks: Vec<MarketMark>,
    #[serde(default)]
    pub rejections: Vec<DataRejection>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MarketMark {
    pub symbol: String,
    pub observed_ts_ms: i64,
    pub mark_px: f64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DataRejection {
    pub symbol: String,
    pub reason: String,
    pub first_missing_ts_ms: Option<i64>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SleeveState {
    pub schema_version: u16,
    pub scorer: ScorerState,
    #[serde(with = "i64_key_map")]
    pub sizing_anchors: BTreeMap<i64, f64>,
    pub fired_exits: BTreeMap<String, i64>,
    pub desired_targets: BTreeMap<String, StoredTarget>,
    pub refused_entries: BTreeSet<String>,
    #[serde(default)]
    pub entry_retry_after_ms: BTreeMap<String, i64>,
    #[serde(default)]
    pub entry_cycle_started_ms: i64,
    #[serde(default)]
    pub entry_cycle_selected_symbols: BTreeSet<String>,
    #[serde(default)]
    pub entry_trail_by_symbol: BTreeMap<String, f64>,
    pub last_publication_decision_ts_ms: i64,
    #[serde(default)]
    pub current_decision: Option<CarryDecision>,
}

mod i64_key_map {
    use std::collections::BTreeMap;

    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S>(value: &BTreeMap<i64, f64>, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        value
            .iter()
            .map(|(key, value)| (key.to_string(), *value))
            .collect::<BTreeMap<_, _>>()
            .serialize(serializer)
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<BTreeMap<i64, f64>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let encoded = BTreeMap::<String, f64>::deserialize(deserializer)?;
        encoded
            .into_iter()
            .map(|(key, value)| {
                let parsed = key.parse::<i64>().map_err(serde::de::Error::custom)?;
                if parsed.to_string() != key {
                    return Err(serde::de::Error::custom(
                        "CARRY sizing-anchor timestamp key is not canonical",
                    ));
                }
                Ok((parsed, value))
            })
            .collect()
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StoredTarget {
    pub notional_usdt: f64,
    pub stop_loss_fraction: f64,
    pub leverage: f64,
    pub entry_valid_until_ms: i64,
}

impl SleeveState {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION
            || self.last_publication_decision_ts_ms < 0
        {
            return Err("unsupported or invalid CARRY checkpoint schema");
        }
        if self
            .sizing_anchors
            .iter()
            .any(|(ts, equity)| *ts <= 0 || !equity.is_finite() || *equity <= 0.0)
            || self
                .fired_exits
                .iter()
                .any(|(symbol, ts)| !valid_symbol(symbol) || *ts <= 0)
            || self.desired_targets.iter().any(|(symbol, target)| {
                !valid_symbol(symbol)
                    || !target.notional_usdt.is_finite()
                    || target.notional_usdt == 0.0
                    || !target.stop_loss_fraction.is_finite()
                    || !(0.0..1.0).contains(&target.stop_loss_fraction)
                    || target.stop_loss_fraction == 0.0
                    || !target.leverage.is_finite()
                    || target.leverage <= 0.0
                    || target.entry_valid_until_ms <= 0
            })
            || self
                .refused_entries
                .iter()
                .any(|symbol| !valid_symbol(symbol))
            || self
                .entry_retry_after_ms
                .iter()
                .any(|(symbol, retry_at)| !self.refused_entries.contains(symbol) || *retry_at <= 0)
            || self.entry_cycle_started_ms < 0
            || self
                .entry_cycle_selected_symbols
                .iter()
                .any(|symbol| !valid_symbol(symbol))
            || (self.entry_cycle_started_ms == 0 && !self.entry_cycle_selected_symbols.is_empty())
            || self
                .entry_trail_by_symbol
                .iter()
                .any(|(symbol, trail)| !valid_symbol(symbol) || !trail.is_finite())
        {
            return Err("CARRY checkpoint portfolio state is invalid");
        }
        if self.scorer.last_decision_ts_ms < 0
            || self.scorer.first_replay_ts_ms < 0
            || self.scorer.last_weights.iter().any(|(symbol, weight)| {
                !valid_symbol(symbol) || !weight.is_finite() || *weight <= 0.0
            })
            || self
                .scorer
                .by_symbol
                .iter()
                .any(|(symbol, row)| !valid_symbol(symbol) || row.last_ts_ms < 0)
        {
            return Err("CARRY scorer checkpoint is invalid");
        }
        if let Some(decision) = &self.current_decision {
            if decision.schema_version != CONTRACT_SCHEMA_VERSION
                || decision.decision_ts_ms <= 0
                || decision.universe_size == 0
                || decision.replay_days < 0
                || !decision.gross.is_finite()
                || decision.gross < 0.0
                || decision.weights.iter().any(|(symbol, weight)| {
                    !valid_symbol(symbol) || !weight.is_finite() || *weight <= 0.0
                })
            {
                return Err("CARRY current decision checkpoint is invalid");
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PlanSummary {
    pub desired_book_size: usize,
    pub desired_gross_weight: f64,
    pub planned_exits: usize,
    pub planned_entries: usize,
    pub planned_resizes: usize,
    pub resize_mark_missing_skips: usize,
    pub entry_cap_deferrals: usize,
    pub entry_validity_expired_skips: usize,
    pub entry_dust_skips: usize,
    pub engine_blocked_entries: usize,
    pub entry_blocked_reason: String,
}

#[derive(Clone, Debug)]
pub struct ReducerInput {
    pub now_ms: i64,
    pub decision: CarryDecision,
    pub upcoming_decision: Option<CarryDecision>,
    pub settled_funding: Vec<SettledFundingObservation>,
    pub presettlement: Vec<PresettlementObservation>,
    pub durable_fires: Vec<CarryPresettlementFire>,
    pub trail_by_symbol: BTreeMap<String, f64>,
    pub entry_blockers: BTreeMap<String, String>,
    pub account_healthy: bool,
    pub equity_usdt: f64,
    pub upcoming_sizing_equity_usdt: Option<f64>,
    pub facts: PlannerFacts,
    pub owned_working_symbols: BTreeSet<String>,
    pub owned_opening_order_ids: BTreeMap<String, Vec<String>>,
    pub checkpoint_fingerprint: Option<String>,
    pub signal_receipt: Option<(String, u64, String)>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ReducerOutput {
    pub next_state: SleeveState,
    pub effective_decision: CarryDecision,
    pub summary: PlanSummary,
    pub settled_exit_fires: Vec<String>,
    pub presettlement_fires: Vec<CarryPresettlementFire>,
    pub drop_exit_fires: Vec<String>,
    pub execution: ExecutionOutput,
}

#[derive(Clone, Debug)]
pub struct ScorerCatchupInput {
    pub decision_ts_ms: i64,
    pub rows: Vec<CarryFeatureRow>,
    pub signal_receipt: (String, u64, String),
}

#[derive(Clone, Debug, Serialize)]
pub struct ScorerCatchupOutput {
    pub next_state: SleeveState,
    pub execution: ExecutionOutput,
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum ReplanMode {
    Ordinary,
    BootRecovery,
}

pub fn reduce_scorer_catchup(
    input: ScorerCatchupInput,
    mut state: SleeveState,
    config: &StrategyConfig,
) -> Result<ScorerCatchupOutput, &'static str> {
    config.validate()?;
    if state.schema_version == 0 {
        state.schema_version = DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION;
    }
    state.validate()?;
    let previous_ts_ms = state.scorer.last_decision_ts_ms;
    if previous_ts_ms > 0
        && input.decision_ts_ms != previous_ts_ms
        && input.decision_ts_ms != previous_ts_ms.saturating_add(DAY_MS)
    {
        return Err("CARRY scorer catch-up is not the next UTC generation");
    }
    let (_, scorer) = score_decision(
        &input.rows,
        input.decision_ts_ms,
        &state.scorer,
        &config.rule,
    )?;
    state.scorer = scorer;
    state.validate()?;
    let (source, sequence, observation_id) = input.signal_receipt;
    Ok(ScorerCatchupOutput {
        execution: ExecutionOutput {
            effects: vec![
                Effect::PersistCheckpoint {
                    symbol: String::new(),
                    config_fingerprint: config.fingerprint(),
                    payload: checkpoint_payload(&state),
                },
                Effect::ConsumeSignal {
                    source,
                    sequence,
                    observation_id,
                },
            ],
            skipped: Vec::new(),
        },
        next_state: state,
    })
}

pub fn reduce_signal(
    batch: CarrySignalBatch,
    mut input: ReducerInput,
    mut prior: SleeveState,
    config: &StrategyConfig,
) -> Result<ReducerOutput, &'static str> {
    if batch.schema_version != CONTRACT_SCHEMA_VERSION
        || batch.decision_ts_ms != input.decision.decision_ts_ms
    {
        return Err("CARRY signal batch identity is invalid");
    }
    let (decision, scorer) = score_decision(
        &batch.rows,
        batch.decision_ts_ms,
        &prior.scorer,
        &config.rule,
    )?;
    let upcoming = if batch.upcoming_rows.is_empty() {
        None
    } else {
        Some(
            score_decision(
                &batch.upcoming_rows,
                batch.decision_ts_ms + DAY_MS,
                &scorer,
                &config.rule,
            )?
            .0,
        )
    };
    prior.scorer = scorer;
    input.decision = decision;
    input.upcoming_decision = upcoming;
    input.settled_funding = batch.settled_funding;
    input.presettlement = batch.presettlement;
    reduce_lifecycle(input, prior, config)
}

pub fn reduce_lifecycle(
    input: ReducerInput,
    state: SleeveState,
    config: &StrategyConfig,
) -> Result<ReducerOutput, &'static str> {
    reduce_lifecycle_with_mode(input, state, config, ReplanMode::Ordinary)
}

pub fn reduce_lifecycle_with_mode(
    input: ReducerInput,
    mut state: SleeveState,
    config: &StrategyConfig,
    replan_mode: ReplanMode,
) -> Result<ReducerOutput, &'static str> {
    config.validate()?;
    if input.now_ms <= 0
        || !input.equity_usdt.is_finite()
        || input.equity_usdt < 0.0
        || input.decision.schema_version != CONTRACT_SCHEMA_VERSION
        || input.decision.decision_ts_ms <= 0
    {
        return Err("CARRY reducer input is invalid");
    }
    let fingerprint = config.fingerprint();
    let previous_targets = state.desired_targets.clone();
    let mismatch = input
        .checkpoint_fingerprint
        .as_ref()
        .is_some_and(|seen| seen != &fingerprint);
    if state.schema_version == 0 {
        state.schema_version = DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION;
    }
    state.validate()?;
    let decision_ts = input.decision.decision_ts_ms;
    let decision_available = decision_ts <= input.now_ms;
    if decision_ts < state.last_publication_decision_ts_ms {
        return Err("CARRY decision regressed behind durable state");
    }
    if mismatch {
        state.desired_targets.clear();
    }

    let new_generation = decision_ts > state.last_publication_decision_ts_ms;
    if new_generation || !input.trail_by_symbol.is_empty() {
        state.entry_trail_by_symbol = input.trail_by_symbol.clone();
    }
    state
        .entry_trail_by_symbol
        .retain(|symbol, _| input.decision.weights.contains_key(symbol));
    let cycle_reset = state.entry_cycle_started_ms == 0
        || input.now_ms >= state.entry_cycle_started_ms.saturating_add(ENTRY_CYCLE_MS);
    if cycle_reset {
        state.entry_cycle_started_ms = input.now_ms;
        state.entry_cycle_selected_symbols.clear();
        state.refused_entries.clear();
        state.entry_retry_after_ms.clear();
    }
    if replan_mode == ReplanMode::BootRecovery && !decision_available {
        state.entry_cycle_selected_symbols.clear();
    }
    if state.entry_cycle_selected_symbols.len() > config.max_new_entries_per_cycle {
        state.entry_cycle_selected_symbols = state
            .entry_cycle_selected_symbols
            .into_iter()
            .take(config.max_new_entries_per_cycle)
            .collect();
    }
    state
        .fired_exits
        .retain(|_, fired_at| *fired_at == decision_ts);
    let mut effective = input.decision.clone();
    let threshold = -(config.exit_bp / 10_000.0);
    let mut settled_fires = Vec::new();
    if config.early_exit_enabled {
        let mut latest = BTreeMap::<String, &SettledFundingObservation>::new();
        for observation in &input.settled_funding {
            if !valid_symbol(&observation.symbol)
                || !observation.rate.is_finite()
                || observation.settlement_ts_ms <= 0
            {
                return Err("CARRY settled-funding observation is invalid");
            }
            if effective.weights.contains_key(&observation.symbol)
                && decision_ts < observation.settlement_ts_ms
                && observation.settlement_ts_ms <= input.now_ms
                && latest
                    .get(&observation.symbol)
                    .is_none_or(|old| observation.settlement_ts_ms > old.settlement_ts_ms)
            {
                latest.insert(observation.symbol.clone(), observation);
            }
        }
        for (symbol, observation) in latest {
            if !state.fired_exits.contains_key(&symbol) && observation.rate >= threshold {
                state.fired_exits.insert(symbol.clone(), decision_ts);
                settled_fires.push(symbol);
            }
        }
    }

    let mut presettlement_fires = Vec::new();
    if config.early_exit_enabled && config.presettlement_exit_enabled {
        let mut durable_fire_ids = BTreeSet::new();
        for fire in &input.durable_fires {
            validate_carry_fire(fire)?;
            durable_fire_ids.insert(fire.event_id.clone());
            if fire.environment == config.environment
                && fire.source_profile == config.profile_name
                && fire.source_config_id == config.rule.config_id
                && fire.decision_ts_ms == decision_ts
                && effective.weights.contains_key(&fire.symbol)
            {
                state.fired_exits.insert(fire.symbol.clone(), decision_ts);
            }
        }
        let mut seen = BTreeSet::new();
        for observation in &input.presettlement {
            if !seen.insert(observation.symbol.clone()) {
                return Err("CARRY pre-settlement observations contain duplicate symbols");
            }
            if !valid_symbol(&observation.symbol)
                || !observation.running_rate.is_finite()
                || observation.observed_ts_ms <= 0
                || observation.settlement_ts_ms <= observation.observed_ts_ms
                || observation
                    .mark_px
                    .is_some_and(|value| !value.is_finite() || value <= 0.0)
            {
                return Err("CARRY pre-settlement observation is invalid");
            }
            if !effective.weights.contains_key(&observation.symbol)
                || state.fired_exits.contains_key(&observation.symbol)
                || observation.observed_ts_ms < decision_ts
                || observation.observed_ts_ms > input.now_ms
                || observation.settlement_ts_ms - observation.observed_ts_ms
                    > config.execution.presettlement_window_ms
                || observation.running_rate < threshold
            {
                continue;
            }
            let event_id = carry_event_id(
                &config.environment,
                &config.rule.config_id,
                decision_ts,
                observation.settlement_ts_ms,
                &observation.symbol,
            );
            state
                .fired_exits
                .insert(observation.symbol.clone(), decision_ts);
            if !durable_fire_ids.contains(&event_id) {
                let holding = input.facts.held.get(&observation.symbol);
                let (carry_side, carry_qty) = holding.map_or((None, None), |held| {
                    (
                        Some(match held.side {
                            Side::Buy => "long".to_owned(),
                            Side::Sell => "short".to_owned(),
                        }),
                        Some(held.qty),
                    )
                });
                presettlement_fires.push(CarryPresettlementFire {
                    event_id,
                    environment: config.environment.clone(),
                    source_profile: config.profile_name.clone(),
                    source_config_id: config.rule.config_id.clone(),
                    decision_ts_ms: decision_ts,
                    fired_ts_ms: observation.observed_ts_ms,
                    settlement_ts_ms: observation.settlement_ts_ms,
                    symbol: observation.symbol.clone(),
                    mark_px: observation
                        .mark_px
                        .or_else(|| input.facts.prices.get(&observation.symbol).copied()),
                    carry_side,
                    carry_qty,
                });
            }
        }
    }

    for symbol in state.fired_exits.keys() {
        effective.weights.remove(symbol);
    }
    let mut drop_fires = Vec::new();
    if let Some(upcoming) = &input.upcoming_decision {
        if upcoming.decision_ts_ms == decision_ts + DAY_MS {
            let dropped = effective
                .weights
                .keys()
                .filter(|symbol| !upcoming.weights.contains_key(*symbol))
                .cloned()
                .collect::<Vec<_>>();
            for symbol in &dropped {
                effective.weights.remove(symbol);
            }
            drop_fires = dropped;
        }
    }
    effective.gross = effective.weights.values().sum();

    if input.account_healthy && input.equity_usdt > 0.0 {
        state
            .sizing_anchors
            .entry(decision_ts)
            .or_insert(input.equity_usdt);
        while state.sizing_anchors.len() > 2 {
            let oldest = *state
                .sizing_anchors
                .keys()
                .next()
                .expect("non-empty sizing anchors");
            state.sizing_anchors.remove(&oldest);
        }
        if let (Some(upcoming), Some(equity)) = (
            input.upcoming_decision.as_ref(),
            input.upcoming_sizing_equity_usdt,
        ) {
            if equity.is_finite() && equity > 0.0 {
                state
                    .sizing_anchors
                    .entry(upcoming.decision_ts_ms)
                    .or_insert(equity);
                while state.sizing_anchors.len() > 2 {
                    let oldest = *state
                        .sizing_anchors
                        .keys()
                        .next()
                        .expect("non-empty sizing anchors");
                    state.sizing_anchors.remove(&oldest);
                }
            }
        }
    }
    let sizing_equity = state
        .sizing_anchors
        .get(&decision_ts)
        .copied()
        .map(|value| {
            if config.capital_reference_usdt > 0.0 {
                value.min(config.capital_reference_usdt)
            } else {
                value
            }
        });

    let standing = input
        .facts
        .held_symbols()
        .into_iter()
        .collect::<BTreeSet<_>>();
    let mut desired_weights = BTreeMap::new();
    let mut entry_effect_symbols = BTreeSet::new();
    let mut summary = PlanSummary {
        desired_book_size: effective.weights.len(),
        desired_gross_weight: effective.gross,
        ..PlanSummary::default()
    };
    let plan_rules = PlanRules {
        entry_floor_usdt: config.execution.entry_floor_usdt,
        resize_floor_usdt: config.execution.resize_floor_usdt,
        resize_floor_fraction: config.execution.resize_floor_fraction,
        entry_cutoff_ms: config.execution.engine_entry_cutoff_ms,
    };
    let entry_valid_until_ms =
        decision_ts + config.execution.signal_validity_ms - config.execution.engine_entry_cutoff_ms;
    if !input.account_healthy || sizing_equity.is_none() {
        summary.entry_blocked_reason = "engine_account_health_unavailable".into();
        for symbol in state
            .desired_targets
            .keys()
            .filter(|symbol| effective.weights.contains_key(*symbol))
        {
            if let Some(weight) = effective.weights.get(symbol) {
                desired_weights.insert(symbol.clone(), *weight);
            }
        }
    } else {
        let sizing = sizing_equity.ok_or("healthy CARRY input has no sizing anchor")?;
        for symbol in standing
            .iter()
            .filter(|symbol| effective.weights.contains_key(*symbol))
        {
            desired_weights.insert(symbol.clone(), effective.weights[symbol]);
        }
        let mut entries = effective
            .weights
            .keys()
            .filter(|symbol| !standing.contains(*symbol))
            .cloned()
            .collect::<Vec<_>>();
        entries.sort_by(|left, right| {
            state
                .entry_trail_by_symbol
                .get(left)
                .copied()
                .unwrap_or(0.0)
                .total_cmp(
                    &state
                        .entry_trail_by_symbol
                        .get(right)
                        .copied()
                        .unwrap_or(0.0),
                )
                .then_with(|| left.cmp(right))
        });
        summary.engine_blocked_entries = entries
            .iter()
            .filter(|symbol| input.entry_blockers.contains_key(*symbol))
            .count();
        if input.now_ms
            >= decision_ts + config.execution.signal_validity_ms
                - config.execution.engine_entry_cutoff_ms
        {
            summary.entry_validity_expired_skips = entries.len();
            entries.clear();
        }
        for symbol in entries {
            let target = effective.weights[&symbol] * sizing * config.notional_multiplier;
            let engine_blocked = input.entry_blockers.contains_key(&symbol) && !cycle_reset;
            if engine_blocked {
                summary.entry_blocked_reason = input.entry_blockers[&symbol].clone();
                if previous_targets.contains_key(&symbol) {
                    desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                }
            } else if target.abs() < config.execution.entry_floor_usdt {
                summary.entry_dust_skips += 1;
            } else if !config.entries_enabled || mismatch || state.refused_entries.contains(&symbol)
            {
                summary.entry_blocked_reason = if !config.entries_enabled {
                    "entries_disabled".into()
                } else if mismatch {
                    "checkpoint_mismatch".into()
                } else {
                    "entry_refused".into()
                };
                if !mismatch && previous_targets.contains_key(&symbol) {
                    desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                }
            } else {
                let already_working = input.owned_working_symbols.contains(&symbol);
                let already_selected = state.entry_cycle_selected_symbols.contains(&symbol);
                if already_working {
                    desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                    continue;
                }

                let candidate = Target {
                    symbol: symbol.clone(),
                    notional_usdt: target,
                    stop_loss_fraction: config.stop_loss_fraction,
                    entry_valid_until_ms: Some(entry_valid_until_ms),
                    target_qty: None,
                };
                let can_enter = decision_available
                    && plan_targets(
                        &[candidate],
                        &[],
                        &input.facts,
                        input.now_ms,
                        decision_ts + config.execution.book_validity_ms,
                        plan_rules,
                    )
                    .steps
                    .iter()
                    .any(|step| matches!(step, Step::Enter { .. }));

                if already_selected {
                    desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                    if replan_mode == ReplanMode::BootRecovery && can_enter {
                        entry_effect_symbols.insert(symbol.clone());
                        summary.planned_entries += 1;
                    }
                } else if !can_enter {
                    // Keep the target retryable, but do not spend a durable
                    // admission slot until the planner can emit an entry.
                    desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                } else if state.entry_cycle_selected_symbols.len()
                    >= config.max_new_entries_per_cycle
                {
                    summary.entry_cap_deferrals += 1;
                    if previous_targets.contains_key(&symbol) {
                        desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                    }
                } else {
                    desired_weights.insert(symbol.clone(), effective.weights[&symbol]);
                    state.entry_cycle_selected_symbols.insert(symbol.clone());
                    entry_effect_symbols.insert(symbol);
                    summary.planned_entries += 1;
                }
            }
        }
    }

    let mut targets = Vec::new();
    if !input.account_healthy || sizing_equity.is_none() {
        // Preserve exact previous asks. Unknown equity is not one dollar and
        // must never turn a valid standing target into an accidental trim.
        for (symbol, stored) in &state.desired_targets {
            if effective.weights.contains_key(symbol) {
                targets.push(PlannedTarget {
                    target: Target {
                        symbol: symbol.clone(),
                        notional_usdt: stored.notional_usdt,
                        stop_loss_fraction: stored.stop_loss_fraction,
                        entry_valid_until_ms: Some(stored.entry_valid_until_ms),
                        target_qty: None,
                    },
                    leverage: stored.leverage,
                });
            }
        }
    } else {
        let sizing = sizing_equity.ok_or("healthy CARRY input has no sizing anchor")?;
        for (symbol, weight) in &desired_weights {
            let mut notional = weight * sizing * config.notional_multiplier;
            if (!config.entries_enabled || mismatch) && standing.contains(symbol) {
                if let Some(held) = input.facts.held.get(symbol) {
                    let standing_notional = held.notional();
                    if standing_notional.signum() == notional.signum()
                        && notional.abs() > standing_notional.abs()
                    {
                        notional = standing_notional;
                    }
                }
            }
            targets.push(PlannedTarget {
                target: Target {
                    symbol: symbol.clone(),
                    notional_usdt: notional,
                    stop_loss_fraction: config.stop_loss_fraction,
                    entry_valid_until_ms: Some(entry_valid_until_ms),
                    target_qty: None,
                },
                leverage: config.entry_leverage,
            });
        }
    }
    // A healthy absolute decision exits every attributed standing symbol it no
    // longer wants. Under unhealthy account input only removal transitions
    // are allowed, which is still represented by omission from `targets`.
    summary.planned_exits = standing
        .iter()
        .filter(|symbol| !desired_weights.contains_key(*symbol))
        .count();

    let raw_targets = targets
        .iter()
        .map(|target| target.target.clone())
        .collect::<Vec<_>>();
    let planned = plan_targets(
        &raw_targets,
        &input.facts.held_symbols(),
        &input.facts,
        input.now_ms,
        decision_ts + config.execution.book_validity_ms,
        plan_rules,
    );
    summary.planned_resizes = planned
        .steps
        .iter()
        .filter(|step| matches!(step, Step::Resize { .. }))
        .count();

    state.desired_targets = targets
        .iter()
        .map(|target| {
            (
                target.target.symbol.clone(),
                StoredTarget {
                    notional_usdt: target.target.notional_usdt,
                    stop_loss_fraction: target.target.stop_loss_fraction,
                    leverage: target.leverage,
                    entry_valid_until_ms: target.target.entry_valid_until_ms.unwrap_or(decision_ts),
                },
            )
        })
        .collect();
    state.last_publication_decision_ts_ms = decision_ts;
    state.current_decision = Some(effective.clone());
    state.validate()?;
    // A durable fire must precede the checkpoint that remembers it. A crash
    // may leave an event without the checkpoint, which is safe to replay; it
    // must never leave fired state without the event Exodus needs.
    let mut effects = presettlement_fires
        .iter()
        .cloned()
        .map(Effect::AppendCarryFire)
        .collect::<Vec<_>>();
    effects.push(Effect::PersistCheckpoint {
        symbol: String::new(),
        config_fingerprint: fingerprint,
        payload: checkpoint_payload(&state),
    });
    if let Some((source, sequence, observation_id)) = input.signal_receipt {
        effects.push(Effect::ConsumeSignal {
            source,
            sequence,
            observation_id,
        });
    }
    for (symbol, client_order_ids) in &input.owned_opening_order_ids {
        let changed = previous_targets.get(symbol) != state.desired_targets.get(symbol);
        if changed || !state.desired_targets.contains_key(symbol) {
            for client_order_id in client_order_ids {
                effects.push(Effect::CancelOwned {
                    symbol: symbol.clone(),
                    client_order_id: client_order_id.clone(),
                });
            }
        }
    }
    let steps = planned
        .steps
        .into_iter()
        .filter(|step| {
            let risk_increase_allows = (input.account_healthy && decision_available)
                || matches!(
                    step,
                    Step::Exit { .. }
                        | Step::Resize {
                            reduce_only: true,
                            ..
                        }
                        | Step::Restop { .. }
                );
            let selected_entry =
                !matches!(step, Step::Enter { .. }) || entry_effect_symbols.contains(step.symbol());
            let opening_not_refused = !matches!(
                step,
                Step::Enter { .. }
                    | Step::Resize {
                        reduce_only: false,
                        ..
                    }
            ) || !state.refused_entries.contains(step.symbol());
            risk_increase_allows
                && selected_entry
                && opening_not_refused
                && (!input.owned_working_symbols.contains(step.symbol())
                    || matches!(step, Step::Restop { .. }))
        })
        .collect();
    effects.extend(order_effects(steps, &targets, "carry-native"));
    Ok(ReducerOutput {
        next_state: state,
        effective_decision: effective,
        summary,
        settled_exit_fires: settled_fires,
        presettlement_fires,
        drop_exit_fires: drop_fires,
        execution: ExecutionOutput {
            effects,
            skipped: planned.skipped,
        },
    })
}

fn validate_carry_fire(fire: &CarryPresettlementFire) -> Result<(), &'static str> {
    if !valid_symbol(&fire.symbol)
        || fire.environment.is_empty()
        || fire.source_profile.is_empty()
        || fire.source_config_id.is_empty()
        || fire.decision_ts_ms <= 0
        || fire.fired_ts_ms < fire.decision_ts_ms
        || fire.settlement_ts_ms <= fire.fired_ts_ms
        || fire
            .mark_px
            .is_some_and(|value| !value.is_finite() || value <= 0.0)
        || fire
            .carry_qty
            .is_some_and(|value| !value.is_finite() || value <= 0.0)
        || fire
            .carry_side
            .as_deref()
            .is_some_and(|side| side != "long" && side != "short")
        || (fire.carry_side.is_none() != fire.carry_qty.is_none())
        || fire.event_id
            != carry_event_id(
                &fire.environment,
                &fire.source_config_id,
                fire.decision_ts_ms,
                fire.settlement_ts_ms,
                &fire.symbol,
            )
    {
        return Err("CARRY durable fire is invalid");
    }
    Ok(())
}

pub fn carry_event_id(
    environment: &str,
    source_config_id: &str,
    decision_ts_ms: i64,
    settlement_ts_ms: i64,
    symbol: &str,
) -> String {
    let identity = json!({
        "decision_ts_ms": decision_ts_ms,
        "environment": environment,
        "schema_version": 1,
        "settlement_ts_ms": settlement_ts_ms,
        "source_config_id": source_config_id,
        "symbol": symbol,
    });
    format!(
        "carry-presettlement-{}",
        hex_digest(&serde_json::to_vec(&identity).expect("event identity JSON"))
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::position_plan::{Held, Skipped};

    const FIXTURE: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/carry_native_replay_v1.json"
    ));

    fn config() -> StrategyConfig {
        StrategyConfig {
            schema_version: 1,
            profile_name: "carry_hold_v7_live_v1".into(),
            environment: "demo".into(),
            rule_sha256: "1".repeat(64),
            feature_contract_sha256: "3".repeat(64),
            operational_profile_sha256: "2".repeat(64),
            entries_enabled: true,
            exodus_sleeve_name: "exodus".into(),
            rule: CarryRuleConfig {
                config_id: "lane2_carry_hold_v7".into(),
                universe_top_n: 100,
                enter_bp: 10.0,
                exit_bp: 3.0,
                per_name_cap: 0.1,
                gross_cap: 1.0,
                depth_ref_bp_per_day: 120.0,
                depth_floor: 0.25,
                depth_exponent: 1.5,
                toxic_band_ret3d_lo: -0.3,
                toxic_band_ret3d_hi: 0.0,
                min_vol30_daily: 0.05,
                trail_recovery_exit_bp_2d: 30.0,
                persistence_cut: 0.1,
                persistence_lo: 0.0,
                flow_cut: 0.4,
                flow_lo: 0.5,
                whale_cut: -0.26,
                whale_lo: 0.5,
            },
            exit_bp: 3.0,
            early_exit_enabled: true,
            presettlement_exit_enabled: true,
            notional_multiplier: 1.0,
            entry_leverage: 2.0,
            stop_loss_fraction: 0.35,
            max_new_entries_per_cycle: 2,
            capital_reference_usdt: 0.0,
            rest_entries: true,
            hold_decision_price: false,
            give_up_instead_of_crossing: false,
            execution: ExecutionRules {
                entry_floor_usdt: 6.0,
                resize_floor_usdt: 1.0,
                resize_floor_fraction: 0.05,
                engine_entry_cutoff_ms: 900_000,
                signal_validity_ms: 21_600_000,
                book_validity_ms: 108_000_000,
                presettlement_window_ms: 900_000,
            },
        }
    }

    #[test]
    fn event_identity_matches_the_checked_in_exodus_fixture() {
        assert_eq!(
            carry_event_id(
                "demo",
                "lane2_carry_hold_v7",
                1_799_971_200_000,
                1_800_000_600_000,
                "AUSDT",
            ),
            "carry-presettlement-6a3b536fb0ff144fd2d452e5875c6383e771ba2c9d2894fc4633549c765a1309"
        );
    }

    #[test]
    fn checkpoint_round_trips_integer_sizing_anchor_keys() {
        let state = SleeveState {
            schema_version: 1,
            sizing_anchors: BTreeMap::from([
                (1_799_913_600_000, 900.0),
                (1_800_000_000_000, 1_000.0),
            ]),
            ..SleeveState::default()
        };
        let bytes = checkpoint_payload(&state);
        let restored: SleeveState = serde_json::from_slice(&bytes).expect("checkpoint restore");
        assert_eq!(restored, state);
        assert!(serde_json::from_str::<SleeveState>(
            r#"{"schema_version":1,"scorer":{"by_symbol":{},"last_decision_ts_ms":0,"first_replay_ts_ms":0,"last_weights":{},"last_universe_size":0},"sizing_anchors":{"01":1.0},"fired_exits":{},"desired_targets":{},"refused_entries":[],"entry_retry_after_ms":{},"last_publication_decision_ts_ms":0,"current_decision":null}"#,
        )
        .is_err());
    }

    fn entry_symbols(effects: &[Effect]) -> BTreeSet<String> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                Effect::Order(crate::native_common::OrderEffect {
                    step: Step::Enter { symbol, .. },
                    ..
                }) => Some(symbol.clone()),
                _ => None,
            })
            .collect()
    }

    fn entry_facts(symbols: &[&str]) -> PlannerFacts {
        let rule = engine_types::InstrumentRule {
            tick_size: 0.01,
            qty_step: 0.1,
            min_qty: 0.1,
            min_notional: 5.0,
        };
        PlannerFacts {
            prices: symbols
                .iter()
                .map(|symbol| ((*symbol).to_owned(), 100.0))
                .collect(),
            rules: symbols
                .iter()
                .map(|symbol| ((*symbol).to_owned(), rule))
                .collect(),
            ..PlannerFacts::default()
        }
    }

    fn scorer_rows(first_day: i64, last_day: i64) -> Vec<CarryFeatureRow> {
        let mut rows = Vec::new();
        for day in first_day..=last_day {
            for index in 0..50 {
                rows.push(CarryFeatureRow {
                    symbol: format!("S{index:03}USDT"),
                    bar_ts_ms: day * DAY_MS,
                    by_funding: Some(-0.002),
                    adv24: Some(1_000.0 - index as f64),
                    trail_fund_24h: Some(-0.012),
                    ret_3d: Some(0.1),
                    vol_30d_daily: Some(0.1),
                    in_universe: true,
                    ..CarryFeatureRow::default()
                });
            }
        }
        rows
    }

    #[test]
    fn takeover_entry_budget_is_durable_and_replans_only_at_allowed_boundaries() {
        let decision_ts_ms = 100 * DAY_MS;
        let now_ms = decision_ts_ms + 1_000;
        let symbols = ["AUSDT", "BUSDT", "CUSDT"];
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: symbols
                .iter()
                .map(|symbol| ((*symbol).to_owned(), 0.1))
                .collect(),
            universe_size: 100,
            replay_days: 45,
            gross: 0.3,
        };
        let valid_until = decision_ts_ms + config().execution.signal_validity_ms
            - config().execution.engine_entry_cutoff_ms;
        let mut state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(decision_ts_ms, 1_000.0)]),
            desired_targets: symbols
                .iter()
                .map(|symbol| {
                    (
                        (*symbol).to_owned(),
                        StoredTarget {
                            notional_usdt: 100.0,
                            stop_loss_fraction: config().stop_loss_fraction,
                            leverage: config().entry_leverage,
                            entry_valid_until_ms: valid_until,
                        },
                    )
                })
                .collect(),
            last_publication_decision_ts_ms: decision_ts_ms,
            current_decision: Some(decision.clone()),
            ..SleeveState::default()
        };
        let input = ReducerInput {
            now_ms,
            decision,
            upcoming_decision: None,
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            durable_fires: Vec::new(),
            trail_by_symbol: BTreeMap::from([
                ("AUSDT".to_owned(), -3.0),
                ("BUSDT".to_owned(), -2.0),
                ("CUSDT".to_owned(), -1.0),
            ]),
            entry_blockers: BTreeMap::new(),
            account_healthy: true,
            equity_usdt: 1_000.0,
            upcoming_sizing_equity_usdt: None,
            facts: entry_facts(&symbols),
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };

        let first = reduce_lifecycle(input.clone(), state.clone(), &config()).expect("takeover");
        assert_eq!(first.next_state.desired_targets.len(), 3);
        assert_eq!(first.next_state.entry_cycle_selected_symbols.len(), 2);
        assert_eq!(entry_symbols(&first.execution.effects).len(), 2);
        assert_eq!(first.summary.entry_cap_deferrals, 1);

        let same_cycle = reduce_lifecycle(input.clone(), first.next_state.clone(), &config())
            .expect("ordinary same-cycle wake");
        assert!(entry_symbols(&same_cycle.execution.effects).is_empty());
        assert_eq!(same_cycle.next_state.desired_targets.len(), 3);

        let restored: SleeveState =
            serde_json::from_slice(&checkpoint_payload(&first.next_state)).expect("restore");
        let boot = reduce_lifecycle_with_mode(
            input.clone(),
            restored,
            &config(),
            ReplanMode::BootRecovery,
        )
        .expect("boot recovery");
        assert_eq!(
            entry_symbols(&boot.execution.effects),
            first.next_state.entry_cycle_selected_symbols
        );

        let first_selected = first.next_state.entry_cycle_selected_symbols.clone();
        let mut next_cycle_input = input.clone();
        next_cycle_input.now_ms += ENTRY_CYCLE_MS;
        next_cycle_input.owned_working_symbols = first_selected;
        let deferred = reduce_lifecycle(next_cycle_input, first.next_state.clone(), &config())
            .expect("deferred next-cycle entry");
        assert_eq!(
            entry_symbols(&deferred.execution.effects),
            BTreeSet::from(["CUSDT".into()])
        );

        let refused_symbol = first
            .next_state
            .entry_cycle_selected_symbols
            .iter()
            .next()
            .expect("selected")
            .clone();
        state = first.next_state;
        state.refused_entries.insert(refused_symbol.clone());
        let mut refused_input = input.clone();
        refused_input
            .entry_blockers
            .insert(refused_symbol.clone(), "entry_refused".into());
        let refused_wake = reduce_lifecycle(refused_input.clone(), state, &config())
            .expect("same-cycle refusal wake");
        assert!(entry_symbols(&refused_wake.execution.effects).is_empty());
        assert_eq!(refused_wake.next_state.desired_targets.len(), 3);

        refused_input.now_ms += ENTRY_CYCLE_MS;
        let retried = reduce_lifecycle(refused_input, refused_wake.next_state, &config())
            .expect("next-cycle refusal retry");
        assert!(entry_symbols(&retried.execution.effects).contains(&refused_symbol));
        assert!(retried.next_state.refused_entries.is_empty());
    }

    #[test]
    fn missing_planner_facts_do_not_consume_the_entry_budget() {
        let decision_ts_ms = 100 * DAY_MS;
        let now_ms = decision_ts_ms + 1_000;
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: BTreeMap::from([
                ("MISSINGPRICEUSDT".into(), 0.1),
                ("MISSINGRULEUSDT".into(), 0.1),
                ("VIABLEUSDT".into(), 0.1),
                ("WAITUSDT".into(), 0.1),
            ]),
            universe_size: 100,
            replay_days: 45,
            gross: 0.4,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(decision_ts_ms, 1_000.0)]),
            ..SleeveState::default()
        };
        let mut cfg = config();
        cfg.max_new_entries_per_cycle = 1;
        let input = ReducerInput {
            now_ms,
            decision,
            upcoming_decision: None,
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            durable_fires: Vec::new(),
            trail_by_symbol: BTreeMap::from([
                ("MISSINGPRICEUSDT".into(), -4.0),
                ("MISSINGRULEUSDT".into(), -3.0),
                ("VIABLEUSDT".into(), -2.0),
                ("WAITUSDT".into(), -1.0),
            ]),
            entry_blockers: BTreeMap::new(),
            account_healthy: true,
            equity_usdt: 1_000.0,
            upcoming_sizing_equity_usdt: None,
            facts: {
                let mut facts = entry_facts(&["VIABLEUSDT", "WAITUSDT"]);
                facts.prices.insert("MISSINGRULEUSDT".into(), 100.0);
                facts
            },
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };

        let first = reduce_lifecycle(input.clone(), state, &cfg).expect("first cycle");
        assert_eq!(
            entry_symbols(&first.execution.effects),
            BTreeSet::from(["VIABLEUSDT".into()])
        );
        assert_eq!(first.summary.planned_entries, 1);
        assert_eq!(first.summary.entry_cap_deferrals, 1);
        assert_eq!(
            first.next_state.entry_cycle_selected_symbols,
            BTreeSet::from(["VIABLEUSDT".into()])
        );
        assert!(first.execution.skipped.iter().any(
            |skip| matches!(skip, Skipped::NoPrice { symbol } if symbol == "MISSINGPRICEUSDT")
        ));
        assert!(first
            .execution
            .skipped
            .iter()
            .any(|skip| matches!(skip, Skipped::NoInstrumentRule { symbol } if symbol == "MISSINGRULEUSDT")));

        let mut recovered_input = input;
        recovered_input.now_ms += ENTRY_CYCLE_MS;
        recovered_input.owned_working_symbols = BTreeSet::from(["VIABLEUSDT".into()]);
        recovered_input.facts = entry_facts(&[
            "MISSINGPRICEUSDT",
            "MISSINGRULEUSDT",
            "VIABLEUSDT",
            "WAITUSDT",
        ]);
        let recovered = reduce_lifecycle(recovered_input, first.next_state, &cfg)
            .expect("next cycle with recovered facts");
        assert_eq!(
            entry_symbols(&recovered.execution.effects),
            BTreeSet::from(["MISSINGPRICEUSDT".into()])
        );
    }

    #[test]
    fn recovered_planner_facts_retry_a_stale_blocker_at_the_next_cycle() {
        let decision_ts_ms = 100 * DAY_MS;
        let now_ms = decision_ts_ms + 2 * ENTRY_CYCLE_MS;
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: BTreeMap::from([("AUSDT".into(), 0.1)]),
            universe_size: 100,
            replay_days: 45,
            gross: 0.1,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(decision_ts_ms, 1_000.0)]),
            desired_targets: BTreeMap::from([(
                "AUSDT".into(),
                StoredTarget {
                    notional_usdt: 100.0,
                    stop_loss_fraction: config().stop_loss_fraction,
                    leverage: config().entry_leverage,
                    entry_valid_until_ms: decision_ts_ms + config().execution.signal_validity_ms
                        - config().execution.engine_entry_cutoff_ms,
                },
            )]),
            last_publication_decision_ts_ms: decision_ts_ms,
            current_decision: Some(decision.clone()),
            entry_cycle_started_ms: now_ms - ENTRY_CYCLE_MS,
            entry_cycle_selected_symbols: BTreeSet::from(["AUSDT".into()]),
            ..SleeveState::default()
        };
        let output = reduce_lifecycle(
            ReducerInput {
                now_ms,
                decision,
                upcoming_decision: None,
                settled_funding: Vec::new(),
                presettlement: Vec::new(),
                durable_fires: Vec::new(),
                trail_by_symbol: BTreeMap::from([("AUSDT".into(), -1.0)]),
                entry_blockers: BTreeMap::from([("AUSDT".into(), "no_price".into())]),
                account_healthy: true,
                equity_usdt: 1_000.0,
                upcoming_sizing_equity_usdt: None,
                facts: entry_facts(&["AUSDT"]),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: None,
            },
            state,
            &config(),
        )
        .expect("recovered next-cycle facts");

        assert_eq!(
            entry_symbols(&output.execution.effects),
            ["AUSDT".into()].into()
        );
        assert_eq!(
            output.next_state.entry_cycle_selected_symbols,
            ["AUSDT".into()].into()
        );
    }

    #[test]
    fn scorer_catchup_advances_only_scorer_then_current_batch_plans_once() {
        let mut state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            desired_targets: BTreeMap::from([(
                "KEEPUSDT".into(),
                StoredTarget {
                    notional_usdt: 20.0,
                    stop_loss_fraction: 0.35,
                    leverage: 2.0,
                    entry_valid_until_ms: 60 * DAY_MS,
                },
            )]),
            refused_entries: BTreeSet::from(["KEEPUSDT".into()]),
            entry_cycle_started_ms: 45 * DAY_MS,
            entry_cycle_selected_symbols: BTreeSet::from(["KEEPUSDT".into()]),
            ..SleeveState::default()
        };
        for (sequence, day) in [46_i64, 47, 48].into_iter().enumerate() {
            let prior = state.clone();
            let rows = if day == 46 {
                scorer_rows(1, day)
            } else {
                scorer_rows(day, day)
            };
            let output = reduce_scorer_catchup(
                ScorerCatchupInput {
                    decision_ts_ms: day * DAY_MS,
                    rows,
                    signal_receipt: (
                        "worker".into(),
                        sequence as u64 + 1,
                        format!("catchup-{day}"),
                    ),
                },
                state,
                &config(),
            )
            .expect("contiguous catch-up");
            assert!(matches!(
                output.execution.effects.as_slice(),
                [
                    Effect::PersistCheckpoint { .. },
                    Effect::ConsumeSignal { .. }
                ]
            ));
            assert!(output.execution.skipped.is_empty());
            assert_eq!(output.next_state.scorer.last_decision_ts_ms, day * DAY_MS);
            let mut expected = prior;
            expected.scorer = output.next_state.scorer.clone();
            assert_eq!(output.next_state, expected);
            state = output.next_state;
        }

        assert!(reduce_scorer_catchup(
            ScorerCatchupInput {
                decision_ts_ms: 50 * DAY_MS,
                rows: scorer_rows(50, 50),
                signal_receipt: ("worker".into(), 4, "gap".into()),
            },
            state.clone(),
            &config(),
        )
        .expect_err("a missing catch-up day must fail")
        .contains("next UTC generation"));

        let current_day = 49;
        let placeholder = CarryDecision {
            schema_version: 1,
            decision_ts_ms: current_day * DAY_MS,
            weights: BTreeMap::new(),
            universe_size: 1,
            replay_days: 0,
            gross: 0.0,
        };
        let input = ReducerInput {
            now_ms: current_day * DAY_MS + 1,
            decision: placeholder,
            upcoming_decision: None,
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            durable_fires: Vec::new(),
            trail_by_symbol: BTreeMap::new(),
            entry_blockers: BTreeMap::new(),
            account_healthy: false,
            equity_usdt: 0.0,
            upcoming_sizing_equity_usdt: None,
            facts: PlannerFacts::default(),
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: Some(("worker".into(), 4, "current".into())),
        };
        let batch = CarrySignalBatch {
            schema_version: 1,
            decision_ts_ms: current_day * DAY_MS,
            rows: scorer_rows(current_day, current_day),
            upcoming_rows: Vec::new(),
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            marks: Vec::new(),
            rejections: Vec::new(),
        };
        let output = reduce_signal(batch.clone(), input.clone(), state, &config())
            .expect("ordinary current planning batch");
        assert_eq!(
            output
                .next_state
                .current_decision
                .as_ref()
                .expect("current decision")
                .decision_ts_ms,
            current_day * DAY_MS
        );
        assert_eq!(
            output.next_state.last_publication_decision_ts_ms,
            current_day * DAY_MS
        );
        let mut duplicate_input = input;
        duplicate_input.signal_receipt = Some(("worker".into(), 5, "current-duplicate".into()));
        let duplicate = reduce_signal(batch, duplicate_input, output.next_state.clone(), &config())
            .expect("same current generation is idempotent");
        assert_eq!(duplicate.next_state, output.next_state);
        assert!(duplicate.execution.skipped.is_empty());
    }

    #[test]
    fn fixture_lifecycle_emits_event_then_checkpoint_then_orders() {
        let fixture: serde_json::Value = serde_json::from_str(FIXTURE).expect("fixture");
        let decision: CarryDecision =
            serde_json::from_value(fixture["decision_input"]["decision"].clone())
                .expect("decision");
        let upcoming: CarryDecision =
            serde_json::from_value(fixture["decision_input"]["upcoming_decision"].clone())
                .expect("upcoming");
        let mut facts = PlannerFacts::default();
        for row in fixture["decision_input"]["holdings"]
            .as_array()
            .expect("holdings")
        {
            let symbol = row["symbol"].as_str().expect("symbol").to_owned();
            facts.held.insert(
                symbol.clone(),
                Held {
                    side: Side::Buy,
                    qty: row["qty"].as_f64().expect("qty"),
                    px: row["mark_px"].as_f64().expect("mark"),
                    entry_px: row["entry_px"].as_f64().expect("entry"),
                    stop_px: row["entry_px"].as_f64().expect("entry") * 0.65,
                },
            );
            facts
                .prices
                .insert(symbol, row["mark_px"].as_f64().expect("mark"));
        }
        for (symbol, price) in fixture["rust_replay"]["prices"]
            .as_object()
            .expect("prices")
        {
            facts
                .prices
                .insert(symbol.clone(), price.as_f64().expect("price"));
        }
        let rule: engine_types::InstrumentRule =
            serde_json::from_value(fixture["rust_replay"]["instrument_rule"].clone())
                .expect("instrument rule");
        facts.rules = decision
            .weights
            .keys()
            .map(|symbol| (symbol.clone(), rule))
            .collect();
        let presettlement: Vec<PresettlementObservation> = fixture["decision_input"]
            ["presettlement"]
            .as_array()
            .expect("pre")
            .iter()
            .map(|row| PresettlementObservation {
                symbol: row["symbol"].as_str().expect("symbol").into(),
                observed_ts_ms: row["observed_ts_ms"].as_i64().expect("observed"),
                settlement_ts_ms: row["settlement_ts_ms"].as_i64().expect("settlement"),
                running_rate: row["running_rate"].as_f64().expect("rate"),
                mark_px: row["mark_px"].as_f64(),
            })
            .collect();
        let settled: Vec<SettledFundingObservation> =
            serde_json::from_value(fixture["decision_input"]["settled_funding"].clone())
                .expect("settled");
        let state = SleeveState {
            schema_version: 1,
            sizing_anchors: BTreeMap::from([
                (1_799_913_600_000, 900.0),
                (1_800_000_000_000, 1_000.0),
            ]),
            ..SleeveState::default()
        };
        let input = ReducerInput {
            now_ms: 1_800_003_000_000,
            decision,
            upcoming_decision: Some(upcoming),
            settled_funding: settled,
            presettlement,
            durable_fires: vec![CarryPresettlementFire {
                event_id: carry_event_id(
                    "demo",
                    "lane2_carry_hold_v7",
                    1_800_000_000_000,
                    1_800_003_600_000,
                    "DURABLEUSDT",
                ),
                environment: "demo".into(),
                source_profile: "carry_hold_v7_live_v1".into(),
                source_config_id: "lane2_carry_hold_v7".into(),
                decision_ts_ms: 1_800_000_000_000,
                fired_ts_ms: 1_800_003_000_000,
                settlement_ts_ms: 1_800_003_600_000,
                symbol: "DURABLEUSDT".into(),
                mark_px: Some(10.0),
                carry_side: Some("long".into()),
                carry_qty: Some(2.0),
            }],
            trail_by_symbol: BTreeMap::from([
                ("BLOCKUSDT".into(), -5.0),
                ("BETAUSDT".into(), -4.0),
                ("GAMMAUSDT".into(), -3.0),
                ("NOPRICEUSDT".into(), -2.0),
                ("CAPUSDT".into(), -1.0),
            ]),
            entry_blockers: BTreeMap::from([("BLOCKUSDT".into(), "refused".into())]),
            account_healthy: true,
            equity_usdt: 1_200.0,
            upcoming_sizing_equity_usdt: Some(1_300.0),
            facts,
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: Some(("worker".into(), 1, "obs".into())),
        };
        let output = reduce_lifecycle(input.clone(), state.clone(), &config()).expect("lifecycle");
        assert_eq!(output.effective_decision.weights.len(), 7);
        assert_eq!(output.summary.planned_entries, 2);
        assert_eq!(output.summary.entry_cap_deferrals, 1);
        assert_eq!(output.presettlement_fires.len(), 1);
        assert!(matches!(
            output.execution.effects.as_slice(),
            [
                Effect::AppendCarryFire(_),
                Effect::PersistCheckpoint { .. },
                Effect::ConsumeSignal { .. },
                ..
            ]
        ));
        assert_eq!(
            output.next_state.sizing_anchors.get(&1_800_086_400_000),
            Some(&1_300.0)
        );

        let target_symbols = output
            .next_state
            .desired_targets
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>();
        assert_eq!(
            output.next_state.entry_cycle_selected_symbols.len(),
            config().max_new_entries_per_cycle
        );
        let regressed_cycle_start = input.now_ms + ENTRY_CYCLE_MS;
        let mut checkpoint_state = output.next_state.clone();
        checkpoint_state.entry_cycle_started_ms = regressed_cycle_start;
        let restored: SleeveState = serde_json::from_slice(&checkpoint_payload(&checkpoint_state))
            .expect("restart checkpoint");
        let mut repeated_input = input.clone();
        repeated_input.trail_by_symbol.clear();
        repeated_input.signal_receipt = None;
        let repeated =
            reduce_lifecycle(repeated_input, restored, &config()).expect("same generation wake");
        assert_eq!(
            repeated.next_state.entry_cycle_started_ms, regressed_cycle_start,
            "a backward wall clock preserves the durable admission window"
        );
        assert_eq!(
            repeated.next_state.entry_cycle_selected_symbols.len(),
            config().max_new_entries_per_cycle
        );
        assert_eq!(
            repeated
                .next_state
                .desired_targets
                .keys()
                .cloned()
                .collect::<BTreeSet<_>>(),
            target_symbols
        );

        let mut catch_up_state = output.next_state.clone();
        catch_up_state.last_publication_decision_ts_ms = input.decision.decision_ts_ms - DAY_MS;
        catch_up_state.entry_cycle_started_ms = input.now_ms;
        catch_up_state.entry_cycle_selected_symbols =
            BTreeSet::from(["BETAUSDT".to_owned(), "GAMMAUSDT".to_owned()]);
        let catch_up_selected = catch_up_state.entry_cycle_selected_symbols.clone();
        let mut catch_up_input = input.clone();
        catch_up_input.now_ms += 1;
        catch_up_input.signal_receipt = None;
        let catch_up = reduce_lifecycle(catch_up_input, catch_up_state, &config())
            .expect("next source generation inside the same admission window");
        assert_eq!(catch_up.next_state.entry_cycle_started_ms, input.now_ms);
        assert_eq!(
            catch_up.next_state.entry_cycle_selected_symbols, catch_up_selected,
            "source generation changes do not reset the durable time budget"
        );

        // Crash boundary: only the first event append reached the WAL. The
        // signal is delivered again with that durable event but the old
        // checkpoint. Recovery persists fired state without publishing a
        // second copy.
        let durable_event = output.presettlement_fires[0].clone();
        let mut retry = input;
        retry.durable_fires.push(durable_event.clone());
        let recovered = reduce_lifecycle(retry, state, &config()).expect("crash replay");
        assert!(recovered
            .execution
            .effects
            .iter()
            .all(|effect| !matches!(effect, Effect::AppendCarryFire(event) if event.event_id == durable_event.event_id)));
        assert!(matches!(
            recovered.execution.effects.first(),
            Some(Effect::PersistCheckpoint { .. })
        ));
        assert_eq!(
            recovered.next_state.fired_exits.get(&durable_event.symbol),
            Some(&durable_event.decision_ts_ms)
        );
    }

    #[test]
    fn disabled_entries_never_block_a_due_exit() {
        let mut cfg = config();
        cfg.entries_enabled = false;
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms: 2 * DAY_MS,
            weights: BTreeMap::new(),
            universe_size: 100,
            replay_days: 90,
            gross: 0.0,
        };
        let mut facts = PlannerFacts::default();
        facts.held.insert(
            "AUSDT".into(),
            Held {
                side: Side::Buy,
                qty: 1.0,
                px: 10.0,
                entry_px: 10.0,
                stop_px: 6.5,
            },
        );
        let output = reduce_lifecycle(
            ReducerInput {
                now_ms: 2 * DAY_MS,
                decision,
                upcoming_decision: None,
                settled_funding: vec![],
                presettlement: vec![],
                durable_fires: Vec::new(),
                trail_by_symbol: BTreeMap::new(),
                entry_blockers: BTreeMap::new(),
                account_healthy: true,
                equity_usdt: 1_000.0,
                upcoming_sizing_equity_usdt: None,
                facts,
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: None,
            },
            SleeveState::default(),
            &cfg,
        )
        .expect("exit");
        assert!(output.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Exit { .. },
                ..
            })
        )));
    }

    #[test]
    fn future_durable_decision_blocks_only_entries_and_growth() {
        let now_ms = 20 * DAY_MS;
        let decision_ts_ms = now_ms + ENTRY_CYCLE_MS;
        let symbols = ["FLATUSDT", "GROWUSDT", "REDUCEUSDT", "RESTOPUSDT"];
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: symbols
                .iter()
                .map(|symbol| ((*symbol).to_owned(), 0.1))
                .collect(),
            universe_size: 100,
            replay_days: 90,
            gross: 0.4,
        };
        let cfg = config();
        let entry_valid_until_ms = decision_ts_ms + cfg.execution.signal_validity_ms
            - cfg.execution.engine_entry_cutoff_ms;
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(decision_ts_ms, 1_000.0)]),
            desired_targets: symbols
                .iter()
                .map(|symbol| {
                    (
                        (*symbol).to_owned(),
                        StoredTarget {
                            notional_usdt: 100.0,
                            stop_loss_fraction: cfg.stop_loss_fraction,
                            leverage: cfg.entry_leverage,
                            entry_valid_until_ms,
                        },
                    )
                })
                .collect(),
            entry_cycle_started_ms: decision_ts_ms,
            entry_cycle_selected_symbols: BTreeSet::from(["FLATUSDT".to_owned()]),
            last_publication_decision_ts_ms: decision_ts_ms,
            current_decision: Some(decision.clone()),
            ..SleeveState::default()
        };
        let mut facts = entry_facts(&[
            "FLATUSDT",
            "GROWUSDT",
            "REDUCEUSDT",
            "RESTOPUSDT",
            "OLDUSDT",
        ]);
        for (symbol, qty, stop_px) in [
            ("GROWUSDT", 0.5, 65.0),
            ("REDUCEUSDT", 1.5, 65.0),
            ("RESTOPUSDT", 1.0, 50.0),
            ("OLDUSDT", 1.0, 65.0),
        ] {
            facts.held.insert(
                symbol.into(),
                Held {
                    side: Side::Buy,
                    qty,
                    px: 100.0,
                    entry_px: 100.0,
                    stop_px,
                },
            );
        }
        let input = ReducerInput {
            now_ms,
            decision,
            upcoming_decision: None,
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            durable_fires: Vec::new(),
            trail_by_symbol: BTreeMap::new(),
            entry_blockers: BTreeMap::new(),
            account_healthy: true,
            equity_usdt: 1_000.0,
            upcoming_sizing_equity_usdt: None,
            facts,
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };

        let output =
            reduce_lifecycle_with_mode(input.clone(), state, &cfg, ReplanMode::BootRecovery)
                .expect("backward-clock boot");
        let steps = output
            .execution
            .effects
            .iter()
            .filter_map(|effect| match effect {
                Effect::Order(order) => Some(&order.step),
                _ => None,
            });
        let steps = steps.collect::<Vec<_>>();
        assert!(!steps.iter().any(|step| matches!(
            step,
            Step::Enter { .. }
                | Step::Resize {
                    reduce_only: false,
                    ..
                }
        )));
        assert!(steps.iter().any(|step| matches!(
            step,
            Step::Resize {
                symbol,
                reduce_only: true,
                ..
            } if symbol == "REDUCEUSDT"
        )));
        assert!(steps
            .iter()
            .any(|step| matches!(step, Step::Restop { symbol, .. } if symbol == "RESTOPUSDT")));
        assert!(steps
            .iter()
            .any(|step| matches!(step, Step::Exit { symbol, .. } if symbol == "OLDUSDT")));
        assert!(!output
            .next_state
            .entry_cycle_selected_symbols
            .contains("FLATUSDT"));

        let mut resumed_input = input;
        resumed_input.now_ms = decision_ts_ms;
        let resumed = reduce_lifecycle(resumed_input, output.next_state, &cfg)
            .expect("decision-time recovery");
        assert!(entry_symbols(&resumed.execution.effects).contains("FLATUSDT"));
    }

    #[test]
    fn unhealthy_account_keeps_orphan_exits_live_across_wakes_and_boot() {
        let decision_ts_ms = 20 * DAY_MS;
        let now_ms = decision_ts_ms + 1_000;
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: BTreeMap::from([("AUSDT".into(), 0.1)]),
            universe_size: 100,
            replay_days: 90,
            gross: 0.1,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(decision_ts_ms, 1_000.0)]),
            desired_targets: BTreeMap::from([(
                "AUSDT".into(),
                StoredTarget {
                    notional_usdt: 100.0,
                    stop_loss_fraction: config().stop_loss_fraction,
                    leverage: config().entry_leverage,
                    entry_valid_until_ms: decision_ts_ms + config().execution.signal_validity_ms
                        - config().execution.engine_entry_cutoff_ms,
                },
            )]),
            last_publication_decision_ts_ms: decision_ts_ms,
            current_decision: Some(decision.clone()),
            entry_cycle_started_ms: now_ms,
            ..SleeveState::default()
        };
        let mut facts = entry_facts(&["AUSDT", "OLDUSDT"]);
        facts.held.insert(
            "OLDUSDT".into(),
            Held {
                side: Side::Buy,
                qty: 1.0,
                px: 10.0,
                entry_px: 10.0,
                stop_px: 6.5,
            },
        );
        let input = ReducerInput {
            now_ms,
            decision,
            upcoming_decision: None,
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            durable_fires: Vec::new(),
            trail_by_symbol: BTreeMap::from([("AUSDT".into(), -1.0)]),
            entry_blockers: BTreeMap::new(),
            account_healthy: false,
            equity_usdt: 1_000.0,
            upcoming_sizing_equity_usdt: None,
            facts,
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };
        let assert_only_orphan_exit = |output: &ReducerOutput| {
            assert!(output.execution.effects.iter().any(|effect| matches!(
                effect,
                Effect::Order(crate::native_common::OrderEffect {
                    step: Step::Exit { symbol, .. },
                    ..
                }) if symbol == "OLDUSDT"
            )));
            assert!(!output.execution.effects.iter().any(|effect| matches!(
                effect,
                Effect::Order(crate::native_common::OrderEffect {
                    step: Step::Enter { .. }
                        | Step::Resize {
                            reduce_only: false,
                            ..
                        },
                    ..
                })
            )));
        };

        let first = reduce_lifecycle(input.clone(), state, &config()).expect("first orphan exit");
        assert_only_orphan_exit(&first);
        let repeated = reduce_lifecycle(input.clone(), first.next_state.clone(), &config())
            .expect("repeated orphan exit");
        assert_only_orphan_exit(&repeated);
        let boot = reduce_lifecycle_with_mode(
            input,
            repeated.next_state,
            &config(),
            ReplanMode::BootRecovery,
        )
        .expect("boot orphan exit");
        assert_only_orphan_exit(&boot);
    }
}
