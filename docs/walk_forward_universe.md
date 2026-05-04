# Walk-Forward Universe Standard

This is the standard for proving the daily-close fade alpha without universe
selection bias.

## Problem

The fast 1-year and 3-year runs that use `universe_top160-current_symbols.txt`
are useful smoke tests, but they are biased:

- the symbols are selected from today's Bybit universe
- delisted contracts are missing
- current 24h turnover is not information a historical trader had
- a dead coin that would have appeared in a top-gainer list may be absent

Those tests can say "the machinery works" and "the edge is worth deeper work."
They cannot say "this is confirmed and tradeable."

## Correct Test

For each UTC day:

1. Build the eligible symbol set from data that existed on or before that day.
2. Include delisted/dead contracts if they traded on Bybit that day.
3. Exclude symbols younger than 10 days using launch time when available, or a
   conservative archive first-seen date when launch time is unavailable.
4. Use only intraday bars/trades from 00:00 UTC through the signal minute.
5. Rank day-to-date top gainers at the signal minute.
6. Apply the sleeve's baseline-liquidity bucket and turnover floors.
7. Size selected shorts with any capacity caps that would have been known at
   that time.
8. Enter the selected shorts after the configured entry delay.
9. Exit mechanically by max hold, stop, trailing stop, or data end.
10. Record every trade, actual weight, capacity cap, and exit reason.

No current rank, current turnover, current survivorship list, or future
performance is allowed in the symbol-selection step.

## Implemented Path

`archive-manifest` scans the Bybit public trading archive and writes
`archive_trade_manifest`, one row per symbol/date archive file.

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  archive-manifest \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --quote-suffix USDT \
  --workers 16
```

`download-data --datasets archive_klines_1m` downloads the public trade archive
for the requested symbol/date range and builds `klines_1m` directly from trades.
This is the route for delisted symbols that Bybit's current REST kline endpoint
no longer returns.

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  download-data \
  --symbols BTCUSDT \
  --start 2023-05-03 \
  --end 2023-05-04 \
  --datasets archive_klines_1m \
  --archive-url-template 'https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz'
```

For full walk-forward runs, prefer the manifest-driven downloader. It resumes by
skipping existing `klines_1m/date=.../symbol=...` partitions and records failed
symbol/date rows in a report instead of silently losing them.

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 32
```

Audit manifest and 1m partition coverage before trusting any walk-forward
result:

```bash
python scripts/report_archive_pit_coverage.py \
  --data-root data/daily-close-fade-pit \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --min-bars-per-day 1200
```

The coverage report writes `archive_pit_coverage_rows.csv`,
`archive_pit_coverage_monthly.csv`, and `archive_pit_coverage_symbols.csv`.
By default it also requires the next UTC date partition, because a daily-close
trade entered around 22:15-23:15 UTC may exit after midnight. Rows that lack the
next-day partition are not counted as usable for close-fade validation.

On Windows, use the bootstrap wrapper to run the manifest, resumable archive
download, and coverage audit in one transcripted job:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_archive_pit_bootstrap.ps1 `
  -DataRoot data\daily-close-fade-pit-20230503-20260503 `
  -Start 2023-05-03 `
  -End 2026-05-03 `
  -ManifestWorkers 16 `
  -DownloadWorkers 16
```

For a first smoke run, add `-MaxSymbols 5 -MaxRows 50`. For the real PIT build,
leave both at `0` so the downloader consumes all missing manifest rows.

`daily-close-fade --require-archive-membership` forces the strategy to select
only symbol/date pairs present in the archive manifest.

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade \
  --signal-time 23:00 \
  --top-n 5 \
  --hold-minutes 90 \
  --pump-filter pump \
  --stop-loss-pct 0.05 \
  --require-archive-membership
```

## Remaining Work

The complete 3-year point-in-time test needs enough disk, network time, and CPU
time to run `archive-download-klines` across every eligible USDT perp
symbol/date. That will be much larger than the current REST survivor tests but
is the correct evidence path.

Until that finishes, label results as:

- `current-top160`: biased benchmark
- `current-all`: reduced ranking bias, still survivorship-biased
- `archive-pit`: proper point-in-time candidate

For rank 151+ microcaps, `archive-pit` is not enough by itself. The test must
also preserve capacity-limited weights from prior baseline turnover and
day-to-date turnover; otherwise it can overstate returns by giving full-size
allocations to symbols that were technically listed but too thin to trade.
