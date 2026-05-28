"""R2 — per-feature standalone decile-sort sweep.

Pre-reg: docs/preregistration/round2/r2-per-feature-standalone.md
Parent plan: docs/preregistration/round2/integrated-strategy-program.md

For each venue (bybit, binance):
  1. Build the feature panel ONCE (all 5 R2 features + realized_vol_7d
     + fwd_ret_{1,3,7}d).
  2. For each (feature × horizon): run decile_spread_pnl + summarize.
  3. Compute Spearman correlation matrix on h=3 daily P&L.
  4. Compute PCA variance shares on h=3 daily P&L.

Outputs:
  ~/SHARED_DATA/r2_per_feature_standalone_2026-05-28_summary.csv
    Per-cell rows: cell_id, venue, feature, horizon, window_days,
                   n_signal_days, total_signals, total_return,
                   max_drawdown, sharpe_like, mar, annualized_return.
  ~/SHARED_DATA/r2_per_feature_standalone_2026-05-28_correlation_<venue>.csv
    5×5 Spearman matrix.
  ~/SHARED_DATA/r2_per_feature_standalone_2026-05-28_pca_<venue>.json
    {"feature_order": [...], "explained_variance_ratio": [...],
     "cumulative_variance": [...]}.

Dispatch:
  POLARS_MAX_THREADS=8 .venv/Scripts/python.exe scripts/r2_per_feature_standalone_sweep.py
"""
from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# These imports require the path insertion above.
from liquidity_migration.r2_decile_sort import (  # noqa: E402
    decile_spread_pnl,
    pca_variance_shares,
    spearman_correlation_matrix,
    summarize_pnl_series,
)
from liquidity_migration.signal_harness import build_feature_panel  # noqa: E402

SWEEP_TAG = "r2_per_feature_standalone_2026-05-28"
START_DATE = "2023-04-01"
END_DATE = "2026-04-30"
HORIZONS = (1, 3, 7)
CORRELATION_HORIZON = 3  # the strongest Phase 5 IC horizon

# 5 features from Round 1 Phase 5 + realized_vol_7d for risk-equal weighting.
FEATURES = (
    "vol_of_vol_30d",
    "realized_vol_7d",
    "dist_from_30d_low",
    "xs_rank_ret_7d",
    "xs_rank_ret_3d",
)

SHARED = Path.home() / "SHARED_DATA"
VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}


def _window_days(start: str, end: str) -> int:
    from datetime import datetime
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    return max(0, (e - s).days)


def run_one_venue(venue: str, data_root: Path) -> tuple[list[dict], dict[str, pl.DataFrame]]:
    """Build panel + run all 15 (feature × horizon) cells for one venue.

    Returns:
      ``rows`` — per-cell summary dicts ready to write to the sweep CSV.
      ``h3_pnls`` — per-feature h=3 daily P&L frames (for correlation + PCA).
    """
    if not data_root.exists():
        print(f"SKIP venue={venue}: data root not found at {data_root}", flush=True)
        return [], {}

    print(f"[{venue}] building feature panel...", flush=True)
    t0 = time.monotonic()
    panel = build_feature_panel(
        data_root,
        start=START_DATE,
        end=END_DATE,
        feature_specs=list(FEATURES),
        forward_horizons=HORIZONS,
    )
    print(f"[{venue}] panel built in {time.monotonic() - t0:.1f}s, "
          f"rows={panel.height}, columns={panel.columns}", flush=True)

    rows: list[dict] = []
    h3_pnls: dict[str, pl.DataFrame] = {}
    window_days = _window_days(START_DATE, END_DATE)

    for feature in FEATURES:
        for horizon in HORIZONS:
            cell_id = f"R2_{feature}_h{horizon}_{venue}"
            t_cell = time.monotonic()
            pnl = decile_spread_pnl(panel, feature=feature, horizon=horizon)
            summary = summarize_pnl_series(
                pnl,
                feature=feature,
                horizon=horizon,
                venue=venue,
                window_days=window_days,
            )
            elapsed = time.monotonic() - t_cell
            row = {
                "cell_id": cell_id,
                "venue": venue,
                "feature": feature,
                "horizon": horizon,
                "window_days": window_days,
                "start_date": START_DATE,
                "end_date": END_DATE,
                "n_signal_days": summary.n_signal_days,
                "total_signals": summary.total_signals,
                "total_return": f"{summary.total_return:.6f}",
                "annualized_return": f"{summary.annualized_return:.6f}",
                "max_drawdown": f"{summary.max_drawdown:.6f}",
                "sharpe_like": f"{summary.sharpe_like:.4f}",
                "mar": f"{summary.mar:.4f}",
                "elapsed_seconds": f"{elapsed:.1f}",
            }
            rows.append(row)
            print(
                f"  [{venue}/{cell_id}] OK ({elapsed:.1f}s)  "
                f"n_days={summary.n_signal_days}  n_signals={summary.total_signals}  "
                f"ret={summary.total_return:+.3f}  dd={summary.max_drawdown:+.3f}  "
                f"sharpe={summary.sharpe_like:+.2f}  MAR={summary.mar:+.2f}",
                flush=True,
            )
            if horizon == CORRELATION_HORIZON:
                h3_pnls[feature] = pnl.select("date", "daily_pnl")
            # Persist per-cell pnl frame (cheap; useful for verdict)
            pnl_dir = data_root / "reports" / SWEEP_TAG / cell_id
            pnl_dir.mkdir(parents=True, exist_ok=True)
            pnl.write_csv(pnl_dir / "daily_pnl.csv")
            (pnl_dir / "summary.json").write_text(
                json.dumps(asdict(summary), indent=2, sort_keys=True)
            )

    return rows, h3_pnls


def main() -> int:
    summary_path = SHARED / f"{SWEEP_TAG}_summary.csv"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    correlation_outputs: list[tuple[str, Path]] = []
    pca_outputs: list[tuple[str, Path]] = []

    sweep_start = time.monotonic()
    for venue, data_root in VENUES.items():
        rows, h3_pnls = run_one_venue(venue, data_root)
        all_rows.extend(rows)

        if not h3_pnls:
            continue
        feature_order = list(FEATURES)
        # Spearman matrix
        corr_path = SHARED / f"{SWEEP_TAG}_correlation_{venue}.csv"
        try:
            matrix = spearman_correlation_matrix(h3_pnls, feature_order)
            matrix.write_csv(corr_path)
            correlation_outputs.append((venue, corr_path))
            print(f"[{venue}] correlation matrix -> {corr_path}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{venue}] correlation matrix FAILED: {exc}", flush=True)

        # PCA
        pca_path = SHARED / f"{SWEEP_TAG}_pca_{venue}.json"
        try:
            pca = pca_variance_shares(h3_pnls, feature_order)
            pca_payload = {
                "venue": venue,
                "feature_order": feature_order,
                "horizon": CORRELATION_HORIZON,
                "explained_variance_ratio": pca["explained_variance_ratio"],
                "cumulative_variance": pca["cumulative_variance"],
            }
            pca_path.write_text(json.dumps(pca_payload, indent=2))
            pca_outputs.append((venue, pca_path))
            print(f"[{venue}] PCA -> {pca_path}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{venue}] PCA FAILED: {exc}", flush=True)

    # Flush summary CSV
    if all_rows:
        fieldnames = sorted({k for r in all_rows for k in r.keys()})
        with open(summary_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nDONE. wrote {len(all_rows)} rows to {summary_path}", flush=True)
    else:
        print("\nDONE. NO rows produced.", flush=True)

    print(f"Total wall: {(time.monotonic() - sweep_start) / 60:.1f} min", flush=True)
    print(f"Correlation matrices: {[str(p) for _, p in correlation_outputs]}", flush=True)
    print(f"PCA outputs: {[str(p) for _, p in pca_outputs]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
