"""15-cell × 2-venue parameter sweep dispatcher (LEGACY 2026-05-28 EXPLORATORY sweep).

Pre-reg: docs/preregistration/2026-05-28-liquidity-capacity-filter-and-filter-tweak-sweep.md
Verdict: REJECTED (commit 2f67746, 2026-05-27).

This script is kept for reproducibility of the 2026-05-28 sweep cells; the
parallel-execution machinery has been factored into ``scripts/_sweep_runtime.py``
so future phase orchestrators (Phase 0 LOO audit, Phase 1 universe diagnostic,
Phase 2 direction grid, Phase 4 hybrid events, Phase 6 combined-portfolio) can
all share the same dispatch + summary-flush + atomic-print primitives.

EXPLORATORY label: not promotion evidence. Decision rule (a priori): a cell
qualifies only if Sharpe Δ ≥ +0.5 vs baseline on BOTH venues AND max-DD Δ ≤
+5pp on both. See pre-reg doc for the full rule.

Dispatch:

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/Scripts/python.exe scripts/sweep_cells.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "sweep_2026-05-28"

# Window: 2025-01-01 → 2026-05-28 (~17 months). Trimmed from the 2023-26
# in-sample window because the sweep is exploratory + computationally
# bounded — each cell takes ~3-5 min on this window vs ~10 min on
# 2024-01-01+. Sample size of ~300+ trades is still enough to rank cells
# by Sharpe / DD. Conclusions here are EXPLORATORY only; promotion would
# need a full-window re-run.
START_DATE = "2025-01-01"
END_DATE = "2026-05-28"

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}


# Baseline = current production promoted profile (matches deploy/systemd
# bybit-demo + scripts/run_fullpit_volume_overnight.sh canonical cell)
BASELINE_PARAMS: dict[str, str] = {
    "--event-types": "liquidity_migration",
    "--thresholds": "0.4",
    "--hold-days": "3",
    "--sides": "reversal",
    "--stop-loss-pcts": "0.12",
    "--take-profit-pcts": "0.26",
    "--cost-multipliers": "3",
    "--gross-exposure": "1.0",
    "--entry-delay-hours": "1",
    "--entry-policy": "promoted_quality_squeeze",
    "--max-active-symbols": "3",
    "--cooldown-days": "5",
    "--rank-exit-threshold": "0.55",
    "--universe-rank-min": "31",
    "--universe-rank-max": "400",
    "--liquidity-migration-rank-improvement-min": "150",
    "--liquidity-migration-turnover-ratio-min": "6.0",
    "--liquidity-migration-event-rank-fraction-max": "0.90",
    "--liquidity-migration-day-return-min": "0.0",
    "--liquidity-migration-residual-return-min": "0.08",
    "--liquidity-migration-close-location-min": "0.30",
    "--liquidity-migration-pit-age-days-min": "90",
    "--liquidity-migration-crowding-filter": "union_pathology",
    "--stop-pressure-window-days": "10",
    "--stop-pressure-stop-count": "7",
    "--realized-loss-pressure-window-days": "5",
    "--realized-loss-pressure-loss-count": "6",
}


CELLS: list[Cell] = [
    Cell("00_baseline", "current promoted defaults (control)"),
    # Group A — liquidity capacity (the operator's hypothesis)
    # REQ on 2026-05-27 had turnover ~$5.7M; $5M floor barely keeps REQ in;
    # $10M floor excludes REQ-class names. $50M is the "majors only" extreme.
    Cell("A2_turnover_5M",  "min turnover $5M/day",   {"--universe-min-daily-turnover": "5000000"}),
    Cell("A3_turnover_10M", "min turnover $10M/day",  {"--universe-min-daily-turnover": "10000000"}),
    Cell("A4_turnover_50M", "min turnover $50M/day",  {"--universe-min-daily-turnover": "50000000"}),
    # Group B — rank-improvement tightening
    Cell("B1_rankimp_200", "rank_improvement_min 200", {"--liquidity-migration-rank-improvement-min": "200"}),
    # Group C — residual-return tightening
    Cell("C1_residret_12", "residual_return_min 0.12", {"--liquidity-migration-residual-return-min": "0.12"}),
    # Group D — hold period (confirm prior 2026-05-23 finding)
    Cell("D1_hold2", "hold_days 2", {"--hold-days": "2"}),
    # Group E — universe-rank tightening
    Cell("E1_rankmax_200", "universe_rank_max 200", {"--universe-rank-max": "200"}),
    # Group F — combos: best individual filter ideas stacked
    Cell("F1_turnover10M_hold2", "$10M + h=2",
         {"--universe-min-daily-turnover": "10000000", "--hold-days": "2"}),
    Cell("F3_turnover10M_hold2_residret12", "$10M + h=2 + resid 0.12",
         {"--universe-min-daily-turnover": "10000000", "--hold-days": "2",
          "--liquidity-migration-residual-return-min": "0.12"}),
]


def main() -> int:
    summary_path = SHARED / f"{SWEEP_TAG}_summary.csv"
    return run_sweep(
        CELLS,
        VENUES,
        baseline_params=BASELINE_PARAMS,
        start_date=START_DATE,
        end_date=END_DATE,
        sweep_tag=SWEEP_TAG,
        summary_path=summary_path,
    )


if __name__ == "__main__":
    sys.exit(main())
