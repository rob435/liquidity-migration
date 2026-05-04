from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_split_diagnostics as split_diagnostics


def test_parse_splits_requires_ordered_windows() -> None:
    assert split_diagnostics._parse_splits("train:2025-01-01:2025-02-01") == [
        ("train", "2025-01-01", "2025-02-01")
    ]


def test_split_summary_prefers_scenarios_that_survive_every_split() -> None:
    frame = pl.DataFrame(
        [
            _scenario("strong", "train", True, 0.0010, 0.60),
            _scenario("strong", "oos", True, 0.0008, 0.55),
            _scenario("fragile", "train", True, 0.0040, 0.80),
            _scenario("fragile", "oos", False, -0.0020, 0.30),
        ]
    )

    summary = split_diagnostics.summarize_split_scenarios(frame, expected_splits=2)

    assert summary.row(0, named=True)["score"] == "strong"
    assert summary.row(0, named=True)["all_splits_cost_pass"] is True
    assert summary.row(1, named=True)["score"] == "fragile"
    assert summary.row(1, named=True)["all_splits_cost_pass"] is False


def _scenario(score: str, split: str, cost_pass: bool, cost_return: float, cost_month_rate: float) -> dict:
    return {
        "score": score,
        "signal_minute": 1320,
        "entry_delay_minutes": 15,
        "horizon_minutes": 180,
        "top_n": 5,
        "split": split,
        "cost_edge_pass": cost_pass,
        "robust_direction_pass": True,
        "mean_basket_short_return": cost_return + 0.001,
        "mean_basket_cost_adjusted_short_return": cost_return,
        "cost_positive_month_rate": cost_month_rate,
        "worst_month_cost_adjusted_short_return": cost_return - 0.01,
        "mean_ic": 0.05,
        "ic_t_stat": 1.0,
        "baskets": 10,
        "obs": 50,
    }
