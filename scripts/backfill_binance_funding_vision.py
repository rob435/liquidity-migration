#!/usr/bin/env python3
"""Rebuild binance_usdm_funding from data.binance.vision monthly fundingRate archives.

Basis: docs/research_summary.md / docs/data_roots.md. Fixes the
51-symbol coverage hole (296/307 baseline short trades booked zero funding) AND the
funding_interval_min=480 hardcode (the vision CSVs carry the TRUE per-settlement
funding_interval_hours). Survivorship-free: the archive serves delisted symbols.

Mechanics:
  1. symbol set = every symbol with kline coverage in the root (PIT universe);
  2. per symbol, monthly zips spanning its kline date range (404 = no-perp-funding
     month, recorded + skipped); current partial month topped up from fapi REST;
  3. zips cached under <root>/_funding_rebuild_cache (idempotent re-runs);
  4. the OLD dataset dir is moved to binance_usdm_funding.pre_rebuild_<date>.bak
     (instant rollback), then the new frame is written fresh via storage.write_dataset;
  5. prints the post-rebuild coverage audit (symbols, rows, modal intervals).

    POLARS_MAX_THREADS=4 .venv/bin/python scripts/backfill_binance_funding_vision.py \
        [--root ~/SHARED_DATA/binance_full_pit] [--workers 8] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.storage import write_dataset  # noqa: E402

VISION = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip"
FAPI = "https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&startTime={st}&limit=1000"


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


def _months(first: str, last: str) -> list[str]:
    y0, m0 = int(first[:4]), int(first[5:7])
    y1, m1 = int(last[:4]), int(last[5:7])
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _fetch(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "liqmig-funding-rebuild"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _month_rows(symbol: str, month: str, cache: Path) -> list[dict]:
    cpath = cache / f"{symbol}-{month}.zip"
    if cpath.exists():
        blob = cpath.read_bytes() or None
    else:
        blob = _fetch(VISION.format(s=symbol, m=month))
        cpath.write_bytes(blob or b"")  # empty file = cached 404
    if not blob:
        return []
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        with zf.open(zf.namelist()[0]) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for r in reader:
                try:
                    rows.append({
                        "ts_ms": int(float(r["calc_time"])),
                        "symbol": symbol,
                        "funding_rate": float(r["last_funding_rate"]),
                        "mark_price": float("nan"),
                        "funding_interval_min": int(float(r["funding_interval_hours"]) * 60)
                        if r.get("funding_interval_hours") not in (None, "", "0") else 480,
                        "source": "binance_usdm_funding_vision",
                    })
                except (KeyError, ValueError):
                    continue
    return rows


def _fapi_topup(symbol: str, since_ms: int) -> list[dict]:
    blob = _fetch(FAPI.format(s=symbol, st=since_ms + 1))
    if not blob:
        return []
    rows = []
    for r in json.loads(blob):
        try:
            rows.append({
                "ts_ms": int(r["fundingTime"]),
                "symbol": symbol,
                "funding_rate": float(r["fundingRate"]),
                "mark_price": float("nan"),
                "funding_interval_min": 480,  # REST omits the interval; P&L path ignores it
                "source": "binance_usdm_funding_fapi",
            })
        except (KeyError, ValueError):
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / "SHARED_DATA" / "binance_full_pit"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="download+report only; do not swap the dataset")
    args = ap.parse_args()
    root = Path(args.root).expanduser()
    cache = root / "_funding_rebuild_cache"
    cache.mkdir(exist_ok=True)

    spans = _kline_spans(root)
    print(f"symbols with kline coverage: {len(spans)}", flush=True)

    jobs = [(s, m) for s, (a, b) in spans.items() for m in _months(a, b)]
    print(f"symbol-months to fetch: {len(jobs)}", flush=True)

    all_rows: list[dict] = []
    missing_months = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_month_rows, s, m, cache): (s, m) for s, m in jobs}
        done = 0
        for fut in as_completed(futs):
            rows = fut.result()
            if not rows:
                missing_months += 1
            all_rows.extend(rows)
            done += 1
            if done % 2000 == 0:
                print(f"  {done}/{len(jobs)} months  rows={len(all_rows):,}", flush=True)

    # current-month top-up from REST for symbols whose kline span reaches this month
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    last_by_symbol: dict[str, int] = {}
    for r in all_rows:
        if r["ts_ms"] > last_by_symbol.get(r["symbol"], 0):
            last_by_symbol[r["symbol"]] = r["ts_ms"]
    topup_syms = [s for s, (a, b) in spans.items() if b[:7] >= this_month]
    print(f"fapi top-up for {len(topup_syms)} active symbols ...", flush=True)
    with ThreadPoolExecutor(max_workers=4) as ex:  # be gentle with fapi rate limits
        futs = {ex.submit(_fapi_topup, s, last_by_symbol.get(s, 0)): s for s in topup_syms}
        for fut in as_completed(futs):
            all_rows.extend(fut.result())

    df = pl.DataFrame(all_rows).unique(["symbol", "ts_ms"], keep="first").sort(["symbol", "ts_ms"])
    print(f"total rows: {df.height:,}  symbols: {df['symbol'].n_unique()}  "
          f"(archive months 404'd: {missing_months})", flush=True)

    if args.dry_run:
        print("[dry-run] not swapping the dataset")
        return 0

    old = root / "binance_usdm_funding"
    bak = root / f"binance_usdm_funding.pre_rebuild_{datetime.now(timezone.utc).date().isoformat()}.bak"
    if old.exists():
        if bak.exists():
            raise SystemExit(f"backup already exists: {bak} — resolve manually first")
        old.rename(bak)
        print(f"old dataset -> {bak}", flush=True)
    write_dataset(df, root, "binance_usdm_funding", append=False)
    print("rebuilt dataset written.", flush=True)

    # post-rebuild audit
    d = (df.sort(["symbol", "ts_ms"])
           .with_columns((pl.col("ts_ms").diff().over("symbol") / 3_600_000).alias("gap_h"))
           .drop_nulls("gap_h"))
    modal = d.group_by("symbol").agg(pl.col("gap_h").mode().first().alias("modal"))
    print("modal settlement interval distribution (symbols):")
    print(modal.group_by("modal").agg(pl.len()).sort("modal"))
    print("stored funding_interval_min distribution:")
    print(df["funding_interval_min"].value_counts().sort("funding_interval_min"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
