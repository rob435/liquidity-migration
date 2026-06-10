# Pre-registration: IR1 Stage-A — intraday residual-reversal scout (hourly cadence)

**Date:** 2026-06-10 (registered BEFORE the run). **Label:** `exploratory` Stage-A
scout — gates an engine DESIGN, never deployment. **Authority:** the 2026-06-09
"intraday-class harvest" proposal (research_summary) was explicitly operator-gated
("nothing runs without operator greenlight + a fresh pre-registration"); the
operator's 2026-06-10 blanket directive ("you can do anything you want") is that
greenlight; this file is the fresh pre-registration.

## Premise (verified, not conjecture)

The residual-reversal/rmom family signal is REAL but lives at 23–47h staleness and
is dead past 47h (latency falsification 2026-06-09, grid audited). A
daily-rebalanced system structurally cannot harvest a <2-day-half-life signal —
that is WHY the daily/continuous attempts failed, not because the idio signal is
absent. The only untried shape: hourly decision cadence, holds inside the decay
window. New-system-class work, not window re-mining (the freeze's sanctioned
exception alongside new data layers).

## Stage-A construction (fixed a-priori; deliberately simple)

Both venues, hourly `klines_1h`, window 2023-04-01→roots end. Liquidity floor:
trailing-24h turnover ≥ $500k; non-BTC/ETH, non-stable; ≥30d listing history.

- **Residuals (single-factor scout version, declared):** r_i(h) − β_i·mkt(h),
  mkt = EW alt-market hourly return; β_i = trailing 168h (7d) cov/var, causal,
  min 100 obs. (The day-grid work used cross-sectional OLS; this trailing-beta
  form is the cheap causal scout equivalent — an engine build would use the
  audited factor machinery.)
- **Signal at hour h:** trailing-24h residual sum (`rmom24`), cross-sectional
  rank among eligible names.
- **Outcomes:** forward 6/12/24h raw and residual returns (next-hour-open-proxy =
  next hourly close entry; declared).
- **Stats per venue:** hourly Spearman IC (signal vs forward residual), day-block
  bootstrap p (seed 20260610, 500 reps); decile D1−D10 spreads; a COSTED read:
  short the bottom rmom24 decile (the P3 sign: idio-weak continues down),
  non-overlapping 12h and 24h holds, 45 bps round trip.
- **Staleness check (the design premise must show up):** IC of the same signal
  lagged 24h and 48h — fresh IC must exceed 24h-stale, and 48h-stale must be ≈0
  (consistent with the verified decay physics). If staleness does NOT decay, the
  premise is wrong and the result is suspect regardless of IC.

## GO bar (engine design proposed only if ALL hold)

1. Fresh |IC| ≥ 0.03, p < 0.05, SAME sign both venues, at ≥2 of the 3 horizons.
2. Costed short-decile net > 0 on BOTH venues at 45 bps for at least one declared
   hold (12h or 24h, same hold both venues).
3. Staleness decay pattern as above on both venues.
4. The economics must clear the documented hurdle: per-trade net ≥ ~2× the daily
   system's per-trade net is the engine-stage bar; Stage-A reports the number.

FAIL → record; the intraday-class idea returns to the shelf with its physics
documented; no scout variants (different windows/factors/horizons) without a new
receipt. Multiple-testing posture: ONE signal form, 3 horizons, 2 venues, declared
sign; the cross-venue + staleness requirements are the guard.

## Artifacts

`C:/Users/user/SHARED_DATA/intraday_residual_scout_2026-06-10/` — report JSON +
per-venue event/decile parquets. Script: `scripts/intraday_residual_scout.py`.

## Verdict (filled in after the run) — bars 1+3 PASS strongly; declared costed form FAIL; sign discovery → Amendment 1

- **Bar 1 PASS (emphatic):** IC(rmom24, fwd residual) = −0.032/−0.036/−0.038 at
  6/12/24h on bybit, −0.030/−0.034/−0.036 on binance, p = 0.0 (day-block
  bootstrap), ~28k hourly cross-sections each. Same sign both venues at ALL
  horizons. The sign is NEGATIVE = REVERSAL — idio-losers bounce, idio-winners
  fade — matching the proposal's own name ("residual-reversal"); this receipt
  mis-declared the costed trade as continuation (P3's sign is event-conditional
  on liquidity-migration candidates, not universe-wide).
- **Bar 3 PASS:** staleness decay reproduces the audited physics on both venues
  (fresh −0.038 → 24h-stale −0.009 → 48h-stale −0.003 bybit; −0.036 → −0.009 →
  −0.004 binance). The signal is real, fast-decaying, and harvestable only at
  intraday cadence — the design premise is CONFIRMED.
- **Bar 2 FAIL as declared:** the declared short-bottom-decile read is decisively
  negative (−25/−29 bps per 12h period) — it shorts the bouncers. With the IC
  sign now established, the economic form to cost is the MIRROR.

**Amendment 1 (pre-registered HERE, before computation; multiple-testing debit:
this is the SECOND costed form tested on the same scout):** costed reads, same
panel/holds/costs (45 bps RT, funding-blind FLAGGED): (a) LONG bottom rmom24
decile; (b) SHORT top decile; (c) the 50/50 L/S. Amended bar 2: form (a) or (c)
net > 0 on BOTH venues at the same declared hold, with per-period economics
reported against the engine-stage hurdle (≥ ~2× the daily system's per-trade
net). All other bars unchanged. FAIL → IR1 closes (signal real, not harvestable
at our cost stack — the alpha-hunt graveyard pattern); no further forms.

## Amendment 1 verdict — FAIL; IR1 CLOSES

The gross reversal spread is REAL and matches the ICs: bottom decile outperforms
top by +13.4/+31.8 bps per 12h/24h (bybit) and +12.7/+24.8 (binance) GROSS. But
the entire alt cross-section drifts down in raw terms over the window, so:
(a) long_bottom −64.6/−79.3 bps per period (bybit 12h/24h), −61.4/−72.0 binance —
decisive FAIL; (c) L/S −38.3/−29.1 and −40.0/−32.6 — decisive FAIL (gross spread
is 3.4×/1.4× SMALLER than the 45 bps cost). (b) short_top is the only positive
cell (+21.0/+6.8 bps at 24h, Sharpe 0.89/0.29) — not a bar form, feeble, and the
documented funding-trap leg (i1b: funding eats ~85% of short-side intraday edge;
funding NOT modeled here, would make it worse).

**Engine-stage hurdle check (bar 4):** needed ≥ ~30 bps NET per trade; achieved
−30..−65 bps net. Off by an order of magnitude.

**CLOSED per the pre-registered clause.** The intraday-class proposal's killer-risk
caveat was exactly right: the universe-wide hourly residual-reversal edge is real,
fast-decaying (physics CONFIRMED on both venues — the scout's lasting value), and
~3–5× too small GROSS to pay taker costs on thin alts. Only conceivable revival,
recorded not proposed: maker-execution economics (~10–15 bps) after R4 fill
calibration, where the 24h spread (~25–32 bps gross) would be marginal at best.
No scout variants, no engine build, no further forms on this window.
