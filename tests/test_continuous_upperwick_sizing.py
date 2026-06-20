"""Operator override 2026-06-20: vol-gated upper_wick entry sizing.

Receipt: docs/preregistration/2026-06-20-operator-override-upperwick-entry-sizing.md

Guards the SHARED causal multiplier (used by both the backtest full-ledger validator and
the live demo book, so they cannot drift) and the live demo no-op safety: the upper_wick
factor must be exactly 1.0 until the live feature pipeline populates it AND the override
flag is on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_entry_sizing import UPPERWICK_MIN_OBS, upperwick_size_mult  # noqa: E402


def test_coldstart_is_noop_until_min_obs():
    # fewer than min_obs priors -> exactly 1.0 (matches backtest lookup + live cold start)
    for n in range(UPPERWICK_MIN_OBS):
        assert upperwick_size_mult(0.5, 0.01, [0.2] * n, [0.01] * n) == 1.0


def test_high_wick_sizes_up_low_wick_sizes_down():
    prior = [0.20, 0.22, 0.18, 0.25, 0.19, 0.21, 0.23, 0.17, 0.24, 0.20, 0.22, 0.19]
    rv_prior = [0.005] * len(prior)
    # a much wickier-than-usual entry -> mult > 1; a clean entry -> mult < 1 (no vol attenuation: low rv)
    up = upperwick_size_mult(0.40, 0.001, prior, rv_prior, vol_attenuate=False)
    dn = upperwick_size_mult(0.05, 0.001, prior, rv_prior, vol_attenuate=False)
    assert up > 1.0
    assert dn < 1.0


def test_clip_bounds_respected():
    prior = [0.20] * 20
    rv_prior = [0.005] * 20
    # extreme z, no attenuation -> pinned at the clip bounds
    assert upperwick_size_mult(10.0, 0.001, prior, rv_prior, vol_attenuate=False) == pytest.approx(1.5)
    assert upperwick_size_mult(-10.0, 0.001, prior, rv_prior, vol_attenuate=False) == pytest.approx(0.5)


def test_vol_attenuation_tapers_tilt_on_high_vol():
    prior = [0.20, 0.22, 0.18, 0.25, 0.19, 0.21, 0.23, 0.17, 0.24, 0.20, 0.22, 0.19]
    rv_prior = [0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013]
    wick = 0.40  # same wicky entry
    low_vol = upperwick_size_mult(wick, 0.001, prior, rv_prior, vol_attenuate=True)   # att ~ 1 (full tilt)
    high_vol = upperwick_size_mult(wick, 0.099, prior, rv_prior, vol_attenuate=True)  # att ~ 0 (no tilt)
    assert low_vol > high_vol
    assert high_vol == pytest.approx(1.0, abs=1e-9)  # fully attenuated on the highest-vol name


def test_live_multiplier_is_safe_noop_until_enabled_and_populated():
    from liquidity_migration.continuous_demo import (
        ContinuousDemoCycleConfig,
        _continuous_upperwick_multiplier,
        apply_continuous_demo_profile,
    )

    base = apply_continuous_demo_profile(ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2"))
    # default profile: override flag is OFF -> always 1.0 even if a value is present
    assert getattr(base, "entry_upperwick_sizing_enabled", False) is False
    assert _continuous_upperwick_multiplier(base, {"upperwick_size_mult": 1.4}) == 1.0
    # enabled but feature absent (live pipeline not wired) -> 1.0 (safe no-op)
    import dataclasses
    on = dataclasses.replace(base, entry_upperwick_sizing_enabled=True)
    assert _continuous_upperwick_multiplier(on, {}) == 1.0
    assert _continuous_upperwick_multiplier(on, {"upperwick_size_mult": None}) == 1.0
    # enabled + valid feature -> applied
    assert _continuous_upperwick_multiplier(on, {"upperwick_size_mult": 1.4}) == pytest.approx(1.4)
    assert _continuous_upperwick_multiplier(on, {"upperwick_size_mult": 0.6}) == pytest.approx(0.6)
    # enabled + non-positive feature -> 1.0 (guard)
    assert _continuous_upperwick_multiplier(on, {"upperwick_size_mult": 0.0}) == 1.0
