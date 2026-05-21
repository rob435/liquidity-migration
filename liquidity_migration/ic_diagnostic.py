"""Information-coefficient diagnostics for the reversion-alpha signals.

WHY THIS MODULE EXISTS
----------------------
The throwaway diagnostic in the ground-up rebuild printed a composite IC of
+0.176 on Binance OOS — roughly 4x its best single component (~0.04). An
equal-weight mean of k signals cannot have an IC more than ~sqrt(k) times its
best component (here sqrt(4) = 2x), so +0.176 was mathematically impossible and
flagged as a bug. The most likely cause: the composite IC was computed by
*pooling* every (symbol, day) observation into one correlation, while the
per-component ICs were computed correctly *per day*. Pooling lets a between-day
(regime) confound — days when both the signal and forward returns are high —
masquerade as cross-sectional skill.

THE FIX
-------
`cross_sectional_ic` computes the information coefficient the only defensible
way: a per-day cross-sectional Spearman correlation of the signal against the
forward return, restricted to days with >= `min_names` ranked names, then
averaged across days. The composite signal (`reversion_score`) is just another
column and goes through the *identical* path — there is no separate code path
for it, which is what structurally prevents the original bug from recurring.

`test_ic_diagnostic.py` pins this: a panel whose per-day IC is +1.0 every day
but whose pooled correlation is negative must report mean IC = +1.0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import math

import numpy as np
import polars as pl

from ._common import MS_PER_HOUR


# --------------------------------------------------------------------------
# Rank correlation
# --------------------------------------------------------------------------

def _rankdata(values: np.ndarray) -> np.ndarray:
    """Average ranks (1-based), tie-aware — equivalent to scipy.stats.rankdata."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(1, len(values) + 1, dtype=float)
    # average tied groups
    sorted_vals = values[order]
    i = 0
    n = len(values)
    while i < n:
        j = i + 1
        while j < n and sorted_vals[j] == sorted_vals[i]:
            j += 1
        if j - i > 1:
            avg = (i + 1 + j) / 2.0  # mean of 1-based ranks i+1 .. j
            ranks[order[i:j]] = avg
        i = j
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation of two equal-length vectors.

    Returns NaN when either vector has zero rank variance (all values tied)
    or fewer than two observations.
    """
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    if xa.shape != ya.shape or xa.size < 2:
        return float("nan")
    rx = _rankdata(xa)
    ry = _rankdata(ya)
    sx = rx.std()
    sy = ry.std()
    if sx <= 1e-12 or sy <= 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


# --------------------------------------------------------------------------
# Forward returns
# --------------------------------------------------------------------------

def _bar_arrays(klines: pl.DataFrame) -> dict[str, dict[str, np.ndarray | dict[int, int]]]:
    """Per-symbol sorted hourly bars: ts/open/close arrays + ts->index map."""
    out: dict[str, dict] = {}
    clean = klines.select(["symbol", "ts_ms", "open", "close"]).drop_nulls()
    for key, part in clean.partition_by("symbol", as_dict=True).items():
        sym = str(key[0] if isinstance(key, tuple) else key)
        part = part.sort("ts_ms")
        ts = part["ts_ms"].to_numpy()
        out[sym] = {
            "ts": ts,
            "open": part["open"].to_numpy(),
            "close": part["close"].to_numpy(),
            "pos": {int(t): i for i, t in enumerate(ts)},
        }
    return out


def add_forward_short_returns(
    panel: pl.DataFrame,
    klines: pl.DataFrame,
    horizons_days: Iterable[int],
    *,
    entry_delay_hours: int = 1,
) -> pl.DataFrame:
    """Attach `fwd_short_return_{h}d` columns to a signal panel.

    The entry timing mirrors `reversion_alpha.simulate`: a panel row's `date`
    is stamped at the signal-close moment, so entry is `entry_delay_hours` after
    `to_datetime(date)`. Entry price is that bar's open; the H-day exit price is
    the close H*24 hours later. The short return is (entry - exit) / entry —
    positive when the name falls, the sign convention every panel signal uses.

    Pairs with no executable entry bar or insufficient forward data are null.
    """
    horizons = sorted({int(h) for h in horizons_days})
    bars = _bar_arrays(klines)
    delay_ms = entry_delay_hours * MS_PER_HOUR

    day_ms = (
        panel.select(pl.col("date").str.to_datetime().dt.timestamp("ms"))
        .to_series()
        .to_list()
    )
    symbols = panel["symbol"].to_list()

    fwd: dict[int, list[float | None]] = {h: [] for h in horizons}
    for sym, dms in zip(symbols, day_ms):
        rec = bars.get(sym)
        if rec is None or dms is None:
            for h in horizons:
                fwd[h].append(None)
            continue
        entry_ts = int(dms) + delay_ms
        idx = rec["pos"].get(entry_ts)
        if idx is None:
            for h in horizons:
                fwd[h].append(None)
            continue
        entry_price = float(rec["open"][idx])
        closes = rec["close"]
        n = len(closes)
        for h in horizons:
            exit_idx = idx + h * 24
            if entry_price > 0.0 and exit_idx < n:
                exit_price = float(closes[exit_idx])
                fwd[h].append((entry_price - exit_price) / entry_price)
            else:
                fwd[h].append(None)

    return panel.with_columns(
        [pl.Series(f"fwd_short_return_{h}d", fwd[h], dtype=pl.Float64) for h in horizons]
    )


# --------------------------------------------------------------------------
# Cross-sectional IC
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ICResult:
    signal: str
    forward: str
    n_days: int          # days that cleared the min_names bar
    n_obs: int           # total (symbol, day) pairs scored
    mean_ic: float       # average daily cross-sectional Spearman IC
    ic_std: float        # sample std of the daily IC series
    t_stat: float        # mean_ic / (ic_std / sqrt(n_days))
    hit_rate: float      # fraction of days with IC > 0
    daily_ic: tuple[float, ...] = ()

    def as_row(self) -> dict[str, float | int | str]:
        return {
            "signal": self.signal,
            "forward": self.forward,
            "n_days": self.n_days,
            "n_obs": self.n_obs,
            "mean_ic": round(self.mean_ic, 4),
            "ic_std": round(self.ic_std, 4),
            "t_stat": round(self.t_stat, 2),
            "hit_rate": round(self.hit_rate, 3),
        }


def cross_sectional_ic(
    panel: pl.DataFrame,
    signal_col: str,
    forward_col: str,
    *,
    min_names: int = 10,
) -> ICResult:
    """Per-day cross-sectional Spearman IC of `signal_col` vs `forward_col`.

    Each calendar day is one observation. A day contributes only when it has
    at least `min_names` rows where BOTH the signal and the forward return are
    non-null. The reported `mean_ic` is the simple average of the daily ICs;
    `t_stat` tests whether that mean differs from zero, treating the daily IC
    series as i.i.d. (a deliberately optimistic assumption — real daily ICs are
    autocorrelated, so the true t-stat is somewhat lower).

    This is the ONLY way IC is computed in this codebase. Pooling observations
    across days is not offered, because it conflates cross-sectional skill with
    between-day regime structure (see module docstring).
    """
    if signal_col not in panel.columns or forward_col not in panel.columns:
        raise KeyError(f"panel missing {signal_col!r} or {forward_col!r}")

    sub = panel.select(["date", signal_col, forward_col]).drop_nulls()
    daily_ic: list[float] = []
    n_obs = 0
    for _, day in sub.partition_by("date", as_dict=True).items():
        if day.height < min_names:
            continue
        ic = spearman(day[signal_col].to_list(), day[forward_col].to_list())
        if math.isnan(ic):
            continue
        daily_ic.append(ic)
        n_obs += day.height

    n_days = len(daily_ic)
    if n_days == 0:
        return ICResult(signal_col, forward_col, 0, 0, 0.0, 0.0, 0.0, 0.0, ())
    arr = np.asarray(daily_ic, dtype=float)
    mean_ic = float(arr.mean())
    ic_std = float(arr.std(ddof=1)) if n_days > 1 else 0.0
    t_stat = mean_ic / (ic_std / math.sqrt(n_days)) if ic_std > 1e-12 else 0.0
    hit_rate = float((arr > 0.0).mean())
    return ICResult(
        signal_col, forward_col, n_days, n_obs,
        mean_ic, ic_std, t_stat, hit_rate, tuple(daily_ic),
    )


def ic_table(
    panel: pl.DataFrame,
    signal_cols: Sequence[str],
    forward_col: str,
    *,
    min_names: int = 10,
) -> pl.DataFrame:
    """IC summary for several signals against one forward-return column."""
    rows = [
        cross_sectional_ic(panel, c, forward_col, min_names=min_names).as_row()
        for c in signal_cols
    ]
    return pl.DataFrame(rows)


def ic_vs_horizon(
    panel: pl.DataFrame,
    signal_col: str,
    horizon_days: Sequence[int],
    *,
    min_names: int = 10,
) -> pl.DataFrame:
    """IC of one signal across forward horizons (panel must already carry the
    `fwd_short_return_{h}d` columns — see `add_forward_short_returns`)."""
    rows = []
    for h in horizon_days:
        col = f"fwd_short_return_{h}d"
        if col not in panel.columns:
            continue
        r = cross_sectional_ic(panel, signal_col, col, min_names=min_names).as_row()
        r["horizon_days"] = h
        rows.append(r)
    return pl.DataFrame(rows)
