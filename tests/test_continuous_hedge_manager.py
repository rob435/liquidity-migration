"""Tests for the live BTC-beta hedge manager (WP3 live wiring, 2026-06-10)."""

from __future__ import annotations

from pathlib import Path

from liquidity_migration.continuous_hedge_manager import (
    ContinuousHedgeConfig,
    compute_hedge_decision,
    load_warmstart_2f,
)


def _anti_correlated(n: int = 120) -> tuple[list[float], list[float | None]]:
    """Book unit returns anti-correlated with BTC (short-alt book vs BTC long).

    beta ~ -0.2 keeps the full-gross hedge ratio (0.2) under the 0.30 equity cap so
    the proportionality property is testable without the clip interfering."""
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    unit = [-0.2 * float(b) + 0.0005 for b in btc]  # beta ~ -0.2
    return unit, btc


def test_hedge_is_long_only_and_sized_to_gross() -> None:
    unit, btc = _anti_correlated(120)
    cfg = ContinuousHedgeConfig()
    # full-gross book -> hedge ratio = clip(-beta,0,2)*1.0
    full = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.5,
        btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert full.hedge_ratio_equity_frac > 0.0  # anti-correlated book -> long BTC hedge
    assert full.plan is not None and full.plan.side == "Buy" and not full.plan.reduce_only
    # half-deployed book -> half the hedge
    half = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.25,
        btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert abs(half.hedge_ratio_equity_frac - 0.5 * full.hedge_ratio_equity_frac) < 1e-9


def test_positive_beta_book_takes_no_hedge() -> None:
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(120)]
    unit = [0.4 * float(b) for b in btc]  # POSITIVE beta -> long-only clip = 0
    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.hedge_ratio_equity_frac == 0.0
    assert d.plan is None  # nothing to buy from flat


def test_hard_equity_cap() -> None:
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(120)]
    unit = [-3.0 * float(b) for b in btc]  # huge negative beta -> ratio would blow past cap
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=0.30)
    d = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.5,
        btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.hedge_ratio_equity_frac == 0.30  # capped


def test_reduce_when_overhedged() -> None:
    unit, btc = _anti_correlated(120)
    d = compute_hedge_decision(
        ContinuousHedgeConfig(),
        unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.5,
        btc_price=100_000.0, current_hedge_qty=1.0, equity_usdt=10_000.0,  # 100k held >> target
    )
    assert d.plan is not None and d.plan.side == "Sell" and d.plan.reduce_only


def test_strategy_does_not_suppress_sub_25_dollar_resize() -> None:
    unit, btc = _anti_correlated(120)
    baseline = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0,
        current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    target_qty = baseline.target_notional_usdt / 100_000.0
    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0,
        current_hedge_qty=target_qty - 0.00001, equity_usdt=10_000.0,
    )
    assert d.plan is not None
    assert 0.0 < abs(d.plan.delta_notional_usdt) < 25.0


def test_min_obs_no_hedge_before_warmup() -> None:
    unit, btc = _anti_correlated(40)  # < beta_min_obs (60)
    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.hedge_ratio_equity_frac == 0.0  # beta=0 until min_obs satisfied


def test_n_obs_counts_beta_window_only() -> None:
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(150)]
    for i in range(60, 91):
        btc[i] = None
    unit = [-0.2 * float(b) if b is not None else 0.0 for b in btc]

    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )

    assert d.n_obs == 59
    assert d.diagnostics["beta_window_observations"] == 59
    assert d.hedge_ratio_equity_frac == 0.0


def test_shipped_warmstart_artifacts_exist_and_load() -> None:
    repo = Path(__file__).resolve().parent.parent
    for venue in ("bybit", "binance"):
        p = repo / "deploy" / "hedge_warmstart" / f"{venue}_warmstart.csv"
        assert p.exists(), f"missing warm-start artifact: {p}"
        unit, btc, _eth = load_warmstart_2f(p)
        assert len(unit) >= 90, f"{venue} warm-start shorter than the 90d beta window"
        assert len(unit) == len(btc)
        assert sum(1 for b in btc if b is not None) >= 60  # enough for beta_min_obs


def test_frozen_hedge_rule_derives_from_forward_config() -> None:
    """audit-iter6: the live hedge manager's FROZEN_HEDGE_RULE must equal the single
    source of truth (FROZEN_FORWARD_CONFIG via frozen_hedge_rule), so it can't drift
    from the forward ledger it mirrors."""
    from liquidity_migration.continuous_forward_replay import frozen_hedge_rule
    from liquidity_migration.continuous_hedge_manager import FROZEN_HEDGE_RULE

    assert FROZEN_HEDGE_RULE == frozen_hedge_rule()
