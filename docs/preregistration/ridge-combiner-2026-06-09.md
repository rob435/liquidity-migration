# Pre-registration: ridge combiner — sizing/ranking within the event-selected short pool

**Date:** 2026-06-09
**Author:** rob435
**Stage:** proposed

## What's changing
Add a walk-forward ridge combiner that maps a frozen set of 8 causal features to a
single predicted-forward-return score, used to **rank/size** names **within** the
event-selected short candidate pool. The event trigger (`turn4_pop4` + age +
liquidity) still defines *which* names enter; ridge only modulates *how much*.
No entry gate is replaced or removed.

## Hypothesis
The deployed system combines its features with hand-tuned hard gates and an
eyeballed ensemble weighting (e.g. the `0.30/0.20/0.30/0.20` continuous ensemble,
the per-venue scales). That hand-weighting is an implicit linear model fit by
sweep-until-it-looks-good — high researcher degrees of freedom, the exact axis this
program has been burned on (parameter mining; the retracted Round-2 null; the
continuous look-ahead). A heavily-L2-regularized ridge, refit causally on an
expanding window, achieves the same feature combination with **one** cross-validated
hyperparameter (λ) instead of dozens of swept thresholds. The expected win is
*fewer degrees of freedom for equal return*, not new alpha.

## Predicted direction + magnitude
- Pooled MAR Δ vs the current hand-weighted sizing: **[-0.3, +0.5]**, modal ≈ 0
  (statistically indistinguishable returns expected).
- Trade count Δ: **~0** — the candidate pool is gate-defined; ridge only sizes.
- Failure mode if hypothesis wrong: ridge underperforms the hand rule on
  out-of-fold walk-forward MAR, OR dominant-feature coefficient signs flip across
  folds (unstable fit). Either falsifies and the candidate is rejected.

## Roots that will be touched
- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper (NOT touched by this run; remains the Tier-3 arbiter)

## Frozen design (locked before the run)
- **Feature set (8, all from `signal_harness.FEATURE_REGISTRY`, all causal at
  decision_ts EOD):** `turnover_delta_7d`, `realized_vol_7d`, `funding_rate_z`,
  `premium_index_z`, `liquidity_rank`, `dist_from_30d_high`, `oi_to_adv`,
  `xs_rank_ret_30d`. (Optional 9th: `residual_momentum` left-joined if the
  precomputed parquet is present; reported separately, not part of the headline.)
- **Target:** `fwd_ret_1d` (matches the deployed 1-day decision horizon).
- **λ grid:** `[0.1, 1, 10, 100, 1000]`, selected by **inner** expanding-window CV
  on the *training fold only* (max mean out-of-fold rank-IC). λ is never tuned on
  any test fold.
- **Walk-forward:** expanding window, 540-day (18mo) warm-up before the first
  score, quarterly (90d) refit, embargo = `horizon + 1` days between train end and
  test start (training target must complete strictly before the test window opens).
- **Standardization:** train-fold mean/std only, applied to the test fold. No global
  z-scores.
- **Sign convention:** the module returns predicted forward return; downstream short
  sizing reads more-negative = stronger short. The module itself is sign-agnostic.

## Decision rule (a priori)
On the **concatenated out-of-fold** walk-forward scores only (never in-fold fit):
ACCEPT to demo-candidate **iff** pooled MAR Δ > **+0.1** vs the hand-weighted control,
**both** venues positive return, neither venue MAR Δ < **-0.5**, AND the dominant
features hold a consistent coefficient sign across **≥80%** of folds. Otherwise
REJECT. In-sample walk-forward never promotes past Tier-2; the forward demo/paper
ledger is the only Tier-3 OOS arbiter (`docs/data_roots.md`).

## Run command
```bash
# Operator data box (full-PIT roots ~23GB; not runnable on the 16GB dev mac):
POLARS_MAX_THREADS=8 .venv/bin/python scripts/ridge_combiner_scout.py \
  --data-roots "bybit=$HOME/SHARED_DATA/bybit_full_pit,binance=$HOME/SHARED_DATA/binance_full_pit" \
  --start 2023-04-01 --end 2026-05-28 \
  --cost-mults 1,2 \
  --out "$HOME/SHARED_DATA/ridge_combiner_2026-06-09"
```

## Post-run results
(fill in after run; include report paths + commit SHA at which the runs landed)

## Verdict
(accepted | rejected | inconclusive — with one-paragraph why)
