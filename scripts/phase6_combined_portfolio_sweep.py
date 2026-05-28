"""Phase 6 — combined-signal portfolio dispatcher.

Pre-reg: docs/preregistration/round1/phase6-combined-signal-portfolio.md
Verdict trigger: docs/preregistration/round1/phase5-verdict.md
  5 survivors pinned: vol_of_vol_30d, realized_vol_7d, dist_from_30d_low,
  xs_rank_ret_7d, xs_rank_ret_3d (all negative IC; short-side signal).

21 cell configurations x 2 venues = 42 runs. Each cell takes a feature
panel + the surviving feature list + weighting/decile/horizon knobs and
produces a per-day position ledger. The cells run via the
`signal-harness combined-portfolio` CLI subcommand.

NOTE: Phase 6 cells are CALCULATION-ONLY (no event-driven backtest
simulation). The output is a position ledger; equity-curve metrics
(Sharpe, DD, drawdown 90d, sub-period sign-consistency) get computed
from the panel's forward returns × per-cell weight. Manifesto candidate
criteria then apply.

Dispatch:

    SWEEP_MAX_WORKERS=4 POLARS_MAX_THREADS=4 \\
      .venv/Scripts/python.exe -u scripts/phase6_combined_portfolio_sweep.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import polars as pl

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
# script-mode invocation puts SCRIPT_DIR on sys.path[0]; add REPO_ROOT so
# `from liquidity_migration import ...` resolves to the package in the repo
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT))
from _sweep_runtime import SHARED  # noqa: E402

from liquidity_migration import signal_harness as sh  # noqa: E402

SWEEP_TAG = "phase6_combined_portfolio_2026-05-27"

# Pinned from Phase 5b survivors (ic-weights are Bybit mean_ic values).
# Closing turnover_delta_30d per FDR ceiling (would be rank 6).
SURVIVORS = [
    "vol_of_vol_30d",
    "realized_vol_7d",
    "dist_from_30d_low",
    "xs_rank_ret_7d",
    "xs_rank_ret_3d",
]
IC_WEIGHTS = {
    "vol_of_vol_30d":    -0.0965,
    "realized_vol_7d":   -0.0880,
    "dist_from_30d_low": -0.0741,
    "xs_rank_ret_7d":    -0.0423,
    "xs_rank_ret_3d":    -0.0401,
}

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}


@dataclass
class P6Cell:
    cell_id: str
    weighting: str         # "equal" or "ic_weighted"
    top_decile: float      # 0.05, 0.10, 0.20
    forward_horizon: int   # 1, 3, 7
    description: str


def _enumerate_cells() -> list[P6Cell]:
    out: list[P6Cell] = []
    # Core 3 schemes (default horizon = 3, top_decile = 0.10)
    out.append(P6Cell("P6_equal_z", "equal", 0.10, 3, "equal-Z combination, top-decile=0.10, fwd_3d"))
    out.append(P6Cell("P6_ic_weighted", "ic_weighted", 0.10, 3, "IC-weighted Z, top-decile=0.10, fwd_3d"))
    out.append(P6Cell("P6_top_decile_short", "equal", 0.10, 3, "alias for equal-Z top-decile-short"))
    # Horizon sweep: 3 schemes × {1d, 7d} (skip 3d to avoid dup with core)
    for h in (1, 7):
        out.append(P6Cell(f"P6_horiz_equal_{h}d", "equal", 0.10, h, f"equal-Z top-decile, fwd_{h}d"))
        out.append(P6Cell(f"P6_horiz_icwt_{h}d", "ic_weighted", 0.10, h, f"IC-weighted top-decile, fwd_{h}d"))
        out.append(P6Cell(f"P6_horiz_topdec_{h}d", "equal", 0.10, h, f"alias top-decile, fwd_{h}d"))
    # Decile sweep: 3 schemes × {5%, 20%} (skip 10% to avoid dup with core)
    for d in (0.05, 0.20):
        d_label = f'{int(d*100):02d}'
        out.append(P6Cell(f"P6_dec_equal_{d_label}", "equal", d, 3, f"equal-Z top-{d_label}%, fwd_3d"))
        out.append(P6Cell(f"P6_dec_icwt_{d_label}", "ic_weighted", d, 3, f"IC-weighted top-{d_label}%, fwd_3d"))
        out.append(P6Cell(f"P6_dec_topdec_{d_label}", "equal", d, 3, f"alias top-{d_label}%, fwd_3d"))
    return out


CELLS: list[P6Cell] = _enumerate_cells()


def _compute_portfolio_metrics(portfolio: pl.DataFrame, *, forward_horizon: int) -> dict:
    """Synthetic equity-curve metrics from per-day position weights × forward returns.

    The Phase 5 panel's forward returns are entry+1h -> entry+1h+Nd, so a
    cell's per-day PnL = sum(weight × fwd_ret_Nd) across active positions.
    Sharpe-like = daily mean / daily std × sqrt(365) (annualised, no Rf).
    """
    fwd_col = f"fwd_ret_{forward_horizon}d"
    active = portfolio.filter(
        (pl.col("position_side") != "flat") & pl.col(fwd_col).is_not_null()
    )
    if active.height == 0:
        return {
            "trades": 0, "total_return": 0.0, "sharpe_like": 0.0,
            "max_drawdown": 0.0, "worst_90d": 0.0,
            "sub_period_returns": [0.0, 0.0, 0.0],
            "promotable": False,
        }
    # Per-day net return = sum(weight × fwd) across active positions
    per_day = (
        active.group_by("ts_ms", maintain_order=True)
        .agg([(pl.col("weight") * pl.col(fwd_col)).sum().alias("day_pnl"),
              pl.len().alias("n_pos")])
        .sort("ts_ms")
    )
    pnls = per_day["day_pnl"].to_list()
    n_days = len(pnls)
    if n_days == 0:
        return {"trades": 0, "total_return": 0.0, "sharpe_like": 0.0,
                "max_drawdown": 0.0, "worst_90d": 0.0,
                "sub_period_returns": [0.0]*3, "promotable": False}
    # Cumulative return (compounded daily)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    eq_path = []
    for p in pnls:
        equity *= (1.0 + p)
        peak = max(peak, equity)
        dd = (equity - peak) / peak if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
        eq_path.append(equity)
    total_return = equity - 1.0
    mean = sum(pnls) / n_days
    var = sum((p - mean)**2 for p in pnls) / max(n_days - 1, 1)
    std = math.sqrt(var)
    sharpe_like = mean / std * math.sqrt(365) if std > 0 else 0.0
    # Worst 90d rolling drawdown
    worst_90d = 0.0
    for i in range(n_days):
        start_eq = eq_path[max(0, i-90)]
        worst = (eq_path[i] - start_eq) / start_eq if start_eq > 0 else 0.0
        worst_90d = min(worst_90d, worst)
    # Sub-period thirds (chunk by row-index)
    chunk = max(n_days // 3, 1)
    sub_rets = []
    for i in range(3):
        lo = i * chunk
        hi = (i + 1) * chunk if i < 2 else n_days
        if hi > lo:
            sub_eq = 1.0
            for p in pnls[lo:hi]:
                sub_eq *= (1.0 + p)
            sub_rets.append(sub_eq - 1.0)
        else:
            sub_rets.append(0.0)
    return {
        "trades": int(active.height),
        "total_return": total_return,
        "sharpe_like": sharpe_like,
        "max_drawdown": max_dd,
        "worst_90d": worst_90d,
        "sub_period_returns": sub_rets,
        "promotable": False,
    }


def _run_cell(cell: P6Cell, venue: str, data_root: Path) -> dict:
    panel_path = data_root / "feature_panel_2026-05-27.parquet"
    if not panel_path.exists():
        return {"venue": venue, "cell_id": cell.cell_id, "description": cell.description,
                "status": "no_panel", "error": f"missing {panel_path}"}
    start = time.monotonic()
    print(f"  [{venue}/{cell.cell_id}] START  {cell.description}", flush=True)
    panel = pl.read_parquet(panel_path)
    try:
        portfolio = sh.build_combined_signal_portfolio(
            panel,
            surviving_features=SURVIVORS,
            weighting=cell.weighting,
            ic_weights=IC_WEIGHTS if cell.weighting == "ic_weighted" else None,
            top_decile=cell.top_decile,
            vol_target_per_name=0.01,
            forward_horizon=cell.forward_horizon,
        )
        metrics = _compute_portfolio_metrics(portfolio, forward_horizon=cell.forward_horizon)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - start
        msg = f"{type(exc).__name__}: {exc}"
        print(f"  [{venue}/{cell.cell_id}] FAILED ({elapsed:.1f}s) {msg}", flush=True)
        return {"venue": venue, "cell_id": cell.cell_id, "description": cell.description,
                "status": "failed", "elapsed_seconds": f"{elapsed:.1f}", "error": msg}
    elapsed = time.monotonic() - start
    # Persist per-cell portfolio + metrics
    out_dir = data_root / "reports" / SWEEP_TAG / cell.cell_id
    out_dir.mkdir(parents=True, exist_ok=True)
    portfolio.write_parquet(out_dir / "portfolio.parquet")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(
        f"  [{venue}/{cell.cell_id}] OK ({elapsed:.1f}s)  "
        f"trades={metrics['trades']}  ret={metrics['total_return']:+.4f}  "
        f"dd={metrics['max_drawdown']:.4f}  sharpe={metrics['sharpe_like']:+.4f}",
        flush=True,
    )
    return {
        "venue": venue,
        "cell_id": cell.cell_id,
        "description": cell.description,
        "status": "ok",
        "elapsed_seconds": f"{elapsed:.1f}",
        "trades": str(metrics["trades"]),
        "total_return": f"{metrics['total_return']:.4f}",
        "sharpe_like": f"{metrics['sharpe_like']:.4f}",
        "max_drawdown": f"{metrics['max_drawdown']:.4f}",
        "worst_90d": f"{metrics['worst_90d']:.4f}",
        "promotable": "False",
        "report_dir": str(out_dir),
    }


def main() -> int:
    import csv
    import threading
    summary_path = SHARED / f"{SWEEP_TAG}_summary.csv"
    work = [(c, v, root) for v, root in VENUES.items() for c in CELLS]
    print(f"sweep summary -> {summary_path}")
    print(f"cells: {len(CELLS)}  venues: {len(VENUES)}  total runs: {len(work)}  "
          f"survivors: {SURVIVORS}")
    print()
    rows: list[dict] = []
    rows_lock = threading.Lock()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    def write_summary():
        if not rows:
            return
        fieldnames = sorted({k for r in rows for k in r.keys()})
        with open(summary_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as ex:
        from concurrent.futures import as_completed
        futures = {ex.submit(_run_cell, c, v, r): (c, v) for c, v, r in work}
        for fut in as_completed(futures):
            row = fut.result()
            with rows_lock:
                rows.append(row)
                write_summary()
    print(f"\nDONE. {len(rows)} cells in {(time.monotonic()-start)/60:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
