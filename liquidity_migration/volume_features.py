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

    symbol_frames = []
    for key, part in daily_rows.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        turnover = np.asarray(part["turnover_quote"].to_list(), dtype=float)
        log_turnover = np.log(turnover + 1.0)
        roll_3 = _rolling_sum(turnover, 3)
        roll_20_mean = _rolling_mean(turnover, 20)
        n = turnover.size

        vc1 = np.full(n, np.nan)
        if n > 1:
            vc1[1:] = np.log((turnover[1:] + 1.0) / (turnover[:-1] + 1.0))

        vc3 = np.full(n, np.nan)
        if n > 5:
            vc3[5:] = np.log((roll_3[5:] + 1.0) / (roll_3[2:n - 3] + 1.0))

        vp = np.full(n, np.nan)
        if n > 19:
            vp[19:] = np.log((roll_3[19:] / 3.0 + 1.0) / (roll_20_mean[19:] + 1.0))

        symbol_frames.append(pl.DataFrame({
            "ts_ms": part["ts_ms"],
            "symbol": pl.Series([symbol] * n, dtype=pl.String),
            "turnover_quote": pl.Series(turnover),
            "log_turnover": pl.Series(log_turnover),
            "volume_change_1d_raw": pl.Series(vc1),
            "volume_change_3d_raw": pl.Series(vc3),
            "volume_persistence_raw": pl.Series(vp),
            "dollar_volume_rank_raw": pl.Series(log_turnover),
        }))
    df = pl.concat(symbol_frames).sort(["ts_ms", "symbol"])
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
    frames = []
    for part in df.partition_by("ts_ms", maintain_order=True):
        values = np.asarray(part[input_col].to_list(), dtype=float)
        finite = np.isfinite(values)
        z = np.full(values.shape, np.nan, dtype=float)
        if finite.sum() >= 3:
            center = float(np.nanmedian(values[finite]))
            mad = float(np.nanmedian(np.abs(values[finite] - center)))
            scale = 1.4826 * mad if mad > 1e-12 else float(np.nanstd(values[finite]))
            if scale > 1e-12:
                z[finite] = np.clip((values[finite] - center) / scale, -3.0, 3.0)
        frames.append(part.with_columns(pl.Series(output_col, z)))
    return pl.concat(frames).sort(["ts_ms", "symbol"]) if frames else df


def _add_liquidity_rank(df: pl.DataFrame) -> pl.DataFrame:
    frames = []
    for part in df.partition_by("ts_ms", maintain_order=True):
        ranked = (
            part.sort("log_turnover", descending=True)
            .with_row_index("liquidity_rank", offset=1)
            .with_columns(
                [
                    pl.lit(part.height).alias("universe_count"),
                    (pl.col("liquidity_rank") / pl.lit(float(part.height))).alias("liquidity_rank_pct"),
                ]
            )
        )
        frames.append(ranked)
    return pl.concat(frames).sort(["ts_ms", "symbol"]) if frames else df


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
