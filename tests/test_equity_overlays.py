from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.volume_events_charts import (
    _OVERLAY_PALETTE,
    _monthly_return_color,
    OverlaySpec,
    _overlay_series_dicts,
    _write_equity_benchmark_chart,
    price_overlay_from_csv,
)


def _write_csv(path: Path, rows: dict) -> Path:
    pl.DataFrame(rows).write_csv(path)
    return path


def test_price_overlay_from_csv_normalises_and_clips(tmp_path: Path) -> None:
    csv = _write_csv(
        tmp_path / "mu.csv",
        {"date": ["2021-01-01", "2021-01-02", "2021-01-03", "2021-01-04"],
         "close": [50.0, 55.0, 60.0, 70.0]},
    )
    ov = price_overlay_from_csv(csv, name="Micron (MU)", start="2021-01-02", end="2021-01-03")
    assert ov.name == "Micron (MU)"
    # clipped to [start,end] and normalised to $1 at the first in-window point (55.0)
    assert [p["date"] for p in ov.points] == ["2021-01-02", "2021-01-03"]
    assert ov.points[0]["value"] == pytest.approx(1.0)
    assert ov.points[1]["value"] == pytest.approx(60.0 / 55.0)


def test_price_overlay_autodetects_value_column(tmp_path: Path) -> None:
    csv = _write_csv(tmp_path / "x.csv", {"day": ["2021-01-01", "2021-01-02"], "price": [10.0, 12.0]})
    ov = price_overlay_from_csv(csv, name="X", start="2020-01-01", end="2099-01-01")
    assert ov.points[-1]["value"] == pytest.approx(1.2)


def test_price_overlay_missing_columns_raises(tmp_path: Path) -> None:
    csv = _write_csv(tmp_path / "bad.csv", {"foo": [1], "bar": [2]})
    with pytest.raises(ValueError, match="date and a value"):
        price_overlay_from_csv(csv, name="bad", start="2020-01-01", end="2099-01-01")


def test_overlay_series_dicts_auto_colors_and_drops_empty() -> None:
    overlays = [
        OverlaySpec(name="A", points=[{"date": "2021-01-01", "value": 1.0}]),  # auto color 0
        OverlaySpec(name="B", points=[{"date": "2021-01-01", "value": 1.0}]),  # auto color 1
        OverlaySpec(name="C", points=[{"date": "2021-01-01", "value": 1.0}], color=(1, 2, 3)),  # explicit
        OverlaySpec(name="EMPTY", points=[]),  # dropped
    ]
    dicts = _overlay_series_dicts(overlays)
    assert [d["name"] for d in dicts] == ["A", "B", "C"]
    assert dicts[0]["color"] == _OVERLAY_PALETTE[0]
    assert dicts[1]["color"] == _OVERLAY_PALETTE[1]
    assert dicts[2]["color"] == (1, 2, 3)


def test_monthly_table_rows_fills_gap_months() -> None:
    """A month where the book sat flat must appear as a +0.00% row, not vanish — a missing
    month is indistinguishable from a rendering bug and hides that the strategy was alive
    but idle (observed: 2025-11 / 2026-02 absent from the deployed-refresh table)."""
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


def test_chart_renders_with_overlay(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    days = ["2021-01-01", "2021-01-02", "2021-01-03"]
    ts = [1609459200000, 1609545600000, 1609632000000]
    equity = pl.DataFrame({"ts_ms": ts, "date": days, "equity": [1.0, 1.1, 1.2]})
    btc = pl.DataFrame(
        {"symbol": ["BTCUSDT"] * 3, "date": days, "ts_ms": ts, "close": [30000.0, 31000.0, 33000.0]}
    )
    ov_csv = _write_csv(tmp_path / "mu.csv", {"date": days, "close": [50.0, 52.0, 49.0]})
    overlay = price_overlay_from_csv(ov_csv, name="Micron (MU)", start=days[0], end=days[-1])

    meta = _write_equity_benchmark_chart(
        tmp_path,
        root=tmp_path,
        equity=equity,
        raw_klines=btc,
        png_name="combined_equity_btc.png",
        overlays=[overlay],
        strategy_name="Strategy 3x",
    )
    assert "Micron (MU)" in meta["overlays"]
    assert meta["series"]["Micron (MU)"] == 3
    assert meta["legend_items"] == 3
    assert (tmp_path / "combined_equity_btc.png").exists()


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
        root=tmp_path,
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
