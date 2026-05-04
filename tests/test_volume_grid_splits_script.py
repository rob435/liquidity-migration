from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_volume_grid_splits as volume_splits


def test_parse_splits_requires_ordered_windows() -> None:
    assert volume_splits._parse_splits("oos:2025-01-01:2025-02-01") == [
        ("oos", "2025-01-01", "2025-02-01")
    ]
    with pytest.raises(ValueError, match="Split end"):
        volume_splits._parse_splits("bad:2025-02-01:2025-01-01")


def test_volume_grid_split_summary_prefers_stable_variants() -> None:
    frame = pl.DataFrame(
        [
            _variant("stable", "train", 0.08, 1.2),
            _variant("stable", "oos", 0.05, 0.9),
            _variant("fragile", "train", 0.40, 3.0),
            _variant("fragile", "oos", -0.10, -0.5),
        ]
    )

    summary = volume_splits.summarize_volume_grid_splits(frame, expected_splits=2)

    assert summary.row(0, named=True)["score"] == "stable"
    assert summary.row(0, named=True)["all_splits_positive"] is True
    assert summary.row(1, named=True)["score"] == "fragile"
    assert summary.row(1, named=True)["all_splits_positive"] is False


def _variant(score: str, split: str, total_return: float, sharpe: float) -> dict:
    return {
        "score": score,
        "split": split,
        "quantile": 0.20,
        "hold_days": 7,
        "rebalance_days": 7,
        "gross_exposure": 1.0,
        "entry_delay_hours": 1,
        "stop_mode": "none",
        "stop_loss_pct": 0.0,
        "vol_stop_multiplier": 3.0,
        "vol_stop_lookback_days": 20,
        "min_stop_loss_pct": 0.0,
        "max_stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
        "min_symbols": 4,
        "cost_multiplier": 1.0,
        "side_mode": "short_high_long_low",
        "rank_exit_enabled": False,
        "rank_exit_threshold": 0.50,
        "universe_rank_min": 81,
        "universe_rank_max": 160,
        "universe_min_daily_turnover": 0.0,
        "include_symbols": "",
        "exclude_symbols": "",
        "total_return": total_return,
        "sharpe_like": sharpe,
        "max_drawdown": -0.20,
        "trades": 100,
        "trade_win_rate": 0.55,
        "long_return": 0.02,
        "short_return": 0.05,
        "cost_return": -0.01,
    }
