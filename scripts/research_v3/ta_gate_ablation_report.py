#!/usr/bin/env python3
"""T-A: paired diff table for the BTC uptrend-gate ablation renders.

Reads two completed research renders of the standard CONTINUOUS chain
(baseline gate-on vs --research-disable-btc-gate) and produces one summary
table answering: what does the gate buy, and what does it cost, per era and
in the tail.  The tail arm is the set of historical common-loss dates (both
V2 barebones sleeves lost on the same UTC day; 156 dates in the V2
diagnostics window) intersected with the render window, plus 2024-08-06
explicitly.  Exploratory Lane-1; no promotion claim.

Usage:
  .venv\\Scripts\\python.exe scripts/research_v3/ta_gate_ablation_report.py \\
      --baseline reports/strategy-research-v3/t-a/<date>/render-baseline \\
      --gate-off reports/strategy-research-v3/t-a/<date>/render-gate-off \\
      --venue bybit
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
from scripts.research_v3 import common  # noqa: E402

EXPECTED_COMMON_LOSS_DATES = 156
NAMED_TAIL_DATE = "2024-08-06"


def common_loss_dates() -> list[str]:
    """Dates where both V2 barebones sleeves lost on the same UTC day."""
    curve = pl.read_parquet(common.CURVE_PATH)
    by_day = curve.pivot(values="net_return", index="date", on="sleeve")
    both = by_day.filter((pl.col("long") < 0.0) & (pl.col("continuous") < 0.0))
    dates = sorted(both["date"].to_list())
    if len(dates) != EXPECTED_COMMON_LOSS_DATES:
        raise RuntimeError(
            f"expected {EXPECTED_COMMON_LOSS_DATES} common-loss dates from the V2 curve, got {len(dates)}"
        )
    return dates


def load_render(render_dir: Path, venue: str) -> tuple[pl.DataFrame, dict[str, Any], int]:
    sleeve_dir = render_dir / "continuous" / venue
    frame = (
        pl.read_csv(sleeve_dir / "continuous_equity.csv")
        .select("ts_ms", "basket_return", "equity")
        .sort("ts_ms")
        .with_columns(
            pl.col("ts_ms").map_elements(common.iso_date, return_dtype=pl.String).alias("date")
        )
    )
    summary = json.loads((sleeve_dir / "continuous_equity_summary.json").read_text(encoding="utf-8"))
    entries = 0
    components_root = render_dir / "continuous" / "components" / venue
    for report in sorted(components_root.glob("*/continuous_report.json")):
        entries += int(json.loads(report.read_text(encoding="utf-8"))["n_trades"])
    return frame, summary, entries


def arm_stats(frame: pl.DataFrame, tail_dates: set[str], entries: int) -> dict[str, Any]:
    if frame.is_empty():
        return {}
    eq_first = float(frame["equity"][0])
    eq_last = float(frame["equity"][-1])
    total = eq_last / eq_first - 1.0
    days = frame.height
    annualized = (1.0 + total) ** (365.0 / days) - 1.0 if days else None
    dd = frame.with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("dd"))
    worst = frame.sort("basket_return").head(1)
    tail = frame.filter(pl.col("date").is_in(sorted(tail_dates)))
    named = frame.filter(pl.col("date") == NAMED_TAIL_DATE)
    return {
        "days": days,
        "total_return": total,
        "annualized_return": annualized,
        "max_drawdown": float(dd["dd"].min()),
        "worst_day_return": float(worst["basket_return"][0]),
        "worst_day": str(worst["date"][0]),
        "entries": entries,
        "per_entry_net_bps": (10_000.0 * total / entries) if entries else None,
        "tail_days_in_window": tail.height,
        "tail_sum_return": float(tail["basket_return"].sum()) if tail.height else 0.0,
        "tail_mean_return": float(tail["basket_return"].mean()) if tail.height else None,
        "tail_worst_return": float(tail["basket_return"].min()) if tail.height else None,
        "tail_negative_days": tail.filter(pl.col("basket_return") < 0).height,
        f"return_{NAMED_TAIL_DATE}": float(named["basket_return"][0]) if named.height else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--gate-off", type=Path, required=True)
    parser.add_argument("--venue", default="bybit")
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    out_dir = common.REPORT_ROOT / "t-a" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)
    v2_identity = common.verify_v2_inputs()

    tail_dates = set(common_loss_dates())
    base_frame, base_summary, base_entries = load_render(args.baseline, args.venue)
    off_frame, off_summary, off_entries = load_render(args.gate_off, args.venue)

    # The engine emits rows only for days with an active book, so the two arms
    # cover different day sets (the gate-on arm is flat far more often).  Align
    # both on the union calendar: absent days are flat (return 0, equity held).
    lo = min(int(base_frame["ts_ms"].min()), int(off_frame["ts_ms"].min()))
    hi = max(int(base_frame["ts_ms"].max()), int(off_frame["ts_ms"].max()))
    calendar = pl.DataFrame(
        {"ts_ms": list(range((lo // MS_PER_DAY) * MS_PER_DAY, hi + MS_PER_DAY, MS_PER_DAY))},
        schema={"ts_ms": pl.Int64},
    ).filter(pl.col("ts_ms") >= lo)

    def align(frame: pl.DataFrame) -> pl.DataFrame:
        return (
            calendar.join(frame.drop("date"), on="ts_ms", how="left")
            .sort("ts_ms")
            .with_columns(
                pl.col("basket_return").fill_null(0.0),
                pl.col("equity").fill_null(strategy="forward").fill_null(1.0),
            )
            .with_columns(
                pl.col("ts_ms").map_elements(common.iso_date, return_dtype=pl.String).alias("date")
            )
        )

    base_days, off_days = base_frame.height, off_frame.height
    base_frame = align(base_frame)
    off_frame = align(off_frame)
    midpoint = ((lo + hi) // 2 // MS_PER_DAY) * MS_PER_DAY

    rows: list[dict[str, Any]] = []
    for era in ("full", "early", "late"):
        for arm, frame, entries in (("baseline", base_frame, base_entries), ("gate_off", off_frame, off_entries)):
            if era == "early":
                part = frame.filter(pl.col("ts_ms") < midpoint)
            elif era == "late":
                part = frame.filter(pl.col("ts_ms") >= midpoint)
            else:
                part = frame
            # Recompute equity within the era so drawdown is era-local.
            part = part.with_columns(
                ((1.0 + pl.col("basket_return")).cum_prod()).alias("equity")
            )
            row = {"era": era, "arm": arm}
            row.update(arm_stats(part, tail_dates, entries if era == "full" else 0))
            rows.append(row)
    table = pl.from_dicts(rows, infer_schema_length=None)
    table_path = out_dir / "ta_paired_diff.csv"
    table.write_csv(table_path)

    inputs = {
        "v2": v2_identity,
        "baseline_render": str(args.baseline),
        "gate_off_render": str(args.gate_off),
        "baseline_equity_csv_sha256": common.sha256_file(
            args.baseline / "continuous" / args.venue / "continuous_equity.csv"
        ),
        "gate_off_equity_csv_sha256": common.sha256_file(
            args.gate_off / "continuous" / args.venue / "continuous_equity.csv"
        ),
        "baseline_summary": base_summary.get("1x"),
        "gate_off_summary": off_summary.get("1x"),
    }
    common.write_manifest(
        out_dir,
        kind="strategy_research_v3_ta_gate_ablation",
        inputs=inputs,
        params={
            "venue": args.venue,
            "tail_definition": "V2 common-loss dates (both sleeves negative) within the render window",
            "tail_dates_total": EXPECTED_COMMON_LOSS_DATES,
            "named_tail_date": NAMED_TAIL_DATE,
            "era_midpoint": common.iso_date(midpoint),
            "identical_cost_model": True,
            "identical_components": True,
            "ablation_flag": "--research-disable-btc-gate (render-only)",
            "alignment": "union daily calendar; days without an active book are flat"
            " (return 0, equity held)",
            "active_days": {"baseline": base_days, "gate_off": off_days},
        },
        output_files={"ta_paired_diff.csv": table_path},
        extra={"explicit_non_conclusions": [
            "exploratory render comparison; equity renders over 2025+ were already"
            " deployed-profile-visible, and label-level holdout data remains unread",
            "no promotion claim; live profile changes only via the normal post-epoch deploy flow",
        ]},
    )
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
