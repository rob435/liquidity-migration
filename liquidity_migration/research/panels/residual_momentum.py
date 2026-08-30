"""Canonical causal residual-momentum owner shared by research and refresh jobs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import MS_PER_DAY
from liquidity_migration.research.panels.daily_feature_panel import _date_str_to_ms

# residual_momentum[D] = sum(residual_return[D-9..D-3]).
RMOM_WINDOW = 7
RMOM_MIN_SAMPLES = 4
RMOM_CAUSAL_SHIFT = 3


def residual_momentum_expr() -> pl.Expr:
    """Return the registered seven-day, shift-three signal expression.

    Row-positional by construction: the caller MUST supply a per-symbol
    contiguous daily grid (``_densify_daily_grid``) so the ten rows the window
    reaches are exactly the ten calendar days ``[D-9..D-3]``.
    """

    return (
        pl.col("residual_return")
        .rolling_sum(window_size=RMOM_WINDOW, min_samples=RMOM_MIN_SAMPLES)
        .shift(RMOM_CAUSAL_SHIFT)
        .over("symbol")
        .alias("residual_momentum")
    )


def _densify_daily_grid(resid: pl.DataFrame) -> pl.DataFrame:
    """Insert null-residual rows for missing per-symbol calendar days.

    ``residual_momentum_expr`` is row-positional: ``rolling_sum(7).shift(3)``
    reaches the 10th *present* row, which spans more than ten calendar days for a
    gapped symbol. Causality holds either way, but the value stops being the
    registered ``sum(residual_return[D-9..D-3])``. Padding restores that window
    exactly; ``RMOM_MIN_SAMPLES`` still tolerates three missing observations
    inside it, so an ordinary short gap produces a value.
    """

    if resid.is_empty():
        return resid
    grid = (
        resid.group_by("symbol")
        .agg(pl.col("ts_ms").min().alias("_first"), pl.col("ts_ms").max().alias("_last"))
        .with_columns(
            pl.int_ranges(pl.col("_first"), pl.col("_last") + MS_PER_DAY, MS_PER_DAY).alias("ts_ms")
        )
        .explode("ts_ms", empty_as_null=True)
        .select("symbol", "ts_ms")
    )
    return (
        grid.join(resid, on=["symbol", "ts_ms"], how="left")
        .select("symbol", "ts_ms", "residual_return")
        .sort(["symbol", "ts_ms"])
    )


def _append_trailing_pad(resid: pl.DataFrame, *, end: str) -> pl.DataFrame:
    if resid.is_empty():
        return resid
    end_day = (_date_str_to_ms(end) // MS_PER_DAY) * MS_PER_DAY
    # A shifted rolling window still emits causal values after a symbol's final
    # real residual. Pad only until fewer than RMOM_MIN_SAMPLES real residuals
    # can remain in the window, so history does not depend on the rebuild date.
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
        .explode("ts_ms", empty_as_null=True)
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
    resid = _densify_daily_grid(resid)
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


def inspect_gate(
    path: Path,
    *,
    max_stable_age_days: float = 2.0,
    min_current_stable_symbols: int = 20,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if max_stable_age_days <= 0.0:
        raise ValueError("max_stable_age_days must be positive")
    if min_current_stable_symbols <= 0:
        raise ValueError("min_current_stable_symbols must be positive")
    if not path.exists():
        raise RuntimeError(f"missing parquet: {path}")
    table = pl.read_parquet(path)
    required = {"symbol", "ts_ms", "residual_momentum", "is_provisional"}
    missing = sorted(required - set(table.columns))
    if missing:
        raise RuntimeError(
            "missing provenance columns " + ",".join(missing)
            + "; rerun the residual-momentum refresh with the current code"
        )
    if table.schema["is_provisional"] != pl.Boolean:
        raise RuntimeError("is_provisional must be a boolean column")
    if table.schema["symbol"] != pl.String:
        raise RuntimeError("symbol must be a String column")
    if table.schema["ts_ms"] != pl.Int64:
        raise RuntimeError("ts_ms must be an Int64 column")
    if table.schema["residual_momentum"] not in (pl.Float32, pl.Float64):
        raise RuntimeError("residual_momentum must be a floating-point column")
    if table["is_provisional"].null_count() > 0:
        raise RuntimeError("is_provisional contains null provenance")
    invalid_keys = table.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("ts_ms").is_null()
        | ((pl.col("ts_ms") % MS_PER_DAY) != 0)
    )
    if not invalid_keys.is_empty():
        raise RuntimeError("symbol/ts_ms contains null, blank, or non-daily keys")
    duplicate_keys = table.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicate_keys.is_empty():
        raise RuntimeError(
            "duplicate (symbol,ts_ms) keys: "
            + repr(duplicate_keys.head(5).select("symbol", "ts_ms", "len").to_dicts())
        )
    invalid_values = table.filter(
        pl.col("residual_momentum").is_null()
        | (~pl.col("residual_momentum").is_finite())
    )
    if not invalid_values.is_empty():
        raise RuntimeError("residual_momentum contains null or non-finite values")
    stable = table.filter(
        (~pl.col("is_provisional"))
        & pl.col("residual_momentum").is_not_null()
        & pl.col("ts_ms").is_not_null()
    )
    if stable.is_empty():
        raise RuntimeError("no stable non-null rows")
    latest_stable_max = stable["ts_ms"].max()
    assert isinstance(latest_stable_max, int)  # Int64 column, non-empty frame
    latest_stable_ts_ms = latest_stable_max
    clock_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    latest_stable_day_ms = (latest_stable_ts_ms // MS_PER_DAY) * MS_PER_DAY
    clock_day_ms = (clock_ms // MS_PER_DAY) * MS_PER_DAY
    if latest_stable_day_ms > clock_day_ms + MS_PER_DAY:
        raise RuntimeError("latest stable day is more than one day in the future")
    stable_age_days = max(clock_ms - latest_stable_ts_ms, 0) / MS_PER_DAY
    if stable_age_days > max_stable_age_days:
        raise RuntimeError(
            f"stable edge is stale: age={stable_age_days:.2f}d "
            f"> max={max_stable_age_days:.2f}d"
        )
    current_stable = stable.filter(pl.col("ts_ms") == clock_day_ms)
    current_stable_symbols = int(current_stable["symbol"].n_unique())
    if current_stable_symbols < min_current_stable_symbols:
        raise RuntimeError(
            f"current-day stable cross-section is too small: symbols={current_stable_symbols} "
            f"< min={min_current_stable_symbols}"
        )
    latest_stable_symbols = int(
        stable.filter(pl.col("ts_ms") == latest_stable_ts_ms)["symbol"].n_unique()
    )
    return {
        "path": str(path),
        "rows": int(table.height),
        "stable_rows": int(stable.height),
        "stable_symbols": int(stable["symbol"].n_unique()),
        "latest_stable_ts_ms": latest_stable_ts_ms,
        "latest_stable_symbols": latest_stable_symbols,
        "current_stable_ts_ms": int(clock_day_ms),
        "current_stable_symbols": current_stable_symbols,
        "stable_age_days": round(stable_age_days, 3),
        "max_stable_age_days": float(max_stable_age_days),
        "min_current_stable_symbols": int(min_current_stable_symbols),
    }
