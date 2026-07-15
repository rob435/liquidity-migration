"""Prepare or verify the filesystem epoch bound by cutover authorization.

The deploy shell surfaces intentionally do not parse authorization JSON or
reconstruct root paths.  This module reopens the authorization and its machine
evidence, then delegates path validation to the stopped- and fresh-epoch
loaders before materializing or verifying the exact systemd EnvironmentFiles.

``prepare`` is the one-time pre-start transition: the registered natural units
must still be stopped and every fresh root must still be empty.  ``verify`` is
read-only and is suitable after activation, when valid owners may have
populated the fresh roots.  ``prepare-evidence-runtime`` creates the explicit
commit/machine-bound marker required before activation, while ``verify-runtime``
performs the bounded check used by the same-process service wrapper.  Neither
surface starts a unit or grants deployment or real-money authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .account_cutover_authority import (
    load_authorization_receipt,
    require_clean_authorized_checkout,
)
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json
from .fresh_deploy_environment import (
    DEFAULT_OUTPUT_DIRECTORY,
    materialize_fresh_deploy_environment,
    verify_fresh_deploy_environment,
)
from .fresh_deploy_epoch import load_fresh_deploy_epoch


STOPPED_ROLE = "stopped_natural_epoch"
FRESH_ROLE = "fresh_deploy_epoch"
RUNTIME_LATCH_FILENAME = "account-execution-fresh-epoch-active.json"
RUNTIME_LATCH_SCHEMA_VERSION = 3
RUNTIME_LATCH_KIND = "authorized_fresh_deploy_runtime_latch"
RUNTIME_LATCH_VALIDATOR = "authorized_fresh_deploy_runtime_latch_v3"
ACTIVATION_HISTORY_FILENAME = "account-execution-fresh-epoch-activation-started.json"
ACTIVATION_HISTORY_SCHEMA_VERSION = 1
ACTIVATION_HISTORY_KIND = "authorized_fresh_deploy_activation_started"
ACTIVATION_HISTORY_VALIDATOR = "authorized_fresh_deploy_activation_started_v1"
DEFAULT_CUTOVER_AUTHORIZATION = Path(
    "/etc/liquidity-migration/account-execution-deploy-ready"
)
DEFAULT_PRE_CUTOVER_RUNTIME_MARKER = Path(
    "/etc/liquidity-migration/account-execution-pre-cutover-ready"
)
PRE_CUTOVER_MARKER_SCHEMA_VERSION = 1
PRE_CUTOVER_MARKER_KIND = "account_execution_pre_cutover_runtime"
PRE_CUTOVER_MARKER_VALIDATOR = "account_execution_pre_cutover_runtime_v1"
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNIT = re.compile(r"liquidity-migration-[a-z0-9-]+\.service")
_PRIVATE_IDENTITY_FIELDS = {
    "path",
    "size_bytes",
    "sha256",
    "device",
    "inode",
    "mtime_ns",
    "mode",
    "uid",
    "nlink",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _self_hash(payload: Mapping[str, Any]) -> str:
    return _sha256(canonical_json({**dict(payload), "artifact_sha256": ""}))


def _runtime_latch_path(output_directory: str | Path) -> Path:
    output = Path(output_directory).expanduser()
    if not output.is_absolute():
        raise ValueError("fresh-deploy environment directory must be absolute")
    return output.parent / RUNTIME_LATCH_FILENAME


def _activation_history_path(output_directory: str | Path) -> Path:
    output = Path(output_directory).expanduser()
    if not output.is_absolute():
        raise ValueError("fresh-deploy environment directory must be absolute")
    return output.parent / ACTIVATION_HISTORY_FILENAME


def classify_authorized_deploy_phase(
    *,
    authorization_path: str | Path,
    output_directory: str | Path,
    pre_cutover_marker_path: str | Path = DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
) -> dict[str, Any]:
    """Classify the one-way activation state without interpreting authority."""

    authorization = Path(authorization_path).expanduser()
    output = Path(output_directory).expanduser()
    marker = Path(pre_cutover_marker_path).expanduser()
    if not authorization.is_absolute() or not marker.is_absolute():
        raise ValueError("authorization and pre-cutover marker paths must be absolute")
    latch = _runtime_latch_path(output)
    activation_history = _activation_history_path(output)
    present = {
        "authorization": os.path.lexists(authorization),
        "activation_history": os.path.lexists(activation_history),
        "environment": os.path.lexists(output),
        "pre_cutover_marker": os.path.lexists(marker),
        "runtime_latch": os.path.lexists(latch),
    }
    if (
        present["pre_cutover_marker"]
        and not present["activation_history"]
        and not present["environment"]
        and not present["runtime_latch"]
    ):
        phase = "preactivation"
    elif (
        not present["pre_cutover_marker"]
        and present["authorization"]
        and present["activation_history"]
        and present["environment"]
        and present["runtime_latch"]
    ):
        phase = "activated"
    else:
        phase = "partial"
    return {
        "phase": phase,
        "present": sorted(name for name, exists in present.items() if exists),
        "missing": sorted(name for name, exists in present.items() if not exists),
    }


def _stable_private_snapshot(
    path: str | Path,
    *,
    label: str,
) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return read_stable_file(
        candidate,
        label=label,
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )


def _stable_private_file(
    path: str | Path,
    *,
    label: str,
) -> tuple[Path, bytes, os.stat_result]:
    snapshot = _stable_private_snapshot(path, label=label)
    return snapshot.path, snapshot.data, snapshot.metadata


def _strict_json_bytes(data: bytes, *, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise ValueError(f"{label} repeats JSON key {key!r}")
            output[key] = value
        return output

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one object")
    return value


def _private_identity_from_snapshot(snapshot: StableFileSnapshot) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "sha256": snapshot.sha256,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "mtime_ns": snapshot.mtime_ns,
        "mode": snapshot.mode,
        "uid": snapshot.uid,
        "nlink": snapshot.nlink,
    }


def _private_identity(
    path: str | Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    observed = snapshot or _stable_private_snapshot(path, label=label)
    if observed.path != Path(path).expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    return _private_identity_from_snapshot(observed)


def _write_private_json_exclusive(
    path: Path,
    payload: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Durably publish one owner-only JSON marker without replacement."""

    parent = path.parent.resolve(strict=True)
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = canonical_json(payload) + b"\n"
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError(f"{label} write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _machine_fingerprint(machine_id_path: str | Path) -> str:
    path = Path(machine_id_path).expanduser()
    if not path.is_absolute():
        raise ValueError("machine-id path must be absolute")
    try:
        snapshot = read_stable_file(
            path,
            label="machine-id",
            reject_empty=True,
            require_single_link=True,
        )
        value = snapshot.data.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("cannot read machine-id for fresh runtime latch") from exc
    if not value:
        raise ValueError("machine-id is empty")
    return _sha256(value.encode("utf-8"))


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _pre_cutover_marker_payload(
    *,
    expected_commit: str,
    repo_root: Path,
    machine_id_path: str | Path,
    created_ts_ns: int,
) -> dict[str, Any]:
    commit = expected_commit.lower()
    if not _FULL_COMMIT.fullmatch(commit):
        raise ValueError("pre-cutover runtime marker requires a full commit")
    payload: dict[str, Any] = {
        "schema_version": PRE_CUTOVER_MARKER_SCHEMA_VERSION,
        "kind": PRE_CUTOVER_MARKER_KIND,
        "validator": PRE_CUTOVER_MARKER_VALIDATOR,
        "created_ts_ns": int(created_ts_ns),
        "candidate_commit": commit,
        "repo_root": str(repo_root.resolve(strict=True)),
        "machine_fingerprint_sha256": _machine_fingerprint(machine_id_path),
        "execution_authorization": "evidence_collection_only_not_deploy",
        "artifact_sha256": "",
    }
    if payload["created_ts_ns"] <= 0:
        raise ValueError("pre-cutover runtime marker timestamp must be positive")
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _load_pre_cutover_runtime_marker(path: str | Path) -> dict[str, Any]:
    _resolved, data, _metadata = _stable_private_file(
        path, label="pre-cutover evidence runtime marker"
    )
    payload = _strict_json_bytes(data, label="pre-cutover evidence runtime marker")
    expected = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "candidate_commit",
        "repo_root",
        "machine_fingerprint_sha256",
        "execution_authorization",
        "artifact_sha256",
    }
    repo_root = Path(str(payload.get("repo_root") or ""))
    if (
        set(payload) != expected
        or payload.get("schema_version") != PRE_CUTOVER_MARKER_SCHEMA_VERSION
        or payload.get("kind") != PRE_CUTOVER_MARKER_KIND
        or payload.get("validator") != PRE_CUTOVER_MARKER_VALIDATOR
        or int(payload.get("created_ts_ns") or 0) <= 0
        or not _FULL_COMMIT.fullmatch(str(payload.get("candidate_commit") or ""))
        or not repo_root.is_absolute()
        or not _LOWER_SHA256.fullmatch(
            str(payload.get("machine_fingerprint_sha256") or "")
        )
        or payload.get("execution_authorization")
        != "evidence_collection_only_not_deploy"
        or payload.get("artifact_sha256") != _self_hash(payload)
    ):
        raise ValueError("pre-cutover evidence runtime marker is invalid")
    return payload


def prepare_pre_cutover_runtime_marker(
    *,
    marker_path: str | Path = DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path = "/etc/machine-id",
    created_ts_ns: int | None = None,
    authorization_path: str | Path = DEFAULT_CUTOVER_AUTHORIZATION,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
) -> dict[str, Any]:
    """Bind legacy evidence-window roots to one clean commit and machine."""

    commit = expected_commit.lower()
    repository = Path(repo_root).expanduser()
    marker = Path(marker_path).expanduser()
    authorization = Path(authorization_path).expanduser()
    output = Path(output_directory).expanduser()
    if (
        not repository.is_absolute()
        or not marker.is_absolute()
        or not authorization.is_absolute()
        or not output.is_absolute()
    ):
        raise ValueError(
            "pre-cutover repository, marker, authorization, and output paths must be absolute"
        )
    activation_state = {
        "activation_history": _activation_history_path(output),
        "authorization": authorization,
        "environment": output,
        "runtime_latch": _runtime_latch_path(output),
    }
    present = sorted(
        name for name, path in activation_state.items() if os.path.lexists(path)
    )
    if present:
        raise ValueError(
            "cannot prepare pre-cutover runtime evidence after activation or partial "
            f"activation state exists: {', '.join(present)}"
        )
    require_clean_authorized_checkout(repository, commit)
    expected = _pre_cutover_marker_payload(
        expected_commit=commit,
        repo_root=repository,
        machine_id_path=machine_id_path,
        created_ts_ns=time.time_ns() if created_ts_ns is None else created_ts_ns,
    )
    if os.path.lexists(marker):
        observed = _load_pre_cutover_runtime_marker(marker)
        comparable = set(expected) - {"created_ts_ns", "artifact_sha256"}
        if any(observed.get(field) != expected.get(field) for field in comparable):
            raise ValueError("pre-cutover runtime marker belongs to another candidate")
        return {
            "status": "pre_cutover_runtime_marker_verified",
            "path": str(marker.resolve(strict=True)),
            **observed,
        }
    parent = marker.parent.resolve(strict=True)
    descriptor = os.open(
        marker,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = canonical_json(expected) + b"\n"
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("pre-cutover runtime marker write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        marker.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    observed = _load_pre_cutover_runtime_marker(marker)
    return {
        "status": "pre_cutover_runtime_marker_prepared",
        "path": str(marker.resolve(strict=True)),
        **observed,
    }


def _activation_history_payload(
    *,
    expected_commit: str,
    repo_root: Path,
    machine_id_path: str | Path,
    pre_cutover_marker_path: Path,
    pre_cutover_marker: Mapping[str, Any],
    created_ts_ns: int,
) -> dict[str, Any]:
    commit = expected_commit.lower()
    if not _FULL_COMMIT.fullmatch(commit):
        raise ValueError("activation history requires a full commit")
    marker_identity = _private_identity(
        pre_cutover_marker_path,
        label="pre-cutover evidence runtime marker",
    )
    marker_identity["artifact_sha256"] = str(
        pre_cutover_marker.get("artifact_sha256") or ""
    )
    if not _LOWER_SHA256.fullmatch(str(marker_identity["artifact_sha256"])):
        raise ValueError("pre-cutover evidence runtime marker has an invalid artifact hash")
    payload: dict[str, Any] = {
        "schema_version": ACTIVATION_HISTORY_SCHEMA_VERSION,
        "kind": ACTIVATION_HISTORY_KIND,
        "validator": ACTIVATION_HISTORY_VALIDATOR,
        "created_ts_ns": int(created_ts_ns),
        "candidate_commit": commit,
        "repo_root": str(repo_root.resolve(strict=True)),
        "machine_fingerprint_sha256": _machine_fingerprint(machine_id_path),
        "pre_cutover_marker": marker_identity,
        "execution_authorization": "activation_started_irreversible_no_rollback",
        "artifact_sha256": "",
    }
    if payload["created_ts_ns"] <= 0:
        raise ValueError("activation history timestamp must be positive")
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _load_activation_history(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    observed = snapshot or _stable_private_snapshot(
        path,
        label="fresh deployment activation history",
    )
    if observed.path != Path(path).expanduser().absolute():
        raise ValueError("fresh deployment activation history snapshot path differs")
    payload = _strict_json_bytes(
        observed.data,
        label="fresh deployment activation history",
    )
    expected = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "candidate_commit",
        "repo_root",
        "machine_fingerprint_sha256",
        "pre_cutover_marker",
        "execution_authorization",
        "artifact_sha256",
    }
    marker_identity = _mapping(
        payload.get("pre_cutover_marker"),
        label="activation history pre-cutover marker",
    )
    if (
        set(payload) != expected
        or payload.get("schema_version") != ACTIVATION_HISTORY_SCHEMA_VERSION
        or payload.get("kind") != ACTIVATION_HISTORY_KIND
        or payload.get("validator") != ACTIVATION_HISTORY_VALIDATOR
        or int(payload.get("created_ts_ns") or 0) <= 0
        or not _FULL_COMMIT.fullmatch(str(payload.get("candidate_commit") or ""))
        or not Path(str(payload.get("repo_root") or "")).is_absolute()
        or not _LOWER_SHA256.fullmatch(
            str(payload.get("machine_fingerprint_sha256") or "")
        )
        or set(marker_identity) != _PRIVATE_IDENTITY_FIELDS | {"artifact_sha256"}
        or not Path(str(marker_identity.get("path") or "")).is_absolute()
        or not _LOWER_SHA256.fullmatch(str(marker_identity.get("sha256") or ""))
        or not _LOWER_SHA256.fullmatch(
            str(marker_identity.get("artifact_sha256") or "")
        )
        or payload.get("execution_authorization")
        != "activation_started_irreversible_no_rollback"
        or payload.get("artifact_sha256") != _self_hash(payload)
    ):
        raise ValueError("fresh deployment activation history is invalid")
    return payload


def _verify_activation_history(
    *,
    history_path: Path,
    expected_commit: str,
    repo_root: Path,
    machine_id_path: str | Path,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    payload = _load_activation_history(history_path, snapshot=snapshot)
    if payload.get("candidate_commit") != expected_commit.lower():
        raise ValueError("fresh deployment activation history belongs to another commit")
    if Path(str(payload.get("repo_root"))).resolve(strict=True) != repo_root.resolve(
        strict=True
    ):
        raise ValueError("fresh deployment activation history belongs to another checkout")
    if payload.get("machine_fingerprint_sha256") != _machine_fingerprint(
        machine_id_path
    ):
        raise ValueError("fresh deployment activation history belongs to another machine")
    return payload


def _prepare_activation_history(
    *,
    history_path: Path,
    expected_commit: str,
    repo_root: Path,
    machine_id_path: str | Path,
    pre_cutover_marker_path: Path,
    pre_cutover_marker: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if os.path.lexists(history_path):
        raise ValueError("fresh deployment activation has already started")
    expected = _activation_history_payload(
        expected_commit=expected_commit,
        repo_root=repo_root,
        machine_id_path=machine_id_path,
        pre_cutover_marker_path=pre_cutover_marker_path,
        pre_cutover_marker=pre_cutover_marker,
        created_ts_ns=time.time_ns(),
    )
    _write_private_json_exclusive(
        history_path,
        expected,
        label="fresh deployment activation history",
    )
    observed = _load_activation_history(history_path)
    if observed != expected:
        raise RuntimeError("fresh deployment activation history changed during publication")
    return history_path.resolve(strict=True), observed


def _verify_pre_cutover_runtime_marker(
    *,
    marker_path: Path,
    expected_commit: str,
    repo_root: Path,
    machine_id_path: str | Path,
) -> dict[str, Any]:
    payload = _load_pre_cutover_runtime_marker(marker_path)
    if payload.get("candidate_commit") != expected_commit.lower():
        raise ValueError("pre-cutover runtime marker belongs to another commit")
    if Path(str(payload.get("repo_root"))).resolve(strict=True) != repo_root.resolve(
        strict=True
    ):
        raise ValueError("pre-cutover runtime marker belongs to another checkout")
    if payload.get("machine_fingerprint_sha256") != _machine_fingerprint(
        machine_id_path
    ):
        raise ValueError("pre-cutover runtime marker belongs to another machine")
    require_clean_authorized_checkout(repo_root, expected_commit.lower())
    return payload


def _remove_pre_cutover_runtime_marker(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _load_pre_cutover_runtime_marker(path)
    parent = path.parent.resolve(strict=True)
    path.unlink()
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _runtime_latch_payload(
    *,
    authorization_path: Path,
    authorization: Mapping[str, Any],
    stopped_path: Path,
    stopped: Mapping[str, Any],
    fresh_path: Path,
    fresh: Mapping[str, Any],
    environment_receipt_path: Path,
    environment: Mapping[str, Any],
    activation_history_path: Path,
    activation_history: Mapping[str, Any],
    output_directory: Path,
    machine_id_path: str | Path,
    created_ts_ns: int,
) -> dict[str, Any]:
    commit = str(authorization.get("authorized_commit") or "").lower()
    if not _FULL_COMMIT.fullmatch(commit):
        raise ValueError("authorization has an invalid commit for the runtime latch")
    late = _mapping(fresh.get("late_environment"), label="fresh late environment")
    units = sorted(str(unit) for unit in late)
    if not units or any(not _UNIT.fullmatch(unit) for unit in units):
        raise ValueError("fresh runtime latch has an invalid unit set")
    authorization_identity = _private_identity(authorization_path, label="cutover authorization receipt")
    authorization_identity["artifact_sha256"] = str(authorization.get("artifact_sha256") or "")
    stopped_identity = _private_identity(stopped_path, label="stopped natural epoch")
    stopped_identity["artifact_sha256"] = str(stopped.get("artifact_sha256") or "")
    fresh_identity = _private_identity(fresh_path, label="fresh deploy epoch")
    fresh_identity["artifact_sha256"] = str(fresh.get("artifact_sha256") or "")
    environment_identity = _private_identity(
        environment_receipt_path, label="fresh environment materialization receipt"
    )
    environment_identity["artifact_sha256"] = str(environment.get("artifact_sha256") or "")
    activation_history_identity = _private_identity(
        activation_history_path,
        label="fresh deployment activation history",
    )
    activation_history_identity["artifact_sha256"] = str(
        activation_history.get("artifact_sha256") or ""
    )
    unit_environments: dict[str, dict[str, str]] = {}
    environment_fragments: dict[str, dict[str, Any]] = {}
    for unit in units:
        raw_environment = _mapping(
            late.get(unit), label=f"fresh late environment for {unit}"
        )
        unit_environment: dict[str, str] = {}
        for key, value in raw_environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"fresh late environment for {unit} is malformed")
            unit_environment[key] = value
        if not unit_environment:
            raise ValueError(f"fresh late environment for {unit} is empty")
        unit_environments[unit] = unit_environment
        environment_fragments[unit] = _private_identity(
            output_directory / f"{unit}.env",
            label=f"fresh environment fragment for {unit}",
        )
    for label, identity in (
        ("authorization", authorization_identity),
        ("stopped epoch", stopped_identity),
        ("fresh epoch", fresh_identity),
        ("environment materialization", environment_identity),
        ("activation history", activation_history_identity),
    ):
        if not _LOWER_SHA256.fullmatch(str(identity.get("artifact_sha256") or "")):
            raise ValueError(f"{label} has an invalid artifact hash")
    payload: dict[str, Any] = {
        "schema_version": RUNTIME_LATCH_SCHEMA_VERSION,
        "kind": RUNTIME_LATCH_KIND,
        "validator": RUNTIME_LATCH_VALIDATOR,
        "created_ts_ns": int(created_ts_ns),
        "authorized_commit": commit,
        "machine_fingerprint_sha256": _machine_fingerprint(machine_id_path),
        "authorization": authorization_identity,
        "stopped_natural_epoch": stopped_identity,
        "fresh_deploy_epoch": fresh_identity,
        "environment_materialization": environment_identity,
        "activation_history": activation_history_identity,
        "unit_environments": unit_environments,
        "environment_fragments": environment_fragments,
        "output_directory": str(output_directory.resolve(strict=True)),
        "units": units,
        "execution_authorization": "activated_by_bound_cutover_authorization",
        "artifact_sha256": "",
    }
    if payload["created_ts_ns"] <= 0:
        raise ValueError("fresh runtime latch timestamp must be positive")
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _load_runtime_latch(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    observed = snapshot or _stable_private_snapshot(
        path,
        label="authorized fresh runtime latch",
    )
    if observed.path != Path(path).expanduser().absolute():
        raise ValueError("authorized fresh runtime latch snapshot path differs")
    payload = _strict_json_bytes(
        observed.data,
        label="authorized fresh runtime latch",
    )
    expected = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "authorized_commit",
        "machine_fingerprint_sha256",
        "authorization",
        "stopped_natural_epoch",
        "fresh_deploy_epoch",
        "environment_materialization",
        "activation_history",
        "unit_environments",
        "environment_fragments",
        "output_directory",
        "units",
        "execution_authorization",
        "artifact_sha256",
    }
    if set(payload) != expected:
        raise ValueError("authorized fresh runtime latch has unexpected or missing fields")
    if (
        payload.get("schema_version") != RUNTIME_LATCH_SCHEMA_VERSION
        or payload.get("kind") != RUNTIME_LATCH_KIND
        or payload.get("validator") != RUNTIME_LATCH_VALIDATOR
        or int(payload.get("created_ts_ns") or 0) <= 0
        or not _FULL_COMMIT.fullmatch(str(payload.get("authorized_commit") or ""))
        or not _LOWER_SHA256.fullmatch(str(payload.get("machine_fingerprint_sha256") or ""))
        or payload.get("execution_authorization") != "activated_by_bound_cutover_authorization"
        or payload.get("artifact_sha256") != _self_hash(payload)
    ):
        raise ValueError("authorized fresh runtime latch is invalid")
    units = payload.get("units")
    if (
        not isinstance(units, list)
        or not units
        or units != sorted(set(str(unit) for unit in units))
        or any(not _UNIT.fullmatch(str(unit)) for unit in units)
    ):
        raise ValueError("authorized fresh runtime latch unit set is invalid")
    unit_environments = _mapping(
        payload.get("unit_environments"), label="runtime latch unit environments"
    )
    fragments = _mapping(
        payload.get("environment_fragments"),
        label="runtime latch environment fragments",
    )
    if sorted(str(item) for item in unit_environments) != units:
        raise ValueError("runtime latch unit environment set is invalid")
    if sorted(str(item) for item in fragments) != units:
        raise ValueError("runtime latch fragment set is invalid")
    for unit in units:
        values = _mapping(
            unit_environments.get(unit),
            label=f"runtime latch environment for {unit}",
        )
        if not values or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError(f"runtime latch environment for {unit} is invalid")
        fragment = _mapping(
            fragments.get(unit), label=f"runtime latch fragment for {unit}"
        )
        if set(fragment) != _PRIVATE_IDENTITY_FIELDS:
            raise ValueError(f"runtime latch fragment identity for {unit} is invalid")
        if not Path(str(fragment.get("path") or "")).is_absolute():
            raise ValueError(f"runtime latch fragment path for {unit} is not absolute")
        if not _LOWER_SHA256.fullmatch(str(fragment.get("sha256") or "")):
            raise ValueError(f"runtime latch fragment hash for {unit} is invalid")
    for field in (
        "authorization",
        "stopped_natural_epoch",
        "fresh_deploy_epoch",
        "environment_materialization",
        "activation_history",
    ):
        identity = _mapping(payload.get(field), label=f"runtime latch {field}")
        if set(identity) != _PRIVATE_IDENTITY_FIELDS | {"artifact_sha256"}:
            raise ValueError(f"runtime latch {field} identity fields are invalid")
        if not Path(str(identity.get("path") or "")).is_absolute():
            raise ValueError(f"runtime latch {field} path is not absolute")
        if not _LOWER_SHA256.fullmatch(str(identity.get("sha256") or "")):
            raise ValueError(f"runtime latch {field} hash is invalid")
        if not _LOWER_SHA256.fullmatch(str(identity.get("artifact_sha256") or "")):
            raise ValueError(f"runtime latch {field} artifact hash is invalid")
    output = Path(str(payload.get("output_directory") or ""))
    if not output.is_absolute():
        raise ValueError("authorized fresh runtime latch output directory is not absolute")
    return payload


def _write_or_verify_runtime_latch(
    *,
    latch_path: Path,
    authorization_path: Path,
    authorization: Mapping[str, Any],
    stopped_path: Path,
    stopped: Mapping[str, Any],
    fresh_path: Path,
    fresh: Mapping[str, Any],
    environment_receipt_path: Path,
    environment: Mapping[str, Any],
    activation_history_path: Path,
    activation_history: Mapping[str, Any],
    output_directory: Path,
    machine_id_path: str | Path,
) -> Path:
    expected = _runtime_latch_payload(
        authorization_path=authorization_path,
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh_path=fresh_path,
        fresh=fresh,
        environment_receipt_path=environment_receipt_path,
        environment=environment,
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output_directory,
        machine_id_path=machine_id_path,
        created_ts_ns=time.time_ns(),
    )
    if os.path.lexists(latch_path):
        observed = _load_runtime_latch(latch_path)
        comparable = set(expected) - {"created_ts_ns", "artifact_sha256"}
        if any(observed.get(field) != expected.get(field) for field in comparable):
            raise ValueError("authorized fresh runtime latch belongs to another deployment")
        return latch_path.resolve(strict=True)
    parent = latch_path.parent.resolve(strict=True)
    if parent != output_directory.parent.resolve(strict=True):
        raise ValueError("authorized fresh runtime latch is outside the environment parent")
    descriptor = os.open(
        latch_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = canonical_json(expected) + b"\n"
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("authorized fresh runtime latch write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        latch_path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _load_runtime_latch(latch_path)
    return latch_path.resolve(strict=True)


def _verify_runtime_latch_dependencies(
    *,
    latch_path: Path,
    authorization_path: Path,
    authorization: Mapping[str, Any],
    stopped_path: Path,
    stopped: Mapping[str, Any],
    fresh_path: Path,
    fresh: Mapping[str, Any],
    environment_receipt_path: Path,
    environment: Mapping[str, Any],
    activation_history_path: Path,
    activation_history: Mapping[str, Any],
    output_directory: Path,
    machine_id_path: str | Path,
) -> dict[str, Any]:
    observed = _load_runtime_latch(latch_path)
    expected = _runtime_latch_payload(
        authorization_path=authorization_path,
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh_path=fresh_path,
        fresh=fresh,
        environment_receipt_path=environment_receipt_path,
        environment=environment,
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output_directory,
        machine_id_path=machine_id_path,
        created_ts_ns=int(observed["created_ts_ns"]),
    )
    if observed != expected:
        raise ValueError("authorized fresh runtime latch no longer matches its dependencies")
    return observed


def _verify_latch_private_identity(
    identity: Mapping[str, Any],
    *,
    label: str,
) -> StableFileSnapshot:
    path = Path(str(identity.get("path") or ""))
    expected = {key: value for key, value in identity.items() if key != "artifact_sha256"}
    snapshot = _stable_private_snapshot(path, label=label)
    if _private_identity(path, label=label, snapshot=snapshot) != expected:
        raise ValueError(f"{label} changed after fresh runtime activation")
    return snapshot


def _verify_activated_epoch(
    *,
    authorization_path: str | Path,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path,
    output_directory: str | Path,
    pre_cutover_marker_path: str | Path,
    systemctl_bin: str,
) -> tuple[
    dict[str, Any],
    Path,
    dict[str, Any],
    Path,
    dict[str, Any],
    dict[str, Any],
    Path,
    dict[str, Any],
]:
    """Reopen an activated latch without renewing its spent authorization."""

    commit = expected_commit.lower()
    if not _FULL_COMMIT.fullmatch(commit):
        raise ValueError("activated fresh epoch requires a full expected commit")
    authorization_candidate = Path(authorization_path).expanduser()
    output_candidate = Path(output_directory).expanduser()
    repository = Path(repo_root).expanduser()
    pre_cutover_marker = Path(pre_cutover_marker_path).expanduser()
    if not authorization_candidate.is_absolute():
        raise ValueError("cutover authorization path must be absolute")
    if not repository.is_absolute() or not pre_cutover_marker.is_absolute():
        raise ValueError("repository root and pre-cutover marker must be absolute")
    latch_candidate = _runtime_latch_path(output_candidate)
    activation_history_candidate = _activation_history_path(output_candidate)
    state_exists = {
        "activation_history": os.path.lexists(activation_history_candidate),
        "authorization": os.path.lexists(authorization_candidate),
        "environment": os.path.lexists(output_candidate),
        "runtime_latch": os.path.lexists(latch_candidate),
    }
    if not all(state_exists.values()):
        missing = ", ".join(
            sorted(name for name, exists in state_exists.items() if not exists)
        )
        raise ValueError(f"activated fresh epoch is incomplete: missing {missing}")
    if os.path.lexists(pre_cutover_marker):
        raise ValueError("authorized fresh deployment retains a pre-cutover marker")

    latch_snapshot = _stable_private_snapshot(
        latch_candidate,
        label="authorized fresh runtime latch",
    )
    latch = _load_runtime_latch(latch_candidate, snapshot=latch_snapshot)
    if latch.get("authorized_commit") != commit:
        raise ValueError("authorized fresh runtime latch belongs to another commit")
    require_clean_authorized_checkout(repository, commit)
    if latch.get("machine_fingerprint_sha256") != _machine_fingerprint(
        machine_id_path
    ):
        raise ValueError("authorized fresh runtime latch belongs to another machine")
    output = output_candidate.resolve(strict=True)
    if Path(str(latch["output_directory"])).resolve(strict=True) != output:
        raise ValueError("authorized fresh runtime latch names another environment")

    activation_history_identity = _mapping(
        latch.get("activation_history"),
        label="runtime latch activation history",
    )
    activation_history_snapshot = _verify_latch_private_identity(
        activation_history_identity,
        label="fresh deployment activation history",
    )
    latched_activation_history_path = activation_history_snapshot.path
    if latched_activation_history_path != activation_history_candidate.resolve(strict=True):
        raise ValueError("authorized fresh runtime latch names another activation history")
    activation_history = _verify_activation_history(
        history_path=latched_activation_history_path,
        expected_commit=commit,
        repo_root=repository,
        machine_id_path=machine_id_path,
        snapshot=activation_history_snapshot,
    )
    if activation_history.get("artifact_sha256") != activation_history_identity.get(
        "artifact_sha256"
    ):
        raise ValueError("fresh deployment activation history artifact changed")

    authorization_identity = _mapping(
        latch.get("authorization"), label="runtime latch authorization"
    )
    authorization_snapshot = _verify_latch_private_identity(
        authorization_identity,
        label="cutover authorization receipt",
    )
    latched_authorization_path = authorization_snapshot.path
    if latched_authorization_path != authorization_candidate.resolve(strict=True):
        raise ValueError("authorized fresh runtime latch names another authorization")

    stopped_identity = _mapping(
        latch.get("stopped_natural_epoch"),
        label="runtime latch stopped natural epoch",
    )
    stopped_snapshot = _verify_latch_private_identity(
        stopped_identity,
        label="stopped natural epoch",
    )
    stopped_path = stopped_snapshot.path
    fresh_identity = _mapping(
        latch.get("fresh_deploy_epoch"), label="runtime latch fresh deploy epoch"
    )
    fresh_snapshot = _verify_latch_private_identity(
        fresh_identity,
        label="fresh deploy epoch",
    )
    fresh_path = fresh_snapshot.path

    stopped = _load_stopped_epoch(
        stopped_path,
        require_currently_stopped=False,
        systemctl_bin=systemctl_bin,
        snapshot=stopped_snapshot,
    )
    fresh = load_fresh_deploy_epoch(
        fresh_path,
        require_empty_roots=False,
        snapshot=fresh_snapshot,
    )
    _basic_epoch_crosscheck(
        expected_commit=commit,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh=fresh,
    )
    environment = verify_fresh_deploy_environment(
        manifest_path=fresh_path,
        output_directory=output,
        require_empty_roots=False,
        manifest_snapshot=fresh_snapshot,
    )
    receipt_path = output / "environment-materialization.json"
    authorization = {
        "authorized_commit": commit,
        "artifact_sha256": authorization_identity.get("artifact_sha256"),
    }
    verified_latch = _verify_runtime_latch_dependencies(
        latch_path=latch_candidate,
        authorization_path=latched_authorization_path,
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh_path=fresh_path,
        fresh=fresh,
        environment_receipt_path=receipt_path,
        environment=environment,
        activation_history_path=latched_activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=machine_id_path,
    )
    for snapshot, label in (
        (latch_snapshot, "authorized fresh runtime latch"),
        (activation_history_snapshot, "fresh deployment activation history"),
        (authorization_snapshot, "cutover authorization receipt"),
        (stopped_snapshot, "stopped natural epoch"),
        (fresh_snapshot, "fresh deploy epoch"),
    ):
        if _stable_private_snapshot(snapshot.path, label=label) != snapshot:
            raise RuntimeError(f"{label} changed during activated epoch verification")
    return (
        authorization,
        stopped_path,
        stopped,
        fresh_path,
        fresh,
        environment,
        latch_candidate.resolve(strict=True),
        verified_latch,
    )


def verify_runtime_fresh_epoch(
    *,
    authorization_path: str | Path,
    repo_root: str | Path,
    output_directory: str | Path,
    unit: str,
    machine_id_path: str | Path = "/etc/machine-id",
    pre_cutover_marker_path: str | Path = DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    observed_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify one bounded runtime latch in the workload's inherited environment."""

    if not _UNIT.fullmatch(unit):
        raise ValueError(f"invalid fresh-runtime unit name: {unit!r}")
    authorization_candidate = Path(authorization_path).expanduser()
    output_candidate = Path(output_directory).expanduser()
    repo_candidate = Path(repo_root).expanduser()
    pre_cutover_marker = Path(pre_cutover_marker_path).expanduser()
    if not authorization_candidate.is_absolute():
        raise ValueError("cutover authorization path must be absolute")
    if not repo_candidate.is_absolute() or not pre_cutover_marker.is_absolute():
        raise ValueError("repository root and pre-cutover marker must be absolute")
    latch_candidate = _runtime_latch_path(output_candidate)
    activation_history_candidate = _activation_history_path(output_candidate)
    state_exists = {
        "activation_history": os.path.lexists(activation_history_candidate),
        "authorization": os.path.lexists(authorization_candidate),
        "environment": os.path.lexists(output_candidate),
        "runtime_latch": os.path.lexists(latch_candidate),
    }
    if not any(state_exists.values()):
        if not os.path.lexists(pre_cutover_marker):
            raise ValueError(
                "no activated fresh epoch and no commit-bound pre-cutover runtime marker"
            )
        pre_cutover = _load_pre_cutover_runtime_marker(pre_cutover_marker)
        commit = str(pre_cutover["candidate_commit"])
        _verify_pre_cutover_runtime_marker(
            marker_path=pre_cutover_marker,
            expected_commit=commit,
            repo_root=repo_candidate,
            machine_id_path=machine_id_path,
        )
        return {
            "status": "pre_cutover_evidence_runtime_verified",
            "unit": unit,
            "candidate_commit": commit,
            "execution_authorization": "evidence_collection_only_not_deploy",
        }
    if not all(state_exists.values()):
        missing = ", ".join(sorted(name for name, exists in state_exists.items() if not exists))
        raise ValueError(f"post-cutover runtime state is incomplete: missing {missing}")
    if os.path.lexists(pre_cutover_marker):
        raise ValueError("activated fresh runtime still has a pre-cutover marker")

    latch_snapshot = _stable_private_snapshot(
        latch_candidate,
        label="authorized fresh runtime latch",
    )
    latch = _load_runtime_latch(latch_candidate, snapshot=latch_snapshot)
    commit = str(latch["authorized_commit"])
    require_clean_authorized_checkout(repo_candidate, commit)
    if Path(str(latch["output_directory"])).resolve(strict=True) != output_candidate.resolve(strict=True):
        raise ValueError("authorized fresh runtime latch names another environment")
    if latch.get("machine_fingerprint_sha256") != _machine_fingerprint(machine_id_path):
        raise ValueError("authorized fresh runtime latch belongs to another machine")
    activation_history_identity = _mapping(
        latch.get("activation_history"),
        label="runtime latch activation history",
    )
    activation_history_snapshot = _verify_latch_private_identity(
        activation_history_identity,
        label="fresh deployment activation history",
    )
    if activation_history_snapshot.path != activation_history_candidate.resolve(
        strict=True
    ):
        raise ValueError("authorized fresh runtime latch names another activation history")
    activation_history = _verify_activation_history(
        history_path=activation_history_candidate,
        expected_commit=commit,
        repo_root=repo_candidate,
        machine_id_path=machine_id_path,
        snapshot=activation_history_snapshot,
    )
    if activation_history.get("artifact_sha256") != activation_history_identity.get(
        "artifact_sha256"
    ):
        raise ValueError("fresh deployment activation history artifact changed")
    authorization_snapshot = _stable_private_snapshot(
        authorization_candidate,
        label="cutover authorization receipt",
    )
    if _private_identity(
        authorization_candidate,
        label="cutover authorization receipt",
        snapshot=authorization_snapshot,
    ) != {
        key: value
        for key, value in _mapping(latch.get("authorization"), label="runtime latch authorization").items()
        if key != "artifact_sha256"
    }:
        raise ValueError("cutover authorization changed after fresh runtime activation")

    stopped_identity = _mapping(
        latch.get("stopped_natural_epoch"),
        label="runtime latch stopped natural epoch",
    )
    stopped_snapshot = _verify_latch_private_identity(
        stopped_identity,
        label="stopped natural epoch",
    )

    fresh_identity = _mapping(latch.get("fresh_deploy_epoch"), label="runtime latch fresh deploy epoch")
    fresh_path = Path(str(fresh_identity.get("path") or ""))
    fresh_snapshot = _stable_private_snapshot(
        fresh_path,
        label="fresh deploy epoch",
    )
    if _private_identity(
        fresh_path,
        label="fresh deploy epoch",
        snapshot=fresh_snapshot,
    ) != {
        key: value for key, value in fresh_identity.items() if key != "artifact_sha256"
    }:
        raise ValueError("fresh deploy epoch changed after runtime activation")
    environment_identity = _mapping(
        latch.get("environment_materialization"),
        label="runtime latch environment materialization",
    )
    receipt_path = Path(str(environment_identity.get("path") or ""))
    expected_receipt_path = output_candidate / "environment-materialization.json"
    if receipt_path != expected_receipt_path:
        raise ValueError("fresh runtime latch names another materialization receipt")
    receipt_snapshot = _stable_private_snapshot(
        receipt_path,
        label="fresh environment materialization receipt",
    )
    if _private_identity(
        receipt_path,
        label="fresh environment materialization receipt",
        snapshot=receipt_snapshot,
    ) != {
        key: value for key, value in environment_identity.items() if key != "artifact_sha256"
    }:
        raise ValueError("fresh environment materialization changed after runtime activation")

    output_metadata = output_candidate.lstat()
    if (
        stat.S_ISLNK(output_metadata.st_mode)
        or not stat.S_ISDIR(output_metadata.st_mode)
        or stat.S_IMODE(output_metadata.st_mode) != 0o700
        or output_metadata.st_uid != os.geteuid()
    ):
        raise ValueError("fresh runtime environment directory must be owner-owned mode 0700")
    fragments = _mapping(
        latch.get("environment_fragments"),
        label="runtime latch environment fragments",
    )
    expected_names = {"environment-materialization.json"}
    fragment_snapshots: list[StableFileSnapshot] = []
    for registered_unit in latch["units"]:
        fragment_identity = _mapping(
            fragments.get(registered_unit),
            label=f"runtime latch fragment for {registered_unit}",
        )
        fragment_path = Path(str(fragment_identity.get("path") or ""))
        expected_path = output_candidate / f"{registered_unit}.env"
        if fragment_path != expected_path:
            raise ValueError(f"fresh runtime latch names another fragment for {registered_unit}")
        fragment_snapshot = _stable_private_snapshot(
            fragment_path,
            label=f"fresh environment fragment for {registered_unit}",
        )
        if _private_identity(
            fragment_path,
            label=f"fresh environment fragment for {registered_unit}",
            snapshot=fragment_snapshot,
        ) != dict(fragment_identity):
            raise ValueError(f"fresh environment fragment changed for {registered_unit}")
        fragment_snapshots.append(fragment_snapshot)
        expected_names.add(fragment_path.name)
    if {item.name for item in output_candidate.iterdir()} != expected_names:
        raise ValueError("fresh runtime environment directory has unexpected or missing files")

    unit_environments = _mapping(
        latch.get("unit_environments"), label="runtime latch unit environments"
    )
    expected = _mapping(
        unit_environments.get(unit), label=f"fresh environment for {unit}"
    )
    if not expected:
        raise ValueError(f"fresh runtime latch does not register {unit}")

    observed = os.environ if observed_environment is None else observed_environment
    mismatches = {
        str(key): {"expected": str(value), "observed": observed.get(str(key))}
        for key, value in expected.items()
        if observed.get(str(key)) != str(value)
    }
    if mismatches:
        raise ValueError(f"unit {unit} did not load its authorized fresh environment: " + ", ".join(sorted(mismatches)))
    unchanged = [
        (latch_snapshot, "authorized fresh runtime latch"),
        (activation_history_snapshot, "fresh deployment activation history"),
        (authorization_snapshot, "cutover authorization receipt"),
        (stopped_snapshot, "stopped natural epoch"),
        (fresh_snapshot, "fresh deploy epoch"),
        (receipt_snapshot, "fresh environment materialization receipt"),
        *(
            (snapshot, f"fresh environment fragment {snapshot.path.name}")
            for snapshot in fragment_snapshots
        ),
    ]
    for snapshot, label in unchanged:
        if _stable_private_snapshot(snapshot.path, label=label) != snapshot:
            raise RuntimeError(f"{label} changed during verification")
    return {
        "status": "authorized_fresh_runtime_verified",
        "unit": unit,
        "authorized_commit": commit,
        "verified_keys": sorted(str(key) for key in expected),
        "execution_authorization": "inherited_from_runtime_latch",
    }


def _evidence_path(receipt: Mapping[str, Any], *, role: str) -> Path:
    evidence = _mapping(receipt.get("evidence"), label="authorization evidence")
    paths: list[Path] = []
    for evidence_id, raw in evidence.items():
        entry = _mapping(raw, label=f"authorization evidence {evidence_id!r}")
        if entry.get("role") != role:
            continue
        path = Path(str(entry.get("path") or "")).expanduser()
        if not path.is_absolute():
            raise ValueError(f"authorization role {role!r} has a non-absolute path")
        paths.append(path.resolve(strict=True))
    if len(paths) != 1:
        raise ValueError(f"authorization must bind exactly one {role!r} artifact, found {len(paths)}")
    return paths[0]


def _load_stopped_epoch(
    path: Path,
    *,
    require_currently_stopped: bool,
    systemctl_bin: str,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    # The import remains local so install-preflight can stage a candidate before
    # the first stopped-epoch receipt exists. Full prepare/verify still fails
    # closed if the registered implementation is absent.
    from .stopped_natural_epoch import load_stopped_natural_epoch_seal

    return load_stopped_natural_epoch_seal(
        path,
        require_currently_stopped=require_currently_stopped,
        systemctl_bin=systemctl_bin,
        snapshot=snapshot,
    )


def _basic_epoch_crosscheck(
    *,
    expected_commit: str,
    stopped_path: Path,
    stopped: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> None:
    stopped_identity = _mapping(stopped.get("identity"), label="stopped natural epoch identity")
    if stopped_identity.get("candidate_commit") != expected_commit:
        raise ValueError("stopped natural epoch belongs to another candidate commit")
    if fresh.get("candidate_commit") != expected_commit:
        raise ValueError("fresh deploy epoch belongs to another candidate commit")
    if stopped_identity.get("freeze_id") != fresh.get("freeze_id"):
        raise ValueError("stopped and fresh epochs bind different natural freezes")
    if stopped.get("execution_authorization") != "not_granted":
        raise ValueError("stopped natural epoch overstates execution authority")
    if fresh.get("execution_authorization") != "not_granted":
        raise ValueError("fresh deploy epoch overstates execution authority")
    if int(fresh.get("created_ts_ns") or 0) <= int(stopped.get("created_ts_ns") or 0):
        raise ValueError("fresh and stopped epoch declared chronology is inconsistent")
    seal = _mapping(fresh.get("stopped_epoch_seal"), label="fresh epoch stopped-seal identity")
    if Path(str(seal.get("path") or "")).resolve(strict=True) != stopped_path:
        raise ValueError("fresh deploy epoch references another stopped seal")


def _authorized_epoch(
    *,
    authorization_path: str | Path,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path,
    require_currently_stopped: bool,
    require_empty_roots: bool,
    systemctl_bin: str,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path, dict[str, Any]]:
    authorization_snapshot = _stable_private_snapshot(
        authorization_path,
        label="cutover authorization receipt",
    )
    authorization = load_authorization_receipt(
        authorization_path,
        expected_commit=expected_commit,
        repo_root=repo_root,
        machine_id_path=machine_id_path,
        snapshot=authorization_snapshot,
    )
    stopped_path = _evidence_path(authorization, role=STOPPED_ROLE)
    fresh_path = _evidence_path(authorization, role=FRESH_ROLE)
    stopped_snapshot = _stable_private_snapshot(
        stopped_path,
        label="stopped natural epoch",
    )
    fresh_snapshot = _stable_private_snapshot(
        fresh_path,
        label="fresh deploy epoch",
    )
    stopped = _load_stopped_epoch(
        stopped_path,
        require_currently_stopped=require_currently_stopped,
        systemctl_bin=systemctl_bin,
        snapshot=stopped_snapshot,
    )
    fresh = load_fresh_deploy_epoch(
        fresh_path,
        require_empty_roots=require_empty_roots,
        snapshot=fresh_snapshot,
    )
    _basic_epoch_crosscheck(
        expected_commit=expected_commit,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh=fresh,
    )
    for snapshot, label in (
        (authorization_snapshot, "cutover authorization receipt"),
        (stopped_snapshot, "stopped natural epoch"),
        (fresh_snapshot, "fresh deploy epoch"),
    ):
        if _stable_private_snapshot(snapshot.path, label=label) != snapshot:
            raise RuntimeError(f"{label} changed during authorized epoch verification")
    return authorization, stopped_path, stopped, fresh_path, fresh


def prepare_authorized_deploy_epoch(
    *,
    authorization_path: str | Path,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path = "/etc/machine-id",
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    pre_cutover_marker_path: str | Path = DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    systemctl_bin: str = "systemctl",
) -> dict[str, Any]:
    """Materialize exact late environment files before the first unit starts."""

    authorization, stopped_path, stopped, fresh_path, fresh = _authorized_epoch(
        authorization_path=authorization_path,
        expected_commit=expected_commit,
        repo_root=repo_root,
        machine_id_path=machine_id_path,
        require_currently_stopped=True,
        require_empty_roots=True,
        systemctl_bin=systemctl_bin,
    )
    repository = Path(repo_root).expanduser().resolve(strict=True)
    pre_cutover_marker = Path(pre_cutover_marker_path).expanduser()
    if not pre_cutover_marker.is_absolute():
        raise ValueError("pre-cutover runtime marker path must be absolute")
    output_candidate = Path(output_directory).expanduser()
    if not output_candidate.is_absolute():
        raise ValueError("fresh-deploy environment directory must be absolute")
    latch_candidate = _runtime_latch_path(output_candidate)
    activation_history_candidate = _activation_history_path(output_candidate)
    existing = sorted(
        name
        for name, path in {
            "activation_history": activation_history_candidate,
            "environment": output_candidate,
            "runtime_latch": latch_candidate,
        }.items()
        if os.path.lexists(path)
    )
    if existing:
        raise ValueError(
            "fresh deployment activation is already started or partial: "
            + ", ".join(existing)
        )
    pre_cutover = _verify_pre_cutover_runtime_marker(
        marker_path=pre_cutover_marker,
        expected_commit=expected_commit,
        repo_root=repository,
        machine_id_path=machine_id_path,
    )
    activation_history_path, activation_history = _prepare_activation_history(
        history_path=activation_history_candidate,
        expected_commit=expected_commit,
        repo_root=repository,
        machine_id_path=machine_id_path,
        pre_cutover_marker_path=pre_cutover_marker,
        pre_cutover_marker=pre_cutover,
    )
    receipt_path = materialize_fresh_deploy_environment(
        manifest_path=fresh_path,
        output_directory=output_directory,
        require_empty_roots=True,
    )
    environment = verify_fresh_deploy_environment(
        manifest_path=fresh_path,
        output_directory=output_directory,
        require_empty_roots=True,
    )
    output = Path(output_directory).expanduser().resolve(strict=True)
    authorization_receipt = Path(authorization_path).expanduser().resolve(strict=True)
    latch_path = _write_or_verify_runtime_latch(
        latch_path=_runtime_latch_path(output),
        authorization_path=authorization_receipt,
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh_path=fresh_path,
        fresh=fresh,
        environment_receipt_path=receipt_path.resolve(strict=True),
        environment=environment,
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=machine_id_path,
    )
    _verify_runtime_latch_dependencies(
        latch_path=latch_path,
        authorization_path=authorization_receipt,
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh_path=fresh_path,
        fresh=fresh,
        environment_receipt_path=receipt_path.resolve(strict=True),
        environment=environment,
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=machine_id_path,
    )
    _remove_pre_cutover_runtime_marker(pre_cutover_marker)
    return _result(
        status="prepared",
        authorization=authorization,
        stopped_path=stopped_path,
        fresh_path=fresh_path,
        fresh=fresh,
        environment=environment,
        environment_receipt_path=receipt_path,
        runtime_latch_path=latch_path,
        activation_history_path=activation_history_path,
    )


def verify_authorized_deploy_epoch(
    *,
    authorization_path: str | Path,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path = "/etc/machine-id",
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    pre_cutover_marker_path: str | Path = DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    systemctl_bin: str = "systemctl",
) -> dict[str, Any]:
    """Verify the immutable activation latch after authorization is spent."""

    (
        authorization,
        stopped_path,
        _stopped,
        fresh_path,
        fresh,
        environment,
        latch_path,
        _latch,
    ) = _verify_activated_epoch(
        authorization_path=authorization_path,
        expected_commit=expected_commit,
        repo_root=repo_root,
        machine_id_path=machine_id_path,
        output_directory=output_directory,
        pre_cutover_marker_path=pre_cutover_marker_path,
        systemctl_bin=systemctl_bin,
    )
    output = Path(output_directory).expanduser().resolve(strict=True)
    receipt_path = output / "environment-materialization.json"
    return _result(
        status="verified",
        authorization=authorization,
        stopped_path=stopped_path,
        fresh_path=fresh_path,
        fresh=fresh,
        environment=environment,
        environment_receipt_path=receipt_path,
        runtime_latch_path=latch_path,
        activation_history_path=_activation_history_path(output),
    )


def _main_pid(*, unit: str, systemctl_bin: str) -> int:
    if not re.fullmatch(r"liquidity-migration-[a-z0-9-]+\.service", unit):
        raise ValueError(f"invalid liquidity-migration service name: {unit!r}")
    result = subprocess.run(
        [
            systemctl_bin,
            "show",
            unit,
            "--property=MainPID",
            "--value",
            "--no-pager",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"cannot inspect MainPID for active unit {unit}")
    raw = result.stdout.strip()
    if not raw.isdigit() or int(raw) <= 1:
        raise ValueError(f"unit {unit} has no active main process")
    return int(raw)


def _read_process_environment(path: Path, *, unit: str) -> dict[str, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read active process environment for {unit}: {exc}") from exc
    output: dict[str, str] = {}
    for raw in data.split(b"\0"):
        if not raw:
            continue
        if b"=" not in raw:
            raise ValueError(f"active process environment for {unit} is malformed")
        key_bytes, value_bytes = raw.split(b"=", 1)
        try:
            key = key_bytes.decode("utf-8")
            value = value_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"active process environment for {unit} is not UTF-8") from exc
        if key in output:
            raise ValueError(f"active process environment for {unit} repeats {key}")
        output[key] = value
    return output


def verify_authorized_process_environments(
    *,
    authorization_path: str | Path,
    expected_commit: str,
    repo_root: str | Path,
    units: Sequence[str],
    machine_id_path: str | Path = "/etc/machine-id",
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    pre_cutover_marker_path: str | Path = DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    systemctl_bin: str = "systemctl",
    proc_root: str | Path = "/proc",
) -> dict[str, Any]:
    """Prove active owner/producer processes consumed their exact late map."""

    if not units or len(set(units)) != len(units):
        raise ValueError("active process verification requires distinct units")
    epoch = verify_authorized_deploy_epoch(
        authorization_path=authorization_path,
        expected_commit=expected_commit,
        repo_root=repo_root,
        machine_id_path=machine_id_path,
        output_directory=output_directory,
        pre_cutover_marker_path=pre_cutover_marker_path,
        systemctl_bin=systemctl_bin,
    )
    latch = _load_runtime_latch(Path(str(epoch["runtime_latch_path"])))
    late = _mapping(
        latch.get("unit_environments"),
        label="runtime latch unit environments",
    )
    process_root = Path(proc_root).expanduser()
    if not process_root.is_absolute():
        raise ValueError("proc root must be absolute")
    verified: dict[str, dict[str, Any]] = {}
    for unit in units:
        raw_expected = late.get(unit)
        expected = _mapping(raw_expected, label=f"fresh environment for {unit}")
        if not expected:
            raise ValueError(f"fresh epoch does not register environment for {unit}")
        pid_before = _main_pid(unit=unit, systemctl_bin=systemctl_bin)
        observed = _read_process_environment(process_root / str(pid_before) / "environ", unit=unit)
        pid_after = _main_pid(unit=unit, systemctl_bin=systemctl_bin)
        if pid_after != pid_before:
            raise ValueError(f"unit {unit} restarted during environment verification")
        mismatches = {
            str(key): {"expected": str(value), "observed": observed.get(str(key))}
            for key, value in expected.items()
            if observed.get(str(key)) != value
        }
        if mismatches:
            names = ", ".join(sorted(mismatches))
            raise ValueError(f"active unit {unit} did not consume fresh values: {names}")
        verified[unit] = {
            "main_pid": pid_before,
            "verified_keys": sorted(str(key) for key in expected),
        }
    return {**epoch, "status": "process_environments_verified", "units": verified}


def _result(
    *,
    status: str,
    authorization: Mapping[str, Any],
    stopped_path: Path,
    fresh_path: Path,
    fresh: Mapping[str, Any],
    environment: Mapping[str, Any],
    environment_receipt_path: Path,
    runtime_latch_path: Path,
    activation_history_path: Path,
) -> dict[str, Any]:
    roots = _mapping(fresh.get("roots"), label="fresh deploy roots")
    return {
        "status": status,
        "authorized_commit": authorization.get("authorized_commit"),
        "authorization_artifact_sha256": authorization.get("artifact_sha256"),
        "stopped_natural_epoch_path": str(stopped_path),
        "fresh_deploy_epoch_path": str(fresh_path),
        "fresh_deploy_epoch_artifact_sha256": fresh.get("artifact_sha256"),
        "fresh_roots": {
            role: str(_mapping(value, label=f"fresh root {role}").get("path") or "")
            for role, value in sorted(roots.items())
        },
        "environment_directory": environment.get("output_directory"),
        "environment_receipt_path": str(environment_receipt_path.resolve(strict=True)),
        "environment_artifact_sha256": environment.get("artifact_sha256"),
        "runtime_latch_path": str(runtime_latch_path.resolve(strict=True)),
        "activation_history_path": str(activation_history_path.resolve(strict=True)),
        "execution_authorization": "not_granted_by_epoch_artifacts",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify the fresh filesystem epoch bound by cutover authority"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("prepare", "require stopped services and empty roots, then materialize environment files"),
        ("verify", "verify an already-materialized epoch after roots may be populated"),
        ("verify-processes", "verify active processes consumed their exact late environment"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--authorization", type=Path, required=True)
        command.add_argument("--expected-commit", required=True)
        command.add_argument("--repo-root", type=Path, required=True)
        command.add_argument("--machine-id-path", type=Path, default=Path("/etc/machine-id"))
        command.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
        command.add_argument(
            "--pre-cutover-marker",
            type=Path,
            default=DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
        )
        command.add_argument("--systemctl-bin", default="systemctl")
        if name == "verify-processes":
            command.add_argument("--unit", action="append", required=True)
            command.add_argument("--proc-root", type=Path, default=Path("/proc"))
    runtime = commands.add_parser(
        "verify-runtime",
        help="fail closed on each service start after fresh-root activation",
    )
    runtime.add_argument("--authorization", type=Path, required=True)
    runtime.add_argument("--repo-root", type=Path, required=True)
    runtime.add_argument("--machine-id-path", type=Path, default=Path("/etc/machine-id"))
    runtime.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    runtime.add_argument(
        "--pre-cutover-marker",
        type=Path,
        default=DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    )
    runtime.add_argument("--unit", required=True)
    phase = commands.add_parser(
        "phase",
        help="classify preactivation, activated, or partial filesystem state",
    )
    phase.add_argument("--authorization", type=Path, required=True)
    phase.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    phase.add_argument(
        "--pre-cutover-marker",
        type=Path,
        default=DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    )
    phase.add_argument("--plain", action="store_true")
    evidence = commands.add_parser(
        "prepare-evidence-runtime",
        help="bind the pre-cutover evidence window to one clean commit and machine",
    )
    evidence.add_argument("--expected-commit", required=True)
    evidence.add_argument("--repo-root", type=Path, required=True)
    evidence.add_argument("--machine-id-path", type=Path, default=Path("/etc/machine-id"))
    evidence.add_argument(
        "--authorization",
        type=Path,
        default=DEFAULT_CUTOVER_AUTHORIZATION,
    )
    evidence.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
    )
    evidence.add_argument(
        "--pre-cutover-marker",
        type=Path,
        default=DEFAULT_PRE_CUTOVER_RUNTIME_MARKER,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "phase":
            result = classify_authorized_deploy_phase(
                authorization_path=args.authorization,
                output_directory=args.output_directory,
                pre_cutover_marker_path=args.pre_cutover_marker,
            )
        elif args.command == "prepare-evidence-runtime":
            result = prepare_pre_cutover_runtime_marker(
                marker_path=args.pre_cutover_marker,
                expected_commit=args.expected_commit.lower(),
                repo_root=args.repo_root,
                machine_id_path=args.machine_id_path,
                authorization_path=args.authorization,
                output_directory=args.output_directory,
            )
        elif args.command == "verify-runtime":
            result = verify_runtime_fresh_epoch(
                authorization_path=args.authorization,
                repo_root=args.repo_root,
                machine_id_path=args.machine_id_path,
                output_directory=args.output_directory,
                pre_cutover_marker_path=args.pre_cutover_marker,
                unit=args.unit,
            )
        else:
            common = {
                "authorization_path": args.authorization,
                "expected_commit": args.expected_commit.lower(),
                "repo_root": args.repo_root,
                "machine_id_path": args.machine_id_path,
                "output_directory": args.output_directory,
                "pre_cutover_marker_path": args.pre_cutover_marker,
                "systemctl_bin": args.systemctl_bin,
            }
            if args.command == "prepare":
                result = prepare_authorized_deploy_epoch(**common)
            elif args.command == "verify":
                result = verify_authorized_deploy_epoch(**common)
            else:
                result = verify_authorized_process_environments(
                    **common,
                    units=args.unit,
                    proc_root=args.proc_root,
                )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"authorized deploy epoch failed: {exc}", file=sys.stderr)
        return 2
    if args.command == "phase" and args.plain:
        print(result["phase"])
        return 0
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FRESH_ROLE",
    "STOPPED_ROLE",
    "classify_authorized_deploy_phase",
    "prepare_authorized_deploy_epoch",
    "prepare_pre_cutover_runtime_marker",
    "verify_authorized_deploy_epoch",
    "verify_authorized_process_environments",
    "verify_runtime_fresh_epoch",
]
