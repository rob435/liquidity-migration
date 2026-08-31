//! Strict stopped-state import for the retired LONG generation. This is a
//! cutover codec, not a runtime strategy or file watcher.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{StrategyImportSource, TranslatedStrategyState};
use serde::Deserialize;

use super::plan::{PriorState, SleeveState, StrategyConfig};
use crate::native_common::{
    checkpoint_payload, valid_symbol, DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};

pub const SOURCE_FORMAT: &str = "long-book-state-v2";
const DAY_MS: i64 = 86_400_000;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyState {
    version: u16,
    held: Vec<LegacyEntry>,
    left_at_ms: BTreeMap<String, i64>,
    attempted_signals_ms: BTreeMap<String, i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyEntry {
    trade_id: String,
    symbol: String,
    strategy_id: String,
    notional_usdt: f64,
    stop_loss_fraction: f64,
    leverage: f64,
    entered_ts_ms: i64,
    entry_price: f64,
    max_hold_deadline_ts_ms: i64,
    seen_held: bool,
    signal_ts_ms: i64,
    stop_decay_after_ms: i64,
    decayed_stop_loss_pct: f64,
    atr_14d_pct: f64,
    pattern: String,
    entry_reason: String,
    venue_qty: f64,
    venue_avg_entry_px: f64,
    venue_ts_ms: i64,
    requested_ts_ms: i64,
    entry_valid_until_ms: i64,
    max_hold_duration_ms: i64,
}

pub fn translate(
    config: &StrategyConfig,
    source_format: &str,
    sources: &[StrategyImportSource],
) -> Result<TranslatedStrategyState, String> {
    if source_format != SOURCE_FORMAT {
        return Err(format!(
            "LONG does not support source format {source_format:?}"
        ));
    }
    if sources.len() != 1 || sources[0].name != "state" {
        return Err("LONG import requires exactly --source state=PATH".to_owned());
    }
    let legacy: LegacyState =
        serde_json::from_slice(&sources[0].bytes).map_err(|error| error.to_string())?;
    if legacy.version != 2 {
        return Err("LONG import only accepts deployed book state v2".to_owned());
    }
    let mut state = SleeveState {
        schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
        ..SleeveState::default()
    };
    let mut seen = BTreeSet::new();
    for row in legacy.held {
        if !seen.insert(row.symbol.clone())
            || !valid_symbol(&row.symbol)
            || row.trade_id.is_empty()
            || row.strategy_id != config.rule.execution_strategy_id
            || !row.notional_usdt.is_finite()
            || row.notional_usdt <= 0.0
            || !row.stop_loss_fraction.is_finite()
            || !(0.0..1.0).contains(&row.stop_loss_fraction)
            || row.stop_loss_fraction == 0.0
            || !row.leverage.is_finite()
            || row.leverage <= 0.0
            || !row.entry_price.is_finite()
            || row.entry_price <= 0.0
            || row.signal_ts_ms <= 0
            || row.stop_decay_after_ms < 0
            || !row.decayed_stop_loss_pct.is_finite()
            || !(0.0..1.0).contains(&row.decayed_stop_loss_pct)
            || !row.atr_14d_pct.is_finite()
            || row.atr_14d_pct < 0.0
            || !row.venue_qty.is_finite()
            || row.venue_qty < 0.0
            || !row.venue_avg_entry_px.is_finite()
            || row.venue_avg_entry_px < 0.0
            || row.venue_ts_ms < 0
            || row.requested_ts_ms <= 0
            || row.entry_valid_until_ms <= row.requested_ts_ms
            || row.max_hold_duration_ms <= 0
            || (row.seen_held
                && (row.entered_ts_ms <= 0 || row.max_hold_deadline_ts_ms <= row.entered_ts_ms))
            || (!row.seen_held && (row.entered_ts_ms != 0 || row.max_hold_deadline_ts_ms != 0))
        {
            return Err(format!("LONG legacy row for {:?} is invalid", row.symbol));
        }
        let _audit_only = (row.pattern, row.entry_reason);
        state.symbols.insert(
            row.symbol.clone(),
            PriorState {
                requested: true,
                filled: row.seen_held,
                entry_ts_ms: row.entered_ts_ms,
                entry_price: row.entry_price,
                target_notional_usdt: row.notional_usdt,
                stop_loss_fraction: row.stop_loss_fraction,
                stop_decay_after_ms: row.stop_decay_after_ms,
                decayed_stop_loss_fraction: row.decayed_stop_loss_pct,
                max_hold_deadline_ts_ms: row.max_hold_deadline_ts_ms,
                max_hold_duration_ms: row.max_hold_duration_ms,
                entry_valid_until_ms: row.entry_valid_until_ms,
                cooldown_until_ms: 0,
                attempted_signal_ts_ms: row.signal_ts_ms,
                active_positions: 0,
            },
        );
        state
            .attempted_signal_ts_ms
            .entry(row.symbol)
            .or_insert(row.signal_ts_ms);
    }
    for (symbol, left_at_ms) in legacy.left_at_ms {
        if !valid_symbol(&symbol) || left_at_ms <= 0 || state.symbols.contains_key(&symbol) {
            return Err("LONG legacy cooldown state is invalid".to_owned());
        }
        state.cooldown_until_ms.insert(
            symbol,
            left_at_ms.saturating_add(config.rule.cooldown_days * DAY_MS),
        );
    }
    for (symbol, signal_ts_ms) in legacy.attempted_signals_ms {
        if !valid_symbol(&symbol) || signal_ts_ms <= 0 {
            return Err("LONG legacy attempted-signal state is invalid".to_owned());
        }
        if state
            .attempted_signal_ts_ms
            .insert(symbol.clone(), signal_ts_ms)
            .is_some_and(|existing| existing != signal_ts_ms)
        {
            return Err(format!(
                "LONG held and global attempted signal disagree for {symbol}"
            ));
        }
    }
    state.validate().map_err(str::to_owned)?;
    Ok(TranslatedStrategyState {
        checkpoint_payload: checkpoint_payload(&state),
        pending_events: Vec::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native_long::plan::RuleConfig;

    fn config() -> StrategyConfig {
        StrategyConfig {
            schema_version: 1,
            profile_name: "v12".into(),
            environment: "mainnet".into(),
            rule_sha256: "a".repeat(64),
            feature_contract_sha256: "c".repeat(64),
            operational_profile_sha256: "b".repeat(64),
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

    #[test]
    fn translates_checked_in_deployed_v2_shape() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/fixtures/long_native_replay_v1.json"
        )))
        .expect("fixture");
        let bytes = serde_json::to_vec(&fixture["expected_live_state"]).expect("state");
        let translated = translate(
            &config(),
            SOURCE_FORMAT,
            &[StrategyImportSource {
                name: "state".into(),
                bytes,
            }],
        )
        .expect("translate");
        let state: SleeveState =
            serde_json::from_slice(&translated.checkpoint_payload).expect("checkpoint");
        assert!(state.symbols["BTCUSDT"].requested);
        assert_eq!(
            state.symbols["BTCUSDT"].target_notional_usdt,
            1800.0000000000002
        );
    }

    #[test]
    fn refuses_identity_mismatch_and_nonfinite_json() {
        let fixture =
            br#"{"version":2,"held":[],"left_at_ms":{},"attempted_signals_ms":{},"extra":1}"#;
        assert!(translate(
            &config(),
            SOURCE_FORMAT,
            &[StrategyImportSource {
                name: "state".into(),
                bytes: fixture.to_vec()
            }]
        )
        .is_err());
        assert!(translate(&config(), "wrong", &[]).is_err());
    }
}
