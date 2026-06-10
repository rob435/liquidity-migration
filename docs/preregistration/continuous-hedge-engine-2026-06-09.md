# Pre-registration: WP3 Stage-B — BTC hedge leg inside the rebalance engine

**Date:** 2026-06-09 (registered BEFORE the run)
**Run label target:** `candidate` if the full battery passes (PIT, costed, ledger-backed,
hedge params FIXED a-priori from Stage-A — no re-tuning here); else `exploratory`.
**Program:** plan WP3 Stage-B. Stage-A receipt: `continuous-hedge-overlay-2026-06-09.md`
(PASS 6/6, BTC selected).

## What Stage-B adds over Stage-A

The hedge leg now lives INSIDE `apply_rebalance_rule` (implemented on main after this
receipt; current code path):
hedge PnL/funding/turnover-cost flow into `basket_return` BEFORE equity compounding, so
the drawdown half-scale state reacts to HEDGED equity (Stage-A hedged after the scale
path was fixed). Vol-target scale stays on raw book returns (live-planner semantics).
Engine-grade additions impossible at Stage-A: 2x BOOK-cost stress through
`_apply_cost_multiplier`, and a beta latency falsifier (`beta_extra_lag_days=1`).
Tests: `tests/test_continuous_rebalance_hedge.py` (8 tests: causality in hedge and book
returns, min-obs warm start, long-only clip, accounting identity, funding sign, gap
close/reopen turnover, lag falsifier) — all pass; 238 continuous/rebalance tests green.

## Fixed parameters (a-priori, from Stage-A — NOT re-tuned)

`ContinuousHedgeRule(beta_window_days=90, beta_min_obs=60, hedge_cap=2.0, cost_bps=5)`;
instrument BTCUSDT (daily close-to-close from the verified panel construction; real
per-event funding day-sums). Book: winner_base components
`{turn3p3:0.30, turn4p3:0.20, turn4p5:0.40, age210tp14:0.10}` combined via the scout's
`_combine_components`, rules `w90/tv0.045/max{4,10}/ddh-0.04/momentum0/resize10bps`.
max4 = binding cell; max10 reported.

## Cells

- Controls (hedge off) max4/max10 — must reproduce the Stage-A controls (same path).
- Primary hedged: W90/min60/cap2/5bps, real funding, max4 + max10.
- Stress: hedge cost 10/20 bps; funding off; W ∈ {60,120,150}; `beta_extra_lag_days=1`;
  2x BOOK cost (control AND hedged) at max4.

## A-priori banking bar (binding max4; deltas vs same-cost control)

c1 positive total return both venues; c2 DD reduced OR Sharpe up both venues;
c3 pooled (mean) MAR Δ > +0.1 and neither venue < −0.5; c4 c1–c3 hold at 2x hedge cost
(10 bps); c5 hedged 2023–24 Sharpe ≥ control both venues; c6 ΔSharpe keeps sign at
W ∈ {60,120,150} both venues; **c7 (new)** under 2x BOOK cost, hedged vs 2x-control
still satisfies c1–c3; **c8 (new)** lag falsifier keeps ΔSharpe sign both venues.

PASS = all 8 → Stage-B banked as engine-grade `candidate`; remaining to `paper_ready`:
live hedge-leg executor plumbing + forward demo accumulation (operator decision).
FAIL on c7/c8 only → record which stress kills it; do not bank.

## Artifacts

`~/SHARED_DATA/continuous_hedge_engine_2026-06-09/` — cells CSV + report JSON.
Driver: `scripts/continuous_hedge_engine_driver.py`.

## Verdict (filled in after the run, same day)

**PASS 8/8 — Stage-B banked as engine-grade `candidate` (in-sample; Tier-2 ceiling).**
Controls reproduced the benchmark bit-close through the engine path (bybit max10
+226.22%/MAR 6.17; binance +142.48%/6.00 — identical to the Stage-A controls), so the
component-combine + rebalance pipeline is verified end-to-end.

Binding max4 cell (W90/min60/cap2/5bps, real BTC funding), hedged vs control:

| venue | ret | ΔMAR | ΔSharpe | ΔDD | ΔSharpe 23-24 |
|---|---|---|---|---|---|
| bybit | +93.18% | +0.50 | +0.233 | −0.05pp | +0.436 |
| binance | +73.66% | +1.07 | +0.382 | +0.02pp | +0.627 |

Engine-grade results slightly beat Stage-A (DD-half state reacting to hedged equity
helps). All stresses pass: 2x/4x hedge cost; funding off (drag ≤0.02 Sharpe); window
grid 60/120/150 sign-stable; **c7 2x BOOK cost: the hedge helps MORE when the book is
cost-stressed** (bybit ΔMAR +0.89, ΔSharpe +0.23, DD +0.58pp better; binance +1.03 /
+0.397 / +0.05pp); **c8 one-day beta latency keeps the effect** (+0.183/+0.375).
At max10 (reported): hedged MAR 8.39/8.17 vs 6.17/6.00, DD improved both venues.
Mean hedge ratio ~4-6% of equity (max4), ~9-13% (max10).

Tier framing: in-sample candidate — never past Tier-2 without forward demo (the
binding Tier-3 arbiter). Remaining to `paper_ready`: live hedge-leg executor plumbing
(a resize-planner for the BTC long leg analogous to `plan_continuous_rebalance_resizes`)
+ forward demo accumulation — both operator decisions.

Artifacts: `~/SHARED_DATA/continuous_hedge_engine_2026-06-09/{cells.csv, report.json}`.
Engine change: `liquidity_migration/continuous_rebalance.py` (`ContinuousHedgeRule`,
`compute_hedge_beta`, hedged `apply_rebalance_rule`; unhedged path byte-identical) +
`tests/test_continuous_rebalance_hedge.py` (8 tests).

## Addendum 2026-06-10

Executor plumbing is now built and deployed as a daily dry-run:
`ContinuousHedgeState`, `compute_continuous_hedge_ratio`, and
`plan_continuous_hedge_resize` live in `continuous_hedge_manager.py` with
backtest/live parity tests. The VPS runs
`liquidity-migration-continuous-hedge.timer` with order submission gated by
`SUBMIT_HEDGE=0`. Remaining to `paper_ready`: explicit operator flip for hedge
submission plus forward demo accumulation; all Tier-3 clocks restarted on the
2026-06-09 rebuild.
