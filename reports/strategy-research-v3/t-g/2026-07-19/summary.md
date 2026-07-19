# T-G — Funding-state entry conditioner (exploratory, Lane 1)

**Status: EXPLORATORY.** Counterfactual post-processing of the spent V2 discovery
surface plus a diagnostic pass over the already-rendered T-A books. No alpha,
robustness, candidate, or promotion claim. Grid exactly as declared in
`docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md`; no cell added.

## What ran

- Feature: `known_rate_prev` = last settled funding rate at or before entry
  (strictly PIT). Bucket edges frozen after reproducing the draft's inspected
  table exactly (deep_neg `r < −0.1%` / neg `< 0` / zero `≤ +0.01%` / pos:
  1,305 / 2,303 / 10,190 / 2,947; nets −5.91 / −3.30 / −12.28 / +1.26 % capital)
  — hard-checked in code. 0 entries missing a prior settlement.
- Declared grid: skip if rate < K for K ∈ {−0.05%, −0.1%, −0.2%}/interval;
  half-weight variants; combination cells (skip iff rate < K AND the T-D
  meanrev φ0.5 forecast predicts the 24h funding sum < K × n_settlements).
  Fixed-capital recurrence, era split at 2023-02-22, salience decomposition.
- Double-verification arm: same bucket diagnostic on both T-A render books
  from a render-window funding cache (1,600,564 settlement rows); 0 entries
  missing a prior rate.

## Results

**Ledger grid (baseline −20.23% net): every declared cell improves net in BOTH
eras** — the only V4 family so far with a fully era-stable ledger effect:

| Cell | Removed | Net Δ full | Net Δ early | Net Δ late |
|---|---:|---:|---:|---:|
| skip_K−0.05% | 1,814 | +5.93pp | +0.23pp | +5.70pp |
| skip_K−0.1% | 1,305 | +5.91pp | +1.83pp | +4.07pp |
| skip_K−0.2% | 850 | +4.75pp | +2.07pp | +2.68pp |
| half_K−0.1% | (½ wt) | +2.95pp | +0.92pp | +2.04pp |
| **combo_K−0.1%** | **1,009** | **+7.99pp** | **+2.60pp** | **+5.38pp** |

- Salience (skip_K−0.1%): funding saved +13.34% + cost saved +2.93% vs gross
  forgone +10.36% — funding dominates, exactly the claimed mechanism.
- **The T-D forecast is economically material here.** combo_K−0.1% removes 296
  FEWER trades than skip_K−0.1% (the meanrev forecast predicts recovery for
  them) yet gains +2.08pp more — the rescued trades are net-positive. T-D's
  tail skill, useless under the failed Stage-2 framing, adds value as a
  conditioner qualifier on this surface.
- Overlap with T-E (from `t-e/2026-07-19/te_overlap_funding.csv`): 43.8% of
  deep_neg is also stale (>24h), but at-high ∩ deep_neg is still negative
  (−1.20%, 234 trades) — the funding cut is not a staleness proxy.

**Render books (double-verification arm) — sign inversion, stronger than T-E's:**

| Book | deep_neg net (full) | deep_neg bps | deep_neg TP rate | deep_neg net (late) |
|---|---:|---:|---:|---:|
| gate-on | **+16.45%** | +4.57 | 40.8% | +13.50% |
| gate-off | **+19.61%** | +2.26 | 42.6% | +16.10% |

On both render books, in both eras, deep-negative-funding entries are the
*best* funding bucket — the exact trades the ledger rule skips. The ledger's
late era (2023-02 → 2024-12) overlaps the render window, so this is a **shape
effect, not a regime effect**: under the deployed exit geometry (TP rates
40%+ vs 15% barebones) the reversion is captured before funding drag
accumulates, while the barebones 12%-TP / 24h-hold shape sits through the
funding bleed.

## Bybit funding-timing semantics (T-B's open question, closed)

Verified from venue documentation and archives (details and URLs recorded in
`bybit_funding_timing.md` beside this file): **Bybit changed mechanism on
2022-06-30 → 2022-07-05.** Before: the rate charged at settlement T was locked
one full interval ahead (fixed at interval start). Since: the rate is computed
progressively over the current interval (linearly weighted premium-index
average, weights rising toward settlement) and is final only at settlement.

Consequence for T-B: the era-stable "next-rate" floor variant is **not
registrable** — under the current regime the "next" rate is a fluctuating
estimate at entry, so any next-rate rule is look-ahead-biased from July 2022
onward. The strictly-PIT `prev` convention used here and in T-B remains valid
across the whole sample. Caveats: per-symbol funding intervals are unstable
(8h/4h/2h/1h switches, sometimes unannounced) — settlement cadence must come
from realized settlement history, which this machinery already does.

## Read

Under the program's double-verification rule, **no T-G cell advances**: fully
same-signed on the barebones ledger and both eras, but opposite-signed on both
render books. Combined with T-E, the pattern is now systematic: *entry-quality
cuts discovered on the barebones surface measure the barebones exit shape, not
the entry*. The deployed shape converts the same conditions (fresh highs,
deep-negative funding) into its best trades. That is the program-level finding;
it redirects future entry-conditioning work to render-book-native surfaces.
The T-D-forecast interaction (+2.08pp) is the one result worth carrying
forward — as a feature/qualifier in render-native designs, not as this filter.

## Limitations

- Spent discovery surface; nothing here is out-of-sample.
- No capacity backfill; render-book arm is a bucket diagnostic on
  already-rendered T-A outputs, not a re-render.
- Funding-semantics history is reconstructed from venue docs/archives; the
  2022-06-30 → 07-05 rollout window is per-symbol ambiguous.

## Next action

No prototype. `known_rate_prev` and the meanrev forecast stay in T-H's frozen
feature set; the funding-timing answer is recorded as the registrability
constraint for any future funding-floor design (prev-convention only).

Artifacts: `tg_grid.csv`, `tg_bucket_diagnostic.csv`, `tg_render_buckets.csv`,
`tg_trade_panel.parquet` (local; hash in `manifest.json`),
`bybit_funding_timing.md`, manifest with grids and reproduction checks.
