from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import polars as pl


def main() -> int:
    args = parse_args()
    summary_path = Path(args.split_summary)
    output_dir = Path(args.output_dir) if args.output_dir else summary_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = _read_csv(summary_path)
    table = build_volume_promotion_table(
        summary,
        require_complete_splits=not args.allow_incomplete_splits,
        min_positive_splits=args.min_positive_splits,
        min_worst_split_return=args.min_worst_split_return,
        min_stability_score=args.min_stability_score,
        max_worst_drawdown=args.max_worst_drawdown,
        min_avg_sharpe=args.min_avg_sharpe,
    )
    metadata = {
        "split_summary": str(summary_path),
        "require_complete_splits": not args.allow_incomplete_splits,
        "min_positive_splits": args.min_positive_splits,
        "min_worst_split_return": args.min_worst_split_return,
        "min_stability_score": args.min_stability_score,
        "max_worst_drawdown": args.max_worst_drawdown,
        "min_avg_sharpe": args.min_avg_sharpe,
        "rows": table.height,
        "promotable_rows": table.filter(pl.col("promotion_gate_pass")).height if not table.is_empty() else 0,
    }
    if not table.is_empty():
        table.write_csv(output_dir / "volume_promotion_candidates.csv")
    (output_dir / "volume_promotion_report.json").write_text(
        json.dumps(
            {
                **metadata,
                "top_rows": table.head(50).to_dicts() if not table.is_empty() else [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "volume_promotion_report.md").write_text(
        format_volume_promotion_report(table, metadata),
        encoding="utf-8",
    )
    print(
        "volume promotion report "
        f"rows={metadata['rows']} promotable={metadata['promotable_rows']} "
        f"path={output_dir / 'volume_promotion_report.md'}"
    )
    return 0 if metadata["promotable_rows"] else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply promotion gates to a volume-alpha split-grid summary.")
    parser.add_argument(
        "--split-summary",
        required=True,
        help="Path to volume_grid_split_summary.csv.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--allow-incomplete-splits",
        action="store_true",
        help="Allow candidates that were not seen in every split.",
    )
    parser.add_argument(
        "--min-positive-splits",
        type=int,
        default=0,
        help="Minimum positive-return split count. 0 means all observed splits.",
    )
    parser.add_argument(
        "--min-worst-split-return",
        type=float,
        default=0.0,
        help="Minimum worst split total return.",
    )
    parser.add_argument(
        "--min-stability-score",
        type=float,
        default=0.0,
        help="Minimum split stability score.",
    )
    parser.add_argument(
        "--max-worst-drawdown",
        type=float,
        default=-1.0,
        help="Minimum acceptable worst max drawdown, e.g. -0.35 rejects worse than -35%%.",
    )
    parser.add_argument(
        "--min-avg-sharpe",
        type=float,
        default=0.0,
        help="Minimum average split Sharpe-like value.",
    )
    return parser.parse_args()


def build_volume_promotion_table(
    summary: pl.DataFrame,
    *,
    require_complete_splits: bool = True,
    min_positive_splits: int = 0,
    min_worst_split_return: float = 0.0,
    min_stability_score: float = 0.0,
    max_worst_drawdown: float = -1.0,
    min_avg_sharpe: float = 0.0,
) -> pl.DataFrame:
    if summary.is_empty():
        return _empty_promotion_table()
    _require_columns(
        summary,
        [
            "splits_seen",
            "positive_return_splits",
            "min_total_return",
            "stability_score",
            "worst_max_drawdown",
            "avg_sharpe_like",
        ],
        label="volume split summary",
    )
    rows = []
    for row in summary.to_dicts():
        splits_seen = int(_number(row.get("splits_seen"), 0))
        positive_splits = int(_number(row.get("positive_return_splits"), 0))
        required_positive_splits = min_positive_splits or splits_seen
        complete_gate_pass = bool(row.get("complete_splits", True)) or not require_complete_splits
        positive_gate_pass = splits_seen > 0 and positive_splits >= required_positive_splits
        return_gate_pass = _number(row.get("min_total_return"), 0.0) >= min_worst_split_return
        stability_gate_pass = _number(row.get("stability_score"), 0.0) >= min_stability_score
        drawdown_gate_pass = _number(row.get("worst_max_drawdown"), -1.0) >= max_worst_drawdown
        sharpe_gate_pass = _number(row.get("avg_sharpe_like"), 0.0) >= min_avg_sharpe
        gate_pass = (
            complete_gate_pass
            and positive_gate_pass
            and return_gate_pass
            and stability_gate_pass
            and drawdown_gate_pass
            and sharpe_gate_pass
        )
        rows.append(
            {
                **row,
                "promotion_gate_pass": gate_pass,
                "promotion_reason": _promotion_reason(
                    complete_gate_pass=complete_gate_pass,
                    positive_gate_pass=positive_gate_pass,
                    return_gate_pass=return_gate_pass,
                    stability_gate_pass=stability_gate_pass,
                    drawdown_gate_pass=drawdown_gate_pass,
                    sharpe_gate_pass=sharpe_gate_pass,
                ),
            }
        )
    if not rows:
        return _empty_promotion_table()
    return pl.DataFrame(rows, infer_schema_length=None).sort(
        [
            "promotion_gate_pass",
            "min_total_return",
            "stability_score",
            "avg_sharpe_like",
            "worst_max_drawdown",
        ],
        descending=[True, True, True, True, True],
    )


def format_volume_promotion_report(table: pl.DataFrame, metadata: dict[str, Any]) -> str:
    lines = [
        "# Volume Alpha Promotion Gate",
        "",
        "This applies explicit promotion thresholds to a volume split-grid summary.",
        "It is a guardrail against promoting the single best historical grid row.",
        "",
        "## Inputs",
        "",
        f"- Split summary: `{metadata['split_summary']}`",
        f"- Require complete splits: {metadata['require_complete_splits']}",
        f"- Minimum positive splits: {metadata['min_positive_splits'] or 'all observed'}",
        f"- Minimum worst-split return: {_pct(metadata['min_worst_split_return'])}",
        f"- Minimum stability score: {_pct(metadata['min_stability_score'])}",
        f"- Maximum accepted worst drawdown: {_pct(metadata['max_worst_drawdown'])}",
        f"- Minimum average Sharpe-like: {_num(metadata['min_avg_sharpe'], 2)}",
        "",
        "## Result",
        "",
        f"- Rows: {metadata['rows']}",
        f"- Promotable rows: {metadata['promotable_rows']}",
        "",
        "## Top Rows",
        "",
        "| Rank | Promote | Reason | Pos Splits | Min Return | Avg Return | Stability | Worst DD | Avg Sharpe | Ranks | Hold | Quantile | Stop | Rank Exit | Side | Cost | Trades |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|---:|",
    ]
    for index, row in enumerate(table.head(50).to_dicts() if not table.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('promotion_gate_pass', False)} | {row.get('promotion_reason', '')} | "
            f"{row.get('positive_return_splits', 0)}/{row.get('splits_seen', 0)} | "
            f"{_pct(row.get('min_total_return'))} | {_pct(row.get('avg_total_return'))} | "
            f"{_pct(row.get('stability_score'))} | {_pct(row.get('worst_max_drawdown'))} | "
            f"{_num(row.get('avg_sharpe_like'), 2)} | "
            f"{row.get('universe_rank_min', 1)}-{row.get('universe_rank_max', 0) or 'all'} | "
            f"{row.get('hold_days', 0)}d | {_pct(row.get('quantile'))} | {_stop_label(row)} | "
            f"{row.get('rank_exit_enabled', False)} | {row.get('side_mode', '')} | "
            f"{_num(row.get('cost_multiplier'), 1)}x | {row.get('trade_count', 0)} |"
        )
    if table.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _promotion_reason(
    *,
    complete_gate_pass: bool,
    positive_gate_pass: bool,
    return_gate_pass: bool,
    stability_gate_pass: bool,
    drawdown_gate_pass: bool,
    sharpe_gate_pass: bool,
) -> str:
    failures = []
    if not complete_gate_pass:
        failures.append("incomplete_splits")
    if not positive_gate_pass:
        failures.append("positive_split_fail")
    if not return_gate_pass:
        failures.append("worst_split_return_fail")
    if not stability_gate_pass:
        failures.append("stability_fail")
    if not drawdown_gate_pass:
        failures.append("drawdown_fail")
    if not sharpe_gate_pass:
        failures.append("avg_sharpe_fail")
    return "pass" if not failures else ",".join(failures)


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
