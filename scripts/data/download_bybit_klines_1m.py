#!/usr/bin/env python3
"""Download Bybit 1-minute klines into the full-PIT root.

Writes ``<root>/klines_1m/date=YYYY-MM-DD/symbol=<SYM>/part.parquet`` with the
same schema as ``klines_1h``, so the two are readable by one glob and one
``pl.scan_parquet``.

This is NOT the retired ``bybit_render_1m`` plan (removed 2026-07-21,
``docs/data.md``). It is a dataset inside the existing ``bybit_full_pit`` root,
fetched through the same ``BybitMarketData.get_klines`` path the 1h builder
uses, with ``interval="1"``.

Resumable at (symbol, date) granularity: existing partitions are never
refetched, so an interrupted run continues where it stopped. Symbols are
processed in the order given, which lets a partial run still answer a question
if the list is ordered by research value.

Usage:
  .venv/bin/python scripts/data/download_bybit_klines_1m.py --symbols-file syms.txt
  .venv/bin/python scripts/data/download_bybit_klines_1m.py --symbols BTCUSDT,ETHUSDT \
      --start 2024-01-01 --end 2024-02-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData  # noqa: E402

MINUTE_MS = 60_000
DAY_MS = 86_400_000
#: Bybit v5 returns at most 1000 klines per request.
PAGE_MINUTES = 1000
SOURCE = "bybit_v5_market_kline_1m"
SCHEMA = {
    "ts_ms": pl.Int64,
    "symbol": pl.String,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume_base": pl.Float64,
    "turnover_quote": pl.Float64,
    "source": pl.String,
    "date": pl.String,
}

_print_lock = threading.Lock()


def _log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def _to_ms(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _dates(start: dt.date, end: dt.date) -> list[dt.date]:
    """Dates in ``[start, end)``."""
    return [start + dt.timedelta(days=i) for i in range((end - start).days)]


def _missing_dates(root: Path, symbol: str, days: list[dt.date]) -> list[dt.date]:
    return [d for d in days if not (root / f"date={d}" / f"symbol={symbol}" / "part.parquet").exists()]


def _runs(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    """Collapse a sorted date list into contiguous [lo, hi] runs."""
    if not days:
        return []
    runs: list[tuple[dt.date, dt.date]] = []
    lo = prev = days[0]
    for day in days[1:]:
        if (day - prev).days == 1:
            prev = day
            continue
        runs.append((lo, prev))
        lo = prev = day
    runs.append((lo, prev))
    return runs


def _fetch_span(client: BybitMarketData, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Every 1m kline in ``[start_ms, end_ms)``, paged at 1000 bars."""
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        stop = min(cursor + PAGE_MINUTES * MINUTE_MS, end_ms)
        page = client.get_klines(symbol, "1", cursor, stop - 1)
        for item in page:
            ts = int(item[0])
            if ts < start_ms or ts >= end_ms:
                continue
            rows.append(
                {
                    "ts_ms": ts,
                    "symbol": symbol,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume_base": float(item[5]),
                    "turnover_quote": float(item[6]),
                    "source": SOURCE,
                }
            )
        cursor = stop
    return rows


def _write(root: Path, symbol: str, rows: list[dict], wanted: set[dt.date]) -> int:
    """Partition rows by UTC date and write the wanted ones. Returns partitions written."""
    if not rows:
        return 0
    frame = (
        pl.DataFrame(rows)
        .unique(subset=["ts_ms"], keep="first")
        .sort("ts_ms")
        .with_columns(
            pl.from_epoch("ts_ms", time_unit="ms").dt.date().cast(pl.String).alias("date")
        )
        .select(list(SCHEMA))
        .cast(SCHEMA)  # type: ignore[arg-type]
    )
    written = 0
    for (day,), part in frame.partition_by("date", as_dict=True).items():
        day_str = str(day)
        if dt.date.fromisoformat(day_str) not in wanted:
            continue
        out = root / f"date={day_str}" / f"symbol={symbol}"
        out.mkdir(parents=True, exist_ok=True)
        part.write_parquet(out / "part.parquet")
        written += 1
    return written


#: Fetch and flush in chunks this many days wide, so an interrupted run keeps
#: everything already downloaded instead of losing a whole symbol's buffer.
CHUNK_DAYS = 14


def _do_symbol(
    root: Path, symbol: str, start: dt.date, end: dt.date, index: int, total: int
) -> tuple[str, int, int]:
    days = _dates(start, end)
    missing = _missing_dates(root, symbol, days)
    if not missing:
        return symbol, 0, 0
    client = BybitMarketData(category="linear", testnet=False)
    wanted = set(missing)
    written = 0
    failures = 0
    for lo, hi in _runs(missing):
        cursor = lo
        while cursor <= hi:
            stop = min(cursor + dt.timedelta(days=CHUNK_DAYS - 1), hi)
            try:
                rows = _fetch_span(client, symbol, _to_ms(cursor), _to_ms(stop) + DAY_MS)
                written += _write(root, symbol, rows, wanted)
            except Exception as exc:  # noqa: BLE001 - one chunk must not kill the run
                failures += 1
                if failures <= 3:
                    _log(f"  [{index}/{total}] {symbol} {cursor}..{stop} FAILED: {type(exc).__name__}: {exc}")
            cursor = stop + dt.timedelta(days=1)
    _log(
        f"  [{index}/{total}] {symbol}: {written}/{len(missing)} partitions"
        + (f", {failures} chunk failures" if failures else "")
    )
    return symbol, written, len(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "bybit_full_pit"))
    ap.add_argument("--symbols", help="comma-separated symbols, in priority order")
    ap.add_argument("--symbols-file", help="one symbol per line, in priority order")
    ap.add_argument(
        "--windows-file",
        help="CSV with symbol,lo,hi giving each symbol's own date window, in priority order. "
        "Avoids requesting years of pre-listing history per symbol.",
    )
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit-symbols", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    # (symbol, start, end) with end EXCLUSIVE, in priority order.
    jobs: list[tuple[str, dt.date, dt.date]] = []
    if args.windows_file:
        import csv

        with Path(args.windows_file).open() as handle:
            for row in csv.DictReader(handle):
                lo = max(dt.date.fromisoformat(row["lo"]), start)
                hi = min(dt.date.fromisoformat(row["hi"]) + dt.timedelta(days=1), end)
                if lo < hi:
                    jobs.append((row["symbol"], lo, hi))
    elif args.symbols_file:
        names = [s.strip() for s in Path(args.symbols_file).read_text().splitlines() if s.strip()]
        jobs = [(s, start, end) for s in names]
    elif args.symbols:
        names = [s.strip() for s in args.symbols.split(",") if s.strip()]
        jobs = [(s, start, end) for s in names]
    else:
        ap.error("one of --symbols, --symbols-file or --windows-file is required")
    if args.limit_symbols:
        jobs = jobs[: args.limit_symbols]

    root = Path(args.root).expanduser() / "klines_1m"
    root.mkdir(parents=True, exist_ok=True)

    total = len(jobs)
    span_days = sum((hi - lo).days for _, lo, hi in jobs)
    _log(f"1m klines -> {root}")
    _log(f"{total} symbols, {span_days:,} symbol-days, {args.workers} workers")

    done = 0
    parts = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_do_symbol, root, sym, lo, hi, i + 1, total): sym
            for i, (sym, lo, hi) in enumerate(jobs)
        }
        for fut in as_completed(futures):
            _, written, _missing = fut.result()
            done += 1
            parts += written
            if done % 25 == 0:
                _log(f"== {done}/{total} symbols, {parts:,} partitions written ==")
    _log(f"DONE: {done}/{total} symbols, {parts:,} partitions written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
