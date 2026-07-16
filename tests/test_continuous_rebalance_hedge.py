"""Hedge-leg tests for the continuous rebalance engine.

Covers decision-time causality, warm-start minimum observations, accounting
reconciliation, gap close/reopen costs, and the no-hedge path.
"""

from __future__ import annotations

import math

from liquidity_migration.continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousHedgeState,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    compute_continuous_hedge_ratio,
    plan_continuous_hedge_resize,
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


def _anti_correlated(n: int = 40) -> tuple[list[float], dict[int, float]]:
    """Book returns anti-correlated with the hedge instrument (short-alt book vs BTC)."""
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    raw = [-0.5 * x + 0.001 for x in h]
    return raw, {d: x for d, x in zip(days, h)}


def test_no_hedge_path_unchanged_schema_and_values() -> None:
    raw = [0.002, -0.001, 0.003, 0.0, 0.001]
    comp = _components(raw)
    df_old = apply_rebalance_rule(comp, _rule())
    df_none = apply_rebalance_rule(comp, _rule(), hedge_rule=None)
    assert df_old.columns == df_none.columns
    assert "hedge_ratio" not in df_old.columns
    assert df_old.equals(df_none)


def test_hedge_ratio_is_causal_in_hedge_return() -> None:
    raw, h = _anti_correlated(40)
    comp = _components(raw)
    days = comp.days
    i = 30
    h2 = dict(h)
    h2[days[i]] = h[days[i]] + 0.05  # perturb ONLY day i's hedge return
    df1 = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h, {})
    df2 = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h2, {})
    # position for day i decided before day i's return exists
    assert df1["hedge_ratio"][i] == df2["hedge_ratio"][i]
    assert df1["hedge_ratio"][: i + 1].equals(df2["hedge_ratio"][: i + 1])
    # day i+1 beta may see the perturbation
    assert df1["hedge_return"][i] != df2["hedge_return"][i]


def test_hedge_ratio_is_causal_in_book_return() -> None:
    raw, h = _anti_correlated(40)
    i = 30
    raw2 = list(raw)
    raw2[i] += 0.05  # perturb ONLY day i's book return
    df1 = apply_rebalance_rule(_components(raw), _rule(), _hedge_rule(), h, {})
    df2 = apply_rebalance_rule(_components(raw2), _rule(), _hedge_rule(), h, {})
    assert df1["hedge_ratio"][i] == df2["hedge_ratio"][i]


def test_warm_start_min_obs() -> None:
    raw, h = _anti_correlated(40)
    df = apply_rebalance_rule(_components(raw), _rule(), _hedge_rule(beta_min_obs=7), h, {})
    assert all(x == 0.0 for x in df["hedge_ratio"][:7])
    assert df["hedge_ratio"][8] > 0.0


def test_long_only_clip() -> None:
    n = 40
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    raw = [+0.5 * x + 0.001 for x in h]  # POSITIVE beta book
    comp = _components(raw, days)
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(), {d: x for d, x in zip(days, h)}, {})
    assert all(x == 0.0 for x in df["hedge_ratio"])


def test_accounting_identity_and_funding_charge() -> None:
    raw, h = _anti_correlated(40)
    comp = _components(raw)
    fund = {d: 0.0002 for d in comp.days}  # positive funding: long hedge pays
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(), h, fund)
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
        assert row["hedge_funding_return"] <= 0.0  # long pays positive funding


def test_gap_close_reopen_turnover_charged() -> None:
    raw, h_map = _anti_correlated(40)
    days = [T0 + i * MS_PER_DAY for i in range(30)] + [
        T0 + (i + 35) * MS_PER_DAY for i in range(10)
    ]  # 5-day flat gap after row 29
    h = {d: v for d, v in zip(days, list(h_map.values())[:40])}
    comp = _components(raw, days)
    df = apply_rebalance_rule(comp, _rule(), _hedge_rule(cost_bps=100.0), h, {})
    ratios = df["hedge_ratio"].to_list()
    costs = df["hedge_cost_return"].to_list()
    assert ratios[29] > 0.0
    # row 30 sits after the gap: close(prev) + reopen(current) both charged
    expected = -(abs(ratios[29]) + abs(ratios[30])) * 100.0 / 10_000.0
    assert math.isclose(costs[30], expected, rel_tol=1e-12)


def test_live_planner_parity_with_backtest_loop() -> None:
    """Same-code gate: the live ratio function fed the engine's prior state must
    reproduce the engine's hedge_ratio for every day."""
    raw, h = _anti_correlated(40)
    comp = _components(raw)
    hr = _hedge_rule()
    df = apply_rebalance_rule(comp, _rule(), hr, h, {})
    days = comp.days
    h_list = [h.get(d) for d in days]
    for i in range(len(days)):
        live = compute_continuous_hedge_ratio(
            ContinuousHedgeState(
                prior_raw_returns=tuple(raw[:i]),
                prior_hedge_returns=tuple(h_list[:i]),
            ),
            hr,
            target_scale=float(df["scale"][i]),
        )
        assert math.isclose(live, float(df["hedge_ratio"][i]), rel_tol=0, abs_tol=1e-15), i


def test_plan_hedge_resize_increase_and_reduce() -> None:
    # increase: no position, target 5% of 10_000 equity at price 100 -> Buy 5 qty
    plan = plan_continuous_hedge_resize(
        hedge_symbol="BTCUSDT", current_qty=0.0, price=100.0, equity_usdt=10_000.0, hedge_ratio=0.05
    )
    assert plan is not None
    assert (plan.side, plan.reduce_only, plan.reason) == ("Buy", False, "hedge_increase")
    assert math.isclose(plan.qty, 5.0)
    # reduce: holding 10 qty, target 2% -> Sell reduce-only 8 qty
    plan = plan_continuous_hedge_resize(
        hedge_symbol="BTCUSDT", current_qty=10.0, price=100.0, equity_usdt=10_000.0, hedge_ratio=0.02
    )
    assert plan is not None
    assert (plan.side, plan.reduce_only, plan.reason) == ("Sell", True, "hedge_reduce")
    assert math.isclose(plan.qty, 8.0)
    # reduce never exceeds the current position
    plan = plan_continuous_hedge_resize(
        hedge_symbol="BTCUSDT", current_qty=1.0, price=100.0, equity_usdt=10_000.0, hedge_ratio=0.0
    )
    assert plan is not None and plan.side == "Sell" and plan.qty <= 1.0


def test_plan_hedge_resize_floors_and_guards() -> None:
    # below the notional floor -> no order
    assert (
        plan_continuous_hedge_resize(
            hedge_symbol="BTCUSDT", current_qty=5.0, price=100.0, equity_usdt=10_000.0,
            hedge_ratio=0.0500001, min_resize_notional_usdt=5.0,
        )
        is None
    )
    # invalid price -> no order
    assert (
        plan_continuous_hedge_resize(
            hedge_symbol="BTCUSDT", current_qty=0.0, price=0.0, equity_usdt=10_000.0, hedge_ratio=0.05
        )
        is None
    )
    # negative ratio treated as 0 (long-only)
    plan = plan_continuous_hedge_resize(
        hedge_symbol="BTCUSDT", current_qty=2.0, price=100.0, equity_usdt=10_000.0, hedge_ratio=-0.5
    )
    assert plan is not None and plan.side == "Sell" and plan.reduce_only


def test_plan_hedge_resize_default_has_no_strategy_side_dollar_floor() -> None:
    plan = plan_continuous_hedge_resize(
        hedge_symbol="XRPUSDT",
        current_qty=0.0,
        price=1.0,
        equity_usdt=10_000.0,
        hedge_ratio=0.0001,
    )

    assert plan is not None
    assert plan.target_notional_usdt == 1.0


def test_beta_extra_lag_changes_estimation_not_position_day() -> None:
    raw, h = _anti_correlated(40)
    comp = _components(raw)
    df0 = apply_rebalance_rule(comp, _rule(), _hedge_rule(beta_extra_lag_days=0), h, {})
    df1 = apply_rebalance_rule(comp, _rule(), _hedge_rule(beta_extra_lag_days=1), h, {})
    # lagged estimate still produces a long hedge on this anti-correlated book
    assert df1["hedge_ratio"][-1] > 0.0
    # and the two differ somewhere (different estimation windows)
    assert df0["hedge_ratio"].to_list() != df1["hedge_ratio"].to_list()


# ---------------------------------------------------------------------------
# Relocated from tests/test_audit_fix_b03.py (sizing-rebalance-1). Reuses the
# module-level `_components`, `_rule`, `_hedge_rule` helpers above.
# ---------------------------------------------------------------------------
def test_hedge_engine_holds_position_on_none_today_like_the_live_twin() -> None:
    """sizing-rebalance-1: when the most-recent hedge return is None (a data-gap day),
    the backtest engine used to report hedge_ratio=0.0 (and charge a phantom
    close+reopen) while the parity-tested live twin holds a fully-sized hedge. The fix
    sizes the engine from beta UNCONDITIONALLY (matching the twin) and only gates the
    realized PnL contribution on the day's value."""
    n = 30
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h_vals = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    raw = [-0.5 * x + 0.001 for x in h_vals]
    comp = _components(raw)
    hr = _hedge_rule()

    # Drop the MOST-RECENT hedge return (None today) — a real, supported live input.
    h_with_gap = {d: v for d, v in zip(days, h_vals)}
    h_with_gap.pop(days[-1])

    df = apply_rebalance_rule(comp, _rule(), hr, h_with_gap, {})
    engine_ratio_last = float(df["hedge_ratio"][-1])

    # The live twin sizes from the prior series alone (no 'today' gate).
    h_list = [h_with_gap.get(d) for d in days]
    twin_last = compute_continuous_hedge_ratio(
        ContinuousHedgeState(
            prior_raw_returns=tuple(raw[: n - 1]),
            prior_hedge_returns=tuple(h_list[: n - 1]),
        ),
        hr,
        target_scale=float(df["scale"][-1]),
    )
    # The engine now HOLDS the hedge on the gap day, matching the twin (was 0.0).
    assert engine_ratio_last > 0.0, "engine must hold the hedge on a None-today gap day"
    assert math.isclose(engine_ratio_last, twin_last, rel_tol=0, abs_tol=1e-12)
    # No realized hedge_return is booked for the missing day (contribution gated).
    assert float(df["hedge_return"][-1]) == 0.0


def test_hedge_engine_unchanged_on_fully_populated_series() -> None:
    """sizing-rebalance-1 guard: the fix must be numerically identical to the prior
    behaviour when every day has a hedge return (the parity-tested contiguous case)."""
    n = 30
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h_vals = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    raw = [-0.5 * x + 0.001 for x in h_vals]
    comp = _components(raw)
    hr = _hedge_rule()
    h = {d: v for d, v in zip(days, h_vals)}

    df = apply_rebalance_rule(comp, _rule(), hr, h, {})
    h_list = [h.get(d) for d in days]
    for i in range(n):
        twin = compute_continuous_hedge_ratio(
            ContinuousHedgeState(
                prior_raw_returns=tuple(raw[:i]),
                prior_hedge_returns=tuple(h_list[:i]),
            ),
            hr,
            target_scale=float(df["scale"][i]),
        )
        assert math.isclose(twin, float(df["hedge_ratio"][i]), rel_tol=0, abs_tol=1e-12), i
