//! LONG's registered decision and execution contract in one Rust reducer.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::native_common::{
    checkpoint_payload, config_fingerprint, order_effects, valid_sha256, valid_symbol, Effect,
    ExecutionOutput, PlannedTarget, PlannerFacts, DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};
use crate::position_plan::{plan as plan_targets, PlanRules, Target};

pub const CONTRACT_SCHEMA_VERSION: u16 = 1;
const HOUR_MS: i64 = 3_600_000;
const DAY_MS: i64 = 24 * HOUR_MS;
// Admission cycles are one minute, matching the registered live-physics clock.
pub const ENTRY_CYCLE_MS: i64 = 60_000;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuleConfig {
    pub execution_strategy_id: String,
    pub entry_delay_hours: i64,
    pub fc_min_day_return: f64,
    pub fc_top_volume_rank_max: f64,
    pub fc_min_close_location: f64,
    pub fc_max_hold_days: i64,
    pub fc_max_atr_pct: f64,
    pub fc_atr_stop_mult: f64,
    pub fc_sigma_mult: f64,
    pub fc_sniper_retrace_pct: f64,
    pub fc_sniper_deadline_hours: i64,
    pub weekend_size_mult: f64,
    pub fc_close_loc_multi_day: f64,
    pub fc_stop_time_decay_hours: i64,
    pub fc_stop_time_decay_atr_mult: f64,
    pub max_concurrent_positions: usize,
    pub cooldown_days: i64,
    pub gross_exposure: f64,
    pub vol_floor_annual: f64,
    pub max_position_weight: f64,
    pub vol_target_annual: f64,
    pub vol_target_min_scale: f64,
    pub vol_target_max_scale: f64,
}

impl RuleConfig {
    pub fn validate_classification(&self) -> Result<(), &'static str> {
        let positive = [
            self.fc_min_day_return,
            self.fc_top_volume_rank_max,
            self.fc_min_close_location,
            self.fc_max_atr_pct,
            self.fc_atr_stop_mult,
            self.fc_sigma_mult,
            self.fc_close_loc_multi_day,
        ];
        if positive
            .iter()
            .any(|value| !value.is_finite() || *value <= 0.0)
            || self.fc_max_hold_days <= 0
            || self.fc_max_atr_pct >= 1.0
        {
            return Err("LONG classification rule is invalid");
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
    pub rule: RuleConfig,
    pub notional_multiplier: f64,
    pub entry_leverage: f64,
    pub order_notional_pct_equity: f64,
    pub wallet_balance_fraction: f64,
    pub max_new_entries_per_cycle: usize,
    pub signal_freshness_ms: i64,
    pub book_validity_ms: i64,
    pub entry_floor_usdt: f64,
    pub resize_floor_usdt: f64,
    pub resize_floor_fraction: f64,
    pub engine_entry_cutoff_ms: i64,
    pub rest_entries: bool,
    pub hold_decision_price: bool,
    pub give_up_instead_of_crossing: bool,
}

impl StrategyConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != CONTRACT_SCHEMA_VERSION {
            return Err("unsupported LONG config schema");
        }
        if self.profile_name.is_empty() || self.environment.is_empty() {
            return Err("LONG profile and environment are required");
        }
        if !valid_sha256(&self.rule_sha256)
            || !valid_sha256(&self.feature_contract_sha256)
            || !valid_sha256(&self.operational_profile_sha256)
        {
            return Err("LONG config hashes must be lowercase SHA-256");
        }
        let positive = [
            self.notional_multiplier,
            self.entry_leverage,
            self.wallet_balance_fraction,
            self.rule.fc_min_day_return,
            self.rule.fc_top_volume_rank_max,
            self.rule.fc_min_close_location,
            self.rule.fc_max_atr_pct,
            self.rule.fc_atr_stop_mult,
            self.rule.fc_sigma_mult,
            self.rule.fc_sniper_retrace_pct,
            self.rule.weekend_size_mult,
            self.rule.fc_close_loc_multi_day,
            self.rule.gross_exposure,
            self.rule.vol_floor_annual,
            self.rule.max_position_weight,
            self.rule.vol_target_annual,
            self.rule.vol_target_min_scale,
            self.rule.vol_target_max_scale,
        ];
        if positive
            .iter()
            .any(|value| !value.is_finite() || *value <= 0.0)
        {
            return Err("LONG positive config field is invalid");
        }
        let nonnegative = [
            self.order_notional_pct_equity,
            self.entry_floor_usdt,
            self.resize_floor_usdt,
            self.resize_floor_fraction,
            self.rule.fc_stop_time_decay_atr_mult,
        ];
        if nonnegative
            .iter()
            .any(|value| !value.is_finite() || *value < 0.0)
        {
            return Err("LONG nonnegative config field is invalid");
        }
        if self.wallet_balance_fraction > 1.0
            || self.order_notional_pct_equity > 10.0
            || self.resize_floor_fraction >= 1.0
            || self.rule.fc_sniper_retrace_pct >= 1.0
            || self.rule.fc_max_atr_pct >= 1.0
            || self.rule.vol_target_min_scale > self.rule.vol_target_max_scale
        {
            return Err("LONG config fraction is out of range");
        }
        if self.max_new_entries_per_cycle == 0
            || self.rule.max_concurrent_positions == 0
            || self.signal_freshness_ms <= 0
            || self.book_validity_ms <= self.engine_entry_cutoff_ms
            || self.engine_entry_cutoff_ms <= 0
            || self.rule.entry_delay_hours < 0
            || self.rule.fc_sniper_deadline_hours <= 0
            || self.rule.fc_max_hold_days <= 0
            || self.rule.cooldown_days < 0
            || self.rule.fc_stop_time_decay_hours < 0
            || (!self.rest_entries
                && (self.hold_decision_price || self.give_up_instead_of_crossing))
        {
            return Err("LONG integer config field is invalid");
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
    rule: &'a RuleConfig,
}

/// Stable identity for signal compatibility and durable reducer state.
/// Operational switches and sizing/execution limits deliberately are not
/// state identity: changing one must not orphan an already-open position.
pub fn decision_fingerprint(config: &StrategyConfig) -> String {
    config_fingerprint(&DecisionIdentity {
        schema_version: CONTRACT_SCHEMA_VERSION,
        sleeve: "long_native",
        profile_name: &config.profile_name,
        environment: &config.environment,
        rule_sha256: &config.rule_sha256,
        feature_contract_sha256: &config.feature_contract_sha256,
        rule: &config.rule,
    })
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct FeatureRow {
    pub symbol: String,
    pub ts_ms: i64,
    pub close: Option<f64>,
    pub turnover_quote: Option<f64>,
    pub log_return: Option<f64>,
    pub realized_vol: Option<f64>,
    pub sigma_daily_30d: Option<f64>,
    pub turnover_median_90d: Option<f64>,
    pub today_volume_rank: Option<f64>,
    pub universe_rank: Option<f64>,
    pub in_universe: bool,
    pub pump_3d_log: Option<f64>,
    pub pump_7d_log: Option<f64>,
    pub close_location: Option<f64>,
    pub close_loc_3d: Option<f64>,
    pub close_loc_7d: Option<f64>,
    pub atr_14d_pct: Option<f64>,
    pub regime_on: bool,
    pub btc_rv_30: Option<f64>,
    pub eth_regime_on: bool,
    pub symbol_age_days: Option<i64>,
}

/// Pump diagnostics and the final entry classification from the same rule the
/// native reducer uses. Research code may add path labels, but does not decide
/// whether a row is an entry.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchClassification {
    pub schema_version: u16,
    pub threshold_1d: f64,
    pub threshold_3d: f64,
    pub threshold_7d: f64,
    pub trigger_1d: bool,
    pub trigger_3d: bool,
    pub trigger_7d: bool,
    pub trigger_any: bool,
    pub source_strength: Option<f64>,
    pub pattern: Option<String>,
    pub stop_loss_fraction: f64,
    pub max_hold_days: i64,
}

impl ResearchClassification {
    fn entry(&self) -> Option<(f64, i64)> {
        self.pattern
            .as_ref()
            .map(|_| (self.stop_loss_fraction, self.max_hold_days))
    }
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

/// Exact `long_feature_batch` body emitted by the Rust signal worker.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LongSignalBatch {
    pub decision_ts_ms: i64,
    pub feature_ts_ms: i64,
    pub rows: Vec<FeatureRow>,
    pub marks: Vec<MarketMark>,
    pub cold_start_fallback_count: usize,
    pub rejections: Vec<DataRejection>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DecisionInput {
    pub decision_ts_ms: i64,
    pub symbol: String,
    pub signal_ts_ms: i64,
    pub signal_close: f64,
    pub market_price: Option<f64>,
    pub observed_low: Option<f64>,
    pub equity_usdt: f64,
    pub feature_row: Option<FeatureRow>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PriorState {
    pub requested: bool,
    pub filled: bool,
    pub entry_ts_ms: i64,
    pub entry_price: f64,
    pub target_notional_usdt: f64,
    pub stop_loss_fraction: f64,
    pub stop_decay_after_ms: i64,
    pub decayed_stop_loss_fraction: f64,
    pub max_hold_deadline_ts_ms: i64,
    #[serde(default)]
    pub max_hold_duration_ms: i64,
    pub entry_valid_until_ms: i64,
    pub cooldown_until_ms: i64,
    pub attempted_signal_ts_ms: i64,
    pub active_positions: usize,
}

#[derive(Copy, Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionAction {
    Wait,
    Enter,
    Hold,
    Exit,
    Reject,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DecisionOutput {
    pub schema_version: u16,
    pub action: DecisionAction,
    pub reason: String,
    pub decision_ts_ms: i64,
    pub symbol: String,
    pub signal_ts_ms: i64,
    pub entry_reason: String,
    pub position_weight: f64,
    pub target_fraction_of_equity: f64,
    pub target_notional_usdt: f64,
    pub entry_leverage: f64,
    pub stop_loss_fraction: f64,
    pub stop_decay_after_ms: i64,
    pub decayed_stop_loss_fraction: f64,
    pub max_hold_duration_ms: i64,
    pub entry_valid_until_ms: i64,
    pub wake_at_or_below: Option<f64>,
}

impl DecisionOutput {
    fn empty(action: DecisionAction, reason: &str, input: &DecisionInput) -> Self {
        Self {
            schema_version: CONTRACT_SCHEMA_VERSION,
            action,
            reason: reason.to_owned(),
            decision_ts_ms: input.decision_ts_ms,
            symbol: input.symbol.clone(),
            signal_ts_ms: 0,
            entry_reason: String::new(),
            position_weight: 0.0,
            target_fraction_of_equity: 0.0,
            target_notional_usdt: 0.0,
            entry_leverage: 0.0,
            stop_loss_fraction: 0.0,
            stop_decay_after_ms: 0,
            decayed_stop_loss_fraction: 0.0,
            max_hold_duration_ms: 0,
            entry_valid_until_ms: 0,
            wake_at_or_below: None,
        }
    }
}

pub fn decide(
    input: &DecisionInput,
    prior: &PriorState,
    config: &StrategyConfig,
) -> Result<DecisionOutput, &'static str> {
    config.validate()?;
    validate_input(input)?;
    if prior.requested {
        return Ok(decide_existing(input, prior, config));
    }
    let Some(row) = input.feature_row.as_ref() else {
        return Ok(DecisionOutput::empty(
            DecisionAction::Reject,
            "no_signal",
            input,
        ));
    };
    let signal_ts_ms = if input.signal_ts_ms == 0 {
        row.ts_ms
    } else {
        input.signal_ts_ms
    };
    let Some(row_close) = safe(row.close) else {
        return Ok(DecisionOutput::empty(
            DecisionAction::Reject,
            "invalid_signal_identity",
            input,
        ));
    };
    let signal_close = if input.signal_close == 0.0 {
        row_close
    } else {
        input.signal_close
    };
    if row.symbol.to_ascii_uppercase() != input.symbol
        || row.ts_ms != signal_ts_ms
        || signal_close <= 0.0
        || row_close <= 0.0
        || signal_close != row_close
    {
        return Ok(DecisionOutput::empty(
            DecisionAction::Reject,
            "invalid_signal_identity",
            input,
        ));
    }
    let Some((stop_loss_fraction, hold_days)) = classify_feature(row, &config.rule).entry() else {
        let mut output = DecisionOutput::empty(DecisionAction::Reject, "no_signal", input);
        output.signal_ts_ms = signal_ts_ms;
        return Ok(output);
    };
    let age_ms = input.decision_ts_ms - signal_ts_ms;
    if age_ms < 0 {
        let mut output = DecisionOutput::empty(DecisionAction::Wait, "signal_not_available", input);
        output.signal_ts_ms = signal_ts_ms;
        return Ok(output);
    }
    for (condition, reason) in [
        (age_ms >= config.signal_freshness_ms, "signal_stale"),
        (prior.cooldown_until_ms > input.decision_ts_ms, "cooldown"),
        (
            signal_ts_ms <= prior.attempted_signal_ts_ms,
            "signal_already_attempted",
        ),
        (
            prior.active_positions >= config.rule.max_concurrent_positions,
            "capacity",
        ),
    ] {
        if condition {
            let mut output = DecisionOutput::empty(DecisionAction::Reject, reason, input);
            output.signal_ts_ms = signal_ts_ms;
            return Ok(output);
        }
    }
    let first_check_ms = signal_ts_ms + config.rule.entry_delay_hours.max(1) * HOUR_MS;
    let retrace = signal_close * (1.0 - config.rule.fc_sniper_retrace_pct);
    if input.decision_ts_ms < first_check_ms {
        let mut output = DecisionOutput::empty(DecisionAction::Wait, "entry_delay", input);
        output.signal_ts_ms = signal_ts_ms;
        output.wake_at_or_below = Some(retrace);
        return Ok(output);
    }
    let market = safe(input.market_price).filter(|value| *value > 0.0);
    let low = safe(input.observed_low).filter(|value| *value > 0.0);
    let observed = match (market, low) {
        (Some(left), Some(right)) => Some(left.min(right)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    };
    if market.is_none() || observed.is_none() {
        let mut output = DecisionOutput::empty(DecisionAction::Wait, "no_market_price", input);
        output.signal_ts_ms = signal_ts_ms;
        output.wake_at_or_below = Some(retrace);
        return Ok(output);
    }
    let deadline = signal_ts_ms + config.rule.fc_sniper_deadline_hours * HOUR_MS;
    let reason = if observed.is_some_and(|value| value <= retrace) {
        "sniper_retrace"
    } else if input.decision_ts_ms >= deadline {
        "sniper_deadline_fallthru"
    } else {
        let mut output = DecisionOutput::empty(DecisionAction::Wait, "awaiting_retrace", input);
        output.signal_ts_ms = signal_ts_ms;
        output.wake_at_or_below = Some(retrace);
        return Ok(output);
    };
    let base = if config.order_notional_pct_equity > 0.0 {
        config.order_notional_pct_equity
    } else {
        config.rule.gross_exposure / config.rule.max_concurrent_positions as f64
            * config.notional_multiplier
    };
    let rv = safe(row.btc_rv_30).filter(|value| *value != 0.0);
    let vol_scale = (config.rule.vol_target_annual
        / rv.unwrap_or(config.rule.vol_target_annual).max(1e-6))
    .clamp(
        config.rule.vol_target_min_scale,
        config.rule.vol_target_max_scale,
    );
    let realized = safe(row.realized_vol).unwrap_or(config.rule.vol_floor_annual);
    let notional_weight = config.rule.gross_exposure / config.rule.max_concurrent_positions as f64;
    let mut position_weight = (config.rule.vol_floor_annual
        / realized.max(config.rule.vol_floor_annual))
    .min(config.rule.max_position_weight / notional_weight)
    .max(0.25);
    if config.rule.weekend_size_mult != 1.0 && is_weekend(input.decision_ts_ms) {
        position_weight *= config.rule.weekend_size_mult;
    }
    let target_fraction = config.wallet_balance_fraction * base * vol_scale * position_weight;
    let target_notional = input.equity_usdt * target_fraction;
    if !target_fraction.is_finite()
        || target_fraction <= 0.0
        || !target_notional.is_finite()
        || target_notional <= 0.0
        || !(0.0..1.0).contains(&stop_loss_fraction)
        || stop_loss_fraction == 0.0
        || hold_days <= 0
    {
        let mut output = DecisionOutput::empty(DecisionAction::Reject, "invalid_entry_plan", input);
        output.signal_ts_ms = signal_ts_ms;
        return Ok(output);
    }
    let atr = safe(row.atr_14d_pct).unwrap_or(0.0);
    let (stop_decay_after_ms, decayed_stop_loss_fraction) = if config.rule.fc_stop_time_decay_hours
        > 0
        && config.rule.fc_stop_time_decay_atr_mult > 0.0
        && atr > 0.0
    {
        (
            config.rule.fc_stop_time_decay_hours * HOUR_MS,
            config.rule.fc_stop_time_decay_atr_mult * atr,
        )
    } else {
        (0, 0.0)
    };
    Ok(DecisionOutput {
        schema_version: CONTRACT_SCHEMA_VERSION,
        action: DecisionAction::Enter,
        reason: reason.to_owned(),
        decision_ts_ms: input.decision_ts_ms,
        symbol: input.symbol.clone(),
        signal_ts_ms,
        entry_reason: reason.to_owned(),
        position_weight,
        target_fraction_of_equity: target_fraction,
        target_notional_usdt: target_notional,
        entry_leverage: config.entry_leverage,
        stop_loss_fraction,
        stop_decay_after_ms,
        decayed_stop_loss_fraction,
        max_hold_duration_ms: hold_days * DAY_MS,
        entry_valid_until_ms: (input.decision_ts_ms + config.book_validity_ms)
            .min(signal_ts_ms + config.signal_freshness_ms),
        wake_at_or_below: None,
    })
}

fn decide_existing(
    input: &DecisionInput,
    prior: &PriorState,
    config: &StrategyConfig,
) -> DecisionOutput {
    if !prior.filled {
        if prior.entry_valid_until_ms > 0 && input.decision_ts_ms >= prior.entry_valid_until_ms {
            let mut output = DecisionOutput::empty(DecisionAction::Exit, "entry_expired", input);
            output.signal_ts_ms = input.signal_ts_ms;
            return output;
        }
        let mut output = DecisionOutput::empty(DecisionAction::Hold, "entry_pending", input);
        output.signal_ts_ms = input.signal_ts_ms;
        output.target_notional_usdt = prior.target_notional_usdt;
        output.entry_leverage = config.entry_leverage;
        output.stop_loss_fraction = prior.stop_loss_fraction;
        output.stop_decay_after_ms = prior.stop_decay_after_ms;
        output.decayed_stop_loss_fraction = prior.decayed_stop_loss_fraction;
        output.entry_valid_until_ms = prior.entry_valid_until_ms;
        return output;
    }
    if prior.max_hold_deadline_ts_ms > 0 && input.decision_ts_ms >= prior.max_hold_deadline_ts_ms {
        let mut output = DecisionOutput::empty(DecisionAction::Exit, "time_stop", input);
        output.signal_ts_ms = input.signal_ts_ms;
        return output;
    }
    let stop = current_stop(prior, input.decision_ts_ms);
    let observed = [safe(input.market_price), safe(input.observed_low)]
        .into_iter()
        .flatten()
        .filter(|value| *value > 0.0)
        .reduce(f64::min);
    if prior.entry_price > 0.0
        && stop > 0.0
        && stop < 1.0
        && observed.is_some_and(|value| value <= prior.entry_price * (1.0 - stop))
    {
        let reason = if stop < prior.stop_loss_fraction {
            "decayed_stop_loss"
        } else {
            "stop_loss"
        };
        let mut output = DecisionOutput::empty(DecisionAction::Exit, reason, input);
        output.signal_ts_ms = input.signal_ts_ms;
        output.stop_loss_fraction = stop;
        return output;
    }
    let mut output = DecisionOutput::empty(DecisionAction::Hold, "held", input);
    output.signal_ts_ms = input.signal_ts_ms;
    output.target_notional_usdt = prior.target_notional_usdt;
    output.entry_leverage = config.entry_leverage;
    output.stop_loss_fraction = stop;
    output.stop_decay_after_ms = prior.stop_decay_after_ms;
    output.decayed_stop_loss_fraction = prior.decayed_stop_loss_fraction;
    output.entry_valid_until_ms = prior.entry_valid_until_ms;
    output
}

fn current_stop(prior: &PriorState, now_ms: i64) -> f64 {
    if prior.stop_decay_after_ms > 0
        && prior.decayed_stop_loss_fraction > 0.0
        && prior.decayed_stop_loss_fraction < 1.0
        && prior.entry_ts_ms > 0
        && prior.entry_price > 0.0
        && now_ms >= prior.entry_ts_ms + prior.stop_decay_after_ms
    {
        prior
            .stop_loss_fraction
            .min(prior.decayed_stop_loss_fraction)
    } else {
        prior.stop_loss_fraction
    }
}

pub fn classify_feature(row: &FeatureRow, rule: &RuleConfig) -> ResearchClassification {
    let sigma = safe(row.sigma_daily_30d);
    let one_day_threshold = if sigma.is_some_and(|value| value > 0.0) {
        rule.fc_sigma_mult * sigma.expect("positive sigma")
    } else {
        rule.fc_min_day_return.ln_1p()
    };
    let threshold_3d = one_day_threshold * 3.0_f64.sqrt();
    let threshold_7d = one_day_threshold * 7.0_f64.sqrt();
    let magnitude_1d = safe(row.log_return).is_some_and(|value| value >= one_day_threshold);
    let magnitude_3d = safe(row.pump_3d_log).is_some_and(|value| value >= threshold_3d);
    let magnitude_7d = safe(row.pump_7d_log).is_some_and(|value| value >= threshold_7d);
    let mut source_strength: Option<f64> = None;
    for (value, threshold) in [
        (safe(row.log_return), one_day_threshold),
        (safe(row.pump_3d_log), threshold_3d),
        (safe(row.pump_7d_log), threshold_7d),
    ] {
        if let Some(ratio) = value
            .filter(|_| threshold > 0.0)
            .map(|value| value / threshold)
        {
            source_strength = Some(source_strength.map_or(ratio, |current| current.max(ratio)));
        }
    }

    let eligible = row.in_universe
        && row.regime_on
        && row.eth_regime_on
        && safe(row.today_volume_rank).is_some_and(|value| value <= rule.fc_top_volume_rank_max)
        && safe(row.log_return).is_some();
    let active_trigger = (magnitude_1d
        && safe(row.close_location).is_some_and(|value| value >= rule.fc_min_close_location))
        || (magnitude_3d
            && safe(row.close_loc_3d).is_some_and(|value| value >= rule.fc_close_loc_multi_day))
        || (magnitude_7d
            && safe(row.close_loc_7d).is_some_and(|value| value >= rule.fc_close_loc_multi_day));
    let atr = safe(row.atr_14d_pct);
    let accepted = eligible
        && active_trigger
        && atr.is_some_and(|value| value > 0.0 && value <= rule.fc_max_atr_pct);
    ResearchClassification {
        schema_version: CONTRACT_SCHEMA_VERSION,
        threshold_1d: one_day_threshold,
        threshold_3d,
        threshold_7d,
        trigger_1d: magnitude_1d,
        trigger_3d: magnitude_3d,
        trigger_7d: magnitude_7d,
        trigger_any: magnitude_1d || magnitude_3d || magnitude_7d,
        source_strength,
        pattern: accepted.then(|| "fomo_chase".to_owned()),
        stop_loss_fraction: if accepted {
            atr.expect("accepted classification has finite ATR") * rule.fc_atr_stop_mult
        } else {
            0.0
        },
        max_hold_days: if accepted { rule.fc_max_hold_days } else { 0 },
    }
}

fn safe(value: Option<f64>) -> Option<f64> {
    value.filter(|number| number.is_finite())
}

fn validate_input(input: &DecisionInput) -> Result<(), &'static str> {
    if input.decision_ts_ms <= 0 {
        return Err("LONG decision time must be positive");
    }
    if !valid_symbol(&input.symbol) {
        return Err("LONG symbol must be uppercase alphanumeric");
    }
    if !input.equity_usdt.is_finite() || input.equity_usdt < 0.0 {
        return Err("LONG equity must be finite and nonnegative");
    }
    Ok(())
}

fn is_weekend(ts_ms: i64) -> bool {
    let days = ts_ms.div_euclid(DAY_MS);
    let weekday = (days + 3).rem_euclid(7);
    weekday >= 5
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SleeveState {
    pub schema_version: u16,
    pub symbols: BTreeMap<String, PriorState>,
    #[serde(default)]
    pub pending_signals: BTreeMap<String, PendingSignal>,
    pub exit_pending: BTreeSet<String>,
    pub cooldown_until_ms: BTreeMap<String, i64>,
    pub attempted_signal_ts_ms: BTreeMap<String, i64>,
    pub refused_entries: BTreeSet<String>,
    #[serde(default)]
    pub entry_cycle_started_ms: i64,
    #[serde(default)]
    pub entry_cycle_selected_symbols: BTreeSet<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PendingSignal {
    pub signal_ts_ms: i64,
    pub signal_close: f64,
    pub feature_row: FeatureRow,
}

impl SleeveState {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION {
            return Err("unsupported LONG checkpoint schema");
        }
        for (symbol, prior) in &self.symbols {
            if !valid_symbol(symbol)
                || ![
                    prior.entry_price,
                    prior.target_notional_usdt,
                    prior.stop_loss_fraction,
                    prior.decayed_stop_loss_fraction,
                ]
                .iter()
                .all(|value| value.is_finite() && *value >= 0.0)
                || prior.entry_ts_ms < 0
                || prior.max_hold_deadline_ts_ms < 0
                || prior.max_hold_duration_ms < 0
                || prior.entry_valid_until_ms < 0
                || prior.cooldown_until_ms < 0
                || prior.attempted_signal_ts_ms < 0
            {
                return Err("LONG checkpoint symbol state is invalid");
            }
            if prior.requested
                && (prior.target_notional_usdt <= 0.0
                    || !(0.0..1.0).contains(&prior.stop_loss_fraction)
                    || prior.entry_valid_until_ms <= 0
                    || prior.attempted_signal_ts_ms <= 0)
            {
                return Err("LONG requested state is invalid");
            }
            if prior.filled
                && (!prior.requested
                    || prior.entry_ts_ms <= 0
                    || prior.entry_price <= 0.0
                    || prior.max_hold_deadline_ts_ms <= prior.entry_ts_ms)
            {
                return Err("LONG filled state is invalid");
            }
        }
        for (symbol, pending) in &self.pending_signals {
            if symbol != &pending.feature_row.symbol
                || !valid_symbol(symbol)
                || pending.signal_ts_ms <= 0
                || pending.feature_row.ts_ms != pending.signal_ts_ms
                || !pending.signal_close.is_finite()
                || pending.signal_close <= 0.0
                || safe(pending.feature_row.close) != Some(pending.signal_close)
            {
                return Err("LONG pending signal is invalid");
            }
        }
        if self.exit_pending.iter().any(|symbol| !valid_symbol(symbol))
            || self
                .cooldown_until_ms
                .iter()
                .any(|(symbol, ts)| !valid_symbol(symbol) || *ts <= 0)
            || self
                .attempted_signal_ts_ms
                .iter()
                .any(|(symbol, ts)| !valid_symbol(symbol) || *ts <= 0)
            || self
                .refused_entries
                .iter()
                .any(|symbol| !valid_symbol(symbol))
            || self.entry_cycle_started_ms < 0
            || self
                .entry_cycle_selected_symbols
                .iter()
                .any(|symbol| !valid_symbol(symbol))
            || (self.entry_cycle_started_ms == 0 && !self.entry_cycle_selected_symbols.is_empty())
        {
            return Err("LONG checkpoint portfolio state is invalid");
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct BatchInput {
    pub now_ms: i64,
    pub decisions: Vec<DecisionInput>,
    pub facts: PlannerFacts,
    pub owned_working_symbols: BTreeSet<String>,
    pub owned_opening_order_ids: BTreeMap<String, Vec<String>>,
    pub checkpoint_fingerprint: Option<String>,
    pub signal_receipt: Option<(String, u64, String)>,
}

#[derive(Clone, Debug, Serialize)]
pub struct BatchOutput {
    pub decisions: Vec<DecisionOutput>,
    pub next_state: SleeveState,
    pub execution: ExecutionOutput,
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum ReplanMode {
    Ordinary,
    BootRecovery,
}

fn remember_pending_signal(state: &mut SleeveState, input: &DecisionInput) {
    let Some(feature_row) = input.feature_row.clone() else {
        return;
    };
    let signal_ts_ms = if input.signal_ts_ms == 0 {
        feature_row.ts_ms
    } else {
        input.signal_ts_ms
    };
    let signal_close = if input.signal_close == 0.0 {
        feature_row.close.unwrap_or(0.0)
    } else {
        input.signal_close
    };
    state.pending_signals.insert(
        input.symbol.clone(),
        PendingSignal {
            signal_ts_ms,
            signal_close,
            feature_row,
        },
    );
}

/// Complete live transition. Restore mismatch with surviving exposure emits
/// only cancels/flat targets; a future signal remains pending until its clock
/// catches up, while a repeated signal changes no state.
pub fn reduce_batch(
    input: BatchInput,
    state: SleeveState,
    config: &StrategyConfig,
) -> Result<BatchOutput, &'static str> {
    reduce_batch_with_mode(input, state, config, ReplanMode::Ordinary)
}

pub fn reduce_batch_with_mode(
    input: BatchInput,
    mut state: SleeveState,
    config: &StrategyConfig,
    replan_mode: ReplanMode,
) -> Result<BatchOutput, &'static str> {
    config.validate()?;
    let original_state = state.clone();
    let fingerprint = config.fingerprint();
    let mismatch = input
        .checkpoint_fingerprint
        .as_ref()
        .is_some_and(|seen| seen != &fingerprint);
    if state.schema_version == 0 {
        state.schema_version = DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION;
    }
    state.validate()?;
    if input.now_ms <= 0
        || input
            .decisions
            .iter()
            .any(|decision| decision.decision_ts_ms > input.now_ms)
    {
        return Err("LONG batch wall clock is invalid");
    }
    let now_ms = input.now_ms;
    let clock_blocked_growth_symbols = state
        .symbols
        .iter()
        .filter(|(_, prior)| prior.attempted_signal_ts_ms > now_ms)
        .map(|(symbol, _)| symbol.clone())
        .collect::<BTreeSet<_>>();
    if !input.decisions.is_empty()
        && (state.entry_cycle_started_ms == 0
            || now_ms >= state.entry_cycle_started_ms.saturating_add(ENTRY_CYCLE_MS))
    {
        state.entry_cycle_started_ms = now_ms;
        state.entry_cycle_selected_symbols.clear();
        state.refused_entries.clear();
    }
    if replan_mode == ReplanMode::BootRecovery {
        state
            .entry_cycle_selected_symbols
            .retain(|symbol| !clock_blocked_growth_symbols.contains(symbol));
    }
    if state.entry_cycle_selected_symbols.len() > config.max_new_entries_per_cycle {
        state.entry_cycle_selected_symbols = state
            .entry_cycle_selected_symbols
            .into_iter()
            .take(config.max_new_entries_per_cycle)
            .collect();
    }

    let mut targets = Vec::new();
    let mut outputs = Vec::new();
    let mut entry_effect_symbols = BTreeSet::new();
    let mut next = state.clone();
    if mismatch {
        let recovery_symbols = input
            .facts
            .held_symbols()
            .into_iter()
            .chain(input.owned_working_symbols.iter().cloned())
            .collect::<BTreeSet<_>>();
        for symbol in &recovery_symbols {
            targets.push(PlannedTarget {
                target: Target {
                    symbol: symbol.clone(),
                    notional_usdt: 0.0,
                    stop_loss_fraction: 0.01,
                    entry_valid_until_ms: None,
                    target_qty: None,
                },
                leverage: config.entry_leverage,
            });
        }
        next.exit_pending.extend(recovery_symbols);
    } else {
        // Reconcile durable requests with the attributed position and owned
        // order books before considering a new signal generation.
        let known_symbols = next.symbols.keys().cloned().collect::<Vec<_>>();
        for symbol in known_symbols {
            let mut remove = false;
            if let Some(prior) = next.symbols.get_mut(&symbol) {
                if let Some(holding) = input.facts.held.get(&symbol) {
                    if !prior.filled {
                        prior.filled = true;
                        prior.entry_ts_ms = now_ms;
                        prior.entry_price = holding.entry_px;
                        prior.max_hold_deadline_ts_ms =
                            now_ms.saturating_add(prior.max_hold_duration_ms);
                    } else if holding.entry_px > 0.0 {
                        prior.entry_price = holding.entry_px;
                    }
                } else if prior.filled && !input.owned_working_symbols.contains(&symbol) {
                    remove = true;
                    next.cooldown_until_ms.insert(
                        symbol.clone(),
                        now_ms.saturating_add(config.rule.cooldown_days * DAY_MS),
                    );
                    next.exit_pending.remove(&symbol);
                }
            }
            if remove {
                next.symbols.remove(&symbol);
                next.pending_signals.remove(&symbol);
            }
        }

        // Newest generation per symbol, then the deployed ranking. This is
        // where portfolio capacity and the per-cycle entry cap live; a plug
        // cannot accidentally admit rows in delivery order.
        let mut newest = BTreeMap::<String, DecisionInput>::new();
        for decision in input.decisions.iter().cloned() {
            let generation = if decision.signal_ts_ms == 0 {
                decision.feature_row.as_ref().map_or(0, |row| row.ts_ms)
            } else {
                decision.signal_ts_ms
            };
            let replace = newest.get(&decision.symbol).is_none_or(|old| {
                let old_generation = if old.signal_ts_ms == 0 {
                    old.feature_row.as_ref().map_or(0, |row| row.ts_ms)
                } else {
                    old.signal_ts_ms
                };
                generation > old_generation
            });
            if replace {
                newest.insert(decision.symbol.clone(), decision);
            }
        }
        let mut decisions = newest.into_values().collect::<Vec<_>>();
        decisions.sort_by(|left, right| {
            let generation = |value: &DecisionInput| {
                if value.signal_ts_ms == 0 {
                    value.feature_row.as_ref().map_or(0, |row| row.ts_ms)
                } else {
                    value.signal_ts_ms
                }
            };
            let score = |value: &DecisionInput| {
                value
                    .feature_row
                    .as_ref()
                    .and_then(|row| row.log_return)
                    .filter(|number| number.is_finite())
                    .unwrap_or(f64::NEG_INFINITY)
            };
            let rank = |value: &DecisionInput| {
                value
                    .feature_row
                    .as_ref()
                    .and_then(|row| row.today_volume_rank)
                    .filter(|number| number.is_finite())
                    .unwrap_or(f64::INFINITY)
            };
            generation(right)
                .cmp(&generation(left))
                .then_with(|| score(right).total_cmp(&score(left)))
                .then_with(|| rank(left).total_cmp(&rank(right)))
                .then_with(|| left.symbol.cmp(&right.symbol))
        });
        let mut active = next
            .symbols
            .values()
            .filter(|state| state.requested)
            .count()
            .max(input.facts.held.len());
        for mut decision_input in decisions {
            let generation = if decision_input.signal_ts_ms == 0 {
                decision_input
                    .feature_row
                    .as_ref()
                    .map_or(0, |row| row.ts_ms)
            } else {
                decision_input.signal_ts_ms
            };
            if next.refused_entries.contains(&decision_input.symbol) {
                let attempted = next
                    .attempted_signal_ts_ms
                    .get(&decision_input.symbol)
                    .copied()
                    .unwrap_or(0);
                if generation <= attempted {
                    continue;
                }
                next.refused_entries.remove(&decision_input.symbol);
            }
            let prior = next
                .symbols
                .get(&decision_input.symbol)
                .cloned()
                .unwrap_or_default();
            let mut prior = prior;
            prior.active_positions = active;
            prior.cooldown_until_ms = next
                .cooldown_until_ms
                .get(&decision_input.symbol)
                .copied()
                .unwrap_or(prior.cooldown_until_ms);
            prior.attempted_signal_ts_ms = next
                .attempted_signal_ts_ms
                .get(&decision_input.symbol)
                .copied()
                .unwrap_or(prior.attempted_signal_ts_ms);
            decision_input.equity_usdt = decision_input.equity_usdt.max(0.0);
            let output = decide(&decision_input, &prior, config)?;
            let mut updated = prior.clone();
            let retire_expired = output.action == DecisionAction::Exit
                && output.reason == "entry_expired"
                && !input.facts.held.contains_key(&decision_input.symbol)
                && !input.owned_working_symbols.contains(&decision_input.symbol);
            match output.action {
                DecisionAction::Enter => {
                    if !prior.requested
                        && !next
                            .entry_cycle_selected_symbols
                            .contains(&decision_input.symbol)
                        && next.entry_cycle_selected_symbols.len()
                            >= config.max_new_entries_per_cycle
                    {
                        remember_pending_signal(&mut next, &decision_input);
                        continue;
                    }
                    if !config.entries_enabled || mismatch {
                        remember_pending_signal(&mut next, &decision_input);
                        outputs.push(output);
                        continue;
                    }
                    updated.requested = true;
                    updated.filled = false;
                    updated.entry_price = decision_input.market_price.unwrap_or(0.0);
                    updated.target_notional_usdt = output.target_notional_usdt;
                    updated.stop_loss_fraction = output.stop_loss_fraction;
                    updated.stop_decay_after_ms = output.stop_decay_after_ms;
                    updated.decayed_stop_loss_fraction = output.decayed_stop_loss_fraction;
                    updated.entry_valid_until_ms = output.entry_valid_until_ms;
                    updated.max_hold_duration_ms = output.max_hold_duration_ms;
                    updated.attempted_signal_ts_ms = output.signal_ts_ms;
                    next.attempted_signal_ts_ms
                        .insert(decision_input.symbol.clone(), output.signal_ts_ms);
                    active += 1;
                    next.pending_signals.remove(&decision_input.symbol);
                }
                DecisionAction::Exit if !retire_expired => {
                    next.exit_pending.insert(decision_input.symbol.clone());
                    next.pending_signals.remove(&decision_input.symbol);
                }
                DecisionAction::Exit => {
                    next.pending_signals.remove(&decision_input.symbol);
                }
                DecisionAction::Wait if !prior.requested => {
                    remember_pending_signal(&mut next, &decision_input);
                }
                DecisionAction::Reject if !prior.requested => {
                    next.pending_signals.remove(&decision_input.symbol);
                }
                DecisionAction::Wait | DecisionAction::Hold | DecisionAction::Reject => {}
            }
            let unresolved_open = (prior.requested || updated.requested)
                && !updated.filled
                && !input.facts.held.contains_key(&decision_input.symbol)
                && !input.owned_working_symbols.contains(&decision_input.symbol)
                && config.entries_enabled
                && !mismatch
                && !clock_blocked_growth_symbols.contains(&decision_input.symbol)
                && !next.refused_entries.contains(&decision_input.symbol);
            if unresolved_open {
                let already_selected = next
                    .entry_cycle_selected_symbols
                    .contains(&decision_input.symbol);
                if already_selected {
                    if replan_mode == ReplanMode::BootRecovery {
                        entry_effect_symbols.insert(decision_input.symbol.clone());
                    }
                } else if next.entry_cycle_selected_symbols.len() < config.max_new_entries_per_cycle
                {
                    next.entry_cycle_selected_symbols
                        .insert(decision_input.symbol.clone());
                    entry_effect_symbols.insert(decision_input.symbol.clone());
                }
            }
            if retire_expired {
                next.symbols.remove(&decision_input.symbol);
            } else if output.action == DecisionAction::Exit {
                targets.push(PlannedTarget {
                    target: Target {
                        symbol: decision_input.symbol.clone(),
                        notional_usdt: 0.0,
                        stop_loss_fraction: updated.stop_loss_fraction.max(0.01),
                        entry_valid_until_ms: None,
                        target_qty: None,
                    },
                    leverage: config.entry_leverage,
                });
            } else if output.target_notional_usdt > 0.0 {
                targets.push(PlannedTarget {
                    target: Target {
                        symbol: decision_input.symbol.clone(),
                        notional_usdt: output.target_notional_usdt,
                        stop_loss_fraction: output.stop_loss_fraction,
                        entry_valid_until_ms: Some(output.entry_valid_until_ms),
                        target_qty: None,
                    },
                    leverage: output.entry_leverage,
                });
            }
            if !retire_expired && (prior.requested || updated.requested) {
                next.symbols.insert(decision_input.symbol.clone(), updated);
            }
            outputs.push(output);
        }

        // A partial feature batch is not a partial desired book. Every
        // unresolved request remains an explicit target until its own reducer
        // transition removes it.
        for (symbol, prior) in &next.symbols {
            if prior.requested
                && !next.exit_pending.contains(symbol)
                && !targets.iter().any(|target| target.target.symbol == *symbol)
            {
                targets.push(PlannedTarget {
                    target: Target {
                        symbol: symbol.clone(),
                        notional_usdt: prior.target_notional_usdt,
                        stop_loss_fraction: current_stop(prior, now_ms),
                        entry_valid_until_ms: Some(prior.entry_valid_until_ms),
                        target_qty: None,
                    },
                    leverage: config.entry_leverage,
                });
            }
        }
    }

    // An exit stays in the checkpoint until the venue is conclusively flat and
    // no owned entry remains. Only that joined fact retires the symbol.
    next.exit_pending.retain(|symbol| {
        input.facts.held.contains_key(symbol) || input.owned_working_symbols.contains(symbol)
    });
    next.cooldown_until_ms.retain(|_, until| *until > now_ms);
    let live_symbols = next.symbols.keys().cloned().collect::<BTreeSet<_>>();
    let pending_symbols = next
        .pending_signals
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>();
    let freshness_floor = now_ms.saturating_sub(config.signal_freshness_ms);
    next.attempted_signal_ts_ms.retain(|symbol, attempted| {
        *attempted >= freshness_floor
            || live_symbols.contains(symbol)
            || pending_symbols.contains(symbol)
    });
    next.refused_entries
        .retain(|symbol| next.attempted_signal_ts_ms.contains_key(symbol));
    let held_symbols = input.facts.held_symbols();
    let raw_targets = targets
        .iter()
        .map(|target| target.target.clone())
        .collect::<Vec<_>>();
    let valid_until = input
        .decisions
        .iter()
        .map(|decision| decision.decision_ts_ms + config.book_validity_ms)
        .max()
        .unwrap_or(i64::MAX);
    let planned = plan_targets(
        &raw_targets,
        &held_symbols,
        &input.facts,
        now_ms,
        valid_until,
        PlanRules {
            entry_floor_usdt: config.entry_floor_usdt,
            resize_floor_usdt: config.resize_floor_usdt,
            resize_floor_fraction: config.resize_floor_fraction,
            entry_cutoff_ms: config.engine_entry_cutoff_ms,
        },
    );
    let mut effects = Vec::new();
    if next != original_state || input.signal_receipt.is_some() {
        effects.push(Effect::PersistCheckpoint {
            symbol: String::new(),
            config_fingerprint: fingerprint,
            payload: checkpoint_payload(&next),
        });
    }
    if let Some((source, sequence, observation_id)) = input.signal_receipt {
        effects.push(Effect::ConsumeSignal {
            source,
            sequence,
            observation_id,
        });
    }
    for symbol in &next.exit_pending {
        for client_order_id in input
            .owned_opening_order_ids
            .get(symbol)
            .into_iter()
            .flatten()
        {
            effects.push(Effect::CancelOwned {
                symbol: symbol.clone(),
                client_order_id: client_order_id.clone(),
            });
        }
    }
    let steps = planned
        .steps
        .into_iter()
        .filter(|step| {
            let selected_entry = !matches!(step, crate::position_plan::Step::Enter { .. })
                || entry_effect_symbols.contains(step.symbol());
            let growth_allowed = config.entries_enabled
                && !mismatch
                && !clock_blocked_growth_symbols.contains(step.symbol());
            let growth_not_blocked = !matches!(
                step,
                crate::position_plan::Step::Enter { .. }
                    | crate::position_plan::Step::Resize {
                        reduce_only: false,
                        ..
                    }
            ) || growth_allowed;
            let opening_not_refused = !matches!(
                step,
                crate::position_plan::Step::Enter { .. }
                    | crate::position_plan::Step::Resize {
                        reduce_only: false,
                        ..
                    }
            ) || !next.refused_entries.contains(step.symbol());
            selected_entry
                && growth_not_blocked
                && opening_not_refused
                && (!input.owned_working_symbols.contains(step.symbol())
                    || matches!(step, crate::position_plan::Step::Restop { .. }))
        })
        .collect();
    effects.extend(order_effects(steps, &targets, "long-native"));
    Ok(BatchOutput {
        decisions: outputs,
        next_state: next,
        execution: ExecutionOutput {
            effects,
            skipped: planned.skipped,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    const FIXTURE: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/long_native_replay_v1.json"
    ));

    fn fixture_config() -> StrategyConfig {
        StrategyConfig {
            schema_version: 1,
            profile_name: "v12".into(),
            environment: "mainnet".into(),
            rule_sha256: "1".repeat(64),
            feature_contract_sha256: "3".repeat(64),
            operational_profile_sha256: "2".repeat(64),
            entries_enabled: true,
            rule: RuleConfig {
                execution_strategy_id: "long_native_v12_wide_stop".into(),
                entry_delay_hours: 1,
                fc_min_day_return: 0.15,
                fc_top_volume_rank_max: 10.0,
                fc_min_close_location: 0.7,
                fc_max_hold_days: 3,
                fc_max_atr_pct: 0.12,
                fc_atr_stop_mult: 3.0,
                fc_sigma_mult: 2.5,
                fc_sniper_retrace_pct: 0.01,
                fc_sniper_deadline_hours: 6,
                weekend_size_mult: 1.5,
                fc_close_loc_multi_day: 0.6,
                fc_stop_time_decay_hours: 48,
                fc_stop_time_decay_atr_mult: 1.5,
                max_concurrent_positions: 10,
                cooldown_days: 7,
                gross_exposure: 1.0,
                vol_floor_annual: 0.3,
                max_position_weight: 0.3,
                vol_target_annual: 0.6,
                vol_target_min_scale: 0.3,
                vol_target_max_scale: 1.25,
            },
            notional_multiplier: 6.0,
            entry_leverage: 5.0,
            order_notional_pct_equity: 0.0,
            wallet_balance_fraction: 1.0,
            max_new_entries_per_cycle: 5,
            signal_freshness_ms: 86_400_000,
            book_validity_ms: 3_600_000,
            entry_floor_usdt: 6.0,
            resize_floor_usdt: 1.0,
            resize_floor_fraction: 0.05,
            engine_entry_cutoff_ms: 900_000,
            rest_entries: false,
            hold_decision_price: false,
            give_up_instead_of_crossing: false,
        }
    }

    fn entry_input(symbol: &str, signal_ts_ms: i64) -> DecisionInput {
        DecisionInput {
            decision_ts_ms: signal_ts_ms + HOUR_MS,
            symbol: symbol.into(),
            signal_ts_ms,
            signal_close: 100.0,
            market_price: Some(98.0),
            observed_low: Some(98.0),
            equity_usdt: 1_000.0,
            feature_row: Some(FeatureRow {
                symbol: symbol.into(),
                ts_ms: signal_ts_ms,
                close: Some(100.0),
                log_return: Some(0.2),
                sigma_daily_30d: Some(0.04),
                today_volume_rank: Some(1.0),
                in_universe: true,
                close_location: Some(0.8),
                atr_14d_pct: Some(0.05),
                regime_on: true,
                eth_regime_on: true,
                ..FeatureRow::default()
            }),
        }
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
                .map(|symbol| ((*symbol).to_owned(), 98.0))
                .collect(),
            rules: symbols
                .iter()
                .map(|symbol| ((*symbol).to_owned(), rule))
                .collect(),
            ..PlannerFacts::default()
        }
    }

    fn entered_symbols(effects: &[Effect]) -> BTreeSet<String> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                Effect::Order(crate::native_common::OrderEffect {
                    step: crate::position_plan::Step::Enter { symbol, .. },
                    ..
                }) => Some(symbol.clone()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn research_classification_is_the_reducer_entry_rule() {
        let config = fixture_config();
        let row = FeatureRow {
            symbol: "ALPHAUSDT".into(),
            ts_ms: DAY_MS,
            close: Some(100.0),
            log_return: Some(0.11),
            sigma_daily_30d: Some(0.04),
            today_volume_rank: Some(1.0),
            in_universe: true,
            close_location: Some(0.8),
            atr_14d_pct: Some(0.05),
            regime_on: true,
            eth_regime_on: true,
            ..FeatureRow::default()
        };

        let classification = classify_feature(&row, &config.rule);

        assert!(classification.trigger_1d);
        assert_eq!(classification.pattern.as_deref(), Some("fomo_chase"));
        assert_eq!(classification.max_hold_days, config.rule.fc_max_hold_days);
        assert_eq!(
            classification.stop_loss_fraction,
            0.05 * config.rule.fc_atr_stop_mult
        );
    }

    #[test]
    fn every_recorded_decision_matches_the_rust_contract() {
        let fixture: Value = serde_json::from_str(FIXTURE).expect("fixture");
        let mut expected_by_name = BTreeMap::<String, DecisionOutput>::new();
        expected_by_name.insert(
            "flat_entry".to_owned(),
            serde_json::from_value(fixture["expected_decision_output"].clone())
                .expect("flat-entry output"),
        );
        for case in fixture["recorded_decision_cases"]
            .as_array()
            .expect("Python cases")
        {
            expected_by_name.insert(
                case["name"].as_str().expect("case name").to_owned(),
                serde_json::from_value(case["expected_decision_output"].clone())
                    .expect("case output"),
            );
        }

        let mut seen = BTreeSet::new();
        for line in fixture["strategy_event_tape"]["utf8"]
            .as_str()
            .expect("event tape")
            .lines()
        {
            let tape_row: Value = serde_json::from_str(line).expect("tape row");
            let envelope = &tape_row["event"]["payload"]["replay_envelope"];
            let name = envelope["case_name"].as_str().expect("case name");
            let mut input_value = envelope["decision_input"].clone();
            input_value
                .as_object_mut()
                .expect("decision input")
                .remove("schema_version");
            let input: DecisionInput =
                serde_json::from_value(input_value).expect("typed decision input");
            let prior: PriorState =
                serde_json::from_value(envelope["prior_state"].clone()).expect("typed prior state");
            let actual = decide(&input, &prior, &fixture_config()).expect("decision");
            assert_eq!(
                actual,
                *expected_by_name.get(name).expect("expected case output"),
                "case {name}"
            );
            seen.insert(name.to_owned());
        }
        assert_eq!(seen.len(), expected_by_name.len());
        assert_eq!(seen, expected_by_name.into_keys().collect());
    }

    #[test]
    fn future_signal_and_checkpoint_mismatch_fail_closed() {
        let config = fixture_config();
        let row = FeatureRow {
            symbol: "BTCUSDT".into(),
            ts_ms: 2_000,
            ..FeatureRow::default()
        };
        let input = DecisionInput {
            decision_ts_ms: 1_000,
            symbol: "BTCUSDT".into(),
            signal_ts_ms: 2_000,
            signal_close: 1.0,
            market_price: Some(1.0),
            observed_low: None,
            equity_usdt: 1.0,
            feature_row: Some(row),
        };
        assert_ne!(
            decide(&input, &PriorState::default(), &config)
                .expect("decision")
                .action,
            DecisionAction::Enter
        );

        let mut facts = PlannerFacts::default();
        facts.held.insert(
            "BTCUSDT".into(),
            crate::position_plan::Held {
                qty: 1.0,
                side: engine_types::Side::Buy,
                px: 10.0,
                entry_px: 10.0,
                stop_px: 8.0,
            },
        );
        let output = reduce_batch(
            BatchInput {
                now_ms: 1_000,
                decisions: vec![],
                facts,
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: Some("f".repeat(64)),
                signal_receipt: None,
            },
            SleeveState::default(),
            &config,
        )
        .expect("recovery plan");
        assert!(matches!(
            output.execution.effects.first(),
            Some(Effect::PersistCheckpoint { .. })
        ));
        assert!(output.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: crate::position_plan::Step::Exit { .. },
                ..
            })
        )));
    }

    #[test]
    fn backward_clock_preserves_a_pending_signal_until_it_can_enter() {
        let generation = 10 * DAY_MS;
        let mut future = entry_input("BTCUSDT", generation);
        future.decision_ts_ms = generation - 1;
        let before = reduce_batch(
            BatchInput {
                now_ms: generation - 1,
                decisions: vec![future],
                facts: entry_facts(&["BTCUSDT"]),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: Some(("worker".into(), 1, "future-observation".into())),
            },
            SleeveState::default(),
            &fixture_config(),
        )
        .expect("backward-clock plan");
        assert!(matches!(
            before.decisions.as_slice(),
            [DecisionOutput {
                action: DecisionAction::Wait,
                reason,
                ..
            }] if reason == "signal_not_available"
        ));
        assert!(before.execution.effects.iter().all(|effect| !matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: crate::position_plan::Step::Enter { .. },
                ..
            })
        )));

        let restored: SleeveState = serde_json::from_slice(&checkpoint_payload(&before.next_state))
            .expect("restart checkpoint");
        let pending = restored
            .pending_signals
            .get("BTCUSDT")
            .expect("the future signal remains durable")
            .clone();
        let caught_up_at = generation + HOUR_MS;
        let after = reduce_batch(
            BatchInput {
                now_ms: caught_up_at,
                decisions: vec![DecisionInput {
                    decision_ts_ms: caught_up_at,
                    symbol: "BTCUSDT".into(),
                    signal_ts_ms: pending.signal_ts_ms,
                    signal_close: pending.signal_close,
                    market_price: Some(98.0),
                    observed_low: Some(98.0),
                    equity_usdt: 1_000.0,
                    feature_row: Some(pending.feature_row),
                }],
                facts: entry_facts(&["BTCUSDT"]),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: None,
            },
            restored,
            &fixture_config(),
        )
        .expect("caught-up plan");

        assert!(entered_symbols(&after.execution.effects).contains("BTCUSDT"));
        assert!(!after.next_state.pending_signals.contains_key("BTCUSDT"));
        assert!(after.next_state.symbols["BTCUSDT"].requested);
    }

    #[test]
    fn disabled_signal_and_cycle_budget_survive_repeated_wakes() {
        let generation = 10 * DAY_MS;
        let mut disabled = fixture_config();
        disabled.entries_enabled = false;
        let first = reduce_batch(
            BatchInput {
                now_ms: generation + HOUR_MS,
                decisions: vec![entry_input("AUSDT", generation)],
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: Some(("worker".into(), 1, "obs".into())),
            },
            SleeveState::default(),
            &disabled,
        )
        .expect("disabled generation");
        assert!(first.next_state.pending_signals.contains_key("AUSDT"));

        let mut enabled = fixture_config();
        enabled.max_new_entries_per_cycle = 1;
        let admitted = reduce_batch(
            BatchInput {
                now_ms: generation + HOUR_MS,
                decisions: vec![
                    entry_input("AUSDT", generation),
                    entry_input("BUSDT", generation),
                ],
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: None,
            },
            first.next_state,
            &enabled,
        )
        .expect("enabled generation");
        assert_eq!(admitted.next_state.symbols.len(), 1);
        assert_eq!(admitted.next_state.entry_cycle_selected_symbols.len(), 1);
        assert_eq!(
            admitted
                .next_state
                .attempted_signal_ts_ms
                .values()
                .filter(|timestamp| **timestamp == generation)
                .count(),
            1
        );

        let mut checkpoint_state = admitted.next_state;
        checkpoint_state.entry_cycle_started_ms = generation + 2 * HOUR_MS;
        let restored: SleeveState = serde_json::from_slice(&checkpoint_payload(&checkpoint_state))
            .expect("restart checkpoint");
        let repeated = reduce_batch(
            BatchInput {
                now_ms: generation + HOUR_MS,
                decisions: vec![
                    entry_input("AUSDT", generation),
                    entry_input("BUSDT", generation),
                ],
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                checkpoint_fingerprint: None,
                signal_receipt: None,
            },
            restored,
            &enabled,
        )
        .expect("repeated generation");
        assert_eq!(repeated.next_state.symbols.len(), 1);
        assert_eq!(repeated.next_state.entry_cycle_selected_symbols.len(), 1);
        assert_eq!(
            repeated.next_state.entry_cycle_started_ms,
            generation + 2 * HOUR_MS,
            "a backward wall clock preserves the durable admission window"
        );
        assert!(repeated.next_state.pending_signals.contains_key("BUSDT"));
        assert_eq!(
            repeated
                .next_state
                .attempted_signal_ts_ms
                .values()
                .filter(|timestamp| **timestamp == generation)
                .count(),
            1
        );
    }

    #[test]
    fn delayed_batch_uses_engine_wall_time_for_the_entry_cutoff() {
        let generation = 10 * DAY_MS;
        let decision = entry_input("BTCUSDT", generation);
        let config = fixture_config();
        let deadline = decision
            .decision_ts_ms
            .saturating_add(config.book_validity_ms)
            .saturating_sub(config.engine_entry_cutoff_ms);
        let make_input = |now_ms| BatchInput {
            now_ms,
            decisions: vec![decision.clone()],
            facts: entry_facts(&["BTCUSDT"]),
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };

        let before = reduce_batch(make_input(deadline - 1), SleeveState::default(), &config)
            .expect("batch before cutoff");
        assert!(entered_symbols(&before.execution.effects).contains("BTCUSDT"));

        let closed = reduce_batch(make_input(deadline), SleeveState::default(), &config)
            .expect("batch at cutoff");
        assert!(entered_symbols(&closed.execution.effects).is_empty());
        assert!(matches!(
            closed.execution.skipped.as_slice(),
            [crate::position_plan::Skipped::EntryWindowClosed { symbol }]
                if symbol == "BTCUSDT"
        ));
    }

    #[test]
    fn takeover_budget_is_durable_silent_same_cycle_and_exact_on_boot() {
        let generation = 10 * DAY_MS;
        let symbols = ["AUSDT", "BUSDT", "CUSDT"];
        let decisions = symbols
            .iter()
            .map(|symbol| entry_input(symbol, generation))
            .collect::<Vec<_>>();
        let facts = entry_facts(&symbols);
        let input = || BatchInput {
            now_ms: generation + HOUR_MS,
            decisions: decisions.clone(),
            facts: facts.clone(),
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };

        let mut seed_config = fixture_config();
        seed_config.max_new_entries_per_cycle = symbols.len();
        let seeded = reduce_batch(input(), SleeveState::default(), &seed_config).expect("seed");
        let mut takeover = seeded.next_state;
        takeover.entry_cycle_started_ms = 0;
        takeover.entry_cycle_selected_symbols.clear();

        let mut config = fixture_config();
        config.max_new_entries_per_cycle = 2;
        let first = reduce_batch(input(), takeover, &config).expect("takeover admission");
        assert_eq!(first.next_state.symbols.len(), 3);
        assert_eq!(first.next_state.entry_cycle_selected_symbols.len(), 2);
        assert_eq!(entered_symbols(&first.execution.effects).len(), 2);
        assert!(matches!(
            first.execution.effects.first(),
            Some(Effect::PersistCheckpoint { .. })
        ));

        let same_cycle = reduce_batch(input(), first.next_state.clone(), &config)
            .expect("ordinary same-cycle wake");
        assert!(entered_symbols(&same_cycle.execution.effects).is_empty());
        assert_eq!(same_cycle.next_state.symbols.len(), 3);

        let restored: SleeveState =
            serde_json::from_slice(&checkpoint_payload(&first.next_state)).expect("restore");
        let boot = reduce_batch_with_mode(input(), restored, &config, ReplanMode::BootRecovery)
            .expect("boot recovery");
        assert_eq!(
            entered_symbols(&boot.execution.effects),
            first.next_state.entry_cycle_selected_symbols
        );

        let refused_symbol = first
            .next_state
            .entry_cycle_selected_symbols
            .iter()
            .next()
            .expect("selected symbol")
            .clone();
        let mut refused = first.next_state;
        refused.refused_entries.insert(refused_symbol.clone());
        let refused_wake = reduce_batch(input(), refused, &config).expect("same-cycle refusal");
        assert!(entered_symbols(&refused_wake.execution.effects).is_empty());
        assert_eq!(refused_wake.next_state.symbols.len(), 3);

        let mut next_cycle_input = input();
        next_cycle_input.now_ms += ENTRY_CYCLE_MS;
        for decision in &mut next_cycle_input.decisions {
            decision.decision_ts_ms += ENTRY_CYCLE_MS;
        }
        let retried = reduce_batch(next_cycle_input, refused_wake.next_state, &config)
            .expect("next-cycle refusal retry");
        assert!(entered_symbols(&retried.execution.effects).contains(&refused_symbol));
        assert!(retried.next_state.refused_entries.is_empty());
    }

    #[test]
    fn future_attempted_signal_blocks_only_growth_and_resumes_at_its_clock() {
        let now_ms = 20 * DAY_MS;
        let attempted_signal_ts_ms = now_ms + ENTRY_CYCLE_MS;
        let symbols = [
            "OPENUSDT",
            "GROWUSDT",
            "REDUCEUSDT",
            "RESTOPUSDT",
            "EXITUSDT",
        ];
        let prior = |filled: bool, max_hold_deadline_ts_ms: i64| PriorState {
            requested: true,
            filled,
            entry_ts_ms: if filled { now_ms - DAY_MS } else { 0 },
            entry_price: if filled { 10.0 } else { 0.0 },
            target_notional_usdt: 100.0,
            stop_loss_fraction: 0.2,
            stop_decay_after_ms: 0,
            decayed_stop_loss_fraction: 0.0,
            max_hold_deadline_ts_ms,
            max_hold_duration_ms: 2 * DAY_MS,
            entry_valid_until_ms: attempted_signal_ts_ms + HOUR_MS,
            cooldown_until_ms: 0,
            attempted_signal_ts_ms,
            active_positions: 0,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            symbols: BTreeMap::from([
                ("OPENUSDT".into(), prior(false, 0)),
                ("GROWUSDT".into(), prior(true, now_ms + DAY_MS)),
                ("REDUCEUSDT".into(), prior(true, now_ms + DAY_MS)),
                ("RESTOPUSDT".into(), prior(true, now_ms + DAY_MS)),
                ("EXITUSDT".into(), prior(true, now_ms)),
            ]),
            entry_cycle_started_ms: attempted_signal_ts_ms,
            entry_cycle_selected_symbols: BTreeSet::from(["OPENUSDT".to_owned()]),
            ..SleeveState::default()
        };
        let mut facts = entry_facts(&symbols);
        for symbol in symbols {
            facts.prices.insert(symbol.to_owned(), 10.0);
        }
        for (symbol, qty, stop_px) in [
            ("GROWUSDT", 5.0, 8.0),
            ("REDUCEUSDT", 15.0, 8.0),
            ("RESTOPUSDT", 10.0, 5.0),
            ("EXITUSDT", 10.0, 8.0),
        ] {
            facts.held.insert(
                symbol.into(),
                crate::position_plan::Held {
                    side: engine_types::Side::Buy,
                    qty,
                    px: 10.0,
                    entry_px: 10.0,
                    stop_px,
                },
            );
        }
        let decisions = symbols
            .iter()
            .map(|symbol| DecisionInput {
                decision_ts_ms: now_ms,
                symbol: (*symbol).to_owned(),
                signal_ts_ms: attempted_signal_ts_ms,
                signal_close: 0.0,
                market_price: Some(10.0),
                observed_low: None,
                equity_usdt: 1_000.0,
                feature_row: None,
            })
            .collect::<Vec<_>>();
        let input = BatchInput {
            now_ms,
            decisions,
            facts,
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            checkpoint_fingerprint: None,
            signal_receipt: None,
        };

        let boot = reduce_batch_with_mode(
            input.clone(),
            state,
            &fixture_config(),
            ReplanMode::BootRecovery,
        )
        .expect("backward-clock boot");
        let steps = boot
            .execution
            .effects
            .iter()
            .filter_map(|effect| match effect {
                Effect::Order(order) => Some(&order.step),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert!(!steps.iter().any(|step| matches!(
            step,
            crate::position_plan::Step::Enter { .. }
                | crate::position_plan::Step::Resize {
                    reduce_only: false,
                    ..
                }
        )));
        assert!(steps.iter().any(|step| matches!(
            step,
            crate::position_plan::Step::Resize {
                symbol,
                reduce_only: true,
                ..
            } if symbol == "REDUCEUSDT"
        )));
        assert!(steps.iter().any(|step| matches!(
            step,
            crate::position_plan::Step::Restop { symbol, .. } if symbol == "RESTOPUSDT"
        )));
        assert!(steps.iter().any(|step| matches!(
            step,
            crate::position_plan::Step::Exit { symbol, .. } if symbol == "EXITUSDT"
        )));
        assert!(!boot
            .next_state
            .entry_cycle_selected_symbols
            .contains("OPENUSDT"));

        let mut resumed_input = input;
        resumed_input.now_ms = attempted_signal_ts_ms;
        for decision in &mut resumed_input.decisions {
            decision.decision_ts_ms = attempted_signal_ts_ms;
        }
        let resumed = reduce_batch(resumed_input, boot.next_state, &fixture_config())
            .expect("clock recovery");
        assert!(entered_symbols(&resumed.execution.effects).contains("OPENUSDT"));
    }

    #[test]
    fn disabled_entries_never_top_up_a_partial_position_but_allow_reduction() {
        let now_ms = 20 * DAY_MS;
        let prior = PriorState {
            requested: true,
            filled: true,
            entry_ts_ms: now_ms - DAY_MS,
            entry_price: 10.0,
            target_notional_usdt: 100.0,
            stop_loss_fraction: 0.2,
            stop_decay_after_ms: now_ms + HOUR_MS,
            decayed_stop_loss_fraction: 0.1,
            max_hold_deadline_ts_ms: now_ms + DAY_MS,
            max_hold_duration_ms: 2 * DAY_MS,
            entry_valid_until_ms: now_ms + HOUR_MS,
            cooldown_until_ms: 0,
            attempted_signal_ts_ms: now_ms - DAY_MS,
            active_positions: 1,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            symbols: BTreeMap::from([("AUSDT".into(), prior)]),
            ..SleeveState::default()
        };
        let mut config = fixture_config();
        config.entries_enabled = false;
        let run = |qty| {
            let mut facts = entry_facts(&["AUSDT"]);
            facts.held.insert(
                "AUSDT".into(),
                crate::position_plan::Held {
                    qty,
                    side: engine_types::Side::Buy,
                    px: 10.0,
                    entry_px: 10.0,
                    stop_px: 8.0,
                },
            );
            reduce_batch(
                BatchInput {
                    now_ms,
                    decisions: Vec::new(),
                    facts,
                    owned_working_symbols: BTreeSet::new(),
                    owned_opening_order_ids: BTreeMap::new(),
                    checkpoint_fingerprint: None,
                    signal_receipt: None,
                },
                state.clone(),
                &config,
            )
            .expect("paused replan")
        };

        let partial = run(5.0);
        assert!(!partial.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: crate::position_plan::Step::Resize {
                    reduce_only: false,
                    ..
                },
                ..
            })
        )));

        let oversized = run(15.0);
        assert!(oversized.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: crate::position_plan::Step::Resize {
                    reduce_only: true,
                    ..
                },
                ..
            })
        )));
    }

    #[test]
    fn checkpoint_mismatch_cancels_a_flat_working_opening_order() {
        let output = reduce_batch(
            BatchInput {
                now_ms: DAY_MS,
                decisions: Vec::new(),
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::from(["AUSDT".into()]),
                owned_opening_order_ids: BTreeMap::from([(
                    "AUSDT".into(),
                    vec!["old-opening".into()],
                )]),
                checkpoint_fingerprint: Some("f".repeat(64)),
                signal_receipt: None,
            },
            SleeveState::default(),
            &fixture_config(),
        )
        .expect("mismatch recovery");

        assert!(output.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::CancelOwned {
                symbol,
                client_order_id,
            } if symbol == "AUSDT" && client_order_id == "old-opening"
        )));
        assert!(!output
            .execution
            .effects
            .iter()
            .any(|effect| matches!(effect, Effect::Order(_))));
    }
}
