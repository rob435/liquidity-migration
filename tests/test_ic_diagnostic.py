"""Tests for the information-coefficient diagnostic.

The load-bearing test is `test_ic_is_per_day_not_pooled`: it constructs a panel
whose per-day cross-sectional IC is +1.0 on every day but whose *pooled*
correlation is strongly negative. A correct tool reports +1.0. A tool that
pools observations across days (the bug that produced the impossible +0.176
composite IC) would report a negative number. This is the regression guard.
"""
from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from liquidity_migration.ic_diagnostic import (
    add_forward_short_returns,
    cross_sectional_ic,
    ic_table,
    spearman,
)

MS_PER_HOUR = 3_600_000


# --------------------------------------------------------------------------
# Spearman primitive
# --------------------------------------------------------------------------

def test_spearman_perfect_positive():
    assert spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)


def test_spearman_perfect_negative():
    assert spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_handles_ties():
    # y has a tied pair; average-rank Spearman must stay finite and < 1.
    ic = spearman([1, 2, 3, 4], [10, 20, 20, 40])
    assert not math.isnan(ic)
    assert 0.0 < ic < 1.0


def test_spearman_zero_variance_is_nan():
    assert math.isnan(spearman([1, 2, 3], [5, 5, 5]))


# --------------------------------------------------------------------------
# Cross-sectional IC — the core fix
# --------------------------------------------------------------------------

def test_cross_sectional_ic_perfect_signal():
    # 3 days, 12 names each; signal ranks the forward return perfectly each day.
    rows = []
    for d in range(3):
        for i in range(12):
            rows.append({"date": f"2024-01-0{d+1}", "sig": float(i),
                         "fwd": float(i) * 2.0})
    res = cross_sectional_ic(pl.DataFrame(rows), "sig", "fwd", min_names=10)
    assert res.n_days == 3
    assert res.mean_ic == pytest.approx(1.0)
    assert res.hit_rate == pytest.approx(1.0)


def test_ic_is_per_day_not_pooled():
    # Per-day IC is +1.0 on both days. But day 2 has much HIGHER signal values
    # and much LOWER forward returns than day 1, so a pooled correlation is
    # strongly negative. The correct per-day tool must report +1.0.
    rows = []
    for i in range(10):                       # day 1: signal & fwd both low
        rows.append({"date": "2024-01-01", "sig": float(i), "fwd": float(i)})
    for i in range(10):                       # day 2: signal high, fwd low
        rows.append({"date": "2024-01-02", "sig": 100.0 + i, "fwd": -100.0 + i})
    panel = pl.DataFrame(rows)

    res = cross_sectional_ic(panel, "sig", "fwd", min_names=10)
    assert res.n_days == 2
    assert res.mean_ic == pytest.approx(1.0)        # per-day skill is perfect

    pooled = spearman(panel["sig"].to_list(), panel["fwd"].to_list())
    assert pooled < -0.5                            # pooling would say the opposite


def test_cross_sectional_ic_min_names_filter():
    # Two valid days (>=10 names) and one thin day (5 names) that must be dropped.
    rows = []
    for i in range(12):
        rows.append({"date": "2024-01-01", "sig": float(i), "fwd": float(i)})
    for i in range(11):
        rows.append({"date": "2024-01-02", "sig": float(i), "fwd": float(i)})
    for i in range(5):
        rows.append({"date": "2024-01-03", "sig": float(i), "fwd": float(i)})
    res = cross_sectional_ic(pl.DataFrame(rows), "sig", "fwd", min_names=10)
    assert res.n_days == 2          # thin day excluded
    assert res.n_obs == 23          # 12 + 11, not 28


def test_cross_sectional_ic_zero_for_independent_signal():
    # 40 days, 20 names; signal and forward drawn independently. Mean daily IC
    # must sit near zero — a property pooling can destroy under a regime drift.
    rng = np.random.default_rng(20260520)
    rows = []
    for d in range(40):
        regime = float(d)  # a strong between-day drift in BOTH series
        for _ in range(20):
            rows.append({
                "date": f"d{d:03d}",
                "sig": regime + rng.normal(),
                "fwd": regime + rng.normal(),   # independent of sig within day
            })
    res = cross_sectional_ic(pl.DataFrame(rows), "sig", "fwd", min_names=10)
    assert abs(res.mean_ic) < 0.10              # per-day: no skill
    pooled = spearman([r["sig"] for r in rows], [r["fwd"] for r in rows])
    assert pooled > 0.5                          # pooled: fooled by the drift


def test_composite_ic_respects_averaging_bound():
    # Four noisy components of one latent signal. Their equal-weight mean (the
    # composite) may beat each component, but a per-day IC cannot exceed ~2x
    # (sqrt(4)) the best component. The +0.176 bug was ~4x — impossible.
    rng = np.random.default_rng(11)
    rows = []
    for d in range(60):
        for _ in range(25):
            latent = rng.normal()
            fwd = latent + 1.5 * rng.normal()        # noisy realised return
            comps = [latent + 2.0 * rng.normal() for _ in range(4)]
            rows.append({
                "date": f"d{d:03d}", "fwd": fwd,
                "c0": comps[0], "c1": comps[1], "c2": comps[2], "c3": comps[3],
                "composite": sum(comps) / 4.0,
            })
    panel = pl.DataFrame(rows)
    tbl = ic_table(panel, ["c0", "c1", "c2", "c3", "composite"], "fwd", min_names=10)
    by = {r["signal"]: r["mean_ic"] for r in tbl.iter_rows(named=True)}
    best_component = max(by["c0"], by["c1"], by["c2"], by["c3"])
    # composite beats the components (averaging cancels independent noise) ...
    assert by["composite"] > best_component
    # ... but stays inside the sqrt(k) ceiling — never the 4x of the bug.
    assert by["composite"] < 2.0 * best_component


# --------------------------------------------------------------------------
# Forward short returns
# --------------------------------------------------------------------------

def _hourly(symbol: str, start_day: str, hours: int, price_fn) -> pl.DataFrame:
    start_ms = int(pl.Series([start_day]).str.to_datetime().dt.timestamp("ms")[0])
    rows = []
    for i in range(hours):
        c = price_fn(i)
        rows.append({"ts_ms": start_ms + i * MS_PER_HOUR, "symbol": symbol,
                     "open": c, "high": c * 1.01, "low": c * 0.99, "close": c})
    return pl.DataFrame(rows)


def test_forward_short_return_sign_and_magnitude():
    # `date` is the signal-close moment; with entry_delay_hours=1 the entry bar
    # is hour 1. 1d exit = hour 25, 2d exit = hour 49.
    def price_fn(i):
        if i < 25:
            return 100.0
        if i < 49:
            return 90.0
        return 80.0
    klines = _hourly("AAAUSDT", "2024-01-01", 120, price_fn)
    panel = pl.DataFrame([{"date": "2024-01-01", "symbol": "AAAUSDT"}])
    out = add_forward_short_returns(panel, klines, [1, 2], entry_delay_hours=1)
    row = out.to_dicts()[0]
    # entry open at hour 1 = 100; 1d exit close at hour 25 = 90; 2d at hour 49 = 80
    assert row["fwd_short_return_1d"] == pytest.approx((100.0 - 90.0) / 100.0)
    assert row["fwd_short_return_2d"] == pytest.approx((100.0 - 80.0) / 100.0)


def test_forward_short_return_null_when_data_too_short():
    klines = _hourly("AAAUSDT", "2024-01-01", 20, price_fn=lambda i: 100.0)
    panel = pl.DataFrame([{"date": "2024-01-01", "symbol": "AAAUSDT"}])
    out = add_forward_short_returns(panel, klines, [1], entry_delay_hours=1)
    # entry at hour 1, 1d exit would need hour 25 — only 20 bars exist.
    assert out.to_dicts()[0]["fwd_short_return_1d"] is None
