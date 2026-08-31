use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use serde_json::Value;

use crate::config::{is_sha256, sha256_hex};
use crate::model::{UniverseIdentity, UniverseMode};
use crate::normalize::normalized_symbol;
use crate::worker::WorkerError;

pub fn load_candidate_universe(
    path: impl AsRef<Path>,
    expected_environment: &str,
) -> Result<UniverseIdentity, WorkerError> {
    let path = path.as_ref();
    let metadata =
        fs::symlink_metadata(path).map_err(|e| WorkerError::io("stat candidate universe", e))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(WorkerError::config(
            "candidate universe must be a non-symlink regular file",
        ));
    }
    validate_access(&metadata)?;
    let bytes = fs::read(path).map_err(|e| WorkerError::io("read candidate universe", e))?;
    let file_sha256 = sha256_hex(&bytes);
    let mut payload: Value = serde_json::from_slice(&bytes)
        .map_err(|e| WorkerError::json("parse candidate universe", e))?;
    let object = payload
        .as_object()
        .ok_or_else(|| WorkerError::config("candidate universe must be an object"))?;
    if object.get("schema_version").and_then(Value::as_u64) != Some(5)
        || object.get("kind").and_then(Value::as_str)
            != Some("account_execution_candidate_universe")
        || object.get("strategy_domain").and_then(Value::as_str) != Some("crypto_perpetuals")
    {
        return Err(WorkerError::config(
            "candidate universe identity or schema is invalid",
        ));
    }
    let environment = required_string(object, "environment")?;
    if environment != expected_environment {
        return Err(WorkerError::config(format!(
            "candidate universe is for {environment}, not {expected_environment}"
        )));
    }
    let expected_endpoint = match environment.as_str() {
        "demo" => "api-demo.bybit.com",
        "mainnet" => "api.bybit.com",
        _ => return Err(WorkerError::config("candidate universe realm is invalid")),
    };
    let endpoint = required_string(object, "endpoint")?;
    if endpoint != expected_endpoint {
        return Err(WorkerError::config(
            "candidate universe endpoint disagrees with its realm",
        ));
    }
    let artifact_sha256 = required_string(object, "artifact_sha256")?;
    if !is_sha256(&artifact_sha256) {
        return Err(WorkerError::config(
            "candidate universe artifact hash is invalid",
        ));
    }
    payload
        .as_object_mut()
        .expect("checked above")
        .insert("artifact_sha256".to_owned(), Value::String(String::new()));
    let canonical = serde_json::to_vec(&payload)
        .map_err(|e| WorkerError::json("canonicalize candidate universe", e))?;
    if sha256_hex(&canonical) != artifact_sha256 {
        return Err(WorkerError::config(
            "candidate universe self-hash does not match canonical content",
        ));
    }
    let object = payload.as_object().expect("checked above");
    let symbols = string_list(object, "symbols")?;
    let symbol_count = object
        .get("symbol_count")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| WorkerError::config("candidate symbol_count is invalid"))?;
    if symbols.len() != symbol_count {
        return Err(WorkerError::config(
            "candidate symbol_count does not match symbols",
        ));
    }
    let profiles = object
        .get("profile_eligible_symbols")
        .and_then(Value::as_object)
        .ok_or_else(|| WorkerError::config("candidate profile populations are missing"))?;
    if profiles.len() != 2 || !profiles.contains_key("long") || !profiles.contains_key("carry") {
        return Err(WorkerError::config(
            "candidate universe must have exactly long and carry profiles",
        ));
    }
    let long_symbols = value_string_list(&profiles["long"], "long profile")?;
    let carry_symbols = value_string_list(&profiles["carry"], "carry profile")?;
    let snapshot_ts_ns = object
        .get("snapshot_ts_ns")
        .and_then(Value::as_i64)
        .filter(|value| *value > 0)
        .ok_or_else(|| WorkerError::config("candidate snapshot_ts_ns is invalid"))?;
    let available_ns = object
        .get("snapshot_completed_ts_ns")
        .and_then(Value::as_i64)
        .unwrap_or(snapshot_ts_ns);
    if available_ns < snapshot_ts_ns {
        return Err(WorkerError::config(
            "candidate acquisition interval is backwards",
        ));
    }
    Ok(UniverseIdentity {
        mode: UniverseMode::Pit,
        environment,
        endpoint,
        snapshot_ts_ms: snapshot_ts_ns / 1_000_000,
        available_at_ms: available_ns / 1_000_000,
        artifact_sha256,
        file_sha256,
        symbols,
        long_symbols,
        carry_symbols,
    })
}

fn required_string(
    object: &serde_json::Map<String, Value>,
    key: &str,
) -> Result<String, WorkerError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| WorkerError::config(format!("candidate {key} is invalid")))
}

fn string_list(
    object: &serde_json::Map<String, Value>,
    key: &str,
) -> Result<Vec<String>, WorkerError> {
    let value = object
        .get(key)
        .ok_or_else(|| WorkerError::config(format!("candidate lacks {key}")))?;
    value_string_list(value, key)
}

fn value_string_list(value: &Value, label: &str) -> Result<Vec<String>, WorkerError> {
    let raw = value
        .as_array()
        .ok_or_else(|| WorkerError::config(format!("candidate {label} is not a list")))?;
    let values = raw
        .iter()
        .map(|row| {
            row.as_str()
                .ok_or_else(|| WorkerError::config(format!("candidate {label} has non-string")))
                .and_then(normalized_symbol)
        })
        .collect::<Result<Vec<_>, _>>()?;
    let sorted: BTreeMap<&str, ()> = values.iter().map(|value| (value.as_str(), ())).collect();
    if values.is_empty()
        || sorted.len() != values.len()
        || sorted.keys().copied().ne(values.iter().map(String::as_str))
    {
        return Err(WorkerError::config(format!(
            "candidate {label} must be non-empty, unique, and sorted"
        )));
    }
    Ok(values)
}

#[cfg(unix)]
fn validate_access(metadata: &fs::Metadata) -> Result<(), WorkerError> {
    use std::os::unix::fs::MetadataExt;
    let uid = unsafe { libc_geteuid() };
    let mode = metadata.mode() & 0o777;
    if (metadata.uid() == 0 && mode == 0o640) || (metadata.uid() == uid && mode & 0o077 == 0) {
        Ok(())
    } else {
        Err(WorkerError::config(
            "candidate universe must be owner-private or root-owned mode 0640",
        ))
    }
}

#[cfg(unix)]
unsafe fn libc_geteuid() -> u32 {
    unsafe extern "C" {
        fn geteuid() -> u32;
    }
    unsafe { geteuid() }
}

#[cfg(not(unix))]
fn validate_access(_: &fs::Metadata) -> Result<(), WorkerError> {
    Ok(())
}
