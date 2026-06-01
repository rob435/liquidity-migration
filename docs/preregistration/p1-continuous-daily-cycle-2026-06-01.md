# Pre-registration — P1/Avenue D: is the continuous edge any-hour, or a DAILY-CYCLE object?

**Date:** 2026-06-01 · **Stage:** EXPLORATORY (look-ahead decile characterization; NOT promotion evidence)
**Plan:** `docs/research_plan_continuous_fade.md` (Phase 1 / Avenue D — the discretization question)
**Builds on:** `docs/preregistration/p0-continuous-rmom-2026-05-31.md` (Phase 0: rmom flips it all-weather)
**Standard:** `docs/backtesting_errors_we_never_repeat.md` · `docs/parameter_pre_registration.md`

> ## ⚠️ CORRECTION (2026-06-01, same day) — THIS RECEIPT'S "NULL" VERDICT IS RETRACTED.
> The null below ("daily cadence load-bearing; the fade is a daily-00:00-close object") was **premature —
> a 24h-HOLD-SPAN ARTIFACT.** Two follow-up tests (`scripts/p1f_entry_vs_hold.py`,
> `scripts/p1g_fresh_intraday.py`) decompose entry-timing from hold-span and overturn it:
> - The **per-hour fade RATE** does NOT peak at the close — it is a broad **+12–15 bps/h plateau from 00:00
>   to ~15:00 UTC** (slightly peaking midday), weakest but still-positive late-evening (+6–8), **positive at
>   EVERY hour, both venues, both eras.** For FRESH D9 spell-entries: close 0-3 **+12.9/+12.1 bps/h**, midday
>   10-13 **+15.4/+15.0**, late 19-22 **+8.2/+6.5** (bybit/binance), all EARLY-positive.
> - The SAME fresh entries show 24h fade peaking at the close (+319) but **6h fade flat** (close +88 vs
>   midday +91) → the 24h decay was the next-day-pump hold-span confound, not a daily-close lock.
> **Corrected verdict: the fade is a real, all-weather, all-day intraday process → CONTINUOUS IS VIABLE.**
> ~82% of fresh fades occur OFF the daily close (entries the once-daily strategy misses) at comparable
> per-hour quality, on a ~17-19-name book. Both boss battles beaten (BB1: a fresh D9 entry IS the
> fade-started confirmation; BB2: all-weather). The right design is **enter-any-hour + short ~6-12h hold**
> (not 24h). Full corrected write-up: receipt `p1b-continuous-intraday-fade-2026-06-01.md` + the
> "Continuous-fade Avenue D CORRECTED" summary section. **Read the rest of THIS receipt as the (flawed)
> 24h-hold first pass and the lesson: never finalize a null on a single hold horizon.**

## Hypothesis (the make-or-break for "continuous")

Phase 0 found the rmom-gated rolling short is all-weather *at the per-ts characterization level*. Avenue D
asks the decisive question the mission hinges on: is the edge genuinely **any-hour** (→ a continuous book
beats/extends the daily), or is it a **daily-cycle object** best entered at the daily close (→ the daily
cadence is load-bearing; continuous adds only lower-edge breadth, or is a null)?
**H_continuous:** de-overlapped trades survive cost AND the per-trade edge is roughly hour-agnostic.
**H_daily (null the plan welcomes):** the edge concentrates at the daily close and decays through the day →
the deployed 01:00 entry is near-optimal, continuous is dominated.

## Method (read-only, on the Phase-0 panel; cached to `<root>/_p1_continuous_panel.parquet`)

`scripts/p1d_continuous_turnover.py` + two cross-tabs on the cached deciled panel (rmom_lo universe, D9 =
the short, fwd = 24h forward, cost 15bps):
1. **Spell-entry edge (tradeable proxy):** count a name only at its gap-aware D9 entry, 24h cooldown →
   mean(−fwd−cost), full/early/recent + 2023/2024/2025H1/recent buckets. Persistence: dwell, %1h-spells,
   spells/yr, book size.
2. **Continuous vs daily:** the same edge restricted to the 01:00-UTC snapshot (= a once-daily decile short).
3. **Edge by hour-of-day** (all D9 memberships) and **by hours-since-entry** (freshness); then the decisive
   cross-tab **fresh-entry edge × hour-of-day** (is the hod-decay a staleness artifact or daily-cycle-driven?).

## Decision rule (pre-committed)

- **Continuous justified** if the per-trade edge is roughly flat across hours-of-day (esp. for *fresh*
  entries) → an any-hour book captures a real distinct edge → proceed to the engine build (A3/H3).
- **NULL (daily cadence load-bearing)** if the edge concentrates at the daily close and decays monotonically,
  *including for fresh entries* → the deployed daily entry is near-optimal; do NOT build the continuous engine.

## Post-run results (2026-06-01; `~/SHARED_DATA/p1d_continuous_turnover_2026-05-31.{json,out}`)

**Tradeability — PASSES.** De-overlapped spell-entry (24h cooldown, 15bps) is all-weather positive both
venues, every bucket, and *stronger* than the per-ts char (entering fresh beats averaging over the dwell):

| short_net bps/trade | bybit FULL / E / R | binance FULL / E / R |
|---|--:|--:|
| per-ts char (every D9 name-hour) | +144 / +126 / +171 | +133 / +105 / +165 |
| **spell-entry (cooldown 24h)** | **+193 / +169 / +226** | **+184 / +144 / +229** |
| daily 01:00 snapshot | +250 / +226 / +285 | +238 / +195 / +288 |

Persistence: mean D9 dwell **6.7h** (35% 1h-flickers), ~18 names concurrent, **~12.7k trades/yr** — a real,
moderate-turnover book, not pathological churn. **The edge is tradeable, not a churn artifact.**

**But the daily-cycle structure is overwhelming. Short edge by hour-of-day (equal n≈21.5k/hr):**

| UTC hour | 0 | 1 | 6 | 12 | 18 | 23 |
|---|--:|--:|--:|--:|--:|--:|
| bybit | **+259** | +250 | +209 | +148 | +66 | **+10** |
| binance | **+245** | +238 | +196 | +133 | +58 | **+3** |

A **monotone ~25× decay from the daily close to 23:00**, identical both venues. **The decisive cross-tab —
FRESH-entry edge × hour-of-day — confirms it is NOT staleness:** fresh D9 entries *also* decay by hour
(bybit hod0 **+304** → hod23 **−5**; binance +299 → −20), and **~4× of all fresh entries occur at hod 0**
(n≈13.6k vs ~2.5k/hr elsewhere). Fresh entries hod<12 +209 vs hod≥12 +67 (bybit); +199 vs +57 (binance).

## Verdict — **NULL for continuous-any-hour: the daily cadence is LOAD-BEARING** (the plan's welcomed result)

The fade is a **daily-00:00-UTC-close-cycle object.** The high-edge entries overwhelmingly arrive at the
daily close (when the trailing-window features refresh on the just-completed day); intraday entries at other
hours are both rarer and lower-edge *noise*, decaying to ~0/negative by day's end — even when fresh. So:
- **The strong continuous thesis ("the 01:00 clock is just a proxy; measure the state and short any hour to
  do as well or better") is FALSIFIED.** The clock is not a proxy — it is the actual information-arrival time,
  and the **deployed 01:00 daily entry is near-optimal** (edge +300→0 across the day).
- A continuous any-hour book would add only **lower-edge intraday-noise breadth** to a near-optimal daily
  strategy. **The expensive engine-grade continuous backtest (A3/H3, ~5-7d) is NOT justified.**
- This is **coherent with the whole program** (E1: fill-timing not the lever; K1a: same-selector detection
  not the lever; the I-phase reconciliation: the daily *late* entry is the safe, funding-light harvest). The
  continuous-fade program adds: the daily entry *timing* is near-optimal because the fade is daily-periodic.

## Valuable byproducts (this null is not empty-handed)

1. **The deployed 01:00 entry is vindicated** as near-optimal — a strong positive validation of the live
   strategy's clock (independent of the event filter).
2. **rmom makes the cross-sectional fade all-weather** (Phase 0) — reconfirming the already-validated daily
   rmom gate from a fully independent (continuous-panel) angle, and showing it beats c2b's age-only flip.
3. **Residual DAILY lead (not continuous):** the rmom-composite-decile *daily* selection (daily_0100,
   all-weather +250/+238) is a broader daily short than the deployed event-filter — worth a proper DAILY
   backtest comparison (concurrency/MAR/DD vs the deployed strategy). This is a daily-selection study, not a
   continuous system, and is the honest place the Phase-0 all-weather finding pays off.

## Honest bounds

EXPLORATORY decile characterization (per-spell means, simplified concurrency, look-ahead decile formation —
the *selection* is PIT-rankable so the per-trade edge is real, but it is not a portfolio MAR). The null is
robust where it counts: monotone hod-decay replicated both venues, for fresh AND stale entries, equal-n per
hour, ~4× fresh-entry concentration at the close — not a marginal single cell. Cost-stressed to 45bps the
spell-entry edge stays all-weather; funding is ≈0 (P0c). Low residual risk that a non-24h hold inverts a 25×
monotone decay.

Artifacts: `~/SHARED_DATA/p1d_continuous_turnover_2026-05-31.{json,out}`; cached panels
`<root>/_p1_continuous_panel.parquet`; scripts `scripts/p1d_continuous_turnover.py` (+ inline hod/freshness
cross-tabs, numbers in this receipt). Label: **EXPLORATORY** (never promotion evidence).

## Next

- **Continuous program: CLOSED as an honest null** (daily cadence load-bearing). Do not build A3/H3.
- **Carry forward (operator's call, DAILY not continuous):** backtest the rmom-composite-decile as a daily
  SELECTION vs the deployed event-filter strategy (does the broader all-weather decile beat/complement the
  event filter on MAR/DD, both venues, funding-costed?). Pre-register separately if pursued.
