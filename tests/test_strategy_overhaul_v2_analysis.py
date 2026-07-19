from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

import scripts.analyze_strategy_overhaul_v2 as analysis_module
from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.config import CostConfig
from scripts.analyze_strategy_overhaul_v2 import (
    CAPITAL_USD,
    ContinuousEventConfig,
    _account_sample,
    _candidate_scores,
    _path_estimate,
    _quartile_contrast,
    _replay_account,
    _simulate_portfolio,
)


def test_path_estimate_balances_waves_then_dates() -> None:
    frame = pl.DataFrame(
        {
            "signal_date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "decision_ts_ms": [1, 1, 2],
            "return_24h": [0.10, 0.30, -0.20],
        }
    )

    result = _path_estimate(frame, "return_24h")

    assert result["finite_sources"] == 3
    assert result["waves"] == 2
    assert result["dates"] == 2
    assert abs(float(result["date_wave_mean"])) < 1e-12


def test_quartile_contrast_uses_frozen_high_minus_low_direction() -> None:
    rows = []
    for index in range(40):
        day = dt.date(2022, 1, 1) + dt.timedelta(days=index * 40)
        rows.append(
            {
                "signal_date": day.isoformat(),
                "decision_ts_ms": index + 1,
                "feature": float(index),
                "return_24h": index / 10_000.0,
                "return_72h": index / 5_000.0,
            }
        )

    result = _quartile_contrast(
        pl.from_dicts(rows),
        family="fixture",
        field="feature",
    )

    assert result["status"] == "estimated"
    assert result["return_24h"]["effect_high_minus_low"] > 0.0
    assert result["q75"] > result["q25"]


def test_account_sample_is_bounded_and_key_only() -> None:
    frame = pl.from_dicts(
        [
            {"source_key": f"source-{index}", "sleeve": sleeve, "return_24h": float(index)}
            for sleeve in ("long", "continuous")
            for index in range(120)
        ]
    )
    changed = frame.with_columns((pl.col("return_24h") * -1000.0).alias("return_24h"))

    selected = _account_sample(frame)
    changed_selected = _account_sample(changed)

    assert selected.group_by("sleeve").len()["len"].to_list() == [100, 100]
    assert set(selected["source_key"]) == set(changed_selected["source_key"])


def test_candidate_below_cost_cannot_qualify_on_other_scores() -> None:
    support = {"sources": 500, "waves": 200, "dates": 150}
    contrast = {
        "status": "estimated",
        "family": "signal_strength",
        "field": "source_strength",
        "return_24h": {
            "effect_high_minus_low": -0.0035,
            "block_ci_95": [-0.01, 0.002],
            "low_support": support,
            "high_support": support,
        },
        "early_return_24h": {"effect_high_minus_low": -0.0035},
        "late_return_24h": {"effect_high_minus_low": -0.0035},
    }
    scores = _candidate_scores(
        {"long": {"contrasts": [contrast]}, "continuous": {"contrasts": []}},
        long_cost_return=0.0045,
        continuous_cost_return=0.002,
    )

    candidate = scores["long"]["considered"][0]
    assert candidate["total_score"] >= 6
    assert candidate["economic_score"] == 0
    assert candidate["eligible"] is False
    assert scores["long"]["selected_mechanical_candidate"] is None


def _write_hourly_fixture(root: Path, start: dt.datetime, hours: int) -> None:
    by_day: dict[str, list[dict[str, object]]] = {}
    for offset in range(hours):
        timestamp = start + dt.timedelta(hours=offset)
        close = 100.0
        row = {
            "ts_ms": int(timestamp.timestamp() * 1000),
            "symbol": "TESTUSDT",
            "open": close,
            "high": 100.1,
            "low": 99.9,
            "close": close,
        }
        by_day.setdefault(timestamp.date().isoformat(), []).append(row)
    for day, rows in by_day.items():
        partition = root / "klines_1h" / f"date={day}" / "symbol=TESTUSDT"
        partition.mkdir(parents=True)
        pl.from_dicts(rows).write_parquet(partition / "part.parquet")


def test_barebones_portfolio_uses_fixed_lifecycle_and_separate_sleeves(tmp_path: Path) -> None:
    start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    _write_hourly_fixture(tmp_path, start, 100)
    entry_ts_ms = int(start.timestamp() * 1000) + MS_PER_HOUR
    funnel = pl.from_dicts(
        [
            {
                "source_key": "long-source",
                "sleeve": "long",
                "symbol": "TESTUSDT",
                "signal_ts_ms": int(start.timestamp() * 1000),
                "entry_ts_ms": entry_ts_ms,
                "entry_price": 100.0,
                "source_strength": 2.0,
                "atr_14d_pct": 0.01,
                "turnover_quote": None,
                "barebones_accepted": True,
            },
            {
                "source_key": "continuous-source",
                "sleeve": "continuous",
                "symbol": "TESTUSDT",
                "signal_ts_ms": int(start.timestamp() * 1000) - MS_PER_HOUR,
                "entry_ts_ms": entry_ts_ms,
                "entry_price": 100.0,
                "source_strength": 1.0,
                "atr_14d_pct": None,
                "turnover_quote": 1_000_000.0,
                "barebones_accepted": True,
            },
        ]
    )
    continuous = ContinuousEventConfig(
        gross_exposure=0.25,
        max_active=25,
        sizing_mode="flat",
        btc_trend_gate="off",
        age_days_min=0,
        entry_crowding_max_fresh=0,
    )

    trades, _daily, stats, files = _simulate_portfolio(
        funnel,
        root=tmp_path,
        long_costs=CostConfig(),
        continuous_config=continuous,
    )

    assert len(trades) == 2
    assert {row["sleeve"] for row in trades} == {"long", "continuous"}
    assert stats["long"]["admitted"] == 1
    assert stats["continuous"]["admitted"] == 1
    assert all(row["exit_reason"] == "max_hold" for row in trades)
    assert len(files) == 5


def test_portable_account_replay_reconciles_and_finishes_flat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start_ms = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    ledger = pl.from_dicts(
        [
            {
                "source_key": "long-source",
                "sleeve": "long",
                "symbol": "LONGUSDT",
                "entry_ts_ms": start_ms,
                "exit_ts_ms": start_ms + MS_PER_HOUR,
                "entry_price": 100.0,
                "exit_price": 101.0,
                "exit_reason": "max_hold",
            },
            {
                "source_key": "continuous-source",
                "sleeve": "continuous",
                "symbol": "SHORTUSDT",
                "entry_ts_ms": start_ms,
                "exit_ts_ms": start_ms + MS_PER_HOUR,
                "entry_price": 100.0,
                "exit_price": 99.0,
                "exit_reason": "max_hold",
            },
        ]
    )

    monkeypatch.setattr("scripts.analyze_strategy_overhaul_v2.os.fsync", lambda _fd: pytest.fail("unexpected fsync"))
    originals = (
        analysis_module.account_kernel_module.exclusive_file_lock,
        analysis_module.account_kernel_module._atomic_replace,
        analysis_module.account_kernel_module._write_transaction,
        analysis_module.account_kernel_module._append_jsonl_projection,
        analysis_module.replay_module.JsonlStrategyEventTape,
    )
    receipt = _replay_account(
        ledger,
        work_root=tmp_path / "account-work",
        long_costs=CostConfig(),
    )
    assert (
        analysis_module.account_kernel_module.exclusive_file_lock,
        analysis_module.account_kernel_module._atomic_replace,
        analysis_module.account_kernel_module._write_transaction,
        analysis_module.account_kernel_module._append_jsonl_projection,
        analysis_module.replay_module.JsonlStrategyEventTape,
    ) == originals

    assert receipt["long"]["final_flat"] is True
    assert receipt["continuous"]["final_flat"] is True
    assert receipt["long"]["expected_fills"] == 2
    assert receipt["continuous"]["expected_fills"] == 2
    assert receipt["long"]["events"] > 0
    assert receipt["long"]["transactions"] > 0
    assert receipt["long"]["original_kernel_transactions"] >= 2
    assert receipt["long"]["compact_authoritative_segments"] == 1
    assert len(receipt["long"]["original_transaction_boundaries_sha256"]) == 64
    assert (tmp_path / "account-work" / "account-long" / "account_journal" / "events.jsonl").is_file()
    assert CAPITAL_USD == 1_000_000.0
