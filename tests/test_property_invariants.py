"""Generative (stdlib-random) invariant tests for the core financial/PIT math.

A new bug-finding method (continuous-audit-loop iter-8, 2026-06-18): rather than
example-based tests, generate many random inputs and assert mathematical invariants
that must hold for ANY input. This catches edge cases static review and fixed-fixture
tests miss, and permanently hardens the net. Pure standard library — no new dependency
(the repo's dev deps are intentionally minimal; `random.Random` is already used in the
suite, e.g. test_r1_robustness).
"""
from __future__ import annotations

import math
import random

import polars as pl

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousHedgeState,
    compute_continuous_hedge_ratio,
)
from liquidity_migration.ingestion import normalize_funding_history
from liquidity_migration.trade_lifecycle import _daily_sharpe, annualized_sharpe

MS_PER_HOUR = MS_PER_DAY // 24


def test_annualized_sharpe_scale_invariant_and_sign_flip() -> None:
    """Sharpe = mean/std is invariant to a positive scale and negates under a sign flip."""
    rng = random.Random(11)
    for _ in range(400):
        n = rng.randint(2, 40)
        xs = [rng.uniform(-0.1, 0.1) for _ in range(n)]
        base = annualized_sharpe(xs)
        if not math.isfinite(base) or base == 0.0:
            continue
        k = rng.uniform(0.01, 100.0)
        assert math.isclose(annualized_sharpe([k * x for x in xs]), base, rel_tol=1e-9), (k, xs)
        assert math.isclose(annualized_sharpe([-x for x in xs]), -base, rel_tol=1e-9), xs


def test_annualized_sharpe_degenerate_is_zero() -> None:
    assert annualized_sharpe([0.05]) == 0.0          # < 2 points
    assert annualized_sharpe([0.03, 0.03, 0.03]) == 0.0  # zero variance
    assert annualized_sharpe([]) == 0.0


def test_daily_sharpe_invariant_to_intraday_exit_hour() -> None:
    """_daily_sharpe must depend only on the calendar day + equity, NEVER the intra-day
    wall-clock hour of the exit. The iter-1 bug (intraday-stamped grid) violated this."""
    rng = random.Random(23)
    for _ in range(200):
        ndays = rng.randint(2, 30)
        days = sorted(rng.sample(range(0, 400), ndays))  # distinct calendar days, random gaps
        eqs, v = [], 1.0
        for _ in range(ndays):
            v *= 1.0 + rng.uniform(-0.05, 0.05)
            eqs.append(v)

        def _df(hours: list[int]) -> pl.DataFrame:
            return pl.DataFrame({
                "ts_ms": [d * MS_PER_DAY + h * MS_PER_HOUR for d, h in zip(days, hours)],
                "equity": eqs,
            })

        s_midnight = _daily_sharpe(_df([0] * ndays))
        s_random = _daily_sharpe(_df([rng.randint(0, 23) for _ in range(ndays)]))
        assert math.isclose(s_midnight, s_random, rel_tol=1e-9, abs_tol=1e-12), (days, eqs)


def test_normalize_funding_8h_equiv_finite_and_correct() -> None:
    """funding_rate_8h_equiv is always finite (0/negative interval clamps to the 8h
    default) and equals funding_rate * 480/clamped_interval (audit-iter6 fix)."""
    rng = random.Random(7)
    rows, expected = [], {}
    for i in range(500):
        sym = f"S{i}"
        rate = rng.uniform(-0.01, 0.01)
        c = rng.random()
        interval = 0 if c < 0.2 else (-rng.randint(1, 600) if c < 0.4 else rng.choice([60, 120, 240, 480, 720]))
        rows.append({"ts_ms": i, "symbol": sym, "funding_rate": rate, "funding_interval_min": interval})
        clamped = interval if interval > 0 else 480
        expected[sym] = rate * (480.0 / clamped)
    out = normalize_funding_history(pl.DataFrame(rows))
    for r in out.iter_rows(named=True):
        assert math.isfinite(r["funding_rate_8h_equiv"]), r
        assert math.isclose(r["funding_rate_8h_equiv"], expected[r["symbol"]], rel_tol=1e-9, abs_tol=1e-15), r


def test_hedge_ratio_bounded_by_cap_times_scale() -> None:
    """compute_continuous_hedge_ratio == clip(-beta,0,cap)*max(scale,0): always finite,
    in [0, cap*max(scale,0)], and exactly 0 for a non-positive target scale."""
    rng = random.Random(5)
    for _ in range(400):
        n = rng.randint(0, 120)
        raw = [rng.uniform(-0.1, 0.1) for _ in range(n)]
        hs = [rng.uniform(-0.1, 0.1) for _ in range(n)]
        cap = rng.uniform(0.5, 4.0)
        rule = ContinuousHedgeRule(
            beta_window_days=90, beta_min_obs=rng.randint(5, 60), hedge_cap=cap, cost_bps=5.0
        )
        scale = rng.uniform(-1.0, 4.0)
        state = ContinuousHedgeState(prior_raw_returns=tuple(raw), prior_hedge_returns=tuple(hs))
        r = compute_continuous_hedge_ratio(state, rule, scale)
        assert math.isfinite(r), (n, cap, scale)
        assert 0.0 <= r <= cap * max(scale, 0.0) + 1e-12, (r, cap, scale)
        if scale <= 0.0:
            assert r == 0.0
