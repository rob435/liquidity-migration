# R2 — Per-feature standalone decile-sort + correlation/PCA — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [r2-per-feature-standalone.md](r2-per-feature-standalone.md) / parent [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R2.
**Run label:** `exploratory` / **descriptive** (per the plan, no R2 feature graduates alone; output feeds R9 only).
**Runner:** `scripts/r2_per_feature_sweep.py` (committed c2a6c0e). Tag `r2_per_feature_2026-05-29`. 5 features × 3 horizons × 2 venues = 30 cells + 5×5 correlation + PCA per venue.

## Headline

The 5 Phase-5 IC features **collapse to ONE dominant factor** (PC1 = 87.8% bybit /
81.4% binance of decile-P&L variance; top-2 = 93.8% / 90.3%), with all pairwise
Spearman correlations **0.72–0.92** on both venues. **The plan's 2-orthogonal-factor
hypothesis (vol/extension ⊥ momentum, inter-ρ ≤ 0.2) is REJECTED** — the features
select largely the same high-vol/extended/high-momentum alt basket. → R9 uses the
pre-registered "not-2-clusters" contingency: diversification-adjusted IC weighting
across the 5 (≈ a single composite IC factor, given the collinearity).

## Integrity

Full-PIT **by construction**: `signal_harness.build_feature_panel` reads the
`*_full_pit` root (delisted-inclusive universe; bybit panel 460,546 rows /
binance 420,581), NOT the volume-events engine — so `--allow-partial-pit` never
applied and there is no survivorship filter. In-process, one venue panel at a
time (memory-safe). Window 2023-04-01 → 2026-05-28 (1153 d).

## Factor structure (the actionable output)

5×5 Spearman on the 3d per-feature decile P&L:

| | vov30 | rv7 | dist_low | xsret7 | xsret3 |
|---|--:|--:|--:|--:|--:|
| **bybit** inter-feature ρ range | — | 0.79–0.92 | (all pairs **0.79–0.92**) | | |
| **binance** inter-feature ρ range | — | 0.72–0.85 | (all pairs **0.72–0.85**) | | |

PCA variance shares: bybit PC1 **0.878**, PC2 0.059 (cum 0.938); binance PC1
**0.814**, PC2 0.088 (cum 0.903). The supposed "vol/extension vs momentum"
split does not exist in the P&L: e.g. `vol_of_vol_30d`↔`xs_rank_ret_3d` ρ = 0.82
(bybit) / 0.72 (binance), far above the 0.2 orthogonality bar. (Spearman/PCA are
robust to the near-daily constant cost term, which centers/ranks out — see below.)

## Per-feature standalone P&L is cost+beta-dominated (uninformative, expected)

Every (feature × horizon × venue) cell shows ret −0.88× to −1.00× / DD −90% to
−100% / MAR −0.5 to −1.0. **This is NOT evidence the features lack edge** — it is
the documented descriptive-simplification artifact: `decile_spread_pnl` re-shorts
a fresh top-decile basket EVERY signal day (~1150 days) at 18 bps round-trip, so
**cost alone compounds to ≈ (1−0.0018)^1150 ≈ −87%**, and short-only baskets add
2023–25 alt-bull-market beta losses. The docstring states this is "valid for
descriptive analysis; R9 uses proper position-lifecycle accounting." So the
**absolute decile P&L is not a tradeable read**; it re-confirms Round-1 Phase-6
that these features are NOT standalone continuous-short signals — they are
cross-sectional rank signals to be used WITHIN the event-driven framework (R9).

## DECISION

- **R2 is descriptive — no feature graduates alone** (per pre-reg).
- **Factor structure for R9:** 2-cluster hypothesis rejected → per the plan's
  contingency, R9 combines the 5 features by **IC-weighted, diversification-
  adjusted** weighting (IC × 1/avg-intra-corr). Given PC1 ≈ 82–88%, this is
  effectively a **single composite IC factor**; adding all 5 vs 1 composite buys
  little orthogonal information. R9 should NOT treat vol/extension and momentum as
  independent sleeves.
- The standalone decile P&L is logged as descriptive context only; not cited as
  edge/anti-edge.

## Next

R3 (bearish-stack honest test — needs ~3h of mirror-filter CLI flags + a sweep);
R4 risk-factor model (foundational code, built in parallel). R9 will consume this
R2 factor finding + the R4 residualization.
