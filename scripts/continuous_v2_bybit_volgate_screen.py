#!/usr/bin/env python3
"""Vol-gated upper_wick sizing — OOS screen of gate designs (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

upper_wick has signal on mid/low-vol fades (IC +0.20/+0.12) but is blind on high-vol names
(IC +0.04), where inverse-vol is already downsizing. So applying the wick tilt there just
adds noise / mild opposition (corr(uw, invvol weight) = -0.19). This screens whether GATING
the wick tilt to where it predicts improves the Bybit sizing OOS, vs the ungated tilt and a
hash null. OOS: standardize uw + set the vol threshold on the EARLY window, validate on LATE.
Gate designs: hard top-tercile off, hard top-half off, smooth vol-attenuated. The winner
goes to a full-ledger confirm (the arbiter). EXPLORATORY screen.
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

ANN = 365.25
K, CLIP = 0.5, 1.5  # the validated tilt strength + clip


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
        df = _load_pre(args.venue, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], cache_root)
        if df is None:
            continue
        f = enriched_features(df, float(t["entry_price"]))
        if f is None:
            continue
        rows.append({"uw": f["upper_wick_mean"], "rv": f["rv_30"], "_g": float(t["gross_trade_return"]),
                     "_net": float(t.get("net_return", t["gross_trade_return"])),
                     "_ets": int(t["entry_ts_ms"]), "_exit_day": int(t["exit_ts_ms"]) // 86_400_000,
                     "_key": f"{t['symbol']}:{t['entry_ts_ms']}"})
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]
    mu = statistics.fmean([r["uw"] for r in early])
    sd = statistics.pstdev([r["uw"] for r in early]) or 1.0
    rv_e = sorted(r["rv"] for r in early)
    thr67 = rv_e[int(len(rv_e) * 0.67)]
    thr50 = rv_e[int(len(rv_e) * 0.50)]
    rv_lo, rv_hi = rv_e[0], rv_e[-1]
    for r in rows:
        r["z"] = (r["uw"] - mu) / sd
        r["volpct"] = float(np.clip((r["rv"] - rv_lo) / (rv_hi - rv_lo + 1e-12), 0.0, 1.0))

    def tilt(members, att_fn):
        z = np.array([r["z"] * att_fn(r) for r in members])
        mult = np.clip(1.0 + K * z, 1.0 / CLIP, CLIP)
        mult = (mult / mult.mean()).tolist()
        return statistics.fmean([r["_g"] * m for r, m in zip(members, mult)]), mar_proxy(members, mult)

    base_mean = statistics.fmean([r["_g"] for r in late])
    base_mar = mar_proxy(late, [1.0] * len(late))
    designs = {
        "all (ungated)": lambda r: 1.0,
        "gate_top33_off": lambda r: 1.0 if r["rv"] <= thr67 else 0.0,
        "gate_top50_off": lambda r: 1.0 if r["rv"] <= thr50 else 0.0,
        "smooth_1minus_volpct": lambda r: 1.0 - r["volpct"],
        "smooth_sqrt": lambda r: (1.0 - r["volpct"]) ** 0.5,
    }
    res = {"venue": args.venue, "n_late": len(late), "control_mar": base_mar, "arms": {}}
    print(f"[{args.venue}] OOS vol-gated upper_wick (late n={len(late)}); control mean={base_mean:+.5f} MAR={base_mar:.2f}")
    print(f"   {'design':24s} {'mean':>9s} {'Δmean':>9s} {'MAR':>7s} {'ΔMAR':>7s}")
    for name, fn in designs.items():
        m, mr = tilt(late, fn)
        res["arms"][name] = {"mean": m, "mar": mr, "mar_delta": (mr - base_mar) if mr and base_mar else None}
        print(f"   {name:24s} {m:+9.5f} {m-base_mean:+9.5f} {mr:7.2f} {mr-base_mar:+7.2f}")
    # hash nulls: shuffle z across trades, for ungated and the best gate
    comp = [r["z"] for r in late]
    order = sorted(range(len(late)), key=lambda i: _hash01("VG:" + late[i]["_key"]))
    for i in range(len(late)):
        late[i]["z_hash"] = comp[order[i]]
    for tag, attfn in [("hash_all", lambda r: 1.0), ("hash_gate33", lambda r: 1.0 if r["rv"] <= thr67 else 0.0)]:
        zz = np.array([r["z_hash"] * attfn(r) for r in late])
        mult = np.clip(1.0 + K * zz, 1.0 / CLIP, CLIP)
        mult = (mult / mult.mean()).tolist()
        mr = mar_proxy(late, mult)
        res["arms"][tag] = {"mar": mr, "mar_delta": (mr - base_mar) if mr and base_mar else None}
        print(f"   {tag:24s} {'':>9s} {'':>9s} {mr:7.2f} {mr-base_mar:+7.2f}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "volgate_screen.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
