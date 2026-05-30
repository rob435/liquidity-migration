# Research summary — liquidity-migration strategy

**Updated 2026-05-29.** Single consolidated research record (replaces the deleted Round 1 +
Round 2 per-phase docs; originals in git history). Methodology standard:
[backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).

## The core distinction: SELECTION signal vs EXECUTION signal

The strategy has two separable layers, and conflating them caused the wrong Round-2
conclusions:

1. **Selection (initial signal) — *which* names are candidates.** A liquidity-migration
   event: a mid-liquidity perp (rank 31–400, top-30 excluded) takes in price-insensitive
   flow — turnover ≥6×, ≥150-place rank climb, modest residual return ≥8%, strong close,
   top-10% of events excluded. This identifies a *candidate pool*. It is **not** an entry.
2. **Execution (entry signal) — *when* to short.** The thesis is the in-migrated flow
   **exhausts and fades**. You do **not** short at the top (the pump can continue). You wait
   for the fade to **confirm**, then enter — "**fade the fade**." The deployed execution
   (`promoted_quality_squeeze`, volume_events.py:1003–1036) already does this: track the
   post-event high, require a **pop**, then enter the short on the **giveback** from that
   high. Selection ≠ execution.

**Why this matters / the bias we removed.** The momentum-continuation finding (the extreme
post-event names *continue up* +26/+39 bps before fading) is **not** evidence the strategy
fails — it is the **reason the execution signal must wait for confirmation**. Immediate
entry shorts into that continuation and gets run over; a fade-confirmation entry sidesteps
it. The old "fade-the-pump short thesis fails" root-cause was an artifact of testing
immediate entry.

## Current status (honest)

The earlier **"Round 2 = documented null"** was substantially wrong, for two compounding
reasons:
- **Methodology (pessimistic execution model):** worst-case `bar_extreme` stop fills +
  `max_active=3` over-concentration + a ×3 (45 bps) cost. Fixed: default is now
  `bar_extreme_capped` (10%), and the validated config is `max_active=12`.
- **Selection/execution conflation:** the continuous test (C2) used *immediate* entry, never
  the fade-confirmation execution.

Under the realistic fill + sane concentration, the **daily** strategy is **positive on
both venues in-sample.** It remains in-sample; the forward demo (since 2026-05-22) is the
arbiter; nothing is promoted.

## E1 (2026-05-30): the EXECUTION signal is NOT load-bearing — the alpha is SELECTION

The E1 experiment ([preregistration/e1-execution-premium-2026-05-29.md](preregistration/e1-execution-premium-2026-05-29.md)
+ the E1b knob-engagement robustness probe) tested the thesis directly: on the *same*
selection pool, costs, and concentration (full-PIT, capped10, max_active=12, 15 bps,
both venues, 2023-04→2026-05), vary only `--entry-policy`: A `fixed_delay` (immediate
+1h) vs B `promoted_quality_squeeze` (fade-confirmation).

| venue | A immediate (fixed_delay) | B fade-confirm (quality_squeeze) | premium B−A |
|---|---:|---:|---:|
| bybit | **+67.3% / MAR 2.76 / Sh 1.07** | +71.2% / MAR 2.93 | +0.17 MAR (LOO-flips, recent-only) |
| binance | **+8.0% / MAR 0.28** (funding-missing) | +7.3% / MAR 0.25 | −0.03 MAR (sign-flip) |

**Verdict: SELECTION-DOMINANT.** The fade-confirmation execution adds nothing robust —
pooled MAR Δ ≈ +0.01 (Tier-2 `descriptive`), it sign-flips across venues, the bybit
premium is a fragile recent-month artifact (LOO flips the sign), and the high-power
paired micro-test on the genuinely time-divergent trades is pure noise (bybit t=+0.35,
binance t=−1.30). The E1b probe forced 6× more engagement (bybit giveback trades 69→267)
and the premium was unchanged → robust to engagement level. **The alpha is the SELECTION
pool + a plain +1h short.** Two reasons the execution layer is inert: (a) `promoted_quality_squeeze`
never *filters* the pool (A and B trade the identical candidates), and (b) at 1h granularity
most "givebacks" complete inside the entry bar, so the timing barely moves (only ~3–9% of
entries differ from immediate). **`promoted_quality_squeeze` ≈ immediate entry in practice.**

**What this corrects:** the 2026-05-29 retraction was right that the old null was a
worst-case-fills + over-concentration artifact (E1 confirms the strategy is strongly
positive — bybit +67% at honest 15 bps). But its framing that the strategy is a SELECTION
*and* an EXECUTION (fade-confirmation) signal — and that execution is the open lead — is
**not supported**. Execution timing is a non-lever at 1h; E3 (sniper) is therefore not
justified (its gate fails). **The open lead is SELECTION refinement.**

## Daily strategy — realistic re-baseline (full-PIT, `bar_extreme_capped` 10%, in-sample 2023-04→2026-05)

| config | venue | stop fill | cost | total ret | max DD | worst day | Sharpe |
|---|---|---|---:|---:|---:|---:|---:|
| baseline, max_active=3 (DEPLOYED) | bybit | `bar_extreme` | 45bps | −32% | −87% | −36% | 0.19 |
| baseline, **max_active=12** | bybit | **capped 10%** | 45bps | **+37.8%** | −27.5% | −4.8% | **0.70** |
| baseline, **max_active=12** | binance | **capped 10%** | 45bps | −4.7% (**gross +16.1%**) | −33.6% | −4.4% | −0.05 |

All rows use the `promoted_quality_squeeze` (fade-confirmation) execution. At the honest
15 bps cost the drag roughly thirds → bybit higher, **binance ~breakeven-to-positive**.
**Both venues are gross-positive.** The top row is the old worst-case (what the "null" was
built on). *(Binance funding is **missing** in these runs — `binance_full_pit` has no funding
dataset wired; label `funding-missing`. Correcting an earlier note: this does NOT understate
binance — E1 shows bybit short funding is a net **−6.2% drag** (modeled), not a credit, so
adding funding would if anything pull binance **down**. The cross-venue gap is real, not a
funding artifact.)*

## The open lead (post-E1): SELECTION refinement, not execution

E1 closed the execution question (above): timing is a non-lever, so E2/E3 pivot away
from execution. The plan ([research_plan_selection_execution.md](research_plan_selection_execution.md))
E1→E2→E3 sequence stands, but with E1's contingency triggered — E2 becomes a **selection
refinement** study, and E3 (sniper) is dropped (its gate, "entry timing matters," failed).

Two concrete selection leads:

- **E2 RESULT (2026-05-30): the age gate is a robust cross-venue refinement.** Testing an
  exhaustion-quality gate ([preregistration/e2-exhaustion-selection-2026-05-30.md](preregistration/e2-exhaustion-selection-2026-05-30.md)),
  the winner is **`pit-age-days-min=300` (drop symbols younger than 300 days)**: daily-DD MAR
  **bybit +2.93→+5.96, binance +0.25→+2.81** (return up, DD down on both), all-thirds-positive
  both venues, LOO-stable, bootstrap P(Δ>0) 83%/93%. It **improves the recent weak third on
  both venues** (bybit −2%→+25%, binance −26%→+4%) — refuting the age→early-regime confound and
  **explaining the recent edge-decay** (young-name shorts are systematic net losers — fresh
  listings squeeze; the signal works on seasoned names; verified cross-venue on the ledgers).
  `prior30-max-return-max=0.14` is a secondary cross-venue risk-reducer (halves DD).
  `universe-rank-max=110` (liquidity-tighten) was **rejected** — it hurt both venues (the
  `liquidity_rank` IC was a within-realized artifact the backtest caught). Note: OI / taker /
  funding features were **excluded** — they are NaN on `binance_full_pit`, so the earlier
  exploratory IC's binance derivative-feature cluster was an artifact. **Tier-2 demo-candidate,
  in-sample** — forward demo is the arbiter; deployment/profile change is the operator's call.
- **The continuous candidate signal carries real cross-venue *selection* IC** (rolling
  features: composite −0.084/−0.085/−0.087 bybit, −0.078/−0.081/−0.085 binance at 24/72/168h;
  `rv_168h` −0.13 @168h). It is a *selection* signal (which names underperform). Its c2
  "not tradeable" label was an immediate-entry test — but E1 shows immediate entry is fine,
  so the open question is whether the continuous selection beats the discrete event selection
  under the *same plain +1h short*, not whether an execution layer rescues it.

**Cross-venue asymmetry is the standing caveat:** bybit MAR 2.76 vs binance 0.28 (and
binance is funding-missing, i.e. optimistic). The edge is also front-loaded (recent third
much weaker). Any selection refinement must narrow this gap / hold up recently, not just
reload the early bybit regime.

## Useful findings worth keeping

1. **Concentration is the deployed config's main risk.** `max_active` 3→12 cuts worst-day
   −36%→−4.8% and DD −87%→−27.5%. The demo runs 3; research-validated is 12. **Move it.**
2. **Stop-fill assumption** dominated the old verdict: `bar_extreme` (worst-case wick) vs a
   10% cap swung the deployed curve −32% → +479% (concentration-amplified). Default is now
   `bar_extreme_capped` 10% (realistic bad-case), calibratable from demo fills.
3. **Component winners** (daily): `risk_equal` 2% sizing (de-concentrates, cuts DD),
   `ff6_4pct` failed-fade exit (best loss-cutter), `drop_all_4` filter set.
4. **Pre-2023 is structurally untradeable** (bybit had 7–182 symbols; rank-31–400 + ≥150
   rank-climb needs the 400+ universe that only existed from ~mid-2024). There is **no
   internal OOS root** — pristine OOS is the forward demo (see [data_roots.md](data_roots.md)).

## Methodology lessons

Engine hardening (2026-05-29) toward honesty was correct in direction: optimistic→honest
stop fills, 100% taker, calendar-exact returns, real promotion gates, full-PIT survivorship.
**The over-correction** was making worst-case `bar_extreme` the *default* (too brutal on 1h
alt wicks — real stop slip median +2.3%, but it assumed wick-tops to +89%). Fixed to a 10%
cap. **The deeper lesson:** never test a strategy with a single hard-coded execution; the
selection signal and the entry signal must be evaluated separately, or you measure the
execution's flaws and blame the signal.

## Was "Round 2 = null" right?

**No.** It was a worst-case execution model *and* a selection/execution conflation. The
daily strategy (with fade-confirmation execution) is positive on both venues in-sample. The
continuous selection signal is real and never got an execution layer. **The strategy is not
dead.** Open work: (a) move the demo to `max_active=12` + capped fills; (b) the
`fixed_delay` vs `quality_squeeze` test to quantify the execution signal's contribution;
(c) apply + refine the execution (sniper) on the continuous candidate pool; (d) forward-demo
confirmation is the arbiter. Any of these is a fresh, dated pre-registration.

## Provenance

Round 1 + Round 2 plans and per-phase verdicts (phase0–6, R1–R13, C0–C3) were consolidated
here and deleted 2026-05-29; originals in git history. Engine/methodology change receipts
are in the git commit log. Backtest artifacts live under the data roots
([data_roots.md](data_roots.md)).
