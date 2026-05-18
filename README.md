# MODEL050426

Bybit demo-account research system for the selected full-PIT liquidity-migration short strategy.

## Current Objective

The repo now has one strategy focus: event-driven PIT liquidity migration. The old fixed daily-close short-fade demo stack has been removed from the active code path so it cannot keep steering work back to calendar-clock entries.

The private Bybit client remains demo-only by design: `demo=False` is refused in code. Real-money trading requires a separate explicit implementation decision.

Current operational proof and deployment status are summarized in
`docs/system_status.md`; dated one-off audit logs have been retired from the
repo surface.

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
- Research-only crowding classifier: `model_v1` exists for audits and
  experiments, but is not promoted or deployed
- Entry policy: `promoted_quality_squeeze`; standard events enter 1 hour after the daily signal close, while promoted-grade squeeze events wait for a causal 25 bps high-since-signal giveback after a 25 bps pop or enter on the 4h deadline
- Research-only execution variants: `execution_pullback_guard`,
  `tiered_execution_sniper`, and `entry_execution_veto_close_location_max`
  exist for audits, but are not promoted or deployed
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

Promoted research frontier after the same-hour crowding audit and conservative
quality-squeeze entry router:

- Variant: adaptive hot-band liquidity migration with `union_pathology` crowding veto
- Report: `/Users/jhbvdnsbkvnsd/agc-bybit-fullpit-funded-20230503-20260503/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md`
- Trades: 444
- Total return, no funding: +2285.54%
- Max drawdown, no funding: -11.05%
- Worst 90d return, no funding: -5.02%
- Worst split return, no funding: +118.81%
- OOS return, no funding: +210.35%
- Total return, funding stress: +1853.99%
- Max drawdown, funding stress: -13.72%
- OOS return, funding stress: +175.32%

The event set, exits, cooldowns, gross exposure, and crowding decisions are
unchanged versus the union promoted path; only the causal post-signal entry
timing changed for promoted-grade squeeze bars.

This remains the promoted research default for `volume-events`. The live demo
entry service can run a separate higher-frequency observation profile, described
below, but that profile is explicitly a demo test system rather than promoted
alpha.

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

## Model Court

Run the formal promotion court after a `volume-events` report exists:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  strategy-tribunal \
  --report-dir DATA_ROOT/reports/volume_event_research \
  --comparison-csv DATA_ROOT/reports/stress_summary.csv \
  --comparison-family promoted_funding \
  --pre-registered-window train:2023-05-03:2024-05-03,validation:2024-05-03:2025-05-03,oos:2025-05-03:2026-05-03 \
  --execution-data-root DATA_ROOT
```

The court reads the scenario summary, best trades, baskets, equity curve, and
stress/sweep CSVs. It writes `strategy_tribunal_report.md/json` plus pairwise
parameter heatmap CSVs with artifact checks, promotion recap,
recomputed-vs-reported path consistency, explicit preregistered window results,
block-bootstrap left-tail stress, random-sign/inverted-edge/shuffled-symbol/
shuffled-time/shuffled-event negative controls, filtered cost/funding/slippage
stress diagnostics, monthly regime diagnostics, live-vs-backtest execution
drift, symbol concentration, and same-hour entry crowding. Use
`--comparison-family` to keep the audit on the exact strategy family being
judged. Missing execution data is a `WATCH`, not proof.

## Bybit Demo Forward Runner

One dry-run cycle:

```bash
python -m aggression_carry \
  --data-root data/bybit-demo-event \
  --config configs/volume_alpha.default.yaml \
  event-demo-cycle
```

Continuous demo runner, checking every 5 minutes by script default. The VPS
systemd entry service intentionally overrides this to `INTERVAL_SECONDS=60` and
`STRATEGY_PROFILE=demo_relaxed`:

```bash
TELEGRAM_ENABLED=1 \
SUBMIT_ORDERS=1 \
CONFIRM_DEMO_ORDERS=1 \
BYBIT_DEMO_API_KEY=... \
BYBIT_DEMO_API_SECRET=... \
bash scripts/run_bybit_demo_event_engine.sh
```

Default forward-test behavior:

- `STRATEGY_PROFILE=demo_relaxed` is a test-only relaxed-gate profile. It keeps the same short liquidity-migration idea, `union_pathology` crowding veto, and conservative `promoted_quality_squeeze` router for promoted-grade events, but uses ranks 11-260, no extra current 24h turnover floor, 80-rank improvement, 3.0x turnover expansion, -3% day-return floor, +3% residual-return floor, 0.25 close-location floor, 10 max active symbols, and 2-day cooldown.
- full-PIT funded observation evidence: 1,268 trades, +221.29% total return, -21.32% max drawdown, -18.90% worst 90d, +12.36% worst split, +142.92% OOS, promotion gate pass. Report: `/Users/jhbvdnsbkvnsd/agc-bybit-fullpit-funded-20230503-20260503/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md`.
- pulls current Bybit USDT perp ranks 1-300 for demo_relaxed mode, then applies the selected strategy profile's rank filter
- rebuilds recent 1h volume features each cycle from a 45-day lookback, using a forward-demo kline cache to fetch only missing/new bars on normal cycles
- enters eligible events through the causal entry router, with stale entries skipped after 15 minutes by default
- sizes each coin from the backtest weight: `gross_exposure / max_active_symbols`, currently `1.00 / 10 = 10.00%` of current Bybit demo USDT equity in demo_relaxed mode
- uses 2x entry leverage in the continuous runner for margin headroom without changing the notional sizing
- exits first on every cycle using fixed stop reconciliation, event decay, rank exit, or 3-day max hold
- sends Telegram status with Bybit demo wallet equity, open positions, position value, and unrealized PnL when `TELEGRAM_ENABLED=1`
- writes ledgers under `event_demo_trades`, `event_demo_orders`, and `event_demo_cycles`
- the separate websocket risk watchdog writes latest reports under `reports/event-risk-ws` and keeps timestamped audit copies for startup/material events

## Useful Files

- `aggression_carry/volume_events.py`: active event-driven strategy, full-PIT gates, ledger, reports
- `aggression_carry/strategy_tribunal.py`: adversarial robustness audit for completed strategy reports
- `aggression_carry/event_demo.py`: Bybit demo forward-cycle runner for the selected event strategy
- `aggression_carry/ws_risk.py`: websocket-first risk watchdog with REST fallback and audit reports
- `aggression_carry/archive_manifest.py`: PIT manifest and 1h kline builders
- `aggression_carry/volume_features.py`: active daily volume and liquidity-rank feature builder
- `aggression_carry/trade_lifecycle.py`: active trade lifecycle, exit, basket, and equity helpers
- `scripts/run_bybit_demo_event_engine.sh`: continuous Bybit demo forward runner
- `scripts/run_bybit_demo_ws_risk_engine.sh`: continuous websocket risk watchdog
- `scripts/run_fullpit_volume_overnight.sh`: selected full-PIT runner
- `scripts/run_fullpit_volume_overnight.ps1`: PowerShell selected full-PIT runner
- `deploy/systemd/model050426-bybit-demo.service`: VPS service definition for the active demo runner
- `docs/system_status.md`: current deployment and demo order-path proof summary
- `docs/volume_alpha.md`: strategy notes and current result
- `docs/bybit_aggression_carry_system_codex_spec.md`: active system spec
