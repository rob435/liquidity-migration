# Pre-registration: DR1 Stage-A — hedged down-regime bounce (standalone product)

**Date:** 2026-06-10 (registered BEFORE the run). **Label:** `exploratory` Stage-A
(vectorized daily simulator; an engine-grade build + forward demo follow only on a
PASS — never direct deployment).

**Authority / door:** operator directive 2026-06-10 ("build a system that trades
during BTC-30d-down regimes since the continuous doesn't; full authority"). This
REOPENS the downtrend question along exactly the path the D3 close-out anticipated:
*"If the operator ever wants a standalone high-vol bounce book, it is a separate
product with its own risk budget, not a sleeve of this system."* This is that
separate product. The D3 close-out's combined-book framing is NOT relitigated.

**Window-spent posture:** the 2023-04→2026-05 window is frozen (2026-06-09). This run
introduces ZERO new mined parameters: the sleeve cells are the two D3
standalone-bar-passing cells verbatim; the hedge construction and all its parameters
are the WP3 Stage-A banked values verbatim. One pre-stated falsifiable hypothesis;
2 cells × 2 venues; no grid, no amendments, no off-menu rescue cells.

## Hypothesis (single, falsifiable)

The D3 bounce sleeve's disqualifying drawdown class (−30..−44%) is mostly HEDGEABLE
MARKET BETA (long falling-knife alts inside a falling BTC regime), not alpha risk. A
causal trailing-beta SHORT-BTC hedge — the exact mirror of the banked WP3 long-BTC
hedge on the short book — moves the sleeve into a deployable risk class while
preserving the verified bounce alpha (D3: Sharpe 2.03 bybit / 0.81 binance, net
+266% / +48%, funding-positive).

## Strategy (fixed a-priori; every element inherited)

On BTC-30d-down days (sign of BTC return over [d-31, d-1], causal through d-1 —
identical to D1/D2/D3 and the engine's btc_trend_gate direction):

- **Sleeve (D3 verbatim):** LONG the bottom decile of `ret_7d` among non-BTC,
  non-stable USDT perps with turnover(d-1) ≥ liq floor; equal weight; k=1 daily
  tranche; sleeve gross 0.5× equity; regime flip to UP closes the sleeve next day;
  costs 12 bps round-trip on traded notional; REAL per-symbol funding on the longs
  (longs pay positive rates, receive negative).
- **Hedge (WP3 mirror, the ONLY new element):** H(d) = clip(beta, 0, 2.0) where beta
  is OLS beta of the sleeve's UNHEDGED net daily return vs BTCUSDT daily return over
  the trailing ≤90 ACTIVE sleeve days strictly before d, min 60 obs (H=0 until warm —
  honest cold start, same convention as WP3). Hedge leg = SHORT BTCUSDT at H(d) ×
  equity units: pnl −H·r_btc, REAL BTC funding (short receives positive rates),
  5 bps on hedge turnover |ΔH|. Regime flip → H→0, turnover charged.

## Declared cells (final menu — no additions)

`liq500k_k1`, `liq2m_k1` (the two D3 standalone passers) × {unhedged control, hedged}
× {bybit, binance}. Sensitivities on the hedged cells only: 2× cost (sleeve 24 bps RT
+ hedge 10 bps), funding-off attribution, beta_extra_lag_days=1 latency variant,
per-year split (years with >60 regime days), leg attribution (gross alt vs hedge pnl).

## Metric conventions (pre-stated)

- Sharpe / DD: on the active-day (in-regime) compounded series — D3's convention, so
  numbers are directly comparable to the D3 receipt.
- MAR: full-calendar (flat days = 0) annualized return ÷ |full-calendar max DD| —
  the deployment view for a standalone product.

## A-priori bars (ALL must hold for the SAME cell id on BOTH venues)

1. **Risk-class transformation (the hypothesis):** hedged active-day max DD ≤ 2/3 of
   the unhedged cell's DD, AND hedged DD ≤ 20% absolute.
2. **Alpha preserved:** hedged net return > 0 and hedged Sharpe ≥ 0.8 (both venues).
3. **MAR-primary product bar:** hedged full-calendar MAR > unhedged MAR on both
   venues; each venue hedged MAR ≥ 0.5; venue-mean hedged MAR ≥ 1.0.
4. **Cost robustness:** 2× cost keeps net return > 0 on both venues.
5. **Not a disguised BTC short:** in leg attribution the alt longs' gross must exceed
   the hedge leg's net contribution over the window on both venues (the alpha source
   is the bounce, the hedge is insurance).
6. **Latency:** beta_extra_lag_days=1 must not flip bars 1–3 (reported; a flip =
   FAIL on robustness grounds).

PASS → DR1 becomes the design basis for an engine-grade standalone down-regime
product (own risk budget, paper/demo first, Tier rules apply to any promotion; the
rmom-style latency and impact debts carry over and must be addressed at engine
stage). FAIL → the downtrend question RE-CLOSES on the D3 terms (hedge + cash), and
the close-out language gains "hedged-bounce synthesis tested and failed" so it is
never re-mined.

## Artifacts

`C:/Users/user/SHARED_DATA/downtrend_hedged_bounce_2026-06-10/` — report JSON + daily
series per cell. Script: `scripts/downtrend_hedged_bounce.py` (extends the D3
simulator; D3 numbers must reproduce as the unhedged control within float tolerance).

## Verdict (filled in after the run, same day) — FAIL; hypothesis falsified

**FAIL on the pre-registered bars — 0 of 2 cells pass; the hedge does not transform
the risk class.** Controls reproduce the D3 report to the digit (harness verified:
+266.46%/2.03/−29.60 and +135.14%/1.39/−43.91 bybit; +47.56%/0.81/−35.58 and
+52.49%/0.86/−35.43 binance).

| cell / venue | control DD → hedged DD | control net → hedged | Sharpe | MAR full-cal |
|---|---|---|---|---|
| liq500k_k1 bybit | −29.60 → −27.85 (bar: ≤ −19.7) | +266.5 → +217.7 | 2.03 → 2.21 | 2.72 → 2.36 |
| liq500k_k1 binance | −35.58 → −33.48 | +47.6 → +30.7 | 0.81 → 0.72 | 0.41 → 0.28 |
| liq2m_k1 bybit | −43.91 → −41.01 | +135.1 → +106.8 | 1.39 → 1.41 | 0.93 → 0.79 |
| liq2m_k1 binance | −35.43 → −33.21 | +52.5 → +34.8 | 0.86 → 0.77 | 0.46 → 0.32 |

The hedge was REAL (mean H ≈ 0.70, max 0.92, 447 hedged days on bybit liq500k) yet
moved DD by only ~2pp everywhere while dragging ~−19..−21% gross (funding gives back
only +3.8/+3.9, cost −3.5). Bar 1 (DD ≤ 2/3 of control AND ≤ 20%) fails on all
cells/venues; bar 3 (MAR must improve) fails everywhere; binance bar 2 fails
(Sharpe 0.72/0.77 < 0.8). Lag-1 and 2× cost change nothing material. Funding-off
attribution confirms the D3 finding that bear-funding receipts are a large share of
sleeve net (bybit liq500k +266→+132 without funding) but DD is funding-invariant.

**Why (mechanism, two parts):** (1) the sleeve's drawdown is alt-idiosyncratic
crash/continuation risk in the falling-knife decile, not index beta — a 90d trailing
beta cannot see or remove it; (2) the regime gate is TRAILING — conditional on
30d-down, forward daily BTC drift in this window is mildly positive (rebound days
inside bear regimes), so a standing short-BTC overlay inside the regime is a
systematically losing position (hedge gross ≈ −20% per venue-cell).

**Disposition (per the pre-registered FAIL clause):** the in-sample downtrend
question RE-CLOSES on the D3 terms — and the close-out language now includes:
**the hedged-bounce synthesis was tested and failed; do not re-mine it.** Any
down-regime product built from the verified D3 bounce alpha must carry its native
−30..−44% drawdown class UNHEDGED as an operator risk-budget decision (the D3
"separate product" clause), with forward paper evidence as the only path forward —
see `downtrend-bounce-forward-paper-2026-06-10.md`.
