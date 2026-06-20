#!/usr/bin/env python3
"""Bybit taker-flow exhaustion features (operator direction 2026-06-20: go after order flow).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

upper_wick captures rejection by candle SHAPE; taker flow captures the actual aggressive
order flow. Theory of the fade: the best shorts are buyer-EXHAUSTION pops — aggressive
buying into a rip that is stalling (blow-off), or price making highs while net taker flow
does not (CVD divergence / absorption). Built from the bybit_full_pit taker_flow_5m tape
(taker_buy_quote/taker_sell_quote/n_buy/n_sell), 100% coverage of the V2_CONTROL windows,
strictly causal ([entry-120m, entry)). IC vs realized gross with train/test stability, vs
the upper_wick baseline (+0.146) and checked for orthogonality. EXPLORATORY feature research.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features, spearman  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

FLOW_ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit" / "taker_flow_5m"
MS_MIN = 60_000
PRE_MIN = 120


def _load_flow(symbol, ets, edate):
    d0 = dt.date.fromisoformat(str(edate)[:10])
    frames = []
    for k in (-1, 0):
        p = FLOW_ROOT / f"date={(d0 + dt.timedelta(days=k)).isoformat()}" / f"symbol={symbol}" / "part.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p))
    if not frames:
        return None
    df = pl.concat(frames).filter((pl.col("ts_ms") >= ets - PRE_MIN * MS_MIN) & (pl.col("ts_ms") < ets)).unique("ts_ms").sort("ts_ms")
    return df if df.height >= 12 else None


def flow_features(df, price_chg):
    b = df["taker_buy_quote"].to_numpy().astype(float)
    s = df["taker_sell_quote"].to_numpy().astype(float)
    nb = df["n_buy"].to_numpy().astype(float)
    ns = df["n_sell"].to_numpy().astype(float)
    tot = b + s
    Tb, Ts = b.sum(), s.sum()
    T = Tb + Ts
    if T <= 0:
        return None
    n = len(b)
    buy_ratio = Tb / T
    # CVD path and its slope (net buying trend over the window)
    cvd = np.cumsum(b - s)
    x = np.arange(n, dtype=float)
    cvd_slope = float(np.polyfit(x, cvd, 1)[0] / (T / n)) if n > 2 and T > 0 else 0.0
    # buyer-exhaustion: buy share in the last 30m (6 bars) minus first 90m
    k = min(6, n - 1)
    recent_buy = b[-k:].sum() / max(tot[-k:].sum(), 1e-9)
    early_buy = b[:-k].sum() / max(tot[:-k].sum(), 1e-9)
    buy_fade = recent_buy - early_buy  # <0 = aggressive buying drying up (exhaustion)
    # average aggressive trade size: buyers vs sellers (whale buyers?)
    avg_buy = Tb / max(nb.sum(), 1e-9)
    avg_sell = Ts / max(ns.sum(), 1e-9)
    buysize_rel = math.log(avg_buy / avg_sell) if avg_sell > 0 and avg_buy > 0 else 0.0
    # absorption: price made a big up-move on little NET buying? price_chg high, cvd low = absorbed
    cvd_norm = (Tb - Ts) / T
    absorption = price_chg - cvd_norm  # price rose more than net flow justifies = sellers absorbing
    # price-flow divergence: price up but buy_ratio not elevated (buyers not confirming highs)
    pf_div = price_chg * (0.5 - buy_ratio)  # >0 when price up while buying weak
    n_imb = (nb.sum() - ns.sum()) / (nb.sum() + ns.sum())
    return {
        "buy_ratio": buy_ratio, "cvd_slope": cvd_slope, "buy_fade_recent": buy_fade,
        "buysize_rel": buysize_rel, "absorption": absorption, "pf_divergence": pf_div, "n_imbalance": n_imb,
    }


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
        if fl is None:
            continue
        pre = _load_pre(args.venue, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], cache_root)
        if pre is None:
            continue
        ef = enriched_features(pre, float(t["entry_price"]))
        c = pre["close"].to_list()
        price_chg = (c[-1] / c[0] - 1.0) if (c[0] and c[-1]) else 0.0
        ff = flow_features(fl, price_chg)
        if ff is None or ef is None:
            continue
        ff["upper_wick_mean"] = ef["upper_wick_mean"]
        ff["_g"] = float(t["gross_trade_return"])
        ff["_ets"] = int(t["entry_ts_ms"])
        rows.append(ff)
    feats = [k for k in rows[0] if not k.startswith("_")]
    early = [r for r in rows if r["_ets"] < args.split_ts]
    late = [r for r in rows if r["_ets"] >= args.split_ts]
    g = [r["_g"] for r in rows]
    uw = [r["upper_wick_mean"] for r in rows]
    print(f"[{args.venue}] n={len(rows)} (early={len(early)} late={len(late)})  taker-flow exhaustion features vs gross:")
    print(f"   {'feature':16s} {'IC_gross':>9s} {'IC_early':>9s} {'IC_late':>9s} {'corr(uw)':>9s} stable")
    res = {"venue": args.venue, "n": len(rows), "features": {}}
    ranked = []
    for f in feats:
        ic = spearman([r[f] for r in rows], g)
        ie = spearman([r[f] for r in early], [r["_g"] for r in early])
        il = spearman([r[f] for r in late], [r["_g"] for r in late])
        cuw = spearman([r[f] for r in rows], uw) if f != "upper_wick_mean" else 1.0
        st = bool(np.isfinite(ie) and np.isfinite(il) and np.sign(ie) == np.sign(il))
        res["features"][f] = {"ic_gross": ic, "ic_early": ie, "ic_late": il, "corr_upper_wick": cuw, "stable": st}
        ranked.append((f, ic, ie, il, cuw, st))
    for f, ic, ie, il, cuw, st in sorted(ranked, key=lambda x: -abs(x[1]) if math.isfinite(x[1]) else 0):
        print(f"   {f:16s} {ic:+9.4f} {ie:+9.4f} {il:+9.4f} {cuw:+9.3f} {'YES' if st else 'no'}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "takerflow_features.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
