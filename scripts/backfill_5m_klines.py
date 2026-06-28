#!/usr/bin/env python3
"""Backfill manifest-gated 5m kline partitions for the full-PIT roots.

Default window is the current continuous-fade validation sample:
2023-04-01 through each root's current PIT manifest tail. The script is
resume-safe because it checks existing ``klines_5m/date=*/symbol=*`` partitions
before downloading and only fetches missing or short symbol-days.

Examples:

    .venv/Scripts/python.exe scripts/backfill_5m_klines.py --venue bybit --workers 6
    .venv/Scripts/python.exe scripts/backfill_5m_klines.py --venue binance --workers 8
    .venv/Scripts/python.exe scripts/backfill_5m_klines.py --venue both --audit-only
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402
from pyarrow import parquet as pq  # noqa: E402

from liquidity_migration.archive_manifest import (  # noqa: E402
    DEFAULT_BYBIT_V5_KLINE_URL,
    ArchiveHourlyKlineApiDownloadConfig,
    _fetch_bybit_api_klines,
    _parse_bybit_api_kline_row,
)
from liquidity_migration.binance_vision import (  # noqa: E402
    _fetch_expected_sha256,
    _verify_download,
)
from liquidity_migration.storage import write_dataset  # noqa: E402

INTERVAL_MINUTES = 5
MS_PER_5M = INTERVAL_MINUTES * 60_000
EXPECTED_5M_BARS_PER_DAY = 24 * 60 // INTERVAL_MINUTES
DEFAULT_START = "2023-04-01"
VISION_FILES = "https://data.binance.vision"


@dataclass(frozen=True, slots=True)
class MissingWork:
    symbol: str
    days: tuple[str, ...]


def _parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def _date_range(start: str, end: str) -> list[str]:
    cursor = _parse_day(start)
    stop = _parse_day(end)
    out: list[str] = []
    while cursor < stop:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _root_for_venue(venue: str) -> Path:
    return Path.home() / "SHARED_DATA" / f"{venue}_full_pit"


def _manifest_end_exclusive(root: Path) -> str:
    manifest = root / "archive_trade_manifest"
    dates: list[date] = []
    if manifest.exists():
        for part in manifest.iterdir():
            if part.is_dir() and part.name.startswith("date="):
                try:
                    dates.append(date.fromisoformat(part.name.split("=", 1)[1]))
                except ValueError:
                    continue
    if not dates:
        raise RuntimeError(f"no archive_trade_manifest/date=* partitions under {root}")
    return (max(dates) + timedelta(days=1)).isoformat()


def _partition_file(root: Path, dataset: str, day: str, symbol: str) -> Path:
    return root / dataset / f"date={day}" / f"symbol={symbol}" / "part.parquet"


def _partition_rows(path: Path) -> int:
    if not path.exists() or path.stat().st_size <= 0:
        return 0
    try:
        return int(pq.ParquetFile(path).metadata.num_rows)
    except Exception:
        return 0


def _manifest_files(root: Path, start: str, end: str) -> list[str]:
    files: list[str] = []
    manifest = root / "archive_trade_manifest"
    for day in _date_range(start, end):
        part = manifest / f"date={day}"
        if part.exists():
            files.extend(str(path) for path in part.glob("*.parquet"))
    return files


def load_missing_work(
    root: Path,
    *,
    start: str,
    end: str,
    symbols: tuple[str, ...] = (),
    dataset: str = "klines_5m",
    min_existing_bars: int = EXPECTED_5M_BARS_PER_DAY,
) -> list[MissingWork]:
    """Return manifest symbol-days whose 5m partition is missing or short."""
    manifest_files = _manifest_files(root, start, end)
    if not manifest_files:
        raise RuntimeError(f"no manifest partitions in [{start}, {end}) under {root}")
    scan = pl.scan_parquet(manifest_files).select(["date", "symbol"])
    if symbols:
        scan = scan.filter(pl.col("symbol").is_in(list(symbols)))
    manifest = scan.unique(["date", "symbol"]).collect().sort(["symbol", "date"])
    missing_by_symbol: dict[str, list[str]] = defaultdict(list)
    for row in manifest.iter_rows(named=True):
        day = str(row["date"])
        symbol = str(row["symbol"])
        if _partition_rows(_partition_file(root, dataset, day, symbol)) < min_existing_bars:
            missing_by_symbol[symbol].append(day)
    return [
        MissingWork(symbol=symbol, days=tuple(days))
        for symbol, days in sorted(missing_by_symbol.items())
        if days
    ]


def _group_contiguous_days(days: Iterable[str]) -> list[tuple[str, str]]:
    parsed = sorted(_parse_day(day) for day in days)
    if not parsed:
        return []
    ranges: list[tuple[date, date]] = []
    start = prev = parsed[0]
    for day in parsed[1:]:
        if day == prev + timedelta(days=1):
            prev = day
            continue
        ranges.append((start, prev))
        start = prev = day
    ranges.append((start, prev))
    return [(a.isoformat(), b.isoformat()) for a, b in ranges]


def _day_start_ms(day: str) -> int:
    d = _parse_day(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def _day_end_5m_ms(day: str) -> int:
    return _day_start_ms(day) + (EXPECTED_5M_BARS_PER_DAY - 1) * MS_PER_5M


def _row_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()


def _write_rows(root: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    df = (
        pl.DataFrame(rows, infer_schema_length=None)
        .unique(["ts_ms", "symbol"], keep="last")
        .sort(["symbol", "ts_ms"])
    )
    write_dataset(df, root, "klines_5m")
    return int(df.height)


def backfill_bybit_symbol(
    root: Path,
    work: MissingWork,
    *,
    limit: int = 1000,
    request_sleep_seconds: float = 0.0,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    config = ArchiveHourlyKlineApiDownloadConfig(
        api_url=DEFAULT_BYBIT_V5_KLINE_URL,
        category="linear",
        interval="5",
        limit=limit,
        retries=5,
        request_sleep_seconds=request_sleep_seconds,
        timeout_seconds=timeout_seconds,
    )
    pending = set(work.days)
    rows_to_write: list[dict[str, Any]] = []
    failures = 0
    empty_days: set[str] = set(pending)
    chunk_bars = max(1, min(limit, 1000))
    for start_day, end_day in _group_contiguous_days(work.days):
        cursor = _day_start_ms(start_day)
        end_ms = _day_end_5m_ms(end_day)
        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + (chunk_bars - 1) * MS_PER_5M)
            try:
                api_rows = _fetch_bybit_api_klines(
                    config,
                    symbol=work.symbol,
                    start_ms=cursor,
                    end_ms=chunk_end,
                )
            except Exception:
                failures += 1
                api_rows = []
            for api_row in api_rows:
                parsed = _parse_bybit_api_kline_row(api_row, symbol=work.symbol)
                if parsed is None:
                    continue
                day = _row_date(int(parsed["ts_ms"]))
                if day not in pending:
                    continue
                parsed["source"] = "bybit_v5_market_kline_5m"
                rows_to_write.append(parsed)
                empty_days.discard(day)
            if request_sleep_seconds > 0:
                time.sleep(request_sleep_seconds)
            cursor = chunk_end + MS_PER_5M
    written = _write_rows(root, rows_to_write)
    return {
        "symbol": work.symbol,
        "requested_days": len(work.days),
        "written_rows": written,
        "empty_days": len(empty_days),
        "failures": failures,
    }


def _binance_month_url(symbol: str, ym: str) -> str:
    encoded = urllib.parse.quote(symbol, safe="")
    return f"{VISION_FILES}/data/futures/um/monthly/klines/{encoded}/5m/{encoded}-5m-{ym}.zip"


def _binance_day_url(symbol: str, day: str) -> str:
    encoded = urllib.parse.quote(symbol, safe="")
    return f"{VISION_FILES}/data/futures/um/daily/klines/{encoded}/5m/{encoded}-5m-{day}.zip"


def _fetch_vision_zip(url: str, *, retries: int = 4, timeout: int = 60) -> bytes | None:
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "liqmig-5m-backfill/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - public market archive
                raw = resp.read()
                header_len = resp.getheader("Content-Length")
            content_length = int(header_len) if header_len is not None else None
            _verify_download(raw, _fetch_expected_sha256(url), content_length)
            return raw
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt + 1 >= retries:
                raise
        except Exception:
            if attempt + 1 >= retries:
                raise
        time.sleep(0.75 * (attempt + 1))
    return None


def _parse_binance_kline_zip(symbol: str, raw: bytes, *, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if not names:
            return rows
        with zf.open(names[0]) as fh:
            for raw_line in io.TextIOWrapper(fh, encoding="utf-8"):
                parts = raw_line.strip().split(",")
                if len(parts) < 11 or not parts[0].lstrip("-").isdigit():
                    continue
                try:
                    rows.append(
                        {
                            "ts_ms": int(parts[0]),
                            "symbol": symbol,
                            "open": float(parts[1]),
                            "high": float(parts[2]),
                            "low": float(parts[3]),
                            "close": float(parts[4]),
                            "volume_base": float(parts[5]),
                            "turnover_quote": float(parts[7]),
                            "trade_count": int(parts[8]),
                            "taker_buy_volume_base": float(parts[9]),
                            "taker_buy_turnover_quote": float(parts[10]),
                            "source": source,
                        }
                    )
                except ValueError:
                    continue
    return rows


def _days_by_month(days: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for day in sorted(days):
        out[day[:7]].append(day)
    return dict(out)


def backfill_binance_symbol(root: Path, work: MissingWork) -> dict[str, Any]:
    pending = set(work.days)
    rows_to_write: list[dict[str, Any]] = []
    failures = 0
    not_found = 0
    for ym, days in _days_by_month(work.days).items():
        month_rows: list[dict[str, Any]] = []
        try:
            raw = _fetch_vision_zip(_binance_month_url(work.symbol, ym))
        except Exception:
            failures += 1
            raw = None
        if raw is not None:
            month_rows = _parse_binance_kline_zip(work.symbol, raw, source="binance_vision_um_5m_monthly")
        else:
            for day in days:
                try:
                    daily_raw = _fetch_vision_zip(_binance_day_url(work.symbol, day))
                except Exception:
                    failures += 1
                    continue
                if daily_raw is None:
                    not_found += 1
                    continue
                month_rows.extend(
                    _parse_binance_kline_zip(work.symbol, daily_raw, source="binance_vision_um_5m_daily")
                )
        for row in month_rows:
            if _row_date(int(row["ts_ms"])) in pending:
                rows_to_write.append(row)
    written = _write_rows(root, rows_to_write)
    days_with_rows = {_row_date(int(row["ts_ms"])) for row in rows_to_write}
    empty_days = len(pending - days_with_rows)
    return {
        "symbol": work.symbol,
        "requested_days": len(work.days),
        "written_rows": written,
        "empty_days": empty_days,
        "not_found_archives": not_found,
        "failures": failures,
    }


def _run_venue(
    venue: str,
    *,
    root: Path,
    start: str,
    end: str,
    workers: int,
    symbols: tuple[str, ...],
    limit_symbols: int,
    audit_only: bool,
    min_existing_bars: int,
    max_failure_ratio: float,
    request_sleep_seconds: float,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    work = load_missing_work(
        root,
        start=start,
        end=end,
        symbols=symbols,
        min_existing_bars=min_existing_bars,
    )
    if limit_symbols > 0:
        work = work[:limit_symbols]
    symbol_days = sum(len(item.days) for item in work)
    summary: dict[str, Any] = {
        "venue": venue,
        "root": str(root),
        "start": start,
        "end_exclusive": end,
        "dataset": "klines_5m",
        "min_existing_bars": min_existing_bars,
        "symbols_with_missing": len(work),
        "missing_symbol_days": symbol_days,
        "audit_only": audit_only,
        "started_at_utc": datetime.now(tz=UTC).isoformat(),
        "results": [],
    }
    print(
        f"{venue}: missing symbols={len(work):,} symbol-days={symbol_days:,} "
        f"window=[{start},{end}) root={root}",
        flush=True,
    )
    if audit_only or not work:
        summary["finished_at_utc"] = datetime.now(tz=UTC).isoformat()
        if progress_path is not None:
            progress_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        return summary

    fn = backfill_bybit_symbol if venue == "bybit" else backfill_binance_symbol
    failures = 0
    done_days = 0
    worker_count = max(1, min(workers, len(work)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {}
        for item in work:
            if venue == "bybit":
                fut = executor.submit(
                    fn,
                    root,
                    item,
                    request_sleep_seconds=request_sleep_seconds,
                )
            else:
                fut = executor.submit(fn, root, item)
            futures[fut] = item
        for idx, fut in enumerate(as_completed(futures), start=1):
            item = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001 - per-symbol failures are audited below
                result = {
                    "symbol": item.symbol,
                    "requested_days": len(item.days),
                    "written_rows": 0,
                    "empty_days": len(item.days),
                    "failures": 1,
                    "error": repr(exc),
                }
            failures += int(result.get("failures", 0))
            done_days += int(result.get("requested_days", 0))
            summary["results"].append(result)
            if idx % 10 == 0 or idx == len(work):
                print(
                    f"{venue}: [{idx:,}/{len(work):,}] symbol_days={done_days:,}/{symbol_days:,} "
                    f"rows_written={sum(int(r.get('written_rows', 0)) for r in summary['results']):,} "
                    f"failures={failures}",
                    flush=True,
                )
                if progress_path is not None:
                    progress_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    summary["finished_at_utc"] = datetime.now(tz=UTC).isoformat()
    summary["failures"] = failures
    summary["written_rows"] = sum(int(r.get("written_rows", 0)) for r in summary["results"])
    summary["empty_days"] = sum(int(r.get("empty_days", 0)) for r in summary["results"])
    failure_ratio = failures / max(len(work), 1)
    summary["failure_ratio_per_symbol"] = failure_ratio
    if failure_ratio > max_failure_ratio:
        summary["status"] = "failed"
    else:
        summary["status"] = "ok"
    if progress_path is not None:
        progress_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venue", choices=("bybit", "binance", "both"), required=True)
    parser.add_argument("--start", default=DEFAULT_START, help="Inclusive start date YYYY-MM-DD.")
    parser.add_argument(
        "--end",
        default=None,
        help="Exclusive end date YYYY-MM-DD. Default is each root's manifest tail + 1 day.",
    )
    parser.add_argument("--bybit-root", default=str(_root_for_venue("bybit")))
    parser.add_argument("--binance-root", default=str(_root_for_venue("binance")))
    parser.add_argument("--workers", type=int, default=4, help="Concurrent symbols.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    parser.add_argument("--limit-symbols", type=int, default=0, help="Process only first N missing symbols.")
    parser.add_argument("--audit-only", action="store_true", help="Only report missing coverage; do not download.")
    parser.add_argument("--min-existing-bars", type=int, default=EXPECTED_5M_BARS_PER_DAY)
    parser.add_argument("--max-failure-ratio", type=float, default=0.005)
    parser.add_argument("--request-sleep-seconds", type=float, default=0.0, help="Bybit per-request throttle inside a worker.")
    parser.add_argument("--report-dir", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venues = ("bybit", "binance") if args.venue == "both" else (args.venue,)
    symbols = tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip())
    reports: list[dict[str, Any]] = []
    for venue in venues:
        root = Path(args.bybit_root if venue == "bybit" else args.binance_root).expanduser()
        end = args.end or _manifest_end_exclusive(root)
        report_dir = Path(args.report_dir).expanduser() if args.report_dir else root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        report_path = report_dir / f"backfill_5m_klines_{venue}_{stamp}.json"
        report = _run_venue(
            venue,
            root=root,
            start=args.start,
            end=end,
            workers=args.workers,
            symbols=symbols,
            limit_symbols=args.limit_symbols,
            audit_only=args.audit_only,
            min_existing_bars=args.min_existing_bars,
            max_failure_ratio=args.max_failure_ratio,
            request_sleep_seconds=args.request_sleep_seconds,
            progress_path=report_path,
        )
        report["report_path"] = str(report_path)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"{venue}: report={report_path}", flush=True)
        reports.append(report)
    return 1 if any(r.get("status") == "failed" for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
