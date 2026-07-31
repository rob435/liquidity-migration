"""Tests for small _common helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from liquidity_migration.core._common import (
    MS_PER_DAY,
    calendar_roll,
    calendar_shift,
    exact_duration_ms,
    is_weekend_ms,
)


def _date_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_calendar_shift_nulls_across_a_gap_but_matches_shift_when_contiguous() -> None:
    """``calendar_shift`` returns the value exactly ``periods`` CALENDAR days back, or
    NULL if that row is missing; a plain ``shift(periods)`` reaches across a gap and
    mislabels the lookback.
    """
    df = pl.DataFrame(
        {
            "symbol": ["X", "X", "X"],
            "ts_ms": [0, MS_PER_DAY, 3 * MS_PER_DAY],  # gap on day 2
            "close": [10.0, 11.0, 13.0],
        }
    ).sort(["symbol", "ts_ms"])
    out = df.with_columns(
        calendar_shift(pl.col("close"), 1).alias("cs"),
        pl.col("close").shift(1).over("symbol").alias("plain"),
    )
    cs = out["cs"].to_list()
    plain = out["plain"].to_list()
    # Contiguous rows agree with a plain shift; the post-gap row is NULL (not
    # back-filled across the missing day, which plain shift does).
    assert cs == [None, 10.0, None]
    assert plain == [None, 10.0, 11.0]


def test_calendar_roll_window_is_calendar_bounded_not_row_bounded() -> None:
    """calendar_roll measures a TIME span, so a missing day shrinks the window
    rather than stretching an 'Nd' feature across more than N calendar days."""
    df = pl.DataFrame(
        {
            "ts_ms": [0, MS_PER_DAY, 3 * MS_PER_DAY],  # gap on day 2
            "v": [1.0, 1.0, 1.0],
        }
    ).sort("ts_ms")
    out = df.with_columns(
        calendar_roll(pl.col("v"), "sum", 2, shifted=False, min_samples=1).alias("cr"),
    )
    # day3's 2-day window is (day1, day3]; it excludes day1, so the sum is 1.0.
    # A row-based rolling_sum(window_size=2) would include day1 and give 2.0.
    assert out["cr"].to_list() == [1.0, 2.0, 1.0]


def test_exact_duration_ms_keeps_month_equivalent_precise() -> None:
    month_ms = 2_629_800_000  # 365.25 / 12 days, in milliseconds.
    anchor = 1_700_000_123_456

    assert exact_duration_ms(days=365.25 / 12) == month_ms
    assert anchor - exact_duration_ms(days=365.25 / 12) == anchor - month_ms


def test_exact_duration_ms_rejects_sub_millisecond_duration() -> None:
    with pytest.raises(ValueError, match="whole milliseconds"):
        exact_duration_ms(seconds="0.0001")


def test_exact_duration_ms_rejects_negative_duration() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        exact_duration_ms(minutes=-1)


def test_is_weekend_ms() -> None:
    # 1970-01-01 (epoch day 0) was a Thursday
    thursday = 0
    saturday = 2 * MS_PER_DAY
    sunday = 3 * MS_PER_DAY
    monday = 4 * MS_PER_DAY
    assert not is_weekend_ms(thursday)
    assert is_weekend_ms(saturday)
    assert is_weekend_ms(saturday + MS_PER_DAY - 1)
    assert is_weekend_ms(sunday)
    assert not is_weekend_ms(monday)
    # a known date: 2026-06-07 was a Sunday
    import datetime as dt
    ts = int(dt.datetime(2026, 6, 7, 12, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert is_weekend_ms(ts)
    assert not is_weekend_ms(ts + MS_PER_DAY)


def test_is_weekend_ms_matches_old_inline_formula() -> None:
    """The helper is byte-identical to the inline math it replaced, so the weekend tilt's selection is unchanged."""
    base = _date_ms("2025-01-01")
    for d in range(14):
        ts = base + d * MS_PER_DAY + 12 * 3_600_000
        assert is_weekend_ms(ts) == (((int(ts) // MS_PER_DAY) + 3) % 7 >= 5)
