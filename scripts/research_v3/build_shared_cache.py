#!/usr/bin/env python3
"""Build and validate the shared V3 research caches (funding panel, kline slice).

Validation gates every downstream V3 analysis:
- the funding panel must reproduce every modeled ledger trade's funding exactly;
- the reconstructed daily curves must match barebones_curve.parquet per day.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/build_shared_cache.py [--out-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import common  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    out_dir = common.REPORT_ROOT / "shared" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger()
    symbols = set(ledger["symbol"].to_list())
    print(f"ledger ok: {ledger.height} trades, {len(symbols)} symbols", flush=True)

    funding_path = out_dir / "funding_events.parquet"
    funding, funding_sha = common.cached_parquet(
        funding_path,
        lambda: common.read_funding_events(args.data_root, symbols=symbols),
    )
    print(f"funding events: {funding.shape} sha={funding_sha[:12]}", flush=True)

    kline_path = out_dir / "kline_slice_1h.parquet"
    klines, kline_sha = common.cached_parquet(
        kline_path,
        lambda: common.read_kline_slice(args.data_root, symbols=symbols),
    )
    print(f"kline slice: {klines.shape} sha={kline_sha[:12]}", flush=True)

    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    crosscheck = common.crosscheck_ledger_funding(ledger, series)
    print(f"funding cross-check ok on modeled trades; {crosscheck}", flush=True)

    contributions = common.trade_daily_contributions(ledger, series, bars)
    official = pl.read_parquet(common.CURVE_PATH)
    worst_by_sleeve: dict[str, float] = {}
    for sleeve in ("long", "continuous"):
        official_part = official.filter(pl.col("sleeve") == sleeve).sort("ts_ms")
        start_day = int(official_part["ts_ms"].min())
        end_day = int(official_part["ts_ms"].max())
        rebuilt = common.daily_curve(
            contributions, sleeve=sleeve, start_day_ms=start_day, end_day_ms=end_day
        )
        joined = official_part.join(rebuilt, left_on="ts_ms", right_on="day_ms", suffix="_rebuilt")
        if joined.height != official_part.height:
            raise RuntimeError(f"{sleeve}: rebuilt curve day count differs")
        worst = 0.0
        for column in ("gross_return", "cost_return", "funding_return", "net_return", "equity", "drawdown"):
            diff = float(
                joined.select((pl.col(column) - pl.col(f"{column}_rebuilt")).abs().max()).item()
            )
            worst = max(worst, diff)
        worst_by_sleeve[sleeve] = worst
        print(f"{sleeve}: rebuilt vs official curve max abs diff {worst:.3e}", flush=True)
        if worst > 1e-9:
            raise RuntimeError(f"{sleeve} curve reconstruction diverges: {worst}")

    manifest = common.write_manifest(
        out_dir,
        kind="strategy_research_v3_shared_cache",
        inputs={
            "v2": v2_identity,
            "data_root": str(args.data_root),
            "funding_window": [common.FUNDING_START.isoformat(), common.FUNDING_END_EXCLUSIVE.isoformat()],
            "kline_window": [common.KLINE_START.isoformat(), common.KLINE_END_EXCLUSIVE.isoformat()],
        },
        params={"symbols": len(symbols)},
        output_files={"funding_events.parquet": funding_path, "kline_slice_1h.parquet": kline_path},
        extra={
            "validation": {
                "funding_crosscheck": crosscheck,
                "curve_reconstruction_max_abs_diff": worst_by_sleeve,
            }
        },
    )
    print(json.dumps({"manifest_payload_sha256": manifest["manifest_payload_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
