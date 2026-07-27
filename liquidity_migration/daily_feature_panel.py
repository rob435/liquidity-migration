"""Daily PIT feature-panel builders used by residual momentum and risk models.

This module builds a wide (symbol, date, feature_1..feature_k, fwd_ret_Nd)
panel from PIT venue roots. It is deliberately a data-preparation module.

Design contract — non-negotiable:

  1. Causality. Every feature value computed for (symbol, date=D) uses only
     data observable at the END-OF-DAY close on D. Rolling windows therefore
     include D's data; "prior 7d mean" rolls D-7..D-1 (excludes D) via shift(1)
     before the rolling stat.

  2. Executable forward returns. fwd_ret_Nd for decision date D is computed as
         close[first-bar-of-D+1+N] / close[first-bar-of-D+1] - 1
     i.e. the trade is opened 1h after EOD (matching the production
     --entry-delay-hours 1 fill model) and held for exactly N trading days.

  3. Cross-sectional ranks are average-tie fractional ranks (polars
     method="average", normalised to [0, 1]), computed ONLY among same-day
     observations, and signed so that LARGER value == HIGHER rank.

  4. Forward returns are computed once at panel-build time. Downstream
     consumers read the panel's columns directly.

The current live residual-momentum refresh depends on these feature builders via
``risk_model.build_factor_panel``. Do not re-introduce research-selection logic here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import polars as pl

from liquidity_migration._common import MS_PER_DAY, calendar_roll, calendar_shift
from liquidity_migration.storage import read_dataset_columns
from liquidity_migration.volume_events_pit import filter_klines_to_pit_membership
TRADING_DAYS_PER_YEAR = 365  # crypto trades 7 days/week; annualisation is calendar-day-based


# ============================================================================
# Public dataclasses
# ============================================================================


@dataclass(frozen=True)
class FeatureSpec:
    """Declarative spec for one univariate feature.

    ``builder`` consumes the shared :class:`FeatureContext` and returns a
    polars frame with columns ``(symbol, ts_ms, <name>)`` where the value is
    causal at the end-of-day-close of the (symbol, date) row.
    """

    name: str
    builder: "Callable[[FeatureContext], pl.DataFrame]"


@dataclass
class FeatureContext:
    """Pre-aggregated daily inputs shared by all feature builders.

    Constructing the context once (per ``build_feature_panel`` call) and
    passing it to every feature builder avoids re-aggregating the hourly
    klines for each of 20 features.
    """

    daily_klines: pl.DataFrame  # symbol, ts_ms (start-of-day UTC), date, open, high, low, close, volume_base, turnover_quote, first_bar_close
    daily_returns: pl.DataFrame  # symbol, ts_ms, ret_1d (close/close - 1)
    funding_daily: pl.DataFrame  # symbol, ts_ms, funding_rate_1d_sum, funding_rate_last
    open_interest_daily: pl.DataFrame  # symbol, ts_ms, open_interest, open_interest_value
    premium_daily: pl.DataFrame  # symbol, ts_ms, premium_close
    universe_min_daily_turnover: float = 0.0


# ============================================================================
# Data loading + daily aggregators
# ============================================================================


def _read_window(
    data_root: Path | str,
    dataset: str,
    *,
    start_ms: int,
    end_ms: int,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Load ``dataset`` from ``data_root`` and filter to [start_ms, end_ms).

    ``end_ms`` is end-exclusive to match the repo's data_root boundary convention.

    The read is pruned at the DIRECTORY level to ``start_ms``'s date before any
    parquet is opened. ``read_dataset_columns`` grew ``since_date`` precisely for
    this (a full ``**/*.parquet`` walk of a (date, symbol)-partitioned root is
    ~500k files / tens of seconds), but nothing on the panel path passed it, so
    every windowed build -- including the live residual-momentum refresh -- read
    the whole multi-year history of four datasets and then filtered it away
    (2026-07-27 audit M9).
    """
    df = read_dataset_columns(
        data_root,
        dataset,
        columns=columns,
        since_date=_ms_to_date_str(start_ms),
    )
    if df.is_empty() or "ts_ms" not in df.columns:
        return df
    return df.filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))


def _ms_to_date_str(ms: int) -> str:
    """UTC ms epoch -> 'YYYY-MM-DD', the `date=` partition key used for pruning."""

    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _date_str_to_ms(date_str: str) -> int:
    """ISO date string → UTC midnight ms epoch. Tolerates 'YYYY-MM-DD' only."""
    from datetime import datetime, timezone

    return int(
        datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def _aggregate_daily_klines(klines_1h: pl.DataFrame) -> pl.DataFrame:
    """Aggregate hourly OHLCV bars to one bar per (symbol, date).

    Daily open  = open of the first hourly bar of the day (lowest ts_ms)
    Daily close = close of the last hourly bar of the day (highest ts_ms)
    Daily high  = max(high) across the day's hourly bars
    Daily low   = min(low) across the day's hourly bars
    volume_base / turnover_quote = sum across the day

    Also emits ``first_bar_close`` = close of the FIRST bar of the day. This
    is the trade-fill price for entries decided at the previous day's EOD
    (matching the --entry-delay-hours 1 fill model). Forward returns later
    consume this column rather than ``close``.
    """
    if klines_1h.is_empty():
        return klines_1h
    # ``ts_ms`` in klines_1h is the bar START. The bar with the smallest
    # ts_ms on a given date is the bar that OPENS at 00:00 UTC and CLOSES
    # at 01:00 UTC — that close is what an event_demo cell with
    # --entry-delay-hours 1 would have filled at for a signal generated at
    # the previous day's 23:00→24:00 bar close.
    daily = (
        klines_1h.sort(["symbol", "ts_ms"])
        .group_by(["symbol", "date"], maintain_order=True)
        .agg(
            [
                pl.col("ts_ms").min().alias("day_start_ms"),
                pl.col("open").first().alias("open"),
                pl.col("close").last().alias("close"),
                pl.col("close").first().alias("first_bar_close"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("volume_base").sum().alias("volume_base"),
                pl.col("turnover_quote").sum().alias("turnover_quote"),
            ]
        )
        .rename({"day_start_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    return daily


def _aggregate_daily_funding(funding: pl.DataFrame) -> pl.DataFrame:
    """Per-(symbol, date): sum actual settlements and retain the last raw rate.

    Canonical funding rows are venue settlement-history events. Summing an
    ``8h_equiv`` rate derived from a default/static interval corrupts days where
    the venue temporarily switches to hourly or four-hour settlement. Prefer the
    raw per-settlement rate; retain the equivalent-rate fallback only for legacy
    frames that do not carry ``funding_rate`` at all.
    """
    if funding.is_empty():
        return funding
    rate_col = "funding_rate" if "funding_rate" in funding.columns else "funding_rate_8h_equiv"
    if rate_col not in funding.columns:
        return pl.DataFrame()
    if "date" not in funding.columns:
        # funding is hourly-or-better partitioned; derive date from ts_ms
        funding = funding.with_columns(
            (pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d")).alias("date")
        )
    return (
        funding.sort(["symbol", "ts_ms"])
        .group_by(["symbol", "date"], maintain_order=True)
        .agg(
            [
                # Snap to the UTC day floor so a missing 00:00 bar cannot create
                # an off-grid join key.
                ((pl.col("ts_ms").min() // MS_PER_DAY) * MS_PER_DAY).alias("day_start_ms"),
                pl.col(rate_col).sum().alias("funding_rate_1d_sum"),
                pl.col(rate_col).last().alias("funding_rate_last"),
            ]
        )
        .rename({"day_start_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )


def _aggregate_daily_open_interest(open_interest: pl.DataFrame) -> pl.DataFrame:
    """Per-(symbol, date): last OI observation of the day (end-of-day snapshot)."""
    if open_interest.is_empty():
        return open_interest
    has_value = "open_interest_value" in open_interest.columns
    aggs: list[pl.Expr] = [
        # Use the UTC day floor to join the kline grid.
        ((pl.col("ts_ms").min() // MS_PER_DAY) * MS_PER_DAY).alias("day_start_ms"),
        pl.col("open_interest").last().alias("open_interest"),
    ]
    if has_value:
        aggs.append(pl.col("open_interest_value").last().alias("open_interest_value"))
    if "date" not in open_interest.columns:
        open_interest = open_interest.with_columns(
            (pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d")).alias("date")
        )
    return (
        open_interest.sort(["symbol", "ts_ms"])
        .group_by(["symbol", "date"], maintain_order=True)
        .agg(aggs)
        .rename({"day_start_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )


def _aggregate_daily_premium(premium_index_1h: pl.DataFrame) -> pl.DataFrame:
    """Per-(symbol, date): last hourly premium-index close (end-of-day snapshot)."""
    if premium_index_1h.is_empty():
        return premium_index_1h
    if "date" not in premium_index_1h.columns:
        premium_index_1h = premium_index_1h.with_columns(
            (pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d")).alias("date")
        )
    return (
        premium_index_1h.sort(["symbol", "ts_ms"])
        .group_by(["symbol", "date"], maintain_order=True)
        .agg(
            [
                # Use the UTC day floor to join the kline grid on gap-edge days.
                ((pl.col("ts_ms").min() // MS_PER_DAY) * MS_PER_DAY).alias("day_start_ms"),
                pl.col("close").last().alias("premium_close"),
            ]
        )
        .rename({"day_start_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )


def _attach_daily_returns(daily_klines: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol daily simple return = close[D] / close[D-1] - 1.

    Calendar-exact: the prior close is resolved by an explicit ``ts_ms - 1 day``
    join, NOT a positional ``shift(1)``. ``ts_ms`` is the uniform 00:00-UTC daily
    grid, so for a symbol with a missing day (delist->relist, data hole) a
    positional shift would silently turn a multi-day move into a "1d" return.
    A gapped row therefore receives null rather than a misaligned return.
    """
    if daily_klines.is_empty():
        return pl.DataFrame()
    df = daily_klines.sort(["symbol", "ts_ms"])
    prev = df.select(
        pl.col("symbol"),
        (pl.col("ts_ms") + MS_PER_DAY).alias("ts_ms"),  # this row is D-1 for ts_ms+1d
        pl.col("close").alias("_prev_close"),
    )
    return (
        df.join(prev, on=["symbol", "ts_ms"], how="left")
        .with_columns((pl.col("close") / pl.col("_prev_close") - 1.0).alias("ret_1d"))
        .drop("_prev_close")
    )


# ============================================================================
# Cross-sectional helpers
# ============================================================================


def _xs_rank(df: pl.DataFrame, value_col: str, *, out_col: str) -> pl.DataFrame:
    """Cross-sectional average-tie rank fraction in [0, 1] per ts_ms.

    Uses ``rank(method="average")`` normalized by the same-day count, not a
    dense rank.
    Larger value -> higher rank. Nulls stay null; their presence does not
    bias the ranks of the rest (rank is computed over non-null values).
    """
    return df.with_columns(
        pl.col(value_col)
        .rank(method="average", descending=False)
        .over("ts_ms")
        .alias(f"{out_col}_raw_rank"),
    ).with_columns(
        (pl.col(f"{out_col}_raw_rank") / pl.col(value_col).count().over("ts_ms")).alias(out_col)
    ).drop([f"{out_col}_raw_rank"])


def _xs_zscore(df: pl.DataFrame, value_col: str, *, out_col: str) -> pl.DataFrame:
    """Cross-sectional Z-score per ts_ms: (x - mean_xs) / std_xs."""
    mean = pl.col(value_col).mean().over("ts_ms")
    std = pl.col(value_col).std().over("ts_ms")
    return df.with_columns(
        pl.when(std > 0.0).then((pl.col(value_col) - mean) / std).otherwise(None).alias(out_col)
    )


# ============================================================================
# 20 feature builders. Each returns (symbol, ts_ms, <name>).
# ============================================================================


def _select_cols(df: pl.DataFrame, *cols: str) -> pl.DataFrame:
    return df.select([c for c in cols if c in df.columns])


def _make_xs_rank_ret_Nd(n: int):
    """``xs_rank_ret_Nd``: cross-sectional rank of N-day total return.

    N=1 -> intraday mean-rev candidate; N=3,7 -> short-horizon momentum vs
    mean-rev; N=30 -> medium-horizon momentum.
    """

    def builder(ctx: FeatureContext) -> pl.DataFrame:
        if ctx.daily_returns.is_empty():
            return pl.DataFrame()
        # Calendar-exact: resolve the D-n close by an explicit ts_ms - n*MS_PER_DAY
        # match (calendar_shift), NOT a positional shift(n). On a gapped symbol a
        # positional shift reaches the n-th PRESENT row, so an "Nd return" would
        # silently span more than N calendar days — the same gap-blindness
        # _attach_daily_returns / _attach_forward_returns deliberately avoid. A
        # gapped row gets a null ret_Nd (no exact D-n partner) instead of a
        # misaligned horizon. Byte-identical to shift(n) on a contiguous series.
        df = ctx.daily_returns.sort(["symbol", "ts_ms"]).with_columns(
            (pl.col("close") / calendar_shift(pl.col("close"), n) - 1.0).alias("ret_Nd")
        )
        ranked = _xs_rank(df, "ret_Nd", out_col=f"xs_rank_ret_{n}d")
        return _select_cols(ranked, "symbol", "ts_ms", f"xs_rank_ret_{n}d")

    return builder


def _build_liquidity_rank(ctx: FeatureContext) -> pl.DataFrame:
    """``liquidity_rank``: cross-sectional rank by 7d trailing mean turnover.

    Matches the production strategy's liquidity-coordinate. Inclusive of today
    (the rolling 7d mean uses turnover[D-6..D]). Rank is 1 = highest liquidity.
    """
    if ctx.daily_klines.is_empty():
        return pl.DataFrame()
    # Calendar-bounded 7d mean (calendar_roll): a missing day shrinks the window
    # rather than stretching the "7d" mean across >7 calendar days on a gapped
    # symbol. shifted=False => trailing 7d INCLUDING today (turnover[D-6..D]).
    # Numerically identical to rolling_mean(window_size=7, min_samples=1) on a
    # contiguous daily grid.
    df = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("turnover_quote"), "mean", 7, shifted=False, min_samples=1)
        .over("symbol")
        .alias("turnover_7d_mean")
    )
    ranked = df.with_columns(
        pl.col("turnover_7d_mean").rank(method="ordinal", descending=True).over("ts_ms").alias("liquidity_rank")
    )
    return _select_cols(ranked, "symbol", "ts_ms", "liquidity_rank")


def _make_liquidity_rank_delta(n: int):
    """``liquidity_rank_delta_Nd``: continuous version of the event signal.

    Positive value = rank improved (number got smaller) over the last N days.
    Mirrors the existing event predicate ``prior_N_liquidity_rank - liquidity_rank``.
    """

    def builder(ctx: FeatureContext) -> pl.DataFrame:
        lr = _build_liquidity_rank(ctx)
        if lr.is_empty():
            return lr
        # Calendar-exact prior rank (calendar_shift): the D-n rank must be the
        # one EXACTLY n calendar days back, else a gapped symbol's "Nd rank
        # delta" silently compares against a >n-day-old rank. Null across a gap
        # rather than misaligned; identical to shift(n) on contiguous data.
        df = lr.sort(["symbol", "ts_ms"]).with_columns(
            (calendar_shift(pl.col("liquidity_rank"), n) - pl.col("liquidity_rank")).alias(
                f"liquidity_rank_delta_{n}d"
            )
        )
        return _select_cols(df, "symbol", "ts_ms", f"liquidity_rank_delta_{n}d")

    return builder


def _make_turnover_delta(n: int):
    """``turnover_delta_Nd``: today's turnover normalised by the prior N-day mean.

    ``(turnover[D] - mean_{D-N..D-1}) / mean_{D-N..D-1}``. The ``shift(1)``
    keeps today out of the denominator window (causality).
    """

    def builder(ctx: FeatureContext) -> pl.DataFrame:
        if ctx.daily_klines.is_empty():
            return pl.DataFrame()
        # Calendar-bounded prior-N-day mean (calendar_roll, shifted=True =>
        # closed="left", the prior N days EXCLUDING today). A positional
        # shift(1).rolling_mean(N) would, on a gapped symbol, average rows that
        # span >N calendar days and mislabel today's turnover_delta. Identical to
        # the positional version on a contiguous daily grid.
        df = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
            calendar_roll(pl.col("turnover_quote"), "mean", n, shifted=True, min_samples=1)
            .over("symbol")
            .alias("prior_mean")
        )
        df = df.with_columns(
            pl.when(pl.col("prior_mean") > 0.0)
            .then((pl.col("turnover_quote") - pl.col("prior_mean")) / pl.col("prior_mean"))
            .otherwise(None)
            .alias(f"turnover_delta_{n}d")
        )
        return _select_cols(df, "symbol", "ts_ms", f"turnover_delta_{n}d")

    return builder


def _build_funding_rate_z(ctx: FeatureContext) -> pl.DataFrame:
    """``funding_rate_z``: cross-sectional Z-score of today's funding rate.

    Positive Z = longs paying shorts more than the universe-average. Causal:
    the funding row carries the funding for the period ending at or before
    the day's last hour, which is observable at EOD.
    """
    if ctx.funding_daily.is_empty():
        return pl.DataFrame()
    z = _xs_zscore(ctx.funding_daily, "funding_rate_1d_sum", out_col="funding_rate_z")
    return _select_cols(z, "symbol", "ts_ms", "funding_rate_z")


def _build_funding_rate_delta_7d(ctx: FeatureContext) -> pl.DataFrame:
    """``funding_rate_delta_7d``: funding momentum.

    Current-week funding sum minus prior-week funding sum, per symbol. Both
    windows are 7-day non-overlapping; the prior-week sum is shifted by 7
    days to keep the comparison causal.
    """
    if ctx.funding_daily.is_empty():
        return pl.DataFrame()
    # Calendar-bounded 7d funding sum (trailing 7 days incl. today) and a
    # calendar-exact 7-day-prior comparison. A positional rolling_sum(7).shift(7)
    # on a gapped symbol would sum across >7 calendar days and compare against a
    # window that is not exactly 7 days old. Identical to the positional form on
    # a contiguous daily grid.
    df = ctx.funding_daily.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("funding_rate_1d_sum"), "sum", 7, shifted=False, min_samples=1)
        .over("symbol")
        .alias("funding_7d_sum")
    )
    df = df.with_columns(
        (pl.col("funding_7d_sum") - calendar_shift(pl.col("funding_7d_sum"), 7)).alias(
            "funding_rate_delta_7d"
        )
    )
    return _select_cols(df, "symbol", "ts_ms", "funding_rate_delta_7d")


def _build_oi_delta_7d(ctx: FeatureContext) -> pl.DataFrame:
    """``oi_delta_7d``: 7-day OI change normalised by 30d ADV (in USD).

    ``(oi_value[D] - oi_value[D-7]) / mean(turnover[D-29..D])``. The ADV
    denominator includes today (intentional — turnover_quote IS in the
    same EOD snapshot as OI). Positive value = OI rising relative to ADV.
    """
    if ctx.open_interest_daily.is_empty() or ctx.daily_klines.is_empty():
        return pl.DataFrame()
    has_value = "open_interest_value" in ctx.open_interest_daily.columns
    oi_col = "open_interest_value" if has_value else "open_interest"
    # Calendar-exact 7-day-prior OI (calendar_shift): on a gapped symbol a
    # positional shift(7) compares against an OI snapshot >7 calendar days old.
    # Null across the gap rather than mislabel the 7d OI change.
    df = ctx.open_interest_daily.sort(["symbol", "ts_ms"]).with_columns(
        (pl.col(oi_col) - calendar_shift(pl.col(oi_col), 7)).alias("oi_delta_raw")
    )
    # Calendar-bounded 30d ADV (trailing 30 days incl. today). Identical to
    # rolling_mean(window_size=30, min_samples=5) on a contiguous daily grid.
    adv = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("turnover_quote"), "mean", 30, shifted=False, min_samples=5)
        .over("symbol")
        .alias("adv_30d")
    ).select(["symbol", "ts_ms", "adv_30d"])
    joined = df.join(adv, on=["symbol", "ts_ms"], how="inner")
    joined = joined.with_columns(
        pl.when(pl.col("adv_30d") > 0.0)
        .then(pl.col("oi_delta_raw") / pl.col("adv_30d"))
        .otherwise(None)
        .alias("oi_delta_7d")
    )
    return _select_cols(joined, "symbol", "ts_ms", "oi_delta_7d")


def _build_oi_to_adv(ctx: FeatureContext) -> pl.DataFrame:
    """``oi_to_adv``: OI USD / 30d ADV USD. Positioning intensity (turns)."""
    if ctx.open_interest_daily.is_empty() or ctx.daily_klines.is_empty():
        return pl.DataFrame()
    has_value = "open_interest_value" in ctx.open_interest_daily.columns
    oi_col = "open_interest_value" if has_value else "open_interest"
    # Calendar-bounded 30d ADV (trailing 30 days incl. today); see _build_oi_delta_7d.
    adv = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("turnover_quote"), "mean", 30, shifted=False, min_samples=5)
        .over("symbol")
        .alias("adv_30d")
    ).select(["symbol", "ts_ms", "adv_30d"])
    df = ctx.open_interest_daily.select(["symbol", "ts_ms", oi_col]).join(
        adv, on=["symbol", "ts_ms"], how="inner"
    )
    df = df.with_columns(
        pl.when(pl.col("adv_30d") > 0.0)
        .then(pl.col(oi_col) / pl.col("adv_30d"))
        .otherwise(None)
        .alias("oi_to_adv")
    )
    return _select_cols(df, "symbol", "ts_ms", "oi_to_adv")


def _build_premium_index_z(ctx: FeatureContext) -> pl.DataFrame:
    """``premium_index_z``: cross-sectional Z of the EOD premium-index close.

    Positive Z = mark trading above index more than the universe-median.
    """
    if ctx.premium_daily.is_empty():
        return pl.DataFrame()
    z = _xs_zscore(ctx.premium_daily, "premium_close", out_col="premium_index_z")
    return _select_cols(z, "symbol", "ts_ms", "premium_index_z")


def _build_realized_vol_7d(ctx: FeatureContext) -> pl.DataFrame:
    """``realized_vol_7d``: annualised stdev of daily returns over the last 7 days.

    Inclusive of today's return (7-day window ending at D). Annualised by
    sqrt(365) per crypto convention.
    """
    if ctx.daily_returns.is_empty():
        return pl.DataFrame()
    # Calendar-bounded 7d vol (trailing 7 days incl. today). A positional
    # rolling_std(7) on a gapped symbol spans >7 calendar days, so this FROZEN
    # feature (also the inverse-vol sizing denominator) would mislabel its
    # horizon. Identical to rolling_std(window_size=7, min_samples=3) on a
    # contiguous daily grid.
    df = ctx.daily_returns.sort(["symbol", "ts_ms"]).with_columns(
        (
            calendar_roll(pl.col("ret_1d"), "std", 7, shifted=False, min_samples=3)
            .over("symbol")
            * math.sqrt(TRADING_DAYS_PER_YEAR)
        ).alias("realized_vol_7d")
    )
    return _select_cols(df, "symbol", "ts_ms", "realized_vol_7d")


def _build_vol_of_vol_30d(ctx: FeatureContext) -> pl.DataFrame:
    """``vol_of_vol_30d``: std over 30 days of the daily |return| series.

    Captures whether vol is itself volatile (regime-switching) vs steady.
    """
    if ctx.daily_returns.is_empty():
        return pl.DataFrame()
    # Calendar-bounded 30d window over |daily return|. Identical to
    # rolling_std(window_size=30, min_samples=10) on a contiguous daily grid;
    # shrinks (not stretches) across a gap.
    df = ctx.daily_returns.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("ret_1d").abs(), "std", 30, shifted=False, min_samples=10)
        .over("symbol")
        .alias("vol_of_vol_30d")
    )
    return _select_cols(df, "symbol", "ts_ms", "vol_of_vol_30d")


def _build_close_location_1d(ctx: FeatureContext) -> pl.DataFrame:
    """``close_location_1d``: (close - low) / (high - low) for today's session.

    1.0 = closed at the high (strong day); 0.0 = closed at the low. Falls
    back to 0.5 when high == low (degenerate session).
    """
    if ctx.daily_klines.is_empty():
        return pl.DataFrame()
    df = ctx.daily_klines.with_columns(
        pl.when(pl.col("high") > pl.col("low"))
        .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
        .otherwise(0.5)
        .alias("close_location_1d")
    )
    return _select_cols(df, "symbol", "ts_ms", "close_location_1d")


def _build_range_extension_30d(ctx: FeatureContext) -> pl.DataFrame:
    """``range_extension_30d``: today's range divided by the 30d mean range.

    >1 = today's range exceeds the recent norm; <1 = quieter than normal.
    Prior-30d mean excludes today (shift(1)) to keep the denominator causal.
    """
    if ctx.daily_klines.is_empty():
        return pl.DataFrame()
    df = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
        (pl.col("high") - pl.col("low")).alias("range_1d")
    )
    # Calendar-bounded prior-30d mean range (shifted=True => prior 30 days
    # EXCLUDING today). Identical to shift(1).rolling_mean(30, min_samples=5) on
    # a contiguous daily grid; shrinks across a gap rather than stretching.
    df = df.with_columns(
        calendar_roll(pl.col("range_1d"), "mean", 30, shifted=True, min_samples=5)
        .over("symbol")
        .alias("prior_range_mean")
    )
    df = df.with_columns(
        pl.when(pl.col("prior_range_mean") > 0.0)
        .then(pl.col("range_1d") / pl.col("prior_range_mean"))
        .otherwise(None)
        .alias("range_extension_30d")
    )
    return _select_cols(df, "symbol", "ts_ms", "range_extension_30d")


def _build_dist_from_30d_high(ctx: FeatureContext) -> pl.DataFrame:
    """``dist_from_30d_high``: (close - max(high over last 30d)) / max(high) — non-positive.

    0 = printed a 30d high today; -0.5 = 50% below the 30d high. The max
    window includes today (a fresh new high yields value 0).
    """
    if ctx.daily_klines.is_empty():
        return pl.DataFrame()
    # Calendar-bounded 30d max (trailing 30 days incl. today). Identical to
    # rolling_max(window_size=30, min_samples=5) on a contiguous daily grid.
    df = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("high"), "max", 30, shifted=False, min_samples=5)
        .over("symbol")
        .alias("high_30d")
    )
    df = df.with_columns(
        pl.when(pl.col("high_30d") > 0.0)
        .then((pl.col("close") - pl.col("high_30d")) / pl.col("high_30d"))
        .otherwise(None)
        .alias("dist_from_30d_high")
    )
    return _select_cols(df, "symbol", "ts_ms", "dist_from_30d_high")


def _build_dist_from_30d_low(ctx: FeatureContext) -> pl.DataFrame:
    """``dist_from_30d_low``: (close - min(low over last 30d)) / min(low) — non-negative.

    0 = printed a 30d low today; +0.5 = 50% above the 30d low. The min
    window includes today.
    """
    if ctx.daily_klines.is_empty():
        return pl.DataFrame()
    # Calendar-bounded 30d min (trailing 30 days incl. today). Identical to
    # rolling_min(window_size=30, min_samples=5) on a contiguous daily grid.
    df = ctx.daily_klines.sort(["symbol", "ts_ms"]).with_columns(
        calendar_roll(pl.col("low"), "min", 30, shifted=False, min_samples=5)
        .over("symbol")
        .alias("low_30d")
    )
    df = df.with_columns(
        pl.when(pl.col("low_30d") > 0.0)
        .then((pl.col("close") - pl.col("low_30d")) / pl.col("low_30d"))
        .otherwise(None)
        .alias("dist_from_30d_low")
    )
    return _select_cols(df, "symbol", "ts_ms", "dist_from_30d_low")


FEATURE_REGISTRY: dict[str, FeatureSpec] = {
    "xs_rank_ret_1d": FeatureSpec("xs_rank_ret_1d", _make_xs_rank_ret_Nd(1)),
    "xs_rank_ret_3d": FeatureSpec("xs_rank_ret_3d", _make_xs_rank_ret_Nd(3)),
    "xs_rank_ret_7d": FeatureSpec("xs_rank_ret_7d", _make_xs_rank_ret_Nd(7)),
    "xs_rank_ret_30d": FeatureSpec("xs_rank_ret_30d", _make_xs_rank_ret_Nd(30)),
    "liquidity_rank": FeatureSpec("liquidity_rank", _build_liquidity_rank),
    "liquidity_rank_delta_7d": FeatureSpec("liquidity_rank_delta_7d", _make_liquidity_rank_delta(7)),
    "liquidity_rank_delta_30d": FeatureSpec("liquidity_rank_delta_30d", _make_liquidity_rank_delta(30)),
    "turnover_delta_7d": FeatureSpec("turnover_delta_7d", _make_turnover_delta(7)),
    "turnover_delta_30d": FeatureSpec("turnover_delta_30d", _make_turnover_delta(30)),
    "funding_rate_z": FeatureSpec("funding_rate_z", _build_funding_rate_z),
    "funding_rate_delta_7d": FeatureSpec("funding_rate_delta_7d", _build_funding_rate_delta_7d),
    "oi_delta_7d": FeatureSpec("oi_delta_7d", _build_oi_delta_7d),
    "oi_to_adv": FeatureSpec("oi_to_adv", _build_oi_to_adv),
    "premium_index_z": FeatureSpec("premium_index_z", _build_premium_index_z),
    "realized_vol_7d": FeatureSpec("realized_vol_7d", _build_realized_vol_7d),
    "vol_of_vol_30d": FeatureSpec("vol_of_vol_30d", _build_vol_of_vol_30d),
    "close_location_1d": FeatureSpec("close_location_1d", _build_close_location_1d),
    "range_extension_30d": FeatureSpec("range_extension_30d", _build_range_extension_30d),
    "dist_from_30d_high": FeatureSpec("dist_from_30d_high", _build_dist_from_30d_high),
    "dist_from_30d_low": FeatureSpec("dist_from_30d_low", _build_dist_from_30d_low),
}


def resolve_feature_specs(specs: "Iterable[FeatureSpec] | str") -> list[FeatureSpec]:
    """Translate "all" or a list of feature names into a list of FeatureSpec.

    Allows the CLI / build_feature_panel to accept either ``"all"``, a list
    of names, or a list of FeatureSpec instances.
    """
    if specs == "all":
        return list(FEATURE_REGISTRY.values())
    if isinstance(specs, str):
        # comma-separated names ("xs_rank_ret_1d,funding_rate_z")
        names = [s.strip() for s in specs.split(",") if s.strip()]
        out = []
        for name in names:
            if name not in FEATURE_REGISTRY:
                raise KeyError(f"Unknown feature {name!r}. Known: {sorted(FEATURE_REGISTRY)}")
            out.append(FEATURE_REGISTRY[name])
        return out
    resolved: list[FeatureSpec] = []
    for s in specs:
        if isinstance(s, FeatureSpec):
            resolved.append(s)
        elif isinstance(s, str):
            if s not in FEATURE_REGISTRY:
                raise KeyError(f"Unknown feature {s!r}. Known: {sorted(FEATURE_REGISTRY)}")
            resolved.append(FEATURE_REGISTRY[s])
        else:
            raise TypeError(f"feature_specs entries must be FeatureSpec or str, got {type(s).__name__}")
    return resolved


# ============================================================================
# Forward returns
# ============================================================================


def _attach_forward_returns(
    daily_klines: pl.DataFrame,
    horizons: tuple[int, ...],
) -> pl.DataFrame:
    """Per (symbol, decision-date D), compute fwd_ret_Nd from first-bar closes.

    entry  = first-bar close of D+1 (the bar opening 1h after EOD of D and
             closing 1h+1h=2h after EOD; the close at 01:00 UTC of D+1).
    exit_N = first-bar close of D+1+N.
    fwd_ret_Nd = exit_N / entry - 1.

    Returns ``(symbol, ts_ms, fwd_ret_<N>d, ...)`` per horizon. Rows whose
    horizon extends past the data window get null fwd returns.
    """
    if daily_klines.is_empty():
        return pl.DataFrame()
    df = daily_klines.sort(["symbol", "ts_ms"]).select(["symbol", "ts_ms", "first_bar_close"])
    # Resolve by exact calendar offset; gaps yield null instead of a longer horizon.
    lookup = df.select(
        pl.col("symbol"),
        pl.col("ts_ms").alias("_lookup_ts"),
        pl.col("first_bar_close").alias("_lookup_close"),
    )

    def _close_at(offset_days: int, alias: str) -> pl.DataFrame:
        return (
            df.select(
                pl.col("symbol"),
                pl.col("ts_ms"),
                (pl.col("ts_ms") + offset_days * MS_PER_DAY).alias("_lookup_ts"),
            )
            .join(lookup, on=["symbol", "_lookup_ts"], how="left")
            .select(["symbol", "ts_ms", pl.col("_lookup_close").alias(alias)])
        )

    result = _close_at(1, "_entry_close")
    for n in horizons:
        result = result.join(_close_at(1 + n, f"_exit_{n}"), on=["symbol", "ts_ms"], how="left")
    exprs: list[pl.Expr] = [pl.col("symbol"), pl.col("ts_ms")]
    for n in horizons:
        fwd = (
            pl.when(pl.col("_entry_close") > 0.0)
            .then(pl.col(f"_exit_{n}") / pl.col("_entry_close") - 1.0)
            .otherwise(None)
        )
        exprs.append(fwd.alias(f"fwd_ret_{n}d"))
    return result.select(exprs)


# ============================================================================
# Public API — panel build
# ============================================================================


def _autodetect_dataset_names(data_root: Path | str) -> dict[str, str]:
    """Detect which dataset-naming convention applies to ``data_root``.

    The repo holds two flavours of per-venue root:
      * Bybit:   funding/, open_interest/, premium_index_1h/, mark_price_1h/
      * Binance: binance_usdm_funding/, binance_usdm_open_interest/,
                 binance_usdm_premium_index_1h/, binance_usdm_mark_price_1h/

    ``klines_1h`` is the same on both venues. We sniff which subdirs exist
    and return the right mapping so callers don't have to know the
    convention up-front. Phase 5a hit this: dispatch used default Bybit
    names against the Binance root, silently produced 100%-null
    funding_rate_z / oi_delta_7d / premium_index_z, and the resulting
    panel was unusable for Phase 5b IC.
    """
    root = Path(str(data_root)).expanduser()
    binance_prefix = "binance_usdm_"
    has_binance = (root / f"{binance_prefix}funding").is_dir() or (
        root / f"{binance_prefix}open_interest"
    ).is_dir()
    if has_binance:
        return {
            "klines_dataset": "klines_1h",
            "funding_dataset": f"{binance_prefix}funding",
            "open_interest_dataset": f"{binance_prefix}open_interest",
            "premium_dataset": f"{binance_prefix}premium_index_1h",
        }
    return {
        "klines_dataset": "klines_1h",
        "funding_dataset": "funding",
        "open_interest_dataset": "open_interest",
        "premium_dataset": "premium_index_1h",
    }


def build_feature_panel_from_daily(
    daily_klines: pl.DataFrame,
    *,
    start: str,
    end: str,
    feature_specs: "Iterable[FeatureSpec] | str" = "all",
    forward_horizons: tuple[int, ...] = (1, 3, 7),
    universe_min_daily_turnover: float = 0.0,
    funding_daily: pl.DataFrame | None = None,
    open_interest_daily: pl.DataFrame | None = None,
    premium_daily: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Build the canonical panel from pre-aggregated PIT-filtered daily inputs.

    This pure owner is used by immutable research reconstruction.
    ``daily_klines`` must already include the caller's causal warm-up and
    forward-return tail. The ordinary data-root builder delegates here after
    reading and aggregating its hourly datasets.
    """

    if daily_klines.is_empty():
        return pl.DataFrame()
    specs = resolve_feature_specs(feature_specs)
    start_ms = _date_str_to_ms(start)
    end_ms = _date_str_to_ms(end)
    ctx = FeatureContext(
        daily_klines=daily_klines,
        daily_returns=_attach_daily_returns(daily_klines),
        funding_daily=funding_daily if funding_daily is not None else pl.DataFrame(),
        open_interest_daily=(
            open_interest_daily if open_interest_daily is not None else pl.DataFrame()
        ),
        premium_daily=premium_daily if premium_daily is not None else pl.DataFrame(),
        universe_min_daily_turnover=universe_min_daily_turnover,
    )

    panel = daily_klines.select(["symbol", "ts_ms", "date", "close", "turnover_quote"])
    for spec in specs:
        feat = spec.builder(ctx)
        if feat.is_empty():
            panel = panel.with_columns(pl.lit(None, dtype=pl.Float64).alias(spec.name))
            continue
        panel = panel.join(feat, on=["symbol", "ts_ms"], how="left")

    fwd = _attach_forward_returns(daily_klines, forward_horizons)
    if not fwd.is_empty():
        panel = panel.join(fwd, on=["symbol", "ts_ms"], how="left")
    if universe_min_daily_turnover > 0.0:
        panel = panel.filter(pl.col("turnover_quote") >= universe_min_daily_turnover)
    panel = panel.filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
    return panel.sort(["ts_ms", "symbol"])


def build_feature_panel(
    data_root: Path | str,
    *,
    start: str,
    end: str,
    feature_specs: "Iterable[FeatureSpec] | str" = "all",
    forward_horizons: tuple[int, ...] = (1, 3, 7),
    universe_min_daily_turnover: float = 0.0,
    klines_dataset: str | None = None,
    funding_dataset: str | None = None,
    open_interest_dataset: str | None = None,
    premium_dataset: str | None = None,
    require_pit_membership: bool = False,
    pit_manifest_dataset: str = "archive_trade_manifest",
) -> pl.DataFrame:
    """Build the (symbol, date, feature_1..k, fwd_ret_*) panel.

    Pads the start by 60 days internally so all rolling-30 features have
    enough warm-up; the returned panel covers exactly [start, end). The
    extended-history-needed flag is the responsibility of the caller —
    a 30d window run with default features will produce ~30 rows of valid
    feature values and the rest will be nulls.

    ``universe_min_daily_turnover``: drop rows where today's turnover is
    below this threshold. 0 (default) keeps every (symbol, date). Use a
    moderate floor (e.g. 1e6 USD) to drop dead pairs from IC computations
    that would otherwise pollute the cross-sectional rank distribution.

    Dataset-name args default to None, which triggers per-venue
    autodetection — Bybit roots resolve to ``funding/open_interest/
    premium_index_1h``, Binance roots resolve to the ``binance_usdm_``-
    prefixed equivalents. Override an arg to force a specific dataset
    name (e.g. when pointing at a side-copy with renamed dirs).

    ``require_pit_membership`` is the decision-grade research mode. It
    semi-joins hourly klines to ``pit_manifest_dataset`` before daily
    aggregation, rolling features, forward-return construction, and every
    cross-sectional rank. The default remains false for operational roots that
    contain a current live universe rather than a historical manifest.
    """
    start_ms = _date_str_to_ms(start)
    end_ms = _date_str_to_ms(end)
    # Pad 60 days backwards so rolling-30 has warm-up. Forward-return columns
    # need (max horizon + 1) days of FUTURE klines beyond ``end``.
    pad_back_ms = 60 * MS_PER_DAY
    pad_forward_ms = (max(forward_horizons) + 2) * MS_PER_DAY
    read_start_ms = start_ms - pad_back_ms
    read_end_ms = end_ms + pad_forward_ms

    autodetected = _autodetect_dataset_names(data_root)
    klines_name = klines_dataset if klines_dataset is not None else autodetected["klines_dataset"]
    funding_name = funding_dataset if funding_dataset is not None else autodetected["funding_dataset"]
    oi_name = open_interest_dataset if open_interest_dataset is not None else autodetected["open_interest_dataset"]
    premium_name = premium_dataset if premium_dataset is not None else autodetected["premium_dataset"]

    klines_1h = _read_window(
        data_root,
        klines_name,
        start_ms=read_start_ms,
        end_ms=read_end_ms,
        columns=["ts_ms", "symbol", "open", "high", "low", "close", "volume_base", "turnover_quote", "date"],
    )
    funding = _read_window(data_root, funding_name, start_ms=read_start_ms, end_ms=read_end_ms)
    open_interest = _read_window(data_root, oi_name, start_ms=read_start_ms, end_ms=read_end_ms)
    premium = _read_window(data_root, premium_name, start_ms=read_start_ms, end_ms=read_end_ms)

    if klines_1h.is_empty():
        return pl.DataFrame()
    if require_pit_membership:
        archive_manifest = read_dataset_columns(
            data_root,
            pit_manifest_dataset,
            columns=["date", "symbol"],
        )
        klines_1h, _pit_filter_receipt = filter_klines_to_pit_membership(
            klines_1h,
            archive_manifest,
        )
        if klines_1h.is_empty():
            return pl.DataFrame()

    daily_klines = _aggregate_daily_klines(klines_1h)
    funding_daily = _aggregate_daily_funding(funding)
    open_interest_daily = _aggregate_daily_open_interest(open_interest)
    premium_daily = _aggregate_daily_premium(premium)
    return build_feature_panel_from_daily(
        daily_klines,
        start=start,
        end=end,
        feature_specs=feature_specs,
        forward_horizons=forward_horizons,
        universe_min_daily_turnover=universe_min_daily_turnover,
        funding_daily=funding_daily,
        open_interest_daily=open_interest_daily,
        premium_daily=premium_daily,
    )
