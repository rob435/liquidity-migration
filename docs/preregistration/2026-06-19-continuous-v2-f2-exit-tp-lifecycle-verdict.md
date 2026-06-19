# Continuous V2 Exit-Alpha — Raised-TP Lifecycle Verdict (both venues)

Date: 2026-06-19

Construction: `docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md`
Sweep: `docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-sweep-verdict.md`
Scope: both-venue full v2 lifecycle (components re-run at the raised TP + frozen rebalance/hedge).
EXPLORATORY: a TP change is an operator-gated frozen-v2 parameter (voids the forward ledger). No
real-money claim. Run: `backtest-runs/continuous_v2_ab_exit_tp_2026-06-19/` (+ robustness.json).

## Results vs v2 control (Bybit MAR 5.660, Binance MAR 8.185)

| arm | venue | return | maxDD | MAR | MAR Δ | boot P(Δ>0) | min LOO Δ |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| EXIT_TP12 | bybit | +121.9% | −5.22% | 7.447 | **+1.787** | 0.90 | +0.185 |
| EXIT_TP12 | binance | +79.7% | **−5.60%** | 4.530 | **−3.655** | 0.03 | −0.071 |
| EXIT_TP15 | bybit | +128.7% | −5.21% | 7.889 | **+2.228** | 0.95 | +0.216 |
| EXIT_TP15 | binance | +83.6% | **−5.61%** | 4.738 | **−3.448** | 0.12 | −0.070 |

Pooled MAR Δ: TP12 −0.934, TP15 −0.610 → both **FALSIFY (pooled ≤ 0)**.

## Verdict — flat raised-TP FALSIFIED on both-venue (venue split)

Raising the take-profit is a **robust, large Bybit improvement** (MAR +1.8/+2.2, return +24–31pp,
Sharpe 3.71→4.06–4.11, bootstrap 90–95% positive, monthly LOO positive) but a **large Binance loss**:
Binance max drawdown nearly doubles (−3.27% → −5.6%) while return is flat, collapsing MAR by ~3.5.
The mechanism is asymmetric microstructure — Bybit fade winners overshoot 10% (a higher TP harvests
them with stable DD), whereas Binance names that have not reverted by 10% tend to keep going against
the short, so a higher target rides them into much deeper drawdowns.

This is a true venue disagreement (a regime/microstructure warning, not alpha), so a flat raised TP
cannot clear the two-venue candidate bar. It also exposes that the **per-trade sweep was misleading**:
it scored Binance tp12 at +4.0% on un-rebalanced contribution, but the DD-aware lifecycle shows
−3.66 MAR. Per-trade contribution sums must not be trusted for exit policy — drawdown is decisive.

## The real insight + next test

The take-profit optimum is **venue / volatility dependent**, not a single flat number. The principled
both-venue test is a **volatility-scaled TP** (TP_i ∝ each name's trailing vol, clipped) — does adapting
the target to each name's typical move capture Bybit's runners without over-holding Binance's into
drawdown? That is the next exit-alpha arm. It must be judged DD-aware (the per-trade sum lies);
preferred path is a per-trade TP engine hook + full lifecycle, or a validated DD-aware proxy.

A flat venue-specific TP (e.g. 12–15% Bybit, 10% Binance) is NOT pursued: venue-specific deployment is
a separate operator-gated policy (plan Book G2), it curve-fits the venue, and the cross-venue
disagreement is itself a warning sign.
