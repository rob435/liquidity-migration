# Volume Alpha Research Path

This is the current implementation plan for the stripped-down rebuild. It tests
the podcast volume hypothesis without mixing in the deleted composite stack.

## Hypothesis

Long coins with stronger volume profiles and short coins with weaker volume
profiles.

This does not test taker aggression, carry, quality, OI impulse, or a blended
score. It tests one alpha family only.

The first implementation separates two things that should not be confused:

- volume expansion: increasing daily/3-day volume versus prior volume
- dollar-volume rank: high absolute turnover versus low absolute turnover

## Command

```bash
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-alpha
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-backtest
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-grid --workers 0
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m aggression_carry --data-root data/universe-research --config configs/volume_alpha.default.yaml discover-universe --name top160-current --rank-start 1 --rank-end 160 --max-symbols 160 --min-turnover-24h 2000000 --min-age-days 30 --include-majors
```

## Outputs

```text
data/agc-bybit-3m/reports/volume_alpha_report.md
data/agc-bybit-3m/reports/volume_alpha_report.json
data/agc-bybit-3m/volume_alpha_features
data/agc-bybit-3m/volume_alpha_metrics
data/agc-bybit-3m/volume_alpha_portfolios
data/agc-bybit-3m/reports/volume_backtest_report.md
data/agc-bybit-3m/reports/volume_backtest_trades.csv
data/agc-bybit-3m/volume_backtest_trades
data/agc-bybit-3m/volume_backtest_baskets
data/agc-bybit-3m/volume_backtest_equity
data/agc-bybit-3m/reports/volume_backtest_equity_vs_btc.csv
data/agc-bybit-3m/reports/volume_backtest_monthly_vs_btc.csv
data/agc-bybit-3m/reports/volume_backtest_equity_curve.svg
data/agc-bybit-3m/reports/volume_backtest_monthly_vs_btc.svg
data/agc-bybit-3m/volume_backtest_equity_vs_btc
data/agc-bybit-3m/volume_backtest_monthly
data/agc-bybit-3m/reports/volume_grid_report.md
data/agc-bybit-3m/reports/volume_grid_results.csv
data/agc-bybit-3m/volume_backtest_grid
data/universe-research/reports/universe_top160-current.md
data/universe-research/reports/universe_top160-current_symbols.txt
data/agc-bybit-1y-auto150-20250503-20260503/reports/volume_bucket_sweep_summary.md
```

## Current Scope

- Uses Bybit 1h klines aggregated into UTC daily turnover.
- Does not require Bybit public-trade archives.
- Builds daily volume-only signals.
- Tests 1d, 3d, and 7d forward returns.
- Runs equal-weight long/short portfolios at 20%, 30%, and 50% quantiles.
- Tests base, 2x, and 3x cost assumptions.
- Runs a detailed trade-ledger backtest for the current lead:
  `dollar_volume_rank`, long high-volume names, short low-volume names.
- Records every trade entry, exit, side, score, rank, stop level, exit reason,
  MAE/MFE, gross return, cost return, and net return.
- Runs concurrent parameter grids over hold period, quantile, no/fixed/volatility
  stops, rank exits, cost multipliers, and optional side reversal.
- Breaks detailed backtests down month-by-month against BTC up/down regimes and
  writes SVG equity/monthly visualizations.
- Discovers current Bybit USDT perp universes from instruments/tickers and can
  test daily liquidity-rank buckets inside a downloaded broad universe.

## Detailed Backtest Defaults

The default detailed test is intentionally simple:

- score: `dollar_volume_rank`
- side mode: `long_high_short_low`
- quantile: `0.50`
- gross exposure: `1.0x`
- hold days: `7`
- rebalance days: `7`
- entry delay: `1h` after the daily signal close
- stop loss: `8%` by default; set `--stop-loss-pct 0` to disable for comparison
- take profit: disabled
- costs: configured round-trip cost model

Important: overlapping baskets are not implemented yet. If `hold_days=7`, use
`rebalance_days >= 7`. To test faster turnover, set both to the same shorter
period, for example `--hold-days 3 --rebalance-days 3`.

Exit reasons currently include:

- `stop_loss`
- `take_profit`
- `rank_exit`
- `max_hold`
- `data_end`

The detailed report also writes:

- month-by-month strategy return vs BTC return
- BTC up/down/flat regime summary
- strategy equity aligned to normalized BTC equity
- browser-viewable SVG charts for the equity curve and monthly returns

## Automated Universe

`discover-universe` builds a current Bybit USDT linear-perp snapshot from
instruments and tickers. It filters status, prelisting contracts, settle coin,
listing age, current 24h turnover, current liquidity rank, and optional symbol
exclusions.

Example broad universe for bucket testing:

```bash
python -m aggression_carry \
  --data-root data/universe-research \
  --config configs/volume_alpha.default.yaml \
  discover-universe \
  --name top160-current \
  --rank-start 1 \
  --rank-end 160 \
  --max-symbols 160 \
  --min-turnover-24h 2000000 \
  --min-age-days 30 \
  --include-majors
```

Important: this is a current snapshot, not a point-in-time historical universe.
Backtests using this symbol list still have survivorship bias. Treat the result
as a stronger first pass than a handpicked list, not as final proof.

After downloading a broad universe, run daily liquidity-rank buckets:

```bash
python scripts/run_volume_bucket_sweep.py \
  --data-root data/agc-bybit-1y-auto150-20250503-20260503 \
  --config configs/volume_alpha.default.yaml \
  --workers 0 \
  --include-reverse
```

The current bucket definitions are:

- core: daily ranks 1-20
- mid: daily ranks 21-80
- tail: daily ranks 81-150

The bucket filter is dynamic by day and based on trailing daily quote turnover
inside the downloaded universe.

## Download Resume Behavior

REST downloads for large symbol lists write each symbol immediately and create a
small completion marker under `_download_markers/` after the symbol succeeds.
If the terminal is interrupted, rerun the same `download-data` command; completed
symbol/range chunks are printed as `cached` and skipped.

For broad 1-year or 3-year universes, expect progress lines like:

```text
klines_1h: 1/156 BTCUSDT downloading
klines_1h: 1/156 BTCUSDT rows=26305
klines_1h: 2/156 ETHUSDT cached
```

Do not delete `_download_markers/` unless you intentionally want to refetch a
completed symbol/range.

Use paired runs when judging stops:

```bash
python -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-backtest
python -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-backtest --stop-loss-pct 0
```

If the no-stop run is materially better than the fixed-stop run, the stop is
probably damaging the alpha rather than controlling useful risk.

## Grid Testing

The grid command runs each parameter variant in a separate worker process when
`--workers` is above `1`.

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-1y \
  --config configs/volume_alpha.default.yaml \
  volume-grid \
  --workers 32 \
  --include-reverse
```

`--workers 0` means auto-select CPU count minus one. On a 5950X, use
`--workers 32` only if RAM is comfortable; otherwise use `--workers 16`.

The current grid tests:

- hold/rebalance: `3d`, `7d`, `14d`
- quantiles: `30%`, `50%`
- fixed stops: disabled, `12%`, `20%`, `30%`
- volatility stops: `3x`, `4x` recent daily realized volatility
- rank exit: off/on
- optional reverse side: `short_high_long_low`
- optional daily universe rank filters via `--universe-rank-min` and
  `--universe-rank-max`

GPU note: this is not a CUDA workload yet. The bottleneck is independent Python
trade simulation variants, so process-level CPU parallelism is the right first
optimization. GPU work would require a separate cuDF/CuPy rewrite and only makes
sense after the trade logic stabilizes.

## Overnight 5950X Sweep

For a Windows workstation with a 5950X, use the overnight runner. It discovers
the current top-160 Bybit universe, resumes the 3-year kline download, rebuilds
the volume-alpha feature report, then runs large bucketed grids with reverse
side enabled.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_volume_overnight_sweep.ps1 -Preset deep -Workers 32
```

Presets:

- `deep`: core, mid, tail, and broad buckets; roughly 4,480 variants.
- `tail`: focuses on ranks 51-160; roughly 10,080 variants.
- `insane`: splits core/mid/tail/broad more finely; roughly 20,160 variants.

The runner writes a transcript under:

```text
data/agc-bybit-3y-auto150-20230503-20260503/logs/
```

The download stage uses `_download_markers/`, so rerunning the same script skips
symbols already fetched for the same date range.

## One-Year Run

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_1y_grid.ps1 -Workers 32
```

Manual equivalent:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-1y \
  --config configs/volume_alpha.default.yaml \
  download-data \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT,BNBUSDT,ADAUSDT,DOTUSDT,LTCUSDT,NEARUSDT,OPUSDT,ARBUSDT,INJUSDT \
  --start 2025-05-01 \
  --end 2026-05-01 \
  --datasets instruments,klines_1h

python -m aggression_carry --data-root data/agc-bybit-1y --config configs/volume_alpha.default.yaml volume-alpha
python -m aggression_carry --data-root data/agc-bybit-1y --config configs/volume_alpha.default.yaml volume-grid --workers 32 --include-reverse
```

## Interpretation Rule

Do not combine this alpha with other signals until it clears costs standalone.
If the standalone volume alpha fails, the composite should not receive a volume
component just because it sounds plausible.

The current corrected 3-month Bybit run says the expansion variants fail, while
`dollar_volume_rank` is the only useful variant so far. Treat that as a lead to
refine, not as proof of a production strategy. The detailed backtest is the next
tool for deciding whether that lead survives actual trade lifecycle assumptions.

## Current Broad-Universe Read

One-year current top-150 snapshot test, using 2025-05-03 to 2026-05-03:

- core ranks 1-20: best `long_high_short_low`, 3d hold, 20% buckets,
  rank exit on, +67.86%, Sharpe-like 1.14, max drawdown -26.52%
- mid ranks 21-80: best `short_high_long_low`, 14d hold, 20% buckets,
  rank exit off, +38.92%, Sharpe-like 1.30, max drawdown -11.56%
- tail ranks 81-150: best `short_high_long_low`, 14d hold, 20% buckets,
  rank exit off, +154.10%, Sharpe-like 2.08, max drawdown -16.97%

This is not simply the original "long high-volume / short low-volume" result
getting stronger in lower caps. In the tail bucket the best result reverses the
direction: long the lowest-volume names inside the tail bucket and short the
highest-volume names inside that same tail bucket. That is a different lead and
must be tested separately.
