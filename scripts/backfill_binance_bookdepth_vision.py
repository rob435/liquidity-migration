#!/usr/bin/env python3
"""Backfill Binance UM `bookDepth` (order-book depth bands) hourly-aggregated.

Charter §8-P9 (recorded spec): raw bookDepth is ~12B rows (1-min × ±1..5% bands);
this ingests the HOURLY aggregate per band — mean and last notional/depth per
(symbol, hour, percentage) — ~210M→~20M rows total. Data layer for execution-cost
realism (P2) + deletion-risk insurance (Binance has deleted Vision datasets before).
No signal claim; no pre-registration required until a signal test is proposed.

Resume-safe per-symbol parquets under <root>/binance_usdm_bookdepth_1h/ + manifest.

    POLARS_MAX_THREADS=4 .venv/bin/python scripts/backfill_binance_bookdepth_vision.py \
        [--root ~/SHARED_DATA/binance_full_pit] [--start 2023-01-01] [--workers 10] \
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

VISION = "https://data.binance.vision/data/futures/um/daily/bookDepth/{s}/{s}-bookDepth-{d}.zip"


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
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    out = []
    d = d0
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _fetch(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "liqmig-bookdepth-backfill"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code >= 500 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return None
    return None


def _day_rows(symbol: str, day: str) -> list[dict] | None:
    blob = _fetch(VISION.format(s=symbol, d=day))
    if not blob:
        return None
    agg: dict[tuple[str, str], list] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for r in reader:
                try:
                    ts = r["timestamp"]            # "YYYY-MM-DD HH:MM:SS"
                    hour = ts[:13]                 # "YYYY-MM-DD HH"
                    pct = r["percentage"]
                    depth = float(r["depth"])
                    notional = float(r["notional"])
                except (KeyError, ValueError):
                    continue
                key = (hour, pct)
                a = agg.get(key)
                if a is None:
                    agg[key] = [depth, notional, depth, notional, 1]  # sum_d, sum_n, last_d, last_n, n
                else:
                    a[0] += depth
                    a[1] += notional
                    a[2] = depth
                    a[3] = notional
                    a[4] += 1
    except zipfile.BadZipFile:
        return None
    rows = []
    for (hour, pct), (sd, sn, ld, ln, n) in agg.items():
        rows.append({"symbol": symbol, "hour": hour, "percentage": pct,
                     "depth_mean": sd / n, "notional_mean": sn / n,
                     "depth_last": ld, "notional_last": ln, "n_snaps": n})
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
        schema = {"symbol": pl.String, "hour": pl.String, "percentage": pl.String,
                  "depth_mean": pl.Float64, "notional_mean": pl.Float64,
                  "depth_last": pl.Float64, "notional_last": pl.Float64, "n_snaps": pl.Int64}
        df = pl.DataFrame(rows, schema=schema).with_columns(
            (pl.col("hour") + ":00:00").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)
            .dt.replace_time_zone("UTC").dt.epoch("ms").alias("ts_ms")
        ).drop_nulls("ts_ms").sort(["ts_ms", "percentage"])
        df.write_parquet(out_dir / f"{symbol}.parquet")
        return {"symbol": symbol, "rows": df.height, "days_404": miss}
    (out_dir / f"{symbol}.parquet").touch()
    return {"symbol": symbol, "rows": 0, "days_404": miss}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "binance_full_pit"))
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--symbols")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", help="i/N: process every N-th symbol starting at i (per-shard manifest)")
    args = ap.parse_args()
    root = Path(args.root).expanduser()
    out_dir = root / "binance_usdm_bookdepth_1h"
    out_dir.mkdir(exist_ok=True)
    shard_i, shard_n = (0, 1)
    if args.shard:
        shard_i, shard_n = (int(x) for x in args.shard.split("/"))
    manifest_path = out_dir / (f"_manifest_{shard_i}.json" if args.shard else "_manifest.json")
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
    if shard_n > 1:
        todo = [t for j, t in enumerate(todo) if j % shard_n == shard_i]
    if args.limit:
        todo = todo[: args.limit]
    print(f"symbols todo: {len(todo)} (of {len(spans)}; shard {shard_i}/{shard_n})", flush=True)

    for i, (s, a, b) in enumerate(todo, 1):
        st = backfill_symbol(s, _days(a, b), out_dir, args.workers)
        manifest[s] = st
        if i % 10 == 0 or i == len(todo):
            manifest_path.write_text(json.dumps(manifest, indent=0))
            done_rows = sum(v["rows"] for v in manifest.values())
            print(f"  [{i}/{len(todo)}] {s}: rows={st['rows']:,} 404s={st['days_404']}  (total {done_rows:,})", flush=True)
    manifest_path.write_text(json.dumps(manifest, indent=0))
    print(f"DONE: {len(manifest)} symbols in {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
