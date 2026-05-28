"""R1 — Per-filter audit dispatcher (Round 2, Investigation tier).

Pre-reg: docs/preregistration/round2/r1-per-filter-audit.md
Parent plan: docs/preregistration/round2/integrated-strategy-program.md

6 cells (1 baseline + 4 LOO + 1 joint-drop) × 2 venues = 12 runs. Window
2023-04-01 -> 2026-04-30 (cross-venue minimum; Binance data ends 2026-04-30).
Decision rule per the Round 2 Investigation tier (MAR-primary,
softer-than-Manifesto, no production-change-without-R10/R11).

Dispatch:

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/Scripts/python.exe scripts/r1_filter_audit_sweep.py

Per-cell reports land under
``~/SHARED_DATA/{bybit,binance}_full_pit/reports/r1_filter_audit_2026-05-28/<cell>/``.
Aggregate summary CSV at ``~/SHARED_DATA/r1_filter_audit_2026-05-28_summary.csv``.

Apply the decision rule afterwards:

    .venv/Scripts/python.exe scripts/apply_decision_rule.py \\
      ~/SHARED_DATA/r1_filter_audit_2026-05-28_summary.csv \\
      --control R1_baseline_v2 \\
      --rule investigation
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the sibling _sweep_runtime importable without polluting sys.path globally.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "r1_filter_audit_2026-05-28"
START_DATE = "2023-04-01"
END_DATE = "2026-04-30"  # Binance USD-M klines coverage limit; see pre-reg.

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Production baseline = current promoted profile. Matches the
# `volume_events_cell.sh` wrapper's baseline table and
# `phase0_loo_sweep.py`'s BASELINE_PARAMS. Each R1 cell overrides exactly
# the flag(s) it's named for; everything else stays at baseline.
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
    "--liquidity-migration-rank-direction": "improvement",
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
    Cell("R1_baseline_v2", "production filter stack (Round 2 control)"),
    Cell(
        "R1_drop_day_return",
        "drop day-return floor (Phase 0 LOO showed near-no-op)",
        {"--liquidity-migration-day-return-min": "-1.0"},
    ),
    Cell(
        "R1_drop_stop_pressure",
        "drop stop-pressure veto (Phase 0 LOO showed near-no-op)",
        {"--stop-pressure-stop-count": "999"},
    ),
    Cell(
        "R1_drop_both_noops",
        "drop both day_return + stop_pressure jointly (catch interactions)",
        {
            "--liquidity-migration-day-return-min": "-1.0",
            "--stop-pressure-stop-count": "999",
        },
    ),
    Cell(
        "R1_retest_rank_max",
        "drop universe-rank upper bound (Phase 0 LOO suggested mild benefit on removal)",
        {"--universe-rank-max": "99999"},
    ),
    Cell(
        "R1_retest_realized_loss",
        "drop realized-loss-pressure veto (Phase 0 LOO suggested Bybit benefit on removal)",
        {"--realized-loss-pressure-loss-count": "999"},
    ),
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
