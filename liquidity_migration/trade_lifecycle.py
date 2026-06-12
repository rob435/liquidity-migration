from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any

import numpy as np
import polars as pl

from .config import TradeLifecycleConfig
from ._common import MS_PER_DAY, MS_PER_HOUR, _iso_date, _iso_month, date_boundary_ms


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
    # Compound the portfolio on a daily grid. Each basket is a fractional slice
    # (weight ~ 1/max_active), so baskets realised on the same day are additive
    # and equity only compounds across days. Per-basket cum_prod in exit order
    # instead multiplied overlapping positions onto one another -- inventing
    # spurious cross-terms and a path-dependent drawdown. (This is realised-PnL
    # accounting: a basket's whole return lands on its exit day; intra-hold
    # mark-to-market would additionally need a daily price path.)
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
    eq_df = equity.sort("ts_ms")
    ts = eq_df["ts_ms"].to_numpy().astype(np.int64)
    eq = eq_df["equity"].to_numpy().astype(float)
    if eq.size < 2:
        return 0.0
    first_ts, last_ts = int(ts[0]), int(ts[-1])
    span_days = max(1, int(round((last_ts - first_ts) / MS_PER_DAY)) + 1)
    if span_days < 2:
        return 0.0
    # Forward-fill onto the calendar-day grid: for each day in [first, last],
    # equity equals the equity of the most recent exit at or before that day.
    grid_eq = np.empty(span_days, dtype=float)
    j = 0
    for i in range(span_days):
        day_ts = first_ts + i * MS_PER_DAY
        while j + 1 < ts.size and ts[j + 1] <= day_ts:
            j += 1
        grid_eq[i] = eq[j]
    daily_ret = np.diff(grid_eq) / grid_eq[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]
    if daily_ret.size < 2:
        return 0.0
    mu = float(daily_ret.mean())
    sd = float(daily_ret.std(ddof=1))
    if sd <= 1e-12:
        return 0.0
    return mu / sd * math.sqrt(365.0)


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
    """Intra-hold adverse-excursion (H2) + realized-gross (M3) diagnostics.

    H2: the realised-PnL-at-exit drawdown ignores how far a position ran against
    us DURING the hold. ``mae`` is each trade's max adverse excursion (<=0); these
    surface that hidden intra-hold risk. NOTE — these are PER-POSITION excursions:
    a true portfolio mark-to-market drawdown (which compounds CONCURRENT open
    positions and re-calibrates the pre-registered DD gate thresholds) is strictly
    deeper and is its own pre-registered sub-phase
    (docs/research_summary.md). Treat
    ``worst_weighted_intrahold_loss`` as a LOWER BOUND on portfolio intra-hold DD.

    M3: ``realized_gross_mean``/``_max`` is the per-basket sum of position gross
    shares (``notional_weight``). risk_equal sizing lets gross float, so a
    cell-vs-control MAR delta can partly reflect different gross rather than better
    risk-adjustment — surfacing realised gross makes that confound auditable.
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
        # Drop non-finite mae: a sleeve that does not track intra-hold path (e.g.
        # the long sleeve) emits NaN, which must read as "not measured" — not a
        # spurious 0 — and must never poison min()/mean() of a sleeve that does.
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


def _worst_volume_day_return(baskets: pl.DataFrame) -> float:
    if baskets.is_empty() or "exit_date" not in baskets.columns:
        return 0.0
    daily = baskets.group_by("exit_date").agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("day_return"))
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


def _filter_universe(part: pl.DataFrame, config: TradeLifecycleConfig) -> pl.DataFrame:
    filtered = part
    include = {symbol.upper() for symbol in config.include_symbols}
    exclude = {symbol.upper() for symbol in config.exclude_symbols}
    if include:
        filtered = filtered.filter(pl.col("symbol").is_in(sorted(include)))
    if exclude:
        filtered = filtered.filter(~pl.col("symbol").is_in(sorted(exclude)))
    if config.universe_min_daily_turnover > 0.0 and "turnover_quote" in filtered.columns:
        filtered = filtered.filter(pl.col("turnover_quote") >= config.universe_min_daily_turnover)
    if "liquidity_rank" in filtered.columns:
        filtered = filtered.filter(pl.col("liquidity_rank") >= config.universe_rank_min)
        if config.universe_rank_max > 0:
            filtered = filtered.filter(pl.col("liquidity_rank") <= config.universe_rank_max)
    return filtered




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
    *,
    interval_by_symbol: dict[str, int] | None = None,
) -> dict[str, dict[str, Any]] | None:
    if funding is None or funding.is_empty() or "symbol" not in funding.columns or "ts_ms" not in funding.columns:
        return None
    rate_col = "funding_rate" if "funding_rate" in funding.columns else "funding_rate_8h_equiv"
    if rate_col not in funding.columns:
        return None
    rows = funding.select(["symbol", "ts_ms", rate_col]).drop_nulls(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    # Raw first/last stamp per symbol — used for the coverage ("partial") check.
    raw_span = {
        str(row["symbol"]): (int(row["start_ts_ms"]), int(row["end_ts_ms"]))
        for row in rows.group_by("symbol")
        .agg(pl.col("ts_ms").min().alias("start_ts_ms"), pl.col("ts_ms").max().alias("end_ts_ms"))
        .to_dicts()
    }
    # Collapse rows belonging to the SAME settlement. Both venues' funding-history endpoints emit ONE
    # row per settlement, so distinct ts_ms ARE distinct settlements: the default is an exact-stamp
    # dedup (which also drops overlapping-fetch duplicates) that counts EVERY settlement. The old code
    # bucketed by the stored funding_interval_min, which _normalize_binance_funding hardcoded to 8h
    # (and the Bybit funding-history endpoint omits -> also 8h); for a real 4h-settling alt that 8h
    # window merged two distinct settlements into one and charged HALF the funding, inflating
    # short-strategy MAR (audit funding-undercount, fixed 2026-06-03). A caller that holds the
    # AUTHORITATIVE per-symbol settlement interval (e.g. instruments.fundingInterval) may pass
    # interval_by_symbol to additionally collapse genuine intra-interval SNAPSHOT rows — an operation
    # only valid with the true interval; symbols absent from the map fall back to exact-stamp dedup.
    if interval_by_symbol:
        interval_df = pl.DataFrame(
            {
                "symbol": list(interval_by_symbol),
                "_interval_min": [int(v) for v in interval_by_symbol.values()],
            }
        )
        rows = rows.join(interval_df, on="symbol", how="left").with_columns(
            pl.when((pl.col("_interval_min").is_not_null()) & (pl.col("_interval_min") > 0))
            .then(pl.col("ts_ms") // (pl.col("_interval_min") * 60_000))
            .otherwise(pl.col("ts_ms"))  # no true interval -> one-per-settlement exact-stamp dedup
            .alias("_settlement")
        )
        rows = (
            rows.group_by(["symbol", "_settlement"], maintain_order=True)
            .agg(pl.col("ts_ms").first(), pl.col(rate_col).first())
            .sort(["symbol", "ts_ms"])
        )
    else:
        rows = rows.unique(["symbol", "ts_ms"], keep="first").sort(["symbol", "ts_ms"])
    output: dict[str, dict[str, Any]] = {}
    for key, part in rows.partition_by("symbol", as_dict=True, maintain_order=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        # Store parallel sorted lists so _perp_funding_return can slice the
        # in-window events in O(log n) via bisect instead of an O(n) scan per
        # trade. ts_list is already sorted by the upstream `.sort(["symbol","ts_ms"])`.
        # Column .to_list() avoids building per-row dicts twice (was two full
        # part.to_dicts() passes); identical values/order (audit pass2 #19).
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
    # A trade whose window extends past the funding dataset is still charged the
    # funding that IS covered, and flagged "partial" -- zeroing the whole trade
    # would silently drop a real cost/credit from total_return.
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
    # Parallel numpy arrays per symbol: ts_ms / bar_end_ts_ms / open / high /
    # low / close. Replaces an earlier dict-of-dicts layout that materialized
    # ~12M Python dicts up front and forced float() casts on every hot-loop
    # read; arrays let consumers index by position in C without a per-bar dict
    # build or attribute access.
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


def _stop_price(entry_price: float, *, side: str, stop_loss_pct: float) -> float | None:
    if stop_loss_pct <= 0.0:
        return None
    return entry_price * (1.0 - stop_loss_pct) if side == "long" else entry_price * (1.0 + stop_loss_pct)


def _take_profit_price(entry_price: float, *, side: str, take_profit_pct: float) -> float | None:
    if take_profit_pct <= 0.0:
        return None
    return entry_price * (1.0 + take_profit_pct) if side == "long" else entry_price * (1.0 - take_profit_pct)


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


# --- shared indexed-bar simulation core (relocated from volume_events.py when the
# daily-short research engine was erased, operator order 2026-06-11; used by the
# continuous engine and any future event-trade simulation) ---

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


def _simulate_indexed_trade(
    *,
    symbol: str,
    side: str,
    score: float,
    rank: int,
    basket_id: str,
    signal_ts_ms: int,
    entry_bar: int,
    symbol_bars: dict[str, Any],
    planned_exit_ts_ms: int,
    notional_weight: float,
    position_weight: float = 1.0,
    config: TradeLifecycleConfig,
    round_trip_cost_bps: float,
    stop_pct: float | None,
    rank_lookup: dict[tuple[str, int], float],
    event_decay_threshold: float,
    funding_lookup: dict[str, dict[str, Any]] | None,
    stop_fill_mode: str = "stop",
    stop_slippage_cap_pct: float = 0.10,
) -> dict[str, Any] | None:
    bar_end_ts_arr = symbol_bars["bar_end_ts_ms"]
    high_arr = symbol_bars["high"]
    low_arr = symbol_bars["low"]
    close_arr = symbol_bars["close"]
    entry_ts_ms = int(bar_end_ts_arr[entry_bar])
    entry_price = float(close_arr[entry_bar])
    if entry_price <= 0.0:
        return None
    ends = symbol_bars["ends"]
    start = bisect_right(ends, entry_ts_ms)
    end = bisect_right(ends, planned_exit_ts_ms)
    if start >= end:
        return None

    stop_price = _stop_price(entry_price, side=side, stop_loss_pct=stop_pct or 0.0)
    loose_stop_price = (
        _stop_price(entry_price, side=side, stop_loss_pct=config.stop_loose_pct)
        if config.stop_loose_window_hours > 0 and config.stop_loose_pct > 0.0
        else None
    )
    take_profit_price = _take_profit_price(entry_price, side=side, take_profit_pct=config.take_profit_pct)
    exit_price = None
    exit_ts_ms = None
    exit_reason = "max_hold"
    mae = 0.0
    mfe = 0.0
    bars_held = 0
    breakeven_armed = False
    profit_lock_armed = False
    for idx in range(start, end):
        bars_held += 1
        bar_high = float(high_arr[idx])
        bar_low = float(low_arr[idx])
        bar_close = float(close_arr[idx])
        bar_end_ts_ms_val = int(bar_end_ts_arr[idx])
        adverse, favorable = _bar_excursion(entry_price, side=side, high=bar_high, low=bar_low)
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        effective_stop_price = (
            loose_stop_price
            if loose_stop_price is not None and bars_held <= config.stop_loose_window_hours
            else stop_price
        )
        stop_hit, take_profit_hit = _bar_exit_hits(
            side=side,
            high=bar_high,
            low=bar_low,
            stop_price=effective_stop_price,
            take_profit_price=take_profit_price,
        )
        if stop_hit:
            exit_price = _stop_fill_price(
                side=side, stop_price=effective_stop_price, high=bar_high, low=bar_low,
                mode=stop_fill_mode, cap_pct=stop_slippage_cap_pct,
            )
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "stop_loss"
            break
        if take_profit_hit:
            exit_price = take_profit_price
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "take_profit"
            break
        close_return = _side_return(entry_price, bar_close, side=side)
        if (
            config.mfe_giveback_trigger_pct > 0.0
            and config.mfe_giveback_retain_pct > 0.0
            and mfe >= config.mfe_giveback_trigger_pct
            and close_return <= mfe * config.mfe_giveback_retain_pct
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "mfe_giveback"
            break
        if config.profit_lock_arm_pct > 0.0 and not profit_lock_armed and mfe >= config.profit_lock_arm_pct:
            profit_lock_armed = True
        if profit_lock_armed and close_return <= config.profit_lock_floor_pct:
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "profit_lock"
            break
        if config.breakeven_arm_pct > 0.0 and not breakeven_armed and mfe >= config.breakeven_arm_pct:
            breakeven_armed = True
        if breakeven_armed and close_return <= 0.0:
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "breakeven_stop"
            break
        if _failed_fade_exit_hit(
            side=side,
            high=bar_high,
            low=bar_low,
            close=bar_close,
            bars_held=bars_held,
            close_return=close_return,
            mfe=mfe,
            config=config,
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "failed_fade"
            break
        if _event_decay_exit_hit(
            symbol=symbol,
            bar_end_ts_ms=bar_end_ts_ms_val,
            rank_lookup=rank_lookup,
            threshold=event_decay_threshold,
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "event_decay"
            break
        if _rank_exit_hit(
            symbol=symbol,
            side=side,
            side_mode=config.side_mode,
            bar_end_ts_ms=bar_end_ts_ms_val,
            rank_lookup=rank_lookup,
            enabled=config.rank_exit_enabled,
            threshold=config.rank_exit_threshold,
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "rank_exit"
            break
    if exit_price is None:
        last_idx = end - 1
        exit_price = float(close_arr[last_idx])
        exit_ts_ms = int(bar_end_ts_arr[last_idx])
        if exit_ts_ms < planned_exit_ts_ms:
            exit_reason = "data_end"

    gross_trade_return = _side_return(entry_price, exit_price, side=side)
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup,
        symbol=symbol,
        side=side,
        entry_ts_ms=entry_ts_ms,
        exit_ts_ms=int(exit_ts_ms),
    )
    effective_weight = notional_weight * position_weight
    funding_return = abs(effective_weight) * raw_funding_return
    cost_return = -abs(effective_weight) * round_trip_cost_bps / 10_000.0
    gross_return = abs(effective_weight) * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    trade_id = f"{basket_id}-{side[0]}-{symbol}"
    return {
        "trade_id": trade_id,
        "basket_id": basket_id,
        "entry_signal_ts_ms": signal_ts_ms,
        "entry_ts_ms": entry_ts_ms,
        "exit_ts_ms": int(exit_ts_ms),
        "entry_date": _iso_date(entry_ts_ms),
        "exit_date": _iso_date(int(exit_ts_ms)),
        "exit_month": _iso_month(int(exit_ts_ms)),
        "symbol": symbol,
        "side": side,
        "score": score,
        "rank": rank,
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "planned_exit_ts_ms": planned_exit_ts_ms,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "notional_weight": abs(effective_weight),
        "position_weight": position_weight,
        "gross_trade_return": gross_trade_return,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "funding_return": funding_return,
        "funding_mode": funding_mode,
        "funding_event_count": funding_event_count,
        "net_return": net_return,
        "mae": mae,
        "mfe": mfe,
        "bars_held": bars_held,
        "hold_hours": (int(exit_ts_ms) - entry_ts_ms) / MS_PER_HOUR,
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



