# Breadth redesign — many small independent bets

Registered 2026-07-20. Statistical power is the binding constraint on this
whole enterprise: forward evidence accrues at one day per day, and the
deployed book takes on the order of **one bet per day** (33 journal fills
across the whole current demo record; CONTINUOUS funnel on 2026-07-20:
36 D9 candidates → 9 after liquidity → 0–3 after event/age/capacity;
LONG holds ≤ 5 concurrent event positions). No governance, tooling, or
engineering improves that number — only breadth does.

## The arithmetic (scripts/breadth_power.py)

Days to a t = 2 read on the book mean, per-bet vol 300 bps, average same-day
pairwise correlation ρ = 0.15:

| bets/day | edge 15 bps | edge 25 bps |
| --- | --- | --- |
| 1 | 4.4 years | 1.6 years |
| 5 | 1.4 years | 0.50 years |
| 10 | 1.0 years | 0.37 years |
| 20 | 0.84 years | 0.30 years |
| 40 | 0.75 years | 0.27 years |

Two design conclusions fall straight out:

1. **Getting from ~1 to ~10 bets/day is worth more than every other
   improvement combined.** It turns "years to know anything" into "quarters
   to know something."
2. **Correlation is the ceiling.** At ρ = 0.15 the annualized book Sharpe
   saturates near 2.3× the per-bet ratio regardless of N; beyond ~20
   bets/day the only gains come from *decorrelated* bet sources (different
   mechanisms, horizons, or sides), not more of the same fade.

## Levers, in order

1. **Per-component notional floor + weight re-normalization** (from
   `docs/forward_record_annotations.md`): drop sub-floor components and
   re-normalize weights so every admitted bet is measurable. Prerequisite
   for everything else — more bets at 28 USDT each would multiply
   quantization noise, not evidence.
2. **CONTINUOUS admission breadth.** The funnel loses 36 → 9 at the
   liquidity stage and most of the rest at event/age gates. Candidate knobs
   (all strategy-relevant, all Lane-1 first): the liquidity threshold, the
   event/age gate widths, `max_new_entries_per_cycle` (5), and decile
   broadening (D8+D9 with decile-relative weighting). `max_active` is
   already 25 and is not the binding constraint.
3. **Decorrelated bet sources** (the ρ lever, larger project): the same
   cross-section traded at a second horizon, the long side of the
   distribution, or the passive-execution flow itself (execution alpha has
   near-zero correlation to signal alpha). The hypothesis-ledger discipline
   applies in full: these are new families, counted as such.
4. **LONG is exempt.** It is a rare-event sleeve by design (≤ 5 concurrent,
   sniper-retrace entries); its kill criteria (K3) already handle the
   consequence — if the event rate cannot validate the sleeve within two
   epochs, it retires for capacity reasons. Breadth is not extracted from a
   strategy whose mechanism does not have it.

## Candidate config: `continuous_breadth_v1` (not yet committed)

One registered change bundling lever 1 + the mildest of lever 2: component
notional floor at 4× venue minimum with re-normalization, liquidity
threshold relaxed one notch, `max_new_entries_per_cycle` 5 → 10, targeting
**≥ 8–10 admitted bets/day** at unchanged gross (smaller per-bet size at
constant `per_position_notional_pct_equity` × more positions). Per the
Progressive Evidence Model this needs a Lane-1 pass on the research
environment first (funnel replay on seen data to verify the admitted-count
and correlation estimates), then the config commit is its registration and
its forward record starts. The hypothesis ledger records it as a
sixth-generation read of the spent surface.

## What this does not claim

Breadth multiplies the *rate of learning*, not the edge. If the fade has no
edge, a 10-bet/day book discovers that in ~1 year instead of never — which
is precisely the point of the kill criteria it feeds.
