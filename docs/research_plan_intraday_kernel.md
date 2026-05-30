# Research plan — the intraday-detection kernel (fast WS reaction to the discrete event)

**Created 2026-05-30.** Supersedes the prior selection-execution (E1→E2→E3) and part-2
research plans — that round concluded (E1 falsified the execution premium, E3/sniper
dropped) and its verdicts are consolidated in `research_summary.md`. Methodology gate:
`backtesting_errors_we_never_repeat.md`. Pre-registration: `parameter_pre_registration.md`.
Decision rule: the three-tier demo-arbiter in `STATE.md`. **All runs are on the 5950X
full-PIT roots** (the 16 GB box can't hold a full cell, ~23 GB; `SWEEP_MAX_WORKERS=1`).

## The hypothesis

The discrete liquidity-migration **event** is the all-weather selector — it is
threshold-triggered and sparse, and (unlike the rank-all continuous decile, which tested
regime-conditional and is rejected — the c2b verdict, see `research_summary.md`) it is
positive in the **early** period too. Today it fires on the **daily-close roll**: one
actionable entry per UTC day per name.

> **Kernel hypothesis:** detecting the event **intraday**, off the live WS stream (rolling
> event features), and entering immediately captures more of the fade — improving the
> strategy **robustly, on both venues, including the early period.**

Keep everything proven — the discrete event selector + the **age gate** (`pit-age-days-min≈300`)
+ the **residual-momentum gate**. Change exactly one thing: **detection latency** (daily-close
→ intraday). Execution stays **immediate** (E1: timing-*within*-a-bar is a non-lever — the
lever under test is *detection* latency, not *fill* latency).

## Explicitly OUT of scope (already settled — do not revisit)

- **Rank-all continuous decile (C0 / Architecture B):** regime-conditional, rejected. The
  kernel keeps the sparse event trigger, NOT a dense always-on rank.
- **Fade-confirmation execution (`promoted_quality_squeeze`) + sniper fill-timing (E3):**
  non-lever (E1). The lever is detection latency, not how fast the fill lands.

## Phases (cheap → expensive; each phase gates the next)

### K0 — Upside-ceiling characterization (CHEAP, read-only, run FIRST)
Read-only analysis on the existing validated daily-event ledger (both venues). For each event
name, reconstruct the intraday price path between the intraday pump-peak and the daily-close
detection bar, and measure **how much of the eventual giveback is already gone by the time the
daily-close entry fires.**
- **Output:** the distribution of "fade already captured by daily-close entry" vs "fade still
  available," both venues, split **early (2023-04→2025-06) / recent (2025-06+)**.
- **Gate → K1:** a material chunk of the giveback is systematically lost to daily-close
  lateness, on **both venues including the early period.**
- **Falsifier (STOP):** if the daily-close entry is not systematically late (most of the fade
  is still ahead) → detection latency is a non-lever (consistent with E1) → do not build.
  File the negative verdict; the discrete daily strategy + age gate stands.
- **Cost:** a precheck script (read-only, in the `c1/c2/c2b` style). No engine build.

### K1 — Intraday-detection backtest (MEDIUM, only if K0 passes)
Build the rolling intraday event-feature pipeline (compute the event features on a trailing
window so the event can fire intraday, **PIT-causal — no within-bar look-ahead**) and backtest
the intraday-detected, age+momentum-gated strategy under the realistic engine (capped fills,
15 & 45 bps, full-PIT) **vs the daily-close baseline**, both venues.
- **Decision:** `scripts/r1_robustness.py` Tier-2 verdict + fragility (bootstrap p5, LOO,
  sub-period thirds). Pre-register before running.
- **Falsifier (STOP):** the lift is regime-conditional (recent-only) like c2b, OR there is no
  robust cross-venue MAR Δ over the daily-close baseline → do not build the live engine.

### K2 — WS live-detection engine + forward demo (EXPENSIVE; explicit operator go ONLY)
The ~5–7 day build: production WS intraday detection running the **same code path** the K1
backtest used (same-code #16), PIT-safe, then forward-demo it. **Hard line:** not started
without an explicit operator go AND K0+K1 passing. The forward demo is the Tier-3 arbiter.

## Guards (the lessons baked in)

- **Both venues + early/recent split on every phase** — the c2b trap: a recent-bear-only edge
  is a regime bet, not an edge.
- **Keep the proven selector** — don't swap the sparse event for a dense rank; don't relax the
  age / residual-momentum gates.
- **PIT / causality** — intraday features must be causal at the detection instant; no
  within-bar look-ahead (backtesting-errors #2 / #13).
- **Same-code** — the forward demo must run the identical detection code as the K1 backtest (#16).
- **Don't block the ready thing** — forward-demo the already-validated discrete + age gate in
  parallel; this kernel is exploration, not a dependency.

## Status

- **K0: DONE — PASS (2026-05-30).** The daily-close (+1h) entry is a median **~8–11% below
  the event-day intraday peak**, positive on **both venues × both early/recent splits ×
  both books** (fixed_delay age≥90 and age≥300 quality_squeeze) — clears the ~15 bps gate by
  ~50–65×. Decomposition (K0b): **~95–98% of that gap is the within-day-D giveback**, the +1h
  fill window only ~2–6% → the lever is **intraday detection**, not faster fills (consistent
  with E1). It is an optimistic *ceiling* (exact-top short) → necessary, not sufficient.
  Receipt: `docs/preregistration/k0-intraday-fade-timing-2026-05-30.md`.
- **K1: DONE — FAILED (K1a, 2026-05-30). The kernel is FALSIFIED; K1b + K2 cancelled.**
  K1a (cheap feasibility, `scripts/k1a_intraday_first_crossing.py`) found the first intraday
  hour the *same selector* fires and the realistic uplift (fill at `h*`+1 vs the daily +1h
  entry). Result: only **~40–85 bps median (~10–15% of the K0 ceiling)**, a **coin flip**
  (45–46% negative, q25 ≈ −3% / q75 ≈ +5%), **negative on binance early**, `corr(lead,uplift)
  ≈0`. **Mechanism:** the selector cannot *confirm* the event (cumulative turnover ≥6×) until
  median `h*` = 15–16:00 UTC — ~9h after the morning peak — by which point price has faded back
  to ≈ the daily entry; confirmation time is decoupled from the price path. The K0 ceiling is
  real but **un-capturable by faster detection of the same event.** Receipt:
  `docs/preregistration/k1-intraday-detection-2026-05-30.md`.
- **K2:** CANCELLED (gated on K1, which failed).

## Outcome — the timing axis is closed; the alpha is SELECTION

E1 killed *fill* timing (within 1h); **K1a kills *detection* timing** (across ~9h, same
selector). Neither faster fills nor faster detection of the same event is a lever — the
daily-close cadence leaves no capturable money on the table because the event cannot be
confirmed any earlier. **The program reverts to its validated SELECTION refinements** — the
age gate (Tier-2, all-weather) + the residual-momentum gate (P3b, demo-eligible) — under
forward demo (the operator-gated arbiter).

## I-phase — purpose-built intraday selector (operator-directed reopening, 2026-05-30)

The operator (correctly) flagged that K1a was too narrow: it tested the *daily selector run
hourly*, not a **purpose-built intraday signal** firing on rate/flow features at the peak — a
genuinely different signal needing custom engineering. Reopened.

- **Channels (verified data):** cross-venue all-weather = klines (price/volume/intrabar/velocity)
  + premium_index (hourly) + funding (8h) both venues + market-context; OI bybit-only;
  taker-flow binance-recent-only (out). 1h grain.
- **I1a (DONE):** faders carry a clear cross-venue intraday **exhaustion fingerprint** — peak
  ~16–17 UTC, turnover climax ~4.2–4.6× day-mean at the peak then rolloff, peak-bar upper-wick
  ~0.43 + mid-range close (rejection), price +20–22% then fades ~6–8%/6h; OI builds into + surges
  after the peak (bybit); premium quiet. Fingerprint EXISTS (necessary). `scripts/i1a_fader_intraday_signature.py`.
- **I1b (DONE — PASS).** Scanned ALL intraday rate-bursts (age≥300, rank 31–400, gain≥8% +
  vol-spike≥5×, cooldown 3d, fwd 48h) over BOTH venues incl. non-events (bybit 8968 / binance 7912),
  labeled forward fade-vs-continue, tested separation + **beta-neutralized**. Result: separation
  SURVIVES beta-neutralization (idiosyncratic, not market-regime beta), robust **cross-venue +
  early/recent** — `idio` (pump size vs market) ic_neutral −0.28…−0.31, velocity/vol-spike/accel
  −0.11…−0.16, wick = noise. Edge is a SELECTION on pump-extremity (extreme quintile beta-neutral
  short +1.2–1.3% early / +4.4–4.7% recent gross 48h; all-bursts ~breakeven). `scripts/i1b_burst_separation.py`.
- **I2 (NEXT — the real test):** pre-register + backtest the extreme-pump-burst short under the
  realistic engine (15 & 45 bps, capped stops, max_active, full-PIT, both venues, early/recent,
  MAR-primary via r1_robustness) AND residualize through `risk_model.decompose_strategy_pnl` — is it
  **unique alpha or the known short-term-reversal factor**? The gross-forward edge must survive
  costs+stops; the edge lives in the extreme subset (selection design matters). Overfitting guard:
  cross-venue + early/recent + full-distribution reporting. Pre-reg:
  `docs/preregistration/i2-intraday-burst-selector-2026-05-30.md`.
- **I3 / live:** gated on I2 + explicit operator go (WS engine + forward demo).
