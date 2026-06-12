# Pre-registration: E2 — BTC-trend regime-response family (3 pre-named variants)

**Date:** 2026-06-12
**Author:** operator + Claude (round-4 session)
**Stage:** proposed
**Plan:** [research_plan_composite_sizing_2026-06-12.md](../research_plan_composite_sizing_2026-06-12.md)

## What's changing

The binary BTC-30d-uptrend entry gate is compared against TWO pre-named
bounded alternatives (thresholds fixed up front):

- **V0** baseline: on iff `trend > 0` (current live gate).
- **V1** euphoria cap: on iff `0 < trend ≤ +0.20`.
- **V2** soft 3-state: `> +0.20` off; `0 < trend ≤ +0.20` full size;
  `≤ 0` quarter-size, top-composite-quintile entries only.

## Hypothesis

2026-06-12 exploratory bucket study (F5 in the plan): the fade book's mean is
NEGATIVE in the euphoria bucket the live gate currently trades (>+20%:
−136bps/24h, 29 episodes) and positive-but-clustered in deep crashes; the
response is non-monotone, so only a bounded family (not a score/curve) is
testable. Catastrophe days are uniform across buckets — this is a MEAN
question; disaster control stays with stops/caps. Funding (unmodeled in the
exploratory) pushes against both tails, so the registered run models funding.

## Predicted direction + magnitude

- V1: small pooled MAR improvement vs V0 (euphoria removal), trade count −10%
  to −20%.
- V2: MAR ≈ V0 ± noise with higher trade count; most likely casualty.
- Falsifier: V1 and V2 both miss the Tier-2 bar at Stage 1 — the binary gate
  stands.

## Roots that will be touched

- [x] bybit_full_pit (Stage-1 family run, funding ON — mandatory)
- [x] binance_full_pit (same; funding-missing label where the root lacks it)
- [x] forward demo/paper (Stage-2 shadow incl. pre-gate candidate evaluation;
      zero order impact)

## Decision rule (set before the run)

- **Sequencing:** Stage 1 may not start before E1's Stage-1 verdict is filed.
- **Stage 1 (decisive, Tier-2 bar vs V0):** full engine, all three variants,
  both venues, funding ON (mandatory), + 2x-cost arm. A variant WINS iff
  positive total return both venues, pooled MAR-Δ > +0.1 vs V0, neither venue
  MAR-Δ < −0.5, survives 2x cost. Episode counts (~29 euphoria / 24
  deep-crash, clustered) are REPORTED as the fragility diagnostic — disclosed,
  never used to rescue. Both variants miss: V0 stands, NULL receipt filed.
- **Stage 2 (adoption):** the winning variant (higher pooled MAR-Δ if both
  pass; no second look, no blending) is proposed to the operator as a
  demo-profile change; forward demo/paper accrues the live verdict; Tier-3
  stays forward-only. No new variants, no threshold adjustment, ever — a
  different idea requires a new pre-registration.

## Run command

```bash
# Stage 1: engine family run via the alpha_sweep dispatcher (cell spec recorded in
# the run commit; funding ON; both venues). Stage 2: shadow build item
# (pre-gate candidate evaluation, dynexit-shadow pattern).
```

## Post-run results

(fill in after each stage; include report paths + commit SHA)

## Verdict

(pending)
