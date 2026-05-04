# Walk-Forward Universe Standard

This is the proof path for the daily-close fade. Current-top-160 tests are
biased benchmarks.

## Bias To Remove

Current-universe runs leak information:

```text
today's listings
today's liquidity ranks
survivor symbols only
no delisted/dead contracts
no historical top-gainer membership
```

Those tests can show the machinery works. They cannot prove the alpha.

## Correct Daily Process

For each UTC date:

1. Build tradable symbols from information available on or before that day.
2. Include delisted symbols that traded on Bybit that day.
3. Exclude symbols younger than 10 days.
4. Use only bars/trades from 00:00 through 22:00 for ranking.
5. Rank day-to-date vol-adjusted top gainers at 22:00.
6. Apply the prior-liquidity bucket and pump-quality filters.
7. Enter selected shorts with the fixed 22:00-23:00 1m TWAP model.
8. Use average entry for PnL and the 20% disaster stop.
9. Exit by whole-symbol flatten: disaster stop, adaptive protection, max hold,
   or data end.
10. Record every fill assumption, weight, exit reason, and daily PnL.

No current rank, future liquidity, future listing status, or future return is
allowed in candidate selection.

## Archive Tooling

Build the Bybit public archive manifest:

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

Download archive-derived 1m bars:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 16
```

For long jobs, use the resumable batch runner:

```bash
python scripts/run_archive_pit_batches.py \
  --data-root data/daily-close-fade-pit \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --batch-rows 1000 \
  --workers 16 \
  --coverage-every 1
```

Audit coverage:

```bash
python scripts/report_archive_pit_coverage.py \
  --data-root data/daily-close-fade-pit \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --min-bars-per-day 1200 \
  --min-usable-rate 0.95
```

The audit should require next-date coverage because trades can exit after
midnight UTC.

## PIT Backtest Command

After coverage is acceptable:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade \
  --require-archive-membership
```

Do not change thresholds for the PIT test. The point is to test the frozen
22:00-23:00 TWAP contract, not optimize again.

## Labels

Use precise labels in reports:

```text
current-top160: current-listing benchmark, biased
current-all: wider current-listing benchmark, still biased
archive-pit: point-in-time candidate test
```

Only `archive-pit` can support promotion.
