"""P3-4 — ledger-level portfolio simulation of the residual-momentum gate (no engine surgery).

P3-3 gave the selected subset's residual Sharpe; this gives the strategy-level metric the demo
arbiter uses: the GATED book's portfolio MAR / DD / return. Faithful first-order reconstruction
from the age300 ledger (no engine change): attach rmom7_lag1 (PIT) to each trade, gate out the
HIGH-rmom half (keep the idiosyncratically-weak = better shorts), and rebuild the monthly portfolio
P&L as sum(net_return * position_weight) by exit_month -> compound -> MAR / monthly-DD. Compares
full age300 vs LOW-rmom-gated vs HIGH-rmom, both venues + recent third.

CAVEAT: first-order/CONSERVATIVE — keeps original position_weights and does NOT refill the
max_active slots freed by dropped trades (a live engine gate would enter other candidates), so this
UNDERSTATES the real gate. It bounds the gate's portfolio benefit from below.

Read-only, EXPLORATORY. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p3_4_gated_portfolio_sim.py
"""
from __future__ import annotations

import csv, json, math, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402
from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
START, END = "2023-04-01", "2026-05-28"
MS_PER_DAY = 86_400_000
RECENT_CUT = "2025-06"
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}
LEDGER = ("e2_exhaustion_select_2026-05-30", "02_age_min")
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]


def _monthly_stats(trades):
    # trades: list of dicts with net_return, position_weight, exit_month
    by_month: dict = {}
    for t in trades:
        by_month.setdefault(t["exit_month"], 0.0)
        by_month[t["exit_month"]] += t["net_return"] * t["position_weight"]
    if len(by_month) < 6:
        return None
    months = sorted(by_month)
    eq = 1.0; peak = 1.0; maxdd = 0.0; rets = []
    for m in months:
        r = by_month[m]; rets.append(r); eq *= (1.0 + r); peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1.0)
    total = eq - 1.0
    mar = total / abs(maxdd) if maxdd < 0 else float("nan")
    mean = sum(rets) / len(rets); var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    sharpe = (mean / math.sqrt(var) * math.sqrt(12)) if var > 0 else float("nan")
    return {"total_return": round(total, 3), "max_dd": round(maxdd, 3), "MAR": round(mar, 2),
            "ann_sharpe": round(sharpe, 2), "n_months": len(months), "n_trades": len(trades)}


def main() -> int:
    print("P3-4 gated-portfolio sim (ledger-level, conservative; gate=drop high residual-momentum)\n", flush=True)
    out = {}
    for venue, sub in VENUES.items():
        root = SHARED / sub
        if not root.exists():
            continue
        print(f"[{venue}] build panel + rmom ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            continue
        _fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)
        resid = resid.sort(["symbol", "ts_ms"]).with_columns(
            pl.col("residual_return").rolling_sum(window_size=7, min_samples=4).shift(1).over("symbol").alias("rmom"))
        rmap = {(r["symbol"], r["ts_ms"]): r["rmom"] for r in resid.iter_rows(named=True)}
        rows = list(csv.DictReader(open(root / "reports" / LEDGER[0] / LEDGER[1] / "volume_event_best_trades.csv")))
        recs = []
        for r in rows:
            try:
                day = (int(r["entry_signal_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
                rm = rmap.get((r["symbol"], day))
                if rm is None:
                    continue
                recs.append({"rmom": float(rm), "net_return": float(r["net_return"]),
                             "position_weight": float(r["position_weight"]), "exit_month": r["exit_month"]})
            except (KeyError, ValueError):
                continue
        recs.sort(key=lambda x: x["rmom"]); h = len(recs) // 2
        groups = {"full_age300": recs, "LOW_rmom_gated": recs[:h], "HIGH_rmom": recs[h:]}
        out[venue] = {"matched": len(recs)}
        print(f"[{venue}] matched={len(recs)}", flush=True)
        for name, g in groups.items():
            full = _monthly_stats(g)
            recent = _monthly_stats([t for t in g if t["exit_month"] >= RECENT_CUT])
            out[venue][name] = {"full": full, "recent": recent}
            if full:
                rmar = recent["MAR"] if recent else float("nan")
                print(f"[{venue}]   {name:15s} ret={full['total_return']:+.2f} DD={full['max_dd']:+.2f} "
                      f"MAR={full['MAR']:+.2f} Sh={full['ann_sharpe']:+.2f} n={full['n_trades']}  recentMAR={rmar:+.2f}", flush=True)
        print(flush=True)
    (SHARED / "p3_4_gated_portfolio_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p3_4_gated_portfolio_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
