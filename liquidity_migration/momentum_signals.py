from __future__ import annotations

import polars as pl

from ._common import MS_PER_DAY


# This module is what remains of an earlier momentum-strategy iteration.
# Most of its features (clenow slope/r², sharpe ranker, BTC regime, funding
# overheat, coil release, liquidity tier, realized-vol/SMA/ATR helpers,
# MomentumSignalsConfig) were never wired into the active strategies after
# the liquidity-migration short took over — the dead exports lingered as
# ~330 LOC of carrying cost. Only ``daily_bars`` and ``add_returns_and_age``
# survived as real utilities used by ``long_native.py`` to resample WS
# klines to daily and tag per-symbol age. If a future strategy needs the
# old momentum building blocks, recover them from git history at commit
# ``c425537`` or earlier.


def daily_bars(klines_1h: pl.DataFrame, *, min_hourly_bars: int = 20) -> pl.DataFrame:
    """Resample 1h klines to daily OHLCV bars.

    ``ts_ms`` of the output represents the day-end (UTC midnight of the
    following day) — matches the convention used by
    ``volume_features._daily_bars`` so downstream lookups against the 1h
    ``bar_end_ts_ms`` are stable.
    """
    if klines_1h.is_empty():
        return _empty_daily_bars()
    required = {"ts_ms", "symbol", "open", "high", "low", "close"}
    missing = required - set(klines_1h.columns)
    if missing:
        raise RuntimeError(f"klines_1h missing required columns: {sorted(missing)}")
    has_volume_base = "volume_base" in klines_1h.columns
    has_turnover = "turnover_quote" in klines_1h.columns
    agg = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.len().alias("hourly_bars"),
    ]
    if has_volume_base:
        agg.append(pl.col("volume_base").sum().alias("volume_base"))
    if has_turnover:
        agg.append(pl.col("turnover_quote").sum().alias("turnover_quote"))
    daily = (
        klines_1h.with_columns(
            (pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"),
        )
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg(agg)
        .filter(pl.col("hourly_bars") >= min_hourly_bars)
        .with_columns(
            [
                (pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"),
                pl.from_epoch(pl.col("day_start_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"),
            ]
        )
    )
    select_cols = ["ts_ms", "date", "symbol", "open", "high", "low", "close", "hourly_bars"]
    if has_volume_base:
        select_cols.append("volume_base")
    if has_turnover:
        select_cols.append("turnover_quote")
    return daily.select(select_cols).sort(["ts_ms", "symbol"])


def _empty_daily_bars() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "date": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "open": pl.Series([], dtype=pl.Float64),
            "high": pl.Series([], dtype=pl.Float64),
            "low": pl.Series([], dtype=pl.Float64),
            "close": pl.Series([], dtype=pl.Float64),
            "hourly_bars": pl.Series([], dtype=pl.UInt32),
            "volume_base": pl.Series([], dtype=pl.Float64),
            "turnover_quote": pl.Series([], dtype=pl.Float64),
        }
    )


def add_returns_and_age(daily: pl.DataFrame) -> pl.DataFrame:
    """Add log_return (per-symbol diff of log close) and symbol_age_days."""
    if daily.is_empty():
        return daily
    return (
        daily.sort(["symbol", "ts_ms"])
        .with_columns(
            [
                (pl.col("close").log() - pl.col("close").log().shift(1).over("symbol")).alias("log_return"),
                ((pl.col("ts_ms") - pl.col("ts_ms").min().over("symbol")) / MS_PER_DAY + 1).cast(pl.Int64).alias("symbol_age_days"),
            ]
        )
        .sort(["ts_ms", "symbol"])
    )
