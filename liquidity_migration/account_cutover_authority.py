"""Commit- and evidence-bound authorization for the account-owner cutover.

The authorization is an operational control, not a digital signature and not
an automatic research verdict.  It makes the human gate decision explicit,
binds that decision to immutable evidence hashes, and prevents an authorization
from being silently reused on another host, commit, or maintenance window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .deterministic_serialization import canonical_json


ASSESSMENT_SCHEMA_VERSION = 1
AUTHORIZATION_SCHEMA_VERSION = 1
REVIEWED_EVIDENCE_SCHEMA_VERSION = 1
ASSESSMENT_KIND = "account_execution_cutover_assessment"
AUTHORIZATION_KIND = "account_execution_deploy_authorization"
REVIEWED_EVIDENCE_KIND = "account_execution_cutover_reviewed_evidence"
DEFAULT_AUTHORIZATION_LIFETIME_SECONDS = 24 * 60 * 60
MAX_AUTHORIZATION_LIFETIME_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_NS = 5 * 60 * 1_000_000_000
OWNER_HEALTH_AUTHORIZATION_MAX_AGE_NS = 5 * 60 * 1_000_000_000

REQUIRED_LIMITATIONS = frozenset(
    {
        "self_hash_is_not_a_signature",
        "authorization_is_demo_and_paper_only",
        "structural_parity_is_not_full_strategy_or_market_tape_parity",
        "operator_review_remains_responsible_for_non_machine-verifiable_claims",
    }
)

REQUIRED_GATE_ROLES: dict[str, frozenset[str]] = {
    "maintenance_topology_and_retired_authority_absence": frozenset(
        {"topology_inventory"}
    ),
    "fresh_demo_and_paper_epochs": frozenset({"reset_archive_receipt"}),
    "credentialed_demo_rule_probe": frozenset({"demo_rule_probe"}),
    "demo_owner_first_health": frozenset(
        {"demo_owner_health_snapshot", "demo_owner_start_sequence"}
    ),
    "actual_demo_target_order_fill_pnl_tape": frozenset(
        {"execution_twin_calibration", "venue_accounting_reconciliation"}
    ),
    "execution_twin_calibration": frozenset({"execution_twin_calibration"}),
    "paper_owner_first_health": frozenset(
        {"paper_owner_health_snapshot", "paper_owner_start_sequence"}
    ),
    "deterministic_cross_environment_replay_comparison": frozenset(
        {"kernel_parity", "event_clock_comparison"}
    ),
    "venue_pnl_and_funding_reconciliation": frozenset(
        {"venue_accounting_reconciliation"}
    ),
    "final_demo_flatness": frozenset({"venue_flatness_snapshot"}),
}

MACHINE_VALIDATED_ROLES = frozenset(
    {
        "demo_rule_probe",
        "demo_owner_health_snapshot",
        "execution_twin_calibration",
        "kernel_parity",
        "paper_owner_health_snapshot",
        "venue_accounting_reconciliation",
        "venue_flatness_snapshot",
    }
)
REVIEWED_EVIDENCE_ROLES = frozenset(
    role
    for roles in REQUIRED_GATE_ROLES.values()
    for role in roles
    if role not in MACHINE_VALIDATED_ROLES
)
ALL_EVIDENCE_ROLES = MACHINE_VALIDATED_ROLES | REVIEWED_EVIDENCE_ROLES


def _lower_sha256(value: Any, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _full_commit(value: Any, *, label: str = "authorized_commit") -> str:
    commit = str(value or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full 40-character lowercase Git commit")
    return commit


def _commit_prefix(value: Any) -> str:
    commit = str(value or "").lower()
    if not 7 <= len(commit) <= 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise ValueError("expected commit must be a 7-40 character hexadecimal commit id")
    return commit


def _strict_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        metadata = candidate.stat()
    except OSError as exc:
        raise ValueError(f"cannot stat {label} {candidate}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file: {candidate}")
    return candidate.resolve(strict=True)


def _file_identity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _machine_fingerprint(machine_id_path: str | Path) -> str:
    path = _strict_file(machine_id_path, label="machine-id file")
    machine_id = path.read_text(encoding="utf-8").strip()
    if not machine_id:
        raise ValueError("machine-id file is empty")
    return hashlib.sha256(machine_id.encode("utf-8")).hexdigest()


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must contain a JSON object")
    return dict(value)


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("cutover receipt write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def build_reviewed_evidence(
    *,
    role: str,
    claim: str,
    reviewed_by: str,
    source_paths: Sequence[str | Path],
    reviewed_ts_ns: int,
) -> dict[str, Any]:
    """Snapshot hashes for a gate whose sufficiency still needs human review."""

    if role not in REVIEWED_EVIDENCE_ROLES:
        raise ValueError(f"role {role!r} is not an operator-reviewed evidence role")
    if not claim.strip() or not reviewed_by.strip():
        raise ValueError("reviewed evidence requires a claim and reviewer")
    if reviewed_ts_ns <= 0:
        raise ValueError("reviewed evidence requires a positive timestamp")
    if not source_paths:
        raise ValueError("reviewed evidence requires at least one source file")
    sources = [
        _file_identity(_strict_file(path, label=f"{role} source"))
        for path in source_paths
    ]
    if len({source["path"] for source in sources}) != len(sources):
        raise ValueError("reviewed evidence source paths must be distinct")
    payload: dict[str, Any] = {
        "schema_version": REVIEWED_EVIDENCE_SCHEMA_VERSION,
        "kind": REVIEWED_EVIDENCE_KIND,
        "evidence_scope": role,
        "claim": claim.strip(),
        "reviewed_by": reviewed_by.strip(),
        "reviewed_ts_ns": int(reviewed_ts_ns),
        "review_type": "operator_attestation_with_source_hashes",
        "sources": sources,
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def verify_reviewed_evidence(
    receipt: Mapping[str, Any], *, expected_role: str
) -> dict[str, Any]:
    payload = dict(receipt)
    expected_fields = {
        "schema_version",
        "kind",
        "evidence_scope",
        "claim",
        "reviewed_by",
        "reviewed_ts_ns",
        "review_type",
        "sources",
        "artifact_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("reviewed evidence has unexpected or missing fields")
    if int(payload.get("schema_version") or 0) != REVIEWED_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported reviewed-evidence schema")
    if payload.get("kind") != REVIEWED_EVIDENCE_KIND:
        raise ValueError("reviewed evidence has the wrong kind")
    if payload.get("evidence_scope") != expected_role:
        raise ValueError(
            f"reviewed evidence scope is {payload.get('evidence_scope')!r}, expected {expected_role!r}"
        )
    if expected_role not in REVIEWED_EVIDENCE_ROLES:
        raise ValueError(f"role {expected_role!r} is not operator-reviewed")
    if payload.get("review_type") != "operator_attestation_with_source_hashes":
        raise ValueError("reviewed evidence has an unsupported review type")
    if not str(payload.get("claim") or "").strip() or not str(
        payload.get("reviewed_by") or ""
    ).strip():
        raise ValueError("reviewed evidence lacks a claim or reviewer")
    if int(payload.get("reviewed_ts_ns") or 0) <= 0:
        raise ValueError("reviewed evidence has an invalid timestamp")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("reviewed evidence lacks source identities")
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise ValueError(f"reviewed evidence source {index} must be an object")
        if set(source) != {"path", "size_bytes", "sha256"}:
            raise ValueError(f"reviewed evidence source {index} has invalid fields")
        _lower_sha256(source.get("sha256"), label=f"reviewed evidence source {index} hash")
        if not str(source.get("path") or "").startswith("/"):
            raise ValueError(f"reviewed evidence source {index} path must be absolute")
        if int(source.get("size_bytes") or -1) < 0:
            raise ValueError(f"reviewed evidence source {index} has an invalid size")
    if str(payload.get("artifact_sha256") or "") != _self_hash(payload):
        raise ValueError("reviewed evidence hash mismatch")
    return payload


def load_reviewed_evidence(path: str | Path, *, expected_role: str) -> dict[str, Any]:
    evidence_path = _strict_file(path, label=f"{expected_role} evidence")
    return verify_reviewed_evidence(
        _load_json_object(evidence_path, label="reviewed evidence"),
        expected_role=expected_role,
    )


def _validate_assessment_structure(assessment: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(assessment)
    expected_fields = {
        "schema_version",
        "kind",
        "authorized_by",
        "authorized_commit",
        "limitations_acknowledged",
        "evidence",
        "gates",
    }
    if set(payload) != expected_fields:
        missing = sorted(expected_fields - set(payload))
        unknown = sorted(set(payload) - expected_fields)
        raise ValueError(f"cutover assessment fields differ: missing={missing}, unknown={unknown}")
    if int(payload.get("schema_version") or 0) != ASSESSMENT_SCHEMA_VERSION:
        raise ValueError("unsupported cutover assessment schema")
    if payload.get("kind") != ASSESSMENT_KIND:
        raise ValueError("cutover assessment has the wrong kind")
    if not str(payload.get("authorized_by") or "").strip():
        raise ValueError("cutover assessment requires authorized_by")
    _full_commit(payload.get("authorized_commit"))
    limitations = payload.get("limitations_acknowledged")
    if not isinstance(limitations, list) or set(limitations) != REQUIRED_LIMITATIONS:
        raise ValueError("cutover assessment must acknowledge the exact authorization limitations")
    evidence = payload.get("evidence")
    gates = payload.get("gates")
    if not isinstance(evidence, Mapping) or not evidence:
        raise ValueError("cutover assessment requires evidence entries")
    if not isinstance(gates, Mapping) or set(gates) != set(REQUIRED_GATE_ROLES):
        raise ValueError("cutover assessment must contain the exact registered gate set")

    normalized_evidence: dict[str, dict[str, str]] = {}
    for evidence_id, raw in evidence.items():
        identity = str(evidence_id)
        if not identity or not isinstance(raw, Mapping):
            raise ValueError("cutover evidence ids and entries must be non-empty objects")
        if set(raw) != {"role", "path", "claim"}:
            raise ValueError(f"cutover evidence {identity!r} has unexpected fields")
        role = str(raw.get("role") or "")
        path = str(raw.get("path") or "")
        claim = str(raw.get("claim") or "").strip()
        if role not in ALL_EVIDENCE_ROLES or not Path(path).expanduser().is_absolute() or not claim:
            raise ValueError(f"cutover evidence {identity!r} has invalid role, path, or claim")
        normalized_evidence[identity] = {"role": role, "path": path, "claim": claim}

    referenced: set[str] = set()
    normalized_gates: dict[str, dict[str, Any]] = {}
    for gate_name, required_roles in REQUIRED_GATE_ROLES.items():
        raw_gate = gates[gate_name]
        if not isinstance(raw_gate, Mapping) or set(raw_gate) != {
            "status",
            "decision",
            "evidence",
        }:
            raise ValueError(f"cutover gate {gate_name!r} has unexpected fields")
        if raw_gate.get("status") != "passed":
            raise ValueError(f"cutover gate {gate_name!r} has not passed")
        decision = str(raw_gate.get("decision") or "").strip()
        evidence_ids = raw_gate.get("evidence")
        if not decision or not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError(f"cutover gate {gate_name!r} lacks a decision or evidence")
        if any(not isinstance(item, str) or item not in normalized_evidence for item in evidence_ids):
            raise ValueError(f"cutover gate {gate_name!r} references unknown evidence")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError(f"cutover gate {gate_name!r} repeats an evidence id")
        observed_roles = {normalized_evidence[item]["role"] for item in evidence_ids}
        missing_roles = sorted(required_roles - observed_roles)
        if missing_roles:
            raise ValueError(
                f"cutover gate {gate_name!r} lacks required roles: {', '.join(missing_roles)}"
            )
        referenced.update(evidence_ids)
        normalized_gates[gate_name] = {
            "status": "passed",
            "decision": decision,
            "evidence": list(evidence_ids),
        }
    unreferenced = sorted(set(normalized_evidence) - referenced)
    if unreferenced:
        raise ValueError(f"cutover assessment contains unreferenced evidence: {', '.join(unreferenced)}")
    payload["evidence"] = normalized_evidence
    payload["gates"] = normalized_gates
    return payload


def _machine_validate_evidence(
    *, role: str, path: Path, now_ns: int
) -> dict[str, Any]:
    if role == "demo_rule_probe":
        from .account_service_runner import load_demo_rules

        rules = load_demo_rules(path, now_ns=now_ns, max_age_seconds=7 * 24 * 60 * 60)
        payload = _load_json_object(path, label="demo-rule receipt")
        return {
            "validator": "demo_rules_v2",
            "status": "passed",
            "artifact_sha256": _lower_sha256(
                payload.get("artifact_sha256"), label="demo-rule artifact hash"
            ),
            "symbols": sorted(rules),
        }
    if role == "execution_twin_calibration":
        from .execution_twin_calibration import load_calibration_receipt

        receipt = load_calibration_receipt(path)
        if receipt.get("execution_twin_gate_passed") is not True:
            raise ValueError("execution-twin calibration gate has not passed")
        return {
            "validator": "execution_twin_calibration_v1",
            "status": "passed",
            "artifact_sha256": _lower_sha256(
                receipt.get("artifact_sha256"), label="calibration artifact hash"
            ),
            "sample_counts": receipt.get("sample_counts"),
        }
    if role in {"demo_owner_health_snapshot", "paper_owner_health_snapshot"}:
        from .account_owner_health import (
            ACCOUNT_OWNER_HEALTH_FILENAME,
            require_recent_account_owner_health,
        )

        if path.name != ACCOUNT_OWNER_HEALTH_FILENAME:
            raise ValueError(
                f"{role} must point to {ACCOUNT_OWNER_HEALTH_FILENAME}, got {path.name}"
            )
        environment = "demo" if role == "demo_owner_health_snapshot" else "paper"
        account_id = "bybit-demo-unified" if environment == "demo" else "bybit-paper-unified"
        health = require_recent_account_owner_health(
            path.parent,
            environment=environment,
            expected_account_id=account_id,
            max_age_ns=OWNER_HEALTH_AUTHORIZATION_MAX_AGE_NS,
            now_ns=now_ns,
        )
        return {
            "validator": "bound_account_owner_health_v1",
            "status": "passed",
            "artifact_sha256": _file_identity(path)["sha256"],
            "environment": environment,
            "account_id": health.account_id,
            "journal_sequence": health.journal_sequence,
            "journal_state_hash": health.journal_state_hash,
        }
    if role == "kernel_parity":
        from .kernel_parity import load_kernel_parity_receipt

        receipt = load_kernel_parity_receipt(path)
        if receipt.get("journal_parity_passed") is not True:
            raise ValueError("account-kernel structural parity gate has not passed")
        return {
            "validator": "account_kernel_parity_v1",
            "status": "passed",
            "artifact_sha256": _lower_sha256(
                receipt.get("artifact_sha256"), label="kernel-parity artifact hash"
            ),
            "evidence_scope": receipt.get("evidence_scope"),
        }
    if role in {"venue_accounting_reconciliation", "venue_flatness_snapshot"}:
        from .account_venue_accounting import load_venue_accounting_receipt

        receipt = load_venue_accounting_receipt(path)
        if receipt.get("account_id") != "bybit-demo-unified":
            raise ValueError("venue-accounting receipt belongs to the wrong demo account")
        if (
            receipt.get("evidence_scope")
            != "bybit_demo_account_pnl_funding_reconciliation"
        ):
            raise ValueError("venue-accounting receipt has the wrong evidence scope")
        gate_field = (
            "venue_accounting_gate_passed"
            if role == "venue_accounting_reconciliation"
            else "final_demo_flatness_gate_passed"
        )
        if receipt.get(gate_field) is not True:
            raise ValueError(f"venue-accounting receipt gate {gate_field!r} has not passed")
        return {
            "validator": "venue_accounting_reconciliation_v1",
            "status": "passed",
            "artifact_sha256": _lower_sha256(
                receipt.get("artifact_sha256"),
                label="venue-accounting artifact hash",
            ),
            "evidence_scope": receipt.get("evidence_scope"),
            "account_id": receipt.get("account_id"),
            "gate_field": gate_field,
            "sample_counts": receipt.get("sample_counts"),
        }
    receipt = load_reviewed_evidence(path, expected_role=role)
    return {
        "validator": "operator_reviewed_evidence_v1",
        "status": "operator_reviewed_integrity_only",
        "artifact_sha256": _lower_sha256(
            receipt.get("artifact_sha256"), label=f"{role} artifact hash"
        ),
        "reviewed_by": receipt.get("reviewed_by"),
        "reviewed_ts_ns": receipt.get("reviewed_ts_ns"),
    }


def build_authorization_receipt(
    assessment: Mapping[str, Any],
    *,
    assessment_path: str | Path,
    machine_id_path: str | Path,
    issued_ts_ns: int,
    lifetime_seconds: int = DEFAULT_AUTHORIZATION_LIFETIME_SECONDS,
) -> dict[str, Any]:
    """Validate one assessment and bind its reviewed evidence into a receipt."""

    if issued_ts_ns <= 0:
        raise ValueError("authorization issuance timestamp must be positive")
    if not 0 < lifetime_seconds <= MAX_AUTHORIZATION_LIFETIME_SECONDS:
        raise ValueError("authorization lifetime must be positive and no more than 24 hours")
    normalized = _validate_assessment_structure(assessment)
    assessment_file = _strict_file(assessment_path, label="cutover assessment")
    on_disk_assessment = _validate_assessment_structure(
        _load_json_object(assessment_file, label="cutover assessment")
    )
    if canonical_json(on_disk_assessment) != canonical_json(normalized):
        raise ValueError("cutover assessment argument does not match the bound assessment file")
    assessment_identity = _file_identity(assessment_file)
    evidence_output: dict[str, dict[str, Any]] = {}
    machine_checks: dict[str, dict[str, Any]] = {}
    for evidence_id, entry in normalized["evidence"].items():
        path = _strict_file(entry["path"], label=f"cutover evidence {evidence_id!r}")
        identity_before = _file_identity(path)
        machine_check = _machine_validate_evidence(
            role=entry["role"], path=path, now_ns=issued_ts_ns
        )
        identity = _file_identity(path)
        if identity != identity_before:
            raise ValueError(f"cutover evidence {evidence_id!r} changed during validation")
        evidence_output[evidence_id] = {
            "role": entry["role"],
            "claim": entry["claim"],
            **identity,
        }
        machine_checks[evidence_id] = machine_check

    receipt: dict[str, Any] = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "kind": AUTHORIZATION_KIND,
        "environment_scope": ["demo", "paper"],
        "authorized_by": normalized["authorized_by"],
        "authorized_commit": normalized["authorized_commit"],
        "machine_fingerprint_sha256": _machine_fingerprint(machine_id_path),
        "issued_ts_ns": int(issued_ts_ns),
        "expires_ts_ns": int(issued_ts_ns + lifetime_seconds * 1_000_000_000),
        "assessment": assessment_identity,
        "gates": normalized["gates"],
        "evidence": evidence_output,
        "machine_checks": machine_checks,
        "limitations_acknowledged": sorted(REQUIRED_LIMITATIONS),
        "substantive_gate_authority": "explicit_operator_assessment_bound_to_evidence",
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = _self_hash(receipt)
    return receipt


def verify_authorization_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_commit: str,
    machine_id_path: str | Path,
    now_ns: int | None = None,
) -> dict[str, Any]:
    payload = dict(receipt)
    expected_fields = {
        "schema_version",
        "kind",
        "environment_scope",
        "authorized_by",
        "authorized_commit",
        "machine_fingerprint_sha256",
        "issued_ts_ns",
        "expires_ts_ns",
        "assessment",
        "gates",
        "evidence",
        "machine_checks",
        "limitations_acknowledged",
        "substantive_gate_authority",
        "artifact_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("account-execution authorization has unexpected or missing fields")
    if int(payload.get("schema_version") or 0) != AUTHORIZATION_SCHEMA_VERSION:
        raise ValueError("unsupported account-execution authorization schema")
    if payload.get("kind") != AUTHORIZATION_KIND:
        raise ValueError("account-execution authorization has the wrong kind")
    if payload.get("environment_scope") != ["demo", "paper"]:
        raise ValueError("account-execution authorization must remain demo/paper scoped")
    commit = _full_commit(payload.get("authorized_commit"))
    prefix = _commit_prefix(expected_commit)
    if not commit.startswith(prefix):
        raise ValueError(
            f"authorization commit {commit} does not match expected commit {prefix}"
        )
    observed_machine = _lower_sha256(
        payload.get("machine_fingerprint_sha256"), label="authorization machine fingerprint"
    )
    if observed_machine != _machine_fingerprint(machine_id_path):
        raise ValueError("account-execution authorization belongs to another host")
    issued_ts_ns = int(payload.get("issued_ts_ns") or 0)
    expires_ts_ns = int(payload.get("expires_ts_ns") or 0)
    observed_now = time.time_ns() if now_ns is None else int(now_ns)
    if issued_ts_ns <= 0 or expires_ts_ns <= issued_ts_ns:
        raise ValueError("account-execution authorization has an invalid time window")
    if expires_ts_ns - issued_ts_ns > MAX_AUTHORIZATION_LIFETIME_SECONDS * 1_000_000_000:
        raise ValueError("account-execution authorization lifetime exceeds 24 hours")
    if issued_ts_ns - observed_now > MAX_FUTURE_SKEW_NS:
        raise ValueError("account-execution authorization is future-dated")
    if observed_now > expires_ts_ns:
        raise ValueError("account-execution authorization has expired")
    if not str(payload.get("authorized_by") or "").strip():
        raise ValueError("account-execution authorization lacks an operator identity")
    if payload.get("substantive_gate_authority") != "explicit_operator_assessment_bound_to_evidence":
        raise ValueError("account-execution authorization misstates its substantive authority")
    if set(payload.get("limitations_acknowledged") or ()) != REQUIRED_LIMITATIONS:
        raise ValueError("account-execution authorization lacks required limitations")
    assessment = payload.get("assessment")
    evidence = payload.get("evidence")
    gates = payload.get("gates")
    machine_checks = payload.get("machine_checks")
    if (
        not isinstance(assessment, Mapping)
        or not isinstance(evidence, Mapping)
        or not isinstance(gates, Mapping)
        or not isinstance(machine_checks, Mapping)
    ):
        raise ValueError("account-execution authorization lacks bound assessment or evidence")
    assessment_map = cast(Mapping[str, Any], assessment)
    evidence_map = cast(Mapping[str, Any], evidence)
    gates_map = cast(Mapping[str, Any], gates)
    machine_checks_map = cast(Mapping[str, Any], machine_checks)
    if set(assessment_map) != {"path", "size_bytes", "sha256"}:
        raise ValueError("account-execution authorization has an invalid assessment identity")
    _lower_sha256(assessment_map.get("sha256"), label="authorization assessment hash")
    if not str(assessment_map.get("path") or "").startswith("/") or int(
        assessment_map.get("size_bytes") or -1
    ) < 0:
        raise ValueError("account-execution authorization has an invalid assessment identity")
    if set(gates_map) != set(REQUIRED_GATE_ROLES):
        raise ValueError("account-execution authorization has the wrong gate set")
    if set(machine_checks_map) != set(evidence_map):
        raise ValueError("account-execution authorization machine checks do not cover its evidence")
    for evidence_id, entry in evidence_map.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"authorization evidence {evidence_id!r} must be an object")
        if set(entry) != {"role", "claim", "path", "size_bytes", "sha256"}:
            raise ValueError(f"authorization evidence {evidence_id!r} has invalid fields")
        role = str(entry.get("role") or "")
        if role not in ALL_EVIDENCE_ROLES:
            raise ValueError(f"authorization evidence {evidence_id!r} has an unknown role")
        _lower_sha256(entry.get("sha256"), label=f"authorization evidence {evidence_id!r} hash")
        if not str(entry.get("path") or "").startswith("/") or int(
            entry.get("size_bytes") or -1
        ) < 0:
            raise ValueError(f"authorization evidence {evidence_id!r} has invalid identity")
        check = machine_checks_map[evidence_id]
        if not isinstance(check, Mapping):
            raise ValueError(f"authorization evidence {evidence_id!r} lacks a machine check")
        expected_status = "passed" if role in MACHINE_VALIDATED_ROLES else "operator_reviewed_integrity_only"
        if check.get("status") != expected_status:
            raise ValueError(f"authorization evidence {evidence_id!r} has an invalid check status")
        _lower_sha256(
            check.get("artifact_sha256"),
            label=f"authorization evidence {evidence_id!r} checked artifact hash",
        )
        if role == "demo_rule_probe":
            if set(check) != {"validator", "status", "artifact_sha256", "symbols"} or check.get(
                "validator"
            ) != "demo_rules_v2" or not isinstance(check.get("symbols"), list) or not check.get(
                "symbols"
            ):
                raise ValueError(f"authorization evidence {evidence_id!r} lacks demo-rule validation")
        elif role == "execution_twin_calibration":
            if set(check) != {
                "validator",
                "status",
                "artifact_sha256",
                "sample_counts",
            } or check.get("validator") != "execution_twin_calibration_v1" or not isinstance(
                check.get("sample_counts"), Mapping
            ):
                raise ValueError(f"authorization evidence {evidence_id!r} lacks twin validation")
        elif role == "kernel_parity":
            if set(check) != {
                "validator",
                "status",
                "artifact_sha256",
                "evidence_scope",
            } or check.get("validator") != "account_kernel_parity_v1" or check.get(
                "evidence_scope"
            ) != "account_journal_structural_parity":
                raise ValueError(f"authorization evidence {evidence_id!r} lacks parity validation")
        elif role in {"demo_owner_health_snapshot", "paper_owner_health_snapshot"}:
            expected_environment = (
                "demo" if role == "demo_owner_health_snapshot" else "paper"
            )
            expected_account_id = (
                "bybit-demo-unified" if expected_environment == "demo" else "bybit-paper-unified"
            )
            if set(check) != {
                "validator",
                "status",
                "artifact_sha256",
                "environment",
                "account_id",
                "journal_sequence",
                "journal_state_hash",
            } or check.get("validator") != "bound_account_owner_health_v1" or check.get(
                "environment"
            ) != expected_environment or check.get("account_id") != expected_account_id or int(
                check.get("journal_sequence", -1)
            ) < 0:
                raise ValueError(f"authorization evidence {evidence_id!r} lacks owner-health validation")
            _lower_sha256(
                check.get("journal_state_hash"),
                label=f"authorization evidence {evidence_id!r} journal state hash",
            )
        elif role in {
            "venue_accounting_reconciliation",
            "venue_flatness_snapshot",
        }:
            expected_gate_field = (
                "venue_accounting_gate_passed"
                if role == "venue_accounting_reconciliation"
                else "final_demo_flatness_gate_passed"
            )
            if set(check) != {
                "validator",
                "status",
                "artifact_sha256",
                "evidence_scope",
                "account_id",
                "gate_field",
                "sample_counts",
            } or check.get(
                "validator"
            ) != "venue_accounting_reconciliation_v1" or check.get(
                "evidence_scope"
            ) != "bybit_demo_account_pnl_funding_reconciliation" or check.get(
                "account_id"
            ) != "bybit-demo-unified" or check.get(
                "gate_field"
            ) != expected_gate_field or not isinstance(
                check.get("sample_counts"), Mapping
            ):
                raise ValueError(
                    f"authorization evidence {evidence_id!r} lacks venue-accounting validation"
                )
        elif set(check) != {
            "validator",
            "status",
            "artifact_sha256",
            "reviewed_by",
            "reviewed_ts_ns",
        } or check.get("validator") != "operator_reviewed_evidence_v1" or not str(
            check.get("reviewed_by") or ""
        ).strip() or int(check.get("reviewed_ts_ns") or 0) <= 0:
            raise ValueError(f"authorization evidence {evidence_id!r} lacks reviewed-evidence validation")
    for gate_name, required_roles in REQUIRED_GATE_ROLES.items():
        gate = gates_map[gate_name]
        if not isinstance(gate, Mapping) or gate.get("status") != "passed":
            raise ValueError(f"authorization gate {gate_name!r} has not passed")
        if set(gate) != {"status", "decision", "evidence"}:
            raise ValueError(f"authorization gate {gate_name!r} has invalid fields")
        evidence_ids = gate.get("evidence")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError(f"authorization gate {gate_name!r} lacks evidence")
        if any(item not in evidence_map for item in evidence_ids):
            raise ValueError(f"authorization gate {gate_name!r} references unknown evidence")
        roles = {
            str(cast(Mapping[str, Any], evidence_map[item]).get("role") or "")
            for item in evidence_ids
        }
        if not required_roles.issubset(roles):
            raise ValueError(f"authorization gate {gate_name!r} lacks its required evidence roles")
        if not str(gate.get("decision") or "").strip():
            raise ValueError(f"authorization gate {gate_name!r} lacks a decision")
    observed_hash = str(payload.get("artifact_sha256") or "")
    if observed_hash != _self_hash(payload):
        raise ValueError("account-execution authorization hash mismatch")
    return payload


def load_authorization_receipt(
    path: str | Path,
    *,
    expected_commit: str,
    machine_id_path: str | Path,
    now_ns: int | None = None,
) -> dict[str, Any]:
    receipt_path = _strict_file(path, label="account-execution deploy authorization")
    metadata = receipt_path.stat()
    if metadata.st_mode & 0o077:
        raise ValueError("account-execution deploy authorization must have mode 0600")
    if metadata.st_uid != os.geteuid():
        raise ValueError("account-execution deploy authorization is not owned by the verifier")
    return verify_authorization_receipt(
        _load_json_object(receipt_path, label="account-execution deploy authorization"),
        expected_commit=expected_commit,
        machine_id_path=machine_id_path,
        now_ns=now_ns,
    )


def _git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def require_clean_authorized_checkout(repo_root: str | Path, authorized_commit: str) -> str:
    root = Path(repo_root).expanduser().resolve(strict=True)
    commit = _full_commit(authorized_commit)
    head = _git_output(root, "rev-parse", "HEAD").lower()
    if head != commit:
        raise ValueError(f"checkout HEAD {head} does not equal authorized commit {commit}")
    if _git_output(root, "status", "--porcelain"):
        raise ValueError("authorized checkout is dirty")
    return head


def assessment_template(*, authorized_commit: str, authorized_by: str) -> dict[str, Any]:
    commit = _full_commit(authorized_commit)
    evidence: dict[str, dict[str, str]] = {}
    gates: dict[str, dict[str, Any]] = {}
    for gate_name, roles in REQUIRED_GATE_ROLES.items():
        evidence_ids: list[str] = []
        for role in sorted(roles):
            evidence_id = role
            evidence.setdefault(
                evidence_id,
                {
                    "role": role,
                    "path": f"/replace/with/immutable/{role}.json",
                    "claim": "REPLACE_WITH_CLAIM_SCOPED_TO_THIS_ARTIFACT",
                },
            )
            evidence_ids.append(evidence_id)
        gates[gate_name] = {
            "status": "open",
            "decision": "REPLACE_ONLY_AFTER_REVIEW; DO_NOT CHANGE A REGISTERED RULE AFTER SEEING ITS RESULT",
            "evidence": evidence_ids,
        }
    return {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "kind": ASSESSMENT_KIND,
        "authorized_by": authorized_by,
        "authorized_commit": commit,
        "limitations_acknowledged": sorted(REQUIRED_LIMITATIONS),
        "evidence": evidence,
        "gates": gates,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue or verify the evidence-bound demo/paper account cutover authorization."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    template = subparsers.add_parser("template", help="Write an intentionally-open assessment template.")
    template.add_argument("--authorized-commit", required=True)
    template.add_argument("--authorized-by", required=True)
    template.add_argument("--output", type=Path, required=True)

    review = subparsers.add_parser(
        "review-evidence",
        help="Snapshot source hashes for a gate that still requires explicit operator judgment.",
    )
    review.add_argument("--role", required=True, choices=sorted(REVIEWED_EVIDENCE_ROLES))
    review.add_argument("--claim", required=True)
    review.add_argument("--reviewed-by", required=True)
    review.add_argument("--source", action="append", required=True)
    review.add_argument("--output", type=Path, required=True)

    issue = subparsers.add_parser("issue", help="Issue the short-lived deploy authorization.")
    issue.add_argument("--assessment", type=Path, required=True)
    issue.add_argument("--output", type=Path, required=True)
    issue.add_argument("--repo-root", type=Path, required=True)
    issue.add_argument("--machine-id-path", type=Path, default=Path("/etc/machine-id"))
    issue.add_argument(
        "--lifetime-seconds",
        type=int,
        default=DEFAULT_AUTHORIZATION_LIFETIME_SECONDS,
    )

    verify = subparsers.add_parser("verify", help="Verify deploy authority without changing state.")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--machine-id-path", type=Path, default=Path("/etc/machine-id"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            output = _atomic_write(
                args.output,
                assessment_template(
                    authorized_commit=args.authorized_commit.lower(),
                    authorized_by=args.authorized_by,
                ),
            )
            print(json.dumps({"output": str(output), "status": "open"}, sort_keys=True))
            return 0
        if args.command == "review-evidence":
            receipt = build_reviewed_evidence(
                role=args.role,
                claim=args.claim,
                reviewed_by=args.reviewed_by,
                source_paths=args.source,
                reviewed_ts_ns=time.time_ns(),
            )
            output = _atomic_write(args.output, receipt)
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "role": args.role,
                        "artifact_sha256": receipt["artifact_sha256"],
                        "review_type": receipt["review_type"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "issue":
            assessment_path = _strict_file(args.assessment, label="cutover assessment")
            assessment = _load_json_object(assessment_path, label="cutover assessment")
            normalized = _validate_assessment_structure(assessment)
            require_clean_authorized_checkout(
                args.repo_root, str(normalized["authorized_commit"])
            )
            receipt = build_authorization_receipt(
                normalized,
                assessment_path=assessment_path,
                machine_id_path=args.machine_id_path,
                issued_ts_ns=time.time_ns(),
                lifetime_seconds=args.lifetime_seconds,
            )
            output = _atomic_write(args.output, receipt)
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "authorized_commit": receipt["authorized_commit"],
                        "expires_ts_ns": receipt["expires_ts_ns"],
                        "artifact_sha256": receipt["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "verify":
            receipt = load_authorization_receipt(
                args.receipt,
                expected_commit=args.expected_commit,
                machine_id_path=args.machine_id_path,
            )
            require_clean_authorized_checkout(
                args.repo_root, str(receipt["authorized_commit"])
            )
            print(
                json.dumps(
                    {
                        "status": "authorized",
                        "authorized_commit": receipt["authorized_commit"],
                        "expires_ts_ns": receipt["expires_ts_ns"],
                        "artifact_sha256": receipt["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"account-execution cutover authority failed: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unsupported command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
