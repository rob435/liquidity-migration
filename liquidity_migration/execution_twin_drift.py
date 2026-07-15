"""Source-bound out-of-sample validation for the demo execution twin.

V7 is training evidence.  This module first binds the archived V7 journal and
raw capture to the schema-v3 calibration receipt, then evaluates a disjoint
natural demo epoch without trusting copied holdout summaries.  The resulting
receipt is an execution-model diagnostic only; it grants no trading or deploy
authority.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import sqlite3
import stat
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .account_kernel import (
    GENESIS_HASH,
    AccountEvent,
    AccountEventType,
    InstrumentRules,
    MarketInputRef,
    OrderCommand,
    account_journal_path,
    account_transactions_path,
    read_account_journal_bytes,
    target_batch_request_hash,
)
from .account_execution_config import load_demo_rules_bytes
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .account_service import SleeveAdapterKind
from .captured_account_replay import (
    HOUR_NS,
    NATURAL_WINDOW_HOURS,
    REGISTERED_MAX_DECISION_AGE_NS,
    load_post_window_safety_manifest,
)
from .clock_offset_series import (
    INTERPOLATION_METHOD,
    UNCERTAINTY_METHOD,
    ClockOffsetInterpolator,
    load_clock_offset_series,
)
from .deterministic_serialization import canonical_json
from .execution_adapters import (
    BookLevel,
    ExecutionObservation,
    ExecutionObservationType,
    ExecutionTwinConfig,
    L2BookSnapshot,
    MarketOrderExecutionTwin,
)
from .execution_twin_calibration import (
    CALIBRATION_SCHEMA_VERSION,
    CalibrationRequirements,
    calibrate_execution_twin,
    execution_twin_config_from_calibration,
    load_calibration_receipt,
)
from .market_capture import capture_record_id
from .natural_cutover_freeze_manifest import load_natural_cutover_freeze_manifest
from .strategy_target_replay import (
    CapturedTargetRequest,
    TargetSchedulingCaptureEvent,
    load_target_scheduling_capture_bytes,
)


DRIFT_SCHEMA_VERSION = 3
DRIFT_KIND = "bybit_demo_execution_twin_oos_drift"
DRIFT_VALIDATOR = "execution_twin_drift_v3"
DRIFT_EVIDENCE_SCOPE = "bybit_demo_market_order_execution_twin_oos_holdout_drift"
CONFIG_SCHEMA_VERSION = 2
CONFIG_KIND = "v7_market_order_execution_twin_config"
ARCHIVE_MAP_SCHEMA_VERSION = 1
ARCHIVE_MAP_KIND = "v7_execution_twin_archive_source_map"

BASELINE_CONFIG_ROLE = "baseline_p50"
STRESS_CONFIG_ROLE = "stress_p95"
NONNEGATIVE_MIN_RATIO = 0.99
STRESS_MIN_COVERAGE = 0.95
STRESS_ROUNDING_TOLERANCE_TICKS = 1
MIN_HOLDOUT_SPACING_SAMPLES = 3
_TERMINAL_STATUSES = frozenset({"filled", "partially_filled_cancelled", "cancelled", "rejected"})
_LIMITATIONS = (
    "v7_is_training_and_natural_demo_is_a_disjoint_forward_holdout",
    "market_by_price_does_not_identify_passive_queue_position_or_market_impact",
    "immutable_decision_books_do_not_model_future_book_mutation",
    "periodic_public_clock_samples_do_not_hard_bound_between_sample_clock_drift",
    "feed_latency_gate_uses_interpolated_clock_point_estimates_with_reported_sensitivity",
    "freeze_manifest_is_a_source_binding_not_execution_or_deploy_authority",
    "stress_coverage_is_not_distributional_stationarity_or_tail_calibration",
    "lifecycle_round_trip_pnl_hourly_coverage_and_accounting_are_separate_gates",
    "receipt_does_not_establish_alpha_deployment_readiness_or_trading_authorization",
)


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    label: str
    path: str
    size: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _LinkedCommand:
    command: OrderCommand
    event: AccountEvent
    market_event: AccountEvent
    market_input: MarketInputRef
    context: Mapping[str, Any]
    book: L2BookSnapshot
    sleeves: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _NaturalScope:
    t0_ns: int
    t1_ns: int
    natural_requests: Mapping[str, CapturedTargetRequest]
    sleeve_by_batch: Mapping[str, str]
    natural_capture_tape_hash: str
    natural_capture_event_count: int
    safety_requests: Mapping[str, CapturedTargetRequest]
    safety_capture_tape_hash: str
    safety_capture_event_count: int
    safety_manifest: Mapping[str, Any]

    @property
    def natural_batch_ids(self) -> frozenset[str]:
        return frozenset(self.natural_requests)

    @property
    def safety_batch_ids(self) -> frozenset[str]:
        return frozenset(self.safety_requests)


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _canonical_json_value(value: Any) -> bytes:
    """Canonical JSON for scalar/list hash material unsupported by canonical_json's type."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _lower_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _strict_json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"{label} contains non-finite JSON token {token}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    _require_finite_tree(value, label=label)
    return value


def _require_finite_tree(value: Any, *, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} contains NaN or infinity")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require_finite_tree(item, label=f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_finite_tree(item, label=f"{label}[{index}]")


def _regular_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {candidate}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a non-symlink regular file")
    return candidate.resolve(strict=True)


def _directory(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {candidate}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a non-symlink directory")
    return candidate.resolve(strict=True)


def _read_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return read_stable_file(
        candidate,
        label=label,
        require_single_link=False,
    )


def _use_snapshot(
    path: str | Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None,
) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if snapshot is None:
        return _read_snapshot(candidate, label=label)
    if snapshot.path != Path(os.path.abspath(candidate)):
        raise ValueError(f"{label} snapshot path differs")
    return snapshot


def _identity_from_snapshot(snapshot: StableFileSnapshot, *, label: str) -> _FileIdentity:
    return _FileIdentity(
        label=label,
        path=str(snapshot.path),
        size=snapshot.size,
        sha256=snapshot.sha256,
        device=snapshot.device,
        inode=snapshot.inode,
        mtime_ns=snapshot.mtime_ns,
        mode=snapshot.mode,
    )


def _read_identity(path: Path, *, label: str) -> _FileIdentity:
    return _identity_from_snapshot(_read_snapshot(path, label=label), label=label)


def _atomic_create(path: str | Path, payload: Mapping[str, Any], *, label: str) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute():
        raise ValueError(f"{label} output must be absolute")
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = _directory(output.parent, label=f"{label} output parent")
    resolved = parent / output.name
    data = canonical_json(dict(payload)) + b"\n"
    descriptor = os.open(str(resolved), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(f"{label} write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(str(parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _require_disjoint(paths: Mapping[str, Path]) -> None:
    rows = list(paths.items())
    for index, (left_label, left) in enumerate(rows):
        for right_label, right in rows[index + 1 :]:
            left_stat = left.stat()
            right_stat = right.stat()
            same_identity = (left_stat.st_dev, left_stat.st_ino) == (
                right_stat.st_dev,
                right_stat.st_ino,
            )
            if _paths_overlap(left, right) or same_identity:
                raise ValueError(f"source epochs overlap: {left_label}={left} and {right_label}={right}")


def _reject_current_original_aliases(*, original: Mapping[str, Any], archived: Mapping[str, Path]) -> None:
    """Reject an archive that is only another name for a current live root.

    The original V7 lexical roots may legitimately have been deleted and then
    recreated for the natural epoch.  Therefore current paths are rejected only
    when their directory identity aliases the corresponding archive, not merely
    because the old path string exists again.
    """

    for key, archived_root in archived.items():
        raw_original = Path(str(original.get(key) or "")).expanduser()
        if not raw_original.is_absolute():
            raise ValueError(f"V7 original {key} must be an absolute path")
        try:
            original_root = raw_original.resolve(strict=True)
            metadata = original_root.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"V7 original {key} cannot be inspected") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        archived_metadata = archived_root.stat()
        if _paths_overlap(original_root, archived_root) or (
            metadata.st_dev,
            metadata.st_ino,
        ) == (archived_metadata.st_dev, archived_metadata.st_ino):
            raise ValueError(f"archived V7 {key} aliases the current original/live root")


def _journal_paths(root: Path, *, prefix: str) -> list[tuple[str, Path]]:
    transaction_root = account_transactions_path(root)
    transactions = sorted(transaction_root.glob("*.json")) if transaction_root.is_dir() else []
    output = [(f"{prefix}/transactions/{path.name}", path) for path in transactions]
    projection = account_journal_path(root)
    if projection.exists():
        output.append((f"{prefix}/events.jsonl", projection))
    if not output:
        raise ValueError(f"no account journal files under {root}")
    return output


def _capture_paths(root: Path, *, prefix: str) -> list[tuple[str, Path]]:
    paths = sorted(root.rglob("segment-*.jsonl"))
    if not paths:
        raise ValueError(f"no market capture segments under {root}")
    return [(f"{prefix}/{path.relative_to(root)}", path) for path in paths]


def _journal_sha256(events: Sequence[AccountEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


def _snapshot_fingerprint(
    snapshots: Mapping[str, StableFileSnapshot],
) -> dict[str, tuple[Any, ...]]:
    return {
        label: (
            str(snapshot.path),
            snapshot.device,
            snapshot.inode,
            snapshot.metadata.st_mode,
            snapshot.uid,
            snapshot.nlink,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.metadata.st_ctime_ns,
            snapshot.sha256,
        )
        for label, snapshot in sorted(snapshots.items())
    }


def _archive_journal_snapshot(
    root: Path,
) -> tuple[list[AccountEvent], dict[str, StableFileSnapshot]]:
    snapshots = {
        label: _read_snapshot(path, label=label)
        for label, path in _journal_paths(root, prefix="archive_map_journal")
    }
    return (
        _account_journal_from_snapshots(snapshots, prefix="archive_map_journal"),
        snapshots,
    )


def _capture_manifest(
    root: Path,
) -> tuple[list[dict[str, str]], str, dict[str, StableFileSnapshot]]:
    rows: list[dict[str, str]] = []
    snapshots: dict[str, StableFileSnapshot] = {}
    for path in sorted(root.rglob("segment-*.jsonl")):
        relative = str(path.relative_to(root))
        snapshot = _read_snapshot(
            path,
            label=f"archive_map_capture/{relative}",
        )
        snapshots[relative] = snapshot
        rows.append(
            {
                "path": relative,
                "sha256": snapshot.sha256,
            }
        )
    if not rows:
        raise ValueError(f"no market capture segments under {root}")
    digest = hashlib.sha256(canonical_json({"files": rows})).hexdigest()
    return rows, digest, snapshots


def _requirements(receipt: Mapping[str, Any]) -> CalibrationRequirements:
    raw = receipt.get("requirements")
    if not isinstance(raw, Mapping):
        raise ValueError("V7 calibration receipt lacks requirements")
    return CalibrationRequirements(**dict(raw))


def _recompute_v7_from_archive(
    receipt: Mapping[str, Any],
    *,
    archived_account_root: Path,
    archived_market_capture_root: Path,
) -> dict[str, Any]:
    rebuilt = calibrate_execution_twin(
        account_root=archived_account_root,
        market_capture_root=archived_market_capture_root,
        expected_account_id=str(receipt.get("expected_account_id") or ""),
        observed_ts_ns=int(receipt.get("observed_ts_ns") or 0),
        local_minus_exchange_ns=cast(Mapping[str, Any], receipt["inputs"]).get("local_minus_exchange_ns"),
        clock_offset_receipt_sha256=str(
            cast(Mapping[str, Any], receipt["inputs"]).get("clock_offset_receipt_sha256") or ""
        ),
        requirements=_requirements(receipt),
    )
    rebuilt_inputs = cast(dict[str, Any], rebuilt["inputs"])
    expected_inputs = cast(Mapping[str, Any], receipt["inputs"])
    # Archive roots intentionally differ from the now-reset lexical V7 roots.
    # Normalize only those two path strings; every source hash and metric must
    # still reproduce exactly.
    rebuilt_inputs["account_root"] = expected_inputs["account_root"]
    rebuilt_inputs["market_capture_root"] = expected_inputs["market_capture_root"]
    rebuilt["artifact_sha256"] = _self_hash(rebuilt)
    if canonical_json(rebuilt) != canonical_json(dict(receipt)):
        raise ValueError("archived V7 sources do not reproduce the schema-v3 calibration receipt")
    return rebuilt


def build_v7_archive_source_map(
    *,
    calibration_file: str | Path,
    archived_account_root: str | Path,
    archived_market_capture_root: str | Path,
) -> dict[str, Any]:
    """Bind moved V7 training sources to their immutable calibration receipt."""

    calibration_path = _regular_file(calibration_file, label="V7 calibration receipt")
    account_root = _directory(archived_account_root, label="archived V7 account root")
    capture_root = _directory(archived_market_capture_root, label="archived V7 market-capture root")
    _require_disjoint({"archived_v7_account_root": account_root, "archived_v7_capture_root": capture_root})
    calibration_snapshot = _read_snapshot(
        calibration_path,
        label="V7 calibration receipt",
    )
    receipt = load_calibration_receipt(
        calibration_path,
        require_registered_requirements=True,
        snapshot=calibration_snapshot,
    )
    if int(receipt.get("schema_version") or 0) != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("V7 archive map refuses a pre-schema-v3 calibration receipt")
    if receipt.get("execution_twin_gate_passed") is not True:
        raise ValueError("V7 calibration gate has not passed")
    _recompute_v7_from_archive(
        receipt,
        archived_account_root=account_root,
        archived_market_capture_root=capture_root,
    )
    events, journal_snapshots = _archive_journal_snapshot(account_root)
    manifest, manifest_sha256, capture_snapshots = _capture_manifest(capture_root)
    inputs = cast(Mapping[str, Any], receipt["inputs"])
    payload: dict[str, Any] = {
        "schema_version": ARCHIVE_MAP_SCHEMA_VERSION,
        "kind": ARCHIVE_MAP_KIND,
        "calibration_artifact_sha256": receipt["artifact_sha256"],
        "original_sources": {
            "account_root": inputs["account_root"],
            "market_capture_root": inputs["market_capture_root"],
        },
        "archived_sources": {
            "account_root": str(account_root),
            "market_capture_root": str(capture_root),
        },
        "account_journal_sha256": _journal_sha256(events),
        "account_last_event_hash": events[-1].event_hash,
        "market_capture_manifest": manifest,
        "market_capture_manifest_sha256": manifest_sha256,
        "execution_authorization": "not_granted",
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    _recompute_v7_from_archive(
        receipt,
        archived_account_root=account_root,
        archived_market_capture_root=capture_root,
    )
    final_events, final_journal_snapshots = _archive_journal_snapshot(account_root)
    (
        final_manifest,
        final_manifest_sha256,
        final_capture_snapshots,
    ) = _capture_manifest(capture_root)
    final_calibration_snapshot = _read_snapshot(
        calibration_path,
        label="V7 calibration receipt",
    )
    if (
        final_calibration_snapshot.sha256 != calibration_snapshot.sha256
        or _snapshot_fingerprint(final_journal_snapshots)
        != _snapshot_fingerprint(journal_snapshots)
        or _snapshot_fingerprint(final_capture_snapshots)
        != _snapshot_fingerprint(capture_snapshots)
        or canonical_json({"events": [event.to_dict() for event in final_events]})
        != canonical_json({"events": [event.to_dict() for event in events]})
        or final_manifest != manifest
        or final_manifest_sha256 != manifest_sha256
    ):
        raise RuntimeError("V7 archive-map sources mutated during construction")
    return payload


def verify_v7_archive_source_map(
    payload: Mapping[str, Any],
    *,
    calibration_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    value = dict(payload)
    if int(value.get("schema_version") or 0) != ARCHIVE_MAP_SCHEMA_VERSION:
        raise ValueError("unsupported V7 archive-source map schema")
    if value.get("kind") != ARCHIVE_MAP_KIND:
        raise ValueError("unexpected V7 archive-source map kind")
    if value.get("execution_authorization") != "not_granted":
        raise ValueError("V7 archive-source map cannot grant execution authority")
    observed = _lower_sha256(value.get("artifact_sha256"), label="archive-map hash")
    if observed != _self_hash(value):
        raise ValueError("V7 archive-source map hash mismatch")
    if value.get("calibration_artifact_sha256") != calibration_receipt.get("artifact_sha256"):
        raise ValueError("V7 archive-source map names another calibration receipt")
    original = value.get("original_sources")
    archived = value.get("archived_sources")
    inputs = calibration_receipt.get("inputs")
    if not isinstance(original, Mapping) or not isinstance(archived, Mapping) or not isinstance(inputs, Mapping):
        raise ValueError("V7 archive-source map source fields are malformed")
    if dict(original) != {
        "account_root": inputs.get("account_root"),
        "market_capture_root": inputs.get("market_capture_root"),
    }:
        raise ValueError("V7 archive-source map changed original source identities")
    account_root = _directory(str(archived.get("account_root") or ""), label="archived V7 account root")
    capture_root = _directory(
        str(archived.get("market_capture_root") or ""),
        label="archived V7 market-capture root",
    )
    _require_disjoint({"archived_v7_account_root": account_root, "archived_v7_capture_root": capture_root})
    _reject_current_original_aliases(
        original=original,
        archived={"account_root": account_root, "market_capture_root": capture_root},
    )
    _recompute_v7_from_archive(
        calibration_receipt,
        archived_account_root=account_root,
        archived_market_capture_root=capture_root,
    )
    events, journal_snapshots = _archive_journal_snapshot(account_root)
    manifest, manifest_sha256, capture_snapshots = _capture_manifest(capture_root)
    if value.get("account_journal_sha256") != _journal_sha256(events):
        raise ValueError("archived V7 journal changed after archive-map creation")
    if value.get("account_last_event_hash") != events[-1].event_hash:
        raise ValueError("archived V7 journal head changed after archive-map creation")
    if (
        value.get("market_capture_manifest") != manifest
        or value.get("market_capture_manifest_sha256") != manifest_sha256
    ):
        raise ValueError("archived V7 market capture changed after archive-map creation")
    _recompute_v7_from_archive(
        calibration_receipt,
        archived_account_root=account_root,
        archived_market_capture_root=capture_root,
    )
    final_events, final_journal_snapshots = _archive_journal_snapshot(account_root)
    (
        final_manifest,
        final_manifest_sha256,
        final_capture_snapshots,
    ) = _capture_manifest(capture_root)
    if (
        _snapshot_fingerprint(final_journal_snapshots)
        != _snapshot_fingerprint(journal_snapshots)
        or _snapshot_fingerprint(final_capture_snapshots)
        != _snapshot_fingerprint(capture_snapshots)
        or canonical_json({"events": [event.to_dict() for event in final_events]})
        != canonical_json({"events": [event.to_dict() for event in events]})
        or final_manifest != manifest
        or final_manifest_sha256 != manifest_sha256
    ):
        raise RuntimeError("V7 archive-map sources mutated during verification")
    return value


def load_v7_archive_source_map(
    path: str | Path,
    *,
    calibration_receipt: Mapping[str, Any],
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    source = _use_snapshot(
        path,
        label="V7 archive-source map",
        snapshot=snapshot,
    )
    return verify_v7_archive_source_map(
        _strict_json(source.data, label="V7 archive-source map"),
        calibration_receipt=calibration_receipt,
    )


def write_v7_archive_source_map(path: str | Path, payload: Mapping[str, Any]) -> Path:
    value = dict(payload)
    if value.get("kind") != ARCHIVE_MAP_KIND or value.get("artifact_sha256") != _self_hash(value):
        raise ValueError("invalid V7 archive-source map")
    archived = cast(Mapping[str, Any], value.get("archived_sources") or {})
    output_candidate = Path(path).expanduser()
    if not output_candidate.is_absolute():
        raise ValueError("V7 archive-source map output must be absolute")
    output = output_candidate.resolve(strict=False)
    for label in ("account_root", "market_capture_root"):
        source = _directory(str(archived.get(label) or ""), label=f"archived V7 {label}")
        if output == source or source in output.parents:
            raise ValueError("V7 archive-source map output cannot be nested in archived sources")
    return _atomic_create(output, value, label="V7 archive-source map")


def _config_for_role(receipt: Mapping[str, Any], *, role: str, max_decision_age_ns: int) -> ExecutionTwinConfig:
    if role == BASELINE_CONFIG_ROLE:
        quantile = "p50"
    elif role == STRESS_CONFIG_ROLE:
        quantile = "p95"
    else:
        raise ValueError(f"unsupported execution-twin config role {role!r}")
    return execution_twin_config_from_calibration(
        receipt,
        max_decision_age_ns=max_decision_age_ns,
        latency_quantile=quantile,
        slippage_quantile=quantile,
        require_gate=True,
        require_registered_requirements=True,
    )


def build_execution_twin_config_artifact(
    calibration_receipt: Mapping[str, Any],
    *,
    role: str,
    max_decision_age_ns: int,
) -> dict[str, Any]:
    if max_decision_age_ns != REGISTERED_MAX_DECISION_AGE_NS:
        raise ValueError(
            "execution-twin config max decision age must equal the registered "
            f"{REGISTERED_MAX_DECISION_AGE_NS} ns"
        )
    config = _config_for_role(calibration_receipt, role=role, max_decision_age_ns=max_decision_age_ns)
    quantile = "p50" if role == BASELINE_CONFIG_ROLE else "p95"
    payload: dict[str, Any] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "kind": CONFIG_KIND,
        "role": role,
        "calibration_artifact_sha256": calibration_receipt["artifact_sha256"],
        "latency_quantile": quantile,
        "slippage_quantile": quantile,
        "max_decision_age_ns": max_decision_age_ns,
        "config": asdict(config),
        "passive_queue_calibrated": False,
        "immutable_replay_book": True,
        "execution_authorization": "not_granted",
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def verify_execution_twin_config_artifact(
    payload: Mapping[str, Any],
    *,
    calibration_receipt: Mapping[str, Any],
    expected_role: str,
) -> dict[str, Any]:
    value = dict(payload)
    if int(value.get("schema_version") or 0) != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported execution-twin config artifact schema")
    if value.get("kind") != CONFIG_KIND or value.get("role") != expected_role:
        raise ValueError("execution-twin config artifact has the wrong kind or role")
    if value.get("execution_authorization") != "not_granted":
        raise ValueError("execution-twin config artifact cannot grant authority")
    if value.get("passive_queue_calibrated") is not False or value.get("immutable_replay_book") is not True:
        raise ValueError("execution-twin config changed its registered model scope")
    if value.get("calibration_artifact_sha256") != calibration_receipt.get("artifact_sha256"):
        raise ValueError("execution-twin config names another calibration receipt")
    max_age = value.get("max_decision_age_ns")
    if type(max_age) is not int or max_age <= 0:
        raise ValueError("execution-twin config max decision age is invalid")
    expected = build_execution_twin_config_artifact(
        calibration_receipt, role=expected_role, max_decision_age_ns=max_age
    )
    if canonical_json(value) != canonical_json(expected):
        raise ValueError("execution-twin config does not reproduce from V7")
    return value


def load_execution_twin_config_artifact(
    path: str | Path,
    *,
    calibration_receipt: Mapping[str, Any],
    expected_role: str,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    source = _use_snapshot(
        path,
        label=f"{expected_role} execution-twin config",
        snapshot=snapshot,
    )
    return verify_execution_twin_config_artifact(
        _strict_json(source.data, label=f"{expected_role} execution-twin config"),
        calibration_receipt=calibration_receipt,
        expected_role=expected_role,
    )


def write_execution_twin_config_artifact(path: str | Path, payload: Mapping[str, Any]) -> Path:
    value = dict(payload)
    if value.get("kind") != CONFIG_KIND or value.get("artifact_sha256") != _self_hash(value):
        raise ValueError("invalid execution-twin config artifact")
    return _atomic_create(path, value, label="execution-twin config artifact")


def _freeze_sources(
    *,
    calibration_file: Path,
    archive_map_file: Path,
    archive_map: Mapping[str, Any],
    natural_account_root: Path,
    natural_capture_root: Path,
    freeze_manifest_file: Path,
    natural_target_capture_file: Path,
    safety_target_capture_file: Path,
    safety_manifest_file: Path,
    demo_rules_file: Path,
    clock_offset_series_file: Path,
    baseline_config_file: Path,
    stress_config_file: Path,
    initial_snapshots: Mapping[str, StableFileSnapshot] | None = None,
) -> tuple[dict[str, _FileIdentity], dict[str, StableFileSnapshot]]:
    archived = cast(Mapping[str, Any], archive_map["archived_sources"])
    archived_account_root = _directory(str(archived["account_root"]), label="archived V7 account root")
    archived_capture_root = _directory(str(archived["market_capture_root"]), label="archived V7 capture root")
    paths: list[tuple[str, Path]] = [
        ("v7_calibration_receipt", calibration_file),
        ("v7_archive_source_map", archive_map_file),
        ("natural_demo_rules", demo_rules_file),
        ("natural_clock_offset_series", clock_offset_series_file),
        ("baseline_twin_config", baseline_config_file),
        ("stress_twin_config", stress_config_file),
        ("natural_cutover_freeze_manifest", freeze_manifest_file),
        ("natural_target_capture", natural_target_capture_file),
        ("post_window_safety_target_capture", safety_target_capture_file),
        ("post_window_safety_manifest", safety_manifest_file),
        *_journal_paths(archived_account_root, prefix="v7_archive_journal"),
        *_capture_paths(archived_capture_root, prefix="v7_archive_capture"),
        *_journal_paths(natural_account_root, prefix="natural_journal"),
        *_capture_paths(natural_capture_root, prefix="natural_capture"),
    ]
    labels = [label for label, _path in paths]
    if len(labels) != len(set(labels)):
        raise RuntimeError("execution-twin drift source labels are not unique")
    resolved_paths = [path.resolve(strict=True) for _label, path in paths]
    if len(resolved_paths) != len(set(resolved_paths)):
        raise ValueError("execution-twin drift sources alias the same file")
    identities: dict[str, _FileIdentity] = {}
    snapshots: dict[str, StableFileSnapshot] = {}
    for label, path in paths:
        supplied = (initial_snapshots or {}).get(label)
        snapshot = _use_snapshot(
            path,
            label=label,
            snapshot=supplied,
        )
        snapshots[label] = snapshot
        identities[label] = _identity_from_snapshot(snapshot, label=label)
    source_identities = [(identity.device, identity.inode) for identity in identities.values()]
    if len(source_identities) != len(set(source_identities)):
        raise ValueError("execution-twin drift sources alias the same file identity")
    return identities, snapshots


def _identity_payload(identities: Mapping[str, _FileIdentity]) -> dict[str, Any]:
    return {label: identity.to_dict() for label, identity in sorted(identities.items())}


def _account_journal_from_snapshots(snapshots: Mapping[str, StableFileSnapshot], *, prefix: str) -> list[AccountEvent]:
    transaction_prefix = prefix + "/transactions/"
    transaction_files = [
        (label.removeprefix(transaction_prefix), snapshots[label].data)
        for label in sorted(snapshots)
        if label.startswith(transaction_prefix)
    ]
    projection = snapshots.get(prefix + "/events.jsonl")
    return read_account_journal_bytes(
        transaction_files=transaction_files,
        projection_data=None if projection is None else projection.data,
        projection_label=prefix + "/events.jsonl",
        verify=True,
    )


def _load_freeze_snapshot(path: Path, snapshot: StableFileSnapshot) -> dict[str, Any]:
    if "snapshot" in inspect.signature(load_natural_cutover_freeze_manifest).parameters:
        return load_natural_cutover_freeze_manifest(path, snapshot=snapshot)
    # Compatibility for narrow test doubles; the production loader consumes
    # the descriptor-bound snapshot above.
    return load_natural_cutover_freeze_manifest(path)


def _require_exact_natural_window(t0_ns: int, t1_ns: int) -> None:
    if type(t0_ns) is not int or type(t1_ns) is not int:
        raise ValueError("natural window timestamps must be integers")
    if t0_ns <= 0 or t0_ns % HOUR_NS != 0:
        raise ValueError("natural T0 must be a positive UTC hour boundary")
    if t1_ns != t0_ns + NATURAL_WINDOW_HOURS * HOUR_NS:
        raise ValueError("natural window must be exactly 120 hours")


def _natural_request_scope(
    capture_events: Sequence[TargetSchedulingCaptureEvent],
    *,
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
) -> tuple[dict[str, CapturedTargetRequest], dict[str, str]]:
    if not capture_events:
        raise ValueError("natural target capture is empty")
    requests: dict[str, CapturedTargetRequest] = {}
    sleeve_by_batch: dict[str, str] = {}
    request_ids: set[str] = set()
    route_id = ""
    for capture in capture_events:
        event = capture.source_event
        if (
            capture.source_environment != "demo"
            or capture.sleeve not in {SleeveAdapterKind.LONG.value, SleeveAdapterKind.CONTINUOUS.value}
            or not (t0_ns <= event.event_ts_ns < t1_ns)
        ):
            raise ValueError("natural target capture changed source/window classification")
        if capture.strategy_profile.startswith("natural-account-safety-flatten"):
            raise ValueError("natural target capture contains a safety producer event")
        for captured in capture.requests:
            request = captured.request
            if request.batch_id in requests:
                raise ValueError(f"natural target capture repeats batch {request.batch_id!r}")
            if request.request_id in request_ids:
                raise ValueError("natural target capture repeats request_id")
            if (
                request.account_id != expected_account_id
                or request.environment != "demo"
                or not (t0_ns <= request.created_ts_ns < t1_ns)
                or request.created_ts_ns < event.event_ts_ns
            ):
                raise ValueError("natural captured request changed account/window identity")
            if route_id and request.route_id != route_id:
                raise ValueError("natural target capture spans multiple account routes")
            route_id = request.route_id
            if any(SleeveAdapterKind(item.adapter_kind).value != capture.sleeve for item in request.intents):
                raise ValueError("natural captured request changed sleeve ownership")
            requests[request.batch_id] = captured
            sleeve_by_batch[request.batch_id] = capture.sleeve
            request_ids.add(request.request_id)
    return requests, sleeve_by_batch


def _load_natural_scope(
    *,
    natural_target_capture_file: Path,
    safety_target_capture_file: Path,
    safety_manifest_file: Path,
    identities: Mapping[str, _FileIdentity],
    snapshots: Mapping[str, StableFileSnapshot],
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
) -> _NaturalScope:
    _require_exact_natural_window(t0_ns, t1_ns)
    if identities["natural_target_capture"].mode != 0o600:
        raise ValueError("natural target capture must be mode 0600")
    natural_events, natural_tape_hash = load_target_scheduling_capture_bytes(snapshots["natural_target_capture"].data)
    natural_requests, sleeve_by_batch = _natural_request_scope(
        natural_events,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    safety_manifest = load_post_window_safety_manifest(
        safety_manifest_file,
        target_capture_path=safety_target_capture_file,
        expected_account_id=expected_account_id,
        expected_t1_ns=t1_ns,
        manifest_snapshot=snapshots["post_window_safety_manifest"],
        capture_snapshot=snapshots["post_window_safety_target_capture"],
    )
    safety_events, safety_tape_hash = load_target_scheduling_capture_bytes(
        snapshots["post_window_safety_target_capture"].data
    )
    safety_requests: dict[str, CapturedTargetRequest] = {}
    for capture in safety_events:
        for captured in capture.requests:
            batch_id = captured.request.batch_id
            if batch_id in safety_requests:
                raise ValueError("post-window safety capture repeats a batch")
            safety_requests[batch_id] = captured
    if list(safety_requests) != safety_manifest.get("batch_ids"):
        raise ValueError("post-window safety manifest batch order changed")
    if frozenset(natural_requests) & frozenset(safety_requests):
        raise ValueError("natural and post-window safety batch sets overlap")
    return _NaturalScope(
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        natural_requests=natural_requests,
        sleeve_by_batch=sleeve_by_batch,
        natural_capture_tape_hash=_lower_sha256(natural_tape_hash, label="natural target capture tape hash"),
        natural_capture_event_count=len(natural_events),
        safety_requests=safety_requests,
        safety_capture_tape_hash=_lower_sha256(safety_tape_hash, label="post-window safety capture tape hash"),
        safety_capture_event_count=len(safety_events),
        safety_manifest=safety_manifest,
    )


def _expected_scoped_risk_hash(
    captured: CapturedTargetRequest,
    *,
    strict_risk_reduction_required: bool,
) -> str:
    request = captured.request
    command_symbols = {str(item.intent.symbol).strip().upper() for item in request.intents}
    if "" in command_symbols:
        raise ValueError("captured target request contains an empty symbol")
    return target_batch_request_hash(
        batch_id=request.batch_id,
        target_payloads=(),
        command_symbols=command_symbols,
        require_strict_risk_reduction=strict_risk_reduction_required,
        request_content_hash=request.content_hash(),
    )


def _require_frozen_artifact_ref(
    raw: object,
    *,
    label: str,
    path: Path,
    identity: _FileIdentity,
    artifact_sha256: object,
) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(f"freeze manifest lacks {label} artifact binding")
    expected = {
        "path": str(path),
        "file_sha256": identity.sha256,
        "artifact_sha256": _lower_sha256(artifact_sha256, label=f"{label} direct artifact hash"),
    }
    if dict(raw) != expected:
        raise ValueError(f"freeze manifest {label} differs from direct drift input")


def _validate_freeze_binding(
    freeze: Mapping[str, Any],
    *,
    freeze_path: Path,
    identities: Mapping[str, _FileIdentity],
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
    natural_account_root: Path,
    natural_capture_root: Path,
    calibration_path: Path,
    calibration: Mapping[str, Any],
    archive_map_path: Path,
    archive_map: Mapping[str, Any],
    baseline_path: Path,
    baseline: Mapping[str, Any],
    stress_path: Path,
    stress: Mapping[str, Any],
    demo_rules_path: Path,
    demo_rules: Mapping[str, Any],
    clock_series_path: Path,
    clock_series: Mapping[str, Any],
    safety_freeze_id: str,
) -> dict[str, str]:
    window = freeze.get("window")
    runtime = freeze.get("runtime")
    training = freeze.get("v7_training")
    population = freeze.get("population")
    frozen_clock = freeze.get("clock")
    if not all(isinstance(value, Mapping) for value in (window, runtime, training, population, frozen_clock)):
        raise ValueError("freeze manifest lacks required drift binding sections")
    window = cast(Mapping[str, Any], window)
    runtime = cast(Mapping[str, Any], runtime)
    training = cast(Mapping[str, Any], training)
    population = cast(Mapping[str, Any], population)
    frozen_clock = cast(Mapping[str, Any], frozen_clock)
    if window.get("t0_ns") != t0_ns or window.get("t1_ns") != t1_ns:
        raise ValueError("freeze manifest natural window differs from drift window")
    account_ids = runtime.get("account_ids")
    roots = runtime.get("roots")
    if not isinstance(account_ids, Mapping) or account_ids.get("demo") != expected_account_id:
        raise ValueError("freeze manifest demo account id differs from drift account")
    demo_roots = roots.get("demo") if isinstance(roots, Mapping) else None
    if not isinstance(demo_roots, Mapping):
        raise ValueError("freeze manifest lacks canonical demo roots")
    if demo_roots.get("account") != str(natural_account_root) or demo_roots.get("capture") != str(natural_capture_root):
        raise ValueError("freeze manifest demo account/capture roots differ from drift inputs")
    refs = (
        (
            training.get("calibration"),
            "V7 calibration",
            calibration_path,
            identities["v7_calibration_receipt"],
            calibration.get("artifact_sha256"),
        ),
        (
            training.get("archive_map"),
            "V7 archive map",
            archive_map_path,
            identities["v7_archive_source_map"],
            archive_map.get("artifact_sha256"),
        ),
        (
            training.get("baseline_config"),
            "baseline config",
            baseline_path,
            identities["baseline_twin_config"],
            baseline.get("artifact_sha256"),
        ),
        (
            training.get("stress_config"),
            "stress config",
            stress_path,
            identities["stress_twin_config"],
            stress.get("artifact_sha256"),
        ),
        (
            population.get("demo_rules"),
            "demo rules",
            demo_rules_path,
            identities["natural_demo_rules"],
            demo_rules.get("artifact_sha256"),
        ),
    )
    for raw, label, path, identity, artifact_hash in refs:
        _require_frozen_artifact_ref(
            raw,
            label=label,
            path=path,
            identity=identity,
            artifact_sha256=artifact_hash,
        )
    series_freeze = clock_series.get("freeze")
    series_window = clock_series.get("window")
    if not isinstance(series_freeze, Mapping) or not isinstance(series_window, Mapping):
        raise ValueError("clock-offset series lacks freeze/window bindings")
    if (
        series_freeze.get("freeze_id") != freeze.get("freeze_id")
        or series_freeze.get("artifact_sha256") != freeze.get("artifact_sha256")
        or series_freeze.get("initial_clock_receipt") != frozen_clock.get("receipt")
        or series_window.get("t0_ns") != t0_ns
        or series_window.get("t1_ns") != t1_ns
    ):
        raise ValueError("clock-offset series differs from the natural cutover freeze")
    series_identity = identities["natural_clock_offset_series"]
    if str(clock_series_path) != series_identity.path:
        raise ValueError("clock-offset series path differs from its frozen identity")
    freeze_id = str(freeze.get("freeze_id") or "")
    if not freeze_id or freeze_id != safety_freeze_id:
        raise ValueError("safety manifest freeze_id differs from natural cutover freeze")
    return {
        "path": str(freeze_path),
        "freeze_id": freeze_id,
        "file_sha256": identities["natural_cutover_freeze_manifest"].sha256,
        "artifact_sha256": _lower_sha256(freeze.get("artifact_sha256"), label="natural cutover freeze artifact hash"),
    }


def _scope_natural_journal(
    events: Sequence[AccountEvent], *, scope: _NaturalScope
) -> tuple[list[AccountEvent], frozenset[str], dict[str, Any]]:
    natural_batches = scope.natural_batch_ids
    safety_batches = scope.safety_batch_ids
    expected_batches = natural_batches | safety_batches
    risk_by_batch: dict[str, AccountEvent] = {}
    for event in events:
        if event.event_type != AccountEventType.RISK_DECISION.value:
            continue
        if event.correlation_id in risk_by_batch:
            raise ValueError(f"natural journal repeats risk batch {event.correlation_id!r}")
        risk_by_batch[event.correlation_id] = event
    if frozenset(risk_by_batch) != expected_batches:
        extras = sorted(frozenset(risk_by_batch) - expected_batches)
        missing = sorted(expected_batches - frozenset(risk_by_batch))
        raise ValueError(
            "natural journal RISK_DECISION scope differs from exact natural+safety "
            f"batch sets: extra={extras[:5]!r}, missing={missing[:5]!r}"
        )
    for batch_id, event in risk_by_batch.items():
        is_safety = batch_id in safety_batches
        captured = scope.safety_requests[batch_id] if is_safety else scope.natural_requests[batch_id]
        if (is_safety and event.wall_ts_ns < scope.t1_ns) or (
            not is_safety and not (scope.t0_ns <= event.wall_ts_ns < scope.t1_ns)
        ):
            raise ValueError("natural/safety risk batch was planned outside its exact window")
        payload = event.payload
        strict = payload.get("strict_risk_reduction_required")
        if type(strict) is not bool or type(payload.get("accepted")) is not bool:
            raise ValueError("scoped risk event lacks exact decision fields")
        if is_safety and strict is not True:
            raise ValueError("post-window safety risk batch was not strict risk reduction")
        expected_hash = _expected_scoped_risk_hash(
            captured,
            strict_risk_reduction_required=strict,
        )
        if payload.get("batch_id") != batch_id or payload.get("request_hash") != expected_hash:
            raise ValueError("scoped risk event does not match its captured target request")

    commands: dict[str, AccountEvent] = {}
    natural_command_ids: set[str] = set()
    safety_command_ids: set[str] = set()
    for event in events:
        if event.event_type != AccountEventType.ORDER_COMMAND.value:
            continue
        command_id = str(event.payload.get("command_id") or "")
        if not command_id or command_id in commands:
            raise ValueError("natural journal has a missing or duplicate command identity")
        batch_id = event.correlation_id
        if batch_id not in expected_batches:
            raise ValueError("natural journal has an order command outside natural+safety scope")
        if batch_id in natural_batches:
            if not (scope.t0_ns <= event.wall_ts_ns < scope.t1_ns):
                raise ValueError("natural order command was planned outside [T0,T1)")
            natural_command_ids.add(command_id)
        else:
            if event.wall_ts_ns < scope.t1_ns:
                raise ValueError("post-window safety command predates T1")
            safety_command_ids.add(command_id)
        commands[command_id] = event

    command_event_types = {
        AccountEventType.ACK.value,
        AccountEventType.ACK_OBSERVATION.value,
        AccountEventType.FILL.value,
        AccountEventType.ORDER_STATUS.value,
    }
    for event in events:
        if event.event_type not in command_event_types:
            continue
        command_id = str(event.payload.get("command_id") or "")
        if command_id not in commands:
            raise ValueError("natural journal has ACK/FILL/STATUS evidence outside command scope")

    natural_market_keys: set[str] = set()
    scoped_events: list[AccountEvent] = []
    natural_batch_event_types = {
        AccountEventType.MARKET_INPUT_REF.value,
        AccountEventType.TARGET.value,
        AccountEventType.ORDER_COMMAND.value,
    }
    for event in events:
        if event.correlation_id in natural_batches and event.event_type in natural_batch_event_types:
            if not (scope.t0_ns <= event.wall_ts_ns < scope.t1_ns):
                raise ValueError("natural plan evidence falls outside [T0,T1)")
            if event.event_type == AccountEventType.MARKET_INPUT_REF.value:
                input_key = str(event.payload.get("input_key") or "")
                if not input_key:
                    raise ValueError("natural market-input reference lacks input_key")
                natural_market_keys.add(input_key)
            scoped_events.append(event)
            continue
        if (
            event.event_type in command_event_types
            and str(event.payload.get("command_id") or "") in natural_command_ids
        ):
            scoped_events.append(event)
    return (
        scoped_events,
        frozenset(natural_market_keys),
        {
            "natural_risk_batch_count": len(natural_batches),
            "safety_risk_batch_count": len(safety_batches),
            "natural_command_count": len(natural_command_ids),
            "safety_command_count": len(safety_command_ids),
            "safety_command_ids": sorted(safety_command_ids),
            "safety_excluded_from_drift_metrics": True,
            "risk_batch_set_exact": True,
        },
    )


class _CaptureAccumulator:
    """Disk-backed exact duplicate and feed-distribution accumulator."""

    def __init__(self) -> None:
        temporary = tempfile.NamedTemporaryFile(prefix="execution-twin-drift-", suffix=".sqlite3", delete=False)
        self.path = Path(temporary.name)
        temporary.close()
        self._connection: sqlite3.Connection | None = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("CREATE TABLE record_ids (record_id TEXT PRIMARY KEY) WITHOUT ROWID")
        self.connection.execute(
            "CREATE TABLE feed (value INTEGER NOT NULL, interval_low INTEGER NOT NULL, interval_high INTEGER NOT NULL)"
        )
        self._ids: list[tuple[str]] = []
        self._feed: list[tuple[int, int, int]] = []

    def __enter__(self) -> _CaptureAccumulator:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("capture accumulator is closed")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self.path.unlink(missing_ok=True)

    def add(
        self,
        *,
        record_id: str,
        adjusted_feed_ns: int | None,
        feed_interval_low_ns: int | None = None,
        feed_interval_high_ns: int | None = None,
    ) -> None:
        values = (
            adjusted_feed_ns,
            feed_interval_low_ns,
            feed_interval_high_ns,
        )
        if adjusted_feed_ns is None:
            if any(value is not None for value in values):
                raise ValueError("feed sensitivity interval exists without a feed row")
        elif (
            feed_interval_low_ns is None
            or feed_interval_high_ns is None
            or feed_interval_low_ns > adjusted_feed_ns
            or adjusted_feed_ns > feed_interval_high_ns
        ):
            raise ValueError("adjusted feed latency sensitivity interval is invalid")
        if any(value is not None and not -(2**63) <= value < 2**63 for value in values):
            raise ValueError("adjusted feed latency exceeds signed 64-bit range")
        self._ids.append((record_id,))
        if adjusted_feed_ns is not None:
            self._feed.append(
                (
                    adjusted_feed_ns,
                    cast(int, feed_interval_low_ns),
                    cast(int, feed_interval_high_ns),
                )
            )
        if len(self._ids) >= 10_000:
            self.flush()

    def flush(self) -> None:
        try:
            with self.connection:
                self.connection.executemany("INSERT INTO record_ids(record_id) VALUES (?)", self._ids)
                self.connection.executemany(
                    "INSERT INTO feed(value, interval_low, interval_high) VALUES (?, ?, ?)",
                    self._feed,
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate raw capture record id") from exc
        self._ids.clear()
        self._feed.clear()

    def feed_summary(
        self,
    ) -> tuple[
        dict[str, float | int | None],
        float,
        dict[str, Any],
    ]:
        self.flush()
        self.connection.execute("CREATE INDEX feed_value ON feed(value)")
        row = self.connection.execute(
            "SELECT COUNT(*), MIN(value), MAX(value), AVG(value), SUM(CASE WHEN value >= 0 THEN 1 ELSE 0 END) FROM feed"
        ).fetchone()
        count = int(row[0])
        if count == 0:
            empty = _distribution(())
            return (
                empty,
                0.0,
                {
                    "latency_if_correction_at_interval_high": empty,
                    "latency_if_correction_at_interval_low": empty,
                    "definitely_nonnegative_ratio": 0.0,
                    "possibly_nonnegative_ratio": 0.0,
                },
            )

        def quantile(column: str, probability: float) -> float:
            if column not in {"value", "interval_low", "interval_high"}:
                raise RuntimeError("unsupported feed summary column")
            position = (count - 1) * probability
            lower = math.floor(position)
            upper = math.ceil(position)
            lower_value = float(
                self.connection.execute(
                    f"SELECT {column} FROM feed ORDER BY {column} LIMIT 1 OFFSET ?",  # noqa: S608
                    (lower,),
                ).fetchone()[0]
            )
            if lower == upper:
                return lower_value
            upper_value = float(
                self.connection.execute(
                    f"SELECT {column} FROM feed ORDER BY {column} LIMIT 1 OFFSET ?",  # noqa: S608
                    (upper,),
                ).fetchone()[0]
            )
            weight = position - lower
            return lower_value * (1.0 - weight) + upper_value * weight

        def distribution(column: str) -> dict[str, float | int | None]:
            column_row = self.connection.execute(
                f"SELECT COUNT(*), MIN({column}), MAX({column}), AVG({column}) FROM feed"  # noqa: S608
            ).fetchone()
            return {
                "count": count,
                "min": float(column_row[1]),
                "p50": quantile(column, 0.50),
                "p75": quantile(column, 0.75),
                "p95": quantile(column, 0.95),
                "p99": quantile(column, 0.99),
                "max": float(column_row[2]),
                "mean": float(column_row[3]),
            }

        point = distribution("value")
        interval_low = distribution("interval_low")
        interval_high = distribution("interval_high")
        sensitivity_counts = self.connection.execute(
            "SELECT "
            "SUM(CASE WHEN interval_low >= 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN interval_high >= 0 THEN 1 ELSE 0 END) FROM feed"
        ).fetchone()
        return (
            point,
            int(row[4]) / count,
            {
                # A high local-minus-exchange correction produces the low latency
                # endpoint and vice versa.
                "latency_if_correction_at_interval_high": interval_low,
                "latency_if_correction_at_interval_low": interval_high,
                "definitely_nonnegative_ratio": int(sensitivity_counts[0]) / count,
                "possibly_nonnegative_ratio": int(sensitivity_counts[1]) / count,
            },
        )


def _parse_capture(
    identities: Mapping[str, _FileIdentity],
    *,
    snapshots: Mapping[str, StableFileSnapshot],
    prefix: str,
    clock_interpolator: ClockOffsetInterpolator,
    context_record_ids: frozenset[str],
    t0_ns: int,
    t1_ns: int,
) -> tuple[
    list[dict[str, Any]],
    dict[str, float | int | None],
    float,
    dict[str, Any],
]:
    contexts: list[dict[str, Any]] = []
    correction_count = 0
    correction_min_ns: int | None = None
    correction_max_ns: int | None = None
    max_uncertainty_ns = 0
    max_bracket_gap_ns = 0
    exact_sample_rows = 0
    sample_indexes_used: set[int] = set()
    bracket_pair_counts: dict[tuple[int, int], int] = {}
    labels = sorted(label for label in identities if label.startswith(prefix + "/"))
    if not labels:
        raise ValueError(f"no frozen capture files for {prefix}")
    with _CaptureAccumulator() as accumulator:
        for label in labels:
            snapshot = snapshots[label]
            if snapshot.sha256 != identities[label].sha256:
                raise RuntimeError(f"frozen capture snapshot differs from {label} identity")
            for line_number, raw_line in enumerate(snapshot.data.splitlines(keepends=True), start=1):
                if not raw_line.endswith(b"\n"):
                    raise ValueError(f"market capture has a partial final line: {label}")
                row = _strict_json(raw_line[:-1], label=f"{label}:{line_number}")
                if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
                    raise ValueError(f"unknown raw market-capture schema: {label}:{line_number}")
                local_ns = row.get("local_receive_ts_ns")
                if type(local_ns) is not int or local_ns <= 0:
                    raise ValueError(f"raw capture row lacks receive time: {label}:{line_number}")
                record_id = str(row.get("record_id") or "")
                if record_id != capture_record_id(row):
                    raise ValueError(f"raw capture record id mismatch: {label}:{line_number}")
                kind = str(row.get("kind") or "")
                adjusted: int | None = None
                interval_low: int | None = None
                interval_high: int | None = None
                if kind in {"orderbook_snapshot", "orderbook_delta"}:
                    exchange_ns = row.get("exchange_engine_ts_ns") or row.get("exchange_system_ts_ns")
                    if type(exchange_ns) is not int or exchange_ns <= 0:
                        raise ValueError(f"order-book row lacks exchange time: {label}:{line_number}")
                    if t0_ns <= local_ns < t1_ns:
                        estimate = clock_interpolator.estimate(local_ns)
                        adjusted = local_ns - exchange_ns - estimate.local_minus_exchange_ns
                elif kind == "public_trade":
                    exchange_ns = row.get("exchange_trade_ts_ns") or row.get("exchange_system_ts_ns")
                    if type(exchange_ns) is not int or exchange_ns <= 0:
                        raise ValueError(f"public-trade row lacks exchange time: {label}:{line_number}")
                    if t0_ns <= local_ns < t1_ns:
                        estimate = clock_interpolator.estimate(local_ns)
                        adjusted = local_ns - exchange_ns - estimate.local_minus_exchange_ns
                elif kind == "book_context":
                    if row.get("context_kind") == "account_service_decision" and record_id in context_record_ids:
                        if not (t0_ns <= local_ns < t1_ns):
                            raise ValueError("natural decision context falls outside [T0,T1)")
                        contexts.append(row)
                else:
                    raise ValueError(f"unsupported raw capture kind {kind!r}: {label}:{line_number}")
                if adjusted is not None:
                    uncertainty_ns = estimate.estimated_uncertainty_ns
                    interval_low = adjusted - uncertainty_ns
                    interval_high = adjusted + uncertainty_ns
                    correction_count += 1
                    correction_min_ns = (
                        estimate.local_minus_exchange_ns
                        if correction_min_ns is None
                        else min(
                            correction_min_ns,
                            estimate.local_minus_exchange_ns,
                        )
                    )
                    correction_max_ns = (
                        estimate.local_minus_exchange_ns
                        if correction_max_ns is None
                        else max(
                            correction_max_ns,
                            estimate.local_minus_exchange_ns,
                        )
                    )
                    max_uncertainty_ns = max(max_uncertainty_ns, estimate.estimated_uncertainty_ns)
                    max_bracket_gap_ns = max(max_bracket_gap_ns, estimate.bracket_gap_ns)
                    exact_sample_rows += int(estimate.exact_sample)
                    sample_indexes_used.update(
                        (
                            estimate.left_sample_index,
                            estimate.right_sample_index,
                        )
                    )
                    pair = (
                        estimate.left_sample_index,
                        estimate.right_sample_index,
                    )
                    bracket_pair_counts[pair] = bracket_pair_counts.get(pair, 0) + 1
                accumulator.add(
                    record_id=record_id,
                    adjusted_feed_ns=adjusted,
                    feed_interval_low_ns=interval_low,
                    feed_interval_high_ns=interval_high,
                )
        feed_distribution, nonnegative_ratio, sensitivity = accumulator.feed_summary()
    clock_application = {
        "corrected_feed_row_count": correction_count,
        "timestamp_field": "local_receive_ts_ns",
        "interpolation_method": INTERPOLATION_METHOD,
        "uncertainty_method": UNCERTAINTY_METHOD,
        "uncertainty_is_hard_bound": False,
        "local_minus_exchange_ns_min": correction_min_ns,
        "local_minus_exchange_ns_max": correction_max_ns,
        "max_estimated_uncertainty_ns": max_uncertainty_ns,
        "max_bracket_gap_used_ns": max_bracket_gap_ns,
        "exact_sample_row_count": exact_sample_rows,
        "sample_indexes_used": sorted(sample_indexes_used),
        "bracket_pair_counts": [
            {
                "left_sample_index": left,
                "right_sample_index": right,
                "feed_row_count": count,
            }
            for (left, right), count in sorted(bracket_pair_counts.items())
        ],
        "feed_latency_sensitivity_ns": sensitivity,
    }
    return contexts, feed_distribution, nonnegative_ratio, clock_application


def _number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    output = float(value)
    if not math.isfinite(output):
        raise ValueError(f"{label} contains NaN or infinity")
    return output


def _integer(value: Any, *, label: str, positive: bool = False) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one observation")
    ordered = sorted(_number(value, label="quantile observation") for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = [_number(value, label="distribution observation") for value in values]
    if not finite:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p75": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(finite),
        "min": min(finite),
        "p50": _quantile(finite, 0.50),
        "p75": _quantile(finite, 0.75),
        "p95": _quantile(finite, 0.95),
        "p99": _quantile(finite, 0.99),
        "max": max(finite),
        "mean": statistics.fmean(finite),
    }


def _levels(row: Mapping[str, Any], key: str) -> tuple[BookLevel, ...]:
    raw = row.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"decision context has no {key}")
    output: list[BookLevel] = []
    for index, level in enumerate(raw):
        if not isinstance(level, list) or len(level) < 2:
            raise ValueError(f"decision context {key}[{index}] is malformed")
        price = _number(level[0], label=f"decision context {key}[{index}] price")
        qty = _number(level[1], label=f"decision context {key}[{index}] qty")
        if price <= 0.0 or qty <= 0.0:
            raise ValueError(f"decision context {key}[{index}] is non-positive")
        output.append(BookLevel(price, qty))
    expected = sorted(output, key=lambda level: level.price, reverse=key == "bids")
    if output != expected:
        raise ValueError(f"decision context {key} are not in canonical book order")
    return tuple(output)


def _same_float(left: Any, right: float, *, label: str) -> None:
    observed = _number(left, label=label)
    if not math.isclose(observed, right, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"{label} does not match the captured decision book")


def _market_and_book(
    event: AccountEvent,
    context: Mapping[str, Any],
) -> tuple[MarketInputRef, L2BookSnapshot]:
    payload = event.payload
    symbol = event.symbol.upper()
    if (
        context.get("kind") != "book_context"
        or context.get("context_kind") != "account_service_decision"
        or str(context.get("reference_key") or "") != event.correlation_id
        or str(context.get("symbol") or "").upper() != symbol
        or context.get("sequence_gap") is not False
    ):
        raise ValueError("market-input reference is not bound to one healthy decision context")
    bids = _levels(context, "bids")
    asks = _levels(context, "asks")
    if bids[0].price > asks[0].price:
        raise ValueError("captured decision book is crossed")
    exchange_ns = context.get("exchange_engine_ts_ns") or context.get("exchange_system_ts_ns")
    exchange_ns = _integer(exchange_ns, label="decision-book exchange time", positive=True)
    book_local_ns = _integer(
        context.get("book_local_receive_ts_ns"),
        label="decision-book local receive time",
        positive=True,
    )
    sequence = _integer(context.get("cross_sequence"), label="decision-book sequence", positive=True)
    midpoint = (bids[0].price + asks[0].price) / 2.0
    if str(payload.get("input_key") or "") != str(context.get("record_id") or ""):
        raise ValueError("market-input reference changed its decision-context identity")
    if str(payload.get("batch_id") or event.correlation_id) != event.correlation_id:
        raise ValueError("market-input reference changed its batch identity")
    if _integer(payload.get("exchange_ts_ns"), label="market exchange time") != exchange_ns:
        raise ValueError("market-input exchange time differs from decision context")
    if _integer(payload.get("local_receive_ts_ns"), label="market local time") != book_local_ns:
        raise ValueError("market-input local time differs from decision context")
    if _integer(payload.get("book_sequence"), label="market book sequence") != sequence:
        raise ValueError("market-input sequence differs from decision context")
    _same_float(payload.get("reference_price"), midpoint, label="market reference price")
    _same_float(payload.get("bid_price"), bids[0].price, label="market bid")
    _same_float(payload.get("ask_price"), asks[0].price, label="market ask")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("market-input metadata is missing")
    if (
        str(metadata.get("capture_record_id") or "") != context.get("record_id")
        or metadata.get("sequence_gap") is not False
        or _integer(metadata.get("update_id"), label="market update id")
        != _integer(context.get("update_id"), label="decision-context update id")
    ):
        raise ValueError("market-input metadata differs from decision context")
    market = MarketInputRef(
        input_key=str(context["record_id"]),
        symbol=symbol,
        exchange_ts_ns=exchange_ns,
        local_receive_ts_ns=book_local_ns,
        reference_price=midpoint,
        bid_price=bids[0].price,
        ask_price=asks[0].price,
        book_sequence=sequence,
        source=str(payload.get("source") or ""),
        metadata=dict(metadata),
    )
    return market, L2BookSnapshot(
        symbol=symbol,
        sequence=sequence,
        previous_sequence=(
            int(metadata["previous_sequence"]) if metadata.get("previous_sequence") is not None else None
        ),
        exchange_ts_ns=exchange_ns,
        local_receive_ts_ns=book_local_ns,
        bids=bids,
        asks=asks,
        sequence_gap=False,
        clock_offset_estimate_ns=(
            int(metadata["clock_offset_estimate_ns"]) if metadata.get("clock_offset_estimate_ns") is not None else None
        ),
    )


def _command(event: AccountEvent) -> OrderCommand:
    payload = event.payload
    command = OrderCommand(
        command_id=str(payload.get("command_id") or ""),
        batch_id=str(payload.get("batch_id") or ""),
        symbol=event.symbol.upper(),
        side=str(payload.get("side") or ""),
        qty=_number(payload.get("qty"), label="command qty"),
        signed_qty=_number(payload.get("signed_qty"), label="command signed qty"),
        reduce_only=bool(payload.get("reduce_only")),
        reference_price=_number(payload.get("reference_price"), label="command reference price"),
        target_signed_qty=_number(payload.get("target_signed_qty"), label="command target signed qty"),
        chunk_index=_integer(payload.get("chunk_index"), label="command chunk index"),
        chunk_count=_integer(payload.get("chunk_count"), label="command chunk count"),
        leverage=_number(payload.get("leverage"), label="command leverage"),
        created_ts_ns=_integer(
            payload.get("created_ts_ns"),
            label="command created time",
            positive=True,
        ),
    )
    if (
        not command.command_id
        or command.batch_id != event.correlation_id
        or command.qty <= 0.0
        or abs(command.signed_qty) <= 0.0
        or command.reference_price <= 0.0
        or command.chunk_index < 0
        or command.chunk_count <= 0
        or command.chunk_index >= command.chunk_count
        or command.side not in {"Buy", "Sell"}
        or (command.signed_qty > 0.0) != (command.side == "Buy")
        or command.created_ts_ns != event.wall_ts_ns
    ):
        raise ValueError(f"invalid order command {command.command_id!r}")
    return command


def _link_commands(
    events: Sequence[AccountEvent],
    capture_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[_LinkedCommand], dict[str, Any]]:
    market_events = [event for event in events if event.event_type == AccountEventType.MARKET_INPUT_REF.value]
    command_events = [event for event in events if event.event_type == AccountEventType.ORDER_COMMAND.value]
    if not command_events:
        raise ValueError("natural demo journal contains no order commands")
    contexts = {
        str(row.get("record_id") or ""): row
        for row in capture_rows
        if row.get("kind") == "book_context" and row.get("context_kind") == "account_service_decision"
    }
    if len(contexts) != sum(
        row.get("kind") == "book_context" and row.get("context_kind") == "account_service_decision"
        for row in capture_rows
    ):
        raise ValueError("duplicate account decision-context identity")
    market_by_key: dict[tuple[str, str], list[AccountEvent]] = {}
    referenced_context_ids: set[str] = set()
    for event in market_events:
        key = (event.correlation_id, event.symbol.upper())
        market_by_key.setdefault(key, []).append(event)
        context_id = str(event.payload.get("input_key") or "")
        if context_id in referenced_context_ids:
            raise ValueError("a captured decision context was reused by market-input events")
        if context_id not in contexts:
            raise ValueError("market-input event has a missing/unlinked decision context")
        referenced_context_ids.add(context_id)
        _market_and_book(event, contexts[context_id])
    if referenced_context_ids != set(contexts):
        extra = sorted(set(contexts) - referenced_context_ids)
        raise ValueError(f"raw capture has extra/unlinked decision contexts: {extra[:5]}")
    for key, rows in market_by_key.items():
        if len(rows) != 1:
            raise ValueError(f"batch/symbol {key!r} has extra market-input references")

    targets_by_batch_symbol: dict[tuple[str, str], set[str]] = {}
    for event in events:
        if event.event_type != AccountEventType.TARGET.value:
            continue
        targets_by_batch_symbol.setdefault((event.correlation_id, event.symbol.upper()), set()).add(
            str(event.sleeve or event.payload.get("sleeve") or "")
        )

    seen_command_ids: set[str] = set()
    seen_command_market_keys: set[tuple[str, str]] = set()
    linked: list[_LinkedCommand] = []
    mapping_rows: list[dict[str, Any]] = []
    for event in command_events:
        command = _command(event)
        if command.command_id in seen_command_ids:
            raise ValueError(f"duplicate command id {command.command_id!r}")
        seen_command_ids.add(command.command_id)
        key = (event.correlation_id, event.symbol.upper())
        if key in seen_command_market_keys:
            raise ValueError("multiple commands reuse one decision context; one-to-one linkage failed")
        seen_command_market_keys.add(key)
        candidates = market_by_key.get(key, [])
        if len(candidates) != 1:
            raise ValueError(f"command {command.command_id!r} lacks one market-input reference")
        market_event = candidates[0]
        context_id = str(market_event.payload.get("input_key") or "")
        context = contexts[context_id]
        market, book = _market_and_book(market_event, context)
        _same_float(
            command.reference_price,
            market.reference_price,
            label=f"command {command.command_id} reference price",
        )
        sleeves = tuple(sorted(sleeve for sleeve in targets_by_batch_symbol.get(key, set()) if sleeve))
        if not sleeves:
            raise ValueError(f"command {command.command_id!r} has no target-sleeve lineage")
        linked.append(_LinkedCommand(command, event, market_event, market, context, book, sleeves))
        mapping_rows.append(
            {
                "command_id": command.command_id,
                "batch_id": command.batch_id,
                "symbol": command.symbol,
                "sleeves": list(sleeves),
                "market_input_event_id": market_event.event_id,
                "capture_record_id": context_id,
                "book_sequence": book.sequence,
            }
        )
    return linked, {
        "command_count": len(command_events),
        "market_input_count": len(market_events),
        "decision_context_count": len(contexts),
        "linked_command_count": len(linked),
        "unused_but_context_linked_market_input_count": len(market_events) - len(linked),
        "mappings": mapping_rows,
    }


def _visible_book_vwap(book: L2BookSnapshot, *, signed_qty: float, fill_qty: float) -> float | None:
    if signed_qty == 0.0 or fill_qty <= 0.0:
        return None
    levels = book.asks if signed_qty > 0.0 else book.bids
    remaining = fill_qty
    notional = 0.0
    executed = 0.0
    for level in levels:
        quantity = min(remaining, level.qty)
        remaining -= quantity
        executed += quantity
        notional += quantity * level.price
        if remaining <= 1e-12:
            break
    if remaining > 1e-9 or executed <= 0.0:
        return None
    return notional / executed


def _training_threshold(receipt: Mapping[str, Any], section: str, metric: str, quantile: str) -> float:
    container = receipt.get(section)
    if not isinstance(container, Mapping):
        raise ValueError(f"V7 receipt lacks {section}")
    distribution = container.get(metric)
    if not isinstance(distribution, Mapping):
        raise ValueError(f"V7 receipt lacks {section}.{metric}")
    value = _number(distribution.get(quantile), label=f"V7 {section}.{metric}.{quantile}")
    return value


def _envelope_gate(holdout: Mapping[str, Any], *, p50_limit: float, p95_limit: float) -> bool:
    p50 = holdout.get("p50")
    p95 = holdout.get("p95")
    return (
        isinstance(p50, (int, float))
        and not isinstance(p50, bool)
        and math.isfinite(float(p50))
        and isinstance(p95, (int, float))
        and not isinstance(p95, bool)
        and math.isfinite(float(p95))
        and float(p50) <= p50_limit
        and float(p95) <= p95_limit
    )


def _modeled_terminal_summary(
    observations: Sequence[ExecutionObservation],
) -> tuple[str, float, list[ExecutionObservation]]:
    fills = [
        observation
        for observation in observations
        if observation.observation_type == ExecutionObservationType.FILL
    ]
    filled_qty = math.fsum(abs(observation.signed_qty) for observation in fills)
    statuses = [
        observation
        for observation in observations
        if observation.observation_type == ExecutionObservationType.ORDER_STATUS
    ]
    rejected = any(
        observation.observation_type == ExecutionObservationType.ACK
        and observation.accepted is False
        for observation in observations
    )
    if statuses:
        terminal_status = str(statuses[-1].status).lower()
    elif rejected:
        terminal_status = "rejected"
    else:
        terminal_status = "nonterminal"
    return terminal_status, filled_qty, fills


def _modeled_vwap_for_quantity(
    fills: Sequence[ExecutionObservation],
    *,
    quantity: float,
) -> float | None:
    """Price the deterministic fill prefix matching the actual filled quantity."""

    if quantity <= 0.0:
        return None
    remaining = quantity
    notional = 0.0
    used = 0.0
    for fill in fills:
        available = abs(fill.signed_qty)
        take = min(remaining, available)
        notional += take * fill.price
        used += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if remaining > 1e-9 or used <= 0.0:
        return None
    return notional / used


def _evaluate_holdout(
    *,
    events: Sequence[AccountEvent],
    capture_rows: Sequence[Mapping[str, Any]],
    feed_distribution: Mapping[str, float | int | None],
    feed_nonnegative_ratio: float,
    instrument_rules: Mapping[str, InstrumentRules],
    calibration_receipt: Mapping[str, Any],
    baseline_config: ExecutionTwinConfig,
    stress_config: ExecutionTwinConfig,
) -> dict[str, Any]:
    linked, linkage = _link_commands(events, capture_rows)
    by_command = {item.command.command_id: item for item in linked}
    if len(by_command) != len(linked):
        raise ValueError("natural holdout repeats a command identity")

    timing_ack_by_command: dict[str, AccountEvent] = {}
    semantic_acks: dict[str, list[AccountEvent]] = {}
    fills_by_command: dict[str, list[AccountEvent]] = {}
    statuses_by_command: dict[str, list[AccountEvent]] = {}
    for event in events:
        if event.event_type == AccountEventType.ACK.value:
            semantic_acks.setdefault(str(event.payload.get("command_id") or ""), []).append(event)
        if event.event_type in {
            AccountEventType.ACK.value,
            AccountEventType.ACK_OBSERVATION.value,
        }:
            if event.payload.get("accepted") is not True:
                continue
            metadata = event.payload.get("metadata")
            if not isinstance(metadata, Mapping) or metadata.get("idempotent_existing_order") is True:
                continue
            command_id = str(event.payload.get("command_id") or "")
            send_ns = metadata.get("local_socket_send_ts_ns")
            ack_ns = event.payload.get("local_ack_ts_ns")
            if not command_id or type(send_ns) is not int or send_ns <= 0:
                continue
            if type(ack_ns) is not int or ack_ns <= 0:
                raise ValueError(f"timing ACK for {command_id!r} lacks a local ACK time")
            current = timing_ack_by_command.get(command_id)
            if current is None or (ack_ns, event.sequence) < (
                int(current.payload["local_ack_ts_ns"]),
                current.sequence,
            ):
                timing_ack_by_command[command_id] = event
        elif event.event_type == AccountEventType.FILL.value:
            fills_by_command.setdefault(str(event.payload.get("command_id") or ""), []).append(event)
        elif event.event_type == AccountEventType.ORDER_STATUS.value:
            statuses_by_command.setdefault(str(event.payload.get("command_id") or ""), []).append(event)

    known_command_ids = set(by_command)
    for label, mapping in (
        ("ACK", semantic_acks),
        ("timing ACK", timing_ack_by_command),
        ("fill", fills_by_command),
        ("status", statuses_by_command),
    ):
        unknown = sorted(set(mapping) - known_command_ids - {""})
        if unknown:
            raise ValueError(f"natural journal has {label} rows for unknown commands: {unknown[:5]}")

    request_ack_rtt_ns: list[float] = []
    timing_rows: list[dict[str, Any]] = []
    for command_id, event in sorted(timing_ack_by_command.items()):
        metadata = cast(Mapping[str, Any], event.payload["metadata"])
        send_ns = int(metadata["local_socket_send_ts_ns"])
        ack_ns = int(event.payload["local_ack_ts_ns"])
        rtt = float(ack_ns - send_ns)
        request_ack_rtt_ns.append(rtt)
        timing_rows.append(
            {
                "command_id": command_id,
                "ack_event_id": event.event_id,
                "local_socket_send_ts_ns": send_ns,
                "local_ack_ts_ns": ack_ns,
                "request_ack_round_trip_ns": rtt,
            }
        )

    classifications: dict[str, list[str]] = {
        "filled": [],
        "rejected": [],
        "cancelled": [],
        "zero_fill": [],
        "terminal_incomplete": [],
        "multifill": [],
        "nonterminal": [],
    }
    positive_spacing_rows: list[dict[str, Any]] = []
    equal_timestamp_fill_pairs = 0
    residual_rows: list[dict[str, Any]] = []
    stress_rows: list[dict[str, Any]] = []
    model_scope_issues: list[dict[str, str]] = []
    command_rows: list[dict[str, Any]] = []
    filled_commands_by_sleeve: dict[str, int] = {}
    commands_by_sleeve: dict[str, int] = {}
    filled_commands_by_symbol: dict[str, int] = {}
    commands_by_symbol: dict[str, int] = {}

    for item in linked:
        command = item.command
        command_id = command.command_id
        rules = instrument_rules.get(command.symbol)
        if rules is None:
            raise ValueError(f"demo rules do not cover command symbol {command.symbol}")
        if rules.tick_size <= 0.0:
            raise ValueError(f"demo rules have no positive tick for {command.symbol}")
        commands_by_symbol[command.symbol] = commands_by_symbol.get(command.symbol, 0) + 1
        for sleeve in item.sleeves:
            commands_by_sleeve[sleeve] = commands_by_sleeve.get(sleeve, 0) + 1

        acks = sorted(semantic_acks.get(command_id, ()), key=lambda event: event.sequence)
        accepted = any(event.payload.get("accepted") is True for event in acks)
        rejected = any(event.payload.get("accepted") is False for event in acks)
        if accepted and rejected:
            raise ValueError(f"command {command_id!r} has contradictory semantic ACKs")
        if rejected:
            classifications["rejected"].append(command_id)
        if not acks:
            model_scope_issues.append({"command_id": command_id, "reason": "missing_semantic_ack"})

        command_fills = sorted(
            fills_by_command.get(command_id, ()),
            key=lambda event: (
                _integer(event.payload.get("exchange_ts_ns"), label="fill exchange time"),
                event.sequence,
            ),
        )
        fill_qty = 0.0
        fill_notional = 0.0
        prior_fill: AccountEvent | None = None
        for fill in command_fills:
            signed_qty = _number(fill.payload.get("signed_qty"), label="fill signed qty")
            price = _number(fill.payload.get("price"), label="fill price")
            if signed_qty == 0.0 or price <= 0.0:
                raise ValueError(f"command {command_id!r} has a zero/invalid fill")
            if (signed_qty > 0.0) != (command.signed_qty > 0.0):
                raise ValueError(f"command {command_id!r} has a fill with the wrong side")
            quantity = abs(signed_qty)
            fill_qty += quantity
            fill_notional += quantity * price
            if prior_fill is not None:
                previous_ns = _integer(
                    prior_fill.payload.get("exchange_ts_ns"),
                    label="prior fill exchange time",
                )
                current_ns = _integer(fill.payload.get("exchange_ts_ns"), label="fill exchange time")
                if current_ns > previous_ns:
                    positive_spacing_rows.append(
                        {
                            "command_id": command_id,
                            "previous_execution_id": str(prior_fill.payload.get("execution_id") or ""),
                            "execution_id": str(fill.payload.get("execution_id") or ""),
                            "previous_exchange_ts_ns": previous_ns,
                            "exchange_ts_ns": current_ns,
                            "spacing_ns": current_ns - previous_ns,
                        }
                    )
                elif current_ns == previous_ns:
                    equal_timestamp_fill_pairs += 1
                else:  # sorting makes this defensive rather than reachable
                    raise ValueError(f"command {command_id!r} fill time regressed")
            prior_fill = fill
        if fill_qty > command.qty + 1e-12:
            raise ValueError(f"command {command_id!r} overfilled its requested quantity")
        if command_fills:
            classifications["filled"].append(command_id)
            filled_commands_by_symbol[command.symbol] = filled_commands_by_symbol.get(command.symbol, 0) + 1
            for sleeve in item.sleeves:
                filled_commands_by_sleeve[sleeve] = filled_commands_by_sleeve.get(sleeve, 0) + 1
        else:
            classifications["zero_fill"].append(command_id)
        if len(command_fills) > 1:
            classifications["multifill"].append(command_id)

        statuses = sorted(statuses_by_command.get(command_id, ()), key=lambda event: event.sequence)
        final_status = str(statuses[-1].payload.get("status") or "").lower() if statuses else ""
        if final_status and final_status not in _TERMINAL_STATUSES:
            raise ValueError(f"command {command_id!r} has unsupported terminal status {final_status!r}")
        terminal = rejected or final_status in _TERMINAL_STATUSES
        if not terminal:
            classifications["nonterminal"].append(command_id)
            model_scope_issues.append({"command_id": command_id, "reason": "terminal_status_missing"})
        if final_status in {"cancelled", "partially_filled_cancelled"}:
            classifications["cancelled"].append(command_id)
        if terminal and fill_qty < command.qty - 1e-12:
            classifications["terminal_incomplete"].append(command_id)

        baseline_twin = MarketOrderExecutionTwin(
            books={command.symbol: item.book},
            instrument_rules={command.symbol: rules},
            config=baseline_config,
            name="v7_p50_holdout_baseline",
            id_seed=f"v7-p50-holdout:{command_id}",
        )
        baseline_raw = tuple(baseline_twin.submit(command, item.market_input))
        baseline_observations = [
            observation
            for observation in baseline_raw
            if isinstance(observation, ExecutionObservation)
        ]
        if len(baseline_observations) != len(baseline_raw):
            raise TypeError("execution twin returned an untyped baseline observation")
        baseline_status, baseline_fill_qty, _baseline_fills = _modeled_terminal_summary(
            baseline_observations
        )
        actual_status = "rejected" if rejected else final_status or "nonterminal"
        if baseline_status != actual_status:
            model_scope_issues.append(
                {"command_id": command_id, "reason": "baseline_terminal_status_mismatch"}
            )
        if not math.isclose(baseline_fill_qty, fill_qty, rel_tol=0.0, abs_tol=1e-12):
            model_scope_issues.append(
                {"command_id": command_id, "reason": "baseline_fill_quantity_mismatch"}
            )
        if accepted and not rejected and terminal and fill_qty == 0.0:
            model_scope_issues.append(
                {
                    "command_id": command_id,
                    "reason": "accepted_zero_fill_terminal_not_represented_by_configured_book_walker",
                }
            )
        if fill_qty > 0.0 and fill_qty < command.qty - 1e-12 and not baseline_config.allow_partial_fills:
            model_scope_issues.append({"command_id": command_id, "reason": "partial_fill_model_disabled"})
        if len(command_fills) > 1 and baseline_config.fill_partition_policy != "book_level":
            model_scope_issues.append({"command_id": command_id, "reason": "multifill_partition_model_disabled"})

        actual_vwap: float | None = None
        visible_vwap: float | None = None
        residual_bps: float | None = None
        stress_vwap: float | None = None
        stress_covered: bool | None = None
        if fill_qty > 0.0:
            actual_vwap = fill_notional / fill_qty
            visible_vwap = _visible_book_vwap(item.book, signed_qty=command.signed_qty, fill_qty=fill_qty)
            if visible_vwap is None:
                model_scope_issues.append(
                    {
                        "command_id": command_id,
                        "reason": "actual_fill_exceeds_visible_decision_book_depth",
                    }
                )
            else:
                direction = 1.0 if command.signed_qty > 0.0 else -1.0
                residual_bps = direction * (actual_vwap - visible_vwap) / command.reference_price * 10_000.0
                residual_rows.append(
                    {
                        "command_id": command_id,
                        "symbol": command.symbol,
                        "actual_fill_qty": fill_qty,
                        "actual_fill_vwap": actual_vwap,
                        "visible_book_vwap": visible_vwap,
                        "residual_adverse_bps": residual_bps,
                    }
                )

            twin = MarketOrderExecutionTwin(
                books={command.symbol: item.book},
                instrument_rules={command.symbol: rules},
                config=stress_config,
                name="v7_p95_holdout_stress",
                id_seed=f"v7-p95-holdout:{command_id}",
            )
            observations = tuple(twin.submit(command, item.market_input))
            typed = [observation for observation in observations if isinstance(observation, ExecutionObservation)]
            stress_fills = [
                observation for observation in typed if observation.observation_type == ExecutionObservationType.FILL
            ]
            stress_qty = math.fsum(abs(observation.signed_qty) for observation in stress_fills)
            stress_vwap = _modeled_vwap_for_quantity(stress_fills, quantity=fill_qty)
            if stress_vwap is None:
                model_scope_issues.append(
                    {
                        "command_id": command_id,
                        "reason": "p95_stress_replay_cannot_represent_actual_fill_quantity",
                    }
                )
            else:
                direction = 1.0 if command.signed_qty > 0.0 else -1.0
                adverse_gap = direction * (stress_vwap - actual_vwap)
                stress_covered = adverse_gap >= (-STRESS_ROUNDING_TOLERANCE_TICKS * rules.tick_size - 1e-12)
                stress_rows.append(
                    {
                        "command_id": command_id,
                        "symbol": command.symbol,
                        "actual_fill_vwap": actual_vwap,
                        "stress_fill_vwap": stress_vwap,
                        "stress_total_fill_qty": stress_qty,
                        "stress_comparison_qty": fill_qty,
                        "tick_size": rules.tick_size,
                        "signed_adverse_gap": adverse_gap,
                        "rounding_tolerance_ticks": STRESS_ROUNDING_TOLERANCE_TICKS,
                        "covered": stress_covered,
                    }
                )

        command_rows.append(
            {
                "command_id": command_id,
                "batch_id": command.batch_id,
                "symbol": command.symbol,
                "sleeves": list(item.sleeves),
                "qty": command.qty,
                "signed_qty": command.signed_qty,
                "accepted_ack": accepted,
                "rejected_ack": rejected,
                "terminal_status": final_status,
                "fill_count": len(command_fills),
                "filled_qty": fill_qty,
                "baseline_terminal_status": baseline_status,
                "baseline_filled_qty": baseline_fill_qty,
                "actual_fill_vwap": actual_vwap,
                "visible_book_vwap": visible_vwap,
                "residual_adverse_bps": residual_bps,
                "stress_fill_vwap": stress_vwap,
                "stress_covered": stress_covered,
            }
        )

    for values in classifications.values():
        values.sort()
    positive_spacing_rows.sort(key=lambda row: (str(row["command_id"]), int(row["exchange_ts_ns"])))
    residual_rows.sort(key=lambda row: str(row["command_id"]))
    stress_rows.sort(key=lambda row: str(row["command_id"]))
    model_scope_issues.sort(key=lambda row: (row["command_id"], row["reason"]))
    command_rows.sort(key=lambda row: str(row["command_id"]))

    request_distribution = _distribution(request_ack_rtt_ns)
    residual_distribution = _distribution([float(row["residual_adverse_bps"]) for row in residual_rows])
    spacing_distribution = _distribution([float(row["spacing_ns"]) for row in positive_spacing_rows])
    request_nonnegative_ratio = (
        sum(value >= 0.0 for value in request_ack_rtt_ns) / len(request_ack_rtt_ns) if request_ack_rtt_ns else 0.0
    )
    stress_coverage = sum(row["covered"] is True for row in stress_rows) / len(stress_rows) if stress_rows else 0.0

    thresholds = {
        "nonnegative_min_ratio": NONNEGATIVE_MIN_RATIO,
        "stress_min_coverage": STRESS_MIN_COVERAGE,
        "stress_rounding_tolerance_ticks": STRESS_ROUNDING_TOLERANCE_TICKS,
        "spacing_min_holdout_samples": MIN_HOLDOUT_SPACING_SAMPLES,
        "feed_adjusted_ns": {
            "holdout_p50_max_v7_p75": _training_threshold(
                calibration_receipt, "latency_ns", "feed_clock_adjusted", "p75"
            ),
            "holdout_p95_max_v7_p99": _training_threshold(
                calibration_receipt, "latency_ns", "feed_clock_adjusted", "p99"
            ),
        },
        "request_ack_round_trip_ns": {
            "holdout_p50_max_v7_p75": _training_threshold(
                calibration_receipt, "latency_ns", "request_ack_round_trip", "p75"
            ),
            "holdout_p95_max_v7_p99": _training_threshold(
                calibration_receipt, "latency_ns", "request_ack_round_trip", "p99"
            ),
        },
        "residual_adverse_bps": {
            "holdout_p50_max_v7_p75": _training_threshold(
                calibration_receipt,
                "slippage",
                "residual_adverse_bps_after_visible_book",
                "p75",
            ),
            "holdout_p95_max_v7_p99": _training_threshold(
                calibration_receipt,
                "slippage",
                "residual_adverse_bps_after_visible_book",
                "p99",
            ),
        },
        "positive_fill_spacing_ns": {
            "holdout_p95_max_v7_p99": _training_threshold(
                calibration_receipt, "latency_ns", "partial_fill_spacing", "p99"
            )
        },
    }
    feed_limits = cast(Mapping[str, float], thresholds["feed_adjusted_ns"])
    request_limits = cast(Mapping[str, float], thresholds["request_ack_round_trip_ns"])
    residual_limits = cast(Mapping[str, float], thresholds["residual_adverse_bps"])
    spacing_limit = cast(Mapping[str, float], thresholds["positive_fill_spacing_ns"])["holdout_p95_max_v7_p99"]
    spacing_sufficient = len(positive_spacing_rows) >= MIN_HOLDOUT_SPACING_SAMPLES
    spacing_passed = not spacing_sufficient or float(cast(float, spacing_distribution["p95"])) <= spacing_limit
    gates = {
        "one_to_one_ungapped_command_book_linkage": linkage["linked_command_count"] == linkage["command_count"],
        "feed_nonnegative_ratio": feed_nonnegative_ratio >= NONNEGATIVE_MIN_RATIO,
        "request_ack_nonnegative_ratio": request_nonnegative_ratio >= NONNEGATIVE_MIN_RATIO,
        "feed_latency_envelope": _envelope_gate(
            feed_distribution,
            p50_limit=feed_limits["holdout_p50_max_v7_p75"],
            p95_limit=feed_limits["holdout_p95_max_v7_p99"],
        ),
        "request_ack_latency_envelope": _envelope_gate(
            request_distribution,
            p50_limit=request_limits["holdout_p50_max_v7_p75"],
            p95_limit=request_limits["holdout_p95_max_v7_p99"],
        ),
        "residual_slippage_envelope": _envelope_gate(
            residual_distribution,
            p50_limit=residual_limits["holdout_p50_max_v7_p75"],
            p95_limit=residual_limits["holdout_p95_max_v7_p99"],
        ),
        "p95_stress_adverse_coverage": stress_coverage >= STRESS_MIN_COVERAGE,
        "partial_fill_spacing_envelope": spacing_passed,
        "model_scope": not model_scope_issues,
    }
    passed = all(gates.values())
    has_minimum_metrics = all(
        cast(int, distribution["count"]) > 0
        for distribution in (
            feed_distribution,
            request_distribution,
            residual_distribution,
        )
    ) and bool(stress_rows)
    return {
        "linkage": linkage,
        "holdout_counts": {
            "commands": len(linked),
            "filled_commands": len(classifications["filled"]),
            "commands_by_sleeve": dict(sorted(commands_by_sleeve.items())),
            "filled_commands_by_sleeve": dict(sorted(filled_commands_by_sleeve.items())),
            "commands_by_symbol": dict(sorted(commands_by_symbol.items())),
            "filled_commands_by_symbol": dict(sorted(filled_commands_by_symbol.items())),
            "multi_sleeve_commands": sum(len(item.sleeves) > 1 for item in linked),
            "multifill_commands": len(classifications["multifill"]),
            "rejected_commands": len(classifications["rejected"]),
            "cancelled_commands": len(classifications["cancelled"]),
            "zero_fill_commands": len(classifications["zero_fill"]),
            "terminal_incomplete_commands": len(classifications["terminal_incomplete"]),
            "nonterminal_commands": len(classifications["nonterminal"]),
            "feed_latency_observations": int(feed_distribution["count"] or 0),
            "request_ack_observations": len(request_ack_rtt_ns),
            "residual_slippage_observations": len(residual_rows),
            "positive_fill_spacing_observations": len(positive_spacing_rows),
            "stress_replay_observations": len(stress_rows),
        },
        "commands": command_rows,
        "classifications": classifications,
        "latency_ns": {
            "feed_clock_adjusted": feed_distribution,
            "request_ack_round_trip": request_distribution,
            "feed_nonnegative_ratio": feed_nonnegative_ratio,
            "request_ack_nonnegative_ratio": request_nonnegative_ratio,
            "request_ack_observations": timing_rows,
        },
        "slippage": {
            "residual_adverse_bps_after_visible_book": residual_distribution,
            "command_observations": residual_rows,
        },
        "partial_fills": {
            "positive_spacing_ns": spacing_distribution,
            "positive_spacing_observations": positive_spacing_rows,
            "equal_exchange_timestamp_pairs": equal_timestamp_fill_pairs,
            "evidence_status": (
                "sufficient_holdout_spacing" if spacing_sufficient else "insufficient_holdout_spacing_does_not_erase_v7"
            ),
        },
        "stress": {
            "config_role": STRESS_CONFIG_ROLE,
            "command_observations": stress_rows,
            "coverage_ratio": stress_coverage,
        },
        "model_scope": {
            "order_type": "market",
            "immutable_replay_book": baseline_config.immutable_replay_book and stress_config.immutable_replay_book,
            "passive_queue_calibrated": False,
            "fill_partition_policy": baseline_config.fill_partition_policy,
            "issues": model_scope_issues,
        },
        "thresholds": thresholds,
        "gates": gates,
        "execution_twin_drift_gate_passed": passed,
        "evidence_result": ("supports" if passed else "contradicts" if has_minimum_metrics else "inconclusive"),
    }


def build_execution_twin_drift_receipt(
    *,
    calibration_file: str | Path,
    v7_archive_map_file: str | Path,
    natural_account_root: str | Path,
    natural_market_capture_root: str | Path,
    freeze_manifest_file: str | Path,
    natural_target_capture_file: str | Path,
    safety_target_capture_file: str | Path,
    safety_manifest_file: str | Path,
    demo_rules_file: str | Path,
    clock_offset_series_file: str | Path,
    baseline_config_file: str | Path,
    stress_config_file: str | Path,
    expected_account_id: str,
    t0_ns: int,
    t1_ns: int,
    observed_ts_ns: int,
) -> dict[str, Any]:
    """Recompute the registered natural holdout checks from immutable sources."""

    if not expected_account_id.strip() or observed_ts_ns <= 0:
        raise ValueError("execution-twin drift requires account id and observation time")
    _require_exact_natural_window(t0_ns, t1_ns)
    if observed_ts_ns < t1_ns:
        raise ValueError("execution-twin drift cannot be observed before natural T1")
    calibration_path = _regular_file(calibration_file, label="V7 calibration receipt")
    archive_map_path = _regular_file(v7_archive_map_file, label="V7 archive-source map")
    natural_account = _directory(natural_account_root, label="natural demo account root")
    natural_capture = _directory(natural_market_capture_root, label="natural demo market-capture root")
    freeze_path = _regular_file(freeze_manifest_file, label="natural cutover freeze manifest")
    natural_target_capture_path = _regular_file(natural_target_capture_file, label="natural target scheduling capture")
    safety_target_capture_path = _regular_file(safety_target_capture_file, label="post-window safety target capture")
    safety_manifest_path = _regular_file(safety_manifest_file, label="post-window safety manifest")
    rules_path = _regular_file(demo_rules_file, label="natural demo rules")
    clock_series_path = _regular_file(clock_offset_series_file, label="natural clock-offset series")
    baseline_path = _regular_file(baseline_config_file, label="baseline execution-twin config")
    stress_path = _regular_file(stress_config_file, label="stress execution-twin config")

    calibration_snapshot = _read_snapshot(calibration_path, label="V7 calibration receipt")
    calibration = load_calibration_receipt(
        calibration_path,
        require_registered_requirements=True,
        snapshot=calibration_snapshot,
    )
    if int(calibration.get("schema_version") or 0) != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("execution-twin drift refuses a pre-schema-v3 V7 receipt")
    if calibration.get("execution_twin_gate_passed") is not True:
        raise ValueError("V7 execution-twin calibration gate has not passed")
    archive_map_snapshot = _read_snapshot(archive_map_path, label="V7 archive-source map")
    archive_map = load_v7_archive_source_map(
        archive_map_path,
        calibration_receipt=calibration,
        snapshot=archive_map_snapshot,
    )
    archived = cast(Mapping[str, Any], archive_map["archived_sources"])
    archived_account = _directory(str(archived["account_root"]), label="archived V7 account root")
    archived_capture = _directory(str(archived["market_capture_root"]), label="archived V7 capture root")
    _require_disjoint(
        {
            "archived_v7_account_root": archived_account,
            "archived_v7_capture_root": archived_capture,
            "natural_account_root": natural_account,
            "natural_capture_root": natural_capture,
        }
    )
    identities, snapshots = _freeze_sources(
        calibration_file=calibration_path,
        archive_map_file=archive_map_path,
        archive_map=archive_map,
        natural_account_root=natural_account,
        natural_capture_root=natural_capture,
        freeze_manifest_file=freeze_path,
        natural_target_capture_file=natural_target_capture_path,
        safety_target_capture_file=safety_target_capture_path,
        safety_manifest_file=safety_manifest_path,
        demo_rules_file=rules_path,
        clock_offset_series_file=clock_series_path,
        baseline_config_file=baseline_path,
        stress_config_file=stress_path,
        initial_snapshots={
            "v7_calibration_receipt": calibration_snapshot,
            "v7_archive_source_map": archive_map_snapshot,
        },
    )
    baseline_artifact = load_execution_twin_config_artifact(
        baseline_path,
        calibration_receipt=calibration,
        expected_role=BASELINE_CONFIG_ROLE,
        snapshot=snapshots["baseline_twin_config"],
    )
    stress_artifact = load_execution_twin_config_artifact(
        stress_path,
        calibration_receipt=calibration,
        expected_role=STRESS_CONFIG_ROLE,
        snapshot=snapshots["stress_twin_config"],
    )
    if baseline_artifact["max_decision_age_ns"] != stress_artifact["max_decision_age_ns"]:
        raise ValueError("baseline and stress configs changed max decision age")
    baseline_config = _config_for_role(
        calibration,
        role=BASELINE_CONFIG_ROLE,
        max_decision_age_ns=int(baseline_artifact["max_decision_age_ns"]),
    )
    stress_config = _config_for_role(
        calibration,
        role=STRESS_CONFIG_ROLE,
        max_decision_age_ns=int(stress_artifact["max_decision_age_ns"]),
    )
    rules_payload = _strict_json(snapshots["natural_demo_rules"].data, label="natural demo rules")
    rules = load_demo_rules_bytes(snapshots["natural_demo_rules"].data)
    clock_series = load_clock_offset_series(
        clock_series_path,
        snapshot=snapshots["natural_clock_offset_series"],
    )
    clock_interpolator = ClockOffsetInterpolator(clock_series)
    freeze = _load_freeze_snapshot(freeze_path, snapshots["natural_cutover_freeze_manifest"])
    scope = _load_natural_scope(
        natural_target_capture_file=natural_target_capture_path,
        safety_target_capture_file=safety_target_capture_path,
        safety_manifest_file=safety_manifest_path,
        identities=identities,
        snapshots=snapshots,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    freeze_binding = _validate_freeze_binding(
        freeze,
        freeze_path=freeze_path,
        identities=identities,
        expected_account_id=expected_account_id,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        natural_account_root=natural_account,
        natural_capture_root=natural_capture,
        calibration_path=calibration_path,
        calibration=calibration,
        archive_map_path=archive_map_path,
        archive_map=archive_map,
        baseline_path=baseline_path,
        baseline=baseline_artifact,
        stress_path=stress_path,
        stress=stress_artifact,
        demo_rules_path=rules_path,
        demo_rules=rules_payload,
        clock_series_path=clock_series_path,
        clock_series=clock_series,
        safety_freeze_id=str(scope.safety_manifest.get("freeze_id") or ""),
    )
    natural_events = _account_journal_from_snapshots(snapshots, prefix="natural_journal")
    if not natural_events or natural_events[0].prev_event_hash != GENESIS_HASH:
        raise ValueError("natural demo journal does not begin at a fresh genesis boundary")
    account_ids = {event.account_id for event in natural_events}
    if account_ids != {expected_account_id}:
        raise ValueError(f"natural journal account ids {sorted(account_ids)!r} do not match {expected_account_id!r}")
    if observed_ts_ns < max(event.wall_ts_ns for event in natural_events):
        raise ValueError("drift receipt observation time predates the natural journal")
    for event in natural_events:
        _require_finite_tree(event.to_dict(), label=f"natural journal event {event.sequence}")
    scoped_events, natural_context_ids, scope_classification = _scope_natural_journal(natural_events, scope=scope)
    (
        capture_rows,
        feed_distribution,
        feed_nonnegative_ratio,
        feed_clock_application,
    ) = _parse_capture(
        identities,
        snapshots=snapshots,
        prefix="natural_capture",
        clock_interpolator=clock_interpolator,
        context_record_ids=natural_context_ids,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
    )
    evaluation = _evaluate_holdout(
        events=scoped_events,
        capture_rows=capture_rows,
        feed_distribution=feed_distribution,
        feed_nonnegative_ratio=feed_nonnegative_ratio,
        instrument_rules=rules,
        calibration_receipt=calibration,
        baseline_config=baseline_config,
        stress_config=stress_config,
    )
    evaluation_gates = cast(dict[str, bool], evaluation["gates"])
    evaluation_gates["clock_offset_series_coverage"] = clock_series.get("clock_offset_series_gate_passed") is True
    evaluation["execution_twin_drift_gate_passed"] = all(evaluation_gates.values())
    # Re-open every source after computation.  A changed segment set, inode,
    # size, mtime, mode, or byte hash invalidates the run rather than producing
    # a receipt over a mixed epoch.
    final_identities, final_snapshots = _freeze_sources(
        calibration_file=calibration_path,
        archive_map_file=archive_map_path,
        archive_map=archive_map,
        natural_account_root=natural_account,
        natural_capture_root=natural_capture,
        freeze_manifest_file=freeze_path,
        natural_target_capture_file=natural_target_capture_path,
        safety_target_capture_file=safety_target_capture_path,
        safety_manifest_file=safety_manifest_path,
        demo_rules_file=rules_path,
        clock_offset_series_file=clock_series_path,
        baseline_config_file=baseline_path,
        stress_config_file=stress_path,
    )
    if _identity_payload(final_identities) != _identity_payload(identities):
        raise RuntimeError("execution-twin drift sources mutated during verification")
    final_freeze = _load_freeze_snapshot(freeze_path, final_snapshots["natural_cutover_freeze_manifest"])
    if canonical_json(final_freeze) != canonical_json(freeze):
        raise RuntimeError("natural cutover freeze changed during drift verification")
    final_clock_series = load_clock_offset_series(
        clock_series_path,
        snapshot=final_snapshots["natural_clock_offset_series"],
    )
    if canonical_json(final_clock_series) != canonical_json(clock_series):
        raise RuntimeError("clock-offset series changed during drift verification")

    natural_request_rows = [
        {
            "batch_id": batch_id,
            "request_id": captured.request.request_id,
            "request_hash": captured.request_hash,
            "sleeve": scope.sleeve_by_batch[batch_id],
        }
        for batch_id, captured in sorted(scope.natural_requests.items())
    ]
    safety_request_rows = [
        {
            "batch_id": batch_id,
            "request_id": captured.request.request_id,
            "request_hash": captured.request_hash,
        }
        for batch_id, captured in sorted(scope.safety_requests.items())
    ]
    scope_material = {
        "t0_ns": t0_ns,
        "t1_ns": t1_ns,
        "freeze_manifest_file_sha256": freeze_binding["file_sha256"],
        "freeze_artifact_sha256": freeze_binding["artifact_sha256"],
        "clock_offset_series_file_sha256": identities["natural_clock_offset_series"].sha256,
        "clock_offset_series_artifact_sha256": clock_series["artifact_sha256"],
        "natural_target_capture_sha256": identities["natural_target_capture"].sha256,
        "natural_target_capture_tape_hash": scope.natural_capture_tape_hash,
        "natural_requests": natural_request_rows,
        "safety_target_capture_sha256": identities["post_window_safety_target_capture"].sha256,
        "safety_target_capture_tape_hash": scope.safety_capture_tape_hash,
        "safety_manifest_sha256": identities["post_window_safety_manifest"].sha256,
        "safety_manifest_artifact_sha256": scope.safety_manifest["artifact_sha256"],
        "freeze_id": scope.safety_manifest["freeze_id"],
        "safety_requests": safety_request_rows,
    }
    natural_scope_sha256 = hashlib.sha256(canonical_json(scope_material)).hexdigest()
    natural_batch_ids_sha256 = hashlib.sha256(_canonical_json_value(sorted(scope.natural_batch_ids))).hexdigest()
    safety_batch_ids_sha256 = hashlib.sha256(_canonical_json_value(sorted(scope.safety_batch_ids))).hexdigest()

    payload: dict[str, Any] = {
        "schema_version": DRIFT_SCHEMA_VERSION,
        "kind": DRIFT_KIND,
        "validator": DRIFT_VALIDATOR,
        "evidence_scope": DRIFT_EVIDENCE_SCOPE,
        "claim": (
            "the V7-calibrated market-order twin stayed within the registered "
            "latency, residual-slippage, stress, spacing, and model-scope envelope "
            "on this disjoint natural Bybit demo holdout"
        ),
        "study_mode": "forward_execution",
        "deployment": "demo",
        "execution_authorization": "not_granted",
        "observed_ts_ns": observed_ts_ns,
        "expected_account_id": expected_account_id,
        "source_roots": {
            "calibration_file": str(calibration_path),
            "v7_archive_map_file": str(archive_map_path),
            "archived_v7_account_root": str(archived_account),
            "archived_v7_market_capture_root": str(archived_capture),
            "natural_account_root": str(natural_account),
            "natural_market_capture_root": str(natural_capture),
            "freeze_manifest_file": str(freeze_path),
            "natural_target_capture_file": str(natural_target_capture_path),
            "safety_target_capture_file": str(safety_target_capture_path),
            "safety_manifest_file": str(safety_manifest_path),
            "demo_rules_file": str(rules_path),
            "clock_offset_series_file": str(clock_series_path),
            "baseline_config_file": str(baseline_path),
            "stress_config_file": str(stress_path),
        },
        "source_files": _identity_payload(identities),
        "holdout_scope": {
            "freeze_id": freeze_binding["freeze_id"],
            "freeze_artifact_sha256": freeze_binding["artifact_sha256"],
            "freeze_manifest_file_sha256": freeze_binding["file_sha256"],
            "t0_ns": t0_ns,
            "t1_ns": t1_ns,
            "clock_offset_series_artifact_sha256": clock_series["artifact_sha256"],
            "clock_offset_series_file_sha256": identities["natural_clock_offset_series"].sha256,
            "clock_offset_series_sample_count": clock_series["coverage"]["sample_count"],
            "clock_offset_series_max_observed_gap_ns": clock_series["coverage"]["max_observed_gap_ns"],
            "clock_offset_series_t0_bracketed": clock_series["coverage"]["t0_bracketed"],
            "clock_offset_series_t1_bracketed": clock_series["coverage"]["t1_bracketed"],
            "natural_batch_ids_sha256": natural_batch_ids_sha256,
            "safety_batch_ids_sha256": safety_batch_ids_sha256,
            "safety_batches_excluded": True,
        },
        "natural_scope": {
            "scope_sha256": natural_scope_sha256,
            "freeze_id": freeze_binding["freeze_id"],
            "freeze_manifest": freeze_binding,
            "window": {
                "t0_ns": t0_ns,
                "t1_ns": t1_ns,
                "hours": NATURAL_WINDOW_HOURS,
                "interval": "half_open",
            },
            "natural_target_capture": {
                "source_file_sha256": identities["natural_target_capture"].sha256,
                "capture_tape_hash": scope.natural_capture_tape_hash,
                "capture_event_count": scope.natural_capture_event_count,
                "durable_request_count": len(scope.natural_requests),
                "requests": natural_request_rows,
                "batch_ids": sorted(scope.natural_batch_ids),
            },
            "post_window_safety": {
                "source_file_sha256": identities["post_window_safety_target_capture"].sha256,
                "capture_tape_hash": scope.safety_capture_tape_hash,
                "capture_event_count": scope.safety_capture_event_count,
                "durable_request_count": len(scope.safety_requests),
                "manifest_source_file_sha256": identities["post_window_safety_manifest"].sha256,
                "manifest_artifact_sha256": scope.safety_manifest["artifact_sha256"],
                "requests": safety_request_rows,
                "batch_ids": sorted(scope.safety_batch_ids),
                "excluded_from_all_drift_metrics": True,
            },
            "journal_classification": scope_classification,
            "scope_material": scope_material,
        },
        "training": {
            "calibration_schema_version": calibration["schema_version"],
            "calibration_artifact_sha256": calibration["artifact_sha256"],
            "archive_map_artifact_sha256": archive_map["artifact_sha256"],
            "account_journal_sha256": archive_map["account_journal_sha256"],
            "market_capture_manifest_sha256": archive_map["market_capture_manifest_sha256"],
        },
        "configs": {
            "baseline_artifact_sha256": baseline_artifact["artifact_sha256"],
            "stress_artifact_sha256": stress_artifact["artifact_sha256"],
            "baseline": asdict(baseline_config),
            "stress": asdict(stress_config),
        },
        "clock_correction": {
            "series_artifact_sha256": clock_series["artifact_sha256"],
            "series_source_file_sha256": identities["natural_clock_offset_series"].sha256,
            "freeze_id": clock_series["freeze"]["freeze_id"],
            "initial_receipt_artifact_sha256": clock_series["freeze"]["initial_clock_receipt"]["artifact_sha256"],
            "coverage": clock_series["coverage"],
            "contract": clock_series["contract"],
            "application": feed_clock_application,
        },
        "natural_journal": {
            "event_count": len(natural_events),
            "scoped_metric_event_count": len(scoped_events),
            "journal_sha256": _journal_sha256(natural_events),
            "last_event_hash": natural_events[-1].event_hash,
            "first_sequence": natural_events[0].sequence,
            "last_sequence": natural_events[-1].sequence,
            "first_wall_ts_ns": natural_events[0].wall_ts_ns,
            "last_wall_ts_ns": natural_events[-1].wall_ts_ns,
        },
        **evaluation,
        "limitations": list(_LIMITATIONS),
        "artifact_sha256": "",
    }
    _require_finite_tree(payload, label="execution-twin drift receipt")
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _precheck_drift_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    if int(value.get("schema_version") or 0) != DRIFT_SCHEMA_VERSION:
        raise ValueError("unsupported execution-twin drift receipt schema")
    if value.get("kind") != DRIFT_KIND or value.get("validator") != DRIFT_VALIDATOR:
        raise ValueError("unexpected execution-twin drift receipt kind/validator")
    if value.get("evidence_scope") != DRIFT_EVIDENCE_SCOPE:
        raise ValueError("execution-twin drift receipt changed evidence scope")
    if value.get("execution_authorization") != "not_granted":
        raise ValueError("execution-twin drift receipt cannot grant execution authority")
    if value.get("limitations") != list(_LIMITATIONS):
        raise ValueError("execution-twin drift receipt limitations changed")
    _require_finite_tree(value, label="execution-twin drift receipt")
    if _lower_sha256(value.get("artifact_sha256"), label="drift receipt hash") != _self_hash(value):
        raise ValueError("execution-twin drift receipt hash mismatch")
    gates = value.get("gates")
    if not isinstance(gates, Mapping) or any(type(flag) is not bool for flag in gates.values()):
        raise ValueError("execution-twin drift receipt gates are malformed")
    if value.get("execution_twin_drift_gate_passed") is not all(gates.values()):
        raise ValueError("execution-twin drift aggregate gate is inconsistent")
    natural_scope = value.get("natural_scope")
    if not isinstance(natural_scope, Mapping):
        raise ValueError("execution-twin drift receipt lacks natural scope")
    scope_material = natural_scope.get("scope_material")
    if not isinstance(scope_material, Mapping):
        raise ValueError("execution-twin drift receipt lacks scope hash material")
    expected_scope_hash = hashlib.sha256(canonical_json(dict(scope_material))).hexdigest()
    if _lower_sha256(natural_scope.get("scope_sha256"), label="natural scope hash") != expected_scope_hash:
        raise ValueError("execution-twin drift natural scope hash mismatch")
    window = natural_scope.get("window")
    if not isinstance(window, Mapping):
        raise ValueError("execution-twin drift receipt lacks natural window")
    _require_exact_natural_window(
        _integer(window.get("t0_ns"), label="natural T0", positive=True),
        _integer(window.get("t1_ns"), label="natural T1", positive=True),
    )
    safety = natural_scope.get("post_window_safety")
    if not isinstance(safety, Mapping) or safety.get("excluded_from_all_drift_metrics") is not True:
        raise ValueError("execution-twin drift receipt did not exclude safety metrics")
    freeze_binding = natural_scope.get("freeze_manifest")
    holdout_scope = value.get("holdout_scope")
    natural_capture = natural_scope.get("natural_target_capture")
    clock_correction = value.get("clock_correction")
    if (
        not isinstance(freeze_binding, Mapping)
        or not isinstance(holdout_scope, Mapping)
        or not isinstance(natural_capture, Mapping)
        or not isinstance(clock_correction, Mapping)
    ):
        raise ValueError("execution-twin drift receipt lacks freeze/holdout binding")
    clock_coverage = clock_correction.get("coverage")
    if not isinstance(clock_coverage, Mapping):
        raise ValueError("execution-twin drift receipt lacks clock-series coverage")
    natural_batch_ids = natural_capture.get("batch_ids")
    safety_batch_ids = safety.get("batch_ids")
    if not isinstance(natural_batch_ids, list) or not isinstance(safety_batch_ids, list):
        raise ValueError("execution-twin drift receipt has malformed scoped batch sets")
    expected_holdout = {
        "freeze_id": freeze_binding.get("freeze_id"),
        "freeze_artifact_sha256": freeze_binding.get("artifact_sha256"),
        "freeze_manifest_file_sha256": freeze_binding.get("file_sha256"),
        "t0_ns": window.get("t0_ns"),
        "t1_ns": window.get("t1_ns"),
        "clock_offset_series_artifact_sha256": clock_correction.get("series_artifact_sha256"),
        "clock_offset_series_file_sha256": clock_correction.get("series_source_file_sha256"),
        "clock_offset_series_sample_count": clock_coverage.get("sample_count"),
        "clock_offset_series_max_observed_gap_ns": clock_coverage.get("max_observed_gap_ns"),
        "clock_offset_series_t0_bracketed": clock_coverage.get("t0_bracketed"),
        "clock_offset_series_t1_bracketed": clock_coverage.get("t1_bracketed"),
        "natural_batch_ids_sha256": hashlib.sha256(
            _canonical_json_value(sorted(str(value) for value in natural_batch_ids))
        ).hexdigest(),
        "safety_batch_ids_sha256": hashlib.sha256(
            _canonical_json_value(sorted(str(value) for value in safety_batch_ids))
        ).hexdigest(),
        "safety_batches_excluded": True,
    }
    if dict(holdout_scope) != expected_holdout:
        raise ValueError("execution-twin drift holdout scope is inconsistent")
    return value


def verify_execution_twin_drift_receipt(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-open every bound source and reproduce a drift receipt exactly."""

    value = _precheck_drift_receipt(payload)
    sources = value.get("source_roots")
    if not isinstance(sources, Mapping):
        raise ValueError("execution-twin drift receipt lacks source roots")
    scope = value.get("natural_scope")
    window = scope.get("window") if isinstance(scope, Mapping) else None
    if not isinstance(window, Mapping):
        raise ValueError("execution-twin drift receipt lacks natural scope/window")
    rebuilt = build_execution_twin_drift_receipt(
        calibration_file=str(sources.get("calibration_file") or ""),
        v7_archive_map_file=str(sources.get("v7_archive_map_file") or ""),
        natural_account_root=str(sources.get("natural_account_root") or ""),
        natural_market_capture_root=str(sources.get("natural_market_capture_root") or ""),
        freeze_manifest_file=str(sources.get("freeze_manifest_file") or ""),
        natural_target_capture_file=str(sources.get("natural_target_capture_file") or ""),
        safety_target_capture_file=str(sources.get("safety_target_capture_file") or ""),
        safety_manifest_file=str(sources.get("safety_manifest_file") or ""),
        demo_rules_file=str(sources.get("demo_rules_file") or ""),
        clock_offset_series_file=str(sources.get("clock_offset_series_file") or ""),
        baseline_config_file=str(sources.get("baseline_config_file") or ""),
        stress_config_file=str(sources.get("stress_config_file") or ""),
        expected_account_id=str(value.get("expected_account_id") or ""),
        t0_ns=_integer(window.get("t0_ns"), label="natural T0", positive=True),
        t1_ns=_integer(window.get("t1_ns"), label="natural T1", positive=True),
        observed_ts_ns=int(value.get("observed_ts_ns") or 0),
    )
    if canonical_json(rebuilt) != canonical_json(value):
        raise ValueError("execution-twin drift receipt does not reproduce from sources")
    return value


def _require_output_outside_sources(path: Path, payload: Mapping[str, Any]) -> None:
    output = path.expanduser().resolve(strict=False)
    sources = cast(Mapping[str, Any], payload["source_roots"])
    for label in (
        "archived_v7_account_root",
        "archived_v7_market_capture_root",
        "natural_account_root",
        "natural_market_capture_root",
    ):
        root = _directory(str(sources[label]), label=label)
        if output == root or root in output.parents:
            raise ValueError(f"drift receipt output is nested in source root {label}")
    source_files = cast(Mapping[str, Any], payload["source_files"])
    for identity in source_files.values():
        if isinstance(identity, Mapping) and output == Path(str(identity.get("path") or "")):
            raise ValueError("drift receipt output aliases a source file")


def write_execution_twin_drift_receipt(path: str | Path, payload: Mapping[str, Any]) -> Path:
    value = verify_execution_twin_drift_receipt(payload)
    output = Path(path).expanduser()
    if not output.is_absolute():
        raise ValueError("execution-twin drift receipt output must be absolute")
    _require_output_outside_sources(output, value)
    return _atomic_create(output, value, label="execution-twin drift receipt")


def load_execution_twin_drift_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    source = _use_snapshot(
        path,
        label="execution-twin drift receipt",
        snapshot=snapshot,
    )
    value = _strict_json(source.data, label="execution-twin drift receipt")
    _require_output_outside_sources(source.path, value)
    return verify_execution_twin_drift_receipt(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze and verify source-bound V7 execution-twin holdout evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    archive = subparsers.add_parser(
        "archive-map",
        help="bind archived V7 journal/capture bytes to the calibration receipt",
    )
    archive.add_argument("--calibration-file", required=True)
    archive.add_argument("--archived-account-root", required=True)
    archive.add_argument("--archived-market-capture-root", required=True)
    archive.add_argument("--output", required=True)

    configs = subparsers.add_parser(
        "freeze-configs",
        help="derive exact baseline-p50 and stress-p95 configs from V7",
    )
    configs.add_argument("--calibration-file", required=True)
    configs.add_argument("--max-decision-age-ms", type=float, required=True)
    configs.add_argument("--baseline-output", required=True)
    configs.add_argument("--stress-output", required=True)

    verify = subparsers.add_parser("verify", help="recompute natural demo holdout drift from frozen sources")
    verify.add_argument("--calibration-file", required=True)
    verify.add_argument("--v7-archive-map", required=True)
    verify.add_argument("--natural-account-root", required=True)
    verify.add_argument("--natural-market-capture-root", required=True)
    verify.add_argument("--freeze-manifest", required=True)
    verify.add_argument("--natural-target-capture", required=True)
    verify.add_argument("--safety-target-capture", required=True)
    verify.add_argument("--safety-manifest", required=True)
    verify.add_argument("--demo-rules-file", required=True)
    verify.add_argument("--clock-offset-series", required=True)
    verify.add_argument("--baseline-config", required=True)
    verify.add_argument("--stress-config", required=True)
    verify.add_argument("--account-id", required=True)
    verify.add_argument("--t0-ns", type=int, required=True)
    verify.add_argument("--t1-ns", type=int, required=True)
    verify.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "archive-map":
            payload = build_v7_archive_source_map(
                calibration_file=args.calibration_file,
                archived_account_root=args.archived_account_root,
                archived_market_capture_root=args.archived_market_capture_root,
            )
            output = write_v7_archive_source_map(args.output, payload)
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "artifact_sha256": payload["artifact_sha256"],
                        "calibration_artifact_sha256": payload["calibration_artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-configs":
            if not math.isfinite(args.max_decision_age_ms) or args.max_decision_age_ms <= 0.0:
                parser.error("--max-decision-age-ms must be finite and positive")
            if int(round(args.max_decision_age_ms * 1_000_000.0)) != REGISTERED_MAX_DECISION_AGE_NS:
                parser.error("--max-decision-age-ms is fixed prospectively at 250")
            calibration = load_calibration_receipt(args.calibration_file, require_registered_requirements=True)
            baseline_output = Path(args.baseline_output).expanduser()
            stress_output = Path(args.stress_output).expanduser()
            if baseline_output.resolve(strict=False) == stress_output.resolve(strict=False):
                raise ValueError("baseline and stress config outputs must be distinct")
            if (
                baseline_output.exists()
                or baseline_output.is_symlink()
                or stress_output.exists()
                or stress_output.is_symlink()
            ):
                raise FileExistsError("config outputs already exist; preserve frozen evidence")
            max_age_ns = int(round(args.max_decision_age_ms * 1_000_000.0))
            baseline = build_execution_twin_config_artifact(
                calibration,
                role=BASELINE_CONFIG_ROLE,
                max_decision_age_ns=max_age_ns,
            )
            stress = build_execution_twin_config_artifact(
                calibration,
                role=STRESS_CONFIG_ROLE,
                max_decision_age_ns=max_age_ns,
            )
            baseline_path = write_execution_twin_config_artifact(baseline_output, baseline)
            stress_path = write_execution_twin_config_artifact(stress_output, stress)
            print(
                json.dumps(
                    {
                        "baseline_output": str(baseline_path),
                        "baseline_artifact_sha256": baseline["artifact_sha256"],
                        "stress_output": str(stress_path),
                        "stress_artifact_sha256": stress["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        receipt = build_execution_twin_drift_receipt(
            calibration_file=args.calibration_file,
            v7_archive_map_file=args.v7_archive_map,
            natural_account_root=args.natural_account_root,
            natural_market_capture_root=args.natural_market_capture_root,
            freeze_manifest_file=args.freeze_manifest,
            natural_target_capture_file=args.natural_target_capture,
            safety_target_capture_file=args.safety_target_capture,
            safety_manifest_file=args.safety_manifest,
            demo_rules_file=args.demo_rules_file,
            clock_offset_series_file=args.clock_offset_series,
            baseline_config_file=args.baseline_config,
            stress_config_file=args.stress_config,
            expected_account_id=args.account_id,
            t0_ns=args.t0_ns,
            t1_ns=args.t1_ns,
            observed_ts_ns=time.time_ns(),
        )
        output = write_execution_twin_drift_receipt(args.output, receipt)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "artifact_sha256": receipt["artifact_sha256"],
                    "holdout_counts": receipt["holdout_counts"],
                    "gates": receipt["gates"],
                    "execution_twin_drift_gate_passed": receipt["execution_twin_drift_gate_passed"],
                    "evidence_result": receipt["evidence_result"],
                },
                sort_keys=True,
            )
        )
        return 0 if receipt["execution_twin_drift_gate_passed"] else 3
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"execution-twin drift failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
