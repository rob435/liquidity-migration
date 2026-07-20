#!/usr/bin/env python3
"""T-L companion: listing-week execution-cost reality read (Lane-1).

Reads 1m bars from ``bybit_render_1m`` for the T-L listing events (d0 >=
2023-03-27, inside the 1m window) and reports execution-cost primitives for
the listing week (d1..d7) against a same-symbol mature baseline (age 60..66
days). No fitted cost model -- primitives only, next to the frozen 45 bp
round-trip hurdle:

- rel_range_1m: (high-low)/close per 1m bar (bid-ask bounce + intrabar vol);
  median and p75 per day.
- sigma_1m_bp: std of 1m log returns, in bp (execution-window drift risk).
- amihud_bp_per_kusd: mean(|1m return| / (1m quote turnover/1000 USD)), in bp
  (participation impact per 1,000 USD).
- zero_vol_minutes: minutes with zero volume (gap illiquidity).

1m/1h feed divergence at listing edges is a known artifact: any event day
whose last 1m close differs from the full-PIT 1h last close by more than 50
bp is quarantined from the cost read and counted, not debugged.

Usage:
  .venv/Scripts/python.exe scripts/research_v3/tl_listing_execution_cost.py \
      [--out-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import common  # noqa: E402
from scripts.research_v3.tl_listing_conditional import (  # noqa: E402
    daily_bar,
    symbol_day_frame,
)

RENDER_1M_ROOT = Path.home() / "SHARED_DATA" / "bybit_render_1m"
FULL_PIT_ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit"
FIRST_1M_D0 = dt.date(2023, 3, 27)
LAST_1M_DAY = dt.date(2026, 7, 9)
LISTING_DAYS = tuple(range(1, 8))
MATURE_DAYS = tuple(range(60, 67))
DIVERGENCE_BP = 50.0


def day_metrics(frame: pl.DataFrame) -> dict[str, float] | None:
    valid = frame.filter(
        pl.all_horizontal(
            [
                pl.col(c).is_not_null() & pl.col(c).is_finite() & (pl.col(c) > 0.0)
                for c in ("open", "high", "low", "close")
            ]
        )
    ).sort("ts_ms")
    if valid.height < 30:
        return None
    close = valid["close"].to_numpy()
    high = valid["high"].to_numpy()
    low = valid["low"].to_numpy()
    volume = valid["volume"].to_numpy()
    turnover = valid["turnover"].to_numpy()
    rel_range = (high - low) / close
    log_ret = np.diff(np.log(close))
    with_turnover = turnover > 0
    amihud = (
        float(np.mean(np.abs(log_ret[with_turnover[1:]]) / (turnover[1:][with_turnover[1:]] / 1000.0)) * 1e4)
        if with_turnover[1:].any()
        else None
    )
    return {
        "n_1m_bars": float(valid.height),
        "rel_range_med_bp": float(np.median(rel_range) * 1e4),
        "rel_range_p75_bp": float(np.percentile(rel_range, 75) * 1e4),
        "sigma_1m_bp": float(np.std(log_ret, ddof=1) * 1e4) if len(log_ret) > 2 else float("nan"),
        "amihud_bp_per_kusd": amihud if amihud is not None else float("nan"),
        "zero_vol_minutes": float((volume <= 0).sum()),
        "day_turnover_kusd": float(turnover.sum() / 1000.0),
        "last_1m_close": float(close[-1]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    study_dir = common.REPORT_ROOT / "t-l" / args.out_date
    population_path = study_dir / "population.csv"
    if not population_path.is_file():
        raise RuntimeError(f"run tl_listing_conditional.py first: missing {population_path}")
    population = pl.read_csv(population_path)

    events = [
        (str(r["symbol"]), dt.date.fromisoformat(str(r["d0"])), str(r["era"]))
        for r in population.iter_rows(named=True)
        if dt.date.fromisoformat(str(r["d0"])) >= FIRST_1M_D0
    ]
    print(f"events in 1m window: {len(events)} / {population.height}", flush=True)

    rows: list[dict[str, Any]] = []
    counters = {
        "symbol_not_in_render_universe": 0,
        "divergent_1m_vs_1h_days": 0,
        "missing_1m_days": 0,
        "mature_window_unavailable": 0,
    }
    for symbol, d0, era in events:
        probe = RENDER_1M_ROOT / "klines_1m" / f"date={d0.isoformat()}" / f"symbol={symbol}"
        if not probe.is_dir():
            counters["symbol_not_in_render_universe"] += 1
            continue
        mature_possible = d0 + dt.timedelta(days=MATURE_DAYS[-1]) <= LAST_1M_DAY
        if not mature_possible:
            counters["mature_window_unavailable"] += 1
        for bucket, offsets in (("listing_d1_d7", LISTING_DAYS), ("mature_d60_d66", MATURE_DAYS)):
            if bucket == "mature_d60_d66" and not mature_possible:
                continue
            for offset in offsets:
                day = d0 + dt.timedelta(days=offset)
                frame = symbol_day_frame(RENDER_1M_ROOT, "klines_1m", symbol, day)
                if frame is None:
                    counters["missing_1m_days"] += 1
                    continue
                metrics = day_metrics(frame)
                if metrics is None:
                    counters["missing_1m_days"] += 1
                    continue
                hour_frame = symbol_day_frame(FULL_PIT_ROOT, "klines_1h", symbol, day)
                if hour_frame is not None:
                    hour_bar = daily_bar(hour_frame)
                    if hour_bar is not None:
                        div_bp = abs(metrics["last_1m_close"] / hour_bar["close"] - 1.0) * 1e4
                        if div_bp > DIVERGENCE_BP:
                            counters["divergent_1m_vs_1h_days"] += 1
                            continue
                rows.append(
                    {
                        "symbol": symbol,
                        "d0": d0.isoformat(),
                        "era": era,
                        "bucket": bucket,
                        "day_offset": offset,
                        **{k: v for k, v in metrics.items() if k != "last_1m_close"},
                    }
                )

    per_day = pl.DataFrame(rows)
    metric_cols = [
        "rel_range_med_bp",
        "rel_range_p75_bp",
        "sigma_1m_bp",
        "amihud_bp_per_kusd",
        "zero_vol_minutes",
        "day_turnover_kusd",
    ]
    summary = (
        per_day.filter(pl.all_horizontal([pl.col(c).is_finite() for c in ("rel_range_med_bp", "sigma_1m_bp")]))
        .group_by(["bucket", "era"])
        .agg(
            pl.len().alias("n_symbol_days"),
            pl.col("symbol").n_unique().alias("n_symbols"),
            *[pl.col(c).median().alias(f"{c}_med") for c in metric_cols],
            *[pl.col(c).quantile(0.75).alias(f"{c}_p75") for c in metric_cols],
        )
        .sort(["bucket", "era"])
    )

    paired = (
        per_day.group_by(["symbol", "bucket"])
        .agg(pl.col("rel_range_med_bp").median().alias("rel_range"))
        .pivot(values="rel_range", index="symbol", on="bucket")
    )
    if "listing_d1_d7" in paired.columns and "mature_d60_d66" in paired.columns:
        both = paired.drop_nulls()
        ratio = (
            float((both["listing_d1_d7"] / both["mature_d60_d66"]).median()) if both.height else math.nan
        )
    else:
        both = paired.clear()
        ratio = math.nan

    out_paths: dict[str, Path] = {}
    for name, frame in (
        ("execution_cost_per_day.csv", per_day),
        ("execution_cost_summary.csv", summary),
    ):
        path = study_dir / name
        frame.write_csv(path)
        out_paths[name] = path

    common.write_manifest(
        study_dir / "execution-cost",
        kind="tl_listing_execution_cost",
        inputs={
            "population": str(population_path),
            "render_1m_root": str(RENDER_1M_ROOT),
            "full_pit_root": str(FULL_PIT_ROOT),
        },
        params={
            "first_1m_d0": FIRST_1M_D0.isoformat(),
            "listing_days": list(LISTING_DAYS),
            "mature_days": list(MATURE_DAYS),
            "divergence_quarantine_bp": DIVERGENCE_BP,
            "min_1m_bars_per_day": 30,
        },
        output_files=out_paths,
        extra={
            "data_root": str(RENDER_1M_ROOT),
            "study": "t-l-execution-cost",
            "counters": counters,
            "paired_symbols": int(both.height),
            "paired_listing_over_mature_rel_range_ratio_median": ratio,
        },
    )
    print(f"counters {counters}", flush=True)
    print(f"paired listing/mature rel-range ratio (median over {both.height} symbols): {ratio}", flush=True)
    print(f"wrote {study_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
