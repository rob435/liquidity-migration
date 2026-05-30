# Pre-registration — I2: intraday extreme-pump-burst short selector (realistic backtest)

**Date:** 2026-05-30 · **Gated on:** I1b PASS (`research_summary.md` I-phase; `scripts/i1b_burst_separation.py`).
**Stage:** CANDIDATE (Tier-2 demo-arbiter) + Tier-3 residual. **Plan:** `docs/research_plan_intraday_kernel.md` (I-phase).
**Standard:** `docs/backtesting_errors_we_never_repeat.md` · `docs/parameter_pre_registration.md`. **5950X full-PIT.**

## Hypothesis

I1b showed a PIT-causal intraday signal (short extreme pump-bursts) separates faders from
continuers and survives beta-neutralization, cross-venue + all-weather, on GROSS 48h forward
returns. **H1:** as a realistic strategy — costs, stops, concentration, +1h fill — the
extreme-pump-burst short is **net-positive, cross-venue, all-weather**, with **residual
(factor-neutral) alpha** beyond the known short-term-reversal factor. **H0:** the gross edge is
eaten by costs/stops, OR is recent-only (regime), OR is entirely short-term-reversal factor
(zero residual). Then it is not a deployable edge — file the honest null.

## Selector — FROZEN from I1b before the backtest (no tuning on the result)

- **Universe:** age ≥ 300; liquidity-rank 31–400 (prior-7d-avg-turnover cross-sectional rank).
- **Burst trigger (causal, hourly):** intraday gain (close_h/day_open−1) ≥ 0.08 AND hourly
  turnover ≥ 5× the prior-7d average hour. First burst per (symbol,day); cross-day cooldown 3d.
- **Extreme-subset SELECTION (the I1b edge):** short only bursts in the top tercile of a composite
  pump-extremity score = z(idio) + z(vel3) + z(vol_spike) (the three features that carried the
  beta-neutral separation; wick excluded — it was noise). The score, its components, and the
  top-tercile cut are **pre-committed here**. (Sensitivity to the cut is reported, not tuned.)
- **Entry:** short at burst_h+1 open (+1h fill, non-negotiable; PIT). **Exit:** max-hold 48h OR
  stop-loss 12% (capped-fill 10%) OR the giveback completing — pre-committed; report the grid.
- **Concentration/sizing:** max_active=12, risk_equal 2% (the validated daily config), cooldown.
- **Costs:** 15 bps base AND 45 bps (×3 stress). 100% taker. Funding noted (binance funding-missing
  in the engine cost model — flag funding-missing; bybit short funding modeled).

## Comparisons / decision (pre-committed)

1. **Selection adds value:** extreme-subset short vs all-burst short — the extreme subset must be
   materially better (the I1b separation must survive the engine).
2. **Tier-2 (demo-candidate), via `scripts/r1_robustness.py`:** return positive BOTH venues; pooled
   MAR Δ (vs the all-burst control) > +0.1; neither venue MAR Δ < −0.5; ≥30 bybit / ≥20 binance
   trades; **all-thirds-positive both venues** (the c2b guard — not recent-only); bootstrap p5, LOO
   reported.
3. **Tier-3 residual:** residualize the strategy PnL through `risk_model.decompose_strategy_pnl`
   (do NOT rebuild). Report residual Sharpe + whether it clears +0.3 **cross-venue**. **Explicitly
   test the short-term-reversal factor:** include/proxy a 1–2d reversal factor; if the residual
   collapses once reversal is in the model, the edge = a known factor (still tradeable if net-positive,
   but NOT unique alpha — label honestly).

## Falsifiers (STOP / honest null)

- Net-negative after 15 bps on either venue, OR recent-only (early third negative), OR the extreme
  selection adds nothing over all-bursts once costed, OR residual Sharpe ≤ 0 cross-venue (pure factor
  with no idiosyncratic alpha AND not net-tradeable).

## PIT / overfitting guards

- Features causal at burst h; +1h fill; no within-bar look-ahead (#2/#13/#14).
- Selector + thresholds frozen above BEFORE the run; the full parameter distribution is reported,
  not the winner (#17/#19); cross-venue + early/recent agreement is the bar (c2b lesson).
- Same-code intent: the burst detector is a pure function of causal features so an eventual live WS
  engine (I3) runs the identical path (#16).

## Build

A standalone burst-portfolio backtester (the `volume-events` engine has no intraday-burst entry
path): enter/exit/cost/concentration sim on the I1b burst signal → trade ledger + equity +
r1_robustness metrics + risk_model residual. Both venues, early/recent. Memory-safe (reuse the
in-memory projected-panel approach from I1b). Pre-register any later change to the selector.

## Status

PENDING — build + run on the 5950X. I3 (live WS engine + forward demo) stays explicit-operator-gated.
