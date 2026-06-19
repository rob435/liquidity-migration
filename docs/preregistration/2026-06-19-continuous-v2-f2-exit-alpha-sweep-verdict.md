# Continuous V2 Exit-Alpha Phase 2 — Sweep Verdict (per-trade) + Lifecycle Validation

Date: 2026-06-19

Construction: `docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md`
Scope: both-venue, exploratory, no-order per-trade re-sim (the lifecycle MAR validation is a
separate two-venue arm). Any winner is an operator-gated frozen-v2 parameter change (voids the
forward ledger). No real-money claim.

## What ran

`scripts/continuous_v2_f2_exit_policy_sweep.py` re-simulated 14 exit policies on the full klines_1h
path (to 72h) for both venues' V2_CONTROL short trades (bybit 2367, binance 2149; recon vs the
recorded ledger exact, err 0.00000). Output: `backtest-runs/continuous_v2_f2_exit_alpha_2026-06-19/`.

## Per-trade contribution Δ vs control (10% TP, 24h)

| policy | bybit Δ% | binance Δ% | bybit pos-month | binance pos-month |
| --- | ---: | ---: | ---: | ---: |
| tp04 / tp05 / tp06 / tp08 (lower TP) | −34% … −8% | −47% … −23% | <35% | <35% |
| **tp12 (raise TP→12%)** | **+16.7%** | **+4.0%** | 77% | 56% |
| **tp15 (raise TP→15%)** | **+16.4%** | **+8.0%** | 63% | 47% |
| hold 36h / 48h / 72h (TP 10%) | +6% / +29% / +10% | −25% / −9% / −33% | mixed | mixed |
| decay 10→6 / 10→4 | −4.5% / −7.4% | −8.8% / −11.4% | <38% | <26% |
| partial 50% @5%/@6% | −13.6% / −11.7% | −23.7% / −19.0% | <35% | <35% |

## Finding — the edge is to LET WINNERS RUN, not cut them

The only both-venue improvers are **raising** the take-profit (tp12, tp15). Every profit-capping or
early-exit idea — lower TP, time-decaying TP, partial profit-taking — is negative on both venues, and
hold-extension venue-splits (helps Bybit, hurts Binance). The fade reversion overshoots 10% on the
winners, so a higher TP harvests more of them; this is the exit-side mirror of the whole study's
lesson (the diffuse winners carry the book and must not be cut/capped).

Caveats: tp12's Binance gain is modest (+4.0%) and only 56% of months positive; tp15's Binance gain
is +8.0% but only 47% of months positive (NOT majority — one-month-driven, fails the stability bar).
And this is a per-trade contribution sum, NOT rebalanced MAR — the daily vol-target rebalance can
dampen a higher-TP (longer-hold) policy.

## Arbiter — lifecycle validation (RESOLVED)

The full v2 lifecycle (re-sim at TP 12%/15% + frozen rebalance/hedge, both venues) showed the
per-trade sum was misleading: raising the TP is a **robust Bybit improvement (MAR +1.79/+2.23)** but a
**Binance loss (−3.66/−3.45) via a doubled drawdown** → venue split, falsified on the two-venue bar. A
volatility-scaled TP does not reconcile it (worse on Binance). See
`...-f2-exit-tp-lifecycle-verdict.md` and `...-f2b-vol-tp-verdict.md`. Net: no both-venue exit-alpha
candidate; a robust Bybit-only TP lead remains (operator-gated). The per-trade contribution sum must
not be trusted for exit policy — drawdown is decisive.
