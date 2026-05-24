from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import polars as pl

from ._common import MS_PER_DAY


RANKER_CLENOW = "clenow_slope_r2"
RANKER_SHARPE = "sharpe_90d"
RANKERS = (RANKER_CLENOW, RANKER_SHARPE)

ANNUALIZATION_SQRT = math.sqrt(365.0)


@dataclass(frozen=True, slots=True)
class MomentumSignalsConfig:
    liquidity_tier_size: int = 30
    liquidity_volume_window_days: int = 90
    min_listing_history_days: int = 180
    ranker: str = RANKER_CLENOW
    ranker_lookback_days: int = 90
    vol_short_window_days: int = 30
    vol_long_window_days: int = 90
    atr_window_days: int = 30
    breakout_window_days: int = 60
    sma_trend_break_days: int = 100
    sma_regime_days: int = 200
    regime_symbol: str = "BTCUSDT"
    vol_floor_annual: float = 0.30
    coil_release_min_compress_days: int = 7
    funding_overheat_window_days: int = 90
    funding_overheat_percentile: float = 0.95
    rank_threshold_quantile: float = 0.75
    rank_exit_quantile: float = 0.50
    vol_shock_multiple: float = 3.0


def daily_bars(klines_1h: pl.DataFrame, *, min_hourly_bars: int = 20) -> pl.DataFrame:
    """Resample 1h klines to daily OHLCV bars.

    `ts_ms` of the output represents the day-end (UTC midnight of the
    following day) — matches the convention used by `volume_features._daily_bars`
    so downstream lookups against the 1h `bar_end_ts_ms` are stable.
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


def add_realized_vol(daily: pl.DataFrame, *, window_days: int, col_name: str | None = None) -> pl.DataFrame:
    """Annualized realized vol of daily log-returns over rolling window."""
    if daily.is_empty():
        return daily
    column = col_name or f"realized_vol_{window_days}d"
    return daily.sort(["symbol", "ts_ms"]).with_columns(
        (
            pl.col("log_return")
            .rolling_std(window_size=window_days, min_samples=window_days)
            .over("symbol")
            * ANNUALIZATION_SQRT
        ).alias(column)
    ).sort(["ts_ms", "symbol"])


def add_sma(daily: pl.DataFrame, *, window_days: int, col_name: str | None = None) -> pl.DataFrame:
    if daily.is_empty():
        return daily
    column = col_name or f"sma_{window_days}d"
    return daily.sort(["symbol", "ts_ms"]).with_columns(
        pl.col("close")
        .rolling_mean(window_size=window_days, min_samples=window_days)
        .over("symbol")
        .alias(column)
    ).sort(["ts_ms", "symbol"])


def add_prior_high(daily: pl.DataFrame, *, window_days: int, col_name: str | None = None) -> pl.DataFrame:
    """Highest daily high in the prior `window_days` (excluding today)."""
    if daily.is_empty():
        return daily
    column = col_name or f"prior_high_{window_days}d"
    return daily.sort(["symbol", "ts_ms"]).with_columns(
        pl.col("high")
        .shift(1)
        .over("symbol")
        .rolling_max(window_size=window_days, min_samples=window_days)
        .over("symbol")
        .alias(column)
    ).sort(["ts_ms", "symbol"])


def add_true_range_and_atr(daily: pl.DataFrame, *, window_days: int) -> pl.DataFrame:
    if daily.is_empty():
        return daily
    atr_col = f"atr_{window_days}d"
    return (
        daily.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.max_horizontal(
                [
                    pl.col("high") - pl.col("low"),
                    (pl.col("high") - pl.col("close").shift(1).over("symbol")).abs(),
                    (pl.col("low") - pl.col("close").shift(1).over("symbol")).abs(),
                ]
            ).alias("true_range")
        )
        .with_columns(
            pl.col("true_range")
            .rolling_mean(window_size=window_days, min_samples=window_days)
            .over("symbol")
            .alias(atr_col)
        )
        .with_columns(
            pl.col("log_return")
            .abs()
            .rolling_median(window_size=window_days, min_samples=window_days)
            .over("symbol")
            .alias(f"abs_return_median_{window_days}d")
        )
        .sort(["ts_ms", "symbol"])
    )


def add_liquidity_tier(daily: pl.DataFrame, *, config: MomentumSignalsConfig) -> pl.DataFrame:
    """Top-N by trailing median turnover. `in_liquidity_tier` is the boolean event."""
    if daily.is_empty():
        return daily
    if "turnover_quote" not in daily.columns:
        raise RuntimeError("turnover_quote is required for liquidity tier computation")
    daily = daily.sort(["symbol", "ts_ms"]).with_columns(
        pl.col("turnover_quote")
        .rolling_median(window_size=config.liquidity_volume_window_days, min_samples=config.liquidity_volume_window_days)
        .over("symbol")
        .alias("turnover_median_90d")
    )
    # Rank within each date (descending). Symbols with null turnover_median get NaN rank,
    # which is handled below by the `is_finite` check.
    daily = daily.with_columns(
        pl.col("turnover_median_90d")
        .rank(method="ordinal", descending=True)
        .over("ts_ms")
        .alias("turnover_rank")
    )
    return daily.with_columns(
        (
            (pl.col("turnover_rank") <= config.liquidity_tier_size)
            & (pl.col("symbol_age_days") >= config.min_listing_history_days)
            & pl.col("turnover_median_90d").is_finite()
        ).alias("in_liquidity_tier")
    ).sort(["ts_ms", "symbol"])


def add_clenow_score(daily: pl.DataFrame, *, lookback_days: int) -> pl.DataFrame:
    """Annualized exp regression slope × R² of log(close) on time, per symbol, rolling."""
    if daily.is_empty():
        return daily.with_columns(pl.Series("clenow_score", [], dtype=pl.Float64))
    frames = []
    for key, part in daily.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        close = np.asarray(part["close"].to_list(), dtype=float)
        scores = _clenow_slope_r2(close, lookback=lookback_days)
        frames.append(part.with_columns(pl.Series("clenow_score", scores)))
    return pl.concat(frames).sort(["ts_ms", "symbol"])


def add_sharpe_score(daily: pl.DataFrame, *, lookback_days: int) -> pl.DataFrame:
    """Trailing Sharpe-like = mean(log_return) / std(log_return), annualized."""
    if daily.is_empty():
        return daily.with_columns(pl.Series("sharpe_score", [], dtype=pl.Float64))
    scored = daily.sort(["symbol", "ts_ms"]).with_columns(
        (
            pl.col("log_return").rolling_mean(window_size=lookback_days, min_samples=lookback_days).over("symbol")
            / pl.col("log_return").rolling_std(window_size=lookback_days, min_samples=lookback_days).over("symbol")
            * ANNUALIZATION_SQRT
        ).alias("sharpe_score")
    )
    return scored.with_columns(
        pl.when(pl.col("sharpe_score").is_finite()).then(pl.col("sharpe_score")).otherwise(None).alias("sharpe_score")
    ).sort(["ts_ms", "symbol"])


def add_cross_sectional_rank(daily: pl.DataFrame, *, config: MomentumSignalsConfig) -> pl.DataFrame:
    """Normalized rank (0 worst, 1 best) of the active ranker within the eligible tier."""
    score_col = "clenow_score" if config.ranker == RANKER_CLENOW else "sharpe_score"
    if daily.is_empty() or score_col not in daily.columns:
        return daily.with_columns(pl.Series("rank_norm", [], dtype=pl.Float64))
    eligible = daily.filter(pl.col("in_liquidity_tier") & pl.col(score_col).is_finite())
    if eligible.is_empty():
        return daily.with_columns(pl.lit(None, dtype=pl.Float64).alias("rank_norm"))
    ranked = eligible.with_columns(
        [
            pl.col(score_col).rank(method="ordinal").over("ts_ms").alias("_score_rank"),
            pl.col(score_col).count().over("ts_ms").cast(pl.Int64).alias("_eligible_count"),
        ]
    ).with_columns(
        pl.when(pl.col("_eligible_count") > 1)
        .then((pl.col("_score_rank") - 1) / (pl.col("_eligible_count") - 1))
        .otherwise(0.5)
        .cast(pl.Float64)
        .alias("rank_norm")
    ).select(["ts_ms", "symbol", "rank_norm"])
    return daily.join(ranked, on=["ts_ms", "symbol"], how="left")


def add_btc_regime(daily: pl.DataFrame, *, config: MomentumSignalsConfig) -> pl.DataFrame:
    """Broadcast BTC SMA-200 regime gate to every (date, symbol) row."""
    if daily.is_empty():
        return daily
    btc = daily.filter(pl.col("symbol") == config.regime_symbol).sort("ts_ms")
    if btc.is_empty():
        return daily.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("regime_close"),
                pl.lit(None, dtype=pl.Float64).alias("regime_sma"),
                pl.lit(False).alias("regime_on"),
            ]
        )
    btc = btc.with_columns(
        pl.col("close")
        .rolling_mean(window_size=config.sma_regime_days, min_samples=config.sma_regime_days)
        .alias("regime_sma")
    ).with_columns(
        pl.col("close").alias("regime_close"),
    ).with_columns(
        (pl.col("close") > pl.col("regime_sma")).alias("regime_on")
    ).select(["ts_ms", "regime_close", "regime_sma", "regime_on"])
    return daily.join(btc, on="ts_ms", how="left").with_columns(
        pl.col("regime_on").fill_null(False)
    )


def add_funding_overheat(
    daily: pl.DataFrame,
    funding: pl.DataFrame | None,
    *,
    config: MomentumSignalsConfig,
) -> pl.DataFrame:
    """Tag (date, symbol) where the latest funding rate exceeds the trailing 95th percentile.

    Falls back to a no-op (overheat=False) when funding is missing, so OOS roots
    without funding data still run — the absence is declared via `funding_mode`
    downstream.
    """
    if daily.is_empty():
        return daily
    if funding is None or funding.is_empty() or "symbol" not in funding.columns or "ts_ms" not in funding.columns:
        return daily.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("funding_rate_recent"),
                pl.lit(None, dtype=pl.Float64).alias("funding_overheat_threshold"),
                pl.lit(False).alias("funding_overheat"),
            ]
        )
    rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in funding.columns else (
        "funding_rate" if "funding_rate" in funding.columns else None
    )
    if rate_col is None:
        return daily.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("funding_rate_recent"),
                pl.lit(None, dtype=pl.Float64).alias("funding_overheat_threshold"),
                pl.lit(False).alias("funding_overheat"),
            ]
        )
    fund_daily = (
        funding.select(["ts_ms", "symbol", rate_col])
        .filter(pl.col(rate_col).is_finite())
        .with_columns(
            ((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)) + MS_PER_DAY).alias("ts_ms_day_end"),
        )
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "ts_ms_day_end"], maintain_order=True)
        .agg(pl.col(rate_col).last().alias("funding_rate_recent"))
        .rename({"ts_ms_day_end": "ts_ms"})
        .sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("funding_rate_recent")
            .rolling_quantile(
                quantile=config.funding_overheat_percentile,
                window_size=config.funding_overheat_window_days,
                min_samples=max(config.funding_overheat_window_days // 2, 5),
            )
            .over("symbol")
            .alias("funding_overheat_threshold")
        )
        .with_columns(
            (pl.col("funding_rate_recent") > pl.col("funding_overheat_threshold")).alias("funding_overheat")
        )
        .select(["ts_ms", "symbol", "funding_rate_recent", "funding_overheat_threshold", "funding_overheat"])
    )
    return daily.join(fund_daily, on=["ts_ms", "symbol"], how="left").with_columns(
        pl.col("funding_overheat").fill_null(False)
    )


def add_coil_release(daily: pl.DataFrame, *, config: MomentumSignalsConfig) -> pl.DataFrame:
    """Vol-compression → expansion event detector.

    Fires on day i when:
      - realized_vol_short(i) > realized_vol_long(i)  (today: expanded)
      - realized_vol_short(i-1) <= realized_vol_long(i-1)  (yesterday: not yet)
      - prior ≥ `min_compress_days` consecutive days were compressed
    """
    short_col = f"realized_vol_{config.vol_short_window_days}d"
    long_col = f"realized_vol_{config.vol_long_window_days}d"
    if daily.is_empty() or short_col not in daily.columns or long_col not in daily.columns:
        return daily.with_columns(pl.lit(False).alias("coil_release_event"))
    frames = []
    min_days = max(config.coil_release_min_compress_days, 1)
    for key, part in daily.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        s = np.asarray(part[short_col].to_list(), dtype=float)
        long_vol = np.asarray(part[long_col].to_list(), dtype=float)
        finite = np.isfinite(s) & np.isfinite(long_vol)
        above = finite & (s > long_vol)
        below = finite & (s <= long_vol)
        n = above.size
        coil = np.zeros(n, dtype=bool)
        streak = 0
        for i in range(n):
            if i > 0 and above[i] and (not above[i - 1]) and streak >= min_days:
                coil[i] = True
            if below[i]:
                streak += 1
            else:
                streak = 0
        frames.append(part.with_columns(pl.Series("coil_release_event", coil)))
    return pl.concat(frames).sort(["ts_ms", "symbol"])


def build_momentum_features(
    klines_1h: pl.DataFrame,
    *,
    funding: pl.DataFrame | None = None,
    config: MomentumSignalsConfig | None = None,
) -> pl.DataFrame:
    """End-to-end feature build for the momentum sleeve.

    Returns one row per (date, symbol) with everything the events module needs.
    Caller is expected to have already excluded blacklisted symbols upstream.
    """
    cfg = config or MomentumSignalsConfig()
    daily = daily_bars(klines_1h)
    if daily.is_empty():
        return daily
    daily = add_returns_and_age(daily)
    daily = add_liquidity_tier(daily, config=cfg)
    daily = add_realized_vol(daily, window_days=cfg.vol_short_window_days)
    daily = add_realized_vol(daily, window_days=cfg.vol_long_window_days)
    daily = add_sma(daily, window_days=cfg.sma_trend_break_days)
    daily = add_prior_high(daily, window_days=cfg.breakout_window_days)
    daily = add_true_range_and_atr(daily, window_days=cfg.atr_window_days)
    if cfg.ranker == RANKER_SHARPE:
        daily = add_sharpe_score(daily, lookback_days=cfg.ranker_lookback_days)
    else:
        daily = add_clenow_score(daily, lookback_days=cfg.ranker_lookback_days)
    daily = add_cross_sectional_rank(daily, config=cfg)
    daily = add_btc_regime(daily, config=cfg)
    daily = add_funding_overheat(daily, funding, config=cfg)
    daily = add_coil_release(daily, config=cfg)
    return daily.sort(["ts_ms", "symbol"])


def _clenow_slope_r2(close: np.ndarray, *, lookback: int) -> np.ndarray:
    """Annualized exp(slope * 365) - 1 × R² of log(close) ~ time over a rolling `lookback`."""
    n = close.size
    result = np.full(n, np.nan, dtype=float)
    if n < lookback or lookback < 2:
        return result
    safe_close = np.maximum(close, 1e-12)
    log_close = np.log(safe_close)
    x = np.arange(lookback, dtype=float)
    sum_x = x.sum()
    sum_x2 = float((x * x).sum())
    denom_x = lookback * sum_x2 - sum_x * sum_x
    if denom_x <= 0:
        return result
    for i in range(lookback - 1, n):
        window = log_close[i - lookback + 1 : i + 1]
        if not np.all(np.isfinite(window)):
            continue
        sum_y = float(window.sum())
        sum_xy = float((x * window).sum())
        sum_y2 = float((window * window).sum())
        num = lookback * sum_xy - sum_x * sum_y
        denom_y = lookback * sum_y2 - sum_y * sum_y
        if denom_y <= 0:
            continue
        slope_per_day = num / denom_x
        annualized_slope = math.exp(slope_per_day * 365.0) - 1.0
        r2 = (num * num) / (denom_x * denom_y)
        result[i] = annualized_slope * r2
    return result
