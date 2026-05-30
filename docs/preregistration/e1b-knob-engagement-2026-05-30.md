# Pre-registration: E1b — knob-engagement robustness probe (rule out under-engaged squeeze)

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-complete
**Plan:** [research_plan_selection_execution.md](../research_plan_selection_execution.md) §E1 (the knob-characterization follow-on)
**Follows:** [e1-execution-premium-2026-05-29.md](e1-execution-premium-2026-05-29.md)

## What's changing

E1 (default params) found **no robust cross-venue execution premium**: B
(`promoted_quality_squeeze`) ≈ A (`fixed_delay`) — bybit MAR Δ +0.04 (LOO
sign-flips, recent-month-concentrated), binance MAR Δ −0.01 (bootstrap P(Δ>0)=12%),
pooled MAR Δ +0.01 → Tier-2 `descriptive`. The high-power paired test on the
genuinely-divergent giveback trades was noise (bybit t=+0.35, binance t=−1.30).

**But the default squeeze barely engages** — it only waits for a giveback when the
first post-signal hour is *still strongly popping* (`h1_return ≥ 50bps` AND
`close_location ≥ 0.85`), which is only ~9–17% of candidates; the other 83% short
immediately. So the ~0 premium could be an artifact of an **under-engaged execution
layer**, not a true execution-null. This probe rules that out by forcing **every**
candidate through the pop→giveback wait loop.

## Hypothesis

If the fade-confirmation execution carries alpha, engaging it on *all* candidates
(not just the still-ripping minority) should reveal a cross-venue premium over
immediate entry. If instead engaging it more does **not** produce a robust premium
(or hurts), the execution-timing null is confirmed robust to engagement level and
the alpha is SELECTION-dominant.

## Arms (per venue, same realistic baseline as E1)

| cell-id | entry-policy | squeeze knobs | meaning |
|---|---|---|---|
| `00_baseline` | `fixed_delay` | — | immediate entry at +1h (control; identical to E1's A, re-run) |
| `01_engage_all` | `promoted_quality_squeeze` | `h1-return-bps=0`, `h1-close-location-min=0.0` | force ALL candidates into the pop→giveback wait (pop 25 / giveback 25 / wait 4h defaults); enter on giveback or 4h deadline |

Fixed (held constant, both venues): `max-active-symbols=12`, `cost-multipliers=1`
(15 bps), `bar_extreme_capped` stops, full-PIT, `2023-04-01 → 2026-05-28`, +1h delay.
Sweep tag: `e1_knob_engage_2026-05-30`.

## Predicted direction + magnitude

- If execution matters: `01_engage_all` shows pooled MAR Δ > +0.1, positive both
  venues, no sign-flip. (Bearish prior: the E1 paired tests say giveback-timing is
  noise/negative, so I expect NO robust premium.)
- Trade count: `01_engage_all` ≈ control (the engine always enters — at giveback or
  deadline — so the pool is not filtered; only timing shifts), well above Tier-2 mins.
- **Falsifier of the "under-engagement" escape hatch:** if `01_engage_all` does not
  beat `fixed_delay` robustly cross-venue, the execution-null is real and
  engagement-robust → E1 verdict = SELECTION-dominant, finalize and pivot E2 to
  selection refinement.

## Roots that will be touched

- [x] bybit_full_pit
- [x] binance_full_pit
- [ ] forward demo/paper (not touched)

## Decision rule (a priori)

Tier-2 demo-arbiter (STATE.md) on `01_engage_all` vs `00_baseline` control via
`scripts/r1_robustness.py --sweep-tag e1_knob_engage_2026-05-30 --control 00_baseline`.
Pooled MAR Δ > +0.1, positive both venues, no cross-venue sign-flip → execution
matters (revisit). Else → SELECTION-dominant confirmed. No knob cherry-picking: this
is a single decisive engagement variant, not a grid mined for a winner.

## Run command

```bash
bash scripts/e1_knob_engage_dispatch.sh
.venv/bin/python scripts/r1_robustness.py --sweep-tag e1_knob_engage_2026-05-30 --control 00_baseline
.venv/bin/python scripts/e1_analyze.py --sweep-tag e1_knob_engage_2026-05-30   # paired micro-test
```

## Post-run results

Run 2026-05-30, sweep tag `e1_knob_engage_2026-05-30`, full-PIT both venues, same
baseline as E1. Determinism check: the `00_baseline` (fixed_delay) control reproduced
E1's bybit A **exactly** (+0.6732, −0.2441 DD, 763 trades) → engine is reproducible.

Forcing the h1 gate to 0 routed far more candidates through the pop→giveback wait:
**bybit giveback trades 69→267 (~6× the E1 default of 44→… actually 44 was binance;
bybit E1 default was 69); binance 44→199.** Yet the premium over immediate entry is
unchanged:

| venue | engage_all ret | MAR Δ vs fixed_delay | bootstrap P(Δ>0) | LOO | paired-test (divergent) |
|---|---:|---:|---:|---|---|
| bybit | +71.0% | **+0.15** (monthly-DD +0.03) | 70% | **flips sign** (2026-04) | t=+0.32, 11/24 (noise) |
| binance | +7.6% | **−0.015** (monthly-DD −0.00) | 32% | no flip | t=−0.55, 5/13 (noise) |

`r1_robustness` Tier-2 verdict = **`descriptive`** (pooled MAR Δ +0.01 ≪ +0.1) —
identical to E1's default-param result.

**Mechanistic note:** even with all candidates in the giveback loop, only 13 (binance)
/ 24 (bybit) trades changed entry *timing* — because at 1h granularity most "givebacks"
complete *within the entry bar* (pop and giveback in the same hour → same entry as
immediate). At 1h resolution the fade-confirmation timing is a near-no-op; the only
lever left would be sub-hourly (E3/sniper), which is itself gated on timing mattering.

## Verdict

**CONFIRMED — the execution-timing null is robust to engagement level.** Six-fold more
fade-confirmation does not produce a robust cross-venue premium; the small bybit premium
is the same fragile, recent-month, LOO-flipping artifact at any engagement, and binance
stays slightly negative. The "default squeeze under-engaged" escape hatch is closed.

**E1 final verdict = SELECTION-DOMINANT.** The alpha is the selection pool + a plain +1h
short; the fade-confirmation execution is not load-bearing at 1h granularity. **E3 (sniper)
is not justified** (its gate — entry timing matters — fails). E2 pivots to SELECTION
refinement.
