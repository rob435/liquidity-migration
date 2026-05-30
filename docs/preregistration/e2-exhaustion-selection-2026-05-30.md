# Pre-registration: E2 — exhaustion-quality SELECTION refinement

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-complete
**Plan:** [research_plan_selection_execution.md](../research_plan_selection_execution.md) §E2 (pivoted to SELECTION per E1's contingency)
**Follows:** [e1-execution-premium-2026-05-29.md](e1-execution-premium-2026-05-29.md) (E1 verdict = selection-dominant)

## What's changing

E1 showed the alpha is SELECTION, not execution. E2 tests whether a pre-registered
**exhaustion-quality gate** on the selection filter improves risk-adjusted return
cross-venue. Thesis: the best shorts are **seasoned, liquid names whose pump is
exhausting** — not fresh alts still ripping. No new code; existing CLI selection knobs.

## Hypothesis + provenance (honest)

Directions are fixed a priori by an **exploratory cross-venue within-selection IC** on
the realized E1 shorts (Spearman of feature vs net_return, both venues same sign):
older `symbol_age`/`pit_age` (+0.11/+0.13), more-liquid `liquidity_rank` (−0.11/−0.14),
no fresh `prior30_max_daily_return` spike (−0.07/−0.13). **Circularity caveat:** features
were chosen from in-sample IC, so any in-sample MAR gain is partly fit. This is therefore
a **Tier-2 demo-candidate** test only (backtest→demo is paper; permissive) — NOT promotion
evidence. The Tier-3 real-money gate (forward demo OOS) is the true arbiter and is untouched.
Derivative features (OI/taker/funding) are **excluded** — they are NaN on binance
([[binance-derivative-metrics-missing]]) so cannot form a cross-venue gate.

## Arms (per venue; common round thresholds = one rule both venues, ~the pooled terciles)

| cell-id | added filter(s) vs baseline | rule |
|---|---|---|
| `00_baseline` | — | control (current promoted selection) |
| `01_prior30_cap` | `prior30-max-return-max=0.14` | drop top-tercile prior-spike names (they keep running) |
| `02_age_min` | `pit-age-days-min=300` | drop youngest-tercile names (fresh listings squeeze) |
| `03_liq_tighten` | `universe-rank-max=110` | keep the more-liquid band (drop least-liquid tercile) |
| `04_exhaustion_combined` | all three above | the combined exhaustion-quality gate |

Fixed both venues: `max-active-symbols=12`, `cost-multipliers=1` (15 bps),
`bar_extreme_capped`, full-PIT, `2023-04-01 → 2026-05-28`. Sweep tag
`e2_exhaustion_select_2026-05-30`. (binance funding-missing label applies.)

## Predicted direction + magnitude

- Each component and the combined gate should **raise MAR vs baseline on BOTH venues**
  (esp. bybit), by removing the worst (still-momentum) shorts. Combined strongest.
- Trade count drops (each tercile cut keeps ~2/3); combined keeps ~30–50% → still
  ≥30 by / ≥20 bn (baseline 763/477). If any cell falls below mins, it's disqualified.
- **Falsifier:** if no cell improves MAR on both venues, OR the improvement is only in
  the strong early thirds (not the recent weak third), the exhaustion-quality thesis is an
  in-sample/regime artifact → document, do not pursue. (The edge is front-loaded, so the
  recent-third check is the key honesty gate against reloading the 2023–24 regime.)

## Roots that will be touched

- [x] bybit_full_pit
- [x] binance_full_pit (funding-missing)
- [ ] forward demo/paper

## Decision rule (a priori)

`scripts/r1_robustness.py --sweep-tag e2_exhaustion_select_2026-05-30 --control 00_baseline`.
Tier-2 demo-candidate: pooled MAR Δ > +0.1, positive both venues, neither worse than
−0.5 MAR, ≥30 by / ≥20 bn trades. **Additional a-priori honesty gate:** the winning cell
must also improve (or not worsen) the **recent third** vs baseline on both venues — a gate
that only helps the early regime is rejected. Report the **full 5-cell distribution**, not
just the best. No threshold re-tuning to rescue a near-miss (single pre-registered rule).

## Run command

```bash
bash scripts/e2_exhaustion_dispatch.sh
.venv/bin/python scripts/r1_robustness.py --sweep-tag e2_exhaustion_select_2026-05-30 --control 00_baseline
```

## Post-run results

Run 2026-05-30, sweep tag `e2_exhaustion_select_2026-05-30`, full-PIT both venues.
Daily-DD MAR (report best_scenario) vs `00_baseline` control:

| cell | bybit MAR (Δ) / trades | binance MAR (Δ) / trades | recent-third (base→cell) | bootstrap P(Δ>0) |
|---|---|---|---|---|
| 00_baseline | +2.93 / 761 | +0.25 / 477 | by +8% / bn −26% | — |
| **02_age_min** | **+5.96 (+3.04) / 579** | **+2.81 (+2.56) / 307** | **by −2%→+25%, bn −26%→+4%** (all-thirds+ both) | by 83% / bn 93% |
| 01_prior30_cap | +4.34 (+1.42) / 543 | +2.14 (+1.89) / 366 | by +8%→+4% (all+), bn −26%→−6% | by 63% / bn 95% |
| 03_liq_tighten | +2.10 (−0.82) / 600 | −0.15 (−0.40) / 381 | by +8%→−10%, bn worse | by 47% / bn 19% |
| 04_combined (pre-reg) | (diluted; incl. rejected liq) | +0.33 (+0.08) / 179 | bn worse | bn 49% |

**Mechanism (verified, cross-venue, on baseline ledgers):** young-name (<300d) shorts are
systematic net losers — bybit −0.06%/tr (55% win), binance −0.08%/tr (52% win) — and worst
recently (bybit recent young −0.22%/tr 49% win; binance recent young −0.21%/tr 48% win, and
they were ~half of recent binance trades). Old-name shorts stay solid (60–64% win). The
2024–25 listing wave flooded the universe with fresh alts that squeeze shorts; the signal
works on seasoned names.

## Verdict

**`02_age_min` (pit-age-days-min=300) is a robust cross-venue SELECTION refinement — Tier-2
demo-candidate.** It ~doubles daily-DD MAR on both venues (return up, DD down), is
all-thirds-positive on both, LOO-stable, bootstrap P(Δ>0) 83%/93%, and — passing the
pre-registered honesty gate — **improves the recent (weak) third on both venues** (the
age→early-regime confound is refuted: it hurts the early third, rescues the recent). It also
**explains the prior "edge decaying recently" caveat**: the decay was concentrated in
fresh-listing shorts.

`01_prior30_cap` is a solid secondary cross-venue risk-reducer (halves DD; MAR Δ +1.4 by /
+1.9 bn). `03_liq_tighten` is **rejected** (hurts both venues — the liquidity_rank IC was a
within-realized artifact). `04_combined` (as pre-registered) is **diluted** by the rejected
liq_tighten — so the pre-registered combined gate is NOT the answer; the *components*
`age_min` and `prior30_cap` are.

**Status:** in-sample Tier-2 demo-candidate — NOT promotion (feature-selection circularity;
forward demo is the Tier-3 arbiter). **Deployment is the operator's call** (the live demo runs
pit-age-days-min≈90; do not change the profile autonomously). **Follow-on (E2b):** age-threshold
sensitivity (200/400, guard against a knife-edge) + a refined `prior30+age` combined (drop the
rejected liq_tighten) — does stacking the two winners beat `age_min` alone cross-venue?
