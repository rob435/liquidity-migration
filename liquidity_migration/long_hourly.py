"""Hourly FOMO scanner — looks for intraday 1h breakout bars in liquid coins.

The daily-frequency FC pattern hits Sharpe 1.48 honest but only fires 14.9
times/year — too sparse for meaningful annualization. This module scans
every 1h bar for the same setup: strong upward move + heavy volume + bullish
regime, with tighter intra-day exits. Goal is 200-500 trades/year so the
Sharpe is statistically robust.

Detection runs per 1h bar:
  - 1h return >= threshold (e.g., +3%)
  - 1h volume in top quartile of trailing 24h
  - daily turnover puts coin in top universe
  - BTC > N-day SMA (regime)
  - cooldown per symbol satisfied

Entry: 1h after the trigger bar's close (one bar later).
Exit: stop, take-profit, OR max-hold-hours (typically 24-48h).
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, date_ms, pct
from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .momentum_signals import daily_bars
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import (
    _funding_lookup,
    _perp_funding_return,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from .volume_events import (
    _date_range,
    _exclude_symbols,
    _full_pit_universe_pass,
    _iso_date,
    _iso_month,
    _pit_manifest_metadata,
)


SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)


@dataclass(frozen=True, slots=True)
class HourlyLongConfig:
    # window
    start_date: str = ""
    end_date: str = ""
    # universe — same daily-rolled top-N approach
    universe_size: int = 30
    universe_volume_window_days: int = 90
    min_listing_history_days: int = 90
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    # broad regime — BTC 1h close > 1h SMA(N days × 24)
    regime_symbol: str = "BTCUSDT"
    regime_sma_hours: int = 24 * 30  # 30-day SMA on hourly bars
    # 1h signal thresholds
    bar_min_return: float = 0.03  # 1h return >= 3%
    bar_volume_lookback_hours: int = 24
    bar_volume_quantile: float = 0.75  # volume in top 25% of trailing 24h
    bar_close_location_min: float = 0.60  # closed in upper 60% of the bar
    # entry
    entry_delay_hours: int = 1
    # exits
    stop_pct: float = 0.05
    take_profit_pct: float = 0.15
    max_hold_hours: int = 48
    # portfolio
    max_concurrent_positions: int = 6
    cooldown_hours: int = 24
    gross_exposure: float = 1.0
    max_position_weight: float = 0.30
    # cost / data
    cost_multiplier: float = 3.0
    require_pit_membership: bool = True
    require_full_pit_universe: bool = True
    # promotion thresholds
    promotion_min_avg_sharpe: float = 1.0
    promotion_max_drawdown: float = -0.30


def run_hourly_long_research(
    data_root: str | Path,
    *,
    config: HourlyLongConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = config or HourlyLongConfig()
    costs = cost_config or CostConfig()
    root = Path(data_root).expanduser()
    output_dir = Path(report_dir) if report_dir else root / "reports" / "hourly_long_research"
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_klines = read_dataset_columns(
        root, "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    if raw_klines.is_empty():
        raise RuntimeError("klines_1h empty")
    funding = read_dataset(root, "funding")
    archive_manifest = read_dataset(root, "archive_trade_manifest")

    klines = _exclude_symbols(raw_klines, cfg.exclude_symbols)
    funding = _exclude_symbols(funding, cfg.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, cfg.exclude_symbols)
    full_pit = _full_pit_universe_pass(klines, archive_manifest)

    # Daily universe membership (rebuilt daily, applied to 1h bars by date).
    daily = daily_bars(klines)
    daily = daily.sort(["symbol", "ts_ms"]).with_columns([
        ((pl.col("ts_ms") - pl.col("ts_ms").min().over("symbol")) / MS_PER_DAY + 1).cast(pl.Int64).alias("symbol_age_days"),
        pl.col("turnover_quote").rolling_median(window_size=cfg.universe_volume_window_days,
                                                min_samples=cfg.universe_volume_window_days)
         .over("symbol").alias("turnover_median_90d"),
    ]).with_columns(
        pl.col("turnover_median_90d").rank(method="ordinal", descending=True).over("ts_ms").alias("universe_rank")
    ).with_columns(
        (
            (pl.col("universe_rank") <= cfg.universe_size)
            & (pl.col("symbol_age_days") >= cfg.min_listing_history_days)
            & pl.col("turnover_median_90d").is_finite()
        ).alias("in_universe")
    ).select(["ts_ms", "symbol", "in_universe"])
    daily = daily.rename({"ts_ms": "day_end_ts"})

    # BTC 1h regime — close > rolling mean over regime_sma_hours
    btc = klines.filter(pl.col("symbol") == cfg.regime_symbol).sort("ts_ms")
    if btc.is_empty():
        regime_by_hour = {}
    else:
        btc = btc.with_columns(
            pl.col("close").rolling_mean(window_size=cfg.regime_sma_hours, min_samples=cfg.regime_sma_hours).alias("btc_sma")
        ).with_columns(
            (pl.col("close") > pl.col("btc_sma")).alias("regime_on")
        ).select(["ts_ms", "regime_on"])
        regime_by_hour = {int(r["ts_ms"]): bool(r["regime_on"]) for r in btc.to_dicts()}

    # Per-symbol 1h arrays with computed signal columns
    bars_index = _build_hourly_index(klines, cfg)

    # Build set of valid (day_end_ts, symbol) for in_universe
    in_universe_set = set(
        (int(r["day_end_ts"]), str(r["symbol"]))
        for r in daily.filter(pl.col("in_universe")).select(["day_end_ts", "symbol"]).to_dicts()
    )

    funding_lookup = _funding_lookup(funding) if funding is not None and not funding.is_empty() else None

    trades, stats = _run_hourly_pipeline(
        bars_index=bars_index,
        in_universe_set=in_universe_set,
        regime_by_hour=regime_by_hour,
        funding_lookup=funding_lookup,
        config=cfg, costs=costs,
        start_ms=date_ms(cfg.start_date) if cfg.start_date else None,
        end_ms=date_ms(cfg.end_date) if cfg.end_date else None,
    )

    bt_config = TradeLifecycleConfig(score="hourly_long", hold_days=max(1, cfg.max_hold_hours // 24),
                                     rebalance_days=max(1, cfg.max_hold_hours // 24),
                                     gross_exposure=cfg.gross_exposure, entry_delay_hours=cfg.entry_delay_hours,
                                     cost_multiplier=cfg.cost_multiplier, side_mode="long_high_short_low")
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    splits = _split_rows(baskets, config=bt_config)
    funding_mode = summary.get("funding_mode", "missing")

    # Also compute honest daily-aligned Sharpe
    daily_sharpe = _daily_aligned_sharpe(equity)

    promo_pass = (
        full_pit and all(r["total_return"] > 0 for r in splits if r["basket_count"] > 0)
        and summary.get("max_drawdown", 0) >= cfg.promotion_max_drawdown
        and daily_sharpe >= cfg.promotion_min_avg_sharpe
        and funding_mode != "missing"
    )
    promotion = {"promotion_gate_pass": bool(promo_pass), "daily_sharpe": daily_sharpe}

    if not trades.is_empty():
        trades.write_csv(output_dir / "hourly_long_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "hourly_long_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "hourly_long_equity.csv")

    metadata = {
        "config": asdict(cfg), "rows": {"trades": trades.height, "baskets": baskets.height},
        "date_range": _date_range(_to_features_for_range(bars_index)),
        "pit_manifest": _pit_manifest_metadata(archive_manifest, _to_features_for_range(bars_index), klines),
        "cost_model": {**asdict(costs), "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
                       "cost_multiplier": cfg.cost_multiplier,
                       "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier},
        "summary": summary, "lifecycle": stats, "splits": splits, "promotion": promotion,
        "daily_sharpe": daily_sharpe,
        "run_label": "full_pit_universe" if full_pit and funding_mode != "missing" else
                     "full_pit_universe_funding_missing" if full_pit else "pit_membership_filtered_current_universe",
    }
    (output_dir / "hourly_long_research_report.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    (output_dir / "hourly_long_research_report.md").write_text(format_hourly_long_report(metadata), encoding="utf-8")
    return {**metadata, "report_dir": str(output_dir)}


def _build_hourly_index(klines: pl.DataFrame, cfg: HourlyLongConfig) -> dict[str, dict[str, Any]]:
    """Per-symbol 1h arrays with bar-return, volume-rank, close-location."""
    output: dict[str, dict[str, Any]] = {}
    # Compute features per symbol
    prepared = klines.with_columns(
        (pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms")
    ).sort(["symbol", "ts_ms"]).with_columns([
        # 1h return = close / prev_close - 1
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("bar_return"),
        # close-location within the bar
        pl.when((pl.col("high") - pl.col("low")) > 1e-12)
          .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
          .otherwise(0.5).alias("close_location"),
        # rolling 24h volume quantile threshold
        pl.col("turnover_quote").rolling_quantile(
            quantile=cfg.bar_volume_quantile,
            window_size=cfg.bar_volume_lookback_hours, min_samples=cfg.bar_volume_lookback_hours
        ).over("symbol").alias("volume_threshold"),
    ])
    for key, part in prepared.partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        ends = part["bar_end_ts_ms"].to_numpy()
        # day-end timestamp for membership check: floor(ts_ms / MS_PER_DAY) * MS_PER_DAY + MS_PER_DAY
        day_end = (part["ts_ms"].cast(pl.Int64).to_numpy() // MS_PER_DAY) * MS_PER_DAY + MS_PER_DAY
        output[symbol] = {
            "ends": ends.tolist(),
            "by_end": {int(e): i for i, e in enumerate(ends)},
            "bar_end_ts_ms": ends,
            "open": part["open"].to_numpy(),
            "high": part["high"].to_numpy(),
            "low": part["low"].to_numpy(),
            "close": part["close"].to_numpy(),
            "bar_return": part["bar_return"].to_numpy(),
            "close_location": part["close_location"].to_numpy(),
            "turnover_quote": part["turnover_quote"].to_numpy(),
            "volume_threshold": part["volume_threshold"].to_numpy(),
            "day_end_for_membership": day_end,
        }
    return output


def _to_features_for_range(bars_index: dict[str, dict[str, Any]]) -> pl.DataFrame:
    """Build a placeholder features df just for date_range computation."""
    if not bars_index:
        return pl.DataFrame({"ts_ms": pl.Series([], dtype=pl.Int64), "symbol": pl.Series([], dtype=pl.String), "date": pl.Series([], dtype=pl.String)})
    rows = []
    for symbol, arr in bars_index.items():
        if len(arr["bar_end_ts_ms"]) == 0:
            continue
        rows.append({"ts_ms": int(arr["bar_end_ts_ms"][0]), "symbol": symbol, "date": _iso_date(int(arr["bar_end_ts_ms"][0]))})
        rows.append({"ts_ms": int(arr["bar_end_ts_ms"][-1]), "symbol": symbol, "date": _iso_date(int(arr["bar_end_ts_ms"][-1]))})
    return pl.DataFrame(rows)


def _run_hourly_pipeline(
    *,
    bars_index: dict[str, dict[str, Any]],
    in_universe_set: set[tuple[int, str]],
    regime_by_hour: dict[int, bool],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: HourlyLongConfig,
    costs: CostConfig,
    start_ms: int | None, end_ms: int | None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    # Build a master list of (ts, symbol, idx) events to iterate.
    # For efficiency: iterate each symbol's bars in time order. Per bar, check
    # if a signal fires. Maintain global open positions and cooldowns.
    stats = {"signals": 0, "skipped_capacity": 0, "skipped_cooldown": 0,
             "skipped_already_held": 0, "exits_stop": 0, "exits_take_profit": 0, "exits_time": 0}
    notional_weight = config.gross_exposure / max(config.max_concurrent_positions, 1)
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier
    cooldown_until: dict[str, int] = {}
    open_positions: dict[str, dict[str, Any]] = {}
    trade_rows: list[dict[str, Any]] = []

    # Gather all candidate signal bars across symbols, sorted by time
    signal_events = []  # (ts_ms, symbol, idx_in_symbol_bars)
    for symbol, arr in bars_index.items():
        if symbol == config.regime_symbol:
            continue  # skip BTC for trading (it's the regime, not the asset)
        ends = arr["bar_end_ts_ms"]
        bar_return = arr["bar_return"]
        close_location = arr["close_location"]
        turnover = arr["turnover_quote"]
        vol_threshold = arr["volume_threshold"]
        day_end = arr["day_end_for_membership"]
        n = len(ends)
        for i in range(config.bar_volume_lookback_hours, n):
            ts = int(ends[i])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts >= end_ms:
                continue
            # regime gate
            if not regime_by_hour.get(ts, False):
                continue
            # universe membership
            if (int(day_end[i]), symbol) not in in_universe_set:
                continue
            r = float(bar_return[i]) if not np.isnan(bar_return[i]) else float("nan")
            if not np.isfinite(r) or r < config.bar_min_return:
                continue
            cl = float(close_location[i])
            if cl < config.bar_close_location_min:
                continue
            tv = float(turnover[i])
            vt = float(vol_threshold[i]) if not np.isnan(vol_threshold[i]) else float("nan")
            if not np.isfinite(vt) or tv < vt:
                continue
            signal_events.append((ts, symbol, i))

    signal_events.sort(key=lambda e: e[0])
    stats["signals"] = len(signal_events)

    # Single-pass simulation: at each timestamp, first check exits, then entries.
    # Build per-symbol time-sorted bar arrays for exit checks.
    # For each event, process exits up to its timestamp, then try to enter.
    # Exits check the symbol's own bars between previous-checked-ts and current event ts.

    def process_exits_for_open(up_to_ts: int):
        for symbol in list(open_positions.keys()):
            pos = open_positions[symbol]
            arr = bars_index.get(symbol)
            if arr is None:
                continue
            ends = arr["ends"]
            entry_idx = pos["entry_bar_idx"]
            # iterate bars after entry up to up_to_ts
            i = entry_idx + 1
            n = len(ends)
            while i < n and ends[i] <= up_to_ts:
                bar_high = float(arr["high"][i])
                bar_low = float(arr["low"][i])
                bar_close = float(arr["close"][i])
                bar_end_ts = int(ends[i])
                # stop
                if bar_low <= pos["stop_price"]:
                    trade = _finalize_trade(pos, exit_ts_ms=bar_end_ts, exit_price=pos["stop_price"],
                                            reason="stop_loss", notional_weight=notional_weight,
                                            round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup)
                    trade_rows.append(trade)
                    cooldown_until[symbol] = bar_end_ts + config.cooldown_hours * MS_PER_HOUR
                    stats["exits_stop"] += 1
                    del open_positions[symbol]
                    break
                if bar_high >= pos["take_profit_price"]:
                    trade = _finalize_trade(pos, exit_ts_ms=bar_end_ts, exit_price=pos["take_profit_price"],
                                            reason="take_profit", notional_weight=notional_weight,
                                            round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup)
                    trade_rows.append(trade)
                    cooldown_until[symbol] = bar_end_ts + config.cooldown_hours * MS_PER_HOUR
                    stats["exits_take_profit"] += 1
                    del open_positions[symbol]
                    break
                if bar_end_ts - pos["entry_ts_ms"] >= config.max_hold_hours * MS_PER_HOUR:
                    trade = _finalize_trade(pos, exit_ts_ms=bar_end_ts, exit_price=bar_close,
                                            reason="time_stop", notional_weight=notional_weight,
                                            round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup)
                    trade_rows.append(trade)
                    cooldown_until[symbol] = bar_end_ts + config.cooldown_hours * MS_PER_HOUR
                    stats["exits_time"] += 1
                    del open_positions[symbol]
                    break
                i += 1
            else:
                # didn't exit; update entry_bar_idx for next time
                pos["entry_bar_idx"] = i - 1
                continue

    for ts, symbol, signal_idx in signal_events:
        # process exits up to this ts
        process_exits_for_open(ts)
        # try entry
        if symbol in open_positions:
            stats["skipped_already_held"] += 1
            continue
        if cooldown_until.get(symbol, 0) > ts:
            stats["skipped_cooldown"] += 1
            continue
        if len(open_positions) >= config.max_concurrent_positions:
            stats["skipped_capacity"] += 1
            continue
        # Entry at next bar (signal_idx + entry_delay_hours)
        bars = bars_index[symbol]
        entry_bar_idx = signal_idx + config.entry_delay_hours
        if entry_bar_idx >= len(bars["close"]):
            continue
        entry_price = float(bars["close"][entry_bar_idx])
        entry_ts = int(bars["bar_end_ts_ms"][entry_bar_idx])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        stop_price = entry_price * (1.0 - config.stop_pct)
        take_profit_price = entry_price * (1.0 + config.take_profit_pct)
        open_positions[symbol] = {
            "symbol": symbol,
            "entry_signal_ts_ms": ts,
            "entry_ts_ms": entry_ts,
            "entry_bar_idx": entry_bar_idx,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "position_weight": 1.0,
            "basket_id": f"hourly-{_iso_date(ts)}-{symbol}-{ts}",
        }

    # Force-close at end
    if open_positions:
        for symbol, pos in list(open_positions.items()):
            bars = bars_index.get(symbol)
            if bars is None or len(bars["close"]) == 0:
                continue
            exit_price = float(bars["close"][-1])
            exit_ts = int(bars["bar_end_ts_ms"][-1])
            trade = _finalize_trade(pos, exit_ts_ms=exit_ts, exit_price=exit_price,
                                    reason="data_end", notional_weight=notional_weight,
                                    round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup)
            trade_rows.append(trade)
            stats["exits_time"] += 1

    trades = pl.DataFrame(trade_rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"]) if trade_rows else _empty_trades()
    return trades, stats


def _finalize_trade(pos, *, exit_ts_ms, exit_price, reason, notional_weight, round_trip_cost_bps, funding_lookup):
    side = "long"
    entry_price = float(pos["entry_price"])
    gross_trade_return = (exit_price / entry_price) - 1.0
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup, symbol=pos["symbol"], side=side,
        entry_ts_ms=int(pos["entry_ts_ms"]), exit_ts_ms=int(exit_ts_ms),
    )
    effective_weight = abs(notional_weight * float(pos["position_weight"]))
    funding_return = effective_weight * raw_funding_return
    cost_return = -effective_weight * round_trip_cost_bps / 10_000.0
    gross_return = effective_weight * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    return {
        "trade_id": f"{pos['basket_id']}-l-{pos['symbol']}",
        "basket_id": pos["basket_id"],
        "entry_signal_ts_ms": int(pos["entry_signal_ts_ms"]),
        "entry_ts_ms": int(pos["entry_ts_ms"]),
        "exit_ts_ms": int(exit_ts_ms),
        "entry_date": _iso_date(int(pos["entry_ts_ms"])),
        "exit_date": _iso_date(int(exit_ts_ms)),
        "exit_month": _iso_month(int(exit_ts_ms)),
        "symbol": pos["symbol"], "side": side, "score": 0.0, "rank": 0,
        "entry_price": entry_price, "exit_price": float(exit_price),
        "exit_reason": reason, "planned_exit_ts_ms": int(exit_ts_ms),
        "stop_price": float(pos["stop_price"]), "take_profit_price": float(pos["take_profit_price"]),
        "notional_weight": effective_weight, "position_weight": float(pos["position_weight"]),
        "gross_trade_return": gross_trade_return, "gross_return": gross_return,
        "cost_return": cost_return, "funding_return": funding_return,
        "funding_mode": funding_mode, "funding_event_count": int(funding_event_count),
        "net_return": net_return, "mae": 0.0, "mfe": 0.0,
        "bars_held": int(round((int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR)),
        "hold_hours": (int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR,
        "actual_entry_delay_hours": (int(pos["entry_ts_ms"]) - int(pos["entry_signal_ts_ms"])) / MS_PER_HOUR,
    }


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame({k: pl.Series([], dtype=pl.Float64 if k.endswith(("_return", "_price", "_weight", "delay_hours", "hold_hours")) or k=="score" or k=="mae" or k=="mfe"
                                                    else pl.Int64 if k.endswith("_ts_ms") or k=="rank" or k=="funding_event_count" or k=="bars_held"
                                                    else pl.String)
                          for k in ["trade_id","basket_id","entry_signal_ts_ms","entry_ts_ms","exit_ts_ms","entry_date","exit_date","exit_month","symbol","side","score","rank","entry_price","exit_price","exit_reason","planned_exit_ts_ms","stop_price","take_profit_price","notional_weight","position_weight","gross_trade_return","gross_return","cost_return","funding_return","funding_mode","funding_event_count","net_return","mae","mfe","bars_held","hold_hours","actual_entry_delay_hours"]})


def _split_rows(baskets: pl.DataFrame, *, config: TradeLifecycleConfig) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in SPLITS:
        start_ms = date_ms(start)
        end_ms = date_ms(end)
        part = (baskets.filter((pl.col("entry_signal_ts_ms") >= start_ms) & (pl.col("entry_signal_ts_ms") < end_ms))
                if not baskets.is_empty() else baskets)
        if part.is_empty():
            rows.append({"name": name, "basket_count": 0, "total_return": 0.0, "sharpe_like": 0.0, "max_drawdown": 0.0})
            continue
        returns = np.asarray(part.sort("entry_signal_ts_ms")["basket_return"].to_list(), dtype=float)
        equity_curve = np.cumprod(1.0 + returns)
        peaks = np.maximum.accumulate(equity_curve)
        drawdowns = equity_curve / peaks - 1.0
        stdev = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
        mean = float(np.mean(returns)) if returns.size else 0.0
        # Use sqrt(N_baskets/N_years) for honest annualization
        n_years = max((part["exit_ts_ms"].max() - part["entry_signal_ts_ms"].min()) / 1000 / 86400 / 365.25, 0.01)
        annual_periods = returns.size / n_years
        rows.append({
            "name": name, "basket_count": int(returns.size),
            "total_return": float(equity_curve[-1] - 1.0),
            "sharpe_like": float(mean / stdev * math.sqrt(annual_periods)) if stdev > 1e-12 else 0.0,
            "max_drawdown": float(drawdowns.min()),
        })
    return rows


def _daily_aligned_sharpe(equity: pl.DataFrame) -> float:
    """Compute honest daily-aligned Sharpe from the equity series."""
    if equity.is_empty():
        return 0.0
    dates = sorted(set(equity["date"].to_list()))
    if len(dates) < 5:
        return 0.0
    eq_by_date = dict(zip(equity["date"].to_list(), equity["equity"].to_list()))
    from datetime import datetime, timedelta
    start = datetime.strptime(min(dates), "%Y-%m-%d")
    end = datetime.strptime(max(dates), "%Y-%m-%d")
    days = (end - start).days + 1
    daily_eq = []
    last = 1.0
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in eq_by_date:
            last = eq_by_date[d]
        daily_eq.append(last)
    daily_returns = np.diff(daily_eq) / np.array(daily_eq[:-1])
    if daily_returns.size < 5:
        return 0.0
    mean_d = float(daily_returns.mean())
    std_d = float(daily_returns.std(ddof=1))
    if std_d <= 0:
        return 0.0
    return float(mean_d / std_d * math.sqrt(365))


def format_hourly_long_report(metadata: dict[str, Any]) -> str:
    cfg = metadata.get("config", {})
    summary = metadata.get("summary", {})
    lifecycle = metadata.get("lifecycle", {})
    splits = metadata.get("splits", [])
    daily_sharpe = metadata.get("daily_sharpe", 0.0)
    lines = [
        "# Hourly Long Sleeve",
        "",
        f"Hourly intraday FOMO scanner. Looks for 1h bars with return ≥{cfg.get('bar_min_return',0):.0%}, "
        f"volume in top {(1-cfg.get('bar_volume_quantile',0))*100:.0f}% of trailing {cfg.get('bar_volume_lookback_hours',0)}h, "
        f"close_location ≥{cfg.get('bar_close_location_min',0):.0%}, BTC > {cfg.get('regime_sma_hours',0)//24}d SMA.",
        "",
        f"Stop {cfg.get('stop_pct',0):.0%} / TP {cfg.get('take_profit_pct',0):.0%} / hold ≤{cfg.get('max_hold_hours',0)}h / "
        f"cooldown {cfg.get('cooldown_hours',0)}h / max {cfg.get('max_concurrent_positions',0)} concurrent.",
        "",
        f"- Trades: {metadata.get('rows',{}).get('trades',0)}",
        f"- Signals fired: {lifecycle.get('signals',0)}",
        f"- Skipped: capacity={lifecycle.get('skipped_capacity',0)}, cooldown={lifecycle.get('skipped_cooldown',0)}, held={lifecycle.get('skipped_already_held',0)}",
        f"- Exits: stop={lifecycle.get('exits_stop',0)}, tp={lifecycle.get('exits_take_profit',0)}, time={lifecycle.get('exits_time',0)}",
        "",
        f"- **Sharpe-like (reported, basket-based):** {summary.get('sharpe_like',0.0):.2f}",
        f"- **Sharpe (DAILY-ALIGNED, honest):** {daily_sharpe:.2f}",
        f"- Total return: {pct(summary.get('total_return'))}",
        f"- Max drawdown: {pct(summary.get('max_drawdown'))}",
        f"- Win rate: {pct(summary.get('trade_win_rate'))}",
        f"- Profit factor: {summary.get('profit_factor',0.0):.2f}",
        f"- Funding mode: {summary.get('funding_mode','missing')}",
        "",
        "## Splits",
        "| Split | Baskets | Return | Sharpe | Max DD |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in splits:
        lines.append(f"| {r['name']} | {r['basket_count']} | {pct(r['total_return'])} | {r['sharpe_like']:.2f} | {pct(r['max_drawdown'])} |")
    return "\n".join(lines)
