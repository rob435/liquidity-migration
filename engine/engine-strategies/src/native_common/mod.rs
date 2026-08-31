//! Shared mechanics for the native directional sleeves.
//!
//! The directional reducers do not send orders themselves. They return this
//! small ordered effect language. The engine adapter persists each checkpoint
//! or handoff before it submits the next order effect, preserving the same
//! state-before-side-effect rule as the WAL-backed in-loop strategies.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    Action, InstrumentRule, Intent, OrderKind, Side, StopSpec, StrategyCheckpoint, StrategyCtx,
    StrategyEvent, StrategyId, WorkPolicy,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::position_plan::{Held, PlanRules, Skipped, Step, SymbolFacts, Target};

pub const DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION: u16 = 1;
pub const CARRY_SLEEVE_NAME: &str = "carry";
pub const EXODUS_SLEEVE_NAME: &str = "exodus";

/// Config identity copied into every signal-worker payload. The strategy
/// checks the fields that bind its own registered rule; the remaining hashes
/// keep the source artifact auditable without making operational settings a
/// durable-state identity.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SignalConfigIdentity {
    pub schema_version: u32,
    pub signal_config_id: String,
    pub long_profile: String,
    pub long_execution_strategy_id: String,
    pub long_rule_sha256: String,
    pub long_feature_contract_sha256: String,
    pub signal_config_sha256: String,
    pub carry_config_id: String,
    pub carry_rule_sha256: String,
    pub carry_feature_contract_sha256: String,
    pub operational_profile_sha256: String,
    pub engine_config_sha256: String,
    pub long_decision_fingerprint: String,
    pub carry_decision_fingerprint: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UniverseMode {
    Current,
    Pit,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UniverseIdentity {
    pub mode: UniverseMode,
    pub environment: String,
    pub endpoint: String,
    pub snapshot_ts_ms: i64,
    pub available_at_ms: i64,
    pub artifact_sha256: String,
    pub file_sha256: String,
    pub symbols: Vec<String>,
    pub long_symbols: Vec<String>,
    pub carry_symbols: Vec<String>,
}

pub fn validate_signal_identity(
    config: &SignalConfigIdentity,
    universe: Option<&UniverseIdentity>,
    expected_environment: &str,
) -> Result<(), &'static str> {
    if config.schema_version != 1
        || config.signal_config_id.is_empty()
        || !valid_sha256(&config.long_rule_sha256)
        || !valid_sha256(&config.long_feature_contract_sha256)
        || !valid_sha256(&config.signal_config_sha256)
        || !valid_sha256(&config.carry_rule_sha256)
        || !valid_sha256(&config.carry_feature_contract_sha256)
        || !valid_sha256(&config.operational_profile_sha256)
        || !valid_sha256(&config.engine_config_sha256)
        || !valid_sha256(&config.long_decision_fingerprint)
        || !valid_sha256(&config.carry_decision_fingerprint)
    {
        return Err("signal payload config identity is invalid");
    }
    let universe = universe.ok_or("signal payload has no universe identity")?;
    if universe.environment != expected_environment
        || universe.endpoint.is_empty()
        || universe.snapshot_ts_ms <= 0
        || universe.available_at_ms < universe.snapshot_ts_ms
        || !valid_sha256(&universe.artifact_sha256)
        || !valid_sha256(&universe.file_sha256)
        || universe.symbols.iter().any(|symbol| !valid_symbol(symbol))
        || universe
            .long_symbols
            .iter()
            .any(|symbol| !valid_symbol(symbol))
        || universe
            .carry_symbols
            .iter()
            .any(|symbol| !valid_symbol(symbol))
    {
        return Err("signal payload universe identity is invalid");
    }
    Ok(())
}

/// One target plus the leverage the position planner deliberately does
/// not carry. Native adapters join leverage back onto opening steps by symbol.
#[derive(Clone, Debug, PartialEq)]
pub struct PlannedTarget {
    pub target: Target,
    pub leverage: f64,
}

/// Complete market/account facts passed to the pure position planner.
#[derive(Clone, Debug, Default)]
pub struct PlannerFacts {
    pub held: BTreeMap<String, Held>,
    pub prices: BTreeMap<String, f64>,
    pub rules: BTreeMap<String, InstrumentRule>,
}

impl PlannerFacts {
    pub fn held_symbols(&self) -> Vec<String> {
        self.held.keys().cloned().collect()
    }
}

impl SymbolFacts for PlannerFacts {
    fn held(&self, symbol: &str) -> Option<Held> {
        self.held.get(symbol).copied()
    }

    fn price(&self, symbol: &str) -> Option<f64> {
        self.prices.get(symbol).copied()
    }

    fn rule(&self, symbol: &str) -> Option<InstrumentRule> {
        self.rules.get(symbol).copied()
    }
}

/// One exact planner step, enriched only with fields the position planner does
/// not own. `leverage` is present only for position-growing work.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct OrderEffect {
    pub step: Step,
    pub leverage: Option<f64>,
    pub tag: &'static str,
}

/// Durable cross-sleeve event emitted by CARRY and consumed by Exodus.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CarryPresettlementFire {
    pub event_id: String,
    pub environment: String,
    pub source_profile: String,
    pub source_config_id: String,
    pub decision_ts_ms: i64,
    pub fired_ts_ms: i64,
    pub settlement_ts_ms: i64,
    pub symbol: String,
    pub mark_px: Option<f64>,
    pub carry_side: Option<String>,
    pub carry_qty: Option<f64>,
}

/// Effects are already ordered. The adapter must not regroup them.
#[derive(Clone, Debug, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Effect {
    PersistCheckpoint {
        symbol: String,
        config_fingerprint: String,
        payload: Vec<u8>,
    },
    AppendCarryFire(CarryPresettlementFire),
    ConsumeSignal {
        source: String,
        sequence: u64,
        observation_id: String,
    },
    ConsumeCarryFire {
        source_strategy_name: String,
        event_id: String,
    },
    ConsumeRuntimeControl {
        request_id: String,
    },
    CancelOwned {
        symbol: String,
        client_order_id: String,
    },
    Order(OrderEffect),
}

#[derive(Clone, Debug, Default, Serialize, PartialEq)]
pub struct ExecutionOutput {
    pub effects: Vec<Effect>,
    pub skipped: Vec<Skipped>,
}

pub fn order_effects(
    steps: Vec<Step>,
    targets: &[PlannedTarget],
    tag: &'static str,
) -> Vec<Effect> {
    let leverage_by_symbol: BTreeMap<&str, f64> = targets
        .iter()
        .map(|target| (target.target.symbol.as_str(), target.leverage))
        .collect();
    steps
        .into_iter()
        .map(|step| {
            let leverage = match &step {
                Step::Enter { symbol, .. }
                | Step::Resize {
                    symbol,
                    reduce_only: false,
                    ..
                } => leverage_by_symbol.get(symbol.as_str()).copied(),
                Step::Exit { .. }
                | Step::Resize {
                    reduce_only: true, ..
                }
                | Step::Restop { .. } => None,
            };
            Effect::Order(OrderEffect {
                step,
                leverage,
                tag,
            })
        })
        .collect()
}

/// Stable JSON fingerprint. `serde_json::Map` is key-sorted without the
/// preserve-order feature, so the resulting bytes match canonical object-key
/// ordering and do not depend on Rust field declaration order.
pub fn config_fingerprint<T: Serialize>(config: &T) -> String {
    let value = serde_json::to_value(config).expect("directional config must serialize");
    hex_digest(&serde_json::to_vec(&value).expect("directional config JSON must serialize"))
}

pub fn checkpoint_payload<T: Serialize>(state: &T) -> Vec<u8> {
    let value = serde_json::to_value(state).expect("directional state must serialize");
    serde_json::to_vec(&value).expect("directional state JSON must serialize")
}

pub fn hex_digest(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

pub fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub fn valid_symbol(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
}

/// Stable identity of public feature construction. Runtime cadence, retry,
/// timeout, destination names, and account-operational settings are excluded.
pub fn signal_feature_contract_sha256(signal_json: &[u8], sleeve: &str) -> Result<String, String> {
    let value: serde_json::Value = serde_json::from_slice(signal_json)
        .map_err(|error| format!("signal config JSON: {error}"))?;
    let required = |path: &[&str]| -> Result<serde_json::Value, String> {
        let mut current = &value;
        for key in path {
            current = current
                .get(*key)
                .ok_or_else(|| format!("signal config is missing {}", path.join(".")))?;
        }
        Ok(current.clone())
    };
    let feature_physics = match sleeve {
        "long" => required(&["long", "features"]),
        "carry" => required(&["carry_feature_physics"]),
        _ => Err("signal feature sleeve must be long or carry".to_owned()),
    }?;
    let contract = serde_json::json!({
        "schema_version": required(&["schema_version"])? ,
        "kind": required(&["kind"])? ,
        "routing_source": required(&["routing", "source"])? ,
        "sources": required(&["sources"])? ,
        "public_market_realm": required(&["live", "public_market_realm"])? ,
        "sleeve": sleeve,
        "feature_physics": feature_physics,
    });
    let bytes = serde_json::to_vec(&contract).map_err(|error| error.to_string())?;
    Ok(hex_digest(&bytes))
}

pub fn signed(side: Side, qty: f64) -> f64 {
    match side {
        Side::Buy => qty,
        Side::Sell => -qty,
    }
}

/// Translate already-decided effects into the engine's action language. No
/// gate, sizing rule, or retry decision belongs here.
pub fn emit_effects(
    effects: Vec<Effect>,
    strategy: StrategyId,
    cross_event_destination: Option<&str>,
    entry_work: Option<WorkPolicy>,
    ctx: &mut dyn StrategyCtx,
) -> Result<(), &'static str> {
    for effect in effects {
        match effect {
            Effect::PersistCheckpoint {
                config_fingerprint,
                payload,
                ..
            } => ctx.emit(Action::SetStrategyGlobalCheckpoint {
                strategy,
                checkpoint: StrategyCheckpoint {
                    schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
                    decision_fingerprint: config_fingerprint,
                    payload,
                },
            }),
            Effect::AppendCarryFire(event) => {
                let destination_name =
                    cross_event_destination.ok_or("CARRY event destination is not configured")?;
                let destination = ctx
                    .strategy_id(destination_name)
                    .ok_or("CARRY event destination strategy is absent")?;
                let event_id = event.event_id.clone();
                let payload = checkpoint_payload(&event);
                ctx.emit(Action::PublishStrategyEvent {
                    event: StrategyEvent {
                        source: strategy,
                        destination,
                        kind: "carry_presettlement_fire".to_owned(),
                        event_id,
                        payload,
                    },
                });
            }
            Effect::ConsumeSignal {
                source,
                sequence,
                observation_id,
            } => ctx.emit(Action::ConsumeSignalObservation {
                strategy,
                source,
                sequence,
                observation_id,
            }),
            Effect::ConsumeCarryFire {
                source_strategy_name,
                event_id,
            } => {
                let source = ctx
                    .strategy_id(&source_strategy_name)
                    .ok_or("CARRY source strategy is absent")?;
                ctx.emit(Action::ConsumeStrategyEvent {
                    source,
                    destination: strategy,
                    event_id,
                });
            }
            Effect::ConsumeRuntimeControl { request_id } => {
                ctx.emit(Action::ConsumeRuntimeControl {
                    strategy,
                    request_id,
                });
            }
            Effect::CancelOwned {
                symbol,
                client_order_id,
            } => {
                let symbol = ctx
                    .symbol_id(&symbol)
                    .ok_or("native cancel symbol was not admitted")?;
                ctx.cancel(symbol, &client_order_id);
            }
            Effect::Order(order) => {
                let symbol_name = order.step.symbol();
                let symbol = ctx
                    .symbol_id(symbol_name)
                    .ok_or("native order symbol was not admitted")?;
                let decided_ns = ctx.now_ns();
                match order.step {
                    Step::Restop { stop_px, .. } => ctx.emit(Action::SetStop {
                        symbol,
                        trigger_px: stop_px,
                    }),
                    Step::Enter {
                        side, qty, stop_px, ..
                    } => ctx.place(Intent {
                        strategy,
                        symbol,
                        side,
                        qty,
                        kind: OrderKind::Market,
                        stop: Some(StopSpec {
                            trigger_px: stop_px,
                        }),
                        reduce_only: false,
                        tag: order.tag.to_owned(),
                        decided_ns,
                        work: entry_work,
                        leverage: order.leverage,
                    }),
                    Step::Exit { side, qty, .. } => ctx.place(Intent {
                        strategy,
                        symbol,
                        side,
                        qty,
                        kind: OrderKind::Market,
                        stop: None,
                        reduce_only: true,
                        tag: order.tag.to_owned(),
                        decided_ns,
                        work: None,
                        leverage: None,
                    }),
                    Step::Resize {
                        side,
                        qty,
                        reduce_only,
                        stop_px,
                        ..
                    } => ctx.place(Intent {
                        strategy,
                        symbol,
                        side,
                        qty,
                        kind: OrderKind::Market,
                        stop: stop_px.map(|trigger_px| StopSpec { trigger_px }),
                        reduce_only,
                        tag: order.tag.to_owned(),
                        decided_ns,
                        work: (!reduce_only).then_some(entry_work).flatten(),
                        leverage: (!reduce_only).then_some(order.leverage).flatten(),
                    }),
                }
            }
        }
    }
    Ok(())
}

/// Drive one sleeve to zero while its durable flatten request is pending.
/// The checkpoint always precedes cancellation, exit, and acknowledgement.
pub struct FlattenExecutionInput<'a> {
    pub config_fingerprint: String,
    pub facts: &'a PlannerFacts,
    pub owned_opening_order_ids: &'a BTreeMap<String, Vec<String>>,
    pub now_ms: i64,
    pub request_id: &'a str,
    pub tag: &'static str,
    pub conclusively_flat: bool,
}

pub fn flatten_execution<T: Serialize>(
    state: &T,
    input: FlattenExecutionInput<'_>,
) -> (ExecutionOutput, bool) {
    let flat = input.conclusively_flat && input.owned_opening_order_ids.values().all(Vec::is_empty);
    let mut effects = vec![Effect::PersistCheckpoint {
        symbol: String::new(),
        config_fingerprint: input.config_fingerprint,
        payload: checkpoint_payload(state),
    }];
    if flat {
        effects.push(Effect::ConsumeRuntimeControl {
            request_id: input.request_id.to_owned(),
        });
        return (
            ExecutionOutput {
                effects,
                skipped: Vec::new(),
            },
            true,
        );
    }
    for (symbol, order_ids) in input.owned_opening_order_ids {
        for client_order_id in order_ids {
            effects.push(Effect::CancelOwned {
                symbol: symbol.clone(),
                client_order_id: client_order_id.clone(),
            });
        }
    }
    let planned = crate::position_plan::plan(
        &[],
        &input.facts.held_symbols(),
        input.facts,
        input.now_ms,
        input.now_ms.saturating_add(1),
        PlanRules::FLEET,
    );
    effects.extend(order_effects(planned.steps, &[], input.tag));
    (
        ExecutionOutput {
            effects,
            skipped: planned.skipped,
        },
        false,
    )
}

/// Gather the target planner's exact live picture for a known symbol set.
pub fn planner_facts(ctx: &dyn StrategyCtx, symbols: &BTreeSet<String>) -> PlannerFacts {
    let mut facts = PlannerFacts::default();
    for name in symbols {
        let Some(symbol) = ctx.symbol_id(name) else {
            continue;
        };
        let quote = ctx.quote(symbol);
        let ticker = ctx.ticker(symbol);
        let mark = if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
            (quote.bid_px + quote.ask_px) / 2.0
        } else if ticker.mark_px.is_finite() && ticker.mark_px > 0.0 {
            ticker.mark_px
        } else if ticker.last_px.is_finite() && ticker.last_px > 0.0 {
            ticker.last_px
        } else {
            0.0
        };
        if mark.is_finite() && mark > 0.0 {
            facts.prices.insert(name.clone(), mark);
        }
        if let Some(rule) = ctx.instrument(symbol) {
            facts.rules.insert(name.clone(), rule);
        }
        if ctx.foreign_position(symbol) {
            continue;
        }
        let venue = ctx.position(symbol);
        let signed_qty = venue
            .as_ref()
            .map(|position| signed(position.side, position.qty))
            .unwrap_or(0.0)
            + ctx.in_flight(symbol);
        if signed_qty.abs() <= f64::EPSILON {
            continue;
        }
        let entry_px = venue.as_ref().map_or(mark, |position| position.entry_px);
        let px = if mark > 0.0 { mark } else { entry_px };
        facts.held.insert(
            name.clone(),
            Held {
                qty: signed_qty.abs(),
                side: if signed_qty > 0.0 {
                    Side::Buy
                } else {
                    Side::Sell
                },
                px,
                entry_px,
                stop_px: venue.as_ref().map_or(0.0, |position| position.stop_px),
            },
        );
    }
    facts
}

/// Snapshot this strategy's live order ownership without leaking borrowed
/// engine rows into a reducer input.
pub fn owned_order_state(
    ctx: &dyn StrategyCtx,
) -> (BTreeSet<String>, BTreeMap<String, Vec<String>>) {
    let mut rows = Vec::new();
    ctx.resting(&mut rows);
    let mut working = BTreeSet::new();
    let mut opening = BTreeMap::<String, Vec<String>>::new();
    for row in rows {
        let Some(symbol) = ctx.symbol_name(row.symbol) else {
            continue;
        };
        working.insert(symbol.to_owned());
        if !row.reduce_only {
            opening
                .entry(symbol.to_owned())
                .or_default()
                .push(row.client_order_id.to_owned());
        }
    }
    for ids in opening.values_mut() {
        ids.sort();
        ids.dedup();
    }
    (working, opening)
}

pub fn attributed_symbols(ctx: &dyn StrategyCtx) -> BTreeSet<String> {
    let mut positions = Vec::new();
    ctx.my_positions(&mut positions);
    positions
        .into_iter()
        .filter_map(|position| ctx.symbol_name(position.symbol).map(str::to_owned))
        .collect()
}

pub fn attributed_exposure_is_flat(ctx: &dyn StrategyCtx, symbols: &BTreeSet<String>) -> bool {
    symbols.iter().all(|name| {
        let Some(symbol) = ctx.symbol_id(name) else {
            return false;
        };
        let venue_flat = ctx
            .position(symbol)
            .is_none_or(|position| position.qty.abs() <= f64::EPSILON);
        venue_flat && ctx.in_flight(symbol).abs() <= f64::EPSILON
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Serialize)]
    struct Config {
        z: u8,
        a: u8,
    }

    #[test]
    fn fingerprint_is_canonical_and_lowercase() {
        let fingerprint = config_fingerprint(&Config { z: 2, a: 1 });
        assert!(valid_sha256(&fingerprint));
        assert_eq!(fingerprint, hex_digest(br#"{"a":1,"z":2}"#),);
    }

    #[test]
    fn invalid_hashes_and_symbols_fail_closed() {
        assert!(!valid_sha256(&"A".repeat(64)));
        assert!(!valid_sha256("abc"));
        assert!(valid_symbol("BTCUSDT"));
        assert!(!valid_symbol("btcUSDT"));
        assert!(!valid_symbol("BTC-USDT"));
    }

    #[test]
    fn flatten_checkpoints_before_exit_and_acknowledges_only_when_flat() {
        let mut facts = PlannerFacts::default();
        facts.held.insert(
            "BTCUSDT".into(),
            Held {
                qty: 1.0,
                side: Side::Buy,
                px: 10.0,
                entry_px: 10.0,
                stop_px: 8.0,
            },
        );
        let (active, flat) = flatten_execution(
            &serde_json::json!({"schema_version": 1}),
            FlattenExecutionInput {
                config_fingerprint: "a".repeat(64),
                facts: &facts,
                owned_opening_order_ids: &BTreeMap::new(),
                now_ms: 1,
                request_id: "flatten-1",
                tag: "test-flatten",
                conclusively_flat: false,
            },
        );
        assert!(!flat);
        assert!(matches!(
            active.effects.as_slice(),
            [
                Effect::PersistCheckpoint { .. },
                Effect::Order(OrderEffect {
                    step: Step::Exit { .. },
                    ..
                })
            ]
        ));

        let (done, flat) = flatten_execution(
            &serde_json::json!({"schema_version": 1}),
            FlattenExecutionInput {
                config_fingerprint: "a".repeat(64),
                facts: &PlannerFacts::default(),
                owned_opening_order_ids: &BTreeMap::new(),
                now_ms: 1,
                request_id: "flatten-1",
                tag: "test-flatten",
                conclusively_flat: true,
            },
        );
        assert!(flat);
        assert!(matches!(
            done.effects.as_slice(),
            [
                Effect::PersistCheckpoint { .. },
                Effect::ConsumeRuntimeControl { request_id }
            ] if request_id == "flatten-1"
        ));
    }
}
