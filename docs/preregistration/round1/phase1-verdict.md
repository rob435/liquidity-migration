# Phase 1 — universe-isolation diagnostic (VERDICT)

**Date:** 2026-05-28 (sweep ran 2026-05-28 00:11 → 01:32, ~81 min wall)
**Stage:** run-complete, **H1 FALSIFIED on baseline pair; H1 NOT CONFIRMED on averaged metric**
**Pre-reg:** [docs/preregistration/round1/phase1-universe-isolation-diagnostic.md](phase1-universe-isolation-diagnostic.md)
**Phase label per parent plan Appendix B:** `biased_benchmark` for all 474
cells (NEVER tradable); `exploratory` descriptive only for the 764 cells.

## Headline

**H1 (universe widening explains the post-fix DD shift on Bybit) is
FALSIFIED on the baseline pair AND not confirmed on the 6-pair average.**

The 474-baseline DD improvement over the 764-baseline is **+1.7pp**,
below the +5pp falsifier threshold. The averaged DD improvement across
all 6 paired configs is **-5.78pp** of improvement (-5.78pp = 474 is
5.78pp less negative on average), below the +8pp confirmation threshold.
The directional split between the 6 pairs is also striking: 4 pairs
show small DD improvements (≤+2.3pp), 2 pairs show large ones (+13.6pp,
+22.0pp), but the LARGE ones are in restrictive-config cells
(rank_max 200 + rank_improvement_min 200), not the baseline.

Sharpe IS strongly affected by universe widening — avg Sharpe Δ across
all 6 pairs is **+1.09** (clears +0.5). The wider universe dilutes
per-trade quality (more trades from lower-liquidity recent listings),
but it does NOT disproportionately inflate tail risk.

**Implication for Phase 2:** in-sample DD numbers should be interpreted
at FACE VALUE. No downweighting required. Universe contamination is not
the dominant DD driver.

## Full per-pair table

Window 2025-01-01 → 2026-05-28, Bybit only, 12 cells = 6 paired configs.

| Pair | 474_sh | 764_sh | sh_Δ | 474_dd | 764_dd | DD_Δ (764-474) | 474_tr | 764_tr | 474_ret | 764_ret |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| baseline       | 3.81 | 2.28 | +1.53 | -40.4% | -42.1% | -1.7pp  | 318 | 415 | +21.2x | +5.2x |
| turn10M        | 2.66 | 2.24 | +0.42 | -28.7% | -28.4% | +0.3pp  | 211 | 289 | +5.2x  | +4.5x |
| rankmax200     | 3.36 | 1.73 | +1.63 | -26.2% | -48.2% | -22.0pp | 318 | 416 | +11.2x | +2.6x |
| rankimp200     | 3.81 | 2.76 | +1.05 | -25.2% | -38.8% | -13.6pp | 284 | 380 | +18.2x | +7.8x |
| hold2          | 3.88 | 2.41 | +1.46 | -40.4% | -38.1% | +2.3pp  | 321 | 426 | +22.0x | +5.9x |
| combo          | 1.96 | 1.49 | +0.47 | -40.5% | -40.5% | 0pp     | 180 | 249 | +1.9x  | +1.4x |
| **avg**        | 3.25 | 2.15 | **+1.09** | -33.6% | -39.4% | **-5.78pp** | 272 | 363 | +13.3x | +4.6x |

## H1 falsification — baseline pair

The H1 falsifier from the parent plan (verbatim):

> Falsifier: 474-baseline DD does not materially improve vs the 764 baseline
> (DD Δ < 5pp), implying the shift is NOT universe-driven and something else
> (bug-fix, regime, code drift) is responsible.

Baseline pair: **474_dd = -40.4%, 764_dd = -42.1%, improvement = +1.7pp**.
This is well below the +5pp threshold. **H1 falsifier condition met on
the baseline pair.**

Reading: the 474-archive-only universe with default production settings
has effectively the same drawdown as the full 764 universe (1.7pp delta
is well within noise for ~17 months of data). So the "+42% DD vs the
historical 22%" shift is NOT explained by the v5-listing supplement.
Other candidates (bug fix removing rank-deteriorating signals; regime
shift over 2025-2026; code drift from intermediate commits) are the
remaining candidates. None of those are testable in Phase 1; they get
addressed elsewhere in the program (Phase 2 explicitly tests the
direction question).

## Averaged interpretation rule — not confirmed

The pre-registered interpretation rule (verbatim):

> If the avg sharpe-like Δ across the 6 paired 474-vs-764 cells is > +0.5
> AND avg DD Δ is < -8pp, conclude universe widening explains most of the
> DD shift.

- avg Sharpe Δ = **+1.09** → clears +0.5
- avg DD Δ = **-5.78pp** improvement → does NOT clear -8pp

The conjunction is not satisfied. The averaged metric says "universe
widening clearly hurts Sharpe (per-trade quality dilution) but its impact
on DD is inconclusive at the population level (and falsified at the
baseline level)".

## Sharpe interpretation

The Sharpe effect IS large and consistent — 6 of 6 pairs show 474 has
HIGHER Sharpe than 764 (+0.42 to +1.63). Average +1.09. The mechanism is
clear: removing 290 low-liquidity recent listings from the universe
removes a population of marginal trades that dilute per-trade quality.

But this is **per-trade Sharpe dilution**, not tail-risk inflation. The
DD numbers across pairs are nearly identical for 4 of 6 configs. The
universe-widening effect is "more trades, lower average quality, similar
worst-case losses" — exactly the trade-count-dilution mechanism predicted
in H1's text, but not the tail-risk-inflation that would have shown up
as large DD widening.

## Pre-commitment compliance

- ✅ DESCRIPTIVE only — no candidate / promotion decision was generated
  from Phase 1.
- ✅ All 474 cells labeled `biased_benchmark`. None are tradable.
- ✅ Interpretation thresholds (+0.5 Sharpe Δ AND -8pp DD Δ) not
  loosened.
- ✅ No cross-venue expansion (Binance has no v5-listing analog, scope
  was Bybit-only as the Phase 1 pre-reg committed).
- ✅ No off-menu cells.

## Secondary observations (DESCRIPTIVE only — NO action)

1. **rank_max 200 + rank_imp 200 on the 474 universe show striking
   numbers** (3.36-3.81 Sharpe, -25 to -26% DD, 284-318 trades). These
   are BIASED_BENCHMARK cells; they CANNOT be traded. Their existence
   is informative for understanding "how good could this strategy look
   if we cheat on the universe" but not for production decisions.

2. **The `combo` cell collapses to ~1.5 Sharpe on both universes.**
   Stacking turn10M + hold=2 + rankimp200 doesn't multiply gains; the
   filters are interacting non-monotonically. Suggests filter stacking
   is non-trivial; Phase 0's LOO-only methodology doesn't catch this.

3. **The Phase 0 BASELINE Sharpe (2.45) is HIGHER than this Phase 1
   764 baseline Sharpe (2.28)** — different windows (Phase 0: 2023-04
   → 2026-04; Phase 1: 2025-01 → 2026-05). The 17-month window is more
   noise-dominated. Both numbers are well above the strategy's
   historical ~1.0-1.2 Sharpe.

## Implications for Phase 2

- **DO NOT downweight Phase 2 in-sample numbers** for universe
  contamination. H1 falsified.
- **Continue to interpret Phase 2 cells on their face**, applying the
  Strictness Manifesto thresholds (+0.5 Sharpe Δ on BOTH venues AND
  -5pp DD Δ on BOTH venues + sign-consistency + ≥50 trades per
  sub-period).
- The remaining candidate explanations for the DD shift (bug-fix
  removing rank-deteriorating signals; regime; code drift) are not
  testable in Phase 1. Phase 2's direction grid is the explicit test
  of one of them (bug-fix-driven direction collapse).

## Pre-commitment for downstream

1. **No 474 cell is promotable.** The two striking 474 cells
   (rankmax200_474, hold2_474) MUST NOT be cited as alpha evidence
   anywhere. They are biased_benchmark by construction.
2. **No Phase 1 cell goes to Phase 7.** Phase 1's labels are
   biased_benchmark / exploratory; neither qualifies for the OOS gate.

## Artifacts

- Pre-reg: `docs/preregistration/round1/phase1-universe-isolation-diagnostic.md`
- Summary CSV: `~/SHARED_DATA/phase1_universe_diag_2026-05-27_summary.csv`
- Per-cell reports:
  - 474: `~/SHARED_DATA/bybit_full_pit_archive_only/reports/phase1_universe_diag_2026-05-27/<cell>/`
  - 764: `~/SHARED_DATA/bybit_full_pit/reports/phase1_universe_diag_2026-05-27/<cell>/`
- Side-copy manifest: `~/SHARED_DATA/bybit_full_pit_archive_only/BUILD_MANIFEST.json`
  (464,475 archive-source rows kept / 79,838 v5-listing dropped, built 2026-05-27)

## Forward pointer

**Next: Phase 5a (build feature panels per venue),** the dependency for
Phase 5b IC measurement. After 5a + 5b, Phase 2 (rank-direction grid)
runs. Conditional Phases 3, 4, 6, 7 dispatch based on Phase 2 / 5 outcomes.
