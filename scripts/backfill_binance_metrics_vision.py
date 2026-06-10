#!/usr/bin/env python3
"""Backfill Binance UM `metrics` (5-min OI + taker/top-trader ratios) from
data.binance.vision daily archives into the research root.

Charter §8-P1 (docs/research_plan_alpha_hunt_2026-06-10.md): the PIT-clean
liquidation-PROXY data layer. Also satisfies the pre-registered ridge-rerun
precondition (Binance OI backfill). Survivorship-free: the archive serves
delisted symbols; the symbol set is everything with kline coverage in the root.

Mechanics (resume-safe):
  1. symbol set = kline coverage spans in <root>/klines_1h, clipped to --start;
  2. per symbol, daily zips data/futures/um/daily/metrics/{S}/{S}-metrics-{d}.zip
     (404 = no-metrics day, normal for pre-listing/delisted tails);
  3. one parquet per symbol under <root>/binance_usdm_metrics_5m/ — symbols with
     an existing parquet are SKIPPED, so re-runs resume;
  4. _manifest.json records per-symbol row counts + 404 days for the audit.

    POLARS_MAX_THREADS=4 .venv/bin/python scripts/backfill_binance_metrics_vision.py \
        [--root ~/SHARED_DATA/binance_full_pit] [--start 2023-01-01] [--workers 12] \
        [--symbols AAA,BBB] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

VISION = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
NUM_COLS = [
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
]


def _kline_spans(root: Path) -> dict[str, tuple[str, str]]:
    spans: dict[str, list[str]] = {}
    kroot = root / "klines_1h"
    for ddir in sorted(p.name for p in kroot.iterdir() if p.name.startswith("date=")):
        d = ddir.split("=", 1)[1]
        for sdir in (kroot / ddir).iterdir():
            if sdir.name.startswith("symbol="):
                s = sdir.name.split("=", 1)[1]
                if s not in spans:
                    spans[s] = [d, d]
                else:
                    spans[s][1] = d
    return {s: (a, b) for s, (a, b) in spans.items()}


def _days(first: str, last: str) -> list[str]:
    d0 = date.fromisoformat(first)
    d1 = date.fromisoformat(last)
    out = []
    d = d0
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _fetch(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "liqmig-metrics-backfill"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code >= 500 and attempt < 3:  # transient archive 5xx: back off and retry
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return b"__RETRY__"
    return b"__RETRY__"


def _day_rows(symbol: str, day: str) -> list[dict] | None:
    blob = _fetch(VISION.format(s=symbol, d=day))
    if blob == b"__RETRY__":
        blob = _fetch(VISION.format(s=symbol, d=day), timeout=60)
        if blob == b"__RETRY__":
            return None
    if not blob:
        return None
    rows: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for r in reader:
                try:
                    out = {"create_time": r.get("create_time"), "symbol": symbol}
                    for c in NUM_COLS:
                        v = r.get(c)
                        out[c] = float(v) if v not in (None, "") else None
                    rows.append(out)
                except ValueError:
                    continue
    except zipfile.BadZipFile:
        return None
    return rows


def backfill_symbol(symbol: str, days: list[str], out_dir: Path, workers: int) -> dict:
    rows: list[dict] = []
    miss = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_day_rows, symbol, d): d for d in days}
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                miss += 1
            else:
                rows.extend(r)
    if rows:
        schema = {"create_time": pl.String, "symbol": pl.String, **{c: pl.Float64 for c in NUM_COLS}}
        df = pl.DataFrame(rows, schema=schema).with_columns(
            pl.col("create_time").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)
            .dt.replace_time_zone("UTC").dt.epoch("ms").alias("ts_ms")
        ).drop("create_time").drop_nulls("ts_ms").unique(["symbol", "ts_ms"]).sort("ts_ms")
        df.write_parquet(out_dir / f"{symbol}.parquet")
        return {"symbol": symbol, "rows": df.height, "days_404": miss}
    (out_dir / f"{symbol}.parquet").touch()  # empty marker: nothing available
    return {"symbol": symbol, "rows": 0, "days_404": miss}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "binance_full_pit"))
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--symbols", help="comma list (smoke tests)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N symbols (0 = all)")
    args = ap.parse_args()
    root = Path(args.root).expanduser()
    out_dir = root / "binance_usdm_metrics_5m"
    out_dir.mkdir(exist_ok=True)
    manifest_path = out_dir / "_manifest.json"
    manifest: dict = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    spans = _kline_spans(root)
    if args.symbols:
        wanted = [s.strip() for s in args.symbols.split(",") if s.strip()]
        spans = {s: spans[s] for s in wanted if s in spans}
    todo = []
    for s, (a, b) in sorted(spans.items()):
        if (out_dir / f"{s}.parquet").exists():
            continue
        first = max(a, args.start)
        if first > b:
            continue
        todo.append((s, first, b))
    if args.limit:
        todo = todo[: args.limit]
    total_days = sum(len(_days(a, b)) for _s, a, b in todo)
    print(f"symbols todo: {len(todo)} (of {len(spans)} with klines)  symbol-days: {total_days:,}", flush=True)

    for i, (s, a, b) in enumerate(todo, 1):
        st = backfill_symbol(s, _days(a, b), out_dir, args.workers)
        manifest[s] = st
        if i % 10 == 0 or i == len(todo):
            manifest_path.write_text(json.dumps(manifest, indent=0))
            done_rows = sum(v["rows"] for v in manifest.values())
            print(f"  [{i}/{len(todo)}] {s}: rows={st['rows']:,} 404s={st['days_404']}  (total rows {done_rows:,})", flush=True)
    manifest_path.write_text(json.dumps(manifest, indent=0))
    rows = sum(v["rows"] for v in manifest.values())
    print(f"DONE: {len(manifest)} symbols, {rows:,} rows in {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
