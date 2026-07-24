# Basket-short tail experiment — registered 2026-07-25

Paper-only A/B on the CONTINUOUS sleeve. **Authorizes nothing**: no runtime
change, no demo exposure change, no mainnet, no `REAL_MONEY`. Registered under
`docs/governance.md`; the commit is the registration.

## Claim under test

> Replacing CONTINUOUS's idiosyncratic short leg with an equal-notional basket
> short removes roughly half of the book's tail while giving up under 5% of its
> mean return.

## Why this and not something else

Three independent measurements point at the same mechanism:

1. The audit measured that a short book in this universe takes **22.4% of its
   losses in 1% of trades**, that the tail is **~95% idiosyncratic**, and that
   removing market beta relieves only **5%** of it. The tail is single-name, so a
   book-level beta hedge cannot reach it.
2. The deployed LONG sleeve is **long-only** and has a max drawdown of
   **−4.11%**. Same universe, same era, no short leg, no tail.
3. A direct structural test on a cross-sectional proxy
   (`docs/anomaly_research_2026-07-24.md` §11.2), same signal throughout, only
   the short leg's construction varying:

| variant | bp/day | Sharpe | worst day | worst 1% | max DD |
| --- | ---: | ---: | ---: | ---: | ---: |
| short the decile (control) | 39.55 | 1.20 | −35.37% | −20.99% | 83.6% |
| **short an equal-notional basket** | 38.62 | **1.55** | **−17.50%** | **−12.12%** | **64.4%** |
| decile minus "crowded" names | 37.82 | 1.13 | −43.63% | −23.91% | 86.5% |
| half decile, half basket | 39.09 | 1.40 | −25.82% | −15.26% | 73.2% |

A crowding screen on funding percentile made the tail **worse**, so the
experiment does not include one.

## Arms

Allocation is by **component-signal hash parity**, fixed at signal time, so the
assignment is deterministic, reproducible from the journal, and cannot drift with
market state. This mirrors the allocation already used by
`passive_execution_experiment_2026-07-20.md`.

| Arm | Long leg | Short leg |
| --- | --- | --- |
| **A (control)** | unchanged | unchanged: per-name short of the selected components |
| **B (treatment)** | unchanged | a single basket short of equal notional to arm A's short leg |

Arm B's basket is the **turnover-weighted top-N of the same eligible universe**,
not BTC and not an index product — the point is to hold the same market exposure
with no single-name concentration. If the venue cannot express the basket inside
existing per-symbol minimums, arm B falls back to the smallest set of names that
reproduces the target notional within tolerance, and that fallback is journaled.

**Everything else is held constant**: entry signal, component selection, sizing,
BTC gate, hedge, protection, and exit logic. If any of those change during the
experiment, the run ends and a new registration starts.

## Primary metrics

The claim is about tail, not mean, so tail statistics are primary:

1. **Worst single day** per arm.
2. **Mean of the worst 1% of days** per arm.
3. **Share of total loss concentrated in the worst 1% of days**.
4. **Compounded maximum drawdown.**

Secondary, to confirm the mean is not quietly destroyed: net bp/day, annualised
Sharpe, hit rate.

Every figure is computed on the canonical account journal, net of **realised**
fees, not modelled ones — see the cost note below.

## Cost basis

The research books assume a 4 bp maker round trip. Measured on 85 forward demo
fills from the 2026-07-22 archived journal, the realised rate is **7.78 bp per
side, notional-weighted — a 15.56 bp round trip, 3.89× the assumption**, with a
median fill at 11.00 bp/side. Fills are being priced as **taker**, not maker.

All experiment reporting uses realised journal fees. No arm may be compared
against a modelled-cost number.

## Sample target and stopping

- **Target: 60 attributed round trips per arm**, or 90 days, whichever comes
  first. The tail metrics need the tail, and a smaller sample cannot see it.
- **No peeking at the primary metrics before the target.** Operational health,
  fill correctness, and arm-assignment integrity may be inspected at any time;
  the tail comparison may not.
- **Early stop for harm only**: if arm B's cumulative net falls more than 400
  USDT below arm A's, stop and report. This is a safety stop, not a result.
- The experiment is **paper-only**. It does not run on the demo account and
  cannot change demo exposure.

## What would falsify the claim

- Arm B's worst 1% is not materially better than arm A's (< 20% improvement).
- Arm B's mean net falls more than 15% below arm A's — the tail relief would then
  be bought with return, not free.
- Arm B's basket cannot be executed within venue minimums often enough to keep
  the arms comparable (tracked as a fallback rate; > 30% invalidates the run).

## Known limitations, stated up front

- The §11.2 evidence is a **cross-sectional proxy**, not the CONTINUOUS component
  book. The proxy could mislead if CONTINUOUS's short leg has a different name
  distribution. This experiment exists precisely because the proxy is not proof.
- Paper is `integration_only_uncalibrated`. It measures routing, lifecycle, and
  relative tail shape between two arms that share an environment — not absolute
  performance.
- 60 round trips per arm is enough to see a drawdown difference and **not** enough
  to resolve a small mean difference. The mean is a guardrail here, not a result.
- A basket short converts idiosyncratic risk into market risk. That is the point,
  but it means arm B carries beta the control does not, and the existing hedge
  must be checked for double-counting before the run starts.
