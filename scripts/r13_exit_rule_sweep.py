"""R13 — exit-rule re-optimization dispatcher (Round 2, conditional on R1).

Pre-reg: docs/preregistration/round2/integrated-strategy-program.md
         (sub-phase R13, added 2026-05-28)

**Why this phase exists.** Round 2's R-phases re-optimize the ENTRY (filters,
features, sizing, cost) but no phase re-optimizes the EXIT rule, even though the
2026-05-23 exit-ladder sweep found exits (failed_fade params + holding period)
were among the highest-leverage knobs (+18% Sharpe). The promoted exit
(take_profit 0.26, failed_fade off, rank_exit 0.55) was tuned for the OLD entry
population. R1's lead candidate `drop_all_4` changes that population, so the
optimal exit very likely shifts. R13 re-optimizes the exit ON the drop_all_4
entry population — strictly conditional on R1 confirming drop_all_4.

**Baseline = R1 lead candidate** (r1_filter_audit_sweep.py baseline + the 4
filter drops). Every cell varies ONLY exit knobs, so trade ENTRIES are identical
across cells — differences are pure exit-rule effects. 8 cells x 2 venues = 16
runs. Window 2023-04-01 -> 2026-05-28 (matches R1).

**Decision rule — Tier-1 Investigation** (carry-forward only; this is in-sample,
no OOS consumed): MAR Delta > 0 on the majority of venues vs 00_baseline_drop4,
no return sign-flip, >=30 Bybit / >=20 Binance trades. Verdict via
scripts/r1_robustness.py --sweep-tag r13_exit_rule_2026-05-28. A winning exit
cell feeds R9 assembly; it does NOT skip the OOS / forward-demo gates.

Dispatch — desktop 5950X (16C/32T):

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/bin/python -u scripts/r13_exit_rule_sweep.py

Per-cell reports land under
``~/SHARED_DATA/{bybit,binance}_full_pit/reports/r13_exit_rule_2026-05-28/<cell>/``.
configs/volume_alpha.default.yaml is no-touch.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "r13_exit_rule_2026-05-28"
START_DATE = "2023-04-01"
END_DATE = "2026-05-28"

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Baseline = R1 lead candidate = r1_filter_audit_sweep.py BASELINE_PARAMS with
# the 4 filters dropped (drop_all_4). Kept inline (repo convention: each sweep
# script is self-contained) and MUST stay identical to r1's baseline + drops so
# the entry population matches the candidate whose exits we re-optimize.
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

# Each cell overrides ONLY exit knobs. Entries are therefore identical across
# cells; any metric delta is a pure exit-rule effect.
CELLS: list[Cell] = [
    Cell("00_baseline_drop4", "drop_all_4 lead candidate, promoted exit (control)"),
    Cell("R13_tp21", "take-profit 0.21 (earlier profit-take)", {"--take-profit-pcts": "0.21"}),
    Cell("R13_tp30", "take-profit 0.30 (let winners run)", {"--take-profit-pcts": "0.30"}),
    Cell(
        "R13_ff6_3pct",
        "failed_fade 6h / 3% loss / 1% mfe (2026-05-23 finding)",
        {
            "--failed-fade-exit-hours": "6",
            "--failed-fade-loss-pct": "0.03",
            "--failed-fade-min-mfe-pct": "0.01",
            "--failed-fade-close-location-min": "0.0",
        },
    ),
    Cell(
        "R13_ff6_4pct",
        "failed_fade 6h / 4% loss / 1% mfe (demo_relaxed values)",
        {
            "--failed-fade-exit-hours": "6",
            "--failed-fade-loss-pct": "0.04",
            "--failed-fade-min-mfe-pct": "0.01",
            "--failed-fade-close-location-min": "0.0",
        },
    ),
    Cell("R13_rankexit_045", "rank-exit threshold 0.45 (exit sooner on rank decay)", {"--rank-exit-threshold": "0.45"}),
    Cell("R13_rankexit_065", "rank-exit threshold 0.65 (hold through more decay)", {"--rank-exit-threshold": "0.65"}),
    Cell("R13_stop10", "tighter fixed stop 0.10", {"--stop-loss-pcts": "0.10"}),
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
