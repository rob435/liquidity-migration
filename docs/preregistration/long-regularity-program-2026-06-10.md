# Pre-registration: LR — long-sleeve regularity program (frequency + DD balancing)

**Date:** 2026-06-10 (registered BEFORE the runs). **Label:** `exploratory`; any
adoption requires the pre-stated bar below and lands as a config-profile change with
this receipt (volup125 precedent). **Authority:** operator directive 2026-06-10
("the long system looks like a step function — concentrated months, not balancing
drawdown; make it better, maybe more frequent like the continuous system; full
authority").

**Posture.** The 2026-06-09 program already established: (a) the step-function look
is substantially exit-booking rendering — honest daily-MTM is MAR 2.38 / DD −8.5% /
active 26% of days (bybit); (b) clean nulls not to re-run: breadth widening,
hold extension, trailing/scaled exits, pyramiding, TSMOM blends; (c) FC is the
selection ceiling — this program varies STRUCTURE around the deployed signal, not
new signal families. The window (2023-04→2026-05) is spent for mining; menus here
are minimal and pre-stated. The operator's two asks map to three pieces:

## Declared cells (engine-grade, exact deployed v11a+div+volup125 profile, research
gates ON, window 2023-04-01→2026-05-28, both venues; no additions)

- `00_baseline` — control (deployed profile verbatim).
- `10_best2` — `fc_daily_best_n=2` (same-day FOMO-cluster throttle, existing knob).
- `11_best3` — `fc_daily_best_n=3`.
  *Rationale (DD balancing):* the 10x stress + regime-concentration work identified
  episodic same-day FOMO concentration as the book's correlated-crash tail; a per-day
  entry cap trims exactly that tail. Untested in the 2026-06-09 menu.
- `20_intra6` — `fc_enable_intraday_trigger=True` (6h window, engine default).
  *Rationale (frequency):* fires on intraday pumps that faded by the daily close
  (no close-location requirement on this trigger) — more events at the same daily
  cadence. CAVEAT pre-stated: this knob shipped with the v11a research wave and was
  not selected into v11a; no recorded verdict exists either way. Adoption therefore
  requires the STRICT bar below, not a marginal pass.

## LR-scout (vectorized, descriptive — NOT a promotion cell)

Hourly-cadence FC detection feasibility ("like the continuous system"): on
`klines_1h`, at each hourly close compute trailing-24h return, trailing-24h
close-location, 24h-turnover rank, regime gates causal through the prior daily
close. Measure: trigger-event count vs the daily grid, overlap, time-advantage on
shared events, and costed forward returns (entry next hourly open, +24/48/72h
exits, 12 bps RT) of (i) shared events entered earlier and (ii) extra events.
**GO bar for proposing an engine build (proposal only, operator-gated):** extra
events ≥ +30% of the daily count AND extra-event mean net forward return ≥ 50% of
the daily events' per-trade net, on BOTH venues, at every tested horizon.

## A-priori adoption bars

Metrics from the engine reports: ret/DD (ordering), total return, worst exit-day,
trade count, per-year splits; honest daily-MTM Sharpe/DD recomputed for the adopted
cell. `run_label` must be clean full-PIT on every cited run.

1. **Throttle (pick at most ONE of best2/best3):** on BOTH venues ret/DD ≥ 1.10×
   baseline AND worst exit-day not worse AND total return ≥ 0.75× baseline AND
   trades ≥ 0.6× baseline. Tie-break pre-committed: the milder throttle (best3).
2. **Intra trigger:** on BOTH venues trades ≥ 1.20× baseline AND ret/DD ≥ baseline
   (no pooled MAR give-up) AND the ADDED trades (ledger diff vs baseline by
   symbol+entry time) carry positive net P&L on each venue. 2× fee/slippage
   stress on the added-trade stream (post-hoc from ledger cost columns) must stay
   net-positive pooled.
3. Any passing cell: per-year splits must not introduce a new always-negative year.
4. FAIL on all → the long sleeve's structure stands; the honest answers to the
   operator are the MTM rendering + book-level composition with the short; record
   and close.

Combined diagnostics (reported, not bars): corr of the adopted long stream vs the
deployed short's daily series; behavior on the short's 20 worst days.

## Artifacts

`<root>/reports/long_regularity_2026-06-10/<cell>/` per venue (engine reports +
ledgers); scout artifacts under `C:/Users/user/SHARED_DATA/long_hourly_scout_2026-06-10/`.
Driver: `scripts/long_improve_sweep.py` (cells added under this receipt);
scout: `scripts/long_hourly_fc_scout.py`.

## Verdict (filled in after the runs) — ALL cells + scout GO bar FAIL; close per §bars(4)

All runs clean `full_pit_universe`, window 2023-04-01→2026-05-28.

| cell | bybit tr/ret/DD/retDD | binance tr/ret/DD/retDD | verdict |
|---|---|---|---|
| 00_baseline | 192 / +28.9% / −3.5% / 8.30 | 195 / +22.8% / −3.9% / 5.90 | control |
| LR10_best2 | 170 / +16.1% / −2.7% / 6.02 | 176 / +14.9% / −2.6% / 5.67 | FAIL (retDD drops) |
| LR11_best3 | 185 / +23.3% / −2.7% / 8.63 | 186 / +18.9% / −3.1% / 6.12 | FAIL (1.04×/1.04× < the 1.10× bar; directionally consistent but below bar — NOT adopted, no rescue) |
| LR20_intra6 | 433 / +18.0% / −9.7% / 1.85 | 427 / +5.0% / −12.2% / 0.41 | FAIL decisively |

Scout (hourly cadence): shared events strongly positive (+8.3/+10.8/+11.0% bybit,
+5.9/+8.0/+6.1% binance mean net at 24/48/72h); EXTRA events negative at every
horizon on BOTH venues (−0.8..−2.9%) → GO bar FAIL.

**The load-bearing finding, now confirmed three independent ways (intra6 engine
cell, scout extra-class, and the v11a-era non-selection): the daily-close
confirmation IS the FC signal — pumps that cannot hold to the daily close are
distribution.** "More frequent" via more events is closed by evidence on this
window. The baseline diagnosis stands as the answer to the operator's complaint:
active 27%/26% of days, top-3 months = 81%/76% of total net — that concentration is
the signal's nature, and every densification lever tested (six constructions across
two programs) pays more than it returns.

Follow-on receipt: `long-provisional-entry-2026-06-10.md` (PE1, execution timing —
FAILED its bar 2 on binance but recorded as the one strong evidence-backed lead).
Shipped from this program instead of a strategy change: the honest daily-MTM
rendering (`long_native_equity_mtm.{csv,png}` + `equity_mtm` report block) — the
step-function LOOK was substantially exit-booking rendering (2026-06-09 finding,
now a first-class artifact).
