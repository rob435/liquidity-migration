"""Event-demo planning tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.event_demo import (
    plan_demo_exits,
    plan_risk_exits,
    plan_stop_repairs,
    select_demo_entry_candidates,
)
from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.volume_events import EventScenario, VolumeEventResearchConfig

from _event_demo_fixtures import *  # noqa: F401,F403  (shared fakes/helpers)
from _event_demo_fixtures import (  # noqa: F401  explicit for the linters
    FailingKlineMarket,
    FakeKlineMarket,
    FakeRiskClient,
    MinimalEventMarket,
    _ClosedPnlClient,
    _RecordingInstrumentsMarket,
    _feature_cache_klines,
    _feature_cache_universe,
    _make_instruments_frame,
    _make_tickers_frame,
    _open_trade_row,
    _patch_minimal_event_cycle,
)


def test_select_demo_entry_candidates_uses_selected_liquidity_migration_filters() -> None:
    signal_ts = 1_700_000_000_000
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.20,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_close_location_min=0.0,
        liquidity_migration_pit_age_days_min=0,
        liquidity_migration_crowding_filter="none",
    )
    features = pl.DataFrame(
        [
            {
                "ts_ms": signal_ts,
                "symbol": "AAAUSDT",
                "dollar_volume_rank_z": 2.0,
                "dollar_volume_rank_z_rank_frac": 0.85,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 225,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "tradable_membership_flag": False,
            },
            {
                "ts_ms": signal_ts,
                "symbol": "BBBUSDT",
                "dollar_volume_rank_z": 2.5,
                "dollar_volume_rank_z_rank_frac": 0.95,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 60,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 8_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "tradable_membership_flag": False,
            },
        ]
    )

    candidates, skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + MS_PER_HOUR + 5 * 60_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=180,
        max_new_entries=6,
    )

    assert [row["symbol"] for row in candidates] == ["AAAUSDT"]
    assert candidates[0]["side"] == "short"
    assert candidates[0]["stop_loss_pct"] == 0.12
    assert candidates[0]["take_profit_pct"] == 0.20
    assert skips["not_ready"] == 0


def test_select_demo_entry_candidates_waits_for_quality_squeeze_giveback() -> None:
    signal_ts = 1_700_000_000_000
    hour = MS_PER_HOUR
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.40,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.25,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_crowding_filter="none",
    )
    features = pl.DataFrame(
        [
            {
                "ts_ms": signal_ts,
                "symbol": "AAAUSDT",
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.85,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 225,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "signal_day_close_location": 0.70,
                "pit_age_days": 120.0,
                "tradable_membership_flag": False,
            }
        ]
    )
    bar_dicts = [
        {"bar_end_ts_ms": signal_ts, "high": 101.0, "low": 99.0, "close": 100.0},
        {"bar_end_ts_ms": signal_ts + hour, "high": 101.2, "low": 100.0, "close": 101.1},
        {"bar_end_ts_ms": signal_ts + 2 * hour, "high": 101.6, "low": 101.0, "close": 101.5},
        {"bar_end_ts_ms": signal_ts + 3 * hour, "high": 101.6, "low": 100.9, "close": 101.1},
    ]
    ends = [int(b["bar_end_ts_ms"]) for b in bar_dicts]
    bars: dict[str, dict[str, Any]] = {
        "AAAUSDT": {
            "ts_ms": np.array([end - hour for end in ends], dtype=np.int64),
            "bar_end_ts_ms": np.array(ends, dtype=np.int64),
            "open": np.array([b["close"] for b in bar_dicts], dtype=np.float64),
            "high": np.array([b["high"] for b in bar_dicts], dtype=np.float64),
            "low": np.array([b["low"] for b in bar_dicts], dtype=np.float64),
            "close": np.array([b["close"] for b in bar_dicts], dtype=np.float64),
            "ends": ends,
            "by_end": {end: idx for idx, end in enumerate(ends)},
        }
    }

    pending_candidates, pending_skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + 2 * hour + 30_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=240,
        max_new_entries=6,
        entry_bars_by_symbol=bars,
    )
    ready_candidates, ready_skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + 3 * hour + 30_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=240,
        max_new_entries=6,
        entry_bars_by_symbol=bars,
    )

    assert pending_candidates == []
    assert pending_skips["not_ready"] == 1
    assert ready_skips["not_ready"] == 0
    assert len(ready_candidates) == 1
    assert ready_candidates[0]["entry_ready_ts_ms"] == signal_ts + 3 * hour
    assert ready_candidates[0]["entry_rule"] == "quality_squeeze_giveback"
    assert ready_candidates[0]["entry_quality_tier"] == "promoted_quality"
    assert ready_candidates[0]["actual_entry_delay_hours"] == 3.0


def test_select_demo_entry_candidates_builds_entry_bars_from_klines() -> None:
    signal_ts = 1_700_000_000_000
    hour = MS_PER_HOUR
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.40,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.25,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_crowding_filter="none",
    )
    features = pl.DataFrame(
        [
            {
                "ts_ms": signal_ts,
                "symbol": "AAAUSDT",
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.85,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 225,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "signal_day_close_location": 0.70,
                "pit_age_days": 120.0,
                "tradable_membership_flag": False,
            }
        ]
    )
    klines = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": signal_ts - hour, "open": 99.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"symbol": "AAAUSDT", "ts_ms": signal_ts, "open": 100.0, "high": 101.2, "low": 100.0, "close": 101.1},
            {
                "symbol": "AAAUSDT",
                "ts_ms": signal_ts + hour,
                "open": 101.1,
                "high": 101.6,
                "low": 101.0,
                "close": 101.5,
            },
            {
                "symbol": "AAAUSDT",
                "ts_ms": signal_ts + 2 * hour,
                "open": 101.5,
                "high": 101.6,
                "low": 100.9,
                "close": 101.1,
            },
            {"symbol": "ZZZUSDT", "ts_ms": signal_ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        ]
    )

    candidates, skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + 3 * hour + 30_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=240,
        max_new_entries=6,
        klines=klines,
    )

    assert skips["not_ready"] == 0
    assert len(candidates) == 1
    assert candidates[0]["entry_ready_ts_ms"] == signal_ts + 3 * hour
    assert candidates[0]["entry_rule"] == "quality_squeeze_giveback"


def test_select_demo_entry_candidates_dedupes_same_symbol_in_cycle() -> None:
    """A symbol with two un-traded events in the lookback window must yield
    only one candidate; otherwise _execute_entries' ThreadPoolExecutor would
    fan out two concurrent place_order calls for the same symbol."""
    earlier_ts = 1_700_000_000_000
    later_ts = earlier_ts + 2 * MS_PER_HOUR
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.20,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_close_location_min=0.0,
        liquidity_migration_pit_age_days_min=0,
        liquidity_migration_crowding_filter="none",
    )
    base_row = {
        "symbol": "AAAUSDT",
        "dollar_volume_rank_z": 2.5,
        "dollar_volume_rank_z_rank_frac": 0.90,
        "prior7_dollar_volume_rank_z_rank_frac": 0.20,
        "liquidity_rank": 55,
        "prior7_liquidity_rank": 230,
        "turnover_quote": 8_000_000.0,
        "prior7_turnover_quote_mean": 1_000_000.0,
        "daily_return_1d": 0.02,
        "residual_return_1d": 0.09,
        "market_pct_up_1d": 0.55,
        "tradable_membership_flag": False,
    }
    features = pl.DataFrame(
        [
            {**base_row, "ts_ms": earlier_ts},
            {**base_row, "ts_ms": later_ts},
        ]
    )

    candidates, skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=later_ts + MS_PER_HOUR + 5 * 60_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=360,
        max_new_entries=6,
    )

    assert [row["symbol"] for row in candidates] == ["AAAUSDT"]
    assert skips["duplicate_symbol"] == 1
    # Sort order is (ts_ms ASC, event_rank ASC, symbol ASC); the earlier
    # event wins the slot, the later one is dropped as a duplicate.
    assert candidates[0]["signal_ts_ms"] == earlier_ts


def test_plan_demo_exits_detects_rank_decay_before_max_hold() -> None:
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
    )
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": 1_000,
                "planned_exit_ts_ms": 1_000 + 24 * MS_PER_HOUR,
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    exits = plan_demo_exits(
        open_trades,
        rank_lookup={("AAAUSDT", 1_000 + MS_PER_HOUR): 0.69},
        klines=pl.DataFrame(),
        price_by_symbol={"AAAUSDT": 99.0},
        now_ms=1_000 + MS_PER_HOUR,
        config=VolumeEventResearchConfig(require_pit_membership=False, require_full_pit_universe=False),
        scenario=scenario,
    )

    assert exits == [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "qty": "1",
            "exit_reason": "event_decay",
            "exit_trigger_ts_ms": 1_000 + MS_PER_HOUR,
            "planned_exit_price": 99.0,
            "planned_exit_ts_ms": 1_000 + 24 * MS_PER_HOUR,
        }
    ]


def test_plan_demo_exits_detects_take_profit_before_max_hold() -> None:
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.20,
    )
    entry_ts = 1_700_000_000_000
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": entry_ts,
                "planned_exit_ts_ms": entry_ts + 24 * MS_PER_HOUR,
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )
    klines = pl.DataFrame(
        [
            {
                "ts_ms": entry_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 101.0,
                "low": 79.0,
                "close": 81.0,
            }
        ]
    )

    exits = plan_demo_exits(
        open_trades,
        rank_lookup={},
        klines=klines,
        price_by_symbol={"AAAUSDT": 81.0},
        now_ms=entry_ts + MS_PER_HOUR,
        config=VolumeEventResearchConfig(require_pit_membership=False, require_full_pit_universe=False),
        scenario=scenario,
    )

    assert exits[0]["exit_reason"] == "take_profit"
    assert exits[0]["planned_exit_price"] == 80.0


def test_plan_demo_exits_detects_failed_fade_on_completed_bar() -> None:
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.0,
    )
    entry_ts = 1_700_000_000_000
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": entry_ts,
                "entry_price": 100.0,
                "planned_exit_ts_ms": entry_ts + 24 * MS_PER_HOUR,
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 0.0,
            }
        ]
    )
    klines = pl.DataFrame(
        [
            {"ts_ms": entry_ts, "symbol": "AAAUSDT", "open": 100.0, "high": 101.2, "low": 99.7, "close": 101.0},
            {
                "ts_ms": entry_ts + MS_PER_HOUR,
                "symbol": "AAAUSDT",
                "open": 101.0,
                "high": 103.0,
                "low": 100.5,
                "close": 102.8,
            },
        ]
    )

    exits = plan_demo_exits(
        open_trades,
        rank_lookup={},
        klines=klines,
        price_by_symbol={"AAAUSDT": 102.8},
        now_ms=entry_ts + 2 * MS_PER_HOUR,
        config=VolumeEventResearchConfig(
            require_pit_membership=False,
            require_full_pit_universe=False,
            failed_fade_exit_hours=2,
            failed_fade_min_mfe_pct=0.005,
            failed_fade_loss_pct=0.025,
            failed_fade_close_location_min=0.85,
        ),
        scenario=scenario,
    )

    assert exits[0]["exit_reason"] == "failed_fade"
    assert exits[0]["exit_trigger_ts_ms"] == entry_ts + 2 * MS_PER_HOUR
    assert exits[0]["planned_exit_price"] == 102.8


def test_plan_risk_exits_uses_live_position_price_for_stops() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            },
            {
                "trade_id": "t2",
                "symbol": "BBBUSDT",
                "side": "short",
                "status": "open",
                "qty": "2",
                "stop_price": 112.0,
                "planned_exit_ts_ms": 1_700_000_000_000,
            },
        ]
    )

    exits = plan_risk_exits(
        open_trades,
        position_by_symbol={
            "AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "markPrice": "113"},
            "BBBUSDT": {"symbol": "BBBUSDT", "side": "Sell", "size": "2", "markPrice": "99"},
        },
        price_by_symbol={"AAAUSDT": 113.0, "BBBUSDT": 99.0},
        now_ms=1_700_000_060_000,
    )

    assert [row["exit_reason"] for row in exits] == ["stop_loss", "max_hold"]
    assert exits[0]["qty"] == "1"
    assert exits[0]["planned_exit_price"] == 113.0


def test_plan_stop_repairs_detects_missing_exchange_stop() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )

    repairs = plan_stop_repairs(
        open_trades,
        position_by_symbol={
            "AAAUSDT": {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "stopLoss": "",
                "takeProfit": "80.0001",
            }
        },
        tolerance_bps=1.0,
    )

    assert len(repairs) == 1
    assert repairs[0]["needs_stop_repair"] is True
    assert repairs[0]["needs_take_profit_repair"] is False

