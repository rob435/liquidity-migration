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
    "ticker_snapshots",
    "archive_trade_manifest",
    "universe_current",
    "volume_alpha_features",
    "volume_alpha_metrics",
    "volume_alpha_portfolios",
    "volume_backtest_trades",
    "volume_backtest_baskets",
    "volume_backtest_equity",
    "volume_backtest_equity_vs_btc",
    "volume_backtest_monthly",
    "volume_backtest_grid",
    "daily_close_fade_features",
    "daily_close_fade_trades",
    "daily_close_fade_entry_fills",
    "daily_close_fade_baskets",
    "daily_close_fade_grid",
    "daily_close_fade_diagnostic_buckets",
    "daily_close_fade_diagnostic_top_baskets",
    "daily_close_fade_diagnostic_ic",
    "daily_close_fade_diagnostic_monthly",
    "daily_close_fade_diagnostic_month_consistency",
    "forward_scan_features",
    "forward_scan_candidates",
    "forward_paper_trades",
    "forward_paper_slices",
    "forward_paper_baskets",
    "demo_execution_orders",
    "demo_fast_protection_state",
    "demo_fast_protection_events",
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
    "ticker_snapshots": ("ts_ms", "symbol"),
    "archive_trade_manifest": ("symbol", "date", "url"),
    "universe_current": ("snapshot_ts_ms", "symbol"),
    "volume_alpha_features": ("ts_ms", "symbol"),
    "volume_alpha_metrics": ("signal", "horizon_d"),
    "volume_alpha_portfolios": ("score", "scenario", "hold_days", "quantile"),
    "volume_backtest_trades": ("trade_id",),
    "volume_backtest_baskets": ("basket_id",),
    "volume_backtest_equity": ("ts_ms",),
    "volume_backtest_equity_vs_btc": ("ts_ms",),
    "volume_backtest_monthly": ("month",),
    "volume_backtest_grid": ("grid_id",),
    "daily_close_fade_features": ("signal_ts_ms", "symbol"),
    "daily_close_fade_trades": ("trade_id",),
    "daily_close_fade_entry_fills": ("trade_id", "slice_index"),
    "daily_close_fade_baskets": ("basket_id",),
    "daily_close_fade_grid": ("grid_id",),
    "daily_close_fade_diagnostic_buckets": ("score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "bucket"),
    "daily_close_fade_diagnostic_top_baskets": ("score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n"),
    "daily_close_fade_diagnostic_ic": ("score", "signal_minute", "entry_delay_minutes", "horizon_minutes"),
    "daily_close_fade_diagnostic_monthly": (
        "score",
        "signal_minute",
        "entry_delay_minutes",
        "horizon_minutes",
        "top_n",
        "month",
    ),
    "daily_close_fade_diagnostic_month_consistency": (
        "score",
        "signal_minute",
        "entry_delay_minutes",
        "horizon_minutes",
        "top_n",
    ),
    "forward_scan_features": ("scan_id", "symbol"),
    "forward_scan_candidates": ("scan_id", "symbol"),
    "forward_paper_trades": ("trade_id",),
    "forward_paper_slices": ("trade_id", "slice_index"),
    "forward_paper_baskets": ("basket_id",),
    "demo_execution_orders": ("order_link_id",),
    "demo_fast_protection_state": ("paper_trade_id", "symbol"),
    "demo_fast_protection_events": ("event_id",),
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
def exclusive_file_lock(path: str | Path, *, stale_seconds: int = 600, poll_seconds: float = 0.05) -> Iterator[None]:
    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
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
    path = dataset_path(data_root, dataset)
    if not path.exists():
        return pl.DataFrame()
    files = sorted(path.glob("**/*.parquet"))
    if not files:
        return pl.DataFrame()
    file_paths = [str(file) for file in files]
    try:
        return pl.scan_parquet(file_paths).collect()
    except pl.exceptions.SchemaError:
        frames = [pl.read_parquet(file) for file in file_paths]
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _write_part(df: pl.DataFrame, path: Path, *, dataset: str, append: bool) -> None:
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
    output.write_parquet(path)
