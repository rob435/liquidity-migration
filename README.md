# MODEL050426

Crypto trading research codebase. The previous live strategy spec has been removed because the system logic is being replaced.

## Current Posture

- The authoritative current plan is [docs/bybit_aggression_carry_system_codex_spec.md](docs/bybit_aggression_carry_system_codex_spec.md).
- The new alpha-proof research path lives in `aggression_carry/`.
- The existing signal/execution runtime is legacy and not a design target for the new system.
- The backtester, cache bundle helper, reconciliation tooling, database helpers, and tests remain useful scaffolding.
- No production deployment files are authoritative right now.
- Do not infer live-trading readiness from old passing tests.

## Useful Commands

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Aggression-carry fixture pipeline:

```bash
python -m aggression_carry --data-root .tmp/agc-fixture download-data --fixture
python -m aggression_carry --data-root .tmp/agc-fixture build-features
python -m aggression_carry --data-root .tmp/agc-fixture alpha-report
python -m aggression_carry --data-root .tmp/agc-fixture portfolio-backtest
```

The alpha report writes:

- `research_returns`
- `research_timestamp_ic`
- `research_quantile_ledger`
- `research_monthly_spreads`
- `reports/alpha_report.md`

The portfolio backtest writes:

- `portfolio_periods`
- `portfolio_positions`
- `portfolio_symbol_attribution`
- `portfolio_monthly_attribution`
- `reports/portfolio_backtest.md`

Bybit REST data download skeleton:

```bash
python -m aggression_carry \
  --config configs/aggression_carry.default.yaml \
  download-data \
  --symbols BTCUSDT,ETHUSDT \
  --start 2025-01-01 \
  --end 2025-01-08 \
  --datasets instruments,klines_1h,klines_5m,funding,open_interest,ticker_snapshots,recent_trades
```

Bybit historical public-trade archive smoke run:

```bash
python -m aggression_carry \
  --data-root data/agc-bybit-smoke \
  --config configs/aggression_carry.default.yaml \
  download-data \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT \
  --start 2025-01-01 \
  --end 2025-01-08 \
  --datasets instruments,klines_1h,klines_5m,funding,open_interest,ticker_snapshots,archive_trades \
  --archive-url-template "https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz"
```

Recent trades alone are not enough to prove the aggression alpha over history. Use Bybit's daily public-trade `.csv.gz` archive for signed taker flow, and keep 5m candles as supporting path/cost data.

Backtest entrypoint:

```bash
python backtest.py --help
```

Cache bundle helper:

```bash
python deploy/cache_bundle.py --help
```

Codex companion tools:

```bash
python deploy/setup_codex_tools.py
```

See [CODEX_TOOLS.md](CODEX_TOOLS.md) for the agent tooling setup.

## Repo Layout

- `aggression_carry/`: isolated Bybit aggression-carry alpha-proof package.
- `configs/aggression_carry.default.yaml`: research config for the new package.
- `backtest.py`: historical replay/backtest harness retained for reuse.
- `exchange.py`: Bybit market-data and trade-client code; keep under review during overhaul.
- `database.py`: SQLite persistence helpers.
- `reconcile.py` / `report.py`: analysis and export tooling.
- `config.py`: current settings surface, likely to shrink once the new strategy is defined.
- `signal_engine.py` / `execution.py` / `main.py`: legacy live runtime path, not a design target.
- `tests/`: regression coverage for the current code, useful as a safety net while refactoring.

## Before The Overhaul

Read [docs/bybit_aggression_carry_system_codex_spec.md](docs/bybit_aggression_carry_system_codex_spec.md) first. That file is the source of truth for the Bybit aggression-carry alpha-proof overhaul. Do not wire new research code into the legacy live runtime.
