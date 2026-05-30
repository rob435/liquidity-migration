# Pre-registration: E2b — age-threshold sensitivity + prior30×age combo

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-pending
**Follows:** [e2-exhaustion-selection-2026-05-30.md](e2-exhaustion-selection-2026-05-30.md) (age_min=300 = robust cross-venue winner; liq_tighten rejected)

## What's changing

Two follow-ons to E2's winner (`pit-age-days-min=300`):
1. **Age-threshold sensitivity** — is 300 a knife-edge or a broad plateau? Test 200 and 400
   (300 already in E2). A robust effect should improve cross-venue across the range and ideally
   trend monotonically (older → better); a knife-edge that only works at 300 is suspicious.
2. **`prior30 × age` combo** — does stacking the two E2 winners (`prior30-max-return-max=0.14`
   + `pit-age-days-min=300`) beat `age_min` alone cross-venue, or are they redundant (a young
   name usually also has a big prior spike)? The rejected `liq_tighten` is dropped.

## Hypothesis

Age is a real structural signal (fresh listings squeeze shorts), so the improvement should be
**monotone-ish and broad** in the age threshold (not a 300-only spike). prior30 and age overlap
mechanically (both proxy "pumpy"), so the combo may add little beyond age alone — testing
whether the marginal DD reduction from prior30 stacks or is already captured by the age cut.

## Arms (per venue; same realistic baseline as E1/E2)

| cell-id | added filter | rule |
|---|---|---|
| `00_baseline` | — | control |
| `01_age200` | `pit-age-days-min=200` | looser age cut |
| `02_age400` | `pit-age-days-min=400` | stricter age cut |
| `03_prior30_age` | `prior30-max-return-max=0.14` + `pit-age-days-min=300` | stack the two E2 winners |

(E2's `02_age_min`=300 is the midpoint, reused for the 200/300/400 sensitivity curve.)
Fixed both venues: `max-active-symbols=12`, `cost-multipliers=1`, `bar_extreme_capped`,
full-PIT, `2023-04-01 → 2026-05-28`. Sweep tag `e2b_age_combo_2026-05-30`.

## Predicted direction + magnitude

- Sensitivity: 200 < 300 ≲ 400 in MAR if monotone; all three should beat baseline cross-venue
  if robust. Trades shrink as the cut tightens (400 keeps fewer) — `02_age400` must stay
  ≥30 by / ≥20 bn or it's disqualified on trade count.
- Combo: `03_prior30_age` MAR ≥ `age_min` alone if prior30 adds marginal DD reduction; ≈
  `age_min` if redundant; < if over-filtering.
- **Falsifier:** if the age improvement is knife-edge (only 300, not 200/400), or the combo
  collapses trades below the minimums / sign-flips a venue, treat age=300-alone as the
  refinement and stop tuning.

## Roots that will be touched

- [x] bybit_full_pit
- [x] binance_full_pit (funding-missing)
- [ ] forward demo/paper

## Decision rule (a priori)

`scripts/r1_robustness.py --sweep-tag e2b_age_combo_2026-05-30 --control 00_baseline`.
Report the full distribution. The age effect is "confirmed robust" iff 200/300/400 all improve
pooled MAR vs baseline cross-venue with recent-third intact. The combo is adopted over
age-alone only if it improves pooled MAR cross-venue **and** holds the recent third **and**
keeps trade counts above the minimums. No threshold cherry-picking — this characterizes
robustness, it does not select a winner to promote (Tier-3 forward demo remains the arbiter).

## Run command

```bash
bash scripts/e2b_age_combo_dispatch.sh
.venv/bin/python scripts/r1_robustness.py --sweep-tag e2b_age_combo_2026-05-30 --control 00_baseline
```

## Post-run results

(pending)

## Verdict

(pending)
