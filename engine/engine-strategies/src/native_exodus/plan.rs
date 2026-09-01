//! Exodus's durable CARRY-fire consumer and exact-quantity short lifecycle.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::native_carry::plan::carry_event_id;
use crate::native_common::{
    checkpoint_payload, config_fingerprint, order_effects, valid_sha256, valid_symbol,
    CarryPresettlementFire, Effect, ExecutionOutput, PlannedTarget, PlannerFacts,
    DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};
use crate::position_plan::{plan as plan_targets, PlanRules, Step, Target};

pub const CONTRACT_SCHEMA_VERSION: u16 = 1;
const MIN_MS: i64 = 60_000;
const EMPTY_VALIDITY_MS: i64 = 6 * 60 * MIN_MS;
const ENGINE_ENTRY_CUTOFF_MS: i64 = 15 * MIN_MS;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuleConfig {
    pub config_id: String,
    pub accepted_source_profile: String,
    pub accepted_source_config_id: String,
    pub cover_minutes_after_settlement: i64,
    pub entry_valid_minutes_after_settlement: i64,
    pub stop_loss_fraction: f64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyConfig {
    pub schema_version: u16,
    pub profile_name: String,
    pub environment: String,
    pub rule_sha256: String,
    pub operational_profile_sha256: String,
    pub entries_enabled: bool,
    /// Stable engine `sleeve`, not the Rust plug name.
    pub carry_sleeve_name: String,
    pub entry_leverage: f64,
    pub rest_entries: bool,
    pub hold_decision_price: bool,
    pub give_up_instead_of_crossing: bool,
    pub rule: RuleConfig,
}

impl StrategyConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != CONTRACT_SCHEMA_VERSION
            || self.profile_name.is_empty()
            || self.environment.is_empty()
            || self.carry_sleeve_name.is_empty()
            || !valid_sha256(&self.rule_sha256)
            || !valid_sha256(&self.operational_profile_sha256)
            || self.rule.config_id.is_empty()
            || self.rule.accepted_source_profile.is_empty()
            || self.rule.accepted_source_config_id.is_empty()
            || self.rule.cover_minutes_after_settlement
                <= self.rule.entry_valid_minutes_after_settlement
            || self.rule.entry_valid_minutes_after_settlement <= 15
            || !self.rule.stop_loss_fraction.is_finite()
            || !(0.0..1.0).contains(&self.rule.stop_loss_fraction)
            || self.rule.stop_loss_fraction == 0.0
            || !self.entry_leverage.is_finite()
            || self.entry_leverage <= 0.0
            || (!self.rest_entries
                && (self.hold_decision_price || self.give_up_instead_of_crossing))
        {
            return Err("Exodus config is invalid");
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
    rule: &'a RuleConfig,
}

/// Stable identity of the CARRY-fire consumer contract. Entry enablement,
/// leverage, and working policy are operational controls, not ownership of
/// already-open records.
pub fn decision_fingerprint(config: &StrategyConfig) -> String {
    config_fingerprint(&DecisionIdentity {
        schema_version: CONTRACT_SCHEMA_VERSION,
        sleeve: "exodus_native",
        profile_name: &config.profile_name,
        environment: &config.environment,
        rule_sha256: &config.rule_sha256,
        rule: &config.rule,
    })
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OpenRecord {
    pub symbol: String,
    pub notional_usdt: f64,
    pub settlement_ts_ms: i64,
    pub fired_ts_ms: i64,
    pub target_qty: Option<f64>,
}

impl OpenRecord {
    fn validate(&self) -> Result<(), &'static str> {
        if !valid_symbol(&self.symbol)
            || !self.notional_usdt.is_finite()
            || self.notional_usdt <= 0.0
            || self.settlement_ts_ms <= 0
            || self.fired_ts_ms <= 0
            || self
                .target_qty
                .is_some_and(|value| !value.is_finite() || value <= 0.0)
        {
            return Err("Exodus open record is invalid");
        }
        Ok(())
    }

    fn cover_ts_ms(&self, config: &StrategyConfig) -> i64 {
        self.settlement_ts_ms + config.rule.cover_minutes_after_settlement * MIN_MS
    }
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SleeveState {
    pub schema_version: u16,
    pub open: BTreeMap<String, OpenRecord>,
    pub consumed_event_ids: BTreeSet<String>,
    pub entry_closed_ts_ms_by_symbol: BTreeMap<String, i64>,
    pub refused_entries: BTreeSet<String>,
    #[serde(default)]
    pub entry_retry_after_ms: BTreeMap<String, i64>,
}

impl SleeveState {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION {
            return Err("unsupported Exodus checkpoint schema");
        }
        for (symbol, record) in &self.open {
            record.validate()?;
            if symbol != &record.symbol {
                return Err("Exodus state key disagrees with its record");
            }
        }
        if self
            .consumed_event_ids
            .iter()
            .any(|value| !value.starts_with("carry-presettlement-"))
            || self
                .entry_closed_ts_ms_by_symbol
                .iter()
                .any(|(symbol, ts)| !self.open.contains_key(symbol) || *ts <= 0)
            || self
                .refused_entries
                .iter()
                .any(|symbol| !valid_symbol(symbol))
            || self.entry_retry_after_ms.iter().any(|(symbol, ts)| {
                !self.refused_entries.contains(symbol)
                    || !self.open.contains_key(symbol)
                    || *ts <= 0
            })
        {
            return Err("Exodus state identity is invalid");
        }
        Ok(())
    }
}

pub struct ReducerInput {
    pub now_ms: i64,
    pub events: Vec<CarryPresettlementFire>,
    pub facts: PlannerFacts,
    pub owned_working_symbols: BTreeSet<String>,
    pub owned_opening_order_ids: BTreeMap<String, Vec<String>>,
    pub account_healthy: bool,
    pub checkpoint_fingerprint: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Summary {
    pub opened_event_ids: Vec<String>,
    pub opened_symbols: Vec<String>,
    pub covered_symbols: Vec<String>,
    pub entry_closed_symbols: Vec<String>,
    pub retired_symbols: Vec<String>,
    pub blocked_events: Vec<(String, String)>,
    pub next_cover_ts_ms: Option<i64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ReducerOutput {
    pub next_state: SleeveState,
    pub summary: Summary,
    pub execution: ExecutionOutput,
}

pub fn reduce(
    input: ReducerInput,
    mut state: SleeveState,
    config: &StrategyConfig,
) -> Result<ReducerOutput, &'static str> {
    config.validate()?;
    if input.now_ms <= 0 {
        return Err("Exodus clock must be positive");
    }
    if state.schema_version == 0 {
        state.schema_version = DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION;
    }
    state.validate()?;
    let fingerprint = config.fingerprint();
    let mismatch = input
        .checkpoint_fingerprint
        .as_ref()
        .is_some_and(|seen| seen != &fingerprint);
    let pending_event_ids = input
        .events
        .iter()
        .map(|event| event.event_id.as_str())
        .collect::<BTreeSet<_>>();
    state
        .consumed_event_ids
        .retain(|event_id| pending_event_ids.contains(event_id.as_str()));
    let mut summary = Summary::default();
    state.entry_retry_after_ms.retain(|symbol, retry_at| {
        let pending = *retry_at > input.now_ms && state.open.contains_key(symbol);
        if !pending {
            state.refused_entries.remove(symbol);
        }
        pending
    });

    // Join the checkpoint to current attributed holdings and orders. A filled
    // short closes the re-entry window. A record retires only after that
    // window closed and both position and entry order are conclusively absent.
    if input.account_healthy {
        let symbols = state.open.keys().cloned().collect::<Vec<_>>();
        for symbol in symbols {
            let held = input.facts.held.get(&symbol);
            if let Some(position) = held {
                let record = &state.open[&symbol];
                let target_reached = position.side == engine_types::Side::Sell
                    && record
                        .target_qty
                        .is_none_or(|target| position.qty >= target * (1.0 - 1e-9));
                if target_reached
                    && !input.owned_working_symbols.contains(&symbol)
                    && !state.entry_closed_ts_ms_by_symbol.contains_key(&symbol)
                {
                    state
                        .entry_closed_ts_ms_by_symbol
                        .insert(symbol.clone(), input.now_ms);
                    summary.entry_closed_symbols.push(symbol.clone());
                }
            } else if state.entry_closed_ts_ms_by_symbol.contains_key(&symbol)
                && !input.owned_working_symbols.contains(&symbol)
            {
                state.open.remove(&symbol);
                state.entry_closed_ts_ms_by_symbol.remove(&symbol);
                state.refused_entries.remove(&symbol);
                state.entry_retry_after_ms.remove(&symbol);
                summary.retired_symbols.push(symbol);
            }
        }
    }

    let mut due = BTreeSet::new();
    for record in state.open.values() {
        if input.now_ms >= record.cover_ts_ms(config) {
            due.insert(record.symbol.clone());
            summary.covered_symbols.push(record.symbol.clone());
        }
    }
    if input.account_healthy {
        for symbol in due.clone() {
            if !input.facts.held.contains_key(&symbol)
                && !input.owned_working_symbols.contains(&symbol)
            {
                state.open.remove(&symbol);
                state.entry_closed_ts_ms_by_symbol.remove(&symbol);
                state.refused_entries.remove(&symbol);
                state.entry_retry_after_ms.remove(&symbol);
                due.remove(&symbol);
                summary.retired_symbols.push(symbol);
            }
        }
    }

    let mut consumed_sources = Vec::<(String, String)>::new();
    for event in input.events {
        validate_event(&event)?;
        if state.consumed_event_ids.contains(&event.event_id) {
            // A crash can leave the checkpoint durable while the source event
            // is still pending. Repeating the acknowledgement retires that
            // event without repeating its trading decision.
            consumed_sources.push((config.carry_sleeve_name.clone(), event.event_id));
            continue;
        }
        let entry_deadline = event.settlement_ts_ms
            + (config.rule.entry_valid_minutes_after_settlement - 15) * MIN_MS;
        let blocked = if event.fired_ts_ms > input.now_ms {
            Some("event_not_yet_available")
        } else if event.environment != config.environment {
            Some("environment_mismatch")
        } else if event.source_profile != config.rule.accepted_source_profile
            || event.source_config_id != config.rule.accepted_source_config_id
        {
            Some("incompatible_source")
        } else {
            None
        };
        if let Some(reason) = blocked {
            summary
                .blocked_events
                .push((event.event_id.clone(), reason.to_owned()));
            continue;
        }
        let terminal_reason = if input.now_ms >= entry_deadline {
            Some("entry_deadline_passed")
        } else if event.carry_side.as_deref() != Some("long")
            || event.carry_qty.is_none()
            || event.mark_px.is_none()
        {
            Some("no_exact_carry_long")
        } else {
            None
        };
        if let Some(reason) = terminal_reason {
            state.consumed_event_ids.insert(event.event_id.clone());
            consumed_sources.push((config.carry_sleeve_name.clone(), event.event_id.clone()));
            summary
                .blocked_events
                .push((event.event_id.clone(), reason.to_owned()));
            continue;
        }
        let transient_block = if !input.account_healthy {
            Some("engine_account_health_unavailable")
        } else if due.contains(&event.symbol) {
            Some("symbol_cover_pending")
        } else if state.open.contains_key(&event.symbol) {
            Some("symbol_already_open")
        } else {
            None
        };
        if let Some(reason) = transient_block {
            summary
                .blocked_events
                .push((event.event_id.clone(), reason.to_owned()));
            continue;
        }
        if input.now_ms < entry_deadline && (!config.entries_enabled || mismatch) {
            summary.blocked_events.push((
                event.event_id.clone(),
                if mismatch {
                    "checkpoint_mismatch".to_owned()
                } else {
                    "entries_disabled".to_owned()
                },
            ));
            continue;
        }
        state.consumed_event_ids.insert(event.event_id.clone());
        consumed_sources.push((config.carry_sleeve_name.clone(), event.event_id.clone()));
        let qty = event.carry_qty.expect("validated quantity");
        let mark = event.mark_px.expect("validated mark");
        state.open.insert(
            event.symbol.clone(),
            OpenRecord {
                symbol: event.symbol.clone(),
                notional_usdt: qty * mark,
                settlement_ts_ms: event.settlement_ts_ms,
                fired_ts_ms: event.fired_ts_ms,
                target_qty: Some(qty),
            },
        );
        summary.opened_event_ids.push(event.event_id);
        summary.opened_symbols.push(event.symbol);
    }

    let mut targets = Vec::new();
    for record in state.open.values() {
        let is_due = due.contains(&record.symbol);
        let growth_blocked = !is_due
            && (record.fired_ts_ms > input.now_ms
                || !config.entries_enabled
                || !input.account_healthy
                || state.refused_entries.contains(&record.symbol));
        let held = input.facts.held.get(&record.symbol);
        if growth_blocked && held.is_none() {
            continue;
        }
        let (capped_qty, capped_notional) = if growth_blocked {
            match held {
                Some(position) if position.side == engine_types::Side::Sell => (
                    record.target_qty.map(|qty| qty.min(position.qty)),
                    -record.notional_usdt.abs().min(position.notional().abs()),
                ),
                Some(_) | None => (None, 0.0),
            }
        } else {
            (record.target_qty, -record.notional_usdt.abs())
        };
        targets.push(PlannedTarget {
            target: Target {
                symbol: record.symbol.clone(),
                notional_usdt: if is_due { 0.0 } else { capped_notional },
                stop_loss_fraction: config.rule.stop_loss_fraction,
                entry_valid_until_ms: (!is_due).then(|| {
                    let ordinary = record.settlement_ts_ms
                        + config.rule.entry_valid_minutes_after_settlement * MIN_MS
                        - ENGINE_ENTRY_CUTOFF_MS;
                    ordinary.min(
                        state
                            .entry_closed_ts_ms_by_symbol
                            .get(&record.symbol)
                            .copied()
                            .unwrap_or(i64::MAX),
                    )
                }),
                target_qty: if is_due {
                    None
                } else {
                    capped_qty.map(|qty| -qty.abs())
                },
            },
            leverage: config.entry_leverage,
        });
    }
    if mismatch {
        for target in &mut targets {
            target.target.notional_usdt = 0.0;
            target.target.target_qty = None;
            target.target.entry_valid_until_ms = None;
        }
    }
    let valid_until_ms = state
        .open
        .values()
        .filter(|record| !due.contains(&record.symbol))
        .map(|record| {
            record.settlement_ts_ms + config.rule.entry_valid_minutes_after_settlement * MIN_MS
        })
        .max()
        .map_or(input.now_ms + EMPTY_VALIDITY_MS, |value| {
            value.max(input.now_ms + MIN_MS)
        });
    let raw_targets = targets
        .iter()
        .map(|target| target.target.clone())
        .collect::<Vec<_>>();
    let planned = plan_targets(
        &raw_targets,
        &input.facts.held_symbols(),
        &input.facts,
        input.now_ms,
        valid_until_ms,
        PlanRules::FLEET,
    );
    summary.next_cover_ts_ms = state
        .open
        .values()
        .filter(|record| !due.contains(&record.symbol))
        .map(|record| record.cover_ts_ms(config))
        .min();

    let mut effects = vec![Effect::PersistCheckpoint {
        symbol: String::new(),
        config_fingerprint: fingerprint,
        payload: checkpoint_payload(&state),
    }];
    effects.extend(
        consumed_sources
            .into_iter()
            .map(
                |(source_strategy_name, event_id)| Effect::ConsumeCarryFire {
                    source_strategy_name,
                    event_id,
                },
            ),
    );
    let mut cancel_symbols = due.clone();
    if mismatch {
        cancel_symbols.extend(input.owned_opening_order_ids.keys().cloned());
    }
    for symbol in &cancel_symbols {
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
            !input.owned_working_symbols.contains(step.symbol())
                || matches!(step, Step::Restop { .. })
        })
        .collect();
    effects.extend(order_effects(steps, &targets, "exodus-native"));
    Ok(ReducerOutput {
        next_state: state,
        summary,
        execution: ExecutionOutput {
            effects,
            skipped: planned.skipped,
        },
    })
}

fn validate_event(event: &CarryPresettlementFire) -> Result<(), &'static str> {
    if !valid_symbol(&event.symbol)
        || event.environment.is_empty()
        || event.source_profile.is_empty()
        || event.source_config_id.is_empty()
        || event.decision_ts_ms <= 0
        || event.fired_ts_ms < event.decision_ts_ms
        || event.settlement_ts_ms <= event.fired_ts_ms
        || event
            .mark_px
            .is_some_and(|value| !value.is_finite() || value <= 0.0)
        || event
            .carry_qty
            .is_some_and(|value| !value.is_finite() || value <= 0.0)
        || event
            .carry_side
            .as_deref()
            .is_some_and(|side| side != "long" && side != "short")
        || (event.carry_side.is_none() != event.carry_qty.is_none())
        || event.event_id
            != carry_event_id(
                &event.environment,
                &event.source_config_id,
                event.decision_ts_ms,
                event.settlement_ts_ms,
                &event.symbol,
            )
    {
        return Err("Exodus CARRY event is invalid");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::position_plan::Held;
    use engine_types::Side;

    const FIXTURE: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/exodus_live_contract_replay_v1.json"
    ));

    fn config(entries_enabled: bool) -> StrategyConfig {
        StrategyConfig {
            schema_version: 1,
            profile_name: "v1".into(),
            environment: "demo".into(),
            rule_sha256: "1".repeat(64),
            operational_profile_sha256: "2".repeat(64),
            entries_enabled,
            carry_sleeve_name: "carry".into(),
            entry_leverage: 5.0,
            rest_entries: false,
            hold_decision_price: false,
            give_up_instead_of_crossing: false,
            rule: RuleConfig {
                config_id: "lane2_exodus_short_v1".into(),
                accepted_source_profile: "carry_hold_v7_live_v1".into(),
                accepted_source_config_id: "lane2_carry_hold_v7".into(),
                cover_minutes_after_settlement: 60,
                entry_valid_minutes_after_settlement: 20,
                stop_loss_fraction: 0.35,
            },
        }
    }

    fn event() -> CarryPresettlementFire {
        let fixture: serde_json::Value = serde_json::from_str(FIXTURE).expect("fixture");
        let value = &fixture["cycles"][0]["decision_input"]["events"][0];
        serde_json::from_value(value.clone()).expect("event")
    }

    #[test]
    fn fixture_cycles_replay_open_fill_cover_and_retire() {
        let mut state = SleeveState::default();
        let cfg = config(true);
        let times = [
            1_800_000_000_000,
            1_800_000_060_000,
            1_800_004_200_000,
            1_800_004_260_000,
        ];
        for (index, now_ms) in times.into_iter().enumerate() {
            let mut facts = PlannerFacts::default();
            if index == 1 || index == 2 {
                facts.held.insert(
                    "AUSDT".into(),
                    Held {
                        side: Side::Sell,
                        qty: 3.25,
                        px: 10.0,
                        entry_px: 10.0,
                        stop_px: 13.5,
                    },
                );
            }
            let output = reduce(
                ReducerInput {
                    now_ms,
                    events: vec![event()],
                    facts,
                    owned_working_symbols: BTreeSet::new(),
                    owned_opening_order_ids: BTreeMap::new(),
                    account_healthy: true,
                    checkpoint_fingerprint: None,
                },
                state,
                &cfg,
            )
            .expect("cycle");
            state = output.next_state;
            match index {
                0 => assert_eq!(output.summary.opened_symbols, ["AUSDT"]),
                1 => assert_eq!(output.summary.entry_closed_symbols, ["AUSDT"]),
                2 => assert_eq!(output.summary.covered_symbols, ["AUSDT"]),
                3 => assert_eq!(output.summary.retired_symbols, ["AUSDT"]),
                _ => unreachable!(),
            }
        }
        assert!(state.open.is_empty());
    }

    #[test]
    fn disabled_entries_leave_valid_fire_pending_but_still_cover_open_record() {
        let fire = event();
        let now_ms = fire.fired_ts_ms;
        let mut state = SleeveState {
            schema_version: 1,
            ..SleeveState::default()
        };
        state.open.insert(
            "BUSDT".into(),
            OpenRecord {
                symbol: "BUSDT".into(),
                notional_usdt: 10.0,
                settlement_ts_ms: now_ms - 4_000_000,
                fired_ts_ms: now_ms - 4_600_000,
                target_qty: Some(1.0),
            },
        );
        let mut facts = PlannerFacts::default();
        facts.held.insert(
            "BUSDT".into(),
            Held {
                side: Side::Sell,
                qty: 1.0,
                px: 10.0,
                entry_px: 10.0,
                stop_px: 13.5,
            },
        );
        let output = reduce(
            ReducerInput {
                now_ms,
                events: vec![fire.clone()],
                facts,
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                account_healthy: true,
                checkpoint_fingerprint: None,
            },
            state,
            &config(false),
        )
        .expect("disabled");
        assert!(output
            .summary
            .blocked_events
            .iter()
            .any(|(_, reason)| reason == "entries_disabled"));
        assert!(!output
            .next_state
            .consumed_event_ids
            .contains(&fire.event_id));
        assert!(output.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Exit { symbol, .. },
                ..
            }) if symbol == "BUSDT"
        )));

        let reopened = reduce(
            ReducerInput {
                now_ms: now_ms + 1,
                events: vec![fire.clone()],
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                account_healthy: true,
                checkpoint_fingerprint: None,
            },
            output.next_state,
            &config(true),
        )
        .expect("re-enabled");
        assert_eq!(reopened.summary.opened_symbols, [fire.symbol]);
        assert!(reopened
            .next_state
            .consumed_event_ids
            .contains(&fire.event_id));
    }

    #[test]
    fn durable_consumption_retires_redelivered_fire_without_reopening() {
        let fire = event();
        let mut state = SleeveState {
            schema_version: 1,
            ..SleeveState::default()
        };
        state.consumed_event_ids.insert(fire.event_id.clone());

        let output = reduce(
            ReducerInput {
                now_ms: fire.fired_ts_ms,
                events: vec![fire.clone()],
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                account_healthy: true,
                checkpoint_fingerprint: Some(config(true).fingerprint()),
            },
            state,
            &config(true),
        )
        .expect("redelivery");

        assert!(output.summary.opened_symbols.is_empty());
        assert_eq!(
            output
                .execution
                .effects
                .iter()
                .filter(|effect| matches!(effect, Effect::Order(_)))
                .count(),
            0
        );
        assert!(matches!(
            output.execution.effects.as_slice(),
            [
                Effect::PersistCheckpoint { .. },
                Effect::ConsumeCarryFire {
                    source_strategy_name,
                    event_id,
                }
            ] if source_strategy_name == "carry" && event_id == &fire.event_id
        ));
    }

    #[test]
    fn expired_fire_is_consumed_even_while_account_health_is_unavailable() {
        let fire = event();
        let deadline = fire.settlement_ts_ms
            + (config(true).rule.entry_valid_minutes_after_settlement - 15) * MIN_MS;
        let output = reduce(
            ReducerInput {
                now_ms: deadline,
                events: vec![fire.clone()],
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                account_healthy: false,
                checkpoint_fingerprint: None,
            },
            SleeveState::default(),
            &config(true),
        )
        .expect("terminal event");
        assert!(output
            .next_state
            .consumed_event_ids
            .contains(&fire.event_id));
        assert!(output
            .summary
            .blocked_events
            .iter()
            .any(|(event_id, reason)| event_id == &fire.event_id
                && reason == "entry_deadline_passed"));
        assert!(output.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::ConsumeCarryFire { event_id, .. } if event_id == &fire.event_id
        )));
    }

    #[test]
    fn consumed_event_tombstone_retires_after_the_engine_event_is_gone() {
        let fire = event();
        let mut state = SleeveState {
            schema_version: 1,
            ..SleeveState::default()
        };
        state.consumed_event_ids.insert(fire.event_id);
        let output = reduce(
            ReducerInput {
                now_ms: 1_800_000_000_000,
                events: Vec::new(),
                facts: PlannerFacts::default(),
                owned_working_symbols: BTreeSet::new(),
                owned_opening_order_ids: BTreeMap::new(),
                account_healthy: true,
                checkpoint_fingerprint: None,
            },
            state,
            &config(true),
        )
        .expect("event acknowledgement observed");
        assert!(output.next_state.consumed_event_ids.is_empty());
    }

    #[test]
    fn unhealthy_account_blocks_partial_growth_but_keeps_reductions_live() {
        let now_ms = 1_800_000_000_000;
        let mut state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        state.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 10.0,
                settlement_ts_ms: now_ms + 20 * MIN_MS,
                fired_ts_ms: now_ms - MIN_MS,
                target_qty: Some(1.0),
            },
        );
        let run = |qty| {
            let mut facts = PlannerFacts::default();
            facts.prices.insert("AUSDT".into(), 10.0);
            facts.rules.insert(
                "AUSDT".into(),
                engine_types::InstrumentRule {
                    tick_size: 0.01,
                    qty_step: 0.1,
                    min_qty: 0.1,
                    min_notional: 5.0,
                },
            );
            facts.held.insert(
                "AUSDT".into(),
                Held {
                    side: Side::Sell,
                    qty,
                    px: 10.0,
                    entry_px: 10.0,
                    stop_px: 13.5,
                },
            );
            reduce(
                ReducerInput {
                    now_ms,
                    events: Vec::new(),
                    facts,
                    owned_working_symbols: BTreeSet::new(),
                    owned_opening_order_ids: BTreeMap::new(),
                    account_healthy: false,
                    checkpoint_fingerprint: None,
                },
                state.clone(),
                &config(true),
            )
            .expect("unhealthy account replan")
        };

        let partial = run(0.5);
        assert!(!partial.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Resize {
                    reduce_only: false,
                    ..
                },
                ..
            })
        )));

        let oversized = run(2.0);
        assert!(oversized.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Resize {
                    reduce_only: true,
                    ..
                },
                ..
            })
        )));
    }

    #[test]
    fn future_durable_fire_blocks_only_entries_and_growth() {
        let now_ms = 1_800_000_000_000;
        let fired_ts_ms = now_ms + MIN_MS;
        let record = OpenRecord {
            symbol: "AUSDT".into(),
            notional_usdt: 10.0,
            settlement_ts_ms: fired_ts_ms + 20 * MIN_MS,
            fired_ts_ms,
            target_qty: Some(1.0),
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            open: BTreeMap::from([("AUSDT".into(), record)]),
            ..SleeveState::default()
        };
        let run = |holding: Option<(Side, f64, f64)>| {
            let mut facts = PlannerFacts::default();
            facts.prices.insert("AUSDT".into(), 10.0);
            facts.rules.insert(
                "AUSDT".into(),
                engine_types::InstrumentRule {
                    tick_size: 0.01,
                    qty_step: 0.1,
                    min_qty: 0.1,
                    min_notional: 5.0,
                },
            );
            if let Some((side, qty, stop_px)) = holding {
                facts.held.insert(
                    "AUSDT".into(),
                    Held {
                        side,
                        qty,
                        px: 10.0,
                        entry_px: 10.0,
                        stop_px,
                    },
                );
            }
            reduce(
                ReducerInput {
                    now_ms,
                    events: Vec::new(),
                    facts,
                    owned_working_symbols: BTreeSet::new(),
                    owned_opening_order_ids: BTreeMap::new(),
                    account_healthy: true,
                    checkpoint_fingerprint: None,
                },
                state.clone(),
                &config(true),
            )
            .expect("backward-clock replan")
        };

        let flat = run(None);
        assert!(!flat
            .execution
            .effects
            .iter()
            .any(|effect| matches!(effect, Effect::Order(_))));

        let partial = run(Some((Side::Sell, 0.5, 13.5)));
        assert!(!partial.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Resize {
                    reduce_only: false,
                    ..
                },
                ..
            })
        )));

        let oversized = run(Some((Side::Sell, 2.0, 13.5)));
        assert!(oversized.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Resize {
                    reduce_only: true,
                    ..
                },
                ..
            })
        )));

        let restop = run(Some((Side::Sell, 1.0, 15.0)));
        assert!(restop.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Restop { .. },
                ..
            })
        )));

        let wrong_side = run(Some((Side::Buy, 1.0, 6.5)));
        assert!(wrong_side.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Exit { .. },
                ..
            })
        )));

        let mut resumed_input = ReducerInput {
            now_ms: fired_ts_ms,
            events: Vec::new(),
            facts: PlannerFacts::default(),
            owned_working_symbols: BTreeSet::new(),
            owned_opening_order_ids: BTreeMap::new(),
            account_healthy: true,
            checkpoint_fingerprint: None,
        };
        resumed_input.facts.prices.insert("AUSDT".into(), 10.0);
        resumed_input.facts.rules.insert(
            "AUSDT".into(),
            engine_types::InstrumentRule {
                tick_size: 0.01,
                qty_step: 0.1,
                min_qty: 0.1,
                min_notional: 5.0,
            },
        );
        let resumed = reduce(resumed_input, flat.next_state, &config(true)).expect("fire time");
        assert!(resumed.execution.effects.iter().any(|effect| matches!(
            effect,
            Effect::Order(crate::native_common::OrderEffect {
                step: Step::Enter { .. },
                ..
            })
        )));
    }
}
