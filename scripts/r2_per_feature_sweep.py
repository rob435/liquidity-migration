"""R2 — per-feature standalone decile-sort + correlation/PCA (Round 2).

Pre-reg: docs/preregistration/round2/r2-per-feature-standalone.md (and the parent
integrated-strategy-program.md sub-phase R2).

DESCRIPTIVE phase (no promotion): for each of the 5 Round-1 Phase-5 IC survivors,
run a daily top-decile short decile-sort at horizons {1,3,7}d on each venue, then
compute the 5x5 Spearman correlation matrix + PCA variance shares of the
per-feature daily P&L (at the 3d reference horizon). Output feeds R9 feature
grouping; no single feature graduates alone.

Full-PIT by construction: `signal_harness.build_feature_panel` reads the
``*_full_pit`` root's klines, which include the full delisted-inclusive PIT
universe, so the cross-sectional decile ranks are NOT survivorship-biased. (This
is the signal-harness path, NOT the volume-events engine, so the --allow-partial-pit
flag never applied.) Runs IN-PROCESS, one venue's panel at a time (peak ~15-23 GB)
— memory-safe on the 32 GB box without parallel workers.

Dispatch (5950X):
    POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/r2_per_feature_sweep.py
Windows: ``$env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\r2_per_feature_sweep.py``

Artifacts (under ~/SHARED_DATA):
    r2_per_feature_2026-05-29_summary.csv          (venue x feature x horizon metrics)
    r2_per_feature_2026-05-29_correlation_<venue>.csv  (5x5 Spearman on 3d P&L)
    r2_per_feature_2026-05-29_pca_<venue>.json     (PCA variance shares on 3d P&L)
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

# UTF-8 + line-buffered stdout (Windows cp1252 crashes on any non-ASCII; matches
# scripts/_sweep_runtime.py). Prints below are ASCII anyway, belt-and-suspenders.
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.r2_decile_sort import (  # noqa: E402
    decile_spread_pnl,
    pca_variance_shares,
    spearman_correlation_matrix,
    summarize_pnl_series,
)
from liquidity_migration.signal_harness import build_feature_panel  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
TAG = "r2_per_feature_2026-05-29"
START_DATE = "2023-04-01"
END_DATE = "2026-05-28"

# The 5 Round-1 Phase-5 IC survivors (all negative IC = short-side). realized_vol_7d
# doubles as the risk-weight denominator for decile_spread_pnl(use_risk_weights=True).
FEATURES = [
    "vol_of_vol_30d",
    "realized_vol_7d",
    "dist_from_30d_low",
    "xs_rank_ret_7d",
    "xs_rank_ret_3d",
]
HORIZONS = (1, 3, 7)
CORR_HORIZON = 3  # Phase-5 reference horizon for correlation + PCA

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}


def _window_days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def main() -> int:
    window_days = _window_days(START_DATE, END_DATE)
    feature_specs = ",".join(FEATURES)
    summary_rows: list[dict] = []

    print(f"R2 per-feature standalone  tag={TAG}  window={START_DATE}->{END_DATE} ({window_days}d)")
    print(f"features={FEATURES}  horizons={HORIZONS}  corr/pca horizon={CORR_HORIZON}d")
    print(f"venues={list(VENUES)}  feature_specs={feature_specs}\n")

    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: data root not found at {root}")
            continue
        print(f"[{venue}] building feature panel from {root} ...", flush=True)
        panel = build_feature_panel(
            root, start=START_DATE, end=END_DATE,
            feature_specs=feature_specs, forward_horizons=HORIZONS,
        )
        print(f"[{venue}] panel rows={panel.height}  cols={len(panel.columns)}", flush=True)
        if panel.is_empty():
            print(f"[{venue}] EMPTY panel -- skipping")
            continue

        corr_pnl: dict = {}  # feature -> per-day pnl frame at CORR_HORIZON
        for feature in FEATURES:
            for horizon in HORIZONS:
                pnl = decile_spread_pnl(panel, feature=feature, horizon=horizon)
                summ = summarize_pnl_series(
                    pnl, feature=feature, horizon=horizon, venue=venue, window_days=window_days,
                )
                summary_rows.append(asdict(summ))
                if horizon == CORR_HORIZON:
                    corr_pnl[feature] = pnl.select("date", "daily_pnl")
                print(
                    f"  [{venue}] {feature:<18} h={horizon}d  ret={summ.total_return:+.3f}x  "
                    f"dd={summ.max_drawdown:+.2%}  MAR={summ.mar:+.2f}  sharpe={summ.sharpe_like:+.2f}  "
                    f"signals={summ.total_signals}",
                    flush=True,
                )

        # 5x5 Spearman correlation + PCA on the CORR_HORIZON per-feature P&L.
        corr = spearman_correlation_matrix(corr_pnl, FEATURES)
        corr_path = SHARED / f"{TAG}_correlation_{venue}.csv"
        corr.write_csv(corr_path)
        pca = pca_variance_shares(corr_pnl, FEATURES)
        pca_path = SHARED / f"{TAG}_pca_{venue}.json"
        pca_path.write_text(json.dumps(pca, indent=2))
        evr = pca.get("explained_variance_ratio", [])
        top2 = sum(evr[:2]) if evr else float("nan")
        print(f"[{venue}] wrote {corr_path.name}, {pca_path.name}  (top-2 PCA variance share={top2:.1%})\n", flush=True)

    if summary_rows:
        summary_path = SHARED / f"{TAG}_summary.csv"
        fieldnames = list(summary_rows[0].keys())
        with open(summary_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(summary_rows)
        print(f"DONE. wrote {len(summary_rows)} rows -> {summary_path}")
    else:
        print("DONE. no summary rows (all venues skipped/empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
