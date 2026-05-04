# MODEL050426

Bybit crypto research repo. This has been stripped down around isolated alpha
research paths. The old live bot and blended signal stack are intentionally
gone.

## Current Source Of Truth

- Current implementation plan: [docs/volume_alpha.md](docs/volume_alpha.md)
- Daily close fade plan: [docs/daily_close_fade.md](docs/daily_close_fade.md)
- Paper forward testing: [docs/forward_testing.md](docs/forward_testing.md)
- Walk-forward universe standard: [docs/walk_forward_universe.md](docs/walk_forward_universe.md)
- Bybit data constraints/background: [docs/bybit_aggression_carry_system_codex_spec.md](docs/bybit_aggression_carry_system_codex_spec.md)
- Windows setup: [docs/WINDOWS_QUICKSTART.md](docs/WINDOWS_QUICKSTART.md)

The Bybit aggression-carry spec is still useful for venue/data details, especially
the warning that taker aggression requires signed public trades. It is not a
license to rebuild the old composite stack.

## What Exists

- `aggression_carry/`: public data download, Parquet storage, signed-flow parsing,
  fixture data, isolated `volume-alpha` research sweep, and detailed
  `volume-backtest` trade-ledger backtest.
- `aggression_carry/daily_close_fade.py`: separate 1m UTC daily-close top-gainer
  short-fade research path, including major/core/microcap sleeve comparison and
  capacity-limited sizing for thin names.
- `aggression_carry/forward_test.py`: public-data paper forward tester for the
  daily-close fade. It scans the live Bybit universe and writes a paper ledger;
  it never submits exchange orders.
- `bybit-demo-probe` and `bybit-demo-sync`: isolated Bybit demo-only order
  plumbing. The probe tests auth/place/cancel. The sync mirrors an existing
  paper ledger into tiny capped demo orders and its own execution ledger.
- `archive-manifest` and `download-data --datasets archive_klines_1m`: public
  Bybit archive path for point-in-time symbol/date membership and
  trade-derived 1m bars.
- `configs/volume_alpha.default.yaml`: current research config.
- `scripts/run_agc_3m.ps1`: Windows 3-month volume-alpha run.
- `scripts/run_volume_bucket_sweep.py`: daily liquidity-rank bucket grid runner.

## What Was Removed

- Old live runtime: `main.py`, `execution.py`, `signal_engine.py`, `state.py`,
  `runtime_monitor.py`, `runtime_validation.py`, `alerting.py`.
- Old root research/backtest plumbing tied to that runtime.
- Old blended aggression/carry/momentum/quality/OI feature/report/sweep modules.
- Tests that only protected the deleted behavior.
- Repo-local Codex hooks and AO/Composio helper installer files. Agent tooling
  now lives outside the trading research repo.

## Commands

Install and test:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Tiny fixture run:

```bash
python -m aggression_carry --data-root .tmp/volume-fixture download-data --fixture
python -m aggression_carry --data-root .tmp/volume-fixture volume-alpha
python -m aggression_carry --data-root .tmp/volume-fixture volume-backtest --hold-days 1 --rebalance-days 1
```

Discover a current Bybit universe:

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

3-month Bybit run:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3m \
  --config configs/volume_alpha.default.yaml \
  download-data \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT,BNBUSDT,ADAUSDT,DOTUSDT,LTCUSDT,NEARUSDT,OPUSDT,ARBUSDT,INJUSDT \
  --start 2025-01-01 \
  --end 2025-04-01 \
  --datasets instruments,klines_1h

python -m aggression_carry \
  --data-root data/agc-bybit-3m \
  --config configs/volume_alpha.default.yaml \
  volume-alpha

python -m aggression_carry \
  --data-root data/agc-bybit-3m \
  --config configs/volume_alpha.default.yaml \
  volume-backtest
```

Reports:

```text
data/agc-bybit-3m/reports/volume_alpha_report.md
data/agc-bybit-3m/reports/volume_backtest_report.md
data/agc-bybit-3m/reports/volume_backtest_trades.csv
data/agc-bybit-3m/reports/volume_backtest_equity_vs_btc.csv
data/agc-bybit-3m/reports/volume_backtest_monthly_vs_btc.csv
data/agc-bybit-3m/reports/volume_backtest_equity_curve.svg
data/agc-bybit-3m/reports/volume_backtest_monthly_vs_btc.svg
```

Paper forward test:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-scan

python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run

python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-report
```

Telegram notification-only mode:

```bash
export TELEGRAM_BOT_TOKEN="123456:abc..."
export TELEGRAM_CHAT_ID="123456789"

python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run \
  --telegram
```

The forward tester uses public market data only. It does not use Bybit private
keys and does not submit orders.

Bybit demo probe:

```bash
export BYBIT_DEMO_API_KEY="..."
export BYBIT_DEMO_API_SECRET="..."

python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-probe \
  --symbol XRPUSDT \
  --side Sell \
  --notional 5 \
  --place-order \
  --i-understand-demo-order
```

Bybit demo sync for the core paper sleeve:

```bash
python -m aggression_carry \
  --data-root data/forward-paper/forward_sleeves/core_31_150 \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-sync \
  --submit-orders \
  --i-understand-demo-sync
```

Demo sync writes:

```text
data/.../reports/bybit_demo_sync_report.md
data/.../reports/bybit_demo_execution_orders.csv
data/.../demo_execution_orders/
```

These are demo-only plumbing checks. They do not prove the alpha and they are
not live execution.

Microcap paper mode is a separate sleeve, not the default core book:

```bash
python -m aggression_carry \
  --data-root data/forward-paper-microcap \
  --config configs/volume_alpha.default.yaml \
  forward-run \
  --top-n 3 \
  --gross-exposure 0.5 \
  --liquidity-rank-min 151 \
  --liquidity-rank-max 0 \
  --min-baseline-turnover 250000 \
  --min-day-turnover 750000 \
  --min-last-60m-turnover 75000 \
  --account-equity 10000 \
  --max-position-weight 0.20 \
  --max-trade-notional-pct-day-turnover 0.002 \
  --max-trade-notional-pct-baseline-turnover 0.005 \
  --min-turnover-24h 750000 \
  --max-spread-bps 80
```

The detailed backtest uses the current lead by default:

```text
score=dollar_volume_rank
quantile=50%
hold_days=7
rebalance_days=7
gross_exposure=1.0x
stop_loss_pct=8%
take_profit_pct=0% disabled
```

Override knobs from the CLI, for example:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3m \
  --config configs/volume_alpha.default.yaml \
  volume-backtest \
  --hold-days 3 \
  --rebalance-days 3 \
  --stop-loss-pct 0.05 \
  --take-profit-pct 0.12
```

To check whether the fixed stop is helping or hurting, run the same backtest
with stops disabled:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3m \
  --config configs/volume_alpha.default.yaml \
  volume-backtest \
  --stop-loss-pct 0
```

Grid test lifecycle assumptions:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-3m \
  --config configs/volume_alpha.default.yaml \
  volume-grid \
  --workers 0
```

`--workers 0` uses CPU count minus one. On a 5950X, `--workers 32` is valid if
RAM is comfortable; use `--workers 16` if memory pressure shows up. The RTX GPU
is not used yet because this backtester is a CPU process-parallel trade
simulation, not a vectorized CUDA workload.

The detailed `volume-backtest` report includes every trade, exit reasons,
monthly strategy performance versus BTC, BTC up/down regime summaries, and SVG
charts for the equity curve and monthly returns.

Bucket sweep after downloading a broad universe:

```bash
python scripts/run_volume_bucket_sweep.py \
  --data-root data/agc-bybit-1y-auto150-20250503-20260503 \
  --config configs/volume_alpha.default.yaml \
  --workers 0 \
  --include-reverse
```

This runs separate grids for daily liquidity-rank buckets: core `1-20`, mid
`21-80`, and tail `81-150`.

Daily close fade 1m grid:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  download-data \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT,BNBUSDT,ADAUSDT,DOTUSDT,LTCUSDT,NEARUSDT,OPUSDT,ARBUSDT,INJUSDT \
  --start 2026-02-03 \
  --end 2026-05-03 \
  --datasets instruments,klines_1m

python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade-grid \
  --signal-times 22:15 \
  --top-ns 5 \
  --hold-minutes 180 \
  --pump-filters pump \
  --liquidity-rank-mins 1,31,81,151 \
  --liquidity-rank-maxs 30,80,150,300 \
  --stop-loss-pcts 0.2 \
  --take-profit-pcts 0 \
  --trailing-stop-pcts 0 \
  --vol-trailing-stop-mults 0,0.25 \
  --vol-trailing-activation-mults 0,0.25,0.5 \
  --mfe-giveback-activation-pcts 0,0.01,0.02 \
  --mfe-giveback-pcts 0,0.20,0.33 \
  --gross-exposures 0.25,0.5,1.0 \
  --basket-stop-loss-pcts 0,0.05 \
  --workers 0
```

Point-in-time daily close fade setup:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  archive-manifest \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --quote-suffix USDT \
  --workers 16

python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  download-data \
  --symbols BTCUSDT \
  --start 2023-05-03 \
  --end 2023-05-04 \
  --datasets archive_klines_1m \
  --archive-url-template 'https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz'

python -m aggression_carry \
  --data-root data/daily-close-fade-pit \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --workers 32
```

The full point-in-time run must download archive-derived 1m bars for every
eligible symbol/date in the manifest, not just today's current universe, before
results can be treated as real alpha evidence. For USDT linear shorts, the
daily-close backtester now uses `(entry_price - exit_price) / entry_price`;
earlier exploratory reports from before that correction should be ignored.

Current daily-close paper-forward default is no fixed TP, 20% disaster stop,
baseline liquidity ranks 31-150, `0.25x` daily-vol trailing after the 15-minute
stop delay, and 20% MFE giveback after +1% favorable movement. In the current
local 3-year top-160 benchmark, the simple no-adaptive entry lost money, while
this adaptive 31-150 version returned +249.03% at base costs and +50.51% at 2x
costs. At 3x costs it lost money, so execution cost is a gating risk.
Current-universe results are still biased until the archive walk-forward path is
complete.

Compare the daily-close sleeves on any 1m dataset:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3m \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade-sleeves
```

The sleeve command writes `daily_close_fade_sleeves_report.md`,
`daily_close_fade_sleeves_results.csv`, and combined sleeve trade/basket CSVs.
The microcap sleeve starts at baseline liquidity rank 151+, top 3 names, 0.50x
gross, turnover floors, and capacity caps. Treat it as experimental until it
survives point-in-time archive data and paper forward fills.

Windows 1m runner:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_close_fade_1m.ps1 -Workers 32
```

One-year grid on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_1y_grid.ps1 -Workers 32
```

## Research Rule

Do not combine signals until a single alpha clears costs standalone. The volume
rank path and the daily-close fade path are separate systems for now. A future
multistrat allocator only makes sense after each one has its own profitable,
cost-adjusted trade ledger.
