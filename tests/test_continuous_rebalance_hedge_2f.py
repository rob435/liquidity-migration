"""Two-leg (BTC+ETH) hedge tests for the continuous rebalance engine.
Covers: unchanged unhedged/single-leg paths, per-leg causality, joint cap
proportionality, collinearity fallback, warm-start, accounting identity,
gap close/reopen, and live-twin parity.
"""

from __future__ import annotations

import math

from liquidity_migration.research.backtest.continuous_rebalance import (
    ContinuousHedge2FState,
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    compute_continuous_hedge_ratios_2f,
    compute_hedge_betas_2f,
)

MS_PER_DAY = 86_400_000
T0 = 1_680_652_800_000  # 2023-04-05 00:00 UTC


def _components(raw: list[float], days: list[int] | None = None) -> ContinuousRebalanceComponents:
    if days is None:
        days = [T0 + i * MS_PER_DAY for i in range(len(raw))]
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


def _hedge_rule(**kw) -> ContinuousHedgeRule:
    base = dict(beta_window_days=10, beta_min_obs=5, hedge_cap=2.0, cost_bps=5.0)
    base.update(kw)
    return ContinuousHedgeRule(**base)


def _two_factor_world(n: int = 40) -> tuple[list[float], dict[int, float], dict[int, float]]:
    """Book anti-correlated with two NON-collinear instruments (period 2 and 3)."""
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h1 = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    h2 = [0.012 if i % 3 == 0 else -0.006 for i in range(n)]
    raw = [-0.4 * a - 0.3 * b + 0.0005 for a, b in zip(h1, h2)]
    return raw, {d: x for d, x in zip(days, h1)}, {d: x for d, x in zip(days, h2)}


def test_unhedged_and_single_leg_paths_unchanged() -> None:
    raw, h1, _h2 = _two_factor_world(40)
    comp = _components(raw)
    df_unhedged_a = apply_rebalance_rule(comp, _rule())
    df_unhedged_b = apply_rebalance_rule(comp, _rule(), None, None, None, None, None)
    assert df_unhedged_a.equals(df_unhedged_b)
    df_single_a = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {})
    df_single_b = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, None, None)
    assert df_single_a.equals(df_single_b)
    assert "hedge_ratio_leg1" not in df_single_a.columns


def test_two_leg_schema_and_recovers_both_betas() -> None:
    raw, h1, h2 = _two_factor_world(60)
    comp = _components(raw)
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, h2, {})
    assert {"hedge_ratio", "hedge_ratio_leg1", "hedge_ratio_leg2"} <= set(df.columns)
    # both legs long on this anti-correlated book once warm
    assert df["hedge_ratio_leg1"][-1] > 0.0
    assert df["hedge_ratio_leg2"][-1] > 0.0
    assert math.isclose(
        df["hedge_ratio"][-1],
        df["hedge_ratio_leg1"][-1] + df["hedge_ratio_leg2"][-1],
        rel_tol=0,
        abs_tol=1e-15,
    )
    # betas recovered near the construction (-0.4, -0.3) -> ratios ~ 0.4*scale, 0.3*scale
    i = 30
    s = float(df["scale"][i])
    assert abs(df["hedge_ratio_leg1"][i] / s - 0.4) < 0.05
    assert abs(df["hedge_ratio_leg2"][i] / s - 0.3) < 0.05


def test_per_leg_causality_day_i_perturbation() -> None:
    raw, h1, h2 = _two_factor_world(40)
    comp = _components(raw)
    days = comp.days
    i = 30
    for which in (1, 2):
        ha, hb = dict(h1), dict(h2)
        (ha if which == 1 else hb)[days[i]] = (h1 if which == 1 else h2)[days[i]] + 0.05
        df1 = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, h2, {})
        df2 = apply_rebalance_rule(comp, _rule(), _hedge_rule(), ha, {}, hb, {})
        for col in ("hedge_ratio_leg1", "hedge_ratio_leg2"):
            assert df1[col][i] == df2[col][i], (which, col)
            assert df1[col][: i + 1].equals(df2[col][: i + 1]), (which, col)
        assert df1["hedge_return"][i] != df2["hedge_return"][i]
    # book-return perturbation on day i must not move day i's ratios either
    raw2 = list(raw)
    raw2[i] += 0.05
    df3 = apply_rebalance_rule(_components(raw2), _rule(), _hedge_rule(), h1, {}, h2, {})
    df0 = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, h2, {})
    assert df0["hedge_ratio_leg1"][i] == df3["hedge_ratio_leg1"][i]
    assert df0["hedge_ratio_leg2"][i] == df3["hedge_ratio_leg2"][i]


def test_joint_cap_proportional() -> None:
    # strongly anti-correlated book to BOTH legs forces big betas; tiny cap binds
    raw, h1, h2 = _two_factor_world(40)
    raw_big = [4.0 * x for x in raw]
    comp = _components(raw_big)
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(hedge_cap=0.5), h1, {}, h2, {})
    for row in df.to_dicts():
        cap_tot = 0.5 * row["scale"] + 1e-12
        assert row["hedge_ratio_leg1"] + row["hedge_ratio_leg2"] <= cap_tot
    # when the cap binds, both legs are shrunk by the same factor (proportional)
    i = 30
    b1, b2 = compute_hedge_betas_2f(
        raw_big, [h1[d] for d in comp.days], [h2[d] for d in comp.days], i, _hedge_rule(hedge_cap=0.5)
    )
    raw_r1 = min(max(-b1, 0.0), 0.5)
    raw_r2 = min(max(-b2, 0.0), 0.5)
    if raw_r1 > 0 and raw_r2 > 0:
        got1, got2 = float(df["hedge_ratio_leg1"][i]), float(df["hedge_ratio_leg2"][i])
        assert math.isclose(got1 / got2, raw_r1 / raw_r2, rel_tol=1e-9)


def test_collinearity_fallback_matches_single_leg() -> None:
    raw, h1, _ = _two_factor_world(40)
    comp = _components(raw)
    df_single = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {})
    df_two = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, {}, dict(h1), {})  # leg2 == leg1
    assert all(x == 0.0 for x in df_two["hedge_ratio_leg2"].to_list())
    # fallback beta is sy1/s11 vs the single-leg (cov/n)/(var/n) — identical math,
    # last-bit float order may differ (repo numerical-equivalence rule)
    for a, b in zip(df_two["hedge_ratio"].to_list(), df_single["hedge_ratio"].to_list()):
        assert math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
    for a, b in zip(df_two["basket_return"].to_list(), df_single["basket_return"].to_list()):
        assert math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)


def test_warm_start_min_joint_obs() -> None:
    raw, h1, h2 = _two_factor_world(40)
    comp = _components(raw)
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(beta_min_obs=7), h1, {}, h2, {})
    assert all(x == 0.0 for x in df["hedge_ratio"][:7])
    assert df["hedge_ratio"][8] > 0.0


def test_accounting_identity_two_leg() -> None:
    raw, h1, h2 = _two_factor_world(40)
    comp = _components(raw)
    f1 = {d: 0.0002 for d in comp.days}
    f2 = {d: 0.0001 for d in comp.days}
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h1, f1, h2, f2)
    eq = 1.0
    for row in df.to_dicts():
        total = (
            row["gross_return"]
            + row["funding_return"]
            + row["entry_cost_return"]
            + row["resize_cost_return"]
            + row["hedge_return"]
            + row["hedge_funding_return"]
            + row["hedge_cost_return"]
        )
        assert math.isclose(row["basket_return"], total, rel_tol=0, abs_tol=1e-15)
        eq *= 1.0 + row["basket_return"]
        assert math.isclose(row["equity"], eq, rel_tol=1e-12)
        assert row["hedge_funding_return"] <= 0.0  # long legs pay positive funding


def test_gap_close_reopen_turnover_charged_two_leg() -> None:
    raw, h1_map, h2_map = _two_factor_world(40)
    days = [T0 + i * MS_PER_DAY for i in range(30)] + [
        T0 + (i + 35) * MS_PER_DAY for i in range(10)
    ]
    h1 = {d: v for d, v in zip(days, list(h1_map.values())[:40])}
    h2 = {d: v for d, v in zip(days, list(h2_map.values())[:40])}
    comp = _components(raw, days)
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(cost_bps=100.0), h1, {}, h2, {})
    l1 = df["hedge_ratio_leg1"].to_list()
    l2 = df["hedge_ratio_leg2"].to_list()
    costs = df["hedge_cost_return"].to_list()
    assert l1[29] + l2[29] > 0.0
    expected = -(abs(l1[29]) + abs(l1[30]) + abs(l2[29]) + abs(l2[30])) * 100.0 / 10_000.0
    assert math.isclose(costs[30], expected, rel_tol=1e-12)


def test_live_planner_parity_two_leg() -> None:
    """Same-code gate: the live per-leg ratio function fed the engine's prior state
    must reproduce the engine's per-leg ratios for every day."""
    raw, h1, h2 = _two_factor_world(40)
    comp = _components(raw)
    hr = _hedge_rule()
    df = apply_rebalance_rule(comp, _rule(), hr, h1, {}, h2, {})
    days = comp.days
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
            target_scale=float(df["scale"][i]),
        )
        assert math.isclose(live1, float(df["hedge_ratio_leg1"][i]), rel_tol=0, abs_tol=1e-15), i
        assert math.isclose(live2, float(df["hedge_ratio_leg2"][i]), rel_tol=0, abs_tol=1e-15), i


def _aligned(raw, h1, h2):
    days = sorted(h1)[: len(raw)]
    return [h1[d] for d in days], [h2[d] for d in days]


def test_shrinkage_weight_zero_reproduces_plain_ols() -> None:
    raw, h1, h2 = _two_factor_world(40)
    h1, h2 = _aligned(raw, h1, h2)
    plain = compute_hedge_betas_2f(raw, h1, h2, len(raw), _hedge_rule())
    shrunk_zero = compute_hedge_betas_2f(
        raw,
        h1,
        h2,
        len(raw),
        _hedge_rule(shrinkage_weight=0.0, prior_beta_1=-9.0, prior_beta_2=9.0),
    )
    assert shrunk_zero == plain


def test_shrinkage_weight_one_returns_prior_when_estimable() -> None:
    raw, h1, h2 = _two_factor_world(40)
    h1, h2 = _aligned(raw, h1, h2)
    b1, b2 = compute_hedge_betas_2f(
        raw,
        h1,
        h2,
        len(raw),
        _hedge_rule(shrinkage_weight=1.0, prior_beta_1=-0.7, prior_beta_2=-0.2),
    )
    assert math.isclose(b1, -0.7, rel_tol=1e-12)
    assert math.isclose(b2, -0.2, rel_tol=1e-12)


def test_shrinkage_blends_linearly_between_ols_and_prior() -> None:
    raw, h1, h2 = _two_factor_world(40)
    h1, h2 = _aligned(raw, h1, h2)
    ols1, ols2 = compute_hedge_betas_2f(raw, h1, h2, len(raw), _hedge_rule())
    mid1, mid2 = compute_hedge_betas_2f(
        raw,
        h1,
        h2,
        len(raw),
        _hedge_rule(shrinkage_weight=0.4, prior_beta_1=-1.0, prior_beta_2=0.5),
    )
    assert math.isclose(mid1, 0.6 * ols1 + 0.4 * -1.0, rel_tol=1e-12)
    assert math.isclose(mid2, 0.6 * ols2 + 0.4 * 0.5, rel_tol=1e-12)


def test_shrinkage_never_replaces_an_unestimable_beta() -> None:
    # Below beta_min_obs the estimator must stay at (0, 0) — no blind prior.
    raw, h1, h2 = _two_factor_world(4)
    h1, h2 = _aligned(raw, h1, h2)
    b1, b2 = compute_hedge_betas_2f(
        raw,
        h1,
        h2,
        len(raw),
        _hedge_rule(shrinkage_weight=1.0, prior_beta_1=-0.7, prior_beta_2=-0.2),
    )
    assert (b1, b2) == (0.0, 0.0)


def test_shrinkage_weight_out_of_range_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="shrinkage_weight"):
        _hedge_rule(shrinkage_weight=1.5)
