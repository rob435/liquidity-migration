#!/usr/bin/env python3
"""T-G: funding-state entry conditioner on the V2 CONTINUOUS barebones ledger.

Exploratory Lane-1 counterfactual post-processing (V4 draft thesis T-G).
T-B's floor mechanism with the correct comparator: instead of comparing the
funding floor to the 12 percent TP distance (which binds almost never), the
conditioner acts on the settled rate known at entry (strictly-PIT ``prev``
convention).  Declared grid, frozen before any cell was run:

- skip entries with known_prev rate < K for K in {-0.05%, -0.1%, -0.2%} per
  interval;
- declared secondary: shrink those entries to half weight instead of skipping;
- declared combination (per K): skip only when BOTH known_prev < K AND the
  T-D mean-reversion forecast (meanrev phi=0.5, mu = trailing mean of the last
  90 settlements, min 10, else rate) predicts the cumulative 24h funding sum
  stays below K x n_settlements.

Entries with no prior settlement (missing rate) pass through at full weight
and are reported.  Double-verification arm: the same funding-state bucket
diagnostic is computed for both T-A render books from the render-window
funding events.  No capacity backfill; no alpha or promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/tg_funding_state.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import exact_duration_ms  # noqa: E402
from scripts.research_v3 import common, v4_shared  # noqa: E402

RATE_CUTS: tuple[float, ...] = (-0.0005, -0.001, -0.002)
MEANREV_PHI = 0.5
TRAILING_MEAN_WINDOW = 90
TRAILING_MEAN_MIN = 10
PLANNED_HOLD_MS = exact_duration_ms(hours=24)

# The draft's inspected funding-bucket counts on this exact ledger.
DRAFT_BUCKET_COUNTS = {"deep_neg": 1305, "neg": 2303, "zero": 10190, "pos": 2947}


def build_panel(trades: pl.DataFrame, series: common.FundingSeries) -> pl.DataFrame:
    """Per-trade PIT funding state: known_prev rate, meanrev forecast, n intervals."""
    rows: list[dict[str, Any]] = []
    for trade in trades.iter_rows(named=True):
        symbol = str(trade["symbol"])
        entry = int(trade["entry_ts_ms"])
        ts_list, rate_list = series.get(symbol, ([], []))
        lo = bisect.bisect_right(ts_list, entry)
        hi = bisect.bisect_right(ts_list, entry + PLANNED_HOLD_MS)
        n_intervals = hi - lo
        known_prev = rate_list[lo - 1] if lo > 0 else None
        if known_prev is None:
            mu = None
            forecast = None
        else:
            window = rate_list[max(0, lo - TRAILING_MEAN_WINDOW) : lo]
            mu = sum(window) / len(window) if len(window) >= TRAILING_MEAN_MIN else known_prev
            forecast = (mu + MEANREV_PHI * (known_prev - mu)) * n_intervals
        rows.append(
            {
                "trade_id": trade["trade_id"],
                "known_rate_prev": known_prev,
                "trailing_mu": mu,
                "meanrev_forecast_24h": forecast,
                "n_intervals_planned_hold": n_intervals,
            }
        )
    return trades.join(pl.from_dicts(rows, infer_schema_length=None), on="trade_id", how="left").with_columns(
        v4_shared.funding_bucket_expr()
    )


def cell_weight_expr(cell: str) -> pl.Expr:
    rate = pl.col("known_rate_prev")
    if cell == "baseline":
        return pl.lit(1.0)
    kind, cut_label = cell.split("_K", maxsplit=1)
    cut = float(cut_label)
    condition = rate.is_not_null() & (rate < cut)
    if kind == "combo":
        forecast = pl.col("meanrev_forecast_24h")
        threshold = cut * pl.col("n_intervals_planned_hold")
        condition = condition & forecast.is_not_null() & (forecast < threshold)
    if kind in ("skip", "combo"):
        return pl.when(condition).then(0.0).otherwise(1.0)
    if kind == "half":
        return pl.when(condition).then(0.5).otherwise(1.0)
    raise ValueError(f"unknown cell {cell}")


def bucket_diagnostic(panel: pl.DataFrame, midpoint: int) -> pl.DataFrame:
    frames = []
    for era in ("full", "early", "late"):
        part = panel
        if era == "early":
            part = panel.filter(pl.col("entry_ts_ms") < midpoint)
        elif era == "late":
            part = panel.filter(pl.col("entry_ts_ms") >= midpoint)
        frames.append(
            part.group_by("fund_bucket")
            .agg(
                pl.len().alias("trades"),
                (100.0 * pl.col("net_return").sum()).alias("net_pct_capital"),
                (10_000.0 * pl.col("net_return").mean()).alias("mean_net_bps"),
                (100.0 * pl.col("gross_return").sum()).alias("gross_pct_capital"),
                (100.0 * pl.col("funding_return").sum()).alias("funding_pct_capital"),
                (pl.col("exit_reason") == "take_profit").mean().alias("tp_rate"),
                pl.col("symbol").n_unique().alias("n_symbols"),
            )
            .with_columns(pl.lit(era).alias("era"))
        )
    return pl.concat(frames, how="vertical").sort(["era", "fund_bucket"])


def render_arm_panel(book: pl.DataFrame, series: common.FundingSeries) -> pl.DataFrame:
    rates = [
        v4_shared.known_prev_rate(str(t["symbol"]), int(t["entry_ts_ms"]), series)
        for t in book.iter_rows(named=True)
    ]
    return book.with_columns(pl.Series("known_rate_prev", rates, dtype=pl.Float64)).with_columns(
        v4_shared.funding_bucket_expr()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-g" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    common.crosscheck_ledger_funding(ledger, series)
    midpoint = common.era_midpoint_ts_ms(ledger)
    start_day = common.utc_day_ms(int(ledger["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(ledger["exit_ts_ms"].max()))

    panel = build_panel(ledger, series)
    counts = {str(k): int(v) for k, v in panel.group_by("fund_bucket").agg(pl.len()).iter_rows()}
    for bucket, expected in DRAFT_BUCKET_COUNTS.items():
        if counts.get(bucket, 0) != expected:
            raise RuntimeError(f"bucket {bucket}: {counts.get(bucket, 0)} trades, draft inspected {expected}")
    print(f"funding bucket counts reproduce the draft exactly; missing={counts.get('missing', 0)}", flush=True)

    panel_path = out_dir / "tg_trade_panel.parquet"
    panel.write_parquet(panel_path)

    diagnostic = bucket_diagnostic(panel, midpoint)
    diagnostic_path = out_dir / "tg_bucket_diagnostic.csv"
    diagnostic.write_csv(diagnostic_path)

    cells = ["baseline"]
    for kind in ("skip", "half", "combo"):
        cells.extend(f"{kind}_K{cut:g}" for cut in RATE_CUTS)
    grid_rows: list[dict[str, Any]] = []
    for cell in cells:
        cell_panel = panel.with_columns(cell_weight_expr(cell).alias("weight_factor"))
        for row in v4_shared.weighted_cell_metrics(
            cell_panel, series, bars, midpoint_ts_ms=midpoint, start_day_ms=start_day, end_day_ms=end_day
        ):
            row["cell"] = cell
            grid_rows.append(row)
        print(f"cell done: {cell}", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None).select(
        "cell", "era", "trades_kept", "trades_removed", "trades_downweighted",
        "net_return", "gross_return", "cost_return", "funding_return", "max_drawdown",
        "worst_day_return", "worst_day", "per_trade_net_bps", "tp_rate", "mean_mae",
        "share_mae_below_10pct", "removed_gross_forgone", "removed_funding_saved",
        "removed_cost_saved", "removed_net_delta",
    )
    grid_path = out_dir / "tg_grid.csv"
    grid.write_csv(grid_path)

    baseline_row = grid.filter((pl.col("cell") == "baseline") & (pl.col("era") == "full"))
    if abs(float(baseline_row["net_return"][0]) - float(ledger["net_return"].sum())) > 1e-9:
        raise RuntimeError("baseline cell does not reproduce the ledger net return")

    render_funding, render_funding_sha = v4_shared.render_funding_cache(args.data_root)
    render_series = common.funding_series_by_symbol(render_funding)
    print(f"render funding cache: {render_funding.shape} sha={render_funding_sha[:12]}", flush=True)
    render_frames = []
    render_missing: dict[str, int] = {}
    for arm in ("gate_on", "gate_off"):
        book = v4_shared.load_render_book(arm)
        featured = render_arm_panel(book, render_series)
        render_missing[arm] = featured.filter(pl.col("known_rate_prev").is_null()).height
        table = v4_shared.render_bucket_table(
            featured, "fund_bucket", v4_shared.render_era_midpoint_ms(book)
        ).with_columns(pl.lit(arm).alias("arm"))
        render_frames.append(table)
        print(f"render arm {arm}: {book.height} entries, missing prev rate {render_missing[arm]}", flush=True)
    render_table = pl.concat(render_frames, how="vertical")
    render_path = out_dir / "tg_render_buckets.csv"
    render_table.write_csv(render_path)

    common.write_manifest(
        out_dir,
        kind="strategy_research_v4_tg_funding_state",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "shared_cache_dir": str(shared_dir),
            "render_funding_cache": {"sha256": render_funding_sha, "dir": str(v4_shared.RENDER_SHARED_DIR)},
            "ta_render_books": str(v4_shared.TA_DIR),
        },
        params={
            "sleeve": "continuous",
            "feature": "known_rate_prev = last settled funding rate at or before entry (strictly PIT)",
            "buckets": {
                "deep_neg": "r < -0.1%", "neg": "-0.1% <= r < 0", "zero": "0 <= r <= +0.01%",
                "pos": "r > +0.01%",
            },
            "grid": {
                "rate_cuts_per_interval": list(RATE_CUTS),
                "kinds": ["skip", "half_weight", "combo_with_meanrev_forecast"],
                "combo_rule": "skip iff known_prev < K AND meanrev_phi0.5 24h forecast < K * n_settlements",
                "meanrev": {
                    "phi": MEANREV_PHI,
                    "mu": f"trailing mean of last {TRAILING_MEAN_WINDOW} settlements (min {TRAILING_MEAN_MIN},"
                    " else known_prev)",
                },
                "missing_rate_treatment": "pass through at weight 1.0",
            },
            "era_midpoint": common.iso_date(midpoint),
            "draft_bucket_counts_check": DRAFT_BUCKET_COUNTS,
            "render_missing_prev_rate": render_missing,
            "overlap_with_te": "see reports/strategy-research-v3/t-e/<date>/te_overlap_funding.csv",
        },
        output_files={
            "tg_bucket_diagnostic.csv": diagnostic_path,
            "tg_grid.csv": grid_path,
            "tg_render_buckets.csv": render_path,
            "tg_trade_panel.parquet": panel_path,
        },
        extra={"explicit_non_conclusions": [
            "exploratory post-processing of a spent discovery surface; no alpha claim",
            "no capacity backfill: removed or down-weighted trades admit no substitutes",
            "render-book arm is a diagnostic on already-rendered T-A outputs; no engine change",
            "no promotion or deployment implication",
        ]},
    )
    print(json.dumps({"cells": cells}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
