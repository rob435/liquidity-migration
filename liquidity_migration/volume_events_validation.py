"""Extracted from volume_events.py — see that module's docstring.

A cohesive slice of volume_events, split out to keep the hub readable.
Imports shared helpers from volume_events (the hub); the hub re-imports
this module's public names at the bottom so external callers
(`from liquidity_migration.volume_events import X`) keep working.
"""

from __future__ import annotations





# Splits live exclusively on VolumeEventResearchConfig.splits now (default ()).
# Whole-period reporting is the post-rebuild norm; pristine OOS is the forward
# demo/paper ledger, not a backtest window.

from .volume_events import (  # noqa: F401  (shared hub helpers)
    ENTRY_POLICIES,
    ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
    EVENT_TYPES,
    LIQUIDITY_MIGRATION_CROWDING_FILTERS,
    POSITION_WEIGHTINGS,
    SIDE_HYPOTHESES,
    VolumeEventResearchConfig,
)




def _validate_scenario_config(config: VolumeEventResearchConfig) -> None:
    unknown_events = sorted(set(config.event_types) - set(EVENT_TYPES))
    if unknown_events:
        raise ValueError(f"Unknown event type(s): {unknown_events}")
    unknown_sides = sorted(set(config.side_hypotheses) - set(SIDE_HYPOTHESES))
    if unknown_sides:
        raise ValueError(f"Unknown side hypothesis(es): {unknown_sides}")
    if any(not 0.0 < item <= 0.5 for item in config.thresholds):
        raise ValueError("thresholds must be in (0, 0.5]")
    if any(item <= 0 for item in config.hold_days):
        raise ValueError("hold days must be positive")
    if any(item < 0.0 or item >= 1.0 for item in config.stop_loss_pcts):
        raise ValueError("stop loss pcts must be in [0, 1)")
    if config.stop_fill_mode not in {"stop", "bar_extreme", "bar_extreme_capped"}:
        raise ValueError("stop_fill_mode must be stop, bar_extreme, or bar_extreme_capped")
    if not 0.0 <= config.stop_slippage_cap_pct < 1.0:
        raise ValueError("stop_slippage_cap_pct must be in [0, 1)")
    if any(item < 0.0 or item >= 1.0 for item in config.take_profit_pcts):
        raise ValueError("take profit pcts must be in [0, 1)")
    if any(item < 0.0 for item in config.cost_multipliers):
        raise ValueError("cost multipliers must be non-negative")

def _validate_exit_config(config: VolumeEventResearchConfig) -> None:
    if config.mfe_giveback_trigger_pct < 0.0 or config.mfe_giveback_trigger_pct >= 1.0:
        raise ValueError("mfe_giveback_trigger_pct must be in [0, 1)")
    if not 0.0 <= config.mfe_giveback_retain_pct <= 1.0:
        raise ValueError("mfe_giveback_retain_pct must be in [0, 1]")
    if config.mfe_giveback_retain_pct > 0.0 and config.mfe_giveback_trigger_pct <= 0.0:
        raise ValueError("mfe_giveback_trigger_pct must be positive when MFE giveback is enabled")
    if config.failed_fade_exit_hours < 0:
        raise ValueError("failed_fade_exit_hours must be non-negative")
    if not 0.0 <= config.failed_fade_min_mfe_pct < 1.0:
        raise ValueError("failed_fade_min_mfe_pct must be in [0, 1)")
    if not 0.0 <= config.failed_fade_loss_pct < 1.0:
        raise ValueError("failed_fade_loss_pct must be in [0, 1)")
    if not 0.0 <= config.failed_fade_close_location_min <= 1.0:
        raise ValueError("failed_fade_close_location_min must be in [0, 1]")
    if config.failed_fade_exit_hours > 0 and config.failed_fade_loss_pct <= 0.0:
        raise ValueError("failed_fade_loss_pct must be positive when failed fade exit is enabled")
    if not 0.0 <= config.breakeven_arm_pct < 1.0:
        raise ValueError("breakeven_arm_pct must be in [0, 1)")
    if not 0.0 <= config.profit_lock_arm_pct < 1.0:
        raise ValueError("profit_lock_arm_pct must be in [0, 1)")
    if not 0.0 <= config.profit_lock_floor_pct < 1.0:
        raise ValueError("profit_lock_floor_pct must be in [0, 1)")
    if config.profit_lock_arm_pct > 0.0 and config.profit_lock_floor_pct >= config.profit_lock_arm_pct:
        raise ValueError("profit_lock_floor_pct must be less than profit_lock_arm_pct")
    if config.stop_loose_window_hours < 0:
        raise ValueError("stop_loose_window_hours must be non-negative")
    if not 0.0 <= config.stop_loose_pct < 1.0:
        raise ValueError("stop_loose_pct must be in [0, 1)")
    if config.stop_loose_window_hours > 0 and config.stop_loose_pct <= 0.0:
        raise ValueError("stop_loose_pct must be positive when stop_loose_window_hours is set")

def _validate_entry_config(config: VolumeEventResearchConfig) -> None:
    if config.gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")
    if config.max_active_symbols <= 0:
        raise ValueError("max_active_symbols must be positive")
    if config.position_weighting not in POSITION_WEIGHTINGS:
        raise ValueError(f"position_weighting must be one of: {', '.join(POSITION_WEIGHTINGS)}")
    if config.position_weight_clamp < 1.0:
        raise ValueError("position_weight_clamp must be >= 1.0")
    if config.position_weighting == "risk_equal" and config.target_vol_per_name <= 0.0:
        raise ValueError("target_vol_per_name must be positive for risk_equal position weighting")
    if config.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    if config.entry_delay_hours < 0:
        raise ValueError("entry_delay_hours must be non-negative")
    if config.entry_policy not in ENTRY_POLICIES:
        raise ValueError(f"entry_policy must be one of: {', '.join(ENTRY_POLICIES)}")
    if config.entry_quality_squeeze_h1_return_bps < 0.0:
        raise ValueError("entry_quality_squeeze_h1_return_bps must be non-negative")
    if not 0.0 <= config.entry_quality_squeeze_h1_close_location_min <= 1.0:
        raise ValueError("entry_quality_squeeze_h1_close_location_min must be in [0, 1]")
    if config.entry_quality_squeeze_pop_bps < 0.0:
        raise ValueError("entry_quality_squeeze_pop_bps must be non-negative")
    if config.entry_quality_squeeze_giveback_bps < 0.0:
        raise ValueError("entry_quality_squeeze_giveback_bps must be non-negative")
    if (
        config.entry_policy == ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE
        and config.entry_quality_squeeze_wait_hours < max(config.entry_delay_hours, 1)
    ):
        raise ValueError("entry_quality_squeeze_wait_hours must be at least the first post-signal entry hour")
    if not 0.0 <= config.entry_execution_veto_close_location_max <= 1.0:
        raise ValueError("entry_execution_veto_close_location_max must be in [0, 1]")

def _validate_universe_config(config: VolumeEventResearchConfig) -> None:
    if not 0.0 < config.rank_exit_threshold <= 1.0:
        raise ValueError("rank_exit_threshold must be in (0, 1]")
    if config.universe_rank_min <= 0:
        raise ValueError("universe_rank_min must be positive")
    if config.universe_rank_max < 0:
        raise ValueError("universe_rank_max must be non-negative")
    if config.universe_min_daily_turnover < 0.0:
        raise ValueError("universe_min_daily_turnover must be non-negative")
    if config.tail_rank_min <= 0 or config.tail_rank_max <= 0:
        raise ValueError("tail rank bounds must be positive")
    if config.tail_rank_min > config.tail_rank_max:
        raise ValueError("tail_rank_min must be <= tail_rank_max")
    if config.tail_rank_improvement_min < 0:
        raise ValueError("tail_rank_improvement_min must be non-negative")

def _validate_liquidity_migration_config(config: VolumeEventResearchConfig) -> None:
    if config.liquidity_migration_rank_improvement_min < 0:
        raise ValueError("liquidity_migration_rank_improvement_min must be non-negative")
    if config.liquidity_migration_rank_direction not in ("improvement", "deterioration", "both"):
        raise ValueError(
            "liquidity_migration_rank_direction must be improvement|deterioration|both, "
            f"got {config.liquidity_migration_rank_direction!r}"
        )
    if config.liquidity_migration_turnover_ratio_min < 0.0:
        raise ValueError("liquidity_migration_turnover_ratio_min must be non-negative")
    if config.liquidity_migration_prior_rank_min < 0:
        raise ValueError("liquidity_migration_prior_rank_min must be non-negative")
    if config.liquidity_migration_current_rank_max < 0:
        raise ValueError("liquidity_migration_current_rank_max must be non-negative")
    if not 0.0 <= config.liquidity_migration_event_rank_fraction_max <= 1.0:
        raise ValueError("liquidity_migration_event_rank_fraction_max must be in [0, 1]")
    if not 0.0 <= config.liquidity_migration_event_rank_fraction_exclude_min <= 1.0:
        raise ValueError("liquidity_migration_event_rank_fraction_exclude_min must be in [0, 1]")
    if not 0.0 <= config.liquidity_migration_event_rank_fraction_exclude_max <= 1.0:
        raise ValueError("liquidity_migration_event_rank_fraction_exclude_max must be in [0, 1]")
    if (
        config.liquidity_migration_event_rank_fraction_exclude_min > 0.0
        or config.liquidity_migration_event_rank_fraction_exclude_max > 0.0
    ) and (
        config.liquidity_migration_event_rank_fraction_exclude_min
        >= config.liquidity_migration_event_rank_fraction_exclude_max
    ):
        raise ValueError(
            "liquidity_migration_event_rank_fraction_exclude_min must be < "
            "liquidity_migration_event_rank_fraction_exclude_max"
        )
    if config.liquidity_migration_score_max < 0.0:
        raise ValueError("liquidity_migration_score_max must be non-negative")
    if config.liquidity_migration_day_return_min > config.liquidity_migration_day_return_max:
        raise ValueError("liquidity_migration_day_return_min must be <= liquidity_migration_day_return_max")
    if config.liquidity_migration_return_7d_min > config.liquidity_migration_return_7d_max:
        raise ValueError("liquidity_migration_return_7d_min must be <= liquidity_migration_return_7d_max")
    if config.liquidity_migration_residual_return_min > config.liquidity_migration_residual_return_max:
        raise ValueError(
            "liquidity_migration_residual_return_min must be <= liquidity_migration_residual_return_max"
        )
    if config.liquidity_migration_close_to_high_7d_min > 0.0:
        raise ValueError("liquidity_migration_close_to_high_7d_min must be <= 0")
    if config.liquidity_migration_close_to_high_30d_min > 0.0:
        raise ValueError("liquidity_migration_close_to_high_30d_min must be <= 0")
    if config.liquidity_migration_prior30_max_return_min > config.liquidity_migration_prior30_max_return_max:
        raise ValueError(
            "liquidity_migration_prior30_max_return_min must be <= liquidity_migration_prior30_max_return_max"
        )
    if (
        config.liquidity_migration_prior7_return_volatility_min < 0.0
        or config.liquidity_migration_prior7_return_volatility_max < 0.0
    ):
        raise ValueError("liquidity_migration_prior7_return_volatility bounds must be non-negative")
    if config.liquidity_migration_prior7_return_volatility_min > config.liquidity_migration_prior7_return_volatility_max:
        raise ValueError(
            "liquidity_migration_prior7_return_volatility_min must be <= "
            "liquidity_migration_prior7_return_volatility_max"
        )
    if config.liquidity_migration_intraday_range_max < 0.0:
        raise ValueError("liquidity_migration_intraday_range_max must be non-negative")
    if config.liquidity_migration_funding_rate_last_min > config.liquidity_migration_funding_rate_last_max:
        raise ValueError("liquidity_migration_funding_rate_last_min must be <= liquidity_migration_funding_rate_last_max")
    if config.liquidity_migration_funding_3d_sum_min > config.liquidity_migration_funding_3d_sum_max:
        raise ValueError("liquidity_migration_funding_3d_sum_min must be <= liquidity_migration_funding_3d_sum_max")
    if config.liquidity_migration_funding_7d_sum_min > config.liquidity_migration_funding_7d_sum_max:
        raise ValueError("liquidity_migration_funding_7d_sum_min must be <= liquidity_migration_funding_7d_sum_max")
    if (
        config.liquidity_migration_open_interest_return_3d_min
        > config.liquidity_migration_open_interest_return_3d_max
    ):
        raise ValueError(
            "liquidity_migration_open_interest_return_3d_min must be <= "
            "liquidity_migration_open_interest_return_3d_max"
        )
    if (
        config.liquidity_migration_open_interest_return_7d_min
        > config.liquidity_migration_open_interest_return_7d_max
    ):
        raise ValueError(
            "liquidity_migration_open_interest_return_7d_min must be <= "
            "liquidity_migration_open_interest_return_7d_max"
        )
    if config.liquidity_migration_volume_to_oi_quote_min < 0.0 or config.liquidity_migration_volume_to_oi_quote_max < 0.0:
        raise ValueError("liquidity_migration_volume_to_oi_quote bounds must be non-negative")
    if (
        config.liquidity_migration_volume_to_oi_quote_max > 0.0
        and config.liquidity_migration_volume_to_oi_quote_min > config.liquidity_migration_volume_to_oi_quote_max
    ):
        raise ValueError("liquidity_migration_volume_to_oi_quote_min must be <= liquidity_migration_volume_to_oi_quote_max")
    if (
        config.liquidity_migration_mark_index_basis_3d_mean_min
        > config.liquidity_migration_mark_index_basis_3d_mean_max
    ):
        raise ValueError(
            "liquidity_migration_mark_index_basis_3d_mean_min must be <= "
            "liquidity_migration_mark_index_basis_3d_mean_max"
        )
    if (
        config.liquidity_migration_premium_index_3d_mean_min
        > config.liquidity_migration_premium_index_3d_mean_max
    ):
        raise ValueError(
            "liquidity_migration_premium_index_3d_mean_min must be <= "
            "liquidity_migration_premium_index_3d_mean_max"
        )
    if not -1.0 <= config.liquidity_migration_taker_imbalance_1d_min <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_1d_min must be in [-1, 1]")
    if not -1.0 <= config.liquidity_migration_taker_imbalance_1d_max <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_1d_max must be in [-1, 1]")
    if config.liquidity_migration_taker_imbalance_1d_min > config.liquidity_migration_taker_imbalance_1d_max:
        raise ValueError("liquidity_migration_taker_imbalance_1d_min must be <= liquidity_migration_taker_imbalance_1d_max")
    if not -1.0 <= config.liquidity_migration_taker_imbalance_3d_min <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_3d_min must be in [-1, 1]")
    if not -1.0 <= config.liquidity_migration_taker_imbalance_3d_max <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_3d_max must be in [-1, 1]")
    if config.liquidity_migration_taker_imbalance_3d_min > config.liquidity_migration_taker_imbalance_3d_max:
        raise ValueError("liquidity_migration_taker_imbalance_3d_min must be <= liquidity_migration_taker_imbalance_3d_max")
    if not 0.0 <= config.liquidity_migration_market_pct_up_max <= 1.0:
        raise ValueError("liquidity_migration_market_pct_up_max must be in [0, 1]")
    if config.liquidity_migration_hot_market_day_return_min < 0.0:
        raise ValueError("liquidity_migration_hot_market_day_return_min must be non-negative")
    if config.liquidity_migration_hot_market_day_return_band < 0.0:
        raise ValueError("liquidity_migration_hot_market_day_return_band must be non-negative")
    if config.liquidity_migration_hot_market_day_return_band > config.liquidity_migration_hot_market_day_return_min:
        raise ValueError("liquidity_migration_hot_market_day_return_band cannot exceed hot return minimum")
    if (
        not 0.0 <= config.liquidity_migration_close_location_min <= 1.0
        or not 0.0 <= config.liquidity_migration_close_location_max <= 1.0
        or config.liquidity_migration_close_location_min > config.liquidity_migration_close_location_max
    ):
        raise ValueError("liquidity_migration_close_location_min must be <= max and both in [0, 1]")
    if config.liquidity_migration_pit_age_days_min < 0:
        raise ValueError("liquidity_migration_pit_age_days_min must be non-negative")
    if config.liquidity_migration_pit_age_days_max < 0:
        raise ValueError("liquidity_migration_pit_age_days_max must be non-negative")
    if (
        config.liquidity_migration_pit_age_days_max > 0
        and config.liquidity_migration_pit_age_days_min > config.liquidity_migration_pit_age_days_max
    ):
        raise ValueError("liquidity_migration_pit_age_days_min must be <= liquidity_migration_pit_age_days_max")
    if config.liquidity_migration_crowding_filter not in LIQUIDITY_MIGRATION_CROWDING_FILTERS:
        raise ValueError("liquidity_migration_crowding_filter is unknown")
    if config.liquidity_migration_crowding_min_signals <= 0:
        raise ValueError("liquidity_migration_crowding_min_signals must be positive")

def _validate_per_event_config(config: VolumeEventResearchConfig) -> None:
    if config.market_median_return_1d_min > config.market_median_return_1d_max:
        raise ValueError("market_median_return_1d_min must be <= market_median_return_1d_max")
    if not 0.0 <= config.market_pct_up_1d_min <= 1.0:
        raise ValueError("market_pct_up_1d_min must be in [0, 1]")
    if not 0.0 <= config.market_pct_up_1d_max <= 1.0:
        raise ValueError("market_pct_up_1d_max must be in [0, 1]")
    if config.market_pct_up_1d_min > config.market_pct_up_1d_max:
        raise ValueError("market_pct_up_1d_min must be <= market_pct_up_1d_max")
    if config.btc_return_1d_min > config.btc_return_1d_max:
        raise ValueError("btc_return_1d_min must be <= btc_return_1d_max")
    if config.stop_pressure_window_days < 0:
        raise ValueError("stop_pressure_window_days must be non-negative")
    if config.stop_pressure_stop_count < 0:
        raise ValueError("stop_pressure_stop_count must be non-negative")
    if config.realized_loss_pressure_window_days < 0:
        raise ValueError("realized_loss_pressure_window_days must be non-negative")
    if config.realized_loss_pressure_loss_count < 0:
        raise ValueError("realized_loss_pressure_loss_count must be non-negative")
    if config.realized_loss_pressure_min_loss_abs < 0.0:
        raise ValueError("realized_loss_pressure_min_loss_abs must be non-negative")
    if config.exhaustion_min_day_return < 0.0:
        raise ValueError("exhaustion_min_day_return must be non-negative")
    if config.selloff_exhaustion_min_abs_day_return < 0.0:
        raise ValueError("selloff_exhaustion_min_abs_day_return must be non-negative")
    if config.absorption_max_abs_day_return < 0.0:
        raise ValueError("absorption_max_abs_day_return must be non-negative")
    if not 0.0 <= config.dryup_prior_volume_rank_max <= 1.0:
        raise ValueError("dryup_prior_volume_rank_max must be in [0, 1]")
    if config.dryup_prior_abs_day_return_max < 0.0:
        raise ValueError("dryup_prior_abs_day_return_max must be non-negative")
    if config.top_volume_rank_max <= 0:
        raise ValueError("top_volume_rank_max must be positive")
    if config.top_volume_prior_rank_min <= 0:
        raise ValueError("top_volume_prior_rank_min must be positive")
    if config.top_volume_min_age_days < 0:
        raise ValueError("top_volume_min_age_days must be non-negative")
    if config.top_volume_turnover_ratio_min < 0.0:
        raise ValueError("top_volume_turnover_ratio_min must be non-negative")
    if config.top_volume_day_return_min < -1.0:
        raise ValueError("top_volume_day_return_min must be >= -1")
    if config.top_volume_residual_return_min < -1.0:
        raise ValueError("top_volume_residual_return_min must be >= -1")
    if not 0.0 <= config.top_volume_close_position_min <= 1.0:
        raise ValueError("top_volume_close_position_min must be in [0, 1]")
    if config.leadership_pullback_rank_max <= 0:
        raise ValueError("leadership_pullback_rank_max must be positive")
    if config.leadership_pullback_min_age_days < 0:
        raise ValueError("leadership_pullback_min_age_days must be non-negative")
    if config.leadership_pullback_prior7_return_min > config.leadership_pullback_prior7_return_max:
        raise ValueError("leadership_pullback_prior7_return_min must be <= leadership_pullback_prior7_return_max")
    if config.leadership_pullback_day_return_min > config.leadership_pullback_day_return_max:
        raise ValueError("leadership_pullback_day_return_min must be <= leadership_pullback_day_return_max")
    if config.leadership_pullback_residual_return_min < -1.0:
        raise ValueError("leadership_pullback_residual_return_min must be >= -1")
    if not 0.0 <= config.leadership_pullback_close_position_min <= 1.0:
        raise ValueError("leadership_pullback_close_position_min must be in [0, 1]")
    if config.leadership_pullback_abs_day_return_max < 0.0:
        raise ValueError("leadership_pullback_abs_day_return_max must be non-negative")
    if config.shelf_reclaim_min_age_days < 0:
        raise ValueError("shelf_reclaim_min_age_days must be non-negative")
    if not 0.0 <= config.shelf_reclaim_prior7_volume_rank_max <= 1.0:
        raise ValueError("shelf_reclaim_prior7_volume_rank_max must be in [0, 1]")
    if config.shelf_reclaim_prior7_abs_return_mean_max < 0.0:
        raise ValueError("shelf_reclaim_prior7_abs_return_mean_max must be non-negative")
    if config.shelf_reclaim_day_return_min > config.shelf_reclaim_day_return_max:
        raise ValueError("shelf_reclaim_day_return_min must be <= shelf_reclaim_day_return_max")
    if config.shelf_reclaim_residual_return_min < -1.0:
        raise ValueError("shelf_reclaim_residual_return_min must be >= -1")
    if not 0.0 <= config.shelf_reclaim_close_position_min <= 1.0:
        raise ValueError("shelf_reclaim_close_position_min must be in [0, 1]")
    if config.shelf_reclaim_close_vs_prior20_high_min > config.shelf_reclaim_close_vs_prior20_high_max:
        raise ValueError("shelf_reclaim_close_vs_prior20_high_min must be <= shelf_reclaim_close_vs_prior20_high_max")
    if config.long_reclaim_day_return_min < -1.0:
        raise ValueError("long_reclaim_day_return_min must be >= -1")
    if config.long_reclaim_residual_return_min < -1.0:
        raise ValueError("long_reclaim_residual_return_min must be >= -1")
    if not 0.0 <= config.long_reclaim_close_position_min <= 1.0:
        raise ValueError("long_reclaim_close_position_min must be in [0, 1]")
    if config.long_reclaim_prior7_abs_return_mean_max < 0.0:
        raise ValueError("long_reclaim_prior7_abs_return_mean_max must be non-negative")
    if config.long_breakout_prior20_high_buffer_min > config.long_breakout_prior20_high_buffer_max:
        raise ValueError("long_breakout_prior20_high_buffer_min must be <= long_breakout_prior20_high_buffer_max")

def _validate_event_config(config: VolumeEventResearchConfig) -> None:
    _validate_scenario_config(config)
    _validate_exit_config(config)
    _validate_entry_config(config)
    _validate_universe_config(config)
    _validate_liquidity_migration_config(config)
    _validate_per_event_config(config)
