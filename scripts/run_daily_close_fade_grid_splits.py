from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import DEFAULT_MAJOR_SYMBOLS, DailyCloseFadeConfig, DailyCloseFadeGridConfig, load_config
from aggression_carry.daily_close_fade import run_daily_close_fade_grid
from aggression_carry.downloaders import parse_date_ms


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)

SCENARIO_COLS = [
    "signal_minute",
    "top_n",
    "hold_minutes",
    "entry_delay_minutes",
    "gross_exposure",
    "score",
    "pump_filter",
    "min_age_days",
    "min_day_turnover",
    "min_last_60m_turnover",
    "liquidity_lookback_days",
    "liquidity_rank_min",
    "liquidity_rank_max",
    "min_baseline_turnover",
    "account_equity",
    "max_position_weight",
    "max_trade_notional_pct_of_day_turnover",
    "max_trade_notional_pct_of_baseline_turnover",
    "stop_loss_pct",
    "take_profit_pct",
    "basket_stop_loss_pct",
    "trailing_stop_pct",
    "trailing_activation_pct",
    "vol_trailing_stop_mult",
    "vol_trailing_activation_mult",
    "mfe_giveback_activation_pct",
    "mfe_giveback_pct",
    "vwap_reversion_pct",
    "stop_delay_minutes",
    "cost_multiplier",
    "round_trip_cost_bps",
    "min_symbols",
    "require_archive_membership",
    "exclude_symbols",
]


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = _base_config(config.daily_close_fade, args)
    grid_base = _grid_config(args, config.daily_close_fade_grid)
    split_specs = _parse_splits(args.splits)
    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "daily_close_fade_grid_splits"
    output_dir.mkdir(parents=True, exist_ok=True)

    frames: list[pl.DataFrame] = []
    run_rows: list[dict[str, Any]] = []
    for split_name, start, end in split_specs:
        split_dir = output_dir / split_name
        split_grid = replace(grid_base, start_ms=parse_date_ms(start), end_ms=parse_date_ms(end))
        payload = run_daily_close_fade_grid(
            data_root,
            grid_config=split_grid,
            base_fade_config=base,
            cost_config=config.costs,
            max_workers=args.workers,
            report_dir=split_dir,
        )
        result_path = split_dir / "daily_close_fade_grid_results.csv"
        if result_path.exists():
            frame = pl.read_csv(result_path).with_columns(
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
                "rows": payload.get("rows", 0),
                "workers": payload.get("workers", 0),
                "worker_backend": payload.get("worker_backend", ""),
            }
        )
        print(
            f"{split_name}: rows={payload.get('rows', 0)} "
            f"report={split_dir / 'daily_close_fade_grid_report.md'}"
        )

    combined = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
    summary = summarize_grid_splits(combined, expected_splits=len(split_specs))
    if not combined.is_empty():
        combined.write_csv(output_dir / "daily_close_fade_grid_split_all_results.csv")
    if run_rows:
        pl.DataFrame(run_rows, infer_schema_length=None).write_csv(output_dir / "daily_close_fade_grid_split_runs.csv")
    if not summary.is_empty():
        summary.write_csv(output_dir / "daily_close_fade_grid_split_summary.csv")
    (output_dir / "daily_close_fade_grid_split_summary.md").write_text(
        format_grid_split_summary(summary, run_rows, split_specs),
        encoding="utf-8",
    )
    print(f"summary={output_dir / 'daily_close_fade_grid_split_summary.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily-close-fade exit grids over fixed time splits.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--splits", default=DEFAULT_SPLITS, help="Comma-separated name:start:end specs.")
    parser.add_argument("--signal-times", default="22:15")
    parser.add_argument("--top-ns", default="5")
    parser.add_argument("--hold-minutes", default="180")
    parser.add_argument("--gross-exposures", default="1.0")
    parser.add_argument("--scores", default="vol_adjusted_day_return")
    parser.add_argument("--pump-filters", default="pump")
    parser.add_argument("--stop-loss-pcts", default="0,0.2")
    parser.add_argument("--take-profit-pcts", default="0")
    parser.add_argument("--basket-stop-loss-pcts", default="0")
    parser.add_argument("--trailing-stop-pcts", default="0")
    parser.add_argument("--trailing-activation-pcts", default="0")
    parser.add_argument("--vol-trailing-stop-mults", default="0,0.25")
    parser.add_argument("--vol-trailing-activation-mults", default="0")
    parser.add_argument("--mfe-giveback-activation-pcts", default="0,0.01")
    parser.add_argument("--mfe-giveback-pcts", default="0,0.2")
    parser.add_argument("--vwap-reversion-pcts", default="0")
    parser.add_argument("--liquidity-lookback-days", default="7")
    parser.add_argument("--liquidity-rank-mins", default="31")
    parser.add_argument("--liquidity-rank-maxs", default="150")
    parser.add_argument("--min-baseline-turnovers", default="0")
    parser.add_argument("--account-equities", default="10000")
    parser.add_argument("--max-position-weights", default="0")
    parser.add_argument("--max-trade-notional-pct-day-turnovers", default="0")
    parser.add_argument("--max-trade-notional-pct-baseline-turnovers", default="0")
    parser.add_argument("--cost-multipliers", default="1,2,3")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--min-age-days", type=int, default=10)
    parser.add_argument("--min-day-turnover", type=float, default=None)
    parser.add_argument("--min-last-60m-turnover", type=float, default=None)
    parser.add_argument("--exclude-symbols", default=None)
    parser.add_argument("--include-majors", action="store_true")
    parser.add_argument("--require-archive-membership", action="store_true")
    return parser.parse_args()


def summarize_grid_splits(frame: pl.DataFrame, *, expected_splits: int) -> pl.DataFrame:
    if frame.is_empty():
        return pl.DataFrame()
    scenario_cols = [column for column in SCENARIO_COLS if column in frame.columns]
    summary = frame.group_by(scenario_cols, maintain_order=True).agg(
        [
            pl.len().alias("split_rows"),
            pl.col("split").n_unique().alias("splits_seen"),
            (pl.col("total_return") > 0.0).cast(pl.Int64).sum().alias("positive_return_splits"),
            pl.col("total_return").mean().alias("avg_total_return"),
            pl.col("total_return").min().alias("min_total_return"),
            pl.col("total_return").max().alias("max_total_return"),
            pl.col("total_return").std(ddof=0).fill_null(0.0).alias("total_return_std"),
            pl.col("sharpe_like").mean().alias("avg_sharpe_like"),
            pl.col("sharpe_like").min().alias("min_sharpe_like"),
            pl.col("max_drawdown").min().alias("worst_max_drawdown"),
            pl.col("trade_count").sum().alias("trade_count"),
            pl.col("win_rate").mean().alias("avg_win_rate"),
        ]
    )
    return (
        summary.with_columns(
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
                "min_total_return",
                "stability_score",
                "avg_sharpe_like",
            ],
            descending=[True, True, True, True, True],
        )
    )


def format_grid_split_summary(
    summary: pl.DataFrame,
    run_rows: list[dict[str, Any]],
    split_specs: list[tuple[str, str, str]],
) -> str:
    lines = [
        "# Daily Close Fade Grid Split Summary",
        "",
        "This compares TP/SL and adaptive-exit grid variants across fixed time splits.",
        "It is an overfit guard, not alpha proof. Prefer variants that survive",
        "every split over variants with one spectacular historical window.",
        "",
        "## Splits",
        "",
        "| Split | Start | End | Rows | Workers | Backend |",
        "|---|---:|---:|---:|---:|---|",
    ]
    run_lookup = {row["split"]: row for row in run_rows}
    for name, start, end in split_specs:
        row = run_lookup.get(name, {})
        lines.append(
            f"| {name} | {start} | {end} | {row.get('rows', 0)} | "
            f"{row.get('workers', 0)} | {row.get('worker_backend', '')} |"
        )

    lines.extend(
        [
            "",
            "## Most Stable Variants",
            "",
            "| Rank | All Positive | Pos Splits | Min Return | Avg Return | Return Std | Stability | Worst DD | Avg Sharpe | Signal | Hold | Top N | Stop | TP | Vol Trail | MFE GB | VWAP | Cost | Trades |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(summary.head(40).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('all_splits_positive', False)} | "
            f"{row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('total_return_std'))} | {_pct(row.get('stability_score'))} | "
            f"{_pct(row.get('worst_max_drawdown'))} | {_num(row.get('avg_sharpe_like'), 2)} | "
            f"{_format_signal_minute(row.get('signal_minute', 0))} | {row.get('hold_minutes', 0)} | "
            f"{row.get('top_n', 0)} | {_pct(row.get('stop_loss_pct'))} | {_pct(row.get('take_profit_pct'))} | "
            f"{_num(row.get('vol_trailing_stop_mult'), 2)}x | {_pct(row.get('mfe_giveback_pct'))} | "
            f"{_pct(row.get('vwap_reversion_pct'))} | {_num(row.get('cost_multiplier'), 1)}x | "
            f"{row.get('trade_count', 0)} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "daily_close_fade_grid_split_runs.csv",
            "daily_close_fade_grid_split_all_results.csv",
            "daily_close_fade_grid_split_summary.csv",
            "daily_close_fade_grid_split_summary.md",
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


def _grid_config(args: argparse.Namespace, base: DailyCloseFadeGridConfig) -> DailyCloseFadeGridConfig:
    return replace(
        base,
        signal_minutes=_csv_signal_minutes(args.signal_times),
        top_ns=_csv_int(args.top_ns),
        hold_minutes=_csv_int(args.hold_minutes),
        gross_exposures=_csv_float(args.gross_exposures),
        scores=_csv_str(args.scores),
        pump_filters=_csv_str(args.pump_filters),
        stop_loss_pcts=_csv_float(args.stop_loss_pcts),
        take_profit_pcts=_csv_float(args.take_profit_pcts),
        basket_stop_loss_pcts=_csv_float(args.basket_stop_loss_pcts),
        trailing_stop_pcts=_csv_float(args.trailing_stop_pcts),
        trailing_activation_pcts=_csv_float(args.trailing_activation_pcts),
        vol_trailing_stop_mults=_csv_float(args.vol_trailing_stop_mults),
        vol_trailing_activation_mults=_csv_float(args.vol_trailing_activation_mults),
        mfe_giveback_activation_pcts=_csv_float(args.mfe_giveback_activation_pcts),
        mfe_giveback_pcts=_csv_float(args.mfe_giveback_pcts),
        vwap_reversion_pcts=_csv_float(args.vwap_reversion_pcts),
        liquidity_lookback_days=_csv_int(args.liquidity_lookback_days),
        liquidity_rank_mins=_csv_int(args.liquidity_rank_mins),
        liquidity_rank_maxs=_csv_int(args.liquidity_rank_maxs),
        min_baseline_turnovers=_csv_float(args.min_baseline_turnovers),
        account_equities=_csv_float(args.account_equities),
        max_position_weights=_csv_float(args.max_position_weights),
        max_trade_notional_pct_day_turnovers=_csv_float(args.max_trade_notional_pct_day_turnovers),
        max_trade_notional_pct_baseline_turnovers=_csv_float(args.max_trade_notional_pct_baseline_turnovers),
        cost_multipliers=_csv_float(args.cost_multipliers),
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


def _format_signal_minute(value: Any) -> str:
    number = int(value)
    return f"{number // 60:02d}:{number % 60:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
