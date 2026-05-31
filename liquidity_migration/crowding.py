from __future__ import annotations

import polars as pl

from ._common import MS_PER_HOUR

CROWDING_TRADEABLE_CLASSES = {
    "isolated_idiosyncratic_event",
    "liquidity_migration_idiosyncratic",
}


def classify_liquidity_migration_crowding(
    events: pl.DataFrame,
    *,
    entry_delay_hours: int = 1,
    signal_ts_col: str = "ts_ms",
    entry_ts_col: str = "entry_ts_ms",
) -> pl.DataFrame:
    if events.is_empty():
        return events
    frame = _with_numeric_columns(
        events,
        {
            "market_pct_up_1d": 0.5,
            "btc_return_1d": 0.0,
            "daily_return_1d": 0.0,
            "residual_return_1d": 0.0,
            "signal_day_last6h_return": 0.0,
            "signal_day_last6h_turnover_share": 0.0,
            "signal_day_close_location": 0.0,
            "signal_day_range_pct": 0.0,
            "liquidity_migration_turnover_ratio": 0.0,
            "pit_age_days": 10_000.0,
            "liquidity_rank": 0.0,
            "event_rank_fraction": 0.0,
        },
    )
    if "liquidity_migration_turnover_ratio" not in events.columns and {"turnover_quote", "prior7_turnover_quote_mean"}.issubset(set(events.columns)):
        frame = frame.with_columns(
            pl.when(pl.col("prior7_turnover_quote_mean") > 0.0)
            .then(pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean"))
            .otherwise(0.0)
            .alias("liquidity_migration_turnover_ratio")
        )
    entry_hour_expr = _entry_hour_expr(frame, signal_ts_col=signal_ts_col, entry_ts_col=entry_ts_col, entry_delay_hours=entry_delay_hours)
    annotated = (
        frame.with_columns(entry_hour_expr.alias("crowding_entry_hour"))
        .with_columns(
            [
                pl.len().over("crowding_entry_hour").alias("crowding_entry_hour_signal_count"),
                pl.col("market_pct_up_1d").mean().over("crowding_entry_hour").alias("crowding_hour_market_pct_up_mean"),
                pl.col("daily_return_1d").mean().over("crowding_entry_hour").alias("crowding_hour_day_return_mean"),
                pl.col("residual_return_1d").mean().over("crowding_entry_hour").alias("crowding_hour_residual_return_mean"),
                pl.col("residual_return_1d").std().over("crowding_entry_hour").fill_null(0.0).alias("crowding_hour_residual_return_std"),
                pl.col("liquidity_migration_turnover_ratio")
                .mean()
                .over("crowding_entry_hour")
                .alias("crowding_hour_turnover_ratio_mean"),
                pl.col("liquidity_migration_turnover_ratio")
                .max()
                .over("crowding_entry_hour")
                .alias("crowding_hour_turnover_ratio_max"),
                pl.col("signal_day_last6h_return").mean().over("crowding_entry_hour").alias("crowding_hour_last6h_return_mean"),
                pl.col("signal_day_last6h_turnover_share")
                .mean()
                .over("crowding_entry_hour")
                .alias("crowding_hour_last6h_turnover_share_mean"),
                pl.col("signal_day_last6h_turnover_share")
                .max()
                .over("crowding_entry_hour")
                .alias("crowding_hour_last6h_turnover_share_max"),
                pl.col("liquidity_rank").std().over("crowding_entry_hour").fill_null(0.0).alias("crowding_hour_liquidity_rank_std"),
            ]
        )
    )
    artifact = (
        (pl.col("signal_day_last6h_turnover_share") >= 0.90)
        | (
            (pl.col("liquidity_migration_turnover_ratio") >= 80.0)
            & (pl.col("signal_day_last6h_turnover_share") >= 0.65)
        )
        | (pl.col("pit_age_days") < 14.0)
    )
    full_market = (
        (pl.col("crowding_entry_hour_signal_count") >= 2)
        & ((pl.col("market_pct_up_1d") >= 0.72) | (pl.col("btc_return_1d") >= 0.035))
        & (pl.col("residual_return_1d") <= 0.10)
    )
    sector_theme = (
        (pl.col("crowding_entry_hour_signal_count") >= 2)
        & (pl.col("crowding_hour_residual_return_mean") >= 0.075)
        & (pl.col("crowding_hour_residual_return_std") <= 0.08)
        & (pl.col("crowding_hour_last6h_turnover_share_max") < 0.90)
    )
    isolated = (
        (pl.col("crowding_entry_hour_signal_count") == 1)
        & (pl.col("residual_return_1d") >= 0.08)
        & (pl.col("market_pct_up_1d") <= 0.65)
        & (pl.col("signal_day_last6h_turnover_share") < 0.75)
    )
    liquidity_idio = (
        (pl.col("residual_return_1d") >= 0.08)
        & (pl.col("market_pct_up_1d") <= 0.68)
        & (pl.col("signal_day_last6h_turnover_share") < 0.85)
        & (pl.col("liquidity_migration_turnover_ratio").is_between(3.0, 60.0))
    )
    return (
        annotated.with_columns(
            pl.when(artifact)
            .then(pl.lit("exchange_liquidity_artifact"))
            .when(full_market)
            .then(pl.lit("full_market_impulse"))
            .when(sector_theme)
            .then(pl.lit("sector_theme_wave"))
            .when(isolated)
            .then(pl.lit("isolated_idiosyncratic_event"))
            .when(liquidity_idio)
            .then(pl.lit("liquidity_migration_idiosyncratic"))
            .otherwise(pl.lit("uncertain_cluster"))
            .alias("crowding_class")
        )
        .with_columns(pl.col("crowding_class").is_in(sorted(CROWDING_TRADEABLE_CLASSES)).alias("crowding_tradeable"))
        .with_columns(_crowding_reason_expr().alias("crowding_reason"))
    )


def _entry_hour_expr(frame: pl.DataFrame, *, signal_ts_col: str, entry_ts_col: str, entry_delay_hours: int) -> pl.Expr:
    if entry_ts_col in frame.columns:
        return (pl.col(entry_ts_col) // MS_PER_HOUR) * MS_PER_HOUR
    if signal_ts_col in frame.columns:
        return ((pl.col(signal_ts_col) + entry_delay_hours * MS_PER_HOUR) // MS_PER_HOUR) * MS_PER_HOUR
    return pl.lit(0)


def _with_numeric_columns(frame: pl.DataFrame, defaults: dict[str, float]) -> pl.DataFrame:
    output = frame
    for column, default in defaults.items():
        if column not in output.columns:
            output = output.with_columns(pl.lit(default).alias(column))
        else:
            output = output.with_columns(pl.col(column).cast(pl.Float64, strict=False).fill_null(default).alias(column))
    return output


def _crowding_reason_expr() -> pl.Expr:
    return (
        pl.when(pl.col("crowding_class") == "exchange_liquidity_artifact")
        .then(pl.lit("late turnover concentration, extreme turnover expansion, or very young listing"))
        .when(pl.col("crowding_class") == "full_market_impulse")
        .then(pl.lit("same-hour cluster during broad market-up impulse"))
        .when(pl.col("crowding_class") == "sector_theme_wave")
        .then(pl.lit("same-hour residual-return cluster with similar cross-sectional behavior"))
        .when(pl.col("crowding_class") == "isolated_idiosyncratic_event")
        .then(pl.lit("single idiosyncratic event with contained market breadth"))
        .when(pl.col("crowding_class") == "liquidity_migration_idiosyncratic")
        .then(pl.lit("residual liquidity-migration event without broad-market or artifact flags"))
        .otherwise(pl.lit("cluster did not clear idiosyncratic, market, sector, or artifact rules"))
    )
