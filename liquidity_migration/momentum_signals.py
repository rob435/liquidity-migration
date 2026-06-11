from __future__ import annotations

import polars as pl
from pathlib import Path

from ._common import MS_PER_DAY, calendar_shift


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
                (pl.col("close").log() - calendar_shift(pl.col("close"), 1).log()).alias("log_return"),
                ((pl.col("ts_ms") - pl.col("ts_ms").min().over("symbol")) / MS_PER_DAY + 1).cast(pl.Int64).alias("symbol_age_days"),
            ]
        )
        .sort(["ts_ms", "symbol"])
    )


def _attach_residual_momentum(features: pl.DataFrame, root: Path) -> pl.DataFrame:
    """Left-join the precomputed residual-momentum signal onto the daily feature panel.

    Candidates with no signal row get a null ``residual_momentum`` and are dropped by the gate's
    is_not_null() guard. Raises if the signal file is absent (the gate needs it).

    DAY ALIGNMENT (fixed 2026-06-03 — was a join-key off-by-one): the two sides use DIFFERENT ts_ms
    conventions, so each is mapped to the same TRADING DAY before matching:
      * residual table ts_ms = START-of-day grid (00:00 UTC of decision day D) -> trading day =
        ``ts_ms // MS_PER_DAY`` = day_start(D).
      * this feature panel ts_ms = END-of-day stamp (day_start(D)+MS_PER_DAY = 00:00 of D+1) ->
        trading day = ``(ts_ms - 1) // MS_PER_DAY`` = day_start(D) (the date of ts_ms-1ms; see
        _common.trading_day_expr).
    Flooring BOTH raw (the old bug) mapped the day-D event to D+1 and attached residual_momentum[D+1]
    (= sum residual_return[D-6..D]) instead of [D] — a whole-day shift that pulled residual_return[D]
    (the event day's own forward residual) into the gate. The precompute is independently made causal
    via shift(3) (scripts/precompute_residual_momentum.py). Pinned by
    test_residual_momentum_join_attaches_decision_day_value.

    NOTE: this fix + the precompute shift re-base the rmom gate, so the rmom-gate MAR verdict and the
    live continuous rmom_quantile must be re-validated before deploy.
    """
    sig_path = Path(root) / "residual_momentum.parquet"
    if not sig_path.exists():
        raise RuntimeError(
            f"liquidity_migration_residual_momentum_max is active but {sig_path} is missing; "
            "run scripts/precompute_residual_momentum.py first."
        )
    ms_per_day = 86_400_000
    sig = (
        pl.read_parquet(sig_path)
        .select(
            pl.col("symbol"),
            # residual table ts_ms is the start-of-day grid -> trading day = floor(ts_ms).
            ((pl.col("ts_ms") // ms_per_day) * ms_per_day).alias("_rm_day"),
            pl.col("residual_momentum"),
        )
        .unique(["symbol", "_rm_day"], keep="first")
    )
    return (
        features.with_columns(
            # panel ts_ms is the END-of-day stamp (00:00 of D+1) -> trading day = floor(ts_ms - 1ms).
            (((pl.col("ts_ms") - 1) // ms_per_day) * ms_per_day).alias("_rm_day")
        )
        .join(sig, on=["symbol", "_rm_day"], how="left")
        .drop("_rm_day")
    )
