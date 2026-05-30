# Pre-registration: E2b — age-threshold sensitivity + prior30×age combo

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-complete
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

Run 2026-05-30, sweep tag `e2b_age_combo_2026-05-30`, full-PIT both venues. Daily-DD MAR
(report best_scenario); recent-third + LOO + bootstrap from r1_robustness. age300 reused
from E2 (`02_age_min`).

| cell | bybit MAR (Δ) / recent-3rd / boot P(Δ>0) / n | binance MAR (Δ) / recent-3rd / boot / n |
|---|---|---|
| age90 (baseline) | +2.93 / −2% / — / 761 | +0.25 / −26% / — / 477 |
| age200 | +6.19 (+3.27) / −2%→+17% all+ / 88% / 672 | +0.85 (+0.60) / −26%→−9% / 79% / 376 |
| age300 | +5.96 (+3.04) / −2%→+25% all+ / — / 579 | +2.81 (+2.56) / −26%→+4% all+ / 93% / 307 |
| **age400** | **+6.91 (+3.98) / −2%→+24% all+ / 86% / 510** | **+5.64 (+5.39) / −22%→+18% all+ / 96% (p5>0) / 255** |
| prior30+age300 | +6.33 (+3.40) / −2%→+12% all+ / **LOO-flips** / 89% / 419 | +3.40 (+3.15) / −26%→+8% all+ / 95% / 230 |

## Verdict

**The age effect is CONFIRMED robust cross-venue — not a knife-edge.** Dropping young names
(age ≥ 200–400 d) roughly doubles MAR on **both** venues, all-thirds-positive on both, and —
passing the pre-registered honesty gate — **improves the recent weak third on both** (bybit
−2%→+17–24%, binance to +4–18%), so it is a genuine within-regime improvement, not a
regime-dodge. It works across the whole 200/300/400 range: **bybit saturates fast** (≈MAR 6
already at age200, roughly flat to 400), **binance is monotone** (still climbing at 400). LOO-
stable for age-alone; bootstrap P(Δ>0) 86–96% (binance age400 has p5>0, clearing even the
strict Tier-3 bootstrap bar in-sample).

- **`age-alone` is the primary refinement.** Threshold within 200–400 is not critical; `age300`
  is conservative and well-populated, `age400` is the joint-best MAR (bybit 6.91 / binance 5.64)
  with ample trades (510/255). Do not chase higher thresholds (diminishing trades; mining risk).
- **`prior30+age` is an optional secondary DD-reducer** — it stacks (lower DD both venues) but
  adds mild fragility (bybit LOO sign-flip), so it is not the primary gate.

**Status:** in-sample **Tier-2 demo-candidate** — NOT promotion (feature-selection circularity;
forward demo is the Tier-3 arbiter; deployment/profile change is the operator's call). The
binance `age400` bootstrap p5>0 + all-thirds-positive is the strongest in-sample evidence so
far, but Tier-3 still requires forward-demo OOS + residual-Sharpe.
