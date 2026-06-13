# Active Receipt: Continuous 2f Hedge

**Status:** recommended in-sample hedge form with Tier-2 ceiling; live path wired
and armed, but blocked by stale warmstart CSVs for risk-increasing legs.

## Final Decision

The retained hedge form is BTC+ETH two-factor hedging through the rebalance
engine. It replaced the single-BTC hedge as the recommended hedge form after the
pre-registered engine run passed its parity and robustness bars.

This is not promotion evidence. It remains below Tier-3 until forward demo/paper
evidence, reconciliation, stress, and capacity bars are satisfied.

## Current Live State

- `HEDGE_MODE=2f`, `SUBMIT_HEDGE=1`, and demo confirmation are wired.
- Every armed run so far has either had a flat/no-action book or been blocked by
  stale warmstart data.
- Stale warmstart blocks only risk-increasing legs; reduce-to-flat stays allowed.
- Operator decision: regenerate `deploy/hedge_warmstart/*.csv` with a refresh
  cadence, or disarm the timer.

Historical Stage-A/Stage-B details are in git history.
