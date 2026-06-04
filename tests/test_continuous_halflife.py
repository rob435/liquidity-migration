"""Causality + correctness tests for the volatility-half-life estimators (continuous-fade v2).

These are the GATE for the whole sleeve (plan §8.3.1 / §10.3, pre-reg 2026-06-03). v1 died of a
look-ahead leak; here we PROVE — not merely intend — that every estimator's value for decision bar
`t` depends only on bars <= t:
  • truncation invariance: recomputing on bars[:t+1] gives the identical prefix;
  • append/mutate-future invariance: changing any bar > t never changes the decision at t;
  • the method-(e) running peak is causal (a later spike cannot retroactively suppress an earlier entry).
Plus correctness sanity (recovers a known half-life; no trigger on a still-rising pump; rejects noise).
The engine-level +1-bar latency falsifier (plan §7 trap #4) is added with the engine wiring.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.continuous_halflife import (
    HalflifeFadeConfig,
    _entry_offset_for_candidate,
    _halflife_entries,
    estimate_expdecay_halflife,
    estimate_ou_halflife,
    estimate_vol_halflife,
    ewma_rv,
    garman_klass_rv,
    method_e_entry_offset,
    parkinson_rv,
    rolling_std_rv,
    rv_series,
)

_LN2 = float(np.log(2.0))


def _synthetic_ohlc(n: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """A pump-then-decay OHLC path: a vol burst around bar n//4 decaying over the rest of the series."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    burst = 0.08 * np.exp(-(t - n // 4) / 18.0)
    burst[t < n // 4] = 0.005
    vol = 0.004 + np.clip(burst, 0.0, None)
    rets = rng.normal(0.0, 1.0, n) * vol
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = np.empty(n)
    open_[0] = 100.0
    open_[1:] = close[:-1]
    intrabar = vol * np.abs(rng.normal(0.0, 1.0, n)) + 1e-4
    high = np.maximum(open_, close) * (1.0 + intrabar)
    low = np.minimum(open_, close) * (1.0 - intrabar)
    return open_, high, low, close


# --------------------------------------------------------------------------- RV-proxy causality

@pytest.mark.parametrize("proxy", ["parkinson", "gk", "rolling_std", "ewma"])
def test_rv_proxy_truncation_invariant(proxy: str) -> None:
    """Recomputing an RV proxy on bars truncated at t yields the identical prefix → causal."""
    op, hi, lo, cl = _synthetic_ohlc(320, seed=1)
    full = rv_series(op, hi, lo, cl, proxy=proxy, window=6)
    for t in (60, 130, 200, 319):
        trunc = rv_series(op[: t + 1], hi[: t + 1], lo[: t + 1], cl[: t + 1], proxy=proxy, window=6)
        assert np.allclose(trunc, full[: t + 1], equal_nan=True), f"{proxy} leaks future at t={t}"


@pytest.mark.parametrize("proxy", ["parkinson", "gk", "rolling_std", "ewma"])
def test_rv_proxy_append_future_does_not_change_past(proxy: str) -> None:
    """Appending arbitrary FUTURE bars cannot change any past RV value (right-aligned/causal windows)."""
    op, hi, lo, cl = _synthetic_ohlc(200, seed=2)
    base = rv_series(op, hi, lo, cl, proxy=proxy, window=6)
    fop, fhi, flo, fcl = _synthetic_ohlc(80, seed=999)   # a totally different future
    ext = rv_series(
        np.concatenate([op, fop]), np.concatenate([hi, fhi]),
        np.concatenate([lo, flo]), np.concatenate([cl, fcl]),
        proxy=proxy, window=6,
    )
    assert np.allclose(ext[:200], base, equal_nan=True), f"{proxy} past changed by future bars"


def test_parkinson_window1_is_pure_within_bar() -> None:
    """window=1 Parkinson depends ONLY on each bar's own high/low (the lowest-leakage proxy)."""
    _op, hi, lo, _cl = _synthetic_ohlc(50, seed=3)
    rv = parkinson_rv(hi, lo, window=1)
    expected = np.abs(np.log(hi / lo)) / (2.0 * np.sqrt(_LN2))
    assert np.allclose(rv, expected, equal_nan=True)


def test_proxies_are_nonnegative_sigma_units() -> None:
    op, hi, lo, cl = _synthetic_ohlc(150, seed=4)
    for fn in (
        parkinson_rv(hi, lo, window=6),
        garman_klass_rv(op, hi, lo, cl, window=6),
        rolling_std_rv(cl, window=6),
        ewma_rv(cl),
    ):
        finite = fn[np.isfinite(fn)]
        assert (finite >= 0.0).all()


# --------------------------------------------------------------------------- method (e) causality

def _decay_rv(peak_idx: int, n: int, tau: float = 10.0, peak: float = 1.0, floor: float = 0.05) -> np.ndarray:
    rv = np.empty(n)
    for i in range(n):
        if i <= peak_idx:
            rv[i] = floor + (peak - floor) * (i / max(peak_idx, 1))
        else:
            rv[i] = floor + (peak - floor) * np.exp(-(i - peak_idx) / tau)
    return rv


def test_method_e_truncation_invariant_at_entry() -> None:
    """The entry decision uses only bars up to the entry bar: truncating there finds the same entry."""
    rv = _decay_rv(peak_idx=8, n=120, tau=12.0)
    entry = method_e_entry_offset(rv, 0, c=0.5, d_min=3, debounce_k=2, t_min_bars=6, max_observe=80)
    assert entry is not None
    same = method_e_entry_offset(rv[: entry + 1], 0, c=0.5, d_min=3, debounce_k=2, t_min_bars=6, max_observe=80)
    assert same == entry


def test_method_e_future_cannot_change_past_entry() -> None:
    """A later spike / NaNs after the entry must NOT move or destroy the (already-decided) entry."""
    rv = _decay_rv(peak_idx=8, n=120, tau=12.0)
    entry = method_e_entry_offset(rv, 0, c=0.5, d_min=3, debounce_k=2, t_min_bars=6, max_observe=80)
    assert entry is not None
    for mutate in (1e3, np.nan, 0.0):
        rv2 = rv.copy()
        rv2[entry + 1 :] = mutate
        assert method_e_entry_offset(rv2, 0, c=0.5, d_min=3, debounce_k=2, t_min_bars=6, max_observe=80) == entry


def test_method_e_no_entry_while_still_pumping() -> None:
    """Monotonically RISING vol (still pumping / double-pump): peak == current every bar → never below
    c·peak → no entry. The trigger must refuse to fade into a rising move."""
    rv = np.linspace(0.1, 2.0, 60)
    assert method_e_entry_offset(rv, 0, c=0.5, d_min=3, debounce_k=2, t_min_bars=6, max_observe=50) is None


def test_method_e_respects_min_gates() -> None:
    """A deep one-bar dip must not trigger when debounce/d_min are unmet; a sustained decay does."""
    rv = _decay_rv(peak_idx=5, n=80, tau=15.0)
    rv[7] = 0.01            # a single deep dip right after the peak
    # debounce k=3 + d_min=4 should ignore the lone dip and enter later on the genuine sustained decay
    entry = method_e_entry_offset(rv, 0, c=0.5, d_min=4, debounce_k=3, t_min_bars=4, max_observe=70)
    assert entry is not None and entry > 7


def test_method_e_c_monotonicity() -> None:
    """A stricter (smaller) c demands more decay → entry no earlier than a looser c."""
    rv = _decay_rv(peak_idx=10, n=120, tau=14.0)
    e_loose = method_e_entry_offset(rv, 0, c=0.7, d_min=2, debounce_k=1, t_min_bars=3, max_observe=90)
    e_strict = method_e_entry_offset(rv, 0, c=0.3, d_min=2, debounce_k=1, t_min_bars=3, max_observe=90)
    assert e_loose is not None and e_strict is not None
    assert e_strict >= e_loose


# --------------------------------------------------------------------------- methods (a)/(b) correctness + causality

def test_expdecay_recovers_known_halflife() -> None:
    """A (near-)pure exp-decay of EXCESS vol recovers H = τ·ln2 with a significantly negative slope.
    (An additive baseline RV_∞ biases the 2-param log-OLS high — the known §4.2 caveat; method (b) fits
    excess vol / drops the floor, so the test series is a near-pure exponential with small log-noise.)"""
    rng = np.random.default_rng(11)
    n, peak_idx, tau = 80, 10, 9.0
    rv = np.empty(n)
    rv[: peak_idx + 1] = np.linspace(0.2, 1.0, peak_idx + 1)            # rise to the peak at peak_idx
    j = np.arange(peak_idx + 1, n)
    rv[peak_idx + 1 :] = np.exp(-(j - peak_idx) / tau) * np.exp(rng.normal(0.0, 0.02, j.size))
    res = estimate_expdecay_halflife(rv, event_idx=0, t=n - 1, n_min=6, t_min=2.0)
    assert res is not None
    halflife, t_stat = res
    assert halflife == pytest.approx(tau * _LN2, rel=0.12)
    assert t_stat < -2.0


def test_expdecay_rejects_constant_vol() -> None:
    """No decay → no significant negative slope → None (timing adds nothing)."""
    rv = np.full(60, 0.5)
    assert estimate_expdecay_halflife(rv, 0, 59, n_min=6) is None


def test_ou_recovers_ar1_halflife() -> None:
    """AR(1) with φ=0.8 → H = −ln2/ln(0.8) ≈ 3.1 bars, recovered by the OLS fit."""
    rng = np.random.default_rng(7)
    n = 800
    x = np.empty(n)
    x[0] = 0.0
    for t in range(1, n):
        x[t] = 0.5 + 0.8 * x[t - 1] + rng.normal(0.0, 0.05)
    res = estimate_ou_halflife(x, min_bars=50, t_min=2.0)
    assert res is not None
    halflife, _t = res
    assert halflife == pytest.approx(-_LN2 / np.log(0.8), rel=0.25)


def test_ou_no_short_halflife_on_random_walk() -> None:
    """A near-random-walk must NOT yield a SHORT (actionable) half-life. The naive |t|≥2 guard can pass
    marginally on a unit root (the AR(1) t-stat is Dickey-Fuller- not normal-distributed, and φ̂ is
    biased slightly below 1), but that gives a LONG H the downstream H_max gate rejects — defense in
    depth. We assert only that no fast-decay claim is produced."""
    rng = np.random.default_rng(8)
    x = np.cumsum(rng.normal(0.0, 1.0, 400))      # φ ≈ 1
    res = estimate_ou_halflife(x, min_bars=50, t_min=2.0)
    assert res is None or res[0] > 8.0


def test_estimators_too_few_bars_return_none() -> None:
    assert estimate_ou_halflife(np.arange(5.0)) is None
    assert estimate_expdecay_halflife(np.array([1.0, 2.0, 3.0]), 0, 2, n_min=6) is None
    assert method_e_entry_offset(np.array([1.0]), 0) is None


# --------------------------------------------------------------------------- dispatcher causality (structural)

@pytest.mark.parametrize("method", ["exp", "ou"])
def test_estimate_vol_halflife_ignores_future_bars(method: str) -> None:
    """estimate_vol_halflife slices to [:t+1], so future bars (even inf/NaN garbage) cannot change it."""
    op, hi, lo, cl = _synthetic_ohlc(160, seed=5)
    t = 110
    base = estimate_vol_halflife(cl, hi, lo, event_bar=20, t=t, method=method, open_=op)
    cl2, hi2, lo2, op2 = cl.copy(), hi.copy(), lo.copy(), op.copy()
    cl2[t + 1 :] = np.inf
    hi2[t + 1 :] = np.inf
    lo2[t + 1 :] = -1.0
    op2[t + 1 :] = np.nan
    after = estimate_vol_halflife(cl2, hi2, lo2, event_bar=20, t=t, method=method, open_=op2)
    assert base == after  # exact: structurally identical inputs over [:t+1]


def test_estimate_vol_halflife_out_of_range_is_none() -> None:
    op, hi, lo, cl = _synthetic_ohlc(40, seed=6)
    assert estimate_vol_halflife(cl, hi, lo, event_bar=0, t=999, open_=op) is None
    assert estimate_vol_halflife(cl, hi, lo, event_bar=30, t=10, open_=op) is None   # event after t


# --------------------------------------------------------------------------- entry wiring (synthetic bars)

def _bars_from_arrays(close: np.ndarray, high: np.ndarray, low: np.ndarray, open_: np.ndarray,
                      ts0: int = 1_700_000_000_000) -> dict:
    """Build a symbol_bars entry in the _indexed_price_bars_by_symbol layout (no data root needed)."""
    n = close.size
    ts = ts0 + np.arange(n, dtype=np.int64) * MS_PER_HOUR
    end = ts + MS_PER_HOUR
    return {"ts_ms": ts, "bar_end_ts_ms": end, "open": open_, "high": high, "low": low, "close": close,
            "ends": end.tolist(), "by_end": {int(e): i for i, e in enumerate(end)}}


def _pump_decay_bars(n: int = 80, event_bar: int = 10, tau: float = 10.0) -> dict:
    """Constant price with a high-low SPREAD that peaks at event_bar then decays -> Parkinson RV peaks at
    event_bar and decays (a clean post-event vol-decay for the method-(e) trigger)."""
    idx = np.arange(n)
    s = np.full(n, 0.001)
    post = idx >= event_bar
    s[post] = np.maximum(0.05 * np.exp(-(idx[post] - event_bar) / tau), 0.001)
    close = np.full(n, 100.0)
    return _bars_from_arrays(close, close * (1.0 + s), close * (1.0 - s), close.copy())


def test_entry_offset_control_enters_at_t_min_bars() -> None:
    bars = _pump_decay_bars()
    rv = rv_series(bars["open"], bars["high"], bars["low"], bars["close"], proxy="parkinson", window=6)
    cfg = HalflifeFadeConfig(disable_halflife_gate=True, t_min_bars=6, max_observe_hours=48)
    assert _entry_offset_for_candidate(rv, 10, cfg) == 16   # event_bar(10) + t_min_bars(6)


def test_entry_offset_method_e_fires_after_decay() -> None:
    bars = _pump_decay_bars(tau=10.0)
    rv = rv_series(bars["open"], bars["high"], bars["low"], bars["close"], proxy="parkinson", window=1)
    cfg = HalflifeFadeConfig(vol_decay_frac=0.5, d_min_hours=3, debounce_k=2, t_min_bars=3, max_observe_hours=48)
    idx = _entry_offset_for_candidate(rv, 10, cfg)
    assert idx is not None and idx >= 10 + 3       # respects t_min_bars; fires once vol decays below c*peak


def test_entry_offset_none_when_still_pumping() -> None:
    n = 60
    s = np.linspace(0.001, 0.1, n)            # monotonically RISING spread -> RV never decays -> no entry
    close = np.full(n, 100.0)
    bars = _bars_from_arrays(close, close * (1.0 + s), close * (1.0 - s), close.copy())
    rv = rv_series(bars["open"], bars["high"], bars["low"], bars["close"], proxy="parkinson", window=1)
    cfg = HalflifeFadeConfig(vol_decay_frac=0.5, d_min_hours=3, debounce_k=2, t_min_bars=3, max_observe_hours=50)
    assert _entry_offset_for_candidate(rv, 5, cfg) is None


def test_halflife_entries_schema_liquidity_gate_one_per_event() -> None:
    bars = _pump_decay_bars()
    event_ts = int(bars["bar_end_ts_ms"][10])
    n = bars["close"].size
    symbol_bars = {"AAA": bars, "BBB": bars}
    turnover = {"AAA": np.full(n, 1e6), "BBB": np.full(n, 1e3)}   # AAA liquid, BBB below the $500k gate
    cand = pl.DataFrame({"symbol": ["AAA", "BBB"], "event_ts": [event_ts, event_ts], "score": [1.0, 2.0]})
    cfg = HalflifeFadeConfig(vol_decay_frac=0.5, d_min_hours=3, debounce_k=2, t_min_bars=3,
                             max_observe_hours=48, liq_turnover_min=500_000.0)
    ent = _halflife_entries(cand, symbol_bars, turnover, cfg)
    assert set(ent.columns) == {"symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts"}
    assert ent["symbol"].to_list() == ["AAA"]        # BBB dropped by the liquidity gate
    assert ent.height == 1                            # one entry per (triggering) event
    assert int(ent["ts_ms"][0]) in {int(t) for t in bars["ts_ms"]}   # sig_ts is a bar START (not bar_end)
    assert float(ent["composite"][0]) == 1.0         # carries the candidate's selection score


def test_halflife_entries_input_lag_never_earlier() -> None:
    """The +1-bar RV input lag must not produce an EARLIER entry (staler inputs -> entry >= the no-lag bar).
    Engine-level companion to the estimator causality tests (plan §7 trap #4)."""
    bars = _pump_decay_bars()
    event_ts = int(bars["bar_end_ts_ms"][10])
    sb = {"AAA": bars}
    tov = {"AAA": np.full(bars["close"].size, 1e6)}
    cand = pl.DataFrame({"symbol": ["AAA"], "event_ts": [event_ts], "score": [1.0]})
    base = HalflifeFadeConfig(vol_decay_frac=0.5, d_min_hours=3, debounce_k=2, t_min_bars=3, max_observe_hours=48)
    e0 = _halflife_entries(cand, sb, tov, base)
    e1 = _halflife_entries(cand, sb, tov, replace(base, halflife_input_lag=1))
    if e0.height and e1.height:
        assert int(e1["ts_ms"][0]) >= int(e0["ts_ms"][0])
