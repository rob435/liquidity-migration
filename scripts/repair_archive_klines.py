from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.archive_manifest import (
    _download_one_archive_kline,
    _kline_partition_valid_bar_rows,
)
from aggression_carry.storage import dataset_path, read_dataset


RESULT_COLUMNS = (
    "symbol",
    "date",
    "url",
    "status",
    "bar_rows",
    "valid_bar_rows",
    "archive_path",
    "archive_deleted",
    "archive_cleanup_error",
    "error",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair missing Bybit public-archive 1m kline partitions.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--start", default=None, help="Inclusive archive date YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Inclusive archive date YYYY-MM-DD.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-rows", type=int, default=0, help="Maximum missing rows to attempt; 0 disables.")
    parser.add_argument("--min-existing-bars", type=int, default=1)
    parser.add_argument("--discard-archives-after-success", action="store_true")
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--name", default="archive-kline-repair")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root).expanduser()
    report_dir = Path(args.report_dir).expanduser() if args.report_dir else data_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(args.name)
    results_path = report_dir / f"{safe_name}_results.csv"
    summary_path = report_dir / f"{safe_name}_summary.json"

    selected_rows = select_missing_rows(
        data_root,
        start=args.start,
        end=args.end,
        symbols=_csv_symbols(args.symbols),
        min_existing_bars=max(int(args.min_existing_bars), 1),
        max_rows=max(int(args.max_rows), 0),
    )
    worker_count = max(1, min(int(args.workers), len(selected_rows))) if selected_rows else 1
    started = time.time()
    counts = {"downloaded": 0, "cached": 0, "empty": 0, "failed": 0}
    attempted = 0

    print(
        "repair selected "
        f"rows={len(selected_rows)} workers={worker_count} "
        f"min_existing_bars={max(int(args.min_existing_bars), 1)} "
        f"results={results_path}",
        flush=True,
    )

    with results_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        for date_rows in _rows_by_date(selected_rows):
            for result in _repair_date_rows(
                data_root,
                date_rows,
                workers=worker_count,
                min_existing_bars=max(int(args.min_existing_bars), 1),
                discard_archives_after_success=bool(args.discard_archives_after_success),
            ):
                attempted += 1
                status = str(result.get("status", "failed"))
                if status not in counts:
                    counts[status] = 0
                counts[status] += 1
                writer.writerow({key: result.get(key, "") for key in RESULT_COLUMNS})
                if attempted % 100 == 0 or status == "failed":
                    elapsed = max(time.time() - started, 1.0)
                    print(
                        "repair progress "
                        f"attempted={attempted}/{len(selected_rows)} "
                        f"downloaded={counts.get('downloaded', 0)} "
                        f"cached={counts.get('cached', 0)} "
                        f"failed={counts.get('failed', 0)} "
                        f"rate_per_min={attempted / elapsed * 60:.1f}",
                        flush=True,
                    )
            handle.flush()

    summary = {
        "name": args.name,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "data_root": str(data_root),
        "rows_selected": len(selected_rows),
        "attempted": attempted,
        "workers": worker_count,
        "min_existing_bars": max(int(args.min_existing_bars), 1),
        "counts": counts,
        "results_path": str(results_path),
        "elapsed_seconds": round(time.time() - started, 3),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"repair complete {json.dumps(summary, sort_keys=True)}", flush=True)
    return 1 if counts.get("failed", 0) else 0


def select_missing_rows(
    data_root: Path,
    *,
    start: str | None,
    end: str | None,
    symbols: tuple[str, ...],
    min_existing_bars: int,
    max_rows: int,
) -> list[dict[str, Any]]:
    manifest = read_dataset(data_root, "archive_trade_manifest")
    if manifest.is_empty():
        raise RuntimeError("archive_trade_manifest is empty; run archive-manifest first")
    frame = manifest
    if start:
        frame = frame.filter(pl.col("date") >= start[:10])
    if end:
        frame = frame.filter(pl.col("date") <= end[:10])
    if symbols:
        frame = frame.filter(pl.col("symbol").is_in(symbols))
    frame = frame.sort(["date", "symbol"])

    if min_existing_bars <= 1:
        existing = _existing_non_empty_kline_keys(data_root)
        rows = [row for row in frame.to_dicts() if (str(row["date"]), str(row["symbol"])) not in existing]
    else:
        rows = [
            row
            for row in frame.to_dicts()
            if _kline_partition_valid_bar_rows(data_root, symbol=str(row["symbol"]), date=str(row["date"]))
            < min_existing_bars
        ]
    if max_rows > 0:
        rows = rows[:max_rows]
    return rows


def _repair_date_rows(
    data_root: Path,
    rows: list[dict[str, Any]],
    *,
    workers: int,
    min_existing_bars: int,
    discard_archives_after_success: bool,
) -> list[dict[str, Any]]:
    if workers <= 1 or len(rows) <= 1:
        return [
            _download_one_archive_kline(
                data_root,
                row,
                missing_only=True,
                min_existing_bars=min_existing_bars,
                discard_archives_after_success=discard_archives_after_success,
            )
            for row in rows
        ]
    results: list[dict[str, Any]] = []
    date_worker_count = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=date_worker_count) as executor:
        futures = [
            executor.submit(
                _download_one_archive_kline,
                data_root,
                row,
                missing_only=True,
                min_existing_bars=min_existing_bars,
                discard_archives_after_success=discard_archives_after_success,
            )
            for row in rows
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def _rows_by_date(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current_date: str | None = None
    for row in rows:
        row_date = str(row["date"])
        if row_date != current_date:
            groups.append([])
            current_date = row_date
        groups[-1].append(row)
    return groups


def _existing_non_empty_kline_keys(data_root: Path) -> set[tuple[str, str]]:
    root = dataset_path(data_root, "klines_1m")
    keys: set[tuple[str, str]] = set()
    if not root.exists():
        return keys
    for part in root.glob("date=*/symbol=*/part.parquet"):
        try:
            if part.stat().st_size <= 0:
                continue
            symbol_dir = part.parent.name
            date_dir = part.parent.parent.name
            if not symbol_dir.startswith("symbol=") or not date_dir.startswith("date="):
                continue
            keys.add((date_dir.split("=", 1)[1], symbol_dir.split("=", 1)[1]))
        except OSError:
            continue
    return keys


def _csv_symbols(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in name).strip("-") or "archive-kline-repair"


if __name__ == "__main__":
    raise SystemExit(main())
