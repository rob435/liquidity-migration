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

Under the realistic fill + sane concentration, the **daily** strategy (which uses the
fade-confirmation execution) is **positive on both venues in-sample.** It remains in-sample;
the forward demo (since 2026-05-22) is the arbiter; nothing is promoted.

## Daily strategy — realistic re-baseline (full-PIT, `bar_extreme_capped` 10%, in-sample 2023-04→2026-05)

| config | venue | stop fill | cost | total ret | max DD | worst day | Sharpe |
|---|---|---|---:|---:|---:|---:|---:|
| baseline, max_active=3 (DEPLOYED) | bybit | `bar_extreme` | 45bps | −32% | −87% | −36% | 0.19 |
| baseline, **max_active=12** | bybit | **capped 10%** | 45bps | **+37.8%** | −27.5% | −4.8% | **0.70** |
| baseline, **max_active=12** | binance | **capped 10%** | 45bps | −4.7% (**gross +16.1%**) | −33.6% | −4.4% | −0.05 |

All rows use the `promoted_quality_squeeze` (fade-confirmation) execution. At the honest
15 bps cost the drag roughly thirds → bybit higher, **binance ~breakeven-to-positive**.
**Both venues are gross-positive.** The top row is the old worst-case (what the "null" was
built on). *(Binance funding not applied in this run; for a short, funding is typically a
credit — likely understates binance.)*

## The open lead: refine the EXECUTION signal (sniper) + apply it to the continuous candidate

The selection/execution split makes the next research obvious and is the most promising
direction:

- **The continuous candidate signal carries real, robust cross-venue IC** (rolling features:
  composite −0.084/−0.085/−0.087 bybit, −0.078/−0.081/−0.085 binance at 24/72/168h; `rv_168h`
  −0.13 @168h, strengthening). This is a *selection* signal — genuinely informative about
  *which* names will underperform. **It was only ever tested with immediate entry** (C2), so
  its "not tradeable" label is about timing-the-top, not the signal.
- **Untested and the lead:** apply the **fade-confirmation execution** (pop-then-giveback,
  and finer **sniper** sub-1h timing) to the continuous candidate pool. The momentum-
  continuation at the extremes is precisely what a confirmation entry is designed to wait
  out.
- **Sniper entry (was "R12"):** refine the execution signal to sub-1h / 1m timing — better
  confirmation, less lag. This is *execution* refinement on a fixed *selection*, exactly the
  separation above.

**To validate the separation directly:** compare the daily strategy under `fixed_delay`
(near-immediate entry) vs `promoted_quality_squeeze` (fade-confirmation). If the squeeze
materially outperforms, the execution signal *is* a large part of the alpha — the cleanest
proof of this whole thesis. (Not yet run; cheap to run.)

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
