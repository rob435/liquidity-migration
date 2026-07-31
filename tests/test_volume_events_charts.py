"""``_strategy_equity_series`` must handle a date-only frame (no ts_ms) and leave the
ts_ms path unchanged.
"""

from __future__ import annotations

import math

import polars as pl

from liquidity_migration.research.backtest.volume_events_charts import _strategy_equity_series


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
    # ts_ms present: ordering comes from ts_ms.
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


# ---------------------------------------------------------------------------
# The y-axis floor follows the data when it goes negative
# ---------------------------------------------------------------------------


def test_nice_axis_floor_follows_negative_data() -> None:
    """A levered equity curve blowing through zero must not have its y-floor pinned at
    0, which draws the wipeout below the plot floor; when min < 0 the floor follows
    the data.
    """
    from liquidity_migration.research.backtest.volume_events_charts import _nice_axis

    low, _high, ticks = _nice_axis(-0.27, 2.0, target_ticks=12)
    assert low < 0.0  # the floor descended below zero to show the blow-through
    assert low <= -0.27  # the worst point is inside the plot, not clipped at the axis
    assert min(ticks) <= -0.27


def test_nice_axis_still_clamps_floor_at_zero_for_nonnegative_data() -> None:
    """A non-negative series whose padded floor would dip below 0 is still pinned at 0 --
    the axis never invents negative territory the curve never visited.
    """
    from liquidity_migration.research.backtest.volume_events_charts import _nice_axis

    # min near zero: the 5% pad would push floor_candidate below 0; the clamp holds it at 0.
    low, _high, ticks = _nice_axis(0.02, 1.5, target_ticks=12)
    assert low == 0.0
    assert min(ticks) >= 0.0
    # a higher non-negative curve also never floors below 0.
    low2, _h2, _t2 = _nice_axis(1.0, 2.0, target_ticks=12)
    assert low2 >= 0.0


# ---------------------------------------------------------------------------
# "Trades" header only when a real trades column is present
# ---------------------------------------------------------------------------


def test_monthly_table_labels_days_when_no_trades_column() -> None:
    """A monthly frame carrying only month+strategy_return has no trade counts, so the
    rows must not claim "Trades: 0": the count is derived from per-month equity DAYS
    and labelled "Days".
    """
    from liquidity_migration.research.backtest.volume_events_charts import _has_columns, _monthly_table_rows

    monthly = pl.DataFrame(
        {"month": ["2025-01", "2025-02"], "strategy_return": [0.05, -0.02]}
    )
    equity = pl.DataFrame(
        {"date": ["2025-01-10", "2025-01-20", "2025-02-05"], "basket_return": [0.01, 0.0, -0.01]}
    )
    # the caller's count_label gate: "Trades" only with a real trades column
    has_real_monthly = (
        not monthly.is_empty() and _has_columns(monthly, "month", "strategy_return", "trades")
    )
    assert has_real_monthly is False  # -> caller renders "Days", not "Trades"

    rows = {r["month"]: r for r in _monthly_table_rows(equity=equity, monthly=monthly)}
    # real returns kept; counts are equity DAYS per month (2 in Jan, 1 in Feb), not 0
    assert abs(rows["2025-01"]["return"] - 0.05) < 1e-12
    assert rows["2025-01"]["count"] == 2
    assert rows["2025-02"]["count"] == 1


def test_monthly_table_labels_trades_when_trades_column_present() -> None:
    """Companion: a frame WITH a trades column keeps the honest "Trades" path."""
    from liquidity_migration.research.backtest.volume_events_charts import _has_columns, _monthly_table_rows

    monthly = pl.DataFrame(
        {"month": ["2025-01"], "strategy_return": [0.05], "trades": [7]}
    )
    assert _has_columns(monthly, "month", "strategy_return", "trades") is True
    rows = _monthly_table_rows(equity=pl.DataFrame(), monthly=monthly)
    assert rows[0]["count"] == 7


# ---------------------------------------------------------------------------
# Legend multiples read over the common (earliest-end) window
# ---------------------------------------------------------------------------


def test_chart_final_values_uses_common_end_window() -> None:
    """When BTC ends before the flat-extended strategy curve, the legend multiple for
    EACH series is read at the last date common to all series, not each series' own
    last point, which compared spans of different length.
    """
    from liquidity_migration.research.backtest.volume_events_charts import _chart_final_values

    series = [
        {"name": "Strategy", "points": [
            {"date": "2025-01-01", "value": 1.0},
            {"date": "2025-02-01", "value": 1.5},
            {"date": "2025-03-01", "value": 2.0},  # flat-extended past BTC
        ]},
        {"name": "BTC", "points": [
            {"date": "2025-01-01", "value": 1.0},
            {"date": "2025-02-01", "value": 1.2},  # BTC ends here
        ]},
    ]
    finals = _chart_final_values(series)
    # common end is 2025-02-01: strategy read THERE (1.5), not at its 2025-03 end (2.0)
    assert abs(finals["Strategy"] - 1.5) < 1e-9
    assert abs(finals["BTC"] - 1.2) < 1e-9
