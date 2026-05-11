# MODEL050426

Bybit crypto research repo. This is a stripped-down alpha lab, not a real-money
trading bot.

## Current Truth

Active research lead: **daily-close short fade with 22:00-23:00 TWAP entry**.

Current implementation contract:

```text
Universe: Bybit USDT linear perps
Ranking time: 22:00 UTC, using only data available at 22:00
Entry: equal 1m TWAP from 22:00 through 22:59
Entry price: average fill price
Exit: whole-symbol flatten, no same-symbol re-entry that day
Max hold: 180m after TWAP completes
Disaster stop: 20% above average entry, active immediately from first fill
Adaptive protection: 0.25x daily-vol trail plus 20% MFE giveback after +1% MFE,
  active only after final add + 15m
Adaptive protection state: starts at activation time; it does not inherit
  pre-activation lows from the TWAP/delay window
Sizing: score-capped, max 80% basket weight per symbol
Liquidity: prior 7d baseline quote-turnover ranks 31-150
Candidate gate: coin excess vs market >= 8%, VWAP extension >= 3.5%,
  late-volume ratio >= 1.0x
```

The 2026-05-08 profit-protection audit invalidated the old headline benchmark:

```text
Old +16,991.58% run: legacy warm-started adaptive protection
Immediate-exit artifact: 647 / 750 trades exited by post-TWAP minute 16
Basket clustering: 342 / 435 baskets had every trade exit by minute 16
Corrected default rerun: -4.75%, Sharpe-like 0.22, max DD -53.15%
Corrected 216-variant grid: 0 variants positive in all train/validation/OOS splits
```

The old local current-top-160 benchmark is retained only as a legacy artifact:

```text
Data: current top-160 Bybit symbols, 2023-05-15 to 2026-05-02
Trades: 750
Total return: +16,991.58%
Sharpe-like: 10.65
Max drawdown: -15.39%
Worst day: -12.84%
Report: data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/
```

This is **not final alpha proof** because it is current-universe biased and
because its adaptive exits used the now-corrected warm-start behavior. The
point-in-time archive path is still the proof gate.

All future research claims must pass
[docs/backtesting_errors_we_never_repeat.md](docs/backtesting_errors_we_never_repeat.md).
If a run violates that document, it is not alpha evidence.

## Important Boundary

Backtest TWAP is implemented. Forward/demo TWAP slicing is intentionally blocked
until explicit per-slice paper and demo order accounting exists. The code must
not fake this as one 22:00 or 23:00 fill.

## Main Files

- [configs/volume_alpha.default.yaml](configs/volume_alpha.default.yaml): active
  research config.
- [docs/daily_close_fade.md](docs/daily_close_fade.md): active strategy notes.
- [docs/profit_protection_audit_20260508.md](docs/profit_protection_audit_20260508.md):
  audit that invalidated the old close-fade headline benchmark.
- [docs/backtesting_errors_we_never_repeat.md](docs/backtesting_errors_we_never_repeat.md):
  permanent backtesting-error standard and promotion checklist.
- [docs/walk_forward_universe.md](docs/walk_forward_universe.md): PIT proof
  standard.
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

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_setup.ps1
```

## Reproduce Legacy Daily-Close Forensics

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

This reruns the corrected implementation on the old current-top-160 dataset.
Use it for forensic comparison only; it is not a promotion path.

Core outputs:

```text
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/reports/daily_close_fade_report.md
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/daily_close_fade_trades/
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/daily_close_fade_baskets/
```

Legacy readable export:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/trades_all.csv
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/equity_curve.svg
```

## Windows Overnight Suite

```powershell
cd C:\Users\user\Desktop\MODEL05042026
git pull --rebase --autostash
powershell -ExecutionPolicy Bypass -File .\scripts\run_research_overnight_suite.ps1 -Suite both -Workers 8
```

If the daily-close 1m dataset is missing, `-Suite both` skips that leg and still
runs the volume sweep. Use `-Suite daily-close` when missing 1m data should be a
hard failure.

The volume leg now defaults to the `promotion` preset. That preset tests actual
volume-change scores plus dollar-volume rank, then runs train/validation/OOS
promotion gates by liquidity bucket. It is slower than the old headline grid,
but much more useful.

Each overnight suite also writes an auditable research record:

```text
data/research_reports/research_log/research_log.md
data/research_reports/research_log/runs/<run_id>.md
```

Review that log before changing research configs or rerunning a similar idea.

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
tooling were intentionally removed. Demo deployment wrappers are also out of
scope. Do not rebuild them into this research repo.
