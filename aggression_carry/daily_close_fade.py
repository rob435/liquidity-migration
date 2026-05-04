from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
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


@dataclass(frozen=True, slots=True)
class DailyCloseFadeDiagnosticsConfig:
    signal_minutes: tuple[int, ...] = (22 * 60,)
    entry_delay_minutes: tuple[int, ...] = (0, 15, 60)
    horizon_minutes: tuple[int, ...] = (60, 180)
    scores: tuple[str, ...] = ("vol_adjusted_day_return", "day_return", "late_volume_ratio", "vwap_extension", "pump_score")
    top_ns: tuple[int, ...] = (3, 5, 10)
    buckets: int = 10
    min_obs_per_bucket: int = 5
    start_ms: int = 0
    end_ms: int = 0


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


def run_daily_close_fade_diagnostics(
    data_root: str | Path,
    *,
    diagnostics_config: DailyCloseFadeDiagnosticsConfig | None = None,
    base_fade_config: DailyCloseFadeConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    diagnostics = diagnostics_config or DailyCloseFadeDiagnosticsConfig()
    base = base_fade_config or DailyCloseFadeConfig()
    costs = cost_config or CostConfig()
    _validate_close_fade_config(base)
    _validate_close_fade_diagnostics_config(diagnostics)
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * base.cost_multiplier

    features = build_daily_close_fade_features(data_root, config=base, signal_minutes=diagnostics.signal_minutes)
    features = _filter_signal_window(features, diagnostics.start_ms, diagnostics.end_ms)
    observations = build_close_fade_diagnostic_observations(
        data_root,
        features,
        base_config=base,
        diagnostics_config=diagnostics,
    )
    bucket_rows = summarize_close_fade_diagnostic_buckets(observations, diagnostics_config=diagnostics)
    top_rows = summarize_close_fade_diagnostic_top_baskets(
        observations,
        diagnostics_config=diagnostics,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    ic_rows = summarize_close_fade_diagnostic_ic(observations)
    monthly_rows = summarize_close_fade_diagnostic_monthly(
        observations,
        diagnostics_config=diagnostics,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    consistency_rows = summarize_close_fade_diagnostic_month_consistency(monthly_rows)
    scenario_rows = summarize_close_fade_diagnostic_scenarios(
        bucket_rows,
        top_rows,
        ic_rows,
        consistency_rows,
        diagnostics_config=diagnostics,
    )
    payload = {
        "config": {
            "base_fade": asdict(base),
            "diagnostics": asdict(diagnostics),
        },
        "round_trip_cost_bps": round_trip_cost_bps,
        "rows": {
            "features": features.height,
            "observations": observations.height,
            "bucket_rows": bucket_rows.height,
            "top_baskets": top_rows.height,
            "ic_rows": ic_rows.height,
            "monthly_rows": monthly_rows.height,
            "consistency_rows": consistency_rows.height,
            "scenarios": scenario_rows.height,
        },
        "date_range": _date_range(features, "signal_ts_ms"),
        "top_scenarios": scenario_rows.head(25).to_dicts() if not scenario_rows.is_empty() else [],
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "daily_close_fade_diagnostics_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_diagnostics_report.md").write_text(
        format_close_fade_diagnostics_report(payload, bucket_rows, top_rows, ic_rows, monthly_rows, scenario_rows),
        encoding="utf-8",
    )
    if not observations.is_empty():
        observations.write_csv(output_dir / "daily_close_fade_diagnostic_observations.csv")
    if not bucket_rows.is_empty():
        bucket_rows.write_csv(output_dir / "daily_close_fade_diagnostic_buckets.csv")
    if not top_rows.is_empty():
        top_rows.write_csv(output_dir / "daily_close_fade_diagnostic_top_baskets.csv")
    if not ic_rows.is_empty():
        ic_rows.write_csv(output_dir / "daily_close_fade_diagnostic_ic.csv")
    if not scenario_rows.is_empty():
        scenario_rows.write_csv(output_dir / "daily_close_fade_diagnostic_scenarios.csv")
    if not monthly_rows.is_empty():
        monthly_rows.write_csv(output_dir / "daily_close_fade_diagnostic_monthly.csv")
    if not consistency_rows.is_empty():
        consistency_rows.write_csv(output_dir / "daily_close_fade_diagnostic_month_consistency.csv")

    _replace_dataset(bucket_rows, data_root, "daily_close_fade_diagnostic_buckets", partition_by=("score", "signal_minute"))
    _replace_dataset(top_rows, data_root, "daily_close_fade_diagnostic_top_baskets", partition_by=("score", "signal_minute"))
    _replace_dataset(ic_rows, data_root, "daily_close_fade_diagnostic_ic", partition_by=("score", "signal_minute"))
    _replace_dataset(monthly_rows, data_root, "daily_close_fade_diagnostic_monthly", partition_by=("score", "signal_minute", "month"))
    _replace_dataset(
        consistency_rows,
        data_root,
        "daily_close_fade_diagnostic_month_consistency",
        partition_by=("score", "signal_minute"),
    )
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
    features = _filter_signal_window(features, grid.start_ms, grid.end_ms)
    variants = list(iter_close_fade_grid_configs(grid, base))
    tasks = [
        (f"close-fade-{index:04d}", config, costs.base_entry_exit_cost_bps * config.cost_multiplier)
        for index, config in enumerate(variants, start=1)
    ]
    workers = _resolve_workers(max_workers, len(tasks))
    backend = _grid_backend(workers)
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
    elif backend == "thread":
        _init_grid_worker(str(Path(data_root)), features, costs.base_entry_exit_cost_bps)
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
            initargs=(str(Path(data_root)), features, costs.base_entry_exit_cost_bps),
        ) as executor:
            rows = list(executor.map(_evaluate_grid_variant_worker, tasks, chunksize=_grid_chunksize(len(tasks), workers)))

    results = pl.DataFrame(rows, infer_schema_length=None)
    if not results.is_empty():
        results = results.sort(["total_return", "sharpe_like"], descending=[True, True])
    payload = {
        "rows": results.height,
        "workers": workers,
        "worker_backend": backend,
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
        max_position_weight=base_config.max_position_weight,
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
        max_position_weight=base_config.max_position_weight,
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
    return attach_close_fade_coin_market_context(features, config)


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
    df = _filter_coin_quality(df, config)
    if df.is_empty():
        return df
    selected = (
        df.with_columns(pl.col(config.score).rank("ordinal", descending=True).over("signal_ts_ms").alias("entry_rank"))
        .filter(pl.col("entry_rank") <= config.top_n)
        .with_columns(pl.len().over("signal_ts_ms").alias("candidate_count"))
        .filter(pl.col("candidate_count") >= config.min_symbols)
        .sort(["signal_ts_ms", "entry_rank"])
    )
    return attach_close_fade_position_sizing(selected, config)


def attach_close_fade_coin_market_context(features: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    if features.is_empty():
        return features
    if "bar_coverage" not in features.columns:
        return features.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("market_median_day_return"),
                pl.lit(None, dtype=pl.Float64).alias("coin_excess_vs_market"),
            ]
        )
    market = (
        features.filter((pl.col("signal_minute") == config.signal_minute) & (pl.col("bar_coverage") >= 0.95))
        .group_by(["date", "signal_ts_ms"], maintain_order=True)
        .agg(pl.col("day_return").median().alias("market_median_day_return"))
    )
    return features.join(market, on=["date", "signal_ts_ms"], how="left").with_columns(
        (pl.col("day_return") - pl.col("market_median_day_return")).alias("coin_excess_vs_market")
    )


def attach_close_fade_position_sizing(selected: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    if selected.is_empty():
        return selected
    selected_counts = selected.group_by("signal_ts_ms").len(name="selected_count")
    selected = selected.drop("selected_count", strict=False).join(selected_counts, on="signal_ts_ms", how="left")
    sizing = config.position_sizing.strip().lower()
    if sizing == "equal":
        return selected.with_columns((pl.lit(config.gross_exposure) / pl.col("selected_count")).alias("position_target_weight"))
    if sizing != "score_capped":
        raise ValueError(f"Unknown daily close fade position_sizing: {config.position_sizing}")

    rows: list[dict[str, Any]] = []
    for _, group in selected.sort(["signal_ts_ms", "entry_rank"]).group_by("signal_ts_ms", maintain_order=True):
        group_rows = group.to_dicts()
        scores = [
            max(float(row.get(config.score) or row.get("score") or 0.0), EPSILON) ** config.score_weight_power
            for row in group_rows
        ]
        weights = capped_proportional_weights(
            scores,
            gross_exposure=config.gross_exposure,
            max_weight=config.max_position_weight,
        )
        for row, weight in zip(group_rows, weights, strict=True):
            row["position_target_weight"] = weight
            rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None).sort(["signal_ts_ms", "entry_rank"])


def capped_proportional_weights(scores: list[float], *, gross_exposure: float, max_weight: float) -> list[float]:
    if not scores:
        return []
    if max_weight <= 0.0:
        total = sum(max(score, EPSILON) for score in scores)
        return [gross_exposure * max(score, EPSILON) / total for score in scores]

    weights = [0.0] * len(scores)
    remaining = set(range(len(scores)))
    remaining_exposure = gross_exposure
    while remaining and remaining_exposure > EPSILON:
        total_score = sum(max(scores[index], EPSILON) for index in remaining)
        proposed = {
            index: remaining_exposure * max(scores[index], EPSILON) / total_score
            for index in remaining
        } if total_score > EPSILON else {
            index: remaining_exposure / len(remaining)
            for index in remaining
        }
        capped = [index for index, value in proposed.items() if value > max_weight]
        if not capped:
            for index, value in proposed.items():
                weights[index] = value
            break
        for index in capped:
            weights[index] = max_weight
            remaining_exposure -= max_weight
            remaining.remove(index)
        if len(capped) == len(proposed):
            break
    return weights


def _filter_coin_quality(df: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    filters: list[pl.Expr] = []
    if config.coin_excess_vs_market_min > 0.0:
        _require_feature_column(df, "coin_excess_vs_market", "coin_excess_vs_market_min")
        filters.append(pl.col("coin_excess_vs_market").fill_null(-999.0) >= config.coin_excess_vs_market_min)
    if config.coin_vwap_extension_min > 0.0:
        _require_feature_column(df, "vwap_extension", "coin_vwap_extension_min")
        filters.append(pl.col("vwap_extension").fill_null(-999.0) >= config.coin_vwap_extension_min)
    if config.coin_late_volume_ratio_min > 0.0:
        _require_feature_column(df, "late_volume_ratio", "coin_late_volume_ratio_min")
        filters.append(pl.col("late_volume_ratio").fill_null(-999.0) >= config.coin_late_volume_ratio_min)
    for expr in filters:
        df = df.filter(expr)
    return df


def _require_feature_column(df: pl.DataFrame, column: str, knob: str) -> None:
    if column not in df.columns:
        raise ValueError(f"{knob} requires feature column {column!r}")


def build_close_fade_diagnostic_observations(
    data_root: str | Path,
    features: pl.DataFrame,
    *,
    base_config: DailyCloseFadeConfig,
    diagnostics_config: DailyCloseFadeDiagnosticsConfig,
) -> pl.DataFrame:
    if features.is_empty():
        return _empty_diagnostic_observations()
    frames: list[pl.DataFrame] = []
    params = pl.DataFrame(
        [
            {"entry_delay_minutes": entry_delay, "horizon_minutes": horizon}
            for entry_delay in diagnostics_config.entry_delay_minutes
            for horizon in diagnostics_config.horizon_minutes
        ],
        infer_schema_length=None,
    )
    bars = _scan_klines_1m(data_root).select(
        [
            "symbol",
            "ts_ms",
            pl.col("open").cast(pl.Float64).alias("_entry_open"),
            pl.col("close").cast(pl.Float64).alias("_exit_close"),
        ]
    )
    entry_prices = bars.select(
        [
            "symbol",
            pl.col("ts_ms").alias("_entry_target_ts_ms"),
            pl.col("_entry_open").alias("entry_price"),
        ]
    )
    exit_prices = bars.select(
        [
            "symbol",
            pl.col("ts_ms").alias("_exit_target_ts_ms"),
            pl.col("_exit_close").alias("exit_price"),
        ]
    )
    for score in diagnostics_config.scores:
        if score not in features.columns:
            raise ValueError(f"Unknown daily close fade diagnostic score: {score}")
    for signal_minute in diagnostics_config.signal_minutes:
        scenario_config = replace(base_config, signal_minute=signal_minute)
        candidates = _diagnostic_candidate_universe(features, scenario_config)
        if candidates.is_empty():
            continue
        score_frames = [
            candidates.with_columns(
                [
                    pl.lit(score).alias("score"),
                    pl.col(score).cast(pl.Float64).alias("score_value"),
                ]
            ).filter(pl.col("score_value").is_not_null())
            for score in diagnostics_config.scores
        ]
        scored_candidates = _concat_frames(score_frames)
        if scored_candidates.is_empty():
            continue
        frame = (
            scored_candidates.lazy()
                .join(params.lazy(), how="cross")
                .with_columns(
                    [
                        (
                            pl.col("signal_ts_ms")
                            + pl.col("entry_delay_minutes").cast(pl.Int64) * MS_PER_MINUTE
                        ).alias("entry_target_ts_ms"),
                        (
                            pl.col("signal_ts_ms")
                            + (pl.col("entry_delay_minutes").cast(pl.Int64) + pl.col("horizon_minutes").cast(pl.Int64))
                            * MS_PER_MINUTE
                        ).alias("exit_target_ts_ms"),
                    ]
                )
                .join(
                    entry_prices,
                    left_on=["symbol", "entry_target_ts_ms"],
                    right_on=["symbol", "_entry_target_ts_ms"],
                    how="inner",
                )
                .join(
                    exit_prices,
                    left_on=["symbol", "exit_target_ts_ms"],
                    right_on=["symbol", "_exit_target_ts_ms"],
                    how="inner",
                )
                .filter((pl.col("entry_price") > 0.0) & (pl.col("exit_price") > 0.0))
                .with_columns(
                    [
                        pl.col("entry_target_ts_ms").alias("entry_ts_ms"),
                        pl.col("exit_target_ts_ms").alias("exit_ts_ms"),
                        ((pl.col("exit_target_ts_ms") - pl.col("entry_target_ts_ms")) / MS_PER_MINUTE).alias(
                            "actual_horizon_minutes"
                        ),
                        ((pl.col("entry_price") - pl.col("exit_price")) / pl.col("entry_price")).alias(
                            "forward_short_return"
                        ),
                        pl.when(pl.col("signal_close") > 0.0)
                        .then((pl.col("signal_close") - pl.col("entry_price")) / pl.col("signal_close"))
                        .otherwise(0.0)
                        .alias("entry_drift_short_return"),
                        (pl.col("exit_price") < pl.col("entry_price")).alias("hit"),
                        pl.col("baseline_liquidity_rank").fill_null(0).cast(pl.Int64),
                        pl.col("baseline_liquidity_turnover").fill_null(0.0),
                        pl.col("day_turnover").fill_null(0.0),
                        pl.col("last_60m_turnover").fill_null(0.0),
                        pl.col("age_days").fill_null(0.0),
                    ]
                )
                .select(
                    [
                        "score",
                        "score_value",
                        "signal_minute",
                        "entry_delay_minutes",
                        "horizon_minutes",
                        "symbol",
                        "date",
                        "signal_ts_ms",
                        "entry_ts_ms",
                        "exit_ts_ms",
                        "actual_horizon_minutes",
                        "signal_close",
                        "entry_price",
                        "exit_price",
                        "forward_short_return",
                        "entry_drift_short_return",
                        "hit",
                        "day_return",
                        "vol_adjusted_day_return",
                        "late_volume_ratio",
                        "vwap_extension",
                        "pump_score",
                        "pump_like",
                        "baseline_liquidity_rank",
                        "baseline_liquidity_turnover",
                        "day_turnover",
                        "last_60m_turnover",
                        "age_days",
                    ]
                )
                .collect()
        )
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return _empty_diagnostic_observations()
    return _concat_frames(frames).sort(
        ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "signal_ts_ms", "symbol"]
    )


def summarize_close_fade_diagnostic_buckets(
    observations: pl.DataFrame,
    *,
    diagnostics_config: DailyCloseFadeDiagnosticsConfig,
) -> pl.DataFrame:
    if observations.is_empty():
        return pl.DataFrame()
    scenario_cols = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes"]
    ranked = (
        observations.with_columns(
            [
                pl.col("score_value").rank("ordinal").over([*scenario_cols, "signal_ts_ms"]).alias("_score_rank"),
                pl.len().over([*scenario_cols, "signal_ts_ms"]).alias("_score_count"),
            ]
        )
        .with_columns(
            (
                ((pl.col("_score_rank") - 1) * diagnostics_config.buckets / pl.col("_score_count"))
                .floor()
                .cast(pl.Int64)
                + 1
            ).alias("bucket")
        )
        .with_columns(pl.col("bucket").clip(lower_bound=1, upper_bound=diagnostics_config.buckets))
    )
    return (
        ranked.group_by([*scenario_cols, "bucket"], maintain_order=True)
        .agg(
            [
                pl.len().alias("obs"),
                pl.col("forward_short_return").mean().alias("mean_short_return"),
                pl.col("forward_short_return").median().alias("median_short_return"),
                (pl.col("forward_short_return") > 0.0).mean().alias("hit_rate"),
                pl.col("score_value").mean().alias("mean_score"),
                pl.col("entry_drift_short_return").mean().alias("mean_entry_drift_short_return"),
                pl.col("baseline_liquidity_rank").mean().alias("avg_baseline_liquidity_rank"),
            ]
        )
        .with_columns((pl.col("obs") >= diagnostics_config.min_obs_per_bucket).alias("enough_obs"))
        .sort([*scenario_cols, "bucket"])
    )


def summarize_close_fade_diagnostic_top_baskets(
    observations: pl.DataFrame,
    *,
    diagnostics_config: DailyCloseFadeDiagnosticsConfig,
    round_trip_cost_bps: float,
) -> pl.DataFrame:
    if observations.is_empty():
        return pl.DataFrame()
    scenario_cols = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes"]
    cost_return = round_trip_cost_bps / 10_000.0
    frames = []
    for top_n in diagnostics_config.top_ns:
        ranked = observations.with_columns(
            pl.col("score_value").rank("ordinal", descending=True).over([*scenario_cols, "signal_ts_ms"]).alias("entry_rank")
        ).filter(pl.col("entry_rank") <= top_n)
        if ranked.is_empty():
            continue
        baskets = (
            ranked.group_by([*scenario_cols, "signal_ts_ms"], maintain_order=True)
            .agg(
                [
                    pl.col("date").first().alias("date"),
                    pl.len().alias("selection_count"),
                    pl.col("forward_short_return").mean().alias("basket_short_return"),
                    pl.col("entry_drift_short_return").mean().alias("basket_entry_drift_short_return"),
                ]
            )
            .with_columns(pl.lit(top_n).alias("top_n"))
            .with_columns((pl.col("basket_short_return") - cost_return).alias("basket_cost_adjusted_short_return"))
        )
        frames.append(
            baskets.group_by([*scenario_cols, "top_n"], maintain_order=True)
            .agg(
                [
                    pl.len().alias("baskets"),
                    pl.col("selection_count").sum().alias("obs"),
                    pl.col("selection_count").mean().alias("avg_selection_count"),
                    pl.col("basket_short_return").mean().alias("mean_basket_short_return"),
                    pl.col("basket_short_return").median().alias("median_basket_short_return"),
                    (pl.col("basket_short_return") > 0.0).mean().alias("basket_hit_rate"),
                    pl.col("basket_cost_adjusted_short_return").mean().alias("mean_basket_cost_adjusted_short_return"),
                    pl.col("basket_cost_adjusted_short_return").median().alias(
                        "median_basket_cost_adjusted_short_return"
                    ),
                    (pl.col("basket_cost_adjusted_short_return") > 0.0).mean().alias("cost_adjusted_basket_hit_rate"),
                    pl.col("basket_entry_drift_short_return").mean().alias("mean_entry_drift_short_return"),
                ]
            )
        )
    return _concat_frames(frames).sort([*scenario_cols, "top_n"]) if frames else pl.DataFrame()


def summarize_close_fade_diagnostic_monthly(
    observations: pl.DataFrame,
    *,
    diagnostics_config: DailyCloseFadeDiagnosticsConfig,
    round_trip_cost_bps: float,
) -> pl.DataFrame:
    if observations.is_empty():
        return pl.DataFrame()
    scenario_cols = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes"]
    cost_return = round_trip_cost_bps / 10_000.0
    frames = []
    for top_n in diagnostics_config.top_ns:
        ranked = observations.with_columns(
            pl.col("score_value").rank("ordinal", descending=True).over([*scenario_cols, "signal_ts_ms"]).alias("entry_rank")
        ).filter(pl.col("entry_rank") <= top_n)
        if ranked.is_empty():
            continue
        baskets = (
            ranked.group_by([*scenario_cols, "signal_ts_ms"], maintain_order=True)
            .agg(
                [
                    pl.col("date").first().alias("date"),
                    pl.len().alias("selection_count"),
                    pl.col("forward_short_return").mean().alias("basket_short_return"),
                    pl.col("entry_drift_short_return").mean().alias("basket_entry_drift_short_return"),
                ]
            )
            .with_columns(
                [
                    pl.lit(top_n).alias("top_n"),
                    pl.col("date").str.slice(0, 7).alias("month"),
                    (pl.col("basket_short_return") - cost_return).alias("basket_cost_adjusted_short_return"),
                ]
            )
        )
        frames.append(
            baskets.group_by([*scenario_cols, "top_n", "month"], maintain_order=True)
            .agg(
                [
                    pl.len().alias("baskets"),
                    pl.col("selection_count").sum().alias("obs"),
                    pl.col("selection_count").mean().alias("avg_selection_count"),
                    pl.col("basket_short_return").mean().alias("mean_basket_short_return"),
                    pl.col("basket_short_return").median().alias("median_basket_short_return"),
                    (pl.col("basket_short_return") > 0.0).mean().alias("basket_hit_rate"),
                    pl.col("basket_cost_adjusted_short_return").mean().alias("mean_basket_cost_adjusted_short_return"),
                    (pl.col("basket_cost_adjusted_short_return") > 0.0).mean().alias("cost_adjusted_basket_hit_rate"),
                    pl.col("basket_entry_drift_short_return").mean().alias("mean_entry_drift_short_return"),
                ]
            )
        )
    return _concat_frames(frames).sort([*scenario_cols, "top_n", "month"]) if frames else pl.DataFrame()


def summarize_close_fade_diagnostic_month_consistency(monthly_rows: pl.DataFrame) -> pl.DataFrame:
    if monthly_rows.is_empty():
        return pl.DataFrame()
    scenario_cols = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n"]
    return (
        monthly_rows.group_by(scenario_cols, maintain_order=True)
        .agg(
            [
                (pl.col("mean_basket_short_return") > 0.0).sum().alias("positive_months"),
                (pl.col("mean_basket_cost_adjusted_short_return") > 0.0).sum().alias("cost_positive_months"),
                pl.len().alias("total_months"),
                pl.col("mean_basket_short_return").min().alias("worst_month_short_return"),
                pl.col("mean_basket_short_return").max().alias("best_month_short_return"),
                pl.col("mean_basket_cost_adjusted_short_return").min().alias("worst_month_cost_adjusted_short_return"),
                pl.col("mean_basket_cost_adjusted_short_return").max().alias("best_month_cost_adjusted_short_return"),
            ]
        )
        .with_columns(
            [
                (pl.col("positive_months") / pl.col("total_months")).alias("positive_month_rate"),
                (pl.col("cost_positive_months") / pl.col("total_months")).alias("cost_positive_month_rate"),
            ]
        )
        .sort(
            ["cost_positive_month_rate", "positive_month_rate", "best_month_cost_adjusted_short_return"],
            descending=[True, True, True],
        )
    )


def summarize_close_fade_diagnostic_ic(observations: pl.DataFrame) -> pl.DataFrame:
    if observations.is_empty():
        return pl.DataFrame()
    scenario_cols = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes"]
    group_cols = [*scenario_cols, "signal_ts_ms"]
    ranked = observations.with_columns(
        [
            pl.col("score_value").rank("average").over(group_cols).alias("_score_rank"),
            pl.col("forward_short_return").rank("average").over(group_cols).alias("_return_rank"),
        ]
    )
    per_signal = (
        ranked.group_by(group_cols, maintain_order=True)
        .agg(
            [
                pl.col("date").first().alias("date"),
                pl.len().alias("obs"),
                pl.col("_score_rank").sum().alias("_sum_x"),
                pl.col("_return_rank").sum().alias("_sum_y"),
                (pl.col("_score_rank") * pl.col("_score_rank")).sum().alias("_sum_x2"),
                (pl.col("_return_rank") * pl.col("_return_rank")).sum().alias("_sum_y2"),
                (pl.col("_score_rank") * pl.col("_return_rank")).sum().alias("_sum_xy"),
            ]
        )
        .with_columns(
            [
                (
                    pl.col("obs") * pl.col("_sum_xy") - pl.col("_sum_x") * pl.col("_sum_y")
                ).alias("_corr_num"),
                (
                    (
                        pl.col("obs") * pl.col("_sum_x2") - pl.col("_sum_x") * pl.col("_sum_x")
                    )
                    * (
                        pl.col("obs") * pl.col("_sum_y2") - pl.col("_sum_y") * pl.col("_sum_y")
                    )
                ).sqrt().alias("_corr_den"),
            ]
        )
        .with_columns(
            pl.when(pl.col("_corr_den") > EPSILON)
            .then(pl.col("_corr_num") / pl.col("_corr_den"))
            .otherwise(None)
            .alias("ic")
        )
        .filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
        .select([*group_cols, "date", "ic", "obs"])
    )
    if per_signal.is_empty():
        return pl.DataFrame()
    return (
        per_signal.group_by(scenario_cols, maintain_order=True)
        .agg(
            [
                pl.len().alias("signal_count"),
                pl.col("obs").sum().alias("ic_obs"),
                pl.col("ic").mean().alias("mean_ic"),
                pl.col("ic").std().alias("ic_std"),
                (pl.col("ic") > 0.0).mean().alias("positive_ic_rate"),
            ]
        )
        .with_columns(
            pl.when(pl.col("ic_std").fill_null(0.0) > EPSILON)
            .then(pl.col("mean_ic") / (pl.col("ic_std") / pl.col("signal_count").sqrt()))
            .otherwise(0.0)
            .alias("ic_t_stat")
        )
        .sort(scenario_cols)
    )


def summarize_close_fade_diagnostic_scenarios(
    bucket_rows: pl.DataFrame,
    top_rows: pl.DataFrame,
    ic_rows: pl.DataFrame,
    consistency_rows: pl.DataFrame,
    *,
    diagnostics_config: DailyCloseFadeDiagnosticsConfig,
) -> pl.DataFrame:
    if top_rows.is_empty():
        return pl.DataFrame()
    scenario_cols = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes"]
    edge_rows: list[dict[str, Any]] = []
    if not bucket_rows.is_empty():
        for key, part in bucket_rows.group_by(scenario_cols, maintain_order=True):
            key_values = key if isinstance(key, tuple) else (key,)
            low = part.filter(pl.col("bucket") == 1)
            high = part.filter(pl.col("bucket") == diagnostics_config.buckets)
            if low.is_empty() or high.is_empty():
                continue
            low_row = low.row(0, named=True)
            high_row = high.row(0, named=True)
            edge_rows.append(
                {
                    **dict(zip(scenario_cols, key_values, strict=True)),
                    "low_bucket_mean": float(low_row["mean_short_return"]),
                    "high_bucket_mean": float(high_row["mean_short_return"]),
                    "high_minus_low": float(high_row["mean_short_return"]) - float(low_row["mean_short_return"]),
                    "low_bucket_obs": int(low_row["obs"]),
                    "high_bucket_obs": int(high_row["obs"]),
                }
            )
    edge_frame = pl.DataFrame(edge_rows, infer_schema_length=None) if edge_rows else pl.DataFrame()
    output = top_rows
    if not edge_frame.is_empty():
        output = output.join(edge_frame, on=scenario_cols, how="left")
    if not ic_rows.is_empty():
        output = output.join(ic_rows, on=scenario_cols, how="left")
    if not consistency_rows.is_empty():
        output = output.join(consistency_rows, on=[*scenario_cols, "top_n"], how="left")
    optional_columns = {
        "high_minus_low": pl.Float64,
        "mean_ic": pl.Float64,
        "ic_t_stat": pl.Float64,
        "positive_ic_rate": pl.Float64,
        "signal_count": pl.Int64,
        "ic_obs": pl.Int64,
        "mean_basket_cost_adjusted_short_return": pl.Float64,
        "cost_adjusted_basket_hit_rate": pl.Float64,
        "positive_month_rate": pl.Float64,
        "cost_positive_month_rate": pl.Float64,
        "total_months": pl.Int64,
        "worst_month_cost_adjusted_short_return": pl.Float64,
    }
    missing_columns = [
        pl.lit(None, dtype=dtype).alias(name)
        for name, dtype in optional_columns.items()
        if name not in output.columns
    ]
    if missing_columns:
        output = output.with_columns(missing_columns)
    return (
        output.with_columns(
            (
                (pl.col("mean_basket_short_return") > 0.0)
                & (pl.col("high_minus_low").fill_null(0.0) > 0.0)
                & (pl.col("mean_ic").fill_null(0.0) > 0.0)
            ).alias("robust_direction_pass")
        )
        .with_columns(
            (
                pl.col("robust_direction_pass")
                & (pl.col("mean_basket_cost_adjusted_short_return").fill_null(-1.0) > 0.0)
                & (pl.col("cost_positive_month_rate").fill_null(0.0) >= 0.5)
            ).alias("cost_edge_pass")
        )
        .sort(
            [
                "cost_edge_pass",
                "robust_direction_pass",
                "mean_basket_cost_adjusted_short_return",
                "cost_positive_month_rate",
                "mean_basket_short_return",
                "high_minus_low",
                "mean_ic",
            ],
            descending=[True, True, True, True, True, True, True],
        )
    )


def _diagnostic_candidate_features(features: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    df = _diagnostic_candidate_universe(features, config)
    return df.filter(pl.col(config.score).is_not_null()).sort(["signal_ts_ms", "symbol"])


def _diagnostic_candidate_universe(features: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    df = features.filter((pl.col("eligible")) & (pl.col("signal_minute") == config.signal_minute))
    df = df.filter(_candidate_liquidity_filter_expr(config))
    if config.pump_filter == "pump":
        df = df.filter(pl.col("pump_like"))
    elif config.pump_filter == "non_pump":
        df = df.filter(~pl.col("pump_like"))
    elif config.pump_filter != "all":
        raise ValueError(f"Unknown pump_filter: {config.pump_filter}")
    return df.sort(["signal_ts_ms", "symbol"])


def _filter_signal_window(df: pl.DataFrame, start_ms: int, end_ms: int) -> pl.DataFrame:
    if df.is_empty():
        return df
    output = df
    if start_ms:
        output = output.filter(pl.col("signal_ts_ms") >= start_ms)
    if end_ms:
        output = output.filter(pl.col("signal_ts_ms") < end_ms)
    return output


def _diagnostic_forward_return_row(
    data_root: str | Path,
    feature: dict[str, Any],
    *,
    score: str,
    entry_delay_minutes: int,
    horizon_minutes: int,
    partition_cache: dict[tuple[str, str], pl.DataFrame],
) -> dict[str, Any] | None:
    symbol = str(feature["symbol"])
    signal_ts_ms = int(feature["signal_ts_ms"])
    entry_target_ts_ms = signal_ts_ms + entry_delay_minutes * MS_PER_MINUTE
    exit_target_ts_ms = entry_target_ts_ms + horizon_minutes * MS_PER_MINUTE
    window = _load_trade_window(data_root, symbol, entry_target_ts_ms, exit_target_ts_ms, partition_cache)
    if window.is_empty():
        return None
    ordered = window.sort("ts_ms")
    entry_candidates = ordered.filter(pl.col("ts_ms") >= entry_target_ts_ms)
    if entry_candidates.is_empty():
        return None
    entry_bar = entry_candidates.row(0, named=True)
    exit_candidates = ordered.filter(pl.col("ts_ms") >= int(entry_bar["ts_ms"]))
    if exit_candidates.is_empty():
        return None
    exit_bar = exit_candidates.tail(1).row(0, named=True)
    actual_minutes = (int(exit_bar["ts_ms"]) - int(entry_bar["ts_ms"])) / MS_PER_MINUTE
    if actual_minutes <= 0.0 or actual_minutes + EPSILON < horizon_minutes * 0.8:
        return None
    entry_price = float(entry_bar["open"])
    exit_price = float(exit_bar["close"])
    if entry_price <= 0.0 or exit_price <= 0.0:
        return None
    signal_close = float(feature.get("signal_close") or 0.0)
    return {
        "score": score,
        "score_value": float(feature.get(score) or 0.0),
        "signal_minute": int(feature["signal_minute"]),
        "entry_delay_minutes": entry_delay_minutes,
        "horizon_minutes": horizon_minutes,
        "symbol": symbol,
        "date": str(feature["date"]),
        "signal_ts_ms": signal_ts_ms,
        "entry_ts_ms": int(entry_bar["ts_ms"]),
        "exit_ts_ms": int(exit_bar["ts_ms"]),
        "actual_horizon_minutes": actual_minutes,
        "signal_close": signal_close,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "forward_short_return": _short_return(entry_price, exit_price),
        "entry_drift_short_return": _short_return(signal_close, entry_price) if signal_close > 0.0 else 0.0,
        "hit": exit_price < entry_price,
        "day_return": float(feature.get("day_return") or 0.0),
        "vol_adjusted_day_return": float(feature.get("vol_adjusted_day_return") or 0.0),
        "late_volume_ratio": float(feature.get("late_volume_ratio") or 0.0),
        "vwap_extension": float(feature.get("vwap_extension") or 0.0),
        "pump_score": int(feature.get("pump_score") or 0),
        "pump_like": bool(feature.get("pump_like")),
        "baseline_liquidity_rank": int(feature.get("baseline_liquidity_rank") or 0),
        "baseline_liquidity_turnover": float(feature.get("baseline_liquidity_turnover") or 0.0),
        "day_turnover": float(feature.get("day_turnover") or 0.0),
        "last_60m_turnover": float(feature.get("last_60m_turnover") or 0.0),
        "age_days": float(feature.get("age_days") or 0.0),
    }


def _validate_close_fade_diagnostics_config(config: DailyCloseFadeDiagnosticsConfig) -> None:
    if not config.signal_minutes:
        raise ValueError("daily close fade diagnostics need at least one signal minute")
    if not config.entry_delay_minutes or any(item < 0 for item in config.entry_delay_minutes):
        raise ValueError("daily close fade diagnostics entry delays must be non-negative")
    if not config.horizon_minutes or any(item <= 0 for item in config.horizon_minutes):
        raise ValueError("daily close fade diagnostics horizons must be positive")
    if not config.scores:
        raise ValueError("daily close fade diagnostics need at least one score")
    if not config.top_ns or any(item <= 0 for item in config.top_ns):
        raise ValueError("daily close fade diagnostics top_ns must be positive")
    if config.buckets < 2:
        raise ValueError("daily close fade diagnostics buckets must be at least 2")
    if config.min_obs_per_bucket < 1:
        raise ValueError("daily close fade diagnostics min_obs_per_bucket must be positive")
    if config.start_ms and config.end_ms and config.end_ms <= config.start_ms:
        raise ValueError("daily close fade diagnostics end_ms must be after start_ms")


def _empty_diagnostic_observations() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "score": pl.Series([], dtype=pl.String),
            "score_value": pl.Series([], dtype=pl.Float64),
            "signal_minute": pl.Series([], dtype=pl.Int64),
            "entry_delay_minutes": pl.Series([], dtype=pl.Int64),
            "horizon_minutes": pl.Series([], dtype=pl.Int64),
            "symbol": pl.Series([], dtype=pl.String),
            "date": pl.Series([], dtype=pl.String),
            "signal_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "actual_horizon_minutes": pl.Series([], dtype=pl.Float64),
            "signal_close": pl.Series([], dtype=pl.Float64),
            "entry_price": pl.Series([], dtype=pl.Float64),
            "exit_price": pl.Series([], dtype=pl.Float64),
            "forward_short_return": pl.Series([], dtype=pl.Float64),
            "entry_drift_short_return": pl.Series([], dtype=pl.Float64),
            "hit": pl.Series([], dtype=pl.Boolean),
            "day_return": pl.Series([], dtype=pl.Float64),
            "vol_adjusted_day_return": pl.Series([], dtype=pl.Float64),
            "late_volume_ratio": pl.Series([], dtype=pl.Float64),
            "vwap_extension": pl.Series([], dtype=pl.Float64),
            "pump_score": pl.Series([], dtype=pl.Int64),
            "pump_like": pl.Series([], dtype=pl.Boolean),
            "baseline_liquidity_rank": pl.Series([], dtype=pl.Int64),
            "baseline_liquidity_turnover": pl.Series([], dtype=pl.Float64),
            "day_turnover": pl.Series([], dtype=pl.Float64),
            "last_60m_turnover": pl.Series([], dtype=pl.Float64),
            "age_days": pl.Series([], dtype=pl.Float64),
        }
    )


def _validate_close_fade_config(config: DailyCloseFadeConfig) -> None:
    if config.top_n <= 0:
        raise ValueError("daily close fade top_n must be positive")
    if config.hold_minutes <= 0:
        raise ValueError("daily close fade hold_minutes must be positive")
    if config.entry_delay_minutes < 0:
        raise ValueError("daily close fade entry_delay_minutes must be non-negative")
    if config.entry_twap_minutes < 0:
        raise ValueError("daily close fade entry_twap_minutes must be non-negative")
    if config.gross_exposure <= 0.0:
        raise ValueError("daily close fade gross_exposure must be positive")
    if config.coin_excess_vs_market_min < 0.0:
        raise ValueError("daily close fade coin_excess_vs_market_min must be non-negative")
    if config.coin_vwap_extension_min < 0.0:
        raise ValueError("daily close fade coin_vwap_extension_min must be non-negative")
    if config.coin_late_volume_ratio_min < 0.0:
        raise ValueError("daily close fade coin_late_volume_ratio_min must be non-negative")
    if config.position_sizing.strip().lower() not in {"equal", "score_capped"}:
        raise ValueError("daily close fade position_sizing must be equal or score_capped")
    if config.score_weight_power < 0.0:
        raise ValueError("daily close fade score_weight_power must be non-negative")
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
        "entry_delay_minutes": config.entry_delay_minutes,
        "entry_twap_minutes": config.entry_twap_minutes,
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


def format_close_fade_diagnostics_report(
    payload: dict[str, Any],
    bucket_rows: pl.DataFrame,
    top_rows: pl.DataFrame,
    ic_rows: pl.DataFrame,
    monthly_rows: pl.DataFrame,
    scenario_rows: pl.DataFrame,
) -> str:
    base = payload.get("config", {}).get("base_fade", {})
    diagnostics = payload.get("config", {}).get("diagnostics", {})
    lines = [
        "# Daily Close Fade Diagnostics",
        "",
        "This report tests whether high daily-close pump scores predict later short returns.",
        "It intentionally ignores TP, SL, trailing stops, basket stops, and compounding.",
        "",
        f"Rows: features={payload.get('rows', {}).get('features', 0)} "
        f"observations={payload.get('rows', {}).get('observations', 0)} "
        f"scenarios={payload.get('rows', {}).get('scenarios', 0)}",
        f"Date range: {payload.get('date_range', {}).get('start')} to {payload.get('date_range', {}).get('end')}",
        f"Round-trip cost assumption: {_num(payload.get('round_trip_cost_bps'), 2)} bps",
        "",
        "## Candidate Filters",
        "",
        f"- pump_filter: {base.get('pump_filter', 'all')}",
        f"- min_age_days: {base.get('min_age_days', 10)}",
        f"- baseline_liquidity: lookback={base.get('liquidity_lookback_days', 7)}d "
        f"rank={base.get('liquidity_rank_min', 1)}-{base.get('liquidity_rank_max', 0) or 'unbounded'}",
        f"- turnover_filters: day>={base.get('min_day_turnover', 0.0):,.0f} "
        f"last_60m>={base.get('min_last_60m_turnover', 0.0):,.0f} "
        f"baseline>={base.get('min_baseline_turnover', 0.0):,.0f}",
        f"- require_archive_membership: {base.get('require_archive_membership', False)}",
        "",
        "## Diagnostic Grid",
        "",
        f"- signal_times: {', '.join(_format_signal_minute(item) for item in diagnostics.get('signal_minutes', ())) or 'none'} UTC",
        f"- entry_delays_minutes: {', '.join(str(item) for item in diagnostics.get('entry_delay_minutes', ())) or 'none'}",
        f"- horizons_minutes: {', '.join(str(item) for item in diagnostics.get('horizon_minutes', ())) or 'none'}",
        f"- scores: {', '.join(diagnostics.get('scores', ())) or 'none'}",
        f"- top_ns: {', '.join(str(item) for item in diagnostics.get('top_ns', ())) or 'none'}",
        f"- buckets: {diagnostics.get('buckets', 0)}",
        f"- start: {_dt_from_ms(int(diagnostics['start_ms'])).isoformat() if diagnostics.get('start_ms') else 'unbounded'}",
        f"- end: {_dt_from_ms(int(diagnostics['end_ms'])).isoformat() if diagnostics.get('end_ms') else 'unbounded'}",
        "",
        "## Best Cost-Aware Direction Scenarios",
        "",
        "| Rank | Cost Pass | Raw Pass | Mean Gross | Mean Cost Adj | Cost+ Months | Worst Cost Month | High-Low Bucket | Mean IC | IC t-stat | Signal | Delay | Horizon | Top N | Score | Baskets | Obs |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for index, row in enumerate(scenario_rows.head(25).to_dicts() if not scenario_rows.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('cost_edge_pass', False)} | {row.get('robust_direction_pass', False)} | "
            f"{_pct(row.get('mean_basket_short_return'))} | "
            f"{_pct(row.get('mean_basket_cost_adjusted_short_return'))} | "
            f"{_pct(row.get('cost_positive_month_rate'))} | "
            f"{_pct(row.get('worst_month_cost_adjusted_short_return'))} | "
            f"{_pct(row.get('high_minus_low'))} | "
            f"{_num(row.get('mean_ic'), 4)} | {_num(row.get('ic_t_stat'), 2)} | "
            f"{_format_signal_minute(int(row.get('signal_minute', 0)))} | "
            f"{row.get('entry_delay_minutes', 0)} | {row.get('horizon_minutes', 0)} | "
            f"{row.get('top_n', 0)} | {row.get('score', '')} | "
            f"{row.get('baskets', 0)} | {row.get('obs', 0)} |"
        )
    if scenario_rows.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## IC Summary",
            "",
            "| Signal | Delay | Horizon | Score | Mean IC | IC t-stat | Positive IC Rate | Signal Count | Obs |",
            "|---:|---:|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in ic_rows.sort(["mean_ic", "ic_t_stat"], descending=[True, True]).head(25).to_dicts() if not ic_rows.is_empty() else []:
        lines.append(
            f"| {_format_signal_minute(int(row.get('signal_minute', 0)))} | "
            f"{row.get('entry_delay_minutes', 0)} | {row.get('horizon_minutes', 0)} | {row.get('score', '')} | "
            f"{_num(row.get('mean_ic'), 4)} | {_num(row.get('ic_t_stat'), 2)} | "
            f"{_pct(row.get('positive_ic_rate'))} | {row.get('signal_count', 0)} | {row.get('ic_obs', 0)} |"
        )

    lines.extend(
        [
            "",
            "## Monthly Consistency",
            "",
            "This table shows the same top-basket raw edge split by month. A scenario that only wins in one cluster is weaker than one that survives several months.",
            "",
            "| Month | Signal | Delay | Horizon | Top N | Score | Mean Gross | Mean Cost Adj | Cost Hit | Baskets | Obs |",
            "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    if not monthly_rows.is_empty():
        top_keys = (
            scenario_rows.head(5)
            .select(["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n"])
            .with_columns(pl.lit(True).alias("_top_scenario"))
        )
        selected = monthly_rows.join(
            top_keys,
            on=["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n"],
            how="inner",
        )
        for row in selected.sort(["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n", "month"]).to_dicts():
            lines.append(
                f"| {row.get('month', '')} | {_format_signal_minute(int(row.get('signal_minute', 0)))} | "
                f"{row.get('entry_delay_minutes', 0)} | {row.get('horizon_minutes', 0)} | {row.get('top_n', 0)} | "
                f"{row.get('score', '')} | {_pct(row.get('mean_basket_short_return'))} | "
                f"{_pct(row.get('mean_basket_cost_adjusted_short_return'))} | "
                f"{_pct(row.get('cost_adjusted_basket_hit_rate'))} | "
                f"{row.get('baskets', 0)} | {row.get('obs', 0)} |"
            )

    lines.extend(
        [
            "",
            "## Bucket Check",
            "",
            "High score buckets should have higher short returns than low score buckets. If that shape is absent, TP/SL optimization is probably fitting noise.",
            "",
            "| Signal | Delay | Horizon | Score | Bucket | Mean Short | Hit Rate | Obs | Enough Obs |",
            "|---:|---:|---:|---|---:|---:|---:|---:|---|",
        ]
    )
    if not bucket_rows.is_empty():
        buckets = diagnostics.get("buckets", 10)
        extreme = bucket_rows.filter(pl.col("bucket").is_in([1, int(buckets)]))
        for row in extreme.sort(["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "bucket"]).head(60).to_dicts():
            lines.append(
                f"| {_format_signal_minute(int(row.get('signal_minute', 0)))} | "
                f"{row.get('entry_delay_minutes', 0)} | {row.get('horizon_minutes', 0)} | {row.get('score', '')} | "
                f"{row.get('bucket', 0)} | {_pct(row.get('mean_short_return'))} | {_pct(row.get('hit_rate'))} | "
                f"{row.get('obs', 0)} | {row.get('enough_obs', False)} |"
            )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_diagnostic_observations.csv",
            "daily_close_fade_diagnostic_buckets.csv",
            "daily_close_fade_diagnostic_top_baskets.csv",
            "daily_close_fade_diagnostic_ic.csv",
            "daily_close_fade_diagnostic_scenarios.csv",
            "daily_close_fade_diagnostic_monthly.csv",
            "daily_close_fade_diagnostic_month_consistency.csv",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


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
        f"- entry: delay={config.get('entry_delay_minutes', 0)}m twap={config.get('entry_twap_minutes', 0)}m",
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
    entry_start_ts_ms = signal_ts_ms + config.entry_delay_minutes * MS_PER_MINUTE
    twap_minutes = max(config.entry_twap_minutes, 0)
    required_fills = max(twap_minutes, 1)
    entry_complete_ts_ms = entry_start_ts_ms + twap_minutes * MS_PER_MINUTE
    target_exit_ts_ms = entry_complete_ts_ms + config.hold_minutes * MS_PER_MINUTE
    window = _load_trade_window(data_root, symbol, entry_start_ts_ms, target_exit_ts_ms, partition_cache)
    if window.is_empty():
        return None
    bars = window.filter(pl.col("ts_ms") >= entry_start_ts_ms).sort("ts_ms").to_dicts()
    if not bars:
        return None

    exit_ts_ms = int(bars[-1]["ts_ms"])
    exit_price = float(bars[-1]["close"])
    exit_reason = "max_hold" if exit_ts_ms >= target_exit_ts_ms else "data_end"
    stop_active_ts_ms = entry_start_ts_ms + config.stop_delay_minutes * MS_PER_MINUTE
    profit_active_ts_ms = entry_complete_ts_ms + config.stop_delay_minutes * MS_PER_MINUTE
    realized_vol = max(float(row.get("realized_vol") or 0.0), 0.0)
    vol_trailing_stop_pct = realized_vol * config.vol_trailing_stop_mult if config.vol_trailing_stop_mult > 0.0 else 0.0
    vol_trailing_activation_pct = (
        realized_vol * config.vol_trailing_activation_mult if config.vol_trailing_stop_mult > 0.0 else 0.0
    )
    trailing_active = False
    vol_trailing_active = False
    mfe_giveback_active = False
    entry_price = 0.0
    entry_fill_sum = 0.0
    entry_fill_count = 0
    entry_ts_ms: int | None = None
    best_price: float | None = None
    max_favorable = 0.0
    max_adverse = 0.0

    for bar in bars:
        ts_ms = int(bar["ts_ms"])
        if ts_ms < entry_start_ts_ms:
            continue
        if twap_minutes <= 0 and entry_fill_count == 0:
            entry_fill_sum = float(bar["open"])
            entry_fill_count = 1
            entry_price = entry_fill_sum
            entry_ts_ms = ts_ms
            best_price = entry_price
            entry_complete_ts_ms = ts_ms
            target_exit_ts_ms = entry_complete_ts_ms + config.hold_minutes * MS_PER_MINUTE
            profit_active_ts_ms = entry_complete_ts_ms + config.stop_delay_minutes * MS_PER_MINUTE
        elif twap_minutes > 0 and entry_start_ts_ms <= ts_ms < entry_complete_ts_ms:
            entry_fill_sum += float(bar["open"])
            entry_fill_count += 1
            entry_price = entry_fill_sum / entry_fill_count
            if entry_ts_ms is None:
                entry_ts_ms = ts_ms
            best_price = entry_price if best_price is None else min(best_price, entry_price)

        if entry_fill_count <= 0:
            continue

        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        max_favorable = max(max_favorable, _short_return(entry_price, low))
        max_adverse = min(max_adverse, _short_return(entry_price, high))

        hard_stop_price = entry_price * (1.0 + config.stop_loss_pct) if config.stop_loss_pct > 0.0 else None
        if ts_ms >= stop_active_ts_ms and hard_stop_price is not None and high >= hard_stop_price:
            exit_ts_ms = ts_ms
            exit_price = hard_stop_price
            exit_reason = "stop_loss"
            break

        profit_exits_active = ts_ms >= profit_active_ts_ms and entry_fill_count >= required_fills
        if profit_exits_active:
            protective_exits: list[tuple[float, str]] = []
            if trailing_active and config.trailing_stop_pct > 0.0:
                trailing_stop = float(best_price) * (1.0 + config.trailing_stop_pct)
                if high >= trailing_stop:
                    protective_exits.append((trailing_stop, "trailing_stop"))
            if vol_trailing_active and vol_trailing_stop_pct > 0.0:
                vol_trailing_stop = float(best_price) * (1.0 + vol_trailing_stop_pct)
                if high >= vol_trailing_stop:
                    protective_exits.append((vol_trailing_stop, "vol_trailing_stop"))
            if mfe_giveback_active and config.mfe_giveback_pct > 0.0:
                mfe_stop = _mfe_giveback_stop_price(entry_price, float(best_price), config.mfe_giveback_pct)
                if high >= mfe_stop:
                    protective_exits.append((mfe_stop, "mfe_giveback"))
            if protective_exits:
                exit_price, exit_reason = max(protective_exits, key=lambda item: item[0])
                exit_ts_ms = ts_ms
                break

        take_profit_price = entry_price * (1.0 - config.take_profit_pct) if config.take_profit_pct > 0.0 else None
        if profit_exits_active and take_profit_price is not None and low <= take_profit_price:
            exit_ts_ms = ts_ms
            exit_price = take_profit_price
            exit_reason = "take_profit"
            break

        vwap_exit_price = _vwap_reversion_exit_price(entry_price, row, config)
        if profit_exits_active and vwap_exit_price is not None and low <= vwap_exit_price:
            exit_ts_ms = ts_ms
            exit_price = vwap_exit_price
            exit_reason = "vwap_reversion"
            break

        if best_price is None or low < best_price:
            best_price = low

        if profit_exits_active:
            best_return = _short_return(entry_price, float(best_price))
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

    if entry_fill_count <= 0:
        return None
    if twap_minutes > 0 and entry_fill_count < required_fills and exit_reason != "stop_loss":
        return None

    gross_return = _short_return(entry_price, exit_price)
    cost_return = round_trip_cost_bps / 10_000.0
    net_return = gross_return - cost_return
    selected_count = max(int(row.get("selected_count") or config.top_n), 1)
    weight_info = _close_fade_position_weight(row, config=config, selected_count=selected_count)
    fill_fraction = min(1.0, entry_fill_count / required_fills)
    weight_info = {
        **weight_info,
        "weight": weight_info["weight"] * fill_fraction,
        "actual_notional": weight_info["actual_notional"] * fill_fraction,
    }
    weight = weight_info["weight"]
    if weight <= EPSILON:
        return None
    entry_dt = _dt_from_ms(int(entry_ts_ms))
    entry_complete_dt = _dt_from_ms(entry_complete_ts_ms)
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
        "entry_ts_ms": int(entry_ts_ms),
        "entry_time": entry_dt.isoformat(),
        "entry_start_ts_ms": entry_start_ts_ms,
        "entry_start_time": _dt_from_ms(entry_start_ts_ms).isoformat(),
        "entry_complete_ts_ms": entry_complete_ts_ms,
        "entry_complete_time": entry_complete_dt.isoformat(),
        "entry_twap_minutes": config.entry_twap_minutes,
        "entry_fill_count": entry_fill_count,
        "entry_fill_fraction": fill_fraction,
        "profit_protection_active_ts_ms": profit_active_ts_ms,
        "profit_protection_active_time": _dt_from_ms(profit_active_ts_ms).isoformat(),
        "exit_ts_ms": exit_ts_ms,
        "exit_time": exit_dt.isoformat(),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "hold_minutes": (exit_ts_ms - entry_ts_ms) / MS_PER_MINUTE,
        "post_twap_hold_minutes": (exit_ts_ms - entry_complete_ts_ms) / MS_PER_MINUTE,
        "entry_rank": int(row.get("entry_rank") or 0),
        "score": float(row.get(config.score) or 0.0),
        "score_name": config.score,
        "day_return": float(row.get("day_return") or 0.0),
        "vol_adjusted_day_return": float(row.get("vol_adjusted_day_return") or 0.0),
        "pump_score": int(row.get("pump_score") or 0),
        "pump_like": bool(row.get("pump_like")),
        "late_volume_ratio": float(row.get("late_volume_ratio") or 0.0),
        "vwap_extension": float(row.get("vwap_extension") or 0.0),
        "market_median_day_return": float(row.get("market_median_day_return") or 0.0),
        "coin_excess_vs_market": float(row.get("coin_excess_vs_market") or 0.0),
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
        "position_sizing": config.position_sizing,
        "score_weight_power": config.score_weight_power,
        "coin_excess_vs_market_min": config.coin_excess_vs_market_min,
        "coin_vwap_extension_min": config.coin_vwap_extension_min,
        "coin_late_volume_ratio_min": config.coin_late_volume_ratio_min,
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
    target_weight = float(row.get("position_target_weight") or (config.gross_exposure / max(selected_count, 1)))
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


def _grid_backend(workers: int) -> str:
    if workers <= 1:
        return "serial"
    if sys.platform.startswith("win"):
        return "thread"
    return "process"


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


def _num(value: Any, digits: int = 2) -> str:
    number = float(value) if value is not None else 0.0
    if not math.isfinite(number):
        return "n/a"
    return f"{number:.{digits}f}"


def _pct(value: Any) -> str:
    number = float(value) if value is not None else 0.0
    if not math.isfinite(number):
        return "n/a"
    return f"{number:.2%}"


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
