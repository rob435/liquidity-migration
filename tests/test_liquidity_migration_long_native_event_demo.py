"""Tests for the long-sleeve forward-testing module (v11a MultiStratV1).

Covers:
- Profile loader returns the v11a uni10 sniper config
- Order-link prefix is `lm-en-l-*` so ws_risk routes fills to the long ledger
- Per-position notional sizing scales by notional_multiplier × base
- Sniper retrace candidate selection enters when live price reaches threshold
  AND falls through after deadline expires
- Cooldown prevents same-symbol re-entry within cooldown_days
- Time-stop exit plans only trigger past planned_exit_ts_ms
- Telegram notification fires only on material events
- Combined-book aggregate roll-up reads both ledgers
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native_event_demo import (
    LONG_DEMO_STRATEGY_PROFILES,
    LONG_DEMO_TRADES_DATASET,
    LONG_ENTRY_LINK_PREFIX,
    LONG_EXIT_LINK_PREFIX,
    LongNativeDemoCycleConfig,
    MULTI_STRAT_V1_STRATEGY_ID,
    _filter_pending_long_entries,
    _long_demo_event_config,
    _long_demo_strategy_id,
    _long_order_link_id,
    _maybe_long_notify,
    _open_long_trades,
    _plan_time_stop_exits,
    _select_long_entry_candidates,
    _validate_long_demo_config,
    _v11a_long_native_config,
    _vol_parity_weight,
    format_combined_book_summary,
    format_long_demo_cycle_summary,
    format_long_telegram_status_message,
    projected_long_initial_margin_pct_equity,
    target_long_order_notional_pct_equity,
    _long_telegram_reason,
)
from liquidity_migration.storage import write_dataset


def test_v11a_config_matches_research_run() -> None:
    cfg = _v11a_long_native_config()
    # FC-only universe
    assert cfg.enable_fomo_chase
    assert not cfg.enable_capitulation_rebound
    assert not cfg.enable_volume_resurrection
    assert not cfg.enable_funding_squeeze
    assert not cfg.enable_oversold_bounce
    assert not cfg.enable_uptrend_dip
    # div: uni50 (promoted 2026-05-30, was uni10)
    assert cfg.universe_size == 50
    # sniper retrace
    assert cfg.fc_use_sniper_entry
    assert cfg.fc_sniper_retrace_pct == pytest.approx(0.01)
    assert cfg.fc_sniper_deadline_hours == 6
    assert cfg.fc_sniper_skip_on_no_retrace is False  # fall-through
    # ATR exits
    assert cfg.fc_use_atr_exits
    assert cfg.fc_atr_stop_mult == pytest.approx(1.5)
    assert cfg.fc_atr_tp_mult == pytest.approx(4.0)
    assert cfg.fc_max_atr_pct == pytest.approx(0.12)
    assert cfg.fc_max_hold_days == 3
    # sigma + multi-day triggers
    assert cfg.fc_use_sigma_threshold
    assert cfg.fc_sigma_mult == pytest.approx(2.5)
    assert cfg.fc_enable_3d_trigger
    assert cfg.fc_enable_7d_trigger
    # Portfolio (div: max_concurrent 5->10)
    assert cfg.max_concurrent_positions == 10
    assert cfg.cooldown_days == 7
    assert cfg.entry_delay_hours == 1
    assert cfg.max_position_weight == pytest.approx(0.30)
    assert cfg.sizing == "vol_parity"
    # div risk-engineering (promoted 2026-05-30): de-risk-only volatility targeting
    assert cfg.enable_vol_target
    assert cfg.vol_target_annual == pytest.approx(0.60)
    assert cfg.vol_target_max_scale == pytest.approx(1.25)  # volup125, operator-promoted 2026-06-09
    assert cfg.vol_target_min_scale == pytest.approx(0.30)


def test_demo_universe_matches_strategy() -> None:
    # div promotion (2026-05-30) widened BOTH the strategy and the live-demo universe to 50.
    # They MUST stay in sync — otherwise the live demo silently trades a different universe
    # than the validated backtest. This guard exists because exactly that drift shipped once.
    assert LongNativeDemoCycleConfig().universe_size == _v11a_long_native_config().universe_size == 50


def test_demo_default_notional_multiplier_is_research_1x() -> None:
    demo = LongNativeDemoCycleConfig()
    strategy = _v11a_long_native_config()
    assert demo.notional_multiplier == pytest.approx(1.0)
    assert target_long_order_notional_pct_equity(demo, strategy) == pytest.approx(
        strategy.gross_exposure / strategy.max_concurrent_positions
    )


def test_projected_margin_guard_rejects_unsafe_levered_full_book() -> None:
    strategy = _v11a_long_native_config()
    unsafe = LongNativeDemoCycleConfig(notional_multiplier=10.0)
    projection = projected_long_initial_margin_pct_equity(unsafe, strategy)
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(1.25)
    with pytest.raises(ValueError, match="projected full-book initial margin"):
        _validate_long_demo_config(unsafe, strategy)


def test_projected_margin_guard_allows_explicit_safe_levered_demo() -> None:
    strategy = _v11a_long_native_config()
    safe = LongNativeDemoCycleConfig(
        notional_multiplier=4.0,
        max_projected_initial_margin_pct_equity=0.50,
    )
    projection = projected_long_initial_margin_pct_equity(safe, strategy)
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.50)
    _validate_long_demo_config(safe, strategy)


def test_vol_target_scale_volup125() -> None:
    """volup125 (operator-promoted 2026-06-09): the cap is 1.25 — mild scale-UP in calm
    regimes, de-risk unchanged. Receipt: long-volup-candidate-2026-06-09.md."""
    from liquidity_migration.long_native import _vol_target_scale

    cfg = _v11a_long_native_config()  # enable_vol_target=True, annual=0.60, max=1.25, min=0.30
    assert _vol_target_scale(cfg, 0.30) == pytest.approx(1.25)  # calm -> mild lever-up, capped at 1.25
    assert _vol_target_scale(cfg, 0.60) == pytest.approx(1.0)   # at target -> 1.0
    assert _vol_target_scale(cfg, 1.20) == pytest.approx(0.5)   # storm -> de-risk to 0.5
    assert _vol_target_scale(cfg, 10.0) == pytest.approx(0.30)  # extreme -> floored at min_scale
    assert _vol_target_scale(cfg, None) == pytest.approx(1.0)   # missing rv -> neutral
    off = replace(cfg, enable_vol_target=False)
    assert _vol_target_scale(off, 1.20) == pytest.approx(1.0)   # disabled -> always 1.0


def test_strategy_profile_resolution() -> None:
    assert LONG_DEMO_STRATEGY_PROFILES == ("MultiStratV1",)
    assert _long_demo_strategy_id("MultiStratV1") == MULTI_STRAT_V1_STRATEGY_ID
    with pytest.raises(ValueError):
        _long_demo_strategy_id("nonexistent")
    cfg = _long_demo_event_config("MultiStratV1")
    assert cfg.enable_fomo_chase
    with pytest.raises(ValueError):
        _long_demo_event_config("nope")


def test_long_order_link_id_uses_long_prefix_for_ws_risk_routing() -> None:
    """ws_risk routes long-sleeve fills to the long ledger based on the
    `lm-en-l-` / `lm-ux-l-` order-link prefixes. Verify both prefixes are used."""
    entry_link = _long_order_link_id(LONG_ENTRY_LINK_PREFIX, symbol="BTCUSDT", signal_ts_ms=1700000000000)
    assert entry_link.startswith("lm-en-l-"), entry_link
    assert len(entry_link) <= 36  # Bybit order_link_id limit

    # Long exit prefix (used by _execute_long_exits via _long_risk_order_link_id)
    # has 'ux-l' base. Smoke-test format.
    from liquidity_migration.long_native_event_demo import _long_risk_order_link_id
    exit_link = _long_risk_order_link_id(LONG_EXIT_LINK_PREFIX, symbol="ETHUSDT", ts_ms=1700000000000, attempt=0)
    assert exit_link.startswith("lm-ux-l-"), exit_link
    assert len(exit_link) <= 36


def test_per_position_notional_scales_by_multiplier() -> None:
    strategy = _v11a_long_native_config()
    # Owner pick: 10x multiplier
    demo_10x = LongNativeDemoCycleConfig(notional_multiplier=10.0)
    base_per_position = strategy.gross_exposure / strategy.max_concurrent_positions
    assert target_long_order_notional_pct_equity(demo_10x, strategy) == pytest.approx(base_per_position * 10.0)
    # 5x = research peak
    demo_5x = LongNativeDemoCycleConfig(notional_multiplier=5.0)
    assert target_long_order_notional_pct_equity(demo_5x, strategy) == pytest.approx(base_per_position * 5.0)
    # Explicit override wins
    demo_override = LongNativeDemoCycleConfig(max_order_notional_pct_equity=0.5)
    assert target_long_order_notional_pct_equity(demo_override, strategy) == pytest.approx(0.5)


def test_vol_parity_weight_floors_and_clamps() -> None:
    # Low realized vol → upper bound from max_position_weight / notional_weight
    w_low = _vol_parity_weight(realized_vol=0.10, vol_floor=0.30, max_position_weight=0.30, notional_weight=0.20)
    # vol_used = max(0.10, 0.30) = 0.30 → vol_floor/vol_used = 1.0; max_pos_w/notional_w = 1.5 → min = 1.0
    assert w_low == pytest.approx(1.0)
    # High realized vol → inverse-vol weight
    w_high = _vol_parity_weight(realized_vol=1.5, vol_floor=0.30, max_position_weight=0.30, notional_weight=0.20)
    # vol_floor/vol_used = 0.30/1.5 = 0.20 → min(0.20, 1.5) = 0.20; max(0.20, 0.25) = 0.25
    assert w_high == pytest.approx(0.25)


def _build_features_with_fc_signal(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal features row that passes detect_pattern_fomo_chase."""
    # FC requires: in_universe, regime_on, eth_regime_on, today_volume_rank <= 10,
    # log_return >= 2.5 * sigma_daily, close_location >= 0.7, atr_14d_pct <= 0.12
    return pl.DataFrame([
        {
            "ts_ms": signal_ts_ms,
            "symbol": symbol,
            "close": signal_close,
            "in_universe": True,
            "regime_on": True,
            "eth_regime_on": True,
            "today_volume_rank": 5,
            "log_return": math.log(1.0 + 0.20),  # 20% day
            "close_location": 0.85,
            "atr_14d_pct": 0.05,
            "sigma_daily_30d": 0.05,  # 2.5*0.05 = 0.125 threshold, 0.20 > 0.125 → trigger
            "pump_3d_log": 0.10,
            "pump_7d_log": 0.20,
            "close_loc_3d": 0.7,
            "close_loc_7d": 0.7,
            "intra_max_Nh_pump_log": 0.0,
            "realized_vol": 0.6,
            "coin_30d_return": 0.5,
            "coin_60d_return": 0.5,
            "coin_fc_sma": None,
            "btc_high_proximity": 0.5,
            "btc_sma_dist": 0.05,
            "vol_vs_30d_median": 2.0,
            "own_pump_quantile_90d": 0.10,
            "own_atr_quantile_90d": 0.10,
            "atr_20d": 5.0,
        }
    ])


def test_sniper_retrace_enters_when_live_price_reaches_threshold() -> None:
    """signal_close=100, retrace_threshold=99 (1% below), live_price=98.5
    → entry fires with reason='sniper_retrace'."""
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000  # not too far in past
    now = signal_ts + 2 * MS_PER_HOUR  # 2h after signal, well inside 6h window
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    klines = pl.DataFrame()  # not used by _select_long_entry_candidates directly
    all_trades = pl.DataFrame()
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=klines, all_trades=all_trades, now_ms=now,
        strategy=strategy, price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["symbol"] == "BTCUSDT"
    assert cand["entry_reason"] == "sniper_retrace"
    assert cand["signal_close"] == pytest.approx(100.0)
    assert cand["retrace_threshold"] == pytest.approx(99.0)


def test_sniper_falls_through_after_deadline_when_no_retrace() -> None:
    """signal_close=100, live_price=100.5 (no retrace), now>deadline
    → entry fires with reason='sniper_deadline_fallthru'."""
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    # Past 6h deadline but within 24h freshness bound
    now = signal_ts + 8 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="ETHUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=pl.DataFrame(),
        now_ms=now, strategy=strategy,
        price_by_symbol={"ETHUSDT": 100.5},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    assert candidates[0]["entry_reason"] == "sniper_deadline_fallthru"


def test_long_candidates_rank_before_max_new_entries_truncation() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    low = _build_features_with_fc_signal(symbol="LOWUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    high = _build_features_with_fc_signal(symbol="HIGHUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    low = low.with_columns([
        pl.lit(math.log1p(0.16)).alias("log_return"),
        pl.lit(10).alias("today_volume_rank"),
    ])
    high = high.with_columns([
        pl.lit(math.log1p(0.30)).alias("log_return"),
        pl.lit(1).alias("today_volume_rank"),
    ])
    features = pl.concat([low, high], how="vertical_relaxed")
    candidates, skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"LOWUSDT": 98.5, "HIGHUSDT": 98.5},
        max_new_entries=1,
    )
    assert skips["no_retrace_yet"] == 0
    assert [c["symbol"] for c in candidates] == ["HIGHUSDT"]


def test_sniper_waits_when_within_window_and_no_retrace() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 3 * MS_PER_HOUR  # inside window, no retrace yet
    features = _build_features_with_fc_signal(symbol="SOLUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=pl.DataFrame(),
        now_ms=now, strategy=strategy,
        price_by_symbol={"SOLUSDT": 100.5},  # no retrace
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_retrace_yet"] == 1


def test_stale_signal_beyond_24h_is_dropped() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 36 * MS_PER_HOUR  # past 24h freshness bound
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=pl.DataFrame(),
        now_ms=now, strategy=strategy, price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_signal"] == 1


def test_cooldown_blocks_re_entry() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    # Recently-closed trade within cooldown
    recent_exit = now - 1 * MS_PER_DAY  # 1 day ago, well inside 7-day cooldown
    all_trades = pl.DataFrame([
        {
            "trade_id": "old", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "closed", "exit_ts_ms": recent_exit, "qty": "0.001",
        }
    ])
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=all_trades,
        now_ms=now, strategy=strategy, price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["cooldown"] == 1


def test_open_position_blocks_re_entry() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    all_trades = pl.DataFrame([
        {
            "trade_id": "open-1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001",
        }
    ])
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=all_trades,
        now_ms=now, strategy=strategy, price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["already_open"] == 1


def test_plan_time_stop_exits_only_for_expired_long_positions() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame([
        {  # Past planned_exit_ts_ms → eligible
            "trade_id": "expired-1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001",
            "planned_exit_ts_ms": now - 1 * MS_PER_HOUR,
        },
        {  # Future planned_exit_ts_ms → not eligible
            "trade_id": "live-1", "sleeve": "long", "symbol": "ETHUSDT", "side": "long",
            "status": "open", "qty": "0.01",
            "planned_exit_ts_ms": now + 24 * MS_PER_HOUR,
        },
        {  # Already has live exit order pending → skipped
            "trade_id": "exiting-1", "sleeve": "long", "symbol": "SOLUSDT", "side": "long",
            "status": "open", "qty": "1.0",
            "planned_exit_ts_ms": now - 1 * MS_PER_HOUR,
        },
    ])
    plans = _plan_time_stop_exits(trades, now_ms=now, live_exit_order_symbols={"SOLUSDT"})
    assert [p["symbol"] for p in plans] == ["BTCUSDT"]
    assert plans[0]["exit_reason"] == "time_stop"


def test_long_dry_run_exit_marks_to_live_price_not_flat() -> None:
    """Paper/dry-run long exits must mark to the live ticker price, not record a FLAT
    0% close at the entry price (long-sleeve-2). Without a live price threaded in, the
    long paper shadow booked 0% on every time-stop and was useless as a forward arbiter."""
    from liquidity_migration.long_native_event_demo import _execute_long_exits

    now = 2_000_000_000_000
    all_trades = pl.DataFrame([
        {
            "trade_id": "t1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001", "entry_price": 100.0,
            "notional_usdt": 1_000.0, "equity_usdt": 10_000.0,
            "planned_exit_ts_ms": now - MS_PER_HOUR,
        },
    ])
    plan = {"trade_id": "t1", "symbol": "BTCUSDT", "qty": "0.001", "exit_reason": "time_stop"}
    demo = LongNativeDemoCycleConfig(submit_orders=False)

    rows, _orders = _execute_long_exits(
        [plan], all_trades, trading_client=None, demo=demo, now_ms=now,
        price_by_symbol={"BTCUSDT": 110.0},  # live price +10%
    )
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    assert float(rows[0]["exit_price"]) == pytest.approx(110.0)
    assert float(rows[0]["gross_trade_return"]) == pytest.approx(0.10, abs=1e-6)
    assert float(rows[0]["net_return"]) != 0.0, "paper exit must not record FLAT PnL"

    # With NO live price available it falls back to the entry price (the old flat behavior).
    rows_flat, _ = _execute_long_exits(
        [plan], all_trades, trading_client=None, demo=demo, now_ms=now, price_by_symbol={},
    )
    assert float(rows_flat[0]["gross_trade_return"]) == pytest.approx(0.0)


def test_long_entry_excludes_incomplete_today_bar() -> None:
    """long-sleeve-1: the current, still-forming daily bar has a FUTURE day-END ts and must NOT be
    eligible — firing FC on a not-yet-closed bar is look-ahead vs the backtest's closed-bar signal."""
    from liquidity_migration.long_native import LongNativeConfig
    from liquidity_migration.long_native_event_demo import _select_long_entry_candidates

    now = 1_700_000_000_000
    # ONLY a future-ts (today, still forming) bar -> its day-END ts is in the future -> not eligible.
    future_bar = pl.DataFrame([{"symbol": "BTCUSDT", "ts_ms": now + MS_PER_HOUR, "close": 100.0}])
    candidates, skips = _select_long_entry_candidates(
        features=future_bar, klines=pl.DataFrame(), all_trades=pl.DataFrame(), now_ms=now,
        strategy=LongNativeConfig(), price_by_symbol={"BTCUSDT": 90.0}, max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_signal"] == 1  # the only bar's day-END ts is in the future -> excluded


def test_pending_entry_dedupe_skips_in_flight_orders() -> None:
    now = 1_700_000_000_000
    candidates = [
        {"trade_id": "t1", "symbol": "BTCUSDT", "signal_ts_ms": now},
        {"trade_id": "t2", "symbol": "ETHUSDT", "signal_ts_ms": now},
    ]
    orders = pl.DataFrame([
        {
            "order_link_id": "lm-en-l-BTC-x", "trade_id": "t1", "symbol": "BTCUSDT",
            "reduce_only": False, "status": "submitted", "ts_ms": now - 60_000,
        }
    ])
    kept, skipped = _filter_pending_long_entries(candidates, orders, now_ms=now)
    assert {c["symbol"] for c in kept} == {"ETHUSDT"}
    assert skipped == 1


def test_open_long_trades_filter_excludes_short_and_closed() -> None:
    trades = pl.DataFrame([
        {"trade_id": "l-open", "sleeve": "long", "symbol": "BTCUSDT", "side": "long", "status": "open"},
        {"trade_id": "l-closed", "sleeve": "long", "symbol": "ETHUSDT", "side": "long", "status": "closed"},
        {"trade_id": "s-open", "sleeve": "short", "symbol": "AAVEUSDT", "side": "short", "status": "open"},
    ])
    result = _open_long_trades(trades)
    assert result["trade_id"].to_list() == ["l-open"]


def test_telegram_notify_quiet_when_no_material_event() -> None:
    payload: dict[str, Any] = {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "submit",
            "equity_usdt": 10_000.0,
            "entries_executed": 0,
            "exits_executed": 0,
            "entry_candidates": 0,
            "exit_candidates": 0,
            "open_long_positions_after": 0,
            "order_notional_pct_equity": 2.0,
            "entry_leverage": 10.0,
            "notional_multiplier": 10.0,
        },
        "entries": [],
        "exits": [],
        "entry_orders": [],
        "exit_orders": [],
        "ledger_position_summary": {},
    }
    sent, reason = _maybe_long_notify(payload, enabled=True)
    assert sent is False
    assert reason == "quiet_no_material_event"


def test_telegram_notify_reason_classification() -> None:
    cycle = {"ts_ms": 1, "mode": "submit", "equity_usdt": 100.0,
             "entries_executed": 0, "exits_executed": 0,
             "entry_candidates": 0, "exit_candidates": 0,
             "order_notional_pct_equity": 0.0, "entry_leverage": 0.0}
    # Entry executed
    assert _long_telegram_reason({"cycle": {**cycle, "entries_executed": 1}, "entry_orders": [], "exit_orders": []}) == "long_entry_executed"
    # Entry error
    assert _long_telegram_reason({"cycle": cycle, "entry_orders": [{"submit_mode": "error"}], "exit_orders": []}) == "long_entry_error"
    # Position report error
    assert _long_telegram_reason({"cycle": {**cycle, "position_report_error": "rest down"}, "entry_orders": [], "exit_orders": []}) == "position_report_error"


def test_format_long_telegram_message_contains_essentials() -> None:
    payload = {
        "cycle": {
            "ts_ms": 1_700_000_000_000, "mode": "submit", "equity_usdt": 10_000.0,
            "entries_executed": 1, "exits_executed": 0,
            "entry_candidates": 1, "exit_candidates": 0,
            "open_long_positions_after": 1,
            "order_notional_pct_equity": 2.0,
            "entry_leverage": 10.0, "notional_multiplier": 10.0,
        },
        "entries": [{
            "symbol": "BTCUSDT", "qty": 0.001, "entry_price": 50000.0,
            "entry_reason": "sniper_retrace", "stop_price": 47500.0, "take_profit_price": 60000.0,
        }],
        "exits": [],
        "ledger_position_summary": {"unrealized_pnl_usdt": 0.0, "pnl_pct": 0.0},
    }
    text = format_long_telegram_status_message(payload, reason="long_entry_executed")
    assert "MultiStratV1" in text
    assert "BTCUSDT" in text
    assert "sniper_retrace" in text
    assert "10×" in text or "10x" in text or "x10" in text or "x" in text  # multiplier marker present


def test_combined_book_summary_reads_every_live_sleeve(tmp_path: Path) -> None:
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    continuous_root = tmp_path / "continuous"
    continuous_paper_root = tmp_path / "continuous-paper"
    hedge_root = tmp_path / "hedge"
    short_root.mkdir()
    long_root.mkdir()
    continuous_root.mkdir()
    continuous_paper_root.mkdir()
    hedge_root.mkdir()
    # Short ledger: 1 closed winning short trade
    write_dataset(
        pl.DataFrame([{
            "trade_id": "s1", "sleeve": "short", "symbol": "AAAUSDT", "side": "short",
            "status": "closed", "qty": 1.0, "entry_price": 100.0, "exit_price": 90.0,
        }]),
        short_root, "event_demo_trades", partition_by=(),
    )
    # Long ledger: 1 open long trade
    write_dataset(
        pl.DataFrame([{
            "trade_id": "l1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": 0.001, "entry_price": 50_000.0,
        }]),
        long_root, LONG_DEMO_TRADES_DATASET, partition_by=(),
    )
    write_dataset(
        pl.DataFrame([{
            "trade_id": "c1", "sleeve": "continuous", "symbol": "ETHUSDT", "side": "short",
            "status": "open", "qty": 2.0, "entry_price": 2_000.0,
        }]),
        continuous_root, "continuous_fade_demo_trades", partition_by=(),
    )
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_700_000_000_000 - 60_000, "mode": "submit"}]),
        continuous_root, "continuous_fade_demo_cycles", partition_by=(),
    )
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_700_000_000_000 - 120_000, "mode": "dry_run"}]),
        continuous_paper_root, "continuous_fade_paper_cycles", partition_by=(),
    )
    text = format_combined_book_summary(
        short_root=short_root,
        long_root=long_root,
        continuous_root=continuous_root,
        continuous_paper_root=continuous_paper_root,
        continuous_hedge_root=hedge_root,
        now_ms=1_700_000_000_000,
        sleeve_states={
            "SHORT_SLEEVE": "off",
            "LONG_SLEEVE": "off",
            "CONTINUOUS_SLEEVE": "on",
            "CONTINUOUS_PAPER_SLEEVE": "on",
        },
    )
    assert "Combined book" in text
    assert "Live sleeves" in text
    assert "Continuous demo (ON)" in text
    assert "Short (OFF)" in text
    assert "Long (OFF)" in text
    assert "BTC hedge (DRY-RUN)" in text
    assert "Continuous paper (ON)" in text
    assert "Action: No action needed." in text
    # Short realized PnL: (100 - 90) * 1 = 10
    assert "$10.00" in text
    # Long open notional: 0.001 * 50_000 = 50
    assert "$50.00" in text
    # Continuous open notional: 2 * 2_000 = 4,000
    assert "$4,000.00" in text
    assert "trades=0" not in text


def test_combined_book_summary_fails_open_on_missing_roots(tmp_path: Path) -> None:
    # Missing roots/datasets should not raise — aggregate reports must
    # never break the cron job
    text = format_combined_book_summary(
        short_root=tmp_path / "no-such-short",
        long_root=tmp_path / "no-such-long",
        continuous_root=tmp_path / "no-such-continuous",
        continuous_paper_root=tmp_path / "no-such-continuous-paper",
        continuous_hedge_root=tmp_path / "no-such-hedge",
        now_ms=1_700_000_000_000,
    )
    assert "Combined book" in text
    assert "no ledger yet" in text
    assert "Action:" in text


def test_long_demo_cycle_summary_includes_key_fields() -> None:
    payload = {
        "cycle": {
            "cycle_id": "abc", "mode": "submit", "strategy_profile": "MultiStratV1",
            "symbols": 10, "feature_rows": 100,
            "entries_executed": 1, "entry_candidates": 1,
            "exits_executed": 0, "exit_candidates": 0,
            "open_long_positions_after": 1, "equity_usdt": 10_000.0,
            "cycle_elapsed_pre_persist_ms": 500.0,
        }
    }
    text = format_long_demo_cycle_summary(payload)
    assert "long-native event demo cycle" in text
    assert "MultiStratV1" in text
    assert "entries=1/1" in text


def test_long_kline_universe_fetcher_scopes_to_top_n_by_turnover() -> None:
    """Long daemon's kline manager must NOT bootstrap all 567 USDT-perps.

    The long sleeve only trades the top-10 by 24h turnover; scoping the
    kline universe to the top-50 keeps memory under the systemd cap (was
    OOM-killing at 1G with the full universe) while leaving 5x rank-shift
    headroom. Anything beyond the top-50 falls back to per-cycle REST.
    """
    from liquidity_migration.long_native_event_demo_daemon import (
        _LONG_KLINE_UNIVERSE_SIZE,
        _build_long_kline_universe,
    )

    class _FakeMarket:
        def get_tickers(self) -> list[dict]:
            rows: list[dict] = []
            for i in range(200):
                rows.append({"symbol": f"SYM{i:03d}USDT", "turnover24h": str(1_000_000 - i)})
            # Non-USDT pair — must be excluded.
            rows.append({"symbol": "BTC-PERP", "turnover24h": "999"})
            # Zero turnover + null turnover — must be excluded.
            rows.append({"symbol": "DEADUSDT", "turnover24h": "0"})
            rows.append({"symbol": "NULLUSDT", "turnover24h": None})
            return rows

    symbols = _build_long_kline_universe(_FakeMarket())
    assert len(symbols) == _LONG_KLINE_UNIVERSE_SIZE
    assert symbols[0] == "SYM000USDT"
    assert symbols[-1] == f"SYM{_LONG_KLINE_UNIVERSE_SIZE - 1:03d}USDT"
    assert "BTC-PERP" not in symbols
    assert "DEADUSDT" not in symbols
    assert "NULLUSDT" not in symbols


def test_long_kline_universe_fetcher_returns_empty_on_rest_failure() -> None:
    """Universe fetch errors must not crash the manager — empty list lets
    the manager bootstrap nothing and the cycle's REST fallback supplies
    everything that day."""
    from liquidity_migration.long_native_event_demo_daemon import (
        _build_long_kline_universe,
    )

    class _FailingMarket:
        def get_tickers(self) -> list[dict]:
            raise RuntimeError("simulated REST outage")

    assert _build_long_kline_universe(_FailingMarket()) == []


def test_compute_long_order_sizing_matches_inline_vol_target_block() -> None:
    """long-sleeve-9: the extracted ``_compute_long_order_sizing`` helper must reproduce the
    prior inline block byte-for-byte — base per-position notional * the de-risk-only vol-target
    scalar keyed on the LATEST non-null ``btc_rv_30`` (after sorting by ts_ms)."""
    from liquidity_migration.long_native import _vol_target_scale
    from liquidity_migration.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    demo = LongNativeDemoCycleConfig()
    strategy = _long_demo_event_config(demo.strategy_profile)
    # ts_ms out of order with an interleaved null — the helper must sort then take the last non-null.
    features = pl.DataFrame({"ts_ms": [3, 1, 2], "btc_rv_30": [0.9, None, 0.4]})
    notional, scale = _compute_long_order_sizing(demo=demo, strategy=strategy, features=features)
    expected_scale = _vol_target_scale(strategy, 0.9)  # latest by ts_ms (ts=3)
    assert scale == expected_scale
    assert notional == pytest.approx(target_long_order_notional_pct_equity(demo, strategy) * expected_scale)

    # No btc_rv_30 column -> latest_btc_rv is None -> the None vol-target path (no de-risk-up).
    bare = pl.DataFrame({"ts_ms": [1, 2]})
    n0, s0 = _compute_long_order_sizing(demo=demo, strategy=strategy, features=bare)
    assert s0 == _vol_target_scale(strategy, None)
    assert n0 == pytest.approx(target_long_order_notional_pct_equity(demo, strategy) * s0)


def test_median_universe_selection_steady_state_is_byte_match_noop() -> None:
    """ls-4: in steady state (every name has a finite 90d-median) the helper re-selects
    the SAME top-N-by-median set that build_long_features already wrote to in_universe, so
    it is a no-op byte-match (fallback_count == 0). This is the consistency guarantee with
    the backtest's own universe selection."""
    from liquidity_migration.long_native_event_demo import _apply_median_universe_selection

    now = 1_700_000_000_000
    prev = now - 86_400_000
    # 5 names on the latest bar, finite medians 50>40>30>20>10. build_long_features would set
    # in_universe = top-3 by median = {s50, s40, s30}. Pre-set it that way; the helper must agree.
    rows = []
    for sym, med, tq, inu in [("s50", 50.0, 1.0, True), ("s40", 40.0, 1.0, True),
                              ("s30", 30.0, 1.0, True), ("s20", 20.0, 9.0, False),
                              ("s10", 10.0, 9.0, False)]:
        rows.append({"ts_ms": now, "symbol": sym, "turnover_median_90d": med,
                     "turnover_quote": tq, "in_universe": inu})
        rows.append({"ts_ms": prev, "symbol": sym, "turnover_median_90d": med,
                     "turnover_quote": tq, "in_universe": True})  # historical bar (must be untouched)
    feat = pl.DataFrame(rows)
    out, fallback = _apply_median_universe_selection(feat, universe_size=3, snapshot_ts_ms=now)
    assert fallback == 0
    latest = out.filter(pl.col("ts_ms") == now).sort("symbol")
    got = dict(zip(latest["symbol"].to_list(), latest["in_universe"].to_list()))
    assert got == {"s50": True, "s40": True, "s30": True, "s20": False, "s10": False}
    # historical bar in_universe is preserved (the helper only rewrites the latest bar)
    hist = out.filter(pl.col("ts_ms") == prev)
    assert all(hist["in_universe"].to_list())


def test_median_universe_selection_cold_start_backfills_by_24h() -> None:
    """ls-4 cold start: when fewer than N names have a finite median (warm-up, <90 daily
    bars), the remainder is backfilled by 24h turnover so the book is never zeroed, and the
    backfill count is surfaced (universe_fallback_24h > 0)."""
    from liquidity_migration.long_native_event_demo import _apply_median_universe_selection

    now = 1_700_000_000_000
    rows = [
        {"ts_ms": now, "symbol": "fin1", "turnover_median_90d": 50.0, "turnover_quote": 1.0, "in_universe": True},
        {"ts_ms": now, "symbol": "fin2", "turnover_median_90d": 40.0, "turnover_quote": 1.0, "in_universe": True},
        # null medians (cold) — must NOT count as finite; 24h fallback ranks them by turnover_quote
        {"ts_ms": now, "symbol": "cold_hi", "turnover_median_90d": None, "turnover_quote": 100.0, "in_universe": False},
        {"ts_ms": now, "symbol": "cold_lo", "turnover_median_90d": None, "turnover_quote": 10.0, "in_universe": False},
    ]
    feat = pl.DataFrame(rows, infer_schema_length=None)
    out, fallback = _apply_median_universe_selection(feat, universe_size=3, snapshot_ts_ms=now)
    assert fallback == 1  # one 24h backfill (need 3, only 2 finite)
    latest = out.filter(pl.col("ts_ms") == now).sort("symbol")
    got = dict(zip(latest["symbol"].to_list(), latest["in_universe"].to_list()))
    # the 2 finite + the highest-24h cold name; the low-24h cold name stays out
    assert got == {"fin1": True, "fin2": True, "cold_hi": True, "cold_lo": False}


def test_median_universe_selection_noop_without_median_column() -> None:
    """ls-4: a features frame lacking turnover_median_90d (degenerate) is returned unchanged
    with fallback 0 — never crashes the cycle."""
    from liquidity_migration.long_native_event_demo import _apply_median_universe_selection

    feat = pl.DataFrame([{"ts_ms": 1, "symbol": "x", "turnover_quote": 1.0, "in_universe": True}])
    out, fallback = _apply_median_universe_selection(feat, universe_size=3, snapshot_ts_ms=1)
    assert fallback == 0 and out.equals(feat)
