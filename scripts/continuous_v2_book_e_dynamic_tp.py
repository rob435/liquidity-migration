#!/usr/bin/env python3
"""Problem Book E — dynamic / MFE-extension take-profit on the 1m path (continuous v2 next-level).

Receipt: docs/preregistration/2026-06-20-continuous-v2-book-e-dynamic-tp-construction.md

The prior F2/F2b exit-alpha work (1h + per-trade proxy) closed the FLAT TP raise as a
fundamental venue split (Bybit-positive / Binance-negative) and showed vol-scaled TP is
DOMINATED by the flat raise. This book does NOT re-run that closed ground. It tests the
one axis the 1h bar could not measure: PATH-CONDITIONAL winner management — a hard TP15
ceiling plus a 1m-path-armed trailing giveback (E5 MFE-extension) that protects the
control's 12% gain while letting strongly-reverting names run. The question: does the
trailing protection RECONCILE the Binance drawdown that flat TP15 caused?

Arms (all re-resolve the V2_CONTROL short trades on the 1m cache; entries fixed):
- E0_TP12:        control (flat TP 12%) — reproduces the frozen exit.
- E1_TP15_FLAT:   flat TP 15% (the known venue-split negative; re-confirmed at 1m).
- E5a/b/c:        TP15 ceiling + trailing armed at 12%, giveback 1.5/2.5/4.0%.
- E8_HASH_TRAIL:  null — same arm/give distribution but a HASH-permuted base TP in [12,15]
                  (matches "sometimes exit wider" frequency, destroys the path mechanism).

Metric: realized-PnL MAR proxy + drawdown + worst-day, both venues. A candidate must
improve MAR vs E0 AND not worsen drawdown, on BOTH venues, beating the hash null.
EXPLORATORY screen (no rebalance/hedge re-solve); survivors need full-ledger validation.
Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(REPO))
from liquidity_migration.intrabar_engine import (  # noqa: E402
    CACHE_ROOT,
    load_1m_window,
    resolve_dynamic_tp_1m,
    resolve_exit_1m,
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


class WindowCache:
    def __init__(self, venue: str, cache_root: Path):
        self.venue, self.cache_root, self._c = venue, cache_root, {}

    def get(self, symbol: str, start_date: str, end_date: str) -> pl.DataFrame:
        key = (symbol, start_date, end_date)
        if key not in self._c:
            self._c[key] = load_1m_window(self.venue, symbol, start_date, end_date, cache_root=self.cache_root)
        return self._c[key]


def _contrib(side_return: float, tr: dict[str, Any]) -> float:
    nw = float(tr["notional_weight"])
    return side_return * nw + float(tr.get("cost_return", 0.0) or 0.0) + float(tr.get("funding_return", 0.0) or 0.0)


# (arm, give) trailing variants for the TP15 ceiling
E5_VARIANTS = {"E5a_arm12_give15": (0.12, 0.015), "E5b_arm12_give25": (0.12, 0.025), "E5c_arm12_give40": (0.12, 0.040)}


def evaluate_venue(trades: list[dict[str, Any]], cache: WindowCache) -> dict[str, Any]:
    prepared = []
    for tr in trades:
        bars = cache.get(str(tr["symbol"]), str(tr["entry_date"]), str(tr["exit_date"]))
        if bars.height:
            prepared.append((tr, bars))
    n = len(prepared)
    out: dict[str, Any] = {"n_with_path": n, "policies": {}}
    if n == 0:
        return out

    def flat(tr, bars, tp):
        return resolve_exit_1m(bars, entry_ts_ms=int(tr["entry_ts_ms"]), entry_price=float(tr["entry_price"]),
                               side="short", take_profit_pct=tp, stop_loss_pct=0.0,
                               planned_exit_ts_ms=int(tr["planned_exit_ts_ms"]))

    def dyn(tr, bars, base, arm, give):
        return resolve_dynamic_tp_1m(bars, entry_ts_ms=int(tr["entry_ts_ms"]), entry_price=float(tr["entry_price"]),
                                     side="short", base_tp_pct=base, trail_arm_pct=arm, trail_give_pct=give,
                                     planned_exit_ts_ms=int(tr["planned_exit_ts_ms"]))

    def run(resolver) -> dict[str, Any]:
        daily: dict[int, float] = {}
        for tr, bars in prepared:
            res = resolver(tr, bars)
            daily[int(res.exit_ts_ms) // MS_DAY] = daily.get(int(res.exit_ts_ms) // MS_DAY, 0.0) + _contrib(res.side_return, tr)
        return mar_proxy(daily)

    out["policies"]["E0_TP12"] = run(lambda tr, b: flat(tr, b, 0.12))
    out["policies"]["E1_TP15_FLAT"] = run(lambda tr, b: flat(tr, b, 0.15))
    for name, (arm, give) in E5_VARIANTS.items():
        out["policies"][name] = run(lambda tr, b, a=arm, g=give: dyn(tr, b, 0.15, a, g))
    # E8 hash null: base TP hash-permuted in [0.12, 0.15], no trail (matches "sometimes wider", no path)
    out["policies"]["E8_HASH_TRAIL"] = run(
        lambda tr, b: flat(tr, b, 0.12 + 0.03 * _hash01(f"E8:{tr['symbol']}:{tr['entry_ts_ms']}"))
    )
    ctrl = out["policies"]["E0_TP12"]
    for name, m in out["policies"].items():
        m["mar_delta_vs_ctrl"] = (m["mar"] - ctrl["mar"]) if (m["mar"] is not None and ctrl["mar"] is not None) else None
        m["dd_delta_vs_ctrl"] = m["max_dd"] - ctrl["max_dd"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_book_e_2026-06-20")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_screen", "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        if args.limit:
            tr = tr.head(args.limit)
        cache = WindowCache(venue, Path(args.cache_root))
        summary["venues"][venue] = evaluate_venue(tr.to_dicts(), cache)
        v = summary["venues"][venue]
        print(f"[{venue}] n={v['n_with_path']}", flush=True)
        for name, m in v["policies"].items():
            print(f"   {name:18s} mar={m['mar']} dd={m['max_dd']:+.4f} worst={m['worst_day']:+.4f} "
                  f"Δmar={m['mar_delta_vs_ctrl']} Δdd={m['dd_delta_vs_ctrl']:+.4f}", flush=True)
    venues = list(summary["venues"])
    winners = []
    if len(venues) == 2:
        a, b = venues
        for name in summary["venues"][a]["policies"]:
            if name == "E0_TP12":
                continue
            pa, pb = summary["venues"][a]["policies"][name], summary["venues"][b]["policies"][name]
            ha, hb = summary["venues"][a]["policies"]["E8_HASH_TRAIL"], summary["venues"][b]["policies"]["E8_HASH_TRAIL"]
            if (pa["mar_delta_vs_ctrl"] and pb["mar_delta_vs_ctrl"]
                    and pa["mar_delta_vs_ctrl"] > 0 and pb["mar_delta_vs_ctrl"] > 0
                    and pa["dd_delta_vs_ctrl"] >= 0 and pb["dd_delta_vs_ctrl"] >= 0
                    and pa["mar"] > ha["mar"] and pb["mar"] > hb["mar"]):
                winners.append(name)
    summary["both_venue_winners"] = winners
    (out / "book_e_dynamic_tp.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"both_venue_winners": winners}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
