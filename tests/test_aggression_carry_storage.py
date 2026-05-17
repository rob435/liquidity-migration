from __future__ import annotations

import json
import os
import time
from pathlib import Path

import polars as pl

from aggression_carry.storage import dataset_lock_path, exclusive_file_lock, read_dataset, write_dataset


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


def test_event_demo_trades_dedupe_by_trade_id(tmp_path: Path) -> None:
    trade = pl.DataFrame(
        [{"trade_id": "trade-1", "symbol": "BTCUSDT", "ts_ms": 1_700_000_000_000, "return": 0.01}]
    )

    write_dataset(trade, tmp_path, "event_demo_trades")
    write_dataset(trade.with_columns(pl.lit(0.02).alias("return")), tmp_path, "event_demo_trades")

    stored = read_dataset(tmp_path, "event_demo_trades")

    assert stored.height == 1
    assert stored["return"][0] == 0.02


def test_read_dataset_handles_schema_evolution_across_partitions(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {
                "trade_id": "trade-1",
                "symbol": "BTCUSDT",
                "date": "2026-01-15",
                "exit_reason": "event_decay",
            }
        ]
    )
    second = pl.DataFrame(
        [
            {
                "trade_id": "trade-2",
                "symbol": "ETHUSDT",
                "date": "2026-01-16",
                "exit_reason": "stop_loss",
                "trigger_price": 99.5,
            }
        ]
    )

    write_dataset(first, tmp_path, "event_demo_trades")
    write_dataset(second, tmp_path, "event_demo_trades")

    stored = read_dataset(tmp_path, "event_demo_trades")

    assert stored.height == 2
    assert "trigger_price" in stored.columns
    assert stored.filter(pl.col("trade_id") == "trade-1").row(0, named=True)["trigger_price"] is None
    assert stored.filter(pl.col("trade_id") == "trade-2").row(0, named=True)["trigger_price"] == 99.5


def test_exclusive_file_lock_cleans_up_lock_file(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_exclusive_file_lock_recovers_dead_pid_lock_even_without_stale_timeout(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 2_147_483_647, "created": 1}), encoding="utf-8")

    with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] != 2_147_483_647

    assert not lock_path.exists()


def test_exclusive_file_lock_recovers_malformed_lock_after_grace_without_stale_timeout(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    old_ts = time.time() - 10.0
    os.utime(lock_path, (old_ts, old_ts))

    with exclusive_file_lock(
        lock_path,
        stale_seconds=0,
        poll_seconds=0.0,
        invalid_lock_stale_seconds=0.01,
    ):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()

    assert not lock_path.exists()
