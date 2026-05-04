from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import DEFAULT_MAJOR_SYMBOLS, DailyCloseFadeConfig, load_config
from aggression_carry.daily_close_fade import (
    MS_PER_DAY,
    backtest_daily_close_fade,
    build_daily_close_fade_features,
    summarize_close_fade_baskets,
)
from aggression_carry.downloaders import parse_date_ms
from aggression_carry.storage import dataset_path


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)
DEFAULT_EMA_PERIODS = (50, 100, 200)
DEFAULT_DISTANCE_THRESHOLDS = (-0.05, -0.02, 0.0, 0.02, 0.05)
EPSILON = 1e-12


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = _base_config(config.daily_close_fade, args)
    split_specs = _parse_splits(args.splits)
    ema_periods = _csv_int(args.ema_periods)
    thresholds = _csv_float(args.distance_thresholds)
    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "daily_close_fade_regime_sweep"
    output_dir.mkdir(parents=True, exist_ok=True)

    min_start_ms = min(parse_date_ms(start) for _, start, _ in split_specs)
    max_end_ms = max(parse_date_ms(end) for _, _, end in split_specs)
    features = build_daily_close_fade_features(data_root, config=base, signal_minutes=(base.signal_minute,))
    features = _filter_signal_window(features, min_start_ms, max_end_ms)
    trades = backtest_daily_close_fade(
        data_root,
        features,
        config=base,
        round_trip_cost_bps=config.costs.base_entry_exit_cost_bps * base.cost_multiplier,
    )
    baskets = summarize_close_fade_baskets(trades)
    regime = build_prior_daily_ema_regime(
        data_root,
        symbol=args.regime_symbol.upper(),
        ema_periods=ema_periods,
        start_ms=min_start_ms,
        end_ms=max_end_ms,
    )
    results = evaluate_regime_sweep(
        baskets,
        regime,
        split_specs=split_specs,
        ema_periods=ema_periods,
        thresholds=thresholds,
        baseline_rule=args.baseline_rule,
    )
    stability = summarize_regime_stability(results, expected_splits=len(split_specs))
    payload = {
        "config": asdict(base),
        "regime_symbol": args.regime_symbol.upper(),
        "ema_periods": ema_periods,
        "distance_thresholds": thresholds,
        "baseline_rule": args.baseline_rule,
        "rows": {
            "features": features.height,
            "trades": trades.height,
            "baskets": baskets.height,
            "regime": regime.height,
            "results": results.height,
            "stability": stability.height,
        },
        "top_stability": stability.head(25).to_dicts() if not stability.is_empty() else [],
    }

    (output_dir / "daily_close_fade_regime_sweep.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_regime_sweep.md").write_text(
        format_regime_sweep_report(payload, results, stability, split_specs),
        encoding="utf-8",
    )
    if not results.is_empty():
        results.write_csv(output_dir / "daily_close_fade_regime_sweep.csv")
    if not stability.is_empty():
        stability.write_csv(output_dir / "daily_close_fade_regime_stability.csv")
    print(f"regime_sweep={output_dir / 'daily_close_fade_regime_sweep.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test daily-close-fade BTC EMA regime overlays without changing runtime.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--splits", default=DEFAULT_SPLITS, help="Comma-separated name:start:end specs.")
    parser.add_argument("--regime-symbol", default="BTCUSDT")
    parser.add_argument("--ema-periods", default="50,100,200")
    parser.add_argument("--distance-thresholds", default="-0.05,-0.02,0,0.02,0.05")
    parser.add_argument(
        "--baseline-rule",
        default="all",
        choices=("all",),
        help="Baseline always trades every base basket. More rules should be researched explicitly.",
    )
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


def build_prior_daily_ema_regime(
    data_root: str | Path,
    *,
    symbol: str,
    ema_periods: tuple[int, ...],
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    files = sorted(dataset_path(data_root, "klines_1m").glob(f"**/symbol={symbol}/*.parquet"))
    if not files:
        raise RuntimeError(f"klines_1m has no {symbol} files; download that symbol before regime tests")
    # Pull enough pre-window history to warm up long EMAs, then only expose prior completed daily bars.
    warmup_ms = (max(ema_periods) + 5) * MS_PER_DAY
    lf = pl.scan_parquet([str(file) for file in files]).filter(
        (pl.col("symbol") == symbol)
        & (pl.col("ts_ms") >= max(0, start_ms - warmup_ms))
        & (pl.col("ts_ms") < end_ms)
    )
    return build_prior_daily_ema_regime_from_klines(lf.collect(), ema_periods=ema_periods)


def build_prior_daily_ema_regime_from_klines(klines: pl.DataFrame, *, ema_periods: tuple[int, ...]) -> pl.DataFrame:
    if klines.is_empty():
        return pl.DataFrame()
    daily = (
        klines.with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
        .sort("ts_ms")
        .group_by("date", maintain_order=True)
        .agg(
            [
                pl.col("symbol").last().alias("regime_symbol"),
                pl.col("close").cast(pl.Float64).last().alias("prior_close"),
                pl.col("ts_ms").last().alias("prior_close_ts_ms"),
            ]
        )
        .sort("date")
    )
    ema_exprs = [
        pl.col("prior_close").ewm_mean(span=period, adjust=False).alias(f"ema_{period}")
        for period in ema_periods
    ]
    daily = daily.with_columns(ema_exprs)
    distance_exprs = [
        (pl.col("prior_close") / pl.col(f"ema_{period}") - 1.0).alias(f"ema_distance_{period}")
        for period in ema_periods
    ]
    return (
        daily.with_columns(distance_exprs)
        .with_columns(
            (
                pl.col("date").str.strptime(pl.Date, "%Y-%m-%d") + pl.duration(days=1)
            ).dt.strftime("%Y-%m-%d").alias("signal_date")
        )
        .select(
            [
                "signal_date",
                "regime_symbol",
                "prior_close",
                "prior_close_ts_ms",
                *[f"ema_{period}" for period in ema_periods],
                *[f"ema_distance_{period}" for period in ema_periods],
            ]
        )
        .sort("signal_date")
    )


def evaluate_regime_sweep(
    baskets: pl.DataFrame,
    regime: pl.DataFrame,
    *,
    split_specs: list[tuple[str, str, str]],
    ema_periods: tuple[int, ...],
    thresholds: tuple[float, ...],
    baseline_rule: str,
) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame()
    joined = baskets.join(regime, left_on="date", right_on="signal_date", how="left")
    rows: list[dict[str, Any]] = []
    for split_name, start, end in split_specs:
        split_baskets = _filter_baskets_by_date(joined, start, end)
        rows.append(
            summarize_regime_selection(
                split_baskets,
                start=start,
                end=end,
                split=split_name,
                rule=baseline_rule,
                ema_period=0,
                threshold=0.0,
                selected_mask=None,
            )
        )
        for period in ema_periods:
            distance_col = f"ema_distance_{period}"
            if distance_col not in split_baskets.columns:
                continue
            for threshold in thresholds:
                rows.append(
                    summarize_regime_selection(
                        split_baskets,
                        start=start,
                        end=end,
                        split=split_name,
                        rule="btc_ema_distance_lte",
                        ema_period=period,
                        threshold=threshold,
                        selected_mask=pl.col(distance_col) <= threshold,
                    )
                )
                rows.append(
                    summarize_regime_selection(
                        split_baskets,
                        start=start,
                        end=end,
                        split=split_name,
                        rule="btc_ema_distance_gt",
                        ema_period=period,
                        threshold=threshold,
                        selected_mask=pl.col(distance_col) > threshold,
                    )
                )
    return pl.DataFrame(rows, infer_schema_length=None).sort(["split", "rule", "ema_period", "threshold"])


def summarize_regime_selection(
    baskets: pl.DataFrame,
    *,
    start: str,
    end: str,
    split: str,
    rule: str,
    ema_period: int,
    threshold: float,
    selected_mask: pl.Expr | None,
) -> dict[str, Any]:
    base = baskets.sort("date")
    if selected_mask is not None and not base.is_empty():
        selected = base.filter(selected_mask.fill_null(False))
    else:
        selected = base
    skipped = base.join(selected.select("basket_id"), on="basket_id", how="anti") if not base.is_empty() else base
    daily = _calendar_daily_returns(selected, start=start, end=end)
    daily_returns = daily["daily_return"].to_list() if not daily.is_empty() else []
    equity = _equity_from_returns(daily_returns)
    stdev = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    mean_return = statistics.fmean(daily_returns) if daily_returns else 0.0
    selected_returns = selected["basket_return"].to_list() if not selected.is_empty() else []
    base_returns = base["basket_return"].to_list() if not base.is_empty() else []
    skipped_returns = skipped["basket_return"].to_list() if not skipped.is_empty() else []
    return {
        "split": split,
        "start": start,
        "end": end,
        "rule": rule,
        "ema_period": ema_period,
        "threshold": threshold,
        "calendar_days": len(daily_returns),
        "base_baskets": base.height,
        "selected_baskets": selected.height,
        "skipped_baskets": skipped.height,
        "active_day_rate": float(selected.height / len(daily_returns)) if daily_returns else 0.0,
        "total_return": float(equity[-1] - 1.0) if equity else 0.0,
        "max_drawdown": _max_drawdown(equity),
        "calendar_mean_return": float(mean_return),
        "calendar_sharpe_like": float((mean_return / stdev) * math.sqrt(365.0)) if stdev > EPSILON else 0.0,
        "selected_avg_basket_return": float(statistics.fmean(selected_returns)) if selected_returns else 0.0,
        "base_avg_basket_return": float(statistics.fmean(base_returns)) if base_returns else 0.0,
        "skipped_avg_basket_return": float(statistics.fmean(skipped_returns)) if skipped_returns else 0.0,
        "selected_basket_hit_rate": _hit_rate(selected_returns),
        "base_basket_hit_rate": _hit_rate(base_returns),
        "skipped_basket_hit_rate": _hit_rate(skipped_returns),
        "trade_count": int(selected["trade_count"].sum())
        if "trade_count" in selected.columns and not selected.is_empty()
        else 0,
        "missing_regime_baskets": int(base.select(pl.col("prior_close").is_null().sum()).item())
        if "prior_close" in base.columns and not base.is_empty()
        else 0,
    }


def summarize_regime_stability(results: pl.DataFrame, *, expected_splits: int) -> pl.DataFrame:
    if results.is_empty():
        return pl.DataFrame()
    cols = ["rule", "ema_period", "threshold"]
    return (
        results.group_by(cols, maintain_order=True)
        .agg(
            [
                pl.col("split").n_unique().alias("splits_seen"),
                (pl.col("total_return") > 0.0).cast(pl.Int64).sum().alias("positive_return_splits"),
                pl.col("total_return").mean().alias("avg_total_return"),
                pl.col("total_return").min().alias("min_total_return"),
                pl.col("total_return").max().alias("max_total_return"),
                pl.col("total_return").std(ddof=0).fill_null(0.0).alias("total_return_std"),
                pl.col("calendar_sharpe_like").mean().alias("avg_calendar_sharpe_like"),
                pl.col("max_drawdown").min().alias("worst_max_drawdown"),
                pl.col("active_day_rate").mean().alias("avg_active_day_rate"),
                pl.col("selected_baskets").sum().alias("selected_baskets"),
                pl.col("trade_count").sum().alias("trade_count"),
                pl.col("missing_regime_baskets").sum().alias("missing_regime_baskets"),
            ]
        )
        .with_columns(
            [
                (pl.col("splits_seen") == expected_splits).alias("complete_splits"),
                (pl.col("positive_return_splits") == expected_splits).alias("all_splits_positive"),
                (
                    pl.col("min_total_return") + pl.col("avg_total_return") - pl.col("total_return_std").fill_null(0.0)
                ).alias("stability_score"),
            ]
        )
        .sort(
            [
                "all_splits_positive",
                "positive_return_splits",
                "stability_score",
                "min_total_return",
                "avg_calendar_sharpe_like",
            ],
            descending=[True, True, True, True, True],
        )
    )


def format_regime_sweep_report(
    payload: dict[str, Any],
    results: pl.DataFrame,
    stability: pl.DataFrame,
    split_specs: list[tuple[str, str, str]],
) -> str:
    lines = [
        "# Daily Close Fade Regime Sweep",
        "",
        "This is a research overlay only. It does not change paper/demo trading.",
        "The BTC EMA state is point-in-time: each signal date uses the previous completed UTC daily close.",
        "",
        "## Inputs",
        "",
        f"- Regime symbol: `{payload['regime_symbol']}`",
        f"- EMA periods: `{', '.join(str(item) for item in payload['ema_periods'])}`",
        f"- Distance thresholds: `{', '.join(_pct(item) for item in payload['distance_thresholds'])}`",
        f"- Base baskets: `{payload['rows']['baskets']}`",
        "",
        "## Splits",
        "",
        "| Split | Start | End |",
        "|---|---:|---:|",
    ]
    for name, start, end in split_specs:
        lines.append(f"| {name} | {start} | {end} |")

    lines.extend(
        [
            "",
            "## Most Stable Regime Rules",
            "",
            "| Rank | Rule | EMA | Threshold | All Positive | Pos Splits | Min Return | Avg Return | Worst DD | Avg Calendar Sharpe | Active Days | Trades | Missing Regime |",
            "|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(stability.head(40).to_dicts() if not stability.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('rule', '')} | {row.get('ema_period', 0)} | {_pct(row.get('threshold'))} | "
            f"{row.get('all_splits_positive', False)} | {row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('worst_max_drawdown'))} | {_num(row.get('avg_calendar_sharpe_like'), 2)} | "
            f"{_pct(row.get('avg_active_day_rate'))} | {row.get('trade_count', 0)} | "
            f"{row.get('missing_regime_baskets', 0)} |"
        )
    if stability.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Split Detail",
            "",
            "| Split | Rule | EMA | Threshold | Total Return | Max DD | Calendar Sharpe | Active Days | Selected Baskets | Skipped Avg |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in results.head(120).to_dicts() if not results.is_empty() else []:
        lines.append(
            f"| {row.get('split', '')} | {row.get('rule', '')} | {row.get('ema_period', 0)} | "
            f"{_pct(row.get('threshold'))} | {_pct(row.get('total_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_num(row.get('calendar_sharpe_like'), 2)} | "
            f"{_pct(row.get('active_day_rate'))} | {row.get('selected_baskets', 0)} | "
            f"{_pct(row.get('skipped_avg_basket_return'))} |"
        )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_regime_sweep.csv",
            "daily_close_fade_regime_stability.csv",
            "daily_close_fade_regime_sweep.json",
            "daily_close_fade_regime_sweep.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


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


def _filter_baskets_by_date(baskets: pl.DataFrame, start: str, end: str) -> pl.DataFrame:
    if baskets.is_empty():
        return baskets
    return baskets.filter((pl.col("date") >= start) & (pl.col("date") < end))


def _calendar_daily_returns(selected: pl.DataFrame, *, start: str, end: str) -> pl.DataFrame:
    dates = _date_range(start, end)
    calendar = pl.DataFrame({"date": dates}) if dates else pl.DataFrame({"date": pl.Series([], dtype=pl.String)})
    if selected.is_empty():
        return calendar.with_columns(pl.lit(0.0).alias("daily_return"))
    selected_daily = selected.group_by("date", maintain_order=True).agg(
        pl.col("basket_return").sum().alias("daily_return")
    )
    return calendar.join(selected_daily, on="date", how="left").with_columns(pl.col("daily_return").fill_null(0.0))


def _date_range(start: str, end: str) -> list[str]:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    output = []
    current = start_date
    while current < end_date:
        output.append(current.isoformat())
        current += timedelta(days=1)
    return output


def _equity_from_returns(returns: list[float]) -> list[float]:
    equity = []
    current = 1.0
    for value in returns:
        current *= 1.0 + float(value)
        equity.append(current)
    return equity


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > EPSILON:
            worst = min(worst, value / peak - 1.0)
    return float(worst)


def _hit_rate(values: list[float]) -> float:
    return float(sum(1 for value in values if value > 0.0) / len(values)) if values else 0.0


def _parse_splits(value: str) -> list[tuple[str, str, str]]:
    splits = []
    for item in _csv_str(value):
        name, start, end = item.split(":", 2)
        if not name or not start or not end:
            raise ValueError(f"Invalid split spec: {item!r}")
        if parse_date_ms(end) <= parse_date_ms(start):
            raise ValueError(f"Split end must be after start: {item!r}")
        splits.append((name.strip(), start.strip(), end.strip()))
    if not splits:
        raise ValueError("At least one split is required")
    return splits


def _signal_minute(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _csv_int(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value))


def _csv_float(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value))


def _csv_str(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2%}"


def _num(value: Any, digits: int) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


if __name__ == "__main__":
    raise SystemExit(main())
