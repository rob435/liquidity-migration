"""Live 2f hedge-manager tests (BTC+ETH legs; 2026-06-10 wiring).

Covers: 2f decision parity with the research live twin, joint cap, ETH-history
fallback to single-leg BTC, per-leg plan generation, warm-start eth_ret loading,
and per-symbol trade-row/link construction.
"""

from __future__ import annotations

import math

import pytest

from liquidity_migration.continuous_hedge_manager import (
    FROZEN_HEDGE_RULE,
    HEDGE_SYMBOL,
    HEDGE_SYMBOL_2,
    ContinuousHedgeConfig,
    compute_hedge_decision_2f,
    load_warmstart_2f,
)
from liquidity_migration.continuous_rebalance import (
    ContinuousHedge2FState,
    compute_continuous_hedge_ratios_2f,
)
from liquidity_migration.continuous_regime import latest_btcvol_intensity


def _two_factor_series(n: int = 120):
    btc = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    eth = [0.012 if i % 3 == 0 else -0.006 for i in range(n)]
    unit = [-0.4 * a - 0.3 * b + 0.0005 for a, b in zip(btc, eth)]
    return unit, btc, eth


def test_2f_decision_matches_live_twin_ratios() -> None:
    unit, btc, eth = _two_factor_series()
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=10.0)  # cap off for parity
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    # The live decision applies the BTC-vol regime-hedge overlay (deployed behavior),
    # so the twin must be fed the same intensity to match (base target_scale = 1.0).
    intensity = latest_btcvol_intensity(btc)
    r1, r2 = compute_continuous_hedge_ratios_2f(
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), FROZEN_HEDGE_RULE, intensity
    )
    assert math.isclose(d.diagnostics["hedge_intensity"], intensity, rel_tol=0, abs_tol=1e-15)
    assert math.isclose(d.ratio_btc, r1, rel_tol=0, abs_tol=1e-15)
    assert math.isclose(d.ratio_eth, r2, rel_tol=0, abs_tol=1e-15)
    assert d.ratio_btc > 0.0 and d.ratio_eth > 0.0
    assert not d.fell_back_to_btc
    assert d.plan_btc is not None and d.plan_btc.symbol == HEDGE_SYMBOL and d.plan_btc.side == "Buy"
    assert d.plan_eth is not None and d.plan_eth.symbol == HEDGE_SYMBOL_2 and d.plan_eth.side == "Buy"


def test_2f_total_cap_scales_legs_proportionally() -> None:
    unit, btc, eth = _two_factor_series()
    unit = [4.0 * u for u in unit]  # big betas
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=0.10)
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.ratio_btc + d.ratio_eth <= 0.10 + 1e-12
    # proportional: ratio of legs preserved vs the uncapped twin
    r1, r2 = compute_continuous_hedge_ratios_2f(
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), FROZEN_HEDGE_RULE, 1.0
    )
    if r2 > 0 and d.ratio_eth > 0:
        assert math.isclose(d.ratio_btc / d.ratio_eth, r1 / r2, rel_tol=1e-9)


def test_2f_falls_back_to_btc_when_eth_history_thin() -> None:
    unit, btc, _ = _two_factor_series()
    eth = [None] * len(unit)  # no ETH history at all
    cfg = ContinuousHedgeConfig()
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.fell_back_to_btc
    assert d.ratio_btc > 0.0
    assert d.ratio_eth == 0.0
    # ETH plan trims to zero target (no position -> no plan)
    assert d.plan_eth is None


def test_load_warmstart_2f_reads_eth_column(tmp_path) -> None:
    p = tmp_path / "w.csv"
    p.write_text(
        "date,unit_ret,btc_ret,eth_ret\n"
        "2025-07-11,0.001,0.01,0.02\n"
        "2025-07-12,-0.002,,\n",
        encoding="utf-8",
    )
    unit, btc, eth = load_warmstart_2f(p)
    assert unit == [0.001, -0.002]
    assert btc == [0.01, None]
    assert eth == [0.02, None]


def test_load_warmstart_2f_backward_compatible_without_eth(tmp_path) -> None:
    p = tmp_path / "w.csv"
    p.write_text("date,unit_ret,btc_ret\n2025-07-11,0.001,0.01\n", encoding="utf-8")
    unit, btc, eth = load_warmstart_2f(p)
    assert unit == [0.001]
    assert btc == [0.01]
    assert eth == [None]


# ==========================================================================
# Relocated from test_audit_fix_b14.py (hedge-1 — 2026-06-14 audit bucket b14).
# Reuses the module-level _two_factor_series helper (identical to the batch's
# _hedge_unit_series).
# ==========================================================================


def test_2f_falls_back_when_trailing_window_eth_thin_but_full_history_deep() -> None:
    """hedge-1: the EXACT shape the original full-series count missed — ETH present
    for the OLD half of the history (full joint >= beta_min_obs) but None for the
    recent beta-window half (window joint < beta_min_obs). The trailing-window beta
    is (0,0); the fallback MUST fire (single-leg BTC) instead of leaving the book
    silently unhedged. The original full-series gate did NOT fire here."""
    n = 200
    unit, btc, eth_full = _two_factor_series(n)
    # ETH known for the first 100 rows, None for the last 100 (the beta window).
    eth = list(eth_full[:100]) + [None] * 100

    # Full-series joint count is large (>= beta_min_obs); the windowed count is 0.
    full_joint = sum(1 for b, e in zip(btc, eth) if b is not None and e is not None)
    assert full_joint >= FROZEN_HEDGE_RULE.beta_min_obs  # the trap condition

    cfg = ContinuousHedgeConfig()
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    # Bug behaviour: no fallback, ratio_btc == ratio_eth == 0 (book unhedged).
    # Fixed behaviour: fall back to single-leg BTC with a non-zero hedge.
    assert d.fell_back_to_btc, "2f hedge silently went to zero (no fallback)"
    assert d.ratio_btc > 0.0
    assert d.ratio_eth == 0.0
    # n_obs_joint must report the WINDOWED count the beta actually used (0 here),
    # not the misleading full-series count.
    assert d.n_obs_joint == 0


def test_2f_full_window_still_matches_live_twin() -> None:
    """hedge-1 guardrail: when the trailing window IS ETH-complete the windowed
    gate must NOT spuriously fall back — the decision still matches the live twin."""
    unit, btc, eth = _two_factor_series(120)
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=10.0)
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    intensity = latest_btcvol_intensity(btc)
    r1, r2 = compute_continuous_hedge_ratios_2f(
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), FROZEN_HEDGE_RULE, intensity,
    )
    assert not d.fell_back_to_btc
    assert d.ratio_btc == pytest.approx(r1, abs=1e-15)
    assert d.ratio_eth == pytest.approx(r2, abs=1e-15)
    assert d.n_obs_joint >= FROZEN_HEDGE_RULE.beta_min_obs
