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

- **K0:** pending — pre-register, then run on the 5950X. (Next step: write the K0 precheck
  script + its pre-registration receipt.)
- **K1 / K2:** gated on K0 (and K1, respectively).
