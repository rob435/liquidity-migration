from __future__ import annotations

import json
import os
import time
from pathlib import Path

import polars as pl

from liquidity_migration.storage import dataset_lock_path, exclusive_file_lock, read_dataset, write_dataset


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


def test_event_demo_orders_concurrent_writers_do_not_lose_rows(tmp_path: Path) -> None:
    """The demo (entry) and risk (exit) services BOTH write to
    event_demo_orders. The write path is read-modify-write under a per-dataset
    exclusive file lock with temp-file-rename atomicity. This test pins that
    contract: 8 threads writing 25 unique order_link_ids each must produce a
    final parquet with exactly 200 rows, no duplicates, no torn writes.
    """
    from concurrent.futures import ThreadPoolExecutor
    rows_per_writer = 25
    writer_count = 8

    def write_batch(writer_id: int) -> None:
        batch = pl.DataFrame(
            [
                {
                    "order_link_id": f"link-w{writer_id}-r{i}",
                    "ts_ms": 1_700_000_000_000 + writer_id * 1000 + i,
                    "symbol": "AAAUSDT",
                    "status": "submitted",
                }
                for i in range(rows_per_writer)
            ]
        )
        write_dataset(batch, tmp_path, "event_demo_orders", partition_by=())

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        list(executor.map(write_batch, range(writer_count)))

    stored = read_dataset(tmp_path, "event_demo_orders")
    assert stored.height == writer_count * rows_per_writer
    unique_links = stored.select("order_link_id").unique().height
    assert unique_links == writer_count * rows_per_writer


def test_event_demo_orders_lock_serializes_concurrent_writers(tmp_path: Path) -> None:
    """No reader should ever observe a torn/partial event_demo_orders parquet:
    while writer A is replacing the file, writer B must either see the
    pre-write contents or block. Implemented via O_CREAT|O_EXCL lock plus
    temp-file rename — verify by reading the dataset between concurrent writes
    and checking row counts are always multiples of the batch size.
    """
    from concurrent.futures import ThreadPoolExecutor
    batch_size = 10

    def write_batch(writer_id: int) -> None:
        batch = pl.DataFrame(
            [
                {
                    "order_link_id": f"link-w{writer_id}-r{i}",
                    "ts_ms": 1_700_000_000_000 + writer_id * 1000 + i,
                    "symbol": "AAAUSDT",
                    "status": "submitted",
                }
                for i in range(batch_size)
            ]
        )
        write_dataset(batch, tmp_path, "event_demo_orders", partition_by=())

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(write_batch, w) for w in range(4)]
        for _ in range(20):
            stored = read_dataset(tmp_path, "event_demo_orders")
            if not stored.is_empty():
                assert stored.height % batch_size == 0
                assert stored.select("order_link_id").n_unique() == stored.height
            time.sleep(0.005)
        for future in futures:
            future.result()
