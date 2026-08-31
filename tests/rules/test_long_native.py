"""The registered LONG profiles, selector, and persisted identities.

The historical-runner tests for the same rule stay in
``tests/research/backtest/test_long_native_active_profile.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    long_v11a_profile,
    long_v12_profile,
    resolve_long_strategy_profile,
)


def test_active_profile_has_no_legacy_strategy_switches() -> None:
    field_names = {field.name for field in dataclasses.fields(LongNativeConfig)}
    assert field_names == {
        "execution_strategy_id",
        "start_date",
        "end_date",
        "universe_size",
        "universe_volume_window_days",
        "min_listing_history_days",
        "exclude_symbols",
        "regime_symbol",
        "regime_sma_days",
        "fc_min_day_return",
        "fc_top_volume_rank_max",
        "fc_min_close_location",
        "fc_max_hold_days",
        "fc_max_atr_pct",
        "fc_atr_stop_mult",
        "fc_sigma_mult",
        "fc_sniper_retrace_pct",
        "fc_sniper_deadline_hours",
        "weekend_size_mult",
        "fc_close_loc_multi_day",
        # v12 stop geometry, consumed by _scan_position_exit
        "fc_stop_time_decay_hours",
        "fc_stop_time_decay_atr_mult",
        "max_concurrent_positions",
        "cooldown_days",
        "entry_delay_hours",
        "gross_exposure",
        "vol_estimate_window_days",
        "vol_floor_annual",
        "max_position_weight",
        "vol_target_annual",
        "vol_target_min_scale",
        "vol_target_max_scale",
        "cost_multiplier",
    }
    with pytest.raises(TypeError):
        LongNativeConfig(enable_fomo_chase=False)  # type: ignore[call-arg]


def test_resolve_long_strategy_profile_maps_registered_names() -> None:
    assert resolve_long_strategy_profile("v11a") == long_v11a_profile()
    assert resolve_long_strategy_profile("v12") == long_v12_profile()
    # Selector normalization tolerates shell-style noise, nothing more.
    assert resolve_long_strategy_profile(" V12 ") == long_v12_profile()
    for invalid in ("", "v13", "wide", None):
        with pytest.raises(ValueError, match="unknown LONG strategy profile"):
            resolve_long_strategy_profile(invalid)  # type: ignore[arg-type]
