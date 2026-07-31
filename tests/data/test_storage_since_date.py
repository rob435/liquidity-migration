"""Tests for read_dataset_columns(since_date=...) date-partition pruning — the
forward-window read floor that lets a backtest skip the multi-year tail."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.data.storage import _partition_date_ge, read_dataset_columns, write_dataset


def _seed(root: Path) -> None:
    # date-partitioned (date, symbol) dataset across three days, two symbols.
    rows = []
    for day in ("2026-01-01", "2026-03-15", "2026-06-10"):
        for sym in ("BTCUSDT", "ETHUSDT"):
            rows.append({"ts_ms": int(pl.Series([day]).str.to_date().dt.epoch("ms")[0]),
                         "date": day, "symbol": sym, "close": 1.0})
    write_dataset(pl.DataFrame(rows), root, "klines_1h", partition_by=("date", "symbol"))


def test_partition_date_ge_extracts_date_part():
    assert _partition_date_ge(Path("root/klines_1h/date=2026-06-10/symbol=BTCUSDT/part.parquet"), "2026-01-05")
    assert not _partition_date_ge(Path("root/klines_1h/date=2026-01-01/symbol=BTCUSDT/part.parquet"), "2026-01-05")
    # no date= partition in path -> never pruned
    assert _partition_date_ge(Path("root/funding/part.parquet"), "2026-01-05")


def test_since_date_prunes_old_partitions(tmp_path):
    _seed(tmp_path)
    full = read_dataset_columns(tmp_path, "klines_1h")
    assert sorted(full["date"].unique().to_list()) == ["2026-01-01", "2026-03-15", "2026-06-10"]

    pruned = read_dataset_columns(tmp_path, "klines_1h", since_date="2026-03-15")
    assert sorted(pruned["date"].unique().to_list()) == ["2026-03-15", "2026-06-10"]
    assert pruned.height == 4  # 2 days * 2 symbols


def test_since_date_none_is_full_read(tmp_path):
    _seed(tmp_path)
    a = read_dataset_columns(tmp_path, "klines_1h")
    b = read_dataset_columns(tmp_path, "klines_1h", since_date=None)
    assert a.sort(["date", "symbol"]).equals(b.sort(["date", "symbol"]))
