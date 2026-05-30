# Pre-registration: E2c — cost-robustness of the discrete age gate (45 bps)

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-pending
**Follows:** [e2-exhaustion-selection-2026-05-30.md](e2-exhaustion-selection-2026-05-30.md), [e2b-age-combo-2026-05-30.md](e2b-age-combo-2026-05-30.md)

## What's changing

E2/E2b established the age gate (`pit-age-days-min` ≥ 200–400) ~doubles MAR cross-venue at the
honest 15 bps cost. Before the operator decides whether to forward-demo it, harden it against
the **conservative ×3 cost (45 bps)**: does the age-gate *improvement vs baseline* survive, and
does the age-gated strategy stay return-positive on both venues at 45 bps?

## Hypothesis

The age gate works mainly by **removing losing trades** (young-name shorts) and cutting trade
count, so its cost drag is *lower* than baseline's — the improvement should be **cost-robust**,
and the gap vs baseline should if anything *widen* at higher cost. Falsifier: if at 45 bps the
age gate no longer beats baseline cross-venue, or the age-gated return goes negative on a venue,
the in-sample edge was cost-fragile and the demo case weakens.

## Arms (per venue; `--cost-multipliers 3` = 45 bps; else the realistic baseline)

| cell-id | filter | cost |
|---|---|---|
| `00_baseline` | age90 (baseline) | 45 bps |
| `01_age300` | `pit-age-days-min=300` | 45 bps |
| `02_age400` | `pit-age-days-min=400` | 45 bps |

Fixed both venues: `max-active-symbols=12`, `bar_extreme_capped`, full-PIT,
`2023-04-01 → 2026-05-28`. Sweep tag `e2c_cost_robust_2026-05-30`.

## Decision rule (a priori)

`scripts/r1_robustness.py --sweep-tag e2c_cost_robust_2026-05-30 --control 00_baseline`. The age
gate is "cost-robust" iff at 45 bps `age300`/`age400` still improve MAR vs baseline cross-venue,
stay return-positive both venues, and hold the recent third. Report the full distribution.

## Run command

```bash
bash scripts/e2c_cost_robust_dispatch.sh
.venv/bin/python scripts/r1_robustness.py --sweep-tag e2c_cost_robust_2026-05-30 --control 00_baseline
```

## Post-run results / Verdict

(pending)
