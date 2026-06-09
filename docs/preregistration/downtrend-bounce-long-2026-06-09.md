# Pre-registration: D3 Stage-A — downtrend bounce-long sleeve (attribution-driven revisit)

**Date:** 2026-06-09 (registered BEFORE the run). **Label:** `exploratory` Stage-A.
**Door:** the D2 FAIL disposition ("a long-only bounce sleeve ... needs its own dated
pre-registration"). **Motivation (from declared D2 attribution, not grid mining):** the
LONG leg carried real gross alpha on BOTH venues (+135% bybit / +58% binance over ~505
regime days); the short leg + its funding bill killed the L/S. A long-only sleeve keeps
the working leg, RECEIVES negative bear funding, and halves turnover — and it deploys
capital on exactly the days the main book is off.

## Strategy (fixed a-priori)

On BTC-30d-down days (causal through d-1): LONG the bottom decile of `ret_7d` among
non-BTC, non-stable names with turnover(d-1) >= liq floor; equal weight; sleeve gross =
0.5x equity. Tranche holds spread entries (a tranche formed at d-1 close is held k
days); regime flip to UP closes the whole sleeve next day. Costs 12 bps round-trip on
traded notional; REAL per-symbol funding on the longs (negative rates = receive).

## Declared cells (final menu for this arc — no further amendments)

liq ∈ {500k, 2M} x hold k ∈ {1, 2, 3} = 6 cells/venue. Sensitivities on the best cell
only: 2x cost; funding-off attribution; per-year split.

## A-priori bars

Standalone (in-regime): net return > 0 AND Sharpe >= 0.8 on BOTH venues for the SAME
cell id. Combined-book (the goal metric): stitching the sleeve's regime-day returns
into the hedged-max4 deployable's calendar must give pooled MAR >= baseline + 0.5 with
BOTH venue deltas positive, DD worse by <= 1.5pp per venue, and survive 2x cost at
both levels. The +30% goal check (pooled >= 7.25) is reported against the combined
book. FAIL -> the downtrend question is CLOSED for this program: hedge + cash is the
final answer; no further downtrend constructions.

## Artifacts

`~/SHARED_DATA/downtrend_bounce_long_2026-06-09/` — cells + report JSON.
Script: `scripts/downtrend_bounce_long.py`.

## Verdict (filled in after the run, same day) — FINAL, closes the downtrend question

**FAIL on the combined-book bar; the pre-registered close-out clause applies.**
The attribution thesis was CONFIRMED mechanically: longs RECEIVE bear funding
(+7.7..+33.6% over the window), costs halve, and two cells pass the standalone bar
(liq500k_k1: Sharpe 2.03 bybit / 0.81 binance, net +266%/+48%; liq2m_k1: 1.39/0.86).
But the sleeve is a -30..-44% drawdown object: stitched into the deployable
(-5.4%/-4.2% DD budget), binance combined MAR collapses 5.64 -> 1.3-1.6 (delta -4)
and even bybit's best cell (+1.75 MAR) carries combined DD -25.2% vs the <=1.5pp-worse
bar. No cell passes. Down-sizing sweeps would be post-hoc mining outside the declared
final menu — not run.

**The downtrend question is CLOSED for this program: the bounce alpha is real but
belongs to a different risk class; downtrend capital = BTC-hedge leg + cash.** If the
operator ever wants a standalone high-vol bounce book, it is a separate product with
its own risk budget, not a sleeve of this system.
