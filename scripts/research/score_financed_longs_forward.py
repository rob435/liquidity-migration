#!/usr/bin/env python3
"""Append the financed-longs rolling forward ledger (one row per config-day).

The Lane-2 recipe for the registered carry-hold configs (``DEFAULT_CONFIGS``
below, ``lane2_carry_hold_v1`` through ``v7``) is one score row per completed
UTC day; only days strictly after each config's registration commit date
count as forward evidence. (The funding-spread and financed-leaders configs
scored here until their 2026-08-19 deletion by operator override; their old
ledger rows remain as receipts.) This tool materializes
that ledger append-first from a cross-venue panel:

* existing (date, config_id) rows are never rewritten — receipts, not state;
* every scored panel day is written (seen-data rows carry
  ``forward_eligible=false``) so the ledger is self-describing;
* the carry-hold sizing experiments get derived rows (``DIFF_PAIRS``):
  ``carry_hold_v2_minus_v1`` through ``carry_hold_v7_minus_v5``, paired daily
  differentials, the primary comparison each registration declares. v7−v5 is
  the currently registered forward experiment. v4's registration declares the
  CAPITAL-NORMALISED differential as its experiment, not this raw one — at its
  own capital v4 is not a return improvement (+1.07 bp/day, t 0.47) and this
  row will show that. Read it beside ``mean_gross``: v4's claim is the same
  money on ~30% less capital.

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

from liquidity_migration.research.backtest.financed_longs import config_scores  # noqa: E402
from liquidity_migration.rules.carry_hold import FinancedLongsError  # noqa: E402

DEFAULT_PANEL_ROOT = Path.home() / "SHARED_DATA" / "cross_venue_panel_v1"
DEFAULT_LEDGER = (
    Path.home() / "SHARED_DATA" / "bybit_full_pit" / "reports" / "financed_longs_forward" / "ledger.csv"
)
DEFAULT_CONFIGS = (
    "lane2_carry_hold_v1.json",
    "lane2_carry_hold_v2.json",
    "lane2_carry_hold_v3.json",
    "lane2_carry_hold_v4.json",
    "lane2_carry_hold_v5.json",
    "lane2_carry_hold_v7.json",
)
DIFF_ID = "carry_hold_v2_minus_v1"
DIFF_PAIRS = (
    ("carry_hold_v2_minus_v1", "lane2_carry_hold_v2", "lane2_carry_hold_v1"),
    ("carry_hold_v3_minus_v2", "lane2_carry_hold_v3", "lane2_carry_hold_v2"),
    ("carry_hold_v4_minus_v3", "lane2_carry_hold_v4", "lane2_carry_hold_v3"),
    ("carry_hold_v5_minus_v4", "lane2_carry_hold_v5", "lane2_carry_hold_v4"),
    ("carry_hold_v7_minus_v5", "lane2_carry_hold_v7", "lane2_carry_hold_v5"),
)
PANEL_COLS = [
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_turnover_quote", "bn_funding", "bn_funding_age_h",
]
#: Present only on panels built with --metrics-root. Kept when every shard has
#: them; v5 and v6 then score, and on an older panel each raises its own loud
#: missing-column error while v1..v4 keep scoring.
OPTIONAL_PANEL_COLS = ["bn_tt_ls", "bn_tt_ls_age_h"]
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
    scans = [pl.scan_parquet(p) for p in shards]
    optional = [
        c
        for c in OPTIONAL_PANEL_COLS
        if all(c in s.collect_schema().names() for s in scans)
    ]
    cols = PANEL_COLS + optional
    return pl.concat(
        [s.select(cols).collect() for s in scans], how="vertical"
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
    """Paired daily differentials (v2-v1, v3-v2, v4-v3) on shared dates."""
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
    # One config's missing panel feature must not cost the others their rows:
    # score what can be scored, report the failure, exit nonzero.
    frames: list[pl.DataFrame] = []
    failed: list[str] = []
    for name in args.config or list(DEFAULT_CONFIGS):
        try:
            frames.append(
                config_rows(
                    panel, args.config_dir / name, end_date=args.end_date, scored_at=scored_at
                )
            )
        except FinancedLongsError as exc:
            failed.append(name)
            print(f"SKIPPED {name}: {exc}", file=sys.stderr)
    if not frames:
        raise SystemExit("no config produced rows")
    fresh = pl.concat(frames, how="vertical")
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
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
