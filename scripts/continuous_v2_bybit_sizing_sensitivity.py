#!/usr/bin/env python3
"""Bybit upper_wick sizing — SENSITIVITY sweep (operator question 2026-06-20: why not more sensitive?).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

Sweeps the tilt strength k and clip width of the upper_wick mean-1 size multiplier
mult = clip(1 + k*z, 1/c, c), z = upper_wick standardized on the EARLY window. Reports the
MAR proxy + mean trade on EARLY (in-sample) and LATE (out-of-sample) so we can see whether
more sensitivity helps OOS or only IS (the overfitting tell). Honest answer to "why not
crank it up". EXPLORATORY screen.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

ANN = 365.25


def mar_proxy(members, mult):
    daily = {}
    for r, m in zip(members, mult):
        daily[r["_exit_day"]] = daily.get(r["_exit_day"], 0.0) + r["_net"] * m
    days = sorted(daily)
    if len(days) < 5:
        return None
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for d in days:
        eq *= 1.0 + daily[d] / 100.0
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    total = eq - 1.0
    yrs = max((days[-1] - days[0]) / ANN, 1e-9)
    return (total / yrs) / abs(mdd) if abs(mdd) > 1e-12 else None


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
        f = enriched_features(df, float(t["entry_price"]))
        if f is None:
            continue
        rows.append({"uw": f["upper_wick_mean"], "_gross": float(t["gross_trade_return"]),
                     "_net": float(t.get("net_return", t["gross_trade_return"])),
                     "_ets": int(t["entry_ts_ms"]), "_exit_day": int(t["exit_ts_ms"]) // 86_400_000})
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]
    mu = statistics.fmean([r["uw"] for r in early])
    sd = statistics.pstdev([r["uw"] for r in early]) or 1.0

    def sweep(members, k, c):
        z = np.array([(r["uw"] - mu) / sd for r in members])
        mult = np.clip(1.0 + k * z, 1.0 / c, c)
        mult = (mult / mult.mean()).tolist()
        mean_trade = statistics.fmean([r["_gross"] * m for r, m in zip(members, mult)])
        frac_clipped = float(np.mean((np.array(mult) <= 1.0001 / c) | (np.array(mult) >= 0.9999 * c)))
        return mean_trade, mar_proxy(members, mult), frac_clipped

    base_e_mar = mar_proxy(early, [1.0] * len(early))
    base_l_mar = mar_proxy(late, [1.0] * len(late))
    res = {"venue": args.venue, "n_early": len(early), "n_late": len(late),
           "control": {"early_mar": base_e_mar, "late_mar": base_l_mar}, "k_sweep": [], "clip_sweep": []}
    print(f"[{args.venue}] control MAR proxy: IS(early)={base_e_mar:.2f}  OOS(late)={base_l_mar:.2f}")

    # (A) fine, broad k sweep at FIXED clip (only the tilt strength varies)
    FIXED_CLIP = 3.0
    print(f"\n   (A) k sweep, clip fixed at {FIXED_CLIP} (tilt strength is the only variable):")
    print(f"   {'k':>5s} | {'IS_mean':>8s} {'IS_MAR':>7s} {'ΔIS':>6s} | {'OOS_mean':>8s} {'OOS_MAR':>8s} {'ΔOOS':>7s} {'%clip':>6s}")
    for k in [0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
        em, emar, _ = sweep(early, k, FIXED_CLIP)
        lm, lmar, fc = sweep(late, k, FIXED_CLIP)
        row = {"k": k, "clip": FIXED_CLIP, "is_mean": em, "is_mar": emar, "oos_mean": lm, "oos_mar": lmar,
               "d_oos_mar": (lmar - base_l_mar) if lmar and base_l_mar else None, "oos_frac_clipped": fc}
        res["k_sweep"].append(row)
        print(f"   {k:5.2f} | {em:+8.4f} {emar:7.2f} {emar-base_e_mar:+6.2f} | "
              f"{lm:+8.4f} {lmar:8.2f} {lmar-base_l_mar:+7.2f} {fc:6.1%}")

    # (B) clip sweep at FIXED k=0.5
    print("\n   (B) clip sweep, k fixed at 0.5:")
    print(f"   {'clip':>5s} | {'OOS_mean':>8s} {'OOS_MAR':>8s} {'ΔOOS':>7s} {'%clip':>6s}")
    for c in [1.25, 1.5, 2.0, 3.0, 5.0, 10.0]:
        lm, lmar, fc = sweep(late, 0.5, c)
        res["clip_sweep"].append({"k": 0.5, "clip": c, "oos_mean": lm, "oos_mar": lmar, "oos_frac_clipped": fc})
        print(f"   {c:5.2f} | {lm:+8.4f} {lmar:8.2f} {lmar-base_l_mar:+7.2f} {fc:6.1%}")

    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "sizing_sensitivity.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
