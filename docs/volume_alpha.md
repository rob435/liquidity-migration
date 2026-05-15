# Volume Alpha

This is secondary research. It is not the current default strategy.

## Hypotheses

Test whether daily volume information contains predictive information:

```text
dollar_volume_rank: absolute liquidity / size effect
volume_change_1d: one-day turnover expansion
volume_change_3d: three-day turnover expansion
volume_persistence: persistent turnover above baseline
volume_composite: simple blend of the above
```

This is separate from the daily-close fade. A volume sleeve becomes relevant to
the demo stack only after it has standalone cost-cleared evidence.

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

Important: the older bucket sweep outputs only tested `dollar_volume_rank`.
They were useful for liquidity-bucket behavior, but they did not fully test the
podcast-style “rising volume predicts future price” claim. The current
`promotion` preset tests all volume score families and then applies fixed split
promotion gates.

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

Kept Python helper scripts:

```bash
python scripts/run_volume_bucket_sweep.py --data-root DATA_ROOT --workers 8
python scripts/run_volume_grid_splits.py --data-root DATA_ROOT --workers 8
python scripts/run_volume_grid_splits.py --data-root DATA_ROOT --preset quick --workers 8
python scripts/run_volume_grid_splits.py --data-root DATA_ROOT --preset promotion --workers 8
python scripts/run_volume_grid_splits.py --data-root DATA_ROOT --preset legacy --workers 8
python scripts/evaluate_volume_promotion.py --split-summary DATA_ROOT/reports/volume_grid_splits/volume_grid_split_summary.csv
```

Windows 5950X overnight runner:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_volume_overnight_5950x.ps1
```

The runner pulls `origin/main`, uses `--preset promotion`, `--workers 16`,
sets the Polars/Rayon/OMP/MKL thread caps to `1`, writes logs under the selected
report directory, and runs the promotion gate after the grid completes.

`run_volume_grid_splits.py` defaults to the `smoke` preset. That is deliberate:
the old default grid evaluated 1,620 full trade-ledger variants across the three
standard splits and can run overnight on a laptop. Use `--preset quick` for a
small all-score triage pass, `--preset promotion` only after quick triage shows
a live-relevant family worth deeper testing, and `--preset legacy` only when you
explicitly want the old broad grid.

Multi-worker volume grids default to a thread backend. Process-pool execution
can hang on Polars-heavy grids on the VPS, so only opt into it deliberately with
`VOLUME_GRID_BACKEND=process`.

The split/promotion workflow tests:

```text
scores: dollar_volume_rank, volume_change_1d, volume_change_3d,
  volume_persistence, volume_composite
buckets: core 1-20, mid 21-80, tail 81-160, broad 1-160
splits: 2023-2024 train, 2024-2025 validation, 2025-2026 OOS
promotion gates: positive split survival, drawdown <= 35%, avg Sharpe >= 0.5
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
reports/volume_promotion_splits/<bucket>/volume_grid_split_summary.md
reports/volume_promotion_splits/<bucket>/promotion/volume_promotion_report.md
```

Large data and report outputs stay under `data/` and are not git artifacts.

## Research Discipline

A volume sleeve should not be promoted from headline return alone. A candidate needs:

```text
positive split performance
tolerable worst-split drawdown
reasonable Sharpe-like metric
clear liquidity bucket behavior
no dependence on current-universe membership
```

The next serious step is point-in-time membership, not more optimized historical
parameter searching.
