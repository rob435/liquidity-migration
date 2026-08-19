#!/usr/bin/env python3
"""Maintain the daily Binance USDM metrics store (open interest, top-trader
long/short ratio, taker flow) under ``~/SHARED_DATA/binance_metrics_raw``,
collapsed to one row per symbol-day in
``~/SHARED_DATA/binance_metrics_daily/_all_daily.parquet``.

Source: the public ``data.binance.vision`` archive's
``futures/um/daily/metrics/`` files — 5-minute rows, immutable once
published, delisted symbols preserved. A symbol-day's file publishes late
morning UTC the following day, the same cadence as the other Binance
dailies the refresh already waits for.

Two jobs, both idempotent:

* fetch the last ``--days`` calendar days for every currently listed USDM
  perp (absences near listings/delistings are expected and recorded);
* re-collapse any symbol whose raw file count changed into its per-symbol
  parquet, then rewrite the combined store.

History note: the store was seeded 2026-08-19 with episode windows around
every deep-funding name since 2022-04-15 (the metrics archive's first
day), not full history for every symbol. Consumers treat a missing
symbol-day as null, and the carry rule's whale multiplier fails OPEN on
null, so sparse history thins the feature rather than biasing it.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import polars as pl

RAW = Path.home() / "SHARED_DATA" / "binance_metrics_raw"
DAILY = Path.home() / "SHARED_DATA" / "binance_metrics_daily"
STORE = DAILY / "_all_daily.parquet"

WANT = [
    "create_time", "sum_open_interest", "sum_open_interest_value",
    "sum_toptrader_long_short_ratio", "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
]


def listed_symbols() -> list[str]:
    with urllib.request.urlopen(
        "https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=60
    ) as r:
        info = json.load(r)
    return sorted(
        s["symbol"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    )


def fetch_one(job: tuple[str, str]) -> str:
    sym, date = job
    dest = RAW / sym / f"{sym}-metrics-{date}.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    url = (
        "https://data.binance.vision/data/futures/um/daily/metrics/"
        f"{sym}/{sym}-metrics-{date}.zip"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            dest.write_bytes(r.read())
        return "ok"
    except urllib.error.HTTPError as e:
        return "absent" if e.code == 404 else f"fail:{e.code}"
    except Exception:  # noqa: BLE001 - one lost day heals on the next run
        return "fail:ERR"


def collapse_symbol(sym_dir: Path) -> pl.DataFrame | None:
    rows = []
    for zp in sorted(sym_dir.glob("*.zip")):
        date = zp.stem.rsplit("-metrics-", 1)[-1]
        try:
            with zipfile.ZipFile(zp) as z:
                df = pl.read_csv(
                    io.BytesIO(z.read(z.namelist()[0])),
                    null_values=["", "null", "NULL"],
                    schema_overrides={c: pl.Float64 for c in WANT if c != "create_time"},
                )
        except Exception:  # noqa: BLE001 - a corrupt zip loses one day
            continue
        if "sum_open_interest" not in df.columns or df.height == 0:
            continue
        oi = df["sum_open_interest"].drop_nulls()
        if not oi.len():
            continue

        def last(c: str):
            if c not in df.columns:
                return None
            s = df[c].drop_nulls()
            return s.tail(1).item() if s.len() else None

        def mean(c: str):
            if c not in df.columns:
                return None
            s = df[c].drop_nulls()
            return s.mean() if s.len() else None

        rows.append({
            "symbol": sym_dir.name,
            "date": date,
            "oi_eod": oi.tail(1).item(),
            "oi_chg_1d": (
                oi.tail(1).item() / oi.head(1).item() - 1 if oi.head(1).item() else None
            ),
            "oi_val_eod": last("sum_open_interest_value"),
            "tt_ls_eod": last("sum_toptrader_long_short_ratio"),
            "acct_ls_eod": last("count_long_short_ratio"),
            "taker_ratio_mean": mean("sum_taker_long_short_vol_ratio"),
            "n_rows": df.height,
        })
    return pl.DataFrame(rows) if rows else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days", type=int, default=3,
        help="fetch this many most-recent complete UTC days (default 3, heals gaps)",
    )
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args(argv)

    today = dt.datetime.now(dt.UTC).date()
    dates = [(today - dt.timedelta(days=n)).isoformat() for n in range(1, args.days + 1)]
    symbols = listed_symbols()
    jobs = [(s, d) for s in symbols for d in dates]
    counts: dict[str, int] = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(fetch_one, jobs):
            counts[res] = counts.get(res, 0) + 1
    print(f"fetch {len(symbols)} symbols x {dates}: {counts}")
    if any(k.startswith("fail") for k in counts):
        print("fetch failures above; continuing with what landed", file=sys.stderr)

    DAILY.mkdir(parents=True, exist_ok=True)
    changed = 0
    for sym_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
        n_zips = len(list(sym_dir.glob("*.zip")))
        marker = DAILY / f"{sym_dir.name}.count"
        cache = DAILY / f"{sym_dir.name}.parquet"
        if cache.exists() and marker.exists() and marker.read_text() == str(n_zips):
            continue
        df = collapse_symbol(sym_dir)
        if df is not None:
            df.write_parquet(cache)
        marker.write_text(str(n_zips))
        changed += 1
    parts = [
        pl.read_parquet(f)
        for f in sorted(DAILY.glob("*.parquet"))
        if f.name != STORE.name
    ]
    store = pl.concat(parts)
    store.write_parquet(STORE)
    print(f"collapsed {changed} changed symbols; store {store.height:,} symbol-days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
