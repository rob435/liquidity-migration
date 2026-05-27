"""Phase 1 — universe-isolation diagnostic dispatcher.

Pre-reg: docs/preregistration/2026-05-27-phase1-universe-isolation-diagnostic.md

12 cells × 1 venue (Bybit) = 12 runs. 6 configs × 2 universes (474 archive-
only side-copy vs 764 full root). The 474 side-copy was built by
scripts/build_legacy_archive_manifest.py on 2026-05-27. Window 2025-01-01
→ 2026-05-28 (matches in-flight EXPLORATORY sweep).

DESCRIPTIVE ONLY: no candidate / promotion decision from Phase 1. The 474
cells are biased_benchmark by construction (archive coverage applied
retroactively). See the pre-reg for the a-priori interpretation rule.

Dispatch:

    SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \\
      .venv/Scripts/python.exe scripts/phase1_universe_diag_sweep.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, Cell, run_sweep  # noqa: E402

SWEEP_TAG = "phase1_universe_diag_2026-05-27"
START_DATE = "2025-01-01"
END_DATE = "2026-05-28"

ROOT_764 = SHARED / "bybit_full_pit"
ROOT_474 = SHARED / "bybit_full_pit_archive_only"

# Single "venue" — Bybit. The per-cell data_root_override routes each cell
# to the right root (474 side-copy or 764 full).
VENUES = {"bybit": ROOT_764}

# Production baseline = current promoted profile. Matches Appendix A of
# the parent plan and the volume_events_cell.sh wrapper baseline.
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


# 6 configs × 2 universes. Each cell carries its own data_root_override so
# the run_sweep loop routes 474 cells to the archive-only side-copy and
# 764 cells to the full root.
def _cell(cell_id: str, description: str, root: Path, **overrides: str) -> Cell:
    return Cell(
        cell_id=cell_id,
        description=description,
        overrides=dict(overrides),
        data_root_override=root,
    )


CELLS: list[Cell] = [
    # baseline pair
    _cell("P1_baseline_474",   "production defaults, 474 archive-only universe", ROOT_474),
    _cell("P1_baseline_764",   "production defaults, 764 full universe (control)", ROOT_764),
    # turnover-floor pair
    _cell("P1_turn10M_474",    "474 + min turnover $10M", ROOT_474,
          **{"--universe-min-daily-turnover": "10000000"}),
    _cell("P1_turn10M_764",    "764 + min turnover $10M", ROOT_764,
          **{"--universe-min-daily-turnover": "10000000"}),
    # rank-max pair
    _cell("P1_rankmax200_474", "474 + universe_rank_max 200", ROOT_474,
          **{"--universe-rank-max": "200"}),
    _cell("P1_rankmax200_764", "764 + universe_rank_max 200", ROOT_764,
          **{"--universe-rank-max": "200"}),
    # rank-improvement-min pair
    _cell("P1_rankimp200_474", "474 + rank_improvement_min 200", ROOT_474,
          **{"--liquidity-migration-rank-improvement-min": "200"}),
    _cell("P1_rankimp200_764", "764 + rank_improvement_min 200", ROOT_764,
          **{"--liquidity-migration-rank-improvement-min": "200"}),
    # hold-2 pair
    _cell("P1_hold2_474",      "474 + hold_days 2", ROOT_474,
          **{"--hold-days": "2"}),
    _cell("P1_hold2_764",      "764 + hold_days 2", ROOT_764,
          **{"--hold-days": "2"}),
    # combo pair
    _cell("P1_combo_474",      "474 + turn10M + hold=2 + rankimp200", ROOT_474,
          **{"--universe-min-daily-turnover": "10000000", "--hold-days": "2",
             "--liquidity-migration-rank-improvement-min": "200"}),
    _cell("P1_combo_764",      "764 + turn10M + hold=2 + rankimp200", ROOT_764,
          **{"--universe-min-daily-turnover": "10000000", "--hold-days": "2",
             "--liquidity-migration-rank-improvement-min": "200"}),
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
