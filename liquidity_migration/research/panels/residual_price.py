"""Idiosyncratic ("idio") price paths cumulated from causal factor residuals.

``risk_model.fit_factor_returns`` produces ``residual_return`` -- the part of a
symbol's move its factor exposures did not explain. This module cumulates it
into a *path*, so chart-shaped signals (range position, distance from a rolling
extreme, breakout, drawdown) can be computed on the idiosyncratic series instead
of the raw close.

Four properties make the path real:

1. **Log space.** ``daily_feature_panel`` produces *simple* returns, and
   ``cumsum`` on simple returns is wrong in the tails. Residuals go through
   ``log1p`` before cumulation, so ``idio_close`` is the exact compounded path.

2. **Calendar re-indexing, not a positional shift.** ``residual_return[d]`` is
   fit to ``fwd_ret_1d``, defined as
   ``first_bar_close(d+2) / first_bar_close(d+1) - 1``, so the move completes at
   01:00 UTC on ``d+2`` and a consumer reading at 00:00 UTC on day ``D`` may only
   use ``d <= D-3``. Cumulating against ``residual_return``'s own ``ts_ms``
   back-dates every move by two days. The re-index uses an explicit calendar
   offset (``ts_ms + 3d``) rather than ``shift(3)``, so a data gap cannot stretch
   the alignment.

3. **Row existence is point-in-time.** A row exists at day ``t`` iff a real
   residual falls in ``[t - max_stale_days, t]``, decided by days ``<= t`` alone.
   Short holes are materialised flat (``is_filled``); a longer hole emits nothing
   until residuals return, because a path joined across a delist/relist hole is a
   different security. Spanning each symbol's ``[first, last]`` residual day
   contiguously leaks instead: a symbol dark at the read day would get rows for
   the dark period only because it later relisted, so "symbols with a row today"
   selects on future listing status. Values stay causal under that construction,
   so a value-only causality assertion cannot detect it — see
   ``test_row_existence_is_invariant_to_a_later_relist``.

4. **Levels are only meaningful in rolling windows.** The daily cross-sectional
   regression carries an intercept, so residuals are demeaned across that day's
   cross-section: an idio path is priced against the equal-weight universe, and
   it accumulates beta-estimation error without bound. Read ``idio_close``
   through rolling windows (30d high, 60d range position), never as distance from
   an all-time idio high.

``idio_zpath`` is the same construction on residuals divided by their trailing
residual volatility: a cumulative-standardised path in standard deviations, not a
price, hence not exponentiated. Use it when a chart pattern must mean the same
thing on a 20-vol and a 200-vol symbol.

Research-only; no operational path imports this.
"""

from __future__ import annotations

import polars as pl

from liquidity_migration.core._common import MS_PER_DAY, calendar_roll
from liquidity_migration.research.panels.daily_feature_panel import _date_str_to_ms

# residual_return[d] describes the move completing at 01:00 UTC on day d+2 ...
RESIDUAL_REALIZATION_LAG_DAYS = 2
# ... so the first 00:00-UTC decision boundary that may read it is d+3, the same
# lag ``residual_momentum.RMOM_CAUSAL_SHIFT`` encodes; a test pins them together.
RESIDUAL_AVAILABILITY_SHIFT_DAYS = RESIDUAL_REALIZATION_LAG_DAYS + 1

DEFAULT_VOL_WINDOW = 30
DEFAULT_VOL_MIN_SAMPLES = 15
DEFAULT_COVERAGE_WINDOW = 30
# How long a chart may persist on imputed zeros after its last real residual.
# Absorbs ordinary archive holes and the per-day drops in ``fit_factor_returns``
# without letting a dark symbol keep a phantom row set alive.
DEFAULT_MAX_STALE_DAYS = 5

# Below this trailing residual stdev the normalised residual is not identified;
# emit null rather than an exploded z-score.
_MIN_RESIDUAL_VOL = 1e-8

_OUTPUT_SCHEMA = {
    "symbol": pl.String,
    "ts_ms": pl.Int64,
    "idio_logret": pl.Float64,
    "idio_close": pl.Float64,
    "idio_logret_vn": pl.Float64,
    "idio_zpath": pl.Float64,
    "is_filled": pl.Boolean,
    "coverage": pl.Float64,
}


def add_log_forward_return(
    panel: pl.DataFrame,
    *,
    source: str = "fwd_ret_1d",
    out: str = "fwd_logret_1d",
) -> pl.DataFrame:
    """Attach the exact log forward return ``log(1 + fwd_ret_Nd)``.

    Pass ``out`` as ``fit_factor_returns(..., target_col=...)`` to obtain
    log-return residuals directly, which removes the ``log1p`` conversion in
    :func:`build_idio_price` (call it with ``residual_scale="log"``). This is
    exact, not an approximation: ``fwd_ret_Nd`` is a simple return over the same
    two closes, so ``log1p`` of it is that interval's log return.

    A forward return of ``-100%`` or worse has no finite log and becomes null
    rather than ``-inf``.
    """

    if source not in panel.columns:
        raise ValueError(f"panel is missing the forward-return column {source!r}")
    return panel.with_columns(
        pl.when(pl.col(source) > -1.0)
        .then(pl.col(source).log1p())
        .otherwise(None)
        .alias(out)
    )


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=_OUTPUT_SCHEMA)


def _validate(residuals: pl.DataFrame) -> pl.DataFrame:
    required = {"symbol", "ts_ms", "residual_return"}
    missing = sorted(required - set(residuals.columns))
    if missing:
        raise ValueError(f"idio-price input missing columns: {missing}")
    frame = residuals.select(
        pl.col("symbol").cast(pl.String, strict=True),
        pl.col("ts_ms").cast(pl.Int64, strict=True),
        pl.col("residual_return").cast(pl.Float64, strict=True),
    )
    if frame.is_empty():
        return frame
    off_grid = frame.filter(
        pl.col("ts_ms").is_null() | ((pl.col("ts_ms") % MS_PER_DAY) != 0)
    )
    if not off_grid.is_empty():
        raise ValueError(
            "residual ts_ms must be on the 00:00-UTC daily grid; the calendar "
            "re-index and the dense grid both assume daily keys"
        )
    # A duplicate (symbol, day) would be summed twice into the path silently.
    duplicates = frame.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ValueError(
            "duplicate (symbol,ts_ms) residuals: "
            + repr(duplicates.head(5).select("symbol", "ts_ms", "len").to_dicts())
        )
    return frame


def _to_log_residual(frame: pl.DataFrame, *, residual_scale: str) -> pl.DataFrame:
    if residual_scale == "log":
        expr = pl.col("residual_return")
    elif residual_scale == "simple":
        # log1p(-1) is -inf and log1p(< -1) is NaN; neither belongs in a path.
        expr = (
            pl.when(pl.col("residual_return") > -1.0)
            .then(pl.col("residual_return").log1p())
            .otherwise(None)
        )
    else:
        raise ValueError(f"residual_scale must be 'simple' or 'log', got {residual_scale!r}")
    return frame.with_columns(
        pl.when(expr.is_finite()).then(expr).otherwise(None).alias("idio_logret")
    )


def _readable_grid(frame: pl.DataFrame, *, end_ms: int, max_stale_days: int) -> pl.DataFrame:
    """Days on which a chart row may exist, decided by days ``<= t`` alone.

    Each real residual day ``d`` licenses rows on ``[d, d + max_stale_days]``,
    and the union of those spans is the grid. Existence at ``t`` therefore
    requires a residual in ``[t - max_stale_days, t]`` -- a strictly backward
    condition, which is what makes the emitted row set point-in-time.

    Taking each symbol's ``[min, max]`` span instead would materialise interior
    holes in full, so a symbol dark at the read day would gain rows purely
    because it relisted afterwards. The trailing edge is bounded so stable
    history does not depend on the rebuild date.
    """

    return (
        frame.select(
            pl.col("symbol"),
            pl.int_ranges(
                pl.col("ts_ms"),
                pl.col("ts_ms") + (max_stale_days + 1) * MS_PER_DAY,
                MS_PER_DAY,
            ).alias("ts_ms"),
        )
        .explode("ts_ms")
        .filter(pl.col("ts_ms") < end_ms)
        .unique(subset=["symbol", "ts_ms"])
    )


def build_idio_price(
    residuals: pl.DataFrame,
    *,
    end: str,
    residual_scale: str = "simple",
    vol_window: int = DEFAULT_VOL_WINDOW,
    vol_min_samples: int = DEFAULT_VOL_MIN_SAMPLES,
    coverage_window: int = DEFAULT_COVERAGE_WINDOW,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> pl.DataFrame:
    """Cumulate factor residuals into per-symbol idiosyncratic price paths.

    ``residuals`` is the ``(symbol, ts_ms, residual_return)`` frame returned by
    ``risk_model.fit_factor_returns``. ``residual_scale`` declares whether those
    residuals were fit to a simple return (the ``fwd_ret_1d`` default) or to a
    log return (see :func:`add_log_forward_return`).

    Returns one row per (symbol, readable decision day) with:

    ``ts_ms``
        The decision day at which the row may be read, on the same 00:00-UTC
        convention as the ``residual_momentum`` table. It is the source
        residual's day plus :data:`RESIDUAL_AVAILABILITY_SHIFT_DAYS`.
    ``idio_logret``
        That day's idiosyncratic log return; null when no residual existed.
    ``idio_close``
        ``exp(cumsum(idio_logret))`` with missing days contributing zero, so
        ``idio_close[t] / idio_close[t-1]`` is exactly ``exp(idio_logret[t])``.
        The base is arbitrary -- read it through rolling windows only.
    ``idio_logret_vn`` / ``idio_zpath``
        The same series divided by its trailing residual volatility (strictly
        prior ``vol_window`` calendar days), and that series' cumulative sum.
        Units of standard deviations, not price; cross-symbol comparable.
    ``is_filled``
        True when the day had no residual and contributed an imputed zero.
    ``coverage``
        Real residual days in the trailing ``coverage_window`` days, divided by
        ``coverage_window``. Filter chart signals on this: a rolling extreme over
        a half-imputed window is not a chart feature, and a symbol younger than
        the window is at its own extreme by construction. A three-day-old chart
        scores ``3/30``, not ``1.0``.

    ``max_stale_days`` bounds how long a chart survives without a new residual;
    beyond it the symbol emits no rows until residuals return. ``end`` is
    exclusive, matching the repository's date-window convention.
    """

    if vol_window <= 0 or coverage_window <= 0:
        raise ValueError("vol_window and coverage_window must be positive")
    if vol_min_samples <= 1:
        raise ValueError("vol_min_samples must exceed 1 for a stdev to be defined")
    if vol_min_samples > vol_window:
        raise ValueError(
            f"vol_min_samples={vol_min_samples} exceeds vol_window={vol_window}: "
            "the gate is unsatisfiable and idio_zpath would be identically zero"
        )
    if max_stale_days < 0:
        raise ValueError("max_stale_days must be non-negative")

    frame = _validate(residuals)
    if frame.is_empty():
        return _empty()

    end_ms = _date_str_to_ms(end)
    frame = _to_log_residual(frame, residual_scale=residual_scale)
    frame = (
        frame.drop_nulls("idio_logret")
        # Explicit calendar offset; a positional shift would be stretched by a gap.
        .with_columns(
            (pl.col("ts_ms") + RESIDUAL_AVAILABILITY_SHIFT_DAYS * MS_PER_DAY).alias("ts_ms")
        )
        .filter(pl.col("ts_ms") < end_ms)
        .select("symbol", "ts_ms", "idio_logret")
    )
    if frame.is_empty():
        return _empty()

    # Volatility is estimated on the SPARSE frame, before densification: polars
    # ``rolling_*_by`` resolves ``min_samples`` against the rows in the window,
    # not the non-null values, so on a dense grid a two-observation stdev after
    # a hole would pass the gate and emit 40-sigma z-scores.
    sparse = frame.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(
            pl.col("idio_logret"), "std", vol_window,
            shifted=True, min_samples=vol_min_samples,
        ).over("symbol").alias("_resid_vol")
    )
    grid = _readable_grid(frame, end_ms=end_ms, max_stale_days=max_stale_days)
    dense = (
        grid.join(sparse, on=["symbol", "ts_ms"], how="left")
        .sort(["symbol", "ts_ms"])
        .with_columns(pl.col("idio_logret").is_null().alias("is_filled"))
    )

    # Coverage divides by the window LENGTH, not by the rows present in it;
    # dividing by present rows reports 1.0 for a three-day-old chart.
    dense = dense.with_columns(
        (
            calendar_roll(
                (~pl.col("is_filled")).cast(pl.Float64), "sum", coverage_window,
                shifted=False, min_samples=1,
            ).over("symbol")
            / float(coverage_window)
        ).alias("coverage")
    )
    dense = dense.with_columns(
        pl.when(pl.col("_resid_vol") > _MIN_RESIDUAL_VOL)
        .then(pl.col("idio_logret") / pl.col("_resid_vol"))
        .otherwise(None)
        .alias("idio_logret_vn")
    )
    return (
        dense.with_columns(
            pl.col("idio_logret").fill_null(0.0).cum_sum().over("symbol").exp().alias("idio_close"),
            pl.col("idio_logret_vn").fill_null(0.0).cum_sum().over("symbol").alias("idio_zpath"),
        )
        .select(list(_OUTPUT_SCHEMA))
        .sort(["symbol", "ts_ms"])
    )
