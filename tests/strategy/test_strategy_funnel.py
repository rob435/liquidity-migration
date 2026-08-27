from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

import polars as pl
import pytest

from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.rules.long_native import long_v11a_profile
from liquidity_migration.strategy.long_native_event_demo import (
    _select_long_entry_candidates,
)
from liquidity_migration.strategy.strategy_funnel import (
    FunnelJsonlWriter,
    finalize_funnel_row,
)


class _Collector:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def observe(self, row: Mapping[str, Any]) -> None:
        self.rows.append(dict(row))


class _RaisingObserver:
    def observe(self, row: Mapping[str, Any]) -> None:
        del row
        raise OSError("diagnostic disk unavailable")


def _long_features(signal_ts_ms: int) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "ts_ms": signal_ts_ms,
                "symbol": "ABCUSDT",
                "close": 100.0,
                "in_universe": True,
                "regime_on": True,
                "eth_regime_on": True,
                "today_volume_rank": 2,
                "universe_rank": 3,
                "log_return": math.log1p(0.20),
                "close_location": 0.85,
                "atr_14d_pct": 0.05,
                "sigma_daily_30d": 0.05,
                "pump_3d_log": 0.10,
                "pump_7d_log": 0.20,
                "close_loc_3d": 0.70,
                "close_loc_7d": 0.70,
                "realized_vol": 0.60,
                "symbol_age_days": 120,
                "turnover_quote": 8_000_000.0,
                "turnover_median_90d": 2_000_000.0,
                "btc_rv_30": 0.7,
            }
        ]
    )


def test_long_writer_on_off_preserves_candidates_order_and_numbers() -> None:
    strategy = long_v11a_profile()
    signal_ts_ms = 1_700_000_000_000
    now_ms = signal_ts_ms + 2 * MS_PER_HOUR
    kwargs = {
        "features": _long_features(signal_ts_ms),
        "all_trades": pl.DataFrame(),
        "now_ms": now_ms,
        "strategy": strategy,
        "price_by_symbol": {"ABCUSDT": 98.5},
        "max_new_entries": 5,
    }

    candidates_off, skips_off = _select_long_entry_candidates(**kwargs)
    collector = _Collector()
    candidates_on, skips_on = _select_long_entry_candidates(
        **kwargs,
        funnel_observer=collector,
    )

    assert candidates_on == candidates_off
    assert skips_on == skips_off
    assert [row["source_key"] for row in collector.rows] == [
        f"long:bybit:ABCUSDT:{signal_ts_ms}"
    ]
    assert collector.rows[0]["gate_pump_trigger"] == "pass"
    assert collector.rows[0]["gate_entry_anchor"] == "pass"


def test_long_writer_failure_cannot_suppress_or_mutate_target_candidate() -> None:
    strategy = long_v11a_profile()
    signal_ts_ms = 1_700_000_000_000
    now_ms = signal_ts_ms + 2 * MS_PER_HOUR
    kwargs = {
        "features": _long_features(signal_ts_ms),
        "all_trades": pl.DataFrame(),
        "now_ms": now_ms,
        "strategy": strategy,
        "price_by_symbol": {"ABCUSDT": 98.5},
        "max_new_entries": 5,
    }

    expected = _select_long_entry_candidates(**kwargs)
    actual = _select_long_entry_candidates(**kwargs, funnel_observer=_RaisingObserver())

    assert actual == expected


def _writer_row(*, liquidity: str, evaluation_ts_ms: int) -> dict[str, Any]:
    return finalize_funnel_row(
        {
            "sleeve": "long",
            "venue": "bybit",
            "symbol": "ABCUSDT",
            "signal_ts_ms": 1_700_000_000_000,
            "feature_ts_ms": 1_700_000_000_000,
            "data_available_ts_ms": 1_700_000_000_000,
            "decision_ts_ms": evaluation_ts_ms,
            "entry_ts_ms": None,
            "evaluation_ts_ms": evaluation_ts_ms,
            "source_strength": 1.2,
            "gate_pit_tradable": "pass",
            "gate_liquidity_floor": liquidity,
        },
        required_gate_order=("pit_tradable", "liquidity_floor"),
    )


def test_jsonl_writer_suppresses_unchanged_evaluations_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "funnel.jsonl"
    writer = FunnelJsonlWriter(path)
    first = _writer_row(liquidity="fail", evaluation_ts_ms=1_700_000_000_100)
    writer.observe(first)
    writer.observe(first)

    second = _writer_row(liquidity="pass", evaluation_ts_ms=1_700_000_000_200)
    writer.observe(second)
    resumed = FunnelJsonlWriter(path)
    resumed.observe(second)

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == ["source", "transition"]
    assert events[0]["source_key"] == events[1]["source_key"]
    assert events[0]["gate_state_sha256"] != events[1]["gate_state_sha256"]


def test_required_not_applicable_gate_is_not_accepted() -> None:
    row = finalize_funnel_row(
        {
            "sleeve": "long",
            "venue": "bybit",
            "symbol": "ABCUSDT",
            "signal_ts_ms": 1_700_000_000_000,
            "gate_pit_tradable": "not_applicable",
            "gate_source_decile_9": "pass",
        },
        required_gate_order=("pit_tradable", "source_decile_9"),
    )

    assert row["first_rejection"] is None
    assert row["barebones_accepted"] is False


def test_jsonl_writer_rejects_transition_without_source(tmp_path: Path) -> None:
    path = tmp_path / "funnel.jsonl"
    row = _writer_row(liquidity="pass", evaluation_ts_ms=1_700_000_000_200)
    event = {
        "schema_version": 1,
        "event_type": "transition",
        "source_key": row["source_key"],
        "source_sha256": "a" * 64,
        "gate_state_sha256": row["gate_state_sha256"],
        "evaluation_ts_ms": row["evaluation_ts_ms"],
        "row": row,
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="transition precedes source"):
        FunnelJsonlWriter(path)
