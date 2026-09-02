"""Daily-panel backtester for cross-sectional and time-series rules.

A signal known at the close of day D sets the weight held over day D+1
(close to close, ``lag`` days after the decision). Costs are one-way turnover
times the per-side fee. Funding: a long pays the day's summed settlement rate.
Book returns are simple daily returns; equity compounds. Annualisation uses
365 days.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.core._common import MS_PER_DAY

#: Measured per-side cost (fee plus slippage) the desk uses, as a fraction of notional.
FEE_PER_SIDE = 7.78e-4

DAYS_PER_YEAR = 365


def universe_mask(
    p: pl.DataFrame,
    top: int = 100,
    min_age: int = 30,
    min_adv: float = 2e6,
    exclude: tuple[str, ...] = (),
) -> pl.Expr:
    """Row filter for the tradeable names: liquid enough, old enough, with a return."""
    m = (
        (pl.col("adv_rank") <= top)
        & (pl.col("age_days") >= min_age)
        & (pl.col("adv_30") >= min_adv)
        & pl.col("ret").is_not_null()
    )
    if exclude:
        m = m & ~pl.col("symbol").is_in(list(exclude))
    return m


def to_wide(p: pl.DataFrame, col: str) -> pl.DataFrame:
    return p.pivot(on="symbol", index="day", values=col, aggregate_function="first").sort("day")


class Panel:
    """Wide matrices (days x symbols) for numpy backtests over the daily panel."""

    def __init__(self, p: pl.DataFrame):
        p = p.sort(["day", "symbol"])
        self.days: np.ndarray = np.array(sorted(p["day"].unique().to_list()))
        self.symbols: np.ndarray = np.array(sorted(p["symbol"].unique().to_list()))
        day_index = {d: i for i, d in enumerate(self.days)}
        symbol_index = {s: i for i, s in enumerate(self.symbols)}
        n, m = len(self.days), len(self.symbols)
        rows = np.array([day_index[d] for d in p["day"].to_list()])
        cols = np.array([symbol_index[s] for s in p["symbol"].to_list()])
        self.rows, self.cols = rows, cols

        def mat(col: str, fill: float = np.nan) -> np.ndarray:
            a = np.full((n, m), fill)
            a[rows, cols] = p[col].to_numpy()
            return a

        self.ret = mat("ret")
        self.close = mat("close")
        self.high = mat("high")
        self.low = mat("low")
        self.open = mat("open")
        self.funding = mat("funding_day", 0.0)
        self.adv = mat("adv_30")
        self.rv30 = mat("rv_30")
        self.rv7 = mat("rv_7")
        self.rv90 = mat("rv_90")
        self.age = mat("age_days", 0)
        self.rank = mat("adv_rank")
        self.oi = mat("oi_value")
        self.prem = mat("premium_mean")
        self.n, self.m = n, m

    def universe(self, top: int = 100, min_age: int = 30, min_adv: float = 2e6) -> np.ndarray:
        return (self.rank <= top) & (self.age >= min_age) & (self.adv >= min_adv) & ~np.isnan(self.ret)


def trailing_return(close: np.ndarray, lookback: int, skip: int = 0) -> np.ndarray:
    """close[t-skip] / close[t-skip-lookback] - 1, NaN where either is missing."""
    out = np.full_like(close, np.nan)
    span = lookback + skip
    n = len(close)
    num = close[span - skip: n - skip] if skip > 0 else close[span:]
    den = close[: n - span]
    out[span:] = num / den - 1
    return out


def ema(x: np.ndarray, span: int) -> np.ndarray:
    """Column-wise exponential moving average over rows; a NaN input carries the previous value."""
    a = 2.0 / (span + 1)
    out = np.full_like(x, np.nan)
    prev = np.full(x.shape[1], np.nan)
    for t in range(x.shape[0]):
        row = x[t]
        new = np.where(np.isnan(prev), row, a * row + (1 - a) * prev)
        new = np.where(np.isnan(row), prev, new)
        out[t] = new
        prev = new
    return out


def run_book(
    P: Panel,
    weights: np.ndarray,
    lag: int = 1,
    fee: float = FEE_PER_SIDE,
    funding: bool = True,
) -> dict[str, Any]:
    """weights[t] is the target weight decided at the close of day t.

    The weight earns ret[t+lag]. Turnover is charged when the held weight
    changes. Returns the daily series: net, gross, fund, cost, turnover,
    gross_exp, n_pos, and the held weights.
    """
    w = np.nan_to_num(weights, nan=0.0)
    w_held = np.zeros_like(w)
    if lag == 0:
        w_held[:] = w
    else:
        w_held[lag:] = w[:-lag]
    r = np.nan_to_num(P.ret, nan=0.0)
    f = np.nan_to_num(P.funding, nan=0.0)
    gross_pnl = (w_held * r).sum(axis=1)
    fund_pnl = -(w_held * f).sum(axis=1) if funding else np.zeros(P.n)
    turnover = np.abs(np.diff(w_held, axis=0, prepend=np.zeros((1, P.m)))).sum(axis=1)
    cost = turnover * fee
    net = gross_pnl + fund_pnl - cost
    return dict(
        net=net, gross=gross_pnl, fund=fund_pnl, cost=cost, turnover=turnover,
        gross_exp=np.abs(w_held).sum(axis=1), n_pos=(w_held != 0).sum(axis=1), w_held=w_held,
    )


def stats(net: np.ndarray, days: np.ndarray | None = None, active_only: bool = False) -> dict[str, Any]:
    """Summary of a daily return series; only ``n`` when fewer than ten days."""
    x = np.asarray(net, dtype=float).copy()
    if active_only:
        x = x[x != 0]
    if len(x) < 10:
        return dict(n=len(x))
    mu, sd = x.mean(), x.std(ddof=1)
    eq = np.cumprod(1 + x)
    dd = eq / np.maximum.accumulate(eq) - 1
    yrs = len(x) / DAYS_PER_YEAR
    return dict(
        n=len(x),
        ann_ret=(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else -1.0,
        ann_vol=sd * np.sqrt(DAYS_PER_YEAR),
        sharpe=mu / sd * np.sqrt(DAYS_PER_YEAR) if sd > 0 else np.nan,
        t=mu / sd * np.sqrt(len(x)) if sd > 0 else np.nan,
        maxdd=dd.min(),
        worst_day=x.min(),
        total=eq[-1] - 1,
    )


def years_of(days_ms: np.ndarray) -> np.ndarray:
    """UTC calendar year of each millisecond day stamp."""
    return np.asarray(days_ms, dtype="int64").astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970


def by_year(net: np.ndarray, days: np.ndarray, min_days: int = 20) -> pl.DataFrame:
    """One row per calendar year with at least ``min_days`` days: days, ann_ret, sharpe, maxdd."""
    yrs = years_of(days)
    rows = []
    for y in sorted(set(yrs.tolist())):
        x = net[yrs == y]
        if len(x) < min_days:
            continue
        s = stats(x)
        rows.append(dict(year=int(y), days=len(x), ann_ret=s["ann_ret"], sharpe=s["sharpe"], maxdd=s["maxdd"]))
    return pl.DataFrame(rows)


def fmt(s: dict[str, Any]) -> str:
    if "sharpe" not in s:
        return f"n={s.get('n')}"
    return (
        f"ret {s['ann_ret'] * 100:6.1f}%/yr  vol {s['ann_vol'] * 100:5.1f}%  Sharpe {s['sharpe']:5.2f}  "
        f"t {s['t']:5.2f}  maxDD {s['maxdd'] * 100:6.1f}%  worst {s['worst_day'] * 100:6.1f}%  "
        f"total {s['total'] * 100:7.1f}%"
    )


def vol_target(
    net: np.ndarray,
    target: float = 0.15,
    window: int = 30,
    lo: float = 0.2,
    hi: float = 2.0,
    lag: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """Scale each day's return by target / trailing realised vol, known ``lag`` days before.

    The scale is clipped to [lo, hi] and is 1.0 where the trailing vol is not
    yet defined. Returns the scaled series and the scale applied.
    """
    s = pl.Series(net).rolling_std(window, min_samples=window // 2).to_numpy() * np.sqrt(DAYS_PER_YEAR)
    scale = np.clip(target / np.where(np.isnan(s) | (s == 0), np.nan, s), lo, hi)
    scale = np.nan_to_num(scale, nan=1.0)
    sc = np.ones_like(net)
    sc[lag:] = scale[:-lag]
    return net * sc, sc


def xs_weights(
    signal: np.ndarray,
    univ: np.ndarray,
    q: float = 0.2,
    long_only: bool = False,
    inv_vol: np.ndarray | None = None,
    rebalance_every: int = 1,
    gross_side: float = 1.0,
    min_names: int = 10,
) -> np.ndarray:
    """Top-q long, bottom-q short by signal within the universe.

    Each side sums to ``gross_side``, equal-weighted or, with ``inv_vol`` (a
    volatility matrix), weighted by one over the volatility. A day with fewer
    than ``min_names`` scored names holds nothing, and days that skip the
    rebalance copy the last day that was scored.
    """
    n, m = signal.shape
    w = np.zeros((n, m))
    last: np.ndarray | None = None
    for t in range(n):
        if rebalance_every > 1 and t % rebalance_every != 0 and last is not None:
            w[t] = last
            continue
        s = np.where(univ[t], signal[t], np.nan)
        ok = ~np.isnan(s)
        k = int(ok.sum())
        if k < min_names:
            last = w[t]
            continue
        order = np.argsort(s[ok])
        idx = np.where(ok)[0][order]
        nq = max(1, int(round(k * q)))
        longs, shorts = idx[-nq:], idx[:nq]
        base = np.ones(m) if inv_vol is None else 1.0 / np.clip(np.nan_to_num(inv_vol[t], nan=np.inf), 1e-3, None)
        wl = base[longs]
        w[t, longs] = wl / wl.sum() * gross_side
        if not long_only:
            ws = base[shorts]
            w[t, shorts] = -ws / ws.sum() * gross_side
        last = w[t]
    return w


__all__ = [
    "DAYS_PER_YEAR", "FEE_PER_SIDE", "MS_PER_DAY", "Panel", "by_year", "ema", "fmt", "run_book",
    "stats", "to_wide", "trailing_return", "universe_mask", "vol_target", "xs_weights", "years_of",
]
