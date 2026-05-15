# Volume Alpha Research Plan

Volume alpha is being reset around event-driven behavior. The old fixed-day
rebalance sweeps are deprecated as a research direction because they force
calendar trades even when no useful volume event occurred.

## Objective

Find whether Bybit perp volume events have standalone, cost-cleared forward
edge that can later justify a demo sleeve.

This remains secondary to the active daily-close fade system until it clears
all gates below.

## Decision

The current priority is event-driven volume entries:

```text
enter only when a symbol has a fresh volume event
exit when the event decays, reverses, risk is hit, or max hold expires
compare against a fixed-rebalance benchmark only as a control
```

Do not promote headline backtest returns from current-universe tests. Those are
biased benchmarks unless tradable membership is point-in-time.

## Deprecated

These workflows are intentionally removed from the active research path:

```text
fixed 7d/14d split-grid helper scripts
liquidity bucket sweep helper scripts
5950X overnight fixed-grid runner
old "run every combination overnight" workflow
```

The reusable Python modules can stay until event-driven replacements exist:

```text
aggression_carry/volume_alpha.py
aggression_carry/volume_backtest.py
```

They still contain useful feature, cost, ledger, and reporting code. The old
standalone fixed-grid/promotion helper scripts have been removed.

## Event Definitions

First implementation should test these event families separately:

```text
fresh_volume_spike:
  volume_change_1d crosses into top 20% or 30%
  previous day was outside that bucket

persistent_volume_breakout:
  volume_persistence crosses into top 20% or 30%
  persistence was below median within the last 3 days

tail_liquidity_jump:
  symbol is in liquidity ranks 81-160
  dollar_volume_rank improves sharply versus its 7d baseline
  exclude symbols without point-in-time tradability proof

volume_exhaustion:
  extreme volume spike after strong price extension
  test both continuation and reversal sides separately
```

## Trade Lifecycle

Each event-driven test needs a full trade ledger:

```text
signal timestamp:
  daily close after all inputs are known

entry:
  next available 1h bar after signal close, or configured delay
  no duplicate entry while symbol already has exposure

side:
  test continuation and reversal as separate hypotheses

exit:
  event decay below threshold
  rank reversal
  fixed or volatility stop
  max hold of 3, 7, or 14 days
  optional cooldown before re-entry

risk:
  cap active symbols
  cap gross exposure
  account for fees, slippage, and funding
```

## Required Gates

A candidate is not demo-ready unless it passes:

```text
point-in-time symbol membership
no current-universe survivorship dependency
positive train, validation, and OOS splits
worst split return >= 0
worst drawdown no worse than -35%
average Sharpe-like >= 0.5
cost multipliers of 1x and 3x reported
trade ledger, equity curve, monthly table, and failure reasons saved
```

## Implementation Order

1. Add an event table builder:

```text
symbol
signal_ts_ms
event_type
score
side_hypothesis
prior_rank
current_rank
liquidity_rank
tradable_membership_flag
```

2. Add an event-driven ledger backtester:

```text
one row per actual event-triggered trade
no calendar-forced basket if no event fired
cooldown and max-active-symbol logic included
```

Current command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events
```

Overnight full-PIT runner:

```bash
nohup bash scripts/run_fullpit_volume_overnight.sh > fullpit_volume_overnight.nohup.log 2>&1 &
tail -f fullpit_volume_overnight.nohup.log
```

Default behavior:

```text
sync repo to origin/main
install/update local venv
run focused smoke tests
build/resume full Bybit USDT public archive manifest
download/resume full PIT 1h klines with 64 workers
validate manifest-to-parquet coverage
run the selected persistent-volume-breakout event backtest
run the full event-driven feature grid
```

The default event grid covers all active event families, top 20% and 30%
thresholds, 3/5/7/14 day holds, continuation and reversal, fixed stops of
0/3/5/8/12%, and 1x/3x cost multipliers. It repeats that grid for max-active
6 and 12 with cooldowns of 3 and 7 days. Override the breadth with
`EVENT_GRID_*` environment variables in `scripts/run_fullpit_volume_overnight.sh`
when a narrower or wider overnight run is needed.

Full PIT data build for event research:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  archive-manifest \
  --name pit-all-usdt-20230503-20260503 \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 32

AGC_ARCHIVE_DOWNLOAD_BACKEND=curl python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines-1h \
  --name fullpit-1h-all-usdt-20230503-20260503 \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 32 \
  --discard-archives-after-success
```

3. Add split evaluation:

```text
train:      2023-05-03 to 2024-05-03
validation: 2024-05-03 to 2025-05-03
OOS:        2025-05-03 to 2026-05-03
```

4. Add promotion report:

```text
event family
side
thresholds
split returns
drawdowns
Sharpe-like
trade count
turnover
fees/funding
top failure reason
```

## Current Status

Fixed-rebalance volume-alpha results are archived mentally as exploratory only.
They are not the path forward.

The event-driven runner now exists as `volume-events`. It writes scenario
summary, best-scenario trades, baskets, equity, monthly returns, JSON, and
Markdown reports. Promotion requires full point-in-time membership and full
PIT universe coverage. Current-150 PIT-filtered tests are useful diagnostics
only; the survivorship-free event run must use a data root whose `klines_1h`
coverage was built from the full `archive_trade_manifest` with
`archive-download-klines-1h`.
