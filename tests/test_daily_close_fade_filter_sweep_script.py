from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_filter_sweep as filter_sweep


def test_filter_sweep_uses_zero_return_for_skipped_days() -> None:
    day_rows = pl.DataFrame(
        [
            _day("a", "2026-01-01", 0.10, 0.02),
            _day("b", "2026-01-02", 0.02, -0.03),
            _day("c", "2026-01-03", 0.12, 0.04),
        ],
        infer_schema_length=None,
    )
    spec = filter_sweep.ContextFilterSpec(selected_excess_vs_market_min=0.08)

    results = filter_sweep.evaluate_filter_sweep(
        day_rows,
        specs=[spec],
        split_specs=[("test", "2026-01-01", "2026-01-04")],
    )

    row = results.filter(pl.col("label") == spec.label).row(0, named=True)
    assert row["selected_days"] == 2
    assert row["skipped_days"] == 1
    assert abs(row["total_return"] - ((1.02 * 1.0 * 1.04) - 1.0)) < 1e-12
    assert row["active_rate"] == 2 / 3


def test_filter_summary_prefers_stable_positive_splits() -> None:
    results = pl.DataFrame(
        [
            _result("stable", "train", 0.05, 0.01, 80),
            _result("stable", "oos", 0.04, 0.01, 80),
            _result("fragile", "train", 0.30, 0.20, 80),
            _result("fragile", "oos", -0.02, -0.05, 80),
        ],
        infer_schema_length=None,
    )

    summary = filter_sweep.summarize_filter_sweep(
        results,
        expected_splits=2,
        min_active_days=40,
        min_active_rate=0.20,
    )

    assert summary.row(0, named=True)["label"] == "stable"
    assert summary.row(0, named=True)["promotion_candidate"] is True
    assert summary.filter(pl.col("label") == "fragile").row(0, named=True)["all_splits_positive"] is False


def _day(basket_id: str, date: str, excess: float, basket_return: float) -> dict:
    return {
        "basket_id": basket_id,
        "date": date,
        "basket_return": basket_return,
        "selected_excess_vs_market": excess,
        "selected_avg_vwap_extension": 0.05,
        "selected_avg_late_volume_ratio": 1.5,
        "market_positive_rate": 0.5,
        "btc_day_return": 0.0,
        "trade_count": 3,
    }


def _result(label: str, split: str, total_return: float, delta: float, selected_days: int) -> dict:
    return {
        "split": split,
        "label": label,
        "selected_excess_vs_market_min": 0.0,
        "selected_avg_vwap_extension_min": 0.0,
        "selected_avg_late_volume_ratio_min": 0.0,
        "market_positive_rate_max": 1.0,
        "btc_day_return_max": 99.0,
        "min_trade_count": 1,
        "total_return": total_return,
        "return_delta_vs_baseline": delta,
        "calendar_sharpe_like": 1.0,
        "max_drawdown": -0.05,
        "active_rate": 0.5,
        "selected_days": selected_days,
        "trades": 100,
    }
