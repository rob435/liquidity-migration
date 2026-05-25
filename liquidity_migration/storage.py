from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import polars as pl


DATASETS = {
    "instruments",
    "klines_1m",
    "klines_1h",
    "klines_5m",
    "raw_public_trades",
    "signed_flow_1m",
    "signed_flow_1h",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
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
    "raw_public_trades": ("symbol", "trade_id"),
    "signed_flow_1m": ("ts_ms", "symbol"),
    "signed_flow_1h": ("ts_ms", "symbol"),
    "funding": ("ts_ms", "symbol"),
    "open_interest": ("ts_ms", "symbol"),
    "mark_price_1h": ("ts_ms", "symbol"),
    "index_price_1h": ("ts_ms", "symbol"),
    "premium_index_1h": ("ts_ms", "symbol"),
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
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_owner_is_dead(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
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
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if stale_seconds > 0 and age > stale_seconds:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            time.sleep(max(poll_seconds, 0.0))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "created": time.time()}))
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _lock_owner_is_dead(lock_path: Path) -> bool:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
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
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
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
