"""Commit- and evidence-bound authorization for the account-owner cutover.

The authorization is an operational control, not a digital signature and not
an automatic research verdict.  It makes the human gate decision explicit,
binds that decision to immutable evidence hashes, and prevents an authorization
from being silently reused on another host, commit, or maintenance window.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json


ASSESSMENT_SCHEMA_VERSION = 3
AUTHORIZATION_SCHEMA_VERSION = 3
REVIEWED_EVIDENCE_SCHEMA_VERSION = 1
ASSESSMENT_KIND = "account_execution_cutover_assessment"
AUTHORIZATION_KIND = "account_execution_deploy_authorization"
REVIEWED_EVIDENCE_KIND = "account_execution_cutover_reviewed_evidence"
DEFAULT_AUTHORIZATION_LIFETIME_SECONDS = 24 * 60 * 60
MAX_AUTHORIZATION_LIFETIME_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_NS = 5 * 60 * 1_000_000_000

AGGREGATE_CHECK_SCHEMA_VERSION = 4
AGGREGATE_CHECK_VALIDATOR = "account_cutover_evidence_set_v4"

REQUIRED_LIMITATIONS = frozenset(
    {
        "self_hash_is_not_a_signature",
        "authorization_is_demo_and_paper_only",
        "structural_parity_is_not_full_strategy_or_market_tape_parity",
        "v7_calibration_is_training_and_cannot_satisfy_the_natural_holdout",
        "an_inconclusive_or_invalid_natural_window_cannot_authorize_deployment",
        "stopped_epoch_seal_is_integrity_evidence_not_filesystem_immutability",
        "fresh_root_emptiness_is_an_immediately_prestart_gate_only",
        "operator_review_remains_responsible_for_non_machine-verifiable_claims",
    }
)

REQUIRED_GATE_ROLES: dict[str, frozenset[str]] = {
    "frozen_candidate_commit_configuration_and_epoch": frozenset({"natural_cutover_freeze_manifest"}),
    "maintenance_topology_and_retired_authority_absence": frozenset(
        {"natural_cutover_freeze_manifest", "topology_inventory"}
    ),
    "fresh_demo_and_paper_epochs": frozenset({"natural_cutover_freeze_manifest"}),
    "stopped_natural_epoch_and_fresh_deploy_roots": frozenset({"stopped_natural_epoch", "fresh_deploy_epoch"}),
    "candidate_universe_and_demo_rules": frozenset(
        {
            "natural_cutover_freeze_manifest",
            "candidate_rule_coverage",
            "demo_rule_probe",
        }
    ),
    "paper_then_demo_owner_first": frozenset(
        {
            "natural_cutover_freeze_manifest",
            "paper_owner_start_sequence",
            "demo_owner_start_sequence",
        }
    ),
    "natural_120h_target_order_fill_pnl_tape": frozenset(
        {
            "natural_tape_sufficiency",
            "captured_account_replay",
            "venue_accounting_reconciliation",
        }
    ),
    "execution_twin_training_and_oos_drift": frozenset({"execution_twin_calibration", "execution_twin_drift"}),
    "deterministic_cross_environment_replay": frozenset(
        {"captured_account_replay", "event_clock_comparison", "kernel_parity"}
    ),
    "venue_pnl_funding_and_final_flatness": frozenset({"venue_accounting_reconciliation", "venue_flatness_snapshot"}),
    "registered_outcome_and_deployment_boundary": frozenset({"final_evidence_card"}),
}

MACHINE_VALIDATED_ROLES = frozenset(
    {
        "natural_cutover_freeze_manifest",
        "candidate_rule_coverage",
        "captured_account_replay",
        "demo_rule_probe",
        "event_clock_comparison",
        "execution_twin_calibration",
        "execution_twin_drift",
        "kernel_parity",
        "natural_tape_sufficiency",
        "stopped_natural_epoch",
        "fresh_deploy_epoch",
        "venue_accounting_reconciliation",
        "venue_flatness_snapshot",
    }
)
REVIEWED_EVIDENCE_ROLES = frozenset(
    role for roles in REQUIRED_GATE_ROLES.values() for role in roles if role not in MACHINE_VALIDATED_ROLES
)
ALL_EVIDENCE_ROLES = MACHINE_VALIDATED_ROLES | REVIEWED_EVIDENCE_ROLES

MACHINE_VALIDATOR_IDS: dict[str, str] = {
    "natural_cutover_freeze_manifest": "natural_cutover_freeze_v1",
    "candidate_rule_coverage": "candidate_rule_coverage_v1",
    "captured_account_replay": "captured_account_replay_v3",
    "demo_rule_probe": "demo_rules_v3",
    "event_clock_comparison": "strategy_event_replay_parity_v3",
    "execution_twin_calibration": "execution_twin_calibration_v2",
    "execution_twin_drift": "execution_twin_drift_v2",
    "kernel_parity": "account_kernel_parity_v4",
    "natural_tape_sufficiency": "natural_tape_sufficiency_v3",
    "stopped_natural_epoch": "stopped_natural_epoch_v1",
    "fresh_deploy_epoch": "fresh_deploy_epoch_v1",
    "venue_accounting_reconciliation": "venue_accounting_reconciliation_v1",
    "venue_flatness_snapshot": "venue_accounting_reconciliation_v1",
}

_VENUE_RECEIPT_ROLES = frozenset({"venue_accounting_reconciliation", "venue_flatness_snapshot"})

_SEALED_SOURCE_ANALYSIS_ROLES = (
    "captured_account_replay",
    "event_clock_comparison",
    "kernel_parity",
    "execution_twin_drift",
    "natural_tape_sufficiency",
)

_OLD_MUTABLE_ROOT_ROLES = (
    "demo_account",
    "demo_inbox",
    "demo_capture",
    "paper_account",
    "paper_inbox",
    "paper_capture",
    "long_demo",
    "long_paper",
    "continuous_demo",
    "continuous_paper",
    "natural_evidence",
)

_FRESH_ROOT_ROLES = _OLD_MUTABLE_ROOT_ROLES[:-1]

_SEALED_SOURCE_ANALYSIS_DEPENDENCIES: Mapping[str, frozenset[str]] = {
    "captured_account_replay": frozenset(),
    "event_clock_comparison": frozenset(),
    "kernel_parity": frozenset({"captured_account_replay", "event_clock_comparison"}),
    "execution_twin_drift": frozenset(),
    "natural_tape_sufficiency": frozenset({"captured_account_replay"}),
}


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


def _file_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return read_stable_file(
        candidate,
        label=label,
        require_single_link=False,
    )


def _file_identity(
    path: Path,
    *,
    snapshot: StableFileSnapshot | None = None,
    label: str = "evidence file",
) -> dict[str, Any]:
    observed = snapshot or _file_snapshot(path, label=label)
    if observed.path != path.expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    return {
        "path": str(observed.path),
        "size_bytes": observed.size,
        "sha256": observed.sha256,
    }


def _machine_fingerprint(machine_id_path: str | Path) -> str:
    path = _strict_file(machine_id_path, label="machine-id file")
    snapshot = _file_snapshot(path, label="machine-id file")
    try:
        machine_id = snapshot.data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("machine-id file is not UTF-8") from exc
    if not machine_id:
        raise ValueError("machine-id file is empty")
    return hashlib.sha256(machine_id.encode("utf-8")).hexdigest()


def _load_json_object(
    path: Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    observed = snapshot or _file_snapshot(path, label=label)
    if observed.path != path.expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    try:
        value = json.loads(observed.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must contain a JSON object")
    return dict(value)


def _load_with_snapshot(
    loader: Any,
    path: Path,
    *,
    snapshot: StableFileSnapshot,
    **kwargs: Any,
) -> Any:
    if "snapshot" in inspect.signature(loader).parameters:
        kwargs["snapshot"] = snapshot
    # The fallback exists only for narrow test doubles. Repository evidence
    # loaders used in authorization all consume the supplied snapshot.
    return loader(path, **kwargs)


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()


def _sequence_sha256(values: Sequence[Any]) -> str:
    payload = json.dumps(
        list(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_identity_set_sha256(values: Sequence[Any]) -> str:
    """Hash the exact ordered path-identity set used by both epoch receipts."""

    return hashlib.sha256(canonical_json({"paths": list(values)})).hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _string(value: Any, *, label: str) -> str:
    output = str(value or "")
    if not output:
        raise ValueError(f"{label} must be non-empty")
    return output


def _absolute_path(value: Any, *, label: str) -> str:
    path = str(value or "")
    candidate = Path(path)
    if not path or not candidate.is_absolute() or ".." in candidate.parts or path != str(candidate):
        raise ValueError(f"{label} must be a canonical absolute path")
    return path


def _source_file_reference(value: Any, *, label: str) -> dict[str, str]:
    source = _mapping(value, label=label)
    return {
        "path": _absolute_path(source.get("path"), label=f"{label} path"),
        "sha256": _lower_sha256(source.get("sha256"), label=f"{label} hash"),
    }


def _source_file_references(values: Mapping[str, Any], *, label: str) -> list[dict[str, str]]:
    references = [
        _source_file_reference(value, label=f"{label} {name}")
        for name, value in sorted(values.items(), key=lambda item: str(item[0]))
    ]
    observed: dict[str, str] = {}
    for reference in references:
        prior = observed.setdefault(reference["path"], reference["sha256"])
        if prior != reference["sha256"]:
            raise ValueError(f"{label} assigns conflicting hashes to {reference['path']}")
    return [{"path": path, "sha256": digest} for path, digest in sorted(observed.items())]


def _source_root_references(values: Sequence[Any], *, label: str) -> list[str]:
    roots = sorted({_absolute_path(value, label=f"{label} root {index}") for index, value in enumerate(values)})
    if not roots:
        raise ValueError(f"{label} root set is empty")
    return roots


def _paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _path_within(path: str, root: str) -> bool:
    candidate = Path(path)
    parent = Path(root)
    return candidate == parent or parent in candidate.parents


def _standard_machine_check(
    *,
    role: str,
    path: Path,
    snapshot: StableFileSnapshot,
    artifact_sha256: Any,
    bindings: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        validator = MACHINE_VALIDATOR_IDS[role]
    except KeyError as exc:
        raise ValueError(f"role {role!r} has no registered machine validator") from exc
    normalized_bindings = dict(bindings)
    normalized_bindings["receipt_file_sha256"] = _file_identity(
        path,
        snapshot=snapshot,
        label=f"{role} evidence",
    )["sha256"]
    return {
        "validator": validator,
        "status": "passed",
        "artifact_sha256": _lower_sha256(
            artifact_sha256,
            label=f"{role} artifact hash",
        ),
        "bindings": normalized_bindings,
    }


def _standard_reviewed_check(*, role: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "validator": "operator_reviewed_evidence_v1",
        "status": "operator_reviewed_integrity_only",
        "artifact_sha256": _lower_sha256(receipt.get("artifact_sha256"), label=f"{role} artifact hash"),
        "bindings": {
            "role": role,
            "reviewed_by": receipt.get("reviewed_by"),
            "reviewed_ts_ns": receipt.get("reviewed_ts_ns"),
        },
    }


def _effective_runtime_config_machine_bindings(
    receipt: Mapping[str, Any],
    *,
    source_file_sha256: Any,
    label: str,
) -> dict[str, Any]:
    effective = _mapping(
        receipt.get("effective_runtime_config"),
        label=f"{label} effective runtime config",
    )
    if effective.get("execution_authorization") != "not_granted":
        raise ValueError(f"{label} effective runtime config grants execution authority")
    bundle_file_sha256 = _lower_sha256(
        effective.get("file_sha256"),
        label=f"{label} effective-config bundle file hash",
    )
    if bundle_file_sha256 != _lower_sha256(
        source_file_sha256,
        label=f"{label} effective-config source file hash",
    ):
        raise ValueError(f"{label} effective-config binding differs from its source file")
    freeze = _mapping(effective.get("freeze"), label=f"{label} effective-config freeze")
    run_config = _mapping(
        effective.get("natural_run_config"),
        label=f"{label} effective natural-run config",
    )
    candidate = _mapping(
        effective.get("candidate_universe"),
        label=f"{label} effective candidate universe",
    )
    window = _mapping(effective.get("window"), label=f"{label} effective natural window")
    repository = _mapping(
        effective.get("repository"),
        label=f"{label} effective repository",
    )
    runtime_paths = _mapping(
        effective.get("runtime_paths"),
        label=f"{label} effective runtime paths",
    )
    t0_ns = int(window.get("t0_ns") or 0)
    t1_ns = int(window.get("t1_ns") or 0)
    if window.get("interval") != "half_open_[t0,t1)" or t0_ns <= 0 or t1_ns <= t0_ns:
        raise ValueError(f"{label} effective runtime config has an invalid natural window")
    return {
        "effective_runtime_config_bundle_file_sha256": bundle_file_sha256,
        "effective_runtime_config_bundle_artifact_sha256": _lower_sha256(
            effective.get("artifact_sha256"),
            label=f"{label} effective-config bundle artifact hash",
        ),
        "effective_runtime_config_validator": _string(
            effective.get("validator"), label=f"{label} effective-config validator"
        ),
        "effective_runtime_config_freeze_path": _string(
            freeze.get("path"), label=f"{label} effective-config freeze path"
        ),
        "effective_runtime_config_freeze_file_sha256": _lower_sha256(
            freeze.get("file_sha256"), label=f"{label} effective-config freeze file hash"
        ),
        "effective_runtime_config_freeze_artifact_sha256": _lower_sha256(
            freeze.get("artifact_sha256"),
            label=f"{label} effective-config freeze artifact hash",
        ),
        "effective_runtime_config_freeze_id": _string(
            freeze.get("freeze_id"), label=f"{label} effective-config freeze id"
        ),
        "effective_runtime_config_run_config_path": _string(
            run_config.get("path"), label=f"{label} natural-run config path"
        ),
        "effective_runtime_config_run_config_file_sha256": _lower_sha256(
            run_config.get("file_sha256"), label=f"{label} natural-run config file hash"
        ),
        "effective_runtime_config_run_config_artifact_sha256": _lower_sha256(
            run_config.get("artifact_sha256"),
            label=f"{label} natural-run config artifact hash",
        ),
        "effective_runtime_config_candidate_path": _string(
            candidate.get("path"), label=f"{label} effective candidate path"
        ),
        "effective_runtime_config_candidate_file_sha256": _lower_sha256(
            candidate.get("file_sha256"), label=f"{label} effective candidate file hash"
        ),
        "effective_runtime_config_candidate_artifact_sha256": _lower_sha256(
            candidate.get("artifact_sha256"),
            label=f"{label} effective candidate artifact hash",
        ),
        "effective_runtime_config_t0_ns": t0_ns,
        "effective_runtime_config_t1_ns": t1_ns,
        "effective_runtime_config_candidate_commit": _full_commit(
            repository.get("candidate_commit"),
            label=f"{label} effective candidate commit",
        ),
        "effective_runtime_config_origin_main_commit": _full_commit(
            repository.get("origin_main_commit"),
            label=f"{label} effective origin/main commit",
        ),
        "effective_runtime_config_target_capture_path": _string(
            runtime_paths.get("target_capture_path"),
            label=f"{label} effective target-capture path",
        ),
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    created = False
    try:
        descriptor = os.open(
            str(path),
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("cutover receipt write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
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
    sources = [_file_identity(_strict_file(path, label=f"{role} source")) for path in source_paths]
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


def verify_reviewed_evidence(receipt: Mapping[str, Any], *, expected_role: str) -> dict[str, Any]:
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
        raise ValueError(f"reviewed evidence scope is {payload.get('evidence_scope')!r}, expected {expected_role!r}")
    if expected_role not in REVIEWED_EVIDENCE_ROLES:
        raise ValueError(f"role {expected_role!r} is not operator-reviewed")
    if payload.get("review_type") != "operator_attestation_with_source_hashes":
        raise ValueError("reviewed evidence has an unsupported review type")
    if not str(payload.get("claim") or "").strip() or not str(payload.get("reviewed_by") or "").strip():
        raise ValueError("reviewed evidence lacks a claim or reviewer")
    if int(payload.get("reviewed_ts_ns") or 0) <= 0:
        raise ValueError("reviewed evidence has an invalid timestamp")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("reviewed evidence lacks source identities")
    observed_paths: set[str] = set()
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
        path = _strict_file(
            str(source["path"]),
            label=f"reviewed evidence source {index}",
        )
        observed = _file_identity(path)
        if observed != dict(source):
            raise ValueError(f"reviewed evidence source {index} changed after review")
        if observed["path"] in observed_paths:
            raise ValueError("reviewed evidence source paths must be distinct")
        observed_paths.add(observed["path"])
    if str(payload.get("artifact_sha256") or "") != _self_hash(payload):
        raise ValueError("reviewed evidence hash mismatch")
    return payload


def load_reviewed_evidence(
    path: str | Path,
    *,
    expected_role: str,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    evidence_path = _strict_file(path, label=f"{expected_role} evidence")
    return verify_reviewed_evidence(
        _load_json_object(
            evidence_path,
            label="reviewed evidence",
            snapshot=snapshot,
        ),
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
    evidence_id_by_role: dict[str, str] = {}
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
        prior_id = evidence_id_by_role.get(role)
        if prior_id is not None:
            raise ValueError(f"cutover evidence role {role!r} is repeated by {prior_id!r} and {identity!r}")
        evidence_id_by_role[role] = identity
        normalized_evidence[identity] = {"role": role, "path": path, "claim": claim}

    if set(evidence_id_by_role) != ALL_EVIDENCE_ROLES:
        missing = sorted(ALL_EVIDENCE_ROLES - set(evidence_id_by_role))
        extra = sorted(set(evidence_id_by_role) - ALL_EVIDENCE_ROLES)
        raise ValueError(f"cutover assessment evidence roles differ: missing={missing}, extra={extra}")

    roles_by_path: dict[str, set[str]] = {}
    for entry in normalized_evidence.values():
        normalized_path = str(Path(entry["path"]).expanduser())
        roles_by_path.setdefault(normalized_path, set()).add(entry["role"])
    for path, roles in roles_by_path.items():
        if len(roles) > 1 and frozenset(roles) != _VENUE_RECEIPT_ROLES:
            raise ValueError(f"cutover evidence path {path!r} is reused by incompatible roles: {sorted(roles)}")

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
        extra_roles = sorted(observed_roles - required_roles)
        if missing_roles or extra_roles:
            raise ValueError(
                f"cutover gate {gate_name!r} evidence roles differ: missing={missing_roles}, extra={extra_roles}"
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
    *,
    role: str,
    path: Path,
    now_ns: int,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or _file_snapshot(path, label=f"{role} evidence")
    if role == "stopped_natural_epoch":
        from .stopped_natural_epoch import load_stopped_natural_epoch_seal

        # This verifier must remain usable after deployment. The receipt
        # reopens the sealed sources, but live systemd state is only required
        # during the immediately-prestart deployment check.
        receipt = _load_with_snapshot(
            load_stopped_natural_epoch_seal,
            path,
            snapshot=snapshot,
            require_currently_stopped=False,
        )
        identity = _mapping(receipt.get("identity"), label="stopped epoch identity")
        sealed = _mapping(
            receipt.get("sealed_namespace"),
            label="stopped epoch namespace",
        )
        inputs = _mapping(receipt.get("inputs"), label="stopped epoch inputs")
        source_files = _mapping(
            receipt.get("source_files"),
            label="stopped epoch source files",
        )
        source_trees = _mapping(
            receipt.get("source_trees"),
            label="stopped epoch source trees",
        )
        tape_semantics = _mapping(
            receipt.get("tape_semantics"),
            label="stopped epoch tape semantics",
        )
        service_state = _mapping(
            receipt.get("service_state"),
            label="stopped epoch service state",
        )
        if receipt.get("execution_authorization") != "not_granted":
            raise ValueError("stopped natural epoch grants execution authority")
        required_roots = sealed.get("required_old_mutable_roots")
        required_files = sealed.get("required_old_mutable_files")
        if not isinstance(required_roots, list) or not required_roots:
            raise ValueError("stopped natural epoch lacks its old mutable roots")
        if not isinstance(required_files, list):
            raise ValueError("stopped natural epoch lacks its old mutable file set")
        old_path_identities: list[dict[str, Any]] = []
        old_root_roles: list[str] = []
        for index, value in enumerate(required_roots):
            registered = _mapping(
                value,
                label=f"stopped epoch registered root {index}",
            )
            root_role = _string(
                registered.get("role"),
                label=f"stopped epoch registered root role {index}",
            )
            registered_path = _string(
                registered.get("path"),
                label=f"stopped epoch registered root path {index}",
            )
            tree = _mapping(
                source_trees.get(root_role),
                label=f"stopped epoch source tree {root_role}",
            )
            root_identity = _mapping(
                tree.get("root_identity"),
                label=f"stopped epoch root identity {root_role}",
            )
            if root_identity.get("path") != registered_path:
                raise ValueError(f"stopped epoch root identity {root_role!r} differs from its namespace path")
            old_path_identities.append(
                {
                    "path": registered_path,
                    "kind": str(root_identity.get("kind") or "directory"),
                    "device": int(root_identity.get("device") or 0),
                    "inode": int(root_identity.get("inode") or 0),
                    "mode": int(root_identity.get("mode") or 0),
                    "uid": int(root_identity.get("uid") or 0),
                }
            )
            old_root_roles.append(root_role)
        freeze_source = source_files.get("freeze_manifest")
        if not isinstance(freeze_source, Mapping):
            raise ValueError("stopped natural epoch lacks the canonical freeze source")
        freeze_source_map = cast(Mapping[str, Any], freeze_source)
        all_units_stopped = (
            service_state.get("all_inactive_before_hashing") is True
            and service_state.get("all_inactive_after_hashing") is True
        )
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "created_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "manifest_path": str(path.resolve(strict=True)),
                "seal_artifact_sha256": _lower_sha256(
                    receipt.get("artifact_sha256"),
                    label="stopped epoch seal artifact hash",
                ),
                "candidate_commit": _full_commit(
                    identity.get("candidate_commit"),
                    label="stopped epoch candidate commit",
                ),
                "origin_main_commit": _full_commit(
                    identity.get("origin_main_commit"),
                    label="stopped epoch origin/main commit",
                ),
                "freeze_id": _string(
                    identity.get("freeze_id"),
                    label="stopped epoch freeze id",
                ),
                "freeze_manifest": {
                    "path": _string(
                        freeze_source_map.get("path"),
                        label="stopped epoch freeze source path",
                    ),
                    "sha256": _lower_sha256(
                        freeze_source_map.get("sha256"),
                        label="stopped epoch freeze source hash",
                    ),
                    "artifact_sha256": _lower_sha256(
                        identity.get("freeze_artifact_sha256"),
                        label="stopped epoch freeze artifact hash",
                    ),
                },
                "t0_ns": int(identity.get("t0_ns") or 0),
                "t1_ns": int(identity.get("t1_ns") or 0),
                "interval": _string(
                    identity.get("interval"),
                    label="stopped epoch interval",
                ),
                "old_mutable_root_roles": old_root_roles,
                "old_sealed_root_paths": [identity["path"] for identity in old_path_identities],
                "old_sealed_root_paths_sha256": _sequence_sha256(
                    [identity["path"] for identity in old_path_identities]
                ),
                "old_sealed_paths": old_path_identities,
                "old_sealed_paths_sha256": _path_identity_set_sha256(old_path_identities),
                "old_mutable_files": list(required_files),
                "inputs": dict(inputs),
                "source_files": dict(source_files),
                "source_trees": dict(source_trees),
                "tape_semantics": dict(tape_semantics),
                "service_state": dict(service_state),
                "all_units_stopped": all_units_stopped,
                "execution_authorization": receipt.get("execution_authorization"),
            },
        )
    if role == "natural_cutover_freeze_manifest":
        from .natural_cutover_freeze_manifest import (
            load_natural_cutover_freeze_manifest,
        )

        receipt = load_natural_cutover_freeze_manifest(path, snapshot=snapshot)
        repository = _mapping(receipt.get("repository"), label="freeze repository")
        window = _mapping(receipt.get("window"), label="freeze window")
        runtime = _mapping(receipt.get("runtime"), label="freeze runtime")
        training = _mapping(receipt.get("v7_training"), label="freeze V7 training")
        population = _mapping(receipt.get("population"), label="freeze population")
        clock = _mapping(receipt.get("clock"), label="freeze clock")
        reset = _mapping(receipt.get("reset"), label="freeze reset")
        owner_first = _mapping(receipt.get("owner_first"), label="freeze owner-first")
        gates = _mapping(receipt.get("gates"), label="freeze gates")
        freeze_source_files = _mapping(receipt.get("source_files"), label="freeze source files")
        if gates.get("pre_window_freeze_passed") is not True or gates.get("execution_authorization") != "not_granted":
            raise ValueError("natural cutover freeze manifest has not passed its pre-window gate")
        t0_ns = int(window.get("t0_ns") or 0)
        t1_ns = int(window.get("t1_ns") or 0)
        if t0_ns <= 0 or t1_ns - t0_ns != 120 * 60 * 60 * 1_000_000_000:
            raise ValueError("natural cutover freeze does not bind the exact 120-hour window")

        def artifact(section: Mapping[str, Any], name: str) -> str:
            return _lower_sha256(
                _mapping(section.get(name), label=f"freeze {name}").get("artifact_sha256"),
                label=f"freeze {name} artifact hash",
            )

        archive_map_ref = _mapping(training.get("archive_map"), label="freeze V7 archive map")
        archive_map_path = _strict_file(
            _absolute_path(archive_map_ref.get("path"), label="freeze V7 archive-map path"),
            label="freeze V7 archive map",
        )
        archive_map_snapshot = _file_snapshot(archive_map_path, label="freeze V7 archive map")
        archive_map_identity = _file_identity(
            archive_map_path,
            snapshot=archive_map_snapshot,
            label="freeze V7 archive map",
        )
        if archive_map_identity["sha256"] != _lower_sha256(
            archive_map_ref.get("file_sha256"),
            label="freeze V7 archive-map file hash",
        ):
            raise ValueError("freeze V7 archive-map file binding changed")
        archive_map_payload = _load_json_object(
            archive_map_path,
            label="freeze V7 archive map",
            snapshot=archive_map_snapshot,
        )
        if (
            _file_identity(
                archive_map_path,
                snapshot=_file_snapshot(archive_map_path, label="freeze V7 archive map"),
                label="freeze V7 archive map",
            )
            != archive_map_identity
        ):
            raise ValueError("freeze V7 archive map changed during validation")
        if archive_map_payload.get("artifact_sha256") != artifact(training, "archive_map"):
            raise ValueError("freeze V7 archive-map artifact binding changed")
        archived_sources = _mapping(
            archive_map_payload.get("archived_sources"),
            label="freeze V7 archived sources",
        )
        registered_preseal_roots = _source_root_references(
            [
                archived_sources.get("account_root"),
                archived_sources.get("market_capture_root"),
            ],
            label="freeze registered pre-seal V7 archive",
        )

        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "freeze_id": _string(receipt.get("freeze_id"), label="freeze id"),
                "freeze_manifest_path": str(path.resolve(strict=True)),
                "created_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "authorized_commit": _full_commit(repository.get("candidate_commit"), label="freeze candidate commit"),
                "origin_main_commit": _full_commit(
                    repository.get("origin_main_commit"), label="freeze origin/main commit"
                ),
                "local_suite_artifact_sha256": artifact(repository, "local_suite"),
                "linux_ci_artifact_sha256": artifact(repository, "linux_ci"),
                "t0_ns": t0_ns,
                "t1_ns": t1_ns,
                "account_ids": dict(_mapping(runtime.get("account_ids"), label="freeze account ids")),
                "roots": dict(_mapping(runtime.get("roots"), label="freeze roots")),
                "risk_policy": dict(_mapping(runtime.get("risk_policy"), label="freeze risk policy")),
                "routes": dict(_mapping(runtime.get("routes"), label="freeze routes")),
                "seed": dict(_mapping(runtime.get("seed"), label="freeze seed")),
                "risk_policy_sha256": _lower_sha256(
                    _mapping(runtime.get("risk_policy"), label="freeze risk policy").get("sha256"),
                    label="freeze risk-policy set hash",
                ),
                "routes_sha256": _lower_sha256(
                    _mapping(runtime.get("routes"), label="freeze routes").get("sha256"),
                    label="freeze route-set hash",
                ),
                "seed_sha256": _lower_sha256(
                    _mapping(runtime.get("seed"), label="freeze seed").get("sha256"),
                    label="freeze seed hash",
                ),
                "calibration_artifact_sha256": artifact(training, "calibration"),
                "archive_map_artifact_sha256": artifact(training, "archive_map"),
                "baseline_config_artifact_sha256": artifact(training, "baseline_config"),
                "stress_config_artifact_sha256": artifact(training, "stress_config"),
                "candidate_universe_artifact_sha256": artifact(population, "candidate_universe"),
                "candidate_universe_file_sha256": _lower_sha256(
                    _mapping(
                        population.get("candidate_universe"),
                        label="freeze candidate universe",
                    ).get("file_sha256"),
                    label="freeze candidate-universe file hash",
                ),
                "candidate_universe_path": _string(
                    _mapping(
                        population.get("candidate_universe"),
                        label="freeze candidate universe",
                    ).get("path"),
                    label="freeze candidate-universe path",
                ),
                "demo_rules_artifact_sha256": artifact(population, "demo_rules"),
                "demo_rules_file_sha256": _lower_sha256(
                    _mapping(population.get("demo_rules"), label="freeze demo rules").get("file_sha256"),
                    label="freeze demo-rules file hash",
                ),
                "rule_coverage_artifact_sha256": artifact(population, "rule_coverage"),
                "clock_artifact_sha256": artifact(clock, "receipt"),
                "clock_file_sha256": _lower_sha256(
                    _mapping(clock.get("receipt"), label="freeze clock receipt").get("file_sha256"),
                    label="freeze clock-receipt file hash",
                ),
                "reset_archive_sha256": _lower_sha256(
                    _mapping(reset.get("archive"), label="freeze reset archive").get("sha256"),
                    label="freeze reset archive hash",
                ),
                "reset_receipt_artifact_sha256": _lower_sha256(
                    _mapping(reset.get("receipt"), label="freeze reset receipt").get("artifact_sha256"),
                    label="freeze reset-receipt artifact hash",
                ),
                "reset_receipt_file_sha256": _lower_sha256(
                    _mapping(reset.get("receipt"), label="freeze reset receipt").get("file_sha256"),
                    label="freeze reset-receipt file hash",
                ),
                "reset_started_ts_ns": int(reset.get("started_ts_ns") or 0),
                "reset_finished_ts_ns": int(reset.get("finished_ts_ns") or 0),
                "fresh_roots_verified_at_reset": reset.get("fresh_roots_verified_at_reset"),
                "reset_account_epoch_roots": list(cast(Sequence[Any], reset.get("account_epoch_roots") or ())),
                "paper_owner_first_artifact_sha256": artifact(owner_first, "paper"),
                "demo_owner_first_artifact_sha256": artifact(owner_first, "demo"),
                "registered_preseal_source_files": _source_file_references(
                    freeze_source_files,
                    label="freeze registered pre-seal source",
                ),
                "registered_preseal_source_roots": registered_preseal_roots,
            },
        )
    if role == "fresh_deploy_epoch":
        from .fresh_deploy_epoch import load_fresh_deploy_epoch

        # Authorization is also verified after the new units have started, so
        # this reopens identities and bindings without requiring roots to stay
        # empty. Deployment performs the stronger immediately-prestart check.
        receipt = _load_with_snapshot(
            load_fresh_deploy_epoch,
            path,
            snapshot=snapshot,
            require_empty_roots=False,
        )
        old_paths = receipt.get("old_sealed_paths")
        roots = receipt.get("roots")
        late_environment = receipt.get("late_environment")
        if not isinstance(old_paths, list) or not old_paths:
            raise ValueError("fresh deploy epoch lacks its old sealed path set")
        if not isinstance(roots, Mapping) or not roots:
            raise ValueError("fresh deploy epoch lacks its fresh-root identities")
        if not isinstance(late_environment, Mapping) or not late_environment:
            raise ValueError("fresh deploy epoch lacks its late environment map")
        if receipt.get("execution_authorization") != "not_granted":
            raise ValueError("fresh deploy epoch grants execution authority")
        seal = _mapping(
            receipt.get("stopped_epoch_seal"),
            label="fresh deploy stopped-epoch seal identity",
        )
        old_root_paths = [
            _string(
                _mapping(value, label=f"fresh deploy old sealed path {index}").get("path"),
                label=f"fresh deploy old sealed path {index}",
            )
            for index, value in enumerate(old_paths)
        ]
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "created_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "epoch_id": _string(receipt.get("epoch_id"), label="fresh deploy epoch id"),
                "candidate_commit": _full_commit(
                    receipt.get("candidate_commit"),
                    label="fresh deploy candidate commit",
                ),
                "freeze_id": _string(
                    receipt.get("freeze_id"),
                    label="fresh deploy freeze id",
                ),
                "manifest_path": str(path.resolve(strict=True)),
                "epoch_parent": _string(
                    receipt.get("epoch_parent"),
                    label="fresh deploy epoch parent",
                ),
                "stopped_epoch_seal": dict(seal),
                "old_sealed_root_paths": old_root_paths,
                "old_sealed_root_paths_sha256": _sequence_sha256(old_root_paths),
                "old_sealed_paths": list(old_paths),
                "old_sealed_paths_sha256": _lower_sha256(
                    receipt.get("old_sealed_paths_sha256"),
                    label="fresh deploy old sealed path-set hash",
                ),
                "roots": dict(roots),
                "late_environment": dict(late_environment),
                "execution_authorization": receipt.get("execution_authorization"),
            },
        )
    if role == "candidate_rule_coverage":
        from .candidate_rule_coverage import load_candidate_rule_coverage

        receipt = load_candidate_rule_coverage(
            path,
            validation_now_ns=now_ns,
            snapshot=snapshot,
        )
        candidate = _mapping(receipt.get("candidate_universe"), label="candidate coverage universe")
        rules = _mapping(receipt.get("demo_rules"), label="candidate coverage rules")
        symbols = receipt.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ValueError("candidate-rule coverage has an empty symbol population")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "candidate_universe_artifact_sha256": _lower_sha256(
                    candidate.get("artifact_sha256"), label="candidate universe artifact hash"
                ),
                "candidate_universe_file_sha256": _lower_sha256(
                    candidate.get("file_sha256"), label="candidate universe file hash"
                ),
                "demo_rules_artifact_sha256": _lower_sha256(
                    rules.get("artifact_sha256"), label="covered demo-rules artifact hash"
                ),
                "demo_rules_file_sha256": _lower_sha256(rules.get("file_sha256"), label="covered demo-rules file hash"),
                "symbol_count": len(symbols),
                "symbol_set_sha256": _sequence_sha256(symbols),
            },
        )
    if role == "demo_rule_probe":
        from .account_execution_config import load_demo_rules

        rules = load_demo_rules(
            path,
            now_ns=now_ns,
            max_age_seconds=7 * 24 * 60 * 60,
            snapshot=snapshot,
        )
        payload = _load_json_object(
            path,
            label="demo-rule receipt",
            snapshot=snapshot,
        )
        symbols = sorted(rules)
        source = _mapping(payload.get("symbol_source"), label="demo-rule symbol source")
        if source.get("kind") != "candidate_universe_artifact":
            raise ValueError("demo-rule receipt is not bound to the frozen candidate universe")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=payload.get("artifact_sha256"),
            bindings={
                "verified_ts_ns": int(payload.get("verified_ts_ns") or 0),
                "candidate_universe_artifact_sha256": _lower_sha256(
                    source.get("artifact_sha256"), label="demo-rule candidate artifact hash"
                ),
                "symbol_count": len(symbols),
                "symbol_set_sha256": _sequence_sha256(symbols),
            },
        )
    if role == "execution_twin_calibration":
        from .execution_twin_calibration import load_calibration_receipt

        receipt = load_calibration_receipt(
            path,
            require_registered_requirements=True,
            snapshot=snapshot,
        )
        if receipt.get("execution_twin_gate_passed") is not True:
            raise ValueError("execution-twin calibration gate has not passed")
        inputs = _mapping(receipt.get("inputs"), label="calibration inputs")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "schema_version": int(receipt.get("schema_version") or 0),
                "account_id": _string(receipt.get("expected_account_id"), label="calibration account id"),
                "account_journal_sha256": _lower_sha256(
                    inputs.get("account_journal_sha256"), label="calibration journal hash"
                ),
                "account_last_event_hash": _lower_sha256(
                    inputs.get("account_last_event_hash"), label="calibration journal head"
                ),
                "market_capture_manifest_sha256": _lower_sha256(
                    inputs.get("market_capture_manifest_sha256"),
                    label="calibration capture manifest hash",
                ),
                "market_order_smoke_gate_passed": receipt.get("market_order_smoke_gate_passed"),
                "partial_fill_calibration_gate_passed": receipt.get("partial_fill_calibration_gate_passed"),
                "sample_counts": dict(_mapping(receipt.get("sample_counts"), label="calibration sample counts")),
            },
        )
    if role == "natural_tape_sufficiency":
        from .natural_tape_sufficiency import load_natural_tape_sufficiency_receipt

        receipt = _load_with_snapshot(
            load_natural_tape_sufficiency_receipt,
            path,
            snapshot=snapshot,
        )
        if (
            receipt.get("integrity_gate_passed") is not True
            or receipt.get("sufficiency_gate_passed") is not True
            or receipt.get("status") != "passed"
        ):
            raise ValueError("natural 120-hour tape is not a sufficient pass")
        window = _mapping(receipt.get("natural_window"), label="natural window")
        capture = _mapping(receipt.get("target_capture"), label="natural target capture")
        replay = _mapping(receipt.get("account_replay"), label="natural account replay")
        venue = _mapping(receipt.get("venue_accounting"), label="natural venue accounting")
        input_paths = _mapping(receipt.get("input_paths"), label="natural input paths")
        safety = _mapping(receipt.get("post_window_safety"), label="natural safety scope")
        freeze_binding = _mapping(
            receipt.get("natural_cutover_freeze"),
            label="natural cutover freeze binding",
        )
        source_files = _mapping(receipt.get("source_files"), label="natural source files")
        target_capture_source = _mapping(
            source_files.get("natural/target_capture"),
            label="natural target-capture source identity",
        )
        effective_config_source = _mapping(
            source_files.get("natural/effective_runtime_config_bundle"),
            label="natural effective-config source identity",
        )
        batch_ids = capture.get("natural_batch_ids")
        safety_batch_ids = safety.get("batch_ids")
        if not isinstance(batch_ids, list) or not batch_ids:
            raise ValueError("natural sufficiency receipt has no natural batch ids")
        if not isinstance(safety_batch_ids, list):
            raise ValueError("natural sufficiency receipt lacks its exact safety batch set")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "account_id": _string(receipt.get("expected_account_id"), label="natural account id"),
                "t0_ns": int(window.get("t0_ns") or 0),
                "t1_ns": int(window.get("t1_ns") or 0),
                "freeze_id": _string(freeze_binding.get("freeze_id"), label="natural freeze id"),
                "freeze_artifact_sha256": _lower_sha256(
                    freeze_binding.get("artifact_sha256"),
                    label="natural freeze artifact hash",
                ),
                "candidate_commit": _full_commit(
                    freeze_binding.get("candidate_commit"),
                    label="natural freeze candidate commit",
                ),
                "origin_main_commit": _full_commit(
                    freeze_binding.get("origin_main_commit"),
                    label="natural freeze origin/main commit",
                ),
                "clock_artifact_sha256": _lower_sha256(
                    freeze_binding.get("clock_artifact_sha256"),
                    label="natural freeze clock artifact hash",
                ),
                "clock_file_sha256": _lower_sha256(
                    freeze_binding.get("clock_file_sha256"),
                    label="natural freeze clock file hash",
                ),
                "demo_account_root": _string(input_paths.get("demo_account_root"), label="natural demo account root"),
                "source_set_sha256": _lower_sha256(receipt.get("source_set_sha256"), label="natural source-set hash"),
                "target_capture_tape_hash": _lower_sha256(
                    capture.get("capture_tape_hash"), label="natural target-capture hash"
                ),
                "target_capture_file_sha256": _lower_sha256(
                    target_capture_source.get("sha256"),
                    label="natural target-capture file hash",
                ),
                "natural_batch_ids_sha256": _sequence_sha256(sorted(batch_ids)),
                "natural_batch_count": len(batch_ids),
                "safety_batch_ids_sha256": _sequence_sha256(sorted(safety_batch_ids)),
                "account_replay_artifact_sha256": _lower_sha256(
                    replay.get("artifact_sha256"), label="natural account-replay hash"
                ),
                "venue_accounting_artifact_sha256": _lower_sha256(
                    venue.get("artifact_sha256"), label="natural venue-accounting hash"
                ),
                "declared_analysis_completed_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "analysis_source_files": _source_file_references(source_files, label="natural sufficiency source"),
                "analysis_source_roots": [
                    _absolute_path(
                        input_paths.get("demo_account_root"),
                        label="natural sufficiency demo account root",
                    )
                ],
                **_effective_runtime_config_machine_bindings(
                    receipt,
                    source_file_sha256=effective_config_source.get("sha256"),
                    label="natural sufficiency",
                ),
            },
        )
    if role == "captured_account_replay":
        from .captured_account_replay import load_captured_account_replay_receipt

        receipt = load_captured_account_replay_receipt(path, snapshot=snapshot)
        if receipt.get("evidence_scope") != "captured_demo_account_kernel_and_execution_twin_replay":
            raise ValueError("captured-account replay has the wrong evidence scope")
        for field in (
            "historical_paper_exact_outcome_passed",
            "demo_plan_parity_passed",
            "exact_preexecution_plan_match",
            "has_durable_request_batches",
        ):
            if receipt.get(field) is not True:
                raise ValueError(f"captured-account replay gate {field!r} has not passed")
        source_roots = _mapping(receipt.get("source_roots"), label="replay source roots")
        input_manifest = _mapping(receipt.get("input_manifest"), label="replay input manifest")
        window = _mapping(input_manifest.get("natural_window"), label="replay natural window")
        capture = _mapping(receipt.get("target_capture"), label="replay target capture")
        safety = _mapping(receipt.get("post_window_safety"), label="replay safety manifest")
        config = _mapping(receipt.get("config"), label="replay config")
        outputs = _mapping(receipt.get("outputs"), label="replay outputs")
        source_files = _mapping(receipt.get("source_files"), label="replay source files")
        freeze_binding = _mapping(
            receipt.get("natural_cutover_freeze"),
            label="replay natural-cutover freeze binding",
        )
        target_capture_source = _mapping(
            source_files.get("target_scheduling_capture"),
            label="replay target-capture source identity",
        )
        freeze_source = _mapping(
            source_files.get("natural_cutover_freeze_manifest"),
            label="replay freeze-manifest source identity",
        )
        rules_source = _mapping(
            source_files.get("demo_rules"),
            label="replay demo-rules source identity",
        )
        risk_source = _mapping(
            source_files.get("risk_policy"),
            label="replay risk-policy source identity",
        )
        effective_config_source = _mapping(
            source_files.get("effective_runtime_config_bundle"),
            label="replay effective-config source identity",
        )
        batch_ids = receipt.get("ordered_batch_ids")
        if not isinstance(batch_ids, list) or not batch_ids:
            raise ValueError("captured-account replay has no ordered natural batches")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "t0_ns": int(window.get("t0_ns") or 0),
                "t1_ns": int(window.get("t1_ns") or 0),
                "freeze_id": _string(freeze_binding.get("freeze_id"), label="replay freeze id"),
                "freeze_artifact_sha256": _lower_sha256(
                    freeze_binding.get("artifact_sha256"),
                    label="replay freeze artifact hash",
                ),
                "freeze_manifest_file_sha256": _lower_sha256(
                    freeze_source.get("sha256"),
                    label="replay freeze-manifest file hash",
                ),
                "candidate_commit": _full_commit(
                    freeze_binding.get("candidate_commit"),
                    label="replay freeze candidate commit",
                ),
                "origin_main_commit": _full_commit(
                    freeze_binding.get("origin_main_commit"),
                    label="replay freeze origin/main commit",
                ),
                "clock_artifact_sha256": _lower_sha256(
                    freeze_binding.get("clock_artifact_sha256"),
                    label="replay freeze clock artifact hash",
                ),
                "clock_file_sha256": _lower_sha256(
                    freeze_binding.get("clock_file_sha256"),
                    label="replay freeze clock file hash",
                ),
                "routes_sha256": _lower_sha256(
                    freeze_binding.get("routes_sha256"),
                    label="replay freeze route-set hash",
                ),
                "risk_policy_sha256": _lower_sha256(
                    freeze_binding.get("risk_policy_sha256"),
                    label="replay freeze risk-policy set hash",
                ),
                "seed_sha256": _lower_sha256(
                    freeze_binding.get("seed_sha256"),
                    label="replay freeze seed hash",
                ),
                "demo_rules_artifact_sha256": _lower_sha256(
                    freeze_binding.get("demo_rules_artifact_sha256"),
                    label="replay freeze demo-rules artifact hash",
                ),
                "demo_rules_file_sha256": _lower_sha256(
                    rules_source.get("sha256"),
                    label="replay demo-rules file hash",
                ),
                "risk_policy_file_sha256": _lower_sha256(
                    risk_source.get("sha256"),
                    label="replay risk-policy file hash",
                ),
                "demo_account_root": _string(source_roots.get("demo_account_root"), label="replay demo account root"),
                "market_capture_root": _string(
                    source_roots.get("market_capture_root"), label="replay market capture root"
                ),
                "target_capture_tape_hash": _lower_sha256(
                    capture.get("capture_tape_hash"), label="replay target-capture hash"
                ),
                "target_capture_file_sha256": _lower_sha256(
                    target_capture_source.get("sha256"),
                    label="replay target-capture file hash",
                ),
                "natural_batch_ids_sha256": _sequence_sha256(sorted(batch_ids)),
                "natural_batch_count": len(batch_ids),
                "calibration_artifact_sha256": _lower_sha256(
                    config.get("execution_twin_calibration_artifact_sha256"),
                    label="replay calibration hash",
                ),
                "historical_root": _string(outputs.get("historical_root"), label="replay historical root"),
                "paper_root": _string(outputs.get("paper_root"), label="replay paper root"),
                "historical_journal_sha256": _lower_sha256(
                    outputs.get("historical_account_journal_sha256"),
                    label="replay historical journal hash",
                ),
                "paper_journal_sha256": _lower_sha256(
                    outputs.get("paper_account_journal_sha256"),
                    label="replay paper journal hash",
                ),
                # The comparison-scope builder source-reopens this receipt and
                # copies the complete outputs object.  Retaining the complete
                # object here lets the aggregate authority prove that the
                # kernel consumed these exact regenerated outputs rather than
                # merely matching two convenient root strings.
                "replay_outputs": dict(outputs),
                "declared_analysis_completed_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "analysis_source_files": _source_file_references(source_files, label="captured-account replay source"),
                "analysis_source_roots": _source_root_references(
                    [
                        source_roots.get("demo_account_root"),
                        source_roots.get("market_capture_root"),
                    ],
                    label="captured-account replay",
                ),
                **_effective_runtime_config_machine_bindings(
                    receipt,
                    source_file_sha256=effective_config_source.get("sha256"),
                    label="captured-account replay",
                ),
            },
        )
    if role == "execution_twin_drift":
        from .execution_twin_drift import load_execution_twin_drift_receipt

        receipt = load_execution_twin_drift_receipt(path, snapshot=snapshot)
        if receipt.get("execution_twin_drift_gate_passed") is not True or receipt.get("evidence_result") != "supports":
            raise ValueError("execution-twin natural holdout drift did not support the gate")
        training = _mapping(receipt.get("training"), label="drift training")
        configs = _mapping(receipt.get("configs"), label="drift configs")
        roots = _mapping(receipt.get("source_roots"), label="drift source roots")
        journal = _mapping(receipt.get("natural_journal"), label="drift natural journal")
        source_files = _mapping(receipt.get("source_files"), label="drift source files")
        clock_correction = _mapping(receipt.get("clock_correction"), label="drift clock correction")
        clock_coverage = _mapping(clock_correction.get("coverage"), label="drift clock-series coverage")
        clock_contract = _mapping(clock_correction.get("contract"), label="drift clock-series contract")
        clock_application = _mapping(clock_correction.get("application"), label="drift clock-series application")
        if (
            clock_contract.get("uncertainty_is_hard_bound") is not False
            or clock_application.get("uncertainty_is_hard_bound") is not False
            or clock_application.get("interpolation_method") != clock_contract.get("interpolation_method")
            or clock_application.get("uncertainty_method") != clock_contract.get("uncertainty_method")
        ):
            raise ValueError("execution-twin drift overclaims or inconsistently applies clock uncertainty")
        clock_series_file_sha256 = _lower_sha256(
            _mapping(
                source_files.get("natural_clock_offset_series"),
                label="drift clock-series source identity",
            ).get("sha256"),
            label="drift clock-series file hash",
        )
        if clock_series_file_sha256 != _lower_sha256(
            clock_correction.get("series_source_file_sha256"),
            label="drift applied clock-series file hash",
        ):
            raise ValueError("execution-twin drift applied another clock-series file")
        natural_scope = _mapping(receipt.get("natural_scope"), label="drift natural scope")
        natural_capture = _mapping(
            natural_scope.get("natural_target_capture"),
            label="drift natural target capture",
        )
        model_scope = _mapping(receipt.get("model_scope"), label="drift model scope")
        if (
            model_scope.get("passive_queue_calibrated") is not False
            or model_scope.get("immutable_replay_book") is not True
        ):
            raise ValueError("execution-twin drift changed its registered model scope")
        bindings: dict[str, Any] = {
            "declared_analysis_completed_ts_ns": int(receipt.get("observed_ts_ns") or 0),
            "analysis_source_files": _source_file_references(source_files, label="execution-twin drift source"),
            "analysis_source_roots": _source_root_references(
                [
                    roots.get("natural_account_root"),
                    roots.get("natural_market_capture_root"),
                    roots.get("archived_v7_account_root"),
                    roots.get("archived_v7_market_capture_root"),
                ],
                label="execution-twin drift",
            ),
            "account_id": _string(receipt.get("expected_account_id"), label="drift account id"),
            "calibration_artifact_sha256": _lower_sha256(
                training.get("calibration_artifact_sha256"), label="drift calibration hash"
            ),
            "archive_map_artifact_sha256": _lower_sha256(
                training.get("archive_map_artifact_sha256"), label="drift archive-map hash"
            ),
            "baseline_config_artifact_sha256": _lower_sha256(
                configs.get("baseline_artifact_sha256"), label="drift baseline-config hash"
            ),
            "stress_config_artifact_sha256": _lower_sha256(
                configs.get("stress_artifact_sha256"), label="drift stress-config hash"
            ),
            "natural_account_root": _string(roots.get("natural_account_root"), label="drift natural account root"),
            "natural_market_capture_root": _string(
                roots.get("natural_market_capture_root"),
                label="drift natural market-capture root",
            ),
            "natural_journal_sha256": _lower_sha256(journal.get("journal_sha256"), label="drift natural journal hash"),
            "natural_last_event_hash": _lower_sha256(
                journal.get("last_event_hash"), label="drift natural journal head"
            ),
            "natural_target_capture_tape_hash": _lower_sha256(
                natural_capture.get("capture_tape_hash"),
                label="drift natural target-capture tape hash",
            ),
            "natural_target_capture_file_sha256": _lower_sha256(
                natural_capture.get("source_file_sha256"),
                label="drift natural target-capture file hash",
            ),
            "clock_offset_series_file_sha256": clock_series_file_sha256,
            "clock_offset_series_artifact_sha256": _lower_sha256(
                clock_correction.get("series_artifact_sha256"),
                label="drift clock-series artifact hash",
            ),
            "initial_clock_receipt_artifact_sha256": _lower_sha256(
                clock_correction.get("initial_receipt_artifact_sha256"),
                label="drift initial clock-receipt artifact hash",
            ),
            "clock_uncertainty_is_hard_bound": False,
        }
        holdout_scope = _mapping(receipt.get("holdout_scope"), label="drift holdout scope")
        bindings.update(
            {
                "freeze_id": holdout_scope.get("freeze_id"),
                "freeze_artifact_sha256": holdout_scope.get("freeze_artifact_sha256"),
                "freeze_manifest_file_sha256": holdout_scope.get("freeze_manifest_file_sha256"),
                "t0_ns": holdout_scope.get("t0_ns"),
                "t1_ns": holdout_scope.get("t1_ns"),
                "clock_offset_series_artifact_sha256": holdout_scope.get("clock_offset_series_artifact_sha256"),
                "clock_offset_series_file_sha256": holdout_scope.get("clock_offset_series_file_sha256"),
                "clock_offset_series_sample_count": holdout_scope.get("clock_offset_series_sample_count"),
                "clock_offset_series_max_observed_gap_ns": holdout_scope.get("clock_offset_series_max_observed_gap_ns"),
                "clock_offset_series_t0_bracketed": holdout_scope.get("clock_offset_series_t0_bracketed"),
                "clock_offset_series_t1_bracketed": holdout_scope.get("clock_offset_series_t1_bracketed"),
                "natural_batch_ids_sha256": holdout_scope.get("natural_batch_ids_sha256"),
                "safety_batch_ids_sha256": holdout_scope.get("safety_batch_ids_sha256"),
                "safety_batches_excluded": holdout_scope.get("safety_batches_excluded"),
            }
        )
        if (
            bindings["clock_offset_series_artifact_sha256"] != clock_correction.get("series_artifact_sha256")
            or bindings["clock_offset_series_file_sha256"] != clock_correction.get("series_source_file_sha256")
            or bindings["clock_offset_series_sample_count"] != clock_coverage.get("sample_count")
            or bindings["clock_offset_series_max_observed_gap_ns"] != clock_coverage.get("max_observed_gap_ns")
            or bindings["clock_offset_series_t0_bracketed"] is not True
            or bindings["clock_offset_series_t1_bracketed"] is not True
        ):
            raise ValueError("execution-twin drift clock-series bindings are inconsistent")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings=bindings,
        )
    if role == "kernel_parity":
        from .kernel_parity import (
            KERNEL_PARITY_CONTRACT_ID,
            KERNEL_PARITY_EVIDENCE_SCOPE,
            KERNEL_PARITY_SCHEMA_VERSION,
            QUANTITY_ABS_TOLERANCE,
            load_kernel_parity_receipt,
        )

        receipt = load_kernel_parity_receipt(path, snapshot=snapshot)
        if receipt.get("journal_parity_passed") is not True:
            raise ValueError("account-kernel structural parity gate has not passed")
        if (
            receipt.get("schema_version") != KERNEL_PARITY_SCHEMA_VERSION
            or receipt.get("contract_id") != KERNEL_PARITY_CONTRACT_ID
            or receipt.get("evidence_scope") != KERNEL_PARITY_EVIDENCE_SCOPE
            or receipt.get("quantity_abs_tolerance") != QUANTITY_ABS_TOLERANCE
        ):
            raise ValueError("account-kernel parity receipt violates the v4 contract")
        report = receipt.get("report")
        scope = receipt.get("comparison_scope")
        if not isinstance(report, Mapping) or not isinstance(scope, Mapping):
            raise ValueError("account-kernel parity receipt lacks report or comparison scope")
        if report.get("historical_paper_normalized_modeled_execution_exact") is not True:
            raise ValueError("historical-paper modeled execution subgate has not passed")
        batch_ids = scope.get("batch_ids")
        if not isinstance(batch_ids, list) or not batch_ids:
            raise ValueError("account-kernel parity comparison scope is empty")
        sources = _mapping(receipt.get("sources"), label="kernel-parity sources")
        evidence = _mapping(receipt.get("evidence_bindings"), label="kernel-parity evidence bindings")
        epochs = _mapping(receipt.get("epoch_bindings"), label="kernel-parity epoch bindings")
        natural_epoch = _mapping(
            epochs.get("natural_post_reset"),
            label="kernel-parity natural epoch",
        )
        natural_epoch_sources = _mapping(
            natural_epoch.get("sources"),
            label="kernel-parity natural epoch sources",
        )
        repo = _mapping(receipt.get("repo_binding"), label="kernel-parity repository")
        effective_config_source = _mapping(
            evidence.get("effective_runtime_config_bundle_file"),
            label="kernel effective-config source binding",
        )
        scope_replay_receipt = _source_file_reference(
            scope.get("captured_account_replay_receipt"),
            label="kernel scope captured-account replay receipt",
        )
        scope_event_receipt = _source_file_reference(
            scope.get("event_parity_receipt"),
            label="kernel scope event-parity receipt",
        )
        scope_replay_outputs = dict(
            _mapping(
                scope.get("captured_replay_outputs"),
                label="kernel scope captured replay outputs",
            )
        )
        scope_event_provenance = dict(
            _mapping(
                scope.get("event_replay_provenance"),
                label="kernel scope event-replay provenance",
            )
        )
        if scope_event_provenance.get("deployment_valid") is not True:
            raise ValueError("account-kernel parity scope lacks deployment-valid event replay provenance")
        if natural_epoch.get("captured_account_replay_receipt_file_sha256") != scope_replay_receipt["sha256"]:
            raise ValueError("account-kernel parity epoch binds another captured-account replay receipt")
        source_bindings: dict[str, Any] = {}
        kernel_analysis_source_values: dict[str, Any] = {}
        for environment in ("historical", "paper", "demo"):
            source = _mapping(sources.get(environment), label=f"kernel {environment} source")
            source_bindings[f"{environment}_root"] = source.get("root")
            source_bindings[f"{environment}_journal_sha256"] = source.get("normalized_journal_sha256")
            raw_files = source.get("files")
            if not isinstance(raw_files, list) or not raw_files:
                raise ValueError(f"kernel {environment} source lacks raw file identities")
            for index, raw_file in enumerate(raw_files):
                kernel_analysis_source_values[f"journal/{environment}/{index}"] = raw_file
        for name, source in evidence.items():
            kernel_analysis_source_values[f"evidence/{name}"] = source
        kernel_analysis_source_values["comparison_scope/captured_account_replay_receipt"] = scope_replay_receipt
        kernel_analysis_source_values["comparison_scope/event_parity_receipt"] = scope_event_receipt
        kernel_analysis_source_values["comparison_scope"] = _mapping(
            scope.get("scope_file"), label="kernel comparison-scope source"
        )
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "authorized_commit": _full_commit(repo.get("commit"), label="kernel-parity repository commit"),
                "contract_id": receipt.get("contract_id"),
                "schema_version": receipt.get("schema_version"),
                "quantity_abs_tolerance": receipt.get("quantity_abs_tolerance"),
                "natural_batch_ids_sha256": _sequence_sha256(sorted(batch_ids)),
                "natural_batch_count": len(batch_ids),
                "event_parity_receipt_file_sha256": _lower_sha256(
                    _mapping(
                        evidence.get("event_parity_receipt"),
                        label="kernel event-parity binding",
                    ).get("sha256"),
                    label="kernel event-parity file hash",
                ),
                "scope_event_parity_receipt": scope_event_receipt,
                "scope_captured_account_replay_receipt": scope_replay_receipt,
                "scope_captured_replay_outputs": scope_replay_outputs,
                "scope_event_replay_provenance": scope_event_provenance,
                "comparison_scope_artifact_sha256": _lower_sha256(
                    scope.get("scope_artifact_sha256"),
                    label="kernel comparison-scope artifact hash",
                ),
                "calibration_receipt_file_sha256": _lower_sha256(
                    _mapping(
                        evidence.get("twin_calibration_receipt"),
                        label="kernel calibration binding",
                    ).get("sha256"),
                    label="kernel calibration file hash",
                ),
                "fresh_epoch_reset_receipt_file_sha256": _lower_sha256(
                    natural_epoch.get("reset_receipt_file_sha256"),
                    label="kernel fresh-epoch reset receipt file hash",
                ),
                "risk_policy_file_sha256": _lower_sha256(
                    _mapping(
                        evidence.get("risk_policy_file"),
                        label="kernel risk-policy binding",
                    ).get("sha256"),
                    label="kernel risk-policy file hash",
                ),
                "rules_file_sha256": _lower_sha256(
                    _mapping(
                        evidence.get("rules_file"),
                        label="kernel rules binding",
                    ).get("sha256"),
                    label="kernel rules file hash",
                ),
                "comparison_scope_file_sha256": _lower_sha256(
                    _mapping(
                        scope.get("scope_file"),
                        label="kernel comparison-scope binding",
                    ).get("sha256"),
                    label="kernel comparison-scope file hash",
                ),
                "comparison_scope_file_path": _absolute_path(
                    _mapping(
                        scope.get("scope_file"),
                        label="kernel comparison-scope binding",
                    ).get("path"),
                    label="kernel comparison-scope file path",
                ),
                "demo_journal_stream_sha256": _lower_sha256(
                    _mapping(
                        natural_epoch_sources.get("demo"),
                        label="kernel natural demo epoch source",
                    ).get("journal_stream_sha256"),
                    label="kernel natural demo journal stream hash",
                ),
                "declared_analysis_completed_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "analysis_source_files": _source_file_references(
                    kernel_analysis_source_values,
                    label="kernel-parity source",
                ),
                "analysis_source_roots": _source_root_references(
                    [source_bindings[f"{environment}_root"] for environment in ("historical", "paper", "demo")],
                    label="kernel parity",
                ),
                **_effective_runtime_config_machine_bindings(
                    receipt,
                    source_file_sha256=effective_config_source.get("sha256"),
                    label="kernel parity",
                ),
                **source_bindings,
            },
        )
    if role == "event_clock_comparison":
        from .strategy_event_parity import load_strategy_event_parity_receipt
        from .strategy_target_replay import (
            REPLAY_MANIFEST_SCHEMA_VERSION,
            load_offline_target_scheduling_replay_manifest,
        )

        receipt = _load_with_snapshot(
            load_strategy_event_parity_receipt,
            path,
            snapshot=snapshot,
        )
        if receipt.get("strategy_event_replay_gate_passed") is not True:
            raise ValueError("strategy-event replay parity gate has not passed")
        replay_provenance = _mapping(
            receipt.get("replay_provenance"),
            label="strategy-event replay provenance",
        )
        if set(replay_provenance) != {
            "deployment_valid",
            "replay_manifest",
            "canonical_source_capture",
        }:
            raise ValueError("strategy-event replay provenance has invalid fields")
        if replay_provenance.get("deployment_valid") is not True:
            raise ValueError("strategy-event replay parity lacks deployment-valid target-replay provenance")
        replay_manifest = _mapping(
            replay_provenance.get("replay_manifest"),
            label="strategy-event replay manifest identity",
        )
        canonical_capture = _mapping(
            replay_provenance.get("canonical_source_capture"),
            label="strategy-event canonical source capture",
        )
        if set(replay_manifest) != {
            "path",
            "size_bytes",
            "sha256",
            "schema_version",
            "artifact_sha256",
            "created_ts_ns",
        }:
            raise ValueError("strategy-event replay manifest identity has invalid fields")
        if set(canonical_capture) != {
            "path",
            "size_bytes",
            "sha256",
            "device",
            "inode",
            "mtime_ns",
            "mode",
            "uid",
            "nlink",
            "capture_event_count",
            "capture_chain_hash",
            "source_environment",
        }:
            raise ValueError("strategy-event canonical source capture has invalid fields")
        if (
            replay_manifest.get("schema_version") != REPLAY_MANIFEST_SCHEMA_VERSION
            or type(replay_manifest.get("created_ts_ns")) is not int
            or int(replay_manifest["created_ts_ns"]) <= 0
            or canonical_capture.get("source_environment") != "demo"
        ):
            raise ValueError("strategy-event replay provenance is not deployment-valid")
        replay_manifest_reference = _source_file_reference(
            replay_manifest,
            label="strategy-event replay manifest",
        )
        canonical_capture_reference = _source_file_reference(
            canonical_capture,
            label="strategy-event canonical source capture",
        )
        _lower_sha256(
            replay_manifest.get("artifact_sha256"),
            label="strategy-event replay manifest artifact",
        )
        _lower_sha256(
            canonical_capture.get("capture_chain_hash"),
            label="strategy-event canonical source capture chain",
        )
        event_sources = receipt.get("sources")
        if not isinstance(event_sources, Mapping):
            raise ValueError("strategy-event replay parity receipt lacks sources")
        event_counts: dict[str, int] = {}
        decision_outcome_counts: dict[str, int] = {}
        event_derived_output_values: dict[str, Any] = {}
        for environment in ("historical", "paper", "demo"):
            source = cast(Mapping[str, Any], event_sources[environment])
            event_tape = cast(Mapping[str, Any], source["event_tape"])
            decision_tape = cast(Mapping[str, Any], source["decision_tape"])
            event_counts[environment] = int(event_tape["event_count"])
            decision_outcome_counts[environment] = int(decision_tape["outcome_count"])
            event_derived_output_values[f"{environment}/event_tape"] = event_tape
            event_derived_output_values[f"{environment}/decision_tape"] = decision_tape
            event_derived_output_values[f"{environment}/replay_input"] = _mapping(
                source.get("replay_input"),
                label=f"{environment} event-parity replay input",
            )
        replay_input_hashes = {
            str(
                cast(
                    Mapping[str, Any],
                    cast(Mapping[str, Any], event_sources[environment])["replay_input"],
                )["sha256"]
            )
            for environment in ("historical", "paper", "demo")
        }
        if len(replay_input_hashes) != 1:
            raise ValueError("strategy-event replay inputs differ")
        replay_input_sha256 = _lower_sha256(
            next(iter(replay_input_hashes)),
            label="strategy-event replay-input hash",
        )
        if replay_input_sha256 != canonical_capture_reference["sha256"]:
            raise ValueError("strategy-event replay input differs from its canonical source capture")

        # Event parity consumes three of the four per-environment artifacts.
        # Reopen the source-bound manifest once more so central authority can
        # inspect the complete twelve-file derived namespace, including the
        # scheduled-target output that parity does not otherwise consume.
        replay_manifest_path = Path(replay_manifest_reference["path"])
        replay_manifest_snapshot = _file_snapshot(
            replay_manifest_path,
            label="strategy-event replay manifest",
        )
        if replay_manifest_snapshot.sha256 != replay_manifest_reference["sha256"]:
            raise ValueError(
                "strategy-event replay manifest bytes differ from the bound provenance"
            )
        replay_manifest_payload = load_offline_target_scheduling_replay_manifest(
            replay_manifest_path,
            snapshot=replay_manifest_snapshot,
        )
        manifest_environments = _mapping(
            replay_manifest_payload.get("environments"),
            label="strategy-event replay manifest environments",
        )
        for environment in ("historical", "paper", "demo"):
            manifest_environment = _mapping(
                manifest_environments.get(environment),
                label=f"strategy-event replay manifest {environment}",
            )
            for output_role in (
                "event_tape",
                "decision_tape",
                "scheduled_targets",
                "replay_input",
            ):
                event_derived_output_values[f"manifest/{environment}/{output_role}"] = _mapping(
                    manifest_environment.get(output_role),
                    label=(f"strategy-event replay manifest {environment} {output_role}"),
                )
        derived_output_files = _source_file_references(
            event_derived_output_values,
            label="strategy-event derived replay output",
        )
        if len(derived_output_files) != 12:
            raise ValueError("strategy-event replay manifest must bind exactly twelve derived output files")
        check = _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "event_counts": event_counts,
                "decision_outcome_counts": decision_outcome_counts,
                "replay_input_sha256": replay_input_sha256,
                "event_replay_provenance": {
                    "deployment_valid": True,
                    "replay_manifest": dict(replay_manifest),
                    "canonical_source_capture": dict(canonical_capture),
                },
                "event_replay_manifest": dict(replay_manifest),
                "event_canonical_source_capture": dict(canonical_capture),
                "target_replay_output_root": str(Path(replay_manifest_reference["path"]).parent),
                "declared_analysis_completed_ts_ns": int(receipt.get("created_ts_ns") or 0),
                "analysis_source_files": _source_file_references(
                    {
                        "replay_manifest": replay_manifest_reference,
                        "canonical_source_capture": canonical_capture_reference,
                    },
                    label="strategy-event parity source",
                ),
                "analysis_derived_output_files": derived_output_files,
                "analysis_source_roots": [],
            },
        )
        # Preserve the pre-aggregate public diagnostic shape used by callers.
        check["event_counts"] = event_counts
        check["decision_outcome_counts"] = decision_outcome_counts
        return check
    if role in {"venue_accounting_reconciliation", "venue_flatness_snapshot"}:
        from .account_venue_accounting import load_venue_accounting_receipt

        receipt = load_venue_accounting_receipt(path, snapshot=snapshot)
        if receipt.get("account_id") != "bybit-demo-unified":
            raise ValueError("venue-accounting receipt belongs to the wrong demo account")
        if receipt.get("evidence_scope") != "bybit_demo_account_pnl_funding_reconciliation":
            raise ValueError("venue-accounting receipt has the wrong evidence scope")
        gate_field = (
            "venue_accounting_gate_passed"
            if role == "venue_accounting_reconciliation"
            else "final_demo_flatness_gate_passed"
        )
        if receipt.get(gate_field) is not True:
            raise ValueError(f"venue-accounting receipt gate {gate_field!r} has not passed")
        journal = _mapping(receipt.get("journal"), label="venue-accounting journal")
        window = _mapping(receipt.get("query_window_ms"), label="venue-accounting window")
        return _standard_machine_check(
            role=role,
            path=path,
            snapshot=snapshot,
            artifact_sha256=receipt.get("artifact_sha256"),
            bindings={
                "account_id": receipt.get("account_id"),
                "account_root": receipt.get("account_root"),
                "journal_sha256": _lower_sha256(
                    journal.get("normalized_journal_sha256"),
                    label="venue-accounting journal hash",
                ),
                "query_start_ms": int(window.get("start") or 0),
                "query_end_ms": int(window.get("end") or 0),
                "venue_accounting_gate_passed": receipt.get("venue_accounting_gate_passed"),
                "final_demo_flatness_gate_passed": receipt.get("final_demo_flatness_gate_passed"),
                "gate_field": gate_field,
                "sample_counts": dict(_mapping(receipt.get("sample_counts"), label="venue sample counts")),
            },
        )
    receipt = load_reviewed_evidence(
        path,
        expected_role=role,
        snapshot=snapshot,
    )
    return _standard_reviewed_check(role=role, receipt=receipt)


def _machine_validate_snapshot(
    *,
    role: str,
    path: Path,
    now_ns: int,
    snapshot: StableFileSnapshot,
) -> dict[str, Any]:
    if "snapshot" in inspect.signature(_machine_validate_evidence).parameters:
        return _machine_validate_evidence(
            role=role,
            path=path,
            now_ns=now_ns,
            snapshot=snapshot,
        )
    # Compatibility for narrow tests that replace the validator. Production
    # validation always receives the captured descriptor snapshot.
    return _machine_validate_evidence(role=role, path=path, now_ns=now_ns)


def _checks_by_role(
    *,
    evidence: Mapping[str, Mapping[str, Any]],
    machine_checks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    output: dict[str, Mapping[str, Any]] = {}
    for evidence_id, entry in evidence.items():
        role = str(entry.get("role") or "")
        if role in output:
            raise ValueError(f"aggregate cutover evidence repeats role {role!r}")
        check = machine_checks.get(evidence_id)
        if not isinstance(check, Mapping):
            raise ValueError(f"aggregate cutover evidence {evidence_id!r} lacks a check")
        output[role] = check
    if set(output) != ALL_EVIDENCE_ROLES:
        raise ValueError("aggregate cutover evidence does not cover the exact role set")
    return output


def _bindings(checks: Mapping[str, Mapping[str, Any]], role: str) -> Mapping[str, Any]:
    check = checks.get(role)
    if not isinstance(check, Mapping):
        raise ValueError(f"aggregate cutover evidence lacks role {role!r}")
    value = check.get("bindings")
    if not isinstance(value, Mapping):
        raise ValueError(f"aggregate cutover role {role!r} lacks bindings")
    return cast(Mapping[str, Any], value)


def _binding(checks: Mapping[str, Mapping[str, Any]], role: str, field: str) -> Any:
    value = _bindings(checks, role).get(field)
    if value is None or value == "":
        raise ValueError(f"aggregate cutover role {role!r} lacks binding {field!r}")
    return value


def _require_equal_binding(label: str, *values: Any) -> None:
    if not values or any(value is None or value == "" for value in values):
        raise ValueError(f"aggregate cutover binding {label!r} is missing")
    baseline = canonical_json({"value": values[0]})
    if any(canonical_json({"value": value}) != baseline for value in values[1:]):
        raise ValueError(f"aggregate cutover binding {label!r} differs across evidence")


def _freeze_demo_account_id(freeze_bindings: Mapping[str, Any]) -> str:
    account_ids = _mapping(freeze_bindings.get("account_ids"), label="freeze account-id bindings")
    for key in ("demo", "demo_account_id", "bybit_demo"):
        if account_ids.get(key):
            return str(account_ids[key])
    raise ValueError("freeze manifest lacks an explicit demo account id")


def _freeze_root(freeze_bindings: Mapping[str, Any], *, environment: str, kind: str) -> str:
    roots = _mapping(freeze_bindings.get("roots"), label="freeze root bindings")
    nested = roots.get(environment)
    if isinstance(nested, Mapping):
        for key in (kind, f"{kind}_root"):
            if nested.get(key):
                return str(nested[key])
    for key in (
        f"{environment}_{kind}_root",
        f"{environment}_{kind}",
        f"{environment}_account_{kind}_root" if kind != "account" else "",
    ):
        if key and roots.get(key):
            return str(roots[key])
    raise ValueError(f"freeze manifest lacks the {environment} {kind} root")


def _indexed_source_references(values: Any, *, label: str) -> dict[str, str]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"{label} must bind at least one exact source file")
    output: dict[str, str] = {}
    for index, value in enumerate(values):
        reference = _source_file_reference(value, label=f"{label} {index}")
        prior = output.setdefault(reference["path"], reference["sha256"])
        if prior != reference["sha256"]:
            raise ValueError(f"{label} assigns conflicting hashes to {reference['path']}")
    return output


def _merge_source_indexes(target: dict[str, str], source: Mapping[str, str], *, label: str) -> None:
    for path, digest in source.items():
        prior = target.setdefault(path, digest)
        if prior != digest:
            raise ValueError(f"{label} assigns conflicting hashes to {path}")


def _stopped_source_index(stopped: Mapping[str, Any]) -> dict[str, str]:
    roots = stopped.get("old_sealed_root_paths")
    trees = stopped.get("source_trees")
    required_files = stopped.get("old_mutable_files")
    if (
        not isinstance(roots, list)
        or len(roots) != len(_OLD_MUTABLE_ROOT_ROLES)
        or not isinstance(trees, Mapping)
        or set(trees) != set(_OLD_MUTABLE_ROOT_ROLES)
        or not isinstance(required_files, list)
    ):
        raise ValueError("stopped natural epoch lacks its exact source-tree set")
    tree_files: dict[str, str] = {}
    for role, root_value in zip(_OLD_MUTABLE_ROOT_ROLES, roots, strict=True):
        root = _absolute_path(root_value, label=f"stopped {role} root")
        tree = _mapping(trees.get(role), label=f"stopped {role} source tree")
        root_identity = _mapping(tree.get("root_identity"), label=f"stopped {role} root identity")
        if root_identity.get("path") != root:
            raise ValueError(f"stopped {role} source tree names another root")
        entries = tree.get("entries")
        if not isinstance(entries, list):
            raise ValueError(f"stopped {role} source tree lacks exact entries")
        for index, value in enumerate(entries):
            entry = _mapping(value, label=f"stopped {role} entry {index}")
            if entry.get("kind") != "file":
                continue
            relative = str(entry.get("relative_path") or "")
            relative_path = Path(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative_path.as_posix() != relative
            ):
                raise ValueError(f"stopped {role} source tree has an unsafe path")
            path = str(Path(root) / relative_path)
            digest = _lower_sha256(entry.get("sha256"), label=f"stopped {role} entry {index} hash")
            prior = tree_files.setdefault(path, digest)
            if prior != digest:
                raise ValueError(f"stopped source trees conflict for {path}")
    if sorted(tree_files) != required_files:
        raise ValueError("stopped natural epoch required-file set differs from its source trees")
    source_files = _mapping(stopped.get("source_files"), label="stopped natural epoch source files")
    _merge_source_indexes(
        tree_files,
        _indexed_source_references(
            _source_file_references(source_files, label="stopped natural epoch registered source"),
            label="stopped natural epoch registered source",
        ),
        label="stopped natural epoch source set",
    )
    return tree_files


def _sealed_source_analysis_gate(
    *,
    evidence: Mapping[str, Mapping[str, Any]],
    checks: Mapping[str, Mapping[str, Any]],
    stopped: Mapping[str, Any],
    fresh: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> dict[str, bool]:
    """Fail closed unless all offline analyses are exactly source-bound.

    Filesystem mtimes are deliberately not used as timing evidence: they are
    mutable metadata and are not part of most downstream receipt self-hashes.
    Bound timestamps enforce receipt-internal declared chronology only; they do
    not prove wall-clock completion after the seal. The machine-verifiable
    dependency claim comes exclusively from reopened source identities and
    exact receipt/manifest hashes.
    """

    stopped_roots = _source_root_references(
        cast(Sequence[Any], stopped.get("old_sealed_root_paths") or ()),
        label="stopped natural epoch",
    )
    if len(stopped_roots) != len(_OLD_MUTABLE_ROOT_ROLES):
        raise ValueError("stopped natural epoch must bind exactly eleven roots")
    fresh_roots_raw = _mapping(fresh.get("roots"), label="fresh deploy roots")
    if set(fresh_roots_raw) != set(_FRESH_ROOT_ROLES):
        raise ValueError("fresh deploy epoch must bind the exact ten root roles")
    fresh_roots = _source_root_references(
        [_mapping(fresh_roots_raw[role], label=f"fresh {role} root").get("path") for role in _FRESH_ROOT_ROLES],
        label="fresh deploy epoch",
    )
    if len(fresh_roots) != len(_FRESH_ROOT_ROLES):
        raise ValueError("fresh deploy epoch roots are not distinct")
    if any(
        _paths_overlap(left, right)
        for index, left in enumerate(stopped_roots + fresh_roots)
        for right in (stopped_roots + fresh_roots)[index + 1 :]
    ):
        raise ValueError("stopped and fresh epoch roots overlap")

    evidence_by_role = {str(entry.get("role") or ""): entry for entry in evidence.values()}
    for role in _SEALED_SOURCE_ANALYSIS_ROLES:
        entry = _mapping(evidence_by_role.get(role), label=f"{role} evidence identity")
        receipt_path = _absolute_path(entry.get("path"), label=f"{role} evidence receipt")
        if any(_path_within(receipt_path, root) for root in (*stopped_roots, *fresh_roots)):
            raise ValueError(f"sealed-source analysis receipt {role!r} is inside an epoch root")

    declared_seal_created_ts_ns = int(stopped.get("created_ts_ns") or 0)
    if declared_seal_created_ts_ns <= 0:
        raise ValueError("stopped seal lacks a positive declared creation timestamp")
    for role in _SEALED_SOURCE_ANALYSIS_ROLES:
        value = _bindings(checks, role).get("declared_analysis_completed_ts_ns")
        if type(value) is not int or value <= 0:
            raise ValueError(f"sealed-source analysis {role!r} lacks a positive declared completion timestamp")
        if value <= declared_seal_created_ts_ns:
            raise ValueError(f"sealed-source analysis {role!r} has inconsistent declared chronology")

    allowed_exact = _stopped_source_index(stopped)
    registered_preseal = _indexed_source_references(
        freeze.get("registered_preseal_source_files"),
        label="frozen registered pre-seal source",
    )
    _merge_source_indexes(
        allowed_exact,
        registered_preseal,
        label="registered pre-seal source set",
    )
    registered_preseal_roots = _source_root_references(
        cast(Sequence[Any], freeze.get("registered_preseal_source_roots") or ()),
        label="frozen registered pre-seal archive",
    )
    if len(registered_preseal_roots) != 2:
        raise ValueError("frozen V7 archive must bind two distinct roots")
    if any(
        _paths_overlap(preseal, epoch_root)
        for preseal in registered_preseal_roots
        for epoch_root in (*stopped_roots, *fresh_roots)
    ):
        raise ValueError("registered pre-seal archive roots overlap an epoch root")
    if _paths_overlap(*registered_preseal_roots):
        raise ValueError("registered pre-seal archive roots overlap each other")
    replay_bindings = _bindings(checks, "captured_account_replay")
    replay_derived_roots = _source_root_references(
        [replay_bindings.get("historical_root"), replay_bindings.get("paper_root")],
        label="captured-account replay derived output",
    )
    if len(replay_derived_roots) != 2:
        raise ValueError("captured-account replay must bind two distinct derived roots")
    if any(
        _paths_overlap(derived, epoch_root)
        for derived in replay_derived_roots
        for epoch_root in (*stopped_roots, *fresh_roots, *registered_preseal_roots)
    ):
        raise ValueError("captured-account replay derived roots overlap an epoch or pre-seal root")
    if _paths_overlap(*replay_derived_roots):
        raise ValueError("captured-account replay derived roots overlap each other")

    event_bindings = _bindings(checks, "event_clock_comparison")
    event_provenance = _mapping(
        event_bindings.get("event_replay_provenance"),
        label="event-clock replay provenance",
    )
    if event_provenance.get("deployment_valid") is not True:
        raise ValueError("event-clock comparison lacks deployment-valid replay provenance")
    event_manifest = _mapping(
        event_bindings.get("event_replay_manifest"),
        label="event-clock replay manifest",
    )
    event_canonical_capture = _mapping(
        event_bindings.get("event_canonical_source_capture"),
        label="event-clock canonical source capture",
    )
    _require_equal_binding(
        "event replay manifest provenance",
        event_manifest,
        event_provenance.get("replay_manifest"),
    )
    _require_equal_binding(
        "event canonical source-capture provenance",
        event_canonical_capture,
        event_provenance.get("canonical_source_capture"),
    )
    event_manifest_reference = _source_file_reference(
        event_manifest,
        label="event-clock replay manifest",
    )
    event_canonical_capture_reference = _source_file_reference(
        event_canonical_capture,
        label="event-clock canonical source capture",
    )
    _require_equal_binding(
        "event replay input and canonical stopped capture",
        event_bindings.get("replay_input_sha256"),
        event_canonical_capture_reference["sha256"],
    )
    manifest_created_ts_ns = event_manifest.get("created_ts_ns")
    if type(manifest_created_ts_ns) is not int or manifest_created_ts_ns <= declared_seal_created_ts_ns:
        raise ValueError("event-clock target replay manifest has inconsistent declared chronology")
    target_replay_root = _absolute_path(
        event_bindings.get("target_replay_output_root"),
        label="event-clock target-replay output root",
    )
    if target_replay_root != str(Path(event_manifest_reference["path"]).parent):
        raise ValueError("event-clock replay manifest is outside its declared output root")
    protected_roots = (
        *stopped_roots,
        *fresh_roots,
        *registered_preseal_roots,
        *replay_derived_roots,
    )
    if any(_paths_overlap(target_replay_root, root) for root in protected_roots):
        raise ValueError("event-clock target-replay output root overlaps an epoch, pre-seal, or captured-replay root")
    event_derived_outputs = _indexed_source_references(
        event_bindings.get("analysis_derived_output_files"),
        label="event-clock derived output set",
    )
    if len(event_derived_outputs) != 12:
        raise ValueError("event-clock comparison must bind exactly twelve target-replay output files")
    for derived_path in event_derived_outputs:
        if not _path_within(derived_path, target_replay_root):
            raise ValueError(f"event-clock derived output {derived_path!r} is outside its target-replay root")
        if any(_path_within(derived_path, root) for root in protected_roots):
            raise ValueError(f"event-clock derived output {derived_path!r} is inside a protected root")

    kernel_bindings = _bindings(checks, "kernel_parity")
    kernel_scope_path = _absolute_path(
        kernel_bindings.get("comparison_scope_file_path"),
        label="kernel comparison-scope file",
    )
    kernel_scope_hash = _lower_sha256(
        kernel_bindings.get("comparison_scope_file_sha256"),
        label="kernel comparison-scope file hash",
    )
    if any(_path_within(kernel_scope_path, root) for root in protected_roots):
        raise ValueError("kernel comparison-scope file is inside an epoch, pre-seal, or derived replay root")
    scope_replay_receipt = _source_file_reference(
        kernel_bindings.get("scope_captured_account_replay_receipt"),
        label="kernel scope captured-account replay receipt",
    )
    scope_event_receipt = _source_file_reference(
        kernel_bindings.get("scope_event_parity_receipt"),
        label="kernel scope event-parity receipt",
    )
    replay_evidence = _mapping(
        evidence_by_role.get("captured_account_replay"),
        label="captured-account replay evidence",
    )
    event_evidence = _mapping(
        evidence_by_role.get("event_clock_comparison"),
        label="event-clock comparison evidence",
    )
    _require_equal_binding(
        "kernel scope captured-account replay receipt path",
        scope_replay_receipt["path"],
        _absolute_path(
            replay_evidence.get("path"),
            label="captured-account replay evidence path",
        ),
    )
    _require_equal_binding(
        "kernel scope captured-account replay receipt hash",
        scope_replay_receipt["sha256"],
        _lower_sha256(
            replay_evidence.get("sha256"),
            label="captured-account replay evidence hash",
        ),
    )
    _require_equal_binding(
        "kernel scope event-parity receipt path",
        scope_event_receipt["path"],
        _absolute_path(
            event_evidence.get("path"),
            label="event-clock comparison evidence path",
        ),
    )
    _require_equal_binding(
        "kernel scope event-parity receipt hash",
        scope_event_receipt["sha256"],
        _lower_sha256(
            event_evidence.get("sha256"),
            label="event-clock comparison evidence hash",
        ),
    )
    _require_equal_binding(
        "kernel scope captured-replay outputs",
        kernel_bindings.get("scope_captured_replay_outputs"),
        replay_bindings.get("replay_outputs"),
    )
    _require_equal_binding(
        "kernel scope event-replay provenance",
        kernel_bindings.get("scope_event_replay_provenance"),
        event_provenance,
    )
    for environment in ("historical", "paper"):
        _require_equal_binding(
            f"{environment} replay derived root",
            replay_bindings.get(f"{environment}_root"),
            kernel_bindings.get(f"{environment}_root"),
        )
        _require_equal_binding(
            f"{environment} replay derived journal",
            replay_bindings.get(f"{environment}_journal_sha256"),
            kernel_bindings.get(f"{environment}_journal_sha256"),
        )

    sealed_source_receipts: dict[str, tuple[str, str]] = {}
    for role in _SEALED_SOURCE_ANALYSIS_ROLES:
        entry = evidence_by_role[role]
        sealed_source_receipts[_absolute_path(entry.get("path"), label=f"{role} receipt")] = (
            role,
            _lower_sha256(entry.get("sha256"), label=f"{role} receipt hash"),
        )

    stopped_root_set = set(stopped_roots)
    receipt_dependencies_by_role: dict[str, set[str]] = {}
    for role in _SEALED_SOURCE_ANALYSIS_ROLES:
        bindings = _bindings(checks, role)
        sources = _indexed_source_references(bindings.get("analysis_source_files"), label=f"{role} source set")
        receipt_dependencies: set[str] = set()
        for path, digest in sources.items():
            expected = allowed_exact.get(path)
            if expected is not None:
                if digest != expected:
                    raise ValueError(
                        f"sealed-source analysis {role!r} source {path!r} differs from its registered identity"
                    )
                continue
            dependency = sealed_source_receipts.get(path)
            if dependency is not None:
                dependency_role, dependency_hash = dependency
                if dependency_role not in _SEALED_SOURCE_ANALYSIS_DEPENDENCIES[role] or digest != dependency_hash:
                    raise ValueError(
                        f"sealed-source analysis {role!r} has an invalid receipt dependency {dependency_role!r}"
                    )
                receipt_dependencies.add(dependency_role)
                continue
            if (
                role == "event_clock_comparison"
                and path == event_manifest_reference["path"]
                and digest == event_manifest_reference["sha256"]
            ):
                # This is a derived manifest, not raw sealed input.  Its loader
                # source-reopens the canonical stopped capture plus all twelve
                # outputs; the namespace checks above keep it outside every
                # protected epoch and replay root.
                continue
            if any(_path_within(path, root) for root in registered_preseal_roots):
                continue
            if (
                role in {"kernel_parity", "natural_tape_sufficiency"}
                and "captured_account_replay" in _SEALED_SOURCE_ANALYSIS_DEPENDENCIES[role]
                and any(_path_within(path, root) for root in replay_derived_roots)
            ):
                continue
            if role == "kernel_parity" and path == kernel_scope_path and digest == kernel_scope_hash:
                continue
            raise ValueError(f"sealed-source analysis {role!r} source {path!r} is not registered or dependency-bound")

        required_dependencies = _SEALED_SOURCE_ANALYSIS_DEPENDENCIES[role]
        if receipt_dependencies != required_dependencies:
            missing = sorted(required_dependencies - receipt_dependencies)
            extra = sorted(receipt_dependencies - required_dependencies)
            raise ValueError(
                f"sealed-source analysis {role!r} receipt dependencies differ (missing={missing}, extra={extra})"
            )
        receipt_dependencies_by_role[role] = receipt_dependencies

        roots = bindings.get("analysis_source_roots")
        if not isinstance(roots, list):
            raise ValueError(f"sealed-source analysis {role!r} lacks its exact source-root set")
        for index, value in enumerate(roots):
            root = _absolute_path(value, label=f"{role} source root {index}")
            if (
                root in stopped_root_set
                or root in registered_preseal_roots
                or (role == "kernel_parity" and root in replay_derived_roots)
            ):
                continue
            raise ValueError(f"sealed-source analysis {role!r} root {root!r} is not a registered immutable source root")

    return {
        "analysis_receipts_outside_epoch_roots": True,
        "declared_analysis_chronology_consistent": True,
        "analysis_sources_reopened_with_exact_hashes": True,
        "analysis_dependency_receipts_exactly_linked": all(
            receipt_dependencies_by_role[role] == _SEALED_SOURCE_ANALYSIS_DEPENDENCIES[role]
            for role in _SEALED_SOURCE_ANALYSIS_ROLES
        ),
    }


def _validate_aggregate_cross_bindings(
    *,
    authorized_commit: str,
    evidence: Mapping[str, Mapping[str, Any]],
    machine_checks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove that individually valid receipts describe one cutover epoch.

    Keep all inter-artifact joins here. Loader-specific validation belongs in
    ``_machine_validate_evidence``; this function prevents a collection of
    individually green receipts from different commits, windows, or roots from
    being promoted as one evidence pack.
    """

    checks = _checks_by_role(evidence=evidence, machine_checks=machine_checks)
    freeze = _bindings(checks, "natural_cutover_freeze_manifest")
    natural = _bindings(checks, "natural_tape_sufficiency")
    replay = _bindings(checks, "captured_account_replay")
    drift = _bindings(checks, "execution_twin_drift")
    calibration = _bindings(checks, "execution_twin_calibration")
    candidate = _bindings(checks, "candidate_rule_coverage")
    rules = _bindings(checks, "demo_rule_probe")
    event = _bindings(checks, "event_clock_comparison")
    kernel = _bindings(checks, "kernel_parity")
    stopped = _bindings(checks, "stopped_natural_epoch")
    fresh = _bindings(checks, "fresh_deploy_epoch")
    venue = _bindings(checks, "venue_accounting_reconciliation")
    flatness = _bindings(checks, "venue_flatness_snapshot")

    passed_checks: dict[str, bool] = {}

    def equal(name: str, *values: Any) -> None:
        _require_equal_binding(name, *values)
        passed_checks[name] = True

    equal(
        "authorized_commit",
        authorized_commit,
        freeze.get("authorized_commit"),
        kernel.get("authorized_commit"),
        stopped.get("candidate_commit"),
        fresh.get("candidate_commit"),
    )
    equal(
        "natural_window_t0",
        freeze.get("t0_ns"),
        natural.get("t0_ns"),
        replay.get("t0_ns"),
        drift.get("t0_ns"),
        stopped.get("t0_ns"),
    )
    equal(
        "natural_window_t1",
        freeze.get("t1_ns"),
        natural.get("t1_ns"),
        replay.get("t1_ns"),
        drift.get("t1_ns"),
        stopped.get("t1_ns"),
    )
    equal(
        "natural_freeze_id",
        freeze.get("freeze_id"),
        natural.get("freeze_id"),
        replay.get("freeze_id"),
        drift.get("freeze_id"),
        stopped.get("freeze_id"),
        fresh.get("freeze_id"),
    )
    stopped_freeze = _mapping(
        stopped.get("freeze_manifest"),
        label="stopped natural epoch freeze-manifest identity",
    )
    equal(
        "stopped_epoch_canonical_freeze_path",
        freeze.get("freeze_manifest_path"),
        stopped_freeze.get("path"),
    )
    equal(
        "stopped_epoch_canonical_freeze_file",
        freeze.get("receipt_file_sha256"),
        stopped_freeze.get("sha256"),
    )
    equal(
        "stopped_epoch_canonical_freeze_artifact",
        checks["natural_cutover_freeze_manifest"].get("artifact_sha256"),
        stopped_freeze.get("artifact_sha256"),
    )
    fresh_seal = _mapping(
        fresh.get("stopped_epoch_seal"),
        label="fresh deploy stopped-epoch seal identity",
    )
    equal(
        "stopped_epoch_seal_artifact",
        checks["stopped_natural_epoch"].get("artifact_sha256"),
        stopped.get("seal_artifact_sha256"),
    )
    equal(
        "fresh_epoch_stopped_seal_path",
        stopped.get("manifest_path"),
        fresh_seal.get("path"),
    )
    equal(
        "fresh_epoch_stopped_seal_file",
        stopped.get("receipt_file_sha256"),
        fresh_seal.get("sha256"),
    )
    equal(
        "fresh_epoch_old_sealed_root_paths",
        stopped.get("old_sealed_root_paths"),
        fresh.get("old_sealed_root_paths"),
    )
    equal(
        "fresh_epoch_old_sealed_root_path_set_hash",
        stopped.get("old_sealed_root_paths_sha256"),
        fresh.get("old_sealed_root_paths_sha256"),
    )
    stopped_paths = stopped.get("old_sealed_paths")
    if not isinstance(stopped_paths, list) or len(stopped_paths) != 11:
        raise ValueError("stopped natural epoch does not bind exactly eleven old mutable roots")
    expected_old_root_roles = [
        "demo_account",
        "demo_inbox",
        "demo_capture",
        "paper_account",
        "paper_inbox",
        "paper_capture",
        "long_demo",
        "long_paper",
        "continuous_demo",
        "continuous_paper",
        "natural_evidence",
    ]
    if stopped.get("old_mutable_root_roles") != expected_old_root_roles:
        raise ValueError("stopped natural epoch does not bind the exact registered old-root roles")
    stopped_path_values: list[str] = []
    for index, identity in enumerate(stopped_paths):
        path_identity = _mapping(
            identity,
            label=f"stopped natural epoch old path {index}",
        )
        stopped_path_values.append(
            _string(
                path_identity.get("path"),
                label=f"stopped natural epoch old path {index}",
            )
        )
    if stopped.get("old_sealed_root_paths") != stopped_path_values:
        raise ValueError("stopped natural epoch root paths differ from its source-tree identities")
    if stopped.get("old_sealed_root_paths_sha256") != _sequence_sha256(stopped_path_values):
        raise ValueError("stopped natural epoch root path-set hash is invalid")
    fresh_path_values = fresh.get("old_sealed_root_paths")
    if not isinstance(fresh_path_values, list) or len(fresh_path_values) != 11:
        raise ValueError("fresh deploy epoch does not bind exactly eleven old mutable roots")
    if fresh.get("old_sealed_root_paths_sha256") != _sequence_sha256(fresh_path_values):
        raise ValueError("fresh deploy epoch root path-set hash is invalid")
    if len(set(stopped_path_values)) != len(stopped_path_values):
        raise ValueError("stopped natural epoch repeats an old mutable root")
    frozen_account_roots = [
        _freeze_root(freeze, environment=environment, kind=kind)
        for environment in ("demo", "paper")
        for kind in ("account", "inbox", "capture")
    ]
    if stopped_path_values[: len(frozen_account_roots)] != frozen_account_roots:
        raise ValueError("stopped natural epoch account roots differ from the canonical freeze")
    equal(
        "stopped_epoch_origin_main",
        freeze.get("origin_main_commit"),
        stopped.get("origin_main_commit"),
    )
    if stopped.get("interval") != "half_open_[t0,t1)":
        raise ValueError("stopped natural epoch has the wrong tape interval")
    old_mutable_files = stopped.get("old_mutable_files")
    if (
        not isinstance(old_mutable_files, list)
        or not old_mutable_files
        or old_mutable_files != sorted(old_mutable_files)
        or len(set(old_mutable_files)) != len(old_mutable_files)
        or any(not isinstance(value, str) or not value.startswith("/") for value in old_mutable_files)
    ):
        raise ValueError("stopped natural epoch lacks an exact sorted old-file set")
    if stopped.get("execution_authorization") != "not_granted":
        raise ValueError("stopped natural epoch grants execution authority")
    if fresh.get("execution_authorization") != "not_granted":
        raise ValueError("fresh deploy epoch grants execution authority")
    if stopped.get("all_units_stopped") is not True:
        raise ValueError("stopped natural epoch does not bind an all-units-stopped seal")
    for field in ("source_files", "source_trees", "tape_semantics", "service_state"):
        value = stopped.get(field)
        if not isinstance(value, Mapping) or not value:
            raise ValueError(f"stopped natural epoch lacks bound {field}")
    if set(cast(Mapping[str, Any], stopped["source_trees"])) != set(expected_old_root_roles):
        raise ValueError("stopped natural epoch source-tree roles differ from its old-root roles")
    if not isinstance(fresh.get("roots"), Mapping) or not fresh.get("roots"):
        raise ValueError("fresh deploy epoch lacks bound root identities")
    if not isinstance(fresh.get("late_environment"), Mapping) or not fresh.get("late_environment"):
        raise ValueError("fresh deploy epoch lacks its late environment map")
    stopped_created_ts_ns = int(stopped.get("created_ts_ns") or 0)
    fresh_created_ts_ns = int(fresh.get("created_ts_ns") or 0)
    if stopped_created_ts_ns <= 0 or fresh_created_ts_ns <= stopped_created_ts_ns:
        raise ValueError("fresh deploy epoch has inconsistent declared chronology")
    passed_checks["stopped_epoch_all_units_stopped"] = True
    passed_checks["stopped_epoch_old_namespace_source_reopened"] = True
    passed_checks["fresh_epoch_exactly_links_stopped_seal"] = True
    passed_checks["declared_fresh_epoch_chronology_consistent"] = True
    passed_checks["fresh_epoch_execution_authorization_not_granted"] = True
    passed_checks.update(
        _sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=stopped,
            fresh=fresh,
            freeze=freeze,
        )
    )
    equal(
        "natural_freeze_artifact",
        checks["natural_cutover_freeze_manifest"].get("artifact_sha256"),
        natural.get("freeze_artifact_sha256"),
        replay.get("freeze_artifact_sha256"),
        drift.get("freeze_artifact_sha256"),
    )
    equal(
        "natural_freeze_file",
        freeze.get("receipt_file_sha256"),
        replay.get("freeze_manifest_file_sha256"),
        drift.get("freeze_manifest_file_sha256"),
    )
    equal(
        "natural_freeze_candidate_commit",
        freeze.get("authorized_commit"),
        natural.get("candidate_commit"),
        replay.get("candidate_commit"),
    )
    equal(
        "natural_freeze_base_commit",
        freeze.get("origin_main_commit"),
        natural.get("origin_main_commit"),
        replay.get("origin_main_commit"),
    )
    for field, label in (
        ("effective_runtime_config_bundle_file_sha256", "effective_config_bundle_file"),
        (
            "effective_runtime_config_bundle_artifact_sha256",
            "effective_config_bundle_artifact",
        ),
        ("effective_runtime_config_validator", "effective_config_validator"),
        ("effective_runtime_config_freeze_path", "effective_config_freeze_path"),
        (
            "effective_runtime_config_freeze_file_sha256",
            "effective_config_freeze_file",
        ),
        (
            "effective_runtime_config_freeze_artifact_sha256",
            "effective_config_freeze_artifact",
        ),
        ("effective_runtime_config_freeze_id", "effective_config_freeze_id"),
        (
            "effective_runtime_config_run_config_path",
            "effective_config_run_config_path",
        ),
        (
            "effective_runtime_config_run_config_file_sha256",
            "effective_config_run_config_file",
        ),
        (
            "effective_runtime_config_run_config_artifact_sha256",
            "effective_config_run_config_artifact",
        ),
        (
            "effective_runtime_config_candidate_path",
            "effective_config_candidate_path",
        ),
        (
            "effective_runtime_config_candidate_file_sha256",
            "effective_config_candidate_file",
        ),
        (
            "effective_runtime_config_candidate_artifact_sha256",
            "effective_config_candidate_artifact",
        ),
        ("effective_runtime_config_t0_ns", "effective_config_window_t0"),
        ("effective_runtime_config_t1_ns", "effective_config_window_t1"),
        (
            "effective_runtime_config_candidate_commit",
            "effective_config_candidate_commit",
        ),
        (
            "effective_runtime_config_origin_main_commit",
            "effective_config_origin_main_commit",
        ),
        (
            "effective_runtime_config_target_capture_path",
            "effective_config_target_capture_path",
        ),
    ):
        equal(label, natural.get(field), replay.get(field), kernel.get(field))
    equal(
        "effective_config_freeze_matches_canonical_path",
        freeze.get("freeze_manifest_path"),
        natural.get("effective_runtime_config_freeze_path"),
    )
    equal(
        "effective_config_freeze_matches_canonical_file",
        freeze.get("receipt_file_sha256"),
        natural.get("effective_runtime_config_freeze_file_sha256"),
    )
    equal(
        "effective_config_freeze_matches_canonical_artifact",
        checks["natural_cutover_freeze_manifest"].get("artifact_sha256"),
        natural.get("effective_runtime_config_freeze_artifact_sha256"),
    )
    equal(
        "effective_config_freeze_matches_canonical_id",
        freeze.get("freeze_id"),
        natural.get("effective_runtime_config_freeze_id"),
    )
    equal(
        "effective_config_candidate_matches_canonical_path",
        freeze.get("candidate_universe_path"),
        natural.get("effective_runtime_config_candidate_path"),
    )
    equal(
        "effective_config_candidate_matches_canonical_file",
        freeze.get("candidate_universe_file_sha256"),
        natural.get("effective_runtime_config_candidate_file_sha256"),
    )
    equal(
        "effective_config_candidate_matches_canonical_artifact",
        freeze.get("candidate_universe_artifact_sha256"),
        natural.get("effective_runtime_config_candidate_artifact_sha256"),
    )
    equal(
        "effective_config_window_matches_canonical_t0",
        freeze.get("t0_ns"),
        natural.get("effective_runtime_config_t0_ns"),
    )
    equal(
        "effective_config_window_matches_canonical_t1",
        freeze.get("t1_ns"),
        natural.get("effective_runtime_config_t1_ns"),
    )
    equal(
        "effective_config_commit_matches_canonical_candidate",
        freeze.get("authorized_commit"),
        natural.get("effective_runtime_config_candidate_commit"),
    )
    equal(
        "effective_config_commit_matches_canonical_base",
        freeze.get("origin_main_commit"),
        natural.get("effective_runtime_config_origin_main_commit"),
    )
    equal(
        "deterministic_clock_artifact",
        freeze.get("clock_artifact_sha256"),
        natural.get("clock_artifact_sha256"),
        replay.get("clock_artifact_sha256"),
    )
    equal(
        "deterministic_clock_file",
        freeze.get("clock_file_sha256"),
        natural.get("clock_file_sha256"),
        replay.get("clock_file_sha256"),
    )
    equal(
        "natural_clock_series_initial_receipt",
        freeze.get("clock_artifact_sha256"),
        drift.get("initial_clock_receipt_artifact_sha256"),
    )
    if (
        drift.get("clock_offset_series_t0_bracketed") is not True
        or drift.get("clock_offset_series_t1_bracketed") is not True
        or int(drift.get("clock_offset_series_sample_count") or 0) < 2
        or int(drift.get("clock_offset_series_max_observed_gap_ns") or 0) <= 0
        or drift.get("clock_uncertainty_is_hard_bound") is not False
    ):
        raise ValueError("natural clock-offset series does not cover the frozen window")
    passed_checks["natural_clock_offset_series_source_reopened"] = True
    passed_checks["natural_clock_uncertainty_not_overclaimed"] = True
    equal(
        "runtime_routes",
        freeze.get("routes_sha256"),
        replay.get("routes_sha256"),
    )
    equal(
        "runtime_risk_policy_set",
        freeze.get("risk_policy_sha256"),
        replay.get("risk_policy_sha256"),
    )
    equal(
        "runtime_seed",
        freeze.get("seed_sha256"),
        replay.get("seed_sha256"),
    )
    equal(
        "demo_account_id",
        _freeze_demo_account_id(freeze),
        natural.get("account_id"),
        drift.get("account_id"),
        calibration.get("account_id"),
        venue.get("account_id"),
        flatness.get("account_id"),
    )
    equal(
        "candidate_universe",
        freeze.get("candidate_universe_artifact_sha256"),
        candidate.get("candidate_universe_artifact_sha256"),
        rules.get("candidate_universe_artifact_sha256"),
    )
    equal(
        "candidate_rule_coverage",
        freeze.get("rule_coverage_artifact_sha256"),
        checks["candidate_rule_coverage"].get("artifact_sha256"),
    )
    equal(
        "candidate_symbol_set",
        candidate.get("symbol_set_sha256"),
        rules.get("symbol_set_sha256"),
    )
    equal(
        "demo_rules",
        freeze.get("demo_rules_artifact_sha256"),
        candidate.get("demo_rules_artifact_sha256"),
        replay.get("demo_rules_artifact_sha256"),
        checks["demo_rule_probe"].get("artifact_sha256"),
    )
    equal(
        "demo_rules_file",
        freeze.get("demo_rules_file_sha256"),
        candidate.get("demo_rules_file_sha256"),
        replay.get("demo_rules_file_sha256"),
        kernel.get("rules_file_sha256"),
    )
    equal(
        "runtime_risk_policy_file",
        replay.get("risk_policy_file_sha256"),
        kernel.get("risk_policy_file_sha256"),
    )
    equal(
        "v7_calibration",
        freeze.get("calibration_artifact_sha256"),
        checks["execution_twin_calibration"].get("artifact_sha256"),
        replay.get("calibration_artifact_sha256"),
        drift.get("calibration_artifact_sha256"),
    )
    equal(
        "v7_archive_map",
        freeze.get("archive_map_artifact_sha256"),
        drift.get("archive_map_artifact_sha256"),
    )
    equal(
        "baseline_twin_config",
        freeze.get("baseline_config_artifact_sha256"),
        drift.get("baseline_config_artifact_sha256"),
    )
    equal(
        "stress_twin_config",
        freeze.get("stress_config_artifact_sha256"),
        drift.get("stress_config_artifact_sha256"),
    )
    equal(
        "natural_target_capture",
        natural.get("target_capture_tape_hash"),
        replay.get("target_capture_tape_hash"),
        drift.get("natural_target_capture_tape_hash"),
    )
    equal(
        "natural_target_capture_bytes",
        natural.get("target_capture_file_sha256"),
        replay.get("target_capture_file_sha256"),
        drift.get("natural_target_capture_file_sha256"),
        event.get("replay_input_sha256"),
    )
    equal(
        "natural_batch_scope",
        natural.get("natural_batch_ids_sha256"),
        replay.get("natural_batch_ids_sha256"),
        drift.get("natural_batch_ids_sha256"),
        kernel.get("natural_batch_ids_sha256"),
    )
    equal(
        "natural_batch_count",
        natural.get("natural_batch_count"),
        replay.get("natural_batch_count"),
        kernel.get("natural_batch_count"),
    )
    equal(
        "registered_safety_scope",
        natural.get("safety_batch_ids_sha256"),
        drift.get("safety_batch_ids_sha256"),
    )
    if drift.get("safety_batches_excluded") is not True:
        raise ValueError("execution-twin drift did not exclude registered safety batches")
    passed_checks["safety_batches_excluded_from_drift"] = True
    equal(
        "captured_account_replay",
        natural.get("account_replay_artifact_sha256"),
        checks["captured_account_replay"].get("artifact_sha256"),
    )
    equal(
        "venue_accounting",
        natural.get("venue_accounting_artifact_sha256"),
        checks["venue_accounting_reconciliation"].get("artifact_sha256"),
        checks["venue_flatness_snapshot"].get("artifact_sha256"),
    )
    equal(
        "venue_receipt_path",
        evidence[
            next(
                evidence_id
                for evidence_id, entry in evidence.items()
                if entry.get("role") == "venue_accounting_reconciliation"
            )
        ].get("path"),
        evidence[
            next(
                evidence_id for evidence_id, entry in evidence.items() if entry.get("role") == "venue_flatness_snapshot"
            )
        ].get("path"),
    )
    equal(
        "demo_account_root",
        _freeze_root(freeze, environment="demo", kind="account"),
        natural.get("demo_account_root"),
        replay.get("demo_account_root"),
        drift.get("natural_account_root"),
        venue.get("account_root"),
        flatness.get("account_root"),
        kernel.get("demo_root"),
    )
    equal(
        "natural_market_capture_root",
        _freeze_root(freeze, environment="demo", kind="capture"),
        replay.get("market_capture_root"),
        drift.get("natural_market_capture_root"),
    )
    equal(
        "historical_replay_root",
        replay.get("historical_root"),
        kernel.get("historical_root"),
    )
    equal("paper_replay_root", replay.get("paper_root"), kernel.get("paper_root"))
    equal(
        "historical_replay_journal",
        replay.get("historical_journal_sha256"),
        kernel.get("historical_journal_sha256"),
    )
    equal(
        "paper_replay_journal",
        replay.get("paper_journal_sha256"),
        kernel.get("paper_journal_sha256"),
    )
    equal(
        "demo_journal",
        venue.get("journal_sha256"),
        flatness.get("journal_sha256"),
        kernel.get("demo_journal_sha256"),
    )
    equal(
        "demo_journal_stream",
        drift.get("natural_journal_sha256"),
        kernel.get("demo_journal_stream_sha256"),
    )
    equal(
        "event_parity_receipt",
        event.get("receipt_file_sha256"),
        kernel.get("event_parity_receipt_file_sha256"),
    )
    equal(
        "calibration_receipt_file",
        calibration.get("receipt_file_sha256"),
        kernel.get("calibration_receipt_file_sha256"),
    )
    equal(
        "fresh_epoch_reset_receipt_file",
        freeze.get("reset_receipt_file_sha256"),
        kernel.get("fresh_epoch_reset_receipt_file_sha256"),
    )
    if freeze.get("fresh_roots_verified_at_reset") is not True:
        raise ValueError("frozen reset receipt did not verify fresh roots at reset time")
    passed_checks["fresh_roots_verified_at_reset"] = True
    equal(
        "paper_owner_first_review",
        freeze.get("paper_owner_first_artifact_sha256"),
        checks["paper_owner_start_sequence"].get("artifact_sha256"),
    )
    equal(
        "demo_owner_first_review",
        freeze.get("demo_owner_first_artifact_sha256"),
        checks["demo_owner_start_sequence"].get("artifact_sha256"),
    )

    t0_ns = int(freeze["t0_ns"])
    t1_ns = int(freeze["t1_ns"])
    if (
        int(venue.get("query_start_ms") or 0) * 1_000_000 > t0_ns
        or int(venue.get("query_end_ms") or 0) * 1_000_000 < t1_ns
    ):
        raise ValueError("venue-accounting query does not cover the frozen natural window")
    passed_checks["venue_query_covers_natural_window"] = True

    calibration_artifact = str(checks["execution_twin_calibration"]["artifact_sha256"])
    forbidden_training_aliases = {
        str(checks[role]["artifact_sha256"])
        for role in (
            "natural_tape_sufficiency",
            "captured_account_replay",
            "execution_twin_drift",
        )
    }
    if calibration_artifact in forbidden_training_aliases:
        raise ValueError("V7 calibration artifact aliases natural holdout evidence")
    passed_checks["v7_training_artifact_is_not_natural_evidence"] = True

    binding_payload = {
        role: {
            "artifact_sha256": check.get("artifact_sha256"),
            "bindings": check.get("bindings"),
        }
        for role, check in sorted(checks.items())
    }
    return {
        "schema_version": AGGREGATE_CHECK_SCHEMA_VERSION,
        "validator": AGGREGATE_CHECK_VALIDATOR,
        "status": "passed",
        "checks": passed_checks,
        "evidence_set_sha256": hashlib.sha256(canonical_json(binding_payload)).hexdigest(),
    }


def build_authorization_receipt(
    assessment: Mapping[str, Any],
    *,
    assessment_path: str | Path,
    repo_root: str | Path,
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
    assessment_snapshot = _file_snapshot(assessment_file, label="cutover assessment")
    assessment_identity = _file_identity(
        assessment_file,
        snapshot=assessment_snapshot,
        label="cutover assessment",
    )
    on_disk_assessment = _validate_assessment_structure(
        _load_json_object(
            assessment_file,
            label="cutover assessment",
            snapshot=assessment_snapshot,
        )
    )
    if (
        _file_identity(
            assessment_file,
            snapshot=_file_snapshot(assessment_file, label="cutover assessment"),
            label="cutover assessment",
        )
        != assessment_identity
    ):
        raise ValueError("cutover assessment changed while being validated")
    if canonical_json(on_disk_assessment) != canonical_json(normalized):
        raise ValueError("cutover assessment argument does not match the bound assessment file")
    evidence_output: dict[str, dict[str, Any]] = {}
    machine_checks: dict[str, dict[str, Any]] = {}
    for evidence_id, entry in normalized["evidence"].items():
        path = _strict_file(entry["path"], label=f"cutover evidence {evidence_id!r}")
        evidence_label = f"cutover evidence {evidence_id!r}"
        snapshot = _file_snapshot(path, label=evidence_label)
        identity = _file_identity(
            path,
            snapshot=snapshot,
            label=evidence_label,
        )
        machine_check = _machine_validate_snapshot(
            role=entry["role"],
            path=path,
            now_ns=issued_ts_ns,
            snapshot=snapshot,
        )
        final_identity = _file_identity(
            path,
            snapshot=_file_snapshot(path, label=evidence_label),
            label=evidence_label,
        )
        if final_identity != identity:
            raise ValueError(f"cutover evidence {evidence_id!r} changed during validation")
        evidence_output[evidence_id] = {
            "role": entry["role"],
            "claim": entry["claim"],
            **identity,
        }
        machine_checks[evidence_id] = machine_check

    resolved_roles_by_path: dict[str, set[str]] = {}
    for entry in evidence_output.values():
        resolved_roles_by_path.setdefault(str(entry["path"]), set()).add(str(entry["role"]))
    for evidence_path, roles in resolved_roles_by_path.items():
        if len(roles) > 1 and frozenset(roles) != _VENUE_RECEIPT_ROLES:
            raise ValueError(
                f"resolved evidence path {evidence_path!r} is reused by incompatible roles: {sorted(roles)}"
            )

    aggregate_check = _validate_aggregate_cross_bindings(
        authorized_commit=str(normalized["authorized_commit"]),
        evidence=evidence_output,
        machine_checks=machine_checks,
    )
    require_clean_authorized_checkout(repo_root, str(normalized["authorized_commit"]))
    checks_by_role = _checks_by_role(
        evidence=evidence_output,
        machine_checks=machine_checks,
    )
    frozen_origin_main = str(
        _binding(
            checks_by_role,
            "natural_cutover_freeze_manifest",
            "origin_main_commit",
        )
    )
    require_fast_forward_candidate(
        repo_root,
        frozen_origin_main_commit=frozen_origin_main,
        authorized_commit=str(normalized["authorized_commit"]),
    )
    require_remote_origin_main(
        repo_root,
        frozen_origin_main,
    )

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
        "aggregate_check": aggregate_check,
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
    repo_root: str | Path,
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
        "aggregate_check",
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
    expected_full_commit = _full_commit(expected_commit, label="expected commit")
    if commit != expected_full_commit:
        raise ValueError(f"authorization commit {commit} does not match expected commit {expected_full_commit}")
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
    aggregate_check = payload.get("aggregate_check")
    if (
        not isinstance(assessment, Mapping)
        or not isinstance(evidence, Mapping)
        or not isinstance(gates, Mapping)
        or not isinstance(machine_checks, Mapping)
        or not isinstance(aggregate_check, Mapping)
    ):
        raise ValueError("account-execution authorization lacks bound assessment or evidence")
    assessment_map = cast(Mapping[str, Any], assessment)
    evidence_map = cast(Mapping[str, Any], evidence)
    gates_map = cast(Mapping[str, Any], gates)
    machine_checks_map = cast(Mapping[str, Any], machine_checks)
    aggregate_check_map = cast(Mapping[str, Any], aggregate_check)
    if set(assessment_map) != {"path", "size_bytes", "sha256"}:
        raise ValueError("account-execution authorization has an invalid assessment identity")
    _lower_sha256(assessment_map.get("sha256"), label="authorization assessment hash")
    if not str(assessment_map.get("path") or "").startswith("/") or int(assessment_map.get("size_bytes") or -1) < 0:
        raise ValueError("account-execution authorization has an invalid assessment identity")
    if set(gates_map) != set(REQUIRED_GATE_ROLES):
        raise ValueError("account-execution authorization has the wrong gate set")
    if set(machine_checks_map) != set(evidence_map):
        raise ValueError("account-execution authorization machine checks do not cover its evidence")
    observed_roles: set[str] = set()
    for evidence_id, entry in evidence_map.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"authorization evidence {evidence_id!r} must be an object")
        if set(entry) != {"role", "claim", "path", "size_bytes", "sha256"}:
            raise ValueError(f"authorization evidence {evidence_id!r} has invalid fields")
        role = str(entry.get("role") or "")
        if role not in ALL_EVIDENCE_ROLES:
            raise ValueError(f"authorization evidence {evidence_id!r} has an unknown role")
        if role in observed_roles:
            raise ValueError(f"authorization evidence repeats role {role!r}")
        observed_roles.add(role)
        _lower_sha256(entry.get("sha256"), label=f"authorization evidence {evidence_id!r} hash")
        if not str(entry.get("path") or "").startswith("/") or int(entry.get("size_bytes") or -1) < 0:
            raise ValueError(f"authorization evidence {evidence_id!r} has invalid identity")
        check = machine_checks_map[evidence_id]
        if not isinstance(check, Mapping) or set(check) != {
            "validator",
            "status",
            "artifact_sha256",
            "bindings",
        }:
            raise ValueError(f"authorization evidence {evidence_id!r} lacks a machine check")
        expected_status = "passed" if role in MACHINE_VALIDATED_ROLES else "operator_reviewed_integrity_only"
        if check.get("status") != expected_status:
            raise ValueError(f"authorization evidence {evidence_id!r} has an invalid check status")
        _lower_sha256(
            check.get("artifact_sha256"),
            label=f"authorization evidence {evidence_id!r} checked artifact hash",
        )
        if not isinstance(check.get("bindings"), Mapping):
            raise ValueError(f"authorization evidence {evidence_id!r} has invalid bindings")
        expected_validator = (
            MACHINE_VALIDATOR_IDS[role] if role in MACHINE_VALIDATED_ROLES else "operator_reviewed_evidence_v1"
        )
        if check.get("validator") != expected_validator:
            raise ValueError(f"authorization evidence {evidence_id!r} has the wrong validator")
    if observed_roles != ALL_EVIDENCE_ROLES:
        raise ValueError("authorization evidence does not contain the exact registered role set")

    if set(aggregate_check_map) != {
        "schema_version",
        "validator",
        "status",
        "checks",
        "evidence_set_sha256",
    }:
        raise ValueError("authorization aggregate evidence check has invalid fields")
    if (
        aggregate_check_map.get("schema_version") != AGGREGATE_CHECK_SCHEMA_VERSION
        or aggregate_check_map.get("validator") != AGGREGATE_CHECK_VALIDATOR
        or aggregate_check_map.get("status") != "passed"
        or not isinstance(aggregate_check_map.get("checks"), Mapping)
        or not aggregate_check_map.get("checks")
    ):
        raise ValueError("authorization aggregate evidence check has not passed")
    _lower_sha256(
        aggregate_check_map.get("evidence_set_sha256"),
        label="authorization aggregate evidence-set hash",
    )
    for gate_name, required_roles in REQUIRED_GATE_ROLES.items():
        gate = gates_map[gate_name]
        if not isinstance(gate, Mapping) or gate.get("status") != "passed":
            raise ValueError(f"authorization gate {gate_name!r} has not passed")
        if set(gate) != {"status", "decision", "evidence"}:
            raise ValueError(f"authorization gate {gate_name!r} has invalid fields")
        evidence_ids = gate.get("evidence")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError(f"authorization gate {gate_name!r} lacks evidence")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError(f"authorization gate {gate_name!r} repeats evidence")
        if any(item not in evidence_map for item in evidence_ids):
            raise ValueError(f"authorization gate {gate_name!r} references unknown evidence")
        roles = {str(cast(Mapping[str, Any], evidence_map[item]).get("role") or "") for item in evidence_ids}
        if roles != required_roles:
            raise ValueError(f"authorization gate {gate_name!r} does not use its exact evidence roles")
        if not str(gate.get("decision") or "").strip():
            raise ValueError(f"authorization gate {gate_name!r} lacks a decision")
    observed_hash = str(payload.get("artifact_sha256") or "")
    if observed_hash != _self_hash(payload):
        raise ValueError("account-execution authorization hash mismatch")

    assessment_path = _strict_file(
        str(assessment_map["path"]),
        label="bound cutover assessment",
    )
    assessment_snapshot = _file_snapshot(assessment_path, label="bound cutover assessment")
    assessment_identity = _file_identity(
        assessment_path,
        snapshot=assessment_snapshot,
        label="bound cutover assessment",
    )
    if assessment_identity != dict(assessment_map):
        raise ValueError("bound cutover assessment changed after authorization issuance")
    reopened_assessment = _validate_assessment_structure(
        _load_json_object(
            assessment_path,
            label="bound cutover assessment",
            snapshot=assessment_snapshot,
        )
    )
    if (
        _file_identity(
            assessment_path,
            snapshot=_file_snapshot(assessment_path, label="bound cutover assessment"),
            label="bound cutover assessment",
        )
        != assessment_identity
    ):
        raise ValueError("bound cutover assessment changed during verification")
    if (
        reopened_assessment["authorized_commit"] != commit
        or reopened_assessment["authorized_by"] != payload["authorized_by"]
        or canonical_json(reopened_assessment["gates"]) != canonical_json(gates_map)
        or set(reopened_assessment["evidence"]) != set(evidence_map)
    ):
        raise ValueError("bound cutover assessment no longer matches the authorization")
    revalidated_checks: dict[str, Mapping[str, Any]] = {}
    for evidence_id, reopened_entry in reopened_assessment["evidence"].items():
        bound_entry = cast(Mapping[str, Any], evidence_map[evidence_id])
        evidence_path = _strict_file(
            reopened_entry["path"],
            label=f"bound cutover evidence {evidence_id!r}",
        )
        if (
            reopened_entry["role"] != bound_entry["role"]
            or reopened_entry["claim"] != bound_entry["claim"]
            or str(evidence_path) != bound_entry["path"]
        ):
            raise ValueError(f"bound cutover evidence {evidence_id!r} no longer matches its assessment")
        evidence_label = f"bound cutover evidence {evidence_id!r}"
        evidence_snapshot = _file_snapshot(evidence_path, label=evidence_label)
        identity = _file_identity(
            evidence_path,
            snapshot=evidence_snapshot,
            label=evidence_label,
        )
        if identity != {
            "path": bound_entry["path"],
            "size_bytes": bound_entry["size_bytes"],
            "sha256": bound_entry["sha256"],
        }:
            raise ValueError(f"bound cutover evidence {evidence_id!r} changed after authorization issuance")
        rebuilt_check = _machine_validate_snapshot(
            role=reopened_entry["role"],
            path=evidence_path,
            now_ns=issued_ts_ns,
            snapshot=evidence_snapshot,
        )
        if (
            _file_identity(
                evidence_path,
                snapshot=_file_snapshot(evidence_path, label=evidence_label),
                label=evidence_label,
            )
            != identity
        ):
            raise ValueError(f"bound cutover evidence {evidence_id!r} changed during revalidation")
        if canonical_json(rebuilt_check) != canonical_json(machine_checks_map[evidence_id]):
            raise ValueError(f"bound cutover evidence {evidence_id!r} no longer reproduces its machine check")
        revalidated_checks[evidence_id] = rebuilt_check
    rebuilt_aggregate = _validate_aggregate_cross_bindings(
        authorized_commit=commit,
        evidence=cast(Mapping[str, Mapping[str, Any]], evidence_map),
        machine_checks=revalidated_checks,
    )
    if canonical_json(rebuilt_aggregate) != canonical_json(aggregate_check_map):
        raise ValueError("bound cutover evidence no longer reproduces its aggregate check")
    require_clean_authorized_checkout(repo_root, commit)
    frozen_origin_main = str(
        _binding(
            _checks_by_role(
                evidence=cast(Mapping[str, Mapping[str, Any]], evidence_map),
                machine_checks=revalidated_checks,
            ),
            "natural_cutover_freeze_manifest",
            "origin_main_commit",
        )
    )
    require_fast_forward_candidate(
        repo_root,
        frozen_origin_main_commit=frozen_origin_main,
        authorized_commit=commit,
    )
    require_remote_origin_main(
        repo_root,
        frozen_origin_main,
        promoted_commit=commit,
    )
    return payload


def load_authorization_receipt(
    path: str | Path,
    *,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path,
    now_ns: int | None = None,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    receipt_path = _strict_file(path, label="account-execution deploy authorization")
    observed = snapshot or _file_snapshot(receipt_path, label="account-execution deploy authorization")
    if observed.path != receipt_path.expanduser().absolute():
        raise ValueError("account-execution deploy authorization snapshot path differs")
    if observed.mode & 0o077:
        raise ValueError("account-execution deploy authorization must have mode 0600")
    if observed.uid != os.geteuid():
        raise ValueError("account-execution deploy authorization is not owned by the verifier")
    return verify_authorization_receipt(
        _load_json_object(
            receipt_path,
            label="account-execution deploy authorization",
            snapshot=observed,
        ),
        expected_commit=expected_commit,
        repo_root=repo_root,
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


def require_remote_origin_main(
    repo_root: str | Path,
    frozen_origin_main_commit: str,
    *,
    promoted_commit: str | None = None,
) -> str:
    """Validate the live remote ref at issuance or the promotion transition.

    Before authorization issuance, ``promoted_commit`` is omitted and live
    ``origin/main`` must still equal the frozen base.  During checked deploy,
    the main-ref update may already have happened, so only that base or the
    exact authorized commit is accepted.  This deliberately rejects every
    intervening or later main commit.
    """

    root = Path(repo_root).expanduser().resolve(strict=True)
    expected = _full_commit(frozen_origin_main_commit, label="frozen origin/main commit")
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    github_token = environment.pop("GITHUB_TOKEN", "")
    remote_url = _git_output(root, "remote", "get-url", "origin")
    if github_token and remote_url.startswith("https://github.com/"):
        if any(character in github_token for character in ("\n", "\r", "\0")):
            raise ValueError("GITHUB_TOKEN contains an invalid control character")
        try:
            config_count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        except ValueError as exc:
            raise ValueError("GIT_CONFIG_COUNT is not an integer") from exc
        if config_count < 0:
            raise ValueError("GIT_CONFIG_COUNT cannot be negative")
        basic = base64.b64encode(f"x-access-token:{github_token}".encode("utf-8")).decode("ascii")
        environment["GIT_CONFIG_COUNT"] = str(config_count + 1)
        environment[f"GIT_CONFIG_KEY_{config_count}"] = "http.https://github.com/.extraheader"
        environment[f"GIT_CONFIG_VALUE_{config_count}"] = f"AUTHORIZATION: Basic {basic}"
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/heads/main",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("live origin/main lookup timed out") from exc
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "cannot resolve live origin/main for cutover authority")
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != "refs/heads/main":
        raise ValueError("live origin/main lookup returned an ambiguous ref set")
    observed = _full_commit(rows[0][0], label="live origin/main commit")
    allowed = {expected}
    if promoted_commit is not None:
        allowed.add(_full_commit(promoted_commit, label="promoted commit"))
    if observed not in allowed:
        raise ValueError(f"live origin/main {observed} is neither the frozen base nor the exact authorized promotion")
    return observed


def require_fast_forward_candidate(
    repo_root: str | Path,
    *,
    frozen_origin_main_commit: str,
    authorized_commit: str,
) -> None:
    """Prove the authorized commit can be promoted from the frozen base by FF."""

    root = Path(repo_root).expanduser().resolve(strict=True)
    base = _full_commit(
        frozen_origin_main_commit,
        label="frozen origin/main commit",
    )
    candidate = _full_commit(authorized_commit)
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, candidate],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        raise ValueError(f"authorized commit {candidate} is not a fast-forward descendant of frozen origin/main {base}")
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "cannot verify fast-forward cutover ancestry")


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
            require_clean_authorized_checkout(args.repo_root, str(normalized["authorized_commit"]))
            receipt = build_authorization_receipt(
                normalized,
                assessment_path=assessment_path,
                repo_root=args.repo_root,
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
                repo_root=args.repo_root,
                machine_id_path=args.machine_id_path,
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
