#!/usr/bin/env python3
"""R3a Lane-1: book-level daily loss budget — historical trigger replay.

Tail-risk program P1.2 (`docs/tail_risk_program.md`). Exploratory Lane-1
replay of the R3a trigger on two SEEN surfaces, graded AS INSURANCE
(taxonomy item 27): trigger correctness, false-trip rate, and premium
(forgone upside next to avoided loss) — explicitly NOT return improvement.

Rule replayed (frozen shape, from the proposal + kill-criteria arithmetic):
  within each UTC day, realized book P&L books at each trade's exit_ts;
  when the cumulative realized day P&L first breaches X of the capital
  reference, all entries strictly after the breach time on that UTC day
  are blocked (entry-side only; existing exposure untouched; reset at the
  next UTC midnight).

Declared cells: X ∈ {-1.0%, -1.5%, -2.0%} — the REGISTERED value is
X = -1.5% (K1 = -5%/epoch ⇒ a daily budget near -1.5% binds well before the
sleeve kill); the flankers are declared sensitivity cells, reported always,
never used to reselect X.

Surfaces: (A) the full V2 barebones book (LONG + CONTINUOUS — the trigger
is book-level) and (B) the T-A render gate_on book (deployed CONTINUOUS
shape; LONG render books do not exist). Realized-at-exit convention: a
trade's full net (incl. modeled cost + funding) books at its recorded
exit_ts_ms — the ledger's own realized accounting; intraday unrealized
excursions are not part of the trigger by design (realized-day-loss rule).

Usage: .venv\\Scripts\\python.exe scripts/research_v3/r3a_loss_budget_lane1.py --shared-date 2026-07-19
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

CAPITAL_REFERENCE = 1.0  # ledger nets are book-fraction units
THRESHOLDS: tuple[float, ...] = (-0.010, -0.015, -0.020)
REGISTERED_THRESHOLD = -0.015


def replay_threshold(trades: pl.DataFrame, threshold: float) -> dict[str, Any]:
    """Replay the trigger over one book; return insurance metrics + blocked ids."""
    exits = (
        trades.select(
            "trade_id",
            pl.col("exit_ts_ms").alias("ts"),
            pl.col("net_return").alias("net"),
        )
        .with_columns(((pl.col("ts") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .sort(["day", "ts"])
    )
    entries = (
        trades.select(
            "trade_id", "symbol",
            pl.col("entry_ts_ms").alias("ts"),
            pl.col("net_return").alias("net"),
        )
        .with_columns(((pl.col("ts") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
    )

    trigger_days: list[dict[str, Any]] = []
    blocked_ids: list[str] = []
    for day, part in exits.partition_by("day", as_dict=True, maintain_order=True).items():
        day_ms = int(day[0] if isinstance(day, tuple) else day)
        cum = 0.0
        trigger_ts: int | None = None
        for ts, net in zip(part["ts"].to_list(), part["net"].to_list()):
            cum += float(net)
            if trigger_ts is None and cum <= threshold * CAPITAL_REFERENCE:
                trigger_ts = int(ts)
        day_final = cum
        if trigger_ts is None:
            continue
        day_blocked = entries.filter((pl.col("day") == day_ms) & (pl.col("ts") > trigger_ts))
        blocked_ids.extend(day_blocked["trade_id"].to_list())
        trigger_days.append(
            {
                "day": dt.datetime.fromtimestamp(day_ms / 1000, tz=dt.timezone.utc).date().isoformat(),
                "trigger_ts_ms": trigger_ts,
                "day_final_realized": day_final,
                "recovered_above_threshold": day_final > threshold,
                "continuation_after_trigger": day_final - threshold,
                "n_blocked_entries": day_blocked.height,
                "n_blocked_unique": day_blocked.select("symbol", "day").unique().height,
                "blocked_net_sum": float(day_blocked["net"].sum()) if day_blocked.height else 0.0,
            }
        )
    return {"trigger_days": trigger_days, "blocked_ids": blocked_ids}


def era_split(rows: list[dict[str, Any]], midpoint_iso: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "full": rows,
        "early": [r for r in rows if r["day"] < midpoint_iso],
        "late": [r for r in rows if r["day"] >= midpoint_iso],
    }


def insurance_summary(rows: list[dict[str, Any]], n_days_window: int) -> dict[str, Any]:
    if not rows:
        return {
            "trigger_days": 0, "triggers_per_year": 0.0, "false_trip_rate": None,
            "mean_continuation": None, "blocked_entries": 0, "blocked_unique": 0,
            "forgone_upside": 0.0, "avoided_loss": 0.0, "blocked_net_sum": 0.0,
        }
    blocked_pos = sum(max(r["blocked_net_sum"], 0.0) for r in rows)
    blocked_neg = sum(min(r["blocked_net_sum"], 0.0) for r in rows)
    return {
        "trigger_days": len(rows),
        "triggers_per_year": 365.25 * len(rows) / max(n_days_window, 1),
        "false_trip_rate": sum(1 for r in rows if r["recovered_above_threshold"]) / len(rows),
        "mean_continuation": sum(r["continuation_after_trigger"] for r in rows) / len(rows),
        "worst_continuation": min(r["continuation_after_trigger"] for r in rows),
        "blocked_entries": sum(r["n_blocked_entries"] for r in rows),
        "blocked_unique": sum(r["n_blocked_unique"] for r in rows),
        "forgone_upside": blocked_pos,
        "avoided_loss": blocked_neg,
        "blocked_net_sum": blocked_pos + blocked_neg,
    }


def surface_report(
    name: str,
    trades: pl.DataFrame,
    series: common.FundingSeries,
    bars: common.BarSeries,
    *,
    midpoint_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_day = common.utc_day_ms(int(trades["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(trades["exit_ts_ms"].max()))
    n_days = int((end_day - start_day) // MS_PER_DAY) + 1
    midpoint_iso = common.iso_date(midpoint_ms)

    # the trigger is book-level: collapse sleeve labels so daily_curve sums both
    trades = trades.with_columns(pl.lit("book").alias("sleeve"))
    base_contrib = common.trade_daily_contributions(trades, series, bars)
    base_curve = common.daily_curve(base_contrib, sleeve="book", start_day_ms=start_day, end_day_ms=end_day)

    grid_rows: list[dict[str, Any]] = []
    detail: dict[str, Any] = {"surface": name, "thresholds": {}}
    for threshold in THRESHOLDS:
        replay = replay_threshold(trades, threshold)
        eras = era_split(replay["trigger_days"], midpoint_iso)
        blocked = set(replay["blocked_ids"])
        kept = trades.filter(~pl.col("trade_id").is_in(sorted(blocked)))
        kept_contrib = common.trade_daily_contributions(kept, series, bars)
        kept_curve = common.daily_curve(kept_contrib, sleeve="book", start_day_ms=start_day, end_day_ms=end_day)
        for era, rows in eras.items():
            if era == "early":
                b_curve = base_curve.filter(pl.col("day_ms") < midpoint_ms)
                k_curve = kept_curve.filter(pl.col("day_ms") < midpoint_ms)
                window_days = max(int((midpoint_ms - start_day) // MS_PER_DAY), 1)
            elif era == "late":
                b_curve = base_curve.filter(pl.col("day_ms") >= midpoint_ms)
                k_curve = kept_curve.filter(pl.col("day_ms") >= midpoint_ms)
                window_days = max(int((end_day - midpoint_ms) // MS_PER_DAY) + 1, 1)
            else:
                b_curve, k_curve, window_days = base_curve, kept_curve, n_days
            summary = insurance_summary(rows, window_days)
            b_daily = b_curve["net_return"].to_list()
            k_daily = k_curve["net_return"].to_list()

            def es(daily: list[float], alpha: float) -> float | None:
                if not daily:
                    return None
                ordered = sorted(daily)
                k = max(1, int(len(ordered) * alpha))
                return sum(ordered[:k]) / k

            grid_rows.append(
                {
                    "surface": name,
                    "threshold": threshold,
                    "registered": threshold == REGISTERED_THRESHOLD,
                    "era": era,
                    **summary,
                    "baseline_net": float(sum(b_daily)),
                    "governed_net": float(sum(k_daily)),
                    "baseline_es95": es(b_daily, 0.05),
                    "governed_es95": es(k_daily, 0.05),
                    "baseline_es99": es(b_daily, 0.01),
                    "governed_es99": es(k_daily, 0.01),
                    "baseline_worst_day": min(b_daily) if b_daily else None,
                    "governed_worst_day": min(k_daily) if k_daily else None,
                }
            )
        detail["thresholds"][f"{threshold:+.3f}"] = replay["trigger_days"]
    return grid_rows, detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = REPO / "reports" / "tail-risk-program" / f"p12-r3a-loss-budget-lane1-{args.out_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger_all = common.load_ledger()  # both sleeves: the trigger is book-level
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)

    grid_rows, detail_a = surface_report(
        "barebones_book",
        ledger_all,
        series,
        bars,
        midpoint_ms=common.era_midpoint_ts_ms(ledger_all),
    )
    print("surface done: barebones_book", flush=True)

    gate_on = v4_shared.load_render_book("gate_on")
    render_klines, render_klines_sha = v4_shared.render_kline_cache(args.data_root)
    render_funding, render_funding_sha = v4_shared.render_funding_cache(args.data_root)
    rows_b, detail_b = surface_report(
        "render_gate_on",
        gate_on,
        common.funding_series_by_symbol(render_funding),
        common.close_series_by_symbol(render_klines),
        midpoint_ms=v4_shared.render_era_midpoint_ms(gate_on),
    )
    grid_rows += rows_b
    print("surface done: render_gate_on", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None)
    grid_path = out_dir / "r3a_grid.csv"
    grid.write_csv(grid_path)
    detail_path = out_dir / "r3a_trigger_days.json"
    detail_path.write_text(json.dumps([detail_a, detail_b], indent=1), encoding="utf-8")

    registered = grid.filter(pl.col("registered") & (pl.col("era") == "full"))
    print(registered.select("surface", "trigger_days", "triggers_per_year", "false_trip_rate",
                            "forgone_upside", "avoided_loss").write_json(), flush=True)

    common.write_manifest(
        out_dir,
        kind="tail_risk_p12_r3a_loss_budget_lane1",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "render_caches": {
                "render_kline_slice_1h.parquet": render_klines_sha,
                "render_funding_events.parquet": render_funding_sha,
            },
        },
        params={
            "rule": "realized day P&L (books at exit_ts) breaches X of capital reference ->"
            " block entries strictly after breach time, same UTC day; reset at midnight;"
            " existing exposure untouched",
            "thresholds": list(THRESHOLDS),
            "registered_threshold": REGISTERED_THRESHOLD,
            "grading": "insurance metrics (item 27): trigger correctness, false-trip rate,"
            " premium = forgone upside next to avoided loss; NOT return improvement",
            "surfaces": {
                "barebones_book": "V2 barebones LONG+CONTINUOUS book",
                "render_gate_on": "T-A deployed-shape CONTINUOUS render book (no LONG renders exist)",
            },
        },
        output_files={"r3a_grid.csv": grid_path, "r3a_trigger_days.json": detail_path},
        extra={"explicit_non_conclusions": [
            "exploratory Lane-1 replay on seen surfaces; no activation decision",
            "realized-at-exit accounting; no intraday unrealized trigger modeled (by design)",
            "no capacity backfill for blocked entries; counterfactual removes their contributions",
            "activation remains an operator decision under the frozen A/B design",
        ]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
