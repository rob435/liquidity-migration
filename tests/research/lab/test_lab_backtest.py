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
    # a missing (day, symbol) cell reads NaN for returns and for funding
    P2 = Panel(frame.filter(~((pl.col("symbol") == "BBB") & (pl.col("day") == T0 + 2 * DAY))))
    assert math.isnan(P2.ret[2, 1]) and math.isnan(P2.funding[2, 1])
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
    # Hand-computed: the target change plus the rebalance back from the previous
    # day's price drift. Days 2..5 hold one fully-invested long, which drifts
    # nowhere: a 100% weight stays 100% whatever the price does.
    np.testing.assert_allclose(
        r["turnover"],
        [
            0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.5,
            0.007389162561576346, 0.007389162561576457, 2.007389162561576,
            0.05457501161170453, 0.059329920893438914,
        ],
    )
    np.testing.assert_allclose(r["cost"], r["turnover"] * FEE_PER_SIDE)
    # day 6: long AAA earns ret_a, short BBB earns -0.5 ret_b; funding is paid by the long, received by the short
    assert r["gross"][6] == pytest.approx(ret_a[6] - 0.5 * ret_b[6])
    assert r["fund"][6] == pytest.approx(-(1.0 * 0.001 + (-0.5) * -0.002))
    np.testing.assert_allclose(r["net"], r["gross"] + r["fund"] - r["cost"])
    assert r["gross_exp"][9] == 1.5 and r["n_pos"][9] == 2 and r["n_pos"][0] == 0
    assert run_book(P, w, lag=1, funding=False)["fund"].tolist() == [0.0] * 12


def _two_day_panel(returns: list[tuple[float, float]]) -> Panel:
    rows = []
    for d, (ra, rb) in enumerate(returns):
        for sym, r in (("AAA", ra), ("BBB", rb)):
            rows.append(
                dict(symbol=sym, day=T0 + d * DAY, ret=r, close=100.0, high=100.0, low=100.0, open=100.0,
                     funding_day=0.0, adv_30=1e7, rv_30=0.02, rv_7=0.02, rv_90=0.02, age_days=d + 1,
                     adv_rank=1.0, oi_value=1.0, premium_mean=0.0)
            )
    return Panel(pl.DataFrame(rows))


def test_run_book_charges_the_rebalance_back_from_price_drift() -> None:
    # 50/50 in two names, AAA doubles on day 0, the day 1 target is unchanged:
    # the book opens day 1 at 2/3 and 1/3, so the rebalance trades a third of it.
    P = _two_day_panel([(1.0, 0.0), (0.0, 0.0)])
    r = run_book(P, np.full((2, 2), 0.5), lag=0, funding=False)
    assert r["turnover"][0] == pytest.approx(1.0)
    assert r["turnover"][1] == pytest.approx(1 / 3)
    assert r["cost"][1] == pytest.approx(2.593333333e-4)


def test_run_book_drift_does_not_divide_by_a_wiped_out_book() -> None:
    P = _two_day_panel([(-1.0, -1.0), (0.0, 0.0)])
    r = run_book(P, np.full((2, 2), 0.5), lag=0, funding=False)
    assert r["turnover"].tolist() == [1.0, 0.0]


def test_run_book_reports_the_weight_standing_on_missing_cells() -> None:
    frame, ret_a, _ = _panel()
    P = Panel(frame.filter(~((pl.col("symbol") == "BBB") & (pl.col("day") == T0 + 4 * DAY))))
    w = np.zeros((12, 2))
    w[:, 0] = 1.0
    w[:, 1] = -0.5
    r = run_book(P, w, lag=1)
    assert r["missing_ret_exp"][4] == pytest.approx(0.5)
    assert r["missing_fund_exp"][4] == pytest.approx(0.5)
    assert r["missing_ret_exp"][3] == 0.0 and r["missing_fund_exp"][3] == 0.0
    # the missing cell still earns and pays zero, it is not dropped from the book
    assert r["gross"][4] == pytest.approx(ret_a[4])
    assert r["fund"][4] == pytest.approx(-0.001)


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


def test_maxdd_measures_from_the_initial_capital() -> None:
    # A book that loses a tenth on day one and never moves again is 10% down,
    # not flat: the peak starts at the capital, before the first return.
    x = np.concatenate(([-0.1], np.zeros(11)))
    assert stats(x)["maxdd"] == pytest.approx(-0.1)
    assert stats(np.concatenate(([0.2], [-0.1], np.zeros(10))))["maxdd"] == pytest.approx(-0.1)


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


def test_vol_target_with_no_lag_scales_the_day_its_vol_was_measured_on() -> None:
    noisy = np.tile([0.1, -0.1], 30)
    scaled, sc = vol_target(noisy, target=0.15, window=10, lo=0.2, hi=2.0, lag=0)
    assert sc[:4].tolist() == [1.0] * 4  # the trailing vol needs five days
    assert sc[4:].tolist() == [0.2] * 56
    np.testing.assert_allclose(scaled, noisy * sc)
    # lag pushes the same scale one day later
    assert vol_target(noisy, target=0.15, window=10, lag=1)[1][1:].tolist() == sc[:-1].tolist()


def test_universe_mask_filters_on_rank_age_liquidity_and_return() -> None:
    frame = pl.DataFrame(
        dict(symbol=["A", "B", "C", "D", "E"], adv_rank=[1.0, 2.0, 200.0, 3.0, 4.0], age_days=[40, 10, 40, 40, 40],
             adv_30=[5e6, 5e6, 5e6, 1e6, 5e6], ret=[0.0, 0.0, 0.0, 0.0, None])
    )
    assert frame.filter(universe_mask(frame))["symbol"].to_list() == ["A"]
    assert frame.filter(universe_mask(frame, min_age=1))["symbol"].to_list() == ["A", "B"]
    assert frame.filter(universe_mask(frame, min_age=1, exclude=("A",)))["symbol"].to_list() == ["B"]
