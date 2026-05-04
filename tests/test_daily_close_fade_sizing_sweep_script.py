from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_sizing_sweep as sizing_sweep
from aggression_carry.config import DailyCloseFadeConfig


def test_capped_proportional_weights_redistribute_until_cap() -> None:
    weights = sizing_sweep.capped_proportional_weights([9.0, 1.0, 1.0], gross_exposure=1.0, max_weight=0.40)

    assert weights[0] == 0.40
    assert abs(weights[1] - 0.30) < 1e-12
    assert abs(weights[2] - 0.30) < 1e-12
    assert abs(sum(weights) - 1.0) < 1e-12


def test_capped_proportional_weights_leaves_cash_when_all_names_hit_cap() -> None:
    weights = sizing_sweep.capped_proportional_weights([1.0, 1.0], gross_exposure=1.0, max_weight=0.30)

    assert weights == [0.30, 0.30]
    assert abs(sum(weights) - 0.60) < 1e-12


def test_build_weighted_baskets_applies_equal_concentration_cap() -> None:
    trades = pl.DataFrame([_trade("a", 1.0, 0.10), _trade("a", 2.0, 0.05)], infer_schema_length=None)

    baskets = sizing_sweep.build_weighted_baskets(
        trades,
        config=DailyCloseFadeConfig(top_n=5, gross_exposure=1.0),
        sizing=sizing_sweep.SizingSpec("capped_equal", max_weight=0.35),
    )

    row = baskets.row(0, named=True)
    assert row["trade_count"] == 2
    assert abs(row["basket_gross_exposure"] - 0.70) < 1e-12
    assert abs(row["basket_return"] - ((0.10 + 0.05) * 0.35)) < 1e-12
    assert abs(row["max_symbol_weight"] - 0.35) < 1e-12


def _trade(basket_id: str, score: float, net_return: float) -> dict:
    return {
        "basket_id": basket_id,
        "signal_ts_ms": 1_000,
        "date": "2026-01-01",
        "signal_minute": 1320,
        "entry_rank": 1,
        "score": score,
        "gross_return": net_return + 0.001,
        "cost_return": 0.001,
        "net_return": net_return,
        "mae": -0.01,
        "mfe": 0.02,
    }
