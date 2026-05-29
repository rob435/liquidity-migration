"""Fetch Binance USD-M futures METRICS from data.binance.vision (EXPLORATORY).

The metrics archive (data/futures/um/daily/metrics/<SYM>/<SYM>-metrics-<DATE>.zip,
back to 2020-09, 5-min granularity) carries the positioning data missing from the
binance_full_pit_strategy root: open interest, top-trader long/short ratio,
global (retail) long/short ratio, taker buy/sell volume ratio.

Aggregates each day to its end-of-day snapshot (last 5-min row = positioning at
the daily signal close, PIT-correct) and writes one parquet per symbol to
<root>/binance_usdm_metrics/<SYMBOL>.parquet with columns:
  ts_ms (day-end), date, symbol, oi, oi_value, toptrader_lsr, global_lsr, taker_lsr

Usage:
  .venv/bin/python scripts/fetch_binance_metrics.py --start 2020-09-01 [--symbols A,B] [--workers 24]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liquidity_migration.binance_vision import VISION_FILES, _s3_keys  # noqa: E402

ROOT = Path.home() / "SHARED_DATA" / "binance_full_pit_strategy" / "binance_usdm_metrics"
PREFIX = "data/futures/um/daily/metrics/{sym}/"


def _day_end_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) + 86_400_000  # next-midnight, matches funding/OI join convention


def list_keys(symbol: str, start_date: str) -> list[str]:
    prefix = PREFIX.format(sym=symbol)
    floor = f"{prefix}{symbol}-metrics-{start_date}.zip"
    return sorted(k for k in _s3_keys(prefix) if k.endswith(".zip") and k >= floor)


def fetch_day(key: str, retries: int = 4) -> dict | None:
    """Download one daily metrics zip, return the END-OF-DAY snapshot row, or None."""
    url = f"{VISION_FILES}/{key}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                raw = resp.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                name = zf.namelist()[0]
                text = zf.read(name).decode("utf-8", "replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if len(lines) < 2:
                return None
            last = lines[-1].split(",")  # end-of-day 5-min snapshot
            date_str = last[0][:10]
            return {
                "ts_ms": _day_end_ms(date_str),
                "date": date_str,
                "symbol": last[1],
                "oi": float(last[2]),
                "oi_value": float(last[3]),
                "toptrader_lsr": float(last[5]),   # sum_toptrader_long_short_ratio (position-weighted)
                "global_lsr": float(last[6]),       # count_long_short_ratio (retail account ratio)
                "taker_lsr": float(last[7]),        # sum_taker_long_short_vol_ratio
            }
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.0 + attempt)
    return None


def fetch_symbol(symbol: str, start_date: str, workers: int, skip_existing: bool) -> tuple[str, int]:
    out = ROOT / f"{symbol}.parquet"
    if skip_existing and out.exists():
        return symbol, -1
    keys = list_keys(symbol, start_date)
    if not keys:
        return symbol, 0
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(fetch_day, keys):
            if r is not None:
                rows.append(r)
    if not rows:
        return symbol, 0
    df = pl.DataFrame(rows).unique(subset=["ts_ms"]).sort("ts_ms")
    ROOT.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    return symbol, df.height


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2020-09-01")
    ap.add_argument("--symbols", default="", help="Comma-separated; default = keys of configs/long_sector_map.json")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    if args.symbols.strip():
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        smap = json.loads((Path(__file__).resolve().parent.parent / "configs" / "long_sector_map.json").read_text())
        symbols = sorted(smap.keys())
    print(f"[metrics] {len(symbols)} symbols from {args.start}, {args.workers} workers -> {ROOT}", flush=True)
    t0 = time.perf_counter()
    for i, sym in enumerate(symbols, 1):
        try:
            s, n = fetch_symbol(sym, args.start, args.workers, args.skip_existing)
            tag = "skip" if n == -1 else f"{n} days"
            print(f"[{i}/{len(symbols)}] {s}: {tag}  ({time.perf_counter()-t0:.0f}s elapsed)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(symbols)}] {sym}: ERROR {type(e).__name__}: {e}", flush=True)
    print(f"[metrics] done in {time.perf_counter()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
