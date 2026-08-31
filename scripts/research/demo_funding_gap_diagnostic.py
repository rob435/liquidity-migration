#!/usr/bin/env python3
"""Lane-1 diagnostic: does the Bybit-minus-Binance funding gap add signal?

The carry-hold book trades Bybit on ``by_funding`` alone. The open question is
whether ``funding_diff_bp`` (bybit minus binance settled funding, per
name-day) carries predictive content on top of Bybit's own funding — the
"route to the venue where funding is most negative" intuition.

This is exploration on already-seen data only: it grades nothing. It rebuilds
the registered v7 book, joins a per-name-day funding-gap and platform-fresh
feature, and splits the book's held name-days by that feature. Every cell and
era split is reported; gross sits next to net.

Run: python scripts/research/demo_funding_gap_diagnostic.py --panel-root ~/SHARED_DATA/cross_venue_panel_v1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import polars as pl

from liquidity_migration.research.backtest.financed_longs import (
    daily_grid,
    prepare,
    top_n_universe,
)
from liquidity_migration.rules.carry_hold import CarryHoldConfig
from liquidity_migration.rules.rust_strategy_contract import rust_carry_research_weights

ROOT = Path(__file__).resolve().parents[2]


def load_panel(panel_root: Path) -> pl.DataFrame:
    files = sorted(panel_root.glob("*/panel.parquet"))
    if not files:
        raise FileNotFoundError(f"no panel shards under {panel_root}")
    frames = []
    for f in files:
        frames.append(pl.read_parquet(f))
    return pl.concat(frames)


def attach_gap_feature(
    grid: pl.DataFrame, *, fresh_hours: float
) -> pl.DataFrame:
    """Attach, per symbol-day on the trading grid, the venues' funding gap.

    ``funding_diff_bp`` is bybit minus binance settled funding (bp-per-day),
    already in the panel. We also require a platform-fresh read of BOTH venues
    (a stale Binance funding is not evidence the two diverged). Null / non-fresh
    values are kept as null so a downstream split can fail open or closed.
    """
    gap = grid.select(
        "bar_ts_ms",
        "symbol",
        "by_funding",
        "funding_diff_bp",
        "bn_funding",
        pl.when(pl.col("by_funding_age_h") <= fresh_hours)
        .then(pl.col("bn_funding_age_h") <= fresh_hours)
        .otherwise(False)
        .alias("both_fresh"),
    )
    return gap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel-root", type=Path, default=Path.home() / "SHARED_DATA/cross_venue_panel_v1")
    ap.add_argument("--config", type=Path, default=ROOT / "configs/lane2_carry_hold_v7.json")
    ap.add_argument("--fresh-hours", type=float, default=48.0)
    ap.add_argument("--out", type=Path, default=ROOT / "reports/demo_funding_gap_diagnostic.json")
    args = ap.parse_args()

    cfg = CarryHoldConfig.from_json(args.config)
    print(f"config={cfg.config_id} venue={cfg.venue}")

    panel = load_panel(args.panel_root)
    grid = daily_grid(prepare(panel))
    universe = top_n_universe(grid, cfg.universe_top_n)
    book = rust_carry_research_weights(universe, cfg)

    # Hold name-days are the rows where the book actually stands long.
    held = universe.join(book, on=["bar_ts_ms", "symbol"], how="inner").filter(
        pl.col("w") > 0.0
    )
    feat = attach_gap_feature(grid, fresh_hours=args.fresh_hours)
    held = held.join(feat, on=["bar_ts_ms", "symbol"], how="left").filter(
        pl.col("both_fresh")
    )

    print(f"held name-days with both venues fresh: {held.height}")

    # Per-name-day net is w * (price_return - funding_paid), in bp; gross is the
    # same without the funding leg, for attribution.
    per_day = held.with_columns(
        (pl.col("w") * pl.col("net_return") * 1e4).alias("net_bp"),
        (pl.col("w") * (pl.col("net_return") - pl.col("funding_paid")) * 1e4).alias("gross_bp"),
    )

    results: dict[str, dict[str, float | int]] = {}

    def report(label: str, frame: pl.DataFrame) -> dict[str, float | int]:
        if frame.height == 0:
            print(f"\n{label}: no rows")
            results[label] = {"n": 0}
            return results[label]
        mean_net = float(frame["net_bp"].mean())  # type: ignore[arg-type]
        mean_gross = float(frame["gross_bp"].mean())  # type: ignore[arg-type]
        if frame.height > 1:
            t = float(frame["net_bp"].mean() / (frame["net_bp"].std(ddof=1) / (frame.height**0.5)))
        else:
            t = 0.0
        row = {"n": frame.height, "gross_bp_per_nameday": round(mean_gross, 2), "net_bp_per_nameday": round(mean_net, 2), "t": round(t, 2)}
        results[label] = row
        print(
            f"\n{label}: n={row['n']} gross={row['gross_bp_per_nameday']:+.2f} "
            f"net={row['net_bp_per_nameday']:+.2f} bp/name-day t={row['t']:+.2f}"
        )
        return row

    # Whole held book (baseline)
    report(f"ALL held ({cfg.config_id})", per_day)

    # The hypothesis: names where Bybit is deeper than Binance (gap very
    # negative = Bybit the more-negative venue) should be stronger, and names
    # where the venues DIVERGE (large |gap|) are the "route to most negative"
    # cohort the idea hunts.
    buckets = [
        ("gap < -40bp/day (Bybit much deeper)", pl.col("funding_diff_bp") < -40.0),
        ("-40 <= gap < -10 (Bybit deeper)", pl.col("funding_diff_bp").is_between(-40.0, -10.0, closed="right")),
        ("-10 <= gap <= 10 (venues agree)", pl.col("funding_diff_bp").is_between(-10.0, 10.0, closed="both")),
        ("10 < gap <= 40 (Binance deeper)", pl.col("funding_diff_bp").is_between(10.0, 40.0, closed="left")),
        ("gap > 40bp/day (Binance much deeper)", pl.col("funding_diff_bp") > 40.0),
    ]
    for name, cond in buckets:
        report(name, per_day.filter(cond))

    # Both venues negative vs Bybit-only-negative, at three gap widths.
    print("\n\n=== cross-venue conditioning (the 'trade the most-negative venue' read) ===")
    report("bybit negative AND binance negative", per_day.filter(pl.col("by_funding") < 0.0, pl.col("bn_funding") < 0.0))
    report("bybit negative AND binance non-negative", per_day.filter(pl.col("by_funding") < 0.0, pl.col("bn_funding") >= 0.0))

    # Era split for the most-interesting bucket (the whole point: does the gap
    # edge live in one era or is it stable?).
    era = (
        per_day.with_columns(
            pl.from_epoch("bar_ts_ms", time_unit="ms").dt.year().cast(pl.String).alias("year")
        )
    )
    print("\n=== era split, Bybit-much-deeper (< -40bp/day) vs agree (-10..10) ===")
    deep = era.filter(pl.col("funding_diff_bp") < -40.0)
    agree = era.filter(pl.col("funding_diff_bp").is_between(-10.0, 10.0, closed="both"))
    for year in sorted(deep["year"].unique().to_list()):
        report(f"{year} bybit-much-deeper", deep.filter(pl.col("year") == year))
    for year in sorted(agree["year"].unique().to_list()):
        report(f"{year} venues-agree", agree.filter(pl.col("year") == year))

    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config_id": cfg.config_id,
        "venue": cfg.venue,
        "lane": 1,
        "fresh_hours": args.fresh_hours,
        "provenance": (
            "seen-data only; grades nothing. "
            "Cross-venue panel (bybit+binance), corrected settlement-exact scorer, "
            "cross_venue_panel_v1."
        ),
        "note": (
            "funding_diff_bp = bybit minus binance settled funding, bp/day. "
            "A negative gap means Bybit is the more-negative-funding venue. "
            "Rows are the registered v7 book's held name-days (w > 0) where both "
            "venues' funding reads are fresh."
        ),
        "results": results,
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\n[written] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
