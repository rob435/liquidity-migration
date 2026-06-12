# Pre-registration: E1 — capped composite size tilt (within the existing gate)

**Date:** 2026-06-12
**Author:** operator + Claude (round-4 session)
**Stage:** proposed
**Plan:** [research_plan_composite_sizing_2026-06-12.md](../research_plan_composite_sizing_2026-06-12.md)

## What's changing

Per-entry size multiplier `m = clip(0.5 + p, 0.5, 1.5)`, `p` = within-ts
percentile of the 5-feature composite (`rv_168h, vov, dist_low, xsret7,
xsret3`), applied multiplicatively on the validated continuous config's
existing sizing. Selection, exits, gate, weights: byte-identical.

## Hypothesis

Within-selection ordinal information exists in the entry set (2026-06-12
exploratory: uptrend mid-quintile monthly IC +0.056, t=+5.7, 83% positive
months on the 5-feature composite) and a gross-neutral capped tilt converts it
into MAR without new trades or costs. A cheap Stage-0 falsifier runs before
the full engine run.

## Predicted direction + magnitude

- Stage-1 window: pooled MAR-Δ in (+0.05, +0.3); trade count Δ = 0 by
  construction; both venue returns keep sign.
- Falsifier: mid-quintile IC vanishes on the fresh panel (Stage 0), or the
  Tier-2 bar missed (Stage 1), or post-adoption forward demo/paper degrades
  vs the flat baseline.

## Roots that will be touched

- [x] bybit_full_pit (fresh 5-feature panel rebuild + Stage-1 A/B)
- [x] binance_full_pit (same; funding gaps disclosed as funding-missing)
- [x] forward demo/paper (post-adoption, demo-profile change is operator-gated)

## Decision rule (set before the run)

- **Stage 0 GO/NO-GO:** rebuild the 5-feature panels on the CURRENT rmom
  vintage; GO iff bybit uptrend no-trigger mid-quintile monthly IC mean
  ≥ +0.04 with ≥65% positive months AND binance not sign-opposed. Else: NULL
  receipt, program over.
- **Stage 1 (decisive, Tier-2 bar):** full-engine A/B both venues + 2x-cost
  arm. WIN iff positive total return both venues, pooled MAR-Δ > +0.1 vs
  flat, neither venue MAR-Δ < −0.5, survives 2x cost; fragility diagnostics
  reported, never used to rescue. Anything less: rejected, NULL receipt.
- **Stage 2 (adoption):** a Stage-1 win is proposed to the operator as a
  demo-profile change (demo + paper twin); forward demo/paper accrues the
  live verdict; Tier-3 stays forward-only.

## Run command

```bash
# Stage 0 (panel rebuild + one-shot diagnostic; exact invocation finalized at run time
# via the alpha_sweep in-memory override pattern — feature_set 5-feature, rmom q25):
PYTHONPATH=. .venv/bin/python scripts/alpha_sweep.py --help  # dispatcher; cell spec in the run commit
# Stage 1: engine A/B via the same dispatcher; Stage 2: shadow module (build item).
```

## Post-run results

(fill in after each stage; include report paths + commit SHA)

## Verdict

(pending)
