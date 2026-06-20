#!/usr/bin/env python3
"""Bybit entry-alpha AB tests with OOS validation (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

Stage 2. Stage 1 ranked causal pre-entry features by IC vs realized gross on Bybit. The
clean ENTRY edge is the EXHAUSTION cluster — features that lift gross WITHOUT raising MAE
(upper_wick_mean, vol_climax, ext_from_vwap, pv_divergence) — vs the "scary=best" cluster
(rv_30/range/run_up) which lifts gross by taking more risk (sizing's job, not entry's).

This tests whether selecting/sizing entries on these features improves the Bybit
equal-weight mean trade return OUT OF SAMPLE (features standardized on the EARLY window,
rule applied to the LATE window), vs a hash null. Honest mining: the composite is
THEORY-driven (exhaustion), the verdict is the LATE/OOS number, every rule is checked
against a same-distribution hash.

Arms (Bybit V2_CONTROL shorts, equal weight, realized gross):
- control:            all trades.
- admit_top67_<feat>: keep the top 2/3 by feature (drop the worst exhaustion tercile).
- size_<feat>:        mean-1 tilt by feature z (gross-neutral).
- exhaustion composite (EQ-weight z of the 4 clean exhaustion features): admit + size.
- hash nulls for the composite admit/size.
EXPLORATORY. Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import hashlib
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

EXHAUSTION = ["upper_wick_mean", "vol_climax", "ext_from_vwap", "pv_divergence"]
SINGLES = ["upper_wick_mean", "vol_climax", "rv_30", "run_up_120", "range_expansion"]


def _hash01(s):
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _mean(xs):
    return statistics.fmean(xs) if xs else float("nan")


def _winrate(xs):
    return sum(1 for x in xs if x > 0) / len(xs) if xs else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_bybit_entry_alpha_2026-06-20")
    ap.add_argument("--split-ts", type=int, default=1748736000000)  # 2025-06-01
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
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
        f["_gross"] = float(t["gross_trade_return"])
        f["_net"] = float(t.get("net_return", t["gross_trade_return"]))
        f["_ets"] = int(t["entry_ts_ms"])
        f["_exit_day"] = int(t["exit_ts_ms"]) // 86_400_000
        f["_key"] = f"{t['symbol']}:{t['entry_ts_ms']}"
        rows.append(f)
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]
    # standardize features on EARLY window (mean/std), apply to LATE
    stats = {f: (_mean([r[f] for r in early]), statistics.pstdev([r[f] for r in early]) or 1.0)
             for f in set(EXHAUSTION + SINGLES)}

    def z(r, f):
        m, s = stats[f]
        return (r[f] - m) / s

    def composite(r):
        return sum(z(r, f) for f in EXHAUSTION) / len(EXHAUSTION)

    ANN = 365.25

    def mar_proxy(members, mult=None):
        """Daily-equity MAR proxy on the LATE window for a subset/sizing of trades (equal base)."""
        daily = {}
        for i, r in enumerate(members):
            w = 1.0 if mult is None else mult[i]
            daily[r["_exit_day"]] = daily.get(r["_exit_day"], 0.0) + r["_net"] * w
        days = sorted(daily)
        if len(days) < 5:
            return None
        eq = 1.0
        peak = 1.0
        mdd = 0.0
        for d in days:
            eq *= 1.0 + daily[d] / max(1, len(members)) * len(members) / 100.0  # scale to ~book units
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1.0)
        total = eq - 1.0
        yrs = max((days[-1] - days[0]) / ANN, 1e-9)
        return (total / yrs) / abs(mdd) if abs(mdd) > 1e-12 else None

    base_late = [r["_gross"] for r in late]
    res = {"venue": args.venue, "n_early": len(early), "n_late": len(late), "split_ts": args.split_ts,
           "control_late_mean": _mean(base_late), "control_late_winrate": _winrate(base_late),
           "control_late_mar": mar_proxy(late), "arms": {}}

    def admit(score_fn, keep_frac):
        scored = sorted(late, key=lambda r: -score_fn(r))
        keep = scored[:int(len(scored) * keep_frac)]
        return {"mean": _mean([r["_gross"] for r in keep]), "n": len(keep), "mar": mar_proxy(keep)}

    def size(score_fn):
        zs = np.array([score_fn(r) for r in late])
        sd = zs.std() or 1.0
        mult = np.clip(1.0 + 0.5 * (zs / sd), 0.5, 1.5)
        mult = (mult / mult.mean()).tolist()
        return {"mean": _mean([float(r["_gross"]) * m for r, m in zip(late, mult)]), "n": len(late),
                "mar": mar_proxy(late, mult)}

    for f in SINGLES:
        res["arms"][f"admit67_{f}"] = admit(lambda r, ff=f: z(r, ff), 0.67)
        res["arms"][f"size_{f}"] = size(lambda r, ff=f: z(r, ff))
    res["arms"]["admit67_EXHAUST"] = admit(composite, 0.67)
    res["arms"]["admit50_EXHAUST"] = admit(composite, 0.50)
    res["arms"]["size_EXHAUST"] = size(composite)
    comp = [composite(r) for r in late]
    order = sorted(range(len(late)), key=lambda i: _hash01(f"H:{late[i]['_key']}"))
    permuted = {late[i]["_key"]: comp[order[i]] for i in range(len(late))}
    res["arms"]["admit67_HASH"] = admit(lambda r: permuted[r["_key"]], 0.67)
    res["arms"]["admit50_HASH"] = admit(lambda r: permuted[r["_key"]], 0.50)
    res["arms"]["size_HASH"] = size(lambda r: permuted[r["_key"]])

    ctrl = res["control_late_mean"]
    cmar = res["control_late_mar"]
    for name, a in res["arms"].items():
        a["delta_vs_control"] = a["mean"] - ctrl
        a["mar_delta"] = (a["mar"] - cmar) if (a.get("mar") is not None and cmar is not None) else None
    (out / "bybit_entry_ab.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(f"[{args.venue}] OOS (late) n={len(late)}  control mean trade={ctrl:+.5f}  control MAR_proxy={cmar}")
    print(f"   {'arm':24s} {'mean':>9s} {'Δmean':>9s} {'n':>5s} {'MAR':>7s} {'ΔMAR':>8s}")
    for name, a in sorted(res["arms"].items(), key=lambda kv: -kv[1]["delta_vs_control"]):
        marv = f"{a['mar']:.2f}" if a.get("mar") is not None else "n/a"
        dmar = f"{a['mar_delta']:+.2f}" if a.get("mar_delta") is not None else "n/a"
        print(f"   {name:24s} {a['mean']:+9.5f} {a['delta_vs_control']:+9.5f} {a['n']:5d} {marv:>7s} {dmar:>8s}")
    # verdict: composite must beat control AND its hash, OOS
    ce = res["arms"]
    print("\n   exhaustion composite beats hash (OOS)? "
          f"admit: {ce['admit67_EXHAUST']['mean'] > ce['admit67_HASH']['mean']}  "
          f"size: {ce['size_EXHAUST']['mean'] > ce['size_HASH']['mean']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
