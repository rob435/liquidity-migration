#!/usr/bin/env python3
"""Reclaim host storage on the trading box without stopping anything that collects.

Hourly root oneshot. Verified history is reclaimed before any new network work:
a WAL segment is deleted only after the remote copy's length and md5 match the
local file and after the completion receipt is in the ledger. Filesystem
pressure changes how much verified history stays local; it never stops or
starts a collector.

Byte convention: every ``bytes`` number reported here is disk blocks released
or releasable (``st_blocks * 512``), not the object's length. An object's
length is checked against the remote ``Size`` during verification and is not
reported. Upload budgets are the exception, and count network bytes
(``st_size``).

Classes: ``wal`` (sealed numbered segments below the engine's own retention
floor), ``archive`` (an archive root uploaded, verified, then deleted),
``release`` and ``staged`` (unreferenced deploy artifacts), ``apt`` (the
package cache).

Never touched: an ``engine.wal`` family file, the newest numbered segments, any
segment at or above the engine's floor, heartbeats, closed trades, spools,
worker state, tape roots, ``/etc``, and the deploy tree outside ``releases/``
and ``staged/``. The only backup-stage path this writes to is the exact hard
link of a segment it is deleting.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.core.durable_file import durable_atomic_replace  # noqa: E402
from liquidity_migration.policy.realms import realms as _realm_rows  # noqa: E402
from market_tape.config import StorageSettings as _TapeStorage  # noqa: E402

GIB = 1024**3
DEFAULT_FILESYSTEM = "/var/lib/liquidity-migration"
DEFAULT_STATE_DIR = "/var/lib/liquidity-migration/storage-reclaim"
DEFAULT_STAMP_FILE = "/var/lib/liquidity-migration/receipts/storage-reclaim.last-success"
DEFAULT_REMOTE = "gdrive:LiquidityMigration/engine-state"
DEFAULT_RCLONE_CONFIG = "/var/lib/liquidity-migration/backup/rclone.conf"
DEFAULT_RCLONE_BIN = "/usr/bin/rclone"
DEFAULT_ENGINE_TOOLS = "/opt/liquidity-migration-engine/bin/engine-tools"
DEFAULT_BACKUP_LOCK = "/var/lib/liquidity-migration/backup/backup.lock"
DEFAULT_BACKUP_STAGE = "/var/lib/liquidity-migration/backup/stage"
DEFAULT_ARCHIVE_ROOT = "/var/lib/liquidity-migration-wal-quarantine"
DEFAULT_RELEASE_DIR = "/opt/liquidity-migration-engine"
DEFAULT_SYSTEMD_DIR = "/etc/systemd/system"
#: The engine build that stays on the host whatever the deploy pins.
DEFAULT_KEEP_COMMIT = "8c92c96464bfe66662891c05f53a9510eefe8ba2"
#: The recorders' capture configs. `[storage].min_free_disk_gb` is the free
#: space under which a recorder counts every frame and writes none.
DEFAULT_TAPE_FLOOR_CONFIG = str(_REPO_ROOT / "deploy" / "capture")

STATUS_NAME = "status.json"
LEDGER_NAME = "ledger.jsonl"
SAMPLES_NAME = "samples.jsonl"
#: Local md5 by (device, inode, length, mtime_ns). A sealed segment is
#: immutable, and re-reading 40 GB an hour beside a live engine is not free.
MD5_CACHE_NAME = "md5-cache.json"

_SEGMENT_NAME = re.compile(r"engine\.wal\.(\d{6})")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_GROWTH_WINDOW_S = 86_400.0
_STAGED_TEMP_MIN_AGE_S = 86_400.0
_MD5_CHUNK = 8 * 1024 * 1024
#: rclone's exit code for a remote directory that does not exist.
_RCLONE_DIR_NOT_FOUND = 3
_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1000,
    "KB": 1000,
    "KIB": 1024,
    "M": 1000**2,
    "MB": 1000**2,
    "MIB": 1024**2,
    "G": 1000**3,
    "GB": 1000**3,
    "GIB": 1024**3,
    "T": 1000**4,
    "TB": 1000**4,
    "TIB": 1024**4,
}


class StatvfsResult(Protocol):
    @property
    def f_blocks(self) -> int: ...
    @property
    def f_bfree(self) -> int: ...
    @property
    def f_bavail(self) -> int: ...
    @property
    def f_frsize(self) -> int: ...
    @property
    def f_favail(self) -> int: ...


StatvfsFn = Callable[[str], StatvfsResult]
NowFn = Callable[[], float]


def default_wal_families() -> tuple[str, ...]:
    """One WAL family per realm, from the realm table."""

    return tuple(row.engine_wal for row in _realm_rows())


@dataclass(frozen=True)
class Settings:
    filesystem: str
    state_dir: Path
    stamp_file: Path
    remote: str
    rclone_config: Path
    rclone_bin: Path
    engine_tools: Path
    backup_lock: Path
    backup_stage: Path
    wal_families: tuple[Path, ...]
    archive_roots: tuple[Path, ...]
    tape_floor_configs: tuple[Path, ...]
    archive_min_age_hours: float
    release_dir: Path
    keep_commits: tuple[str, ...]
    release_age_days: float
    systemd_dir: Path
    apt_clean: bool
    reserve_fraction: float
    reserve_floor_gib: float
    writer_headroom_gib: float
    high_water_days: float
    high_water_default_gib: float
    wal_min_age_hours: float
    wal_keep_newest: int
    max_wal_bytes_per_run: int
    max_upload_bytes_per_run: int
    lock_timeout_s: float
    dry_run: bool
    json_output: bool


@dataclass(frozen=True)
class Sample:
    measured_at: float
    used_bytes: int
    reclaimed_bytes: int


@dataclass(frozen=True)
class Budget:
    measured_at: float
    capacity_bytes: int
    used_bytes: int
    free_bytes: int
    inodes_free: int
    reserve_bytes: int
    tape_floor_bytes: int
    low_water_bytes: int
    high_water_bytes: int
    growth_bytes_per_s: float | None


@dataclass(frozen=True)
class Segment:
    family: Path
    index: int
    path: Path
    size: int
    freeable_bytes: int
    mtime: float
    nlink: int
    device: int
    inode: int
    #: The backup stage copy, only when it is the same inode as the source.
    stage: Path | None


@dataclass(frozen=True)
class ArchiveFile:
    path: Path
    size: int
    freeable_bytes: int
    mtime: float
    device: int
    inode: int


@dataclass(frozen=True)
class BacklogEntry:
    family: Path
    segment: int
    reason: str
    freeable_bytes: int


@dataclass
class FamilyPlan:
    family: Path
    current_segment: int | None = None
    retention_floor_segment: int | None = None
    candidates: list[Segment] = field(default_factory=list)
    verified: list[Segment] = field(default_factory=list)
    backlog: list[BacklogEntry] = field(default_factory=list)
    reclaimed: list[Segment] = field(default_factory=list)


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    md5: str | None


@dataclass(frozen=True)
class PlanItem:
    kind: str
    path: Path
    freeable_bytes: int
    remote: str | None


def parse_bytes(text: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*", text)
    if match is None:
        raise argparse.ArgumentTypeError(f"not a byte size: {text}")
    unit = _SIZE_UNITS.get(match[2].upper())
    if unit is None:
        raise argparse.ArgumentTypeError(f"unknown byte unit: {match[2]}")
    value = int(float(match[1]) * unit)
    if value < 0:
        raise argparse.ArgumentTypeError(f"byte size must not be negative: {text}")
    return value


def _nonneg_float(text: str) -> float:
    value = float(text)
    if value < 0 or value != value:
        raise argparse.ArgumentTypeError(f"must not be negative: {text}")
    return value


def _nonneg_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {text}")
    return value


def _env(name: str, fallback: str) -> str:
    return os.environ.get(f"RECLAIM_{name}", fallback)


def _env_list(name: str) -> list[str]:
    return os.environ.get(f"RECLAIM_{name}", "").split()


def _env_bool(name: str, fallback: bool) -> bool:
    raw = os.environ.get(f"RECLAIM_{name}")
    if raw is None or raw.strip() == "":
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_settings(argv: Sequence[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("DRY_RUN", False))
    parser.add_argument("--json", dest="json_output", action="store_true", default=_env_bool("JSON", False))
    parser.add_argument("--filesystem", default=_env("FILESYSTEM", DEFAULT_FILESYSTEM))
    parser.add_argument("--state-dir", type=Path, default=_env("STATE_DIR", DEFAULT_STATE_DIR))
    parser.add_argument("--stamp-file", type=Path, default=_env("STAMP_FILE", DEFAULT_STAMP_FILE))
    parser.add_argument("--remote", default=_env("REMOTE", DEFAULT_REMOTE))
    parser.add_argument("--rclone-config", type=Path, default=_env("RCLONE_CONFIG", DEFAULT_RCLONE_CONFIG))
    parser.add_argument("--rclone-bin", type=Path, default=_env("RCLONE_BIN", DEFAULT_RCLONE_BIN))
    parser.add_argument("--engine-tools", type=Path, default=_env("ENGINE_TOOLS", DEFAULT_ENGINE_TOOLS))
    parser.add_argument("--backup-lock", type=Path, default=_env("BACKUP_LOCK", DEFAULT_BACKUP_LOCK))
    parser.add_argument("--backup-stage", type=Path, default=_env("BACKUP_STAGE", DEFAULT_BACKUP_STAGE))
    parser.add_argument("--wal-family", action="append", default=[])
    parser.add_argument("--archive-root", action="append", default=[])
    parser.add_argument("--tape-floor-config", action="append", default=[])
    parser.add_argument(
        "--archive-min-age-hours", type=_nonneg_float, default=_env("ARCHIVE_MIN_AGE_HOURS", "24")
    )
    parser.add_argument("--release-dir", type=Path, default=_env("RELEASE_DIR", DEFAULT_RELEASE_DIR))
    parser.add_argument("--keep-commit", action="append", default=[])
    parser.add_argument("--release-age-days", type=_nonneg_float, default=_env("RELEASE_AGE_DAYS", "1"))
    parser.add_argument("--systemd-dir", type=Path, default=_env("SYSTEMD_DIR", DEFAULT_SYSTEMD_DIR))
    parser.add_argument(
        "--apt-clean", action=argparse.BooleanOptionalAction, default=_env_bool("APT_CLEAN", True)
    )
    parser.add_argument("--reserve-fraction", type=_nonneg_float, default=_env("RESERVE_FRACTION", "0.12"))
    parser.add_argument("--reserve-floor-gib", type=_nonneg_float, default=_env("RESERVE_FLOOR_GIB", "8"))
    parser.add_argument("--writer-headroom-gib", type=_nonneg_float, default=_env("WRITER_HEADROOM_GIB", "6"))
    parser.add_argument("--high-water-days", type=_nonneg_float, default=_env("HIGH_WATER_DAYS", "2"))
    parser.add_argument(
        "--high-water-default-gib", type=_nonneg_float, default=_env("HIGH_WATER_DEFAULT_GIB", "10")
    )
    parser.add_argument("--wal-min-age-hours", type=_nonneg_float, default=_env("WAL_MIN_AGE_HOURS", "48"))
    parser.add_argument("--wal-keep-newest", type=_nonneg_int, default=_env("WAL_KEEP_NEWEST", "3"))
    parser.add_argument(
        "--max-wal-bytes-per-run", type=parse_bytes, default=_env("MAX_WAL_BYTES_PER_RUN", "12GiB")
    )
    parser.add_argument(
        "--max-upload-bytes-per-run", type=parse_bytes, default=_env("MAX_UPLOAD_BYTES_PER_RUN", "2GiB")
    )
    parser.add_argument("--lock-timeout-s", type=_nonneg_float, default=_env("LOCK_TIMEOUT_S", "300"))
    args = parser.parse_args(argv)

    if ":" not in args.remote:
        parser.error(f"--remote must be an rclone remote (remote:path): {args.remote}")
    families = tuple(Path(item) for item in (args.wal_family or _env_list("WAL_FAMILY") or default_wal_families()))
    roots = tuple(
        Path(item) for item in (args.archive_root or _env_list("ARCHIVE_ROOT") or [DEFAULT_ARCHIVE_ROOT])
    )
    tape_floor_configs = tuple(
        Path(item)
        for item in (args.tape_floor_config or _env_list("TAPE_FLOOR_CONFIG") or [DEFAULT_TAPE_FLOOR_CONFIG])
    )
    keep = tuple(args.keep_commit or _env_list("KEEP_COMMIT") or [DEFAULT_KEEP_COMMIT])
    for commit in keep:
        if not _COMMIT.fullmatch(commit):
            parser.error(f"--keep-commit must be a 40-hex commit: {commit}")
    absolute: list[Path] = [
        args.state_dir,
        args.stamp_file,
        args.backup_stage,
        args.backup_lock,
        args.rclone_bin,
        args.rclone_config,
        args.engine_tools,
        args.release_dir,
        args.systemd_dir,
        *families,
        *roots,
        *tape_floor_configs,
    ]
    for path in absolute:
        if not path.is_absolute():
            parser.error(f"paths must be absolute: {path}")
    for family in families:
        if family.name != "engine.wal":
            parser.error(f"--wal-family must name an engine.wal family file: {family}")

    return Settings(
        filesystem=args.filesystem,
        state_dir=args.state_dir,
        stamp_file=args.stamp_file,
        remote=args.remote.rstrip("/"),
        rclone_config=args.rclone_config,
        rclone_bin=args.rclone_bin,
        engine_tools=args.engine_tools,
        backup_lock=args.backup_lock,
        backup_stage=args.backup_stage,
        wal_families=families,
        archive_roots=roots,
        tape_floor_configs=tape_floor_configs,
        archive_min_age_hours=args.archive_min_age_hours,
        release_dir=args.release_dir,
        keep_commits=keep,
        release_age_days=args.release_age_days,
        systemd_dir=args.systemd_dir,
        apt_clean=args.apt_clean,
        reserve_fraction=args.reserve_fraction,
        reserve_floor_gib=args.reserve_floor_gib,
        writer_headroom_gib=args.writer_headroom_gib,
        high_water_days=args.high_water_days,
        high_water_default_gib=args.high_water_default_gib,
        wal_min_age_hours=args.wal_min_age_hours,
        wal_keep_newest=args.wal_keep_newest,
        max_wal_bytes_per_run=args.max_wal_bytes_per_run,
        max_upload_bytes_per_run=args.max_upload_bytes_per_run,
        lock_timeout_s=args.lock_timeout_s,
        dry_run=args.dry_run,
        json_output=args.json_output,
    )


def stamp_time(when: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def read_samples(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return samples
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            samples.append(
                Sample(
                    measured_at=float(row["measured_at_s"]),
                    used_bytes=int(row["used_bytes"]),
                    reclaimed_bytes=int(row.get("reclaimed_bytes", 0)),
                )
            )
        except (ValueError, TypeError, KeyError):
            continue
    return samples


def growth_bytes_per_s(samples: Iterable[Sample], now: float) -> float | None:
    """Bytes/s of writer growth, with reclamation added back so it cannot hide growth."""

    window = sorted((s for s in samples if now - s.measured_at <= _GROWTH_WINDOW_S), key=lambda s: s.measured_at)
    if len(window) < 2:
        return None
    span = window[-1].measured_at - window[0].measured_at
    if span <= 0:
        return None
    grown = window[-1].used_bytes - window[0].used_bytes + sum(s.reclaimed_bytes for s in window[:-1])
    return grown / span


def tape_free_floor_bytes(configs: Iterable[Path]) -> tuple[int, list[str]]:
    """The highest `[storage].min_free_disk_gb` across the recorders' capture
    configs, in bytes, with the configs that could not be read. A directory
    stands for its `*.toml` files; a missing key is the recorder's default."""

    files: list[Path] = []
    for config in configs:
        files.extend(sorted(config.glob("*.toml")) if config.is_dir() else [config])
    floor = 0
    problems: list[str] = []
    for path in files:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            storage = data.get("storage") or {}
            if not isinstance(storage, dict):
                raise ValueError("[storage] must be a table")
            value = storage.get("min_free_disk_gb", _TapeStorage().min_free_disk_gb)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0:
                raise ValueError(f"storage.min_free_disk_gb must be a non-negative number, not {value!r}")
            floor = max(floor, int(float(value) * GIB))
        except (OSError, ValueError) as exc:
            problems.append(f"tape floor {path}: {exc}")
    return floor, problems


def measure_budget(
    stats: StatvfsResult, samples: Iterable[Sample], now: float, settings: Settings, tape_floor_bytes: int = 0
) -> Budget:
    capacity = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    used = capacity - stats.f_bfree * stats.f_frsize
    reserve = int(max(settings.reserve_fraction * capacity, settings.reserve_floor_gib * GIB))
    # Reclaim before either writer is refused room. A recorder under its floor
    # loses frames for good; a sealed segment below the engine's floor has a
    # verified copy, so the WAL yields first.
    low_water = max(reserve, tape_floor_bytes) + int(settings.writer_headroom_gib * GIB)
    growth = growth_bytes_per_s(samples, now)
    if growth is None:
        headroom = int(settings.high_water_default_gib * GIB)
    else:
        headroom = max(int(growth * settings.high_water_days * 86_400.0), 0)
    return Budget(
        measured_at=now,
        capacity_bytes=capacity,
        used_bytes=used,
        free_bytes=free,
        inodes_free=stats.f_favail,
        reserve_bytes=reserve,
        tape_floor_bytes=tape_floor_bytes,
        low_water_bytes=low_water,
        high_water_bytes=low_water + headroom,
        growth_bytes_per_s=growth,
    )


def runway_seconds(free: int, low_water: int, growth: float | None) -> float | None:
    if growth is None or growth <= 0:
        return None
    return (free - low_water) / growth


def remote_relative(path: Path) -> str:
    """The absolute path without its leading separator, as the backup mirrors it."""

    return str(path.relative_to(path.anchor))


def ensure_directory(path: Path, mode: int) -> None:
    """Create a directory the watchdog's user can read, whatever the unit's umask."""

    if path.is_dir():
        return
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def append_line(path: Path, row: dict[str, Any], mode: int = 0o644) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    try:
        os.fchmod(descriptor, mode)
        os.write(descriptor, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mtime_of(path: Path) -> float | None:
    """None when the entry went away between the listing and the stat."""

    try:
        return path.stat().st_mtime
    except OSError:
        return None


def file_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_MD5_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def classify_segments(
    segments: Sequence[Segment], floor: int, now: float, settings: Settings
) -> tuple[list[Segment], list[BacklogEntry]]:
    """Split numbered segments into reclaim candidates and unverifiable backlog."""

    numbered = sorted(segments, key=lambda item: item.index)
    keep = settings.wal_keep_newest
    newest = {item.index for item in numbered[max(len(numbered) - keep, 0):]} if keep else set()
    min_age = settings.wal_min_age_hours * 3600.0
    candidates: list[Segment] = []
    backlog: list[BacklogEntry] = []
    for segment in numbered:
        if segment.index < 2 or segment.index >= floor or segment.index in newest:
            continue
        if now - segment.mtime < min_age:
            continue
        if segment.stage is None:
            backlog.append(
                BacklogEntry(segment.family, segment.index, "stage_not_linked", segment.freeable_bytes)
            )
            continue
        candidates.append(segment)
    return candidates, backlog


def select_for_reclaim(
    verified: Sequence[Segment], available: int, high_water: int, cap: int
) -> list[Segment]:
    """Oldest verified history first, until the high water mark or the per-run cap."""

    chosen: list[Segment] = []
    freed = 0
    for segment in sorted(verified, key=lambda item: (item.mtime, str(item.path))):
        if available + freed >= high_water:
            break
        if freed + segment.freeable_bytes > cap:
            break
        chosen.append(segment)
        freed += segment.freeable_bytes
    return chosen


@contextlib.contextmanager
def backup_lock(path: Path, timeout_s: float) -> Iterator[bool]:
    """The backup's own lock, so a deletion never races an rsync or a link pass."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.1, remaining))
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class Ledger:
    """One fsynced JSON line per reclaimed object, written before its deletion.

    A `wal` row carries the deleted inode's `st_dev` and `st_ino`, which is
    what the realm watchdogs read to tell a reclaimed segment from a lost one
    (`reclaimed_wal_identities` in check_fleet_liveness.py).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []

    def append(self, row: dict[str, Any]) -> None:
        append_line(self.path, row)
        self.rows.append(row)


class Reclaimer:
    def __init__(self, settings: Settings, statvfs: StatvfsFn, now: NowFn) -> None:
        self.settings = settings
        self.statvfs = statvfs
        self.now = now
        self.started_at = now()
        self.errors: list[str] = []
        self.lock_timeout = False
        self.plan: list[PlanItem] = []
        self.reclaimed: dict[str, int] = {"wal": 0, "archive": 0, "release": 0, "staged": 0, "apt": 0}
        self.families: list[FamilyPlan] = []
        self.backlog: list[BacklogEntry] = []
        self.retained: list[Segment] = []
        self.archive_pending_bytes = 0
        self.last_remote_verification_at: str | None = None
        self.verified_this_run = False
        self._md5_cache: dict[str, str] = {}
        self._md5_seen: dict[str, str] = {}
        self._ledger: Ledger | None = None

    # -- state -----------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.settings.state_dir

    def _fail(self, message: str) -> None:
        self.errors.append(message)
        print(f"reclaim: {message}", file=sys.stderr)

    def _note(self, message: str) -> None:
        print(f"reclaim: {message}", file=sys.stderr)

    def ledger(self) -> Ledger:
        if self._ledger is None:
            ensure_directory(self.state_dir, 0o755)
            self._ledger = Ledger(self.state_dir / LEDGER_NAME)
        return self._ledger

    def _record(self, kind: str, path: Path, freed: int, remote: str | None) -> None:
        self.reclaimed[kind] += freed
        self.plan.append(PlanItem(kind, path, freed, remote))

    def _plan_only(self, kind: str, path: Path, freed: int, remote: str | None) -> None:
        self.plan.append(PlanItem(kind, path, freed, remote))

    def load_md5_cache(self) -> None:
        try:
            raw = json.loads((self.state_dir / MD5_CACHE_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._md5_cache = {str(key): str(value) for key, value in raw.items()}

    def save_md5_cache(self, *, prune: bool = False) -> None:
        """Keep the hashing a killed run already paid for; prune only a whole run."""

        if self.settings.dry_run or not self._md5_seen:
            return
        payload = self._md5_seen if prune else {**self._md5_cache, **self._md5_seen}
        ensure_directory(self.state_dir, 0o755)
        durable_atomic_replace(
            self.state_dir / MD5_CACHE_NAME,
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            mode=0o644,
            label="md5 cache",
        )

    def md5_of(self, segment: Segment) -> str:
        stats = os.stat(segment.path)
        key = f"{stats.st_dev}:{stats.st_ino}:{stats.st_size}:{stats.st_mtime_ns}"
        digest = self._md5_cache.get(key) or file_md5(segment.path)
        self._md5_seen[key] = digest
        return digest

    # -- subprocesses ----------------------------------------------------

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, text=True, capture_output=True, check=False)
        except OSError as exc:
            # A binary this host does not have yet is a failed step, not a traceback.
            return subprocess.CompletedProcess(args, 127, "", f"{args[0]}: {exc}")

    def lsjson(self, remote_dir: str, *, missing_ok: bool) -> dict[str, RemoteFile] | None:
        args = [
            str(self.settings.rclone_bin),
            "lsjson",
            "--hash",
            "--files-only",
            remote_dir,
            "--config",
            str(self.settings.rclone_config),
        ]
        result = self._run(args)
        if result.returncode != 0:
            missing = result.returncode == _RCLONE_DIR_NOT_FOUND or "directory not found" in result.stderr.lower()
            if missing and missing_ok:
                return {}
            self._fail(f"lsjson {remote_dir}: exit {result.returncode} {result.stderr.strip()}")
            return None
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError as exc:
            self._fail(f"lsjson {remote_dir}: unreadable listing ({exc})")
            return None
        if not isinstance(rows, list):
            self._fail(f"lsjson {remote_dir}: listing is not a list")
            return None
        listing: dict[str, RemoteFile] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or row.get("Path") or "")
            if not name:
                continue
            hashes = row.get("Hashes")
            md5 = None
            if isinstance(hashes, dict) and isinstance(hashes.get("md5"), str):
                md5 = hashes["md5"]
            try:
                size = int(row.get("Size", -1))
            except (TypeError, ValueError):
                continue
            listing[name] = RemoteFile(name=name, size=size, md5=md5)
        return listing

    def copyto(self, source: str, destination: str) -> bool:
        args = [
            str(self.settings.rclone_bin),
            "copyto",
            source,
            destination,
            "--config",
            str(self.settings.rclone_config),
            "--retries",
            "5",
            "--low-level-retries",
            "10",
        ]
        result = self._run(args)
        if result.returncode != 0:
            self._fail(f"copyto {source} -> {destination}: exit {result.returncode} {result.stderr.strip()}")
            return False
        return True

    def retention_floor(self, family: Path) -> tuple[int, int] | None:
        """(current_segment, retention_floor_segment) from the engine's own tool."""

        result = self._run([str(self.settings.engine_tools), "wal-retention", "--wal", str(family), "--json"])
        if result.returncode != 0:
            self._fail(f"wal-retention {family}: exit {result.returncode} {result.stderr.strip()}")
            return None
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            self._fail(f"wal-retention {family}: unreadable output ({exc})")
            return None
        if not isinstance(payload, dict):
            self._fail(f"wal-retention {family}: output is not an object")
            return None
        floor = payload.get("retention_floor_segment")
        current = payload.get("current_segment")
        if not isinstance(floor, int) or isinstance(floor, bool) or floor < 1:
            self._fail(f"wal-retention {family}: retention_floor_segment is not a segment index")
            return None
        if not isinstance(current, int) or isinstance(current, bool) or current < 1:
            self._fail(f"wal-retention {family}: current_segment is not a segment index")
            return None
        return current, floor

    # -- WAL -------------------------------------------------------------

    def segments(self, family: Path) -> list[Segment]:
        found: list[Segment] = []
        try:
            entries = sorted(family.parent.iterdir())
        except OSError as exc:
            self._fail(f"wal {family}: cannot read its directory ({exc})")
            return found
        for path in entries:
            match = _SEGMENT_NAME.fullmatch(path.name)
            if match is None:
                continue
            index = int(match[1])
            if index < 2:
                continue
            try:
                stats = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(stats.st_mode):
                continue
            stage = self.settings.backup_stage / remote_relative(path)
            linked: Path | None = None
            try:
                staged = os.lstat(stage)
                if (staged.st_dev, staged.st_ino) == (stats.st_dev, stats.st_ino):
                    linked = stage
            except OSError:
                linked = None
            found.append(
                Segment(
                    family=family,
                    index=index,
                    path=path,
                    size=stats.st_size,
                    freeable_bytes=stats.st_blocks * 512,
                    mtime=stats.st_mtime,
                    nlink=stats.st_nlink,
                    device=stats.st_dev,
                    inode=stats.st_ino,
                    stage=linked,
                )
            )
        return found

    def family_plan(self, family: Path) -> FamilyPlan:
        plan = FamilyPlan(family=family)
        if not family.exists() and not self.segments(family):
            # A realm that has never run has no log: nothing to keep or reclaim.
            self._note(f"wal {family}: no log yet")
            return plan
        floors = self.retention_floor(family)
        if floors is None:
            return plan
        plan.current_segment, plan.retention_floor_segment = floors
        candidates, backlog = classify_segments(
            self.segments(family), plan.retention_floor_segment, self.now(), self.settings
        )
        plan.candidates = candidates
        plan.backlog = backlog
        if not candidates:
            return plan
        listing = self.lsjson(f"{self.settings.remote}/latest/{remote_relative(family.parent)}", missing_ok=False)
        if listing is None:
            plan.backlog.extend(
                BacklogEntry(family, item.index, "remote_listing_failed", item.freeable_bytes)
                for item in candidates
            )
            return plan
        for segment in candidates:
            entry = listing.get(segment.path.name)
            if entry is None:
                plan.backlog.append(BacklogEntry(family, segment.index, "remote_missing", segment.freeable_bytes))
                continue
            if entry.size != segment.size:
                plan.backlog.append(BacklogEntry(family, segment.index, "size_mismatch", segment.freeable_bytes))
                continue
            if entry.md5 is None:
                plan.backlog.append(BacklogEntry(family, segment.index, "no_md5", segment.freeable_bytes))
                continue
            if entry.md5 != self.md5_of(segment):
                plan.backlog.append(BacklogEntry(family, segment.index, "md5_mismatch", segment.freeable_bytes))
                continue
            plan.verified.append(segment)
            self.verified_this_run = True
        return plan

    def seal_family(self, family: Path, chosen: Sequence[Segment]) -> list[Segment]:
        """Put each chosen segment in the permanent archive and prove it landed."""

        sealed_dir = f"{self.settings.remote}/sealed/{remote_relative(family.parent)}"
        listing = self.lsjson(sealed_dir, missing_ok=True)
        if listing is None:
            return []
        pending: list[Segment] = []
        for segment in chosen:
            entry = listing.get(segment.path.name)
            if entry is not None and entry.size == segment.size and entry.md5 == self.md5_of(segment):
                continue
            pending.append(segment)
        failed: set[Path] = set()
        for segment in pending:
            source = f"{self.settings.remote}/latest/{remote_relative(segment.path)}"
            destination = f"{self.settings.remote}/sealed/{remote_relative(segment.path)}"
            if not self.copyto(source, destination):
                failed.add(segment.path)
        if pending:
            listing = self.lsjson(sealed_dir, missing_ok=False)
            if listing is None:
                return []
        sealed: list[Segment] = []
        for segment in chosen:
            if segment.path in failed:
                continue
            entry = listing.get(segment.path.name)
            if entry is None or entry.size != segment.size or entry.md5 != self.md5_of(segment):
                self._fail(f"sealed {segment.path}: archive copy does not verify")
                continue
            sealed.append(segment)
        return sealed

    def unlink_segment(self, segment: Segment) -> int:
        links = 1
        os.unlink(segment.path)
        if segment.stage is not None:
            try:
                staged = os.lstat(segment.stage)
                # The stage is touched only where it is this segment's own inode.
                if (staged.st_dev, staged.st_ino) == (segment.device, segment.inode):
                    os.unlink(segment.stage)
                    links += 1
            except FileNotFoundError:
                pass
        return segment.freeable_bytes if segment.nlink - links <= 0 else 0

    def wal_step(self, budget: Budget) -> None:
        for family in self.settings.wal_families:
            plan = self.family_plan(family)
            self.families.append(plan)
            self.backlog.extend(plan.backlog)
            self.save_md5_cache()
        available = budget.free_bytes + sum(self.reclaimed.values())
        verified = [segment for plan in self.families for segment in plan.verified]
        if available >= budget.low_water_bytes:
            self.retained = verified
            return
        chosen = select_for_reclaim(
            verified, available, budget.high_water_bytes, self.settings.max_wal_bytes_per_run
        )
        if self.settings.dry_run:
            for segment in chosen:
                self._plan_only(
                    "wal",
                    segment.path,
                    segment.freeable_bytes,
                    f"{self.settings.remote}/sealed/{remote_relative(segment.path)}",
                )
            self.retained = [segment for segment in verified if segment not in chosen]
            return
        sealed: list[Segment] = []
        by_family: dict[Path, list[Segment]] = {}
        for segment in chosen:
            by_family.setdefault(segment.family, []).append(segment)
        for family, group in by_family.items():
            sealed.extend(self.seal_family(family, group))
        ordered = sorted(sealed, key=lambda item: (item.mtime, str(item.path)))
        deleted = self.delete_segments(ordered)
        done = {segment.path for segment in deleted}
        for plan in self.families:
            plan.reclaimed = [segment for segment in plan.verified if segment.path in done]
        self.retained = [segment for segment in verified if segment.path not in done]

    def delete_segments(self, ordered: Sequence[Segment]) -> list[Segment]:
        if not ordered:
            return []
        deleted: list[Segment] = []
        with backup_lock(self.settings.backup_lock, self.settings.lock_timeout_s) as held:
            if not held:
                self.lock_timeout = True
                self._fail("lock_timeout: the backup lock is held; no deletion this run")
                return deleted
            for segment in ordered:
                remote = f"{self.settings.remote}/sealed/{remote_relative(segment.path)}"
                self.ledger().append(
                    {
                        "reclaimed_at": stamp_time(self.now()),
                        "class": "wal",
                        "path": str(segment.path),
                        "bytes": segment.freeable_bytes,
                        "md5": self.md5_of(segment),
                        "remote": remote,
                        "family": str(segment.family),
                        "segment": segment.index,
                        "st_dev": segment.device,
                        "st_ino": segment.inode,
                    }
                )
                try:
                    freed = self.unlink_segment(segment)
                except OSError as exc:
                    self._fail(f"wal {segment.path}: cannot delete ({exc})")
                    continue
                self._record("wal", segment.path, freed, remote)
                deleted.append(segment)
        return deleted

    # -- archive roots ---------------------------------------------------

    def archive_step(self) -> None:
        for root in self.settings.archive_roots:
            if not root.is_dir() or root.is_symlink():
                continue
            files: list[ArchiveFile] = []
            pending = 0
            for parent, _, names in os.walk(root, followlinks=False):
                for name in names:
                    path = Path(parent) / name
                    if path.is_symlink():
                        self._note(f"archive {path}: symlink skipped")
                        continue
                    try:
                        stats = os.lstat(path)
                    except OSError:
                        continue
                    if not stat.S_ISREG(stats.st_mode):
                        continue
                    pending += stats.st_blocks * 512
                    if self.now() - stats.st_mtime < self.settings.archive_min_age_hours * 3600.0:
                        continue
                    files.append(
                        ArchiveFile(
                            path=path,
                            size=stats.st_size,
                            freeable_bytes=stats.st_blocks * 512,
                            mtime=stats.st_mtime,
                            device=stats.st_dev,
                            inode=stats.st_ino,
                        )
                    )
            uploaded = 0
            chosen: list[ArchiveFile] = []
            for item in sorted(files, key=lambda entry: (entry.mtime, str(entry.path))):
                if uploaded + item.size > self.settings.max_upload_bytes_per_run:
                    break
                uploaded += item.size
                chosen.append(item)
            freed = self.archive_upload(chosen)
            self.archive_pending_bytes += max(pending - freed, 0)
            if freed and not self.settings.dry_run:
                self.remove_empty_directories(root)

    def archive_upload(self, chosen: Sequence[ArchiveFile]) -> int:
        if not chosen:
            return 0
        by_directory: dict[Path, list[ArchiveFile]] = {}
        for item in chosen:
            by_directory.setdefault(item.path.parent, []).append(item)
        freed = 0
        for directory, group in by_directory.items():
            sealed_dir = f"{self.settings.remote}/sealed/{remote_relative(directory)}"
            if self.settings.dry_run:
                for item in group:
                    self._plan_only(
                        "archive",
                        item.path,
                        item.freeable_bytes,
                        f"{self.settings.remote}/sealed/{remote_relative(item.path)}",
                    )
                continue
            failed: set[Path] = set()
            for item in group:
                destination = f"{self.settings.remote}/sealed/{remote_relative(item.path)}"
                if not self.copyto(str(item.path), destination):
                    failed.add(item.path)
            listing = self.lsjson(sealed_dir, missing_ok=False)
            if listing is None:
                continue
            for item in group:
                if item.path in failed:
                    continue
                entry = listing.get(item.path.name)
                digest = file_md5(item.path)
                if entry is None or entry.size != item.size or entry.md5 != digest:
                    self._fail(f"archive {item.path}: archive copy does not verify")
                    continue
                remote = f"{self.settings.remote}/sealed/{remote_relative(item.path)}"
                self.ledger().append(
                    {
                        "reclaimed_at": stamp_time(self.now()),
                        "class": "archive",
                        "path": str(item.path),
                        "bytes": item.freeable_bytes,
                        "md5": digest,
                        "remote": remote,
                        "family": None,
                        "segment": None,
                        "st_dev": item.device,
                        "st_ino": item.inode,
                    }
                )
                try:
                    os.unlink(item.path)
                except OSError as exc:
                    self._fail(f"archive {item.path}: cannot delete ({exc})")
                    continue
                self._record("archive", item.path, item.freeable_bytes, remote)
                self.verified_this_run = True
                freed += item.freeable_bytes
        return freed

    def remove_empty_directories(self, root: Path) -> None:
        # Deepest first, and rmdir itself refuses a directory that still holds
        # anything, so a chain emptied by this run goes in one pass.
        for parent, _, _ in os.walk(root, topdown=False, followlinks=False):
            here = Path(parent)
            if here == root:
                continue
            try:
                here.rmdir()
            except OSError:
                continue

    # -- deploy artifacts and the package cache --------------------------

    def pinned_commits(self) -> set[str]:
        pinned: set[str] = set()
        for name in ("deployed-commit", "previous-commit"):
            path = self.settings.release_dir / name
            try:
                head = path.read_text(encoding="utf-8").strip().splitlines()
            except OSError:
                continue
            if head and _COMMIT.fullmatch(head[0].strip()):
                pinned.add(head[0].strip())
            elif head:
                self._note(f"{path}: not a 40-hex commit; nothing pinned from it")
        return pinned

    def systemd_commits(self) -> set[str]:
        referenced: set[str] = set()
        directory = self.settings.systemd_dir
        if not directory.is_dir():
            return referenced
        candidates = [*directory.glob("*.d/*.conf"), *directory.glob("liquidity-migration-*.service")]
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            referenced.update(_COMMIT.findall(text))
        return referenced

    def tree_bytes(self, path: Path) -> int:
        total = 0
        try:
            total += os.lstat(path).st_blocks * 512
        except OSError:
            return 0
        for parent, directories, names in os.walk(path, followlinks=False):
            for name in (*directories, *names):
                try:
                    total += os.lstat(Path(parent) / name).st_blocks * 512
                except OSError:
                    continue
        return total

    def release_step(self) -> None:
        root = self.settings.release_dir
        releases = root / "releases"
        staged = root / "staged"
        if not root.is_dir():
            return
        keep = set(self.settings.keep_commits) | self.pinned_commits() | self.systemd_commits()
        now = self.now()
        cutoff = now - self.settings.release_age_days * 86_400.0
        directories: dict[str, Path] = {}
        tarballs: dict[str, Path] = {}
        young: set[str] = set()
        if releases.is_dir():
            for path in sorted(releases.iterdir()):
                if not _COMMIT.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                    continue
                mtime = mtime_of(path)
                if mtime is None:
                    continue
                directories[path.name] = path
                if mtime >= cutoff:
                    young.add(path.name)
        if staged.is_dir():
            for path in sorted(staged.iterdir()):
                if path.is_symlink():
                    continue
                mtime = mtime_of(path)
                if mtime is None:
                    continue
                if path.name.startswith("."):
                    if mtime < now - _STAGED_TEMP_MIN_AGE_S:
                        self.remove_artifact("staged", path)
                    continue
                if not path.name.endswith(".tar.gz") or not _COMMIT.fullmatch(path.name[: -len(".tar.gz")]):
                    continue
                commit = path.name[: -len(".tar.gz")]
                tarballs[commit] = path
                if mtime >= cutoff:
                    young.add(commit)
        for commit, path in directories.items():
            if commit in keep or commit in young:
                continue
            self.remove_artifact("release", path)
        for commit, path in tarballs.items():
            if commit in keep or commit in young:
                continue
            self.remove_artifact("staged", path)

    def remove_artifact(self, kind: str, path: Path) -> None:
        freed = self.tree_bytes(path)
        if self.settings.dry_run:
            self._plan_only(kind, path, freed, None)
            return
        self.ledger().append(
            {
                "reclaimed_at": stamp_time(self.now()),
                "class": kind,
                "path": str(path),
                "bytes": freed,
                "md5": None,
                "remote": None,
                "family": None,
                "segment": None,
                "st_dev": None,
                "st_ino": None,
            }
        )
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError as exc:
            self._fail(f"{kind} {path}: cannot delete ({exc})")
            return
        self._record(kind, path, freed, None)

    def apt_step(self) -> None:
        if not self.settings.apt_clean:
            return
        binary = shutil.which("apt-get")
        if binary is None:
            self._note("apt-get is not installed; package cache left alone")
            return
        if self.settings.dry_run:
            self._plan_only("apt", Path(binary), 0, None)
            return
        before = self.free_bytes()
        result = self._run([binary, "clean"])
        if result.returncode != 0:
            self._fail(f"apt-get clean: exit {result.returncode} {result.stderr.strip()}")
            return
        after = self.free_bytes()
        if before is not None and after is not None:
            self.reclaimed["apt"] += max(after - before, 0)

    def free_bytes(self) -> int | None:
        try:
            stats = self.statvfs(self.settings.filesystem)
        except OSError:
            return None
        return stats.f_bavail * stats.f_frsize

    # -- status ----------------------------------------------------------

    def previous_verification(self) -> str | None:
        try:
            payload = json.loads((self.state_dir / STATUS_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("last_remote_verification_at"), str):
            return payload["last_remote_verification_at"]
        return None

    def status(self, budget: Budget) -> dict[str, Any]:
        reclaimed_total = sum(self.reclaimed.values())
        free_after = budget.free_bytes + reclaimed_total
        retained_bytes = sum(segment.freeable_bytes for segment in self.retained)
        growth = budget.growth_bytes_per_s
        return {
            "measured_at": stamp_time(budget.measured_at),
            "filesystem": self.settings.filesystem,
            "capacity_bytes": budget.capacity_bytes,
            "used_bytes": budget.used_bytes,
            "free_bytes": budget.free_bytes,
            "inodes_free": budget.inodes_free,
            "reserve_bytes": budget.reserve_bytes,
            "tape_floor_bytes": budget.tape_floor_bytes,
            "low_water_bytes": budget.low_water_bytes,
            "high_water_bytes": budget.high_water_bytes,
            "growth_bytes_per_s": growth,
            "reclaimed_bytes_this_run": reclaimed_total,
            "reclaimed_by_class": dict(self.reclaimed),
            "retained_verified_wal_bytes": retained_bytes,
            "retained_verified_wal_segments": len(self.retained),
            "unverified_backlog_bytes": sum(item.freeable_bytes for item in self.backlog),
            "unverified_backlog": [
                {"family": str(item.family), "segment": item.segment, "reason": item.reason}
                for item in self.backlog
            ],
            "per_family": [
                {
                    "family": str(plan.family),
                    "current_segment": plan.current_segment,
                    "retention_floor_segment": plan.retention_floor_segment,
                    "candidates": len(plan.candidates),
                    "verified": len(plan.verified),
                    "reclaimed": len(plan.reclaimed),
                }
                for plan in self.families
            ],
            "last_remote_verification_at": self.last_remote_verification_at,
            "estimated_runway_s": runway_seconds(free_after, budget.low_water_bytes, growth),
            "runway_with_verified_history_s": runway_seconds(
                free_after + retained_bytes, budget.low_water_bytes, growth
            ),
            "archive_pending_bytes": self.archive_pending_bytes,
            "lock_timeout": self.lock_timeout,
            "dry_run": self.settings.dry_run,
            "errors": list(self.errors),
            "plan": [
                {
                    "class": item.kind,
                    "path": str(item.path),
                    "bytes": item.freeable_bytes,
                    "remote": item.remote,
                }
                for item in self.plan
            ],
        }

    def write_stamp(self, status: dict[str, Any]) -> None:
        runway = status["estimated_runway_s"]
        lines = [
            f"reclaimed_at={stamp_time(self.now())}",
            f"free_bytes={status['free_bytes']}",
            f"low_water_bytes={status['low_water_bytes']}",
            f"high_water_bytes={status['high_water_bytes']}",
            f"reclaimed_bytes={status['reclaimed_bytes_this_run']}",
            f"wal_segments_reclaimed={sum(len(plan.reclaimed) for plan in self.families)}",
            f"unverified_backlog_bytes={status['unverified_backlog_bytes']}",
            f"estimated_runway_s={'' if runway is None else int(runway)}",
        ]
        ensure_directory(self.settings.stamp_file.parent, 0o755)
        durable_atomic_replace(
            self.settings.stamp_file,
            ("\n".join(lines) + "\n").encode("utf-8"),
            mode=0o644,
            label="reclaim stamp",
        )

    def persist(self, status: dict[str, Any], budget: Budget) -> None:
        ensure_directory(self.state_dir, 0o755)
        durable_atomic_replace(
            self.state_dir / STATUS_NAME,
            (json.dumps(status, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o644,
            label="reclaim status",
        )
        sample = {
            "measured_at": stamp_time(budget.measured_at),
            "measured_at_s": budget.measured_at,
            "capacity_bytes": budget.capacity_bytes,
            "used_bytes": budget.used_bytes,
            "free_bytes": budget.free_bytes,
            "reclaimed_bytes": status["reclaimed_bytes_this_run"],
        }
        append_line(self.state_dir / SAMPLES_NAME, sample)

    # -- run -------------------------------------------------------------

    def run(self) -> int:
        try:
            stats = self.statvfs(self.settings.filesystem)
        except OSError as exc:
            self._fail(f"statvfs {self.settings.filesystem}: {exc}")
            return 1
        self.load_md5_cache()
        samples = read_samples(self.state_dir / SAMPLES_NAME)
        tape_floor, unreadable = tape_free_floor_bytes(self.settings.tape_floor_configs)
        for problem in unreadable:
            self._fail(problem)
        budget = measure_budget(stats, samples, self.now(), self.settings, tape_floor)
        self.last_remote_verification_at = self.previous_verification()

        self.release_step()
        self.apt_step()
        self.wal_step(budget)
        self.archive_step()
        if self.verified_this_run:
            self.last_remote_verification_at = stamp_time(self.now())

        status = self.status(budget)
        if not self.settings.dry_run:
            self.persist(status, budget)
            self.save_md5_cache(prune=True)
        if self.errors:
            self.report(status)
            return 1
        if not self.settings.dry_run:
            self.write_stamp(status)
        self.report(status)
        return 0

    def report(self, status: dict[str, Any]) -> None:
        if self.settings.json_output:
            print(json.dumps(status, indent=2, sort_keys=True))
            return
        prefix = "reclaim (dry run)" if self.settings.dry_run else "reclaim"
        for item in status["plan"]:
            print(f"{prefix}: {item['class']} {item['path']} bytes={item['bytes']} remote={item['remote'] or '-'}")
        print(
            f"{prefix}: free={status['free_bytes']} tape_floor={status['tape_floor_bytes']} "
            f"low_water={status['low_water_bytes']} high_water={status['high_water_bytes']} "
            f"reclaimed={status['reclaimed_bytes_this_run']} "
            f"retained_verified={status['retained_verified_wal_bytes']} "
            f"backlog={status['unverified_backlog_bytes']} errors={len(status['errors'])}"
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    statvfs: StatvfsFn | None = None,
    now: NowFn | None = None,
) -> int:
    settings = parse_settings(argv)
    return Reclaimer(settings, statvfs or os.statvfs, now or time.time).run()


if __name__ == "__main__":
    raise SystemExit(main())
