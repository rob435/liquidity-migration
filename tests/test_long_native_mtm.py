"""Tests for the honest daily mark-to-market rendering of the long sleeve."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from liquidity_migration.long_native import _mtm_daily_curve, _mtm_summary

D1 = dt.date(2026, 1, 1)
MS_DAY = 86_400_000


def _ts(day: dt.date, hour: int = 12) -> int:
    return int(dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _klines(rows: list[tuple[str, dt.date, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "ts_ms": [_ts(d, 23) for _, d, _ in rows],
        "symbol": [s for s, _, _ in rows],
        "date": [d.isoformat() for _, d, _ in rows],
        "close": [c for _, _, c in rows],
    })


def _trade(symbol: str, nw: float, entry_px: float, exit_px: float,
           d_in: dt.date, d_out: dt.date, costfund: float = 0.0) -> dict:
    return {
        "symbol": symbol, "notional_weight": nw,
        "entry_price": entry_px, "exit_price": exit_px,
        "cost_return": costfund, "funding_return": 0.0,
        "entry_ts_ms": _ts(d_in, 2), "exit_ts_ms": _ts(d_out, 2),
    }


def test_mtm_marks_daily_and_books_costs_on_exit() -> None:
    d2, d3 = D1 + dt.timedelta(days=1), D1 + dt.timedelta(days=2)
    klines = _klines([("AAAUSDT", D1, 104.0), ("AAAUSDT", d2, 108.0), ("AAAUSDT", d3, 999.0)])
    trades = pl.DataFrame([_trade("AAAUSDT", 0.1, 100.0, 110.0, D1, d3, costfund=-0.001)])
    mtm = _mtm_daily_curve(trades, klines)
    assert mtm.height == 3
    rets = mtm["mtm_return"].to_list()
    assert rets[0] == pytest.approx(0.1 * (104 / 100 - 1))
    assert rets[1] == pytest.approx(0.1 * (108 / 104 - 1))
    # exit day uses the EXIT price (not the day close) + costs/funding
    assert rets[2] == pytest.approx(0.1 * (110 / 108 - 1) - 0.001)
    eq = mtm["equity"].to_list()
    assert eq[-1] == pytest.approx((1 + rets[0]) * (1 + rets[1]) * (1 + rets[2]))


def test_flat_days_are_zero_and_summary_counts_active() -> None:
    d4, d6 = D1 + dt.timedelta(days=3), D1 + dt.timedelta(days=5)
    klines = _klines([("AAAUSDT", D1, 100.0), ("BBBUSDT", d6, 50.0)])
    trades = pl.DataFrame([
        _trade("AAAUSDT", 0.1, 100.0, 102.0, D1, D1),
        _trade("BBBUSDT", 0.2, 40.0, 44.0, d6, d6),
    ])
    mtm = _mtm_daily_curve(trades, klines)
    assert mtm.height == 6
    rets = mtm["mtm_return"].to_list()
    assert rets[0] == pytest.approx(0.1 * 0.02)
    assert rets[1] == rets[2] == rets[3] == rets[4] == 0.0
    assert rets[5] == pytest.approx(0.2 * 0.1)
    s = _mtm_summary(mtm)
    assert s["mtm_active_day_frac"] == pytest.approx(2 / 6, abs=1e-3)
    assert s["mtm_max_drawdown"] == 0.0
    _ = d4


def test_empty_trades_yield_empty_frame() -> None:
    klines = _klines([("AAAUSDT", D1, 100.0)])
    mtm = _mtm_daily_curve(pl.DataFrame(), klines)
    assert mtm.is_empty()
    assert _mtm_summary(mtm) == {}
