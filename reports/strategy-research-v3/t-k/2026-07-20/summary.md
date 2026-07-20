# T-K — Lane-1 funnel replay for continuous_breadth_v1 (exploratory)

**Status: EXPLORATORY.** The Lane-1 prerequisite from
`docs/breadth_redesign_2026-07-20.md` before the breadth config can be
committed: replay the LIVE CONTINUOUS admission funnel over seen data with
the candidate knobs and measure (1) admitted bets/day and (2) the same-day
pairwise correlation ρ. No alpha or promotion claim; the config commit, not
this replay, would be the registration.

## What ran

Live-producer selection semantics translated line-for-line from
`continuous_demo` (not the historical crowd2 engine): engine feature pipeline
+ `cross_sectional_decile` at the live rmom quantile 0.33, live components
(turn3_pop3 w=1/3, turn4_pop3 w=2/9, turn4_pop5 w=4/9; age 240d; TP 12%),
one global per-cycle cap shared across components, held-while-open blocking
(no post-exit cooldown), BTC uptrend gate (fail closed), max_active 25, and
the deployed adverse-exit circuit breaker (pause after ≥8 net-negative
covers/24h). Exits: TP touch, left-decile, 48h cap. Window 2023-04 →
2026-07 (28,632 hourly cycles; 683 gate-open days, 510 gate-blocked).
Declared grid: liq ∈ {500k, 250k, 100k} × cap ∈ {5, 10} — the design doc
does not pin the "one notch" number, so both relaxations are reported.

## Verification verdict

**The candidate knobs do NOT produce 8–10 admitted bets/day.** Candidate
cell (250k / cap 10): mean 7.30 per gate-open day, **median 6**, p25 = 3,
**88 of 683 open days with zero bets** (pump-clustered arrivals). Only 40%
of open days reach ≥8. The prerequisite question is answered in the
negative on both of its parts: the admitted-count target is missed, and the
measured (ρ, per-bet vol) break the power table's day-count promises (see
below). This is a completed verification with a negative result, not a
failed replay.

## The two numbers (deployed constraints on)

| liq | cap | bets/open-day | days ≥8 | ρ̂ | per-bet vol |
|---|---|---:|---:|---:|---:|
| 500k | 5 (baseline) | 6.55 | 37% | 0.219 | 1,018 bps |
| 500k | 10 | 6.68 | 37% | 0.221 | 1,003 bps |
| **250k** | **10** (candidate) | **7.30** | 40% | **0.212** | 1,000 bps |
| 100k | 10 | 7.70 | 43% | 0.193 | 991 bps |

(ρ̂ = pooled same-day pairwise moment estimator over demeaned per-bet gross
returns, matching the power formula's variance structure; bets are
component-entries, as the deployed book takes them, so ρ̂ correctly includes
same-symbol multi-component duplication.)

## Funnel attribution — the cap is not the lever

Stage totals (baseline cell, gate-open cycles): D9 202,151 → liquidity
69,544 (34% pass) → event triggers 13,948 (**20% pass — the binding
stage**) → age 7,989 → available 4,552 → admitted 4,473. Raising the cap
5 → 10 adds only +0.1–0.2 bets/day (it binds on <2% of component-cycles);
the liquidity notch adds +0.7 (250k) to +1.1 (100k); 8.3–8.9 is reached
only with the circuit breaker and max_active removed, which is not the
deployed object. Headroom arithmetic: the age-stage flow is ≈ 12.8
component-entries per open day at the candidate liquidity notch — the
no-event-gate ceiling — so reaching 8–10 requires substantially widening
the event triggers, a lever the redesign doc names under lever 2 but the
candidate config deliberately did not bundle. That would be a new declared
design, not a re-parameterization of this one.

## Whether the power table's promises hold: they do not, as parameterized

The table in `scripts/breadth_power.py` assumed per-bet vol 300 bps and
ρ 0.15. Measured: **vol ≈ 1,000 bps, ρ ≈ 0.21**. Re-running the same formula
with measured inputs (N = 7.30, ρ = 0.212, vol = 1,000):

| hypothesized edge | days to t=2 | years |
|---:|---:|---:|
| 15 bps | 5,691 | 15.6 |
| 25 bps | 2,048 | 5.6 |
| 40 bps | 800 | 2.2 |
| 50 bps | 512 | 1.4 |

And the knobs barely move it: baseline (N 6.55, ρ 0.219, vol 1,018) needs
6,232 days at 15 bps — the candidate cuts days-to-significance by **~9%**,
not the ~4× the 1→10 row of the original table suggested (the deployed book
was never at 1 bet/day under this replay's semantics; it is at ~6.5).
Per-bet volatility — set by the ±12%-TP/48h trade shape — dominates the
arithmetic: at 1,000 bps/bet, a 15–25 bps edge is undetectable in useful
time at ANY breadth this funnel can produce (the N→∞ floor at ρ 0.21 is
≈ 9.5 years at 15 bps). The levers that actually move power are per-bet vol
(shape), edge size, and decorrelated sources (redesign lever 3) — not
admission count.

## Fidelity caveats

- Replay universe is the full-PIT root (includes delisted symbols); live
  trades current listings on WS data. Signal code path is the engine's own
  (`per_symbol_timeseries_features` + `cross_sectional_decile`).
- Left-decile exits evaluated on the same shifted hourly clock as entries
  (≤1h skew vs live); no account-kernel rejections, reserved-target or
  same-signal nuances beyond held-blocking.
- Per-bet mean returns (62–88 bps across cells) are spent-surface context
  only — sixth-generation read of an already-mined window; ρ and vol are
  second-moment estimates and more robust, but still historical.
- 2 bets pending at window end (excluded from ρ); paused cycles from the
  circuit breaker are recorded per cell in the grid.

## Read (for the config decision — owner's call)

The replay verifies the mechanics (the knobs deliver ~7.3 bets/day, ~12%
more than baseline) but refutes the power-table premise at the hypothesized
15–25 bps edge: measured vol and ρ make the promised "quarters to know
something" unreachable via admission breadth alone. If
`continuous_breadth_v1` is committed anyway, its forward record accrues
correctly but cannot adjudicate a 15–25 bps edge on any useful horizon;
the honest next lever is per-bet vol / decorrelated sources, or an edge
hypothesis ≥40–50 bps.

Artifacts: `tk_funnel_grid.csv`, `tk_daily_bets.parquet` (local),
`tk_live_panel_rmom33.parquet` (local cache), manifest with grids and
estimator definitions.
