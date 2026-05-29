"""Fetch Bybit long/short account ratio (cross-venue positioning) — EXPLORATORY.

Bybit v5 /market/account-ratio serves daily global account long/short ratio
(buyRatio/sellRatio) multi-year via windowed queries (confirmed back to 2021).
This is the venue-matched equivalent of Binance's retail `global_lsr`
(count_long_short_ratio), enabling the retail-contrarian factor cross-venue.

Writes one parquet per symbol to <root>/positioning_lsr/<SYMBOL>.parquet with the
SAME schema as binance_usdm_metrics (oi/oi_value/toptrader_lsr/taker_lsr are null
on Bybit — only the global account L/S ratio is exposed by this endpoint):
  ts_ms (day-end), date, symbol, oi, oi_value, toptrader_lsr, global_lsr, taker_lsr

Usage: .venv/bin/python scripts/fetch_bybit_lsr.py [--symbols A,B] [--workers 8]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit" / "positioning_lsr"
API = "https://api.bybit.com/v5/market/account-ratio"
MS_DAY = 86_400_000


def _windows(start_year: int = 2020) -> list[tuple[int, int]]:
    out = []
    for y in range(start_year, datetime.now(timezone.utc).year + 1):
        s = int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        e = int(datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        out.append((s, e))
    return out


def _get(symbol: str, start: int, end: int, retries: int = 4) -> list[dict]:
    url = f"{API}?category=linear&symbol={symbol}&period=1d&startTime={start}&endTime={end}&limit=500"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                d = json.load(r)
            return d.get("result", {}).get("list", []) or []
        except Exception:
            if attempt == retries - 1:
                return []
            time.sleep(1.0 + attempt)
    return []


def fetch_symbol(symbol: str, skip_existing: bool) -> tuple[str, int]:
    out = ROOT / f"{symbol}.parquet"
    if skip_existing and out.exists():
        return symbol, -1
    rows: dict[int, float] = {}
    for s, e in _windows():
        for rec in _get(symbol, s, e):
            try:
                buy, sell = float(rec["buyRatio"]), float(rec["sellRatio"])
                if sell <= 0:
                    continue
                ts = int(rec["timestamp"])
                rows[ts] = buy / sell  # long/short account ratio, matches Binance global_lsr
            except (KeyError, ValueError, ZeroDivisionError):
                continue
    if not rows:
        return symbol, 0
    recs = []
    for ts, lsr in sorted(rows.items()):
        date = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        recs.append({
            "ts_ms": ts + MS_DAY,  # day-end convention, matches funding/OI/metrics joins
            "date": date,
            "symbol": symbol,
            "oi": None,
            "oi_value": None,
            "toptrader_lsr": None,
            "global_lsr": lsr,
            "taker_lsr": None,
        })
    df = pl.DataFrame(recs, schema_overrides={
        "oi": pl.Float64, "oi_value": pl.Float64, "toptrader_lsr": pl.Float64, "taker_lsr": pl.Float64,
    })
    ROOT.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    return symbol, df.height


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()
    if args.symbols.strip():
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        smap = json.loads((Path(__file__).resolve().parent.parent / "configs" / "long_sector_map.json").read_text())
        symbols = sorted(smap.keys())
    print(f"[bybit-lsr] {len(symbols)} symbols, {args.workers} workers -> {ROOT}", flush=True)
    t0 = time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for sym, n in ex.map(lambda s: fetch_symbol(s, args.skip_existing), symbols):
            done += 1
            tag = "skip" if n == -1 else f"{n} days"
            print(f"[{done}/{len(symbols)}] {sym}: {tag}", flush=True)
    print(f"[bybit-lsr] done in {time.perf_counter()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
