"""Causal BTC-volatility regime intensity for the continuous-book hedge.

This module owns the code-defined regime signal used by the demo hedge
and standard historical refresh. Parameters are active defaults, not an
evidence verdict or authorization.

``intensity[d]`` uses BTC returns strictly BEFORE day ``d`` only. The trailing vol
for day ``d`` is the population stdev of the ``vol_window`` returns preceding ``d``
(``range(i - vol_window, i)`` excludes ``i``); its percentile is taken against the
deque of PRIOR days' vols (today's vol is appended only after it is scored). So the
day-``d`` hedge sized as ``beta(through d-1) * scale * intensity[d]`` never reads
day-``d`` data.
"""

from __future__ import annotations

import statistics as st
from collections import deque
from typing import Any

# Minimum prior returns before a trailing-vol point is defined; below this the
# vol (and therefore the intensity) is None -> 1.0 (no modulation).
VOL_MIN_OBS = 10
# Minimum accumulated vol observations before the percentile is trusted; below
# this the intensity is 1.0 (warm-up = no modulation, never look-ahead).
PCT_WARMUP = 50

# The active regime is read by the hedge manager and equity refresh. The
# intensity is symmetric about 1.0 (mean-1: a reallocation of hedge weight
# across regimes, not a larger average hedge), bounded to [1-lam, 1+lam].
ACTIVE_BTCVOL_REGIME: dict[str, Any] = {
    "kind": "btcvol",
    "lam": 0.5,
    "vol_window": 30,
    "pct_window": 250,
}


def _btc_vol_series(
    days: list[int], btc_rets: dict[int, float], vol_window: int
) -> list[float | None]:
    """Trailing population-stdev of BTC daily returns, causal per day.

    ``vols[i]`` uses returns on the ``vol_window`` day-positions strictly before
    ``days[i]`` (gap days absent from ``btc_rets`` are skipped); None until
    ``VOL_MIN_OBS`` prior returns exist."""
    vols: list[float | None] = []
    for i in range(len(days)):
        prior = [
            btc_rets[days[j]]
            for j in range(max(0, i - vol_window), i)
            if days[j] in btc_rets
        ]
        vols.append(st.pstdev(prior) if len(prior) >= VOL_MIN_OBS else None)
    return vols


def btcvol_intensity_series(
    days: list[int],
    btc_rets: dict[int, float],
    lam: float = ACTIVE_BTCVOL_REGIME["lam"],
    vol_window: int = ACTIVE_BTCVOL_REGIME["vol_window"],
    pct_window: int = ACTIVE_BTCVOL_REGIME["pct_window"],
) -> dict[int, float]:
    """Per-day mean-1 hedge-intensity ``1 + lam*(2*pct - 1)`` keyed by day-ms.

    ``pct`` = trailing-``pct_window`` percentile rank of the day's trailing vol
    among PRIOR days' vols (causal). Days before warm-up, or with an undefined
    vol, get intensity 1.0 (no modulation). Suitable as the ``hedge_intensity``
    argument to ``continuous_rebalance.apply_rebalance_rule``."""
    vols = _btc_vol_series(days, btc_rets, vol_window)
    dq: deque[float] = deque(maxlen=pct_window)
    intensity: dict[int, float] = {}
    for i, d in enumerate(days):
        v = vols[i]
        # Clamp warm-up to the deque capacity so short windows can activate.
        if v is None or len(dq) < min(PCT_WARMUP, pct_window):
            intensity[d] = 1.0
        else:
            pct = sum(1 for x in dq if x <= v) / len(dq)
            intensity[d] = 1.0 + lam * (2.0 * pct - 1.0)
        if v is not None:
            dq.append(v)
    return intensity


def latest_btcvol_intensity(
    btc_returns: list[float | None],
    lam: float = ACTIVE_BTCVOL_REGIME["lam"],
    vol_window: int = ACTIVE_BTCVOL_REGIME["vol_window"],
    pct_window: int = ACTIVE_BTCVOL_REGIME["pct_window"],
) -> float:
    """Intensity for the NEXT (to-be-sized) day given the prior BTC return series.

    The live hedge manager holds the BTC hedge-leg return series through yesterday
    (``ContinuousHedge2FState.prior_hedge_returns_1``) and sizes today's hedge. We
    map that list to consecutive synthetic day-indices ``0..n`` (today = ``n``, no
    return yet) and reuse ``btcvol_intensity_series`` verbatim, so the live "today"
    intensity is numerically identical to the backtest/forward intensity for the
    same day (parity is asserted in the test-suite). Returns 1.0 for an empty or
    all-warm-up series."""
    n = len(btc_returns)
    days = list(range(n + 1))
    btc_rets = {k: float(r) for k, r in enumerate(btc_returns) if r is not None}
    return btcvol_intensity_series(days, btc_rets, lam, vol_window, pct_window)[n]
