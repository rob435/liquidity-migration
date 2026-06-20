"""Causal upper_wick entry-size multiplier — shared by backtest and the live demo book.

Receipt: docs/preregistration/2026-06-20-operator-override-upperwick-entry-sizing.md

The continuous v2 entry-quality sizing tilt validated 2026-06-20 (full-ledger Bybit MAR
6.387 -> 6.555, +0.168 vs inverse-vol alone, +1.62 vs hash). To guarantee the LIVE demo
book and the research backtest compute the IDENTICAL multiplier (the live<->backtest parity
gate this repo enforces), both call this single pure function.

The multiplier is strictly causal: it uses only PRIOR observations of the same symbol.
mult = clip(1 + k * z_uw * att), where
  z_uw = (upper_wick - mean(prior upper_wick)) / std(prior upper_wick)   [per-symbol, expanding]
  att  = 1 - (expanding percentile of rv among prior rv)                 [vol attenuation]
Below ``min_obs`` prior observations -> 1.0 (no tilt). Mean-1 in expectation (z ~ mean 0),
so it is a within-book reweighting, not a leverage change. att tapers the tilt toward 0 on
high-vol names (where upper_wick is empirically blind and inverse-vol is already downsizing),
which improved the full-ledger MAR over the ungated tilt.
"""
from __future__ import annotations

from collections.abc import Sequence

UPPERWICK_K = 0.5
UPPERWICK_CLIP = (0.5, 1.5)
UPPERWICK_MIN_OBS = 10


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
    """Causal per-symbol upper_wick size multiplier (see module docstring).

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
