from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from liquidity_migration.research.lab.backtest import (
    FEE_PER_SIDE,
    Panel,
    by_year,
    ema,
    fmt,
    run_book,
    stats,
    trailing_return,
    universe_mask,
    vol_target,
    xs_weights,
    years_of,
)

DAY = 86_400_000
T0 = 1_704_067_200_000  # 2024-01-01


def _panel(n_days: int = 12) -> tuple[pl.DataFrame, np.ndarray, np.ndarray]:
    ret_a = np.linspace(0.01, 0.02, n_days)
    ret_b = np.linspace(-0.01, 0.01, n_days)
    rows = []
    for d in range(n_days):
        for sym, r, f in (("AAA", ret_a[d], 0.001), ("BBB", ret_b[d], -0.002)):
            rows.append(
                dict(symbol=sym, day=T0 + d * DAY, ret=float(r), close=100.0 + d, high=101.0 + d, low=99.0 + d,
                     open=100.0 + d, funding_day=f, adv_30=1e7, rv_30=0.02, rv_7=0.03, rv_90=0.02, age_days=d + 1,
                     adv_rank=1.0 if sym == "AAA" else 2.0, oi_value=1.0, premium_mean=0.0)
            )
    return pl.DataFrame(rows), ret_a, ret_b


def test_panel_matrices_are_days_by_symbols() -> None:
    frame, ret_a, ret_b = _panel()
    P = Panel(frame)
    assert P.n == 12 and P.m == 2
    assert list(P.symbols) == ["AAA", "BBB"]
    np.testing.assert_allclose(P.ret[:, 0], ret_a)
    np.testing.assert_allclose(P.ret[:, 1], ret_b)
    assert P.funding[3, 1] == -0.002
    # a missing (day, symbol) cell reads NaN for returns and 0 for funding
    P2 = Panel(frame.filter(~((pl.col("symbol") == "BBB") & (pl.col("day") == T0 + 2 * DAY))))
    assert math.isnan(P2.ret[2, 1]) and P2.funding[2, 1] == 0.0
    assert P.universe(top=1, min_age=1).sum() == 12
    assert P.universe(top=1).sum() == 0  # the default asks for 30 days of history


def test_run_book_charges_turnover_and_funding_one_day_after_the_decision() -> None:
    frame, ret_a, ret_b = _panel()
    P = Panel(frame)
    w = np.zeros((12, 2))
    w[:, 0] = 1.0  # long AAA from the first close
    w[5:, 1] = -0.5  # short half a unit of BBB from day 5's close
    w[8:, 0] = -1.0  # flip AAA at day 8's close
    r = run_book(P, w, lag=1)
    held = r["w_held"]
    assert held[0].tolist() == [0.0, 0.0]
    assert held[1].tolist() == [1.0, 0.0]
    assert held[6].tolist() == [1.0, -0.5]
    assert held[9].tolist() == [-1.0, -0.5]
    np.testing.assert_allclose(r["turnover"], [0, 1, 0, 0, 0, 0, 0.5, 0, 0, 2, 0, 0])
    np.testing.assert_allclose(r["cost"], r["turnover"] * FEE_PER_SIDE)
    # day 6: long AAA earns ret_a, short BBB earns -0.5 ret_b; funding is paid by the long, received by the short
    assert r["gross"][6] == pytest.approx(ret_a[6] - 0.5 * ret_b[6])
    assert r["fund"][6] == pytest.approx(-(1.0 * 0.001 + (-0.5) * -0.002))
    np.testing.assert_allclose(r["net"], r["gross"] + r["fund"] - r["cost"])
    assert r["gross_exp"][9] == 1.5 and r["n_pos"][9] == 2 and r["n_pos"][0] == 0
    assert run_book(P, w, lag=1, funding=False)["fund"].tolist() == [0.0] * 12


def test_run_book_lag_zero_holds_the_decision_day_and_lag_two_shifts_twice() -> None:
    frame, _, _ = _panel()
    P = Panel(frame)
    w = np.zeros((12, 2))
    w[3:, 0] = 1.0
    assert run_book(P, w, lag=0)["w_held"][3, 0] == 1.0
    held2 = run_book(P, w, lag=2)["w_held"]
    assert held2[4, 0] == 0.0 and held2[5, 0] == 1.0


def test_stats_on_a_known_alternating_series() -> None:
    x = np.array([0.01, -0.005] * 6)
    s = stats(x)
    mu, sd = x.mean(), x.std(ddof=1)
    eq = np.cumprod(1 + x)
    assert s["n"] == 12
    assert s["sharpe"] == pytest.approx(mu / sd * math.sqrt(365))
    assert s["t"] == pytest.approx(mu / sd * math.sqrt(12))
    assert s["ann_vol"] == pytest.approx(sd * math.sqrt(365))
    assert s["total"] == pytest.approx(eq[-1] - 1)
    assert s["ann_ret"] == pytest.approx(eq[-1] ** (365 / 12) - 1)
    assert s["maxdd"] == pytest.approx(-0.005)
    assert s["worst_day"] == -0.005
    assert stats(np.zeros(5)) == {"n": 5}
    assert stats(np.concatenate([x, np.zeros(30)]), active_only=True)["n"] == 12
    flat = stats(np.zeros(12))
    assert math.isnan(flat["sharpe"]) and math.isnan(flat["t"]) and flat["total"] == 0.0
    assert "Sharpe" in fmt(s) and fmt({"n": 5}) == "n=5"


def test_years_of_and_by_year_split_on_utc_calendar_years() -> None:
    start = 1_701_388_800_000  # 2023-12-01
    days = start + DAY * np.arange(31 + 31 + 15)  # 31 days of 2023, 31 of January 2024, 15 of February
    assert years_of(days[:31]).tolist() == [2023] * 31
    assert years_of(days[31:]).tolist() == [2024] * 46
    net = np.where(years_of(days) == 2023, 0.001, -0.001) + np.tile([0.0005, -0.0005], 39)[:77]
    table = by_year(net, days)
    assert table["year"].to_list() == [2023, 2024]
    assert table["days"].to_list() == [31, 46]
    assert table["ann_ret"][0] > 0 > table["ann_ret"][1]
    assert by_year(net[:31], days[:31], min_days=40).height == 0


def test_trailing_return_with_and_without_skip() -> None:
    close = np.array([[1.0], [2.0], [4.0], [8.0], [16.0]])
    np.testing.assert_allclose(trailing_return(close, 1)[1:, 0], [1, 1, 1, 1])
    assert np.isnan(trailing_return(close, 1)[0, 0])
    np.testing.assert_allclose(trailing_return(close, 2)[2:, 0], [3, 3, 3])
    skipped = trailing_return(close, 1, skip=1)
    assert np.isnan(skipped[:2, 0]).all()
    np.testing.assert_allclose(skipped[2:, 0], [1, 1, 1])


def test_ema_carries_the_previous_value_through_a_gap() -> None:
    x = np.array([[1.0, 10.0], [np.nan, 30.0], [3.0, 30.0]])
    out = ema(x, span=3)
    np.testing.assert_allclose(out[:, 0], [1.0, 1.0, 2.0])
    np.testing.assert_allclose(out[:, 1], [10.0, 20.0, 25.0])


def test_xs_weights_quantiles_long_only_and_inverse_vol() -> None:
    signal = np.arange(12, dtype=float)[None, :].repeat(3, axis=0)
    univ = np.ones((3, 12), dtype=bool)
    w = xs_weights(signal, univ, q=0.25)
    np.testing.assert_allclose(w[0, 9:], 1 / 3)
    np.testing.assert_allclose(w[0, :3], -1 / 3)
    assert w[0, 3:9].tolist() == [0.0] * 6
    lo = xs_weights(signal, univ, q=0.25, long_only=True, gross_side=2.0)
    assert lo[0].sum() == pytest.approx(2.0) and (lo[0] >= 0).all()
    vol = np.full((3, 12), 1.0)
    vol[:, 11] = 3.0
    iv = xs_weights(signal, univ, q=0.25, inv_vol=vol)
    assert iv[0, 9] == pytest.approx(iv[0, 10]) and iv[0, 11] == pytest.approx(iv[0, 9] / 3)
    # too few names: the day holds nothing, and a skipped rebalance after it copies that
    thin = univ.copy()
    thin[1, :5] = False
    w2 = xs_weights(signal, thin, q=0.25)
    assert w2[1].tolist() == [0.0] * 12 and w2[2].tolist() == w[0].tolist()
    thin3 = univ.copy()
    thin3[0, :5] = False
    assert xs_weights(signal, thin3, q=0.25, rebalance_every=2)[1].tolist() == [0.0] * 12
    # rebalance every second day copies the previous row even when the signal moved
    drift = signal.copy()
    drift[1] = drift[1][::-1]
    w3 = xs_weights(drift, univ, q=0.25, rebalance_every=2)
    np.testing.assert_allclose(w3[1], w3[0])


def test_vol_target_clips_and_lags_the_scale() -> None:
    n = 60
    noisy = np.tile([0.1, -0.1], n // 2)
    scaled, sc = vol_target(noisy, target=0.15, window=10, lo=0.2, hi=2.0, lag=1)
    assert sc[0] == 1.0
    assert sc[20:].tolist() == [0.2] * 40
    np.testing.assert_allclose(scaled, noisy * sc)
    quiet = np.tile([1e-4, -1e-4], n // 2)
    assert vol_target(quiet, window=10)[1][20:].tolist() == [2.0] * 40
    flat = np.zeros(n)
    assert vol_target(flat, window=10)[1].tolist() == [1.0] * n
    lagged = vol_target(noisy, window=10, lag=3)[1]
    assert lagged[:3].tolist() == [1.0] * 3


def test_universe_mask_filters_on_rank_age_liquidity_and_return() -> None:
    frame = pl.DataFrame(
        dict(symbol=["A", "B", "C", "D", "E"], adv_rank=[1.0, 2.0, 200.0, 3.0, 4.0], age_days=[40, 10, 40, 40, 40],
             adv_30=[5e6, 5e6, 5e6, 1e6, 5e6], ret=[0.0, 0.0, 0.0, 0.0, None])
    )
    assert frame.filter(universe_mask(frame))["symbol"].to_list() == ["A"]
    assert frame.filter(universe_mask(frame, min_age=1))["symbol"].to_list() == ["A", "B"]
    assert frame.filter(universe_mask(frame, min_age=1, exclude=("A",)))["symbol"].to_list() == ["B"]
