from __future__ import annotations

import json
import math
import os
import shutil
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

from .config import CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig
from .storage import dataset_path, read_dataset, write_dataset


MS_PER_MINUTE = 60_000
MS_PER_DAY = 86_400_000
EPSILON = 1e-12

_GRID_FEATURES: pl.DataFrame | None = None
_GRID_DATA_ROOT: str | None = None
_GRID_COST_BPS: float | None = None


def run_daily_close_fade(
    data_root: str | Path,
    *,
    fade_config: DailyCloseFadeConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    config = fade_config or DailyCloseFadeConfig()
    costs = cost_config or CostConfig()
    features = build_daily_close_fade_features(data_root, config=config, signal_minutes=(config.signal_minute,))
    trades = backtest_daily_close_fade(
        data_root,
        features,
        config=config,
        round_trip_cost_bps=costs.base_entry_exit_cost_bps * config.cost_multiplier,
    )
    baskets = summarize_close_fade_baskets(trades)
    equity = build_close_fade_equity(baskets)
    summary = summarize_close_fade(trades, baskets, equity, config=config)
    payload = {
        "config": asdict(config),
        "summary": summary,
        "rows": {"features": features.height, "trades": trades.height, "baskets": baskets.height},
        "date_range": _date_range(features, "signal_ts_ms"),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "daily_close_fade_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_report.md").write_text(format_close_fade_report(payload), encoding="utf-8")
    if not trades.is_empty():
        trades.write_csv(output_dir / "daily_close_fade_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "daily_close_fade_baskets.csv")

    _replace_dataset(features, data_root, "daily_close_fade_features", partition_by=("date", "signal_minute"))
    _replace_dataset(trades, data_root, "daily_close_fade_trades", partition_by=("entry_date", "symbol"))
    _replace_dataset(baskets, data_root, "daily_close_fade_baskets", partition_by=("date",))
    return payload


def run_daily_close_fade_grid(
    data_root: str | Path,
    *,
    grid_config: DailyCloseFadeGridConfig | None = None,
    base_fade_config: DailyCloseFadeConfig | None = None,
    cost_config: CostConfig | None = None,
    max_workers: int | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    grid = grid_config or DailyCloseFadeGridConfig()
    base = base_fade_config or DailyCloseFadeConfig()
    costs = cost_config or CostConfig()
    features = build_daily_close_fade_features(data_root, config=base, signal_minutes=grid.signal_minutes)
    variants = list(iter_close_fade_grid_configs(grid, base))
    tasks = [
        (f"close-fade-{index:04d}", config, costs.base_entry_exit_cost_bps * config.cost_multiplier)
        for index, config in enumerate(variants, start=1)
    ]
    workers = _resolve_workers(max_workers, len(tasks))
    if workers <= 1:
        rows = [
            _evaluate_grid_variant(
                data_root,
                features,
                grid_id=grid_id,
                config=config,
                round_trip_cost_bps=round_trip_cost_bps,
            )
            for grid_id, config, round_trip_cost_bps in tasks
        ]
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_grid_worker,
            initargs=(str(Path(data_root)), features, costs.base_entry_exit_cost_bps),
        ) as executor:
            rows = list(executor.map(_evaluate_grid_variant_worker, tasks, chunksize=_grid_chunksize(len(tasks), workers)))

    results = pl.DataFrame(rows, infer_schema_length=None)
    if not results.is_empty():
        results = results.sort(["total_return", "sharpe_like"], descending=[True, True])
    payload = {
        "rows": results.height,
        "workers": workers,
        "date_range": _date_range(features, "signal_ts_ms"),
        "best_total_return": results.head(1).to_dicts()[0] if not results.is_empty() else {},
        "best_sharpe_like": results.sort("sharpe_like", descending=True).head(1).to_dicts()[0] if not results.is_empty() else {},
        "results": results.to_dicts(),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "daily_close_fade_grid_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_grid_report.md").write_text(format_close_fade_grid_report(payload), encoding="utf-8")
    if not results.is_empty():
        results.write_csv(output_dir / "daily_close_fade_grid_results.csv")
    _replace_dataset(results, data_root, "daily_close_fade_grid", partition_by=("score", "pump_filter"))
    return payload


def build_daily_close_fade_features(
    data_root: str | Path,
    *,
    config: DailyCloseFadeConfig,
    signal_minutes: tuple[int, ...],
) -> pl.DataFrame:
    lf = _scan_klines_1m(data_root)
    base = _minute_frame(lf)
    frames = [_signal_feature_frame(base, signal_minute).collect() for signal_minute in tuple(dict.fromkeys(signal_minutes))]
    if not frames:
        return pl.DataFrame()
    features = pl.concat(frames, how="diagonal_relaxed")
    if features.is_empty():
        return features

    daily_vol = _daily_realized_vol(base, config.vol_lookback_days)
    features = features.join(daily_vol, on=["symbol", "date"], how="left")
    features = _attach_instrument_age(data_root, features, min_age_days=config.min_age_days)
    median_vol = features.select(pl.col("realized_vol").median()).item()
    fallback_vol = float(median_vol) if median_vol is not None and median_vol > EPSILON else 0.03

    exclude_symbols = tuple(symbol.upper() for symbol in config.exclude_symbols)
    features = (
        features.with_columns(
            [
                pl.col("realized_vol").fill_null(fallback_vol).clip(lower_bound=0.002).alias("realized_vol"),
                (pl.col("day_return") / pl.col("realized_vol").fill_null(fallback_vol).clip(lower_bound=0.002)).alias(
                    "vol_adjusted_day_return"
                ),
                pl.when(pl.col("day_volume_base") > 0.0)
                .then(pl.col("day_turnover") / pl.col("day_volume_base"))
                .otherwise(None)
                .alias("intraday_vwap"),
                (pl.col("bar_count") / (pl.col("signal_minute") + 1)).alias("bar_coverage"),
            ]
        )
        .with_columns(
            [
                (pl.col("signal_close") / pl.col("intraday_vwap") - 1.0).fill_null(0.0).alias("vwap_extension"),
                (pl.col("last_60m_turnover") / (pl.col("day_turnover") / ((pl.col("signal_minute") + 1) / 60.0)).clip(EPSILON))
                .fill_null(0.0)
                .alias("late_volume_ratio"),
                (pl.col("signal_close") >= pl.col("day_high") * 0.995).alias("fresh_day_high"),
                pl.col("symbol").is_in(exclude_symbols).alias("excluded_symbol"),
            ]
        )
        .with_columns(
            [
                (
                    (pl.col("day_return") >= 0.03).cast(pl.Int8)
                    + (pl.col("vol_adjusted_day_return") >= 1.5).cast(pl.Int8)
                    + (pl.col("late_volume_ratio") >= 1.5).cast(pl.Int8)
                    + (pl.col("vwap_extension") >= 0.015).cast(pl.Int8)
                    + (pl.col("last_60m_return") >= 0.01).fill_null(False).cast(pl.Int8)
                    + pl.col("fresh_day_high").cast(pl.Int8)
                ).alias("pump_score")
            ]
        )
        .with_columns((pl.col("pump_score") >= 3).alias("pump_like"))
        .with_columns(
            (
                (~pl.col("excluded_symbol"))
                & (pl.col("age_days").fill_null(-1.0) >= float(config.min_age_days))
                & (pl.col("day_turnover") >= config.min_day_turnover)
                & (pl.col("last_60m_turnover") >= config.min_last_60m_turnover)
                & (pl.col("bar_coverage") >= 0.95)
            ).alias("eligible")
        )
        .sort(["signal_ts_ms", "symbol"])
    )
    return features


def backtest_daily_close_fade(
    data_root: str | Path,
    features: pl.DataFrame,
    *,
    config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
) -> pl.DataFrame:
    selected = select_close_fade_candidates(features, config)
    if selected.is_empty():
        return pl.DataFrame()
    partition_cache: dict[tuple[str, str], pl.DataFrame] = {}
    selected_counts = selected.group_by("signal_ts_ms").len(name="selected_count")
    selected = selected.join(selected_counts, on="signal_ts_ms", how="left")
    rows = []
    for row in selected.to_dicts():
        trade = _simulate_short_trade(
            data_root,
            row,
            config=config,
            round_trip_cost_bps=round_trip_cost_bps,
            partition_cache=partition_cache,
        )
        if trade:
            rows.append(trade)
    return pl.DataFrame(rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"]) if rows else pl.DataFrame()


def select_close_fade_candidates(features: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    if features.is_empty():
        return features
    if config.score not in features.columns:
        raise ValueError(f"Unknown daily close fade score: {config.score}")
    df = features.filter((pl.col("eligible")) & (pl.col("signal_minute") == config.signal_minute))
    if config.pump_filter == "pump":
        df = df.filter(pl.col("pump_like"))
    elif config.pump_filter == "non_pump":
        df = df.filter(~pl.col("pump_like"))
    elif config.pump_filter != "all":
        raise ValueError(f"Unknown pump_filter: {config.pump_filter}")
    if df.is_empty():
        return df
    return (
        df.with_columns(pl.col(config.score).rank("ordinal", descending=True).over("signal_ts_ms").alias("entry_rank"))
        .filter(pl.col("entry_rank") <= config.top_n)
        .with_columns(pl.len().over("signal_ts_ms").alias("candidate_count"))
        .filter(pl.col("candidate_count") >= config.min_symbols)
        .sort(["signal_ts_ms", "entry_rank"])
    )


def summarize_close_fade_baskets(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    baskets = (
        trades.group_by(["basket_id", "signal_ts_ms", "date", "signal_minute"], maintain_order=True)
        .agg(
            [
                pl.len().alias("trade_count"),
                pl.col("weighted_net_return").sum().alias("basket_return"),
                pl.col("weighted_gross_return").sum().alias("basket_gross_return"),
                pl.col("weighted_cost_return").sum().alias("basket_cost_return"),
                pl.col("net_return").mean().alias("avg_trade_return"),
                pl.col("mae").min().alias("worst_mae"),
                pl.col("mfe").max().alias("best_mfe"),
            ]
        )
        .sort("signal_ts_ms")
    )
    return baskets


def build_close_fade_equity(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame()
    equity = (
        baskets.sort("signal_ts_ms")
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )
    return equity


def summarize_close_fade(
    trades: pl.DataFrame,
    baskets: pl.DataFrame,
    equity: pl.DataFrame,
    *,
    config: DailyCloseFadeConfig,
) -> dict[str, Any]:
    if trades.is_empty() or baskets.is_empty() or equity.is_empty():
        return {
            "total_return": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
            "trade_count": 0,
            "basket_count": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
        }
    basket_returns = baskets["basket_return"].to_list()
    trade_returns = trades["net_return"].to_list()
    mean_return = statistics.fmean(basket_returns)
    stdev = statistics.stdev(basket_returns) if len(basket_returns) > 1 else 0.0
    wins = [item for item in trade_returns if item > 0.0]
    losses = [item for item in trade_returns if item < 0.0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf") if wins else 0.0
    summary = {
        "total_return": float(equity["equity"].tail(1).item() - 1.0),
        "sharpe_like": float((mean_return / stdev) * math.sqrt(365.0)) if stdev > EPSILON else 0.0,
        "max_drawdown": float(equity["drawdown"].min()),
        "trade_count": trades.height,
        "basket_count": baskets.height,
        "win_rate": float(len(wins) / len(trade_returns)) if trade_returns else 0.0,
        "profit_factor": float(profit_factor),
        "avg_basket_return": float(mean_return),
        "avg_trade_return": float(statistics.fmean(trade_returns)) if trade_returns else 0.0,
        "avg_trades_per_basket": float(statistics.fmean(baskets["trade_count"].to_list())),
        "signal_minute": config.signal_minute,
        "top_n": config.top_n,
        "hold_minutes": config.hold_minutes,
        "score": config.score,
        "pump_filter": config.pump_filter,
        "stop_loss_pct": config.stop_loss_pct,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_activation_pct": config.trailing_activation_pct,
        "cost_multiplier": config.cost_multiplier,
    }
    for reason, count in trades.group_by("exit_reason").len().iter_rows():
        summary[f"exit_{reason}"] = int(count)
    return summary


def iter_close_fade_grid_configs(
    grid: DailyCloseFadeGridConfig,
    base: DailyCloseFadeConfig | None = None,
) -> list[DailyCloseFadeConfig]:
    base_config = base or DailyCloseFadeConfig()
    configs = []
    seen = set()
    for signal_minute, top_n, hold_minutes, score, pump_filter, stop_loss_pct, trailing_stop_pct, activation, cost_multiplier in product(
        grid.signal_minutes,
        grid.top_ns,
        grid.hold_minutes,
        grid.scores,
        grid.pump_filters,
        grid.stop_loss_pcts,
        grid.trailing_stop_pcts,
        grid.trailing_activation_pcts,
        grid.cost_multipliers,
    ):
        normalized_activation = 0.0 if trailing_stop_pct <= 0.0 else activation
        key = (
            signal_minute,
            top_n,
            hold_minutes,
            score,
            pump_filter,
            stop_loss_pct,
            trailing_stop_pct,
            normalized_activation,
            cost_multiplier,
        )
        if key in seen:
            continue
        seen.add(key)
        configs.append(
            replace(
                base_config,
                signal_minute=signal_minute,
                top_n=top_n,
                hold_minutes=hold_minutes,
                score=score,
                pump_filter=pump_filter,
                stop_loss_pct=stop_loss_pct,
                trailing_stop_pct=trailing_stop_pct,
                trailing_activation_pct=normalized_activation,
                cost_multiplier=cost_multiplier,
            )
        )
    return configs


def format_close_fade_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    config = payload["config"]
    lines = [
        "# Daily Close Fade Report",
        "",
        f"Rows: features={payload['rows']['features']} trades={payload['rows']['trades']} baskets={payload['rows']['baskets']}",
        f"Date range: {payload['date_range'].get('start')} to {payload['date_range'].get('end')}",
        "",
        "## Config",
        "",
        f"- signal: {_format_signal_minute(config['signal_minute'])} UTC",
        f"- top_n: {config['top_n']}",
        f"- hold_minutes: {config['hold_minutes']}",
        f"- score: {config['score']}",
        f"- pump_filter: {config['pump_filter']}",
        f"- min_age_days: {config['min_age_days']}",
        f"- stop_loss_pct: {config['stop_loss_pct']:.2%}",
        f"- trailing_stop_pct: {config['trailing_stop_pct']:.2%}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total return | {summary.get('total_return', 0.0):.2%} |",
        f"| Sharpe-like | {summary.get('sharpe_like', 0.0):.2f} |",
        f"| Max drawdown | {summary.get('max_drawdown', 0.0):.2%} |",
        f"| Trade count | {summary.get('trade_count', 0)} |",
        f"| Basket count | {summary.get('basket_count', 0)} |",
        f"| Win rate | {summary.get('win_rate', 0.0):.2%} |",
        f"| Profit factor | {summary.get('profit_factor', 0.0):.2f} |",
        f"| Avg basket return | {summary.get('avg_basket_return', 0.0):.4%} |",
        "",
    ]
    return "\n".join(lines)


def format_close_fade_grid_report(payload: dict[str, Any]) -> str:
    rows = payload.get("results", [])
    lines = [
        "# Daily Close Fade Grid",
        "",
        f"Rows: {payload.get('rows', 0)}",
        f"Workers: {payload.get('workers', 0)}",
        f"Date range: {payload.get('date_range', {}).get('start')} to {payload.get('date_range', {}).get('end')}",
        "",
        "## Best By Total Return",
        "",
    ]
    best = payload.get("best_total_return", {})
    if best:
        lines.extend(_format_grid_row(best))
    lines.extend(
        [
            "",
            "## Top 25",
            "",
            "| Rank | Return | Sharpe | Max DD | Signal | Top N | Hold | Score | Pump | Stop | Trail | Cost | Trades | Win |",
            "|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(rows[:25], start=1):
        lines.append(
            f"| {index} | {row.get('total_return', 0.0):.2%} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{row.get('max_drawdown', 0.0):.2%} | {_format_signal_minute(row.get('signal_minute', 0))} | "
            f"{row.get('top_n')} | {row.get('hold_minutes')} | {row.get('score')} | {row.get('pump_filter')} | "
            f"{row.get('stop_loss_pct', 0.0):.1%} | {row.get('trailing_stop_pct', 0.0):.1%} | "
            f"{row.get('cost_multiplier', 1.0):.1f}x | {row.get('trade_count', 0)} | {row.get('win_rate', 0.0):.1%} |"
        )
    lines.append("")
    return "\n".join(lines)


def _signal_feature_frame(base: pl.LazyFrame, signal_minute: int) -> pl.LazyFrame:
    subset = base.filter(pl.col("minute_of_day") <= signal_minute).sort(["symbol", "date", "ts_ms"])
    avg_hours = max((signal_minute + 1) / 60.0, 1.0)
    return (
        subset.group_by(["symbol", "date"], maintain_order=True)
        .agg(
            [
                pl.col("ts_ms").last().alias("signal_ts_ms"),
                pl.col("open").first().alias("day_open"),
                pl.col("close").last().alias("signal_close"),
                pl.col("high").max().alias("day_high"),
                pl.col("low").min().alias("day_low"),
                pl.col("turnover_quote").sum().alias("day_turnover"),
                pl.col("volume_base").sum().alias("day_volume_base"),
                pl.len().alias("bar_count"),
                pl.col("turnover_quote").filter(pl.col("minute_of_day") > signal_minute - 60).sum().alias("last_60m_turnover"),
                pl.col("close").filter(pl.col("minute_of_day") <= signal_minute - 15).last().alias("close_15m_ago"),
                pl.col("close").filter(pl.col("minute_of_day") <= signal_minute - 60).last().alias("close_60m_ago"),
            ]
        )
        .with_columns(
            [
                pl.lit(signal_minute).alias("signal_minute"),
                (pl.col("signal_close") / pl.col("day_open") - 1.0).alias("day_return"),
                (pl.col("signal_close").log() - pl.col("day_open").log()).alias("day_log_return"),
                (pl.col("signal_close") / pl.col("close_15m_ago") - 1.0).alias("last_15m_return"),
                (pl.col("signal_close") / pl.col("close_60m_ago") - 1.0).alias("last_60m_return"),
                (pl.col("day_turnover") / avg_hours).alias("avg_hourly_turnover_so_far"),
            ]
        )
    )


def _daily_realized_vol(base: pl.LazyFrame, lookback_days: int) -> pl.DataFrame:
    daily = (
        base.sort(["symbol", "date", "ts_ms"])
        .group_by(["symbol", "date"], maintain_order=True)
        .agg(pl.col("close").last().alias("day_close"))
        .collect()
        .sort(["symbol", "date"])
    )
    if daily.is_empty():
        return daily
    return (
        daily.with_columns((pl.col("day_close").log() - pl.col("day_close").shift(1).over("symbol").log()).alias("daily_log_ret"))
        .with_columns(
            pl.col("daily_log_ret")
            .rolling_std(window_size=max(lookback_days, 2), min_samples=2)
            .over("symbol")
            .alias("realized_vol_raw")
        )
        .with_columns(pl.col("realized_vol_raw").shift(1).over("symbol").alias("realized_vol"))
        .select(["symbol", "date", "realized_vol"])
    )


def _scan_klines_1m(data_root: str | Path) -> pl.LazyFrame:
    path = dataset_path(data_root, "klines_1m")
    files = sorted(path.glob("**/*.parquet"))
    if not files:
        raise RuntimeError("klines_1m is empty; run download-data with --datasets instruments,klines_1m first")
    return pl.scan_parquet([str(file) for file in files])


def _minute_frame(lf: pl.LazyFrame) -> pl.LazyFrame:
    dt = pl.from_epoch(pl.col("ts_ms"), time_unit="ms")
    return lf.with_columns(
        [
            dt.dt.strftime("%Y-%m-%d").alias("date"),
            (dt.dt.hour().cast(pl.Int16) * 60 + dt.dt.minute().cast(pl.Int16)).alias("minute_of_day"),
        ]
    )


def _attach_instrument_age(data_root: str | Path, features: pl.DataFrame, *, min_age_days: int) -> pl.DataFrame:
    instruments = read_dataset(data_root, "instruments")
    if instruments.is_empty() and min_age_days > 0:
        raise RuntimeError("instruments is empty; daily-close-fade needs launch_time_ms for the 10-day age filter")
    if instruments.is_empty():
        return features.with_columns(pl.lit(None).cast(pl.Float64).alias("age_days"))
    latest = (
        instruments.sort(["symbol", "ts_ms"])
        .unique(subset=["symbol"], keep="last")
        .select(["symbol", "launch_time_ms"])
    )
    return features.join(latest, on="symbol", how="left").with_columns(
        ((pl.col("signal_ts_ms") - pl.col("launch_time_ms")) / MS_PER_DAY).alias("age_days")
    )


def _simulate_short_trade(
    data_root: str | Path,
    row: dict[str, Any],
    *,
    config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
    partition_cache: dict[tuple[str, str], pl.DataFrame],
) -> dict[str, Any] | None:
    symbol = str(row["symbol"])
    signal_ts_ms = int(row["signal_ts_ms"])
    entry_ts_ms = signal_ts_ms + config.entry_delay_minutes * MS_PER_MINUTE
    target_exit_ts_ms = entry_ts_ms + config.hold_minutes * MS_PER_MINUTE
    window = _load_trade_window(data_root, symbol, entry_ts_ms, target_exit_ts_ms, partition_cache)
    if window.is_empty():
        return None
    entry_candidates = window.filter(pl.col("ts_ms") >= entry_ts_ms).sort("ts_ms")
    if entry_candidates.is_empty():
        return None
    entry_bar = entry_candidates.row(0, named=True)
    entry_price = float(entry_bar["open"])
    bars = window.filter(pl.col("ts_ms") >= int(entry_bar["ts_ms"])).sort("ts_ms").to_dicts()
    if not bars:
        return None

    exit_ts_ms = int(bars[-1]["ts_ms"])
    exit_price = float(bars[-1]["close"])
    exit_reason = "max_hold" if exit_ts_ms >= target_exit_ts_ms else "data_end"
    stop_active_ts_ms = entry_ts_ms + config.stop_delay_minutes * MS_PER_MINUTE
    hard_stop_price = entry_price * (1.0 + config.stop_loss_pct) if config.stop_loss_pct > 0.0 else None
    trailing_active = False
    best_price = entry_price
    max_favorable = 0.0
    max_adverse = 0.0

    for bar in bars:
        ts_ms = int(bar["ts_ms"])
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        max_favorable = max(max_favorable, entry_price / max(low, EPSILON) - 1.0)
        max_adverse = min(max_adverse, entry_price / max(high, EPSILON) - 1.0)

        if ts_ms >= stop_active_ts_ms and hard_stop_price is not None and high >= hard_stop_price:
            exit_ts_ms = ts_ms
            exit_price = hard_stop_price
            exit_reason = "stop_loss"
            break

        if config.trailing_stop_pct > 0.0 and ts_ms >= stop_active_ts_ms:
            if trailing_active:
                trailing_stop = best_price * (1.0 + config.trailing_stop_pct)
                if high >= trailing_stop:
                    exit_ts_ms = ts_ms
                    exit_price = trailing_stop
                    exit_reason = "trailing_stop"
                    break
            if low < best_price:
                best_price = low
            if not trailing_active and (entry_price / max(best_price, EPSILON) - 1.0) >= config.trailing_activation_pct:
                trailing_active = True

        if ts_ms >= target_exit_ts_ms:
            exit_ts_ms = ts_ms
            exit_price = close
            exit_reason = "max_hold"
            break

    gross_return = entry_price / max(exit_price, EPSILON) - 1.0
    cost_return = round_trip_cost_bps / 10_000.0
    net_return = gross_return - cost_return
    selected_count = max(int(row.get("selected_count") or config.top_n), 1)
    weight = config.gross_exposure / selected_count
    entry_dt = _dt_from_ms(entry_ts_ms)
    exit_dt = _dt_from_ms(exit_ts_ms)
    signal_dt = _dt_from_ms(signal_ts_ms)
    trade_id = f"{signal_ts_ms}-{symbol}-{int(row.get('entry_rank', 0))}"
    return {
        "trade_id": trade_id,
        "basket_id": f"{signal_ts_ms}-{config.signal_minute}",
        "symbol": symbol,
        "side": "short",
        "date": str(row["date"]),
        "entry_date": entry_dt.date().isoformat(),
        "exit_date": exit_dt.date().isoformat(),
        "signal_ts_ms": signal_ts_ms,
        "signal_time": signal_dt.isoformat(),
        "signal_minute": config.signal_minute,
        "entry_ts_ms": entry_ts_ms,
        "entry_time": entry_dt.isoformat(),
        "exit_ts_ms": exit_ts_ms,
        "exit_time": exit_dt.isoformat(),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "hold_minutes": (exit_ts_ms - entry_ts_ms) / MS_PER_MINUTE,
        "entry_rank": int(row.get("entry_rank") or 0),
        "score": float(row.get(config.score) or 0.0),
        "score_name": config.score,
        "day_return": float(row.get("day_return") or 0.0),
        "vol_adjusted_day_return": float(row.get("vol_adjusted_day_return") or 0.0),
        "pump_score": int(row.get("pump_score") or 0),
        "pump_like": bool(row.get("pump_like")),
        "late_volume_ratio": float(row.get("late_volume_ratio") or 0.0),
        "vwap_extension": float(row.get("vwap_extension") or 0.0),
        "age_days": float(row.get("age_days") or 0.0),
        "weight": weight,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "net_return": net_return,
        "weighted_gross_return": gross_return * weight,
        "weighted_cost_return": cost_return * weight,
        "weighted_net_return": net_return * weight,
        "mae": max_adverse,
        "mfe": max_favorable,
        "stop_loss_pct": config.stop_loss_pct,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_activation_pct": config.trailing_activation_pct,
        "cost_multiplier": config.cost_multiplier,
        "pump_filter": config.pump_filter,
        "top_n": config.top_n,
    }


def _load_trade_window(
    data_root: str | Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
    partition_cache: dict[tuple[str, str], pl.DataFrame],
) -> pl.DataFrame:
    frames = []
    for date in _dates_for_window(start_ms, end_ms):
        key = (symbol, date)
        if key not in partition_cache:
            part = dataset_path(data_root, "klines_1m") / f"date={date}" / f"symbol={symbol}" / "part.parquet"
            partition_cache[key] = pl.read_parquet(part) if part.exists() else pl.DataFrame()
        if not partition_cache[key].is_empty():
            frames.append(partition_cache[key])
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed").filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") <= end_ms))


def _dates_for_window(start_ms: int, end_ms: int) -> list[str]:
    start = _dt_from_ms(start_ms).date()
    end = _dt_from_ms(end_ms).date()
    output = []
    current = start
    while current <= end:
        output.append(current.isoformat())
        current += timedelta(days=1)
    return output


def _evaluate_grid_variant(
    data_root: str | Path,
    features: pl.DataFrame,
    *,
    grid_id: str,
    config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
) -> dict[str, Any]:
    trades = backtest_daily_close_fade(data_root, features, config=config, round_trip_cost_bps=round_trip_cost_bps)
    baskets = summarize_close_fade_baskets(trades)
    equity = build_close_fade_equity(baskets)
    summary = summarize_close_fade(trades, baskets, equity, config=config)
    config_row = asdict(config)
    config_row["exclude_symbols"] = ",".join(config.exclude_symbols)
    return {
        "grid_id": grid_id,
        **config_row,
        "round_trip_cost_bps": round_trip_cost_bps,
        **summary,
    }


def _init_grid_worker(data_root: str, features: pl.DataFrame, base_cost_bps: float) -> None:
    global _GRID_DATA_ROOT, _GRID_FEATURES, _GRID_COST_BPS
    _GRID_DATA_ROOT = data_root
    _GRID_FEATURES = features
    _GRID_COST_BPS = base_cost_bps


def _evaluate_grid_variant_worker(task: tuple[str, DailyCloseFadeConfig, float]) -> dict[str, Any]:
    if _GRID_DATA_ROOT is None or _GRID_FEATURES is None:
        raise RuntimeError("daily close fade grid worker was not initialized")
    grid_id, config, round_trip_cost_bps = task
    return _evaluate_grid_variant(
        _GRID_DATA_ROOT,
        _GRID_FEATURES,
        grid_id=grid_id,
        config=config,
        round_trip_cost_bps=round_trip_cost_bps,
    )


def _resolve_workers(max_workers: int | None, task_count: int) -> int:
    if task_count <= 1:
        return 1
    if max_workers is not None and max_workers > 0:
        return min(max_workers, task_count)
    if max_workers == 1:
        return 1
    cpus = os.cpu_count() or 2
    return max(1, min(task_count, cpus - 1))


def _grid_chunksize(task_count: int, workers: int) -> int:
    return max(1, math.ceil(task_count / (workers * 4)))


def _replace_dataset(df: pl.DataFrame, data_root: str | Path, dataset: str, *, partition_by: tuple[str, ...]) -> None:
    path = dataset_path(data_root, dataset)
    if path.exists():
        shutil.rmtree(path)
    write_dataset(df, data_root, dataset, partition_by=partition_by, append=False)


def _format_grid_row(row: dict[str, Any]) -> list[str]:
    return [
        f"- Return: {row.get('total_return', 0.0):.2%}",
        f"- Sharpe-like: {row.get('sharpe_like', 0.0):.2f}",
        f"- Max drawdown: {row.get('max_drawdown', 0.0):.2%}",
        f"- Signal: {_format_signal_minute(row.get('signal_minute', 0))} UTC",
        f"- Top N: {row.get('top_n')}",
        f"- Hold: {row.get('hold_minutes')} minutes",
        f"- Score: {row.get('score')}",
        f"- Pump filter: {row.get('pump_filter')}",
    ]


def _format_signal_minute(value: int) -> str:
    hour = int(value) // 60
    minute = int(value) % 60
    return f"{hour:02d}:{minute:02d}"


def _date_range(df: pl.DataFrame, ts_col: str) -> dict[str, str | None]:
    if df.is_empty() or ts_col not in df.columns:
        return {"start": None, "end": None}
    return {
        "start": _dt_from_ms(int(df[ts_col].min())).isoformat(),
        "end": _dt_from_ms(int(df[ts_col].max())).isoformat(),
    }


def _dt_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)
