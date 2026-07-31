"""Tests for generation-bound strategy completion evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from liquidity_migration.strategy.strategy_cycle_health import (
    StrategyCycleHealth,
    read_strategy_cycle_health,
    strategy_cycle_health_path,
    write_strategy_cycle_health,
)


INVOCATION_ID = "12" * 16


def _health(**overrides: object) -> StrategyCycleHealth:
    values: dict[str, object] = {
        "sleeve": "continuous",
        "environment": "paper",
        "cycle_id": "continuous-target-test-1000",
        "cycle_ts_ms": 1_000,
        "completed_ts_ns": 2_000_000_000,
        "invocation_id": INVOCATION_ID,
        "ws_kline_store_rows": 383_711,
    }
    values.update(overrides)
    return StrategyCycleHealth(**values)  # type: ignore[arg-type]


def test_round_trip_is_private_atomic_and_replaces_latest(tmp_path: Path) -> None:
    path = write_strategy_cycle_health(tmp_path, _health())

    assert path == strategy_cycle_health_path(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    assert read_strategy_cycle_health(tmp_path) == _health()

    updated = _health(
        cycle_id="continuous-target-test-2000",
        cycle_ts_ms=2_000,
        completed_ts_ns=3_000_000_000,
        ws_kline_store_rows=400_000,
    )
    write_strategy_cycle_health(tmp_path, updated)

    assert read_strategy_cycle_health(tmp_path) == updated
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_reader_rejects_noncanonical_and_hardlinked_artifacts(tmp_path: Path) -> None:
    path = write_strategy_cycle_health(tmp_path, _health())
    path.write_text('{"schema_version": 1}\n', encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(ValueError, match="missing fields|not canonical"):
        read_strategy_cycle_health(tmp_path)

    write_strategy_cycle_health(tmp_path, _health())
    os.link(path, tmp_path / "second-link.json")
    with pytest.raises(ValueError, match="hard-linked"):
        read_strategy_cycle_health(tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"invocation_id": "0" * 32}, "zero identifier"),
        ({"cycle_ts_ms": True}, "positive integer"),
        ({"completed_ts_ns": 0}, "positive integer"),
        ({"completed_ts_ns": 999_999_999}, "cannot predate"),
        ({"ws_kline_store_rows": -1}, "non-negative integer"),
        ({"environment": "live"}, "environment"),
        ({"schema_version": True}, "unsupported"),
    ],
)
def test_schema_rejects_unbound_or_malformed_evidence(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _health(**overrides)
