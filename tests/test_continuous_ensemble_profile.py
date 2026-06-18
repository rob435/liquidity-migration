"""v2 continuous ensemble live-wiring tests.

Covers: profile resolution, weighted sizing + component-tagged ids + venue-side
TP in the entry executor, and component-less candidate behavior.
"""

from __future__ import annotations

from liquidity_migration.continuous_demo import (
    CONTINUOUS_DEMO_PROFILES,
    ContinuousDemoCycleConfig,
    _execute_continuous_entries,
    apply_continuous_demo_profile,
)


def test_ensemble_profile_resolves_continuous_ensemble_v2() -> None:
    # The deployed gate (uptrend) arrives via the --btc-trend-gate / BTC_TREND_GATE
    # knob, not the profile; pass it in to mirror the live CLI/env wiring.
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend")
    )
    comps = {c[0]: c for c in cfg.ensemble_components}
    # Current three-component object frozen 2026-06-18;
    # remaining three weights renormalized = old/0.90.
    assert set(comps) == {"p3", "p4p3", "p4p5"}
    assert comps["p3"] == ("p3", "turn3_pop3", 240, 0.10, 0.3333333333333333)
    assert comps["p4p3"] == ("p4p3", "turn4_pop3", 240, 0.10, 0.2222222222222222)
    assert comps["p4p5"] == ("p4p5", "turn4_pop5", 240, 0.10, 0.4444444444444444)
    assert abs(sum(c[4] for c in cfg.ensemble_components) - 1.0) < 1e-12  # renormalized weights sum to 1
    assert cfg.rmom_quantile == 0.25
    assert cfg.btc_trend_gate == "uptrend"
    assert cfg.max_hold_hours == 24
    assert cfg.daily_rebalance_enabled
    assert cfg.daily_rebalance_realized_vol_window_days == 90
    assert cfg.daily_rebalance_max_scale == 4.0
    assert cfg.daily_rebalance_target_daily_vol == 0.045
    assert cfg.daily_rebalance_strategy_momentum_window_days == 0  # the merged test's winning arm


def test_ensemble_v2_disables_damaging_daemon_exits() -> None:
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend")
    )
    comps = {c[0]: c for c in cfg.ensemble_components}
    assert set(comps) == {"p3", "p4p3", "p4p5"}
    assert cfg.max_hold_hours == 24
    assert cfg.left_decile_exit_enabled is False
    assert cfg.reentry_cooldown_minutes == 0
    assert cfg.stop_approach_frac == 0.0
    assert cfg.failed_fade_hours == 0
    assert cfg.failed_fade_loss_pct == 0.0
    assert cfg.failed_fade_min_mfe_pct == 0.0
    assert cfg.breakeven_arm_pct == 0.0
    assert cfg.stop_loss_pct == 0.0
    assert cfg.sizing_mode == "inverse_vol"
    assert cfg.target_vol_per_name == 0.01
    assert cfg.vol_weight_clamp == 2.0
    assert cfg.daily_rebalance_enabled
    assert cfg.daily_rebalance_realized_vol_window_days == 90
    assert cfg.daily_rebalance_target_daily_vol == 0.045
    assert cfg.daily_rebalance_max_scale == 4.0
    assert cfg.daily_rebalance_strategy_momentum_window_days == 0
    assert cfg.daily_rebalance_strategy_momentum_min_return == 0.0
    assert cfg.daily_rebalance_strategy_momentum_scale_when_below == 0.0


def test_profile_does_not_override_btc_trend_gate() -> None:
    # Single source of truth: the profile must PASS THROUGH the gate from the
    # CLI/env knob, never pin it. Pinning it (the pre-2026-06-16 bug) silently
    # made BTC_TREND_GATE=off a no-op for the deployed ensemble.
    for gate in ("uptrend", "off", "downtrend"):
        cfg = apply_continuous_demo_profile(
            ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate=gate)
        )
        assert cfg.btc_trend_gate == gate


def test_only_frozen_v2_profile_is_selectable() -> None:
    assert CONTINUOUS_DEMO_PROFILES == ("continuous_ensemble_v2",)


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


def test_inverse_vol_entry_sizing_multiplies_component_weight() -> None:
    demo = ContinuousDemoCycleConfig(
        submit_orders=False,
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
    )
    rows, orders = _execute_continuous_entries(
        [_cand(component="p4p5", weight=0.40, tp=0.10) | {"rv_168h": 0.02}],
        trading_client=None,
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_frac=0.02,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.01, "qty_step": 0.1}},
        now_ms=1_700_000_100_000,
        strategy_id="s",
        record_preflight=None,
        execution_event_router=None,
    )
    assert len(rows) == 1
    assert len(orders) == 1
    # 10_000 * 0.02 * 0.40 * (0.01 / 0.02) = 40 USDT at price 100 -> 0.4 qty
    assert abs(float(rows[0]["qty"]) - 0.4) < 1e-9
    assert rows[0]["sizing_mode"] == "inverse_vol"
    assert abs(float(rows[0]["entry_vol"]) - 0.02) < 1e-12
    assert abs(float(rows[0]["vol_weight_multiplier"]) - 0.5) < 1e-12
    assert abs(float(orders[0]["vol_weight_multiplier"]) - 0.5) < 1e-12


def test_two_components_same_symbol_distinct_ids() -> None:
    # Generic executor test: two distinct component tags + different TPs on one symbol get
    # distinct ids and the right per-candidate TP price. Synthetic tags (decoupled from the
    # deployed component set, which no longer includes a 0.14-TP leg.
    rows, _ = _run([
        _cand(component="cmpA", weight=0.30, tp=0.10),
        _cand(component="cmpB", weight=0.10, tp=0.14),
    ])
    assert len(rows) == 2
    ids = {r["trade_id"] for r in rows}
    links = {r["entry_order_link_id"] for r in rows}
    assert len(ids) == 2 and len(links) == 2
    tp_by_comp = {r["component"]: r["take_profit_price"] for r in rows}
    assert abs(tp_by_comp["cmpA"] - 90.0) < 1e-9
    assert abs(tp_by_comp["cmpB"] - 86.0) < 1e-9


def test_legacy_candidate_unchanged() -> None:
    rows, _ = _run([_cand()])
    r = rows[0]
    assert r["component"] == "" and r["component_weight"] == 1.0
    assert r["take_profit_price"] == 0.0
    assert not r["trade_id"].endswith("-p3")
    assert abs(float(r["qty"]) - 2.0) < 1e-9  # 10_000*0.02 = 200 USDT / 100
