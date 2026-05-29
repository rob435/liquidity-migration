"""R3 — Bearish-stack honest test (H2 retried), Round 2.

Pre-reg: docs/research_summary.md sub-phase R3.

Round-1 Phase-2 found H2 "falsified-by-construction": the bullish quality gates
(day_return >= 0, residual >= 0.08, close_location >= 0.30, rank improvement)
exclude bearish names, so the deterioration direction produced ~0 trades. R3
tests the deterioration direction HONESTLY by mirror-imaging those quality gates
(the engine already supports every mirror: rank_direction=deterioration + the
*_max bounds, validated min<=max). Load-bearing filters (crowding, turnover,
event_rank_frac, entry_delay, cooldown, universe_rank, pit_age) are KEPT — R1
confirmed they are decisive.

Baseline / control = the R1 lead candidate `drop_all_4` (re-baseline cascade).
`R3_bearish_only` mirrors ONLY the direction/quality gates the control uses;
the drop_all_4 entry-population drops (stop_pressure, realized_loss, rank_max)
are held identical so the only difference is the bullish->bearish mirror.

2 cells x 2 venues = 4 runs (Tier-1 Investigation). The pre-reg's third cell,
`R3_market_neutral` (separate long+short slot pools), needs a parallel-pool
combination harness rather than one volume-events config; per the plan it is the
"most interesting IF both legs are investigation-positive" cell, so it is run as
a CONDITIONAL follow-up only if `R3_bearish_only` clears the Investigation bar.

Decision: Tier-1 Investigation vs `R3_baseline_v2`. If `R3_bearish_only` is
investigation-positive => the deterioration direction carries short-side edge =>
opens a parallel bearish R9 line + triggers R3_market_neutral. If negative => H2
is decisively closed (bearish direction has no edge even under proper filters).
Verdict via apply_decision_rule.py --rule investigation --control R3_baseline_v2.

Dispatch (5950X, full-PIT, SERIAL — 23 GB/cell):
    SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/r3_bearish_stack_sweep.py
Windows: ``$env:SWEEP_MAX_WORKERS=1; $env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\r3_bearish_stack_sweep.py``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Full-PIT klines reads peak ~23 GB/cell: declare it so _sweep_runtime's
# memory-aware default drops to 1 worker on a 32 GB box even if the operator
# forgets SWEEP_MAX_WORKERS=1 (an explicit env value still wins over this).
os.environ.setdefault("SWEEP_CELL_GB", "23")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "r3_bearish_stack_2026-05-29"
START_DATE = "2023-04-01"
END_DATE = "2026-05-28"

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Baseline = drop_all_4 bullish stack (IDENTICAL to r13/r5 BASELINE_PARAMS).
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
    "--max-active-symbols": "12",
    "--cooldown-days": "5",
    "--rank-exit-threshold": "0.55",
    "--universe-rank-min": "31",
    "--universe-rank-max": "99999",          # drop_all_4
    "--liquidity-migration-rank-improvement-min": "150",
    "--liquidity-migration-rank-direction": "improvement",
    "--liquidity-migration-turnover-ratio-min": "6.0",
    "--liquidity-migration-event-rank-fraction-max": "0.90",
    "--liquidity-migration-day-return-min": "-1.0",   # drop_all_4
    "--liquidity-migration-residual-return-min": "0.08",
    "--liquidity-migration-close-location-min": "0.30",
    "--liquidity-migration-pit-age-days-min": "90",
    "--liquidity-migration-crowding-filter": "union_pathology",
    "--stop-pressure-window-days": "10",
    "--stop-pressure-stop-count": "999",              # drop_all_4
    "--realized-loss-pressure-window-days": "5",
    "--realized-loss-pressure-loss-count": "999",     # drop_all_4
}

# Bearish mirror: flip the 4 direction/quality gates (rank, residual,
# close_location, day_return). Turn each bullish *_min OFF (non-binding) and set
# the mirror *_max. Validation requires min <= max — satisfied (e.g. residual
# min -10 <= max -0.08). Load-bearing filters unchanged.
BEARISH_ONLY = {
    "--liquidity-migration-rank-direction": "deterioration",
    "--liquidity-migration-residual-return-min": "-10.0",
    "--liquidity-migration-residual-return-max": "-0.08",
    "--liquidity-migration-close-location-min": "0.0",
    "--liquidity-migration-close-location-max": "0.70",
    "--liquidity-migration-day-return-min": "-10.0",
    "--liquidity-migration-day-return-max": "0.0",
}

CELLS: list[Cell] = [
    Cell("R3_baseline_v2", "drop_all_4 bullish stack (control)"),
    Cell("R3_bearish_only", "mirror-imaged bearish quality stack (deterioration direction)", dict(BEARISH_ONLY)),
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
