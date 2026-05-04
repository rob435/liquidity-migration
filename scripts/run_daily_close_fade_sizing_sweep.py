from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_daily_close_fade_coin_filter_sweep as coin_sweep
from aggression_carry.config import DailyCloseFadeConfig, load_config
from aggression_carry.daily_close_fade import (
    backtest_daily_close_fade,
    build_daily_close_fade_features,
)
from aggression_carry.downloaders import parse_date_ms


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)
DEFAULT_FILTERS = "5:0.025:0.75:1,8:0.015:1.0:1,8:0.035:1.0:1"
EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class SizingSpec:
    mode: str
    max_weight: float = 0.0
    score_power: float = 0.0

    @property
    def label(self) -> str:
        if self.mode == "reallocate_equal":
            return "reallocate_equal"
        if self.mode == "fixed_slot":
            return "fixed_slot"
        if self.mode == "capped_equal":
            return f"capped_equal max={self.max_weight:.0%}"
        if self.mode == "capped_score":
            return f"capped_score max={self.max_weight:.0%} power={self.score_power:g}"
        return self.mode


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = replace(
        config.daily_close_fade,
        coin_excess_vs_market_min=0.0,
        coin_vwap_extension_min=0.0,
        coin_late_volume_ratio_min=0.0,
        position_sizing="equal",
        max_position_weight=0.0,
    )
    start_ms = parse_date_ms(args.start) if args.start else 0
    end_ms = parse_date_ms(args.end) if args.end else 0
    split_specs = _parse_splits(args.splits)
    filter_specs = _parse_filter_specs(args.filters)
    sizing_specs = build_sizing_specs(
        caps=_csv_float(args.max_weights),
        score_powers=_csv_float(args.score_powers),
        include_uncapped=args.include_uncapped,
    )

    features = build_daily_close_fade_features(data_root, config=base, signal_minutes=(base.signal_minute,))
    features = _filter_signal_window(features, start_ms, end_ms)
    features = coin_sweep.attach_coin_market_context(features, base)
    results = evaluate_sizing_sweep(
        data_root,
        features,
        base_config=base,
        filter_specs=filter_specs,
        sizing_specs=sizing_specs,
        split_specs=split_specs,
        round_trip_cost_bps=config.costs.base_entry_exit_cost_bps * base.cost_multiplier,
    )
    summary = summarize_sizing_sweep(results, expected_splits=len(split_specs))

    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "daily_close_fade_sizing_sweep"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": asdict(base),
        "filters": [spec.label for spec in filter_specs],
        "sizing_specs": [spec.label for spec in sizing_specs],
        "splits": [{"name": name, "start": start, "end": end} for name, start, end in split_specs],
        "rows": {
            "features": features.height,
            "filter_specs": len(filter_specs),
            "sizing_specs": len(sizing_specs),
            "results": results.height,
            "summary": summary.height,
        },
        "top_summary": summary.head(25).to_dicts() if not summary.is_empty() else [],
    }
    (output_dir / "daily_close_fade_sizing_sweep.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_sizing_sweep.md").write_text(
        format_sizing_sweep_report(payload, results, summary),
        encoding="utf-8",
    )
    if not results.is_empty():
        results.write_csv(output_dir / "daily_close_fade_sizing_sweep_results.csv")
    if not summary.is_empty():
        summary.write_csv(output_dir / "daily_close_fade_sizing_sweep_summary.csv")
    print(f"sizing_sweep={output_dir / 'daily_close_fade_sizing_sweep.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test concentration caps and score-tilted sizing for daily-close fade.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--splits", default=DEFAULT_SPLITS)
    parser.add_argument("--filters", default=DEFAULT_FILTERS)
    parser.add_argument("--max-weights", default="0.25,0.30,0.35,0.40,0.50")
    parser.add_argument("--score-powers", default="0.5,1.0")
    parser.add_argument("--include-uncapped", action="store_true")
    return parser.parse_args()


def build_sizing_specs(
    *,
    caps: tuple[float, ...],
    score_powers: tuple[float, ...],
    include_uncapped: bool,
) -> list[SizingSpec]:
    specs = [SizingSpec("fixed_slot")]
    if include_uncapped:
        specs.append(SizingSpec("reallocate_equal"))
    for cap in caps:
        specs.append(SizingSpec("capped_equal", max_weight=cap))
        for power in score_powers:
            specs.append(SizingSpec("capped_score", max_weight=cap, score_power=power))
    return specs


def evaluate_sizing_sweep(
    data_root: str | Path,
    features: pl.DataFrame,
    *,
    base_config: DailyCloseFadeConfig,
    filter_specs: list[coin_sweep.CoinFilterSpec],
    sizing_specs: list[SizingSpec],
    split_specs: list[tuple[str, str, str]],
    round_trip_cost_bps: float,
) -> pl.DataFrame:
    baseline_trades = backtest_daily_close_fade(
        data_root,
        features,
        config=base_config,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    baseline_baskets = build_weighted_baskets(
        baseline_trades,
        config=base_config,
        sizing=SizingSpec("reallocate_equal"),
    )
    baseline_by_split = {
        split: _summarize_basket_returns(
            _filter_split(baseline_baskets, start, end),
            _filter_split(baseline_baskets, start, end),
            split=split,
            filter_label="baseline",
            sizing_spec=SizingSpec("reallocate_equal"),
            baseline_total_return=0.0,
            baseline_max_drawdown=0.0,
        )
        for split, start, end in split_specs
    }
    rows: list[dict[str, Any]] = []
    for split, start, end in split_specs:
        rows.append({**baseline_by_split[split], "return_delta_vs_baseline": 0.0, "drawdown_delta_vs_baseline": 0.0})

    for filter_spec in filter_specs:
        filtered_features = coin_sweep.apply_coin_filter(features, filter_spec)
        variant_config = replace(base_config, min_symbols=filter_spec.min_symbols)
        trades = backtest_daily_close_fade(
            data_root,
            filtered_features,
            config=variant_config,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        for sizing_spec in sizing_specs:
            baskets = build_weighted_baskets(trades, config=base_config, sizing=sizing_spec)
            for split, start, end in split_specs:
                calendar = _filter_split(baseline_baskets, start, end)
                baseline = baseline_by_split[split]
                rows.append(
                    _summarize_basket_returns(
                        calendar,
                        _filter_split(baskets, start, end),
                        split=split,
                        filter_label=filter_spec.label,
                        sizing_spec=sizing_spec,
                        baseline_total_return=float(baseline["total_return"]),
                        baseline_max_drawdown=float(baseline["max_drawdown"]),
                    )
                )
    return pl.DataFrame(rows, infer_schema_length=None).sort(["split", "total_return"], descending=[False, True])


def build_weighted_baskets(trades: pl.DataFrame, *, config: DailyCloseFadeConfig, sizing: SizingSpec) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    rows = []
    for basket_key, basket_rows in trades.sort(["basket_id", "entry_rank"]).group_by(
        ["basket_id", "signal_ts_ms", "date", "signal_minute"], maintain_order=True
    ):
        basket_id, signal_ts_ms, date, signal_minute = basket_key
        trade_dicts = basket_rows.to_dicts()
        weights = _weights_for_basket(trade_dicts, config=config, sizing=sizing)
        basket_return = sum(float(row["net_return"]) * weight for row, weight in zip(trade_dicts, weights, strict=True))
        rows.append(
            {
                "basket_id": basket_id,
                "signal_ts_ms": int(signal_ts_ms),
                "date": str(date),
                "signal_minute": int(signal_minute),
                "trade_count": len(trade_dicts),
                "basket_return": basket_return,
                "basket_gross_return": sum(float(row["gross_return"]) * weight for row, weight in zip(trade_dicts, weights, strict=True)),
                "basket_cost_return": sum(float(row["cost_return"]) * weight for row, weight in zip(trade_dicts, weights, strict=True)),
                "basket_gross_exposure": sum(weights),
                "max_symbol_weight": max(weights) if weights else 0.0,
                "avg_symbol_weight": statistics.fmean(weights) if weights else 0.0,
                "worst_mae": min(float(row.get("mae") or 0.0) for row in trade_dicts),
                "best_mfe": max(float(row.get("mfe") or 0.0) for row in trade_dicts),
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None).sort("signal_ts_ms")


def _weights_for_basket(
    trade_rows: list[dict[str, Any]],
    *,
    config: DailyCloseFadeConfig,
    sizing: SizingSpec,
) -> list[float]:
    count = len(trade_rows)
    if count == 0:
        return []
    if sizing.mode == "fixed_slot":
        return [config.gross_exposure / max(config.top_n, 1)] * count
    if sizing.mode == "reallocate_equal":
        return [config.gross_exposure / count] * count
    if sizing.mode == "capped_equal":
        return [min(config.gross_exposure / count, sizing.max_weight)] * count
    if sizing.mode == "capped_score":
        scores = [max(float(row.get("score") or 0.0), EPSILON) ** sizing.score_power for row in trade_rows]
        return capped_proportional_weights(scores, gross_exposure=config.gross_exposure, max_weight=sizing.max_weight)
    raise ValueError(f"Unknown sizing mode: {sizing.mode}")


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
        if total_score <= EPSILON:
            share = remaining_exposure / len(remaining)
            proposed = {index: share for index in remaining}
        else:
            proposed = {
                index: remaining_exposure * max(scores[index], EPSILON) / total_score
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


def summarize_sizing_sweep(results: pl.DataFrame, *, expected_splits: int) -> pl.DataFrame:
    if results.is_empty():
        return pl.DataFrame()
    cols = ["filter_label", "sizing_label", "mode", "max_weight", "score_power"]
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
                pl.col("worst_day_return").min().alias("worst_day_return"),
                pl.col("avg_gross_exposure").mean().alias("avg_gross_exposure"),
                pl.col("avg_max_symbol_weight").mean().alias("avg_max_symbol_weight"),
                pl.col("active_rate").mean().alias("avg_active_rate"),
                pl.col("selected_days").min().alias("min_selected_days"),
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
                    - (pl.col("avg_max_symbol_weight") / 10.0)
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


def _summarize_basket_returns(
    calendar: pl.DataFrame,
    selected: pl.DataFrame,
    *,
    split: str,
    filter_label: str,
    sizing_spec: SizingSpec,
    baseline_total_return: float,
    baseline_max_drawdown: float,
) -> dict[str, Any]:
    selected_by_id = {
        str(row["basket_id"]): row
        for row in selected.to_dicts()
    } if not selected.is_empty() else {}
    returns = []
    gross_exposures = []
    max_weights = []
    trades = 0
    for row in calendar.select(["basket_id"]).to_dicts() if not calendar.is_empty() else []:
        selected_row = selected_by_id.get(str(row["basket_id"]))
        if selected_row is None:
            returns.append(0.0)
            gross_exposures.append(0.0)
            max_weights.append(0.0)
            continue
        returns.append(float(selected_row["basket_return"]))
        gross_exposures.append(float(selected_row.get("basket_gross_exposure") or 0.0))
        max_weights.append(float(selected_row.get("max_symbol_weight") or 0.0))
        trades += int(selected_row.get("trade_count") or 0)
    equity = _equity_from_returns(returns)
    mean_return = statistics.fmean(returns) if returns else 0.0
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    total_return = float(equity[-1] - 1.0) if equity else 0.0
    max_drawdown = _max_drawdown(equity)
    selected_days = len(selected_by_id)
    base_days = calendar.height
    return {
        "split": split,
        "filter_label": filter_label,
        "sizing_label": sizing_spec.label,
        "mode": sizing_spec.mode,
        "max_weight": sizing_spec.max_weight,
        "score_power": sizing_spec.score_power,
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
        "hit_rate": float(sum(1 for value in returns if value > 0.0) / len(returns)) if returns else 0.0,
        "worst_day_return": float(min(returns)) if returns else 0.0,
        "best_day_return": float(max(returns)) if returns else 0.0,
        "avg_gross_exposure": statistics.fmean(gross_exposures) if gross_exposures else 0.0,
        "avg_max_symbol_weight": statistics.fmean(max_weights) if max_weights else 0.0,
        "trades": trades,
    }


def format_sizing_sweep_report(payload: dict[str, Any], results: pl.DataFrame, summary: pl.DataFrame) -> str:
    lines = [
        "# Daily Close Fade Sizing Sweep",
        "",
        "This report tests concentration caps and score-tilted sizing on per-coin filtered close-fade trades.",
        "It does not change live/demo trading.",
        "",
        f"Filter specs: `{payload['rows']['filter_specs']}`",
        f"Sizing specs: `{payload['rows']['sizing_specs']}`",
        "",
        "## Most Stable Sizing Rules",
        "",
        "| Rank | Beat All | Pos Splits | Min Ret | Avg Ret | Avg Delta | Worst DD | Worst Day | Avg Sharpe | Avg Gross | Avg Max Coin | Active | Trades | Filter | Sizing |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for index, row in enumerate(summary.head(50).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('beats_baseline_all_splits', False)} | "
            f"{row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('avg_return_delta_vs_baseline'))} | {_pct(row.get('worst_max_drawdown'))} | "
            f"{_pct(row.get('worst_day_return'))} | {_num(row.get('avg_calendar_sharpe_like'), 2)} | "
            f"{_pct(row.get('avg_gross_exposure'))} | {_pct(row.get('avg_max_symbol_weight'))} | "
            f"{_pct(row.get('avg_active_rate'))} | {row.get('trades', 0)} | "
            f"{row.get('filter_label', '')} | {row.get('sizing_label', '')} |"
        )

    lines.extend(
        [
            "",
            "## Baseline And Top Split Detail",
            "",
            "| Split | Total Ret | Delta | DD | Worst Day | Sharpe | Avg Gross | Avg Max Coin | Active | Trades | Filter | Sizing |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    top_pairs = {("baseline", "reallocate_equal")}
    if not summary.is_empty():
        top_pairs.update((row["filter_label"], row["sizing_label"]) for row in summary.head(10).to_dicts())
    pair_expr = None
    for filter_label, sizing_label in top_pairs:
        expr = (pl.col("filter_label") == filter_label) & (pl.col("sizing_label") == sizing_label)
        pair_expr = expr if pair_expr is None else pair_expr | expr
    detail = results.filter(pair_expr) if pair_expr is not None and not results.is_empty() else pl.DataFrame()
    for row in detail.sort(["filter_label", "sizing_label", "split"]).to_dicts() if not detail.is_empty() else []:
        lines.append(
            f"| {row.get('split', '')} | {_pct(row.get('total_return'))} | "
            f"{_pct(row.get('return_delta_vs_baseline'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('worst_day_return'))} | {_num(row.get('calendar_sharpe_like'), 2)} | "
            f"{_pct(row.get('avg_gross_exposure'))} | {_pct(row.get('avg_max_symbol_weight'))} | "
            f"{_pct(row.get('active_rate'))} | {row.get('trades', 0)} | "
            f"{row.get('filter_label', '')} | {row.get('sizing_label', '')} |"
        )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_sizing_sweep_results.csv",
            "daily_close_fade_sizing_sweep_summary.csv",
            "daily_close_fade_sizing_sweep.json",
            "daily_close_fade_sizing_sweep.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_filter_specs(value: str) -> list[coin_sweep.CoinFilterSpec]:
    specs = []
    for item in _csv_str(value):
        excess_pct, vwap, late_volume, min_symbols = item.split(":", 3)
        specs.append(
            coin_sweep.CoinFilterSpec(
                coin_excess_vs_market_min=float(excess_pct) / 100.0,
                coin_vwap_extension_min=float(vwap),
                coin_late_volume_ratio_min=float(late_volume),
                min_symbols=int(min_symbols),
            )
        )
    return specs


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


def _csv_str(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _csv_float(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value))


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
