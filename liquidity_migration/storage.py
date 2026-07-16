from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import polars as pl


# Per-process thread-lock per dataset path. The file-based lock that follows
# only serializes ACROSS processes; within a single process, multiple worker
# threads contending on the file lock can wedge because they all write the
# same pid into the lock file and then read it back as "my own pid -> still
# alive somewhere -> keep waiting" even when the actual holder has silently
# dropped the file via an unlink race. This per-process lock ensures only
# one thread of this process ever enters the file-lock acquire/release dance.
_DATASET_THREAD_LOCKS: dict[str, threading.Lock] = {}
_DATASET_THREAD_LOCKS_GUARD = threading.Lock()
_DATASET_TMP_SWEEP_LAST: dict[str, float] = {}
_DATASET_TMP_SWEEP_GUARD = threading.Lock()


# storage-concurrency-2: per-acquisition tokens currently OWNED by THIS live
# process. _lock_owner_is_dead short-circuits a pid==os.getpid() payload to
# "alive" so a process never evicts its own live lock. But a singleton lock
# taken with stale_seconds=0 has no age-eviction
# backstop: if the daemon crashes holding the lock and systemd restarts it
# within RestartSec and the kernel hands the SAME pid back (Linux pids cycle to
# pid_max), the new process reads its dead predecessor's pid==getpid() and would
# block forever. The token written into every payload (line ~286) is the
# tiebreaker: a token NOT in this set is from a previous incarnation that merely
# reused our pid, so the owner is genuinely dead and the lock is evictable.
_LIVE_OWNED_TOKENS: set[str] = set()
_LIVE_OWNED_TOKENS_GUARD = threading.Lock()


def _register_owned_token(token: str) -> None:
    with _LIVE_OWNED_TOKENS_GUARD:
        _LIVE_OWNED_TOKENS.add(token)


def _unregister_owned_token(token: str) -> None:
    with _LIVE_OWNED_TOKENS_GUARD:
        _LIVE_OWNED_TOKENS.discard(token)


def _token_is_live_owned(token: str | None) -> bool:
    if not token:
        return False
    with _LIVE_OWNED_TOKENS_GUARD:
        return token in _LIVE_OWNED_TOKENS


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
    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Acquire the per-process thread-lock FIRST so only one thread of this
    # process can be in the file-lock body at a time. The file-lock below
    # then only serializes across processes, which is its real job and what
    # it actually handles correctly.
    with _thread_lock_for(lock_path):
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                # Got the lock fresh — break out of the wait loop.
                break
            if _lock_owner_is_dead(lock_path):
                lock_path.unlink(missing_ok=True)
                continue
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            invalid_lock_stale = (
                _lock_payload_is_invalid(lock_path)
                and invalid_lock_stale_seconds >= 0
                and age > invalid_lock_stale_seconds
            )
            if invalid_lock_stale:
                lock_path.unlink(missing_ok=True)
                continue
            if stale_seconds > 0 and age > stale_seconds:
                lock_path.unlink(missing_ok=True)
                continue
            time.sleep(max(poll_seconds, 0.0))
        # Capture the inode of the file WE created (fd is still open here, before
        # os.fdopen consumes it) so release only unlinks OUR lock. If our lock is
        # stale-evicted mid-critical-section and a successor recreates the path with a
        # new inode, an unconditional unlink-by-path would delete the SUCCESSOR's lock
        # and admit a second concurrent writer (CROS-1).
        try:
            _owned = os.fstat(fd)
        except OSError:
            os.close(fd)
            raise
        owned_key = (_owned.st_dev, _owned.st_ino)
        # CROS-1b (audit 2026-06-09): (dev, ino) equality is NOT proof the path is
        # still OUR lock — ext4/overlayfs can hand a freed inode straight to a
        # successor's lock file created at the same path. A per-acquisition token
        # in the payload is the tiebreaker the inode check can't provide.
        owned_token = os.urandom(16).hex()
        # Register the token as live-owned BEFORE writing the payload: once the
        # bytes are on disk a concurrent _lock_owner_is_dead read could observe
        # our pid+token, and it must find the token live (not a reused-pid
        # ghost). Registering first closes that window.
        _register_owned_token(owned_token)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created": time.time(), "token": owned_token}))
            yield
        finally:
            _unregister_owned_token(owned_token)
            try:
                cur = os.stat(lock_path)
                release_ours = (cur.st_dev, cur.st_ino) == owned_key
            except FileNotFoundError:
                release_ours = False  # already gone -> nothing to unlink
            except OSError:
                release_ours = False
            if release_ours:
                # Inode matched — confirm the payload token before unlinking
                # (inode-reuse defense, CROS-1b).
                text = _read_lock_text_safe(lock_path)
                if text is None:
                    release_ours = False
                else:
                    try:
                        release_ours = json.loads(text).get("token") == owned_token
                    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                        release_ours = False  # foreign/corrupt payload -> not ours
            if release_ours:
                lock_path.unlink(missing_ok=True)


def _read_lock_text_safe(lock_path: Path) -> str | None:
    try:
        return lock_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _pid_started_after(pid: int, created_ts: float) -> bool | None:
    """True if the live process ``pid`` started strictly AFTER ``created_ts`` (epoch
    seconds) — i.e. the lock's pid was REUSED by a newer process, so the original
    lock owner is actually dead. None when the start time can't be determined
    (non-Linux / no /proc), so the caller stays conservative and does NOT evict on a
    pid that os.kill reports as live. Linux-only via /proc/<pid>/stat; never raises
    (CROS-2: os.kill(pid,0) alone false-positives "alive" on a reused pid)."""
    if created_ts <= 0.0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            # comm (field 2) may contain spaces/parens; split on the LAST ") " so the
            # remaining whitespace-split fields start at field 3 (state).
            after_comm = fh.read().rsplit(") ", 1)[1].split()
        starttime_ticks = float(after_comm[19])  # field 22 (starttime), 0-indexed from field 3
        btime = 0.0
        with open("/proc/stat", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    break
        if btime <= 0.0:
            return None
        clk = os.sysconf("SC_CLK_TCK")
        if clk <= 0:
            return None
        proc_start_epoch = btime + starttime_ticks / clk
        return proc_start_epoch > created_ts + 1.0  # 1s slack vs clock granularity
    except Exception:  # noqa: BLE001 - any /proc parsing failure -> "unknown", stay conservative
        return None


def _lock_owner_is_dead(lock_path: Path) -> bool:
    text = _read_lock_text_safe(lock_path)
    if text is None:
        return True
    try:
        payload = json.loads(text)
        pid = int(payload.get("pid") or 0)
        created_ts = float(payload.get("created") or 0.0)
        token = payload.get("token")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if (
        pid <= 0
        or created_ts <= 0.0
        or type(token) is not str
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
    ):
        return False
    if pid == os.getpid():
        # Normally a lock bearing our own pid is our own LIVE lock -> not dead.
        # But after a crash+fast-restart the kernel can hand the same pid to the
        # successor (Linux pids cycle to pid_max). The successor would then read
        # its dead predecessor's pid==getpid() and, with no age backstop on a
        # stale_seconds=0 singleton lock, block forever (storage-concurrency-2).
        # The per-acquisition token disambiguates: if the payload's token is one
        # WE currently own it is genuinely our live lock; otherwise it is a
        # predecessor that merely reused our pid and is dead -> evictable. A
        return not _token_is_live_owned(token)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Foreign live pid — but it could be a REUSED pid whose original (dead) owner
        # held this lock. If the live pid started after the lock was created, evict.
        return _pid_started_after(pid, created_ts) is True
    except OverflowError:
        return True
    except OSError:
        return False
    # os.kill succeeded -> a live, signalable process holds this pid. It may be a
    # REUSED pid (the original owner was killed without cleanup); if the live process
    # started after the lock was created, the real owner is dead -> evict (CROS-2).
    return _pid_started_after(pid, created_ts) is True


def _lock_payload_is_invalid(lock_path: Path) -> bool:
    text = _read_lock_text_safe(lock_path)
    if text is None:
        return True
    try:
        payload = json.loads(text)
        pid = int(payload.get("pid") or 0)
        created_ts = float(payload.get("created") or 0.0)
        token = payload.get("token")
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return True
    return (
        pid <= 0
        or created_ts <= 0.0
        or type(token) is not str
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
    )


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
            part_path = part_path / f"{col}={value}"
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
