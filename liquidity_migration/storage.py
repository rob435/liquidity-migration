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


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    key = str(lock_path.resolve())
    with _DATASET_THREAD_LOCKS_GUARD:
        lock = _DATASET_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DATASET_THREAD_LOCKS[key] = lock
        return lock


def _unlink_with_retry(lock_path: Path, *, retries: int = 40, delay: float = 0.05) -> None:
    """Unlink a lock file with retries on Windows PermissionError (WinError 32).

    Windows raises ``PermissionError: [WinError 32] The process cannot
    access the file because it is being used by another process`` whenever
    ANY process holds the file open — including the brief windows when
    another process is mid-``_read_lock_text_safe`` reading our payload to
    check liveness. With many parallel sweep workers all contending on the
    same dataset lock, those windows overlap with the lock holder's release
    and naively-`.unlink()`-only-catching-FileNotFoundError crashes the
    whole subprocess (and propagates failure to the orchestrator).

    The retry loop tolerates this transient contention. If retries exhaust,
    the file is left in place — the next acquire's stale-detection (dead
    pid via _lock_owner_is_dead) will clean it up, so no permanent leak.
    Defaults: 40 retries × 50ms ≈ 2s, double the 1s ``_read_lock_text_safe``
    timeout the contending readers wait."""
    for _ in range(retries):
        try:
            lock_path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(delay)
    # Last resort: leave the file. The next acquire's stale-detection
    # path is the safety net.


DATASETS = {
    "instruments",
    "klines_1m",
    "klines_1h",
    "klines_5m",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    # Read-only legacy: download path was deleted with the signed_flow
    # cleanup (validated as not-an-edge across 5+ tests, see commit
    # 6e5e977). volume_events still consumes signed_flow_1h when present
    # via _signed_flow_feature_frame for the taker_imbalance research
    # knobs; without this allowlist entry read_dataset throws
    # "Unknown dataset" on roots that have no signed_flow_1h directory.
    "signed_flow_1h",
    "ticker_snapshots",
    "archive_trade_manifest",
    "universe_current",
    "event_demo_klines_1h",
    "event_demo_trades",
    "event_demo_orders",
    "event_demo_cycles",
    "long_native_demo_trades",
    "long_native_demo_orders",
    "long_native_demo_cycles",
    # B.4: paper-shadow ledger for the long sleeve. Same schema as the demo
    # ledger; written by the paper runner which records idealised fills at the
    # signal price and never submits orders.
    "long_native_paper_trades",
    "long_native_paper_orders",
    "long_native_paper_cycles",
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
    "klines_1m": ("ts_ms", "symbol"),
    "klines_1h": ("ts_ms", "symbol"),
    "klines_5m": ("ts_ms", "symbol"),
    "funding": ("ts_ms", "symbol"),
    "open_interest": ("ts_ms", "symbol"),
    "mark_price_1h": ("ts_ms", "symbol"),
    "index_price_1h": ("ts_ms", "symbol"),
    "premium_index_1h": ("ts_ms", "symbol"),
    "signed_flow_1h": ("ts_ms", "symbol"),
    "ticker_snapshots": ("ts_ms", "symbol"),
    "archive_trade_manifest": ("symbol", "date", "url"),
    "universe_current": ("snapshot_ts_ms", "symbol"),
    "event_demo_klines_1h": ("ts_ms", "symbol"),
    "event_demo_trades": ("trade_id",),
    "event_demo_orders": ("order_link_id",),
    "event_demo_cycles": ("cycle_id",),
    "long_native_demo_trades": ("trade_id",),
    "long_native_demo_orders": ("order_link_id",),
    "long_native_demo_cycles": ("cycle_id",),
    "long_native_paper_trades": ("trade_id",),
    "long_native_paper_orders": ("order_link_id",),
    "long_native_paper_cycles": ("cycle_id",),
    "binance_usdm_klines_1h": ("ts_ms", "symbol"),
    "binance_usdm_mark_price_1h": ("ts_ms", "symbol"),
    "binance_usdm_index_price_1h": ("ts_ms", "symbol"),
    "binance_usdm_premium_index_1h": ("ts_ms", "symbol"),
    "binance_usdm_funding": ("ts_ms", "symbol"),
    "binance_usdm_open_interest": ("ts_ms", "symbol"),
    "binance_usdm_taker_flow_1h": ("ts_ms", "symbol"),
}


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
                # POSIX-style "file already exists" — fall through to the
                # liveness / staleness / wait logic below.
                pass
            except PermissionError:
                # Windows-specific: when another process is mid-unlink the
                # file enters "delete-pending" state. A concurrent O_CREAT|O_EXCL
                # then raises PermissionError [Errno 13] / EACCES instead of
                # FileExistsError, even though the semantically-correct answer
                # is "the file exists, try again". Treat exactly like
                # FileExistsError so the same recovery / wait path runs.
                # Observed in Phase 0 dispatch: control cell crashed on lock
                # acquire of funding.lock while another worker was releasing it.
                pass
            else:
                # Got the lock fresh — break out of the wait loop.
                break
            if _lock_owner_is_dead(lock_path):
                _unlink_with_retry(lock_path)
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
                _unlink_with_retry(lock_path)
                continue
            if stale_seconds > 0 and age > stale_seconds:
                _unlink_with_retry(lock_path)
                continue
            time.sleep(max(poll_seconds, 0.0))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created": time.time()}))
            yield
        finally:
            _unlink_with_retry(lock_path)


def _read_lock_text_safe(lock_path: Path, timeout: float = 1.0) -> str | None:
    """Read the lock file text with a hard timeout. Returns None if the read
    blocks (e.g. Windows 'delete-pending' state where another thread of this
    same process unlinked the file but a handle keeps it half-alive — the
    naive Path.read_text() hangs in Path.open() forever). The outer lock
    loop treats None as 'unreadable, assume stale' and self-heals."""
    import threading
    box: list[str | None] = [None]
    def _read() -> None:
        try:
            box[0] = lock_path.read_text(encoding="utf-8")
        except Exception:
            pass
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return box[0]


def _lock_owner_is_dead(lock_path: Path) -> bool:
    text = _read_lock_text_safe(lock_path)
    if text is None:
        # File missing OR read hung (Windows delete-pending). Either way it
        # is safe to treat the owner as dead — the next iteration's unlink
        # is a no-op when the file is gone, and breaks delete-pending stalls
        # when it isn't.
        return True
    try:
        payload = json.loads(text)
        pid = int(payload.get("pid") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OverflowError:
        return True
    except OSError as exc:
        # Windows: os.kill(pid, 0) on a non-existent pid raises a bare OSError
        # with winerror 87 ("the parameter is incorrect"), NOT ProcessLookupError.
        # Without this branch, stale-lock recovery never fires on Windows: a
        # lock orphaned by a killed process is treated as live and every
        # subsequent read_dataset/write_dataset blocks until the 6h stale
        # timeout. winerror 87 from os.kill means the pid is not a live
        # process -> treat the owner as dead.
        if getattr(exc, "winerror", None) == 87:
            return True
        return False
    return False


def _lock_payload_is_invalid(lock_path: Path) -> bool:
    text = _read_lock_text_safe(lock_path)
    if text is None:
        # File missing or read hung — treat as invalid so the outer loop
        # can unlink and retry (self-heals Windows delete-pending stalls).
        return True
    try:
        payload = json.loads(text)
        pid = int(payload.get("pid") or 0)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return True
    return pid <= 0


def with_date_column(df: pl.DataFrame, ts_col: str = "ts_ms") -> pl.DataFrame:
    if "date" in df.columns:
        return df
    return df.with_columns(
        pl.from_epoch(pl.col(ts_col), time_unit="ms")
        .dt.strftime("%Y-%m-%d")
        .alias("date")
    )


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
    if df.is_empty():
        path.mkdir(parents=True, exist_ok=True)
        return path

    if "ts_ms" in df.columns:
        df = with_date_column(df)
    path.mkdir(parents=True, exist_ok=True)

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


def read_dataset_columns(
    data_root: str | Path,
    dataset: str,
    *,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Eagerly read a dataset, optionally projecting only ``columns``.

    ``columns=None`` reproduces ``read_dataset``'s full-frame contract exactly.
    Passing an explicit list pushes the projection into ``scan_parquet`` so
    polars only decodes the requested columns from each parquet file — a large
    saving for wide datasets (e.g. klines_1h) on hot read paths. Any requested
    column absent from a partition is tolerated; the projection is intersected
    with the on-disk schema before collecting.
    """
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
        files = sorted(path.glob("**/*.parquet"))
        if not files:
            return pl.DataFrame()
        file_paths = [str(file) for file in files]
        try:
            lf = pl.scan_parquet(file_paths)
            if columns is not None:
                present = [col for col in columns if col in lf.collect_schema().names()]
                lf = lf.select(present)
            return lf.collect()
        except pl.exceptions.SchemaError:
            frames = [pl.read_parquet(file) for file in file_paths]
            joined = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
            if columns is not None and not joined.is_empty():
                joined = joined.select([col for col in columns if col in joined.columns])
            return joined


def _write_part(df: pl.DataFrame, path: Path, *, dataset: str, append: bool) -> None:
    # Invariant: only ever called from inside write_dataset, which holds
    # `exclusive_file_lock(dataset_lock_path(...))`. The pid + nanosecond temp
    # filename therefore can't collide with a concurrent writer — there ISN'T
    # one. If this is ever called from outside that lock, switch to a uuid4
    # temp name and re-derive the dedup story per dataset.
    output = df
    if append and path.exists():
        existing = pl.read_parquet(path)
        output = pl.concat([existing, output], how="diagonal_relaxed")
    keys = [col for col in DATASET_KEYS.get(dataset, ()) if col in output.columns]
    if keys:
        output = output.unique(subset=keys, keep="last")
    sort_cols = [col for col in ("symbol", "ts_ms") if col in output.columns]
    if sort_cols:
        output = output.sort(sort_cols)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        output.write_parquet(temp_path)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
