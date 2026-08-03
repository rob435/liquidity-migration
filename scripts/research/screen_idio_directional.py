#!/usr/bin/env python3
"""The discriminating test: idio charts in a DIRECTIONAL single-name book.

Everything in ``scripts/research/screen_idio_charts.py`` is a cross-sectional decile
spread, and that is the construction least favourable to the idio-chart claim:
the long and short legs share the common factor, so it cancels in the spread
whether or not you residualise first. The measured null there (0/48) is
therefore weak evidence about the claim itself and strong evidence only about
that book.

This script runs the construction where the claim SHOULD bite. Each name gets a
position from its own price path with no cross-sectional information at all, so
common-factor motion does not cancel -- it goes straight into the P&L. If
residualising ever pays, it pays here.

THE RULE, uniform across all six features and both price sources, with one
parameter fixed in advance:

    z[i,t]   = (feature[i,t] - trailing_mean) / trailing_sd    over Z_WINDOW days,
               strictly per symbol, strictly prior
    pos[i,t] = sign(z[i,t])
    book[t]  = mean_i( pos[i,t] * fwd_ret[i,t] )

A time-series z-score is the least arbitrary way to turn a level feature
(``range_pos_30d`` lives in [0,1], ``dist_from_30d_high`` is non-positive) into
a directional call without inventing a per-feature threshold, and it uses only
that symbol's own history.

The book is reported unhedged and BTC-beta-hedged, since a directional book
carries real net beta by construction rather than by accident:

    net_beta[t]   = mean_i( pos[i,t] * beta[i,t] )
    hedged[t]     = book[t] - net_beta[t] * btc_ret[t]

Costs: position turnover on the name leg and |delta net_beta| on the hedge leg,
both at the measured round trip. A sign-flip book turns over 2 units per flip,
which is charged.

MULTIPLE TESTING. This adds 6 features x 4 sources x 2 (hedged/unhedged) = 48
cells to the family, which was 92 after the cross-sectional screen. The
threshold is restated here rather than inherited silently.

Dispatch:
    .venv/bin/python -u scripts/research/screen_idio_directional.py \\
        --panel data/idio_panel/bybit/panel_common4.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.core._common import calendar_roll  # noqa: E402
from liquidity_migration.research.panels.cross_section import (  # noqa: E402
    MEASURED_ROUND_TRIP_BP,
    summary,
    top_by,
)
from liquidity_migration.research.panels.idio_features import CHART_FEATURES  # noqa: E402
from scripts.research.screen_idio_charts import (  # noqa: E402
    ALL_SOURCES,
    DAYS_PER_YEAR,
    PRIOR_MECHANISMS,
    PROGRAM_T,
    bonferroni_t,
)

BTC = "BTCUSDT"
Z_WINDOW = 60
Z_MIN_SAMPLES = 30
#: Cells added to the family by this screen.
NEW_CELLS = len(CHART_FEATURES) * len(ALL_SOURCES) * 2
#: Cells the cross-sectional screen already added.
CROSS_SECTIONAL_CELLS = 48


def directional_book(
    panel: pl.DataFrame, *, signal: str, btc: pl.DataFrame, min_names: int
) -> pl.DataFrame | None:
    """Per-day equal-weight directional book from per-symbol z-scored signals."""
    need = ["ts_ms", "symbol", signal, "fwd_ret_1d", "btc_beta"]
    frame = panel.select(need).drop_nulls().sort(["symbol", "ts_ms"])
    if frame.is_empty():
        return None

    # Strictly-prior trailing moments per symbol, computed on the sparse frame so
    # min_samples counts real observations rather than calendar grid rows.
    frame = frame.with_columns(
        calendar_roll(pl.col(signal), "mean", Z_WINDOW, shifted=True, min_samples=Z_MIN_SAMPLES)
        .over("symbol").alias("_mu"),
        calendar_roll(pl.col(signal), "std", Z_WINDOW, shifted=True, min_samples=Z_MIN_SAMPLES)
        .over("symbol").alias("_sd"),
    )
    frame = frame.with_columns(
        pl.when(pl.col("_sd") > 1e-12)
        .then((pl.col(signal) - pl.col("_mu")) / pl.col("_sd"))
        .otherwise(None)
        .alias("_z")
    ).drop_nulls("_z")
    if frame.is_empty():
        return None
    frame = frame.with_columns(pl.col("_z").sign().alias("_pos")).filter(pl.col("_pos") != 0)
    if frame.is_empty():
        return None

    per_day = (
        frame.group_by("ts_ms")
        .agg(
            (pl.col("_pos") * pl.col("fwd_ret_1d")).mean().alias("_r"),
            (pl.col("_pos") * pl.col("btc_beta")).mean().alias("net_beta"),
            pl.col("_pos").mean().alias("net_dir"),
            pl.len().alias("n"),
        )
        .filter(pl.col("n") >= min_names)
        .drop_nulls()
        .join(btc, on="ts_ms", how="inner")
        .sort("ts_ms")
    )
    if per_day.height < 100:
        return None

    # Position turnover: mean |delta pos| over names, /2 so a full sign flip on
    # every name is 1.0 of gross turned over.
    turn = (
        frame.select("ts_ms", "symbol", "_pos")
        .sort(["symbol", "ts_ms"])
        .with_columns(
            pl.when(pl.col("ts_ms").shift(1).over("symbol") == pl.col("ts_ms") - 86_400_000)
            .then((pl.col("_pos") - pl.col("_pos").shift(1).over("symbol")).abs())
            .otherwise(None)
            .alias("_d")
        )
        .group_by("ts_ms")
        .agg((pl.col("_d").mean() / 2.0).alias("pos_turn"))
    )
    return (
        per_day.join(turn, on="ts_ms", how="left")
        .with_columns(
            (pl.col("_r") * 1e4).alias("book_bp"),
            (pl.col("_r") * 1e4 - pl.col("net_beta") * pl.col("_btc_ret") * 1e4).alias("hedged_bp"),
            pl.col("net_beta").diff().abs().fill_null(0.0).alias("hedge_turn"),
            pl.col("pos_turn").fill_null(0.0),
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", required=True, type=Path)
    ap.add_argument("--universe", type=int, default=150)
    ap.add_argument("--min-names", type=int, default=20)
    ap.add_argument("--coverage-floor", type=float, default=0.90)
    args = ap.parse_args(argv)

    panel = pl.read_parquet(args.panel)
    if "btc_beta" not in panel.columns:
        raise SystemExit("panel lacks btc_beta; rebuild with the current build_idio_panel.py")
    btc = (
        panel.filter(pl.col("symbol") == BTC)
        .select("ts_ms", pl.col("fwd_ret_1d").alias("_btc_ret"))
        .unique(subset="ts_ms").drop_nulls()
    )
    print(f"panel {args.panel}")
    print(f"  rows={panel.height} days={panel['ts_ms'].n_unique()}\n")
    if args.universe > 0:
        panel = top_by(panel, "turnover_quote", args.universe, period="ts_ms")

    family = PRIOR_MECHANISMS + CROSS_SECTIONAL_CELLS + NEW_CELLS
    t_crit = PROGRAM_T
    print(f"bar |t| > {t_crit:.2f} (docs/research/governance.md 2). Retired family-wise threshold for "
          f"{PRIOR_MECHANISMS} prior + {CROSS_SECTIONAL_CELLS} cross-sectional "
          f"+ {NEW_CELLS} directional = {family} was {bonferroni_t(family):.2f}\n")
    print("DIRECTIONAL single-name book: pos = sign(60d z-score of the feature),")
    print("equal weight, no cross-sectional information. Common-factor motion does")
    print("NOT cancel here -- this is where residualising should pay if it ever does.\n")
    print(f"  {'feature':<20s} {'source':<8s} {'N':>5s} {'netDir':>7s} {'|beta|':>7s} "
          f"{'turn':>6s} {'unhedged':>9s} {'unh_t':>6s} {'hedged':>8s} {'hed_t':>6s} {'hed_shrp':>9s}")

    idio_mask = pl.col("coverage").fill_null(0.0) >= args.coverage_floor
    rows: list[dict] = []
    for source in ALL_SOURCES:
        for feature in CHART_FEATURES:
            signal = f"{source}_{feature}"
            if signal not in panel.columns:
                continue
            scoped = panel.filter(idio_mask) if source in ("idio", "iz") else panel
            book = directional_book(scoped, signal=signal, btc=btc, min_names=args.min_names)
            if book is None:
                continue
            pos_cost = float(book["pos_turn"].mean()) * 2.0 * MEASURED_ROUND_TRIP_BP
            hedge_cost = float(book["hedge_turn"].mean()) * MEASURED_ROUND_TRIP_BP
            sgn = 1.0 if float(book["book_bp"].mean()) >= 0 else -1.0
            unh = summary(np.asarray(book["book_bp"].to_list()) * sgn,
                          periods_per_year=DAYS_PER_YEAR, cost_bp=pos_cost)
            hed = summary(np.asarray(book["hedged_bp"].to_list()) * sgn,
                          periods_per_year=DAYS_PER_YEAR, cost_bp=pos_cost + hedge_cost)
            rows.append({
                "source": source, "feature": feature, "n": book.height,
                "net_dir": float(book["net_dir"].mean()),
                "abs_beta": float(book["net_beta"].abs().mean()),
                "pos_turn": float(book["pos_turn"].mean()),
                "unhedged_bp": unh.mean_bp, "unhedged_t": unh.t_stat, "unhedged_sharpe": unh.sharpe,
                "hedged_bp": hed.mean_bp, "hedged_t": hed.t_stat, "hedged_sharpe": hed.sharpe,
            })
            r = rows[-1]
            print(f"  {feature:<20s} {source:<8s} {r['n']:>5d} {r['net_dir']:>7.3f} "
                  f"{r['abs_beta']:>7.3f} {r['pos_turn']:>6.3f} {unh.mean_bp:>9.2f} "
                  f"{unh.t_stat:>6.2f} {hed.mean_bp:>8.2f} {hed.t_stat:>6.2f} {hed.sharpe:>9.2f}")

    if not rows:
        print("no scorable cells")
        return 1
    res = pl.DataFrame(rows)
    print("\n" + "=" * 100)
    print("DOES RESIDUALISING PAY IN A DIRECTIONAL BOOK? (hedged Sharpe, idio vs rawlag)")
    print("=" * 100)
    piv = (
        res.filter(pl.col("source").is_in(["rawlag", "idio"]))
        .pivot(values="hedged_sharpe", index="feature", on="source")
    )
    if {"idio", "rawlag"} <= set(piv.columns):
        piv = piv.with_columns((pl.col("idio") - pl.col("rawlag")).alias("idio_minus_rawlag"))
        print(piv.sort("idio_minus_rawlag", descending=True))
        wins = int(piv.filter(pl.col("idio_minus_rawlag") > 0).height)
        print(f"\nidio beats the information-matched control in {wins}/{piv.height} features")
        print(f"  median delta = {piv['idio_minus_rawlag'].median():+.3f}")
    surv = res.filter((pl.col("hedged_bp") > 0) & (pl.col("hedged_t").abs() > t_crit))
    print(f"\nhedged directional cells profitable and clearing t > {t_crit:.2f}: "
          f"{surv.height}/{res.height}")
    if not surv.is_empty():
        print(surv.sort(pl.col("hedged_t").abs(), descending=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
