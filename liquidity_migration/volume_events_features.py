"""Extracted from volume_events.py — see that module's docstring.

A cohesive slice of volume_events, split out to keep the hub readable.
Imports shared helpers from volume_events (the hub); the hub re-imports
this module's public names at the bottom so external callers
(`from liquidity_migration.volume_events import X`) keep working.
"""

from __future__ import annotations


import polars as pl

from ._common import MS_PER_DAY
from .volume_features import VOLUME_SCORE_COLUMNS


# Splits live exclusively on VolumeEventResearchConfig.splits now (default ()).
# Whole-period reporting is the post-rebuild norm; pristine OOS is the forward
# demo/paper ledger, not a backtest window.

from .volume_events import (  # noqa: F401  (shared hub helpers)
    SIDE_HYPOTHESES,
    _has_columns,
)




def _enriched_event_features(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    *,
    funding: pl.DataFrame | None = None,
    open_interest: pl.DataFrame | None = None,
    signed_flow_1h: pl.DataFrame | None = None,
    mark_price_1h: pl.DataFrame | None = None,
    index_price_1h: pl.DataFrame | None = None,
    premium_index_1h: pl.DataFrame | None = None,
) -> pl.DataFrame:
    if features.is_empty():
        return features
    daily_returns = _daily_return_frame(klines)
    enriched = features.join(daily_returns, on=["ts_ms", "symbol"], how="left")
    funding_features = _funding_feature_frame(funding)
    if not funding_features.is_empty():
        enriched = enriched.join(funding_features, on=["ts_ms", "symbol"], how="left")
    open_interest_features = _open_interest_feature_frame(open_interest, daily_returns)
    if not open_interest_features.is_empty():
        enriched = enriched.join(open_interest_features, on=["ts_ms", "symbol"], how="left")
    flow_features = _signed_flow_feature_frame(signed_flow_1h)
    if not flow_features.is_empty():
        enriched = enriched.join(flow_features, on=["ts_ms", "symbol"], how="left")
    basis_features = _basis_feature_frame(mark_price_1h, index_price_1h, premium_index_1h)
    if not basis_features.is_empty():
        enriched = enriched.join(basis_features, on=["ts_ms", "symbol"], how="left")
    if _has_columns(enriched, "turnover_quote", "open_interest_quote"):
        enriched = enriched.with_columns(
            pl.when(pl.col("open_interest_quote") > 0.0)
            .then(pl.col("turnover_quote") / pl.col("open_interest_quote"))
            .otherwise(None)
            .alias("volume_to_open_interest_quote")
        )
    if "daily_return_1d" in enriched.columns:
        enriched = enriched.with_columns(pl.col("daily_return_1d").abs().alias("abs_daily_return_1d"))
        enriched = _attach_market_context(enriched)
        if "market_median_return_1d" in enriched.columns:
            enriched = enriched.with_columns(
                (pl.col("daily_return_1d") - pl.col("market_median_return_1d")).alias("residual_return_1d")
            )
    rank_inputs = {
        "volume_change_1d_z": "volume_change_1d_z_rank_frac",
        "volume_change_3d_z": "volume_change_3d_z_rank_frac",
        "volume_persistence_z": "volume_persistence_z_rank_frac",
        "dollar_volume_rank_z": "dollar_volume_rank_z_rank_frac",
        "volume_composite": "volume_composite_rank_frac",
        "daily_return_1d": "daily_return_rank_frac",
        "abs_daily_return_1d": "abs_daily_return_rank_frac",
        "residual_return_1d": "residual_return_rank_frac",
        "close_position_1d": "close_position_rank_frac",
        "close_vs_prior20_high": "close_vs_prior20_high_rank_frac",
        "prior7_return": "prior7_return_rank_frac",
        "prior20_drawdown": "prior20_drawdown_rank_frac",
        "return_7d": "return_7d_rank_frac",
        "prior30_max_daily_return": "prior30_max_daily_return_rank_frac",
        "prior7_return_volatility": "prior7_return_volatility_rank_frac",
        "intraday_range_1d": "intraday_range_rank_frac",
        "intraday_range_expansion_7d": "intraday_range_expansion_rank_frac",
        "funding_rate_last": "funding_rate_last_rank_frac",
        "funding_rate_3d_sum": "funding_rate_3d_sum_rank_frac",
        "funding_rate_7d_sum": "funding_rate_7d_sum_rank_frac",
        "mark_index_basis_last": "mark_index_basis_last_rank_frac",
        "mark_index_basis_1d_mean": "mark_index_basis_1d_mean_rank_frac",
        "mark_index_basis_3d_mean": "mark_index_basis_3d_mean_rank_frac",
        "premium_index_last": "premium_index_last_rank_frac",
        "premium_index_1d_mean": "premium_index_1d_mean_rank_frac",
        "premium_index_3d_mean": "premium_index_3d_mean_rank_frac",
        "open_interest_return_3d": "open_interest_return_3d_rank_frac",
        "open_interest_return_7d": "open_interest_return_7d_rank_frac",
        "volume_to_open_interest_quote": "volume_to_open_interest_quote_rank_frac",
        "taker_imbalance_1d": "taker_imbalance_1d_rank_frac",
        "taker_imbalance_3d": "taker_imbalance_3d_rank_frac",
    }
    enriched = _add_rank_fractions_batch(
        enriched,
        [(source, alias) for source, alias in rank_inputs.items() if source in enriched.columns],
    )
    enriched = _add_event_uniqueness_score(enriched)
    if "event_uniqueness_score" in enriched.columns:
        enriched = _add_rank_fractions_batch(
            enriched, [("event_uniqueness_score", "event_uniqueness_score_rank_frac")]
        )
    enriched = _add_reclaim_scores(enriched)
    reclaim_rank_inputs = {
        "reclaim_breakout_score": "reclaim_breakout_score_rank_frac",
        "capitulation_reclaim_score": "capitulation_reclaim_score_rank_frac",
        "orderly_leadership_pullback_score": "orderly_leadership_pullback_score_rank_frac",
        "volume_shelf_reclaim_score": "volume_shelf_reclaim_score_rank_frac",
    }
    enriched = _add_rank_fractions_batch(
        enriched,
        [(source, alias) for source, alias in reclaim_rank_inputs.items() if source in enriched.columns],
    )
    shift_cols = [
        "volume_change_1d_z_rank_frac",
        "volume_persistence_z_rank_frac",
        "dollar_volume_rank_z_rank_frac",
        "volume_composite_rank_frac",
        "reclaim_breakout_score_rank_frac",
        "capitulation_reclaim_score_rank_frac",
        "orderly_leadership_pullback_score_rank_frac",
        "volume_shelf_reclaim_score_rank_frac",
    ]
    expressions = []
    for col in shift_cols:
        if col in enriched.columns:
            expressions.append(pl.col(col).shift(1).over("symbol").alias(f"prior_{col}"))
            expressions.append(pl.col(col).shift(7).over("symbol").alias(f"prior7_{col}"))
    expressions.extend(
        [
            pl.col("volume_persistence_z_rank_frac")
            .shift(1)
            .rolling_min(window_size=3, min_samples=1)
            .over("symbol")
            .alias("prior3_volume_persistence_rank_min"),
            pl.col("volume_persistence_z_rank_frac")
            .shift(1)
            .rolling_max(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_volume_persistence_rank_max"),
            pl.col("abs_daily_return_1d")
            .shift(1)
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_abs_daily_return_mean"),
            pl.col("liquidity_rank").shift(7).over("symbol").alias("prior7_liquidity_rank"),
            pl.col("liquidity_rank").shift(1).over("symbol").alias("prior1_liquidity_rank"),
            pl.col("liquidity_rank").shift(3).over("symbol").alias("prior3_liquidity_rank"),
            pl.col("turnover_quote")
            .shift(1)
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_turnover_quote_mean"),
        ]
    )
    enriched = enriched.sort(["symbol", "ts_ms"]).with_columns(expressions)
    enriched = _add_liquidity_migration_speed_features(enriched)
    return _attach_event_archive_membership(
        enriched.sort(["ts_ms", "symbol"]),
        archive_manifest,
    )

def _funding_feature_frame(funding: pl.DataFrame | None) -> pl.DataFrame:
    if funding is None or funding.is_empty() or not _has_columns(funding, "symbol", "ts_ms"):
        return pl.DataFrame()
    rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in funding.columns else "funding_rate"
    if rate_col not in funding.columns:
        return pl.DataFrame()
    daily = (
        funding.select(["symbol", "ts_ms", rate_col])
        .drop_nulls(["symbol", "ts_ms", rate_col])
        .sort(["symbol", "ts_ms"])
        .with_columns((((pl.col("ts_ms") - 1) // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(
            [
                pl.col(rate_col).last().alias("funding_rate_last"),
                pl.col(rate_col).sum().alias("funding_rate_1d_sum"),
                (pl.col(rate_col) > 0.0).mean().alias("funding_positive_fraction_1d"),
                pl.len().alias("funding_event_count_1d"),
            ]
        )
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
        .with_columns(
            [
                pl.col("funding_rate_1d_sum")
                .rolling_sum(window_size=3, min_samples=1)
                .over("symbol")
                .alias("funding_rate_3d_sum"),
                pl.col("funding_rate_1d_sum")
                .rolling_sum(window_size=7, min_samples=1)
                .over("symbol")
                .alias("funding_rate_7d_sum"),
                pl.col("funding_rate_1d_sum")
                .rolling_mean(window_size=7, min_samples=1)
                .over("symbol")
                .alias("funding_rate_7d_mean"),
                pl.col("funding_positive_fraction_1d")
                .rolling_mean(window_size=7, min_samples=1)
                .over("symbol")
                .alias("funding_positive_fraction_7d"),
            ]
        )
    )
    return daily

def _open_interest_feature_frame(open_interest: pl.DataFrame | None, daily_returns: pl.DataFrame) -> pl.DataFrame:
    if open_interest is None or open_interest.is_empty() or not _has_columns(open_interest, "symbol", "ts_ms", "open_interest"):
        return pl.DataFrame()
    daily = (
        open_interest.select(["symbol", "ts_ms", "open_interest"])
        .drop_nulls(["symbol", "ts_ms", "open_interest"])
        .sort(["symbol", "ts_ms"])
        .with_columns((((pl.col("ts_ms") - 1) // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(pl.col("open_interest").last().alias("open_interest"))
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    if not daily_returns.is_empty() and _has_columns(daily_returns, "symbol", "ts_ms", "daily_close"):
        daily = daily.join(daily_returns.select(["symbol", "ts_ms", "daily_close"]), on=["symbol", "ts_ms"], how="left")
        daily = daily.with_columns((pl.col("open_interest") * pl.col("daily_close")).alias("open_interest_quote"))
    else:
        daily = daily.with_columns(pl.lit(None, dtype=pl.Float64).alias("open_interest_quote"))
    return daily.with_columns(
        [
            (pl.col("open_interest") / pl.col("open_interest").shift(1).over("symbol") - 1.0).alias(
                "open_interest_return_1d"
            ),
            (pl.col("open_interest") / pl.col("open_interest").shift(3).over("symbol") - 1.0).alias(
                "open_interest_return_3d"
            ),
            (pl.col("open_interest") / pl.col("open_interest").shift(7).over("symbol") - 1.0).alias(
                "open_interest_return_7d"
            ),
            (pl.col("open_interest_quote") / pl.col("open_interest_quote").shift(1).over("symbol") - 1.0).alias(
                "open_interest_quote_return_1d"
            ),
            (pl.col("open_interest_quote") / pl.col("open_interest_quote").shift(3).over("symbol") - 1.0).alias(
                "open_interest_quote_return_3d"
            ),
            (pl.col("open_interest_quote") / pl.col("open_interest_quote").shift(7).over("symbol") - 1.0).alias(
                "open_interest_quote_return_7d"
            ),
        ]
    )

def _signed_flow_feature_frame(flow: pl.DataFrame | None) -> pl.DataFrame:
    if flow is None or flow.is_empty() or not _has_columns(flow, "symbol", "ts_ms"):
        return pl.DataFrame()
    required = {"buy_quote", "sell_quote", "signed_quote", "total_quote"}
    if not required <= set(flow.columns):
        return pl.DataFrame()
    daily = (
        flow.select(["symbol", "ts_ms", "buy_quote", "sell_quote", "signed_quote", "total_quote"])
        .drop_nulls(["symbol", "ts_ms"])
        .sort(["symbol", "ts_ms"])
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY + 1) * MS_PER_DAY).alias("ts_ms"))
        .group_by(["symbol", "ts_ms"], maintain_order=True)
        .agg(
            [
                pl.col("buy_quote").sum().alias("taker_buy_quote_1d"),
                pl.col("sell_quote").sum().alias("taker_sell_quote_1d"),
                pl.col("signed_quote").sum().alias("taker_signed_quote_1d"),
                pl.col("total_quote").sum().alias("taker_total_quote_1d"),
            ]
        )
        .with_columns(
            pl.when(pl.col("taker_total_quote_1d") > 0.0)
            .then(pl.col("taker_signed_quote_1d") / pl.col("taker_total_quote_1d"))
            .otherwise(None)
            .alias("taker_imbalance_1d")
        )
        .sort(["symbol", "ts_ms"])
        .with_columns(
            [
                pl.col("taker_signed_quote_1d")
                .rolling_sum(window_size=3, min_samples=1)
                .over("symbol")
                .alias("taker_signed_quote_3d"),
                pl.col("taker_total_quote_1d")
                .rolling_sum(window_size=3, min_samples=1)
                .over("symbol")
                .alias("taker_total_quote_3d"),
            ]
        )
        .with_columns(
            pl.when(pl.col("taker_total_quote_3d") > 0.0)
            .then(pl.col("taker_signed_quote_3d") / pl.col("taker_total_quote_3d"))
            .otherwise(None)
            .alias("taker_imbalance_3d")
        )
    )
    return daily

def _basis_feature_frame(
    mark_price_1h: pl.DataFrame | None,
    index_price_1h: pl.DataFrame | None,
    premium_index_1h: pl.DataFrame | None,
) -> pl.DataFrame:
    basis = _mark_index_basis_frame(mark_price_1h, index_price_1h)
    premium = _premium_index_frame(premium_index_1h)
    if basis.is_empty():
        return premium
    if premium.is_empty():
        return basis
    return basis.join(premium, on=["symbol", "ts_ms"], how="full", coalesce=True).sort(["symbol", "ts_ms"])

def _mark_index_basis_frame(mark_price_1h: pl.DataFrame | None, index_price_1h: pl.DataFrame | None) -> pl.DataFrame:
    if (
        mark_price_1h is None
        or index_price_1h is None
        or mark_price_1h.is_empty()
        or index_price_1h.is_empty()
        or not _has_columns(mark_price_1h, "symbol", "ts_ms", "close")
        or not _has_columns(index_price_1h, "symbol", "ts_ms", "close")
    ):
        return pl.DataFrame()
    basis = (
        mark_price_1h.select(["symbol", "ts_ms", pl.col("close").alias("mark_price_close")])
        .drop_nulls(["symbol", "ts_ms", "mark_price_close"])
        .join(
            index_price_1h.select(["symbol", "ts_ms", pl.col("close").alias("index_price_close")]).drop_nulls(
                ["symbol", "ts_ms", "index_price_close"]
            ),
            on=["symbol", "ts_ms"],
            how="inner",
        )
        .filter(pl.col("index_price_close") > 0.0)
        .with_columns((pl.col("mark_price_close") / pl.col("index_price_close") - 1.0).alias("mark_index_basis"))
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(
            [
                pl.col("mark_index_basis").last().alias("mark_index_basis_last"),
                pl.col("mark_index_basis").mean().alias("mark_index_basis_1d_mean"),
                pl.col("mark_index_basis").abs().max().alias("mark_index_basis_1d_abs_max"),
            ]
        )
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    return basis.with_columns(
        [
            pl.col("mark_index_basis_1d_mean")
            .rolling_mean(window_size=3, min_samples=1)
            .over("symbol")
            .alias("mark_index_basis_3d_mean"),
            pl.col("mark_index_basis_1d_mean")
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("mark_index_basis_7d_mean"),
        ]
    )

def _premium_index_frame(premium_index_1h: pl.DataFrame | None) -> pl.DataFrame:
    if premium_index_1h is None or premium_index_1h.is_empty() or not _has_columns(premium_index_1h, "symbol", "ts_ms", "close"):
        return pl.DataFrame()
    premium = (
        premium_index_1h.select(["symbol", "ts_ms", pl.col("close").alias("premium_index")])
        .drop_nulls(["symbol", "ts_ms", "premium_index"])
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(
            [
                pl.col("premium_index").last().alias("premium_index_last"),
                pl.col("premium_index").mean().alias("premium_index_1d_mean"),
                pl.col("premium_index").abs().max().alias("premium_index_1d_abs_max"),
            ]
        )
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    return premium.with_columns(
        [
            pl.col("premium_index_1d_mean")
            .rolling_mean(window_size=3, min_samples=1)
            .over("symbol")
            .alias("premium_index_3d_mean"),
            pl.col("premium_index_1d_mean")
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("premium_index_7d_mean"),
        ]
    )

def _add_event_uniqueness_score(features: pl.DataFrame) -> pl.DataFrame:
    required = {
        "residual_return_rank_frac",
        "volume_change_1d_z_rank_frac",
        "dollar_volume_rank_z_rank_frac",
        "intraday_range_expansion_rank_frac",
        "market_pct_up_1d",
    }
    if features.is_empty() or not required.issubset(set(features.columns)):
        return features
    market_isolation = (1.0 - pl.col("market_pct_up_1d").clip(0.0, 1.0)).fill_null(0.5).fill_nan(0.5)
    return features.with_columns(
        (
            0.30 * pl.col("residual_return_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.25 * pl.col("volume_change_1d_z_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.20 * pl.col("dollar_volume_rank_z_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.15 * pl.col("intraday_range_expansion_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.10 * market_isolation
        )
        .clip(0.0, 1.0)
        .alias("event_uniqueness_score")
    )

def _add_liquidity_migration_speed_features(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty() or not _has_columns(
        features,
        "liquidity_rank",
        "prior1_liquidity_rank",
        "prior3_liquidity_rank",
        "prior7_liquidity_rank",
    ):
        return features
    # liquidity_rank is u32 (it's a positive rank computed via volume_features).
    # u32 - u32 in polars produces an unsigned result, so when current_rank is
    # numerically larger than prior_rank (i.e. liquidity got WORSE in the
    # period), the subtraction wraps to a huge value near 2^32 instead of
    # going negative. That made `improvement >= rank_improvement_min` falsely
    # True for symbols whose ranks actually deteriorated (observed 2026-05-26
    # on WAVESUSDT: prior7=111, current=201, raw subtraction = 4294967206).
    # Cast to Int64 first so the diff carries a real sign.
    prior1 = pl.col("prior1_liquidity_rank").cast(pl.Int64)
    prior3 = pl.col("prior3_liquidity_rank").cast(pl.Int64)
    prior7 = pl.col("prior7_liquidity_rank").cast(pl.Int64)
    current = pl.col("liquidity_rank").cast(pl.Int64)
    return features.with_columns(
        [
            (prior1 - current).alias("liquidity_rank_improvement_1d"),
            (prior3 - current).alias("liquidity_rank_improvement_3d"),
            (prior7 - current).alias("liquidity_rank_improvement_7d"),
            ((prior3 - current) / 3.0).alias("liquidity_rank_speed_3d"),
        ]
    )

def _attach_market_context(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty() or "daily_return_1d" not in features.columns:
        return features
    market_context = (
        features.group_by("ts_ms")
        .agg(
            [
                pl.col("daily_return_1d").median().alias("market_median_return_1d"),
                pl.col("daily_return_1d").mean().alias("market_mean_return_1d"),
                (pl.col("daily_return_1d") > 0.0).mean().alias("market_pct_up_1d"),
                pl.col("abs_daily_return_1d").median().alias("market_median_abs_return_1d"),
            ]
        )
        .sort("ts_ms")
        .with_columns(
            [
                pl.col("market_median_return_1d").rolling_sum(7).alias("market_median_return_7d_sum"),
                pl.col("market_median_return_1d").rolling_sum(30).alias("market_median_return_30d_sum"),
                pl.col("market_pct_up_1d").rolling_mean(7).alias("market_pct_up_7d_mean"),
                pl.col("market_pct_up_1d").rolling_mean(30).alias("market_pct_up_30d_mean"),
            ]
        )
    )
    btc_context = features.filter(pl.col("symbol") == "BTCUSDT").select(
        [
            "ts_ms",
            pl.col("daily_return_1d").alias("btc_return_1d"),
            pl.col("abs_daily_return_1d").alias("btc_abs_return_1d"),
        ]
    )
    return features.join(market_context, on="ts_ms", how="left").join(btc_context, on="ts_ms", how="left")

def _attach_event_archive_membership(features: pl.DataFrame, archive_manifest: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return features.with_columns(
            [
                pl.lit(False).alias("tradable_membership_flag"),
                pl.lit(None, dtype=pl.Int64).alias("symbol_age_days"),
                pl.lit(None, dtype=pl.Float64).alias("pit_age_days"),
            ]
        )
    frame = features
    if "date" not in frame.columns:
        frame = frame.with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
    if archive_manifest.is_empty():
        return frame.with_columns(
            [
                pl.lit(False).alias("tradable_membership_flag"),
                pl.lit(None, dtype=pl.Int64).alias("symbol_age_days"),
                pl.lit(None, dtype=pl.Float64).alias("pit_age_days"),
            ]
        )
    membership = archive_manifest.select(["symbol", "date"]).unique().with_columns(pl.lit(True).alias("tradable_membership_flag"))
    first_seen = archive_manifest.group_by("symbol").agg(pl.col("date").min().alias("first_manifest_date"))
    return (
        frame.join(membership, on=["symbol", "date"], how="left")
        .join(first_seen, on="symbol", how="left")
        .with_columns(
            [
                pl.col("tradable_membership_flag").fill_null(False),
                (
                    pl.col("date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                    - pl.col("first_manifest_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                )
                .dt.total_days()
                .cast(pl.Int64)
                .alias("symbol_age_days"),
                (
                    pl.col("date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                    - pl.col("first_manifest_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                )
                .dt.total_days()
                .cast(pl.Float64)
                .alias("pit_age_days"),
            ]
        )
    )

def _daily_return_frame(klines: pl.DataFrame) -> pl.DataFrame:
    daily = (
        klines.with_columns((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"))
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg(
            [
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
                pl.col("open").tail(6).first().alias("signal_day_last6h_open"),
                pl.col("turnover_quote").sum().alias("signal_day_turnover"),
                pl.col("turnover_quote").tail(6).sum().alias("signal_day_last6h_turnover"),
                pl.col("turnover_quote").filter(pl.col("close") > pl.col("open")).sum().alias("signal_day_up_turnover"),
                pl.len().alias("hourly_bars"),
            ]
        )
        .filter(pl.col("hourly_bars") >= 20)
        .with_columns((pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"))
        .sort(["symbol", "ts_ms"])
        .with_columns(
            [
                (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("daily_return_1d"),
                (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1.0).alias("return_3d"),
                (pl.col("close") / pl.col("close").shift(7).over("symbol") - 1.0).alias("return_7d"),
                (pl.col("close") / pl.col("close").shift(14).over("symbol") - 1.0).alias("return_14d"),
                (pl.col("close") / pl.col("close").shift(30).over("symbol") - 1.0).alias("return_30d"),
                (pl.col("high") / pl.col("low") - 1.0).alias("intraday_range_1d"),
                (pl.col("close") / pl.col("open") - 1.0).alias("daily_intraday_return_1d"),
                (pl.col("close").shift(1).over("symbol") / pl.col("close").shift(8).over("symbol") - 1.0).alias(
                    "prior7_return"
                ),
                (pl.col("close").shift(1).over("symbol") / pl.col("close").shift(15).over("symbol") - 1.0).alias(
                    "prior14_return"
                ),
                pl.col("close").shift(1).rolling_max(window_size=20, min_samples=5).over("symbol").alias(
                    "prior20_close_high"
                ),
                pl.col("close").shift(1).rolling_min(window_size=20, min_samples=5).over("symbol").alias(
                    "prior20_close_low"
                ),
                pl.when((pl.col("high") - pl.col("low")).abs() > 1e-12)
                .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
                .otherwise(0.5)
                .clip(0.0, 1.0)
                .alias("close_position_1d"),
                pl.when((pl.col("high") - pl.col("low")).abs() > 1e-12)
                .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
                .otherwise(0.5)
                .clip(0.0, 1.0)
                .alias("signal_day_close_location"),
                pl.when(pl.col("signal_day_last6h_open") > 0.0)
                .then(pl.col("close") / pl.col("signal_day_last6h_open") - 1.0)
                .otherwise(None)
                .alias("signal_day_last6h_return"),
                pl.when(pl.col("signal_day_turnover") > 0.0)
                .then(pl.col("signal_day_last6h_turnover") / pl.col("signal_day_turnover"))
                .otherwise(None)
                .alias("signal_day_last6h_turnover_share"),
                # Share of the signal day's turnover that traded in UP hours
                # (hourly close > open). One-sided up-volume = FOMO chase, the
                # unsustainable kind of pop -> a stronger fade. Causal: all of
                # the signal day's hourly bars are complete at the decision ts.
                pl.when(pl.col("signal_day_turnover") > 0.0)
                .then(pl.col("signal_day_up_turnover") / pl.col("signal_day_turnover"))
                .otherwise(None)
                .alias("up_volume_concentration"),
                pl.when(pl.col("close") > 0.0)
                .then((pl.col("high") - pl.col("low")) / pl.col("close"))
                .otherwise(None)
                .alias("signal_day_range_pct"),
            ]
        )
        .with_columns(
            [
                (pl.col("close") / pl.col("prior20_close_high") - 1.0).alias("close_vs_prior20_high"),
                (pl.col("close") / pl.col("prior20_close_low") - 1.0).alias("close_vs_prior20_low"),
                (pl.col("close").shift(1).over("symbol") / pl.col("prior20_close_high") - 1.0).alias(
                    "prior20_drawdown"
                ),
            ]
        )
        .with_columns(
            [
                (pl.col("close") / pl.col("high").rolling_max(window_size=7, min_samples=3).over("symbol") - 1.0).alias(
                    "close_to_high_7d"
                ),
                (pl.col("close") / pl.col("high").rolling_max(window_size=30, min_samples=10).over("symbol") - 1.0).alias(
                    "close_to_high_30d"
                ),
                (pl.col("close") / pl.col("low").rolling_min(window_size=7, min_samples=3).over("symbol") - 1.0).alias(
                    "close_to_low_7d"
                ),
                pl.col("daily_return_1d")
                .shift(1)
                .rolling_max(window_size=30, min_samples=5)
                .over("symbol")
                .alias("prior30_max_daily_return"),
                pl.col("daily_return_1d")
                .shift(1)
                .rolling_min(window_size=30, min_samples=5)
                .over("symbol")
                .alias("prior30_min_daily_return"),
                pl.col("daily_return_1d")
                .shift(1)
                .rolling_std(window_size=7, min_samples=4)
                .over("symbol")
                .alias("prior7_return_volatility"),
                pl.col("intraday_range_1d")
                .shift(1)
                .rolling_mean(window_size=7, min_samples=4)
                .over("symbol")
                .alias("prior7_intraday_range_mean"),
            ]
        )
        .with_columns(
            [
                pl.when(pl.col("prior7_intraday_range_mean") > 0.0)
                .then(pl.col("intraday_range_1d") / pl.col("prior7_intraday_range_mean"))
                .otherwise(None)
                .alias("intraday_range_expansion_7d"),
            ]
        )
        .select(
            [
                "ts_ms",
                "symbol",
                pl.col("close").alias("daily_close"),
                "daily_return_1d",
                "daily_intraday_return_1d",
                "return_3d",
                "return_7d",
                "return_14d",
                "return_30d",
                "prior7_return",
                "prior14_return",
                "prior20_close_high",
                "prior20_close_low",
                "close_vs_prior20_high",
                "close_vs_prior20_low",
                "prior20_drawdown",
                "intraday_range_1d",
                "close_position_1d",
                "signal_day_close_location",
                "signal_day_last6h_return",
                "signal_day_last6h_turnover_share",
                "up_volume_concentration",
                "signal_day_range_pct",
                "close_to_high_7d",
                "close_to_high_30d",
                "close_to_low_7d",
                "prior30_max_daily_return",
                "prior30_min_daily_return",
                "prior7_return_volatility",
                "prior7_intraday_range_mean",
                "intraday_range_expansion_7d",
            ]
        )
    )
    return daily

def _add_rank_fraction(frame: pl.DataFrame, source: str, alias: str) -> pl.DataFrame:
    return _add_rank_fractions_batch(frame, [(source, alias)])

def _add_rank_fractions_batch(frame: pl.DataFrame, pairs: list[tuple[str, str]]) -> pl.DataFrame:
    # Batched equivalent of N sequential _add_rank_fraction calls. Emits all
    # rank/count window passes in a single with_columns, then all divisions in
    # a single with_columns, then drops the scratch columns once. Polars can't
    # fuse across separate with_columns calls so this collapses 2N+1 query
    # plans into 3.
    if not pairs:
        return frame
    rank_count_exprs: list[pl.Expr] = []
    division_exprs: list[pl.Expr] = []
    drop_cols: list[str] = []
    for source, alias in pairs:
        rank_col = f"_{alias}_rank"
        count_col = f"_{alias}_count"
        rank_count_exprs.append(pl.col(source).rank("ordinal").over("ts_ms").alias(rank_col))
        rank_count_exprs.append(pl.col(source).count().over("ts_ms").alias(count_col))
        division_exprs.append(
            pl.when(pl.col(count_col) > 1)
            .then((pl.col(rank_col) - 1) / (pl.col(count_col) - 1))
            .otherwise(None)
            .alias(alias)
        )
        drop_cols.extend([rank_col, count_col])
    return frame.with_columns(rank_count_exprs).with_columns(division_exprs).drop(drop_cols)

def _add_reclaim_scores(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return features
    available = set(features.columns)
    output = []
    volume = pl.col("volume_change_1d_z").fill_null(0.0).fill_nan(0.0)
    persistence = pl.col("volume_persistence_z").fill_null(0.0).fill_nan(0.0)
    dollar_volume = pl.col("dollar_volume_rank_z").fill_null(0.0).fill_nan(0.0)
    daily = _centered_rank("daily_return_rank_frac")
    close_position = _centered_rank("close_position_rank_frac")
    residual = _centered_rank("residual_return_rank_frac")
    high_reclaim = _centered_rank("close_vs_prior20_high_rank_frac")
    prior_drawdown_depth = -_centered_rank("prior20_drawdown_rank_frac")
    prior_weakness = -_centered_rank("prior7_return_rank_frac")
    prior_strength = _centered_rank("prior7_return_rank_frac")
    quiet_extension = _centered_rank("abs_daily_return_rank_frac")
    reclaim_cols = {
        "volume_change_1d_z",
        "daily_return_rank_frac",
        "close_position_rank_frac",
        "residual_return_rank_frac",
        "close_vs_prior20_high_rank_frac",
        "prior7_return_rank_frac",
        "prior20_drawdown_rank_frac",
    }
    if reclaim_cols.issubset(available):
        output.extend(
            [
                (0.35 * volume + 0.20 * daily + 0.20 * close_position + 0.15 * residual + 0.10 * high_reclaim)
                .clip(-3.0, 3.0)
                .alias("reclaim_breakout_score"),
                (
                    0.30 * volume
                    + 0.20 * daily
                    + 0.20 * close_position
                    + 0.15 * residual
                    + 0.10 * prior_drawdown_depth
                    + 0.05 * prior_weakness
                )
                .clip(-3.0, 3.0)
                .alias("capitulation_reclaim_score"),
            ]
        )
    leadership_cols = {
        "dollar_volume_rank_z",
        "volume_persistence_z",
        "prior7_return_rank_frac",
        "close_position_rank_frac",
        "residual_return_rank_frac",
        "abs_daily_return_rank_frac",
    }
    if leadership_cols.issubset(available):
        output.append(
            (
                0.20 * dollar_volume
                + 0.20 * persistence
                + 0.20 * prior_strength
                + 0.15 * close_position
                + 0.15 * residual
                - 0.10 * quiet_extension
            )
            .clip(-3.0, 3.0)
            .alias("orderly_leadership_pullback_score")
        )
    shelf_cols = {
        "volume_change_1d_z",
        "volume_persistence_z",
        "daily_return_rank_frac",
        "close_position_rank_frac",
        "residual_return_rank_frac",
        "close_vs_prior20_high_rank_frac",
    }
    if shelf_cols.issubset(available):
        output.append(
            (0.35 * volume + 0.20 * daily + 0.20 * close_position + 0.15 * residual + 0.10 * high_reclaim)
            .clip(-3.0, 3.0)
            .alias("volume_shelf_reclaim_score")
        )
    return features.with_columns(output) if output else features

def _centered_rank(column: str) -> pl.Expr:
    return (pl.col(column).fill_null(0.5).fill_nan(0.5) - 0.5) * 2.0

def _event_score(event_type: str) -> tuple[str, str]:
    if event_type in {
        "fresh_volume_spike",
        "volume_exhaustion",
        "volume_absorption",
        "dryup_reacceleration",
        "selloff_exhaustion",
    }:
        return "volume_change_1d", VOLUME_SCORE_COLUMNS["volume_change_1d"]
    if event_type == "persistent_volume_breakout":
        return "volume_persistence", VOLUME_SCORE_COLUMNS["volume_persistence"]
    if event_type in {"tail_liquidity_jump", "liquidity_migration", "top_volume_leadership"}:
        return "dollar_volume_rank", VOLUME_SCORE_COLUMNS["dollar_volume_rank"]
    if event_type == "reclaim_breakout":
        return "reclaim_breakout", "reclaim_breakout_score"
    if event_type == "capitulation_reclaim":
        return "capitulation_reclaim", "capitulation_reclaim_score"
    if event_type == "orderly_leadership_pullback":
        return "orderly_leadership_pullback", "orderly_leadership_pullback_score"
    if event_type == "volume_shelf_reclaim":
        return "volume_shelf_reclaim", "volume_shelf_reclaim_score"
    raise ValueError(f"Unknown event type: {event_type}")

def _scenario_side(event_type: str, side_hypothesis: str) -> str:
    if side_hypothesis not in SIDE_HYPOTHESES:
        raise ValueError(f"Unknown side hypothesis: {side_hypothesis}")
    if event_type == "selloff_exhaustion":
        return "short" if side_hypothesis == "continuation" else "long"
    return "long" if side_hypothesis == "continuation" else "short"
