"""Source-bound effective configuration receipts for the natural holdout.

The freeze binds files and the natural run config binds the only appendable
paths.  Neither proves which dataclass values the CLI actually constructed.
Each producer therefore records its fully resolved in-memory configuration
before constructing public-market resources.  A restart may reuse the receipt
only when the newly resolved values are exactly equal.

The post-window bundle reopens both receipts and gives replay, parity, and
authorization one immutable configuration identity.  These artifacts contain
no credentials and grant no execution authority.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import math
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json
from .natural_cutover_freeze_manifest import load_natural_cutover_freeze_manifest
from .natural_run_config import (
    NaturalRunConfig,
    canonical_natural_run_paths,
    load_natural_run_config,
)


RECEIPT_SCHEMA_VERSION = 1
RECEIPT_KIND = "natural_effective_runtime_config"
RECEIPT_VALIDATOR = "natural_effective_runtime_config_v1"
BUNDLE_SCHEMA_VERSION = 2
BUNDLE_KIND = "natural_effective_runtime_config_bundle"
BUNDLE_VALIDATOR = "natural_effective_runtime_config_bundle_v2"
SLEEVES = ("continuous", "long")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lower_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _full_commit(value: object, *, label: str) -> str:
    commit = str(value or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full lowercase Git commit")
    return commit


def _json_value(value: Any, *, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite float")
        return value
    if isinstance(value, Path):
        return str(value.expanduser().resolve(strict=False))
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-string mapping key")
            output[key] = _json_value(item, label=f"{label}.{key}")
        return output
    if isinstance(value, (list, tuple)):
        return [_json_value(item, label=f"{label}[{index}]") for index, item in enumerate(value)]
    raise ValueError(f"{label} contains unsupported value type {type(value).__name__}")


def _dataclass_binding(value: object, *, label: str) -> dict[str, Any]:
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError(f"{label} must be a dataclass instance")
    payload = _json_value(asdict(value), label=label)
    if not isinstance(payload, dict):  # pragma: no cover - asdict is always a mapping
        raise TypeError(f"{label} did not serialize as a mapping")
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "values": payload,
    }


def _artifact_hash(payload: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json({**dict(payload), "artifact_sha256": ""}))


def _read_private(
    path: str | Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[Path, bytes]:
    if snapshot is None:
        snapshot = _private_snapshot(path, label=label)
    else:
        resolved = Path(path).expanduser().absolute()
        if snapshot.path != resolved:
            raise ValueError(f"{label} snapshot path differs")
        if snapshot.mode != 0o600 or snapshot.uid != os.geteuid():
            raise ValueError(f"{label} must be owner-owned with exact mode 0600")
    return snapshot.path, snapshot.data


def _private_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    return read_stable_file(
        path,
        label=label,
        require_mode=0o600,
        require_owner=True,
        require_single_link=False,
    )


def _accepts_snapshot(loader: object) -> bool:
    try:
        return "snapshot" in inspect.signature(loader).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _load_natural_run_snapshot(
    path: str | Path,
    snapshot: StableFileSnapshot,
) -> NaturalRunConfig:
    if _accepts_snapshot(load_natural_run_config):
        return load_natural_run_config(path, snapshot=snapshot)
    return load_natural_run_config(path)


def _load_freeze_snapshot(
    path: str | Path,
    snapshot: StableFileSnapshot,
) -> dict[str, Any]:
    if _accepts_snapshot(load_natural_cutover_freeze_manifest):
        return load_natural_cutover_freeze_manifest(path, snapshot=snapshot)
    return load_natural_cutover_freeze_manifest(path)


def _strict_json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            data,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"{label} contains non-finite token {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def canonical_receipt_path(config: NaturalRunConfig, sleeve: str) -> Path:
    if sleeve not in SLEEVES:
        raise ValueError(f"unknown natural sleeve: {sleeve!r}")
    return canonical_natural_run_paths(config.repository_root)[f"{sleeve}_effective_config"]


def canonical_bundle_path(config: NaturalRunConfig) -> Path:
    return canonical_natural_run_paths(config.repository_root)["effective_config_bundle"]


def _source_bindings(
    config: NaturalRunConfig,
    *,
    run_snapshot: StableFileSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if run_snapshot is None:
        run_snapshot = _private_snapshot(config.path, label="natural run config")
    loaded = _load_natural_run_snapshot(config.path, run_snapshot)
    if loaded != config:
        raise ValueError("natural run config changed before effective configuration capture")
    run_path, run_bytes = _read_private(
        config.path,
        label="natural run config",
        snapshot=run_snapshot,
    )
    freeze_snapshot = _private_snapshot(
        config.freeze_manifest_path,
        label="natural cutover freeze manifest",
    )
    freeze_path, freeze_bytes = _read_private(
        config.freeze_manifest_path,
        label="natural cutover freeze manifest",
        snapshot=freeze_snapshot,
    )
    freeze = _load_freeze_snapshot(
        freeze_path,
        freeze_snapshot,
    )
    repository = cast(Mapping[str, Any], freeze.get("repository"))
    if (
        _sha256_bytes(freeze_bytes) != config.freeze_manifest_file_sha256
        or freeze.get("artifact_sha256") != config.freeze_artifact_sha256
        or freeze.get("freeze_id") != config.freeze_id
    ):
        raise ValueError("natural effective configuration names a different freeze")
    repository_binding = {
        "root": str(config.repository_root),
        "candidate_commit": _full_commit(
            repository.get("candidate_commit"), label="freeze candidate commit"
        ),
        "origin_main_commit": _full_commit(
            repository.get("origin_main_commit"), label="freeze origin/main commit"
        ),
    }
    freeze_binding = {
        "path": str(freeze_path),
        "file_sha256": _sha256_bytes(freeze_bytes),
        "artifact_sha256": config.freeze_artifact_sha256,
        "freeze_id": config.freeze_id,
    }
    run_binding = {
        "path": str(run_path),
        "file_sha256": _sha256_bytes(run_bytes),
        "artifact_sha256": config.artifact_sha256,
    }
    return repository_binding, freeze_binding, run_binding, freeze


def build_effective_runtime_config(
    *,
    natural_run_config: NaturalRunConfig,
    sleeve: str,
    research_config: object,
    sleeve_config: object,
    strategy_config: object | None,
    scheduling: Mapping[str, Any],
    created_ts_ns: int | None = None,
) -> dict[str, Any]:
    """Build the exact config identity used by one natural producer process."""

    if sleeve not in SLEEVES:
        raise ValueError(f"unknown natural sleeve: {sleeve!r}")
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= 0 or created >= natural_run_config.t1_ns:
        raise ValueError("effective runtime config must be captured before natural T1")
    repository, freeze, run, freeze_payload = _source_bindings(natural_run_config)
    runtime = natural_run_config.sleeve(sleeve)
    normalized_scheduling = _json_value(dict(scheduling), label="scheduling")
    if not isinstance(normalized_scheduling, dict) or not normalized_scheduling:
        raise ValueError("effective runtime config requires non-empty scheduling settings")
    research_binding = _dataclass_binding(research_config, label="research config")
    sleeve_binding = _dataclass_binding(sleeve_config, label=f"{sleeve} config")
    configs: dict[str, Any] = {
        "research": research_binding,
        "sleeve": sleeve_binding,
        "strategy": (
            _dataclass_binding(strategy_config, label=f"{sleeve} strategy config")
            if strategy_config is not None
            else None
        ),
    }
    sleeve_values = cast(Mapping[str, Any], sleeve_binding["values"])
    if sleeve_values.get("execution_environment") != "demo":
        raise ValueError("natural effective runtime configuration is demo-only")
    if Path(str(sleeve_values.get("candidate_universe_file") or "")).resolve(strict=False) != (
        natural_run_config.candidate_universe_path
    ):
        raise ValueError("effective sleeve config uses a different candidate universe")
    frozen_runtime = freeze_payload.get("runtime")
    frozen_roots = frozen_runtime.get("roots") if isinstance(frozen_runtime, Mapping) else None
    frozen_demo = frozen_roots.get("demo") if isinstance(frozen_roots, Mapping) else None
    if not isinstance(frozen_demo, Mapping):
        raise ValueError("natural freeze lacks exact demo route roots")
    route_fields = {
        "account_execution_root": "account",
        "account_intent_inbox_root": "inbox",
    }
    for config_field, freeze_field in route_fields.items():
        actual = Path(str(sleeve_values.get(config_field) or "")).expanduser()
        expected = Path(str(frozen_demo.get(freeze_field) or "")).expanduser()
        if (
            not actual.is_absolute()
            or not expected.is_absolute()
            or actual.resolve(strict=False) != expected.resolve(strict=False)
        ):
            raise ValueError(
                f"effective sleeve config {config_field} differs from the frozen demo route"
            )
    payload: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "validator": RECEIPT_VALIDATOR,
        "created_ts_ns": created,
        "sleeve": sleeve,
        "execution_environment": "demo",
        "repository": repository,
        "freeze": freeze,
        "natural_run_config": run,
        "window": {
            "t0_ns": natural_run_config.t0_ns,
            "t1_ns": natural_run_config.t1_ns,
            "interval": "half_open_[t0,t1)",
        },
        "runtime_paths": {
            "data_root": str(runtime.data_root),
            "event_tape_path": str(runtime.event_tape_path),
            "outcome_tape_path": str(runtime.outcome_tape_path),
            "target_capture_path": str(natural_run_config.target_capture_path),
            "candidate_universe_path": str(natural_run_config.candidate_universe_path),
        },
        "configs": configs,
        "scheduling": normalized_scheduling,
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def _exclusive_write(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    data = canonical_json(payload) + b"\n"
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("effective configuration write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def _validate_receipt_shape(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "sleeve",
        "execution_environment",
        "repository",
        "freeze",
        "natural_run_config",
        "window",
        "runtime_paths",
        "configs",
        "scheduling",
        "artifact_sha256",
    }
    if set(payload) != expected:
        raise ValueError("effective runtime config has unexpected or missing fields")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != RECEIPT_KIND
        or payload.get("validator") != RECEIPT_VALIDATOR
        or payload.get("execution_environment") != "demo"
        or payload.get("sleeve") not in SLEEVES
        or payload.get("artifact_sha256") != _artifact_hash(payload)
    ):
        raise ValueError("effective runtime config identity or self-hash is invalid")


def load_effective_runtime_config(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Reopen a per-sleeve receipt and every frozen source it names."""

    receipt_path, data = _read_private(
        path,
        label="natural effective runtime config",
        snapshot=snapshot,
    )
    payload = _strict_json(data, label="natural effective runtime config")
    _validate_receipt_shape(payload)
    run_ref = cast(Mapping[str, Any], payload.get("natural_run_config"))
    if set(run_ref) != {"path", "file_sha256", "artifact_sha256"}:
        raise ValueError("effective runtime config has an invalid run-config binding")
    run_snapshot = _private_snapshot(
        str(run_ref.get("path") or ""),
        label="natural run config",
    )
    run_path, run_bytes = _read_private(
        str(run_ref.get("path") or ""),
        label="natural run config",
        snapshot=run_snapshot,
    )
    if _sha256_bytes(run_bytes) != _lower_sha256(
        run_ref.get("file_sha256"), label="natural run-config file hash"
    ):
        raise ValueError("natural run config bytes changed after effective capture")
    config = _load_natural_run_snapshot(run_path, run_snapshot)
    if config.artifact_sha256 != _lower_sha256(
        run_ref.get("artifact_sha256"), label="natural run-config artifact hash"
    ):
        raise ValueError("natural run config artifact differs from effective capture")
    sleeve = str(payload["sleeve"])
    if receipt_path != canonical_receipt_path(config, sleeve):
        raise ValueError("effective runtime config is not at the canonical sleeve path")
    repository, freeze, run, _freeze_payload = _source_bindings(
        config,
        run_snapshot=run_snapshot,
    )
    if (
        payload.get("repository") != repository
        or payload.get("freeze") != freeze
        or payload.get("natural_run_config") != run
    ):
        raise ValueError("effective runtime configuration source bindings changed")
    runtime = config.sleeve(sleeve)
    expected_paths = {
        "data_root": str(runtime.data_root),
        "event_tape_path": str(runtime.event_tape_path),
        "outcome_tape_path": str(runtime.outcome_tape_path),
        "target_capture_path": str(config.target_capture_path),
        "candidate_universe_path": str(config.candidate_universe_path),
    }
    if payload.get("runtime_paths") != expected_paths:
        raise ValueError("effective runtime configuration paths changed")
    window = payload.get("window")
    if window != {
        "t0_ns": config.t0_ns,
        "t1_ns": config.t1_ns,
        "interval": "half_open_[t0,t1)",
    }:
        raise ValueError("effective runtime configuration window changed")
    if int(payload.get("created_ts_ns") or 0) <= 0 or int(payload["created_ts_ns"]) >= config.t1_ns:
        raise ValueError("effective runtime configuration was not captured before T1")
    configs = payload.get("configs")
    scheduling = payload.get("scheduling")
    if not isinstance(configs, Mapping) or set(configs) != {"research", "sleeve", "strategy"}:
        raise ValueError("effective runtime configuration has invalid config bindings")
    if not isinstance(scheduling, Mapping) or not scheduling:
        raise ValueError("effective runtime configuration has no scheduling settings")
    for name in ("research", "sleeve"):
        binding = configs.get(name)
        if not isinstance(binding, Mapping) or set(binding) != {"type", "values"}:
            raise ValueError(f"effective runtime configuration {name} binding is invalid")
    strategy = configs.get("strategy")
    if strategy is not None and (
        not isinstance(strategy, Mapping) or set(strategy) != {"type", "values"}
    ):
        raise ValueError("effective runtime configuration strategy binding is invalid")
    after_path, after_data = _read_private(receipt_path, label="natural effective runtime config")
    if after_path != receipt_path or after_data != data:
        raise RuntimeError("effective runtime configuration changed while it was validated")
    return payload


def write_or_verify_effective_runtime_config(
    *,
    natural_run_config: NaturalRunConfig,
    sleeve: str,
    research_config: object,
    sleeve_config: object,
    strategy_config: object | None,
    scheduling: Mapping[str, Any],
    created_ts_ns: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Create once, or prove that a restart resolved byte-equivalent semantics."""

    path = canonical_receipt_path(natural_run_config, sleeve)
    if path.exists():
        existing = load_effective_runtime_config(path)
        expected = build_effective_runtime_config(
            natural_run_config=natural_run_config,
            sleeve=sleeve,
            research_config=research_config,
            sleeve_config=sleeve_config,
            strategy_config=strategy_config,
            scheduling=scheduling,
            created_ts_ns=int(existing["created_ts_ns"]),
        )
        if canonical_json(existing) != canonical_json(expected):
            raise ValueError(f"{sleeve} restart effective configuration differs from the first process")
        return path, existing
    payload = build_effective_runtime_config(
        natural_run_config=natural_run_config,
        sleeve=sleeve,
        research_config=research_config,
        sleeve_config=sleeve_config,
        strategy_config=strategy_config,
        scheduling=scheduling,
        created_ts_ns=created_ts_ns,
    )
    try:
        _exclusive_write(path, payload)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        return write_or_verify_effective_runtime_config(
            natural_run_config=natural_run_config,
            sleeve=sleeve,
            research_config=research_config,
            sleeve_config=sleeve_config,
            strategy_config=strategy_config,
            scheduling=scheduling,
            created_ts_ns=created_ts_ns,
        )
    return path, load_effective_runtime_config(path)


def _receipt_identity(
    snapshot: StableFileSnapshot,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "file_sha256": snapshot.sha256,
        "artifact_sha256": _lower_sha256(
            payload.get("artifact_sha256"), label="effective config artifact hash"
        ),
    }


def _candidate_binding(
    *,
    natural_run_config: NaturalRunConfig,
    freeze: Mapping[str, Any],
) -> dict[str, str]:
    population = freeze.get("population")
    candidate = population.get("candidate_universe") if isinstance(population, Mapping) else None
    if not isinstance(candidate, Mapping):
        raise ValueError("effective runtime config bundle freeze lacks candidate universe")
    binding = {
        "path": str(natural_run_config.candidate_universe_path),
        "file_sha256": natural_run_config.candidate_universe_file_sha256,
        "artifact_sha256": _lower_sha256(
            candidate.get("artifact_sha256"),
            label="candidate-universe artifact hash",
        ),
    }
    expected = {
        "path": str(candidate.get("path") or ""),
        "file_sha256": _lower_sha256(
            candidate.get("file_sha256"),
            label="candidate-universe file hash",
        ),
        "artifact_sha256": binding["artifact_sha256"],
    }
    if binding != expected:
        raise ValueError(
            "effective runtime config bundle candidate differs from run config/freeze"
        )
    return binding


def build_effective_runtime_config_bundle(
    receipt_paths: Mapping[str, str | Path],
    *,
    created_ts_ns: int | None = None,
    receipt_snapshots: Mapping[str, StableFileSnapshot] | None = None,
) -> dict[str, Any]:
    """Reopen and bind exactly the LONG and CONTINUOUS effective receipts."""

    if set(receipt_paths) != set(SLEEVES):
        raise ValueError("effective runtime config bundle requires exactly LONG and CONTINUOUS")
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= 0:
        raise ValueError("effective runtime config bundle creation time must be positive")
    receipts: dict[str, dict[str, Any]] = {}
    loaded: dict[str, dict[str, Any]] = {}
    for sleeve in SLEEVES:
        snapshot = (
            receipt_snapshots[sleeve]
            if receipt_snapshots is not None
            else _private_snapshot(
                receipt_paths[sleeve],
                label=f"{sleeve} effective config",
            )
        )
        path = snapshot.path
        payload = load_effective_runtime_config(path, snapshot=snapshot)
        if payload.get("sleeve") != sleeve:
            raise ValueError(f"effective config path labelled {sleeve} contains another sleeve")
        receipts[sleeve] = _receipt_identity(snapshot, payload)
        loaded[sleeve] = payload
    reference = loaded[SLEEVES[0]]
    for sleeve in SLEEVES[1:]:
        other = loaded[sleeve]
        for field in ("repository", "freeze", "natural_run_config", "window"):
            if other.get(field) != reference.get(field):
                raise ValueError(f"effective runtime configs disagree on {field}")
    run_ref = cast(Mapping[str, Any], reference["natural_run_config"])
    run_snapshot = _private_snapshot(
        str(run_ref.get("path") or ""),
        label="natural run config",
    )
    natural_run_config = _load_natural_run_snapshot(
        str(run_ref.get("path") or ""),
        run_snapshot,
    )
    freeze_ref = cast(Mapping[str, Any], reference["freeze"])
    freeze_snapshot = _private_snapshot(
        str(freeze_ref.get("path") or ""),
        label="natural cutover freeze manifest",
    )
    freeze = _load_freeze_snapshot(
        str(freeze_ref.get("path") or ""),
        freeze_snapshot,
    )
    candidate = _candidate_binding(
        natural_run_config=natural_run_config,
        freeze=freeze,
    )
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "validator": BUNDLE_VALIDATOR,
        "created_ts_ns": created,
        "execution_environment": "demo",
        "repository": reference["repository"],
        "freeze": reference["freeze"],
        "natural_run_config": reference["natural_run_config"],
        "candidate_universe": candidate,
        "window": reference["window"],
        "receipts": receipts,
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def load_effective_runtime_config_bundle(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    bundle_path, data = _read_private(
        path,
        label="effective runtime config bundle",
        snapshot=snapshot,
    )
    payload = _strict_json(data, label="effective runtime config bundle")
    expected_fields = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "execution_environment",
        "repository",
        "freeze",
        "natural_run_config",
        "candidate_universe",
        "window",
        "receipts",
        "artifact_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("effective runtime config bundle has unexpected or missing fields")
    if (
        payload.get("schema_version") != BUNDLE_SCHEMA_VERSION
        or payload.get("kind") != BUNDLE_KIND
        or payload.get("validator") != BUNDLE_VALIDATOR
        or payload.get("execution_environment") != "demo"
        or payload.get("artifact_sha256") != _artifact_hash(payload)
    ):
        raise ValueError("effective runtime config bundle identity or self-hash is invalid")
    receipts = payload.get("receipts")
    if not isinstance(receipts, Mapping) or set(receipts) != set(SLEEVES):
        raise ValueError("effective runtime config bundle lacks exact sleeve receipts")
    receipt_paths: dict[str, str] = {}
    receipt_snapshots: dict[str, StableFileSnapshot] = {}
    for sleeve in SLEEVES:
        identity = receipts.get(sleeve)
        if not isinstance(identity, Mapping) or set(identity) != {
            "path",
            "size_bytes",
            "file_sha256",
            "artifact_sha256",
        }:
            raise ValueError(f"effective runtime config bundle {sleeve} identity is invalid")
        receipt_snapshot = _private_snapshot(
            str(identity.get("path") or ""),
            label=f"{sleeve} effective config",
        )
        receipt_path = receipt_snapshot.path
        if receipt_snapshot.size != int(identity.get("size_bytes") or 0):
            raise ValueError(f"{sleeve} effective config size changed after bundling")
        if receipt_snapshot.sha256 != _lower_sha256(
            identity.get("file_sha256"), label=f"{sleeve} effective config file hash"
        ):
            raise ValueError(f"{sleeve} effective config bytes changed after bundling")
        loaded = load_effective_runtime_config(
            receipt_path,
            snapshot=receipt_snapshot,
        )
        if loaded.get("artifact_sha256") != identity.get("artifact_sha256"):
            raise ValueError(f"{sleeve} effective config artifact changed after bundling")
        receipt_paths[sleeve] = str(receipt_path)
        receipt_snapshots[sleeve] = receipt_snapshot
    rebuilt = build_effective_runtime_config_bundle(
        receipt_paths,
        created_ts_ns=int(payload.get("created_ts_ns") or 0),
        receipt_snapshots=receipt_snapshots,
    )
    if canonical_json(rebuilt) != canonical_json(payload):
        raise ValueError("effective runtime config bundle does not match its source receipts")
    run_ref = cast(Mapping[str, Any], payload.get("natural_run_config"))
    run_snapshot = _private_snapshot(
        str(run_ref.get("path") or ""),
        label="natural run config",
    )
    config = _load_natural_run_snapshot(
        str(run_ref.get("path") or ""),
        run_snapshot,
    )
    if bundle_path != canonical_bundle_path(config):
        raise ValueError("effective runtime config bundle is not at its canonical path")
    after_path, after_data = _read_private(bundle_path, label="effective runtime config bundle")
    if after_path != bundle_path or after_data != data:
        raise RuntimeError("effective runtime config bundle changed while it was validated")
    return payload


def load_effective_runtime_config_bundle_binding(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the verified bundle and its exact downstream evidence join."""

    bundle_path, before_data = _read_private(
        path,
        label="effective runtime config bundle",
        snapshot=snapshot,
    )
    payload = load_effective_runtime_config_bundle(
        bundle_path,
        snapshot=snapshot,
    )
    run_ref = cast(Mapping[str, Any], payload["natural_run_config"])
    run_snapshot = _private_snapshot(
        str(run_ref.get("path") or ""),
        label="natural run config",
    )
    natural_run_config = _load_natural_run_snapshot(
        str(run_ref.get("path") or ""),
        run_snapshot,
    )
    freeze_ref = cast(Mapping[str, Any], payload["freeze"])
    freeze_snapshot = _private_snapshot(
        str(freeze_ref.get("path") or ""),
        label="natural cutover freeze manifest",
    )
    freeze = _load_freeze_snapshot(
        str(freeze_ref.get("path") or ""),
        freeze_snapshot,
    )
    candidate = _candidate_binding(
        natural_run_config=natural_run_config,
        freeze=freeze,
    )
    if payload.get("candidate_universe") != candidate:
        raise ValueError("effective runtime config bundle candidate binding changed")

    sleeve_paths: dict[str, Any] = {}
    receipts = cast(Mapping[str, Any], payload["receipts"])
    for sleeve in SLEEVES:
        identity = cast(Mapping[str, Any], receipts[sleeve])
        receipt_snapshot = _private_snapshot(
            str(identity.get("path") or ""),
            label=f"{sleeve} effective config",
        )
        if (
            receipt_snapshot.size != int(identity.get("size_bytes") or 0)
            or receipt_snapshot.sha256 != identity.get("file_sha256")
        ):
            raise ValueError(f"effective runtime config bundle {sleeve} changed")
        receipt = load_effective_runtime_config(
            str(identity.get("path") or ""),
            snapshot=receipt_snapshot,
        )
        if (
            receipt.get("freeze") != payload["freeze"]
            or receipt.get("natural_run_config") != payload["natural_run_config"]
            or receipt.get("window") != payload["window"]
        ):
            raise ValueError(f"effective runtime config bundle {sleeve} joins changed")
        sleeve_paths[sleeve] = dict(
            cast(Mapping[str, Any], receipt["runtime_paths"])
        )

    after_path, after_data = _read_private(
        bundle_path, label="effective runtime config bundle"
    )
    if after_path != bundle_path or after_data != before_data:
        raise RuntimeError("effective runtime config bundle changed while binding")
    binding = {
        "path": str(bundle_path),
        "file_sha256": _sha256_bytes(before_data),
        "artifact_sha256": _lower_sha256(
            payload.get("artifact_sha256"),
            label="effective runtime config bundle artifact hash",
        ),
        "validator": BUNDLE_VALIDATOR,
        "created_ts_ns": int(payload["created_ts_ns"]),
        "repository": dict(cast(Mapping[str, Any], payload["repository"])),
        "freeze": dict(cast(Mapping[str, Any], payload["freeze"])),
        "natural_run_config": dict(
            cast(Mapping[str, Any], payload["natural_run_config"])
        ),
        "candidate_universe": candidate,
        "window": dict(cast(Mapping[str, Any], payload["window"])),
        "runtime_paths": {
            "target_capture_path": str(natural_run_config.target_capture_path),
            "sleeves": sleeve_paths,
        },
        "receipts": {
            sleeve: dict(cast(Mapping[str, Any], receipts[sleeve]))
            for sleeve in SLEEVES
        },
        "execution_authorization": "not_granted",
    }
    return payload, binding


def validate_effective_runtime_config_bundle_join(
    binding: Mapping[str, Any],
    *,
    freeze_manifest_path: str | Path,
    freeze_manifest_file_sha256: str,
    freeze_artifact_sha256: str,
    freeze_id: str,
    candidate_universe_path: str | Path,
    candidate_universe_file_sha256: str,
    candidate_universe_artifact_sha256: str,
    t0_ns: int,
    t1_ns: int,
    target_capture_path: str | Path,
    sleeve_tape_paths: Mapping[str, Mapping[str, str | Path]] | None = None,
) -> dict[str, Any]:
    """Require the exact freeze/run/candidate/window/runtime join for evidence."""

    freeze = binding.get("freeze")
    candidate = binding.get("candidate_universe")
    window = binding.get("window")
    runtime_paths = binding.get("runtime_paths")
    run_config = binding.get("natural_run_config")
    if not all(
        isinstance(value, Mapping)
        for value in (freeze, candidate, window, runtime_paths, run_config)
    ):
        raise ValueError("effective runtime config bundle binding is incomplete")
    freeze = cast(Mapping[str, Any], freeze)
    candidate = cast(Mapping[str, Any], candidate)
    window = cast(Mapping[str, Any], window)
    runtime_paths = cast(Mapping[str, Any], runtime_paths)
    run_config = cast(Mapping[str, Any], run_config)
    expected_freeze = {
        "path": str(Path(freeze_manifest_path).expanduser().resolve(strict=True)),
        "file_sha256": _lower_sha256(
            freeze_manifest_file_sha256, label="expected freeze file hash"
        ),
        "artifact_sha256": _lower_sha256(
            freeze_artifact_sha256, label="expected freeze artifact hash"
        ),
        "freeze_id": str(freeze_id),
    }
    if dict(freeze) != expected_freeze:
        raise ValueError("effective runtime config bundle names another freeze")
    expected_candidate = {
        "path": str(Path(candidate_universe_path).expanduser().resolve(strict=True)),
        "file_sha256": _lower_sha256(
            candidate_universe_file_sha256,
            label="expected candidate-universe file hash",
        ),
        "artifact_sha256": _lower_sha256(
            candidate_universe_artifact_sha256,
            label="expected candidate-universe artifact hash",
        ),
    }
    if dict(candidate) != expected_candidate:
        raise ValueError("effective runtime config bundle names another candidate universe")
    if dict(window) != {
        "t0_ns": t0_ns,
        "t1_ns": t1_ns,
        "interval": "half_open_[t0,t1)",
    }:
        raise ValueError("effective runtime config bundle names another natural window")
    if set(run_config) != {"path", "file_sha256", "artifact_sha256"}:
        raise ValueError("effective runtime config bundle run-config binding is malformed")
    if Path(str(runtime_paths.get("target_capture_path") or "")).resolve(
        strict=False
    ) != Path(target_capture_path).expanduser().resolve(strict=False):
        raise ValueError("effective runtime config bundle names another target capture")
    if sleeve_tape_paths is not None:
        if set(sleeve_tape_paths) != set(SLEEVES):
            raise ValueError("effective runtime config join requires LONG and CONTINUOUS tapes")
        bound_sleeves = runtime_paths.get("sleeves")
        if not isinstance(bound_sleeves, Mapping) or set(bound_sleeves) != set(SLEEVES):
            raise ValueError("effective runtime config bundle lacks exact sleeve paths")
        for sleeve in SLEEVES:
            supplied = sleeve_tape_paths[sleeve]
            bound = bound_sleeves.get(sleeve)
            if not isinstance(bound, Mapping):
                raise ValueError(f"effective runtime config bundle lacks {sleeve} paths")
            for field in ("event_tape_path", "outcome_tape_path"):
                if Path(str(bound.get(field) or "")).resolve(strict=False) != Path(
                    supplied[field]
                ).expanduser().resolve(strict=False):
                    raise ValueError(
                        f"effective runtime config bundle {sleeve} {field} differs"
                    )
    return dict(binding)


def write_effective_runtime_config_bundle(
    path: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    run_ref = cast(Mapping[str, Any], payload.get("natural_run_config"))
    run_snapshot = _private_snapshot(
        str(run_ref.get("path") or ""),
        label="natural run config",
    )
    config = _load_natural_run_snapshot(
        str(run_ref.get("path") or ""),
        run_snapshot,
    )
    expected = canonical_bundle_path(config)
    output = Path(path).expanduser()
    if not output.is_absolute() or output.resolve(strict=False) != expected:
        raise ValueError(f"effective runtime config bundle output must be {expected}")
    if payload.get("artifact_sha256") != _artifact_hash(payload):
        raise ValueError("effective runtime config bundle self-hash is invalid")
    _exclusive_write(expected, payload)
    load_effective_runtime_config_bundle(expected)
    return expected


def _parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    bundle = subparsers.add_parser("bundle", help="bind both stopped natural producer configs")
    bundle.add_argument("--receipt", action="append", default=[], metavar="SLEEVE=/ABSOLUTE/PATH")
    bundle.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-bundle", help="source-reopen an effective-config bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    return parser


def _named_receipts(values: Sequence[str]) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or name not in SLEEVES or name in output or not raw_path:
            raise ValueError("--receipt must name each sleeve once as SLEEVE=/ABSOLUTE/PATH")
        output[name] = Path(raw_path)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify-bundle":
        payload = load_effective_runtime_config_bundle(args.bundle)
        print(json.dumps({"artifact_sha256": payload["artifact_sha256"], "status": "verified"}, sort_keys=True))
        return 0
    receipts = _named_receipts(args.receipt)
    payload = build_effective_runtime_config_bundle(receipts)
    path = write_effective_runtime_config_bundle(args.output, payload)
    print(json.dumps({"artifact_sha256": payload["artifact_sha256"], "path": str(path)}, sort_keys=True))
    return 0


__all__ = [
    "BUNDLE_KIND",
    "BUNDLE_VALIDATOR",
    "RECEIPT_KIND",
    "RECEIPT_VALIDATOR",
    "build_effective_runtime_config",
    "build_effective_runtime_config_bundle",
    "canonical_bundle_path",
    "canonical_receipt_path",
    "load_effective_runtime_config",
    "load_effective_runtime_config_bundle",
    "load_effective_runtime_config_bundle_binding",
    "validate_effective_runtime_config_bundle_join",
    "main",
    "write_effective_runtime_config_bundle",
    "write_or_verify_effective_runtime_config",
]


if __name__ == "__main__":
    raise SystemExit(main())
