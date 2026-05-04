# Volume Alpha

This is secondary research. It is not the current default strategy.

## Hypothesis

Test whether daily dollar-volume rank contains predictive information:

```text
core names: high volume can trend
mid/tail names: high volume can fade
```

This is separate from the daily-close fade. Do not blend the two until both are
validated standalone.

## Current Honest Read

The 3-year current-top-160 tests were encouraging but not proof:

```text
Universe: current Bybit top-160 snapshot
Bias: survivorship and current-liquidity selection
Weak period: 2024-2025 split
Best next gate: point-in-time historical universe
```

The broad result is not one universal rule. It changes by liquidity bucket:

```text
Ranks 1-20: high-volume names behaved more like trend/liquidity winners.
Ranks 21-80: high-volume names more often faded.
Ranks 81-160: high-volume fade was promising but recent-listing heavy.
```

## Commands

Feature/IC report:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3y-auto150-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  volume-alpha
```

Detailed ledger backtest:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3y-auto150-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  volume-backtest
```

Grid:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3y-auto150-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  volume-grid \
  --workers 8 \
  --include-reverse
```

Windows overnight suite:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_research_overnight_suite.ps1 `
  -Suite volume `
  -VolumePreset insane `
  -Workers 8
```

## Backtest Accounting

Linear perp returns:

```text
long return  = exit / entry - 1
short return = (entry - exit) / entry
```

Stops and take-profits are side-aware:

```text
long stop: entry * (1 - stop_loss_pct)
short stop: entry * (1 + stop_loss_pct)
long TP: entry * (1 + take_profit_pct)
short TP: entry * (1 - take_profit_pct)
```

If both stop and TP touch inside the same 1h OHLC bar, the stop wins. That is
conservative because 1h bars do not reveal intrabar path.

## Generated Outputs

Typical report paths:

```text
reports/volume_alpha_report.md
reports/volume_backtest_report.md
reports/volume_backtest_trades.csv
reports/volume_backtest_equity_curve.svg
reports/volume_grid_report.md
reports/volume_grid_results.csv
reports/volume_bucket_sweep_summary.md
```

Large data and report outputs stay under `data/` and are not git artifacts.

## Research Discipline

Do not promote a volume sleeve from headline return alone. A candidate needs:

```text
positive split performance
tolerable worst-split drawdown
reasonable Sharpe-like metric
clear liquidity bucket behavior
no dependence on current-universe membership
```

The next serious step is point-in-time membership, not more optimized historical
parameter searching.
