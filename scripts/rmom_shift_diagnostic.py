#!/usr/bin/env python3
"""rmom re-validation step 1 — quantify the bottom-third membership shift from the
residual-momentum causal-shift fix (shift1 -> shift3).

The continuous-fade gate selects the within-ts rmom-LOW third (rmom_quantile, default 0.33)
by residual_momentum. The shift1->shift3 fix re-bases residual_momentum *values* (the
quantile is a rank fraction, so the *fraction* selected is unchanged, but *which names*
land in the bottom third reshuffles). This script recomputes residual_return ONCE on a
research root, then computes the bottom-third membership under both shifts and reports how
much it flips — so the operator can size the rmom_quantile recalibration before re-running
the full gated backtest.

    .venv/bin/python scripts/rmom_shift_diagnostic.py [ROOT] [--quantile 0.33] [--lookback-days 450]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import polars as pl  # noqa: E402

from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402

COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]
RMOM_WINDOW = 7


def _rmom(resid: pl.DataFrame, shift: int) -> pl.DataFrame:
    return (
        resid.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("residual_return")
            .rolling_sum(window_size=RMOM_WINDOW, min_samples=4)
            .shift(shift)
            .over("symbol")
            .alias("rmom")
        )
        .select("symbol", "ts_ms", "rmom")
        .drop_nulls("rmom")
    )


def _bottom_third(rmom: pl.DataFrame, quantile: float) -> pl.DataFrame:
    # gate: keep within-ts residual_momentum rank fraction <= quantile (rmom-LOW = selected).
    return rmom.with_columns(
        (pl.col("rmom").rank(method="ordinal").over("ts_ms") / pl.col("rmom").count().over("ts_ms"))
        .alias("rank_frac")
    ).with_columns((pl.col("rank_frac") <= quantile).alias("selected"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="~/SHARED_DATA/bybit_full_pit")
    ap.add_argument("--quantile", type=float, default=0.33)
    ap.add_argument("--lookback-days", type=int, default=450)
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    start = (datetime.now(timezone.utc).date() - timedelta(days=args.lookback_days)).isoformat()
    kname = "klines_1h" if (root / "klines_1h").is_dir() else "event_demo_klines_1h"
    print(f"[{root.name}] factor panel + common4 residuals [{start}..{end}) klines={kname} ...", flush=True)
    panel = build_factor_panel(root, start=start, end=end, klines_dataset=kname)
    if panel.is_empty():
        print("EMPTY panel — no data for this window/root.")
        return 1
    _fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)
    resid = resid.sort(["symbol", "ts_ms"]).select("symbol", "ts_ms", "residual_return")

    sel1 = _bottom_third(_rmom(resid, 1), args.quantile).select("symbol", "ts_ms", "selected")
    sel3 = _bottom_third(_rmom(resid, 3), args.quantile).select("symbol", "ts_ms", "selected")
    joined = sel1.join(sel3, on=["symbol", "ts_ms"], how="inner", suffix="_3")
    n = joined.height
    if n == 0:
        print("no overlapping (symbol, ts) rows between the two shifts.")
        return 1
    both = joined.filter(pl.col("selected") & pl.col("selected_3")).height
    only1 = joined.filter(pl.col("selected") & ~pl.col("selected_3")).height
    only3 = joined.filter(~pl.col("selected") & pl.col("selected_3")).height
    sel1_n = both + only1
    sel3_n = both + only3
    union = both + only1 + only3
    # per-ts Jaccard of the selected sets, averaged
    per_ts = (
        joined.group_by("ts_ms")
        .agg(
            (pl.col("selected") & pl.col("selected_3")).sum().alias("both"),
            (pl.col("selected") | pl.col("selected_3")).sum().alias("union"),
        )
        .filter(pl.col("union") > 0)
        .with_columns((pl.col("both") / pl.col("union")).alias("jaccard"))
    )
    print("\n=== rmom bottom-third membership: shift1 (old, look-ahead) vs shift3 (fixed) ===")
    print(f"window: {start}..{end}  quantile={args.quantile}  klines={kname}")
    print(f"(symbol,ts) rows compared: {n:,}   distinct ts: {per_ts.height}")
    print(f"selected under shift1: {sel1_n:,}   under shift3: {sel3_n:,}")
    print(f"  stayed selected (both): {both:,}")
    print(f"  flipped OUT (shift1 only): {only1:,}   flipped IN (shift3 only): {only3:,}")
    churn = (only1 + only3) / union if union else 0.0
    print(f"  symmetric churn of the selected set: {churn:6.1%}  (fraction of the union that flips)")
    print(f"  mean per-ts Jaccard(shift1, shift3 selected sets): {per_ts['jaccard'].mean():.3f}")
    retained = both / sel1_n if sel1_n else 0.0
    print(f"  fraction of the OLD (shift1) selection retained under shift3: {retained:6.1%}")
    print("\nInterpretation: high churn / low Jaccard => the gate's selection moved a lot, so the")
    print("rmom_quantile=0.33 choice and the gated-backtest MAR verdict genuinely need re-running")
    print("(runbook: docs/audit_2026-06-03/rmom_revalidation_runbook.md). Low churn => 0.33 likely holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
