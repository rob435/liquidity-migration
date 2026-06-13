"""Event-anchored Binance UM `metrics` backfill (P10 binance arm, data layer only).

The full-history layer (`scripts/backfill_binance_metrics_vision.py`, 670
symbols / 112M rows) lives on the original research box. The P10 taker-flow
scout needs only [t0−26h, t0] per winner_base event, so this wrapper reuses
the full backfiller's fetch/parse and writes the SAME per-symbol parquet
layout into `binance_usdm_metrics_5m/`, restricted to event-window days.

CAVEAT (recorded in `_manifest_eventanchored.json`): coverage is
EVENT-ANCHORED, not full-history. Absent rows mean "not downloaded", never
"no data". If a full backfill is ever wanted on this box, delete the
event-anchored parquets first — the full backfiller's resume logic skips
symbols with an existing parquet.

    POLARS_MAX_THREADS=4 PYTHONPATH=. .venv/bin/python \
        scripts/backfill_binance_metrics_eventanchored.py [--workers 12]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import backfill_binance_metrics_vision as full  # noqa: E402
import continuous_ensemble_rebalance_scout as scout  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
OUT_DIR = SHARED / "binance_full_pit" / "binance_usdm_metrics_5m"
MANIFEST = OUT_DIR / "_manifest_eventanchored.json"
COMP_PRIORITY = ["turn4p5", "turn3p3", "turn4p3", "age210tp14"]
MS_H = 3_600_000
LOOKBACK_H = 26  # scout windows reach t0−25h; one hour of slack


def load_binance_events() -> pl.DataFrame:
    frames = []
    for rank, src in enumerate(COMP_PRIORITY):
        spec = scout.SOURCES[src]
        t = pl.read_csv(
            spec.root / "binance" / spec.cell / "continuous_trades.csv",
            columns=["symbol", "entry_signal_ts_ms"],
        ).with_columns(pl.lit(rank).alias("prio"))
        frames.append(t)
    return (
        pl.concat(frames)
        .sort("prio")
        .unique(subset=["symbol", "entry_signal_ts_ms"], keep="first")
        .drop("prio")
    )


def needed_days_by_symbol(events: pl.DataFrame) -> dict[str, list[str]]:
    need: dict[str, set[str]] = {}
    for sym, t0 in events.select("symbol", "entry_signal_ts_ms").iter_rows():
        d0 = dt.datetime.fromtimestamp((int(t0) - LOOKBACK_H * MS_H) / 1000, tz=dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp(int(t0) / 1000, tz=dt.timezone.utc).date()
        s = need.setdefault(str(sym), set())
        d = d0
        while d <= d1:
            s.add(d.isoformat())
            d += dt.timedelta(days=1)
    return {s: sorted(v) for s, v in need.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    events = load_binance_events()
    plan = needed_days_by_symbol(events)
    total_days = sum(len(v) for v in plan.values())
    print(f"{events.height} events -> {len(plan)} symbols / {total_days} symbol-days", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text())
    done = 0
    for sym, days in sorted(plan.items()):
        done += 1
        prior = manifest.get(sym, {})
        missing = [d for d in days if d not in set(prior.get("days_done", []))]
        if not missing and (OUT_DIR / f"{sym}.parquet").exists():
            continue
        res = full.backfill_symbol(sym, sorted(set(days)), OUT_DIR, args.workers)
        manifest[sym] = {
            "scope": "event_anchored",
            "days_done": sorted(set(days)),
            "rows": res["rows"],
            "days_404": res["days_404"],
        }
        MANIFEST.write_text(json.dumps(manifest, indent=2))
        if done % 25 == 0 or res["rows"] == 0:
            print(f"  [{done}/{len(plan)}] {sym}: rows={res['rows']} 404s={res['days_404']}", flush=True)
    print(f"done: {len(plan)} symbols; manifest {MANIFEST}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
