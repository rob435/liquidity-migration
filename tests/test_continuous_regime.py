"""Causality and runtime/historical parity tests for the BTC-vol hedge regime."""

from __future__ import annotations

import math

from liquidity_migration.research.backtest.continuous_profile import (
    ACTIVE_CONTINUOUS_CONFIG,
    active_hedge_regime,
)
from liquidity_migration.research.backtest.continuous_rebalance import (
    ContinuousHedge2FState,
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    compute_continuous_hedge_ratios_2f,
)
from liquidity_migration.research.backtest.continuous_regime import (
    ACTIVE_BTCVOL_REGIME,
    PCT_WARMUP,
    btcvol_intensity_series,
    latest_btcvol_intensity,
)

MS_PER_DAY = 86_400_000
T0 = 1_680_652_800_000  # 2023-04-05 00:00 UTC
LAM = ACTIVE_BTCVOL_REGIME["lam"]
VOL_WINDOW = ACTIVE_BTCVOL_REGIME["vol_window"]
PCT_WINDOW = ACTIVE_BTCVOL_REGIME["pct_window"]


def _pseudo_returns(n: int) -> list[float]:
    """Deterministic, sign-varying, vol-clustered BTC-like return series."""
    out = []
    for i in range(n):
        amp = 0.005 + 0.02 * ((i // 37) % 3)  # vol regimes that shift every 37 days
        out.append(amp * math.sin(i * 1.7) - 0.0003 * math.cos(i * 0.3))
    return out


def _days(n: int) -> list[int]:
    return [T0 + i * MS_PER_DAY for i in range(n)]


# ---------------------------------------------------------------------------
# Signal properties
# ---------------------------------------------------------------------------


def test_intensity_bounds_and_mean_one_symmetry() -> None:
    n = 600
    days = _days(n)
    rets = {d: r for d, r in zip(days, _pseudo_returns(n))}
    intensity = btcvol_intensity_series(days, rets, LAM, VOL_WINDOW, PCT_WINDOW)
    # Every value within the mean-1 band [1-lam, 1+lam].
    for v in intensity.values():
        assert 1.0 - LAM - 1e-12 <= v <= 1.0 + LAM + 1e-12
    # The active (post-warmup) intensities are a percentile-rank reflection about
    # 1.0, so their mean sits near 1.0 (mean-1 reallocation, not a bigger hedge).
    active = [v for v in intensity.values() if v != 1.0]
    assert active, "expected some post-warmup modulation"
    assert abs(sum(active) / len(active) - 1.0) < 0.15


def test_intensity_warmup_is_neutral() -> None:
    n = 120
    days = _days(n)
    rets = {d: r for d, r in zip(days, _pseudo_returns(n))}
    intensity = btcvol_intensity_series(days, rets, LAM, VOL_WINDOW, PCT_WINDOW)
    # Until PCT_WARMUP vol observations accumulate the overlay is exactly neutral.
    assert intensity[days[0]] == 1.0
    assert intensity[days[PCT_WARMUP - 1]] == 1.0


def test_intensity_is_causal_no_lookahead() -> None:
    """intensity[d] must depend only on returns strictly before d: mutating a
    future return cannot move any intensity at or before that index."""
    n = 400
    days = _days(n)
    base = _pseudo_returns(n)
    cut = 250
    a = btcvol_intensity_series(days, {d: r for d, r in zip(days, base)}, LAM, VOL_WINDOW, PCT_WINDOW)
    bumped = list(base)
    bumped[cut] += 0.5  # a huge shock AT index `cut`
    b = btcvol_intensity_series(days, {d: r for d, r in zip(days, bumped)}, LAM, VOL_WINDOW, PCT_WINDOW)
    for i in range(0, cut + 1):  # days <= cut use only returns < cut
        assert a[days[i]] == b[days[i]], i
    # And it DOES change something afterwards (the signal is not inert).
    assert any(a[days[i]] != b[days[i]] for i in range(cut + 1, n))


def test_intensity_deterministic() -> None:
    n = 300
    days = _days(n)
    rets = {d: r for d, r in zip(days, _pseudo_returns(n))}
    assert btcvol_intensity_series(days, rets) == btcvol_intensity_series(days, rets)


def test_latest_matches_series_for_every_day() -> None:
    """The live manager's ``latest_btcvol_intensity(prior_returns)`` must equal the
    historical ``btcvol_intensity_series(...)[today]`` for that same day."""
    n = 360
    rets_list = _pseudo_returns(n)
    days = list(range(n))
    series = btcvol_intensity_series(days, {i: r for i, r in enumerate(rets_list)}, LAM, VOL_WINDOW, PCT_WINDOW)
    for k in range(n):
        live = latest_btcvol_intensity(rets_list[:k], LAM, VOL_WINDOW, PCT_WINDOW)
        assert live == series[k], k


def test_latest_handles_gaps_and_empty() -> None:
    assert latest_btcvol_intensity([]) == 1.0
    assert latest_btcvol_intensity([0.01] * 5) == 1.0  # below warmup -> neutral
    # None gap days are skipped, not treated as zero returns.
    series = [0.01 if i % 2 else None for i in range(120)]
    val = latest_btcvol_intensity(series, LAM, VOL_WINDOW, PCT_WINDOW)
    assert 1.0 - LAM - 1e-12 <= val <= 1.0 + LAM + 1e-12


# ---------------------------------------------------------------------------
# Engine wiring: intensity scales BOTH 2f legs; off == plain hedge
# ---------------------------------------------------------------------------


def _components(raw: list[float], days: list[int]) -> ContinuousRebalanceComponents:
    raw_by_day = {d: r for d, r in zip(days, raw)}
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day=raw_by_day,
        gross_by_day=dict(raw_by_day),
        cost_events={},
        funding_by_day={},
        active_gross_start={d: 0.0 for d in days},
        impact_exponent=0.5,
    )


def _rule() -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=90,
        target_daily_vol=0.045,
        max_scale=4.0,
        drawdown_half_threshold=-0.04,
        drawdown_zero_threshold=None,
        resize_cost_bps=10.0,
        strategy_momentum_window_days=0,
    )


def _hedge_rule() -> ContinuousHedgeRule:
    return ContinuousHedgeRule(beta_window_days=10, beta_min_obs=5, hedge_cap=2.0, cost_bps=5.0)


def _two_factor_world(n: int):
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h1 = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    h2 = [0.012 if i % 3 == 0 else -0.006 for i in range(n)]
    raw = [-0.4 * a - 0.3 * b + 0.0005 for a, b in zip(h1, h2)]
    return days, raw, {d: x for d, x in zip(days, h1)}, {d: x for d, x in zip(days, h2)}


def test_intensity_none_is_byte_identical() -> None:
    days, raw, h1, h2 = _two_factor_world(40)
    comp = _components(raw, days)
    plain = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, h2, {})
    none_i = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, h2, {}, hedge_intensity=None)
    ones = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, h2, {}, hedge_intensity={d: 1.0 for d in days})
    assert plain.equals(none_i)
    assert plain.equals(ones)


def test_runtime_historical_parity_with_intensity_two_leg() -> None:
    """The runtime calculator must reproduce historical per-leg ratios."""
    days, raw, h1, h2 = _two_factor_world(40)
    comp = _components(raw, days)
    hr = _hedge_rule()
    intensity = {d: 1.0 + LAM * (0.5 if i % 2 else -0.5) for i, d in enumerate(days)}
    df = apply_rebalance_rule(comp, _rule(), hr, h1, {}, h2, {}, hedge_intensity=intensity)
    h1_list = [h1.get(d) for d in days]
    h2_list = [h2.get(d) for d in days]
    for i in range(len(days)):
        live1, live2 = compute_continuous_hedge_ratios_2f(
            ContinuousHedge2FState(
                prior_raw_returns=tuple(raw[:i]),
                prior_hedge_returns_1=tuple(h1_list[:i]),
                prior_hedge_returns_2=tuple(h2_list[:i]),
            ),
            hr,
            target_scale=float(df["scale"][i]) * intensity[days[i]],
        )
        assert math.isclose(live1, float(df["hedge_ratio_leg1"][i]), rel_tol=0, abs_tol=1e-15), i
        assert math.isclose(live2, float(df["hedge_ratio_leg2"][i]), rel_tol=0, abs_tol=1e-15), i


def test_regime_is_embedded_in_active_config() -> None:
    assert active_hedge_regime() == ACTIVE_BTCVOL_REGIME
    assert ACTIVE_CONTINUOUS_CONFIG["hedge"]["regime"] == ACTIVE_BTCVOL_REGIME
