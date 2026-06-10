# Pre-registration: downtrend_bounce_v1 — forward paper clock + promotion bar

**Date:** 2026-06-10 (registered at collector start). **Label:** forward evidence
collection for a **standalone product** (the D3 "separate product with its own risk
budget" clause, activated by operator directive 2026-06-10).

## What runs

`python -m liquidity_migration downtrend-bounce-paper --data-root <full-pit root>
--venue {bybit,binance}` — one cycle per UTC day (module
`liquidity_migration/downtrend_bounce.py`, strategy id `downtrend_bounce_v1`).
PAPER ONLY: no orders, an append-only JSONL ledger per venue under
`<root>/downtrend_bounce_paper/`.

Profile (frozen from the D3 receipt; DR1 falsified the hedge → UNHEDGED):
BTC-30d-down regime (causal through the decision close), LONG bottom decile of
ret_7d, non-BTC/non-stable, turnover ≥ $500k, equal weight, gross 0.5×, k=1 daily
tranche, flat in up-regimes. Marks: close-to-close, 12 bps RT on one-way turnover,
real funding day-sums (flagged when the dataset lags).

Clocks seeded 2026-06-10: bybit from decision 2026-06-08 (root fresh to 06-09);
binance from decision 2026-05-30 (root to 05-31; catches up with the fapi stage-2
refresh). Both venues opened IN-REGIME (btc30 ≈ −0.22/−0.25 bybit, −0.03/−0.06
binance).

## Known risk class (pre-stated, not a surprise to relitigate later)

In-sample (D3): in-regime DD −30..−44%, Sharpe 2.03/0.81, funding-positive. This
product is expected to draw down hard inside bear regimes. The risk budget is the
operator's allocation decision; the in-sample class is the reference point.

## Promotion bar — paper → demo orders (ALL required; never loosened)

1. ≥ 60 in-regime paper days accumulated on the venue being promoted.
2. Paper net return > 0 on BOTH venues over their accumulated windows.
3. Paper max DD within 1.5× the in-sample class (i.e., not beyond −66%) — worse
   means the in-sample estimate is broken, not that the bar adapts.
4. Funding coverage present on ≥ 90% of marked days (funding is a large share of
   the edge; a funding-blind paper record is not evidence).
5. Operator risk-budget sign-off: an explicit gross-USDT budget for the product,
   isolated from the continuous book's budget.
6. **Account-level latched disaster stop wired BEFORE any order submission**: a
   ws_risk-style halt that flattens + blocks entries + requires manual re-arm,
   with a persisted high-water mark; level set by the operator at or inside the
   Tier-3 DD<50% bound. (Per the 2026-06-10 session finding: per-position stops
   exist in the demo stack, but no account-level halt — this product's risk class
   makes one mandatory.)
7. Real-money promotion is a separate, stricter question: full Tier-3, untouched.

## Non-negotiables inherited

Paper/demo evidence is execution evidence and forward-alpha evidence, never an
excuse to re-mine the spent 2023-04→2026-05 window (DR1's FAIL clause re-closed
in-sample downtrend constructions, including the hedged-bounce synthesis). No
parameter changes to the frozen profile without a fresh pre-registration.
