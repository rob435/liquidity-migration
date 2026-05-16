# MODEL050426

Bybit demo-account research system for the selected full-PIT liquidity-migration short strategy.

## Current Objective

The repo now has one strategy focus: event-driven PIT liquidity migration. The old fixed daily-close short-fade demo stack has been removed from the active code path so it cannot keep steering work back to calendar-clock entries.

The private Bybit client remains demo-only by design: `demo=False` is refused in code. Real-money trading requires a separate explicit implementation decision.

## Active Strategy

Command:

```bash
python -m aggression_carry --data-root DATA_ROOT --config configs/volume_alpha.default.yaml volume-events
```

The `volume-events` defaults are the selected strategy:

- Event: `liquidity_migration`
- Side: reversal, which means short
- Threshold: top 30% dollar-volume rank migration
- Universe: point-in-time liquidity rank 31-150
- Rank improvement: at least 150 places versus the 7-day prior rank
- Turnover expansion: current turnover / prior 7-day mean turnover at least 6.0
- Overheat filter: event rank fraction no higher than 0.90
- Entry: 1 hour after the daily signal close
- Exit: event decay, 12% fixed stop, or 1-day max hold
- Capacity: max 6 active symbols, 5-day symbol cooldown
- Stop-pressure throttle: pause new entries after 12 realized stops inside 14 days
- Cost model: 3x base round-trip costs
- Gross exposure: 1.0, split across active capacity

Latest full-PIT reference run, 2023-05-03 to 2026-05-03:

- Trades: 1,138
- Total return: +466.57%
- Max drawdown: -20.34%
- Worst split return: +34.72%
- Worst split drawdown: -21.06%
- Average split Sharpe-like: 2.19
- Promotion gate: pass

Reference report:

```text
data/agc-bybit-fullpit-1h-20230503-20260503/reports/SELECTED_liqmig_dd_repair_turn6_rank31_150_eventcap90_stoppressure_20260516
```

## Full-PIT Runner

Linux/macOS:

```bash
bash scripts/run_fullpit_volume_overnight.sh
```

PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_fullpit_volume_overnight.ps1
```

The runner syncs `main`, installs the local Python environment, runs smoke tests, builds/resumes the full Bybit public archive manifest, fills full PIT 1h klines from the Bybit v5 API, validates manifest coverage, then runs the selected liquidity-migration strategy.

## Useful Files

- `aggression_carry/volume_events.py`: active event-driven strategy, full-PIT gates, ledger, reports
- `aggression_carry/archive_manifest.py`: PIT manifest and 1h kline builders
- `aggression_carry/volume_alpha.py`: reusable feature builder for event research
- `aggression_carry/volume_backtest.py`: reusable trade/equity/cost helpers used by event research
- `scripts/run_fullpit_volume_overnight.sh`: selected full-PIT runner
- `scripts/run_fullpit_volume_overnight.ps1`: PowerShell selected full-PIT runner
- `docs/volume_alpha.md`: strategy notes and current result
- `docs/bybit_aggression_carry_system_codex_spec.md`: active system spec
