from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate_close_fade_promotion as promotion


def test_promotion_requires_raw_and_exit_split_survival() -> None:
    diagnostics = pl.DataFrame(
        [
            _diagnostic("stable", cost_pass_splits=2, all_cost_pass=True),
            _diagnostic("exit_only", cost_pass_splits=1, all_cost_pass=False),
        ]
    )
    grid = pl.DataFrame(
        [
            _grid("stable", positive_splits=2, min_return=0.02),
            _grid("exit_only", positive_splits=2, min_return=0.05),
            _grid("raw_missing", positive_splits=2, min_return=0.10),
            _grid("exit_fail", positive_splits=1, min_return=-0.01),
        ]
    )

    table = promotion.build_promotion_table(diagnostics, grid)

    stable = table.filter(pl.col("score") == "stable").row(0, named=True)
    exit_only = table.filter(pl.col("score") == "exit_only").row(0, named=True)
    raw_missing = table.filter(pl.col("score") == "raw_missing").row(0, named=True)
    exit_fail = table.filter(pl.col("score") == "exit_fail").row(0, named=True)

    assert stable["promotion_gate_pass"] is True
    assert exit_only["promotion_gate_pass"] is False
    assert exit_only["promotion_reason"] == "raw_cost_split_fail"
    assert raw_missing["promotion_reason"] == "no_matching_raw_diagnostic"
    assert exit_fail["promotion_reason"] == "no_matching_raw_diagnostic,grid_positive_split_fail"


def _diagnostic(score: str, *, cost_pass_splits: int, all_cost_pass: bool) -> dict:
    return {
        "score": score,
        "signal_minute": 1320,
        "entry_delay_minutes": 1,
        "horizon_minutes": 180,
        "top_n": 5,
        "splits_seen": 2,
        "cost_pass_splits": cost_pass_splits,
        "all_splits_cost_pass": all_cost_pass,
        "avg_cost_adjusted_short_return": 0.01,
        "min_cost_adjusted_short_return": 0.005,
        "avg_ic_t_stat": 1.5,
    }


def _grid(score: str, *, positive_splits: int, min_return: float) -> dict:
    return {
        "score": score,
        "signal_minute": 1320,
        "entry_delay_minutes": 1,
        "hold_minutes": 180,
        "top_n": 5,
        "splits_seen": 2,
        "positive_return_splits": positive_splits,
        "all_splits_positive": positive_splits == 2,
        "min_total_return": min_return,
        "avg_total_return": max(min_return, 0.02),
        "stability_score": min_return,
        "sharpe_like": 1.0,
        "avg_sharpe_like": 1.0,
        "max_drawdown": -0.10,
        "trade_count": 20,
        "win_rate": 0.55,
        "stop_loss_pct": 0.20,
        "take_profit_pct": 0.0,
        "vol_trailing_stop_mult": 0.25,
        "mfe_giveback_pct": 0.20,
        "cost_multiplier": 1.0,
    }
