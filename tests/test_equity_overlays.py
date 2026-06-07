from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.volume_events_charts import (
    _OVERLAY_PALETTE,
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
    assert (tmp_path / "combined_equity_btc.png").exists()
