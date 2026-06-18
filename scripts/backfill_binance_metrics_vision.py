#!/usr/bin/env python3
"""Backfill Binance UM `metrics` (5-min OI + taker/top-trader ratios) from
data.binance.vision daily archives into the research root.

PIT-clean liquidation/OI proxy data layer. Current research state lives in
STATE.md and docs/research_summary.md; historical alpha-hunt plan stubs were
folded into the summary. Survivorship-free: the archive serves delisted symbols;
the symbol set is everything with kline coverage in the root.

Mechanics (resume-safe):
  1. symbol set = kline coverage spans in <root>/klines_1h, clipped to --start;
  2. per symbol, daily zips data/futures/um/daily/metrics/{S}/{S}-metrics-{d}.zip
     (404 = no-metrics day, normal for pre-listing/delisted tails);
  3. one parquet per symbol under <root>/binance_usdm_metrics_5m/ — symbols with
     a COMPLETE existing parquet are SKIPPED, so re-runs resume; an incomplete
     symbol (a prior transient outage) is re-fetched and READ-MERGED onto its
     existing rows (no clobber) so this shared canonical root is never silently
     truncated (audit backfill-writers-1/2). Each parquet carries a self-describing
     `coverage` column ('full' here vs 'event_anchored' from the event-anchored arm);
  4. _manifest.json records per-symbol row counts, 404 days, transient_fail count,
     complete flag, and coverage for the audit.

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

# Sentinel for a TRANSIENT day failure (exhausted retries on a 5xx / DNS / connection
# error), as distinct from a genuine 404 ("no metrics this day", a normal pre-listing/
# delisted tail). A transient day must NOT be frozen into a permanent empty marker the
# resume guard treats as complete — that turns an outage into silent zero coverage,
# violating data_roots.md's "absence == not-downloaded, never no-data" invariant
# (audit backfill-writers-2).
_TRANSIENT_DAY = object()

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
            # transient: archive 5xx, plus 408/429 (rate-limit / timeout). Back off and
            # retry; on exhaustion signal __RETRY__ so the day degrades to a retryable
            # transient marker instead of crashing the whole multi-symbol run
            # (audit-iter3 backlog scripts).
            if (exc.code >= 500 or exc.code in (408, 429)) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            return b"__RETRY__"
        except Exception:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return b"__RETRY__"
    return b"__RETRY__"


def _day_rows(symbol: str, day: str) -> list[dict] | None | object:
    """Rows for one symbol-day. Returns ``None`` for a genuine 404 / empty / bad zip
    (a real no-data day) and the ``_TRANSIENT_DAY`` sentinel when the fetch failed
    transiently (exhausted retries) — the caller must treat these differently so a
    transient outage is never frozen as a permanent no-data marker."""
    blob = _fetch(VISION.format(s=symbol, d=day))
    if blob == b"__RETRY__":
        blob = _fetch(VISION.format(s=symbol, d=day), timeout=60)
        if blob == b"__RETRY__":
            return _TRANSIENT_DAY
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


def _stamp_coverage(df: pl.DataFrame, coverage: str) -> pl.DataFrame:
    """Stamp a self-describing ``coverage`` column ('full' | 'event_anchored') onto a
    per-symbol frame so the parquet records its own coverage scope on disk, rather than
    relying on a side manifest that can drift from the file (audit backfill-writers-1)."""
    return df.with_columns(pl.lit(coverage, dtype=pl.String).alias("coverage"))


def _merge_with_existing(df: pl.DataFrame, path: Path) -> pl.DataFrame:
    """Concat ``df`` with the rows already on disk at ``path`` (if any), de-duplicating on
    (symbol, ts_ms) and keeping the LAST occurrence so freshly-fetched/corrected rows
    supersede stale ones. An absent file or an empty ``.touch()`` marker (a genuine
    no-data symbol from a prior pass) contributes no rows. Any pre-existing ``coverage``
    column is dropped before concat so the caller can re-stamp it uniformly; column order
    is otherwise preserved so the on-disk schema is stable (audit backfill-writers-1)."""
    if not path.exists() or path.stat().st_size == 0:
        return df
    try:
        prior = pl.read_parquet(path)
    except Exception:
        # Unreadable / non-parquet marker — treat as no prior rows rather than crashing
        # the whole symbol; the fresh rows are still written.
        return df
    if prior.height == 0:
        return df
    if "coverage" in prior.columns:
        prior = prior.drop("coverage")
    # Align prior to df's full schema: add any column df has but prior lacks (older
    # on-disk schema) as typed nulls, then order to df.columns — else the concat
    # crashes on a column-count mismatch (audit-iter3 backlog scripts).
    for c in df.columns:
        if c not in prior.columns:
            prior = prior.with_columns(pl.lit(None).cast(df.schema[c]).alias(c))
    prior = prior.select(df.columns)
    return (
        pl.concat([prior, df], how="vertical_relaxed")
        .unique(["symbol", "ts_ms"], keep="last")
        .sort("ts_ms")
    )


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
    # A symbol is COMPLETE only when every non-data day was a genuine 404; any
    # transient failure means its coverage is unknown and it must be re-runnable
    # (audit backfill-writers-2). The resume guard in main() skips a symbol only
    # when complete, so an incomplete symbol is retried on the next run.
    complete = transient == 0
    if rows:
        schema = {"create_time": pl.String, "symbol": pl.String, **{c: pl.Float64 for c in NUM_COLS}}
        df = pl.DataFrame(rows, schema=schema).with_columns(
            pl.col("create_time").str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)
            .dt.replace_time_zone("UTC").dt.epoch("ms").alias("ts_ms")
        ).drop("create_time").drop_nulls("ts_ms").unique(["symbol", "ts_ms"]).sort("ts_ms")
        # READ-MERGE the existing per-symbol parquet rather than clobbering it
        # (audit backfill-writers-1). This dir is a CANONICAL PIT root shared with the
        # event-anchored wrapper, which passes only [t0-26h, t0] window days. A bare
        # write_parquet of just the days THIS call was passed would truncate a
        # previously-full-history symbol into a present-but-sparse file that a reader
        # mistakes for "no data" rather than "overwritten". Concat + unique(symbol,ts_ms)
        # keeps prior coverage and is idempotent on overlapping days (last write wins
        # via keep="last", so a re-fetch of a corrected day supersedes the stale one).
        df = _merge_with_existing(df, out_dir / f"{symbol}.parquet")
        # Stamp self-describing coverage on disk so the parquet itself records that this
        # is FULL-history (vs the event-anchored wrapper's event_anchored), closing the
        # "manifest says X, parquet holds Y" ambiguity at the writer (backfill-writers-1).
        df = _stamp_coverage(df, "full")
        df.write_parquet(out_dir / f"{symbol}.parquet")
        return {"symbol": symbol, "rows": df.height, "days_404": miss,
                "transient_fail": transient, "complete": complete, "coverage": "full"}
    if not complete:
        # No rows AND a transient failure: do NOT write the empty .touch() marker.
        # Freezing it would turn a transient outage into permanent zero coverage,
        # which the resume guard would skip forever — exactly the silent-gap bug.
        return {"symbol": symbol, "rows": 0, "days_404": miss,
                "transient_fail": transient, "complete": False, "coverage": "full"}
    (out_dir / f"{symbol}.parquet").touch()  # empty marker: genuinely no data on the archive
    return {"symbol": symbol, "rows": 0, "days_404": miss,
            "transient_fail": 0, "complete": True, "coverage": "full"}


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
        # Resume only past COMPLETE symbols. A symbol with a parquet is complete
        # unless the manifest explicitly marks it incomplete (a prior run hit a
        # transient outage and left rows-but-a-gap or no marker at all). Older
        # manifests predating the "complete" field have no such flag and are
        # treated as complete (back-compat). This is what stops a transient outage
        # from being frozen into permanent zero coverage (audit backfill-writers-2).
        prior = manifest.get(s)
        prior_incomplete = isinstance(prior, dict) and prior.get("complete") is False
        if (out_dir / f"{s}.parquet").exists() and not prior_incomplete:
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
