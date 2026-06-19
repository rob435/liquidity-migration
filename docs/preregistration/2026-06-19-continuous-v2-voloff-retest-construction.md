# Continuous V2 — Vol-Off Retest of One-Venue Arms (Pre-Registration)

Date: 2026-06-19
Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
System: operator-override object {daily volatility adjuster OFF, component TP 12%}
(`docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md`).
Scope: CONTINUOUS demo/paper research, both venues. No real-money claim. `REAL_MONEY` false.

## Question

Several arms won on one venue but failed the two-venue bar under the *vol-on* system. The vol-target
rebalance was shown to **amplify** venue splits (~2× for the TP arms). So: does removing the daily
volatility adjuster flip those one-venue wins into two-venue candidates, or is the split structural?
Get a clear read by re-running them through the updated {vol-off, TP12} system, same process.

## Arms retested (both-venue candidate-track arms with a one-venue win under vol-on)

- `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY` (+ `A4B_PRICE_CARRY_HASH_CONTROL`): regime hedge-intensity
  overlay. Vol-on result: Bybit MAR Δ +0.439 / Binance −0.099 (mixed, not accepted).
- `B1_SCORE_MARGIN_SIZING` (+ `B6_SCORE_MARGIN_HASH_CONTROL`): conviction sizing. Vol-on result:
  Bybit MAR Δ +0.232 / Binance −0.356 (venue-split, failed).

(Excluded: B1P path-shape sizing — negative on BOTH venues, not a one-venue win; the TP arms — TP12
is now the control; the C-flow arms — Binance-only, no two-venue test.)

## Method (same as the prior process)

- Fresh both-venue full-lifecycle A/B in `backtest-runs/continuous_v2_ab_voloff_retest_2026-06-19/`,
  control = V2_CONTROL at the updated object {TP12, vol adjuster OFF}. A4B reuses the control
  components (hedge-intensity overlay); B1 re-runs components with the causal mean-1 size_mult.
  Both arms and the control share {TP12, vol-off}, so the arm increment is isolated.
- Feature tape (TP-independent entries): `continuous_v2_feature_almanac_2026-06-19_flow_topup`
  (A4B + score_margin features at full coverage on both venues, verified).
- Robustness: this runner's monthly diagnostics (thirds, leave-one-month-out, block bootstrap).
- Negative controls mandatory (A4B_HASH, B6) — judge each arm against its matched hash, not a
  loose rule. Also report constant-gross is now the lens (vol adjuster off), so MAR == the
  un-amplified read.

## Pass bar (two-venue candidate)

- Improves MAR on **both** venues vs the vol-off control, beats its hash control on both, monthly
  thirds / LOO not one-period-driven, bootstrap P(Δ>0) supportive on both venues.
- Else: FALSIFY — the one-venue success does not generalize; the venue split is structural (not a
  rebalance-amplification artifact), and the arm stays closed (or Bybit-only).

## Honest expectation (not a result)

The TP no-rebalance check showed venue splits SHRINK but PERSIST without the adjuster. A4B/B1 had
small Binance fails (−0.10 / −0.36), so they *might* flip; the run decides. No threshold is moved
after seeing results.
