"""Focused tests for the Strategy Research V4 exploratory analysis machinery.

Synthetic-data tests only: no SHARED_DATA root, no V2 artifacts, no network.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR
from scripts.research_v3 import common, v4_shared
from scripts.research_v3.tf_mfe_giveback import walk_exit
from scripts.research_v3.th_expected_net import fit_ridge, quarterly_refits, rank_transform
from scripts.research_v3.ti_regime_intensity import member_weight

BASE = 1_622_505_600_000  # 2021-06-01 00:00:00 UTC


def test_freshness_bucket_edges() -> None:
    assert v4_shared.freshness_bucket(None) == "unknown"
    assert v4_shared.freshness_bucket(0.0) == "at_high_le1h"
    assert v4_shared.freshness_bucket(1.0) == "at_high_le1h"
    assert v4_shared.freshness_bucket(1.0001) == "1_6h"
    assert v4_shared.freshness_bucket(6.0) == "1_6h"
    assert v4_shared.freshness_bucket(24.0) == "6_24h"
    assert v4_shared.freshness_bucket(24.5) == "gt_24h"


def test_funding_bucket_edges() -> None:
    assert v4_shared.funding_bucket(None) == "missing"
    assert v4_shared.funding_bucket(-0.0011) == "deep_neg"
    assert v4_shared.funding_bucket(-0.001) == "neg"  # edge is strict <
    assert v4_shared.funding_bucket(-1e-9) == "neg"
    assert v4_shared.funding_bucket(0.0) == "zero"
    assert v4_shared.funding_bucket(0.0001) == "zero"
    assert v4_shared.funding_bucket(0.000101) == "pos"


def test_hours_since_high_tolerant() -> None:
    ends = [BASE + i * MS_PER_HOUR for i in range(40)]
    closes = [100.0] * 40
    closes[30] = 120.0  # the high
    hours, status = v4_shared.hours_since_high_tolerant(ends[35], ends, closes)
    assert status == "ok"
    assert hours == pytest.approx(5.0)
    # Entry between bars: last bar at or before is used, status flags it.
    hours2, status2 = v4_shared.hours_since_high_tolerant(ends[35] + 1, ends, closes)
    assert status2 == "misaligned"
    assert hours2 == pytest.approx(5.0 + 1 / MS_PER_HOUR)
    assert v4_shared.hours_since_high_tolerant(BASE - 1, ends, closes) == (None, "no_bar")
    assert v4_shared.hours_since_high_tolerant(ends[5], ends, closes)[1] == "short_history"


def test_known_prev_rate_pit() -> None:
    series: common.FundingSeries = {
        "TESTUSDT": ([BASE, BASE + 8 * MS_PER_HOUR], [-0.003, 0.002])
    }
    # A settlement exactly at entry has just realized: it is known.
    assert v4_shared.known_prev_rate("TESTUSDT", BASE, series) == pytest.approx(-0.003)
    assert v4_shared.known_prev_rate("TESTUSDT", BASE - 1, series) is None
    assert v4_shared.known_prev_rate("TESTUSDT", BASE + 9 * MS_PER_HOUR, series) == pytest.approx(0.002)


def _weighted_panel() -> pl.DataFrame:
    day2 = BASE + 24 * MS_PER_HOUR
    return pl.DataFrame(
        {
            "trade_id": ["a", "b", "c"],
            "sleeve": ["continuous"] * 3,
            "symbol": ["TESTUSDT"] * 3,
            "side": ["short"] * 3,
            "entry_ts_ms": [BASE + 2 * MS_PER_HOUR, BASE + 2 * MS_PER_HOUR, day2 + 2 * MS_PER_HOUR],
            "exit_ts_ms": [BASE + 4 * MS_PER_HOUR, BASE + 4 * MS_PER_HOUR, day2 + 4 * MS_PER_HOUR],
            "entry_price": [100.0] * 3,
            "exit_price": [98.0] * 3,
            "exit_reason": ["max_hold"] * 3,
            "notional_weight": [0.01] * 3,
            "gross_return": [0.0002] * 3,
            "cost_return": [-0.0001] * 3,
            "funding_return": [0.0] * 3,
            "net_return": [0.0001] * 3,
            "mae": [-0.01] * 3,
            "weight_factor": [1.0, 0.5, 0.0],
        }
    )


def test_apply_weight_factor_scales_and_drops() -> None:
    scaled = v4_shared.apply_weight_factor(_weighted_panel())
    assert scaled.height == 2
    assert scaled["notional_weight"].to_list() == pytest.approx([0.01, 0.005])
    assert scaled["net_return"].to_list() == pytest.approx([0.0001, 0.00005])


def test_weighted_cell_metrics_decomposition_identity() -> None:
    panel = _weighted_panel()
    series: common.FundingSeries = {"TESTUSDT": ([], [])}
    ends = [BASE + i * MS_PER_HOUR for i in range(30)]
    bars: common.BarSeries = {"TESTUSDT": (ends, [99.0] * 30)}
    rows = v4_shared.weighted_cell_metrics(
        panel, series, bars,
        midpoint_ts_ms=BASE + 24 * MS_PER_HOUR,
        start_day_ms=common.utc_day_ms(BASE),
        end_day_ms=common.utc_day_ms(BASE + 24 * MS_PER_HOUR),
    )
    full, early, late = rows
    baseline_net = float(panel["net_return"].sum())
    # Linearity: modified net = baseline net + removed_net_delta (delta > 0 means
    # the removal helped), exactly as the grid columns are read.
    assert full["net_return"] == pytest.approx(baseline_net + full["removed_net_delta"])
    assert full["trades_kept"] == 2
    assert full["trades_removed"] == 1
    assert full["trades_downweighted"] == 1
    assert full["removed_cost_saved"] == pytest.approx(0.0001 * 1.5)
    # Trade c (weight 0) is the only late-era trade; its removal is era-attributed.
    assert early["trades_removed"] == 0
    assert late["trades_removed"] == 1
    assert late["removed_net_delta"] == pytest.approx(-0.0001)


def _tf_trade() -> dict[str, object]:
    return {
        "trade_id": "t1",
        "symbol": "TESTUSDT",
        "entry_ts_ms": BASE,
        "exit_ts_ms": BASE + 24 * MS_PER_HOUR,
        "entry_price": 100.0,
        "take_profit_price": 88.0,
        "exit_reason": "max_hold",
        "notional_weight": 0.01,
        "cost_return": -0.0001,
        "funding_mode": "modeled",
    }


def _tf_ohlc(lows: list[float], closes: list[float]) -> dict[str, tuple[list[int], ...]]:
    n = len(closes)
    ends = [BASE + i * MS_PER_HOUR for i in range(n)]
    highs = [c * 1.001 for c in closes]
    return {"TESTUSDT": (ends, highs, lows, closes)}


def test_tf_tp_touch_beats_giveback_same_bar() -> None:
    closes = [100.0] * 30
    lows = [99.0] * 30
    lows[3] = 87.0  # touches TP 88 on the same bar a giveback close would trigger
    closes[3] = 99.5
    ohlc = _tf_ohlc(lows, closes)
    out = walk_exit(_tf_trade(), ohlc, rule="giveback", arm_pct=0.04, retain_frac=0.5)
    assert out["exit_reason"] == "take_profit"
    assert out["exit_price"] == pytest.approx(88.0)


def test_tf_giveback_arms_and_exits_at_close() -> None:
    closes = [100.0] * 30
    lows = [99.0] * 30
    lows[2] = 94.0   # MFE reaches 6% intrabar
    closes[2] = 98.0  # close return 2% <= 0.5 * 6% -> giveback fires on the arming bar
    ohlc = _tf_ohlc(lows, closes)
    out = walk_exit(_tf_trade(), ohlc, rule="giveback", arm_pct=0.06, retain_frac=0.5)
    assert out["exit_reason"] == "mfe_giveback"
    assert out["exit_ts_ms"] == BASE + 2 * MS_PER_HOUR
    assert out["exit_price"] == pytest.approx(98.0)
    assert out["mfe"] == pytest.approx(0.06)
    # Higher arm: never triggers; runs to the 24h boundary close.
    out2 = walk_exit(_tf_trade(), ohlc, rule="giveback", arm_pct=0.08, retain_frac=0.5)
    assert out2["exit_reason"] == "max_hold"
    assert out2["exit_ts_ms"] == BASE + 24 * MS_PER_HOUR


def test_tf_breakeven_sticky_arm() -> None:
    closes = [99.5] * 30   # profitable closes: no exit while armed and positive
    lows = [99.0] * 30
    lows[2] = 95.0     # arms at 5% MFE
    closes[2] = 96.0   # still profitable at close
    closes[5] = 100.5  # close return dips to -0.5% -> breakeven stop
    ohlc = _tf_ohlc(lows, closes)
    out = walk_exit(_tf_trade(), ohlc, rule="breakeven", arm_pct=0.04, retain_frac=None)
    assert out["exit_reason"] == "breakeven_stop"
    assert out["exit_ts_ms"] == BASE + 5 * MS_PER_HOUR
    assert out["exit_price"] == pytest.approx(100.5)


def test_th_rank_transform_and_ridge_sign() -> None:
    y = np.array([3.0, -1.0, 2.0, 0.0])
    ranks = rank_transform(y)
    assert ranks.min() == pytest.approx(-0.5)
    assert ranks.max() == pytest.approx(0.5)
    assert list(np.argsort(ranks)) == list(np.argsort(y))
    rng = np.random.default_rng(7)
    x1 = rng.normal(size=400)
    x = np.column_stack([x1, np.ones(400)])
    beta = fit_ridge(x, 2.0 * x1 + rng.normal(scale=0.1, size=400))
    assert beta[0] > 1.5


def test_th_quarterly_refits() -> None:
    import datetime as dt

    refits = quarterly_refits(dt.date(2022, 7, 1), dt.date(2023, 4, 1))
    assert [common.iso_date(r) for r in refits] == ["2022-07-01", "2022-10-01", "2023-01-01"]


def test_ti_member_weights() -> None:
    for member in ("binary_gate", "linear", "two_sided"):
        assert member_weight(member, None) == 0.0  # fail closed
    assert member_weight("baseline", None) == 1.0
    assert member_weight("binary_gate", 1e-9) == 1.0
    assert member_weight("binary_gate", 0.0) == 0.0
    assert member_weight("linear", 0.05) == pytest.approx(0.5)
    assert member_weight("linear", 0.25) == 1.0
    assert member_weight("linear", -0.01) == 0.0
    assert member_weight("two_sided", 0.10) == 1.0
    assert member_weight("two_sided", -0.10) == 0.25
    assert member_weight("two_sided", 0.0) == 0.5
