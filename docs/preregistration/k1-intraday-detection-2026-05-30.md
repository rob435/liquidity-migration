# Pre-registration — K1: intraday-detection backtest vs the daily-close baseline

**Date:** 2026-05-30 · **Gated on:** K0 PASS (`k0-intraday-fade-timing-2026-05-30.md`).
**Stages:** K1a EXPLORATORY (feasibility/timing) → K1b CANDIDATE (Tier-2 demo-arbiter).
**Plan:** `docs/research_plan_intraday_kernel.md` · **Standard:** `docs/backtesting_errors_we_never_repeat.md` · `docs/parameter_pre_registration.md`.
**Run on:** the 5950X full-PIT roots.

## Hypothesis

K0 showed the daily-close (+1h) short enters a median ~8–11% below the event-day intraday
peak, ~95–98% of it within-day-D (the lever is *detection*, not fills). **H1:** detecting
the **same** liquidity-migration event **intraday** (rolling-24h features, PIT-causal) and
entering immediately (the same +1h fill) captures materially more of that fade than
daily-close detection — **robustly cross-venue and in both early/recent.** **H0:** intraday
detection adds no robust cross-venue MAR Δ over the daily-close baseline (or only recent) →
detection latency is not capturable in practice → do not build the K2 live engine.

## What changes — exactly ONE thing: detection latency

- **Selector UNCHANGED.** Identical event thresholds to the validated daily event
  (`_filter_liquidity_migration`): rank-climb ≥ `rank_improvement_min`, turnover ≥
  `turnover_ratio_min`×prior-7d, residual return ≥ `residual_return_min`, close strength,
  the rank band, **age ≥ 300, residual-momentum gate** — all kept. Not a new selector.
- **Detection: daily-close roll (00:00 D+1) → rolling hourly.** At each hour `h` of day D,
  recompute the event features on a **trailing-24h** window: turnover-ratio (trailing-24h
  turnover / prior-7d daily baseline), residual return (trailing-24h return − market
  trailing-24h median), close_location (current price in the trailing-24h range), and the
  cross-sectional liquidity rank (by trailing-24h turnover across the universe). **Fire on
  the FIRST hour the conjunction crosses** — one entry per name per event, cooldown = the
  daily event's.
- **Entry: immediate = +1h fill** (the non-negotiable fill delay, applied at the detection
  hour). NOT faster fills — E1 settled fill-timing is a non-lever, and K0b put the +1h fill
  at only ~2–6% of the gap. Intraday detection `h` → fill at `h+1` open.
- **Engine/costs/concentration UNCHANGED:** `bar_extreme_capped` 10%, 15 & 45 bps,
  `max_active=12`, full-PIT, both venues, early/recent split.

## PIT-causality — the correctness gate (#2 / #13 / #14 / #16)

- Decision at hour `h` uses ONLY klines with close ≤ `h` (open-stamped bar opening ≤ `h−1`).
  **No within-bar look-ahead** — the hour-`h` bar's high/close may not inform the hour-`h`
  decision before that bar closes.
- Cross-sectional features (rank, market median) at `h` use the universe's trailing-24h
  state **through `h` only**.
- Fill at `h+1` open. **Same-code (#16):** the K1 detector is a pure function of
  `(rolling features through h)` so the K2 WS detector runs the identical path.

## K1a — cheap feasibility + realistic-uplift (EXPLORATORY, run FIRST)

Conditioned on the **daily-firing event names** (the validated ledgers used in K0). For each,
reconstruct the within-symbol trailing-24h pump features hour-by-hour through day D, find the
**first hour `h*`** the pump thresholds cross (turnover-ratio + residual return + strong
current price; the cross-sectional **rank is APPROXIMATED as satisfied** — documented
limitation, it is the expensive full-universe piece deferred to K1b), and measure:
- **lead** = (daily-close − `h*`) in hours;
- **realistic_uplift** = (price(`h*`+1) − daily_entry_price)/daily_entry_price — the
  **realistic** analog of K0's ceiling (entry at first-crossing, not the exact peak).

Report median lead + realistic_uplift, the fraction crossing before the close, **both venues,
early/recent.**
- **GATE → K1b:** realistic_uplift materially positive (≥ ~round-trip cost) on **both venues
  + both splits**, with a non-trivial lead.
- **FALSIFIER → STOP/rethink:** first-crossing at/after the close, OR realistic_uplift
  ~0/negative on a venue, OR recent-only → intraday detection captures little even
  optimistically; the daily strategy + age/rmom gates stand.
- **Caveat:** conditioned on daily-firers (ignores intraday **false positives**) + rank
  approximated → a feasibility/timing characterization, **NOT** a performance estimate.
  EXPLORATORY — never promotion evidence.

## K1b — full rolling-universe backtest (CANDIDATE, only if K1a passes)

Build the full hourly rolling event-feature pipeline over the full-PIT universe (proper
cross-sectional intraday rank + market median), fire intraday on the **whole universe**
(accepting intraday false positives), enter +1h, backtest vs the daily-close baseline under
the realistic engine, both venues. This is the real test (net of false positives).
- **Correctness gate before trusting K1b:** restricted to fire only at the close hour, the
  rolling detector must reproduce the daily-close event firings (same-selector sanity check)
  to within a tight tolerance.
- **Decision:** `scripts/r1_robustness.py --sweep-tag` Tier-2 verdict (MAR-primary) +
  fragility (bootstrap p5, LOO, sub-period thirds), both venues, early/recent. **Residualize
  through `risk_model.decompose_strategy_pnl`** (don't rebuild). Pre-register the exact run
  before executing.
- **FALSIFIER → STOP:** lift recent-only (c2b trap), OR no robust cross-venue MAR Δ over the
  daily baseline, OR the intraday false positives sink net performance.

## What would make this INVALID

- Within-bar look-ahead (using the hour-`h` bar's close/high to decide at `h`), or
  cross-sectional features using future symbols' data.
- Citing K1a as performance/promotion evidence (conditioned on daily-firers + rank-approx →
  EXPLORATORY feasibility only).
- A detector code path that cannot map to the K2 WS order lifecycle (#16).

## Verdict — K1a FAILS the gate → STOP. The intraday-detection kernel is FALSIFIED.

Ran K1a 2026-05-30 (5950X, both `fixed_delay` ledgers; `scripts/k1a_intraday_first_crossing.py`).
For every daily-firing event, found the first intraday hour `h*` the same selector
(cumulative-since-day-start turnover ≥6×, residual ≥8%, close-loc ≥0.30) crosses, and the
**realistic** entry uplift (fill at `h*`+1 vs the daily +1h entry):

| venue | split | n | fired | realistic_uplift median | lead | ceiling | captured | frac<0 |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| bybit | ALL | 763 | 100% | **+58 bps** | 9h | 971 | 0.13 | 0.45 |
| bybit | EARLY | 363 | 100% | +39 | 8h | 928 | 0.10 | — |
| bybit | RECENT | 400 | 100% | +85 | 9h | 985 | 0.15 | — |
| binance | ALL | 477 | 100% | **+40 bps** | 8h | 851 | 0.10 | 0.46 |
| binance | EARLY | 169 | 100% | **−7 bps** | 7h | 678 | 0.06 | — |
| binance | RECENT | 308 | 100% | +64 | 8h | 960 | 0.11 | — |

**FALSIFIER TRIPPED** (negative on binance EARLY; realistic uplift ≈ 0 everywhere): the
same-selector intraday detector captures only **~10–15% of the K0 ceiling** (~40–85 bps
median vs ~9%), and it is a **coin flip** — **45–46% of trades are negative** (q25 ≈ −3%,
q75 ≈ +5%), median barely positive, negative on binance early. `corr(lead, uplift) ≈ +0.07`
(no monotone "earlier is better"); the faint long-lead tercile (+100–200 bps) is still ~40%
negative and would need a *different* selector to isolate.

**Mechanism (coherent with K0b):** the giveback happens *early* (around the morning peak),
but the selector cannot **confirm** the event until cumulative turnover hits 6× — median
`h*` = **15–16:00 UTC**, ~9h after the peak — by which point price has faded back to ≈ the
daily entry. **Confirmation time (turnover accumulation) is decoupled from the price path**,
so you enter at an essentially random point ±3–5% around the daily entry. The ~9% K0 ceiling
is real but **un-capturable by faster detection of the same event.**

**This closes the entire timing axis:** E1 killed *fill* timing (within 1h); K1a kills
*detection* timing (across ~9h, same selector). The alpha is **purely SELECTION**, and the
daily-close cadence leaves no capturable money on the table — you cannot confirm the event
any earlier. **Do NOT build K1b or K2.** The discrete daily strategy + the age gate +
residual-momentum gate (forward-demo-gated) stand as the program. A *new* intraday-native
selector (fire on early-confirming high-intensity pumps) is a separate, unchartered
direction with unassessed false-positive risk — not this kernel; not pursued.

Per-trade tables: `~/SHARED_DATA/k1a_{bybit,binance}_fixed_delay.csv`. Label: **EXPLORATORY**
(conditioned on daily-firers + rank-approx; a feasibility null, not a performance backtest —
but decisive: the *optimistic* test already fails). K1b/K2 cancelled.
