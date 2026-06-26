from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "continuous_daily_rebalance_ab.py"
    spec = importlib.util.spec_from_file_location("_continuous_daily_rebalance_ab", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_arm_specs_pin_control_and_primary_on() -> None:
    mod = _load_module()
    arms = mod.arm_specs()

    assert arms[mod.CONTROL].enabled is False
    assert arms[mod.PRIMARY_ON].enabled is True
    assert arms[mod.PRIMARY_ON].target_daily_vol == pytest.approx(0.045)
    assert arms[mod.PRIMARY_ON].max_scale == pytest.approx(4.0)
    assert arms[mod.PRIMARY_ON].drawdown_half_threshold == pytest.approx(-0.04)


def test_summary_metrics_gap_fills_calendar_days() -> None:
    mod = _load_module()
    d = mod.MS_PER_DAY
    df = pl.DataFrame(
        {
            "ts_ms": [0, 2 * d],
            "basket_return": [0.10, -0.10],
            "scale": [1.0, 1.0],
            "resize_cost_return": [0.0, 0.0],
            "hedge_cost_return": [0.0, 0.0],
        }
    )

    metrics = mod.summary_metrics(df)

    assert metrics["calendar_days"] == 3
    assert metrics["ledger_days"] == 2
    assert metrics["total_return"] == pytest.approx(-0.01)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)


def test_primary_acceptance_rejects_single_venue_mar_failure() -> None:
    mod = _load_module()
    summaries = [
        {"venue": "bybit", "arm": mod.CONTROL, "mar": 1.0, "total_return": 0.10},
        {"venue": "bybit", "arm": mod.PRIMARY_ON, "mar": 1.05, "total_return": 0.12},
    ]
    comparisons = [
        {
            "venue": "bybit",
            "arm": mod.PRIMARY_ON,
            "total_return_delta": 0.02,
            "max_drawdown_delta": 0.01,
            "worst_90d_delta": 0.01,
            "lomo_flips_positive_edge": False,
        }
    ]

    verdict, reasons = mod.primary_acceptance(comparisons, summaries)

    assert verdict == "REJECT_KEEP_DAILY_REBALANCE_DISABLED"
    assert any("MAR" in reason for reason in reasons)
