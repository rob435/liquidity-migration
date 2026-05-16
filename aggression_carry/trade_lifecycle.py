from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl

from .config import TradeLifecycleConfig
from .volume_features import MS_PER_HOUR


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
    rows = []
    equity = 1.0
    peak = 1.0
    for row in baskets.sort("exit_ts_ms").to_dicts():
        basket_return = float(row["basket_return"])
        equity *= 1.0 + basket_return
        peak = max(peak, equity)
        rows.append(
            {
                "ts_ms": int(row["exit_ts_ms"]),
                "equity": equity,
                "drawdown": equity / peak - 1.0,
                "basket_return": basket_return,
            }
        )
    return pl.DataFrame(rows).with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
    )


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
        }
    basket_returns = np.asarray(baskets["basket_return"].to_list(), dtype=float)
    mean_return = float(np.mean(basket_returns)) if basket_returns.size else 0.0
    vol = float(np.std(basket_returns, ddof=1)) if basket_returns.size > 1 else 0.0
    annual_periods = 365.0 / config.rebalance_days
    wins = trades.filter(pl.col("net_return") > 0.0)
    losses = trades.filter(pl.col("net_return") < 0.0)
    profit = float(wins["net_return"].sum()) if not wins.is_empty() else 0.0
    loss = float(losses["net_return"].sum()) if not losses.is_empty() else 0.0
    return {
        "total_return": float(equity["equity"][-1] - 1.0),
        "sharpe_like": float(mean_return / vol * math.sqrt(annual_periods)) if vol > 1e-12 else 0.0,
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
    }


def _funding_mode_summary(trades: pl.DataFrame) -> str:
    if trades.is_empty() or "funding_mode" not in trades.columns:
        return "missing"
    modes = set(str(item) for item in trades["funding_mode"].to_list())
    if modes == {"modeled"}:
        return "modeled"
    if "modeled" in modes:
        return "partial"
    return "missing"


def _worst_volume_day_return(baskets: pl.DataFrame) -> float:
    if baskets.is_empty() or "exit_date" not in baskets.columns:
        return 0.0
    daily = baskets.group_by("exit_date").agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("day_return"))
    return float(daily["day_return"].min()) if not daily.is_empty() else 0.0


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
    text = str(value or "").strip()
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp() * 1000)


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


def _funding_lookup(funding: pl.DataFrame | None) -> dict[str, list[tuple[int, float]]] | None:
    if funding is None or funding.is_empty() or "symbol" not in funding.columns or "ts_ms" not in funding.columns:
        return None
    rate_col = "funding_rate" if "funding_rate" in funding.columns else "funding_rate_8h_equiv"
    if rate_col not in funding.columns:
        return None
    output: dict[str, list[tuple[int, float]]] = {}
    rows = funding.select(["symbol", "ts_ms", rate_col]).drop_nulls(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    for key, part in rows.partition_by("symbol", as_dict=True, maintain_order=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = [(int(row["ts_ms"]), float(row[rate_col])) for row in part.to_dicts()]
    return output


def _perp_funding_return(
    funding_lookup: dict[str, list[tuple[int, float]]] | None,
    *,
    symbol: str,
    side: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> tuple[float, str, int]:
    if funding_lookup is None:
        return 0.0, "missing", 0
    events = [
        rate
        for ts_ms, rate in funding_lookup.get(symbol, [])
        if entry_ts_ms < ts_ms <= exit_ts_ms
    ]
    if not events:
        return 0.0, "modeled", 0
    signed = sum(events)
    return (float(-signed) if side == "long" else float(signed)), "modeled", len(events)


def _price_bars_by_symbol(klines: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    required = {"ts_ms", "symbol", "open", "high", "low", "close"}
    missing = required - set(klines.columns)
    if missing:
        raise RuntimeError(f"klines_1h is missing required columns: {sorted(missing)}")
    output: dict[str, list[dict[str, Any]]] = {}
    prepared = klines.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
    for key, part in prepared.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = part.to_dicts()
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
