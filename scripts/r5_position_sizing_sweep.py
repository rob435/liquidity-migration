"""R5 — 1/realized-vol position-sizing sweep (Round 2).

Pre-reg: docs/research_summary.md (sub-phase R5).

Tests ``risk_equal`` sizing (absolute ``target_vol_per_name / realized_vol``,
clamped) against the dollar-equal baseline, on the R1 ``drop_all_4`` re-baselined
entry stack with the promoted exit. Entries + exits are IDENTICAL across all cells
(same 816 by / 509 bn signals), so every metric delta is a **pure sizing effect**.
4 cells x 2 venues = 8 runs, window 2023-04-01 -> 2026-05-28 (matches R1/R13).

Decision rule — Tier-1 Investigation (carry-forward only; in-sample, no OOS
consumed): MAR Delta > 0 on the majority of venues vs ``R5_baseline_dollar_equal``,
no return sign-flip, >=30 by / >=20 bn trades. The sizing winner pins R9's
``target_vol_per_name`` knob. Verdict via
``scripts/apply_decision_rule.py --rule investigation --control R5_baseline_dollar_equal``
+ ``scripts/r1_robustness.py --sweep-tag r5_position_sizing_2026-05-29 --control R5_baseline_dollar_equal``.
A winning sizing cell feeds R9; it does NOT skip the R11 OOS / forward-demo gates.

Dispatch — desktop 5950X, full-PIT, memory-safe SERIAL (one full-PIT cell peaks
~23 GB; 8 workers OOMs on a 32 GB box):

    SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/r5_position_sizing_sweep.py

Windows: ``$env:SWEEP_MAX_WORKERS=1; $env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\r5_position_sizing_sweep.py``
configs/volume_alpha.default.yaml is no-touch.
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

SWEEP_TAG = "r5_position_sizing_2026-05-29"
START_DATE = "2023-04-01"
END_DATE = "2026-05-28"

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Baseline = the R1 lead candidate (drop_all_4 entries) + promoted exit, IDENTICAL
# to r13_exit_rule_sweep.py's BASELINE_PARAMS so the entry+exit population matches
# the carry-forward candidate. Cells override ONLY the sizing knobs.
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
    "--universe-rank-max": "99999",          # drop_all_4: rank_max dropped
    "--liquidity-migration-rank-improvement-min": "150",
    "--liquidity-migration-rank-direction": "improvement",
    "--liquidity-migration-turnover-ratio-min": "6.0",
    "--liquidity-migration-event-rank-fraction-max": "0.90",
    "--liquidity-migration-day-return-min": "-1.0",   # drop_all_4: day_return dropped
    "--liquidity-migration-residual-return-min": "0.08",
    "--liquidity-migration-close-location-min": "0.30",
    "--liquidity-migration-pit-age-days-min": "90",
    "--liquidity-migration-crowding-filter": "union_pathology",
    "--stop-pressure-window-days": "10",
    "--stop-pressure-stop-count": "999",              # drop_all_4: stop_pressure dropped
    "--realized-loss-pressure-window-days": "5",
    "--realized-loss-pressure-loss-count": "999",     # drop_all_4: realized_loss dropped
}

# Each cell overrides ONLY sizing knobs. The control takes no override (engine
# default = dollar-equal), so it reproduces r13's 00_baseline_drop4 bit-for-bit.
CELLS: list[Cell] = [
    Cell("R5_baseline_dollar_equal", "drop_all_4 + promoted exit, dollar-equal sizing (control)"),
    Cell("R5_risk_equal_1pct", "risk_equal, target vol 1.0%/name/day",
         {"--position-weighting": "risk_equal", "--target-vol-per-name": "0.01"}),
    Cell("R5_risk_equal_1.5pct", "risk_equal, target vol 1.5%/name/day",
         {"--position-weighting": "risk_equal", "--target-vol-per-name": "0.015"}),
    Cell("R5_risk_equal_2pct", "risk_equal, target vol 2.0%/name/day",
         {"--position-weighting": "risk_equal", "--target-vol-per-name": "0.02"}),
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
