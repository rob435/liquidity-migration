from __future__ import annotations

import numpy as np
import polars as pl


from ._common import MS_PER_DAY, MS_PER_HOUR

VOLUME_SCORE_COLUMNS = {
    "volume_change_1d": "volume_change_1d_z",
    "volume_change_3d": "volume_change_3d_z",
    "volume_persistence": "volume_persistence_z",
    "dollar_volume_rank": "dollar_volume_rank_z",
    "volume_composite": "volume_composite",
}


def build_volume_features(klines: pl.DataFrame, *, aggregation_ms: int = MS_PER_DAY) -> pl.DataFrame:
    # aggregation_ms is the feature-bar interval. Default = MS_PER_DAY = the
    # current/deployed daily-close cadence (Architecture A). Architecture-B
    # research can pass a finer interval (e.g. 4h) to recompute the SAME volume
    # features on a sub-daily grid — the rolling windows below operate on bars,
    # so they are interval-agnostic; only the aggregation granularity changes.
    # The deployed call sites do not pass this, so live behavior is unchanged.
    daily_rows = _daily_bars(klines, aggregation_ms=aggregation_ms)
    if daily_rows.is_empty():
        return daily_rows

    # Vectorized per-symbol features (was a 566-iteration Python loop with numpy
    # round-trips). The rolling windows operate on bars within each symbol via
    # .over("symbol"); numerically equivalent to the prior numpy implementation
    # (last-bit float-order differences from polars rolling vs numpy cumsum-diff are
    # immaterial — see test_build_volume_features_matches_numpy_reference). The
    # leading-edge nulls are filled to NaN to match the numpy nan-padding so the
    # downstream cross-sectional-z (which treats both as not-finite) is unchanged.
    tq = pl.col("turnover_quote")
    df = (
        daily_rows.sort(["symbol", "ts_ms"])
        .with_columns((tq + 1.0).log().alias("log_turnover"))
        .with_columns(
            tq.rolling_sum(window_size=3).over("symbol").alias("_roll3"),
            tq.rolling_mean(window_size=20).over("symbol").alias("_roll20_mean"),
        )
        .with_columns(
            ((tq + 1.0) / (tq.shift(1).over("symbol") + 1.0)).log()
                .fill_null(float("nan")).alias("volume_change_1d_raw"),
            ((pl.col("_roll3") + 1.0) / (pl.col("_roll3").shift(3).over("symbol") + 1.0)).log()
                .fill_null(float("nan")).alias("volume_change_3d_raw"),
            ((pl.col("_roll3") / 3.0 + 1.0) / (pl.col("_roll20_mean") + 1.0)).log()
                .fill_null(float("nan")).alias("volume_persistence_raw"),
            pl.col("log_turnover").alias("dollar_volume_rank_raw"),
        )
        .select(
            "ts_ms", "symbol", "turnover_quote", "log_turnover",
            "volume_change_1d_raw", "volume_change_3d_raw",
            "volume_persistence_raw", "dollar_volume_rank_raw",
        )
        .sort(["ts_ms", "symbol"])
    )
    for raw_col in (
        "volume_change_1d_raw",
        "volume_change_3d_raw",
        "volume_persistence_raw",
        "dollar_volume_rank_raw",
    ):
        df = _add_cross_sectional_z(df, raw_col, raw_col.replace("_raw", "_z"))
    df = _add_liquidity_rank(df)
    return df.with_columns(
        (
            0.35 * pl.col("volume_change_1d_z").fill_nan(0.0)
            + 0.35 * pl.col("volume_change_3d_z").fill_nan(0.0)
            + 0.20 * pl.col("volume_persistence_z").fill_nan(0.0)
            + 0.10 * pl.col("dollar_volume_rank_z").fill_nan(0.0)
        )
        .clip(-3.0, 3.0)
        .alias("volume_composite")
    )


def _daily_bars(klines: pl.DataFrame, *, aggregation_ms: int = MS_PER_DAY) -> pl.DataFrame:
    # Bar-completeness threshold scales with the interval: a daily bar (24 hourly
    # bars) requires >=20 (~83%); a finer aggregation_ms requires the same fraction
    # of its expected hourly-bar count. For aggregation_ms = MS_PER_DAY this is
    # exactly 20, so the daily default is byte-identical to the prior hard-coded
    # behavior (verified by test_daily_bars_default_matches_legacy).
    bars_per_interval = max(1, aggregation_ms // MS_PER_HOUR)
    min_bars = max(1, round((20.0 / 24.0) * bars_per_interval))
    return (
        klines.with_columns(
            [
                (pl.col("ts_ms") - (pl.col("ts_ms") % aggregation_ms)).alias("day_start_ms"),
            ]
        )
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg(
            [
                pl.col("turnover_quote").sum().alias("turnover_quote"),
                pl.col("close").last().alias("close"),
                pl.len().alias("hourly_bars"),
            ]
        )
        .filter(pl.col("hourly_bars") >= min_bars)
        .with_columns((pl.col("day_start_ms") + aggregation_ms).alias("ts_ms"))
        .select(["ts_ms", "symbol", "turnover_quote", "close", "hourly_bars"])
        .sort(["ts_ms", "symbol"])
    )


def _add_cross_sectional_z(df: pl.DataFrame, input_col: str, output_col: str) -> pl.DataFrame:
    # Vectorized robust cross-sectional z per ts_ms (was a per-ts_ms Python loop +
    # numpy). Center = median, scale = 1.4826*MAD with a population-std fallback when
    # MAD~0; computed over FINITE values only, and only when >=3 are finite. Rows
    # that are not finite / under-populated / zero-scale get NaN — same as the numpy
    # version (numerically equivalent; polars median/std vs numpy differ only at the
    # last float bit). NaN is used (not null) so the downstream fill_nan still applies.
    finite = pl.col(input_col).is_finite()
    val = pl.when(finite).then(pl.col(input_col)).otherwise(None)
    center = val.median().over("ts_ms")
    mad = (val - center).abs().median().over("ts_ms")
    std = val.std(ddof=0).over("ts_ms")
    scale = pl.when(mad > 1e-12).then(1.4826 * mad).otherwise(std)
    n_finite = finite.sum().over("ts_ms")
    z = (
        pl.when(finite & (n_finite >= 3) & (scale > 1e-12))
        .then(((pl.col(input_col) - center) / scale).clip(-3.0, 3.0))
        .otherwise(float("nan"))
    )
    return df.with_columns(z.alias(output_col)).sort(["ts_ms", "symbol"])


def _add_liquidity_rank(df: pl.DataFrame) -> pl.DataFrame:
    # Vectorized per-ts_ms ordinal liquidity rank (was a per-ts_ms Python loop).
    rank = pl.col("log_turnover").rank(method="ordinal", descending=True).over("ts_ms")
    count = pl.len().over("ts_ms")
    return df.with_columns(
        rank.alias("liquidity_rank"),
        count.cast(pl.Int64).alias("universe_count"),
    ).with_columns(
        (pl.col("liquidity_rank") / pl.col("universe_count").cast(pl.Float64)).alias("liquidity_rank_pct")
    ).sort(["ts_ms", "symbol"])


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    output = np.full(values.shape, np.nan, dtype=float)
    if window <= 0 or window > values.size:
        return output
    cs = np.cumsum(values)
    output[window - 1] = cs[window - 1]
    if window < values.size:
        output[window:] = cs[window:] - cs[:-window]
    return output


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    s = _rolling_sum(values, window)
    valid = ~np.isnan(s)
    s[valid] /= window
    return s
