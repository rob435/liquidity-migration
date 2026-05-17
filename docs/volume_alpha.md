# Liquidity-Migration Strategy Plan

This repo is now focused on the event-driven full-PIT liquidity-migration
strategy. The old fixed daily-close short fade is retired, and fixed-day
volume rebalance sweeps are legacy benchmarks only.

## Objective

Turn the selected Bybit perp liquidity-migration short strategy into a
demo-account system with reproducible full-PIT backtests, clear reports, and
event-lifecycle execution parity.

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

The default tradable universe excludes only stable/peg perps, including failed
peg remnants such as `USTCUSDT`, before ranks/features are built. Do not
manually blacklist top coins such as BTC, ETH, SOL, BNB, XRP, or TRX unless a
fresh full-PIT run proves the blacklist improves the promoted objective.

## Retired

These workflows are intentionally removed from the active research path:

```text
fixed daily-close short fade
old daily candidate scan and sleeve runner
old forward paper/demo executor
fixed 7d/14d split-grid helper scripts
liquidity bucket sweep helper scripts
5950X overnight fixed-grid runner
old "run every combination overnight" workflow
```

The legacy Python entrypoints are removed. The active shared code is now:

```text
aggression_carry/volume_features.py
aggression_carry/trade_lifecycle.py
```

`volume_features.py` builds the causal daily liquidity/volume features.
`trade_lifecycle.py` contains the event strategy's exit, basket, and equity
helpers. The old fixed-rebalance alpha/grid backtest APIs are no longer part of
the repo surface.

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
worst drawdown no worse than -25%
average Sharpe-like >= 0.5
cost multipliers of 1x and 3x reported
trade ledger, equity curve, monthly table, and failure reasons saved
equity chart with BTC overlay plus monthly/growth gridlines
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

Current command, using the selected strategy defaults:

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

Fixed-rebalance volume results are archived as exploratory only.
They are not the path forward.

The event-driven runner now exists as `volume-events`. It writes scenario
summary, best-scenario trades, baskets, equity, monthly returns, JSON, and
Markdown reports. Promotion requires full point-in-time membership and full
PIT universe coverage. Do not use current-universe subsets for strategy
research decisions. The survivorship-free event run must use a data root whose
`klines_1h` coverage was built from the full `archive_trade_manifest` with
`archive-download-klines-1h-api`; raw trade archive repair remains the fallback
for any API gaps.

Forward demo testing now uses `event-demo-cycle`, not the retired daily-close
executor. The continuous runner is:

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_event_engine.sh
```

The runner checks every 5 minutes by default, sizes each accepted coin from
the backtest weight (`gross_exposure / max_active_symbols`, currently 20.83% of
current Bybit demo USDT equity), exits before entries, sends Telegram only for
material events when enabled, and records `event_demo_trades`,
`event_demo_orders`, and `event_demo_cycles` ledgers. It is a current-universe
forward tester, so it is allowed for demo evidence and operations, not for
historical promotion evidence.
Recent 1h bars are cached in `event_demo_klines_1h`, keeping the forward-demo
cache separate from the full-PIT research `klines_1h` dataset.
Entry orders attach native stop/TP immediately, then confirmed fills recompute
the ledger stop/TP from actual fill price and repair Bybit trading-stop state
when rounding moves the protection levels.

Fast exit enforcement is handled by the separate exit-only risk watchdog:

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_ws_risk_engine.sh
```

The watchdog now defaults to `event-risk-ws`: exchange-native stops first,
demo private WebSocket position/order/execution streams plus the mainnet public
ticker stream second, and REST only for demo fallback/reconciliation. It does not scan for entries. It repairs
exchange-native stops/TPs, subscribes to active-position ticker streams, forces
reduce-only exits on streamed stop, take-profit, or max-hold breaches, and can
mark the ledger closed from WebSocket execution messages. Bybit currently does
not support WebSocket Trade order entry for demo trading, so the demo VPS uses
`ORDER_SUBMIT_MODE=ws_then_rest`: WebSocket decides, REST submits only when demo
WS order entry is unavailable.
The demo private socket rejects `execution.fast`, so the VPS uses the normal
private execution stream unless that limitation is retested and cleared.
Socket startup is bounded by `STREAM_START_TIMEOUT_SECONDS` so a blocked
private/public subscription reports an error and leaves REST reconciliation plus
exchange-native stops running instead of hanging the watchdog before startup.
The watchdog writes latest reports under `reports/event-risk-ws` every heartbeat
and keeps timestamped JSON/Markdown snapshots for startup and material risk
events, so exit decisions survive later quiet heartbeat overwrites.
Fresh pending reduce-only untracked-position exits are restored after restart
even though they have no ledger trade ID, so the watchdog does not duplicate a
still-pending emergency flatten order after process loss.
Material Telegram alert keys are persisted in the same report directory, so
restarting the watchdog does not resend the same alert.
Stop-repair alerts are keyed by symbol and target stop/TP rather than synthetic
repair order-link IDs, so repeated confirmations for the same protection target
do not create notification noise.

Order-path latency can be measured on the demo account with
`scripts/probe_bybit_demo_order_latency.py`, which places tiny far-from-touch
post-only demo orders and cancels them immediately.

## Creative Alpha Research Log

Latest full-PIT creative alpha pass:

```text
docs/creative_alpha_research_20260517.md
```

Decision: do not change the selected demo strategy. The pass added
disabled-by-default research controls for 7d momentum, proximity-to-high,
prior-month MAX/salience, prior return volatility, and intraday range. None beat
the current liquidity-migration short on strict full-PIT improvement criteria.
The closest result was a lower-drawdown near-30d-high sleeve, but it sacrificed
too much return and was delay sensitive.

## Selected Full-PIT Result

Promoted result after the hold/exit frontier confirmation:

```text
event: liquidity_migration
side: reversal (short)
threshold: top 30% dollar-volume rank migration
filters:
  point-in-time liquidity rank 31-150
  liquidity-rank improvement >= 150
  turnover / prior 7d mean >= 6.0
  event rank fraction <= 0.90
  coin daily_return_1d >= 0%
  coin daily_return_1d - market_median_return_1d >= +8%
  market_pct_up_1d <= 0.55 OR coin daily_return_1d >= +20%
entry delay: 1 hour after signal close
hold: 3 days max
stop: 12% fixed
take profit: 20% fixed
gross exposure: 1.25
capacity: max 6 active symbols
cooldown: 5 days
stop-pressure throttle: pause new entries after 12 realized stops inside 14 days
cost: 3x base round-trip cost
```

Equivalent explicit command:

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
  --take-profit-pcts 0.20 \
  --cost-multipliers 3 \
  --entry-delay-hours 1 \
  --gross-exposure 1.25 \
  --max-active-symbols 6 \
  --cooldown-days 5 \
  --rank-exit-threshold 0.55 \
  --universe-rank-min 31 \
  --universe-rank-max 150 \
  --liquidity-migration-rank-improvement-min 150 \
  --liquidity-migration-turnover-ratio-min 6.0 \
  --liquidity-migration-event-rank-fraction-max 0.90 \
  --liquidity-migration-event-rank-fraction-exclude-min 0 \
  --liquidity-migration-event-rank-fraction-exclude-max 0 \
  --liquidity-migration-day-return-min 0.0 \
  --liquidity-migration-residual-return-min 0.08 \
  --liquidity-migration-market-pct-up-max 0.55 \
  --liquidity-migration-hot-market-day-return-min 0.20 \
  --stop-pressure-window-days 14 \
  --stop-pressure-stop-count 12
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
report: data/research_reports/research_20260516_promoted_default_after_patch
trades: 516
total return: +1218.79%
max drawdown: -14.54%
max no-new-high stretch: 90 days
worst 90d return: -5.89%
worst split return: +75.64%
average split Sharpe-like: 2.67
train return: +75.64%
validation return: +254.58%
OOS return: +111.75%
default chart: volume_event_best_equity_btc.png with BTC overlay and monthly/growth gridlines
promotion gate: pass
```

Funding and execution-realism update from `2026-05-17`:

```text
funded default report: data/research_reports/research_20260517_default_with_funding_oi_features
funded default return: +957.82%
funded default max drawdown: -18.62%
funding return drag: -22.03%

adverse stop-fill report: data/research_reports/research_20260517_default_funding_bar_extreme_stops
adverse stop-fill return: +141.34%
adverse stop-fill max drawdown: -35.88%
adverse stop-fill OOS return: -13.69%
adverse stop-fill promotion gate: fail
```

The strongest new research candidate is the same liquidity-migration short
strategy gated to `funding_7d_sum >= 0` at signal close. It is not the active
demo default yet because there is no forward/demo evidence and the demo cycle
does not currently compute the 7d funding gate. Full report:

```text
data/research_reports/research_20260517_funding7_positive_bar_extreme_stops
return: +213.66%
max drawdown: -16.61%
min split return: +12.10%
funding return: +3.13%
promotion gate: pass under adverse hourly stop fills
```

In the promotion comparison, BTC/ETH/SOL/BNB/XRP/TRX took no direct trades, but
including them improved the PIT rank map and market context. That is why the
default exclusion rule is now stable/peg-only rather than a manual top-coin
blacklist.
