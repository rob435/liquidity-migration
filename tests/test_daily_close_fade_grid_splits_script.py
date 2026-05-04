from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_grid_splits as grid_splits


def test_grid_split_summary_prefers_stable_variants() -> None:
    frame = pl.DataFrame(
        [
            _variant("stable", "train", 0.03, 1.2),
            _variant("stable", "oos", 0.02, 1.0),
            _variant("fragile", "train", 0.20, 4.0),
            _variant("fragile", "oos", -0.05, -0.5),
        ]
    )

    summary = grid_splits.summarize_grid_splits(frame, expected_splits=2)

    assert summary.row(0, named=True)["score"] == "stable"
    assert summary.row(0, named=True)["all_splits_positive"] is True
    assert summary.row(1, named=True)["score"] == "fragile"
    assert summary.row(1, named=True)["all_splits_positive"] is False


def _variant(score: str, split: str, total_return: float, sharpe: float) -> dict:
    return {
        "score": score,
        "split": split,
        "signal_minute": 1335,
        "top_n": 5,
        "hold_minutes": 180,
        "stop_loss_pct": 0.20,
        "take_profit_pct": 0.0,
        "vol_trailing_stop_mult": 0.25,
        "mfe_giveback_pct": 0.20,
        "vwap_reversion_pct": 0.0,
        "cost_multiplier": 1.0,
        "round_trip_cost_bps": 12.0,
        "total_return": total_return,
        "sharpe_like": sharpe,
        "max_drawdown": -0.10,
        "trade_count": 20,
        "win_rate": 0.55,
    }
