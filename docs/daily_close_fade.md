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
than 10 days before the signal. For point-in-time archive tests,
`archive_trade_manifest` can provide a conservative first-seen date for symbols
that are no longer in Bybit's current instruments endpoint.

Current-universe tests are research benchmarks only. They are not acceptable as
final evidence because they use symbols known today. The clean path is in
`docs/walk_forward_universe.md`.

## Signal

For each symbol and UTC date:

- compute day-to-date return from the first 1m bar after 00:00 UTC
- compute a vol-adjusted return using prior daily realized volatility
- tag pump-like behavior using extreme return, late volume, VWAP extension,
  late acceleration, and fresh-day-high evidence
- exclude BTC/ETH/SOL/BNB by default
- exclude symbols younger than 10 days
- compute baseline liquidity from prior daily quote turnover, ranked
  cross-sectionally by date
- default to baseline liquidity ranks 31-150 so the system skips the largest
  names without hand-maintaining a stale ignore list
- optionally require `archive_trade_manifest` symbol/date membership so the
  ranking only sees symbols with public archive coverage on that UTC day

The pump tag is not a hand label. The grid tests `all`, `pump`, and `non_pump`
candidate buckets separately.

## Backtest

The backtest is short-only:

- select top N candidates at the signal minute
- start from `gross_exposure / selected_count`
- optionally cap actual weight by per-symbol weight, day-to-date turnover, and
  prior baseline turnover so thin microcaps do not get fake full-size fills
- enter after `entry_delay_minutes`
- exit after `hold_minutes`, unless a stop, fixed TP, trailing stop,
  volatility-scaled trail, MFE-giveback, or VWAP-reversion exit triggers
- record every trade with score, rank, age, pump tags, MAE/MFE, costs, and exit
  reason

For Bybit USDT linear perps, short return is modeled as:

```text
(entry_price - exit_price) / entry_price
```

Do not use inverse-contract math here. Earlier exploratory daily-close numbers
from before this correction were too optimistic and are not evidence.

Exit reasons:

```text
max_hold
stop_loss
take_profit
basket_stop
trailing_stop
vol_trailing_stop
mfe_giveback
vwap_reversion
data_end
```

Fixed TP is active immediately after entry. `stop_delay_minutes` delays only the
hard stop, trailing stop, volatility trail, and MFE-giveback exit. If a stop and
TP are both active and both touched inside the same 1m OHLC bar, the stop wins
because the intrabar path is unknown.

Current best adaptive-exit candidate:

- no fixed TP
- 20% per-symbol disaster stop
- baseline liquidity ranks 31-150 using prior 7-day average quote turnover
- after the 15-minute stop delay, trail the short from best observed price by
  `0.25 * prior_daily_realized_vol`
- after a +1% favorable move, allow only 20% giveback of max favorable excursion
- no VWAP-reversion exit

Capacity controls are off for the core sleeve by default. They are available for
microcap tests:

```text
account_equity
max_position_weight
max_trade_notional_pct_of_day_turnover
max_trade_notional_pct_of_baseline_turnover
```

If a cap is active, the trade ledger records `target_weight`, actual `weight`,
`position_weight_cap`, `capacity_limited`, `capacity_cap_reason`,
`target_notional`, and `actual_notional`. This makes lower-liquidity tests more
honest: a rank-250 coin can still qualify, but it may only get a small sleeve
weight if normal turnover cannot support the target notional.

This is not the same as a fixed take-profit. It tries to leave room for large
fades while cutting rebounds after the move has started.

The baseline no-stop result is important. Stops are tested against it rather
than assumed to help.

`basket_stop` is a research-level portfolio loss cap. It marks the open basket
minute by minute using 1m closes and closes still-open trades once aggregate
basket PnL crosses `-basket_stop_loss_pct`. This is not a live kill switch; it
exists so the trade ledger can show whether a portfolio-level cap helps or just
cuts the alpha.

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
  --signal-time 22:15 \
  --top-n 5 \
  --hold-minutes 180 \
  --pump-filter pump \
  --stop-loss-pct 0.20 \
  --take-profit-pct 0 \
  --liquidity-lookback-days 7 \
  --liquidity-rank-min 31 \
  --liquidity-rank-max 150 \
  --account-equity 10000 \
  --trailing-stop-pct 0 \
  --vol-trailing-stop-mult 0.25 \
  --vol-trailing-activation-mult 0 \
  --mfe-giveback-activation-pct 0.01 \
  --mfe-giveback-pct 0.20
```

Build a public-archive symbol/date manifest for walk-forward universe research:

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

Download trade-derived 1m bars for a symbol from Bybit public archives:

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

Or consume the manifest directly and resume missing symbol/date rows:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 32
```

Run a manifest-gated backtest once the archive-derived 1m bars are present:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade \
  --signal-time 22:15 \
  --top-n 5 \
  --gross-exposure 1.0 \
  --hold-minutes 180 \
  --pump-filter pump \
  --liquidity-rank-min 31 \
  --liquidity-rank-max 150 \
  --stop-loss-pct 0.20 \
  --basket-stop-loss-pct 0 \
  --require-archive-membership
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

Run the three-sleeve comparison:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade-sleeves
```

Sleeves:

```text
major_control: ranks 1-30, includes majors, no capacity caps
core: ranks 31-150, top 5, no capacity caps
microcap: ranks 151+, top 3, 0.50x gross, minimum turnover filters,
          and capacity caps for a $10k account
```

Microcap starting assumptions:

```text
min_baseline_turnover = 250,000 quote/day
min_day_turnover = 750,000 quote by signal time
min_last_60m_turnover = 75,000 quote
max_position_weight = 20%
max_trade_notional_pct_of_day_turnover = 0.20%
max_trade_notional_pct_of_baseline_turnover = 0.50%
```

Outputs:

```text
data/.../reports/daily_close_fade_sleeves_report.md
data/.../reports/daily_close_fade_sleeves_results.csv
data/.../reports/daily_close_fade_sleeves_trades.csv
```

## Paper Forward Test

The live observation path is documented in `docs/forward_testing.md`.

It scans the full current Bybit USDT-linear public universe, applies the same
daily-close fade candidate logic, opens paper shorts only, tracks exits, and can
send Telegram notifications. It does not use Bybit private keys and does not
submit demo or live orders.

## Current Caution

One-minute data is much heavier than the 1h volume-rank path. Start with 3
months, inspect the trade ledger, then scale to 1 year only if the signal clears
costs and does not depend on one or two symbols.

Do not treat current top-160 or current all-symbol tests as production-grade
alpha proof. They miss delisted contracts and can overstate results by excluding
dead symbols that a trader would have seen at the time.

## Current Risk Read

Corrected current-universe benchmarks, still survivorship-biased:

- raw/simple setup: 22:15 UTC, short top 5 pump-like names,
  `vol_adjusted_day_return`, 180m hold, 20% per-symbol stop, no fixed TP
- one-year simple/no-adaptive result: +86.69%, Sharpe-like 1.39, max drawdown
  -28.95%
- three-year current-universe simple/no-adaptive result: -60.96%, Sharpe-like
  -0.31, max drawdown -90.38%; the simple entry is not confirmed
- fixed TP is rejected for now. It raises win rate but cuts too much right tail.
  VWAP-reversion exits had the same problem.
- best current default candidate: 22:15 UTC, top 5 pump-like shorts, baseline
  liquidity ranks 31-150, 180m max hold, 20% disaster stop, no fixed TP, no
  standard trailing stop, `0.25x` daily-vol trail active after the 15-minute
  stop delay, and 20% MFE giveback after +1% favorable excursion.
- liquidity bucket read:
  - top ranks 1-30 were weaker: 3y +77.96%, Sharpe-like 1.31, max drawdown
    -21.04%, and negative at 2x costs
  - ranks 31-150 are the current default: 1y +126.33%, Sharpe-like 4.48, max
    drawdown -6.48%; 3y +249.03%, Sharpe-like 2.92, max drawdown -8.08%
  - ranks 151-300 failed on the current data; too few trades and negative
    returns
  - all ranks 1-300 still had the highest raw 3y return, +333.35%, but keeping
    the big names violates the thesis and worsens the top-bucket risk profile
- cost sensitivity matters. On the 3y current top-160 sample, this adaptive
  31-150 candidate is +249.03% at base costs, +50.51% at 2x costs, and -35.15%
  at 3x costs. It needs execution monitoring before risking money.
- research artifacts are under
  `data/research_reports/liquidity_rank_filter/`.

Honest read: the adaptive exit is promising; the raw daily-close entry is not.
The 31-150 liquidity filter better matches the pump-and-dump thesis, but this
is still not final alpha proof until the point-in-time archive universe is
complete.

Next research step: treat rank 151+ as a separate microcap sleeve, not as a
blind extension of the core book. The live scanner will see more rank-151+
symbols than the current top-160 historical sample, but that edge is only real
if it survives turnover floors, spread checks, and capacity-limited sizing.
- at `0.25x` gross: +20.63%, Sharpe-like 1.39, max drawdown -7.82%

The 5% stop lowers drawdown but damages the return profile. Basket stops at
3-10% did not improve the current benchmark enough to justify making them the
lead variant. The locked-in research default is the 20% per-symbol disaster
stop plus no fixed TP. For paper forward testing, use the 1.5% trailing stop
after +2% favorable excursion because it materially improved drawdown with a
small raw-return give-up. Change gross exposure, not the disaster stop, when
dialing risk down.

Latest TP/exit sweep summary:

```text
data/daily-close-fade-1m-1y-20250503-20260503/reports/tp_exit_sweep_summary.md
```
