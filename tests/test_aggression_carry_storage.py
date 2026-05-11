from __future__ import annotations

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


def test_demo_fast_protection_state_and_events_dedupe_by_keys(tmp_path: Path) -> None:
    state = pl.DataFrame(
        [{"paper_trade_id": "paper-1", "symbol": "BTCUSDT", "ts_ms": 1_700_000_000_000, "best_price": 99.0}]
    )
    event = pl.DataFrame(
        [{"event_id": "event-1", "paper_trade_id": "paper-1", "symbol": "BTCUSDT", "decision": "observed"}]
    )

    write_dataset(state, tmp_path, "demo_fast_protection_state")
    write_dataset(state.with_columns(pl.lit(98.0).alias("best_price")), tmp_path, "demo_fast_protection_state")
    write_dataset(event, tmp_path, "demo_fast_protection_events")
    write_dataset(event.with_columns(pl.lit("triggered").alias("decision")), tmp_path, "demo_fast_protection_events")

    stored_state = read_dataset(tmp_path, "demo_fast_protection_state")
    stored_events = read_dataset(tmp_path, "demo_fast_protection_events")

    assert stored_state.height == 1
    assert stored_state["best_price"][0] == 98.0
    assert stored_events.height == 1
    assert stored_events["decision"][0] == "triggered"


def test_read_dataset_handles_schema_evolution_across_partitions(tmp_path: Path) -> None:
    first = pl.DataFrame(
        [
            {
                "event_id": "event-1",
                "paper_trade_id": "paper-1",
                "symbol": "BTCUSDT",
                "date": "2026-01-15",
                "decision": "observe_only",
            }
        ]
    )
    second = pl.DataFrame(
        [
            {
                "event_id": "event-2",
                "paper_trade_id": "paper-2",
                "symbol": "ETHUSDT",
                "date": "2026-01-16",
                "decision": "accepted",
                "trigger_price": 99.5,
            }
        ]
    )

    write_dataset(first, tmp_path, "demo_fast_protection_events")
    write_dataset(second, tmp_path, "demo_fast_protection_events")

    stored = read_dataset(tmp_path, "demo_fast_protection_events")

    assert stored.height == 2
    assert "trigger_price" in stored.columns
    assert stored.filter(pl.col("event_id") == "event-1").row(0, named=True)["trigger_price"] is None
    assert stored.filter(pl.col("event_id") == "event-2").row(0, named=True)["trigger_price"] == 99.5


def test_exclusive_file_lock_cleans_up_lock_file(tmp_path: Path) -> None:
    lock_path = dataset_lock_path(tmp_path, "demo_execution_orders")

    with exclusive_file_lock(lock_path, poll_seconds=0.0):
        assert lock_path.exists()

    assert not lock_path.exists()
