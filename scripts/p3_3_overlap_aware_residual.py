"""P3-3 — overlap-aware residual Sharpe of the residual-momentum-selected subset (resolves the P3 caveat).

P3-2 found the LOW-rmom subset clears the Tier-3 residual gate (+0.47 bybit / +1.25 binance) but the
sqrt(trades/yr) annualization is optimistic given overlapping ~3-day-hold trades, AND IC(rmom,residual)
is weak — so that lift may be an annualization artifact. This re-computes the residual Sharpe with an
OVERLAP-AWARE annualization: bucket trades by ISO entry-week, average the per-trade residual within each
week (~non-overlapping for 3-day holds), and annualize the weekly series by sqrt(52). Reports per-trade
sqrt(trades/yr) AND weekly-overlap-aware Sharpe for full / LOW-rmom / HIGH-rmom, both venues + recent/early.

Read-only, EXPLORATORY. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p3_3_overlap_aware_residual.py
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


def _per_trade_ann(resids, dates):
    r = [x for x in resids if x is not None and not math.isnan(x)]
    if len(r) < 5: return float("nan")
    m = sum(r) / len(r); v = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    if v <= 0: return float("nan")
    d = sorted(dates); span = (date.fromisoformat(d[-1]) - date.fromisoformat(d[0])).days or 1
    return (m / math.sqrt(v)) * math.sqrt(len(r) / (span / 365.0))


def _weekly_ann(recs):  # recs: list of (resid, date_str); overlap-aware via ISO-week buckets
    buckets: dict = {}
    for resid, ds in recs:
        if resid is None or math.isnan(resid): continue
        iso = date.fromisoformat(ds).isocalendar()
        buckets.setdefault((iso[0], iso[1]), []).append(resid)
    weekly = [sum(v) / len(v) for v in buckets.values()]
    if len(weekly) < 5: return float("nan"), len(weekly)
    m = sum(weekly) / len(weekly); v = sum((x - m) ** 2 for x in weekly) / (len(weekly) - 1)
    if v <= 0: return float("nan"), len(weekly)
    return (m / math.sqrt(v)) * math.sqrt(52.0), len(weekly)


def main() -> int:
    print("P3-3 overlap-aware residual Sharpe (weekly buckets) — resolves the annualization caveat\n", flush=True)
    out = {}
    for venue, sub in VENUES.items():
        root = SHARED / sub
        if not root.exists(): continue
        print(f"[{venue}] build panel + residuals ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty(): continue
        fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)
        resid = resid.sort(["symbol", "ts_ms"]).with_columns(
            pl.col("residual_return").rolling_sum(window_size=7, min_samples=4).shift(1).over("symbol").alias("rmom"))
        rmap = {(r["symbol"], r["ts_ms"]): r["rmom"] for r in resid.iter_rows(named=True)}
        rows = list(csv.DictReader(open(root / "reports" / LEDGER[0] / LEDGER[1] / "volume_event_best_trades.csv")))
        trades = pl.DataFrame([{"symbol": r["symbol"], "entry_ts_ms": int(r["entry_ts_ms"]),
                                "hold_days": max(1, round(float(r.get("hold_hours") or 0) / 24)),
                                "realized_return": float(r["net_return"])} for r in rows])
        dec = decompose_strategy_pnl(trades, panel, fr, factor_cols=COMMON4)
        presid = {(t["symbol"], t["entry_ts_ms"]): t["residual"] for t in dec["per_trade"].iter_rows(named=True)}
        recs = []
        for r in rows:
            try:
                day = (int(r["entry_signal_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
                rm = rmap.get((r["symbol"], day)); fres = presid.get((r["symbol"], int(r["entry_ts_ms"])))
                if rm is None or fres is None or math.isnan(fres): continue
                recs.append({"rmom": float(rm), "fresid": float(fres), "date": r["entry_date"][:10]})
            except (KeyError, ValueError):
                continue
        recs.sort(key=lambda x: x["rmom"]); h = len(recs) // 2
        subsets = {"full": recs, "LOW_rmom": recs[:h], "HIGH_rmom": recs[h:]}
        out[venue] = {"n_matched": len(recs)}
        print(f"[{venue}] matched={len(recs)}", flush=True)
        for name, s in subsets.items():
            pt = _per_trade_ann([x["fresid"] for x in s], [x["date"] for x in s])
            wk, nwk = _weekly_ann([(x["fresid"], x["date"]) for x in s])
            recent = [x for x in s if x["date"] >= RECENT_CUT]
            wk_r, _ = _weekly_ann([(x["fresid"], x["date"]) for x in recent])
            out[venue][name] = {"n": len(s), "ann_resid_sharpe_pertrade": round(pt, 3),
                                "ann_resid_sharpe_weekly": round(wk, 3), "n_weeks": nwk,
                                "weekly_recent": round(wk_r, 3)}
            print(f"[{venue}]   {name:9s} n={len(s):4d}  per-trade-ann={pt:+.2f}  WEEKLY-overlap-aware={wk:+.2f} "
                  f"(weeks={nwk})  weekly-recent={wk_r:+.2f}", flush=True)
        print(flush=True)
    (SHARED / "p3_3_overlap_aware_residual_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p3_3_overlap_aware_residual_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
