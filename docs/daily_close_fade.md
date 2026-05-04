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

Current best adaptive-exit candidate from the biased current-universe benchmark:

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

Exit tuning is not alpha proof. Before promoting a TP/SL variant, run the raw
diagnostic command and check that the score itself has a stable cross-sectional
relationship with forward short returns. If high score buckets do not beat low
score buckets without exits, a good-looking TP/SL grid is probably overfit.

The diagnostic path deliberately ignores TP, SL, trailing stops, basket stops,
and compounding. It writes:

```text
daily_close_fade_diagnostic_observations.csv
daily_close_fade_diagnostic_buckets.csv
daily_close_fade_diagnostic_top_baskets.csv
daily_close_fade_diagnostic_ic.csv
daily_close_fade_diagnostic_scenarios.csv
daily_close_fade_diagnostic_monthly.csv
daily_close_fade_diagnostic_month_consistency.csv
```

The report treats cost-adjusted evidence as first-class. A scenario can look
directionally correct before costs but still fail the `cost_edge_pass` check if
the average top-basket edge is negative after the configured round-trip cost or
if fewer than half of the tested months are positive after cost. Use `--start`
and `--end` for in-sample/out-of-sample splits before promoting an exit rule.

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

Run raw anti-overfit diagnostics before trusting exit variants:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade-diagnostics \
  --signal-times 22:15 \
  --entry-delays 1,15,60 \
  --horizons 60,180 \
  --scores vol_adjusted_day_return,day_return,late_volume_ratio,vwap_extension,pump_score \
  --top-ns 3,5,10 \
  --buckets 10 \
  --cost-multiplier 1 \
  --start 2026-02-03 \
  --end 2026-05-03 \
  --pump-filter pump \
  --liquidity-rank-min 31 \
  --liquidity-rank-max 150
```

Use the wider `22:15,22:45,23:00` by `1,15,60,120` by `30,60,120,180`
diagnostic only as a batch job. On the 3-year 1m current top-160 dataset it is
large enough to be annoying on a laptop.

Run fixed train/validation/OOS split diagnostics:

```bash
python scripts/run_daily_close_fade_split_diagnostics.py \
  --data-root data/daily-close-fade-1m-3y-current-top160-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  --splits train_2023_2024:2023-05-03:2024-05-03,validation_2024_2025:2024-05-03:2025-05-03,oos_2025_2026:2025-05-03:2026-05-03 \
  --signal-times 22:15 \
  --entry-delays 1,15,60 \
  --horizons 60,180 \
  --scores vol_adjusted_day_return,day_return,late_volume_ratio,vwap_extension,pump_score \
  --top-ns 3,5,10 \
  --pump-filter pump \
  --liquidity-rank-min 31 \
  --liquidity-rank-max 150
```

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_close_fade_oos_splits.ps1
```

The split runner is intentionally conservative. It ranks scenarios by whether
they survive each split after costs, not by the single best historical return.
That is the right way to use it before touching TP/SL rules.

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

Audit archive PIT coverage before running the backtest:

```bash
python scripts/report_archive_pit_coverage.py \
  --data-root data/daily-close-fade-pit \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --min-bars-per-day 1200
```

On Windows, the combined PIT bootstrap is:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_archive_pit_bootstrap.ps1 `
  -DataRoot data\daily-close-fade-pit-20230503-20260503 `
  -Start 2023-05-03 `
  -End 2026-05-03 `
  -ManifestWorkers 16 `
  -DownloadWorkers 16
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
submit demo or live strategy orders. The separate `bybit-demo-probe` command is
only for tiny demo auth/order create/cancel checks, and `bybit-demo-sync` can
mirror an existing paper ledger into its own capped demo execution ledger.

Use the sleeve runner for the current paper-forward campaign:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run-sleeves
```

This runs isolated ledgers for:

```text
control_top_1_30
core_31_150
microcap_151_plus
```

The aggregate report is:

```text
data/forward-paper/reports/forward_sleeves_report.md
```

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
- raw diagnostics are sobering. On the 3y current top-160 sample, the narrowed
  diagnostic produced 256,830 observations across 90 scenarios. Best raw
  direction was `pump_score`, 22:15 signal, 15-minute entry delay, 60-minute
  horizon, top 5: mean basket short return +0.036% gross with IC t-stat 1.41.
  That is below the configured round-trip cost model. The matching costed
  trade-ledger check with 20% disaster stop and no adaptive exit returned
  -49.50%. So the naked entry is not tradable as-is.
- split diagnostics are even stricter. On the same current top-160 sample split
  into 2023-2024 train, 2024-2025 validation, and 2025-2026 OOS windows, no raw
  diagnostic scenario passed costs in all three windows. The strongest 2025-2026
  `pump_score` setups were positive after costs, but they failed in the older
  windows. This means the current adaptive-exit result is not enough by itself;
  promotion needs point-in-time universe work and stronger entry conditioning.
- research artifacts are under
  `data/research_reports/liquidity_rank_filter/`.

Honest read: the adaptive exit is promising, but it came from grid testing and
must be treated as a candidate, not truth. The 31-150 liquidity filter better
matches the pump-and-dump thesis, but the entry only deserves promotion if the
diagnostic bucket/IC reports show that high scores predict future short returns
without exit engineering at a size that can pay costs. The current raw
diagnostic does not clear that bar. This is still not final alpha proof until
the point-in-time archive universe is complete.

Next research step: treat rank 151+ as a separate microcap sleeve, not as a
blind extension of the core book. The live scanner will see more rank-151+
symbols than the current top-160 historical sample, but that edge is only real
if it survives turnover floors, spread checks, and capacity-limited sizing.

Latest TP/exit sweep summary:

```text
data/daily-close-fade-1m-1y-20250503-20260503/reports/tp_exit_sweep_summary.md
```
