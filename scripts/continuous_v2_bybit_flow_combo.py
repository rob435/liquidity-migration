#!/usr/bin/env python3
"""Bybit upper_wick + flow-absorption COMBO entry signal (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

Taker-flow 'absorption' (price rose without aggressive net buying = hollow pop) has IC
+0.163 vs gross, BEATS upper_wick (+0.146), and is only ~0.10 correlated with it. So they
are near-orthogonal exhaustion signals (flow support vs candle shape). This tests the COMBO
z(upper_wick)+z(absorption): IC vs gross AND vs MAE (clean vs risk-coupled?), and OOS sizing
(standardize on early, validate on late) + hash null + MAR proxy, vs each feature alone.
EXPLORATORY screen; a winner goes to full-ledger confirmation.
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

from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features, spearman  # noqa: E402
from continuous_v2_bybit_takerflow import _load_flow, flow_features  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

ANN = 365.25


def _hash01(s):
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def mar_proxy(members, mult):
    daily = {}
    for r, m in zip(members, mult):
        daily[r["_exit_day"]] = daily.get(r["_exit_day"], 0.0) + r["_net"] * m
    days = sorted(daily)
    if len(days) < 5:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
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
        fl = _load_flow(str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"])
        pre = _load_pre(args.venue, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], cache_root)
        if fl is None or pre is None:
            continue
        ef = enriched_features(pre, float(t["entry_price"]))
        c = pre["close"].to_list()
        pchg = (c[-1] / c[0] - 1.0) if (c[0] and c[-1]) else 0.0
        ff = flow_features(fl, pchg)
        if ef is None or ff is None:
            continue
        rows.append({"uw": ef["upper_wick_mean"], "ab": ff["absorption"],
                     "run_up": ef["run_up_120"], "rv": ef["rv_30"], "_g": float(t["gross_trade_return"]),
                     "_mae": float(t["mae"]), "_net": float(t.get("net_return", t["gross_trade_return"])),
                     "_ets": int(t["entry_ts_ms"]), "_exit_day": int(t["exit_ts_ms"]) // 86_400_000,
                     "_key": f"{t['symbol']}:{t['entry_ts_ms']}"})
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]

    def stdz(name):
        mu = statistics.fmean([r[name] for r in early])
        sd = statistics.pstdev([r[name] for r in early]) or 1.0
        return mu, sd

    mus = {f: stdz(f) for f in ("uw", "ab")}
    for r in rows:
        r["z_uw"] = (r["uw"] - mus["uw"][0]) / mus["uw"][1]
        r["z_ab"] = (r["ab"] - mus["ab"][0]) / mus["ab"][1]
        r["z_combo"] = (r["z_uw"] + r["z_ab"]) / 2.0
    # residualize absorption on the RISK features (run_up, rv): fit OLS on EARLY, apply to all,
    # to strip the tail-coupling and keep the orthogonal-to-risk flow alpha.
    Xe = np.array([[1.0, r["run_up"], r["rv"]] for r in early])
    ye = np.array([r["ab"] for r in early])
    beta, *_ = np.linalg.lstsq(Xe, ye, rcond=None)
    ab_mu = statistics.fmean([r["ab"] - (beta[0] + beta[1] * r["run_up"] + beta[2] * r["rv"]) for r in early])
    ab_sd = statistics.pstdev([r["ab"] - (beta[0] + beta[1] * r["run_up"] + beta[2] * r["rv"]) for r in early]) or 1.0
    for r in rows:
        resid = r["ab"] - (beta[0] + beta[1] * r["run_up"] + beta[2] * r["rv"])
        r["z_abresid"] = (resid - ab_mu) / ab_sd
        r["z_combo2"] = (r["z_uw"] + r["z_abresid"]) / 2.0
    g = [r["_g"] for r in rows]
    mae = [r["_mae"] for r in rows]
    res = {"venue": args.venue, "n": len(rows), "ic": {}, "sizing_oos": {}}
    print(f"[{args.venue}] n={len(rows)}  COMBO upper_wick + flow-absorption")
    print("   IC vs gross / vs MAE (clean = high gross IC, ~0 MAE IC):")
    for f in ("z_uw", "z_ab", "z_combo", "z_abresid", "z_combo2"):
        icg = spearman([r[f] for r in rows], g)
        icm = spearman([r[f] for r in rows], mae)
        icl = spearman([r[f] for r in late], [r["_g"] for r in late])
        res["ic"][f] = {"ic_gross": icg, "ic_mae": icm, "ic_gross_late": icl}
        print(f"     {f:10s} IC_gross={icg:+.4f}  IC_late={icl:+.4f}  IC_mae={icm:+.4f}")

    # OOS sizing (late), mean-1 tilt k=0.5 clip[1/3,3] (the refined clip), + hash
    base_mean = statistics.fmean([r["_g"] for r in late])
    base_mar = mar_proxy(late, [1.0] * len(late))

    def size(scorekey, members):
        z = np.array([r[scorekey] for r in members])
        mult = np.clip(1.0 + 0.5 * z, 1.0 / 3.0, 3.0)
        mult = (mult / mult.mean()).tolist()
        return statistics.fmean([r["_g"] * m for r, m in zip(members, mult)]), mar_proxy(members, mult)

    print(f"   OOS sizing (late n={len(late)}); control mean={base_mean:+.5f} MAR={base_mar:.2f}:")
    for f in ("z_uw", "z_ab", "z_combo", "z_abresid", "z_combo2"):
        m, mr = size(f, late)
        res["sizing_oos"][f] = {"mean": m, "mar": mr, "mar_delta": (mr - base_mar) if mr and base_mar else None}
        print(f"     size_{f:10s} mean={m:+.5f} (Δ{m-base_mean:+.5f})  MAR={mr:.2f} (Δ{mr-base_mar:+.2f})")
    # hash null for combo
    comp = [r["z_combo"] for r in late]
    order = sorted(range(len(late)), key=lambda i: _hash01("FC:" + late[i]["_key"]))
    for i in range(len(late)):
        late[i]["z_hash"] = comp[order[i]]
    mh, mrh = size("z_hash", late)
    res["sizing_oos"]["z_hash"] = {"mean": mh, "mar": mrh, "mar_delta": (mrh - base_mar) if mrh and base_mar else None}
    print(f"     size_z_hash    mean={mh:+.5f} (Δ{mh-base_mean:+.5f})  MAR={mrh:.2f} (Δ{mrh-base_mar:+.2f})")
    print(f"\n   combo beats hash on MAR? {res['sizing_oos']['z_combo']['mar'] > mrh}  "
          f"combo beats upper_wick alone? {res['sizing_oos']['z_combo']['mar'] > res['sizing_oos']['z_uw']['mar']}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "flow_combo.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
