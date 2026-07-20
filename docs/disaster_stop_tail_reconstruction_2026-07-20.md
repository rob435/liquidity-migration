# RECONSTRUCTION (2026-07-20) — 2026-06-20 disaster-stop TAIL receipt

**This is a reconstruction, not an original receipt.** The original document
`docs/preregistration/2026-06-20-continuous-v2-disaster-stop-tail-construction.md`
and its runner `scripts/continuous_v2_book_a_disaster_stops.py` were committed
in `1fa7045d28cde0f3b8210f796b1ea7d7516b224b` (2026-06-20) and later pruned in
`f7dadd6` (2026-06-26). The local `backtest-runs/` artifacts it referenced no
longer exist on any host. Everything below is recovered verbatim from git
history at `1fa7045`; the numbers survive **only in the receipt text** — the
underlying per-trade artifacts are gone, so the tables cannot be independently
re-verified without a re-run through the P0.1 harness. Recovered under
tail-risk program task P0.5 (`docs/tail_risk_program.md`) so the
sizing-is-the-disaster-control claim has a citable anchor.

What this reconstruction is evidence of: exactly what was claimed and measured
on 2026-06-20, as recorded at commit time. What it is not: a re-verification
of those measurements, an alpha/robustness claim, or authority for any
runtime change.

To re-obtain the runner: `git show 1fa7045:scripts/continuous_v2_book_a_disaster_stops.py`.

---

## Original text (verbatim from `1fa7045`)

# Construction + Verdict: Continuous V2 Next-Level — Disaster-Stop TAIL Analysis (Book A addendum)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction + verdict (tail lens)
Parent: `docs/preregistration/2026-06-20-continuous-v2-book-a-stops-tpsl-construction.md`
Run label: `exploratory`. **Verdict: the inverse-vol per-name sizing IS the disaster control. Price stops add negligible tail protection at high MAR cost. The disaster-stop need is downstream of the leverage (Book G) decision, not a standalone exit rule.**

## Why this addendum exists

Book A's first pass judged stops by MAR and (correctly) found stops do not improve MAR.
But that is the wrong objective for a DISASTER stop, whose job is capping the
catastrophic tail / preventing liquidation, not lifting MAR. This addendum re-frames
stops around tail metrics and directly informs the OPEN mainnet risk-control question
(STATE.md: `continuous_ensemble_v2` is "intentionally no-stop demo/paper only; any
mainnet path needs a new risk-control design").

## The tail is real (control no-stop book)

| venue | worst MAE | trades MAE≤−100% | worst realized trade |
|-------|----------:|-----------------:|---------------------:|
| bybit | −143% | 9 (0.38%) | BOBAUSDT gross −75% |
| binance | −258% | 12 (0.56%) | HIGHUSDT MAE −236%, gross −73% |

All worst trades exit via `max_hold` (run against the short the full 24h). The backtest
"survives" a −236% MAE only by assuming infinite margin — on a leveraged mainnet account
that is a liquidation, with no partial recovery.

## Method

`scripts/continuous_v2_book_a_disaster_stops.py` re-resolves V2_CONTROL trades on the 1m
path under wide disaster stops (25/40/60/80%), reporting per-trade tail (worst trade,
CVaR 1%/5%), worst day, max drawdown, MAR (the cost), and the MAE-breach count (control
trades the no-stop book holds through).

## Results (full run 2026-06-20)

bybit (control: MAR 6.15, worst_trade −0.86%, worst_day −2.33%, CVaR1% −0.64%):

| stop | MAR / Δ | return cost | worst_trade (capped) | stop_rate | MAE-breach trades |
|------|--------:|------------:|---------------------:|----------:|------------------:|
| 25% | 1.49 / −4.66 | −0.426 | −0.79% (+0.07%) | 9.9% | 234 |
| 40% | 4.10 / −2.06 | −0.170 | −0.73% (+0.13%) | 3.1% | 73 |
| 60% | 4.53 / −1.63 | −0.145 | −0.83% (+0.03%) | 1.4% | 34 |
| 80% | 5.26 / −0.89 | −0.097 | −0.91% (−0.05%) | 0.5% | 13 |

binance (control: MAR 4.32, worst_trade −0.82%, worst_day −2.67%):

| stop | MAR / Δ | return cost | worst_trade (capped) | stop_rate |
|------|--------:|------------:|---------------------:|----------:|
| 25% | 1.32 / −3.00 | −0.275 | −0.75% (+0.08%) | 9.5% |
| 40% | 2.40 / −1.92 | −0.144 | −0.87% (−0.05%) | 3.4% |
| 60% | 2.43 / −1.89 | −0.126 | −1.15% (−0.32%) | 1.6% |
| 80% | 2.56 / −1.75 | −0.119 | −0.84% (−0.01%) | 0.9% |

## Verdict — sizing is the disaster control, not a price stop

1. **The worst single trade costs only ~0.85% of book equity even with MAE −143%/−258%**,
   because `target_vol_per_name=0.01` inverse-vol sizing sizes the blow-up-prone (high-
   vol) names DOWN to ~1% notional. The per-name book tail is bounded by SIZING, not by
   holding. (Check: worst gross −0.754 × notional ≈ 0.011 → −0.0086 book; the 1.1%
   notional confirms the sizing cap.)
2. **Disaster stops therefore cap almost nothing** (+0.07% to −0.05% on the worst trade;
   on Binance the wider stops make it WORSE — the stop fills at the intrabar spike that
   would have reverted by max_hold) **while costing 0.9–4.7 MAR and up to −0.43 return.**
   A price stop on this book pays a large premium for tail protection the sizing already
   provides.
3. **The disaster-stop need is downstream of the LEVERAGE decision (Book G).** The −0.85%
   worst-trade bound holds at the book's native ~1× gross (each name ~1% notional). If
   gross is levered (the Book G vol-adjuster dial — e.g. 4×), each name becomes ~4%
   notional and a −236% MAE = ~−9.4% equity at the position level → a genuine liquidation
   tail reappears, and the worst-trade book contribution scales with gross. So the honest
   mainnet risk-control answer is: keep gross low (sizing self-protects) — and if a
   levered path is ever wanted, the fix is lower gross + a POSITION-level liquidation /
   margin guard, NOT a profit-style price stop on the fade (which destroys MAR and barely
   caps the tail).

## Answer to the open mainnet risk-control question (STATE.md)

A disaster/price stop is NOT the missing mainnet risk control for this book. The existing
inverse-vol per-name sizing already bounds per-name tail to ~1% of equity at native gross.
The real mainnet risks are (a) running gross too high (re-creating the liquidation tail —
see Book G: the vol-adjuster is a leverage dial), and (b) correlated multi-name squeeze
days (a portfolio/Book-I question, not a per-trade stop). Recommended mainnet design:
cap gross + position-level liquidation guard + correlated-squeeze day cap — not a per-name
profit stop.

## Falsifiers / honesty

- The "stop helps tail" hypothesis is falsified by the worst_trade-capped column (≈0 or
  negative). The MAR cost is large and monotonic in stop tightness.
- Per-trade realized-PnL screen on the 1m path (adverse-first); no rebalance/hedge
  re-solve. A clean conclusion a screen can establish (the tail bound is a sizing fact).

## No real-money / promotion claim

`REAL_MONEY` stays false. No stop is added to the frozen object. This informs, but does
not decide, the operator's mainnet risk-control design.

---

*End of verbatim recovery. Reconstruction note: the 2026-06-20 study is the
direct ancestor of tail-risk program items R3b (its "correlated-squeeze day
cap" was recommended and never built) and R3c, and one of the two receipts
behind the program's closed per-trade-stop line. Its MAR framing predates the
program's grading standards; under those standards its cost column would be
reported as ES/net-based premium instead. The claims remain Lane-1
(exploratory, spent surface).*
