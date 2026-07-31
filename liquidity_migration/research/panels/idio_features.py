"""Close-only chart features, computable identically on any daily price path.

Every feature takes a single price column and nothing else, so the raw and idio
arms share one functional form and a comparison between them cannot smuggle in a
high-versus-close difference. It is also forced: an idio path is cumulated from
daily residual returns, so it has a close and no open/high/low. The two
``daily_feature_panel.FEATURE_REGISTRY`` features that need the intraday range
(``close_location_1d``, ``range_extension_30d``) are absent rather than
approximated, since a close-only "range" is a different feature.

Causality: a feature at row ``ts_ms = D`` reads prices through ``D`` inclusive.
Raw daily closes are known at the EOD close of ``D``, and
``residual_price.build_idio_price`` already stamps ``ts_ms`` as the day the row
is readable (residual day + 3). Windows use ``_common.calendar_roll`` /
``calendar_shift`` so a data gap shrinks a window rather than stretching it
across more calendar days than the feature name claims.

Scale invariance: an idio path's base level is arbitrary, so every feature must
stay a ratio or a within-window position. A level-sensitive addition would make
the idio arm depend on each symbol's listing date.
"""

from __future__ import annotations

import polars as pl

from liquidity_migration.core._common import calendar_roll, calendar_shift

#: Pre-declared feature set. The Bonferroni family is
#: len(CHART_FEATURES) * len(price sources) * len(targets), so widening this
#: list after seeing results requires restating the threshold.
CHART_FEATURES: tuple[str, ...] = (
    "ret_7d",
    "ret_30d",
    "dist_from_30d_high",
    "range_pos_30d",
    "ma_ratio_7_30",
    "vol_of_vol_30d",
)

_MOMENTUM_WINDOWS = (7, 30)
_EXTREME_WINDOW = 30
_EXTREME_MIN_SAMPLES = 15
_MA_FAST, _MA_SLOW = 7, 30
_MA_FAST_MIN, _MA_SLOW_MIN = 4, 15
_VOV_WINDOW = 30
_VOV_MIN_SAMPLES = 15


def _positive(col: str) -> pl.Expr:
    """Guard division: a non-positive price is not a price."""
    return pl.when(pl.col(col) > 0.0).then(pl.col(col)).otherwise(None)


def chart_features(
    prices: pl.DataFrame,
    *,
    price_col: str = "close",
    prefix: str = "",
) -> pl.DataFrame:
    """Compute :data:`CHART_FEATURES` from a ``(symbol, ts_ms, <price_col>)`` frame.

    ``prefix`` namespaces the output columns (e.g. ``"idio_"`` yields
    ``idio_ret_7d``), which lets the raw and idio arms live side by side in one
    panel without a rename step that could transpose them.

    Returns ``(symbol, ts_ms, <prefix>ret_7d, ...)``. Rows whose window is not
    warm yield nulls rather than a partially-warm value.
    """

    required = {"symbol", "ts_ms", price_col}
    missing = sorted(required - set(prices.columns))
    if missing:
        raise ValueError(f"chart_features input missing columns: {missing}")

    df = (
        prices.select(
            pl.col("symbol").cast(pl.String, strict=True),
            pl.col("ts_ms").cast(pl.Int64, strict=True),
            _positive(price_col).cast(pl.Float64, strict=True).alias("_p"),
        )
        # Drop unusable prices here so ``min_samples`` keeps meaning
        # "observations": polars ``rolling_*_by`` counts ROWS in the window and
        # skips nulls in the aggregation, so a null-bearing column satisfies the
        # gate. ``_abs_logret`` stays null-bearing and gets its own count below.
        .drop_nulls("_p")
        .sort(["symbol", "ts_ms"])
    )
    if df.is_empty():
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts_ms": pl.Int64,
                **{f"{prefix}{name}": pl.Float64 for name in CHART_FEATURES},
            }
        )

    # --- momentum: exact N-calendar-day return, null across a gap -----------
    for n in _MOMENTUM_WINDOWS:
        df = df.with_columns(calendar_shift(pl.col("_p"), n).alias(f"_p_lag{n}"))
    df = df.with_columns(
        *[
            pl.when(pl.col(f"_p_lag{n}") > 0.0)
            .then(pl.col("_p") / pl.col(f"_p_lag{n}") - 1.0)
            .otherwise(None)
            .alias(f"{prefix}ret_{n}d")
            for n in _MOMENTUM_WINDOWS
        ]
    )

    # --- rolling extremes: trailing window INCLUDING today, so a fresh high
    #     reads exactly 0.0 and a fresh low reads exactly 1.0 -----------------
    df = df.with_columns(
        calendar_roll(
            pl.col("_p"), "max", _EXTREME_WINDOW,
            shifted=False, min_samples=_EXTREME_MIN_SAMPLES,
        ).over("symbol").alias("_hi"),
        calendar_roll(
            pl.col("_p"), "min", _EXTREME_WINDOW,
            shifted=False, min_samples=_EXTREME_MIN_SAMPLES,
        ).over("symbol").alias("_lo"),
    )
    df = df.with_columns(
        pl.when(pl.col("_hi") > 0.0)
        .then(pl.col("_p") / pl.col("_hi") - 1.0)
        .otherwise(None)
        .alias(f"{prefix}dist_from_30d_high"),
        # Position in the 30d range: 1.0 at the high, 0.0 at the low. A flat
        # window has no position, so it stays null rather than reading 0.5.
        pl.when(pl.col("_hi") > pl.col("_lo"))
        .then((pl.col("_p") - pl.col("_lo")) / (pl.col("_hi") - pl.col("_lo")))
        .otherwise(None)
        .alias(f"{prefix}range_pos_30d"),
    )

    # --- trend: fast/slow moving-average ratio ------------------------------
    df = df.with_columns(
        calendar_roll(
            pl.col("_p"), "mean", _MA_FAST, shifted=False, min_samples=_MA_FAST_MIN,
        ).over("symbol").alias("_ma_fast"),
        calendar_roll(
            pl.col("_p"), "mean", _MA_SLOW, shifted=False, min_samples=_MA_SLOW_MIN,
        ).over("symbol").alias("_ma_slow"),
    )
    df = df.with_columns(
        pl.when(pl.col("_ma_slow") > 0.0)
        .then(pl.col("_ma_fast") / pl.col("_ma_slow") - 1.0)
        .otherwise(None)
        .alias(f"{prefix}ma_ratio_7_30")
    )

    # --- regime: volatility of the absolute daily log return ----------------
    # Computed in log space so the measure is symmetric in up and down moves;
    # an arithmetic return series makes a -50% day look smaller than a +50% one.
    df = df.with_columns(calendar_shift(pl.col("_p"), 1).alias("_p_prev"))
    df = df.with_columns(
        pl.when(pl.col("_p_prev") > 0.0)
        .then((pl.col("_p") / pl.col("_p_prev")).log().abs())
        .otherwise(None)
        .alias("_abs_logret")
    )
    df = df.with_columns(
        calendar_roll(
            pl.col("_abs_logret"), "std", _VOV_WINDOW,
            shifted=False, min_samples=_VOV_MIN_SAMPLES,
        ).over("symbol").alias("_vov"),
        calendar_roll(
            pl.col("_abs_logret").is_not_null().cast(pl.Float64), "sum", _VOV_WINDOW,
            shifted=False, min_samples=1,
        ).over("symbol").alias("_vov_obs"),
    )
    df = df.with_columns(
        pl.when(pl.col("_vov_obs") >= float(_VOV_MIN_SAMPLES))
        .then(pl.col("_vov"))
        .otherwise(None)
        .alias(f"{prefix}vol_of_vol_30d")
    )

    return df.select(
        "symbol", "ts_ms", *[f"{prefix}{name}" for name in CHART_FEATURES]
    )
