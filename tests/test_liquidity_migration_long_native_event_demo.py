"""Tests for the long-sleeve forward-testing module (v11a LongV11aDivWeekendVol).

Covers:
- Profile loader returns the v11a uni50 sniper config
- Per-position notional sizing scales by notional_multiplier × base
- Sniper retrace candidate selection enters when live price reaches threshold
  AND falls through after deadline expires
- Cooldown prevents same-symbol re-entry within cooldown_days
- Time-stop exit plans publish zero component targets
- Demo and paper cycles publish only through the account owner inbox
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import liquidity_migration.long_native_event_demo as lnd
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms
from liquidity_migration.account_route import (
    AccountRoute,
    AccountRouteMismatchError,
    ensure_account_route,
)
from liquidity_migration.config import ResearchConfig
from liquidity_migration.long_native_event_demo import (
    FC_VOLUME_RANK_TELEMETRY_MARGIN,
    LONG_DEMO_STRATEGY_PROFILES,
    LongNativeDemoCycleConfig,
    LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
    _fc_rank_is_near_boundary,
    _log_fc_rank_boundary,
    _count_long_target_reservations,
    _long_demo_event_config,
    _long_demo_strategy_id,
    _open_long_trades,
    _plan_time_stop_exits,
    _select_long_entry_candidates,
    _validate_long_demo_config,
    _v11a_long_native_config,
    _vol_parity_weight,
    format_long_demo_cycle_summary,
    projected_long_initial_margin_pct_equity,
    run_long_native_demo_cycle,
    target_long_order_notional_pct_equity,
)
from liquidity_migration.strategy_target_replay import PublishedTargetCyclePayload


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
        execution_environment="demo",
        account_intent_inbox_root="inbox",
        account_execution_root="account",
    )
    projection = projected_long_initial_margin_pct_equity(safe, strategy)
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.375)
    _validate_long_demo_config(safe, strategy)


@pytest.mark.parametrize("missing_root", [None, "", "   "])
def test_submit_mode_requires_account_owner_route(
    tmp_path: Path,
    missing_root: str | None,
) -> None:
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=missing_root,
        account_execution_root=missing_root,
    )

    with pytest.raises(ValueError, match="direct sleeve order authority is retired"):
        run_long_native_demo_cycle(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=demo,
        )


def test_long_config_has_no_direct_execution_or_telegram_fields() -> None:
    retired = {
        "fallback_equity_usdt",
        "entry_order_type",
        "exit_order_type",
        "order_fill_confirm_seconds",
        "order_fill_poll_interval_seconds",
        "order_fill_fast_poll_interval_seconds",
        "order_fill_fast_poll_seconds",
        "telegram",
        "record_dry_run",
        "account_type",
        "settle_coin",
    }
    assert retired.isdisjoint(LongNativeDemoCycleConfig.__dataclass_fields__)
    assert not hasattr(lnd, "_execute_long_entries")
    assert not hasattr(lnd, "_execute_long_exits")
    assert not hasattr(lnd, "_resolve_private_snapshot")


def test_long_cycle_refuses_local_dry_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="execution_environment"):
        run_long_native_demo_cycle(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=LongNativeDemoCycleConfig(),
        )


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
    assert _vol_target_scale(cfg, 0.60) == pytest.approx(1.0)  # at target -> 1.0
    assert _vol_target_scale(cfg, 1.20) == pytest.approx(0.5)  # storm -> de-risk to 0.5
    assert _vol_target_scale(cfg, 10.0) == pytest.approx(0.30)  # extreme -> floored at min_scale
    assert _vol_target_scale(cfg, None) == pytest.approx(1.0)  # missing rv -> neutral
    off = replace(cfg, enable_vol_target=False)
    assert _vol_target_scale(off, 1.20) == pytest.approx(1.0)  # disabled -> always 1.0


def test_strategy_profile_resolution() -> None:
    assert LONG_DEMO_STRATEGY_PROFILES == ("LongV11aDivWeekendVol",)
    assert _long_demo_strategy_id("LongV11aDivWeekendVol") == LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    with pytest.raises(ValueError):
        _long_demo_strategy_id("nonexistent")
    cfg = _long_demo_event_config("LongV11aDivWeekendVol")
    assert cfg.enable_fomo_chase
    with pytest.raises(ValueError):
        _long_demo_event_config("nope")


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
    return pl.DataFrame(
        [
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
        ]
    )


def _build_features_without_fc_signal(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal full feature row that does not pass detect_pattern_fomo_chase."""
    return _build_features_with_fc_signal(
        symbol=symbol,
        signal_ts_ms=signal_ts_ms,
        signal_close=signal_close,
    ).with_columns(
        [
            pl.lit(math.log1p(0.01)).alias("log_return"),
            pl.lit(0.10).alias("close_location"),
        ]
    )


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
        features=features,
        klines=klines,
        all_trades=all_trades,
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
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


def test_sniper_entry_delay_uses_exact_elapsed_hours() -> None:
    strategy = replace(_v11a_long_native_config(), entry_delay_hours=1.5)
    signal_ts = 1_700_000_123_456
    delay_ms = exact_duration_ms(hours=1.5)
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)

    early, early_skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=signal_ts + delay_ms - 1,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
    )
    on_boundary, boundary_skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=signal_ts + delay_ms,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
    )

    assert early == []
    assert early_skips["entry_delay"] == 1
    assert len(on_boundary) == 1
    assert on_boundary[0]["first_entry_check_ts_ms"] == signal_ts + delay_ms
    assert boundary_skips["entry_delay"] == 0


def test_long_cooldown_until_uses_exact_elapsed_days() -> None:
    exit_ts = 1_700_000_123_456
    trades = pl.DataFrame(
        [
            {"symbol": "BTCUSDT", "status": "closed", "exit_ts_ms": exit_ts},
        ]
    )

    cooldown = lnd._cooldown_until_long(trades, cooldown_days=7)

    assert cooldown == {"BTCUSDT": exit_ts + exact_duration_ms(days=7)}


def test_sniper_falls_through_after_deadline_when_no_retrace() -> None:
    """signal_close=100, live_price=100.5 (no retrace), now>deadline
    → entry fires with reason='sniper_deadline_fallthru'."""
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    # Past 6h deadline but within 24h freshness bound
    now = signal_ts + 8 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="ETHUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
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
    low = low.with_columns(
        [
            pl.lit(math.log1p(0.16)).alias("log_return"),
            pl.lit(10).alias("today_volume_rank"),
        ]
    )
    high = high.with_columns(
        [
            pl.lit(math.log1p(0.30)).alias("log_return"),
            pl.lit(1).alias("today_volume_rank"),
        ]
    )
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
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
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
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
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
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "old",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "closed",
                "exit_ts_ms": recent_exit,
                "qty": "0.001",
            }
        ]
    )
    candidates, skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=all_trades,
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["cooldown"] == 1


def test_open_position_blocks_re_entry() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "open-1",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
            }
        ]
    )
    candidates, skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=all_trades,
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["already_open"] == 1


@pytest.mark.parametrize("target_action", ["open_or_resize", "close"])
def test_pending_long_target_reserves_entry_but_is_not_exit_or_pnl_open(
    target_action: str,
) -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(
        symbol="BTCUSDT",
        signal_ts_ms=signal_ts,
        signal_close=100.0,
    )
    pending = pl.DataFrame([
        {
            "trade_id": "pending-1",
            "sleeve": "long",
            "strategy_id": "strategy",
            "symbol": "BTCUSDT",
            "side": "long",
            "status": "target_pending",
            "target_action": target_action,
            "qty": "0.001",
            "planned_exit_ts_ms": now - MS_PER_HOUR,
        }
    ])

    candidates, skips = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pending,
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )

    assert candidates == []
    assert skips["already_open"] == 1
    assert _count_long_target_reservations(pending) == 1
    assert _open_long_trades(pending).is_empty()
    assert _plan_time_stop_exits(
        pending,
        now_ms=now,
    ) == []


def test_plan_time_stop_exits_only_for_expired_long_positions() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            {  # Past planned_exit_ts_ms → eligible
                "trade_id": "expired-1",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
                "planned_exit_ts_ms": now - 1 * MS_PER_HOUR,
            },
            {  # Future planned_exit_ts_ms → not eligible
                "trade_id": "live-1",
                "sleeve": "long",
                "symbol": "ETHUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.01",
                "planned_exit_ts_ms": now + 24 * MS_PER_HOUR,
            },
        ]
    )
    plans = _plan_time_stop_exits(trades, now_ms=now)
    assert [p["symbol"] for p in plans] == ["BTCUSDT"]
    assert plans[0]["exit_reason"] == "time_stop"


def test_long_entry_excludes_incomplete_today_bar() -> None:
    """long-sleeve-1: the current, still-forming daily bar has a FUTURE day-END ts and must NOT be
    eligible — firing FC on a not-yet-closed bar is look-ahead vs the backtest's closed-bar signal."""
    from liquidity_migration.long_native import LongNativeConfig
    from liquidity_migration.long_native_event_demo import _select_long_entry_candidates

    now = 1_700_000_000_000
    # ONLY a future-ts (today, still forming) bar -> its day-END ts is in the future -> not eligible.
    future_bar = pl.DataFrame([{"symbol": "BTCUSDT", "ts_ms": now + MS_PER_HOUR, "close": 100.0}])
    candidates, skips = _select_long_entry_candidates(
        features=future_bar,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=LongNativeConfig(),
        price_by_symbol={"BTCUSDT": 90.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_signal"] == 1  # the only bar's day-END ts is in the future -> excluded


def test_open_long_trades_filter_excludes_short_and_closed() -> None:
    trades = pl.DataFrame(
        [
            {"trade_id": "l-open", "sleeve": "long", "symbol": "BTCUSDT", "side": "long", "status": "open"},
            {"trade_id": "l-closed", "sleeve": "long", "symbol": "ETHUSDT", "side": "long", "status": "closed"},
            {"trade_id": "s-open", "sleeve": "short", "symbol": "AAVEUSDT", "side": "short", "status": "open"},
        ]
    )
    result = _open_long_trades(trades)
    assert result["trade_id"].to_list() == ["l-open"]


def test_long_demo_cycle_summary_includes_key_fields() -> None:
    payload = {
        "cycle": {
            "cycle_id": "abc",
            "mode": "demo_target",
            "strategy_profile": "LongV11aDivWeekendVol",
            "symbols": 10,
            "feature_rows": 100,
            "entry_candidates": 1,
            "entry_targets_queued": 1,
            "exit_candidates": 0,
            "exit_targets_queued": 0,
            "open_long_components": 1,
            "equity_usdt": 10_000.0,
            "account_owner_health_error": "",
            "cycle_elapsed_pre_persist_ms": 500.0,
        }
    }
    text = format_long_demo_cycle_summary(payload)
    assert "long target producer" in text
    assert "LongV11aDivWeekendVol" in text
    assert "targets=entry:1 exit:0" in text


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
        demo=demo,
        strategy=strategy,
        features=features,
        now_ms=now,
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
        demo=demo,
        strategy=strategy,
        features=features,
        now_ms=now,
    )

    expected_scale = _vol_target_scale(strategy, None)
    assert scale == pytest.approx(expected_scale)
    assert notional == pytest.approx(target_long_order_notional_pct_equity(demo, strategy) * expected_scale)


def test_long_entry_and_exit_adapters_share_stable_component_target_key() -> None:
    now_ms = 1_700_000_000_000
    strategy_id = "long-v11a"
    candidate = {
        "trade_id": "long-trade-1",
        "symbol": "ABCUSDT",
        "signal_ts_ms": now_ms - 60_000,
        "entry_reason": "fomo_chase",
        "position_weight": 0.5,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.08,
        "max_hold_days": 3,
    }
    demo = LongNativeDemoCycleConfig(entry_leverage=10.0)

    entries = lnd._long_entry_target_intents(
        [candidate],
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.10,
        price_by_symbol={"ABCUSDT": 2.0},
        now_ms=now_ms,
        strategy_id=strategy_id,
    )
    assert len(entries) == 1
    entry = entries[0].intent
    # Raw strategy notional is preserved. Venue step/minimum decisions happen
    # later in the account kernel using verified demo rules.
    assert entry.signed_notional_usdt == 500.0
    assert entry.leverage == 10.0
    assert entry.metadata["quantity_authority"] == "account_kernel_demo_rules"
    assert entry.metadata["entry_attempt_key"] == f"entry-attempt/{entry.target_key}"
    assert entry.metadata["signal_valid_until_ms"] == (
        candidate["signal_ts_ms"] + lnd.SIGNAL_FRESHNESS_MS
    )
    assert entry.metadata["stop_loss_pct"] == pytest.approx(0.03)
    assert entry.metadata["take_profit_pct"] == pytest.approx(0.08)
    assert entry.metadata["max_hold_duration_ms"] == 3 * lnd.MS_PER_DAY
    assert {
        "stop_price",
        "take_profit_price",
        "planned_exit_ts_ms",
    }.isdisjoint(entry.metadata)

    trades = pl.DataFrame(
        [
            {
                "trade_id": "long-trade-1",
                "symbol": "ABCUSDT",
                "entry_leverage": 10.0,
                "planned_exit_ts_ms": now_ms,
            }
        ]
    )
    exits = lnd._long_exit_target_intents(
        [{"trade_id": "long-trade-1", "symbol": "ABCUSDT", "exit_reason": "time_stop"}],
        trades,
        strategy_id=strategy_id,
        now_ms=now_ms + 1,
        default_leverage=10.0,
    )
    assert len(exits) == 1
    exit_intent = exits[0].intent
    assert exit_intent.signed_notional_usdt == 0.0
    assert exit_intent.target_key == entry.target_key
    assert exit_intent.reason == "time_stop"


def test_registered_long_profile_carries_live_kernel_identity_and_leverage() -> None:
    strategy = _long_demo_event_config("LongV11aDivWeekendVol")
    assert strategy.execution_strategy_id == LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    assert strategy.execution_leverage == 10.0


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
    for sym, med, tq, inu in [
        ("s50", 50.0, 1.0, True),
        ("s40", 40.0, 1.0, True),
        ("s30", 30.0, 1.0, True),
        ("s20", 20.0, 9.0, False),
        ("s10", 10.0, 9.0, False),
    ]:
        rows.append({"ts_ms": now, "symbol": sym, "turnover_median_90d": med, "turnover_quote": tq, "in_universe": inu})
        rows.append(
            {"ts_ms": prev, "symbol": sym, "turnover_median_90d": med, "turnover_quote": tq, "in_universe": True}
        )  # historical bar (must be untouched)
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
        execution_environment="demo",
        account_intent_inbox_root="inbox",
        account_execution_root="account",
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
# Account-target cycle fixtures                                                #
# --------------------------------------------------------------------------- #
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
    """Patch public-market collaborators so target publication is offline."""
    universe = pl.DataFrame({"symbol": [c["symbol"] for c in candidates] or ["AAAUSDT"]})

    monkeypatch.setattr(lnd, "_demo_instruments", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(lnd, "_resolve_ticker_snapshot", lambda *a, **k: ([], "rest"))
    monkeypatch.setattr(lnd, "_normalize_tickers", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(lnd, "_build_long_universe", lambda *a, **k: universe)
    monkeypatch.setattr(
        lnd,
        "_download_recent_1h_klines",
        lambda *a, **k: (
            pl.DataFrame(),
            {"cache_rows": 0, "fetched_rows": 0, "store_rows": 0, "store_symbols": 0},
        ),
    )
    monkeypatch.setattr(lnd, "build_long_features", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(lnd, "_apply_median_universe_selection", lambda features, **k: (features, 0))
    monkeypatch.setattr(
        lnd,
        "_price_lookup_from_tickers_and_klines",
        lambda *a, **k: {c["symbol"]: c["live_price"] for c in candidates},
    )
    monkeypatch.setattr(
        lnd,
        "_select_long_entry_candidates",
        lambda **k: (list(candidates), {"no_signal": 0}),
    )


def _run_cycle(tmp_path: Path, demo: LongNativeDemoCycleConfig) -> dict[str, Any]:
    return run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=demo,
        now_ms=1_700_000_300_000,
    )


def _ensure_owner_route(
    account_root: Path,
    inbox_root: Path,
    *,
    environment: str,
) -> AccountRoute:
    account_id = (
        "bybit-demo-unified" if environment == "demo" else "bybit-paper-unified"
    )
    return ensure_account_route(
        account_id=account_id,
        environment=environment,
        account_root=account_root,
        inbox_root=inbox_root,
    )


def _write_owner_health(
    account_root: Path,
    inbox_root: Path,
    *,
    environment: str,
    equity_usdt: float = 10_000.0,
    now_ms: int = 1_700_000_300_000,
) -> None:
    from liquidity_migration.account_kernel import read_account_journal
    from liquidity_migration.account_owner_health import (
        TEST_ACCOUNT_OWNER_INVOCATION_ID,
        AccountOwnerHealth,
        write_account_owner_health,
    )

    route = _ensure_owner_route(
        account_root,
        inbox_root,
        environment=environment,
    )
    journal = read_account_journal(account_root, verify=True)
    write_account_owner_health(
        account_root,
        AccountOwnerHealth(
            owner="account_execution",
            environment=environment,
            account_id=route.account_id,
            status="healthy",
            observed_ts_ns=now_ms * 1_000_000,
            loop_sequence=1,
            journal_sequence=journal[-1].sequence if journal else 0,
            journal_state_hash=journal[-1].state_hash if journal else "0" * 64,
            equity_usdt=equity_usdt,
            available_margin_usdt=equity_usdt,
            requested_symbols_ready=True,
            invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        ),
    )


def test_submit_cycle_with_account_inbox_never_calls_direct_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate("AAAUSDT")
    _stub_cycle_dependencies(monkeypatch, candidates=[candidate])
    inbox = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    _write_owner_health(
        account_root,
        inbox,
        environment="demo",
        equity_usdt=12_345.0,
    )

    payload = _run_cycle(tmp_path / "long", demo)

    assert type(payload) is PublishedTargetCyclePayload
    assert payload.publication.entry_request is not None
    assert payload.route.environment == "demo"
    assert payload["cycle"]["account_target_route"] is True
    assert payload["cycle"]["entry_targets_queued"] == 1
    assert payload["cycle"]["equity_usdt"] == pytest.approx(12_345.0)
    assert payload["cycle"]["account_state_source"] == "account_owner_health:demo"
    assert "entries_executed" not in payload["cycle"]
    assert "bybit_positions" not in payload
    assert payload["account_target_requests"]["entry_request"]["intent_count"] == 1
    assert payload["account_target_requests"]["exit_request_ids"] == []
    pending = list((inbox / "pending").glob("*.json"))
    assert len(pending) == 1
    request = json.loads(pending[0].read_bytes())
    intent = request["intents"][0]["intent"]
    assert intent["target_key"].startswith("long/")
    assert intent["signed_notional_usdt"] > 0.0


def test_long_producer_rejects_cross_wired_route_before_cycle_resources(
    tmp_path: Path,
) -> None:
    account_a = tmp_path / "account-a"
    inbox_a = tmp_path / "inbox-a"
    account_b = tmp_path / "account-b"
    inbox_b = tmp_path / "inbox-b"
    _ensure_owner_route(account_a, inbox_a, environment="demo")
    _ensure_owner_route(account_b, inbox_b, environment="demo")
    cycle_root = tmp_path / "long-cycle"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox_b),
        account_execution_root=str(account_a),
        ws_klines_enabled=False,
    )

    with pytest.raises(AccountRouteMismatchError, match="manifests disagree"):
        run_long_native_demo_cycle(
            cycle_root,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=demo,
            now_ms=1_700_000_300_000,
        )

    assert not cycle_root.exists()


def test_account_route_blocks_risk_increase_without_fresh_owner_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_cycle_dependencies(monkeypatch, candidates=[_candidate("AAAUSDT")])
    inbox = tmp_path / "account-inbox"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(tmp_path / "missing-account"),
        ws_klines_enabled=False,
    )
    _ensure_owner_route(
        tmp_path / "missing-account",
        inbox,
        environment="demo",
    )

    payload = _run_cycle(tmp_path / "long", demo)

    assert payload["cycle"]["entry_targets_queued"] == 0
    assert payload["cycle"]["skipped_account_owner_health"] == 1
    assert payload["cycle"]["equity_usdt"] == 0.0
    assert payload["cycle"]["account_owner_health_error"]
    assert list((inbox / "pending").glob("*.json")) == []


def test_account_route_pending_entry_is_not_republished_on_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = [_candidate("AAAUSDT")]
    _stub_cycle_dependencies(monkeypatch, candidates=candidates)
    inbox = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    _write_owner_health(account_root, inbox, environment="demo")

    first = _run_cycle(tmp_path / "long", demo)
    second = _run_cycle(tmp_path / "long", demo)

    assert first["cycle"]["entry_targets_queued"] == 1
    assert second["cycle"]["entry_targets_queued"] == 0
    assert second["cycle"]["unresolved_entry_target_suppressions"] == 1
    assert len(list((inbox / "pending").glob("*.json"))) == 1


def test_account_route_new_signal_key_remains_eligible_while_prior_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = [_candidate("AAAUSDT")]
    _stub_cycle_dependencies(monkeypatch, candidates=candidates)
    inbox = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    _write_owner_health(account_root, inbox, environment="demo")
    _run_cycle(tmp_path / "long", demo)

    candidates[:] = [_candidate("AAAUSDT", signal_ts_ms=1_700_000_060_000)]
    second = _run_cycle(tmp_path / "long", demo)

    assert second["cycle"]["entry_targets_queued"] == 1
    assert second["cycle"]["unresolved_entry_target_suppressions"] == 0
    assert len(list((inbox / "pending").glob("*.json"))) == 2


def test_account_risk_rejected_exact_entry_attempt_is_not_republished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from liquidity_migration.account_kernel import (
        AccountExecutionKernel,
        AccountRiskPolicy,
        AccountRiskSnapshot,
        DesiredTarget,
        InstrumentRules,
        MarketInputRef,
    )
    from liquidity_migration.deterministic_runtime import VirtualClock

    candidate = _candidate("AAAUSDT")
    candidates = [candidate]
    _stub_cycle_dependencies(monkeypatch, candidates=candidates)
    inbox = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    strategy_id = _long_demo_strategy_id(demo.strategy_profile)
    route = _ensure_owner_route(account_root, inbox, environment="demo")
    proposed = lnd._long_entry_target_intents(
        candidates,
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.10,
        price_by_symbol={"AAAUSDT": 99.0},
        now_ms=1_700_000_300_000,
        strategy_id=strategy_id,
    )[0].intent
    clock = VirtualClock(
        current_wall_ns=1_700_000_300_000_000_000,
        current_monotonic_ns=1,
    )
    kernel = AccountExecutionKernel(
        route.account_path,
        account_id=route.account_id,
        clock=clock,
    )
    result = kernel.submit_targets(
        batch_id="risk-rejected-entry",
        market_inputs=[MarketInputRef("book", "AAAUSDT", 1, 2, 99.0)],
        targets=[DesiredTarget(
            decision_key=proposed.decision_key,
            target_key=proposed.target_key,
            sleeve="long",
            strategy_id=proposed.strategy_id,
            component_id=proposed.component_id,
            symbol="AAAUSDT",
            signed_qty=1_000.0,
            reference_price=99.0,
            leverage=10.0,
            reason=proposed.reason,
            metadata=proposed.metadata,
        )],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 10_000.0, "wallet", 3),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 100.0, 10.0),
        instrument_rules={"AAAUSDT": InstrumentRules("AAAUSDT", 0.1, 0.1, 1.0)},
    )
    assert not result.accepted
    _write_owner_health(account_root, inbox, environment="demo")

    payload = _run_cycle(tmp_path / "long", demo)

    assert payload["cycle"]["entry_targets_queued"] == 0
    assert payload["cycle"]["terminal_entry_attempt_suppressions"] == 1
    assert list((inbox / "pending").glob("*.json")) == []


def test_service_expired_entry_receipt_suppresses_same_attempt_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from liquidity_migration.account_intent_client import AccountTargetPublisher
    from liquidity_migration.account_service import AccountServiceReceipt

    candidate = _candidate(
        "AAAUSDT",
        signal_ts_ms=1_700_000_300_000 - 25 * MS_PER_HOUR,
    )
    candidates = [candidate]
    _stub_cycle_dependencies(monkeypatch, candidates=candidates)
    inbox_root = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox_root),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    route = _ensure_owner_route(account_root, inbox_root, environment="demo")
    strategy_id = _long_demo_strategy_id(demo.strategy_profile)
    proposed = lnd._long_entry_target_intents(
        candidates,
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.10,
        price_by_symbol={"AAAUSDT": 99.0},
        now_ms=1_700_000_300_000,
        strategy_id=strategy_id,
    )[0]
    publisher = AccountTargetPublisher(route)
    published = publisher.publish(
        batch_id="expired-before-restart",
        intents=(proposed,),
        created_ts_ns=1_700_000_299_000_000_000,
    )
    claimed = publisher.inbox.claim_next()
    assert claimed is not None
    publisher.inbox.complete(
        claimed[0],
        AccountServiceReceipt(
            request_id=published.request.request_id,
            request_hash=published.request.content_hash(),
            batch_id=published.request.batch_id,
            accepted=False,
            rejection_keys=(
                "account-service:entry-signal-expired:"
                f"{proposed.intent.metadata['entry_attempt_key']}",
            ),
            command_ids=(),
            execution_event_ids=(),
            final_state_hash="0" * 64,
            disposition="expired",
        ),
    )
    _write_owner_health(account_root, inbox_root, environment="demo")

    payload = _run_cycle(tmp_path / "long", demo)

    assert payload["cycle"]["entry_targets_queued"] == 0
    assert payload["cycle"]["terminal_entry_attempt_suppressions"] == 1
    assert list((inbox_root / "pending").glob("*.json")) == []


def test_paper_cycle_uses_account_inbox_and_never_writes_idealized_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_cycle_dependencies(monkeypatch, candidates=[_candidate("AAAUSDT")])
    inbox = tmp_path / "paper-inbox"
    account_root = tmp_path / "paper-account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="paper",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    _write_owner_health(
        account_root,
        inbox,
        environment="paper",
        equity_usdt=7_654.0,
    )

    payload = _run_cycle(tmp_path / "long-paper", demo)

    assert payload["cycle"]["account_target_route"] is True
    assert payload["cycle"]["entry_targets_queued"] == 1
    assert payload["cycle"]["equity_usdt"] == pytest.approx(7_654.0)
    assert payload["cycle"]["account_state_source"] == "account_owner_health:paper"
    assert "entries_executed" not in payload["cycle"]
    assert len(list((inbox / "pending").glob("*.json"))) == 1


def _fc_signal_features(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal feature row that passes detect_pattern_fomo_chase (mirrors the
    long_native_event_demo test fixture)."""
    return pl.DataFrame(
        [
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
        ]
    )


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
    closed = now - 3_600_000  # latest closed bar (<= now)
    future = now + 3_600_000  # next-midnight partial bar (> now)
    rows = []
    for sym, med in [("s30", 30.0), ("s20", 20.0), ("s10", 10.0)]:
        # CLOSED bar: top-2-by-median = {s30, s20}; pre-set in_universe all False.
        rows.append(
            {"ts_ms": closed, "symbol": sym, "turnover_median_90d": med, "turnover_quote": 1.0, "in_universe": False}
        )
        # FUTURE bar: DIFFERENT (inverted) ranking + pre-set True; must stay untouched.
        rows.append(
            {
                "ts_ms": future,
                "symbol": sym,
                "turnover_median_90d": 100.0 - med,
                "turnover_quote": 1.0,
                "in_universe": True,
            }
        )
    feat = pl.DataFrame(rows)
    out, fallback = _apply_median_universe_selection(feat, universe_size=2, snapshot_ts_ms=now)
    assert fallback == 0
    closed_rows = out.filter(pl.col("ts_ms") == closed).sort("symbol")
    closed_sel = dict(zip(closed_rows["symbol"].to_list(), closed_rows["in_universe"].to_list()))
    assert closed_sel == {"s30": True, "s20": True, "s10": False}  # re-selected on CLOSED bar
    fut = out.filter(pl.col("ts_ms") == future)
    assert all(fut["in_universe"].to_list())  # future bar untouched
