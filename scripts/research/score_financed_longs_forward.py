#!/usr/bin/env python3
"""Append the financed-longs rolling forward ledger (one row per config-day).

The Lane-2 recipe for the registered financed-longs configs (``DEFAULT_CONFIGS``
below: ``lane2_carry_hold_v1``, ``lane2_carry_hold_v2``, ``lane2_carry_hold_v3``,
``lane2_funding_spread_v1``, ``lane2_financed_leaders_v1``,
``lane2_financed_leaders_binance_v1``) is one
score row per completed UTC day; only days strictly after each config's
registration commit date count as forward evidence. This tool materializes
that ledger append-first from a cross-venue panel:

* existing (date, config_id) rows are never rewritten — receipts, not state;
* every scored panel day is written (seen-data rows carry
  ``forward_eligible=false``) so the ledger is self-describing;
* the carry-hold sizing experiments get derived rows (``DIFF_PAIRS``):
  ``carry_hold_v2_minus_v1`` and ``carry_hold_v3_minus_v2``, paired daily
  differentials, the primary comparison each registration declares.

Extending the input panel in time belongs to ``scripts/ops.sh research-refresh``
and ``scripts/data/build_cross_venue_panel.py``, not here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.financed_longs import config_scores  # noqa: E402

DEFAULT_PANEL_ROOT = Path.home() / "SHARED_DATA" / "cross_venue_panel_v1"
DEFAULT_LEDGER = (
    Path.home() / "SHARED_DATA" / "bybit_full_pit" / "reports" / "financed_longs_forward" / "ledger.csv"
)
DEFAULT_CONFIGS = (
    "lane2_carry_hold_v1.json",
    "lane2_carry_hold_v2.json",
    "lane2_carry_hold_v3.json",
    "lane2_funding_spread_v1.json",
    "lane2_financed_leaders_v1.json",
    "lane2_financed_leaders_binance_v1.json",
)
DIFF_ID = "carry_hold_v2_minus_v1"
DIFF_PAIRS = (
    ("carry_hold_v2_minus_v1", "lane2_carry_hold_v2", "lane2_carry_hold_v1"),
    ("carry_hold_v3_minus_v2", "lane2_carry_hold_v3", "lane2_carry_hold_v2"),
)
PANEL_COLS = [
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_turnover_quote", "bn_funding", "bn_funding_age_h",
]
SCHEMA = {
    "date": pl.String,
    "config_id": pl.String,
    "venue": pl.String,
    "gross_bp": pl.Float64,
    "oneway": pl.Float64,
    "cost_bp": pl.Float64,
    "net_bp": pl.Float64,
    "forward_eligible": pl.Boolean,
    "scored_at": pl.String,
}


def load_panel(root: Path) -> pl.DataFrame:
    shards = sorted(root.glob("*/panel.parquet"))
    if not shards:
        raise SystemExit(f"no panel shards under {root}")
    return pl.concat(
        [pl.scan_parquet(p).select(PANEL_COLS).collect() for p in shards], how="vertical"
    ).sort(["symbol", "bar_ts_ms"])


def config_rows(
    panel: pl.DataFrame, config_path: Path, *, end_date: str | None, scored_at: str
) -> pl.DataFrame:
    registered = str(json.loads(config_path.read_text(encoding="utf-8"))["registered"])
    scores, _, config_id, venue = config_scores(panel, config_path)
    rows = scores.with_columns(
        pl.from_epoch("bar_ts_ms", time_unit="ms").dt.date().cast(pl.String).alias("date")
    )
    if end_date is not None:
        rows = rows.filter(pl.col("date") < end_date)
    return rows.select(
        "date",
        pl.lit(config_id).alias("config_id"),
        pl.lit(venue).alias("venue"),
        pl.col("gross_bp").round(6),
        pl.col("oneway").round(8),
        pl.col("cost_bp").round(6),
        pl.col("net_bp").round(6),
        (pl.col("date") > registered).alias("forward_eligible"),
        pl.lit(scored_at).alias("scored_at"),
    )


def diff_rows(rows: pl.DataFrame) -> pl.DataFrame:
    """Paired daily differentials (v2-v1, v3-v2) on shared dates."""
    out: list[pl.DataFrame] = []
    for diff_id, a_id, b_id in DIFF_PAIRS:
        a = rows.filter(pl.col("config_id") == a_id)
        b = rows.filter(pl.col("config_id") == b_id).select(
            "date",
            pl.col("gross_bp").alias("_g"),
            pl.col("oneway").alias("_o"),
            pl.col("cost_bp").alias("_c"),
            pl.col("net_bp").alias("_n"),
        )
        if a.is_empty() or b.is_empty():
            continue
        out.append(
            a.join(b, on="date", how="inner").select(
                "date",
                pl.lit(diff_id).alias("config_id"),
                "venue",
                (pl.col("gross_bp") - pl.col("_g")).round(6).alias("gross_bp"),
                (pl.col("oneway") - pl.col("_o")).round(8).alias("oneway"),
                (pl.col("cost_bp") - pl.col("_c")).round(6).alias("cost_bp"),
                (pl.col("net_bp") - pl.col("_n")).round(6).alias("net_bp"),
                "forward_eligible",
                "scored_at",
            )
        )
    if not out:
        return rows.clear()
    return pl.concat(out, how="vertical")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--panel-root", type=Path, default=DEFAULT_PANEL_ROOT)
    ap.add_argument("--config-dir", type=Path, default=REPO_ROOT / "configs")
    ap.add_argument(
        "--config", action="append", default=None,
        help="config filename (repeatable; default: the four registered financed-longs configs)",
    )
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument(
        "--end-date", default=None,
        help="EXCLUSIVE last date to score (YYYY-MM-DD); default: every panel day",
    )
    args = ap.parse_args(argv)

    panel = load_panel(args.panel_root)
    scored_at = dt.datetime.now(dt.UTC).date().isoformat()
    fresh = pl.concat(
        [
            config_rows(
                panel, args.config_dir / name, end_date=args.end_date, scored_at=scored_at
            )
            for name in (args.config or list(DEFAULT_CONFIGS))
        ],
        how="vertical",
    )
    fresh = pl.concat([fresh, diff_rows(fresh)], how="vertical")

    if args.ledger.exists():
        existing = pl.read_csv(args.ledger, schema_overrides=SCHEMA)
        keep = fresh.join(
            existing.select("date", "config_id"), on=["date", "config_id"], how="anti"
        )
        merged = pl.concat([existing, keep], how="vertical")
    else:
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        existing, keep, merged = None, fresh, fresh
    merged = merged.sort(["config_id", "date"])
    merged.write_csv(args.ledger)

    print(f"ledger: {args.ledger}  rows {merged.height} (+{keep.height} appended)")
    for (cid,), g in sorted(merged.group_by("config_id"), key=lambda kv: kv[0]):
        f = g.filter(pl.col("forward_eligible"))
        line = f"  {cid:28s} days {g.height:5d}  last {g['date'].max()}"
        if f.height:
            line += f"  | FORWARD {f.height:4d} days, mean {f['net_bp'].mean():+7.2f} bp/day"
        else:
            line += "  | no forward-eligible days yet"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
