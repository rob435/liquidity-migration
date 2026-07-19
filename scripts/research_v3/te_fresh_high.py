#!/usr/bin/env python3
"""T-E: fresh-high entry conditioning on the V2 CONTINUOUS barebones ledger.

Exploratory Lane-1 counterfactual post-processing (V4 draft thesis T-E).
The feature is ``hours_since_high_168h`` exactly as computed by the T-C
machinery (PIT at the entry bar close).  Declared grid, frozen before any new
cell was run:

- skip rules: drop entries with hours_since_high_168h > H for H in {1, 6, 24};
- sizing tilt: weight 1.0 for at-high (<=1h), 0.5 for (1, 6]h, 0.25 beyond;
- nothing else is tried on this surface.

Entries whose feature is unavailable (insufficient history) pass through at
full weight and are reported.  Double-verification arm: the same feature is
recomputed from PIT klines for both T-A render books (gate on / gate off) and
the bucket monotonicity is reported per book and era; no engine change is made
here.  No capacity backfill; no alpha or promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/te_fresh_high.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from scripts.research_v3 import common, v4_shared  # noqa: E402
from scripts.research_v3 import tc_pump_deceleration as tc  # noqa: E402

SKIP_HOURS: tuple[float, ...] = (1.0, 6.0, 24.0)
TILT_WEIGHTS = {"at_high_le1h": 1.0, "1_6h": 0.5, "6_24h": 0.25, "gt_24h": 0.25, "unknown": 1.0}

# The draft's inspected bucket counts on this exact ledger; a mismatch means
# the feature being tested is not the structure that was inspected.
DRAFT_BUCKET_COUNTS = {"at_high_le1h": 2530, "1_6h": 1105, "6_24h": 2336, "gt_24h": 10774}


def cell_weight_expr(cell: str) -> pl.Expr:
    h = pl.col("hours_since_high_168h")
    if cell == "baseline":
        return pl.lit(1.0)
    if cell.startswith("skip_h"):
        limit = float(cell.removeprefix("skip_h"))
        return pl.when(h.is_null()).then(1.0).when(h <= limit).then(1.0).otherwise(0.0)
    if cell == "tilt":
        return (
            pl.when(h.is_null())
            .then(TILT_WEIGHTS["unknown"])
            .when(h <= 1.0)
            .then(TILT_WEIGHTS["at_high_le1h"])
            .when(h <= 6.0)
            .then(TILT_WEIGHTS["1_6h"])
            .otherwise(TILT_WEIGHTS["6_24h"])
        )
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
            part.group_by("fresh_bucket")
            .agg(
                pl.len().alias("trades"),
                (100.0 * pl.col("net_return").sum()).alias("net_pct_capital"),
                (10_000.0 * pl.col("net_return").mean()).alias("mean_net_bps"),
                (pl.col("exit_reason") == "take_profit").mean().alias("tp_rate"),
                pl.col("mae").mean().alias("mean_mae"),
                (pl.col("mae") < -0.10).mean().alias("share_mae_below_10pct"),
                pl.col("mfe").mean().alias("mean_mfe"),
                pl.col("age_days_censored").median().alias("median_age_days_censored"),
                pl.col("symbol").n_unique().alias("n_symbols"),
            )
            .with_columns(pl.lit(era).alias("era"))
        )
    return pl.concat(frames, how="vertical").sort(["era", "fresh_bucket"])


def composition_by_year(panel: pl.DataFrame) -> pl.DataFrame:
    return (
        panel.with_columns(pl.col("entry_date").str.slice(0, 4).alias("year"))
        .group_by(["fresh_bucket", "year"])
        .agg(
            pl.len().alias("trades"),
            pl.col("symbol").n_unique().alias("n_symbols"),
            (100.0 * pl.col("net_return").sum()).alias("net_pct_capital"),
            pl.col("age_days_censored").median().alias("median_age_days_censored"),
        )
        .sort(["fresh_bucket", "year"])
    )


def overlap_with_funding(panel: pl.DataFrame) -> pl.DataFrame:
    return (
        panel.group_by(["fresh_bucket", "fund_bucket"])
        .agg(
            pl.len().alias("trades"),
            (100.0 * pl.col("net_return").sum()).alias("net_pct_capital"),
            (10_000.0 * pl.col("net_return").mean()).alias("mean_net_bps"),
        )
        .sort(["fresh_bucket", "fund_bucket"])
    )


def render_arm_features(book: pl.DataFrame, klines: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, int]]:
    ohlc = tc.ohlc_series_by_symbol(klines)
    closes_by_symbol = {symbol: (ends, closes) for symbol, (ends, _h, _l, closes) in ohlc.items()}
    hours: list[float | None] = []
    statuses: list[str] = []
    for trade in book.iter_rows(named=True):
        ends, closes = closes_by_symbol.get(str(trade["symbol"]), ([], []))
        value, status = v4_shared.hours_since_high_tolerant(int(trade["entry_ts_ms"]), ends, closes)
        hours.append(value)
        statuses.append(status)
    out = book.with_columns(
        pl.Series("hours_since_high_168h", hours, dtype=pl.Float64),
        pl.Series("feature_status", statuses),
    ).with_columns(v4_shared.freshness_bucket_expr())
    status_counts = {
        str(k): int(v)
        for k, v in out.group_by("feature_status").agg(pl.len()).iter_rows()
    }
    return out, status_counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-e" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    ohlc = tc.ohlc_series_by_symbol(klines)
    common.crosscheck_ledger_funding(ledger, series)
    midpoint = common.era_midpoint_ts_ms(ledger)
    start_day = common.utc_day_ms(int(ledger["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(ledger["exit_ts_ms"].max()))

    panel = tc.compute_features(ledger, ohlc).with_columns(v4_shared.freshness_bucket_expr())
    first_bar = (
        klines.group_by("symbol").agg(pl.col("bar_end_ts_ms").min().alias("first_bar_end_ms"))
    )
    panel = panel.join(first_bar, on="symbol", how="left").with_columns(
        ((pl.col("entry_ts_ms") - pl.col("first_bar_end_ms")) / MS_PER_DAY).alias("age_days_censored")
    )
    known_prev = [
        v4_shared.known_prev_rate(str(t["symbol"]), int(t["entry_ts_ms"]), series)
        for t in panel.iter_rows(named=True)
    ]
    panel = panel.with_columns(pl.Series("known_rate_prev", known_prev, dtype=pl.Float64)).with_columns(
        v4_shared.funding_bucket_expr()
    )

    counts = {str(k): int(v) for k, v in panel.group_by("fresh_bucket").agg(pl.len()).iter_rows()}
    for bucket, expected in DRAFT_BUCKET_COUNTS.items():
        if counts.get(bucket, 0) != expected:
            raise RuntimeError(f"bucket {bucket}: {counts.get(bucket, 0)} trades, draft inspected {expected}")
    unknown_count = counts.get("unknown", 0)
    print(f"bucket counts reproduce the draft exactly; unknown={unknown_count}", flush=True)

    panel_path = out_dir / "te_trade_features.parquet"
    panel.write_parquet(panel_path)

    diagnostic = bucket_diagnostic(panel, midpoint)
    diagnostic_path = out_dir / "te_bucket_diagnostic.csv"
    diagnostic.write_csv(diagnostic_path)

    composition = composition_by_year(panel)
    composition_path = out_dir / "te_composition_by_year.csv"
    composition.write_csv(composition_path)

    overlap = overlap_with_funding(panel)
    overlap_path = out_dir / "te_overlap_funding.csv"
    overlap.write_csv(overlap_path)

    cells = ["baseline"] + [f"skip_h{h:g}" for h in SKIP_HOURS] + ["tilt"]
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
    grid_path = out_dir / "te_grid.csv"
    grid.write_csv(grid_path)

    baseline_row = grid.filter((pl.col("cell") == "baseline") & (pl.col("era") == "full"))
    if abs(float(baseline_row["net_return"][0]) - float(ledger["net_return"].sum())) > 1e-9:
        raise RuntimeError("baseline cell does not reproduce the ledger net return")

    render_klines, render_kline_sha = v4_shared.render_kline_cache(args.data_root)
    print(f"render kline cache: {render_klines.shape} sha={render_kline_sha[:12]}", flush=True)
    render_frames = []
    render_status: dict[str, dict[str, int]] = {}
    for arm in ("gate_on", "gate_off"):
        book = v4_shared.load_render_book(arm)
        featured, statuses = render_arm_features(book, render_klines)
        render_status[arm] = statuses
        table = v4_shared.render_bucket_table(
            featured, "fresh_bucket", v4_shared.render_era_midpoint_ms(book)
        ).with_columns(pl.lit(arm).alias("arm"))
        render_frames.append(table)
        print(f"render arm {arm}: {book.height} entries, statuses {statuses}", flush=True)
    render_table = pl.concat(render_frames, how="vertical")
    render_path = out_dir / "te_render_buckets.csv"
    render_table.write_csv(render_path)

    common.write_manifest(
        out_dir,
        kind="strategy_research_v4_te_fresh_high",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "shared_cache_dir": str(shared_dir),
            "render_kline_cache": {"sha256": render_kline_sha, "dir": str(v4_shared.RENDER_SHARED_DIR)},
            "ta_render_books": str(v4_shared.TA_DIR),
        },
        params={
            "sleeve": "continuous",
            "feature": "hours_since_high_168h (T-C definition, PIT at entry bar close)",
            "buckets": {
                "at_high_le1h": "h <= 1", "1_6h": "1 < h <= 6", "6_24h": "6 < h <= 24", "gt_24h": "h > 24",
            },
            "grid": {
                "skip_hours": list(SKIP_HOURS),
                "tilt_weights": TILT_WEIGHTS,
                "unknown_treatment": "pass through at weight 1.0 (0 unknown observed)",
            },
            "era_midpoint": common.iso_date(midpoint),
            "draft_bucket_counts_check": DRAFT_BUCKET_COUNTS,
            "render_era_midpoints": {
                arm: common.iso_date(v4_shared.render_era_midpoint_ms(v4_shared.load_render_book(arm)))
                for arm in ("gate_on", "gate_off")
            },
            "render_feature_status": render_status,
            "age_days_censored_origin": "first bar end per symbol in the shared kline slice"
            f" (slice starts {common.KLINE_START.isoformat()}; ages are censored at that origin)",
        },
        output_files={
            "te_bucket_diagnostic.csv": diagnostic_path,
            "te_composition_by_year.csv": composition_path,
            "te_overlap_funding.csv": overlap_path,
            "te_grid.csv": grid_path,
            "te_render_buckets.csv": render_path,
            "te_trade_features.parquet": panel_path,
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
