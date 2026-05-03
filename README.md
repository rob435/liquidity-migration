# MODEL050426

Bybit crypto research repo. This has been stripped down around one alpha family:
daily volume / dollar-volume ranking. The old live bot and blended signal stack
are intentionally gone.

## Current Source Of Truth

- Current implementation plan: [docs/volume_alpha.md](docs/volume_alpha.md)
- Bybit data constraints/background: [docs/bybit_aggression_carry_system_codex_spec.md](docs/bybit_aggression_carry_system_codex_spec.md)
- Windows setup: [docs/WINDOWS_QUICKSTART.md](docs/WINDOWS_QUICKSTART.md)

The Bybit aggression-carry spec is still useful for venue/data details, especially
the warning that taker aggression requires signed public trades. It is not a
license to rebuild the old composite stack.

## What Exists

- `aggression_carry/`: public data download, Parquet storage, signed-flow parsing,
  fixture data, isolated `volume-alpha` research sweep, and detailed
  `volume-backtest` trade-ledger backtest.
- `configs/volume_alpha.default.yaml`: current research config.
- `scripts/run_agc_3m.ps1`: Windows 3-month volume-alpha run.
- `scripts/run_volume_bucket_sweep.py`: daily liquidity-rank bucket grid runner.
- `deploy/setup_codex_tools.py`: optional Codex/Graphify/AO/Composio helper setup.

## What Was Removed

- Old live runtime: `main.py`, `execution.py`, `signal_engine.py`, `state.py`,
  `runtime_monitor.py`, `runtime_validation.py`, `alerting.py`.
- Old root research/backtest plumbing tied to that runtime.
- Old blended aggression/carry/momentum/quality/OI feature/report/sweep modules.
- Tests that only protected the deleted behavior.

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

One-year grid on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_1y_grid.ps1 -Workers 32
```

## Research Rule

Do not combine signals until a single alpha clears costs standalone. The latest
lead is `dollar_volume_rank`; the previous increasing-volume variants failed in
the corrected 3-month test. The current broad-universe test found a separate
tail-liquid lead where the best direction reversed to `short_high_long_low`, so
that must be researched separately instead of being blended into the original
result.
