"""P2-1b — residual-alpha decomposition, RESOLUTION-FIXED cross-venue.

P2-1 (v1) found the age gate converts factor exposure -> residual alpha on bybit (resolved=1.0),
but binance resolved only 0.63-0.66 (< the 0.70 trust threshold) because the funding/premium
factors are sparse on binance and drop symbol-days from the daily regression. This re-runs the
decomposition under TWO factor sets per venue:
  * full6   = all 6 R4 factors (btc_beta, xs_rank_ret_30d, realized_vol_rank, funding_rate_z,
              liquidity_rank, premium_index_z)
  * common4 = the 4 always-present klines/price factors (drop funding_rate_z, premium_index_z)
and prints per-factor null fractions. common4 should restore full resolution on binance and give
an apples-to-apples cross-venue residual-Sharpe comparison.

Read-only. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p2_1b_residual_alpha_v2.py
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
from liquidity_migration.risk_model import (  # noqa: E402
    build_factor_panel, fit_factor_returns, decompose_strategy_pnl, _FACTOR_COLUMNS,
)

SHARED = Path.home() / "SHARED_DATA"
START, END = "2023-04-01", "2026-05-28"
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
SWEEP = "e2_exhaustion_select_2026-05-30"
CELLS = {"baseline_age90": "00_baseline", "age300": "02_age_min"}
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]
FACTOR_SETS = {"full6": list(_FACTOR_COLUMNS), "common4": COMMON4}


def _trades_df(cell_dir: Path) -> pl.DataFrame:
    rows = []
    with open(cell_dir / "volume_event_best_trades.csv") as f:
        for r in csv.DictReader(f):
            try:
                hh = float(r.get("hold_hours") or 0.0)
                rows.append({"symbol": r["symbol"], "entry_ts_ms": int(r["entry_ts_ms"]),
                             "hold_days": max(1, round(hh / 24.0)), "realized_return": float(r["net_return"]),
                             "entry_date": r["entry_date"][:10]})
            except (KeyError, ValueError):
                continue
    return pl.DataFrame(rows)


def _annf(trades: pl.DataFrame) -> float:
    d = sorted(trades["entry_date"].to_list())
    if len(d) < 2:
        return 1.0
    span = (date.fromisoformat(d[-1]) - date.fromisoformat(d[0])).days or 1
    return math.sqrt(len(d) / (span / 365.0))


def main() -> int:
    print(f"P2-1b resolution-fixed decomposition  window={START}->{END}  Tier-3 gate ann resid Sharpe>=+0.3\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}"); continue
        print(f"[{venue}] build factor panel ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            print(f"[{venue}] empty panel"); continue
        nulls = {f: round(panel[f].null_count() / panel.height, 3) for f in _FACTOR_COLUMNS if f in panel.columns}
        print(f"[{venue}] panel rows={panel.height}  null_frac={nulls}", flush=True)
        out[venue] = {"null_frac": nulls}
        for set_name, fcols in FACTOR_SETS.items():
            fr, _ = fit_factor_returns(panel, factor_cols=fcols)
            for label, cell in CELLS.items():
                cdir = root / "reports" / SWEEP / cell
                trades = _trades_df(cdir)
                res = decompose_strategy_pnl(trades, panel, fr, factor_cols=fcols)
                annf = _annf(trades)
                rr = trades["realized_return"].to_numpy()
                rec = {
                    "n_trades": res["n_trades"], "resolved_fraction": round(res["resolved_fraction"], 3),
                    "mean_realized": round(float(rr.mean()), 6), "mean_residual": round(res["mean_residual"], 6),
                    "ann_residual_sharpe": round(res["residual_sharpe"] * annf, 3),
                    "tier3_pass": bool(res["residual_sharpe"] * annf >= 0.3),
                }
                out[venue][f"{set_name}/{label}"] = rec
                print(f"[{venue}] {set_name:7s} {label:14s} n={rec['n_trades']:4d} resolved={rec['resolved_fraction']:.2f}  "
                      f"realized={rec['mean_realized']:+.5f} resid={rec['mean_residual']:+.5f}  "
                      f"ANN_RESID_SHARPE={rec['ann_residual_sharpe']:+.2f}  Tier3: {'PASS' if rec['tier3_pass'] else 'fail'}", flush=True)
        print(flush=True)
    (SHARED / "p2_1b_residual_alpha_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p2_1b_residual_alpha_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
