//! Deterministic native-directional config rendering from machine authorities.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::native_carry::plan::{
    ExecutionRules as CarryExecutionRules, StrategyConfig as CarryStrategyConfig,
};
use crate::native_carry::scorer::CarryRuleConfig;
use crate::native_common::{
    hex_digest, signal_feature_contract_sha256, CARRY_SLEEVE_NAME, EXODUS_SLEEVE_NAME,
};
use crate::native_exodus::plan::{
    RuleConfig as ExodusRuleConfig, StrategyConfig as ExodusStrategyConfig,
};
use crate::native_long::plan::{
    RuleConfig as LongRuleConfig, StrategyConfig as LongStrategyConfig,
};

const HOUR_MS: i64 = 3_600_000;
pub const NATIVE_BLOCKS_BEGIN: &str = "# BEGIN GENERATED NATIVE DIRECTIONAL STRATEGIES";
pub const NATIVE_BLOCKS_END: &str = "# END GENERATED NATIVE DIRECTIONAL STRATEGIES";
pub const MAKER_RULE_BEGIN: &str =
    "# BEGIN GENERATED MAKER CANARY RULE -- engine render-native-config";
pub const MAKER_RULE_END: &str = "# END GENERATED MAKER CANARY RULE";

pub struct NativeConfigSources<'a> {
    pub realm: &'a str,
    pub signal_config: &'a [u8],
    pub long_rule: &'a [u8],
    pub carry_rule: &'a [u8],
    pub exodus_rule: &'a [u8],
    pub operational_config: &'a [u8],
    pub long_entries_enabled: bool,
    pub carry_entries_enabled: bool,
    pub exodus_entries_enabled: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct NativeConfigRender {
    pub schema_version: u16,
    pub realm: String,
    pub signal_config_sha256: String,
    pub long_rule_sha256: String,
    pub long_feature_contract_sha256: String,
    pub carry_rule_sha256: String,
    pub carry_feature_contract_sha256: String,
    pub exodus_rule_sha256: String,
    pub operational_config_sha256: String,
    pub long_decision_fingerprint: String,
    pub carry_decision_fingerprint: String,
    pub exodus_decision_fingerprint: String,
    pub long: LongStrategyConfig,
    pub carry: CarryStrategyConfig,
    pub exodus: ExodusStrategyConfig,
    pub toml_blocks: String,
}

pub fn render_native_config(
    sources: NativeConfigSources<'_>,
) -> Result<NativeConfigRender, String> {
    if !matches!(sources.realm, "demo" | "mainnet") {
        return Err("native config realm must be demo or mainnet".to_owned());
    }
    let signal = parse_json(sources.signal_config, "signal config")?;
    validate_signal(&signal, sources.realm)?;
    let long_file: RegisteredLongRuleFile = serde_json::from_slice(sources.long_rule)
        .map_err(|error| format!("LONG registered rule: {error}"))?;
    long_file.validate()?;
    let signal_long_profile = string_path(&signal, &["long", "profile_name"])?;
    if signal_long_profile != long_file.profile_name {
        return Err("signal config and LONG rule select different profiles".to_owned());
    }
    if let Ok(signal_strategy_id) = string_path(&signal, &["long", "execution_strategy_id"]) {
        if signal_strategy_id != long_file.rule.execution_strategy_id {
            return Err(
                "signal config and LONG rule select different strategy identities".to_owned(),
            );
        }
    }
    let carry_profile = string_path(&signal, &["carry_profile_name"])?;
    let presettlement_window_ms = i64_path(
        &signal,
        &["carry_feature_physics", "presettlement_window_ms"],
    )?;
    let operational = parse_json(sources.operational_config, "operational config")?;
    validate_operational(&operational)?;
    let operational_sha256 = hex_digest(sources.operational_config);
    let long_rule_sha256 = hex_digest(sources.long_rule);
    let carry_rule_sha256 = hex_digest(sources.carry_rule);
    let exodus_rule_sha256 = hex_digest(sources.exodus_rule);
    let long_feature_contract_sha256 = long_feature_contract_sha256(sources.signal_config)?;
    let carry_feature_contract_sha256 = carry_feature_contract_sha256(sources.signal_config)?;

    let long_operational = object_path(&operational, &["long"])?;
    let long = LongStrategyConfig {
        schema_version: 1,
        profile_name: long_file.profile_name.clone(),
        environment: sources.realm.to_owned(),
        rule_sha256: long_rule_sha256.clone(),
        feature_contract_sha256: long_feature_contract_sha256.clone(),
        operational_profile_sha256: operational_sha256.clone(),
        entries_enabled: sources.long_entries_enabled,
        rule: long_file.rule.to_native(),
        notional_multiplier: f64_field(long_operational, "notional_multiplier")?,
        entry_leverage: f64_field(long_operational, "entry_leverage")?,
        order_notional_pct_equity: f64_field(long_operational, "order_notional_pct_equity")?,
        wallet_balance_fraction: 1.0,
        max_new_entries_per_cycle: usize_field(long_operational, "max_new_entries_per_cycle")?,
        signal_freshness_ms: 24 * HOUR_MS,
        book_validity_ms: HOUR_MS,
        entry_floor_usdt: 6.0,
        resize_floor_usdt: 1.0,
        resize_floor_fraction: 0.05,
        engine_entry_cutoff_ms: 900_000,
        rest_entries: false,
        hold_decision_price: false,
        give_up_instead_of_crossing: false,
    };
    long.validate().map_err(str::to_owned)?;

    let carry_rule_value = parse_json(sources.carry_rule, "CARRY registered rule")?;
    let carry_rule = parse_carry_rule(&carry_rule_value)?;
    let carry_operational = object_path(&operational, &["carry"])?;
    let carry = CarryStrategyConfig {
        schema_version: 1,
        profile_name: carry_profile.to_owned(),
        environment: sources.realm.to_owned(),
        rule_sha256: carry_rule_sha256.clone(),
        feature_contract_sha256: carry_feature_contract_sha256.clone(),
        operational_profile_sha256: operational_sha256.clone(),
        entries_enabled: sources.carry_entries_enabled,
        exodus_sleeve_name: EXODUS_SLEEVE_NAME.to_owned(),
        exit_bp: carry_rule.exit_bp,
        rule: carry_rule,
        early_exit_enabled: true,
        presettlement_exit_enabled: true,
        notional_multiplier: f64_field(carry_operational, "notional_multiplier")?,
        entry_leverage: f64_field(carry_operational, "entry_leverage")?,
        stop_loss_fraction: f64_field(carry_operational, "declared_stop_loss_fraction")?,
        max_new_entries_per_cycle: usize_field(carry_operational, "max_new_entries_per_cycle")?,
        capital_reference_usdt: producer_capital_reference(&operational)?,
        rest_entries: true,
        hold_decision_price: false,
        give_up_instead_of_crossing: false,
        execution: CarryExecutionRules {
            entry_floor_usdt: 6.0,
            resize_floor_usdt: 1.0,
            resize_floor_fraction: 0.05,
            engine_entry_cutoff_ms: 900_000,
            signal_validity_ms: 6 * HOUR_MS,
            book_validity_ms: 30 * HOUR_MS,
            presettlement_window_ms,
        },
    };
    carry.validate().map_err(str::to_owned)?;

    let exodus_rule_value = parse_json(sources.exodus_rule, "Exodus registered rule")?;
    let exodus_rule = parse_exodus_rule(&exodus_rule_value)?;
    let exodus = ExodusStrategyConfig {
        schema_version: 1,
        profile_name: "v1".to_owned(),
        environment: sources.realm.to_owned(),
        rule_sha256: exodus_rule_sha256.clone(),
        operational_profile_sha256: operational_sha256.clone(),
        entries_enabled: sources.exodus_entries_enabled,
        carry_sleeve_name: CARRY_SLEEVE_NAME.to_owned(),
        entry_leverage: f64_field(carry_operational, "entry_leverage")?,
        rest_entries: false,
        hold_decision_price: false,
        give_up_instead_of_crossing: false,
        rule: exodus_rule,
    };
    exodus.validate().map_err(str::to_owned)?;

    let toml_blocks = render_blocks(&carry, &long, &exodus)?;
    Ok(NativeConfigRender {
        schema_version: 1,
        realm: sources.realm.to_owned(),
        signal_config_sha256: hex_digest(sources.signal_config),
        long_rule_sha256,
        long_feature_contract_sha256,
        carry_rule_sha256,
        carry_feature_contract_sha256,
        exodus_rule_sha256,
        operational_config_sha256: operational_sha256,
        long_decision_fingerprint: long.fingerprint(),
        carry_decision_fingerprint: carry.fingerprint(),
        exodus_decision_fingerprint: exodus.fingerprint(),
        long,
        carry,
        exodus,
        toml_blocks,
    })
}

/// Hash only the stable source and LONG feature physics, excluding polling,
/// routing, realm, and operator cadence.
pub fn long_feature_contract_sha256(signal_config: &[u8]) -> Result<String, String> {
    signal_feature_contract_sha256(signal_config, "long")
}

/// Hash only the stable source and CARRY feature physics, excluding polling,
/// routing, realm, and operator cadence.
pub fn carry_feature_contract_sha256(signal_config: &[u8]) -> Result<String, String> {
    signal_feature_contract_sha256(signal_config, "carry")
}

pub fn insert_native_blocks(template: &str, blocks: &str) -> Result<String, String> {
    let begin = template
        .find(NATIVE_BLOCKS_BEGIN)
        .ok_or("engine template is missing native-strategy begin marker")?;
    let after_begin = begin + NATIVE_BLOCKS_BEGIN.len();
    let end = after_begin
        + template[after_begin..]
            .find(NATIVE_BLOCKS_END)
            .ok_or("engine template is missing native-strategy end marker")?;
    if template[end + NATIVE_BLOCKS_END.len()..].contains(NATIVE_BLOCKS_END)
        || template[after_begin..end].contains(NATIVE_BLOCKS_BEGIN)
    {
        return Err("engine template has duplicate native-strategy markers".to_owned());
    }
    let mut out = String::with_capacity(template.len() + blocks.len());
    out.push_str(&template[..after_begin]);
    out.push('\n');
    out.push_str(blocks.trim_end());
    out.push('\n');
    out.push_str(&template[end..]);
    Ok(out)
}

pub fn render_maker_rule(registered_json: &[u8]) -> Result<String, String> {
    let value = parse_json(registered_json, "maker registered rule")?;
    exact_object_keys(
        &value,
        &[
            "authorizes",
            "claim",
            "config_id",
            "known_weaknesses",
            "lane",
            "measured_at_registration",
            "registered",
            "rule",
            "scoring_recipe",
            "surface",
        ],
        "maker registered rule",
    )?;
    if string_path(&value, &["config_id"])? != "lane2_toxic_flow_quoter_v1" {
        return Err("unsupported maker registered rule identity".to_owned());
    }
    let rule = object_path(&value, &["rule"])?;
    exact_map_keys(
        rule,
        &[
            "book_lean_bps",
            "flow",
            "half_spread_bps",
            "maker_fee_bps",
            "max_position_usdt",
            "min_edge_bps",
            "queue_reprice_edge_bps",
            "quote_notional_usdt",
            "requote_bps",
            "signal_half_life_ms",
            "skew_bps",
            "stop_loss_fraction",
            "symbol",
            "volatility_multiplier",
        ],
        "maker rule",
    )?;
    let flow = object_path(&value, &["rule", "flow"])?;
    exact_map_keys(
        flow,
        &[
            "fast_half_life_ms",
            "fast_weight",
            "max_score",
            "max_widen_bps",
            "near_depth_bps",
            "pull_score",
            "response_bps",
            "slow_half_life_ms",
            "slow_weight",
            "volatility_depth_multiplier",
        ],
        "maker flow rule",
    )?;
    let symbol = string_field(rule, "symbol")?;
    if !crate::native_common::valid_symbol(symbol) {
        return Err("maker rule symbol is invalid".to_owned());
    }
    let fields = [
        ("qty_usdt", f64_field(rule, "quote_notional_usdt")?),
        ("max_position_usdt", f64_field(rule, "max_position_usdt")?),
        ("half_spread_bps", f64_field(rule, "half_spread_bps")?),
        ("requote_bps", f64_field(rule, "requote_bps")?),
        ("skew_bps", f64_field(rule, "skew_bps")?),
        ("stop_loss_fraction", f64_field(rule, "stop_loss_fraction")?),
        ("maker_fee_bps", f64_field(rule, "maker_fee_bps")?),
        ("min_edge_bps", f64_field(rule, "min_edge_bps")?),
        (
            "volatility_multiplier",
            f64_field(rule, "volatility_multiplier")?,
        ),
        ("book_lean_bps", f64_field(rule, "book_lean_bps")?),
        (
            "signal_half_life_ms",
            f64_field(rule, "signal_half_life_ms")?,
        ),
        (
            "flow_fast_half_life_ms",
            f64_field(flow, "fast_half_life_ms")?,
        ),
        (
            "flow_slow_half_life_ms",
            f64_field(flow, "slow_half_life_ms")?,
        ),
        ("flow_fast_weight", f64_field(flow, "fast_weight")?),
        ("flow_slow_weight", f64_field(flow, "slow_weight")?),
        ("flow_response_bps", f64_field(flow, "response_bps")?),
        ("flow_max_widen_bps", f64_field(flow, "max_widen_bps")?),
        ("flow_depth_bps", f64_field(flow, "near_depth_bps")?),
        (
            "flow_volatility_depth_multiplier",
            f64_field(flow, "volatility_depth_multiplier")?,
        ),
        ("flow_max_score", f64_field(flow, "max_score")?),
        (
            "queue_reprice_edge_bps",
            f64_field(rule, "queue_reprice_edge_bps")?,
        ),
    ];
    if fields.iter().any(|(_, value)| *value <= 0.0) {
        return Err("maker rule numeric fields must be positive".to_owned());
    }
    let mut lines = vec![
        MAKER_RULE_BEGIN.to_owned(),
        format!("symbols = [{}]", serde_json::to_string(symbol).unwrap()),
    ];
    for (name, value) in fields {
        if name == "flow_depth_bps" {
            if let Some(pull) = flow.get("pull_score") {
                if !pull.is_null() {
                    let pull = pull
                        .as_f64()
                        .filter(|number| number.is_finite() && *number > 0.0)
                        .ok_or("maker flow pull_score must be null or positive")?;
                    lines.push(format!("flow_pull_score = {pull:?}"));
                }
            }
        }
        lines.push(format!("{name} = {value:?}"));
    }
    lines.push(MAKER_RULE_END.to_owned());
    Ok(lines.join("\n"))
}

pub fn insert_maker_rule(template: &str, generated: &str) -> Result<String, String> {
    replace_marked_region(template, MAKER_RULE_BEGIN, MAKER_RULE_END, generated)
}

fn replace_marked_region(
    template: &str,
    begin_marker: &str,
    end_marker: &str,
    generated: &str,
) -> Result<String, String> {
    let begin = template
        .find(begin_marker)
        .ok_or_else(|| format!("engine template is missing marker {begin_marker:?}"))?;
    let end = begin
        + template[begin..]
            .find(end_marker)
            .ok_or_else(|| format!("engine template is missing marker {end_marker:?}"))?
        + end_marker.len();
    if template[end..].contains(end_marker) {
        return Err(format!(
            "engine template has duplicate marker {end_marker:?}"
        ));
    }
    let mut out = String::with_capacity(template.len() + generated.len());
    out.push_str(&template[..begin]);
    out.push_str(generated);
    out.push_str(&template[end..]);
    Ok(out)
}

fn render_blocks(
    carry: &CarryStrategyConfig,
    long: &LongStrategyConfig,
    exodus: &ExodusStrategyConfig,
) -> Result<String, String> {
    Ok(format!(
        "[[strategy]]\nname = \"carry_native\"\nsleeve = \"carry\"\nconfig_json = '''{}'''\n\n[[strategy]]\nname = \"long_native\"\nsleeve = \"long\"\nconfig_json = '''{}'''\n\n[[strategy]]\nname = \"exodus_native\"\nsleeve = \"exodus\"\nconfig_json = '''{}'''\n",
        canonical_json(carry)?,
        canonical_json(long)?,
        canonical_json(exodus)?,
    ))
}

fn canonical_json<T: Serialize>(value: &T) -> Result<String, String> {
    let value = serde_json::to_value(value).map_err(|error| error.to_string())?;
    serde_json::to_string(&value).map_err(|error| error.to_string())
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RegisteredLongRuleFile {
    schema_version: u16,
    kind: String,
    profile_name: String,
    rule: RegisteredLongRule,
}

impl RegisteredLongRuleFile {
    fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1
            || self.kind != "liquidity_migration_long_native_rule"
            || !matches!(self.profile_name.as_str(), "v11a" | "v12")
            || self.rule.execution_strategy_id.is_empty()
            || self.rule.exclude_symbols.is_empty()
            || self
                .rule
                .exclude_symbols
                .iter()
                .any(|symbol| symbol.is_empty())
        {
            return Err("LONG registered rule identity is invalid".to_owned());
        }
        let mut sorted = self.rule.exclude_symbols.clone();
        sorted.sort();
        sorted.dedup();
        if sorted != self.rule.exclude_symbols {
            return Err("LONG excluded symbols are not unique and sorted".to_owned());
        }
        let native = self.rule.to_native();
        if (self.profile_name == "v11a"
            && native.execution_strategy_id != "long_native_v11a_div_weekend_vol")
            || (self.profile_name == "v12"
                && native.execution_strategy_id != "long_native_v12_wide_stop")
        {
            return Err("LONG profile and execution identity disagree".to_owned());
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RegisteredLongRule {
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

impl RegisteredLongRule {
    fn to_native(&self) -> LongRuleConfig {
        let _feature_authority = (
            &self.start_date,
            &self.end_date,
            self.universe_size,
            self.universe_volume_window_days,
            self.min_listing_history_days,
            &self.regime_symbol,
            self.regime_sma_days,
            self.vol_estimate_window_days,
            self.cost_multiplier,
        );
        LongRuleConfig {
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

fn parse_carry_rule(value: &Value) -> Result<CarryRuleConfig, String> {
    let config_id = string_path(value, &["config_id"])?;
    let universe = object_path(value, &["rule", "universe"])?;
    if string_field(universe, "venue")? != "bybit"
        || string_field(universe, "rank_by")? != "trailing_24h_quote_turnover"
    {
        return Err("unsupported CARRY universe contract".to_owned());
    }
    let state = object_path(value, &["rule", "state"])?;
    let filters = object_path(value, &["rule", "filters"])?;
    let toxic = object_path(value, &["rule", "filters", "toxic_band_ret_3d"])?;
    let sizing = object_path(value, &["rule", "sizing"])?;
    let depth = object_path(value, &["rule", "sizing", "depth_scaling"])?;
    let persistence = object_path(value, &["rule", "sizing", "persistence_scaling"])?;
    let flow = object_path(value, &["rule", "sizing", "flow_scaling"])?;
    let whale = object_path(value, &["rule", "sizing", "whale_scaling"])?;
    if string_field(depth, "basis")? != "trail_fund_24h"
        || string_field(persistence, "basis")? != "deep_settlement_share"
        || string_field(flow, "basis")? != "turnover_growth_3d"
        || string_field(whale, "basis")? != "binance_toptrader_ls_3d_change"
        || usize_field(persistence, "window_settlements")? != 20
    {
        return Err("unsupported CARRY feature or sizing basis".to_owned());
    }
    let rule = CarryRuleConfig {
        config_id: config_id.to_owned(),
        universe_top_n: usize_field(universe, "top_n")?,
        enter_bp: f64_field(state, "enter_below_funding_bp")?,
        exit_bp: f64_field(state, "exit_above_funding_bp")?,
        per_name_cap: f64_field(sizing, "per_name_cap")?,
        gross_cap: f64_field(sizing, "gross_cap")?,
        depth_ref_bp_per_day: f64_field(depth, "ref_bp_per_day")?,
        depth_floor: f64_field(depth, "floor")?,
        depth_exponent: f64_field(depth, "exponent")?,
        toxic_band_ret3d_lo: f64_field(toxic, "lo")?,
        toxic_band_ret3d_hi: f64_field(toxic, "hi")?,
        min_vol30_daily: f64_field(filters, "min_vol_30d_daily")?,
        trail_recovery_exit_bp_2d: f64_field(state, "exit_on_trail_recovery_bp_2d")?,
        persistence_cut: f64_field(persistence, "cut")?,
        persistence_lo: f64_field(persistence, "low_multiplier")?,
        flow_cut: f64_field(flow, "cut")?,
        flow_lo: f64_field(flow, "low_multiplier")?,
        whale_cut: f64_field(whale, "cut")?,
        whale_lo: f64_field(whale, "low_multiplier")?,
    };
    rule.validate().map_err(str::to_owned)?;
    Ok(rule)
}

fn parse_exodus_rule(value: &Value) -> Result<ExodusRuleConfig, String> {
    let config_id = string_path(value, &["config_id"])?;
    if config_id != "lane2_exodus_short_v1" {
        return Err("unsupported Exodus registered profile identity".to_owned());
    }
    let trigger = object_path(value, &["rule", "trigger"])?;
    let entry = object_path(value, &["rule", "entry"])?;
    let cover = object_path(value, &["rule", "cover"])?;
    let sizing = object_path(value, &["rule", "sizing"])?;
    let stop = object_path(value, &["rule", "stop"])?;
    if string_field(trigger, "basis")? != "carry_presettle_exit_fire"
        || string_field(entry, "side")? != "short"
        || string_field(entry, "at")? != "the fire, immediately; the engine crosses"
        || string_field(sizing, "basis")? != "carry_position_at_fire"
    {
        return Err("unsupported Exodus trigger, entry, or sizing contract".to_owned());
    }
    Ok(ExodusRuleConfig {
        config_id: config_id.to_owned(),
        accepted_source_profile: string_field(trigger, "source_profile")?.to_owned(),
        accepted_source_config_id: string_field(trigger, "source_config_id")?.to_owned(),
        cover_minutes_after_settlement: i64_field(cover, "minutes_after_settlement")?,
        entry_valid_minutes_after_settlement: i64_field(entry, "valid_minutes_after_settlement")?,
        stop_loss_fraction: f64_field(stop, "stop_loss_fraction")?,
    })
}

fn validate_signal(value: &Value, realm: &str) -> Result<(), String> {
    if u64_path(value, &["schema_version"])? != 1
        || string_path(value, &["kind"])? != "liquidity_migration_signal_worker"
        || string_path(value, &["live", "environment"])? != realm
        || string_path(value, &["routing", "long_sleeve"])? != "long"
        || string_path(value, &["routing", "carry_sleeve"])? != "carry"
    {
        return Err("signal config does not bind the requested native realm".to_owned());
    }
    Ok(())
}

fn validate_operational(value: &Value) -> Result<(), String> {
    if u64_path(value, &["schema_version"])? != 3
        || string_path(value, &["kind"])? != "liquidity_migration_operational_profile"
    {
        return Err("unsupported operational config".to_owned());
    }
    let reference = f64_path(value, &["capital_reference_usdt"])?;
    if reference <= 0.0 {
        return Err("operational capital reference is invalid".to_owned());
    }
    Ok(())
}

fn producer_capital_reference(value: &Value) -> Result<f64, String> {
    match value.get("capital_reference") {
        None => f64_path(value, &["capital_reference_usdt"]),
        Some(reference) => {
            if string_path(reference, &["mode"])? != "account_equity" {
                return Err("unsupported operational capital-reference mode".to_owned());
            }
            Ok(0.0)
        }
    }
}

fn parse_json(bytes: &[u8], label: &str) -> Result<Value, String> {
    serde_json::from_slice(bytes).map_err(|error| format!("{label}: {error}"))
}

fn exact_object_keys(value: &Value, expected: &[&str], label: &str) -> Result<(), String> {
    let object = value
        .as_object()
        .ok_or_else(|| format!("{label} must be an object"))?;
    exact_map_keys(object, expected, label)
}

fn exact_map_keys(
    object: &Map<String, Value>,
    expected: &[&str],
    label: &str,
) -> Result<(), String> {
    let mut actual = object.keys().map(String::as_str).collect::<Vec<_>>();
    actual.sort_unstable();
    let mut expected = expected.to_vec();
    expected.sort_unstable();
    if actual != expected {
        return Err(format!("{label} has unexpected or missing fields"));
    }
    Ok(())
}

fn object_path<'a>(value: &'a Value, path: &[&str]) -> Result<&'a Map<String, Value>, String> {
    let mut current = value;
    for key in path {
        current = current
            .get(*key)
            .ok_or_else(|| format!("missing JSON field {}", path.join(".")))?;
    }
    current
        .as_object()
        .ok_or_else(|| format!("JSON field {} must be an object", path.join(".")))
}

fn string_path<'a>(value: &'a Value, path: &[&str]) -> Result<&'a str, String> {
    let mut current = value;
    for key in path {
        current = current
            .get(*key)
            .ok_or_else(|| format!("missing JSON field {}", path.join(".")))?;
    }
    current
        .as_str()
        .ok_or_else(|| format!("JSON field {} must be a string", path.join(".")))
}

fn f64_path(value: &Value, path: &[&str]) -> Result<f64, String> {
    let mut current = value;
    for key in path {
        current = current
            .get(*key)
            .ok_or_else(|| format!("missing JSON field {}", path.join(".")))?;
    }
    current
        .as_f64()
        .filter(|number| number.is_finite())
        .ok_or_else(|| format!("JSON field {} must be a finite number", path.join(".")))
}

fn u64_path(value: &Value, path: &[&str]) -> Result<u64, String> {
    let mut current = value;
    for key in path {
        current = current
            .get(*key)
            .ok_or_else(|| format!("missing JSON field {}", path.join(".")))?;
    }
    current
        .as_u64()
        .ok_or_else(|| format!("JSON field {} must be an integer", path.join(".")))
}

fn i64_path(value: &Value, path: &[&str]) -> Result<i64, String> {
    let mut current = value;
    for key in path {
        current = current
            .get(*key)
            .ok_or_else(|| format!("missing JSON field {}", path.join(".")))?;
    }
    current
        .as_i64()
        .ok_or_else(|| format!("JSON field {} must be an integer", path.join(".")))
}

fn string_field<'a>(object: &'a Map<String, Value>, name: &str) -> Result<&'a str, String> {
    object
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("JSON field {name} must be a string"))
}

fn f64_field(object: &Map<String, Value>, name: &str) -> Result<f64, String> {
    object
        .get(name)
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
        .ok_or_else(|| format!("JSON field {name} must be a finite number"))
}

fn i64_field(object: &Map<String, Value>, name: &str) -> Result<i64, String> {
    object
        .get(name)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("JSON field {name} must be an integer"))
}

fn usize_field(object: &Map<String, Value>, name: &str) -> Result<usize, String> {
    let value = object
        .get(name)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("JSON field {name} must be a nonnegative integer"))?;
    usize::try_from(value).map_err(|_| format!("JSON field {name} exceeds usize"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checked_in_realms_render_strict_directional_blocks() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("repository root");
        for realm in ["demo", "mainnet"] {
            let signal = std::fs::read(root.join(format!("configs/signal-worker.{realm}.json")))
                .expect("signal config");
            let long = std::fs::read(root.join("configs/long_native_v12.json")).expect("LONG rule");
            let carry =
                std::fs::read(root.join("configs/lane2_carry_hold_v7.json")).expect("CARRY rule");
            let exodus = std::fs::read(root.join("configs/lane2_exodus_short_v1.json"))
                .expect("Exodus rule");
            let operational = std::fs::read(root.join(format!("configs/operational.{realm}.json")))
                .expect("operational config");
            let rendered = render_native_config(NativeConfigSources {
                realm,
                signal_config: &signal,
                long_rule: &long,
                carry_rule: &carry,
                exodus_rule: &exodus,
                operational_config: &operational,
                long_entries_enabled: true,
                carry_entries_enabled: true,
                exodus_entries_enabled: true,
            })
            .expect("render");
            assert!(rendered.toml_blocks.contains("name = \"carry_native\""));
            assert!(rendered.toml_blocks.contains("name = \"long_native\""));
            assert!(rendered.toml_blocks.contains("name = \"exodus_native\""));
            assert_eq!(rendered.long.rule_sha256, rendered.long_rule_sha256);
            assert_eq!(rendered.carry.rule_sha256, rendered.carry_rule_sha256);
            assert_eq!(rendered.exodus.rule_sha256, rendered.exodus_rule_sha256);
            assert_eq!(rendered.carry.exodus_sleeve_name, EXODUS_SLEEVE_NAME);
            assert_eq!(rendered.exodus.carry_sleeve_name, CARRY_SLEEVE_NAME);
        }
    }

    #[test]
    fn generated_region_preserves_everything_outside_it() {
        let template = format!("head\n{NATIVE_BLOCKS_BEGIN}\nold\n{NATIVE_BLOCKS_END}\ntail\n");
        let output = insert_native_blocks(&template, "new\n").unwrap();
        assert_eq!(
            output,
            format!("head\n{NATIVE_BLOCKS_BEGIN}\nnew\n{NATIVE_BLOCKS_END}\ntail\n")
        );
    }

    #[test]
    fn registered_maker_rule_matches_the_checked_in_mainnet_region() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .unwrap();
        let registered = std::fs::read(root.join("configs/lane2_toxic_flow_quoter_v1.json"))
            .expect("maker rule");
        let generated = render_maker_rule(&registered).expect("render maker");
        let template =
            std::fs::read_to_string(root.join("deploy/engine.mainnet.toml.template")).unwrap();
        assert_eq!(insert_maker_rule(&template, &generated).unwrap(), template);

        let mut malformed: Value = serde_json::from_slice(&registered).unwrap();
        malformed["rule"]["unknown"] = Value::Bool(true);
        assert!(render_maker_rule(&serde_json::to_vec(&malformed).unwrap()).is_err());
    }
}
