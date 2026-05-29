"""R9 sizing-rescue test = R5 re-baseline under the hardened engine (Round 2).

The R1 hardened re-baseline FALSIFIED drop_all_4 at Tier-2 (binance negative); the R9
IC-selectivity pre-check showed the composite IC ANTI-selects within events (high-IC
event names are the worst shorts). The one remaining daily lever that can plausibly flip
a negative return is R5 ``risk_equal`` sizing — it sizes inversely to realized vol, so it
DOWN-WEIGHTS exactly the high-vol names the pre-check flags as the losers.

This re-runs the R5 sizing cells (drop_all_4 entries + promoted exit; dollar-equal control
vs risk_equal at target vol 1.0/1.5/2.0%/name/day) UNCHANGED under the now-hardened engine
defaults (bar_extreme stops + 100% taker + calendar returns). It is BOTH the R5
re-baseline and R9's `event_only`-with-sizing test. If no risk_equal cell makes drop_all_4
clear Tier-2 (positive both venues, pooled MAR Δ > +0.1), sizing does not rescue the daily
strategy → with R1/R2/R3 also exhausted, the daily Architecture A is a documented null.

Distinct tag (does NOT overwrite the pre-hardening r5_position_sizing_2026-05-29). The
honest 15 bps recost (R6) is applied post-hoc; cost_multiplier left at 3 (isolates the
hardening). Dispatch (5950X, Windows; 23 GB/cell → workers=1):
    $env:SWEEP_MAX_WORKERS=1; $env:POLARS_MAX_THREADS=8; \
      .venv\\Scripts\\python.exe -u scripts\\r9_event_sizing_hardened_sweep.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("SWEEP_CELL_GB", "23")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, run_sweep  # noqa: E402
from r5_position_sizing_sweep import (  # noqa: E402
    BASELINE_PARAMS,
    CELLS,
    END_DATE,
    START_DATE,
    VENUES,
)

SWEEP_TAG = "r9_event_sizing_hardened_2026-05-29"


def main() -> int:
    return run_sweep(
        CELLS,
        VENUES,
        baseline_params=BASELINE_PARAMS,
        start_date=START_DATE,
        end_date=END_DATE,
        sweep_tag=SWEEP_TAG,
        summary_path=SHARED / f"{SWEEP_TAG}_summary.csv",
    )


if __name__ == "__main__":
    sys.exit(main())
