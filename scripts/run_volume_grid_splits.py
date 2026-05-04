from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import VolumeBacktestConfig, VolumeGridConfig, load_config
from aggression_carry.volume_backtest import run_volume_grid


DEFAULT_SPLITS = (
    "train_2023_2024:2023-05-03:2024-05-03,"
    "validation_2024_2025:2024-05-03:2025-05-03,"
    "oos_2025_2026:2025-05-03:2026-05-03"
)

SCENARIO_COLS = [
    "score",
    "quantile",
    "hold_days",
    "rebalance_days",
    "gross_exposure",
    "entry_delay_hours",
    "stop_mode",
    "stop_loss_pct",
    "vol_stop_multiplier",
    "vol_stop_lookback_days",
    "min_stop_loss_pct",
    "max_stop_loss_pct",
    "take_profit_pct",
    "min_symbols",
    "cost_multiplier",
    "side_mode",
    "rank_exit_enabled",
    "rank_exit_threshold",
    "universe_rank_min",
    "universe_rank_max",
    "universe_min_daily_turnover",
    "include_symbols",
    "exclude_symbols",
]


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    base = _base_config(config.volume_backtest, args)
    grid = _grid_config(args)
    split_specs = _parse_splits(args.splits)
    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "volume_grid_splits"
    output_dir.mkdir(parents=True, exist_ok=True)

    frames: list[pl.DataFrame] = []
    run_rows: list[dict[str, Any]] = []
    for split_name, start, end in split_specs:
        split_dir = output_dir / split_name
        split_base = replace(base, start_date=start, end_date=end)
        payload = run_volume_grid(
            data_root,
            grid_config=grid,
            base_backtest_config=split_base,
            cost_config=config.costs,
            max_workers=args.workers,
            report_dir=split_dir,
        )
        result_path = split_dir / "volume_grid_results.csv"
        if result_path.exists():
            frames.append(
                pl.read_csv(result_path).with_columns(
                    [
                        pl.lit(split_name).alias("split"),
                        pl.lit(start).alias("split_start"),
                        pl.lit(end).alias("split_end"),
                    ]
                )
            )
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
            f"report={split_dir / 'volume_grid_report.md'}"
        )

    combined = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
    summary = summarize_volume_grid_splits(combined, expected_splits=len(split_specs))
    if not combined.is_empty():
        combined.write_csv(output_dir / "volume_grid_split_all_results.csv")
    if run_rows:
        pl.DataFrame(run_rows, infer_schema_length=None).write_csv(output_dir / "volume_grid_split_runs.csv")
    if not summary.is_empty():
        summary.write_csv(output_dir / "volume_grid_split_summary.csv")
    (output_dir / "volume_grid_split_summary.md").write_text(
        format_volume_grid_split_summary(summary, run_rows, split_specs),
        encoding="utf-8",
    )
    print(f"summary={output_dir / 'volume_grid_split_summary.md'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run volume-alpha grids over named train/validation/OOS splits.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--splits", default=DEFAULT_SPLITS, help="Comma-separated name:start:end specs.")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--scores", default="dollar_volume_rank")
    parser.add_argument("--quantiles", default="0.2,0.3,0.5")
    parser.add_argument("--hold-days", default="3,5,7,10,14")
    parser.add_argument("--fixed-stops", default="0,0.12,0.2,0.3")
    parser.add_argument("--vol-stops", default="3,4")
    parser.add_argument("--rank-exits", default="false,true")
    parser.add_argument("--take-profits", default="0")
    parser.add_argument("--cost-multipliers", default="1,2,3")
    parser.add_argument("--include-reverse", action="store_true")
    parser.add_argument("--gross-exposure", type=float, default=None)
    parser.add_argument("--entry-delay-hours", type=int, default=None)
    parser.add_argument("--universe-rank-min", type=int, default=None)
    parser.add_argument("--universe-rank-max", type=int, default=None)
    parser.add_argument("--universe-min-daily-turnover", type=float, default=None)
    parser.add_argument("--include-symbols", default=None)
    parser.add_argument("--exclude-symbols", default=None)
    return parser.parse_args()


def summarize_volume_grid_splits(frame: pl.DataFrame, *, expected_splits: int) -> pl.DataFrame:
    if frame.is_empty():
        return pl.DataFrame()
    _require_columns(frame, ["split", "total_return", "sharpe_like", "max_drawdown"], label="volume grid split results")
    scenario_cols = [column for column in SCENARIO_COLS if column in frame.columns]
    trade_col = "trades" if "trades" in frame.columns else "trade_count"
    aggregations = [
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
        pl.col(trade_col).sum().alias("trade_count") if trade_col in frame.columns else pl.lit(0).alias("trade_count"),
    ]
    for optional in ("trade_win_rate", "long_return", "short_return", "cost_return"):
        if optional in frame.columns:
            aggregations.append(pl.col(optional).mean().alias(f"avg_{optional}"))
    return (
        frame.group_by(scenario_cols, maintain_order=True)
        .agg(aggregations)
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
                "min_total_return",
                "stability_score",
                "avg_sharpe_like",
            ],
            descending=[True, True, True, True, True],
        )
    )


def format_volume_grid_split_summary(
    summary: pl.DataFrame,
    run_rows: list[dict[str, Any]],
    split_specs: list[tuple[str, str, str]],
) -> str:
    lines = [
        "# Volume Alpha Grid Split Summary",
        "",
        "This compares volume-alpha grid variants across fixed time splits.",
        "Use it to prefer robust settings over the single best historical row.",
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
            "| Rank | All Positive | Pos Splits | Min Return | Avg Return | Return Std | Stability | Worst DD | Avg Sharpe | Ranks | Hold | Quantile | Stop | Rank Exit | Side | Cost | Long | Short | Trades |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(summary.head(50).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('all_splits_positive', False)} | "
            f"{row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('total_return_std'))} | {_pct(row.get('stability_score'))} | "
            f"{_pct(row.get('worst_max_drawdown'))} | {_num(row.get('avg_sharpe_like'), 2)} | "
            f"{row.get('universe_rank_min', 1)}-{row.get('universe_rank_max', 0) or 'all'} | "
            f"{row.get('hold_days', 0)}d | {_pct(row.get('quantile'))} | "
            f"{_stop_label(row)} | {row.get('rank_exit_enabled', False)} | {row.get('side_mode', '')} | "
            f"{_num(row.get('cost_multiplier'), 1)}x | {_pct(row.get('avg_long_return'))} | "
            f"{_pct(row.get('avg_short_return'))} | {row.get('trade_count', 0)} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "volume_grid_split_runs.csv",
            "volume_grid_split_all_results.csv",
            "volume_grid_split_summary.csv",
            "volume_grid_split_summary.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _base_config(base: VolumeBacktestConfig, args: argparse.Namespace) -> VolumeBacktestConfig:
    values = {
        "gross_exposure": args.gross_exposure if args.gross_exposure is not None else base.gross_exposure,
        "entry_delay_hours": args.entry_delay_hours if args.entry_delay_hours is not None else base.entry_delay_hours,
        "universe_rank_min": args.universe_rank_min if args.universe_rank_min is not None else base.universe_rank_min,
        "universe_rank_max": args.universe_rank_max if args.universe_rank_max is not None else base.universe_rank_max,
        "universe_min_daily_turnover": (
            args.universe_min_daily_turnover
            if args.universe_min_daily_turnover is not None
            else base.universe_min_daily_turnover
        ),
        "include_symbols": _csv_str(args.include_symbols) if args.include_symbols is not None else base.include_symbols,
        "exclude_symbols": _csv_str(args.exclude_symbols) if args.exclude_symbols is not None else base.exclude_symbols,
    }
    return replace(base, **values)


def _grid_config(args: argparse.Namespace) -> VolumeGridConfig:
    return VolumeGridConfig(
        scores=_csv_str(args.scores),
        quantiles=_csv_float(args.quantiles),
        hold_days=_csv_int(args.hold_days),
        fixed_stop_loss_pcts=_csv_float(args.fixed_stops),
        vol_stop_multipliers=_csv_float(args.vol_stops),
        rank_exit_modes=_csv_bool(args.rank_exits),
        include_reverse_side=args.include_reverse,
        take_profit_pcts=_csv_float(args.take_profits),
        cost_multipliers=_csv_float(args.cost_multipliers),
    )


def _parse_splits(value: str) -> list[tuple[str, str, str]]:
    splits = []
    for item in _csv_str(value):
        name, start, end = item.split(":", 2)
        if not name or not start or not end:
            raise ValueError(f"Invalid split spec: {item!r}")
        if _date_ms(end) <= _date_ms(start):
            raise ValueError(f"Split end must be after start: {item!r}")
        splits.append((name.strip(), start.strip(), end.strip()))
    if not splits:
        raise ValueError("At least one split is required")
    return splits


def _csv_int(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value))


def _csv_float(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value))


def _csv_bool(value: str) -> tuple[bool, ...]:
    return tuple(item.lower() in {"1", "true", "yes", "y", "on"} for item in _csv_str(value))


def _csv_str(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


def _require_columns(frame: pl.DataFrame, columns: list[str], *, label: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{label} missing columns: {', '.join(missing)}")


def _date_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp() * 1000)


def _stop_label(row: dict[str, Any]) -> str:
    mode = str(row.get("stop_mode", "none"))
    if mode == "none":
        return "none"
    if mode == "volatility":
        return f"{_num(row.get('vol_stop_multiplier'), 2)}x vol"
    return _pct(row.get("stop_loss_pct"))


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
