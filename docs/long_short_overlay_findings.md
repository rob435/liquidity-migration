# Long-on-Short Diversification Overlay — Findings & Forward-Test Plan

**Status:** EXPLORATORY (portfolio-construction diagnostic on existing ledgers).
**Date:** 2026-05-29. **Tooling:** `scripts/combine_sleeves_analysis.py`.

## TL;DR

The v11a long sleeve's real value is **not** as a standalone return engine — an
exhaustive 7-wave search ([[long-sleeve-alpha-search-null]]) showed its FC signal
is the quality ceiling and can't be beaten on both venues. Its value is as a
**low-correlation overlay on the short book**: the two sleeves fire in opposite
regimes, so combining them lifts the *combined* book's risk-adjusted return with
no new signal mining.

## Method

Additive sleeve overlay (matches `portfolio_hedge.run_portfolio_hedge_report`):
`combined_daily = short_return + w · long_return`, aligned by exit-date over the
overlap window, measured with calendar-day-aligned Sharpe and MAR (both
leverage-invariant, so they isolate the diversification benefit from gross-exposure
effects). Short baseline = the promoted volume-events book; long = v11a FC sleeve.

## Results (cross-venue)

| Venue | short↔long corr | short-alone (Sharpe / MAR) | + long w=0.5 | + long w=1.0 |
|---|---:|---|---|---|
| Binance | **−0.045** | 1.17 / 1.02 | 1.34 / 1.21 | **1.48 / 1.41** |
| Bybit | **−0.022** | 2.32 / 5.46 | 2.40 / 5.91 | **2.47 / 6.39** |

- Near-zero/negative correlation on both venues (the sleeves are temporally
  complementary: long fires only in BTC-up regimes ~7% of days; short fires on
  crowded-fade/crash days).
- The long leg adds **+2.7% (Binance) / +4.4% (Bybit) on the short book's
  worst-20 days** — it pays off precisely when the short bleeds.
- At w=1.0: combined **MAR +38% (Binance) / +17% (Bybit)**, Sharpe +0.31 / +0.15,
  max drawdown flat-to-better. Combined Sharpe approaches `sqrt(S_short²+S_long²)`
  — textbook uncorrelated-portfolio behaviour.

## Integrity caveats (read before trusting magnitudes)

- The long sleeve's standalone return is a **2023–2026 in-sample** phenomenon
  (FC fails pre-2023 OOS; the dedicated OOS roots are spent). So the *magnitude*
  of the uplift is optimistic.
- The **robust** part is the correlation / variance-reduction, which is
  structural (regime complementarity), not a tuned parameter — it holds even if
  the long sleeve's future return is only modestly positive.
- Do **not** over-size the overlay: high weights (w≥2) increasingly just bet the
  in-sample-fragile long edge. A **moderate overlay (w ≈ 0.5–1.0× short notional)**
  captures most of the benefit while keeping the book short-dominated.

## Forward-test plan

The combined book is **already deployed on demo** (commit `20d0ae0`, profile
`MultiStratV1`: short + v11a long sleeve, same account). Forward validation:

1. **Track the deployed combined-book ledger** — compare realized combined
   Sharpe/MAR/drawdown to the short-alone demo ledger over the forward window.
   The forward demo is the only pristine OOS surface (the backtest OOS is spent).
2. **Recommended sizing:** long notional ≈ 0.5–1.0× the short book's per-position
   notional (the deployment's owner-locked `notional_multiplier` governs this);
   the backtest does not support a more aggressive long tilt.
3. **Kill criterion:** if the forward combined book does not show lower drawdown
   than short-alone over ≥3 months, the diversification benefit is not surviving
   OOS and the long sleeve should be cut back to a token weight.

No live-deployment parameter is changed by this document; it records the finding
and the forward-validation rule. Related: [[long-sleeve-diversifier]].
