"""Causal BTC-volatility regime intensity for the continuous-book hedge.

The W5 program (`docs/research_plans/w5_continuous_signal_alpha/PROGRAM_REPORT.md`)
found exactly one robust, both-venue, trade-keeping improvement to the continuous
fade book: modulate the hedge leg by a causal BTC-volatility regime — hedge MORE
in turbulence, LESS in calm — via a mean-1 daily ``intensity`` multiplier. This
module is the single source of truth for that signal; both the live demo hedge
manager (`continuous_hedge_manager`) and the no-order forward signal clock
(`continuous_forward_replay` / the orchestrator) compute the intensity here so the
demo and the forward ledger track the identical hedge object.

Causality (a hard methodology gate — `docs/backtesting_errors_we_never_repeat.md`):
``intensity[d]`` uses BTC returns strictly BEFORE day ``d`` only. The trailing vol
for day ``d`` is the population stdev of the ``vol_window`` returns preceding ``d``
(``range(i - vol_window, i)`` excludes ``i``); its percentile is taken against the
deque of PRIOR days' vols (today's vol is appended only after it is scored). So the
day-``d`` hedge sized as ``beta(through d-1) * scale * intensity[d]`` never reads
day-``d`` data.

Ported verbatim (numbers/order preserved) from
`scripts/w5_continuous_stage8_regime_hedge.py` (`_btc_vol_series` /
`_btcvol_intensity`), where the deliverable was validated.
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

# The frozen, deployed regime. Embedded into FROZEN_FORWARD_CONFIG['hedge'] so it
# is part of frozen_config_hash() (changing it voids the forward ledger by design),
# and read by the live hedge manager so demo + forward use identical parameters.
# lam=0.5 is the W5 Stage 8c deliverable (robust across {0.25,0.5,0.75}); the
# intensity is symmetric about 1.0 (mean-1: a reallocation of hedge weight across
# regimes, not a larger average hedge), bounded to [1-lam, 1+lam].
FROZEN_BTCVOL_REGIME: dict[str, Any] = {
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
    lam: float = FROZEN_BTCVOL_REGIME["lam"],
    vol_window: int = FROZEN_BTCVOL_REGIME["vol_window"],
    pct_window: int = FROZEN_BTCVOL_REGIME["pct_window"],
) -> dict[int, float]:
    """Per-day mean-1 hedge-intensity ``1 + lam*(2*pct - 1)`` keyed by day-ms.

    ``pct`` = trailing-``pct_window`` percentile rank of the day's trailing vol
    among PRIOR days' vols (causal). Days before warm-up, or with an undefined
    vol, get intensity 1.0 (no modulation). Suitable as the ``hedge_intensity``
    argument to ``continuous_rebalance.apply_rebalance_rule`` /
    ``continuous_forward_replay.build_full_ledger``."""
    vols = _btc_vol_series(days, btc_rets, vol_window)
    dq: deque[float] = deque(maxlen=pct_window)
    intensity: dict[int, float] = {}
    for i, d in enumerate(days):
        v = vols[i]
        if v is None or len(dq) < PCT_WARMUP:
            intensity[d] = 1.0
        else:
            pct = sum(1 for x in dq if x <= v) / len(dq)
            intensity[d] = 1.0 + lam * (2.0 * pct - 1.0)
        if v is not None:
            dq.append(v)
    return intensity


def latest_btcvol_intensity(
    btc_returns: list[float | None],
    lam: float = FROZEN_BTCVOL_REGIME["lam"],
    vol_window: int = FROZEN_BTCVOL_REGIME["vol_window"],
    pct_window: int = FROZEN_BTCVOL_REGIME["pct_window"],
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
