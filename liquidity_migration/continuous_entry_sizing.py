"""Causal upper_wick entry-size multiplier retained for audit, flag-off.

The upper_wick sizing attempt was withdrawn after live<->backtest parity exposed
a component duplicate-counting artifact. Corrected one-observation-per-decision
validation was slightly negative and below hash. This pure helper stays for the
disabled audit path and future explicitly registered wick work; it is not
evidence that the tilt is valid or active.

The multiplier is strictly causal: it uses only PRIOR observations of the same symbol.
mult = clip(1 + k * z_uw * att), where
  z_uw = (upper_wick - mean(prior upper_wick)) / std(prior upper_wick)   [per-symbol, expanding]
  att  = 1 - (expanding percentile of rv among prior rv)                 [vol attenuation]
Below ``min_obs`` prior observations -> 1.0 (no tilt). Mean-1 in expectation (z ~ mean 0),
so it is a within-book reweighting, not a leverage change. The live flag remains off.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

UPPERWICK_K = 0.5
UPPERWICK_CLIP = (0.5, 1.5)
UPPERWICK_MIN_OBS = 10
UPPERWICK_WINDOW_MIN = 120
UPPERWICK_RV_N = 30


def upper_wick_and_rv_from_ohlc(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> tuple[float, float]:
    """Canonical pre-entry (upper_wick_mean, rv_30) from a 1m OHLC window.

    upper_wick_mean = mean over the window of (high - max(open, close)) /
    (high - low) on bars with high > low; rv_30 = population std of the last 30
    one-minute log returns (>5 returns required, else 0). Both the corrected
    audit path and the flag-off live helper use this one construction.
    """
    wicks = [
        (h - max(o, c)) / (h - low_)
        for o, h, low_, c in zip(opens, highs, lows, closes)
        if None not in (o, h, low_, c) and h > low_
    ]
    uw = (sum(wicks) / len(wicks)) if wicks else 0.0
    logr = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] and closes[i - 1]
    ]
    tail = logr[-UPPERWICK_RV_N:]
    rv = _std(tail) if len(logr) > 5 else 0.0
    return float(uw), float(rv)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def upperwick_size_mult(
    upper_wick: float,
    rv: float,
    prior_upper_wick: Sequence[float],
    prior_rv: Sequence[float],
    *,
    k: float = UPPERWICK_K,
    clip: tuple[float, float] = UPPERWICK_CLIP,
    vol_attenuate: bool = True,
    min_obs: int = UPPERWICK_MIN_OBS,
) -> float:
    """Causal per-symbol upper_wick size multiplier (flag-off audit helper).

    ``prior_upper_wick`` / ``prior_rv`` are this symbol's observations STRICTLY BEFORE the
    current entry (the caller must not include the current row). Returns 1.0 until at least
    ``min_obs`` priors exist, matching the backtest lookup and the live cold-start.
    """
    if len(prior_upper_wick) < min_obs:
        return 1.0
    sd = _std(prior_upper_wick) or 1.0
    z = (upper_wick - _mean(prior_upper_wick)) / sd
    att = 1.0
    if vol_attenuate and prior_rv:
        pctl = sum(1.0 for x in prior_rv if x <= rv) / len(prior_rv)
        att = 1.0 - pctl
    lo, hi = clip
    return float(min(max(1.0 + k * z * att, lo), hi))
