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
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_oos_validation/volume_oos_validation_summary.md
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
- Supports signal date windows on `volume-backtest` and `volume-grid` with
  `--start` and `--end`. The start is inclusive and the end is exclusive.
  Trades opened before the end can still exit naturally after the end date.
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
- signal window: all downloaded history unless `--start` / `--end` is set
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

## Stop / Take-Profit Accounting

The detailed `volume-backtest` command uses linear perp return math:

```text
long return  = exit / entry - 1
short return = (entry - exit) / entry
```

Stops and take-profits are side-aware:

- long stop: `entry * (1 - stop_loss_pct)`
- short stop: `entry * (1 + stop_loss_pct)`
- long take profit: `entry * (1 + take_profit_pct)`
- short take profit: `entry * (1 - take_profit_pct)`

If both stop and take-profit are touched inside the same 1h OHLC bar, the
backtester exits at the stop. This is deliberately conservative because 1h bars
do not reveal the true intrabar path.

Returns are weighted before basket aggregation. A 10% trade move at `0.25`
notional weight contributes 2.5% to that basket before costs.

Current limitations:

- stop/TP fills assume the trigger price is fillable inside the bar
- slippage beyond the configured round-trip cost model is not modeled
- `take_profit_pct` must be below 100%, because the system tests long/short
  linear perps and a short-side TP cannot exceed the entry price
- the simple `volume-alpha` IC/forward-return sweep has no TP/SL; TP/SL only
  exists in the detailed `volume-backtest` lifecycle

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
- tail: daily ranks 81-160

The bucket filter is dynamic by day and based on trailing daily quote turnover
inside the downloaded universe.

Latest 3-year current-universe bucket grid:

- core ranks 1-20 favor `long_high_short_low`, meaning high-volume major/core
  names still behave more like trend/liquidity winners than pump fades.
- mid ranks 21-80 favor `short_high_long_low`, meaning the sign flips below
  the core bucket.
- tail ranks 81-160 also favor `short_high_long_low`, but the most aggressive
  return row is stop-heavy and should be treated as an optimized research lead,
  not the production default.
- broad ranks 1-160 can also work with `short_high_long_low`, but it mixes
  regimes and is less interpretable than explicit bucket sleeves.

Current frozen candidate rules for stability testing:

| Candidate | Ranks | Direction | Hold | Quantile | Stop | Rank exit | Status |
|---|---:|---|---:|---:|---|---:|---|
| core trend | 1-20 | long high / short low | 10d | 20% | fixed 30% | on | not active; recent years weak |
| mid fade | 21-80 | short high / long low | 10d | 10% | 3x daily vol | off | promising but failed year 2 |
| tail fade clean | 81-160 | short high / long low | 5d | 20% | none | off | best clean tail lead, recent-listing heavy |
| tail fade max-return | 81-160 | short high / long low | 10d | 50% | fixed 12% | off | high return, optimized/suspicious |
| broad fade | 1-160 | short high / long low | 21d | 10% | none | off | robust full-period, mixed regime |

Stability validation output:

```text
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_oos_validation/volume_oos_validation_summary.md
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_oos_validation/volume_universe_coverage_by_month.csv
```

Current honest read:

- The full 3-year numbers are encouraging, but they are not final proof because
  they use a current top-160 universe.
- Tail ranks had no trades in year 1 because the downloaded current top-160
  universe only had 57-70 active historical symbols in early 2023. The tail
  result is therefore mostly a recent-listing / recent-market result.
- Year 2 is the stress period. Mid fade lost -30.87%, tail clean lost -5.47%,
  and broad fade lost -9.81% at 1x costs. Any official volume sleeve must be
  judged against that failure period.
- The next validation gate is a true point-in-time universe from Bybit archive
  availability/listing dates, not a wider current-universe download.

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

Use date windows for stability checks:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3y-auto150-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  volume-backtest \
  --start 2025-05-03 \
  --end 2026-05-03 \
  --score dollar_volume_rank \
  --universe-rank-min 81 \
  --universe-rank-max 160 \
  --side-mode short_high_long_low \
  --hold-days 5 \
  --rebalance-days 5 \
  --quantile 0.20 \
  --stop-mode none \
  --stop-loss-pct 0
```

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

The corrected Bybit runs say the expansion variants fail, while
`dollar_volume_rank` is the only useful variant so far. Treat that as a lead to
refine, not as proof of a production strategy.

## Current Broad-Universe Read

The broad-universe result is not one simple "volume is bullish" rule. It splits
by liquidity bucket:

- Core/top names: high dollar-volume rank tends to trend.
- Mid/tail names: high dollar-volume rank tends to fade.
- The 2024-2025 year is the weak point for the fade sleeves, so do not mark the
  volume bucket system as official until point-in-time universe testing explains
  or survives that period.

Current priority: build and test point-in-time historical universe membership.
Downloading every current Bybit listing is useful, but it still cannot prove
what a trader would have seen each day in 2023 or 2024.
