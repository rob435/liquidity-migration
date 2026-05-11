from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aggression_carry.archive_manifest import ArchiveKlineDownloadConfig, run_archive_klines_download
from aggression_carry.storage import read_dataset
from report_archive_pit_coverage import (
    _filtered_manifest,
    _payload_summary,
    build_archive_pit_coverage,
    format_archive_pit_coverage_report,
    summarize_coverage_monthly,
    summarize_coverage_symbols,
)


DownloadFunc = Callable[[str | Path], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ArchivePitBatchConfig:
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    batch_rows: int = 1000
    max_batches: int = 0
    workers: int = 16
    name: str = "pit_batches"
    coverage_every: int = 1
    min_bars_per_day: int = 1200
    require_next_day: bool = True
    include_flow: bool = False
    keep_archives: bool = False
    skip_failed_rows: bool = False


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    report_dir = Path(args.report_dir) if args.report_dir else data_root / "reports" / "archive_pit_batches"
    config = ArchivePitBatchConfig(
        start=args.start,
        end=args.end,
        symbols=_symbol_filters(args.symbols, args.symbols_file),
        batch_rows=args.batch_rows,
        max_batches=args.max_batches,
        workers=args.workers,
        name=args.name,
        coverage_every=args.coverage_every,
        min_bars_per_day=args.min_bars_per_day,
        require_next_day=not args.no_require_next_day,
        include_flow=args.include_flow,
        keep_archives=args.keep_archives,
        skip_failed_rows=args.skip_failed_rows,
    )
    payload = run_archive_pit_batches(data_root, config=config, report_dir=report_dir)
    print(
        "archive PIT batches "
        f"batches={payload['batches']} selected={payload['selected_rows']} "
        f"downloaded={payload['downloaded']} failed={payload['failures']} "
        f"path={report_dir / 'archive_pit_batch_summary.md'}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run resumable archive PIT kline downloads in bounded batches.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None, help="Accepted for command consistency; archive batch download does not read it.")
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--name", default="pit_batches")
    parser.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Inclusive archive end date YYYY-MM-DD.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    parser.add_argument("--symbols-file", default="", help="Optional file containing comma- or newline-separated symbol allowlist.")
    parser.add_argument("--batch-rows", type=int, default=1000, help="Maximum missing manifest rows to attempt per batch.")
    parser.add_argument("--max-batches", type=int, default=0, help="Maximum batches to run; 0 means until exhausted or stopped.")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--coverage-every", type=int, default=1, help="Run PIT coverage audit every N batches; 0 disables.")
    parser.add_argument("--min-bars-per-day", type=int, default=1200)
    parser.add_argument("--no-require-next-day", action="store_true")
    parser.add_argument("--include-flow", action="store_true", help="Also build signed_flow_1m and signed_flow_1h from each archive file.")
    parser.add_argument("--keep-archives", action="store_true", help="Keep compressed public trade archive files after derived parquet outputs are written.")
    parser.add_argument("--skip-failed-rows", action="store_true", help="Do not retry symbol/date rows already reported failed in this report directory.")
    return parser.parse_args()


def run_archive_pit_batches(
    data_root: str | Path,
    *,
    config: ArchivePitBatchConfig,
    report_dir: str | Path,
    download_func: Callable[..., dict[str, Any]] = run_archive_klines_download,
) -> dict[str, Any]:
    if config.batch_rows <= 0:
        raise ValueError("batch_rows must be positive")
    if config.coverage_every < 0:
        raise ValueError("coverage_every must be non-negative")

    output_dir = Path(report_dir)
    batch_report_dir = output_dir / "batches"
    batch_report_dir.mkdir(parents=True, exist_ok=True)

    batch_rows: list[dict[str, Any]] = []
    failed_keys = _load_failed_keys(batch_report_dir) if config.skip_failed_rows else set()
    initial_failed_keys = len(failed_keys)
    batch_index = 0
    stop_reason = "max_batches"
    while config.max_batches <= 0 or batch_index < config.max_batches:
        batch_index += 1
        batch_name = f"{config.name}_batch_{batch_index:04d}"
        payload = download_func(
            data_root,
            config=ArchiveKlineDownloadConfig(
                start=config.start,
                end=config.end,
                symbols=config.symbols,
                max_rows=config.batch_rows,
                workers=config.workers,
                missing_only=True,
                include_flow=config.include_flow,
                keep_archives=config.keep_archives,
                name=batch_name,
                exclude_keys=tuple(sorted(failed_keys)) if config.skip_failed_rows else (),
            ),
            report_dir=batch_report_dir,
        )
        row = _batch_row(batch_index, payload)
        batch_rows.append(row)
        if config.skip_failed_rows:
            failed_keys.update(_batch_failed_keys(batch_report_dir, batch_name))
        print(
            f"batch={batch_index} rows={row['selected_rows']} downloaded={row['downloaded']} "
            f"cached={row['cached']} empty={row['empty']} failed={row['failures']}"
        )

        if config.coverage_every and batch_index % config.coverage_every == 0:
            _write_batch_coverage(data_root, output_dir, config=config)

        if row["selected_rows"] == 0:
            stop_reason = "complete"
            break
        if row["progress_rows"] == 0 and row["failures"] > 0:
            stop_reason = "no_progress_failure_batch"
            break

    if batch_rows and (not config.coverage_every or batch_index % config.coverage_every != 0):
        _write_batch_coverage(data_root, output_dir, config=config)

    summary = _summarize_batches(
        batch_rows,
        config=config,
        stop_reason=stop_reason,
        initial_failed_keys=initial_failed_keys,
        final_failed_keys=len(failed_keys),
    )
    _write_batch_summary(output_dir, summary, batch_rows)
    return summary


def _batch_row(batch_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    downloaded = int(payload.get("downloaded", 0))
    cached = int(payload.get("cached", 0))
    empty = int(payload.get("empty", 0))
    failures = int(payload.get("failures", 0))
    return {
        "batch": batch_index,
        "selected_rows": int(payload.get("rows", 0)),
        "workers": int(payload.get("workers", 0)),
        "downloaded": downloaded,
        "cached": cached,
        "empty": empty,
        "failures": failures,
        "bar_rows": int(payload.get("bar_rows", 0) or 0),
        "flow_1m_rows": int(payload.get("flow_1m_rows", 0) or 0),
        "flow_1h_rows": int(payload.get("flow_1h_rows", 0) or 0),
        "progress_rows": downloaded + cached + empty,
        "created_at": payload.get("created_at", ""),
    }


def _summarize_batches(
    batch_rows: list[dict[str, Any]],
    *,
    config: ArchivePitBatchConfig,
    stop_reason: str,
    initial_failed_keys: int = 0,
    final_failed_keys: int = 0,
) -> dict[str, Any]:
    return {
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config": asdict(config),
        "batches": len(batch_rows),
        "selected_rows": sum(row["selected_rows"] for row in batch_rows),
        "downloaded": sum(row["downloaded"] for row in batch_rows),
        "cached": sum(row["cached"] for row in batch_rows),
        "empty": sum(row["empty"] for row in batch_rows),
        "failures": sum(row["failures"] for row in batch_rows),
        "bar_rows": sum(row["bar_rows"] for row in batch_rows),
        "flow_1m_rows": sum(row["flow_1m_rows"] for row in batch_rows),
        "flow_1h_rows": sum(row["flow_1h_rows"] for row in batch_rows),
        "progress_rows": sum(row["progress_rows"] for row in batch_rows),
        "stop_reason": stop_reason,
        "initial_failed_keys": initial_failed_keys,
        "final_failed_keys": final_failed_keys,
    }


def _write_batch_summary(output_dir: Path, summary: dict[str, Any], batch_rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_frame = pl.DataFrame(batch_rows, infer_schema_length=None) if batch_rows else _empty_batch_frame()
    batch_frame.write_csv(output_dir / "archive_pit_batch_rows.csv")
    (output_dir / "archive_pit_batch_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / "archive_pit_batch_summary.md").write_text(format_batch_summary(summary, batch_rows), encoding="utf-8")


def format_batch_summary(summary: dict[str, Any], batch_rows: list[dict[str, Any]]) -> str:
    config = summary.get("config", {})
    lines = [
        "# Archive PIT Batch Download",
        "",
        f"Created: {summary.get('created_at')}",
        f"Stop reason: `{summary.get('stop_reason')}`",
        f"Window: {config.get('start') or 'all'} to {config.get('end') or 'all'}",
        f"Batch rows: {config.get('batch_rows')}",
        f"Workers: {config.get('workers')}",
        f"Include flow: {config.get('include_flow')}",
        f"Keep archives: {config.get('keep_archives')}",
        f"Skip failed rows: {config.get('skip_failed_rows')}",
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Batches | {summary.get('batches', 0)} |",
        f"| Selected rows | {summary.get('selected_rows', 0)} |",
        f"| Downloaded | {summary.get('downloaded', 0)} |",
        f"| Cached | {summary.get('cached', 0)} |",
        f"| Empty | {summary.get('empty', 0)} |",
        f"| Failed | {summary.get('failures', 0)} |",
        f"| Kline rows | {summary.get('bar_rows', 0)} |",
        f"| Flow 1m rows | {summary.get('flow_1m_rows', 0)} |",
        f"| Flow 1h rows | {summary.get('flow_1h_rows', 0)} |",
        f"| Initial failed keys skipped | {summary.get('initial_failed_keys', 0)} |",
        f"| Final failed keys skipped | {summary.get('final_failed_keys', 0)} |",
        "",
        "## Batches",
        "",
        "| Batch | Selected | Downloaded | Cached | Empty | Failed | Kline Rows | Flow 1h Rows | Progress |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in batch_rows:
        lines.append(
            f"| {row['batch']} | {row['selected_rows']} | {row['downloaded']} | {row['cached']} | "
            f"{row['empty']} | {row['failures']} | {row['bar_rows']} | {row['flow_1h_rows']} | {row['progress_rows']} |"
        )
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "archive_pit_batch_rows.csv",
            "archive_pit_batch_summary.json",
            "archive_pit_batch_summary.md",
            "archive_pit_batch_coverage_report.md",
            "archive_pit_batch_coverage_rows.csv",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _write_batch_coverage(data_root: str | Path, output_dir: Path, *, config: ArchivePitBatchConfig) -> None:
    manifest = _filtered_manifest(
        read_dataset(data_root, "archive_trade_manifest"),
        start=config.start,
        end=config.end,
        symbols=config.symbols,
        max_rows=0,
    )
    if manifest.is_empty():
        return
    coverage = build_archive_pit_coverage(
        data_root,
        manifest,
        min_bars_per_day=config.min_bars_per_day,
        require_next_day=config.require_next_day,
        require_flow=config.include_flow,
    )
    monthly = summarize_coverage_monthly(coverage)
    symbols = summarize_coverage_symbols(coverage)
    payload = {
        "created_at": datetime.now(tz=UTC).isoformat(),
        "data_root": str(data_root),
        "start": config.start,
        "end": config.end,
        "symbols_filter": list(config.symbols),
        "min_bars_per_day": config.min_bars_per_day,
        "require_flow": config.include_flow,
        "min_flow_hours_per_day": 20,
        "require_next_day": config.require_next_day,
        "rows": coverage.height,
        "summary": _payload_summary(coverage),
    }
    coverage.write_csv(output_dir / "archive_pit_batch_coverage_rows.csv")
    monthly.write_csv(output_dir / "archive_pit_batch_coverage_monthly.csv")
    symbols.write_csv(output_dir / "archive_pit_batch_coverage_symbols.csv")
    (output_dir / "archive_pit_batch_coverage_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "archive_pit_batch_coverage_report.md").write_text(
        format_archive_pit_coverage_report(payload, monthly, symbols),
        encoding="utf-8",
    )


def _empty_batch_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "batch": pl.Series([], dtype=pl.Int64),
            "selected_rows": pl.Series([], dtype=pl.Int64),
            "workers": pl.Series([], dtype=pl.Int64),
            "downloaded": pl.Series([], dtype=pl.Int64),
            "cached": pl.Series([], dtype=pl.Int64),
            "empty": pl.Series([], dtype=pl.Int64),
            "failures": pl.Series([], dtype=pl.Int64),
            "progress_rows": pl.Series([], dtype=pl.Int64),
            "created_at": pl.Series([], dtype=pl.String),
        }
    )


def _load_failed_keys(batch_report_dir: Path) -> set[str]:
    failed: set[str] = set()
    for path in sorted(batch_report_dir.glob("archive_klines_*.csv")):
        failed.update(_failed_keys_from_csv(path))
    return failed


def _batch_failed_keys(batch_report_dir: Path, batch_name: str) -> set[str]:
    path = batch_report_dir / f"archive_klines_{_safe_name(batch_name)}.csv"
    return _failed_keys_from_csv(path)


def _failed_keys_from_csv(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size <= 0:
        return set()
    try:
        frame = pl.read_csv(path)
    except Exception:  # noqa: BLE001 - failure ledger is a resume aid only
        return set()
    if frame.is_empty() or not {"symbol", "date", "status"}.issubset(frame.columns):
        return set()
    return {
        f"{row['symbol']}|{row['date']}"
        for row in frame.filter(pl.col("status") == "failed").select(["symbol", "date"]).to_dicts()
    }


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return cleaned.strip("_") or "pit_batches"


def _csv_symbols(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


def _symbol_filters(symbols: str, symbols_file: str) -> tuple[str, ...]:
    values = list(_csv_symbols(symbols))
    if symbols_file:
        path = Path(symbols_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"symbols file not found: {path}")
        text = path.read_text(encoding="utf-8")
        values.extend(item.strip().upper() for chunk in text.splitlines() for item in chunk.split(",") if item.strip())
    return tuple(dict.fromkeys(values))


if __name__ == "__main__":
    raise SystemExit(main())
