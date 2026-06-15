"""audit2 regression: regenerate_hedge_warmstart.daily_returns gap-guard.

The naive builder paired adjacent PRESENT kline days with no calendar guard, so
a missing UTC day made a multi-day close-to-close move be mislabeled as a single
day's return -> corrupted the BTC/ETH beta in deploy/hedge_warmstart/*.csv. The
fix only emits a return when cur - prev == one calendar day; contiguous series
are numerically identical to the naive computation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "scripts" / "regenerate_hedge_warmstart.py"
_spec = importlib.util.spec_from_file_location("_audit2_regen_warmstart", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

MS_DAY = mod.MS_DAY


def _naive(closes: dict[int, float]) -> dict[int, float]:
    """The pre-fix builder: pairs adjacent present days, no calendar guard."""
    days = sorted(closes)
    out: dict[int, float] = {}
    for prev, cur in zip(days, days[1:]):
        if closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def test_gap_day_pair_is_dropped() -> None:
    # day0, day1 present, day2 MISSING, day3 present -> the (day1, day3) pair
    # spans 2 calendar days and must NOT emit a return.
    d0 = 0
    d1 = MS_DAY
    d3 = 3 * MS_DAY  # day2 (2*MS_DAY) absent -> a 1-day gap
    closes = {d0: 100.0, d1: 110.0, d3: 132.0}

    out = mod.daily_returns(closes)

    # The contiguous d0->d1 return survives.
    assert d1 in out
    assert out[d1] == 110.0 / 100.0 - 1.0
    # The post-gap day is dropped: no multi-day return mislabeled as one day.
    assert d3 not in out

    # The naive builder DID mislabel it (guards the regression direction).
    naive = _naive(closes)
    assert d3 in naive
    assert naive[d3] == 132.0 / 110.0 - 1.0


def test_contiguous_series_identical_to_naive() -> None:
    # Fully calendar-consecutive days: the gap-guard is a no-op, output must
    # match the naive computation exactly (numerical equivalence on happy path).
    closes = {i * MS_DAY: 100.0 + i for i in range(6)}

    out = mod.daily_returns(closes)
    naive = _naive(closes)

    assert out == naive
    assert set(out) == {i * MS_DAY for i in range(1, 6)}
    # Spot-check a concrete value to anchor the equivalence.
    assert out[MS_DAY] == 101.0 / 100.0 - 1.0
