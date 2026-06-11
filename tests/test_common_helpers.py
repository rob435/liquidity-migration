"""Tests for small _common helpers."""

from __future__ import annotations

from liquidity_migration._common import MS_PER_DAY, is_weekend_ms


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
