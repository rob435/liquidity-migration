from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import DEFAULT_MAJOR_SYMBOLS, DailyCloseFadeConfig, load_config
from aggression_carry.daily_close_fade import (
    MS_PER_MINUTE,
    backtest_daily_close_fade,
    build_daily_close_fade_features,
    summarize_close_fade_baskets,
)
from aggression_carry.downloaders import parse_date_ms
from aggression_carry.storage import dataset_path


EPSILON = 1e-12
PRE_SIGNAL_CONTRAST_COLUMNS = (
    "btc_day_return",
    "btc_last_60m_return",
    "btc_last_240m_return",
    "market_median_day_return",
    "market_positive_rate",
    "market_up_2_rate",
    "market_up_5_rate",
    "market_p90_day_return",
    "market_dispersion_day_return",
    "tradeable_median_day_return",
    "tradeable_positive_rate",
    "tradeable_pump_like_rate",
    "candidate_count",
    "candidate_avg_day_return",
    "candidate_max_day_return",
    "candidate_avg_late_volume_ratio",
    "selected_avg_day_return",
    "selected_max_day_return",
    "selected_avg_vol_adjusted_day_return",
    "selected_avg_late_volume_ratio",
    "selected_avg_vwap_extension",
    "selected_avg_pump_score",
    "selected_avg_baseline_liquidity_rank",
    "selected_excess_vs_market",
    "selected_excess_vs_btc",
    "selected_excess_vs_tradeable",
)
BUCKET_COLUMNS = (
    "btc_day_return",
    "btc_last_240m_return",
    "market_positive_rate",
    "market_up_5_rate",
    "market_dispersion_day_return",
    "selected_excess_vs_market",
    "selected_excess_vs_btc",
    "selected_avg_late_volume_ratio",
    "selected_avg_vwap_extension",
    "candidate_count",
)


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = _base_config(config.daily_close_fade, args)
    start_ms = parse_date_ms(args.start) if args.start else 0
    end_ms = parse_date_ms(args.end) if args.end else 0
    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "daily_close_fade_day_audit"
    output_dir.mkdir(parents=True, exist_ok=True)

    features = build_daily_close_fade_features(data_root, config=base, signal_minutes=(base.signal_minute,))
    features = _filter_signal_window(features, start_ms, end_ms)
    trades = backtest_daily_close_fade(
        data_root,
        features,
        config=base,
        round_trip_cost_bps=config.costs.base_entry_exit_cost_bps * base.cost_multiplier,
    )
    baskets = summarize_close_fade_baskets(trades)
    btc_context = build_btc_signal_context(
        data_root,
        signal_minute=base.signal_minute,
        start_ms=start_ms or int(features["signal_ts_ms"].min()),
        end_ms=end_ms or int(features["signal_ts_ms"].max()) + MS_PER_MINUTE,
    )
    day_rows = build_day_audit_rows(features, trades, baskets, btc_context, config=base)
    monthly = build_monthly_summary(day_rows)
    exit_summary = build_exit_summary(trades)
    contrast = build_win_loss_contrast(day_rows)
    buckets = build_context_bucket_summary(day_rows)
    payload = {
        "config": asdict(base),
        "rows": {
            "features": features.height,
            "trades": trades.height,
            "baskets": baskets.height,
            "day_rows": day_rows.height,
            "monthly": monthly.height,
            "exit_summary": exit_summary.height,
            "contrast": contrast.height,
            "buckets": buckets.height,
        },
        "summary": summarize_day_rows(day_rows),
        "date_range": _date_range(features, "signal_ts_ms"),
    }

    (output_dir / "daily_close_fade_day_audit.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_day_audit.md").write_text(
        format_day_audit_report(payload, day_rows, monthly, exit_summary, contrast, buckets),
        encoding="utf-8",
    )
    if not day_rows.is_empty():
        day_rows.write_csv(output_dir / "daily_close_fade_day_audit.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "daily_close_fade_day_audit_monthly.csv")
    if not exit_summary.is_empty():
        exit_summary.write_csv(output_dir / "daily_close_fade_day_audit_exit_reasons.csv")
    if not contrast.is_empty():
        contrast.write_csv(output_dir / "daily_close_fade_day_audit_win_loss_contrast.csv")
    if not buckets.is_empty():
        buckets.write_csv(output_dir / "daily_close_fade_day_audit_context_buckets.csv")
    print(f"day_audit={output_dir / 'daily_close_fade_day_audit.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit daily-close-fade day-by-day context and PnL patterns.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--signal-time", default="22:15")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--hold-minutes", type=int, default=180)
    parser.add_argument("--entry-delay-minutes", type=int, default=1)
    parser.add_argument("--score", default="vol_adjusted_day_return")
    parser.add_argument("--pump-filter", default="pump")
    parser.add_argument("--gross-exposure", type=float, default=1.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.20)
    parser.add_argument("--take-profit-pct", type=float, default=0.0)
    parser.add_argument("--basket-stop-loss-pct", type=float, default=0.0)
    parser.add_argument("--trailing-stop-pct", type=float, default=0.0)
    parser.add_argument("--trailing-activation-pct", type=float, default=0.0)
    parser.add_argument("--vol-trailing-stop-mult", type=float, default=0.25)
    parser.add_argument("--vol-trailing-activation-mult", type=float, default=0.0)
    parser.add_argument("--mfe-giveback-activation-pct", type=float, default=0.01)
    parser.add_argument("--mfe-giveback-pct", type=float, default=0.20)
    parser.add_argument("--vwap-reversion-pct", type=float, default=0.0)
    parser.add_argument("--stop-delay-minutes", type=int, default=15)
    parser.add_argument("--cost-multiplier", type=float, default=1.0)
    parser.add_argument("--liquidity-lookback-days", type=int, default=7)
    parser.add_argument("--liquidity-rank-min", type=int, default=31)
    parser.add_argument("--liquidity-rank-max", type=int, default=150)
    parser.add_argument("--min-baseline-turnover", type=float, default=0.0)
    parser.add_argument("--account-equity", type=float, default=10_000.0)
    parser.add_argument("--max-position-weight", type=float, default=0.0)
    parser.add_argument("--max-trade-notional-pct-day-turnover", type=float, default=0.0)
    parser.add_argument("--max-trade-notional-pct-baseline-turnover", type=float, default=0.0)
    parser.add_argument("--min-age-days", type=int, default=10)
    parser.add_argument("--min-day-turnover", type=float, default=None)
    parser.add_argument("--min-last-60m-turnover", type=float, default=None)
    parser.add_argument("--exclude-symbols", default=None)
    parser.add_argument("--include-majors", action="store_true")
    parser.add_argument("--require-archive-membership", action="store_true")
    return parser.parse_args()


def build_btc_signal_context(
    data_root: str | Path,
    *,
    signal_minute: int,
    start_ms: int,
    end_ms: int,
    symbol: str = "BTCUSDT",
) -> pl.DataFrame:
    files = sorted(dataset_path(data_root, "klines_1m").glob(f"**/symbol={symbol}/*.parquet"))
    if not files:
        return pl.DataFrame()
    warmup_ms = 24 * 60 * MS_PER_MINUTE
    frame = (
        pl.scan_parquet([str(file) for file in files])
        .filter(
            (pl.col("symbol") == symbol)
            & (pl.col("ts_ms") >= max(0, start_ms - warmup_ms))
            & (pl.col("ts_ms") < end_ms)
        )
        .with_columns(
            [
                pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"),
                (
                    pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.hour().cast(pl.Int16) * 60
                    + pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.minute().cast(pl.Int16)
                ).alias("minute_of_day"),
            ]
        )
        .filter(pl.col("minute_of_day") <= signal_minute)
        .sort(["date", "ts_ms"])
        .group_by("date", maintain_order=True)
        .agg(
            [
                pl.col("ts_ms").last().alias("btc_signal_ts_ms"),
                pl.col("open").first().alias("btc_day_open"),
                pl.col("close").last().alias("btc_signal_close"),
                pl.col("close").filter(pl.col("minute_of_day") <= signal_minute - 60).last().alias("btc_close_60m_ago"),
                pl.col("close").filter(pl.col("minute_of_day") <= signal_minute - 240).last().alias("btc_close_240m_ago"),
            ]
        )
        .with_columns(
            [
                (pl.col("btc_signal_close") / pl.col("btc_day_open") - 1.0).alias("btc_day_return"),
                (pl.col("btc_signal_close") / pl.col("btc_close_60m_ago") - 1.0).alias("btc_last_60m_return"),
                (pl.col("btc_signal_close") / pl.col("btc_close_240m_ago") - 1.0).alias("btc_last_240m_return"),
            ]
        )
        .select(["date", "btc_day_return", "btc_last_60m_return", "btc_last_240m_return"])
        .collect()
    )
    return frame


def build_day_audit_rows(
    features: pl.DataFrame,
    trades: pl.DataFrame,
    baskets: pl.DataFrame,
    btc_context: pl.DataFrame,
    *,
    config: DailyCloseFadeConfig,
) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame()
    market = _market_context(features, config=config)
    tradeable = _tradeable_context(features, config=config)
    candidates = _candidate_context(features, config=config)
    selected = _selected_context(trades)
    exits = _exit_reason_context(trades)
    output = (
        baskets.join(market, on=["date", "signal_ts_ms"], how="left")
        .join(tradeable, on=["date", "signal_ts_ms"], how="left")
        .join(candidates, on=["date", "signal_ts_ms"], how="left")
        .join(selected, on=["basket_id", "date", "signal_ts_ms"], how="left")
        .join(exits, on=["basket_id"], how="left")
        .join(btc_context, on="date", how="left")
        .with_columns(
            [
                (pl.col("basket_return") > 0.0).alias("winning_day"),
                pl.col("date").str.slice(0, 7).alias("month"),
                (pl.col("selected_avg_day_return") - pl.col("market_median_day_return")).alias(
                    "selected_excess_vs_market"
                ),
                (pl.col("selected_avg_day_return") - pl.col("btc_day_return")).alias("selected_excess_vs_btc"),
                (pl.col("selected_avg_day_return") - pl.col("tradeable_median_day_return")).alias(
                    "selected_excess_vs_tradeable"
                ),
            ]
        )
        .sort("signal_ts_ms")
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )
    return output


def build_monthly_summary(day_rows: pl.DataFrame) -> pl.DataFrame:
    if day_rows.is_empty():
        return pl.DataFrame()
    return (
        day_rows.group_by("month", maintain_order=True)
        .agg(
            [
                pl.len().alias("trading_days"),
                (pl.col("basket_return") > 0.0).mean().alias("hit_rate"),
                ((1.0 + pl.col("basket_return")).product() - 1.0).alias("month_return"),
                pl.col("basket_return").mean().alias("avg_day_return"),
                pl.col("basket_return").min().alias("worst_day_return"),
                pl.col("basket_return").max().alias("best_day_return"),
                pl.col("trade_count").sum().alias("trades"),
                pl.col("btc_day_return").mean().alias("avg_btc_day_return"),
                pl.col("market_positive_rate").mean().alias("avg_market_positive_rate"),
            ]
        )
        .sort("month")
    )


def build_exit_summary(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    return (
        trades.group_by("exit_reason", maintain_order=True)
        .agg(
            [
                pl.len().alias("trades"),
                pl.col("weighted_net_return").sum().alias("weighted_return_sum"),
                pl.col("net_return").mean().alias("avg_trade_return"),
                (pl.col("net_return") > 0.0).mean().alias("hit_rate"),
                pl.col("mae").mean().alias("avg_mae"),
                pl.col("mfe").mean().alias("avg_mfe"),
            ]
        )
        .sort("weighted_return_sum", descending=True)
    )


def build_win_loss_contrast(day_rows: pl.DataFrame) -> pl.DataFrame:
    if day_rows.is_empty():
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    for column in PRE_SIGNAL_CONTRAST_COLUMNS:
        if column not in day_rows.columns:
            continue
        values = day_rows.select([column, "winning_day"]).drop_nulls(column)
        wins = values.filter(pl.col("winning_day"))[column].to_list()
        losses = values.filter(~pl.col("winning_day"))[column].to_list()
        if not wins or not losses:
            continue
        all_values = values[column].to_list()
        std = statistics.stdev(all_values) if len(all_values) > 1 else 0.0
        win_mean = statistics.fmean(wins)
        loss_mean = statistics.fmean(losses)
        rows.append(
            {
                "metric": column,
                "win_mean": win_mean,
                "loss_mean": loss_mean,
                "loss_minus_win": loss_mean - win_mean,
                "standardized_diff": (loss_mean - win_mean) / std if std > EPSILON else 0.0,
                "win_obs": len(wins),
                "loss_obs": len(losses),
            }
        )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None).sort("standardized_diff", descending=False)


def build_context_bucket_summary(day_rows: pl.DataFrame, *, buckets: int = 5) -> pl.DataFrame:
    if day_rows.is_empty():
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    for column in BUCKET_COLUMNS:
        if column not in day_rows.columns:
            continue
        values = day_rows.select([column, "basket_return", "trade_count"]).drop_nulls(column)
        if values.height < buckets:
            continue
        ranked = (
            values.with_columns(
                [
                    pl.col(column).rank("ordinal").alias("_rank"),
                    pl.len().alias("_count"),
                ]
            )
            .with_columns((((pl.col("_rank") - 1) * buckets / pl.col("_count")).floor() + 1).alias("bucket"))
            .with_columns(pl.col("bucket").clip(lower_bound=1, upper_bound=buckets).cast(pl.Int64))
        )
        summary = ranked.group_by("bucket", maintain_order=True).agg(
            [
                pl.len().alias("days"),
                pl.col(column).min().alias("min_value"),
                pl.col(column).max().alias("max_value"),
                pl.col(column).mean().alias("avg_value"),
                pl.col("basket_return").mean().alias("avg_return"),
                ((1.0 + pl.col("basket_return")).product() - 1.0).alias("compounded_return"),
                (pl.col("basket_return") > 0.0).mean().alias("hit_rate"),
                pl.col("trade_count").sum().alias("trades"),
            ]
        )
        for row in summary.sort("bucket").to_dicts():
            rows.append({"metric": column, **row})
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None).sort(["metric", "bucket"])


def summarize_day_rows(day_rows: pl.DataFrame) -> dict[str, Any]:
    if day_rows.is_empty():
        return {
            "trading_days": 0,
            "total_return": 0.0,
            "hit_rate": 0.0,
            "avg_day_return": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
            "worst_day_return": 0.0,
            "best_day_return": 0.0,
        }
    returns = day_rows["basket_return"].to_list()
    mean_return = statistics.fmean(returns)
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    return {
        "trading_days": day_rows.height,
        "total_return": float(day_rows["equity"].tail(1).item() - 1.0),
        "hit_rate": float(sum(1 for item in returns if item > 0.0) / len(returns)),
        "avg_day_return": float(mean_return),
        "sharpe_like": float((mean_return / stdev) * math.sqrt(365.0)) if stdev > EPSILON else 0.0,
        "max_drawdown": float(day_rows["drawdown"].min()),
        "worst_day_return": float(min(returns)),
        "best_day_return": float(max(returns)),
    }


def format_day_audit_report(
    payload: dict[str, Any],
    day_rows: pl.DataFrame,
    monthly: pl.DataFrame,
    exit_summary: pl.DataFrame,
    contrast: pl.DataFrame,
    buckets: pl.DataFrame,
) -> str:
    summary = payload["summary"]
    lines = [
        "# Daily Close Fade Day Audit",
        "",
        "This report looks for day-level patterns in the current daily-close-fade setup.",
        "Pre-signal context uses only data available at the signal minute. MAE/MFE and",
        "exit reasons are post-trade diagnostics, not valid entry filters by themselves.",
        "",
        f"Date range: {payload.get('date_range', {}).get('start')} to {payload.get('date_range', {}).get('end')}",
        f"Rows: features={payload['rows']['features']} trades={payload['rows']['trades']} days={payload['rows']['day_rows']}",
        "",
        "## Backtest Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Trading days | {summary.get('trading_days', 0)} |",
        f"| Total return | {_pct(summary.get('total_return'))} |",
        f"| Hit rate | {_pct(summary.get('hit_rate'))} |",
        f"| Avg day return | {_pct(summary.get('avg_day_return'))} |",
        f"| Sharpe-like | {_num(summary.get('sharpe_like'), 2)} |",
        f"| Max drawdown | {_pct(summary.get('max_drawdown'))} |",
        f"| Worst day | {_pct(summary.get('worst_day_return'))} |",
        f"| Best day | {_pct(summary.get('best_day_return'))} |",
        "",
        "## Winning Vs Losing Days: Pre-Signal Context",
        "",
        "Negative standardized differences mean the metric was lower on losing days.",
        "Positive differences mean it was higher on losing days.",
        "",
        "| Metric | Win Mean | Loss Mean | Loss-Win | Std Diff | Win Obs | Loss Obs |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in contrast.head(30).to_dicts() if not contrast.is_empty() else []:
        metric = str(row.get("metric", ""))
        lines.append(
            f"| {metric} | {_format_metric_value(metric, row.get('win_mean'))} | "
            f"{_format_metric_value(metric, row.get('loss_mean'))} | "
            f"{_format_metric_value(metric, row.get('loss_minus_win'))} | "
            f"{_num(row.get('standardized_diff'), 2)} | {row.get('win_obs', 0)} | {row.get('loss_obs', 0)} |"
        )

    lines.extend(
        [
            "",
            "## Context Buckets",
            "",
            "| Metric | Bucket | Min | Max | Avg | Avg Return | Comp Return | Hit Rate | Days | Trades |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in buckets.to_dicts() if not buckets.is_empty() else []:
        metric = str(row.get("metric", ""))
        lines.append(
            f"| {metric} | {row.get('bucket', 0)} | {_format_metric_value(metric, row.get('min_value'))} | "
            f"{_format_metric_value(metric, row.get('max_value'))} | "
            f"{_format_metric_value(metric, row.get('avg_value'))} | "
            f"{_pct(row.get('avg_return'))} | {_pct(row.get('compounded_return'))} | "
            f"{_pct(row.get('hit_rate'))} | {row.get('days', 0)} | {row.get('trades', 0)} |"
        )

    lines.extend(
        [
            "",
            "## Worst Trading Days",
            "",
            "| Date | Return | DD | BTC DTD | Market +% | Market Up 5% | Sel Excess Mkt | Symbols | Exit Mix |",
            "|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in day_rows.sort("basket_return").head(20).to_dicts() if not day_rows.is_empty() else []:
        lines.append(_format_day_row(row))

    lines.extend(
        [
            "",
            "## Best Trading Days",
            "",
            "| Date | Return | DD | BTC DTD | Market +% | Market Up 5% | Sel Excess Mkt | Symbols | Exit Mix |",
            "|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in day_rows.sort("basket_return", descending=True).head(20).to_dicts() if not day_rows.is_empty() else []:
        lines.append(_format_day_row(row))

    lines.extend(
        [
            "",
            "## Exit Reason Attribution",
            "",
            "| Exit Reason | Trades | Weighted Return Sum | Avg Trade | Hit Rate | Avg MAE | Avg MFE |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in exit_summary.to_dicts() if not exit_summary.is_empty() else []:
        lines.append(
            f"| {row.get('exit_reason', '')} | {row.get('trades', 0)} | "
            f"{_pct(row.get('weighted_return_sum'))} | {_pct(row.get('avg_trade_return'))} | "
            f"{_pct(row.get('hit_rate'))} | {_pct(row.get('avg_mae'))} | {_pct(row.get('avg_mfe'))} |"
        )

    lines.extend(
        [
            "",
            "## Monthly Summary",
            "",
            "| Month | Return | Hit Rate | Trading Days | Worst Day | Best Day | Avg BTC DTD | Avg Market +% | Trades |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in monthly.to_dicts() if not monthly.is_empty() else []:
        lines.append(
            f"| {row.get('month', '')} | {_pct(row.get('month_return'))} | {_pct(row.get('hit_rate'))} | "
            f"{row.get('trading_days', 0)} | {_pct(row.get('worst_day_return'))} | "
            f"{_pct(row.get('best_day_return'))} | {_pct(row.get('avg_btc_day_return'))} | "
            f"{_pct(row.get('avg_market_positive_rate'))} | {row.get('trades', 0)} |"
        )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_day_audit.csv",
            "daily_close_fade_day_audit_monthly.csv",
            "daily_close_fade_day_audit_exit_reasons.csv",
            "daily_close_fade_day_audit_win_loss_contrast.csv",
            "daily_close_fade_day_audit_context_buckets.csv",
            "daily_close_fade_day_audit.json",
            "daily_close_fade_day_audit.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _market_context(features: pl.DataFrame, *, config: DailyCloseFadeConfig) -> pl.DataFrame:
    df = features.filter((pl.col("signal_minute") == config.signal_minute) & (pl.col("bar_coverage") >= 0.95))
    return _context_aggs(df, prefix="market")


def _tradeable_context(features: pl.DataFrame, *, config: DailyCloseFadeConfig) -> pl.DataFrame:
    df = features.filter(
        (pl.col("signal_minute") == config.signal_minute)
        & (pl.col("eligible"))
        & _liquidity_expr(config)
    )
    return _context_aggs(df, prefix="tradeable")


def _candidate_context(features: pl.DataFrame, *, config: DailyCloseFadeConfig) -> pl.DataFrame:
    df = features.filter(
        (pl.col("signal_minute") == config.signal_minute)
        & (pl.col("eligible"))
        & _liquidity_expr(config)
    )
    if config.pump_filter == "pump":
        df = df.filter(pl.col("pump_like"))
    elif config.pump_filter == "non_pump":
        df = df.filter(~pl.col("pump_like"))
    elif config.pump_filter != "all":
        raise ValueError(f"Unknown pump_filter: {config.pump_filter}")
    return _context_aggs(df, prefix="candidate")


def _context_aggs(df: pl.DataFrame, *, prefix: str) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()
    return (
        df.group_by(["date", "signal_ts_ms"], maintain_order=True)
        .agg(
            [
                pl.len().alias(f"{prefix}_count"),
                pl.col("day_return").mean().alias(f"{prefix}_avg_day_return"),
                pl.col("day_return").median().alias(f"{prefix}_median_day_return"),
                pl.col("day_return").quantile(0.1).alias(f"{prefix}_p10_day_return"),
                pl.col("day_return").quantile(0.9).alias(f"{prefix}_p90_day_return"),
                pl.col("day_return").std(ddof=0).fill_null(0.0).alias(f"{prefix}_std_day_return"),
                pl.col("day_return").max().alias(f"{prefix}_max_day_return"),
                (pl.col("day_return") > 0.0).mean().alias(f"{prefix}_positive_rate"),
                (pl.col("day_return") >= 0.02).mean().alias(f"{prefix}_up_2_rate"),
                (pl.col("day_return") >= 0.05).mean().alias(f"{prefix}_up_5_rate"),
                (pl.col("day_return") >= 0.10).mean().alias(f"{prefix}_up_10_rate"),
                pl.col("vol_adjusted_day_return").median().alias(f"{prefix}_median_vol_adjusted_day_return"),
                pl.col("late_volume_ratio").mean().alias(f"{prefix}_avg_late_volume_ratio"),
                pl.col("vwap_extension").mean().alias(f"{prefix}_avg_vwap_extension"),
                pl.col("pump_like").mean().alias(f"{prefix}_pump_like_rate"),
            ]
        )
        .with_columns(
            (pl.col(f"{prefix}_p90_day_return") - pl.col(f"{prefix}_p10_day_return")).alias(
                f"{prefix}_dispersion_day_return"
            )
        )
    )


def _selected_context(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    return (
        trades.sort(["basket_id", "entry_rank"])
        .group_by(["basket_id", "date", "signal_ts_ms"], maintain_order=True)
        .agg(
            [
                pl.col("symbol").str.join(",").alias("symbols"),
                pl.col("day_return").mean().alias("selected_avg_day_return"),
                pl.col("day_return").max().alias("selected_max_day_return"),
                pl.col("vol_adjusted_day_return").mean().alias("selected_avg_vol_adjusted_day_return"),
                pl.col("late_volume_ratio").mean().alias("selected_avg_late_volume_ratio"),
                pl.col("vwap_extension").mean().alias("selected_avg_vwap_extension"),
                pl.col("pump_score").mean().alias("selected_avg_pump_score"),
                pl.col("baseline_liquidity_rank").mean().alias("selected_avg_baseline_liquidity_rank"),
                pl.col("mae").mean().alias("selected_avg_mae"),
                pl.col("mae").min().alias("selected_worst_mae"),
                pl.col("mfe").mean().alias("selected_avg_mfe"),
                pl.col("mfe").max().alias("selected_best_mfe"),
            ]
        )
    )


def _exit_reason_context(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    counts = (
        trades.group_by(["basket_id", "exit_reason"], maintain_order=True)
        .agg(pl.len().alias("_exit_count"))
        .sort(["basket_id", "_exit_count", "exit_reason"], descending=[False, True, False])
    )
    return counts.group_by("basket_id", maintain_order=True).agg(
        [
            (pl.col("exit_reason") + ":" + pl.col("_exit_count").cast(pl.String)).str.join(",").alias("exit_mix"),
            pl.col("exit_reason").first().alias("dominant_exit_reason"),
        ]
    )


def _liquidity_expr(config: DailyCloseFadeConfig) -> pl.Expr:
    expr = (pl.col("day_turnover") >= config.min_day_turnover) & (
        pl.col("last_60m_turnover") >= config.min_last_60m_turnover
    )
    if config.liquidity_rank_min > 1 or config.liquidity_rank_max > 0:
        expr = expr & pl.col("baseline_liquidity_rank").is_not_null()
        expr = expr & (pl.col("baseline_liquidity_rank") >= config.liquidity_rank_min)
        if config.liquidity_rank_max > 0:
            expr = expr & (pl.col("baseline_liquidity_rank") <= config.liquidity_rank_max)
    if config.min_baseline_turnover > 0.0:
        expr = expr & pl.col("baseline_liquidity_turnover").is_not_null()
        expr = expr & (pl.col("baseline_liquidity_turnover") >= config.min_baseline_turnover)
    return expr


def _base_config(base: DailyCloseFadeConfig, args: argparse.Namespace) -> DailyCloseFadeConfig:
    exclusions = set(base.exclude_symbols)
    if args.exclude_symbols:
        exclusions.update(item.upper() for item in _csv_str(args.exclude_symbols))
    if args.include_majors:
        exclusions.difference_update(DEFAULT_MAJOR_SYMBOLS)
    return replace(
        base,
        signal_minute=_signal_minute(args.signal_time),
        top_n=args.top_n,
        hold_minutes=args.hold_minutes,
        entry_delay_minutes=args.entry_delay_minutes,
        score=args.score,
        pump_filter=args.pump_filter,
        gross_exposure=args.gross_exposure,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        basket_stop_loss_pct=args.basket_stop_loss_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        trailing_activation_pct=args.trailing_activation_pct,
        vol_trailing_stop_mult=args.vol_trailing_stop_mult,
        vol_trailing_activation_mult=args.vol_trailing_activation_mult,
        mfe_giveback_activation_pct=args.mfe_giveback_activation_pct,
        mfe_giveback_pct=args.mfe_giveback_pct,
        vwap_reversion_pct=args.vwap_reversion_pct,
        stop_delay_minutes=args.stop_delay_minutes,
        cost_multiplier=args.cost_multiplier,
        liquidity_lookback_days=args.liquidity_lookback_days,
        liquidity_rank_min=args.liquidity_rank_min,
        liquidity_rank_max=args.liquidity_rank_max,
        min_baseline_turnover=args.min_baseline_turnover,
        account_equity=args.account_equity,
        max_position_weight=args.max_position_weight,
        max_trade_notional_pct_of_day_turnover=args.max_trade_notional_pct_day_turnover,
        max_trade_notional_pct_of_baseline_turnover=args.max_trade_notional_pct_baseline_turnover,
        min_age_days=args.min_age_days,
        min_day_turnover=args.min_day_turnover if args.min_day_turnover is not None else base.min_day_turnover,
        min_last_60m_turnover=(
            args.min_last_60m_turnover
            if args.min_last_60m_turnover is not None
            else base.min_last_60m_turnover
        ),
        exclude_symbols=tuple(sorted(exclusions)),
        require_archive_membership=args.require_archive_membership or base.require_archive_membership,
    )


def _filter_signal_window(df: pl.DataFrame, start_ms: int, end_ms: int) -> pl.DataFrame:
    if df.is_empty():
        return df
    output = df
    if start_ms:
        output = output.filter(pl.col("signal_ts_ms") >= start_ms)
    if end_ms:
        output = output.filter(pl.col("signal_ts_ms") < end_ms)
    return output


def _date_range(df: pl.DataFrame, ts_col: str) -> dict[str, str | None]:
    if df.is_empty() or ts_col not in df.columns:
        return {"start": None, "end": None}
    return {"start": _ts_iso(int(df[ts_col].min())), "end": _ts_iso(int(df[ts_col].max()))}


def _ts_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_day_row(row: dict[str, Any]) -> str:
    return (
        f"| {row.get('date', '')} | {_pct(row.get('basket_return'))} | {_pct(row.get('drawdown'))} | "
        f"{_pct(row.get('btc_day_return'))} | {_pct(row.get('market_positive_rate'))} | "
        f"{_pct(row.get('market_up_5_rate'))} | {_pct(row.get('selected_excess_vs_market'))} | "
        f"{row.get('symbols', '')} | {row.get('exit_mix', '')} |"
    )


def _signal_minute(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _csv_str(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any, digits: int) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _format_metric_value(metric: str, value: Any) -> str:
    if "late_volume_ratio" in metric:
        return f"{float(value):.2f}x" if value is not None else "n/a"
    if (
        "count" in metric
        or "rank" in metric
        or "pump_score" in metric
        or "vol_adjusted" in metric
    ):
        return _num(value, 2)
    return _num_or_pct(value)


def _num_or_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    if abs(number) <= 2.0:
        return _pct(number)
    return _num(number, 2)


if __name__ == "__main__":
    raise SystemExit(main())
