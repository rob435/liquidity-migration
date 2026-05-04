from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import polars as pl


JOIN_COLS = ["score", "signal_minute", "entry_delay_minutes", "horizon_minutes", "top_n"]


def main() -> int:
    args = parse_args()
    diagnostic_path = Path(args.diagnostic_summary)
    grid_path = Path(args.grid_summary)
    output_dir = Path(args.output_dir) if args.output_dir else grid_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    diagnostic_summary = _read_csv(diagnostic_path)
    grid_summary = _read_csv(grid_path)
    table = build_promotion_table(
        diagnostic_summary,
        grid_summary,
        require_raw_cost_pass=not args.allow_raw_cost_fail,
        min_raw_cost_pass_splits=args.min_raw_cost_pass_splits,
        min_grid_positive_splits=args.min_grid_positive_splits,
        min_grid_min_return=args.min_grid_min_return,
        min_grid_stability=args.min_grid_stability,
    )
    metadata = {
        "diagnostic_summary": str(diagnostic_path),
        "grid_summary": str(grid_path),
        "require_raw_cost_pass": not args.allow_raw_cost_fail,
        "min_raw_cost_pass_splits": args.min_raw_cost_pass_splits,
        "min_grid_positive_splits": args.min_grid_positive_splits,
        "min_grid_min_return": args.min_grid_min_return,
        "min_grid_stability": args.min_grid_stability,
        "rows": table.height,
        "promotable_rows": (
            table.filter(pl.col("promotion_gate_pass")).height if not table.is_empty() else 0
        ),
    }
    if not table.is_empty():
        table.write_csv(output_dir / "daily_close_fade_promotion_candidates.csv")
    (output_dir / "daily_close_fade_promotion_report.json").write_text(
        json.dumps(
            {
                **metadata,
                "top_rows": table.head(50).to_dicts() if not table.is_empty() else [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "daily_close_fade_promotion_report.md").write_text(
        format_promotion_report(table, metadata),
        encoding="utf-8",
    )
    print(
        "promotion report "
        f"rows={metadata['rows']} promotable={metadata['promotable_rows']} "
        f"path={output_dir / 'daily_close_fade_promotion_report.md'}"
    )
    return 0 if metadata["promotable_rows"] else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join close-fade raw split diagnostics and split-grid exits into one promotion gate."
    )
    parser.add_argument(
        "--diagnostic-summary",
        required=True,
        help="Path to daily_close_fade_split_diagnostics_summary.csv.",
    )
    parser.add_argument(
        "--grid-summary",
        required=True,
        help="Path to daily_close_fade_grid_split_summary.csv.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--allow-raw-cost-fail",
        action="store_true",
        help="Do not require the raw diagnostic scenario to pass costs in all splits.",
    )
    parser.add_argument(
        "--min-raw-cost-pass-splits",
        type=int,
        default=0,
        help="Minimum raw diagnostic cost-pass split count. 0 means all observed splits when raw cost pass is required.",
    )
    parser.add_argument(
        "--min-grid-positive-splits",
        type=int,
        default=0,
        help="Minimum exit-grid positive split count. 0 means all observed splits.",
    )
    parser.add_argument(
        "--min-grid-min-return",
        type=float,
        default=0.0,
        help="Minimum worst split total return for the exit-grid variant.",
    )
    parser.add_argument(
        "--min-grid-stability",
        type=float,
        default=0.0,
        help="Minimum split-grid stability score.",
    )
    return parser.parse_args()


def build_promotion_table(
    diagnostic_summary: pl.DataFrame,
    grid_summary: pl.DataFrame,
    *,
    require_raw_cost_pass: bool = True,
    min_raw_cost_pass_splits: int = 0,
    min_grid_positive_splits: int = 0,
    min_grid_min_return: float = 0.0,
    min_grid_stability: float = 0.0,
) -> pl.DataFrame:
    if diagnostic_summary.is_empty() or grid_summary.is_empty():
        return _empty_promotion_table()
    _require_columns(diagnostic_summary, JOIN_COLS, label="diagnostic summary")
    _require_columns(grid_summary, ["score", "signal_minute", "entry_delay_minutes", "hold_minutes", "top_n"], label="grid summary")

    grid = grid_summary.with_columns(pl.col("hold_minutes").alias("horizon_minutes"))
    joined = grid.join(diagnostic_summary, on=JOIN_COLS, how="left", suffix="_raw")
    rows = []
    for row in joined.to_dicts():
        raw_splits_seen = int(_number(row.get("splits_seen_raw"), 0))
        raw_cost_pass_splits = int(_number(row.get("cost_pass_splits"), 0))
        grid_splits_seen = int(_number(row.get("splits_seen"), 0))
        grid_positive_splits = int(_number(row.get("positive_return_splits"), 0))
        raw_required_splits = min_raw_cost_pass_splits or raw_splits_seen
        grid_required_splits = min_grid_positive_splits or grid_splits_seen
        raw_gate_pass = (
            raw_splits_seen > 0
            and (
                raw_cost_pass_splits >= raw_required_splits
                if require_raw_cost_pass
                else raw_cost_pass_splits >= min_raw_cost_pass_splits
            )
        )
        exit_gate_pass = (
            grid_splits_seen > 0
            and grid_positive_splits >= grid_required_splits
            and _number(row.get("min_total_return"), 0.0) >= min_grid_min_return
            and _number(row.get("stability_score"), 0.0) >= min_grid_stability
        )
        reason = _promotion_reason(
            raw_gate_pass=raw_gate_pass,
            exit_gate_pass=exit_gate_pass,
            raw_splits_seen=raw_splits_seen,
            raw_cost_pass_splits=raw_cost_pass_splits,
            raw_required_splits=raw_required_splits,
            grid_splits_seen=grid_splits_seen,
            grid_positive_splits=grid_positive_splits,
            grid_required_splits=grid_required_splits,
        )
        rows.append(
            {
                **row,
                "raw_splits_seen": raw_splits_seen,
                "raw_cost_pass_splits": raw_cost_pass_splits,
                "raw_gate_pass": raw_gate_pass,
                "exit_gate_pass": exit_gate_pass,
                "promotion_gate_pass": raw_gate_pass and exit_gate_pass,
                "promotion_reason": reason,
            }
        )
    if not rows:
        return _empty_promotion_table()
    return pl.DataFrame(rows, infer_schema_length=None).sort(
        [
            "promotion_gate_pass",
            "raw_gate_pass",
            "exit_gate_pass",
            "min_total_return",
            "stability_score",
            "avg_total_return",
        ],
        descending=[True, True, True, True, True, True],
    )


def format_promotion_report(table: pl.DataFrame, metadata: dict[str, Any]) -> str:
    lines = [
        "# Daily Close Fade Promotion Gate",
        "",
        "This joins raw score split diagnostics with split-grid exit results.",
        "A variant should not be treated as promoted unless the raw entry edge",
        "and the exit behavior both pass their gates.",
        "",
        "## Inputs",
        "",
        f"- Diagnostic summary: `{metadata['diagnostic_summary']}`",
        f"- Grid summary: `{metadata['grid_summary']}`",
        f"- Require raw all-split cost pass: {metadata['require_raw_cost_pass']}",
        f"- Minimum raw cost-pass splits: {metadata['min_raw_cost_pass_splits'] or 'all observed'}",
        f"- Minimum grid positive splits: {metadata['min_grid_positive_splits'] or 'all observed'}",
        f"- Minimum grid worst-split return: {_pct(metadata['min_grid_min_return'])}",
        f"- Minimum grid stability score: {_pct(metadata['min_grid_stability'])}",
        "",
        "## Result",
        "",
        f"- Joined rows: {metadata['rows']}",
        f"- Promotable rows: {metadata['promotable_rows']}",
        "",
        "## Top Rows",
        "",
        "| Rank | Promote | Raw OK | Exit OK | Reason | Raw Cost Splits | Grid Positive Splits | Grid Min Return | Grid Avg Return | Stability | Signal | Delay | Horizon | Top N | Score | Stop | TP | Vol Trail | MFE GB | Cost |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for index, row in enumerate(table.head(50).to_dicts() if not table.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('promotion_gate_pass', False)} | "
            f"{row.get('raw_gate_pass', False)} | {row.get('exit_gate_pass', False)} | "
            f"{row.get('promotion_reason', '')} | "
            f"{row.get('raw_cost_pass_splits', 0)}/{row.get('raw_splits_seen', 0)} | "
            f"{row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('stability_score'))} | {_format_signal_minute(row.get('signal_minute', 0))} | "
            f"{row.get('entry_delay_minutes', 0)} | {row.get('horizon_minutes', 0)} | "
            f"{row.get('top_n', 0)} | {row.get('score', '')} | {_pct(row.get('stop_loss_pct'))} | "
            f"{_pct(row.get('take_profit_pct'))} | {_num(row.get('vol_trailing_stop_mult'), 2)}x | "
            f"{_pct(row.get('mfe_giveback_pct'))} | {_num(row.get('cost_multiplier'), 1)}x |"
        )
    if table.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _promotion_reason(
    *,
    raw_gate_pass: bool,
    exit_gate_pass: bool,
    raw_splits_seen: int,
    raw_cost_pass_splits: int,
    raw_required_splits: int,
    grid_splits_seen: int,
    grid_positive_splits: int,
    grid_required_splits: int,
) -> str:
    if raw_gate_pass and exit_gate_pass:
        return "entry_and_exit_pass"
    reasons = []
    if raw_splits_seen <= 0:
        reasons.append("no_matching_raw_diagnostic")
    elif raw_cost_pass_splits < raw_required_splits:
        reasons.append("raw_cost_split_fail")
    if grid_splits_seen <= 0:
        reasons.append("no_grid_split")
    elif grid_positive_splits < grid_required_splits:
        reasons.append("grid_positive_split_fail")
    return ",".join(reasons) if reasons else "threshold_fail"


def _read_csv(path: Path) -> pl.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.stat().st_size <= 0:
        return pl.DataFrame()
    return pl.read_csv(path)


def _require_columns(frame: pl.DataFrame, columns: list[str], *, label: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{label} missing columns: {', '.join(missing)}")


def _empty_promotion_table() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "promotion_gate_pass": pl.Boolean,
            "raw_gate_pass": pl.Boolean,
            "exit_gate_pass": pl.Boolean,
            "promotion_reason": pl.String,
        }
    )


def _number(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
