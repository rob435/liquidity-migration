from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from pyarrow import parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.storage import dataset_path, read_dataset


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    output_dir = Path(args.report_dir) if args.report_dir else data_root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = _filtered_manifest(
        read_dataset(data_root, "archive_trade_manifest"),
        start=args.start,
        end=args.end,
        symbols=_csv_symbols(args.symbols),
        max_rows=args.max_rows,
    )
    if manifest.is_empty():
        raise RuntimeError("archive_trade_manifest is empty after filters")

    coverage = build_archive_pit_coverage(
        data_root,
        manifest,
        min_bars_per_day=args.min_bars_per_day,
        require_next_day=not args.no_require_next_day,
    )
    monthly = summarize_coverage_monthly(coverage)
    symbols = summarize_coverage_symbols(coverage)
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "data_root": str(data_root),
        "start": args.start,
        "end": args.end,
        "symbols_filter": list(_csv_symbols(args.symbols)),
        "min_bars_per_day": args.min_bars_per_day,
        "require_next_day": not args.no_require_next_day,
        "min_coverage_rate": args.min_coverage_rate,
        "min_usable_rate": args.min_usable_rate,
        "rows": coverage.height,
        "summary": _payload_summary(coverage),
    }

    coverage.write_csv(output_dir / "archive_pit_coverage_rows.csv")
    monthly.write_csv(output_dir / "archive_pit_coverage_monthly.csv")
    symbols.write_csv(output_dir / "archive_pit_coverage_symbols.csv")
    (output_dir / "archive_pit_coverage_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "archive_pit_coverage_report.md").write_text(
        format_archive_pit_coverage_report(payload, monthly, symbols),
        encoding="utf-8",
    )
    print(
        "archive pit coverage "
        f"rows={payload['rows']} usable={payload['summary']['usable_rows']} "
        f"usable_rate={payload['summary']['usable_rate']:.2%} "
        f"path={output_dir / 'archive_pit_coverage_report.md'}"
    )
    if not _coverage_thresholds_pass(payload["summary"], min_coverage_rate=args.min_coverage_rate, min_usable_rate=args.min_usable_rate):
        return 2
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit archive-manifest symbol/date coverage against klines_1m partitions.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--start", default=None, help="Inclusive manifest date filter YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Inclusive manifest date filter YYYY-MM-DD.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    parser.add_argument("--max-rows", type=int, default=0, help="Maximum manifest rows to audit; 0 disables.")
    parser.add_argument("--min-bars-per-day", type=int, default=1200, help="Minimum 1m bars for a partition to count covered.")
    parser.add_argument("--min-coverage-rate", type=float, default=0.0, help="Exit 2 if covered-row rate is below this fraction.")
    parser.add_argument("--min-usable-rate", type=float, default=0.0, help="Exit 2 if close-fade usable-row rate is below this fraction.")
    parser.add_argument(
        "--no-require-next-day",
        action="store_true",
        help="Do not require the next UTC date partition for daily-close exits that cross midnight.",
    )
    return parser.parse_args()


def build_archive_pit_coverage(
    data_root: str | Path,
    manifest: pl.DataFrame,
    *,
    min_bars_per_day: int = 1200,
    require_next_day: bool = True,
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in manifest.select(["symbol", "date", "url"]).sort(["date", "symbol"]).to_dicts():
        symbol = str(item["symbol"])
        archive_date = str(item["date"])
        bar_rows = _partition_bar_rows(data_root, symbol=symbol, date=archive_date)
        status = _coverage_status(bar_rows, min_bars_per_day=min_bars_per_day)
        next_date = _next_date(archive_date)
        next_bar_rows = _partition_bar_rows(data_root, symbol=symbol, date=next_date)
        next_status = _coverage_status(next_bar_rows, min_bars_per_day=min_bars_per_day)
        usable = status == "covered" and (not require_next_day or next_status == "covered")
        rows.append(
            {
                "symbol": symbol,
                "date": archive_date,
                "month": archive_date[:7],
                "url": str(item.get("url") or ""),
                "bar_rows": bar_rows,
                "status": status,
                "next_date": next_date,
                "next_bar_rows": next_bar_rows,
                "next_status": next_status,
                "usable_for_close_fade": usable,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def summarize_coverage_monthly(coverage: pl.DataFrame) -> pl.DataFrame:
    if coverage.is_empty():
        return pl.DataFrame()
    return (
        coverage.group_by("month", maintain_order=True)
        .agg(_coverage_aggs())
        .with_columns(_coverage_rates())
        .sort("month")
    )


def summarize_coverage_symbols(coverage: pl.DataFrame) -> pl.DataFrame:
    if coverage.is_empty():
        return pl.DataFrame()
    return (
        coverage.group_by("symbol", maintain_order=True)
        .agg(_coverage_aggs())
        .with_columns(_coverage_rates())
        .sort(["usable_rate", "coverage_rate", "manifest_rows"], descending=[False, False, True])
    )


def format_archive_pit_coverage_report(
    payload: dict[str, Any],
    monthly: pl.DataFrame,
    symbols: pl.DataFrame,
) -> str:
    summary = payload["summary"]
    lines = [
        "# Archive PIT Coverage",
        "",
        f"Created: {payload['created_at']}",
        f"Data root: `{payload['data_root']}`",
        f"Date filter: {payload.get('start') or 'all'} to {payload.get('end') or 'all'}",
        f"Min bars/day: {payload['min_bars_per_day']}",
        f"Require next-day partition: {payload['require_next_day']}",
        f"Minimum coverage gate: {payload.get('min_coverage_rate', 0.0):.2%}",
        f"Minimum usable gate: {payload.get('min_usable_rate', 0.0):.2%}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Manifest rows | {summary['manifest_rows']} |",
        f"| Covered rows | {summary['covered_rows']} |",
        f"| Sparse rows | {summary['sparse_rows']} |",
        f"| Missing rows | {summary['missing_rows']} |",
        f"| Usable close-fade rows | {summary['usable_rows']} |",
        f"| Coverage rate | {summary['coverage_rate']:.2%} |",
        f"| Usable rate | {summary['usable_rate']:.2%} |",
        "",
        "## Monthly Coverage",
        "",
        "| Month | Manifest | Covered | Sparse | Missing | Usable | Coverage | Usable | Symbols |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in monthly.to_dicts():
        lines.append(
            f"| {row['month']} | {row['manifest_rows']} | {row['covered_rows']} | "
            f"{row['sparse_rows']} | {row['missing_rows']} | {row['usable_rows']} | "
            f"{row['coverage_rate']:.2%} | {row['usable_rate']:.2%} | {row['symbols']} |"
        )

    lines.extend(
        [
            "",
            "## Weakest Symbols",
            "",
            "| Symbol | Manifest | Covered | Sparse | Missing | Usable | Coverage | Usable |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in symbols.head(30).to_dicts() if not symbols.is_empty() else []:
        lines.append(
            f"| {row['symbol']} | {row['manifest_rows']} | {row['covered_rows']} | "
            f"{row['sparse_rows']} | {row['missing_rows']} | {row['usable_rows']} | "
            f"{row['coverage_rate']:.2%} | {row['usable_rate']:.2%} |"
        )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "archive_pit_coverage_rows.csv",
            "archive_pit_coverage_monthly.csv",
            "archive_pit_coverage_symbols.csv",
            "archive_pit_coverage_report.json",
            "archive_pit_coverage_report.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _filtered_manifest(
    manifest: pl.DataFrame,
    *,
    start: str | None,
    end: str | None,
    symbols: tuple[str, ...],
    max_rows: int,
) -> pl.DataFrame:
    if manifest.is_empty():
        return manifest
    frame = manifest.select(["symbol", "date", "url"]).unique().sort(["date", "symbol"])
    if start:
        frame = frame.filter(pl.col("date") >= start[:10])
    if end:
        frame = frame.filter(pl.col("date") <= end[:10])
    if symbols:
        frame = frame.filter(pl.col("symbol").is_in(symbols))
    if max_rows > 0:
        frame = frame.head(max_rows)
    return frame


def _coverage_aggs() -> list[pl.Expr]:
    return [
        pl.len().alias("manifest_rows"),
        (pl.col("status") == "covered").sum().alias("covered_rows"),
        (pl.col("status") == "sparse").sum().alias("sparse_rows"),
        (pl.col("status") == "missing").sum().alias("missing_rows"),
        pl.col("usable_for_close_fade").sum().alias("usable_rows"),
        pl.col("symbol").n_unique().alias("symbols"),
    ]


def _coverage_rates() -> list[pl.Expr]:
    return [
        (pl.col("covered_rows") / pl.col("manifest_rows")).alias("coverage_rate"),
        (pl.col("usable_rows") / pl.col("manifest_rows")).alias("usable_rate"),
    ]


def _payload_summary(coverage: pl.DataFrame) -> dict[str, Any]:
    if coverage.is_empty():
        return {
            "manifest_rows": 0,
            "covered_rows": 0,
            "sparse_rows": 0,
            "missing_rows": 0,
            "usable_rows": 0,
            "coverage_rate": 0.0,
            "usable_rate": 0.0,
        }
    row = (
        coverage.select(_coverage_aggs())
        .with_columns(_coverage_rates())
        .row(0, named=True)
    )
    return {key: int(value) if isinstance(value, int) else float(value) for key, value in row.items()}


def _coverage_thresholds_pass(
    summary: dict[str, Any],
    *,
    min_coverage_rate: float,
    min_usable_rate: float,
) -> bool:
    return (
        float(summary.get("coverage_rate", 0.0)) + 1e-12 >= min_coverage_rate
        and float(summary.get("usable_rate", 0.0)) + 1e-12 >= min_usable_rate
    )


def _partition_bar_rows(data_root: str | Path, *, symbol: str, date: str) -> int:
    part = dataset_path(data_root, "klines_1m") / f"date={date}" / f"symbol={symbol}" / "part.parquet"
    if not part.exists() or part.stat().st_size <= 0:
        return 0
    try:
        return int(pq.ParquetFile(part).metadata.num_rows)
    except Exception:  # noqa: BLE001 - coverage audit should report sparse instead of crashing on one bad file
        return 0


def _coverage_status(bar_rows: int, *, min_bars_per_day: int) -> str:
    if bar_rows <= 0:
        return "missing"
    if bar_rows < min_bars_per_day:
        return "sparse"
    return "covered"


def _next_date(value: str) -> str:
    return (date.fromisoformat(value[:10]) + timedelta(days=1)).isoformat()


def _csv_symbols(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


if __name__ == "__main__":
    raise SystemExit(main())
