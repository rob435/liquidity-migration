from __future__ import annotations

from pathlib import Path

import polars as pl

from aggression_carry.storage import read_dataset, write_dataset


def test_incremental_parquet_writes_merge_existing_partition(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "buy_quote": 100.0, "sell_quote": 50.0},
        ]
    )
    second = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_060_000, "symbol": "BTCUSDT", "buy_quote": 200.0, "sell_quote": 75.0},
        ]
    )

    write_dataset(first, tmp_path, "signed_flow_1m")
    write_dataset(second, tmp_path, "signed_flow_1m")
    stored = read_dataset(tmp_path, "signed_flow_1m")

    assert stored.height == 2
    assert stored["buy_quote"].sum() == 300.0


def test_incremental_parquet_writes_replace_duplicate_keys(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "buy_quote": 100.0, "sell_quote": 50.0},
        ]
    )
    correction = pl.DataFrame(
        [
            {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "buy_quote": 125.0, "sell_quote": 50.0},
        ]
    )

    write_dataset(first, tmp_path, "signed_flow_1m")
    write_dataset(correction, tmp_path, "signed_flow_1m")
    stored = read_dataset(tmp_path, "signed_flow_1m")

    assert stored.height == 1
    assert stored["buy_quote"][0] == 125.0
