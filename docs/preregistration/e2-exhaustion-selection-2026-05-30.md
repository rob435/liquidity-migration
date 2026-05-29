# Pre-registration: E2 — exhaustion-quality SELECTION refinement

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-pending
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

(pending)

## Verdict

(pending)
