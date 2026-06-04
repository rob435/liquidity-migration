"""Causal volatility-half-life estimators for the continuous-fade v2 sleeve.

Centerpiece of `docs/research/continuous_halflife_fade_research_plan.md` (§4) and frozen in
`docs/preregistration/2026-06-03-continuous-halflife-fade.md`. Every function here is **CAUSAL BY
CONSTRUCTION**: the value computed for decision bar `t` is a function ONLY of bars with index <= t.
That single property's violation (a ~25h look-ahead in the rmom day-join) killed the v1 continuous
sleeve — its +319% gross became −68% net once made causal — so causality is enforced here both
structurally (the `estimate_vol_halflife` dispatcher slices to `close[:t+1]` at entry) AND by the
truncation / append-invariance / +1-bar-latency unit tests (`tests/test_continuous_halflife.py`,
plan §7 / §8.3.1) which MUST pass before any sweep.

RV-proxy convention: every realized-vol proxy is returned in per-bar **sigma (std) units** (not
variance), so the "died-down fraction" `c` and the OU / exp-decay half-life fits mean the same thing
across proxies.

These are pure numpy functions (no polars, no I/O), so (a) they are trivially truncation-testable, and
(b) they satisfy the plan's live-parity constraint (§4.7): each is a trailing per-symbol window, so it
reproduces byte-identically in the backtest (`continuous_events.py`) and live
(`continuous_demo.LivePanelCache`). An estimator that could not be a trailing per-symbol window is
rejected at design time — none here need to be.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .continuous_events import (
    ContinuousEventConfig,
    _additive_equity,
    _daily_pnl_metrics,
    _market_daily_returns,
    _portfolio_mtm_equity,
    _run_trades,
    _split_metrics,
    _write_equity_png,
)
from .signal_harness import _autodetect_dataset_names, _date_str_to_ms, _read_window
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import _empty_trades, _filter_signal_window, _funding_lookup
from .volume_events import (
    VolumeEventResearchConfig,
    _attach_residual_momentum,
    _indexed_price_bars_by_symbol,
    _window_config,
)
from .volume_events_features import _enriched_event_features, _event_score
from .volume_events_filters import _exclude_symbols, _select_events
from .volume_events_pit import _full_pit_universe_pass
from .volume_features import build_volume_features

_LN2 = float(np.log(2.0))
_PARKINSON_C = 1.0 / (4.0 * _LN2)          # Parkinson variance constant 1/(4 ln2) ≈ 0.3607
_GK_C2 = 2.0 * _LN2 - 1.0                   # Garman-Klass cross term (2 ln2 − 1) ≈ 0.3863


# --------------------------------------------------------------------------- trailing aggregators (causal)

def _trailing_mean_ignore_nan(x: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """Right-aligned (trailing) mean over the last `window` bars ending at each index, ignoring NaN.

    O(n) via cumulative sums. The window for index i is [i-window+1 .. i] (inclusive) — strictly bars
    <= i, so it is causal. NaN where fewer than `min_periods` finite values fall in the window.
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n == 0:
        return x.copy()
    valid = np.isfinite(x)
    v0 = np.where(valid, x, 0.0)
    csum = np.concatenate(([0.0], np.cumsum(v0)))
    ccnt = np.concatenate(([0], np.cumsum(valid.astype(np.int64))))
    hi = np.arange(1, n + 1)
    lo = np.maximum(0, hi - window)
    wsum = csum[hi] - csum[lo]
    wcnt = ccnt[hi] - ccnt[lo]
    out = np.full(n, np.nan, dtype=np.float64)
    ok = wcnt >= max(1, min_periods)
    out[ok] = wsum[ok] / wcnt[ok]
    return out


def _trailing_std_ignore_nan(x: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """Right-aligned trailing population std (ddof=0) over the last `window` bars, ignoring NaN. Causal."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n == 0:
        return x.copy()
    valid = np.isfinite(x)
    v0 = np.where(valid, x, 0.0)
    csum = np.concatenate(([0.0], np.cumsum(v0)))
    csum2 = np.concatenate(([0.0], np.cumsum(v0 * v0)))
    ccnt = np.concatenate(([0], np.cumsum(valid.astype(np.int64))))
    hi = np.arange(1, n + 1)
    lo = np.maximum(0, hi - window)
    wsum = csum[hi] - csum[lo]
    wsum2 = csum2[hi] - csum2[lo]
    wcnt = (ccnt[hi] - ccnt[lo]).astype(np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    ok = wcnt >= max(2, min_periods)
    mean = np.where(ok, wsum / np.where(ok, wcnt, 1.0), np.nan)
    var = np.where(ok, wsum2 / np.where(ok, wcnt, 1.0) - mean * mean, np.nan)
    var = np.where(var < 0.0, 0.0, var)    # clamp tiny negative from float cancellation
    out[ok] = np.sqrt(var[ok])
    return out


# --------------------------------------------------------------------------- RV proxies (per-bar σ, causal)

def parkinson_rv(high: np.ndarray, low: np.ndarray, *, window: int = 1, min_periods: int = 1) -> np.ndarray:
    """Per-bar Parkinson volatility, σ units: sqrt( trailing-mean over `window` of ln(H/L)²/(4 ln2) ).

    `window=1` → single-bar Parkinson (depends only on bar i; the strictly within-bar, lowest-leakage
    proxy). `window>1` → trailing mean of the per-bar Parkinson variance over the last `window` bars,
    sqrt'd (causal, right-aligned). NaN where H<=0 / L<=0 / too few valid bars.
    """
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.log(high / low)
    var_bar = _PARKINSON_C * ratio * ratio
    var_bar = np.where((high > 0.0) & (low > 0.0) & np.isfinite(var_bar), var_bar, np.nan)
    return np.sqrt(_trailing_mean_ignore_nan(var_bar, window=window, min_periods=min_periods))


def garman_klass_rv(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    *, window: int = 1, min_periods: int = 1,
) -> np.ndarray:
    """Per-bar Garman-Klass volatility, σ units: sqrt( trailing-mean of 0.5·ln(H/L)² − (2ln2−1)·ln(C/O)² ).

    Lower-variance OHLC estimator than Parkinson; within-bar (causal). NaN where any OHLC <= 0.
    """
    open_ = np.asarray(open_, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(high / low)
        co = np.log(close / open_)
    var_bar = 0.5 * hl * hl - _GK_C2 * co * co
    good = (open_ > 0.0) & (high > 0.0) & (low > 0.0) & (close > 0.0) & np.isfinite(var_bar)
    var_bar = np.where(good & (var_bar >= 0.0), var_bar, np.nan)
    return np.sqrt(_trailing_mean_ignore_nan(var_bar, window=window, min_periods=min_periods))


def rolling_std_rv(close: np.ndarray, *, window: int = 6, min_periods: int = 3) -> np.ndarray:
    """Per-bar volatility = trailing population std of log-returns over `window` bars ending at i. Causal.

    The simplest return-based proxy; aligns the log-return r_i (over bar i) at index i, so the std at i
    uses returns of bars <= i only.
    """
    close = np.asarray(close, dtype=np.float64)
    n = close.size
    r = np.full(n, np.nan, dtype=np.float64)
    if n >= 2:
        with np.errstate(divide="ignore", invalid="ignore"):
            r[1:] = np.diff(np.log(np.where(close > 0.0, close, np.nan)))
    return _trailing_std_ignore_nan(r, window=window, min_periods=min_periods)


def ewma_rv(close: np.ndarray, *, lam: float = 0.97, seed_window: int = 72, min_periods: int = 24) -> np.ndarray:
    """RiskMetrics EWMA volatility (σ units): σ²_t = λ σ²_{t−1} + (1−λ) r²_{t−1}. Strictly causal.

    Uses the *previous* bar's squared return, so σ_t is known at bar t's close from bars < t. Seeded with
    the trailing-`seed_window` variance at the first bar that has `min_periods` returns (NEVER the
    full-sample variance, plan §4.4). Returns NaN until seeded.
    """
    close = np.asarray(close, dtype=np.float64)
    n = close.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(np.where(close > 0.0, close, np.nan)))   # r[k] = return over bar k+1
    r2 = r * r
    var = np.nan
    seeded = False
    for i in range(1, n):
        ri2 = r2[i - 1]                                  # squared return of bar i (known at bar i close)
        if not seeded:
            lo = max(0, i - seed_window)
            seg = r2[lo:i]
            seg = seg[np.isfinite(seg)]
            if seg.size >= min_periods:
                var = float(np.mean(seg))
                seeded = True
            else:
                continue
        elif np.isfinite(ri2):
            var = lam * var + (1.0 - lam) * float(ri2)
        out[i] = np.sqrt(var) if (seeded and np.isfinite(var)) else np.nan
    return out


# --------------------------------------------------------------------------- method (e): RV ≤ c·running-peak

def method_e_entry_offset(
    rv: np.ndarray, event_idx: int, *,
    c: float = 0.5, d_min: int = 6, debounce_k: int = 2, t_min_bars: int = 6, max_observe: int = 48,
) -> int | None:
    """Method (e), the DEFAULT trigger: first bar `t` where post-event vol has "died down".

    Walk i from event_idx forward (closed bars only). Maintain the CAUSAL running peak
    `peak(i) = max(rv[event_idx..i])` and its argmax `t_peak`. ENTER at the first i in
    (event_idx, event_idx+max_observe] with ALL of:
      • rv[i] <= c·peak(i)                       (vol decayed below the died-down fraction; c=0.5 ⇔ 1 half-life)
      • (i − t_peak) >= d_min                    (enough bars since the peak; not a one-bar dip)
      • (i − event_idx) >= t_min_bars            (a minimum sample after the event)
      • the last `debounce_k` bars all <= c·peak  (consecutive-bar noise guard)
    Returns the ABSOLUTE bar index t (or None if no trigger in the window). The decision at t reads only
    rv[event_idx..t] — provably causal (the running peak never looks past i; verified by truncation test).
    A bar making a new high resets the consecutive counter (rv[i]=peak>c·peak), correctly deferring entry
    into a still-rising / double-pump move.
    """
    rv = np.asarray(rv, dtype=np.float64)
    n = rv.size
    if event_idx < 0 or event_idx >= n:
        return None
    end = min(n - 1, event_idx + max_observe)
    peak = -np.inf
    t_peak = event_idx
    consec = 0
    for i in range(event_idx, end + 1):
        vi = rv[i]
        if not np.isfinite(vi):
            consec = 0
            continue
        if vi > peak:
            peak = vi
            t_peak = i
        below = peak > 0.0 and vi <= c * peak
        consec = consec + 1 if below else 0
        if i == event_idx:
            continue
        if (
            below
            and consec >= debounce_k
            and (i - t_peak) >= d_min
            and (i - event_idx) >= t_min_bars
        ):
            return i
    return None


# --------------------------------------------------------------------------- method (a): OU / AR(1) half-life

def estimate_ou_halflife(x: np.ndarray, *, min_bars: int = 20, t_min: float = 2.0) -> tuple[float, float] | None:
    """OU/AR(1) mean-reversion half-life from a trailing window `x` (the RV series ending at decision t).

    Chan form: regress Δx_t on the lagged level x_{t−1} WITH intercept (the intercept handles de-meaning,
    plan §4.1): Δx = α + β·x_lag + ε, φ = 1+β, H = −ln2 / ln(1+β) (the exact form, not the small-β
    shortcut). Validity gates: φ ∈ (0,1) (else trending/explosive or oscillatory → reject) and |t(β̂)| ≥
    `t_min`. Returns (H, t_stat) or None. Causal: the caller passes a closed window ending at t.
    """
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    if x.size < min_bars + 1:
        return None
    x_lag = x[:-1]
    dx = np.diff(x)
    nn = x_lag.size
    design = np.column_stack([np.ones(nn), x_lag])
    coef, _res, _rank, _sv = np.linalg.lstsq(design, dx, rcond=None)
    beta = float(coef[1])
    phi = 1.0 + beta
    if not (0.0 < phi < 1.0):
        return None
    dof = nn - 2
    if dof <= 0:
        return None
    resid = dx - design @ coef
    sigma2 = float(resid @ resid) / dof
    try:
        xtx_inv = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        return None
    var_beta = sigma2 * float(xtx_inv[1, 1])
    if not np.isfinite(var_beta) or var_beta <= 0.0:
        return None
    t_stat = beta / float(np.sqrt(var_beta))
    if abs(t_stat) < t_min:
        return None
    halflife = -_LN2 / float(np.log(phi))
    if not np.isfinite(halflife) or halflife <= 0.0:
        return None
    return (halflife, t_stat)


# --------------------------------------------------------------------------- method (b): exp-decay of RV

def estimate_expdecay_halflife(
    rv: np.ndarray, event_idx: int, t: int, *, n_min: int = 6, t_min: float = 2.0,
) -> tuple[float, float] | None:
    """Exp-decay half-life from post-(running)peak RV (plan §4.2).

    t_peak = argmax(rv[event_idx..t]) (CAUSAL running peak). Fit ln(rv_i) = a − (1/τ)(i − t_peak) + ε by
    OLS over i ∈ (t_peak, t] with rv_i > 0; slope = −1/τ̂; H = τ̂·ln2. Gates on a significantly NEGATIVE
    slope (t_stat ≤ −t_min) and ≥ `n_min` post-peak points. Returns (H, slope_t_stat) or None. Causal:
    reads only rv[event_idx..t].
    """
    rv = np.asarray(rv, dtype=np.float64)
    if event_idx < 0 or t <= event_idx or t >= rv.size:
        return None
    seg = rv[event_idx : t + 1]
    finite = np.isfinite(seg)
    if not finite.any():
        return None
    peak_rel = int(np.nanargmax(np.where(finite, seg, -np.inf)))
    idx = np.arange(peak_rel + 1, seg.size)
    if idx.size < n_min:
        return None
    yv = seg[idx]
    good = np.isfinite(yv) & (yv > 0.0)
    if int(good.sum()) < n_min:
        return None
    xs = idx[good].astype(np.float64)
    ys = np.log(yv[good])
    nn = xs.size
    design = np.column_stack([np.ones(nn), xs])
    coef, _res, _rank, _sv = np.linalg.lstsq(design, ys, rcond=None)
    slope = float(coef[1])
    if slope >= 0.0:
        return None
    dof = nn - 2
    if dof <= 0:
        return None
    resid = ys - design @ coef
    sigma2 = float(resid @ resid) / dof
    try:
        xtx_inv = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        return None
    var_slope = sigma2 * float(xtx_inv[1, 1])
    if not np.isfinite(var_slope) or var_slope <= 0.0:
        return None
    t_stat = slope / float(np.sqrt(var_slope))
    if t_stat > -t_min:
        return None
    tau = -1.0 / slope
    halflife = tau * _LN2
    if not np.isfinite(halflife) or halflife <= 0.0:
        return None
    return (halflife, t_stat)


# --------------------------------------------------------------------------- RV-proxy dispatch + half-life API

def rv_series(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    *, proxy: str = "parkinson", window: int = 6, ewma_lambda: float = 0.97,
) -> np.ndarray:
    """Build the per-bar σ-unit RV series for the chosen proxy (causal). `window` is the trailing
    smoothing window for parkinson/gk/rolling_std; `ewma_lambda` is used by the EWMA proxy."""
    if proxy == "parkinson":
        return parkinson_rv(high, low, window=window, min_periods=max(1, window // 2))
    if proxy in ("gk", "garman_klass"):
        return garman_klass_rv(open_, high, low, close, window=window, min_periods=max(1, window // 2))
    if proxy in ("rolling_std", "std"):
        return rolling_std_rv(close, window=window, min_periods=max(2, window // 2))
    if proxy == "ewma":
        return ewma_rv(close, lam=ewma_lambda)
    raise ValueError(f"unknown RV proxy: {proxy!r}")


def estimate_vol_halflife(
    close: np.ndarray, high: np.ndarray, low: np.ndarray, event_bar: int, t: int,
    *, min_bars: int = 6, method: str = "exp", rv_proxy: str = "parkinson",
    rv_window: int = 6, fit_window: int = 72, t_min: float = 2.0,
    open_: np.ndarray | None = None,
) -> float | None:
    """Half-life estimate (in bars) for the decay observed up to decision bar `t` (methods (a)/(b)).

    Matches the plan §10.1 signature. CAUSAL BY CONSTRUCTION: every input array is sliced to `[:t+1]`
    here, so the result cannot depend on any bar after t regardless of what the caller passes. Returns
    the half-life (bars) or None when the fit is invalid/insignificant. `method="exp"` → method (b)
    post-peak log-OLS; `method="ou"` → method (a) AR(1) on the trailing `fit_window` of the RV series.
    """
    close = np.asarray(close, dtype=np.float64)
    n = close.size
    if t < 0 or t >= n or event_bar < 0 or event_bar > t:
        return None
    hi = np.asarray(high, dtype=np.float64)[: t + 1]
    lo = np.asarray(low, dtype=np.float64)[: t + 1]
    cl = close[: t + 1]
    op = np.asarray(open_, dtype=np.float64)[: t + 1] if open_ is not None else cl
    rv = rv_series(op, hi, lo, cl, proxy=rv_proxy, window=rv_window)
    if method == "exp":
        res = estimate_expdecay_halflife(rv, event_bar, t, n_min=max(3, min_bars), t_min=t_min)
    elif method == "ou":
        lo_fit = max(event_bar, t - fit_window + 1)
        res = estimate_ou_halflife(rv[lo_fit : t + 1], min_bars=max(min_bars, 20), t_min=t_min)
    else:
        raise ValueError(f"unknown half-life method: {method!r}")
    return None if res is None else res[0]


# ===========================================================================
# Engine wiring: candidate screen (§3) + half-life entries (§5) + runner (§6)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class HalflifeFadeConfig(ContinuousEventConfig):
    """Continuous-fade v2 config: the KEPT execution/cost/accounting core (ContinuousEventConfig) + the
    new half-life entry-timing layer (this subclass). See the plan + the 2026-06-03 pre-registration.
    Stage-1 default = method (e) running-peak crossing, Parkinson RV, fixed hold_hours time-stop."""

    # --- half-life entry timing (the new alpha layer) ---
    halflife_estimator: str = "method_e"   # "method_e" (§4.5 default) | "ou" (§4.1) | "exp" (§4.2)
    rv_proxy: str = "parkinson"            # parkinson | gk | rolling_std | ewma
    rv_window: int = 6                     # trailing smoothing window for the RV proxy (hours)
    ewma_lambda: float = 0.97
    vol_decay_frac: float = 0.5            # c: enter when RV <= c*running-peak (⇔ # half-lives waited)
    d_min_hours: int = 6                   # method (e): min bars since the running peak
    debounce_k: int = 2                    # method (e): consecutive bars below c*peak (noise guard)
    t_min_bars: int = 6                    # min bars after the event before any entry
    max_observe_hours: int = 48            # watch window per event; no trigger in it -> stay flat
    fit_window: int = 72                   # (a)/(b): trailing window for the OU / exp fit
    halflife_t_min: float = 2.0            # (a)/(b): slope/beta significance gate
    halflife_max_hours: float = 24.0       # (a)/(b): max accepted half-life (H_max)
    require_ou_confirm: bool = False       # headline cell: also require OU H<=H_max at the (e) entry bar
    disable_halflife_gate: bool = False    # CONTROL: enter-immediately-on-event (the timing baseline)
    halflife_input_lag: int = 0            # FALSIFIER (§7/4): shift the RV inputs +N bars; a real edge
    #                                        degrades gracefully, a 1-bar look-ahead artifact collapses.


def _selection_config(config: HalflifeFadeConfig) -> VolumeEventResearchConfig:
    """The SHORT system's promoted selection (drop_all_4 + age300 + ff6), windowed to the run, with the
    BACKTEST PIT hard-gates ON (§3.4). `_demo_event_config` returns the LIVE config (PIT off + listing-age
    substitution); the backtest flips require_full_pit_universe/require_pit_membership back on so the
    candidate universe is full point-in-time. The promoted selection uses NO rmom gate
    (liquidity_migration_residual_momentum_max=10.0 = off) -> v2 never touches v1's leaky rmom day-join."""
    from .event_demo import _demo_event_config

    promoted = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    return replace(
        promoted,
        require_full_pit_universe=True,
        require_pit_membership=True,
        start_date=config.start_date,
        end_date=config.end_date,
        exclude_symbols=config.exclude_symbols,
    )


def _candidates_cache_stale(cache_path: Path, root: Path) -> bool:
    """Stale if the cache is missing or any klines_1h / archive_trade_manifest parquet is newer than it.
    The candidate frame depends ONLY on the selection config + the klines/manifest data (NOT the half-life
    estimator), so its freshness tracks those data dirs (mirrors continuous_events._panel_cache_stale vs
    rmom). §8.5: the per-cell half-life is computed at run time from bars, never cached, so there is no
    estimator-staleness surface to guard -- only this candidate cache (cleared by `rm -f
    <root>/_continuous_halflife_*.parquet`). Fail-safe to stale on any error."""
    try:
        if not cache_path.exists():
            return True
        cache_mtime = cache_path.stat().st_mtime
        for sub in ("klines_1h", "archive_trade_manifest"):
            d = root / sub
            if d.is_dir():
                for p in d.glob("**/*.parquet"):
                    if p.stat().st_mtime > cache_mtime:
                        return True
        return False
    except OSError:
        return True


def build_event_candidates(
    data_root: str | Path, config: HalflifeFadeConfig, *, cache: bool = True,
) -> pl.DataFrame:
    """§3 candidate screen: run the SHORT system's `_select_events` chain (verbatim) at the promoted config
    to get every liquidity-migration event, PIT-correct. Returns (symbol, event_ts, score) where event_ts
    is the DAILY event stamp (day_start + 1 day = when the daily signal becomes known). REPLACES the leaky
    build_continuous_panel: no decile panel, no rmom join -- only the proven-causal short selection. Cached
    per selection-config hash with a klines/manifest freshness guard (§8.5)."""
    root = Path(str(data_root)).expanduser()
    selcfg = _selection_config(config)
    sel_hash = hashlib.sha256(
        json.dumps(asdict(selcfg), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    cache_path = root / f"_continuous_halflife_candidates_{sel_hash}.parquet"
    if cache and not _candidates_cache_stale(cache_path, root):
        return pl.read_parquet(cache_path)

    raw_klines = read_dataset_columns(
        root, "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    if raw_klines.is_empty():
        return pl.DataFrame({"symbol": [], "event_ts": [], "score": []},
                            schema={"symbol": pl.Utf8, "event_ts": pl.Int64, "score": pl.Float64})
    ex = selcfg.exclude_symbols
    klines = _exclude_symbols(raw_klines, ex)
    archive_manifest = _exclude_symbols(read_dataset(root, "archive_trade_manifest"), ex)
    funding = _exclude_symbols(read_dataset(root, "funding"), ex)
    open_interest = _exclude_symbols(read_dataset(root, "open_interest"), ex)
    signed_flow_1h = _exclude_symbols(read_dataset(root, "signed_flow_1h"), ex)
    mark_price_1h = _exclude_symbols(read_dataset(root, "mark_price_1h"), ex)
    index_price_1h = _exclude_symbols(read_dataset(root, "index_price_1h"), ex)
    premium_index_1h = _exclude_symbols(read_dataset(root, "premium_index_1h"), ex)
    features = _filter_signal_window(
        _enriched_event_features(
            build_volume_features(klines), klines, archive_manifest,
            funding=funding, open_interest=open_interest, signed_flow_1h=signed_flow_1h,
            mark_price_1h=mark_price_1h, index_price_1h=index_price_1h, premium_index_1h=premium_index_1h,
        ),
        _window_config(selcfg),
    )
    if selcfg.liquidity_migration_residual_momentum_max < 10.0:
        features = _attach_residual_momentum(features, root)
    if selcfg.require_full_pit_universe and not _full_pit_universe_pass(klines, archive_manifest):
        raise RuntimeError("full-PIT universe coverage gap -- candidate screen is not full-PIT (invalid)")
    from .event_demo import _selected_scenario

    scenario = _selected_scenario(selcfg)
    _, score_col = _event_score(scenario.event_type)
    events = _select_events(features, scenario=scenario, config=selcfg, score_col=score_col)
    if events.is_empty():
        out = pl.DataFrame({"symbol": [], "event_ts": [], "score": []},
                           schema={"symbol": pl.Utf8, "event_ts": pl.Int64, "score": pl.Float64})
    else:
        score_expr = pl.col(score_col).cast(pl.Float64) if score_col in events.columns else pl.lit(0.0)
        out = (
            events.select(
                pl.col("symbol"), pl.col("ts_ms").cast(pl.Int64).alias("event_ts"), score_expr.alias("score"),
            )
            .unique(["symbol", "event_ts"])
            .sort(["event_ts", "symbol"])
        )
    if cache:
        out.write_parquet(cache_path)
    return out


def _turnover_by_symbol(klines: pl.DataFrame) -> dict[str, np.ndarray]:
    """Per-symbol hourly turnover_quote arrays, aligned index-for-index to _indexed_price_bars_by_symbol
    (identical sort(['symbol','ts_ms']) + partition_by('symbol')), so turnover[sym][i] is the turnover of
    bars[sym]['close'][i]. The bars dict carries no turnover; this feeds the signal-bar liquidity gate."""
    if klines.is_empty() or "turnover_quote" not in klines.columns:
        return {}
    out: dict[str, np.ndarray] = {}
    for key, part in klines.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        out[symbol] = part["turnover_quote"].to_numpy().astype(np.float64, copy=False)
    return out


def _entry_offset_for_candidate(rv: np.ndarray, event_bar: int, config: HalflifeFadeConfig) -> int | None:
    """The per-candidate entry bar index for the configured estimator, or None (no entry -> stay flat).
    CONTROL (disable_halflife_gate): enter at event_bar + t_min_bars (no vol gate -> the timing baseline).
    method_e: the running-peak crossing. ou/exp: first bar where the trailing half-life is valid and <=
    halflife_max_hours. require_ou_confirm adds the OU gate to the (e) bar (the headline (e)+(a) cell). All
    decisions read rv[<= the decision bar] only (rv is causal per the unit tests)."""
    n = rv.size
    end = min(n - 1, event_bar + config.max_observe_hours)
    first = event_bar + max(1, config.t_min_bars)
    if first > end:
        return None
    if config.disable_halflife_gate:
        return first
    if config.halflife_estimator == "method_e":
        idx = method_e_entry_offset(
            rv, event_bar, c=config.vol_decay_frac, d_min=config.d_min_hours,
            debounce_k=config.debounce_k, t_min_bars=config.t_min_bars, max_observe=config.max_observe_hours,
        )
        if idx is None or not config.require_ou_confirm:
            return idx
        lo = max(event_bar, idx - config.fit_window + 1)
        ou = estimate_ou_halflife(rv[lo : idx + 1], min_bars=20, t_min=config.halflife_t_min)
        return idx if (ou is not None and ou[0] <= config.halflife_max_hours) else None
    for t in range(first, end + 1):
        if config.halflife_estimator == "ou":
            lo = max(event_bar, t - config.fit_window + 1)
            res = estimate_ou_halflife(rv[lo : t + 1], min_bars=20, t_min=config.halflife_t_min)
        else:  # "exp"
            res = estimate_expdecay_halflife(rv, event_bar, t, n_min=6, t_min=config.halflife_t_min)
        if res is not None and res[0] <= config.halflife_max_hours:
            return t
    return None


def _halflife_entries(
    candidates: pl.DataFrame, symbol_bars: dict[str, Any],
    turnover_by_symbol: dict[str, np.ndarray], config: HalflifeFadeConfig,
) -> pl.DataFrame:
    """For each (symbol, event_ts) candidate, watch post-event HOURLY bars and emit ONE entry at the bar
    where vol has died down (all features causal: bars <= the decision bar). Output schema ==
    continuous_events._fresh_entries, so it flows through _run_trades unchanged. event_bar = the daily-close
    hour (bar_end == event_ts); the walk + running peak start there; entries fire >= t_min_bars later. Most
    candidates never trigger (no row) -- that sparsity is the anti-cost mechanism."""
    empty = pl.DataFrame(
        {"symbol": [], "ts_ms": [], "composite": [], "turnover_quote": [], "spell_end_ts": []},
        schema={"symbol": pl.Utf8, "ts_ms": pl.Int64, "composite": pl.Float64,
                "turnover_quote": pl.Float64, "spell_end_ts": pl.Int64},
    )
    if candidates.is_empty():
        return empty
    rv_cache: dict[str, np.ndarray] = {}
    rows: list[dict[str, Any]] = []
    syms = candidates["symbol"].to_list()
    evts = candidates["event_ts"].to_list()
    scores = candidates["score"].to_list() if "score" in candidates.columns else [0.0] * len(syms)
    for sym, ev_ts, score in zip(syms, evts, scores):
        bars = symbol_bars.get(sym)
        if bars is None:
            continue
        event_bar = bars["by_end"].get(int(ev_ts))
        if event_bar is None:
            ends = bars["bar_end_ts_ms"]
            j = int(np.searchsorted(ends, int(ev_ts), side="left"))
            if j >= ends.size:
                continue
            event_bar = j
        rv = rv_cache.get(sym)
        if rv is None:
            rv = rv_series(bars["open"], bars["high"], bars["low"], bars["close"],
                           proxy=config.rv_proxy, window=config.rv_window, ewma_lambda=config.ewma_lambda)
            if config.halflife_input_lag > 0:
                lag = config.halflife_input_lag        # +1-bar latency falsifier: the decision at bar t now
                shifted = np.full_like(rv, np.nan)      # sees the RV that was true at t-lag (inputs arrive late)
                shifted[lag:] = rv[:-lag]
                rv = shifted
            rv_cache[sym] = rv
        entry_idx = _entry_offset_for_candidate(rv, int(event_bar), config)
        if entry_idx is None:
            continue
        turns = turnover_by_symbol.get(sym)
        if turns is None or entry_idx >= turns.size:
            continue
        turn = float(turns[entry_idx])
        if turn < config.liq_turnover_min:
            continue
        sig_ts = int(bars["ts_ms"][entry_idx])
        rows.append({
            "symbol": sym, "ts_ms": sig_ts,
            "composite": float(score) if score is not None else 0.0,
            "turnover_quote": turn, "spell_end_ts": sig_ts,
        })
    if not rows:
        return empty
    return pl.DataFrame(rows).sort(["ts_ms", "symbol"])


@dataclass(frozen=True, slots=True)
class _EngineInputs:
    """The EXPENSIVE, cell-invariant inputs (candidates + bars + turnover + funding + market) loaded once
    and shared across a half-life/exit sweep -- candidates depend only on the selection config, bars on the
    klines window, both constant across the grid."""

    candidates: pl.DataFrame
    symbol_bars: dict[str, Any]
    turnover_by_symbol: dict[str, np.ndarray]
    funding_lookup: dict[str, dict[str, Any]] | None
    klines: pl.DataFrame
    market_daily: dict[int, float] | None


def _load_engine_inputs(data_root: str | Path, config: HalflifeFadeConfig) -> _EngineInputs:
    """Load candidates (cached, §3), per-symbol bars + aligned turnover, the funding lookup (autodetected so
    funding is costed on BOTH venues), and the optional market series -- ONCE. A sweep calls this once with a
    config carrying the MAX hold/observe of the grid (so the forward pad covers every cell), then runs many
    cells via _run_cell."""
    root = Path(str(data_root)).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Data root does not exist: {root}")
    candidates = build_event_candidates(root, config)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    start_ms, end_ms = _date_str_to_ms(config.start_date), _date_str_to_ms(config.end_date)
    pad_fwd = (config.hold_hours + config.entry_delay_hours + config.max_observe_hours + 4) * MS_PER_HOUR
    klines = _read_window(
        root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms + pad_fwd,
        columns=["ts_ms", "symbol", "open", "high", "low", "close", "turnover_quote"],
    )
    if config.exclude_symbols and not klines.is_empty():
        klines = klines.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    symbol_bars = _indexed_price_bars_by_symbol(klines) if not klines.is_empty() else {}
    turnover_by_symbol = _turnover_by_symbol(klines)
    funding_lookup = None
    if config.use_funding:
        fname = _autodetect_dataset_names(root)["funding_dataset"]
        funding = _read_window(root, fname, start_ms=start_ms - 10 * MS_PER_DAY, end_ms=end_ms + pad_fwd)
        funding_lookup = _funding_lookup(funding)
    market_daily = None
    if config.market_min_ret_1d > -1.0 and not klines.is_empty():
        market_daily = _market_daily_returns(klines)
    return _EngineInputs(candidates, symbol_bars, turnover_by_symbol, funding_lookup, klines, market_daily)


def _run_cell(
    inp: _EngineInputs, config: HalflifeFadeConfig, *,
    data_root: str = "", report_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run ONE half-life/exit cell against preloaded inputs: §5 entries -> the kept _run_trades -> metrics.
    Fast (entries + sim only); the expensive loads live in _load_engine_inputs."""
    if not inp.candidates.is_empty() and inp.symbol_bars:
        entries = _halflife_entries(inp.candidates, inp.symbol_bars, inp.turnover_by_symbol, config)
    else:
        entries = pl.DataFrame()
    if not entries.is_empty():
        trades, skips = _run_trades(entries, inp.symbol_bars, inp.funding_lookup, config, inp.market_daily)
    else:
        trades, skips = _empty_trades(), {}

    equity = _additive_equity(trades)
    splits = _split_metrics(trades, config)
    mtm_equity = _portfolio_mtm_equity(trades, inp.klines)
    mtm = _daily_pnl_metrics(mtm_equity)

    payload: dict[str, Any] = {
        "config": asdict(config),
        "config_hash": config.config_hash(),
        "data_root": data_root,
        "run_label": "exploratory",
        "full_pit_universe_pass": True,   # build_event_candidates raises on coverage gaps (require_full_pit)
        "n_candidates": int(inp.candidates.height),
        "n_entries": int(entries.height) if not entries.is_empty() else 0,
        "n_trades": int(trades.height),
        "skips": skips,
        "funding_mode": splits.get("full", {}).get("funding_mode", "missing"),
        "metrics": splits,
        "metrics_mtm": mtm,
    }
    if report_dir is not None:
        out_dir = Path(str(report_dir)).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        if not trades.is_empty():
            trades.write_csv(out_dir / "halflife_trades.csv")
            equity.write_csv(out_dir / "halflife_equity.csv")
            mtm_equity.write_csv(out_dir / "halflife_mtm_equity.csv")
            _write_equity_png(
                mtm_equity, out_dir / "halflife_mtm_equity.png",
                title=(f"HALFLIFE FADE v2 — {config.halflife_estimator} c={config.vol_decay_frac} "
                       f"hold {config.hold_hours}h | MTM-MAR {mtm.get('mar')} "
                       f"DD {abs(mtm.get('max_drawdown') or 0) * 100:.1f}%  [EXPLORATORY]"),
            )
        (out_dir / "halflife_report.json").write_text(json.dumps(payload, indent=2, default=str))
        payload["report_dir"] = str(out_dir)
    return payload


def run_halflife_fade_research(
    data_root: str | Path, *,
    config: HalflifeFadeConfig | None = None, report_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Single-cell continuous-fade v2 backtest: §3 candidate screen -> §5 half-life entries -> the kept
    continuous engine execution core. EXPLORATORY (research-tuned + modeled impact). Thin wrapper over
    _load_engine_inputs + _run_cell (the sweep driver reuses those to load once / run many)."""
    config = config or HalflifeFadeConfig()
    root = Path(str(data_root)).expanduser()
    inp = _load_engine_inputs(root, config)
    return _run_cell(inp, config, data_root=str(root), report_dir=report_dir)
