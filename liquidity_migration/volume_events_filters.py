"""Extracted from volume_events.py — see that module's docstring.

A cohesive slice of volume_events, split out to keep the hub readable.
Imports shared helpers from volume_events (the hub); the hub re-imports
this module's public names at the bottom so external callers
(`from liquidity_migration.volume_events import X`) keep working.
"""

from __future__ import annotations


import polars as pl

from .crowding import classify_liquidity_migration_crowding
from ._common import MS_PER_HOUR


# Splits live exclusively on VolumeEventResearchConfig.splits now (default ()).
# Whole-period reporting is the post-rebuild norm; pristine OOS is the forward
# demo/paper ledger, not a backtest window.

from .volume_events import (  # noqa: F401  (shared hub helpers)
    EventScenario,
    VolumeEventResearchConfig,
    _has_columns,
)




def _select_events(
    features: pl.DataFrame,
    *,
    scenario: EventScenario,
    config: VolumeEventResearchConfig,
    score_col: str,
) -> pl.DataFrame:
    events, _ = select_events_with_stage_counts(features, scenario=scenario, config=config, score_col=score_col)
    return events

def select_events_with_stage_counts(
    features: pl.DataFrame,
    *,
    scenario: EventScenario,
    config: VolumeEventResearchConfig,
    score_col: str,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Run the same filter chain as `_select_events` and also report per-stage row counts.

    Operators can read the stages dict to tell *which* filter killed events when
    the cycle reports `entries=0`. Hidden silent-zero failure modes
    (universe too narrow, crowding filter rejects everything, threshold too
    strict) all flatten into `events=0` otherwise.
    """
    stages = {
        "features": features.height,
        "after_threshold_filter": 0,
        "after_crowding_filter": 0,
        "final": 0,
    }
    if features.is_empty():
        return features, stages
    rank_col = f"{score_col}_rank_frac"
    top_cut = 1.0 - scenario.threshold
    filtered = _event_filter(features, scenario.event_type, score_col=score_col, rank_col=rank_col, top_cut=top_cut, config=config)
    stages["after_threshold_filter"] = filtered.height
    if filtered.is_empty():
        return filtered, stages
    if scenario.event_type == "liquidity_migration":
        filtered = _apply_liquidity_migration_crowding_filter(filtered, config=config)
        stages["after_crowding_filter"] = filtered.height
        if filtered.is_empty():
            return filtered, stages
    else:
        # Non-liquidity-migration scenarios skip the crowding filter — record the
        # passthrough count so downstream diagnostics don't mistake "no filter
        # applied" for "filter killed everything".
        stages["after_crowding_filter"] = filtered.height
    events = (
        filtered.sort(["ts_ms", rank_col, "turnover_quote"], descending=[False, True, True])
        .with_columns(pl.col(rank_col).rank("ordinal", descending=True).over("ts_ms").alias("event_rank"))
    )
    stages["final"] = events.height
    return events, stages

def _apply_liquidity_migration_crowding_filter(events: pl.DataFrame, *, config: VolumeEventResearchConfig) -> pl.DataFrame:
    mode = config.liquidity_migration_crowding_filter
    if mode == "none" or events.is_empty():
        return events
    if mode == "model_v1":
        classified = classify_liquidity_migration_crowding(
            events,
            entry_delay_hours=config.entry_delay_hours,
            signal_ts_col="ts_ms",
            entry_ts_col="",
        )
        return classified.filter(pl.col("crowding_tradeable"))
    if mode != "union_pathology":
        raise ValueError(f"Unknown liquidity migration crowding filter: {mode}")
    required_cols = {
        "ts_ms",
        "turnover_quote",
        "prior7_turnover_quote_mean",
        "signal_day_close_location",
        "signal_day_last6h_return",
        "signal_day_last6h_turnover_share",
        "market_pct_up_1d",
    }
    if not required_cols.issubset(set(events.columns)):
        return events.head(0)

    turnover_ratio = pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean")
    annotated = (
        events.with_columns(((pl.col("ts_ms") + config.entry_delay_hours * MS_PER_HOUR) // MS_PER_HOUR).alias("_entry_hour"))
        .with_columns(
            [
                pl.len().over("_entry_hour").alias("_entry_hour_signal_count"),
                pl.col("signal_day_last6h_return").mean().over("_entry_hour").alias("_entry_hour_avg_last6h_return"),
                pl.col("signal_day_last6h_turnover_share")
                .mean()
                .over("_entry_hour")
                .alias("_entry_hour_avg_last6h_turnover_share"),
                pl.col("signal_day_last6h_turnover_share")
                .max()
                .over("_entry_hour")
                .alias("_entry_hour_max_last6h_turnover_share"),
            ]
        )
        .with_columns(turnover_ratio.alias("_liquidity_migration_turnover_ratio"))
    )
    crowded = pl.col("_entry_hour_signal_count") >= config.liquidity_migration_crowding_min_signals
    stalled_low_turnover = (
        (pl.col("_entry_hour_avg_last6h_return") <= config.liquidity_migration_crowding_stalled_last6h_return_max)
        & (pl.col("signal_day_close_location") >= config.liquidity_migration_crowding_stalled_close_location_min)
        & (
            pl.col("_liquidity_migration_turnover_ratio")
            <= config.liquidity_migration_crowding_stalled_turnover_ratio_max
        )
    )
    late_concentration = (
        (
            pl.col("_entry_hour_max_last6h_turnover_share")
            >= config.liquidity_migration_crowding_late_max_turnover_share_min
        )
        & (pl.col("signal_day_last6h_return") >= config.liquidity_migration_crowding_late_last6h_return_min)
        & (pl.col("_liquidity_migration_turnover_ratio") >= config.liquidity_migration_crowding_late_turnover_ratio_min)
    )
    weak_tape_high_share = (
        (pl.col("market_pct_up_1d") <= config.liquidity_migration_crowding_weak_market_pct_up_max)
        & (
            pl.col("_entry_hour_avg_last6h_turnover_share")
            >= config.liquidity_migration_crowding_weak_avg_turnover_share_min
        )
    )
    return annotated.filter(~(crowded & (stalled_low_turnover | late_concentration | weak_tape_high_share))).drop(
        [
            "_entry_hour",
            "_entry_hour_signal_count",
            "_entry_hour_avg_last6h_return",
            "_entry_hour_avg_last6h_turnover_share",
            "_entry_hour_max_last6h_turnover_share",
            "_liquidity_migration_turnover_ratio",
        ]
    )

def _event_filter_base(
    features: pl.DataFrame,
    event_type: str,
    *,
    score_col: str,
    rank_col: str,
    top_cut: float,
    config: VolumeEventResearchConfig,
) -> pl.DataFrame:
    """Universe + PIT-membership + market-context filter, BEFORE any event-specific
    gates run. Extracted so per-gate rejection-trace instrumentation can take this
    pre-event-filter dataframe as its input -- the explain function then identifies
    which event-specific gate dropped each row."""
    base = features.filter(pl.col(score_col).is_not_null() & pl.col(score_col).is_finite())
    base = _exclude_symbols(base, config.exclude_symbols)
    if config.require_pit_membership:
        if "tradable_membership_flag" not in base.columns:
            return base.head(0)
        base = base.filter(pl.col("tradable_membership_flag"))
    if config.universe_min_daily_turnover > 0.0:
        base = base.filter(pl.col("turnover_quote") >= config.universe_min_daily_turnover)
    if event_type != "top_volume_leadership":
        if config.universe_rank_min > 1:
            base = base.filter(pl.col("liquidity_rank") >= config.universe_rank_min)
        if config.universe_rank_max > 0:
            base = base.filter(pl.col("liquidity_rank") <= config.universe_rank_max)
    return _apply_market_context_filters(base, config)

def _event_filter(
    features: pl.DataFrame,
    event_type: str,
    *,
    score_col: str,
    rank_col: str,
    top_cut: float,
    config: VolumeEventResearchConfig,
) -> pl.DataFrame:
    base = _event_filter_base(features, event_type, score_col=score_col, rank_col=rank_col, top_cut=top_cut, config=config)
    if event_type == "fresh_volume_spike":
        return base.filter((pl.col(rank_col) >= top_cut) & (pl.col(f"prior_{rank_col}") < top_cut))
    if event_type == "persistent_volume_breakout":
        return base.filter((pl.col(rank_col) >= top_cut) & (pl.col("prior3_volume_persistence_rank_min") < 0.50))
    if event_type == "tail_liquidity_jump":
        if "tradable_membership_flag" not in base.columns:
            return base.head(0)
        # Same u32-underflow guard as _filter_liquidity_migration: cast both
        # rank columns to Int64 so a deteriorating rank produces a negative
        # delta instead of wrapping to ~2^32.
        tail_rank_delta = pl.col("prior7_liquidity_rank").cast(pl.Int64) - pl.col("liquidity_rank").cast(pl.Int64)
        return base.filter(
            pl.col("tradable_membership_flag")
            & (pl.col("liquidity_rank") >= config.tail_rank_min)
            & (pl.col("liquidity_rank") <= config.tail_rank_max)
            & (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior7_{rank_col}") < top_cut)
            & (tail_rank_delta >= config.tail_rank_improvement_min)
        )
    if event_type == "volume_exhaustion":
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("daily_return_1d") >= config.exhaustion_min_day_return)
            & (pl.col("daily_return_rank_frac") >= top_cut)
        )
    if event_type == "volume_absorption":
        if not _has_columns(base, "daily_return_1d"):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & pl.col("daily_return_1d").is_not_null()
            & (pl.col("daily_return_1d").abs() <= config.absorption_max_abs_day_return)
        )
    if event_type == "dryup_reacceleration":
        if not _has_columns(base, "prior7_volume_persistence_rank_max", "prior7_abs_daily_return_mean", f"prior_{rank_col}"):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior_{rank_col}") < top_cut)
            & (pl.col("prior7_volume_persistence_rank_max") <= config.dryup_prior_volume_rank_max)
            & (pl.col("prior7_abs_daily_return_mean") <= config.dryup_prior_abs_day_return_max)
        )
    if event_type == "top_volume_leadership":
        required_cols = ["prior7_liquidity_rank", "prior7_turnover_quote_mean", "symbol_age_days", f"prior7_{rank_col}"]
        if config.top_volume_day_return_min > -1.0:
            required_cols.append("daily_return_1d")
        if config.top_volume_residual_return_min > -1.0:
            required_cols.append("residual_return_1d")
        if config.top_volume_close_position_min > 0.0:
            required_cols.append("close_position_1d")
        if not _has_columns(base, *required_cols):
            return base.head(0)
        predicate = (
            (pl.col("liquidity_rank") <= config.top_volume_rank_max)
            & (pl.col("symbol_age_days") >= config.top_volume_min_age_days)
            & (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior7_{rank_col}") < top_cut)
            & (pl.col("prior7_liquidity_rank") >= config.top_volume_prior_rank_min)
            & (pl.col("prior7_turnover_quote_mean") > 0.0)
            & ((pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean")) >= config.top_volume_turnover_ratio_min)
        )
        if config.top_volume_day_return_min > -1.0:
            predicate = predicate & (pl.col("daily_return_1d") >= config.top_volume_day_return_min)
        if config.top_volume_residual_return_min > -1.0:
            predicate = predicate & (pl.col("residual_return_1d") >= config.top_volume_residual_return_min)
        if config.top_volume_close_position_min > 0.0:
            predicate = predicate & (pl.col("close_position_1d") >= config.top_volume_close_position_min)
        return base.filter(predicate)
    if event_type == "orderly_leadership_pullback":
        required_cols = [
            "symbol_age_days",
            "volume_persistence_z_rank_frac",
            "daily_return_1d",
            "abs_daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "prior7_return",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("liquidity_rank") <= config.leadership_pullback_rank_max)
            & (pl.col("symbol_age_days") >= config.leadership_pullback_min_age_days)
            & (pl.col("volume_persistence_z_rank_frac") >= top_cut)
            & (pl.col("prior7_return") >= config.leadership_pullback_prior7_return_min)
            & (pl.col("prior7_return") <= config.leadership_pullback_prior7_return_max)
            & (pl.col("daily_return_1d") >= config.leadership_pullback_day_return_min)
            & (pl.col("daily_return_1d") <= config.leadership_pullback_day_return_max)
            & (pl.col("abs_daily_return_1d") <= config.leadership_pullback_abs_day_return_max)
            & (pl.col("residual_return_1d") >= config.leadership_pullback_residual_return_min)
            & (pl.col("close_position_1d") >= config.leadership_pullback_close_position_min)
        )
    if event_type == "volume_shelf_reclaim":
        required_cols = [
            "symbol_age_days",
            "prior7_volume_persistence_rank_max",
            "prior7_abs_daily_return_mean",
            "daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "close_vs_prior20_high",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("symbol_age_days") >= config.shelf_reclaim_min_age_days)
            & (pl.col("prior7_volume_persistence_rank_max") <= config.shelf_reclaim_prior7_volume_rank_max)
            & (pl.col("prior7_abs_daily_return_mean") <= config.shelf_reclaim_prior7_abs_return_mean_max)
            & (pl.col("daily_return_1d") >= config.shelf_reclaim_day_return_min)
            & (pl.col("daily_return_1d") <= config.shelf_reclaim_day_return_max)
            & (pl.col("residual_return_1d") >= config.shelf_reclaim_residual_return_min)
            & (pl.col("close_position_1d") >= config.shelf_reclaim_close_position_min)
            & (pl.col("close_vs_prior20_high") >= config.shelf_reclaim_close_vs_prior20_high_min)
            & (pl.col("close_vs_prior20_high") <= config.shelf_reclaim_close_vs_prior20_high_max)
        )
    if event_type == "reclaim_breakout":
        required_cols = [
            f"prior_{rank_col}",
            "daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "close_vs_prior20_high",
            "prior7_abs_daily_return_mean",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior_{rank_col}") < top_cut)
            & (pl.col("daily_return_1d") >= config.long_reclaim_day_return_min)
            & (pl.col("residual_return_1d") >= config.long_reclaim_residual_return_min)
            & (pl.col("close_position_1d") >= config.long_reclaim_close_position_min)
            & (pl.col("close_vs_prior20_high") >= config.long_breakout_prior20_high_buffer_min)
            & (pl.col("close_vs_prior20_high") <= config.long_breakout_prior20_high_buffer_max)
            & (pl.col("prior7_abs_daily_return_mean") <= config.long_reclaim_prior7_abs_return_mean_max)
        )
    if event_type == "capitulation_reclaim":
        required_cols = [
            f"prior_{rank_col}",
            "daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "close_vs_prior20_high",
            "prior7_return",
            "prior20_drawdown",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior_{rank_col}") < top_cut)
            & (pl.col("daily_return_1d") >= config.long_reclaim_day_return_min)
            & (pl.col("residual_return_1d") >= config.long_reclaim_residual_return_min)
            & (pl.col("close_position_1d") >= config.long_reclaim_close_position_min)
            & (pl.col("prior7_return") <= config.capitulation_reclaim_prior7_return_max)
            & (pl.col("prior20_drawdown") <= config.capitulation_reclaim_prior20_drawdown_max)
            & (pl.col("close_vs_prior20_high") <= config.capitulation_reclaim_close_vs_prior20_high_max)
        )
    if event_type == "liquidity_migration":
        return _filter_liquidity_migration(
            base, score_col=score_col, rank_col=rank_col, top_cut=top_cut, config=config
        )
    if event_type == "selloff_exhaustion":
        if not _has_columns(base, "daily_return_1d", "daily_return_rank_frac"):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("daily_return_1d") <= -config.selloff_exhaustion_min_abs_day_return)
            & (pl.col("daily_return_rank_frac") <= _bottom_cut_from_top_cut(top_cut))
        )
    raise ValueError(f"Unknown event type: {event_type}")

def _filter_liquidity_migration(
    base: pl.DataFrame,
    *,
    score_col: str,
    rank_col: str,
    top_cut: float,
    config: VolumeEventResearchConfig,
) -> pl.DataFrame:
    required_cols = ["prior7_liquidity_rank", f"prior7_{rank_col}"]
    if config.liquidity_migration_turnover_ratio_min > 0.0:
        required_cols.append("prior7_turnover_quote_mean")
    if config.liquidity_migration_day_return_min > -1.0 or config.liquidity_migration_day_return_max < 10.0:
        required_cols.append("daily_return_1d")
    if config.liquidity_migration_return_7d_min > -10.0 or config.liquidity_migration_return_7d_max < 10.0:
        required_cols.append("return_7d")
    if (
        config.liquidity_migration_residual_return_min > -10.0
        or config.liquidity_migration_residual_return_max < 10.0
    ):
        required_cols.append("residual_return_1d")
    if config.liquidity_migration_close_to_high_7d_min > -10.0:
        required_cols.append("close_to_high_7d")
    if config.liquidity_migration_close_to_high_30d_min > -10.0:
        required_cols.append("close_to_high_30d")
    if (
        config.liquidity_migration_prior30_max_return_min > -10.0
        or config.liquidity_migration_prior30_max_return_max < 10.0
    ):
        required_cols.append("prior30_max_daily_return")
    if (
        config.liquidity_migration_prior7_return_volatility_min > 0.0
        or config.liquidity_migration_prior7_return_volatility_max < 10.0
    ):
        required_cols.append("prior7_return_volatility")
    if config.liquidity_migration_intraday_range_max < 10.0:
        required_cols.append("intraday_range_1d")
    if (
        config.liquidity_migration_funding_rate_last_min > -10.0
        or config.liquidity_migration_funding_rate_last_max < 10.0
    ):
        required_cols.append("funding_rate_last")
    if (
        config.liquidity_migration_funding_3d_sum_min > -10.0
        or config.liquidity_migration_funding_3d_sum_max < 10.0
    ):
        required_cols.append("funding_rate_3d_sum")
    if (
        config.liquidity_migration_funding_7d_sum_min > -10.0
        or config.liquidity_migration_funding_7d_sum_max < 10.0
    ):
        required_cols.append("funding_rate_7d_sum")
    if (
        config.liquidity_migration_open_interest_return_3d_min > -10.0
        or config.liquidity_migration_open_interest_return_3d_max < 10.0
    ):
        required_cols.append("open_interest_return_3d")
    if (
        config.liquidity_migration_open_interest_return_7d_min > -10.0
        or config.liquidity_migration_open_interest_return_7d_max < 10.0
    ):
        required_cols.append("open_interest_return_7d")
    if config.liquidity_migration_volume_to_oi_quote_min > 0.0 or config.liquidity_migration_volume_to_oi_quote_max > 0.0:
        required_cols.append("volume_to_open_interest_quote")
    if (
        config.liquidity_migration_mark_index_basis_3d_mean_min > -10.0
        or config.liquidity_migration_mark_index_basis_3d_mean_max < 10.0
    ):
        required_cols.append("mark_index_basis_3d_mean")
    if (
        config.liquidity_migration_premium_index_3d_mean_min > -10.0
        or config.liquidity_migration_premium_index_3d_mean_max < 10.0
    ):
        required_cols.append("premium_index_3d_mean")
    if (
        config.liquidity_migration_taker_imbalance_1d_min > -1.0
        or config.liquidity_migration_taker_imbalance_1d_max < 1.0
    ):
        required_cols.append("taker_imbalance_1d")
    if (
        config.liquidity_migration_taker_imbalance_3d_min > -1.0
        or config.liquidity_migration_taker_imbalance_3d_max < 1.0
    ):
        required_cols.append("taker_imbalance_3d")
    if config.liquidity_migration_market_pct_up_max < 1.0:
        required_cols.append("market_pct_up_1d")
        if config.liquidity_migration_hot_market_day_return_min < 10.0:
            required_cols.append("daily_return_1d")
    if config.liquidity_migration_market_median_return_30d_max < 10.0:
        required_cols.append("market_median_return_30d_sum")
    if config.liquidity_migration_market_median_return_7d_max < 10.0:
        required_cols.append("market_median_return_7d_sum")
    if config.liquidity_migration_market_pct_up_30d_max < 1.0:
        required_cols.append("market_pct_up_30d_mean")
    if config.liquidity_migration_market_pct_up_7d_max < 1.0:
        required_cols.append("market_pct_up_7d_mean")
    if (
        config.liquidity_migration_close_location_min > 0.0
        or config.liquidity_migration_close_location_max < 1.0
    ):
        required_cols.append("signal_day_close_location")
    if config.liquidity_migration_signal_last6h_turnover_share_max < 1.0:
        required_cols.append("signal_day_last6h_turnover_share")
    if config.liquidity_migration_up_volume_concentration_min > 0.0:
        required_cols.append("up_volume_concentration")
    if config.liquidity_migration_pit_age_days_min > 0 or config.liquidity_migration_pit_age_days_max > 0:
        required_cols.append("pit_age_days")
    if not _has_columns(base, *required_cols):
        return base.head(0)
    # prior7_liquidity_rank and liquidity_rank are both u32. Subtracting them
    # directly produces an unsigned result that wraps to ~2^32 when the
    # current rank is numerically larger than the prior — meaning a symbol
    # whose rank deteriorated would falsely satisfy the improvement-min
    # threshold (huge wrapped value >= 150). Cast to Int64 first so the
    # signed delta gives a real comparison. See `_add_liquidity_migration_
    # speed_features` for the matching fix on the feature columns.
    rank_delta = pl.col("prior7_liquidity_rank").cast(pl.Int64) - pl.col("liquidity_rank").cast(pl.Int64)
    threshold = config.liquidity_migration_rank_improvement_min
    direction = config.liquidity_migration_rank_direction
    if direction == "improvement":
        rank_predicate = rank_delta >= threshold
    elif direction == "deterioration":
        rank_predicate = rank_delta <= -threshold
    elif direction == "both":
        rank_predicate = rank_delta.abs() >= threshold
    else:
        raise ValueError(
            f"liquidity_migration_rank_direction must be improvement|deterioration|both, got {direction!r}"
        )
    predicate = (
        (pl.col(rank_col) >= top_cut)
        & (pl.col(f"prior7_{rank_col}") < top_cut)
        & rank_predicate
    )
    if config.liquidity_migration_turnover_ratio_min > 0.0:
        predicate = (
            predicate
            & (pl.col("prior7_turnover_quote_mean") > 0.0)
            & (
                (pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean"))
                >= config.liquidity_migration_turnover_ratio_min
            )
        )
    if config.liquidity_migration_prior_rank_min > 0:
        predicate = predicate & (pl.col("prior7_liquidity_rank") >= config.liquidity_migration_prior_rank_min)
    if config.liquidity_migration_current_rank_max > 0:
        predicate = predicate & (pl.col("liquidity_rank") <= config.liquidity_migration_current_rank_max)
    if config.liquidity_migration_event_rank_fraction_max > 0.0:
        predicate = predicate & (pl.col(rank_col) <= config.liquidity_migration_event_rank_fraction_max)
    if (
        config.liquidity_migration_event_rank_fraction_exclude_min > 0.0
        or config.liquidity_migration_event_rank_fraction_exclude_max > 0.0
    ):
        predicate = predicate & (
            (pl.col(rank_col) <= config.liquidity_migration_event_rank_fraction_exclude_min)
            | (pl.col(rank_col) >= config.liquidity_migration_event_rank_fraction_exclude_max)
        )
    if config.liquidity_migration_score_max > 0.0:
        predicate = predicate & (pl.col(score_col) <= config.liquidity_migration_score_max)
    if config.liquidity_migration_day_return_min > -1.0 or config.liquidity_migration_day_return_max < 10.0:
        predicate = (
            predicate
            & pl.col("daily_return_1d").is_not_null()
            & (pl.col("daily_return_1d") >= config.liquidity_migration_day_return_min)
            & (pl.col("daily_return_1d") <= config.liquidity_migration_day_return_max)
        )
    if config.liquidity_migration_return_7d_min > -10.0 or config.liquidity_migration_return_7d_max < 10.0:
        predicate = (
            predicate
            & pl.col("return_7d").is_not_null()
            & (pl.col("return_7d") >= config.liquidity_migration_return_7d_min)
            & (pl.col("return_7d") <= config.liquidity_migration_return_7d_max)
        )
    if (
        config.liquidity_migration_residual_return_min > -10.0
        or config.liquidity_migration_residual_return_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("residual_return_1d").is_not_null()
            & (pl.col("residual_return_1d") >= config.liquidity_migration_residual_return_min)
            & (pl.col("residual_return_1d") <= config.liquidity_migration_residual_return_max)
        )
    if config.liquidity_migration_close_to_high_7d_min > -10.0:
        predicate = (
            predicate
            & pl.col("close_to_high_7d").is_not_null()
            & (pl.col("close_to_high_7d") >= config.liquidity_migration_close_to_high_7d_min)
        )
    if config.liquidity_migration_close_to_high_30d_min > -10.0:
        predicate = (
            predicate
            & pl.col("close_to_high_30d").is_not_null()
            & (pl.col("close_to_high_30d") >= config.liquidity_migration_close_to_high_30d_min)
        )
    if (
        config.liquidity_migration_prior30_max_return_min > -10.0
        or config.liquidity_migration_prior30_max_return_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("prior30_max_daily_return").is_not_null()
            & (pl.col("prior30_max_daily_return") >= config.liquidity_migration_prior30_max_return_min)
            & (pl.col("prior30_max_daily_return") <= config.liquidity_migration_prior30_max_return_max)
        )
    if (
        config.liquidity_migration_prior7_return_volatility_min > 0.0
        or config.liquidity_migration_prior7_return_volatility_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("prior7_return_volatility").is_not_null()
            & (pl.col("prior7_return_volatility") >= config.liquidity_migration_prior7_return_volatility_min)
            & (pl.col("prior7_return_volatility") <= config.liquidity_migration_prior7_return_volatility_max)
        )
    if config.liquidity_migration_intraday_range_max < 10.0:
        predicate = (
            predicate
            & pl.col("intraday_range_1d").is_not_null()
            & (pl.col("intraday_range_1d") <= config.liquidity_migration_intraday_range_max)
        )
    if config.liquidity_migration_up_volume_concentration_min > 0.0:
        predicate = (
            predicate
            & pl.col("up_volume_concentration").is_not_null()
            & (pl.col("up_volume_concentration") >= config.liquidity_migration_up_volume_concentration_min)
        )
    if (
        config.liquidity_migration_funding_rate_last_min > -10.0
        or config.liquidity_migration_funding_rate_last_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("funding_rate_last").is_not_null()
            & (pl.col("funding_rate_last") >= config.liquidity_migration_funding_rate_last_min)
            & (pl.col("funding_rate_last") <= config.liquidity_migration_funding_rate_last_max)
        )
    if (
        config.liquidity_migration_funding_3d_sum_min > -10.0
        or config.liquidity_migration_funding_3d_sum_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("funding_rate_3d_sum").is_not_null()
            & (pl.col("funding_rate_3d_sum") >= config.liquidity_migration_funding_3d_sum_min)
            & (pl.col("funding_rate_3d_sum") <= config.liquidity_migration_funding_3d_sum_max)
        )
    if (
        config.liquidity_migration_funding_7d_sum_min > -10.0
        or config.liquidity_migration_funding_7d_sum_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("funding_rate_7d_sum").is_not_null()
            & (pl.col("funding_rate_7d_sum") >= config.liquidity_migration_funding_7d_sum_min)
            & (pl.col("funding_rate_7d_sum") <= config.liquidity_migration_funding_7d_sum_max)
        )
    if (
        config.liquidity_migration_open_interest_return_3d_min > -10.0
        or config.liquidity_migration_open_interest_return_3d_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("open_interest_return_3d").is_not_null()
            & (pl.col("open_interest_return_3d") >= config.liquidity_migration_open_interest_return_3d_min)
            & (pl.col("open_interest_return_3d") <= config.liquidity_migration_open_interest_return_3d_max)
        )
    if (
        config.liquidity_migration_open_interest_return_7d_min > -10.0
        or config.liquidity_migration_open_interest_return_7d_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("open_interest_return_7d").is_not_null()
            & (pl.col("open_interest_return_7d") >= config.liquidity_migration_open_interest_return_7d_min)
            & (pl.col("open_interest_return_7d") <= config.liquidity_migration_open_interest_return_7d_max)
        )
    if config.liquidity_migration_volume_to_oi_quote_min > 0.0 or config.liquidity_migration_volume_to_oi_quote_max > 0.0:
        predicate = predicate & pl.col("volume_to_open_interest_quote").is_not_null()
        if config.liquidity_migration_volume_to_oi_quote_min > 0.0:
            predicate = predicate & (
                pl.col("volume_to_open_interest_quote") >= config.liquidity_migration_volume_to_oi_quote_min
            )
        if config.liquidity_migration_volume_to_oi_quote_max > 0.0:
            predicate = predicate & (
                pl.col("volume_to_open_interest_quote") <= config.liquidity_migration_volume_to_oi_quote_max
            )
    if (
        config.liquidity_migration_mark_index_basis_3d_mean_min > -10.0
        or config.liquidity_migration_mark_index_basis_3d_mean_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("mark_index_basis_3d_mean").is_not_null()
            & (pl.col("mark_index_basis_3d_mean") >= config.liquidity_migration_mark_index_basis_3d_mean_min)
            & (pl.col("mark_index_basis_3d_mean") <= config.liquidity_migration_mark_index_basis_3d_mean_max)
        )
    if (
        config.liquidity_migration_premium_index_3d_mean_min > -10.0
        or config.liquidity_migration_premium_index_3d_mean_max < 10.0
    ):
        predicate = (
            predicate
            & pl.col("premium_index_3d_mean").is_not_null()
            & (pl.col("premium_index_3d_mean") >= config.liquidity_migration_premium_index_3d_mean_min)
            & (pl.col("premium_index_3d_mean") <= config.liquidity_migration_premium_index_3d_mean_max)
        )
    if (
        config.liquidity_migration_taker_imbalance_1d_min > -1.0
        or config.liquidity_migration_taker_imbalance_1d_max < 1.0
    ):
        predicate = (
            predicate
            & pl.col("taker_imbalance_1d").is_not_null()
            & (pl.col("taker_imbalance_1d") >= config.liquidity_migration_taker_imbalance_1d_min)
            & (pl.col("taker_imbalance_1d") <= config.liquidity_migration_taker_imbalance_1d_max)
        )
    if (
        config.liquidity_migration_taker_imbalance_3d_min > -1.0
        or config.liquidity_migration_taker_imbalance_3d_max < 1.0
    ):
        predicate = (
            predicate
            & pl.col("taker_imbalance_3d").is_not_null()
            & (pl.col("taker_imbalance_3d") >= config.liquidity_migration_taker_imbalance_3d_min)
            & (pl.col("taker_imbalance_3d") <= config.liquidity_migration_taker_imbalance_3d_max)
        )
    if config.liquidity_migration_market_pct_up_max < 1.0:
        market_ok = pl.col("market_pct_up_1d").is_not_null() & (
            pl.col("market_pct_up_1d") <= config.liquidity_migration_market_pct_up_max
        )
        if config.liquidity_migration_hot_market_day_return_min < 10.0:
            hot_coin_ok = pl.col("daily_return_1d").is_not_null() & (
                pl.col("daily_return_1d") >= _liquidity_migration_hot_return_threshold_expr(config)
            )
            predicate = predicate & (market_ok | hot_coin_ok)
        else:
            predicate = predicate & market_ok
    if config.liquidity_migration_market_median_return_30d_max < 10.0:
        predicate = predicate & (
            pl.col("market_median_return_30d_sum").is_not_null()
            & (pl.col("market_median_return_30d_sum") <= config.liquidity_migration_market_median_return_30d_max)
        )
    if config.liquidity_migration_market_median_return_7d_max < 10.0:
        predicate = predicate & (
            pl.col("market_median_return_7d_sum").is_not_null()
            & (pl.col("market_median_return_7d_sum") <= config.liquidity_migration_market_median_return_7d_max)
        )
    if config.liquidity_migration_market_pct_up_30d_max < 1.0:
        predicate = predicate & (
            pl.col("market_pct_up_30d_mean").is_not_null()
            & (pl.col("market_pct_up_30d_mean") <= config.liquidity_migration_market_pct_up_30d_max)
        )
    if config.liquidity_migration_market_pct_up_7d_max < 1.0:
        predicate = predicate & (
            pl.col("market_pct_up_7d_mean").is_not_null()
            & (pl.col("market_pct_up_7d_mean") <= config.liquidity_migration_market_pct_up_7d_max)
        )
    if (
        config.liquidity_migration_close_location_min > 0.0
        or config.liquidity_migration_close_location_max < 1.0
    ):
        predicate = (
            predicate
            & pl.col("signal_day_close_location").is_not_null()
            & (pl.col("signal_day_close_location") >= config.liquidity_migration_close_location_min)
            & (pl.col("signal_day_close_location") <= config.liquidity_migration_close_location_max)
        )
    if config.liquidity_migration_signal_last6h_turnover_share_max < 1.0:
        predicate = (
            predicate
            & pl.col("signal_day_last6h_turnover_share").is_not_null()
            & (
                pl.col("signal_day_last6h_turnover_share")
                <= config.liquidity_migration_signal_last6h_turnover_share_max
            )
        )
    if config.liquidity_migration_pit_age_days_min > 0 or config.liquidity_migration_pit_age_days_max > 0:
        predicate = predicate & pl.col("pit_age_days").is_not_null()
        if config.liquidity_migration_pit_age_days_min > 0:
            predicate = predicate & (pl.col("pit_age_days") >= float(config.liquidity_migration_pit_age_days_min))
        if config.liquidity_migration_pit_age_days_max > 0:
            predicate = predicate & (pl.col("pit_age_days") <= float(config.liquidity_migration_pit_age_days_max))
    return base.filter(predicate)

def _explain_liquidity_migration_rejections(
    base: pl.DataFrame,
    *,
    score_col: str,
    rank_col: str,
    top_cut: float,
    config: VolumeEventResearchConfig,
) -> pl.DataFrame:
    """Per-(symbol, ts_ms) rejection trace for ``_filter_liquidity_migration``.

    Independently evaluates each gate in the same order ``_filter_liquidity_migration``
    applies them, and records the FIRST failing gate per row plus the row's actual
    value and the threshold. Rows that survive every gate get ``first_failing_gate=""``.

    Output columns:
        symbol, ts_ms, first_failing_gate, first_failing_value, first_failing_threshold

    Use case: diagnose "why didn't symbol X get entered on signal day Y?" without
    monkey-patching the strategy. Identifies the binding constraint per candidate.
    Only emits for the liquidity_migration event family (the strategy's primary
    event type); other event families' gates remain inspectable via the same
    pattern if extended later.
    """
    if base.is_empty():
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "ts_ms": pl.Int64,
                "first_failing_gate": pl.Utf8,
                "first_failing_value": pl.Float64,
                "first_failing_threshold": pl.Float64,
            }
        )

    rank_delta = pl.col("prior7_liquidity_rank").cast(pl.Int64) - pl.col("liquidity_rank").cast(pl.Int64)

    # Gate registry: ordered list of (name, predicate_expr, value_expr, threshold_value).
    # Order MUST match ``_filter_liquidity_migration``'s application order so the
    # 'first failing' label reflects the same gate the production filter would have
    # cited as the binding constraint.
    gates: list[tuple[str, pl.Expr, pl.Expr, float]] = []

    # Base gates (always evaluated)
    gates.append((
        "event_rank_above_threshold",
        pl.col(rank_col) >= top_cut,
        pl.col(rank_col),
        top_cut,
    ))
    gates.append((
        "prior7_event_rank_below_threshold",
        pl.col(f"prior7_{rank_col}") < top_cut,
        pl.col(f"prior7_{rank_col}"),
        top_cut,
    ))
    # Gate name + value direction switch: the trace must reflect what
    # constraint is actually active. For direction=deterioration the binding
    # is `rank_delta <= -T`, which is equivalent to `-rank_delta >= T`, so
    # the reported value is -rank_delta with the same magnitude threshold.
    # For direction=both, the constraint is `|rank_delta| >= T`.
    direction = config.liquidity_migration_rank_direction
    rank_threshold = float(config.liquidity_migration_rank_improvement_min)
    if direction == "improvement":
        gate_name = "rank_improvement_min"
        rank_gate_pred = rank_delta >= config.liquidity_migration_rank_improvement_min
        rank_gate_value = rank_delta.cast(pl.Float64)
    elif direction == "deterioration":
        gate_name = "rank_deterioration_min"
        rank_gate_pred = rank_delta <= -config.liquidity_migration_rank_improvement_min
        rank_gate_value = (-rank_delta).cast(pl.Float64)
    elif direction == "both":
        gate_name = "rank_abs_delta_min"
        rank_gate_pred = rank_delta.abs() >= config.liquidity_migration_rank_improvement_min
        rank_gate_value = rank_delta.abs().cast(pl.Float64)
    else:
        raise ValueError(
            f"liquidity_migration_rank_direction must be improvement|deterioration|both, got {direction!r}"
        )
    gates.append((gate_name, rank_gate_pred, rank_gate_value, rank_threshold))

    # Conditional gates (only added when the config moves them off the default)
    if config.liquidity_migration_turnover_ratio_min > 0.0:
        turnover_ratio = pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean")
        gates.append((
            "turnover_ratio_min",
            (pl.col("prior7_turnover_quote_mean") > 0.0) & (turnover_ratio >= config.liquidity_migration_turnover_ratio_min),
            turnover_ratio,
            config.liquidity_migration_turnover_ratio_min,
        ))
    if config.liquidity_migration_prior_rank_min > 0:
        gates.append((
            "prior7_liquidity_rank_min",
            pl.col("prior7_liquidity_rank") >= config.liquidity_migration_prior_rank_min,
            pl.col("prior7_liquidity_rank").cast(pl.Float64),
            float(config.liquidity_migration_prior_rank_min),
        ))
    if config.liquidity_migration_current_rank_max > 0:
        gates.append((
            "liquidity_rank_max",
            pl.col("liquidity_rank") <= config.liquidity_migration_current_rank_max,
            pl.col("liquidity_rank").cast(pl.Float64),
            float(config.liquidity_migration_current_rank_max),
        ))
    if config.liquidity_migration_event_rank_fraction_max > 0.0:
        gates.append((
            "event_rank_fraction_max",
            pl.col(rank_col) <= config.liquidity_migration_event_rank_fraction_max,
            pl.col(rank_col),
            config.liquidity_migration_event_rank_fraction_max,
        ))
    if config.liquidity_migration_day_return_min > -1.0:
        gates.append((
            "day_return_min",
            pl.col("daily_return_1d").is_not_null() & (pl.col("daily_return_1d") >= config.liquidity_migration_day_return_min),
            pl.col("daily_return_1d"),
            config.liquidity_migration_day_return_min,
        ))
    if config.liquidity_migration_day_return_max < 10.0:
        gates.append((
            "day_return_max",
            pl.col("daily_return_1d").is_not_null() & (pl.col("daily_return_1d") <= config.liquidity_migration_day_return_max),
            pl.col("daily_return_1d"),
            config.liquidity_migration_day_return_max,
        ))
    if config.liquidity_migration_residual_return_min > -10.0:
        gates.append((
            "residual_return_min",
            pl.col("residual_return_1d").is_not_null() & (pl.col("residual_return_1d") >= config.liquidity_migration_residual_return_min),
            pl.col("residual_return_1d"),
            config.liquidity_migration_residual_return_min,
        ))
    if config.liquidity_migration_close_location_min > 0.0:
        gates.append((
            "close_location_min",
            pl.col("signal_day_close_location").is_not_null()
            & (pl.col("signal_day_close_location") >= config.liquidity_migration_close_location_min),
            pl.col("signal_day_close_location"),
            config.liquidity_migration_close_location_min,
        ))
    if config.liquidity_migration_pit_age_days_min > 0:
        gates.append((
            "pit_age_days_min",
            pl.col("pit_age_days").is_not_null() & (pl.col("pit_age_days") >= float(config.liquidity_migration_pit_age_days_min)),
            pl.col("pit_age_days"),
            float(config.liquidity_migration_pit_age_days_min),
        ))
    # Market-pct-up gate: short-circuits via the "OR hot coin" clause -- a row
    # passes if the market breadth is moderate OR if the symbol itself is hot
    # enough. We treat this as a single gate keyed by market_pct_up_1d.
    if config.liquidity_migration_market_pct_up_max < 1.0:
        market_ok = pl.col("market_pct_up_1d").is_not_null() & (
            pl.col("market_pct_up_1d") <= config.liquidity_migration_market_pct_up_max
        )
        if config.liquidity_migration_hot_market_day_return_min < 10.0:
            hot_ok = pl.col("daily_return_1d").is_not_null() & (
                pl.col("daily_return_1d") >= _liquidity_migration_hot_return_threshold_expr(config)
            )
            gates.append((
                "market_or_hot_coin",
                market_ok | hot_ok,
                pl.col("market_pct_up_1d"),
                config.liquidity_migration_market_pct_up_max,
            ))
        else:
            gates.append((
                "market_pct_up_max",
                market_ok,
                pl.col("market_pct_up_1d"),
                config.liquidity_migration_market_pct_up_max,
            ))

    # Build a column per gate carrying the pass-bool + value on the full base
    # frame (gate predicates reference feature columns that only exist on `base`,
    # not on a slimmer projection), then derive the first-failing label.
    annotated = base.with_columns([
        *[predicate.fill_null(False).alias(f"_pass_{idx}") for idx, (_, predicate, *_) in enumerate(gates)],
        *[value.cast(pl.Float64, strict=False).alias(f"_val_{idx}") for idx, (_, _, value, _) in enumerate(gates)],
    ])

    # Construct the first-failing expression: reverse the chain so later gates
    # only label when no earlier gate failed.
    first_gate_expr: pl.Expr = pl.lit("")
    first_value_expr: pl.Expr = pl.lit(None, dtype=pl.Float64)
    first_threshold_expr: pl.Expr = pl.lit(None, dtype=pl.Float64)
    for idx, (name, _, _, threshold) in reversed(list(enumerate(gates))):
        first_gate_expr = pl.when(~pl.col(f"_pass_{idx}")).then(pl.lit(name)).otherwise(first_gate_expr)
        first_value_expr = pl.when(~pl.col(f"_pass_{idx}")).then(pl.col(f"_val_{idx}")).otherwise(first_value_expr)
        first_threshold_expr = pl.when(~pl.col(f"_pass_{idx}")).then(pl.lit(threshold)).otherwise(first_threshold_expr)

    result = annotated.with_columns([
        first_gate_expr.alias("first_failing_gate"),
        first_value_expr.alias("first_failing_value"),
        first_threshold_expr.alias("first_failing_threshold"),
    ]).select(["symbol", "ts_ms", "first_failing_gate", "first_failing_value", "first_failing_threshold"])

    return result

def _exclude_symbols(frame: pl.DataFrame, symbols: tuple[str, ...]) -> pl.DataFrame:
    if frame.is_empty() or "symbol" not in frame.columns or not symbols:
        return frame
    excluded = {symbol.upper() for symbol in symbols}
    return frame.filter(~pl.col("symbol").str.to_uppercase().is_in(sorted(excluded)))

def _apply_market_context_filters(frame: pl.DataFrame, config: VolumeEventResearchConfig) -> pl.DataFrame:
    output = frame
    if config.market_median_return_1d_min > -1.0 or config.market_median_return_1d_max < 1.0:
        if "market_median_return_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("market_median_return_1d") >= config.market_median_return_1d_min)
            & (pl.col("market_median_return_1d") <= config.market_median_return_1d_max)
        )
    if config.market_pct_up_1d_min > 0.0 or config.market_pct_up_1d_max < 1.0:
        if "market_pct_up_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("market_pct_up_1d") >= config.market_pct_up_1d_min)
            & (pl.col("market_pct_up_1d") <= config.market_pct_up_1d_max)
        )
    if config.btc_return_1d_min > -1.0 or config.btc_return_1d_max < 1.0:
        if "btc_return_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("btc_return_1d") >= config.btc_return_1d_min)
            & (pl.col("btc_return_1d") <= config.btc_return_1d_max)
        )
    return output

def _liquidity_migration_hot_return_threshold_expr(config: VolumeEventResearchConfig) -> pl.Expr:
    base = config.liquidity_migration_hot_market_day_return_min
    band = config.liquidity_migration_hot_market_day_return_band
    if band <= 0.0 or config.liquidity_migration_market_pct_up_max >= 1.0:
        return pl.lit(base)
    breadth_span = max(1.0 - config.liquidity_migration_market_pct_up_max, 1e-9)
    hot_breadth_position = (
        (pl.col("market_pct_up_1d") - config.liquidity_migration_market_pct_up_max).clip(0.0, breadth_span)
        / breadth_span
    )
    return pl.lit(base - band) + hot_breadth_position * (2.0 * band)

def _bottom_cut_from_top_cut(top_cut: float) -> float:
    return 1.0 - top_cut

def _event_decay_exit_hit(
    *,
    symbol: str,
    bar_end_ts_ms: int,
    rank_lookup: dict[tuple[str, int], float],
    threshold: float,
) -> bool:
    rank_fraction = rank_lookup.get((symbol, bar_end_ts_ms))
    return rank_fraction is not None and rank_fraction < threshold
