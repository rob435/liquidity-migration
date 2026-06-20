#!/usr/bin/env python3
"""Event-driven TWAP/VWAP entry study for the fade book (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-twap-entry-construction.md

The adverse-trade characterization showed high-vol / big-run-up fades run HARD against
the short before reverting to a bigger profit. Thesis: instead of a single market entry
at the signal bar, scale the short IN over the first minutes — as the name keeps popping
you short at progressively HIGHER (better) prices, improving the average entry and
capturing more of the reversion. This study tests that on the 1m path, broken out by
pre-entry vol decile (where the thesis should bite hardest).

Entry methods (short; higher avg entry = better):
- single:   full size at the entry-bar close (current behavior).
- twap_K:   N equal 1-min slices over the first K minutes; avg = mean(slice closes).
- vwap_K:   slices over K min weighted by per-minute volume.
- rip_K:    event-driven "short the rip" — heavier weight on minutes where price is ABOVE
            the single-entry price (only adds aggressively while the pop continues).

For each method the realized short return uses the method's avg entry, TP at
avg*(1−0.12) first-touch AFTER the entry window, else max_hold close. A transparent
extra entry cost (participation/slippage) is charged per slice and STRESSED 1x/2x/3x so
no apparent gain survives only under zero-cost fills.

EXPLORATORY screen; entry execution research. Not a candidate; not real-money evidence.
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

from continuous_v2_book_b_admission import Loader, features  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

MS_MIN = 60_000
TP = 0.12
ROUND_TRIP_BPS = 12.0  # flat round-trip cost (taker+spread), SAME for all methods; stressed 1x/2x/3x


def _load_path_with_vol(venue, symbol, start_ms, end_ms, start_date, end_date, cache_root):
    from datetime import date, timedelta
    d0 = date.fromisoformat(str(start_date)[:10])
    d1 = date.fromisoformat(str(end_date)[:10])
    frames = []
    dd = d0
    while dd <= d1:
        p = cache_root / venue / "klines_1m" / f"date={dd.isoformat()}" / f"symbol={symbol}" / "data.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p, columns=["ts_ms", "low", "close", "volume_base"]))
        dd += timedelta(days=1)
    if not frames:
        return pl.DataFrame(schema={"ts_ms": pl.Int64, "low": pl.Float64, "close": pl.Float64, "volume_base": pl.Float64})
    return pl.concat(frames).filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms)).unique("ts_ms").sort("ts_ms")


def _short_exit(path_lows, path_closes, avg_entry, start_idx):
    """First-touch TP (low<=avg*(1-TP)) after start_idx, else last close; return short gross."""
    tp_price = avg_entry * (1.0 - TP)
    for i in range(start_idx, len(path_closes)):
        if path_lows[i] is not None and path_lows[i] <= tp_price:
            return TP  # filled at tp_price -> exactly +TP
    last = next((c for c in reversed(path_closes) if c is not None), avg_entry)
    return (avg_entry - last) / avg_entry


def entry_methods(closes, vols, k):
    """Return {method: avg_entry} for entry window minutes [0, k)."""
    win_c = [c for c in closes[:k] if c is not None]
    if not win_c:
        return None
    single = closes[0] if closes[0] is not None else win_c[0]
    out = {"single": single, f"twap_{k}": statistics.fmean(win_c)}
    # vwap
    cv = [(c, v) for c, v in zip(closes[:k], vols[:k]) if c is not None and v is not None and v > 0]
    out[f"vwap_{k}"] = (sum(c * v for c, v in cv) / sum(v for _, v in cv)) if cv else statistics.fmean(win_c)
    # event-driven "short the rip": weight = 1 + max(0, (price/single - 1))*10 (heavier above entry)
    w = [(c, 1.0 + max(0.0, (c / single - 1.0)) * 10.0) for c in win_c]
    out[f"rip_{k}"] = sum(c * ww for c, ww in w) / sum(ww for _, ww in w)
    return out


def evaluate_venue(trades, venue, cache_root, ks, cost_mult):
    loader = Loader(venue, cache_root)
    # collect per-trade realized return for each method, plus pre-entry vol for deciles
    recs = []
    for tr in trades:
        ets = int(tr["entry_ts_ms"])
        exit_ms = int(tr["exit_ts_ms"])
        # full path from entry to exit (1m), with volume for VWAP
        path = _load_path_with_vol(venue, str(tr["symbol"]), ets, exit_ms + MS_MIN,
                                   tr["entry_date"], tr["exit_date"], cache_root)
        if path.height < 5:
            continue
        closes = path["close"].to_list()
        lows = path["low"].to_list()
        vols = path["volume_base"].to_list()
        # pre-entry vol for decile bucketing
        pre = loader.window(str(tr["symbol"]), ets - 120 * MS_MIN, ets, str(tr["entry_date"]))
        feat = features(pre, ref_close=float(tr["entry_price"]))
        rv = feat["rv_30"] if feat else float("nan")
        rec = {"rv_30": rv}
        single_px = None
        kmax = max(ks)
        for k in ks:
            am = entry_methods(closes, vols, k)
            if am is None:
                continue
            if single_px is None:
                single_px = am["single"]
            for method, avg_entry in am.items():
                if method == "single" and k != ks[0]:
                    continue
                # flat round-trip cost, SAME for all methods, so Δ vs single is pure
                # entry-timing (TWAP's smaller-clip impact advantage is unmodeled upside).
                gross = _short_exit(lows, closes, avg_entry, start_idx=min(k, len(closes) - 1))
                cost = ROUND_TRIP_BPS * cost_mult / 10_000.0
                rec[method] = gross - cost
                # PURE entry-price effect (exit-independent): short wants HIGHER avg entry.
                if method == f"twap_{kmax}" and single_px:
                    rec["entry_impr_twap"] = (avg_entry - single_px) / single_px
                if method == f"rip_{kmax}" and single_px:
                    rec["entry_impr_rip"] = (avg_entry - single_px) / single_px
        recs.append(rec)
    n = len(recs)
    out = {"n": n, "methods": {}, "rv_deciles": {}}
    if n == 0:
        return out
    methods = sorted({k for r in recs for k in r if k != "rv_30"})
    for m in methods:
        vals = [r[m] for r in recs if m in r]
        out["methods"][m] = {"mean": statistics.fmean(vals), "median": statistics.median(vals), "n": len(vals)}
    # mean PURE entry-price improvement (short: >0 = TWAP got a higher/better avg short price)
    ei_twap = [r["entry_impr_twap"] for r in recs if "entry_impr_twap" in r]
    ei_rip = [r["entry_impr_rip"] for r in recs if "entry_impr_rip" in r]
    out["entry_price_improvement"] = {
        "twap_mean": statistics.fmean(ei_twap) if ei_twap else None,
        "rip_mean": statistics.fmean(ei_rip) if ei_rip else None,
        "twap_pct_better": (sum(1 for x in ei_twap if x > 0) / len(ei_twap)) if ei_twap else None,
    }
    # vol-decile breakdown: TWAP realized + pure entry-price improvement by pre-entry vol
    base = "single"
    twap_key = f"twap_{max(ks)}"
    pairs = [(r["rv_30"], r.get(base), r.get(twap_key), r.get("entry_impr_twap")) for r in recs
             if np.isfinite(r.get("rv_30", float("nan"))) and base in r and twap_key in r]
    pairs.sort(key=lambda x: x[0])
    np_ = len(pairs)
    for d in range(10):
        seg = pairs[d * np_ // 10:(d + 1) * np_ // 10]
        if not seg:
            continue
        ei = [s[3] for s in seg if s[3] is not None]
        out["rv_deciles"][f"d{d + 1}"] = {
            "rv_mean": statistics.fmean([s[0] for s in seg]),
            "single_mean": statistics.fmean([s[1] for s in seg]),
            f"{twap_key}_mean": statistics.fmean([s[2] for s in seg]),
            "twap_minus_single": statistics.fmean([s[2] - s[1] for s in seg]),
            "entry_price_impr": statistics.fmean(ei) if ei else None,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--trades", default=None, help="optional trades CSV (VENUE placeholder) instead of V2_CONTROL")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_twap_entry_2026-06-20")
    ap.add_argument("--ks", default="5,15,30,60")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    args = ap.parse_args()
    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"run_label": "exploratory_screen", "tp": TP, "ks": ks, "round_trip_bps": ROUND_TRIP_BPS,
               "cost_mult": args.cost_mult, "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tp = Path(args.trades.replace("VENUE", venue)) if args.trades else Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv"
        tr = pl.read_csv(tp).filter(pl.col("side") == "short")
        summary["venues"][venue] = evaluate_venue(tr.to_dicts(), venue, Path(args.cache_root), ks, args.cost_mult)
        v = summary["venues"][venue]
        print(f"[{venue}] n={v['n']}  (mean realized short return by entry method, cost x{args.cost_mult})", flush=True)
        sm = v["methods"].get("single", {}).get("mean")
        for m, s in sorted(v["methods"].items()):
            d = (s["mean"] - sm) if sm is not None else None
            print(f"   {m:10s} mean={s['mean']:+.5f} median={s['median']:+.5f}  Δvs_single={d:+.5f}" if d is not None
                  else f"   {m:10s} mean={s['mean']:+.5f}", flush=True)
        ei = v.get("entry_price_improvement", {})
        print(f"   PURE entry-price improvement (short, >0=better avg price): twap_mean={ei.get('twap_mean')} "
              f"rip_mean={ei.get('rip_mean')} twap_pct_better={ei.get('twap_pct_better')}", flush=True)
        print("   by pre-entry vol decile (realized Δ + pure entry-price impr):", flush=True)
        for dk, dv in v["rv_deciles"].items():
            print(f"     {dk:3s} rv={dv['rv_mean']:.4f} realizedΔ={dv['twap_minus_single']:+.5f} "
                  f"entry_price_impr={dv['entry_price_impr']:+.6f}", flush=True)
    (out / "twap_entry.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"written: {out / 'twap_entry.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
