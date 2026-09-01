//! Strict stopped-state import for CARRY's retired reducer checkpoint and
//! final absolute position snapshot. This is a cutover codec, not a runtime
//! strategy or file watcher.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{StrategyImportSource, TranslatedStrategyState};
use serde::Deserialize;

use super::plan::{SleeveState, StoredTarget, StrategyConfig};
use super::scorer::CarryDecision;
use crate::native_common::{
    checkpoint_payload, valid_symbol, DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};

pub const SOURCE_FORMAT: &str = "carry-reducer-v2-target-book-v1";
pub const SOURCE_FORMAT_LEGACY_V1: &str = "carry-sizing-anchors-v1-early-exits-v1-target-book-v1";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyReducerState {
    schema_version: u16,
    anchors: BTreeMap<String, f64>,
    fired: BTreeMap<String, i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacySizingAnchorsV1 {
    schema_version: u16,
    anchors: BTreeMap<String, f64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyEarlyExitsV1 {
    fired: BTreeMap<String, i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyBook {
    version: u16,
    source: String,
    decision_ts_ms: i64,
    valid_until_ms: i64,
    targets: Vec<LegacyTarget>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyTarget {
    symbol: String,
    notional_usdt: f64,
    stop_loss_fraction: f64,
    leverage: f64,
    #[serde(default)]
    entry_valid_until_ms: Option<i64>,
    #[serde(default)]
    target_qty: Option<f64>,
}

pub fn translate(
    config: &StrategyConfig,
    source_format: &str,
    sources: &[StrategyImportSource],
) -> Result<TranslatedStrategyState, String> {
    let (legacy, book_source) = match source_format {
        SOURCE_FORMAT => {
            require_source_names(sources, &["reducer_checkpoint", "target_book"])?;
            let legacy: LegacyReducerState = serde_json::from_slice(&sources[0].bytes)
                .map_err(|error| format!("CARRY reducer checkpoint: {error}"))?;
            if legacy.schema_version != 2 {
                return Err("CARRY reducer checkpoint schema is not v2".to_owned());
            }
            (legacy, &sources[1])
        }
        SOURCE_FORMAT_LEGACY_V1 => {
            require_source_names(sources, &["early_exits", "sizing_anchors", "target_book"])?;
            let anchors: LegacySizingAnchorsV1 = serde_json::from_slice(&sources[1].bytes)
                .map_err(|error| format!("CARRY sizing anchors: {error}"))?;
            if anchors.schema_version != 1 {
                return Err("CARRY sizing-anchor schema is not v1".to_owned());
            }
            let exits: LegacyEarlyExitsV1 = serde_json::from_slice(&sources[0].bytes)
                .map_err(|error| format!("CARRY early exits: {error}"))?;
            (
                LegacyReducerState {
                    schema_version: 2,
                    anchors: anchors.anchors,
                    fired: exits.fired,
                },
                &sources[2],
            )
        }
        _ => {
            return Err(format!(
                "CARRY does not support source format {source_format:?}"
            ))
        }
    };
    if legacy.anchors.len() > 2 {
        return Err("CARRY reducer checkpoint retains more than two sizing anchors".to_owned());
    }
    let book: LegacyBook = serde_json::from_slice(&book_source.bytes)
        .map_err(|error| format!("CARRY legacy position snapshot: {error}"))?;
    if !matches!(book.version, 1 | 2)
        || book.source != config.profile_name
        || book.decision_ts_ms <= 0
        || book.valid_until_ms <= book.decision_ts_ms
    {
        return Err("CARRY legacy position snapshot identity is invalid".to_owned());
    }
    let mut state = SleeveState {
        schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
        last_publication_decision_ts_ms: book.decision_ts_ms,
        ..SleeveState::default()
    };
    for (raw_ts, equity) in legacy.anchors {
        if raw_ts.is_empty()
            || raw_ts.starts_with('0')
            || !raw_ts.bytes().all(|byte| byte.is_ascii_digit())
            || !equity.is_finite()
            || equity <= 0.0
        {
            return Err("CARRY sizing anchor is invalid".to_owned());
        }
        let ts = raw_ts
            .parse::<i64>()
            .map_err(|_| "CARRY sizing anchor timestamp overflows")?;
        if ts <= 0 || state.sizing_anchors.insert(ts, equity).is_some() {
            return Err("CARRY sizing anchor is invalid".to_owned());
        }
    }
    for (symbol, fired_at) in legacy.fired {
        if !valid_symbol(&symbol) || fired_at <= 0 {
            return Err("CARRY fired exit is invalid".to_owned());
        }
        state.fired_exits.insert(symbol, fired_at);
    }
    let anchor = state.sizing_anchors.get(&book.decision_ts_ms).copied();
    let mut seen = BTreeSet::new();
    let mut weights = BTreeMap::new();
    let mut has_v2_extension = false;
    for target in book.targets {
        if !seen.insert(target.symbol.clone())
            || !valid_symbol(&target.symbol)
            || !target.notional_usdt.is_finite()
            || target.notional_usdt < 0.0
            || !target.stop_loss_fraction.is_finite()
            || !(0.0..1.0).contains(&target.stop_loss_fraction)
            || target.stop_loss_fraction == 0.0
            || !target.leverage.is_finite()
            || target.leverage <= 0.0
            || target.entry_valid_until_ms.is_some_and(|ts| ts <= 0)
            || target
                .target_qty
                .is_some_and(|qty| !qty.is_finite() || qty == 0.0)
        {
            return Err("CARRY legacy position snapshot row is invalid".to_owned());
        }
        has_v2_extension |= target.entry_valid_until_ms.is_some() || target.target_qty.is_some();
        if book.version == 1
            && (target.entry_valid_until_ms.is_some() || target.target_qty.is_some())
        {
            return Err("CARRY legacy position snapshot v1 contains v2 fields".to_owned());
        }
        if target.target_qty.is_some() {
            return Err("CARRY legacy book may not carry exact-quantity targets".to_owned());
        }
        if target.notional_usdt == 0.0 {
            continue;
        }
        let anchor = anchor
            .ok_or("CARRY nonempty legacy snapshot has no matching persisted sizing anchor")?;
        let denominator = anchor * config.notional_multiplier;
        let weight = target.notional_usdt / denominator;
        if !denominator.is_finite() || denominator <= 0.0 || !weight.is_finite() || weight <= 0.0 {
            return Err("CARRY target cannot be joined to its sizing anchor".to_owned());
        }
        let entry_valid_until_ms = target.entry_valid_until_ms.unwrap_or_else(|| {
            book.valid_until_ms
                .saturating_sub(config.execution.engine_entry_cutoff_ms)
        });
        state.desired_targets.insert(
            target.symbol.clone(),
            StoredTarget {
                notional_usdt: target.notional_usdt,
                stop_loss_fraction: target.stop_loss_fraction,
                leverage: target.leverage,
                entry_valid_until_ms,
            },
        );
        weights.insert(target.symbol, weight);
    }
    if book.version == 2 && !has_v2_extension {
        return Err("CARRY legacy position snapshot v2 contains no v2 field".to_owned());
    }
    let gross = weights.values().sum();
    state.current_decision = Some(CarryDecision {
        schema_version: 1,
        decision_ts_ms: book.decision_ts_ms,
        weights,
        universe_size: config.rule.universe_top_n,
        replay_days: 45,
        gross,
    });
    state.validate().map_err(str::to_owned)?;
    Ok(TranslatedStrategyState {
        checkpoint_payload: checkpoint_payload(&state),
        pending_events: Vec::new(),
    })
}

fn require_source_names(sources: &[StrategyImportSource], expected: &[&str]) -> Result<(), String> {
    if sources.len() != expected.len()
        || sources
            .iter()
            .map(|source| source.name.as_str())
            .ne(expected.iter().copied())
    {
        return Err(format!(
            "CARRY import requires exact sorted sources: {}",
            expected.join(", ")
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native_carry::plan::{ExecutionRules, StrategyConfig};
    use crate::native_carry::scorer::CarryRuleConfig;

    fn config() -> StrategyConfig {
        StrategyConfig {
            schema_version: 1,
            profile_name: "carry_hold_v7_live_v1".into(),
            environment: "demo".into(),
            rule_sha256: "a".repeat(64),
            feature_contract_sha256: "c".repeat(64),
            operational_profile_sha256: "b".repeat(64),
            entries_enabled: true,
            exodus_sleeve_name: "exodus".into(),
            rule: CarryRuleConfig {
                config_id: "lane2_carry_hold_v7".into(),
                universe_top_n: 100,
                enter_bp: 10.0,
                exit_bp: 3.0,
                per_name_cap: 0.02,
                gross_cap: 1.0,
                depth_ref_bp_per_day: 20.0,
                depth_floor: 0.25,
                depth_exponent: 1.0,
                toxic_band_ret3d_lo: 0.1,
                toxic_band_ret3d_hi: 0.2,
                min_vol30_daily: 0.005,
                trail_recovery_exit_bp_2d: 4.0,
                persistence_cut: 0.5,
                persistence_lo: 0.5,
                flow_cut: 0.0,
                flow_lo: 0.5,
                whale_cut: 0.0,
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
    fn translates_recorded_checkpoint_and_exact_position_snapshot() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/fixtures/carry_native_replay_v1.json"
        )))
        .expect("fixture");
        let prior = &fixture["prior_state"];
        let anchors = prior["sizing_anchors"]
            .as_array()
            .expect("anchors")
            .iter()
            .map(|row| {
                (
                    row[0].as_i64().expect("ts").to_string(),
                    row[1].as_f64().expect("equity"),
                )
            })
            .collect::<BTreeMap<_, _>>();
        let fired = prior["fired_exits"]
            .as_array()
            .expect("fired")
            .iter()
            .map(|row| {
                (
                    row[0].as_str().expect("symbol").to_owned(),
                    row[1].as_i64().expect("ts"),
                )
            })
            .collect::<BTreeMap<_, _>>();
        let reducer = serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "anchors": anchors,
            "fired": fired,
        }))
        .expect("reducer");
        let book = fixture["target_book_utf8"]
            .as_str()
            .expect("book")
            .as_bytes()
            .to_vec();
        let translated = translate(
            &config(),
            SOURCE_FORMAT,
            &[
                StrategyImportSource {
                    name: "reducer_checkpoint".into(),
                    bytes: reducer,
                },
                StrategyImportSource {
                    name: "target_book".into(),
                    bytes: book,
                },
            ],
        )
        .expect("translate");
        let state: SleeveState =
            serde_json::from_slice(&translated.checkpoint_payload).expect("checkpoint");
        assert_eq!(state.desired_targets["ALPHAUSDT"].notional_usdt, 20.0);
        assert_eq!(
            state
                .current_decision
                .as_ref()
                .expect("decision")
                .decision_ts_ms,
            1_800_000_000_000
        );
    }

    #[test]
    fn translates_the_split_v1_files_written_by_the_retired_producer() {
        let decision_ts_ms = 1_800_000_000_000_i64;
        let translated = translate(
            &config(),
            SOURCE_FORMAT_LEGACY_V1,
            &[
                StrategyImportSource {
                    name: "early_exits".into(),
                    bytes: serde_json::to_vec(&serde_json::json!({
                        "fired": {"BETAUSDT": decision_ts_ms}
                    }))
                    .expect("early exits"),
                },
                StrategyImportSource {
                    name: "sizing_anchors".into(),
                    bytes: serde_json::to_vec(&serde_json::json!({
                        "schema_version": 1,
                        "anchors": {decision_ts_ms.to_string(): 1_000.0}
                    }))
                    .expect("sizing anchors"),
                },
                StrategyImportSource {
                    name: "target_book".into(),
                    bytes: serde_json::to_vec(&serde_json::json!({
                        "version": 1,
                        "source": "carry_hold_v7_live_v1",
                        "decision_ts_ms": decision_ts_ms,
                        "valid_until_ms": decision_ts_ms + 108_000_000,
                        "targets": [{
                            "symbol": "ALPHAUSDT",
                            "notional_usdt": 20.0,
                            "stop_loss_fraction": 0.35,
                            "leverage": 2.0
                        }]
                    }))
                    .expect("target book"),
                },
            ],
        )
        .expect("translate split v1 files");

        let state: SleeveState =
            serde_json::from_slice(&translated.checkpoint_payload).expect("checkpoint");
        assert_eq!(state.sizing_anchors[&decision_ts_ms], 1_000.0);
        assert_eq!(state.fired_exits["BETAUSDT"], decision_ts_ms);
        assert_eq!(state.desired_targets["ALPHAUSDT"].notional_usdt, 20.0);
    }

    #[test]
    fn split_v1_import_rejects_an_upgraded_or_reordered_source_bundle() {
        let sources = [
            StrategyImportSource {
                name: "sizing_anchors".into(),
                bytes: br#"{"schema_version":1,"anchors":{}}"#.to_vec(),
            },
            StrategyImportSource {
                name: "early_exits".into(),
                bytes: br#"{"fired":{}}"#.to_vec(),
            },
            StrategyImportSource {
                name: "target_book".into(),
                bytes: Vec::new(),
            },
        ];
        assert!(translate(&config(), SOURCE_FORMAT_LEGACY_V1, &sources)
            .expect_err("source order is part of the stopped-state contract")
            .contains("exact sorted sources"));
    }

    #[test]
    fn split_v1_import_rejects_a_present_malformed_early_exit_file() {
        let decision_ts_ms = 1_800_000_000_000_i64;
        let sources = [
            StrategyImportSource {
                name: "early_exits".into(),
                bytes: br#"{"fired":[]}"#.to_vec(),
            },
            StrategyImportSource {
                name: "sizing_anchors".into(),
                bytes: br#"{"schema_version":1,"anchors":{}}"#.to_vec(),
            },
            StrategyImportSource {
                name: "target_book".into(),
                bytes: serde_json::to_vec(&serde_json::json!({
                    "version": 1,
                    "source": "carry_hold_v7_live_v1",
                    "decision_ts_ms": decision_ts_ms,
                    "valid_until_ms": decision_ts_ms + 108_000_000,
                    "targets": [],
                }))
                .expect("target book"),
            },
        ];
        assert!(translate(&config(), SOURCE_FORMAT_LEGACY_V1, &sources)
            .expect_err("a present malformed source must not become an empty default")
            .contains("CARRY early exits"));
    }
}
