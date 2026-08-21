"""Tests for the long-sleeve forward-testing module (v11a LongV11aDivWeekendVol).

Covers:
- Profile loader returns the v11a uni50 sniper config
- Per-position notional sizing scales by notional_multiplier × base
- Sniper retrace candidate selection enters when live price reaches threshold
  AND falls through after deadline expires
- Cooldown prevents same-symbol re-entry within cooldown_days
- Time-stop exit plans publish zero component targets
- Demo cycles publish only through the account owner inbox
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

import liquidity_migration.strategy.long_native_event_demo as lnd
import liquidity_migration.strategy.strategy_planning as planning_module


from liquidity_migration.core._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms
from liquidity_migration.account.account_route import (
    AccountRoute,
    AccountRouteMismatchError,
    ensure_account_route,
)
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.rules.long_identity import (
    LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
    LONG_V12_WIDE_STOP_STRATEGY_ID,
)
from liquidity_migration.rules.long_native import long_v11a_profile, long_v12_profile
from liquidity_migration.strategy.long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _count_long_target_reservations,
    _open_long_trades,
    _plan_time_stop_exits,
    _select_long_entry_candidates,
    _validate_long_demo_config,
    _vol_parity_weight,
    format_long_demo_cycle_summary,
    run_long_native_demo_cycle,
    target_long_order_notional_pct_equity,
)
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload


@pytest.fixture(autouse=True)
def _no_leaked_heartbeat_path() -> Any:
    """`_write_owner_health` points the producer at a tmp heartbeat; a path
    left in the environment would follow the next test into a deleted tmp dir
    and read as "the engine is down" rather than as this test's own mess."""

    yield
    os.environ.pop("ENGINE_ACCOUNT_HEARTBEAT_FILE", None)


def test_v11a_config_matches_research_run() -> None:
    cfg = long_v11a_profile()
    assert cfg.universe_size == 50
    assert cfg.fc_sniper_retrace_pct == pytest.approx(0.01)
    assert cfg.fc_sniper_deadline_hours == 6
    assert cfg.fc_atr_stop_mult == pytest.approx(1.5)
    assert cfg.fc_atr_tp_mult == pytest.approx(4.0)
    assert cfg.fc_max_atr_pct == pytest.approx(0.12)
    assert cfg.fc_max_hold_days == 3
    assert cfg.fc_sigma_mult == pytest.approx(2.5)
    assert cfg.max_concurrent_positions == 10
    assert cfg.cooldown_days == 7
    assert cfg.entry_delay_hours == 1
    assert cfg.max_position_weight == pytest.approx(0.30)
    assert cfg.vol_target_annual == pytest.approx(0.60)
    assert cfg.vol_target_max_scale == pytest.approx(1.25)
    assert cfg.vol_target_min_scale == pytest.approx(0.30)


def test_demo_default_notional_multiplier_is_research_1x() -> None:
    demo = LongNativeDemoCycleConfig()
    strategy = long_v11a_profile()
    assert demo.notional_multiplier == pytest.approx(1.0)
    assert target_long_order_notional_pct_equity(demo, strategy) == pytest.approx(
        strategy.gross_exposure / strategy.max_concurrent_positions
    )


def test_a_large_levered_multiplier_validates_without_a_margin_refusal() -> None:
    """Sizing is the owner's dial: no load-time projection refuses it.

    Per-position risk is bounded by each position's own venue-native stop;
    there is no book-level margin ceiling to trip.
    """

    strategy = long_v11a_profile()
    big = LongNativeDemoCycleConfig(
        notional_multiplier=10.0,
        execution_environment="demo",
        account_intent_inbox_root="inbox",
        account_execution_root="account",
    )
    _validate_long_demo_config(big, strategy)


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
        "strategy_profile",
        "universe_size",
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


def test_vol_target_scale_volup125() -> None:
    """volup125: the cap is 1.25 -- mild scale-up in calm regimes, de-risk unchanged."""
    from liquidity_migration.rules.long_native import _vol_target_scale

    cfg = long_v11a_profile()
    assert _vol_target_scale(cfg, 0.30) == pytest.approx(1.25)  # calm -> mild lever-up, capped at 1.25
    assert _vol_target_scale(cfg, 0.60) == pytest.approx(1.0)  # at target -> 1.0
    assert _vol_target_scale(cfg, 1.20) == pytest.approx(0.5)  # storm -> de-risk to 0.5
    assert _vol_target_scale(cfg, 10.0) == pytest.approx(0.30)  # extreme -> floored at min_scale
    assert _vol_target_scale(cfg, None) == pytest.approx(1.0)  # missing rv -> neutral


def test_per_position_notional_scales_by_multiplier() -> None:
    strategy = long_v11a_profile()
    # Owner pick: 10x multiplier
    demo_10x = LongNativeDemoCycleConfig(notional_multiplier=10.0)
    base_per_position = strategy.gross_exposure / strategy.max_concurrent_positions
    assert target_long_order_notional_pct_equity(demo_10x, strategy) == pytest.approx(base_per_position * 10.0)
    # 5x = research peak
    demo_5x = LongNativeDemoCycleConfig(notional_multiplier=5.0)
    assert target_long_order_notional_pct_equity(demo_5x, strategy) == pytest.approx(base_per_position * 5.0)
    # Explicit override wins
    demo_override = LongNativeDemoCycleConfig(order_notional_pct_equity=0.5)
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
    """signal_close=100, retrace_threshold=99 (1% below), live_price=98.5 → entry fires with reason='sniper_retrace'."""
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000  # not too far in past
    now = signal_ts + 2 * MS_PER_HOUR  # 2h after signal, well inside 6h window
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    all_trades = pl.DataFrame()
    candidates, skips = _select_long_entry_candidates(
        features=features,
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
    strategy = long_v11a_profile()
    assert strategy.entry_delay_hours == 1
    signal_ts = 1_700_000_000_000
    now = signal_ts + MS_PER_HOUR // 2
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)

    candidates, skips = _select_long_entry_candidates(
        features=features,
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
    strategy = replace(long_v11a_profile(), entry_delay_hours=1.5)
    signal_ts = 1_700_000_123_456
    delay_ms = exact_duration_ms(hours=1.5)
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)

    early, early_skips = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=signal_ts + delay_ms - 1,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
    )
    on_boundary, boundary_skips = _select_long_entry_candidates(
        features=features,
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
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    # Past 6h deadline but within 24h freshness bound
    now = signal_ts + 8 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="ETHUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"ETHUSDT": 100.5},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    assert candidates[0]["entry_reason"] == "sniper_deadline_fallthru"


def test_long_candidates_rank_before_max_new_entries_truncation() -> None:
    strategy = long_v11a_profile()
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
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"LOWUSDT": 98.5, "HIGHUSDT": 98.5},
        max_new_entries=1,
    )
    assert skips["no_retrace_yet"] == 0
    assert [c["symbol"] for c in candidates] == ["HIGHUSDT"]


def test_sniper_waits_when_within_window_and_no_retrace() -> None:
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 3 * MS_PER_HOUR  # inside window, no retrace yet
    features = _build_features_with_fc_signal(symbol="SOLUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, skips = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"SOLUSDT": 100.5},  # no retrace
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_retrace_yet"] == 1


def test_stale_signal_beyond_24h_is_dropped() -> None:
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 36 * MS_PER_HOUR  # past 24h freshness bound
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, skips = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    # A >24h signal is attributed to the stale_signal counter, not no_signal.
    assert skips["stale_signal"] == 1
    assert skips["no_signal"] == 0


def test_old_non_signal_history_does_not_count_as_stale_signal() -> None:
    strategy = long_v11a_profile()
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
    strategy = long_v11a_profile()
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
        all_trades=all_trades,
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["cooldown"] == 1


def test_open_position_blocks_re_entry() -> None:
    strategy = long_v11a_profile()
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
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(
        symbol="BTCUSDT",
        signal_ts_ms=signal_ts,
        signal_close=100.0,
    )
    pending = pl.DataFrame(
        [
            {
                "trade_id": "pending-1",
                "sleeve": "long",
                "strategy_id": "strategy",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "target_pending",
                "target_action": target_action,
                "qty": "0.001",
                "max_hold_deadline_ts_ms": now - MS_PER_HOUR,
            }
        ]
    )

    candidates, skips = _select_long_entry_candidates(
        features=features,
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
    assert (
        _plan_time_stop_exits(
            pending,
            now_ms=now,
        )
        == []
    )


def test_plan_time_stop_exits_only_for_expired_long_positions() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            {  # Past max-hold deadline → eligible
                "trade_id": "expired-1",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
                "max_hold_deadline_ts_ms": now - 1 * MS_PER_HOUR,
            },
            {  # Future max-hold deadline → not eligible
                "trade_id": "live-1",
                "sleeve": "long",
                "symbol": "ETHUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.01",
                "max_hold_deadline_ts_ms": now + 24 * MS_PER_HOUR,
            },
        ]
    )
    plans = _plan_time_stop_exits(trades, now_ms=now)
    assert [p["symbol"] for p in plans] == ["BTCUSDT"]
    assert plans[0]["exit_reason"] == "time_stop"


def test_long_entry_excludes_incomplete_today_bar() -> None:
    """The current, still-forming daily bar has a FUTURE day-END ts and must not be
    eligible: firing on a not-yet-closed bar is look-ahead against the backtest's
    closed-bar signal.
    """
    from liquidity_migration.rules.long_native import LongNativeConfig
    from liquidity_migration.strategy.long_native_event_demo import _select_long_entry_candidates

    now = 1_700_000_000_000
    # ONLY a future-ts (today, still forming) bar -> its day-END ts is in the future -> not eligible.
    future_bar = pl.DataFrame([{"symbol": "BTCUSDT", "ts_ms": now + MS_PER_HOUR, "close": 100.0}])
    candidates, skips = _select_long_entry_candidates(
        features=future_bar,
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
    """The long daemon's kline manager must not bootstrap all 567 USDT-perps: the
    sleeve trades the top-10 by 24h turnover, so scoping to the top-50 keeps memory
    under the systemd cap with 5x rank-shift headroom. Beyond that, per-cycle REST.
    """
    from liquidity_migration.strategy.long_native_event_demo_daemon import (
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
    """A universe fetch error must not crash the manager: an empty list bootstraps
    nothing and the cycle's REST fallback supplies everything that day.
    """
    from liquidity_migration.strategy.long_native_event_demo_daemon import (
        _build_long_kline_universe,
    )

    class _FailingMarket:
        def get_tickers(self) -> list[dict]:
            raise RuntimeError("simulated REST outage")

    assert _build_long_kline_universe(_FailingMarket()) == []


def test_compute_long_order_sizing_matches_inline_vol_target_block() -> None:
    """``_compute_long_order_sizing`` reproduces the prior inline block byte-for-byte:
    base per-position notional * the de-risk-only vol-target scalar keyed on the
    latest non-null ``btc_rv_30`` after sorting by ts_ms.
    """
    from liquidity_migration.rules.long_native import _vol_target_scale
    from liquidity_migration.strategy.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    demo = LongNativeDemoCycleConfig()
    strategy = long_v11a_profile()
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
    from liquidity_migration.rules.long_native import _vol_target_scale
    from liquidity_migration.strategy.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    demo = LongNativeDemoCycleConfig()
    strategy = long_v11a_profile()
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
    from liquidity_migration.rules.long_native import _vol_target_scale
    from liquidity_migration.strategy.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    demo = LongNativeDemoCycleConfig()
    strategy = long_v11a_profile()
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
    assert entry.metadata["signal_valid_until_ms"] == (candidate["signal_ts_ms"] + lnd.SIGNAL_FRESHNESS_MS)
    assert entry.metadata["stop_loss_pct"] == pytest.approx(0.03)
    assert entry.metadata["take_profit_pct"] == pytest.approx(0.08)
    assert entry.metadata["max_hold_duration_ms"] == 3 * lnd.MS_PER_DAY
    assert {
        "stop_price",
        "take_profit_price",
        "max_hold_deadline_ts_ms",
    }.isdisjoint(entry.metadata)

    trades = pl.DataFrame(
        [
            {
                "trade_id": "long-trade-1",
                "symbol": "ABCUSDT",
                "entry_leverage": 10.0,
                "max_hold_deadline_ts_ms": now_ms,
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
    strategy = long_v11a_profile()
    assert strategy.execution_strategy_id == LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    assert strategy.execution_leverage == 10.0


def test_median_universe_selection_steady_state_is_byte_match_noop() -> None:
    """In steady state (every name has a finite 90d median) the helper re-selects the
    same top-N-by-median set ``build_long_features`` already wrote to ``in_universe``,
    so it is a no-op byte match (fallback_count == 0) -- the consistency guarantee
    with the backtest's own universe selection.
    """
    from liquidity_migration.strategy.long_native_event_demo import _apply_median_universe_selection

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
    """Cold start: when fewer than N names have a finite median (<90 daily bars), the
    remainder is backfilled by 24h turnover so the book is never zeroed, and the
    backfill count is surfaced (universe_fallback_24h > 0).
    """
    from liquidity_migration.strategy.long_native_event_demo import _apply_median_universe_selection

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
    """A features frame lacking ``turnover_median_90d`` is returned unchanged with
    fallback 0, never crashing the cycle.
    """
    from liquidity_migration.strategy.long_native_event_demo import _apply_median_universe_selection

    feat = pl.DataFrame([{"ts_ms": 1, "symbol": "x", "turnover_quote": 1.0, "in_universe": True}])
    out, fallback = _apply_median_universe_selection(feat, universe_size=3, snapshot_ts_ms=1)
    assert fallback == 0 and out.equals(feat)


# --------------------------------------------------------------------------- #
# The LLM gate's judged events enter as candidates in the native shape         #
# --------------------------------------------------------------------------- #
def _gate_event(
    symbol: str = "PUMPUSDT",
    *,
    trigger_ts_ms: int = 1_700_000_000_000,
    atr: float = 0.05,
    sigma: float = 0.03,
    score: float = 7,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "score": score,
        "trigger_ts_ms": trigger_ts_ms,
        "trigger_price": 10.0,
        "atr_pct": atr,
        "sigma_daily_30d": sigma,
        "turnover_rank": 4,
        "trigger_window_h": 4,
    }


class TestLlmGateCandidates:
    def test_a_judged_event_becomes_a_candidate_with_the_v12_risk_contract(self) -> None:
        strategy = long_v12_profile()
        now_ms = 1_700_000_100_000
        candidates, skips = _llm_gate_candidates_for_test(
            [_gate_event()], strategy=strategy, now_ms=now_ms
        )
        assert skips == {k: 0 for k in skips}
        (cand,) = candidates
        assert cand["symbol"] == "PUMPUSDT"
        assert cand["pattern"] == "llm_gate"
        # Stop, take-profit, hold clock, and decay contract are the strategy's
        # own constants applied to the event's ATR — identical to an FC entry.
        assert cand["stop_loss_pct"] == pytest.approx(0.05 * strategy.fc_atr_stop_mult)
        assert cand["take_profit_pct"] == pytest.approx(0.05 * strategy.fc_atr_tp_mult)
        assert cand["max_hold_days"] == strategy.fc_max_hold_days
        assert cand["stop_decay_after_ms"] == exact_duration_ms(
            hours=strategy.fc_stop_time_decay_hours
        )
        assert cand["decayed_stop_loss_pct"] == pytest.approx(
            strategy.fc_stop_time_decay_atr_mult * 0.05
        )

    def test_the_position_weight_is_the_strategy_vol_parity_weight(self) -> None:
        strategy = long_v12_profile()
        now_ms = 1_700_000_100_000
        candidates, _skips = _llm_gate_candidates_for_test(
            [_gate_event()], strategy=strategy, now_ms=now_ms
        )
        (cand,) = candidates
        expected = _vol_parity_weight(
            realized_vol=0.03 * math.sqrt(365.0),
            vol_floor=strategy.vol_floor_annual,
            max_position_weight=strategy.max_position_weight,
            notional_weight=strategy.gross_exposure / strategy.max_concurrent_positions,
        )
        assert cand["position_weight"] == pytest.approx(expected)

    def test_an_event_with_no_measured_volatility_is_not_entered(self) -> None:
        # The vol-parity rule reads an absent sigma as the floor, which is its
        # CEILING weight -- so entering would put the largest position in the
        # book on the name we know least about. The ledger needs 31 daily bars
        # for sigma against 15 for the ATR, so a young listing publishes with
        # one and not the other as a matter of course.
        strategy = long_v12_profile()
        now_ms = 1_700_000_100_000
        candidates, skips = _llm_gate_candidates_for_test(
            [_gate_event("YOUNGUSDT", sigma=0.0)], strategy=strategy, now_ms=now_ms
        )
        assert candidates == []
        assert skips["llm_gate_no_vol"] == 1

    def test_a_signal_older_than_an_hour_is_not_entered(self) -> None:
        # The gate republishes every hour, so a live signal is minutes old. An
        # hour-plus means a run was missed, and the pump it named has had an
        # hour to resolve without us. Ninety minutes used to enter.
        strategy = long_v12_profile()
        now_ms = 1_700_000_100_000
        fresh, fresh_skips = _llm_gate_candidates_for_test(
            [_gate_event("AAAUSDT", trigger_ts_ms=now_ms - exact_duration_ms(minutes=59))],
            strategy=strategy,
            now_ms=now_ms,
        )
        assert [c["symbol"] for c in fresh] == ["AAAUSDT"]
        assert fresh_skips["llm_gate_stale"] == 0

        stale, stale_skips = _llm_gate_candidates_for_test(
            [_gate_event("AAAUSDT", trigger_ts_ms=now_ms - exact_duration_ms(minutes=90))],
            strategy=strategy,
            now_ms=now_ms,
        )
        assert stale == []
        assert stale_skips["llm_gate_stale"] == 1

    def test_a_file_written_more_than_an_hour_ago_reads_as_no_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json

        from liquidity_migration.strategy.long_native_event_demo import (
            LLM_GATE_CANDIDATES_PATH_ENV,
            _read_llm_gate_events,
        )

        # No valid_until_ms at all: the file's own age is the only bound left,
        # and it is an hour. Two hours used to read.
        path = tmp_path / "candidates.json"
        now_ms = 1_700_000_000_000
        path.write_text(
            _json.dumps(
                {
                    "decision_ts_ms": now_ms - exact_duration_ms(minutes=90),
                    "events": [_gate_event()],
                }
            )
        )
        monkeypatch.setenv(LLM_GATE_CANDIDATES_PATH_ENV, str(path))
        assert _read_llm_gate_events(now_ms=now_ms) == []

    def test_stale_open_cooled_and_unpriced_events_are_counted_not_entered(self) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _llm_gate_candidates

        strategy = long_v12_profile()
        now_ms = 1_700_000_100_000
        stale_ts = now_ms - exact_duration_ms(hours=25)
        events = [
            _gate_event("STALEUSDT", trigger_ts_ms=stale_ts),
            _gate_event("OPENUSDT"),
            _gate_event("COOLEDUSDT"),
            _gate_event("NOPRICEUSDT"),
            _gate_event("BADUSDT", atr=0.0),
        ]
        price_by_symbol = {
            "OPENUSDT": 9.0,
            "COOLEDUSDT": 9.0,
            "NOPRICEUSDT": 0.0,
        }
        candidates, skips = _llm_gate_candidates(
            events,
            strategy=strategy,
            price_by_symbol=price_by_symbol,
            open_symbols={"OPENUSDT"},
            cooldown_until={"COOLEDUSDT": now_ms + exact_duration_ms(days=1)},
            now_ms=now_ms,
        )
        assert candidates == []
        assert skips == {
            "llm_gate_stale": 1,
            "llm_gate_already_open": 1,
            "llm_gate_cooldown": 1,
            "llm_gate_no_live_price": 1,
            "llm_gate_bad_event": 1,
            "llm_gate_duplicate": 0,
            "llm_gate_no_vol": 0,
        }

    def test_a_duplicate_symbol_is_collapsed_to_one_candidate(self) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _llm_gate_candidates

        strategy = long_v12_profile()
        now_ms = 1_700_000_100_000
        candidates, skips = _llm_gate_candidates(
            [_gate_event(), _gate_event()],
            strategy=strategy,
            price_by_symbol={"PUMPUSDT": 9.0},
            open_symbols=set(),
            cooldown_until={},
            now_ms=now_ms,
        )
        assert len(candidates) == 1
        assert skips["llm_gate_duplicate"] == 1


def _llm_gate_candidates_for_test(events, *, strategy, now_ms):
    from liquidity_migration.strategy.long_native_event_demo import _llm_gate_candidates

    return _llm_gate_candidates(
        events,
        strategy=strategy,
        price_by_symbol={e["symbol"]: 9.0 for e in events},
        open_symbols=set(),
        cooldown_until={},
        now_ms=now_ms,
    )


class TestReadLlmGateEvents:
    def test_no_path_configured_reads_as_no_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _read_llm_gate_events

        monkeypatch.delenv("LONG_ENGINE_LLM_GATE_CANDIDATES_PATH", raising=False)
        assert _read_llm_gate_events(now_ms=1) == []

    def test_missing_file_reads_as_no_signal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _read_llm_gate_events

        monkeypatch.setenv(
            "LONG_ENGINE_LLM_GATE_CANDIDATES_PATH", str(tmp_path / "absent.json")
        )
        assert _read_llm_gate_events(now_ms=1) == []

    def test_a_fresh_file_yields_its_events(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json as _json

        from liquidity_migration.strategy.long_native_event_demo import (
            LLM_GATE_CANDIDATES_PATH_ENV,
            _read_llm_gate_events,
        )

        path = tmp_path / "candidates.json"
        now_ms = 1_700_000_000_000
        payload = {
            "decision_ts_ms": now_ms - 60_000,
            "valid_until_ms": now_ms + 3_600_000,
            "events": [_gate_event()],
        }
        path.write_text(_json.dumps(payload))
        monkeypatch.setenv(LLM_GATE_CANDIDATES_PATH_ENV, str(path))
        assert _read_llm_gate_events(now_ms=now_ms) == [_gate_event()]

    def test_a_stale_or_expired_file_reads_as_no_signal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json as _json

        from liquidity_migration.strategy.long_native_event_demo import (
            LLM_GATE_CANDIDATES_PATH_ENV,
            _read_llm_gate_events,
        )

        path = tmp_path / "candidates.json"
        payload = {
            "decision_ts_ms": 1_690_000_000_000,
            "valid_until_ms": 1_690_000_000_000 + 3_600_000,
            "events": [_gate_event()],
        }
        path.write_text(_json.dumps(payload))
        monkeypatch.setenv(LLM_GATE_CANDIDATES_PATH_ENV, str(path))
        assert _read_llm_gate_events(now_ms=1_700_000_000_000) == []

    def test_a_malformed_file_reads_as_no_signal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from liquidity_migration.strategy.long_native_event_demo import (
            LLM_GATE_CANDIDATES_PATH_ENV,
            _read_llm_gate_events,
        )

        path = tmp_path / "candidates.json"
        path.write_text("{not json")
        monkeypatch.setenv(LLM_GATE_CANDIDATES_PATH_ENV, str(path))
        assert _read_llm_gate_events(now_ms=1_700_000_000_000) == []


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
        "sniper_deadline_ms": signal_ts_ms + 6 * MS_PER_HOUR,
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
    account_id = "bybit-demo-unified" if environment == "demo" else "bybit-mainnet-unified"
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
    now_ms: int | None = None,
) -> None:
    """Write the account reading a producer sizes from.

    This used to write the Python owner's ``account_owner_health.json``. The
    engine owns the account now and says the same thing in its heartbeat, so
    this writes one of those and points the producer at it. The keys are the
    ones ``engine-core/src/heartbeat.rs`` renders.
    """

    route = _ensure_owner_route(
        account_root,
        inbox_root,
        environment=environment,
    )
    observed_wall_ts_ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    heartbeat = account_root / "engine-heartbeat.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(
        json.dumps(
            {
                "account_available_usdt": equity_usdt,
                "account_equity_usdt": equity_usdt,
                "account_observed_wall_ts_ms": observed_wall_ts_ms,
                "account_user_id": route.account_id,
                "realm": environment,
                "may_open": True,
                "mode": "live",
            }
        )
    )
    os.environ["ENGINE_ACCOUNT_HEARTBEAT_FILE"] = str(heartbeat)


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
    real_health_check = planning_module.require_recent_engine_account
    owner_health_call: dict[str, Any] = {}

    def owner_health(*args: Any, **kwargs: Any) -> Any:
        owner_health_call.update(kwargs)
        return real_health_check(*args, **kwargs)

    monkeypatch.setattr(planning_module, "require_recent_engine_account", owner_health)

    payload = _run_cycle(tmp_path / "long", demo)

    assert type(payload) is PublishedTargetCyclePayload
    assert len(payload.publication.entry_requests) == 1
    assert payload.route.environment == "demo"
    assert payload["cycle"]["account_target_route"] is True
    assert payload["cycle"]["entry_targets_queued"] == 1
    assert payload["cycle"]["equity_usdt"] == pytest.approx(12_345.0)
    assert payload["cycle"]["account_state_source"] == "account_owner_health:demo"
    # The fleet watchdog's WS-staleness alarm reads this column from the
    # cycles dataset; it must exist even when the WS store served nothing.
    assert "kline_store_max_ts_ms" in payload["cycle"]
    assert "now_ns" not in owner_health_call
    assert "entries_executed" not in payload["cycle"]
    assert "bybit_positions" not in payload
    assert payload["account_target_requests"]["entry_requests"][0]["intent_count"] == 1
    assert payload["account_target_requests"]["exit_request_ids"] == []
    pending = list((inbox / "pending").glob("*.json"))
    assert len(pending) == 1
    request = json.loads(pending[0].read_bytes())["request"]
    intent = request["intents"][0]["intent"]
    assert intent["target_key"].startswith("long/")
    assert intent["signed_notional_usdt"] > 0.0


# --------------------------------------------------------------------------- #
# The cycle context a fast wake needs                                           #
# --------------------------------------------------------------------------- #
class _CountingTimeModule:
    """`time` stand-in for strategy_planning: sleeps counted, rest passed through."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))

    def __getattr__(self, name: str) -> Any:
        import time as real_time

        return getattr(real_time, name)


class TestFastWakeSpendsTheStoredHealthReading:
    """A wake that exists to act fast must not spend its first seconds on the
    owner-health read, whose retry ladder sleeps a second between attempts
    when the owner's receipt is mid-publish. LONG had no cycle context at
    all, so every wake paid it."""

    def _cycle(
        self,
        tmp_path: Path,
        demo: LongNativeDemoCycleConfig,
        *,
        now_ms: int,
        state: lnd.LongCycleState,
        cycle_kind: str = "timer",
    ) -> Any:
        return run_long_native_demo_cycle(
            tmp_path / "long",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=demo,
            now_ms=now_ms,
            cycle_state=state,
            cycle_kind=cycle_kind,
        )

    def _demo(self, tmp_path: Path) -> LongNativeDemoCycleConfig:
        inbox = tmp_path / "account-inbox"
        account_root = tmp_path / "account"
        _ensure_owner_route(account_root, inbox, environment="demo")
        return LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(inbox),
            account_execution_root=str(account_root),
            ws_klines_enabled=False,
        )

    def test_a_price_touch_wake_does_zero_health_reads_or_sleeps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_cycle_dependencies(monkeypatch, candidates=[])
        demo = self._demo(tmp_path)
        now = 1_700_000_300_000
        monkeypatch.setattr(
            planning_module,
            "require_recent_engine_account",
            lambda *_a, **_k: SimpleNamespace(equity_usdt=10_000.0, observed_wall_ts_ms=1_700_000_300_000),
        )

        # An ordinary cycle takes the reading the fast wake will spend.
        state = lnd.LongCycleState()
        self._cycle(tmp_path, demo, now_ms=now, state=state)
        assert state.owner_health_reading is not None
        assert state.owner_health_reading.read_wall_ts_ns == now * 1_000_000
        assert state.owner_health_reading.equity_usdt == pytest.approx(10_000.0)

        # From here a live read fails. There is no retry ladder to walk any
        # more -- the engine heartbeat is one file replaced by rename, so a
        # read either lands or it does not -- but a failed read still costs the
        # wake a read, which is the thing this test is about.
        live_reads: list[str] = []

        def unreadable(*_args: Any, **_kwargs: Any) -> Any:
            live_reads.append("read")
            raise OSError("synthetic unreadable heartbeat")

        monkeypatch.setattr(planning_module, "require_recent_engine_account", unreadable)
        counting_time = _CountingTimeModule()
        monkeypatch.setattr(planning_module, "time", counting_time)

        # CONTROL — no stored reading, so the wake pays the live read.
        control = self._cycle(
            tmp_path,
            demo,
            now_ms=now + 10_000,
            state=lnd.LongCycleState(),
            cycle_kind="price_touch",
        )
        assert len(live_reads) == 1
        assert counting_time.sleeps == []
        assert control["cycle"]["equity_usdt"] is None

        # TREATED — the reading is 10s old, inside the same 30s freshness
        # bound a live read enforces: zero reads, zero sleeps.
        live_reads.clear()
        counting_time.sleeps.clear()
        wake = self._cycle(
            tmp_path, demo, now_ms=now + 10_000, state=state, cycle_kind="price_touch"
        )

        assert live_reads == []
        assert counting_time.sleeps == []
        assert wake["cycle"]["equity_usdt"] == pytest.approx(10_000.0)

    def test_a_reading_older_than_a_live_read_would_allow_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is widened: past 30s the stored reading is worth no more
        than it would be to a live read, so the cycle reads live."""

        _stub_cycle_dependencies(monkeypatch, candidates=[])
        demo = self._demo(tmp_path)
        now = 1_700_000_300_000
        live_reads: list[str] = []

        def live_health(*_args: Any, **_kwargs: Any) -> Any:
            live_reads.append("read")
            return SimpleNamespace(equity_usdt=10_000.0, observed_wall_ts_ms=1_700_000_300_000)

        monkeypatch.setattr(planning_module, "require_recent_engine_account", live_health)
        state = lnd.LongCycleState()
        self._cycle(tmp_path, demo, now_ms=now, state=state)

        live_reads.clear()
        self._cycle(
            tmp_path,
            demo,
            now_ms=now + 30_001,
            state=state,
            cycle_kind="market_boundary",
        )

        assert live_reads  # the wake paid its own live read
        assert state.owner_health_reading.read_wall_ts_ns == (now + 30_001) * 1_000_000

    def test_a_served_reading_never_outlives_its_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ages never stack: a reading taken off an already-old receipt is
        refused as soon as a live read would refuse that receipt, not a full
        reading-lifetime later."""

        _stub_cycle_dependencies(monkeypatch, candidates=[])
        demo = self._demo(tmp_path)
        now = 1_700_000_300_000
        live_reads: list[str] = []

        def aged_receipt(*_args: Any, **_kwargs: Any) -> Any:
            live_reads.append("read")
            return SimpleNamespace(
                equity_usdt=10_000.0,
                observed_wall_ts_ms=now - 25_000,
            )

        monkeypatch.setattr(planning_module, "require_recent_engine_account", aged_receipt)
        state = lnd.LongCycleState()
        self._cycle(tmp_path, demo, now_ms=now, state=state)
        assert live_reads == ["read"]
        stored = state.owner_health_reading
        assert stored is not None
        assert stored.receipt_wall_ts_ns == (now - 25_000) * 1_000_000

        # Six seconds later the reading is young (6 s) but its receipt is 31 s
        # old -- a live read would refuse it, so the fast wake must read live.
        self._cycle(
            tmp_path,
            demo,
            now_ms=now + 6_000,
            state=state,
            cycle_kind="price_touch",
        )
        assert live_reads == ["read", "read"]

    def test_a_journal_change_wake_reads_live_even_with_a_fresh_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A journal wake fires BECAUSE the journal moved, so a stored reading
        could predate the very fill that woke it."""

        _stub_cycle_dependencies(monkeypatch, candidates=[])
        demo = self._demo(tmp_path)
        now = 1_700_000_300_000
        live_reads: list[str] = []

        def live_health(*_args: Any, **_kwargs: Any) -> Any:
            live_reads.append("read")
            return SimpleNamespace(equity_usdt=10_000.0, observed_wall_ts_ms=1_700_000_300_000)

        monkeypatch.setattr(planning_module, "require_recent_engine_account", live_health)
        state = lnd.LongCycleState()
        self._cycle(tmp_path, demo, now_ms=now, state=state)
        assert state.owner_health_reading is not None

        live_reads.clear()
        self._cycle(
            tmp_path, demo, now_ms=now + 5_000, state=state, cycle_kind="journal_change"
        )

        assert live_reads

    def test_the_daemon_hands_its_cycles_the_wake_reason_and_one_state(
        self, tmp_path: Path
    ) -> None:
        from liquidity_migration.strategy.long_native_event_demo_daemon import (
            LongNativeDemoDaemon,
        )

        daemon = LongNativeDemoDaemon(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=LongNativeDemoCycleConfig(
                execution_environment="demo",
                account_intent_inbox_root=str(tmp_path / "inbox"),
                account_execution_root=str(tmp_path / "account"),
                ws_klines_enabled=False,
            ),
            cycle_runner=lambda *a, **k: None,
        )

        daemon._pending_cycle_kind = "price_touch"
        first = daemon._extra_cycle_kwargs()
        second = daemon._extra_cycle_kwargs()

        assert first["cycle_kind"] == "price_touch"
        # One state for the daemon's life, or a reading could never outlive
        # the cycle that took it.
        assert first["cycle_state"] is second["cycle_state"]
        assert type(first["cycle_state"]) is lnd.LongCycleState


# --------------------------------------------------------------------------- #
# Price levels the cycle asks to be woken for                                   #
# --------------------------------------------------------------------------- #
def test_a_candidate_still_waiting_for_its_retrace_reports_the_price() -> None:
    """The level the entry is waiting for is exactly the published threshold."""

    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(
        symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0
    )
    watch: list[dict[str, Any]] = []

    candidates, skips = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        # Above the 1%-below threshold: no entry yet, so the cycle waits.
        price_by_symbol={"BTCUSDT": 99.5},
        max_new_entries=5,
        retrace_watch=watch,
    )

    assert candidates == []
    assert skips["no_retrace_yet"] == 1
    assert watch == [{"symbol": "BTCUSDT", "at_or_below": pytest.approx(99.0)}]


def test_a_candidate_that_already_entered_asks_for_no_price_wake() -> None:
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    features = _build_features_with_fc_signal(
        symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0
    )
    watch: list[dict[str, Any]] = []

    candidates, _skips = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=signal_ts + 2 * MS_PER_HOUR,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
        retrace_watch=watch,
    )

    assert len(candidates) == 1
    assert watch == []


def test_an_armed_decayed_stop_is_a_price_the_cycle_watches() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            {
                "trade_id": "armed",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
                "stop_decay_after_ms": 48 * MS_PER_HOUR,
                "decayed_stop_loss_pct": 0.05,
                "entry_ts_ms": now - 49 * MS_PER_HOUR,
                "entry_price": 100.0,
            }
        ]
    )

    levels = lnd._long_price_wake_levels(trades, retrace_watch=[], now_ms=now)

    assert levels == [{"symbol": "BTCUSDT", "at_or_below": pytest.approx(95.0)}]


def test_a_decayed_stop_whose_clock_has_not_started_is_not_watched() -> None:
    """The arming instant is already a reported time deadline; watching the
    price early would wake cycles that can do nothing with the touch."""

    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            {
                "trade_id": "unarmed",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
                "stop_decay_after_ms": 48 * MS_PER_HOUR,
                "decayed_stop_loss_pct": 0.05,
                "entry_ts_ms": now - 47 * MS_PER_HOUR,
                "entry_price": 100.0,
            }
        ]
    )

    assert lnd._long_price_wake_levels(trades, retrace_watch=[], now_ms=now) == []


def test_trades_a_decay_exit_could_not_act_on_are_not_watched() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            {
                "trade_id": "no-qty",
                "sleeve": "long",
                "symbol": "BTCUSDT",
                "side": "long",
                "status": "open",
                "qty": "0",
                "stop_decay_after_ms": MS_PER_HOUR,
                "decayed_stop_loss_pct": 0.05,
                "entry_ts_ms": now - 2 * MS_PER_HOUR,
                "entry_price": 100.0,
            },
            {
                "trade_id": "no-fill-price",
                "sleeve": "long",
                "symbol": "ETHUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
                "stop_decay_after_ms": MS_PER_HOUR,
                "decayed_stop_loss_pct": 0.05,
                "entry_ts_ms": now - 2 * MS_PER_HOUR,
                "entry_price": 0.0,
            },
            {
                "trade_id": "no-decay-contract",
                "sleeve": "long",
                "symbol": "SOLUSDT",
                "side": "long",
                "status": "open",
                "qty": "0.001",
                "entry_ts_ms": now - 2 * MS_PER_HOUR,
                "entry_price": 100.0,
            },
        ]
    )

    assert lnd._long_price_wake_levels(trades, retrace_watch=[], now_ms=now) == []
    assert lnd._long_price_wake_levels(pl.DataFrame(), retrace_watch=[], now_ms=now) == []


def test_the_cycle_payload_carries_the_prices_the_daemon_should_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails without the fix: the payload had no price levels at all, so the
    daemon could only ever notice a retrace on whatever cycle came next."""

    _stub_cycle_dependencies(monkeypatch, candidates=[_candidate("AAAUSDT")])

    def waiting_on_a_retrace(**kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
        kwargs["retrace_watch"].append({"symbol": "AAAUSDT", "at_or_below": 42.5})
        return [], {"no_retrace_yet": 1}

    monkeypatch.setattr(lnd, "_select_long_entry_candidates", waiting_on_a_retrace)
    inbox = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    _write_owner_health(account_root, inbox, environment="demo")

    payload = _run_cycle(tmp_path / "long", demo)

    assert payload["price_wake_levels"] == [{"symbol": "AAAUSDT", "at_or_below": 42.5}]


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
    # Null, not a 0.0 sentinel: cycles-derived equity curves must not read a
    # blocked-owner cycle as a -100% equity spike.
    assert payload["cycle"]["equity_usdt"] is None
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
    from liquidity_migration.account.account_kernel import (
        AccountExecutionKernel,
        AccountRiskPolicy,
        AccountRiskSnapshot,
        DesiredTarget,
        InstrumentRules,
        MarketInputRef,
    )
    from liquidity_migration.core.deterministic_runtime import VirtualClock

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
    strategy_id = long_v11a_profile().execution_strategy_id
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
        targets=[
            DesiredTarget(
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
            )
        ],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 10_000.0, "wallet", 3),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 10.0),
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
    from liquidity_migration.account.account_intent_client import AccountTargetPublisher
    from liquidity_migration.account.account_service import AccountServiceReceipt

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
    strategy_id = long_v11a_profile().execution_strategy_id
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
            rejection_keys=(f"account-service:entry-signal-expired:{proposed.intent.metadata['entry_attempt_key']}",),
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


def _fc_signal_features(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal feature row that passes detect_pattern_fomo_chase (mirrors the long_native_event_demo test fixture)."""
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
    strategy = long_v11a_profile()
    assert strategy.weekend_size_mult == 1.5, "v11a profile must carry the 1.5x weekend tilt"
    signal_ts = now_ms - 2 * MS_PER_HOUR  # fresh, same UTC day, retrace fired
    features = _fc_signal_features(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features,
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


def test_median_universe_selection_targets_latest_closed_bar_not_future_bar() -> None:
    """A daily feature row is stamped at the day END, so a still-forming UTC day yields
    a bar stamped after ``snapshot_ts_ms``. Entries fire from the latest CLOSED bar,
    so the re-selection and its telemetry must target that bar, not the partial one.
    """
    from liquidity_migration.strategy.long_native_event_demo import _apply_median_universe_selection

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


# --- v12 (LongV12WideStop) decayed-stop wiring --------------------------------


def _open_trade_row(
    *,
    trade_id: str = "long-ABCUSDT-1",
    symbol: str = "ABCUSDT",
    strategy_id: str = LONG_V12_WIDE_STOP_STRATEGY_ID,
    entry_ts_ms: int | None,
    entry_price: float | None,
    with_decay_contract: bool = True,
    max_hold_deadline_ts_ms: int = 0,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trade_id": trade_id,
        "sleeve": "long",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": "long",
        "status": "open",
        "qty": "10",
        "entry_ts_ms": entry_ts_ms,
        "entry_price": entry_price,
        "max_hold_deadline_ts_ms": max_hold_deadline_ts_ms,
    }
    if with_decay_contract:
        # v12 with atr_14d_pct=0.05: initial stop 15%, decayed stop 7.5%.
        row["stop_decay_after_ms"] = 48 * MS_PER_HOUR
        row["decayed_stop_loss_pct"] = 0.075
    return row


def test_v12_candidates_carry_stop_decay_contract() -> None:
    strategy = long_v12_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts)
    candidates, _ = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    # Frozen off the signal-day ATR (0.05): wide stop 3x, decayed 1.5x.
    assert candidate["stop_loss_pct"] == pytest.approx(0.15)
    assert candidate["decayed_stop_loss_pct"] == pytest.approx(0.075)
    assert candidate["stop_decay_after_ms"] == 48 * MS_PER_HOUR
    assert candidate["take_profit_pct"] == pytest.approx(0.20)


def test_v11a_candidates_do_not_carry_stop_decay_contract() -> None:
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts)
    candidates, _ = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=long_v11a_profile(),
        price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    assert candidates[0]["stop_loss_pct"] == pytest.approx(0.075)
    assert "stop_decay_after_ms" not in candidates[0]
    assert "decayed_stop_loss_pct" not in candidates[0]


def test_entry_intent_metadata_freezes_decay_contract() -> None:
    now_ms = 1_700_000_000_000
    base_candidate = {
        "trade_id": "long-trade-1",
        "symbol": "ABCUSDT",
        "signal_ts_ms": now_ms - 60_000,
        "entry_reason": "fomo_chase",
        "position_weight": 0.5,
        "stop_loss_pct": 0.15,
        "take_profit_pct": 0.20,
        "max_hold_days": 3,
        "atr_14d_pct": 0.05,
    }
    demo = LongNativeDemoCycleConfig(entry_leverage=10.0)
    with_contract = dict(
        base_candidate,
        stop_decay_after_ms=48 * MS_PER_HOUR,
        decayed_stop_loss_pct=0.075,
    )
    entries = lnd._long_entry_target_intents(
        [with_contract],
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.10,
        price_by_symbol={"ABCUSDT": 2.0},
        now_ms=now_ms,
        strategy_id=LONG_V12_WIDE_STOP_STRATEGY_ID,
    )
    assert len(entries) == 1
    metadata = entries[0].intent.metadata
    assert metadata["stop_decay_after_ms"] == 48 * MS_PER_HOUR
    assert metadata["decayed_stop_loss_pct"] == pytest.approx(0.075)
    assert metadata["atr_14d_pct"] == pytest.approx(0.05)

    plain = lnd._long_entry_target_intents(
        [dict(base_candidate)],
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.10,
        price_by_symbol={"ABCUSDT": 2.0},
        now_ms=now_ms,
        strategy_id=LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
    )
    assert {"stop_decay_after_ms", "decayed_stop_loss_pct", "atr_14d_pct"}.isdisjoint(
        plain[0].intent.metadata
    )


def test_planning_metadata_whitelist_round_trips_decay_contract() -> None:
    from liquidity_migration.strategy.account_strategy_state import _planning_metadata

    surviving = _planning_metadata(
        {
            "stop_loss_pct": 0.15,
            "stop_decay_after_ms": 48 * MS_PER_HOUR,
            "decayed_stop_loss_pct": 0.075,
            "atr_14d_pct": 0.05,
            "unrelated_key": "dropped",
        }
    )
    assert surviving == {
        "stop_loss_pct": 0.15,
        "stop_decay_after_ms": 48 * MS_PER_HOUR,
        "decayed_stop_loss_pct": 0.075,
        "atr_14d_pct": 0.05,
    }


def test_plan_decayed_stop_fires_only_after_decay_age_and_breach() -> None:
    now = 2_000_000_000_000
    entry_price = 100.0
    aged_entry = now - 50 * MS_PER_HOUR
    young_entry = now - 47 * MS_PER_HOUR
    decayed_level = entry_price * (1.0 - 0.075)  # 92.5

    aged_and_breached = pl.DataFrame(
        [_open_trade_row(entry_ts_ms=aged_entry, entry_price=entry_price)]
    )
    plans = _plan_time_stop_exits(
        aged_and_breached, now_ms=now, price_by_symbol={"ABCUSDT": 92.0}
    )
    assert [p["exit_reason"] for p in plans] == ["decayed_stop_loss"]
    assert plans[0]["decayed_stop_price"] == pytest.approx(decayed_level)
    assert plans[0]["stop_decay_deadline_ts_ms"] == aged_entry + 48 * MS_PER_HOUR
    assert plans[0]["decision_reference_price"] == pytest.approx(92.0)

    # At the level exactly: breach (research convention is <=).
    at_level = _plan_time_stop_exits(
        aged_and_breached, now_ms=now, price_by_symbol={"ABCUSDT": decayed_level}
    )
    assert [p["exit_reason"] for p in at_level] == ["decayed_stop_loss"]

    # Above the decayed level: no exit.
    assert (
        _plan_time_stop_exits(
            aged_and_breached, now_ms=now, price_by_symbol={"ABCUSDT": 93.0}
        )
        == []
    )

    # Breached but younger than the decay age: no exit.
    young = pl.DataFrame(
        [_open_trade_row(entry_ts_ms=young_entry, entry_price=entry_price)]
    )
    assert (
        _plan_time_stop_exits(young, now_ms=now, price_by_symbol={"ABCUSDT": 92.0})
        == []
    )


def test_plan_decayed_stop_ignores_trades_without_contract() -> None:
    now = 2_000_000_000_000
    v11a = pl.DataFrame(
        [
            _open_trade_row(
                strategy_id=LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
                entry_ts_ms=now - 60 * MS_PER_HOUR,
                entry_price=100.0,
                with_decay_contract=False,
                max_hold_deadline_ts_ms=now + 12 * MS_PER_HOUR,
            )
        ]
    )
    assert (
        _plan_time_stop_exits(v11a, now_ms=now, price_by_symbol={"ABCUSDT": 50.0})
        == []
    )


def test_plan_decayed_stop_requires_fill_anchor_and_live_price() -> None:
    now = 2_000_000_000_000
    no_anchor = pl.DataFrame(
        [_open_trade_row(entry_ts_ms=None, entry_price=None)]
    )
    assert (
        _plan_time_stop_exits(no_anchor, now_ms=now, price_by_symbol={"ABCUSDT": 1.0})
        == []
    )
    anchored = pl.DataFrame(
        [_open_trade_row(entry_ts_ms=now - 50 * MS_PER_HOUR, entry_price=100.0)]
    )
    # No live price this cycle: defer, the venue-native stop stays armed.
    assert _plan_time_stop_exits(anchored, now_ms=now, price_by_symbol={}) == []
    assert _plan_time_stop_exits(anchored, now_ms=now) == []


def test_time_stop_wins_over_decayed_stop() -> None:
    now = 2_000_000_000_000
    both_due = pl.DataFrame(
        [
            _open_trade_row(
                entry_ts_ms=now - 80 * MS_PER_HOUR,
                entry_price=100.0,
                max_hold_deadline_ts_ms=now - MS_PER_HOUR,
            )
        ]
    )
    plans = _plan_time_stop_exits(both_due, now_ms=now, price_by_symbol={"ABCUSDT": 50.0})
    assert [p["exit_reason"] for p in plans] == ["time_stop"]


def test_exit_intents_keyed_by_owning_trade_strategy_id() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            _open_trade_row(
                trade_id="long-OLD-1",
                symbol="OLDUSDT",
                strategy_id=LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
                entry_ts_ms=now - 80 * MS_PER_HOUR,
                entry_price=100.0,
                with_decay_contract=False,
                max_hold_deadline_ts_ms=now - MS_PER_HOUR,
            ),
            _open_trade_row(
                trade_id="long-NEW-1",
                symbol="NEWUSDT",
                strategy_id=LONG_V12_WIDE_STOP_STRATEGY_ID,
                entry_ts_ms=now - 50 * MS_PER_HOUR,
                entry_price=100.0,
            ),
        ]
    )
    plans = _plan_time_stop_exits(
        trades, now_ms=now, price_by_symbol={"OLDUSDT": 99.0, "NEWUSDT": 92.0}
    )
    assert {p["exit_reason"] for p in plans} == {"time_stop", "decayed_stop_loss"}
    intents = lnd._long_exit_target_intents(
        plans,
        trades,
        strategy_id=LONG_V12_WIDE_STOP_STRATEGY_ID,
        now_ms=now,
        default_leverage=10.0,
    )
    keys_by_symbol = {intent.intent.symbol: intent.intent.target_key for intent in intents}
    # A v11a residue component exits under the v11a identity even though the
    # producer now runs v12 — the target key must match the standing target.
    assert LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID in keys_by_symbol["OLDUSDT"]
    assert LONG_V12_WIDE_STOP_STRATEGY_ID in keys_by_symbol["NEWUSDT"]
    decayed = [i for i in intents if i.intent.symbol == "NEWUSDT"]
    assert decayed[0].intent.reason == "decayed_stop_loss"
    assert decayed[0].intent.metadata["decayed_stop_price"] == pytest.approx(92.5)


def test_validate_rejects_unregistered_strategy_identity() -> None:
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root="/tmp/inbox",
        account_execution_root="/tmp/account",
    )
    rogue = replace(long_v12_profile(), execution_strategy_id="long_native_v13_unregistered")
    with pytest.raises(ValueError, match="unsupported LONG execution_strategy_id"):
        _validate_long_demo_config(demo, rogue)


class _HealthSentinel(RuntimeError):
    """Marks that the cycle reached the owner-health read past the gate."""


def test_retiring_symbol_with_exposure_does_not_wedge_the_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled retirement with live exposure must not fail the cycle.

    The pre-2026-08-13 gate raised before exit planning, so the one producer
    able to publish the flattening exits refused to run — a deadlock broken
    only by venue settlement or an operator flatten. The cycle now reports the
    draining symbol and keeps going: with the fix, execution reaches the
    owner-health read (the sentinel below); without it, the flatness
    RuntimeError fires first and this test fails.
    """

    from liquidity_migration.account.account_intent_client import (
        AccountTargetPublisher,
        requested_target,
    )
    from liquidity_migration.account.account_service import SleeveAdapterKind
    from liquidity_migration.strategy.account_candidate_universe import (
        build_candidate_universe_artifact,
        write_candidate_universe,
    )
    from tests.strategy.test_account_candidate_universe import (
        SNAPSHOT_NS,
        _instrument,
        _ticker,
    )

    now_ms = SNAPSHOT_NS // 1_000_000 + 60_000
    # Both symbols clear every LONG population filter at freeze time, so the
    # frozen long profile holds both and BBBUSDT's later delivery evidence is
    # a scheduled retirement rather than a never-member.
    artifact = write_candidate_universe(
        tmp_path / "candidate.json",
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT"), _instrument("BBBUSDT")],
            [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "4000000")],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
        ),
    )

    class _PublicClient:
        def get_instruments_info(self) -> list[dict[str, Any]]:
            # The venue has announced BBBUSDT's retirement: still trading,
            # delivery in the future.
            return [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(now_ms + 86_400_000)),
            ]

        def get_tickers(self) -> list[dict[str, Any]]:
            return [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "4000000")]

    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    AccountTargetPublisher(route).publish(
        batch_id="bbb-standing-exposure",
        intents=(
            requested_target(
                adapter_kind=SleeveAdapterKind.HEDGE,
                decision_key="bbb-standing-exposure/BBBUSDT",
                target_key="hedge/test/bbb/BBBUSDT",
                strategy_id="test",
                component_id="bbb",
                symbol="BBBUSDT",
                signed_notional_usdt=100.0,
                leverage=2.0,
                reason="test",
            ),
        ),
        created_ts_ns=now_ms * 1_000_000,
    )

    def _sentinel_health(*args: Any, **kwargs: Any) -> Any:
        raise _HealthSentinel("cycle proceeded past the retirement gate")

    monkeypatch.setattr(lnd, "account_owner_health_reading", _sentinel_health)

    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_execution_root=str(tmp_path / "account"),
        account_intent_inbox_root=str(tmp_path / "inbox"),
        candidate_universe_file=str(artifact),
    )
    with pytest.raises(_HealthSentinel):
        run_long_native_demo_cycle(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=demo,
            market_client=_PublicClient(),
            now_ms=now_ms,
        )


# --------------------------------------------------------------------------- #
# What the engine says back: refusals, venue truth, and the regime anchors     #
# --------------------------------------------------------------------------- #


def test_regime_blocked_pumps_count_separately_from_no_signal() -> None:
    """A pump that fired but the regime gate refused used to land in the same
    no_signal count as a quiet day, so a gate stuck off looked exactly like no
    signals anywhere."""
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(
        symbol="AAAUSDT", signal_ts_ms=signal_ts
    ).with_columns(
        [
            pl.lit(False).alias("regime_on"),
            pl.lit(False).alias("eth_regime_on"),
        ]
    )

    candidates, skips = _select_long_entry_candidates(
        features=features,
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"AAAUSDT": 98.5},
        max_new_entries=5,
    )

    assert candidates == []
    assert skips["no_signal"] == 1
    assert skips["regime_btc_off"] == 1
    assert skips["regime_eth_off"] == 1


def test_regime_anchors_are_fetched_even_when_the_universe_lacks_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BTC or ETH missing from the frame both regime flags read false and
    every native entry stops without a word. The two anchors are fetched for
    the regime join whatever the frozen artifact says, their absence is said
    out loud, and a force-added anchor never becomes a candidate."""
    candidate = _candidate("AAAUSDT")
    _stub_cycle_dependencies(monkeypatch, candidates=[candidate])
    fetched: list[list[str]] = []

    def _capture_klines(symbols: list[str], *args: Any, **kwargs: Any) -> tuple[pl.DataFrame, dict[str, int]]:
        fetched.append(list(symbols))
        return pl.DataFrame(), {
            "cache_rows": 0,
            "fetched_rows": 0,
            "store_rows": 0,
            "store_symbols": 0,
        }

    def _features_without_anchors(klines: pl.DataFrame, config: Any = None) -> pl.DataFrame:
        # The universe row arrived; the two regime anchors did not.
        return pl.DataFrame(
            {
                "ts_ms": [1_700_000_000_000],
                "symbol": ["AAAUSDT"],
                "regime_on": [True],
                "eth_regime_on": [True],
            }
        )

    monkeypatch.setattr(lnd, "_download_recent_1h_klines", _capture_klines)
    monkeypatch.setattr(lnd, "build_long_features", _features_without_anchors)

    inbox = tmp_path / "account-inbox"
    account_root = tmp_path / "account"
    demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(inbox),
        account_execution_root=str(account_root),
        ws_klines_enabled=False,
    )
    _write_owner_health(account_root, inbox, environment="demo")

    payload = _run_cycle(tmp_path / "long", demo)

    assert {"BTCUSDT", "ETHUSDT"} <= set(fetched[0]), "anchors are always fetched"
    cycle = payload["cycle"]
    assert cycle["regime_btc_on"] is None
    assert json.loads(cycle["regime_anchors_missing_json"]) == ["BTCUSDT", "ETHUSDT"]
    # The anchor rows themselves are gone from candidacy; only AAA was asked.
    assert [c["symbol"] for c in payload["candidates"]] == ["AAAUSDT"]


def _write_engine_heartbeat(
    account_root: Path,
    inbox_root: Path,
    *,
    environment: str = "demo",
    equity_usdt: float = 10_000.0,
    positions: list[dict[str, Any]] | None = None,
    entry_blockers: list[dict[str, str]] | None = None,
) -> None:
    """The engine heartbeat, with the position and refusal arrays it renders."""
    route = _ensure_owner_route(account_root, inbox_root, environment=environment)
    heartbeat = account_root / "engine-heartbeat.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "account_available_usdt": equity_usdt,
        "account_equity_usdt": equity_usdt,
        "account_observed_wall_ts_ms": time.time_ns() // 1_000_000,
        "account_user_id": route.account_id,
        "realm": environment,
        "may_open": True,
        "mode": "live",
    }
    if positions is not None:
        payload["positions"] = positions
    if entry_blockers is not None:
        payload["entry_blockers"] = entry_blockers
    heartbeat.write_text(json.dumps(payload))
    os.environ["ENGINE_ACCOUNT_HEARTBEAT_FILE"] = str(heartbeat)


def _book_mode_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    from liquidity_migration.strategy.long_book_state import LONG_BOOK_STATE_PATH_ENV

    book = tmp_path / "targets" / "long-demo.json"
    state = tmp_path / "targets" / "long-demo-state.json"
    monkeypatch.setenv(lnd.ENGINE_TARGET_BOOK_PATH_ENV, str(book))
    monkeypatch.setenv(LONG_BOOK_STATE_PATH_ENV, str(state))
    return book, state


def _book_demo_config(tmp_path: Path) -> LongNativeDemoCycleConfig:
    return LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(tmp_path / "account-inbox"),
        account_execution_root=str(tmp_path / "account"),
        ws_klines_enabled=False,
    )


def test_book_mode_counts_what_the_book_took_in_and_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In book mode nothing is published to the inbox, so the queued counters
    read what the book itself did. They used to be zero on every cycle for
    ever, including the journald summary line."""
    from liquidity_migration.strategy.long_book_state import (
        LongBookEntry,
        LongBookState,
        read_book_state,
        write_book_state,
    )

    _stub_cycle_dependencies(monkeypatch, candidates=[])
    _book_mode_paths(tmp_path, monkeypatch)
    account_root = tmp_path / "account"
    _write_engine_heartbeat(account_root, tmp_path / "account-inbox")

    now_ms = 1_700_000_300_000
    expired = LongBookEntry(
        trade_id="long-KAITOUSDT-1",
        symbol="KAITOUSDT",
        strategy_id="long_v11a_div_weekend_vol",
        notional_usdt=500.0,
        stop_loss_fraction=0.15,
        leverage=2.0,
        entered_ts_ms=now_ms - exact_duration_ms(days=4),
        entry_price=10.0,
        max_hold_deadline_ts_ms=now_ms - 1_000,
        seen_held=True,
    )
    write_book_state(
        tmp_path / "targets" / "long-demo-state.json",
        LongBookState(held={"KAITOUSDT": expired}),
    )

    payload = run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_book_demo_config(tmp_path),
        now_ms=now_ms,
    )
    cycle = payload["cycle"]

    assert cycle["exit_targets_queued"] == 1, "the time stop left the book"
    assert cycle["entry_targets_queued"] == 0
    assert cycle["book_targets"] == 0
    back = read_book_state(tmp_path / "targets" / "long-demo-state.json")
    assert back.held == {}


def test_book_mode_drops_a_never_held_ask_the_engine_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ask the kernel refused never becomes a position, but it reserved a
    slot until its three-day deadline -- closed_elsewhere could not free it
    because it never became seen_held. The refusal now crosses the heartbeat
    and the ask leaves the record the same cycle, with no cooldown: the name
    never held."""
    from liquidity_migration.strategy.long_book_state import (
        LongBookEntry,
        LongBookState,
        read_book_state,
        write_book_state,
    )

    _stub_cycle_dependencies(monkeypatch, candidates=[])
    _book_mode_paths(tmp_path, monkeypatch)
    _write_engine_heartbeat(
        tmp_path / "account",
        tmp_path / "account-inbox",
        positions=[],
        entry_blockers=[
            {"symbol": "KAITOUSDT", "reason": "AvailableMarginExhausted { available_usdt: 0.5 }"}
        ],
    )

    now_ms = 1_700_000_300_000
    pending = LongBookEntry(
        trade_id="long-KAITOUSDT-1",
        symbol="KAITOUSDT",
        strategy_id="long_v11a_div_weekend_vol",
        notional_usdt=500.0,
        stop_loss_fraction=0.15,
        leverage=2.0,
        entered_ts_ms=now_ms - exact_duration_ms(hours=2),
        entry_price=10.0,
        max_hold_deadline_ts_ms=now_ms + exact_duration_ms(days=3),
        seen_held=False,
    )
    state_path = tmp_path / "targets" / "long-demo-state.json"
    write_book_state(state_path, LongBookState(held={"KAITOUSDT": pending}))

    payload = run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_book_demo_config(tmp_path),
        now_ms=now_ms,
    )
    cycle = payload["cycle"]

    assert cycle["engine_blocked_asks"] == 1
    back = read_book_state(state_path)
    assert back.held == {}, "the refused ask left the record"
    assert back.left_at_ms == {}, "no cooldown: the name never held"


def test_book_mode_does_not_drop_a_confirmed_holding_the_engine_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocker on a name the engine HAS confirmed is exit business, not
    entry bookkeeping: the holding stays in the record until its own exit or
    a confirmed close says otherwise."""
    from liquidity_migration.strategy.long_book_state import (
        LongBookEntry,
        LongBookState,
        read_book_state,
        write_book_state,
    )

    _stub_cycle_dependencies(monkeypatch, candidates=[])
    _book_mode_paths(tmp_path, monkeypatch)
    _write_engine_heartbeat(
        tmp_path / "account",
        tmp_path / "account-inbox",
        positions=[{"symbol": "KAITOUSDT", "side": "long", "qty": 50.0, "entry_px": 10.0}],
        entry_blockers=[{"symbol": "KAITOUSDT", "reason": "stale_quote"}],
    )

    now_ms = 1_700_000_300_000
    held = LongBookEntry(
        trade_id="long-KAITOUSDT-1",
        symbol="KAITOUSDT",
        strategy_id="long_v11a_div_weekend_vol",
        notional_usdt=500.0,
        stop_loss_fraction=0.15,
        leverage=2.0,
        entered_ts_ms=now_ms - exact_duration_ms(hours=2),
        entry_price=10.0,
        max_hold_deadline_ts_ms=now_ms + exact_duration_ms(days=3),
        seen_held=True,
    )
    state_path = tmp_path / "targets" / "long-demo-state.json"
    write_book_state(state_path, LongBookState(held={"KAITOUSDT": held}))

    payload = run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_book_demo_config(tmp_path),
        now_ms=now_ms,
    )

    assert payload["cycle"]["engine_blocked_asks"] == 0
    back = read_book_state(state_path)
    assert list(back.held) == ["KAITOUSDT"], "a confirmed holding is not an ask"
    assert back.held["KAITOUSDT"].venue_qty == 50.0, "and its venue truth is recorded"


def test_book_mode_skips_a_candidate_the_engine_is_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-asking a standing refusal every cycle would write the ask into the
    book only to drop it again on the next one. It is skipped and counted
    instead, which frees the slot for a candidate that can actually fill."""
    blocked = _candidate("AAAUSDT")
    free = _candidate("BBBUSDT")
    _stub_cycle_dependencies(monkeypatch, candidates=[blocked, free])
    _book_mode_paths(tmp_path, monkeypatch)
    _write_engine_heartbeat(
        tmp_path / "account",
        tmp_path / "account-inbox",
        positions=[],
        entry_blockers=[{"symbol": "AAAUSDT", "reason": "below_entry_floor"}],
    )

    payload = run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_book_demo_config(tmp_path),
        now_ms=1_700_000_300_000,
    )
    cycle = payload["cycle"]

    assert cycle["skipped_engine_blocked"] == 1
    from liquidity_migration.strategy.long_book_state import read_book_state

    back = read_book_state(tmp_path / "targets" / "long-demo-state.json")
    assert sorted(back.held) == ["BBBUSDT"]
    assert cycle["entry_targets_queued"] == 1


class TestBookDeclaresTheDecayedStop:
    """v12 narrows a held name's stop after 48h. The engine attaches a
    venue-native stop from what the book declares, so the narrower distance is
    only real once the book says it."""

    @staticmethod
    def _entry(**over: object) -> Any:
        from liquidity_migration.strategy.long_book_state import LongBookEntry

        base: dict[str, Any] = {
            "trade_id": "long-AAAUSDT-1",
            "symbol": "AAAUSDT",
            "strategy_id": "long_native_active_v12",
            "notional_usdt": 100.0,
            "stop_loss_fraction": 0.30,
            "leverage": 5.0,
            "entered_ts_ms": 1_700_000_000_000,
            "entry_price": 10.0,
            "max_hold_deadline_ts_ms": 1_700_000_000_000 + exact_duration_ms(days=3),
            "stop_decay_after_ms": exact_duration_ms(hours=48),
            "decayed_stop_loss_pct": 0.15,
        }
        base.update(over)
        return LongBookEntry(**base)  # type: ignore[arg-type]

    def test_before_the_decay_deadline_the_book_declares_the_opening_stop(self) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _long_stop_fraction_now

        entry = self._entry()
        at = entry.entered_ts_ms + exact_duration_ms(hours=47)
        assert _long_stop_fraction_now(entry, now_ms=at) == 0.30

    def test_after_the_decay_deadline_the_book_declares_the_narrower_stop(self) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _long_stop_fraction_now

        entry = self._entry()
        at = entry.entered_ts_ms + exact_duration_ms(hours=49)
        assert _long_stop_fraction_now(entry, now_ms=at) == 0.15

    def test_a_trade_with_no_decay_contract_keeps_its_opening_stop(self) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _long_stop_fraction_now

        entry = self._entry(stop_decay_after_ms=0, decayed_stop_loss_pct=0.0)
        at = entry.entered_ts_ms + exact_duration_ms(days=7)
        assert _long_stop_fraction_now(entry, now_ms=at) == 0.30

    def test_the_contract_can_only_tighten(self) -> None:
        from liquidity_migration.strategy.long_native_event_demo import _long_stop_fraction_now

        # A record whose decayed number is WIDER than what it opened behind.
        entry = self._entry(decayed_stop_loss_pct=0.50)
        at = entry.entered_ts_ms + exact_duration_ms(hours=49)
        assert _long_stop_fraction_now(entry, now_ms=at) == 0.30

    def test_the_rendered_book_carries_the_narrower_stop(self) -> None:
        import json as _json

        from liquidity_migration.strategy.long_book_state import LongBookState
        from liquidity_migration.strategy.long_native_event_demo import _long_engine_target_book

        entry = self._entry()
        state = LongBookState(held={entry.symbol: entry})
        at = entry.entered_ts_ms + exact_duration_ms(hours=49)
        book = _json.loads(
            _long_engine_target_book(state, decision_ts_ms=at, strategy_profile="long_v12")
        )
        (target,) = book["targets"]
        assert target["stop_loss_fraction"] == 0.15
