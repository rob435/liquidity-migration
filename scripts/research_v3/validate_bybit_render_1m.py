#!/usr/bin/env python3
"""Validate the bybit_render_1m research root against the render books.

Checks, written as a validation receipt into the root:
1. entry coverage — every render-book entry (both arms) has 1m bars for its
   entry bar's minutes ((entry_ts - 60m, entry_ts]);
2. 1m -> 1h reconstruction — for a seeded sample of covered (symbol, day)
   pairs, hourly OHLC aggregated from the 1m bars must match the verified
   render 1h kline cache exactly (prices) on fully-covered hours;
3. inventory — partitions, rows, date span.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/validate_bybit_render_1m.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from scripts.research_v3 import v4_shared  # noqa: E402

DEFAULT_ROOT = Path.home() / "SHARED_DATA" / "bybit_render_1m"
SAMPLE_DAYS = 60
SAMPLE_SEED = 20260720
# Verified upstream purges: the v5 API serves NO klines (any interval) for
# these (symbol, entry day) pairs; the render's 1h bars predate the purge.
# Backfillable from the public.bybit.com tick archive if a thesis needs them.
KNOWN_UPSTREAM_GAPS = {("DATAUSDT", "2024-08-20")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    kline_root = args.root / "klines_1m"

    # 1. Entry coverage for both arms.
    entries: list[tuple[str, str, int]] = []
    for arm in ("gate_on", "gate_off"):
        book = v4_shared.load_render_book(arm)
        for trade in book.unique(subset=["symbol", "entry_ts_ms"]).iter_rows(named=True):
            entries.append((str(trade["symbol"]), str(trade["entry_date"]), int(trade["entry_ts_ms"])))
    entries = sorted(set(entries))
    missing_partition = 0
    missing_entry_bar = 0
    known_gaps = 0
    checked = 0
    for symbol, entry_date, entry_ts in entries:
        # The entry bar is (entry_ts - 1h, entry_ts]; for entries at 00:00 UTC
        # its minutes live in the PREVIOUS day's partition, so read both.
        prev = (dt.date.fromisoformat(entry_date) - dt.timedelta(days=1)).isoformat()
        frames = []
        for day in (entry_date, prev):
            part = kline_root / f"date={day}" / f"symbol={symbol}" / "bars.parquet"
            if part.is_file():
                frames.append(pl.read_parquet(part, columns=["ts_ms"]))
        if not frames:
            if (symbol, entry_date) in KNOWN_UPSTREAM_GAPS:
                known_gaps += 1
            else:
                missing_partition += 1
            continue
        frame = pl.concat(frames, how="vertical")
        bar_minutes = frame.filter(
            (pl.col("ts_ms") > entry_ts - MS_PER_HOUR) & (pl.col("ts_ms") <= entry_ts - 60_000)
        )
        if bar_minutes.is_empty():
            missing_entry_bar += 1
        checked += 1

    # 2. 1m -> 1h reconstruction against the verified render 1h cache.
    render_1h = pl.read_parquet(v4_shared.RENDER_SHARED_DIR / "render_kline_slice_1h.parquet")
    rng = random.Random(SAMPLE_SEED)
    sample = rng.sample(entries, min(SAMPLE_DAYS, len(entries)))
    worst_price_rel = 0.0
    hours_compared = 0
    hours_incomplete = 0
    for symbol, entry_date, _entry_ts in sample:
        part = kline_root / f"date={entry_date}" / f"symbol={symbol}" / "bars.parquet"
        if not part.is_file():
            continue
        m1 = pl.read_parquet(part)
        hourly = (
            m1.with_columns(((pl.col("ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("hour"))
            .group_by("hour")
            .agg(
                pl.len().alias("n"),
                pl.col("open").sort_by("ts_ms").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").sort_by("ts_ms").last().alias("close"),
            )
        )
        reference = render_1h.filter(
            (pl.col("symbol") == symbol)
            & (pl.col("ts_ms").is_in(hourly["hour"].to_list()))
        ).select("ts_ms", "open", "high", "low", "close")
        joined = hourly.join(reference, left_on="hour", right_on="ts_ms", how="inner", suffix="_ref")
        complete = joined.filter(pl.col("n") == 60)
        hours_incomplete += joined.height - complete.height
        if complete.is_empty():
            continue
        for column in ("open", "high", "low", "close"):
            diff = complete.select(
                ((pl.col(column) - pl.col(f"{column}_ref")).abs() / pl.col(f"{column}_ref")).max()
            ).item()
            worst_price_rel = max(worst_price_rel, float(diff or 0.0))
        hours_compared += complete.height

    # 3. Inventory.
    dates = sorted(p.name.removeprefix("date=") for p in kline_root.iterdir() if p.is_dir())
    n_partitions = sum(1 for _ in kline_root.rglob("bars.parquet"))

    receipt = {
        "kind": "bybit_render_1m_validation",
        "created_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "entries_checked": checked,
        "entries_total": len(entries),
        "entries_missing_partition": missing_partition,
        "entries_missing_entry_bar": missing_entry_bar,
        "entries_known_upstream_gaps": known_gaps,
        "known_upstream_gaps": sorted(KNOWN_UPSTREAM_GAPS),
        "reconstruction": {
            "sampled_entry_days": len(sample),
            "hours_compared_complete": hours_compared,
            "hours_incomplete_1m_coverage": hours_incomplete,
            "worst_price_rel_diff_vs_1h_cache": worst_price_rel,
        },
        "inventory": {
            "day_partitions": len(dates),
            "first_day": dates[0] if dates else None,
            "last_day": dates[-1] if dates else None,
            "symbol_day_partitions": n_partitions,
        },
    }
    (args.root / "validation_receipt.json").write_text(
        json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, indent=1, sort_keys=True))
    ok = (
        missing_partition == 0
        and missing_entry_bar == 0
        and worst_price_rel < 1e-9
        and hours_compared > 0
    )
    print(f"validation {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
