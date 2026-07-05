"""Withdrawn upper_wick entry-sizing audit plumbing.

Guards the shared causal multiplier retained after the duplicate-counting
artifact was found, and the live no-op safety: the upper_wick factor must be
exactly 1.0 unless the disabled flag is explicitly enabled and a finite
multiplier is populated.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_entry_sizing import (  # noqa: E402
    UPPERWICK_MIN_OBS,
    upper_wick_and_rv_from_ohlc,
    upperwick_size_mult,
)


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


def test_feature_computation_handcomputed():
    # 20 bars: open=close=100, high=110, low=90 -> upper_wick=(110-100)/(110-90)=0.5; flat closes -> rv 0
    n = 20
    uw, rv = upper_wick_and_rv_from_ohlc([100.0] * n, [110.0] * n, [90.0] * n, [100.0] * n)
    assert uw == pytest.approx(0.5)
    assert rv == pytest.approx(0.0)
    # bars with no upper wick (close at high) -> upper_wick 0
    uw0, _ = upper_wick_and_rv_from_ohlc([100.0] * n, [110.0] * n, [90.0] * n, [110.0] * n)
    assert uw0 == pytest.approx(0.0)


def test_parse_1m_klines_excludes_forming_bar():
    from liquidity_migration.continuous_upperwick_live import parse_1m_klines
    end = 10 * 60_000
    items = [[str(i * 60_000), "1", "2", "0.5", "1.5"] for i in range(11)]  # bars 0..10
    o, h, low, c = parse_1m_klines(items, end)
    # bar at ts=9*60000 closes at 10*60000 == end -> included; bar at 10*60000 -> excluded (>= end)
    assert len(c) == 10  # bars 0..9
    assert all(x == 2.0 for x in h)


def test_fetch_upper_wick_uses_exact_minute_window():
    from liquidity_migration.continuous_upperwick_live import fetch_upper_wick_rv

    class Client:
        calls = []

        def get_klines(self, symbol, interval, start_ms, end_ms):
            self.calls.append((symbol, interval, start_ms, end_ms))
            return []

    client = Client()
    end_ms = 1_700_000_123_456

    assert fetch_upper_wick_rv(client, "AAAUSDT", end_ms, window_min=30) is None
    assert client.calls == [("AAAUSDT", "1", end_ms - 30 * 60_000, end_ms - 1)]


def test_live_sizer_warmstart_record_and_parity(tmp_path):
    from liquidity_migration.continuous_upperwick_live import UpperwickLiveSizer
    sp = tmp_path / "uw_state.parquet"
    sizer = UpperwickLiveSizer(sp, vol_attenuate=False)
    # warm-start a symbol with 12 priors (uw ~0.2, rv ~0.005); rows are (symbol, signal_ts, uw, rv)
    base = [("AAA", 1000 + s, 0.20 + 0.01 * ((s % 3) - 1), 0.005) for s in range(12)]
    sizer.seed(base)
    # the sizer multiplier == the shared function on the same prior arrays
    prior_uw = [r[2] for r in base]
    prior_rv = [r[3] for r in base]
    m_sizer = sizer.mult_for("AAA", signal_ts=99999, upper_wick=0.40, rv=0.005)
    m_fn = upperwick_size_mult(0.40, 0.005, prior_uw, prior_rv, vol_attenuate=False)
    assert m_sizer == pytest.approx(m_fn)
    assert m_sizer > 1.0  # wickier than its history -> sized up
    # unknown symbol / cold start -> 1.0
    assert sizer.mult_for("ZZZ", 1, 0.5, 0.01) == 1.0
    # record + save round-trips
    sizer.record("AAA", 99999, 0.40, 0.005)
    sizer.save()
    reloaded = UpperwickLiveSizer(sp, vol_attenuate=False)
    # the recorded entry is now PRIOR to a later signal_ts
    assert reloaded.mult_for("AAA", 100000, 0.40, 0.005) == pytest.approx(m_sizer)
