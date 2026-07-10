"""Immutable, value-blind S01 snapshots for registered strategy-overhaul roots.

Phase 0 proves structural feasibility without hashing numeric source bytes.  An
S01 child must subsequently bind the exact files it will use before any S02
feature or S03/S04 outcome artifact is constructed.  This module performs that
narrow transition: it hashes bytes and partition identities, but never decodes a
market value, calculates a return, or authorizes an outcome run.

The file manifest itself is path-relative and reproducible.  The broader
``snapshot_chain_identity_sha256`` also binds the Phase-0 receipt, whose observed
root identity may contain absolute paths, so this module does not claim that the
complete chain is location-independent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from liquidity_migration.strategy_overhaul_phase0_verifier import (
    Phase0BundleVerificationError,
    verify_phase0_bundle,
)


ROOT_SNAPSHOT_SCHEMA_VERSION = 2
SUPPORTED_VENUES = frozenset({"bybit", "binance"})
_PARTITIONED_DATASETS = MappingProxyType(
    {
        "klines_1h": "label_end_date_exclusive",
        "archive_trade_manifest": "signal_end_date_exclusive",
    }
)
_SINGLE_FILE_DATASETS = ("residual_momentum.parquet",)
_HASH_CHUNK_BYTES = 1024 * 1024
_ROOT_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "status",
        "venue",
        "root_path",
        "window",
        "phase0_bundle_receipt_path",
        "phase0_bundle_receipt_sha256",
        "file_manifest_format",
        "file_manifest_sha256",
        "file_count",
        "total_bytes",
        "datasets",
        "snapshot_chain_identity_sha256",
        "identity_path_independent",
        "path_independence_limitation",
        "phase0_bundle_bytes_verified",
        "phase0_internal_reexecution_verified",
        "phase0_semantics_fully_verified",
        "source_authenticity_proven",
        "full_process_environment_identity_proven",
        "upstream_root_lineage_proven",
        "registered_scope_verified",
        "earliest_root_history_proven",
        "registered_s01_ready",
        "readiness_limitation",
        "numeric_values_decoded",
        "file_bytes_hashed",
        "returns_calculated",
        "labels_calculated",
        "outcome_run_authorized",
        "real_money_authorized",
        "artifact_sha256",
    }
)
class RootSnapshotError(RuntimeError):
    """A Phase-0 chain, root shape, quiescence, or content check failed."""


@dataclass(frozen=True, slots=True)
class RootSnapshotWindow:
    identity_history_start_date: str
    causal_read_start_date: str
    signal_end_date_exclusive: str
    label_end_date_exclusive: str

    def __post_init__(self) -> None:
        history_start = _parse_date(self.identity_history_start_date, name="identity_history_start_date")
        start = _parse_date(self.causal_read_start_date, name="causal_read_start_date")
        signal_end = _parse_date(self.signal_end_date_exclusive, name="signal_end_date_exclusive")
        label_end = _parse_date(self.label_end_date_exclusive, name="label_end_date_exclusive")
        if not history_start <= start < signal_end <= label_end:
            raise ValueError(
                "root snapshot window must satisfy identity_history_start_date <= causal_read_start_date < "
                "signal_end_date_exclusive <= label_end_date_exclusive"
            )


@dataclass(frozen=True, slots=True)
class RootSnapshotArtifacts:
    receipt: Mapping[str, Any]
    file_manifest_jsonl: bytes


@dataclass(frozen=True, slots=True)
class _ObservedFile:
    dataset: str
    relative_path: str
    date: str | None
    bytes: int
    mode: int
    mtime_ns: int
    inode: int


def _parse_date(value: str, *, name: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be canonical ISO YYYY-MM-DD")
    return parsed


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RootSnapshotError(f"root snapshot contains a non-JSON value: {exc}") from exc


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RootSnapshotError(f"cannot open regular non-symlink file for hashing: {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RootSnapshotError(f"hash input is not a regular file: {path}")
        while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IMODE(after.st_mode),
    )
    if before_identity != after_identity:
        raise RootSnapshotError(f"file changed while hashing: {path}")
    observed_path = _regular_file_stat(path, label=str(path))
    observed_identity = (
        observed_path.st_dev,
        observed_path.st_ino,
        observed_path.st_size,
        observed_path.st_mtime_ns,
        stat.S_IMODE(observed_path.st_mode),
    )
    if after_identity != observed_identity:
        raise RootSnapshotError(f"file path was replaced while hashing: {path}")
    return digest.hexdigest()


def _regular_file_stat(path: Path, *, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise RootSnapshotError(f"cannot stat {label}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise RootSnapshotError(f"{label} must be a regular non-symlink file")
    return observed


def _date_range(start: dt.date, end: dt.date) -> tuple[str, ...]:
    values: list[str] = []
    day = start
    while day < end:
        values.append(day.isoformat())
        day += dt.timedelta(days=1)
    return tuple(values)


def _partition_date(path: Path, *, dataset_root: Path) -> str:
    try:
        relative_parts = path.relative_to(dataset_root).parts
    except ValueError as exc:  # pragma: no cover - internal discovery invariant
        raise RootSnapshotError(f"partition file escaped dataset root: {path}") from exc
    values = [part.removeprefix("date=") for part in relative_parts[:-1] if part.startswith("date=")]
    if len(values) != 1:
        raise RootSnapshotError(f"partition file must have exactly one date=YYYY-MM-DD component: {path}")
    _parse_date(values[0], name="partition date")
    return values[0]


def _relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:  # pragma: no cover - internal discovery invariant
        raise RootSnapshotError(f"source file escaped root: {path}") from exc
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        raise RootSnapshotError(f"non-canonical root-relative path: {relative!r}")
    return relative


def _discover_partitioned_dataset(
    root: Path,
    dataset: str,
    *,
    start: dt.date,
    end: dt.date,
) -> list[_ObservedFile]:
    dataset_root = root / dataset
    if not dataset_root.is_dir() or dataset_root.is_symlink():
        raise RootSnapshotError(f"required dataset directory is absent or a symlink: {dataset_root}")
    rows: list[_ObservedFile] = []
    dates_seen: set[str] = set()
    for path in sorted(dataset_root.rglob("*.parquet"), key=lambda value: value.as_posix()):
        observed = _regular_file_stat(path, label=f"{dataset} source file")
        date_value = _partition_date(path, dataset_root=dataset_root)
        date = dt.date.fromisoformat(date_value)
        if not start <= date < end:
            continue
        dates_seen.add(date_value)
        rows.append(
            _ObservedFile(
                dataset=dataset,
                relative_path=_relative_path(root, path),
                date=date_value,
                bytes=observed.st_size,
                mode=stat.S_IMODE(observed.st_mode),
                mtime_ns=observed.st_mtime_ns,
                inode=observed.st_ino,
            )
        )
    missing_dates = sorted(set(_date_range(start, end)) - dates_seen)
    if missing_dates:
        raise RootSnapshotError(
            f"{dataset} is missing {len(missing_dates)} required date partitions; sample={missing_dates[:10]}"
        )
    if not rows:
        raise RootSnapshotError(f"{dataset} contains no registered-window parquet files")
    return rows


def _discover_single_file(root: Path, relative: str) -> _ObservedFile:
    path = root / relative
    observed = _regular_file_stat(path, label=relative)
    return _ObservedFile(
        dataset=relative,
        relative_path=relative,
        date=None,
        bytes=observed.st_size,
        mode=stat.S_IMODE(observed.st_mode),
        mtime_ns=observed.st_mtime_ns,
        inode=observed.st_ino,
    )


def _discover(root: Path, window: RootSnapshotWindow) -> list[_ObservedFile]:
    # The byte snapshot starts before the feature warmup because LONG age is an
    # identity derived from full manifest-covered root history.  Numeric feature
    # loaders may still apply ``causal_read_start_date`` later.
    start = dt.date.fromisoformat(window.identity_history_start_date)
    endpoints = {
        "signal_end_date_exclusive": dt.date.fromisoformat(window.signal_end_date_exclusive),
        "label_end_date_exclusive": dt.date.fromisoformat(window.label_end_date_exclusive),
    }
    rows: list[_ObservedFile] = []
    for dataset, endpoint_name in _PARTITIONED_DATASETS.items():
        rows.extend(
            _discover_partitioned_dataset(
                root,
                dataset,
                start=start,
                end=endpoints[endpoint_name],
            )
        )
    rows.extend(_discover_single_file(root, relative) for relative in _SINGLE_FILE_DATASETS)
    rows.sort(key=lambda row: (row.dataset, row.relative_path))
    relative_paths = [row.relative_path for row in rows]
    if len(relative_paths) != len(set(relative_paths)):
        raise RootSnapshotError("root snapshot discovery produced duplicate relative paths")
    return rows


def _observation_identity(rows: Sequence[_ObservedFile]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (row.dataset, row.relative_path, row.date, row.bytes, row.mode, row.mtime_ns, row.inode)
        for row in rows
    )


def _validate_phase0_receipt(
    path: Path,
    *,
    venue: str,
    root: Path,
    window: RootSnapshotWindow,
) -> tuple[dict[str, Any], str]:
    try:
        verified = verify_phase0_bundle(
            path,
            require_ready=True,
            expected_venue=venue,
            expected_root=root,
            expected_window=dataclasses.asdict(window),
        )
    except Phase0BundleVerificationError as exc:
        raise RootSnapshotError(f"strict Phase-0 semantic verification failed: {exc}") from exc
    return dict(verified.receipt), verified.receipt_sha256


def _assert_phase0_bundle_bytes_unchanged(path: Path, payload: Mapping[str, Any], receipt_sha256: str) -> None:
    if _file_sha256(path) != receipt_sha256:
        raise RootSnapshotError("Phase-0 receipt changed during root snapshot")
    raw_rows = payload.get("files")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise RootSnapshotError("verified Phase-0 receipt lost its file inventory")
    expected_names = {str(row.get("path")) for row in raw_rows} | {"receipt.json"}
    actual_names = {entry.name for entry in path.parent.iterdir()}
    if actual_names != expected_names:
        raise RootSnapshotError("Phase-0 bundle directory changed during root snapshot")
    for row in raw_rows:
        artifact = path.parent / str(row["path"])
        observed = _regular_file_stat(artifact, label=f"Phase-0 artifact {row['path']}")
        if observed.st_size != row.get("bytes") or _file_sha256(artifact) != row.get("sha256"):
            raise RootSnapshotError(f"Phase-0 artifact changed during root snapshot: {row['path']}")


def _verify_local_identity_history_boundary(root: Path, window: RootSnapshotWindow) -> None:
    """Refuse a caller-selected truncation of locally present identity history.

    This proves only the earliest partition present in the bound local root.  It
    cannot prove that the upstream venue/archive has no earlier recoverable
    history, so ``earliest_root_history_proven`` remains false in the receipt.
    """

    expected = window.identity_history_start_date
    for dataset in _PARTITIONED_DATASETS:
        dataset_root = root / dataset
        if not dataset_root.is_dir() or dataset_root.is_symlink():
            raise RootSnapshotError(f"required dataset directory is absent or a symlink: {dataset_root}")
        observed_dates: list[str] = []
        for partition in dataset_root.glob("date=*"):
            if partition.is_symlink() or not partition.is_dir():
                raise RootSnapshotError(f"{dataset} partition must be a non-symlink directory: {partition}")
            date_value = partition.name.removeprefix("date=")
            _parse_date(date_value, name=f"{dataset} partition date")
            if any(path.is_file() and not path.is_symlink() for path in partition.rglob("*.parquet")):
                observed_dates.append(date_value)
        if not observed_dates:
            raise RootSnapshotError(f"{dataset} has no parquet-backed date partitions")
        earliest = min(observed_dates)
        if earliest != expected:
            raise RootSnapshotError(
                f"root snapshot identity_history_start_date must equal the earliest local {dataset} "
                f"partition: expected {earliest}, got {expected}"
            )


def _manifest_rows(rows: Sequence[_ObservedFile], root: Path) -> tuple[list[dict[str, Any]], bytes]:
    output: list[dict[str, Any]] = []
    for observed in rows:
        path = root / observed.relative_path
        before = _regular_file_stat(path, label=observed.relative_path)
        digest = _file_sha256(path)
        after = _regular_file_stat(path, label=observed.relative_path)
        stable = (
            before.st_size,
            stat.S_IMODE(before.st_mode),
            before.st_mtime_ns,
            before.st_ino,
        ) == (
            after.st_size,
            stat.S_IMODE(after.st_mode),
            after.st_mtime_ns,
            after.st_ino,
        )
        if not stable or _observation_identity((observed,))[0][3:] != (
            after.st_size,
            stat.S_IMODE(after.st_mode),
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise RootSnapshotError(f"source file changed while hashing: {observed.relative_path}")
        output.append(
            {
                "dataset": observed.dataset,
                "relative_path": observed.relative_path,
                "date": observed.date,
                "bytes": observed.bytes,
                "mode": f"{observed.mode:04o}",
                "sha256": digest,
            }
        )
    manifest = b"".join(_canonical_json(row) + b"\n" for row in output)
    return output, manifest


def _dataset_summaries(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    datasets = sorted({str(row["dataset"]) for row in rows})
    return [
        {
            "dataset": dataset,
            "file_count": sum(row["dataset"] == dataset for row in rows),
            "total_bytes": sum(int(row["bytes"]) for row in rows if row["dataset"] == dataset),
            "date_count": len({row["date"] for row in rows if row["dataset"] == dataset and row["date"]}),
        }
        for dataset in datasets
    ]


def build_root_snapshot(
    root: str | Path,
    *,
    venue: str,
    window: RootSnapshotWindow,
    phase0_bundle_receipt: str | Path,
) -> RootSnapshotArtifacts:
    """Hash one quiescent registered root without decoding market values."""

    if venue not in SUPPORTED_VENUES:
        raise ValueError(f"venue must be one of {sorted(SUPPORTED_VENUES)}")
    supplied_root = Path(root).expanduser()
    if supplied_root.is_symlink():
        raise RootSnapshotError(f"root must not be a symlink: {supplied_root}")
    root_path = supplied_root.resolve()
    if not root_path.is_dir():
        raise RootSnapshotError(f"root must be an existing non-symlink directory: {root_path}")
    supplied_phase0 = Path(phase0_bundle_receipt).expanduser()
    if supplied_phase0.is_symlink():
        raise RootSnapshotError(f"Phase-0 receipt must not be a symlink: {supplied_phase0}")
    phase0_path = supplied_phase0.resolve()
    phase0_payload, phase0_sha256 = _validate_phase0_receipt(
        phase0_path,
        venue=venue,
        root=root_path,
        window=window,
    )
    _verify_local_identity_history_boundary(root_path, window)

    discovered_before = _discover(root_path, window)
    manifest_rows, manifest = _manifest_rows(discovered_before, root_path)
    discovered_after = _discover(root_path, window)
    if _observation_identity(discovered_before) != _observation_identity(discovered_after):
        raise RootSnapshotError("registered root file inventory changed during snapshot")
    _assert_phase0_bundle_bytes_unchanged(phase0_path, phase0_payload, phase0_sha256)

    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    identity = {
        "schema_version": ROOT_SNAPSHOT_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_root_snapshot_identity",
        "venue": venue,
        "window": dataclasses.asdict(window),
        "phase0_id": phase0_payload.get("phase0_id"),
        "phase0_bundle_receipt_sha256": phase0_sha256,
        "file_manifest_sha256": manifest_sha256,
        "file_count": len(manifest_rows),
        "total_bytes": sum(int(row["bytes"]) for row in manifest_rows),
        "datasets": _dataset_summaries(manifest_rows),
    }
    identity_sha256 = _json_sha256(identity)
    receipt: dict[str, Any] = {
        "schema_version": ROOT_SNAPSHOT_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_root_snapshot",
        "status": "BYTE_SNAPSHOT_ONLY",
        "venue": venue,
        "root_path": str(root_path),
        "window": dataclasses.asdict(window),
        "phase0_bundle_receipt_path": str(phase0_path),
        "phase0_bundle_receipt_sha256": phase0_sha256,
        "file_manifest_format": "canonical_json_lines_utf8",
        "file_manifest_sha256": manifest_sha256,
        "file_count": len(manifest_rows),
        "total_bytes": sum(int(row["bytes"]) for row in manifest_rows),
        "datasets": identity["datasets"],
        "snapshot_chain_identity_sha256": identity_sha256,
        "identity_path_independent": False,
        "path_independence_limitation": (
            "the chain includes the Phase-0 receipt hash, whose observed-root identity may contain absolute paths"
        ),
        "phase0_bundle_bytes_verified": True,
        "phase0_internal_reexecution_verified": True,
        "phase0_semantics_fully_verified": False,
        "source_authenticity_proven": False,
        "full_process_environment_identity_proven": False,
        "upstream_root_lineage_proven": False,
        "registered_scope_verified": False,
        "earliest_root_history_proven": False,
        "registered_s01_ready": False,
        "readiness_limitation": (
            "internal Phase-0 derivations and exact boundary/path bytes were rechecked, but source authenticity, "
            "full process-environment identity, upstream root lineage, label-tail Parquet/value semantics, and "
            "earliest-history completeness remain unproven; S01 cannot treat this as a canonical root receipt"
        ),
        "numeric_values_decoded": False,
        "file_bytes_hashed": True,
        "returns_calculated": False,
        "labels_calculated": False,
        "outcome_run_authorized": False,
        "real_money_authorized": False,
    }
    receipt["artifact_sha256"] = _json_sha256(receipt)
    return RootSnapshotArtifacts(receipt=MappingProxyType(receipt), file_manifest_jsonl=manifest)


def _parse_manifest(manifest: bytes) -> list[dict[str, Any]]:
    if not manifest or not manifest.endswith(b"\n"):
        raise RootSnapshotError("root snapshot file manifest must be non-empty newline-terminated JSONL")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(manifest.splitlines(), start=1):
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RootSnapshotError(f"invalid root snapshot manifest row {index}: {exc}") from exc
        if not isinstance(row, dict) or _canonical_json(row) != raw:
            raise RootSnapshotError(f"root snapshot manifest row {index} is not canonical JSON")
        if set(row) != {"dataset", "relative_path", "date", "bytes", "mode", "sha256"}:
            raise RootSnapshotError(f"root snapshot manifest row {index} has an invalid schema")
        relative = str(row["relative_path"])
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
            raise RootSnapshotError(f"root snapshot manifest row {index} has a non-canonical path")
        rows.append(row)
    if rows != sorted(rows, key=lambda row: (str(row["dataset"]), str(row["relative_path"]))):
        raise RootSnapshotError("root snapshot manifest rows are not in canonical order")
    if len({str(row["relative_path"]) for row in rows}) != len(rows):
        raise RootSnapshotError("root snapshot manifest contains duplicate paths")
    return rows


def verify_root_snapshot(
    receipt: Mapping[str, Any],
    file_manifest_jsonl: bytes,
    *,
    root: str | Path | None = None,
    phase0_bundle_receipt: str | Path | None = None,
) -> None:
    """Rehash every bound input and fail on any receipt, path, or byte drift."""

    original = dict(receipt)
    if set(original) != set(_ROOT_RECEIPT_KEYS):
        raise RootSnapshotError(
            "root snapshot receipt schema mismatch; "
            f"missing={sorted(_ROOT_RECEIPT_KEYS - set(original))}, "
            f"unknown={sorted(set(original) - _ROOT_RECEIPT_KEYS)}"
        )
    payload = dict(original)
    observed_artifact_sha256 = payload.pop("artifact_sha256", None)
    if observed_artifact_sha256 != _json_sha256(payload):
        raise RootSnapshotError("root snapshot artifact SHA-256 mismatch")
    expected_constants = {
        "schema_version": ROOT_SNAPSHOT_SCHEMA_VERSION,
        "artifact_type": "strategy_overhaul_root_snapshot",
        "status": "BYTE_SNAPSHOT_ONLY",
        "file_manifest_format": "canonical_json_lines_utf8",
        "identity_path_independent": False,
        "phase0_bundle_bytes_verified": True,
        "phase0_internal_reexecution_verified": True,
        "phase0_semantics_fully_verified": False,
        "source_authenticity_proven": False,
        "full_process_environment_identity_proven": False,
        "upstream_root_lineage_proven": False,
        "registered_scope_verified": False,
        "earliest_root_history_proven": False,
        "registered_s01_ready": False,
        "numeric_values_decoded": False,
        "file_bytes_hashed": True,
        "returns_calculated": False,
        "labels_calculated": False,
        "outcome_run_authorized": False,
        "real_money_authorized": False,
    }
    for name, expected in expected_constants.items():
        if payload.get(name) != expected:
            raise RootSnapshotError(f"root snapshot receipt {name} must equal {expected!r}")
    if hashlib.sha256(file_manifest_jsonl).hexdigest() != payload.get("file_manifest_sha256"):
        raise RootSnapshotError("root snapshot file-manifest SHA-256 mismatch")
    rows = _parse_manifest(file_manifest_jsonl)
    if len(rows) != payload.get("file_count") or sum(int(row["bytes"]) for row in rows) != payload.get("total_bytes"):
        raise RootSnapshotError("root snapshot file-manifest counts disagree with receipt")
    if payload.get("datasets") != _dataset_summaries(rows):
        raise RootSnapshotError("root snapshot dataset summaries disagree with file manifest")

    supplied_root = Path(root or str(payload.get("root_path"))).expanduser()
    if supplied_root.is_symlink():
        raise RootSnapshotError(f"root must not be a symlink: {supplied_root}")
    root_path = supplied_root.resolve()
    supplied_phase0 = Path(
        phase0_bundle_receipt or str(payload.get("phase0_bundle_receipt_path"))
    ).expanduser()
    if supplied_phase0.is_symlink():
        raise RootSnapshotError(f"Phase-0 receipt must not be a symlink: {supplied_phase0}")
    phase0_path = supplied_phase0.resolve()
    _phase0_payload, phase0_sha256 = _validate_phase0_receipt(
        phase0_path,
        venue=str(payload["venue"]),
        root=root_path,
        window=RootSnapshotWindow(**dict(payload["window"])),
    )
    if phase0_sha256 != payload.get("phase0_bundle_receipt_sha256"):
        raise RootSnapshotError("root snapshot Phase-0 receipt SHA-256 mismatch")
    for row in rows:
        path = root_path / str(row["relative_path"])
        observed = _regular_file_stat(path, label=str(row["relative_path"]))
        if observed.st_size != row["bytes"] or f"{stat.S_IMODE(observed.st_mode):04o}" != row["mode"]:
            raise RootSnapshotError(f"root snapshot file metadata mismatch: {row['relative_path']}")
        if _file_sha256(path) != row["sha256"]:
            raise RootSnapshotError(f"root snapshot file SHA-256 mismatch: {row['relative_path']}")

    rebuilt = build_root_snapshot(
        root_path,
        venue=str(payload["venue"]),
        window=RootSnapshotWindow(**dict(payload["window"])),
        phase0_bundle_receipt=phase0_path,
    )
    if rebuilt.file_manifest_jsonl != file_manifest_jsonl:
        raise RootSnapshotError("root snapshot manifest no longer matches registered files")
    if dict(rebuilt.receipt) != original:
        raise RootSnapshotError("root snapshot receipt no longer equals the fully rebuilt receipt")


__all__ = [
    "ROOT_SNAPSHOT_SCHEMA_VERSION",
    "RootSnapshotArtifacts",
    "RootSnapshotError",
    "RootSnapshotWindow",
    "build_root_snapshot",
    "verify_root_snapshot",
]
