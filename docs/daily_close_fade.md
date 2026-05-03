# Daily Close Fade Research Path

This is a separate alpha from the daily volume-rank system. Do not blend it into
the volume system until it clears costs standalone.

## Hypothesis

Near the UTC daily close, the strongest small/mid-cap perp gainers mean-revert.
The first test shorts the top day-to-date gainers around 22:00-23:15 UTC and
exits mechanically after 30-180 minutes.

This is not a rolling 24h top-gainer test. The ranking is based on performance
from 00:00 UTC to the signal minute because the claimed behavior is tied to the
daily candle close.

## Data

Required datasets:

```text
instruments
klines_1m
```

`instruments` is required because the hard filter excludes symbols listed less
than 10 days before the signal.

## Signal

For each symbol and UTC date:

- compute day-to-date return from the first 1m bar after 00:00 UTC
- compute a vol-adjusted return using prior daily realized volatility
- tag pump-like behavior using extreme return, late volume, VWAP extension,
  late acceleration, and fresh-day-high evidence
- exclude BTC/ETH/SOL/BNB by default
- exclude symbols younger than 10 days

The pump tag is not a hand label. The grid tests `all`, `pump`, and `non_pump`
candidate buckets separately.

## Backtest

The backtest is short-only:

- select top N candidates at the signal minute
- enter after `entry_delay_minutes`
- exit after `hold_minutes`, unless a stop triggers
- record every trade with score, rank, age, pump tags, MAE/MFE, costs, and exit
  reason

Exit reasons:

```text
max_hold
stop_loss
trailing_stop
data_end
```

The baseline no-stop result is important. Stops are tested against it rather
than assumed to help.

## Commands

Download 1m data for a 3-month first pass:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  download-data \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT,BNBUSDT,ADAUSDT,DOTUSDT,LTCUSDT,NEARUSDT,OPUSDT,ARBUSDT,INJUSDT \
  --start 2026-02-03 \
  --end 2026-05-03 \
  --datasets instruments,klines_1m
```

Run one default backtest:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade \
  --signal-time 23:00 \
  --top-n 3 \
  --hold-minutes 120
```

Run the grid:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade-grid \
  --workers 32
```

Windows runner:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_close_fade_1m.ps1 -Workers 32
```

Outputs:

```text
data/daily-close-fade-1m-3m/reports/daily_close_fade_report.md
data/daily-close-fade-1m-3m/reports/daily_close_fade_trades.csv
data/daily-close-fade-1m-3m/reports/daily_close_fade_grid_report.md
data/daily-close-fade-1m-3m/reports/daily_close_fade_grid_results.csv
```

## Current Caution

One-minute data is much heavier than the 1h volume-rank path. Start with 3
months, inspect the trade ledger, then scale to 1 year only if the signal clears
costs and does not depend on one or two symbols.
