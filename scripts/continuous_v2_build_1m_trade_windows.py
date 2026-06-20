#!/usr/bin/env python3
"""Wave 1: targeted trade-window 1m PIT cache for the continuous v2 next-level program.

Receipt: docs/preregistration/2026-06-20-continuous-v2-1m-data-foundation-construction.md

Builds 1m klines ONLY for the (symbol, date) partitions the Phase 0 trades touch
([entry_date-1 .. exit_date]); see coverage_needed_<venue>.parquet. This is a
trade-window cache, deliberately NOT a dense full-universe root (see the receipt's
scoping decision). It lives under its own cache root so it can never be mistaken
for a full klines_1m.

Sources (both reachable + checksum-valid from this box):
- Bybit: public.bybit.com trade archives -> ingestion.aggregate_trade_klines_1m
  -> densify_trade_klines_1m (full 1440-minute grid, carry-forward close).
- Binance: data.binance.vision daily 1m zips, sha256-gated via binance_vision
  helpers, parsed with parse_month_csv (same CSV shape at 1m).

Integrity: missing partitions (404 / delisted / pre-listing / archive gap) are
LEDGERED, never silently filled; Binance bodies are sha256-checksum-validated;
zero-volume (no-trade) minutes are counted per partition so density is auditable.
Resumable (skips partitions already written). Label: exploratory (data only).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from liquidity_migration.archive import read_public_trade_archive  # noqa: E402
from liquidity_migration.binance_vision import (  # noqa: E402
    VISION_FILES,
    _fetch_expected_sha256,
    _verify_download,
    parse_month_csv,
)
from liquidity_migration.ingestion import (  # noqa: E402
    aggregate_trade_klines_1m,
    densify_trade_klines_1m,
)

CACHE = Path.home() / "SHARED_DATA" / "continuous_v2_1m"
BYBIT_TRADE_URL = "https://public.bybit.com/trading/{sym}/{sym}{date}.csv.gz"
BINANCE_1M_URL = VISION_FILES + "/data/futures/um/daily/klines/{sym}/1m/{sym}-1m-{date}.zip"
OUT_COLS = ["ts_ms", "symbol", "open", "high", "low", "close", "volume_base", "turnover_quote", "source"]
USER_AGENT = "liqmig-research/1.0 (continuous-v2 1m trade-window build)"


def _http_get(url: str, *, timeout: int = 60, retries: int = 4) -> tuple[bytes, str | None]:
    """GET with retry/backoff. Raises HTTPError(404) immediately (permanent)."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - public archive
                return r.read(), r.getheader("Content-Length")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e  # 429/5xx transient
        except Exception as e:  # noqa: BLE001 - transient network
            last = e
        time.sleep(0.5 * (attempt + 1))
    raise last if last is not None else RuntimeError(f"unreachable: {url}")


def _zero_vol_minutes(df: pl.DataFrame) -> int:
    if df.is_empty() or "volume_base" not in df.columns:
        return 0
    return int(df.filter(pl.col("volume_base") <= 0.0).height)


def build_bybit_day(sym: str, date: str) -> tuple[pl.DataFrame | None, str]:
    url = BYBIT_TRADE_URL.format(sym=sym, date=date)
    try:
        raw, _ = _http_get(url)
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"net_error:{type(e).__name__}"
    with tempfile.NamedTemporaryFile(suffix=".csv.gz", delete=False) as tf:
        tf.write(raw)
        tmp = Path(tf.name)
    try:
        # sanity: the body must be a valid gzip (a truncated body is a transient corrupt)
        try:
            gzip.decompress(raw)
        except OSError:
            return None, "corrupt_gzip"
        trades = read_public_trade_archive(tmp, symbol=sym)
        if trades.is_empty():
            return None, "empty_trades"
        bars = aggregate_trade_klines_1m(trades)
        if bars.is_empty():
            return None, "empty_bars"
        dense = densify_trade_klines_1m(bars, archive_date=date)
        return dense.select(OUT_COLS), "ok"
    finally:
        tmp.unlink(missing_ok=True)


def build_binance_day(sym: str, date: str) -> tuple[pl.DataFrame | None, str]:
    url = BINANCE_1M_URL.format(sym=sym, date=date)
    try:
        raw, clen = _http_get(url)
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"net_error:{type(e).__name__}"
    expected = _fetch_expected_sha256(url)
    try:
        _verify_download(raw, expected, int(clen) if clen is not None else None)
    except ValueError:
        return None, "checksum_fail"
    rows = parse_month_csv(sym, raw)
    if not rows:
        return None, "empty"
    df = pl.DataFrame(rows).with_columns(pl.lit("binance_vision_um_1m").alias("source"))
    for c in OUT_COLS:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None).alias(c))
    return df.select(OUT_COLS).sort("ts_ms"), "ok"


BUILDERS = {"bybit": build_bybit_day, "binance": build_binance_day}


def _partition_path(venue: str, sym: str, date: str) -> Path:
    return CACHE / venue / "klines_1m" / f"date={date}" / f"symbol={sym}" / "data.parquet"


def _one(venue: str, sym: str, date: str, resume: bool) -> dict:
    out = _partition_path(venue, sym, date)
    if resume and out.exists():
        try:
            n = pl.scan_parquet(out).select(pl.len()).collect().item()
        except Exception:  # noqa: BLE001
            n = -1
        return {"venue": venue, "symbol": sym, "date": date, "status": "cached", "rows": n, "zero_vol_minutes": None}
    df, status = BUILDERS[venue](sym, date)
    if df is None:
        return {"venue": venue, "symbol": sym, "date": date, "status": status, "rows": 0, "zero_vol_minutes": None}
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    return {
        "venue": venue, "symbol": sym, "date": date, "status": "ok",
        "rows": int(df.height), "zero_vol_minutes": _zero_vol_minutes(df),
    }


def run_venue(venue: str, *, resume: bool, workers: int, limit: int | None) -> list[dict]:
    man = pl.read_parquet(CACHE / f"coverage_needed_{venue}.parquet")
    parts = list(man.iter_rows(named=True))
    if limit:
        parts = parts[:limit]
    total = len(parts)
    print(f"[{venue}] {total} partitions (workers={workers}, resume={resume})", flush=True)
    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, venue, p["symbol"], p["date"], resume): p for p in parts}
        for fut in as_completed(futs):
            rows.append(fut.result())
            done += 1
            if done % 200 == 0 or done == total:
                ok = sum(1 for r in rows if r["status"] in ("ok", "cached"))
                miss = sum(1 for r in rows if r["status"].startswith("http_404"))
                print(f"[{venue}] {done}/{total}  ok/cached={ok}  missing404={miss}", flush=True)
    return rows


def write_audit(all_rows: list[dict], date_tag: str) -> Path:
    adir = CACHE / f"audit_{date_tag}"
    adir.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(all_rows)
    df = df.with_columns((pl.lit(1440) - pl.col("zero_vol_minutes")).alias("traded_minutes"))
    df.sort(["venue", "symbol", "date"]).write_csv(adir / "coverage_by_symbol_date.csv")
    # gap minutes (zero-volume) for built partitions
    df.filter(pl.col("status").is_in(["ok", "cached"])).select(
        ["venue", "symbol", "date", "rows", "zero_vol_minutes", "traded_minutes"]
    ).write_csv(adir / "gap_minutes.csv")
    # missing partitions (no source data) — ledgered, never filled
    df.filter(~pl.col("status").is_in(["ok", "cached"])).select(
        ["venue", "symbol", "date", "status"]
    ).write_csv(adir / "missing_partitions.csv")
    # per-venue / symbol pass-fail summary
    summary = (
        df.group_by("venue")
        .agg(
            pl.len().alias("partitions"),
            pl.col("status").is_in(["ok", "cached"]).sum().alias("built"),
            (pl.col("status") == "ok").sum().alias("newly_built"),
            pl.col("status").str.starts_with("http_404").sum().alias("missing_404"),
            (pl.col("status") == "checksum_fail").sum().alias("checksum_fail"),
            pl.col("rows").filter(pl.col("status").is_in(["ok", "cached"])).sum().alias("rows_total"),
        )
        .sort("venue")
    )
    summary.write_csv(adir / "venue_summary.csv")
    (adir / "source_identity.json").write_text(json.dumps({
        "program": "continuous_v2_next_level", "wave": "1_1m_trade_windows",
        "built_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "bybit_url_template": BYBIT_TRADE_URL, "binance_url_template": BINANCE_1M_URL,
        "binance_integrity": "sha256 .CHECKSUM gate (binance_vision._verify_download)",
        "bybit_pipeline": "read_public_trade_archive -> aggregate_trade_klines_1m -> densify_trade_klines_1m",
        "cache_root": str(CACHE), "label": "exploratory",
    }, indent=2))
    return adir, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="smoke: only first N partitions/venue")
    ap.add_argument("--date-tag", default=dt.date.today().isoformat())
    args = ap.parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    all_rows: list[dict] = []
    for v in venues:
        all_rows += run_venue(v, resume=args.resume, workers=args.workers, limit=args.limit)
    adir, summary = write_audit(all_rows, args.date_tag)
    print("=== venue summary ===", flush=True)
    for r in summary.to_dicts():
        print("  " + json.dumps(r), flush=True)
    print(f"audit dir: {adir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
