"""Tests for the Bybit forward depth collector's band aggregation."""

from __future__ import annotations

import pytest

from liquidity_migration.depth_collector import band_notionals


def test_band_cumulative_notional_and_null_beyond_span() -> None:
    # mid = 100; bids at -0.1%, -0.9%, -1.8%; asks at +0.1%, +0.5% only (thin ask side)
    bids = [(99.9, 10.0), (99.1, 20.0), (98.2, 30.0)]
    asks = [(100.1, 5.0), (100.5, 5.0)]
    out = band_notionals(bids, asks)
    assert out is not None
    assert out["mid"] == pytest.approx(100.0)
    # bid side spans 1.8% -> 0.2% and 1% bands measured, 2%+ unmeasured (None)
    assert out["bid_0p2"] == pytest.approx(99.9 * 10.0)
    assert out["bid_1p0"] == pytest.approx(99.9 * 10.0 + 99.1 * 20.0)
    assert out["bid_2p0"] is None and out["bid_5p0"] is None
    assert out["bid_span_pct"] == pytest.approx(1.8, abs=1e-3)
    # ask side spans only 0.5% -> even the 1% band is unmeasured
    assert out["ask_0p2"] == pytest.approx(100.1 * 5.0)
    assert out["ask_1p0"] is None
    assert out["n_bid_levels"] == 3 and out["n_ask_levels"] == 2


def test_deep_book_measures_all_bands() -> None:
    bids = [(100.0 - i * 0.5, 1.0) for i in range(1, 13)]  # to -6%
    asks = [(100.0 + i * 0.5, 1.0) for i in range(1, 13)]
    out = band_notionals(bids, asks)
    assert out is not None
    for band in ("0p2", "1p0", "2p0", "3p0", "4p0", "5p0"):
        assert out[f"bid_{band}"] is not None
        assert out[f"ask_{band}"] is not None
    # 5% band on the bid side: levels 99.5 .. 95.0 (10 levels within -5% of mid 100)
    assert out["bid_5p0"] == pytest.approx(sum(100.0 - i * 0.5 for i in range(1, 11)))


def test_empty_side_returns_none() -> None:
    assert band_notionals([], [(100.1, 1.0)]) is None
    assert band_notionals([(99.9, 1.0)], []) is None
