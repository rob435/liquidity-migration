#!/usr/bin/env python3
"""Fetch Binance Vision alternative/granular datasets for render-overlap symbols.

Owner-directed data acquisition (2026-07-20).  Builds a NEW research root
(default ``~/SHARED_DATA/binance_vision_alt``) from the public checksummed
archive (the FAPI-REST region caveat does not apply to the Vision CDN):

- ``funding/symbol=S/month=YYYY-MM.parquet``      — settled funding (monthly)
- ``premium_1m/symbol=S/month=YYYY-MM.parquet``   — 1m premium-index klines
- ``klines_1m/symbol=S/month=YYYY-MM.parquet``    — 1m klines incl. taker-buy flow
- ``metrics/symbol=S/month=YYYY-MM.parquet``      — 5m OI + long/short + taker ratios

Monthly zips first, then daily zips for the tail month.  liquidationSnapshot
no longer exists in the archive catalog (checked 2026-07-20) and is recorded
as unavailable.  Every output file is written atomically and doubles as the
resume marker; zip payloads are verified against the archive's .CHECKSUM
when present.  Timestamps are normalized to ms (2025+ Vision files switched
some columns to microseconds).  Symbol mapping from the Bybit render universe
tries the identical name, then the leading/trailing multiplier transform
(e.g. SHIB1000USDT <-> 1000SHIBUSDT); unmatched symbols are recorded.

Usage:
  .venv\\Scripts\\python.exe scripts/research_v3/fetch_binance_vision_alt.py \\
      [--out-root ...] [--start-month 2023-03] [--end-day 2026-07-10] [--rate 20]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import v4_shared  # noqa: E402

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
UM = "data/futures/um"
DEFAULT_OUT = Path.home() / "SHARED_DATA" / "binance_vision_alt"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 5

_print_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_request = [0.0]


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def throttle(min_interval: float) -> None:
    with _throttle_lock:
        wait = _last_request[0] + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def http_bytes(url: str, min_interval: float, *, missing_ok: bool = False) -> bytes | None:
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        throttle(min_interval)
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                if missing_ok:
                    return None
                raise
            if attempt == MAX_RETRIES - 1:
                raise
        except Exception:  # noqa: BLE001 - retry loop
            if attempt == MAX_RETRIES - 1:
                raise
        time.sleep(delay)
        delay = min(delay * 2.0, 30.0)
    return None


def s3_list(prefix: str, min_interval: float, *, delimiter: str = "/") -> tuple[list[str], list[str]]:
    """All CommonPrefixes and Keys under a prefix, following pagination markers."""
    prefixes: list[str] = []
    keys: list[str] = []
    marker = ""
    while True:
        url = f"{S3}?delimiter={delimiter}&prefix={urllib.parse.quote(prefix)}"
        if marker:
            url += f"&marker={urllib.parse.quote(marker)}"
        raw = http_bytes(url, min_interval)
        text = raw.decode("utf-8", errors="replace")
        prefixes.extend(re.findall(r"<Prefix>([^<]+)</Prefix>", text))
        page_keys = re.findall(r"<Key>([^<]+)</Key>", text)
        keys.extend(page_keys)
        if "<IsTruncated>true</IsTruncated>" not in text:
            break
        next_marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", text)
        candidates = page_keys or [p for p in prefixes if p != prefix]
        if next_marker:
            marker = next_marker.group(1)
        elif candidates:
            marker = candidates[-1]
        else:
            break
    return [p for p in dict.fromkeys(prefixes) if p != prefix], list(dict.fromkeys(keys))


def fetch_zip_csv(url: str, min_interval: float) -> list[list[str]] | None:
    raw = http_bytes(url, min_interval, missing_ok=True)
    if raw is None:
        return None
    checksum_raw = http_bytes(url + ".CHECKSUM", min_interval, missing_ok=True)
    if checksum_raw:
        expected = checksum_raw.decode("utf-8", errors="replace").split()[0].strip().lower()
        actual = hashlib.sha256(raw).hexdigest()
        if re.fullmatch(r"[0-9a-f]{64}", expected) and actual != expected:
            raise RuntimeError(f"checksum mismatch for {url}")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", errors="replace")
            rows = list(csv.reader(text))
    return rows


def normalize_ts_ms(value: str) -> int:
    ts = int(float(value))
    if ts >= 10**15:  # microseconds (Vision 2025+ files)
        return ts // 1000
    return ts


def parse_ts_or_datetime_ms(value: str) -> int:
    value = value.strip()
    if re.fullmatch(r"\d{10,}", value):
        return normalize_ts_ms(value)
    parsed = dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


HEADER_FIRST_CELLS = {"open_time", "calc_time", "create_time"}


def _is_header(row: list[str]) -> bool:
    return bool(row) and row[0].strip().lower() in HEADER_FIRST_CELLS


def parse_klines(symbol: str, rows: list[list[str]]) -> pl.DataFrame:
    body = [r for r in rows if r and not _is_header(r)]
    return pl.DataFrame(
        {
            "ts_ms": [normalize_ts_ms(r[0]) for r in body],
            "symbol": symbol,
            "open": [float(r[1]) for r in body],
            "high": [float(r[2]) for r in body],
            "low": [float(r[3]) for r in body],
            "close": [float(r[4]) for r in body],
            "volume": [float(r[5]) for r in body],
            "quote_volume": [float(r[7]) for r in body],
            "trade_count": [int(float(r[8])) for r in body],
            "taker_buy_volume": [float(r[9]) for r in body],
            "taker_buy_quote_volume": [float(r[10]) for r in body],
        }
    ).unique("ts_ms").sort("ts_ms")


def parse_premium(symbol: str, rows: list[list[str]]) -> pl.DataFrame:
    body = [r for r in rows if r and not _is_header(r)]
    return pl.DataFrame(
        {
            "ts_ms": [normalize_ts_ms(r[0]) for r in body],
            "symbol": symbol,
            "open": [float(r[1]) for r in body],
            "high": [float(r[2]) for r in body],
            "low": [float(r[3]) for r in body],
            "close": [float(r[4]) for r in body],
        }
    ).unique("ts_ms").sort("ts_ms")


def parse_funding(symbol: str, rows: list[list[str]]) -> pl.DataFrame:
    body = [r for r in rows if r and not _is_header(r)]
    return pl.DataFrame(
        {
            "ts_ms": [parse_ts_or_datetime_ms(r[0]) for r in body],
            "symbol": symbol,
            "funding_interval_hours": [float(r[1]) if r[1].strip() else None for r in body],
            "funding_rate": [float(r[2]) for r in body],
        },
        schema={
            "ts_ms": pl.Int64, "symbol": pl.String,
            "funding_interval_hours": pl.Float64, "funding_rate": pl.Float64,
        },
    ).unique("ts_ms").sort("ts_ms")


def parse_metrics(symbol: str, rows: list[list[str]]) -> pl.DataFrame:
    body = [r for r in rows if r and not _is_header(r)]

    def opt(value: str) -> float | None:
        value = value.strip()
        return float(value) if value else None

    return pl.DataFrame(
        {
            "ts_ms": [parse_ts_or_datetime_ms(r[0]) for r in body],
            "symbol": symbol,
            "sum_open_interest": [opt(r[2]) for r in body],
            "sum_open_interest_value": [opt(r[3]) for r in body],
            "count_toptrader_long_short_ratio": [opt(r[4]) for r in body],
            "sum_toptrader_long_short_ratio": [opt(r[5]) for r in body],
            "count_long_short_ratio": [opt(r[6]) for r in body],
            "sum_taker_long_short_vol_ratio": [opt(r[7]) for r in body],
        },
        schema={
            "ts_ms": pl.Int64, "symbol": pl.String,
            "sum_open_interest": pl.Float64, "sum_open_interest_value": pl.Float64,
            "count_toptrader_long_short_ratio": pl.Float64, "sum_toptrader_long_short_ratio": pl.Float64,
            "count_long_short_ratio": pl.Float64, "sum_taker_long_short_vol_ratio": pl.Float64,
        },
    ).unique("ts_ms").sort("ts_ms")


def month_range(start_month: str, end_day: dt.date) -> list[str]:
    year, month = int(start_month[:4]), int(start_month[5:7])
    months = []
    while (year, month) <= (end_day.year, end_day.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


def write_atomic(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, path)


def map_symbols(render: set[str], vision: set[str]) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    unmatched: list[str] = []
    for symbol in sorted(render):
        if symbol in vision:
            mapping[symbol] = symbol
            continue
        moved = None
        match = re.fullmatch(r"([A-Z]+?)(1[0]{2,8})USDT", symbol)  # SHIB1000USDT -> 1000SHIBUSDT
        if match:
            moved = f"{match.group(2)}{match.group(1)}USDT"
        else:
            match = re.fullmatch(r"(1[0]{2,8})([A-Z]+?)USDT", symbol)  # 1000XUSDT -> X1000USDT
            if match:
                moved = f"{match.group(2)}{match.group(1)}USDT"
        if moved and moved in vision:
            mapping[symbol] = moved
        else:
            unmatched.append(symbol)
    return mapping, unmatched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start-month", default="2023-03")
    parser.add_argument("--end-day", default="2026-07-10", help="end-exclusive UTC date")
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--datasets", nargs="*", default=["funding", "premium_1m", "klines_1m", "metrics"])
    parser.add_argument("--limit-symbols", type=int, default=None, help="smoke-test cap")
    args = parser.parse_args()
    min_interval = 1.0 / args.rate
    end_day = dt.date.fromisoformat(args.end_day)
    months = month_range(args.start_month, end_day)
    tail_month = months[-1]

    prefixes, _ = s3_list(f"{UM}/monthly/klines/", min_interval)
    vision_universe = {p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes}
    render = v4_shared.render_symbols()
    mapping, unmatched = map_symbols(render, vision_universe)
    log(f"render symbols {len(render)}; matched on Vision {len(mapping)}; unmatched {len(unmatched)}")
    if args.limit_symbols:
        mapping = dict(sorted(mapping.items())[: args.limit_symbols])

    args.out_root.mkdir(parents=True, exist_ok=True)
    (args.out_root / "fetch_run.json").write_text(
        json.dumps(
            {
                "kind": "binance_vision_alt_fetch",
                "started_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
                "months": [months[0], months[-1]],
                "end_day_exclusive": args.end_day,
                "datasets": args.datasets,
                "symbol_mapping": mapping,
                "unmatched_render_symbols": unmatched,
                "liquidationSnapshot": "absent from the Vision catalog as of 2026-07-20",
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    def days_of_tail_month() -> list[str]:
        first = dt.date(int(tail_month[:4]), int(tail_month[5:7]), 1)
        days = []
        day = first
        while day < end_day:
            days.append(day.isoformat())
            day += dt.timedelta(days=1)
        return days

    def job_monthly(
        dataset: str, url_dataset: str, path_seg: str, file_seg: str, parse: Callable
    ) -> list[tuple]:
        jobs = []
        for bybit_symbol, symbol in mapping.items():
            for month in months[:-1]:
                out = args.out_root / dataset / f"symbol={bybit_symbol}" / f"month={month}.parquet"
                if out.is_file():
                    continue
                url = f"{CDN}/{UM}/monthly/{url_dataset}/{symbol}{path_seg}/{symbol}{file_seg}-{month}.zip"
                jobs.append((dataset, bybit_symbol, symbol, month, url, parse, out, "monthly"))
        return jobs

    def job_tail_daily(
        dataset: str, url_dataset: str, path_seg: str, file_seg: str, parse: Callable
    ) -> list[tuple]:
        jobs = []
        for bybit_symbol, symbol in mapping.items():
            out = args.out_root / dataset / f"symbol={bybit_symbol}" / f"month={tail_month}.partial.parquet"
            if out.is_file():
                continue
            urls = [
                f"{CDN}/{UM}/daily/{url_dataset}/{symbol}{path_seg}/{symbol}{file_seg}-{day}.zip"
                for day in days_of_tail_month()
            ]
            jobs.append((dataset, bybit_symbol, symbol, tail_month, urls, parse, out, "daily-tail"))
        return jobs

    def job_metrics() -> list[tuple]:
        jobs = []
        for bybit_symbol, symbol in mapping.items():
            for month in months:
                suffix = ".partial.parquet" if month == tail_month else ".parquet"
                out = args.out_root / "metrics" / f"symbol={bybit_symbol}" / f"month={month}{suffix}"
                if out.is_file():
                    continue
                if month == tail_month:
                    days = days_of_tail_month()
                else:
                    first = dt.date(int(month[:4]), int(month[5:7]), 1)
                    nxt = dt.date(first.year + (first.month == 12), first.month % 12 + 1, 1)
                    days = [
                        (first + dt.timedelta(days=i)).isoformat() for i in range((nxt - first).days)
                    ]
                urls = [
                    f"{CDN}/{UM}/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip" for day in days
                ]
                jobs.append(("metrics", bybit_symbol, symbol, month, urls, parse_metrics, out, "daily-agg"))
        return jobs

    jobs: list[tuple] = []
    if "funding" in args.datasets:
        jobs += job_monthly("funding", "fundingRate", "", "-fundingRate", parse_funding)
    if "premium_1m" in args.datasets:
        jobs += job_monthly("premium_1m", "premiumIndexKlines", "/1m", "-1m", parse_premium)
        jobs += job_tail_daily("premium_1m", "premiumIndexKlines", "/1m", "-1m", parse_premium)
    if "klines_1m" in args.datasets:
        jobs += job_monthly("klines_1m", "klines", "/1m", "-1m", parse_klines)
        jobs += job_tail_daily("klines_1m", "klines", "/1m", "-1m", parse_klines)
    if "metrics" in args.datasets:
        jobs += job_metrics()
    log(f"pending jobs: {len(jobs)}")

    done = [0]
    missing_files = [0]
    failures: list[str] = []
    lock = threading.Lock()

    def run_job(job: tuple) -> None:
        dataset, bybit_symbol, symbol, month, source, parse, out, kind = job
        try:
            if kind == "monthly":
                rows = fetch_zip_csv(source, min_interval)
                if rows is None:
                    with lock:
                        missing_files[0] += 1
                    write_atomic(pl.DataFrame(), out)  # empty marker: month absent upstream
                    return
                frame = parse(symbol, rows).with_columns(pl.lit(bybit_symbol).alias("symbol"))
            else:
                frames = []
                for url in source:
                    rows = fetch_zip_csv(url, min_interval)
                    if rows is None:
                        with lock:
                            missing_files[0] += 1
                        continue
                    frames.append(parse(symbol, rows))
                if not frames:
                    write_atomic(pl.DataFrame(), out)
                    return
                frame = (
                    pl.concat(frames, how="vertical")
                    .unique("ts_ms")
                    .sort("ts_ms")
                    .with_columns(pl.lit(bybit_symbol).alias("symbol"))
                )
            write_atomic(frame, out)
            with lock:
                done[0] += 1
                if done[0] % 200 == 0:
                    log(f"progress: {done[0]} jobs done, {missing_files[0]} absent upstream")
        except Exception as error:  # noqa: BLE001 - record and continue
            with lock:
                failures.append(f"{dataset}/{bybit_symbol}/{month}: {error}")
                log(f"FAILED {dataset}/{bybit_symbol}/{month}: {error}")

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run_job, jobs))

    summary: dict[str, Any] = {
        "finished_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "jobs_done": done[0],
        "files_absent_upstream": missing_files[0],
        "failures": failures[:200],
        "failure_count": len(failures),
    }
    (args.out_root / "fetch_summary.json").write_text(
        json.dumps(summary, indent=1) + "\n", encoding="utf-8"
    )
    log(json.dumps({k: v for k, v in summary.items() if k != "failures"}))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
