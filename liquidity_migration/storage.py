from __future__ import annotations

import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import polars as pl

from .symbol_codec import encode_symbol_partition


# Per-process thread-lock per dataset path. POSIX flock semantics are process
# friendly but do not provide the intended exclusion between separate opens in
# every same-process/thread pattern, so serialize threads explicitly as well.
_DATASET_THREAD_LOCKS: dict[str, threading.Lock] = {}
_DATASET_THREAD_LOCKS_GUARD = threading.Lock()
_DATASET_TMP_SWEEP_LAST: dict[str, float] = {}
_DATASET_TMP_SWEEP_GUARD = threading.Lock()

# Every opened/waiting/held flock descriptor is registered across its complete
# lifetime. The at-fork barrier closes inherited descriptors in the child and
# resets thread mutexes that may have been held by vanished parent threads.
_ACTIVE_FLOCK_FDS: set[int] = set()
_ACTIVE_FLOCK_FDS_GUARD = threading.Lock()
_LOCK_CREATE_SUFFIX_BYTES = 16
_LOCK_CREATE_ORPHAN_SECONDS = 600.0
_LOCK_CREATE_SWEEP_CACHE: dict[
    str,
    tuple[tuple[int, int, int], float | None],
] = {}
_LOCK_CREATE_SWEEP_GUARD = threading.Lock()


def _before_fork() -> None:
    _ACTIVE_FLOCK_FDS_GUARD.acquire()


def _after_fork_parent() -> None:
    _ACTIVE_FLOCK_FDS_GUARD.release()


def _after_fork_child() -> None:
    global _ACTIVE_FLOCK_FDS
    global _ACTIVE_FLOCK_FDS_GUARD
    global _DATASET_THREAD_LOCKS
    global _DATASET_THREAD_LOCKS_GUARD
    global _DATASET_TMP_SWEEP_GUARD
    global _LOCK_CREATE_SWEEP_GUARD

    for fd in tuple(_ACTIVE_FLOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            pass
    _ACTIVE_FLOCK_FDS = set()
    _ACTIVE_FLOCK_FDS_GUARD = threading.Lock()
    _DATASET_THREAD_LOCKS = {}
    _DATASET_THREAD_LOCKS_GUARD = threading.Lock()
    _DATASET_TMP_SWEEP_LAST.clear()
    _DATASET_TMP_SWEEP_GUARD = threading.Lock()
    _LOCK_CREATE_SWEEP_CACHE.clear()
    _LOCK_CREATE_SWEEP_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    key = str(lock_path.resolve())
    with _DATASET_THREAD_LOCKS_GUARD:
        lock = _DATASET_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DATASET_THREAD_LOCKS[key] = lock
        return lock


DATASETS = {
    "instruments",
    "klines_1h",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    "ticker_snapshots",
    "archive_trade_manifest",
    "universe_current",
    "event_demo_klines_1h",
    "long_native_demo_cycles",
    "long_native_paper_cycles",
    "continuous_fade_demo_cycles",
    "continuous_fade_paper_cycles",
    "binance_usdm_klines_1h",
    "binance_usdm_mark_price_1h",
    "binance_usdm_index_price_1h",
    "binance_usdm_premium_index_1h",
    "binance_usdm_funding",
    "binance_usdm_open_interest",
    "binance_usdm_taker_flow_1h",
}

DATASET_KEYS = {
    "instruments": ("symbol",),
    "klines_1h": ("ts_ms", "symbol"),
    "funding": ("ts_ms", "symbol"),
    "open_interest": ("ts_ms", "symbol"),
    "mark_price_1h": ("ts_ms", "symbol"),
    "index_price_1h": ("ts_ms", "symbol"),
    "premium_index_1h": ("ts_ms", "symbol"),
    "ticker_snapshots": ("ts_ms", "symbol"),
    "archive_trade_manifest": ("symbol", "date", "url"),
    "universe_current": ("snapshot_ts_ms", "symbol"),
    "event_demo_klines_1h": ("ts_ms", "symbol"),
    "long_native_demo_cycles": ("cycle_id",),
    "long_native_paper_cycles": ("cycle_id",),
    "continuous_fade_demo_cycles": ("cycle_id",),
    "continuous_fade_paper_cycles": ("cycle_id",),
    "binance_usdm_klines_1h": ("ts_ms", "symbol"),
    "binance_usdm_mark_price_1h": ("ts_ms", "symbol"),
    "binance_usdm_index_price_1h": ("ts_ms", "symbol"),
    "binance_usdm_premium_index_1h": ("ts_ms", "symbol"),
    "binance_usdm_funding": ("ts_ms", "symbol"),
    "binance_usdm_open_interest": ("ts_ms", "symbol"),
    "binance_usdm_taker_flow_1h": ("ts_ms", "symbol"),
}


# Canonical-name fallbacks: when a root stores a dataset under a venue-specific
# name instead of the canonical one, reads of the canonical name transparently
# resolve to whichever venue variant is actually present. This is why a raw
# per-venue root (e.g. binance_full_pit, which stores funding as
# binance_usdm_funding) is funding-modeled with NO symlink/rename — read_dataset
# (root, "funding") finds binance_usdm_funding on its own. Extend per venue.
_DATASET_FALLBACKS: dict[str, tuple[str, ...]] = {
    "funding": ("binance_usdm_funding",),
    "open_interest": ("binance_usdm_open_interest",),
}


# pit-data-6 is now enforced by canonical-precedence in resolve_dataset_name (a real
# Bybit root always carries its own canonical funding/ dir, which wins over any
# binance_usdm_* proxy on the same root) rather than a klines-name marker — the marker
# false-positived on Binance full-PIT roots that store klines under the canonical name.

def resolve_dataset_name(data_root: str | Path, dataset: str) -> str:
    """Map a canonical dataset request to the variant actually present in ``root``.

    Returns ``dataset`` unchanged when the canonical directory exists (or when no
    fallback applies); otherwise the first known venue-variant that exists on
    disk. The returned name is always a member of :data:`DATASETS`, so the lock
    and path helpers stay valid.

    pit-data-6: a missing canonical funding/OI dir must never silently resolve to
    the WRONG venue's modeled cost curve. The safety invariant used here is that a
    ``binance_usdm_*`` variant dir only ever exists on a Binance root, so a present
    variant is authoritative and a Bybit root (which never carries one) falls back
    to the canonical (empty) name -> funding_mode=missing.
    """
    fallbacks = _DATASET_FALLBACKS.get(dataset)
    if not fallbacks:
        return dataset
    root = Path(data_root).expanduser()
    if (root / dataset).exists():
        return dataset
    # A present venue-variant dataset is AUTHORITATIVE: a ``binance_usdm_*`` dir only
    # ever exists on a Binance root, so prefer it even when the root stores klines
    # under the canonical ``klines_1h/`` name. The old code suppressed this whenever
    # a Bybit-native kline marker was present, which false-positived on Binance
    # full-PIT roots that use canonical kline naming -> funding/OI silently uncosted
    # (the 2026-06-15 resolver regression: funding_mode=missing on a fully populated
    # binance_usdm_funding).
    for alt in fallbacks:
        if (root / alt).exists():
            return alt
    # No variant present. A Bybit root whose funding/OI dir is simply absent returns
    # the canonical (empty) name -> funding_mode=missing, never substituting a
    # wrong-venue cost curve (pit-data-6 safety preserved: a Bybit root never carries
    # a binance_usdm_* dir, so the loop above cannot mis-substitute).
    return dataset


def dataset_path(data_root: str | Path, dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    return Path(data_root).expanduser() / dataset


def dataset_lock_path(data_root: str | Path, dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    return Path(data_root).expanduser() / ".locks" / f"{dataset}.lock"


def ensure_data_root(data_root: str | Path) -> Path:
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def exclusive_file_lock(
    path: str | Path,
    *,
    stale_seconds: float = 600,
    poll_seconds: float = 0.05,
    invalid_lock_stale_seconds: float = 30.0,
) -> Iterator[None]:
    """Serialize a critical section across threads and local POSIX processes.

    The lock leaf is persistent and never unlinked during normal operation.
    Kernel ``flock`` ownership ends automatically on descriptor close or process
    death, so recovery never infers liveness from a pathname, PID, payload, or
    wall-clock age. ``stale_seconds`` and ``invalid_lock_stale_seconds`` remain
    compatibility no-ops; ``poll_seconds`` still controls nonblocking wait
    cadence.

    This protocol requires a local flock-capable filesystem and a quiescent
    migration from the retired create/unlink implementation. Explicitly forking
    inside the yielded critical section is unsupported; fork/exec helpers and
    forks from other threads are cleaned up by the module's at-fork handler.
    """
    del stale_seconds, invalid_lock_stale_seconds
    lock_path = Path(path).expanduser()
    _ensure_lock_directory(lock_path)
    poll = max(float(poll_seconds), 0.0)
    with _thread_lock_for(lock_path):
        fd = -1
        directory_key: tuple[int, int] | None = None
        acquisition_pid = os.getpid()
        while True:
            try:
                fd, directory_key = _open_registered_lock_fd(lock_path)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(poll)
                        continue
                    break
                _recover_internal_lock_alias(lock_path, fd, directory_key=directory_key)
                _validate_lock_fd_path(lock_path, fd, directory_key=directory_key)
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    os.fchmod(fd, 0o600)
                _validate_lock_fd_path(lock_path, fd, directory_key=directory_key)
            except _LockPathChanged:
                _close_registered_lock_fd(fd, acquisition_pid=acquisition_pid)
                fd = -1
                directory_key = None
                time.sleep(poll)
                continue
            except BaseException:
                _close_registered_lock_fd(fd, acquisition_pid=acquisition_pid)
                fd = -1
                directory_key = None
                raise
            break
        try:
            yield
        finally:
            _close_registered_lock_fd(fd, acquisition_pid=acquisition_pid)


class _LockPathChanged(RuntimeError):
    """The opened lock inode stopped being the persistent path while waiting."""


def _lock_owner_allowed(owner_uid: int, *, effective_uid: int | None = None) -> bool:
    """Root may observe an owner-controlled lock; other users may only use their own."""
    current_uid = os.geteuid() if effective_uid is None else int(effective_uid)
    return current_uid == 0 or int(owner_uid) == current_uid


def _required_directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - supported deployment platforms are POSIX
        raise RuntimeError("secure file locking requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC") from exc


def _open_lock_directory(lock_path: Path) -> tuple[int, os.stat_result]:
    try:
        directory_fd = os.open(lock_path.parent, _required_directory_flags())
    except OSError as exc:
        raise RuntimeError(f"cannot safely open lock directory {lock_path.parent}: {exc}") from exc
    try:
        descriptor = os.fstat(directory_fd)
        current = os.lstat(lock_path.parent)
        if not stat.S_ISDIR(descriptor.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise RuntimeError(f"lock directory must be a real directory: {lock_path.parent}")
        if (descriptor.st_dev, descriptor.st_ino) != (current.st_dev, current.st_ino):
            raise _LockPathChanged(f"lock directory changed while acquiring: {lock_path.parent}")
        if descriptor.st_mode & 0o022:
            raise RuntimeError(f"lock directory must not be group/world writable: {lock_path.parent}")
        if not _lock_owner_allowed(descriptor.st_uid):
            raise RuntimeError(f"lock directory must be owned by the current user: {lock_path.parent}")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd, descriptor


def _ensure_lock_directory(lock_path: Path) -> None:
    """Create a secure lock directory, inheriting a dataset root owner for root observers.

    Operational dataset locks live below ``ROOT/.locks``. When euid 0 is the
    first reader of a paper-owned root, that directory must still belong to the
    paper runtime. Deployment pre-creates it; this bootstrap/repair path covers
    fresh roots and an interrupted bootstrap without making path contents an
    ownership authority.
    """
    directory = lock_path.parent
    inherited_owner: tuple[int, int] | None = None
    if directory.name == ".locks" and directory.parent.exists():
        try:
            parent = os.lstat(directory.parent)
        except OSError as exc:
            raise RuntimeError(f"cannot inspect lock root {directory.parent}: {exc}") from exc
        if not stat.S_ISDIR(parent.st_mode):
            raise RuntimeError(f"lock root must be a real directory: {directory.parent}")
        inherited_owner = (parent.st_uid, parent.st_gid)

    try:
        os.mkdir(directory, 0o700)
        created = True
    except FileExistsError:
        created = False
    except FileNotFoundError:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        created = True
    except OSError as exc:
        raise RuntimeError(f"cannot create lock directory {directory}: {exc}") from exc

    directory_fd, descriptor = _open_lock_directory(lock_path)
    try:
        if (
            os.geteuid() == 0
            and inherited_owner is not None
            and (descriptor.st_uid, descriptor.st_gid) != inherited_owner
        ):
            os.fchown(directory_fd, *inherited_owner)
            os.fchmod(directory_fd, 0o700)
            descriptor = os.fstat(directory_fd)
        elif created:
            os.fchmod(directory_fd, 0o700)
        if inherited_owner is not None and descriptor.st_uid != inherited_owner[0]:
            raise RuntimeError(
                f"lock directory owner does not match its data root: {directory}"
            )
    finally:
        os.close(directory_fd)


def _validate_lock_fd_identity(
    lock_path: Path,
    fd: int,
    *,
    directory_fd: int,
    directory: os.stat_result,
) -> os.stat_result:
    descriptor = os.fstat(fd)
    if not stat.S_ISREG(descriptor.st_mode):
        raise RuntimeError(f"lock path must be a regular file: {lock_path}")
    if descriptor.st_nlink == 0:
        raise _LockPathChanged(f"lock path was removed while acquiring: {lock_path}")
    try:
        current = os.stat(lock_path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _LockPathChanged(f"lock path disappeared while acquiring: {lock_path}") from exc
    if not stat.S_ISREG(current.st_mode):
        raise RuntimeError(f"lock path must resolve to a regular file: {lock_path}")
    if (current.st_dev, current.st_ino) != (descriptor.st_dev, descriptor.st_ino):
        raise _LockPathChanged(f"lock path changed inode while acquiring: {lock_path}")
    if descriptor.st_uid != directory.st_uid:
        raise RuntimeError(f"lock path owner must match its lock directory: {lock_path}")
    return descriptor


def _validate_lock_fd_path(
    lock_path: Path,
    fd: int,
    *,
    directory_key: tuple[int, int] | None = None,
) -> None:
    directory_fd, directory = _open_lock_directory(lock_path)
    try:
        current_key = (directory.st_dev, directory.st_ino)
        if directory_key is not None and current_key != directory_key:
            raise _LockPathChanged(f"lock directory changed while acquiring: {lock_path.parent}")
        descriptor = _validate_lock_fd_identity(
            lock_path,
            fd,
            directory_fd=directory_fd,
            directory=directory,
        )
        if descriptor.st_nlink != 1:
            raise RuntimeError(f"lock path must have exactly one hard link: {lock_path}")
    finally:
        os.close(directory_fd)


def _lock_create_prefix(lock_name: str) -> str:
    return f".{lock_name}.create-"


def _is_lock_create_alias(lock_name: str, candidate: str) -> bool:
    prefix = _lock_create_prefix(lock_name)
    suffix = candidate[len(prefix):] if candidate.startswith(prefix) else ""
    return (
        len(suffix) == _LOCK_CREATE_SUFFIX_BYTES * 2
        and all(character in "0123456789abcdef" for character in suffix)
    )


def _matching_internal_aliases(
    *,
    directory_fd: int,
    lock_name: str,
    descriptor: os.stat_result,
) -> list[str]:
    aliases: list[str] = []
    for candidate in os.listdir(directory_fd):
        if not _is_lock_create_alias(lock_name, candidate):
            continue
        try:
            current = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (current.st_dev, current.st_ino) == (descriptor.st_dev, descriptor.st_ino):
            aliases.append(candidate)
    return aliases


def _recover_internal_lock_alias(
    lock_path: Path,
    fd: int,
    *,
    directory_key: tuple[int, int],
) -> None:
    descriptor = os.fstat(fd)
    if descriptor.st_nlink == 1:
        return
    directory_fd, directory = _open_lock_directory(lock_path)
    try:
        if (directory.st_dev, directory.st_ino) != directory_key:
            raise _LockPathChanged(f"lock directory changed while acquiring: {lock_path.parent}")
        _validate_lock_fd_identity(
            lock_path,
            fd,
            directory_fd=directory_fd,
            directory=directory,
        )
        aliases = _matching_internal_aliases(
            directory_fd=directory_fd,
            lock_name=lock_path.name,
            descriptor=descriptor,
        )
        if descriptor.st_nlink != 1 + len(aliases):
            raise RuntimeError(f"lock path has an unexplained hard link: {lock_path}")
        for alias in aliases:
            os.unlink(alias, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _cleanup_old_lock_create_orphans(
    *,
    directory_fd: int,
    lock_name: str,
) -> float | None:
    now = time.time()
    cutoff = now - _LOCK_CREATE_ORPHAN_SECONDS
    next_sweep_at: float | None = None
    for candidate in os.listdir(directory_fd):
        if not _is_lock_create_alias(lock_name, candidate):
            continue
        try:
            current = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if _lock_create_orphan_removable(current, cutoff=cutoff):
            # The name belongs to our private staging namespace and the trusted
            # directory owner controls unlink. A root creator can die before
            # fchown, leaving exactly this uid-mismatched, unpublished inode.
            try:
                os.unlink(candidate, dir_fd=directory_fd)
            except FileNotFoundError:
                # Another process can sweep the same orphan before either one
                # has acquired the canonical lock. Both outcomes are clean.
                pass
        elif stat.S_ISREG(current.st_mode) and current.st_nlink == 1:
            eligible_at = current.st_mtime + _LOCK_CREATE_ORPHAN_SECONDS
            if next_sweep_at is None or eligible_at < next_sweep_at:
                next_sweep_at = eligible_at
    return next_sweep_at


def _lock_create_orphan_removable(current: os.stat_result, *, cutoff: float) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and current.st_mtime < cutoff
    )


def _sweep_lock_create_orphans_if_directory_changed(
    *,
    lock_path: Path,
    directory_fd: int,
    directory: os.stat_result,
) -> None:
    signature = (directory.st_dev, directory.st_ino, directory.st_mtime_ns)
    key = str(lock_path)
    with _LOCK_CREATE_SWEEP_GUARD:
        now = time.time()
        cached = _LOCK_CREATE_SWEEP_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            next_sweep_at = cached[1]
            if next_sweep_at is None or now <= next_sweep_at:
                return
        next_sweep_at = _cleanup_old_lock_create_orphans(
            directory_fd=directory_fd,
            lock_name=lock_path.name,
        )
        # Cache the signature observed before scanning. If another process
        # changes the directory during/after the scan, the next acquisition
        # sees a different signature and scans again rather than missing it.
        _LOCK_CREATE_SWEEP_CACHE[key] = (signature, next_sweep_at)


def _close_registered_fd_while_guarded(fd: int) -> None:
    _ACTIVE_FLOCK_FDS.discard(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _open_registered_lock_fd(lock_path: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    with _ACTIVE_FLOCK_FDS_GUARD:
        while True:
            directory_fd, directory = _open_lock_directory(lock_path)
            directory_key = (directory.st_dev, directory.st_ino)
            try:
                _sweep_lock_create_orphans_if_directory_changed(
                    lock_path=lock_path,
                    directory_fd=directory_fd,
                    directory=directory,
                )
                try:
                    fd = os.open(lock_path.name, flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    alias = _lock_create_prefix(lock_path.name) + os.urandom(
                        _LOCK_CREATE_SUFFIX_BYTES
                    ).hex()
                    try:
                        fd = os.open(
                            alias,
                            flags | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        raise RuntimeError(f"cannot safely stage lock path {lock_path}: {exc}") from exc
                    _ACTIVE_FLOCK_FDS.add(fd)
                    published = False
                    try:
                        os.fchown(fd, directory.st_uid, directory.st_gid)
                        os.fchmod(fd, 0o600)
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        staged = os.fstat(fd)
                        current_alias = os.stat(alias, dir_fd=directory_fd, follow_symlinks=False)
                        if (
                            not stat.S_ISREG(staged.st_mode)
                            or staged.st_nlink != 1
                            or (staged.st_dev, staged.st_ino)
                            != (current_alias.st_dev, current_alias.st_ino)
                            or staged.st_uid != directory.st_uid
                        ):
                            raise RuntimeError(f"staged lock path changed before publication: {lock_path}")
                        try:
                            os.link(
                                alias,
                                lock_path.name,
                                src_dir_fd=directory_fd,
                                dst_dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        except FileExistsError:
                            pass
                        else:
                            published = True
                        os.unlink(alias, dir_fd=directory_fd)
                        if published:
                            _validate_lock_fd_identity(
                                lock_path,
                                fd,
                                directory_fd=directory_fd,
                                directory=directory,
                            )
                            return fd, directory_key
                    except BaseException:
                        try:
                            os.unlink(alias, dir_fd=directory_fd)
                        except OSError:
                            pass
                        _close_registered_fd_while_guarded(fd)
                        raise
                    _close_registered_fd_while_guarded(fd)
                    continue
                except OSError as exc:
                    raise RuntimeError(f"cannot safely open lock path {lock_path}: {exc}") from exc
                _ACTIVE_FLOCK_FDS.add(fd)
                try:
                    _validate_lock_fd_identity(
                        lock_path,
                        fd,
                        directory_fd=directory_fd,
                        directory=directory,
                    )
                except BaseException:
                    _close_registered_fd_while_guarded(fd)
                    raise
                return fd, directory_key
            finally:
                os.close(directory_fd)


def _close_registered_lock_fd(fd: int, *, acquisition_pid: int) -> None:
    if fd < 0 or os.getpid() != acquisition_pid:
        # The child at-fork hook already closed inherited descriptors. Avoid a
        # second close against an fd number the child may since have reused.
        return
    with _ACTIVE_FLOCK_FDS_GUARD:
        if fd not in _ACTIVE_FLOCK_FDS:
            return
        _close_registered_fd_while_guarded(fd)


def with_date_column(df: pl.DataFrame, ts_col: str = "ts_ms") -> pl.DataFrame:
    if "date" in df.columns:
        return df
    return df.with_columns(
        pl.from_epoch(pl.col(ts_col), time_unit="ms")
        .dt.strftime("%Y-%m-%d")
        .alias("date")
    )


# Continuous cycle heartbeats are wide and written every minute, so partition
# them monthly instead of rewriting an unbounded monolith.
_LEDGER_MONTH_COL = "_ledger_month"
LEDGER_BUCKET_SOURCE: dict[str, str] = {
    "continuous_fade_demo_cycles": "ts_ms",
    "continuous_fade_paper_cycles": "ts_ms",
}


def _with_ledger_month(df: pl.DataFrame, dataset: str) -> pl.DataFrame:
    """Add the int yyyymm _ledger_month partition column for a bucketed ledger
    dataset, derived from its registered timestamp source. Rows whose source ts
    is missing/null/<=0 fall into bucket 0 so a malformed row never crashes the
    write. A no-op for datasets not in
    LEDGER_BUCKET_SOURCE or when the source column is absent."""
    src = LEDGER_BUCKET_SOURCE.get(dataset)
    if src is None or src not in df.columns or df.is_empty():
        return df
    month = pl.col(src).cast(pl.Int64, strict=False)
    return df.with_columns(
        pl.when(month.is_null() | (month <= 0))
        .then(pl.lit(0, dtype=pl.Int64))
        .otherwise(
            pl.from_epoch(month, time_unit="ms").dt.strftime("%Y%m").cast(pl.Int64, strict=False)
        )
        .fill_null(0)
        .alias(_LEDGER_MONTH_COL)
    )

# storage-concurrency-4: how stale an orphaned `.*.tmp` part file must be before
# the sweep removes it. The temp file only exists for the brief window between
# write_parquet and the atomic rename in _write_part; any `.tmp` older than this
# is from a process that was SIGKILLed (OOM / TimeoutStopSec->SIGKILL / hard
# crash) mid-write, whose `finally: temp_path.unlink()` never ran. Generous so a
# slow in-flight write on another (impossible here — we hold the dataset lock)
# path is never clobbered.
_STALE_TMP_SECONDS = 600.0
_TMP_SWEEP_INTERVAL_SECONDS = 600.0


def _sweep_orphaned_tmp_parts(
    path: Path,
    *,
    stale_seconds: float = _STALE_TMP_SECONDS,
    recursive: bool = True,
) -> None:
    """Remove orphaned `.*.tmp` part files left by a crash between
    ``write_parquet`` and the atomic rename in :func:`_write_part`.

    _write_part writes to ``.{name}.{pid}.{ns}.tmp`` then renames; a SIGKILL in
    that window orphans the temp file because the ``finally`` unlink never runs.
    Readers never consume them (``glob('**/*.parquet')`` cannot match a leading-
    dot `.tmp` name), so this is a pure resource leak — but a long-lived
    Restart=always daemon that occasionally crashes accumulates them until the
    disk fills, which then fails the very ledger writes that record live orders.

    MUST be called while holding the dataset lock (every caller is inside
    :func:`write_dataset`'s ``exclusive_file_lock``), so there is no live writer
    whose in-flight temp could be deleted. The age gate is a second belt: only
    temp files older than ``stale_seconds`` are removed."""
    if not path.exists():
        return
    now = time.time()
    pattern = "**/.*.tmp" if recursive else ".*.tmp"
    for tmp in path.glob(pattern):
        try:
            if now - tmp.stat().st_mtime <= stale_seconds:
                continue
            tmp.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # Best-effort cleanup must not break the write that triggered it.
            continue


def _sweep_orphaned_tmp_parts_if_due(
    path: Path,
    *,
    interval_seconds: float = _TMP_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Throttle the expensive full-tree temp sweep per process and dataset.

    Full-PIT datasets can contain hundreds of thousands of partition files. A
    recursive glob before every tiny partition write turns a top-up into
    O(writes * whole-tree walk). _write_part still sweeps the target directory
    on every write; this preserves the broad cleanup safety net without putting
    it on every partition.
    """
    key = str(path.resolve())
    now = time.time()
    with _DATASET_TMP_SWEEP_GUARD:
        last = _DATASET_TMP_SWEEP_LAST.get(key)
        if last is not None and now - last < interval_seconds:
            return
        _DATASET_TMP_SWEEP_LAST[key] = now
    _sweep_orphaned_tmp_parts(path, recursive=True)


def write_dataset(
    df: pl.DataFrame,
    data_root: str | Path,
    dataset: str,
    *,
    partition_by: tuple[str, ...] = ("date", "symbol"),
    append: bool = True,
) -> Path:
    root = ensure_data_root(data_root)
    with exclusive_file_lock(dataset_lock_path(root, dataset), stale_seconds=21_600, poll_seconds=0.01):
        return _write_dataset_unlocked(df, root, dataset, partition_by=partition_by, append=append)


def _write_dataset_unlocked(
    df: pl.DataFrame,
    root: Path,
    dataset: str,
    *,
    partition_by: tuple[str, ...],
    append: bool,
) -> Path:
    path = dataset_path(root, dataset)
    # storage-concurrency-4: opportunistically sweep orphaned `.*.tmp` part files
    # left by a prior crash before this write. Safe here because we hold the
    # dataset lock (no concurrent writer's in-flight temp can be clobbered).
    _sweep_orphaned_tmp_parts(path, recursive=False)
    _sweep_orphaned_tmp_parts_if_due(path)
    if df.is_empty():
        path.mkdir(parents=True, exist_ok=True)
        return path

    if "ts_ms" in df.columns:
        df = with_date_column(df)
    path.mkdir(parents=True, exist_ok=True)

    # Month-bucket the demo/paper ledgers regardless of the partition_by the caller
    # passed (every ledger writer passes partition_by=()): force the immutable-keyed
    # _ledger_month partition so the hot-path write touches only the current month,
    # not the whole-history monolith (reconcile-ledger-5). See LEDGER_BUCKET_SOURCE.
    if dataset in LEDGER_BUCKET_SOURCE:
        df = _with_ledger_month(df, dataset)
        if _LEDGER_MONTH_COL in df.columns:
            partition_by = (_LEDGER_MONTH_COL,)

    partition_cols = [col for col in partition_by if col in df.columns]
    if not partition_cols:
        _write_part(df, path / "part.parquet", dataset=dataset, append=append)
        return path

    for key, part in df.partition_by(partition_cols, as_dict=True, maintain_order=True).items():
        key_tuple = key if isinstance(key, tuple) else (key,)
        part_path = path
        for col, value in zip(partition_cols, key_tuple):
            rendered = encode_symbol_partition(value) if col == "symbol" else str(value)
            part_path = part_path / f"{col}={rendered}"
        part_path.mkdir(parents=True, exist_ok=True)
        _write_part(part, part_path / "part.parquet", dataset=dataset, append=append)
    return path


def read_dataset(data_root: str | Path, dataset: str) -> pl.DataFrame:
    return read_dataset_columns(data_root, dataset)


def _partition_date_ge(file: Path, since: str) -> bool:
    """True if a `date=YYYY-MM-DD`-partitioned file is on/after ``since`` (or has no
    date partition, so it is never pruned). Lets a reader skip old partitions by
    path alone — no parquet opened for pruned dates."""
    for part in file.parts:
        if part.startswith("date="):
            return part[len("date="):] >= since
    return True


def read_dataset_columns(
    data_root: str | Path,
    dataset: str,
    *,
    columns: list[str] | None = None,
    since_date: str | None = None,
) -> pl.DataFrame:
    """Eagerly read a dataset, optionally projecting only ``columns``.

    ``columns=None`` reproduces ``read_dataset``'s full-frame contract exactly.
    Passing an explicit list pushes the projection into ``scan_parquet`` so
    polars only decodes the requested columns from each parquet file — a large
    saving for wide datasets (e.g. klines_1h) on hot read paths. Any requested
    column absent from a partition is tolerated; the projection is intersected
    with the on-disk schema before collecting.

    ``since_date`` (YYYY-MM-DD) prunes `date=`-partitioned files to that date
    forward BEFORE any parquet is opened — a forward-window backtest then never
    pays to read the multi-year tail. Non-date-partitioned files are never pruned.

    A canonical request (e.g. ``funding``) transparently resolves to the
    venue-specific variant actually present on the root (e.g.
    ``binance_usdm_funding``); see :func:`resolve_dataset_name`.
    """
    dataset = resolve_dataset_name(data_root, dataset)
    path = dataset_path(data_root, dataset)
    if not path.exists():
        return pl.DataFrame()
    # Take the same per-dataset lock that writers hold. write_dataset performs
    # read-modify-write under this lock, and writers replace files atomically
    # via temp-file rename; without a reader-side lock, a reader's
    # scan_parquet -> collect can straddle a rename and observe a torn file
    # ("Invalid thrift: end of file"). Acquiring the lock here serialises with
    # writers cheaply (<10ms typical) and guarantees readers see a consistent
    # snapshot of the dataset. The collect() below MUST stay inside the lock so
    # the actual file reads complete before a writer can rename underneath us.
    with exclusive_file_lock(dataset_lock_path(data_root, dataset), stale_seconds=21_600, poll_seconds=0.01):
        # When since_date is set and the dataset is top-level `date=`-partitioned,
        # prune at the DIRECTORY level before globbing: a full `**/*.parquet` walk of
        # a (date,symbol)-partitioned root is ~500k files / tens of seconds, almost
        # all of it discarded. Globbing only the kept date dirs avoids that walk.
        if since_date:
            top_date_dirs = [d for d in path.glob("date=*") if d.is_dir()]
            if top_date_dirs:
                kept = [d for d in top_date_dirs if d.name[len("date="):] >= since_date]
                files = sorted(f for d in kept for f in d.glob("**/*.parquet"))
            else:
                files = [f for f in sorted(path.glob("**/*.parquet")) if _partition_date_ge(f, since_date)]
        else:
            files = sorted(path.glob("**/*.parquet"))
        return _collect_files(files, columns=columns)


def _collect_files(
    files: list[Path],
    *,
    columns: list[str] | None,
) -> pl.DataFrame:
    """Union parquet parts and hide the internal month-partition column."""
    if not files:
        return pl.DataFrame()
    file_paths = [str(file) for file in files]
    try:
        lf = pl.scan_parquet(file_paths)
        names = lf.collect_schema().names()
        if _LEDGER_MONTH_COL in names:
            lf = lf.drop(_LEDGER_MONTH_COL)
            names = [n for n in names if n != _LEDGER_MONTH_COL]
        if columns is not None:
            lf = lf.select([col for col in columns if col in names])
        out = lf.collect()
    except (
        pl.exceptions.SchemaError,
        pl.exceptions.ColumnNotFoundError,
        pl.exceptions.SchemaFieldNotFoundError,
        pl.exceptions.StructFieldNotFoundError,
        pl.exceptions.ComputeError,
        pl.exceptions.ShapeError,
    ):
        # Schema can evolve across partitions. Fall back to per-file reads plus
        # diagonal concat; genuinely unreadable files still fail loudly.
        frames = [pl.read_parquet(file) for file in file_paths]
        out = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        if _LEDGER_MONTH_COL in out.columns:
            out = out.drop(_LEDGER_MONTH_COL)
        if columns is not None and not out.is_empty():
            out = out.select([col for col in columns if col in out.columns])
    return out


def _write_part(df: pl.DataFrame, path: Path, *, dataset: str, append: bool) -> None:
    # Invariant: only ever called from inside write_dataset, which holds
    # `exclusive_file_lock(dataset_lock_path(...))`. The pid + nanosecond temp
    # filename therefore can't collide with a concurrent writer — there ISN'T
    # one. If this is ever called from outside that lock, switch to a uuid4
    # temp name and re-derive the dedup story per dataset.
    _sweep_orphaned_tmp_parts(path.parent, recursive=False)
    output = df
    if append and path.exists():
        existing = pl.read_parquet(path)
        output = pl.concat([existing, output], how="diagonal_relaxed")
    keys = [col for col in DATASET_KEYS.get(dataset, ()) if col in output.columns]
    if keys:
        # Dedup by natural key. If rows are versioned, the freshest version wins;
        # ties retain append order so the new row wins.
        if "updated_at_ms" in output.columns:
            output = output.sort("updated_at_ms", nulls_last=False, maintain_order=True)
        output = output.unique(subset=keys, keep="last")
    sort_cols = [col for col in ("symbol", "ts_ms") if col in output.columns]
    if sort_cols:
        output = output.sort(sort_cols)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        output.write_parquet(temp_path)
        # fsync before the rename (round 4): the rename is atomic against a
        # PROCESS crash, but a hard power loss inside the page-cache window can
        # surface a truncated part file — and because this is a read-modify-
        # rewrite, that file is the only copy of the bucket's whole history
        # (the demo-forward evidence record). Cost is negligible at ledger
        # write rates.
        fd = os.open(temp_path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        temp_path.replace(path)
        # storage-concurrency-5: fsync the PARENT DIRECTORY after the rename.
        # The file-fsync above makes the temp file's CONTENTS durable, but on
        # POSIX the rename itself (the directory entry now pointing `path` at the
        # new inode) is only durable after fsync of the containing directory fd.
        # Without it a hard power loss after replace() can revert the name to the
        # OLD inode, losing the most recent ledger update on a read-modify-rewrite
        # single-copy part file. Unsupported directory fsync failures are
        # swallowed; file-content durability is already established above.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)
