from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.long_native import _long_equity_chart_metrics


def test_long_equity_chart_metrics_match_official_chart_contract() -> None:
    equity = pl.DataFrame(
        {
            "date": ["2024-01-01", "2024-07-01", "2025-01-01"],
            "equity": [1.0, 1.1, 1.2],
        }
    )
    summary = {
        "total_return": 0.20,
        "max_drawdown": -0.10,
        "worst_day_return": -0.03,
        "sharpe_like": 1.5,
    }

    metrics = _long_equity_chart_metrics(summary, equity)

    assert metrics["total_return_pct"] == pytest.approx(20.0)
    assert metrics["max_drawdown_pct"] == pytest.approx(-10.0)
    assert metrics["worst_day_pct"] == pytest.approx(-3.0)
    assert metrics["sharpe_daily_ann"] == pytest.approx(1.5)
    assert metrics["annualized_pct"] == pytest.approx(((1.20 ** (1 / metrics["years"])) - 1.0) * 100.0)
    assert metrics["mar"] == pytest.approx((0.20 / metrics["years"]) / 0.10)
