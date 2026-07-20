#!/usr/bin/env python3
"""Fetch full-coverage Bybit 1m klines for the T-A render-book symbols.

Owner-directed data acquisition (2026-07-20) for render-native granular
research.  Builds a NEW research root (default
``~/SHARED_DATA/bybit_render_1m``) with hive partitions
``klines_1m/date=YYYY-MM-DD/symbol=SYMBOL/bars.parquet`` over the full render
window — full coverage, not event-sliced, so counterfactual timing studies
are not conditioned on entries having happened.  The June 2026 event-sliced
root (``continuous_v2_1m``) is left untouched.

Mechanics: Bybit v5 ``market/kline`` (category=linear, interval=1), ascending
1000-bar pages per symbol, per-symbol JSON progress markers (resume-safe),
atomic day-partition writes (tmp+rename), validation (minute grid, finite
positive prices, in-window, dedup).  Symbols carrying render entries that the
June root does not cover are fetched first.  Read-only against the exchange;
writes only under the new research root.  No runtime interaction.

Usage:
  .venv\\Scripts\\python.exe scripts/research_v3/fetch_bybit_render_1m.py \\
      [--workers 8] [--out-root ...] [--start 2023-03-26] [--end 2026-07-10]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from scripts.research_v3 import v4_shared  # noqa: E402

API = "https://api.bybit.com/v5/market/kline"
PAGE_BARS = 1000
MS_PER_MINUTE = 60_000
DEFAULT_OUT = Path.home() / "SHARED_DATA" / "bybit_render_1m"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 6

_print_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_request = [0.0]


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def throttle(min_interval: float) -> None:
    """Global pacing across threads (Bybit market-data budget is shared per IP)."""
    with _throttle_lock:
        wait = _last_request[0] + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def fetch_page(symbol: str, start_ms: int, end_ms: int, min_interval: float) -> list[list[str]]:
    params = urllib.parse.urlencode(
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "start": start_ms,
            "end": end_ms - 1,
            "limit": PAGE_BARS,
        }
    )
    url = f"{API}?{params}"
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        throttle(min_interval)
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read())
            if payload.get("retCode") != 0:
                raise RuntimeError(f"retCode {payload.get('retCode')}: {payload.get('retMsg')}")
            rows = payload.get("result", {}).get("list", [])
            return list(reversed(rows))  # API returns descending; we want ascending
        except Exception as error:  # noqa: BLE001 - retry loop reports at the end
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"{symbol} @ {start_ms}: {error}") from error
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)
    return []


def validate_rows(symbol: str, rows: list[list[str]], start_ms: int, end_ms: int) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(
            schema={
                "ts_ms": pl.Int64, "symbol": pl.String, "open": pl.Float64, "high": pl.Float64,
                "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "turnover": pl.Float64,
            }
        )
    frame = pl.DataFrame(
        {
            "ts_ms": [int(r[0]) for r in rows],
            "symbol": symbol,
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
            "turnover": [float(r[6]) for r in rows],
        }
    ).unique("ts_ms").sort("ts_ms")
    frame = frame.filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
    bad_grid = frame.filter(pl.col("ts_ms") % MS_PER_MINUTE != 0)
    if not bad_grid.is_empty():
        raise RuntimeError(f"{symbol}: {bad_grid.height} bars off the minute grid")
    bad_price = frame.filter(
        ~(
            pl.col("open").is_finite() & (pl.col("open") > 0)
            & pl.col("high").is_finite() & (pl.col("high") > 0)
            & pl.col("low").is_finite() & (pl.col("low") > 0)
            & pl.col("close").is_finite() & (pl.col("close") > 0)
        )
    )
    if not bad_price.is_empty():
        raise RuntimeError(f"{symbol}: {bad_price.height} bars with invalid prices")
    return frame


def write_day(out_root: Path, symbol: str, day_ms: int, frame: pl.DataFrame) -> None:
    day = dt.datetime.fromtimestamp(day_ms / 1000, tz=dt.timezone.utc).date().isoformat()
    part_dir = out_root / "klines_1m" / f"date={day}" / f"symbol={symbol}"
    part_dir.mkdir(parents=True, exist_ok=True)
    tmp = part_dir / f".bars.{os.getpid()}.tmp.parquet"
    frame.write_parquet(tmp)
    os.replace(tmp, part_dir / "bars.parquet")


def marker_path(out_root: Path, symbol: str) -> Path:
    return out_root / "_markers" / f"{symbol}.json"


def load_marker(out_root: Path, symbol: str, default_start: int) -> dict[str, Any]:
    path = marker_path(out_root, symbol)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"next_start_ms": default_start, "days_written": 0, "bars_written": 0, "done": False}


def save_marker(out_root: Path, symbol: str, state: dict[str, Any]) -> None:
    path = marker_path(out_root, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fetch_symbol(
    out_root: Path, symbol: str, start_ms: int, end_ms: int, min_interval: float
) -> dict[str, Any]:
    state = load_marker(out_root, symbol, start_ms)
    if state.get("done"):
        return state
    cursor = int(state["next_start_ms"])
    pending: dict[int, pl.DataFrame] = {}
    empty_pages = 0
    while cursor < end_ms:
        page_end = min(cursor + PAGE_BARS * MS_PER_MINUTE, end_ms)
        rows = fetch_page(symbol, cursor, page_end, min_interval)
        frame = validate_rows(symbol, rows, cursor, page_end)
        if frame.is_empty():
            empty_pages += 1
        else:
            for day_ms, part in frame.with_columns(
                ((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("_day")
            ).partition_by("_day", as_dict=True).items():
                key = int(day_ms[0] if isinstance(day_ms, tuple) else day_ms)
                part = part.drop("_day")
                pending[key] = (
                    pl.concat([pending[key], part]).unique("ts_ms").sort("ts_ms")
                    if key in pending
                    else part
                )
        cursor = page_end
        # Flush every completed day (all pending days strictly before the cursor's day).
        cursor_day = (cursor // MS_PER_DAY) * MS_PER_DAY
        for key in sorted(k for k in pending if k < cursor_day):
            write_day(out_root, symbol, key, pending.pop(key))
            state["days_written"] = int(state.get("days_written", 0)) + 1
        state["bars_written"] = int(state.get("bars_written", 0)) + frame.height
        state["next_start_ms"] = cursor
        save_marker(out_root, symbol, state)
    for key in sorted(pending):
        write_day(out_root, symbol, key, pending.pop(key))
        state["days_written"] = int(state.get("days_written", 0)) + 1
    state["done"] = True
    state["empty_pages"] = empty_pages
    save_marker(out_root, symbol, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start", default="2023-03-26")
    parser.add_argument("--end", default="2026-07-10", help="end-exclusive UTC date")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rate", type=float, default=25.0, help="max requests/second overall")
    parser.add_argument("--symbols", nargs="*", default=None, help="override symbol list")
    args = parser.parse_args()

    start_ms = int(dt.datetime.fromisoformat(args.start + "T00:00:00+00:00").timestamp() * 1000)
    end_ms = int(dt.datetime.fromisoformat(args.end + "T00:00:00+00:00").timestamp() * 1000)
    min_interval = 1.0 / args.rate

    if args.symbols:
        symbols = sorted({s.upper() for s in args.symbols})
        priority = symbols
    else:
        symbols = sorted(v4_shared.render_symbols())
        # Render-entry (symbol, day) pairs missing from the June event-sliced root
        # go first so entry-window analysis unblocks early.
        june_cov = pl.read_parquet(
            Path.home() / "SHARED_DATA" / "continuous_v2_1m" / "coverage_needed_bybit.parquet"
        )
        book = v4_shared.load_render_book("gate_off")
        entries = book.unique(subset=["symbol", "entry_date"]).select("symbol", "entry_date")
        missing = entries.join(
            june_cov, left_on=["symbol", "entry_date"], right_on=["symbol", "date"], how="anti"
        )
        missing_symbols = sorted(set(missing["symbol"].to_list()))
        priority = missing_symbols + [s for s in symbols if s not in set(missing_symbols)]
        log(f"{len(symbols)} render symbols; {len(missing_symbols)} carry uncovered entries (fetched first)")

    args.out_root.mkdir(parents=True, exist_ok=True)
    run_info = {
        "kind": "bybit_render_1m_fetch",
        "started_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "window": [args.start, args.end],
        "symbols": len(priority),
        "source": "bybit v5 market/kline category=linear interval=1",
    }
    (args.out_root / "fetch_run.json").write_text(json.dumps(run_info, indent=1) + "\n", encoding="utf-8")

    done = 0
    failed: list[str] = []
    lock = threading.Lock()

    def worker(symbol: str) -> None:
        nonlocal done
        try:
            state = fetch_symbol(args.out_root, symbol, start_ms, end_ms, min_interval)
            with lock:
                done += 1
                log(
                    f"[{done}/{len(priority)}] {symbol}: days={state.get('days_written')} "
                    f"bars={state.get('bars_written')} empty_pages={state.get('empty_pages', '?')}"
                )
        except Exception as error:  # noqa: BLE001 - record and continue with other symbols
            with lock:
                failed.append(symbol)
                log(f"FAILED {symbol}: {error}")

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(worker, priority))

    summary = {
        "finished_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "symbols_done": done,
        "symbols_failed": failed,
    }
    (args.out_root / "fetch_summary.json").write_text(
        json.dumps(summary, indent=1) + "\n", encoding="utf-8"
    )
    log(json.dumps(summary))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
