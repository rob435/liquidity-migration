"""Tests for the Bybit forward depth collector: band aggregation + universe hardening."""

from __future__ import annotations

import json
import urllib.error

import pytest

import liquidity_migration.depth_collector as depth_collector
from liquidity_migration.depth_collector import _refresh_universe, band_notionals, trading_universe


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


def test_trading_universe_breaks_on_repeating_cursor(monkeypatch) -> None:
    """A misbehaving endpoint that returns the same nextPageCursor forever must
    not spin trading_universe into an unbounded request loop."""
    calls: list[str] = []

    def fake_get_json(url: str) -> dict:
        calls.append(url)
        return {
            "result": {
                "list": [{"symbol": "AAAUSDT", "status": "Trading"}],
                "nextPageCursor": "samecursor",
            }
        }

    monkeypatch.setattr(depth_collector, "_get_json", fake_get_json)
    assert trading_universe() == ["AAAUSDT"]
    # page 1 (empty cursor) yields "samecursor"; page 2 repeats it -> break
    assert len(calls) == 2


def test_trading_universe_caps_at_ten_pages(monkeypatch) -> None:
    """Distinct cursors forever: pagination is hard-capped at 10 pages
    (mirrors the liquidation collector's defensive cap)."""
    counter = {"n": 0}

    def fake_get_json(url: str) -> dict:
        counter["n"] += 1
        return {
            "result": {
                "list": [
                    {"symbol": f"S{counter['n']:02d}USDT", "status": "Trading"},
                    {"symbol": f"X{counter['n']:02d}USDT", "status": "PreLaunch"},
                    {"symbol": f"P{counter['n']:02d}USDC", "status": "Trading"},
                ],
                "nextPageCursor": f"cursor-{counter['n']}",
            }
        }

    monkeypatch.setattr(depth_collector, "_get_json", fake_get_json)
    symbols = trading_universe()
    assert counter["n"] == 10
    # only Trading USDT perps survive the filter, one per page
    assert symbols == sorted(f"S{i:02d}USDT" for i in range(1, 11))


def test_refresh_universe_failure_keeps_previous_universe(monkeypatch) -> None:
    """A transient instruments-info failure must not propagate out of the
    periodic refresh (it used to raise out of main() and kill the daemon)."""

    def network_boom() -> list[str]:
        raise urllib.error.URLError("instruments endpoint down")

    monkeypatch.setattr(depth_collector, "trading_universe", network_boom)
    assert _refresh_universe(["BTCUSDT", "ETHUSDT"]) == ["BTCUSDT", "ETHUSDT"]
    assert _refresh_universe([]) == []  # nothing to fall back on -> empty, no raise

    def payload_boom() -> list[str]:
        raise json.JSONDecodeError("bad payload", "doc", 0)

    monkeypatch.setattr(depth_collector, "trading_universe", payload_boom)
    assert _refresh_universe(["BTCUSDT"]) == ["BTCUSDT"]

    monkeypatch.setattr(depth_collector, "trading_universe", lambda: ["NEWUSDT"])
    assert _refresh_universe(["BTCUSDT"]) == ["NEWUSDT"]  # success replaces previous
