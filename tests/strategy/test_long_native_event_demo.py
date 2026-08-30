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

import math
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import liquidity_migration.strategy.long_native_event_demo as lnd

from liquidity_migration.core._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.rules.long_identity import (
    LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
    LONG_V12_WIDE_STOP_STRATEGY_ID,
)
from liquidity_migration.rules.long_contract import ConfigLayer, resolve_strategy_config
from liquidity_migration.rules.long_native import long_v11a_profile, long_v12_profile
from liquidity_migration.strategy.long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _count_long_target_reservations,
    _open_long_trades,
    _plan_time_stop_exits as _plan_time_stop_exits_contract,
    _select_long_entry_candidates as _select_long_entry_candidates_contract,
    _vol_parity_weight,
    format_long_demo_cycle_summary,
    target_long_order_notional_pct_equity,
)


def _effective_long_config(
    *,
    notional_multiplier: float = 1.0,
    entry_leverage: float = 10.0,
    order_notional_pct_equity: float = 0.0,
):
    return resolve_strategy_config(
        "v11a",
        layers=(
            ConfigLayer(
                source="test",
                values={
                    "notional_multiplier": notional_multiplier,
                    "entry_leverage": entry_leverage,
                    "order_notional_pct_equity": order_notional_pct_equity,
                },
            ),
        ),
    )


def _select_long_entry_candidates(**kwargs: Any):
    """Test adapter that makes every legacy fixture name its typed contract."""

    strategy = kwargs["strategy"]
    maximum = kwargs.pop("max_new_entries", None)
    effective = kwargs.get("effective_config")
    if effective is None:
        layers = ()
        if maximum is not None:
            layers = (
                ConfigLayer(
                    source="test_fixture",
                    values={"max_new_entries_per_cycle": maximum},
                ),
            )
        profile = "v12" if strategy.execution_strategy_id == LONG_V12_WIDE_STOP_STRATEGY_ID else "v11a"
        effective = resolve_strategy_config(
            profile,
            rule=strategy,
            layers=layers,
            rule_source="test_fixture",
        )
        kwargs["effective_config"] = effective
    elif maximum is not None:
        assert maximum == effective.max_new_entries_per_cycle
    kwargs.setdefault("equity_usdt", 1.0)
    return _select_long_entry_candidates_contract(**kwargs)


def _plan_time_stop_exits(*args: Any, **kwargs: Any):
    kwargs.setdefault("effective_config", resolve_strategy_config("v12"))
    return _plan_time_stop_exits_contract(*args, **kwargs)


@pytest.fixture(autouse=True)
def _no_leaked_heartbeat_path() -> Any:
    """Tests may point the producer at a temporary heartbeat; a path
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
    effective = _effective_long_config()
    assert effective.notional_multiplier == pytest.approx(1.0)
    assert target_long_order_notional_pct_equity(effective) == pytest.approx(
        effective.rule.gross_exposure / effective.rule.max_concurrent_positions
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
        lnd.resolve_long_effective_config(
            LongNativeDemoCycleConfig(),
            runtime=lnd.LongRuntimeConfig(data_root=tmp_path),
            strategy=_effective_long_config(),
            exchange=ResearchConfig().exchange,
            exchange_source="test",
            operational_profile_source="test",
            operational_profile_sha256="11" * 32,
            target_book_path=tmp_path / "long.json",
            book_state_path=tmp_path / "state.json",
            book_transitions_path=None,
            engine_heartbeat_path=tmp_path / "heartbeat.json",
            expected_account_user_id="account-1",
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
    base_per_position = strategy.gross_exposure / strategy.max_concurrent_positions
    assert target_long_order_notional_pct_equity(_effective_long_config(notional_multiplier=10.0)) == pytest.approx(
        base_per_position * 10.0
    )
    assert target_long_order_notional_pct_equity(_effective_long_config(notional_multiplier=5.0)) == pytest.approx(
        base_per_position * 5.0
    )
    assert target_long_order_notional_pct_equity(
        _effective_long_config(order_notional_pct_equity=0.5)
    ) == pytest.approx(0.5)


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


def test_long_candidate_carries_the_reducers_exact_dollar_target() -> None:
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    equity = 12_345.67
    effective = resolve_strategy_config(
        "v11a",
        rule=strategy,
        layers=(
            ConfigLayer(
                source="test",
                values={"notional_multiplier": 2.0, "round_trip_cost_bps": 11.0},
            ),
        ),
    )
    candidates, _ = _select_long_entry_candidates(
        features=_build_features_with_fc_signal(
            symbol="BTCUSDT",
            signal_ts_ms=signal_ts,
            signal_close=100.0,
        ),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=effective.max_new_entries_per_cycle,
        effective_config=effective,
        equity_usdt=equity,
    )

    (candidate,) = candidates
    assert candidate["target_notional_usdt"] == pytest.approx(equity * candidate["target_fraction_of_equity"])

    state, _ = lnd._advance_long_book_state(
        lnd.LongBookState(),
        exit_plans=[],
        candidates=candidates,
        price_by_symbol={"BTCUSDT": 98.5},
        strategy_id=strategy.execution_strategy_id,
        now_ms=now,
        cooldown_days=int(strategy.cooldown_days),
        held_symbols=frozenset(),
    )
    assert state.held["BTCUSDT"].notional_usdt == pytest.approx(candidate["target_notional_usdt"])


def test_blocked_and_attempted_names_do_not_spend_batch_or_capacity() -> None:
    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    frames = []
    for symbol, score in (
        ("BLOCKEDUSDT", 0.30),
        ("ATTEMPTEDUSDT", 0.25),
        ("READYUSDT", 0.20),
    ):
        frames.append(
            _build_features_with_fc_signal(
                symbol=symbol,
                signal_ts_ms=signal_ts,
                signal_close=100.0,
            ).with_columns(pl.lit(math.log1p(score)).alias("log_return"))
        )

    candidates, skips = _select_long_entry_candidates(
        features=pl.concat(frames, how="vertical_relaxed"),
        all_trades=pl.DataFrame(),
        now_ms=now,
        strategy=strategy,
        price_by_symbol={symbol: 98.5 for symbol in ("BLOCKEDUSDT", "ATTEMPTEDUSDT", "READYUSDT")},
        max_new_entries=1,
        attempted_signals_ms={"ATTEMPTEDUSDT": signal_ts},
        blocked_symbols=frozenset({"BLOCKEDUSDT"}),
        active_positions=strategy.max_concurrent_positions - 1,
    )

    assert [candidate["symbol"] for candidate in candidates] == ["READYUSDT"]
    assert skips["engine_blocked"] == 1
    assert skips["already_attempted"] == 1
    assert skips["capacity"] == 0


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


def test_pending_long_book_entry_reserves_admission_but_is_not_an_open_position() -> None:
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
                "status": "pending",
                "qty": "0.001",
                "max_hold_deadline_ts_ms": 0,
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
            "engine_account_health_error": "",
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


def test_long_daemon_cycle_call_uses_only_the_effective_config() -> None:
    import inspect

    from liquidity_migration.strategy.long_native_event_demo import (
        run_long_native_demo_cycle,
    )
    from liquidity_migration.strategy.long_native_event_demo_daemon import (
        LongNativeDemoDaemon,
    )

    daemon = object.__new__(LongNativeDemoDaemon)
    effective = object()
    daemon._long_target_producer = True
    daemon._effective_config = effective  # type: ignore[assignment]
    shared = {
        "now_ms": 1,
        "kline_store": None,
        "ticker_cache": None,
        "state_cache_stale_seconds": 120.0,
    }

    kwargs = daemon._cycle_call_kwargs(shared)

    assert kwargs == {
        "now_ms": 1,
        "kline_store": None,
        "ticker_cache": None,
        "effective_config": effective,
    }
    inspect.signature(run_long_native_demo_cycle).bind(**kwargs)
    assert "config" not in kwargs
    assert "demo_config" not in kwargs


@pytest.mark.parametrize("environment", ["demo", "mainnet"])
def test_long_daemon_dispatch_matches_cycle_runner_signature(
    tmp_path: Path,
    environment: str,
) -> None:
    from liquidity_migration.rules.engine_targets import render_target_book, write_target_book
    from liquidity_migration.strategy.long_native_event_demo import LongRuntimeConfig
    from liquidity_migration.strategy.long_native_event_demo_daemon import LongNativeDemoDaemon
    from liquidity_migration.strategy.strategy_event_clock import StrategyEvent
    from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload

    root = tmp_path / environment
    book = root / "long.json"
    write_target_book(
        book,
        render_target_book(
            source=f"long-{environment}",
            decision_ts_ms=1_000,
            valid_until_ms=61_000,
            targets=[],
        ),
    )
    cycle = LongNativeDemoCycleConfig(execution_environment=environment)
    effective = lnd.resolve_long_effective_config(
        cycle,
        runtime=LongRuntimeConfig(data_root=root, state_cache_stale_seconds=37.0),
        strategy=_effective_long_config(),
        exchange=ResearchConfig().exchange,
        exchange_source="test",
        operational_profile_source="test",
        operational_profile_sha256="11" * 32,
        target_book_path=book,
        book_state_path=root / "state.json",
        book_transitions_path=None,
        engine_heartbeat_path=root / "heartbeat.json",
        expected_account_user_id=f"{environment}-account",
    )

    class _KlineManager:
        store_value = object()

        def store(self) -> object:
            return self.store_value

    received: dict[str, object] = {}

    def runner(
        *,
        effective_config: object,
        now_ms: int,
        kline_store: object,
        ticker_cache: object,
    ) -> PublishedTargetCyclePayload:
        received.update(
            effective_config=effective_config,
            now_ms=now_ms,
            kline_store=kline_store,
            ticker_cache=ticker_cache,
        )
        return PublishedTargetCyclePayload(
            {"cycle": {"cycle_id": f"{environment}-cycle"}},
            target_book_path=book,
        )

    manager = _KlineManager()
    daemon = LongNativeDemoDaemon(
        effective_config=effective,
        cycle_runner=runner,
        kline_stream_manager=manager,
    )
    event = StrategyEvent(
        event_ts_ns=1_000_000_000,
        ingest_ts_ns=1_000_000_000,
        source=f"long:{environment}",
        source_sequence=1,
        kind="startup",
        payload={
            "execution_environment": environment,
            "strategy_profile": effective.strategy.profile_name,
        },
    )

    payload = daemon._execute_cycle_event(event)

    assert payload is not None
    assert received == {
        "effective_config": effective,
        "now_ms": 1_000,
        "kline_store": manager.store_value,
        "ticker_cache": daemon._ticker_cache,
    }
    assert daemon._cycles_run == 1
    assert daemon._cycle_errors == 0


def test_long_daemon_refuses_a_missing_effective_config() -> None:
    from liquidity_migration.strategy.long_native_event_demo_daemon import (
        LongNativeDemoDaemon,
    )

    daemon = object.__new__(LongNativeDemoDaemon)
    daemon._long_target_producer = True
    daemon._effective_config = None

    with pytest.raises(RuntimeError, match="effective configuration"):
        daemon._cycle_call_kwargs({})
    with pytest.raises(RuntimeError, match="effective configuration"):
        daemon._strategy_profile_name()
    with pytest.raises(RuntimeError, match="effective configuration"):
        daemon._sizing_summary()


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

    effective = _effective_long_config()
    strategy = effective.rule
    # ts_ms out of order with an interleaved null — the helper must sort then take the last non-null.
    features = pl.DataFrame({"ts_ms": [3, 1, 2], "btc_rv_30": [0.9, None, 0.4]})
    notional, scale = _compute_long_order_sizing(config=effective, features=features)
    expected_scale = _vol_target_scale(strategy, 0.9)  # latest by ts_ms (ts=3)
    assert scale == expected_scale
    assert notional == pytest.approx(target_long_order_notional_pct_equity(effective) * expected_scale)

    # No btc_rv_30 column -> latest_btc_rv is None -> the None vol-target path (no de-risk-up).
    bare = pl.DataFrame({"ts_ms": [1, 2]})
    n0, s0 = _compute_long_order_sizing(config=effective, features=bare)
    assert s0 == _vol_target_scale(strategy, None)
    assert n0 == pytest.approx(target_long_order_notional_pct_equity(effective) * s0)


def test_compute_long_order_sizing_uses_latest_closed_btc_rv_when_clocked() -> None:
    from liquidity_migration.rules.long_native import _vol_target_scale
    from liquidity_migration.strategy.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    effective = _effective_long_config()
    strategy = effective.rule
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
        config=effective,
        features=features,
        now_ms=now,
    )

    expected_scale = _vol_target_scale(strategy, 1.20)
    assert scale == pytest.approx(expected_scale)
    assert scale != pytest.approx(_vol_target_scale(strategy, 0.10))
    assert notional == pytest.approx(target_long_order_notional_pct_equity(effective) * expected_scale)


def test_compute_long_order_sizing_falls_back_when_only_unclosed_btc_rv_exists() -> None:
    from liquidity_migration.rules.long_native import _vol_target_scale
    from liquidity_migration.strategy.long_native_event_demo import (
        _compute_long_order_sizing,
        target_long_order_notional_pct_equity,
    )

    effective = _effective_long_config()
    strategy = effective.rule
    now = 1_700_000_000_000
    current_day_start = now - (now % MS_PER_DAY)
    features = pl.DataFrame(
        {
            "ts_ms": [current_day_start + MS_PER_DAY],
            "btc_rv_30": [0.10],
        }
    )

    notional, scale = _compute_long_order_sizing(
        config=effective,
        features=features,
        now_ms=now,
    )

    expected_scale = _vol_target_scale(strategy, None)
    assert scale == pytest.approx(expected_scale)
    assert notional == pytest.approx(target_long_order_notional_pct_equity(effective) * expected_scale)


def test_registered_long_profile_carries_live_kernel_identity_and_leverage() -> None:
    strategy = long_v11a_profile()
    assert strategy.execution_strategy_id == LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    assert _effective_long_config().entry_leverage == 10.0


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


# --------------------------------------------------------------------------- #
# The cycle context a fast wake needs                                           #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Price levels the cycle asks to be woken for                                   #
# --------------------------------------------------------------------------- #
def test_a_candidate_still_waiting_for_its_retrace_reports_the_price() -> None:
    """The level the entry is waiting for is exactly the published threshold."""

    strategy = long_v11a_profile()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
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
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
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
        "stop_loss_pct": 0.15,
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
    assert "take_profit_pct" not in candidate


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


def test_plan_decayed_stop_fires_only_after_decay_age_and_breach() -> None:
    now = 2_000_000_000_000
    entry_price = 100.0
    aged_entry = now - 50 * MS_PER_HOUR
    young_entry = now - 47 * MS_PER_HOUR
    decayed_level = entry_price * (1.0 - 0.075)  # 92.5

    aged_and_breached = pl.DataFrame([_open_trade_row(entry_ts_ms=aged_entry, entry_price=entry_price)])
    plans = _plan_time_stop_exits(aged_and_breached, now_ms=now, price_by_symbol={"ABCUSDT": 92.0})
    assert [p["exit_reason"] for p in plans] == ["decayed_stop_loss"]
    assert plans[0]["decayed_stop_price"] == pytest.approx(decayed_level)
    assert plans[0]["stop_decay_deadline_ts_ms"] == aged_entry + 48 * MS_PER_HOUR
    assert plans[0]["decision_reference_price"] == pytest.approx(92.0)

    # At the level exactly: breach (research convention is <=).
    at_level = _plan_time_stop_exits(aged_and_breached, now_ms=now, price_by_symbol={"ABCUSDT": decayed_level})
    assert [p["exit_reason"] for p in at_level] == ["decayed_stop_loss"]

    # Above the decayed level: no exit.
    assert _plan_time_stop_exits(aged_and_breached, now_ms=now, price_by_symbol={"ABCUSDT": 93.0}) == []

    # Breached but younger than the decay age: no exit.
    young = pl.DataFrame([_open_trade_row(entry_ts_ms=young_entry, entry_price=entry_price)])
    assert _plan_time_stop_exits(young, now_ms=now, price_by_symbol={"ABCUSDT": 92.0}) == []


def test_plan_decayed_stop_uses_the_current_venue_average_after_resize() -> None:
    now = 2_000_000_000_000
    trade = pl.DataFrame(
        [
            _open_trade_row(
                entry_ts_ms=now - 50 * MS_PER_HOUR,
                entry_price=100.0,
            )
        ]
    )
    holdings = {"ABCUSDT": ("long", 10.0, 90.0)}

    assert (
        _plan_time_stop_exits(
            trade,
            now_ms=now,
            price_by_symbol={"ABCUSDT": 84.0},
            venue_holdings=holdings,
        )
        == []
    )
    plans = _plan_time_stop_exits(
        trade,
        now_ms=now,
        price_by_symbol={"ABCUSDT": 83.0},
        venue_holdings=holdings,
    )
    assert [plan["exit_reason"] for plan in plans] == ["decayed_stop_loss"]
    assert plans[0]["decayed_stop_price"] == pytest.approx(90.0 * (1.0 - 0.075))
    assert plans[0]["stop_price"] == pytest.approx(90.0 * (1.0 - 0.075))
    assert plans[0]["stop_anchor_price"] == pytest.approx(90.0)


def test_plan_base_stop_has_base_fields_and_uses_current_venue_average() -> None:
    now = 2_000_000_000_000
    trade = pl.DataFrame(
        [
            _open_trade_row(
                strategy_id=LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
                entry_ts_ms=now - 2 * MS_PER_HOUR,
                entry_price=100.0,
                with_decay_contract=False,
            )
        ]
    )

    plans = _plan_time_stop_exits(
        trade,
        now_ms=now,
        price_by_symbol={"ABCUSDT": 76.0},
        venue_holdings={"ABCUSDT": ("long", 10.0, 90.0)},
    )

    assert [plan["exit_reason"] for plan in plans] == ["stop_loss"]
    assert plans[0]["stop_anchor_price"] == pytest.approx(90.0)
    assert plans[0]["stop_loss_pct"] == pytest.approx(0.15)
    assert plans[0]["stop_price"] == pytest.approx(76.5)
    assert "decayed_stop_price" not in plans[0]
    assert "decayed_stop_loss_pct" not in plans[0]
    assert "stop_decay_deadline_ts_ms" not in plans[0]


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
    assert _plan_time_stop_exits(v11a, now_ms=now, price_by_symbol={"ABCUSDT": 90.0}) == []


def test_plan_decayed_stop_requires_fill_anchor_and_live_price() -> None:
    now = 2_000_000_000_000
    no_anchor = pl.DataFrame([_open_trade_row(entry_ts_ms=None, entry_price=None)])
    assert _plan_time_stop_exits(no_anchor, now_ms=now, price_by_symbol={"ABCUSDT": 1.0}) == []
    anchored = pl.DataFrame([_open_trade_row(entry_ts_ms=now - 50 * MS_PER_HOUR, entry_price=100.0)])
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
    features = _build_features_with_fc_signal(symbol="AAAUSDT", signal_ts_ms=signal_ts).with_columns(
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
            _long_engine_target_book(
                state,
                decision_ts_ms=at,
                strategy_profile="long_v12",
                effective_config=resolve_strategy_config("v12"),
            )
        )
        (target,) = book["targets"]
        assert target["stop_loss_fraction"] == 0.15

    def test_pending_book_refresh_keeps_the_original_entry_deadline(self) -> None:
        import json as _json

        from liquidity_migration.strategy.long_book_state import LongBookState
        from liquidity_migration.strategy.long_native_event_demo import _long_engine_target_book

        deadline = 1_700_000_000_000 + exact_duration_ms(hours=1)
        entry = self._entry(
            entered_ts_ms=0,
            seen_held=False,
            requested_ts_ms=1_700_000_000_000,
            entry_valid_until_ms=deadline,
        )
        refreshed_at = deadline - exact_duration_ms(minutes=20)
        book = _json.loads(
            _long_engine_target_book(
                LongBookState(held={entry.symbol: entry}),
                decision_ts_ms=refreshed_at,
                strategy_profile="long_v12",
                effective_config=resolve_strategy_config("v12"),
            )
        )

        assert book["version"] == 2
        assert book["targets"][0]["entry_valid_until_ms"] == deadline
