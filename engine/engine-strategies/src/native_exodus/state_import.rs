//! Strict stopped-state import for the retired Exodus state and CARRY event
//! tape. This is a cutover codec, not a runtime strategy or file watcher.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{StrategyImportSource, TranslatedStrategyEvent, TranslatedStrategyState};
use serde_json::{json, Map, Value};

use super::plan::{OpenRecord, SleeveState, StrategyConfig};
use crate::native_carry::plan::carry_event_id;
use crate::native_common::{
    checkpoint_payload, hex_digest, valid_sha256, valid_symbol, CarryPresettlementFire,
    DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION, EXODUS_SLEEVE_NAME,
};

pub const SOURCE_FORMAT: &str = "exodus-state-v1-v4-event-tape-v1";
pub const SOURCE_FORMAT_IDENTITY_V2: &str = "exodus-state-v1-v4-event-tape-v1-identity-v2";
const STATE_NAME: &str = "exodus_state.json";
const EVENT_KIND: &str = "presettlement_exit";
const ENGINE_EVENT_KIND: &str = "carry_presettlement_fire";

#[derive(Clone, Copy, Debug)]
pub struct LegacyImportIdentity<'a> {
    pub venue: &'a str,
    pub realm: &'a str,
    pub account_user_id: &'a str,
}

#[derive(Clone, Debug)]
struct LegacyPaths {
    event_path: String,
    target_book_path: String,
    engine_heartbeat_path: String,
}

pub fn translate(
    config: &StrategyConfig,
    identity: LegacyImportIdentity<'_>,
    source_format: &str,
    sources: &[StrategyImportSource],
) -> Result<TranslatedStrategyState, String> {
    let (legacy_paths, state_index) = match source_format {
        SOURCE_FORMAT => {
            require_source_names(sources, &["carry_events", "identity", "state"])?;
            (None, 2)
        }
        SOURCE_FORMAT_IDENTITY_V2 => {
            require_source_names(
                sources,
                &["carry_events", "identity", "legacy_paths", "state"],
            )?;
            (Some(parse_legacy_paths(&sources[2].bytes)?), 3)
        }
        _ => {
            return Err(format!(
                "Exodus does not support source format {source_format:?}"
            ))
        }
    };
    if identity.venue != "bybit"
        || identity.realm != config.environment
        || identity.account_user_id.trim().is_empty()
    {
        return Err("Exodus import account realm does not match the native config".to_owned());
    }

    let state_value = parse_canonical_object(&sources[state_index].bytes, "Exodus state")?;
    let state = parse_state(&state_value)?;
    validate_identity(
        config,
        identity.account_user_id,
        &sources[1].bytes,
        &state,
        legacy_paths.as_ref(),
    )?;
    let tape = parse_event_tape(&sources[0].bytes, config, identity.realm)?;
    let tape_ids = tape
        .iter()
        .map(|fire| fire.event_id.as_str())
        .collect::<BTreeSet<_>>();
    if state
        .consumed_event_ids
        .iter()
        .any(|event_id| !tape_ids.contains(event_id.as_str()))
    {
        return Err("Exodus consumed state names an event absent from the CARRY tape".to_owned());
    }
    let pending_events = tape
        .into_iter()
        .filter(|fire| !state.consumed_event_ids.contains(&fire.event_id))
        .map(|fire| TranslatedStrategyEvent {
            source_strategy: config.carry_sleeve_name.clone(),
            destination_strategy: EXODUS_SLEEVE_NAME.to_owned(),
            kind: ENGINE_EVENT_KIND.to_owned(),
            event_id: fire.event_id.clone(),
            payload: checkpoint_payload(&fire),
        })
        .collect();
    state.validate().map_err(str::to_owned)?;
    Ok(TranslatedStrategyState {
        checkpoint_payload: checkpoint_payload(&state),
        pending_events,
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
            "Exodus import requires exact sorted sources: {}",
            expected.join(", ")
        ));
    }
    Ok(())
}

fn parse_legacy_paths(bytes: &[u8]) -> Result<LegacyPaths, String> {
    let value = parse_canonical_object(bytes, "Exodus legacy paths")?;
    let object = value
        .as_object()
        .ok_or_else(|| "Exodus legacy paths must be an object".to_owned())?;
    require_keys(
        object,
        &[
            "engine_heartbeat_path",
            "event_path",
            "schema_version",
            "target_book_path",
        ],
        "Exodus legacy paths",
    )?;
    if exact_u64(object, "schema_version", "Exodus legacy paths")? != 1 {
        return Err("unsupported Exodus legacy-path schema".to_owned());
    }
    let path = |name: &str| -> Result<String, String> {
        let value = exact_string(object, name, "Exodus legacy paths")?;
        if !std::path::Path::new(value).is_absolute() {
            return Err(format!("Exodus legacy {name} must be absolute"));
        }
        Ok(value.to_owned())
    };
    Ok(LegacyPaths {
        event_path: path("event_path")?,
        target_book_path: path("target_book_path")?,
        engine_heartbeat_path: path("engine_heartbeat_path")?,
    })
}

fn parse_state(value: &Value) -> Result<SleeveState, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "Exodus state must contain an object".to_owned())?;
    let keys = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let (record_schema, consumed, closed) = if keys == BTreeSet::from(["open"]) {
        (1_u64, BTreeSet::new(), BTreeMap::new())
    } else if keys == BTreeSet::from(["open", "schema_version"]) {
        let schema = exact_u64(object, "schema_version", "Exodus state")?;
        if !matches!(schema, 1 | 2) {
            return Err("unsupported Exodus producer state schema".to_owned());
        }
        (schema, BTreeSet::new(), BTreeMap::new())
    } else if keys == BTreeSet::from(["consumed_event_ids", "open", "schema_version"]) {
        if exact_u64(object, "schema_version", "Exodus state")? != 3 {
            return Err("unsupported Exodus producer state schema".to_owned());
        }
        (2, parse_consumed(object)?, BTreeMap::new())
    } else if keys
        == BTreeSet::from([
            "consumed_event_ids",
            "entry_closed_ts_ms_by_symbol",
            "open",
            "schema_version",
        ])
    {
        if exact_u64(object, "schema_version", "Exodus state")? != 4 {
            return Err("unsupported Exodus producer state schema".to_owned());
        }
        (2, parse_consumed(object)?, parse_closed(object)?)
    } else {
        return Err("Exodus state has unexpected or missing fields".to_owned());
    };
    let rows = object
        .get("open")
        .and_then(Value::as_array)
        .ok_or_else(|| "Exodus state open must be an array".to_owned())?;
    let mut open = BTreeMap::new();
    for (index, row) in rows.iter().enumerate() {
        let row = row
            .as_object()
            .ok_or_else(|| format!("Exodus state row {index} is not an object"))?;
        let expected = if record_schema == 1 {
            BTreeSet::from(["fired_ts_ms", "notional_usdt", "settlement_ts_ms", "symbol"])
        } else {
            BTreeSet::from([
                "fired_ts_ms",
                "notional_usdt",
                "settlement_ts_ms",
                "symbol",
                "target_qty",
            ])
        };
        if row.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected {
            return Err(format!("Exodus state row {index} has an invalid shape"));
        }
        let symbol = exact_string(row, "symbol", "Exodus state row")?.to_owned();
        let notional_usdt = exact_f64(row, "notional_usdt", "Exodus state row")?;
        let settlement_ts_ms = exact_i64(row, "settlement_ts_ms", "Exodus state row")?;
        let fired_ts_ms = exact_i64(row, "fired_ts_ms", "Exodus state row")?;
        let target_qty = if record_schema == 1 || row.get("target_qty") == Some(&Value::Null) {
            None
        } else {
            Some(exact_f64(row, "target_qty", "Exodus state row")?)
        };
        let record = OpenRecord {
            symbol: symbol.clone(),
            notional_usdt,
            settlement_ts_ms,
            fired_ts_ms,
            target_qty,
        };
        if open.insert(symbol.clone(), record).is_some() {
            return Err(format!("Exodus state row {index} repeats {symbol}"));
        }
    }
    if closed
        .iter()
        .any(|(symbol, ts)| !open.contains_key(symbol) || *ts <= 0)
    {
        return Err("Exodus closed-entry state has an invalid row".to_owned());
    }
    Ok(SleeveState {
        schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
        open,
        consumed_event_ids: consumed,
        entry_closed_ts_ms_by_symbol: closed,
        refused_entries: BTreeSet::new(),
        entry_retry_after_ms: BTreeMap::new(),
    })
}

fn parse_consumed(object: &Map<String, Value>) -> Result<BTreeSet<String>, String> {
    let rows = object
        .get("consumed_event_ids")
        .and_then(Value::as_array)
        .ok_or_else(|| "Exodus consumed_event_ids must be an array".to_owned())?;
    let ordered = rows
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| "Exodus consumed_event_ids must contain strings".to_owned())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let sorted = ordered.iter().cloned().collect::<BTreeSet<_>>();
    if sorted.len() != ordered.len()
        || sorted
            .iter()
            .map(String::as_str)
            .ne(ordered.iter().map(String::as_str))
        || sorted
            .iter()
            .any(|value| !value.starts_with("carry-presettlement-") || value.len() != 84)
    {
        return Err("Exodus consumed event ids are not unique canonical ids".to_owned());
    }
    Ok(sorted)
}

fn parse_closed(object: &Map<String, Value>) -> Result<BTreeMap<String, i64>, String> {
    let raw = object
        .get("entry_closed_ts_ms_by_symbol")
        .and_then(Value::as_object)
        .ok_or_else(|| "Exodus entry_closed_ts_ms_by_symbol must be an object".to_owned())?;
    raw.iter()
        .map(|(symbol, value)| {
            let ts = value
                .as_i64()
                .filter(|value| *value > 0)
                .ok_or_else(|| "Exodus closed-entry timestamp is invalid".to_owned())?;
            if !valid_symbol(symbol) {
                return Err("Exodus closed-entry symbol is invalid".to_owned());
            }
            Ok((symbol.clone(), ts))
        })
        .collect()
}

fn validate_identity(
    config: &StrategyConfig,
    account_user_id: &str,
    bytes: &[u8],
    state: &SleeveState,
    legacy_paths: Option<&LegacyPaths>,
) -> Result<(), String> {
    let value = parse_canonical_object(bytes, "Exodus state identity")?;
    let object = value
        .as_object()
        .ok_or_else(|| "Exodus state identity must be an object".to_owned())?;
    let schema = exact_u64(object, "schema_version", "Exodus state identity")?;
    let common = BTreeSet::from([
        "genesis_source",
        "legacy_path",
        "legacy_sha256",
        "schema_version",
        "state_path",
    ]);
    let mut expected = common.clone();
    match schema {
        1 => {}
        2 => {
            expected.insert("effective_config_sha256");
        }
        3 => {
            expected.insert("state_contract_sha256");
        }
        _ => return Err("unsupported Exodus state identity schema".to_owned()),
    }
    if object.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected {
        return Err("Exodus state identity has unexpected or missing fields".to_owned());
    }
    let state_path = exact_string(object, "state_path", "Exodus state identity")?;
    if !std::path::Path::new(state_path).is_absolute()
        || std::path::Path::new(state_path)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(STATE_NAME)
    {
        return Err("Exodus state identity names an invalid state path".to_owned());
    }
    if !matches!(
        exact_string(object, "genesis_source", "Exodus state identity")?,
        "adopted_owned" | "legacy_import" | "initialized_empty"
    ) {
        return Err("Exodus state identity has an invalid genesis source".to_owned());
    }
    let legacy_path = exact_string(object, "legacy_path", "Exodus state identity")?;
    let legacy_sha = exact_string(object, "legacy_sha256", "Exodus state identity")?;
    if (legacy_path.is_empty() != legacy_sha.is_empty())
        || (!legacy_path.is_empty()
            && (!std::path::Path::new(legacy_path).is_absolute() || !valid_sha256(legacy_sha)))
    {
        return Err("Exodus state identity has invalid legacy provenance".to_owned());
    }
    match schema {
        1 => {
            if legacy_paths.is_some() {
                return Err("Exodus identity v1 must use the ordinary source format".to_owned());
            }
            if !state.open.is_empty()
                || !state.consumed_event_ids.is_empty()
                || !state.entry_closed_ts_ms_by_symbol.is_empty()
            {
                return Err(
                    "Exodus identity v1 cannot attribute nonempty state to this account".to_owned(),
                );
            }
        }
        2 => {
            let digest = exact_string(object, "effective_config_sha256", "Exodus state identity")?;
            if !valid_sha256(digest) {
                return Err("Exodus identity v2 has an invalid digest".to_owned());
            }
            let paths = legacy_paths.ok_or(
                "Exodus identity v2 requires the identity-v2 source format and legacy_paths source",
            )?;
            if digest != legacy_v2_state_contract_sha256(config, account_user_id, paths) {
                return Err(
                    "Exodus identity v2 belongs to a different config or account".to_owned(),
                );
            }
        }
        3 => {
            if legacy_paths.is_some() {
                return Err("Exodus identity v3 must use the ordinary source format".to_owned());
            }
            let digest = exact_string(object, "state_contract_sha256", "Exodus state identity")?;
            if digest != state_contract_sha256(config, account_user_id) {
                return Err("Exodus state belongs to a different config or account".to_owned());
            }
        }
        _ => unreachable!(),
    }
    Ok(())
}

fn legacy_v2_state_contract_sha256(
    config: &StrategyConfig,
    account_user_id: &str,
    paths: &LegacyPaths,
) -> String {
    let material = json!({
        "profile_name": config.profile_name,
        "rule": config.rule,
        "environment": config.environment,
        "event_path": paths.event_path,
        "target_book_path": paths.target_book_path,
        "engine_heartbeat_path": paths.engine_heartbeat_path,
        "expected_account_user_id": account_user_id,
        "entry_leverage": config.entry_leverage,
    });
    hex_digest(&serde_json::to_vec(&material).expect("Exodus legacy identity JSON"))
}

fn state_contract_sha256(config: &StrategyConfig, account_user_id: &str) -> String {
    let material = json!({
        "decision": {
            "profile_name": config.profile_name,
            "rule": config.rule,
            "environment": config.environment,
            "entry_leverage": config.entry_leverage,
        },
        "expected_account_user_id": account_user_id,
    });
    hex_digest(&serde_json::to_vec(&material).expect("Exodus identity JSON"))
}

fn parse_event_tape(
    bytes: &[u8],
    config: &StrategyConfig,
    realm: &str,
) -> Result<Vec<CarryPresettlementFire>, String> {
    if !bytes.is_empty() && !bytes.ends_with(b"\n") {
        return Err("CARRY event tape has an unterminated final row".to_owned());
    }
    let mut prior_hash = hex_digest(b"liquidity-migration-strategy-event-tape-v1");
    let mut previous_order: Option<(i64, String, u64, String)> = None;
    let mut generic_ids = BTreeSet::new();
    let mut semantic = BTreeMap::<String, CarryPresettlementFire>::new();
    for (line_index, raw) in bytes.split_inclusive(|byte| *byte == b'\n').enumerate() {
        if raw.is_empty() {
            continue;
        }
        let line = line_index + 1;
        let value = parse_canonical_object(raw, &format!("CARRY event tape row {line}"))?;
        let row = value
            .as_object()
            .ok_or_else(|| format!("CARRY event tape row {line} is not an object"))?;
        require_keys(
            row,
            &["event", "prior_tape_hash", "schema_version", "tape_hash"],
            "CARRY event tape row",
        )?;
        if exact_u64(row, "schema_version", "CARRY event tape row")? != 1
            || exact_string(row, "prior_tape_hash", "CARRY event tape row")? != prior_hash
        {
            return Err(format!("CARRY event tape chain breaks at row {line}"));
        }
        let event = row
            .get("event")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("CARRY event tape row {line} has no event object"))?;
        require_keys(
            event,
            &[
                "event_id",
                "event_ts_ns",
                "ingest_ts_ns",
                "kind",
                "payload",
                "source",
                "source_sequence",
            ],
            "CARRY strategy event",
        )?;
        let event_ts_ns = exact_i64(event, "event_ts_ns", "CARRY strategy event")?;
        let ingest_ts_ns = exact_i64(event, "ingest_ts_ns", "CARRY strategy event")?;
        let source_sequence = exact_u64(event, "source_sequence", "CARRY strategy event")?;
        let kind = exact_string(event, "kind", "CARRY strategy event")?;
        let source = exact_string(event, "source", "CARRY strategy event")?;
        let event_id = exact_string(event, "event_id", "CARRY strategy event")?;
        if event_ts_ns <= 0
            || ingest_ts_ns <= 0
            || kind != EVENT_KIND
            || source != format!("carry_hold:{realm}")
        {
            return Err(format!("CARRY strategy event at row {line} is invalid"));
        }
        let id_material = json!({
            "event_ts_ns": event_ts_ns,
            "kind": kind,
            "payload": event.get("payload").expect("required payload"),
            "source": source,
            "source_sequence": source_sequence,
        });
        let expected_event_id = format!(
            "strategy-event-{}",
            hex_digest(&serde_json::to_vec(&id_material).expect("strategy event id JSON"))
        );
        if event_id != expected_event_id || !generic_ids.insert(event_id.to_owned()) {
            return Err(format!("CARRY strategy event id is invalid at row {line}"));
        }
        let order = (
            event_ts_ns,
            source.to_owned(),
            source_sequence,
            event_id.to_owned(),
        );
        if previous_order
            .as_ref()
            .is_some_and(|previous| order < *previous)
        {
            return Err(format!("CARRY event tape moves backward at row {line}"));
        }
        previous_order = Some(order);
        let wrapper = json!({"event": Value::Object(event.clone())});
        let mut hash_material = prior_hash.as_bytes().to_vec();
        hash_material
            .extend_from_slice(&serde_json::to_vec(&wrapper).expect("strategy tape hash JSON"));
        let next_hash = hex_digest(&hash_material);
        if exact_string(row, "tape_hash", "CARRY event tape row")? != next_hash {
            return Err(format!("CARRY event tape hash is invalid at row {line}"));
        }
        prior_hash = next_hash;
        let fire = parse_fire(event.get("payload").expect("required CARRY event payload"))?;
        if fire.environment != config.environment {
            return Err("CARRY event tape belongs to a different realm".to_owned());
        }
        if semantic.insert(fire.event_id.clone(), fire).is_some() {
            return Err("CARRY event tape repeats one semantic fire".to_owned());
        }
    }
    Ok(semantic.into_values().collect())
}

fn parse_fire(value: &Value) -> Result<CarryPresettlementFire, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "CARRY pre-settlement payload must be an object".to_owned())?;
    require_keys(
        object,
        &[
            "carry_avg_entry_px",
            "carry_qty",
            "carry_side",
            "decision_ts_ms",
            "environment",
            "event_id",
            "fired_ts_ms",
            "mark_px",
            "running_rate",
            "schema_version",
            "settlement_ts_ms",
            "source_config_id",
            "source_profile",
            "symbol",
        ],
        "CARRY pre-settlement payload",
    )?;
    if exact_u64(object, "schema_version", "CARRY pre-settlement payload")? != 1 {
        return Err("unsupported CARRY pre-settlement payload schema".to_owned());
    }
    let optional_positive = |name: &str| -> Result<Option<f64>, String> {
        if object.get(name) == Some(&Value::Null) {
            return Ok(None);
        }
        let value = exact_f64(object, name, "CARRY pre-settlement payload")?;
        if value <= 0.0 {
            return Err(format!("CARRY pre-settlement {name} is invalid"));
        }
        Ok(Some(value))
    };
    let carry_side = match object.get("carry_side") {
        Some(Value::Null) => None,
        Some(Value::String(value)) if matches!(value.as_str(), "long" | "short") => {
            Some(value.clone())
        }
        _ => return Err("CARRY pre-settlement carry_side is invalid".to_owned()),
    };
    let carry_qty = optional_positive("carry_qty")?;
    let carry_avg_entry_px = optional_positive("carry_avg_entry_px")?;
    if carry_side.is_none() != carry_qty.is_none()
        || carry_side.is_none() != carry_avg_entry_px.is_none()
    {
        return Err("CARRY pre-settlement holding is incomplete".to_owned());
    }
    let decision_ts_ms = exact_i64(object, "decision_ts_ms", "CARRY pre-settlement payload")?;
    let fired_ts_ms = exact_i64(object, "fired_ts_ms", "CARRY pre-settlement payload")?;
    let settlement_ts_ms = exact_i64(object, "settlement_ts_ms", "CARRY pre-settlement payload")?;
    let environment = exact_string(object, "environment", "CARRY pre-settlement payload")?;
    let source_profile = exact_string(object, "source_profile", "CARRY pre-settlement payload")?;
    let source_config_id =
        exact_string(object, "source_config_id", "CARRY pre-settlement payload")?;
    let symbol = exact_string(object, "symbol", "CARRY pre-settlement payload")?;
    let running_rate = exact_f64(object, "running_rate", "CARRY pre-settlement payload")?;
    let mark_px = optional_positive("mark_px")?;
    let expected_id = carry_event_id(
        environment,
        source_config_id,
        decision_ts_ms,
        settlement_ts_ms,
        symbol,
    );
    if !valid_symbol(symbol)
        || environment.is_empty()
        || source_profile.is_empty()
        || source_config_id.is_empty()
        || decision_ts_ms <= 0
        || fired_ts_ms < decision_ts_ms
        || settlement_ts_ms <= fired_ts_ms
        || !running_rate.is_finite()
        || exact_string(object, "event_id", "CARRY pre-settlement payload")? != expected_id
    {
        return Err("CARRY pre-settlement payload is invalid".to_owned());
    }
    Ok(CarryPresettlementFire {
        event_id: expected_id,
        environment: environment.to_owned(),
        source_profile: source_profile.to_owned(),
        source_config_id: source_config_id.to_owned(),
        decision_ts_ms,
        fired_ts_ms,
        settlement_ts_ms,
        symbol: symbol.to_owned(),
        mark_px,
        carry_side,
        carry_qty,
    })
}

fn parse_canonical_object(bytes: &[u8], label: &str) -> Result<Value, String> {
    if !bytes.ends_with(b"\n") {
        return Err(format!("{label} is not newline-terminated canonical JSON"));
    }
    let value: Value =
        serde_json::from_slice(bytes).map_err(|error| format!("{label}: {error}"))?;
    if !value.is_object() {
        return Err(format!("{label} must contain an object"));
    }
    let mut canonical = serde_json::to_vec(&value).map_err(|error| error.to_string())?;
    canonical.push(b'\n');
    if canonical != bytes {
        return Err(format!("{label} is not canonical JSON"));
    }
    Ok(value)
}

fn require_keys(object: &Map<String, Value>, expected: &[&str], label: &str) -> Result<(), String> {
    if object.keys().map(String::as_str).collect::<BTreeSet<_>>()
        != expected.iter().copied().collect::<BTreeSet<_>>()
    {
        return Err(format!("{label} has unexpected or missing fields"));
    }
    Ok(())
}

fn exact_string<'a>(
    object: &'a Map<String, Value>,
    name: &str,
    label: &str,
) -> Result<&'a str, String> {
    object
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{label} {name} must be a string"))
}

fn exact_i64(object: &Map<String, Value>, name: &str, label: &str) -> Result<i64, String> {
    object
        .get(name)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{label} {name} must be an integer"))
}

fn exact_u64(object: &Map<String, Value>, name: &str, label: &str) -> Result<u64, String> {
    object
        .get(name)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{label} {name} must be a nonnegative integer"))
}

fn exact_f64(object: &Map<String, Value>, name: &str, label: &str) -> Result<f64, String> {
    object
        .get(name)
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
        .ok_or_else(|| format!("{label} {name} must be a finite number"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native_exodus::plan::RuleConfig;

    fn config() -> StrategyConfig {
        StrategyConfig {
            schema_version: 1,
            profile_name: "v1".into(),
            environment: "demo".into(),
            rule_sha256: "a".repeat(64),
            operational_profile_sha256: "b".repeat(64),
            entries_enabled: true,
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

    fn canonical(value: Value) -> Vec<u8> {
        let mut bytes = serde_json::to_vec(&value).expect("json");
        bytes.push(b'\n');
        bytes
    }

    fn identity(config: &StrategyConfig, user: &str) -> Vec<u8> {
        canonical(json!({
            "schema_version": 3,
            "state_path": "/var/lib/liquidity-migration/exodus_state.json",
            "genesis_source": "initialized_empty",
            "legacy_path": "",
            "legacy_sha256": "",
            "state_contract_sha256": state_contract_sha256(config, user),
        }))
    }

    fn legacy_paths() -> LegacyPaths {
        LegacyPaths {
            event_path: "/var/lib/liquidity-migration/exodus/carry_presettlement_events.jsonl"
                .into(),
            target_book_path: "/var/lib/liquidity-migration/targets/exodus-demo.json".into(),
            engine_heartbeat_path: "/run/liquidity-migration/engine-demo.json".into(),
        }
    }

    fn legacy_paths_bytes(paths: &LegacyPaths) -> Vec<u8> {
        canonical(json!({
            "schema_version": 1,
            "event_path": paths.event_path,
            "target_book_path": paths.target_book_path,
            "engine_heartbeat_path": paths.engine_heartbeat_path,
        }))
    }

    fn event_tape(fire: &CarryPresettlementFire) -> Vec<u8> {
        let payload = json!({
            "schema_version": 1,
            "event_id": fire.event_id,
            "environment": fire.environment,
            "source_profile": fire.source_profile,
            "source_config_id": fire.source_config_id,
            "decision_ts_ms": fire.decision_ts_ms,
            "fired_ts_ms": fire.fired_ts_ms,
            "settlement_ts_ms": fire.settlement_ts_ms,
            "symbol": fire.symbol,
            "running_rate": -0.0001,
            "mark_px": fire.mark_px,
            "carry_side": fire.carry_side,
            "carry_qty": fire.carry_qty,
            "carry_avg_entry_px": 10.0,
        });
        let event_ts_ns = fire.fired_ts_ms * 1_000_000;
        let id_material = json!({
            "event_ts_ns": event_ts_ns,
            "kind": EVENT_KIND,
            "payload": payload,
            "source": "carry_hold:demo",
            "source_sequence": 1,
        });
        let event = json!({
            "event_id": format!(
                "strategy-event-{}",
                hex_digest(&serde_json::to_vec(&id_material).expect("event id")),
            ),
            "event_ts_ns": event_ts_ns,
            "ingest_ts_ns": event_ts_ns + 1,
            "kind": EVENT_KIND,
            "payload": payload,
            "source": "carry_hold:demo",
            "source_sequence": 1,
        });
        let prior = hex_digest(b"liquidity-migration-strategy-event-tape-v1");
        let mut hash_material = prior.as_bytes().to_vec();
        hash_material.extend_from_slice(
            &serde_json::to_vec(&json!({"event": event})).expect("tape hash material"),
        );
        canonical(json!({
            "schema_version": 1,
            "event": event,
            "prior_tape_hash": prior,
            "tape_hash": hex_digest(&hash_material),
        }))
    }

    #[test]
    fn translates_empty_v4_with_a_verified_empty_tape() {
        let config = config();
        let translated = translate(
            &config,
            LegacyImportIdentity {
                venue: "bybit",
                realm: "demo",
                account_user_id: "account-1",
            },
            SOURCE_FORMAT,
            &[
                StrategyImportSource {
                    name: "carry_events".into(),
                    bytes: Vec::new(),
                },
                StrategyImportSource {
                    name: "identity".into(),
                    bytes: identity(&config, "account-1"),
                },
                StrategyImportSource {
                    name: "state".into(),
                    bytes: canonical(json!({
                        "schema_version": 4,
                        "consumed_event_ids": [],
                        "entry_closed_ts_ms_by_symbol": {},
                        "open": [],
                    })),
                },
            ],
        )
        .expect("translate");
        let state: SleeveState =
            serde_json::from_slice(&translated.checkpoint_payload).expect("checkpoint");
        assert!(state.open.is_empty());
        assert!(translated.pending_events.is_empty());
    }

    #[test]
    fn empty_event_tape_is_valid_but_a_present_malformed_tape_is_rejected() {
        let config = config();
        assert!(parse_event_tape(b"", &config, "demo")
            .expect("absent-is-empty staging bytes")
            .is_empty());
        assert!(parse_event_tape(b"{}\n", &config, "demo").is_err());
    }

    #[test]
    fn refuses_wrong_account_noncanonical_and_nonempty_v1_identity() {
        let config = config();
        let sources = |identity_bytes: Vec<u8>, state: Vec<u8>| {
            vec![
                StrategyImportSource {
                    name: "carry_events".into(),
                    bytes: Vec::new(),
                },
                StrategyImportSource {
                    name: "identity".into(),
                    bytes: identity_bytes,
                },
                StrategyImportSource {
                    name: "state".into(),
                    bytes: state,
                },
            ]
        };
        assert!(translate(
            &config,
            LegacyImportIdentity {
                venue: "bybit",
                realm: "demo",
                account_user_id: "wrong-account",
            },
            SOURCE_FORMAT,
            &sources(
                identity(&config, "account-1"),
                canonical(json!({
                    "schema_version": 4,
                    "consumed_event_ids": [],
                    "entry_closed_ts_ms_by_symbol": {},
                    "open": [],
                })),
            ),
        )
        .is_err());
        assert!(parse_canonical_object(b"{\"open\": []}\n", "state").is_err());
        let v1_identity = canonical(json!({
            "schema_version": 1,
            "state_path": "/var/lib/liquidity-migration/exodus_state.json",
            "genesis_source": "adopted_owned",
            "legacy_path": "",
            "legacy_sha256": "",
        }));
        assert!(translate(
            &config,
            LegacyImportIdentity {
                venue: "bybit",
                realm: "demo",
                account_user_id: "account-1",
            },
            SOURCE_FORMAT,
            &sources(
                v1_identity,
                canonical(json!({
                    "schema_version": 2,
                    "open": [{
                        "symbol": "AUSDT",
                        "notional_usdt": 10.0,
                        "settlement_ts_ms": 200,
                        "fired_ts_ms": 100,
                        "target_qty": 1.0,
                    }],
                })),
            ),
        )
        .is_err());
    }

    #[test]
    fn translates_nonempty_identity_v2_with_hash_bound_legacy_paths() {
        let config = config();
        let paths = legacy_paths();
        let identity = canonical(json!({
            "schema_version": 2,
            "state_path": "/var/lib/liquidity-migration/exodus_state.json",
            "genesis_source": "adopted_owned",
            "legacy_path": "",
            "legacy_sha256": "",
            "effective_config_sha256": legacy_v2_state_contract_sha256(
                &config,
                "account-1",
                &paths,
            ),
        }));
        let translated = translate(
            &config,
            LegacyImportIdentity {
                venue: "bybit",
                realm: "demo",
                account_user_id: "account-1",
            },
            SOURCE_FORMAT_IDENTITY_V2,
            &[
                StrategyImportSource {
                    name: "carry_events".into(),
                    bytes: Vec::new(),
                },
                StrategyImportSource {
                    name: "identity".into(),
                    bytes: identity,
                },
                StrategyImportSource {
                    name: "legacy_paths".into(),
                    bytes: legacy_paths_bytes(&paths),
                },
                StrategyImportSource {
                    name: "state".into(),
                    bytes: canonical(json!({
                        "schema_version": 2,
                        "open": [{
                            "symbol": "AUSDT",
                            "notional_usdt": 10.0,
                            "settlement_ts_ms": 200,
                            "fired_ts_ms": 100,
                            "target_qty": 1.0,
                        }],
                    })),
                },
            ],
        )
        .expect("v2 translation");
        let state: SleeveState =
            serde_json::from_slice(&translated.checkpoint_payload).expect("checkpoint");
        assert_eq!(state.open["AUSDT"].target_qty, Some(1.0));

        let mut wrong = legacy_paths();
        wrong.target_book_path.push_str(".wrong");
        assert_ne!(
            legacy_v2_state_contract_sha256(&config, "account-1", &wrong),
            legacy_v2_state_contract_sha256(&config, "account-1", &paths),
        );
    }

    #[test]
    fn imports_unconsumed_carry_fires_as_pending_engine_events() {
        let config = config();
        let fire = CarryPresettlementFire {
            event_id: carry_event_id(
                "demo",
                "lane2_carry_hold_v7",
                1_799_971_200_000,
                1_800_000_600_000,
                "AUSDT",
            ),
            environment: "demo".into(),
            source_profile: "carry_hold_v7_live_v1".into(),
            source_config_id: "lane2_carry_hold_v7".into(),
            decision_ts_ms: 1_799_971_200_000,
            fired_ts_ms: 1_800_000_000_000,
            settlement_ts_ms: 1_800_000_600_000,
            symbol: "AUSDT".into(),
            mark_px: Some(10.0),
            carry_side: Some("long".into()),
            carry_qty: Some(3.25),
        };
        let translated = translate(
            &config,
            LegacyImportIdentity {
                venue: "bybit",
                realm: "demo",
                account_user_id: "account-1",
            },
            SOURCE_FORMAT,
            &[
                StrategyImportSource {
                    name: "carry_events".into(),
                    bytes: event_tape(&fire),
                },
                StrategyImportSource {
                    name: "identity".into(),
                    bytes: identity(&config, "account-1"),
                },
                StrategyImportSource {
                    name: "state".into(),
                    bytes: canonical(json!({
                        "schema_version": 4,
                        "consumed_event_ids": [],
                        "entry_closed_ts_ms_by_symbol": {},
                        "open": [],
                    })),
                },
            ],
        )
        .expect("pending event translation");
        assert_eq!(translated.pending_events.len(), 1);
        let pending = &translated.pending_events[0];
        assert_eq!(pending.source_strategy, "carry");
        assert_eq!(pending.destination_strategy, "exodus");
        assert_eq!(pending.kind, ENGINE_EVENT_KIND);
        assert_eq!(pending.event_id, fire.event_id);
        assert_eq!(
            serde_json::from_slice::<CarryPresettlementFire>(&pending.payload).expect("fire"),
            fire,
        );
    }
}
