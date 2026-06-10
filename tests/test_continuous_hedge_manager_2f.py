"""Live 2f hedge-manager tests (BTC+ETH legs; 2026-06-10 wiring).

Covers: 2f decision parity with the research live twin, joint cap, ETH-history
fallback to single-leg BTC, per-leg plan generation, warm-start eth_ret loading,
and per-symbol trade-row/link construction.
"""

from __future__ import annotations

import math

from liquidity_migration.continuous_hedge_manager import (
    FROZEN_HEDGE_RULE,
    HEDGE_SYMBOL,
    HEDGE_SYMBOL_2,
    ContinuousHedgeConfig,
    build_hedge_trade_row,
    compute_hedge_decision_2f,
    load_warmstart_2f,
)
from liquidity_migration.continuous_rebalance import (
    ContinuousHedge2FState,
    compute_continuous_hedge_ratios_2f,
)


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
    r1, r2 = compute_continuous_hedge_ratios_2f(
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), FROZEN_HEDGE_RULE, 1.0
    )
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


def test_hedge_trade_row_symbol_parameter() -> None:
    cfg = ContinuousHedgeConfig()
    row = build_hedge_trade_row(
        cfg, qty=0.5, entry_price=3_000.0, now_ms=1_700_000_000_000,
        order_link_id="lm-en-ca-test", symbol=HEDGE_SYMBOL_2,
    )
    assert row["symbol"] == HEDGE_SYMBOL_2
    assert row["side"] == "long"
    assert row["sleeve"] == "continuous_addon"
    # force-exit triggers all disabled (externally managed) — the ws_risk safety contract
    assert row["stop_price"] == 0.0 and row["take_profit_price"] == 0.0 and row["planned_exit_ts_ms"] == 0
