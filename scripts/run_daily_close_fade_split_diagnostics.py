from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import DEFAULT_MAJOR_SYMBOLS, DailyCloseFadeConfig, load_config
from aggression_carry.daily_close_fade import DailyCloseFadeDiagnosticsConfig, run_daily_close_fade_diagnostics
from aggression_carry.downloaders import parse_date_ms


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)
DEFAULT_SCORES = "vol_adjusted_day_return,day_return,late_volume_ratio,vwap_extension,pump_score"
SCENARIO_COLS = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n"]


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = _base_config(config.daily_close_fade, args)
    diagnostics_base = DailyCloseFadeDiagnosticsConfig(
        signal_minutes=_csv_signal_minutes(args.signal_times),
        entry_delay_minutes=_csv_int(args.entry_delays),
        horizon_minutes=_csv_int(args.horizons),
        scores=_csv_str(args.scores),
        top_ns=_csv_int(args.top_ns),
        buckets=args.buckets,
        min_obs_per_bucket=args.min_obs_per_bucket,
    )
    split_specs = _parse_splits(args.splits)
    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "daily_close_fade_splits"
    output_dir.mkdir(parents=True, exist_ok=True)

    frames: list[pl.DataFrame] = []
    run_rows: list[dict[str, Any]] = []
    for split_name, start, end in split_specs:
        split_dir = output_dir / split_name
        split_config = replace(
            diagnostics_base,
            start_ms=parse_date_ms(start),
            end_ms=parse_date_ms(end),
        )
        payload = run_daily_close_fade_diagnostics(
            data_root,
            diagnostics_config=split_config,
            base_fade_config=base,
            cost_config=config.costs,
            report_dir=split_dir,
        )
        scenario_path = split_dir / "daily_close_fade_diagnostic_scenarios.csv"
        if scenario_path.exists():
            frame = pl.read_csv(scenario_path).with_columns(
                [
                    pl.lit(split_name).alias("split"),
                    pl.lit(start).alias("split_start"),
                    pl.lit(end).alias("split_end"),
                ]
            )
            frames.append(frame)
        run_rows.append(
            {
                "split": split_name,
                "start": start,
                "end": end,
                **payload.get("rows", {}),
            }
        )
        print(
            f"{split_name}: observations={payload.get('rows', {}).get('observations', 0)} "
            f"scenarios={payload.get('rows', {}).get('scenarios', 0)} "
            f"report={split_dir / 'daily_close_fade_diagnostics_report.md'}"
        )

    combined = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
    summary = summarize_split_scenarios(combined, expected_splits=len(split_specs))
    if not combined.is_empty():
        combined.write_csv(output_dir / "daily_close_fade_split_diagnostics_all_scenarios.csv")
    if run_rows:
        pl.DataFrame(run_rows, infer_schema_length=None).write_csv(output_dir / "daily_close_fade_split_diagnostics_runs.csv")
    if not summary.is_empty():
        summary.write_csv(output_dir / "daily_close_fade_split_diagnostics_summary.csv")
    (output_dir / "daily_close_fade_split_diagnostics_summary.md").write_text(
        format_split_summary(summary, run_rows, split_specs),
        encoding="utf-8",
    )
    print(f"summary={output_dir / 'daily_close_fade_split_diagnostics_summary.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily-close-fade diagnostics over named train/validation/OOS splits.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--splits", default=DEFAULT_SPLITS, help="Comma-separated name:start:end specs.")
    parser.add_argument("--signal-times", default="22:15")
    parser.add_argument("--entry-delays", default="1,15,60")
    parser.add_argument("--horizons", default="60,180")
    parser.add_argument("--scores", default=DEFAULT_SCORES)
    parser.add_argument("--top-ns", default="3,5,10")
    parser.add_argument("--buckets", type=int, default=10)
    parser.add_argument("--min-obs-per-bucket", type=int, default=20)
    parser.add_argument("--pump-filter", default="pump")
    parser.add_argument("--min-age-days", type=int, default=10)
    parser.add_argument("--min-day-turnover", type=float, default=None)
    parser.add_argument("--min-last-60m-turnover", type=float, default=None)
    parser.add_argument("--liquidity-lookback-days", type=int, default=7)
    parser.add_argument("--liquidity-rank-min", type=int, default=31)
    parser.add_argument("--liquidity-rank-max", type=int, default=150)
    parser.add_argument("--min-baseline-turnover", type=float, default=None)
    parser.add_argument("--cost-multiplier", type=float, default=1.0)
    parser.add_argument("--exclude-symbols", default=None)
    parser.add_argument("--include-majors", action="store_true")
    parser.add_argument("--require-archive-membership", action="store_true")
    return parser.parse_args()


def summarize_split_scenarios(frame: pl.DataFrame, *, expected_splits: int) -> pl.DataFrame:
    if frame.is_empty():
        return pl.DataFrame()
    return (
        frame.group_by(SCENARIO_COLS, maintain_order=True)
        .agg(
            [
                pl.len().alias("split_rows"),
                pl.col("split").n_unique().alias("splits_seen"),
                pl.col("cost_edge_pass").cast(pl.Int64).sum().alias("cost_pass_splits"),
                pl.col("robust_direction_pass").cast(pl.Int64).sum().alias("raw_pass_splits"),
                pl.col("mean_basket_short_return").mean().alias("avg_mean_basket_short_return"),
                pl.col("mean_basket_short_return").min().alias("min_mean_basket_short_return"),
                pl.col("mean_basket_cost_adjusted_short_return").mean().alias("avg_cost_adjusted_short_return"),
                pl.col("mean_basket_cost_adjusted_short_return").min().alias("min_cost_adjusted_short_return"),
                pl.col("cost_positive_month_rate").mean().alias("avg_cost_positive_month_rate"),
                pl.col("cost_positive_month_rate").min().alias("min_cost_positive_month_rate"),
                pl.col("worst_month_cost_adjusted_short_return").min().alias("worst_month_cost_adjusted_short_return"),
                pl.col("mean_ic").mean().alias("avg_mean_ic"),
                pl.col("ic_t_stat").mean().alias("avg_ic_t_stat"),
                pl.col("baskets").sum().alias("baskets"),
                pl.col("obs").sum().alias("obs"),
            ]
        )
        .with_columns(
            [
                (pl.col("splits_seen") == expected_splits).alias("complete_splits"),
                (pl.col("cost_pass_splits") == expected_splits).alias("all_splits_cost_pass"),
                (pl.col("raw_pass_splits") == expected_splits).alias("all_splits_raw_pass"),
            ]
        )
        .sort(
            [
                "all_splits_cost_pass",
                "cost_pass_splits",
                "min_cost_adjusted_short_return",
                "avg_cost_adjusted_short_return",
                "min_cost_positive_month_rate",
                "avg_ic_t_stat",
            ],
            descending=[True, True, True, True, True, True],
        )
    )


def format_split_summary(
    summary: pl.DataFrame,
    run_rows: list[dict[str, Any]],
    split_specs: list[tuple[str, str, str]],
) -> str:
    lines = [
        "# Daily Close Fade Split Diagnostics",
        "",
        "This compares raw daily-close-fade diagnostic scenarios across fixed time splits.",
        "It does not change trading logic and does not run TP/SL optimization.",
        "",
        "## Splits",
        "",
        "| Split | Start | End | Observations | Scenarios |",
        "|---|---:|---:|---:|---:|",
    ]
    run_lookup = {row["split"]: row for row in run_rows}
    for name, start, end in split_specs:
        row = run_lookup.get(name, {})
        lines.append(
            f"| {name} | {start} | {end} | {row.get('observations', 0)} | {row.get('scenarios', 0)} |"
        )

    lines.extend(
        [
            "",
            "## Best Cross-Split Scenarios",
            "",
            "| Rank | All Cost Pass | Cost Passes | Min Cost Adj | Avg Cost Adj | Min Cost+ Months | Worst Month | Avg IC t | Signal | Delay | Horizon | Top N | Score | Obs |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
        ]
    )
    for index, row in enumerate(summary.head(40).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('all_splits_cost_pass', False)} | "
            f"{row.get('cost_pass_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_cost_adjusted_short_return'))} | "
            f"{_pct(row.get('avg_cost_adjusted_short_return'))} | "
            f"{_pct(row.get('min_cost_positive_month_rate'))} | "
            f"{_pct(row.get('worst_month_cost_adjusted_short_return'))} | "
            f"{_num(row.get('avg_ic_t_stat'), 2)} | "
            f"{_format_signal_minute(row.get('signal_minute', 0))} | "
            f"{row.get('entry_delay_minutes', 0)} | {row.get('horizon_minutes', 0)} | "
            f"{row.get('top_n', 0)} | {row.get('score', '')} | {row.get('obs', 0)} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_split_diagnostics_runs.csv",
            "daily_close_fade_split_diagnostics_all_scenarios.csv",
            "daily_close_fade_split_diagnostics_summary.csv",
            "daily_close_fade_split_diagnostics_summary.md",
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
        pump_filter=args.pump_filter,
        min_age_days=args.min_age_days,
        min_day_turnover=args.min_day_turnover if args.min_day_turnover is not None else base.min_day_turnover,
        min_last_60m_turnover=(
            args.min_last_60m_turnover
            if args.min_last_60m_turnover is not None
            else base.min_last_60m_turnover
        ),
        liquidity_lookback_days=args.liquidity_lookback_days,
        liquidity_rank_min=args.liquidity_rank_min,
        liquidity_rank_max=args.liquidity_rank_max,
        min_baseline_turnover=(
            args.min_baseline_turnover if args.min_baseline_turnover is not None else base.min_baseline_turnover
        ),
        cost_multiplier=args.cost_multiplier,
        exclude_symbols=tuple(sorted(exclusions)),
        require_archive_membership=args.require_archive_membership or base.require_archive_membership,
    )


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


def _csv_signal_minutes(value: str) -> tuple[int, ...]:
    output = []
    for item in _csv_str(value):
        hour, minute = item.split(":", 1)
        output.append(int(hour) * 60 + int(minute))
    return tuple(output)


def _csv_int(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value))


def _csv_str(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    return f"{number:.2%}"


def _num(value: Any, digits: int) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _format_signal_minute(value: Any) -> str:
    number = int(value)
    return f"{number // 60:02d}:{number % 60:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
