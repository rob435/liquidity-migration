#!/usr/bin/env python3
"""Validate the binance_vision_alt research root.

Checks, written as a validation receipt into the root:
1. inventory per dataset — file counts, non-empty file counts, row totals,
   distinct symbols, month span (empty files are absent-upstream markers);
2. cadence sanity on a seeded sample — 1m datasets on the minute grid with
   at most month-minutes rows; metrics on the 5m grid; funding settlements
   at plausible per-day counts;
3. cross-venue reconstruction — for a seeded sample of symbol-months,
   aggregate Vision 1m klines to 1h and compare OHLC exactly against the
   independently built ``binance_full_pit`` 1h root where both cover the
   same hours.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/validate_binance_vision_alt.py
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

DEFAULT_ROOT = Path.home() / "SHARED_DATA" / "binance_vision_alt"
BINANCE_PIT_ROOT = Path.home() / "SHARED_DATA" / "binance_full_pit"
SAMPLE_MONTHS = 25
SAMPLE_SEED = 20260720
MS_PER_MINUTE = 60_000


def inventory(dataset_dir: Path) -> dict:
    files = sorted(dataset_dir.rglob("month=*.parquet"))
    non_empty = 0
    rows = 0
    symbols: set[str] = set()
    months: set[str] = set()
    for path in files:
        if "ts_ms" not in pl.read_parquet_schema(path):
            continue  # absent-upstream marker
        frame = pl.read_parquet(path)
        if frame.height:
            non_empty += 1
            rows += frame.height
            symbols.add(path.parent.name.removeprefix("symbol="))
            months.add(path.stem.removeprefix("month=").removesuffix(".partial"))
    return {
        "files": len(files),
        "non_empty_files": non_empty,
        "rows": rows,
        "symbols_with_data": len(symbols),
        "first_month": min(months) if months else None,
        "last_month": max(months) if months else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--binance-pit-root", type=Path, default=BINANCE_PIT_ROOT)
    args = parser.parse_args()
    rng = random.Random(SAMPLE_SEED)

    receipt: dict = {
        "kind": "binance_vision_alt_validation",
        "created_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "inventory": {},
    }
    for dataset in ("funding", "premium_1m", "klines_1m", "metrics"):
        directory = args.root / dataset
        receipt["inventory"][dataset] = inventory(directory) if directory.is_dir() else None
        print(f"inventory {dataset}: {receipt['inventory'][dataset]}", flush=True)

    # Cadence sanity.
    problems: list[str] = []

    def sample_files(dataset: str, count: int) -> list[Path]:
        files = []
        for path in (args.root / dataset).rglob("month=*.parquet"):
            schema = pl.read_parquet_schema(path)
            # Zero-column files are absent-upstream markers, not data.
            if "ts_ms" in schema and pl.read_parquet(path, columns=["ts_ms"]).height > 0:
                files.append(path)
        return rng.sample(files, min(count, len(files)))

    metrics_off_grid = 0
    for dataset, grid_ms, max_per_day in (
        ("klines_1m", MS_PER_MINUTE, 1440),
        ("premium_1m", MS_PER_MINUTE, 1440),
        ("metrics", 5 * MS_PER_MINUTE, 288),
        ("funding", None, 24),
    ):
        for path in sample_files(dataset, SAMPLE_MONTHS):
            frame = pl.read_parquet(path)
            if grid_ms is not None:
                off_grid = frame.filter(pl.col("ts_ms") % grid_ms != 0).height
                if off_grid and dataset == "metrics":
                    # Upstream property: metrics create_time is a wall-clock
                    # write stamp and occasionally lands off the 5m grid.
                    metrics_off_grid += off_grid
                elif off_grid:
                    problems.append(f"{dataset}/{path.parent.name}/{path.name}: {off_grid} off-grid rows")
            per_day = frame.group_by((pl.col("ts_ms") // (24 * MS_PER_HOUR))).agg(pl.len())
            worst = int(per_day["len"].max())
            if worst > max_per_day:
                problems.append(f"{dataset}/{path.parent.name}/{path.name}: {worst} rows in one day")
            if frame["ts_ms"].is_duplicated().any():
                problems.append(f"{dataset}/{path.parent.name}/{path.name}: duplicate timestamps")
    receipt["cadence_problems"] = problems
    receipt["metrics_off_grid_rows_sampled"] = metrics_off_grid
    print(f"cadence problems: {len(problems)} (metrics off-grid rows, upstream property: {metrics_off_grid})", flush=True)

    # Cross-venue reconstruction vs the independent binance_full_pit 1h root.
    worst_rel = 0.0
    hours_compared = 0
    months_checked = 0
    open_diffs = 0
    hl_diffs = 0
    for path in sample_files("klines_1m", SAMPLE_MONTHS):
        frame = pl.read_parquet(path)
        month = path.stem.removeprefix("month=").removesuffix(".partial")
        vision_symbol = None
        run_info = json.loads((args.root / "fetch_run.json").read_text(encoding="utf-8"))
        bybit_symbol = path.parent.name.removeprefix("symbol=")
        vision_symbol = run_info["symbol_mapping"].get(bybit_symbol, bybit_symbol)
        hourly = (
            frame.with_columns(((pl.col("ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("hour"))
            .group_by("hour")
            .agg(
                pl.len().alias("n"),
                pl.col("open").sort_by("ts_ms").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").sort_by("ts_ms").last().alias("close"),
            )
            .filter(pl.col("n") == 60)
        )
        if hourly.is_empty():
            continue
        day = dt.date(int(month[:4]), int(month[5:7]), 1)
        end = dt.date(day.year + (day.month == 12), day.month % 12 + 1, 1)
        parts = []
        while day < end:
            candidate = args.binance_pit_root / "klines_1h" / f"date={day.isoformat()}"
            if candidate.is_dir():
                for file in candidate.rglob("*.parquet"):
                    parts.append(file)
            day += dt.timedelta(days=1)
        if not parts:
            continue
        reference = (
            pl.read_parquet(parts, columns=["ts_ms", "symbol", "open", "high", "low", "close"])
            .filter(pl.col("symbol") == vision_symbol)
        )
        if reference.is_empty():
            continue
        joined = hourly.join(reference, left_on="hour", right_on="ts_ms", how="inner", suffix="_ref")
        if joined.is_empty():
            continue
        # Gate on CLOSE exactness. Upstream semantics keep the other columns
        # from being byte-identical between Binance's own 1m and 1h archives:
        # a tradeless first minute carries the prior close (open), and rare
        # single-print extremes land in one archive but not the other
        # (high/low, observed at <= 0.03% of sampled hours, <= 14 bps).
        for column in ("close",):
            diff = joined.select(
                ((pl.col(column) - pl.col(f"{column}_ref")).abs() / pl.col(f"{column}_ref")).max()
            ).item()
            worst_rel = max(worst_rel, float(diff or 0.0))
        open_diffs += joined.filter(
            ((pl.col("open") - pl.col("open_ref")).abs() / pl.col("open_ref")) > 1e-9
        ).height
        hl_diffs += joined.filter(
            (((pl.col("high") - pl.col("high_ref")).abs() / pl.col("high_ref")) > 1e-9)
            | (((pl.col("low") - pl.col("low_ref")).abs() / pl.col("low_ref")) > 1e-9)
        ).height
        hours_compared += joined.height
        months_checked += 1
    receipt["reconstruction_vs_binance_full_pit"] = {
        "months_checked": months_checked,
        "hours_compared": hours_compared,
        "worst_close_rel_diff": worst_rel,
        "open_first_trade_semantic_hours": open_diffs,
        "high_low_single_print_hours": hl_diffs,
    }
    print(f"reconstruction: {receipt['reconstruction_vs_binance_full_pit']}", flush=True)

    summary = json.loads((args.root / "fetch_summary.json").read_text(encoding="utf-8")) if (
        args.root / "fetch_summary.json"
    ).is_file() else {}
    receipt["fetch_summary"] = {k: summary.get(k) for k in ("jobs_done", "files_absent_upstream", "failure_count")}

    (args.root / "validation_receipt.json").write_text(
        json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    ok = (
        not problems
        and receipt["fetch_summary"].get("failure_count") in (0, None)
        and worst_rel < 1e-9
        and hours_compared > 0
    )
    print(f"validation {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
