# Bybit Demo Trading System Spec

## Objective

The project focus is a profitable Bybit demo-account system built around the selected full-PIT liquidity-migration short strategy. The old fixed daily-close short fade is retired and removed from the active repo surface.

Work should improve one of four things:

- point-in-time data quality
- event feature quality
- event-driven backtest and risk realism
- demo execution parity for the selected event lifecycle

The current private Bybit client is demo-only and refuses `demo=False`. Real-money support is a separate implementation, not a hidden mode.

## Active Strategy

Canonical command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events
```

`volume-events` defaults are the selected full-PIT strategy:

```text
event: liquidity_migration
side: reversal / short
threshold: top 30% dollar-volume rank migration
PIT liquidity rank: 31-150
excluded: stable/peg perps only, including failed peg remnants such as USTCUSDT
rank improvement: >= 150 versus prior 7d liquidity rank
turnover expansion: current turnover / prior 7d mean >= 6.0
event rank fraction cap: <= 0.90
regime gate: market_pct_up_1d <= 0.60 OR coin daily_return_1d >= +20%
entry delay: 1 hour after daily signal close
max hold: 1 day
stop: 12% fixed
gross exposure: 1.0
max active symbols: 6
symbol cooldown: 5 days
stop-pressure throttle: pause after 12 realized stops inside 14 days
cost multiplier: 3x base round-trip cost
```

The strategy is short-only because the best full-PIT evidence is in reversal after liquidity migration. Long continuation is not promoted unless a fresh full-PIT run clears costs, splits, drawdown, and report gates.

## Reference Evidence

Promoted full-PIT report after switching to stable/peg-only exclusions and the
liquidity-migration OR regime gate:

```text
data/agc-bybit-fullpit-1h-20230503-20260503/reports/research_20260516_promoted_default_stable_peg_or_gate
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
trades: 810
total return: +344.73%
max drawdown: -16.86%
worst split return: +24.58%
worst split drawdown: -15.43%
average split Sharpe-like: 2.44
train return: +24.58%
validation return: +91.49%
OOS return: +86.42%
default chart: volume_event_best_equity_btc_spy.png with BTC/SPY overlays and small red/green strategy return markers
promotion gate: pass
```

Top coins are no longer manually blacklisted. In the promotion comparison,
BTC/ETH/SOL/BNB/XRP/TRX took no direct trades, but including them improved the
point-in-time rank universe and market context used by the alpha.

Funding is not modeled in that root because funding data is missing. Fees and slippage are stress-tested through the 3x cost multiplier; funding remains a required demo/forward parity item before real-money work.

## Data Contract

Serious strategy runs must use:

- `archive_trade_manifest` as point-in-time symbol/date membership
- `klines_1h` coverage for every manifest symbol/date in the run window
- causal daily signal features, with entry delayed to the next configured 1h bar
- full trade ledger, basket ledger, equity curve, BTC/SPY overlay chart, monthly returns, JSON config, and Markdown report

`volume-events` requires full PIT coverage by default. `--allow-partial-pit` is only for explicitly biased diagnostics and must not be used as promotion evidence.

## Execution Status

The old daily-close demo executor was intentionally removed because it encoded the retired calendar-clock candidate scan and sleeve assumptions. Do not revive it.

The active demo execution path is `event-demo-cycle` plus the continuous runner:

```bash
python -m aggression_carry \
  --data-root data/bybit-demo-event \
  --config configs/volume_alpha.default.yaml \
  event-demo-cycle
```

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_event_engine.sh
```

The runner loops every `INTERVAL_SECONDS=60` by default. Each cycle:

1. Pulls the current Bybit USDT perpetual universe through rank 220 so the selected rank 31-150 strategy can be evaluated forward.
2. Excludes only stable/peg perps, including failed peg remnants such as USTCUSDT, before ranks/features are built.
3. Rebuilds recent 1h volume features from a 45-day lookback.
4. Exits existing demo positions first on fixed-stop reconciliation, event decay, rank exit, or 1-day max hold.
5. Enters accepted liquidity-migration events after the 1-hour signal delay, subject to max-active, cooldown, stop-pressure, and stale-entry gates. Stale entries are skipped after 15 minutes by default so demo fills stay close to the backtest entry timestamp.
6. Sizes each accepted coin from the same weight used by the backtest: `gross_exposure / max_active_symbols`, currently `1.0 / 6 = 16.67%` of current Bybit demo USDT equity. `--max-order-notional-pct-equity` is only an explicit override.
7. Sends Telegram status with wallet equity, Bybit demo open positions, position value, and unrealized PnL when Telegram is enabled.
8. Writes expected/submitted order state into `event_demo_orders`, trade state into `event_demo_trades`, cycle telemetry into `event_demo_cycles`, and Markdown/JSON reports under `reports/event-demo`.

Order submission is still fail-closed: `--submit-orders` requires `--confirm-demo-orders`, `BYBIT_DEMO_API_KEY`, and `BYBIT_DEMO_API_SECRET`. Without those, the command is a dry-run scan.

Telegram may notify, but it must not approve or submit orders. The continuous runner now fails startup when `TELEGRAM_ENABLED=1` but Telegram or Bybit demo credentials are missing, because position/PnL reporting would be incomplete.
