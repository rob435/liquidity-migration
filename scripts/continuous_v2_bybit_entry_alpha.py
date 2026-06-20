#!/usr/bin/env python3
"""Bybit entry-alpha research — creative causal pre-entry features (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

Focus: BYBIT only, perfect the ENTRY. Theory of the fade: we short a pop and profit on
reversion, so the BEST entries are where the pop is EXHAUSTING (climactic, rejected,
over-extended, decelerating into a wall) rather than trending. This builds a rich library
of causal pre-entry 1m features capturing exhaustion / over-extension / climax, ranks them
by IC vs realized gross with sub-period stability, then (stage 2) AB-tests admission /
sizing / combos with a TIME SPLIT (fit on the early window, validate on the late window)
and hash nulls — honest mining, not curve-fitting.

Stage 1 (this script): feature IC + stability + blow-up association on the Bybit
V2_CONTROL fades (uptrend-gated = the deployed entry set; in the 1m cache).
EXPLORATORY. Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

MS_MIN = 60_000
PRE_MIN = 120


def spearman(x, y):
    a, b = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return float("nan")
    ar = np.argsort(np.argsort(a[m])).astype(float)
    br = np.argsort(np.argsort(b[m])).astype(float)
    ar -= ar.mean()
    br -= br.mean()
    den = math.sqrt((ar * ar).sum() * (br * br).sum())
    return float((ar * br).sum() / den) if den > 0 else float("nan")


def _load_pre(venue, symbol, entry_ts, entry_date, cache_root):
    from datetime import date, timedelta
    d0 = date.fromisoformat(str(entry_date)[:10]) - timedelta(days=1)
    frames = []
    for k in range(3):
        p = cache_root / venue / "klines_1m" / f"date={(d0 + timedelta(days=k)).isoformat()}" / f"symbol={symbol}" / "data.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p, columns=["ts_ms", "open", "high", "low", "close", "volume_base"]))
    if not frames:
        return None
    df = pl.concat(frames).filter((pl.col("ts_ms") >= entry_ts - PRE_MIN * MS_MIN) & (pl.col("ts_ms") < entry_ts)).unique("ts_ms").sort("ts_ms")
    return df if df.height >= 20 else None


def enriched_features(df, entry_price):
    o = df["open"].to_list()
    h = df["high"].to_list()
    low = df["low"].to_list()
    c = df["close"].to_list()
    v = [x if x is not None else 0.0 for x in df["volume_base"].to_list()]
    n = len(c)
    last = c[-1]
    if last is None or last <= 0:
        return None

    def ret(a, b):
        return (c[b] / c[a] - 1.0) if (c[a] and c[b]) else 0.0

    win_low = min(x for x in low if x is not None)
    win_hi = max(x for x in h if x is not None)
    logr = [math.log(c[i] / c[i - 1]) for i in range(1, n) if c[i] and c[i - 1]]
    # upper-wick fractions
    wicks = [(hh - max(oo, cc)) / (hh - ll) for oo, hh, ll, cc in zip(o, h, low, c)
             if None not in (oo, hh, ll, cc) and hh > ll]
    # vwap over window
    tv = sum(cc * vv for cc, vv in zip(c, v) if cc is not None)
    tvol = sum(vv for cc, vv in zip(c, v) if cc is not None)
    vwap = tv / tvol if tvol > 0 else last
    # consecutive up minutes into entry
    consec = 0
    for i in range(n - 1, 0, -1):
        if c[i] is not None and c[i - 1] is not None and c[i] > c[i - 1]:
            consec += 1
        else:
            break
    # volume: last-15 mean vs prior baseline (climax)
    v15 = statistics.fmean(v[-15:]) if len(v) >= 15 else statistics.fmean(v)
    vbase = statistics.fmean(v[:-15]) if len(v) > 15 else statistics.fmean(v)
    # price-volume divergence: corr(price change, volume) over window (neg = up on falling vol = weak)
    dc = [c[i] - c[i - 1] for i in range(1, n) if c[i] is not None and c[i - 1] is not None]
    vv = v[1:1 + len(dc)]
    pv_corr = float(np.corrcoef(dc, vv)[0, 1]) if len(dc) > 5 and np.std(dc) > 0 and np.std(vv) > 0 else 0.0
    return {
        "ret_last15": ret(max(0, n - 16), n - 1),
        "ret_last30": ret(max(0, n - 31), n - 1),
        "run_up_120": (last / win_low - 1.0) if win_low else 0.0,
        "accel": ret(max(0, n - 31), n - 1) - ret(max(0, n - 61), max(0, n - 31)),  # parabolic if >0
        "dist_from_hi": (win_hi - last) / last,
        "upper_wick_mean": float(np.mean(wicks)) if wicks else 0.0,
        "upper_wick_last15": float(np.mean(wicks[-15:])) if len(wicks) >= 15 else (float(np.mean(wicks)) if wicks else 0.0),
        "rv_30": float(np.std(logr[-30:])) if len(logr) > 5 else 0.0,
        "ext_from_vwap": (last - vwap) / vwap if vwap else 0.0,  # over-extension above vwap
        "vol_climax": (v15 / vbase - 1.0) if vbase > 0 else 0.0,  # volume spike into entry
        "pv_divergence": -pv_corr,  # high = price rose on FALLING volume (exhaustion)
        "consec_up": float(consec),
        "range_expansion": (win_hi - win_low) / last,
    }


FEATS = ["ret_last15", "ret_last30", "run_up_120", "accel", "dist_from_hi", "upper_wick_mean",
         "upper_wick_last15", "rv_30", "ext_from_vwap", "vol_climax", "pv_divergence", "consec_up", "range_expansion"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_bybit_entry_alpha_2026-06-20")
    ap.add_argument("--split-ts", type=int, default=1748736000000, help="train/test boundary ms (default 2025-06-01)")
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
        f["_mae"] = float(t["mae"])
        f["_ets"] = int(t["entry_ts_ms"])
        rows.append(f)
    n = len(rows)
    gross = [r["_gross"] for r in rows]
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]
    res = {"venue": args.venue, "n": n, "n_early": len(early), "n_late": len(late), "features": {}}
    for f in FEATS:
        ic_all = spearman([r[f] for r in rows], gross)
        ic_e = spearman([r[f] for r in early], [r["_gross"] for r in early]) if len(early) > 40 else float("nan")
        ic_l = spearman([r[f] for r in late], [r["_gross"] for r in late]) if len(late) > 40 else float("nan")
        ic_mae = spearman([r[f] for r in rows], [r["_mae"] for r in rows])
        res["features"][f] = {"ic_gross": ic_all, "ic_gross_early": ic_e, "ic_gross_late": ic_l, "ic_mae": ic_mae,
                              "stable": bool(np.isfinite(ic_e) and np.isfinite(ic_l) and np.sign(ic_e) == np.sign(ic_l))}
    (out / "entry_feature_ic.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    ranked = sorted(res["features"].items(), key=lambda kv: -abs(kv[1]["ic_gross"]) if np.isfinite(kv[1]["ic_gross"]) else 0)
    print(f"[{args.venue}] n={n} (early={len(early)} late={len(late)})  entry features ranked by |IC vs gross|:")
    print(f"   {'feature':18s} {'IC_gross':>9s} {'IC_early':>9s} {'IC_late':>9s} {'IC_mae':>8s} stable")
    for f, d in ranked:
        print(f"   {f:18s} {d['ic_gross']:+9.4f} {d['ic_gross_early']:+9.4f} {d['ic_gross_late']:+9.4f} "
              f"{d['ic_mae']:+8.4f} {'YES' if d['stable'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
