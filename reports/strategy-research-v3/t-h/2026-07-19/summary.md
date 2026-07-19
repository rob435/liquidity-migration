# T-H — Expected-net ranker (exploratory, Lane 1)

**Status: EXPLORATORY.** Walk-forward model development inside the spent V2
window. No alpha, robustness, candidate, or promotion claim. Feature set,
model family, actions, and comparators exactly as declared in
`docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md`; the free
parameters the draft left open were frozen before any fit was inspected
(ridge λ=1.0 on train-standardized winsorized features, logistic IRLS with the
same L2, training set = trades whose **exit** precedes the refit boundary,
decile/quintile cutoffs from the train-window score distribution, sizing map
0.25/0.75/1.0/1.25/1.5, unscored pre-first-refit trades pass through).

## What ran

- 9 frozen features (freshness, momentum shape, funding state incl. EWMA hl3,
  settlement cadence, modeled cost, signal score, censored symbol age);
  rank-transformed net-per-unit target; 10 quarterly expanding refits
  2022-07-01 → 2024-10-01; 15,212 of 16,745 trades scored strictly
  out-of-fit-window.
- Actions and comparators all evaluated on the same scored sample
  (baseline −31.60% net there): drop bottom decile; quintile sizing; T-E best
  declared cell (skip_h1); T-G best declared cell (combo_K−0.001); T-E ∧ T-G.
- Double-verification arm: final-refit model scores both T-A render books
  (ranking-transfer diagnostic only; the age feature is censored at a
  different origin there, and scoring pre-2024 render entries with the final
  model is not a tradeable rule).

## Results

**The ML actions lose to the simple conditioners they partly encode, by an
order of magnitude:**

| Action | Net Δ full | Net Δ early | Net Δ late | MaxDD |
|---|---:|---:|---:|---:|
| drop_bottom_decile | +4.72pp | +0.92pp | +3.79pp | −35.4% |
| quintile_sizing | +2.61pp | +1.62pp | +0.99pp | **−40.1%** (worse than baseline) |
| T-E skip_h1 | +39.09pp | +9.37pp | +29.73pp | −6.9% |
| **T-E ∧ T-G** | **+41.09pp** | +9.10pp | +32.00pp | **−5.7%** |

The declared survival test — beat T-E ∧ T-G — fails decisively. Dropping the
bottom decile does not even improve the surviving book's per-trade quality
(−0.208 bps/trade before and after): it only shrinks a negative-mean book.

**Declared refutation criteria also trip:**

- Coefficient stability: 4 of 9 ridge features flip sign across the 10 refits
  (`r_1h` 3×, `known_rate_prev` 2×, `funding_ewma_hl3` 2×,
  `hours_since_high_168h` 1× — the model gave freshness a *positive* stale
  weight through 2023-01, chasing the 2022 regime where stale entries were
  profitable, then reversed). Under the draft's rule — "a sign-flipping model
  is a refutation regardless of net" — this refutes the thesis on its own.
- Decile monotonicity: absent in the middle (D7 is the *worst* decile,
  −75 bps/unit full era); only D1 (bad) and D9–D10 (good) separate, and D10
  decays to ≈0 in the late era. Win rate is the only cleanly monotone
  statistic (0.512 → 0.623).

**Render transfer (fixed for cross-component trade_id duplication; counts
verified 2,300/4,019):** the one transferring signal is bottom-decile
toxicity — D1 is strongly negative in BOTH books (gate-on −471, gate-off
−564 bps/unit) — but on 19+32 trades only. The remaining deciles carry no
order, and the ledger-fit cutoffs push most render mass into D10 (distribution
shift). Not actionable.

## Read

**Refuted; no prototype.** A shallow linear ranker over these features cannot
beat the two hand-built cuts it partly encodes, its coefficients are not
sign-stable across refits, and its ranking does not transfer to the deployed
shape beyond a tiny extreme-bottom tail. Consistent with T-E/T-G: the
per-trade expected-net structure of the barebones surface is dominated by a
few coarse, era-unstable margins, not by a learnable smooth ranking.

## Limitations

- Spent discovery surface; walk-forward inside an already-inspected window.
- Linear models only (declared); no interactions beyond what features encode.
- Scored-sample baseline (−31.60%) differs from the full ledger (−20.23%)
  because the profitable pre-2022-07 stretch is unscored by construction.
- Render arm: distribution shift (age censoring origin, score ranges) makes
  decile assignment there indicative only.

## Next action

No prototype. If ranking is ever revisited it needs render-native features
and labels, not this surface.

Artifacts: `th_grid.csv`, `th_decile_diagnostic.csv`, `th_coefficients.csv`,
`th_render_deciles.csv`, `th_trade_panel.parquet` (local; hash in
`manifest.json`), manifest with the frozen spec.
