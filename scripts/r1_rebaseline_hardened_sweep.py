"""R1 re-baseline under the `9f52819` + `b1a3368` hardened engine defaults (Round 2).

Executes the re-baseline rule pre-registered in
``docs/preregistration/round2/r-audit-methodology-hardening.md`` ("treat the first sweep
under the applied defaults as a re-baseline: re-run the control under identical settings
before any cell-vs-control MAR delta is read as Tier-1/Tier-2 evidence"). The original
R1 audit (tag ``r1_filter_audit_max12_2026-05-28``) ran on the PRE-hardening engine.

What changed (engine defaults, already on main — NOT set here):
  - 100%-taker base: ``maker_fill_probability=0`` in configs/volume_alpha.default.yaml
    (M2) → ``base_entry_exit_cost_bps`` = 15 bps.
  - ``stop_fill_mode=bar_extreme`` (H3): stops fill at the bar's adverse extreme.
  - calendar-exact forward/daily returns (M4/B3); DD+Sharpe promotion gate (M1).

What is UNCHANGED from the original R1: baseline params, the cells, window, and
``--cost-multipliers 3``. Keeping cost_multiplier=3 ISOLATES the hardening effect (no new
parameter to pre-register); under the honest 15 bps base that is a conservative 45 bps
round-trip. The honest 15 bps-taker recost is applied post-hoc via
``cost_model.recost_trades`` (R6) for the model-vs-legacy side-by-side in the verdict.

SCOPE: the decision-critical subset only — ``00_baseline`` (control) + ``R1_drop_all_4``
(the demo-eligible lead). These two answer the re-baseline question (does the lead's
pooled-MAR-Δ edge survive honest costs?) and set R9's ``R9_event_only`` baseline. The 5
decomposition cells (which filter carries the effect) were settled pre-hardening and the
hardening shifts levels ~uniformly across cells, so they are not re-run here.

Distinct tag — does NOT overwrite the pre-hardening artifacts.

Dispatch (5950X, Windows; 23 GB/cell on a 32 GB box → workers=1, see memory):
    $env:SWEEP_MAX_WORKERS=1; $env:POLARS_MAX_THREADS=8; \
      .venv\\Scripts\\python.exe -u scripts\\r1_rebaseline_hardened_sweep.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sweep_runtime import SHARED, run_sweep  # noqa: E402
from r1_filter_audit_sweep import (  # noqa: E402
    BASELINE_PARAMS,
    CELLS,
    END_DATE,
    START_DATE,
    VENUES,
)

SWEEP_TAG = "r1_rebaseline_hardened_2026-05-29"
_KEEP = {"00_baseline", "R1_drop_all_4"}
REBASELINE_CELLS = [c for c in CELLS if c.cell_id in _KEEP]


def main() -> int:
    return run_sweep(
        REBASELINE_CELLS,
        VENUES,
        baseline_params=BASELINE_PARAMS,
        start_date=START_DATE,
        end_date=END_DATE,
        sweep_tag=SWEEP_TAG,
        summary_path=SHARED / f"{SWEEP_TAG}_summary.csv",
    )


if __name__ == "__main__":
    sys.exit(main())
