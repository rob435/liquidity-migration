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

## Verdict (run 2026-05-30, 5950X, `scripts/i2_burst_backtest.py`) — signal REAL, naïve stopped strategy does NOT pass

| EXTREME subset, per-trade net | no-stop @15bps | **12% stop @15bps** | 12% stop @45bps | frac stopped |
|---|--:|--:|--:|--:|
| bybit ALL / EARLY / RECENT | +1.79 / +0.74 / +3.47% | **−0.01 / −0.22 / +0.33%** | −0.31 / −0.52 / +0.03% | 0.37–0.40 |
| binance ALL / EARLY / RECENT | +1.74 / +1.10 / +2.90% | **+0.05 / +0.19 / −0.21%** | −0.25 / −0.11 / −0.51% | 0.33–0.41 |

- **The extreme selection beats all-bursts decisively** (no-stop +1.7% vs +0.4%; all-burst portfolio −40 to −48%) — the I1b signal is real, not an artifact, **cross-venue + all-weather (no-stop positive both eras both venues).** ✓ criterion 1.
- **But the frozen strategy FAILS the realistic stop+cost test (criterion 2):** with the 12% stop, per-trade net is ~breakeven at 15 bps and **negative at 45 bps**; **33–41% of trades stop out** (−14% each). Portfolio MAR (extreme, net15) bybit −0.11 / binance +0.18 (~0); net45 −0.83 / −0.73 (clearly negative); recent-skewed. **Not a Tier-2 pass.**
- **Diagnosis (the no-stop column):** the edge is real but lives in the *unstopped* fade — the median trade is +2.3% and wins 55%, but the pump-shorts wiggle up ≥12% before fading often enough that any risk-bounding stop is hit ~40% of the time at −14%, eating the mean. No-stop is positive but **undeployable** (a short into a pump has unbounded tail risk — one 3–5× continuation ruins the book).

**This RECONCILES with the strategy's founding philosophy (E1): short the confirmed FADE, not the top.** The burst-short is "catch the top," and I2 rediscovers why that's unsafe. **Tier-3 residual (risk_model) not run — moot until a deployable execution exists.**

**Verdict: the naïve intraday-burst top-short is NOT a deployable edge** (real signal, fails realistic risk control). **Next = I2b (pre-register):** apply the proven fade-confirm execution at the intraday scale — use the burst only to *flag the candidate*, then short the intraday **giveback** (pop-then-fade) rather than the burst itself, avoiding shorting into the continuation that causes the stop-outs. If the intraday fade-confirm captures the unstopped edge with a survivable stop, cross-venue/all-weather → real; if not, the honest conclusion is that intraday detection finds a real effect that the existing daily fade-confirm strategy already captures what's safely capturable of. Per-trade tables: `~/SHARED_DATA/i2_{bybit,binance}.trades.csv`. I3/live stays operator-gated.

## UPDATE (2026-05-30) — the verdict above (12% stop) is too narrow: a WIDE stop monetizes it

The "NOT deployable" conclusion was specific to the **12% stop** (the daily strategy's value — too tight
for a catch-the-top short). Two follow-ups changed the picture:
- **I2b (fade-confirm intraday, `scripts/i2b_burst_fade_confirm.py`):** did NOT rescue it — confirm-rate
  ≈1.0 (no selectivity), still recent-only/early-negative both venues. Wrong fix.
- **Stop-width frontier (`i2_burst_backtest.py --stop-pct`, EXTREME, both venues, net45):** MONOTONIC
  improvement 12%→50% — 12% MAR −0.8 (fails) → **30% MAR 3.2/2.79, DD 14.5/10.7%, all-weather** → 50%
  MAR 4.88/8.01, DD 16/9% → no-stop best return but tail blows out (DD 26/20%, −20% day). A wide (30–50%)
  stop is the signal's natural risk level: all-weather positive both venues × both eras × both costs,
  **bounded** tail (worst-day ~−4%).

**Revised verdict: a REAL, promising, cross-venue, all-weather intraday lead** (extreme-burst short +
wide stop) — NOT validated (Stage-B proxy; wide-stop gap/fill realism; funding unmodeled; back-loaded;
STR-factor open). → **I3** (`docs/preregistration/i3-intraday-burst-engine-2026-05-30.md`): engine-grade
backtest + funding + risk_model residual + STR-factor test. Operator-gated.
