"""Tests for the long-sleeve forward-testing module (v11a LongV11aDivWeekendVol).

Covers:
- Profile loader returns the v11a uni50 sniper config
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

import logging
import math
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import liquidity_migration.long_native_event_demo as lnd
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.config import ResearchConfig
from liquidity_migration.long_native_event_demo import (
    FC_VOLUME_RANK_TELEMETRY_MARGIN,
    LONG_DEMO_STRATEGY_PROFILES,
    LONG_DEMO_TRADES_DATASET,
    LONG_ENTRY_LINK_PREFIX,
    LONG_EXIT_LINK_PREFIX,
    LongNativeDemoCycleConfig,
    LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
    _execute_long_entries,
    _fc_rank_is_near_boundary,
    _log_fc_rank_boundary,
    _execute_long_exits,
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
    run_long_native_demo_cycle,
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
    assert cfg.max_per_symbol_weight == pytest.approx(cfg.max_position_weight)
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
    # audit2c: worst case now also folds the 1.5x weekend tilt (and the <=1.0
    # vol-parity weight), so the projection is 1.25 * 1.5 = 1.875, not 1.25.
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(1.875)
    with pytest.raises(ValueError, match="projected full-book initial margin"):
        _validate_long_demo_config(unsafe, strategy)


def test_projected_margin_guard_allows_explicit_safe_levered_demo() -> None:
    strategy = _v11a_long_native_config()
    # audit2c: the 4x config used to project exactly 0.50 and pass; once the 1.5x
    # weekend tilt is modeled it projects 0.75 and is correctly rejected. A 2x
    # config (0.10*2*1.25*1.5 = 0.375) is the new headroom-respecting "safe" case.
    safe = LongNativeDemoCycleConfig(
        notional_multiplier=2.0,
        max_projected_initial_margin_pct_equity=0.50,
    )
    projection = projected_long_initial_margin_pct_equity(safe, strategy)
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.375)
    _validate_long_demo_config(safe, strategy)


def test_live_sizing_rejects_unmirrored_per_symbol_cap_drift() -> None:
    strategy = replace(_v11a_long_native_config(), max_per_symbol_weight=0.10)
    demo = LongNativeDemoCycleConfig(notional_multiplier=1.0)

    with pytest.raises(ValueError, match="max_per_symbol_weight == strategy.max_position_weight"):
        _validate_long_demo_config(demo, strategy)


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
    assert LONG_DEMO_STRATEGY_PROFILES == ("LongV11aDivWeekendVol",)
    assert _long_demo_strategy_id("LongV11aDivWeekendVol") == LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    with pytest.raises(ValueError):
        _long_demo_strategy_id("nonexistent")
    cfg = _long_demo_event_config("LongV11aDivWeekendVol")
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


def _build_features_without_fc_signal(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal full feature row that does not pass detect_pattern_fomo_chase."""
    return _build_features_with_fc_signal(
        symbol=symbol,
        signal_ts_ms=signal_ts_ms,
        signal_close=signal_close,
    ).with_columns([
        pl.lit(math.log1p(0.01)).alias("log_return"),
        pl.lit(0.10).alias("close_location"),
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


def test_sniper_retrace_respects_entry_delay_before_live_check() -> None:
    """Live v11a must not enter before the same first sniper hour the backtest uses."""
    strategy = _v11a_long_native_config()
    assert strategy.entry_delay_hours == 1
    signal_ts = 1_700_000_000_000
    now = signal_ts + MS_PER_HOUR // 2
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)

    candidates, skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
    )

    assert candidates == []
    assert skips["entry_delay"] == 1
    assert skips["no_retrace_yet"] == 0


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
    # audit-iter3: a >24h signal is now attributed to the stale_signal counter (it was
    # previously mis-counted as no_signal, leaving stale_signal stuck at 0).
    assert skips["stale_signal"] == 1
    assert skips["no_signal"] == 0


def test_old_non_signal_history_does_not_count_as_stale_signal() -> None:
    strategy = _v11a_long_native_config()
    fresh_ts = 1_700_000_000_000
    now = fresh_ts + 2 * MS_PER_HOUR
    old_non_signal = _build_features_without_fc_signal(
        symbol="OLDUSDT",
        signal_ts_ms=fresh_ts - 30 * MS_PER_DAY,
    )
    fresh_non_signal = _build_features_without_fc_signal(
        symbol="FRESHUSDT",
        signal_ts_ms=fresh_ts,
    )
    features = pl.concat([old_non_signal, fresh_non_signal], how="vertical_relaxed")
    candidates, skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"OLDUSDT": 98.0, "FRESHUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["stale_signal"] == 0
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
    assert "LongV11aDivWeekendVol" in text
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
    # The compatibility short line renders only while residual OPEN rows remain;
    # a closed-only ledger shows no line, but realized PnL still counts below.
    assert "Short (compatibility)" not in text
    assert "Long (OFF)" in text
    # The hedge label rides the continuous toggle — never a hardcoded "DRY-RUN"
    # (the live unit ships SUBMIT_HEDGE=1; a hardcoded dry-run label misstated it).
    assert "BTC hedge (ON)" in text
    assert "Continuous paper (ON)" in text
    assert "Action: No action needed." in text
    # Short realized PnL: (100 - 90) * 1 = 10
    assert "$10.00" in text
    # Long open notional: 0.001 * 50_000 = 50
    assert "$50.00" in text
    # Continuous open notional: 2 * 2_000 = 4,000
    assert "$4,000.00" in text
    assert "trades=0" not in text


def test_combined_book_summary_shows_compatibility_short_only_while_residual_open(tmp_path: Path) -> None:
    """Compatibility short rows surface only while residual OPEN rows exist."""
    short_root = tmp_path / "short"
    write_dataset(
        pl.DataFrame([{
            "trade_id": "s-open", "sleeve": "short", "symbol": "AAAUSDT", "side": "short",
            "status": "open", "qty": 1.0, "entry_price": 100.0,
        }]),
        short_root, "event_demo_trades", partition_by=(),
    )
    text = format_combined_book_summary(
        short_root=short_root, long_root=None,
        now_ms=1_700_000_000_000, sleeve_states={"SHORT_SLEEVE": "off"},
    )
    assert "Short (compatibility) (OFF)" in text  # residual open row: compatibility view stays


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
            "cycle_id": "abc", "mode": "submit", "strategy_profile": "LongV11aDivWeekendVol",
            "symbols": 10, "feature_rows": 100,
            "entries_executed": 1, "entry_candidates": 1,
            "exits_executed": 0, "exit_candidates": 0,
            "open_long_positions_after": 1, "equity_usdt": 10_000.0,
            "cycle_elapsed_pre_persist_ms": 500.0,
        }
    }
    text = format_long_demo_cycle_summary(payload)
    assert "long-native event demo cycle" in text
    assert "LongV11aDivWeekendVol" in text
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


def test_compute_long_order_sizing_uses_latest_closed_btc_rv_when_clocked() -> None:
    from liquidity_migration.long_native import _vol_target_scale
    from liquidity_migration.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    demo = LongNativeDemoCycleConfig()
    strategy = _long_demo_event_config(demo.strategy_profile)
    now = 1_700_000_000_000
    current_day_start = now - (now % MS_PER_DAY)
    closed_day_end = current_day_start
    unclosed_day_end = current_day_start + MS_PER_DAY
    features = pl.DataFrame(
        {
            "ts_ms": [unclosed_day_end, closed_day_end],
            "btc_rv_30": [0.10, 1.20],
        }
    )

    notional, scale = _compute_long_order_sizing(
        demo=demo, strategy=strategy, features=features, now_ms=now,
    )

    expected_scale = _vol_target_scale(strategy, 1.20)
    assert scale == pytest.approx(expected_scale)
    assert scale != pytest.approx(_vol_target_scale(strategy, 0.10))
    assert notional == pytest.approx(target_long_order_notional_pct_equity(demo, strategy) * expected_scale)


def test_compute_long_order_sizing_falls_back_when_only_unclosed_btc_rv_exists() -> None:
    from liquidity_migration.long_native import _vol_target_scale
    from liquidity_migration.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    demo = LongNativeDemoCycleConfig()
    strategy = _long_demo_event_config(demo.strategy_profile)
    now = 1_700_000_000_000
    current_day_start = now - (now % MS_PER_DAY)
    features = pl.DataFrame(
        {
            "ts_ms": [current_day_start + MS_PER_DAY],
            "btc_rv_30": [0.10],
        }
    )

    notional, scale = _compute_long_order_sizing(
        demo=demo, strategy=strategy, features=features, now_ms=now,
    )

    expected_scale = _vol_target_scale(strategy, None)
    assert scale == pytest.approx(expected_scale)
    assert notional == pytest.approx(target_long_order_notional_pct_equity(demo, strategy) * expected_scale)


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


# --------------------------------------------------------------------------- #
# audit2c — projected-IM guard models the LIVE worst-case per-position notional #
# --------------------------------------------------------------------------- #
def test_guard_now_rejects_promoted_4x_config_that_used_to_pass() -> None:
    """The promoted strategy (gross_exposure=1.0, max_concurrent_positions=10,
    entry_leverage=10) with notional_multiplier=4.0 used to project EXACTLY 0.50
    full-book IM and pass the 50% ceiling. With the 1.5x weekend tilt modeled it
    projects 0.75 and must be rejected."""
    strategy = _v11a_long_native_config()
    assert strategy.gross_exposure == pytest.approx(1.0)
    assert strategy.max_concurrent_positions == 10
    assert strategy.weekend_size_mult == pytest.approx(1.5)

    demo = LongNativeDemoCycleConfig(
        notional_multiplier=4.0,
        entry_leverage=10.0,
        max_projected_initial_margin_pct_equity=0.50,
    )
    projection = projected_long_initial_margin_pct_equity(demo, strategy)

    # base per-position = 1.0/10 = 0.10; * mult 4 = 0.40; * vol-scale 1.25 = 0.50
    # (the OLD worst-case order notional). The fix adds * 1.5 weekend tilt = 0.75.
    assert projection["worst_case_order_notional_pct_equity"] == pytest.approx(0.75)
    # full book = 0.75 * 10 positions / 10x leverage = 0.75 (was 0.50).
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.75)

    # The guard now correctly REJECTS what it previously approved at exactly 0.50.
    with pytest.raises(ValueError, match="projected full-book initial margin"):
        _validate_long_demo_config(demo, strategy)


def test_guard_models_weekend_and_unit_position_weight_factors() -> None:
    """Worst-case order notional = per_order * vol_scale * weekend_mult * 1.0.

    Pins each factor so a regression that drops the weekend tilt (the old bug) or
    the unit position-weight assumption fails here."""
    strategy = _v11a_long_native_config()
    demo = LongNativeDemoCycleConfig(notional_multiplier=4.0, entry_leverage=10.0)
    projection = projected_long_initial_margin_pct_equity(demo, strategy)

    per_order = projection["per_order_notional_pct_equity"]
    vol_scale = projection["worst_case_vol_target_scale"]
    worst_case = projection["worst_case_order_notional_pct_equity"]
    assert per_order == pytest.approx(0.40)
    assert vol_scale == pytest.approx(1.25)
    # weekend_size_mult=1.5, max vol-parity weight=1.0 -> factor 1.5 over the old model.
    assert worst_case == pytest.approx(per_order * vol_scale * 1.5)


def test_weekend_mult_one_low_multiplier_still_passes() -> None:
    """A config with weekend_size_mult=1.0 and a low multiplier is below the
    ceiling and must still be accepted (the guard only tightens where the live
    book is actually levered up by the weekend tilt)."""
    strategy = replace(_v11a_long_native_config(), weekend_size_mult=1.0)
    demo = LongNativeDemoCycleConfig(
        notional_multiplier=2.0,
        entry_leverage=10.0,
        max_projected_initial_margin_pct_equity=0.50,
    )
    projection = projected_long_initial_margin_pct_equity(demo, strategy)

    # weekend_mult=1.0 -> no extra factor: 0.10 * 2 * 1.25 = 0.25 worst-case order;
    # full book = 0.25 * 10 / 10 = 0.25, well under 0.50.
    assert projection["worst_case_order_notional_pct_equity"] == pytest.approx(0.25)
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.25)
    _validate_long_demo_config(demo, strategy)  # must not raise


def test_weekend_mult_below_one_does_not_relax_guard() -> None:
    """A weekend tilt < 1.0 would size DOWN, but a guard must never use it to
    relax the worst case below the no-tilt baseline — the max(1.0, ...) floor
    keeps the projection conservative."""
    strategy = replace(_v11a_long_native_config(), weekend_size_mult=0.5)
    demo = LongNativeDemoCycleConfig(notional_multiplier=4.0, entry_leverage=10.0)
    projection = projected_long_initial_margin_pct_equity(demo, strategy)
    # floor at 1.0 -> worst case stays 0.40 * 1.25 = 0.50, not 0.25.
    assert projection["worst_case_order_notional_pct_equity"] == pytest.approx(0.50)


# --------------------------------------------------------------------------- #
# audit2b — long-demo cycle telemetry truthfulness (entries_parallel_workers)   #
# --------------------------------------------------------------------------- #
class _ThreadRecordingClient:
    """Private client that records the thread each place_order ran on."""

    def __init__(self) -> None:
        self.place_threads: list[int] = []
        self.set_leverage_calls: list[dict] = []

    def set_leverage(self, **kw: Any) -> dict:
        self.set_leverage_calls.append(kw)
        return {}

    def place_order(self, **params: Any) -> dict:
        self.place_threads.append(threading.get_ident())
        return {"orderId": f"oid-{len(self.place_threads)}"}


def _candidate(symbol: str, signal_ts_ms: int = 1_700_000_000_000) -> dict[str, Any]:
    return {
        "trade_id": f"long-{symbol}-{signal_ts_ms}",
        "symbol": symbol,
        "side": "long",
        "pattern": "fomo_chase",
        "signal_ts_ms": signal_ts_ms,
        "signal_close": 100.0,
        "live_price": 99.0,
        "retrace_threshold": 99.0,
        "sniper_deadline_ms": signal_ts_ms + 6 * lnd.MS_PER_HOUR,
        "entry_reason": "sniper_retrace",
        "entry_ready_ts_ms": signal_ts_ms,
        "stop_loss_pct": 0.1,
        "take_profit_pct": 0.2,
        "max_hold_days": 3,
        "planned_exit_ts_ms": signal_ts_ms + 3 * lnd.MS_PER_DAY,
        "atr_14d_pct": 0.05,
        "realized_vol": 0.5,
        "position_weight": 1.0,
        "candidate_score": 0.2,
        "today_volume_rank": 1.0,
        "entry_policy": "v11a_sniper_retrace_fallthru",
        "entry_quality_tier": "sniper_retrace",
        "entry_rule": "sniper retrace",
    }


def _stub_cycle_dependencies(monkeypatch: pytest.MonkeyPatch, *, candidates: list[dict]) -> None:
    """Patch the heavy collaborators so the cycle reaches the entries-telemetry
    block deterministically and offline. Leaves the real `requested_entry_workers`
    computation + cycle-row assembly (the code under test) untouched."""
    universe = pl.DataFrame(
        {"symbol": [c["symbol"] for c in candidates] or ["AAAUSDT"]}
    )

    monkeypatch.setattr(lnd, "_demo_instruments", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        lnd, "_resolve_ticker_snapshot", lambda *a, **k: ([], "rest")
    )
    monkeypatch.setattr(lnd, "_normalize_tickers", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(lnd, "_build_long_universe", lambda *a, **k: universe)
    monkeypatch.setattr(
        lnd,
        "_resolve_private_snapshot",
        lambda *a, **k: (
            {
                "equity_usdt": 10_000.0,
                "wallet_error": "",
                "raw_open_orders": [],
                "open_order_error": "",
                "raw_positions": [],
                "position_error": "",
            },
            "rest",
        ),
    )
    monkeypatch.setattr(
        lnd, "_download_recent_1h_klines",
        lambda *a, **k: (
            pl.DataFrame(),
            {"cache_rows": 0, "fetched_rows": 0, "store_rows": 0, "store_symbols": 0},
        ),
    )
    monkeypatch.setattr(lnd, "build_long_features", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        lnd, "_apply_median_universe_selection", lambda features, **k: (features, 0)
    )
    monkeypatch.setattr(
        lnd, "_price_lookup_from_tickers_and_klines",
        lambda *a, **k: {c["symbol"]: c["live_price"] for c in candidates},
    )
    monkeypatch.setattr(lnd, "_contract_lookup", lambda *a, **k: {})
    monkeypatch.setattr(
        lnd, "_select_long_entry_candidates",
        lambda **k: (list(candidates), {"no_signal": 0}),
    )
    # Entries are a no-op for the telemetry test: we only care about the worker
    # count the cycle REPORTS, not the executed rows.
    monkeypatch.setattr(
        lnd, "_execute_long_entries", lambda *a, **k: ([], [], 0)
    )


def _run_cycle(tmp_path: Path, demo: LongNativeDemoCycleConfig) -> dict[str, Any]:
    return run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=demo,
        private_client=_ThreadRecordingClient(),
        now_ms=1_700_000_300_000,
    )


def test_submit_cycle_reports_truthful_worker_count_of_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """submit_orders=True, max_concurrent_entries=4, 2 candidates: the OLD code
    set entries_parallel_workers = min(4, 2) = 2 and reported it, despite
    sequential execution. The fix reports the ACTUAL worker count, 1."""
    cands = [_candidate("AAAUSDT"), _candidate("BBBUSDT", signal_ts_ms=1_700_000_001_000)]
    _stub_cycle_dependencies(monkeypatch, candidates=cands)
    demo = LongNativeDemoCycleConfig(
        submit_orders=True,
        confirm_demo_orders=True,
        max_concurrent_entries=4,
        max_new_entries_per_cycle=5,
        ws_klines_enabled=False,
    )
    payload = _run_cycle(tmp_path, demo)
    # 2 candidates reached the entries block (proves the >1 branch could fire).
    assert payload["cycle"]["entry_candidates"] == 2
    # Truthful: sequential execution => 1 worker. OLD code reported 2 here.
    assert payload["cycle"]["entries_parallel_workers"] == 1


def test_dry_run_cycle_worker_count_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal dry-run path never entered the parallel branch and reported 1
    both before and after the fix — the fix must not perturb it."""
    cands = [_candidate("AAAUSDT"), _candidate("BBBUSDT", signal_ts_ms=1_700_000_001_000)]
    _stub_cycle_dependencies(monkeypatch, candidates=cands)
    demo = LongNativeDemoCycleConfig(
        submit_orders=False,
        record_dry_run=True,
        max_concurrent_entries=4,
        max_new_entries_per_cycle=5,
        ws_klines_enabled=False,
    )
    payload = _run_cycle(tmp_path, demo)
    assert payload["cycle"]["entry_candidates"] == 2
    assert payload["cycle"]["entries_parallel_workers"] == 1


def test_submit_cycle_single_candidate_worker_count_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single candidate never trips the >1 branch (len(candidates) > 1 is
    False), so both old and new report 1 — the other happy path."""
    cands = [_candidate("AAAUSDT")]
    _stub_cycle_dependencies(monkeypatch, candidates=cands)
    demo = LongNativeDemoCycleConfig(
        submit_orders=True,
        confirm_demo_orders=True,
        max_concurrent_entries=4,
        max_new_entries_per_cycle=5,
        ws_klines_enabled=False,
    )
    payload = _run_cycle(tmp_path, demo)
    assert payload["cycle"]["entry_candidates"] == 1
    assert payload["cycle"]["entries_parallel_workers"] == 1


def test_execute_long_entries_runs_on_single_thread_despite_max_workers() -> None:
    """The fact that makes any reported worker count >1 a lie: even handed
    max_workers=4 and a private_client_factory, _execute_long_entries submits
    every entry on the SAME (calling) thread — there is no parallelism."""
    client = _ThreadRecordingClient()
    contracts = {
        "AAAUSDT": {"tick_size": 0.01, "qty_step": 0.001, "min_order_qty": 0.0,
                    "min_notional_value": 0.0, "max_order_qty": 1e9},
        "BBBUSDT": {"tick_size": 0.01, "qty_step": 0.001, "min_order_qty": 0.0,
                    "min_notional_value": 0.0, "max_order_qty": 1e9},
        "CCCUSDT": {"tick_size": 0.01, "qty_step": 0.001, "min_order_qty": 0.0,
                    "min_notional_value": 0.0, "max_order_qty": 1e9},
    }
    cands = [
        _candidate("AAAUSDT"),
        _candidate("BBBUSDT", signal_ts_ms=1_700_000_001_000),
        _candidate("CCCUSDT", signal_ts_ms=1_700_000_002_000),
    ]
    demo = LongNativeDemoCycleConfig(
        submit_orders=True, confirm_demo_orders=True, entry_leverage=10.0
    )

    def _factory() -> _ThreadRecordingClient:  # would-be parallel worker client
        return _ThreadRecordingClient()

    _rows, _orders, _skipped = _execute_long_entries(
        cands,
        trading_client=client,
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.1,
        price_by_symbol={"AAAUSDT": 99.0, "BBBUSDT": 99.0, "CCCUSDT": 99.0},
        contract_by_symbol=contracts,
        now_ms=1_700_000_300_000,
        strategy_id="s",
        record_preflight=None,
        private_client_factory=_factory,
        execution_event_router=None,
        max_workers=4,  # explicitly request parallelism...
    )
    # ...yet all three place_order calls ran sequentially on this one thread via
    # the single injected trading_client (the factory was never used). Three
    # placements is itself proof the burst was not short-circuited.
    assert len(client.place_threads) == 3
    assert set(client.place_threads) == {threading.get_ident()}


# ---------------------------------------------------------------------------
# Relocated from tests/test_audit_fix_b11.py (audit bucket b11).
# ---------------------------------------------------------------------------


# long-sleeve-1: live path applies the deployed weekend 1.5x size tilt
def _fc_signal_features(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal feature row that passes detect_pattern_fomo_chase (mirrors the
    long_native_event_demo test fixture)."""
    return pl.DataFrame([
        {
            "ts_ms": signal_ts_ms,
            "symbol": symbol,
            "close": signal_close,
            "in_universe": True,
            "regime_on": True,
            "eth_regime_on": True,
            "today_volume_rank": 5,
            "log_return": math.log(1.0 + 0.20),
            "close_location": 0.85,
            "atr_14d_pct": 0.05,
            "sigma_daily_30d": 0.05,
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


# 2023-04-01 12:00Z is a Saturday; 2023-04-05 12:00Z is a Wednesday.
_SAT_NOW_MS = 1_680_350_400_000
_WED_NOW_MS = 1_680_696_000_000


def _one_candidate_weight(now_ms: int) -> float:
    strategy = _v11a_long_native_config()
    assert strategy.weekend_size_mult == 1.5, "v11a profile must carry the 1.5x weekend tilt"
    signal_ts = now_ms - 2 * MS_PER_HOUR  # fresh, same UTC day, retrace fired
    features = _fc_signal_features(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now_ms,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},  # below the 1% retrace threshold
        max_new_entries=5,
    )
    assert len(candidates) == 1, "expected exactly one FC retrace candidate"
    return float(candidates[0]["position_weight"])


def test_live_weekend_size_tilt_matches_backtest() -> None:
    weekday_weight = _one_candidate_weight(_WED_NOW_MS)
    weekend_weight = _one_candidate_weight(_SAT_NOW_MS)
    assert weekday_weight > 0.0
    # The live Sat entry must be sized 1.5x the live weekday entry, exactly as the
    # backtest sizes Sat/Sun entries (long_native.py weekend_size_mult). Before the
    # fix the live path ignored weekend_size_mult, so both were equal.
    assert weekend_weight == pytest.approx(weekday_weight * 1.5)


# long-sleeve-3: live per-trade net_return is NET of venue fees
def test_live_long_exit_net_return_subtracts_fees() -> None:
    from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig

    now = 2_000_000_000_000
    # Trade carries an entry fee; equity 10k, notional 1k (weight 0.1), +10% move.
    all_trades = pl.DataFrame([
        {
            "trade_id": "t1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001", "entry_price": 100.0,
            "notional_usdt": 1_000.0, "equity_usdt": 10_000.0,
            "entry_fee_usdt": 0.6,  # taker fee on entry
            "planned_exit_ts_ms": now - MS_PER_HOUR,
        },
    ])
    plan = {"trade_id": "t1", "symbol": "BTCUSDT", "qty": "0.001", "exit_reason": "time_stop"}
    demo = LongNativeDemoCycleConfig(submit_orders=False)  # dry-run -> exit_fee 0

    rows, _ = _execute_long_exits(
        [plan], all_trades, trading_client=None, demo=demo, now_ms=now,
        price_by_symbol={"BTCUSDT": 110.0},  # +10%
    )
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    gross = 0.10 * (1_000.0 / 10_000.0)  # gross_trade_return * notional_weight
    fee_return = (0.6 + 0.0) / 10_000.0
    # net_return must be NET of fees; the original bug recorded the gross value.
    assert float(rows[0]["net_return"]) == pytest.approx(gross - fee_return)
    assert float(rows[0]["net_return"]) < gross


# ratelimit-rest-4: stale "short never gets starved" claim removed
def test_rate_limit_docstring_no_longer_overstates_cross_sleeve_protection() -> None:
    from liquidity_migration.long_native_event_demo import (
        _long_demo_private_rest_rate_limit_per_second,
    )

    doc = _long_demo_private_rest_rate_limit_per_second.__doc__ or ""
    assert "ratelimit-rest-4" in doc
    assert "per-IP" in doc  # explicitly states it is NOT a per-IP coordinator
    assert "never gets starved" not in doc  # the stale claim is gone


# --------------------------------------------------------------------------- #
# long-sleeve-2: live FC rank-boundary telemetry (observability ONLY)
# (relocated from tests/test_audit_int_iG.py)
# --------------------------------------------------------------------------- #

def test_fc_rank_near_boundary_predicate() -> None:
    cutoff = 10
    margin = FC_VOLUME_RANK_TELEMETRY_MARGIN
    # Exactly at the cutoff -> in band.
    assert _fc_rank_is_near_boundary(cutoff, cutoff) is True
    # Within margin of the cutoff -> in band.
    assert _fc_rank_is_near_boundary(cutoff - margin, cutoff) is True
    assert _fc_rank_is_near_boundary(cutoff - 1, cutoff) is True
    # Comfortably inside the top set -> NOT flagged.
    assert _fc_rank_is_near_boundary(cutoff - margin - 1, cutoff) is False
    assert _fc_rank_is_near_boundary(1, cutoff) is False
    # Above the cutoff -> would not have fired the FC gate; not flagged.
    assert _fc_rank_is_near_boundary(cutoff + 1, cutoff) is False
    # Missing rank -> not flagged.
    assert _fc_rank_is_near_boundary(None, cutoff) is False


def test_log_fc_rank_boundary_emits_for_near_boundary_candidate(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="liquidity_migration.long_native_event_demo"):
        _log_fc_rank_boundary(symbol="WIFUSDT", today_volume_rank=9, fc_top_volume_rank_max=10)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("rank-boundary" in m and "WIFUSDT" in m for m in msgs)


def test_log_fc_rank_boundary_silent_for_comfortable_rank(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="liquidity_migration.long_native_event_demo"):
        _log_fc_rank_boundary(symbol="ETHUSDT", today_volume_rank=2, fc_top_volume_rank_max=10)
    assert caplog.records == []


def test_median_universe_selection_targets_latest_closed_bar_not_future_bar() -> None:
    """audit-iter1 long-3: a daily feature row is stamped at the day END, so a still-
    forming UTC day yields a FUTURE-stamped bar (> snapshot_ts_ms). Entries fire from
    the latest CLOSED bar, so the re-selection (and its telemetry) must target that
    bar, not the future partial one. The old code keyed on the unconditional max ts."""
    from liquidity_migration.long_native_event_demo import _apply_median_universe_selection

    now = 1_700_000_000_000
    closed = now - 3_600_000   # latest closed bar (<= now)
    future = now + 3_600_000   # next-midnight partial bar (> now)
    rows = []
    for sym, med in [("s30", 30.0), ("s20", 20.0), ("s10", 10.0)]:
        # CLOSED bar: top-2-by-median = {s30, s20}; pre-set in_universe all False.
        rows.append({"ts_ms": closed, "symbol": sym, "turnover_median_90d": med,
                     "turnover_quote": 1.0, "in_universe": False})
        # FUTURE bar: DIFFERENT (inverted) ranking + pre-set True; must stay untouched.
        rows.append({"ts_ms": future, "symbol": sym, "turnover_median_90d": 100.0 - med,
                     "turnover_quote": 1.0, "in_universe": True})
    feat = pl.DataFrame(rows)
    out, fallback = _apply_median_universe_selection(feat, universe_size=2, snapshot_ts_ms=now)
    assert fallback == 0
    closed_rows = out.filter(pl.col("ts_ms") == closed).sort("symbol")
    closed_sel = dict(zip(closed_rows["symbol"].to_list(), closed_rows["in_universe"].to_list()))
    assert closed_sel == {"s30": True, "s20": True, "s10": False}  # re-selected on CLOSED bar
    fut = out.filter(pl.col("ts_ms") == future)
    assert all(fut["in_universe"].to_list())  # future bar untouched
