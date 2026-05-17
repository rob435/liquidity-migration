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
- Threshold: top 40% dollar-volume rank migration
- Universe: point-in-time liquidity rank 31-150
- Exclusions: stable/peg perps only, including failed peg remnants such as `USTCUSDT`
- Rank improvement: at least 150 places versus the 7-day prior rank
- Turnover expansion: current turnover / prior 7-day mean turnover at least 6.0
- Overheat filter: event rank fraction no higher than 0.90
- Idiosyncratic move gate: `daily_return_1d - market_median_return_1d >= +8%`
- Regime gate: `market_pct_up_1d <= 0.65 OR coin daily_return_1d` clears the adaptive 16% +/- 1.5% hot-market band
- Strong-close gate: signal-day close location at least 0.45
- Maturity gate: PIT/listing age at least 90 days
- Crowding veto: `union_pathology` same-entry-hour pathology filter
- Entry: 1 hour after the daily signal close
- Exit: event decay, rank exit, 12% fixed stop, 25% fixed take profit, or 3-day max hold
- Capacity: max 5 active symbols, 5-day symbol cooldown
- Stop-pressure throttle: pause new entries after 7 realized stops inside 10 days
- Realized-loss throttle: pause new entries after 6 realized losses inside 5 days
- Cost model: 3x base round-trip costs
- Gross exposure: 1.00, split across active capacity

Legacy full-PIT baseline run, 2023-05-03 to 2026-05-03:

- Trades: 516
- Total return: +1218.79%
- Max drawdown: -14.54%
- Max no-new-high stretch: 90 days
- Worst 90d return: -5.89%
- Worst split return: +75.64%
- Average split Sharpe-like: 2.67
- OOS return: +111.75%
- Default chart: `volume_event_best_equity_btc.png` with BTC overlay and monthly/growth gridlines
- Promotion gate: pass

Reference report:

```text
data/research_reports/research_20260516_promoted_default_after_patch
```

Promoted research frontier after the same-hour crowding audit:

- Variant: adaptive hot-band liquidity migration with `union_pathology` crowding veto
- Report: `data/research_reports/frontier_union_crowding_promoted_20260517`
- Trades: 444
- Total return: +2143.28%
- Max drawdown: -11.05%
- Worst 90d return: -4.80%
- Worst split return: +118.65%
- Average split Sharpe-like: 3.72
- OOS return: +186.06%

These promoted frontier metrics are the clean `1.00` gross exposure rescale of
the existing full-PIT ledger; the event set, exits, cooldowns, and crowding
decisions are unchanged by the gross cleanup.

This supersedes the earlier March-specific crowding patch and is now the
research, `volume-events`, and Bybit demo default as of 2026-05-17. The paper
shadow step was intentionally skipped by user decision; keep that risk
contained to demo-only trading.

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

## Bybit Demo Forward Runner

One dry-run cycle:

```bash
python -m aggression_carry \
  --data-root data/bybit-demo-event \
  --config configs/volume_alpha.default.yaml \
  event-demo-cycle
```

Continuous demo runner, checking every 5 minutes by default:

```bash
TELEGRAM_ENABLED=1 \
SUBMIT_ORDERS=1 \
CONFIRM_DEMO_ORDERS=1 \
BYBIT_DEMO_API_KEY=... \
BYBIT_DEMO_API_SECRET=... \
bash scripts/run_bybit_demo_event_engine.sh
```

Default forward-test behavior:

- pulls current Bybit USDT perp ranks 1-220, then applies the selected rank 31-150 liquidity-migration filter
- rebuilds recent 1h volume features each cycle from a 45-day lookback, using a forward-demo kline cache to fetch only missing/new bars on normal cycles
- enters eligible events after the 1-hour signal delay, with stale entries skipped after 15 minutes by default
- sizes each coin from the backtest weight: `gross_exposure / max_active_symbols`, currently `1.00 / 5 = 20.00%` of current Bybit demo USDT equity
- uses 2x entry leverage in the continuous runner for margin headroom without changing the notional sizing
- exits first on every cycle using fixed stop reconciliation, event decay, rank exit, or 3-day max hold
- sends Telegram status with Bybit demo wallet equity, open positions, position value, and unrealized PnL when `TELEGRAM_ENABLED=1`
- writes ledgers under `event_demo_trades`, `event_demo_orders`, and `event_demo_cycles`
- the separate websocket risk watchdog writes latest reports under `reports/event-risk-ws` and keeps timestamped audit copies for startup/material events

## Useful Files

- `aggression_carry/volume_events.py`: active event-driven strategy, full-PIT gates, ledger, reports
- `aggression_carry/event_demo.py`: Bybit demo forward-cycle runner for the selected event strategy
- `aggression_carry/ws_risk.py`: websocket-first risk watchdog with REST fallback and audit reports
- `aggression_carry/archive_manifest.py`: PIT manifest and 1h kline builders
- `aggression_carry/volume_features.py`: active daily volume and liquidity-rank feature builder
- `aggression_carry/trade_lifecycle.py`: active trade lifecycle, exit, basket, and equity helpers
- `scripts/run_bybit_demo_event_engine.sh`: continuous Bybit demo forward runner
- `scripts/run_bybit_demo_ws_risk_engine.sh`: continuous websocket risk watchdog
- `scripts/prove_bybit_demo_order_lifecycle.py`: guarded demo order-path proof for strategy-sized short entry, native protection, reduce-only exit, and WebSocket Trade availability
- `scripts/run_fullpit_volume_overnight.sh`: selected full-PIT runner
- `scripts/run_fullpit_volume_overnight.ps1`: PowerShell selected full-PIT runner
- `deploy/systemd/model050426-bybit-demo.service`: VPS service definition for the active demo runner
- `docs/volume_alpha.md`: strategy notes and current result
- `docs/bybit_aggression_carry_system_codex_spec.md`: active system spec
