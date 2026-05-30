"""P3-2 — does residual-momentum SELECTION extract Tier-3 residual alpha? (decisive)

P3-1b found a robust, PIT-clean, cross-venue residual-momentum signal (rmom7_lag1 IC -0.19 bybit /
-0.35 binance) vs the age300 short's net_return. But net_return contains factor exposure. The
decisive question: does SELECTING the idiosyncratically-weak candidates (low rmom7_lag1) produce a
subset whose **residual Sharpe** clears the Tier-3 gate (>=+0.3) cross-venue? And does rmom predict
the per-trade FORWARD residual (not just net_return)? And does it hold in the recent third?

Method (both venues, common4 residuals = full coverage):
  * rmom7_lag1 per (symbol, signal-day), PIT (excludes signal-day residual).
  * decompose age300 trades -> per-trade forward residual.
  * IC(rmom7_lag1, net_return) and IC(rmom7_lag1, forward_residual), full window + recent(>=2025-06).
  * annualized residual Sharpe (common4) of: full age300 / LOW-rmom half (the signal's shorts) /
    HIGH-rmom half. Does the LOW-rmom half clear +0.3 both venues?
Read-only, EXPLORATORY. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p3_2_residual_gate_validation.py
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
MS_PER_DAY = 86_400_000
RECENT_CUT = "2025-06-01"
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}
LEDGER = ("e2_exhaustion_select_2026-05-30", "02_age_min")
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]


def _spear(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None and not (isinstance(a, float) and math.isnan(a)) and not (isinstance(b, float) and math.isnan(b))]
    n = len(pairs)
    if n < 20:
        return float("nan")
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    def rk(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[o[j + 1]] == v[o[i]]: j += 1
            for k in range(i, j + 1): r[o[k]] = (i + j) / 2.0
            i = j + 1
        return r
    rx, ry = rk(xs), rk(ys); mx = sum(rx) / n; my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx)); vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx > 0 and vy > 0 else float("nan")


def _ann_resid_sharpe(residuals, entry_dates):
    resid = [r for r in residuals if r is not None and not math.isnan(r)]
    if len(resid) < 5:
        return float("nan")
    mean = sum(resid) / len(resid)
    var = sum((x - mean) ** 2 for x in resid) / (len(resid) - 1)
    if var <= 0:
        return float("nan")
    per_trade = mean / math.sqrt(var)
    d = sorted(entry_dates)
    span = (date.fromisoformat(d[-1]) - date.fromisoformat(d[0])).days or 1
    return per_trade * math.sqrt(len(d) / (span / 365.0))


def main() -> int:
    print("P3-2 residual-momentum gate validation (common4 residuals)\n", flush=True)
    out = {}
    for venue, sub in VENUES.items():
        root = SHARED / sub
        if not root.exists():
            continue
        print(f"[{venue}] build panel + residuals ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            continue
        fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)
        resid = resid.sort(["symbol", "ts_ms"]).with_columns(
            pl.col("residual_return").rolling_sum(window_size=7, min_samples=4).shift(1).over("symbol").alias("rmom7_lag1")
        )
        rmap = {(r["symbol"], r["ts_ms"]): r["rmom7_lag1"] for r in resid.iter_rows(named=True)}
        rows = list(csv.DictReader(open(root / "reports" / LEDGER[0] / LEDGER[1] / "volume_event_best_trades.csv")))
        # decompose for per-trade forward residual (common4)
        trades = pl.DataFrame([{"symbol": r["symbol"], "entry_ts_ms": int(r["entry_ts_ms"]),
                                "hold_days": max(1, round(float(r.get("hold_hours") or 0) / 24)),
                                "realized_return": float(r["net_return"])} for r in rows])
        dec = decompose_strategy_pnl(trades, panel, fr, factor_cols=COMMON4)
        presid = {(t["symbol"], t["entry_ts_ms"]): t["residual"] for t in dec["per_trade"].iter_rows(named=True)}
        recs = []
        for r in rows:
            try:
                day = (int(r["entry_signal_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
                rm = rmap.get((r["symbol"], day))
                if rm is None:
                    continue
                fres = presid.get((r["symbol"], int(r["entry_ts_ms"])))
                recs.append({"rmom": float(rm), "net": float(r["net_return"]), "fresid": fres,
                             "date": r["entry_date"][:10]})
            except (KeyError, ValueError):
                continue
        out[venue] = {"matched": len(recs), "total": len(rows)}
        ic_net = _spear([(x["rmom"], x["net"]) for x in recs])
        ic_res = _spear([(x["rmom"], x["fresid"]) for x in recs])
        rec_recent = [x for x in recs if x["date"] >= RECENT_CUT]
        ic_net_r = _spear([(x["rmom"], x["net"]) for x in rec_recent])
        ic_res_r = _spear([(x["rmom"], x["fresid"]) for x in rec_recent])
        # split by rmom median -> low half (signal's shorts) vs high half; residual Sharpe each
        valid = [x for x in recs if x["fresid"] is not None and not math.isnan(x["fresid"])]
        valid.sort(key=lambda x: x["rmom"]); h = len(valid) // 2
        full_rs = _ann_resid_sharpe([x["fresid"] for x in valid], [x["date"] for x in valid])
        lo_rs = _ann_resid_sharpe([x["fresid"] for x in valid[:h]], [x["date"] for x in valid[:h]])
        hi_rs = _ann_resid_sharpe([x["fresid"] for x in valid[h:]], [x["date"] for x in valid[h:]])
        out[venue].update({"ic_net": round(ic_net, 3), "ic_resid": round(ic_res, 3),
                           "ic_net_recent": round(ic_net_r, 3), "ic_resid_recent": round(ic_res_r, 3),
                           "ann_resid_sharpe_full": round(full_rs, 3), "ann_resid_sharpe_LOWrmom": round(lo_rs, 3),
                           "ann_resid_sharpe_HIGHrmom": round(hi_rs, 3), "n_low": h, "n_high": len(valid) - h})
        print(f"[{venue}] matched={len(recs)}/{len(rows)}  IC(rmom,net)={ic_net:+.3f} IC(rmom,RESID)={ic_res:+.3f}  "
              f"recent: net={ic_net_r:+.3f} resid={ic_res_r:+.3f}", flush=True)
        print(f"[{venue}] ann residual Sharpe: full={full_rs:+.2f}  LOW-rmom(signal's shorts)={lo_rs:+.2f}  "
              f"HIGH-rmom={hi_rs:+.2f}  (Tier-3 gate +0.3)", flush=True)
        print(flush=True)
    (SHARED / "p3_2_residual_gate_validation_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p3_2_residual_gate_validation_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
