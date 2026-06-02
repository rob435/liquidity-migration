# What the continuous sleeve should inherit from the daily system

**Date:** 2026-06-01 · operator-directed ("this system is very barebones, not as sophisticated").
**Status:** design analysis — a prioritized research/engineering backlog, NOT yet built. Each idea is a
hypothesis to validate in the continuous *engine* (`continuous_events.py`) BEFORE it is wired to the demo
sleeve. Nothing here changes the live profile until backtested + operator-approved.

## The thesis (why "barebones" is exactly right)

Both systems short the same object — a pumped alt that fades. But the **daily system's ~85
`VolumeEventResearchConfig` gates are not the signal; they are a learned squeeze-avoidance apparatus**
built up over the whole research arc. The daily strategy spent that arc discovering *which* pumps are safe
to short (seasoned names, post-confirmation, not-crowded, not-pumping-against-a-weak-market, volatility-sized)
and which ones squeeze your face off. The raw "short the extreme mover" idea was always there; the
sophistication is everything wrapped around it that stops you shorting the continuers.

The **continuous sleeve shorts the raw top composite decile with almost none of that defense.** That is
literally the audit's #1 unresolved live risk (intraday squeeze) and #2 (borrow/availability). So the
inheritance priority is overwhelmingly **squeeze-defense + risk-sizing**, not signal. The disaster stop we
just added (server-side, 25%) is the crudest backstop; the inheritances below are the sophistication that
lets the book *not reach* that stop as often.

## What the continuous sleeve already has

rmom-low gate · liquid-turnover gate (analog of the rank band) · +1h entry leakage guard · `max_active`
concurrency cap · a state exit (leave-the-decile = the daily's `rank_exit`/`event_decay`) · a 48h max-hold ·
the just-added wide server-side disaster stop. That is the skeleton. The flesh is missing.

## Inheritance catalog (grouped; ⚠️ = squeeze-defense)

### A. Position sizing — the single highest-value, lowest-risk inheritance
- **⚠️ Risk-equal / inverse-vol sizing** (daily: `position_weighting="risk_equal"`, `target_vol_per_name`).
  The continuous book is flat **2% per name**, so the wildest pumped alt (highest realized vol) carries the
  most dollar risk and dominates the drawdowns — the exact names most likely to squeeze get the *largest*
  weight. Sizing each short by its volatility (equal risk contribution, clamped) is what the daily does and
  is a direct, well-understood risk-quality upgrade. **Priority: HIGHEST.** Cheap (the engine already has the
  `_PositionSizer`), and it attacks the squeeze tail at the sizing layer, not just the stop layer.

### B. Selection gates — stop shorting the names that squeeze (⚠️ all)
- **⚠️ Age floor** (daily: `pit_age_days_min=300`). Continuous P0 dropped age for rmom *for the signal*, but
  for **live squeeze-defense** young/fresh listings are the canonical squeezers (thin float, listing pumps).
  Re-adding an age floor (or at least excluding <30–90d listings) is cheap insurance the continuous sleeve
  entirely lacks. **Priority: HIGH.**
- **⚠️ Momentum-deceleration / "don't short a name still accelerating up"** (the i1b finding: velocity &
  acceleration separate faders from continuers). A D9 name still ripping upward is a *continuer* (squeeze);
  one that has started ticking down is a *fader*. Gating entry on "the up-move has decelerated" is the
  continuous-native version of the daily's fade-confirmation. **Priority: HIGH.**
- **⚠️ Market-context gate** (daily: `market_pct_up_max`, hot-market, BTC bounds; RD1). Don't short a coin
  pumping *against a weak/down market* — that is genuine idiosyncratic strength and the prime squeeze setup.
  RD1 showed the short works best when the pump rides a broad up-market that mean-reverts, and worst when it
  fights a weak tape. The continuous sleeve has zero market awareness. **Priority: HIGH.**
- **⚠️ Cap the extremity** (daily: `event_rank_fraction_max=0.90` EXCLUDES the top-10% most extreme pumps).
  The continuous sleeve shorts the **top decile = the most extreme** — the opposite of the daily, which
  learned the most violent pumps continue. Worth testing shorting D8 instead of D9, or capping by a hard
  extremity ceiling. **Priority: MEDIUM-HIGH** (a genuine signal tension to resolve in the engine).
- **⚠️ Require a real turnover/flow surge to enter** (the daily event needs turnover ≥6×). The continuous
  composite uses volatility but never *requires* an actual flow event, so it can short high-vol drifters with
  no migration. Adding a turnover-spike gate keeps it shorting genuine liquidity-migration pumps. **MEDIUM.**
- **⚠️ Crowded-short / funding-state gate** (daily crowding filter; I2i). Skip names already crowded-short
  (perp at a discount, short pays funding, squeeze-prone). **MEDIUM** (funding ≈0 here, so lower urgency).

### C. Entry execution
- **⚠️ Fade-confirmation entry** (daily: `promoted_quality_squeeze` — wait for a pop then a giveback). E1
  found this was a *non-lever for the daily* because the daily already enters a day late, past the squeeze.
  But the continuous sleeve enters **intraday, right at the extreme** — so a "wait until the fade actually
  starts" confirmation matters *more* here than it ever did for the daily. Adapt, don't copy: enter a D9 name
  only after an N-bar down-confirmation. **Priority: HIGH** (overlaps B's deceleration gate).
- **Entry-bar veto** (daily: `entry_execution_veto_close_location_max`). Don't open if the entry bar closes
  near its high (still being bid). **MEDIUM**, cheap.

### D. Exit sophistication — cut losers earlier, protect winners
- **⚠️ Failed-fade exit** (daily: `failed_fade` / ff6 — after N hours, if the fade hasn't worked and the
  position is down with a strong close, cut it). This is the daily's best loss-mitigation exit and the
  continuous sleeve has nothing between "leave-decile" (which a squeezer never triggers) and the 25% disaster
  stop. A failed-fade exit cuts the squeeze *before* it reaches the disaster stop. **Priority: HIGH.**
- **Breakeven stop / profit-lock / MFE-giveback** (daily exit ladder). Protect a continuous short that ran
  deep into profit then reverses — move the stop to entry once armed, or exit on a giveback of the max
  favorable excursion. The state exit is coarse (decile membership); these are finer profit-protection.
  **Priority: MEDIUM.**
- **Time-adaptive (loose→tight) stop** (daily: `stop_loose_window_hours`). Let the position breathe through
  the entry-bar wiggle, then tighten. Pairs naturally with the wide disaster stop. **MEDIUM.**

### E. Portfolio-level circuit breakers (⚠️ correlated-squeeze defense)
- **⚠️ Stop-pressure / realized-loss-pressure** (daily: pause new entries after N stops/losses in M days).
  A market-wide alt squeeze hits *many* continuous shorts at once (they're correlated — the audit's whole
  MTM-drawdown point). The continuous sleeve has **no portfolio kill-switch**: it would keep opening fresh
  shorts into a melt-up. A pressure gate is the portfolio-level analog of the disaster stop. **Priority:
  HIGH** (this is arguably as important as the per-trade stop).
- **⚠️ Staggered / crowding-aware entry** (the funding/crowding lesson at book level). Don't pile into many
  shorts in the same minute on a market-wide pump. **MEDIUM.**

## What does NOT port (or needs rethinking, not copying)

- **The discrete event thresholds** (turnover ≥6×, rank-climb ≥150, residual ≥8%) are an *event detector*;
  the continuous cross-sectional rank replaces that mechanism. Inherit the *spirit* (require a genuine flow
  surge — see B) not the exact thresholds.
- **Fixed take-profit.** A fade strategy's "take-profit" is the state exit (cover when it fades out of the
  decile). A fixed TP would cap the very fades the strategy exists to harvest. Skip.
- **The full 85-gate event filter wholesale.** Most gates are tuned to the daily event object; porting them
  blindly would just re-impose the daily's once-a-day sparsity onto a continuous book. Inherit the *defensive
  intent*, re-derive the *thresholds* in the continuous engine.

## Continuous-native sophistication (beyond the daily)

- **Borrow / short-availability gate** (the audit's #2 risk): skip names you cannot actually short, or only
  at punitive borrow — the daily's late entry mostly sidesteps this; the continuous intraday entry does not.
- **Beta-neutral overlay** (the L/S the audit measured): the short-only book carries residual short-beta; a
  D0-long / D9-short overlay strips it and was the continuous program's one genuinely-additive deliverable.
- **Intraday (hourly) MTM risk monitor**: the audit showed daily-close marks understate the squeeze
  drawdown; the live sleeve already reacts on ticker prices, so an hourly-marked book-drawdown circuit
  breaker is natural and native.

## Recommended sequence (methodology — engine first, then demo)

These are **research hypotheses, validated in the continuous engine, not bolted onto the live sleeve.** The
order maximizes risk-reduction per unit effort:

1. **Risk-equal sizing** (A) — biggest risk-quality gain, lowest risk, engine already supports it.
2. **Failed-fade exit + the disaster stop** (D, done) — cut losers before catastrophe.
3. **Squeeze-defense selection: age floor + momentum-deceleration + market-context** (B/C) — stop shorting
   the continuers in the first place. Re-decile / re-test each in `continuous_events.py`.
4. **Portfolio pressure circuit-breaker** (E) — correlated-squeeze kill-switch.
5. **The finer profit-protection exits + extremity cap + turnover-surge gate** (D/B) — polish.

Each step: pre-register → backtest in `continuous_events.py` (does it improve MTM-MAR / cut the squeeze tail
without killing the edge, all-weather both venues?) → if it passes, wire the validated parameter into
`continuous_demo.py` → forward demo. The demo sleeve should only ever run *validated* sophistication, the
same discipline the daily system was built under.

## VERDICT — tested in the engine, both venues (2026-06-01)

Every idea was added as an ablation knob to `continuous_events.py` and swept against the no-stop/flat
baseline on both venues (daily-MTM MAR/DD, worst-day, per-trade p5 squeeze tail, early/recent). Headline:
**the daily system's protective EXITS transfer cleanly; its SELECTION gates mostly backfire** — because the
continuous edge mechanics are the opposite of what those gates assume (it lives in the *most-extreme* decile,
in *down* markets, on *still-rising* names).

**ADOPTED (wired into the live sleeve `continuous_demo.py`):**
- **Failed-fade exit (ff6)** — the one clean cross-venue win: MAR up + DD down on BOTH (bybit 29.2→30.8,
  binance 35.2→**39.9**, DD 5.07→**4.08**).
- **Breakeven stop @ +10% MFE** — big on bybit (MAR 31→**38**, DD 3.2→**2.6**), ~neutral on binance. (At +5%
  it hurt binance; +10% is the cross-venue-safe arm.)
- **Age floor (30d)** — engine-neutral both venues; harmless fresh-listing-squeezer insurance.
- **Disaster stop (25%, server-side)** — safety, not edge; ~8% return cost, MAR roughly flat. Kept for live.

**REJECTED (the data killed them):**
- **Entry deceleration / fade-confirmation** — cut 63% of trades, MAR 29→13. The continuous edge is in names
  *still rising*; waiting for a down-tick removes the best entries. (Confirms E1's "execution-timing is a
  non-lever" at the continuous scale — here actively harmful.)
- **Market-context gate** ("short only when market not weak") — MAR 29→4. It removes the down-market regime
  where the short profits (the short-beta tailwind). The daily's market gate is for a different setup.
- **Extremity cap (short D8 not D9)** — CATASTROPHIC (MAR −0.3, DD 55–170%, negative return). The edge is
  *specifically* the top decile; the daily's "exclude the most extreme" does NOT transfer.
- **Inverse-vol sizing** — venue-split and trades DD up for return (not a risk-adjusted win); even a gentle
  clamp raised DD on both venues. Not adopted (re-tunable later, but not a clean improvement as tested).

**Adopted combo (live), vs the raw baseline:** bybit MAR ~29→**~38**, DD 3.9→**2.6%**; binance MAR ~35→~30
(the disaster stop's deliberate safety cost; ff6 alone *raised* binance to ~40). All-weather both venues. The
sophistication that helped was the *risk machinery* (exits + stop), not new selection signal — consistent with
the audit's finding that the continuous edge is a real-but-thin STR factor whose main live risk is the squeeze
tail, which these exits attack.
