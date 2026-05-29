"""R9 best-stack daily test — drop_all_4 + risk_equal 2% + loss-cutting EXIT variants, hardened.

The LAST untested daily lever before the do-nothing verdict. Under the honest engine
(bar_extreme stops + 100% taker + calendar returns): R1 (drop_all_4) FALSIFIED, R2/R9 IC
ANTI-selects, R5 risk_equal sizing got binance only to near-breakeven (ret -0.035, still
negative). This tests whether a loss-cutting EXIT (R13 failed-fade / tighter stop) ON TOP
of the best sizing (risk_equal 2%) finally crosses zero on binance — the strongest
combined stack in the pre-registered daily space.

baseline = R13/R1 drop_all_4 entries + risk_equal 2% sizing; cells vary ONLY the exit:
  00_baseline_drop4 (promoted exit; reproduces R5_risk_equal_2pct as a sanity check),
  R13_ff6_3pct, R13_ff6_4pct (failed-fade: cut failed fades at 6h / 3-4% loss),
  R13_stop10 (tighter fixed stop 0.10).
The tp21/tp30/rank-exit variants are intentionally NOT run — they tune winners/timing,
not the loss problem; only loss-cutting exits can flip a near-breakeven negative. If NONE
clears Tier-2 (positive return BOTH venues) => every pre-registered daily lever (entries,
IC, sizing, exits) is exhausted => daily Architecture A is a documented null under honest
methodology => do nothing.

Hardened defaults inherited from the engine/config; cost_multiplier=3 (honest 15bps recost
applied post-hoc via cost_model.recost_trades). Distinct tag. Dispatch (23 GB/cell → workers=1):
    $env:SWEEP_MAX_WORKERS=1; $env:POLARS_MAX_THREADS=8; \
      .venv\\Scripts\\python.exe -u scripts\\r9_exit_sizing_hardened_sweep.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("SWEEP_CELL_GB", "23")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, run_sweep  # noqa: E402
from r13_exit_rule_sweep import (  # noqa: E402
    BASELINE_PARAMS as R13_BASE,
    CELLS as R13_CELLS,
    END_DATE,
    START_DATE,
    VENUES,
)

SWEEP_TAG = "r9_exit_sizing_hardened_2026-05-29"
# Best sizing from the R5 hardened re-baseline (risk_equal 2% = best binance MAR / loss).
BASELINE_PARAMS = {**R13_BASE, "--position-weighting": "risk_equal", "--target-vol-per-name": "0.02"}
_KEEP = {"00_baseline_drop4", "R13_ff6_3pct", "R13_ff6_4pct", "R13_stop10"}
CELLS = [c for c in R13_CELLS if c.cell_id in _KEEP]


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
