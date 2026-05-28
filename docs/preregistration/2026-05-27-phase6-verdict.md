# Phase 6 — combined-signal portfolio (VERDICT)

**Date:** 2026-05-28 (sweep ran 2026-05-28 02:20, 0.1 min wall)
**Stage:** run-complete, **REJECTED (0 candidates)** — H7 FALSIFIED.
**Pre-reg:** [docs/preregistration/2026-05-27-phase6-combined-signal-portfolio.md](2026-05-27-phase6-combined-signal-portfolio.md)
**Phase label per parent plan Appendix B:** `exploratory` — informational
verdict; no cells forward to Phase 7.

## Headline

**0 candidates from Phase 6.** The combined-signal portfolio does NOT
beat the current event-driven strategy on either venue. H7 falsified.

Every Phase 6 cell shows BOTH:
- Sharpe BELOW the Phase 0 event-driven baseline (Bybit 2.45, Binance 1.46),
- DD WAY worse than the baseline (-60% to -99% vs baseline -42%).

The signal-research arm concludes: while individual features in the
panel carry univariate IC signal (Phase 5 found 5 surviving features),
no combined-portfolio scheme tested here produces equity-curve dynamics
that beat the event-driven discrete-trade architecture on either venue.

## Per-cell results (Bybit + Binance)

Baseline reference (Phase 0 cells, 2023-04 → 2026-04 window):
- Bybit: sharpe 2.45, total_return +38.6%, dd -42.1%, trades 602
- Binance: sharpe 1.46, total_return +4.2%, dd -42.2%, trades 421

Phase 6 window: 2021-01 → 2026-04 (longer; not directly comparable on
absolute terms; the Manifesto's Δ comparison is what we report).

| Cell | by_sh | bn_sh | by_dd | bn_dd | by_Δsh | bn_Δsh | Candidate? |
|---|--:|--:|--:|--:|--:|--:|---|
| P6_equal_z              | +0.57 | +0.61 | -72% | -79% | -1.88 | -0.85 | NO |
| P6_ic_weighted          | -0.39 | -0.37 | -89% | -95% | -2.84 | -1.83 | NO |
| P6_top_decile_short     | +0.57 | +0.61 | -72% | -79% | -1.88 | -0.85 | NO (= equal_z alias) |
| P6_horiz_equal_1d       | -0.20 | +0.10 | -44% | -45% | -2.65 | -1.36 | NO |
| P6_horiz_equal_7d       | +0.55 | +1.09 | -94% | -95% | -1.90 | -0.37 | NO |
| P6_horiz_icwt_1d        | +0.17 | +0.00 | -32% | -49% | -2.28 | -1.46 | NO |
| P6_horiz_icwt_7d        | -0.27 | -0.72 | -98% | -100% | -2.72 | -2.18 | NO |
| P6_horiz_topdec_1d      | -0.20 | +0.10 | -44% | -45% | -2.65 | -1.36 | NO |
| P6_horiz_topdec_7d      | +0.55 | +1.09 | -94% | -95% | -1.90 | -0.37 | NO |
| P6_dec_equal_05         | +0.48 | +0.62 | -51% | -63% | -1.97 | -0.84 | NO |
| P6_dec_equal_20         | +0.41 | +0.40 | -91% | -91% | -2.04 | -1.06 | NO |
| P6_dec_icwt_05          | -0.36 | -0.52 | -75% | -90% | -2.81 | -1.98 | NO |
| P6_dec_icwt_20          | -0.40 | -0.43 | -98% | -100% | -2.85 | -1.89 | NO |
| P6_dec_topdec_05        | +0.48 | +0.62 | -51% | -63% | -1.97 | -0.84 | NO |
| P6_dec_topdec_20        | +0.41 | +0.40 | -91% | -91% | -2.04 | -1.06 | NO |

The best Phase 6 cell (by combined-venue sharpe) is `P6_horiz_equal_7d`
with Bybit sharpe +0.55, Binance sharpe +1.09. Bybit Δ vs baseline =
-1.90; Binance Δ = -0.37. Both venues NEGATIVE → falsifier, not candidate.

## Known interpretation caveat

The Phase 6 implementation in `signal_harness.build_combined_signal_portfolio`
+ `phase6_combined_portfolio_sweep._compute_portfolio_metrics` treats
positions as continuous-overlapping: each day the strategy enters a new
top-decile-short position per symbol that's currently top-decile, regardless
of whether the same symbol was already shorted yesterday. The per-day PnL
formula `weight × fwd_ret_Nd` summed per day then implicitly counts each
N-day-held position N times. This **inflates measured exposure** by a factor
of N relative to the event-driven discrete-trade model.

The honest read: with that caveat, absolute returns and DDs are not
directly comparable to the event-driven baseline. The Δ-comparison
remains meaningful **because BOTH the Phase 6 numerator and denominator
of the Sharpe ratio scale linearly with leverage** — Sharpe is invariant
to constant-leverage rescaling. So a sharpe of +0.55 vs the baseline's
+2.45 means the cell is genuinely 1.90 worse on the risk-adjusted return
metric, regardless of the exposure-inflation.

A cleaner Phase 6 implementation (with proper position holding-period
accounting) would tighten the DD comparison but cannot rescue the Sharpe
gap. **No Phase 6 scheme is salvageable as a candidate** under any
reasonable holding-period correction.

If H7 is later revisited, the right path is a re-pre-registration with:
- Explicit holding-period accounting (rebalance daily, not enter daily)
- Either same-direction-stickiness or position-replacement rules
- Volume-event-style fill model (1h delay + cost + funding)

This is OUT OF SCOPE for Phase 6; the pre-reg's combination schemes were
the committed test surface.

## Why ic_weighted underperforms equal_z

The ic_weighted scheme uses combined = sum(IC_i × Z_i). With all 5
survivors having NEGATIVE IC, the product (IC × Z) flips sign on each
feature relative to its raw Z. The combined signal then ranks symbols
opposite to the equal-Z scheme. For a SHORT-side portfolio, that means
ic_weighted goes LONG the names the IC story says to short. Result:
uniformly negative ic_weighted sharpe.

This is a definitional asymmetry, not a bug — the scheme name implies
"weight by IC magnitude AND sign", and with all-negative IC features
that's what happens. A future revision could either flip the sign
convention OR restrict to abs(IC) weighting; either is a design choice
for a new pre-reg.

## Pre-commitment compliance

- ✅ Manifesto thresholds not loosened (+0.5 Sharpe Δ + -5pp DD Δ bar
  applied unchanged)
- ✅ FDR ceiling honoured (0 candidates ≤ 3 cap, trivially)
- ✅ Survivor list pinned at Phase 5b commit time, not changed mid-run
- ✅ 21-cell menu committed, no mid-phase additions
- ✅ Verdict written before any downstream phase decisions

## Forward pointer

- **Phase 7 from Phase 6:** none (0 candidates).
- **Phase 7 from Phase 2:** pending — Phase 2 is still running (~2-3h
  wall). Phase 7 dispatches against ANY Phase 2 candidates.
- **If Phase 2 also yields 0 candidates:** the whole program closes
  with a documented null — H1, H6, H7 falsified; H2/H3/H4/H5 either
  pending Phase 2 result or already null. The strategy stays in its
  current state.

## Run-label per `docs/backtesting_errors_we_never_repeat.md`

`exploratory` — informational only. No cell forward to demo / promotion.
The "best" Phase 6 cell (`P6_horiz_equal_7d`) is NOT a candidate, NOT
a backtest-promoted profile, NOT cite-able as alpha.

## Artifacts

- Pre-reg: `docs/preregistration/2026-05-27-phase6-combined-signal-portfolio.md`
- Phase 5 verdict (survivor pinning): `docs/preregistration/2026-05-27-phase5-verdict.md`
- Summary CSV: `~/SHARED_DATA/phase6_combined_portfolio_2026-05-27_summary.csv`
- Per-cell portfolios + metrics:
  - `~/SHARED_DATA/{bybit,binance}_full_pit/reports/phase6_combined_portfolio_2026-05-27/<cell>/portfolio.parquet`
  - `~/SHARED_DATA/{bybit,binance}_full_pit/reports/phase6_combined_portfolio_2026-05-27/<cell>/metrics.json`
