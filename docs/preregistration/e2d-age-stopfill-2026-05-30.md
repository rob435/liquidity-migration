# Pre-registration: E2d — worst-case stop-fill robustness of the discrete age gate

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-pending
**Follows:** E2/E2b/E2c (age gate robust to threshold, regime, cost)

## What's changing

The last in-sample robustness dimension: stop-fill assumption. The realistic default is
`bar_extreme_capped` (10% cap). This re-runs the age gate under the **worst-case
`bar_extreme`** (fills at the full adverse hourly wick — the brutal assumption that helped
manufacture the original Round-2 null). Does the age gate survive worst-case fills?

## Hypothesis

The age gate drops young/fresh-listed names, which have the **wildest wicks** (largest
`bar_extreme` slippage). So the age gate should be **especially** robust to worst-case fills —
the improvement vs baseline should *persist or widen* under `bar_extreme`. Falsifier: if the
age gate collapses under `bar_extreme` (like the original null), its strength is fill-model-
dependent and the demo case must note that.

## Arms (per venue; `--stop-fill-mode bar_extreme`; 15 bps; max_active=12)

| cell-id | filter |
|---|---|
| `00_baseline` | age90 |
| `01_age300` | `pit-age-days-min=300` |
| `02_age400` | `pit-age-days-min=400` |

Fixed both venues: full-PIT, `2023-04-01 → 2026-05-28`. Sweep tag `e2d_stopfill_2026-05-30`.

## Decision rule (a priori)

`scripts/r1_robustness.py --sweep-tag e2d_stopfill_2026-05-30 --control 00_baseline`. The age gate
is "fill-robust" iff under `bar_extreme` it still beats baseline cross-venue, stays return-positive
both venues, and holds the recent third. Report the full distribution.

## Run command

```bash
bash scripts/e2d_stopfill_dispatch.sh
.venv/bin/python scripts/r1_robustness.py --sweep-tag e2d_stopfill_2026-05-30 --control 00_baseline
```

## Post-run results / Verdict

(pending)
