"""Seal the stopped natural-window namespace before fresh-root creation.

The seal is integrity evidence, not a filesystem lock. Creation requires the
registered owner/producer/auxiliary units to be inactive both before and after
hashing. Verification always reopens every source file and tree; callers may
optionally require the units to remain inactive for the immediately-prestart
gate. Post-start authorization verification deliberately omits that live-state
requirement while retaining all content and inode checks.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json


SCHEMA_VERSION = 1
KIND = "stopped_natural_epoch_seal"
VALIDATOR = "stopped_natural_epoch_v1"
EXECUTION_AUTHORIZATION = "not_granted"

OLD_ROOT_ROLES = (
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

REQUIRED_INPUT_FILE_ROLES = (
    "freeze_manifest",
    "effective_runtime_config",
    "clock_offset_series",
    "natural_safety_flatten",
    "venue_accounting",
)

REGISTERED_UNITS = (
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-continuous-hedge.service",
    "liquidity-migration-continuous-hedge.timer",
    "liquidity-migration-continuous-rmom-refresh.service",
    "liquidity-migration-continuous-rmom-refresh.timer",
    "liquidity-migration-demo-liveness.service",
    "liquidity-migration-demo-liveness.timer",
)

_PAPER_ROOT_UNITS = {
    "long_paper": "liquidity-migration-bybit-long-paper.service",
    "continuous_paper": "liquidity-migration-bybit-continuous-paper.service",
}

_DEMO_ROOT_SLEEVES = {
    "long_demo": "LONG",
    "continuous_demo": "CONTINUOUS",
}

TAPE_SEMANTICS: Mapping[str, Any] = {
    "interval": "half_open_[t0,t1)",
    "registered_profiles": ["LONG", "CONTINUOUS"],
    "target_environments": ["demo", "paper"],
    "replay_environments": ["historical", "paper", "demo"],
    "deterministic_clock": "strategy_event_clock",
    "lifecycle_order": ["target", "order", "ack", "fill", "pnl"],
    "post_t1_safety_batches_excluded_from_natural_metrics": True,
}


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _self_hash(payload: Mapping[str, Any]) -> str:
    return _sha256(canonical_json({**dict(payload), "artifact_sha256": ""}))


def _strict_json_bytes(data: bytes, *, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in values:
            if key in output:
                raise ValueError(f"{label} repeats JSON key {key!r}")
            output[key] = value
        return output

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _absolute_no_symlink(
    path: str | Path,
    *,
    label: str,
    require_exists: bool,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be absolute")
    current = Path(candidate.anchor)
    parts = candidate.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if require_exists or index != len(parts) - 1:
                raise ValueError(f"{label} is unavailable: {candidate}") from None
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} must not traverse a symbolic link")
    return candidate.resolve(strict=require_exists)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _path_identity(path: Path, *, label: str) -> dict[str, Any]:
    resolved = _absolute_no_symlink(path, label=label, require_exists=True)
    metadata = resolved.stat()
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        raise ValueError(f"{label} must be a regular file or directory")
    return {
        "path": str(resolved),
        "kind": kind,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }


def _snapshot_identity(snapshot: StableFileSnapshot) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "sha256": snapshot.sha256,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "mtime_ns": snapshot.mtime_ns,
        "mode": snapshot.mode,
        "uid": snapshot.uid,
    }


def _file_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    source = _absolute_no_symlink(path, label=label, require_exists=True)
    return read_stable_file(
        source,
        label=label,
        require_single_link=True,
    )


def _file_identity(path: str | Path, *, label: str) -> dict[str, Any]:
    return _snapshot_identity(_file_snapshot(path, label=label))


def _tree_snapshot(root: Path, *, role: str) -> dict[str, Any]:
    identity_before = _path_identity(root, label=f"old {role} root")
    if identity_before["kind"] != "directory":
        raise ValueError(f"old {role} root must be a directory")
    entries: list[dict[str, Any]] = []
    observed_inodes: dict[tuple[int, int], str] = {}
    for item in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix()
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"old {role} tree contains symbolic link {relative!r}")
        key = (metadata.st_dev, metadata.st_ino)
        prior = observed_inodes.get(key)
        if prior is not None:
            raise ValueError(f"old {role} tree aliases inode between {prior!r} and {relative!r}")
        observed_inodes[key] = relative
        common = {
            "relative_path": relative,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "mtime_ns": metadata.st_mtime_ns,
        }
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({**common, "kind": "directory"})
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"old {role} tree contains non-regular entry {relative!r}")
        if metadata.st_nlink != 1:
            raise ValueError(f"old {role} tree contains hard-linked file {relative!r}")
        file_identity = _file_identity(item, label=f"old {role} file {relative}")
        if any(
            file_identity[key] != common[key]
            for key in ("device", "inode", "mtime_ns", "mode", "uid")
        ):
            raise RuntimeError(f"old {role} file {relative!r} changed during enumeration")
        entries.append(
            {
                **common,
                "kind": "file",
                "size_bytes": file_identity["size_bytes"],
                "sha256": file_identity["sha256"],
            }
        )
    identity_after = _path_identity(root, label=f"old {role} root")
    if identity_after != identity_before:
        raise RuntimeError(f"old {role} root changed while it was hashed")
    return {
        "root_identity": identity_before,
        "entry_count": len(entries),
        "entries": entries,
        "tree_sha256": _sha256(canonical_json({"entries": entries})),
    }


def _unit_state(*, unit: str, systemctl_bin: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            systemctl_bin,
            "show",
            unit,
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--value",
            "--no-pager",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"cannot inspect systemd unit {unit}")
    values = result.stdout.splitlines()
    if len(values) != 3 or not values[2].strip().isdigit():
        raise ValueError(f"systemd returned malformed state for {unit}")
    state = {
        "unit": unit,
        "active_state": values[0].strip(),
        "sub_state": values[1].strip(),
        "main_pid": int(values[2].strip()),
    }
    state["inactive"] = state["active_state"] == "inactive" and state["main_pid"] == 0
    return state


def _fleet_state(*, systemctl_bin: str) -> list[dict[str, Any]]:
    states = [_unit_state(unit=unit, systemctl_bin=systemctl_bin) for unit in REGISTERED_UNITS]
    if not all(state["inactive"] is True for state in states):
        active = [state["unit"] for state in states if state["inactive"] is not True]
        raise ValueError(f"registered natural units are not all inactive: {active}")
    return states


def _full_commit(value: object, *, label: str) -> str:
    commit = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"{label} must be a full lowercase Git commit")
    return commit


def _artifact_hash(payload: Mapping[str, Any], *, label: str) -> str:
    digest = str(payload.get("artifact_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{label} lacks a lowercase artifact hash")
    return digest


def _source_file(
    *,
    role: str,
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], StableFileSnapshot]:
    snapshot = _file_snapshot(path, label=f"stopped epoch input {role}")
    identity = _snapshot_identity(snapshot)
    if identity["mode"] != 0o600 or identity["uid"] != os.geteuid():
        raise ValueError(f"stopped epoch input {role} must be owner-owned mode 0600")
    payload = _strict_json_bytes(snapshot.data, label=f"stopped epoch input {role}")
    compact = {
        "path": identity["path"],
        "sha256": identity["sha256"],
        "artifact_sha256": _artifact_hash(payload, label=f"stopped epoch input {role}"),
    }
    return identity, compact, payload, snapshot


def _normalize_inputs(
    values: Mapping[str, str | Path],
) -> dict[str, Path]:
    if set(values) != set(REQUIRED_INPUT_FILE_ROLES):
        missing = sorted(set(REQUIRED_INPUT_FILE_ROLES) - set(values))
        extra = sorted(set(values) - set(REQUIRED_INPUT_FILE_ROLES))
        raise ValueError(f"stopped epoch input roles differ: missing={missing}, extra={extra}")
    return {
        role: _absolute_no_symlink(values[role], label=f"stopped epoch input {role}", require_exists=True)
        for role in REQUIRED_INPUT_FILE_ROLES
    }


def _normalize_roots(values: Mapping[str, str | Path]) -> dict[str, Path]:
    if set(values) != set(OLD_ROOT_ROLES):
        missing = sorted(set(OLD_ROOT_ROLES) - set(values))
        extra = sorted(set(values) - set(OLD_ROOT_ROLES))
        raise ValueError(f"old mutable root roles differ: missing={missing}, extra={extra}")
    roots = {
        role: _absolute_no_symlink(values[role], label=f"old {role} root", require_exists=True)
        for role in OLD_ROOT_ROLES
    }
    paths = list(roots.values())
    if any(_paths_overlap(left, right) for index, left in enumerate(paths) for right in paths[index + 1 :]):
        raise ValueError("old mutable roots must be pairwise disjoint and non-nested")
    identities = [_path_identity(path, label=f"old {role} root") for role, path in roots.items()]
    if len({(value["device"], value["inode"]) for value in identities}) != len(identities):
        raise ValueError("old mutable roots alias an inode")
    return roots


def _require_loaded_source(
    *,
    role: str,
    loaded: Mapping[str, Any],
    source_payload: Mapping[str, Any],
) -> None:
    if canonical_json(dict(loaded)) != canonical_json(dict(source_payload)):
        raise ValueError(f"stopped epoch input {role} does not reproduce from its semantic loader")


def _loader_accepts_keyword(loader: object, keyword: str) -> bool:
    try:
        return keyword in inspect.signature(loader).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _systemd_logical_lines(text: str, *, label: str) -> list[str]:
    logical: list[str] = []
    pending = ""
    for physical in text.splitlines():
        line = f"{pending}{physical.lstrip()}" if pending else physical
        trimmed = line.rstrip()
        trailing_slashes = len(trimmed) - len(trimmed.rstrip("\\"))
        if trailing_slashes % 2:
            pending = f"{trimmed[:-1]} "
            continue
        logical.append(line)
        pending = ""
    if pending:
        raise ValueError(f"{label} ends in an unterminated continuation")
    return logical


def _paper_root_from_candidate_unit(*, repository_root: Path, role: str) -> Path:
    unit_name = _PAPER_ROOT_UNITS[role]
    unit_path = _absolute_no_symlink(
        repository_root / "deploy" / "systemd" / unit_name,
        label=f"candidate systemd unit {unit_name}",
        require_exists=True,
    )
    snapshot = _file_snapshot(unit_path, label=f"candidate systemd unit {unit_name}")
    try:
        text = snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"candidate systemd unit {unit_name} is not UTF-8") from exc

    section = ""
    declarations: list[str] = []
    working_directories: list[str] = []
    for raw_line in _systemd_logical_lines(text, label=f"candidate systemd unit {unit_name}"):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Service":
            continue
        if line.startswith("WorkingDirectory="):
            working_directories.append(line.partition("=")[2].strip())
            continue
        if not line.startswith("Environment="):
            continue
        try:
            assignments = shlex.split(line.partition("=")[2], comments=False, posix=True)
        except ValueError as exc:
            raise ValueError(f"candidate systemd unit {unit_name} has malformed Environment syntax") from exc
        declarations.extend(
            assignment.partition("=")[2] for assignment in assignments if assignment.startswith("DATA_ROOT=")
        )
    if len(declarations) != 1 or not declarations[0]:
        raise ValueError(f"candidate systemd unit {unit_name} must declare exactly one DATA_ROOT")
    if len(working_directories) != 1 or not working_directories[0]:
        raise ValueError(f"candidate systemd unit {unit_name} must declare exactly one WorkingDirectory")
    working_directory = Path(working_directories[0])
    if not working_directory.is_absolute() or working_directory.resolve(strict=False) != repository_root:
        raise ValueError(f"candidate systemd unit {unit_name} WorkingDirectory differs from the verified repository")
    declared = Path(declarations[0])
    candidate = declared if declared.is_absolute() else repository_root / declared
    resolved = _absolute_no_symlink(
        candidate,
        label=f"candidate systemd unit {unit_name} DATA_ROOT",
        require_exists=True,
    )
    if repository_root not in resolved.parents:
        raise ValueError(f"candidate systemd unit {unit_name} DATA_ROOT must remain under the verified repository")
    if _path_identity(resolved, label=f"candidate systemd unit {unit_name} DATA_ROOT")["kind"] != "directory":
        raise ValueError(f"candidate systemd unit {unit_name} DATA_ROOT must be a directory")
    return resolved


def _semantic_epoch_identity(
    *,
    input_paths: Mapping[str, Path],
    source_files: Mapping[str, Mapping[str, Any]],
    source_payloads: Mapping[str, Mapping[str, Any]],
    source_snapshots: Mapping[str, StableFileSnapshot],
    roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Rebuild every pre-seal source and derive the one canonical old namespace."""

    from .account_venue_accounting import load_venue_accounting_receipt
    from .captured_account_replay import load_post_window_safety_manifest
    from .clock_offset_series import load_clock_offset_series
    from .natural_cutover_freeze_manifest import load_natural_cutover_freeze_manifest
    from .natural_effective_config import load_effective_runtime_config_bundle_binding

    freeze_path = input_paths["freeze_manifest"]
    freeze_file = source_files["freeze_manifest"]
    if _loader_accepts_keyword(load_natural_cutover_freeze_manifest, "snapshot"):
        freeze = load_natural_cutover_freeze_manifest(
            freeze_path,
            snapshot=source_snapshots["freeze_manifest"],
        )
    else:  # Test doubles and legacy callers cannot consume descriptor snapshots.
        freeze = load_natural_cutover_freeze_manifest(freeze_path)
    _require_loaded_source(
        role="freeze_manifest",
        loaded=freeze,
        source_payload=source_payloads["freeze_manifest"],
    )
    repository = _mapping(freeze.get("repository"), label="natural freeze repository")
    repository_root = _absolute_no_symlink(
        str(repository.get("root") or ""),
        label="verified natural repository root",
        require_exists=True,
    )
    if _path_identity(repository_root, label="verified natural repository root")["kind"] != "directory":
        raise ValueError("verified natural repository root must be a directory")
    candidate_commit = _full_commit(repository.get("candidate_commit"), label="candidate commit")
    origin_main_commit = _full_commit(repository.get("origin_main_commit"), label="origin/main commit")
    window = _mapping(freeze.get("window"), label="natural freeze window")
    t0_ns = int(window.get("t0_ns") or 0)
    t1_ns = int(window.get("t1_ns") or 0)
    if t0_ns <= 0 or t1_ns - t0_ns != 120 * 60 * 60 * 1_000_000_000:
        raise ValueError("natural freeze does not bind the exact 120-hour window")
    freeze_id = str(freeze.get("freeze_id") or "")
    if not re.fullmatch(r"natural-cutover-[0-9a-f]{64}", freeze_id):
        raise ValueError("natural freeze lacks its identity")
    freeze_artifact = _artifact_hash(freeze, label="natural freeze manifest")

    runtime = _mapping(freeze.get("runtime"), label="natural freeze runtime")
    runtime_roots = _mapping(runtime.get("roots"), label="natural freeze roots")
    account_ids = _mapping(runtime.get("account_ids"), label="natural freeze account ids")
    demo_account_id = str(account_ids.get("demo") or "")
    if not demo_account_id:
        raise ValueError("natural freeze lacks the demo account identity")

    expected_roots: dict[str, Path] = {}
    for environment in ("demo", "paper"):
        environment_roots = _mapping(
            runtime_roots.get(environment),
            label=f"natural freeze {environment} roots",
        )
        for kind in ("account", "inbox", "capture"):
            role = f"{environment}_{kind}"
            expected_roots[role] = _absolute_no_symlink(
                str(environment_roots.get(kind) or ""),
                label=f"natural freeze {role} root",
                require_exists=True,
            )

    if _loader_accepts_keyword(
        load_effective_runtime_config_bundle_binding,
        "snapshot",
    ):
        effective_payload, effective_binding = load_effective_runtime_config_bundle_binding(
            input_paths["effective_runtime_config"],
            snapshot=source_snapshots["effective_runtime_config"],
        )
    else:  # Test doubles and legacy callers cannot consume descriptor snapshots.
        effective_payload, effective_binding = load_effective_runtime_config_bundle_binding(
            input_paths["effective_runtime_config"]
        )
    _require_loaded_source(
        role="effective_runtime_config",
        loaded=effective_payload,
        source_payload=source_payloads["effective_runtime_config"],
    )
    effective_file = source_files["effective_runtime_config"]
    if (
        effective_binding.get("path") != effective_file["path"]
        or effective_binding.get("file_sha256") != effective_file["sha256"]
        or effective_binding.get("artifact_sha256")
        != _artifact_hash(effective_payload, label="effective runtime config bundle")
    ):
        raise ValueError("effective runtime config bundle binding differs from its exact source file")
    expected_repository = {
        "root": str(repository_root),
        "candidate_commit": candidate_commit,
        "origin_main_commit": origin_main_commit,
    }
    if dict(_mapping(effective_binding.get("repository"), label="effective repository binding")) != expected_repository:
        raise ValueError("effective runtime config bundle names another repository")
    expected_freeze_binding = {
        "path": freeze_file["path"],
        "file_sha256": freeze_file["sha256"],
        "artifact_sha256": freeze_artifact,
        "freeze_id": freeze_id,
    }
    if dict(_mapping(effective_binding.get("freeze"), label="effective freeze binding")) != (expected_freeze_binding):
        raise ValueError("effective runtime config bundle names another natural freeze")
    expected_window = {
        "t0_ns": t0_ns,
        "t1_ns": t1_ns,
        "interval": "half_open_[t0,t1)",
    }
    if dict(_mapping(effective_binding.get("window"), label="effective window binding")) != (expected_window):
        raise ValueError("effective runtime config bundle names another natural window")

    runtime_paths = _mapping(effective_binding.get("runtime_paths"), label="effective runtime paths")
    sleeve_paths = _mapping(runtime_paths.get("sleeves"), label="effective sleeve paths")
    for role, sleeve in _DEMO_ROOT_SLEEVES.items():
        sleeve_runtime = _mapping(sleeve_paths.get(sleeve), label=f"effective {sleeve} runtime paths")
        expected_roots[role] = _absolute_no_symlink(
            str(sleeve_runtime.get("data_root") or ""),
            label=f"effective {sleeve} data root",
            require_exists=True,
        )
    target_capture = _absolute_no_symlink(
        str(runtime_paths.get("target_capture_path") or ""),
        label="effective natural target capture",
        require_exists=True,
    )
    if _path_identity(target_capture, label="effective natural target capture")["kind"] != "file":
        raise ValueError("effective natural target capture must be a regular file")
    expected_roots["natural_evidence"] = target_capture.parent
    for role in _PAPER_ROOT_UNITS:
        expected_roots[role] = _paper_root_from_candidate_unit(
            repository_root=repository_root,
            role=role,
        )

    mismatched_roots = [role for role in OLD_ROOT_ROLES if roots[role] != expected_roots[role]]
    if mismatched_roots:
        raise ValueError(f"old mutable roots differ from canonical freeze/effective/systemd roots: {mismatched_roots}")

    if _loader_accepts_keyword(load_clock_offset_series, "snapshot"):
        clock = load_clock_offset_series(
            input_paths["clock_offset_series"],
            snapshot=source_snapshots["clock_offset_series"],
        )
    else:
        clock = load_clock_offset_series(input_paths["clock_offset_series"])
    _require_loaded_source(
        role="clock_offset_series",
        loaded=clock,
        source_payload=source_payloads["clock_offset_series"],
    )
    clock_freeze = _mapping(clock.get("freeze"), label="clock-offset series freeze")
    clock_freeze_source = _mapping(clock_freeze.get("source_identity"), label="clock-offset series freeze source")
    freeze_source_fields = {
        "path": freeze_file["path"],
        "size_bytes": freeze_file["size_bytes"],
        "sha256": freeze_file["sha256"],
        "device": freeze_file["device"],
        "inode": freeze_file["inode"],
        "mtime_ns": freeze_file["mtime_ns"],
        "mode": freeze_file["mode"],
        "uid": freeze_file["uid"],
    }
    if any(clock_freeze_source.get(key) != value for key, value in freeze_source_fields.items()):
        raise ValueError("clock-offset series names another freeze source file")
    if clock_freeze.get("freeze_id") != freeze_id or clock_freeze.get("artifact_sha256") != freeze_artifact:
        raise ValueError("clock-offset series differs from the natural freeze")
    clock_window = _mapping(clock.get("window"), label="clock-offset series window")
    if clock_window.get("t0_ns") != t0_ns or clock_window.get("t1_ns") != t1_ns:
        raise ValueError("clock-offset series differs from the natural window")

    safety_source = source_payloads["natural_safety_flatten"]
    safety_capture = _absolute_no_symlink(
        str(safety_source.get("target_capture_path") or ""),
        label="post-window safety target capture",
        require_exists=True,
    )
    if safety_capture == target_capture:
        raise ValueError("post-window safety capture must be separate from the natural capture")
    if roots["natural_evidence"] not in safety_capture.parents:
        raise ValueError("post-window safety capture must be inside the natural evidence root")
    safety_capture_snapshot = _file_snapshot(
        safety_capture,
        label="post-window safety target capture",
    )
    safety_kwargs: dict[str, Any] = {
        "target_capture_path": safety_capture,
        "expected_account_id": demo_account_id,
        "expected_t1_ns": t1_ns,
    }
    if _loader_accepts_keyword(
        load_post_window_safety_manifest,
        "manifest_snapshot",
    ):
        safety_kwargs["manifest_snapshot"] = source_snapshots[
            "natural_safety_flatten"
        ]
    if _loader_accepts_keyword(
        load_post_window_safety_manifest,
        "capture_snapshot",
    ):
        safety_kwargs["capture_snapshot"] = safety_capture_snapshot
    safety = load_post_window_safety_manifest(
        input_paths["natural_safety_flatten"],
        **safety_kwargs,
    )
    _require_loaded_source(
        role="natural_safety_flatten",
        loaded=safety,
        source_payload=safety_source,
    )
    if (
        safety.get("freeze_id") != freeze_id
        or safety.get("t1_ns") != t1_ns
        or safety.get("expected_account_id") != demo_account_id
        or safety.get("target_capture_path") != str(safety_capture)
    ):
        raise ValueError("post-window safety manifest differs from the natural freeze/account/window")
    safety_capture_file = _snapshot_identity(safety_capture_snapshot)
    safety_capture_binding = _mapping(safety.get("target_capture"), label="post-window safety target capture binding")
    expected_safety_capture_fields = {
        "path": safety_capture_file["path"],
        "size": safety_capture_file["size_bytes"],
        "sha256": safety_capture_file["sha256"],
        "device": safety_capture_file["device"],
        "inode": safety_capture_file["inode"],
        "mtime_ns": safety_capture_file["mtime_ns"],
        "mode": safety_capture_file["mode"],
    }
    if any(safety_capture_binding.get(key) != value for key, value in expected_safety_capture_fields.items()):
        raise ValueError("post-window safety manifest names another target-capture file")

    if _loader_accepts_keyword(load_venue_accounting_receipt, "snapshot"):
        venue = load_venue_accounting_receipt(
            input_paths["venue_accounting"],
            snapshot=source_snapshots["venue_accounting"],
        )
    else:
        venue = load_venue_accounting_receipt(input_paths["venue_accounting"])
    _require_loaded_source(
        role="venue_accounting",
        loaded=venue,
        source_payload=source_payloads["venue_accounting"],
    )
    venue_account_root = _absolute_no_symlink(
        str(venue.get("account_root") or ""),
        label="venue-accounting demo account root",
        require_exists=True,
    )
    query_window = _mapping(venue.get("query_window_ms"), label="venue-accounting query window")
    query_start_ms = int(query_window.get("start") or 0)
    query_end_ms = int(query_window.get("end") or 0)
    if (
        venue.get("environment") != "demo"
        or venue.get("account_id") != demo_account_id
        or venue_account_root != roots["demo_account"]
        or venue.get("venue_accounting_gate_passed") is not True
        or venue.get("final_demo_flatness_gate_passed") is not True
    ):
        raise ValueError("venue accounting does not pass for the frozen demo account/root")
    if (
        query_start_ms > t0_ns // 1_000_000
        or query_end_ms < (t1_ns + 999_999) // 1_000_000
        or int(venue.get("observed_ts_ns") or 0) < t1_ns
    ):
        raise ValueError("venue accounting does not cover the complete natural window")

    return {
        "candidate_commit": candidate_commit,
        "origin_main_commit": origin_main_commit,
        "freeze_id": freeze_id,
        "freeze_artifact_sha256": freeze_artifact,
        "freeze_file_sha256": freeze_file["sha256"],
        "t0_ns": t0_ns,
        "t1_ns": t1_ns,
        "interval": "half_open_[t0,t1)",
    }


def _exclusive_write(path: Path, payload: Mapping[str, Any]) -> None:
    data = canonical_json(dict(payload)) + b"\n"
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        offset = 0
        view = memoryview(data)
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("stopped epoch seal write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)


def create_stopped_natural_epoch_seal(
    *,
    input_files: Mapping[str, str | Path],
    old_mutable_roots: Mapping[str, str | Path],
    output_path: str | Path,
    systemctl_bin: str = "systemctl",
    created_ts_ns: int | None = None,
) -> Path:
    """Hash the exact stopped namespace and publish a private seal."""

    inputs = _normalize_inputs(input_files)
    roots = _normalize_roots(old_mutable_roots)
    output = _absolute_no_symlink(output_path, label="stopped epoch seal output", require_exists=False)
    if output.exists():
        raise FileExistsError(f"stopped epoch seal output exists: {output}")
    if any(_paths_overlap(output, root) for root in roots.values()):
        raise ValueError("stopped epoch seal output must be outside every sealed root")
    if output.parent == output or not output.parent.exists():
        raise ValueError("stopped epoch seal output parent must already exist")
    before_units = _fleet_state(systemctl_bin=systemctl_bin)
    source_files: dict[str, dict[str, Any]] = {}
    compact_inputs: dict[str, dict[str, Any]] = {}
    source_payloads: dict[str, dict[str, Any]] = {}
    source_snapshots: dict[str, StableFileSnapshot] = {}
    for role, path in inputs.items():
        (
            source_files[role],
            compact_inputs[role],
            source_payloads[role],
            source_snapshots[role],
        ) = _source_file(role=role, path=path)
    source_inodes = [(int(value["device"]), int(value["inode"])) for value in source_files.values()]
    if len(set(source_inodes)) != len(source_inodes):
        raise ValueError("stopped epoch input roles must use distinct source files")
    identity = _semantic_epoch_identity(
        input_paths=inputs,
        source_files=source_files,
        source_payloads=source_payloads,
        source_snapshots=source_snapshots,
        roots=roots,
    )
    first_trees = {role: _tree_snapshot(root, role=role) for role, root in roots.items()}
    second_files: dict[str, dict[str, Any]] = {}
    for role, path in inputs.items():
        second_files[role], _compact, _payload, _snapshot = _source_file(
            role=role,
            path=path,
        )
    second_trees = {role: _tree_snapshot(root, role=role) for role, root in roots.items()}
    after_units = _fleet_state(systemctl_bin=systemctl_bin)
    if canonical_json(source_files) != canonical_json(second_files):
        raise RuntimeError("stopped epoch input files changed while the namespace was sealed")
    if canonical_json(first_trees) != canonical_json(second_trees):
        raise RuntimeError("old mutable namespace changed while it was sealed")
    required_files = sorted(
        str(roots[role] / entry["relative_path"])
        for role, tree in first_trees.items()
        for entry in cast(Sequence[Mapping[str, Any]], tree["entries"])
        if entry.get("kind") == "file"
    )
    if not required_files:
        raise ValueError("stopped epoch cannot seal an empty old mutable namespace")
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= int(identity["t1_ns"]):
        raise ValueError("stopped epoch seal must be created after the natural window")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "created_ts_ns": created,
        "identity": identity,
        "inputs": compact_inputs,
        "sealed_namespace": {
            "required_old_mutable_roots": [{"role": role, "path": str(roots[role])} for role in OLD_ROOT_ROLES],
            "required_old_mutable_files": required_files,
        },
        "source_files": source_files,
        "source_trees": first_trees,
        "tape_semantics": dict(TAPE_SEMANTICS),
        "service_state": {
            "registered_units": list(REGISTERED_UNITS),
            "before_hashing": before_units,
            "after_hashing": after_units,
            "all_inactive_before_hashing": True,
            "all_inactive_after_hashing": True,
        },
        "execution_authorization": EXECUTION_AUTHORIZATION,
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    _exclusive_write(output, payload)
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    load_stopped_natural_epoch_seal(output, require_currently_stopped=True, systemctl_bin=systemctl_bin)
    return output


def _validate_file_identity(value: object, *, label: str) -> dict[str, Any]:
    expected_fields = {
        "path",
        "size_bytes",
        "sha256",
        "device",
        "inode",
        "mtime_ns",
        "mode",
        "uid",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError(f"{label} identity is malformed")
    return dict(value)


def load_stopped_natural_epoch_seal(
    path: str | Path,
    *,
    require_currently_stopped: bool = False,
    systemctl_bin: str = "systemctl",
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Reopen every sealed source and optionally recheck live unit inactivity."""

    if snapshot is None:
        seal_path = _absolute_no_symlink(
            path,
            label="stopped natural epoch seal",
            require_exists=True,
        )
        snapshot = _file_snapshot(seal_path, label="stopped natural epoch seal")
    else:
        seal_path = snapshot.path
        if seal_path != Path(path).expanduser().absolute():
            raise ValueError("stopped natural epoch seal snapshot path differs")
    if (
        snapshot.mode != 0o600
        or snapshot.uid != os.geteuid()
        or snapshot.nlink != 1
    ):
        raise ValueError("stopped natural epoch seal must be owner-owned mode 0600")
    before_seal = _snapshot_identity(snapshot)
    payload = _strict_json_bytes(snapshot.data, label="stopped natural epoch seal")
    expected_fields = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "identity",
        "inputs",
        "sealed_namespace",
        "source_files",
        "source_trees",
        "tape_semantics",
        "service_state",
        "execution_authorization",
        "artifact_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("stopped natural epoch seal has unexpected or missing fields")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("validator") != VALIDATOR
        or int(payload.get("created_ts_ns") or 0) <= 0
        or payload.get("execution_authorization") != EXECUTION_AUTHORIZATION
        or payload.get("artifact_sha256") != _self_hash(payload)
    ):
        raise ValueError("stopped natural epoch seal identity or self-hash is invalid")
    identity = payload.get("identity")
    identity_fields = {
        "candidate_commit",
        "origin_main_commit",
        "freeze_id",
        "freeze_artifact_sha256",
        "freeze_file_sha256",
        "t0_ns",
        "t1_ns",
        "interval",
    }
    if not isinstance(identity, Mapping) or set(identity) != identity_fields:
        raise ValueError("stopped natural epoch identity is malformed")
    _full_commit(identity.get("candidate_commit"), label="candidate commit")
    _full_commit(identity.get("origin_main_commit"), label="origin/main commit")
    if (
        not str(identity.get("freeze_id") or "")
        or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("freeze_artifact_sha256") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("freeze_file_sha256") or ""))
        or int(identity.get("t0_ns") or 0) <= 0
        or int(identity.get("t1_ns") or 0) - int(identity.get("t0_ns") or 0) != 120 * 60 * 60 * 1_000_000_000
        or int(payload.get("created_ts_ns") or 0) <= int(identity.get("t1_ns") or 0)
        or identity.get("interval") != "half_open_[t0,t1)"
    ):
        raise ValueError("stopped natural epoch freeze/window identity is invalid")
    inputs = payload.get("inputs")
    source_files = payload.get("source_files")
    if (
        not isinstance(inputs, Mapping)
        or set(inputs) != set(REQUIRED_INPUT_FILE_ROLES)
        or not isinstance(source_files, Mapping)
        or set(source_files) != set(REQUIRED_INPUT_FILE_ROLES)
    ):
        raise ValueError("stopped natural epoch input/source roles differ")
    input_paths: dict[str, Path] = {}
    source_payloads: dict[str, dict[str, Any]] = {}
    source_snapshots: dict[str, StableFileSnapshot] = {}
    for role in REQUIRED_INPUT_FILE_ROLES:
        expected = _validate_file_identity(source_files[role], label=f"source {role}")
        source_snapshot = _file_snapshot(
            str(expected["path"]),
            label=f"source {role}",
        )
        actual = _snapshot_identity(source_snapshot)
        if actual != expected:
            raise ValueError(f"stopped natural epoch source {role!r} changed")
        if actual["mode"] != 0o600 or actual["uid"] != os.geteuid():
            raise ValueError(f"stopped epoch input {role} must be owner-owned mode 0600")
        compact = inputs[role]
        if not isinstance(compact, Mapping) or set(compact) != {
            "path",
            "sha256",
            "artifact_sha256",
        }:
            raise ValueError(f"stopped natural epoch compact input {role!r} is malformed")
        if compact.get("path") != expected["path"] or compact.get("sha256") != expected["sha256"]:
            raise ValueError(f"stopped natural epoch compact input {role!r} differs")
        input_paths[role] = Path(str(expected["path"]))
        source_payloads[role] = _strict_json_bytes(
            source_snapshot.data,
            label=f"stopped epoch input {role}",
        )
        source_snapshots[role] = source_snapshot
        source_artifact = _artifact_hash(source_payloads[role], label=f"stopped epoch input {role}")
        if compact.get("artifact_sha256") != source_artifact:
            raise ValueError(f"stopped natural epoch compact input {role!r} lacks artifact hash")
    source_inode_pairs = [
        (
            int(cast(Mapping[str, Any], source_files[role])["device"]),
            int(cast(Mapping[str, Any], source_files[role])["inode"]),
        )
        for role in REQUIRED_INPUT_FILE_ROLES
    ]
    if len(set(source_inode_pairs)) != len(source_inode_pairs):
        raise ValueError("stopped natural epoch source roles alias a file")
    freeze_compact = cast(Mapping[str, Any], inputs["freeze_manifest"])
    if freeze_compact.get("sha256") != identity.get("freeze_file_sha256") or freeze_compact.get(
        "artifact_sha256"
    ) != identity.get("freeze_artifact_sha256"):
        raise ValueError("stopped natural epoch freeze identity differs from its source")
    sealed = payload.get("sealed_namespace")
    trees = payload.get("source_trees")
    if not isinstance(sealed, Mapping) or set(sealed) != {
        "required_old_mutable_roots",
        "required_old_mutable_files",
    }:
        raise ValueError("stopped natural epoch namespace is malformed")
    roots = sealed.get("required_old_mutable_roots")
    files = sealed.get("required_old_mutable_files")
    if not isinstance(roots, list) or not isinstance(files, list):
        raise ValueError("stopped natural epoch namespace lists are malformed")
    expected_roots: dict[str, Path] = {}
    for index, role in enumerate(OLD_ROOT_ROLES):
        if index >= len(roots) or not isinstance(roots[index], Mapping):
            raise ValueError("stopped natural epoch lacks its exact old-root order")
        entry = cast(Mapping[str, Any], roots[index])
        if set(entry) != {"role", "path"} or entry.get("role") != role:
            raise ValueError("stopped natural epoch old-root order differs")
        expected_roots[role] = Path(str(entry.get("path") or ""))
    if len(roots) != len(OLD_ROOT_ROLES):
        raise ValueError("stopped natural epoch must bind exactly eleven roots")
    normalized_roots = _normalize_roots(expected_roots)
    rebuilt_identity = _semantic_epoch_identity(
        input_paths=input_paths,
        source_files={role: cast(Mapping[str, Any], source_files[role]) for role in REQUIRED_INPUT_FILE_ROLES},
        source_payloads=source_payloads,
        source_snapshots=source_snapshots,
        roots=normalized_roots,
    )
    if rebuilt_identity != dict(identity):
        raise ValueError("stopped natural epoch identity differs from its natural freeze")
    if not isinstance(trees, Mapping) or set(trees) != set(OLD_ROOT_ROLES):
        raise ValueError("stopped natural epoch source-tree roles differ")
    rebuilt_trees = {role: _tree_snapshot(normalized_roots[role], role=role) for role in OLD_ROOT_ROLES}
    if canonical_json(rebuilt_trees) != canonical_json(dict(trees)):
        raise ValueError("stopped natural epoch tree content or identity changed")
    rebuilt_files = sorted(
        str(normalized_roots[role] / entry["relative_path"])
        for role, tree in rebuilt_trees.items()
        for entry in cast(Sequence[Mapping[str, Any]], tree["entries"])
        if entry.get("kind") == "file"
    )
    if files != rebuilt_files:
        raise ValueError("stopped natural epoch old-file set differs from its trees")
    if not rebuilt_files:
        raise ValueError("stopped natural epoch old-file set is empty")
    if payload.get("tape_semantics") != dict(TAPE_SEMANTICS):
        raise ValueError("stopped natural epoch tape semantics differ")
    service = payload.get("service_state")
    if not isinstance(service, Mapping) or set(service) != {
        "registered_units",
        "before_hashing",
        "after_hashing",
        "all_inactive_before_hashing",
        "all_inactive_after_hashing",
    }:
        raise ValueError("stopped natural epoch service state is malformed")
    if (
        service.get("registered_units") != list(REGISTERED_UNITS)
        or service.get("all_inactive_before_hashing") is not True
        or service.get("all_inactive_after_hashing") is not True
    ):
        raise ValueError("stopped natural epoch does not bind the exact stopped fleet")
    for phase in ("before_hashing", "after_hashing"):
        states = service.get(phase)
        if not isinstance(states, list) or any(not isinstance(state, Mapping) for state in states):
            raise ValueError(f"stopped natural epoch {phase} unit set differs")
        state_maps = [cast(Mapping[str, Any], state) for state in states]
        if [state.get("unit") for state in state_maps] != list(REGISTERED_UNITS):
            raise ValueError(f"stopped natural epoch {phase} unit set differs")
        if any(
            set(state) != {"unit", "active_state", "sub_state", "main_pid", "inactive"}
            or state.get("active_state") != "inactive"
            or not str(state.get("sub_state") or "")
            or state.get("main_pid") != 0
            or state.get("inactive") is not True
            for state in state_maps
        ):
            raise ValueError(f"stopped natural epoch {phase} includes an active unit")
    if require_currently_stopped:
        _fleet_state(systemctl_bin=systemctl_bin)
    final_snapshot = _file_snapshot(seal_path, label="stopped natural epoch seal")
    if (
        _snapshot_identity(final_snapshot) != before_seal
        or final_snapshot.data != snapshot.data
    ):
        raise RuntimeError("stopped natural epoch seal changed during verification")
    return payload


def _role_paths(values: Sequence[str], *, label: str) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for value in values:
        role, separator, raw_path = value.partition("=")
        if not separator or not role or not raw_path or role in output:
            raise ValueError(f"{label} must use distinct ROLE=/absolute/path entries")
        output[role] = Path(raw_path)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify the stopped natural-epoch namespace seal")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--input", action="append", required=True)
    create.add_argument("--root", action="append", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--systemctl-bin", default="systemctl")
    verify = commands.add_parser("verify")
    verify.add_argument("--seal", type=Path, required=True)
    verify.add_argument("--require-currently-stopped", action="store_true")
    verify.add_argument("--systemctl-bin", default="systemctl")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            output = create_stopped_natural_epoch_seal(
                input_files=_role_paths(args.input, label="--input"),
                old_mutable_roots=_role_paths(args.root, label="--root"),
                output_path=args.output,
                systemctl_bin=args.systemctl_bin,
            )
            print(str(output))
            return 0
        receipt = load_stopped_natural_epoch_seal(
            args.seal,
            require_currently_stopped=bool(args.require_currently_stopped),
            systemctl_bin=args.systemctl_bin,
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "artifact_sha256": receipt["artifact_sha256"],
                    "candidate_commit": cast(Mapping[str, Any], receipt["identity"])["candidate_commit"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"stopped natural epoch failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXECUTION_AUTHORIZATION",
    "KIND",
    "OLD_ROOT_ROLES",
    "REGISTERED_UNITS",
    "REQUIRED_INPUT_FILE_ROLES",
    "VALIDATOR",
    "create_stopped_natural_epoch_seal",
    "load_stopped_natural_epoch_seal",
]
