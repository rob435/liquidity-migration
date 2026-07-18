"""Canonical causal residual-momentum owner shared by research and refresh jobs."""

from __future__ import annotations

import polars as pl

from ._common import MS_PER_DAY
from .daily_feature_panel import _date_str_to_ms

# residual_momentum[D] = sum(residual_return[D-9..D-3]).
RMOM_WINDOW = 7
RMOM_MIN_SAMPLES = 4
RMOM_CAUSAL_SHIFT = 3


def residual_momentum_expr() -> pl.Expr:
    """Return the registered seven-observation, shift-three signal expression."""

    return (
        pl.col("residual_return")
        .rolling_sum(window_size=RMOM_WINDOW, min_samples=RMOM_MIN_SAMPLES)
        .shift(RMOM_CAUSAL_SHIFT)
        .over("symbol")
        .alias("residual_momentum")
    )


def _append_trailing_pad(resid: pl.DataFrame, *, end: str) -> pl.DataFrame:
    if resid.is_empty():
        return resid
    end_day = (_date_str_to_ms(end) // MS_PER_DAY) * MS_PER_DAY
    # A shifted rolling window can still emit causal values after a symbol's
    # final real residual. Pad only until fewer than RMOM_MIN_SAMPLES real
    # residuals can remain in the window. This makes stable history independent
    # of the date on which the table is rebuilt.
    trailing_days = RMOM_CAUSAL_SHIFT + RMOM_WINDOW - RMOM_MIN_SAMPLES
    pad = (
        resid.group_by("symbol")
        .agg(pl.col("ts_ms").max().alias("_last"))
        .filter(pl.col("_last") < end_day)
        .with_columns(
            pl.min_horizontal(
                pl.col("_last") + trailing_days * MS_PER_DAY,
                pl.lit(end_day, dtype=pl.Int64),
            ).alias("_pad_end")
        )
        .with_columns(
            pl.int_ranges(
                pl.col("_last") + MS_PER_DAY,
                pl.col("_pad_end") + MS_PER_DAY,
                MS_PER_DAY,
            ).alias("ts_ms")
        )
        .explode("ts_ms")
        .filter(pl.col("ts_ms").is_not_null())
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("residual_return"))
        .select("symbol", "ts_ms", "residual_return")
    )
    if pad.is_empty():
        return resid
    return pl.concat([resid, pad], how="vertical")


def residual_momentum_from_residuals(
    resid: pl.DataFrame,
    *,
    end: str,
) -> pl.DataFrame:
    """Apply the canonical shift-three window and provisional-state owner."""

    required = {"symbol", "ts_ms", "residual_return"}
    missing = sorted(required - set(resid.columns))
    if missing:
        raise ValueError(f"residual owner input missing columns: {missing}")
    resid = resid.select(
        pl.col("symbol").cast(pl.String, strict=True),
        pl.col("ts_ms").cast(pl.Int64, strict=True),
        pl.col("residual_return").cast(pl.Float64, strict=True),
    ).sort(["symbol", "ts_ms"])
    if resid.is_empty():
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts_ms": pl.Int64,
                "residual_momentum": pl.Float64,
                "is_provisional": pl.Boolean,
            }
        )
    last_real = (
        resid.filter(pl.col("residual_return").is_not_null())
        .group_by("symbol")
        .agg(pl.col("ts_ms").max().alias("_last_real_ts_ms"))
    )
    resid = _append_trailing_pad(resid, end=end)
    return (
        resid.sort(["symbol", "ts_ms"])
        .join(last_real, on="symbol", how="left")
        .with_columns(residual_momentum_expr())
        .with_columns(
            (
                (pl.col("ts_ms") - RMOM_CAUSAL_SHIFT * MS_PER_DAY)
                > pl.col("_last_real_ts_ms")
            )
            .fill_null(True)
            .alias("is_provisional")
        )
        .select("symbol", "ts_ms", "residual_momentum", "is_provisional")
        .drop_nulls("residual_momentum")
    )
