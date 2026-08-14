from __future__ import annotations

import hashlib
import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.core.config import TradeLifecycleConfig
from liquidity_migration.core._common import MS_PER_HOUR, _iso_date, _iso_month, date_boundary_ms


def summarize_baskets(trades: pl.DataFrame, *, config: TradeLifecycleConfig) -> pl.DataFrame:
    if trades.is_empty():
        return _empty_baskets()
    return (
        trades.group_by("basket_id", maintain_order=True)
        .agg(
            [
                pl.col("entry_signal_ts_ms").min(),
                pl.col("entry_ts_ms").min(),
                pl.col("exit_ts_ms").max(),
                pl.col("net_return").sum().alias("basket_return"),
                pl.col("gross_return").sum().alias("gross_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.col("funding_return").sum().alias("funding_return"),
                pl.when(pl.col("side") == "long").then(pl.col("net_return")).otherwise(0.0).sum().alias("long_return"),
                pl.when(pl.col("side") == "short").then(pl.col("net_return")).otherwise(0.0).sum().alias("short_return"),
                pl.len().alias("trades"),
                (pl.col("net_return") > 0.0).sum().alias("winning_trades"),
            ]
        )
        .with_columns(
            [
                pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("exit_date"),
                pl.lit(config.score).alias("score"),
                pl.lit(config.quantile).alias("quantile"),
                pl.lit(config.hold_days).alias("hold_days"),
                pl.lit(config.rebalance_days).alias("rebalance_days"),
            ]
        )
        .sort("entry_ts_ms")
    )


def build_equity_curve(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame(
            {
                "ts_ms": pl.Series([], dtype=pl.Int64),
                "equity": pl.Series([], dtype=pl.Float64),
                "drawdown": pl.Series([], dtype=pl.Float64),
                "basket_return": pl.Series([], dtype=pl.Float64),
            }
        )
    # Compound on a daily grid. Each basket is a fractional slice
    # (weight ~ 1/max_active), so same-day baskets are additive and equity only
    # compounds across days; a per-basket cum_prod in exit order would multiply
    # overlapping positions together. Realised-PnL accounting: a basket's whole
    # return lands on its exit day.
    return (
        baskets.with_columns(
            pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
        )
        .group_by("date")
        .agg(
            pl.col("basket_return").sum().alias("basket_return"),
            pl.col("exit_ts_ms").max().alias("ts_ms"),
        )
        .sort("ts_ms")
        .with_columns((pl.col("basket_return") + 1.0).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
        .select("ts_ms", "equity", "drawdown", "basket_return", "date")
    )


def annualized_sharpe(daily_returns: "np.ndarray | list[float]", *, ann_days: float = 365.25) -> float:
    """Canonical annualised Sharpe = mean / std(ddof=1) * sqrt(ann_days) over a daily
    return series — the convention shared across the backtest engines.
    Returns 0.0 for fewer than 2 finite points or zero variance. Callers
    pass the daily series (the equity-based sites forward-fill the calendar grid first)."""
    arr = np.asarray(
        [float(x) for x in daily_returns if x is not None and math.isfinite(float(x))], dtype=float
    )
    if arr.size < 2:
        return 0.0
    sd = float(arr.std(ddof=1))
    return float(arr.mean() / sd * math.sqrt(ann_days)) if sd > 1e-12 else 0.0


def _daily_sharpe(equity: pl.DataFrame) -> float:
    """Annualised Sharpe from the daily equity series.

    Honest across firing frequencies — does not assume `365 / rebalance_days`
    periods per year. A strategy that fires 20 trades a year and a strategy
    that fires 200 produce the same Sharpe scale when their daily PnL volatility
    is the same.

    `build_equity_curve` emits one row per exit-date (sparse for low-frequency
    strategies), so we forward-fill onto the calendar-day grid between the
    first and last exit before computing diffs. Otherwise the "daily" diff
    is actually inter-exit and sparse strategies still inflate.
    """
    if equity.is_empty() or "equity" not in equity.columns:
        return 0.0
    # Forward-fill on calendar days; exit timestamps need not be midnight-aligned.
    values = np.asarray(_daily_equity_values(equity), dtype=float)
    if values.size < 2:
        return 0.0
    daily_ret = np.diff(values) / values[:-1]
    # Use the shared daily Sharpe convention across reports.
    return annualized_sharpe(daily_ret)


def summarize_trade_backtest(
    trades: pl.DataFrame,
    baskets: pl.DataFrame,
    equity: pl.DataFrame,
    *,
    config: TradeLifecycleConfig,
) -> dict[str, Any]:
    if trades.is_empty() or baskets.is_empty() or equity.is_empty():
        return {
            "total_return": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
            "trades": 0,
            "baskets": 0,
            "trade_win_rate": 0.0,
            "profit_factor": 0.0,
            "long_return": 0.0,
            "short_return": 0.0,
            "cost_return": 0.0,
            "funding_return": 0.0,
            "funding_mode": "missing",
            "funding_modeled_fraction": 0.0,
            "funding_event_count": 0,
            "worst_basket_return": 0.0,
            "worst_day_return": 0.0,
            "max_underwater_days": 0,
            "worst_30d_return": 0.0,
            "worst_60d_return": 0.0,
            "worst_90d_return": 0.0,
            "worst_120d_return": 0.0,
            "position_weight_mean": 1.0,
            "position_weight_std": 0.0,
            "position_weight_min": 1.0,
            "position_weight_max": 1.0,
            "worst_trade_mae": 0.0,
            "mean_trade_mae": 0.0,
            "worst_weighted_intrahold_loss": 0.0,
            "realized_gross_mean": 0.0,
            "realized_gross_max": 0.0,
        }
    basket_returns = np.asarray(baskets["basket_return"].to_list(), dtype=float)
    mean_return = float(np.mean(basket_returns)) if basket_returns.size else 0.0
    wins = trades.filter(pl.col("net_return") > 0.0)
    losses = trades.filter(pl.col("net_return") < 0.0)
    profit = float(wins["net_return"].sum()) if not wins.is_empty() else 0.0
    loss = float(losses["net_return"].sum()) if not losses.is_empty() else 0.0
    return {
        "total_return": float(equity["equity"][-1] - 1.0),
        "sharpe_like": _daily_sharpe(equity),
        "max_drawdown": float(equity["drawdown"].min()),
        "trades": trades.height,
        "baskets": baskets.height,
        "trade_win_rate": float((trades["net_return"] > 0.0).mean()),
        "profit_factor": float(profit / abs(loss)) if loss < -1e-12 else 0.0,
        "mean_basket_return": mean_return,
        "mean_trade_return": float(trades["net_return"].mean()),
        "long_return": float(trades.filter(pl.col("side") == "long")["net_return"].sum()),
        "short_return": float(trades.filter(pl.col("side") == "short")["net_return"].sum()),
        "gross_return": float(trades["gross_return"].sum()),
        "cost_return": float(trades["cost_return"].sum()),
        "funding_return": float(trades["funding_return"].sum()) if "funding_return" in trades.columns else 0.0,
        "funding_mode": _funding_mode_summary(trades),
        "funding_modeled_fraction": _funding_modeled_fraction(trades),
        "funding_event_count": int(trades["funding_event_count"].sum()) if "funding_event_count" in trades.columns else 0,
        "worst_basket_return": float(basket_returns.min()) if basket_returns.size else 0.0,
        "worst_day_return": _worst_volume_day_return(baskets),
        "max_underwater_days": _max_underwater_days(equity),
        "worst_30d_return": _worst_rolling_equity_return(equity, 30),
        "worst_60d_return": _worst_rolling_equity_return(equity, 60),
        "worst_90d_return": _worst_rolling_equity_return(equity, 90),
        "worst_120d_return": _worst_rolling_equity_return(equity, 120),
        **_position_weight_stats(trades),
        **_intrahold_and_gross_stats(trades),
    }


def _intrahold_and_gross_stats(trades: pl.DataFrame) -> dict[str, float]:
    """Return per-position adverse excursion and realized-gross diagnostics.

    ``worst_weighted_intrahold_loss`` is not portfolio mark-to-market drawdown;
    concurrent positions can make the latter deeper. Realized gross exposes
    sizing differences that can otherwise confound strategy comparisons.
    """
    out = {
        "worst_trade_mae": 0.0,
        "mean_trade_mae": 0.0,
        "worst_weighted_intrahold_loss": 0.0,
        "realized_gross_mean": 0.0,
        "realized_gross_max": 0.0,
    }
    if trades.is_empty():
        return out
    if "mae" in trades.columns:
        # A sleeve that does not track the intra-hold path emits NaN, meaning
        # "not measured" rather than 0; it must not enter min()/mean().
        finite_mae = trades.filter(pl.col("mae").is_finite())
        mae = finite_mae["mae"]
        if not mae.is_empty():
            out["worst_trade_mae"] = float(mae.min())
            out["mean_trade_mae"] = float(mae.mean())
        if "notional_weight" in finite_mae.columns:
            weighted = (
                finite_mae.select((pl.col("mae") * pl.col("notional_weight").abs()).alias("w"))
                .get_column("w")
                .drop_nulls()
            )
            if not weighted.is_empty():
                out["worst_weighted_intrahold_loss"] = float(weighted.min())
    if {"basket_id", "notional_weight"}.issubset(trades.columns):
        per_basket = trades.group_by("basket_id").agg(
            pl.col("notional_weight").abs().sum().alias("gross")
        )
        gross = per_basket["gross"].drop_nulls()
        if not gross.is_empty():
            out["realized_gross_mean"] = float(gross.mean())
            out["realized_gross_max"] = float(gross.max())
    return out


def _position_weight_stats(trades: pl.DataFrame) -> dict[str, float]:
    if "position_weight" not in trades.columns:
        return {"position_weight_mean": 1.0, "position_weight_std": 0.0, "position_weight_min": 1.0, "position_weight_max": 1.0}
    pw = trades["position_weight"].drop_nulls()
    if pw.is_empty():
        return {"position_weight_mean": 1.0, "position_weight_std": 0.0, "position_weight_min": 1.0, "position_weight_max": 1.0}
    return {
        "position_weight_mean": float(pw.mean()),
        "position_weight_std": float(pw.std(ddof=1)) if pw.len() > 1 else 0.0,
        "position_weight_min": float(pw.min()),
        "position_weight_max": float(pw.max()),
    }


def _funding_mode_summary(trades: pl.DataFrame) -> str:
    if trades.is_empty() or "funding_mode" not in trades.columns:
        return "missing"
    modes = set(str(item) for item in trades["funding_mode"].to_list())
    if not modes or modes == {"missing"}:
        return "missing"
    if modes == {"modeled"}:
        return "modeled"
    return "partial"


def _funding_modeled_fraction(trades: pl.DataFrame) -> float:
    """Fraction of traded gross notional whose funding was fully modeled.

    The coarse ``partial`` label cannot show materiality, so weight coverage by
    absolute trade notional. Missing funding contributes zero modeled coverage.

    Returns 1.0 for a fully-modeled book and 0.0 for an empty / all-missing one;
    weight falls back to equal-per-trade when ``notional_weight`` is unavailable.
    """
    if trades.is_empty() or "funding_mode" not in trades.columns:
        return 0.0
    if "notional_weight" in trades.columns:
        weighted = trades.select(
            pl.col("notional_weight").abs().alias("_w"),
            (pl.col("funding_mode") == "modeled").cast(pl.Float64).alias("_modeled"),
        ).drop_nulls("_w")
        total = float(weighted["_w"].sum())
        if total > 0.0:
            return float((weighted["_w"] * weighted["_modeled"]).sum()) / total
    modes = trades["funding_mode"].to_list()
    if not modes:
        return 0.0
    return sum(1 for m in modes if str(m) == "modeled") / len(modes)


def _worst_volume_day_return(baskets: pl.DataFrame) -> float:
    if baskets.is_empty() or "exit_date" not in baskets.columns:
        return 0.0
    # Match the equity curve: sum same-day baskets and compound across days.
    daily = baskets.group_by("exit_date").agg(pl.col("basket_return").sum().alias("day_return"))
    return float(daily["day_return"].min()) if not daily.is_empty() else 0.0


def _daily_equity_values(equity: pl.DataFrame) -> list[float]:
    if equity.is_empty() or "ts_ms" not in equity.columns or "equity" not in equity.columns:
        return []
    daily = (
        equity.sort("ts_ms")
        .with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().alias("_d"))
        .group_by("_d")
        .agg(pl.col("equity").last())
        .sort("_d")
    )
    if daily.is_empty():
        return []
    start_date = daily["_d"].min()
    end_date = daily["_d"].max()
    all_dates = pl.DataFrame({"_d": pl.date_range(start_date, end_date, interval="1d", eager=True)})
    return (
        all_dates.join(daily, on="_d", how="left")
        .with_columns(pl.col("equity").forward_fill())
        ["equity"].to_list()
    )


def _max_underwater_days(equity: pl.DataFrame) -> int:
    values = _daily_equity_values(equity)
    if not values:
        return 0
    peak = values[0]
    peak_index = 0
    max_days = 0
    for index, value in enumerate(values):
        if value >= peak - 1e-12:
            peak = value
            peak_index = index
        else:
            max_days = max(max_days, index - peak_index)
    return max_days


def _worst_rolling_equity_return(equity: pl.DataFrame, days: int) -> float:
    values = np.asarray(_daily_equity_values(equity), dtype=float)
    if days <= 0 or values.size <= days:
        return 0.0
    returns = values[days:] / values[:-days] - 1.0
    return float(returns.min()) if returns.size else 0.0


def _filter_signal_window(features: pl.DataFrame, config: TradeLifecycleConfig) -> pl.DataFrame:
    if features.is_empty():
        return features
    start_ms = _date_boundary_ms(config.start_date)
    end_ms = _date_boundary_ms(config.end_date)
    filtered = features
    if start_ms is not None:
        filtered = filtered.filter(pl.col("ts_ms") >= start_ms)
    if end_ms is not None:
        filtered = filtered.filter(pl.col("ts_ms") < end_ms)
    return filtered


def _date_boundary_ms(value: str) -> int | None:
    return date_boundary_ms(value)


def _rank_exit_hit(
    *,
    symbol: str,
    side: str,
    side_mode: str,
    bar_end_ts_ms: int,
    rank_lookup: dict[tuple[str, int], float],
    enabled: bool,
    threshold: float,
) -> bool:
    if not enabled:
        return False
    rank_fraction = rank_lookup.get((symbol, bar_end_ts_ms))
    if rank_fraction is None:
        return False
    if side_mode == "long_high_short_low":
        if side == "long":
            return rank_fraction < threshold
        return rank_fraction > 1.0 - threshold
    if side == "long":
        return rank_fraction > 1.0 - threshold
    return rank_fraction < threshold


def _funding_lookup(
    funding: pl.DataFrame | None,
) -> dict[str, dict[str, Any]] | None:
    """Index canonical funding-history settlements by exact venue timestamp.

    The Bybit and Binance canonical funding datasets are downloaded from their
    funding-history endpoints. Each distinct ``(symbol, ts_ms)`` row is therefore
    a realized settlement, including temporary venue cadence changes. Inferring
    one modal cadence per symbol and bucketing these rows merges genuine stressed
    hourly/four-hour settlements and undercharges path-dependent carry.

    Identical rows repeated by overlapping downloads are deduplicated. Conflicting
    duplicate rates, non-finite rates, or rows explicitly marked as non-settlement
    semantics fail closed. A ticker/snapshot dataset must be normalized into
    settlement history before it reaches this accounting boundary.
    """
    if funding is None or funding.is_empty() or "symbol" not in funding.columns or "ts_ms" not in funding.columns:
        return None
    rate_col = "funding_rate" if "funding_rate" in funding.columns else "funding_rate_8h_equiv"
    if rate_col not in funding.columns:
        return None

    if "funding_event_kind" in funding.columns:
        invalid_kinds = (
            funding.filter(
                pl.col("funding_event_kind").is_not_null()
                & (pl.col("funding_event_kind") != "settlement")
            )
            .select("funding_event_kind")
            .unique()
        )
        if not invalid_kinds.is_empty():
            kinds = sorted(str(value) for value in invalid_kinds["funding_event_kind"].to_list())
            raise RuntimeError(
                "funding accounting requires settlement-history rows; "
                f"found non-settlement funding_event_kind values: {kinds}"
            )

    rows = (
        funding.select(["symbol", "ts_ms", rate_col])
        .drop_nulls(["symbol", "ts_ms"])
        .with_columns(pl.col(rate_col).cast(pl.Float64, strict=False))
    )
    invalid_rates = rows.filter(pl.col(rate_col).is_null() | ~pl.col(rate_col).is_finite())
    if not invalid_rates.is_empty():
        sample = invalid_rates.select("symbol", "ts_ms").head(3).to_dicts()
        raise RuntimeError(
            "funding settlement history contains missing or non-finite rates; "
            f"rows={invalid_rates.height} sample={sample}"
        )

    conflicts = (
        rows.group_by(["symbol", "ts_ms"])
        .agg(pl.col(rate_col).n_unique().alias("_rate_count"))
        .filter(pl.col("_rate_count") > 1)
        .sort(["symbol", "ts_ms"])
    )
    if not conflicts.is_empty():
        raise RuntimeError(
            "funding settlement history has conflicting duplicate rates for the same "
            f"(symbol, ts_ms); rows={conflicts.height} sample={conflicts.head(3).to_dicts()}"
        )

    rows = rows.unique(["symbol", "ts_ms"], keep="first").sort(["symbol", "ts_ms"])
    # Raw first/last stamp per symbol — used for the coverage ("partial") check.
    raw_span = {
        str(row["symbol"]): (int(row["start_ts_ms"]), int(row["end_ts_ms"]))
        for row in rows.group_by("symbol")
        .agg(pl.col("ts_ms").min().alias("start_ts_ms"), pl.col("ts_ms").max().alias("end_ts_ms"))
        .to_dicts()
    }
    output: dict[str, dict[str, Any]] = {}
    for key, part in rows.partition_by("symbol", as_dict=True, maintain_order=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        # Parallel sorted lists permit O(log n) window lookup via bisect.
        ts_list = [int(x) for x in part["ts_ms"].to_list()]
        rate_list = [float(x) for x in part[rate_col].to_list()]
        if ts_list:
            start, end = raw_span.get(symbol, (ts_list[0], ts_list[-1]))
            output[symbol] = {
                "events_ts": ts_list,
                "events_rate": rate_list,
                "start_ts_ms": start,
                "end_ts_ms": end,
            }
    return output


def _perp_funding_return(
    funding_lookup: dict[str, dict[str, Any]] | None,
    *,
    symbol: str,
    side: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> tuple[float, str, int]:
    if funding_lookup is None:
        return 0.0, "missing", 0
    series = funding_lookup.get(symbol)
    if series is None:
        return 0.0, "missing", 0
    # A trade extending past the funding dataset is still charged the funding
    # that is covered and flagged "partial"; zeroing it would drop a real cost.
    fully_covered = entry_ts_ms >= int(series["start_ts_ms"]) and exit_ts_ms <= int(series["end_ts_ms"])
    mode = "modeled" if fully_covered else "partial"
    # Bisect the pre-sorted ts_list to slice the in-window events in O(log n).
    ts_list = series["events_ts"]
    lo = bisect_right(ts_list, entry_ts_ms)
    hi = bisect_right(ts_list, exit_ts_ms)
    if lo >= hi:
        return 0.0, mode, 0
    signed = sum(series["events_rate"][lo:hi])
    return (float(-signed) if side == "long" else float(signed)), mode, hi - lo


def _price_bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, np.ndarray]]:
    # Parallel numpy arrays per symbol (ts_ms / bar_end_ts_ms / OHLC), so hot
    # loops index by position instead of building ~12M per-bar dicts.
    required = {"ts_ms", "symbol", "open", "high", "low", "close"}
    missing = required - set(klines.columns)
    if missing:
        raise RuntimeError(f"klines_1h is missing required columns: {sorted(missing)}")
    output: dict[str, dict[str, np.ndarray]] = {}
    prepared = klines.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
    for key, part in prepared.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = {
            "ts_ms": part["ts_ms"].to_numpy().astype(np.int64, copy=False),
            "bar_end_ts_ms": part["bar_end_ts_ms"].to_numpy().astype(np.int64, copy=False),
            "open": part["open"].to_numpy().astype(np.float64, copy=False),
            "high": part["high"].to_numpy().astype(np.float64, copy=False),
            "low": part["low"].to_numpy().astype(np.float64, copy=False),
            "close": part["close"].to_numpy().astype(np.float64, copy=False),
        }
    return output


def _bar_exit_hits(
    *,
    side: str,
    high: float,
    low: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> tuple[bool, bool]:
    if side == "long":
        stop_hit = stop_price is not None and low <= stop_price
        take_profit_hit = take_profit_price is not None and high >= take_profit_price
    else:
        stop_hit = stop_price is not None and high >= stop_price
        take_profit_hit = take_profit_price is not None and low <= take_profit_price
    return stop_hit, bool(take_profit_hit)


def _bar_excursion(entry_price: float, *, side: str, high: float, low: float) -> tuple[float, float]:
    # Returns (adverse, favorable). Sign convention is the same for both sides:
    #   adverse   <= 0  (loss-side excursion since entry)
    #   favorable >= 0  (gain-side excursion since entry)
    # For shorts, `1 - high/entry` is negative when price moved up (adverse),
    # so callers can accumulate with `mae = min(0, adverse)` symmetrically.
    if side == "long":
        return low / entry_price - 1.0, high / entry_price - 1.0
    return 1.0 - high / entry_price, 1.0 - low / entry_price


def _side_return(entry_price: float, exit_price: float, *, side: str) -> float:
    simple = exit_price / entry_price - 1.0
    return simple if side == "long" else -simple


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_id": pl.Series([], dtype=pl.String),
            "basket_id": pl.Series([], dtype=pl.String),
            "entry_signal_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_date": pl.Series([], dtype=pl.String),
            "exit_date": pl.Series([], dtype=pl.String),
            "exit_month": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "side": pl.Series([], dtype=pl.String),
            "score": pl.Series([], dtype=pl.Float64),
            "rank": pl.Series([], dtype=pl.Int64),
            "entry_price": pl.Series([], dtype=pl.Float64),
            "exit_price": pl.Series([], dtype=pl.Float64),
            "exit_reason": pl.Series([], dtype=pl.String),
            "planned_exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "stop_price": pl.Series([], dtype=pl.Float64),
            "take_profit_price": pl.Series([], dtype=pl.Float64),
            "notional_weight": pl.Series([], dtype=pl.Float64),
            "position_weight": pl.Series([], dtype=pl.Float64),
            "gross_trade_return": pl.Series([], dtype=pl.Float64),
            "gross_return": pl.Series([], dtype=pl.Float64),
            "cost_return": pl.Series([], dtype=pl.Float64),
            "funding_return": pl.Series([], dtype=pl.Float64),
            "funding_mode": pl.Series([], dtype=pl.String),
            "funding_event_count": pl.Series([], dtype=pl.Int64),
            "net_return": pl.Series([], dtype=pl.Float64),
            "mae": pl.Series([], dtype=pl.Float64),
            "mfe": pl.Series([], dtype=pl.Float64),
            "bars_held": pl.Series([], dtype=pl.Int64),
            "hold_hours": pl.Series([], dtype=pl.Float64),
        }
    )


def _empty_baskets() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "basket_id": pl.Series([], dtype=pl.String),
            "entry_signal_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "basket_return": pl.Series([], dtype=pl.Float64),
            "gross_return": pl.Series([], dtype=pl.Float64),
            "cost_return": pl.Series([], dtype=pl.Float64),
            "funding_return": pl.Series([], dtype=pl.Float64),
            "long_return": pl.Series([], dtype=pl.Float64),
            "short_return": pl.Series([], dtype=pl.Float64),
            "trades": pl.Series([], dtype=pl.Int64),
            "winning_trades": pl.Series([], dtype=pl.Int64),
            "exit_date": pl.Series([], dtype=pl.String),
            "score": pl.Series([], dtype=pl.String),
            "quantile": pl.Series([], dtype=pl.Float64),
            "hold_days": pl.Series([], dtype=pl.Int64),
            "rebalance_days": pl.Series([], dtype=pl.Int64),
        }
    )


# --- shared indexed-bar simulation core for event-trade simulation ---

def _indexed_price_bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, Any]]:
    # Wraps the per-symbol parallel arrays from _price_bars_by_symbol with an
    # `ends` Python-int list (for bisect_right against entry/exit ts) and a
    # `by_end` map from bar_end_ts_ms -> row index. Downstream consumers read
    # bar fields via symbol_bars["close"][idx] etc. -- no per-bar dict.
    indexed: dict[str, dict[str, Any]] = {}
    for symbol, arrays in _price_bars_by_symbol(klines).items():
        ends = arrays["bar_end_ts_ms"].tolist()
        indexed[symbol] = {
            **arrays,
            "ends": ends,
            "by_end": {end_ts: idx for idx, end_ts in enumerate(ends)},
        }
    return indexed




def _bar_close_location(high: float, low: float, close: float) -> float:
    if abs(high - low) <= 1e-12:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))




def _event_decay_exit_hit(
    *,
    symbol: str,
    bar_end_ts_ms: int,
    rank_lookup: dict[tuple[str, int], float],
    threshold: float,
) -> bool:
    rank_fraction = rank_lookup.get((symbol, bar_end_ts_ms))
    return rank_fraction is not None and rank_fraction < threshold


@dataclass(slots=True)
class _IndexedTradeState:
    """Chronological lifecycle state for one accepted strategy position."""

    symbol: str
    side: str
    score: float
    rank: int
    basket_id: str
    signal_ts_ms: int
    entry_ts_ms: int
    entry_price: float
    planned_exit_ts_ms: int
    notional_weight: float
    position_weight: float
    config: TradeLifecycleConfig
    round_trip_cost_bps: float
    stop_price: float | None
    take_profit_price: float | None
    rank_lookup: dict[tuple[str, int], float]
    event_decay_threshold: float
    funding_lookup: dict[str, dict[str, Any]] | None
    stop_fill_mode: str
    stop_slippage_cap_pct: float
    mae: float = 0.0
    mfe: float = 0.0
    bars_held: int = 0
    breakeven_armed: bool = False
    exit_price: float | None = None
    exit_ts_ms: int | None = None
    exit_reason: str = "max_hold"

    @property
    def closed(self) -> bool:
        return self.exit_price is not None and self.exit_ts_ms is not None

    def on_bar(self, *, high: float, low: float, close: float, bar_end_ts_ms: int) -> bool:
        """Advance one available bar and return whether this bar closes the trade."""

        if self.closed:
            raise ValueError("cannot advance a closed indexed trade")
        self.bars_held += 1
        adverse, favorable = _bar_excursion(
            self.entry_price,
            side=self.side,
            high=high,
            low=low,
        )
        self.mae = min(self.mae, adverse)
        self.mfe = max(self.mfe, favorable)
        stop_hit, take_profit_hit = _bar_exit_hits(
            side=self.side,
            high=high,
            low=low,
            stop_price=self.stop_price,
            take_profit_price=self.take_profit_price,
        )
        if stop_hit:
            self.exit_price = _stop_fill_price(
                side=self.side,
                stop_price=self.stop_price,
                high=high,
                low=low,
                mode=self.stop_fill_mode,
                cap_pct=self.stop_slippage_cap_pct,
            )
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "stop_loss"
            return True
        if take_profit_hit:
            self.exit_price = self.take_profit_price
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "take_profit"
            return True
        close_return = _side_return(self.entry_price, close, side=self.side)
        if (
            self.config.mfe_giveback_trigger_pct > 0.0
            and self.config.mfe_giveback_retain_pct > 0.0
            and self.mfe >= self.config.mfe_giveback_trigger_pct
            and close_return <= self.mfe * self.config.mfe_giveback_retain_pct
        ):
            self.exit_price = close
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "mfe_giveback"
            return True
        if (
            self.config.breakeven_arm_pct > 0.0
            and not self.breakeven_armed
            and self.mfe >= self.config.breakeven_arm_pct
        ):
            self.breakeven_armed = True
        if self.breakeven_armed and close_return <= 0.0:
            self.exit_price = close
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "breakeven_stop"
            return True
        if _failed_fade_exit_hit(
            side=self.side,
            high=high,
            low=low,
            close=close,
            bars_held=self.bars_held,
            close_return=close_return,
            mfe=self.mfe,
            config=self.config,
        ):
            self.exit_price = close
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "failed_fade"
            return True
        if _event_decay_exit_hit(
            symbol=self.symbol,
            bar_end_ts_ms=bar_end_ts_ms,
            rank_lookup=self.rank_lookup,
            threshold=self.event_decay_threshold,
        ):
            self.exit_price = close
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "event_decay"
            return True
        if _rank_exit_hit(
            symbol=self.symbol,
            side=self.side,
            side_mode=self.config.side_mode,
            bar_end_ts_ms=bar_end_ts_ms,
            rank_lookup=self.rank_lookup,
            enabled=self.config.rank_exit_enabled,
            threshold=self.config.rank_exit_threshold,
        ):
            self.exit_price = close
            self.exit_ts_ms = bar_end_ts_ms
            self.exit_reason = "rank_exit"
            return True
        if self.config.hash_exit_prob > 0.0:
            hashed = (
                int(
                    hashlib.sha256(
                        f"{self.symbol}:{bar_end_ts_ms}".encode()
                    ).hexdigest()[:8],
                    16,
                )
                % 1_000_000
                / 1_000_000.0
            )
            if hashed < self.config.hash_exit_prob:
                self.exit_price = close
                self.exit_ts_ms = bar_end_ts_ms
                self.exit_reason = "hash_exit"
                return True
        return False

    def close_at_boundary(self, *, close: float, bar_end_ts_ms: int) -> None:
        if self.closed:
            raise ValueError("cannot boundary-close an already closed indexed trade")
        self.exit_price = close
        self.exit_ts_ms = bar_end_ts_ms
        self.exit_reason = (
            "data_end" if bar_end_ts_ms < self.planned_exit_ts_ms else "max_hold"
        )

    def to_trade(self) -> dict[str, Any]:
        if not self.closed:
            raise ValueError("cannot materialize an open indexed trade")
        assert self.exit_price is not None and self.exit_ts_ms is not None
        gross_trade_return = _side_return(
            self.entry_price,
            self.exit_price,
            side=self.side,
        )
        raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
            self.funding_lookup,
            symbol=self.symbol,
            side=self.side,
            entry_ts_ms=self.entry_ts_ms,
            exit_ts_ms=self.exit_ts_ms,
        )
        # Funding uses entry notional, not mark-to-market notional at settlement.
        effective_weight = self.notional_weight * self.position_weight
        funding_return = abs(effective_weight) * raw_funding_return
        cost_return = -abs(effective_weight) * self.round_trip_cost_bps / 10_000.0
        gross_return = abs(effective_weight) * gross_trade_return
        net_return = gross_return + cost_return + funding_return
        return {
            "trade_id": f"{self.basket_id}-{self.side[0]}-{self.symbol}",
            "basket_id": self.basket_id,
            "entry_signal_ts_ms": self.signal_ts_ms,
            "entry_ts_ms": self.entry_ts_ms,
            "exit_ts_ms": self.exit_ts_ms,
            "entry_date": _iso_date(self.entry_ts_ms),
            "exit_date": _iso_date(self.exit_ts_ms),
            "exit_month": _iso_month(self.exit_ts_ms),
            "symbol": self.symbol,
            "side": self.side,
            "score": self.score,
            "rank": self.rank,
            "entry_price": self.entry_price,
            "exit_price": float(self.exit_price),
            "exit_reason": self.exit_reason,
            "planned_exit_ts_ms": self.planned_exit_ts_ms,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "notional_weight": abs(effective_weight),
            "position_weight": self.position_weight,
            "gross_trade_return": gross_trade_return,
            "gross_return": gross_return,
            "cost_return": cost_return,
            "funding_return": funding_return,
            "funding_mode": funding_mode,
            "funding_event_count": funding_event_count,
            "net_return": net_return,
            "mae": self.mae,
            "mfe": self.mfe,
            "bars_held": self.bars_held,
            "hold_hours": (self.exit_ts_ms - self.entry_ts_ms) / MS_PER_HOUR,
        }


def _failed_fade_exit_hit(
    *,
    side: str,
    high: float,
    low: float,
    close: float,
    bars_held: int,
    close_return: float,
    mfe: float,
    config: TradeLifecycleConfig,
) -> bool:
    if (
        config.failed_fade_exit_hours <= 0
        or config.failed_fade_loss_pct <= 0.0
        or config.failed_fade_min_mfe_pct < 0.0
        or not 0.0 <= config.failed_fade_close_location_min <= 1.0
        or bars_held < config.failed_fade_exit_hours
    ):
        return False
    if mfe >= config.failed_fade_min_mfe_pct:
        return False
    if close_return > -config.failed_fade_loss_pct:
        return False
    close_location = _bar_close_location(high, low, close)
    if side == "short":
        return close_location >= config.failed_fade_close_location_min
    return close_location <= 1.0 - config.failed_fade_close_location_min


def _stop_fill_price(
    *, side: str, stop_price: float | None, high: float, low: float, mode: str, cap_pct: float = 0.10
) -> float:
    if stop_price is None:
        return float("nan")
    if mode == "stop":
        return float(stop_price)
    if mode == "bar_extreme":
        return float(min(stop_price, low) if side == "long" else max(stop_price, high))
    if mode == "bar_extreme_capped":
        # Bar extreme, but cap adverse slippage at cap_pct beyond the trigger so a single
        # thin 1h wick cannot dictate the fill (the realistic-bad-case default).
        if side == "long":
            return float(max(min(stop_price, low), stop_price * (1.0 - max(cap_pct, 0.0))))
        return float(min(max(stop_price, high), stop_price * (1.0 + max(cap_pct, 0.0))))
    raise ValueError(f"Unknown stop_fill_mode: {mode}")


def _has_columns(frame: pl.DataFrame, *columns: str) -> bool:
    available = set(frame.columns)
    return all(column in available for column in columns)
