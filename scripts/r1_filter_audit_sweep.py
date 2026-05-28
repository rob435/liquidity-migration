"""R1 — per-filter hypothesis audit dispatcher (Round 2), WIDE-FUNNEL variant.

Pre-reg: docs/preregistration/round2/integrated-strategy-program.md
         (sub-phase R1 + the 2026-05-28 max_active=12 amendment)

7 cells (1 baseline + lead candidate + 5 decomposition/re-test) × 2 venues =
14 runs. Window 2023-04-01 -> 2026-05-28.

**max_active_symbols = 12** (not the production 3): the goal is a LARGE trade
dataset that the IC features can later filter down. With gross_exposure 1.0
this is the same total risk spread over more names — diversifies away
coin-specific noise (more reliable edge estimate); the systematic/beta
exposure is still bounded by gross + the R4 factor caps. Because the baseline
itself now runs at 12 slots, results are NOT directly comparable to the
earlier max_active=3 exploratory drop_all_4 numbers — all cells compare to the
12-slot 00_baseline control.

The 4 dropped filters and their override values are the canonical leave-one-out
definitions: dropping a filter = setting its threshold to a non-binding
sentinel. R1_drop_all_4 drops all four at once;
the single-drop / both-noops cells decompose which filter carries the effect.

Dispatch — desktop 5950X (16C/32T). Linux/macOS:

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/bin/python -u scripts/r1_filter_audit_sweep.py

Windows (PowerShell):

    $env:SWEEP_MAX_WORKERS=8; $env:POLARS_MAX_THREADS=4; \\
      .venv\\Scripts\\python.exe -u scripts\\r1_filter_audit_sweep.py

Per-cell reports land under
``~/SHARED_DATA/{bybit,binance}_full_pit/reports/r1_filter_audit_max12_2026-05-28/<cell>/``.
Aggregate summary CSV at ``~/SHARED_DATA/r1_filter_audit_max12_2026-05-28_summary.csv``.

Sub-period stability + bootstrap robustness + the Tier 2 demo-candidate verdict
are computed post-hoc from the per-cell ledgers by scripts/r1_robustness.py
(--sweep-tag r1_filter_audit_max12_2026-05-28). The native engine splits flag
is YAML-only and configs/volume_alpha.default.yaml is no-touch.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "r1_filter_audit_max12_2026-05-28"
START_DATE = "2023-04-01"
END_DATE = "2026-05-28"  # matches the lead-candidate exploratory window

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Production baseline = current promoted profile (matches the
# volume_events_cell.sh wrapper's BASELINE table).
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
    # Wide funnel: 12 slots (NOT the production value of 3) to gather a large
    # trade dataset that the IC features can filter down post-hoc. Per the
    # 2026-05-28 amendment. gross_exposure stays 1.0, so each of 12 names is
    # ~8% of equity (same total risk, thinner slices) — the count is bounded
    # by risk (gross + R4 factor caps), not an arbitrary cap.
    "--max-active-symbols": "12",
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

# Non-binding sentinels = "drop this filter" (canonical leave-one-out values).
DROP_DAY_RETURN = {"--liquidity-migration-day-return-min": "-1.0"}
DROP_STOP_PRESSURE = {"--stop-pressure-stop-count": "999"}
DROP_REALIZED_LOSS = {"--realized-loss-pressure-loss-count": "999"}
DROP_RANK_MAX = {"--universe-rank-max": "99999"}

DROP_ALL_4 = {**DROP_DAY_RETURN, **DROP_STOP_PRESSURE, **DROP_REALIZED_LOSS, **DROP_RANK_MAX}

CELLS: list[Cell] = [
    Cell("00_baseline", "production baseline (control)"),
    Cell("R1_drop_all_4", "drop day_return + stop_pressure + realized_loss + rank_max (LEAD)", dict(DROP_ALL_4)),
    Cell("R1_drop_day_return", "drop day-return floor", dict(DROP_DAY_RETURN)),
    Cell("R1_drop_stop_pressure", "drop stop-pressure veto", dict(DROP_STOP_PRESSURE)),
    Cell("R1_drop_both_noops", "drop day_return + stop_pressure", {**DROP_DAY_RETURN, **DROP_STOP_PRESSURE}),
    Cell("R1_retest_rank_max", "drop universe-rank upper bound", dict(DROP_RANK_MAX)),
    Cell("R1_retest_realized_loss", "drop realized-loss-pressure veto", dict(DROP_REALIZED_LOSS)),
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
