from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate_volume_promotion as promotion


def test_volume_promotion_requires_split_survival_and_drawdown() -> None:
    summary = pl.DataFrame(
        [
            _candidate("stable", positive_splits=3, min_return=0.04, worst_drawdown=-0.20, sharpe=1.2),
            _candidate("fragile", positive_splits=2, min_return=-0.03, worst_drawdown=-0.15, sharpe=1.0),
            _candidate("drawdown", positive_splits=3, min_return=0.05, worst_drawdown=-0.60, sharpe=1.5),
        ]
    )

    table = promotion.build_volume_promotion_table(
        summary,
        max_worst_drawdown=-0.35,
        min_avg_sharpe=0.5,
    )

    stable = table.filter(pl.col("score") == "stable").row(0, named=True)
    fragile = table.filter(pl.col("score") == "fragile").row(0, named=True)
    drawdown = table.filter(pl.col("score") == "drawdown").row(0, named=True)

    assert stable["promotion_gate_pass"] is True
    assert stable["promotion_reason"] == "pass"
    assert fragile["promotion_gate_pass"] is False
    assert fragile["promotion_reason"] == "positive_split_fail,worst_split_return_fail,stability_fail"
    assert drawdown["promotion_gate_pass"] is False
    assert drawdown["promotion_reason"] == "drawdown_fail"

    report = promotion.format_volume_promotion_report(
        table,
        {
            "split_summary": "summary.csv",
            "require_complete_splits": True,
            "min_positive_splits": 0,
            "min_worst_split_return": 0.0,
            "min_stability_score": 0.0,
            "max_worst_drawdown": -0.35,
            "min_avg_sharpe": 0.5,
            "rows": table.height,
            "promotable_rows": 1,
        },
    )
    assert "| Rank | Score |" in report
    assert "| 1 | stable | True | pass |" in report


def _candidate(
    score: str,
    *,
    positive_splits: int,
    min_return: float,
    worst_drawdown: float,
    sharpe: float,
) -> dict:
    return {
        "score": score,
        "splits_seen": 3,
        "complete_splits": True,
        "positive_return_splits": positive_splits,
        "min_total_return": min_return,
        "avg_total_return": max(min_return, 0.06),
        "max_total_return": 0.20,
        "total_return_std": 0.02,
        "stability_score": min_return,
        "worst_max_drawdown": worst_drawdown,
        "avg_sharpe_like": sharpe,
        "universe_rank_min": 81,
        "universe_rank_max": 160,
        "hold_days": 7,
        "quantile": 0.20,
        "stop_mode": "none",
        "rank_exit_enabled": False,
        "side_mode": "short_high_long_low",
        "cost_multiplier": 1.0,
        "trade_count": 100,
    }
