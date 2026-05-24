from __future__ import annotations


import polars as pl
import pytest

from liquidity_migration.momentum_events import (
    EXIT_RANK_DECAY,
    EXIT_REGIME_BREAK,
    EXIT_TRAILING_ATR,
    EXIT_TREND_BREAK,
    EXIT_UNIVERSE_DEMOTION,
    EXIT_VOL_SHOCK,
    MomentumEventsConfig,
    detect_entry_events,
    exit_reason_for_position,
)


def _features_row(**overrides) -> dict:
    """Default 'all conditions met for entry' feature row, mutable."""
    base = {
        "ts_ms": 1,
        "symbol": "AAA",
        "close": 100.0,
        "high": 100.0,
        "low": 99.0,
        "log_return": 0.01,
        "in_liquidity_tier": True,
        "coil_release_event": True,
        "rank_norm": 0.80,
        "prior_high_60d": 99.0,
        "sma_100d": 95.0,
        "abs_return_median_30d": 0.02,
        "atr_30d": 3.0,
        "regime_on": True,
        "funding_overheat": False,
    }
    base.update(overrides)
    return base


def _features_df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_entry_fires_when_all_conditions_met():
    df = _features_df([_features_row()])
    out = detect_entry_events(df)
    assert out.height == 1
    assert out["symbol"].to_list() == ["AAA"]


def test_entry_blocked_by_no_coil_release():
    df = _features_df([_features_row(coil_release_event=False)])
    assert detect_entry_events(df).is_empty()


def test_entry_blocked_below_rank_threshold():
    df = _features_df([_features_row(rank_norm=0.50)])
    assert detect_entry_events(df).is_empty()


def test_entry_blocked_without_breakout():
    df = _features_df([_features_row(close=98.0, prior_high_60d=99.0)])
    assert detect_entry_events(df).is_empty()


def test_entry_blocked_when_regime_off():
    df = _features_df([_features_row(regime_on=False)])
    assert detect_entry_events(df).is_empty()


def test_entry_blocked_when_funding_overheated():
    df = _features_df([_features_row(funding_overheat=True)])
    assert detect_entry_events(df).is_empty()


def test_entry_blocked_when_not_in_tier():
    df = _features_df([_features_row(in_liquidity_tier=False)])
    assert detect_entry_events(df).is_empty()


def test_entry_funding_check_can_be_disabled():
    cfg = MomentumEventsConfig(require_funding_not_overheated=False)
    df = _features_df([_features_row(funding_overheat=True)])
    assert detect_entry_events(df, config=cfg).height == 1


def test_entry_regime_check_can_be_disabled():
    cfg = MomentumEventsConfig(require_regime_on_entry=False)
    df = _features_df([_features_row(regime_on=False)])
    assert detect_entry_events(df, config=cfg).height == 1


def test_entry_raises_on_missing_columns():
    df = pl.DataFrame({"ts_ms": [1], "symbol": ["AAA"]})
    with pytest.raises(RuntimeError):
        detect_entry_events(df)


def test_exit_regime_break_wins_over_others():
    row = _features_row(regime_on=False, in_liquidity_tier=False, rank_norm=0.10)
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason == EXIT_REGIME_BREAK


def test_exit_universe_demotion_when_regime_on():
    row = _features_row(in_liquidity_tier=False)
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason == EXIT_UNIVERSE_DEMOTION


def test_exit_trend_break_fires_under_sma():
    row = _features_row(close=90.0, sma_100d=95.0)
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason == EXIT_TREND_BREAK


def test_exit_rank_decay_below_median():
    row = _features_row(rank_norm=0.30, close=100.0, sma_100d=90.0)
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason == EXIT_RANK_DECAY


def test_exit_vol_shock_on_extreme_move():
    row = _features_row(log_return=0.20, abs_return_median_30d=0.02)
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason == EXIT_VOL_SHOCK


def test_exit_trailing_atr_on_giveback():
    row = _features_row(close=90.0, atr_30d=3.0, sma_100d=80.0, rank_norm=0.95)
    # high_water = 110, threshold = 110 - 4*3 = 98. close 90 < 98 → trailing exit.
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason == EXIT_TRAILING_ATR


def test_exit_hold_when_all_clear():
    row = _features_row()
    reason = exit_reason_for_position(row, high_water_close=100.0)
    assert reason is None


def test_exit_none_when_atr_threshold_not_breached():
    row = _features_row(close=104.0, atr_30d=2.0)
    # high_water = 110, threshold = 110 - 8 = 102. close 104 >= 102 → hold.
    reason = exit_reason_for_position(row, high_water_close=110.0)
    assert reason is None


def test_exit_ignores_nan_log_return():
    row = _features_row(log_return=float("nan"))
    reason = exit_reason_for_position(row, high_water_close=100.0)
    assert reason is None
