# Pre-registration: R1 — walk-forward causal-allocator falsifier (weight-overfit attack)

**Date:** 2026-06-09 (registered BEFORE the run)
**Run label:** `exploratory` (inference diagnostic; outputs a selection-haircut number and
a weight policy for the deployable — not itself promotion evidence)
**Program:** `docs/research_plan_continuous_live_readiness_2026-06-09.md` R1.

## Question

The winner weights `{turn3p3:.30, turn4p3:.20, turn4p5:.40, age210tp14:.10}` were chosen
on the full window. The simplex is a plateau (robustness receipt), but a chooser still
chose in-sample. How much OOS performance would a CAUSAL chooser have delivered, and how
big is the in-sample selection haircut?

## Protocol (fixed a-priori)

- Components: the 4 winner sources (scout registry), both venues. Rebalance rule fixed
  at the deployable anchor `w90/tv0.045/max4/ddh-0.04/momentum0/resize10bps`. Weight
  space: the same 0.1-step 4-simplex (286 vectors).
- Precompute each vector's full rebalanced daily series once per venue (the engine loop
  is day-causal, so trailing-window slices of the full series are exact for selection).
- Walk-forward: estimation dates quarterly from 2024-04-01 through 2026-04-01 (first 12
  months = warmup). At each date T the chooser sees only days < T (expanding window,
  primary) and picks one vector; its days in [T, next T) are the OOS evaluation. A
  weight switch is charged a one-off penalty on the first OOS day:
  `L1(w_new - w_old) x scale(first day) x 10bps`.
- Choosers (fixed menu, no post-hoc additions):
  A (primary): max mean-venue Sharpe on trailing window;
  B: max min-venue MAR on trailing window;
  C: equal-weight 0.25x4 (no chooser — the floor);
  REF: the fixed winner vector evaluated on the SAME OOS days (the in-sample-chosen object).
- Metrics on the stitched OOS window (2024-04-01..ledger end), full-calendar convention
  as in the hedge receipts: return, MAR, Sharpe, per venue + pooled (mean of venues).
- Sensitivities (reported): rolling-12m estimation window; semiannual cadence; the
  per-quarter chosen-weight path (does the chooser wander or sit near the winner?).

## Causality declaration

Selection at T uses only data < T. Approximation (declared): each vector's OOS slice
carries its own full-history vol/DD state rather than a hybrid switch-state — selection-
causal, state-approximate; the switch penalty bounds the unmodeled resize.

## A-priori reading (haircut = 1 − causal-A pooled OOS Sharpe / REF pooled OOS Sharpe)

- haircut ≤ 15% AND causal-A positive return both venues → **weight-overfit concern
  DEAD**; freeze the receipt weights for deployment (re-estimation adds nothing).
- 15% < haircut ≤ 40% → **modest selection flattery**; deployable keeps frozen weights
  but forward expectations are set from causal-A numbers.
- haircut > 40% OR causal-A loses money on a venue while REF does not → **material
  weight overfitting**; the deployable becomes the causal allocator itself and all
  prior winner-level expectations are voided.
- Bonus reading: if C (equal-weight) is within 10% of REF pooled OOS Sharpe, weights
  barely matter (plateau confirmed causally) — strongest anti-overfit answer.

## Artifacts

`~/SHARED_DATA/continuous_walkforward_allocator_2026-06-09/` — report JSON + per-quarter
choices CSV. Driver: `scripts/continuous_walkforward_allocator.py`.

## Verdict (filled in after the run, same day)

**Weight-overfit concern DEAD (pre-registered reading #1), with the bonus reading
triggered: weights are not load-bearing.** Stitched OOS (2024-04-01..ledger end),
pooled Sharpe:

| object | pooled OOS Sharpe | bybit | binance | both ret>0 |
|---|---|---|---|---|
| REF fixed winner (in-sample ceiling) | 2.317 | 2.733 / MAR 5.54 | 1.902 / MAR 4.25 | yes |
| causal A (expanding, mean-Sharpe) | 1.998 | — | — | yes |
| causal B (rolling-12m, min-MAR) | 2.277 | 2.686 / 5.52 | 1.868 / 3.51 | yes |
| **C equal-weight (no chooser)** | **2.334** | 2.874 / 6.43 | 1.794 / 3.84 | yes |

- Haircut (causal-A vs REF) = **13.8%** ≤ 15% bar, causal-A positive both venues →
  concern dead; freeze the receipt weights.
- **Equal-weight matches/beats the winner OOS** (2.334 vs 2.317) → plateau confirmed
  causally; the edge is in the COMPONENTS, not the mix. (We do NOT switch the deployable
  to equal weights — that would be a fresh post-hoc choice; the point is any reasonable
  fixed mix works.)
- The adaptive chooser actively hurt: quarterly choices wandered (e.g. 0.9/0/0.1/0 in
  early 2025) chasing trailing performance and paid L1-switch costs converging back.
  **Do NOT build weight re-estimation into the live system.** Min-MAR criterion (B,
  rolling) was the most stable chooser and still ≤ REF.

Honest framing note: REF's weights saw these "OOS" days when originally chosen, so REF
is the in-sample ceiling on this window, exactly as registered — the haircut measures
what an honest chooser loses vs that ceiling, and the answer is "little, and a
no-chooser loses nothing."

**Live weight policy (binding for the deployable):** frozen receipt weights
`{turn3p3:.30, turn4p3:.20, turn4p5:.40, age210tp14:.10}`; no adaptive re-weighting.

Artifacts: `~/SHARED_DATA/continuous_walkforward_allocator_2026-06-09/report.json`
(includes per-quarter choice path). Driver: `scripts/continuous_walkforward_allocator.py`.
