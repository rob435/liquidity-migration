from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, asdict
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.downloaders import parse_date_ms


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)
EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class ContextFilterSpec:
    selected_excess_vs_market_min: float = 0.0
    selected_avg_vwap_extension_min: float = 0.0
    selected_avg_late_volume_ratio_min: float = 0.0
    market_positive_rate_max: float = 1.0
    btc_day_return_max: float = 99.0
    min_trade_count: int = 1

    @property
    def label(self) -> str:
        parts = [
            f"excess>={self.selected_excess_vs_market_min:.1%}",
            f"vwap>={self.selected_avg_vwap_extension_min:.1%}",
            f"latevol>={self.selected_avg_late_volume_ratio_min:.2f}x",
            f"mkt+<={self.market_positive_rate_max:.0%}",
            "btc<=none" if self.btc_day_return_max >= 10.0 else f"btc<={self.btc_day_return_max:.1%}",
            f"n>={self.min_trade_count}",
        ]
        return " ".join(parts)


def main() -> int:
    args = parse_args()
    day_audit_path = Path(args.day_audit_csv) if args.day_audit_csv else (
        Path(args.data_root) / "reports" / "daily_close_fade_day_audit" / "daily_close_fade_day_audit.csv"
    )
    if not day_audit_path.exists():
        raise FileNotFoundError(f"Day audit CSV not found: {day_audit_path}")
    day_rows = pl.read_csv(day_audit_path)
    split_specs = _parse_splits(args.splits)
    specs = build_filter_specs(
        excess_thresholds=_csv_float(args.excess_thresholds),
        vwap_thresholds=_csv_float(args.vwap_extension_thresholds),
        late_volume_thresholds=_csv_float(args.late_volume_thresholds),
        market_positive_maxes=_csv_float(args.market_positive_maxes),
        btc_day_return_maxes=_csv_float(args.btc_day_return_maxes),
        min_trade_counts=_csv_int(args.min_trade_counts),
    )
    results = evaluate_filter_sweep(day_rows, specs=specs, split_specs=split_specs)
    summary = summarize_filter_sweep(
        results,
        expected_splits=len(split_specs),
        min_active_days=args.min_active_days,
        min_active_rate=args.min_active_rate,
    )
    output_dir = Path(args.report_dir) if args.report_dir else day_audit_path.parent / "filter_sweep"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "day_audit_csv": str(day_audit_path),
        "splits": [{"name": name, "start": start, "end": end} for name, start, end in split_specs],
        "thresholds": {
            "excess": _csv_float(args.excess_thresholds),
            "vwap_extension": _csv_float(args.vwap_extension_thresholds),
            "late_volume": _csv_float(args.late_volume_thresholds),
            "market_positive_max": _csv_float(args.market_positive_maxes),
            "btc_day_return_max": _csv_float(args.btc_day_return_maxes),
            "min_trade_count": _csv_int(args.min_trade_counts),
        },
        "gates": {
            "min_active_days": args.min_active_days,
            "min_active_rate": args.min_active_rate,
        },
        "rows": {
            "day_rows": day_rows.height,
            "specs": len(specs),
            "results": results.height,
            "summary": summary.height,
        },
        "top_summary": summary.head(25).to_dicts() if not summary.is_empty() else [],
    }
    (output_dir / "daily_close_fade_filter_sweep.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_filter_sweep.md").write_text(
        format_filter_sweep_report(payload, results, summary),
        encoding="utf-8",
    )
    if not results.is_empty():
        results.write_csv(output_dir / "daily_close_fade_filter_sweep_results.csv")
    if not summary.is_empty():
        summary.write_csv(output_dir / "daily_close_fade_filter_sweep_summary.csv")
    print(f"filter_sweep={output_dir / 'daily_close_fade_filter_sweep.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promotion-style sweep for daily-close-fade pre-signal context filters.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--day-audit-csv", default="")
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--splits", default=DEFAULT_SPLITS)
    parser.add_argument("--excess-thresholds", default="0,0.05,0.08,0.11,0.15")
    parser.add_argument("--vwap-extension-thresholds", default="0,0.025,0.035,0.045,0.06")
    parser.add_argument("--late-volume-thresholds", default="0,0.75,1.0,1.25,1.5")
    parser.add_argument("--market-positive-maxes", default="1.0,0.95,0.88,0.72")
    parser.add_argument("--btc-day-return-maxes", default="99,0.019,0.0075,0")
    parser.add_argument("--min-trade-counts", default="1,2,3")
    parser.add_argument("--min-active-days", type=int, default=40)
    parser.add_argument("--min-active-rate", type=float, default=0.20)
    return parser.parse_args()


def build_filter_specs(
    *,
    excess_thresholds: tuple[float, ...],
    vwap_thresholds: tuple[float, ...],
    late_volume_thresholds: tuple[float, ...],
    market_positive_maxes: tuple[float, ...],
    btc_day_return_maxes: tuple[float, ...],
    min_trade_counts: tuple[int, ...],
) -> list[ContextFilterSpec]:
    specs = []
    seen = set()
    for values in product(
        excess_thresholds,
        vwap_thresholds,
        late_volume_thresholds,
        market_positive_maxes,
        btc_day_return_maxes,
        min_trade_counts,
    ):
        spec = ContextFilterSpec(*values)
        key = asdict(spec)
        key_tuple = tuple(key.items())
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        specs.append(spec)
    return specs


def evaluate_filter_sweep(
    day_rows: pl.DataFrame,
    *,
    specs: list[ContextFilterSpec],
    split_specs: list[tuple[str, str, str]],
) -> pl.DataFrame:
    if day_rows.is_empty():
        return pl.DataFrame()
    baseline_by_split = {
        split_name: summarize_selection(
            _filter_split(day_rows, start, end),
            _filter_split(day_rows, start, end),
            split=split_name,
            spec=ContextFilterSpec(),
            baseline_total_return=0.0,
            baseline_max_drawdown=0.0,
            label="baseline",
        )
        for split_name, start, end in split_specs
    }
    rows: list[dict[str, Any]] = []
    for split_name, start, end in split_specs:
        base = _filter_split(day_rows, start, end)
        baseline = baseline_by_split[split_name]
        rows.append({**baseline, "return_delta_vs_baseline": 0.0, "drawdown_delta_vs_baseline": 0.0})
        for spec in specs:
            selected = select_rows(base, spec)
            rows.append(
                summarize_selection(
                    base,
                    selected,
                    split=split_name,
                    spec=spec,
                    baseline_total_return=float(baseline["total_return"]),
                    baseline_max_drawdown=float(baseline["max_drawdown"]),
                    label=spec.label,
                )
            )
    return pl.DataFrame(rows, infer_schema_length=None).sort(["split", "total_return"], descending=[False, True])


def select_rows(day_rows: pl.DataFrame, spec: ContextFilterSpec) -> pl.DataFrame:
    if day_rows.is_empty():
        return day_rows
    return day_rows.filter(
        (pl.col("selected_excess_vs_market") >= spec.selected_excess_vs_market_min)
        & (pl.col("selected_avg_vwap_extension") >= spec.selected_avg_vwap_extension_min)
        & (pl.col("selected_avg_late_volume_ratio") >= spec.selected_avg_late_volume_ratio_min)
        & (pl.col("market_positive_rate") <= spec.market_positive_rate_max)
        & (pl.col("btc_day_return") <= spec.btc_day_return_max)
        & (pl.col("trade_count") >= spec.min_trade_count)
    )


def summarize_selection(
    base: pl.DataFrame,
    selected: pl.DataFrame,
    *,
    split: str,
    spec: ContextFilterSpec,
    baseline_total_return: float,
    baseline_max_drawdown: float,
    label: str,
) -> dict[str, Any]:
    selected_ids = set(selected["basket_id"].to_list()) if not selected.is_empty() and "basket_id" in selected.columns else set()
    returns = [
        float(row["basket_return"]) if row["basket_id"] in selected_ids else 0.0
        for row in base.select(["basket_id", "basket_return"]).to_dicts()
    ]
    equity = _equity_from_returns(returns)
    selected_returns = selected["basket_return"].to_list() if not selected.is_empty() else []
    mean_return = statistics.fmean(returns) if returns else 0.0
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    total_return = float(equity[-1] - 1.0) if equity else 0.0
    max_drawdown = _max_drawdown(equity)
    return {
        "split": split,
        "label": label,
        **asdict(spec),
        "base_days": base.height,
        "selected_days": selected.height,
        "skipped_days": base.height - selected.height,
        "active_rate": float(selected.height / base.height) if base.height else 0.0,
        "total_return": total_return,
        "return_delta_vs_baseline": total_return - baseline_total_return,
        "max_drawdown": max_drawdown,
        "drawdown_delta_vs_baseline": max_drawdown - baseline_max_drawdown,
        "calendar_mean_return": float(mean_return),
        "calendar_sharpe_like": float((mean_return / stdev) * math.sqrt(365.0)) if stdev > EPSILON else 0.0,
        "selected_avg_return": float(statistics.fmean(selected_returns)) if selected_returns else 0.0,
        "selected_hit_rate": _hit_rate(selected_returns),
        "worst_selected_day": float(min(selected_returns)) if selected_returns else 0.0,
        "best_selected_day": float(max(selected_returns)) if selected_returns else 0.0,
        "trades": int(selected["trade_count"].sum()) if not selected.is_empty() and "trade_count" in selected.columns else 0,
    }


def summarize_filter_sweep(
    results: pl.DataFrame,
    *,
    expected_splits: int,
    min_active_days: int,
    min_active_rate: float,
) -> pl.DataFrame:
    if results.is_empty():
        return pl.DataFrame()
    cols = [
        "label",
        "selected_excess_vs_market_min",
        "selected_avg_vwap_extension_min",
        "selected_avg_late_volume_ratio_min",
        "market_positive_rate_max",
        "btc_day_return_max",
        "min_trade_count",
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
                pl.col("total_return").max().alias("max_total_return"),
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
                    (pl.col("splits_seen") == expected_splits)
                    & (pl.col("positive_return_splits") == expected_splits)
                    & (pl.col("min_selected_days") >= min_active_days)
                    & (pl.col("avg_active_rate") >= min_active_rate)
                ).alias("promotion_candidate"),
                (
                    pl.col("min_total_return")
                    + pl.col("avg_total_return")
                    - pl.col("total_return_std").fill_null(0.0)
                    + (pl.col("avg_calendar_sharpe_like") / 100.0)
                ).alias("stability_score"),
            ]
        )
        .sort(
            [
                "promotion_candidate",
                "beats_baseline_all_splits",
                "all_splits_positive",
                "stability_score",
                "min_total_return",
                "avg_calendar_sharpe_like",
            ],
            descending=[True, True, True, True, True, True],
        )
    )


def format_filter_sweep_report(payload: dict[str, Any], results: pl.DataFrame, summary: pl.DataFrame) -> str:
    lines = [
        "# Daily Close Fade Context Filter Sweep",
        "",
        "This report tests pre-signal context filters found in the day audit.",
        "It is a promotion-style research report, not a live/demo config change.",
        "",
        f"Day audit CSV: `{payload['day_audit_csv']}`",
        f"Specs tested: `{payload['rows']['specs']}`",
        f"Gate: min_active_days={payload['gates']['min_active_days']} "
        f"min_active_rate={payload['gates']['min_active_rate']:.0%}",
        "",
        "## Most Stable Filters",
        "",
        "| Rank | Promote | Beat All | Pos Splits | Min Ret | Avg Ret | Avg Delta | Worst DD | Avg Sharpe | Active | Min Days | Trades | Filter |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for index, row in enumerate(summary.head(50).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('promotion_candidate', False)} | "
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
            "| Split | Total Ret | Delta | DD | Sharpe | Active | Days | Trades | Filter |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    top_labels = {"baseline"}
    if not summary.is_empty():
        top_labels.update(summary.head(10)["label"].to_list())
    detail = results.filter(pl.col("label").is_in(top_labels)) if not results.is_empty() else pl.DataFrame()
    for row in detail.sort(["label", "split"]).to_dicts() if not detail.is_empty() else []:
        lines.append(
            f"| {row.get('split', '')} | {_pct(row.get('total_return'))} | "
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
            "daily_close_fade_filter_sweep_results.csv",
            "daily_close_fade_filter_sweep_summary.csv",
            "daily_close_fade_filter_sweep.json",
            "daily_close_fade_filter_sweep.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _filter_split(day_rows: pl.DataFrame, start: str, end: str) -> pl.DataFrame:
    return day_rows.filter((pl.col("date") >= start) & (pl.col("date") < end)).sort("date")


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
