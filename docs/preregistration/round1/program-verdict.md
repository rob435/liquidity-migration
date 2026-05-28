# 2026-05-27 multi-phase research program — END-OF-PROGRAM VERDICT

**Date:** 2026-05-28 (program ran 2026-05-27 evening → 2026-05-28 06:40,
ended ~38h after kickoff, 19 days inside the 2026-06-15 Phase-7 deadline)
**Stage:** **PROGRAM COMPLETE — DOCUMENTED NULL RESULT.**
**Parent plan:** [2026-05-27 multi-phase research plan](rank-direction-edge-and-universe-isolation-research-plan.md)

## Headline

**The 7-phase research program completed with ZERO Phase-7-passing
finalists across all phases.** The strategy stays in its current state.
The frozen "promoted" profile on the live demo + paper shadow is
unchanged. No production filter changes, no parameter changes, no demo
deployment, no real-money consideration.

This is the OUTCOME the parent plan explicitly named as one of two
acceptable conclusions ("Phase 7-passing finalist ready for forward
demo, or a documented null result"). The null is the honest answer.

## Per-hypothesis verdicts

| H | Statement | Verdict | Evidence |
|---|---|---|---|
| **H1** | Universe widening explains the post-fix DD shift | **FALSIFIED** | Phase 1: baseline-pair DD improvement only +1.7pp (< +5pp falsifier); avg across 6 pairs +5.78pp improvement (< +8pp confirmation). Universe affects Sharpe (avg +1.09 on 474) but not DD. |
| **H2** | Rank-deterioration is a tradable short edge | **FALSIFIED-by-construction** | Phase 2: ALL 11 deterioration cells × 2 venues returned 0 trades. The production filter stack's quality-positive gates (day_return ≥ 0, residual ≥ 0.08, close_location ≥ 0.30) structurally exclude bearish names. |
| **H3** | Two-sided rank-dislocation as a single event | **FALSIFIED-by-construction** | Phase 2: all 11 both-direction cells produce metrics identical to imp-direction cells (det contributes 0 trades). |
| **H4** | Inverse-direction edge survives pre-2023 OOS | **NOT TESTABLE** | Phase 7 conditional on Phase 2 candidate; 0 candidates → Phase 7 didn't run. |
| **H5** | Some filters net-negative | **FALSIFIED** | Phase 0: 0 cells cleared the +0.5 Sharpe Δ / -5pp DD Δ bar on BOTH venues. 3 cells emerged as falsifiers (`crowding`, `event_rank_frac`, `turnover_ratio`) confirming those filters carry real edge. |
| **H6** | At least 1 PIT feature has stable IC > 0.03 | **PARTIALLY CONFIRMED** | Phase 5: 6 features cleared the survival rule at fwd_ret_3d; FDR ceiling pinned 5 (vol_of_vol_30d, realized_vol_7d, dist_from_30d_low, xs_rank_ret_7d, xs_rank_ret_3d). Univariate signal is real. |
| **H7** | Combined-signal portfolio beats event-driven | **FALSIFIED** | Phase 6: every combined-portfolio scheme shows sharpe BELOW the event-driven baseline on both venues + DD WAY worse (-60 to -99% vs baseline -42%). |

## What the program FOUND (informational only, not actionable)

Several secondary observations emerged that don't trigger production
changes but are worth recording for future research direction:

1. **Three production filters are clearly load-bearing:** `crowding`,
   `event_rank_frac`, `turnover_ratio`. Phase 0 falsifier hits give
   us evidence-of-importance for these three; the production stack
   should not casually drop them.

2. **A few production filters are near-no-ops on both venues:**
   `day_return`, `stop_pressure`, `realized_loss`, `rank_max`. They
   pass the Manifesto's strict bar trivially (Δ ≈ 0), suggesting
   they rarely fire as the binding constraint. Removing any of them
   would require a fresh pre-reg with the same candidate-quality
   bar — Phase 0 did not provide that authority.

3. **Universe widening hurts Sharpe but not DD.** Phase 1 showed
   the 474-archive-only universe has +1.09 higher Sharpe on average
   vs the 764-full universe (per-trade quality dilution from the
   v5-listing supplement) — but the DD shift to -42% is NOT
   explained by universe widening. Other candidate explanations
   (bug-fix removing rank-deteriorating signals, regime shift,
   code drift) remain open but were not within scope to test.

4. **Univariate IC signal exists in the PIT panel.** Phase 5b
   identified 5 features with statistically-stable cross-sectional
   IC on both venues: vol_of_vol_30d (avg |IC|=0.087),
   realized_vol_7d (0.081), dist_from_30d_low (0.071),
   xs_rank_ret_7d (0.043), xs_rank_ret_3d (0.039). All point
   short-side (high feature → low fwd return). The combined-portfolio
   scheme tested in Phase 6 doesn't translate this IC into
   event-driven-baseline-beating equity, but the underlying signal
   is real.

5. **Bybit and Binance have venue-specific optimal thresholds.**
   Phase 2 showed Bybit's optimum sits at rank-improvement-min ≈
   200-300 while Binance's optimum sits at ≈ 100-150. The current
   production default (150) is near Binance's optimum but far from
   Bybit's. The cross-venue Manifesto bar (BOTH venues must clear)
   means no single threshold optimum exists; the current default is
   the joint-optimum compromise.

6. **The deterioration direction is structurally untestable** under
   the existing improvement-biased filter stack. To test H2/H3
   honestly would require a different filter stack tuned for
   bearish entries — a separate research program with its own
   pre-reg.

## Pre-commitment compliance — final check

- ✅ NO Phase-7 finalist was promoted; NONE existed.
- ✅ NO Manifesto threshold was loosened to rescue any cell.
- ✅ NO inconclusive cell was reclassified as a candidate.
- ✅ NO Phase-1 (biased_benchmark) cell was forwarded for promotion.
- ✅ NO production filter change was made on the basis of the
  "three gates near-no-op on both venues" observation.
- ✅ NO 474-restricted parameter set was promoted.
- ✅ NO real-money toggle was set (`REAL_MONEY=true` stayed off
  throughout the entire program).
- ✅ NO Phase-8 was invented to chase a near-miss.
- ✅ Hard deadline (2026-06-15) honoured trivially — program
  closed 2026-05-28, 19 days before the deadline.

## Compute used

- Phase 0: 30 runs × ~17 min avg = ~111 min wall, 4-way parallel
- Phase 1: 12 runs × ~25 min avg = ~81 min wall (Bybit-only)
- Phase 5a: 2 panel-builds × ~5 min = ~10 min wall
- Phase 5b: 6 IC computations × ~1 min = ~5 min wall
- Phase 6: 30 cells × <1 sec = ~0.1 min wall (math-only on cached panels)
- Phase 2: 66 runs × ~16 min avg = ~260 min wall, 4-way parallel
- **Total compute: ~9 hours sweep wall** on a Ryzen 5950X workstation
  (Windows). Significantly under the parent plan's ~20-hour estimate
  because the Phase 5/6 math turned out to be much faster than the
  parent plan assumed (panel ops are polars-native bulk operations,
  not per-cell volume-events backtests).

## Commit history

The full program is reconstructible from the git log:

- `ef9d3aa` research(scaffolding): code changes 1-3 (rank-direction flag,
  sweep parallelism, legacy-archive manifest builder)
- `ed7c5d8` research(signal-harness): code change 4 (signal_harness module,
  20 features, IC, portfolio CLI)
- `54f7163` research(phase-0): pre-reg LOO audit + orchestrator + shared
  sweep runtime
- `8d7e1de` research(phase-0): verdict REJECTED (0 candidates, 3 falsifiers)
  + Windows dispatch fixes + Phase 1/2/5/7 prep
- `a5b7c05` research(phase-1): verdict H1 FALSIFIED on baseline pair
- `b90b07c` research(phase-5): verdict 5 survivors triggers Phase 6 + bug fixes
- `9bd99e7` research(phase-6): verdict REJECTED (0 candidates, H7 falsified)
- _(this commit)_ research(phase-2 + program): verdict REJECTED + final
  end-of-program verdict (documented null)

## Forward operational state (unchanged from program start)

- **Live demo** (Singapore VPS 5.223.42.109): event_demo_daemon +
  ws_risk_daemon + long_native_event_demo_daemon under systemd.
  Frozen promoted profile, unchanged.
- **Paper shadow**: data/bybit-paper-event, unchanged.
- **No mainnet trading.** `REAL_MONEY` flag remains off.

## What's NEXT (operator decision)

Several non-trivial options are open as separate research programs;
NONE are auto-triggered:

A. **Test H2 honestly under a relaxed filter stack** — separate
   pre-reg, distinct from this program. Risk: data-mining; ROI
   uncertain given H1/H5 already suggest the existing improvement-
   direction filter stack is non-trivially load-bearing.

B. **Investigate the H6 → H7 gap** — the univariate IC signal is real
   but doesn't translate into combined-portfolio edge. Possible angles:
   (1) re-implement Phase 6 with proper holding-period accounting,
   (2) test signed-flow / order-book features (currently out of scope
   per parent plan), (3) build an ML signal combiner.

C. **Test the 3 near-no-op filters' joint removal** — fresh pre-reg
   with the same +0.5 Sharpe Δ / -5pp DD Δ bar. Phase 0 only tested
   leave-one-out; joint removal might (or might not) clear the bar.

D. **Backfill Binance OI history** so oi_delta_7d and oi_to_adv can
   be Phase-5-tested cross-venue. Currently Binance OI is only
   ~33 days; not enough for IC validation.

E. **Do nothing.** The most consistent reading of the program: the
   current strategy is in a local optimum. Forward-demo + paper-shadow
   evidence accumulates; at some point that becomes the only clean
   evidence surface left, and that's where promotion to mainnet
   would be argued from.

The default — per the parent plan's pre-commitment #5 — is **(E) do
nothing**. The strategy stays in its current state. Forward demo +
paper continue. No mainnet conversation until/unless a new pre-reg
opens a new research program with a clear hypothesis to test.

## Run-label

`exploratory` for every cell in the program. ZERO cells reached
`candidate` status. The forward-demo `paper_ready` ladder is unchanged
from program start.
