"""Tests for the live continuous-fade demo sleeve (liquidity_migration.continuous_demo).

The headline test is EQUIVALENCE: the live state (confirmed history + live price as the current bar)
must reproduce the verified backtest decile exactly — the live signal == the backtest signal. Plus
entry/exit selection and the distinct continuous orderLinkId prefix (ws_risk fill routing).
"""
from __future__ import annotations

import numpy as np
import polars as pl

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.continuous_demo import (
    ContinuousDemoCycleConfig,
    LivePanelCache,
    build_live_continuous_state,
    continuous_dataset_names,
    _continuous_order_link_id,
    _protective_exit_reason,
    _recent_exit_cooldown_symbols,
    entry_circuit_breaker_tripped,
    plan_continuous_exits,
    plan_protective_exits,
    run_continuous_protective_exit_cycle,
    select_continuous_entries,
)
from liquidity_migration.continuous_events import compute_continuous_decile_panel
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
    sleeve, signal_ts_ms = decoded
    assert sleeve == "continuous"
    assert signal_ts_ms == (sig // 1000) * 1000
    # the short (4-part) and long (en-l) links still decode as their own sleeves
    assert decode_entry_order_link_id("lm-en-BTC-abcd")[0] == "short"
    assert decode_entry_order_link_id("lm-en-l-BTC-abcd")[0] == "long"


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


def test_execute_exits_dry_run_closes_short() -> None:
    from liquidity_migration.continuous_demo import _execute_continuous_exits

    cfg = ContinuousDemoCycleConfig(submit_orders=False, record_dry_run=True)
    trade = {"trade_id": "continuous_fade_v1-WIFUSDT-1700000000", "symbol": "WIFUSDT", "side": "short",
             "status": "open", "entry_price": 100.0, "qty": "2", "equity_usdt": 10_000.0, "notional_usdt": 200.0}
    all_trades = pl.DataFrame([trade])
    plan = [{**trade, "exit_reason": "left_decile"}]
    rows, orders = _execute_continuous_exits(plan, all_trades, trading_client=None, demo=cfg,
                                             now_ms=1_700_100_000_000, record_preflight=None)
    assert len(rows) == 1 and orders[0]["side"] == "Buy" and orders[0]["reduce_only"] is True
    assert rows[0]["status"] == "closed" and rows[0]["exit_reason"] == "left_decile"
    assert orders[0]["sleeve"] == "continuous" and orders[0]["order_link_id"].startswith("lm-ux-c-")


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
