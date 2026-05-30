"""P3b verdict — overlap-aware residual Sharpe of the engine-gated cells (the certification metric).

After the P3b backtest (scripts/p3b_rmom_gate_dispatch.sh), decompose the 00_baseline (age300) and
01_rmom_gated cells' ledgers through the 6-factor model (common4) and report the OVERLAP-AWARE
(weekly-bucketed) annualized residual Sharpe + per-trade + recent third, both venues. The gated
cell's weekly residual Sharpe >= +0.3 cross-venue = certified factor-neutral alpha (the prize).

Read-only. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p3b_verdict_decompose.py
"""
from __future__ import annotations

import csv, json, math, sys
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402
from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns, decompose_strategy_pnl  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
START, END = "2023-04-01", "2026-05-28"
RECENT_CUT = "2025-06-01"
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}
TAG = "p3b_rmom_gate_2026-05-30"
CELLS = ["00_baseline", "01_rmom_gated"]
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]


def _weekly_ann(pairs):  # pairs: (residual, date_str)
    buckets: dict = {}
    for resid, ds in pairs:
        if resid is None or math.isnan(resid): continue
        iso = date.fromisoformat(ds).isocalendar()
        buckets.setdefault((iso[0], iso[1]), []).append(resid)
    weekly = [sum(v) / len(v) for v in buckets.values()]
    if len(weekly) < 5: return float("nan"), len(weekly)
    m = sum(weekly) / len(weekly); var = sum((x - m) ** 2 for x in weekly) / (len(weekly) - 1)
    return (m / math.sqrt(var) * math.sqrt(52.0) if var > 0 else float("nan")), len(weekly)


def main() -> int:
    print(f"P3b verdict decompose  tag={TAG}  (overlap-aware weekly residual Sharpe; Tier-3 +0.3)\n", flush=True)
    out = {}
    for venue, sub in VENUES.items():
        root = SHARED / sub
        base = root / "reports" / TAG
        if not (base / "01_rmom_gated" / "volume_event_research_report.json").exists():
            print(f"[{venue}] gated cell missing — skip"); continue
        print(f"[{venue}] build panel + residuals ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty(): continue
        fr, _ = fit_factor_returns(panel, factor_cols=COMMON4)
        out[venue] = {}
        for cell in CELLS:
            cdir = base / cell
            p = cdir / "volume_event_best_trades.csv"
            if not p.exists():
                print(f"[{venue}] {cell}: ledger missing"); continue
            rows = list(csv.DictReader(open(p)))
            trades = pl.DataFrame([{"symbol": r["symbol"], "entry_ts_ms": int(r["entry_ts_ms"]),
                                    "hold_days": max(1, round(float(r.get("hold_hours") or 0) / 24)),
                                    "realized_return": float(r["net_return"])} for r in rows])
            dec = decompose_strategy_pnl(trades, panel, fr, factor_cols=COMMON4)
            presid = {(t["symbol"], t["entry_ts_ms"]): t["residual"] for t in dec["per_trade"].iter_rows(named=True)}
            pairs = [(presid.get((r["symbol"], int(r["entry_ts_ms"]))), r["entry_date"][:10]) for r in rows]
            wk, nwk = _weekly_ann(pairs)
            wk_recent, _ = _weekly_ann([(x, d) for x, d in pairs if d >= RECENT_CUT])
            # report JSON metrics
            rep = json.loads((cdir / "volume_event_research_report.json").read_text())["best_scenario"]
            ret = float(rep["total_return"]); dd = float(rep["max_drawdown"]); tr = int(rep["trades"])
            rec = {"n_trades": tr, "ret": round(ret, 3), "dd": round(dd, 3),
                   "MAR_dailyDD": round(ret / abs(dd), 2) if dd else None,
                   "resolved": round(dec["resolved_fraction"], 2),
                   "weekly_resid_sharpe": round(wk, 3), "n_weeks": nwk, "weekly_resid_recent": round(wk_recent, 3)}
            out[venue][cell] = rec
            print(f"[{venue}] {cell:13s} n={tr:4d} ret={ret:+.2f} DD={dd:+.2f} MAR={rec['MAR_dailyDD']}  "
                  f"resolved={rec['resolved']}  WEEKLY_RESID_SHARPE={wk:+.2f} (recent {wk_recent:+.2f}) Tier3>=+0.3", flush=True)
        print(flush=True)
    (SHARED / "p3b_verdict_decompose_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p3b_verdict_decompose_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
