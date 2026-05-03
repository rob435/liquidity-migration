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
    _validate_close_fade_config(config)
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
    _validate_close_fade_config(base)
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


def run_daily_close_fade_sleeves(
    data_root: str | Path,
    *,
    fade_config: DailyCloseFadeConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    base = fade_config or DailyCloseFadeConfig()
    _validate_close_fade_config(base)
    costs = cost_config or CostConfig()
    sleeves = close_fade_sleeve_configs(base)
    features = build_daily_close_fade_features(
        data_root,
        config=base,
        signal_minutes=tuple(dict.fromkeys(config.signal_minute for _, config in sleeves)),
    )

    result_rows: list[dict[str, Any]] = []
    trade_frames: list[pl.DataFrame] = []
    basket_frames: list[pl.DataFrame] = []
    equity_frames: list[pl.DataFrame] = []
    for sleeve_name, config in sleeves:
        _validate_close_fade_config(config)
        round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier
        trades = backtest_daily_close_fade(
            data_root,
            features,
            config=config,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        baskets = summarize_close_fade_baskets(trades)
        equity = build_close_fade_equity(baskets)
        summary = summarize_close_fade(trades, baskets, equity, config=config)
        config_row = asdict(config)
        config_row["exclude_symbols"] = ",".join(config.exclude_symbols)
        result_rows.append(
            {
                "sleeve": sleeve_name,
                "round_trip_cost_bps": round_trip_cost_bps,
                **config_row,
                **summary,
            }
        )
        if not trades.is_empty():
            trade_frames.append(trades.with_columns(pl.lit(sleeve_name).alias("sleeve")))
        if not baskets.is_empty():
            basket_frames.append(baskets.with_columns(pl.lit(sleeve_name).alias("sleeve")))
        if not equity.is_empty():
            equity_frames.append(equity.with_columns(pl.lit(sleeve_name).alias("sleeve")))

    results = pl.DataFrame(result_rows, infer_schema_length=None)
    trades_all = _concat_frames(trade_frames)
    baskets_all = _concat_frames(basket_frames)
    equity_all = _concat_frames(equity_frames)
    if not results.is_empty():
        results = results.sort(["total_return", "sharpe_like"], descending=[True, True])
    payload = {
        "rows": {
            "features": features.height,
            "results": results.height,
            "trades": trades_all.height,
            "baskets": baskets_all.height,
        },
        "date_range": _date_range(features, "signal_ts_ms"),
        "results": results.to_dicts(),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "daily_close_fade_sleeves_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_sleeves_report.md").write_text(format_close_fade_sleeves_report(payload), encoding="utf-8")
    if not results.is_empty():
        results.write_csv(output_dir / "daily_close_fade_sleeves_results.csv")
    if not trades_all.is_empty():
        trades_all.write_csv(output_dir / "daily_close_fade_sleeves_trades.csv")
    if not baskets_all.is_empty():
        baskets_all.write_csv(output_dir / "daily_close_fade_sleeves_baskets.csv")
    _replace_dataset(results, data_root, "daily_close_fade_sleeves", partition_by=("sleeve",))
    _replace_dataset(trades_all, data_root, "daily_close_fade_sleeve_trades", partition_by=("sleeve", "entry_date", "symbol"))
    _replace_dataset(baskets_all, data_root, "daily_close_fade_sleeve_baskets", partition_by=("sleeve", "date"))
    _replace_dataset(equity_all, data_root, "daily_close_fade_sleeve_equity", partition_by=("sleeve", "date"))
    return payload


def close_fade_sleeve_configs(base: DailyCloseFadeConfig | None = None) -> tuple[tuple[str, DailyCloseFadeConfig], ...]:
    base_config = base or DailyCloseFadeConfig()
    major = replace(
        base_config,
        liquidity_rank_min=1,
        liquidity_rank_max=30,
        min_baseline_turnover=0.0,
        min_day_turnover=0.0,
        min_last_60m_turnover=0.0,
        max_position_weight=0.0,
        max_trade_notional_pct_of_day_turnover=0.0,
        max_trade_notional_pct_of_baseline_turnover=0.0,
        exclude_symbols=(),
    )
    core = replace(
        base_config,
        top_n=5,
        liquidity_rank_min=31,
        liquidity_rank_max=150,
        min_baseline_turnover=max(base_config.min_baseline_turnover, 0.0),
        min_day_turnover=max(base_config.min_day_turnover, 0.0),
        min_last_60m_turnover=max(base_config.min_last_60m_turnover, 0.0),
        max_position_weight=0.0,
        max_trade_notional_pct_of_day_turnover=0.0,
        max_trade_notional_pct_of_baseline_turnover=0.0,
    )
    microcap = replace(
        base_config,
        top_n=min(base_config.top_n, 3),
        gross_exposure=min(base_config.gross_exposure, 0.50),
        liquidity_rank_min=151,
        liquidity_rank_max=0,
        min_baseline_turnover=max(base_config.min_baseline_turnover, 250_000.0),
        min_day_turnover=max(base_config.min_day_turnover, 750_000.0),
        min_last_60m_turnover=max(base_config.min_last_60m_turnover, 75_000.0),
        max_position_weight=base_config.max_position_weight or 0.20,
        max_trade_notional_pct_of_day_turnover=base_config.max_trade_notional_pct_of_day_turnover or 0.002,
        max_trade_notional_pct_of_baseline_turnover=(
            base_config.max_trade_notional_pct_of_baseline_turnover or 0.005
        ),
    )
    return (("major_control", major), ("core", core), ("microcap", microcap))


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
    baseline_liquidity = _baseline_liquidity_rank(base, config.liquidity_lookback_days)
    features = features.join(baseline_liquidity, on=["symbol", "date"], how="left")
    features = _attach_instrument_age(data_root, features, min_age_days=config.min_age_days)
    features = _attach_archive_membership(data_root, features, required=config.require_archive_membership)
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
                & (pl.col("bar_coverage") >= 0.95)
                & ((~pl.lit(config.require_archive_membership)) | pl.col("archive_tradable"))
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
    _validate_close_fade_config(config)
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
    if not rows:
        return pl.DataFrame()
    trades = pl.DataFrame(rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"])
    if config.basket_stop_loss_pct > 0.0:
        trades = apply_close_fade_basket_stop(
            data_root,
            trades,
            config=config,
            round_trip_cost_bps=round_trip_cost_bps,
            partition_cache=partition_cache,
        )
    return trades.sort(["entry_ts_ms", "symbol"])


def select_close_fade_candidates(features: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    if features.is_empty():
        return features
    if config.score not in features.columns:
        raise ValueError(f"Unknown daily close fade score: {config.score}")
    df = features.filter((pl.col("eligible")) & (pl.col("signal_minute") == config.signal_minute))
    df = df.filter(_candidate_liquidity_filter_expr(config))
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


def _validate_close_fade_config(config: DailyCloseFadeConfig) -> None:
    if config.top_n <= 0:
        raise ValueError("daily close fade top_n must be positive")
    if config.hold_minutes <= 0:
        raise ValueError("daily close fade hold_minutes must be positive")
    if config.entry_delay_minutes < 0:
        raise ValueError("daily close fade entry_delay_minutes must be non-negative")
    if config.gross_exposure <= 0.0:
        raise ValueError("daily close fade gross_exposure must be positive")
    if config.stop_loss_pct < 0.0:
        raise ValueError("daily close fade stop_loss_pct must be non-negative")
    if not 0.0 <= config.take_profit_pct < 1.0:
        raise ValueError("daily close fade take_profit_pct must be in [0, 1)")
    if config.basket_stop_loss_pct < 0.0:
        raise ValueError("daily close fade basket_stop_loss_pct must be non-negative")
    if config.trailing_stop_pct < 0.0:
        raise ValueError("daily close fade trailing_stop_pct must be non-negative")
    if config.trailing_activation_pct < 0.0:
        raise ValueError("daily close fade trailing_activation_pct must be non-negative")
    if config.liquidity_lookback_days <= 0:
        raise ValueError("daily close fade liquidity_lookback_days must be positive")
    if config.liquidity_rank_min <= 0:
        raise ValueError("daily close fade liquidity_rank_min must be positive")
    if config.liquidity_rank_max < 0:
        raise ValueError("daily close fade liquidity_rank_max must be non-negative")
    if config.liquidity_rank_max and config.liquidity_rank_max < config.liquidity_rank_min:
        raise ValueError("daily close fade liquidity_rank_max must be >= liquidity_rank_min")
    if config.min_baseline_turnover < 0.0:
        raise ValueError("daily close fade min_baseline_turnover must be non-negative")
    if config.account_equity <= 0.0:
        raise ValueError("daily close fade account_equity must be positive")
    if config.max_position_weight < 0.0:
        raise ValueError("daily close fade max_position_weight must be non-negative")
    if config.max_trade_notional_pct_of_day_turnover < 0.0:
        raise ValueError("daily close fade max_trade_notional_pct_of_day_turnover must be non-negative")
    if config.max_trade_notional_pct_of_baseline_turnover < 0.0:
        raise ValueError("daily close fade max_trade_notional_pct_of_baseline_turnover must be non-negative")
    if config.vol_trailing_stop_mult < 0.0 or config.vol_trailing_activation_mult < 0.0:
        raise ValueError("daily close fade vol trailing multipliers must be non-negative")
    if config.mfe_giveback_activation_pct < 0.0:
        raise ValueError("daily close fade mfe_giveback_activation_pct must be non-negative")
    if not 0.0 <= config.mfe_giveback_pct < 1.0:
        raise ValueError("daily close fade mfe_giveback_pct must be in [0, 1)")
    if not 0.0 <= config.vwap_reversion_pct <= 1.0:
        raise ValueError("daily close fade vwap_reversion_pct must be in [0, 1]")
    if config.stop_delay_minutes < 0:
        raise ValueError("daily close fade stop_delay_minutes must be non-negative")
    if config.cost_multiplier < 0.0:
        raise ValueError("daily close fade cost_multiplier must be non-negative")


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
                pl.col("target_weight").sum().alias("target_gross_exposure"),
                pl.col("weight").sum().alias("basket_gross_exposure"),
                pl.col("capacity_limited").sum().alias("capacity_limited_count"),
                pl.col("net_return").mean().alias("avg_trade_return"),
                pl.col("mae").min().alias("worst_mae"),
                pl.col("mfe").max().alias("best_mfe"),
            ]
        )
        .sort("signal_ts_ms")
    )
    return baskets


def apply_close_fade_basket_stop(
    data_root: str | Path,
    trades: pl.DataFrame,
    *,
    config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
    partition_cache: dict[tuple[str, str], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    if trades.is_empty() or config.basket_stop_loss_pct <= 0.0:
        return trades
    cache = partition_cache if partition_cache is not None else {}
    rows: list[dict[str, Any]] = []
    for basket_id in trades.select("basket_id").unique(maintain_order=True)["basket_id"].to_list():
        basket = trades.filter(pl.col("basket_id") == basket_id).sort(["entry_ts_ms", "symbol"])
        rows.extend(
            _apply_basket_stop_to_rows(
                data_root,
                basket.to_dicts(),
                config=config,
                round_trip_cost_bps=round_trip_cost_bps,
                partition_cache=cache,
            )
        )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else trades


def _apply_basket_stop_to_rows(
    data_root: str | Path,
    rows: list[dict[str, Any]],
    *,
    config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
    partition_cache: dict[tuple[str, str], pl.DataFrame],
) -> list[dict[str, Any]]:
    if not rows:
        return rows

    start_ms = min(int(row["entry_ts_ms"]) for row in rows)
    end_ms = max(int(row["exit_ts_ms"]) for row in rows)
    symbol_bars: dict[str, dict[int, dict[str, Any]]] = {}
    timestamps: set[int] = set()
    last_price: dict[str, float] = {}

    for row in rows:
        symbol = str(row["symbol"])
        entry_ts_ms = int(row["entry_ts_ms"])
        exit_ts_ms = int(row["exit_ts_ms"])
        window = _load_trade_window(data_root, symbol, entry_ts_ms, exit_ts_ms, partition_cache).sort("ts_ms")
        bars = {int(bar["ts_ms"]): bar for bar in window.to_dicts()}
        symbol_bars[symbol] = bars
        timestamps.update(ts for ts in bars if start_ms <= ts <= end_ms)
        last_price[symbol] = float(row["entry_price"])

    if not timestamps:
        return rows

    cost_return = round_trip_cost_bps / 10_000.0
    stop_ts_ms: int | None = None
    stop_prices: dict[str, float] = {}
    closed_contribution: dict[str, float] = {
        str(row["trade_id"]): float(row["weighted_net_return"])
        for row in rows
    }

    for ts_ms in sorted(timestamps):
        basket_mark_return = 0.0
        for row in rows:
            symbol = str(row["symbol"])
            bar = symbol_bars.get(symbol, {}).get(ts_ms)
            if bar is not None:
                last_price[symbol] = float(bar["close"])

            entry_ts_ms = int(row["entry_ts_ms"])
            exit_ts_ms = int(row["exit_ts_ms"])
            if ts_ms < entry_ts_ms:
                continue
            if ts_ms >= exit_ts_ms:
                basket_mark_return += closed_contribution[str(row["trade_id"])]
                continue

            gross_return = _short_return(float(row["entry_price"]), last_price[symbol])
            basket_mark_return += (gross_return - cost_return) * float(row["weight"])

        if basket_mark_return <= -config.basket_stop_loss_pct:
            stop_ts_ms = ts_ms
            stop_prices = dict(last_price)
            break

    if stop_ts_ms is None:
        return rows

    adjusted: list[dict[str, Any]] = []
    for row in rows:
        if int(row["exit_ts_ms"]) <= stop_ts_ms:
            adjusted.append(row)
            continue
        symbol = str(row["symbol"])
        exit_price = stop_prices.get(symbol, float(row["entry_price"]))
        adjusted.append(
            _rewrite_trade_exit(
                data_root,
                row,
                exit_ts_ms=stop_ts_ms,
                exit_price=exit_price,
                exit_reason="basket_stop",
                cost_return=cost_return,
                partition_cache=partition_cache,
            )
        )
    return adjusted


def _rewrite_trade_exit(
    data_root: str | Path,
    row: dict[str, Any],
    *,
    exit_ts_ms: int,
    exit_price: float,
    exit_reason: str,
    cost_return: float,
    partition_cache: dict[tuple[str, str], pl.DataFrame],
) -> dict[str, Any]:
    updated = dict(row)
    gross_return = _short_return(float(row["entry_price"]), exit_price)
    net_return = gross_return - cost_return
    weight = float(row["weight"])
    mae, mfe = _trade_excursion(
        data_root,
        str(row["symbol"]),
        entry_ts_ms=int(row["entry_ts_ms"]),
        exit_ts_ms=exit_ts_ms,
        entry_price=float(row["entry_price"]),
        partition_cache=partition_cache,
    )
    exit_dt = _dt_from_ms(exit_ts_ms)
    updated.update(
        {
            "exit_ts_ms": exit_ts_ms,
            "exit_time": exit_dt.isoformat(),
            "exit_date": exit_dt.date().isoformat(),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "hold_minutes": (exit_ts_ms - int(row["entry_ts_ms"])) / MS_PER_MINUTE,
            "gross_return": gross_return,
            "cost_return": cost_return,
            "net_return": net_return,
            "weighted_gross_return": gross_return * weight,
            "weighted_cost_return": cost_return * weight,
            "weighted_net_return": net_return * weight,
            "mae": mae,
            "mfe": mfe,
        }
    )
    return updated


def _trade_excursion(
    data_root: str | Path,
    symbol: str,
    *,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_price: float,
    partition_cache: dict[tuple[str, str], pl.DataFrame],
) -> tuple[float, float]:
    window = _load_trade_window(data_root, symbol, entry_ts_ms, exit_ts_ms, partition_cache).sort("ts_ms")
    max_favorable = 0.0
    max_adverse = 0.0
    for bar in window.to_dicts():
        max_favorable = max(max_favorable, _short_return(entry_price, float(bar["low"])))
        max_adverse = min(max_adverse, _short_return(entry_price, float(bar["high"])))
    return max_adverse, max_favorable


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
            "avg_basket_return": 0.0,
            "avg_trade_return": 0.0,
            "avg_trades_per_basket": 0.0,
            "avg_basket_gross_exposure": 0.0,
            "capacity_limited_trades": 0,
            "capacity_limited_rate": 0.0,
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
        "avg_basket_gross_exposure": float(statistics.fmean(baskets["basket_gross_exposure"].to_list()))
        if "basket_gross_exposure" in baskets.columns
        else config.gross_exposure,
        "capacity_limited_trades": int(trades["capacity_limited"].sum()) if "capacity_limited" in trades.columns else 0,
        "capacity_limited_rate": float(trades["capacity_limited"].sum() / trades.height)
        if "capacity_limited" in trades.columns and trades.height
        else 0.0,
        "signal_minute": config.signal_minute,
        "top_n": config.top_n,
        "hold_minutes": config.hold_minutes,
        "gross_exposure": config.gross_exposure,
        "score": config.score,
        "pump_filter": config.pump_filter,
        "liquidity_lookback_days": config.liquidity_lookback_days,
        "liquidity_rank_min": config.liquidity_rank_min,
        "liquidity_rank_max": config.liquidity_rank_max,
        "min_baseline_turnover": config.min_baseline_turnover,
        "account_equity": config.account_equity,
        "max_position_weight": config.max_position_weight,
        "max_trade_notional_pct_of_day_turnover": config.max_trade_notional_pct_of_day_turnover,
        "max_trade_notional_pct_of_baseline_turnover": config.max_trade_notional_pct_of_baseline_turnover,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "basket_stop_loss_pct": config.basket_stop_loss_pct,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_activation_pct": config.trailing_activation_pct,
        "vol_trailing_stop_mult": config.vol_trailing_stop_mult,
        "vol_trailing_activation_mult": config.vol_trailing_activation_mult,
        "mfe_giveback_activation_pct": config.mfe_giveback_activation_pct,
        "mfe_giveback_pct": config.mfe_giveback_pct,
        "vwap_reversion_pct": config.vwap_reversion_pct,
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
    for (
        signal_minute,
        top_n,
        hold_minutes,
        gross_exposure,
        score,
        pump_filter,
        stop_loss_pct,
        take_profit_pct,
        basket_stop_loss_pct,
        trailing_stop_pct,
        activation,
        vol_trailing_stop_mult,
        vol_trailing_activation_mult,
        mfe_giveback_activation_pct,
        mfe_giveback_pct,
        vwap_reversion_pct,
        liquidity_lookback_days,
        liquidity_rank_min,
        liquidity_rank_max,
        min_baseline_turnover,
        account_equity,
        max_position_weight,
        max_trade_notional_pct_of_day_turnover,
        max_trade_notional_pct_of_baseline_turnover,
        cost_multiplier,
    ) in product(
        grid.signal_minutes,
        grid.top_ns,
        grid.hold_minutes,
        grid.gross_exposures,
        grid.scores,
        grid.pump_filters,
        grid.stop_loss_pcts,
        grid.take_profit_pcts,
        grid.basket_stop_loss_pcts,
        grid.trailing_stop_pcts,
        grid.trailing_activation_pcts,
        grid.vol_trailing_stop_mults,
        grid.vol_trailing_activation_mults,
        grid.mfe_giveback_activation_pcts,
        grid.mfe_giveback_pcts,
        grid.vwap_reversion_pcts,
        grid.liquidity_lookback_days,
        grid.liquidity_rank_mins,
        grid.liquidity_rank_maxs,
        grid.min_baseline_turnovers,
        grid.account_equities,
        grid.max_position_weights,
        grid.max_trade_notional_pct_day_turnovers,
        grid.max_trade_notional_pct_baseline_turnovers,
        grid.cost_multipliers,
    ):
        normalized_activation = 0.0 if trailing_stop_pct <= 0.0 else activation
        normalized_vol_activation = 0.0 if vol_trailing_stop_mult <= 0.0 else vol_trailing_activation_mult
        normalized_mfe_activation = 0.0 if mfe_giveback_pct <= 0.0 else mfe_giveback_activation_pct
        if liquidity_rank_max > 0 and liquidity_rank_max < liquidity_rank_min:
            continue
        key = (
            signal_minute,
            top_n,
            hold_minutes,
            gross_exposure,
            score,
            pump_filter,
            stop_loss_pct,
            take_profit_pct,
            basket_stop_loss_pct,
            trailing_stop_pct,
            normalized_activation,
            vol_trailing_stop_mult,
            normalized_vol_activation,
            normalized_mfe_activation,
            mfe_giveback_pct,
            vwap_reversion_pct,
            liquidity_lookback_days,
            liquidity_rank_min,
            liquidity_rank_max,
            min_baseline_turnover,
            account_equity,
            max_position_weight,
            max_trade_notional_pct_of_day_turnover,
            max_trade_notional_pct_of_baseline_turnover,
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
                gross_exposure=gross_exposure,
                score=score,
                pump_filter=pump_filter,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                basket_stop_loss_pct=basket_stop_loss_pct,
                trailing_stop_pct=trailing_stop_pct,
                trailing_activation_pct=normalized_activation,
                vol_trailing_stop_mult=vol_trailing_stop_mult,
                vol_trailing_activation_mult=normalized_vol_activation,
                mfe_giveback_activation_pct=normalized_mfe_activation,
                mfe_giveback_pct=mfe_giveback_pct,
                vwap_reversion_pct=vwap_reversion_pct,
                liquidity_lookback_days=liquidity_lookback_days,
                liquidity_rank_min=liquidity_rank_min,
                liquidity_rank_max=liquidity_rank_max,
                min_baseline_turnover=min_baseline_turnover,
                account_equity=account_equity,
                max_position_weight=max_position_weight,
                max_trade_notional_pct_of_day_turnover=max_trade_notional_pct_of_day_turnover,
                max_trade_notional_pct_of_baseline_turnover=max_trade_notional_pct_of_baseline_turnover,
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
        f"- gross_exposure: {config.get('gross_exposure', 1.0):.2f}x",
        f"- score: {config['score']}",
        f"- pump_filter: {config['pump_filter']}",
        f"- min_age_days: {config['min_age_days']}",
        f"- baseline_liquidity: lookback={config.get('liquidity_lookback_days', 7)}d "
        f"rank={config.get('liquidity_rank_min', 1)}-"
        f"{config.get('liquidity_rank_max', 0) or 'unbounded'}",
        f"- turnover_filters: day>={config.get('min_day_turnover', 0.0):,.0f} "
        f"last_60m>={config.get('min_last_60m_turnover', 0.0):,.0f} "
        f"baseline>={config.get('min_baseline_turnover', 0.0):,.0f}",
        f"- capacity_caps: account={config.get('account_equity', 10000.0):,.0f} "
        f"max_weight={config.get('max_position_weight', 0.0):.2%} "
        f"dtd_turnover_cap={config.get('max_trade_notional_pct_of_day_turnover', 0.0):.2%} "
        f"baseline_turnover_cap={config.get('max_trade_notional_pct_of_baseline_turnover', 0.0):.2%}",
        f"- require_archive_membership: {config.get('require_archive_membership', False)}",
        f"- stop_loss_pct: {config['stop_loss_pct']:.2%}",
        f"- take_profit_pct: {config.get('take_profit_pct', 0.0):.2%}",
        f"- basket_stop_loss_pct: {config.get('basket_stop_loss_pct', 0.0):.2%}",
        f"- trailing_stop_pct: {config['trailing_stop_pct']:.2%}",
        f"- vol_trailing_stop_mult: {config.get('vol_trailing_stop_mult', 0.0):.2f}x daily vol",
        f"- mfe_giveback: activation={config.get('mfe_giveback_activation_pct', 0.0):.2%} giveback={config.get('mfe_giveback_pct', 0.0):.2%}",
        f"- vwap_reversion_pct: {config.get('vwap_reversion_pct', 0.0):.2%}",
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
        f"| Avg basket gross exposure | {summary.get('avg_basket_gross_exposure', 0.0):.2%} |",
        f"| Capacity-limited trades | {summary.get('capacity_limited_trades', 0)} |",
        "",
    ]
    return "\n".join(lines)


def format_close_fade_sleeves_report(payload: dict[str, Any]) -> str:
    rows = payload.get("results", [])
    lines = [
        "# Daily Close Fade Sleeve Comparison",
        "",
        f"Rows: features={payload.get('rows', {}).get('features', 0)} "
        f"trades={payload.get('rows', {}).get('trades', 0)} "
        f"baskets={payload.get('rows', {}).get('baskets', 0)}",
        f"Date range: {payload.get('date_range', {}).get('start')} to {payload.get('date_range', {}).get('end')}",
        "",
        "| Sleeve | Return | Sharpe | Max DD | Avg Gross | Trades | Cap-Limited | Liq Rank | Top N | Min DTD Turnover | Weight Cap | Day Cap | Baseline Cap |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('sleeve')} | {row.get('total_return', 0.0):.2%} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{row.get('max_drawdown', 0.0):.2%} | {row.get('avg_basket_gross_exposure', 0.0):.2%} | "
            f"{row.get('trade_count', 0)} | {row.get('capacity_limited_trades', 0)} | "
            f"{row.get('liquidity_rank_min', 1)}-{row.get('liquidity_rank_max', 0) or '∞'} | "
            f"{row.get('top_n', 0)} | {row.get('min_day_turnover', 0.0):,.0f} | "
            f"{row.get('max_position_weight', 0.0):.1%} | "
            f"{row.get('max_trade_notional_pct_of_day_turnover', 0.0):.2%} | "
            f"{row.get('max_trade_notional_pct_of_baseline_turnover', 0.0):.2%} |"
        )
    lines.append("")
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
            "| Rank | Return | Sharpe | Max DD | Signal | Top N | Gross | Hold | Score | Pump | Liq Rank | Stop | TP | Basket Stop | Trail | Vol Trail | MFE GB | VWAP | Cost | Trades | Win |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(rows[:25], start=1):
        lines.append(
            f"| {index} | {row.get('total_return', 0.0):.2%} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{row.get('max_drawdown', 0.0):.2%} | {_format_signal_minute(row.get('signal_minute', 0))} | "
            f"{row.get('top_n')} | {row.get('gross_exposure', 1.0):.2f}x | {row.get('hold_minutes')} | "
            f"{row.get('score')} | {row.get('pump_filter')} | "
            f"{row.get('liquidity_rank_min', 1)}-{row.get('liquidity_rank_max', 0) or '∞'} | "
            f"{row.get('stop_loss_pct', 0.0):.1%} | {row.get('take_profit_pct', 0.0):.1%} | "
            f"{row.get('basket_stop_loss_pct', 0.0):.1%} | "
            f"{row.get('trailing_stop_pct', 0.0):.1%} | {row.get('vol_trailing_stop_mult', 0.0):.2f}x | "
            f"{row.get('mfe_giveback_pct', 0.0):.0%} | {row.get('vwap_reversion_pct', 0.0):.0%} | "
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


def _baseline_liquidity_rank(base: pl.LazyFrame, lookback_days: int) -> pl.DataFrame:
    daily = (
        base.sort(["symbol", "date", "ts_ms"])
        .group_by(["symbol", "date"], maintain_order=True)
        .agg(pl.col("turnover_quote").sum().alias("daily_turnover"))
        .collect()
        .sort(["symbol", "date"])
    )
    if daily.is_empty():
        return pl.DataFrame(
            {
                "symbol": pl.Series([], dtype=pl.String),
                "date": pl.Series([], dtype=pl.String),
                "baseline_liquidity_turnover": pl.Series([], dtype=pl.Float64),
                "baseline_liquidity_rank": pl.Series([], dtype=pl.Int64),
            }
        )
    return (
        daily.with_columns(
            pl.col("daily_turnover")
            .rolling_mean(window_size=max(lookback_days, 1), min_samples=1)
            .over("symbol")
            .alias("baseline_liquidity_raw")
        )
        .with_columns(pl.col("baseline_liquidity_raw").shift(1).over("symbol").alias("baseline_liquidity_turnover"))
        .with_columns(
            pl.col("baseline_liquidity_turnover")
            .rank("ordinal", descending=True)
            .over("date")
            .cast(pl.Int64)
            .alias("baseline_liquidity_rank")
        )
        .select(["symbol", "date", "baseline_liquidity_turnover", "baseline_liquidity_rank"])
    )


def _baseline_liquidity_filter_expr(config: DailyCloseFadeConfig) -> pl.Expr:
    rank = pl.col("baseline_liquidity_rank")
    turnover = pl.col("baseline_liquidity_turnover")
    rank_filter_enabled = config.liquidity_rank_min > 1 or config.liquidity_rank_max > 0
    turnover_filter_enabled = config.min_baseline_turnover > 0.0
    expr = pl.lit(True)
    if rank_filter_enabled:
        expr = expr & rank.is_not_null() & (rank >= config.liquidity_rank_min)
        if config.liquidity_rank_max > 0:
            expr = expr & (rank <= config.liquidity_rank_max)
    if turnover_filter_enabled:
        expr = expr & turnover.is_not_null() & (turnover >= config.min_baseline_turnover)
    return expr


def _candidate_liquidity_filter_expr(config: DailyCloseFadeConfig) -> pl.Expr:
    return (
        (pl.col("day_turnover") >= config.min_day_turnover)
        & (pl.col("last_60m_turnover") >= config.min_last_60m_turnover)
        & _baseline_liquidity_filter_expr(config)
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
    archive_manifest = read_dataset(data_root, "archive_trade_manifest")
    if instruments.is_empty() and archive_manifest.is_empty() and min_age_days > 0:
        raise RuntimeError(
            "daily-close-fade needs instruments launch_time_ms or archive_trade_manifest first_date for the age filter"
        )
    if instruments.is_empty():
        latest = pl.DataFrame({"symbol": pl.Series([], dtype=pl.String), "launch_time_ms": pl.Series([], dtype=pl.Int64)})
    else:
        latest = (
            instruments.sort(["symbol", "ts_ms"])
            .unique(subset=["symbol"], keep="last")
            .select(["symbol", "launch_time_ms"])
        )
    if not archive_manifest.is_empty():
        archive_first = (
            archive_manifest.select(["symbol", "date"])
            .unique()
            .with_columns(pl.col("date").str.strptime(pl.Datetime, "%Y-%m-%d").dt.timestamp("ms").alias("archive_first_ts_ms"))
            .group_by("symbol")
            .agg(pl.col("archive_first_ts_ms").min())
        )
        latest = latest.join(archive_first, on="symbol", how="full", coalesce=True).with_columns(
            pl.coalesce(["launch_time_ms", "archive_first_ts_ms"]).alias("launch_time_ms")
        )
    return features.join(latest.select(["symbol", "launch_time_ms"]), on="symbol", how="left").with_columns(
        ((pl.col("signal_ts_ms") - pl.col("launch_time_ms")) / MS_PER_DAY).alias("age_days")
    )


def _attach_archive_membership(data_root: str | Path, features: pl.DataFrame, *, required: bool) -> pl.DataFrame:
    manifest = read_dataset(data_root, "archive_trade_manifest")
    if manifest.is_empty():
        if required:
            raise RuntimeError("archive_trade_manifest is empty; run archive-manifest before --require-archive-membership")
        return features.with_columns(pl.lit(False).alias("archive_tradable"))
    membership = manifest.select(["symbol", "date"]).unique().with_columns(pl.lit(True).alias("archive_tradable"))
    return features.join(membership, on=["symbol", "date"], how="left").with_columns(
        pl.col("archive_tradable").fill_null(False)
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
    take_profit_price = entry_price * (1.0 - config.take_profit_pct) if config.take_profit_pct > 0.0 else None
    vwap_exit_price = _vwap_reversion_exit_price(entry_price, row, config)
    realized_vol = max(float(row.get("realized_vol") or 0.0), 0.0)
    vol_trailing_stop_pct = realized_vol * config.vol_trailing_stop_mult if config.vol_trailing_stop_mult > 0.0 else 0.0
    vol_trailing_activation_pct = (
        realized_vol * config.vol_trailing_activation_mult if config.vol_trailing_stop_mult > 0.0 else 0.0
    )
    trailing_active = False
    vol_trailing_active = False
    mfe_giveback_active = False
    best_price = entry_price
    max_favorable = 0.0
    max_adverse = 0.0

    for bar in bars:
        ts_ms = int(bar["ts_ms"])
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        max_favorable = max(max_favorable, _short_return(entry_price, low))
        max_adverse = min(max_adverse, _short_return(entry_price, high))

        if ts_ms >= stop_active_ts_ms and hard_stop_price is not None and high >= hard_stop_price:
            exit_ts_ms = ts_ms
            exit_price = hard_stop_price
            exit_reason = "stop_loss"
            break

        if ts_ms >= stop_active_ts_ms:
            protective_exits: list[tuple[float, str]] = []
            if trailing_active and config.trailing_stop_pct > 0.0:
                trailing_stop = best_price * (1.0 + config.trailing_stop_pct)
                if high >= trailing_stop:
                    protective_exits.append((trailing_stop, "trailing_stop"))
            if vol_trailing_active and vol_trailing_stop_pct > 0.0:
                vol_trailing_stop = best_price * (1.0 + vol_trailing_stop_pct)
                if high >= vol_trailing_stop:
                    protective_exits.append((vol_trailing_stop, "vol_trailing_stop"))
            if mfe_giveback_active and config.mfe_giveback_pct > 0.0:
                mfe_stop = _mfe_giveback_stop_price(entry_price, best_price, config.mfe_giveback_pct)
                if high >= mfe_stop:
                    protective_exits.append((mfe_stop, "mfe_giveback"))
            if protective_exits:
                exit_price, exit_reason = max(protective_exits, key=lambda item: item[0])
                exit_ts_ms = ts_ms
                break

        if take_profit_price is not None and low <= take_profit_price:
            exit_ts_ms = ts_ms
            exit_price = take_profit_price
            exit_reason = "take_profit"
            break

        if vwap_exit_price is not None and low <= vwap_exit_price:
            exit_ts_ms = ts_ms
            exit_price = vwap_exit_price
            exit_reason = "vwap_reversion"
            break

        if low < best_price:
            best_price = low

        if ts_ms >= stop_active_ts_ms:
            best_return = _short_return(entry_price, best_price)
            if config.trailing_stop_pct > 0.0 and not trailing_active and best_return >= config.trailing_activation_pct:
                trailing_active = True
            if vol_trailing_stop_pct > 0.0 and not vol_trailing_active and best_return >= vol_trailing_activation_pct:
                vol_trailing_active = True
            if (
                config.mfe_giveback_pct > 0.0
                and not mfe_giveback_active
                and best_return >= config.mfe_giveback_activation_pct
            ):
                mfe_giveback_active = True

        if ts_ms >= target_exit_ts_ms:
            exit_ts_ms = ts_ms
            exit_price = close
            exit_reason = "max_hold"
            break

    gross_return = _short_return(entry_price, exit_price)
    cost_return = round_trip_cost_bps / 10_000.0
    net_return = gross_return - cost_return
    selected_count = max(int(row.get("selected_count") or config.top_n), 1)
    weight_info = _close_fade_position_weight(row, config=config, selected_count=selected_count)
    weight = weight_info["weight"]
    if weight <= EPSILON:
        return None
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
        "baseline_liquidity_turnover": float(row.get("baseline_liquidity_turnover") or 0.0),
        "baseline_liquidity_rank": int(row.get("baseline_liquidity_rank") or 0),
        "age_days": float(row.get("age_days") or 0.0),
        "day_turnover": float(row.get("day_turnover") or 0.0),
        "last_60m_turnover": float(row.get("last_60m_turnover") or 0.0),
        "target_weight": weight_info["target_weight"],
        "weight": weight,
        "position_weight_cap": weight_info["position_weight_cap"],
        "capacity_limited": weight_info["capacity_limited"],
        "capacity_cap_reason": weight_info["capacity_cap_reason"],
        "account_equity": config.account_equity,
        "target_notional": weight_info["target_notional"],
        "actual_notional": weight_info["actual_notional"],
        "gross_return": gross_return,
        "cost_return": cost_return,
        "net_return": net_return,
        "weighted_gross_return": gross_return * weight,
        "weighted_cost_return": cost_return * weight,
        "weighted_net_return": net_return * weight,
        "mae": max_adverse,
        "mfe": max_favorable,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "basket_stop_loss_pct": config.basket_stop_loss_pct,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_activation_pct": config.trailing_activation_pct,
        "vol_trailing_stop_mult": config.vol_trailing_stop_mult,
        "vol_trailing_activation_mult": config.vol_trailing_activation_mult,
        "mfe_giveback_activation_pct": config.mfe_giveback_activation_pct,
        "mfe_giveback_pct": config.mfe_giveback_pct,
        "vwap_reversion_pct": config.vwap_reversion_pct,
        "cost_multiplier": config.cost_multiplier,
        "pump_filter": config.pump_filter,
        "liquidity_lookback_days": config.liquidity_lookback_days,
        "liquidity_rank_min": config.liquidity_rank_min,
        "liquidity_rank_max": config.liquidity_rank_max,
        "min_baseline_turnover": config.min_baseline_turnover,
        "max_position_weight": config.max_position_weight,
        "max_trade_notional_pct_of_day_turnover": config.max_trade_notional_pct_of_day_turnover,
        "max_trade_notional_pct_of_baseline_turnover": config.max_trade_notional_pct_of_baseline_turnover,
        "top_n": config.top_n,
    }


def _close_fade_position_weight(
    row: dict[str, Any],
    *,
    config: DailyCloseFadeConfig,
    selected_count: int,
) -> dict[str, Any]:
    target_weight = config.gross_exposure / max(selected_count, 1)
    caps: list[tuple[float, str]] = [(target_weight, "target")]
    if config.max_position_weight > 0.0:
        caps.append((config.max_position_weight, "max_position_weight"))
    if config.max_trade_notional_pct_of_day_turnover > 0.0:
        day_turnover = float(row.get("day_turnover") or 0.0)
        caps.append(
            (
                day_turnover * config.max_trade_notional_pct_of_day_turnover / config.account_equity,
                "day_turnover",
            )
        )
    if config.max_trade_notional_pct_of_baseline_turnover > 0.0:
        baseline_turnover = float(row.get("baseline_liquidity_turnover") or 0.0)
        caps.append(
            (
                baseline_turnover * config.max_trade_notional_pct_of_baseline_turnover / config.account_equity,
                "baseline_turnover",
            )
        )
    capped_weight, cap_reason = min(caps, key=lambda item: item[0])
    weight = max(0.0, min(target_weight, capped_weight))
    return {
        "target_weight": target_weight,
        "weight": weight,
        "position_weight_cap": capped_weight,
        "capacity_limited": weight + EPSILON < target_weight,
        "capacity_cap_reason": cap_reason if weight + EPSILON < target_weight else "",
        "target_notional": target_weight * config.account_equity,
        "actual_notional": weight * config.account_equity,
    }


def _vwap_reversion_exit_price(entry_price: float, row: dict[str, Any], config: DailyCloseFadeConfig) -> float | None:
    if config.vwap_reversion_pct <= 0.0:
        return None
    vwap = float(row.get("intraday_vwap") or 0.0)
    if vwap <= 0.0 or vwap >= entry_price:
        return None
    pct = min(config.vwap_reversion_pct, 1.0)
    return entry_price - (entry_price - vwap) * pct


def _mfe_giveback_stop_price(entry_price: float, best_price: float, giveback_pct: float) -> float:
    max_favorable = max(_short_return(entry_price, best_price), 0.0)
    retained_return = max_favorable * max(0.0, 1.0 - giveback_pct)
    return entry_price * (1.0 - retained_return)


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


def _short_return(entry_price: float, exit_price: float) -> float:
    return (entry_price - exit_price) / max(entry_price, EPSILON)


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


def _concat_frames(frames: list[pl.DataFrame]) -> pl.DataFrame:
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


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
        f"- Gross exposure: {row.get('gross_exposure', 1.0):.2f}x",
        f"- Hold: {row.get('hold_minutes')} minutes",
        f"- Score: {row.get('score')}",
        f"- Pump filter: {row.get('pump_filter')}",
        f"- Liquidity rank: {row.get('liquidity_rank_min', 1)}-{row.get('liquidity_rank_max', 0) or 'unbounded'} "
        f"over {row.get('liquidity_lookback_days', 7)}d baseline",
        f"- Stop: {row.get('stop_loss_pct', 0.0):.2%}",
        f"- Take profit: {row.get('take_profit_pct', 0.0):.2%}",
        f"- Basket stop: {row.get('basket_stop_loss_pct', 0.0):.2%}",
        f"- Trail: {row.get('trailing_stop_pct', 0.0):.2%} after {row.get('trailing_activation_pct', 0.0):.2%}",
        f"- Vol trail: {row.get('vol_trailing_stop_mult', 0.0):.2f}x after {row.get('vol_trailing_activation_mult', 0.0):.2f}x daily vol",
        f"- MFE giveback: {row.get('mfe_giveback_pct', 0.0):.2%} after {row.get('mfe_giveback_activation_pct', 0.0):.2%}",
        f"- VWAP reversion: {row.get('vwap_reversion_pct', 0.0):.2%}",
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
