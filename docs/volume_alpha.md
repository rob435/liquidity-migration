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

top_volume_leadership:
  symbol freshly enters the PIT top-volume/top-liquidity cohort
  requires mature listing age, turnover expansion, positive residual return,
  strong daily close location, and broad market confirmation

orderly_leadership_pullback:
  liquid prior leader rests without a blow-off day
  tests whether persistent volume leadership continues after a controlled pause

volume_shelf_reclaim:
  quiet prior regime re-accelerates with a strong close and modest range reclaim
  tests a broader, lower-convexity long sleeve than top-volume leadership

reclaim_breakout:
  volume/price reclaim through the prior range high
  tests long continuation after a quiet prior regime

capitulation_reclaim:
  rebound after prior drawdown and capitulation
  tests long snapback after punished names reclaim the daily range

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

The runner checks every 60 seconds by default, sizes each accepted coin from
the backtest weight (`gross_exposure / max_active_symbols`, currently 19.40% of
current Bybit demo USDT equity), exits before entries, sends Telegram status
with wallet equity/open positions/unrealized PnL when enabled, and records
`event_demo_trades`, `event_demo_orders`, and `event_demo_cycles` ledgers. It is
a current-universe forward tester, so it is allowed for demo evidence and
operations, not for historical promotion evidence.

## Legacy Full-PIT Baseline

Superseded result after the hold/exit frontier confirmation:

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

## Promoted Active Strategy - Union Crowding Veto

The March-only crowding patch is rejected. A hostile CSV audit found 10
same-entry-hour toxic stop clusters and 20 stop-loss trades inside those
clusters; the March rule covered only 4 of those 20 stop trades.

Promoted research and demo-default strategy:

```text
variant: adaptive hot-band liquidity_migration short + union_pathology crowding veto
report: data/research_reports/frontier_union_crowding_promoted_20260517
audit: data/research_reports/frontier_crowding_pathology_audit_20260517
trades: 444
total return: +1950.72%
max drawdown: -10.74%
max no-new-high stretch: 51 days
worst 90d return: -4.64%
worst split return: +113.73%
average split Sharpe-like: 3.72
OOS return: +177.58%
full PIT universe pass: true
promotion status: active volume-events and Bybit demo default as of 2026-05-17
```

The paper shadow step was skipped by explicit user decision. That is a real
validation gap, so this promotion remains demo-only and must not be interpreted
as real-money readiness.

Equivalent explicit command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events \
  --event-types liquidity_migration \
  --thresholds 0.4 \
  --hold-days 3 \
  --sides reversal \
  --stop-loss-pcts 0.12 \
  --take-profit-pcts 0.25 \
  --cost-multipliers 3 \
  --entry-delay-hours 1 \
  --gross-exposure 0.97 \
  --max-active-symbols 5 \
  --cooldown-days 5 \
  --rank-exit-threshold 0.55 \
  --universe-rank-min 31 \
  --universe-rank-max 150 \
  --liquidity-migration-rank-improvement-min 150 \
  --liquidity-migration-turnover-ratio-min 6.0 \
  --liquidity-migration-event-rank-fraction-max 0.90 \
  --liquidity-migration-day-return-min 0.0 \
  --liquidity-migration-residual-return-min 0.08 \
  --liquidity-migration-market-pct-up-max 0.65 \
  --liquidity-migration-hot-market-day-return-min 0.16 \
  --liquidity-migration-hot-market-day-return-band 0.015 \
  --liquidity-migration-close-location-min 0.45 \
  --liquidity-migration-pit-age-days-min 90 \
  --stop-pressure-window-days 10 \
  --stop-pressure-stop-count 7 \
  --realized-loss-pressure-window-days 5 \
  --realized-loss-pressure-loss-count 6 \
  --realized-loss-pressure-min-loss-abs 0.0 \
  --liquidity-migration-crowding-filter union_pathology
```

The `union_pathology` veto is causal and only uses selected same-entry-hour
signals plus event-day tape shape:

```text
crowded hour: selected entry-hour signal count >= 2

veto if any regime is true:
  stalled low-turnover migration:
    entry-hour average final-6h return <= 3%
    individual close location >= 65%
    turnover / prior 7d mean <= 20

  late-turnover concentration:
    entry-hour max final-6h turnover share >= 90%
    individual final-6h return >= 3%
    turnover / prior 7d mean >= 12

  weak-tape high-share squeeze:
    market_pct_up_1d <= 65%
    entry-hour average final-6h turnover share >= 50%
```

Legacy selected-default full-PIT result on `2023-05-03` to `2026-05-03`:

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

In the promotion comparison, BTC/ETH/SOL/BNB/XRP/TRX took no direct trades, but
including them improved the PIT rank map and market context. That is why the
default exclusion rule is now stable/peg-only rather than a manual top-coin
blacklist.

## Long Complement Candidate

This is a standalone long research result to complement the selected short
liquidity-migration system. It is not the demo default until forward/demo
evidence exists and portfolio interaction with the short book is tested.

Selected candidate:

```text
event: top_volume_leadership
side: continuation (long)
threshold: top 25% dollar-volume rank score
filters:
  point-in-time liquidity rank <= 30
  prior 7d liquidity rank >= 31
  symbol age >= 120 days
  turnover / prior 7d mean >= 1.25
  coin daily_return_1d >= +3%
  coin daily_return_1d - market_median_return_1d >= +3%
  daily close position in high-low range >= 0.80
  market_pct_up_1d >= 0.55
  market_median_return_1d >= 0%
  BTC return >= 0%
hold: 3 days max
stop: 12% fixed
take profit: 50% fixed
gross exposure: 1.25
capacity: max 6 active symbols
cooldown: 2 days
```

Equivalent explicit command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events \
  --event-types top_volume_leadership \
  --thresholds 0.25 \
  --hold-days 3 \
  --sides continuation \
  --stop-loss-pcts 0.12 \
  --take-profit-pcts 0.50 \
  --cost-multipliers 1,3,5 \
  --max-active-symbols 6 \
  --cooldown-days 2 \
  --top-volume-rank-max 30 \
  --top-volume-prior-rank-min 31 \
  --top-volume-min-age-days 120 \
  --top-volume-turnover-ratio-min 1.25 \
  --top-volume-day-return-min 0.03 \
  --top-volume-residual-return-min 0.03 \
  --top-volume-close-position-min 0.80 \
  --market-pct-up-1d-min 0.55 \
  --market-median-return-1d-min 0 \
  --btc-return-1d-min 0
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
report: data/research_reports/top_volume_aggressive_q25h3_cost_grid_20260517
3x audit ledger: data/research_reports/top_volume_aggressive_q25h3_selected_c3_20260517
trades: 137
cost 1x return: +125.68%, max drawdown: -19.47%, min split: +13.24%
cost 3x return: +113.70%, max drawdown: -20.63%, min split: +11.14%
cost 5x return: +102.36%, max drawdown: -21.77%, min split: +9.07%
worst 90d return at 3x cost: -14.22%
train / validation / OOS at 3x cost: +11.14% / +53.90% / +24.95%
promotion gate: pass
```

Rejected or secondary long research from the same session:

```text
generic breadth-gated volume families:
  report: data/research_reports/orthogonal_existing_long_breadth_20260517
  scenarios: 270
  promotable: 0
  best return: -18.91%

selloff-exhaustion reversal:
  report: data/research_reports/orthogonal_selloff_reversal_20260517
  scenarios: 144
  promotable: 0
  best return: -65.84%

volume_shelf_reclaim:
  report: data/research_reports/volume_shelf_reclaim_selected_cost_grid_20260517
  3x selected return: +45.30%, max drawdown: -15.98%, trades: 371
  status: viable secondary sleeve, not a replacement; fails 5x split robustness

top-volume + shelf blend:
  report: data/research_reports/long_blend_top_volume_shelf_20260517
  status: did not improve top-volume standalone return or drawdown enough
```

## True Hedge Candidate

The current best hedge research does not promote a standalone long alpha. The
short book's worst drawdowns are mostly idiosyncratic alt squeezes with
exploding open interest, often while BTC is flat or down. A generic long-only
top-volume sleeve is not a good hedge for that risk.

Recommended balanced hedge overlay:

```text
base system: selected liquidity_migration short
when signal open_interest_return_1d >= +40%:
  size that short trade at 75% of normal notional
when BTC return >= 0% OR market_pct_up_1d >= 70%:
  add BTCUSDT long hedge at 50% of that short trade's normal notional
  hold the BTC hedge for the same entry/exit window as the short trade
cost: 3x base round-trip cost on the hedge leg
```

Full-PIT result versus the inspected short baseline:

```text
research report: data/research_reports/true_hedge_research_20260517
short baseline report: /Users/jhbvdnsbkvnsd/Desktop/MODEL050426/data/research_reports/research_20260517_default_with_funding_oi_features

baseline return: +957.82%
baseline max drawdown: -18.62%
baseline worst 90d: -7.00%
baseline split returns: +77.60% / +221.83% / +85.07%

balanced hedge return: +874.95%
balanced hedge max drawdown: -14.86%
balanced hedge worst 90d: -4.37%
balanced hedge split returns: +76.45% / +213.32% / +76.35%
return retention: 91.35%
drawdown improvement: +3.77 percentage points
worst-90d improvement: +2.63 percentage points
promotion status: research pass, not demo-integrated
```

Maximum-protection variant:

```text
when signal open_interest_return_1d >= +125%:
  size that short trade at 50% of normal notional
when BTC return >= 0% OR market_pct_up_1d >= 70%:
  add ETHUSDT long hedge at 50% of that short trade's normal notional

return: +838.34%
max drawdown: -13.97%
worst 90d: -5.35%
return retention: 87.53%
```

Do not mix the prior long-only top-volume sleeve into the short stack as a
hedge. In capital-normalized tests it cut drawdown only by reallocating away
from the short edge and failed the true-hedge retention gate.
