from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_regime_sweep as regime_sweep


def test_prior_daily_ema_regime_uses_previous_completed_daily_close() -> None:
    klines = pl.DataFrame(
        [
            _bar("2026-01-01T23:59:00+00:00", 100.0),
            _bar("2026-01-02T23:59:00+00:00", 200.0),
        ],
        infer_schema_length=None,
    )

    regime = regime_sweep.build_prior_daily_ema_regime_from_klines(klines, ema_periods=(2,))

    jan_2 = regime.filter(pl.col("signal_date") == "2026-01-02").row(0, named=True)
    jan_3 = regime.filter(pl.col("signal_date") == "2026-01-03").row(0, named=True)
    assert jan_2["prior_close"] == 100.0
    assert jan_3["prior_close"] == 200.0
    assert "ema_distance_2" in regime.columns


def test_regime_selection_counts_inactive_calendar_days_as_zero_return() -> None:
    baskets = pl.DataFrame(
        [
            {"basket_id": "a", "date": "2026-01-02", "basket_return": 0.01, "trade_count": 5},
            {"basket_id": "b", "date": "2026-01-03", "basket_return": -0.02, "trade_count": 5},
            {"basket_id": "c", "date": "2026-01-04", "basket_return": 0.03, "trade_count": 5},
        ],
        infer_schema_length=None,
    )
    regime = pl.DataFrame(
        [
            {"signal_date": "2026-01-02", "prior_close": 100.0, "ema_distance_50": -0.01},
            {"signal_date": "2026-01-03", "prior_close": 103.0, "ema_distance_50": 0.03},
            {"signal_date": "2026-01-04", "prior_close": 97.0, "ema_distance_50": -0.06},
        ],
        infer_schema_length=None,
    )

    results = regime_sweep.evaluate_regime_sweep(
        baskets,
        regime,
        split_specs=[("test", "2026-01-02", "2026-01-05")],
        ema_periods=(50,),
        thresholds=(0.0,),
        baseline_rule="all",
    )

    below = results.filter((pl.col("rule") == "btc_ema_distance_lte") & (pl.col("threshold") == 0.0)).row(
        0,
        named=True,
    )
    assert below["selected_baskets"] == 2
    assert below["skipped_baskets"] == 1
    assert below["calendar_days"] == 3
    assert below["active_day_rate"] == 2 / 3
    assert below["trade_count"] == 10
    assert abs(below["total_return"] - ((1.01 * 1.0 * 1.03) - 1.0)) < 1e-12


def test_regime_stability_prefers_split_survival_over_big_single_window() -> None:
    results = pl.DataFrame(
        [
            _result("stable", 100, 0.02, "train", 0.03),
            _result("stable", 100, 0.02, "oos", 0.02),
            _result("fragile", 200, 0.05, "train", 0.20),
            _result("fragile", 200, 0.05, "oos", -0.04),
        ],
        infer_schema_length=None,
    )

    summary = regime_sweep.summarize_regime_stability(results, expected_splits=2)

    assert summary.row(0, named=True)["rule"] == "stable"
    assert summary.row(0, named=True)["all_splits_positive"] is True
    assert summary.row(1, named=True)["rule"] == "fragile"
    assert summary.row(1, named=True)["all_splits_positive"] is False


def _bar(ts: str, close: float) -> dict:
    return {
        "symbol": "BTCUSDT",
        "ts_ms": int(datetime.fromisoformat(ts).astimezone(UTC).timestamp() * 1000),
        "close": close,
    }


def _result(rule: str, period: int, threshold: float, split: str, total_return: float) -> dict:
    return {
        "split": split,
        "rule": rule,
        "ema_period": period,
        "threshold": threshold,
        "total_return": total_return,
        "calendar_sharpe_like": 1.0,
        "max_drawdown": -0.10,
        "active_day_rate": 0.50,
        "selected_baskets": 10,
        "trade_count": 50,
        "missing_regime_baskets": 0,
    }
