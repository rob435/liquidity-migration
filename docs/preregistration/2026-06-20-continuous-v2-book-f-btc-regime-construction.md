# Construction + Verdict: Continuous V2 Next-Level — Problem Book F (BTC Regime Sizing)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction + verdict
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Run label: `exploratory` (per-trade realized-PnL screen). **Verdict: NO both-venue candidate. BTC-vol regime book-timing is noise-dominated / venue-split; the regime stays on the hedge only. Book F CLOSED.**

## Objective

The book already has a BTC-UPTREND entry gate and a BTC-vol regime HEDGE intensity.
This book asks the new question: does a causal BTC-vol regime signal, applied to the
fade BOOK exposure (not just the hedge), time the exposure better than random?

## Method & the load-bearing constraint (from Book G)

Book G proved that an exposure control which merely lowers average gross is a leverage
effect, not a timing edge. So every F arm uses a **MEAN-1 (gross-neutral)** per-trade
multiplier keyed on the trade's ENTRY day (causal at sizing) — average gross unchanged,
so any MAR change is PURE TIMING — and must beat a **HASH-PERMUTED null** (same
multiplier distribution, regime labels shuffled).

`scripts/continuous_v2_book_f_btc_regime.py`, re-weighting V2_CONTROL trades:
- `F0_CONTROL` (mult 1.0) · `F2_DERISK_HIVOL` (scale down on high-BTC-vol entry days,
  mean-1) · `F2b_LEVER_HIVOL` (scale up, mean-1) · `F8_HASH_REGIME` (F2 multipliers
  hash-permuted). Intensity = the deployed `btcvol_intensity_series` (lam 0.5, vol_window
  30, pct_window 250). Realized-PnL MAR proxy, both venues. Hedge unchanged (this tests
  BOOK gross timing only).

## Results (full run 2026-06-20)

| arm | bybit MAR / Δctrl | binance MAR / Δctrl |
|-----|-------------------|----------------------|
| F0_CONTROL | 6.149 / — | 4.331 / — |
| F2_DERISK_HIVOL | 6.179 / **+0.03** | 3.997 / **−0.33** |
| F2b_LEVER_HIVOL | 4.188 / −1.96 | 3.889 / −0.44 |
| F8_HASH_REGIME | 4.807 | 4.532 |

## Verdict — no robust regime book-timing edge

- **No both-venue winner.** `F2_DERISK_HIVOL` (derisk the fade in high BTC vol)
  marginally beats control on **Bybit** (+0.03, within noise) and beats its hash null
  there (6.18 > 4.81) — a faint real timing. But on **Binance** it HURTS (−0.33) and
  **loses to its own hash null** (4.00 < 4.53): high-BTC-vol days are not systematically
  bad for the Binance fade, so random reweighting does better. Levering up in high vol
  (F2b) is strongly negative on both venues — so the only directional signal (mild
  derisk-in-stress) is Bybit-only and tiny.
- **The mean-1 book-gross reweighting at this dispersion is concentration-noise-
  dominated.** The hash null swings wildly by venue (bybit −1.34, binance +0.20 vs
  control), showing MAR is driven more by which heavy-weighted trades randomly land on
  which days than by the regime signal. A real edge would beat hash on BOTH venues; this
  does not.
- **The BTC-vol regime belongs on the hedge, not the book gross.** It is already wired
  there (causal, mean-1, both legs) and validated as forward-watch. Extending it to book
  exposure adds no robust two-venue value.

## Falsifiers applied

- Hash-permuted null (the load-bearing one): F2_DERISK loses to it on Binance → not real there.
- Mean-1 gross-neutral construction: rules out the Book-G leverage trap (any gain is timing).
- Both directions tested: lever-in-high-vol is strongly negative → the sign isn't free.
- Both-venue: none.

## Honest caveats / scope

- Per-trade realized-PnL screen; the BTC-vol HEDGE intensity is unchanged and the daily
  rebalance is not re-solved (a clean NEGATIVE a screen can establish).
- Soft-trend sizing (F1) and drawdown-age (F4) were not separately run: F2's failure to
  beat the hash on both venues, plus Book G showing exposure timing is leverage/noise on
  this book, makes them low-prior; recorded as not-pursued rather than tested. The
  existing hard BTC-uptrend gate (F0) is retained.

## No real-money / promotion claim

`REAL_MONEY` stays false. Book F closed; BTC-vol regime stays on the hedge.
