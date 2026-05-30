"""P2-1 — Tier-3 residual-alpha decomposition of the age gate.

Plan: docs/research_plan_part2.md §P2-1.

Question: is the discrete age-gate edge REAL ALPHA, or just factor exposure (the gate removes
high-vol freshly-listed names -> a low-vol / short-beta tilt)? Decompose the baseline and age300
trade ledgers through the validated 6-factor risk model (risk_model.decompose_strategy_pnl) and
report the **annualized residual Sharpe** (Tier-3 gate >= +0.3) on BOTH venues.

If age300 residual Sharpe >= +0.3 cross-venue AND >= baseline -> the edge survives factor
stripping = real alpha. If < baseline or < 0.3 -> "selling vol / buying beta", not alpha.

Read-only. One venue at a time (panel build is memory-heavy). EXPLORATORY-grade infra reuse but
the residual-Sharpe number is the literal Tier-3 gate metric.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p2_1_residual_alpha.py
"""
from __future__ import annotations

import csv
import json
import math
import sys
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
    build_factor_panel, fit_factor_returns, decompose_strategy_pnl,
)

SHARED = Path.home() / "SHARED_DATA"
START, END = "2023-04-01", "2026-05-28"
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
# the realistic 15bps E2 run: 00_baseline (age90) vs 02_age_min (age300)
SWEEP = "e2_exhaustion_select_2026-05-30"
CELLS = {"baseline_age90": "00_baseline", "age300": "02_age_min"}


def _trades_df(cell_dir: Path) -> pl.DataFrame:
    rows = []
    with open(cell_dir / "volume_event_best_trades.csv") as f:
        for r in csv.DictReader(f):
            try:
                hh = float(r.get("hold_hours") or 0.0)
                rows.append({
                    "symbol": r["symbol"],
                    "entry_ts_ms": int(r["entry_ts_ms"]),
                    "hold_days": max(1, round(hh / 24.0)),
                    "realized_return": float(r["net_return"]),
                    "entry_date": r["entry_date"][:10],
                })
            except (KeyError, ValueError):
                continue
    return pl.DataFrame(rows)


def _ann_factor(trades: pl.DataFrame) -> float:
    d = sorted(trades["entry_date"].to_list())
    if len(d) < 2:
        return 1.0
    span = (date.fromisoformat(d[-1]) - date.fromisoformat(d[0])).days or 1
    tpy = len(d) / (span / 365.0)
    return math.sqrt(tpy)


def main() -> int:
    print(f"P2-1 residual-alpha decomposition  window={START}->{END}  Tier-3 gate: ann residual Sharpe >= +0.3\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}"); continue
        print(f"[{venue}] build factor panel ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            print(f"[{venue}] empty panel -- skip"); continue
        print(f"[{venue}] panel rows={panel.height}; fit factor returns ...", flush=True)
        factor_returns, _resid = fit_factor_returns(panel)
        out[venue] = {}
        for label, cell in CELLS.items():
            cdir = root / "reports" / SWEEP / cell
            if not (cdir / "volume_event_best_trades.csv").exists():
                print(f"[{venue}] {label}: ledger missing"); continue
            trades = _trades_df(cdir)
            res = decompose_strategy_pnl(trades, panel, factor_returns)
            annf = _ann_factor(trades)
            ann_resid = res["residual_sharpe"] * annf
            # raw per-trade Sharpe (realized) for comparison
            rr = trades["realized_return"].to_numpy()
            raw_sharpe = float(rr.mean() / rr.std(ddof=1)) if rr.size > 1 and rr.std(ddof=1) > 0 else 0.0
            ann_raw = raw_sharpe * annf
            rec = {
                "n_trades": res["n_trades"], "resolved_fraction": round(res["resolved_fraction"], 3),
                "mean_realized": round(float(rr.mean()), 6), "mean_residual": round(res["mean_residual"], 6),
                "mean_explained": round(float(rr.mean()) - res["mean_residual"], 6),
                "residual_sharpe_per_trade": round(res["residual_sharpe"], 4),
                "ann_residual_sharpe": round(ann_resid, 3),
                "ann_raw_sharpe": round(ann_raw, 3),
                "tier3_pass": bool(ann_resid >= 0.3),
            }
            out[venue][label] = rec
            print(f"[{venue}] {label:14s} n={rec['n_trades']:4d} resolved={rec['resolved_fraction']:.2f}  "
                  f"realized={rec['mean_realized']:+.5f}=expl({rec['mean_explained']:+.5f})+resid({rec['mean_residual']:+.5f})  "
                  f"ANN_RESID_SHARPE={rec['ann_residual_sharpe']:+.2f} (raw {rec['ann_raw_sharpe']:+.2f})  "
                  f"Tier3>=0.3: {'PASS' if rec['tier3_pass'] else 'fail'}", flush=True)
        print(flush=True)
    (SHARED / "p2_1_residual_alpha_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p2_1_residual_alpha_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
