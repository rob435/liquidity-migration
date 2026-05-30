"""P2-2/3 — residual-alpha leaderboard across SELECTION configs.

P2-1 showed residual alpha is a SELECTION property (per-trade, size-agnostic). So the question
"can this be real alpha?" = which selection config has the highest residual Sharpe? Decompose
several existing ledgers (no new backtests) on one factor-panel build per venue, under full6 +
common4 factor sets, and rank by annualized residual Sharpe (Tier-3 gate >= +0.3).

Configs (existing ledgers):
  baseline_15bps     e2 00_baseline      (age90, production filters, 15bps)
  age300_15bps       e2 02_age_min       (the Part-1 winner, 15bps)
  age400_15bps       e2b 02_age400
  drop_all_4         r5 R5_baseline_dollar_equal  (the big MAR lever; note: older cost config)
  drop_all_4_rebase  r1 R1_drop_all_4
Cost caveat: the r5/r1 ledgers may be at a different cost (x3) than the e2 15bps ones; residual
SHAPE is still comparable but absolute levels carry that caveat.

Read-only. Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p2_2_residual_leaderboard.py
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
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]
# (label, sweep_tag, cell)
LEDGERS = [
    ("baseline_15bps",    "e2_exhaustion_select_2026-05-30", "00_baseline"),
    ("age300_15bps",      "e2_exhaustion_select_2026-05-30", "02_age_min"),
    ("age400_15bps",      "e2b_age_combo_2026-05-30",        "02_age400"),
    ("drop_all_4",        "r5_position_sizing_2026-05-29",   "R5_baseline_dollar_equal"),
    ("drop_all_4_rebase", "r1_rebaseline_hardened_2026-05-29", "R1_drop_all_4"),
]


def _trades_df(cell_dir: Path) -> pl.DataFrame | None:
    p = cell_dir / "volume_event_best_trades.csv"
    if not p.exists():
        return None
    rows = []
    for r in csv.DictReader(open(p)):
        try:
            hh = float(r.get("hold_hours") or 0.0)
            rows.append({"symbol": r["symbol"], "entry_ts_ms": int(r["entry_ts_ms"]),
                         "hold_days": max(1, round(hh / 24.0)), "realized_return": float(r["net_return"]),
                         "entry_date": r["entry_date"][:10]})
        except (KeyError, ValueError):
            continue
    return pl.DataFrame(rows) if rows else None


def _annf(trades: pl.DataFrame) -> float:
    d = sorted(trades["entry_date"].to_list())
    if len(d) < 2:
        return 1.0
    span = (date.fromisoformat(d[-1]) - date.fromisoformat(d[0])).days or 1
    return math.sqrt(len(d) / (span / 365.0))


def main() -> int:
    print(f"P2-2/3 residual leaderboard  window={START}->{END}  Tier-3 gate ann resid Sharpe>=+0.3\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            continue
        print(f"[{venue}] build factor panel ...", flush=True)
        panel = build_factor_panel(root, start=START, end=END)
        if panel.is_empty():
            continue
        fr6, _ = fit_factor_returns(panel, factor_cols=list(_FACTOR_COLUMNS))
        fr4, _ = fit_factor_returns(panel, factor_cols=COMMON4)
        out[venue] = {}
        print(f"[{venue}] {'config':20s} {'n':>5} {'resid6':>7} {'res6_ok':>7} {'resid4':>7} {'res4_ok':>7}  realized", flush=True)
        for label, tag, cell in LEDGERS:
            trades = _trades_df(root / "reports" / tag / cell)
            if trades is None:
                print(f"[{venue}] {label:20s} (ledger missing)"); continue
            annf = _annf(trades)
            r6 = decompose_strategy_pnl(trades, panel, fr6, factor_cols=list(_FACTOR_COLUMNS))
            r4 = decompose_strategy_pnl(trades, panel, fr4, factor_cols=COMMON4)
            rr = float(trades["realized_return"].mean())
            rec = {"n": r6["n_trades"],
                   "ann_resid_full6": round(r6["residual_sharpe"] * annf, 3), "resolved6": round(r6["resolved_fraction"], 2),
                   "ann_resid_common4": round(r4["residual_sharpe"] * annf, 3), "resolved4": round(r4["resolved_fraction"], 2),
                   "mean_realized": round(rr, 6)}
            out[venue][label] = rec
            print(f"[{venue}] {label:20s} {rec['n']:5d} {rec['ann_resid_full6']:+7.2f} "
                  f"(r{rec['resolved6']:.2f}) {rec['ann_resid_common4']:+7.2f} (r{rec['resolved4']:.2f})  {rr:+.5f}", flush=True)
        print(flush=True)
    (SHARED / "p2_2_residual_leaderboard_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p2_2_residual_leaderboard_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
