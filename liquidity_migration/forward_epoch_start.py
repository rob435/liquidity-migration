"""Pure receipt rules for the prospective forward execution epoch.

Live collection is intentionally kept in ``scripts/freeze_forward_epoch_start.py``.
This module owns deterministic schedules, comparator validation, and canonical
self-hashed receipts so those rules remain unit-testable without systemd.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .artifact_snapshot import read_stable_file
from .deterministic_serialization import canonical_json


FORWARD_EPOCH_ID = "prospective-runtime-parity-execution-epoch-2026-07-18"
START_RECEIPT_SCHEMA_VERSION = 1
START_RECEIPT_KIND = "prospective_forward_execution_epoch_start"
COMPARATOR_VERIFICATION_SCHEMA_VERSION = 1
COMPARATOR_VERIFICATION_KIND = "integrated_runtime_comparator_verification"
HOUR_NS = 3_600_000_000_000
DAY_NS = 86_400_000_000_000
MINIMUM_PUBLICATION_LEAD_NS = 300_000_000_000
EXPECTED_POST22_AMENDMENTS_SHA256 = "c10fd87913310ff6b6f6bade08e532f9b58a966b3ad5ee95dddfb2c79b70d13e"
EXPECTED_PERSISTENT_UNITS = (
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
)
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def utc_iso(ts_ns: int) -> str:
    if type(ts_ns) is not int or ts_ns <= 0:
        raise ValueError("UTC timestamp must be a positive integer nanosecond value")
    value = dt.datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=dt.timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def next_whole_utc_hour(ts_ns: int) -> int:
    if type(ts_ns) is not int or ts_ns <= 0:
        raise ValueError("start observation timestamp must be positive")
    return (ts_ns // HOUR_NS + 1) * HOUR_NS


def self_hash(payload: Mapping[str, Any], *, field: str = "artifact_sha256") -> str:
    material = dict(payload)
    if field not in material:
        raise ValueError(f"self-hashed payload is missing {field}")
    material[field] = ""
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _canonical_object(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain one object")
    if canonical_json(payload) + b"\n" != data:
        raise ValueError(f"{label} is not canonical newline-terminated JSON")
    return payload


def validate_integrated_comparator_payload(
    payload: Mapping[str, Any],
    *,
    expected_commit: str,
) -> dict[str, Any]:
    """Validate the frozen structural gates needed by the forward start."""

    if not _FULL_COMMIT.fullmatch(expected_commit):
        raise ValueError("expected comparator commit must be a full lowercase Git commit")
    if payload.get("status") != "pass":
        raise ValueError("integrated comparator did not pass")
    if payload.get("code_commit") != expected_commit or payload.get("git_dirty") is not False:
        raise ValueError("integrated comparator does not bind the clean installed commit")
    claimed_payload_hash = payload.get("receipt_payload_sha256")
    if not isinstance(claimed_payload_hash, str) or not _SHA256.fullmatch(claimed_payload_hash):
        raise ValueError("integrated comparator payload hash is invalid")
    unhashed = dict(payload)
    unhashed.pop("receipt_payload_sha256", None)
    if hashlib.sha256(canonical_json(unhashed)).hexdigest() != claimed_payload_hash:
        raise ValueError("integrated comparator payload hash does not verify")

    registered = payload.get("registered_inputs")
    if not isinstance(registered, Mapping):
        raise ValueError("integrated comparator registered inputs are missing")
    post22 = registered.get("post22_amendments")
    if not isinstance(post22, Mapping) or post22.get("sha256") != EXPECTED_POST22_AMENDMENTS_SHA256:
        raise ValueError("integrated comparator does not bind Amendments 23--24")

    structural = payload.get("structural")
    traces = payload.get("trace_counts")
    prefix = payload.get("performance_refactor_prefix_equivalence")
    if not isinstance(structural, Mapping) or not isinstance(traces, Mapping):
        raise ValueError("integrated comparator structural evidence is missing")
    required_structural = {
        "cycles": 29_449,
        "requests": 911,
        "account_events": 12_812,
        "venue_lifecycle_registered_events": 235,
        "venue_lifecycle_observed_events": 235,
        "btc_risk_reconciliation_error": 0,
        "rejected_strict_risk_reduction_batches": 0,
    }
    for key, expected in required_structural.items():
        if structural.get(key) != expected:
            raise ValueError(f"integrated comparator structural {key} is {structural.get(key)!r}, expected {expected}")
    if structural.get("final_flat") is not True or payload.get("journal_verified") is not True:
        raise ValueError("integrated comparator journal or terminal-flat gate failed")
    required_traces = {
        "cycles": 29_449,
        "continuous_gate_rows": 2_868_677,
        "long_funnel_rows": 665_562,
        "request_intents": 963,
        "requests": 911,
        "source_decisions": 847,
        "rejected_requests": 0,
    }
    for key, expected in required_traces.items():
        if traces.get(key) != expected:
            raise ValueError(f"integrated comparator trace {key} is {traces.get(key)!r}, expected {expected}")
    if not isinstance(prefix, Mapping) or prefix.get("status") != "pass":
        raise ValueError("integrated comparator repair-aware prefix gate failed")
    return {
        "status": "pass",
        "code_commit": expected_commit,
        "receipt_payload_sha256": claimed_payload_hash,
        "cycles": structural["cycles"],
        "requests": structural["requests"],
        "account_events": structural["account_events"],
        "final_flat": True,
        "journal_verified": True,
        "rejected_strict_risk_reduction_batches": 0,
        "btc_risk_reconciliation_error": 0,
        "venue_lifecycle_events": 235,
        "continuous_gate_rows": traces["continuous_gate_rows"],
        "long_funnel_rows": traces["long_funnel_rows"],
        "source_decisions": traces["source_decisions"],
        "post22_amendments_sha256": EXPECTED_POST22_AMENDMENTS_SHA256,
    }


def load_integrated_comparator_receipt(
    path: str | Path,
    *,
    expected_commit: str,
    require_mode: int | None = None,
    require_owner: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = read_stable_file(
        path,
        label="integrated comparator receipt",
        reject_empty=True,
        require_mode=require_mode,
        require_owner=require_owner,
        require_single_link=True,
        max_bytes=2 * 1024 * 1024,
    )
    payload = _canonical_object(snapshot.data, label="integrated comparator receipt")
    summary = validate_integrated_comparator_payload(payload, expected_commit=expected_commit)
    summary.update(
        {
            "path": str(snapshot.path),
            "bytes": snapshot.size,
            "sha256": snapshot.sha256,
        }
    )
    return payload, summary


def verify_integrated_comparator_files(
    payload: Mapping[str, Any],
    *,
    output_root: str | Path,
) -> dict[str, Any]:
    root = Path(output_root).expanduser().resolve(strict=True)
    files = payload.get("files")
    if not isinstance(files, Mapping) or not files:
        raise ValueError("integrated comparator file manifest is missing")
    identities: list[dict[str, Any]] = []
    total_bytes = 0
    for relative, raw_identity in sorted(files.items(), key=lambda item: str(item[0])):
        if not isinstance(relative, str) or not isinstance(raw_identity, Mapping):
            raise ValueError("integrated comparator file manifest is malformed")
        logical = Path(*relative.split("/"))
        if logical.is_absolute() or any(part in {"", ".", ".."} for part in logical.parts):
            raise ValueError(f"integrated comparator path is unsafe: {relative!r}")
        candidate = root / logical
        snapshot = read_stable_file(
            candidate,
            label=f"integrated comparator artifact {relative}",
            require_single_link=True,
        )
        expected_bytes = raw_identity.get("bytes")
        expected_sha256 = raw_identity.get("sha256")
        if snapshot.size != expected_bytes or snapshot.sha256 != expected_sha256:
            raise ValueError(f"integrated comparator artifact identity failed: {relative}")
        identity = {
            "path": relative,
            "bytes": snapshot.size,
            "sha256": snapshot.sha256,
        }
        identities.append(identity)
        total_bytes += snapshot.size
    logical_sha256 = hashlib.sha256(canonical_json({"files": identities})).hexdigest()
    return {
        "files": len(identities),
        "bytes": total_bytes,
        "logical_sha256": logical_sha256,
    }


def build_comparator_verification_receipt(
    *,
    created_ts_ns: int,
    comparator_summary: Mapping[str, Any],
    file_verification: Mapping[str, Any],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": COMPARATOR_VERIFICATION_SCHEMA_VERSION,
        "kind": COMPARATOR_VERIFICATION_KIND,
        "created_ts_ns": created_ts_ns,
        "created_at_utc": utc_iso(created_ts_ns),
        "status": "pass",
        "comparator": dict(comparator_summary),
        "file_verification": dict(file_verification),
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = self_hash(receipt)
    validate_comparator_verification_payload(receipt)
    return receipt


def validate_comparator_verification_payload(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "kind",
        "created_ts_ns",
        "created_at_utc",
        "status",
        "comparator",
        "file_verification",
        "artifact_sha256",
    }
    if set(payload) != expected:
        raise ValueError("comparator verification receipt fields are invalid")
    if (
        payload.get("schema_version") != COMPARATOR_VERIFICATION_SCHEMA_VERSION
        or payload.get("kind") != COMPARATOR_VERIFICATION_KIND
        or payload.get("status") != "pass"
        or payload.get("artifact_sha256") != self_hash(payload)
    ):
        raise ValueError("comparator verification receipt is invalid")
    comparator = payload.get("comparator")
    files = payload.get("file_verification")
    if not isinstance(comparator, Mapping) or not isinstance(files, Mapping):
        raise ValueError("comparator verification evidence is malformed")
    if comparator.get("status") != "pass" or comparator.get("final_flat") is not True:
        raise ValueError("comparator verification does not attest a passing flat run")
    if type(files.get("files")) is not int or int(files["files"]) <= 0:
        raise ValueError("comparator verification has no files")
    if type(files.get("bytes")) is not int or int(files["bytes"]) <= 0:
        raise ValueError("comparator verification has no bytes")
    if not isinstance(files.get("logical_sha256"), str) or not _SHA256.fullmatch(str(files["logical_sha256"])):
        raise ValueError("comparator verification logical hash is invalid")


def load_comparator_verification_receipt(
    path: str | Path,
    *,
    require_mode: int | None = None,
    require_owner: bool = False,
) -> dict[str, Any]:
    snapshot = read_stable_file(
        path,
        label="integrated comparator verification receipt",
        reject_empty=True,
        require_mode=require_mode,
        require_owner=require_owner,
        require_single_link=True,
        max_bytes=128 * 1024,
    )
    payload = _canonical_object(
        snapshot.data,
        label="integrated comparator verification receipt",
    )
    validate_comparator_verification_payload(payload)
    return {
        **payload,
        "receipt_path": str(snapshot.path),
        "receipt_bytes": snapshot.size,
        "receipt_sha256": snapshot.sha256,
    }


def build_start_receipt(
    *,
    collected_ts_ns: int,
    installed_commit: str,
    contracts: Mapping[str, Any],
    operational_authorization: Mapping[str, Any],
    integrated_comparator: Mapping[str, Any],
    systemd_units: Mapping[str, Any],
    account_owner_readiness: Mapping[str, Any],
    account_journals: Mapping[str, Any],
    producer_cycle_health: Mapping[str, Any],
    strategy_event_tapes: Mapping[str, Any],
    target_capture_tapes: Mapping[str, Any],
    legacy_target_capture_tapes: Mapping[str, Any],
    market_capture_prefixes: Mapping[str, Any],
    intent_queues: Mapping[str, Any],
    analysis_boundary: Mapping[str, Any],
) -> dict[str, Any]:
    start_ts_ns = next_whole_utc_hour(collected_ts_ns)
    if start_ts_ns - collected_ts_ns < MINIMUM_PUBLICATION_LEAD_NS:
        raise ValueError("forward start receipt has less than five minutes of publication lead")
    calibration_end_ts_ns = start_ts_ns + 45 * DAY_NS
    epoch_end_ts_ns = start_ts_ns + 90 * DAY_NS
    receipt: dict[str, Any] = {
        "schema_version": START_RECEIPT_SCHEMA_VERSION,
        "kind": START_RECEIPT_KIND,
        "epoch_id": FORWARD_EPOCH_ID,
        "status": "registered_waiting_for_start",
        "collected_ts_ns": collected_ts_ns,
        "collected_at_utc": utc_iso(collected_ts_ns),
        "start_ts_ns": start_ts_ns,
        "start_at_utc": utc_iso(start_ts_ns),
        "calibration_end_ts_ns": calibration_end_ts_ns,
        "calibration_end_at_utc": utc_iso(calibration_end_ts_ns),
        "validation_start_ts_ns": calibration_end_ts_ns,
        "validation_start_at_utc": utc_iso(calibration_end_ts_ns),
        "epoch_end_ts_ns": epoch_end_ts_ns,
        "epoch_end_at_utc": utc_iso(epoch_end_ts_ns),
        "installed_commit": installed_commit,
        "contracts": dict(contracts),
        "operational_authorization": dict(operational_authorization),
        "integrated_comparator": dict(integrated_comparator),
        "systemd_units": dict(systemd_units),
        "account_owner_readiness": dict(account_owner_readiness),
        "account_journals": dict(account_journals),
        "producer_cycle_health": dict(producer_cycle_health),
        "strategy_event_tapes": dict(strategy_event_tapes),
        "target_capture_tapes": dict(target_capture_tapes),
        "legacy_target_capture_tapes": dict(legacy_target_capture_tapes),
        "market_capture_prefixes": dict(market_capture_prefixes),
        "intent_queues": dict(intent_queues),
        "analysis_boundary": dict(analysis_boundary),
        "inherited_state_policy": "retain_and_classify_never_reset_flatten_cancel_or_erase",
        "change_point_policy": "append_only_hash_chained_no_clock_reset_or_extension",
        "explicit_non_conclusions": [
            "forward observations have not yet validated structural parity",
            "TCA calibration and validation have not occurred",
            "no return, alpha, strategy thesis, or profile-promotion conclusion",
            "no mainnet, capital, or real-money authority",
        ],
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = self_hash(receipt)
    validate_start_receipt_payload(receipt)
    return receipt


def _require_keyset(value: object, expected: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} keys are invalid")
    return value


def validate_start_receipt_payload(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "kind",
        "epoch_id",
        "status",
        "collected_ts_ns",
        "collected_at_utc",
        "start_ts_ns",
        "start_at_utc",
        "calibration_end_ts_ns",
        "calibration_end_at_utc",
        "validation_start_ts_ns",
        "validation_start_at_utc",
        "epoch_end_ts_ns",
        "epoch_end_at_utc",
        "installed_commit",
        "contracts",
        "operational_authorization",
        "integrated_comparator",
        "systemd_units",
        "account_owner_readiness",
        "account_journals",
        "producer_cycle_health",
        "strategy_event_tapes",
        "target_capture_tapes",
        "legacy_target_capture_tapes",
        "market_capture_prefixes",
        "intent_queues",
        "analysis_boundary",
        "inherited_state_policy",
        "change_point_policy",
        "explicit_non_conclusions",
        "artifact_sha256",
    }
    if set(payload) != expected:
        raise ValueError("forward start receipt fields are invalid")
    if (
        payload.get("schema_version") != START_RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != START_RECEIPT_KIND
        or payload.get("epoch_id") != FORWARD_EPOCH_ID
        or payload.get("status") != "registered_waiting_for_start"
    ):
        raise ValueError("forward start receipt identity is invalid")
    commit = payload.get("installed_commit")
    if not isinstance(commit, str) or not _FULL_COMMIT.fullmatch(commit):
        raise ValueError("forward start receipt installed commit is invalid")
    collected = payload.get("collected_ts_ns")
    if type(collected) is not int:
        raise ValueError("forward start collection timestamp is invalid")
    start = next_whole_utc_hour(collected)
    if start - collected < MINIMUM_PUBLICATION_LEAD_NS or payload.get("start_ts_ns") != start:
        raise ValueError("forward start boundary is invalid")
    calibration_end = start + 45 * DAY_NS
    epoch_end = start + 90 * DAY_NS
    if (
        payload.get("calibration_end_ts_ns") != calibration_end
        or payload.get("validation_start_ts_ns") != calibration_end
        or payload.get("epoch_end_ts_ns") != epoch_end
        or payload.get("artifact_sha256") != self_hash(payload)
    ):
        raise ValueError("forward start receipt schedule or self hash is invalid")
    for key, timestamp in (
        ("collected_at_utc", collected),
        ("start_at_utc", start),
        ("calibration_end_at_utc", calibration_end),
        ("validation_start_at_utc", calibration_end),
        ("epoch_end_at_utc", epoch_end),
    ):
        if payload.get(key) != utc_iso(timestamp):
            raise ValueError(f"forward start receipt {key} is invalid")

    contracts = payload.get("contracts")
    if not isinstance(contracts, Mapping):
        raise ValueError("forward start contract identities are missing")
    post22 = contracts.get("post22_amendments")
    if not isinstance(post22, Mapping) or post22.get("sha256") != EXPECTED_POST22_AMENDMENTS_SHA256:
        raise ValueError("forward start receipt does not bind Amendments 23--24")
    authorization = payload.get("operational_authorization")
    comparator = payload.get("integrated_comparator")
    if not isinstance(authorization, Mapping) or not isinstance(comparator, Mapping):
        raise ValueError("forward start runtime identities are missing")
    if authorization.get("authorized_commit") != commit or authorization.get("profile") != "operational":
        raise ValueError("forward start operational authorization is invalid")
    if comparator.get("code_commit") != commit or comparator.get("status") != "pass":
        raise ValueError("forward start integrated comparator is invalid")
    if comparator.get("final_flat") is not True or comparator.get("journal_verified") is not True:
        raise ValueError("forward start comparator lacks flat verified evidence")

    units = _require_keyset(
        payload.get("systemd_units"),
        set(EXPECTED_PERSISTENT_UNITS),
        label="forward start systemd units",
    )
    for unit, state in units.items():
        if not isinstance(state, Mapping) or (
            state.get("LoadState") != "loaded"
            or state.get("ActiveState") != "active"
            or state.get("SubState") != "running"
            or state.get("UnitFileState") != "enabled"
            or state.get("NRestarts") != 0
            or not isinstance(state.get("InvocationID"), str)
        ):
            raise ValueError(f"forward start unit is not one clean running generation: {unit}")

    _require_keyset(
        payload.get("account_owner_readiness"),
        {"demo", "paper"},
        label="forward start account owners",
    )
    _require_keyset(
        payload.get("account_journals"),
        {"demo", "paper"},
        label="forward start journals",
    )
    _require_keyset(
        payload.get("producer_cycle_health"),
        {"demo_long", "demo_continuous", "paper_long", "paper_continuous"},
        label="forward start producer health",
    )
    _require_keyset(
        payload.get("strategy_event_tapes"),
        {"demo_long", "demo_continuous", "paper_long", "paper_continuous"},
        label="forward start scheduling tapes",
    )
    target_tapes = _require_keyset(
        payload.get("target_capture_tapes"),
        {"demo", "paper"},
        label="forward start target tapes",
    )
    for environment, boundary in target_tapes.items():
        if not isinstance(boundary, Mapping) or boundary.get("verified") is not True:
            raise ValueError(f"forward start target tape is unverified: {environment}")
    _require_keyset(
        payload.get("legacy_target_capture_tapes"),
        {"demo_long", "demo_continuous", "paper_long", "paper_continuous"},
        label="forward start legacy target tapes",
    )
    _require_keyset(
        payload.get("market_capture_prefixes"),
        {"demo", "paper"},
        label="forward start market captures",
    )
    _require_keyset(
        payload.get("intent_queues"),
        {"demo", "paper"},
        label="forward start intent queues",
    )
    analysis = payload.get("analysis_boundary")
    if not isinstance(analysis, Mapping) or set(analysis) != {
        "analysis",
        "structural",
        "tca",
    }:
        raise ValueError("forward start analysis boundary is invalid")
    if any(not isinstance(value, Mapping) or value.get("exists") is not False for value in analysis.values()):
        raise ValueError("forward analysis exists before the registered start")


def validate_start_receipt_bytes(data: bytes) -> dict[str, Any]:
    payload = _canonical_object(data, label="forward start receipt")
    validate_start_receipt_payload(payload)
    return payload


def contract_identities(repo_root: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(repo_root).expanduser().resolve(strict=True)
    directory = root / "docs" / "preregistration"
    names = {
        "base_contract": "prospective_runtime_parity_execution_epoch_2026-07-18.md",
        "amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_amendments.md",
        "post17_amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_post17_amendments.md",
        "post18_amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_post18_amendments.md",
        "post19_amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_post19_amendments.md",
        "post20_amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_post20_amendments.md",
        "post21_amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_post21_amendments.md",
        "post22_amendments": "prospective_runtime_parity_execution_epoch_2026-07-18_post22_amendments.md",
    }
    output: dict[str, dict[str, Any]] = {}
    for key, name in names.items():
        snapshot = read_stable_file(
            directory / name,
            label=f"prospective contract {key}",
            reject_empty=True,
            require_single_link=True,
            max_bytes=1024 * 1024,
        )
        output[key] = {
            "path": str(snapshot.path),
            "bytes": snapshot.size,
            "sha256": snapshot.sha256,
        }
    if output["post22_amendments"]["sha256"] != EXPECTED_POST22_AMENDMENTS_SHA256:
        raise ValueError("Amendments 23--24 changed after registration")
    return output


def logical_name_hash(names: Sequence[str]) -> str:
    normalized = sorted(str(value) for value in names)
    if len(set(normalized)) != len(normalized):
        raise ValueError("logical name inventory contains duplicates")
    return hashlib.sha256(canonical_json({"names": normalized})).hexdigest()


__all__ = [
    "COMPARATOR_VERIFICATION_KIND",
    "EXPECTED_PERSISTENT_UNITS",
    "FORWARD_EPOCH_ID",
    "MINIMUM_PUBLICATION_LEAD_NS",
    "START_RECEIPT_KIND",
    "build_comparator_verification_receipt",
    "build_start_receipt",
    "contract_identities",
    "load_comparator_verification_receipt",
    "load_integrated_comparator_receipt",
    "logical_name_hash",
    "next_whole_utc_hour",
    "self_hash",
    "utc_iso",
    "validate_comparator_verification_payload",
    "validate_integrated_comparator_payload",
    "validate_start_receipt_bytes",
    "validate_start_receipt_payload",
    "verify_integrated_comparator_files",
]
