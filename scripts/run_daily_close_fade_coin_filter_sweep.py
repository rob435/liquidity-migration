from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import DailyCloseFadeConfig, load_config
from aggression_carry.daily_close_fade import (
    backtest_daily_close_fade,
    build_daily_close_fade_features,
    summarize_close_fade_baskets,
)
from aggression_carry.downloaders import parse_date_ms


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)
EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class CoinFilterSpec:
    coin_excess_vs_market_min: float = 0.0
    coin_vwap_extension_min: float = 0.0
    coin_late_volume_ratio_min: float = 0.0
    min_symbols: int = 1

    @property
    def label(self) -> str:
        return (
            f"coin_excess>={self.coin_excess_vs_market_min:.1%} "
            f"coin_vwap>={self.coin_vwap_extension_min:.1%} "
            f"coin_latevol>={self.coin_late_volume_ratio_min:.2f}x "
            f"n>={self.min_symbols}"
        )


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = _base_config(config.daily_close_fade, args)
    start_ms = parse_date_ms(args.start) if args.start else 0
    end_ms = parse_date_ms(args.end) if args.end else 0
    split_specs = _parse_splits(args.splits)
    specs = build_coin_filter_specs(
        excess_thresholds=_csv_float(args.coin_excess_thresholds),
        vwap_thresholds=_csv_float(args.coin_vwap_extension_thresholds),
        late_volume_thresholds=_csv_float(args.coin_late_volume_thresholds),
        min_symbols=_csv_int(args.min_symbols),
    )

    features = build_daily_close_fade_features(data_root, config=base, signal_minutes=(base.signal_minute,))
    features = _filter_signal_window(features, start_ms, end_ms)
    features = attach_coin_market_context(features, base)
    results = evaluate_coin_filter_sweep(
        data_root,
        features,
        base_config=base,
        specs=specs,
        split_specs=split_specs,
        round_trip_cost_bps=config.costs.base_entry_exit_cost_bps * base.cost_multiplier,
        allocation_modes=_csv_str(args.allocation_modes),
    )
    summary = summarize_coin_filter_sweep(results, expected_splits=len(split_specs))

    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "daily_close_fade_coin_filter_sweep"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": asdict(base),
        "splits": [{"name": name, "start": start, "end": end} for name, start, end in split_specs],
        "thresholds": {
            "coin_excess_vs_market": _csv_float(args.coin_excess_thresholds),
            "coin_vwap_extension": _csv_float(args.coin_vwap_extension_thresholds),
            "coin_late_volume_ratio": _csv_float(args.coin_late_volume_thresholds),
            "min_symbols": _csv_int(args.min_symbols),
            "allocation_modes": _csv_str(args.allocation_modes),
        },
        "rows": {
            "features": features.height,
            "specs": len(specs),
            "results": results.height,
            "summary": summary.height,
        },
        "top_summary": summary.head(25).to_dicts() if not summary.is_empty() else [],
    }
    (output_dir / "daily_close_fade_coin_filter_sweep.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_coin_filter_sweep.md").write_text(
        format_coin_filter_sweep_report(payload, results, summary),
        encoding="utf-8",
    )
    if not results.is_empty():
        results.write_csv(output_dir / "daily_close_fade_coin_filter_sweep_results.csv")
    if not summary.is_empty():
        summary.write_csv(output_dir / "daily_close_fade_coin_filter_sweep_summary.csv")
    print(f"coin_filter_sweep={output_dir / 'daily_close_fade_coin_filter_sweep.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test individual-coin pump-quality gates for daily-close fade.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--splits", default=DEFAULT_SPLITS)
    parser.add_argument("--coin-excess-thresholds", default="0.03,0.05,0.08")
    parser.add_argument("--coin-vwap-extension-thresholds", default="0.015,0.025,0.035")
    parser.add_argument("--coin-late-volume-thresholds", default="0.5,0.75,1.0")
    parser.add_argument("--min-symbols", default="1,2")
    parser.add_argument("--allocation-modes", default="reallocate,fixed_slot")
    return parser.parse_args()


def build_coin_filter_specs(
    *,
    excess_thresholds: tuple[float, ...],
    vwap_thresholds: tuple[float, ...],
    late_volume_thresholds: tuple[float, ...],
    min_symbols: tuple[int, ...],
) -> list[CoinFilterSpec]:
    specs = []
    for values in product(excess_thresholds, vwap_thresholds, late_volume_thresholds, min_symbols):
        specs.append(CoinFilterSpec(*values))
    return specs


def attach_coin_market_context(features: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    if features.is_empty():
        return features
    market = (
        features.filter((pl.col("signal_minute") == config.signal_minute) & (pl.col("bar_coverage") >= 0.95))
        .group_by(["date", "signal_ts_ms"], maintain_order=True)
        .agg(pl.col("day_return").median().alias("market_median_day_return"))
    )
    return features.join(market, on=["date", "signal_ts_ms"], how="left").with_columns(
        (pl.col("day_return") - pl.col("market_median_day_return")).alias("coin_excess_vs_market")
    )


def apply_coin_filter(features: pl.DataFrame, spec: CoinFilterSpec) -> pl.DataFrame:
    if features.is_empty():
        return features
    gate = (
        (pl.col("coin_excess_vs_market").fill_null(-999.0) >= spec.coin_excess_vs_market_min)
        & (pl.col("vwap_extension").fill_null(-999.0) >= spec.coin_vwap_extension_min)
        & (pl.col("late_volume_ratio").fill_null(-999.0) >= spec.coin_late_volume_ratio_min)
    )
    return features.with_columns((pl.col("eligible") & gate).alias("eligible"))


def evaluate_coin_filter_sweep(
    data_root: str | Path,
    features: pl.DataFrame,
    *,
    base_config: DailyCloseFadeConfig,
    specs: list[CoinFilterSpec],
    split_specs: list[tuple[str, str, str]],
    round_trip_cost_bps: float,
    allocation_modes: tuple[str, ...],
) -> pl.DataFrame:
    baseline_trades = backtest_daily_close_fade(
        data_root,
        features,
        config=base_config,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    baseline_baskets_by_mode = _basket_sets_by_allocation(baseline_trades, base_config, allocation_modes)
    rows: list[dict[str, Any]] = []
    baselines = {
        (split, mode): summarize_baskets_against_calendar(
            _filter_split(baskets, start, end),
            _filter_split(baskets, start, end),
            split=split,
            allocation_mode=mode,
            label="baseline",
            spec=None,
            baseline_total_return=0.0,
            baseline_max_drawdown=0.0,
        )
        for split, start, end in split_specs
        for mode, baskets in baseline_baskets_by_mode.items()
    }
    for split, start, end in split_specs:
        for mode in baseline_baskets_by_mode:
            baseline = baselines[(split, mode)]
            rows.append({**baseline, "return_delta_vs_baseline": 0.0, "drawdown_delta_vs_baseline": 0.0})

    for spec in specs:
        filtered_features = apply_coin_filter(features, spec)
        variant_config = replace(base_config, min_symbols=spec.min_symbols)
        trades = backtest_daily_close_fade(
            data_root,
            filtered_features,
            config=variant_config,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        baskets_by_mode = _basket_sets_by_allocation(trades, variant_config, allocation_modes)
        for split, start, end in split_specs:
            for mode, baskets in baskets_by_mode.items():
                baseline_baskets = baseline_baskets_by_mode[mode]
                base_calendar = _filter_split(baseline_baskets, start, end)
                baseline = baselines[(split, mode)]
                rows.append(
                    summarize_baskets_against_calendar(
                        base_calendar,
                        _filter_split(baskets, start, end),
                        split=split,
                        allocation_mode=mode,
                        label=spec.label,
                        spec=spec,
                        baseline_total_return=float(baseline["total_return"]),
                        baseline_max_drawdown=float(baseline["max_drawdown"]),
                    )
                )
    return pl.DataFrame(rows, infer_schema_length=None).sort(["split", "total_return"], descending=[False, True])


def _basket_sets_by_allocation(
    trades: pl.DataFrame,
    config: DailyCloseFadeConfig,
    allocation_modes: tuple[str, ...],
) -> dict[str, pl.DataFrame]:
    output: dict[str, pl.DataFrame] = {}
    for mode in allocation_modes:
        if mode == "reallocate":
            output[mode] = summarize_close_fade_baskets(trades)
        elif mode == "fixed_slot":
            output[mode] = summarize_fixed_slot_baskets(trades, config)
        else:
            raise ValueError(f"Unknown allocation mode: {mode}")
    return output


def summarize_fixed_slot_baskets(trades: pl.DataFrame, config: DailyCloseFadeConfig) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    slot_weight = config.gross_exposure / max(config.top_n, 1)
    return (
        trades.group_by(["basket_id", "signal_ts_ms", "date", "signal_minute"], maintain_order=True)
        .agg(
            [
                pl.len().alias("trade_count"),
                (pl.col("gross_return") * slot_weight).sum().alias("basket_gross_return"),
                (pl.col("cost_return") * slot_weight).sum().alias("basket_cost_return"),
                (pl.col("net_return") * slot_weight).sum().alias("basket_return"),
                pl.lit(config.gross_exposure).alias("target_gross_exposure"),
                (pl.len() * slot_weight).alias("basket_gross_exposure"),
                pl.col("net_return").mean().alias("avg_trade_return"),
                pl.col("mae").min().alias("worst_mae"),
                pl.col("mfe").max().alias("best_mfe"),
            ]
        )
        .sort("signal_ts_ms")
    )


def summarize_baskets_against_calendar(
    calendar: pl.DataFrame,
    selected: pl.DataFrame,
    *,
    split: str,
    allocation_mode: str,
    label: str,
    spec: CoinFilterSpec | None,
    baseline_total_return: float,
    baseline_max_drawdown: float,
) -> dict[str, Any]:
    selected_by_id = {
        str(row["basket_id"]): row
        for row in selected.select(["basket_id", "basket_return", "trade_count"]).to_dicts()
    } if not selected.is_empty() else {}
    returns = []
    trades = 0
    for row in calendar.select(["basket_id"]).to_dicts() if not calendar.is_empty() else []:
        selected_row = selected_by_id.get(str(row["basket_id"]))
        if selected_row is None:
            returns.append(0.0)
            continue
        returns.append(float(selected_row["basket_return"]))
        trades += int(selected_row.get("trade_count") or 0)
    equity = _equity_from_returns(returns)
    mean_return = statistics.fmean(returns) if returns else 0.0
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    total_return = float(equity[-1] - 1.0) if equity else 0.0
    max_drawdown = _max_drawdown(equity)
    selected_days = len(selected_by_id)
    base_days = calendar.height
    row = {
        "split": split,
        "allocation_mode": allocation_mode,
        "label": label,
        "base_days": base_days,
        "selected_days": selected_days,
        "skipped_days": max(base_days - selected_days, 0),
        "active_rate": float(selected_days / base_days) if base_days else 0.0,
        "total_return": total_return,
        "return_delta_vs_baseline": total_return - baseline_total_return,
        "max_drawdown": max_drawdown,
        "drawdown_delta_vs_baseline": max_drawdown - baseline_max_drawdown,
        "calendar_mean_return": float(mean_return),
        "calendar_sharpe_like": float((mean_return / stdev) * math.sqrt(365.0)) if stdev > EPSILON else 0.0,
        "trades": trades,
    }
    if spec is None:
        row.update(asdict(CoinFilterSpec()))
    else:
        row.update(asdict(spec))
    return row


def summarize_coin_filter_sweep(results: pl.DataFrame, *, expected_splits: int) -> pl.DataFrame:
    if results.is_empty():
        return pl.DataFrame()
    cols = [
        "allocation_mode",
        "label",
        "coin_excess_vs_market_min",
        "coin_vwap_extension_min",
        "coin_late_volume_ratio_min",
        "min_symbols",
    ]
    return (
        results.group_by(cols, maintain_order=True)
        .agg(
            [
                pl.col("split").n_unique().alias("splits_seen"),
                (pl.col("total_return") > 0.0).cast(pl.Int64).sum().alias("positive_return_splits"),
                (pl.col("return_delta_vs_baseline") > 0.0).cast(pl.Int64).sum().alias("beat_baseline_splits"),
                pl.col("total_return").mean().alias("avg_total_return"),
                pl.col("total_return").min().alias("min_total_return"),
                pl.col("total_return").std(ddof=0).fill_null(0.0).alias("total_return_std"),
                pl.col("return_delta_vs_baseline").mean().alias("avg_return_delta_vs_baseline"),
                pl.col("return_delta_vs_baseline").min().alias("min_return_delta_vs_baseline"),
                pl.col("calendar_sharpe_like").mean().alias("avg_calendar_sharpe_like"),
                pl.col("max_drawdown").min().alias("worst_max_drawdown"),
                pl.col("active_rate").mean().alias("avg_active_rate"),
                pl.col("selected_days").min().alias("min_selected_days"),
                pl.col("selected_days").sum().alias("selected_days"),
                pl.col("trades").sum().alias("trades"),
            ]
        )
        .with_columns(
            [
                (pl.col("splits_seen") == expected_splits).alias("complete_splits"),
                (pl.col("positive_return_splits") == expected_splits).alias("all_splits_positive"),
                (pl.col("beat_baseline_splits") == expected_splits).alias("beats_baseline_all_splits"),
                (
                    pl.col("min_total_return")
                    + pl.col("avg_total_return")
                    - pl.col("total_return_std")
                    + (pl.col("avg_calendar_sharpe_like") / 100.0)
                ).alias("stability_score"),
            ]
        )
        .sort(
            [
                "beats_baseline_all_splits",
                "all_splits_positive",
                "stability_score",
                "avg_calendar_sharpe_like",
            ],
            descending=[True, True, True, True],
        )
    )


def format_coin_filter_sweep_report(payload: dict[str, Any], results: pl.DataFrame, summary: pl.DataFrame) -> str:
    lines = [
        "# Daily Close Fade Individual-Coin Filter Sweep",
        "",
        "This report applies pump-quality gates to each candidate coin before top-N selection.",
        "It is stricter than the basket/day filter and is not a live/demo config change.",
        "",
        f"Specs tested: `{payload['rows']['specs']}`",
        f"Features: `{payload['rows']['features']}`",
        "",
        "## Most Stable Filters",
        "",
        "| Rank | Alloc | Beat All | Pos Splits | Min Ret | Avg Ret | Avg Delta | Worst DD | Avg Sharpe | Active | Min Days | Trades | Filter |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for index, row in enumerate(summary.head(50).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('allocation_mode', '')} | "
            f"{row.get('beats_baseline_all_splits', False)} | "
            f"{row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('avg_return_delta_vs_baseline'))} | {_pct(row.get('worst_max_drawdown'))} | "
            f"{_num(row.get('avg_calendar_sharpe_like'), 2)} | {_pct(row.get('avg_active_rate'))} | "
            f"{row.get('min_selected_days', 0)} | {row.get('trades', 0)} | {row.get('label', '')} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Baseline And Top Split Detail",
            "",
            "| Split | Alloc | Total Ret | Delta | DD | Sharpe | Active | Days | Trades | Filter |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    top_labels = {"baseline"}
    if not summary.is_empty():
        top_labels.update(summary.head(10)["label"].to_list())
    detail = results.filter(pl.col("label").is_in(top_labels)) if not results.is_empty() else pl.DataFrame()
    for row in detail.sort(["label", "allocation_mode", "split"]).to_dicts() if not detail.is_empty() else []:
        lines.append(
            f"| {row.get('split', '')} | {row.get('allocation_mode', '')} | {_pct(row.get('total_return'))} | "
            f"{_pct(row.get('return_delta_vs_baseline'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_num(row.get('calendar_sharpe_like'), 2)} | {_pct(row.get('active_rate'))} | "
            f"{row.get('selected_days', 0)} | {row.get('trades', 0)} | {row.get('label', '')} |"
        )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_coin_filter_sweep_results.csv",
            "daily_close_fade_coin_filter_sweep_summary.csv",
            "daily_close_fade_coin_filter_sweep.json",
            "daily_close_fade_coin_filter_sweep.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _base_config(base: DailyCloseFadeConfig, args: argparse.Namespace) -> DailyCloseFadeConfig:
    return replace(base)


def _filter_signal_window(df: pl.DataFrame, start_ms: int, end_ms: int) -> pl.DataFrame:
    if df.is_empty():
        return df
    output = df
    if start_ms:
        output = output.filter(pl.col("signal_ts_ms") >= start_ms)
    if end_ms:
        output = output.filter(pl.col("signal_ts_ms") < end_ms)
    return output


def _filter_split(baskets: pl.DataFrame, start: str, end: str) -> pl.DataFrame:
    if baskets.is_empty():
        return baskets
    return baskets.filter((pl.col("date") >= start) & (pl.col("date") < end)).sort("date")


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


def _parse_splits(value: str) -> list[tuple[str, str, str]]:
    splits = []
    for item in _csv_str(value):
        name, start, end = item.split(":", 2)
        if parse_date_ms(end) <= parse_date_ms(start):
            raise ValueError(f"Split end must be after start: {item!r}")
        splits.append((name.strip(), start.strip(), end.strip()))
    if not splits:
        raise ValueError("At least one split is required")
    return splits


def _csv_str(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _csv_float(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value))


def _csv_int(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value))


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
