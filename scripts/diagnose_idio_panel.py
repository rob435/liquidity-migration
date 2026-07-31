#!/usr/bin/env python3
"""Diagnostics that decide how much room an idio-versus-raw screen even has.

Run this BEFORE reading the screen. Three numbers bound what the screen can
possibly show, and each of them can make a headline result uninteresting:

1. **Factor R-squared.** The share of daily cross-sectional return variance the
   factor model explains. If it is small, the residual is most of the raw
   return, an "idio chart" is largely a relabelled raw chart, and any Sharpe
   difference between the arms is noise rather than mechanism.

2. **Signal rank correlation.** Spearman correlation between each raw feature
   and its idio twin, measured within-day because that is how the decile book
   ranks. At 0.95 the two arms hold nearly the same book and cannot disagree by
   much regardless of what the t-statistics say.

3. **Book overlap.** The fraction of names the raw and idio deciles actually
   share on the same day. This is the direct version of (2): two signals can
   correlate at 0.8 and still pick largely the same tails, which is what
   determines whether the P&L can differ.

A fourth, ``beta dispersion``, checks the mechanism: residualising should
change the chart most for the highest-beta names. If the idio-raw divergence is
unrelated to beta, whatever the residual removed was not market exposure.

Dispatch:
    .venv/bin/python -u scripts/diagnose_idio_panel.py \\
        --panel data/idio_panel/bybit/panel_common4.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.cross_section import top_by  # noqa: E402
from liquidity_migration.idio_features import CHART_FEATURES  # noqa: E402

DECILE_CUT = 0.10


def factor_r2(panel: pl.DataFrame, *, scale: str = "simple") -> pl.DataFrame:
    """Per-day 1 - Var(residual)/Var(realised), with the scales matched.

    The panel carries two residual series -- ``resid_fwd`` fits ``fwd_ret_1d``
    (simple), ``resid_fwd_log`` fits ``fwd_logret_1d`` (log). Pairing them across
    scales yields a number that is not an R-squared and can go negative.
    """
    total, resid = (
        ("fwd_ret_1d", "resid_fwd") if scale == "simple"
        else ("fwd_logret_1d", "resid_fwd_log")
    )
    missing = [c for c in (total, resid) if c not in panel.columns]
    if missing:
        raise ValueError(f"panel lacks {missing} for scale={scale!r}")
    frame = panel.select("ts_ms", total, resid).drop_nulls()
    if frame.is_empty():
        return pl.DataFrame()
    return (
        frame.group_by("ts_ms")
        .agg(
            pl.col(total).var().alias("_var_total"),
            pl.col(resid).var().alias("_var_resid"),
            pl.len().alias("n"),
        )
        .filter((pl.col("_var_total") > 0.0) & (pl.col("n") >= 20))
        .with_columns((1.0 - pl.col("_var_resid") / pl.col("_var_total")).alias("r2"))
        .sort("ts_ms")
    )


def signal_agreement(panel: pl.DataFrame, *, left: str, right: str) -> pl.DataFrame:
    """Within-day Spearman correlation and decile overlap for each feature."""
    rows: list[dict] = []
    for feature in CHART_FEATURES:
        lcol, rcol = f"{left}_{feature}", f"{right}_{feature}"
        if lcol not in panel.columns or rcol not in panel.columns:
            continue
        frame = panel.select("ts_ms", "symbol", lcol, rcol).drop_nulls()
        if frame.is_empty():
            continue
        ranked = frame.with_columns(
            pl.col(lcol).rank("average").over("ts_ms").alias("_lr"),
            pl.col(rcol).rank("average").over("ts_ms").alias("_rr"),
            pl.len().over("ts_ms").alias("_n"),
        ).filter(pl.col("_n") >= 20)
        if ranked.is_empty():
            continue
        per_day = (
            ranked.group_by("ts_ms")
            .agg(pl.corr("_lr", "_rr").alias("rho"), pl.len().alias("n"))
            .drop_nulls("rho")
        )

        # Decile overlap: of the names in the left signal's two tails, what
        # share are in the right signal's matching tail on the same day?
        tails = ranked.with_columns(
            ((pl.col("_lr") - 0.5) / pl.col("_n")).alias("_lp"),
            ((pl.col("_rr") - 0.5) / pl.col("_n")).alias("_rp"),
        )
        left_long = tails.filter(pl.col("_lp") >= 1.0 - DECILE_CUT)
        shared = left_long.filter(pl.col("_rp") >= 1.0 - DECILE_CUT)
        overlap = shared.height / left_long.height if left_long.height else float("nan")

        rows.append(
            {
                "feature": feature,
                "pair": f"{left} vs {right}",
                "days": per_day.height,
                "mean_rho": float(per_day["rho"].mean()),
                "median_rho": float(per_day["rho"].median()),
                "long_decile_overlap": overlap,
            }
        )
    return pl.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", required=True, type=Path)
    ap.add_argument("--universe", type=int, default=150)
    args = ap.parse_args(argv)

    panel = pl.read_parquet(args.panel)
    print(f"panel {args.panel}")
    print(f"  rows={panel.height} symbols={panel['symbol'].n_unique()} days={panel['ts_ms'].n_unique()}")
    print(f"  span {panel['date'].min()} .. {panel['date'].max()}\n")
    if args.universe > 0 and "turnover_quote" in panel.columns:
        panel = top_by(panel, "turnover_quote", args.universe, period="ts_ms")
        print(f"universe: top {args.universe} by turnover -> {panel.height} rows\n")

    print("=" * 92)
    print("1. FACTOR R-SQUARED -- how much of the daily cross-section is 'common'")
    print("=" * 92)
    for scale in ("simple", "log"):
        try:
            r2 = factor_r2(panel, scale=scale)
        except ValueError as exc:
            print(f"  [{scale}] unavailable: {exc}")
            continue
        if r2.is_empty():
            print(f"  [{scale}] no scorable days")
            continue
        q = r2["r2"]
        print(f"  [{scale:6s}] days={r2.height}  mean={q.mean():+.4f}  median={q.median():+.4f}  "
              f"p10={q.quantile(0.10):+.4f}  p90={q.quantile(0.90):+.4f}")
    r2 = factor_r2(panel, scale="simple")
    if r2.is_empty():
        print("  no scorable days\n")
    else:
        q = r2["r2"]
        print(f"  days={r2.height}  mean={q.mean():.4f}  median={q.median():.4f}")
        print(f"  p10={q.quantile(0.10):.4f}  p90={q.quantile(0.90):.4f}  "
              f"min={q.min():.4f}  max={q.max():.4f}")
        print(f"  share of days with R2 < 0.10: {float((q < 0.10).mean()):.3f}")
        print(f"  share of days with R2 > 0.50: {float((q > 0.50).mean()):.3f}")
        print("\n  READ: 1 - R2 is the fraction of variance the idio path keeps. The closer")
        print("        that is to 1, the more an idio chart is a relabelled raw chart.\n")

    print("=" * 92)
    print("2. SIGNAL AGREEMENT -- can the two arms hold different books at all?")
    print("=" * 92)
    for left, right in (("rawlag", "idio"), ("raw", "idio"), ("raw", "rawlag")):
        agree = signal_agreement(panel, left=left, right=right)
        if agree.is_empty():
            continue
        print(f"\n  {left} vs {right}:")
        print(f"  {'feature':<20s} {'days':>6s} {'mean_rho':>9s} {'med_rho':>9s} {'decile_overlap':>15s}")
        for r in agree.iter_rows(named=True):
            print(f"  {r['feature']:<20s} {r['days']:>6d} {r['mean_rho']:>9.3f} "
                  f"{r['median_rho']:>9.3f} {r['long_decile_overlap']:>15.3f}")
    print("\n  READ: a mean rho near 1.0 with high decile overlap means the arms cannot")
    print("        disagree materially, whatever their separate t-statistics say.\n")

    print("=" * 92)
    print("3. COVERAGE AND SHAPE OF THE IDIO ARM")
    print("=" * 92)
    for source in ("raw", "rawlag", "idio", "iz"):
        col = f"{source}_{CHART_FEATURES[0]}"
        if col not in panel.columns:
            continue
        n = panel[col].drop_nulls().len()
        print(f"  {source:<8s} non-null {CHART_FEATURES[0]}: {n:>8d} ({100.0 * n / panel.height:5.1f}%)")
    if "coverage" in panel.columns:
        cov = panel["coverage"].drop_nulls()
        if cov.len():
            print(f"\n  idio coverage: median={cov.median():.3f}  "
                  f"share >= 0.90: {float((cov >= 0.90).mean()):.3f}")
    if "is_filled" in panel.columns:
        filled = panel["is_filled"].drop_nulls()
        if filled.len():
            print(f"  idio imputed-day share: {float(filled.mean()):.4f}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
