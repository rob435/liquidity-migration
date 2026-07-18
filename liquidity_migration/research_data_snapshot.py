"""Create-only content snapshots for decision-influencing research inputs.

The ordinary research roots are mutable working datasets.  This module turns a
declared subset into one SQLite content container whose logical file manifest
and physical bytes are independently hashed.  A published container is opened
read-only and can be reconstructed into a new run-scoped root; feature builders
never need to trust the mutable source after publication.

Snapshotting is deliberately independent of :mod:`liquidity_migration.storage`
so the outcome-blind capture path works on Windows without pretending to offer
POSIX dataset-lock semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .deterministic_serialization import canonical_json


SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_DATASETS = (
    "archive_trade_manifest",
    "klines_1h",
    "klines_5m",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
)
_DATE_PARTITION = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_READ_CHUNK = 1024 * 1024


class ResearchSnapshotError(RuntimeError):
    """The declared snapshot cannot be proved or reconstructed."""


@dataclass(frozen=True, slots=True)
class PlannedFile:
    relative_path: str
    dataset: str
    size: int
    mtime_ns: int
    mode: int

    def identity(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "relative_path": self.relative_path,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class SnapshotPlan:
    source_root: Path
    start: date
    end: date
    datasets: tuple[str, ...]
    files: tuple[PlannedFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(row.size for row in self.files)

    def counts_by_dataset(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
        for row in self.files:
            counts[row.dataset]["files"] += 1
            counts[row.dataset]["bytes"] += row.size
        return {key: counts[key] for key in sorted(counts)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_lstat(path: Path, *, label: str, require_directory: bool = False) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ResearchSnapshotError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ResearchSnapshotError(f"{label} must not be a symlink or reparse point: {path}")
    if require_directory and not stat.S_ISDIR(metadata.st_mode):
        raise ResearchSnapshotError(f"{label} must be a directory: {path}")
    return metadata


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _normalize_relative(path: Path, *, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ResearchSnapshotError(f"snapshot input escaped its source root: {path}") from exc
    pure = PurePosixPath(*relative.parts)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ResearchSnapshotError(f"unsafe snapshot-relative path: {pure}")
    normalized = pure.as_posix()
    if "\x00" in normalized:
        raise ResearchSnapshotError("snapshot-relative path contains NUL")
    return normalized


def _walk_parquet_files(root: Path) -> Iterator[Path]:
    """Walk one selected dataset subtree without following reparse directories."""

    _safe_lstat(root, label="snapshot dataset directory", require_directory=True)
    for current_raw, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        safe_directories: list[str] = []
        for name in sorted(directories):
            candidate = current / name
            _safe_lstat(candidate, label="snapshot dataset subdirectory", require_directory=True)
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in sorted(filenames):
            if not name.lower().endswith(".parquet"):
                continue
            candidate = current / name
            metadata = _safe_lstat(candidate, label="snapshot input file")
            if not stat.S_ISREG(metadata.st_mode):
                raise ResearchSnapshotError(f"snapshot input must be a regular file: {candidate}")
            yield candidate


def _selected_partition_roots(
    dataset_root: Path,
    *,
    dataset: str,
    start: date,
    end: date,
) -> Iterator[Path]:
    if not dataset_root.exists():
        return
    _safe_lstat(dataset_root, label=f"{dataset} dataset", require_directory=True)
    children = sorted(dataset_root.iterdir(), key=lambda path: path.name)
    for child in children:
        metadata = _safe_lstat(child, label=f"{dataset} dataset entry")
        if not stat.S_ISDIR(metadata.st_mode):
            if child.suffix.lower() == ".parquet":
                raise ResearchSnapshotError(
                    f"{dataset} contains an unpartitioned parquet outside the declared date boundary: {child}"
                )
            continue
        match = _DATE_PARTITION.fullmatch(child.name)
        if match is None:
            raise ResearchSnapshotError(
                f"{dataset} contains an unrecognized directory that cannot be time-bounded: {child}"
            )
        partition_day = date.fromisoformat(match.group(1))
        if dataset == "archive_trade_manifest" or start <= partition_day < end:
            yield child


def build_snapshot_plan(
    source_root: str | Path,
    *,
    start: date,
    end: date,
    datasets: Sequence[str] = DEFAULT_DATASETS,
) -> SnapshotPlan:
    """Inventory the exact files in a snapshot without reading outcome values."""

    if end <= start:
        raise ValueError("snapshot end must be after start")
    root = Path(source_root).expanduser().resolve(strict=True)
    _safe_lstat(root, label="snapshot source root", require_directory=True)
    normalized_datasets = tuple(sorted(dict.fromkeys(str(value).strip() for value in datasets)))
    if not normalized_datasets or any(not value or "/" in value or "\\" in value for value in normalized_datasets):
        raise ValueError("snapshot datasets must be non-empty top-level names")

    planned: list[PlannedFile] = []
    seen: set[str] = set()
    for dataset in normalized_datasets:
        dataset_root = root / dataset
        for partition in _selected_partition_roots(
            dataset_root,
            dataset=dataset,
            start=start,
            end=end,
        ):
            for path in _walk_parquet_files(partition):
                metadata = path.lstat()
                relative = _normalize_relative(path, root=root)
                if relative in seen:
                    raise ResearchSnapshotError(f"duplicate snapshot-relative path: {relative}")
                seen.add(relative)
                planned.append(
                    PlannedFile(
                        relative_path=relative,
                        dataset=dataset,
                        size=int(metadata.st_size),
                        mtime_ns=int(metadata.st_mtime_ns),
                        mode=stat.S_IMODE(metadata.st_mode),
                    )
                )
    planned.sort(key=lambda row: row.relative_path)
    if not planned:
        raise ResearchSnapshotError("snapshot plan selected no parquet inputs")
    return SnapshotPlan(
        source_root=root,
        start=start,
        end=end,
        datasets=normalized_datasets,
        files=tuple(planned),
    )


def plan_payload(plan: SnapshotPlan) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": "research_data_snapshot_plan",
        "source_root": str(plan.source_root),
        "start": plan.start.isoformat(),
        "end_exclusive": plan.end.isoformat(),
        "datasets": list(plan.datasets),
        "file_count": len(plan.files),
        "total_bytes": plan.total_bytes,
        "by_dataset": plan.counts_by_dataset(),
        "outcomes_inspected": False,
    }


def _set_metadata(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
        (key, canonical_json({"value": value}).decode("utf-8")),
    )


def _get_metadata(connection: sqlite3.Connection, key: str) -> Any:
    row = connection.execute("SELECT value_json FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise ResearchSnapshotError(f"snapshot container lacks metadata {key!r}")
    parsed = json.loads(str(row[0]))
    if not isinstance(parsed, dict) or set(parsed) != {"value"}:
        raise ResearchSnapshotError(f"snapshot metadata {key!r} is malformed")
    return parsed["value"]


def _initialize_container(
    path: Path,
    *,
    plan: SnapshotPlan,
    contract_sha256: str,
    code_commit: str,
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60.0)
    try:
        connection.execute("PRAGMA page_size = 32768")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE planned_files (
                relative_path TEXT PRIMARY KEY,
                dataset TEXT NOT NULL,
                size INTEGER NOT NULL CHECK(size >= 0),
                mtime_ns INTEGER NOT NULL CHECK(mtime_ns >= 0),
                mode INTEGER NOT NULL CHECK(mode >= 0)
            ) WITHOUT ROWID;
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                dataset TEXT NOT NULL,
                size INTEGER NOT NULL CHECK(size >= 0),
                mtime_ns INTEGER NOT NULL CHECK(mtime_ns >= 0),
                mode INTEGER NOT NULL CHECK(mode >= 0),
                sha256 TEXT NOT NULL,
                content BLOB NOT NULL
            );
            """
        )
        _set_metadata(connection, "schema_version", SNAPSHOT_SCHEMA_VERSION)
        _set_metadata(connection, "status", "capturing")
        _set_metadata(connection, "source_root", str(plan.source_root))
        _set_metadata(connection, "start", plan.start.isoformat())
        _set_metadata(connection, "end_exclusive", plan.end.isoformat())
        _set_metadata(connection, "datasets", list(plan.datasets))
        _set_metadata(connection, "contract_sha256", contract_sha256)
        _set_metadata(connection, "code_commit", code_commit)
        _set_metadata(connection, "created_at", _utc_now())
        connection.executemany(
            "INSERT INTO planned_files(relative_path, dataset, size, mtime_ns, mode) VALUES (?, ?, ?, ?, ?)",
            (
                (row.relative_path, row.dataset, row.size, row.mtime_ns, row.mode)
                for row in plan.files
            ),
        )
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def _validate_container_config(
    connection: sqlite3.Connection,
    *,
    plan: SnapshotPlan,
    contract_sha256: str,
    code_commit: str,
) -> None:
    expected = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source_root": str(plan.source_root),
        "start": plan.start.isoformat(),
        "end_exclusive": plan.end.isoformat(),
        "datasets": list(plan.datasets),
        "contract_sha256": contract_sha256,
        "code_commit": code_commit,
    }
    for key, value in expected.items():
        if _get_metadata(connection, key) != value:
            raise ResearchSnapshotError(f"working snapshot metadata changed for {key}")
    if _get_metadata(connection, "status") != "capturing":
        raise ResearchSnapshotError("working snapshot is not resumable capturing state")
    planned_rows = connection.execute(
        "SELECT relative_path, dataset, size, mtime_ns, mode FROM planned_files ORDER BY relative_path"
    )
    for stored, expected_row in zip(planned_rows, plan.files, strict=True):
        if tuple(stored) != (
            expected_row.relative_path,
            expected_row.dataset,
            expected_row.size,
            expected_row.mtime_ns,
            expected_row.mode,
        ):
            raise ResearchSnapshotError("working snapshot planned inventory changed")


def _open_stable_source(path: Path, *, expected: PlannedFile) -> tuple[BinaryIO, os.stat_result]:
    before_path = _safe_lstat(path, label="snapshot source file")
    if not stat.S_ISREG(before_path.st_mode):
        raise ResearchSnapshotError(f"snapshot source is not a regular file: {path}")
    actual = (int(before_path.st_size), int(before_path.st_mtime_ns), stat.S_IMODE(before_path.st_mode))
    planned = (expected.size, expected.mtime_ns, expected.mode)
    if actual != planned:
        raise ResearchSnapshotError(f"snapshot source metadata changed after inventory: {expected.relative_path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    handle = os.fdopen(descriptor, "rb", closefd=True)
    before_descriptor = os.fstat(handle.fileno())
    if _file_signature(before_descriptor) != _file_signature(before_path):
        handle.close()
        raise ResearchSnapshotError(f"snapshot source path changed while opening: {expected.relative_path}")
    return handle, before_descriptor


def _capture_one(
    connection: sqlite3.Connection,
    *,
    plan: SnapshotPlan,
    row: PlannedFile,
) -> str:
    path = plan.source_root.joinpath(*PurePosixPath(row.relative_path).parts)
    handle, before = _open_stable_source(path, expected=row)
    digest = hashlib.sha256()
    cursor = connection.execute(
        """
        INSERT INTO files(relative_path, dataset, size, mtime_ns, mode, sha256, content)
        VALUES (?, ?, ?, ?, ?, ?, zeroblob(?))
        """,
        (row.relative_path, row.dataset, row.size, row.mtime_ns, row.mode, "0" * 64, row.size),
    )
    if cursor.lastrowid is None:
        raise ResearchSnapshotError("snapshot container did not assign a file row ID")
    row_id = int(cursor.lastrowid)
    blob = connection.blobopen("files", "content", row_id, readonly=False)
    read_size = 0
    try:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            read_size += len(chunk)
            digest.update(chunk)
            blob.write(chunk)
        after_descriptor = os.fstat(handle.fileno())
    finally:
        blob.close()
        handle.close()
    after_path = _safe_lstat(path, label="snapshot source file")
    if (
        _file_signature(before) != _file_signature(after_descriptor)
        or _file_signature(before) != _file_signature(after_path)
        or read_size != row.size
    ):
        raise ResearchSnapshotError(f"snapshot source changed while read: {row.relative_path}")
    file_hash = digest.hexdigest()
    connection.execute("UPDATE files SET sha256 = ? WHERE id = ?", (file_hash, row_id))
    return file_hash


def _logical_hash(connection: sqlite3.Connection) -> tuple[str, dict[str, dict[str, Any]]]:
    digest = hashlib.sha256()
    per_dataset: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"files": 0, "bytes": 0, "logical_sha256": hashlib.sha256()}
    )
    rows = connection.execute(
        "SELECT relative_path, dataset, size, mtime_ns, mode, sha256 FROM files ORDER BY relative_path"
    )
    for relative, dataset, size, mtime_ns, mode, file_hash in rows:
        identity = {
            "dataset": str(dataset),
            "mode": int(mode),
            "mtime_ns": int(mtime_ns),
            "relative_path": str(relative),
            "sha256": str(file_hash),
            "size": int(size),
        }
        line = canonical_json(identity) + b"\n"
        digest.update(line)
        group = per_dataset[str(dataset)]
        group["files"] += 1
        group["bytes"] += int(size)
        group["logical_sha256"].update(line)
    summary: dict[str, dict[str, Any]] = {}
    for dataset in sorted(per_dataset):
        group = per_dataset[dataset]
        summary[dataset] = {
            "files": int(group["files"]),
            "bytes": int(group["bytes"]),
            "logical_sha256": group["logical_sha256"].hexdigest(),
        }
    return digest.hexdigest(), summary


def _verify_source_against_container(
    connection: sqlite3.Connection,
    *,
    plan: SnapshotPlan,
    progress_every: int,
) -> None:
    current = build_snapshot_plan(
        plan.source_root,
        start=plan.start,
        end=plan.end,
        datasets=plan.datasets,
    )
    if current.files != plan.files:
        raise ResearchSnapshotError("source inventory changed before snapshot publication")
    expected_hashes = {
        str(relative): str(file_hash)
        for relative, file_hash in connection.execute(
            "SELECT relative_path, sha256 FROM files ORDER BY relative_path"
        )
    }
    verified = 0
    for row in plan.files:
        path = plan.source_root.joinpath(*PurePosixPath(row.relative_path).parts)
        handle, before = _open_stable_source(path, expected=row)
        digest = hashlib.sha256()
        read_size = 0
        try:
            for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
                read_size += len(chunk)
                digest.update(chunk)
            after_descriptor = os.fstat(handle.fileno())
        finally:
            handle.close()
        after_path = _safe_lstat(path, label="snapshot verification source")
        if (
            _file_signature(before) != _file_signature(after_descriptor)
            or _file_signature(before) != _file_signature(after_path)
            or read_size != row.size
            or digest.hexdigest() != expected_hashes.get(row.relative_path)
        ):
            raise ResearchSnapshotError(f"source bytes changed before publication: {row.relative_path}")
        verified += 1
        if progress_every > 0 and verified % progress_every == 0:
            print(f"source-verify files={verified}/{len(plan.files)}", flush=True)


def _publish_noreplace(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"immutable snapshot already exists: {destination}")
    if os.name == "nt":
        os.rename(source, destination)
        return
    from .artifact_snapshot import rename_noreplace

    rename_noreplace(source, destination, label="research snapshot")


def _write_create_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o444)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("create-only evidence write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, stat.S_IREAD)


def capture_snapshot(
    plan: SnapshotPlan,
    *,
    output: str | Path,
    receipt_path: str | Path,
    contract_sha256: str,
    code_commit: str,
    batch_size: int = 500,
    progress_every: int = 10_000,
) -> dict[str, Any]:
    """Capture, re-read, publish, and verify one immutable content container."""

    if not _SHA256_RE.fullmatch(contract_sha256):
        raise ValueError("contract_sha256 must be lowercase SHA-256")
    if not re.fullmatch(r"[0-9a-f]{40}", code_commit):
        raise ValueError("code_commit must be a full lowercase Git commit")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    final = Path(output).expanduser().resolve(strict=False)
    receipt = Path(receipt_path).expanduser().resolve(strict=False)
    working = final.with_name(f".{final.name}.working")
    if final.exists() or receipt.exists():
        raise FileExistsError("snapshot output and receipt are create-only")
    if final == receipt or working == receipt:
        raise ValueError("snapshot container and receipt paths must differ")
    try:
        final.relative_to(plan.source_root)
    except ValueError:
        pass
    else:
        raise ValueError("snapshot output must be outside the mutable source root")

    if working.exists():
        connection = sqlite3.connect(working, timeout=60.0)
        _validate_container_config(
            connection,
            plan=plan,
            contract_sha256=contract_sha256,
            code_commit=code_commit,
        )
    else:
        connection = _initialize_container(
            working,
            plan=plan,
            contract_sha256=contract_sha256,
            code_commit=code_commit,
        )

    try:
        missing_rows = connection.execute(
            """
            SELECT p.relative_path, p.dataset, p.size, p.mtime_ns, p.mode
            FROM planned_files AS p
            LEFT JOIN files AS f ON f.relative_path = p.relative_path
            WHERE f.id IS NULL
            ORDER BY p.relative_path
            """
        )
        captured = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        connection.execute("BEGIN IMMEDIATE")
        in_batch = 0
        for raw in missing_rows:
            row = PlannedFile(
                relative_path=str(raw[0]),
                dataset=str(raw[1]),
                size=int(raw[2]),
                mtime_ns=int(raw[3]),
                mode=int(raw[4]),
            )
            _capture_one(connection, plan=plan, row=row)
            captured += 1
            in_batch += 1
            if in_batch >= batch_size:
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                in_batch = 0
            if progress_every > 0 and captured % progress_every == 0:
                print(
                    f"capture files={captured}/{len(plan.files)} bytes={row.size}",
                    flush=True,
                )
        connection.commit()
        actual_count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        if actual_count != len(plan.files):
            raise ResearchSnapshotError(
                f"snapshot capture is incomplete: {actual_count} != {len(plan.files)}"
            )
        print("capture complete; starting independent source re-read", flush=True)
        _verify_source_against_container(
            connection,
            plan=plan,
            progress_every=progress_every,
        )
        logical_sha256, by_dataset = _logical_hash(connection)
        _set_metadata(connection, "logical_sha256", logical_sha256)
        _set_metadata(connection, "by_dataset", by_dataset)
        _set_metadata(connection, "file_count", len(plan.files))
        _set_metadata(connection, "total_bytes", plan.total_bytes)
        _set_metadata(connection, "source_verified_at", _utc_now())
        _set_metadata(connection, "status", "complete")
        connection.commit()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ResearchSnapshotError(f"snapshot SQLite integrity failed: {integrity}")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.commit()
    finally:
        connection.close()

    for sidecar in (Path(str(working) + "-wal"), Path(str(working) + "-shm")):
        if sidecar.exists():
            raise ResearchSnapshotError(f"snapshot staging sidecar remained after finalization: {sidecar}")
    _publish_noreplace(working, final)
    os.chmod(final, stat.S_IREAD)
    container_sha256 = _sha256_file(final)
    receipt_payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": "immutable_research_data_snapshot",
        "created_at": _utc_now(),
        "source_root": str(plan.source_root),
        "window": {
            "start": plan.start.isoformat(),
            "end_exclusive": plan.end.isoformat(),
            "manifest_history": "all_available_date_partitions",
        },
        "datasets": list(plan.datasets),
        "file_count": len(plan.files),
        "total_bytes": plan.total_bytes,
        "by_dataset": by_dataset,
        "logical_sha256": logical_sha256,
        "container": {
            "path": str(final),
            "bytes": final.stat().st_size,
            "sha256": container_sha256,
        },
        "contract_sha256": contract_sha256,
        "code_commit": code_commit,
        "source_verification": "full_second_inventory_and_sha256_reread",
        "outcomes_inspected": False,
    }
    _write_create_only(receipt, canonical_json(receipt_payload) + b"\n")
    verified = verify_snapshot(final, receipt_path=receipt, full_content=True)
    print(
        f"published snapshot logical_sha256={logical_sha256} files={len(plan.files)}",
        flush=True,
    )
    return verified


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchSnapshotError(f"snapshot receipt is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ResearchSnapshotError("snapshot receipt must be a JSON object")
    return value


def _open_readonly_container(path: Path) -> sqlite3.Connection:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=60.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def verify_snapshot(
    container_path: str | Path,
    *,
    receipt_path: str | Path,
    full_content: bool = True,
    progress_every: int = 10_000,
) -> dict[str, Any]:
    """Verify physical, logical, schema, and optionally every content hash."""

    container = Path(container_path).expanduser().resolve(strict=True)
    receipt_file = Path(receipt_path).expanduser().resolve(strict=True)
    receipt = _load_receipt(receipt_file)
    if receipt.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ResearchSnapshotError("snapshot receipt schema is unsupported")
    if receipt.get("kind") != "immutable_research_data_snapshot":
        raise ResearchSnapshotError("snapshot receipt has the wrong kind")
    container_identity = receipt.get("container")
    if not isinstance(container_identity, dict):
        raise ResearchSnapshotError("snapshot receipt lacks container identity")
    actual_container_hash = _sha256_file(container)
    if container_identity.get("sha256") != actual_container_hash:
        raise ResearchSnapshotError("snapshot container SHA-256 mismatch")
    if int(container_identity.get("bytes") or -1) != container.stat().st_size:
        raise ResearchSnapshotError("snapshot container size mismatch")

    connection = _open_readonly_container(container)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ResearchSnapshotError(f"snapshot SQLite integrity failed: {integrity}")
        if _get_metadata(connection, "status") != "complete":
            raise ResearchSnapshotError("snapshot container is not complete")
        logical_sha256, by_dataset = _logical_hash(connection)
        count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        total_bytes = int(connection.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0])
        expected = {
            "logical_sha256": logical_sha256,
            "file_count": count,
            "total_bytes": total_bytes,
            "by_dataset": by_dataset,
            "contract_sha256": _get_metadata(connection, "contract_sha256"),
            "code_commit": _get_metadata(connection, "code_commit"),
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                raise ResearchSnapshotError(f"snapshot receipt/container mismatch for {key}")
        if full_content:
            verified = 0
            rows = connection.execute("SELECT id, relative_path, size, sha256 FROM files ORDER BY relative_path")
            for row_id, relative, size, expected_hash in rows:
                digest = hashlib.sha256()
                read_size = 0
                blob = connection.blobopen("files", "content", int(row_id), readonly=True)
                try:
                    while read_size < int(size):
                        chunk = blob.read(min(_READ_CHUNK, int(size) - read_size))
                        if not chunk:
                            break
                        read_size += len(chunk)
                        digest.update(chunk)
                finally:
                    blob.close()
                if read_size != int(size) or digest.hexdigest() != str(expected_hash):
                    raise ResearchSnapshotError(f"snapshot content hash mismatch: {relative}")
                verified += 1
                if progress_every > 0 and verified % progress_every == 0:
                    print(f"container-verify files={verified}/{count}", flush=True)
    finally:
        connection.close()
    return {
        **receipt,
        "receipt_path": str(receipt_file),
        "receipt_sha256": _sha256_file(receipt_file),
        "verification": "full_content" if full_content else "logical_and_container",
        "verified_at": _utc_now(),
    }


def _safe_output_path(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ResearchSnapshotError(f"unsafe path inside snapshot container: {relative_path!r}")
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResearchSnapshotError(f"snapshot extraction escaped output root: {relative_path!r}") from exc
    return candidate


def extract_snapshot(
    container_path: str | Path,
    *,
    receipt_path: str | Path,
    output_root: str | Path,
    reconstruction_receipt_path: str | Path,
    progress_every: int = 10_000,
) -> dict[str, Any]:
    """Reconstruct a verified container into a new create-only run root."""

    container = Path(container_path).expanduser().resolve(strict=True)
    receipt = Path(receipt_path).expanduser().resolve(strict=True)
    output = Path(output_root).expanduser().resolve(strict=False)
    reconstruction_receipt = Path(reconstruction_receipt_path).expanduser().resolve(strict=False)
    if output.exists() or reconstruction_receipt.exists():
        raise FileExistsError("snapshot reconstruction output is create-only")
    working = output.with_name(f".{output.name}.working")
    if working.exists():
        raise FileExistsError(f"snapshot reconstruction working root already exists: {working}")
    source_receipt = verify_snapshot(container, receipt_path=receipt, full_content=True)
    working.mkdir(parents=True, exist_ok=False)
    connection = _open_readonly_container(container)
    extracted = 0
    extracted_bytes = 0
    try:
        rows = connection.execute(
            "SELECT id, relative_path, size, mtime_ns, mode, sha256 FROM files ORDER BY relative_path"
        )
        for row_id, relative, size, mtime_ns, mode, expected_hash in rows:
            destination = _safe_output_path(working, str(relative))
            destination.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(destination, flags, int(mode) or 0o600)
            digest = hashlib.sha256()
            written = 0
            blob = connection.blobopen("files", "content", int(row_id), readonly=True)
            try:
                while written < int(size):
                    chunk = blob.read(min(_READ_CHUNK, int(size) - written))
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    offset = 0
                    while offset < len(view):
                        amount = os.write(descriptor, view[offset:])
                        if amount <= 0:
                            raise OSError("snapshot extraction write made no progress")
                        offset += amount
                    written += len(chunk)
                    digest.update(chunk)
            finally:
                blob.close()
                os.close(descriptor)
            if written != int(size) or digest.hexdigest() != str(expected_hash):
                raise ResearchSnapshotError(f"snapshot extraction hash mismatch: {relative}")
            os.utime(destination, ns=(int(mtime_ns), int(mtime_ns)))
            extracted += 1
            extracted_bytes += written
            if progress_every > 0 and extracted % progress_every == 0:
                print(f"extract files={extracted}/{source_receipt['file_count']}", flush=True)
    finally:
        connection.close()
    _publish_noreplace(working, output)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": "research_data_snapshot_reconstruction",
        "created_at": _utc_now(),
        "container_sha256": source_receipt["container"]["sha256"],
        "source_receipt_sha256": source_receipt["receipt_sha256"],
        "logical_sha256": source_receipt["logical_sha256"],
        "output_root": str(output),
        "file_count": extracted,
        "total_bytes": extracted_bytes,
        "full_content_verified_before_and_during_extraction": True,
        "outcomes_inspected": False,
    }
    _write_create_only(reconstruction_receipt, canonical_json(payload) + b"\n")
    return {
        **payload,
        "reconstruction_receipt_path": str(reconstruction_receipt),
        "reconstruction_receipt_sha256": _sha256_file(reconstruction_receipt),
    }


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def contract_sha256(path: str | Path) -> str:
    contract = Path(path).expanduser().resolve(strict=True)
    return _sha256_file(contract)


def monotonic_seconds() -> float:
    """Testing seam for progress/performance receipts."""

    return time.monotonic()


__all__ = [
    "DEFAULT_DATASETS",
    "PlannedFile",
    "ResearchSnapshotError",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotPlan",
    "build_snapshot_plan",
    "capture_snapshot",
    "contract_sha256",
    "extract_snapshot",
    "parse_date",
    "plan_payload",
    "verify_snapshot",
]
