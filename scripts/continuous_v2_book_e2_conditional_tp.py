#!/usr/bin/env python3
"""Book E2 — feature-conditional & time-decay dynamic TP (operator direction 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-book-e2-conditional-tp-construction.md

The adverse-trade characterization showed run_up_120 (size of the pop faded) has +0.17
IC on realized gross — bigger-pop fades revert FURTHER. So set the take-profit per trade
from the pre-entry signal: WIDER TP for trades that should revert further. This is new
vs the closed flat (F2) / vol-scaled (F2b) / trailing (E5) work. Equal-weight ("perfect
the trade") per-venue, vs flat baselines and a hash null. Also E6 time-decay TP (wide
early -> narrow late) on the 1m path.

Arms (re-resolve V2_CONTROL shorts on the 1m engine; entries fixed; EQUAL WEIGHT):
- E0_TP12 / E1_TP15:           flat baselines.
- E_RUNUP_TP:                  TP = clip(0.10 + 0.30*run_up_120, 0.08, 0.20).
- E_VOL_TP:                    TP = clip(0.10 + 8*rv_30, 0.08, 0.20)  (re-confirm vol-scaled at 1m EW).
- E6_DECAY_TP:                 TP starts 0.18, decays linearly to 0.08 over the 24h hold.
- E_RUNUP_HASH / E_VOL_HASH:   per-trade TP values hash-permuted across trades (timing destroyed).

Per-venue verdict: a conditional rule must beat BOTH flat baselines AND its hash null on
the SAME venue (per-venue policy is allowed as an operator-gated lead; two-venue needs
both). EXPLORATORY screen. Not a candidate; not real-money evidence.
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

from continuous_v2_book_b_admission import Loader, features  # noqa: E402
from liquidity_migration.intrabar_engine import (  # noqa: E402
    CACHE_ROOT,
    _side_return,
    load_1m_window,
)
from liquidity_migration.trade_lifecycle import _take_profit_price  # noqa: E402

MS_MIN = 60_000
MS_DAY = 86_400_000


def _hash01(s: str) -> float:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def resolve_flat_tp(bars, entry_ts, entry_px, planned_exit, tp_pct):
    """Short first-touch TP at tp_pct, else max_hold close. Returns side_return."""
    win = bars.filter((pl.col("ts_ms") >= entry_ts) & (pl.col("ts_ms") < planned_exit))
    if win.height == 0:
        return 0.0
    tp_price = _take_profit_price(entry_px, side="short", take_profit_pct=tp_pct)
    lows = win["low"].to_list()
    closes = win["close"].to_list()
    last = entry_px
    for i in range(len(lows)):
        if closes[i] is not None:
            last = float(closes[i])
        if lows[i] is not None and lows[i] <= tp_price:
            return tp_pct
    return _side_return(entry_px, last, side="short")


def resolve_decay_tp(bars, entry_ts, entry_px, planned_exit, tp_hi, tp_lo):
    """Time-decay TP: target linearly decays tp_hi->tp_lo across [entry, planned_exit)."""
    win = bars.filter((pl.col("ts_ms") >= entry_ts) & (pl.col("ts_ms") < planned_exit))
    if win.height == 0:
        return 0.0
    span = max(1, planned_exit - entry_ts)
    ts = win["ts_ms"].to_list()
    lows = win["low"].to_list()
    closes = win["close"].to_list()
    last = entry_px
    for i in range(len(ts)):
        if closes[i] is not None:
            last = float(closes[i])
        frac = (int(ts[i]) - entry_ts) / span
        tp_t = tp_hi + (tp_lo - tp_hi) * frac
        tp_price = entry_px * (1.0 - tp_t)
        if lows[i] is not None and lows[i] <= tp_price:
            return tp_t
    return _side_return(entry_px, last, side="short")


def evaluate_venue(trades, venue, cache_root):
    loader = Loader(venue, cache_root)
    recs = []
    for tr in trades:
        ets = int(tr["entry_ts_ms"])
        planned = int(tr["planned_exit_ts_ms"])
        epx = float(tr["entry_price"])
        bars = load_1m_window(venue, str(tr["symbol"]), str(tr["entry_date"]), str(tr["exit_date"]), cache_root=cache_root)
        if bars.height == 0:
            continue
        pre = loader.window(str(tr["symbol"]), ets - 120 * MS_MIN, ets, str(tr["entry_date"]))
        feat = features(pre, ref_close=epx)
        if feat is None:
            continue
        recs.append({"tr": tr, "bars": bars, "ets": ets, "planned": planned, "epx": epx, "feat": feat,
                     "key": f"{tr['symbol']}:{ets}"})
    n = len(recs)
    out = {"n": n, "policies": {}}
    if n == 0:
        return out

    def agg(fn):
        vals = [fn(r) for r in recs]
        return {"mean_trade": statistics.fmean(vals), "median_trade": statistics.median(vals),
                "winrate": sum(1 for x in vals if x > 0) / len(vals)}

    out["policies"]["E0_TP12"] = agg(lambda r: resolve_flat_tp(r["bars"], r["ets"], r["epx"], r["planned"], 0.12))
    out["policies"]["E1_TP15"] = agg(lambda r: resolve_flat_tp(r["bars"], r["ets"], r["epx"], r["planned"], 0.15))
    out["policies"]["E_RUNUP_TP"] = agg(
        lambda r: resolve_flat_tp(r["bars"], r["ets"], r["epx"], r["planned"], _clip(0.10 + 0.30 * r["feat"]["run_up_120"], 0.08, 0.20)))
    out["policies"]["E_VOL_TP"] = agg(
        lambda r: resolve_flat_tp(r["bars"], r["ets"], r["epx"], r["planned"], _clip(0.10 + 8.0 * r["feat"]["rv_30"], 0.08, 0.20)))
    out["policies"]["E6_DECAY_TP"] = agg(
        lambda r: resolve_decay_tp(r["bars"], r["ets"], r["epx"], r["planned"], 0.18, 0.08))
    # hash nulls: permute the per-trade conditional TP across trades
    for label, slope, feat_key, lo, hi in [("E_RUNUP_HASH", 0.30, "run_up_120", 0.08, 0.20),
                                           ("E_VOL_HASH", 8.0, "rv_30", 0.08, 0.20)]:
        tps = [_clip(0.10 + slope * r["feat"][feat_key], lo, hi) for r in recs]
        order = sorted(range(n), key=lambda i: _hash01(f"{label}:{recs[i]['key']}"))
        perm = [tps[order[i]] for i in range(n)]
        vals = [resolve_flat_tp(recs[i]["bars"], recs[i]["ets"], recs[i]["epx"], recs[i]["planned"], perm[i]) for i in range(n)]
        out["policies"][label] = {"mean_trade": statistics.fmean(vals), "median_trade": statistics.median(vals),
                                  "winrate": sum(1 for x in vals if x > 0) / len(vals)}
    ctrl = out["policies"]["E0_TP12"]["mean_trade"]
    for name, m in out["policies"].items():
        m["delta_vs_tp12"] = m["mean_trade"] - ctrl
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--trades", default=None)
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_book_e2_2026-06-20")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"run_label": "exploratory_screen", "basis": "equal_weight_per_trade", "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tp = Path(args.trades.replace("VENUE", venue)) if args.trades else Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv"
        tr = pl.read_csv(tp).filter(pl.col("side") == "short")
        summary["venues"][venue] = evaluate_venue(tr.to_dicts(), venue, Path(args.cache_root))
        v = summary["venues"][venue]
        print(f"[{venue}] n={v['n']}  (EQUAL-WEIGHT mean trade return; Δ vs flat TP12)", flush=True)
        for name, m in v["policies"].items():
            print(f"   {name:14s} mean={m['mean_trade']:+.5f} winrate={m['winrate']:.3f} Δvs_TP12={m['delta_vs_tp12']:+.5f}", flush=True)
    # per-venue verdict: conditional beats BOTH flats AND its hash, on a venue
    def beats(p, name, hashname):
        return (name in p and p[name]["mean_trade"] > p["E0_TP12"]["mean_trade"]
                and p[name]["mean_trade"] > p["E1_TP15"]["mean_trade"]
                and p[name]["mean_trade"] > p[hashname]["mean_trade"])
    for venue, v in summary["venues"].items():
        v["runup_wins"] = beats(v["policies"], "E_RUNUP_TP", "E_RUNUP_HASH")
        v["vol_wins"] = beats(v["policies"], "E_VOL_TP", "E_VOL_HASH")
    (out / "book_e2_conditional_tp.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({vn: {"runup_wins": vv["runup_wins"], "vol_wins": vv["vol_wins"]}
                      for vn, vv in summary["venues"].items()}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
