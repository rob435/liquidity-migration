# MODEL050426

Bybit crypto research repo. This is a stripped-down alpha lab, not a real-money
trading bot.

## Current Truth

Active research lead: **daily-close short fade with 22:01-23:00 TWAP entry**.

Current promoted research contract:

```text
Universe: Bybit USDT linear perps
Ranking time: 22:00 UTC, using only bars ended by 22:00
Entry: 60 equal 1m TWAP slices from 22:01 through 23:00
Entry price: average fill price
Exit: whole-symbol flatten, no same-symbol re-entry that day
Max hold: 180m after TWAP completes
Disaster stop: 20% above average entry, active immediately from first fill
Adaptive protection: 0.25x daily-vol trail plus 20% MFE giveback after +1% MFE,
  active only after final scheduled add + 15m
Sizing: score-capped, max 80% basket weight per symbol
Liquidity: prior 7d baseline quote-turnover ranks 31+; top 30 excluded
Candidate gate: coin excess vs market >= 8%, VWAP extension >= 3.5%,
  late-volume ratio >= 1.0x
```

The latest local current-top-160 benchmark is deliberately labeled as biased
and should be rerun before comparison under the strict 22:00 bar-availability
convention:

```text
Data: current top-160 Bybit symbols, 2023-05-15 to 2026-05-02
Trades: 750
Total return: +16,991.58%
Sharpe-like: 10.65
Max drawdown: -15.39%
Worst day: -12.84%
Report: data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/
```

This is **not final alpha proof** because the current-top-160 universe is
survivorship-biased. The point-in-time archive path is still the proof gate.

## Important Boundary

Backtest, forward paper, and Bybit demo shadowing now use explicit TWAP child
slices. The code must not fake this as one 22:00 or 23:00 fill. The 22:00 rank
uses only minute-open bars that have ended by 22:00; the first entry child is
still the 22:01 open.

## Main Files

- [configs/volume_alpha.default.yaml](configs/volume_alpha.default.yaml): active
  research config.
- [docs/daily_close_fade.md](docs/daily_close_fade.md): active strategy notes.
- [docs/forward_testing.md](docs/forward_testing.md): forward/demo boundary.
- [docs/volume_alpha.md](docs/volume_alpha.md): secondary volume-alpha research.
- [docs/bybit_aggression_carry_system_codex_spec.md](docs/bybit_aggression_carry_system_codex_spec.md):
  compressed Bybit data/API reference; old composite spec is no longer active.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Run Current Daily-Close TWAP Backtest

Requires the 1m dataset:

```text
data/daily-close-fade-1m-3y-current-top160-20230503-20260503
```

Run:

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3y-current-top160-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade
```

Core outputs:

```text
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/reports/daily_close_fade_report.md
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/daily_close_fade_trades/
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/daily_close_fade_entry_fills/
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/daily_close_fade_baskets/
```

Readable export from the latest run:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/trades_all.csv
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/equity_curve.svg
```

## Active Helper Scripts

Only the current runtime and volume-alpha helpers are kept:

```text
scripts/install_bybit_demo_systemd.sh
scripts/run_bybit_demo_engine.sh
scripts/run_bybit_demo_cycle_with_audit.sh
scripts/run_forward_signal_with_audit.sh
scripts/run_volume_bucket_sweep.py
scripts/run_volume_grid_splits.py
scripts/evaluate_volume_promotion.py
```

## Point-In-Time Proof

Do not promote the TWAP result to real money until:

1. Bybit archive symbol/date membership is complete.
2. Archive-derived 1m bars cover all eligible symbols, not only current winners.
3. The same TWAP contract survives train/validation/OOS splits.
4. Forward paper/demo implements real TWAP slices and the audit shows acceptable
   missed fills, slippage, and execution drift.

## Removed

The old live runtime, blended signal stack, Telegram trading bot behavior,
legacy SignalEngine/execution/state/alerting modules, and repo-local agent
tooling were intentionally removed. Do not rebuild them into this research repo.
