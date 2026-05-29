# C0–C3 — Architecture B (continuous signal) — VERDICT: DOCUMENTED NULL (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phases C0–C3.
**Run label:** full-PIT, in-sample 2023-04→2026-05, hourly (`klines_1h`). Read-only IC /
tradeability pre-checks (no strategy-P&L promotion claim).
**Tools:** `scripts/c1_continuous_ic_precheck.py`, `scripts/c2_continuous_tradeability_precheck.py`.
Artifacts `~/SHARED_DATA/c1_continuous_ic_precheck_2026-05-29.json`,
`c2_continuous_tradeability_precheck_2026-05-29.json`.

## Headline

**Architecture B (the continuous / higher-frequency formulation of the
liquidity-migration signal) is a DOCUMENTED NULL.** The rolling features carry a *real
but weak* cross-venue signal at the FEATURE level — but it is **non-monotonic, wrong-signed
at the tradeable extremes, and cost-dominated at every horizon**, so no tradeable form
clears the bar. The full C0 continuous-engine build (~5–7 days) is **not warranted**: the
pre-checks are decisive (a position-lifecycle engine only adds frictions; it cannot make a
wrong-signed, cost-dominated cross-sectional signal profitable). **With Architecture A
(daily) also null (R9), the entire Round-2 program concludes at DO NOTHING** — the frozen
promoted profile is unchanged.

## C1 — continuous feature IC (REAL, feature-level)

Hourly rolling versions of the 5 Phase-5 IC features vs forward returns {1,3,24,72,168}h,
rank IC (per-ts cross-sectional Spearman, averaged), both venues:

| feature | bybit 24h / 72h / 168h | binance 24h / 72h / 168h |
|---|---|---|
| rv_168h (realized vol) | −0.093 / −0.108 / **−0.128** | −0.084 / −0.101 / **−0.126** |
| vov (vol-of-vol) | −0.059 / −0.070 / −0.086 | −0.051 / −0.064 / −0.084 |
| xsret3 / xsret7 (momentum) | ~−0.04 | ~−0.04 |

The features have genuine, cross-venue-consistent **negative IC** (high feature → lower
forward return), strengthening with horizon — the first *unconditional* signal the program
surfaced (the daily strategy's apparent edge was conditioned on the event trigger and
turned out to be a stop-fill artifact). **But IC ≠ tradeable** (cf. Round-1 Phase-6, where
daily-combined features lost).

## C2 — tradeability: NOT tradeable (non-monotonic, wrong-signed extremes, cost-dominated)

Composite = mean of the 5 features' per-ts cross-sectional ranks (high = strong short).
Within-timestamp decile L/S (long bottom-composite, short top-composite), averaged over
timestamps — the market-neutral form that strips the alt-beta that sank the daily
short-only strategy:

| | bybit 24h / 72h / 168h | binance 24h / 72h / 168h |
|---|---|---|
| L/S spread per hold (gross) | −1 / **−17** / **−27** bps | −2 / **−12** / **−37** bps |
| L/S net per hold (− 30 bps cost) | −31 / −47 / −57 bps | −32 / −42 / −67 bps |
| composite IC (per-ts Spearman) | −0.084 / −0.085 / −0.087 | −0.078 / −0.081 / −0.085 |

**Decile forward-return profile [D0..D9], 168h (bps):**
- bybit: −1 +12 −3 −1 +5 +8 +12 +2 +7 **+27**
- binance: +2 −3 −2 −11 −0 −7 +10 +7 +2 **+39**

The extreme top decile **D9 (highest composite = strongest short signal) RALLIES the
hardest** (+27 / +39 bps) — the high-vol/extended/momentum names *continue up*. A short
strategy shorts exactly those names → loses. The weak negative composite IC (−0.087) is a
noisy bulk tendency that does **not** manifest as shortable underperformance (D4–D8 are
mostly positive too). Net L/S is cost-dominated at every horizon; the only favorably-signed
horizons (1h/3h) are +1 bps gross — far below the ~30 bps round-trip cost.

## Root cause (unifies the whole program)

The liquidity-migration **short** thesis is "fade the volume-spike pump." Across every
test — daily filters (R1), daily integrated stack (R9), within-event IC (R9 pre-check),
and now the continuous cross-section (C1/C2) — the names the strategy selects (extreme
high-vol / extended / high-momentum) exhibit **MOMENTUM CONTINUATION, not reversion**,
especially at the extremes that any threshold / decile / event trigger picks. The negative
IC that exists is a weak bulk tendency overwhelmed by the wrong-signed extremes and is not
tradeable after honest costs. This single mechanism explains the daily null, the R9
within-events anti-selection, and the continuous null.

**Post-hoc note (NOT promotable):** the consistently-rallying D9 implies a *momentum*
(continuation) thesis — the OPPOSITE of the fade thesis. That is a different strategy, a
post-hoc observation here (error #17), and would require a fresh OOS pre-registration; it
is NOT promoted.

## Methodology integrity

- Full-PIT (`klines_1h` from the `*_full_pit` roots); rolling features strictly backward.
- **Caught + corrected a Simpson's-paradox time-confound:** the first C2 pooled decile
  means across timestamps, which conflated the cross-sectional signal with the alt-bull
  regime (the vol composite is high-prevalence in high-return periods). The honest metric
  is the *within-timestamp* L/S (above); both the pooled and within-ts versions agree the
  L/S is negative, but only the within-ts version is methodologically valid.
- C0 engine intentionally NOT built: the C1/C2 pre-checks are decisive on tradeability;
  building a ~5–7d continuous backtest engine to confirm a wrong-signed, cost-dominated
  signal would burn compute on a determined outcome (the engine only adds frictions).

## DECISION

- **Architecture B (C0–C3): DOCUMENTED NULL — not tradeable.** No continuous formulation
  (cross-sectional L/S, short-extreme, or event-driven-continuous = the failed daily R9)
  clears the bar.
- **Round 2 program COMPLETE.** Both architectures (A daily, B continuous) are documented
  nulls under honest methodology. **DO NOTHING** — the frozen promoted demo profile is
  unchanged; nothing is promoted to demo-candidate or real money.
- C0 continuous engine, R12 sniper: not built (both moot given the signal is not tradeable
  / cannot create the absent edge).

## Next

None within the pre-registered Round-2 scope — the program is complete at a documented
null. Any future work (a momentum-continuation thesis from the D9 observation; a
bybit-only daily strategy from the R9 single-venue edge) would be a NEW pre-registration
and an explicit operator decision, not a continuation of this program.
