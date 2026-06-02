# cb1 — continuous-fade entry circuit breaker: engine validation verdict

**Date:** 2026-06-02. **Type:** engine ablation (EXPLORATORY engine — modeled impact; not promotion
evidence). **Tool:** `scripts/cb1_circuit_breaker_validate.py` + the breaker knob in
`continuous_events.ContinuousEventConfig` (`entry_pause_after_adverse_exits`, `entry_pause_window_hours`).
**Question (operator-directed):** does the portfolio circuit breaker — pause new entries when ≥ N
net-negative covers (correlated-squeeze footprint) close within the trailing window — improve the
continuous sleeve? Turn it on if beneficial.

## Setup

Config mirrors the LIVE sleeve: short D9, rmom-low, liq ≥ $500k, +1h honest entry, state exit, max_hold
48h, 25% server stop (`bar_extreme_capped`, 10% slip cap), ff6, breakeven@10%, age 30d, gross 0.5 /
max_active 25. Metric = the inheritance-doc methodology: portfolio **daily-MTM** MAR / max-DD / worst-day
(`_portfolio_mtm_equity`), full window 2023-04-01 → 2026-05-28, both venues. Swept window ∈ {12,24,36,48}h
× threshold ∈ {3,5,6,8,10,12,16} (19 distinct cells/venue).

## Results (MTM-MAR; baseline = breaker OFF)

**bybit** — OFF: **MAR 38.6**, DD 2.6%, ret 304%.
| w24 | n6 | n8 | n10 | n12 | n16 |
|--|--|--|--|--|--|
| MAR | 28.9 | **41.1** | 33.6 | 35.2 | 37.0 |
w36 (n6..16): 36.3 / 30.5 / 33.5 / 32.0 / 34.3 — all < 38.6. w12/w48: all < 38.6.
→ **Only w24/n8 beats OFF (a lone spike: both neighbors n6=28.9, n10=33.6 are far below baseline).**
18 of 19 cells degrade MAR.

**binance** — OFF: **MAR 30.4**, DD 5.1%, ret 460%.
| w24 | n6 | n8 | n10 | n12 | n16 |
|--|--|--|--|--|--|
| MAR | 36.1 | 38.5 | 33.6 | 33.6 | 41.2 |
→ **The entire w24 column beats OFF (robust plateau; DD cut to ~2%).** w36/w48 over-pause (all < OFF).

**Cells beating OFF on BOTH venues:** exactly **{w24/n8}** — and on bybit that cell is the noise spike.

## Verdict — NOT ADOPTED (default OFF)

- **Venue-divergent, not robust.** The breaker robustly helps the *squeezier* book (binance, baseline
  DD 5.1% — whole w24 column up) but **hurts the already-clean book (bybit, baseline DD 2.6%): off is
  optimal, 18/19 cells lose MAR**, and the one winner (w24/n8) is an isolated spike whose immediate
  neighbors degrade MAR 13–25%. It fails the cross-venue robustness bar the other inheritances cleared.
- **The live continuous sleeve runs on BYBIT**, where the validation says *off* is best. Turning it on
  would either pick the bybit noise-spike cell — exactly the "rescue a specific cell" the repo's
  non-negotiables forbid — or land on a neighbor and degrade the book.
- **What IS robust:** every cell cuts DD (more pausing → lower DD, monotonic) and the breaker cuts DD
  *more* than return where the squeeze tail is large (binance). So the breaker reliably **trades return
  for drawdown**; it just is **not a robust risk-adjusted (MAR) win on bybit**, and it roughly halves
  binance absolute return for its DD gain.
- **Caveat (honest):** the metric is *daily* MTM DD; the breaker targets the *intraday/hourly* squeeze
  tail (audit: 6–7%, vs 2.6% daily here) that this metric under-counts — so the test is conservative-
  to-the-breaker on DD yet still finds no robust bybit MAR benefit. It also can't see an out-of-sample
  correlated melt-up worse than 2023–26 contained.

## Disposition

- **Engine default stays OFF** (`entry_pause_after_adverse_exits=0` in `continuous_events`), like the
  other ablation knobs (ff6/breakeven/age are also engine-default-off, set per-config).
- **LIVE sleeve: ENABLED at w24/n8 — operator-directed 2026-06-02.** `continuous_demo` ships
  `entry_pause_after_adverse_exits=8`, `entry_pause_window_minutes=1440`. **This does NOT overturn the
  verdict above** — it is a deliberate operator choice to run the breaker as protective TAIL INSURANCE,
  accepting the in-sample cost (≈ −21% bybit return for −27% DD; n8 is a fragile cell so live behaviour
  may sit nearer its weaker neighbours). It only ever pauses entries (never adds risk), on the demo
  sleeve; the forward demo is the arbiter. Disable with `entry_pause_after_adverse_exits=0`.
- Mechanism + engine/live regression tests retained. Artifacts: `/tmp/cb1_{bybit,binance}{,_fine}.json`;
  reproduce via the two-line dispatch in `scripts/cb1_circuit_breaker_validate.py`.
