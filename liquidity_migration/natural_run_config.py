"""Frozen runtime boundary for the 120-hour natural demo holdout.

The pre-window freeze proves which candidate, account epoch, and supporting
artifacts were selected.  This smaller runtime config binds that freeze to the
only files the LONG and CONTINUOUS producers may append during the natural
window.  Producers load it before constructing public-market resources and use
the half-open ``[T0, T1)`` boundary before appending a scheduling event.

This module grants no execution authority.  It contains no credentials and is
valid only for the explicit ``demo`` target-producer environment.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json
from .natural_cutover_freeze_manifest import (
    HOUR_NS,
    WINDOW_HOURS,
    WINDOW_NS,
    load_natural_cutover_freeze_manifest,
)
from .storage import exclusive_file_lock
from .strategy_event_clock import StrategyEvent, load_strategy_event_tape
from .strategy_event_outcome import load_strategy_event_decision_tape
from .strategy_target_replay import load_target_scheduling_capture


SCHEMA_VERSION = 1
KIND = "natural_strategy_run_config"
VALIDATOR = "natural_strategy_run_config_v1"
SLEEVES = ("long", "continuous")
EXECUTION_ENVIRONMENT = "demo"
DEFAULT_NATURAL_RUN_ENVIRONMENT = Path("/etc/liquidity-migration/natural-run.env")

_RELATIVE_DATA_ROOTS = {
    "long": Path("data/bybit-long-demo-event"),
    "continuous": Path("data/bybit-continuous-demo-event"),
}
_EVENT_TAPE_NAME = "strategy_event_tape.jsonl"
_OUTCOME_TAPE_NAME = "strategy_event_decision_tape.jsonl"
_RELATIVE_NATURAL_ROOT = Path("data/bybit-natural-account-cutover")
_TARGET_CAPTURE_NAME = "strategy_target_scheduling_capture.jsonl"
_CONFIG_NAME = "natural-run-config.json"
_EFFECTIVE_CONFIG_NAME = "natural-effective-runtime-config.json"
_EFFECTIVE_CONFIG_BUNDLE_NAME = "effective-runtime-config-bundle.json"


@dataclass(frozen=True, slots=True)
class NaturalSleeveRuntime:
    data_root: Path
    event_tape_path: Path
    outcome_tape_path: Path


@dataclass(frozen=True, slots=True)
class NaturalRunConfig:
    path: Path
    freeze_manifest_path: Path
    freeze_manifest_file_sha256: str
    freeze_artifact_sha256: str
    freeze_id: str
    repository_root: Path
    candidate_universe_path: Path
    candidate_universe_file_sha256: str
    t0_ns: int
    t1_ns: int
    target_capture_path: Path
    sleeves: Mapping[str, NaturalSleeveRuntime]
    artifact_sha256: str

    def sleeve(self, name: str) -> NaturalSleeveRuntime:
        try:
            return self.sleeves[name]
        except KeyError as exc:
            raise ValueError(f"unknown natural runtime sleeve: {name!r}") from exc


def canonical_natural_run_paths(repository_root: str | Path) -> dict[str, Path]:
    """Return the one reset-covered file layout for the natural demo epoch."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    values: dict[str, Path] = {
        "config": root / _RELATIVE_NATURAL_ROOT / _CONFIG_NAME,
        "target_capture": root / _RELATIVE_NATURAL_ROOT / _TARGET_CAPTURE_NAME,
        "effective_config_bundle": (root / _RELATIVE_NATURAL_ROOT / _EFFECTIVE_CONFIG_BUNDLE_NAME),
    }
    for sleeve, relative in _RELATIVE_DATA_ROOTS.items():
        data_root = root / relative
        values[f"{sleeve}_data_root"] = data_root
        values[f"{sleeve}_event_tape"] = data_root / _EVENT_TAPE_NAME
        values[f"{sleeve}_outcome_tape"] = data_root / _OUTCOME_TAPE_NAME
        values[f"{sleeve}_effective_config"] = data_root / _EFFECTIVE_CONFIG_NAME
    return values


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lower_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _freeze_id(value: object) -> str:
    identifier = str(value or "")
    prefix = "natural-cutover-"
    digest = identifier.removeprefix(prefix)
    if (
        not identifier.startswith(prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("natural runtime freeze_id is invalid")
    return identifier


def _private_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return read_stable_file(
        candidate,
        label=label,
        require_mode=0o600,
        require_owner=True,
    )


def _read_private_file(
    path: str | Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[Path, bytes]:
    if snapshot is None:
        snapshot = _private_snapshot(path, label=label)
    else:
        expected = Path(path).expanduser().absolute()
        if snapshot.path != expected:
            raise ValueError(f"{label} snapshot path differs")
        if (
            snapshot.mode != 0o600
            or snapshot.uid != os.geteuid()
            or snapshot.nlink != 1
        ):
            raise ValueError(f"{label} must be owner-owned mode 0600 and not hard-linked")
    return snapshot.path, snapshot.data


def _load_freeze_snapshot(
    path: Path,
    snapshot: StableFileSnapshot,
) -> dict[str, Any]:
    try:
        accepts_snapshot = "snapshot" in inspect.signature(
            load_natural_cutover_freeze_manifest
        ).parameters
    except (TypeError, ValueError):
        accepts_snapshot = False
    if accepts_snapshot:
        return load_natural_cutover_freeze_manifest(path, snapshot=snapshot)
    # Compatibility for narrow test doubles. The production loader consumes
    # the descriptor-bound snapshot above.
    return load_natural_cutover_freeze_manifest(path)


def _strict_json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"{label} contains non-finite token {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _artifact_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _assert_empty_runtime_outputs(paths: Mapping[str, Path]) -> None:
    for label, path in paths.items():
        if label == "config" or label.endswith("_data_root") or not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"natural runtime output {label} must be absent or an empty regular file")
        metadata = path.stat()
        if metadata.st_size != 0:
            raise ValueError(f"natural runtime output {label} contains a pre-reset prefix: {path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid():
            raise ValueError(f"empty natural runtime output {label} must be owner-only mode 0600")


def build_natural_run_config(
    *,
    freeze_manifest_path: str | Path,
    created_ts_ns: int | None = None,
) -> dict[str, Any]:
    """Bind a validated pre-window freeze to the reset-covered runtime tapes."""

    freeze_snapshot = _private_snapshot(
        freeze_manifest_path,
        label="natural cutover freeze manifest",
    )
    freeze_path, freeze_bytes = _read_private_file(
        freeze_manifest_path,
        label="natural cutover freeze manifest",
        snapshot=freeze_snapshot,
    )
    freeze = _load_freeze_snapshot(freeze_path, freeze_snapshot)
    repository = cast(Mapping[str, Any], freeze.get("repository"))
    window = cast(Mapping[str, Any], freeze.get("window"))
    population = cast(Mapping[str, Any], freeze.get("population"))
    candidate = cast(Mapping[str, Any], population.get("candidate_universe"))
    repository_root = Path(str(repository.get("root") or "")).resolve(strict=True)
    paths = canonical_natural_run_paths(repository_root)

    candidate_snapshot = _private_snapshot(
        str(candidate.get("path") or ""),
        label="frozen candidate universe",
    )
    candidate_path = candidate_snapshot.path
    candidate_hash = candidate_snapshot.sha256
    if candidate_hash != _lower_sha256(candidate.get("file_sha256"), label="frozen candidate-universe file hash"):
        raise ValueError("candidate-universe bytes do not match the pre-window freeze")

    t0_ns = int(window.get("t0_ns") or 0)
    t1_ns = int(window.get("t1_ns") or 0)
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if (
        created <= 0
        or created >= t0_ns
        or t0_ns % HOUR_NS
        or t1_ns - t0_ns != WINDOW_NS
        or window.get("duration_hours") != WINDOW_HOURS
        or window.get("interval") != "half_open_[t0,t1)"
    ):
        raise ValueError("natural run config requires a future exact 120-hour [T0,T1) freeze")

    _assert_empty_runtime_outputs(paths)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "created_ts_ns": created,
        "execution_environment": EXECUTION_ENVIRONMENT,
        "freeze": {
            "path": str(freeze_path),
            "file_sha256": _sha256_bytes(freeze_bytes),
            "artifact_sha256": _lower_sha256(freeze.get("artifact_sha256"), label="freeze artifact hash"),
            "freeze_id": _freeze_id(freeze.get("freeze_id")),
        },
        "repository_root": str(repository_root),
        "window": {
            "t0_ns": t0_ns,
            "t1_ns": t1_ns,
            "duration_hours": WINDOW_HOURS,
            "interval": "half_open_[t0,t1)",
        },
        "candidate_universe": {
            "path": str(candidate_path),
            "file_sha256": candidate_hash,
            "artifact_sha256": _lower_sha256(
                candidate.get("artifact_sha256"),
                label="candidate-universe artifact hash",
            ),
        },
        "target_capture_path": str(paths["target_capture"]),
        "sleeves": {
            sleeve: {
                "data_root": str(paths[f"{sleeve}_data_root"]),
                "event_tape_path": str(paths[f"{sleeve}_event_tape"]),
                "outcome_tape_path": str(paths[f"{sleeve}_outcome_tape"]),
            }
            for sleeve in SLEEVES
        },
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    if _private_snapshot(
        freeze_path,
        label="natural cutover freeze manifest",
    ) != freeze_snapshot:
        raise RuntimeError("natural cutover freeze changed while the runtime config was built")
    if _private_snapshot(
        candidate_path,
        label="frozen candidate universe",
    ) != candidate_snapshot:
        raise RuntimeError("candidate universe changed while the runtime config was built")
    return payload


def write_natural_run_config(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Create the canonical owner-only config once; overwrite is refused."""

    value = dict(payload)
    repository_root = Path(str(value.get("repository_root") or "")).resolve(strict=True)
    expected = canonical_natural_run_paths(repository_root)["config"]
    output = Path(path).expanduser()
    if not output.is_absolute() or output.resolve(strict=False) != expected:
        raise ValueError(f"natural run config output must be the canonical path {expected}")
    if value.get("artifact_sha256") != _artifact_hash(value):
        raise ValueError("natural run config self-hash is invalid")
    expected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(expected.parent, 0o700)
    data = canonical_json(value) + b"\n"
    descriptor = os.open(str(expected), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("natural run config write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        expected.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory_fd = os.open(str(expected.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return expected


def load_natural_run_config(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> NaturalRunConfig:
    """Source-reopen and validate one frozen natural runtime config."""

    config_path, data = _read_private_file(
        path,
        label="natural run config",
        snapshot=snapshot,
    )
    payload = _strict_json(data, label="natural run config")
    expected_top = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "execution_environment",
        "freeze",
        "repository_root",
        "window",
        "candidate_universe",
        "target_capture_path",
        "sleeves",
        "artifact_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("natural run config has unexpected or missing fields")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("validator") != VALIDATOR
        or payload.get("execution_environment") != EXECUTION_ENVIRONMENT
        or payload.get("artifact_sha256") != _artifact_hash(payload)
    ):
        raise ValueError("natural run config identity or self-hash is invalid")

    freeze_ref = cast(Mapping[str, Any], payload.get("freeze"))
    if set(freeze_ref) != {"path", "file_sha256", "artifact_sha256", "freeze_id"}:
        raise ValueError("natural run config freeze binding is invalid")
    freeze_snapshot = _private_snapshot(
        str(freeze_ref.get("path") or ""),
        label="natural cutover freeze manifest",
    )
    freeze_path, freeze_bytes = _read_private_file(
        str(freeze_ref.get("path") or ""),
        label="natural cutover freeze manifest",
        snapshot=freeze_snapshot,
    )
    if _sha256_bytes(freeze_bytes) != _lower_sha256(freeze_ref.get("file_sha256"), label="freeze manifest file hash"):
        raise ValueError("natural cutover freeze bytes changed after runtime binding")
    freeze = _load_freeze_snapshot(freeze_path, freeze_snapshot)
    freeze_id = _freeze_id(freeze_ref.get("freeze_id"))
    if freeze.get("freeze_id") != freeze_id or freeze.get("artifact_sha256") != _lower_sha256(
        freeze_ref.get("artifact_sha256"), label="freeze artifact hash"
    ):
        raise ValueError("natural run config names another pre-window freeze")

    repository_root = Path(str(payload.get("repository_root") or "")).resolve(strict=True)
    freeze_repository = cast(Mapping[str, Any], freeze.get("repository"))
    if repository_root != Path(str(freeze_repository.get("root") or "")).resolve(strict=True):
        raise ValueError("natural run config repository does not match its freeze")
    paths = canonical_natural_run_paths(repository_root)
    if config_path != paths["config"]:
        raise ValueError("natural run config is not at the reset-covered canonical path")

    window = cast(Mapping[str, Any], payload.get("window"))
    freeze_window = cast(Mapping[str, Any], freeze.get("window"))
    if dict(window) != dict(freeze_window):
        raise ValueError("natural run config window does not exactly match its freeze")
    t0_ns = int(window.get("t0_ns") or 0)
    t1_ns = int(window.get("t1_ns") or 0)
    created = int(payload.get("created_ts_ns") or 0)
    if created <= 0 or created >= t0_ns or t0_ns % HOUR_NS or t1_ns - t0_ns != WINDOW_NS:
        raise ValueError("natural run config does not preserve the exact half-open 120-hour window")

    candidate_ref = cast(Mapping[str, Any], payload.get("candidate_universe"))
    frozen_candidate = cast(
        Mapping[str, Any], cast(Mapping[str, Any], freeze.get("population")).get("candidate_universe")
    )
    if dict(candidate_ref) != dict(frozen_candidate):
        raise ValueError("natural run config candidate universe does not exactly match its freeze")
    candidate_snapshot = _private_snapshot(
        str(candidate_ref.get("path") or ""),
        label="frozen candidate universe",
    )
    candidate_path = candidate_snapshot.path
    candidate_hash = candidate_snapshot.sha256
    if candidate_hash != _lower_sha256(candidate_ref.get("file_sha256"), label="candidate-universe file hash"):
        raise ValueError("candidate-universe bytes changed after runtime binding")

    if Path(str(payload.get("target_capture_path") or "")).resolve(strict=False) != paths["target_capture"]:
        raise ValueError("natural target capture is not the reset-covered canonical path")
    raw_sleeves = payload.get("sleeves")
    if not isinstance(raw_sleeves, Mapping) or set(raw_sleeves) != set(SLEEVES):
        raise ValueError("natural run config must bind exactly LONG and CONTINUOUS")
    sleeves: dict[str, NaturalSleeveRuntime] = {}
    for sleeve in SLEEVES:
        raw = raw_sleeves.get(sleeve)
        if not isinstance(raw, Mapping) or set(raw) != {
            "data_root",
            "event_tape_path",
            "outcome_tape_path",
        }:
            raise ValueError(f"natural run config {sleeve} paths are invalid")
        runtime = NaturalSleeveRuntime(
            data_root=Path(str(raw.get("data_root") or "")).resolve(strict=False),
            event_tape_path=Path(str(raw.get("event_tape_path") or "")).resolve(strict=False),
            outcome_tape_path=Path(str(raw.get("outcome_tape_path") or "")).resolve(strict=False),
        )
        expected_runtime = NaturalSleeveRuntime(
            data_root=paths[f"{sleeve}_data_root"],
            event_tape_path=paths[f"{sleeve}_event_tape"],
            outcome_tape_path=paths[f"{sleeve}_outcome_tape"],
        )
        if runtime != expected_runtime:
            raise ValueError(f"natural run config {sleeve} paths are not canonical")
        sleeves[sleeve] = runtime

    after_path, after_data = _read_private_file(config_path, label="natural run config")
    if after_path != config_path or after_data != data:
        raise RuntimeError("natural run config changed while it was validated")
    if _private_snapshot(
        candidate_path,
        label="frozen candidate universe",
    ) != candidate_snapshot:
        raise RuntimeError("candidate universe changed while the runtime config was validated")
    return NaturalRunConfig(
        path=config_path,
        freeze_manifest_path=freeze_path,
        freeze_manifest_file_sha256=_sha256_bytes(freeze_bytes),
        freeze_artifact_sha256=str(freeze["artifact_sha256"]),
        freeze_id=freeze_id,
        repository_root=repository_root,
        candidate_universe_path=candidate_path,
        candidate_universe_file_sha256=candidate_hash,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        target_capture_path=paths["target_capture"],
        sleeves=sleeves,
        artifact_sha256=str(payload["artifact_sha256"]),
    )


def _natural_environment_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("natural run environment file must be an absolute path")
    if candidate.name != DEFAULT_NATURAL_RUN_ENVIRONMENT.name:
        raise ValueError(f"natural run environment file must be named {DEFAULT_NATURAL_RUN_ENVIRONMENT.name}")
    return Path(os.path.abspath(candidate))


def _natural_environment_bytes(config_path: Path) -> bytes:
    value = str(config_path)
    if not config_path.is_absolute():
        raise ValueError("NATURAL_RUN_CONFIG must be an absolute path")
    if any(character in value for character in ("'", '"', "\\")) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("NATURAL_RUN_CONFIG path cannot contain quotes, backslashes, or control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("NATURAL_RUN_CONFIG path must be valid UTF-8") from exc
    return b"NATURAL_EVIDENCE_REQUIRED=1\n" + b'NATURAL_RUN_CONFIG="' + encoded + b'"\n'


def verify_natural_run_environment(
    *,
    config_path: str | Path,
    environment_path: str | Path = DEFAULT_NATURAL_RUN_ENVIRONMENT,
) -> tuple[Path, NaturalRunConfig]:
    """Source-reopen and exactly verify the natural producer environment."""

    config = load_natural_run_config(config_path)
    expected = _natural_environment_bytes(config.path)
    requested = _natural_environment_path(environment_path)
    installed, data = _read_private_file(
        requested,
        label="natural run environment file",
    )
    if data != expected:
        raise ValueError("natural run environment bytes do not exactly bind the expected config")
    return installed, config


def materialize_natural_run_environment(
    *,
    config_path: str | Path,
    output_path: str | Path = DEFAULT_NATURAL_RUN_ENVIRONMENT,
) -> tuple[Path, NaturalRunConfig]:
    """Atomically replace the exact owner-only natural producer environment."""

    config = load_natural_run_config(config_path)
    data = _natural_environment_bytes(config.path)
    output = _natural_environment_path(output_path)
    parent = output.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise ValueError(f"natural run environment directory is unavailable: {parent}") from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("natural run environment parent must be a non-symlink directory")
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o022:
        raise ValueError("natural run environment parent must be owner-controlled")
    try:
        existing = output.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f"natural run environment file is unavailable: {output}") from exc
    else:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise ValueError("existing natural run environment must be a non-symlink regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("natural run environment write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, output)
        directory_descriptor = os.open(
            str(parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    installed, reopened = verify_natural_run_environment(
        config_path=config.path,
        environment_path=output,
    )
    return installed, reopened


def _validate_natural_event(
    event: StrategyEvent,
    *,
    config: NaturalRunConfig,
    sleeve: str,
    expected_sequence: int,
) -> None:
    if not (config.t0_ns <= event.event_ts_ns < config.t1_ns):
        raise ValueError("natural strategy event lies outside the frozen [T0,T1) window")
    if event.source != f"{sleeve}:demo" or event.source_sequence != expected_sequence:
        raise ValueError("natural strategy event source/sequence does not restart contiguously at 1")
    required_payload = {
        "execution_environment": EXECUTION_ENVIRONMENT,
        "natural_evidence_required": True,
        "natural_freeze_id": config.freeze_id,
        "natural_t0_ns": config.t0_ns,
        "natural_t1_ns": config.t1_ns,
    }
    for key, expected in required_payload.items():
        if event.payload.get(key) != expected:
            raise ValueError(f"natural strategy event has a mismatched {key}")


def validate_natural_runtime_binding(
    config: NaturalRunConfig,
    *,
    sleeve: str,
    execution_environment: str,
    data_root: str | Path,
    candidate_universe_path: str | Path | None = None,
    target_capture_path: str | Path | None = None,
) -> NaturalSleeveRuntime:
    """Validate one daemon's exact paths and any already-durable restart prefix."""

    if sleeve not in SLEEVES:
        raise ValueError(f"unknown natural runtime sleeve: {sleeve!r}")
    if execution_environment != EXECUTION_ENVIRONMENT:
        raise ValueError("natural runtime is demo-only")
    runtime = config.sleeve(sleeve)
    if Path(data_root).expanduser().resolve(strict=False) != runtime.data_root:
        raise ValueError(f"{sleeve} data root does not match the frozen natural run config")
    if candidate_universe_path is not None and (
        Path(candidate_universe_path).expanduser().resolve(strict=False) != config.candidate_universe_path
    ):
        raise ValueError("candidate-universe path does not match the frozen natural run config")
    if target_capture_path is not None and (
        Path(target_capture_path).expanduser().resolve(strict=False) != config.target_capture_path
    ):
        raise ValueError("target-capture path does not match the frozen natural run config")

    events, _event_hash = load_strategy_event_tape(runtime.event_tape_path)
    outcomes, _outcome_hash = load_strategy_event_decision_tape(runtime.outcome_tape_path)
    for index, event in enumerate(events, start=1):
        _validate_natural_event(
            event,
            config=config,
            sleeve=sleeve,
            expected_sequence=index,
        )
    if tuple(outcome.event_id for outcome in outcomes) != tuple(event.event_id for event in events):
        raise ValueError(f"{sleeve} natural event/outcome prefix is incomplete; restart is refused")

    lock_path = config.target_capture_path.parent / ".locks" / (f"{config.target_capture_path.name}.lock")
    with exclusive_file_lock(lock_path, stale_seconds=600, poll_seconds=0.01):
        captures, _capture_hash = load_target_scheduling_capture(config.target_capture_path)
    own_captures = []
    capture_sequences = {name: 0 for name in SLEEVES}
    for capture in captures:
        capture_sequences[capture.sleeve] += 1
        _validate_natural_event(
            capture.source_event,
            config=config,
            sleeve=capture.sleeve,
            expected_sequence=capture_sequences[capture.sleeve],
        )
        if capture.source_environment != EXECUTION_ENVIRONMENT:
            raise ValueError("natural target capture contains a non-demo event")
        if capture.sleeve == sleeve:
            own_captures.append(capture)
    if tuple(capture.source_event.event_id for capture in own_captures) != tuple(event.event_id for event in events):
        raise ValueError(f"{sleeve} natural event/capture prefix is incomplete; restart is refused")
    decisions = {outcome.event_id: outcome.decision_keys for outcome in outcomes}
    for capture in own_captures:
        if decisions.get(capture.source_event.event_id) != capture.decision_keys:
            raise ValueError("natural capture and outcome decision keys disagree")
    return runtime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="create the post-reset frozen runtime config")
    build.add_argument("--freeze-manifest", required=True)
    build.add_argument("--output", required=True)
    validate = subparsers.add_parser("validate", help="reopen a config and validate one sleeve")
    validate.add_argument("--config", required=True)
    validate.add_argument("--sleeve", choices=SLEEVES, required=True)
    validate.add_argument("--execution-environment", choices=(EXECUTION_ENVIRONMENT,), required=True)
    validate.add_argument("--data-root", required=True)
    materialize_env = subparsers.add_parser(
        "materialize-env",
        help="atomically install the exact owner-only natural producer environment",
    )
    materialize_env.add_argument("--config", required=True)
    materialize_env.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_NATURAL_RUN_ENVIRONMENT,
    )
    verify_env = subparsers.add_parser(
        "verify-env",
        help="source-reopen and verify the installed natural producer environment",
    )
    verify_env.add_argument("--config", required=True)
    verify_env.add_argument(
        "--environment-file",
        type=Path,
        default=DEFAULT_NATURAL_RUN_ENVIRONMENT,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            payload = build_natural_run_config(freeze_manifest_path=args.freeze_manifest)
            output = write_natural_run_config(args.output, payload)
            print(
                json.dumps(
                    {
                        "artifact_sha256": payload["artifact_sha256"],
                        "freeze_id": cast(Mapping[str, Any], payload["freeze"])["freeze_id"],
                        "output": str(output),
                        "t0_ns": cast(Mapping[str, Any], payload["window"])["t0_ns"],
                        "t1_ns": cast(Mapping[str, Any], payload["window"])["t1_ns"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "validate":
            config = load_natural_run_config(args.config)
            validate_natural_runtime_binding(
                config,
                sleeve=args.sleeve,
                execution_environment=args.execution_environment,
                data_root=args.data_root,
            )
            result = {
                "freeze_id": config.freeze_id,
                "sleeve": args.sleeve,
                "status": "valid",
                "t0_ns": config.t0_ns,
                "t1_ns": config.t1_ns,
            }
        elif args.command == "materialize-env":
            environment, config = materialize_natural_run_environment(
                config_path=args.config,
                output_path=args.output,
            )
            result = {
                "config": str(config.path),
                "environment_file": str(environment),
                "environment_sha256": _sha256_bytes(_natural_environment_bytes(config.path)),
                "freeze_id": config.freeze_id,
                "status": "materialized",
            }
        else:
            environment, config = verify_natural_run_environment(
                config_path=args.config,
                environment_path=args.environment_file,
            )
            result = {
                "config": str(config.path),
                "environment_file": str(environment),
                "environment_sha256": _sha256_bytes(_natural_environment_bytes(config.path)),
                "freeze_id": config.freeze_id,
                "status": "verified",
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"natural run config failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
