#!/usr/bin/env python3
"""Download Bybit 1-minute trade or mark-price klines into the full-PIT root.

Trade bars write under ``klines_1m``. Mark-price bars write under
``mark_price_1m`` and are the trigger stream for Bybit position stops. Both use
the same partition and OHLC schema; mark-price volume fields are null because
the endpoint does not publish traded volume.

Resumable at (symbol, date) granularity: existing partitions are never
refetched, so an interrupted run continues where it stopped. Symbols are
processed in the order given, which lets a partial run still answer a question
if the list is ordered by research value.

Usage:
  .venv/bin/python scripts/data/download_bybit_klines_1m.py --symbols-file syms.txt
  .venv/bin/python scripts/data/download_bybit_klines_1m.py --symbols BTCUSDT,ETHUSDT \
      --start 2024-01-01 --end 2024-02-01
  .venv/bin/python scripts/data/download_bybit_klines_1m.py --price-stream mark \
      --windows-file candidate-windows.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, cast

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData  # noqa: E402

MINUTE_MS = 60_000
DAY_MS = 86_400_000
#: Bybit v5 returns at most 1000 klines per request.
PAGE_MINUTES = 1000
TRADE_SOURCE = "bybit_v5_market_kline_1m"
MARK_SOURCE = "bybit_v5_market_mark_price_kline_1m"
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


def _merge_jobs(
    jobs: list[tuple[str, dt.date, dt.date]],
) -> list[tuple[str, dt.date, dt.date]]:
    """Merge overlapping date windows without changing symbol priority."""

    symbol_order = list(dict.fromkeys(symbol for symbol, _, _ in jobs))
    by_symbol: dict[str, list[tuple[dt.date, dt.date]]] = {symbol: [] for symbol in symbol_order}
    for symbol, start, end in jobs:
        by_symbol[symbol].append((start, end))
    merged: list[tuple[str, dt.date, dt.date]] = []
    for symbol in symbol_order:
        spans: list[tuple[dt.date, dt.date]] = []
        for start, end in sorted(by_symbol[symbol]):
            if not spans or start > spans[-1][1]:
                spans.append((start, end))
            else:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        merged.extend((symbol, start, end) for start, end in spans)
    return merged


def _fetch_span(
    client: BybitMarketData,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    price_stream: str,
) -> list[dict]:
    """Every 1m kline in ``[start_ms, end_ms)``, paged at 1000 bars."""
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        stop = min(cursor + PAGE_MINUTES * MINUTE_MS, end_ms)
        if price_stream == "trade":
            page = client.get_klines(symbol, "1", cursor, stop - 1)
            source = TRADE_SOURCE
        elif price_stream == "mark":
            page = cast(
                list[list[Any]],
                client.get_mark_price_klines(symbol, "1", cursor, stop - 1),
            )
            source = MARK_SOURCE
        else:
            raise ValueError(f"unsupported price stream: {price_stream}")
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
                    "volume_base": float(item[5]) if price_stream == "trade" else None,
                    "turnover_quote": float(item[6]) if price_stream == "trade" else None,
                    "source": source,
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
        .with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.date().cast(pl.String).alias("date"))
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
    root: Path,
    symbol: str,
    start: dt.date,
    end: dt.date,
    index: int,
    total: int,
    *,
    price_stream: str,
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
                rows = _fetch_span(
                    client,
                    symbol,
                    _to_ms(cursor),
                    _to_ms(stop) + DAY_MS,
                    price_stream=price_stream,
                )
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
    ap.add_argument(
        "--price-stream",
        choices=("trade", "mark"),
        default="trade",
        help="trade writes klines_1m; mark writes mark_price_1m",
    )
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
    jobs = _merge_jobs(jobs)
    if args.limit_symbols:
        allowed = set(list(dict.fromkeys(symbol for symbol, _, _ in jobs))[: args.limit_symbols])
        jobs = [job for job in jobs if job[0] in allowed]

    dataset = "klines_1m" if args.price_stream == "trade" else "mark_price_1m"
    root = Path(args.root).expanduser() / dataset
    root.mkdir(parents=True, exist_ok=True)

    total = len(jobs)
    total_symbols = len({symbol for symbol, _, _ in jobs})
    span_days = sum((hi - lo).days for _, lo, hi in jobs)
    _log(f"Bybit 1m {args.price_stream}-price klines -> {root}")
    _log(f"{total_symbols} symbols, {total} windows, {span_days:,} symbol-days, {args.workers} workers")

    done = 0
    parts = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _do_symbol,
                root,
                sym,
                lo,
                hi,
                i + 1,
                total,
                price_stream=args.price_stream,
            ): sym
            for i, (sym, lo, hi) in enumerate(jobs)
        }
        for fut in as_completed(futures):
            _, written, _missing = fut.result()
            done += 1
            parts += written
            if done % 25 == 0:
                _log(f"== {done}/{total} windows, {parts:,} partitions written ==")
    _log(f"DONE: {done}/{total} windows across {total_symbols} symbols, {parts:,} partitions written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
