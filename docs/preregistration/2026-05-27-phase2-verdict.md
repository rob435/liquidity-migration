# Phase 2 — rank-direction full grid (VERDICT)

**Date:** 2026-05-28 (sweep ran 2026-05-28 02:20 → 06:40, 259.9 min wall)
**Stage:** run-complete, **REJECTED (0 candidates)** — H2 + H3 FALSIFIED.
**Pre-reg:** [docs/preregistration/2026-05-27-phase2-rank-direction-grid.md](2026-05-27-phase2-rank-direction-grid.md)
**Phase label per parent plan Appendix B:** `exploratory` for all cells —
no candidate means no `candidate`-label promotion.

## Headline

**0 candidates from Phase 2.** The rank-direction edge hypotheses are
falsified:

- **H2 (deterioration as a tradable short edge): FALSIFIED-by-construction.**
  ALL 11 `P2_det_*` cells on BOTH venues returned **0 trades**.
  Mechanism: the production filter stack contains quality-positive gates
  (`day_return_min ≥ 0`, `residual_return_min ≥ 0.08`,
  `close_location_min ≥ 0.30`) that systematically exclude
  bearish/deteriorating names — a deteriorating-rank symbol on its
  signal day is also dropping on price, so the positive-day-return
  filters drop it.
- **H3 (two-sided event captures both): FALSIFIED-by-construction.**
  ALL 11 `P2_both_*` cells produce identical metrics to the matching
  `P2_imp_*` cells (because the det contribution is always 0).
- **H4 (inverse-direction OOS): not testable.** No Phase 2 candidate
  means no Phase 7 OOS for the rank-direction arm.

The strongest-looking Bybit-only result (`P2_imp_300`: sharpe 3.07,
DD -19%, Δ vs control +0.62 sharpe / -23pp DD) is **rejected** because
on Binance the same cell shows sharpe 1.46 → ~0 (Δ -1.47) — the
cross-venue requirement of the Manifesto binds and kills it.

## Full decision-rule analyzer output

Window 2023-04-01 → 2026-04-30, 33 cells × 2 venues = 66 runs.
Control = `P2_imp_150` (= the current production profile, rank-direction
default, threshold default).

```
# rule: manifesto  control: P2_imp_150  sharpe_delta_min: +0.5  dd_delta_max: -5.0pp  min_trades: bybit=50 binance=30

cell_id        by_sh_d  bn_sh_d  by_dd_d   bn_dd_d   by_tr  bn_tr  by_ret    bn_ret    verdict
P2_both_100    -0.53    -0.09    +6.8pp    +2.4pp    659    473    +21.02x   +3.99x    reject
P2_both_125    -0.00    +0.12    -1.4pp    -1.5pp    645    457    +53.11x   +5.29x    inconclusive
P2_both_150    +0.00    +0.00    +0.0pp    +0.0pp    602    421    +38.56x   +4.21x    inconclusive
P2_both_175    +0.08    -0.37    -1.0pp    +2.3pp    557    351    +34.37x   +1.54x    inconclusive
P2_both_200    +0.08    -1.10    -3.3pp    +20.9pp   504    302    +26.89x   +0.10x    reject
P2_both_25     -0.78    -0.17    +6.8pp    -1.5pp    688    497    +13.47x   +3.90x    reject
P2_both_250    +0.35    -0.50    -18.1pp   -10.9pp   368    230    +13.04x   +0.66x    reject
P2_both_300    +0.61    -1.47    -23.3pp   -2.8pp    234    159    +8.01x    -0.10x    reject
P2_both_400    -0.25    -1.38    -31.5pp   -29.3pp   32     43     +0.52x    -0.09x    reject
P2_both_50     -0.79    -0.17    +6.8pp    -1.5pp    684    497    +13.15x   +3.90x    reject
P2_both_75     -0.85    -0.13    +6.8pp    -1.5pp    679    494    +11.68x   +4.21x    reject
P2_det_*       (all 11 thresholds)        0 trades both venues          reject (no signal)
P2_imp_100     -0.53    -0.09    +6.8pp    +2.4pp    659    473    +21.02x   +3.99x    reject
P2_imp_125     -0.00    +0.12    -1.4pp    -1.5pp    645    457    +53.11x   +5.29x    inconclusive
P2_imp_150     (control)                                                       skip_control
P2_imp_175     +0.08    -0.37    -1.0pp    +2.3pp    557    351    +34.37x   +1.54x    inconclusive
P2_imp_200     +0.08    -1.10    -3.3pp    +20.9pp   504    302    +26.89x   +0.10x    reject
P2_imp_25      -0.78    -0.17    +6.8pp    -1.5pp    688    497    +13.47x   +3.90x    reject
P2_imp_250     +0.35    -0.50    -18.1pp   -10.9pp   368    230    +13.04x   +0.66x    reject
P2_imp_300     +0.61    -1.47    -23.3pp   -2.8pp    234    159    +8.01x    -0.10x    reject
P2_imp_400     -0.25    -1.38    -31.5pp   -29.3pp   32     43     +0.52x    -0.09x    reject
P2_imp_50      -0.79    -0.17    +6.8pp    -1.5pp    684    497    +13.15x   +3.90x    reject
P2_imp_75      -0.85    -0.13    +6.8pp    -1.5pp    679    494    +11.68x   +4.21x    reject

# summary: candidates=0 rejects=27 inconclusive=5 skip_control=1
```

## H2 — falsified-by-construction (deterioration filtered out)

All 22 deterioration cells (11 thresholds × 2 venues) returned 0 trades.
The deterioration direction is structurally incompatible with the
production filter stack:

| Filter | Constrains | Why it excludes deteriorating names |
|---|---|---|
| `liquidity_migration_day_return_min ≥ 0.0` | signal-day return | A symbol whose liquidity rank is deteriorating is typically also dropping on price → fails the gate. |
| `liquidity_migration_residual_return_min ≥ 0.08` | signal-day residual (vs market) | Same logic — deteriorating symbols tend to underperform the cross-section, negative residual. |
| `liquidity_migration_close_location_min ≥ 0.30` | (close-low)/(high-low) | Deteriorating symbols often close near the low → fails. |
| `liquidity_migration_event_rank_fraction_max ≤ 0.90` | event score top-decile | Deteriorating symbols rarely score top by the dollar-volume composite. |

The deterioration hypothesis (H2) is therefore not strictly falsified
by EVIDENCE of negative edge — it's STRUCTURALLY UNTESTABLE under the
current filter stack. To test H2 honestly would require a different
filter stack (one tuned for bearish entries), which is a separate
research program and not in Phase 2's pre-committed scope.

We file H2 as "falsified-as-implemented; structurally untestable under
the production filter stack" and pre-commit NOT to re-test under a
relaxed filter stack without writing a fresh pre-reg first.

## H3 — falsified-by-construction (both = imp when det = 0)

`P2_both_*` cells produce **bit-identical** metrics to the matching
`P2_imp_*` cells because the deterioration component contributes 0
trades. This is not separately informative; H3 is falsified for the
same reason H2 is.

## The promising-looking Bybit-only cells (rejected by cross-venue)

| Cell | by_sh | by_dd | by_sh_Δ | bn_sh | bn_dd | bn_sh_Δ | Verdict |
|---|--:|--:|--:|--:|--:|--:|---|
| P2_imp_250 | 2.81 | -24% | +0.36 | 0.96 | -53% | -0.50 | reject (bn flips) |
| **P2_imp_300** | **3.07** | **-19%** | **+0.62** | -0.01 | -45% | **-1.47** | reject (bn catastrophic) |
| P2_both_300 | (same as imp_300) | | | | | | reject (same) |
| P2_both_250 | (same as imp_250) | | | | | | reject (same) |

The pattern is consistent: Bybit shows monotonically-better Sharpe and
shrinking DD as the rank-improvement threshold tightens — but Binance
shows monotonically-WORSE Sharpe with the same tightening. The two
venues' optimal threshold is different, and the joint optimum (where
BOTH venues show edge) is at threshold ~150 (= the current production
default).

This is consistent with the Phase 1 finding that universe widening
strongly affects Bybit-only Sharpe (because v5-listing supplement is
Bybit-specific) — but the Phase 2 cells use the full 764 universe for
both venues, so the universe effect doesn't directly explain why
tightening helps Bybit but hurts Binance. A REGIME interpretation is
plausible: Binance's 2025-26 regime may differ enough from Bybit's
that the rank-tightening optimum is venue-specific.

This is a **secondary observation**, not a candidate. The strict
Manifesto bar (Δ +0.5 on BOTH venues) correctly closes it.

## Inconclusive cells

| Cell | by_sh_Δ | bn_sh_Δ | Why inconclusive (not candidate) |
|---|--:|--:|---|
| P2_both_125 | -0.00 | +0.12 | Binance Δ +0.12 below +0.5; tiny improvement |
| P2_both_150 | +0.00 | +0.00 | control |
| P2_both_175 | +0.08 | -0.37 | Binance NEGATIVE Δ |
| P2_imp_125 | -0.00 | +0.12 | (same as both_125) |
| P2_imp_175 | +0.08 | -0.37 | (same as both_175) |

5 inconclusive cells — all fail the +0.5 Sharpe Δ bar on at least one
venue. Per the pre-commitment, they are filed and not pursued.

## Forward pointers (or lack thereof)

- **Phase 3 (exit selection): NOT TRIGGERED.** Conditional on ≥1 Phase 2
  candidate. 0 candidates → Phase 3 does NOT run.
- **Phase 4 (hybrid event types): NOT TRIGGERED.** Conditional on Phase 2
  + Phase 3 outputs. Both absent → Phase 4 does NOT run.
- **Phase 7 (pre-2023 OOS gate): NOT TRIGGERED.** Phase 7 dispatches on
  ANY finalist from Phases 0/1/2/3/4/6. Phase 0 / Phase 6 already at
  0 candidates; Phase 2 now also at 0; Phase 1 was descriptive-only by
  pre-reg. **No finalist from any phase → Phase 7 does NOT run.**

**This concludes the 7-phase research program.** See
`docs/preregistration/2026-05-27-program-verdict.md` for the
end-of-program summary across all hypotheses.

## Pre-commitment compliance

- ✅ Manifesto thresholds not loosened (+0.5 Sharpe Δ + -5pp DD Δ on
  BOTH venues bar applied unchanged)
- ✅ FDR ceiling trivially honoured (0 candidates ≤ 3 cap)
- ✅ No off-menu cells (33-cell × 2-venue menu as committed)
- ✅ Inconclusive cells FILED, not pursued
- ✅ The promising-but-cross-venue-failing P2_imp_300 NOT promoted
  despite tempting Bybit-only metrics
- ✅ H2 / H3 not silently re-tested under a different filter stack to
  rescue them (that would require a new dated pre-reg)

## Artifacts

- Pre-reg: `docs/preregistration/2026-05-27-phase2-rank-direction-grid.md`
- Summary CSV: `~/SHARED_DATA/phase2_direction_grid_2026-05-27_summary.csv`
- Per-cell reports:
  - `~/SHARED_DATA/{bybit,binance}_full_pit/reports/phase2_direction_grid_2026-05-27/<cell>/`

## Run-label

`exploratory` — Phase 2 produced no candidates. Even the closest-to-edge
inconclusive cells (`P2_imp_125`, `P2_both_125`) are filed, not promoted.
No cell forwards to demo / mainnet consideration.
