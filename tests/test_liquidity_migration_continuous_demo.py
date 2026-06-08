"""Tests for the continuous-fade demo sleeve (liquidity_migration.continuous_demo) — experimental, OFF / de-promoted.

The headline test is EQUIVALENCE: the live state (confirmed history + live price as the current bar)
must reproduce the verified backtest decile exactly — the live signal == the backtest signal. Plus
entry/exit selection and the distinct continuous orderLinkId prefix (ws_risk fill routing).
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.continuous_demo import (
    ContinuousDemoCycleConfig,
    LivePanelCache,
    _build_continuous_rebalance_resize_rows,
    _continuous_rebalance_cycle_fields,
    _continuous_rebalance_mark_prices_json,
    _continuous_rebalance_resize_checked_today,
    _continuous_rebalance_scale_state_from_cycles,
    _continuous_entry_candidates_with_signal_metadata,
    _execute_continuous_rebalance_resizes,
    _validate_continuous_demo_config,
    active_primary_pnl_gate_allows_addon,
    apply_continuous_demo_profile,
    build_confirmed_entry_state,
    build_live_continuous_state,
    continuous_dataset_names,
    continuous_rebalance_rule,
    filter_addon_candidates_by_active_primary_pnl_gate,
    format_continuous_demo_cycle_summary,
    _continuous_order_link_id,
    _protective_exit_reason,
    _recent_exit_cooldown_symbols,
    _recent_entry_cooldown_symbols,
    continuous_sleeve_name,
    continuous_strategy_id,
    entry_circuit_breaker_tripped,
    plan_continuous_exits,
    plan_protective_exits,
    run_continuous_protective_exit_cycle,
    select_continuous_entries,
)
from liquidity_migration.continuous_events import compute_continuous_decile_panel
from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan
from liquidity_migration.event_demo import decode_entry_order_link_id


def _synth(n_symbols: int = 26, n_bars: int = 320, start: int = 1_700_000_000_000):
    start -= start % MS_PER_DAY
    rows = []
    for s in range(n_symbols):
        p = 100.0 + s
        for i in range(n_bars):
            wob = 1.0 + 0.02 * ((s * 7 + i * 13) % 11 - 5) / 5.0
            p = max(1.0, p * wob)
            rows.append({"ts_ms": start + i * MS_PER_HOUR, "symbol": f"S{s:02d}", "close": p,
                         "turnover_quote": 1_000_000.0})
    klines = pl.DataFrame(rows)
    days = sorted({(start + i * MS_PER_HOUR) // MS_PER_DAY * MS_PER_DAY for i in range(n_bars)})
    rmom = pl.DataFrame(
        [{"symbol": f"S{s:02d}", "day_ts": d, "residual_momentum": (s % 13) * 0.001 - 0.006}
         for d in days for s in range(n_symbols)]
    )
    return klines, rmom, start, n_bars


def test_live_state_reproduces_backtest_decile() -> None:
    """The live decile (history < T + live price at T) == the backtest decile at T, exactly."""
    klines, rmom, start, n_bars = _synth()
    cfg = ContinuousDemoCycleConfig()
    T = start + (n_bars - 1) * MS_PER_HOUR  # the current hour slot
    # backtest: full pipeline over all bars, take decile at ts == T
    bt = compute_continuous_decile_panel(klines, rmom, rmom_quantile=cfg.rmom_quantile, start_ms=0)
    bt_T = {r["symbol"]: r["decile"] for r in bt.filter(pl.col("ts_ms") == T).to_dicts()}
    # live: confirmed bars strictly before T + the live price (close at T) as the in-progress bar
    hist = klines.filter(pl.col("ts_ms") < T)
    cur_price = {r["symbol"]: r["close"] for r in klines.filter(pl.col("ts_ms") == T).to_dicts()}
    live = build_live_continuous_state(hist, cur_price, rmom, now_ts_ms=T + 1_800_000, config=cfg)
    live_d = {r["symbol"]: r["decile"] for r in live.to_dicts()}
    assert bt_T, "backtest produced no deciles at T (warmup too short)"
    assert live_d == bt_T  # live signal is identical to the verified backtest signal


def test_confirmed_entry_state_is_the_deciding_bar_decile_no_live_price() -> None:
    """Entry-timing fix: the entry decile comes from the CONFIRMED bar that closed +1h before entry
    (deciding bar = cur_hour - 2h for delay=1), computed with NO live price — i.e. the backtest decile
    at that bar. Distinct from the intra-hour live decile (which the engine showed ~halves MAR)."""
    klines, rmom, start, n_bars = _synth()
    cfg = ContinuousDemoCycleConfig(entry_confirm_delay_hours=1)
    now = start + (n_bars - 1) * MS_PER_HOUR + 1_800_000  # mid current hour
    cur_ts = start + (n_bars - 1) * MS_PER_HOUR
    deciding_ts = cur_ts - 2 * MS_PER_HOUR                 # 1+delay = 2h back
    es = build_confirmed_entry_state(klines, rmom, now_ts_ms=now, config=cfg)
    # reference: backtest decile over confirmed bars (< cur_ts), at the deciding bar
    bt = compute_continuous_decile_panel(klines.filter(pl.col("ts_ms") < cur_ts), rmom,
                                         rmom_quantile=cfg.rmom_quantile, start_ms=0)
    ref = {r["symbol"]: r["decile"] for r in bt.filter(pl.col("ts_ms") == deciding_ts).to_dicts()}
    got = {r["symbol"]: r["decile"] for r in es.to_dicts()}
    assert ref and got == ref                              # confirmed deciding-bar decile, no live bar
    assert {"ret1", "max_ret168", "prior6_ret1_max"} <= set(es.columns)


def test_select_entries_respects_decile_liquidity_held_and_capacity() -> None:
    state = pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E"],
        "decile": [9, 9, 9, 8, 9],
        "composite": [0.95, 0.90, 0.99, 0.5, 0.85],
        "turnover_quote": [1e6, 1e3, 1e6, 1e6, 1e6],  # B illiquid
    })
    cfg = ContinuousDemoCycleConfig(decile=9, liq_turnover_min=500_000.0, max_active=25,
                                    max_new_entries_per_cycle=5)
    out = select_continuous_entries(state, held_symbols={"C"}, cooldown_symbols=set(),
                                    open_count=1, config=cfg)
    syms = [r["symbol"] for r in out]
    assert syms == ["A", "E"]  # D9+liquid+not-held; C held, B illiquid, D wrong decile; ranked by composite
    # capacity: max_active reached -> no entries
    assert select_continuous_entries(state, held_symbols=set(), cooldown_symbols=set(),
                                     open_count=25, config=cfg) == []


def test_select_entries_applies_confirmed_event_trigger() -> None:
    state = pl.DataFrame({
        "symbol": ["POPUSDT", "OLDPOPUSDT", "SMALLUSDT"],
        "decile": [9, 9, 9],
        "composite": [0.7, 0.9, 0.8],
        "turnover_quote": [1e6, 1e6, 1e6],
        "ret1": [0.26, 0.26, 0.20],
        "max_ret168": [0.26, 0.30, 0.20],
    })
    cfg = ContinuousDemoCycleConfig(
        decile=9,
        entry_event_trigger="fresh_pop25",
        liq_turnover_min=500_000.0,
        max_new_entries_per_cycle=5,
    )

    out = select_continuous_entries(
        state, held_symbols=set(), cooldown_symbols=set(), open_count=0, config=cfg
    )

    assert [r["symbol"] for r in out] == ["POPUSDT"]


def test_plan_exits_on_left_decile_and_max_hold() -> None:
    state = pl.DataFrame({"symbol": ["A", "C"], "decile": [9, 9], "composite": [0.9, 0.9],
                          "turnover_quote": [1e6, 1e6]})
    cfg = ContinuousDemoCycleConfig(decile=9, max_hold_hours=48)
    now = 2_000_000_000_000
    open_trades = [
        {"symbol": "A", "entry_ts_ms": now - 2 * MS_PER_HOUR},   # still in D9, fresh -> hold
        {"symbol": "B", "entry_ts_ms": now - 2 * MS_PER_HOUR},   # NOT in D9 -> left_decile exit
        {"symbol": "C", "entry_ts_ms": now - 60 * MS_PER_HOUR},  # in D9 but past max_hold -> exit
    ]
    exits = plan_continuous_exits(open_trades, state, now_ms=now, config=cfg)
    by = {e["symbol"]: e["exit_reason"] for e in exits}
    assert by == {"B": "left_decile", "C": "max_hold"}


def _hold_klines(symbol: str, lows: list[float], start: int = 0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol] * len(lows), "ts_ms": [start + i * MS_PER_HOUR for i in range(len(lows))],
        "open": lows, "high": [x * 1.01 for x in lows], "low": lows, "close": lows,
    })


def test_exit_breakeven_after_mfe_then_giveback() -> None:
    """Inherited breakeven exit: a short that reached >=10% favorable then returned to entry covers."""
    base = 1_700_000_000_000
    cfg = ContinuousDemoCycleConfig(breakeven_arm_pct=0.10, decile=9)
    state = pl.DataFrame({"symbol": ["A"], "decile": [9], "composite": [0.9], "turnover_quote": [1e6]})  # still in D9
    trade = {"trade_id": "t", "symbol": "A", "entry_ts_ms": base, "entry_price": 100.0, "qty": "1"}
    klines = _hold_klines("A", [100.0, 92.0, 88.0, 95.0], start=base)  # dipped to 88 -> MFE = 12% >= 10%
    exits = plan_continuous_exits([trade], state, now_ms=base + 4 * MS_PER_HOUR, config=cfg,
                                  klines=klines, price_by_symbol={"A": 100.0})  # back at entry
    assert [e["exit_reason"] for e in exits] == ["breakeven"]


def test_exit_failed_fade_when_down_and_never_worked() -> None:
    """Inherited ff6 exit: held >= ff hours, never reached ff_min_mfe, now down > ff_loss_pct."""
    base = 1_700_000_000_000
    cfg = ContinuousDemoCycleConfig(failed_fade_hours=6, failed_fade_loss_pct=0.04, failed_fade_min_mfe_pct=0.01, decile=9)
    state = pl.DataFrame({"symbol": ["A"], "decile": [9], "composite": [0.9], "turnover_quote": [1e6]})
    trade = {"trade_id": "t", "symbol": "A", "entry_ts_ms": base, "entry_price": 100.0, "qty": "1"}
    klines = _hold_klines("A", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0], start=base)  # never below 100 -> MFE ~0
    exits = plan_continuous_exits([trade], state, now_ms=base + 7 * MS_PER_HOUR, config=cfg,
                                  klines=klines, price_by_symbol={"A": 105.0})  # down 5% on the short
    assert [e["exit_reason"] for e in exits] == ["failed_fade"]


def test_active_primary_pnl_gate_allows_addon_when_no_same_symbol_primary() -> None:
    rows = [{"symbol": "OTHER", "status": "open", "side": "short", "entry_price": 100.0}]
    allowed, worst = active_primary_pnl_gate_allows_addon(
        rows, symbol="ADDON", current_price=120.0, min_unrealized_return=0.0
    )
    assert allowed is True
    assert worst is None


def test_active_primary_pnl_gate_blocks_underwater_same_symbol_short() -> None:
    rows = [{"symbol": "RAVEUSDT", "status": "open", "side": "short", "entry_price": 100.0}]
    allowed, worst = active_primary_pnl_gate_allows_addon(
        rows, symbol="RAVEUSDT", current_price=110.0, min_unrealized_return=0.0
    )
    assert allowed is False
    assert worst == pytest.approx(100.0 / 110.0 - 1.0)


def test_active_primary_pnl_gate_allows_working_same_symbol_short() -> None:
    rows = [{"symbol": "RAVEUSDT", "status": "open", "side": "short", "entry_price": 100.0}]
    allowed, worst = active_primary_pnl_gate_allows_addon(
        rows, symbol="RAVEUSDT", current_price=90.0, min_unrealized_return=0.0
    )
    assert allowed is True
    assert worst == pytest.approx(100.0 / 90.0 - 1.0)


def test_active_primary_pnl_gate_uses_worst_active_same_symbol_trade() -> None:
    rows = [
        {"symbol": "RAVEUSDT", "status": "open", "side": "short", "entry_price": 100.0},
        {"symbol": "RAVEUSDT", "status": "open", "side": "short", "entry_price": 120.0},
        {"symbol": "RAVEUSDT", "status": "closed", "side": "short", "entry_price": 50.0},
    ]
    allowed, worst = active_primary_pnl_gate_allows_addon(
        rows, symbol="RAVEUSDT", current_price=110.0, min_unrealized_return=0.0
    )
    assert allowed is False
    assert worst == pytest.approx(100.0 / 110.0 - 1.0)


def test_active_primary_pnl_gate_fails_closed_when_same_symbol_primary_mark_missing() -> None:
    rows = [{"symbol": "RAVEUSDT", "status": "open", "side": "short", "entry_price": 100.0}]
    allowed, worst = active_primary_pnl_gate_allows_addon(
        rows, symbol="RAVEUSDT", current_price=0.0, min_unrealized_return=0.0
    )
    assert allowed is False
    assert worst is None


def test_active_primary_pnl_gate_handles_long_side_for_shadow_tests() -> None:
    rows = [{"symbol": "BTCUSDT", "status": "open", "side": "long", "entry_price": 100.0}]
    allowed, worst = active_primary_pnl_gate_allows_addon(
        rows, symbol="BTCUSDT", current_price=101.0, min_unrealized_return=0.0
    )
    assert allowed is True
    assert worst == pytest.approx(0.01)


def test_filter_addon_candidates_applies_active_primary_pnl_gate() -> None:
    primary = [
        {"symbol": "GOODUSDT", "status": "open", "side": "short", "entry_price": 100.0},
        {"symbol": "BADUSDT", "status": "open", "side": "short", "entry_price": 100.0},
    ]
    candidates = [
        {"symbol": "GOODUSDT", "live_price": 90.0},
        {"symbol": "BADUSDT", "live_price": 110.0},
        {"symbol": "NOPRIMARYUSDT", "live_price": 0.0},
    ]

    kept, stats = filter_addon_candidates_by_active_primary_pnl_gate(
        candidates, primary, price_by_symbol={}, min_unrealized_return=0.0
    )

    assert [r["symbol"] for r in kept] == ["GOODUSDT", "NOPRIMARYUSDT"]
    assert stats["addon_primary_pnl_gate_skips"] == 1
    assert stats["addon_primary_pnl_gate_skip_symbols"] == ["BADUSDT"]
    assert stats["addon_primary_pnl_gate_skipped"][0]["worst_primary_unrealized_return"] == pytest.approx(
        100.0 / 110.0 - 1.0
    )


def test_filter_addon_candidates_fails_closed_when_primary_mark_missing() -> None:
    primary = [{"symbol": "BADUSDT", "status": "open", "side": "short", "entry_price": 100.0}]
    candidates = [{"symbol": "BADUSDT"}]

    kept, stats = filter_addon_candidates_by_active_primary_pnl_gate(
        candidates, primary, price_by_symbol={}, min_unrealized_return=0.0
    )

    assert kept == []
    assert stats["addon_primary_pnl_gate_skips"] == 1
    assert stats["addon_primary_pnl_gate_skipped"][0]["current_price"] is None


def test_age_gate_excludes_ineligible_symbols() -> None:
    state = pl.DataFrame({"symbol": ["A", "B"], "decile": [9, 9], "composite": [0.9, 0.8],
                          "turnover_quote": [1e6, 1e6]})
    cfg = ContinuousDemoCycleConfig(decile=9)
    out = select_continuous_entries(state, held_symbols=set(), cooldown_symbols=set(), open_count=0,
                                    config=cfg, eligible_symbols={"A"})  # B too young
    assert [r["symbol"] for r in out] == ["A"]


def test_continuous_order_link_prefix_routes_as_distinct_sleeve() -> None:
    sig = 1_700_000_000_000
    link = _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig)
    assert link.startswith("lm-en-c-")
    # ws_risk must decode it as the CONTINUOUS sleeve (not short/long), recovering signal_ts
    decoded = decode_entry_order_link_id(link)
    assert decoded is not None
    sleeve, signal_ts_ms, reentry_seq = decoded
    assert sleeve == "continuous"
    assert signal_ts_ms == (sig // 1000) * 1000
    assert reentry_seq == 0  # a first-entry (5-part) link carries no re-entry seq
    # the short (4-part) and long (en-l) links still decode as their own sleeves
    assert decode_entry_order_link_id("lm-en-BTC-abcd")[0] == "short"
    assert decode_entry_order_link_id("lm-en-l-BTC-abcd")[0] == "long"

    # A same-window RE-ENTRY (seq>0) gets a DISTINCT link + trade_id that still round-trips, and is
    # idempotent: the same (symbol, signal_ts, seq) reproduces the same link (continuous-2).
    from liquidity_migration.continuous_demo import _continuous_trade_id

    link0 = _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=0)
    link1 = _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=1)
    assert link0 == link  # seq=0 is byte-identical to the legacy form
    assert link1 != link0 and link1 == f"{link0}-1"
    assert decode_entry_order_link_id(link1) == ("continuous", (sig // 1000) * 1000, 1)
    addon_link = _continuous_order_link_id("en-ca", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=1)
    assert addon_link.startswith("lm-en-ca-")
    assert decode_entry_order_link_id(addon_link) == ("continuous_addon", (sig // 1000) * 1000, 1)
    tid0 = _continuous_trade_id("STRAT", "WIFUSDT", sig, 0)
    tid1 = _continuous_trade_id("STRAT", "WIFUSDT", sig, 1)
    assert tid0 == f"STRAT-WIFUSDT-{sig}"  # seq=0 byte-identical to legacy
    assert tid1 == f"{tid0}-1" and tid1 != tid0
    # idempotent: identical inputs reproduce identical id + link
    assert _continuous_trade_id("STRAT", "WIFUSDT", sig, 1) == tid1
    assert _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=1) == link1


def test_execute_entries_dry_run_builds_short_rows() -> None:
    """Dry-run short entry: side='short', lm-en-c- link, stop ABOVE entry, ~2% notional, no network."""
    from liquidity_migration.continuous_demo import _execute_continuous_entries

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True, stop_loss_pct=0.25,
                                    per_position_notional_pct_equity=2.0)
    cand = [{"symbol": "WIFUSDT", "decile": 9, "composite": 0.95, "turnover_quote": 2e6,
             "signal_ts_ms": 1_700_000_000_000, "stop_loss_pct": 0.25, "live_price": 100.0}]
    contracts = {"WIFUSDT": {"tick_size": 0.0001, "qty_step": 0.001, "min_order_qty": 0.001}}
    rows, orders = _execute_continuous_entries(
        cand, trading_client=None, demo=cfg, equity_usdt=10_000.0, order_notional_frac=0.02,
        price_by_symbol={"WIFUSDT": 100.0}, contract_by_symbol=contracts, now_ms=1_700_000_000_000,
        strategy_id="continuous_fade_v1", record_preflight=None, execution_event_router=None)
    assert len(rows) == 1 and len(orders) == 1
    r, o = rows[0], orders[0]
    assert r["side"] == "short" and o["side"] == "Sell"
    assert r["entry_order_link_id"].startswith("lm-en-c-")
    assert r["stop_price"] > r["entry_price"]            # short stop is ABOVE entry
    assert abs(r["notional_usdt"] - 200.0) < 1e-6        # 2% of 10k
    assert r["status"] == "open" and o["reduce_only"] is False
    assert r["sleeve"] == "continuous"                   # self-identifying for ws_risk routing


def test_execute_entries_dry_run_builds_addon_identity_rows() -> None:
    from liquidity_migration.continuous_demo import _execute_continuous_entries

    cfg = ContinuousDemoCycleConfig(
        submit_orders=False,
        record_dry_run=True,
        strategy_profile="continuous_addon_v1",
        stop_loss_pct=0.25,
        per_position_notional_pct_equity=2.0,
    )
    cand = [{"symbol": "WIFUSDT", "decile": 9, "composite": 0.95, "turnover_quote": 2e6,
             "signal_ts_ms": 1_700_000_000_000, "stop_loss_pct": 0.25, "live_price": 100.0}]
    contracts = {"WIFUSDT": {"tick_size": 0.0001, "qty_step": 0.001, "min_order_qty": 0.001}}

    rows, orders = _execute_continuous_entries(
        cand, trading_client=None, demo=cfg, equity_usdt=10_000.0, order_notional_frac=0.02,
        price_by_symbol={"WIFUSDT": 100.0}, contract_by_symbol=contracts, now_ms=1_700_000_000_000,
        strategy_id=continuous_strategy_id(cfg), record_preflight=None, execution_event_router=None)

    assert continuous_sleeve_name(cfg) == "continuous_addon"
    assert rows[0]["strategy_id"] == "continuous_fade_addon_v1"
    assert rows[0]["sleeve"] == "continuous_addon"
    assert orders[0]["sleeve"] == "continuous_addon"
    assert rows[0]["entry_order_link_id"].startswith("lm-en-ca-")
    assert decode_entry_order_link_id(rows[0]["entry_order_link_id"])[0] == "continuous_addon"


def test_execute_exits_dry_run_closes_short() -> None:
    from liquidity_migration.continuous_demo import _execute_continuous_exits

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {"trade_id": "continuous_fade_v1-WIFUSDT-1700000000", "symbol": "WIFUSDT", "side": "short",
             "sleeve": "continuous",
             "status": "open", "entry_price": 100.0, "qty": "2", "equity_usdt": 10_000.0, "notional_usdt": 200.0}
    all_trades = pl.DataFrame([trade])
    plan = [{**trade, "exit_reason": "left_decile"}]
    rows, orders = _execute_continuous_exits(plan, all_trades, trading_client=None, demo=cfg,
                                             now_ms=1_700_100_000_000, record_preflight=None)
    assert len(rows) == 1 and orders[0]["side"] == "Buy" and orders[0]["reduce_only"] is True
    assert rows[0]["status"] == "closed" and rows[0]["exit_reason"] == "left_decile"
    assert rows[0]["sleeve"] == "continuous"             # exit row inherits the entry's sleeve tag (dict(trade))
    assert orders[0]["sleeve"] == "continuous" and orders[0]["order_link_id"].startswith("lm-ux-c-")


def test_rebalance_resize_rows_reduce_open_short() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {
        "trade_id": "t1",
        "strategy_id": continuous_strategy_id(cfg),
        "symbol": "ABCUSDT",
        "side": "short",
        "sleeve": "continuous",
        "status": "open",
        "entry_price": 100.0,
        "qty": "3",
        "notional_usdt": 300.0,
        "equity_usdt": 10_000.0,
        "qty_step": 0.1,
    }
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="ABCUSDT",
        side="Buy",
        reduce_only=True,
        qty=1.0,
        current_notional_usdt=300.0,
        target_notional_usdt=200.0,
        delta_notional_usdt=-100.0,
        reason="rebalance_reduce",
    )

    rows, orders = _build_continuous_rebalance_resize_rows(
        [plan],
        pl.DataFrame([trade]),
        demo=cfg,
        price_by_symbol={"ABCUSDT": 100.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
    )

    assert len(rows) == 1 and len(orders) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "2"
    assert rows[0]["notional_usdt"] == pytest.approx(200.0)
    assert rows[0]["rebalance_realized_return"] == pytest.approx(0.0)
    assert orders[0]["side"] == "Buy"
    assert orders[0]["reduce_only"] is True
    assert orders[0]["resize_reason"] == "rebalance_reduce"
    assert orders[0]["order_link_id"].startswith("lm-ux-c-")


def test_rebalance_resize_rows_increase_short_reweights_entry() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {
        "trade_id": "t1",
        "strategy_id": continuous_strategy_id(cfg),
        "symbol": "ABCUSDT",
        "side": "short",
        "sleeve": "continuous",
        "status": "open",
        "entry_price": 100.0,
        "qty": "1",
        "notional_usdt": 100.0,
        "equity_usdt": 10_000.0,
        "qty_step": 0.1,
    }
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="ABCUSDT",
        side="Sell",
        reduce_only=False,
        qty=2.0,
        current_notional_usdt=120.0,
        target_notional_usdt=360.0,
        delta_notional_usdt=240.0,
        reason="rebalance_increase",
    )

    rows, orders = _build_continuous_rebalance_resize_rows(
        [plan],
        pl.DataFrame([trade]),
        demo=cfg,
        price_by_symbol={"ABCUSDT": 120.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "3"
    assert rows[0]["entry_price"] == pytest.approx((100.0 + 2.0 * 120.0) / 3.0)
    assert rows[0]["notional_usdt"] == pytest.approx(340.0)
    assert orders[0]["side"] == "Sell"
    assert orders[0]["reduce_only"] is False
    assert orders[0]["order_link_id"].startswith("lm-en-c-")


def test_rebalance_resize_rows_zero_scale_closes_short() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {
        "trade_id": "t1",
        "strategy_id": continuous_strategy_id(cfg),
        "symbol": "ABCUSDT",
        "side": "short",
        "sleeve": "continuous",
        "status": "open",
        "entry_price": 100.0,
        "qty": "3",
        "notional_usdt": 300.0,
        "equity_usdt": 10_000.0,
        "qty_step": 0.1,
    }
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="ABCUSDT",
        side="Buy",
        reduce_only=True,
        qty=3.0,
        current_notional_usdt=270.0,
        target_notional_usdt=0.0,
        delta_notional_usdt=-300.0,
        reason="rebalance_reduce",
    )

    rows, orders = _build_continuous_rebalance_resize_rows(
        [plan],
        pl.DataFrame([trade]),
        demo=cfg,
        price_by_symbol={"ABCUSDT": 90.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
    )

    assert rows[0]["status"] == "closed"
    assert rows[0]["qty"] == "0"
    assert rows[0]["exit_reason"] == "rebalance_zero"
    assert rows[0]["net_return"] == pytest.approx(0.003)
    assert orders[0]["qty"] == "3"
    assert orders[0]["notional_usdt"] == pytest.approx(270.0)


def test_rebalance_resize_rows_skip_unroundable_dust() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {
        "trade_id": "t1",
        "strategy_id": continuous_strategy_id(cfg),
        "symbol": "ABCUSDT",
        "side": "short",
        "sleeve": "continuous",
        "status": "open",
        "entry_price": 100.0,
        "qty": "3",
        "notional_usdt": 300.0,
        "equity_usdt": 10_000.0,
        "qty_step": 1.0,
    }
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="ABCUSDT",
        side="Buy",
        reduce_only=True,
        qty=0.01,
        current_notional_usdt=300.0,
        target_notional_usdt=299.0,
        delta_notional_usdt=-1.0,
        reason="rebalance_reduce",
    )

    rows, orders = _build_continuous_rebalance_resize_rows(
        [plan],
        pl.DataFrame([trade]),
        demo=cfg,
        price_by_symbol={"ABCUSDT": 100.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 1.0, "min_order_qty": 1.0}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
    )

    assert rows == []
    assert orders == []


def test_execute_rebalance_resizes_submitted_uses_confirmed_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    import liquidity_migration.continuous_demo as cd

    cfg = ContinuousDemoCycleConfig(submit_orders=True, confirm_demo_orders=True, daily_rebalance_enabled=True)
    trade = {
        "trade_id": "t1",
        "strategy_id": continuous_strategy_id(cfg),
        "symbol": "ABCUSDT",
        "side": "short",
        "sleeve": "continuous",
        "status": "open",
        "entry_price": 100.0,
        "qty": "1",
        "notional_usdt": 100.0,
        "equity_usdt": 10_000.0,
        "qty_step": 0.1,
    }
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="ABCUSDT",
        side="Sell",
        reduce_only=False,
        qty=2.0,
        current_notional_usdt=120.0,
        target_notional_usdt=360.0,
        delta_notional_usdt=240.0,
        reason="rebalance_increase",
    )

    class FakeClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def place_order(self, **kwargs: object) -> dict[str, str]:
            self.orders.append(kwargs)
            return {"orderId": "oid-1"}

    def fake_wait(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"qty": "2", "avg_price": "120", "fee": "0.12", "exec_time_ms": 7}

    preflight_rows: list[dict[str, object]] = []
    monkeypatch.setattr(cd, "_wait_for_execution_summary", fake_wait)
    client = FakeClient()

    rows, orders = _execute_continuous_rebalance_resizes(
        [plan],
        pl.DataFrame([trade]),
        trading_client=client,
        demo=cfg,
        price_by_symbol={"ABCUSDT": 120.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
        record_preflight=preflight_rows.append,
    )

    assert len(client.orders) == 1
    assert client.orders[0]["side"] == "Sell"
    assert client.orders[0]["reduceOnly"] is False
    assert len(preflight_rows) == 1
    assert rows[0]["qty"] == "3"
    assert rows[0]["entry_price"] == pytest.approx((100.0 + 2.0 * 120.0) / 3.0)
    assert rows[0]["last_rebalance_fee_usdt"] == pytest.approx(0.12)
    assert rows[0]["submit_mode"] == "submitted"
    assert orders[0]["status"] == "filled"
    assert orders[0]["order_id"] == "oid-1"
    assert orders[0]["fee_usdt"] == pytest.approx(0.12)


def test_execute_rebalance_resizes_unconfirmed_does_not_mutate_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    import liquidity_migration.continuous_demo as cd

    cfg = ContinuousDemoCycleConfig(submit_orders=True, confirm_demo_orders=True, daily_rebalance_enabled=True)
    trade = {
        "trade_id": "t1",
        "strategy_id": continuous_strategy_id(cfg),
        "symbol": "ABCUSDT",
        "side": "short",
        "sleeve": "continuous",
        "status": "open",
        "entry_price": 100.0,
        "qty": "1",
        "notional_usdt": 100.0,
        "equity_usdt": 10_000.0,
        "qty_step": 0.1,
    }
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="ABCUSDT",
        side="Sell",
        reduce_only=False,
        qty=2.0,
        current_notional_usdt=120.0,
        target_notional_usdt=360.0,
        delta_notional_usdt=240.0,
        reason="rebalance_increase",
    )

    class FakeClient:
        def place_order(self, **_kwargs: object) -> dict[str, str]:
            return {"orderId": "oid-1"}

    def fake_wait(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"qty": "0", "avg_price": "0", "fee": "0", "exec_time_ms": 7}

    monkeypatch.setattr(cd, "_wait_for_execution_summary", fake_wait)
    rows, orders = _execute_continuous_rebalance_resizes(
        [plan],
        pl.DataFrame([trade]),
        trading_client=FakeClient(),
        demo=cfg,
        price_by_symbol={"ABCUSDT": 120.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
    )

    assert rows == []
    assert len(orders) == 1
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["filled_qty"] == ""


def test_rebalance_scale_state_uses_latest_prior_cycle_per_day() -> None:
    base = 1_700_000_000_000
    day0 = (base // MS_PER_DAY) * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_scaled_equity": 1.01,
                "rebalance_scaled_peak": 1.01,
            },
            {
                "ts_ms": day0 + 2,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.02,
                "rebalance_scaled_equity": 1.02,
                "rebalance_scaled_peak": 1.02,
            },
            {
                "ts_ms": day0 + MS_PER_DAY + 1,
                "rebalance_day_ts": day0 + MS_PER_DAY,
                "rebalance_raw_return": -0.03,
                "rebalance_scaled_equity": 0.99,
                "rebalance_scaled_peak": 1.02,
            },
        ],
        infer_schema_length=None,
    )

    state = _continuous_rebalance_scale_state_from_cycles(
        cycles,
        current_day_ts=day0 + 2 * MS_PER_DAY,
    )

    assert state.prior_raw_returns == pytest.approx((0.02, -0.03))
    assert state.prior_scaled_equity == pytest.approx(0.99)
    assert state.prior_scaled_peak == pytest.approx(1.02)


def test_rebalance_scale_state_ignores_current_day_and_missing_rows() -> None:
    base = 1_700_000_000_000
    day0 = (base // MS_PER_DAY) * MS_PER_DAY
    current_day = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {"ts_ms": day0 + 1, "rebalance_day_ts": day0, "rebalance_raw_return": 0.01},
            {"ts_ms": current_day + 1, "rebalance_day_ts": current_day, "rebalance_raw_return": 0.50},
            {"ts_ms": day0 + 3, "rebalance_day_ts": day0 - MS_PER_DAY},
            {"ts_ms": day0 + 4, "rebalance_raw_return": 0.25},
        ],
        infer_schema_length=None,
    )

    state = _continuous_rebalance_scale_state_from_cycles(cycles, current_day_ts=current_day)

    assert state.prior_raw_returns == pytest.approx((0.01,))
    assert state.prior_scaled_equity == pytest.approx(1.0)
    assert state.prior_scaled_peak == pytest.approx(1.0)


def test_rebalance_scale_state_empty_defaults_safe() -> None:
    state = _continuous_rebalance_scale_state_from_cycles(pl.DataFrame(), current_day_ts=1_700_000_000_000)

    assert state.prior_raw_returns == ()
    assert state.prior_scaled_equity == pytest.approx(1.0)
    assert state.prior_scaled_peak == pytest.approx(1.0)


def test_continuous_rebalance_rule_matches_default_candidate_knobs() -> None:
    rule = continuous_rebalance_rule(ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True))

    assert rule.realized_vol_window_days == 90
    assert rule.target_daily_vol == pytest.approx(0.025)
    assert rule.max_scale == pytest.approx(4.0)
    assert rule.drawdown_half_threshold == pytest.approx(-0.04)
    assert rule.resize_cost_bps == pytest.approx(10.0)
    assert rule.strategy_momentum_window_days == 180
    assert rule.strategy_momentum_min_return == pytest.approx(0.02)
    assert rule.strategy_momentum_scale_when_below == pytest.approx(0.0)


def test_continuous_rebalance_profile_resolves_to_pinned_candidate_contract() -> None:
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_rebalance_v1", paper_mode=True, record_dry_run=True)
    )

    assert cfg.rmom_quantile == pytest.approx(0.25)
    assert cfg.feature_set == ("max_ret168",)
    assert cfg.liq_turnover_min == pytest.approx(500_000.0)
    assert cfg.max_hold_hours == 24
    assert cfg.entry_confirm_delay_hours == 1
    assert cfg.entry_event_trigger == "turn4_pop4"
    assert cfg.btc_trend_gate == "uptrend"
    assert cfg.daily_rebalance_enabled is True
    assert continuous_rebalance_rule(cfg).target_daily_vol == pytest.approx(0.025)


def test_continuous_rebalance_mode_requires_persistence_or_submitted_demo() -> None:
    _validate_continuous_demo_config(ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True))
    _validate_continuous_demo_config(
        ContinuousDemoCycleConfig(
            daily_rebalance_enabled=True,
            submit_orders=True,
            confirm_demo_orders=True,
            record_dry_run=False,
        )
    )

    with pytest.raises(ValueError, match="requires record_dry_run"):
        _validate_continuous_demo_config(ContinuousDemoCycleConfig(daily_rebalance_enabled=True))


def test_rebalance_cycle_fields_bootstrap_marks_without_fake_return() -> None:
    cfg = ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True)
    day = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": continuous_strategy_id(cfg),
                "symbol": "ABCUSDT",
                "status": "open",
                "entry_ts_ms": day - MS_PER_DAY,
                "entry_price": 100.0,
                "qty": "2",
                "equity_usdt": 10_000.0,
            }
        ],
        infer_schema_length=None,
    )

    fields = _continuous_rebalance_cycle_fields(
        trades,
        pl.DataFrame(),
        price_by_symbol={"ABCUSDT": 90.0},
        current_day_ts=day,
        now_ms=day + 1,
        strategy_id=continuous_strategy_id(cfg),
        rule=continuous_rebalance_rule(cfg),
    )

    assert fields["rebalance_raw_return"] == pytest.approx(0.0)
    assert fields["rebalance_scaled_equity"] == pytest.approx(1.0)
    assert fields["rebalance_marked_trades"] == 1
    assert fields["rebalance_mark_prices_json"] == _continuous_rebalance_mark_prices_json({"t1": 90.0})


def test_rebalance_cycle_fields_marks_open_short_from_prior_mark() -> None:
    cfg = ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True)
    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    day1 = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_scaled_equity": 1.01,
                "rebalance_scaled_peak": 1.01,
                "rebalance_mark_prices_json": _continuous_rebalance_mark_prices_json({"t1": 100.0}),
            }
        ],
        infer_schema_length=None,
    )
    trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": continuous_strategy_id(cfg),
                "symbol": "ABCUSDT",
                "status": "open",
                "entry_ts_ms": day0 - MS_PER_DAY,
                "entry_price": 80.0,
                "qty": "2",
                "equity_usdt": 10_000.0,
            }
        ],
        infer_schema_length=None,
    )

    fields = _continuous_rebalance_cycle_fields(
        trades,
        cycles,
        price_by_symbol={"ABCUSDT": 90.0},
        current_day_ts=day1,
        now_ms=day1 + 1,
        strategy_id=continuous_strategy_id(cfg),
        rule=continuous_rebalance_rule(cfg),
    )

    expected_raw = ((100.0 - 90.0) / 100.0) * (2.0 * 100.0 / 10_000.0)
    assert fields["rebalance_raw_return"] == pytest.approx(expected_raw)
    assert fields["rebalance_target_scale"] == pytest.approx(1.0)
    assert fields["rebalance_scaled_equity"] == pytest.approx(1.01 * (1.0 + expected_raw))
    assert fields["rebalance_mark_prices_json"] == _continuous_rebalance_mark_prices_json({"t1": 90.0})


def test_rebalance_cycle_fields_normalizes_by_prior_target_scale() -> None:
    cfg = ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True)
    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    day1 = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_scaled_equity": 1.02,
                "rebalance_scaled_peak": 1.02,
                "rebalance_target_scale": 2.0,
                "rebalance_mark_prices_json": _continuous_rebalance_mark_prices_json({"t1": 100.0}),
            }
        ],
        infer_schema_length=None,
    )
    trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": continuous_strategy_id(cfg),
                "symbol": "ABCUSDT",
                "status": "open",
                "entry_ts_ms": day0 - MS_PER_DAY,
                "entry_price": 80.0,
                "qty": "4",
                "equity_usdt": 10_000.0,
            }
        ],
        infer_schema_length=None,
    )

    fields = _continuous_rebalance_cycle_fields(
        trades,
        cycles,
        price_by_symbol={"ABCUSDT": 90.0},
        current_day_ts=day1,
        now_ms=day1 + 1,
        strategy_id=continuous_strategy_id(cfg),
        rule=continuous_rebalance_rule(cfg),
    )

    observed_scaled = ((100.0 - 90.0) / 100.0) * (4.0 * 100.0 / 10_000.0)
    assert fields["rebalance_raw_return"] == pytest.approx(observed_scaled / 2.0)


def test_rebalance_cycle_fields_includes_closed_trade_since_prior_mark() -> None:
    cfg = ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True)
    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    day1 = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_mark_prices_json": _continuous_rebalance_mark_prices_json({"t1": 100.0}),
            }
        ],
        infer_schema_length=None,
    )
    trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": continuous_strategy_id(cfg),
                "symbol": "ABCUSDT",
                "status": "closed",
                "entry_ts_ms": day0 - MS_PER_DAY,
                "exit_ts_ms": day1 + 1000,
                "entry_price": 80.0,
                "exit_price": 90.0,
                "qty": "2",
                "equity_usdt": 10_000.0,
            }
        ],
        infer_schema_length=None,
    )

    fields = _continuous_rebalance_cycle_fields(
        trades,
        cycles,
        price_by_symbol={},
        current_day_ts=day1,
        now_ms=day1 + 2000,
        strategy_id=continuous_strategy_id(cfg),
        rule=continuous_rebalance_rule(cfg),
    )

    expected_raw = ((100.0 - 90.0) / 100.0) * (2.0 * 100.0 / 10_000.0)
    assert fields["rebalance_raw_return"] == pytest.approx(expected_raw)
    assert fields["rebalance_closed_contributors"] == 1
    assert fields["rebalance_mark_prices_json"] == "{}"


def test_rebalance_resize_checked_today_uses_current_day_only() -> None:
    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {"rebalance_day_ts": day0, "rebalance_resize_checked": True},
            {"rebalance_day_ts": day0 + MS_PER_DAY, "rebalance_resize_checked": False},
        ],
        infer_schema_length=None,
    )

    assert _continuous_rebalance_resize_checked_today(cycles, current_day_ts=day0) is True
    assert _continuous_rebalance_resize_checked_today(cycles, current_day_ts=day0 + MS_PER_DAY) is False
    assert _continuous_rebalance_resize_checked_today(pl.DataFrame(), current_day_ts=day0) is False


def test_continuous_cycle_daily_rebalance_resizes_once_per_day(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import liquidity_migration.continuous_demo as cd
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.storage import read_dataset, write_dataset

    cfg = ContinuousDemoCycleConfig(
        submit_orders=False,
        record_dry_run=True,
        daily_rebalance_enabled=True,
        stop_approach_frac=0.0,
        failed_fade_hours=9999,
        max_hold_hours=9999,
    )
    root = tmp_path / "continuous-rebalance-cycle"
    trades_ds, orders_ds, cycles_ds = continuous_dataset_names(cfg)
    now = 1_700_000_000_000
    day = (now // MS_PER_DAY) * MS_PER_DAY
    strat = continuous_strategy_id(cfg)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t1",
                    "strategy_id": strat,
                    "symbol": "ABCUSDT",
                    "side": "short",
                    "sleeve": "continuous",
                    "status": "open",
                    "entry_ts_ms": day - MS_PER_DAY,
                    "entry_price": 100.0,
                    "qty": "1",
                    "notional_usdt": 100.0,
                    "equity_usdt": 10_000.0,
                    "qty_step": 0.1,
                }
            ],
            infer_schema_length=None,
        ),
        root,
        trades_ds,
        partition_by=(),
    )

    price = {"value": 100.0}

    def fake_resolve_cycle_universe(**_kwargs: object) -> tuple[pl.DataFrame, list[str], pl.DataFrame, str]:
        universe = pl.DataFrame(
            [
                {
                    "symbol": "ABCUSDT",
                    "tick_size": 0.0001,
                    "qty_step": 0.1,
                    "min_order_qty": 0.1,
                    "min_notional_value": 1.0,
                    "max_market_order_qty": 10_000.0,
                }
            ],
            infer_schema_length=None,
        )
        tickers = pl.DataFrame(
            [{"symbol": "ABCUSDT", "mark_price": price["value"], "last_price": price["value"]}],
            infer_schema_length=None,
        )
        return universe, ["ABCUSDT"], tickers, "test"

    def fake_klines(*_args: object, **_kwargs: object) -> tuple[pl.DataFrame, dict[str, int]]:
        return pl.DataFrame(), {"store_rows": 0}

    monkeypatch.setattr(cd, "_resolve_cycle_universe", fake_resolve_cycle_universe)
    monkeypatch.setattr(cd, "_download_recent_1h_klines", fake_klines)

    first = cd.run_continuous_demo_cycle(root, config=ResearchConfig(), demo_config=cfg, now_ms=now)
    after_first = read_dataset(root, trades_ds)
    resized = after_first.filter(pl.col("trade_id") == "t1").to_dicts()[0]
    first_orders = read_dataset(root, orders_ds)

    assert first["rebalance_resize_orders"] == 1
    assert first["rebalance_resize_skipped_same_day"] is False
    assert resized["qty"] == "2"
    assert first_orders.filter(pl.col("resize_reason") == "rebalance_increase").height == 1

    price["value"] = 50.0
    second = cd.run_continuous_demo_cycle(root, config=ResearchConfig(), demo_config=cfg, now_ms=now + MS_PER_HOUR)
    after_second = read_dataset(root, trades_ds)
    second_trade = after_second.filter(pl.col("trade_id") == "t1").to_dicts()[0]
    second_orders = read_dataset(root, orders_ds)
    cycles = read_dataset(root, cycles_ds)

    assert second["rebalance_resize_orders"] == 0
    assert second["rebalance_resize_skipped_same_day"] is True
    assert second_trade["qty"] == "2"
    assert second_orders.filter(pl.col("resize_reason") == "rebalance_increase").height == 1
    assert cycles.filter(pl.col("rebalance_resize_checked") == True).height == 2  # noqa: E712


def test_load_rmom_table_degrades_to_none_on_corrupt_file(tmp_path) -> None:
    """A corrupt/torn residual_momentum.parquet must DEGRADE to None (rmom-absent,
    which the watchdog pages on) rather than raise out of the cycle and write no
    cycle row -- which would blind the rmom-staleness guard to a crashing cycle."""
    from liquidity_migration.continuous_demo import _load_rmom_table

    (tmp_path / "residual_momentum.parquet").write_bytes(b"not a parquet file")
    assert _load_rmom_table(tmp_path) is None
    (tmp_path / "residual_momentum.parquet").unlink()
    assert _load_rmom_table(tmp_path) is None  # absent file unchanged


def test_dataset_names_separate_from_other_sleeves() -> None:
    demo = continuous_dataset_names(ContinuousDemoCycleConfig())
    paper = continuous_dataset_names(ContinuousDemoCycleConfig(paper_mode=True))
    assert demo == ("continuous_fade_demo_trades", "continuous_fade_demo_orders", "continuous_fade_demo_cycles")
    assert paper[0] == "continuous_fade_paper_trades"
    assert "event_demo" not in demo[0] and "long_native" not in demo[0]


def test_shipped_default_carries_a_disaster_stop() -> None:
    """Safety guard: the SHIPPED live config must default to a non-zero protective stop, and every
    entry it produces must carry a server-side stop ABOVE entry. Makes an unstopped continuous short
    impossible to ship by accident (the state exit is a PROFIT exit, not a risk control)."""
    from liquidity_migration.continuous_demo import _execute_continuous_entries

    cfg = ContinuousDemoCycleConfig()  # the SHIPPED default
    assert cfg.stop_loss_pct > 0.0, "live continuous sleeve must ship with a disaster stop"
    cand = [{"symbol": "WIFUSDT", "decile": 9, "composite": 0.9, "turnover_quote": 2e6,
             "signal_ts_ms": 1_700_000_000_000, "stop_loss_pct": cfg.stop_loss_pct, "live_price": 100.0}]
    rows, orders = _execute_continuous_entries(
        cand, trading_client=None, demo=cfg, equity_usdt=10_000.0,
        order_notional_frac=cfg.per_position_notional_pct_equity / 100.0,
        price_by_symbol={"WIFUSDT": 100.0},
        contract_by_symbol={"WIFUSDT": {"tick_size": 0.0001, "qty_step": 0.001, "min_order_qty": 0.001}},
        now_ms=1_700_000_000_000, strategy_id="continuous_fade_v1", record_preflight=None,
        execution_event_router=None)
    assert rows[0]["stop_price"] > rows[0]["entry_price"] > 0.0   # short stop is above entry, non-zero
    assert rows[0]["stop_loss_pct"] == cfg.stop_loss_pct


def test_daemon_constructs_without_network(tmp_path) -> None:
    """ContinuousDemoDaemon must construct (reusing the long scaffolding) with the continuous cycle
    runner + a separate data root, no network/credentials touched at __init__."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import run_continuous_demo_cycle
    from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon

    daemon = ContinuousDemoDaemon(
        tmp_path / "bybit-continuous-demo-event",
        config=ResearchConfig(),
        demo_config=ContinuousDemoCycleConfig(submit_orders=False),
        interval_seconds=60.0,
    )
    assert daemon._cycle_runner is run_continuous_demo_cycle
    assert daemon.interval_seconds == 60.0


def test_cycle_summary_formatter_routing_is_sleeve_safe(tmp_path) -> None:
    """Regression pin for the 2026-06-02 KeyError:'cycle' prod bug: the continuous daemon subclasses
    the long daemon, so the inherited long formatter would KeyError on the continuous FLAT payload and
    the daemon's `except Exception: _logger.exception` swallowed it opaquely every cycle. The long
    formatter must now fail LOUD + self-describing on a flat payload; the continuous formatter must
    handle it; and the continuous daemon must route to its own formatter (override intact)."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import format_continuous_demo_cycle_summary
    from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon
    from liquidity_migration.long_native_event_demo import format_long_demo_cycle_summary

    flat = {"cycle_id": "c1", "ts_ms": 1_700_000_000_000, "mode": "dry_run",
            "universe_symbols": 5, "live_d9_symbols": 2, "rmom_present": True, "entries": [], "exits": []}

    # (1) the inherited long formatter fails LOUD + self-describing (not a bare KeyError('cycle')).
    with pytest.raises(KeyError, match="FLAT payload"):
        format_long_demo_cycle_summary(flat)
    # (2) the continuous formatter handles the flat shape.
    assert isinstance(format_continuous_demo_cycle_summary(flat), str)
    # (3) the continuous daemon routes to its OWN formatter (the override that fixed the prod bug).
    daemon = ContinuousDemoDaemon(
        tmp_path / "bybit-continuous-demo-event", config=ResearchConfig(),
        demo_config=ContinuousDemoCycleConfig(submit_orders=False), interval_seconds=60.0)
    assert isinstance(daemon._format_cycle_summary(flat), str)


def test_cycle_summary_surfaces_addon_primary_pnl_gate_skips() -> None:
    msg = format_continuous_demo_cycle_summary({
        "cycle_id": "c1",
        "mode": "dry_run",
        "universe_symbols": 5,
        "rmom_present": True,
        "live_d9_symbols": 2,
        "candidates": 1,
        "entries": 0,
        "exits": 0,
        "open_positions": 1,
        "equity_usdt": 10_000.0,
        "entry_paused": False,
        "addon_primary_pnl_gate_skips": 2,
        "addon_same_symbol_entry_cooldown_symbols": 3,
    })
    assert "addon_pnl_gate_skips=2" in msg
    assert "addon_entry_cooldown_symbols=3" in msg


# ============================================================================
# Tier 2 — LivePanelCache equivalence (the repo's np.allclose gate)
# ============================================================================

def _dispersed_synth(n_symbols: int = 60, n_bars: int = 460, start: int = 1_700_000_000_000):
    """Continuous-price synthetic with DISTINCT per-symbol vol/drift (so feature values + composites
    are well-separated, the realistic live regime) plus 3 late-listing symbols and 1 gapped symbol —
    the cache must reproduce the full recompute through warmup, gaps and young names."""
    start -= start % MS_PER_DAY
    rows = []
    for s in range(n_symbols):
        rng = np.random.default_rng(1000 + s)
        p = 10.0 + 3.0 * s
        vol = 0.015 + 0.05 * (s / n_symbols)
        drift = -0.002 + 0.00012 * s
        first = (n_bars - 160) if s >= n_symbols - 3 else 0          # 3 young symbols
        for i in range(n_bars):
            if i < first:
                continue
            if s == n_symbols - 4 and i % 41 == 0 and i > first + 5:  # 1 gapped symbol
                continue
            p = max(0.5, p * (1.0 + drift + rng.normal(0, vol)))
            rows.append({"ts_ms": start + i * MS_PER_HOUR, "symbol": f"S{s:02d}", "close": p,
                         "turnover_quote": 500_000.0 + 10_000.0 * s})
    klines = pl.DataFrame(rows)
    days = sorted({(start + i * MS_PER_HOUR) // MS_PER_DAY * MS_PER_DAY for i in range(n_bars)})
    rng = np.random.default_rng(42)
    rmom = pl.DataFrame([{"symbol": f"S{s:02d}", "day_ts": d, "residual_momentum": float(rng.normal(0, 0.01))}
                         for d in days for s in range(n_symbols)])
    return klines, rmom, start, n_bars


def _decile_map(df: pl.DataFrame) -> dict[str, int]:
    return {r["symbol"]: r["decile"] for r in df.to_dicts()}


def _composite_map(df: pl.DataFrame) -> dict[str, float]:
    return {r["symbol"]: r["composite"] for r in df.to_dicts()}


def test_live_panel_cache_matches_full_recompute() -> None:
    """Tier-2 gate: LivePanelCache.state is np.allclose on composite and EXACT on the operative
    trading sets (D9 entry membership + the hysteresis hold-band, decile>=8) vs the full recompute
    build_live_continuous_state, across mature timestamps incl. young + gapped symbols."""
    klines, rmom, start, n_bars = _dispersed_synth()
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache(rmom_quantile=cfg.rmom_quantile, exclude_symbols=cfg.exclude_symbols)
    checked = 0
    for T_i in (180, 240, 300, 360, 420, n_bars - 1):
        T = start + T_i * MS_PER_HOUR
        hist = klines.filter(pl.col("ts_ms") < T)
        price = {r["symbol"]: r["close"] for r in klines.filter(pl.col("ts_ms") == T).to_dicts()}
        if not price:
            continue
        ref = build_live_continuous_state(hist, price, rmom, now_ts_ms=T + 1_800_000, config=cfg)
        cac = cache.state(hist, price, rmom, now_ts_ms=T + 1_800_000, config=cfg)
        rd, cd = _decile_map(ref), _decile_map(cac)
        rc, cc = _composite_map(ref), _composite_map(cac)
        assert set(rd) == set(cd), f"symbol set diverged at T={T_i}"
        inter = sorted(set(rc) & set(cc))
        ref_c = [rc[s] for s in inter if rc[s] is not None and cc[s] is not None]
        cac_c = [cc[s] for s in inter if rc[s] is not None and cc[s] is not None]
        assert np.allclose(ref_c, cac_c, atol=1e-9, rtol=1e-6), f"composite not allclose at T={T_i}"
        assert {s for s, d in rd.items() if d == 9} == {s for s, d in cd.items() if d == 9}, \
            f"D9 entry membership diverged at T={T_i}"
        assert {s for s, d in rd.items() if d >= 8} == {s for s, d in cd.items() if d >= 8}, \
            f"hold-band membership diverged at T={T_i}"
        checked += 1
    assert checked >= 5


def test_live_panel_cache_intra_hour_reuse() -> None:
    """Within one hour slot the confirmed-bar carry is reused: only the live-price term refreshes
    (refreshes stays 1, live_updates increments), and the result still matches the full recompute."""
    klines, rmom, start, n_bars = _dispersed_synth()
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache(rmom_quantile=cfg.rmom_quantile, exclude_symbols=cfg.exclude_symbols)
    T = start + (n_bars - 1) * MS_PER_HOUR
    hist = klines.filter(pl.col("ts_ms") < T)
    base = {r["symbol"]: r["close"] for r in klines.filter(pl.col("ts_ms") == T).to_dicts()}
    for k, bump in enumerate((1.0, 1.01, 0.98, 1.03)):
        price = {s: p * bump for s, p in base.items()}
        # same hour slot -> now_ts within the hour
        cac = cache.state(hist, price, rmom, now_ts_ms=T + 60_000 * (k + 1), config=cfg)
        ref = build_live_continuous_state(hist, price, rmom, now_ts_ms=T + 60_000 * (k + 1), config=cfg)
        assert {s for s, d in _decile_map(cac).items() if d == 9} == \
               {s for s, d in _decile_map(ref).items() if d == 9}
    assert cache.refreshes == 1            # heavy recompute happened ONCE
    assert cache.live_updates == 4         # cheap re-rank happened every wake


def test_live_panel_cache_invalidates_on_redelivered_confirmed_bar() -> None:
    """Within the SAME hour, if a confirmed bar is backfilled/re-delivered with corrected values
    (Bybit re-pushes a just-closed bar after late trades), the cache MUST re-refresh — the content
    signature catches it — and still match the full recompute. Without this the carry goes stale."""
    klines, rmom, start, n_bars = _dispersed_synth()
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache(rmom_quantile=cfg.rmom_quantile, exclude_symbols=cfg.exclude_symbols)
    T = start + (n_bars - 1) * MS_PER_HOUR
    hist = klines.filter(pl.col("ts_ms") < T)
    price = {r["symbol"]: r["close"] for r in klines.filter(pl.col("ts_ms") == T).to_dicts()}
    cache.state(hist, price, rmom, now_ts_ms=T, config=cfg)
    assert cache.refreshes == 1
    cache.state(hist, price, rmom, now_ts_ms=T + 30_000, config=cfg)   # same hour, same klines -> reuse
    assert cache.refreshes == 1
    # re-deliver the last confirmed bar of one symbol with a corrected close (same hour slot)
    last_ts = int(hist["ts_ms"].max())
    mutated = hist.with_columns(
        pl.when((pl.col("symbol") == "S05") & (pl.col("ts_ms") == last_ts))
        .then(pl.col("close") * 1.05).otherwise(pl.col("close")).alias("close"))
    cac = cache.state(mutated, price, rmom, now_ts_ms=T + 60_000, config=cfg)
    assert cache.refreshes == 2          # signature change detected -> re-refreshed (not stale)
    ref = build_live_continuous_state(mutated, price, rmom, now_ts_ms=T + 60_000, config=cfg)
    assert {s for s, d in _decile_map(cac).items() if d == 9} == \
           {s for s, d in _decile_map(ref).items() if d == 9}


def test_live_panel_cache_refreshes_on_hour_change() -> None:
    klines, rmom, start, n_bars = _dispersed_synth()
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache(rmom_quantile=cfg.rmom_quantile, exclude_symbols=cfg.exclude_symbols)
    for T_i in (300, 360, 420):
        T = start + T_i * MS_PER_HOUR
        hist = klines.filter(pl.col("ts_ms") < T)
        price = {r["symbol"]: r["close"] for r in klines.filter(pl.col("ts_ms") == T).to_dicts()}
        cache.state(hist, price, rmom, now_ts_ms=T, config=cfg)
    assert cache.refreshes == 3 and cache.live_updates == 3
    assert cache.confirmed_hour() == start + 420 * MS_PER_HOUR


# ============================================================================
# Tier 1 — anti-thrash (hysteresis + re-entry cooldown)
# ============================================================================

def _state(rows: list[tuple[str, int]]) -> pl.DataFrame:
    return pl.DataFrame({"symbol": [s for s, _ in rows], "decile": [d for _, d in rows],
                         "composite": [0.9] * len(rows), "turnover_quote": [1e6] * len(rows)})


def test_hysteresis_holds_in_buffer_band_exits_when_clearly_out() -> None:
    """exit_decile_buffer=1: a held name still at D8 (in the wobble band) is HELD; only a name that
    has dropped to D7 or below (or vanished from the panel) covers as left_decile."""
    cfg = ContinuousDemoCycleConfig(decile=9, exit_decile_buffer=1, breakeven_arm_pct=0.0,
                                    stop_approach_frac=0.0, max_hold_hours=0, failed_fade_hours=0)
    now = 2_000_000_000_000
    state = _state([("HOLD8", 8), ("OUT7", 7), ("HOLD9", 9)])
    trades = [
        {"symbol": "HOLD8", "entry_ts_ms": now - 2 * MS_PER_HOUR, "entry_price": 100.0},
        {"symbol": "OUT7", "entry_ts_ms": now - 2 * MS_PER_HOUR, "entry_price": 100.0},
        {"symbol": "HOLD9", "entry_ts_ms": now - 2 * MS_PER_HOUR, "entry_price": 100.0},
        {"symbol": "GONE", "entry_ts_ms": now - 2 * MS_PER_HOUR, "entry_price": 100.0},  # absent from panel
    ]
    exits = {e["symbol"]: e["exit_reason"] for e in plan_continuous_exits(trades, state, now_ms=now, config=cfg)}
    assert exits == {"OUT7": "left_decile", "GONE": "left_decile"}   # HOLD8/HOLD9 retained


def test_hysteresis_buffer_zero_is_legacy_exit_on_leaving_top_decile() -> None:
    """buffer=0 reproduces the original behaviour: cover the instant a name leaves D9 (D8 exits)."""
    cfg = ContinuousDemoCycleConfig(decile=9, exit_decile_buffer=0, breakeven_arm_pct=0.0,
                                    stop_approach_frac=0.0, max_hold_hours=0, failed_fade_hours=0)
    now = 2_000_000_000_000
    state = _state([("D8", 8), ("D9", 9)])
    trades = [{"symbol": "D8", "entry_ts_ms": now - MS_PER_HOUR, "entry_price": 100.0},
              {"symbol": "D9", "entry_ts_ms": now - MS_PER_HOUR, "entry_price": 100.0}]
    exits = {e["symbol"]: e["exit_reason"] for e in plan_continuous_exits(trades, state, now_ms=now, config=cfg)}
    assert exits == {"D8": "left_decile"}


def test_reentry_cooldown_blocks_recently_exited_symbol() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame([
        {"trade_id": "t1", "strategy_id": "continuous_fade_v1", "symbol": "FRESH", "status": "closed",
         "exit_ts_ms": now - 10 * 60_000},                       # exited 10 min ago -> in cooldown
        {"trade_id": "t2", "strategy_id": "continuous_fade_v1", "symbol": "OLD", "status": "closed",
         "exit_ts_ms": now - 120 * 60_000},                      # exited 2h ago -> clear
        {"trade_id": "t3", "strategy_id": "other", "symbol": "OTHER", "status": "closed",
         "exit_ts_ms": now - 1 * 60_000},                        # different strategy -> ignored
    ])
    cooled = _recent_exit_cooldown_symbols(trades, now_ms=now, cooldown_minutes=30,
                                           strategy_id="continuous_fade_v1")
    assert cooled == {"FRESH"}
    # cooldown disabled
    assert _recent_exit_cooldown_symbols(trades, now_ms=now, cooldown_minutes=0,
                                         strategy_id="continuous_fade_v1") == set()


def test_recent_entry_cooldown_blocks_recent_addon_entry_symbol() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame([
        {"trade_id": "t1", "strategy_id": "continuous_fade_addon_v1", "symbol": "FRESH", "entry_ts_ms": now - 10 * 60_000},
        {"trade_id": "t2", "strategy_id": "continuous_fade_addon_v1", "symbol": "OLD", "entry_ts_ms": now - 120 * 60_000},
        {"trade_id": "t3", "strategy_id": "continuous_fade_v1", "symbol": "PRIMARY", "entry_ts_ms": now - 1 * 60_000},
        {"trade_id": "t4", "strategy_id": "continuous_fade_addon_v1", "symbol": "SIGNAL", "signal_ts_ms": now - 5 * 60_000},
    ])

    cooled = _recent_entry_cooldown_symbols(
        trades,
        now_ms=now,
        cooldown_minutes=30,
        strategy_id="continuous_fade_addon_v1",
    )

    assert cooled == {"FRESH", "SIGNAL"}
    assert _recent_entry_cooldown_symbols(
        trades,
        now_ms=now,
        cooldown_minutes=0,
        strategy_id="continuous_fade_addon_v1",
    ) == set()


def test_entry_circuit_breaker_trips_on_adverse_cluster_and_self_clears() -> None:
    """Correlated-squeeze defense: pause entries once >= N adverse covers land within the window;
    disabled by default; self-clears as the cluster ages out of the window (stateless)."""
    now = 2_000_000_000_000
    strat = "continuous_fade_v1"

    def _rows(n, age_min, reason="stop_approach", nr=-0.01):
        return [{"trade_id": f"t{reason}{i}", "strategy_id": strat, "symbol": f"S{i}", "status": "closed",
                 "exit_ts_ms": now - age_min * 60_000, "exit_reason": reason, "net_return": nr} for i in range(n)]

    cfg = ContinuousDemoCycleConfig(entry_pause_after_adverse_exits=5, entry_pause_window_minutes=30)
    df4 = pl.DataFrame(_rows(4, 5))
    assert entry_circuit_breaker_tripped(df4, now_ms=now, config=cfg, strategy_id=strat) == (False, 4)
    df5 = pl.DataFrame(_rows(4, 5) + _rows(1, 5, reason="failed_fade", nr=-0.02))
    tripped, n = entry_circuit_breaker_tripped(df5, now_ms=now, config=cfg, strategy_id=strat)
    assert tripped and n == 5
    # DISABLED when threshold 0 -> never trips
    assert entry_circuit_breaker_tripped(df5, now_ms=now,
                                         config=ContinuousDemoCycleConfig(entry_pause_after_adverse_exits=0),
                                         strategy_id=strat) == (False, 0)
    # old adverse covers age out of the 30-min window
    old = pl.DataFrame(_rows(10, 60))
    assert entry_circuit_breaker_tripped(old, now_ms=now, config=cfg, strategy_id=strat) == (False, 0)


def test_shipped_default_enables_circuit_breaker_at_w24_n8() -> None:
    """Operator-directed 2026-06-02: the SHIPPED live config arms the breaker at the engine-tested
    w24/n8 (8 adverse covers in 1440min) as protective tail insurance. Guard against an accidental
    default flip."""
    cfg = ContinuousDemoCycleConfig()
    assert cfg.entry_pause_after_adverse_exits == 8
    assert cfg.entry_pause_window_minutes == 1440
    now = 2_000_000_000_000
    strat = "continuous_fade_v1"
    rows = [{"trade_id": f"t{i}", "strategy_id": strat, "symbol": f"S{i}", "status": "closed",
             "exit_ts_ms": now - 60 * 60_000, "exit_reason": "failed_fade", "net_return": -0.02}
            for i in range(8)]   # 8 adverse covers an hour ago, inside the 24h window
    tripped, n = entry_circuit_breaker_tripped(pl.DataFrame(rows), now_ms=now, config=cfg, strategy_id=strat)
    assert tripped and n == 8
    # profitable covers (net_return>=0, non-loss reason) are NOT adverse
    wins = pl.DataFrame([{"trade_id": f"w{i}", "strategy_id": strat, "symbol": f"W{i}", "status": "closed",
                          "exit_ts_ms": now - 5 * 60_000, "exit_reason": "left_decile", "net_return": 0.02}
                         for i in range(8)])
    assert entry_circuit_breaker_tripped(wins, now_ms=now, config=cfg, strategy_id=strat) == (False, 0)


def test_cooldown_symbols_excluded_from_entries() -> None:
    state = _state([("A", 9), ("B", 9)])
    cfg = ContinuousDemoCycleConfig(decile=9)
    out = select_continuous_entries(state, held_symbols=set(), cooldown_symbols={"A"},
                                    open_count=0, config=cfg)
    assert [r["symbol"] for r in out] == ["B"]   # A in cooldown -> skipped


# ============================================================================
# Tier 1 — tick-driven protective exits (stop-approach + state-free planner)
# ============================================================================

def test_stop_approach_reason_fires_near_disaster_stop() -> None:
    cfg = ContinuousDemoCycleConfig(stop_loss_pct=0.25, stop_approach_frac=0.8,
                                    breakeven_arm_pct=0.0, failed_fade_hours=0, max_hold_hours=0)
    # threshold = 0.8 * 0.25 = 0.20 loss
    assert _protective_exit_reason(held_ms=0, mfe=0.0, cur_ret=-0.21, config=cfg) == "stop_approach"
    assert _protective_exit_reason(held_ms=0, mfe=0.0, cur_ret=-0.19, config=cfg) is None
    # disabled
    off = ContinuousDemoCycleConfig(stop_loss_pct=0.25, stop_approach_frac=0.0,
                                    breakeven_arm_pct=0.0, failed_fade_hours=0, max_hold_hours=0)
    assert _protective_exit_reason(held_ms=0, mfe=0.0, cur_ret=-0.30, config=off) is None


def test_plan_protective_exits_is_state_free_and_uses_live_price() -> None:
    """plan_protective_exits needs NO panel: stop_approach fires purely off the live price, and a
    short still in profit (no giveback) is held."""
    now = 1_700_000_000_000
    cfg = ContinuousDemoCycleConfig(stop_loss_pct=0.25, stop_approach_frac=0.8, breakeven_arm_pct=0.10,
                                    failed_fade_hours=0, max_hold_hours=0)
    # stop_approach: short entered at 100, live 130 -> -30% loss (past the 0.8*0.25=0.20 trigger)
    sa = plan_protective_exits([{"symbol": "A", "entry_ts_ms": now - MS_PER_HOUR, "entry_price": 100.0}],
                               now_ms=now, config=cfg, klines=None, price_by_symbol={"A": 130.0})
    assert [e["exit_reason"] for e in sa] == ["stop_approach"]
    # live 88 = short up 12% (in profit, hasn't given it back) -> no exit; mfe>=arm but cur_ret>0
    none_yet = plan_protective_exits([{"symbol": "A", "entry_ts_ms": now - MS_PER_HOUR, "entry_price": 100.0}],
                                     now_ms=now, config=cfg, klines=None, price_by_symbol={"A": 88.0})
    assert none_yet == []


def test_plan_protective_exits_breakeven_from_live_dip_then_giveback() -> None:
    """A short whose only record of the 12% favorable excursion is a confirmed kline low covers at
    breakeven once the live price returns to entry — the tick loop's live-inclusive MFE path."""
    now = 1_700_000_000_000
    cfg = ContinuousDemoCycleConfig(stop_approach_frac=0.0, breakeven_arm_pct=0.10,
                                    failed_fade_hours=0, max_hold_hours=0)
    kl = _hold_klines("A", [100.0, 92.0, 88.0, 96.0], start=now - 3 * MS_PER_HOUR)
    out = plan_protective_exits([{"symbol": "A", "entry_ts_ms": now - 3 * MS_PER_HOUR, "entry_price": 100.0}],
                                now_ms=now, config=cfg, klines=kl, price_by_symbol={"A": 100.0})
    assert [e["exit_reason"] for e in out] == ["breakeven"]


# ============================================================================
# Tier 1 — run_continuous_protective_exit_cycle (the daemon's fast path)
# ============================================================================

def test_protective_exit_cycle_covers_stop_approach_dry_run(tmp_path) -> None:
    """The fast exit-only cycle reads the open continuous trade, prices it off the WS ticker cache,
    and covers a short that has run into a stop-approach loss — writing the closed row to the ledger,
    with no universe build / decile recompute / network."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import continuous_strategy_id
    from liquidity_migration.storage import read_dataset, write_dataset
    from liquidity_migration.ws_state_cache import TickerCache

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True,
                                    stop_loss_pct=0.25, stop_approach_frac=0.8)
    root = tmp_path / "bybit-continuous-demo-event"
    trades_ds, _orders, _cycles = continuous_dataset_names(cfg)
    now = 1_700_000_000_000
    strat = continuous_strategy_id(cfg)
    write_dataset(pl.DataFrame([{
        "trade_id": f"{strat}-WIFUSDT-1699", "strategy_id": strat, "symbol": "WIFUSDT", "side": "short",
        "status": "open", "entry_price": 100.0, "qty": "2", "notional_usdt": 200.0, "equity_usdt": 10_000.0,
        "entry_ts_ms": now - 3 * MS_PER_HOUR, "updated_at_ms": now - 3 * MS_PER_HOUR,
    }], infer_schema_length=None), root, trades_ds, partition_by=())

    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "130.0"}])   # short down 30% -> past 0.8*0.25 stop-approach

    payload = run_continuous_protective_exit_cycle(
        root, config=ResearchConfig(), demo_config=cfg, trading_client=None,
        kline_store=None, ticker_cache=tc, private_state_cache=None, now_ms=now)
    assert payload["exits"] == 1
    assert "stop_approach" in payload["reasons"]
    after = read_dataset(root, trades_ds)
    closed = after.filter(pl.col("status") == "closed")
    assert closed.height == 1
    assert closed["exit_reason"].to_list() == ["stop_approach"]


def test_continuous_same_window_reentry_both_rows_survive(tmp_path) -> None:
    """continuous-8: a same-signal-window cover-then-re-enter must produce DISTINCT trade_ids so the
    closed row is NOT overwritten by storage's trade_id dedup (the continuous-2 ledger data loss).
    Mirrors the cycle's seq accounting: one prior (symbol, signal_ts) trade -> the re-entry gets
    seq=1; both the closed original and the open re-entry survive write_dataset -> read_dataset."""
    from liquidity_migration.continuous_demo import _continuous_trade_id, continuous_strategy_id
    from liquidity_migration.storage import read_dataset, write_dataset

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    root = tmp_path / "bybit-continuous-demo-event"
    trades_ds, _o, _c = continuous_dataset_names(cfg)
    strat = continuous_strategy_id(cfg)
    sig = 1_700_000_000_000

    tid0 = _continuous_trade_id(strat, "WIFUSDT", sig, 0)          # original (now covered)
    tid1 = _continuous_trade_id(strat, "WIFUSDT", sig, 1)          # re-entry, seq=1 (one prior trade)
    assert tid1 != tid0

    # cycle N: the original closes
    write_dataset(pl.DataFrame([{
        "trade_id": tid0, "strategy_id": strat, "symbol": "WIFUSDT", "side": "short",
        "status": "closed", "signal_ts_ms": sig, "entry_ts_ms": sig + 60_000, "net_return": -0.01,
    }], infer_schema_length=None), root, trades_ds, partition_by=())
    # cycle N+k (same window, after cooldown): re-enter with the seq-distinct id
    write_dataset(pl.DataFrame([{
        "trade_id": tid1, "strategy_id": strat, "symbol": "WIFUSDT", "side": "short",
        "status": "open", "signal_ts_ms": sig, "entry_ts_ms": sig + 2_400_000,
    }], infer_schema_length=None), root, trades_ds, partition_by=())

    after = read_dataset(root, trades_ds)
    assert sorted(after["trade_id"].to_list()) == sorted([tid0, tid1])
    assert after.filter(pl.col("status") == "closed").height == 1
    assert after.filter(pl.col("status") == "open").height == 1

    # Contrast: the OLD scheme reused ONE trade_id across the two writes -> storage dedup keeps a
    # single row (the closed original is silently overwritten — exactly the data loss the seq fixes).
    bug_root = tmp_path / "bug"
    for status in ("closed", "open"):
        write_dataset(pl.DataFrame([{
            "trade_id": tid0, "symbol": "WIFUSDT", "side": "short", "status": status, "signal_ts_ms": sig,
        }], infer_schema_length=None), bug_root, trades_ds, partition_by=())
    assert read_dataset(bug_root, trades_ds).height == 1


def test_continuous_entry_metadata_blocks_same_signal_reentry_by_default() -> None:
    cfg = ContinuousDemoCycleConfig()
    strat = continuous_strategy_id(cfg)
    sig = 1_700_000_000_000
    prior = pl.DataFrame(
        [
            {
                "trade_id": f"{strat}-WIFUSDT-{sig}",
                "strategy_id": strat,
                "symbol": "WIFUSDT",
                "status": "closed",
                "signal_ts_ms": sig,
            },
            {
                "trade_id": f"other-OTHERUSDT-{sig}",
                "strategy_id": "other",
                "symbol": "OTHERUSDT",
                "status": "closed",
                "signal_ts_ms": sig,
            },
        ]
    )

    candidates, skipped = _continuous_entry_candidates_with_signal_metadata(
        [
            {"symbol": "WIFUSDT", "decile": 9, "composite": 1.0},
            {"symbol": "FRESHUSDT", "decile": 9, "composite": 0.9},
            {"symbol": "OTHERUSDT", "decile": 9, "composite": 0.8},
        ],
        prior,
        signal_ts=sig,
        strategy_id=strat,
        price_by_symbol={"WIFUSDT": 100.0, "FRESHUSDT": 50.0, "OTHERUSDT": 25.0},
        stop_loss_pct=cfg.stop_loss_pct,
        allow_same_signal_reentry=False,
    )

    assert skipped == 1
    assert [row["symbol"] for row in candidates] == ["FRESHUSDT", "OTHERUSDT"]
    assert [row["reentry_seq"] for row in candidates] == [0, 0]
    assert candidates[0]["trade_id"] == f"{strat}-FRESHUSDT-{sig}"


def test_continuous_entry_metadata_blocks_same_signal_order_attempt_by_default() -> None:
    cfg = ContinuousDemoCycleConfig()
    strat = continuous_strategy_id(cfg)
    sig = 1_700_000_000_000
    prior_orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-c-WIF-abc",
                "trade_id": f"{strat}-WIFUSDT-{sig}",
                "strategy_id": strat,
                "symbol": "WIFUSDT",
                "reduce_only": False,
                "status": "submitted",
                "signal_ts_ms": sig,
            },
            {
                "order_link_id": "lm-en-c-WIF-abc",
                "trade_id": f"{strat}-WIFUSDT-{sig}",
                "strategy_id": strat,
                "symbol": "WIFUSDT",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
                "signal_ts_ms": sig,
            },
            {
                "order_link_id": "lm-ux-c-WIF-exit",
                "trade_id": f"{strat}-WIFUSDT-{sig}",
                "strategy_id": strat,
                "symbol": "WIFUSDT",
                "reduce_only": True,
                "status": "submitted",
                "signal_ts_ms": sig,
            },
            {
                "order_link_id": "lm-en-c-OTHER-abc",
                "trade_id": f"other-OTHERUSDT-{sig}",
                "strategy_id": "other",
                "symbol": "OTHERUSDT",
                "reduce_only": False,
                "status": "submitted",
                "signal_ts_ms": sig,
            },
        ]
    )

    candidates, skipped = _continuous_entry_candidates_with_signal_metadata(
        [
            {"symbol": "WIFUSDT", "decile": 9, "composite": 1.0},
            {"symbol": "FRESHUSDT", "decile": 9, "composite": 0.9},
            {"symbol": "OTHERUSDT", "decile": 9, "composite": 0.8},
        ],
        pl.DataFrame(),
        all_orders=prior_orders,
        signal_ts=sig,
        strategy_id=strat,
        price_by_symbol={"WIFUSDT": 100.0, "FRESHUSDT": 50.0, "OTHERUSDT": 25.0},
        stop_loss_pct=cfg.stop_loss_pct,
        allow_same_signal_reentry=False,
    )

    assert skipped == 1
    assert [row["symbol"] for row in candidates] == ["FRESHUSDT", "OTHERUSDT"]


def test_continuous_entry_metadata_can_allow_same_signal_reentry() -> None:
    cfg = ContinuousDemoCycleConfig(allow_same_signal_reentry=True)
    strat = continuous_strategy_id(cfg)
    sig = 1_700_000_000_000
    prior = pl.DataFrame(
        [
            {
                "trade_id": f"{strat}-WIFUSDT-{sig}",
                "strategy_id": strat,
                "symbol": "WIFUSDT",
                "status": "closed",
                "signal_ts_ms": sig,
            },
        ]
    )

    candidates, skipped = _continuous_entry_candidates_with_signal_metadata(
        [{"symbol": "WIFUSDT", "decile": 9, "composite": 1.0}],
        prior,
        signal_ts=sig,
        strategy_id=strat,
        price_by_symbol={"WIFUSDT": 100.0},
        stop_loss_pct=cfg.stop_loss_pct,
        allow_same_signal_reentry=cfg.allow_same_signal_reentry,
    )

    assert skipped == 0
    assert candidates[0]["reentry_seq"] == 1
    assert candidates[0]["trade_id"] == f"{strat}-WIFUSDT-{sig}-1"


def test_protective_exit_cycle_skips_in_flight_exit(tmp_path) -> None:
    """Fast loop must NOT submit a second cover for a trade that already carries an exit_order_link_id
    (a cover in flight, unconfirmed) — the WS-snapshot-independent guard against a double reduce-only,
    even when a stop-approach loss is showing."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import continuous_strategy_id
    from liquidity_migration.storage import read_dataset, write_dataset
    from liquidity_migration.ws_state_cache import TickerCache

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True,
                                    stop_loss_pct=0.25, stop_approach_frac=0.8)
    root = tmp_path / "bybit-continuous-demo-event"
    trades_ds, _orders, _cycles = continuous_dataset_names(cfg)
    now = 1_700_000_000_000
    strat = continuous_strategy_id(cfg)
    write_dataset(pl.DataFrame([{
        "trade_id": f"{strat}-WIFUSDT-1699", "strategy_id": strat, "symbol": "WIFUSDT", "side": "short",
        "status": "open", "entry_price": 100.0, "qty": "2", "notional_usdt": 200.0, "equity_usdt": 10_000.0,
        "entry_ts_ms": now - 3 * MS_PER_HOUR, "updated_at_ms": now - 3 * MS_PER_HOUR,
        "exit_order_link_id": "lm-ux-c-WIF-zzz",   # cover already submitted, not yet confirmed closed
    }], infer_schema_length=None), root, trades_ds, partition_by=())
    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "130.0"}])   # would otherwise trigger stop_approach
    payload = run_continuous_protective_exit_cycle(
        root, config=ResearchConfig(), demo_config=cfg, ticker_cache=tc, now_ms=now)
    assert payload["exits"] == 0                  # in-flight guard suppressed the second cover
    after = read_dataset(root, trades_ds)
    assert after.filter(pl.col("status") == "closed").height == 0


def test_protective_exit_cycle_noop_without_open_trades(tmp_path) -> None:
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.ws_state_cache import TickerCache

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "100.0"}])
    payload = run_continuous_protective_exit_cycle(
        tmp_path / "root", config=ResearchConfig(), demo_config=cfg, ticker_cache=tc, now_ms=1)
    assert payload["exits"] == 0 and payload["open_positions"] == 0


# ============================================================================
# Tiers 1/3/4 — daemon reactivity wiring (no network)
# ============================================================================

def _daemon(tmp_path, **overrides):
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon
    cfg = ContinuousDemoCycleConfig(submit_orders=False, **overrides)
    return ContinuousDemoDaemon(tmp_path / "r", config=ResearchConfig(), demo_config=cfg, interval_seconds=60.0)


def test_ticker_message_nudges_fast_loop(tmp_path) -> None:
    d = _daemon(tmp_path)
    assert not d._tick_event.is_set()
    d._handle_ticker_message({"data": [{"symbol": "WIFUSDT", "lastPrice": "1.0"}]})
    assert d._tick_event.is_set()                    # Tier 1: a tick wakes the protective loop
    assert d._ticker_cache.symbol_count() == 1       # base behaviour preserved (cache updated)


def test_ticker_batch_wake_sets_bar_event_after_threshold(tmp_path) -> None:
    d = _daemon(tmp_path, ticker_batch_wake_threshold=3)
    d._handle_ticker_message({"data": [{"symbol": "A", "lastPrice": "1"}, {"symbol": "B", "lastPrice": "1"}]})
    assert not d._bar_event.is_set()                 # 2 < 3, no cycle requested yet
    d._handle_ticker_message({"data": [{"symbol": "C", "lastPrice": "1"}]})
    assert d._bar_event.is_set()                     # Tier 3: batch reached -> full cycle requested
    assert d._ticker_batch_wakes == 1 and d._ticker_update_count == 0


def test_ticker_batch_wake_off_by_default(tmp_path) -> None:
    d = _daemon(tmp_path)                             # threshold defaults to 0 (off)
    for _ in range(50):
        d._handle_ticker_message({"data": [{"symbol": "A", "lastPrice": "1"}]})
    assert not d._bar_event.is_set() and d._ticker_batch_wakes == 0


def test_execution_fill_nudge_only_for_continuous_sleeve(tmp_path) -> None:
    d = _daemon(tmp_path)
    # a SHORT-sleeve fill (4-part link) must NOT nudge the continuous daemon
    d._handle_execution_message({"data": [{"orderLinkId": "lm-en-BTC-abcd", "execId": "1"}]})
    assert d._fill_nudges == 0 and not d._tick_event.is_set() and not d._bar_event.is_set()
    # a CONTINUOUS fill (lm-en-c- / lm-ux-c-) triggers Tier-4 state refresh
    d._handle_execution_message({"data": [{"orderLinkId": "lm-en-c-WIF-abcd", "execId": "2"}]})
    assert d._fill_nudges == 1 and d._tick_event.is_set() and d._bar_event.is_set()


def test_protective_exit_check_defers_to_running_main_cycle(tmp_path) -> None:
    """The fast loop must skip when the main cycle holds the mutex (it covers exits itself), so they
    never race the ledger or double-submit."""
    d = _daemon(tmp_path, record_dry_run=True)
    d._cycle_mutex.acquire()                          # simulate the main cycle running
    try:
        d._run_protective_exit_check()                # must return immediately without acting
    finally:
        d._cycle_mutex.release()
    assert d._protective_exit_checks == 0             # never entered the body


# --- order-submit safety guard (audit 2026-06-02 #1/#9) -------------------------

def test_validate_continuous_demo_config_rejects_paper_with_submit() -> None:
    """A paper-shadow unit must never submit real demo orders."""
    with pytest.raises(ValueError, match="paper_mode"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(paper_mode=True, submit_orders=True, confirm_demo_orders=True)
        )


def test_validate_continuous_demo_config_rejects_paper_without_record_dry_run() -> None:
    with pytest.raises(ValueError, match="record_dry_run"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(paper_mode=True, submit_orders=False, record_dry_run=False)
        )


def test_validate_continuous_demo_config_requires_confirm_flag_to_submit() -> None:
    """submit_orders without --confirm-demo-orders is refused (the repo money-safety invariant
    every other live sleeve enforces; this sleeve was the one missing it)."""
    with pytest.raises(RuntimeError, match="confirm-demo-orders"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(submit_orders=True, confirm_demo_orders=False)
        )


def test_validate_continuous_demo_config_rejects_event_trigger_without_confirmed_entry() -> None:
    with pytest.raises(ValueError, match="confirmed-bar"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(entry_event_trigger="fresh_pop25", entry_confirm_delay_hours=0)
        )


def test_validate_continuous_demo_config_requires_addon_profile_for_submit_gate() -> None:
    with pytest.raises(ValueError, match="continuous_addon_v1"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                addon_primary_pnl_gate=True,
                submit_orders=True,
                confirm_demo_orders=True,
            )
        )


def test_validate_continuous_demo_config_requires_primary_root_for_submit_gate() -> None:
    with pytest.raises(ValueError, match="addon_primary_data_root"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                strategy_profile="continuous_addon_v1",
                addon_primary_pnl_gate=True,
                submit_orders=True,
                confirm_demo_orders=True,
            )
        )


def test_validate_continuous_demo_config_requires_addon_profile_for_addon_entry_cooldown() -> None:
    with pytest.raises(ValueError, match="addon_same_symbol_entry_cooldown_minutes"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(addon_same_symbol_entry_cooldown_minutes=15)
        )


def test_validate_continuous_demo_config_allows_addon_submit_identity_when_rooted(tmp_path) -> None:
    _validate_continuous_demo_config(
        ContinuousDemoCycleConfig(
            strategy_profile="continuous_addon_v1",
            addon_primary_pnl_gate=True,
            addon_primary_data_root=str(tmp_path / "primary"),
            submit_orders=True,
            confirm_demo_orders=True,
        )
    )


def test_validate_continuous_demo_config_allows_valid_demo_runs() -> None:
    # non-submitting demo run: no guard trips
    _validate_continuous_demo_config(ContinuousDemoCycleConfig(submit_orders=False))
    # paper shadow done right
    _validate_continuous_demo_config(
        ContinuousDemoCycleConfig(paper_mode=True, submit_orders=False, record_dry_run=True)
    )


def test_continuous_live_config_golden_values() -> None:
    """Pin the operator-directed live values so a silent revert toward the engine
    defaults is caught (audit 2026-06-02 #11). rmom_quantile=0.33: alpha-sweep
    2026-06-02; breaker w24/n8 + 0.25 stop: cb1 / I-phase receipts."""
    c = ContinuousDemoCycleConfig()
    assert c.rmom_quantile == 0.33
    assert c.entry_pause_after_adverse_exits == 8
    assert c.entry_pause_window_minutes == 1440
    assert c.stop_loss_pct == 0.25


def test_format_continuous_demo_cycle_summary_handles_flat_payload() -> None:
    # The continuous daemon subclasses the long daemon, whose formatter expects payload['cycle'];
    # feeding it the flat continuous payload KeyError'd every cycle (audit 2026-06-02). This formatter
    # must accept the FLAT shape and surface the rmom gate freshness.
    payload = {
        "cycle_id": "20260602-1", "mode": "submit", "universe_symbols": 550,
        "rmom_present": True, "max_rmom_day_ts": 1780444800000, "rmom_stale_days": 0,
        "live_d9_symbols": 17, "candidates": 2, "entries": 1, "exits": 0,
        "open_positions": 2, "equity_usdt": 12345.6,
    }
    s = format_continuous_demo_cycle_summary(payload)
    assert "continuous-fade demo cycle" in s
    assert "d9=17" in s and "open=2" in s and "rmom=present" in s
    # robust to a missing/None equity and an empty payload (never raises)
    assert "$0.00" in format_continuous_demo_cycle_summary({"rmom_present": False})


def test_entry_caps_qty_at_max_order_qty_documented() -> None:
    """EXEC-3: a single continuous entry whose notional exceeds Bybit's maxMktOrderQty is CAPPED to
    max_order_qty (documented behaviour), not split. Pins the cap so a future split fix is a conscious
    change. A cheap-priced name ($0.0001) at 2% of a large equity forces qty > max_qty."""
    from liquidity_migration.continuous_demo import _execute_continuous_entries

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True, per_position_notional_pct_equity=2.0)
    equity = 10_000_000.0  # large equity -> 2% = $200k notional
    price = 0.0001         # cheap coin -> raw qty = 2e9 contracts
    contract = {"tick_size": 0.00001, "qty_step": 1.0, "min_order_qty": 1.0,
                "max_market_order_qty": 1_000_000.0}  # cap well below the 2e9 raw qty
    rows, _orders = _execute_continuous_entries(
        [{"symbol": "CHEAPUSDT", "decile": 9, "composite": 0.9}],
        trading_client=None, demo=cfg, equity_usdt=equity,
        order_notional_frac=cfg.per_position_notional_pct_equity / 100.0,
        price_by_symbol={"CHEAPUSDT": price},
        contract_by_symbol={"CHEAPUSDT": contract}, now_ms=1_700_000_000_000,
        strategy_id="strat", record_preflight=None, execution_event_router=None)
    assert len(rows) == 1                       # ONE trade row (no per-child split)
    qty = float(rows[0]["qty"])
    assert qty == 1_000_000.0                    # capped at max_market_order_qty, NOT 2e9
    # documents the under-size: filled notional is the capped qty*price, far below the $200k target
    assert float(rows[0]["notional_usdt"]) == 1_000_000.0 * price


def test_left_decile_exit_keys_on_the_confirmed_selection_decile_not_a_time_floor() -> None:
    """continuous-6 (correct fix): the `left_decile` SELECTION exit fires exactly when the decile
    snapshot the caller passes (``entry_state`` = the SAME confirmed-bar decile that selected the entry)
    drops the name out of the fade band — immediately, with NO hold-time floor. The cycle passes
    ``entry_state`` (not the live intra-hour decile) so entry and exit read the identical signal, which
    is what removes the same-hour thrash; the exit itself never delays the system's signal."""
    cfg = ContinuousDemoCycleConfig(decile=9, exit_decile_buffer=1, max_hold_hours=48)
    now = 2_000_000_000_000
    fresh = {"symbol": "FRESH", "entry_ts_ms": now - 5 * 60_000}   # held only 5 min
    # 1) Confirmed decile still IN the band (D9) -> held, even for an old trade. The exit does not churn
    #    a name the selection signal still likes.
    in_band = pl.DataFrame({"symbol": ["FRESH"], "decile": [9], "composite": [0.9], "turnover_quote": [1e6]})
    assert plan_continuous_exits([fresh], in_band, now_ms=now, config=cfg) == []
    # 2) Confirmed decile dropped OUT of the band (D3 < D9-1) -> covered IMMEDIATELY, no time floor: the
    #    exit fires the instant the system's real signal says the name left the fade pool.
    out_band = pl.DataFrame({"symbol": ["FRESH"], "decile": [3], "composite": [0.1], "turnover_quote": [1e6]})
    out = plan_continuous_exits([fresh], out_band, now_ms=now, config=cfg)
    assert [e["exit_reason"] for e in out] == ["left_decile"]
    # 3) Hysteresis: a one-decile wobble (D8 == decile-buffer) is still held; only a CLEAR drop covers.
    wobble = pl.DataFrame({"symbol": ["FRESH"], "decile": [8], "composite": [0.5], "turnover_quote": [1e6]})
    assert plan_continuous_exits([fresh], wobble, now_ms=now, config=cfg) == []
    # 4) Protective squeeze cover is PRICE-driven and independent of the decile snapshot: a fresh name in
    #    the band but 25% underwater still fires stop_approach immediately.
    cfg_stop = ContinuousDemoCycleConfig(decile=9, stop_loss_pct=0.25, stop_approach_frac=0.8)
    fresh_stop = [{"symbol": "FRESH", "entry_ts_ms": now - 5 * 60_000, "entry_price": 100.0, "qty": "1"}]
    ex = plan_continuous_exits(fresh_stop, in_band, now_ms=now, config=cfg_stop,
                               price_by_symbol={"FRESH": 125.0})  # 25% loss on the short -> stop_approach
    assert [e["exit_reason"] for e in ex] == ["stop_approach"]


def test_continuous_cycle_feeds_confirmed_entry_state_to_the_exit_planner() -> None:
    """continuous-6 (root cause): the cycle must hand `plan_continuous_exits` the SAME confirmed-bar
    decile snapshot it uses for entries (`entry_state`), NOT the live intra-hour `live_state`. This
    locks the wiring so entry and exit can never again read disagreeing decile signals."""
    import inspect

    from liquidity_migration import continuous_demo as cd

    src = inspect.getsource(cd.run_continuous_demo_cycle)
    # The exit planner is called with the confirmed entry_state as its decile snapshot, never live_state.
    assert "plan_continuous_exits(" in src
    assert "open_trades.to_dicts(), entry_state" in src, "exit planner must receive the confirmed entry_state"
    assert "open_trades.to_dicts(), live_state" not in src, "exit planner must NOT receive the live decile"


def test_circuit_breaker_counts_fee_negative_covers_as_adverse() -> None:
    """continuous-4: a cover with gross net_return >= 0 but NEGATIVE after realised round-trip fees must
    count as an adverse cover (matching the engine's net-of-cost net_return<0 proxy). Pins the breaker
    reading a cost-consistent metric without mutating the stored gross net_return."""
    cfg = ContinuousDemoCycleConfig(entry_pause_after_adverse_exits=2, entry_pause_window_minutes=1440)
    now = 1_700_000_000_000
    strat = "continuous_fade_v1"
    # Two covers, each gross-positive (+0.0001) but fee tax (4 USDT on 10k equity = 0.0004 of equity)
    # makes them net-NEGATIVE. Old breaker (gross only) sees 0 adverse; fixed breaker sees 2 -> trips.
    rows = [
        {"trade_id": f"{strat}-S{i}-{now}", "strategy_id": strat, "symbol": f"S{i}", "status": "closed",
         "exit_reason": "left_decile", "exit_ts_ms": now - 60 * 60_000,
         "net_return": 0.0001, "entry_fee_usdt": 2.0, "exit_fee_usdt": 2.0, "equity_usdt": 10_000.0}
        for i in range(2)
    ]
    df = pl.DataFrame(rows, infer_schema_length=None)
    tripped, count = entry_circuit_breaker_tripped(df, now_ms=now, config=cfg, strategy_id=strat)
    assert count == 2          # both fee-negative covers counted as adverse
    assert tripped is True      # >= entry_pause_after_adverse_exits=2 -> breaker trips
    # control: a genuinely gross-positive cover whose fees do NOT flip it stays benign
    benign = pl.DataFrame([{
        "trade_id": f"{strat}-X-{now}", "strategy_id": strat, "symbol": "X", "status": "closed",
        "exit_reason": "left_decile", "exit_ts_ms": now - 60 * 60_000,
        "net_return": 0.05, "entry_fee_usdt": 2.0, "exit_fee_usdt": 2.0, "equity_usdt": 10_000.0,
    }], infer_schema_length=None)
    assert entry_circuit_breaker_tripped(benign, now_ms=now, config=cfg, strategy_id=strat)[1] == 0
