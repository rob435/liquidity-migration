//! The registered CARRY daily hysteresis and weight rule.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::native_common::valid_symbol;

pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 24 * HOUR_MS;
pub const MIN_REPLAY_DAYS: i64 = 45;
pub const MIN_DECISION_SYMBOLS: usize = 50;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CarryRuleConfig {
    pub config_id: String,
    pub universe_top_n: usize,
    pub enter_bp: f64,
    pub exit_bp: f64,
    pub per_name_cap: f64,
    pub gross_cap: f64,
    pub depth_ref_bp_per_day: f64,
    pub depth_floor: f64,
    pub depth_exponent: f64,
    pub toxic_band_ret3d_lo: f64,
    pub toxic_band_ret3d_hi: f64,
    pub min_vol30_daily: f64,
    pub trail_recovery_exit_bp_2d: f64,
    pub persistence_cut: f64,
    pub persistence_lo: f64,
    pub flow_cut: f64,
    pub flow_lo: f64,
    pub whale_cut: f64,
    pub whale_lo: f64,
}

/// Registered research variants before v7. Optional fields mean the rule did
/// not use that feature; a null market feature still fails open when enabled.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchRuleConfig {
    pub config_id: String,
    pub enter_bp: f64,
    pub exit_bp: f64,
    pub per_name_cap: f64,
    pub gross_cap: f64,
    pub depth_ref_bp_per_day: Option<f64>,
    pub depth_floor: f64,
    pub depth_exponent: f64,
    pub toxic_band_ret3d: Option<(f64, f64)>,
    pub min_vol30_daily: Option<f64>,
    pub trail_recovery_exit_bp_2d: Option<f64>,
    pub persistence_cut: Option<f64>,
    pub persistence_lo: f64,
    pub flow_cut: Option<f64>,
    pub flow_lo: f64,
    pub whale_cut: Option<f64>,
    pub whale_lo: f64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchWeight {
    pub bar_ts_ms: i64,
    pub symbol: String,
    pub w: f64,
}

impl ResearchRuleConfig {
    fn validate(&self) -> Result<(), &'static str> {
        if self.config_id.is_empty() {
            return Err("CARRY research rule identity is required");
        }
        let positive = [
            self.enter_bp,
            self.exit_bp,
            self.per_name_cap,
            self.gross_cap,
            self.depth_floor,
            self.depth_exponent,
            self.persistence_lo,
            self.flow_lo,
            self.whale_lo,
        ];
        if positive
            .iter()
            .any(|value| !value.is_finite() || *value < 0.0)
            || self.enter_bp == 0.0
            || self.exit_bp == 0.0
            || self.per_name_cap == 0.0
            || self.gross_cap == 0.0
            || self.depth_exponent == 0.0
            || self
                .depth_ref_bp_per_day
                .is_some_and(|value| !value.is_finite() || value <= 0.0)
            || self.min_vol30_daily.is_some_and(|value| !value.is_finite())
            || self
                .trail_recovery_exit_bp_2d
                .is_some_and(|value| !value.is_finite())
            || self.persistence_cut.is_some_and(|value| !value.is_finite())
            || self.flow_cut.is_some_and(|value| !value.is_finite())
            || self.whale_cut.is_some_and(|value| !value.is_finite())
        {
            return Err("CARRY research rule number is invalid");
        }
        if self.per_name_cap > self.gross_cap
            || self.depth_floor > 1.0
            || self.persistence_lo > 1.0
            || self.flow_lo > 1.0
            || self.whale_lo > 1.0
            || self
                .toxic_band_ret3d
                .is_some_and(|(lo, hi)| !lo.is_finite() || !hi.is_finite() || lo >= hi)
        {
            return Err("CARRY research rule bounds are invalid");
        }
        Ok(())
    }
}

impl CarryRuleConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.config_id.is_empty() || self.universe_top_n == 0 {
            return Err("CARRY rule identity and universe are required");
        }
        let positive = [
            self.enter_bp,
            self.exit_bp,
            self.per_name_cap,
            self.gross_cap,
            self.depth_ref_bp_per_day,
            self.depth_floor,
            self.depth_exponent,
            self.min_vol30_daily,
            self.trail_recovery_exit_bp_2d,
            self.flow_lo,
            self.whale_lo,
        ];
        if positive
            .iter()
            .any(|value| !value.is_finite() || *value <= 0.0)
        {
            return Err("CARRY positive rule field is invalid");
        }
        let finite = [
            self.toxic_band_ret3d_lo,
            self.toxic_band_ret3d_hi,
            self.persistence_cut,
            self.persistence_lo,
            self.flow_cut,
            self.whale_cut,
        ];
        if finite.iter().any(|value| !value.is_finite()) {
            return Err("CARRY rule field is non-finite");
        }
        if self.toxic_band_ret3d_lo >= self.toxic_band_ret3d_hi
            || self.per_name_cap > self.gross_cap
            || self.depth_floor > 1.0
            || self.persistence_lo < 0.0
            || self.persistence_lo > 1.0
            || self.flow_lo > 1.0
            || self.whale_lo > 1.0
        {
            return Err("CARRY rule bounds are invalid");
        }
        Ok(())
    }
}

impl From<&CarryRuleConfig> for ResearchRuleConfig {
    fn from(config: &CarryRuleConfig) -> Self {
        Self {
            config_id: config.config_id.clone(),
            enter_bp: config.enter_bp,
            exit_bp: config.exit_bp,
            per_name_cap: config.per_name_cap,
            gross_cap: config.gross_cap,
            depth_ref_bp_per_day: Some(config.depth_ref_bp_per_day),
            depth_floor: config.depth_floor,
            depth_exponent: config.depth_exponent,
            toxic_band_ret3d: Some((config.toxic_band_ret3d_lo, config.toxic_band_ret3d_hi)),
            min_vol30_daily: Some(config.min_vol30_daily),
            trail_recovery_exit_bp_2d: Some(config.trail_recovery_exit_bp_2d),
            persistence_cut: Some(config.persistence_cut),
            persistence_lo: config.persistence_lo,
            flow_cut: Some(config.flow_cut),
            flow_lo: config.flow_lo,
            whale_cut: Some(config.whale_cut),
            whale_lo: config.whale_lo,
        }
    }
}

/// Already causal decision-grid features produced by the Rust signal worker.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct CarryFeatureRow {
    pub symbol: String,
    pub bar_ts_ms: i64,
    pub by_close: Option<f64>,
    pub by_turnover_quote: Option<f64>,
    pub by_funding: Option<f64>,
    pub by_funding_age_h: Option<f64>,
    pub adv24: Option<f64>,
    pub trail_fund_24h: Option<f64>,
    pub momentum: Option<f64>,
    pub ret_3d: Option<f64>,
    pub vol_30d_daily: Option<f64>,
    pub dtrail_2d: Option<f64>,
    pub crowd_persistence: Option<f64>,
    pub turn_growth_3d: Option<f64>,
    pub d_tt_ls_3d: Option<f64>,
    pub adv_rank: Option<u32>,
    pub in_universe: bool,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HysteresisState {
    pub held: bool,
    pub last_ts_ms: i64,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ScorerState {
    pub by_symbol: BTreeMap<String, HysteresisState>,
    pub last_decision_ts_ms: i64,
    pub first_replay_ts_ms: i64,
    pub last_weights: BTreeMap<String, f64>,
    pub last_universe_size: usize,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CarryDecision {
    pub schema_version: u16,
    pub decision_ts_ms: i64,
    pub weights: BTreeMap<String, f64>,
    pub universe_size: usize,
    pub replay_days: i64,
    pub gross: f64,
}

/// Rebuild or incrementally advance the registered daily scorer. Rows may
/// contain a full replay window; already-checkpointed days are ignored.
pub fn score_decision(
    rows: &[CarryFeatureRow],
    decision_ts_ms: i64,
    prior: &ScorerState,
    config: &CarryRuleConfig,
) -> Result<(CarryDecision, ScorerState), &'static str> {
    config.validate()?;
    if decision_ts_ms <= 0 || decision_ts_ms % DAY_MS != 0 {
        return Err("CARRY decision must be a positive UTC day boundary");
    }
    if rows.is_empty() {
        return Err("CARRY feature batch is empty");
    }
    for row in rows {
        if !valid_symbol(&row.symbol) || row.bar_ts_ms <= 0 || row.bar_ts_ms % DAY_MS != 0 {
            return Err("CARRY feature row identity is invalid");
        }
        if row.by_funding.is_some_and(|value| !value.is_finite()) {
            return Err("CARRY funding is non-finite");
        }
    }
    let first = rows
        .iter()
        .map(|row| row.bar_ts_ms)
        .min()
        .ok_or("CARRY feature batch is empty")?;
    let last = rows
        .iter()
        .map(|row| row.bar_ts_ms)
        .max()
        .ok_or("CARRY feature batch is empty")?;
    if last < decision_ts_ms {
        return Err("CARRY feature batch is stale");
    }
    let replay_days = (decision_ts_ms - first) / DAY_MS;
    if prior.last_decision_ts_ms == 0 && replay_days < MIN_REPLAY_DAYS {
        return Err("CARRY replay is below the 45-day floor");
    }
    if prior.last_decision_ts_ms == decision_ts_ms {
        return Ok((
            CarryDecision {
                schema_version: 1,
                decision_ts_ms,
                weights: prior.last_weights.clone(),
                universe_size: prior.last_universe_size,
                replay_days: (decision_ts_ms
                    - if prior.first_replay_ts_ms > 0 {
                        prior.first_replay_ts_ms
                    } else {
                        first
                    })
                    / DAY_MS,
                gross: prior.last_weights.values().sum(),
            },
            prior.clone(),
        ));
    }

    // Recreate Polars' per-day top-N universe deterministically. A tie is
    // broken by symbol rather than input order, so spool chunking cannot alter
    // membership.
    let mut by_ts = BTreeMap::<i64, Vec<&CarryFeatureRow>>::new();
    for row in rows.iter().filter(|row| row.bar_ts_ms <= decision_ts_ms) {
        by_ts.entry(row.bar_ts_ms).or_default().push(row);
    }
    let mut selected = Vec::new();
    let mut current_universe = BTreeSet::new();
    for (ts, day_rows) in &mut by_ts {
        day_rows.sort_by(|left, right| {
            let left_adv = finite(left.adv24).unwrap_or(f64::NEG_INFINITY);
            let right_adv = finite(right.adv24).unwrap_or(f64::NEG_INFINITY);
            right_adv
                .partial_cmp(&left_adv)
                .unwrap_or(Ordering::Equal)
                .then_with(|| left.symbol.cmp(&right.symbol))
        });
        // `_signal_frame` is symbol-then-time sorted before Polars applies its
        // ordinal rank. Symbol order therefore is the exact tie break, not a
        // new economic rule. Every mature row participates; `in_universe` is
        // worker audit metadata and does not gate the registered top-N.
        for row in day_rows
            .iter()
            .filter(|row| finite(row.adv24).is_some())
            .take(config.universe_top_n)
        {
            selected.push(*row);
            if *ts == decision_ts_ms {
                current_universe.insert(row.symbol.clone());
            }
        }
    }
    if current_universe.len() < MIN_DECISION_SYMBOLS {
        return Err("CARRY decision universe has fewer than 50 symbols");
    }

    let mut state = prior.clone();
    let scoring_rule = ResearchRuleConfig::from(config);
    let mut by_symbol = BTreeMap::<String, Vec<&CarryFeatureRow>>::new();
    for row in selected {
        // Python drops null funding before the state loop. A missing print is
        // therefore a missing daily state row, and the next row resets on the
        // timestamp gap instead of preserving continuity through the hole.
        if row.bar_ts_ms > prior.last_decision_ts_ms && finite(row.by_funding).is_some() {
            by_symbol.entry(row.symbol.clone()).or_default().push(row);
        }
    }
    let mut current_weights = BTreeMap::new();
    for (symbol, symbol_rows) in by_symbol {
        let mut hysteresis = state.by_symbol.get(&symbol).cloned().unwrap_or_default();
        let mut rows = symbol_rows;
        rows.sort_by_key(|row| row.bar_ts_ms);
        for row in rows {
            if let Some(weight) = advance_row(&mut hysteresis, row, &scoring_rule) {
                if row.bar_ts_ms == decision_ts_ms {
                    current_weights.insert(symbol.clone(), weight);
                }
            }
        }
        state.by_symbol.insert(symbol, hysteresis);
    }

    let raw_gross: f64 = current_weights.values().sum();
    if raw_gross > config.gross_cap {
        let scale = config.gross_cap / raw_gross;
        for weight in current_weights.values_mut() {
            *weight *= scale;
        }
    }
    let gross = current_weights.values().sum();
    state.last_decision_ts_ms = decision_ts_ms;
    if state.first_replay_ts_ms == 0 {
        state.first_replay_ts_ms = first;
    }
    state.last_weights = current_weights.clone();
    state.last_universe_size = current_universe.len();
    Ok((
        CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: current_weights,
            universe_size: current_universe.len(),
            replay_days,
            gross,
        },
        state,
    ))
}

/// Score an already-ranked research universe with the same per-row state
/// transition used by the live v7 scorer. The caller owns feature construction
/// and top-N membership; Rust owns every entry, exit, suspension and weight.
pub fn score_research(
    rows: &[CarryFeatureRow],
    config: &ResearchRuleConfig,
) -> Result<Vec<ResearchWeight>, &'static str> {
    config.validate()?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    for row in rows {
        if !valid_symbol(&row.symbol) || row.bar_ts_ms < 0 {
            return Err("CARRY research feature row identity is invalid");
        }
        if row.by_funding.is_some_and(|value| !value.is_finite()) {
            return Err("CARRY funding is non-finite");
        }
    }

    let mut by_symbol = BTreeMap::<String, Vec<&CarryFeatureRow>>::new();
    for row in rows.iter().filter(|row| finite(row.by_funding).is_some()) {
        by_symbol.entry(row.symbol.clone()).or_default().push(row);
    }
    let mut by_day = BTreeMap::<i64, Vec<ResearchWeight>>::new();
    for (symbol, symbol_rows) in by_symbol {
        let mut hysteresis = HysteresisState::default();
        let mut ordered = symbol_rows;
        ordered.sort_by_key(|row| row.bar_ts_ms);
        for row in ordered {
            if let Some(weight) = advance_row(&mut hysteresis, row, config) {
                by_day
                    .entry(row.bar_ts_ms)
                    .or_default()
                    .push(ResearchWeight {
                        bar_ts_ms: row.bar_ts_ms,
                        symbol: symbol.clone(),
                        w: weight,
                    });
            }
        }
    }

    let mut output = Vec::new();
    for weights in by_day.values_mut() {
        let gross: f64 = weights.iter().map(|row| row.w.abs()).sum();
        if gross > config.gross_cap {
            let scale = config.gross_cap / gross;
            for row in weights.iter_mut() {
                row.w *= scale;
            }
        }
        output.append(weights);
    }
    output.sort_by(|left, right| {
        left.symbol
            .cmp(&right.symbol)
            .then_with(|| left.bar_ts_ms.cmp(&right.bar_ts_ms))
    });
    Ok(output)
}

fn advance_row(
    state: &mut HysteresisState,
    row: &CarryFeatureRow,
    config: &ResearchRuleConfig,
) -> Option<f64> {
    if (state.last_ts_ms > 0 || state.held) && row.bar_ts_ms != state.last_ts_ms + DAY_MS {
        state.held = false;
    }
    state.last_ts_ms = row.bar_ts_ms;
    let funding = finite(row.by_funding).expect("null funding filtered before state loop");
    let mut exited = false;
    if state.held && funding >= -(config.exit_bp / 10_000.0) {
        state.held = false;
        exited = true;
    }
    if state.held
        && config.trail_recovery_exit_bp_2d.is_some_and(|threshold| {
            finite(row.dtrail_2d).is_some_and(|value| value > threshold / 10_000.0)
        })
    {
        state.held = false;
        exited = true;
    }

    let toxic = config
        .toxic_band_ret3d
        .is_some_and(|(lo, hi)| finite(row.ret_3d).is_some_and(|value| value >= lo && value < hi));
    if !state.held && !exited && funding < -(config.enter_bp / 10_000.0) {
        let dead = config
            .min_vol30_daily
            .is_some_and(|floor| finite(row.vol_30d_daily).is_some_and(|value| value < floor));
        if !toxic && !dead {
            state.held = true;
        }
    }
    if !state.held || toxic {
        return None;
    }

    let mut weight = config.per_name_cap;
    if let Some(reference_bp) = config.depth_ref_bp_per_day {
        let depth = finite(row.trail_fund_24h).unwrap_or(0.0).abs();
        let mut scale = depth / (reference_bp / 10_000.0);
        if config.depth_exponent != 1.0 {
            scale = scale.powf(config.depth_exponent);
        }
        weight *= scale.clamp(config.depth_floor, 1.0);
    }
    if config
        .persistence_cut
        .is_some_and(|cut| finite(row.crowd_persistence).is_some_and(|value| value <= cut))
    {
        weight *= config.persistence_lo;
    }
    if config
        .flow_cut
        .is_some_and(|cut| finite(row.turn_growth_3d).is_some_and(|value| value <= cut))
    {
        weight *= config.flow_lo;
    }
    if config
        .whale_cut
        .is_some_and(|cut| finite(row.d_tt_ls_3d).is_some_and(|value| value <= cut))
    {
        weight *= config.whale_lo;
    }
    (weight > 0.0).then_some(weight)
}

fn finite(value: Option<f64>) -> Option<f64> {
    value.filter(|number| number.is_finite())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> CarryRuleConfig {
        CarryRuleConfig {
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
        }
    }

    #[test]
    fn one_day_gap_resets_hysteresis_before_reentry() {
        let mut rows = Vec::new();
        let decision = 100 * DAY_MS;
        for day in 1..=100 {
            for index in 0..50 {
                if index == 0 && day == 99 {
                    continue;
                }
                rows.push(CarryFeatureRow {
                    symbol: format!("S{index:02}USDT"),
                    bar_ts_ms: day * DAY_MS,
                    by_funding: Some(-0.002),
                    adv24: Some((100 - index) as f64),
                    trail_fund_24h: Some(-0.012),
                    ret_3d: Some(0.1),
                    vol_30d_daily: Some(0.1),
                    dtrail_2d: Some(0.0),
                    crowd_persistence: Some(0.5),
                    turn_growth_3d: Some(1.0),
                    d_tt_ls_3d: Some(1.0),
                    in_universe: true,
                    ..CarryFeatureRow::default()
                });
            }
        }
        let (book, _) =
            score_decision(&rows, decision, &ScorerState::default(), &config()).expect("book");
        assert!(book.weights.contains_key("S00USDT"));
        assert!(book.gross <= 1.0 + 1e-12);
    }

    #[test]
    fn null_funding_day_breaks_continuity_and_same_day_exit_cannot_reenter() {
        let mut cfg = config();
        cfg.universe_top_n = 50;
        let mut rows = Vec::new();
        for day in 1..=50 {
            for index in 0..50 {
                let funding = if index == 0 && day == 49 {
                    None
                } else if index == 1 && day == 50 {
                    Some(0.0)
                } else {
                    Some(-0.002)
                };
                rows.push(CarryFeatureRow {
                    symbol: format!("S{index:02}USDT"),
                    bar_ts_ms: day * DAY_MS,
                    by_funding: funding,
                    adv24: Some((100 - index) as f64),
                    trail_fund_24h: Some(-0.012),
                    ret_3d: Some(0.1),
                    vol_30d_daily: Some(0.1),
                    dtrail_2d: Some(0.0),
                    crowd_persistence: Some(0.5),
                    turn_growth_3d: Some(1.0),
                    d_tt_ls_3d: Some(1.0),
                    in_universe: true,
                    ..CarryFeatureRow::default()
                });
            }
        }
        let mut prior = ScorerState::default();
        prior.by_symbol.insert(
            "S01USDT".into(),
            HysteresisState {
                held: true,
                last_ts_ms: 49 * DAY_MS,
            },
        );
        prior.last_decision_ts_ms = 49 * DAY_MS;
        let (book, state) = score_decision(&rows, 50 * DAY_MS, &prior, &cfg).expect("book");
        assert!(!book.weights.contains_key("S01USDT"));
        assert!(!state.by_symbol["S01USDT"].held);
        assert!(book.weights.contains_key("S00USDT"));
    }

    #[test]
    fn toxic_hold_suspends_and_missing_conditioning_fails_open() {
        let mut rows = Vec::new();
        for day in 1..=50 {
            for index in 0..50 {
                rows.push(CarryFeatureRow {
                    symbol: format!("S{index:02}USDT"),
                    bar_ts_ms: day * DAY_MS,
                    by_funding: Some(-0.002),
                    adv24: Some((100 - index) as f64),
                    trail_fund_24h: Some(-0.012),
                    ret_3d: if day == 50 && index == 0 {
                        Some(-0.1)
                    } else {
                        None
                    },
                    vol_30d_daily: None,
                    dtrail_2d: None,
                    crowd_persistence: None,
                    turn_growth_3d: None,
                    d_tt_ls_3d: None,
                    in_universe: true,
                    ..CarryFeatureRow::default()
                });
            }
        }
        let (book, _) =
            score_decision(&rows, 50 * DAY_MS, &ScorerState::default(), &config()).expect("book");
        assert!(!book.weights.contains_key("S00USDT"));
        assert!(book.weights.contains_key("S01USDT"));
    }

    #[test]
    fn gross_cap_scales_every_name_proportionally() {
        let mut cfg = config();
        cfg.gross_cap = 0.5;
        let mut rows = Vec::new();
        for day in 1..=50 {
            for index in 0..50 {
                rows.push(CarryFeatureRow {
                    symbol: format!("S{index:02}USDT"),
                    bar_ts_ms: day * DAY_MS,
                    by_funding: Some(-0.002),
                    adv24: Some((100 - index) as f64),
                    trail_fund_24h: Some(-0.012),
                    ret_3d: Some(0.1),
                    vol_30d_daily: Some(0.1),
                    dtrail_2d: Some(0.0),
                    crowd_persistence: Some(0.5),
                    turn_growth_3d: Some(1.0),
                    d_tt_ls_3d: Some(1.0),
                    in_universe: true,
                    ..CarryFeatureRow::default()
                });
            }
        }
        let (book, _) =
            score_decision(&rows, 50 * DAY_MS, &ScorerState::default(), &cfg).expect("book");
        assert!((book.gross - 0.5).abs() < 1e-12);
        assert!(book
            .weights
            .values()
            .all(|weight| (*weight - 0.01).abs() < 1e-12));
    }

    #[test]
    fn research_score_uses_null_fail_open_and_resets_on_a_gap() {
        let rule = ResearchRuleConfig::from(&config());
        let rows = vec![
            CarryFeatureRow {
                symbol: "ALPHAUSDT".into(),
                bar_ts_ms: DAY_MS,
                by_funding: Some(-0.002),
                trail_fund_24h: None,
                ret_3d: None,
                vol_30d_daily: None,
                dtrail_2d: None,
                crowd_persistence: None,
                turn_growth_3d: None,
                d_tt_ls_3d: None,
                ..CarryFeatureRow::default()
            },
            CarryFeatureRow {
                symbol: "ALPHAUSDT".into(),
                bar_ts_ms: 3 * DAY_MS,
                by_funding: Some(-0.0005),
                trail_fund_24h: Some(-0.012),
                ret_3d: Some(0.1),
                vol_30d_daily: Some(0.1),
                dtrail_2d: Some(0.0),
                crowd_persistence: Some(0.5),
                turn_growth_3d: Some(1.0),
                d_tt_ls_3d: Some(1.0),
                ..CarryFeatureRow::default()
            },
        ];

        let weights = score_research(&rows, &rule).expect("research weights");

        assert_eq!(weights.len(), 1);
        assert_eq!(weights[0].bar_ts_ms, DAY_MS);
        assert!((weights[0].w - 0.025).abs() < 1e-12);
    }
}
