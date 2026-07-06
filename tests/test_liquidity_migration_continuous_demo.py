"""Tests for the continuous-fade demo/paper sleeve (liquidity_migration.continuous_demo).

The headline test is EQUIVALENCE: the live state (confirmed history + live price as the current bar)
must reproduce the verified backtest decile exactly — the live signal == the backtest signal. Plus
entry/exit selection and the distinct continuous orderLinkId prefix (ws_risk fill routing).
"""
from __future__ import annotations

import logging
import json
import math
import zlib

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, finite_float
from liquidity_migration.continuous_demo import (
    CONTINUOUS_ENTRY_LINK_PREFIX,
    CONTINUOUS_LIFECYCLE_EVENTS_FILE,
    CONTINUOUS_RISK_EVENTS_FILE,
    SNIPER_REASON,
    ContinuousDemoCycleConfig,
    LivePanelCache,
    _btc_risk_sizing_payload_fields,
    _btc_trend_gate_payload_fields,
    _build_continuous_rebalance_resize_rows,
    _continuous_age_eligible_symbols,
    _continuous_rebalance_cycle_fields,
    _continuous_rebalance_mark_prices_json,
    _continuous_rebalance_resize_checked_today,
    _continuous_rebalance_scale_state_from_cycles,
    _continuous_entry_candidates_with_signal_metadata,
    _continuous_entry_risk_health,
    _continuous_account_drawdown_fields,
    _append_continuous_risk_event,
    _enforce_continuous_lifecycle_transitions,
    _continuous_lifecycle_state,
    _append_continuous_lifecycle_event,
    _continuous_lifecycle_event_from_order_row,
    _continuous_lifecycle_event_from_trade_row,
    _continuous_lifecycle_snapshot_update_rows,
    _continuous_portfolio_heat_fields,
    _continuous_sniper_link_prefix,
    _continuous_suborder_link_id,
    _continuous_telegram_reason,
    _continuous_trade_id,
    _execute_continuous_rebalance_resizes,
    _execute_sniper_placements,
    _finite_or_none,
    _known_ensemble_component_tags,
    _payload_float,
    _recent_adverse_exit_count,
    _rmom_freshness_payload_fields,
    _sniper_fill_ts_ms,
    _sniper_order_state,
    _submission_error_count,
    _validate_continuous_demo_config,
    active_primary_pnl_gate_allows_addon,
    apply_continuous_demo_profile,
    build_confirmed_entry_state,
    build_live_continuous_state,
    continuous_dataset_names,
    continuous_rebalance_rule,
    filter_addon_candidates_by_active_primary_pnl_gate,
    format_continuous_demo_cycle_summary,
    format_continuous_telegram_status_message,
    _continuous_order_link_id,
    _protective_exit_reason,
    _recent_exit_cooldown_symbols,
    _recent_entry_cooldown_symbols,
    continuous_strategy_id,
    entry_circuit_breaker_tripped,
    plan_continuous_exits,
    plan_continuous_sniper_orders,
    plan_protective_exits,
    reconcile_continuous_snipes,
    recover_snipe_trade_id_from_link,
    run_continuous_protective_exit_cycle,
    select_continuous_entries,
)
from liquidity_migration.continuous_events import compute_continuous_decile_panel
from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan
from liquidity_migration.event_demo import decode_entry_order_link_id
from liquidity_migration.order_link_id import _base36, assert_routable_component_tags


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


def test_continuous_age_uses_universe_listing_age_not_kline_cache() -> None:
    """The authoritative v5 launchTime age (universe.listing_age_days) wins over the rolling
    kline cache: a genuinely-old coin (A) is eligible even though its cache only reaches a few
    days back, while a genuinely-young coin (B) is excluded — the live-vs-PIT age fix."""
    now = 1_000 * MS_PER_DAY
    universe = pl.DataFrame({"symbol": ["A", "B"], "listing_age_days": [400.0, 10.0]})
    # Cache disagrees: only ~5 days of bars for BOTH (cache-first-bar would wrongly gate out A).
    klines = pl.DataFrame(
        {"symbol": ["A", "A", "B", "B"],
         "ts_ms": [now - 5 * MS_PER_DAY, now, now - 5 * MS_PER_DAY, now]}
    )
    assert _continuous_age_eligible_symbols(universe, klines, age_days_min=300, now_ms=now) == {"A"}


def test_continuous_age_excludes_null_listing_age() -> None:
    universe = pl.DataFrame({"symbol": ["A", "B"], "listing_age_days": [400.0, None]})
    elig = _continuous_age_eligible_symbols(universe, pl.DataFrame(), age_days_min=30, now_ms=MS_PER_DAY)
    assert elig == {"A"}  # null/unknown launch age is ineligible (conservative)


def test_continuous_age_floor_zero_means_no_gate() -> None:
    universe = pl.DataFrame({"symbol": ["A"], "listing_age_days": [1.0]})
    assert _continuous_age_eligible_symbols(universe, pl.DataFrame(), age_days_min=0, now_ms=0) is None


def test_continuous_age_falls_back_to_kline_cache_without_listing_age() -> None:
    """Degraded universe (no listing_age_days) → legacy kline-cache first-bar gate."""
    now = 1_000 * MS_PER_DAY
    universe = pl.DataFrame({"symbol": ["A", "B"]})
    klines = pl.DataFrame(
        {"symbol": ["A", "B"], "ts_ms": [now - 60 * MS_PER_DAY, now - 10 * MS_PER_DAY]}
    )
    assert _continuous_age_eligible_symbols(universe, klines, age_days_min=30, now_ms=now) == {"A"}


def test_continuous_age_no_source_returns_none() -> None:
    """No listing age and no klines → cannot gate → None (preserves empty-klines cycle behavior)."""
    assert _continuous_age_eligible_symbols(pl.DataFrame(), pl.DataFrame(), age_days_min=30, now_ms=0) is None


def test_continuous_order_link_prefix_routes_as_distinct_sleeve() -> None:
    sig = 1_700_000_000_000
    link = _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig)
    assert link.startswith("lm-en-c-")
    # ws_risk must decode it as the CONTINUOUS sleeve (not short/long), recovering signal_ts
    decoded = decode_entry_order_link_id(link)
    assert decoded is not None
    sleeve, signal_ts_ms, reentry_seq, component_tag = decoded
    assert sleeve == "continuous"
    assert signal_ts_ms == (sig // 1000) * 1000
    assert reentry_seq == 0  # a first-entry (5-part) link carries no re-entry seq
    assert component_tag == ""  # plain book — no ensemble component
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
    assert decode_entry_order_link_id(link1) == ("continuous", (sig // 1000) * 1000, 1, "")
    addon_link = _continuous_order_link_id("en-ca", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=1)
    assert addon_link.startswith("lm-en-ca-")
    assert decode_entry_order_link_id(addon_link) == ("continuous_addon", (sig // 1000) * 1000, 1, "")
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
        strategy_id="continuous_fade_v2", record_preflight=None, execution_event_router=None)
    assert len(rows) == 1 and len(orders) == 1
    r, o = rows[0], orders[0]
    assert r["side"] == "short" and o["side"] == "Sell"
    assert r["entry_order_link_id"].startswith("lm-en-c-")
    assert r["stop_price"] > r["entry_price"]            # short stop is ABOVE entry
    assert abs(r["notional_usdt"] - 200.0) < 1e-6        # 2% of 10k
    assert r["status"] == "open" and o["reduce_only"] is False
    assert r["sleeve"] == "continuous"                   # self-identifying for ws_risk routing
    assert o["updated_at_ms"] == 1_700_000_000_000


def test_submitted_unconfirmed_entry_keeps_reservation_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    import liquidity_migration.continuous_demo as cd
    from liquidity_migration.continuous_demo import _continuous_accepted_entry_symbols, _execute_continuous_entries

    cfg = ContinuousDemoCycleConfig(submit_orders=True, record_dry_run=False)
    cand = [{"symbol": "WIFUSDT", "decile": 9, "composite": 0.95, "turnover_quote": 2e6,
             "signal_ts_ms": 1_700_000_000_000, "stop_loss_pct": 0.25, "live_price": 100.0}]
    contracts = {"WIFUSDT": {"tick_size": 0.0001, "qty_step": 0.001, "min_order_qty": 0.001}}

    class FakeClient:
        def set_leverage(self, **_kwargs: object) -> None:
            return None

        def place_order(self, **_kwargs: object) -> dict[str, str]:
            return {"orderId": "oid-accepted"}

    def fail_confirm(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("confirmation timeout")

    monkeypatch.setattr(cd, "_wait_for_execution_summary", fail_confirm)
    rows, orders = _execute_continuous_entries(
        cand, trading_client=FakeClient(), demo=cfg, equity_usdt=10_000.0, order_notional_frac=0.02,
        price_by_symbol={"WIFUSDT": 100.0}, contract_by_symbol=contracts, now_ms=1_700_000_000_000,
        strategy_id="continuous_fade_v2", record_preflight=None, execution_event_router=None)

    assert rows == []
    assert orders[0]["submit_mode"] == "submitted"
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert _continuous_accepted_entry_symbols(rows, orders) == {"WIFUSDT"}
    assert _continuous_accepted_entry_symbols([], [{"symbol": "BADUSDT", "submit_mode": "error"}]) == set()


def test_execute_exits_dry_run_closes_short() -> None:
    from liquidity_migration.continuous_demo import _execute_continuous_exits

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {"trade_id": "continuous_fade_v2-WIFUSDT-1700000000", "symbol": "WIFUSDT", "side": "short",
             "sleeve": "continuous",
             "status": "open", "entry_price": 100.0, "qty": "2", "equity_usdt": 10_000.0, "notional_usdt": 200.0}
    all_trades = pl.DataFrame([trade])
    plan = [{**trade, "exit_reason": "left_decile"}]
    rows, orders = _execute_continuous_exits(plan, all_trades, trading_client=None, demo=cfg,
                                             now_ms=1_700_100_000_000, record_preflight=None,
                                             price_by_symbol={"WIFUSDT": 92.0})
    assert len(rows) == 1 and orders[0]["side"] == "Buy" and orders[0]["reduce_only"] is True
    assert rows[0]["status"] == "closed" and rows[0]["exit_reason"] == "left_decile"
    assert rows[0]["sleeve"] == "continuous"             # exit row inherits the entry's sleeve tag (dict(trade))
    assert orders[0]["sleeve"] == "continuous" and orders[0]["order_link_id"].startswith("lm-ux-c-")
    assert orders[0]["updated_at_ms"] == 1_700_100_000_000
    # Regression (audit 2026-06-12 round 3): paper/dry-run closes must mark at the
    # LIVE price, not the entry price — the entry fallback booked every paper
    # round-trip at exactly 0% PnL and gutted paper<->demo reconciliation.
    assert rows[0]["exit_price"] == pytest.approx(92.0)
    # short 100 -> 92: gross = (100-92)/100 = +8%; net = gross * (200/10000)
    assert rows[0]["gross_trade_return"] == pytest.approx(0.08)
    assert rows[0]["net_return"] == pytest.approx(0.08 * 0.02)


def test_execute_exits_live_preflight_is_restart_loadable() -> None:
    from liquidity_migration.continuous_demo import _execute_continuous_exits

    cfg = ContinuousDemoCycleConfig(submit_orders=True, record_dry_run=False)
    trade = {"trade_id": "continuous_fade_v2-WIFUSDT-1700000000", "symbol": "WIFUSDT", "side": "short",
             "sleeve": "continuous",
             "status": "open", "entry_price": 100.0, "qty": "2", "equity_usdt": 10_000.0, "notional_usdt": 200.0}
    plan = [{**trade, "exit_reason": "left_decile", "exit_trigger_ts_ms": 1_700_099_999_000}]
    preflight_rows: list[dict[str, object]] = []

    class FailingClient:
        def place_order(self, **_kwargs: object) -> dict[str, str]:
            raise RuntimeError("venue down")

    _rows, orders = _execute_continuous_exits(
        plan, pl.DataFrame([trade]), trading_client=FailingClient(), demo=cfg,
        now_ms=1_700_100_000_000, record_preflight=preflight_rows.append,
        price_by_symbol={"WIFUSDT": 92.0})

    assert len(preflight_rows) == 1
    pre = preflight_rows[0]
    assert pre["updated_at_ms"] == 1_700_100_000_000
    assert pre["exit_reason"] == "left_decile"
    assert pre["exit_trigger_ts_ms"] == 1_700_099_999_000
    assert pre["target_qty"] == "2"
    assert pre["filled_qty"] == ""
    assert pre["order_type"] == cfg.exit_order_type
    assert orders[0]["target_qty"] == "2"


def test_execute_exits_dry_run_without_live_price_falls_back_to_entry() -> None:
    from liquidity_migration.continuous_demo import _execute_continuous_exits

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {"trade_id": "continuous_fade_v2-WIFUSDT-1700000000", "symbol": "WIFUSDT", "side": "short",
             "sleeve": "continuous",
             "status": "open", "entry_price": 100.0, "qty": "2", "equity_usdt": 10_000.0, "notional_usdt": 200.0}
    plan = [{**trade, "exit_reason": "left_decile"}]
    rows, _orders = _execute_continuous_exits(plan, pl.DataFrame([trade]), trading_client=None, demo=cfg,
                                              now_ms=1_700_100_000_000, record_preflight=None)
    assert rows[0]["status"] == "closed"
    assert rows[0]["exit_price"] == pytest.approx(100.0)


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
    assert preflight_rows[0]["updated_at_ms"] == 1_700_100_000_000
    assert rows[0]["qty"] == "3"
    assert rows[0]["entry_price"] == pytest.approx((100.0 + 2.0 * 120.0) / 3.0)
    assert rows[0]["last_rebalance_fee_usdt"] == pytest.approx(0.12)
    assert rows[0]["submit_mode"] == "submitted"
    assert orders[0]["status"] == "filled"
    assert orders[0]["order_id"] == "oid-1"
    assert orders[0]["fee_usdt"] == pytest.approx(0.12)
    assert orders[0]["updated_at_ms"] == 1_700_100_000_000


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
    assert orders[0]["updated_at_ms"] == 1_700_100_000_000


def test_execute_rebalance_resizes_duplicate_generated_link_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import liquidity_migration.continuous_demo as cd
    import liquidity_migration.continuous_identity as ci

    cfg = ContinuousDemoCycleConfig(submit_orders=True, confirm_demo_orders=True, daily_rebalance_enabled=True)
    trades = [
        {
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
        },
        {
            "trade_id": "t2",
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
        },
    ]
    plans = [
        ContinuousRebalanceResizePlan(
            trade_id="t1",
            symbol="ABCUSDT",
            side="Sell",
            reduce_only=False,
            qty=1.0,
            current_notional_usdt=100.0,
            target_notional_usdt=200.0,
            delta_notional_usdt=100.0,
            reason="rebalance_increase",
        ),
        ContinuousRebalanceResizePlan(
            trade_id="t2",
            symbol="ABCUSDT",
            side="Sell",
            reduce_only=False,
            qty=1.0,
            current_notional_usdt=100.0,
            target_notional_usdt=200.0,
            delta_notional_usdt=100.0,
            reason="rebalance_increase",
        ),
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def place_order(self, **kwargs: object) -> dict[str, str]:
            self.orders.append(kwargs)
            return {"orderId": f"oid-{len(self.orders)}"}

    def fake_wait(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"qty": "1", "avg_price": "100", "fee": "0.01", "exec_time_ms": 1}

    monkeypatch.setattr(ci.zlib, "crc32", lambda _data: 7)
    monkeypatch.setattr(cd, "_wait_for_execution_summary", fake_wait)
    client = FakeClient()

    _rows, orders = _execute_continuous_rebalance_resizes(
        plans,
        pl.DataFrame(trades),
        trading_client=client,
        demo=cfg,
        price_by_symbol={"ABCUSDT": 100.0},
        contract_by_symbol={"ABCUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_100_000_000,
        strategy_id=continuous_strategy_id(cfg),
    )

    assert len(client.orders) == 1
    assert len(orders) == 2
    assert orders[0]["status"] == "filled"
    assert orders[1]["status"] == "failed"
    assert orders[1]["submit_mode"] == "error"
    assert "duplicate generated order_link_id collision" in orders[1]["error"]


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


def test_continuous_rebalance_rule_matches_v2_knobs() -> None:
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", daily_rebalance_enabled=True, record_dry_run=True)
    )
    rule = continuous_rebalance_rule(cfg)

    assert rule.realized_vol_window_days == 90
    assert rule.target_daily_vol == pytest.approx(0.045)
    assert rule.max_scale == pytest.approx(4.0)
    assert rule.drawdown_half_threshold == pytest.approx(-0.04)
    assert rule.resize_cost_bps == pytest.approx(10.0)
    assert rule.strategy_momentum_window_days == 0
    assert rule.strategy_momentum_min_return == pytest.approx(0.0)
    assert rule.strategy_momentum_scale_when_below == pytest.approx(0.0)


def test_continuous_rebalance_profile_resolves_to_pinned_candidate_contract() -> None:
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend",
            paper_mode=True, record_dry_run=True,
        )
    )

    assert cfg.rmom_quantile == pytest.approx(0.25)
    assert cfg.feature_set == ("max_ret168",)
    assert cfg.liq_turnover_min == pytest.approx(500_000.0)
    assert cfg.max_hold_hours == 24
    assert cfg.entry_confirm_delay_hours == 1
    assert cfg.entry_event_trigger == "none"
    assert cfg.btc_trend_gate == "uptrend"  # pass-through from the CLI/env knob, not pinned by the profile
    assert cfg.daily_rebalance_enabled is False  # current target disables daily vol adjuster
    assert continuous_rebalance_rule(cfg).target_daily_vol == pytest.approx(0.045)  # params retained for the rework


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


def test_continuous_cycle_daily_rebalance_disabled_under_v2_override(
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
                        "entry_ts_ms": now - MS_PER_HOUR,
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

    # The v2 profile forces daily_rebalance_enabled=False.
    # (the daily volatility adjuster is disabled), so run_continuous_demo_cycle resolves it
    # off even though the raw cfg requested True -> no resize occurs and the open trade qty
    # is unchanged. The resize MECHANISM itself stays covered by the continuous_rebalance
    # unit tests (apply_rebalance_rule / compute_continuous_rebalance_scale with the rule
    # enabled); restore the resize assertions here when the adjuster is re-enabled at the
    # volatility rework.
    assert first.get("rebalance_resize_orders", 0) == 0
    assert resized["qty"] == "1"  # unchanged: no rebalance resize under the disabled adjuster

    # Second cycle (same day): still no resize — the adjuster remains disabled by the override.
    price["value"] = 50.0
    second = cd.run_continuous_demo_cycle(root, config=ResearchConfig(), demo_config=cfg, now_ms=now + MS_PER_HOUR)
    after_second = read_dataset(root, trades_ds)
    second_trade = after_second.filter(pl.col("trade_id") == "t1").to_dicts()[0]

    assert second.get("rebalance_resize_orders", 0) == 0
    assert second_trade["qty"] == "1"  # unchanged across both cycles (adjuster disabled)


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


def test_live_v2_ships_without_server_stop_demo_paper_only() -> None:
    """The registered 2026-06-18 redesign deliberately ships demo/paper v2 without a server stop."""
    from liquidity_migration.continuous_demo import _execute_continuous_entries

    cfg = apply_continuous_demo_profile(ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2"))
    assert cfg.stop_loss_pct == 0.0
    cand = [{"symbol": "WIFUSDT", "decile": 9, "composite": 0.9, "turnover_quote": 2e6,
             "signal_ts_ms": 1_700_000_000_000, "stop_loss_pct": cfg.stop_loss_pct, "live_price": 100.0}]
    rows, orders = _execute_continuous_entries(
        cand, trading_client=None, demo=cfg, equity_usdt=10_000.0,
        order_notional_frac=cfg.per_position_notional_pct_equity / 100.0,
        price_by_symbol={"WIFUSDT": 100.0},
        contract_by_symbol={"WIFUSDT": {"tick_size": 0.0001, "qty_step": 0.001, "min_order_qty": 0.001}},
        now_ms=1_700_000_000_000, strategy_id="continuous_fade_v2", record_preflight=None,
        execution_event_router=None)
    assert rows[0]["stop_price"] == 0.0
    assert rows[0]["stop_loss_pct"] == cfg.stop_loss_pct


def test_cycle_refreshes_btc_trend_gate_input_outside_tradable_universe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BTC regime gate is market context, not a tradable-universe member.

    If BTC is missing/stale in the main universe kline frame, the cycle must still
    load BTC separately for the gate and must not rewrite the main compact kline
    cache with a BTC-only symbol set.
    """
    import liquidity_migration.continuous_demo as cd
    from liquidity_migration.config import ResearchConfig

    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    signal_day = day0 + 31 * MS_PER_DAY
    now = signal_day + 3 * MS_PER_HOUR
    price = 100.0
    btc_rows = []
    for i in range(32):
        price *= 1.01
        btc_rows.append(
            {
                "ts_ms": day0 + i * MS_PER_DAY,
                "symbol": "BTCUSDT",
                "close": price,
                "turnover_quote": 1_000_000_000.0,
            }
        )
    btc_klines = pl.DataFrame(btc_rows)

    def fake_resolve_cycle_universe(**_kwargs: object):
        universe = pl.DataFrame(
            [
                {
                    "symbol": "AAAUSDT",
                    "listing_age_days": 365.0,
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
            [{"symbol": "AAAUSDT", "mark_price": 100.0, "last_price": 100.0}],
            infer_schema_length=None,
        )
        return universe, ["AAAUSDT"], tickers, "test"

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_klines(symbols: list[str], **kwargs: object):
        calls.append((list(symbols), dict(kwargs)))
        if symbols == ["BTCUSDT"]:
            return btc_klines, {
                "fetch_symbols": 1,
                "fetched_rows": btc_klines.height,
                "output_rows": btc_klines.height,
            }
        return pl.DataFrame(), {"fetch_symbols": 1, "output_rows": 0}

    monkeypatch.setattr(cd, "_resolve_cycle_universe", fake_resolve_cycle_universe)
    monkeypatch.setattr(cd, "_download_recent_1h_klines", fake_klines)

    payload = cd.run_continuous_demo_cycle(
        tmp_path / "continuous-btc-gate",
        config=ResearchConfig(),
        demo_config=ContinuousDemoCycleConfig(btc_trend_gate="uptrend"),
        now_ms=now,
    )

    assert calls[0][0] == ["AAAUSDT"]
    assert calls[1][0] == ["BTCUSDT"]
    assert calls[1][1]["write_compact_cache"] is False
    assert payload["btc_trend_gate_value"] > 0.0
    assert payload["btc_trend_gate_allows_entry"] is True
    assert payload["btc_trend_gate_btc_rows"] == btc_klines.height
    assert payload["btc_trend_gate_btc_max_ts_ms"] == signal_day


def test_btc_trend_gate_payload_fields_apply_cycle_defaults() -> None:
    fields = _btc_trend_gate_payload_fields(
        config=ContinuousDemoCycleConfig(btc_trend_gate="uptrend"),
        allows_entry=True,
        trend_value=0.12,
        kline_stats={"btc_rows": 42, "btc_max_ts_ms": 123, "fetch_symbols": 1},
    )

    assert fields == {
        "btc_trend_gate": "uptrend",
        "btc_trend_gate_mode": "daily_prior",
        "btc_trend_gate_lookback_days": 30,
        "btc_trend_gate_lookback_duration_ms": 30 * MS_PER_DAY,
        "btc_trend_gate_allows_entry": True,
        "btc_trend_gate_value": 0.12,
        "btc_trend_gate_btc_rows": 42,
        "btc_trend_gate_btc_max_ts_ms": 123,
        "btc_trend_gate_kline_store_rows": 0,
        "btc_trend_gate_kline_cache_rows": 0,
        "btc_trend_gate_kline_fetch_symbols": 1,
        "btc_trend_gate_kline_fetched_rows": 0,
        "btc_trend_gate_kline_error": 0,
    }


def test_btc_risk_sizing_payload_fields_apply_cycle_defaults() -> None:
    fields = _btc_risk_sizing_payload_fields(
        {
            "enabled": True,
            "arm_id": "btc-risk-v1",
            "candidate_rows": 3,
            "scored": 2,
            "tail_selected": 1,
            "mean_btc_risk_score": 0.4,
        }
    )

    assert fields == {
        "btc_risk_sizing_enabled": True,
        "btc_risk_sizing_arm_id": "btc-risk-v1",
        "btc_risk_sizing_candidate_rows": 3,
        "btc_risk_sizing_scored": 2,
        "btc_risk_sizing_duplicates": 0,
        "btc_risk_sizing_tail_selected": 1,
        "btc_risk_sizing_warmup": 0,
        "btc_risk_sizing_state_rows": 0,
        "btc_risk_sizing_min_stack_mult": 1.0,
        "btc_risk_sizing_max_stack_mult": 1.0,
        "btc_risk_sizing_mean_score": 0.4,
        "btc_risk_sizing_error": 0,
    }


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


def test_live_panel_cache_supports_v2_max_ret168() -> None:
    """v2 ranks on max_ret168; the cheap live cache must carry it too."""
    klines, rmom, start, n_bars = _dispersed_synth()
    cfg = apply_continuous_demo_profile(ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2"))
    assert cfg.feature_set == ("max_ret168",)
    cache = LivePanelCache(
        rmom_quantile=cfg.rmom_quantile,
        feature_set=cfg.feature_set,
        exclude_symbols=cfg.exclude_symbols,
    )
    T = start + (n_bars - 1) * MS_PER_HOUR
    hist = klines.filter(pl.col("ts_ms") < T)
    price = {r["symbol"]: r["close"] for r in klines.filter(pl.col("ts_ms") == T).to_dicts()}

    ref = build_live_continuous_state(hist, price, rmom, now_ts_ms=T + 1_800_000, config=cfg)
    cac = cache.state(hist, price, rmom, now_ts_ms=T + 1_800_000, config=cfg)

    assert not cac.is_empty()
    assert set(_decile_map(ref)) == set(_decile_map(cac))
    assert {s for s, d in _decile_map(ref).items() if d == 9} == \
           {s for s, d in _decile_map(cac).items() if d == 9}


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


def test_left_decile_exit_can_be_disabled_for_live_v2() -> None:
    cfg = ContinuousDemoCycleConfig(
        decile=9,
        left_decile_exit_enabled=False,
        breakeven_arm_pct=0.0,
        stop_approach_frac=0.0,
        max_hold_hours=0,
        failed_fade_hours=0,
    )
    now = 2_000_000_000_000
    state = _state([("OUT7", 7)])
    trades = [{"symbol": "OUT7", "entry_ts_ms": now - MS_PER_HOUR, "entry_price": 100.0}]
    assert plan_continuous_exits(trades, state, now_ms=now, config=cfg) == []


def test_reentry_cooldown_blocks_recently_exited_symbol() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame([
        {"trade_id": "t1", "strategy_id": "continuous_fade_v2", "symbol": "FRESH", "status": "closed",
         "exit_ts_ms": now - 10 * 60_000},                       # exited 10 min ago -> in cooldown
        {"trade_id": "t2", "strategy_id": "continuous_fade_v2", "symbol": "OLD", "status": "closed",
         "exit_ts_ms": now - 120 * 60_000},                      # exited 2h ago -> clear
        {"trade_id": "t3", "strategy_id": "other", "symbol": "OTHER", "status": "closed",
         "exit_ts_ms": now - 1 * 60_000},                        # different strategy -> ignored
    ])
    cooled = _recent_exit_cooldown_symbols(trades, now_ms=now, cooldown_minutes=30,
                                           strategy_id="continuous_fade_v2")
    assert cooled == {"FRESH"}
    # cooldown disabled
    assert _recent_exit_cooldown_symbols(trades, now_ms=now, cooldown_minutes=0,
                                         strategy_id="continuous_fade_v2") == set()


def test_reentry_cooldown_uses_exact_millisecond_boundary() -> None:
    now = 2_000_000_000_123
    cutoff = now - 30 * 60_000
    trades = pl.DataFrame([
        {"trade_id": "t1", "strategy_id": "continuous_fade_v2", "symbol": "AT_BOUNDARY", "status": "closed",
         "exit_ts_ms": cutoff},
        {"trade_id": "t2", "strategy_id": "continuous_fade_v2", "symbol": "ONE_MS_OLD", "status": "closed",
         "exit_ts_ms": cutoff - 1},
    ])

    assert _recent_exit_cooldown_symbols(
        trades,
        now_ms=now,
        cooldown_minutes=30,
        strategy_id="continuous_fade_v2",
    ) == {"AT_BOUNDARY"}


def test_recent_entry_cooldown_blocks_recent_addon_entry_symbol() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame([
        {"trade_id": "t1", "strategy_id": "continuous_fade_addon_v2", "symbol": "FRESH", "entry_ts_ms": now - 10 * 60_000},
        {"trade_id": "t2", "strategy_id": "continuous_fade_addon_v2", "symbol": "OLD", "entry_ts_ms": now - 120 * 60_000},
        {"trade_id": "t3", "strategy_id": "continuous_fade_v2", "symbol": "PRIMARY", "entry_ts_ms": now - 1 * 60_000},
        {"trade_id": "t4", "strategy_id": "continuous_fade_addon_v2", "symbol": "SIGNAL", "signal_ts_ms": now - 5 * 60_000},
    ])

    cooled = _recent_entry_cooldown_symbols(
        trades,
        now_ms=now,
        cooldown_minutes=30,
        strategy_id="continuous_fade_addon_v2",
    )

    assert cooled == {"FRESH", "SIGNAL"}
    assert _recent_entry_cooldown_symbols(
        trades,
        now_ms=now,
        cooldown_minutes=0,
        strategy_id="continuous_fade_addon_v2",
    ) == set()


def test_recent_entry_cooldown_uses_exact_millisecond_boundary() -> None:
    now = 2_000_000_000_123
    cutoff = now - 30 * 60_000
    trades = pl.DataFrame([
        {"trade_id": "t1", "strategy_id": "continuous_fade_addon_v2", "symbol": "AT_BOUNDARY",
         "entry_ts_ms": cutoff},
        {"trade_id": "t2", "strategy_id": "continuous_fade_addon_v2", "symbol": "ONE_MS_OLD",
         "entry_ts_ms": cutoff - 1},
    ])

    assert _recent_entry_cooldown_symbols(
        trades,
        now_ms=now,
        cooldown_minutes=30,
        strategy_id="continuous_fade_addon_v2",
    ) == {"AT_BOUNDARY"}


def test_entry_circuit_breaker_trips_on_adverse_cluster_and_self_clears() -> None:
    """Correlated-squeeze defense: pause entries once >= N adverse covers land within the window;
    disabled by default; self-clears as the cluster ages out of the window (stateless)."""
    now = 2_000_000_000_000
    strat = "continuous_fade_v2"

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
    strat = "continuous_fade_v2"
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


def test_protective_exit_timers_use_exact_millisecond_boundary() -> None:
    cfg = ContinuousDemoCycleConfig(
        failed_fade_hours=1.5,
        failed_fade_loss_pct=0.01,
        failed_fade_min_mfe_pct=0.01,
        max_hold_hours=2.5,
        stop_approach_frac=0.0,
        breakeven_arm_pct=0.0,
    )

    assert _protective_exit_reason(
        held_ms=90 * 60_000 - 1,
        mfe=0.0,
        cur_ret=-0.02,
        config=cfg,
    ) is None
    assert _protective_exit_reason(
        held_ms=90 * 60_000,
        mfe=0.0,
        cur_ret=-0.02,
        config=cfg,
    ) == "failed_fade"
    assert _protective_exit_reason(
        held_ms=150 * 60_000 - 1,
        mfe=0.02,
        cur_ret=0.0,
        config=cfg,
    ) is None
    assert _protective_exit_reason(
        held_ms=150 * 60_000,
        mfe=0.02,
        cur_ret=0.0,
        config=cfg,
    ) == "max_hold"


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

def test_protective_exit_cycle_covers_max_hold_dry_run(tmp_path) -> None:
    """The fast exit-only cycle reads the open continuous trade, prices it off the WS ticker cache,
    and covers a short that has reached the frozen v2 24h max hold — writing the closed row to the ledger,
    with no universe build / decile recompute / network."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import continuous_strategy_id
    from liquidity_migration.storage import read_dataset, write_dataset
    from liquidity_migration.ws_state_cache import TickerCache

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    root = tmp_path / "bybit-continuous-demo-event"
    trades_ds, _orders, _cycles = continuous_dataset_names(cfg)
    now = 1_700_000_000_000
    strat = continuous_strategy_id(cfg)
    write_dataset(pl.DataFrame([{
        "trade_id": f"{strat}-WIFUSDT-1699", "strategy_id": strat, "symbol": "WIFUSDT", "side": "short",
        "status": "open", "entry_price": 100.0, "qty": "2", "notional_usdt": 200.0, "equity_usdt": 10_000.0,
        "entry_ts_ms": now - 25 * MS_PER_HOUR, "updated_at_ms": now - 25 * MS_PER_HOUR,
    }], infer_schema_length=None), root, trades_ds, partition_by=())

    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "101.0"}])

    payload = run_continuous_protective_exit_cycle(
        root, config=ResearchConfig(), demo_config=cfg, trading_client=None,
        kline_store=None, ticker_cache=tc, private_state_cache=None, now_ms=now)
    assert payload["exits"] == 1
    assert "max_hold" in payload["reasons"]
    after = read_dataset(root, trades_ds)
    closed = after.filter(pl.col("status") == "closed")
    assert closed.height == 1
    assert closed["exit_reason"].to_list() == ["max_hold"]


def test_protective_exit_cycle_sends_telegram(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (audit 2026-06-12 round 3): the fast loop runs at ~2s vs the main
    cycle's 60s, so it wins the race for most protective exits — those covers (and
    their submit errors) were invisible to every notify path. The fast cycle must
    notify after its ledger writes; the price comes from the live ticker, not entry."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import continuous_strategy_id
    from liquidity_migration.storage import write_dataset
    from liquidity_migration.ws_state_cache import TickerCache

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True, telegram=True)
    root = tmp_path / "bybit-continuous-demo-event"
    trades_ds, _orders, _cycles = continuous_dataset_names(cfg)
    now = 1_700_000_000_000
    strat = continuous_strategy_id(cfg)
    write_dataset(pl.DataFrame([{
        "trade_id": f"{strat}-WIFUSDT-1699", "strategy_id": strat, "symbol": "WIFUSDT", "side": "short",
        "status": "open", "entry_price": 100.0, "qty": "2", "notional_usdt": 200.0, "equity_usdt": 10_000.0,
        "entry_ts_ms": now - 25 * MS_PER_HOUR, "updated_at_ms": now - 25 * MS_PER_HOUR,
    }], infer_schema_length=None), root, trades_ds, partition_by=())

    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "101.0"}])
    sent: list[str] = []
    monkeypatch.setattr(
        "liquidity_migration.telegram.send_telegram_message",
        lambda text, enabled=True: sent.append(text) or True,
    )

    payload = run_continuous_protective_exit_cycle(
        root, config=ResearchConfig(), demo_config=cfg, trading_client=None,
        kline_store=None, ticker_cache=tc, private_state_cache=None, now_ms=now)
    assert payload["exits"] == 1
    assert payload["telegram_sent"] is True
    assert len(sent) == 1
    assert "reason=continuous_exit_executed" in sent[0]
    assert "(fast protective-exit loop)" in sent[0]
    assert "max_hold" in sent[0]
    # paper marks at the live ticker price, not entry (round-3 exit-price fix)
    assert "@$101" in sent[0]


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
        "entry_ts_ms": now - 25 * MS_PER_HOUR, "updated_at_ms": now - 10_000,
        "exit_order_link_id": "lm-ux-c-WIF-zzz",   # cover already submitted, not yet confirmed closed
    }], infer_schema_length=None), root, trades_ds, partition_by=())
    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "130.0"}])   # would otherwise trigger stop_approach
    payload = run_continuous_protective_exit_cycle(
        root, config=ResearchConfig(), demo_config=cfg, ticker_cache=tc, now_ms=now)
    assert payload["exits"] == 0                  # in-flight guard suppressed the second cover
    after = read_dataset(root, trades_ds)
    assert after.filter(pl.col("status") == "closed").height == 0


def test_protective_exit_cycle_retries_stale_in_flight_exit(tmp_path) -> None:
    """A stale submitted_unconfirmed exit link must not suppress protective covers forever."""
    from liquidity_migration.config import ResearchConfig
    from liquidity_migration.continuous_demo import continuous_strategy_id
    from liquidity_migration.storage import read_dataset, write_dataset
    from liquidity_migration.ws_state_cache import TickerCache

    cfg = ContinuousDemoCycleConfig(
        submit_orders=False,
        record_dry_run=True,
        stop_loss_pct=0.25,
        stop_approach_frac=0.8,
    )
    root = tmp_path / "bybit-continuous-demo-event"
    trades_ds, _orders, _cycles = continuous_dataset_names(cfg)
    now = 1_700_000_000_000
    strat = continuous_strategy_id(cfg)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": f"{strat}-WIFUSDT-1699",
                    "strategy_id": strat,
                    "symbol": "WIFUSDT",
                    "side": "short",
                    "status": "open",
                    "entry_price": 100.0,
                    "qty": "2",
                    "notional_usdt": 200.0,
                    "equity_usdt": 10_000.0,
                    "entry_ts_ms": now - 25 * MS_PER_HOUR,
                    "updated_at_ms": now - 3 * MS_PER_HOUR,
                    "exit_order_link_id": "lm-ux-c-WIF-stale",
                }
            ],
            infer_schema_length=None,
        ),
        root,
        trades_ds,
        partition_by=(),
    )
    tc = TickerCache()
    tc.seed([{"symbol": "WIFUSDT", "lastPrice": "130.0"}])

    payload = run_continuous_protective_exit_cycle(
        root,
        config=ResearchConfig(),
        demo_config=cfg,
        ticker_cache=tc,
        now_ms=now,
    )

    assert payload["exits"] == 1
    after = read_dataset(root, trades_ds)
    assert after.filter(pl.col("status") == "closed").height == 1


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


def test_daemon_cycle_kwargs_include_private_ws_health(tmp_path) -> None:
    class _ConnectedPrivateWs:
        def is_connected(self) -> bool:
            return True

    d = _daemon(tmp_path)
    d._ws_stream = _ConnectedPrivateWs()  # type: ignore[assignment]
    d._private_state_ws_subscriptions_ok = True

    assert d._extra_cycle_kwargs()["private_ws_health_ok"] is True


def test_daemon_cycle_kwargs_mark_private_ws_unhealthy_when_subscriptions_missing(tmp_path) -> None:
    class _ConnectedPrivateWs:
        def is_connected(self) -> bool:
            return True

    d = _daemon(tmp_path)
    d._ws_stream = _ConnectedPrivateWs()  # type: ignore[assignment]
    d._private_state_ws_subscriptions_ok = False

    assert d._extra_cycle_kwargs()["private_ws_health_ok"] is False


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
    # a compatibility short fill (4-part link) must NOT nudge the continuous daemon
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


def test_validate_continuous_demo_config_retires_addon_primary_gate() -> None:
    with pytest.raises(ValueError, match="retired"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                addon_primary_pnl_gate=True,
                submit_orders=True,
                confirm_demo_orders=True,
            )
        )


def test_validate_continuous_demo_config_retires_addon_entry_cooldown() -> None:
    with pytest.raises(ValueError, match="retired"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(addon_same_symbol_entry_cooldown_minutes=15)
        )


def test_validate_continuous_demo_config_allows_valid_demo_runs() -> None:
    # non-submitting demo run: no guard trips
    _validate_continuous_demo_config(ContinuousDemoCycleConfig(submit_orders=False))
    # paper shadow done right
    _validate_continuous_demo_config(
        ContinuousDemoCycleConfig(paper_mode=True, submit_orders=False, record_dry_run=True)
    )


def test_continuous_live_config_golden_values() -> None:
    """Pin the resolved live-v2 values so a silent revert to retired exits is caught."""
    c = apply_continuous_demo_profile(ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2"))
    assert c.rmom_quantile == 0.25
    assert c.entry_pause_after_adverse_exits == 8
    assert c.entry_pause_window_minutes == 1440
    assert c.left_decile_exit_enabled is False
    assert c.stop_loss_pct == 0.0
    assert c.stop_approach_frac == 0.0
    assert c.failed_fade_hours == 0
    assert c.breakeven_arm_pct == 0.0
    assert c.sizing_mode == "inverse_vol"
    assert c.target_vol_per_name == 0.01
    assert c.vol_weight_clamp == 2.0
    assert c.daily_rebalance_enabled is False  # current target disables daily vol adjuster
    assert c.daily_rebalance_realized_vol_window_days == 90
    assert c.daily_rebalance_target_daily_vol == 0.045
    assert c.daily_rebalance_max_scale == 4.0
    assert c.daily_rebalance_strategy_momentum_window_days == 0
    assert c.daily_rebalance_strategy_momentum_min_return == 0.0
    assert c.daily_rebalance_strategy_momentum_scale_when_below == 0.0


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


def test_payload_float_keeps_continuous_formatter_fallback() -> None:
    assert _payload_float("12.5") == 12.5
    assert _payload_float(None) == 0.0
    assert _payload_float("not-a-number") == 0.0


def test_rmom_freshness_payload_fields_floor_future_refresh() -> None:
    current_day_ts = 10 * MS_PER_DAY
    rmom = pl.DataFrame({"day_ts": [current_day_ts + MS_PER_DAY]})

    fields = _rmom_freshness_payload_fields(rmom, current_day_ts=current_day_ts)

    assert fields == {
        "rmom_present": True,
        "max_rmom_day_ts": current_day_ts + MS_PER_DAY,
        "rmom_stale_days": 0,
    }


def test_rmom_freshness_payload_fields_handles_absent_or_unusable_table() -> None:
    assert _rmom_freshness_payload_fields(None, current_day_ts=10 * MS_PER_DAY) == {
        "rmom_present": False,
        "max_rmom_day_ts": 0,
        "rmom_stale_days": None,
    }
    assert _rmom_freshness_payload_fields(pl.DataFrame(), current_day_ts=10 * MS_PER_DAY) == {
        "rmom_present": True,
        "max_rmom_day_ts": 0,
        "rmom_stale_days": None,
    }


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
    strat = "continuous_fade_v2"
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


# ---------------------------------------------------------------------------
# Per-cycle telegram (added 2026-06-11 — the live sleeve previously sent NOTHING
# despite TELEGRAM_ENABLED=1 in its unit; only ws_risk's exit alerts fired)
# ---------------------------------------------------------------------------


def test_continuous_telegram_quiet_cycle_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import liquidity_migration.continuous_demo as cd

    sent: list[str] = []
    monkeypatch.setattr(
        "liquidity_migration.telegram.send_telegram_message",
        lambda text, enabled=True: sent.append(text) or True,
    )
    payload = {"entries": 0, "exits": 0, "mode": "submit", "equity_usdt": 10_000.0}
    ok, why = cd._maybe_continuous_notify(payload, [], [], enabled=True)
    assert not ok and why == "quiet_no_material_event"
    assert sent == []
    # disabled flag short-circuits before any work
    ok, why = cd._maybe_continuous_notify(payload, [], [], enabled=False)
    assert not ok and why == "disabled"


def test_continuous_telegram_entry_and_error_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    import liquidity_migration.continuous_demo as cd

    sent: list[str] = []
    monkeypatch.setattr(
        "liquidity_migration.telegram.send_telegram_message",
        lambda text, enabled=True: sent.append(text) or True,
    )
    entry = {"symbol": "AAAUSDT", "qty": 5.0, "entry_price": 1.25, "status": "filled"}
    payload = {"entries": 1, "exits": 0, "mode": "submit", "equity_usdt": 10_000.0,
               "open_positions": 1, "candidates": 3, "rmom_present": True, "entry_paused": False}
    ok, why = cd._maybe_continuous_notify(payload, [entry], [], enabled=True)
    assert ok and why == ""
    assert len(sent) == 1
    assert "[Continuous sleeve]" in sent[0]
    assert "AAAUSDT" in sent[0]
    assert "reason=continuous_entry_executed" in sent[0]
    # an errored entry row outranks the executed-count reason
    bad = dict(entry, submit_mode="error")
    assert cd._continuous_telegram_reason(payload, [bad], []) == "continuous_entry_error"


def test_continuous_telegram_sniper_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (audit 2026-06-12): the ARMED sniper's fills/exits/errors were invisible
    to the notify plane — a snipe fill is a NEW live short and an error a stuck resting
    order; both must page. Counts ride the payload; errors outrank fills."""
    import liquidity_migration.continuous_demo as cd

    base = {"entries": 0, "exits": 0, "mode": "submit", "equity_usdt": 10_000.0}
    assert cd._continuous_telegram_reason({**base, "sniper_fills": 1}, [], []) == "continuous_sniper_fill"
    assert cd._continuous_telegram_reason({**base, "sniper_exits": 2}, [], []) == "continuous_exit_executed"
    assert (
        cd._continuous_telegram_reason({**base, "sniper_fills": 1, "sniper_errors": 1}, [], [])
        == "continuous_sniper_error"
    )
    sent: list[str] = []
    monkeypatch.setattr(
        "liquidity_migration.telegram.send_telegram_message",
        lambda text, enabled=True: sent.append(text) or True,
    )
    ok, why = cd._maybe_continuous_notify({**base, "sniper_fills": 1, "sniper_exits": 1}, [], [], enabled=True)
    assert ok and why == ""
    assert "sniper: fills=1 exits=1" in sent[0]
    # the journald cycle line carries the sniper counts and the telegram OUTCOME
    line = cd.format_continuous_demo_cycle_summary(
        {**base, "sniper_fills": 1, "sniper_exits": 0, "sniper_errors": 0, "telegram_sent": True}
    )
    assert "sniper=1/0/0" in line and "telegram=sent" in line
    line = cd.format_continuous_demo_cycle_summary({**base, "telegram_error": "boom"})
    assert "telegram=boom" in line


def test_continuous_telegram_order_row_errors_reach_reason_via_payload() -> None:
    """REGRESSION (audit 2026-06-12 round 3): a failed live entry/exit submit appends
    NO trade row — errors live on ORDER rows only, surfaced through payload counts.
    Resize orders (real daily Market orders) must be visible too."""
    import liquidity_migration.continuous_demo as cd

    base = {"entries": 0, "exits": 0, "mode": "submit", "equity_usdt": 10_000.0}
    assert cd._continuous_telegram_reason({**base, "entry_errors": 1}, [], []) == "continuous_entry_error"
    assert cd._continuous_telegram_reason({**base, "exit_errors": 2}, [], []) == "continuous_exit_error"
    assert cd._continuous_telegram_reason({**base, "resize_errors": 1}, [], []) == "continuous_resize_error"
    assert (
        cd._continuous_telegram_reason({**base, "rebalance_resizes": 3}, [], [])
        == "continuous_rebalance_resize"
    )
    # errors outrank executions
    assert (
        cd._continuous_telegram_reason({**base, "entries": 1, "entry_errors": 1}, [], [])
        == "continuous_entry_error"
    )
    msg = cd.format_continuous_telegram_status_message(
        {**base, "entry_errors": 1, "exit_errors": 0, "resize_errors": 2, "rebalance_resizes": 2},
        [], [], reason="continuous_resize_error",
    )
    assert "submit errors: entries=1 exits=0 resizes=2" in msg
    assert "rebalance resizes: 2" in msg


def test_submission_error_count_preserves_continuous_notification_modes() -> None:
    rows = [
        {"submit_mode": "error", "status": "submitted"},
        {"submit_mode": "submitted", "status": "failed"},
        {"submit_mode": "submitted", "status": "partial", "error": "venue warning"},
        {"submit_mode": "submitted", "status": "filled"},
    ]

    assert _submission_error_count(rows) == 2
    assert _submission_error_count(rows, include_failed_status=False) == 1
    assert _submission_error_count(rows, include_error_text=True) == 3


def test_continuous_telegram_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A telegram/formatter fault must degrade to a reported error, never raise into
    the cycle (orders have already executed by the time the notify runs)."""
    import liquidity_migration.continuous_demo as cd

    def _boom(text: str, enabled: bool = True) -> bool:
        raise RuntimeError("telegram down")

    monkeypatch.setattr("liquidity_migration.telegram.send_telegram_message", _boom)
    payload = {"entries": 1, "exits": 0}
    ok, why = cd._maybe_continuous_notify(payload, [], [], enabled=True)
    assert not ok and "telegram down" in why


def test_component_and_sniper_links_decode_as_continuous() -> None:
    """REGRESSION (audit 2026-06-11): the ensemble profiles append the
    component tag to the link prefix ('en-c'+'p3' -> lm-en-cp3-...) and the sniper
    appends 's' (lm-en-cs-...). The decoder only knew bare 'c' — every LIVE entry's
    link decoded to None, so on a VPS rebuild/orphan ws_risk's side-based fallback
    adopted continuous positions into the compatibility root with the
    default adopt stops instead of the ensemble contract."""
    sig = 1_765_400_000_000
    expected_ts = (sig // 1000) * 1000
    # every component tag the deployed profile can emit (continuous_demo profile
    # components) + the sniper form, first-entry and re-entry
    for component in ("p3", "p4p3", "p4p5", "s"):
        link = _continuous_order_link_id(f"en-c{component}", symbol="WLDUSDT", signal_ts_ms=sig)
        assert decode_entry_order_link_id(link) == ("continuous", expected_ts, 0, component), link
        relink = _continuous_order_link_id(f"en-c{component}", symbol="WLDUSDT", signal_ts_ms=sig, reentry_seq=2)
        assert decode_entry_order_link_id(relink) == ("continuous", expected_ts, 2, component), relink
    # the addon (hedge) prefix must NOT be swallowed by the component family
    addon = _continuous_order_link_id("en-ca", symbol="BTCUSDT", signal_ts_ms=sig)
    assert decode_entry_order_link_id(addon) == ("continuous_addon", expected_ts, 0, "")
    # junk tags still fall back to None (adopted-*)
    assert decode_entry_order_link_id("lm-en-x9-BTC-abcd") is None


def test_tier4_link_matcher_catches_component_and_sniper_fills() -> None:
    """REGRESSION (audit 2026-06-12): the Tier-4 fill nudge matched only the literal
    'lm-en-c-' prefix — the deployed ensemble emits ONLY component-tagged entry links
    (lm-en-cp3-…) and sniper links (lm-en-cs-…), so entry/snipe fills (exactly the
    between-cycle sniper window the nudge exists for) fell through to the 60s heartbeat."""
    from liquidity_migration.continuous_demo import _continuous_suborder_link_id
    from liquidity_migration.continuous_demo_daemon import _continuous_links_in_message

    sig = 1_765_400_000_000

    def _msg(link: str) -> dict:
        return {"data": [{"orderLinkId": link}]}

    for prefix in ("en-cp3", "en-cp4p3", "en-cp4p5", "en-cs", "en-c", "en-ca"):
        link = _continuous_order_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=sig)
        assert _continuous_links_in_message(_msg(link)), link
    # exits (incl. hash-suffixed) and addon exits match by prefix
    exit_link = _continuous_suborder_link_id("ux-c", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="t-p3")
    assert _continuous_links_in_message(_msg(exit_link))
    # other sleeves' fills must NOT nudge the continuous daemon
    assert not _continuous_links_in_message(_msg(_continuous_order_link_id("en-l", symbol="WIFUSDT", signal_ts_ms=sig)))
    assert not _continuous_links_in_message(_msg("lm-en-BTC-abcd"))  # compatibility short
    assert not _continuous_links_in_message(_msg("manual-order-1"))


def test_cycle_failure_telegram_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (audit 2026-06-12): a wedged 60s-cycle daemon paged on EVERY failure
    (~1440/day — guaranteed Telegram 429s drowning real alerts). Same signature is
    deduped within the cooldown; a NEW failure signature pages immediately."""
    from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon

    sent: list[str] = []
    daemon = LongNativeDemoDaemon.__new__(LongNativeDemoDaemon)  # behavior-only: skip heavy __init__
    daemon._cycle_failure_telegram_sent = {}
    daemon._sleeve_label = "continuous"
    # _send_telegram returns True only on CONFIRMED delivery (round 3): the
    # cooldown must advance only then.
    daemon._send_telegram = lambda text: sent.append(text) or True  # type: ignore[method-assign]

    daemon._maybe_send_cycle_failure_telegram(RuntimeError("schema mismatch"))
    daemon._maybe_send_cycle_failure_telegram(RuntimeError("schema mismatch"))
    assert len(sent) == 1  # same wedge within cooldown: one page
    daemon._maybe_send_cycle_failure_telegram(ValueError("different failure"))
    assert len(sent) == 2  # a NEW signature pages immediately
    # after the cooldown elapses the same wedge re-pages
    daemon._cycle_failure_telegram_sent = {
        k: v - daemon._CYCLE_FAILURE_TELEGRAM_COOLDOWN_SECONDS - 1
        for k, v in daemon._cycle_failure_telegram_sent.items()
    }
    daemon._maybe_send_cycle_failure_telegram(RuntimeError("schema mismatch"))
    assert len(sent) == 3

    # REGRESSION (round 3): a FAILED delivery must NOT advance the cooldown —
    # recording-before-send suppressed the page for the whole window on a
    # transient outage (and forever on a missing token). The next failure retries.
    attempts: list[str] = []
    daemon._cycle_failure_telegram_sent = {}
    daemon._send_telegram = lambda text: attempts.append(text) or False  # type: ignore[method-assign]
    daemon._maybe_send_cycle_failure_telegram(RuntimeError("schema mismatch"))
    daemon._maybe_send_cycle_failure_telegram(RuntimeError("schema mismatch"))
    assert len(attempts) == 2  # retried — cooldown never advanced
    assert daemon._cycle_failure_telegram_sent == {}


def test_suborder_link_uniquifies_and_still_decodes() -> None:
    """REGRESSION (audit 2026-06-12): two same-symbol orders in the same second (sniper
    base+snipe exits, multi-component resizes/snipes) shared one orderLinkId — Bybit
    accepts a reused link once the first order is terminal, and the WS execution router
    buffers per link, so the second order inherited the first's fills. The sub-order
    hash makes links distinct per trade_id, deterministic per retry, and the decoder
    strips the suffix so entry-prefix (resize/sniper) links still decode."""
    from liquidity_migration.continuous_demo import _continuous_suborder_link_id

    sig = 1_765_400_000_000
    expected_ts = (sig // 1000) * 1000
    base = _continuous_suborder_link_id("ux-c", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="STRAT-WIFUSDT-1-p3")
    snipe = _continuous_suborder_link_id("ux-c", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="STRAT-WIFUSDT-1-p3-snipe")
    other = _continuous_suborder_link_id("ux-c", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="STRAT-WIFUSDT-1-p4p5")
    assert len({base, snipe, other}) == 3  # same symbol+second, three distinct links
    assert all(link.startswith("lm-ux-c-") and len(link) <= 36 for link in (base, snipe, other))
    # deterministic: a crash-retry of the same logical order reuses the same link
    assert base == _continuous_suborder_link_id("ux-c", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="STRAT-WIFUSDT-1-p3")
    # entry-prefix sub-orders (resize increases, sniper placements) still DECODE: the
    # -x suffix is stripped and the component tag survives
    resize = _continuous_suborder_link_id("en-cp3", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="STRAT-WIFUSDT-1-p3")
    assert decode_entry_order_link_id(resize) == ("continuous", expected_ts, 0, "p3")
    snipe_place = _continuous_suborder_link_id("en-cs", symbol="WIFUSDT", signal_ts_ms=sig, trade_id="STRAT-WIFUSDT-1-p3-snipe")
    assert decode_entry_order_link_id(snipe_place) == ("continuous", expected_ts, 0, "s")


def test_cycle_counts_entries_notifies_and_sends_outside_lock(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 4 triple pin. (1) payload["entries"] counted TRADE rows with the
    ORDER-row status vocabulary ("filled"/"partial"/"planned") that no trade
    row ever carries — it was permanently 0, so (2) the
    continuous_entry_executed telegram could never fire for a successful live
    entry. (3) The send must run OUTSIDE the cycle file lock (lock-held-I/O
    contract): the lock file must be released by the time the transport runs."""
    import liquidity_migration.continuous_demo as cd
    import liquidity_migration.telegram as tg
    from liquidity_migration.config import ResearchConfig

    cfg = ContinuousDemoCycleConfig(
        submit_orders=False,
        record_dry_run=True,
        telegram=True,
    )
    root = tmp_path / "continuous-entry-notify"
    now = 1_700_000_000_000

    def fake_resolve_cycle_universe(**_kwargs: object):
        universe = pl.DataFrame(
            [{"symbol": "ABCUSDT", "tick_size": 0.0001, "qty_step": 0.1,
              "min_order_qty": 0.1, "min_notional_value": 1.0,
              "max_market_order_qty": 10_000.0}],
            infer_schema_length=None,
        )
        tickers = pl.DataFrame(
            [{"symbol": "ABCUSDT", "mark_price": 100.0, "last_price": 100.0}],
            infer_schema_length=None,
        )
        return universe, ["ABCUSDT"], tickers, "test"

    def fake_klines(*_args: object, **_kwargs: object):
        return pl.DataFrame(), {"store_rows": 0}

    entry_trade_row = {
        "trade_id": "cf-ABCUSDT-1700000000000-p3", "strategy_id": "continuous_fade_v2",
        "symbol": "ABCUSDT", "side": "short", "sleeve": "continuous", "status": "open",
        "ts_ms": now, "entry_ts_ms": now, "opened_at_ms": now, "updated_at_ms": now,
        "signal_ts_ms": now - 3_600_000, "entry_price": 100.0, "qty": "1",
        "notional_usdt": 100.0, "equity_usdt": 10_000.0,
        "component": "p3", "component_weight": 0.30, "submit_mode": "dry_run",
    }
    entry_order_row = {
        "order_link_id": "lm-en-cp3-ABC-1", "ts_ms": now,
        "trade_id": entry_trade_row["trade_id"], "strategy_id": "continuous_fade_v2",
        "symbol": "ABCUSDT", "side": "Sell", "qty": "1", "reduce_only": False,
        "submit_mode": "dry_run", "status": "planned", "trade_side": "short",
        "sleeve": "continuous", "signal_ts_ms": now - 3_600_000,
        "equity_usdt": 10_000.0, "component": "p3", "component_weight": 0.30,
    }

    def fake_execute_entries(*_args: object, **_kwargs: object):
        return [dict(entry_trade_row)], [dict(entry_order_row)]

    sends: list[tuple[str, bool]] = []

    def fake_send(text: str, **_kwargs: object) -> bool:
        lock_held = (root / ".locks" / "continuous_demo_cycle.lock").exists()
        sends.append((text, lock_held))
        return True

    monkeypatch.setattr(cd, "_resolve_cycle_universe", fake_resolve_cycle_universe)
    monkeypatch.setattr(cd, "_download_recent_1h_klines", fake_klines)
    monkeypatch.setattr(cd, "_execute_continuous_entries", fake_execute_entries)
    monkeypatch.setattr(tg, "send_telegram_message", fake_send)

    payload = cd.run_continuous_demo_cycle(
        root, config=ResearchConfig(), demo_config=cfg, now_ms=now
    )

    # (1) the entry is counted.
    assert payload["entries"] == 1
    # (2) the material-event filter fires for it and the send goes out.
    assert payload["telegram_sent"] is True
    assert sends, "telegram send was never attempted"
    # (3) the cycle lock was already released when the transport ran.
    assert sends[0][1] is False, "telegram send ran while the cycle file lock was held"


# =========================================================================== #
# Relocated from tests/test_audit_fix_b00.py (audit bucket b00: sniper /        #
# exec-router / pit-engine / sizing-rebalance / cross-sleeve). Each test pins a  #
# specific finding's root-cause fix (would FAIL pre-fix, PASS now).             #
# =========================================================================== #
class _FakeClient:
    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []
        self.open_orders: list[dict] = []
        self.history: list[dict] = []

    def place_order(self, **params):
        self.placed.append(params)
        return {"orderId": f"oid-{len(self.placed)}"}

    def cancel_order(self, *, symbol: str, order_link_id: str):
        self.cancelled.append((symbol, order_link_id))
        return {}

    def get_open_orders(self, *, symbol: str | None = None, **_kw):
        return [o for o in self.open_orders if symbol is None or o.get("symbol") == symbol]

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None, **_kw):
        return [
            h for h in self.history
            if (symbol is None or h.get("symbol") == symbol)
            and (order_link_id is None or h.get("orderLinkId") == order_link_id)
        ]


def _sniper_cfg(**kw) -> ContinuousDemoCycleConfig:
    base = dict(
        sniper_enabled=True, sniper_wick_pct=0.08, sniper_size_frac=0.25,
        stop_loss_pct=0.25, submit_orders=True, confirm_demo_orders=True,
    )
    base.update(kw)
    return ContinuousDemoCycleConfig(**base)


def _entry(symbol="AAAUSDT", trade_id="t1", price=100.0, qty=10.0, signal_ts_ms=1_700_000_000_000):
    return {
        "symbol": symbol, "trade_id": trade_id, "entry_price": price, "qty": qty,
        "notional_usdt": price * qty, "signal_ts_ms": signal_ts_ms,
        "tick_size": 0.01, "qty_step": 0.1,
    }


# --------------------------------------------------------------------------- #
# sniper-1 — venue contract-filter-aware snipe sizing                           #
# --------------------------------------------------------------------------- #
def test_sniper1_skips_snipe_below_min_order_qty_and_min_notional() -> None:
    """A quarter-size snipe on a low-priced name that floors below Bybit's
    minOrderQty / minNotional must NOT be submitted into a guaranteed venue
    rejection — it is skipped. (The old _sniper_round_qty floored to qty_step
    and only rejected below ONE step, never consulting min_order_qty.)"""
    client = _FakeClient()
    contracts = {"BBBUSDT": {
        "tick_size": 0.001, "qty_step": 0.001,
        "min_order_qty": 1.0, "min_notional_value": 5.0, "max_order_qty": 1_000.0,
    }}
    orders = _execute_sniper_placements(
        [_entry("BBBUSDT", "t2", price=1.0, qty=1.0)],
        trading_client=client, demo=_sniper_cfg(), now_ms=1, strategy_id="s",
        price_by_symbol={"BBBUSDT": 1.0}, contract_by_symbol=contracts,
    )
    assert orders == []
    assert client.placed == []  # nothing sent to the venue


def test_sniper1_caps_snipe_at_max_order_qty() -> None:
    """An oversized snipe (cheap-priced name, large base) is capped at
    max_order_qty instead of being sent uncapped and rejected."""
    client = _FakeClient()
    contracts = {"CCCUSDT": {
        "tick_size": 0.001, "qty_step": 1.0,
        "min_order_qty": 1.0, "min_notional_value": 0.0, "max_order_qty": 100.0,
    }}
    orders = _execute_sniper_placements(
        [_entry("CCCUSDT", "t3", price=1.0, qty=10_000.0)],
        trading_client=client, demo=_sniper_cfg(), now_ms=1, strategy_id="s",
        price_by_symbol={"CCCUSDT": 1.0}, contract_by_symbol=contracts,
    )
    assert len(orders) == 1
    assert float(client.placed[0]["qty"]) == 100.0  # capped, not 0.25 * 10000


def test_sniper1_preserves_quarter_size_target_without_contract_filters() -> None:
    """With no contract map (dry-run/tests) the snipe still sizes to a quarter of
    the base qty (the research form), modulo step."""
    orders = _execute_sniper_placements(
        [_entry()], trading_client=_FakeClient(), demo=_sniper_cfg(),
        now_ms=1_700_000_100_000, strategy_id="s", price_by_symbol={"AAAUSDT": 100.0},
    )
    assert len(orders) == 1
    assert float(orders[0]["qty"]) == 2.5  # 0.25 * 10


# --------------------------------------------------------------------------- #
# sniper-3 — updated_at_ms recency on every sniper order row                    #
# --------------------------------------------------------------------------- #
def test_sniper3_placement_and_updates_stamp_updated_at_ms() -> None:
    """Every sniper order row (placement + each reconcile update) carries
    updated_at_ms so the orders ledger dedups by true recency, not by an implicit
    ts_ms-bucket-monotonic coincidence."""
    client = _FakeClient()
    placement = _execute_sniper_placements(
        [_entry()], trading_client=client, demo=_sniper_cfg(), now_ms=1_700_000_100_000,
        strategy_id="s", price_by_symbol={"AAAUSDT": 100.0},
    )
    assert placement[0]["updated_at_ms"] == 1_700_000_100_000

    # a reconcile cancel update must also stamp updated_at_ms
    client.open_orders = [{"orderLinkId": placement[0]["order_link_id"], "symbol": "AAAUSDT"}]
    trades = pl.DataFrame(
        [{"trade_id": "t1", "status": "closed", "symbol": "AAAUSDT", "equity_usdt": 10_000.0}],
        infer_schema_length=None,
    )
    _fills, updates, _exits = reconcile_continuous_snipes(
        trades, pl.DataFrame(placement, infer_schema_length=None),
        trading_client=client, demo=_sniper_cfg(), now_ms=1_700_000_200_000,
    )
    assert updates and all(u["updated_at_ms"] == 1_700_000_200_000 for u in updates)


# --------------------------------------------------------------------------- #
# sniper-4 — ambiguous trade_id recovery is logged, not silent                  #
# --------------------------------------------------------------------------- #
def _find_recovery_collision() -> tuple[int, str] | None:
    """A (signal_ts, suffix) for which >=2 enumerated candidates collide on
    crc32%46656 — the silent ambiguous-recovery trigger."""
    comps = _known_ensemble_component_tags()
    for ts in range(1, 6000):
        seen: dict[str, int] = {}
        for seq in range(4):
            base = _continuous_trade_id("S", "WIFUSDT", ts * 1000, seq)
            for comp in (*comps, ""):
                cand = f"{base}-{comp}-snipe" if comp else f"{base}-snipe"
                suf = _base36(zlib.crc32(cand.encode("utf-8")) % 46656).rjust(3, "0")
                seen[suf] = seen.get(suf, 0) + 1
        for suf, n in seen.items():
            if n >= 2:
                return ts * 1000, suf
    return None


def test_sniper4_ambiguous_recovery_logs_warning(caplog) -> None:
    """An ambiguous recovery (>=2 candidates collide) returns None AND emits an
    operator WARNING — the old code returned None silently, so a post-rebuild
    paper<->demo mis-pairing was invisible."""
    hit = _find_recovery_collision()
    assert hit is not None, "expected at least one crc32%46656 collision in the search range"
    signal_ts, suffix = hit
    link = f"lm-en-cs-WIF-zzz-x{suffix}"
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.continuous_demo"):
        result = recover_snipe_trade_id_from_link(
            link, strategy_id="S", symbol="WIFUSDT", signal_ts_ms=signal_ts,
        )
    assert result is None
    assert any("AMBIGUOUS" in rec.message for rec in caplog.records)


def test_sniper4_empty_match_is_silent(caplog) -> None:
    """A link that matches NO candidate (routine 'not my sleeve/symbol') returns
    None WITHOUT a warning — only the >=2 collision is surfaced, so the operator
    log is not spammed by every foreign link."""
    # 'x000' is verified to match no enumerated candidate for this (strategy, symbol,
    # signal_ts), so found is empty (not an ambiguous >=2 collision).
    link = "lm-en-cs-WIF-zzz-x000"
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.continuous_demo"):
        result = recover_snipe_trade_id_from_link(
            link, strategy_id="S", symbol="WIFUSDT", signal_ts_ms=999_000_000_000,
        )
    assert result is None
    assert not any("AMBIGUOUS" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# sniper-5 — crash-orphaned preflight snipe is surfaced + cancellable           #
# --------------------------------------------------------------------------- #
def _preflight_orphan_row(link="lm-en-cs-WIF-abc-x123", base="t1", symbol="AAAUSDT"):
    """The ONLY ledger trace after a crash between place_order and the placement
    flush: the preflight intent row."""
    return {
        "order_link_id": link, "ts_ms": 100, "updated_at_ms": 100, "trade_id": f"{base}-snipe",
        "strategy_id": "s", "symbol": symbol, "side": "Sell", "submit_mode": "preflight",
        "status": "submitted", "trade_side": "short", "sleeve": "continuous",
        "signal_ts_ms": 1, "stop_price": 135.0, "reason": SNIPER_REASON, "base_trade_id": base,
    }


def test_sniper5_orphan_preflight_is_surfaced_as_resting() -> None:
    """A surviving preflight intent (no placement/terminal row) is surfaced by
    _sniper_order_state re-tagged submit_mode='submitted' so reconcile routes it
    through the venue check. The old view excluded preflight rows entirely."""
    state = _sniper_order_state(pl.DataFrame([_preflight_orphan_row()], infer_schema_length=None))
    assert len(state) == 1
    assert state[0]["submit_mode"] == "submitted"
    assert state[0]["base_trade_id"] == "t1"


def test_sniper5_placement_row_supersedes_preflight() -> None:
    """In the NORMAL (non-crash) case the end-of-cycle placement row supersedes the
    preflight intent — the orphan path must NOT fire."""
    placement = {**_preflight_orphan_row(), "ts_ms": 101, "updated_at_ms": 101,
                 "submit_mode": "submitted", "status": "resting"}
    orders = pl.concat(
        [pl.DataFrame([_preflight_orphan_row()], infer_schema_length=None),
         pl.DataFrame([placement], infer_schema_length=None)],
        how="diagonal_relaxed",
    )
    state = _sniper_order_state(orders)
    assert len(state) == 1
    assert state[0]["status"] == "resting" and state[0]["submit_mode"] == "submitted"


def test_sniper5_orphan_preflight_is_cancelled_when_base_closed() -> None:
    """End-to-end: a crash-orphaned snipe still resting on the venue is CANCELLED
    when its base thesis is gone — the unmanaged-order leak is closed."""
    client = _FakeClient()
    link = "lm-en-cs-WIF-abc-x123"
    client.open_orders = [{"orderLinkId": link, "symbol": "AAAUSDT"}]  # still resting on venue
    trades = pl.DataFrame(
        [{"trade_id": "t1", "status": "closed", "symbol": "AAAUSDT", "equity_usdt": 10_000.0}],
        infer_schema_length=None,
    )
    _fills, _updates, _exits = reconcile_continuous_snipes(
        trades, pl.DataFrame([_preflight_orphan_row(link=link)], infer_schema_length=None),
        trading_client=client, demo=_sniper_cfg(), now_ms=200,
    )
    assert client.cancelled == [("AAAUSDT", link)]


# --------------------------------------------------------------------------- #
# sniper-6 — partial-cancel fill time is the execution time, not cancel time    #
# --------------------------------------------------------------------------- #
def test_sniper6_prefers_exec_time_over_updated_time() -> None:
    """For a PartiallyFilledCanceled order updatedTime is the (later) CANCEL time;
    the fill time must come from execTime when present so held_ms / max_hold are
    not understated."""
    row = {"execTime": "1700000200000", "updatedTime": "1700000900000", "orderStatus": "PartiallyFilledCanceled"}
    assert _sniper_fill_ts_ms(row, now_ms=1_700_000_999_000) == 1_700_000_200_000


def test_sniper6_falls_back_to_updated_time_then_now() -> None:
    assert _sniper_fill_ts_ms({"updatedTime": "1700000200000"}, now_ms=999) == 1_700_000_200_000
    assert _sniper_fill_ts_ms({"execTime": "0", "updatedTime": ""}, now_ms=4242) == 4242


# --------------------------------------------------------------------------- #
# sniper-7 — snipe fill inherits a real equity when base equity is missing       #
# --------------------------------------------------------------------------- #
def _resting_row(link="lnk1", symbol="AAAUSDT", base="t1"):
    return {
        "order_link_id": link, "ts_ms": 1, "updated_at_ms": 1, "trade_id": f"{base}-snipe",
        "strategy_id": "s", "symbol": symbol, "side": "Sell", "order_type": "Limit", "qty": "2.5",
        "reduce_only": False, "order_id": "oid-1", "submit_mode": "submitted", "status": "resting",
        "signal_ts_ms": 1, "tick_size": 0.01, "qty_step": 0.1, "stop_price": 135.0,
        "stop_loss_pct": 0.25, "sleeve": "continuous", "reason": SNIPER_REASON,
        "limit_price": 108.0, "base_trade_id": base,
    }


def test_sniper7_fill_uses_account_equity_when_base_equity_missing() -> None:
    """When the base row carries no positive equity_usdt (adopted/recovered base
    whose equity stamp was lost) the snipe fill must inherit the live account
    equity snapshot, not 0.0 — a 0.0 stamp zeroes the snipe's notional weight and
    therefore its booked net_return despite real gross PnL."""
    client = _FakeClient()
    client.open_orders = []
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "2.5",
                       "avgPrice": "108.0", "updatedTime": "1700000200000", "orderStatus": "Filled"}]
    # base row exists but equity_usdt is 0 (lost stamp) -> equity_by_trade_id is empty
    trades = pl.DataFrame(
        [{"trade_id": "t1", "status": "open", "symbol": "AAAUSDT", "equity_usdt": 0.0}],
        infer_schema_length=None,
    )
    fills, _updates, _exits = reconcile_continuous_snipes(
        trades, pl.DataFrame([_resting_row()], infer_schema_length=None),
        trading_client=client, demo=_sniper_cfg(), now_ms=1_700_000_300_000,
        account_equity_usdt=12_345.0,
    )
    assert len(fills) == 1
    assert fills[0]["equity_usdt"] == 12_345.0  # NOT 0.0


# --------------------------------------------------------------------------- #
# sniper-8 — a snipe with no signal_ts is skipped (link must round-trip)         #
# --------------------------------------------------------------------------- #
def test_sniper8_skips_entry_with_zero_signal_ts() -> None:
    """An entry with signal_ts_ms=0 produces NO snipe plan — the link could not
    round-trip to the base trade_id, so a recoverable snipe is impossible. The old
    code defaulted the link ts to now_ms (a DIFFERENT value), breaking recovery."""
    cfg = _sniper_cfg()
    plans = plan_continuous_sniper_orders(
        [_entry(signal_ts_ms=0)], config=cfg, price_by_symbol={"AAAUSDT": 100.0},
    )
    assert plans == []


def test_sniper8_valid_signal_ts_is_carried_through() -> None:
    cfg = _sniper_cfg()
    plans = plan_continuous_sniper_orders(
        [_entry(signal_ts_ms=123_000)], config=cfg, price_by_symbol={"AAAUSDT": 100.0},
    )
    assert len(plans) == 1 and plans[0]["signal_ts_ms"] == 123_000


# --------------------------------------------------------------------------- #
# exec-router-6 — the crc32%46656 collision is a real, bounded residual hazard   #
# --------------------------------------------------------------------------- #
def test_execrouter6_distinct_trade_ids_can_collide_on_3char_suffix() -> None:
    """Documents the residual: the suborder-link uniqueness suffix is only
    crc32(trade_id)%46656 (46656 distinct values), so two DISTINCT same-symbol
    same-second trade_ids CAN share an orderLinkId. The full fix (a wider suffix)
    needs the cross-file orderLinkId decoder change (needs_integration); this test
    pins the hazard so a future widening is verifiably collision-free here."""
    prefix = _continuous_sniper_link_prefix(_sniper_cfg())
    sig = 1_700_000_000_000
    # Search a small space of distinct trade_ids for a colliding pair on one symbol/second.
    suffix_to_id: dict[str, str] = {}
    collision = None
    for i in range(200_000):
        tid = f"S-WIFUSDT-{sig}-{i}-snipe"
        link = _continuous_suborder_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=sig, trade_id=tid)
        suf = link[-3:]
        prior = suffix_to_id.get(suf)
        if prior is not None and prior != tid:
            collision = (prior, tid, link)
            break
        suffix_to_id[suf] = tid
    assert collision is not None, "expected a 3-char suffix collision within 46656 buckets"
    prior, tid, link = collision
    other = _continuous_suborder_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=sig, trade_id=prior)
    assert other == link  # two distinct trade_ids -> identical orderLinkId (the hazard)


# --------------------------------------------------------------------------- #
# pit-engine-3 — fingerprint catches a value-preserving multi-symbol backfill    #
# --------------------------------------------------------------------------- #
def test_pitengine3_signature_changes_on_sum_preserving_backfill() -> None:
    """A multi-symbol backfill that conserves BOTH close_sum and turnover_sum
    (one symbol +X, another -X) used to leave the old (count, max_ts, close_sum,
    turnover_sum) fingerprint unchanged -> stale carry served silently. The new
    content-hash fingerprint MUST change."""
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache()
    cur_ts = 1_700_003_600_000
    confirmed = pl.DataFrame({
        "ts_ms": [cur_ts - MS_PER_HOUR, cur_ts - MS_PER_HOUR],
        "symbol": ["AAA", "BBB"],
        "close": [100.0, 200.0],
        "turnover_quote": [1_000.0, 2_000.0],
    })
    mutated = confirmed.with_columns(
        pl.when(pl.col("symbol") == "AAA").then(pl.col("close") + 10.0)
        .when(pl.col("symbol") == "BBB").then(pl.col("close") - 10.0)
        .otherwise(pl.col("close")).alias("close")
    )
    assert confirmed["close"].sum() == mutated["close"].sum()           # sum preserved
    assert confirmed["turnover_quote"].sum() == mutated["turnover_quote"].sum()

    sig_before = cache._confirmed_signature(confirmed, cur_ts, cfg)
    sig_after = cache._confirmed_signature(mutated, cur_ts, cfg)
    assert sig_before != sig_after  # the old sum-based signature would be EQUAL here


def test_pitengine3_signature_stable_on_identical_bars() -> None:
    """Identical confirmed bars must yield an identical signature (no spurious
    refresh / no cache thrash)."""
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache()
    cur_ts = 1_700_003_600_000
    df = pl.DataFrame({
        "ts_ms": [cur_ts - MS_PER_HOUR, cur_ts - MS_PER_HOUR],
        "symbol": ["AAA", "BBB"], "close": [100.0, 200.0], "turnover_quote": [1_000.0, 2_000.0],
    })
    assert cache._confirmed_signature(df, cur_ts, cfg) == cache._confirmed_signature(df.clone(), cur_ts, cfg)


# --------------------------------------------------------------------------- #
# sizing-rebalance-3 — resize-day raw return weights by the held (pre-resize) qty #
# --------------------------------------------------------------------------- #
def test_sizingrebalance3_marks_weight_by_qty_held_over_interval() -> None:
    """The prev->cur move must be weighted by the qty held OVER that interval. A
    rebalance-INCREASE day's added qty was opened at ~today's price and earned no
    prev->cur move, so weighting by the post-resize qty (qty_new) double-counts.
    The live cycle now marks on the PRE-resize ledger (qty_old); this pins the
    weighting at the function level: passing qty_new biases the raw_return."""
    cfg = ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True)
    sid = continuous_strategy_id(cfg)
    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    day1 = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [{
            "ts_ms": day0 + 1, "rebalance_day_ts": day0, "rebalance_raw_return": 0.0,
            "rebalance_scaled_equity": 1.0, "rebalance_scaled_peak": 1.0,
            "rebalance_mark_prices_json": _continuous_rebalance_mark_prices_json({"t1": 100.0}),
        }],
        infer_schema_length=None,
    )

    def raw_return_for_qty(qty: float) -> float:
        trades = pl.DataFrame(
            [{
                "trade_id": "t1", "strategy_id": sid, "symbol": "ABCUSDT", "status": "open",
                "entry_ts_ms": day0 - MS_PER_DAY, "entry_price": 80.0,
                "qty": str(qty), "equity_usdt": 10_000.0,
            }],
            infer_schema_length=None,
        )
        fields = _continuous_rebalance_cycle_fields(
            trades, cycles, price_by_symbol={"ABCUSDT": 110.0},
            current_day_ts=day1, now_ms=day1 + 1, strategy_id=sid, rule=continuous_rebalance_rule(cfg),
        )
        return fields["rebalance_raw_return"]

    short_move = (100.0 - 110.0) / 100.0  # short return prev->cur
    qty_old_raw = raw_return_for_qty(2.0)   # the qty actually held over the interval
    qty_new_raw = raw_return_for_qty(4.0)   # the (biased) post-resize qty

    assert qty_old_raw == pytest.approx(short_move * (2.0 * 100.0 / 10_000.0))
    assert qty_new_raw == pytest.approx(short_move * (4.0 * 100.0 / 10_000.0))
    # The doubled qty exactly doubles the contribution — the bias the pre-resize
    # snapshot removes by marking with qty_old.
    assert qty_new_raw == pytest.approx(2.0 * qty_old_raw)


# --------------------------------------------------------------------------- #
# cross-sleeve-3 — reservation trade_id == executed (component-suffixed) id       #
# --------------------------------------------------------------------------- #
def test_crosssleeve3_component_trade_id_matches_executed_row() -> None:
    """The live ensemble path stamps the COMPONENT-suffixed trade_id onto the
    candidate so the cross-sleeve reservation (claim/release/closed-GC keys on
    cand['trade_id']) matches the executed trade row's id (which appends
    -{component}). This pins the format equivalence: the candidate id and the id
    _execute_continuous_entries rebuilds are identical, so a closed-trade GC keyed
    on the real (suffixed) trade_id can match the reservation."""
    base_id = _continuous_trade_id("S", "WIFUSDT", 1_700_000_000_000, 0)
    component = "p3"
    # what run_continuous_demo_cycle now stamps onto the ensemble candidate:
    candidate_trade_id = f"{base_id}-{component}"
    # what _execute_continuous_entries rebuilds for the persisted row (base id from
    # signal_ts/seq, then -{component}):
    executed_trade_id = f"{_continuous_trade_id('S', 'WIFUSDT', 1_700_000_000_000, 0)}-{component}"
    assert candidate_trade_id == executed_trade_id
    # and it is NOT the component-less base (the pre-fix reservation id) — that was
    # the mismatch against the persisted row.
    assert candidate_trade_id != base_id


# ──────────────────────────────────────────────────────────────────────────────
# Relocated from audit bucket iA (cross-file completion regression tests).
# Covers exec-router-6 (widened suborder suffix, backward-compatible decode),
# exec-router-3 (routing invariant enforced at continuous_demo module load),
# ws-risk-6 (in-flight partial-reduce losses count toward the breaker),
# reports-charts-1 (wallet outage surfaced on the LIVE continuous telegram), and
# code-quality-5 (_finite_or_none delegates to _common.finite_float).
# Each test FAILs on the original (pre-fix) code and PASSes on the fix.
# ──────────────────────────────────────────────────────────────────────────────
_STRATEGY = "continuous_fade_v2"
_SIG = 1_765_400_000_000


# exec-router-6 — widened suborder suffix, backward-compatible decode
def _legacy_3char_suffix_link(new_link: str, trade_id: str) -> str:
    """Rebuild the pre-widening (x+3 base36, %46656) form of a suborder link for the
    same base + trade_id, mirroring the old _continuous_suborder_link_id math."""
    base_link = new_link.split("-x")[0]
    suffix = "-x" + _base36(zlib.crc32(trade_id.encode("utf-8")) % 46656).rjust(3, "0")
    return f"{base_link[: 36 - len(suffix)]}{suffix}"


def test_execrouter6_new_suffix_is_four_base36_chars() -> None:
    tid = f"{_STRATEGY}-WIFUSDT-{_SIG}-p3"
    link = _continuous_suborder_link_id(
        CONTINUOUS_ENTRY_LINK_PREFIX, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
    )
    assert "-x" in link
    suffix = link.rsplit("-x", 1)[1]
    assert len(suffix) == 4, link  # widened from 3 -> 4 base36 chars (1.68M buckets)
    assert len(link) <= 36
    # All 4 chars are base36 (the modulus is 36**4).
    assert int(suffix, 36) == zlib.crc32(tid.encode("utf-8")) % 1_679_616


def test_execrouter6_new_link_decodes_to_continuous_sleeve() -> None:
    tid = f"{_STRATEGY}-WIFUSDT-{_SIG}-p3"
    link = _continuous_suborder_link_id(
        CONTINUOUS_ENTRY_LINK_PREFIX, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
    )
    decoded = decode_entry_order_link_id(link)
    assert decoded is not None
    sleeve, ts_ms, seq, _component = decoded
    assert sleeve == "continuous"
    assert ts_ms == _SIG
    assert seq == 0


def test_execrouter6_legacy_3char_link_still_decodes() -> None:
    """LIVE order-routing identity: orderLinkIds written before the widening (x+3, len-4)
    must still strip+decode on a VPS rebuild, NOT fall through to the lossy adopted-* id."""
    tid = f"{_STRATEGY}-WIFUSDT-{_SIG}-p3"
    new_link = _continuous_suborder_link_id(
        CONTINUOUS_ENTRY_LINK_PREFIX, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
    )
    legacy = _legacy_3char_suffix_link(new_link, tid)
    assert len(legacy.rsplit("-x", 1)[1]) == 3
    decoded = decode_entry_order_link_id(legacy)
    assert decoded is not None
    assert decoded[0] == "continuous"
    assert decoded[1] == _SIG


def test_execrouter6_snipe_recovery_round_trips_both_widths() -> None:
    """recover_snipe_trade_id_from_link recovers the EXACT live trade_id from BOTH a new
    4-char link and a legacy 3-char link, recomputing the candidate crc at the link's width."""
    prefix = _continuous_sniper_link_prefix(ContinuousDemoCycleConfig())
    for component in ("p3", "p4p3", ""):
        for seq in (0, 1):
            base = _continuous_trade_id(_STRATEGY, "WIFUSDT", _SIG, seq)
            tid = f"{base}-{component}-snipe" if component else f"{base}-snipe"
            new_link = _continuous_suborder_link_id(
                prefix, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
            )
            assert recover_snipe_trade_id_from_link(
                new_link, strategy_id=_STRATEGY, symbol="WIFUSDT", signal_ts_ms=_SIG
            ) == tid, ("new", component, seq)
            legacy = _legacy_3char_suffix_link(new_link, tid)
            assert recover_snipe_trade_id_from_link(
                legacy, strategy_id=_STRATEGY, symbol="WIFUSDT", signal_ts_ms=_SIG
            ) == tid, ("legacy", component, seq)
    # A link with no -x suffix makes no recovery claim.
    assert recover_snipe_trade_id_from_link(
        "lm-en-cs-WIFUSDT-abc123", strategy_id=_STRATEGY, symbol="WIFUSDT", signal_ts_ms=_SIG
    ) is None


def test_execrouter6_widened_space_lowers_collision_density() -> None:
    """The widened 4-char suffix has ~36x more buckets, so distinct same-symbol same-second
    trade_ids that DID collide on the old 3-char space generally separate on the new one. We
    assert the new full-suffix collision rate is materially below the old 3-char rate over a
    fixed sample (a wider hash is the whole point of the fix)."""
    prefix = _continuous_sniper_link_prefix(ContinuousDemoCycleConfig())
    new_suffixes: dict[str, str] = {}
    old_suffixes: dict[str, str] = {}
    new_collisions = old_collisions = 0
    for i in range(40_000):
        tid = f"S-WIFUSDT-{_SIG}-{i}-snipe"
        link = _continuous_suborder_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid)
        new_suf = link.rsplit("-x", 1)[1]
        old_suf = _base36(zlib.crc32(tid.encode("utf-8")) % 46656).rjust(3, "0")
        if new_suf in new_suffixes and new_suffixes[new_suf] != tid:
            new_collisions += 1
        if old_suf in old_suffixes and old_suffixes[old_suf] != tid:
            old_collisions += 1
        new_suffixes[new_suf] = tid
        old_suffixes[old_suf] = tid
    assert old_collisions > 0  # the old 3-char space saturates well within 40k
    assert new_collisions < old_collisions / 4  # the wider space collides far less


# exec-router-3 — the routing invariant is ENFORCED at config/module load
def test_execrouter3_module_load_enforced_deployed_tags_are_routable() -> None:
    """Importing continuous_demo runs assert_routable_component_tags at module load (the call
    site this bucket wired). The deployed tags must pass — if the guard fired the import above
    would have raised, so reaching here proves the enforced call site exists and is satisfied."""
    tags = _known_ensemble_component_tags()
    assert tags  # non-empty (p3/p4p3/p4p5)
    assert not any(t.startswith("a") for t in tags)
    assert_routable_component_tags(tags)  # explicit no-raise on the live vocabulary


def test_execrouter3_guard_would_have_caught_an_a_prefixed_tag() -> None:
    with pytest.raises(ValueError, match="must not begin with 'a'"):
        assert_routable_component_tags([*_known_ensemble_component_tags(), "avg"])


# ws-risk-6 — in-flight partial-reduce losses count toward the breaker
def _now() -> int:
    return _SIG


def test_wsrisk6_open_partial_reduce_loss_counts_toward_breaker() -> None:
    now = _now()
    df = pl.DataFrame(
        [
            # OPEN row whose in-flight partial reduce crystallized a loss, in-window -> counts.
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.012,
                "partial_exit_ts_ms": now - 10 * 60_000, "updated_at_ms": now - 10 * 60_000,
            },
            # OPEN row with a partial GAIN -> not adverse.
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": 0.02,
                "partial_exit_ts_ms": now - 5 * 60_000, "updated_at_ms": now - 5 * 60_000,
            },
        ],
        infer_schema_length=None,
    )
    # Pre-fix: the closed-only breaker read both open rows as net 0 -> 0.
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 1


def test_wsrisk6_partial_loss_outside_window_not_counted() -> None:
    now = _now()
    df = pl.DataFrame(
        [
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.05,
                "partial_exit_ts_ms": now - 600 * 60_000, "updated_at_ms": now - 600 * 60_000,
            },
        ],
        infer_schema_length=None,
    )
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 0


def test_recent_adverse_exit_count_uses_exact_millisecond_boundary() -> None:
    now = _SIG + 123
    cutoff = now - 60 * 60_000
    df = pl.DataFrame(
        [
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": cutoff, "net_return": -0.03},
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": cutoff - 1, "net_return": -0.03},
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.01,
                "partial_exit_ts_ms": cutoff, "updated_at_ms": cutoff,
                "net_return": None,
            },
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.01,
                "partial_exit_ts_ms": cutoff - 1, "updated_at_ms": cutoff - 1,
                "net_return": None,
            },
        ],
        infer_schema_length=None,
    )

    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 2


def test_wsrisk6_partial_plus_closed_are_additive_and_not_double_counted() -> None:
    now = _now()
    df = pl.DataFrame(
        [
            # open partial loss in-window
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.01,
                "partial_exit_ts_ms": now - 8 * 60_000, "updated_at_ms": now - 8 * 60_000,
                "net_return": None,
            },
            # closed adverse in-window (a DIFFERENT trade)
            {
                "status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000,
                "partial_exit_realized_return": None, "partial_exit_ts_ms": None,
                "updated_at_ms": now - 20 * 60_000, "net_return": -0.03,
            },
            # a closed row whose partial_exit fields are still stamped (it had a partial then
            # fully closed): status=='closed' means the open branch ignores it -> counted once
            # via the closed branch only (no double count).
            {
                "status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 15 * 60_000,
                "partial_exit_realized_return": -0.02, "partial_exit_ts_ms": now - 30 * 60_000,
                "updated_at_ms": now - 15 * 60_000, "net_return": -0.04,
            },
        ],
        infer_schema_length=None,
    )
    # 1 open partial loss + 2 closed adverse = 3 (the closed-with-stamped-partial is NOT 2).
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 3


def test_wsrisk6_legacy_schema_without_partial_columns_unchanged() -> None:
    """Backward compat: a ledger frame with no partial_exit_* columns behaves exactly as the
    pre-fix closed-only count (numerically equivalent on the old schema)."""
    now = _now()
    df = pl.DataFrame(
        [
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000, "net_return": -0.03},
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000, "net_return": 0.04},
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000,
             "exit_reason": "stop_approach", "net_return": 0.0},
        ],
        infer_schema_length=None,
    )
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 2


# reports-charts-1 — wallet outage surfaced on the LIVE continuous telegram
class _FakePrivateStateCache:
    def __init__(self, seconds_since_ws: float) -> None:
        self._seconds_since_ws = seconds_since_ws

    def seconds_since_last_ws_event(self) -> float:
        return self._seconds_since_ws


def test_continuous_entry_risk_health_blocks_submit_on_private_ws_stale() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, entry_private_ws_stale_seconds=300.0)

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="rest",
        private_state_cache=_FakePrivateStateCache(301.0),
        open_trades=pl.DataFrame(),
        live_position_symbols=set(),
    )

    assert got["entry_risk_health_ok"] is False
    assert got["entry_risk_health_reasons"] == "private_ws_stale"
    assert got["entry_risk_health_private_ws_silence_seconds"] == pytest.approx(301.0)
    assert got["entry_risk_health_private_ws_health_ok"] is None


def test_continuous_entry_risk_health_accepts_quiet_healthy_private_ws() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, entry_private_ws_stale_seconds=300.0)

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(10_000.0),
        open_trades=pl.DataFrame(),
        live_position_symbols=set(),
        private_ws_health_ok=True,
    )

    assert got["entry_risk_health_ok"] is True
    assert got["entry_risk_health_reasons"] == ""
    assert got["entry_risk_health_private_ws_silence_seconds"] == pytest.approx(10_000.0)
    assert got["entry_risk_health_private_ws_health_ok"] is True


def test_continuous_entry_risk_health_blocks_quiet_unhealthy_private_ws() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, entry_private_ws_stale_seconds=300.0)

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(301.0),
        open_trades=pl.DataFrame(),
        live_position_symbols=set(),
        private_ws_health_ok=False,
    )

    assert got["entry_risk_health_ok"] is False
    assert got["entry_risk_health_reasons"] == "private_ws_stale"
    assert got["entry_risk_health_private_ws_health_ok"] is False


def test_continuous_entry_risk_health_does_not_block_dry_run_on_private_ws_stale() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=False, entry_private_ws_stale_seconds=300.0)

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="rest",
        private_state_cache=_FakePrivateStateCache(10_000.0),
        open_trades=pl.DataFrame(),
        live_position_symbols=set(),
    )

    assert got["entry_risk_health_ok"] is True
    assert got["entry_risk_health_reasons"] == ""


def test_continuous_entry_risk_health_blocks_submit_on_ledger_position_mismatch() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True)
    open_trades = pl.DataFrame(
        [{"strategy_id": _STRATEGY, "status": "open", "symbol": "AAAUSDT"}],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=open_trades,
        live_position_symbols=set(),
    )

    assert got["entry_risk_health_ok"] is False
    assert got["entry_risk_health_reasons"] == "ledger_position_mismatch"
    assert got["entry_risk_health_ledger_missing_positions"] == "AAAUSDT"


def test_continuous_entry_risk_health_blocks_submit_on_unprotected_non_hedge_position() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, stop_loss_pct=0.25)
    now = 1_800_000_000_000
    open_trades = pl.DataFrame(
        [
            {
                "strategy_id": _STRATEGY,
                "status": "open",
                "symbol": "AAAUSDT",
                "side": "short",
                "opened_at_ms": now - 300_000,
            }
        ],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=open_trades,
        live_position_symbols={"AAAUSDT"},
        live_positions_by_symbol={"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "0"}},
        now_ms=now,
    )

    assert got["entry_risk_health_ok"] is False
    assert got["entry_risk_health_reasons"] == "unprotected_non_hedge_position"
    assert got["entry_risk_health_unprotected_positions"] == "AAAUSDT"
    assert got["entry_risk_health_unprotected_position_ages"] == "AAAUSDT:300"
    assert got["entry_risk_health_unprotected_max_age_seconds"] == pytest.approx(300.0)


def test_continuous_entry_risk_health_records_unprotected_stopless_v2_without_blocking() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, stop_loss_pct=0.0)
    now = 1_800_000_000_000
    open_trades = pl.DataFrame(
        [
            {
                "strategy_id": _STRATEGY,
                "status": "open",
                "symbol": "AAAUSDT",
                "side": "short",
                "opened_at_ms": now - 300_000,
            }
        ],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=open_trades,
        live_position_symbols={"AAAUSDT"},
        live_positions_by_symbol={"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "0"}},
        now_ms=now,
    )

    assert got["entry_risk_health_ok"] is True
    assert got["entry_risk_health_reasons"] == ""
    assert got["entry_risk_health_unprotected_positions"] == "AAAUSDT"
    assert got["entry_risk_health_unprotected_position_ages"] == "AAAUSDT:300"
    assert got["entry_risk_health_unprotected_max_age_seconds"] == pytest.approx(300.0)


def test_continuous_entry_risk_health_accepts_protected_non_hedge_position() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True)
    open_trades = pl.DataFrame(
        [{"strategy_id": _STRATEGY, "status": "open", "symbol": "AAAUSDT", "side": "short"}],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=open_trades,
        live_position_symbols={"AAAUSDT"},
        live_positions_by_symbol={"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "125"}},
    )

    assert got["entry_risk_health_ok"] is True
    assert got["entry_risk_health_reasons"] == ""
    assert got["entry_risk_health_unprotected_positions"] == ""


def test_continuous_entry_risk_health_does_not_block_unprotected_hedge_position() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True)
    open_trades = pl.DataFrame(
        [
            {
                "strategy_id": _STRATEGY,
                "status": "open",
                "symbol": "BTCUSDT",
                "side": "long",
                "sleeve": "continuous_addon",
            }
        ],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=open_trades,
        live_position_symbols={"BTCUSDT"},
        live_positions_by_symbol={"BTCUSDT": {"symbol": "BTCUSDT", "side": "Buy", "size": "1", "stopLoss": "0"}},
    )

    assert got["entry_risk_health_ok"] is True
    assert got["entry_risk_health_reasons"] == ""
    assert got["entry_risk_health_unprotected_positions"] == ""


def test_continuous_lifecycle_state_classifier() -> None:
    live_stop = {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "125"}
    live_no_stop = {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "0"}

    assert _continuous_lifecycle_state({"status": "closed"}, live_position=live_stop) == "CLOSED"
    assert _continuous_lifecycle_state({"status": "failed"}, live_position=live_stop) == "FAILED"
    assert (
        _continuous_lifecycle_state({"trade_id": "adopted-AAAUSDT", "status": "open"}, live_position=live_stop)
        == "ADOPTED"
    )
    assert (
        _continuous_lifecycle_state(
            {"status": "open", "exit_order_link_id": "lm-ux-c-AAA-1"},
            live_position=live_stop,
        )
        == "EXIT_ORDER_SUBMITTED"
    )
    assert _continuous_lifecycle_state({"status": "open", "side": "short"}, live_position=None) == "ORPHAN"
    assert _continuous_lifecycle_state({"status": "open", "side": "short"}, live_position=live_stop) == "PROTECTED"
    assert (
        _continuous_lifecycle_state({"status": "open", "side": "short"}, live_position=live_no_stop)
        == "PROTECTION_PENDING"
    )
    assert _continuous_lifecycle_state({"status": "open", "side": "long"}, live_position=live_no_stop) == "FILLED"


def test_continuous_entry_risk_health_records_lifecycle_state_counts() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True)
    open_trades = pl.DataFrame(
        [
            {"strategy_id": _STRATEGY, "status": "open", "symbol": "PROTUSDT", "side": "short"},
            {"strategy_id": _STRATEGY, "status": "open", "symbol": "PENDUSDT", "side": "short"},
            {"strategy_id": _STRATEGY, "status": "open", "symbol": "MISSUSDT", "side": "short"},
        ],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=open_trades,
        live_position_symbols={"PROTUSDT", "PENDUSDT"},
        live_positions_by_symbol={
            "PROTUSDT": {"symbol": "PROTUSDT", "side": "Sell", "size": "1", "stopLoss": "125"},
            "PENDUSDT": {"symbol": "PENDUSDT", "side": "Sell", "size": "1", "stopLoss": "0"},
        },
    )

    assert got["entry_risk_health_ok"] is False
    assert got["entry_risk_health_lifecycle_states"] == "PROTECTION_PENDING:1,PROTECTED:1,ORPHAN:1"
    assert got["entry_risk_health_lifecycle_protection_pending"] == 1
    assert got["entry_risk_health_lifecycle_orphan"] == 1


def test_continuous_lifecycle_snapshot_update_rows_promotes_protected_trade() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": _STRATEGY,
                "symbol": "AAAUSDT",
                "status": "open",
                "side": "short",
                "qty": "1",
                "entry_price": 100.0,
                "updated_at_ms": 1,
            }
        ],
        infer_schema_length=None,
    )

    rows = _continuous_lifecycle_snapshot_update_rows(
        open_trades,
        live_positions_by_symbol={"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "125"}},
        now_ms=123_000,
    )

    assert len(rows) == 1
    assert rows[0]["trade_id"] == "t1"
    assert rows[0]["lifecycle_state"] == "PROTECTED"
    assert rows[0]["lifecycle_state_source"] == "private_position_snapshot"
    assert rows[0]["lifecycle_state_updated_at_ms"] == 123_000
    assert rows[0]["lifecycle_position_stop_loss"] == pytest.approx(125.0)
    assert rows[0]["updated_at_ms"] == 123_000
    assert rows[0]["qty"] == "1"


def test_continuous_lifecycle_snapshot_update_rows_skips_already_protected_trade() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": _STRATEGY,
                "symbol": "AAAUSDT",
                "status": "open",
                "side": "short",
                "lifecycle_state": "PROTECTED",
            }
        ],
        infer_schema_length=None,
    )

    rows = _continuous_lifecycle_snapshot_update_rows(
        open_trades,
        live_positions_by_symbol={"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "125"}},
        now_ms=123_000,
    )

    assert rows == []


def test_continuous_lifecycle_snapshot_update_rows_never_demotes_missing_stop() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": _STRATEGY,
                "symbol": "AAAUSDT",
                "status": "open",
                "side": "short",
                "lifecycle_state": "PROTECTED",
            }
        ],
        infer_schema_length=None,
    )

    rows = _continuous_lifecycle_snapshot_update_rows(
        open_trades,
        live_positions_by_symbol={"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "stopLoss": "0"}},
        now_ms=123_000,
    )

    assert rows == []


def test_continuous_lifecycle_transition_guard_rejects_closed_trade_reopen() -> None:
    existing = pl.DataFrame(
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "closed", "lifecycle_state": "CLOSED"}],
        infer_schema_length=None,
    )

    accepted, violations = _enforce_continuous_lifecycle_transitions(
        existing,
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "open", "side": "short"}],
        submit_orders=True,
    )

    assert accepted == []
    assert violations == [
        {
            "reason": "closed_trade_reopened",
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "incoming_status": "open",
            "incoming_lifecycle_state": "PROTECTION_PENDING",
            "prior_status": "closed",
            "prior_lifecycle_state": "CLOSED",
        }
    ]


def test_continuous_lifecycle_transition_guard_rejects_unknown_close() -> None:
    accepted, violations = _enforce_continuous_lifecycle_transitions(
        pl.DataFrame(),
        [{"trade_id": "missing", "symbol": "AAAUSDT", "status": "closed"}],
        submit_orders=True,
    )

    assert accepted == []
    assert violations[0]["reason"] == "closed_without_prior_trade"
    assert violations[0]["incoming_lifecycle_state"] == "CLOSED"


def test_continuous_lifecycle_transition_guard_accepts_open_to_closed() -> None:
    existing = pl.DataFrame(
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "open", "side": "short"}],
        infer_schema_length=None,
    )

    accepted, violations = _enforce_continuous_lifecycle_transitions(
        existing,
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "closed", "exit_order_link_id": "lm-ux-c-AAA-1"}],
        submit_orders=True,
    )

    assert violations == []
    assert accepted[0]["lifecycle_state"] == "CLOSED"


def test_continuous_lifecycle_transition_guard_rejects_protection_regression() -> None:
    existing = pl.DataFrame(
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "open", "side": "short", "lifecycle_state": "PROTECTED"}],
        infer_schema_length=None,
    )

    accepted, violations = _enforce_continuous_lifecycle_transitions(
        existing,
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "open", "side": "short"}],
        submit_orders=True,
    )

    assert accepted == []
    assert violations[0]["reason"] == "illegal_lifecycle_transition"
    assert violations[0]["prior_lifecycle_state"] == "PROTECTED"
    assert violations[0]["incoming_lifecycle_state"] == "PROTECTION_PENDING"


def test_continuous_lifecycle_transition_guard_preserves_protected_plain_update() -> None:
    existing = pl.DataFrame(
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "open", "side": "short", "lifecycle_state": "PROTECTED"}],
        infer_schema_length=None,
    )

    accepted, violations = _enforce_continuous_lifecycle_transitions(
        existing,
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "status": "open",
                "side": "short",
                "qty": "1.5",
                "lifecycle_state": "PROTECTED",
            }
        ],
        submit_orders=True,
    )

    assert violations == []
    assert accepted[0]["lifecycle_state"] == "PROTECTED"


def test_continuous_lifecycle_transition_guard_rejects_exit_link_loss() -> None:
    existing = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "status": "open",
                "side": "short",
                "exit_order_link_id": "lm-ux-c-AAA-1",
                "lifecycle_state": "EXIT_ORDER_SUBMITTED",
            }
        ],
        infer_schema_length=None,
    )

    accepted, violations = _enforce_continuous_lifecycle_transitions(
        existing,
        [{"trade_id": "t1", "symbol": "AAAUSDT", "status": "open", "side": "short"}],
        submit_orders=True,
    )

    assert accepted == []
    assert violations[0]["reason"] == "illegal_lifecycle_transition"
    assert violations[0]["prior_lifecycle_state"] == "EXIT_ORDER_SUBMITTED"
    assert violations[0]["incoming_lifecycle_state"] == "PROTECTION_PENDING"


def test_continuous_lifecycle_transition_guard_stamps_dry_run_without_rejecting() -> None:
    accepted, violations = _enforce_continuous_lifecycle_transitions(
        pl.DataFrame(),
        [{"trade_id": "missing", "symbol": "AAAUSDT", "status": "closed"}],
        submit_orders=False,
    )

    assert violations == []
    assert accepted[0]["lifecycle_state"] == "CLOSED"


def test_continuous_portfolio_heat_cap_clamps_submit_entry_capacity() -> None:
    cfg = ContinuousDemoCycleConfig(
        submit_orders=True,
        entry_portfolio_heat_cap_frac=0.05,
        entry_portfolio_heat_shock_frac=1.0,
        per_position_notional_pct_equity=2.0,
        sizing_mode="flat",
    )
    open_trades = pl.DataFrame(
        [
            {"status": "open", "symbol": "AAAUSDT", "side": "short", "notional_usdt": 400.0},
            {"status": "open", "symbol": "BTCUSDT", "side": "long", "notional_usdt": 10_000.0},
        ],
        infer_schema_length=None,
    )

    got = _continuous_portfolio_heat_fields(
        open_trades,
        equity_usdt=10_000.0,
        config=cfg,
        max_new_entries=5,
        enabled=True,
    )

    assert got["portfolio_heat_current_frac"] == pytest.approx(0.04)
    assert got["portfolio_heat_entry_unit_frac"] == pytest.approx(0.02)
    assert got["portfolio_heat_entry_capacity"] == 0
    assert got["portfolio_heat_clamped"] is True
    assert got["skipped_portfolio_heat"] == 5


def test_continuous_portfolio_heat_telemetry_does_not_clamp_when_disabled() -> None:
    cfg = ContinuousDemoCycleConfig(
        submit_orders=False,
        entry_portfolio_heat_cap_frac=0.05,
        entry_portfolio_heat_shock_frac=1.0,
        per_position_notional_pct_equity=2.0,
        sizing_mode="flat",
    )
    open_trades = pl.DataFrame(
        [{"status": "open", "symbol": "AAAUSDT", "side": "short", "notional_usdt": 400.0}],
        infer_schema_length=None,
    )

    got = _continuous_portfolio_heat_fields(
        open_trades,
        equity_usdt=10_000.0,
        config=cfg,
        max_new_entries=5,
        enabled=False,
    )

    assert got["portfolio_heat_current_frac"] == pytest.approx(0.04)
    assert got["portfolio_heat_entry_capacity"] == 5
    assert got["portfolio_heat_clamped"] is False
    assert got["skipped_portfolio_heat"] == 0


def test_continuous_account_drawdown_kill_switch_trips_on_healthy_equity_drawdown() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, entry_account_drawdown_kill_switch_frac=0.02)
    cycles = pl.DataFrame(
        [
            {"equity_usdt": 10_000.0, "wallet_error": ""},
            {"equity_usdt": 11_000.0, "wallet_error": ""},
            {"equity_usdt": 12_000.0, "wallet_error": "wallet timeout"},
        ],
        infer_schema_length=None,
    )

    got = _continuous_account_drawdown_fields(
        cycles,
        current_equity_usdt=10_600.0,
        config=cfg,
        current_snapshot_ok=True,
    )

    assert got["entry_account_equity_high_water_usdt"] == pytest.approx(11_000.0)
    assert got["entry_account_drawdown_frac"] == pytest.approx((10_600.0 / 11_000.0) - 1.0)
    assert got["entry_account_drawdown_kill_switch_tripped"] is True


def test_continuous_account_drawdown_kill_switch_ignores_unhealthy_current_snapshot() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True, entry_account_drawdown_kill_switch_frac=0.02)
    cycles = pl.DataFrame([{"equity_usdt": 11_000.0, "wallet_error": ""}], infer_schema_length=None)

    got = _continuous_account_drawdown_fields(
        cycles,
        current_equity_usdt=10_000.0,
        config=cfg,
        current_snapshot_ok=False,
    )

    assert got["entry_account_drawdown_frac"] == pytest.approx((10_000.0 / 11_000.0) - 1.0)
    assert got["entry_account_drawdown_kill_switch_tripped"] is False


def test_continuous_entry_risk_health_blocks_submit_on_attributed_exchange_only_position() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True)
    now = 1_800_000_000_000
    entry_link = _continuous_order_link_id("en-c", symbol="AAAUSDT", signal_ts_ms=now - MS_PER_HOUR)
    all_orders = pl.DataFrame(
        [
            {
                "order_link_id": entry_link,
                "strategy_id": _STRATEGY,
                "symbol": "AAAUSDT",
                "reduce_only": False,
                "submit_mode": "submitted",
                "status": "submitted_unconfirmed",
                "ts_ms": now - 60_000,
                "updated_at_ms": now - 60_000,
            }
        ],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=pl.DataFrame(),
        live_position_symbols={"AAAUSDT"},
        all_orders=all_orders,
        now_ms=now,
        managed_strategy_ids=(_STRATEGY,),
    )

    assert got["entry_risk_health_ok"] is False
    assert got["entry_risk_health_reasons"] == "exchange_position_without_ledger"
    assert got["entry_risk_health_exchange_only_positions"] == "AAAUSDT"


def test_continuous_entry_risk_health_does_not_block_unattributed_exchange_only_position() -> None:
    cfg = ContinuousDemoCycleConfig(submit_orders=True)
    now = 1_800_000_000_000
    old_link = _continuous_order_link_id("en-c", symbol="OLDUSDT", signal_ts_ms=now - 100 * MS_PER_HOUR)
    all_orders = pl.DataFrame(
        [
            {
                "order_link_id": old_link,
                "strategy_id": _STRATEGY,
                "symbol": "OLDUSDT",
                "reduce_only": False,
                "submit_mode": "submitted",
                "status": "filled",
                "ts_ms": now - 100 * MS_PER_HOUR,
                "updated_at_ms": now - 100 * MS_PER_HOUR,
            },
            {
                "order_link_id": "manual",
                "strategy_id": "long_native_v11a",
                "symbol": "LONGUSDT",
                "reduce_only": False,
                "submit_mode": "submitted",
                "status": "submitted",
                "ts_ms": now - 60_000,
                "updated_at_ms": now - 60_000,
            },
        ],
        infer_schema_length=None,
    )

    got = _continuous_entry_risk_health(
        config=cfg,
        snapshot={"equity_usdt": 10_000.0, "raw_open_orders": [], "raw_positions": []},
        snapshot_source="ws_cache",
        private_state_cache=_FakePrivateStateCache(1.0),
        open_trades=pl.DataFrame(),
        live_position_symbols={"OLDUSDT", "LONGUSDT"},
        all_orders=all_orders,
        now_ms=now,
        managed_strategy_ids=(_STRATEGY,),
    )

    assert got["entry_risk_health_ok"] is True
    assert got["entry_risk_health_reasons"] == ""
    assert got["entry_risk_health_exchange_only_positions"] == ""


def test_continuous_entry_risk_health_pages_and_formats_reason() -> None:
    payload = {
        "mode": "submit",
        "equity_usdt": 10_000.0,
        "entries": 0,
        "exits": 0,
        "open_positions": 1,
        "candidates": 0,
        "rmom_present": True,
        "entry_paused": False,
        "entry_risk_health_ok": False,
        "entry_risk_health_reasons": (
            "private_ws_stale,ledger_position_mismatch,unprotected_non_hedge_position,"
            "exchange_position_without_ledger,account_drawdown_kill_switch"
        ),
        "entry_risk_health_snapshot_source": "rest",
        "entry_risk_health_ledger_missing_positions": "AAAUSDT",
        "entry_risk_health_unprotected_positions": "CCCUSDT",
        "entry_risk_health_unprotected_position_ages": "CCCUSDT:900",
        "entry_risk_health_unprotected_max_age_seconds": 900.0,
        "entry_risk_health_exchange_only_positions": "BBBUSDT",
        "entry_risk_health_lifecycle_states": "PROTECTION_PENDING:1,ORPHAN:1",
        "entry_account_drawdown_kill_switch_tripped": True,
        "entry_account_drawdown_frac": -0.03125,
        "entry_account_equity_high_water_usdt": 16_000.0,
        "entry_account_drawdown_kill_switch_frac": 0.02,
        "portfolio_overview": "Portfolio overview\n- Long (ON): 1 open [ADAUSDT long $591.83]",
    }

    assert _continuous_telegram_reason(payload, [], []) == "continuous_entry_risk_health_blocked"
    msg = format_continuous_telegram_status_message(
        payload,
        [],
        [],
        reason="continuous_entry_risk_health_blocked",
    )

    assert (
        "entry_risk_health=private_ws_stale,ledger_position_mismatch,unprotected_non_hedge_position,"
        "exchange_position_without_ledger,account_drawdown_kill_switch snapshot=rest"
    ) in msg
    assert "ledger_missing_positions=AAAUSDT" in msg
    assert "unprotected_positions=CCCUSDT max_age_s=900" in msg
    assert "unprotected_position_ages=CCCUSDT:900" in msg
    assert "exchange_only_positions=BBBUSDT" in msg
    assert "lifecycle_states=PROTECTION_PENDING:1,ORPHAN:1" in msg
    assert "account_drawdown=-0.0312 high_water=$16,000.00 limit=0.0200" in msg
    assert "Portfolio overview" in msg
    assert "ADAUSDT long" in msg
    assert (
        "risk_health=private_ws_stale,ledger_position_mismatch,unprotected_non_hedge_position,"
        "exchange_position_without_ledger,account_drawdown_kill_switch"
    ) in format_continuous_demo_cycle_summary(payload)


def test_append_continuous_risk_event_writes_jsonl(tmp_path) -> None:
    first = _append_continuous_risk_event(
        tmp_path,
        {"event": "entry_risk_health_blocked", "ts_ms": 1, "reasons": "private_ws_stale"},
    )
    second = _append_continuous_risk_event(
        tmp_path,
        {"event": "entry_risk_health_blocked", "ts_ms": 2, "reasons": "ledger_position_mismatch"},
    )

    assert first == second == tmp_path / CONTINUOUS_RISK_EVENTS_FILE
    rows = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"event": "entry_risk_health_blocked", "reasons": "private_ws_stale", "ts_ms": 1},
        {"event": "entry_risk_health_blocked", "reasons": "ledger_position_mismatch", "ts_ms": 2},
    ]


def test_append_continuous_lifecycle_event_writes_jsonl(tmp_path) -> None:
    path = _append_continuous_lifecycle_event(
        tmp_path,
        {
            "event": "lifecycle_state_transition",
            "cycle_id": "c1",
            "trade_id": "t1",
            "lifecycle_state": "ORDER_PREPARED",
        },
    )

    assert path == tmp_path / CONTINUOUS_LIFECYCLE_EVENTS_FILE
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "cycle_id": "c1",
            "event": "lifecycle_state_transition",
            "lifecycle_state": "ORDER_PREPARED",
            "trade_id": "t1",
        }
    ]


def test_continuous_lifecycle_order_event_classifies_preflight_entry() -> None:
    event = _continuous_lifecycle_event_from_order_row(
        {
            "order_link_id": "lm-en-c-AAA-abc",
            "trade_id": "t1",
            "strategy_id": _STRATEGY,
            "symbol": "AAAUSDT",
            "side": "Sell",
            "reduce_only": False,
            "submit_mode": "preflight",
            "status": "submitted",
            "updated_at_ms": 123,
        },
        cycle_id="cycle-1",
        source="order_preflight",
        now_ms=999,
    )

    assert event["event"] == "lifecycle_state_transition"
    assert event["event_key"] == "cycle-1:order_preflight:t1:lm-en-c-AAA-abc:ORDER_PREPARED"
    assert event["lifecycle_state"] == "ORDER_PREPARED"
    assert event["lifecycle_state_source"] == "order_preflight"
    assert event["ts_ms"] == 123


def test_continuous_lifecycle_order_event_classifies_reduce_only_fill_as_closed() -> None:
    event = _continuous_lifecycle_event_from_order_row(
        {
            "order_link_id": "lm-ux-c-AAA-abc",
            "trade_id": "t1",
            "strategy_id": _STRATEGY,
            "symbol": "AAAUSDT",
            "side": "Buy",
            "reduce_only": True,
            "submit_mode": "submitted",
            "status": "filled",
        },
        cycle_id="cycle-1",
        source="order_final",
        now_ms=999,
    )

    assert event["lifecycle_state"] == "CLOSED"
    assert event["lifecycle_state_source"] == "order_final"
    assert event["ts_ms"] == 999


def test_continuous_lifecycle_trade_event_uses_persisted_protection_source() -> None:
    event = _continuous_lifecycle_event_from_trade_row(
        {
            "trade_id": "t1",
            "strategy_id": _STRATEGY,
            "symbol": "AAAUSDT",
            "status": "open",
            "side": "short",
            "lifecycle_state": "PROTECTED",
            "lifecycle_state_source": "private_position_snapshot",
            "updated_at_ms": 123,
        },
        cycle_id="cycle-1",
        source="trade_ledger",
        now_ms=999,
    )

    assert event["event_key"] == "cycle-1:private_position_snapshot:t1:PROTECTED"
    assert event["lifecycle_state"] == "PROTECTED"
    assert event["lifecycle_state_source"] == "private_position_snapshot"
    assert event["ts_ms"] == 123


def _wallet_outage_payload() -> dict:
    return {
        "mode": "submit", "equity_usdt": 10_000.0,
        "wallet_error": "wallet equity unavailable: 401 rate-limited",
        "entries": 0, "exits": 0, "open_positions": 2, "candidates": 3,
        "rmom_present": True, "entry_paused": False,
    }


def test_reportscharts1_wallet_error_pages_the_operator() -> None:
    payload = _wallet_outage_payload()
    # Pre-fix: a wallet-only outage produced "" (quiet) -> no page.
    assert _continuous_telegram_reason(payload, [], []) == "continuous_wallet_error"


def test_reportscharts1_message_tags_fallback_and_surfaces_error() -> None:
    payload = _wallet_outage_payload()
    msg = format_continuous_telegram_status_message(payload, [], [], reason="continuous_wallet_error")
    assert "(FALLBACK - wallet read failed)" in msg  # the $10,000 print is no longer trusted
    assert "wallet_error=wallet equity unavailable: 401 rate-limited" in msg


def test_reportscharts1_healthy_read_is_quiet_and_untagged() -> None:
    payload = _wallet_outage_payload()
    payload["wallet_error"] = ""  # event_demo returns "" on a healthy read
    assert _continuous_telegram_reason(payload, [], []) == ""  # quiet, no spurious page
    msg = format_continuous_telegram_status_message(payload, [], [], reason="continuous_entry_executed")
    assert "FALLBACK" not in msg
    assert "wallet_error=" not in msg
    assert "equity=$10,000.00" in msg


# code-quality-5 — _finite_or_none delegates to _common.finite_float
def test_codequality5_finite_or_none_matches_finite_float_default_none() -> None:
    cases = [1.5, "2.0", None, "not-a-number", float("nan"), float("inf"), float("-inf"), 0, "", 42]
    for value in cases:
        expected = finite_float(value, default=None)
        got = _finite_or_none(value)
        # Treat NaN-vs-NaN as equal (finite_float never returns NaN, but be explicit).
        if isinstance(expected, float) and isinstance(got, float) and math.isnan(expected) and math.isnan(got):
            continue
        assert got == expected, (value, got, expected)
    # Spot-check the contract directly.
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(None) is None
    assert _finite_or_none("3.25") == 3.25


def test_validate_rejects_ensemble_component_invalid_trigger() -> None:
    """audit-iter3: an unknown ensemble component trigger must fail fast in the
    validator, not at cycle time."""
    with pytest.raises(ValueError):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                record_dry_run=True,
                ensemble_components=(("c1", "not_a_real_trigger", 0, 0.1, 1.0),),
            )
        )


def test_validate_rejects_ensemble_trigger_without_confirm_delay() -> None:
    """audit-iter3: an ensemble component event-trigger requires confirmed-bar timing."""
    with pytest.raises(ValueError, match="confirmed-bar"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                record_dry_run=True,
                entry_confirm_delay_hours=0,
                ensemble_components=(("c1", "fresh_pop25", 0, 0.1, 1.0),),
            )
        )
