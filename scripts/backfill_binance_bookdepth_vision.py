#!/usr/bin/env python3
"""Backfill Binance UM `bookDepth` (order-book depth bands) hourly-aggregated.

Charter §8-P9 (recorded spec): raw bookDepth is ~12B rows (1-min × ±1..5% bands);
this ingests the HOURLY aggregate per band — mean and last notional/depth per
(symbol, hour, percentage) — ~210M→~20M rows total. Data layer for execution-cost
realism (P2) + deletion-risk insurance (Binance has deleted Vision datasets before).
No signal claim; no pre-registration required until a signal test is proposed.

Resume-safe per-symbol parquets under <root>/binance_usdm_bookdepth_1h/ + manifest.
A 404 day = no-bookDepth day (normal pre-listing/delisted tail); a TRANSIENT day
(exhausted retries on a 5xx/DNS/connection error) is tracked separately so an outage
is never frozen as a permanent no-data gap — the manifest "complete" flag is False
when any day failed transiently, and the resume guard re-attempts incomplete symbols
(audit backfill-writers-2; mirrors scripts/backfill_binance_metrics_vision.py).

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

# Sentinel for a TRANSIENT day failure (exhausted retries on a 5xx / DNS / connection
# error), as distinct from a genuine 404 ("no bookDepth this day", a normal pre-listing/
# delisted tail). A transient day must NOT be frozen into a permanent empty marker the
# resume guard treats as complete — that turns an outage into silent zero coverage,
# violating data_roots.md's "absence == not-downloaded, never no-data" invariant
# (audit backfill-writers-2; mirrors scripts/backfill_binance_metrics_vision.py).
_TRANSIENT_DAY = object()


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
            # audit2: exhausted retries on a transient (DNS/connection) error is NOT a
            # 404 — signal "unknown" with __RETRY__ so the caller can re-attempt rather
            # than freeze a permanent no-data gap (backfill-writers-2).
            return b"__RETRY__"
    return b"__RETRY__"


def _day_rows(symbol: str, day: str) -> list[dict] | None | object:
    """Rows for one symbol-day. Returns ``None`` for a genuine 404 / empty / bad zip
    (a real no-data day) and the ``_TRANSIENT_DAY`` sentinel when the fetch failed
    transiently (exhausted retries) — the caller must treat these differently so a
    transient outage is never frozen as a permanent no-data marker (audit backfill-writers-2)."""
    blob = _fetch(VISION.format(s=symbol, d=day))
    if blob == b"__RETRY__":
        blob = _fetch(VISION.format(s=symbol, d=day), timeout=60)
        if blob == b"__RETRY__":
            return _TRANSIENT_DAY
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
    miss = 0          # genuine 404 / no-data days
    transient = 0     # transient fetch failures (NOT no-data)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_day_rows, symbol, d): d for d in days}
        for fut in as_completed(futs):
            r = fut.result()
            if r is _TRANSIENT_DAY:
                transient += 1
            elif r is None:
                miss += 1
            else:
                rows.extend(r)
    # audit2: a symbol is COMPLETE only when every non-data day was a genuine 404; any
    # transient failure means its coverage is unknown and it must be re-runnable. The
    # resume guard in main() skips a symbol only when complete (backfill-writers-2).
    complete = transient == 0
    if rows:
        schema = {"symbol": pl.String, "hour": pl.String, "percentage": pl.String,
                  "depth_mean": pl.Float64, "notional_mean": pl.Float64,
                  "depth_last": pl.Float64, "notional_last": pl.Float64, "n_snaps": pl.Int64}
        df = pl.DataFrame(rows, schema=schema).with_columns(
            (pl.col("hour") + ":00:00").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)
            .dt.replace_time_zone("UTC").dt.epoch("ms").alias("ts_ms")
        ).drop_nulls("ts_ms").sort(["ts_ms", "percentage"])
        df.write_parquet(out_dir / f"{symbol}.parquet")
        return {"symbol": symbol, "rows": df.height, "days_404": miss,
                "transient_fail": transient, "complete": complete}
    if not complete:
        # audit2: no rows AND a transient failure: do NOT write the empty .touch()
        # marker. Freezing it would turn a transient outage into permanent zero
        # coverage the resume guard skips forever — exactly the silent-gap bug.
        return {"symbol": symbol, "rows": 0, "days_404": miss,
                "transient_fail": transient, "complete": False}
    (out_dir / f"{symbol}.parquet").touch()  # empty marker: genuinely no data on the archive
    return {"symbol": symbol, "rows": 0, "days_404": miss,
            "transient_fail": 0, "complete": True}


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
        # audit2: resume only past COMPLETE symbols. A symbol with a parquet is complete
        # unless the manifest explicitly marks it incomplete (a prior run hit a transient
        # outage and left rows-but-a-gap or no marker at all). Older manifests predating
        # the "complete" field have no such flag and are treated as complete (back-compat).
        # This is what stops a transient outage from being frozen into permanent zero
        # coverage (backfill-writers-2; mirrors backfill_binance_metrics_vision.py).
        prior = manifest.get(s)
        prior_incomplete = isinstance(prior, dict) and prior.get("complete") is False
        if (out_dir / f"{s}.parquet").exists() and not prior_incomplete:
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
