"""audit2b regression: _strategy_equity_series must not crash on a date-only
frame (no ts_ms) and must leave the ts_ms happy path unchanged."""

from __future__ import annotations

import math

import polars as pl

from liquidity_migration.volume_events_charts import _strategy_equity_series


def test_date_only_frame_does_not_crash():
    # Frame carries the guarded date+equity columns but NOT ts_ms. Old code
    # passed the guard then raised ColumnNotFoundError at .sort("ts_ms").
    frame = pl.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-01", "2024-01-03"],
            "equity": [101.0, 100.0, 102.0],
        }
    )
    out = _strategy_equity_series(frame)
    # Falls back to date ordering; series is sorted ascending by date.
    assert out == [
        {"date": "2024-01-01", "value": 100.0},
        {"date": "2024-01-02", "value": 101.0},
        {"date": "2024-01-03", "value": 102.0},
    ]


def test_ts_ms_happy_path_unchanged():
    # ts_ms present: ordering comes from ts_ms exactly as before the fix.
    frame = pl.DataFrame(
        {
            "ts_ms": [2000, 1000, 3000],
            "date": ["2024-01-02", "2024-01-01", "2024-01-03"],
            "equity": [101.0, 100.0, 102.0],
        }
    )
    out = _strategy_equity_series(frame)
    assert out == [
        {"date": "2024-01-01", "value": 100.0},
        {"date": "2024-01-02", "value": 101.0},
        {"date": "2024-01-03", "value": 102.0},
    ]


def test_ts_ms_ordering_drives_output_not_date():
    # When ts_ms disagrees with date order, ts_ms wins (unchanged semantics):
    # rows emitted in ts_ms order, carrying their own date string.
    frame = pl.DataFrame(
        {
            "ts_ms": [3000, 1000, 2000],
            "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "equity": [100.0, 101.0, 102.0],
        }
    )
    out = _strategy_equity_series(frame)
    assert out == [
        {"date": "2024-01-02", "value": 101.0},
        {"date": "2024-01-03", "value": 102.0},
        {"date": "2024-01-01", "value": 100.0},
    ]


def test_non_finite_and_unparseable_rows_dropped():
    frame = pl.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-02", "bad-date"],
            "equity": [100.0, float("nan"), 102.0],
        }
    )
    out = _strategy_equity_series(frame)
    assert out == [{"date": "2024-01-01", "value": 100.0}]
    assert all(math.isfinite(r["value"]) for r in out)
