#!/usr/bin/env python3
"""Backfill Binance UM 5m klines (full PIT universe) from data.binance.vision.

Execution-research substrate (slice schedules, participation/volume curves,
intra-hour path features). Survivorship-free: the vision archive serves
delisted symbols; the symbol set is everything with kline coverage in the
root (same PIT membership as klines_1h). Data layer only — no signal claim,
no receipt needed until a measurement/test is proposed.

Mechanics (resume-safe):
  1. symbol set + spans from <root>/klines_1h, clipped to --start;
  2. per symbol, monthly zips data/futures/um/monthly/klines/{S}/5m/
     {S}-5m-{YYYY-MM}.zip (404 = no-data month, normal at listing/delisting
     edges; the current partial month is intentionally absent — top up later
     via the daily files if execution work ever needs the live tail);
  3. ONE parquet per symbol at <root>/klines_5m/{S}.parquet (monthly-per-symbol
     packing — ~12x the 1h rows but compact, unlike the tiny-file 1h layout);
     kept columns include taker_buy_quote so this layer doubles as a
     full-universe 5m signed-flow source;
  4. _manifest.json records months done / rows / 404s per symbol; symbols with
     all wanted months present are SKIPPED, so re-runs resume.

    POLARS_MAX_THREADS=4 PYTHONPATH=. .venv/bin/python \
        scripts/backfill_binance_klines5m_vision.py \
        [--root ~/SHARED_DATA/binance_full_pit] [--start 2023-01-01] \
        [--workers 12] [--symbols AAA,BBB] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))

import polars as pl  # noqa: E402

from backfill_binance_metrics_vision import _fetch, _kline_spans  # noqa: E402

VISION = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/5m/{s}-5m-{m}.zip"
COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
KEEP = {
    "open_time": "ts_ms", "open": "open", "high": "high", "low": "low",
    "close": "close", "volume": "volume", "quote_volume": "turnover_quote",
    "count": "trade_count", "taker_buy_quote_volume": "taker_buy_quote",
}


def _months(first: str, last: str) -> list[str]:
    d0, d1 = date.fromisoformat(first).replace(day=1), date.fromisoformat(last).replace(day=1)
    out = []
    d = d0
    while d <= d1:
        out.append(f"{d.year:04d}-{d.month:02d}")
        d = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return out


def _month_rows(symbol: str, month: str) -> list[dict] | None:
    blob = _fetch(VISION.format(s=symbol, m=month), timeout=60)
    if blob == b"__RETRY__":
        blob = _fetch(VISION.format(s=symbol, m=month), timeout=120)
        if blob == b"__RETRY__":
            return None
    if not blob:
        return None
    rows: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf, zf.open(zf.namelist()[0]) as f:
            text = io.TextIOWrapper(f, encoding="utf-8")
            first = text.readline()
            if not first:
                return []
            has_header = first.split(",")[0].strip() == "open_time"
            lines = text if has_header else [first, *text]
            reader = csv.reader(lines)
            for raw in reader:
                if len(raw) < len(COLS) - 1:
                    continue
                try:
                    ts = int(float(raw[0]))
                    if ts > 10**15:  # microsecond stamps in newer archives
                        ts //= 1000
                    rows.append({
                        "ts_ms": ts,
                        "open": float(raw[1]), "high": float(raw[2]),
                        "low": float(raw[3]), "close": float(raw[4]),
                        "volume": float(raw[5]), "turnover_quote": float(raw[7]),
                        "trade_count": float(raw[8]),
                        "taker_buy_quote": float(raw[10]),
                    })
                except (ValueError, IndexError):
                    continue
    except zipfile.BadZipFile:
        return None
    return rows


def backfill_symbol(symbol: str, months: list[str], out_dir: Path, workers: int) -> dict:
    rows: list[dict] = []
    miss: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_month_rows, symbol, m): m for m in months}
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                miss.append(futs[fut])
            else:
                rows.extend(r)
    if rows:
        df = (
            pl.DataFrame(rows)
            .with_columns(pl.lit(symbol).alias("symbol"))
            .unique(["symbol", "ts_ms"])
            .sort("ts_ms")
        )
        df.write_parquet(out_dir / f"{symbol}.parquet", compression="zstd")
        return {"symbol": symbol, "rows": df.height, "months_404": sorted(miss)}
    (out_dir / f"{symbol}.parquet").touch()
    return {"symbol": symbol, "rows": 0, "months_404": sorted(miss)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "binance_full_pit"))
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--symbols", help="comma list (smoke tests)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N symbols (0 = all)")
    args = ap.parse_args()
    root = Path(args.root).expanduser()
    out_dir = root / "klines_5m"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "_manifest.json"
    manifest: dict[str, dict] = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    spans = _kline_spans(root)
    if args.symbols:
        wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
        spans = {s: v for s, v in spans.items() if s in wanted}
    plan: dict[str, list[str]] = {}
    for sym, (first, last) in sorted(spans.items()):
        first = max(first, args.start)
        if first > last:
            continue
        plan[sym] = _months(first, last)
    total = sum(len(v) for v in plan.values())
    print(f"{len(plan)} symbols / {total} symbol-months (start clip {args.start})", flush=True)

    done = 0
    for sym, months in plan.items():
        if args.limit and done >= args.limit:
            break
        prior = manifest.get(sym, {})
        if (out_dir / f"{sym}.parquet").exists() and set(months) <= set(prior.get("months_wanted", [])):
            continue
        res = backfill_symbol(sym, months, out_dir, args.workers)
        manifest[sym] = {**res, "months_wanted": months}
        manifest_path.write_text(json.dumps(manifest, indent=2))
        done += 1
        if done % 20 == 0 or res["rows"] == 0:
            print(f"  [{done}/{len(plan)}] {sym}: rows={res['rows']} 404s={len(res['months_404'])}", flush=True)
    print(f"done: {done} symbols processed; manifest {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
