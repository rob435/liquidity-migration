#!/usr/bin/env python3
"""Bybit dynamic-TP push — reversion-speed-adaptive & exhaustion-conditional (2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

Creative dynamic TP for Bybit (operator: keep pushing dynamic TP, be experimental).
Two new path-/feature-aware ideas, OOS (split), equal-weight, vs flat baselines + hash:

- SPEED-ADAPTIVE TP: if the fade reaches +arm% favorable FAST (within T_fast minutes),
  it's a strong reverter -> widen TP to tp_wide; else keep tp_base. Decided causally as
  the path unfolds.
- UPPER_WICK-CONDITIONAL TP: high pre-entry exhaustion (top tercile upper_wick) -> tp_wide;
  else tp_base. Uses the entry signal that carries Bybit alpha.

Bybit fades revert fast-and-hard then bounce (tight TP usually best), so the bet is that a
SUBSET (fast/strong reverters) can profitably run wider. EXPLORATORY screen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT, load_1m_window  # noqa: E402

MS_MIN = 60_000


def _hash01(s):
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _path(bars, entry_ts, planned):
    w = bars.filter((pl.col("ts_ms") >= entry_ts) & (pl.col("ts_ms") < planned)).sort("ts_ms")
    return w["ts_ms"].to_list(), w["low"].to_list(), w["close"].to_list()


def realized_flat(bars, entry_ts, entry_px, planned, tp):
    ts, lows, closes = _path(bars, entry_ts, planned)
    if not ts:
        return 0.0
    tp_price = entry_px * (1.0 - tp)
    last = entry_px
    for i in range(len(ts)):
        if closes[i] is not None:
            last = float(closes[i])
        if lows[i] is not None and lows[i] <= tp_price:
            return tp
    return (entry_px - last) / entry_px


def realized_speed_adaptive(bars, entry_ts, entry_px, planned, *, arm, t_fast_ms, tp_base, tp_wide):
    """Widen TP to tp_wide if +arm favorable reached within t_fast; else tp_base. Causal."""
    ts, lows, closes = _path(bars, entry_ts, planned)
    if not ts:
        return 0.0
    arm_price = entry_px * (1.0 - arm)
    tp = tp_base
    widened = False
    last = entry_px
    for i in range(len(ts)):
        if closes[i] is not None:
            last = float(closes[i])
        if not widened and lows[i] is not None and lows[i] <= arm_price:
            # reached +arm favorable; fast?
            if int(ts[i]) - entry_ts <= t_fast_ms:
                tp = tp_wide
            widened = True
        if lows[i] is not None and lows[i] <= entry_px * (1.0 - tp):
            return tp
    return (entry_px - last) / entry_px


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_bybit_dyntp_2026-06-20")
    ap.add_argument("--split-ts", type=int, default=1748736000000)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache_root = Path(args.cache_root)
    tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / args.venue / "trades.csv").filter(pl.col("side") == "short")
    recs = []
    for t in tr.to_dicts():
        bars = load_1m_window(args.venue, str(t["symbol"]), str(t["entry_date"]), str(t["exit_date"]), cache_root=cache_root)
        if bars.height == 0:
            continue
        pre = _load_pre(args.venue, str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], cache_root)
        uw = enriched_features(pre, float(t["entry_price"]))["upper_wick_mean"] if pre is not None else None
        recs.append({"bars": bars, "ets": int(t["entry_ts_ms"]), "epx": float(t["entry_price"]),
                     "planned": int(t["planned_exit_ts_ms"]), "uw": uw, "split_ts": int(t["entry_ts_ms"]),
                     "key": f"{t['symbol']}:{t['entry_ts_ms']}"})
    late = [r for r in recs if r["ets"] >= args.split_ts]
    uw_vals = sorted(r["uw"] for r in late if r["uw"] is not None)
    uw_hi = uw_vals[int(len(uw_vals) * 0.67)] if uw_vals else 0.0  # top tercile threshold

    def agg(fn):
        v = [fn(r) for r in late]
        return {"mean": statistics.fmean(v), "winrate": sum(1 for x in v if x > 0) / len(v)}

    res = {"venue": args.venue, "n_late": len(late), "policies": {}}
    res["policies"]["flat_TP12"] = agg(lambda r: realized_flat(r["bars"], r["ets"], r["epx"], r["planned"], 0.12))
    res["policies"]["flat_TP15"] = agg(lambda r: realized_flat(r["bars"], r["ets"], r["epx"], r["planned"], 0.15))
    # speed-adaptive variants
    for arm, tfast_min, base, wide in [(0.03, 60, 0.12, 0.15), (0.04, 90, 0.12, 0.16), (0.03, 120, 0.10, 0.15)]:
        nm = f"speed_arm{int(arm*100)}_t{tfast_min}_{int(base*100)}to{int(wide*100)}"
        res["policies"][nm] = agg(lambda r, a=arm, tf=tfast_min*MS_MIN, b=base, w=wide:
                                  realized_speed_adaptive(r["bars"], r["ets"], r["epx"], r["planned"], arm=a, t_fast_ms=tf, tp_base=b, tp_wide=w))
    # upper_wick-conditional: top-tercile exhaustion -> wide
    res["policies"]["uw_cond_12to15"] = agg(lambda r: realized_flat(r["bars"], r["ets"], r["epx"], r["planned"],
                                                                    0.15 if (r["uw"] is not None and r["uw"] >= uw_hi) else 0.12))
    # hash null: assign wide to a random top-tercile-sized subset
    hv = {r["key"]: _hash01("DTPH:" + r["key"]) for r in late}
    thr = sorted(hv.values())[int(len(hv) * 0.67)] if hv else 1.0
    res["policies"]["uw_cond_HASH"] = agg(lambda r: realized_flat(r["bars"], r["ets"], r["epx"], r["planned"],
                                                                  0.15 if hv[r["key"]] >= thr else 0.12))
    ctrl = res["policies"]["flat_TP12"]["mean"]
    for nm, p in res["policies"].items():
        p["delta_vs_tp12"] = p["mean"] - ctrl
    (out / "bybit_dyntp.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(f"[{args.venue}] OOS dynamic-TP (late n={len(late)}); control flat_TP12 mean={ctrl:+.5f}")
    for nm, p in sorted(res["policies"].items(), key=lambda kv: -kv[1]["delta_vs_tp12"]):
        print(f"   {nm:30s} mean={p['mean']:+.5f} winrate={p['winrate']:.3f} Δvs_TP12={p['delta_vs_tp12']:+.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
