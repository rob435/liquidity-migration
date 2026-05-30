# Scope + operator flag: the C0 continuous-engine build (Architecture B)

**Date:** 2026-05-30
**Stage:** EXPLORATORY (scoping note — not a run; no evidence claims)
**Decision owner:** operator (this is a multi-day build; per the hard line I do not start it autonomously)

## What it is

The discrete strategy (Architecture A) fires on a **liquidity-migration event** (rank-climb +
turnover + strong close) to pick candidates. **Architecture B / C0** instead ranks *all*
in-universe names **continuously** (hourly) by a composite of the 5 rolling IC features
(`rv_168h`, `vov`, `dist_low`, `xsret7`, `xsret3`), and trades the tail (short the top
composite, or a beta-neutral decile L/S). The c1/c2 prechecks gated this; the full engine
(emit continuous candidates → realistic capped execution / costs / concentration / full-PIT,
matching the volume-events engine) was estimated at **~5–7 days** of build.

## What we already know (the prechecks)

- **c1 (IC):** the rolling features carry **real but modest** cross-venue IC — `rv_168h`
  ≈ −0.13 @168h (strengthening with horizon), composite ≈ −0.08 both venues. Genuine
  *selection* information.
- **c2 (tradeability):** the naive decile **L/S spread did not clearly survive 15 bps cost**
  on both venues → "not tradeable" as tested.

## How E1/E2 reframe the value (the honest part)

1. **E1 removed one of c2's confounds but not the binding one.** c2's null was about *cost on a
   decile L/S*, not entry timing — and E1 showed entry timing is a non-lever anyway. So E1 does
   **not** obviously rescue C0; c2's cost problem stands until retested.
2. **E2 raises the bar C0 must clear.** The discrete strategy + age gate is now strong
   (bybit MAR ≈ 6–7, binance ≈ 3–6, recent-regime fixed, bootstrap p5>0 on binance age400).
   For C0 to be worth 5–7 days it must beat *that*, not the old daily null. The age-gate
   insight (don't short fresh listings) is **not yet applied to the continuous panel** — that
   is the one genuinely untested, cheap-ish question.

## Recommendation (operator decides)

**Do the cheap test before the expensive build.** Before committing ~5–7 days to C0, re-run the
c2 tradeability precheck **with the age gate** (exclude names younger than ~300 d) and **short-
only** (not just beta-neutral L/S) — both are small edits to `c2_continuous_tradeability_precheck.py`,
not the full engine. If the age-gated continuous short shows a clearly cost-surviving edge on
both venues, C0 becomes worth building; if not, C0 stays a null and we don't spend the week.

**Higher-priority alternative:** the discrete + age-gate refinement is a ready **Tier-2 demo-
candidate now**. The highest-value next step is likely **forward-demo validation of the age gate**
(operator's call to move the demo profile from pit-age≈90 to 300–400) — real OOS evidence that
no in-sample sweep can give — rather than more in-sample architecture exploration.

## RESULT — c2 retest: full-period "flip" is a RECENT-REGIME ARTIFACT (2026-05-30, EXPLORATORY)

Ran `scripts/c2b_continuous_age_precheck.py` (read-only; look-ahead decile characterization;
true PIT listing age). **First read (overstated):** on the full window the age gate flips the
continuous decile short from losing to cost-positive cross-venue @168h (age≥300 short-only net
+29 by / +35 bn bps; even beta-neutral L/S +14/+22) — monotone in the age cut. That looked like
the gate rescuing the continuous architecture.

**The recent-vs-early split refutes the strong claim.** Splitting age≥300 @168h at 2025-06:

| | EARLY 2023-04→2025-06 (~26 mo) | RECENT 2025-06+ (~12 mo) |
|---|---|---|
| bybit short-only net / **L/S net** | −44 / **−14 bps** | +194 / +75 bps |
| binance short-only net / **L/S net** | −18 / **−12 bps** | +161 / +103 bps |

**Even the beta-neutral L/S is negative in the early 26 months** and only positive in the recent
~12. So the continuous age-gated edge is **entirely a recent-regime (2025–26 alt-bear) phenomenon
— substantially short-beta — with no edge in the earlier 2+ years.** The full-period positive was
the recent bear dominating the average. This is **not** a robust, all-weather edge.

**Corrected verdict:** the cheap retest is a **regime-conditional positive at best — it does NOT
justify the multi-day C0 build.** Building a continuous engine on a recent-bear-only signal would
chase a regime, not an edge. (Contrast the *discrete* E2 strategy, which is all-thirds-positive
**including the early period** — that remains the robust finding.) This corrects an earlier
over-claim in this note + commit a56e918.

## Ask

Operator: **C0 is not recommended on current evidence** (regime-conditional, short-beta-heavy).
The robust, all-weather result is the **discrete age gate** — the highest-value step is your call
on whether to **move the demo profile to the age gate (pit-age ~90 → 300)** for forward OOS
validation. I will not build C0 or change the demo profile without an explicit go. (If you still
want the continuous angle explored, the honest next test is a *regime-neutral* continuous study,
not the C0 build.)
