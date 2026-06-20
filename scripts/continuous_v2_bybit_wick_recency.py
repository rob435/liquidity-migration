#!/usr/bin/env python3
"""Does recency-weighting upper_wick help? EMA vs simple mean (operator question 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

Tests whether an EXPONENTIAL (recency-weighted) average of the per-minute upper-wick
fractions predicts the Bybit fade outcome better than the simple 120m mean. Maps the full
half-life curve (5m..very-long==simple-mean) plus linear-recency and recent-window
variants, IC vs realized gross with train/test stability. Prior: upper_wick_last15 was
WEAKER than the full mean (+0.024 vs +0.146), hinting persistent rejection > recent.
EXPLORATORY feature research.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_entry_alpha import _load_pre, spearman  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

PRE_MIN = 120


def wick_series(df):
    o, h, low, c = (df[col].to_list() for col in ("open", "high", "low", "close"))
    return [(hh - max(oo, cc)) / (hh - ll) for oo, hh, ll, cc in zip(o, h, low, c)
            if None not in (oo, hh, ll, cc) and hh > ll]


def variants(wicks):
    """Return dict of upper_wick aggregations. wicks ordered oldest->newest."""
    n = len(wicks)
    w = np.array(wicks, float)
    age = np.arange(n)[::-1]  # newest=0 ... oldest=n-1
    out = {"uw_mean": float(w.mean())}
    for hl in (5, 15, 30, 60, 120):
        decay = 0.5 ** (age / hl)
        out[f"uw_ema_h{hl}"] = float((w * decay).sum() / decay.sum())
    # linear recency (weight ~ proximity to entry)
    lin = (n - age).astype(float)
    out["uw_linear_recent"] = float((w * lin).sum() / lin.sum())
    # recent windows
    out["uw_last15"] = float(w[-15:].mean()) if n >= 15 else float(w.mean())
    out["uw_last30"] = float(w[-30:].mean()) if n >= 30 else float(w.mean())
    # OLDEST-weighted (reverse) as a falsifier — if recency mattered this should be worse
    rev = 0.5 ** ((n - 1 - age) / 30)
    out["uw_ema_OLD30"] = float((w * rev).sum() / rev.sum())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--split-ts", type=int, default=1748736000000)
    ap.add_argument("--out", default="backtest-runs/continuous_v2_bybit_entry_alpha_2026-06-20")
    args = ap.parse_args()
    cache_root = Path(args.cache_root)
    tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / args.venue / "trades.csv").filter(pl.col("side") == "short")
    rows = []
    for t in tr.to_dicts():
        df = _load_pre(args.venue, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], cache_root)
        if df is None:
            continue
        ws = wick_series(df)
        if len(ws) < 20:
            continue
        v = variants(ws)
        v["_g"] = float(t["gross_trade_return"])
        v["_mae"] = float(t["mae"])
        v["_ets"] = int(t["entry_ts_ms"])
        rows.append(v)
    feats = [k for k in rows[0] if not k.startswith("_")]
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]
    res = {"venue": args.venue, "n": len(rows), "features": {}}
    print(f"[{args.venue}] n={len(rows)} (early={len(early)} late={len(late)})  upper_wick recency variants:")
    print(f"   {'variant':18s} {'IC_gross':>9s} {'IC_early':>9s} {'IC_late':>9s} {'IC_mae':>8s} stable")
    g = [r["_g"] for r in rows]
    rank = []
    for f in feats:
        ic = spearman([r[f] for r in rows], g)
        ie = spearman([r[f] for r in early], [r["_g"] for r in early])
        il = spearman([r[f] for r in late], [r["_g"] for r in late])
        im = spearman([r[f] for r in rows], [r["_mae"] for r in rows])
        stable = bool(np.isfinite(ie) and np.isfinite(il) and np.sign(ie) == np.sign(il))
        res["features"][f] = {"ic_gross": ic, "ic_early": ie, "ic_late": il, "ic_mae": im, "stable": stable}
        rank.append((f, ic, ie, il, im, stable))
    for f, ic, ie, il, im, st in sorted(rank, key=lambda x: -abs(x[1]) if math.isfinite(x[1]) else 0):
        print(f"   {f:18s} {ic:+9.4f} {ie:+9.4f} {il:+9.4f} {im:+8.4f} {'YES' if st else 'no'}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "wick_recency.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
