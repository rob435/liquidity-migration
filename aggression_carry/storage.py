from __future__ import annotations

from pathlib import Path

import polars as pl


DATASETS = {
    "instruments",
    "klines_1h",
    "klines_5m",
    "raw_public_trades",
    "signed_flow_1m",
    "signed_flow_1h",
    "funding",
    "open_interest",
    "ticker_snapshots",
    "features_1h",
    "research_returns",
    "research_timestamp_ic",
    "research_quantile_ledger",
    "research_monthly_spreads",
    "portfolio_backtest",
    "portfolio_periods",
    "portfolio_positions",
    "portfolio_symbol_attribution",
    "portfolio_monthly_attribution",
}

DATASET_KEYS = {
    "instruments": ("symbol",),
    "klines_1h": ("ts_ms", "symbol"),
    "klines_5m": ("ts_ms", "symbol"),
    "raw_public_trades": ("symbol", "trade_id"),
    "signed_flow_1m": ("ts_ms", "symbol"),
    "signed_flow_1h": ("ts_ms", "symbol"),
    "funding": ("ts_ms", "symbol"),
    "open_interest": ("ts_ms", "symbol"),
    "ticker_snapshots": ("ts_ms", "symbol"),
    "features_1h": ("ts_ms", "symbol"),
    "research_returns": ("ts_ms", "symbol"),
    "research_timestamp_ic": ("signal", "horizon_h", "ts_ms"),
    "research_quantile_ledger": ("signal", "horizon_h", "ts_ms", "symbol"),
    "research_monthly_spreads": ("signal", "horizon_h", "month"),
    "portfolio_backtest": ("scenario",),
    "portfolio_periods": ("scenario", "ts_ms"),
    "portfolio_positions": ("scenario", "ts_ms", "symbol"),
    "portfolio_symbol_attribution": ("scenario", "symbol"),
    "portfolio_monthly_attribution": ("scenario", "month"),
}


def dataset_path(data_root: str | Path, dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    return Path(data_root).expanduser() / dataset


def ensure_data_root(data_root: str | Path) -> Path:
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    return pl.scan_parquet([str(file) for file in files]).collect()


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
