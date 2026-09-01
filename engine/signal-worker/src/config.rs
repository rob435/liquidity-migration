use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::worker::WorkerError;
use crate::SCHEMA_VERSION;
pub use engine_strategies::native_common::SignalConfigIdentity as ConfigIdentity;

pub const MAX_LONG_COLD_START_LOOKBACK_DAYS: usize = 180;
pub const MAX_CARRY_SOURCE_HISTORY_HOURS: i64 = 182 * 24;
pub const MAX_WHALE_FEED_DAYS: usize = 30;
const SOURCE_HISTORY_PADDING_HOURS: i64 = 48;

pub(crate) fn carry_kline_feature_history_hours(carry: &CarryFeatureConfig) -> Option<i64> {
    let volatility = carry
        .vol_window_hours
        .checked_add(carry.vol_return_lag_hours)?;
    let trail = carry
        .trail_change_lookback_hours
        .checked_add(carry.trail_window_hours)?;
    let turnover = carry
        .turn_growth_lookback_hours
        .checked_add(carry.adv_window_hours)?;
    [
        carry.momentum_lookback_hours,
        carry.return_lookback_hours,
        volatility,
        trail,
        turnover,
    ]
    .into_iter()
    .max()
}

pub(crate) fn carry_source_history_hours(
    carry: &CarryFeatureConfig,
    include_replay: bool,
) -> Option<i64> {
    let replay_hours = if include_replay {
        i64::try_from(carry.minimum_replay_days)
            .ok()?
            .checked_mul(24)?
    } else {
        0
    };
    replay_hours
        .checked_add(carry_kline_feature_history_hours(carry)?)?
        .checked_add(SOURCE_HISTORY_PADDING_HOURS)
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LongFeatureConfig {
    pub profile_name: String,
    pub execution_strategy_id: String,
    pub exclude_symbols: Vec<String>,
    pub universe_size: usize,
    pub universe_volume_window_days: usize,
    pub min_listing_history_days: usize,
    pub regime_symbol: String,
    pub regime_sma_days: usize,
    pub vol_estimate_window_days: usize,
    pub daily_min_hourly_bars: usize,
    pub cold_start_lookback_days: usize,
    pub pump_lookback_days: [usize; 2],
    pub atr_window_days: usize,
    pub atr_min_samples: usize,
    pub btc_rv_window_days: usize,
    pub btc_rv_min_samples: usize,
    pub btc_rv_null_value: f64,
    pub regime_missing_is_on: bool,
    pub median_fallback_to_daily_turnover: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CarryFeatureConfig {
    pub config_id: String,
    pub universe_top_n: usize,
    pub enter_bp: f64,
    pub persistence_window_settlements: Option<usize>,
    pub momentum_lookback_hours: i64,
    pub adv_window_hours: i64,
    pub return_lookback_hours: i64,
    pub vol_window_hours: i64,
    pub vol_return_lag_hours: i64,
    pub vol_required_finite_samples: usize,
    pub trail_window_hours: i64,
    pub trail_change_lookback_hours: i64,
    pub turn_growth_lookback_hours: i64,
    pub whale_change_lookback_hours: i64,
    pub whale_freshness_hours: i64,
    pub whale_feed_days: usize,
    pub settlement_age_reset_threshold_hours: f64,
    pub decision_phase_ms: i64,
    pub decision_kline_lag_ms: i64,
    pub minimum_replay_days: usize,
    pub minimum_decision_symbols: usize,
    pub minimum_funding_coverage: f64,
    pub standing_funding_max_age_hours: f64,
    pub presettlement_window_ms: i64,
    pub missing_conditioning: String,
    pub missing_depth: String,
    pub stale_whale: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourceContract {
    pub bybit_category: String,
    pub bybit_settle_coin: String,
    pub bybit_mainnet_host: String,
    pub bybit_demo_host: String,
    pub binance_host: String,
    pub kline_interval_minutes: u32,
    pub funding_event_kind: String,
    pub whale_source: String,
    pub whale_period: String,
    pub mark_max_age_ms: i64,
    pub universe_identity_required: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LiveAcquisitionConfig {
    /// Realm whose reviewed universe and strategy state this process serves.
    pub environment: String,
    /// Public market-data realm. Demo execution deliberately observes mainnet.
    pub public_market_realm: String,
    pub request_timeout_ms: u64,
    pub request_retries: usize,
    pub retry_base_ms: u64,
    pub ticker_cadence_ms: u64,
    pub instrument_cadence_ms: u64,
    pub funding_cadence_ms: u64,
    pub kline_cadence_ms: u64,
    pub whale_cadence_ms: u64,
    pub max_parallel_requests: usize,
    pub kline_page_limit: usize,
    pub funding_page_limit: usize,
    pub whale_page_limit: usize,
    pub instrument_max_pages: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SignalRouting {
    pub source: String,
    pub long_sleeve: String,
    pub carry_sleeve: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct MachineSignalConfig {
    schema_version: u32,
    kind: String,
    config_id: String,
    routing: SignalRouting,
    sources: SourceContract,
    live: LiveAcquisitionConfig,
    long: LongMachineRule,
    carry_profile_name: String,
    carry_feature_physics: CarryFeaturePhysics,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct LongMachineRule {
    profile_name: String,
    features: LongFeatureBody,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct LongFeatureBody {
    daily_min_hourly_bars: usize,
    cold_start_lookback_days: usize,
    pump_lookback_days: [usize; 2],
    atr_window_days: usize,
    atr_min_samples: usize,
    btc_rv_window_days: usize,
    btc_rv_min_samples: usize,
    btc_rv_null_value: f64,
    regime_missing_is_on: bool,
    median_fallback_to_daily_turnover: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct RegisteredLongArtifact {
    schema_version: u32,
    kind: String,
    profile_name: String,
    rule: RegisteredLongProfile,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct RegisteredLongProfile {
    execution_strategy_id: String,
    start_date: String,
    end_date: String,
    universe_size: usize,
    universe_volume_window_days: usize,
    min_listing_history_days: usize,
    exclude_symbols: Vec<String>,
    regime_symbol: String,
    regime_sma_days: usize,
    fc_min_day_return: f64,
    fc_top_volume_rank_max: f64,
    fc_min_close_location: f64,
    fc_max_hold_days: i64,
    fc_max_atr_pct: f64,
    fc_atr_stop_mult: f64,
    fc_sigma_mult: f64,
    fc_sniper_retrace_pct: f64,
    fc_sniper_deadline_hours: i64,
    weekend_size_mult: f64,
    fc_close_loc_multi_day: f64,
    fc_stop_time_decay_hours: i64,
    fc_stop_time_decay_atr_mult: f64,
    max_concurrent_positions: usize,
    cooldown_days: i64,
    entry_delay_hours: i64,
    gross_exposure: f64,
    vol_estimate_window_days: usize,
    vol_floor_annual: f64,
    max_position_weight: f64,
    vol_target_annual: f64,
    vol_target_min_scale: f64,
    vol_target_max_scale: f64,
    cost_multiplier: f64,
}

impl RegisteredLongProfile {
    fn native_rule(&self) -> engine_strategies::native_long::plan::RuleConfig {
        engine_strategies::native_long::plan::RuleConfig {
            execution_strategy_id: self.execution_strategy_id.clone(),
            entry_delay_hours: self.entry_delay_hours,
            fc_min_day_return: self.fc_min_day_return,
            fc_top_volume_rank_max: self.fc_top_volume_rank_max,
            fc_min_close_location: self.fc_min_close_location,
            fc_max_hold_days: self.fc_max_hold_days,
            fc_max_atr_pct: self.fc_max_atr_pct,
            fc_atr_stop_mult: self.fc_atr_stop_mult,
            fc_sigma_mult: self.fc_sigma_mult,
            fc_sniper_retrace_pct: self.fc_sniper_retrace_pct,
            fc_sniper_deadline_hours: self.fc_sniper_deadline_hours,
            weekend_size_mult: self.weekend_size_mult,
            fc_close_loc_multi_day: self.fc_close_loc_multi_day,
            fc_stop_time_decay_hours: self.fc_stop_time_decay_hours,
            fc_stop_time_decay_atr_mult: self.fc_stop_time_decay_atr_mult,
            max_concurrent_positions: self.max_concurrent_positions,
            cooldown_days: self.cooldown_days,
            gross_exposure: self.gross_exposure,
            vol_floor_annual: self.vol_floor_annual,
            max_position_weight: self.max_position_weight,
            vol_target_annual: self.vol_target_annual,
            vol_target_min_scale: self.vol_target_min_scale,
            vol_target_max_scale: self.vol_target_max_scale,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct CarryFeaturePhysics {
    momentum_lookback_hours: i64,
    adv_window_hours: i64,
    return_lookback_hours: i64,
    vol_window_hours: i64,
    vol_return_lag_hours: i64,
    vol_required_finite_samples: usize,
    trail_window_hours: i64,
    trail_change_lookback_hours: i64,
    turn_growth_lookback_hours: i64,
    whale_change_lookback_hours: i64,
    whale_freshness_hours: i64,
    whale_feed_days: usize,
    settlement_age_reset_threshold_hours: f64,
    decision_phase_ms: i64,
    decision_kline_lag_ms: i64,
    minimum_replay_days: usize,
    minimum_decision_symbols: usize,
    minimum_funding_coverage: f64,
    standing_funding_max_age_hours: f64,
    presettlement_window_ms: i64,
    missing_conditioning: String,
    missing_depth: String,
    stale_whale: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SignalWorkerConfig {
    pub long: LongFeatureConfig,
    pub carry: CarryFeatureConfig,
    pub routing: SignalRouting,
    pub sources: SourceContract,
    pub live: LiveAcquisitionConfig,
    pub identity: ConfigIdentity,
    pub long_destination: u16,
    pub carry_destination: u16,
    pub signal_path: PathBuf,
    pub long_rule_path: PathBuf,
    pub carry_path: PathBuf,
    pub operational_path: PathBuf,
    pub engine_path: PathBuf,
}

impl SignalWorkerConfig {
    pub fn load(
        signal_path: impl AsRef<Path>,
        long_rule_path: impl AsRef<Path>,
        carry_path: impl AsRef<Path>,
        operational_path: impl AsRef<Path>,
        engine_path: impl AsRef<Path>,
    ) -> Result<Self, WorkerError> {
        let signal_path = signal_path.as_ref().to_path_buf();
        let long_rule_path = long_rule_path.as_ref().to_path_buf();
        let carry_path = carry_path.as_ref().to_path_buf();
        let operational_path = operational_path.as_ref().to_path_buf();
        let engine_path = engine_path.as_ref().to_path_buf();
        let signal_bytes =
            fs::read(&signal_path).map_err(|e| WorkerError::io("read signal config", e))?;
        let long_rule_bytes =
            fs::read(&long_rule_path).map_err(|e| WorkerError::io("read LONG rule config", e))?;
        let carry_bytes =
            fs::read(&carry_path).map_err(|e| WorkerError::io("read CARRY config", e))?;
        let operational_bytes = fs::read(&operational_path)
            .map_err(|e| WorkerError::io("read operational config", e))?;
        let engine_bytes =
            fs::read(&engine_path).map_err(|e| WorkerError::io("read engine config", e))?;
        let machine: MachineSignalConfig = serde_json::from_slice(&signal_bytes)
            .map_err(|e| WorkerError::json("parse signal config", e))?;
        let long_rule: RegisteredLongArtifact = serde_json::from_slice(&long_rule_bytes)
            .map_err(|e| WorkerError::json("parse LONG rule config", e))?;
        if long_rule.schema_version != SCHEMA_VERSION
            || long_rule.kind != "liquidity_migration_long_native_rule"
            || long_rule.profile_name != machine.long.profile_name
        {
            return Err(WorkerError::config(
                "LONG rule artifact identity disagrees with selected profile",
            ));
        }
        let long_profile = &long_rule.rule;
        validate_registered_long(long_profile)?;
        let carry_value: Value = serde_json::from_slice(&carry_bytes)
            .map_err(|e| WorkerError::json("parse CARRY config", e))?;
        let operational_value: Value = serde_json::from_slice(&operational_bytes)
            .map_err(|e| WorkerError::json("parse operational config", e))?;
        validate_operational(&operational_value)?;
        let engine_text = std::str::from_utf8(&engine_bytes)
            .map_err(|_| WorkerError::config("engine config is not UTF-8"))?;
        let engine_value: toml::Value = toml::from_str(engine_text)
            .map_err(|e| WorkerError::config(format!("parse engine config: {e}")))?;
        let (long_destination, long_params, carry_destination, carry_params) =
            resolve_destinations(
                &engine_value,
                &machine.routing.long_sleeve,
                &machine.routing.carry_sleeve,
            )?;
        let long_native = engine_strategies::native_long::plug::config_from_params(&long_params)
            .map_err(|error| WorkerError::config(format!("native LONG params: {error}")))?;
        let carry_native = engine_strategies::native_carry::plug::config_from_params(&carry_params)
            .map_err(|error| WorkerError::config(format!("native CARRY params: {error}")))?;

        let body = &machine.long.features;
        let long = LongFeatureConfig {
            profile_name: machine.long.profile_name.clone(),
            execution_strategy_id: long_profile.execution_strategy_id.clone(),
            exclude_symbols: long_profile.exclude_symbols.clone(),
            universe_size: long_profile.universe_size,
            universe_volume_window_days: long_profile.universe_volume_window_days,
            min_listing_history_days: long_profile.min_listing_history_days,
            regime_symbol: long_profile.regime_symbol.clone(),
            regime_sma_days: long_profile.regime_sma_days,
            vol_estimate_window_days: long_profile.vol_estimate_window_days,
            daily_min_hourly_bars: body.daily_min_hourly_bars,
            cold_start_lookback_days: body.cold_start_lookback_days,
            pump_lookback_days: body.pump_lookback_days,
            atr_window_days: body.atr_window_days,
            atr_min_samples: body.atr_min_samples,
            btc_rv_window_days: body.btc_rv_window_days,
            btc_rv_min_samples: body.btc_rv_min_samples,
            btc_rv_null_value: body.btc_rv_null_value,
            regime_missing_is_on: body.regime_missing_is_on,
            median_fallback_to_daily_turnover: body.median_fallback_to_daily_turnover,
        };
        let (carry, carry_rule) = parse_carry(&carry_value, &machine.carry_feature_physics)?;
        let long_rule_sha256 = sha256_hex(&long_rule_bytes);
        let long_feature_contract_sha256 =
            engine_strategies::native_common::signal_feature_contract_sha256(&signal_bytes, "long")
                .map_err(|error| WorkerError::config(format!("LONG feature identity: {error}")))?;
        let carry_feature_contract_sha256 =
            engine_strategies::native_common::signal_feature_contract_sha256(
                &signal_bytes,
                "carry",
            )
            .map_err(|error| WorkerError::config(format!("CARRY feature identity: {error}")))?;
        if long_native.profile_name != machine.long.profile_name
            || long_native.environment != machine.live.environment
            || long_native.rule_sha256 != long_rule_sha256
            || long_native.feature_contract_sha256 != long_feature_contract_sha256
            || long_native.rule != long_profile.native_rule()
        {
            return Err(WorkerError::config(
                "signal feature profile disagrees with native LONG engine params",
            ));
        }
        if carry_native.profile_name != machine.carry_profile_name
            || carry_native.environment != machine.live.environment
            || carry_native.rule != carry_rule
            || carry_native.rule_sha256 != sha256_hex(&carry_bytes)
            || carry_native.feature_contract_sha256 != carry_feature_contract_sha256
        {
            return Err(WorkerError::config(
                "registered CARRY JSON disagrees with native CARRY engine params",
            ));
        }
        let operational_sha256 = sha256_hex(&operational_bytes);
        if long_native.operational_profile_sha256 != operational_sha256
            || carry_native.operational_profile_sha256 != operational_sha256
            || carry_native.execution.presettlement_window_ms != carry.presettlement_window_ms
        {
            return Err(WorkerError::config(
                "operational profile or CARRY public clock disagrees with native engine params",
            ));
        }
        let identity = ConfigIdentity {
            schema_version: SCHEMA_VERSION,
            signal_config_id: machine.config_id.clone(),
            long_profile: long.profile_name.clone(),
            long_execution_strategy_id: long.execution_strategy_id.clone(),
            signal_config_sha256: sha256_hex(&signal_bytes),
            long_rule_sha256,
            long_feature_contract_sha256,
            carry_config_id: carry.config_id.clone(),
            carry_rule_sha256: sha256_hex(&carry_bytes),
            carry_feature_contract_sha256,
            operational_profile_sha256: sha256_hex(&operational_bytes),
            engine_config_sha256: sha256_hex(&engine_bytes),
            long_decision_fingerprint:
                engine_strategies::native_long::plug::decision_fingerprint_from_params(&long_params)
                    .map_err(|error| {
                        WorkerError::config(format!("native LONG identity: {error}"))
                    })?,
            carry_decision_fingerprint:
                engine_strategies::native_carry::plug::decision_fingerprint_from_params(
                    &carry_params,
                )
                .map_err(|error| WorkerError::config(format!("native CARRY identity: {error}")))?,
        };
        validate_config(&machine, &long, &carry, &identity)?;
        Ok(Self {
            long,
            carry,
            routing: machine.routing,
            sources: machine.sources,
            live: machine.live,
            identity,
            long_destination,
            carry_destination,
            signal_path,
            long_rule_path,
            carry_path,
            operational_path,
            engine_path,
        })
    }
}

fn resolve_destinations(
    engine: &toml::Value,
    long_sleeve: &str,
    carry_sleeve: &str,
) -> Result<(u16, toml::Value, u16, toml::Value), WorkerError> {
    let strategies = engine
        .get("strategy")
        .and_then(toml::Value::as_array)
        .ok_or_else(|| WorkerError::config("engine config lacks [[strategy]]"))?;
    let find = |sleeve: &str| -> Result<(u16, toml::Value), WorkerError> {
        let matches: Vec<usize> = strategies
            .iter()
            .enumerate()
            .filter_map(|(index, row)| {
                (row.get("sleeve").and_then(toml::Value::as_str) == Some(sleeve)).then_some(index)
            })
            .collect();
        if matches.len() != 1 {
            return Err(WorkerError::config(format!(
                "engine config must contain exactly one {sleeve} sleeve"
            )));
        }
        let index = matches[0];
        let destination = u16::try_from(index)
            .map_err(|_| WorkerError::config("engine strategy index exceeds u16"))?;
        let mut params = strategies[index]
            .as_table()
            .cloned()
            .ok_or_else(|| WorkerError::config("engine strategy entry is not a table"))?;
        for field in ["name", "sleeve"] {
            params.remove(field);
        }
        Ok((destination, toml::Value::Table(params)))
    };
    let long = find(long_sleeve)?;
    let carry = find(carry_sleeve)?;
    if long.0 == carry.0 {
        return Err(WorkerError::config(
            "LONG and CARRY resolve to one engine slot",
        ));
    }
    Ok((long.0, long.1, carry.0, carry.1))
}

fn parse_carry(
    value: &Value,
    physics: &CarryFeaturePhysics,
) -> Result<
    (
        CarryFeatureConfig,
        engine_strategies::native_carry::scorer::CarryRuleConfig,
    ),
    WorkerError,
> {
    let config_id = string_at(value, &["config_id"])?;
    let rule = object_at(value, "rule")?;
    let universe = object_at(rule, "universe")?;
    if string_at(universe, &["venue"])? != "bybit"
        || string_at(universe, &["rank_by"])? != "trailing_24h_quote_turnover"
    {
        return Err(WorkerError::config("unsupported CARRY universe contract"));
    }
    let state = object_at(rule, "state")?;
    let sizing = object_at(rule, "sizing")?;
    let persistence = sizing.get("persistence_scaling");
    if persistence.is_some_and(|row| {
        string_at(row, &["basis"]).ok().as_deref() != Some("deep_settlement_share")
    }) {
        return Err(WorkerError::config("unsupported CARRY persistence basis"));
    }
    let whale = sizing.get("whale_scaling");
    if whale.is_some_and(|row| {
        string_at(row, &["basis"]).ok().as_deref() != Some("binance_toptrader_ls_3d_change")
    }) {
        return Err(WorkerError::config("unsupported CARRY whale basis"));
    }
    let filters = object_at(rule, "filters")?;
    let toxic = object_at(filters, "toxic_band_ret_3d")?;
    let risk = object_at(rule, "risk")?;
    let depth = sizing
        .get("depth_scaling")
        .filter(|value| value.is_object())
        .ok_or_else(|| WorkerError::config("CARRY config lacks depth_scaling"))?;
    let persistence = persistence
        .filter(|value| value.is_object())
        .ok_or_else(|| WorkerError::config("CARRY config lacks persistence_scaling"))?;
    let flow = sizing
        .get("flow_scaling")
        .filter(|value| value.is_object())
        .ok_or_else(|| WorkerError::config("CARRY config lacks flow_scaling"))?;
    let whale = whale
        .filter(|value| value.is_object())
        .ok_or_else(|| WorkerError::config("CARRY config lacks whale_scaling"))?;
    let carry = CarryFeatureConfig {
        config_id,
        universe_top_n: usize_at(universe, "top_n")?,
        enter_bp: f64_at(state, "enter_below_funding_bp")?,
        persistence_window_settlements: Some(usize_at(persistence, "window_settlements")?),
        momentum_lookback_hours: physics.momentum_lookback_hours,
        adv_window_hours: physics.adv_window_hours,
        return_lookback_hours: physics.return_lookback_hours,
        vol_window_hours: physics.vol_window_hours,
        vol_return_lag_hours: physics.vol_return_lag_hours,
        vol_required_finite_samples: physics.vol_required_finite_samples,
        trail_window_hours: physics.trail_window_hours,
        trail_change_lookback_hours: physics.trail_change_lookback_hours,
        turn_growth_lookback_hours: physics.turn_growth_lookback_hours,
        whale_change_lookback_hours: physics.whale_change_lookback_hours,
        whale_freshness_hours: physics.whale_freshness_hours,
        whale_feed_days: physics.whale_feed_days,
        settlement_age_reset_threshold_hours: physics.settlement_age_reset_threshold_hours,
        decision_phase_ms: physics.decision_phase_ms,
        decision_kline_lag_ms: physics.decision_kline_lag_ms,
        minimum_replay_days: physics.minimum_replay_days,
        minimum_decision_symbols: physics.minimum_decision_symbols,
        minimum_funding_coverage: physics.minimum_funding_coverage,
        standing_funding_max_age_hours: physics.standing_funding_max_age_hours,
        presettlement_window_ms: physics.presettlement_window_ms,
        missing_conditioning: physics.missing_conditioning.clone(),
        missing_depth: physics.missing_depth.clone(),
        stale_whale: physics.stale_whale.clone(),
    };
    let native = engine_strategies::native_carry::scorer::CarryRuleConfig {
        config_id: carry.config_id.clone(),
        universe_top_n: carry.universe_top_n,
        enter_bp: carry.enter_bp,
        exit_bp: f64_at(state, "exit_above_funding_bp")?,
        per_name_cap: f64_at(sizing, "per_name_cap")?,
        gross_cap: f64_at(sizing, "gross_cap")?,
        depth_ref_bp_per_day: f64_at(depth, "ref_bp_per_day")?,
        depth_floor: f64_at(depth, "floor")?,
        depth_exponent: f64_at(depth, "exponent")?,
        toxic_band_ret3d_lo: f64_at(toxic, "lo")?,
        toxic_band_ret3d_hi: f64_at(toxic, "hi")?,
        min_vol30_daily: f64_at(filters, "min_vol_30d_daily")?,
        trail_recovery_exit_bp_2d: f64_at(state, "exit_on_trail_recovery_bp_2d")?,
        persistence_cut: f64_at(persistence, "cut")?,
        persistence_lo: f64_at(persistence, "low_multiplier")?,
        flow_cut: f64_at(flow, "cut")?,
        flow_lo: f64_at(flow, "low_multiplier")?,
        whale_cut: f64_at(whale, "cut")?,
        whale_lo: f64_at(whale, "low_multiplier")?,
    };
    native.validate().map_err(WorkerError::config)?;
    if f64_at(risk, "vol_lookback_days")? != 30.0 {
        return Err(WorkerError::config(
            "CARRY risk vol lookback disagrees with feature physics",
        ));
    }
    Ok((carry, native))
}

fn validate_operational(value: &Value) -> Result<(), WorkerError> {
    if value.get("kind").and_then(Value::as_str) != Some("liquidity_migration_operational_profile")
        || value.get("schema_version").and_then(Value::as_u64) != Some(3)
    {
        return Err(WorkerError::config("unsupported operational profile"));
    }
    Ok(())
}

fn validate_registered_long(profile: &RegisteredLongProfile) -> Result<(), WorkerError> {
    let excluded: BTreeSet<&str> = profile.exclude_symbols.iter().map(String::as_str).collect();
    if profile.execution_strategy_id.trim().is_empty()
        || !profile.start_date.is_empty()
        || !profile.end_date.is_empty()
        || profile.universe_size == 0
        || profile.universe_volume_window_days < 2
        || profile.min_listing_history_days == 0
        || profile.regime_symbol.trim().is_empty()
        || profile.regime_sma_days < 2
        || profile.vol_estimate_window_days < 2
        || profile.exclude_symbols.len() != excluded.len()
        || profile.exclude_symbols.iter().any(|symbol| {
            symbol.is_empty()
                || !symbol
                    .bytes()
                    .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
        })
        || !profile.cost_multiplier.is_finite()
        || profile.cost_multiplier <= 0.0
    {
        return Err(WorkerError::config(
            "selected LONG registered profile is invalid for live use",
        ));
    }
    Ok(())
}

fn validate_config(
    machine: &MachineSignalConfig,
    long: &LongFeatureConfig,
    carry: &CarryFeatureConfig,
    identity: &ConfigIdentity,
) -> Result<(), WorkerError> {
    if machine.schema_version != SCHEMA_VERSION
        || machine.kind != "liquidity_migration_signal_worker"
        || machine.config_id.trim().is_empty()
    {
        return Err(WorkerError::config("unsupported signal-worker config"));
    }
    if long.profile_name.trim().is_empty()
        || long.execution_strategy_id.trim().is_empty()
        || long.universe_size == 0
        || long.universe_volume_window_days < 2
        || long.min_listing_history_days == 0
        || long.regime_sma_days < 2
        || long.vol_estimate_window_days < 2
        || !(1..=24).contains(&long.daily_min_hourly_bars)
        || long.cold_start_lookback_days < long.universe_volume_window_days
        || long.pump_lookback_days[0] == 0
        || long.pump_lookback_days[0] >= long.pump_lookback_days[1]
        || long.atr_min_samples == 0
        || long.atr_min_samples > long.atr_window_days
        || long.btc_rv_min_samples == 0
        || long.btc_rv_min_samples > long.btc_rv_window_days
        || !long.btc_rv_null_value.is_finite()
        || long.regime_missing_is_on
        || !long.median_fallback_to_daily_turnover
    {
        return Err(WorkerError::config("LONG feature contract is invalid"));
    }
    if carry.universe_top_n == 0
        || !carry.enter_bp.is_finite()
        || carry.enter_bp <= 0.0
        || carry.persistence_window_settlements == Some(0)
        || carry.momentum_lookback_hours <= 0
        || carry.adv_window_hours <= 0
        || carry.return_lookback_hours <= 0
        || carry.vol_window_hours <= 0
        || carry.vol_return_lag_hours <= 0
        || carry.vol_required_finite_samples == 0
        || carry.trail_window_hours <= 0
        || carry.trail_change_lookback_hours <= 0
        || carry.turn_growth_lookback_hours <= 0
        || carry.whale_change_lookback_hours <= 0
        || carry.whale_freshness_hours <= 0
        || carry.whale_feed_days == 0
        || carry.whale_feed_days > MAX_WHALE_FEED_DAYS
        || !carry.settlement_age_reset_threshold_hours.is_finite()
        || carry.settlement_age_reset_threshold_hours <= 0.0
        || carry.decision_phase_ms < 0
        || carry.decision_kline_lag_ms < 0
        || carry.minimum_replay_days == 0
        || carry.minimum_decision_symbols == 0
        || !(0.0..=1.0).contains(&carry.minimum_funding_coverage)
        || !carry.standing_funding_max_age_hours.is_finite()
        || carry.standing_funding_max_age_hours <= 0.0
        || carry.presettlement_window_ms <= 0
        || carry.missing_conditioning != "fail_open"
        || carry.missing_depth != "floor"
        || carry.stale_whale != "null_fail_open"
    {
        return Err(WorkerError::config("CARRY feature contract is invalid"));
    }
    validate_source_history_bounds(long, carry)?;
    let source = &machine.sources;
    if source.bybit_category != "linear"
        || source.bybit_settle_coin != "USDT"
        || source.bybit_mainnet_host != "api.bybit.com"
        || source.bybit_demo_host != "api-demo.bybit.com"
        || source.binance_host != "fapi.binance.com"
        || source.kline_interval_minutes != 60
        || source.funding_event_kind != "settlement"
        || source.whale_source != "binance_toptrader_position_long_short_ratio"
        || source.whale_period != "5m_eod"
        || source.mark_max_age_ms <= 0
        || !source.universe_identity_required
        || machine.routing.source.trim().is_empty()
        || machine.routing.long_sleeve == machine.routing.carry_sleeve
    {
        return Err(WorkerError::config(
            "signal source or routing contract is invalid",
        ));
    }
    let live = &machine.live;
    if !matches!(live.environment.as_str(), "demo" | "mainnet")
        || live.public_market_realm != "mainnet"
        || live.request_timeout_ms == 0
        || live.request_retries == 0
        || live.retry_base_ms == 0
        || live.ticker_cadence_ms == 0
        || live.instrument_cadence_ms == 0
        || live.funding_cadence_ms == 0
        || live.kline_cadence_ms == 0
        || live.whale_cadence_ms == 0
        || !(1..=4).contains(&live.max_parallel_requests)
        || !(1..=1000).contains(&live.kline_page_limit)
        || !(1..=200).contains(&live.funding_page_limit)
        || !(1..=500).contains(&live.whale_page_limit)
        || !(1..=50).contains(&live.instrument_max_pages)
    {
        return Err(WorkerError::config("live acquisition contract is invalid"));
    }
    for digest in [
        &identity.signal_config_sha256,
        &identity.long_rule_sha256,
        &identity.long_feature_contract_sha256,
        &identity.carry_rule_sha256,
        &identity.carry_feature_contract_sha256,
        &identity.operational_profile_sha256,
        &identity.engine_config_sha256,
        &identity.long_decision_fingerprint,
        &identity.carry_decision_fingerprint,
    ] {
        if !is_sha256(digest) {
            return Err(WorkerError::config(
                "config fingerprint is not lowercase sha256",
            ));
        }
    }
    Ok(())
}

fn validate_source_history_bounds(
    long: &LongFeatureConfig,
    carry: &CarryFeatureConfig,
) -> Result<(), WorkerError> {
    if long.cold_start_lookback_days > MAX_LONG_COLD_START_LOOKBACK_DAYS {
        return Err(WorkerError::config(format!(
            "LONG cold-start lookback exceeds {MAX_LONG_COLD_START_LOOKBACK_DAYS} days"
        )));
    }
    let source_hours = carry_source_history_hours(carry, true);
    if source_hours.is_none_or(|hours| hours > MAX_CARRY_SOURCE_HISTORY_HOURS) {
        return Err(WorkerError::config(format!(
            "CARRY replay and feature history exceeds {MAX_CARRY_SOURCE_HISTORY_HOURS} hours"
        )));
    }
    Ok(())
}

fn object_at<'a>(value: &'a Value, key: &str) -> Result<&'a Value, WorkerError> {
    value
        .get(key)
        .filter(|row| row.is_object())
        .ok_or_else(|| WorkerError::config(format!("config lacks object {key}")))
}

fn string_at(value: &Value, path: &[&str]) -> Result<String, WorkerError> {
    let mut current = value;
    for key in path {
        current = current
            .get(*key)
            .ok_or_else(|| WorkerError::config(format!("config lacks {}", path.join("."))))?;
    }
    let text = current
        .as_str()
        .ok_or_else(|| WorkerError::config(format!("config {} is not a string", path.join("."))))?;
    if text.trim().is_empty() {
        return Err(WorkerError::config(format!(
            "config {} is empty",
            path.join(".")
        )));
    }
    Ok(text.to_owned())
}

fn usize_at(value: &Value, key: &str) -> Result<usize, WorkerError> {
    let raw = value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| WorkerError::config(format!("config {key} is not an unsigned integer")))?;
    usize::try_from(raw).map_err(|_| WorkerError::config(format!("config {key} is too large")))
}

fn f64_at(value: &Value, key: &str) -> Result<f64, WorkerError> {
    let out = value
        .get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| WorkerError::config(format!("config {key} is not numeric")))?;
    if !out.is_finite() {
        return Err(WorkerError::config(format!("config {key} is non-finite")));
    }
    Ok(out)
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

pub fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::{
        carry_source_history_hours, sha256_hex, validate_source_history_bounds, SignalWorkerConfig,
        MAX_CARRY_SOURCE_HISTORY_HOURS, MAX_LONG_COLD_START_LOOKBACK_DAYS,
        SOURCE_HISTORY_PADDING_HOURS,
    };
    use engine_strategies::native_carry::plan::{
        ExecutionRules as CarryExecutionRules, StrategyConfig as CarryStrategyConfig,
    };
    use engine_strategies::native_carry::scorer::CarryRuleConfig;
    use engine_strategies::native_long::plan::{
        RuleConfig as LongRuleConfig, StrategyConfig as LongStrategyConfig,
    };

    #[test]
    fn checked_in_realm_configs_derive_native_fingerprints() {
        for realm in ["demo", "mainnet"] {
            validate_realm(realm);
        }
    }

    #[test]
    fn source_history_caps_accept_the_boundary_and_reject_the_next_unit() {
        let config = checked_realm_config("demo");
        let mut long = config.long;
        let mut carry = config.carry;

        long.cold_start_lookback_days = MAX_LONG_COLD_START_LOOKBACK_DAYS;
        carry.minimum_replay_days = 149;
        assert_eq!(
            carry_source_history_hours(&carry, true),
            Some(MAX_CARRY_SOURCE_HISTORY_HOURS)
        );
        validate_source_history_bounds(&long, &carry).unwrap();

        long.cold_start_lookback_days = MAX_LONG_COLD_START_LOOKBACK_DAYS + 1;
        assert!(validate_source_history_bounds(&long, &carry)
            .unwrap_err()
            .to_string()
            .contains("LONG cold-start lookback"));
        long.cold_start_lookback_days = MAX_LONG_COLD_START_LOOKBACK_DAYS;

        carry.vol_window_hours += 1;
        assert!(validate_source_history_bounds(&long, &carry)
            .unwrap_err()
            .to_string()
            .contains("CARRY replay and feature history"));
        carry.vol_window_hours -= 1;

        let feature_boundary = MAX_CARRY_SOURCE_HISTORY_HOURS - 24 - SOURCE_HISTORY_PADDING_HOURS;
        for arm in ["momentum", "return", "volatility", "trail", "turnover"] {
            let mut candidate = carry.clone();
            candidate.minimum_replay_days = 1;
            candidate.momentum_lookback_hours = 1;
            candidate.return_lookback_hours = 1;
            candidate.vol_window_hours = 1;
            candidate.vol_return_lag_hours = 1;
            candidate.trail_change_lookback_hours = 1;
            candidate.trail_window_hours = 1;
            candidate.turn_growth_lookback_hours = 1;
            candidate.adv_window_hours = 1;
            match arm {
                "momentum" => candidate.momentum_lookback_hours = feature_boundary,
                "return" => candidate.return_lookback_hours = feature_boundary,
                "volatility" => candidate.vol_window_hours = feature_boundary - 1,
                "trail" => candidate.trail_change_lookback_hours = feature_boundary - 1,
                "turnover" => candidate.turn_growth_lookback_hours = feature_boundary - 1,
                _ => unreachable!(),
            }
            assert_eq!(
                carry_source_history_hours(&candidate, true),
                Some(MAX_CARRY_SOURCE_HISTORY_HOURS),
                "{arm} boundary"
            );
            validate_source_history_bounds(&long, &candidate).unwrap();
            match arm {
                "momentum" => candidate.momentum_lookback_hours += 1,
                "return" => candidate.return_lookback_hours += 1,
                "volatility" => candidate.vol_window_hours += 1,
                "trail" => candidate.trail_change_lookback_hours += 1,
                "turnover" => candidate.turn_growth_lookback_hours += 1,
                _ => unreachable!(),
            }
            assert!(
                validate_source_history_bounds(&long, &candidate).is_err(),
                "{arm} boundary plus one"
            );
        }

        carry.minimum_replay_days = usize::MAX;
        assert!(validate_source_history_bounds(&long, &carry).is_err());
    }

    fn checked_realm_config(realm: &str) -> SignalWorkerConfig {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .unwrap();
        SignalWorkerConfig::load(
            root.join(format!("configs/signal-worker.{realm}.json")),
            root.join("configs/long_native_v12.json"),
            root.join("configs/lane2_carry_hold_v7.json"),
            root.join(format!("configs/operational.{realm}.json")),
            root.join(format!("deploy/engine.{realm}.toml.template")),
        )
        .unwrap()
    }

    fn validate_realm(realm: &str) {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .unwrap();
        let operational = root.join(format!("configs/operational.{realm}.json"));
        let operational_bytes = std::fs::read(&operational).unwrap();
        let operational_sha = sha256_hex(&operational_bytes);
        let carry_path = root.join("configs/lane2_carry_hold_v7.json");
        let carry_sha = sha256_hex(&std::fs::read(&carry_path).unwrap());
        let long_rule_path = root.join("configs/long_native_v12.json");
        let long_rule_sha = sha256_hex(&std::fs::read(&long_rule_path).unwrap());
        let signal_path = root.join(format!("configs/signal-worker.{realm}.json"));
        let signal_bytes = std::fs::read(&signal_path).unwrap();
        let long_feature_contract_sha =
            engine_strategies::native_common::signal_feature_contract_sha256(&signal_bytes, "long")
                .unwrap();
        let carry_feature_contract_sha =
            engine_strategies::native_common::signal_feature_contract_sha256(
                &signal_bytes,
                "carry",
            )
            .unwrap();
        let long = LongStrategyConfig {
            schema_version: 1,
            profile_name: "v12".into(),
            environment: realm.into(),
            rule_sha256: long_rule_sha,
            feature_contract_sha256: long_feature_contract_sha,
            operational_profile_sha256: operational_sha.clone(),
            entries_enabled: false,
            rule: LongRuleConfig {
                execution_strategy_id: "long_native_v12_wide_stop".into(),
                entry_delay_hours: 1,
                fc_min_day_return: 0.15,
                fc_top_volume_rank_max: 10.0,
                fc_min_close_location: 0.70,
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
                vol_floor_annual: 0.30,
                max_position_weight: 0.30,
                vol_target_annual: 0.60,
                vol_target_min_scale: 0.30,
                vol_target_max_scale: 1.25,
            },
            notional_multiplier: 6.0,
            entry_leverage: 5.0,
            order_notional_pct_equity: 0.0,
            wallet_balance_fraction: 1.0,
            max_new_entries_per_cycle: 5,
            signal_freshness_ms: 24 * 3_600_000,
            book_validity_ms: 3_600_000,
            entry_floor_usdt: 6.0,
            resize_floor_usdt: 1.0,
            resize_floor_fraction: 0.05,
            engine_entry_cutoff_ms: 900_000,
            rest_entries: false,
            hold_decision_price: false,
            give_up_instead_of_crossing: false,
        };
        let carry = CarryStrategyConfig {
            schema_version: 1,
            profile_name: "carry_hold_v7_live_v1".into(),
            environment: realm.into(),
            rule_sha256: carry_sha,
            feature_contract_sha256: carry_feature_contract_sha,
            operational_profile_sha256: operational_sha,
            entries_enabled: false,
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
                toxic_band_ret3d_lo: -0.30,
                toxic_band_ret3d_hi: 0.0,
                min_vol30_daily: 0.05,
                trail_recovery_exit_bp_2d: 30.0,
                persistence_cut: 0.10,
                persistence_lo: 0.0,
                flow_cut: 0.40,
                flow_lo: 0.5,
                whale_cut: -0.26,
                whale_lo: 0.5,
            },
            exit_bp: 3.0,
            early_exit_enabled: true,
            presettlement_exit_enabled: true,
            notional_multiplier: 3.0,
            entry_leverage: 5.0,
            stop_loss_fraction: 0.35,
            max_new_entries_per_cycle: 10,
            capital_reference_usdt: 0.0,
            rest_entries: true,
            hold_decision_price: false,
            give_up_instead_of_crossing: false,
            execution: CarryExecutionRules {
                entry_floor_usdt: 6.0,
                resize_floor_usdt: 1.0,
                resize_floor_fraction: 0.05,
                engine_entry_cutoff_ms: 900_000,
                signal_validity_ms: 6 * 3_600_000,
                book_validity_ms: 30 * 3_600_000,
                presettlement_window_ms: 900_000,
            },
        };
        long.validate().unwrap();
        carry.validate().unwrap();
        let engine = format!(
            "[[strategy]]\nname = \"carry_native\"\nsleeve = \"carry\"\nconfig_json = '''{}'''\n\n[[strategy]]\nname = \"long_native\"\nsleeve = \"long\"\nconfig_json = '''{}'''\n",
            serde_json::to_string(&carry).unwrap(),
            serde_json::to_string(&long).unwrap(),
        );
        let path = std::env::temp_dir().join(format!(
            "signal-worker-engine-{realm}-{}.toml",
            std::process::id()
        ));
        std::fs::write(&path, engine).unwrap();
        let config = SignalWorkerConfig::load(
            root.join(format!("configs/signal-worker.{realm}.json")),
            long_rule_path,
            carry_path,
            operational,
            &path,
        )
        .unwrap();
        assert_eq!(config.live.environment, realm);
        assert_eq!(config.live.public_market_realm, "mainnet");
        assert_eq!(config.carry_destination, 0);
        assert_eq!(config.long_destination, 1);
        assert_eq!(
            config.identity.long_decision_fingerprint,
            long.fingerprint()
        );
        assert_eq!(
            config.identity.carry_decision_fingerprint,
            carry.fingerprint()
        );
        std::fs::remove_file(path).unwrap();

        let template = root.join(format!("deploy/engine.{realm}.toml.template"));
        let exact = SignalWorkerConfig::load(
            root.join(format!("configs/signal-worker.{realm}.json")),
            root.join("configs/long_native_v12.json"),
            root.join("configs/lane2_carry_hold_v7.json"),
            root.join(format!("configs/operational.{realm}.json")),
            template,
        )
        .unwrap();
        assert_eq!(exact.carry_destination, 0);
        assert_eq!(exact.long_destination, 1);
        assert_eq!(
            exact.identity.long_feature_contract_sha256,
            long.feature_contract_sha256
        );
        assert_eq!(
            exact.identity.carry_feature_contract_sha256,
            carry.feature_contract_sha256
        );
    }
}
