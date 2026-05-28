# Phase 6 — combined-signal portfolio (pre-registration)

**Date:** 2026-05-27
**Stage:** pre-registered; **CONDITIONAL** on ≥3 features surviving Phase 5b
(Phase 5 IC test).
**Parent plan:** [2026-05-27 multi-phase research plan](rank-direction-edge-and-universe-isolation-research-plan.md)
**Phase label per parent plan Appendix B:** `candidate` (cells satisfying
Manifesto criteria become candidates for Phase 7 OOS, subject to FDR ceiling).

## Trigger condition

Phase 6 runs iff Phase 5b yields **≥3 features that survive ALL of:**

- `|mean_ic| ≥ 0.03` on both venues
- sub-period sign-consistent across all 3 sub-periods on both venues
- `|t-stat| ≥ 3` on both venues

The surviving feature list is the input to this phase's cells. If <3
features survive, Phase 6 does NOT run; the signal-research arm
concludes "H7 (combined-signal portfolio beats event-driven) not
testable; H6 not strong enough to feed a combination" and the
single-event strategy stands.

The feature list AND the per-feature IC weights (for the ic_weighted
cell) are pinned at Phase 5b completion and committed in a Phase 6
dispatch-time addendum. NO discretion at dispatch — the survivor list
is mechanically read from the Phase 5b decision rule.

## Purpose

Test H7 from the parent plan: *a combined-signal portfolio from H6
survivors beats the current event-driven strategy*.

Three combination schemes, two calibration sweeps. The control is the
current event-driven baseline (matching the Phase 0 `00_baseline` cell
on the same window, not a Phase 6-specific control).

## Window

**2021-01-01 → 2026-04-30** (matches the Phase 5 panel build window).
Cross-venue minimum; both venues observe the same period.

## Cells (3 + 9 + 9 = 21 cells × 2 venues = 42 runs)

### Core 3 combination schemes

| Cell | Combination | Sizing |
|---|---|---|
| `P6_equal_z`        | sum of per-survivor cross-sectional Z-scores | 1/realized-vol per name |
| `P6_ic_weighted`    | sum_i (IC_i × Z_i)                            | 1/realized-vol per name |
| `P6_top_decile_short` | top-decile short selection by combined Z      | 1/realized-vol per name |

(`top_decile_short` uses `--top-decile 0.10`. `vol_target_per_name=0.01`.)

### Calibration sweeps (informational; same survival rule applies)

`P6_horizon_sweep`: each of the 3 core cells × {1d, 3d, 7d} forward
horizons = **9 cells**.

`P6_decile_sweep`: each of the 3 core cells × {5%, 10%, 20%} top-decile
threshold = **9 cells**.

All cells run on both venues.

## Decision rule (Strictness Manifesto)

Standard candidate criteria (ALL of):

- Sharpe-like Δ ≥ **+0.5** vs the current event-driven baseline on
  **both** venues, AND
- max-DD Δ ≤ **-5pp** vs baseline on **both** venues, AND
- sign-consistent direction of edge across **3** sub-period thirds, AND
- per-sub-period trade count ≥ **50** on Bybit (≥30 on Binance), AND
- total-return sign positive on both venues across the full window.

Falsifiers:
- Sharpe Δ < -0.5 on either venue
- Sign flip between venues or sub-periods
- Any sub-period DD > 60%

### FDR ceiling

Max **3 candidates** from Phase 6 may forward to Phase 7. If more than 3
cells qualify, the top-3 by combined-venue Sharpe forward; the rest are
CLOSED-REJECTED. Phase 6's FDR ceiling is SEPARATE from Phase 2-4's (so
worst-case Phase 7 has 3 from Phases 2-4 + 3 from Phase 6 = 6 finalists).

## Estimated cost

42 cells × ~10 min/cell × parallel-4 ≈ **~105 min wall** on the 5950X.

(Faster than the parent plan's 60-min estimate would be if combined-
portfolio compute is light vs feature-panel reads. The panel is pre-built
and cached; each Phase 6 cell reads from the panel parquet, runs the
combination + sizing, and produces a synthetic equity curve. The
volume-events-style fill simulation may add overhead but the per-day
position math is dominated by polars-native ops.)

## Dispatch (to be scripted as `scripts/phase6_combined_portfolio_sweep.py`
once Phase 5b survivors are known)

The orchestrator template will be drafted at dispatch time, parameterized
on the surviving-feature list. Until then, the dispatch shape is:

```bash
SWEEP_MAX_WORKERS=4 POLARS_MAX_THREADS=4 \
  .venv/Scripts/python.exe -u scripts/phase6_combined_portfolio_sweep.py \
    --survivors "f1,f2,f3,..." \
    --ic-weights "f1=0.04,f2=-0.05,f3=0.03,..."
```

The orchestrator wraps `signal-harness combined-portfolio` per cell and
produces per-cell volume-event-style metrics for the decision rule.

## Pre-commitments

1. **No threshold loosening** — the Manifesto's +0.5/-5pp bars apply.
2. **No off-menu cells** — 21 cell configurations are committed; adding
   a cell mid-phase requires a plan amendment.
3. **No survivor-set re-derivation** — once Phase 5b's survivors are
   pinned, Phase 6 uses that exact set. No "we also tried adding
   feature X" mid-run.
4. **Combined portfolios that fail Manifesto are FILED, not promoted.**
   Even a Phase 6 cell with strong-looking numbers must clear the
   conjunctive rule on both venues.

## Forward pointer

- Phase 6 candidates → Phase 7 OOS (mandatory gate, max 3 forward per
  the FDR ceiling).
- 0 Phase 6 candidates → the signal-research arm concludes
  "combined-signal portfolio does not beat the event-driven strategy"
  and the strategy stays as-is.

## Run-label

`exploratory` until any cell passes Phase 7 → `candidate` until forward
demo → `paper_ready` after ≥30-day demo reconciliation. No cell jumps
ladder rungs.
