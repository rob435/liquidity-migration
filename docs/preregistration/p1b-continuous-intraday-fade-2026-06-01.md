# Pre-registration — P1b/Avenue D (CORRECTED): the fade is an all-day intraday process → continuous VIABLE

**Date:** 2026-06-01 · **Stage:** EXPLORATORY (look-ahead decile characterization; NOT promotion evidence)
**Plan:** `docs/research_plan_continuous_fade.md` (Phase 1 / Avenue D) · **Supersedes the null in**
`p1-continuous-daily-cycle-2026-06-01.md` (that 24h-hold first pass was a hold-span artifact).
**Standard:** `docs/backtesting_errors_we_never_repeat.md`

## What happened (an honest self-correction)

Avenue D's first pass measured the rmom-gated D9 short edge on a **24h hold** by hour-of-day and found a
monotone ~25× decay from the daily close → I concluded "daily cadence is load-bearing; continuous is a
null." **That was premature.** A 24h hold entered late in the UTC day mechanically spans the *next* day's
pump, which manufactures a close-peaked decay even if the fade itself is hour-agnostic. The mission's core
discipline ("never finalize a null prematurely" — this program mis-called nulls twice in the intraday arc)
demanded I break the confound before finalizing. I did, and it overturned the null.

## The two decisive tests (read-only, cached panel + klines close)

**p1f — entry-timing vs hold-span:** the GROSS fade and PER-HOUR fade rate of the D9 short at horizons
{1,3,6,12,24}h, by entry-hour. The 24h fade peaks at the close, **but the per-hour fade RATE peaks at
MIDDAY**: 6h-per-hour fade close 0-3 **+11.8/+11.1**, midday 10-13 **+17.1/+15.9**, late 19-22 **+8.8/+7.9**
bps/h (bybit/binance), positive every hour, all-weather. → the 24h decay is a hold-span artifact.

**p1g — the mechanism fork (fresh entries only):** is the intraday fade real for FRESH spell-entries
(Case A: continuous viable) or only stale/rolled-over memberships (Case B: daily near-optimal)? **CASE A.**
FRESH-entry 6h-per-hour fade, all EARLY-positive both venues:

| FRESH 6h/h (bps/h) | close 0-3 | morning 6-9 | midday 10-13 | late 19-22 |
|---|--:|--:|--:|--:|
| bybit (FULL / E / R) | +12.9 / +11.3 / +15.1 | +14.6 / +12.9 / +17.1 | +15.4 / +14.5 / +16.6 | +8.2 / +7.8 / +8.7 |
| binance (FULL / E / R) | +12.1 / +9.8 / +14.7 | +13.7 / +11.8 / +15.9 | +15.0 / +12.0 / +18.7 | +6.5 / +5.4 / +7.8 |

The clincher: the SAME fresh entries fade +88/+82 (bybit/binance) over 6h at the close but only continue to
+319/+314 over 24h, whereas a fresh midday entry fades +91/+90 over 6h and only +129/+139 over 24h — i.e.
the per-hour fade is comparable at all hours; the 24h close-peak is purely the longer post-close *runway*
(+ the next-pump span on late entries), not a daily-close lock.

## Corrected verdict — **CONTINUOUS IS VIABLE, all-weather; the null is RETRACTED**

The liquidity-migration fade is a **real, all-weather, all-day intraday process.** A fresh rmom-gated D9
entry (a confirmed pumped-and-now-idiosyncratically-weak name) fades at **+6–15 bps/h at every hour** (broad
00:00–15:00 UTC plateau, weakest but still-positive late-evening), both venues, both eras. Therefore:
- **Both boss battles are genuinely beaten.** BB1 (wrong-sign): a *fresh* D9 entry IS the causal
  "fade-has-started" confirmation — these names fade immediately, not continue. BB2 (recent-only): the fade
  rate is EARLY-positive at every hour (the rmom gate, Phase 0). The mission's thesis ("measure the state,
  short any hour") is **vindicated, not falsified.**
- **The right continuous design** (corrected): enter when a name *freshly* enters the rmom-gated composite
  D9 at ANY hour, **hold ~6–12h** (NOT 24h — the long hold is a daily-strategy artifact that spans the next
  pump and penalizes off-close entries). Optionally underweight late-evening (19–23 UTC) entries.
- **Continuous genuinely EXTENDS the daily**, it does not merely add lower-edge breadth: **~82% of fresh
  fades occur off the daily close** (entries the once-daily strategy never sees) at *comparable* per-hour
  fade. The earlier "daily 01:00 is the best entry" was the 24h-hold artifact; per-hour, any morning/midday
  hour is as good. Book size ~17–19 concurrent names, ~67 entries/day — operationally reasonable.

## Honest bounds (still NOT a validated strategy)

- EXPLORATORY decile characterization (per-spell/per-ts means, look-ahead decile formation — the *selection*
  is PIT-rankable so the per-hour fade is real, but it is **not a portfolio MAR**).
- **The now-justified next step is a concurrency/capacity-aware portfolio backtest** of the corrected design
  (enter fresh D9 any hour, hold 6–12h, 15→45 bps cost, funding-to-exit, max_active concurrency, both
  venues, early/recent): does the signal-level all-weather edge survive into a portfolio MAR that
  beats/complements the daily? **Capacity** (concurrent illiquid-alt shorts, borrow, impact) and **turnover
  cost** at a 6h hold are the key open risks. Funding is ≈0 at this entry profile (P0c), a tailwind.
- Substantially the STR/residual-reversal factor (rmom) — a continuous factor-harvest, not certified unique
  alpha.

## Lesson logged

Never finalize a null on a **single hold horizon** — decompose entry-timing from hold-span first. The 24h
hold conflated "when you enter" with "which 24h window you span." Caught only because the mission's
not-bail discipline forced one more decisive test. Artifacts:
`~/SHARED_DATA/p1f_entry_vs_hold_2026-06-01.out`, `p1g_fresh_intraday_2026-06-01.out`; scripts
`scripts/p1f_entry_vs_hold.py`, `scripts/p1g_fresh_intraday.py`. Label: **EXPLORATORY**.

## Next

Pre-register + run the **short-hold continuous portfolio proxy backtest** (the now-justified step;
concurrency + cost + funding). If it holds up cross-venue all-weather, the engine-grade rolling backtest /
forward-demo path becomes the operator-gated decision.
