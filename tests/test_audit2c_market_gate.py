"""audit2c: the equal-weight market-regime gate computes gap-aware daily returns.

A symbol with a missing calendar day must NOT contribute a multi-day return mislabelled
as that day's 1-day return to the cross-sectional mean.
"""

from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.continuous_events import _market_daily_returns

D0, D1, D2 = 0, MS_PER_DAY, 2 * MS_PER_DAY


def _bar(symbol, day_ts, close):
    return {"ts_ms": day_ts + 3600_000, "symbol": symbol, "close": close}


def test_market_daily_returns_excludes_gap_day() -> None:
    klines = pl.DataFrame([
        # Symbol A: contiguous days 0,1,2 -> +10% each day.
        _bar("A", D0, 100.0), _bar("A", D1, 110.0), _bar("A", D2, 121.0),
        # Symbol B: GAP at day 1 (present on day 0 and day 2 only).
        _bar("B", D0, 100.0), _bar("B", D2, 120.0),
    ])
    out = _market_daily_returns(klines)
    # Day 1: only A has a return (B has no day-1 bar).
    assert out[D1] == pytest.approx(0.10)
    # Day 2: A = +10%; B's return is gap-aware NULL (no calendar-consecutive day-1
    # predecessor), so B is EXCLUDED — the mean is 0.10, NOT mean(0.10, 0.20)=0.15 that
    # the old gap-blind shift(1) would have produced.
    assert out[D2] == pytest.approx(0.10)


def test_market_daily_returns_contiguous_unchanged() -> None:
    klines = pl.DataFrame([
        _bar("A", D0, 100.0), _bar("A", D1, 110.0),
        _bar("B", D0, 200.0), _bar("B", D1, 210.0),
    ])
    out = _market_daily_returns(klines)
    # A +10%, B +5% -> mean 7.5% (byte-identical to the plain-shift result on contiguous data).
    assert out[D1] == pytest.approx(0.075)
