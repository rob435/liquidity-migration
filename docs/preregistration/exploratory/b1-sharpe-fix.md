# Pre-registration: B.1 — switch `sharpe_like` to daily-aligned Sharpe

**Date:** 2026-05-24
**Author:** repo owner (via this session)
**Stage:** run-complete

## What's changing
Replace the basket-frequency Sharpe formula in
`trade_lifecycle.summarize_trade_backtest` and `_split_rows` with a
daily-aligned Sharpe derived from the calendar-day equity curve (forward-filled
across exit days). The basket-frequency formula was deleted in the same
overhaul — there is no `sharpe_basket_frequency_legacy` field carried forward.
Lower the long-only promotion-gate Sharpe threshold from 1.0 to 0.7 to track
the new honest scale.

## Hypothesis
The basket-frequency Sharpe assumes the strategy fires at `365 / rebalance_days`
baskets per year. The short sleeve trades densely enough for that to be
approximately right; the long sleeve fires 15-30 trades/year vs an implied
121, inflating its reported Sharpe by a factor of ~2-3×. A daily-aligned
Sharpe annualises off the actual daily PnL series and is invariant to firing
frequency.

## Predicted direction + magnitude
- Honest Sharpe values for the long sleeve drop by ~2-3× relative to the
  legacy value (e.g. v11a's reported 2.85 collapses to ~1.0-1.5).
- Short-sleeve Sharpe values change <10% (dense firing).
- Promotion-gate threshold drop from 1.0 → 0.7 absorbs the bias change so
  the *effective* bar is unchanged.
- Failure mode: if daily Sharpe is *higher* than legacy, the implementation
  is wrong; abort and investigate.

## Roots that will be touched
- [ ] bybit_full_pit — not touched yet (rebuild not started; this change
      lands ahead of the rebuild and applies to subsequent runs).
- [ ] binance_full_pit — same.
- [x] No data root touched by the code change itself; only computational
      paths change. Per-venue numbers will move when the next run lands.

## Decision rule (a priori)
"Accept iff:
  (a) all current tests still pass after the new value is wired in;
  (b) `sharpe_like` is invariant to TradeLifecycleConfig.rebalance_days for
      a fixed daily PnL series — asserted by
      `test_sparse_strategy_daily_sharpe_is_finite_and_unbiased_by_firing_rate`
      in `tests/test_liquidity_migration_trade_lifecycle_sharpe.py`.
Reject otherwise."

## Run command
Implementation is the diff in this PR. To verify locally:

```bash
.venv/bin/python -m ruff check liquidity_migration tests
.venv/bin/python -m pytest -q tests/test_liquidity_migration_trade_lifecycle_sharpe.py
.venv/bin/python -m pytest -q
```

## Post-run results
- `test_sparse_strategy_daily_sharpe_is_finite_and_unbiased_by_firing_rate`
  asserts (b) directly by comparing two TradeLifecycleConfigs against the
  same daily PnL series.
- The legacy basket-frequency Sharpe formula and its
  `sharpe_basket_frequency_legacy` field are removed; the only Sharpe field
  in outputs is `sharpe_like` (daily-aligned).
- The Sharpe-annualisation history previously lived in
  `docs/long_native_findings.md`, removed in the 2026-05-27 doc cleanup;
  see git history for the fix narrative.

## Verdict
**accepted.** The mechanism is mechanical (Sharpe formula change), the
firing-rate-invariance test fixes the property the legacy formula violated,
and the 0.7 threshold preserves the effective promotion bar. Cross-venue
walk-forward verification is deferred to the post-rebuild run set, which
will itself be pre-registered.
