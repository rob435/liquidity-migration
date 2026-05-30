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

## RESULT — the cheap age-gated c2 retest PASSED (2026-05-30, EXPLORATORY)

Ran `scripts/c2b_continuous_age_precheck.py` (read-only; look-ahead decile characterization;
true PIT listing age; not a backtest). The age gate **flips the sign** of the continuous
decile short:

| venue / 168h | baseline D9 fwd → short-net | **age≥300** D9 fwd → short-net | beta-neutral L/S net (base → age) |
|---|---|---|---|
| bybit | +27 → **−42 bps** (loses) | **−44 → +29 bps** (wins) | −58 → **+14 bps** |
| binance | +39 → **−54 bps** (loses) | **−50 → +35 bps** (wins) | −67 → **+22 bps** |

Without the gate the top-composite decile **rallies** (the young-name momentum-continuation
that sank c2). With the gate it **fades**, and the short is **cost-positive cross-venue at
168h** — even the **beta-neutral L/S** is positive. The young-name squeeze poisoned *both* the
discrete (E2) and continuous (c2) signals; they are the same truth. (72h is ~breakeven — the
continuous edge is a **weekly/168h** phenomenon, distinct from the discrete 3-day hold.)

**Revised recommendation:** the gate that c2 failed now **passes**, so **C0 is justified** —
but as a *weekly* continuous short on the age-gated, top-composite names. EXPLORATORY look-ahead
evidence only (no execution/capacity/funding) — it warrants the build, it is not itself a P&L.

## Ask

Operator: (a) **greenlight the C0 build** (~5–7 d) now that the age-gated precheck passes — a
weekly continuous short on seasoned, top-composite names with realistic execution/costs/capacity;
and/or (b) decide whether to move the demo profile to the age gate for forward validation of the
*discrete* strategy. I will not build C0 or change the demo profile without an explicit go.
