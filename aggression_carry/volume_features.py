from __future__ import annotations

import math

import numpy as np
import polars as pl


MS_PER_HOUR = 60 * 60 * 1000
MS_PER_DAY = 24 * MS_PER_HOUR

VOLUME_SCORE_COLUMNS = {
    "volume_change_1d": "volume_change_1d_z",
    "volume_change_3d": "volume_change_3d_z",
    "volume_persistence": "volume_persistence_z",
    "dollar_volume_rank": "dollar_volume_rank_z",
    "volume_composite": "volume_composite",
}


def build_volume_features(klines: pl.DataFrame) -> pl.DataFrame:
    daily_rows = _daily_bars(klines)
    if daily_rows.is_empty():
        return daily_rows

    rows = []
    for key, part in daily_rows.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        turnover = np.asarray(part["turnover_quote"].to_list(), dtype=float)
        log_turnover = np.log(turnover + 1.0)
        roll_3 = _rolling_sum(turnover, 3)
        roll_20_mean = _rolling_mean(turnover, 20)
        for index, row in enumerate(part.to_dicts()):
            volume_change_1d = math.log((turnover[index] + 1.0) / (turnover[index - 1] + 1.0)) if index >= 1 else float("nan")
            volume_change_3d = math.log((roll_3[index] + 1.0) / (roll_3[index - 3] + 1.0)) if index >= 5 else float("nan")
            volume_persistence = math.log((roll_3[index] / 3.0 + 1.0) / (roll_20_mean[index] + 1.0)) if index >= 19 else float("nan")
            rows.append(
                {
                    "ts_ms": int(row["ts_ms"]),
                    "symbol": symbol,
                    "turnover_quote": float(turnover[index]),
                    "log_turnover": float(log_turnover[index]),
                    "volume_change_1d_raw": volume_change_1d,
                    "volume_change_3d_raw": volume_change_3d,
                    "volume_persistence_raw": volume_persistence,
                    "dollar_volume_rank_raw": float(log_turnover[index]),
                }
            )
    df = pl.DataFrame(rows).sort(["ts_ms", "symbol"])
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


def _daily_bars(klines: pl.DataFrame) -> pl.DataFrame:
    return (
        klines.with_columns(
            [
                (pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"),
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
        .filter(pl.col("hourly_bars") >= 20)
        .with_columns((pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"))
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
    for index in range(window - 1, values.size):
        output[index] = float(np.sum(values[index - window + 1 : index + 1]))
    return output


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    output = np.full(values.shape, np.nan, dtype=float)
    for index in range(window - 1, values.size):
        output[index] = float(np.mean(values[index - window + 1 : index + 1]))
    return output
