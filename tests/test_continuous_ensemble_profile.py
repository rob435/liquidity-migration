"""winner_base ensemble live-wiring tests (2026-06-10).

Covers: profile resolution (frozen receipt weights, research rule, NO momentum
hurdle), weighted sizing + component-tagged ids + venue-side TP in the entry
executor, and legacy-path invariance (no component keys => old behavior).
"""

from __future__ import annotations

from liquidity_migration.continuous_demo import (
    ContinuousDemoCycleConfig,
    _execute_continuous_entries,
    apply_continuous_demo_profile,
)


def test_ensemble_profile_resolves_winner_base() -> None:
    cfg = apply_continuous_demo_profile(ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v1"))
    comps = {c[0]: c for c in cfg.ensemble_components}
    assert set(comps) == {"p3", "p4p3", "p4p5", "tp14"}
    assert comps["p3"] == ("p3", "turn3_pop3", 240, 0.10, 0.30)
    assert comps["p4p3"] == ("p4p3", "turn4_pop3", 240, 0.10, 0.20)
    assert comps["p4p5"] == ("p4p5", "turn4_pop5", 240, 0.10, 0.40)
    assert comps["tp14"] == ("tp14", "none", 210, 0.14, 0.10)
    assert abs(sum(c[4] for c in cfg.ensemble_components) - 1.0) < 1e-12  # frozen weights sum to 1
    assert cfg.rmom_quantile == 0.25
    assert cfg.btc_trend_gate == "uptrend"
    assert cfg.max_hold_hours == 24
    assert cfg.daily_rebalance_enabled
    assert cfg.daily_rebalance_realized_vol_window_days == 90
    assert cfg.daily_rebalance_max_scale == 4.0
    assert cfg.daily_rebalance_target_daily_vol == 0.045
    assert cfg.daily_rebalance_strategy_momentum_window_days == 0  # the merged test's winning arm


def test_old_profile_still_resolves_unchanged() -> None:
    cfg = apply_continuous_demo_profile(ContinuousDemoCycleConfig(strategy_profile="continuous_rebalance_v1"))
    assert cfg.entry_event_trigger == "turn4_pop4"
    assert cfg.ensemble_components == ()


def _cand(symbol="AAAUSDT", component=None, weight=None, tp=None):
    c = {"symbol": symbol, "live_price": 100.0, "signal_ts_ms": 1_700_000_000_000,
         "stop_loss_pct": 0.25, "decile": 9, "composite": 1.0}
    if component is not None:
        c.update({"component": component, "component_weight": weight, "take_profit_pct": tp})
    return c


def _run(cands):
    demo = ContinuousDemoCycleConfig(submit_orders=False)
    return _execute_continuous_entries(
        cands, trading_client=None, demo=demo, equity_usdt=10_000.0,
        order_notional_frac=0.02, price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.01, "qty_step": 0.1}},
        now_ms=1_700_000_100_000, strategy_id="s",
        record_preflight=None, execution_event_router=None)


def test_component_entry_weighted_sizing_tp_and_ids() -> None:
    rows, orders = _run([_cand(component="p4p5", weight=0.40, tp=0.10)])
    assert len(rows) == 1
    r = rows[0]
    # 10_000 * 0.02 * 0.40 = 80 USDT at price 100 -> 0.8 qty
    assert abs(float(r["qty"]) - 0.8) < 1e-9
    assert r["component"] == "p4p5" and abs(r["component_weight"] - 0.40) < 1e-12
    assert abs(r["take_profit_price"] - 90.0) < 1e-9
    assert r["trade_id"].endswith("-p4p5")
    assert "p4p5" in r["entry_order_link_id"]


def test_two_components_same_symbol_distinct_ids() -> None:
    rows, _ = _run([
        _cand(component="p3", weight=0.30, tp=0.10),
        _cand(component="tp14", weight=0.10, tp=0.14),
    ])
    assert len(rows) == 2
    ids = {r["trade_id"] for r in rows}
    links = {r["entry_order_link_id"] for r in rows}
    assert len(ids) == 2 and len(links) == 2
    tp_by_comp = {r["component"]: r["take_profit_price"] for r in rows}
    assert abs(tp_by_comp["p3"] - 90.0) < 1e-9
    assert abs(tp_by_comp["tp14"] - 86.0) < 1e-9


def test_legacy_candidate_unchanged() -> None:
    rows, _ = _run([_cand()])
    r = rows[0]
    assert r["component"] == "" and r["component_weight"] == 1.0
    assert r["take_profit_price"] == 0.0
    assert not r["trade_id"].endswith("-p3")
    assert abs(float(r["qty"]) - 2.0) < 1e-9  # 10_000*0.02 = 200 USDT / 100
