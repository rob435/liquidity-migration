from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any

import numpy as np
import polars as pl

from .config import TradeLifecycleConfig
from ._common import MS_PER_DAY, MS_PER_HOUR, date_boundary_ms


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
    (docs/preregistration/round2/r-audit-methodology-hardening.md). Treat
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
        mae = trades["mae"].drop_nulls()
        if not mae.is_empty():
            out["worst_trade_mae"] = float(mae.min())
            out["mean_trade_mae"] = float(mae.mean())
        if "notional_weight" in trades.columns:
            weighted = (
                trades.select((pl.col("mae") * pl.col("notional_weight").abs()).alias("w"))
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


def _rank_lookup(
    features: pl.DataFrame,
    *,
    score_col: str,
    entry_delay_hours: int,
    config: TradeLifecycleConfig,
) -> dict[tuple[str, int], float]:
    output: dict[tuple[str, int], float] = {}
    if features.is_empty() or score_col not in features.columns:
        return output
    for part in features.sort(["ts_ms", "symbol"]).partition_by("ts_ms", maintain_order=True):
        part = _filter_universe(part, config)
        values = part.select(["symbol", score_col]).drop_nulls().sort(score_col)
        values = values.filter(pl.col(score_col).is_finite())
        if values.height < 2:
            continue
        exit_check_ts_ms = int(part["ts_ms"][0]) + entry_delay_hours * MS_PER_HOUR
        denom = max(values.height - 1, 1)
        for rank, row in enumerate(values.to_dicts()):
            output[(str(row["symbol"]), exit_check_ts_ms)] = rank / denom
    return output


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


def _funding_lookup(funding: pl.DataFrame | None) -> dict[str, dict[str, Any]] | None:
    if funding is None or funding.is_empty() or "symbol" not in funding.columns or "ts_ms" not in funding.columns:
        return None
    rate_col = "funding_rate" if "funding_rate" in funding.columns else "funding_rate_8h_equiv"
    if rate_col not in funding.columns:
        return None
    keep = ["symbol", "ts_ms", rate_col]
    if "funding_interval_min" in funding.columns:
        keep.append("funding_interval_min")
    rows = funding.select(keep).drop_nulls(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    # Raw first/last stamp per symbol — used for the coverage ("partial") check.
    raw_span = {
        str(row["symbol"]): (int(row["start_ts_ms"]), int(row["end_ts_ms"]))
        for row in rows.group_by("symbol")
        .agg(pl.col("ts_ms").min().alias("start_ts_ms"), pl.col("ts_ms").max().alias("end_ts_ms"))
        .to_dicts()
    }
    if "funding_interval_min" in rows.columns:
        # Funding settles once per `funding_interval_min`, but some symbols carry
        # intra-interval snapshot rows (e.g. hourly rows of an 8h rate). Charging
        # every row would bill the settlement rate up to 8x, so collapse each
        # settlement window to its boundary-aligned row.
        interval_ms = (
            pl.col("funding_interval_min").cast(pl.Int64, strict=False).fill_null(480).clip(1) * 60_000
        )
        rows = (
            rows.with_columns((pl.col("ts_ms") // interval_ms).alias("_settlement"))
            .group_by(["symbol", "_settlement"], maintain_order=True)
            .agg(pl.col("ts_ms").first(), pl.col(rate_col).first())
            .sort(["symbol", "ts_ms"])
        )
    output: dict[str, dict[str, Any]] = {}
    for key, part in rows.partition_by("symbol", as_dict=True, maintain_order=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        # Store parallel sorted lists so _perp_funding_return can slice the
        # in-window events in O(log n) via bisect instead of an O(n) scan per
        # trade. ts_list is already sorted by the upstream `.sort(["symbol","ts_ms"])`.
        ts_list = [int(row["ts_ms"]) for row in part.to_dicts()]
        rate_list = [float(row[rate_col]) for row in part.to_dicts()]
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


def _exit_reason_rows(trades: pl.DataFrame) -> list[dict[str, Any]]:
    if trades.is_empty():
        return []
    return (
        trades.group_by("exit_reason")
        .agg(
            [
                pl.len().alias("trades"),
                pl.col("net_return").sum().alias("net_return"),
                pl.col("net_return").mean().alias("avg_trade_return"),
            ]
        )
        .sort("net_return", descending=True)
        .to_dicts()
    )
