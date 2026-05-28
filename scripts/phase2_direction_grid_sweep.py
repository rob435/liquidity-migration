"""Phase 2 — rank-direction full grid dispatcher.

Pre-reg: docs/preregistration/round1/phase2-rank-direction-grid.md

3 directions {improvement, deterioration, both} x 11 thresholds
{25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400}
= 33 cells x 2 venues = 66 runs.
Window 2023-04-01 -> 2026-04-30 (cross-venue minimum).

Control: P2_imp_150 (= current production profile bit-for-bit, by virtue
of --liquidity-migration-rank-direction defaulting to "improvement" and
--liquidity-migration-rank-improvement-min defaulting to 150).

Strictness Manifesto decision rule + FDR ceiling apply (see pre-reg).

Dispatch:

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/Scripts/python.exe scripts/phase2_direction_grid_sweep.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "phase2_direction_grid_2026-05-27"
START_DATE = "2023-04-01"
END_DATE = "2026-04-30"

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}

# Production baseline = current promoted profile. Matches Appendix A of
# the parent plan. Default rank-direction=improvement, threshold=150.
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
    # rank-improvement-min and rank-direction are SET PER CELL below
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

DIRECTIONS = ("improvement", "deterioration", "both")
DIRECTION_TAG = {"improvement": "imp", "deterioration": "det", "both": "both"}
THRESHOLDS = (25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400)


def _cells() -> list[Cell]:
    out: list[Cell] = []
    for direction in DIRECTIONS:
        for t in THRESHOLDS:
            tag = DIRECTION_TAG[direction]
            cell_id = f"P2_{tag}_{t}"
            description = f"direction={direction} threshold={t}"
            out.append(Cell(
                cell_id=cell_id,
                description=description,
                overrides={
                    "--liquidity-migration-rank-direction": direction,
                    "--liquidity-migration-rank-improvement-min": str(t),
                },
            ))
    return out


CELLS: list[Cell] = _cells()


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
