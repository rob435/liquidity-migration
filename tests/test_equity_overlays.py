from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.volume_events_charts import (
    _monthly_return_color,
    _write_equity_benchmark_chart,
)


def test_monthly_table_rows_fills_gap_months() -> None:
    """A month where the book sat flat must appear as a +0.00% row, not vanish -- a
    missing month is indistinguishable from a rendering bug.
    """
    from liquidity_migration.volume_events_charts import _monthly_table_rows

    equity = pl.DataFrame({
        "date": ["2025-01-10", "2025-01-20", "2025-03-05"],
        "basket_return": [0.01, 0.02, -0.01],
        "equity": [1.01, 1.03, 1.02],
    })
    rows = _monthly_table_rows(equity=equity, monthly=None)
    assert [r["month"] for r in rows] == ["2025-01", "2025-02", "2025-03"]
    gap = rows[1]
    assert gap["return"] == 0.0
    assert gap["count"] == 0


def test_monthly_table_rows_real_monthly_counts_trades_and_fills_gaps() -> None:
    from liquidity_migration.volume_events_charts import _monthly_table_rows

    monthly = pl.DataFrame({
        "month": ["2025-01", "2025-03"],
        "strategy_return": [0.05, -0.02],
        "trades": [7, 3],
    })
    rows = _monthly_table_rows(equity=pl.DataFrame(), monthly=monthly)
    assert [r["month"] for r in rows] == ["2025-01", "2025-02", "2025-03"]
    assert rows[0]["count"] == 7
    assert rows[1]["count"] == 0


def test_chart_renders_metric_tiles(tmp_path: Path) -> None:
    pil_image = pytest.importorskip("PIL.Image")
    days = ["2021-01-01", "2021-01-02", "2021-01-03"]
    ts = [1609459200000, 1609545600000, 1609632000000]
    equity = pl.DataFrame({"ts_ms": ts, "date": days, "equity": [1.0, 1.1, 1.2]})
    btc = pl.DataFrame(
        {"symbol": ["BTCUSDT"] * 3, "date": days, "ts_ms": ts, "close": [30000.0, 31000.0, 33000.0]}
    )

    meta = _write_equity_benchmark_chart(
        tmp_path,
        equity=equity,
        raw_klines=btc,
        png_name="metrics_equity_btc.png",
        metrics={
            "total_return_pct": 20.0,
            "annualized_pct": 12.3,
            "max_drawdown_pct": -4.5,
            "worst_day_pct": -2.0,
            "sharpe_daily_ann": 1.8,
            "mar": 2.7,
            "final_equity": 1.2,
            "years": 1.0,
        },
    )

    path = tmp_path / "metrics_equity_btc.png"
    assert meta["metric_tiles"] == 7
    assert meta["legend_items"] == 2
    assert path.exists()
    with pil_image.open(path) as image:
        assert image.size[1] > 940


def test_monthly_return_color_treats_display_zero_as_neutral() -> None:
    assert _monthly_return_color(0.0) == (100, 116, 139, 255)
    assert _monthly_return_color(0.000049) == (100, 116, 139, 255)
    assert _monthly_return_color(0.000051) == (22, 101, 52, 255)
    assert _monthly_return_color(-0.000051) == (185, 28, 28, 255)
