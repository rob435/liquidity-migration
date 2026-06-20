#!/usr/bin/env python3
"""Problem Book F — BTC regime sizing of the fade book (continuous v2 next-level).

Receipt: docs/preregistration/2026-06-20-continuous-v2-book-f-btc-regime-construction.md

The book already has a BTC-UPTREND entry gate and a BTC-vol regime HEDGE intensity.
This book asks the new question: does a causal BTC-vol regime signal, applied to the
fade BOOK exposure (not just the hedge), time the exposure better than random?

Book G's lesson is the load-bearing constraint: an exposure control that just lowers
average gross is a leverage effect, not a timing edge. So every F arm uses a MEAN-1
(gross-neutral) per-trade multiplier keyed on the trade's ENTRY day (causal at sizing
time) and must beat a HASH-PERMUTED null (same multiplier distribution, regime labels
shuffled). A mean-1 multiplier leaves average gross unchanged, so any MAR change is
PURE TIMING.

Arms (re-weight the V2_CONTROL trades; entries/exits fixed):
- F0_CONTROL:       multiplier 1.0 everywhere (reproduces control).
- F2_DERISK_HIVOL:  scale DOWN on high-BTC-vol entry days (intensity inverted), mean-1.
- F2b_LEVER_HIVOL:  scale UP on high-BTC-vol entry days (intensity direct), mean-1.
- F8_HASH_REGIME:   F2's multipliers hash-permuted across trades (timing destroyed).

EXPLORATORY screen: per-trade realized PnL re-weighted; no rebalance/hedge re-solve
(the BTC-vol HEDGE intensity is already live and unchanged — this tests BOOK gross
timing only). Survivors need full-ledger validation. Not a candidate; not real-money.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import continuous_v2_ab_research_runner as abr  # noqa: E402
from liquidity_migration.continuous_regime import (  # noqa: E402
    FROZEN_BTCVOL_REGIME,
    btcvol_intensity_series,
)

ANN_DAYS = 365.25
MS_DAY = 86_400_000


def _hash01(s: str) -> float:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def mar_proxy(daily: dict[int, float]) -> dict[str, Any]:
    days = sorted(daily)
    if not days:
        return {"total": 0.0, "mar": None, "max_dd": 0.0, "worst_day": 0.0}
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    worst = 0.0
    for d in days:
        r = daily[d]
        eq *= 1.0 + r
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1.0)
        worst = min(worst, r)
    total = eq - 1.0
    yrs = max((days[-1] - days[0]) / ANN_DAYS, 1e-9)
    return {"total": total, "max_dd": maxdd, "worst_day": worst,
            "mar": (total / yrs) / abs(maxdd) if abs(maxdd) > 1e-12 else None}


def evaluate_venue(trades: list[dict[str, Any]], venue: str) -> dict[str, Any]:
    # BTC daily returns for this venue -> causal mean-1 vol intensity per day.
    data_root = abr.ROOTS[venue]
    # intensity series is keyed by day-ms; build over the union of entry+exit days
    all_days_ms = sorted({(int(tr["entry_ts_ms"]) // MS_DAY) * MS_DAY for tr in trades}
                         | {(int(tr["exit_ts_ms"]) // MS_DAY) * MS_DAY for tr in trades})
    btc_ret, _btc_fund = abr.instrument_inputs(data_root, venue, all_days_ms, "BTCUSDT")
    reg = FROZEN_BTCVOL_REGIME
    intensity = btcvol_intensity_series(all_days_ms, btc_ret, reg["lam"], reg["vol_window"], reg["pct_window"])

    # per-trade raw intensity on the ENTRY day (causal), then mean-1 normalize across trades.
    raw_int = []
    for tr in trades:
        eday_ms = (int(tr["entry_ts_ms"]) // MS_DAY) * MS_DAY
        raw_int.append(intensity.get(eday_ms, 1.0))
    mean_int = sum(raw_int) / len(raw_int) if raw_int else 1.0
    # direct (lever in high vol) and inverted (derisk in high vol), both mean-1.
    direct = [x / mean_int for x in raw_int]
    inv_raw = [2.0 - x for x in raw_int]  # reflect around 1 so high-vol -> low mult
    mean_inv = sum(inv_raw) / len(inv_raw) if inv_raw else 1.0
    invert = [x / mean_inv for x in inv_raw]

    def daily_from_mult(mult: list[float]) -> dict[int, float]:
        daily: dict[int, float] = {}
        for tr, m in zip(trades, mult):
            c = float(tr["net_return"]) * m
            d = int(tr["exit_ts_ms"]) // MS_DAY
            daily[d] = daily.get(d, 0.0) + c
        return daily

    out: dict[str, Any] = {"n": len(trades), "mean_raw_intensity": mean_int, "policies": {}}
    out["policies"]["F0_CONTROL"] = mar_proxy(daily_from_mult([1.0] * len(trades)))
    out["policies"]["F2_DERISK_HIVOL"] = mar_proxy(daily_from_mult(invert))
    out["policies"]["F2b_LEVER_HIVOL"] = mar_proxy(daily_from_mult(direct))
    # hash null: permute the F2 (invert) multipliers across trades
    order = sorted(range(len(trades)), key=lambda i: _hash01(f"F8:{trades[i]['symbol']}:{trades[i]['entry_ts_ms']}"))
    permuted = [0.0] * len(trades)
    for new_i, old_i in enumerate(order):
        permuted[new_i] = invert[old_i]
    out["policies"]["F8_HASH_REGIME"] = mar_proxy(daily_from_mult(permuted))
    ctrl = out["policies"]["F0_CONTROL"]
    for name, m in out["policies"].items():
        m["mar_delta_vs_ctrl"] = (m["mar"] - ctrl["mar"]) if (m["mar"] is not None and ctrl["mar"] is not None) else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--out", default="backtest-runs/continuous_v2_book_f_2026-06-20")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_screen",
                               "note": "mean-1 BTC-vol book-gross timing; hedge unchanged; no rebalance re-solve",
                               "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        summary["venues"][venue] = evaluate_venue(tr.to_dicts(), venue)
        v = summary["venues"][venue]
        print(f"[{venue}] n={v['n']} mean_intensity={v['mean_raw_intensity']:.4f}", flush=True)
        for name, m in v["policies"].items():
            print(f"   {name:18s} mar={m['mar']} dd={m['max_dd']:+.4f} Δmar={m['mar_delta_vs_ctrl']}", flush=True)
    venues = list(summary["venues"])
    winners = []
    if len(venues) == 2:
        a, b = venues
        for name in ["F2_DERISK_HIVOL", "F2b_LEVER_HIVOL"]:
            pa, pb = summary["venues"][a]["policies"][name], summary["venues"][b]["policies"][name]
            ha, hb = summary["venues"][a]["policies"]["F8_HASH_REGIME"], summary["venues"][b]["policies"]["F8_HASH_REGIME"]
            if (pa["mar_delta_vs_ctrl"] and pb["mar_delta_vs_ctrl"]
                    and pa["mar_delta_vs_ctrl"] > 0 and pb["mar_delta_vs_ctrl"] > 0
                    and pa["mar"] > ha["mar"] and pb["mar"] > hb["mar"]):
                winners.append(name)
    summary["both_venue_winners"] = winners
    (out / "book_f_btc_regime.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"both_venue_winners": winners}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
