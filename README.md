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
  fixture data, and the isolated `volume-alpha` backtest.
- `configs/volume_alpha.default.yaml`: current research config.
- `scripts/run_agc_3m.ps1`: Windows 3-month volume-alpha run.
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
```

Report:

```text
data/agc-bybit-3m/reports/volume_alpha_report.md
```

## Research Rule

Do not combine signals until a single alpha clears costs standalone. The latest
lead is `dollar_volume_rank`; the previous increasing-volume variants failed in
the corrected 3-month test.
