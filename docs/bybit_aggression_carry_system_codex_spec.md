# Bybit Demo Trading System Spec

## Objective

The project focus is a profitable Bybit demo-account system built around the selected full-PIT liquidity-migration short strategy. The old fixed daily-close short fade is retired and removed from the active repo surface.

Work should improve one of four things:

- point-in-time data quality
- event feature quality
- event-driven backtest and risk realism
- demo execution parity for the selected event lifecycle

The current private Bybit client is demo-only and refuses `demo=False`. Real-money support is a separate implementation, not a hidden mode.

## Active Strategy

Canonical command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events
```

`volume-events` defaults are the selected full-PIT candidate:

```text
event: liquidity_migration
side: reversal / short
threshold: top 30% dollar-volume rank migration
PIT liquidity rank: 31-150
rank improvement: >= 150 versus prior 7d liquidity rank
turnover expansion: current turnover / prior 7d mean >= 6.0
event rank fraction cap: <= 0.90
entry delay: 1 hour after daily signal close
max hold: 1 day
stop: 12% fixed
gross exposure: 1.0
max active symbols: 6
symbol cooldown: 5 days
stop-pressure throttle: pause after 12 realized stops inside 14 days
cost multiplier: 3x base round-trip cost
```

The strategy is short-only because the best full-PIT evidence is in reversal after liquidity migration. Long continuation is not promoted unless a fresh full-PIT run clears costs, splits, drawdown, and report gates.

## Reference Evidence

Selected report:

```text
data/agc-bybit-fullpit-1h-20230503-20260503/reports/SELECTED_liqmig_dd_repair_turn6_rank31_150_eventcap90_stoppressure_20260516
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
trades: 1,138
total return: +466.57%
max drawdown: -20.34%
worst split return: +34.72%
worst split drawdown: -21.06%
average split Sharpe-like: 2.19
train return: +34.72%
validation return: +122.24%
OOS return: +86.76%
promotion gate: pass
```

Funding is not modeled in that root because funding data is missing. Fees and slippage are stress-tested through the 3x cost multiplier; funding remains a required demo/forward parity item before real-money work.

## Data Contract

Serious strategy runs must use:

- `archive_trade_manifest` as point-in-time symbol/date membership
- `klines_1h` coverage for every manifest symbol/date in the run window
- causal daily signal features, with entry delayed to the next configured 1h bar
- full trade ledger, basket ledger, equity curve, monthly returns, JSON config, and Markdown report

`volume-events` requires full PIT coverage by default. `--allow-partial-pit` is only for explicitly biased diagnostics and must not be used as promotion evidence.

## Execution Status

The old daily-close demo executor was intentionally removed because it encoded the retired calendar-clock candidate scan and sleeve assumptions. Do not revive it.

The next demo execution implementation should be built against this lifecycle:

1. At daily signal close, compute PIT-safe liquidity-migration events.
2. One hour later, submit event entries for accepted symbols subject to max-active, cooldown, and stop-pressure state.
3. Size each accepted coin from current Bybit demo wallet equity under an explicit per-coin cap.
4. Mark exits from event decay, fixed stop, and max hold using the same state transitions as the backtest.
5. Reconcile expected orders, submitted orders, fills, fees, funding, misses, slippage, and PnL drift.

Telegram may notify, but it must not approve or submit orders.
