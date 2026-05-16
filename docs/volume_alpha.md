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

`volume-events` now requires full PIT universe coverage by default. It should
raise before running if `archive_trade_manifest` is missing or if `klines_1h`
does not cover every manifest symbol. Use `--allow-partial-pit` only for an
explicitly biased diagnostic, not for real research decisions.

A "top 150" universe is acceptable only when it is point-in-time, for example
running on the completed full-PIT root with `--universe-rank-max 150`. That
means top 150 by historical daily liquidity rank on each signal date, not the
top 150 coins by today's turnover.

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

volume_absorption:
  volume spikes while same-day price movement stays muted
  tests hidden absorption before a delayed continuation/reversal

dryup_reacceleration:
  volume re-accelerates after a low-persistence, quiet-price regime
  tests coil-break behavior without forcing calendar rebalances

liquidity_migration:
  dollar-volume rank jumps sharply across the whole PIT universe
  unlike tail_liquidity_jump, this is not restricted to ranks 81-160

selloff_exhaustion:
  extreme volume spike after a strong negative same-day move
  tests panic continuation versus snapback separately
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
bash scripts/run_fullpit_volume_overnight.sh
```

PowerShell runner:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_fullpit_volume_overnight.ps1
```

Default full-PIT runner behavior:

```text
sync repo to origin/main
install/update local venv
run focused smoke tests
build/resume full Bybit USDT public archive manifest
fill/resume full PIT 1h klines from Bybit v5 API with 16 workers
validate manifest-to-parquet coverage
run the selected liquidity-migration reversal backtest only
```

The old background creative watcher and event-grid runner path are removed from
the active workflow. New ideas should be run as named, foreground
`volume-events` commands with explicit event families, parameters, and report
directories.

`volume-events` also exposes research controls for entry delay, rank-decay exit
threshold, global liquidity filters, tail-liquidity rank bounds, rank
improvement, absorption move caps, dry-up quiet-regime filters, and exhaustion
day-return thresholds. Use these to test whether an edge is immediate, delayed,
concentrated in tails, or only an exhaustion artifact.

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

python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines-1h-api \
  --name fullpit-1h-all-usdt-20230503-20260503 \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 16 \
  --min-existing-bars 1 \
  --limit 1000 \
  --retries 8 \
  --timeout-seconds 30 \
  --request-sleep-seconds 0.02
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
PIT universe coverage. Do not use current-universe subsets for volume-alpha
research decisions. The survivorship-free event run must use a data root whose
`klines_1h` coverage was built from the full `archive_trade_manifest` with
`archive-download-klines-1h-api`; raw trade archive repair remains the fallback
for any API gaps.

## Selected Full-PIT Result

Current best volume-alpha candidate:

```text
event: liquidity_migration
side: reversal (short)
threshold: top 30% dollar-volume rank migration
filters: liquidity-rank improvement >= 150, turnover / prior 7d mean >= 1.75
entry delay: 1 hour after signal close
hold: 3 days max
stop: 12% fixed
capacity: max 8 active symbols
cooldown: 5 days
cost: 3x base round-trip cost
```

Command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events \
  --event-types liquidity_migration \
  --thresholds 0.3 \
  --hold-days 3 \
  --sides reversal \
  --stop-loss-pcts 0.12 \
  --cost-multipliers 3 \
  --entry-delay-hours 1 \
  --gross-exposure 1 \
  --max-active-symbols 8 \
  --cooldown-days 5 \
  --rank-exit-threshold 0.55 \
  --liquidity-migration-rank-improvement-min 150 \
  --liquidity-migration-turnover-ratio-min 1.75
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
trades: 1,963
total return: +395.37%
max drawdown: -34.72%
worst split return: +41.14%
worst split drawdown: -33.10%
average split Sharpe-like: 0.81
train return: +48.10%
validation return: +41.14%
OOS return: +131.52%
promotion gate: pass
```
