"""Focused tests for the Strategy Research V3 exploratory analysis machinery.

Synthetic-data tests only: no SHARED_DATA root, no V2 artifacts, no network.
"""

from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from scripts.research_v3 import common
from scripts.research_v3.tb_funding_floor import build_trade_panel, simulate_drain_exits
from scripts.research_v3.tc_pump_deceleration import (
    compute_features,
    entry_state,
    resimulate_from,
)

BASE = 1_622_505_600_000  # 2021-06-01 00:00:00 UTC (a UTC midnight)


def _funding_series() -> common.FundingSeries:
    return {
        "TESTUSDT": (
            [BASE, BASE + 8 * MS_PER_HOUR, BASE + 16 * MS_PER_HOUR, BASE + 24 * MS_PER_HOUR],
            [-0.003, -0.004, -0.004, 0.001],
        )
    }


def _bar_series(closes: list[float], start_end_ms: int) -> common.BarSeries:
    ends = [start_end_ms + i * MS_PER_HOUR for i in range(len(closes))]
    return {"TESTUSDT": (ends, closes)}


def _trade(**overrides: object) -> dict[str, object]:
    trade = {
        "trade_id": "t1",
        "sleeve": "continuous",
        "symbol": "TESTUSDT",
        "side": "short",
        "entry_ts_ms": BASE + 2 * MS_PER_HOUR,
        "exit_ts_ms": BASE + 26 * MS_PER_HOUR,
        "entry_price": 100.0,
        "exit_price": 90.0,
        "take_profit_price": 88.0,
        "notional_weight": 0.01,
        "cost_return": -0.0001,
        "funding_return": 0.01 * (-(-0.004) + -(-0.004) + 0.001) * -1.0,  # placeholder, unused
        "exit_reason": "max_hold",
        "exit_date": "2021-06-02",
    }
    trade["gross_return"] = 0.01 * (100.0 - 90.0) / 100.0
    trade["net_return"] = 0.0
    trade.update(overrides)
    return trade


def test_signed_funding_events_window_and_sign() -> None:
    series = _funding_series()
    ts, signed = common.signed_funding_events(
        side="short",
        symbol="TESTUSDT",
        entry_ts_ms=BASE,  # entry exactly at a settlement: excluded
        exit_ts_ms=BASE + 24 * MS_PER_HOUR,  # exit at a settlement: included
        series=series,
    )
    assert ts == [BASE + 8 * MS_PER_HOUR, BASE + 16 * MS_PER_HOUR, BASE + 24 * MS_PER_HOUR]
    assert signed == [-0.004, -0.004, 0.001]  # short: +rate
    _ts, signed_long = common.signed_funding_events(
        side="long",
        symbol="TESTUSDT",
        entry_ts_ms=BASE,
        exit_ts_ms=BASE + 24 * MS_PER_HOUR,
        series=series,
    )
    assert signed_long == [0.004, 0.004, -0.001]  # long: -rate


def test_trade_daily_contributions_exact_buckets() -> None:
    closes = [100.0 + i for i in range(30)]  # bar ends BASE+3h .. BASE+32h
    bars = _bar_series(closes, BASE + 3 * MS_PER_HOUR)
    trades = pl.DataFrame([_trade()])
    contributions = common.trade_daily_contributions(trades, _funding_series(), bars)
    day0 = BASE
    day1 = BASE + MS_PER_DAY
    frame = contributions.sort(["day_ms", "component"])
    cost = frame.filter(pl.col("component") == "cost_return")
    assert cost["day_ms"].to_list() == [day0]
    assert cost["value"][0] == pytest.approx(-0.0001)
    funding = frame.filter(pl.col("component") == "funding_return").sort("day_ms")
    # settlements at +8h and +16h land on day0; +24h lands exactly on day1
    assert funding["day_ms"].to_list() == [day0, day1]
    assert funding["value"].to_list() == pytest.approx([0.01 * (-0.008), 0.01 * 0.001])
    gross = frame.filter(pl.col("component") == "gross_return").sort("day_ms")
    # marks: bar ends in (entry, exit) plus exit at exit_price; day0's last
    # mark is the bar ending 23:00 (close 100+20=120), day1 ends at exit 90.
    expected_day0 = 0.01 * -1.0 * (120.0 - 100.0) / 100.0
    expected_day1 = 0.01 * -1.0 * (90.0 - 120.0) / 100.0
    assert gross["day_ms"].to_list() == [day0, day1]
    assert gross["value"].to_list() == pytest.approx([expected_day0, expected_day1])
    assert sum(gross["value"].to_list()) == pytest.approx(float(trades["gross_return"][0]))


def test_daily_curve_additive_equity_and_drawdown() -> None:
    contributions = pl.DataFrame(
        {
            "sleeve": ["continuous"] * 3,
            "day_ms": [BASE, BASE, BASE + MS_PER_DAY],
            "component": ["gross_return", "cost_return", "gross_return"],
            "value": [-0.02, -0.01, 0.01],
        }
    )
    curve = common.daily_curve(
        contributions, sleeve="continuous", start_day_ms=BASE, end_day_ms=BASE + 2 * MS_PER_DAY
    )
    assert curve["equity"].to_list() == pytest.approx([0.97, 0.98, 0.98])
    assert curve["drawdown"].to_list() == pytest.approx([-0.03, -0.02, -0.02])


def test_tb_floor_conventions() -> None:
    trades = pl.DataFrame([_trade()])
    panel = build_trade_panel(trades, _funding_series())
    row = panel.to_dicts()[0]
    assert row["n_intervals_planned_hold"] == 3  # +8h, +16h, +24h within (entry, entry+24h]
    assert row["known_rate_prev"] == pytest.approx(-0.003)
    assert row["known_rate_next"] == pytest.approx(-0.004)
    assert row["floor_prev"] == pytest.approx(0.009)
    assert row["floor_next"] == pytest.approx(0.012)
    assert row["tp_distance"] == pytest.approx(0.12)
    assert row["cost_per_unit"] == pytest.approx(0.01)

    positive = {
        "TESTUSDT": (
            [BASE, BASE + 8 * MS_PER_HOUR],
            [0.01, 0.01],
        )
    }
    row_positive = build_trade_panel(trades, positive).to_dicts()[0]
    assert row_positive["floor_prev"] == 0.0  # expected credit: no floor


def test_tb_drain_exit_trigger_and_recompute() -> None:
    closes = [100.0 - i for i in range(30)]  # bar ends BASE+3h .. BASE+32h
    bars = _bar_series(closes, BASE + 3 * MS_PER_HOUR)
    trades = pl.DataFrame([_trade()])
    panel = build_trade_panel(trades, _funding_series())

    # fraction 0.05 -> threshold 0.006; at the first settlement realized 0.004
    # + projected 0.008 = 0.012 triggers immediately.
    modified, counters = simulate_drain_exits(panel, _funding_series(), bars, 0.05)
    assert counters == {"triggered": 1, "no_exit_bar": 0}
    row = modified.to_dicts()[0]
    assert row["exit_reason"] == "funding_drain"
    assert row["exit_ts_ms"] == BASE + 8 * MS_PER_HOUR  # first bar end >= trigger
    exit_close = closes[5]  # bar ending BASE+8h
    assert row["exit_price"] == pytest.approx(exit_close)
    assert row["gross_return"] == pytest.approx(0.01 * (100.0 - exit_close) / 100.0)
    assert row["funding_return"] == pytest.approx(0.01 * -0.004)
    assert row["funding_event_count"] == 1

    # fraction 0.2 -> threshold 0.024; never reached, trade unchanged.
    unchanged, counters2 = simulate_drain_exits(panel, _funding_series(), bars, 0.2)
    assert counters2 == {"triggered": 0, "no_exit_bar": 0}
    assert unchanged.to_dicts()[0]["exit_reason"] == "max_hold"


def test_tc_entry_state_classification() -> None:
    assert entry_state(0.02, 0.01) == "accelerating"
    assert entry_state(0.02, -0.001) == "decelerating"
    assert entry_state(-0.01, 0.05) == "pullback"
    assert entry_state(None, None) == "unknown"


def test_tc_compute_features_and_entry_bar_check() -> None:
    closes = [100.0] * 27 + [100.0, 101.0, 103.0]  # entry bar last: r=1.98%, prev r=1%
    ends_start = BASE - 29 * MS_PER_HOUR
    bars = {"TESTUSDT": ([ends_start + i * MS_PER_HOUR for i in range(30)], None, None, closes)}
    ohlc = {
        "TESTUSDT": (
            bars["TESTUSDT"][0],
            [c * 1.01 for c in closes],
            [c * 0.99 for c in closes],
            closes,
        )
    }
    trades = pl.DataFrame(
        [_trade(entry_ts_ms=ends_start + 29 * MS_PER_HOUR, entry_price=103.0)]
    )
    panel = compute_features(trades, ohlc)
    row = panel.to_dicts()[0]
    assert row["state"] == "accelerating"
    assert row["r_1h"] == pytest.approx(103.0 / 101.0 - 1.0)
    assert row["mom_delta"] == pytest.approx((103.0 / 101.0 - 1.0) - 0.01)
    assert row["hours_since_high_168h"] == pytest.approx(0.0)  # entry bar is the high

    with pytest.raises(RuntimeError, match="entry bar close differs"):
        compute_features(
            pl.DataFrame([_trade(entry_ts_ms=ends_start + 29 * MS_PER_HOUR, entry_price=999.0)]),
            ohlc,
        )


def test_tc_resimulate_take_profit_exit() -> None:
    ends = [BASE + i * MS_PER_HOUR for i in range(30)]
    closes = [100.0] * 30
    highs = [101.0] * 30
    lows = [99.0] * 30
    lows[6] = 87.0  # TP 88 touched on the bar after the delayed entry
    ohlc = {"TESTUSDT": (ends, highs, lows, closes)}
    series: common.FundingSeries = {"TESTUSDT": ([], [])}
    trade = _trade()
    row = resimulate_from(trade, ohlc, series, new_entry_index=5)
    assert row is not None
    assert row["entry_ts_ms"] == ends[5]
    assert row["entry_price"] == pytest.approx(100.0)
    assert row["exit_reason"] == "take_profit"
    assert row["exit_ts_ms"] == ends[6]
    assert row["exit_price"] == pytest.approx(88.0)
    assert row["gross_return"] == pytest.approx(0.01 * (100.0 - 88.0) / 100.0)
    assert row["funding_return"] == 0.0
    assert row["net_return"] == pytest.approx(row["gross_return"] - 0.0001)
