# Phase 2 — rank-direction full grid (pre-registration)

**Date:** 2026-05-27
**Stage:** pre-registered, not yet run.
**Parent plan:** [2026-05-27 multi-phase research plan](rank-direction-edge-and-universe-isolation-research-plan.md)
**Phase label per plan Appendix B:** `candidate` (cells qualifying under
the Strictness Manifesto become candidates for Phase 7 OOS, subject to
the FDR ceiling).

## Purpose

Empirically test H2 (rank-deterioration as a tradable short edge) and
H3 (two-sided rank-dislocation as a single event) from the parent plan.

H2 mechanism: rapid liquidity-rank deterioration = capital leaving =
loss of speculator interest = continuation lower. Distinct from the
rank-improvement-fade thesis (mean-reversion against fresh crowding) —
H2 is a *continuation* story on the same coordinate system.

H3 mechanism: if both improvement-fade and deterioration-continuation
carry edge, a single event firing on `|rank_improvement| >= X` might
capture both with one set of plumbing.

## Window

**2023-04-01 → 2026-04-30** (cross-venue minimum, matches Phase 0).

3 years 1 month of clean PIT data on both venues. Three non-overlapping
sub-periods of ~13 months each for the cross-sub-period sign-consistency
check the Strictness Manifesto requires.

## Cells (33 × 2 venues = 66 runs)

3 directions × 11 thresholds = 33 cells per venue.

| Direction | Thresholds |
|---|---|
| `improvement` (current production) | 25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400 |
| `deterioration` (the H2 test)       | 25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400 |
| `both` (the H3 test)                 | 25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400 |

Cell IDs follow `P2_{imp|det|both}_{T}` (e.g. `P2_det_150`, `P2_both_25`).
**`P2_imp_150` is the control** — it equals the current production
profile bit-for-bit (the rank-direction-flag default).

All other parameters at production defaults. No exit changes, no
universe changes. The variable axes are direction × threshold only.

## Decision rule (Strictness Manifesto, no loosening)

Per-cell candidate criteria (ALL of):

- sharpe-like Δ ≥ **+0.5** vs control on **both** venues, AND
- max-DD Δ ≤ **-5pp** vs control on **both** venues, AND
- sign-consistent direction of edge across **3** non-overlapping
  sub-period thirds of the in-sample window, AND
- per-sub-period trade count ≥ **50** on Bybit (≥30 on Binance), AND
- total-return sign positive on both venues across the full window.

Per-cell falsifier (ANY of):
- sharpe-like Δ < -0.5 on either venue, OR
- sign flip between venues OR between sub-periods, OR
- any sub-period DD > 60%.

Inconclusive cells (not candidate, not falsifier) are FILED and not
pursued. No "well it's very close" reasoning.

### FDR ceiling

Max **3 candidates** from Phases 2/3/4 combined may forward to Phase 7.
If more than 3 cells satisfy candidate criteria, `apply_decision_rule.py`
sorts by combined-venue mean Sharpe (pre-committed tie-break) and
selects the top 3; the rest are CLOSED-REJECTED (not a "menu for later").

## Estimated cost

66 cells × ~10 min/run (3-year window) × parallel-8 ≈ **~85 min wall**
on the 5950X.

## Dispatch

```bash
SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \
  .venv/Scripts/python.exe scripts/phase2_direction_grid_sweep.py
```

> _Historical: `scripts/phase2_direction_grid_sweep.py` was removed in the 2026-05-28
> cleanup. Phase 2 is COMPLETE (REJECTED, 0 candidates — see `phase2-verdict.md`); not for re-running._

Reports under
`~/SHARED_DATA/{bybit,binance}_full_pit/reports/phase2_direction_grid_2026-05-27/<cell>/`.
Aggregate summary at
`~/SHARED_DATA/phase2_direction_grid_2026-05-27_summary.csv`.

## Post-dispatch analysis

```bash
python scripts/apply_decision_rule.py \
  ~/SHARED_DATA/phase2_direction_grid_2026-05-27_summary.csv \
  --control P2_imp_150
```

The control is `P2_imp_150`, not `00_baseline` — Phase 2's universe
construction means the control is the production-default-direction
cell at the production-default threshold, which equals the existing
production profile.

## Pre-commitments

1. **No threshold loosening.** If a cell falls 0.01 short of the +0.5
   Sharpe Δ or 0.4pp short of the -5pp DD bar on either venue, it is
   inconclusive, not a candidate. Inconclusive cells are filed and not
   pursued.
2. **No off-menu thresholds.** Threshold grid is committed to
   {25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400}. Adding a
   threshold post-hoc requires a plan amendment + new dated pre-reg.
3. **FDR ceiling is binding.** If 5 cells pass candidate criteria,
   the top 3 by combined-venue Sharpe go to Phase 7; the bottom 2 are
   closed-rejected. They are NOT a "menu for later" and will NOT be
   resurrected as alternative Phase 3 starting points.
4. **Phase 1 outcome modulates interpretation, not selection.** If
   Phase 1 confirmed universe-widening dominates the DD shift, any
   Phase 2 candidate's in-sample Δ is downweighted — but the candidate
   criteria themselves are unchanged. Phase 7 OOS is the only place
   the candidate is validated or killed.

## Forward pointers

- ≥1 candidate in direction-deterioration or direction-both →
  triggers **Phase 3** (exit selection) for the winning candidate
  cell.
- ≥1 candidate overall → triggers **Phase 7 OOS** at session end
  (mandatory regardless of which phase produced the finalist).
- 0 candidates → H2 + H3 are FALSIFIED for this in-sample window.
  Phase 3 and 4 do NOT run. Phase 5/6 still run (they test H6/H7
  independently). Phase 7 still runs against any Phase 6 candidates.
- All-falsified → the strategy's existing rank-improvement-only
  direction stays as-is and the program proceeds to the signal-harness
  arm only.
