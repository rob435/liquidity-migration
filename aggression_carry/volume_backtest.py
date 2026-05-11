from __future__ import annotations

import json
import math
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .backtest_audit import volume_backtest_audit
from .config import CostConfig, VolumeBacktestConfig, VolumeGridConfig
from .storage import dataset_path, read_dataset, write_dataset
from .volume_alpha import MS_PER_DAY, MS_PER_HOUR, VOLUME_SCORE_COLUMNS, build_volume_features


_GRID_FEATURES: pl.DataFrame | None = None
_GRID_KLINES: pl.DataFrame | None = None
_GRID_RANK_FEATURES: pl.DataFrame | None = None
_GRID_FUNDING: pl.DataFrame | None = None


def run_volume_trade_backtest(
    data_root: str | Path,
    *,
    backtest_config: VolumeBacktestConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    config = backtest_config or VolumeBacktestConfig()
    costs = cost_config or CostConfig()
    _validate_config(config)

    klines = read_dataset(data_root, "klines_1h")
    if klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")

    all_features = build_volume_features(klines)
    features = _filter_signal_window(all_features, config)
    funding = read_dataset(data_root, "funding")
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier
    trades = backtest_volume_trades(
        features,
        klines,
        config=config,
        round_trip_cost_bps=round_trip_cost_bps,
        rank_features=all_features,
        funding=funding,
    )
    baskets = summarize_baskets(trades, config=config)
    equity = build_equity_curve(baskets)
    monthly_vs_btc = build_monthly_vs_btc(baskets, trades, klines)
    equity_vs_btc = build_equity_vs_btc(equity, klines)
    regime_summary = summarize_btc_regimes(monthly_vs_btc)
    summary = summarize_trade_backtest(trades, baskets, equity, config=config)
    split_metrics = summarize_volume_splits(baskets, config=config)
    cost_model = {
        **asdict(costs),
        "round_trip_cost_bps": round_trip_cost_bps,
    }

    payload = {
        "config": asdict(config),
        "cost_model": cost_model,
        "rows": {
            "features": features.height,
            "trades": trades.height,
            "baskets": baskets.height,
        },
        "date_range": _date_range(features),
        "summary": summary,
        "split_metrics": split_metrics,
        "exit_reasons": _exit_reason_rows(trades),
        "symbol_attribution": _attribution_rows(trades, "symbol"),
        "monthly_attribution": _attribution_rows(trades, "exit_month"),
        "monthly_vs_btc": monthly_vs_btc.to_dicts(),
        "btc_regime_summary": regime_summary,
        "visualizations": {
            "equity_curve": "reports/volume_backtest_equity_curve.svg",
            "monthly_vs_btc": "reports/volume_backtest_monthly_vs_btc.svg",
        },
        "trades": trades.to_dicts(),
        "baskets": baskets.to_dicts(),
    }
    payload["backtest_validity"] = volume_backtest_audit(
        config=config,
        summary=summary,
        rows=payload["rows"],
        cost_model=cost_model,
        split_metrics=split_metrics,
        data_root=data_root,
    )

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "volume_backtest_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "volume_backtest_report.md").write_text(format_volume_backtest_report(payload), encoding="utf-8")
    if not trades.is_empty():
        trades.write_csv(output_dir / "volume_backtest_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "volume_backtest_baskets.csv")
    if not equity_vs_btc.is_empty():
        equity_vs_btc.write_csv(output_dir / "volume_backtest_equity_vs_btc.csv")
    if not monthly_vs_btc.is_empty():
        monthly_vs_btc.write_csv(output_dir / "volume_backtest_monthly_vs_btc.csv")
    _write_visualizations(output_dir, equity_vs_btc=equity_vs_btc, monthly_vs_btc=monthly_vs_btc)

    _replace_dataset(trades, data_root, "volume_backtest_trades", partition_by=("exit_date", "symbol"))
    _replace_dataset(baskets, data_root, "volume_backtest_baskets", partition_by=("exit_date",))
    _replace_dataset(equity, data_root, "volume_backtest_equity", partition_by=("date",))
    _replace_dataset(equity_vs_btc, data_root, "volume_backtest_equity_vs_btc", partition_by=("date",))
    _replace_dataset(monthly_vs_btc, data_root, "volume_backtest_monthly", partition_by=("month",))
    return payload


def run_volume_grid(
    data_root: str | Path,
    *,
    grid_config: VolumeGridConfig | None = None,
    base_backtest_config: VolumeBacktestConfig | None = None,
    cost_config: CostConfig | None = None,
    max_workers: int | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    grid = grid_config or VolumeGridConfig()
    base = base_backtest_config or VolumeBacktestConfig()
    costs = cost_config or CostConfig()
    klines = read_dataset(data_root, "klines_1h")
    if klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    funding = read_dataset(data_root, "funding")

    all_features = build_volume_features(klines)
    features = _filter_signal_window(all_features, base)
    variants = list(iter_grid_configs(grid, base))
    tasks = [
        (f"grid-{index:04d}", config, costs.base_entry_exit_cost_bps * config.cost_multiplier)
        for index, config in enumerate(variants, start=1)
    ]
    workers = _resolve_workers(max_workers, len(tasks))
    backend = _grid_backend(workers)
    if workers <= 1:
        rows = [
            _evaluate_grid_variant(
                features,
                klines,
                grid_id=grid_id,
                config=config,
                round_trip_cost_bps=round_trip_cost_bps,
                rank_features=all_features,
                funding=funding,
            )
            for grid_id, config, round_trip_cost_bps in tasks
        ]
    elif backend == "thread":
        _init_grid_worker(features, klines, all_features, funding)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            rows = list(
                executor.map(
                    _evaluate_grid_variant_worker,
                    tasks,
                    chunksize=_grid_chunksize(len(tasks), workers),
                )
            )
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_grid_worker,
            initargs=(features, klines, all_features, funding),
        ) as executor:
            rows = list(executor.map(_evaluate_grid_variant_worker, tasks, chunksize=_grid_chunksize(len(tasks), workers)))

    results = pl.DataFrame(rows).sort(["total_return", "sharpe_like"], descending=[True, True]) if rows else pl.DataFrame()
    payload = {
        "rows": results.height,
        "workers": workers,
        "worker_backend": backend,
        "date_range": _date_range(features),
        "best_total_return": results.head(1).to_dicts()[0] if not results.is_empty() else {},
        "best_sharpe_like": results.sort("sharpe_like", descending=True).head(1).to_dicts()[0] if not results.is_empty() else {},
        "results": results.to_dicts(),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "volume_grid_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "volume_grid_report.md").write_text(format_volume_grid_report(payload), encoding="utf-8")
    if not results.is_empty():
        results.write_csv(output_dir / "volume_grid_results.csv")
    _replace_dataset(results, data_root, "volume_backtest_grid", partition_by=("score", "stop_mode"))
    return payload


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


def _grid_backend(workers: int) -> str:
    if workers <= 1:
        return "serial"
    backend = os.environ.get("VOLUME_GRID_BACKEND", "thread").strip().lower()
    if backend in {"process", "processes", "process_pool"}:
        return "process"
    return "thread"


def _init_grid_worker(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    rank_features: pl.DataFrame,
    funding: pl.DataFrame | None = None,
) -> None:
    global _GRID_FEATURES, _GRID_KLINES, _GRID_RANK_FEATURES, _GRID_FUNDING
    _GRID_FEATURES = features
    _GRID_KLINES = klines
    _GRID_RANK_FEATURES = rank_features
    _GRID_FUNDING = funding if funding is not None else pl.DataFrame()


def _evaluate_grid_variant_worker(task: tuple[str, VolumeBacktestConfig, float]) -> dict[str, Any]:
    if _GRID_FEATURES is None or _GRID_KLINES is None or _GRID_RANK_FEATURES is None:
        raise RuntimeError("grid worker was not initialized")
    grid_id, config, round_trip_cost_bps = task
    return _evaluate_grid_variant(
        _GRID_FEATURES,
        _GRID_KLINES,
        grid_id=grid_id,
        config=config,
        round_trip_cost_bps=round_trip_cost_bps,
        rank_features=_GRID_RANK_FEATURES,
        funding=_GRID_FUNDING,
    )


def _evaluate_grid_variant(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    grid_id: str,
    config: VolumeBacktestConfig,
    round_trip_cost_bps: float,
    rank_features: pl.DataFrame | None = None,
    funding: pl.DataFrame | None = None,
) -> dict[str, Any]:
    trades = backtest_volume_trades(
        features,
        klines,
        config=config,
        round_trip_cost_bps=round_trip_cost_bps,
        rank_features=rank_features,
        funding=funding,
    )
    baskets = summarize_baskets(trades, config=config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=config)
    config_row = asdict(config)
    config_row["include_symbols"] = ",".join(config.include_symbols)
    config_row["exclude_symbols"] = ",".join(config.exclude_symbols)
    return {
        "grid_id": grid_id,
        **config_row,
        "round_trip_cost_bps": round_trip_cost_bps,
        **summary,
        **_exit_reason_counts(trades),
    }


def iter_grid_configs(
    grid: VolumeGridConfig,
    base: VolumeBacktestConfig | None = None,
) -> list[VolumeBacktestConfig]:
    base_config = base or VolumeBacktestConfig()
    side_modes = ["long_high_short_low"]
    if grid.include_reverse_side:
        side_modes.append("short_high_long_low")
    configs = []
    stop_variants: list[dict[str, float | str]] = [
        {
            "stop_mode": "none" if stop_pct <= 0.0 else "fixed",
            "stop_loss_pct": stop_pct,
            "vol_stop_multiplier": base_config.vol_stop_multiplier,
        }
        for stop_pct in grid.fixed_stop_loss_pcts
    ]
    stop_variants.extend(
        {
            "stop_mode": "volatility",
            "stop_loss_pct": base_config.stop_loss_pct,
            "vol_stop_multiplier": multiplier,
        }
        for multiplier in grid.vol_stop_multipliers
    )
    for score, quantile, hold_days, stop, rank_exit, take_profit, cost_multiplier, side_mode in product(
        grid.scores,
        grid.quantiles,
        grid.hold_days,
        stop_variants,
        grid.rank_exit_modes,
        grid.take_profit_pcts,
        grid.cost_multipliers,
        side_modes,
    ):
        configs.append(
            VolumeBacktestConfig(
                score=score,
                start_date=base_config.start_date,
                end_date=base_config.end_date,
                quantile=quantile,
                hold_days=hold_days,
                rebalance_days=hold_days,
                gross_exposure=base_config.gross_exposure,
                entry_delay_hours=base_config.entry_delay_hours,
                stop_mode=str(stop["stop_mode"]),
                stop_loss_pct=float(stop["stop_loss_pct"]),
                vol_stop_multiplier=float(stop["vol_stop_multiplier"]),
                vol_stop_lookback_days=base_config.vol_stop_lookback_days,
                min_stop_loss_pct=base_config.min_stop_loss_pct,
                max_stop_loss_pct=base_config.max_stop_loss_pct,
                take_profit_pct=take_profit,
                min_symbols=base_config.min_symbols,
                cost_multiplier=cost_multiplier,
                side_mode=side_mode,
                rank_exit_enabled=rank_exit,
                rank_exit_threshold=base_config.rank_exit_threshold,
                universe_rank_min=base_config.universe_rank_min,
                universe_rank_max=base_config.universe_rank_max,
                universe_min_daily_turnover=base_config.universe_min_daily_turnover,
                include_symbols=base_config.include_symbols,
                exclude_symbols=base_config.exclude_symbols,
            )
        )
    return configs


def backtest_volume_trades(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    config: VolumeBacktestConfig,
    round_trip_cost_bps: float,
    rank_features: pl.DataFrame | None = None,
    funding: pl.DataFrame | None = None,
) -> pl.DataFrame:
    _validate_config(config)
    score_col = _score_column(config.score)
    if features.is_empty():
        return _empty_trades()
    if score_col not in features.columns:
        raise RuntimeError(f"score column {score_col!r} is missing from volume features")

    bars = _price_bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding)
    stop_lookup = _volatility_stop_lookup(klines, lookback_days=config.vol_stop_lookback_days)
    rank_lookup = _rank_lookup(
        rank_features if rank_features is not None else features,
        score_col=score_col,
        entry_delay_hours=config.entry_delay_hours,
        config=config,
    )
    first_signal_ts = int(features["ts_ms"].min())
    rows: list[dict[str, Any]] = []

    for part in features.sort(["ts_ms", "symbol"]).partition_by("ts_ms", maintain_order=True):
        signal_ts_ms = int(part["ts_ms"][0])
        day_index = (signal_ts_ms - first_signal_ts) // MS_PER_DAY
        if day_index % config.rebalance_days != 0:
            continue
        universe_part = _filter_universe(part, config)
        basket_id = (
            f"{_iso_date(signal_ts_ms)}-{config.score}-q{int(config.quantile * 10000)}"
            f"-u{config.universe_rank_min}-{config.universe_rank_max or 'all'}"
        )
        selected = _select_basket(universe_part, score_col=score_col, config=config)
        if not selected:
            continue
        entry_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
        planned_exit_ts_ms = entry_ts_ms + config.hold_days * MS_PER_DAY
        long_count = sum(1 for item in selected if item["side"] == "long")
        short_count = sum(1 for item in selected if item["side"] == "short")
        long_weight = (config.gross_exposure * 0.5 / long_count) if long_count else 0.0
        short_weight = (config.gross_exposure * 0.5 / short_count) if short_count else 0.0

        for item in selected:
            symbol = item["symbol"]
            symbol_bars = bars.get(symbol, [])
            entry_bar = _bar_at_close(symbol_bars, entry_ts_ms)
            if entry_bar is None:
                continue
            notional_weight = long_weight if item["side"] == "long" else short_weight
            trade = _simulate_trade(
                symbol=symbol,
                side=item["side"],
                score=float(item["score"]),
                rank=int(item["rank"]),
                basket_id=basket_id,
                signal_ts_ms=signal_ts_ms,
                entry_bar=entry_bar,
                symbol_bars=symbol_bars,
                planned_exit_ts_ms=planned_exit_ts_ms,
                notional_weight=notional_weight,
                config=config,
                round_trip_cost_bps=round_trip_cost_bps,
                stop_pct=_stop_pct_for_trade(
                    config=config,
                    symbol=symbol,
                    signal_ts_ms=signal_ts_ms,
                    stop_lookup=stop_lookup,
                ),
                rank_lookup=rank_lookup,
                funding_lookup=funding_lookup,
            )
            if trade is not None:
                rows.append(trade)

    if not rows:
        return _empty_trades()
    return pl.DataFrame(rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol", "side"])


def summarize_baskets(trades: pl.DataFrame, *, config: VolumeBacktestConfig) -> pl.DataFrame:
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


def build_monthly_vs_btc(baskets: pl.DataFrame, trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return _empty_monthly_vs_btc()

    monthly_strategy = (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.col("long_return").sum().alias("long_return"),
                pl.col("short_return").sum().alias("short_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.len().alias("baskets"),
            ]
        )
        .sort("month")
    )
    trade_stats = _monthly_trade_stats(trades)
    btc_returns = _btc_monthly_returns(klines)
    joined = monthly_strategy.join(trade_stats, on="month", how="left").join(btc_returns, on="month", how="left")

    rows: list[dict[str, Any]] = []
    strategy_equity = 1.0
    btc_equity = 1.0
    for row in joined.sort("month").to_dicts():
        strategy_return = float(row["strategy_return"])
        btc_return = _finite_float(row.get("btc_return"))
        strategy_equity *= 1.0 + strategy_return
        if btc_return is not None:
            btc_equity *= 1.0 + btc_return
        rows.append(
            {
                "month": str(row["month"]),
                "strategy_return": strategy_return,
                "btc_return": btc_return,
                "strategy_minus_btc": strategy_return - btc_return if btc_return is not None else None,
                "btc_regime": _btc_regime(btc_return),
                "strategy_equity": strategy_equity,
                "btc_equity": btc_equity if btc_return is not None else None,
                "long_return": float(row["long_return"]),
                "short_return": float(row["short_return"]),
                "cost_return": float(row["cost_return"]),
                "baskets": int(row["baskets"]),
                "trades": int(row["trades"]) if row.get("trades") is not None else 0,
                "trade_win_rate": _finite_float(row.get("trade_win_rate")),
                "avg_trade_return": _finite_float(row.get("avg_trade_return")),
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else _empty_monthly_vs_btc()


def build_equity_vs_btc(equity: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    if equity.is_empty():
        return _empty_equity_vs_btc()
    btc_bars = (
        klines.filter(pl.col("symbol") == "BTCUSDT")
        .select(["ts_ms", "close"])
        .drop_nulls()
        .sort("ts_ms")
        .to_dicts()
    )
    if not btc_bars:
        rows = [
            {
                "ts_ms": int(row["ts_ms"]),
                "date": str(row["date"]),
                "strategy_equity": float(row["equity"]),
                "strategy_drawdown": float(row["drawdown"]),
                "basket_return": float(row["basket_return"]),
                "btc_equity": None,
                "btc_return": None,
            }
            for row in equity.sort("ts_ms").to_dicts()
        ]
        return pl.DataFrame(rows, infer_schema_length=None) if rows else _empty_equity_vs_btc()

    first_close = float(btc_bars[0]["close"])
    btc_index = 0
    rows = []
    for row in equity.sort("ts_ms").to_dicts():
        ts_ms = int(row["ts_ms"])
        while btc_index + 1 < len(btc_bars) and int(btc_bars[btc_index + 1]["ts_ms"]) <= ts_ms:
            btc_index += 1
        btc_close = float(btc_bars[btc_index]["close"])
        btc_equity = btc_close / first_close if first_close > 0.0 else None
        rows.append(
            {
                "ts_ms": ts_ms,
                "date": str(row["date"]),
                "strategy_equity": float(row["equity"]),
                "strategy_drawdown": float(row["drawdown"]),
                "basket_return": float(row["basket_return"]),
                "btc_equity": btc_equity,
                "btc_return": btc_equity - 1.0 if btc_equity is not None else None,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else _empty_equity_vs_btc()


def summarize_btc_regimes(monthly_vs_btc: pl.DataFrame) -> list[dict[str, Any]]:
    if monthly_vs_btc.is_empty() or "btc_regime" not in monthly_vs_btc.columns:
        return []
    rows = []
    for regime in ("btc_up", "btc_down", "btc_flat", "btc_missing"):
        part = monthly_vs_btc.filter(pl.col("btc_regime") == regime)
        if part.is_empty():
            continue
        strategy_returns = [float(item) for item in part["strategy_return"].to_list()]
        btc_returns = [_finite_float(item) for item in part["btc_return"].to_list()]
        finite_btc = [item for item in btc_returns if item is not None]
        rows.append(
            {
                "btc_regime": regime,
                "months": part.height,
                "strategy_return": _compound(strategy_returns),
                "avg_strategy_month": float(np.mean(strategy_returns)) if strategy_returns else 0.0,
                "strategy_win_rate": float((part["strategy_return"] > 0.0).mean()),
                "btc_return": _compound(finite_btc) if finite_btc else None,
                "avg_btc_month": float(np.mean(finite_btc)) if finite_btc else None,
            }
        )
    return rows


def summarize_trade_backtest(
    trades: pl.DataFrame,
    baskets: pl.DataFrame,
    equity: pl.DataFrame,
    *,
    config: VolumeBacktestConfig,
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


def summarize_volume_splits(baskets: pl.DataFrame, *, config: VolumeBacktestConfig) -> list[dict[str, Any]]:
    if baskets.is_empty():
        return []
    ordered = baskets.sort("entry_ts_ms")
    rows = ordered.to_dicts()
    split_count = min(3, len(rows))
    split_size = math.ceil(len(rows) / split_count)
    output = []
    for index in range(split_count):
        chunk = rows[index * split_size : (index + 1) * split_size]
        if chunk:
            output.append(_summarize_volume_basket_chunk(chunk, name=f"chronological_{index + 1}_of_{split_count}", config=config))
    if ordered.height:
        latest_ts = int(ordered["entry_ts_ms"].max())
        trailing = ordered.filter(pl.col("entry_ts_ms") >= latest_ts - 365 * MS_PER_DAY).to_dicts()
        if trailing and len(trailing) < len(rows):
            output.append(_summarize_volume_basket_chunk(trailing, name="trailing_365d", config=config))
    return output


def _summarize_volume_basket_chunk(
    rows: list[dict[str, Any]],
    *,
    name: str,
    config: VolumeBacktestConfig,
) -> dict[str, Any]:
    returns = [float(row["basket_return"]) for row in rows]
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    mean_return = float(np.mean(returns)) if returns else 0.0
    stdev = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    annual_periods = 365.0 / config.rebalance_days
    return {
        "name": name,
        "start": str(rows[0].get("exit_date", "")),
        "end": str(rows[-1].get("exit_date", "")),
        "basket_count": len(rows),
        "total_return": float(equity - 1.0),
        "sharpe_like": float(mean_return / stdev * math.sqrt(annual_periods)) if stdev > 1e-12 else 0.0,
        "max_drawdown": float(max_dd),
        "worst_basket_return": float(min(returns)) if returns else 0.0,
        "avg_basket_return": float(mean_return),
    }


def format_volume_backtest_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    config = payload["config"]
    validity = payload.get("backtest_validity", {})
    lines = [
        "# Volume Trade Backtest",
        "",
        f"Date range: {payload['date_range']['start']} to {payload['date_range']['end']}",
        f"Feature rows: {payload['rows']['features']}",
        f"Trades: {payload['rows']['trades']}",
        f"Baskets: {payload['rows']['baskets']}",
        f"Validity label: `{validity.get('label', 'unknown')}`",
        f"Can support promotion: `{validity.get('can_support_promotion', False)}`",
        "",
        "## Rule Gates",
        "",
        "| Gate | Status | Severity | Detail |",
        "|---|---|---|---|",
    ]
    for item in validity.get("gates", []):
        lines.append(f"| {item.get('name')} | {item.get('status')} | {item.get('severity')} | {item.get('detail')} |")
    lines.extend(
        [
        "",
        "## Trading Logic",
        "",
        f"- Score: `{config['score']}`",
        f"- Side mode: `{config['side_mode']}`",
        f"- Quantile bucket: {config['quantile']:.0%}",
        f"- Hold/rebalance: {config['hold_days']}d / {config['rebalance_days']}d",
        f"- Signal window: {_window_label(config)}",
        f"- Gross exposure: {config['gross_exposure']:.2f}x",
        f"- Entry delay: {config['entry_delay_hours']}h after daily signal close",
        f"- Stop: {_grid_stop_label(config)}",
        f"- Rank exit: {config['rank_exit_enabled']}",
        f"- Take profit: {_pct_or_disabled(config['take_profit_pct'])}",
        f"- Daily universe ranks: {_universe_label(config)}",
        f"- Min daily turnover: {_currency_or_disabled(config['universe_min_daily_turnover'])}",
        f"- Included symbols: {_symbols_or_all(config['include_symbols'])}",
        f"- Excluded symbols: {_symbols_or_none(config['exclude_symbols'])}",
        f"- Round-trip cost: {payload['cost_model']['round_trip_cost_bps']:.2f} bps",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total return | {summary['total_return']:.2%} |",
        f"| Sharpe-like | {summary['sharpe_like']:.2f} |",
        f"| Max drawdown | {summary['max_drawdown']:.2%} |",
        f"| Trade win rate | {summary['trade_win_rate']:.2%} |",
        f"| Profit factor | {summary['profit_factor']:.2f} |",
        f"| Long contribution | {summary['long_return']:.2%} |",
        f"| Short contribution | {summary['short_return']:.2%} |",
        f"| Costs | {summary['cost_return']:.2%} |",
        f"| Funding | {summary.get('funding_return', 0.0):.2%} |",
        f"| Funding mode | {summary.get('funding_mode', 'missing')} |",
        f"| Funding events | {summary.get('funding_event_count', 0)} |",
        f"| Worst basket | {summary.get('worst_basket_return', 0.0):.2%} |",
        f"| Worst day | {summary.get('worst_day_return', 0.0):.2%} |",
        "",
        "## Split Metrics",
        "",
        "| Split | Start | End | Return | Sharpe | Max DD | Baskets | Worst Basket |",
        "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("split_metrics", []):
        lines.append(
            f"| {row.get('name')} | {row.get('start')} | {row.get('end')} | "
            f"{row.get('total_return', 0.0):.2%} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{row.get('max_drawdown', 0.0):.2%} | {row.get('basket_count', 0)} | "
            f"{row.get('worst_basket_return', 0.0):.2%} |"
        )
    lines.extend(
        [
        "",
        "## Exit Reasons",
        "",
        "| Reason | Trades | Net return | Avg trade |",
        "|---|---:|---:|---:|",
        ]
    )
    for item in payload["exit_reasons"]:
        lines.append(
            f"| {item['exit_reason']} | {item['trades']} | {item['net_return']:.2%} | {item['avg_trade_return']:.3%} |"
        )
    lines.extend(
        [
            "",
            "## Symbol Attribution",
            "",
            "| Symbol | Trades | Net return | Win rate | Avg trade |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for item in payload["symbol_attribution"][:20]:
        lines.append(
            f"| {item['symbol']} | {item['trades']} | {item['net_return']:.2%} | "
            f"{item['win_rate']:.2%} | {item['avg_trade_return']:.3%} |"
        )
    lines.extend(
        [
        "",
        "## Monthly Attribution",
        "",
        "| Month | Trades | Net return | Win rate | Avg trade |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for item in payload["monthly_attribution"]:
        lines.append(
            f"| {item['exit_month']} | {item['trades']} | {item['net_return']:.2%} | "
            f"{item['win_rate']:.2%} | {item['avg_trade_return']:.3%} |"
        )
    lines.extend(
        [
            "",
            "## Monthly Performance Vs BTC",
            "",
            "| Month | Strategy | BTC | Strategy - BTC | BTC Regime | Long | Short | Costs | Trades | Win rate |",
            "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["monthly_vs_btc"]:
        lines.append(
            f"| {item['month']} | {_format_pct(item['strategy_return'])} | {_format_pct(item['btc_return'])} | "
            f"{_format_pct(item['strategy_minus_btc'])} | {item['btc_regime']} | "
            f"{_format_pct(item['long_return'])} | {_format_pct(item['short_return'])} | "
            f"{_format_pct(item['cost_return'])} | {item['trades']} | {_format_pct(item['trade_win_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## BTC Regime Summary",
            "",
            "| BTC Regime | Months | Strategy return | Avg strategy month | Strategy win rate | BTC return | Avg BTC month |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["btc_regime_summary"]:
        lines.append(
            f"| {item['btc_regime']} | {item['months']} | {_format_pct(item['strategy_return'])} | "
            f"{_format_pct(item['avg_strategy_month'])} | {_format_pct(item['strategy_win_rate'])} | "
            f"{_format_pct(item['btc_return'])} | {_format_pct(item['avg_btc_month'])} |"
        )
    lines.extend(
        [
            "",
            "## Visualizations",
            "",
            "![Equity curve vs BTC](volume_backtest_equity_curve.svg)",
            "",
            "![Monthly strategy vs BTC](volume_backtest_monthly_vs_btc.svg)",
        ]
    )
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "- `reports/volume_backtest_trades.csv` has every trade entry, exit, side, reason, score, and return.",
            "- `reports/volume_backtest_baskets.csv` has per-rebalance basket returns.",
            "- `reports/volume_backtest_equity_vs_btc.csv` has strategy equity aligned to BTC.",
            "- `reports/volume_backtest_monthly_vs_btc.csv` has month-by-month strategy/BTC regime performance.",
            "- `reports/volume_backtest_equity_curve.svg` and `reports/volume_backtest_monthly_vs_btc.svg` are browser-viewable charts.",
            "- `volume_backtest_trades`, `volume_backtest_baskets`, and `volume_backtest_equity` are written as Parquet datasets.",
            "",
        ]
    )
    return "\n".join(lines)


def format_volume_grid_report(payload: dict[str, Any]) -> str:
    best_return = payload.get("best_total_return") or {}
    best_sharpe = payload.get("best_sharpe_like") or {}
    lines = [
        "# Volume Backtest Grid",
        "",
        f"Date range: {payload['date_range']['start']} to {payload['date_range']['end']}",
        f"Rows: {payload['rows']}",
        f"Workers: {payload['workers']}",
        "",
        "## Best Total Return",
        "",
        _format_grid_pick(best_return),
        "",
        "## Best Sharpe-Like",
        "",
        _format_grid_pick(best_sharpe),
        "",
        "## Top 25 By Total Return",
        "",
        "| Rank | Score | Hold | Quantile | Stop | Rank Exit | Side Mode | Return | Sharpe | Max DD | Long | Short | Stops | Rank Exits |",
        "|---:|---|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for index, row in enumerate(payload["results"][:25], start=1):
        lines.append(
            f"| {index} | {row['score']} | {row['hold_days']}d | {row['quantile']:.0%} | "
            f"{_grid_stop_label(row)} | {str(row['rank_exit_enabled'])} | {row['side_mode']} | "
            f"{row['total_return']:.2%} | {row['sharpe_like']:.2f} | {row['max_drawdown']:.2%} | "
            f"{row['long_return']:.2%} | {row['short_return']:.2%} | "
            f"{int(row.get('exit_stop_loss', 0))} | {int(row.get('exit_rank_exit', 0))} |"
        )
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "- `reports/volume_grid_results.csv` contains every tested parameter combination.",
            "- Rerun promising rows with `volume-backtest` to inspect the full trade ledger.",
            "",
        ]
    )
    return "\n".join(lines)


def _monthly_trade_stats(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame(
            {
                "month": pl.Series([], dtype=pl.String),
                "trades": pl.Series([], dtype=pl.Int64),
                "trade_win_rate": pl.Series([], dtype=pl.Float64),
                "avg_trade_return": pl.Series([], dtype=pl.Float64),
            }
        )
    return (
        trades.group_by("exit_month")
        .agg(
            [
                pl.len().alias("trades"),
                (pl.col("net_return") > 0.0).mean().alias("trade_win_rate"),
                pl.col("net_return").mean().alias("avg_trade_return"),
            ]
        )
        .rename({"exit_month": "month"})
        .sort("month")
    )


def _btc_monthly_returns(klines: pl.DataFrame) -> pl.DataFrame:
    empty = pl.DataFrame(
        {
            "month": pl.Series([], dtype=pl.String),
            "btc_return": pl.Series([], dtype=pl.Float64),
            "btc_first_close": pl.Series([], dtype=pl.Float64),
            "btc_last_close": pl.Series([], dtype=pl.Float64),
        }
    )
    if klines.is_empty() or not {"symbol", "ts_ms", "close"}.issubset(set(klines.columns)):
        return empty
    btc = klines.filter(pl.col("symbol") == "BTCUSDT").select(["ts_ms", "close"]).drop_nulls().sort("ts_ms")
    if btc.is_empty():
        return empty
    return (
        btc.with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month", maintain_order=True)
        .agg(
            [
                pl.col("close").first().alias("btc_first_close"),
                pl.col("close").last().alias("btc_last_close"),
            ]
        )
        .with_columns((pl.col("btc_last_close") / pl.col("btc_first_close") - 1.0).alias("btc_return"))
        .select(["month", "btc_return", "btc_first_close", "btc_last_close"])
        .sort("month")
    )


def _write_visualizations(output_dir: Path, *, equity_vs_btc: pl.DataFrame, monthly_vs_btc: pl.DataFrame) -> None:
    (output_dir / "volume_backtest_equity_curve.svg").write_text(
        _render_equity_curve_svg(equity_vs_btc),
        encoding="utf-8",
    )
    (output_dir / "volume_backtest_monthly_vs_btc.svg").write_text(
        _render_monthly_vs_btc_svg(monthly_vs_btc),
        encoding="utf-8",
    )


def _render_equity_curve_svg(equity_vs_btc: pl.DataFrame) -> str:
    width = 1100
    height = 520
    margin_left = 72
    margin_right = 32
    margin_top = 58
    margin_bottom = 72
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    if equity_vs_btc.is_empty():
        return _empty_svg(width, height, "No equity data")

    rows = equity_vs_btc.sort("ts_ms").to_dicts()
    ts_values = [int(row["ts_ms"]) for row in rows]
    strategy = [float(row["strategy_equity"]) for row in rows]
    btc = [_finite_float(row.get("btc_equity")) for row in rows]
    values = strategy + [item for item in btc if item is not None]
    y_min = min(values + [1.0])
    y_max = max(values + [1.0])
    padding = max((y_max - y_min) * 0.08, 0.02)
    y_min -= padding
    y_max += padding
    ts_min = min(ts_values)
    ts_max = max(ts_values)

    def x_pos(ts_ms: int) -> float:
        if ts_max == ts_min:
            return margin_left + plot_width / 2
        return margin_left + (ts_ms - ts_min) / (ts_max - ts_min) * plot_width

    def y_pos(value: float) -> float:
        if y_max == y_min:
            return margin_top + plot_height / 2
        return margin_top + (y_max - value) / (y_max - y_min) * plot_height

    strategy_points = " ".join(f"{x_pos(int(row['ts_ms'])):.1f},{y_pos(float(row['strategy_equity'])):.1f}" for row in rows)
    btc_points = " ".join(
        f"{x_pos(int(row['ts_ms'])):.1f},{y_pos(float(row['btc_equity'])):.1f}"
        for row in rows
        if row.get("btc_equity") is not None
    )
    grid = _svg_y_grid(margin_left, margin_top, plot_width, plot_height, y_min, y_max, value_fmt=lambda v: f"{v:.2f}x")
    x_labels = _svg_x_labels(rows, margin_left, margin_top, plot_width, plot_height)
    last_strategy = strategy[-1]
    last_btc = next((item for item in reversed(btc) if item is not None), None)
    btc_label = "n/a" if last_btc is None else f"{last_btc - 1.0:.1%}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Volume alpha equity curve vs BTC">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="{margin_left}" y="32" font-family="Arial, sans-serif" font-size="22" font-weight="700" fill="#111827">Volume Alpha Equity Curve vs BTC</text>
  <text x="{margin_left}" y="52" font-family="Arial, sans-serif" font-size="13" fill="#4b5563">Strategy return {last_strategy - 1.0:.1%}; BTC return {btc_label}</text>
  <g>{grid}</g>
  <line x1="{margin_left}" y1="{margin_top + plot_height}" x2="{margin_left + plot_width}" y2="{margin_top + plot_height}" stroke="#111827" stroke-width="1"/>
  <line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{margin_top + plot_height}" stroke="#111827" stroke-width="1"/>
  <polyline points="{strategy_points}" fill="none" stroke="#047857" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
  <polyline points="{btc_points}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
  <g>{x_labels}</g>
  <rect x="{width - 266}" y="22" width="214" height="52" rx="6" fill="#ffffff" stroke="#d1d5db"/>
  <line x1="{width - 246}" y1="42" x2="{width - 212}" y2="42" stroke="#047857" stroke-width="3"/>
  <text x="{width - 202}" y="47" font-family="Arial, sans-serif" font-size="13" fill="#111827">Strategy equity</text>
  <line x1="{width - 246}" y1="62" x2="{width - 212}" y2="62" stroke="#2563eb" stroke-width="3"/>
  <text x="{width - 202}" y="67" font-family="Arial, sans-serif" font-size="13" fill="#111827">BTC normalized</text>
</svg>
"""


def _render_monthly_vs_btc_svg(monthly_vs_btc: pl.DataFrame) -> str:
    width = 1280
    height = 560
    margin_left = 74
    margin_right = 28
    margin_top = 58
    margin_bottom = 110
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    if monthly_vs_btc.is_empty():
        return _empty_svg(width, height, "No monthly data")

    rows = monthly_vs_btc.sort("month").to_dicts()
    values = [float(row["strategy_return"]) for row in rows]
    values.extend(float(row["btc_return"]) for row in rows if row.get("btc_return") is not None)
    abs_max = max(abs(min(values + [0.0])), abs(max(values + [0.0])), 0.01)
    y_min = -abs_max * 1.15
    y_max = abs_max * 1.15

    def y_pos(value: float) -> float:
        return margin_top + (y_max - value) / (y_max - y_min) * plot_height

    zero_y = y_pos(0.0)
    month_width = plot_width / max(len(rows), 1)
    bar_width = max(4.0, min(13.0, month_width * 0.28))
    bars = []
    labels = []
    for index, row in enumerate(rows):
        center = margin_left + month_width * (index + 0.5)
        strategy_return = float(row["strategy_return"])
        btc_return = _finite_float(row.get("btc_return"))
        strategy_color = "#047857" if strategy_return >= 0.0 else "#b91c1c"
        btc_color = "#f59e0b" if (btc_return or 0.0) >= 0.0 else "#2563eb"
        bars.append(_svg_bar(center - bar_width * 0.65, zero_y, y_pos(strategy_return), bar_width, strategy_color))
        if btc_return is not None:
            bars.append(_svg_bar(center + bar_width * 0.65, zero_y, y_pos(btc_return), bar_width, btc_color))
        label = escape(str(row["month"])[2:])
        labels.append(
            f'<text x="{center:.1f}" y="{margin_top + plot_height + 28}" transform="rotate(-45 {center:.1f} {margin_top + plot_height + 28})" '
            'font-family="Arial, sans-serif" font-size="11" text-anchor="end" fill="#374151">'
            f"{label}</text>"
        )

    grid = _svg_y_grid(margin_left, margin_top, plot_width, plot_height, y_min, y_max, value_fmt=lambda v: f"{v:.0%}")
    strategy_total = _compound([float(row["strategy_return"]) for row in rows])
    btc_total = _compound([float(row["btc_return"]) for row in rows if row.get("btc_return") is not None])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Monthly strategy performance versus BTC">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="{margin_left}" y="32" font-family="Arial, sans-serif" font-size="22" font-weight="700" fill="#111827">Monthly Performance vs BTC Regime</text>
  <text x="{margin_left}" y="52" font-family="Arial, sans-serif" font-size="13" fill="#4b5563">Compounded strategy {strategy_total:.1%}; BTC {btc_total:.1%}. Green/red bars are strategy. Orange/blue bars are BTC up/down months.</text>
  <g>{grid}</g>
  <line x1="{margin_left}" y1="{zero_y:.1f}" x2="{margin_left + plot_width}" y2="{zero_y:.1f}" stroke="#111827" stroke-width="1.2"/>
  <g>{"".join(bars)}</g>
  <g>{"".join(labels)}</g>
  <rect x="{width - 290}" y="22" width="246" height="72" rx="6" fill="#ffffff" stroke="#d1d5db"/>
  <rect x="{width - 270}" y="40" width="14" height="14" fill="#047857"/><text x="{width - 248}" y="52" font-family="Arial, sans-serif" font-size="13" fill="#111827">Strategy positive</text>
  <rect x="{width - 270}" y="62" width="14" height="14" fill="#b91c1c"/><text x="{width - 248}" y="74" font-family="Arial, sans-serif" font-size="13" fill="#111827">Strategy negative</text>
  <rect x="{width - 138}" y="40" width="14" height="14" fill="#f59e0b"/><text x="{width - 116}" y="52" font-family="Arial, sans-serif" font-size="13" fill="#111827">BTC up</text>
  <rect x="{width - 138}" y="62" width="14" height="14" fill="#2563eb"/><text x="{width - 116}" y="74" font-family="Arial, sans-serif" font-size="13" fill="#111827">BTC down</text>
</svg>
"""


def _svg_y_grid(
    margin_left: int,
    margin_top: int,
    plot_width: int,
    plot_height: int,
    y_min: float,
    y_max: float,
    *,
    value_fmt,
) -> str:
    lines = []
    for index in range(6):
        value = y_min + (y_max - y_min) * index / 5
        y = margin_top + (y_max - value) / (y_max - y_min) * plot_height if y_max != y_min else margin_top
        lines.append(
            f'<line x1="{margin_left}" y1="{y:.1f}" x2="{margin_left + plot_width}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{margin_left - 10}" y="{y + 4:.1f}" font-family="Arial, sans-serif" font-size="11" text-anchor="end" fill="#6b7280">{escape(value_fmt(value))}</text>'
        )
    return "".join(lines)


def _svg_x_labels(rows: list[dict[str, Any]], margin_left: int, margin_top: int, plot_width: int, plot_height: int) -> str:
    if not rows:
        return ""
    sample_count = min(8, len(rows))
    indexes = sorted({round(index * (len(rows) - 1) / max(sample_count - 1, 1)) for index in range(sample_count)})
    ts_min = int(rows[0]["ts_ms"])
    ts_max = int(rows[-1]["ts_ms"])
    labels = []
    for index in indexes:
        row = rows[index]
        ts_ms = int(row["ts_ms"])
        x = margin_left + (ts_ms - ts_min) / max(ts_max - ts_min, 1) * plot_width
        labels.append(
            f'<text x="{x:.1f}" y="{margin_top + plot_height + 28}" font-family="Arial, sans-serif" font-size="11" text-anchor="middle" fill="#374151">{escape(str(row["date"]))}</text>'
        )
    return "".join(labels)


def _svg_bar(x_center: float, zero_y: float, value_y: float, width: float, color: str) -> str:
    y = min(zero_y, value_y)
    height = max(abs(value_y - zero_y), 1.0)
    return f'<rect x="{x_center - width / 2:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" fill="{color}" opacity="0.88"/>'


def _empty_svg(width: int, height: int, message: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="40" y="50" font-family="Arial, sans-serif" font-size="20" fill="#111827">{escape(message)}</text>
</svg>
"""


def _empty_monthly_vs_btc() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "month": pl.Series([], dtype=pl.String),
            "strategy_return": pl.Series([], dtype=pl.Float64),
            "btc_return": pl.Series([], dtype=pl.Float64),
            "strategy_minus_btc": pl.Series([], dtype=pl.Float64),
            "btc_regime": pl.Series([], dtype=pl.String),
            "strategy_equity": pl.Series([], dtype=pl.Float64),
            "btc_equity": pl.Series([], dtype=pl.Float64),
            "long_return": pl.Series([], dtype=pl.Float64),
            "short_return": pl.Series([], dtype=pl.Float64),
            "cost_return": pl.Series([], dtype=pl.Float64),
            "baskets": pl.Series([], dtype=pl.Int64),
            "trades": pl.Series([], dtype=pl.Int64),
            "trade_win_rate": pl.Series([], dtype=pl.Float64),
            "avg_trade_return": pl.Series([], dtype=pl.Float64),
        }
    )


def _empty_equity_vs_btc() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "date": pl.Series([], dtype=pl.String),
            "strategy_equity": pl.Series([], dtype=pl.Float64),
            "strategy_drawdown": pl.Series([], dtype=pl.Float64),
            "basket_return": pl.Series([], dtype=pl.Float64),
            "btc_equity": pl.Series([], dtype=pl.Float64),
            "btc_return": pl.Series([], dtype=pl.Float64),
        }
    )


def _btc_regime(value: float | None) -> str:
    if value is None:
        return "btc_missing"
    if value > 0.0:
        return "btc_up"
    if value < 0.0:
        return "btc_down"
    return "btc_flat"


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _compound(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= 1.0 + float(value)
    return equity - 1.0


def _format_pct(value: Any) -> str:
    numeric = _finite_float(value)
    return "n/a" if numeric is None else f"{numeric:.2%}"


def _validate_config(config: VolumeBacktestConfig) -> None:
    if config.score not in VOLUME_SCORE_COLUMNS:
        raise ValueError(f"Unknown volume score {config.score!r}; choose one of {sorted(VOLUME_SCORE_COLUMNS)}")
    if not 0.0 < config.quantile <= 0.5:
        raise ValueError("volume backtest quantile must be > 0 and <= 0.5")
    if config.hold_days <= 0 or config.rebalance_days <= 0:
        raise ValueError("hold_days and rebalance_days must be positive")
    if config.rebalance_days < config.hold_days:
        raise ValueError("overlapping baskets are not implemented yet; set rebalance_days >= hold_days")
    if config.gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")
    if config.entry_delay_hours < 0:
        raise ValueError("entry_delay_hours must be non-negative")
    if config.stop_mode not in {"fixed", "none", "volatility"}:
        raise ValueError("stop_mode must be fixed, none, or volatility")
    if not 0.0 <= config.stop_loss_pct < 1.0:
        raise ValueError("stop_loss_pct must be in [0, 1)")
    if config.vol_stop_multiplier < 0.0:
        raise ValueError("vol_stop_multiplier must be non-negative")
    if config.vol_stop_lookback_days < 2:
        raise ValueError("vol_stop_lookback_days must be at least 2")
    if config.min_stop_loss_pct < 0.0 or config.max_stop_loss_pct < 0.0:
        raise ValueError("min/max stop loss pct values must be non-negative")
    if config.max_stop_loss_pct and config.min_stop_loss_pct > config.max_stop_loss_pct:
        raise ValueError("min_stop_loss_pct cannot exceed max_stop_loss_pct")
    if not 0.0 <= config.take_profit_pct < 1.0:
        raise ValueError("take_profit_pct must be in [0, 1) for long/short linear perp tests")
    if config.min_symbols < 4:
        raise ValueError("min_symbols must be at least 4")
    if config.cost_multiplier < 0.0:
        raise ValueError("cost_multiplier must be non-negative")
    if config.side_mode not in {"long_high_short_low", "short_high_long_low"}:
        raise ValueError("side_mode must be long_high_short_low or short_high_long_low")
    if not 0.0 < config.rank_exit_threshold < 1.0:
        raise ValueError("rank_exit_threshold must be between 0 and 1")
    if config.universe_rank_min < 1:
        raise ValueError("universe_rank_min must be at least 1")
    if config.universe_rank_max and config.universe_rank_max < config.universe_rank_min:
        raise ValueError("universe_rank_max must be 0 or >= universe_rank_min")
    if config.universe_min_daily_turnover < 0.0:
        raise ValueError("universe_min_daily_turnover must be non-negative")
    start_ms = _date_boundary_ms(config.start_date)
    end_ms = _date_boundary_ms(config.end_date)
    if start_ms is not None and end_ms is not None and end_ms <= start_ms:
        raise ValueError("end_date must be after start_date")


def _filter_signal_window(features: pl.DataFrame, config: VolumeBacktestConfig) -> pl.DataFrame:
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


def _filter_universe(part: pl.DataFrame, config: VolumeBacktestConfig) -> pl.DataFrame:
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


def _select_basket(part: pl.DataFrame, *, score_col: str, config: VolumeBacktestConfig) -> list[dict[str, Any]]:
    values = part.select(["symbol", score_col]).drop_nulls().sort(score_col)
    values = values.filter(pl.col(score_col).is_finite())
    if values.height < config.min_symbols:
        return []
    bucket = min(max(1, int(math.ceil(values.height * config.quantile))), values.height // 2)
    if bucket < 1:
        return []
    low = values.head(bucket).with_row_index("rank")
    high = values.tail(bucket).with_row_index("rank", offset=values.height - bucket)
    selected: list[dict[str, Any]] = []
    if config.side_mode == "long_high_short_low":
        selected.extend(_side_rows(high, score_col=score_col, side="long"))
        selected.extend(_side_rows(low, score_col=score_col, side="short"))
    else:
        selected.extend(_side_rows(high, score_col=score_col, side="short"))
        selected.extend(_side_rows(low, score_col=score_col, side="long"))
    return selected


def _side_rows(frame: pl.DataFrame, *, score_col: str, side: str) -> list[dict[str, Any]]:
    return [
        {"symbol": str(row["symbol"]), "score": float(row[score_col]), "rank": int(row["rank"]) + 1, "side": side}
        for row in frame.to_dicts()
    ]


def _stop_pct_for_trade(
    *,
    config: VolumeBacktestConfig,
    symbol: str,
    signal_ts_ms: int,
    stop_lookup: dict[tuple[str, int], float],
) -> float | None:
    if config.stop_mode == "none" or (config.stop_mode == "fixed" and config.stop_loss_pct <= 0.0):
        return None
    if config.stop_mode == "fixed":
        return config.stop_loss_pct
    pct = stop_lookup.get((symbol, signal_ts_ms))
    if pct is None or not math.isfinite(pct) or pct <= 0.0:
        return None
    pct *= config.vol_stop_multiplier
    if config.min_stop_loss_pct > 0.0:
        pct = max(pct, config.min_stop_loss_pct)
    if config.max_stop_loss_pct > 0.0:
        pct = min(pct, config.max_stop_loss_pct)
    return pct if pct > 0.0 else None


def _rank_lookup(
    features: pl.DataFrame,
    *,
    score_col: str,
    entry_delay_hours: int,
    config: VolumeBacktestConfig,
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


def _simulate_trade(
    *,
    symbol: str,
    side: str,
    score: float,
    rank: int,
    basket_id: str,
    signal_ts_ms: int,
    entry_bar: dict[str, Any],
    symbol_bars: list[dict[str, Any]],
    planned_exit_ts_ms: int,
    notional_weight: float,
    config: VolumeBacktestConfig,
    round_trip_cost_bps: float,
    stop_pct: float | None,
    rank_lookup: dict[tuple[str, int], float],
    funding_lookup: dict[str, list[tuple[int, float]]] | None,
) -> dict[str, Any] | None:
    entry_ts_ms = int(entry_bar["bar_end_ts_ms"])
    entry_price = float(entry_bar["close"])
    if entry_price <= 0.0:
        return None
    stop_price = _stop_price(entry_price, side=side, stop_loss_pct=stop_pct or 0.0)
    take_profit_price = _take_profit_price(entry_price, side=side, take_profit_pct=config.take_profit_pct)
    exit_price = None
    exit_ts_ms = None
    exit_reason = "max_hold"
    mae = 0.0
    mfe = 0.0
    bars_held = 0

    future_bars = [
        bar
        for bar in symbol_bars
        if int(bar["bar_end_ts_ms"]) > entry_ts_ms and int(bar["bar_end_ts_ms"]) <= planned_exit_ts_ms
    ]
    if not future_bars:
        return None

    for bar in future_bars:
        bars_held += 1
        adverse, favorable = _bar_excursion(entry_price, side=side, high=float(bar["high"]), low=float(bar["low"]))
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        stop_hit, take_profit_hit = _bar_exit_hits(
            side=side,
            high=float(bar["high"]),
            low=float(bar["low"]),
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        if stop_hit:
            exit_price = stop_price
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "stop_loss"
            break
        if take_profit_hit:
            exit_price = take_profit_price
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "take_profit"
            break
        if _rank_exit_hit(
            symbol=symbol,
            side=side,
            side_mode=config.side_mode,
            bar_end_ts_ms=int(bar["bar_end_ts_ms"]),
            rank_lookup=rank_lookup,
            enabled=config.rank_exit_enabled,
            threshold=config.rank_exit_threshold,
        ):
            exit_price = float(bar["close"])
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "rank_exit"
            break

    if exit_price is None:
        last = future_bars[-1]
        exit_price = float(last["close"])
        exit_ts_ms = int(last["bar_end_ts_ms"])
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
    funding_return = abs(notional_weight) * raw_funding_return
    cost_return = -abs(notional_weight) * round_trip_cost_bps / 10_000.0
    gross_return = abs(notional_weight) * gross_trade_return
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
        "notional_weight": abs(notional_weight),
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


def _volatility_stop_lookup(klines: pl.DataFrame, *, lookback_days: int) -> dict[tuple[str, int], float]:
    daily = _daily_close_rows(klines)
    output: dict[tuple[str, int], float] = {}
    for key, part in daily.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        closes = np.asarray(part["close"].to_list(), dtype=float)
        ts_values = [int(item) for item in part["ts_ms"].to_list()]
        if closes.size < lookback_days + 1:
            continue
        log_returns = np.full(closes.shape, np.nan, dtype=float)
        valid = (closes[1:] > 0.0) & (closes[:-1] > 0.0)
        return_values = np.full(closes.size - 1, np.nan, dtype=float)
        return_values[valid] = np.log(closes[1:][valid] / closes[:-1][valid])
        log_returns[1:] = return_values
        for index in range(lookback_days, closes.size):
            window = log_returns[index - lookback_days + 1 : index + 1]
            finite = window[np.isfinite(window)]
            if finite.size >= max(3, lookback_days // 2):
                output[(symbol, ts_values[index])] = float(np.std(finite, ddof=1))
    return output


def _daily_close_rows(klines: pl.DataFrame) -> pl.DataFrame:
    return (
        klines.with_columns((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"))
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg(
            [
                pl.col("close").last().alias("close"),
                pl.len().alias("hourly_bars"),
            ]
        )
        .filter(pl.col("hourly_bars") >= 20)
        .with_columns((pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"))
        .select(["ts_ms", "symbol", "close"])
        .sort(["ts_ms", "symbol"])
    )


def _bar_at_close(symbol_bars: list[dict[str, Any]], close_ts_ms: int) -> dict[str, Any] | None:
    for bar in symbol_bars:
        if int(bar["bar_end_ts_ms"]) == close_ts_ms:
            return bar
    return None


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


def _score_column(score: str) -> str:
    return VOLUME_SCORE_COLUMNS[score]


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


def _exit_reason_counts(trades: pl.DataFrame) -> dict[str, int]:
    counts = {
        "exit_stop_loss": 0,
        "exit_take_profit": 0,
        "exit_rank_exit": 0,
        "exit_max_hold": 0,
        "exit_data_end": 0,
    }
    if trades.is_empty():
        return counts
    for row in trades.group_by("exit_reason").agg(pl.len().alias("count")).to_dicts():
        key = f"exit_{row['exit_reason']}"
        if key in counts:
            counts[key] = int(row["count"])
    return counts


def _attribution_rows(trades: pl.DataFrame, column: str) -> list[dict[str, Any]]:
    if trades.is_empty() or column not in trades.columns:
        return []
    return (
        trades.group_by(column)
        .agg(
            [
                pl.len().alias("trades"),
                pl.col("net_return").sum().alias("net_return"),
                (pl.col("net_return") > 0.0).mean().alias("win_rate"),
                pl.col("net_return").mean().alias("avg_trade_return"),
            ]
        )
        .sort("net_return", descending=True)
        .to_dicts()
    )


def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty():
        return {"start": None, "end": None}
    return {
        "start": datetime.fromtimestamp(int(df["ts_ms"].min()) / 1000, tz=UTC).isoformat(),
        "end": datetime.fromtimestamp(int(df["ts_ms"].max()) / 1000, tz=UTC).isoformat(),
    }


def _iso_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()


def _iso_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m")


def _pct_or_disabled(value: float) -> str:
    return "disabled" if value <= 0.0 else f"{value:.2%}"


def _universe_label(row: dict[str, Any]) -> str:
    rank_min = int(row.get("universe_rank_min", 1) or 1)
    rank_max = int(row.get("universe_rank_max", 0) or 0)
    return f"{rank_min}-{'all' if rank_max <= 0 else rank_max}"


def _currency_or_disabled(value: float) -> str:
    if value <= 0.0:
        return "disabled"
    return f"${value:,.0f}"


def _window_label(row: dict[str, Any]) -> str:
    start = str(row.get("start_date") or "").strip() or "all history"
    end = str(row.get("end_date") or "").strip() or "open ended"
    return f"{start} <= signal < {end}"


def _symbols_or_all(value: list[str] | tuple[str, ...]) -> str:
    return "all downloaded symbols" if not value else ", ".join(value)


def _symbols_or_none(value: list[str] | tuple[str, ...]) -> str:
    return "none" if not value else ", ".join(value)


def _grid_stop_label(row: dict[str, Any]) -> str:
    if row["stop_mode"] == "none":
        return "none"
    if row["stop_mode"] == "volatility":
        return f"vol {row['vol_stop_multiplier']:.1f}x"
    return _pct_or_disabled(float(row["stop_loss_pct"]))


def _format_grid_pick(row: dict[str, Any]) -> str:
    if not row:
        return "No rows."
    return "\n".join(
        [
            f"- Score: `{row['score']}`",
            f"- Hold: {row['hold_days']}d",
            f"- Quantile: {row['quantile']:.0%}",
            f"- Stop: {_grid_stop_label(row)}",
            f"- Rank exit: {row['rank_exit_enabled']}",
            f"- Side mode: `{row['side_mode']}`",
            f"- Total return: {row['total_return']:.2%}",
            f"- Sharpe-like: {row['sharpe_like']:.2f}",
            f"- Max drawdown: {row['max_drawdown']:.2%}",
        ]
    )


def _replace_dataset(
    df: pl.DataFrame,
    data_root: str | Path,
    dataset: str,
    *,
    partition_by: tuple[str, ...],
) -> None:
    shutil.rmtree(dataset_path(data_root, dataset), ignore_errors=True)
    write_dataset(df, data_root, dataset, partition_by=partition_by, append=False)
