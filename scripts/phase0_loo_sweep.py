"""Phase 0 — filter leave-one-out audit dispatcher.

Pre-reg: docs/preregistration/2026-05-27-phase0-filter-loo-audit.md

15 cells (1 baseline + 14 LOO) × 2 venues = 30 runs. Window 2023-04-01 →
2026-04-30 (cross-venue minimum; Binance data ends 2026-04-30). Decision
rule per the Strictness Manifesto in the parent plan.

Dispatch:

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/Scripts/python.exe scripts/phase0_loo_sweep.py

Per-cell reports land under
``~/SHARED_DATA/{bybit,binance}_full_pit/reports/phase0_loo_2026-05-27/<cell>/``.
Aggregate summary CSV at ``~/SHARED_DATA/phase0_loo_2026-05-27_summary.csv``.

Apply the decision rule afterwards:

    python scripts/apply_decision_rule.py \\
      ~/SHARED_DATA/phase0_loo_2026-05-27_summary.csv \\
      --control 00_baseline
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the sibling _sweep_runtime importable without polluting sys.path globally.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "phase0_loo_2026-05-27"
START_DATE = "2023-04-01"
END_DATE = "2026-04-30"  # Binance USD-M klines coverage limit; see pre-reg.

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Production baseline = current promoted profile (matches Appendix A of the
# parent plan, also matches the volume_events_cell.sh wrapper's BASELINE
# table). Cells override one knob at a time to leave that filter OUT.
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


# Each Phase 0 cell disables ONE filter (or knob). Other knobs at baseline.
# The `00_baseline` control runs the full production stack — its metrics
# are the reference the decision-rule analyzer uses.
CELLS: list[Cell] = [
    Cell("00_baseline", "production baseline (control)"),
    Cell("P0_noflt_turnover_ratio",   "drop turnover-ratio gate",       {"--liquidity-migration-turnover-ratio-min": "0"}),
    Cell("P0_noflt_event_rank_frac",  "drop event-rank-fraction max",   {"--liquidity-migration-event-rank-fraction-max": "1.0"}),
    Cell("P0_noflt_day_return",       "drop day-return floor",          {"--liquidity-migration-day-return-min": "-1.0"}),
    Cell("P0_noflt_residual_return",  "drop residual-return floor",     {"--liquidity-migration-residual-return-min": "0"}),
    Cell("P0_noflt_close_location",   "drop close-location floor",      {"--liquidity-migration-close-location-min": "0"}),
    Cell("P0_noflt_pit_age",          "drop PIT-age floor",             {"--liquidity-migration-pit-age-days-min": "0"}),
    Cell("P0_noflt_crowding",         "drop crowding-detection family", {"--liquidity-migration-crowding-filter": "none"}),
    Cell("P0_noflt_stop_pressure",    "drop stop-pressure veto",        {"--stop-pressure-stop-count": "999"}),
    Cell("P0_noflt_realized_loss",    "drop realized-loss-pressure veto", {"--realized-loss-pressure-loss-count": "999"}),
    Cell("P0_noflt_rank_min",         "drop universe-rank lower bound", {"--universe-rank-min": "1"}),
    Cell("P0_noflt_rank_max",         "drop universe-rank upper bound", {"--universe-rank-max": "99999"}),
    Cell("P0_noflt_cooldown",         "drop per-symbol cooldown",       {"--cooldown-days": "0"}),
    Cell("P0_noflt_max_active",       "drop concurrent-position cap",   {"--max-active-symbols": "999"}),
    Cell("P0_noflt_entry_delay",      "drop 1h entry delay",            {"--entry-delay-hours": "0"}),
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
