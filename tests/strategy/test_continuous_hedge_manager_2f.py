"""Tests for the active BTC+ETH continuous hedge manager."""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from liquidity_migration.strategy.continuous_hedge_manager import (
    ACTIVE_HEDGE_RULE,
    HEDGE_SYMBOL,
    HEDGE_SYMBOL_2,
    ContinuousHedgeConfig,
    compute_hedge_decision_2f,
    load_hedge_model_prior,
    require_usable_hedge_model_prior,
)
from liquidity_migration.research.backtest.continuous_rebalance import (
    ContinuousHedge2FState,
    compute_continuous_hedge_ratios_2f,
)
from liquidity_migration.research.backtest.continuous_regime import latest_btcvol_intensity


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
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), ACTIVE_HEDGE_RULE, intensity
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
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), ACTIVE_HEDGE_RULE, 1.0
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


def test_load_hedge_model_prior_reads_eth_column_and_provenance(tmp_path) -> None:
    p = tmp_path / "w.csv"
    source_hash = "a" * 64
    p.write_text(
        "date,unit_ret,btc_ret,eth_ret,data_through_date,source_summary_sha256\n"
        f"2025-07-11,0.001,0.01,0.02,2025-07-12,{source_hash}\n"
        f"2025-07-12,-0.002,,,2025-07-12,{source_hash}\n",
        encoding="utf-8",
    )
    prior = load_hedge_model_prior(p)
    assert prior.unit_returns == (0.001, -0.002)
    assert prior.btc_returns == (0.01, None)
    assert prior.eth_returns == (0.02, None)
    assert prior.data_through_date == date(2025, 7, 12)
    assert prior.source_summary_sha256 == source_hash
    assert len(prior.artifact_sha256) == 64


def test_load_hedge_model_prior_rejects_unprovenanced_schema(tmp_path) -> None:
    p = tmp_path / "w.csv"
    p.write_text("date,unit_ret,btc_ret\n2025-07-11,0.001,0.01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected schema"):
        load_hedge_model_prior(p)


def test_shipped_model_prior_is_canonical_and_usable() -> None:
    path = Path(__file__).resolve().parents[2] / "deploy" / "hedge_warmstart" / "bybit_warmstart.csv"
    prior = require_usable_hedge_model_prior(
        load_hedge_model_prior(path),
        as_of_date=date(2026, 7, 16),
    )
    assert len(prior.unit_returns) >= 90
    assert len(prior.unit_returns) == len(prior.btc_returns) == len(prior.eth_returns)
    assert sum(value is not None for value in prior.btc_returns) >= 60
    assert sum(value is not None for value in prior.eth_returns) >= 60
    assert prior.provenance()["model_prior_live_extension"] is False


def test_model_prior_runtime_gate_rejects_future_and_too_short_inputs(tmp_path) -> None:
    source_hash = "a" * 64
    start = date(2026, 1, 1)
    rows = [
        f"{(start + timedelta(days=index)).isoformat()},0.001,0.01,0.02,2026-02-28,{source_hash}"
        for index in range(59)
    ]
    p = tmp_path / "short.csv"
    p.write_text(
        "date,unit_ret,btc_ret,eth_ret,data_through_date,source_summary_sha256\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    prior = load_hedge_model_prior(p)
    with pytest.raises(ValueError, match="59 usable rows"):
        require_usable_hedge_model_prior(prior, as_of_date=date(2026, 3, 2))

    shipped = Path(__file__).resolve().parents[2] / "deploy" / "hedge_warmstart" / "bybit_warmstart.csv"
    with pytest.raises(ValueError, match="must end before"):
        require_usable_hedge_model_prior(
            load_hedge_model_prior(shipped),
            as_of_date=date(2026, 7, 9),
        )


def test_model_prior_parser_rejects_malformed_rows_instead_of_skipping(tmp_path) -> None:
    p = tmp_path / "malformed.csv"
    p.write_text(
        "date,unit_ret,btc_ret,eth_ret,data_through_date,source_summary_sha256\n"
        f"2026-07-09,not-a-number,0.01,0.02,2026-07-09,{'a' * 64}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid unit_ret"):
        load_hedge_model_prior(p)


def test_active_hedge_rule_derives_from_profile() -> None:
    from liquidity_migration.research.backtest.continuous_profile import active_hedge_rule

    assert ACTIVE_HEDGE_RULE == active_hedge_rule()


def test_2f_falls_back_when_trailing_window_eth_thin_but_full_history_deep() -> None:
    """A deep full history must not hide a sparse trailing ETH window."""
    n = 200
    unit, btc, eth_full = _two_factor_series(n)
    # ETH known for the first 100 rows, None for the last 100 (the beta window).
    eth = list(eth_full[:100]) + [None] * 100

    # Full-series joint count is large (>= beta_min_obs); the windowed count is 0.
    full_joint = sum(1 for b, e in zip(btc, eth) if b is not None and e is not None)
    assert full_joint >= ACTIVE_HEDGE_RULE.beta_min_obs

    cfg = ContinuousHedgeConfig()
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.fell_back_to_btc, "2f hedge silently went to zero (no fallback)"
    assert d.ratio_btc > 0.0
    assert d.ratio_eth == 0.0
    # n_obs_joint must report the WINDOWED count the beta actually used (0 here),
    # not the misleading full-series count.
    assert d.n_obs_joint == 0


def test_2f_full_window_matches_shared_rule() -> None:
    """An ETH-complete trailing window must not spuriously fall back."""
    unit, btc, eth = _two_factor_series(120)
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=10.0)
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    intensity = latest_btcvol_intensity(btc)
    r1, r2 = compute_continuous_hedge_ratios_2f(
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), ACTIVE_HEDGE_RULE, intensity,
    )
    assert not d.fell_back_to_btc
    assert d.ratio_btc == pytest.approx(r1, abs=1e-15)
    assert d.ratio_eth == pytest.approx(r2, abs=1e-15)
    assert d.n_obs_joint >= ACTIVE_HEDGE_RULE.beta_min_obs
