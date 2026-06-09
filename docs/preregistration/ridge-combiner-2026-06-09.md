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

**Run commit:** `5e1c960` (this scaffold commit; no code changed for the run).
**Run date:** 2026-06-09. **Machine:** Windows research box (5950X), full-PIT roots at `C:\Users\user\SHARED_DATA`.

**Window adjustment vs the locked command:** klines coverage ends *before* the
pre-registered `--end 2026-05-28` on both roots — bybit `klines_1h` → 2026-05-26 23:00,
binance `klines_1h` → 2026-04-30 23:00. The single `--end` was therefore capped at
**2026-05-27** (end-exclusive; includes all bybit data, not past it). binance's effective
window truncates at its own 2026-04-30 coverage. `--cost-mults` from the locked command is
not a parameter of `ridge_combiner_scout.py` and was omitted (the scout is a rank-IC /
sign-stability falsifier, not a PnL/cost run); all other frozen params used as registered
(λ grid 0.1,1,10,100,1000; warmup 540d; step 90d; embargo 2d; min-train 200; universe
turnover floor 1e6).

**Headline artifacts (frozen 8 features, both venues):**
`~/SHARED_DATA/ridge_combiner_2026-06-09/{summary.csv, fold_coefficients.csv, run_receipt.json, bybit_ridge_score.parquet, binance_ridge_score.parquet}` — `config_hash c17e9ee4f4fc`.

- **bybit:** n_scored=163,854; folds=7; pooled OOF rank-IC = **−0.0404**; mean fold rank-IC
  = −0.0476. Per-fold rank-IC is negative in **all 7** folds
  (−0.006, −0.136, −0.034, −0.074, −0.012, −0.031, −0.041). Sign-stable features 5/8
  (realized_vol_7d, funding_rate_z, liquidity_rank, oi_to_adv all 1.0; dist_from_30d_high
  0.857; below the 0.80 bar: turnover_delta_7d 0.714, premium_index_z 0.714,
  xs_rank_ret_30d 0.571).
- **binance:** n_scored=**0**; folds=**0**; pooled rank-IC undefined (written as 0.0).
  **Cause (diagnosed, not assumed):** `oi_to_adv` is **100% null** in the binance_full_pit
  feature panel (open-interest not wired into that feature on binance), so
  `walk_forward_scores`' `drop_nulls(subset=all 8 features)` drops every binance row → 0
  train rows → 0 folds. Diagnostic on a binance 2024-06-01→2025-01-01 panel (61,731 rows):
  per-feature null fractions — oi_to_adv 100.0%, funding_rate_z 15.6%, xs_rank_ret_30d 4.2%,
  all others <1%; rows surviving `drop_nulls(all 8 + fwd_ret_1d)` = **0**. This is a
  structural data-wiring gap, NOT a real "0 IC".

**Optional 9th feature (residual_momentum), bybit-only, reported separately (not headline):**
`~/SHARED_DATA/ridge_combiner_2026-06-09_rmom9/` (`config_hash f4e2b7c45fb2`). bybit
n_scored=162,409; folds=7; pooled OOF rank-IC = **−0.0305** (mean fold −0.0253; 4/9
sign-stable). Adding residual_momentum does **not** flip bybit positive — still wrong-signed.
binance was not re-run (same `oi_to_adv` 100%-null gate would yield 0 folds; the 9th feature
cannot un-null the other 8).

## Verdict

**REJECTED at Tier-1** — cheap falsification, **no engine change** (the ridge is *not* wired
into the sizing path). The a-priori Tier-1 gate is positive pooled OOF rank-IC on **both**
venues + dominant features sign-stable across ≥80% of folds. It fails on two independent
grounds, either sufficient: **(1)** bybit's pooled OOF rank-IC is −0.0404 and negative in all
7 folds — the combiner is weakly *anti*-predictive out-of-fold, the opposite of the required
signal; stable coefficients (5/8 ≥80%) pointing the wrong way out-of-fold are not signal.
**(2)** The frozen 8-feature set is not jointly computable on binance — `oi_to_adv` is 100%
null there, so the scout scored zero binance rows and the both-venues requirement is
structurally unmeetable as pre-registered. The residual_momentum 9th-feature variant (bybit
−0.0305) does not rescue it. The hand-weighted sizing stands; in-sample walk-forward never
promotes past Tier-2 and the forward demo/paper ledger remains the only Tier-3 arbiter.
**Follow-ups (non-gating, for any re-attempt):** revise the frozen set to drop/replace
`oi_to_adv` for cross-venue work *or* wire binance OI into the panel before re-registering;
bybit's persistent negative IC is independent evidence that a linear ridge over these
features carries no usable 1-day forward-return ranking signal *within* the event pool, so a
re-attempt should change the feature hypothesis, not just the venue plumbing.

**Independent same-day replication (parallel session):** a second, independently-executed run of the same pre-registered command reproduced the verdict — bybit pooled OOF rank-IC −0.0396 (vs −0.0404 here), binance unmeasurable for the same panel-null reason. Two independent executions, one conclusion.
