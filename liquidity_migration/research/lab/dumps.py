"""One read of each point-in-time dataset, kept as a single parquet per dataset.

A dataset read from the root holds that dataset's exclusive file lock for the
whole read, and the funding root is hundreds of thousands of small files (about
five minutes). Every lab script reads these dumps instead, so concurrent studies
stop queueing on the lock.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Mapping, Sequence

import polars as pl

from liquidity_migration.data.storage import read_dataset_columns

#: The columns the daily panel needs from each dataset. A dataset not listed
#: here is dumped whole.
PANEL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "klines_1h": ("ts_ms", "symbol", "open", "high", "low", "close", "turnover_quote"),
    "funding": ("ts_ms", "symbol", "funding_rate"),
    "open_interest": ("ts_ms", "symbol", "open_interest", "open_interest_value"),
    "premium_index_1h": ("ts_ms", "symbol", "open", "high", "low", "close"),
}

DEFAULT_DATASETS: tuple[str, ...] = tuple(PANEL_COLUMNS)


def inputs_dir(out_dir: str | Path) -> Path:
    return Path(out_dir).expanduser() / "inputs"


def dump_path(out_dir: str | Path, dataset: str) -> Path:
    return inputs_dir(out_dir) / f"{dataset}.parquet"


def dump_inputs(
    data_root: str | Path,
    out_dir: str | Path,
    *,
    datasets: Sequence[str] = DEFAULT_DATASETS,
    start_ms: int | None = None,
    end_ms: int | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """Write ``out_dir/inputs/<dataset>.parquet`` for each dataset and return the paths.

    ``start_ms`` is inclusive and ``end_ms`` exclusive, both on ``ts_ms``. The
    panel books a funding settlement stamped exactly midnight to the day that
    ended, so a window that ends at midnight loses the last day's closing
    settlement; end one day later than the last panel day wanted. A dump that
    already exists is left alone unless ``force`` is set.
    """
    target_dir = inputs_dir(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    since = None
    if start_ms is not None:
        since = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")
    written: dict[str, Path] = {}
    for name in datasets:
        target = target_dir / f"{name}.parquet"
        written[name] = target
        if target.exists() and not force:
            continue
        columns = list(PANEL_COLUMNS[name]) if name in PANEL_COLUMNS else None
        frame = read_dataset_columns(data_root, name, columns=columns, since_date=since)
        if frame.is_empty():
            raise FileNotFoundError(f"{name}: nothing to read under {Path(data_root).expanduser()}")
        if "ts_ms" in frame.columns:
            if start_ms is not None:
                frame = frame.filter(pl.col("ts_ms") >= start_ms)
            if end_ms is not None:
                frame = frame.filter(pl.col("ts_ms") < end_ms)
        # Written beside the target and renamed, so a killed dump never leaves a
        # half file that the next run would skip as finished.
        partial = target.with_name(target.name + ".partial")
        frame.write_parquet(partial)
        partial.replace(target)
    return written
